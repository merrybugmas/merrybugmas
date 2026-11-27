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
        "count": row.get("count"),
    }


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
        self.transport = transport or self._get
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
        n = 0
        with self.lock:
            for row in reversed(rows[:self.limit]):  # 오래된 것부터
                issue = to_issue(row)
                if issue["id"] not in self.seen:
                    self.seen[issue["id"]] = issue
                    self.fresh.put(dict(issue))
                    n += 1
        return n

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

    def _get(self, url):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token,
                                                   "User-Agent": "merrybugmas-dashboard"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))


def describe(e):
    """토큰·주소 없이 원인만 한 줄로."""
    if isinstance(e, urllib.error.HTTPError):
        hint = {401: "토큰이 틀렸거나 만료", 403: "토큰 권한 부족 (event:read 필요)",
                404: "조직·프로젝트 slug 또는 SENTRY_URL 확인"}.get(e.code, "")
        return "HTTP %d %s" % (e.code, hint)
    return "%s: %s" % (type(e).__name__, str(e)[:60])
