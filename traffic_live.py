# -*- coding: utf-8 -*-
"""트래픽 실연동 (읽기 전용) — Prometheus 형식 /metrics 의 요청 수 카운터를 읽어 rps 를 낸다.

  python3 xmas_market.py --live      # MockSource.rps 대신 실제 서버의 초당 요청 수로 줄을 세운다

Grafana 를 거치지 않고 서버의 /metrics 를 직접 읽는다 (Prometheus 가 긁어 가는 것과 같은 방식).
카운터(누적 요청 수)의 증가분 ÷ 지난 시간 = rps. 표준 라이브러리만 쓴다.

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


def parse_counter(text, metric):
    """Prometheus 텍스트에서 metric 의 값을 모두 더한다. 없으면 ValueError."""
    total, found = 0.0, False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(None, 1)[0]
        if name != metric:
            continue
        rest = line[line.index("}") + 1:] if "{" in line else line[len(name):]
        total += float(rest.split()[0])
        found = True
    if not found:
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
        self.calls = self.fails = 0
        self.last_error = None
        self.down = False  # 연속 실패 중인지 (메인 루프가 로그를 한 번만 남기도록)
        self.started = False

    def sample(self):
        """한 번 읽어 rps 를 갱신한다. 실패하면 예외."""
        self.calls += 1
        try:
            total = parse_counter(self.transport(self.url), self.metric)
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
        return self.value

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
