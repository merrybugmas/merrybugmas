# -*- coding: utf-8 -*-
"""비즈니스 규칙 (네트워크·OpenAI 없음): python3 -m unittest discover -s tests -v

- rules: 마크다운 표 읽기, 스택·패치와 대상 비교, 질문 초안
- 대시보드: '문의' 규칙에 걸린 쥐는 고치지 않고 팀 문의 우리로, 사람이 "확인 완료 진행" 하면 다시 고친다
- fixer: '주의' 규칙은 패치 요청·리뷰에 붙고, 패치가 '문의' 대상을 바꾸면 PR 대신 AskTeam
- 규칙이 없으면 예전과 같다
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixer  # noqa: E402
import rules  # noqa: E402
import xmas_market as xm  # noqa: E402
from test_smoke import new_world, step, wait_ai  # noqa: E402

TABLE = """# 규칙

설명 문단은 무시된다.

| id | 대상 | 규칙 | 조치 | 담당 |
|---|---|---|---|---|
| DLV-01 | `app.js:showDelivery` | 배송 예정일은 영업일 기준 | 문의 | 물류팀 |
| GIFT-01 | app.js:renderGifts, server.py | 품절 상품도 숨기지 않는다 | 주의 | 상품팀 |
| BAD-01 | app.js | 조치가 이상한 줄 | 몰라 | 누군가 |
|  | app.js | id 없는 줄 | 문의 | 누군가 |
| PAY-01 | server.py:confirm_payment | 결제 금액 계산 | 문의 | |
"""
MAP_TITLE = "TypeError: Cannot read properties of undefined (reading 'map')"
DATE_TITLE = "RangeError: Invalid time value"
DLV_FRAMES = [{"file": "app.js", "line": 67, "function": "showDelivery", "code": "new Date(d.eta).toISOString()"}]
GIFT_FRAMES = [{"file": "src/app.js", "line": 42, "function": "Object.renderGifts", "code": "data.items.map"}]


class ParseTest(unittest.TestCase):
    def test_table_rows(self):
        rs = rules.parse(TABLE)
        self.assertEqual([r["id"] for r in rs], ["DLV-01", "GIFT-01", "PAY-01"])  # 잘못된 줄은 건너뛴다
        self.assertEqual(rs[0]["targets"], [("app.js", "showDelivery")])  # 백틱은 벗긴다
        self.assertEqual(rs[1]["targets"], [("app.js", "renderGifts"), ("server.py", None)])
        self.assertEqual(rs[2]["team"], "담당 팀")  # 담당이 비면 기본값

    def test_empty_or_missing(self):
        self.assertEqual(rules.parse(""), [])
        self.assertEqual(rules.parse("| a | b |\n|---|---|\n| 1 | 2 |"), [])  # 열 이름이 다르면 규칙 표가 아니다
        self.assertEqual(rules.load("/nonexistent/BUSINESS_RULES.md"), [])

    def test_mock_rules_file_parses(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "mock-business-rules.md")
        rs = rules.load(path)
        self.assertEqual([r["action"] for r in rs], ["문의", "주의", "주의"])
        titles = {t for t, fr in xm.MOCK_FRAMES.items() if rules.match(rs, fr)}  # 예시 규칙은 mock 스택에 실제로 걸린다
        self.assertIn(DATE_TITLE, titles)
        self.assertIn(MAP_TITLE, titles)

    def test_default_path(self):
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.environ.pop("XMAS_RULES", None)
        os.environ["XMAS_FIX_REPO"] = d
        self.assertIsNone(rules.default_path())  # 레포에 규칙 파일이 없으면 규칙 없음
        open(os.path.join(d, "BUSINESS_RULES.md"), "w").close()
        self.assertEqual(rules.default_path(), os.path.join(d, "BUSINESS_RULES.md"))
        os.environ["XMAS_RULES"] = "/x/rules.md"
        self.assertEqual(rules.default_path(), "/x/rules.md")  # XMAS_RULES 가 먼저


class MatchTest(unittest.TestCase):
    RS = rules.parse(TABLE)

    def test_frames(self):
        self.assertEqual([r["id"] for r in rules.match(self.RS, DLV_FRAMES)], ["DLV-01"])
        self.assertEqual([r["id"] for r in rules.match(self.RS, GIFT_FRAMES)], ["GIFT-01"])  # 경로 끝·앞붙은 이름도
        other = [{"file": "app.js", "line": 1, "function": "loadGifts", "code": ""}]
        self.assertEqual(rules.match(self.RS, other), [])  # 함수가 다르면 안 걸린다
        self.assertEqual(rules.match(self.RS, [{"file": "myapp.js", "function": "showDelivery"}]), [])  # 이름 일부만 같은 파일
        self.assertEqual(rules.match(self.RS, None), [])
        self.assertEqual(rules.match([], DLV_FRAMES), [])

    def test_patch(self):
        diff = "@@ -1 +1 @@ function showDelivery(d) {\n-  a\n+  b\n"
        self.assertEqual([r["id"] for r in rules.match_patch(self.RS, "app.js", diff)], ["DLV-01"])
        self.assertEqual([r["id"] for r in rules.match_patch(self.RS, "server.py", "")], ["GIFT-01"])  # 파일 전체 대상
        self.assertEqual(rules.match_patch(self.RS, "app.js", "@@ loadGifts @@"), [])

    def test_blocking_brief_notes_question(self):
        hit = rules.match(self.RS, DLV_FRAMES + GIFT_FRAMES)
        self.assertEqual([r["id"] for r in rules.blocking(hit)], ["DLV-01"])
        self.assertEqual(rules.brief(hit)[0], {"id": "DLV-01", "action": "문의", "team": "물류팀"})
        self.assertIn("[GIFT-01 · 주의 · 상품팀] 품절 상품도 숨기지 않는다", rules.notes(hit))
        q = rules.question({"id": "WEB-9", "title": DATE_TITLE, "frames": DLV_FRAMES}, rules.blocking(hit))
        self.assertTrue(q.startswith("[물류팀] WEB-9 (RangeError: Invalid time value) 수정이 DLV-01(배송 예정일은 영업일 기준)"))
        self.assertIn("에러 위치: app.js:67 showDelivery.", q)


def rule_world():
    w = new_world("WEB", "sentry")
    w.rules = rules.parse(TABLE)
    return w


def spawn(w, iid, title, frames, **extra):
    w.spawn_mouse(dict({"id": iid, "title": title, "kind": "fixable", "review_block": False, "frames": frames}, **extra))


def outcomes(w, iid):
    return [r["outcome"] for r in w.history if r["id"] == iid]


class DashboardAskTest(unittest.TestCase):
    def test_blocking_rule_cages_mouse_and_proceed_fixes(self):
        w = rule_world()
        spawn(w, "WEB-D1", DATE_TITLE, DLV_FRAMES)
        step(w, 40)
        self.assertEqual(outcomes(w, "WEB-D1"), ["ask"])  # MR 없이 팀 문의
        m = [m for m in w.mice if m.issue["id"] == "WEB-D1"][0]
        self.assertEqual(m.state, "caged")
        st = {x["id"]: x for x in w.snapshot_state()["mice"]}["WEB-D1"]
        self.assertTrue(st["ask"])
        self.assertEqual(st["rules"], [{"id": "DLV-01", "action": "문의", "team": "물류팀"}])
        self.assertTrue(st["question"].startswith("[물류팀] WEB-D1"))
        self.assertEqual(w.stats["ask"], 1)
        hist = [r for r in w.snapshot_state()["history"] if r["id"] == "WEB-D1"][0]
        self.assertEqual(hist["question"], st["question"])

        w.auto = True
        step(w, 40)  # 팀 문의 쥐는 시간이 지나도 저절로 풀리지 않는다
        self.assertEqual(m.state, "caged")
        w.auto = False

        w.order_keywords("WEB-D1 잡아", "루돌프")  # 그냥 잡으라고 하면 팀 확인을 기다린다고 답한다
        self.assertIn("팀 문의 중", w.soldiers[0].speech)
        w.order_keywords("WEB-D1 확인 완료 진행", "루돌프")
        step(w, 40)
        self.assertEqual(outcomes(w, "WEB-D1"), ["ask", "fixed"])
        self.assertTrue(m.issue["ask_ok"])

    def test_caution_rule_does_not_stop_fix(self):
        w = rule_world()
        spawn(w, "WEB-G1", MAP_TITLE, GIFT_FRAMES)
        step(w, 40)
        self.assertEqual(outcomes(w, "WEB-G1"), ["fixed"])
        rec = [r for r in w.history if r["id"] == "WEB-G1"][0]
        self.assertEqual(rec["rules"], [{"id": "GIFT-01", "action": "주의", "team": "상품팀"}])

    def test_proceed_needs_ask_mouse(self):
        w = rule_world()
        w.order_keywords("WEB-NOPE 확인 완료 진행", "루돌프")
        self.assertIn("팀 문의 중인 쥐가 아닙니다", w.soldiers[0].speech)

    def test_proceed_keywords(self):
        def intent(text):
            return next((n for n, words in xm.ORDER_WORDS if any(k in text.lower() for k in words)), None)
        self.assertEqual(intent("WEB-D1 확인 완료 진행"), "proceed")
        self.assertEqual(intent("담당자 확인완료, 진행해"), "proceed")  # human 보다 먼저
        self.assertEqual(intent("진행 상황 보고"), "report")  # 그냥 '진행' 은 proceed 가 아니다

    def test_without_rules_behavior_is_unchanged(self):
        w = new_world("WEB", "sentry")
        self.assertEqual(w.rules, [])
        spawn(w, "WEB-D2", DATE_TITLE, DLV_FRAMES)
        step(w, 40)
        self.assertEqual(outcomes(w, "WEB-D2"), ["fixed"])
        st = [r for r in w.snapshot_state()["history"] if r["id"] == "WEB-D2"][0]
        self.assertEqual((st["rules"], st["question"]), ([], ""))


class FixerRulesTest(unittest.TestCase):
    SRC = ("function showDelivery(d) {\n  out.textContent = new Date(d.eta).toISOString(); // 버그\n}\n"
           "function renderGifts(data) {\n  return data.items.map((g) => g.name); // 버그\n}\n")

    def setUp(self):
        self.d = tempfile.mkdtemp()  # publish 를 가짜로 끼우므로 git·gh 는 쓰지 않는다
        self.addCleanup(shutil.rmtree, self.d, True)
        with open(os.path.join(self.d, "app.js"), "w", encoding="utf-8") as f:
            f.write(self.SRC)
        self.asked, self.published, self.reviewed = [], [], []

    def fixer(self, find, replace):
        def ask(instructions, user, name, schema):
            self.asked.append(user)
            return {"file": "app.js", "find": find, "replace": replace, "title": "fix: x", "explanation": "e"}

        def publish(issue, f, new, title, body):
            self.published.append(body)
            return {"pr": 3, "url": "https://github.com/x/demo/pull/3", "title": title}

        def review(issue, f, diff, repo):
            self.reviewed.append(issue.get("rule_notes"))
            return {"verdict": "approve", "reason": "ok"}
        fx = fixer.Fixer(ask=ask, repo=self.d, files=["app.js"], publish=publish, review=review)
        fx.rules = rules.parse(TABLE)
        return fx

    def test_caution_notes_reach_prompt_review_and_pr(self):
        fx = self.fixer("return data.items.map((g) => g.name); // 버그", "return (data.items || []).map((g) => g.name);")
        issue = {"id": "WEB-G", "title": MAP_TITLE, "frames": GIFT_FRAMES, "rule_notes": rules.notes(rules.match(fx.rules, GIFT_FRAMES))}
        res = fx.fix(issue)
        self.assertEqual(res["pr"], 3)
        self.assertIn("지켜야 할 비즈니스 규칙", self.asked[0])
        self.assertIn("GIFT-01", self.asked[0])
        self.assertIn("GIFT-01", self.reviewed[0])
        self.assertIn("📜 **비즈니스 규칙**", self.published[0])

    def test_patch_touching_blocking_target_raises_ask_team(self):
        fx = self.fixer("out.textContent = new Date(d.eta).toISOString(); // 버그",
                        "out.textContent = d.eta ? new Date(d.eta).toISOString() : '-';")
        issue = {"id": "WEB-M", "title": MAP_TITLE, "frames": GIFT_FRAMES}  # 스택은 안 걸렸지만 패치가 showDelivery 를 바꾼다
        with self.assertRaises(fixer.AskTeam) as cm:
            fx.fix(issue)
        self.assertEqual([r["id"] for r in cm.exception.rules], ["DLV-01"])
        self.assertEqual((self.reviewed, self.published), ([], []))  # 리뷰·PR 전에 멈춘다
        issue["ask_ok"] = True  # 팀이 확인했으면 고친다
        self.assertEqual(fx.fix(issue)["pr"], 3)

    def test_prompt_unchanged_without_rules(self):
        fx = self.fixer("return data.items.map((g) => g.name); // 버그", "return (data.items || []).map((g) => g.name);")
        fx.rules = []
        fx.fix({"id": "WEB-G", "title": MAP_TITLE})
        self.assertNotIn("비즈니스 규칙", self.asked[0])
        self.assertNotIn("비즈니스 규칙", self.published[0])

    def test_live_ask_team_goes_to_ask_cage(self):
        fx = self.fixer("out.textContent = new Date(d.eta).toISOString(); // 버그",
                        "out.textContent = d.eta ? new Date(d.eta).toISOString() : '-';")
        xm.R.seed(7)
        w = xm.World()
        w.auto = False
        w.fixer = fx
        w.start_stage(w.find_service("WEB"), "sentry")
        xm.compose(w, 100, 30)
        spawn(w, "WEB-L", MAP_TITLE, [{"file": "app.js", "line": 5, "function": "loadGifts", "code": ""}], live=True)
        self.assertTrue(wait_ai(w, lambda: any(r["id"] == "WEB-L" for r in w.history), timeout=8))
        self.assertEqual(outcomes(w, "WEB-L"), ["ask"])
        self.assertEqual(self.published, [])
        st = [x for x in w.snapshot_state()["mice"] if x["id"] == "WEB-L"][0]
        self.assertTrue(st["ask"])
        self.assertIn("DLV-01", st["question"])


if __name__ == "__main__":
    unittest.main()
