# 운영 서버는 그대로 두고 복사한 시험 서버에서 고친 뒤 채택된 수정만 PR로 준비
import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from agent_bridge import publish

from . import runner

ROOT = Path(__file__).resolve().parents[1]


# 운영 서버에서 가장 최근에 보낸 실험의 번호와 유입 조건 조회
def latest_run(data_dir):
    folders = [f for f in (Path(data_dir) / "runs").glob("*") if (f / "config.json").exists()] if (Path(data_dir) / "runs").is_dir() else []

    if not folders:
        raise SystemExit(json.dumps({"error": "NO_RUN", "message": "넘겨받을 실험이 없습니다. 화면에서 유입을 먼저 보내세요."}, ensure_ascii=False))

    folder = max(folders, key=lambda f: (f / "config.json").stat().st_mtime)

    return folder.name, json.loads((folder / "config.json").read_text())


# 시험 서버의 진행 상황을 운영 서버의 화면에 전달
def notifier(control, run_id):
    def notify(phase, message):
        body = {"run_id": run_id, "operation_id": "agent", "message_id": "msg-" + uuid.uuid4().hex[:16], "phase": phase, "message": message}
        request = urllib.request.Request(control + "/api/agent/progress", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})

        # 화면 알림이 실패해도 수정 작업은 계속함
        try:
            urllib.request.urlopen(request, timeout=3).close()
        except OSError:
            pass

    return notify


# 시험 환경에서 고치고 채택된 수정의 PR 준비
def repair(data_dir, control, confirm=False, draft=True, agent=True, model="gpt-6.2-sol", effort="medium", timeout=1800):
    run_id, config = latest_run(data_dir)
    notify = notifier(control, run_id)
    notify("analyzing", "운영 서버는 그대로 두고 시험 서버를 따로 준비합니다.")
    dest = Path(tempfile.mkdtemp(prefix="mbm-repair-")) / time.strftime("%H%M%S")
    result = runner.run_once("live", dest, Path(data_dir) / "catalog.sqlite", agent=agent, model=model, effort=effort, timeout=timeout,
                             load=config, strip=False, notify=notify)
    out = {"production_run": run_id, "test_environment": result["root"], "test_port": result["port"], "load": result.get("load"),
           "outcome": result.get("outcome"), "passed": result.get("passed"), "decisions": result.get("decisions")}

    if result.get("outcome") != "repaired" or not result.get("passed"):
        notify("finished" if result.get("outcome") == "untouched" else "failed",
               "시험 서버에서 정상으로 판정돼 수정하지 않았습니다." if result.get("outcome") == "untouched" else "채택된 수정이 없어 PR을 만들지 않았습니다.")

        return out

    # 시험 환경의 작업 폴더에서 바뀐 내용만 뽑아 PR로 올림
    diff = subprocess.run(["git", "diff", "HEAD", "--", *publish.FILES], cwd=Path(result["workspace"]).parent, capture_output=True, text=True, check=True).stdout
    out["pr"] = publish.publish(Path(result["judge"]) / "data", confirm, draft, diff=diff)
    notify("finished", "채택된 수정을 PR로 올렸습니다. 병합은 사람이 합니다." if out["pr"].get("published") else "채택된 수정의 PR 내용을 준비했습니다. 운영 서버는 바뀌지 않았습니다.")

    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="evals.repair")
    p.add_argument("--publish", action="store_true")
    p.add_argument("--ready", action="store_true")
    p.add_argument("--skip-agent", action="store_true")
    p.add_argument("--model", default=os.getenv("CODEX_MODEL", "gpt-6.2-sol"))
    p.add_argument("--effort", default=os.getenv("CODEX_EFFORT", "medium"))
    p.add_argument("--timeout", type=int, default=1800)
    args = p.parse_args()
    data = Path(os.getenv("MBM_DATA_DIR", ROOT / "data")).resolve()
    control = os.getenv("MBM_CONTROL_URL", "http://127.0.0.1:8000")
    print(json.dumps(repair(data, control, args.publish, not args.ready, not args.skip_agent, args.model, args.effort, args.timeout), ensure_ascii=False, indent=2))
