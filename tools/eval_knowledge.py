#!/usr/bin/env python3
"""검색 회귀 평가. --live이면 같은 입력으로 KB OFF/ON 실제 분류도 비교한다 (기본 OpenAI 0회)."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai
import knowledge


def evaluate(cases, kb, brain=None):
    rows = []
    for c in cases:
        issue = {k: c[k] for k in ("id", "title", "service", "culprit")}
        ctx = kb.retrieve(issue, c["repo"], c["service"])
        found = [x["id"] for x in ctx["cases"]]
        row = {"id": c["id"], "expected": c["expected"], "retrieved": found,
               "retrieval_pass": c["expected"] in found if c["expected"] else not found}
        if brain:
            for mode in ("off", "on"):
                payload = dict(issue, knowledge=ctx if mode == "on" else None)
                start = time.monotonic()
                try:
                    result = brain.triage(payload)
                    row[mode] = {"result": result, "correct": result["kind"] == c["kind"]}
                except Exception as e:
                    row[mode] = {"error": type(e).__name__, "correct": False}
                row[mode]["seconds"] = round(time.monotonic() - start, 3)
        rows.append(row)
    result = {"mode": "live-paired-triage" if brain else "offline-retrieval-regression",
              "scope": "작은 수작업 회귀 fixture. 일반화된 정확도/수정 성공률을 뜻하지 않음.",
              "revision": kb.revision, "model": brain.model if brain else None,
              "total": len(rows), "retrieval_pass": sum(r["retrieval_pass"] for r in rows), "rows": rows}
    if brain:
        result["classification"] = {mode: {"correct": sum(r[mode]["correct"] for r in rows),
            "errors": sum("error" in r[mode] for r in rows),
            "false_noise": sum(c["kind"] != "noise" and r[mode].get("result", {}).get("kind") == "noise"
                               for c, r in zip(cases, rows))} for mode in ("off", "on")}
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=str(Path(__file__).resolve().parents[1] / "tests/data/knowledge_eval.jsonl"))
    ap.add_argument("--knowledge", default=str(knowledge.DEFAULT_DIR))
    ap.add_argument("--live", action="store_true", help="실제 OpenAI로 OFF/ON 비교 (사례당 2회 요청, 재시도 가능)")
    ap.add_argument("--model")
    ap.add_argument("--output")
    args = ap.parse_args()
    kb = knowledge.KnowledgeBase(args.knowledge)
    if kb.errors:
        sys.exit("지식 파일 검증 실패")
    if args.live and not ai.available():
        sys.exit("--live에는 OPENAI_API_KEY가 필요합니다")
    cases = [json.loads(line) for line in Path(args.data).read_text(encoding="utf-8").splitlines() if line.strip()]
    report = evaluate(cases, kb, ai.Brain(model=args.model, workers=0) if args.live else None)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text)
    return 0 if report["retrieval_pass"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
