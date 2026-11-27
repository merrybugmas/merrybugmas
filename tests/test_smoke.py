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


if __name__ == "__main__":
    unittest.main()
