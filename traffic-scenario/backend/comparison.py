import json
from statistics import mean

from .common import ApiError, canonical

FIXED = ("dataset_hash", "runtime_config_hash", "request_plan_hash", "warmup_policy")


# 실험 전후 성능 비교
def compare(store, settings, body):
    before, after = store.raw(body.before_run_id), store.raw(body.after_run_id)

    try:
        policies = json.loads(settings.policies_path.read_text())
        policy = policies[body.criteria_id]
        minimum = int(policy["min_repeats"])
        min_sent = int(policy["min_sent"])
        improvement = float(policy["min_relative_improvement"])
        tolerance = float(policy["allowed_relative_regression"])
        failure_delta = float(policy["max_failure_rate_increase"])

        if minimum < 1 or min_sent < 1 or not 0 <= improvement < 1 or not 0 <= tolerance < 1 or not 0 <= failure_delta <= 1:
            raise ValueError("invalid policy")
    except (OSError, ValueError, KeyError, TypeError):
        raise ApiError(409, "POLICY_NOT_READY", "판정 정책이 등록·확정되지 않았습니다.")

    for r in (before, after):
        if r["status"] != "completed":
            raise ApiError(409, "INVALID_RUN_STATE", "완료된 실험만 비교할 수 있습니다.")

        if not r["measurement_final"] or not r["telemetry_complete"] or not r["summary"] or r["summary"]["dropped"]:
            raise ApiError(409, "COMPARISON_NOT_READY", "수집 누락 또는 발생기 드롭이 있습니다.")

    if before["config"] != after["config"] or any(before["artifact"][k] != after["artifact"][k] for k in FIXED):
        raise ApiError(409, "INCOMPATIBLE_RUNS", "고정 실험 조건이 다릅니다.")

    runs = store.all_runs()

    # 같은 조건으로 반복한 실험 조회
    def cohort(reference):
        return [r for r in runs if r["status"] == "completed" and r["measurement_final"]
                and r["telemetry_complete"] and r["summary"] and not r["summary"]["dropped"]
                and r["config"] == reference["config"] and r["artifact"] == reference["artifact"]]

    bgroup, agroup = cohort(before), cohort(after)
    bvalues = [store.snapshot(r["run_id"]) for r in bgroup]
    avalues = [store.snapshot(r["run_id"]) for r in agroup]
    correct = True

    for r in {r["run_id"]: r for r in bgroup + agroup}.values():
        successful = [m for m in store.records(r["run_id"]) if m["outcome"] == "success"]

        if not successful:
            correct = False

        for m in successful:
            # 후보 코드가 만든 정답으로 잘못된 결과를 통과시키지 않도록 기준 실험 사용
            if m["items"] != before["expected"].get(canonical(m["query"])):
                correct = False

            detail = store.detail(r["run_id"], m["request_id"])

            if detail["server_status"] != "succeeded":
                raise ApiError(409, "COMPARISON_NOT_READY", "고객 성공 결과에 대응하는 서버 완료 이벤트가 없습니다.")

    rows = []

    for name in ("avg_response_ms", "p95_response_ms", "failure_rate", "success_rps"):
        b = [r["metrics"][name] for r in bvalues if r["metrics"][name] is not None]
        a = [r["metrics"][name] for r in avalues if r["metrics"][name] is not None]
        x, y = mean(b) if b else None, mean(a) if a else None
        rows.append({"name": name, "before": x, "after": y,
                     "delta": y - x if x is not None and y is not None else None,
                     "relative_change": (y - x) / x if x not in (None, 0) and y is not None else None})

    reasons = []
    ready = len(bgroup) >= minimum and len(agroup) >= minimum and all(v["client_counts"]["sent"] >= min_sent for v in bvalues + avalues)
    metrics = {v["name"]: v for v in rows}

    if not correct:
        verdict = "regressed"
        reasons.append("검색 결과의 정확성 또는 정렬 순서가 보존되지 않았습니다.")
    elif not ready or any(v["before"] is None or v["after"] is None for v in rows):
        verdict = "inconclusive"
        reasons.append(f"정책의 반복·표본 요건 미충족: 기준 {len(bgroup)}회, 후보 {len(agroup)}회, 필요 {minimum}회.")
    else:
        failure = metrics["failure_rate"]
        slower = any(metrics[n]["after"] > metrics[n]["before"] * (1 + tolerance) for n in ("avg_response_ms", "p95_response_ms"))
        throughput_down = metrics["success_rps"]["after"] < metrics["success_rps"]["before"] * (1 - tolerance)

        if failure["after"] > failure["before"] + failure_delta or slower or throughput_down:
            verdict = "regressed"
            reasons.append("실패율 증가 또는 허용 범위를 넘는 지연·처리량 악화가 있습니다.")
        elif (any(metrics[n]["after"] < metrics[n]["before"] * (1 - improvement) for n in ("avg_response_ms", "p95_response_ms"))
              or metrics["success_rps"]["after"] > metrics["success_rps"]["before"] * (1 + improvement)
              or failure["after"] < failure["before"]):
            verdict = "improved"
            reasons.append("고정 정책의 개선 기준을 충족하고 오류·지연·처리량의 회귀가 없습니다.")
        else:
            verdict = "inconclusive"
            reasons.append("차이가 정책의 최소 개선 폭에 미달합니다.")

    result = {"before_run_id": body.before_run_id, "after_run_id": body.after_run_id, "criteria_id": body.criteria_id,
              "verdict": verdict, "reasons": reasons, "metrics": rows, "correctness_passed": correct}
    folder = settings.runs_dir / body.after_run_id
    (folder / f"comparison-{body.before_run_id}-{body.criteria_id}.json").write_text(json.dumps({**result,
        "policy": policy, "before_cohort": [r["run_id"] for r in bgroup], "after_cohort": [r["run_id"] for r in agroup]}, ensure_ascii=False, indent=2))

    return result
