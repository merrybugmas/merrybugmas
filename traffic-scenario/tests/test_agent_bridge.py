import json
import random
import sqlite3
import subprocess

import pytest

from agent_bridge import operations as cli
from agent_bridge import guard, publish, scenario, verify
from backend import catalog


def write_run(settings, rows, summary):
    folder = settings.runs_dir / "run-001"
    folder.mkdir()
    (folder / "client-raw.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    (folder / "loadgen-summary.json").write_text(json.dumps(summary))
    (folder / "config.json").write_text(json.dumps({"client_timeout_ms": 5000}))


def rows(settings, count=4, items=None):
    query = {"category": "cookie", "max_price": 20000}
    answer = catalog.search(settings.catalog, **query) if items is None else items
    return [{"request_id": f"r{i}", "query": query, "scheduled_offset_ms": i * 10.0, "sent_offset_ms": i * 10.0 + 1,
             "elapsed_ms": 5.0, "outcome": "success", "http_status": 200, "items": answer} for i in range(count)]


def summary(planned=4, sent=4, dropped=0):
    return {"planned": planned, "sent": sent, "dropped": dropped, "elapsed_ms": 50, "termination": "completed"}


def test_healthy_run(seeded):
    write_run(seeded, rows(seeded), summary())
    out = verify.report(seeded.data_dir, "run-001")
    assert out["judgement"] == "healthy" and out["customer_success_rate"] == 1.0


def test_fast_but_wrong_results_are_not_counted(seeded):
    write_run(seeded, rows(seeded, items=[]), summary())
    out = verify.report(seeded.data_dir, "run-001")
    assert out["judgement"] == "wrong_results" and out["correct_in_time"] == 0


def test_undelivered_load_is_invalid_not_healthy(seeded):
    write_run(seeded, rows(seeded, count=2), summary(planned=4, sent=2, dropped=2))
    assert verify.report(seeded.data_dir, "run-001")["judgement"] == "measurement_invalid"


def test_failures_lower_customer_success_rate(seeded):
    data = rows(seeded)
    data[0].update(outcome="http_error", http_status=429, items=None)
    write_run(seeded, data, summary())
    out = verify.report(seeded.data_dir, "run-001")
    assert out["judgement"] == "degraded" and out["customer_success_rate"] == 0.75


def test_random_conditions_stay_inside_limits():
    for seed in range(200):
        body = cli.randomized(seed)
        low, high = verify.LIMITS["request_count"]
        rate = body["request_count"] / body["arrival_duration_s"]
        assert low <= body["request_count"] <= high
        assert verify.LIMITS["rate_per_s"][0] - 1 <= rate <= verify.LIMITS["rate_per_s"][1] + 1

    assert cli.randomized(7) == cli.randomized(7)
    assert len({json.dumps(cli.randomized(random.Random(i).randrange(10**6))) for i in range(20)}) > 1


def test_fault_files_apply_to_committed_code():
    for patch in scenario.FAULTS.glob("*.patch"):
        result = subprocess.run(["git", "apply", "--check", str(patch)], cwd=scenario.ROOT, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_sealed_truth_survives_later_db_change(seeded):
    write_run(seeded, rows(seeded), summary())
    assert verify.report(seeded.data_dir, "run-001", seal=True)["truth"] == "sealed"
    con = sqlite3.connect(seeded.catalog)
    con.execute("DELETE FROM products")
    con.commit()
    con.close()
    assert verify.report(seeded.data_dir, "run-001")["judgement"] == "healthy"


BEFORE, AFTER = ["b1", "b2", "b3"], ["a1", "a2", "a3"]


def fake_compare(monkeypatch, tmp_path, reports, verdict="improved", cohort=None, error=None, later=()):
    folder = tmp_path / "runs" / "a1"
    folder.mkdir(parents=True)
    (folder / "comparison-b1-demo-v1.json").write_text(json.dumps({"before_cohort": BEFORE, "after_cohort": cohort or AFTER}))
    ledger = [{"run_id": r, "source": None if r == "b1" else "b1"} for r in BEFORE] + [{"restart": True}] + [{"run_id": r, "source": "b1"} for r in AFTER] + list(later)
    (tmp_path / "bridge-runs.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ledger))
    monkeypatch.setattr(cli, "DATA", tmp_path)
    monkeypatch.setattr(cli.guard, "scope", lambda data: {"ok": True})
    monkeypatch.setattr(cli.verify, "report", lambda data, run_id: {"run_id": run_id, **reports[run_id]})
    calls = []

    def call(*args):
        calls.append(args)

        if error:
            raise SystemExit(error)

        return {"verdict": verdict, "reasons": [], "correctness_passed": True, "metrics": []}

    monkeypatch.setattr(cli, "call", call)

    return cli.compare("b1", "a1"), calls


def state(rate, judgement, valid=True, successes=1):
    return {"valid": valid, "wrong_results": 0, "customer_success_rate": rate, "judgement": judgement, "successes": successes}


def healthy_case():
    return {**{r: state(0.7, "degraded") for r in BEFORE}, **{r: state(1.0, "healthy") for r in AFTER}}


def test_improved_but_below_goal_is_not_accepted(monkeypatch, tmp_path):
    reports = healthy_case()
    assert fake_compare(monkeypatch, tmp_path, reports)[0]["decision"] == "accept"
    reports["a2"] = state(0.7, "degraded")
    assert fake_compare(monkeypatch, tmp_path / "low", reports)[0]["decision"] == "hold"


def test_invalid_repeat_skips_server_comparison(monkeypatch, tmp_path):
    reports = healthy_case()
    reports["a3"] = state(1.0, "measurement_invalid", valid=False)
    out, calls = fake_compare(monkeypatch, tmp_path, reports)
    assert out["decision"] == "measurement_invalid" and not calls


def test_runs_from_an_earlier_attempt_do_not_count(monkeypatch, tmp_path):
    out, _ = fake_compare(monkeypatch, tmp_path, healthy_case(), cohort=["a1", "old1", "old2"])
    assert out["decision"] == "measurement_invalid"


def test_server_side_missing_telemetry_is_invalid(monkeypatch, tmp_path):
    out, _ = fake_compare(monkeypatch, tmp_path, healthy_case(), error='{"body": {"error": {"code": "COMPARISON_NOT_READY"}}}')
    assert out["decision"] == "measurement_invalid"

    with pytest.raises(SystemExit):
        fake_compare(monkeypatch, tmp_path / "policy", healthy_case(), error='{"body": {"error": {"code": "POLICY_NOT_READY"}}}')


def test_recovery_from_zero_success_baseline_is_not_rejected(monkeypatch, tmp_path):
    reports = healthy_case()
    reports["b2"] = state(0.0, "degraded", successes=0)
    assert fake_compare(monkeypatch, tmp_path, reports, verdict="regressed")[0]["decision"] == "hold"


def test_correct_answer_after_deadline_is_not_success(seeded):
    data = rows(seeded, count=100)
    data[0]["elapsed_ms"] = data[1]["elapsed_ms"] = 5005.0
    write_run(seeded, data, summary(planned=100, sent=100))
    out = verify.report(seeded.data_dir, "run-001")
    assert out["correct_in_time"] == 98 and out["correct_but_late"] == 2 and out["judgement"] == "degraded"


def test_replay_without_original_truth_is_invalid(seeded):
    write_run(seeded, rows(seeded), summary())
    (seeded.runs_dir / "old").mkdir()
    out = verify.report(seeded.data_dir, "run-001", seal=True, source="old")
    assert out["judgement"] == "measurement_invalid" and not (seeded.runs_dir / "run-001" / "bridge-truth.json").exists()


def test_missing_or_broken_records_are_invalid(seeded):
    (seeded.runs_dir / "run-001").mkdir()
    assert verify.report(seeded.data_dir, "run-001")["judgement"] == "measurement_invalid"
    (seeded.runs_dir / "run-001").rmdir()
    write_run(seeded, rows(seeded), summary())
    raw = seeded.runs_dir / "run-001" / "client-raw.jsonl"
    raw.write_text(raw.read_text()[:-20])
    assert verify.report(seeded.data_dir, "run-001")["judgement"] == "measurement_invalid"
    write_run_again = rows(seeded)
    raw.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in write_run_again))
    (seeded.runs_dir / "run-001" / "config.json").unlink()
    assert verify.report(seeded.data_dir, "run-001")["judgement"] == "measurement_invalid"


def test_orphan_replay_stays_invalid_when_read_again(seeded):
    write_run(seeded, rows(seeded), summary())
    (seeded.runs_dir / "old").mkdir()
    verify.report(seeded.data_dir, "run-001", seal=True, source="old")
    assert verify.report(seeded.data_dir, "run-001")["judgement"] == "measurement_invalid"


def test_generator_lag_is_not_blamed_on_server(seeded):
    data = rows(seeded, count=600)

    for r in data[:12]:
        r["sent_offset_ms"] += 5100

    write_run(seeded, data, summary(planned=600, sent=600))
    out = verify.report(seeded.data_dir, "run-001")
    assert out["judgement"] == "measurement_invalid" and out["sent_after_deadline"] == 12


def test_accept_of_an_older_candidate_is_not_reused(monkeypatch, tmp_path):
    out, _ = fake_compare(monkeypatch, tmp_path, healthy_case(), later=[{"restart": True}])
    assert out["decision"] == "measurement_invalid"


def test_rejected_restart_does_not_split_group(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "DATA", tmp_path)
    monkeypatch.setattr(cli.guard, "scope", lambda data: {"ok": True})

    def call(method, path, body=None):
        if method == "POST":
            raise SystemExit("409")

        return {"last_seq": 0}

    monkeypatch.setattr(cli, "call", call)

    with pytest.raises(SystemExit):
        cli.restart("r", "x")

    assert cli.ledger()[1] == 0


def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "catalog.py").write_text("a = 1\n")
    (root / "loadgen.py").write_text("x = 1\n")

    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=root, check=True)

    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "catalog.sqlite").execute("CREATE TABLE products(product_id)").connection.commit()
    monkeypatch.setattr(guard, "ROOT", root)
    monkeypatch.setattr(scenario, "ROOT", root)

    return root, data


def test_scope_sees_edits_to_files_dirty_before_snapshot(tmp_path, monkeypatch):
    root, data = repo(tmp_path, monkeypatch)
    (root / "loadgen.py").write_text("x = 2\n")
    guard.snapshot(data)
    assert guard.scope(data)["ok"]
    (root / "backend" / "catalog.py").write_text("a = 2\n")
    assert guard.scope(data)["ok"]
    (root / "loadgen.py").write_text("x = 3\n")
    subprocess.run(["git", "add", "loadgen.py"], cwd=root, check=True)
    assert guard.scope(data)["outside_allowed"] == ["loadgen.py"]


def test_rollback_restores_uncommitted_baseline(tmp_path, monkeypatch):
    root, data = repo(tmp_path, monkeypatch)
    (root / "backend" / "catalog.py").write_text("a = 'mine'\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    (root / "backend" / "notes.py").write_text("keep\n")
    guard.snapshot(data)
    (root / "backend" / "catalog.py").write_text("a = 'agent'\n")
    (root / "backend" / "extra.py").write_text("new\n")
    sqlite3.connect(data / "catalog.sqlite").execute("CREATE INDEX i ON products(product_id)").connection.commit()
    out = guard.rollback(data)
    assert (root / "backend" / "catalog.py").read_text() == "a = 'mine'\n" and (root / "backend" / "notes.py").exists()
    assert out["removed_files"] == ["backend/extra.py"] and out["dropped_objects"] == ["index:i"] and out["restored"]


def test_scenario_stops_before_db_when_backend_has_leftovers(tmp_path, monkeypatch):
    root, data = repo(tmp_path, monkeypatch)
    (root / "backend" / "catalog.py").write_text("a = 'fixed by agent'\n")
    sqlite3.connect(data / "catalog.sqlite").execute("CREATE TABLE stock(product_id)").connection.commit()

    for name in ("b", "c"):
        with pytest.raises(SystemExit):
            scenario.prepare(name, data / "catalog.sqlite", 1)

    subprocess.run(["git", "checkout", "--", "backend"], cwd=root, check=True)
    (root / "backend" / "untracked.py").write_text("x\n")

    with pytest.raises(SystemExit):
        scenario.prepare("c", data / "catalog.sqlite", 1)

    assert "table:stock" in guard.objects(data / "catalog.sqlite")
    subprocess.run(["git", "add", "backend/untracked.py"], cwd=root, check=True)

    with pytest.raises(SystemExit):
        scenario.prepare("c", data / "catalog.sqlite", 1)

    subprocess.run(["git", "rm", "-qf", "backend/untracked.py"], cwd=root, check=True)
    scenario.prepare("a", data / "catalog.sqlite", 1)
    assert "table:stock" not in guard.objects(data / "catalog.sqlite")


def test_failed_snapshot_keeps_previous_copy(tmp_path, monkeypatch):
    root, data = repo(tmp_path, monkeypatch)
    guard.snapshot(data)
    (root / "backend" / "catalog.py").write_text("a = 'agent'\n")
    monkeypatch.setattr(guard.shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError):
        guard.snapshot(data)

    monkeypatch.undo()
    monkeypatch.setattr(guard, "ROOT", root)
    guard.rollback(data)
    assert (root / "backend" / "catalog.py").read_text() == "a = 1\n"
    monkeypatch.setattr(guard.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("io")))

    with pytest.raises(OSError):
        guard.snapshot(data)

    monkeypatch.undo()
    monkeypatch.setattr(guard, "ROOT", root)
    (root / "backend" / "catalog.py").write_text("a = 'agent again'\n")
    guard.rollback(data)
    assert (root / "backend" / "catalog.py").read_text() == "a = 1\n"
    (guard.current(data) / "backend" / "catalog.py").write_text("broken\n")
    (root / "backend" / "extra.py").write_text("new\n")

    with pytest.raises(SystemExit):
        guard.rollback(data)

    assert (root / "backend" / "extra.py").exists()


def test_incomplete_server_telemetry_stays_invalid_on_verify(seeded):
    write_run(seeded, rows(seeded), summary())
    assert verify.report(seeded.data_dir, "run-001")["judgement"] == "healthy"
    (seeded.runs_dir / "run-001" / "bridge-incomplete").write_text("")
    assert verify.report(seeded.data_dir, "run-001")["judgement"] == "measurement_invalid"


def test_adopt_takes_over_latest_screen_run(seeded, monkeypatch):
    write_run(seeded, rows(seeded), summary())
    monkeypatch.setattr(cli, "DATA", seeded.data_dir)
    monkeypatch.setattr(cli, "wait", lambda run_id: {"run_id": run_id, "status": "completed", "config": {}, "metrics": {}, "source_run_id": None})
    out = cli.adopt(None)
    assert out["run_id"] == "run-001" and out["truth"] == "sealed" and out["judgement"] == "healthy"
    cli.adopt(None)
    assert list(cli.ledger()[0]) == ["run-001"] and len((seeded.data_dir / "bridge-runs.jsonl").read_text().splitlines()) == 1


def pr_setup(tmp_path, monkeypatch, decision="accept", diff="diff --git a/traffic-scenario/backend/catalog.py b/traffic-scenario/backend/catalog.py\n+x\n"):
    (tmp_path / "bridge-decision.json").write_text(json.dumps({"decision": decision, "reasons": ["r"], "before": "run-B1", "after": "run-a1",
        "customer_success_rate": {"before": 0.4743, "after": 1.0}, "goal_success_rate": 0.99, "runs": {"before": ["run-B1"], "after": ["run-a1"]}}))
    monkeypatch.setattr(publish.guard, "scope", lambda data: {"ok": True})
    monkeypatch.setattr(publish, "ROOT", tmp_path)
    monkeypatch.delenv("XMAS_FIX_BASE", raising=False)
    calls = []

    def sh(argv, cwd, timeout=90):
        calls.append(argv)

        if argv[:2] == ["git", "diff"]:
            return diff

        if argv[:2] == ["git", "rev-parse"]:
            return "feat/traffic-scenario\n"

        return "https://example.test/pr/7\n" if argv[:3] == ["gh", "pr", "create"] else ""

    return calls, sh


def test_pr_is_only_prepared_for_accepted_fix_with_code_change(tmp_path, monkeypatch):
    calls, sh = pr_setup(tmp_path, monkeypatch)
    out = publish.publish(tmp_path, sh=sh)
    assert out["dry_run"] and not out["published"] and out["base"] == "feat/traffic-scenario"
    assert out["branch"].startswith("perf-bot/run-b1-") and "47.4% → 100.0%" in out["title"]
    assert not any(c[:2] in (["git", "push"], ["gh", "pr"]) for c in calls)

    for kwargs in ({"decision": "hold"}, {"diff": ""}):
        _, sh = pr_setup(tmp_path, monkeypatch, **kwargs)

        with pytest.raises(SystemExit):
            publish.publish(tmp_path, confirm=True, sh=sh)


def test_pr_is_opened_once_on_new_branch_without_merging(tmp_path, monkeypatch):
    calls, sh = pr_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(publish.subprocess, "run", lambda *a, **k: None)
    out = publish.publish(tmp_path, confirm=True, sh=sh)
    order = [" ".join(c[:3]) for c in calls]
    assert out["published"] and out["url"] == "https://example.test/pr/7"
    assert order.index("git worktree add") < order.index("git push -q") < order.index("gh pr create")
    assert not any("merge" in c for c in calls) and ["git", "push", "-q", "-u", "origin", out["branch"]] in calls
    again = publish.publish(tmp_path, confirm=True, sh=sh)
    assert not again["published"] and again["already"]["url"] == out["url"]
    assert "--draft" not in [a for c in calls for a in c]
    (tmp_path / ".runtime" / "perf-prs.json").unlink()
    assert publish.publish(tmp_path, confirm=True, draft=True, sh=sh)["draft"]
    assert "--draft" in calls[-1]
