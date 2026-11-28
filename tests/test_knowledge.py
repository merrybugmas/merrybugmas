"""지식 검색/격리/프롬프트/PR/화면/이벤트 수집 회귀 테스트. 외부 호출 없음."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ai
import fixer
import knowledge
import rules
import sentry_live
import xmas_market as xm
from test_smoke import FakeOpenAI, new_world, wait_ai

ISSUE = {"id": "WEB-KB1", "title": "TypeError: Cannot read properties of null (reading 'rate')",
         "culprit": "cartTotal (app.js)", "kind": "fixable", "review_block": False, "project": "web"}


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.kb = knowledge.KnowledgeBase()

    def retrieve(self, issue=None, repo="merrybugmas/demo-web", service="WEB"):
        return self.kb.retrieve(issue or ISSUE, repo, service)

    def test_relations_and_sources(self):
        self.assertEqual(self.kb.errors, [])
        ctx = self.retrieve()
        self.assertEqual(ctx["cases"][0]["id"], "CASE-001")
        self.assertIn(["CASE-001", "constrained-by", "RULE-COUPON"], ctx["relations"])
        self.assertTrue(ctx["cases"][0]["sources"])
        self.assertEqual(ctx["cases"][0]["status"], "reference")

    def test_unknown_repo_service_and_generic_error_have_no_match(self):
        for ctx in (self.retrieve(repo="other/demo-web"), self.retrieve(service="PAY"),
                    self.retrieve({"title": "TypeError: broken", "culprit": "app.js"})):
            self.assertFalse(ctx["cases"])
        self.assertEqual(self.retrieve(repo="")["reason"], "scope-missing")

    def test_candidate_rejected_and_deprecated_are_not_searchable(self):
        for status in ("candidate", "rejected", "deprecated"):
            self.kb.cases["CASE-001"]["status"] = status
            self.assertFalse(self.retrieve()["cases"])

    def test_frontend_cases_do_not_leak_into_server_errors(self):
        self.assertFalse(self.retrieve(dict(ISSUE, layer="server"))["cases"])

    def test_extension_origin_excludes_our_code_fix(self):
        issue = dict(ISSUE, culprit="chrome-extension://id/app.js")
        self.assertFalse(self.retrieve(issue)["cases"])

    def test_snapshot_does_not_change_with_corpus(self):
        ctx = self.retrieve()
        self.kb.rules["RULE-COUPON"]["text"] = "changed"
        self.assertNotEqual(ctx["rules"][0]["text"], "changed")
        ctx["cases"][0]["title"] = "mutated"
        self.assertNotEqual(self.kb.cases["CASE-001"]["title"], "mutated")

    def test_stable_ranking_and_top_k(self):
        issue = dict(ISSUE, title=ISSUE["title"] + " Invalid Date sessionStorage ChunkLoadError")
        a = self.retrieve(issue)
        self.assertEqual(len(a["cases"]), 3)
        self.assertEqual(a, self.retrieve(issue))

    def test_corrupt_and_dangling_references_disable_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(str(knowledge.DEFAULT_DIR), tmp, dirs_exist_ok=True)
            path = Path(tmp) / "cases/01-coupon.json"
            c = json.loads(path.read_text())
            c["rule_ids"] = ["MISSING"]
            path.write_text(json.dumps(c))
            kb = knowledge.KnowledgeBase(tmp)
            self.assertTrue(kb.errors)
            self.assertFalse(kb.retrieve(ISSUE, "merrybugmas/demo-web", "WEB")["cases"])
            path.write_text("{")
            self.assertTrue(knowledge.KnowledgeBase(tmp).errors)

    def test_project_mapping_is_explicit(self):
        self.assertEqual(self.kb.service_for("merrybugmas/demo-web", "web"), "WEB")
        self.assertEqual(self.kb.service_for("other/repo", "web"), "")

    def test_repo_identity_uses_origin(self):
        with patch.dict(os.environ, {"XMAS_KB_REPO": ""}), patch("knowledge.subprocess.run") as run:
            for url in ("git@github.com:merrybugmas/demo-web.git", "https://github.com/merrybugmas/demo-web.git"):
                run.return_value = subprocess.CompletedProcess([], 0, url + "\n")
                self.assertEqual(knowledge.repo_name("/tmp/repo"), "merrybugmas/demo-web")


class IntegrationTest(unittest.TestCase):
    def world(self, live=False):
        w = new_world()
        w.src.poll_issues = lambda *args: []
        w.kb = knowledge.KnowledgeBase()
        w.kb_repo = "merrybugmas/demo-web" if live else ""
        return w

    def test_triage_receives_snapshot_and_usage_is_recorded(self):
        w = self.world()
        fake = FakeOpenAI({"sentry_triage": {"kind": "fixable", "reason": "쿠폰 누락", "fix": "기본 할인율"}})
        w.ai = ai.Brain(key="fake", model="fake", transport=fake)
        w.spawn_mouse(dict(ISSUE))
        m = w.mice[-1]
        self.assertTrue(wait_ai(w, lambda: m.issue.get("ai") == "done"))
        self.assertIn("CASE-001", fake.bodies[0]["input"])
        self.assertIn("RULE-COUPON", fake.bodies[0]["input"])
        self.assertEqual(m.issue["knowledge_usage"], "triage")
        w.record(m, "fixed", w.soldiers[0], pr={"pr": 123, "url": "https://example.invalid/pull/123", "title": "fix"})
        r = w.snapshot_state()["history"][-1]
        self.assertEqual(r["resolution_status"], "pr-open")
        self.assertEqual(r["knowledge"]["cases"][0]["id"], "CASE-001")

    def test_live_scope_does_not_follow_selected_stage(self):
        w = self.world(live=True)
        w.start_stage(w.find_service("SEARCH"), "sentry")
        w.spawn_mouse(dict(ISSUE, live=True))
        self.assertEqual(w.mice[-1].issue["knowledge"]["service"], "WEB")
        w.spawn_mouse(dict(ISSUE, id="WEB-UNKNOWN", project="unknown", live=True))
        self.assertFalse(w.mice[-1].issue["knowledge"]["cases"])

    def test_disabled_kb_keeps_original_triage(self):
        w = new_world()
        with patch.dict(os.environ, {"XMAS_KB": "off"}):
            self.assertFalse(xm.attach_knowledge(w))
        w.spawn_mouse(dict(ISSUE))
        self.assertNotIn("knowledge", w.mice[-1].issue)

    def test_kb_command_and_small_screens(self):
        w = self.world()
        w.spawn_mouse(dict(ISSUE))
        w.command("kb WEB-KB1")
        text = xm.ANSI.sub("", "\n".join(xm.compose(w, 120, 40)))
        self.assertIn("CASE-001", text)
        self.assertIn("검색 참고 자료", text)
        for width, height in ((60, 22), (80, 24), (100, 30)):
            frame = xm.compose(w, width, height)
            self.assertEqual(len(frame), height)
            self.assertTrue(all(xm.dw(xm.ANSI.sub("", line)) <= width for line in frame))

    def test_replay_is_not_claimed_to_have_used_new_knowledge(self):
        w = self.world()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replay.jsonl"
            path.write_text(json.dumps({"kind": "triage", "key": ISSUE["title"], "result": {
                "kind": "fixable", "reason": "recorded", "fix": "fix"}, "at": "2026-11-28", "model": "recorded"}) + "\n")
            w.ai = ai.ReplayBrain(str(path))
            w.spawn_mouse(dict(ISSUE))
            w.pump_ai()
            self.assertEqual(w.mice[-1].issue["knowledge_usage"], "reference-only")

    def test_manual_verdict_is_preserved_with_kb(self):
        w = self.world()
        w.spawn_mouse(dict(ISSUE))
        issue = w.mice[-1].issue
        issue.update(ai="manual", kind="noise")
        w._apply_ai("triage", w.stage_gen, issue, {"kind": "fixable", "reason": "x", "fix": "x"}, None)
        self.assertEqual(issue["kind"], "noise")


class FixKnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.src = "const rate = cart.coupon.rate;\n"
        Path(self.repo, "app.js").write_text(self.src)
        self.issue = dict(ISSUE, live=True, knowledge=knowledge.KnowledgeBase().retrieve(ISSUE, "merrybugmas/demo-web", "WEB"))
        self.inputs, self.review_inputs, self.published = [], [], []

    def make_fixer(self, verdict="approve"):
        def ask(prompt, user, *args):
            self.inputs.append(user)
            return {"file": "app.js", "find": self.src, "replace": "const rate = cart.coupon ? cart.coupon.rate : 0;\n",
                    "title": "fix: coupon", "explanation": "proposed cause"}
        def review(issue, *args):
            self.review_inputs.append(copy.deepcopy(issue))
            return {"verdict": verdict, "reason": "test reason"}
        def publish(issue, f, new, title, body):
            self.published.append(body)
            return {"pr": 1, "url": "https://example.invalid/pull/1", "title": title}
        return fixer.Fixer(ask, repo=self.repo, files=["app.js"], runtime_dir=self.repo,
                           publish=publish, review=review)

    def test_same_snapshot_reaches_fix_review_pr_and_candidate(self):
        res = self.make_fixer().fix(self.issue)
        self.assertIn("CASE-001", self.inputs[0])
        self.assertEqual(self.review_inputs[0]["knowledge"], self.issue["knowledge"])
        self.assertIn("CASE-001", self.published[0])
        self.assertIn("- [ ]", self.published[0])
        c = json.loads(Path(res["knowledge_candidate"]).read_text())
        self.assertEqual(c["status"], "candidate")
        self.assertFalse(c["verification"]["resolved"])
        self.assertIn("diff", c["attempt"])
        self.assertIn("app.js", c["attempt"]["source_hashes"])
        self.assertEqual(Path(self.repo, "app.js").read_text(), self.src)

    def test_rejected_patch_is_saved_without_pr(self):
        with self.assertRaises(fixer.ReviewRejected):
            self.make_fixer("reject").fix(self.issue)
        c = json.loads(next(Path(self.repo, "knowledge_candidates").glob("*.json")).read_text())
        self.assertEqual(c["attempt"]["review"]["verdict"], "reject")
        self.assertEqual(c["status"], "candidate")
        self.assertEqual(self.published, [])

    def test_business_rules_still_block_with_knowledge(self):
        f = self.make_fixer()
        f.rules = rules.parse("| id | 대상 | 규칙 | 조치 | 담당 |\n|---|---|---|---|---|\n| PAY | app.js | 결제 확인 | 문의 | 정산팀 |")
        with self.assertRaises(fixer.AskTeam):
            f.fix(self.issue)
        self.assertEqual(self.published, [])

    def test_candidate_write_failure_does_not_lose_open_pr(self):
        with patch("knowledge.save_candidate", side_effect=OSError("disk full")):
            res = self.make_fixer().fix(self.issue)
        self.assertEqual(res["pr"], 1)

    def test_actual_codex_prompt_includes_knowledge(self):
        def run(argv, **kwargs):
            self.assertIn("CASE-001", argv[-1])
            Path(argv[argv.index("-o") + 1]).write_text('{"verdict":"approve","reason":"ok"}')
            return subprocess.CompletedProcess(argv, 0, "", "")
        fixer.codex_review(self.issue, "app.js", "diff", self.repo, run=run)


class EventContextTest(unittest.TestCase):
    EVENT = {"eventID": "event-1", "release": {"version": "r1"}, "user": {"email": "private@example.com"},
             "tags": [{"key": "environment", "value": "test"}], "entries": [{"type": "exception", "data": {
                 "values": [{"type": "TypeError", "value": "private", "stacktrace": {"frames": [
                     {"filename": "https://user:secret@example.com/app.js?token=secret", "function": "cartTotal",
                      "lineNo": 5, "inApp": True, "vars": {"secret": "secret"}}]}}]}}]}

    def test_allowlist_drops_private_fields(self):
        ctx = sentry_live.event_context(self.EVENT)
        encoded = json.dumps(ctx)
        for word in ("private", "secret", "vars", "cookie"):
            self.assertNotIn(word, encoded)
        self.assertEqual(ctx["frames"][0]["filename"], "/app.js")
        self.assertEqual(ctx["release"], "r1")

    def test_detail_is_fetched_once_and_uses_numeric_sentry_id(self):
        urls = []
        def transport(url):
            urls.append(url)
            return self.EVENT if "/events/latest/" in url else [dict(ISSUE, shortId="WEB-KB1", id="42")]
        feed = sentry_live.SentryFeed(transport=transport, with_context=True)
        feed.org, feed.project = "example", "web"
        feed.fetch(); feed.fetch()
        self.assertEqual(sum("/issues/42/events/latest/" in u for u in urls), 1)
        row = feed.poll()[0]
        self.assertEqual(row["sentry_id"], "42")
        self.assertEqual(row["event_context"]["event_id"], "event-1")

    def test_event_failure_keeps_issue(self):
        def transport(url):
            if "/events/" in url:
                raise OSError("not available")
            return [dict(ISSUE, shortId="WEB-KB1", id="42")]
        feed = sentry_live.SentryFeed(transport=transport, with_context=True)
        self.assertEqual(feed.fetch(), 1)
        self.assertEqual(feed.poll()[0]["event_context_error"], "OSError")


if __name__ == "__main__":
    unittest.main()
