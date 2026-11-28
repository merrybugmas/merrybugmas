# 제어 서버 호출과 실험 묶음 기록과 전후 비교 판정
import json
import os
import random
import time
from pathlib import Path

import httpx

from . import guard, verify

ROOT = Path(__file__).resolve().parents[1]
CONTROL = os.getenv("MBM_CONTROL_URL", "http://127.0.0.1:8000")
DATA = Path(os.getenv("MBM_DATA_DIR", ROOT / "data")).resolve()
LIMITS = verify.LIMITS
DONE = {"completed", "failed", "stopped"}


# 제어 서버 API 호출
def call(method, path, body=None):
    try:
        response = httpx.request(method, CONTROL + path, json=body, timeout=60, trust_env=False)
    except httpx.HTTPError as exc:
        raise SystemExit(json.dumps({"error": "CONTROL_UNREACHABLE", "message": str(exc)}, ensure_ascii=False))

    if response.status_code >= 400:
        raise SystemExit(json.dumps({"error": response.status_code, "body": response.json()}, ensure_ascii=False))

    return response.json()


# 실험이 끝나고 기록 수집이 끝날 때까지 대기
def wait(run_id):
    ended = 0

    for _ in range(6000):
        snapshot = call("GET", f"/api/runs/{run_id}")
        ended += snapshot["status"] in DONE

        # 실패하거나 멈춘 실험은 수집이 끝나지 않을 수 있어 30초만 더 기다림
        if snapshot["status"] in DONE and (snapshot["telemetry_complete"] or ended > 300):
            return snapshot

        time.sleep(0.1)

    raise SystemExit(json.dumps({"error": "RUN_WAIT_TIMEOUT", "run_id": run_id}))


# 실험 시작 후 끝까지 기다려 검증 결과 반환
def execute(body):
    for _ in range(100):
        response = httpx.post(CONTROL + "/api/runs", json=body, timeout=60, trust_env=False)

        # 직전 실험의 서버 요청 회수가 끝나기 전에는 시작이 거절됨
        if response.status_code != 409 or response.json()["error"]["code"] != "RUN_ALREADY_ACTIVE":
            break

        time.sleep(0.5)

    if response.status_code >= 400:
        raise SystemExit(json.dumps({"error": response.status_code, "body": response.json()}, ensure_ascii=False))

    snapshot = wait(response.json()["run_id"])
    note({"run_id": snapshot["run_id"], "source": body.get("source_run_id")})

    # 나중에 다시 채점해도 같은 답이 나오도록 수집 미완료를 실험 폴더에 남김
    if not snapshot["telemetry_complete"]:
        (DATA / "runs" / snapshot["run_id"]).mkdir(parents=True, exist_ok=True)
        (DATA / "runs" / snapshot["run_id"] / "bridge-incomplete").write_text("")

    out = verify.report(DATA, snapshot["run_id"], seal=True, source=body.get("source_run_id"))

    return {"status": snapshot["status"], "config": snapshot["config"], "server_metrics": snapshot["metrics"], **out}


# 이번 시도에서 실행한 실험과 재시작을 순서대로 기록
def note(entry):
    with (DATA / "bridge-runs.jsonl").open("a") as file:
        file.write(json.dumps(entry) + "\n")


# 이번 시도의 실험마다 재시작 횟수와 처음 실험을 정리
def ledger():
    path = DATA / "bridge-runs.jsonl"
    entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    epoch, found = 0, {}

    # 재시작마다 코드가 달라질 수 있으므로 재시작 사이의 실험만 한 묶음
    for e in entries:
        if "restart" in e:
            epoch += 1
        else:
            found[e["run_id"]] = (epoch, e["source"] or e["run_id"])

    return found, epoch


# 화면에서 보낸 실험을 넘겨받아 이번 시도의 기준 실험으로 등록
def adopt(run_id):
    folders = [f for f in (DATA / "runs").glob("*") if (f / "config.json").exists()] if (DATA / "runs").is_dir() else []

    if not run_id and not folders:
        raise SystemExit(json.dumps({"error": "NO_RUN", "message": "넘겨받을 실험이 없습니다. 화면에서 유입을 먼저 보내세요."}, ensure_ascii=False))

    # 화면이 실험 번호를 알려 주지 않으므로 가장 최근에 시작한 실험을 방금 보낸 유입으로 봄
    run_id = run_id or max(folders, key=lambda f: (f / "config.json").stat().st_mtime).name
    snapshot = wait(run_id)

    if run_id not in ledger()[0]:
        note({"run_id": run_id, "source": snapshot.get("source_run_id")})

    out = verify.report(DATA, run_id, seal=True, source=snapshot.get("source_run_id"))

    return {"status": snapshot["status"], "config": snapshot["config"], "server_metrics": snapshot["metrics"], **out}


# 한도 파일의 범위 안에서 유입 조건 선택
def randomized(seed):
    rng = random.Random(seed)
    count = rng.randint(*LIMITS["request_count"])
    rate = rng.randint(*LIMITS["rate_per_s"])

    return {"scenario_id": "surge", "request_count": count, "arrival_duration_s": round(count / rate, 2),
            "seed": seed, "client_timeout_ms": LIMITS["client_timeout_ms"]}


# 서버 재시작 후 준비 완료까지 대기
def restart(run_id, reason):
    scope = guard.scope(DATA)

    # 허용 범위 밖의 파일이 바뀐 채로 재시작하면 그 뒤 측정을 믿을 수 없음
    if not scope["ok"]:
        raise SystemExit(json.dumps({"error": "OUTSIDE_ALLOWED_SCOPE", "scope": scope}, ensure_ascii=False))

    seq = call("GET", f"/api/runs/{run_id}")["last_seq"]
    operation = call("POST", "/api/service/restart", {"run_id": run_id, "reason": reason})["operation_id"]
    # 거절된 재시작까지 세면 같은 코드로 잰 실험이 두 묶음으로 갈림
    note({"restart": True})

    for _ in range(300):
        time.sleep(0.2)
        events = call("GET", f"/api/runs/{run_id}/events?after_seq={seq}&limit=1000")["events"]

        for event in events:
            if event["type"] in ("service.restarted", "service.restart_failed") and event["data"].get("operation_id") == operation:
                return {"operation_id": operation, "status": event["data"]["status"]}

    raise SystemExit(json.dumps({"error": "RESTART_WAIT_TIMEOUT", "operation_id": operation}))


# 전후 실험 비교에 고객 기준 결과를 붙여 최종 판정
def compare(before, after):
    scope = guard.scope(DATA)
    found, epoch = ledger()
    ids = {k: sorted(r for r, key in found.items() if key == found.get(v)) for k, v in (("before", before), ("after", after))}
    groups = {k: [verify.report(DATA, r) for r in v] for k, v in ids.items()}
    b, a = groups["before"], groups["after"]
    result = {"verdict": None, "reasons": [], "correctness_passed": None, "metrics": []}
    invalid = [f"{r['run_id']}: {r.get('invalid_reason') or '예정한 부하가 전달되지 않았습니다.'}" for r in b + a if not r["valid"]]

    if min(len(b), len(a)) < LIMITS["repeats"]:
        invalid.append(f"이번 시도의 실험이 기준 {len(b)}회와 후보 {len(a)}회입니다. 각 {LIMITS['repeats']}회가 필요합니다.")

    # 지난 후보의 판정을 지금 실행 중인 코드의 판정으로 쓰지 못하게 함
    if after in found and found[after][0] != epoch:
        invalid.append("되돌리기 또는 재시작으로 후보를 측정할 때의 상태가 바뀌었습니다. 지금 코드로 다시 측정하세요.")

    # 서버 비교 API는 수집이 빠진 실험을 오류로 거절하므로 유효성을 먼저 확인
    if not invalid:
        try:
            result = call("POST", "/api/comparisons", {"before_run_id": before, "after_run_id": after, "criteria_id": LIMITS["criteria_id"]})
            saved = json.loads((DATA / "runs" / after / f"comparison-{before}-{LIMITS['criteria_id']}.json").read_text())

            # 서버가 지난 시도의 실험을 평균에 섞었으면 이번 수정의 검증이 아님
            if any(sorted(saved[k + "_cohort"]) != ids[k] for k in ids):
                invalid.append("서버가 비교한 실험이 이번 시도의 실험과 다릅니다. 새 요청 목록으로 다시 측정하세요.")
        except SystemExit as exc:
            if "COMPARISON_NOT_READY" not in str(exc):
                raise

            invalid.append("서버 쪽 수집이 빠진 실험이 있습니다.")

    if invalid:
        decision, result["reasons"] = "measurement_invalid", invalid
    elif not scope["ok"] or any(r["wrong_results"] for r in a):
        decision = "reject"
    elif result["verdict"] == "regressed" and any(not r["successes"] for r in b) and all(r["judgement"] == "healthy" for r in a):
        # 서버 비교는 기준 실험 하나라도 성공 응답이 없으면 후보가 정상이어도 악화로 판정함
        decision = "hold"
        result["reasons"] = [("성공 응답이 없는 기준 실험이 있어 지표를 비교할 수 없습니다. 후보는 고객 목표를 충족했습니다.")]
    elif result["verdict"] == "regressed":
        decision = "reject"
    elif result["verdict"] == "improved" and all(r["judgement"] == "healthy" for r in a):
        decision = "accept"
    else:
        decision = "hold"

        if result["verdict"] == "improved":
            result["reasons"].append("지표는 나아졌지만 고객 목표에 미달한 실험이 있습니다.")

    rate = {k: round(sum(r["customer_success_rate"] for r in g) / len(g), 4) if g else None for k, g in groups.items()}

    return {"decision": decision, "verdict": result["verdict"], "reasons": result["reasons"],
            "correctness_passed": result["correctness_passed"], "scope": scope, "customer_success_rate": rate,
            "goal_success_rate": LIMITS["goal_success_rate"], "runs": ids, "metrics": result["metrics"]}
