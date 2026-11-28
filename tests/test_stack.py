# -*- coding: utf-8 -*-
"""스택트레이스 기반 트리아지 (네트워크·OpenAI 없음): python3 -m unittest discover -s tests -v

- sentry_live: 최신 이벤트 JSON → 스택 프레임, 이슈에 붙이기, 실패·끔이면 제목만
- ai.triage: 스택이 있으면 판정 입력에 붙고, 없으면 예전과 같은 입력
- 대시보드: mock 이슈에 가짜 스택, status 의 mice[]/history[] 에 frames
- fixer: 패치 요청에 스택 줄
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai  # noqa: E402
import fixer  # noqa: E402
import sentry_live  # noqa: E402
import xmas_market as xm  # noqa: E402
from test_smoke import FakeOpenAI, new_world, step, wait_ai  # noqa: E402

# Sentry "Retrieve an Issue Event" 응답의 스택 부분만 (예시 응답과 같은 모양). 프레임은 바깥 호출부터 적힌다.
EVENT = {"eventID": "e1", "entries": [
    {"type": "breadcrumbs", "data": {"values": []}},
    {"type": "exception", "data": {"values": [
        {"type": "Error", "stacktrace": {"frames": [{"filename": "inner.js", "function": "cause", "lineNo": 1, "inApp": True}]}},
        {"type": "TypeError", "value": "Cannot read properties of undefined (reading 'map')", "stacktrace": {"frames": [
            {"filename": "https://cdn.example.com/vendor.js", "function": "dispatch", "lineNo": 900, "inApp": False},
            {"filename": "app.js", "function": "loadGifts", "lineNo": 31, "inApp": True,
             "context": [[30, "  const res = await fetch('/api/gifts');"], [31, "  renderGifts(await res.json());"]]},
            {"filename": "app.js", "function": "renderGifts", "lineNo": 42, "inApp": True,
             "context": [[42, "  list.innerHTML = data.items.map(giftRow).join('');"]]},
        ]}}]}},
]}


class FramesFromEventTest(unittest.TestCase):
    def test_raised_exception_inapp_frames_from_crash_site(self):
        frames = sentry_live.frames_from_event(EVENT)
        self.assertEqual([(f["file"], f["line"], f["function"]) for f in frames],
                         [("app.js", 42, "renderGifts"), ("app.js", 31, "loadGifts")])  # 에러 난 곳부터, 우리 코드만
        self.assertEqual(frames[0]["code"], "list.innerHTML = data.items.map(giftRow).join('');")

    def test_no_inapp_keeps_all_frames_and_limit(self):
        ev = {"entries": [{"type": "exception", "data": {"values": [{"stacktrace": {"frames": [
            {"filename": "f%d.js" % i, "function": "fn%d" % i, "lineNo": i} for i in range(9)]}}]}}]}
        frames = sentry_live.frames_from_event(ev)
        self.assertEqual(len(frames), sentry_live.FRAME_LIMIT)
        self.assertEqual(frames[0]["file"], "f8.js")
        self.assertEqual(frames[0]["code"], "")  # context 가 없으면 빈 줄

    def test_odd_shapes_give_empty(self):
        for ev in (None, [], {}, {"entries": "x"}, {"entries": [{"type": "exception", "data": {}}]},
                   {"entries": [{"type": "message", "data": {"formatted": "hi"}}]}):
            self.assertEqual(sentry_live.frames_from_event(ev), [])


class FeedStackTest(unittest.TestCase):
    ROWS = [{"id": "4001", "shortId": "DEMO-1", "title": "TypeError: Cannot read properties of undefined (reading 'map')"},
            {"id": "4002", "shortId": "DEMO-2", "title": "API Error: 503 /api/member/info"}]

    def feed(self, event_fail=False, env=None):
        old = dict(os.environ)
        os.environ.update(env or {})
        try:
            self.urls = []

            def transport(url):
                self.urls.append(url)
                if "/events/latest/" in url:
                    if event_fail:
                        raise OSError("down")
                    return EVENT if "/issues/4001/" in url else {"entries": []}
                return list(self.ROWS)
            f = sentry_live.SentryFeed(transport=transport, interval=3600)
        finally:
            os.environ.clear()
            os.environ.update(old)
        f.org, f.project = "acme", "demo"
        return f

    def test_new_issue_gets_frames_from_latest_event(self):
        f = self.feed()
        self.assertEqual(f.fetch(), 2)
        got = {i["id"]: i for i in f.poll()}
        self.assertEqual(got["DEMO-1"]["frames"][0]["function"], "renderGifts")
        self.assertEqual(got["DEMO-2"]["frames"], [])  # 스택 없는 이벤트
        self.assertTrue(any(u.endswith("/api/0/organizations/acme/issues/4001/events/latest/") for u in self.urls))  # 숫자 ID 로 부른다
        f.fetch()  # 이미 본 이슈의 스택은 다시 읽지 않는다
        self.assertEqual(sum("/events/latest/" in u for u in self.urls), 2)

    def test_event_failure_still_delivers_issue_without_frames(self):
        f = self.feed(event_fail=True)
        self.assertEqual(f.fetch(), 2)
        got = f.poll()
        self.assertEqual(sorted(i["id"] for i in got), ["DEMO-1", "DEMO-2"])
        self.assertTrue(all(i["frames"] == [] for i in got))
        self.assertEqual(f.stack_fails, 2)

    def test_stack_off_makes_no_event_calls(self):
        f = self.feed(env={"SENTRY_STACK": "off"})
        f.fetch()
        self.assertFalse(any("/events/" in u for u in self.urls))

    def test_budget_spent_skips_rest(self):
        f = self.feed()
        f.stack_budget = 0  # 시간이 다 됐으면 남은 이슈는 스택 없이 바로 내보낸다
        f.fetch()
        self.assertFalse(any("/events/" in u for u in self.urls))
        self.assertEqual(len(f.poll()), 2)

    def test_reset_keeps_frames(self):
        f = self.feed()
        f.fetch()
        f.poll()
        f.reset()
        self.assertEqual({i["id"]: bool(i["frames"]) for i in f.poll()}, {"DEMO-1": True, "DEMO-2": False})


class TriageInputTest(unittest.TestCase):
    TRIAGE = {"sentry_triage": {"kind": "fixable", "reason": "items 없이 map", "fix": "items 방어"}}

    def ask(self, payload):
        fake = FakeOpenAI(self.TRIAGE)
        res = ai.Brain(key="k", transport=fake, workers=1).triage(payload)
        return fake.bodies[0]["input"], res

    def test_frames_are_added_to_triage_input(self):
        frames = sentry_live.frames_from_event(EVENT)
        text, res = self.ask({"id": "WEB-1", "title": "TypeError: x", "service": "WEB", "frames": frames})
        self.assertIn("에러 위치", text)
        self.assertIn("renderGifts (app.js:42)", text)
        self.assertIn("data.items.map(giftRow)", text)
        self.assertEqual(res["kind"], "fixable")

    def test_without_frames_input_is_unchanged(self):
        text, _ = self.ask({"id": "WEB-1", "title": "TypeError: x", "service": "WEB"})
        self.assertEqual(text, "서비스: WEB\n이슈 ID: WEB-1\n제목: TypeError: x")
        text2, _ = self.ask({"id": "WEB-1", "title": "TypeError: x", "service": "WEB", "frames": []})
        self.assertEqual(text2, text)

    def test_frames_text_is_capped(self):
        many = [{"file": "f%d.js" % i, "line": i, "function": "fn", "code": "x" * 100} for i in range(20)]
        self.assertLessEqual(len(ai.frames_text(many)), ai.FRAMES_TEXT_MAX)
        self.assertEqual(ai.frames_text(None), "")


class DashboardFramesTest(unittest.TestCase):
    def test_mock_issue_has_frames_and_same_randomness(self):
        xm.R.seed(3)
        issue = xm.MockSource().make_issue("WEB")
        self.assertEqual(issue["frames"], xm.MOCK_FRAMES.get(issue["title"], []))
        xm.R.seed(3)
        again = xm.MockSource().make_issue("WEB")
        self.assertEqual((again["id"], again["title"]), (issue["id"], issue["title"]))
        for title in xm.MOCK_FRAMES:  # 가짜 스택은 목록에 있는 제목에만
            self.assertIn(title, [c[1] for c in xm.ISSUE_CATALOG])

    def test_status_has_frames_for_mice_and_history(self):
        w = new_world("WEB", "sentry")
        w.spawn_mouse({"id": "WEB-F1", "title": "TypeError: Cannot read properties of undefined (reading 'map')",
                       "kind": "fixable", "review_block": False,
                       "frames": [{"file": "app.js", "line": i, "function": "f", "code": ""} for i in range(5)]})
        mice = {m["id"]: m for m in w.snapshot_state()["mice"]}
        self.assertEqual(len(mice["WEB-F1"]["frames"]), 3)  # status 에는 3개까지
        step(w, 40)
        hist = [r for r in w.snapshot_state()["history"] if r["id"] == "WEB-F1"]
        self.assertTrue(hist)
        self.assertEqual(hist[0]["frames"][0]["file"], "app.js")

    def test_ai_triage_receives_frames(self):
        fake = FakeOpenAI({"sentry_triage": {"kind": "noise", "reason": "확장 프로그램", "fix": ""}})
        w = new_world("WEB", "sentry")
        w.ai = ai.Brain(key="k", transport=fake, workers=1)
        frames = [{"file": "chrome-extension://abc/inpage.js", "line": 1, "function": "connect", "code": ""}]
        w.spawn_mouse({"id": "WEB-N1", "title": "MetaMask inpage.js: Object.connect failed", "kind": "fixable",
                       "review_block": False, "frames": frames})
        self.assertTrue(wait_ai(w, lambda: bool(fake.bodies) and w.mice[-1].issue.get("ai") == "done"))
        self.assertIn("chrome-extension://abc/inpage.js", fake.bodies[0]["input"])
        self.assertEqual(w.mice[-1].issue["kind"], "noise")


class FixerFramesTest(unittest.TestCase):
    def test_frames_lines(self):
        self.assertEqual(fixer.frames_lines([{"file": "app.js", "line": 42, "function": "renderGifts", "code": "a.map(b)"}]),
                         "  app.js:42 in renderGifts  > a.map(b)")
        self.assertEqual(fixer.frames_lines([]), "")

    def test_live_issue_with_frames_but_no_culprit_is_fixed(self):
        import shutil
        import tempfile
        d = tempfile.mkdtemp()  # publish 를 가짜로 끼우므로 git·gh 는 쓰지 않는다
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "app.js"), "w", encoding="utf-8") as f:
            f.write("function renderGifts(data) {\n  return data.items.map((g) => g.name); // 버그\n}\n")
        asked, published = [], []

        def ask(instructions, user, name, schema):
            asked.append(user)
            return {"file": "app.js", "find": "return data.items.map((g) => g.name); // 버그",
                    "replace": "return (data.items || []).map((g) => g.name);", "title": "fix: items 방어",
                    "explanation": "e"}

        def publish(issue, f, new, title, body):
            published.append(title)
            return {"pr": 9, "url": "https://github.com/x/demo/pull/9", "title": title}
        fx = fixer.Fixer(ask=ask, repo=d, files=["app.js"], publish=publish, review=None)
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        w.fixer = fx
        w.start_stage(w.find_service("WEB"), "sentry")
        xm.compose(w, 100, 30)
        w.spawn_mouse({"id": "WEB-K", "title": "TypeError: Cannot read properties of undefined (reading 'map')",
                       "kind": "fixable", "review_block": False, "live": True,
                       "frames": [{"file": "app.js", "line": 2, "function": "renderGifts", "code": "return data.items.map"}]})
        self.assertTrue(wait_ai(w, lambda: any(r["id"] == "WEB-K" for r in w.history), timeout=8))
        rec = [r for r in w.history if r["id"] == "WEB-K"][0]
        self.assertEqual((rec["outcome"], rec["pr"]), ("fixed", 9))  # culprit 이 없어도 스택이 있으면 고친다
        self.assertIn("app.js:2 in renderGifts", asked[0])


if __name__ == "__main__":
    unittest.main()
