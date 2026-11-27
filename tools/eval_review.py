#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex PR 리뷰어 평가: 정답이 붙은 패치(맞는 수정 / 틀린 수정)를 fixer.codex_review 로 판정시켜 정확도를 잰다.

  python3 tools/eval_review.py [demo-web 경로, 기본 XMAS_FIX_REPO 또는 ~/demo-web]  → docs/eval/review-codex-run.md (여러 실행을 합친 정리는 review-codex.md)

패치는 demo-web 의 app.js 에 일부러 심어 둔 버그 4곳(WEB-C·D·E·J)에 대한 것이다.
  good: 에러 원인을 막고 정상 동작은 유지 (실제 PR #5~#7 의 패치 포함)  → approve 가 정답
  bad : 에러를 못 막음 / 정상 동작을 깨뜨림 / 관계없는 곳 수정            → reject 가 정답
Codex 는 읽기 전용 샌드박스에서 돈다. OpenAI API 크레딧은 쓰지 않는다 (Codex 사용량만).
"""
import concurrent.futures as cf
import difflib
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ai  # noqa: E402
import fixer  # noqa: E402

C = {"id": "WEB-C", "title": "TypeError: Cannot read properties of undefined (reading 'map')", "culprit": "app.js gifts"}
D = {"id": "WEB-D", "title": "TypeError: Cannot read properties of null (reading 'rate')", "culprit": "app.js cart"}
E = {"id": "WEB-E", "title": "RangeError: Invalid time value", "culprit": "app.js date"}
J = {"id": "WEB-J", "title": "SyntaxError: Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON", "culprit": "app.js notice"}

C_LINE = "      return data.items.map((g) => g.name); // 버그: items 가 undefined"
D_LINE = "      return cart.items.length * (1 - cart.coupon.rate); // 버그: coupon 이 null"
E_LINE = '    run: () => new Date("2026-12-25T25:61").toISOString(), // 버그: Invalid Date'
J_LINE = "      return JSON.parse(await res.text()); // 버그: HTML 을 JSON 으로 파싱"

# (이름, 이슈, 정답, 왜, find, replace)
CASES = [
    ("C-실제PR#6", C, "approve", "items 가 배열일 때만 map",
     C_LINE, "      return Array.isArray(data.items) ? data.items.map((g) => g.name) : []; // 수정: items 없으면 빈 배열"),
    ("C-빈배열대체", C, "approve", "items 가 없으면 빈 배열",
     C_LINE, "      return (data.items || []).map((g) => g.name); // 수정: items 가 없을 때 방어"),
    ("D-실제PR#5", D, "approve", "coupon null 이면 할인율 0",
     D_LINE, "      const rate = cart.coupon && typeof cart.coupon.rate === \"number\" ? cart.coupon.rate : 0;\n"
             "      return cart.items.length * (1 - rate); // 수정: coupon 이 null 이면 할인 없음"),
    ("E-실제PR#7", E, "approve", "유효한 날짜일 때만 toISOString",
     E_LINE, "    run: () => {\n      const d = new Date(\"2026-12-25T25:61\");\n"
             "      return Number.isFinite(d.getTime()) ? d.toISOString() : \"Invalid Date\"; // 수정: 날짜 검사\n    },"),
    ("J-타입확인", J, "approve", "JSON 일 때만 파싱, 아니면 null",
     J_LINE, "      const text = await res.text();\n"
             "      const type = res.headers.get(\"content-type\") || \"\";\n"
             "      return res.ok && type.includes(\"json\") ? JSON.parse(text) : null; // 수정: HTML 이면 파싱 안 함"),
    ("D-여전히null접근", D, "reject", "cart.coupon.rate 를 그대로 읽어 여전히 터짐",
     D_LINE, "      return cart.items.length * (1 - (cart.coupon.rate || 0)); // 수정: 할인율 기본값"),
    ("J-res.json", J, "reject", "res.json() 도 HTML 이면 같은 에러",
     J_LINE, "      return await res.json(); // 수정: json() 으로 읽기"),
    ("E-여전히잘못된날짜", E, "reject", "25시 61분은 Z 를 붙여도 Invalid Date",
     E_LINE, '    run: () => new Date("2026-12-25T25:61Z").toISOString(), // 수정: UTC 로 지정'),
    ("C-관계없는곳", C, "reject", "설명 문구만 바꾸고 map 은 그대로",
     '    desc: "API 응답에 items 가 없는데 확인 없이 .map() 을 부른다.",',
     '    desc: "API 응답의 items 로 선물 이름 목록을 만든다.",'),
    ("C-항상빈배열", C, "reject", "에러는 사라지지만 정상 응답도 버림",
     C_LINE, "      return []; // 수정: 에러 방지"),
    ("D-항상0", D, "reject", "에러는 사라지지만 합계가 늘 0",
     D_LINE, "      return 0; // 수정: null 방어"),
]


def main():
    ai.load_dotenv()
    repo = sys.argv[1] if len(sys.argv) > 1 else (os.environ.get("XMAS_FIX_REPO") or os.path.expanduser("~/demo-web"))
    with open(os.path.join(repo, "app.js"), encoding="utf-8") as f:
        src = f.read()
    only = os.environ.get("EVAL_ONLY", "").strip()  # approve / reject 만 다시 돌릴 때
    jobs = []
    for name, issue, want, why, find, rep in CASES:
        if only and want != only:
            continue
        new = fixer.apply_patch(src, find, rep)  # 정확히 한 곳에 맞는지 먼저 확인
        diff = "".join(difflib.unified_diff(src.splitlines(True), new.splitlines(True), "a/app.js", "b/app.js"))
        jobs.append((name, issue, want, why, diff))

    def one(job):
        name, issue, want, why, diff = job
        t = time.time()
        try:
            rv = fixer.codex_review(issue, "app.js", diff, repo)
        except Exception as e:  # noqa: BLE001
            rv = {"verdict": "error", "reason": str(e)[:100]}
        return name, issue["id"], want, why, rv, time.time() - t

    rows = []
    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        for r in ex.map(one, jobs):
            rows.append(r)
            print("%-14s 정답 %-7s → Codex %-7s %4.0f초 %s회  %s" % (r[0], r[2], r[4]["verdict"], r[5], r[4].get("tries", "-"), r[4]["reason"][:70]), flush=True)

    ok = sum(1 for r in rows if r[4]["verdict"] == r[2])
    bad = [r for r in rows if r[2] == "reject"]
    good = [r for r in rows if r[2] == "approve"]
    caught = sum(1 for r in bad if r[4]["verdict"] == "reject")
    passed = sum(1 for r in good if r[4]["verdict"] == "approve")
    errors = sum(1 for r in rows if r[4]["verdict"] == "error")
    retried = sum(1 for r in rows if r[4].get("tries", 1) > 1)
    avg = sum(r[5] for r in rows) / len(rows)
    lines = [
        "# Codex PR 리뷰어 평가",
        "",
        "`python3 tools/eval_review.py` · %s · Codex CLI(`codex exec -s read-only --output-schema`)" % time.strftime("%Y-%m-%d"),
        "",
        "- **정확도: %d / %d = %.1f%%**" % (ok, len(rows), 100.0 * ok / len(rows)),
        "- 틀린 패치를 반려: **%d / %d** · 맞는 패치를 승인: **%d / %d** · 호출 실패 %d건 · 시간 초과로 재시도 %d건 · 평균 %.0f초" % (
            caught, len(bad), passed, len(good), errors, retried, avg),
        "- 대상: demo-web `app.js` 에 심어 둔 버그 4곳(WEB-C·D·E·J). 맞는 패치 %d개(실제 PR #5~#7 포함), 틀린 패치 %d개"
        "(에러를 못 막음 4 · 에러는 사라지지만 정상 동작을 깨뜨림 2)" % (len(good), len(bad)),
        "- 한계: 직접 만든 11건, 1회 실행. 작은 레포의 한 줄짜리 수정만 다룬다",
        "",
        "| 결과 | 패치 | 이슈 | 정답 | Codex | 정답 이유 | Codex 근거 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, iid, want, why, rv, sec in rows:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            "✅" if rv["verdict"] == want else "❌", name, iid, want, rv["verdict"], why, rv["reason"].replace("|", "/")))
    out = os.path.join(ROOT, "docs", "eval", "review-codex-run.md")  # 실행 1회분 (여러 실행을 합친 정리는 review-codex.md)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines[4:6]) + "\n→ " + out)


if __name__ == "__main__":
    main()
