# -*- coding: utf-8 -*-
"""비즈니스 규칙 (네트워크·OpenAI 없음): python3 -m unittest discover -s tests -v

- rules: 마크다운 표 읽기, 제목·culprit·패치와 대상 비교, 질문 초안
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
| SRCH-01 | 제목:hydration failed, 제목: | 검색 결과 수는 서버 기준 | 주의 | 검색팀 |
"""
MAP_TITLE = "TypeError: Cannot read properties of undefined (reading 'map')"
DATE_TITLE = "RangeError: Invalid time value"
DLV_CULPRIT = "showDelivery(app.js)"  # Sentry culprit 은 플랫폼마다 모양이 다르다
GIFT_CULPRIT = "Object.renderGifts(src/app)"


class ParseTest(unittest.TestCase):
    def test_table_rows(self):
        rs = rules.parse(TABLE)
        self.assertEqual([r["id"] for r in rs], ["DLV-01", "GIFT-01", "PAY-01", "SRCH-01"])  # 잘못된 줄은 건너뛴다
        self.assertEqual(rs[0]["targets"], [{"file": "app.js", "func": "showDelivery"}])  # 백틱은 벗긴다
        self.assertEqual(rs[1]["targets"], [{"file": "app.js", "func": "renderGifts"}, {"file": "server.py", "func": None}])
        self.assertEqual(rs[2]["team"], "담당 팀")  # 담당이 비면 기본값
        self.assertEqual(rs[3]["targets"], [{"title": "hydration failed"}])  # 빈 제목 키워드는 버린다

    def test_empty_or_missing(self):
        self.assertEqual(rules.parse(""), [])
        self.assertEqual(rules.parse("| a | b |\n|---|---|\n| 1 | 2 |"), [])  # 열 이름이 다르면 규칙 표가 아니다
        self.assertEqual(rules.load("/nonexistent/BUSINESS_RULES.md"), [])

    def test_mock_rules_file_parses(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "mock-business-rules.md")
        rs = rules.load(path)
        self.assertEqual([r["action"] for r in rs], ["문의", "주의", "주의"])
        titles = {c[1] for c in xm.ISSUE_CATALOG if rules.match(rs, {"title": c[1]})}  # 예시 규칙은 mock 제목에 실제로 걸린다
        self.assertIn(DATE_TITLE, titles)
        self.assertIn(MAP_TITLE, titles)
        self.assertEqual([r["id"] for r in rules.blocking(rules.match(rs, {"title": DATE_TITLE}))], ["DLV-01"])

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

    def ids(self, **issue):
        return [r["id"] for r in rules.match(self.RS, issue)]

    def test_culprit(self):
        self.assertEqual(self.ids(culprit=DLV_CULPRIT), ["DLV-01"])
        self.assertEqual(self.ids(culprit=GIFT_CULPRIT), ["GIFT-01"])  # 확장자 뺀 이름·앞붙은 이름도
        self.assertEqual(self.ids(culprit="server in confirm_payment"), ["GIFT-01", "PAY-01"])  # 파이썬 모양, 파일 전체 대상도
        self.assertEqual(self.ids(culprit="loadGifts(app.js)"), [])  # 함수가 다르면 안 걸린다
        self.assertEqual(self.ids(culprit="showDeliveryAll(app.js)"), [])  # 이름 일부만 같은 함수
        self.assertEqual(self.ids(culprit="showDelivery(myapp.js)"), [])  # 이름 일부만 같은 파일
        self.assertEqual(self.ids(), [])
        self.assertEqual(rules.match(self.RS, None), [])
        self.assertEqual(rules.match([], {"culprit": DLV_CULPRIT}), [])

    def test_title(self):
        self.assertEqual(self.ids(title="Hydration failed: server/client text mismatch"), ["SRCH-01"])  # 대소문자 무시
        self.assertEqual(self.ids(title="app.js showDelivery"), [])  # 파일·함수 대상은 제목에 비교하지 않는다

    def test_patch(self):
        diff = "@@ -1 +1 @@ function showDelivery(d) {\n-  a\n+  b\n"
        self.assertEqual([r["id"] for r in rules.match_patch(self.RS, "app.js", diff)], ["DLV-01"])
        self.assertEqual([r["id"] for r in rules.match_patch(self.RS, "server.py", "")], ["GIFT-01"])  # 파일 전체 대상
        self.assertEqual(rules.match_patch(self.RS, "app.js", "@@ loadGifts @@"), [])
        self.assertEqual(rules.match_patch(self.RS, "search.js", "hydration failed"), [])  # 제목 대상은 패치에 비교하지 않는다

    def test_blocking_brief_notes_question(self):
        hit = rules.match(self.RS, {"culprit": DLV_CULPRIT}) + rules.match(self.RS, {"culprit": GIFT_CULPRIT})
        self.assertEqual([r["id"] for r in rules.blocking(hit)], ["DLV-01"])
        self.assertEqual(rules.brief(hit)[0], {"id": "DLV-01", "action": "문의", "team": "물류팀"})
        self.assertIn("[GIFT-01 · 주의 · 상품팀] 품절 상품도 숨기지 않는다", rules.notes(hit))
        q = rules.question({"id": "WEB-9", "title": DATE_TITLE, "culprit": DLV_CULPRIT}, rules.blocking(hit))
        self.assertTrue(q.startswith("[물류팀] WEB-9 (RangeError: Invalid time value) 수정이 DLV-01(배송 예정일은 영업일 기준)"))
        self.assertIn("에러 위치: showDelivery(app.js).", q)
        self.assertNotIn("에러 위치", rules.question({"id": "WEB-9", "title": DATE_TITLE}, rules.blocking(hit)))


def rule_world():
    w = new_world("WEB", "sentry")
    w.rules = rules.parse(TABLE)
    return w


def spawn(w, iid, title, culprit, **extra):
    w.spawn_mouse(dict({"id": iid, "title": title, "kind": "fixable", "review_block": False, "culprit": culprit}, **extra))


def outcomes(w, iid):
    return [r["outcome"] for r in w.history if r["id"] == iid]


class DashboardAskTest(unittest.TestCase):
    def test_blocking_rule_cages_mouse_and_proceed_fixes(self):
        w = rule_world()
        spawn(w, "WEB-D1", DATE_TITLE, DLV_CULPRIT)
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
        spawn(w, "WEB-G1", MAP_TITLE, GIFT_CULPRIT)
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
        spawn(w, "WEB-D2", DATE_TITLE, DLV_CULPRIT)
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
        issue = {"id": "WEB-G", "title": MAP_TITLE, "culprit": GIFT_CULPRIT}
        issue["rule_notes"] = rules.notes(rules.match(fx.rules, issue))
        res = fx.fix(issue)
        self.assertEqual(res["pr"], 3)
        self.assertIn("지켜야 할 비즈니스 규칙", self.asked[0])
        self.assertIn("GIFT-01", self.asked[0])
        self.assertIn("GIFT-01", self.reviewed[0])
        self.assertIn("📜 **비즈니스 규칙**", self.published[0])

    def test_patch_touching_blocking_target_raises_ask_team(self):
        fx = self.fixer("out.textContent = new Date(d.eta).toISOString(); // 버그",
                        "out.textContent = d.eta ? new Date(d.eta).toISOString() : '-';")
        issue = {"id": "WEB-M", "title": MAP_TITLE, "culprit": GIFT_CULPRIT}  # 이슈 위치는 안 걸렸지만 패치가 showDelivery 를 바꾼다
        with self.assertRaises(fixer.AskTeam) as cm:
            fx.fix(issue)
        self.assertEqual([r["id"] for r in cm.exception.rules], ["DLV-01"])
        self.assertEqual((self.reviewed, self.published), ([], []))  # 리뷰·PR 전에 멈춘다
        issue["ask_ok"] = True  # 팀이 확인했으면 고친다
        self.assertEqual(fx.fix(issue)["pr"], 3)

    def test_change_deep_inside_blocking_function_is_caught(self):
        body = "".join("  const v%d = %d;\n" % (i, i) for i in range(20))
        with open(os.path.join(self.d, "app.js"), "w", encoding="utf-8") as f:
            f.write("function showDelivery(d) {\n" + body + "  out.textContent = new Date(d.eta).toISOString(); // 버그\n}\n")
        fx = self.fixer("out.textContent = new Date(d.eta).toISOString(); // 버그",
                        "out.textContent = d.eta ? new Date(d.eta).toISOString() : '-';")
        with self.assertRaises(fixer.AskTeam):  # diff 에 함수 이름이 안 보여도 앞 줄에서 찾는다
            fx.fix({"id": "WEB-M2", "title": DATE_TITLE})

    def test_enclosing_def(self):
        js = "function a() {\n  x();\n}\nconst b = async (q) => {\n  if (q) {\n    y();\n  }\n};\n"
        self.assertEqual(fixer.enclosing_def(js, "y();"), "const b = async (q) => {")  # if 줄은 함수가 아니다
        self.assertEqual(fixer.enclosing_def(js, "x();"), "function a() {")
        self.assertEqual(fixer.enclosing_def("def pay(o):\n    return o.total\n", "return o.total"), "def pay(o):")
        self.assertEqual(fixer.enclosing_def(js, "없는 줄"), "")

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
        spawn(w, "WEB-L", MAP_TITLE, "loadGifts(app.js)", live=True)
        self.assertTrue(wait_ai(w, lambda: any(r["id"] == "WEB-L" for r in w.history), timeout=8))
        self.assertEqual(outcomes(w, "WEB-L"), ["ask"])
        self.assertEqual(self.published, [])
        st = [x for x in w.snapshot_state()["mice"] if x["id"] == "WEB-L"][0]
        self.assertTrue(st["ask"])
        self.assertIn("DLV-01", st["question"])


if __name__ == "__main__":
    unittest.main()
