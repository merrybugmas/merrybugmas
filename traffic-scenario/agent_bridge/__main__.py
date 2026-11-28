# 사람과 Codex가 쓰는 명령의 인자 해석과 실행 연결
import argparse
import json
import os
import random
import subprocess
import uuid

from . import guard, operations as ops, publish, scenario, verify


# 명령 처리
def main():
    p = argparse.ArgumentParser(prog="agent_bridge")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--random", action="store_true")
    run.add_argument("--seed", type=int)
    run.add_argument("--count", type=int)
    run.add_argument("--duration", type=float)
    run.add_argument("--timeout-ms", type=int, default=ops.LIMITS["client_timeout_ms"])
    replay = sub.add_parser("replay")
    replay.add_argument("run_id")
    replay.add_argument("--repeat", type=int, default=1)

    sub.add_parser("adopt").add_argument("run_id", nargs="?")
    sub.add_parser("surge").add_argument("--seed", type=int)

    for name in ("status", "logs", "verify"):
        sub.add_parser(name).add_argument("run_id")

    again = sub.add_parser("restart")
    again.add_argument("run_id")
    again.add_argument("--reason", default="에이전트 수정 반영")
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("before")
    cmp_.add_argument("after")
    progress = sub.add_parser("progress")
    progress.add_argument("run_id")
    progress.add_argument("phase", choices=["idle", "analyzing", "modifying", "restarting", "testing", "comparing", "finished", "failed", "celebrating"])
    progress.add_argument("message")
    progress.add_argument("--operation", default="agent")

    for name in ("snapshot", "rollback", "scope"):
        sub.add_parser(name)

    pr = sub.add_parser("pr")
    pr.add_argument("--publish", action="store_true")
    pr.add_argument("--draft", action="store_true")
    sub.add_parser("scenario").add_argument("name", choices=sorted(scenario.STATES))
    args = p.parse_args()

    if args.command == "run":
        seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1_000_000)

        if args.random:
            body = ops.randomized(seed, verify=True)
        elif args.count and args.duration:
            body = {"scenario_id": "surge", "request_count": args.count, "arrival_duration_s": args.duration,
                    "seed": seed, "client_timeout_ms": args.timeout_ms}
        else:
            p.error("--random 또는 --count와 --duration이 필요합니다.")

        out = ops.execute(body)
    elif args.command == "replay":
        out = [ops.execute({"source_run_id": args.run_id}) for _ in range(args.repeat)]
    elif args.command == "surge":
        # 화면이 시작한 실험만 화면에 표시되므로 유입을 직접 보내지 않고 화면에 명령을 넘김
        body = ops.randomized(args.seed if args.seed is not None else random.SystemRandom().randrange(1_000_000))
        out = {"request_count": body["request_count"], "arrival_duration_s": body["arrival_duration_s"]}
        subprocess.run([str(ops.ROOT / "market"), "experiment", "surge", str(out["request_count"]), str(out["arrival_duration_s"])], check=True)
    elif args.command == "adopt":
        out = ops.adopt(args.run_id)
    elif args.command == "status":
        out = ops.call("GET", f"/api/runs/{args.run_id}")
    elif args.command == "logs":
        out = ops.call("GET", f"/api/runs/{args.run_id}/logs?after_seq=0&limit=200")
    elif args.command == "verify":
        out = verify.report(ops.DATA, args.run_id)
    elif args.command == "restart":
        out = ops.restart(args.run_id, args.reason)
    elif args.command == "compare":
        out = ops.compare(args.before, args.after)
    elif args.command == "progress":
        out = ops.call("POST", "/api/agent/progress", {"run_id": args.run_id, "operation_id": args.operation,
                   "message_id": "msg-" + uuid.uuid4().hex[:16], "phase": args.phase, "message": args.message})
    elif args.command == "snapshot":
        out = guard.snapshot(ops.DATA)
    elif args.command == "rollback":
        # 되돌린 뒤에는 재시작이 실패해도 지난 후보의 판정을 쓰지 못하게 묶음을 나눔
        ops.note({"restart": True, "by": "rollback"})
        out = guard.rollback(ops.DATA)
    elif args.command == "pr":
        out = publish.publish(ops.DATA, args.publish, args.draft)
    elif args.command == "scope":
        out = guard.scope(ops.DATA)
    else:
        out = scenario.prepare(args.name, ops.DATA / "catalog.sqlite", int(os.getenv("MBM_TARGET_PORT", "8001")))

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
