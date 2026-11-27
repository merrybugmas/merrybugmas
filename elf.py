# -*- coding: utf-8 -*-
"""엘프 점원 — 트래픽 대응 에이전트. 상점 앞에 손님(요청)이 몰리면 레버를 당겨 해소한다.

  감지 → 판단 → 조치 → 확인 → (가라앉으면) 원복

레버 (demo-web 서버의 /admin/levers, mock 이면 대시보드 안에서 흉내)
  cache     🎁 느린 API 응답 캐시           자동
  capacity  🧾 처리 슬롯(계산대) +2, 최대 8  자동
  waitroom  🎫 번호표: 넘치는 요청은 429      사람 승인 필요
  human     📞 레버로 안 되는 문제 → 담당자 호출

판단은 규칙으로 하고, OpenAI 가 켜져 있으면 경보마다 첫 판단만 AI 에게 묻는다 (재시도 없이 HTTP 1회).
AI 호출은 대시보드 실행 한 번에 XMAS_AI_TRAFFIC_MAX 번(기본 5)까지만 (판·서비스를 바꿔도 이어서 센다) — 넘으면 규칙으로만 움직인다.
"""
import os

LEVER_WORD = {"cache": "🎁 캐시", "capacity": "🧾 계산대", "waitroom": "🎫 번호표", "human": "📞 담당자 호출",
              "none": "지켜보기"}
STATE_WORD = {"idle": "감시 중", "think": "🧠 판단 중", "ask": "승인 대기!", "watch": "효과 확인", "flag": "담당자 호출",
              "cool": "대기", "off": "자동 꺼짐"}
BASE = {"cache": False, "capacity": 2, "waitroom": False}
MAX_CAP = 8
ALARM_HOLD = 2.0     # 경보가 이만큼 이어져야 움직인다 (순간 출렁임 무시)
WATCH_SEC = 10.0     # 레버를 당긴 뒤 효과를 지켜보는 시간
CALM_SEC = 15.0      # 이만큼 잠잠하면 켰던 레버를 하나씩 원복
THINK_TIMEOUT = 12.0  # AI 답을 기다리는 최대 시간 (넘으면 규칙으로)
FLAG_COOL = 30.0     # 담당자를 부른 뒤 다시 움직이기까지

APPROVE_SHORT = ("ㅇㅋ", "오케이", "ok", "yes", "좋아", "그래", "허가")  # 이 말 하나만 했을 때만 승인 ("보고해줘" 같은 말로 켜지지 않게)
REJECT_SHORT = ("취소", "no", "안돼", "안 돼", "하지마", "하지 마")
ASK_LIMIT = 60.0  # 승인을 이만큼 못 받고 계속 밀리면 담당자를 부른다
ON = ("켜", "on", "올려", "열어")
OFF = ("꺼", "off", "내려", "닫아")


def ai_budget():
    try:
        return max(0, int(os.environ.get("XMAS_AI_TRAFFIC_MAX", "5").strip() or 5))
    except ValueError:
        return 5


class Elf:
    def __init__(self, client=None, ai_max=None):
        self.client = client  # traffic_live.LeverClient (실서버) 또는 None (mock: 대시보드 안에서 바로 반영)
        self.levers, self.base = dict(BASE), dict(BASE)
        self.synced = False  # 실서버 레버 상태를 한 번이라도 읽었는지 (처음 값이 원복 기준)
        self.sending = 0  # 서버에 보내고 답을 못 받은 변경 수 (그동안은 /metrics 값으로 덮지 않는다)
        self.expect, self.expect_t = None, 0.0  # 서버가 확인해 준 레버 값: /metrics 가 이 값이 될 때까지(최대 5초) 덮지 않는다
        self.state, self.timer = "idle", 0.0
        self.alarm_t = self.calm_t = 0.0
        self.tried, self.pending, self.before = [], None, None
        self.actions = []  # 조치 기록: {t, lever, by, reason, result}
        self.auto = True
        self.ai_used, self.ai_max, self.ai_note = 0, ai_budget() if ai_max is None else ai_max, False
        self.speech, self.speech_t = "", 0.0
        self.last = {}  # 마지막으로 잰 압력

    # ---- 말
    def say(self, world, text, color=120, cat="traffic"):
        self.speech, self.speech_t = text, max(4.0, 0.15 * len(text))
        world.log("🧝 엘프: " + text, color, cat)

    # ---- 압력 재기
    def relief(self):
        """처리 능력 배수: 계산대 수 ÷ 기본 2 × 캐시 1.2 (mock 부하와 줄이 빠지는 속도에 쓴다)."""
        return (self.levers["capacity"] / 2.0) * (1.2 if self.levers["cache"] else 1.0)

    def pressure(self, world):
        """지금 얼마나 밀리는지. 실연동이면 서버 지표, mock 이면 rps ÷ 처리 능력으로 추정한 값."""
        ratio = world.ratio
        if world.traffic is not None:
            m = world.web_metrics()
            err, lat = m.get("error_pct"), m.get("latency_ms")
            waiting = (world.traffic.levers or {}).get("waiting", 0)
            e, l = err or 0.0, lat or 0.0
            alarm = e >= 5 or l >= 300 or waiting >= 2 * self.levers["capacity"]
            # 부하는 안 밀리는데(응답 빠르고 대기 없음) 5xx 만 높으면 과부하가 아니라 API 장애다
            api_errors = e >= 5 and l < 150 and waiting == 0
            calm = e < 1 and l < 150 and ratio < 0.6
            p = {"mock": False, "load": round(ratio, 2), "error_pct": err, "latency_ms": lat, "waiting": waiting,
                 "api_errors": api_errors}
        else:
            load = ratio / self.relief()
            err = 0.0 if self.levers["waitroom"] else round(min(60.0, max(0.0, load - 0.9) * 60), 1)
            lat = round(min(3000, 40 + 700 * max(0.0, load - 0.7)))
            alarm, calm = load >= 0.9, ratio < 0.6
            p = {"mock": True, "load": round(load, 2), "error_pct": err, "latency_ms": lat, "waiting": 0,
                 "api_errors": False}
        why = []
        if (p["error_pct"] or 0) >= 5:
            why.append("5xx %.1f%%" % p["error_pct"])
        if (p["latency_ms"] or 0) >= 300:
            why.append("응답 %dms" % p["latency_ms"])
        if p["mock"] and alarm and not why:
            why.append("부하 %.2f" % p["load"])
        if p["waiting"] and p["waiting"] >= 2 * self.levers["capacity"]:
            why.append("대기 %d건" % p["waiting"])
        p.update(alarm=alarm, calm=calm, why=", ".join(why), rps=int(world.rps), peak_rps=int(world.peak))
        return p

    # ---- 매 프레임
    def update(self, world, dt):
        self.speech_t -= dt
        self.pump_client(world)
        if world.traffic is not None and world.traffic.levers and not self.sending:
            lv = {k: world.traffic.levers[k] for k in BASE}
            if self.expect is not None and lv != self.expect and world.t - self.expect_t < 5.0:
                lv = self.levers  # 변경 전에 읽은 /metrics: 아직 믿지 않는다
            else:
                self.expect = None
            self.levers = dict(lv)
            if not self.synced:  # 서버가 처음 알려 준 값이 원복 기준
                self.synced, self.base = True, dict(self.levers)
        p = self.last = self.pressure(world)
        self.alarm_t = self.alarm_t + dt if p["alarm"] else 0.0
        self.calm_t = self.calm_t + dt if p["calm"] else 0.0
        if not self.auto:
            return
        self.timer -= dt
        if self.state == "idle":
            if self.alarm_t >= ALARM_HOLD:
                self.tried, self.before = [], p
                world.log("🚨 트래픽 경보: %s (rps %d)" % (p["why"] or "밀림", p["rps"]), 196, "all")
                self.decide(world, p, first=True)
            elif self.calm_t >= CALM_SEC and self.levers != self.base:
                self.restore_one(world)
                self.calm_t = CALM_SEC - 5.0  # 다음 원복은 5초 뒤
        elif self.state == "think" and self.timer <= 0:
            world.log("🧠 엘프 AI 답이 늦어 규칙으로 판단", 245, "traffic")
            self.act(world, self.rule(p), "AI 응답 지연 - 규칙", "규칙")
        elif self.state == "ask" and not p["alarm"] and self.alarm_t == 0 and self.timer <= 0:
            self.pending, self.state = None, "idle"
            if not self.levers["waitroom"]:
                self.say(world, "경보가 풀렸어요. 번호표는 안 켤게요", 245)
        elif self.state == "ask" and self.timer <= -ASK_LIMIT:
            self.pending = None
            self.act(world, "human", "번호표 승인을 %d초 못 받음, %s" % (ASK_LIMIT, p["why"] or "계속 밀림"), "규칙")
        elif self.state == "watch" and self.timer <= 0:
            self.check(world, p)
        elif self.state in ("flag", "cool") and self.timer <= 0:
            self.state, self.alarm_t = "idle", 0.0

    def check(self, world, p):
        """레버를 당기고 WATCH_SEC 뒤: 나아졌는지 숫자로 비교한다."""
        b = self.before or p
        diff = "5xx %.1f→%.1f%% · 응답 %d→%dms" % (b["error_pct"] or 0, p["error_pct"] or 0,
                                                 b["latency_ms"] or 0, p["latency_ms"] or 0)
        if p["mock"]:
            diff += " · 부하 %.2f→%.2f" % (b["load"], p["load"])
        if self.actions:
            self.actions[-1]["result"] = ("부족: " if p["alarm"] else "효과 있음: ") + diff
        if p["alarm"]:
            world.log("🧝 아직 밀림 (%s) - 다음 조치" % diff, 214, "all")
            self.decide(world, p)
        else:
            self.state, self.alarm_t = "idle", 0.0
            self.say(world, "해소! " + diff, 46, "all")

    # ---- 판단
    def can(self, lever):
        if lever == "cache":
            return not self.levers["cache"]
        if lever == "capacity":
            return self.levers["capacity"] < MAX_CAP
        if lever == "waitroom":
            return not self.levers["waitroom"]
        return lever in ("human", "none")

    def rule(self, p):
        """AI 없이 고르는 순서: (API 장애면 담당자) → 캐시 → 계산대 → 번호표 → 담당자."""
        if p.get("api_errors"):
            return "human"
        for lever in ("cache", "capacity", "waitroom"):
            if self.can(lever) and (lever == "capacity" or lever not in self.tried):
                return lever
        return "human"

    def ai_ready(self, world):
        return world.ai is not None and hasattr(world.ai, "ask") and not getattr(world.ai, "replay", False)

    def decide(self, world, p, first=False):
        """경보 첫 판단만 AI 에게 묻는다 (경보 한 번에 최대 1회). 이어지는 판단은 규칙."""
        if first and self.ai_ready(world) and self.ai_used < self.ai_max:
            self.ai_used += 1
            payload = {k: p[k] for k in ("load", "rps", "peak_rps", "error_pct", "latency_ms", "waiting", "api_errors")}
            payload.update(levers=dict(self.levers), tried=list(self.tried), max_capacity=MAX_CAP,
                           source="mock 추정" if p["mock"] else "실서버 /metrics")
            world.ai.submit("traffic", payload, (world.stage_gen, "elf"))
            self.state, self.timer = "think", THINK_TIMEOUT
            world.log("🧠 엘프가 AI 에게 묻는 중 (이번 실행 %d/%d회)" % (self.ai_used, self.ai_max), 141, "traffic")
            return
        if first and self.ai_ready(world) and not self.ai_note:
            self.ai_note = True
            world.log("🧠 엘프 AI 예산 %d회 다 씀 - 이제 규칙으로만 판단" % self.ai_max, 245, "all")
        self.act(world, self.rule(p), p["why"] or "밀림", "규칙")

    def on_ai(self, world, res, err):
        if self.state != "think":
            return  # 이미 규칙으로 처리했다
        p = self.last or self.pressure(world)
        if err is not None:
            world.log("🧠 엘프 AI 실패 (%s) - 규칙으로" % type(err).__name__, 245, "traffic")
            return self.act(world, self.rule(p), p["why"] or "밀림", "규칙")
        lever = res["lever"]
        if not self.can(lever) or (lever in self.tried and lever != "capacity"):
            world.log("🧠 AI 가 고른 %s 는 이미 썼음 - 규칙으로" % LEVER_WORD.get(lever, lever), 245, "traffic")
            return self.act(world, self.rule(p), res["reason"] or p["why"], "규칙")
        self.act(world, lever, res["reason"] or p["why"], "AI", res.get("say"))

    # ---- 조치
    def act(self, world, lever, reason, by, said=None):
        self.actions.append({"t": round(world.t, 1), "lever": lever, "by": by, "reason": reason[:60], "result": None})
        self.actions = self.actions[-30:]
        tag = "🧠" if by == "AI" else "📏"
        if lever in BASE and not self.has_levers(world):
            self.state, self.timer = "flag", FLAG_COOL
            self.actions[-1].update(lever="human", result="레버 없음 - 담당자 호출")
            self.say(world, "%s 이 필요한데 서버에 레버가 없어요. 담당자를 부릅니다! (%s)"
                     % (LEVER_WORD[lever], reason), 203, "all")
            return
        if lever == "waitroom":
            self.pending, self.state, self.timer = "waitroom", "ask", 5.0
            self.actions[-1]["result"] = "승인 대기"
            self.say(world, "%s번호표를 켤까요? 넘치는 손님은 429로 돌려보내요 (%s) → 승인: tell 엘프 승인"
                     % (said + " " if said else "", reason), 226, "all")
            return
        if lever == "human":
            self.state, self.timer = "flag", FLAG_COOL
            self.actions[-1]["result"] = "담당자 호출"
            self.say(world, "레버로 안 풀려요. 담당자를 부릅니다! (%s)" % reason, 203, "all")
            return
        if lever == "none":
            self.state, self.timer = "cool", 10.0
            world.log("%s 엘프: 지켜보기 (%s)" % (tag, reason), 245, "traffic")
            return
        self.tried.append(lever)
        self.pull(world, lever, by)
        self.say(world, "%s %s%s (%s)" % (tag, self.lever_text(lever), " · " + said if said else "", reason), 120, "all")
        self.state, self.timer, self.before = "watch", WATCH_SEC, self.last or self.before

    def lever_text(self, lever):
        if lever == "capacity":
            return "계산대 %d개로 늘림" % self.levers["capacity"]
        return {"cache": "캐시 켬", "waitroom": "번호표 켬"}.get(lever, lever)

    def pull(self, world, lever, by="사람", value=None):
        """레버 하나를 바꾼다 (mock 은 즉시, 실서버는 백그라운드로 POST)."""
        if lever == "capacity":
            value = min(MAX_CAP, self.levers["capacity"] + 2) if value is None else max(1, min(MAX_CAP, value))
        elif value is None:
            value = True
        return self.send(world, {lever: value})

    def has_levers(self, world):
        """mock 이면 늘 있고, 실서버면 /admin/levers 를 붙였을 때만 (없는 레버를 당긴 척하지 않는다)."""
        return world.traffic is None or self.client is not None

    def send(self, world, changes):
        if not self.has_levers(world):
            world.log("🧝 서버에 레버가 없어 바꾸지 못함 (demo-web 의 /admin/levers 필요)", 245, "all")
            return False
        self.levers.update(changes)
        if self.client is not None:
            self.sending += 1
            self.client.submit(changes, world.stage_gen)

    def pump_client(self, world):
        if self.client is None:
            return
        for _gen, res, err in self.client.poll():
            self.sending = max(0, self.sending - 1)
            if err is not None:
                world.log("🧝 서버 레버 실패 (%s: %s)" % (type(err).__name__, str(err)[:40]), 196, "all")
                self.state, self.timer = "cool", 10.0
            elif isinstance(res, dict) and "capacity" in res:
                self.levers = {k: res[k] for k in BASE}
                self.expect, self.expect_t = dict(self.levers), world.t
                world.log("🔧 서버 반영: 캐시 %s · 계산대 %d · 번호표 %s" % (
                    "ON" if res["cache"] else "OFF", res["capacity"], "ON" if res["waitroom"] else "OFF"), 245, "traffic")

    def restore_one(self, world):
        """잠잠해지면 번호표 → 계산대 → 캐시 순으로 하나씩 되돌린다."""
        for lever in ("waitroom", "capacity", "cache"):
            if self.levers[lever] != self.base[lever]:
                self.send(world, {lever: self.base[lever]})
                self.actions.append({"t": round(world.t, 1), "lever": lever, "by": "원복", "reason": "잠잠해짐",
                                     "result": "원래대로"})
                self.say(world, "🧹 손님이 줄었어요. %s 원래대로" % LEVER_WORD[lever], 245)
                return

    # ---- 채팅 명령 (키워드만: OpenAI 를 쓰지 않는다)
    def command(self, world, text):
        low = text.strip().lower()
        has = lambda words: any(w in low for w in words)  # noqa: E731
        bare = low.strip(" .!~?")
        if self.pending and ("승인" in low or bare in APPROVE_SHORT):
            lever, self.pending = self.pending, None
            self.tried.append(lever)
            if self.pull(world, lever) is False:
                return
            if self.actions:
                self.actions[-1]["result"] = "사람 승인"
            self.state, self.timer, self.before = "watch", WATCH_SEC, self.last or self.before
            return self.say(world, "승인 감사합니다! %s 켰어요" % LEVER_WORD[lever], 46, "all")
        if self.pending and ("거절" in low or bare in REJECT_SHORT):
            self.pending, self.state, self.timer = None, "cool", 15.0
            return self.say(world, "알겠어요, 번호표는 안 켭니다", 245)
        if has(("자동",)) and has(OFF + ("멈춰", "그만")):
            self.auto, self.state, self.pending = False, "off", None
            return self.say(world, "자동 대응을 멈춥니다. 레버는 말로만 움직일게요", 245)
        if has(("자동", "재개", "다시 해")) and not has(OFF):
            self.auto, self.state = True, "idle"
            return self.say(world, "자동 대응 다시 시작!", 120)
        if has(("원복", "되돌", "원래대로", "reset")):
            if self.send(world, dict(self.base)) is False:
                return
            return self.say(world, "레버를 모두 원래대로 돌렸어요", 245)
        num = [int(t) for t in low.replace("개", " ").split() if t.isdecimal() and t.isascii()]
        if has(("계산대", "용량", "슬롯", "capacity", "증설")):
            cap = self.levers["capacity"]
            new = num[0] if num else (cap - 2 if has(("줄여", "빼", "감소")) else cap + 2)
            if self.pull(world, "capacity", value=new) is False:
                return
            return self.say(world, "계산대 %d → %d개" % (cap, self.levers["capacity"]), 120)
        for lever, words in (("cache", ("캐시", "cache", "포장")), ("waitroom", ("번호표", "대기실", "waitroom"))):
            if has(words) and (has(ON) or has(OFF)):
                on = not has(OFF)
                if self.send(world, {lever: on}) is False:
                    return
                if lever == "waitroom" and self.pending == "waitroom":
                    self.pending = None
                    if on:  # 사람이 직접 켰으면 승인한 것과 같다: 효과를 지켜본다
                        self.tried.append("waitroom")
                        self.state, self.timer, self.before = "watch", WATCH_SEC, self.last or self.before
                return self.say(world, "%s %s (사람 지시)" % (LEVER_WORD[lever], "켰어요" if on else "껐어요"), 120)
        if has(("보고", "상황", "뭐 해", "뭐해", "어때")):
            return self.say(world, self.report())
        self.say(world, "예: 캐시 켜 / 계산대 6 / 번호표 꺼 / 승인 / 원복 / 보고 / 자동 꺼", 245)

    def report(self):
        p = self.last or {}
        lv = self.levers
        s = "%s · 캐시 %s · 계산대 %d · 번호표 %s" % (STATE_WORD.get(self.state, self.state), "ON" if lv["cache"] else "OFF",
                                                lv["capacity"], "ON" if lv["waitroom"] else "OFF")
        if p:
            s += " · 5xx %.1f%% · 응답 %dms" % (p.get("error_pct") or 0, p.get("latency_ms") or 0)
        done = [a for a in self.actions if a["by"] in ("AI", "규칙")]
        return s + " · 조치 %d건" % len(done)

    def snapshot(self):
        p = self.last or {}
        return {"state": self.state if self.auto else "off", "auto": self.auto, "levers": dict(self.levers),
                "base": dict(self.base), "pending_approval": self.pending,
                "pressure": {k: p.get(k) for k in ("load", "error_pct", "latency_ms", "waiting", "alarm", "mock")},
                "ai": {"used": self.ai_used, "max": self.ai_max},
                "actions": [dict(a) for a in self.actions[-8:]]}
