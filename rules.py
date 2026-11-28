# -*- coding: utf-8 -*-
"""비즈니스 규칙 — 버그를 고칠 때 조심해야 할 업무 규칙과, 고치기 전에 담당 팀에 물어야 하는 곳.

고칠 레포에 마크다운 표로 둔다 (팀이 읽고 고치기 쉽게, 표준 라이브러리만으로 읽히게):

  | id | 대상 | 규칙 | 조치 | 담당 |
  |---|---|---|---|---|
  | PAY-01 | app.js:cartTotal, server.py:confirm_payment | 결제 금액 계산을 바꾸면 정산이 달라진다 | 문의 | 정산팀 |

  대상: `파일` · `파일:함수` · `제목:키워드`, 쉼표로 여러 개 (키워드에는 쉼표를 쓰지 않는다)
        파일·함수는 Sentry 이슈의 culprit(에러 위치 한 줄)과, 패치가 바꾸는 파일·diff 에 비교한다
        제목:키워드는 이슈 제목에 그 글자가 들어 있으면 맞는다 (대소문자 무시). 위치 정보가 없는 mock 이슈도 이것으로 걸린다
  조치: 문의 = 담당 팀 확인 전에는 고치지 않는다 (PR 없이 "팀 문의" 우리로, 질문 초안을 남긴다)
        주의 = 고치되 규칙을 패치 요청과 Codex 리뷰에 같이 넘긴다

규칙 확인은 AI 없이 한다: 이슈의 제목·culprit 과, 실제 패치가 바꾸는 파일·diff 를 대상과 비교한다.
그래서 OpenAI 호출이 늘지 않고, 같은 이슈면 늘 같은 결과다. 팀에 메시지를 보내지는 않는다 (초안만).

  XMAS_RULES   규칙 파일 경로. 없으면 XMAS_FIX_REPO 의 BUSINESS_RULES.md, 그것도 없으면 규칙 없음(지금과 같음)
"""
import os
import re

ACTIONS = ("문의", "주의")
TITLE_KEYS = ("제목", "title")
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
            head, _, rest = t.partition(":")
            if head.strip() in TITLE_KEYS:
                if rest.strip():
                    targets.append({"title": rest.strip()})
                continue
            targets.append({"file": head.strip(), "func": rest.strip() or None})
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


def _word(text, word):
    """text 에 word 가 낱말로 들어 있나 (renderGifts ↔ Object.renderGifts, renderGiftsAll 은 아님)."""
    return bool(word) and re.search(r"(?<![\w$])%s(?![\w$])" % re.escape(word), text or "") is not None


def _in_culprit(target, culprit):
    """culprit 이 대상 파일을 가리키나. Sentry culprit 은 플랫폼마다 모양이 달라
    (`renderGifts(app.js)` · `https://host/app.js` · `server in confirm_payment`) 파일 이름이나 확장자 뺀 이름이 낱말로 있으면 맞다고 본다."""
    name = target.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return _word(culprit, name) or _word(culprit, stem)


def _hits(t, issue):
    if "title" in t:
        return t["title"].lower() in (issue.get("title") or "").lower()
    culprit = issue.get("culprit") or ""
    return _in_culprit(t["file"], culprit) and (t["func"] is None or _word(culprit, t["func"]))


def match(rules, issue):
    """이슈(제목·culprit)에 걸리는 규칙들 (규칙 순서대로)."""
    return [r for r in rules or [] if any(_hits(t, issue or {}) for t in r["targets"])]


def match_patch(rules, path, diff=""):
    """패치가 바꾸는 파일에 걸리는 규칙들. 함수가 지정된 대상은 그 함수 이름이 diff 에 나올 때만. 제목 대상은 보지 않는다."""
    out = []
    for r in rules or []:
        if any("file" in t and _same_file(t["file"], path) and (t["func"] is None or _word(diff, t["func"]))
               for t in r["targets"]):
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
    at = " 에러 위치: %s." % issue["culprit"] if issue.get("culprit") else ""
    rules_txt = " / ".join("%s(%s)" % (r["id"], r["rule"]) for r in rs)
    return ("[%s] %s (%s) 수정이 %s 에 걸립니다.%s 어떻게 처리하면 될지 확인 부탁드립니다."
            % (teams, issue.get("id", "?"), (issue.get("title") or "")[:60], rules_txt, at))
