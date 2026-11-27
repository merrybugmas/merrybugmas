# -*- coding: utf-8 -*-
"""병정의 두뇌 — OpenAI API (Responses API + structured output).

  order  : 채팅 명령 한 줄 → {병정, 행동, 이슈 ID, 대답}   (키워드에 없는 말도 알아듣는다)
  triage : Sentry 이슈 제목 → {fixable / noise / needs-human, 판정 근거, 수정 요약}
  brief  : 대시보드 현황 JSON → 운영 브리핑 3줄 + 권고 1줄
  traffic: 트래픽 지표·레버 상태 → 엘프가 당길 레버 하나와 근거 (트래픽 대응 에이전트, 경보 때만 1회)

대시보드가 멈추지 않도록 호출은 백그라운드 스레드에서 하고, 결과는 World 가 매 프레임 poll() 로 가져간다.
OPENAI_API_KEY 가 없거나 호출이 실패하면 대시보드는 예전 키워드·mock 판정으로 그대로 돈다.
표준 라이브러리만 쓴다 (의존성 없음).

환경변수 (레포의 .env 에 적어도 된다 — git 에 안 올라감)
  OPENAI_API_KEY     필수 (없으면 AI 꺼짐)
  XMAS_AI=off        키가 있어도 끄기
  XMAS_AI_MODEL      모델 (기본 gpt-5-mini)
  XMAS_AI_EFFORT     reasoning effort (기본 low, 빈 값이면 보내지 않음)
  OPENAI_BASE_URL    API 주소 (기본 https://api.openai.com/v1)
  XMAS_AI_RECORD     경로를 주면 실제 트리아지 결과를 녹화 (python3 xmas_market.py --replay 로 재생)
"""
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "gpt-5-mini"

KINDS = ["fixable", "noise", "needs-human"]
INTENTS = ["catch", "noise", "human", "rest", "go", "report", "answer", "unknown"]

ORDER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["soldier", "intent", "issue_id", "reply"],
    "properties": {
        "soldier": {"type": "string", "enum": ["1", "2", "3", "all", "none"]},
        "intent": {"type": "string", "enum": INTENTS},
        "issue_id": {"type": "string"},
        "reply": {"type": "string"},
    },
}
TRIAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "reason", "fix"],
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "reason": {"type": "string"},
        "fix": {"type": "string"},
    },
}
BRIEF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "advice"],
    "properties": {
        "summary": {"type": "array", "items": {"type": "string"}},
        "advice": {"type": "string"},
    },
}

REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "causes", "human_todo", "next_steps"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "array", "items": {"type": "string"}},
        "causes": {"type": "array", "items": {"type": "string"}},
        "human_todo": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
}

LEVERS = ["cache", "capacity", "waitroom", "human", "none"]
TRAFFIC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lever", "reason", "say"],
    "properties": {
        "lever": {"type": "string", "enum": LEVERS},
        "reason": {"type": "string"},
        "say": {"type": "string"},
    },
}

ORDER_PROMPT = """너는 크리스마스 마켓 운영 대시보드의 호두까기 병정 지휘 통역이다.
병정은 Sentry 이슈(쥐)를 처리하는 sentry-bot 서브 에이전트 셋이다: 1 루돌프, 2 진저, 3 호두.
사용자의 한국어 명령 한 줄과 현재 상황(JSON)을 받아 어떤 병정이 무엇을 할지 정한다.

soldier: 명령을 받을 병정 "1"/"2"/"3", 모두에게면 "all", 병정을 특정하지 않았으면 "all".
  병정과 무관한 말이면 "none".
intent:
  catch  = 그 쥐(이슈)를 잡아 고쳐라 / 먼저 처리해라
  noise  = 그 쥐는 noise 다, 쫓아내라
  human  = 그 쥐는 사람(담당자)에게 넘겨라
  rest   = 복귀·대기·쉬어라
  go     = 출동·재개해라
  report = 지금 뭐 하는지 보고해라
  answer = 질문이다 ("가장 큰 에러가 뭐였어?", "몇 마리 잡았어?", "PR 뭐 올렸어?" 등). 상황 JSON 으로 답한다
  unknown = 위에 해당하지 않음 (인사·칭찬·잡담 등)
issue_id: catch/noise/human 이면 상황의 mice 중 하나의 id 를 그대로 쓴다.
  "제일 오래된 쥐", "검색 쪽 503 에러", "null 에러 난 거"처럼 묘사로 가리키면 mice 의 title·age_s·state 로 골라낸다.
  가리키는 쥐가 없으면 "".
reply: 병정이 할 짧은 대답(한국어, 씩씩한 병정 말투). 보통 40자 이내.
  intent 가 answer 면 상황의 history(처리한 쥐: outcome fixed=고침 / noise=쫓아냄 / human=담당자에게 넘김,
  pr 은 실제로 연 GitHub PR 번호)·stats·mice 를 근거로 사람에게 말하듯 1~2문장, 80자 이내로 답한다.
  - 필드 이름(history, stats, fixed, mice 등)이나 JSON 을 그대로 읽지 말고 우리말로 풀어 쓴다
  - 에러는 제목을 통째로 읽지 말고 무엇이 문제인지 짧게 (예: "결제 확인 API 500 에러")
  - 이슈 ID·PR 번호는 필요할 때 하나만 붙인다 (예: "WEB-F", "PR #2")
  - "알겠습니다!" 같은 복명복창 없이 바로 답한다
  - 상황에 없는 사실은 지어내지 말고 "기록에 없습니다"라고 한다
  "가장 큰/심각한 에러"는 사람에게 넘긴 것(human)을 먼저, 없으면 가장 많이 나온 에러를 말한다.
"""

TRIAGE_PROMPT = """너는 sentry-bot 의 트리아지 담당이다. 프론트엔드 서비스의 Sentry 이슈 하나를 보고 판정한다.
kind:
  fixable     = 우리 레포 코드의 버그라서 코드 수정 MR 로 고칠 수 있다 (타입 오류, null 접근, 미지원 API 사용 등)
  noise       = 레포 밖 출처라 고칠 게 없다 (브라우저 확장, 광고·서드파티 스크립트, 배포 직후 청크 로딩 실패 같은 일시적 네트워크 오류)
  needs-human = 백엔드 장애·보안·인프라·정책 판단이 필요해 사람이 봐야 한다 (5xx API 오류, 보안 정책 위반, 런타임 변조 등)
reason: 판정 근거 (한국어, 35자 이내)
fix: fixable 이면 MR 제목으로 쓸 수정 요약 (한국어, 30자 이내). 아니면 "".
"""

REPORT_PROMPT = """너는 장애 대응이 끝난 뒤 사후 보고서(포스트모템) 초안을 쓰는 운영 담당이다.
입력 JSON 은 대시보드가 기록한 사실뿐이다: history(처리한 Sentry 이슈: outcome fixed/noise/human, 판정 근거 note, PR),
prs(병정이 연 PR 과 Codex 리뷰 근거), elf_actions(트래픽 대응 레버 조치와 결과), stats, traffic.
입력에 없는 사실·숫자는 지어내지 않는다. 숫자는 입력에서 그대로 옮긴다.
title: 보고서 제목 (한국어, 40자 이내)
summary: 무슨 일이 있었고 어떻게 대응했는지 2~4줄 (각 60자 이내)
causes: 이슈를 원인별로 묶은 2~4줄 (예: "프론트 null 접근 3건 - PR 로 수정")
human_todo: 사람이 해야 할 일 (needs-human 이슈, 열린 PR 리뷰·머지, 번호표 승인 등). 없으면 빈 배열
next_steps: 재발 방지·개선 제안 2~3줄
"""

BRIEF_PROMPT = """너는 온콜 운영자를 돕는 브리핑 담당이다. 대시보드 현황 JSON 을 보고 지금 상황을 짚어 준다.
traffic.rps/peak_rps/crowd 는 트래픽, mice 는 미해결 Sentry 이슈(state: roam 배회, held 병정 처리 중, caged 사람 대기),
soldiers 는 sentry-bot 서브 에이전트, history 는 처리 결과, stats 는 누적 통계다.
summary: 숫자를 넣어 상황을 2~3줄로 (각 줄 45자 이내, 한국어)
advice: 운영자가 지금 하면 좋을 일 한 줄 (45자 이내). 병정 명령이 필요하면 예: "진저에게 WEB-2B1 먼저 잡게 하세요"
"""

TRAFFIC_PROMPT = """너는 크리스마스 마켓 상점의 엘프 점원이자 트래픽 대응 에이전트다. 웹 서버에 요청이 몰려 경보가 울렸다.
규칙이 방금 레버 하나(just_pulled)를 당겼다. 상황 JSON 을 보고, 그것으로 부족하면 다음에 당길 레버를 하나 고른다.
  load: rps ÷ 처리 가능량 추정 (1 이상이면 넘침), rps·peak_rps, error_pct(5xx %), latency_ms(평균 응답),
  waiting(슬롯을 기다리는 요청 수), levers(지금 레버 상태), tried(이번 경보에서 이미 당긴 레버), api_errors(5xx 가 과부하 때문이 아닐 수 있음)
lever:
  (levers 는 just_pulled 를 당긴 뒤의 상태다. just_pulled 만으로 충분해 보이면 none)
  cache     = 느린 API 응답 캐시를 켠다. 응답 시간이 높고 캐시가 꺼져 있을 때 먼저. 위험 낮음
  capacity  = 처리 슬롯(계산대)을 늘린다. 캐시 뒤에도 넘치거나 waiting 이 쌓일 때. 최대 8
  waitroom  = 번호표: 넘치는 요청을 기다리게 두지 않고 429 로 돌려보낸다. 다른 레버로 안 될 때 최후 수단 (사람 승인 필요)
  human     = 레버로 해결할 문제가 아니다 (부하는 낮은데 5xx, 특정 API 장애 등). 담당자를 부른다
  none      = 조치가 필요 없다 (일시적인 출렁임)
이미 켠 레버(levers)·tried 에 있는 레버는 다시 고르지 않는다 (capacity 는 최대 8 전까지 다시 가능).
reason: 판단 근거 (한국어, 숫자 포함, 40자 이내)
say: 엘프가 할 짧은 말 (한국어, 30자 이내)
"""


def load_dotenv(path=None):
    """레포의 .env (KEY=VALUE 줄)를 환경변수로 읽는다. 이미 있는 환경변수는 덮지 않는다. .env 는 git 에 안 올라간다."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.replace("export ", "").strip()
                os.environ.setdefault(k, v.strip().strip("'\""))
    except OSError:
        pass


load_dotenv()


def available():
    """키가 있고 XMAS_AI=off 가 아니면 True."""
    if os.environ.get("XMAS_AI", "on").strip().lower() in ("off", "0", "false", "no"):
        return False
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _str(v):
    return v.strip() if isinstance(v, str) else ""


def _choice(v, allowed, field):
    if v not in allowed:
        raise ValueError("%s 값이 이상함: %r" % (field, v))
    return v


def extract_text(resp):
    """Responses API 응답 JSON 에서 모델이 쓴 글을 꺼낸다."""
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"]
    parts = []
    for item in resp.get("output") or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if c.get("type") == "output_text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "refusal":
                raise RuntimeError("refusal: %s" % c.get("refusal", "")[:80])
    if not parts:
        raise RuntimeError("응답에 output_text 가 없음 (status=%s)" % resp.get("status"))
    return "".join(parts)


class Brain:
    """OpenAI 호출을 백그라운드에서 돌리는 작업 큐. transport 를 바꿔 끼우면 테스트에서 네트워크 없이 쓴다."""

    def __init__(self, key=None, model=None, effort=None, timeout=25.0, transport=None, workers=2):
        self.key = key or os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = model or os.environ.get("XMAS_AI_MODEL", "").strip() or DEFAULT_MODEL
        self.effort = os.environ.get("XMAS_AI_EFFORT", "low").strip() if effort is None else effort
        self.url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses"
        self.timeout = timeout
        self.transport = transport or self._post
        self.jobs, self.results = queue.Queue(), queue.Queue()
        self.calls = self.fails = 0
        self.lock = threading.Lock()
        self.last_error = None
        for _ in range(workers):
            threading.Thread(target=self._work, daemon=True).start()

    # ---- 큐
    def submit(self, kind, payload, ctx=None):
        self.jobs.put((kind, payload, ctx))

    def poll(self):
        """끝난 작업들: [(kind, ctx, result, error)]. 메인 스레드에서 매 프레임 부른다."""
        out = []
        while True:
            try:
                out.append(self.results.get_nowait())
            except queue.Empty:
                return out

    def _work(self):
        while True:
            kind, payload, ctx = self.jobs.get()
            try:
                res = getattr(self, kind)(payload)
                self._record(kind, payload, res)
                self.results.put((kind, ctx, res, None))
            except Exception as e:  # noqa: BLE001 — 어떤 실패든 대시보드는 계속 돈다
                with self.lock:
                    self.fails += 1
                self.last_error = "%s: %s" % (type(e).__name__, str(e)[:120])
                self.results.put((kind, ctx, None, e))

    def _record(self, kind, payload, res):
        """XMAS_AI_RECORD=경로 이면 트리아지 결과를 녹화한다 (--replay 로 키 없이 다시 보여 주기)."""
        path = os.environ.get("XMAS_AI_RECORD", "").strip()
        if not path or kind != "triage":
            return
        line = json.dumps({"kind": kind, "key": payload["title"], "model": self.model,
                           "at": time.strftime("%Y-%m-%d"), "result": res}, ensure_ascii=False)
        with self.lock, open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---- 작업 종류
    def order(self, p):
        user = "명령: %s\n\n상황:\n%s" % (p["text"], json.dumps(p["context"], ensure_ascii=False))
        r = self.ask(ORDER_PROMPT, user, "soldier_order", ORDER_SCHEMA)
        return {"soldier": _choice(r.get("soldier"), ("1", "2", "3", "all", "none"), "soldier"),
                "intent": _choice(r.get("intent"), INTENTS, "intent"),
                "issue_id": _str(r.get("issue_id")), "reply": _str(r.get("reply"))}

    def triage(self, p):
        user = "서비스: %s\n이슈 ID: %s\n제목: %s" % (p.get("service") or "-", p["id"], p["title"])
        r = self.ask(TRIAGE_PROMPT, user, "sentry_triage", TRIAGE_SCHEMA)
        kind = _choice(r.get("kind"), KINDS, "kind")
        return {"kind": kind, "reason": _str(r.get("reason")), "fix": _str(r.get("fix")) if kind == "fixable" else ""}

    def report(self, p):
        r = self.ask(REPORT_PROMPT, json.dumps(p, ensure_ascii=False), "postmortem", REPORT_SCHEMA)
        lines = lambda k: [_str(x) for x in r.get(k) or [] if _str(x)][:6]  # noqa: E731
        return {"title": _str(r.get("title")), "summary": lines("summary"), "causes": lines("causes"),
                "human_todo": lines("human_todo"), "next_steps": lines("next_steps")}

    def brief(self, p):
        r = self.ask(BRIEF_PROMPT, json.dumps(p, ensure_ascii=False), "ops_brief", BRIEF_SCHEMA)
        lines = [_str(x) for x in r.get("summary") or [] if _str(x)]
        if not lines:
            raise ValueError("브리핑이 비었음")
        return {"summary": lines[:3], "advice": _str(r.get("advice"))}

    def traffic(self, p):
        r = self.ask(TRAFFIC_PROMPT, json.dumps(p, ensure_ascii=False), "traffic_lever", TRAFFIC_SCHEMA, tries=1)  # 예산 아끼기: 재시도 없음
        return {"lever": _choice(r.get("lever"), LEVERS, "lever"), "reason": _str(r.get("reason")),
                "say": _str(r.get("say"))}

    # ---- OpenAI 호출
    def ask(self, instructions, user, name, schema, tries=3):
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": user,
            "text": {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
        }
        if self.effort:
            body["reasoning"] = {"effort": self.effort}
        with self.lock:
            self.calls += 1
        for attempt in range(tries):
            try:
                return json.loads(extract_text(self.transport(body)))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300] if e.fp is not None else ""
                if e.code == 400 and "reasoning" in body and "reasoning" in detail:
                    self.effort = ""  # reasoning 을 모르는 모델이면 빼고 다시 보낸다
                    body.pop("reasoning")
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError("HTTP %d: %s" % (e.code, detail[:160]))
            except (urllib.error.URLError, TimeoutError):
                if attempt < tries - 1:
                    time.sleep(1.0)
                    continue
                raise
        raise RuntimeError("재시도 초과")

    def _post(self, body):
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))


class ReplayBrain:
    """녹화된 실제 OpenAI 트리아지 결과를 다시 보여 준다 (API 키 없이 심사·시연용).

    같은 이슈 제목이면 녹화된 판정·근거·수정 요약을 그대로 쓴다. 녹화에 없는 이슈와 명령 해석·브리핑은
    실패로 돌려 대시보드가 키워드·mock 으로 처리하게 한다. 새로 지어내는 응답은 없다.
    """
    replay = True

    def __init__(self, path):
        self.table, models, dates = {}, set(), set()
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("kind") == "triage":
                    self.table[r["key"]] = r["result"]
                    models.add(r.get("model", "?"))
                    dates.add(r.get("at", "?"))
        if not self.table:
            raise ValueError("녹화에 트리아지 결과가 없다: %s" % path)
        self.model = "%s, %s" % ("/".join(sorted(models)), max(dates))
        self.calls = self.fails = 0
        self.last_error = None
        self.results = queue.Queue()

    def submit(self, kind, payload, ctx=None):
        res = self.table.get(payload.get("title")) if kind == "triage" else None
        if res is None:
            self.fails += 1
            self.results.put((kind, ctx, None, LookupError("녹화에 없음")))
        else:
            self.calls += 1
            self.results.put((kind, ctx, dict(res), None))

    def poll(self):
        out = []
        while True:
            try:
                out.append(self.results.get_nowait())
            except queue.Empty:
                return out
