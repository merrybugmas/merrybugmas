# -*- coding: utf-8 -*-
"""Sentry 실연동 (읽기 전용) — 미해결 이슈를 가져와 쥐로 만든다.

  python3 xmas_market.py --live      # MockSource.poll_issues 대신 실제 Sentry 프로젝트의 미해결 이슈

Sentry 에는 아무것도 쓰지 않는다. 병정의 판정(noise·사람)과 fix·MR 은 대시보드 안의 연출이고,
Sentry 이슈 상태는 그대로다. 트래픽(rps)은 여전히 mock 이다. 표준 라이브러리만 쓴다.

환경변수 (레포의 .env 에 적어도 된다 — git 에 안 올라감)
  SENTRY_AUTH_TOKEN  필수. 개인 토큰(User Auth Token), 권한은 event:read 만
  SENTRY_ORG         필수. 조직 slug
  SENTRY_PROJECT     필수. 프로젝트 slug
  SENTRY_URL         기본 https://sentry.io (EU 조직은 https://de.sentry.io, 자체 호스팅은 그 주소)
  SENTRY_LIMIT       한 번에 가져올 미해결 이슈 수 (기본 20)
  SENTRY_POLL        다시 읽는 주기, 초 (기본 15)
  SENTRY_STACK       off 면 스택을 가져오지 않는다 (기본 on: 새 이슈마다 최신 이벤트를 1회 읽어 스택 프레임을 붙인다)
  SENTRY_STACK_TIMEOUT  스택 한 건을 기다리는 최대 초 (기본 3). 한 번 읽을 때 스택에 쓰는 시간은 모두 합쳐 8초까지,
                     넘거나 실패한 이슈는 스택 없이(제목만으로) 내보낸다
"""
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

NOISE_WORDS = ("extension", "chunkloaderror", "script error", "doubleclick", "googletag", "gtag", "metamask",
               "resizeobserver", "networkerror", "failed to fetch", "load failed", "third-party")
HUMAN_WORDS = ("api error", " 500", " 502", " 503", " 504", " 401", " 429", "security policy", "csp",
               "override", "personal data")


def guess_kind(title):
    """AI 가 꺼져 있을 때 쓰는 키워드 판정. 실제 이슈에는 mock 판정이 없으므로 제목의 단어로만 가른다."""
    t = " " + title.lower()
    if any(w in t for w in HUMAN_WORDS):
        return "needs-human"
    if any(w in t for w in NOISE_WORDS):
        return "noise"
    return "fixable"


def configured():
    return all(os.environ.get(k, "").strip() for k in ("SENTRY_AUTH_TOKEN", "SENTRY_ORG", "SENTRY_PROJECT"))


def to_issue(row):
    """Sentry 이슈 한 건 → 대시보드 이슈. id 는 Sentry 의 shortId (예: WEB-1A)."""
    title = str(row.get("title") or (row.get("metadata") or {}).get("title") or "(제목 없음)")
    return {
        "id": str(row.get("shortId") or row.get("id")),
        "title": title,
        "kind": guess_kind(title),
        "review_block": False,
        "live": True,
        "url": row.get("permalink") or "",
        "culprit": str(row.get("culprit") or "")[:120],
        "count": row.get("count"),
        "sentry_id": str(row.get("id") or ""),  # 숫자 ID. 이벤트 API 는 이 값으로 부른다 (shortId 가 되는지는 문서에 없음)
        "frames": [],
    }


FRAME_LIMIT = 5


def frames_from_event(event, limit=FRAME_LIMIT):
    """Sentry 이벤트 JSON → 스택 프레임 [{file, line, function, code}], 에러가 난 곳부터.

    entries[] 중 type "exception" 의 data.values[] 마지막(실제로 던져진 예외)의 stacktrace.frames 를 쓴다.
    Sentry 는 프레임을 바깥 호출부터 적으므로 뒤집는다. 우리 코드(inApp) 프레임이 있으면 그것만 쓴다.
    context([[줄 번호, 코드], ...])는 없을 수 있다. 모양이 다르면 빈 목록.
    """
    if not isinstance(event, dict):
        return []
    values = []
    for e in event.get("entries") or []:
        if isinstance(e, dict) and e.get("type") == "exception":
            values = (e.get("data") or {}).get("values") or []
    raw = []
    for v in reversed(values):
        st = (v or {}).get("stacktrace") or {}
        if st.get("frames"):
            raw = [f for f in st["frames"] if isinstance(f, dict)]
            break
    raw = list(reversed(raw))
    if any(f.get("inApp") for f in raw):
        raw = [f for f in raw if f.get("inApp")]
    out = []
    for f in raw[:limit]:
        line = f.get("lineNo")
        code = ""
        for pair in f.get("context") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == line:
                code = str(pair[1]).strip()[:120]
        out.append({"file": str(f.get("filename") or f.get("absPath") or f.get("module") or "?")[:120],
                    "line": line if isinstance(line, int) else None,
                    "function": str(f.get("function") or "?")[:60],
                    "code": code})
    return out


class SentryFeed:
    """Sentry 미해결 이슈를 백그라운드에서 주기적으로 읽는다. 처음 본 이슈만 poll() 로 내준다.

    transport(url) -> 응답 JSON(list) 을 바꿔 끼우면 테스트에서 네트워크 없이 쓴다.
    """

    def __init__(self, transport=None, interval=None, timeout=8.0):
        env = os.environ.get
        self.base = env("SENTRY_URL", "").strip().rstrip("/") or "https://sentry.io"
        self.org, self.project = env("SENTRY_ORG", "").strip(), env("SENTRY_PROJECT", "").strip()
        self.token = env("SENTRY_AUTH_TOKEN", "").strip()
        self.limit = int(env("SENTRY_LIMIT", "").strip() or 20)
        self.interval = float(env("SENTRY_POLL", "").strip() or 15) if interval is None else interval
        self.timeout = timeout
        self.stack = env("SENTRY_STACK", "on").strip().lower() not in ("off", "0", "false", "no")
        self.stack_timeout = float(env("SENTRY_STACK_TIMEOUT", "").strip() or 3)
        self.stack_budget = 8.0  # 한 번 읽을 때 스택 조회에 쓰는 시간 합계 상한 (첫 읽기가 대시보드 시작을 오래 막지 않게)
        self.stack_calls = self.stack_fails = 0
        self.transport = transport or self._get
        # 스택 조회는 짧게 기다린다. 가짜 transport 를 끼우면 이벤트 조회도 그것으로 한다 (테스트)
        self.event_transport = transport or (lambda url: self._get(url, self.stack_timeout))
        self.seen, self.fresh = {}, queue.Queue()  # seen: id → 이슈 (본 순서대로)
        self.calls = self.fails = 0
        self.last_error = None
        self.errors = queue.Queue()  # 메인 루프가 로그로 옮길 실패 메시지
        self.lock = threading.Lock()
        self.started = False

    @property
    def url(self):
        q = urllib.parse.urlencode({"query": "is:unresolved", "limit": self.limit})
        return "%s/api/0/projects/%s/%s/issues/?%s" % (
            self.base, urllib.parse.quote(self.org), urllib.parse.quote(self.project), q)

    def fetch(self):
        """한 번 읽어 처음 본 이슈를 fresh 에 넣는다. 실패하면 예외."""
        self.calls += 1
        try:
            rows = self.transport(self.url)
            if not isinstance(rows, list):
                raise ValueError("이슈 목록이 아님")
        except Exception as e:  # noqa: BLE001
            self.fails += 1
            self.last_error = describe(e)
            raise
        new = []
        with self.lock:
            for row in reversed(rows[:self.limit]):  # 오래된 것부터
                issue = to_issue(row)
                if issue["id"] not in self.seen:
                    self.seen[issue["id"]] = issue
                    new.append(issue)
        deadline = time.monotonic() + self.stack_budget
        for issue in new:
            if self.stack and issue["sentry_id"] and time.monotonic() < deadline:
                issue["frames"] = self.fetch_frames(issue["sentry_id"])
            self.fresh.put(dict(issue))
        return len(new)

    def event_url(self, sentry_id):
        return "%s/api/0/organizations/%s/issues/%s/events/latest/" % (
            self.base, urllib.parse.quote(self.org), urllib.parse.quote(sentry_id))

    def fetch_frames(self, sentry_id):
        """이슈의 최신 이벤트에서 스택 프레임을 읽는다. 실패하면 [] (제목만으로 판정한다)."""
        self.stack_calls += 1
        try:
            return frames_from_event(self.event_transport(self.event_url(sentry_id)))
        except Exception:  # noqa: BLE001 — 스택은 덤이다. 실패해도 이슈는 그대로 내보낸다
            self.stack_fails += 1
            return []

    def start(self):
        """첫 읽기는 호출한 쪽에서 fetch() 로 확인하고, 그 뒤 주기 읽기를 백그라운드로 돌린다."""
        if not self.started:
            self.started = True
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(self.interval)
            try:
                self.fetch()
            except Exception:  # noqa: BLE001 — 실패해도 대시보드는 계속 돈다
                self.errors.put(self.last_error)

    def poll(self):
        """처음 본 이슈들. 메인 스레드에서 매 프레임 부른다."""
        out = []
        while True:
            try:
                out.append(self.fresh.get_nowait())
            except queue.Empty:
                return out

    def reset(self):
        """새 판: 지금까지 본 미해결 이슈를 처음부터 다시 내준다."""
        with self.lock:
            self.poll()
            for issue in self.seen.values():
                self.fresh.put(dict(issue))

    def _get(self, url, timeout=None):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token,
                                                   "User-Agent": "merrybugmas-dashboard"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))


def describe(e):
    """토큰·주소 없이 원인만 한 줄로."""
    if isinstance(e, urllib.error.HTTPError):
        hint = {401: "토큰이 틀렸거나 만료", 403: "토큰 권한 부족 (event:read 필요)",
                404: "조직·프로젝트 slug 또는 SENTRY_URL 확인"}.get(e.code, "")
        return "HTTP %d %s" % (e.code, hint)
    return "%s: %s" % (type(e).__name__, str(e)[:60])
