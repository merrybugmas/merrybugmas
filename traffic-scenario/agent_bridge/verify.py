# 검색 서버 코드와 분리된 기준으로 실험 결과를 판정하는 독립 검증기
import collections
import json
import sqlite3
from pathlib import Path

REFERENCE = """SELECT product_id,name,price,shop_name,location FROM products
WHERE category=? AND price<=? ORDER BY price,product_id LIMIT 10"""
REFERENCE_STOCK = """SELECT p.product_id,p.name,p.price,p.shop_name,p.location FROM products p
JOIN stock s ON s.product_id=p.product_id
WHERE p.category=? AND p.price<=? AND s.quantity>0 ORDER BY p.price,p.product_id LIMIT 10"""
LIMITS = json.loads((Path(__file__).parent / "limits.json").read_text())


# 읽기 전용 상품 DB 연결
def readonly(catalog):
    con = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row

    return con


# 검색 조건별 기준 정답 조회
def answers(catalog, queries):
    con = readonly(catalog)

    try:
        stock = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock'").fetchone()
        sql = REFERENCE_STOCK if stock else REFERENCE

        return {key: [dict(r) for r in con.execute(sql, key)] for key in queries}
    finally:
        con.close()


# 정렬된 값의 95번째 백분위 계산
def p95(values):
    values = sorted(values)

    return values[max(-(-len(values) * 95 // 100) - 1, 0)] if values else None


# 채점할 수 없는 실험의 측정 무효 결과
def invalid(run_id, reason):
    return {"run_id": run_id, "valid": False, "judgement": "measurement_invalid", "invalid_reason": reason,
            "successes": 0, "wrong_results": 0, "customer_success_rate": 0.0}


# 실험 한 번의 고객 기준 결과와 측정 유효성 판정
def report(data_dir, run_id, seal=False, source=None):
    folder = Path(data_dir) / "runs" / run_id
    sealed, marker = folder / "bridge-truth.json", folder / "bridge-source"

    # 나중에 다시 채점할 때도 재실험이었다는 사실을 잊지 않도록 출처를 남김
    if source and folder.is_dir():
        marker.write_text(source)
    elif marker.exists():
        source = marker.read_text()

    origin = Path(data_dir) / "runs" / source / "bridge-truth.json" if source else None

    if (folder / "bridge-incomplete").exists():
        return invalid(run_id, "서버 쪽 수집이 끝나지 않은 실험입니다.")

    try:
        rows = [json.loads(line) for line in (folder / "client-raw.jsonl").read_text().splitlines() if line]
        summary = json.loads((folder / "loadgen-summary.json").read_text())
        # 대기 한도를 확인할 수 없으면 기본값으로 채점하지 않음
        timeout = json.loads((folder / "config.json").read_text())["client_timeout_ms"]
    except (OSError, ValueError, KeyError):
        return invalid(run_id, "원본 기록이나 실험 설정이 없거나 손상됐습니다.")

    # 재실험은 처음 실험의 정답을 물려받아 나중에 DB가 바뀌어도 같은 기준으로 채점
    if not sealed.exists() and origin and origin.exists():
        sealed.write_text(origin.read_text())

    # 물려받을 정답이 없는 재실험은 수정된 DB가 정답이 될 수 있어 판정에서 제외
    orphan = bool(source) and not sealed.exists()

    if sealed.exists():
        truth = {tuple(json.loads(k)): v for k, v in json.loads(sealed.read_text()).items()}
    else:
        truth = answers(Path(data_dir) / "catalog.sqlite", {(r["query"]["category"], r["query"]["max_price"]) for r in rows})

        if seal and not orphan:
            sealed.write_text(json.dumps({json.dumps(k): v for k, v in truth.items()}, ensure_ascii=False))

    correct = wrong = late = 0
    lags = [r["sent_offset_ms"] - r["scheduled_offset_ms"] for r in rows]
    # 보내기도 전에 대기 한도를 다 쓴 요청은 서버가 아무리 빨라도 실패함
    spent = sum(lag >= timeout for lag in lags)

    for r in rows:
        if r["outcome"] == "success":
            same = r["items"] == truth.get((r["query"]["category"], r["query"]["max_price"]))
            # 고객 대기는 예정 도착 시각부터 세므로 전송 지연을 응답 시간에 더함
            slow = r["sent_offset_ms"] - r["scheduled_offset_ms"] + r["elapsed_ms"] > timeout
            wrong += not same
            late += same and slow
            correct += same and not slow

    lag = p95(lags)
    rate = correct / summary["planned"] if summary["planned"] else 0.0
    # 예정한 부하가 서버에 전달되지 않은 실험은 서버가 정상으로 보여도 판정에서 제외
    valid = (summary["sent"] == summary["planned"] and not summary["dropped"] and summary["termination"] == "completed"
             and len(rows) == summary["sent"] and lag is not None and lag <= LIMITS["max_send_lag_p95_ms"] and not orphan
             and (summary["planned"] - spent) / summary["planned"] >= LIMITS["goal_success_rate"])

    if not valid:
        judgement = "measurement_invalid"
    elif wrong:
        judgement = "wrong_results"
    elif rate >= LIMITS["goal_success_rate"]:
        judgement = "healthy"
    else:
        judgement = "degraded"

    return {"run_id": run_id, "planned": summary["planned"], "sent": summary["sent"], "dropped": summary["dropped"],
            "correct_in_time": correct, "correct_but_late": late, "wrong_results": wrong, "successes": correct + late + wrong,
            "customer_success_rate": round(rate, 4), "goal_success_rate": LIMITS["goal_success_rate"],
            "client_timeout_ms": timeout, "send_lag_p95_ms": round(lag, 1) if lag is not None else None,
            "p95_all_requests_ms": round(p95([r["elapsed_ms"] for r in rows]) or 0, 1),
            "outcomes": dict(collections.Counter(f"{r['outcome']}:{r['http_status']}" for r in rows)),
            "truth": "sealed" if sealed.exists() else "live",
            "sent_after_deadline": spent, "invalid_reason": None if valid else "처음 실험의 정답 기록이 없는 재실험입니다." if orphan
            else "예정한 부하가 제때 전달되지 않았습니다. 서버가 아니라 부하를 보내는 쪽을 점검하세요.", "valid": valid, "judgement": judgement}
