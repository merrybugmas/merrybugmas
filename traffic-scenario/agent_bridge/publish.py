# 채택된 수정을 새 브랜치에 커밋해 PR로 올리고 병합은 사람에게 맡기는 명령
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import guard

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
FILES = sorted("traffic-scenario/" + name for name in guard.ALLOWED)


# 저장소에서 명령을 실행하고 실패하면 사유와 함께 중단
def run(argv, cwd, timeout=90):
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    if done.returncode:
        raise SystemExit(json.dumps({"error": "PUBLISH_FAILED", "command": " ".join(argv[:3]), "message": (done.stderr or done.stdout).strip()[-300:]}, ensure_ascii=False))

    return done.stdout


# 올릴 PR의 브랜치와 제목과 본문과 변경 내용 준비
def prepare(data_dir, sh=run):
    path = Path(data_dir) / "bridge-decision.json"

    if not path.exists():
        raise SystemExit(json.dumps({"error": "NO_DECISION", "message": "compare 결과가 없습니다."}, ensure_ascii=False))

    decision = json.loads(path.read_text())

    # 독립 검증이 채택한 수정만 올림
    if decision["decision"] != "accept":
        raise SystemExit(json.dumps({"error": "NOT_ACCEPTED", "decision": decision["decision"], "reasons": decision["reasons"]}, ensure_ascii=False))

    scope = guard.scope(data_dir)

    if not scope["ok"]:
        raise SystemExit(json.dumps({"error": "OUTSIDE_ALLOWED_SCOPE", "scope": scope}, ensure_ascii=False))

    diff = sh(["git", "diff", "HEAD", "--", *FILES], REPO)

    # DB에만 한 변경은 PR에 담을 수 없음
    if not diff.strip():
        raise SystemExit(json.dumps({"error": "NO_CODE_CHANGE", "message": "코드 변경이 없습니다. DB 구조 변경은 코드에 넣어야 PR에 담깁니다."}, ensure_ascii=False))

    run_id, rate = decision["before"], decision["customer_success_rate"]
    base = os.getenv("XMAS_FIX_BASE", "").strip() or sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], REPO).strip()
    branch = "perf-bot/%s-%s" % (re.sub(r"[^A-Za-z0-9-]", "-", run_id).lower(), time.strftime("%H%M%S"))
    title = f"fix: 검색 성능 개선 (기한 내 정답 {rate['before']:.1%} → {rate['after']:.1%})"
    body = "\n".join([
        "성능 개선 에이전트가 만든 수정이다. 병합은 사람이 검토한 뒤에 한다.", "",
        "| 항목 | 값 |", "|---|---|",
        f"| 기한 내 정답 비율 | {rate['before']:.1%} → {rate['after']:.1%} |",
        f"| 고객 목표 | {decision['goal_success_rate']:.0%} |",
        f"| 판정 | {decision['decision']} |",
        f"| 기준 실험 | {' '.join(decision['runs']['before'])} |",
        f"| 후보 실험 | {' '.join(decision['runs']['after'])} |", "",
        "같은 요청 목록을 수정 전후에 각각 보내 독립 검증기가 결과의 내용과 순서와 도착 시간을 확인했다."])

    return {"run_id": run_id, "base": base, "branch": branch, "title": title, "body": body, "diff": diff,
            "files": [f for f in FILES if f in diff]}


# 사용 중인 작업 폴더를 건드리지 않도록 별도 worktree의 새 브랜치에 커밋해 PR 생성
def publish(data_dir, confirm=False, sh=run):
    plan = prepare(data_dir, sh)
    record = ROOT / ".runtime" / "perf-prs.json"
    opened = json.loads(record.read_text()) if record.exists() else {}

    # 같은 실험의 수정은 PR을 한 번만 엶
    if plan["run_id"] in opened:
        return {"published": False, "already": opened[plan["run_id"]]}

    if not confirm:
        return {"published": False, "dry_run": True, **plan}

    work = tempfile.mkdtemp(prefix="perf-pr-")

    try:
        sh(["git", "fetch", "-q", "origin", plan["base"]], REPO)
        sh(["git", "worktree", "add", "-q", "-b", plan["branch"], work, "origin/" + plan["base"]], REPO)
        patch = Path(work) / ".perf.patch"
        patch.write_text(plan["diff"])
        sh(["git", "apply", str(patch)], work)
        patch.unlink()
        message = Path(work) / ".perf-message"
        message.write_text(plan["title"] + "\n\n" + plan["body"] + "\n")
        sh(["git", "add", "--", *plan["files"]], work)
        sh(["git", "commit", "-q", "-F", str(message)], work)
        message.unlink()
        sh(["git", "push", "-q", "-u", "origin", plan["branch"]], work)
        url = sh(["gh", "pr", "create", "--base", plan["base"], "--head", plan["branch"], "--title", plan["title"], "--body", plan["body"]], work).strip()
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", work], cwd=REPO, capture_output=True)

    opened[plan["run_id"]] = {"url": url, "branch": plan["branch"], "title": plan["title"]}
    record.parent.mkdir(exist_ok=True)
    record.write_text(json.dumps(opened, ensure_ascii=False, indent=2))

    return {"published": True, "url": url, "branch": plan["branch"], "base": plan["base"], "title": plan["title"], "files": plan["files"]}
