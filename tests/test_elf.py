# -*- coding: utf-8 -*-
"""엘프(트래픽 대응 에이전트) 테스트 — 네트워크·OpenAI 없이 (가짜 /metrics · 가짜 레버 · 가짜 AI)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import ai  # noqa: E402
import elf  # noqa: E402
import xmas_market as xm  # noqa: E402
from test_smoke import FakeOpenAI, step, wait_ai  # noqa: E402


def mock_world(rps=None, ai_brain=None, ai_max=5):
    xm.R.seed(7)
    w = xm.World()
    w.auto = False  # 자동 급증 연출 끔 (엘프 자동 대응은 켜져 있다)
    w.ai = ai_brain
    w.elf.ai_max = ai_max
    w.start_stage(w.find_service("ALL"), "traffic")
    xm.compose(w, 100, 30)
    if rps is not None:
        w.src.rps = lambda t: rps
    return w


def text_of(w):
    return " ".join(t for _, t, _, _ in w.logs)


class MockElfTest(unittest.TestCase):
    def test_calm_traffic_no_action(self):
        w = mock_world(rps=900.0)  # 3000 의 30%
        step(w, 5)
        self.assertEqual(w.elf.state, "idle")
        self.assertEqual(w.elf.actions, [])

    def test_spike_rule_cache_then_capacity_then_resolved(self):
        w = mock_world(rps=3300.0)  # 부하 1.1
        step(w, 3)
        self.assertTrue(w.elf.levers["cache"])  # 1단계: 캐시 (부하 1.1/1.2 = 0.92, 아직 경보)
        self.assertEqual(w.elf.actions[0]["by"], "규칙")
        step(w, 11)
        self.assertEqual(w.elf.levers["capacity"], 4)  # 2단계: 계산대 증설
        step(w, 11)
        self.assertEqual(w.elf.state, "idle")
        self.assertIn("해소!", text_of(w))
        self.assertFalse(w.elf.levers["waitroom"])  # 번호표까지 갈 필요 없음
        snap = w.snapshot_state()["traffic"]["elf"]
        self.assertEqual(snap["levers"], {"cache": True, "capacity": 4, "waitroom": False})
        self.assertTrue(snap["actions"][-1]["result"].startswith("효과 있음"))

    def test_waitroom_needs_approval(self):
        w = mock_world(rps=20000.0)  # 계산대 8개로도 넘친다
        step(w, 50)
        self.assertEqual(w.elf.levers["capacity"], elf.MAX_CAP)
        self.assertEqual(w.elf.state, "ask")
        self.assertFalse(w.elf.levers["waitroom"])  # 승인 전에는 켜지 않는다
        w.command("tell 엘프야 승인")
        self.assertTrue(w.elf.levers["waitroom"])
        self.assertEqual(w.elf.state, "watch")

    def test_ordinary_words_do_not_approve(self):
        w = mock_world(rps=20000.0)
        step(w, 50)
        self.assertEqual(w.elf.state, "ask")
        w.command("tell 엘프 상황 보고해줘")
        w.command("tell 엘프 좋아요 계속 지켜봐 줘")
        self.assertFalse(w.elf.levers["waitroom"])
        self.assertEqual(w.elf.pending, "waitroom")
        w.command("tell 엘프 ok")
        self.assertTrue(w.elf.levers["waitroom"])

    def test_manual_waitroom_while_asking_moves_on(self):
        w = mock_world(rps=20000.0)
        step(w, 50)
        w.command("tell 엘프 번호표 켜")
        self.assertEqual((w.elf.state, w.elf.pending), ("watch", None))

    def test_unanswered_ask_escalates_to_human(self):
        w = mock_world(rps=20000.0)
        step(w, 50)
        self.assertEqual(w.elf.state, "ask")
        step(w, 70)
        self.assertNotEqual(w.elf.state, "ask")
        self.assertIsNone(w.elf.pending)
        self.assertFalse(w.elf.levers["waitroom"])
        self.assertIn("승인을 60초 못 받음", text_of(w))

    def test_odd_digits_do_not_crash(self):
        w = mock_world(rps=600.0)
        w.command("tell 엘프 계산대 ²")
        w.command("elf 계산대 ٣")
        self.assertEqual(w.elf.levers["capacity"], 6)  # 숫자로 안 보고 +2 씩

    def test_restore_after_calm(self):
        w = mock_world(rps=3300.0)
        step(w, 3)
        self.assertTrue(w.elf.levers["cache"])
        w.src.rps = lambda t: 600.0
        step(w, 40)
        self.assertEqual(w.elf.levers, elf.BASE)
        self.assertIn("원래대로", text_of(w))

    def test_chat_commands_keyword_only(self):
        brain = ai.Brain(key="k", transport=FakeOpenAI({}), workers=1)
        w = mock_world(rps=600.0, ai_brain=brain)
        w.command("tell 엘프 캐시 켜")
        w.command("elf 계산대 6개")
        self.assertEqual(w.elf.levers, {"cache": True, "capacity": 6, "waitroom": False})
        w.command("tell 엘프 번호표 켜줘")
        self.assertTrue(w.elf.levers["waitroom"])  # 사람이 직접 시킨 것은 승인 불필요
        w.command("tell 엘프 보고")
        self.assertIn("계산대 6", w.elf.speech)
        w.command("tell 엘프 원복")
        self.assertEqual(w.elf.levers, elf.BASE)
        self.assertEqual(brain.calls, 0)  # 엘프 명령은 OpenAI 를 쓰지 않는다
        w.command("tell 엘프 자동 꺼")
        w.src.rps = lambda t: 5000.0
        step(w, 5)
        self.assertEqual(w.elf.actions, [])

    def test_renders_everywhere(self):
        w = mock_world(rps=3300.0)
        step(w, 3)
        for v in ("traffic", "overview"):
            w.set_view(v)
            out = "\n".join(xm.ANSI.sub("", l) for l in xm.compose(w, 120, 34))
            self.assertIn("엘프", out)
            self.assertIn("캐시 ON", out)
        xm.compose(w, 60, 22)


class AiElfTest(unittest.TestCase):
    def test_ai_picks_lever_once_per_alarm_and_budget(self):
        fake = FakeOpenAI({"traffic_lever": {"lever": "capacity", "reason": "대기 많음 부하 1.1", "say": "계산대 열게요"}})
        brain = ai.Brain(key="k", transport=fake, workers=1)
        w = mock_world(rps=3300.0, ai_brain=brain, ai_max=1)
        step(w, 2.5)
        self.assertEqual(w.elf.state, "watch")  # AI 를 기다리지 않고 규칙이 바로 첫 레버를 당긴다
        self.assertTrue(w.elf.levers["cache"])
        self.assertEqual(w.elf.actions[-1]["by"], "규칙")
        self.assertTrue(wait_ai(w, lambda: w.elf.ai_next is not None))  # AI 는 그동안 다음 조치를 고른다
        self.assertEqual(w.elf.ai_next["lever"], "capacity")
        asked = [b for b in fake.bodies if b["text"]["format"]["name"] == "traffic_lever"]
        self.assertEqual(len(asked), 1)
        self.assertIn('"just_pulled": "cache"', asked[0]["input"])
        w.src.rps = lambda t: 9000.0
        step(w, 11)  # 아직 넘침 → AI 가 고른 계산대
        self.assertEqual(w.elf.levers["capacity"], 4)
        self.assertEqual(w.elf.actions[-1]["by"], "AI")
        self.assertEqual(sum(1 for b in fake.bodies if b["text"]["format"]["name"] == "traffic_lever"), 1)

    def test_budget_spans_alarms(self):
        fake = FakeOpenAI({"traffic_lever": {"lever": "cache", "reason": "응답 느림", "say": ""}})
        w = mock_world(rps=3300.0, ai_brain=ai.Brain(key="k", transport=fake, workers=1), ai_max=1)
        step(w, 2.5)
        wait_ai(w, lambda: w.elf.state == "watch")
        w.src.rps = lambda t: 600.0
        step(w, 60)  # 해소·원복
        w.src.rps = lambda t: 3300.0
        step(w, 3)  # 두 번째 경보: 예산 소진 → 규칙
        self.assertEqual(sum(1 for b in fake.bodies if b["text"]["format"]["name"] == "traffic_lever"), 1)
        self.assertIn("AI 예산 1회 다 씀", text_of(w))
        self.assertTrue(w.elf.levers["cache"])

    def test_ai_repeat_lever_or_failure_falls_back_to_rule(self):
        fake = FakeOpenAI({}, fail=True)
        brain = ai.Brain(key="k", transport=fake, workers=1)
        w = mock_world(rps=3300.0, ai_brain=brain)
        step(w, 2.5)
        self.assertTrue(wait_ai(w, lambda: w.elf.state == "watch", timeout=8))
        self.assertTrue(w.elf.levers["cache"])
        self.assertEqual(w.elf.actions[-1]["by"], "규칙")

    def test_replay_brain_is_not_asked(self):
        class Replay:
            replay = True
            calls = 0

            def submit(self, kind, *a):
                if kind == "traffic":
                    raise AssertionError("녹화 재생에는 묻지 않는다")

            def poll(self):
                return []
        w = mock_world(rps=3300.0, ai_brain=Replay())
        step(w, 3)
        self.assertTrue(w.elf.levers["cache"])


class LiveElfTest(unittest.TestCase):
    """실서버 모드: 가짜 /metrics(레버 게이지 포함) + 가짜 /admin/levers."""

    def setUp(self):
        import traffic_live
        self.now, self.total, self.err = [100.0], [0], [0]
        self.dur = [0.0]
        self.server = {"cache": False, "capacity": 2, "waitroom": False, "waiting": 0, "inflight": 0}
        self.posts = []

        def metrics(url):
            s = self.server
            return ('http_requests_total{status="2xx"} %d\nhttp_requests_total{status="5xx"} %d\n'
                    'http_request_duration_seconds_sum %f\nhttp_request_duration_seconds_count %d\n'
                    'demo_capacity %d\ndemo_cache_enabled %d\ndemo_waitroom_enabled %d\ndemo_waiting %d\n'
                    'demo_inflight %d\n' % (self.total[0] - self.err[0], self.err[0], self.dur[0], self.total[0],
                                            s["capacity"], s["cache"], s["waitroom"], s["waiting"], s["inflight"]))

        def post(url, body):
            self.posts.append(body)
            self.server.update(body)
            return dict(self.server)
        self.feed = traffic_live.TrafficFeed(transport=metrics, interval=3600, clock=lambda: self.now[0])
        self.feed.metric, self.feed.peak = "http_requests_total", 100.0
        self.levers = traffic_live.LeverClient(url="http://test/admin/levers", transport=post)
        xm.R.seed(7)
        self.w = xm.World()
        self.w.auto = False
        self.assertTrue(xm.attach_traffic(self.w, self.feed, levers=self.levers))
        self.w.start_stage(self.w.find_service("WEB"), "traffic")
        xm.compose(self.w, 100, 30)

    def tick(self, req, err, latency, sec=1.0):
        self.total[0] += req
        self.err[0] += err
        self.dur[0] += req * latency
        self.now[0] += sec
        self.feed.sample()
        step(self.w, sec)

    def test_overload_pulls_real_lever(self):
        w = self.w
        self.server["waiting"] = 12
        for _ in range(4):
            self.tick(110, 17, 0.77)  # 5xx 15% · 응답 770ms
        wait_ai(w, lambda: bool(self.posts))
        self.assertEqual(self.posts[0], {"cache": True})
        wait_ai(w, lambda: w.elf.sending == 0)
        self.assertTrue(w.elf.levers["cache"])
        step(w, 1)  # 서버 확인 직후의 /metrics 는 아직 예전 값일 수 있다: 같아질 때까지 덮지 않는다
        self.assertTrue(w.elf.levers["cache"])
        self.server["cache"] = False  # 변경 전에 시작된 읽기가 늦게 도착한 경우
        self.tick(110, 0, 0.05)
        self.assertTrue(w.elf.levers["cache"])
        self.server["cache"] = True
        self.tick(110, 0, 0.05)
        self.assertTrue(w.elf.levers["cache"])  # 새로 읽은 게이지도 ON
        self.assertIn("서버 반영", text_of(w))
        self.assertFalse(w.snapshot_state()["traffic"]["elf"]["pressure"]["mock"])

    def test_api_errors_without_overload_calls_human(self):
        w = self.w
        for _ in range(4):
            self.tick(30, 6, 0.03)  # 부하는 낮은데 5xx 20%: 특정 API 장애
        self.assertEqual(self.posts, [])
        self.assertEqual(w.elf.state, "flag")
        self.assertIn("담당자", w.elf.speech)

    def test_no_lever_server_only_alerts(self):
        import traffic_live
        f = traffic_live.TrafficFeed(transport=lambda url: 'http_requests_total{status="5xx"} 0\n',
                                     interval=3600, clock=lambda: 0.0)
        w = xm.World()
        self.assertTrue(xm.attach_traffic(w, f))
        self.assertIsNone(w.elf.client)
        w.command("tell 엘프 캐시 켜")
        self.assertFalse(w.elf.levers["cache"])  # 없는 레버를 켠 척하지 않는다


if __name__ == "__main__":
    unittest.main()
