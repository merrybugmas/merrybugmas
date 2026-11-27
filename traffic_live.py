# -*- coding: utf-8 -*-
"""트래픽 실연동 (읽기 전용) — Prometheus 형식 /metrics 의 요청 수 카운터를 읽어 rps 를 낸다.

  python3 xmas_market.py --live      # MockSource.rps 대신 실제 서버의 초당 요청 수로 줄을 세운다

Grafana 를 거치지 않고 서버의 /metrics 를 직접 읽는다 (Prometheus 가 긁어 가는 것과 같은 방식).
카운터(누적 요청 수)의 증가분 ÷ 지난 시간 = rps. 표준 라이브러리만 쓴다.

같은 /metrics 에 아래 지표가 있으면 함께 읽는다 (없으면 그 항목만 비워 둔다)
  http_requests_total{status="5xx"}                      → 5xx 비율 (최근 10초)
  http_request_duration_seconds_sum / _count             → 평균 응답 시간 (최근 10초)
  web_vitals_lcp_seconds · web_vitals_cls · web_vitals_inp_seconds · web_vitals_ttfb_seconds
                                                         → 브라우저가 보고한 Web Vitals (서버가 낸 p75)

환경변수 (레포의 .env 에 적어도 된다)
  TRAFFIC_METRICS_URL  필수. 예: http://localhost:8765/metrics  (demo-web 서버)
  TRAFFIC_METRIC       카운터 이름 (기본 http_requests_total, 라벨이 여러 개면 모두 더한다)
  TRAFFIC_PEAK         "최고치"로 볼 rps (기본 100). 줄 길이·인파 경보는 rps ÷ 이 값으로 정한다
  TRAFFIC_POLL         읽는 주기, 초 (기본 1)
"""
import os
import threading
import time
import urllib.request

STALE_SEC = 5.0  # 이만큼 못 읽으면 0 rps 로 본다 (마지막 값을 계속 보여 주지 않는다)


def configured():
    return bool(os.environ.get("TRAFFIC_METRICS_URL", "").strip())


WINDOW_SEC = 10.0  # 5xx 비율·응답 시간을 내는 구간
VITALS = (("lcp_s", "web_vitals_lcp_seconds"), ("cls", "web_vitals_cls"),
          ("inp_s", "web_vitals_inp_seconds"), ("ttfb_s", "web_vitals_ttfb_seconds"))


def parse_samples(text):
    """Prometheus 텍스트 → [(이름, 라벨 문자열, 값)]. 값이 숫자가 아닌 줄(NaN 포함)은 건너뛴다."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(None, 1)[0]
        labels = line[line.index("{") + 1:line.index("}")] if "{" in line and "}" in line else ""
        rest = line[line.index("}") + 1:] if "{" in line and "}" in line else line[len(name):]
        try:
            v = float(rest.split()[0])
        except (ValueError, IndexError):
            continue
        if v == v:  # NaN 제외
            out.append((name, labels, v))
    return out


def total_of(samples, metric, label_has=None):
    """metric 의 값을 모두 더한다 (label_has 가 있으면 그 글자가 든 라벨만). 하나도 없으면 None."""
    vals = [v for n, labels, v in samples if n == metric and (label_has is None or label_has in labels)]
    return sum(vals) if vals else None


def parse_counter(text, metric):
    """Prometheus 텍스트에서 metric 의 값을 모두 더한다. 없으면 ValueError."""
    total = total_of(parse_samples(text), metric)
    if total is None:
        raise ValueError("%s 카운터가 없음" % metric)
    return total


class TrafficFeed:
    """/metrics 를 주기적으로 읽어 rps 를 계산한다. transport(url) -> 텍스트, clock() -> 초 를 바꿔 끼우면 테스트용."""

    def __init__(self, transport=None, interval=None, clock=time.time, timeout=3.0):
        env = os.environ.get
        self.url = env("TRAFFIC_METRICS_URL", "").strip()
        self.metric = env("TRAFFIC_METRIC", "").strip() or "http_requests_total"
        self.peak = float(env("TRAFFIC_PEAK", "").strip() or 100)
        self.interval = float(env("TRAFFIC_POLL", "").strip() or 1) if interval is None else interval
        self.transport, self.clock, self.timeout = transport or self._get, clock, timeout
        self.prev = None  # (시각, 누적 요청 수)
        self.value, self.ok_at = 0.0, None
        self.window = []  # 최근 WINDOW_SEC 의 (시각, 요청 수, 5xx 수, 응답 시간 합, 응답 시간 건수)
        self.extra = {}  # error_pct · latency_ms · lcp_s · cls · inp_s · ttfb_s · vitals_reports (없는 것은 None)
        self.calls = self.fails = 0
        self.last_error = None
        self.down = False  # 연속 실패 중인지 (메인 루프가 로그를 한 번만 남기도록)
        self.started = False

    def sample(self):
        """한 번 읽어 rps 를 갱신한다. 실패하면 예외."""
        self.calls += 1
        try:
            samples = parse_samples(self.transport(self.url))
            total = total_of(samples, self.metric)
            if total is None:
                raise ValueError("%s 카운터가 없음" % self.metric)
        except Exception as e:  # noqa: BLE001
            self.fails += 1
            self.last_error = "%s: %s" % (type(e).__name__, str(e)[:60])
            raise
        now = self.clock()
        if self.prev is not None and total >= self.prev[1] and now > self.prev[0]:
            rate = (total - self.prev[1]) / (now - self.prev[0])
            self.value = rate if self.ok_at is None else 0.5 * self.value + 0.5 * rate  # 살짝 부드럽게
            self.ok_at = now
        # total < prev 면 서버가 다시 떠서 카운터가 0 부터 시작한 것: 이번 구간은 건너뛴다
        self.prev = (now, total)
        self.read_extra(samples, now, total)
        return self.value

    def read_extra(self, samples, now, total):
        """rps 말고 같이 볼 지표. 서버가 내보내지 않는 항목은 None 으로 둔다."""
        point = (now, total, total_of(samples, self.metric, 'status="5xx"'),
                 total_of(samples, "http_request_duration_seconds_sum"),
                 total_of(samples, "http_request_duration_seconds_count"))
        if self.window and total < self.window[-1][1]:
            self.window = []  # 서버 재시작
        self.window = [p for p in self.window if now - p[0] <= WINDOW_SEC] + [point]
        old = self.window[0]
        extra = {"error_pct": None, "latency_ms": None}
        d_req = total - old[1]
        if point[2] is not None and old[2] is not None and d_req > 0:
            extra["error_pct"] = round(100.0 * max(0.0, point[2] - old[2]) / d_req, 1)
        if None not in (point[3], point[4], old[3], old[4]) and point[4] - old[4] > 0:
            extra["latency_ms"] = round(1000.0 * (point[3] - old[3]) / (point[4] - old[4]), 1)
        for key, name in VITALS:
            extra[key] = total_of(samples, name)
        extra["vitals_reports"] = total_of(samples, "web_vitals_reports_total")
        self.extra = extra

    def metrics(self):
        """지금 보여 줄 부가 지표. 오래 못 읽었으면 빈 값."""
        if self.ok_at is None or self.clock() - self.ok_at > STALE_SEC:
            return {}
        return dict(self.extra)

    def rps(self):
        if self.ok_at is None or self.clock() - self.ok_at > STALE_SEC:
            return 0.0
        return self.value

    def start(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(self.interval)
            try:
                self.sample()
                self.down = False
            except Exception:  # noqa: BLE001 — 실패해도 대시보드는 계속 돈다
                self.down = True

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "merrybugmas-dashboard"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "replace")
