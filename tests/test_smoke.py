# -*- coding: utf-8 -*-
"""스모크 테스트 (표준 라이브러리만): python3 -m unittest discover -s tests -v

- 모든 서비스 × 화면이 오류 없이 그려지는지
- 키워드 병정 명령
- AI 병정: 가짜 OpenAI 응답으로 명령 해석·트리아지·브리핑, 실패 시 키워드 폴백 (네트워크 안 씀)
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import ai  # noqa: E402
import xmas_market as xm  # noqa: E402


def new_world(service="WEB", view="sentry", seed=7):
    xm.R.seed(seed)
    w = xm.World()
    w.auto = False
    w.start_stage(w.find_service(service), view)
    xm.compose(w, 100, 30)
    return w


def step(w, sec):
    for _ in range(int(sec * xm.FPS)):
        w.update(1.0 / xm.FPS)


def wait_ai(w, cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        w.update(1.0 / xm.FPS)
        if cond():
            return True
        time.sleep(0.01)
    return False


def fake_response(obj):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(obj, ensure_ascii=False)}]}]}


class FakeOpenAI:
    """요청 이름(json_schema name)별로 정해 둔 답을 돌려준다."""

    def __init__(self, answers, fail=False):
        self.answers, self.fail, self.bodies = answers, fail, []

    def __call__(self, body):
        self.bodies.append(body)
        if self.fail:
            raise OSError("network down")
        name = body["text"]["format"]["name"]
        ans = self.answers[name]
        return fake_response(ans(body) if callable(ans) else ans)


class RenderTest(unittest.TestCase):
    def test_every_service_and_view_renders(self):
        n = 0
        for svc in xm.SERVICES:
            for view in ("overview", "traffic", "sentry"):
                with self.subTest(service=svc[0], view=view):
                    w = new_world(svc[0], view)
                    step(w, 50)
                    frame = xm.compose(w, 100, 30)
                    self.assertEqual(len(frame), 30)
                    n += 1
        self.assertEqual(n, len(xm.SERVICES) * 3)

    def test_menu_intro_and_history_render(self):
        w = xm.World()
        for v in ("intro", "menu"):
            w.set_view(v)
            self.assertEqual(len(xm.compose(w, 80, 24)), 24)
        w = new_world()
        step(w, 60)
        w.open_history()
        self.assertEqual(len(xm.compose(w, 100, 30)), 30)

    def test_small_terminal_message(self):
        w = new_world()
        self.assertIn("60x22", "".join(xm.compose(w, 50, 20)))


class KeywordOrderTest(unittest.TestCase):
    def test_named_soldier_catches_issue(self):
        w = new_world()
        w.spawn_mouse()
        iid = w.mice[-1].issue["id"]
        w.command("tell 진저 %s 먼저 잡아" % iid)
        self.assertEqual(w.mice[-1].assigned, 1)

    def test_rest_and_report(self):
        w = new_world()
        w.command("tell 1 복귀")
        self.assertTrue(w.soldiers[0].off)
        w.command("tell 모두 보고")
        self.assertTrue(all(s.speech for s in w.soldiers))

    def test_unknown_soldier(self):
        w = new_world()
        w.command("tell 산타 출동")  # 이름이 없으면 모두에게 — 죽지 않아야 한다
        self.assertTrue(w.logs)


class AiBrainTest(unittest.TestCase):
    def test_extract_text_variants(self):
        self.assertEqual(ai.extract_text({"output_text": "x"}), "x")
        self.assertEqual(json.loads(ai.extract_text(fake_response({"a": 1}))), {"a": 1})
        with self.assertRaises(RuntimeError):
            ai.extract_text({"output": [], "status": "incomplete"})

    def test_request_uses_structured_output(self):
        fake = FakeOpenAI({"sentry_triage": {"kind": "noise", "reason": "광고 스크립트", "fix": "무시"}})
        b = ai.Brain(key="test", model="m", effort="low", transport=fake)
        res = b.triage({"id": "WEB-1", "title": "Failed to fetch (ad.doubleclick.net)"})
        body = fake.bodies[0]
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(res["fix"], "")  # fixable 이 아니면 수정 요약을 비운다

    def test_available_respects_env(self):
        old = dict(os.environ)
        try:
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertFalse(ai.available())
            os.environ["OPENAI_API_KEY"] = "x"
            self.assertTrue(ai.available())
            os.environ["XMAS_AI"] = "off"
            self.assertFalse(ai.available())
        finally:
            os.environ.clear()
            os.environ.update(old)


class AiWorldTest(unittest.TestCase):
    def world_with(self, fake):
        w = new_world()
        w.ai = ai.Brain(key="test", model="fake", transport=fake)
        return w

    def test_ai_triage_overrides_mock_verdict_and_mr_uses_fix(self):
        fake = FakeOpenAI({"sentry_triage": {"kind": "fixable", "reason": "날짜 파싱 버그", "fix": "Invalid Date 가드 추가"}})
        w = self.world_with(fake)
        w.spawn_mouse({"id": "WEB-AI1", "title": "RangeError: Invalid time value", "kind": "noise", "review_block": False})
        m = w.mice[-1]
        self.assertTrue(wait_ai(w, lambda: m.issue.get("ai") == "done"))
        self.assertEqual(m.issue["kind"], "fixable")
        step(w, 40)
        rec = [r for r in w.history if r["id"] == "WEB-AI1"]
        self.assertTrue(rec, "병정이 AI 판정 쥐를 처리해야 한다")
        self.assertEqual(rec[0]["outcome"], "fixed")
        self.assertEqual(rec[0]["note"], "Invalid Date 가드 추가")
        self.assertTrue(any("Invalid Date 가드 추가" in l[1] for l in w.logs))

    def test_ai_order_understands_description(self):
        def order(body):  # '503 에러 난 쥐' → 상황 속 mice 에서 503 이슈를 찾아 2번 병정에게
            ctx = json.loads(body["input"].split("상황:\n", 1)[1])
            target = next(m["id"] for m in ctx["mice"] if "503" in m["title"])
            return {"soldier": "2", "intent": "catch", "issue_id": target, "reply": "갑니다!"}
        fake = FakeOpenAI({"soldier_order": order,
                           "sentry_triage": {"kind": "needs-human", "reason": "백엔드 5xx", "fix": ""}})
        w = self.world_with(fake)
        w.spawn_mouse({"id": "WEB-503", "title": "API Error: 503 /api/member/info", "kind": "needs-human",
                       "review_block": False})
        w.command("tell 진저한테 503 에러 난 쥐부터 처리하라고 해")
        m = w.mice[-1]
        self.assertTrue(wait_ai(w, lambda: m.assigned == 1))
        self.assertTrue(any("🧠 해석" in l[1] for l in w.logs))

    def test_ai_chitchat_reply(self):
        fake = FakeOpenAI({"soldier_order": {"soldier": "1", "intent": "unknown", "issue_id": "",
                                             "reply": "감사합니다! 계속 지키겠습니다"}})
        w = self.world_with(fake)
        w.command("tell 루돌프 고생 많았어")
        self.assertTrue(wait_ai(w, lambda: "감사합니다" in w.soldiers[0].speech))

    def test_ai_failure_falls_back_to_keywords(self):
        w = self.world_with(FakeOpenAI({}, fail=True))
        w.spawn_mouse({"id": "WEB-FB1", "title": "TypeError: x", "kind": "fixable", "review_block": False})
        m = w.mice[-1]
        self.assertTrue(wait_ai(w, lambda: m.issue.get("ai") == "failed"))
        w.command("tell 3 WEB-FB1 잡아")
        self.assertTrue(wait_ai(w, lambda: m.assigned == 2))
        self.assertTrue(any("키워드로 처리" in l[1] for l in w.logs))

    def test_brief(self):
        fake = FakeOpenAI({"ops_brief": {"summary": ["rps 1,200 활기참", "미해결 쥐 2마리"], "advice": "진저에게 WEB-1 먼저"}})
        w = self.world_with(fake)
        w.command("brief")
        self.assertTrue(wait_ai(w, lambda: any("권고" in l[1] for l in w.logs)))

    def test_brief_without_ai(self):
        w = new_world()
        w.command("brief")
        self.assertTrue(any("브리핑" in l[1] for l in w.logs))

    def test_state_json_exposes_ai(self):
        w = self.world_with(FakeOpenAI({}))
        st = w.snapshot_state()
        self.assertTrue(st["ai"]["on"])
        json.dumps(st, ensure_ascii=False)


class AiEdgeCaseTest(unittest.TestCase):
    """코드 리뷰에서 나온 경합·이상 응답 케이스."""

    def test_manual_order_beats_late_ai_verdict(self):
        gate = []
        fake = FakeOpenAI({"sentry_triage": lambda b: (gate.append(1), time.sleep(0.3))[0] and None
                           or {"kind": "fixable", "reason": "r", "fix": "f"}})
        w = new_world()
        w.ai = ai.Brain(key="t", model="m", transport=fake)
        w.spawn_mouse({"id": "WEB-M1", "title": "x", "kind": "fixable", "review_block": False})
        m = w.mice[-1]
        w.order_keywords("1 WEB-M1 noise로 처리")  # AI 판정이 오기 전에 사람이 noise 로 정함
        time.sleep(0.5)
        w.pump_ai()
        self.assertEqual(m.issue["kind"], "noise")
        self.assertEqual(m.issue["ai"], "manual")

    def test_triage_timeout_clears_pending(self):
        fake = FakeOpenAI({"sentry_triage": lambda b: time.sleep(5)})  # 응답이 안 온다
        w = new_world()
        w.ai = ai.Brain(key="t", model="m", transport=fake)
        w.spawn_mouse({"id": "WEB-T1", "title": "x", "kind": "noise", "review_block": False})
        m = w.mice[-1]
        w.src.poll_issues = lambda *a, **k: []  # 다른 쥐가 끼어들지 않게
        self.assertTrue(any(True for _ in range(60 * xm.FPS) if not w.update(1.0 / xm.FPS) and m.issue.get("triaged")))
        self.assertEqual(m.issue.get("ai"), "timeout")
        self.assertEqual(xm.kind_word(m.issue), "noise")

    def test_malformed_output_does_not_crash(self):
        fake = FakeOpenAI({"sentry_triage": {"kind": "banana", "reason": 3, "fix": None},
                           "ops_brief": {"summary": [], "advice": ""}})
        w = new_world()
        w.ai = ai.Brain(key="t", model="m", transport=fake)
        w.spawn_mouse({"id": "WEB-B1", "title": "x", "kind": "fixable", "review_block": False})
        m = w.mice[-1]
        self.assertTrue(wait_ai(w, lambda: m.issue.get("ai") == "failed"))
        self.assertEqual(m.issue["kind"], "fixable")
        w.command("brief")
        self.assertTrue(wait_ai(w, lambda: any("브리핑 실패" in l[1] for l in w.logs)))

    def test_stale_stage_results_dropped(self):
        fake = FakeOpenAI({"soldier_order": lambda b: (time.sleep(0.2), {"soldier": "1", "intent": "rest",
                                                                         "issue_id": "", "reply": ""})[1]})
        w = new_world("WEB")
        w.ai = ai.Brain(key="t", model="m", transport=fake)
        w.command("tell 루돌프 쉬어")
        w.start_stage(w.find_service("SEARCH"), "sentry")  # 새 판
        time.sleep(0.4)
        w.pump_ai()
        self.assertFalse(w.soldiers[0].off)

    def test_retries_on_429_then_succeeds(self):
        import urllib.error
        calls = []

        def flaky(body):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError("u", 429, "rate", {}, None)
            return fake_response({"kind": "noise", "reason": "r", "fix": ""})
        b = ai.Brain(key="t", model="m", transport=flaky)
        orig = ai.time.sleep
        ai.time.sleep = lambda s: None
        try:
            self.assertEqual(b.triage({"id": "A-1", "title": "t"})["kind"], "noise")
        finally:
            ai.time.sleep = orig
        self.assertEqual(len(calls), 2)


class LiveSentryTest(unittest.TestCase):
    """--live: 가짜 Sentry 응답으로 (네트워크 안 씀)."""
    ROWS = [{"shortId": "DEMO-2", "title": "API Error: 503 /api/member/info", "permalink": "https://example.invalid/2"},
            {"shortId": "DEMO-1", "title": "TypeError: Cannot read properties of undefined (reading 'map')"}]

    def feed(self, rows=None, fail=False):
        import sentry_live

        def transport(url):
            self.urls.append(url)
            if fail:
                raise OSError("down")
            return list(self.ROWS if rows is None else rows)
        self.urls = []
        f = sentry_live.SentryFeed(transport=transport, interval=3600)
        f.org, f.project = "acme", "demo"
        return f

    def live_world(self, feed):
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        ok = xm.attach_live(w, feed)
        w.start_stage(w.find_service("WEB"), "sentry")
        xm.compose(w, 100, 30)
        return w, ok

    def test_real_issues_become_mice_and_no_mock_mice(self):
        w, ok = self.live_world(self.feed())
        self.assertTrue(ok)
        self.assertIn("is%3Aunresolved", self.urls[0])
        self.assertIn("/projects/acme/demo/issues/", self.urls[0])
        step(w, 4)
        self.assertEqual(sorted(m.issue["id"] for m in w.mice), ["DEMO-1", "DEMO-2"])  # 중복 없이, 오래된 것부터
        self.assertEqual({m.issue["id"]: m.issue["kind"] for m in w.mice}["DEMO-2"], "needs-human")
        w.outbreak()
        w.spawn_one()
        w.command("mouse")
        w.command("outbreak 3")
        step(w, 30)
        seen = {m.issue["id"] for m in w.mice} | {r["id"] for r in w.history}
        self.assertEqual(seen, {"DEMO-1", "DEMO-2"})  # 가짜 쥐가 섞이지 않는다
        self.assertEqual(w.snapshot_state()["live"]["source"], "sentry")
        self.assertIn("LIVE SENTRY", "\n".join(xm.ANSI.sub("", l) for l in xm.compose(w, 100, 30)))

    def test_same_issue_not_spawned_twice(self):
        f = self.feed()
        w, _ = self.live_world(f)
        step(w, 4)
        f.fetch()
        step(w, 4)
        ids = [m.issue["id"] for m in w.mice] + [r["id"] for r in w.history]
        self.assertEqual(len(ids), len(set(ids)))

    def test_ai_triages_real_title(self):
        f = self.feed(rows=[self.ROWS[1]])
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        fake = FakeOpenAI({"sentry_triage": {"kind": "noise", "reason": "테스트 근거", "fix": ""}})
        w.ai = ai.Brain(key="k", transport=fake)
        xm.attach_live(w, f)
        w.start_stage(w.find_service("WEB"), "sentry")
        xm.compose(w, 100, 30)
        step(w, 2)
        self.assertTrue(wait_ai(w, lambda: w.mice and w.mice[0].issue.get("note") == "테스트 근거"))
        self.assertEqual(w.mice[0].issue["kind"], "noise")

    def test_failure_falls_back_to_mock(self):
        w, ok = self.live_world(self.feed(fail=True))
        self.assertFalse(ok)
        self.assertIsNone(w.live)
        w.outbreak(2)
        self.assertEqual(len(w.mice), 2)
        self.assertTrue(any("mock 으로 진행" in t for _, t, _, _ in w.logs))


class LiveTrafficTest(unittest.TestCase):
    """--live 트래픽: 가짜 /metrics 와 가짜 시계로 (네트워크 안 씀)."""

    def feed(self):
        import traffic_live
        self.now, self.total, self.fail = [100.0], [0], [False]

        def transport(url):
            if self.fail[0]:
                raise OSError("down")
            return ('# TYPE http_requests_total counter\n'
                    'http_requests_total{path="/a"} %d\nhttp_requests_total{path="/b"} 0\nother_total 999\n' % self.total[0])
        f = traffic_live.TrafficFeed(transport=transport, interval=3600, clock=lambda: self.now[0])
        f.url, f.metric, f.peak = "http://test/metrics", "http_requests_total", 100.0
        return f

    def tick(self, f, requests, sec=1.0):
        self.total[0] += requests
        self.now[0] += sec
        return f.sample()

    def test_rps_from_counter_delta(self):
        f = self.feed()
        f.sample()
        self.assertEqual(f.rps(), 0.0)  # 한 번만 읽어서는 속도를 모른다
        self.assertAlmostEqual(self.tick(f, 50), 50.0)
        self.assertAlmostEqual(self.tick(f, 150, 2.0), 62.5)  # (50 + 75) / 2 로 부드럽게
        self.total[0] = 5  # 서버 재시작: 카운터가 줄면 그 구간은 건너뛴다
        self.now[0] += 1
        self.assertAlmostEqual(f.sample(), 62.5)
        self.now[0] += 10  # 오래 못 읽으면 마지막 값을 붙들지 않는다
        self.assertEqual(f.rps(), 0.0)

    def test_dashboard_uses_real_rps_and_no_fake_spike(self):
        f = self.feed()
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        self.assertTrue(xm.attach_traffic(w, f))
        w.start_stage(w.find_service("WEB"), "traffic")
        xm.compose(w, 100, 30)
        self.tick(f, 80)
        step(w, 1)
        self.assertAlmostEqual(w.rps, 80.0)
        self.assertEqual(w.peak, 100.0)  # 서비스 배율(WEB 0.55)을 곱하지 않는다
        w.traffic_spike()
        self.assertEqual(w.src.spikes, [])
        self.assertEqual(w.snapshot_state()["live"]["traffic"], "metrics")
        self.assertIn("LIVE TRAFFIC", "\n".join(xm.ANSI.sub("", l) for l in xm.compose(w, 100, 30)))

    def test_error_rate_latency_and_web_vitals(self):
        import traffic_live
        now, page = [0.0], [""]

        def metrics(ok, err, dur_sum, extra=""):
            page[0] = ('http_requests_total{status="2xx"} %d\nhttp_requests_total{status="5xx"} %d\n'
                       'http_request_duration_seconds_sum %f\nhttp_request_duration_seconds_count %d\n%s'
                       % (ok, err, dur_sum, ok + err, extra))
        f = traffic_live.TrafficFeed(transport=lambda url: page[0], interval=3600, clock=lambda: now[0])
        f.metric = "http_requests_total"
        metrics(100, 0, 1.0)
        f.sample()
        now[0] = 2.0
        metrics(180, 20, 6.0, "web_vitals_lcp_seconds 1.25\nweb_vitals_cls 0.02\n")
        self.assertAlmostEqual(f.sample(), 50.0)  # 2xx 와 5xx 를 합쳐 100건 / 2초
        m = f.metrics()
        self.assertEqual(m["error_pct"], 20.0)   # 늘어난 100건 중 5xx 20건
        self.assertEqual(m["latency_ms"], 50.0)  # 5초 / 100건
        self.assertEqual((m["lcp_s"], m["cls"], m["inp_s"]), (1.25, 0.02, None))  # 서버가 안 준 항목은 None
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        w.traffic = f
        w.start_stage(w.find_service("WEB"), "traffic")
        line = "\n".join(xm.ANSI.sub("", l) for l in xm.compose(w, 120, 30))
        self.assertIn("5xx 20.0%", line)
        self.assertIn("LCP 1.25s", line)
        self.assertNotIn("INP", line)
        self.assertEqual(w.snapshot_state()["traffic"]["web"]["error_pct"], 20.0)
        self.assertEqual(xm.World().snapshot_state()["traffic"]["web"], {})  # mock 일 때는 지어내지 않는다

    def test_failure_falls_back_to_mock_traffic(self):
        f = self.feed()
        self.fail[0] = True
        xm.R.seed(7)
        w = xm.World()
        self.assertFalse(xm.attach_traffic(w, f))
        self.assertIsNone(w.traffic)
        w.start_stage(w.find_service("WEB"), "traffic")
        xm.compose(w, 100, 30)
        step(w, 1)
        self.assertGreater(w.rps, 0)  # mock 트래픽
        self.assertTrue(any("mock 으로 진행" in t for _, t, _, _ in w.logs))


class RealFixTest(unittest.TestCase):
    """병정의 실제 수정(PR): 가짜 OpenAI 패치 + 가짜 git/gh publish 로 (네트워크·git 안 씀)."""

    def setUp(self):
        import tempfile
        import fixer
        self.fixer = fixer
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "app.js"), "w") as f:
            f.write("function gifts(data) {\n  return data.items.map((g) => g.name); // 버그\n}\n")
        self.published = []

    def publish(self, issue, f, new, title, body):
        self.published.append((issue["id"], f, new, title))
        return {"pr": 7, "url": "https://github.com/x/demo/pull/7", "title": title, "branch": "b", "file": f, "diff": ""}

    def make(self, answer, runtime=None):
        ask = lambda *a: answer  # noqa: E731
        return self.fixer.Fixer(ask=ask, repo=self.dir, files=["app.js"], runtime_dir=runtime, publish=self.publish)

    def test_apply_patch_rules(self):
        self.assertEqual(self.fixer.apply_patch("a b a", "b", "c"), "a c a")
        for find in ("", "a", "zzz"):  # 패치 없음 · 두 곳 · 없는 곳
            with self.assertRaises(ValueError):
                self.fixer.apply_patch("a b a", find, "c")

    def test_live_fixable_issue_opens_pr_once(self):
        good = {"file": "app.js", "find": "return data.items.map((g) => g.name); // 버그",
                "replace": "return (data.items || []).map((g) => g.name);", "title": "fix: items 없을 때 방어", "explanation": "e"}
        fx = self.make(good, runtime=self.dir)
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        w.fixer = fx
        w.start_stage(w.find_service("WEB"), "sentry")
        xm.compose(w, 100, 30)
        issue = {"id": "WEB-C", "title": "TypeError: Cannot read properties of undefined (reading 'map')",
                 "kind": "fixable", "review_block": False, "live": True, "culprit": "Object.run(app)"}
        w.spawn_mouse(issue)
        self.assertTrue(wait_ai(w, lambda: any(r["id"] == "WEB-C" for r in w.history), timeout=8))
        rec = [r for r in w.history if r["id"] == "WEB-C"][0]
        self.assertEqual((rec["outcome"], rec["pr"], rec["url"]), ("fixed", 7, "https://github.com/x/demo/pull/7"))
        self.assertEqual(len(self.published), 1)
        self.assertIn("(data.items || [])", self.published[0][2])
        self.assertTrue(any("PR #7" in t for _, t, _, _ in w.logs))
        self.assertEqual(w.snapshot_state()["prs"][0]["issue"], "WEB-C")
        fx.submit(issue, (0, dict(issue)))  # 같은 이슈를 다시 고치려 하면 새 PR 없이 기존 PR
        self.assertEqual(len(self.published), 1)
        self.assertTrue(fx.poll()[0][1]["reused"])

    def test_failed_fix_goes_to_human_not_fake_mr(self):
        bad = {"file": "app.js", "find": "없는 코드", "replace": "x", "title": "fix: x", "explanation": "e"}
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        w.fixer = self.make(bad)
        w.start_stage(w.find_service("WEB"), "sentry")
        xm.compose(w, 100, 30)
        w.spawn_mouse({"id": "WEB-D", "title": "TypeError: x", "kind": "fixable", "review_block": False, "live": True,
                       "culprit": "run(app)"})
        w.spawn_mouse({"id": "WEB-S", "title": "TypeError: y", "kind": "fixable", "review_block": False, "live": True})
        self.assertTrue(wait_ai(w, lambda: any(r["id"] == "WEB-S" for r in w.history), timeout=8))
        self.assertEqual([r["outcome"] for r in w.history if r["id"] == "WEB-S"], ["human"])  # 스택 없는 이벤트
        self.assertTrue(wait_ai(w, lambda: any(r["id"] == "WEB-D" for r in w.history), timeout=8))
        rec = [r for r in w.history if r["id"] == "WEB-D"][0]
        self.assertEqual((rec["outcome"], rec["pr"], rec["mr"]), ("human", None, None))
        self.assertEqual(self.published, [])

    def test_mock_issue_keeps_simulated_mr(self):
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        w.fixer = self.make({})
        w.start_stage(w.find_service("WEB"), "sentry")
        xm.compose(w, 100, 30)
        w.spawn_mouse({"id": "WEB-M", "title": "RangeError: Invalid time value", "kind": "fixable", "review_block": False})
        step(w, 25)
        rec = [r for r in w.history if r["id"] == "WEB-M"][0]
        self.assertEqual((rec["pr"], rec["mr"] is not None), (None, True))
        self.assertEqual(self.published, [])


class AnswerTest(unittest.TestCase):
    def test_soldier_answers_question_from_history(self):
        fake = FakeOpenAI({"soldier_order": lambda body: {
            "soldier": "2", "intent": "answer", "issue_id": "",
            "reply": "가장 많이 나온 건 RangeError 입니다 (%s건)" % json.loads(
                body["input"].split("상황:\n", 1)[1])["stats"]["fixed"]}})
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        w.ai = ai.Brain(key="k", transport=fake)
        w.start_stage(w.find_service("WEB"), "sentry")
        xm.compose(w, 100, 30)
        w.stats["fixed"] = 4
        w.command("tell 진저 가장 큰 에러가 뭐였어?")
        self.assertTrue(wait_ai(w, lambda: any("진저: 가장 많이" in t for _, t, _, _ in w.logs)))
        self.assertIn("4건", w.soldiers[1].speech)
        ctx = json.loads(fake.bodies[0]["input"].split("상황:\n", 1)[1])
        self.assertIn("history", ctx)
        self.assertIn("answer", fake.bodies[0]["text"]["format"]["schema"]["properties"]["intent"]["enum"])


class ReplayTest(unittest.TestCase):
    def test_record_then_replay(self):
        import tempfile
        d = tempfile.mkdtemp()
        rec = os.path.join(d, "ai.jsonl")
        os.environ["XMAS_AI_RECORD"] = rec
        try:
            fake = FakeOpenAI({"sentry_triage": {"kind": "noise", "reason": "광고 스크립트", "fix": ""}})
            w = new_world()
            w.ai = ai.Brain(key="t", model="m", transport=fake)
            w.spawn_mouse({"id": "WEB-R1", "title": "Failed to fetch (ad.doubleclick.net)", "kind": "fixable",
                           "review_block": False})
            m = w.mice[-1]
            self.assertTrue(wait_ai(w, lambda: m.issue.get("ai") == "done"))
        finally:
            os.environ.pop("XMAS_AI_RECORD", None)
        w = new_world()
        xm.attach_ai(w, True, rec)
        self.assertTrue(getattr(w.ai, "replay", False))
        w.spawn_mouse({"id": "WEB-R2", "title": "Failed to fetch (ad.doubleclick.net)", "kind": "fixable",
                       "review_block": False})
        w.spawn_mouse({"id": "WEB-R3", "title": "녹화에 없는 이슈", "kind": "fixable", "review_block": False})
        w.update(1.0 / xm.FPS)
        a, b = w.mice[-2].issue, w.mice[-1].issue
        self.assertEqual((a["kind"], a["note"], a["ai"]), ("noise", "광고 스크립트", "done"))
        self.assertEqual((b["kind"], b["ai"]), ("fixable", "failed"))  # 지어내지 않는다
        w.command("tell 1 복귀")  # 명령은 키워드로
        self.assertTrue(w.soldiers[0].off)
        w.command("brief")
        self.assertTrue(any("브리핑:" in l[1] for l in w.logs))


if __name__ == "__main__":
    unittest.main()
