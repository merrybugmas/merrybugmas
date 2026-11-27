#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 트리아지 정확도 평가: 정답 판정이 달린 Sentry 이슈 제목을 OpenAI API 로 판정해 맞힌 비율과 응답 시간을 잰다.

  python3 tools/eval_triage.py                         # tests/data/triage_eval.jsonl 전부
  python3 tools/eval_triage.py --record recordings/ai.jsonl   # 결과를 녹화 파일에도 남긴다 (--replay 용)

정답(kind)은 ai.py TRIAGE_PROMPT 와 같은 기준으로 사람이 붙였다.
  fixable = 레포 코드 버그 / noise = 레포 밖 출처 / needs-human = 백엔드·보안·인프라 판단 필요
결과는 표준출력과 docs/eval/triage-<모델>.md 에 쓴다. OPENAI_API_KEY 필요 (.env 가능).
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ai  # noqa: E402

KINDS = ai.KINDS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "tests", "data", "triage_eval.jsonl"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--record", default=None, help="판정 결과를 녹화 파일(jsonl)에 덧붙인다")
    args = ap.parse_args()
    if not ai.available():
        sys.exit("OPENAI_API_KEY 가 없다 (.env 에 OPENAI_API_KEY=... 를 넣는다)")

    cases = [json.loads(l) for l in open(args.data, encoding="utf-8") if l.strip()]
    brain = ai.Brain(model=args.model, workers=0)

    def judge(i, c):
        t0 = time.time()
        try:
            res = brain.triage({"id": "EVAL-%d" % i, "title": c["title"], "service": c["service"]})
            return c, res, time.time() - t0, None
        except Exception as e:  # noqa: BLE001
            return c, None, time.time() - t0, "%s: %s" % (type(e).__name__, str(e)[:100])

    print("모델 %s · %d건 판정 중…" % (brain.model, len(cases)))
    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(lambda p: judge(*p), enumerate(cases)))

    ok = [r for r in results if r[3] is None]
    hit = [r for r in ok if r[1]["kind"] == r[0]["kind"]]
    lat = sorted(r[2] for r in ok)
    conf = {a: {b: 0 for b in KINDS} for a in KINDS}
    for c, res, _, _ in ok:
        conf[c["kind"]][res["kind"]] += 1

    lines = ["# AI 트리아지 평가 — %s" % brain.model, "",
             "- 일시: %s" % time.strftime("%Y-%m-%d %H:%M"),
             "- 데이터: `%s` (%d건, 정답은 사람이 붙임)" % (os.path.relpath(args.data, ROOT), len(cases)),
             "- **정확도: %d / %d = %.1f%%**" % (len(hit), len(ok), 100.0 * len(hit) / max(1, len(ok))),
             "- 응답 시간: 평균 %.2f초 · 중앙값 %.2f초 · 최대 %.2f초" % (
                 sum(lat) / max(1, len(lat)), lat[len(lat) // 2] if lat else 0, lat[-1] if lat else 0),
             "- 호출 실패: %d건" % (len(results) - len(ok)), "",
             "## 혼동 행렬 (행 = 정답, 열 = AI 판정)", "",
             "| 정답 \\ AI | " + " | ".join(KINDS) + " |", "|---" * (len(KINDS) + 1) + "|"]
    for a in KINDS:
        lines.append("| %s | " % a + " | ".join(str(conf[a][b]) for b in KINDS) + " |")
    lines += ["", "## 건별 결과", "", "| | 서비스 | 제목 | 정답 | AI | 근거 | 수정 요약 |", "|---|---|---|---|---|---|---|"]
    for c, res, sec, err in results:
        if err:
            lines.append("| ⚠ | %s | %s | %s | 실패 | %s | |" % (c["service"], c["title"][:60], c["kind"], err))
            continue
        mark = "✅" if res["kind"] == c["kind"] else "❌"
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            mark, c["service"], c["title"][:60].replace("|", "/"), c["kind"], res["kind"],
            res["reason"].replace("|", "/"), res["fix"].replace("|", "/")))
    report = "\n".join(lines) + "\n"
    print(report)

    out = os.path.join(ROOT, "docs", "eval", "triage-%s.md" % brain.model.replace("/", "_"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print("저장:", os.path.relpath(out, ROOT))

    if args.record:
        path = os.path.join(ROOT, args.record) if not os.path.isabs(args.record) else args.record
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for c, res, sec, err in ok:
                f.write(json.dumps({"kind": "triage", "key": c["title"], "model": brain.model,
                                    "at": time.strftime("%Y-%m-%d"), "result": res}, ensure_ascii=False) + "\n")
        print("녹화:", os.path.relpath(path, ROOT), "%d건" % len(ok))


if __name__ == "__main__":
    main()
