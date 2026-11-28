# -*- coding: utf-8 -*-
"""비즈니스 규칙 — 버그를 고칠 때 조심해야 할 업무 규칙과, 고치기 전에 담당 팀에 물어야 하는 곳.

고칠 레포에 마크다운 표로 둔다 (팀이 읽고 고치기 쉽게, 표준 라이브러리만으로 읽히게):

  | id | 대상 | 규칙 | 조치 | 담당 |
  |---|---|---|---|---|
  | PAY-01 | app.js:cartTotal, server.py:confirm_payment | 결제 금액 계산을 바꾸면 정산이 달라진다 | 문의 | 정산팀 |

  대상: `파일` 또는 `파일:함수`, 쉼표로 여러 개. 파일은 경로 끝이 같으면 맞는다 (app.js ↔ src/app.js)
  조치: 문의 = 담당 팀 확인 전에는 고치지 않는다 (PR 없이 "팀 문의" 우리로, 질문 초안을 남긴다)
        주의 = 고치되 규칙을 패치 요청과 Codex 리뷰에 같이 넘긴다

규칙 확인은 AI 없이 한다: 쥐의 스택 프레임(파일·함수)과, 실제 패치가 바꾸는 파일을 대상과 비교한다.
그래서 OpenAI 호출이 늘지 않고, 같은 이슈면 늘 같은 결과다. 팀에 메시지를 보내지는 않는다 (초안만).

  XMAS_RULES   규칙 파일 경로. 없으면 XMAS_FIX_REPO 의 BUSINESS_RULES.md, 그것도 없으면 규칙 없음(지금과 같음)
"""
import os
import re

ACTIONS = ("문의", "주의")
COLUMNS = {"id": "id", "대상": "targets", "규칙": "rule", "조치": "action", "담당": "team"}


def _cells(line):
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip().strip("`").strip() for c in s[1:-1].split("|")]


def parse(text):
    """마크다운 표 → 규칙 목록. 머리줄의 열 이름(id·대상·규칙·조치·담당)으로 열을 찾는다. 잘못된 줄은 건너뛴다."""
    out, cols = [], None
    for line in (text or "").splitlines():
        cells = _cells(line)
        if cells is None:
            cols = None  # 표가 끝났다
            continue
        if cols is None:
            names = [COLUMNS.get(c.lower() if c.isascii() else c) for c in cells]
            if "id" in names and "targets" in names and "action" in names:
                cols = names
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # 구분줄
        row = {k: cells[i] if i < len(cells) else "" for i, k in enumerate(cols) if k}
        action = row.get("action", "")
        if not row.get("id") or action not in ACTIONS:
            continue
        targets = []
        for t in row.get("targets", "").split(","):
            t = t.strip()
            if not t:
                continue
            f, _, fn = t.partition(":")
            targets.append((f.strip(), fn.strip() or None))
        if targets:
            out.append({"id": row["id"], "targets": targets, "rule": row.get("rule", ""),
                        "action": action, "team": row.get("team", "") or "담당 팀"})
    return out


def load(path):
    """규칙 파일을 읽는다. 없거나 못 읽으면 []."""
    try:
        with open(path, encoding="utf-8") as f:
            return parse(f.read())
    except OSError:
        return []


def default_path():
    p = os.environ.get("XMAS_RULES", "").strip()
    if p:
        return p
    repo = os.environ.get("XMAS_FIX_REPO", "").strip()
    if repo and os.path.isfile(os.path.join(repo, "BUSINESS_RULES.md")):
        return os.path.join(repo, "BUSINESS_RULES.md")
    return None


def _same_file(target, path):
    """대상 파일과 경로가 같은 파일인가: 끝이 같으면 (app.js ↔ src/app.js, https://host/app.js)."""
    path = (path or "").split("?")[0]
    return bool(target) and (path == target or path.endswith("/" + target))


def _same_func(target, fn):
    """함수 이름이 같은가: Object.cartTotal · async cartTotal 처럼 앞에 붙은 것은 무시."""
    fn = (fn or "").strip()
    return fn == target or re.search(r"(^|[.\s])%s$" % re.escape(target), fn) is not None


def match(rules, frames=None):
    """스택 프레임에 걸리는 규칙들 (규칙 순서대로, 중복 없이)."""
    out = []
    for r in rules or []:
        for f in frames or []:
            if any(_same_file(tf, f.get("file")) and (tfn is None or _same_func(tfn, f.get("function")))
                   for tf, tfn in r["targets"]):
                out.append(r)
                break
    return out


def match_patch(rules, path, diff=""):
    """패치가 바꾸는 파일에 걸리는 규칙들. 함수가 지정된 대상은 그 함수 이름이 diff 에 나올 때만."""
    out = []
    for r in rules or []:
        if any(_same_file(tf, path) and (tfn is None or tfn in (diff or "")) for tf, tfn in r["targets"]):
            out.append(r)
    return out


def blocking(rs):
    return [r for r in rs if r["action"] == "문의"]


def brief(rs):
    """status 에 넣을 모양."""
    return [{"id": r["id"], "action": r["action"], "team": r["team"]} for r in rs]


def notes(rs):
    """패치 요청·리뷰에 붙일 규칙 글."""
    return "\n".join("- [%s · %s · %s] %s" % (r["id"], r["action"], r["team"], r["rule"]) for r in rs)


def question(issue, rs):
    """담당 팀에 보낼 질문 초안 (AI 없이 틀대로). 보내지는 않는다."""
    teams = "·".join(sorted({r["team"] for r in rs}))
    f = (issue.get("frames") or [{}])[0]
    at = ""
    if f.get("file"):
        at = " 에러 위치: %s%s %s." % (f["file"], ":%s" % f["line"] if f.get("line") is not None else "",
                                     f.get("function") or "")
    rules_txt = " / ".join("%s(%s)" % (r["id"], r["rule"]) for r in rs)
    return ("[%s] %s (%s) 수정이 %s 에 걸립니다.%s 어떻게 처리하면 될지 확인 부탁드립니다."
            % (teams, issue.get("id", "?"), (issue.get("title") or "")[:60], rules_txt, at))
