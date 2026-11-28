# -*- coding: utf-8 -*-
"""서버 에러 쥐 (네트워크·OpenAI 없음): python3 -m unittest discover -s tests -v

- 쥐마다 layer: front(브라우저) / server(서버). status 의 mice[]·history[] 에 나간다
- mock: 서버 쥐는 ./market outbreak N server 로만 나온다 (평소 흐름은 그대로)
- 실연동: Sentry 이슈의 platform 이 서버 언어면 server
- 판정: 서버면 AI 입력에 "계층: 서버" 한 줄, 키워드 판정에 서버 단어
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai  # noqa: E402
import sentry_live  # noqa: E402
import xmas_market as xm  # noqa: E402
from test_smoke import FakeOpenAI, new_world, step, wait_ai  # noqa: E402


class MockServerIssueTest(unittest.TestCase):
    def test_server_issue_shape(self):
        xm.R.seed(1)
        src = xm.MockSource()
        titles = {t for t, _ in xm.SERVER_CATALOG}
        for _ in range(20):
            i = src.make_server_issue("SEARCH")
            self.assertEqual(i["layer"], "server")
            self.assertIn(i["title"], titles)
            self.assertEqual(i["kind"], dict(xm.SERVER_CATALOG)[i["title"]])
            self.assertTrue(i["id"].startswith("SEARCH-"))
        self.assertTrue(src.make_server_issue("ALL")["id"].startswith("WEB-"))  # 전체 보기면 WEB 으로
        self.assertEqual(src.make_issue("WEB")["layer"], "front")

    def test_outbreak_server_command(self):
        w = new_world("WEB", "sentry")
        n = len(w.mice)
        w.command("outbreak 3 server")
        new = w.mice[n:]
        self.assertEqual(len(new), 3)
        self.assertTrue(all(m.issue["layer"] == "server" for m in new))
        st = {m["id"]: m for m in w.snapshot_state()["mice"]}
        self.assertTrue(all(st[m.issue["id"]]["layer"] == "server" for m in new))
        step(w, 60)
        hist = w.snapshot_state()["history"]
        self.assertTrue(any(r["layer"] == "server" for r in hist))
        self.assertTrue(all(r["layer"] in ("front", "server") for r in hist))

    def test_outbreak_default_is_front(self):
        w = new_world("WEB", "sentry")
        n = len(w.mice)
        w.command("outbreak 2")
        w.command("outbreak 서버")  # 숫자가 없으면 5마리
        layers = [m.issue["layer"] for m in w.mice[n:]]
        self.assertEqual(layers, ["front"] * 2 + ["server"] * 5)

    def test_ai_context_has_layer(self):
        w = new_world("WEB", "sentry")
        w.command("outbreak 1 server")
        self.assertEqual(w.ai_context()["mice"][-1]["layer"], "server")


class LiveLayerTest(unittest.TestCase):
    def test_platform_to_layer(self):
        row = {"shortId": "WEB-9", "title": "KeyError: 'rate'"}
        self.assertEqual(sentry_live.to_issue(dict(row, platform="python"))["layer"], "server")
        self.assertEqual(sentry_live.to_issue(dict(row, platform="javascript"))["layer"], "front")
        self.assertEqual(sentry_live.to_issue(row)["layer"], "front")  # 모르면 예전과 같이 front

    def test_feed_keeps_layer(self):
        rows = [{"id": "1", "shortId": "WEB-A", "title": "KeyError: 'rate'", "platform": "python"},
                {"id": "2", "shortId": "WEB-B", "title": "RangeError: Invalid time value", "platform": "javascript"}]
        f = sentry_live.SentryFeed(transport=lambda url: list(rows), interval=3600)
        f.org, f.project = "acme", "demo"
        f.fetch()
        self.assertEqual({i["id"]: i["layer"] for i in f.poll()}, {"WEB-A": "server", "WEB-B": "front"})

    def test_keyword_kind_for_server_titles(self):
        g = sentry_live.guess_kind
        self.assertEqual(g("ConnectionRefusedError: [Errno 61] Connection refused"), "needs-human")
        self.assertEqual(g("sqlite3.OperationalError: database is locked"), "needs-human")
        self.assertEqual(g("BrokenPipeError: [Errno 32] Broken pipe"), "noise")
        self.assertEqual(g("ConnectionResetError: [Errno 54] Connection reset by peer"), "noise")
        self.assertEqual(g("KeyError: 'rate'"), "fixable")
        self.assertEqual(g("ZeroDivisionError: division by zero"), "fixable")
        # 브라우저 제목은 그대로
        self.assertEqual(g("API Error: 503 /api/member/info"), "needs-human")
        self.assertEqual(g("ChunkLoadError: Loading chunk 7967 failed"), "noise")
        self.assertEqual(g("TypeError: Cannot read properties of undefined (reading 'map')"), "fixable")

    def test_live_refuses_fake_server_mice(self):
        w = new_world("WEB", "sentry")
        w.live = object()  # 실연동 중
        n = len(w.mice)
        w.outbreak(3, "server")
        self.assertEqual(len(w.mice), n)


class TriageLayerTest(unittest.TestCase):
    TRIAGE = {"sentry_triage": {"kind": "fixable", "reason": "키 이름 오류", "fix": "percent 로 읽기"}}

    def ask(self, payload):
        fake = FakeOpenAI(self.TRIAGE)
        ai.Brain(key="k", transport=fake, workers=1).triage(payload)
        return fake.bodies[0]["input"]

    def test_server_line_only_for_server(self):
        base = {"id": "WEB-1", "title": "TypeError: x", "service": "WEB"}
        self.assertEqual(self.ask(base), "서비스: WEB\n이슈 ID: WEB-1\n제목: TypeError: x")  # 프론트는 예전 입력 그대로
        self.assertEqual(self.ask(dict(base, layer="front")), "서비스: WEB\n이슈 ID: WEB-1\n제목: TypeError: x")
        self.assertEqual(self.ask(dict(base, layer="server")), "서비스: WEB\n이슈 ID: WEB-1\n제목: TypeError: x\n계층: 서버")

    def test_dashboard_sends_layer_to_triage(self):
        fake = FakeOpenAI(self.TRIAGE)
        w = new_world("WEB", "sentry")
        w.ai = ai.Brain(key="k", transport=fake, workers=1)
        w.spawn_mouse({"id": "WEB-S1", "title": "KeyError: 'rate'", "kind": "noise", "layer": "server", "review_block": False})
        self.assertTrue(wait_ai(w, lambda: bool(fake.bodies) and w.mice[-1].issue.get("ai") == "done"))
        self.assertTrue(fake.bodies[0]["input"].endswith("계층: 서버"))
        self.assertEqual(w.mice[-1].issue["kind"], "fixable")


if __name__ == "__main__":
    unittest.main()
