import ast
import json
import subprocess
import sys
from pathlib import Path

from agent_bridge import __main__ as entry
from agent_bridge import guard
from evals import judge, make_workspace, repair, runner, score_run

ROOT = Path(__file__).resolve().parents[1]


def imports(path):
    tree = ast.parse(path.read_text())
    found = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.update(f"{node.module or ''}.{a.name}".strip(".") for a in node.names)
            found.add(node.module or "")
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)

    return found


def test_verifier_and_guard_do_not_depend_on_cli_or_server_code():
    for name in ("verify.py", "guard.py"):
        used = imports(ROOT / "agent_bridge" / name)
        assert not {"operations", "scenario", "__main__"} & used, name
        assert not any(u.startswith(("backend", "frontend")) for u in used), name


def test_server_and_screen_do_not_depend_on_agent_code():
    for folder in ("backend", "frontend"):
        for path in (ROOT / folder).glob("*.py"):
            assert not any(u.startswith(("agent_bridge", "evals")) for u in imports(path)), path.name


def test_rollback_command_marks_boundary_before_restoring(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(entry.ops, "DATA", tmp_path)
    monkeypatch.setattr(guard, "rollback", lambda data: calls.append("rollback") or {"restored": True})
    monkeypatch.setattr(sys, "argv", ["agent_bridge", "rollback"])
    entry.main()
    assert calls == ["rollback"] and entry.ops.ledger()[1] == 1
    assert json.loads(capsys.readouterr().out) == {"restored": True}


def test_eval_workspace_separates_judge_from_agent(tmp_path):
    for state, rules in (("a", "study"), ("b", "baseline")):
        out = make_workspace.build(state, tmp_path / state, rules=rules)
        work, judge = Path(out["workspace"]), Path(out["judge"])
        text = "".join(p.read_text(errors="ignore") for p in work.rglob("*") if p.is_file() and "runs" not in p.parts)

        for leak in ("INDEX_SQL", "idx_products", "REFERENCE", "stock_recheck", "JOIN stock", "demo-v1"):
            assert leak not in text, (state, leak)

        # 작업 폴더에는 검증기와 정책과 결함 파일과 테스트가 없고 명령 전달 코드만 있음
        assert sorted(p.name for p in (work / "agent_bridge").iterdir()) == ["__main__.py"]
        assert not any((work / name).exists() for name in ("evals", "tests", "docs", "config"))
        assert (judge / "agent_bridge" / "verify.py").exists() and (judge / "data" / "runs").is_dir()
        # 심판 폴더의 서버는 작업 폴더의 두 파일을 실행하고 실험 기록은 심판 폴더에만 있음
        assert (judge / "backend" / "catalog.py").resolve() == work / "backend" / "catalog.py"
        assert (work / "data" / "runs").resolve() == judge / "data" / "runs"
        skill = (work / ".agents" / "skills" / "performance-repair" / "SKILL.md").read_text()
        assert ("틀렸다고 볼 조건" in skill) == (rules == "study") and ".venv" not in skill
        guide = (work / "AGENTS.md").read_text()
        assert "pytest" not in guide and "agent_bridge/README.md" not in guide and "주석 작성 규칙" in guide
        check = subprocess.run([sys.executable, "-B", "-c", "import backend.__main__, agent_bridge.operations"], cwd=judge, capture_output=True, text=True)
        assert check.returncode == 0, check.stderr

        for folder in (work.parent, judge.parent):
            assert len(subprocess.run(["git", "log", "--oneline"], cwd=folder, capture_output=True, text=True).stdout.splitlines()) == 1

    assert "in_stock" in (tmp_path / "b" / "work" / "traffic-scenario" / "backend" / "catalog.py").read_text()


def test_judge_only_runs_agent_commands(monkeypatch):
    ran = []
    monkeypatch.setattr(judge.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or type("Done", (), {"stdout": "{}", "stderr": "", "returncode": 0})())

    for argv in (["scenario", "c"], ["surge"], [], "adopt", ["adopt", 1]):
        assert judge.execute(argv)["code"] == 2

    assert not ran and judge.execute(["adopt"])["code"] == 0 and ran[0][-1] == "adopt"


def test_runner_denies_judge_and_protects_instructions(tmp_path):
    info = {"root": "/e", "judge": "/e/judge/traffic-scenario", "workspace": "/e/work/traffic-scenario"}
    rule = runner.permissions(info)
    assert '":root"="deny"' in rule and '"/e"="deny"' in rule and '"/e/work/traffic-scenario"="write"' in rule
    assert '"/e/judge/traffic-scenario/data/runs"="read"' in rule and '"/e/work/traffic-scenario/.agents"="read"' in rule


def event(item):
    return json.dumps({"type": "item.completed", "item": item})


def command(text, output="", code=0):
    return event({"type": "command_execution", "command": text, "aggregated_output": output, "exit_code": code})


def test_score_separates_repair_from_untouched_and_unresolved(tmp_path):
    work = tmp_path / "ws" / "traffic-scenario"
    (work / "backend").mkdir(parents=True)
    (work / "backend" / "catalog.py").write_text("a = 1\n")
    (work / "loadgen.py").write_text("x = 1\n")

    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=work.parent, check=True)

    healthy = command("python -m agent_bridge adopt", '"judgement": "healthy"')
    log = tmp_path / "log.jsonl"
    log.write_text(healthy + "\n" + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}) + "\n")
    out = score_run.score(log, work, "c")
    assert out["passed"] and out["outcome"] == "untouched" and out["input_tokens"] == 5
    assert not score_run.score(log, work, "a")["passed"]

    (work / "backend" / "catalog.py").write_text("a = 2\n")
    lines = [command("python -m agent_bridge adopt", '"judgement": "degraded"'),
             command("python -m agent_bridge progress r analyzing 가설"), event({"type": "file_change"}),
             command("python -m agent_bridge compare r1 r2", '"decision": "accept"'),
             command("python -m agent_bridge run --random", '"judgement": "healthy"')]
    log.write_text("\n".join(lines) + "\n")
    out = score_run.score(log, work, "a")
    assert out["passed"] and out["accepted_first_try"] and out["hypothesis_before_edit"]

    (work / "loadgen.py").write_text("x = 2\n")
    out = score_run.score(log, work, "a")
    assert not out["passed"] and out["changed_outside_allowed"] == ["traffic-scenario/loadgen.py"]


def test_verification_load_is_a_short_sample_at_the_same_rate():
    config = {"scenario_id": "surge", "request_count": 6000, "arrival_duration_s": 60.0, "seed": 9, "client_timeout_ms": 5000}
    short = repair.sample(config)
    assert short["request_count"] == 1000 and short["arrival_duration_s"] == 10.0 and short["seed"] == 9
    assert repair.sample({**config, "request_count": 500, "arrival_duration_s": 5.0})["request_count"] == 500


def test_judge_relays_agent_progress_to_production_screen(monkeypatch):
    sent = []
    monkeypatch.setattr(judge.subprocess, "run", lambda cmd, **kw: type("Done", (), {"stdout": "{}", "stderr": "", "returncode": 0})())
    monkeypatch.setenv("MBM_RELAY_CONTROL", "http://127.0.0.1:1")
    monkeypatch.setenv("MBM_RELAY_RUN", "run-prod")
    monkeypatch.setattr(judge.urllib.request, "urlopen", lambda request, timeout: sent.append(json.loads(request.data)) or type("R", (), {"close": lambda self: None})())
    judge.execute(["progress", "run-test", "analyzing", "가설"])
    judge.execute(["scope"])
    assert len(sent) == 1 and sent[0]["run_id"] == "run-prod" and sent[0]["phase"] == "analyzing" and sent[0]["message"] == "가설"
