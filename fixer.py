# -*- coding: utf-8 -*-
"""병정의 실제 수정 — 실연동(--live) 이슈를 고치는 코드 패치를 만들어 GitHub PR 을 연다.

  병정이 fixable 판정을 내린 실제 Sentry 이슈 → OpenAI 가 대상 레포 소스를 읽고 최소 패치(찾아 바꾸기)를 만든다
  → 패치가 정확히 한 곳에 맞는지 확인 → Codex(codex exec, 읽기 전용)가 변경을 리뷰해 승인·반려
  → 반려면 PR 없이 사람에게 넘긴다 → 승인이면 고친 파일 문법 확인 → 별도 git worktree 의 새 브랜치에 커밋
  → push → gh pr create. 결과 PR 주소는 잡은 쥐 기록·작전 로그·./market status 에 남는다.

사람이 리뷰하고 머지한다. 이 모듈은 머지하지 않고, 기본 브랜치에 직접 푸시하지 않는다.
같은 이슈는 PR 을 한 번만 연다 (.runtime/prs.json). 표준 라이브러리 + git · gh CLI.

환경변수 (레포의 .env 에 적어도 된다)
  XMAS_FIX_REPO    고칠 레포의 로컬 경로 (예: /Users/me/demo-web). 없으면 이 기능은 꺼진다
  XMAS_FIX_FILES   패치 대상으로 보여 줄 파일들, 쉼표 구분 (기본 app.js,server.py)
  XMAS_FIX_BASE    PR 의 기준 브랜치 (기본 main)
  XMAS_FIX_MAX     한 번 실행에서 열 PR 최대 수 (기본 5)
  XMAS_FIX_REVIEW  codex(기본: codex CLI 가 있으면 PR 전에 Codex 리뷰) / off
  XMAS_FIX_REVIEW_TIMEOUT  Codex 리뷰 한 번을 기다리는 최대 초 (기본 90). 넘으면 한 번 더 시도하고, 그래도 안 되면
                           PR 을 열지 않고 사람에게 넘긴다

Codex 리뷰는 한 번에 하나씩, 추론 강도 medium · MCP 서버 없이 돈다. codex exec 가 가끔 답 없이 멈춰서
(평가 중 11건 중 6건이 120초 초과) 시간 제한 + 1회 재시도를 둔다.
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import rules

FIX_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["file", "find", "replace", "title", "explanation"],
    "properties": {
        "file": {"type": "string"},
        "find": {"type": "string"},
        "replace": {"type": "string"},
        "title": {"type": "string"},
        "explanation": {"type": "string"},
    },
}

FIX_PROMPT = """너는 sentry-bot 의 수정 담당 병정이다. Sentry 이슈 하나와 레포의 소스 파일들을 받아, 그 에러를 없애는 가장 작은 코드 수정을 만든다.
- file: 고칠 파일 경로 (받은 파일 중 하나 그대로)
- find: 그 파일에서 바꿀 부분을 그대로 복사한 문자열. 파일 안에서 정확히 한 번만 나와야 한다. 들여쓰기·공백까지 같게. 3~12줄 정도로 충분히 길게
- replace: find 를 대신할 코드. 에러 원인을 방어하거나 바로잡는다. 주변 스타일을 따르고, 관계없는 곳은 건드리지 않는다. 주석의 '버그:' 표시는 무엇을 고쳤는지로 바꾼다
- title: PR 제목 (한국어, 50자 이내, "fix: " 로 시작)
- explanation: 원인과 수정 내용 (한국어, 2~3문장)
백엔드가 5xx 를 주는 것처럼 프론트 코드로 못 고치는 문제는 만들지 말고 find 를 "" 로 둔다.
"""

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "reason": {"type": "string"},
    },
}

REVIEW_PROMPT = """너는 sentry-bot 이 만든 자동 수정 PR 을 올리기 전에 검토하는 리뷰어다. 파일은 읽기만 하고 고치지 않는다.
아래 Sentry 이슈와 변경(diff)을 보고, 필요하면 레포의 %s 를 열어 주변 코드를 확인한다.
- approve: 이 변경이 그 에러의 원인을 막고, 관계없는 동작을 바꾸지 않으며, 새 버그를 만들지 않는다
- reject: 에러를 못 막거나(증상만 숨김 포함), 다른 동작을 깨거나, 이슈와 관계없는 수정이다
reason 은 한국어 한 문장(80자 이내)으로 판단 근거를 쓴다.

Sentry 이슈 %s: %s
위치: %s

--- diff ---
%s
"""

_lock = threading.Lock()
_review_lock = threading.Lock()  # codex exec 는 한 번에 하나씩


class ReviewRejected(Exception):
    """Codex 리뷰가 변경을 반려했다 — PR 을 열지 않고 사람에게 넘긴다."""


class AskTeam(Exception):
    """패치가 비즈니스 규칙('문의')에 걸리는 곳을 바꾼다 — PR 을 열지 않고 담당 팀에 묻는다."""

    def __init__(self, hit):
        super().__init__(", ".join(r["id"] for r in hit))
        self.rules = hit


def review_enabled():
    mode = os.environ.get("XMAS_FIX_REVIEW", "codex").strip().lower()
    return mode not in ("off", "0", "no", "false") and shutil.which("codex") is not None


def codex_review(issue, f, diff, repo, run=None, timeout=None, tries=2):
    """codex exec(읽기 전용 샌드박스)로 diff 를 리뷰한다. {"verdict": approve|reject, "reason": ..., "tries": n} 를 돌려준다.
    시간 초과면 tries 번까지 다시 한다. run 은 테스트에서 subprocess.run 대신 끼우는 함수다."""
    run = run or subprocess.run
    timeout = timeout or float(os.environ.get("XMAS_FIX_REVIEW_TIMEOUT", "").strip() or 90)
    for n in range(1, tries + 1):
        try:
            return dict(_codex_review_once(issue, f, diff, repo, run, timeout), tries=n)
        except subprocess.TimeoutExpired:
            if n == tries:
                raise RuntimeError("codex exec 가 %d초 안에 답하지 않음 (%d회 시도)" % (timeout, tries))


def _codex_review_once(issue, f, diff, repo, run, timeout):
    with _review_lock:  # codex exec 는 한 번에 하나씩
        tmp = tempfile.mkdtemp(prefix="xmas-review-")
        try:
            schema, out = os.path.join(tmp, "schema.json"), os.path.join(tmp, "out.json")
            with open(schema, "w", encoding="utf-8") as fh:
                json.dump(REVIEW_SCHEMA, fh)
            prompt = REVIEW_PROMPT % (f, issue["id"], issue["title"], issue.get("culprit") or "-", diff)
            if issue.get("rule_notes"):  # 비즈니스 규칙: 어기는 변경이면 reject
                prompt += ("\n--- 지켜야 할 비즈니스 규칙 (어기는 변경이면 reject) ---\n%s\n" % issue["rule_notes"])
            r = run(["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-C", repo,
                     "-c", 'model_reasoning_effort="medium"', "-c", "mcp_servers={}",
                     "--output-schema", schema, "-o", out, prompt],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                raise RuntimeError("codex exec 실패: %s" % (r.stderr or r.stdout or "").strip()[-120:])
            with open(out, encoding="utf-8") as fh:
                obj = json.loads(fh.read().strip())
            if obj.get("verdict") not in ("approve", "reject"):
                raise ValueError("리뷰 응답 형식이 다름")
            return {"verdict": obj["verdict"], "reason": str(obj.get("reason", ""))[:200], "by": "Codex"}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def frames_lines(frames, limit=3):
    """스택 프레임 [{file, line, function, code}] → 패치 요청에 붙일 줄들 (에러가 난 곳부터 limit 개)."""
    out = []
    for f in (frames or [])[:limit]:
        at = "%s%s" % (f.get("file") or "?", ":%s" % f["line"] if f.get("line") is not None else "")
        out.append("  %s in %s%s" % (at, f.get("function") or "?", ("  > " + f["code"]) if f.get("code") else ""))
    return "\n".join(out)


def configured():
    repo = os.environ.get("XMAS_FIX_REPO", "").strip()
    return bool(repo) and os.path.isdir(os.path.join(repo, ".git"))


def _run(argv, cwd, timeout=60):
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("%s 실패: %s" % (" ".join(argv[:3]), (r.stderr or r.stdout).strip()[-200:]))
    return r.stdout.strip()


def check_syntax(path):
    """고친 파일이 문법상 맞는지. node·python 이 없으면 그 검사는 건너뛴다."""
    if path.endswith(".js") and shutil.which("node"):
        _run(["node", "--check", path], os.path.dirname(path))
    elif path.endswith(".py"):
        _run(["python3", "-m", "py_compile", path], os.path.dirname(path))


def apply_patch(src, find, replace):
    """find 가 정확히 한 번 나와야 바꾼다."""
    if not find.strip():
        raise ValueError("프론트 코드로 고칠 수 없다고 판단 (패치 없음)")
    n = src.count(find)
    if n != 1:
        raise ValueError("패치 위치가 %d곳에 맞음 (정확히 1곳이어야 함)" % n)
    if find == replace:
        raise ValueError("바뀐 내용이 없음")
    return src.replace(find, replace, 1)


class Fixer:
    """수정 작업을 백그라운드 스레드에서 한다. 결과는 World 가 poll() 로 가져간다.

    ask(instructions, user, name, schema) 는 ai.Brain.ask 를 쓴다 (테스트에서는 가짜로 바꿔 끼운다).
    git·gh 를 부르는 publish 도 바꿔 끼울 수 있다.
    """

    def __init__(self, ask, repo=None, files=None, base=None, max_prs=None, runtime_dir=None, publish=None, review=None):
        env = os.environ.get
        self.ask = ask
        self.repo = repo or env("XMAS_FIX_REPO", "").strip()
        self.files = files or [f.strip() for f in (env("XMAS_FIX_FILES", "") or "app.js,server.py").split(",") if f.strip()]
        self.base = base or env("XMAS_FIX_BASE", "").strip() or "main"
        self.max_prs = max_prs if max_prs is not None else int(env("XMAS_FIX_MAX", "").strip() or 5)
        self.runtime_dir = runtime_dir
        self.publish = publish or self._publish
        self.review = review  # (issue, file, diff, repo) -> {"verdict", "reason"}. None 이면 리뷰 없이 PR
        self.rules = []  # 비즈니스 규칙 (rules.parse). 비어 있으면 예전과 같다
        self.results, self.opened, self.busy = [], 0, set()
        self.db_path = os.path.join(runtime_dir, "prs.json") if runtime_dir else None
        self.db = self._load()

    @property
    def name(self):
        return os.path.basename(self.repo.rstrip("/"))

    def _load(self):
        try:
            with open(self.db_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self):
        if not self.db_path:
            return
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(self.db, f, ensure_ascii=False, indent=1)

    def submit(self, issue, ctx):
        """이슈 하나 고치기 시작. 이미 PR 이 있으면 바로 그 결과를 돌려준다."""
        key = issue["id"]
        with _lock:
            if key in self.db:
                self.results.append((ctx, dict(self.db[key], reused=True), None))
                return
            if key in self.busy:
                return
            if self.opened >= self.max_prs:
                self.results.append((ctx, None, RuntimeError("이번 실행의 PR 한도(%d개)에 닿음" % self.max_prs)))
                return
            self.busy.add(key)
        threading.Thread(target=self._work, args=(issue, ctx), daemon=True).start()

    def poll(self):
        with _lock:
            out, self.results = self.results, []
        return out

    def _work(self, issue, ctx):
        try:
            res = self.fix(issue)
            with _lock:
                self.db[issue["id"]] = res
                self.opened += 1
                self._save()
                self.results.append((ctx, res, None))
        except Exception as e:  # noqa: BLE001 — 실패해도 대시보드는 계속 돈다
            with _lock:
                self.results.append((ctx, None, e))
        finally:
            with _lock:
                self.busy.discard(issue["id"])

    def sources(self):
        out = {}
        for f in self.files:
            p = os.path.join(self.repo, f)
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    out[f] = fh.read()
        if not out:
            raise RuntimeError("%s 에서 고칠 파일(%s)을 못 찾음" % (self.repo, ", ".join(self.files)))
        return out

    def fix(self, issue):
        src = self.sources()
        user = "Sentry 이슈 %s\n제목: %s\n위치: %s\n" % (issue["id"], issue["title"], issue.get("culprit") or "-")
        stack = frames_lines(issue.get("frames"))
        if stack:  # 스택이 있으면 에러가 난 파일·줄·코드를 같이 준다 (없으면 예전과 같은 입력)
            user += "스택 (에러가 난 곳부터):\n%s\n" % stack
        if issue.get("rule_notes"):  # 걸린 비즈니스 규칙 (없으면 예전과 같은 입력)
            user += "지켜야 할 비즈니스 규칙 (어기는 수정은 만들지 않는다):\n%s\n" % issue["rule_notes"]
        user += "\n"
        user += "\n\n".join("=== %s ===\n%s" % (f, body) for f, body in src.items())
        p = self.ask(FIX_PROMPT, user, "sentry_fix", FIX_SCHEMA)
        f = p.get("file", "")
        if f not in src:
            raise ValueError("모르는 파일을 고치려 함: %r" % f[:40])
        new = apply_patch(src[f], p.get("find", ""), p.get("replace", ""))
        title = (p.get("title") or "fix: %s" % issue["title"])[:72]
        if not title.startswith("fix"):
            title = "fix: " + title
        diff = "".join(difflib.unified_diff(src[f].splitlines(True), new.splitlines(True), "a/" + f, "b/" + f))
        if self.rules and not issue.get("ask_ok"):  # 패치가 '문의' 규칙에 걸리는 곳을 바꾸면 PR 대신 팀에 묻는다
            hit = rules.blocking(rules.match_patch(self.rules, f, diff))
            if hit:
                raise AskTeam(hit)
        rv = None
        if self.review is not None:
            try:
                rv = self.review(issue, f, diff, self.repo)
            except Exception as e:  # noqa: BLE001 — 검토하지 못한 수정은 PR 로 올리지 않는다
                raise ReviewRejected("Codex 리뷰를 못 함 (%s)" % str(e)[:80])
            if rv["verdict"] != "approve":
                raise ReviewRejected(rv["reason"])
        review_line = "" if rv is None else "🔍 **Codex 리뷰: 승인** — %s\n\n" % rv["reason"]
        rules_line = ""
        if issue.get("rule_notes"):
            rules_line = "📜 **비즈니스 규칙**%s\n%s\n\n" % (" (담당 팀 확인 후 진행)" if issue.get("ask_ok") else "",
                                                      issue["rule_notes"])
        body = ("Sentry 이슈 **%s** — `%s`\n\n%s\n\n" % (issue["id"], issue["title"], (p.get("explanation") or "").strip())
                + review_line + rules_line
                + ("Sentry: %s\n\n" % issue["url"] if issue.get("url") else "")
                + "---\n이 PR 은 MERRY BUGMAS 대시보드의 병정(sentry-bot)이 OpenAI API 로 만들고"
                + (" Codex 가 리뷰한" if rv else "") + " 자동 수정이다. 사람이 리뷰한 뒤 머지한다.\n")
        res = self.publish(issue, f, new, title, body)
        if rv is not None:
            res = dict(res, review=rv)
        return res

    def _publish(self, issue, f, new, title, body):
        """새 브랜치를 별도 worktree 에 만들어 커밋·push 하고 PR 을 연다. 사용 중인 체크아웃은 건드리지 않는다."""
        branch = "sentry-bot/%s-%s" % (re.sub(r"[^A-Za-z0-9-]", "-", issue["id"]).lower(), time.strftime("%H%M%S"))
        tmp = tempfile.mkdtemp(prefix="xmas-fix-")
        wt = os.path.join(tmp, "wt")
        try:
            _run(["git", "fetch", "-q", "origin", self.base], self.repo)
            _run(["git", "worktree", "add", "-q", "-b", branch, wt, "origin/" + self.base], self.repo)
            path = os.path.join(wt, f)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
            check_syntax(path)
            diff = _run(["git", "diff", "--stat"], wt)
            _run(["git", "add", f], wt)
            _run(["git", "commit", "-q", "-m", "%s\n\nSentry %s: %s\n\nCo-Authored-By: MERRY BUGMAS sentry-bot (OpenAI API)"
                  % (title, issue["id"], issue["title"])], wt)
            _run(["git", "push", "-q", "-u", "origin", branch], wt, timeout=90)
            url = _run(["gh", "pr", "create", "--base", self.base, "--head", branch, "--title", title, "--body", body], wt, timeout=90)
            url = url.strip().splitlines()[-1]
            num = int(re.search(r"/pull/(\d+)", url).group(1))
            return {"pr": num, "url": url, "title": title, "branch": branch, "file": f, "diff": diff,
                    "at": time.strftime("%Y-%m-%d %H:%M")}
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=self.repo, capture_output=True)
            shutil.rmtree(tmp, ignore_errors=True)
