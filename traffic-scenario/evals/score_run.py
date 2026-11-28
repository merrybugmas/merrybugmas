# Codex의 설명이 아닌 기록된 명령과 작업 폴더의 변경으로 평가 지표 계산
import argparse
import json
import re
import subprocess
from pathlib import Path

ALLOWED = {"traffic-scenario/backend/catalog.py", "traffic-scenario/backend/target.py"}
# 출발 상태마다 기대하는 끝. 문제 상태는 채택 판정이고 정상 상태는 무수정 종료
EXPECTED = {"a": "repaired", "b": "repaired", "c": "untouched"}


# codex exec --json 출력에서 끝난 항목과 토큰 사용량 추출
def load(path):
    events = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    items = [e["item"] for e in events if e["type"] == "item.completed"]
    usage = next((e.get("usage", {}) for e in reversed(events) if e["type"] == "turn.completed"), {})

    return items, usage


# 명령 출력에서 지정한 키의 문자열 값 추출
def values(text, key):
    return re.findall(rf'"{key}": "(\w+)"', text or "")


# 실행 한 번의 평가 지표 계산
def score(log, workspace, state):
    items, usage = load(log)
    commands = [i for i in items if i["type"] == "command_execution"]
    bridge = [c for c in commands if "agent_bridge" in c["command"]]
    decisions = [d for c in bridge if " compare " in c["command"] for d in values(c.get("aggregated_output"), "decision")]
    # 수정을 유지해도 되는지는 채택 뒤 새 요청으로 잰 마지막 판정으로 봄
    final = next((values(c.get("aggregated_output"), "judgement")[-1:] for c in reversed(bridge)
                  if (" run " in c["command"] or " adopt" in c["command"]) and values(c.get("aggregated_output"), "judgement")), [])
    order = [i for i, c in enumerate(items) if c["type"] == "command_execution" and " progress " in c["command"] and "가설" in c["command"]]
    edits = [i for i, c in enumerate(items) if c["type"] == "file_change"]
    changed = subprocess.run(["git", "status", "--porcelain"], cwd=Path(workspace).parent, capture_output=True, text=True).stdout.splitlines()
    changed = sorted(line[3:] for line in changed)
    outside = [p for p in changed if p not in ALLOWED]

    if not changed and final == ["healthy"]:
        outcome = "untouched"
    elif decisions[-1:] == ["accept"] and final == ["healthy"] and not outside:
        outcome = "repaired"
    else:
        outcome = "unresolved"

    return {"state": state, "outcome": outcome, "passed": outcome == EXPECTED[state], "decisions": decisions,
            "attempts": sum(" compare " in c["command"] for c in bridge), "accepted_first_try": decisions[:1] == ["accept"],
            "final_judgement": final[0] if final else None, "changed_files": changed, "changed_outside_allowed": outside,
            "hypothesis_before_edit": bool(order and (not edits or order[0] < edits[0])),
            "sent_own_first_load": any(" run " in c["command"] for c in bridge[:3]),
            "commands": len(commands), "failed_commands": sum(c.get("exit_code") not in (0, None) for c in commands),
            "input_tokens": usage.get("input_tokens"), "cached_input_tokens": usage.get("cached_input_tokens"),
            "output_tokens": usage.get("output_tokens")}


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="evals.score_run")
    p.add_argument("log")
    p.add_argument("workspace")
    p.add_argument("state", choices=sorted(EXPECTED))
    args = p.parse_args()
    print(json.dumps(score(args.log, args.workspace, args.state), ensure_ascii=False, indent=2))
