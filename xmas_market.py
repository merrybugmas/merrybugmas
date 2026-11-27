#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ops Christmas Market — 터미널 메타버스 대시보드 PoC (mock 데이터, 픽셀아트)

  시작하면 타이틀(MERRY BUGMAS) → 메뉴(서비스·시나리오 고르기) → 스테이지.
  트래픽(Grafana 자리)      → 상점 앞 대기열. 트래픽이 오르면 줄이 길어진다.
  Sentry 미해결 이슈        → 호두까기 인형의 쥐. 이슈 하나 = 쥐 한 마리.
  sentry-bot 에이전트       → 호두까기 병정. triage → fix → review → MR 로 쥐를 처치한다.

화면은 반블록 문자(▀)로 그린다. 한 칸 = 위아래 픽셀 2개라 세로 해상도가 두 배다.
지금은 MockSource 가 가짜 데이터를 만든다. 실연동은 MockSource 의 rps()/poll_issues()만
Grafana·Sentry 호출로 바꾸면 된다. 의존성 없음(Python 3.8+ 표준 라이브러리만).

  python3 xmas_market.py              # 실행 (80x24 이상, 256색 터미널)
  python3 xmas_market.py --snapshot 60 --size 100x30   # 60초 시뮬레이션 뒤 한 프레임 출력
"""
import argparse
import math
import os
import random
import re
import select
import sys
import time
import unicodedata

try:
    import ai  # 병정의 두뇌 (OpenAI API). 키가 없으면 키워드·mock 판정으로 돈다
except ImportError:  # pragma: no cover
    ai = None
try:
    import sentry_live  # Sentry 실연동 (읽기 전용, --live)
except ImportError:  # pragma: no cover
    sentry_live = None

FPS = 12
PEAK_RPS = 3000
LOG_H = 6  # 로그 영역 높이(테두리 1 + 본문 5)
R = random.Random()

# ------------------------------------------------------------------ 출력 유틸


def cw(c):
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in "WF" else 1


def sgr(fg, bg, bold=False):
    return "\x1b[0;%s38;5;%d;48;5;%dm" % ("1;" if bold else "", fg, bg)


def seg_line(segs, w, bg):
    """[(text, fg[, bold]), ...] 를 화면 폭 w 에 맞춘 한 줄로."""
    out, n = [], 0
    for seg in segs:
        text, fg = seg[0], seg[1]
        out.append(sgr(fg, seg[3] if len(seg) > 3 else bg, len(seg) > 2 and seg[2]))
        for c in text:
            k = cw(c)
            if n + k > w:
                break
            out.append(c)
            n += k
    out.append(sgr(7, bg) + " " * (w - n) + "\x1b[0m")
    return "".join(out)


def seg_width(segs):
    return sum(cw(c) for s in segs for c in s[0])


def dw(s):
    return sum(cw(c) for c in s)


def pad(s, n):
    return s + " " * max(0, n - dw(s))


def fit_text(s, n):
    """표시 폭 n 으로 자르고(…) 모자라면 공백으로 채운다."""
    if dw(s) <= n:
        return pad(s, n)
    out = ""
    for c in s:
        if dw(out + c) > n - 1:
            break
        out += c
    return pad(out + "…", n)


def fit(segs, n):
    """segs 를 표시 폭 n 으로 자르고 남는 칸은 공백으로 채운다."""
    out, used = [], 0
    for seg in segs:
        text, keep = seg[0], ""
        for c in text:
            if used + cw(c) > n:
                break
            keep += c
            used += cw(c)
        if keep:
            out.append((keep,) + tuple(seg[1:]))
        if used >= n:
            break
    if used < n:
        out.append((" " * (n - used), 7))
    return out


ISSUE_HEADER = " " + pad("ID", 12) + pad("판정", 13) + pad("상태", 15) + "Sentry 제목"
KIND_COL = {"fixable": 46, "noise": 245, "needs-human": 226}


def put_segs(cv, x, row, segs, bg):
    for seg in segs:
        cv.put(x, row, seg[0], seg[1], bg, len(seg) > 2 and seg[2])
        x += dw(seg[0])


class PixelCanvas:
    """w x (2*h) 픽셀 버퍼 + 칸 단위 글자 오버레이. 한 칸 = ▀ (윗픽셀=fg, 아랫픽셀=bg)."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.px = [[16] * w for _ in range(2 * h)]
        self.text = {}

    def set(self, x, y, c):
        x, y = int(math.floor(x)), int(math.floor(y))
        if 0 <= x < self.w and 0 <= y < 2 * self.h:
            self.px[y][x] = c

    def get(self, x, y):
        if 0 <= x < self.w and 0 <= y < 2 * self.h:
            return self.px[y][x]
        return 16

    def rect(self, x, y, w, h, c):
        for yy in range(int(y), int(y) + h):
            for xx in range(int(x), int(x) + w):
                self.set(xx, yy, c)

    def blit(self, spr, x, y, pal, flip=False):
        x, y = int(round(x)), int(round(y))
        for dy, row in enumerate(spr):
            if flip:
                row = row[::-1]
            for dx, ch in enumerate(row):
                if ch != ".":
                    self.set(x + dx, y + dy, pal[ch])

    def put(self, x, row, text, fg, bg=None, bold=False):
        col = int(math.floor(x))
        for ch in text:
            k = cw(ch)
            if col + k > self.w:
                break
            if col >= 0 and 0 <= row < self.h:
                self.text[(col, row)] = (ch, fg, bg, bold)
                if k == 2:  # 넓은 글자(한글)는 다음 칸을 차지한다
                    self.text[(col + 1, row)] = ("", fg, bg, bold)
            col += k

    def lines(self):
        out = []
        for r in range(self.h):
            top, bot = self.px[2 * r], self.px[2 * r + 1]
            buf, last = [], None
            for x in range(self.w):
                t = self.text.get((x, r))
                if t:
                    ch, fg, bg, bold = t
                    if ch == "":
                        continue
                    st = (fg, bot[x] if bg is None else bg, bold)
                elif top[x] == bot[x]:
                    ch = " "
                    st = (last[0] if last else 7, top[x], False)
                else:
                    ch = "▀"
                    st = (top[x], bot[x], False)
                if st != last:
                    buf.append(sgr(*st))
                    last = st
                buf.append(ch)
            buf.append("\x1b[0m")
            out.append("".join(buf))
        return out


# ------------------------------------------------------------------ 스프라이트 (. = 투명)

# 쥐: 회색 몸통 + 짙은 음영 + 분홍 꼬리·발 (오른쪽 보기, 왼쪽은 좌우 반전)
RAT_PAL = {"g": 240, "d": 238, "p": 174, "r": 131, "e": 138, "k": 16, "x": 196}
RAT_WALK = [
    [
        ".....ggggg.e...",
        "...gggggggggkg.",
        "..gggddddgggggp",
        "pp.gdddddddg...",
        "p...r.r...r.r..",
    ],
    [
        ".....ggggg.e...",
        "...gggggggggkg.",
        "..gggddddgggggp",
        "pp.gdddddddg...",
        "p..r...rr...r..",
    ],
]
RAT_FRONT = [  # 들켰을 때 - 정면으로 굳는다
    "e.ggggg.e",
    "pggkgkggp",
    ".gggpggg.",
    ".dgdddgd.",
    "..ddddd..",
    ".rp...pr.",
]
RAT_DEAD = [row.replace("k", "x") for row in RAT_WALK[0][::-1]]
RAT_MINI = ["..ggg.", "pgggkg", "..r.r."]  # 뒤쪽 우리 안의 잡은 쥐 (6 x 3)  # 뒤집힌 쥐, 눈은 x

# 호두까기 병정 (7 x 14)
SOLDIER_PAL = {"K": 16, "Y": 220, "S": 223, "k": 16, "W": 255, "R": 160, "B": 25, "m": 52}
SOLDIER = [
    "..KKK..",
    ".KKKKK.",
    ".KYKYK.",
    ".YYYYY.",
    ".SSSSS.",
    ".SkSkS.",
    ".WSmSW.",
    ".WWWWW.",
    "YRRYRRY",
    "SRRYRRS",
    ".RRYRR.",
    ".BBBBB.",
    ".BB.BB.",
    ".kk.kk.",
]
SOLDIER_STEP = ["BB...BB", "kk...kk"]

# 방문객 (3 x 6) - H(모자/머리) C(외투) P(바지) 는 사람마다 다르다
VISITOR = [".H.", ".S.", "CCC", "CCC", "P.P", "k.k"]
VISITOR_STEP = [".P.", ".k."]

COATS = [196, 28, 33, 214, 129, 166, 37, 160, 25, 205, 99, 30]
HATS = [196, 196, 255, 94, 130, 16, 178, 33, 28]
PANTS = [24, 58, 236, 95, 60]
LIGHTS = [196, 46, 226, 51, 201, 208]

# ------------------------------------------------------------------ 타이틀 · 메뉴

# 5x5 점 글씨 - 꽉 찬 블록(█)만 써서 글꼴이 달라도 폭이 어긋나지 않는다
FONT = {
    "M": ["#...#", "##.##", "#.#.#", "#...#", "#...#"],
    "E": ["#####", "#....", "####.", "#....", "#####"],
    "R": ["####.", "#...#", "####.", "#..#.", "#...#"],
    "Y": ["#...#", ".#.#.", "..#..", "..#..", "..#.."],
    "B": ["####.", "#...#", "####.", "#...#", "####."],
    "U": ["#...#", "#...#", "#...#", "#...#", ".###."],
    "G": [".####", "#....", "#.###", "#...#", ".###."],
    "A": [".###.", "#...#", "#####", "#...#", "#...#"],
    "S": [".####", "#....", ".###.", "....#", "####."],
}
LOGO_COLORS = ([196, 196, 160, 160, 124], [46, 46, 34, 34, 28])  # MERRY 빨강 · BUGMAS 초록 (위→아래)
INTRO_SNOW, INTRO_REVEAL = 1.6, 1.2  # 초: 눈발이 굵어지는 시간 · 가운데가 걷히며 글씨가 나오는 시간


def logo_rows(word, px, gap):
    """글씨 5줄. px = 점 하나의 가로 칸 수, gap = 글자 사이 칸 수."""
    return [(" " * gap).join("".join(("█" if c == "#" else " ") * px for c in FONT[ch][r]) for ch in word)
            for r in range(5)]


SERVICES = [
    # (키 = Sentry 프로젝트 접두사, 이름, 설명, 트래픽 배율, 버그 배율)
    ("ALL", "전체", "모든 서비스 합산", 1.0, 1.0),
    ("WEB", "웹 프론트", "메인 · 공고 목록", 0.55, 1.2),
    ("SEARCH", "검색", "검색 결과 · 자동완성", 0.35, 0.9),
    ("RESUME", "이력서", "이력서 작성 · 지원", 0.2, 0.7),
    ("DISPLAY", "전시·광고", "배너 · 추천 영역", 0.3, 0.6),
    ("MOBILE", "모바일 웹", "m. 도메인 전체", 0.45, 1.4),
]
SCENARIOS = [
    ("traffic", "🎄 트래픽", "상점 앞 대기열",
     ["손님이 상점(SHOP) 앞에 한 줄로 서서 차례로 계산한다.", "rps↑ = 줄이 길어진다. 화면 밖까지 밀리면 인파 경보."]),
    ("sentry", "🐭 Sentry 버그", "병정의 쥐 사냥",
     ["Sentry 미해결 이슈 = 쥐. 병정(sentry-bot)이 출동해", "triage → fix → review → MR 로 처치한다."]),
    ("overview", "📊 개요", "숫자 대시보드",
     ["트래픽과 Sentry를 숫자·그래프로 한눈에.", "움직이는 장면 없이 1초에 한 번 갱신."]),
]


def stars(v):
    n = max(1, min(5, int(round(v * 5))))
    return "★" * n + "☆" * (5 - n)

# ------------------------------------------------------------------ 데이터 소스

ISSUE_CATALOG = [
    # (프로젝트 접두사, 제목, 판정)
    ("WEB", "TypeError: Cannot read properties of undefined (reading 'map')", "fixable"),
    ("WEB", "RangeError: Invalid time value", "fixable"),
    ("SEARCH", "Hydration failed: server/client text mismatch", "fixable"),
    ("RESUME", "TypeError: e.toSorted is not a function", "fixable"),
    ("DISPLAY", "SecurityError: sessionStorage access denied", "fixable"),
    ("MOBILE", "TypeError: null is not an object (evaluating 'el.scrollTop')", "fixable"),
    ("WEB", "ChunkLoadError: Loading chunk 7967 failed", "noise"),
    ("MOBILE", "Failed to fetch (ad.doubleclick.net)", "noise"),
    ("WEB", "MetaMask inpage.js: Object.connect failed", "noise"),
    ("MOBILE", "API Error: 503 /api/member/info", "needs-human"),
    ("SEARCH", "<obscura:bootstrap> globalThis.fetch override", "needs-human"),
]
KIND_WEIGHTS = {"fixable": 0.55, "noise": 0.25, "needs-human": 0.20}


class MockSource:
    """Grafana(트래픽) · Sentry(이슈) 자리. 실연동 시 이 클래스만 교체한다."""

    def __init__(self):
        self.spikes = []  # (시작, 크기, 길이)
        self.seq = 0x2A0

    def add_spike(self, t, amp=1900, dur=20):
        self.spikes.append((t, amp, dur))

    def rps(self, t):
        v = 850 + 420 * math.sin(t / 15.0) + 140 * math.sin(t / 3.3)
        for s, a, d in self.spikes:
            if s <= t <= s + d:
                v += a * math.sin(math.pi * (t - s) / d) ** 0.5
        self.spikes = [sp for sp in self.spikes if t <= sp[0] + sp[2]]
        return max(60.0, v + R.gauss(0, 35))

    def poll_issues(self, t, dt, ratio, bug=1.0, service=None):
        """새로 생긴 미해결 이슈. 트래픽(ratio = rps / 최고치)이 높을수록 자주 생긴다."""
        lam = (1 / 16.0 + 0.25 * max(0.0, ratio - 0.53)) * bug
        return [self.make_issue(service)] if R.random() < lam * dt else []

    def make_issue(self, service=None):
        """service 를 주면 그 서비스(Sentry 프로젝트) 이슈만 만든다."""
        kind = R.choices(list(KIND_WEIGHTS), weights=list(KIND_WEIGHTS.values()))[0]
        pool = [c for c in ISSUE_CATALOG if c[2] == kind]
        proj, title, _ = R.choice([c for c in pool if c[0] == service] or pool)
        proj = service or proj
        self.seq += R.randint(1, 9)
        return {
            "id": "%s-%s" % (proj, base36(self.seq)),
            "title": title,
            "kind": kind,
            "review_block": kind == "fixable" and R.random() < 0.15,
        }


def base36(n):
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"[r] + s
    return s or "0"


# ------------------------------------------------------------------ 등장인물 (좌표: x=칸=픽셀, y=픽셀)


def visitor_pal():
    return {"H": R.choice(HATS), "S": 223, "C": R.choice(COATS), "P": R.choice(PANTS), "k": 16}


class Queuer:
    """입장 대기열의 한 사람. d = 맨 앞 자리(입구)에서 줄을 따라 잰 거리(픽셀). 줄 위로만 걷는다."""
    SPEED = 14.0
    GAP = 4.0  # 앞사람과의 간격(픽셀)

    def __init__(self, d):
        self.d = d
        self.pal = visitor_pal()
        self.step = R.random()
        self.moving = False

    def update(self, dt, goal):
        self.moving = self.d > goal + 0.05
        if self.moving:
            self.d -= min(self.d - goal, self.SPEED * dt)
            self.step += dt

    @property
    def walking(self):
        return self.moving and int(self.step * 6) % 2 == 1


class Mouse:
    W = 15

    def __init__(self, world, issue):
        self.issue = issue
        self.dir = -1 if R.random() < 0.6 else 1
        self.x = float(world.w - 1) if self.dir < 0 else -15.0
        self.y = world.lanes[R.randrange(len(world.lanes))]  # 발바닥 픽셀 행
        self.speed = R.uniform(3, 8)
        self.state = "roam"  # roam / held / dead / flee / caged
        self.claimed = None
        self.ttl = 0.0
        self.born = world.t
        self.step = R.random()
        self.priority = False  # 채팅 focus 지시
        self.assigned = None  # 채팅 명령으로 이 쥐를 맡은 병정 번호(0부터)

    def update(self, world, dt):
        self.step += dt
        if self.state == "roam":
            if R.random() < 0.6 * dt:
                self.dir *= -1
            self.x += self.dir * self.speed * dt
            lo, hi = 12.0, world.w - 16.0
            if self.x < lo and self.dir < 0 or self.x > hi and self.dir > 0:
                self.dir *= -1
        elif self.state == "flee":
            self.x += self.dir * 26 * dt
            if self.x < -16 or self.x > world.w + 2:
                self.ttl = -1
        elif self.state in ("dead", "caged"):
            self.ttl -= dt


SOLDIER_NAMES = ["루돌프", "진저", "호두"]
ISSUE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]+-[0-9A-Za-z]+")
# 명령 뜻 (앞에 있는 것부터 본다)
ORDER_WORDS = [
    ("report", ("보고", "상황", "뭐해", "현황", "어때", "status", "report")),
    ("noise", ("noise", "노이즈", "무시", "쫓아")),
    ("human", ("사람", "담당자", "휴먼", "human", "에스컬")),
    ("catch", ("잡아", "잡자", "처치", "해결", "고쳐", "수정", "먼저", "우선", "맡아", "catch", "fix", "focus")),
    ("rest", ("복귀", "쉬어", "돌아와", "대기", "멈춰", "그만", "휴식", "rest", "home", "stop")),
    ("go", ("출동", "가자", "일해", "시작", "재개", "나가", "go", "start")),
]
OUTCOME_WORD = {"fixed": "MR 생성", "noise": "noise 쫓아냄", "human": "사람 호출"}
def kind_word(issue):
    """화면에 쓸 판정: AI 판정을 기다리는 중이면 '판정 중'."""
    return "판정 중" if issue.get("ai") == "pending" else issue["kind"]


STATE_WORD = {"walk": "추격", "triage": "트리아지", "fix": "수정", "review": "리뷰", "shoo": "쫓는", "flag": "사람 호출",
              "after": "마무리"}


class Soldier:
    """sentry-bot 서브 에이전트 하나. 채팅 명령(World.order)을 받아 움직인다."""
    W, H = 7, 14

    def __init__(self, idx, world):
        self.idx = idx
        self.name = SOLDIER_NAMES[idx % len(SOLDIER_NAMES)]
        self.off = False  # 복귀 명령: 지금 건만 마치고 위병소에서 대기
        self.speech, self.speech_t = "", 0.0
        self.home = 2.0
        self.x = self.home
        self.face = 1
        self.state = "home"
        self.target = None
        self.timer = 0.0
        self.bubble = ""
        self.phase = 0.0

    @property
    def busy(self):
        return self.state != "home"

    @property
    def label(self):
        return "병정%d %s" % (self.idx + 1, self.name)

    def say(self, world, text, color=183):
        """말풍선 + 작전 로그 한 줄 (명령에 대한 대답)."""
        self.speech, self.speech_t = text, 4.0
        world.log("💂 %s: %s" % (self.label, text), color)

    def release(self):
        """쫓던 쥐를 놓아준다 (다른 쥐로 바꾸거나 복귀할 때)."""
        if self.target is not None and self.target.state == "held":
            self.target.state, self.target.claimed = "roam", None
        self.target, self.bubble = None, ""

    def claim_next(self, world):
        free = [m for m in world.mice if m.state == "roam" and m.claimed is None and m.assigned in (None, self.idx)
                and 10 <= m.x <= world.w - 16]
        if not free:
            return False
        m = min(free, key=lambda m: (m.assigned != self.idx, not m.priority, m.born, abs(m.x - self.x)))
        m.claimed, m.state = self.idx, "held"
        self.target = m
        self.state = "walk"
        self.bubble = ""
        world.log("%s 출동 -> %s 발견" % (self.label, m.issue["id"]), 117)
        return True

    def update(self, world, dt):
        self.phase += dt
        self.speech_t -= dt
        m = self.target
        if self.state == "home":
            if self.off:
                return
            mine = any(mm.assigned == self.idx and mm.state == "roam" for mm in world.mice)
            backlog = sum(1 for mm in world.mice if mm.state == "roam" and mm.claimed is None and mm.assigned is None)
            if mine or backlog >= 1 + 3 * self.idx:
                self.claim_next(world)
            return
        if self.state in ("walk", "return"):
            if self.state == "return":
                goal = self.home
            else:
                goal = m.x - 9 if m.x > self.x else m.x + 16
            dx = goal - self.x
            look = (m.x + 7) if self.state == "walk" else goal
            self.face = 1 if look >= self.x + 3 else -1
            if abs(dx) < 0.5:
                if self.state == "return":
                    self.state = "home"
                else:
                    self.state, self.timer, self.bubble = "triage", 1.1, "? triage"
                return
            self.x += math.copysign(min(abs(dx), 15 * dt), dx)
            return

        self.timer -= dt
        if self.state == "fix":
            p = 1 - self.timer / 2.6
            n = int(p * 6)
            self.bubble = "fix [%s%s]" % ("#" * n, "-" * (6 - n))
            if R.random() < 14 * dt:
                world.spark(m.x + R.uniform(0, 14), m.y - R.uniform(0, 5))
        if self.timer > 0:
            return
        if self.state == "triage" and m.issue.get("ai") == "pending" and self.timer > -8:
            self.bubble = "? AI 판정 중"  # AI 판정이 올 때까지 최대 8초 더 기다린다
            return

        iid = m.issue["id"]
        if self.state == "triage":
            kind = m.issue["kind"]
            m.issue["triaged"] = True
            if m.issue.get("ai") == "pending":  # 8초 안에 AI 판정이 안 오면 mock 판정으로 간다
                m.issue["ai"] = "timeout"
            if m.issue.get("note"):
                world.log("🧠 트리아지 %s -> %s: %s" % (iid, kind, m.issue["note"]), 229)
            else:
                world.log("트리아지 %s: %s -> %s" % (iid, m.issue["title"][:38], kind), 229)
            if kind == "fixable":
                self.state, self.timer = "fix", 2.6
            elif kind == "noise":
                self.state, self.timer, self.bubble = "shoo", 0.9, "noise! shoo!"
            else:
                self.state, self.timer, self.bubble = "flag", 1.1, "!! call human"
        elif self.state == "fix":
            self.state, self.timer, self.bubble = "review", 1.1, "review..."
        elif self.state == "review":
            if m.issue["review_block"]:
                m.issue["review_block"] = False
                m.state, m.claimed = "roam", None
                world.stats["blocked"] += 1
                world.log("독립 리뷰 block: %s 수정 반려 -> 다음 런 재시도" % iid, 208)
                self.bubble, self.state, self.timer = "blocked :(", "after", 1.0
            else:
                world.mr += 1
                world.stats["fixed"] += 1
                m.state, m.ttl = "dead", 0.4
                world.poof(m.x + 7, m.y - 2)
                if m.issue.get("fix"):
                    world.log("MR !%d 생성 - fix: %s (%s, %s)" % (world.mr, m.issue["fix"], iid, self.label), 46)
                else:
                    world.log("MR !%d 생성 - fix: %s 처치 완료 (%s)" % (world.mr, iid, self.label), 46)
                world.record(m, "fixed", self, world.mr)
                self.bubble, self.state, self.timer = "MR !%d" % world.mr, "after", 1.0
        elif self.state == "shoo":
            m.state, m.dir = "flee", (1 if m.x > self.x else -1)
            world.stats["noise"] += 1
            world.log("%s 은(는) noise - 레포 밖 출처, 쫓아냄" % iid, 245)
            world.record(m, "noise", self)
            self.state, self.timer = "after", 0.3
        elif self.state == "flag":
            m.state, m.ttl = "caged", 30.0
            world.stats["human"] += 1
            world.log("%s needs-human - 우리에 가두고 담당자 호출" % iid, 226)
            world.record(m, "human", self)
            self.state, self.timer = "after", 0.4
        elif self.state == "after":
            self.target = None
            self.bubble = ""
            if self.off or not self.claim_next(world):
                self.state = "return"


# ------------------------------------------------------------------ 세계

BOOTH_NAMES = ["GLUHWEIN", "COOKIES", "ORNAMENTS", "BRATWURST", "HOT CHOCO", "CANDLES", "PRETZEL", "TOYS"]
BOOTH_STRIPES = [(196, 255), (28, 255), (196, 220), (25, 255)]


class World:
    def __init__(self):
        self.src = MockSource()
        self.t = 0.0
        self.w = self.sh = 0
        self.lanes = [1, 1]  # layout() 전에 쥐가 생겨도 되도록 (layout 이 제자리로 옮긴다)
        self.mice, self.particles = [], []
        self.queue, self.passing = [], []  # 입장 대기열 · 입구를 지나가는 사람 [x, y, pal, step]
        self.admit_acc, self.admitted = 0.0, 0
        self.soldiers = []
        self.logs = []
        self.rps = 0.0
        self.hist, self.ehist, self.spawn_times = [], [], []
        self.hist_acc = 0.0
        self.mr = 1400
        self.stats = {"fixed": 0, "noise": 0, "human": 0, "blocked": 0}
        self.history = []  # 잡은 쥐 기록: 처리 결과(fixed/noise/human)마다 한 건
        self.flying = []  # 처치된 쥐가 뒤쪽 우리로 날아가는 중: [x, y, 우리 안 번호, 경과]
        self.show_hist, self.hist_sel = False, None  # 기록 창을 열었는지 · 강조할 이슈 ID
        self.auto = True
        self.stage_gen = 0  # 판이 바뀌면 +1. 이전 판에 보낸 AI 결과는 버린다
        self.ai = None  # ai.Brain: 있으면 병정 명령 해석·트리아지·브리핑을 OpenAI API 로 한다
        self.live = None  # sentry_live.SentryFeed: 있으면 쥐는 실제 Sentry 미해결 이슈다 (트래픽은 mock)
        self.live_fail = None  # --live 를 켰는데 못 붙은 이유 (스테이지 시작 로그에 다시 보여 준다)
        self.live_wait, self.live_acc = [], 0.0  # 아직 화면에 안 나온 실제 이슈 · 다음 등장까지 남은 시간
        self.fired = set()
        self.phase = "intro"  # intro(타이틀) / menu(스테이지 고르기) / play
        self.intro_t = 0.0
        self.tsnow, self.tsize = [], None  # 타이틀·메뉴 화면 눈발
        self.menu_col, self.svc_i, self.scn_i = 0, 0, 0  # 메뉴 포커스(0 서비스 / 1 시나리오) · 고른 줄
        self.service = None  # 지금 스테이지의 SERVICES 항목 (시작 전에는 None)
        self.peak = PEAK_RPS
        self.view = "overview"  # overview(정적 개요) / traffic(광장) / sentry(전투)
        self.unseen = 0  # 광장을 보는 동안 새로 나온 쥐
        self.hits = []  # 클릭 영역 (x0, x1, y0, y1, 동작), 1부터 세는 칸 좌표
        self.scene_hits = []
        self.log("크리스마스 마켓 개장! (mock 데이터)", 220, "all")

    @property
    def svc(self):
        return self.service or SERVICES[self.svc_i]

    @property
    def svc_key(self):
        """Sentry 프로젝트 접두사. 전체면 None."""
        return None if self.svc[0] == "ALL" else self.svc[0]

    @property
    def ratio(self):
        return self.rps / self.peak

    # ---- 스테이지 · 메뉴
    def start_stage(self, svc_i, view):
        """서비스를 바꾸면 새 판(쥐·대기열·통계 초기화). 같은 서비스면 화면만 바꾼다."""
        svc = SERVICES[svc_i]
        self.svc_i = svc_i
        if svc is not self.service:
            self.service, self.peak = svc, PEAK_RPS * svc[3]
            self.mice, self.queue, self.passing, self.particles = [], [], [], []
            self.soldiers = [Soldier(i, self) for i in range(3)]
            self.stage_gen += 1
            self.stats = {"fixed": 0, "noise": 0, "human": 0, "blocked": 0}
            self.history, self.show_hist, self.hist_sel, self.flying = [], False, None, []
            self.mr, self.admitted, self.admit_acc = 1400, 0, 0.0
            self.hist, self.ehist, self.spawn_times = [], [], []
            self.src.spikes, self.fired, self.t, self.logs = [], set(), 0.0, []
            if self.live is not None:
                self.live.reset()
                self.live_wait = []
                self.log("STAGE %s %s 시작! (쥐 = Sentry %s/%s 실제 미해결 이슈, 트래픽은 mock)" % (
                    svc[0], svc[1], self.live.org, self.live.project), 220, "all")
            else:
                self.log("STAGE %s %s 시작! (mock 데이터)" % (svc[0], svc[1]), 220, "all")
                if self.live_fail:
                    self.log("Sentry 실연동 실패 (%s) - mock 으로 진행" % self.live_fail, 203, "all")
        self.phase = "play"
        self.set_view(view)

    def menu_move(self, d):
        if self.menu_col == 0:
            self.svc_i = (self.svc_i + d) % len(SERVICES)
        else:
            self.scn_i = (self.scn_i + d) % len(SCENARIOS)

    def menu_enter(self):
        if self.menu_col == 0:
            self.menu_col = 1
        else:
            self.start_stage(self.svc_i, SCENARIOS[self.scn_i][0])

    def menu_back(self):
        if self.menu_col == 1:
            self.menu_col = 0
        else:
            self.set_view("intro")

    # ---- 잡은 쥐 기록
    def record(self, m, outcome, s, mr=None):
        if outcome == "fixed":  # 처치한 쥐는 뒤쪽 우리로 날아간다
            self.flying.append([m.x + 4, m.y - 3, sum(1 for r in self.history if r["outcome"] == "fixed"), 0.0])
        self.history.append({"id": m.issue["id"], "title": m.issue["title"], "kind": m.issue["kind"],
                             "outcome": outcome, "soldier": s.idx + 1, "mr": mr, "t": self.t,
                             "note": m.issue.get("fix") or m.issue.get("note") or ""})
        self.history = self.history[-300:]

    def open_history(self, iid=None):
        """기록 창 열기. iid 를 주면 그 쥐 줄을 강조한다. 같은 걸 다시 부르면 닫는다."""
        if self.show_hist and (iid is None or iid == self.hist_sel):
            self.show_hist, self.hist_sel = False, None
        else:
            self.show_hist, self.hist_sel = True, iid

    # ---- 병정(서브 에이전트)에게 내리는 명령
    def find_soldiers(self, who):
        """'2', '병정2', '2번', '진저', 'all/모두/전원' → 병정 목록."""
        w = who.strip().lower()
        if w in ("all", "모두", "전원", "다", "전체", "*"):
            return list(self.soldiers)
        num = re.search(r"\d", w)
        if num and 1 <= int(num.group()) <= len(self.soldiers):
            return [self.soldiers[int(num.group()) - 1]]
        return [s for s in self.soldiers if s.name in who]

    def order(self, text, who=None):
        """자연어 명령 한 줄. AI 가 켜져 있으면 OpenAI 로 해석하고(키워드에 없는 말도), 아니면 키워드로 알아듣는다."""
        if self.ai is not None and who is None and not getattr(self.ai, "replay", False):
            self.log("📣 명령: %s  (🧠 해석 중…)" % text.strip()[:50], 229, "all")
            self.ai.submit("order", {"text": text, "context": self.ai_context()}, (self.stage_gen, text))
            return
        self.order_keywords(text, who)

    def ai_context(self):
        """명령 해석용 상황 요약: 병정과 살아 있는 쥐."""
        return {
            "soldiers": [{"id": s.idx + 1, "name": s.name, "state": s.state, "off": s.off,
                          "target": s.target.issue["id"] if s.target else None} for s in self.soldiers],
            "mice": [{"id": m.issue["id"], "title": m.issue["title"], "kind": kind_word(m.issue), "state": m.state,
                      "age_s": round(self.t - m.issue.get("born_t", self.t)),
                      "chasedBy": None if m.claimed is None else m.claimed + 1}
                     for m in self.mice if m.state in ("roam", "held", "caged")],
        }

    def apply_ai_order(self, text, res):
        who, intent = res.get("soldier", "all"), res.get("intent", "unknown")
        iid = (res.get("issue_id") or "").strip().upper()
        reply = (res.get("reply") or "").strip()
        targets = self.find_soldiers("all" if who == "none" else who)
        self.log("🧠 해석: %s · %s%s" % ("모두" if who in ("all", "none") else "병정" + who, intent,
                                       " · " + iid if iid else ""), 141, "all")
        if intent == "unknown":
            s = targets[0] if len(targets) == 1 else next((x for x in self.soldiers if not x.busy), self.soldiers[0])
            s.say(self, reply or "무슨 명령인지 모르겠어요 (예: WEB-2B1 잡아 / 복귀 / 출동 / 보고)")
            return
        self.dispatch(targets, intent, [iid] if iid else [], text)

    def order_keywords(self, text, who=None):
        """키워드로 알아듣는 명령 (AI 가 꺼졌거나 실패했을 때)."""
        if who is None:
            m = re.match(r"\s*(병정\s*\d|\d\s*번|\d|all|모두|전원|%s)\s*[,:야]?\s*(.*)" % "|".join(SOLDIER_NAMES), text)
            who, text = (m.group(1), m.group(2)) if m else ("all", text)
        targets = self.find_soldiers(who)
        if not targets:
            self.log("명령: '%s' 라는 병정은 없습니다 (1~%d, %s)" % (who, len(self.soldiers), "·".join(SOLDIER_NAMES)), 245, "all")
            return
        ids = [i.upper() for i in ISSUE_RE.findall(text)]
        low = text.lower()
        intent = next((name for name, words in ORDER_WORDS if any(k in low for k in words)), None)
        if intent is None and ids:
            intent = "catch"
        self.log("📣 명령 → %s: %s" % (", ".join(s.label for s in targets), text.strip()[:50]), 229, "all")
        self.dispatch(targets, intent, ids, text)

    def dispatch(self, targets, intent, ids, text):
        if intent in ("catch", "noise", "human") and len(targets) > 1:
            # 쥐 한 마리는 한 병정이: 놀고 있는 병정, 그다음 복귀 중인 병정, 그다음 번호 순
            rank = {"home": 0, "return": 1, "walk": 2}
            targets = [min(targets, key=lambda s: (s.off, rank.get(s.state, 3), s.idx))]
        for s in targets:
            self.do_order(s, intent, ids)

    def do_order(self, s, intent, ids):
        live = {m.issue["id"].upper(): m for m in self.mice if m.state in ("roam", "held", "caged")}
        if intent in ("catch", "noise", "human"):
            if not ids:
                s.say(self, "어느 쥐요? 이슈 ID를 같이 말해 주세요 (예: WEB-2B1)")
                return
            m = live.get(ids[0])
            if m is None:
                done = [r for r in self.history if r["id"].upper() == ids[0]]
                s.say(self, "%s 는 이미 처리했습니다 (%s)" % (ids[0], OUTCOME_WORD[done[-1]["outcome"]]) if done
                      else "%s 쥐는 안 보입니다" % ids[0])
                return
            if m.state == "caged":
                s.say(self, "%s 는 우리에 있어요. 담당자 확인을 기다리는 중입니다" % ids[0])
                return
            if m.state == "held" and m.claimed != s.idx:
                s.say(self, "%s 는 병정%d 이 이미 잡고 있습니다" % (ids[0], m.claimed + 1))
                return
            if intent != "catch":
                m.issue["kind"] = "noise" if intent == "noise" else "needs-human"
                m.issue["ai"] = "manual"  # 사람이 정한 판정은 늦게 온 AI 판정이 덮지 않는다
                m.issue["review_block"] = False
            prev = m.assigned
            m.assigned, m.priority, s.off = s.idx, True, False
            how = {"catch": "잡으러", "noise": "noise 로 쫓아내러", "human": "우리에 가두러"}[intent]
            if prev is not None and prev != s.idx:
                how = "병정%d 대신 %s" % (prev + 1, how)
            if s.target is m:
                s.say(self, "지금 %s 처리 중입니다!" % ids[0])
            elif s.state in ("home", "walk", "return"):
                s.release()
                s.state = "home"
                s.claim_next(self)
                s.say(self, "알겠습니다! %s %s 갑니다" % (ids[0], how))
            else:
                s.say(self, "지금 %s 끝나는 대로 %s %s 가겠습니다" % (s.target.issue["id"], ids[0], how))
        elif intent == "rest":
            s.off = True
            if s.state == "walk":
                s.release()
                s.state = "return"
            s.say(self, "지금 건만 마치고 위병소로 복귀합니다" if s.state not in ("home", "return") else "위병소에서 대기하겠습니다")
        elif intent == "go":
            s.off = False
            if s.state == "home" and not s.claim_next(self):
                s.say(self, "출동 준비 완료! 지금은 잡을 쥐가 없어요")
            else:
                s.say(self, "출동합니다!")
        elif intent == "report":
            mine = [r for r in self.history if r["soldier"] == s.idx + 1]
            cnt = {k: sum(1 for r in mine if r["outcome"] == k) for k in ("fixed", "noise", "human")}
            queued = [m.issue["id"] for m in self.mice if m.assigned == s.idx and m.state == "roam"]
            now = ("%s %s 중" % (s.target.issue["id"], STATE_WORD.get(s.state, s.state))) if s.target else \
                ("%s 맡음, 곧 출동" % queued[0]) if queued else ("복귀 대기" if s.off else "위병소 대기")
            s.say(self, "%s · 지금까지 MR %d · noise %d · 사람 %d" % (now, cnt["fixed"], cnt["noise"], cnt["human"]))
        else:
            s.say(self, "무슨 명령인지 모르겠어요 (예: WEB-2B1 잡아 / 복귀 / 출동 / 보고)", 245)

    def find_service(self, key):
        keys = [s[0].lower() for s in SERVICES]
        return keys.index(key.lower()) if key.lower() in keys else None

    # ---- 레이아웃
    def layout(self, w, sh):
        if (w, sh) == (self.w, self.sh):
            return
        self.w, self.sh = w, sh
        self.gt = max(5, int(sh * 0.42))  # 광장(지면) 시작 칸 행
        self.gpx = 2 * self.gt  # 광장 시작 픽셀 행
        self.bottom = 2 * sh - 1  # 맨 아래 픽셀 행
        self.lanes = [self.bottom, self.bottom - 3]
        # 입장 대기열: 오른쪽 입구에서 왼쪽으로 한 줄, 길면 뱀처럼 접어 올라간다 (줄 수는 홀수라 꼬리는 늘 왼쪽 화면 밖)
        self.gate_x = w - 9
        y0 = self.bottom - 5  # 맨 앞 줄 사람의 머리 픽셀 행
        rows = max(1, (y0 - (self.gpx + 2)) // 7 + 1)
        rows = min(3, rows if rows % 2 else rows - 1)
        xl, xr = 3, self.gate_x - 4
        xr_up = xr - 3  # 위 줄은 상점 간판과 겹치지 않게 조금 왼쪽에서 꺾는다
        pts = [(xr, y0)]
        for r in range(rows):
            if r:
                pts.append((pts[-1][0], y0 - 7 * r))
            pts.append((xl if r % 2 == 0 else xr_up, y0 - 7 * r))
        pts.append((-4, pts[-1][1]))
        self.qpts = pts
        self.qvis = sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(pts, pts[1:]))  # 화면에 보이는 줄 길이
        self.qcap = int(self.qvis // Queuer.GAP)
        self.tree_cx = w // 2
        self.booths = []
        names = iter(BOOTH_NAMES)
        for a, b in ((11, self.tree_cx - 9), (self.tree_cx + 10, w - 1)):
            x = a
            for name in names:
                bw = len(name) + 4
                if x + bw > b:
                    break
                self.booths.append((x, name, BOOTH_STRIPES[len(self.booths) % len(BOOTH_STRIPES)]))
                x += bw + 2
        self.snow = [[R.uniform(0, w), R.uniform(0, self.gpx), R.uniform(3, 7), R.uniform(-1.5, 1.5)]
                     for _ in range(w * self.gt // 10)]
        self.stars = [(R.randrange(w), R.randrange(4, max(5, self.gpx - 10))) for _ in range(w // 4)]
        self.ground_tex = {(R.randrange(w), R.randrange(self.gpx + 2, 2 * sh)): R.choice((252, 253, 251))
                           for _ in range(w * (sh - self.gt) // 6)}
        if not self.soldiers:
            self.soldiers = [Soldier(i, self) for i in range(3)]
        for m in self.mice:
            m.y = self.lanes[0] if m.y % 2 == self.lanes[0] % 2 else self.lanes[1]

    def log(self, text, color=252, cat="sentry"):
        self.logs.append((self.t, text, color, cat))
        self.logs = self.logs[-50:]

    def spark(self, x, y):
        self.particles.append([x, y, R.uniform(-8, 8), R.uniform(-8, 2), R.choice([226, 231, 214]), 0.35])

    def poof(self, x, y):
        for _ in range(18):
            a = R.uniform(0, math.tau)
            sp = R.uniform(6, 16)
            self.particles.append([x, y, math.cos(a) * sp, math.sin(a) * sp * 0.8, R.choice([231, 226, 46, 220]), 0.7])

    def spawn_mouse(self, issue=None):
        if sum(1 for m in self.mice if m.state != "dead") >= 16:
            return
        if issue is None and self.live is not None:
            return  # 실연동 중에는 가짜 쥐를 섞지 않는다
        issue = issue or self.src.make_issue(self.svc_key)
        issue.setdefault("born_t", self.t)
        if self.ai is not None:  # mock 판정 대신 AI 가 제목을 보고 판정한다 (오기 전까지 병정은 트리아지에서 기다림)
            issue["ai"] = "pending"
            self.ai.submit("triage", {"id": issue["id"], "title": issue["title"], "service": self.svc_key},
                           (self.stage_gen, issue))
        self.mice.append(Mouse(self, issue))
        if self.view == "traffic":
            self.unseen += 1
        self.spawn_times.append(self.t)

    def poll_new_issues(self, dt):
        """새 미해결 이슈. 실연동이면 Sentry 에서 읽은 것을 0.8초에 하나씩, 아니면 MockSource."""
        if self.live is None:
            return self.src.poll_issues(self.t, dt, self.ratio, self.svc[4], self.svc_key)
        self.live_wait += self.live.poll()
        while True:
            try:
                self.log("Sentry 읽기 실패 (%s) - 다음 주기에 다시 시도" % self.live.errors.get_nowait(), 245, "all")
            except Exception:  # noqa: BLE001 — queue.Empty
                break
        self.live_acc -= dt
        if not self.live_wait or self.live_acc > 0 or sum(1 for m in self.mice if m.state != "dead") >= 16:
            return []
        self.live_acc = 0.8
        return [self.live_wait.pop(0)]

    # ---- 이벤트(키 입력 · 자동 연출 공용)
    def traffic_spike(self):
        self.src.add_spike(self.t)
        self.log("크리스마스 특가 오픈! 트래픽 급증 - 광장이 붐빈다", 213, "traffic")

    def spawn_one(self):
        """쥐 1마리 (키 n · ./market mouse). 실연동 중에는 outbreak 과 같이 만들지 않는다."""
        if self.live is not None:
            return self.outbreak(1)
        n = len(self.mice)
        self.spawn_mouse()
        if len(self.mice) > n:
            self.log("Sentry 신규 이슈 %s" % self.mice[-1].issue["id"], 203)

    def outbreak(self, n=5):
        if self.live is not None:
            self.log("Sentry 실연동 중 - 가짜 쥐는 만들지 않는다 (실제 미해결 이슈만 나온다)", 245)
            return
        self.log("release-261225 배포 직후 쥐 떼 출몰! (신규 이슈 %d건)" % n, 203)
        for _ in range(n):
            self.spawn_mouse()

    def human_clears(self):
        caged = [m for m in self.mice if m.state == "caged"]
        for m in caged:
            m.state, m.ttl = "dead", -1
        self.log("담당자가 우리 속 쥐 %d마리 처리 완료" % len(caged), 159)

    def set_view(self, v):
        if v == "menu":
            self.phase, self.show_hist = "menu", False
        elif v == "intro":
            self.phase, self.intro_t = "intro", 0.0
        elif v in ("overview", "traffic", "sentry"):
            if self.service is None:  # 메뉴를 거치지 않고 바로 화면을 부르면 지금 고른 서비스로 시작
                self.start_stage(self.svc_i, v)
                return
            self.phase = "play"
            self.view = v
            if v != "traffic":
                self.unseen = 0

    # ---- 입장 대기열 (트래픽)
    def qpos(self, d):
        """줄을 따라 맨 앞에서 d 픽셀 떨어진 자리의 (x, 머리 y). 보이는 줄보다 멀면 왼쪽 화면 밖으로 이어진다."""
        pts = self.qpts
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            seg = abs(x1 - x0) + abs(y1 - y0)
            if d <= seg:
                f = d / seg if seg else 0.0
                return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
            d -= seg
        x, y = pts[-1]
        return x - d, y

    def queue_target(self):
        """트래픽에 맞는 줄 길이. 최고치(peak) 근처면 화면을 넘친다."""
        return 2 + int(self.qcap * 1.1 * min(1.3, self.ratio) ** 1.4)

    def queue_offscreen(self):
        return sum(1 for p in self.queue if p.d > self.qvis - 1)

    def update_queue(self, dt):
        target, n = self.queue_target(), len(self.queue)
        if not n and not self.admitted:  # 스테이지 첫 프레임: 줄을 미리 세워 둔다
            self.queue = [Queuer(Queuer.GAP * i) for i in range(target)]
            n = target
        # 입구는 늘 사람을 들이고(줄이 움직이도록), 줄이 목표보다 길면 더 빨리 들인다
        mu = min(3.5, 1.2 + 0.15 * max(0, n - target))
        lam = 1.2 + 1.5 * max(0, target - n) if n < target + 3 else 0.2
        if R.random() < lam * dt:
            self.queue.append(Queuer(max(self.qvis + 6, Queuer.GAP * n + 4)))
        self.admit_acc = min(1.0, self.admit_acc + mu * dt)
        if self.queue and self.admit_acc >= 1.0 and self.queue[0].d < 0.5:
            p = self.queue.pop(0)
            x, y = self.qpos(0)
            self.passing.append([x, y, p.pal, 0.0])
            self.admit_acc = 0.0
            self.admitted += 1
            for _ in range(4 if self.view == "traffic" else 0):  # 계산: 동전이 튄다 (트래픽 화면에서만)
                self.particles.append([self.gate_x + 1, y + 2, R.uniform(-3, 3), R.uniform(-9, -4),
                                       R.choice([220, 226, 214]), 0.6])
        for i, p in enumerate(self.queue):
            p.update(dt, Queuer.GAP * i)
        for p in self.passing:
            p[0] += 8 * dt
            p[3] += dt
        self.passing = [p for p in self.passing if p[0] < self.gate_x + 3]  # 카운터 뒤 가게 안으로

    # ---- 갱신
    def pump_ai(self):
        """백그라운드 OpenAI 호출 결과를 반영한다 (메인 스레드에서만 world 를 바꾼다)."""
        if self.ai is None:
            return
        for kind, ctx, res, err in self.ai.poll():
            gen, obj = ctx
            try:
                self._apply_ai(kind, gen, obj, res, err)
            except Exception as e:  # noqa: BLE001 — 이상한 응답이 대시보드를 죽이지 않게
                self.log("🧠 결과 처리 실패 (%s)" % type(e).__name__, 245, "all")

    def _apply_ai(self, kind, gen, obj, res, err):
        if kind == "triage":
            issue = obj
            if issue.get("ai") != "pending" or issue.get("triaged") or gen != self.stage_gen:
                if issue.get("ai") == "pending":
                    issue["ai"] = "timeout"
                return
            if err is not None:
                issue["ai"] = "failed"  # mock 판정 그대로
                return
            issue.update(kind=res["kind"], note=res["reason"][:60], fix=res["fix"][:50], ai="done")
            if res["kind"] != "fixable":
                issue["review_block"] = False
            return
        if gen != self.stage_gen:  # 판이 바뀌었다
            return
        if kind == "order":
            if err is not None:
                self.log("🧠 해석 실패 (%s) - 키워드로 처리" % type(err).__name__, 245, "all")
                self.order_keywords(obj)
            else:
                self.apply_ai_order(obj, res)
        elif kind == "brief":
            if err is not None:
                self.log("🧠 브리핑 실패 (%s)" % type(err).__name__, 245, "all")
            else:
                for line in res["summary"][:3]:
                    self.log("🧠 " + line, 159, "all")
                self.log("🧠 권고: " + res["advice"], 213, "all")

    def brief(self):
        if self.ai is None or getattr(self.ai, "replay", False):
            st = self.snapshot_state()
            live = [m for m in st["mice"]]
            self.log("📋 브리핑: rps %s (%s) · 미해결 쥐 %d · MR %d · 사람 대기 %d  (AI 꺼짐: OPENAI_API_KEY)" % (
                format(st["traffic"]["rps"], ","), st["traffic"]["crowd"], len(live), st["stats"]["mrs"],
                sum(1 for m in live if m["state"] == "caged")), 159, "all")
            return
        self.log("🧠 브리핑 준비 중…", 245, "all")
        self.ai.submit("brief", self.snapshot_state(), (self.stage_gen, None))

    def update(self, dt):
        self.pump_ai()
        if self.phase != "play":  # 타이틀·메뉴: 눈발만 움직인다
            self.intro_t += dt
            if self.tsize:
                for f in self.tsnow:
                    f[1] += f[2] * dt
                    f[0] += f[3] * dt + 0.6 * math.sin(self.intro_t * 1.5 + f[2]) * dt
                    if f[1] >= 2 * self.tsize[1]:
                        f[0], f[1] = R.uniform(0, self.tsize[0]), 0.0
            return
        if not self.w:  # 첫 compose()가 layout을 잡기 전
            return
        self.t += dt
        if self.auto:
            cyc, ct = divmod(self.t, 90.0)
            for at, name, fn in ((12, "spike", self.traffic_spike), (45, "outbreak", self.outbreak)):
                if ct >= at and (cyc, name) not in self.fired:
                    self.fired.add((cyc, name))
                    if not (name == "outbreak" and self.live is not None):
                        fn()

        self.rps = self.src.rps(self.t) * self.svc[3]
        for issue in self.poll_new_issues(dt):
            self.log("Sentry %s이슈 %s: %s" % ("미해결 " if issue.get("live") else "신규 ", issue["id"],
                                             issue["title"][:40]), 203)
            self.spawn_mouse(issue)

        self.hist_acc += dt
        if self.hist_acc >= 0.5:
            self.hist_acc = 0.0
            self.spawn_times = [s for s in self.spawn_times if self.t - s < 20]
            self.hist = (self.hist + [self.rps])[-60:]
            self.ehist = (self.ehist + [len(self.spawn_times)])[-60:]

        self.update_queue(dt)  # 대기열 길이 = 트래픽

        for m in self.mice:
            m.update(self, dt)
            if m.state == "caged" and m.ttl <= 0 and self.auto:
                self.log("담당자가 %s 확인 완료 (우리 해제)" % m.issue["id"], 159)
                m.state, m.ttl = "dead", -1
        self.mice = [m for m in self.mice if not (m.state in ("dead", "flee") and m.ttl < 0)]

        for s in self.soldiers:
            s.update(self, dt)

        for f in self.flying:
            f[3] += dt
        self.flying = [f for f in self.flying if f[3] < 1.2]
        for p in self.particles:
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[3] += 20 * dt
            p[5] -= dt
        self.particles = [p for p in self.particles if p[5] > 0]

        for f in self.snow:
            f[1] += f[2] * dt
            f[0] += f[3] * dt + 0.6 * math.sin(self.t * 1.5 + f[2]) * dt
            if f[1] >= self.gpx:
                f[0], f[1] = R.uniform(0, self.w), 0.0

    # ---- 채팅(Codex) 브리지: .runtime/cmd 로 명령을 받고 .runtime/state.json 에 현황을 쓴다
    def command(self, line):
        """채팅·패널 명령 한 줄. 잘못된 명령이 대시보드를 죽이지 않게 오류는 로그로만 남긴다."""
        try:
            self._command(line)
        except Exception as e:  # noqa: BLE001
            self.log("명령 처리 실패: %s (%s)" % (line.strip()[:30], type(e).__name__), 196, "all")

    def _command(self, line):
        parts = line.strip().split(None, 1)
        if not parts:
            return
        op, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if op in ("spike", "traffic"):
            self.traffic_spike()
        elif op in ("outbreak", "bugs"):
            self.outbreak(int(arg) if arg.isdigit() else 5)
        elif op in ("mouse", "bug"):
            self.spawn_one()
        elif op == "human":
            self.human_clears()
        elif op == "auto":
            self.auto = arg.lower() != "off"
            self.log("자동 연출 %s" % ("ON" if self.auto else "OFF"), 245, "all")
        elif op == "focus":
            hit = [m for m in self.mice if m.issue["id"].lower() == arg.lower()]
            if hit:
                hit[0].priority = True
                self.log("채팅 지시: %s 우선 처치" % hit[0].issue["id"], 183)
            else:
                self.log("채팅 지시: %s 쥐를 찾지 못함" % arg, 245)
        elif op == "view":
            self.set_view({"0": "overview", "1": "traffic", "2": "sentry"}.get(arg, arg.lower()))
        elif op in ("start", "stage", "service"):
            a = arg.split()
            idx = self.find_service(a[0]) if a else self.svc_i
            scn = a[1].lower() if len(a) > 1 else (self.view if self.phase == "play" else "sentry")
            if idx is None or scn not in ("overview", "traffic", "sentry"):
                self.log("채팅 지시: 서비스/화면을 모름 (%s)" % arg[:30], 245, "all")
            else:
                self.start_stage(idx, scn)
        elif op in ("tell", "order"):
            self.order(arg)
        elif op == "history":
            if arg.lower() in ("off", "close", "닫기"):
                self.show_hist, self.hist_sel = False, None
            else:
                self.open_history(arg.upper() or None)
        elif op in ("brief", "briefing", "브리핑"):
            self.brief()
        elif op == "say":
            self.log("💬 codex: " + arg, 183, "all")
        else:
            self.log("알 수 없는 명령: %s" % line.strip()[:40], 245, "all")

    def snapshot_state(self):
        label, _ = self.crowd_label()
        return {
            "t": round(self.t, 1),
            "phase": self.phase,
            "service": self.svc[0] if self.service else None,
            "traffic": {"rps": int(self.rps), "peak_rps": int(self.peak), "queue": len(self.queue),
                        "queue_offscreen": self.queue_offscreen() if self.w else 0,
                        "admitted": self.admitted, "crowd": label,
                        "history_rps": [int(v) for v in self.hist[-20:]]},
            "mice": [{"id": m.issue["id"], "title": m.issue["title"],
                      "kind": "pending" if m.issue.get("ai") == "pending" else m.issue["kind"],
                      "note": m.issue.get("note", ""),
                      "state": m.state, "chasedBy": None if m.claimed is None else m.claimed + 1}
                     for m in self.mice if m.state != "dead"],
            "history": [{"id": r["id"], "outcome": r["outcome"], "soldier": r["soldier"], "mr": r["mr"],
                         "title": r["title"], "note": r.get("note", "")} for r in self.history[-20:]],
            "soldiers": [{"id": s.idx + 1, "name": s.name, "state": s.state, "off": s.off,
                          "handled": sum(1 for r in self.history if r["soldier"] == s.idx + 1),
                          "target": s.target.issue["id"] if s.target else None} for s in self.soldiers],
            "stats": dict(self.stats, mrs=self.mr - 1400),
            "auto": self.auto,
            "live": {"on": self.live is not None, "source": "sentry" if self.live else "mock",
                     "calls": self.live.calls if self.live else 0, "fails": self.live.fails if self.live else 0},
            "ai": {"on": self.ai is not None, "model": self.ai.model if self.ai else None,
                   "calls": self.ai.calls if self.ai else 0, "fails": self.ai.fails if self.ai else 0},
            "view": self.view,
            "log": ["%02d:%02d %s" % (divmod(int(ts), 60) + (text,)) for ts, text, _, _ in self.logs[-12:]],
        }

    # ---- 그리기
    def crowd_label(self):
        r = self.ratio
        if r < 0.2:
            return "한산함", 250
        if r < 0.43:
            return "활기참", 120
        if r < 0.73:
            return "북적북적", 214
        return "인파 경보!", 196

    def issue_status(self, m):
        if m.state == "roam":
            return "배회 중", 203
        if m.state == "held" and m.claimed is not None:
            s = self.soldiers[m.claimed]
            word = {"walk": "출동", "shoo": "쫓는 중", "flag": "사람 호출"}.get(s.state, s.state)
            return "병정%d %s" % (m.claimed + 1, word), 117
        if m.state == "caged":
            return "담당자 대기", 226
        if m.state == "flee":
            return "쫓겨남", 245
        return m.state, 245

    def live_issues(self):
        order = {"held": 0, "roam": 1, "caged": 2, "flee": 3}
        live = [m for m in self.mice if m.state in order]
        return sorted(live, key=lambda m: (not m.priority, order[m.state], m.born))

    def issue_segs(self, m):
        st, sc = self.issue_status(m)
        return [("★" if m.priority else " ", 220), (pad(m.issue["id"], 12), 231, True),
                (pad(kind_word(m.issue), 13), KIND_COL.get(kind_word(m.issue), 141)), (pad(st, 15), sc),
                (m.issue["title"], 244)]

    def draw(self, view="traffic"):
        market, bugs = view == "traffic", view == "sentry"
        w, sh, t = self.w, self.sh, self.t
        gpx, bottom = self.gpx, self.bottom
        cv = PixelCanvas(w, sh)

        # 하늘 · 달 · 별 · 눈발
        for y in range(gpx):
            c = 17 if y < gpx * 0.55 else 18
            for x in range(w):
                cv.px[y][x] = c
        mx, my = w - 9, 6
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                if dx * dx + dy * dy <= 9 and (dx + 1.5) ** 2 + (dy + 0.5) ** 2 > 6:
                    cv.set(mx + dx, my + dy, 230)
        for x, y in self.stars:
            if (x * 7 + y + int(t * 2)) % 9:
                cv.set(x, y, 229 if (x + y) % 3 else 255)
        for f in self.snow:
            cv.set(f[0], f[1], 255 if f[2] > 5 else 252)
        # 전구 가랜드 (처지는 줄 + 반짝이는 전구)
        for x in range(w):
            sag = 1 if x % 8 in (3, 4, 5) else 0
            cv.set(x, 1 + sag, 22)
            if x % 8 == 4:
                cv.set(x, 3, LIGHTS[(x // 8 + int(t * 2)) % len(LIGHTS)])

        # 눈 덮인 광장
        for y in range(gpx, 2 * sh):
            row = cv.px[y]
            base = 255 if y < gpx + 2 else 254
            for x in range(w):
                row[x] = base
        for (x, y), c in self.ground_tex.items():
            cv.set(x, y, c)

        top = gpx - 8  # 노점·위병소·트리 지붕 픽셀 행 (높이 10px, 바닥 gpx+1)

        if market:
            self.draw_market(cv, top)
        if bugs:
            self.draw_bugs(cv, top)
        return cv

    def draw_bugs(self, cv, top):
        w, gpx, bottom = self.w, self.gpx, self.bottom
        self.scene_hits = []  # (x0, x1, 행0, 행1, 동작): 장면 안 칸 좌표(0부터), compose 가 화면 좌표로 옮긴다
        # 위병소 (SENTRY box)
        bx = 1
        cv.rect(bx + 1, top, 6, 1, 255)
        cv.rect(bx, top + 1, 8, 3, 124)
        for i in range(8):
            if i % 3 == 1:
                cv.set(bx + i, top + 2, 220)
        cv.rect(bx, top + 6, 8, 4, 94)
        cv.rect(bx + 2, top + 6, 4, 4, 234)
        cv.put(bx, (top + 4) // 2, " SENTRY ", 226, 22, True)
        if not self.soldiers[0].busy:
            cv.blit(["YYY", "SSS", "kWk"[:3], "RYR"], bx + 2.5, top + 6, {"Y": 220, "S": 223, "k": 16, "W": 255, "R": 160})

        # 쥐
        for m in sorted(self.mice, key=lambda m: m.y):
            if m.state == "caged":
                continue
            if m.state in ("dead", "held"):
                iid = m.issue["id"]
                self.scene_hits.append((int(m.x), int(m.x) + 14, max(0, (m.y - 5) // 2 - 1), m.y // 2,
                                        lambda iid=iid: self.open_history(iid)))
            if m.state == "dead":
                cv.blit(RAT_DEAD, m.x, m.y - 4, RAT_PAL, flip=m.dir < 0)
                continue
            if m.state == "held":
                jx = R.choice((-1, 0, 0, 1))
                cv.blit(RAT_FRONT, m.x + 3 + jx, m.y - 5, RAT_PAL)
                cv.put(m.x, (m.y - 5) // 2 - 1, "!" + m.issue["id"], 160, None, True)
                continue
            frame = RAT_WALK[int(m.step * 6) % 2]
            cv.blit(frame, m.x, m.y - 4, RAT_PAL, flip=m.dir < 0)

        # 우리(needs-human 대기열) - 위병소 아래
        caged = [m for m in self.mice if m.state == "caged"]
        kx, ky = 0, gpx + 3
        cv.rect(kx, ky, 11, 8, 254)
        if caged:
            cv.blit(RAT_FRONT, kx + 1, ky + 1, RAT_PAL)
        for i in range(0, 11, 2):
            cv.rect(kx + i, ky, 1, 8, 178)
        cv.rect(kx, ky, 11, 1, 136)
        cv.rect(kx, ky + 7, 11, 1, 136)
        cv.put(kx, (ky + 8) // 2 + 1, "HUMAN %d" % len(caged), 16 if caged else 244, None, bool(caged))

        # 병정
        for s in self.soldiers:
            if not s.busy:
                continue
            x, y = s.x, bottom - Soldier.H + 1
            walking = s.state in ("walk", "return") and int(s.phase * 8) % 2
            spr = SOLDIER[:12] + (SOLDIER_STEP if walking else SOLDIER[12:])
            cv.blit(spr, x, y, SOLDIER_PAL, flip=s.face < 0)
            if s.state == "fix":
                hand_y = y + 9
                swing = int(s.phase * 8) % 2
                for k in range(5):
                    if swing:  # 찌르기
                        px, py = (x + 7 + k, hand_y) if s.face > 0 else (x - 1 - k, hand_y)
                    else:  # 치켜들기
                        px, py = (x + 6 + k, hand_y - 1 - k) if s.face > 0 else (x - k, hand_y - 1 - k)
                    cv.set(px, py, 220 if k == 0 else 255)
            row = y // 2 - 1
            if s.bubble:
                text = " %s " % s.bubble
                bxx = min(max(0, int(x) + 3 - len(text) // 2), w - len(text))
                cv.put(bxx, row, text, 16, 230, True)
                row -= 1
            if s.speech_t > 0:  # 명령에 대한 대답
                text = " %s " % s.speech
                bxx = min(max(0, int(x) + 3 - dw(text) // 2), max(0, w - dw(text)))
                cv.put(bxx, row, text, 231, 53, True)
                row -= 1
            cv.put(x + 3, row, str(s.idx + 1), 25, None, True)

        # 위병소에서 대기 중인 병정의 대답은 위병소 위에
        row = max(0, top // 2 - 1)
        for s in self.soldiers:
            if not s.busy and s.speech_t > 0 and row >= 0:
                cv.put(1, row, " %d %s: %s " % (s.idx + 1, s.name, s.speech), 231, 53, True)
                row -= 1

        self.draw_trophy_cage(cv, top)

        for p in self.particles:
            cv.set(p[0], p[1], p[4])

    def cage_box(self):
        """뒤쪽(지평선 위) 잡은 쥐 우리: (x0, 윗 픽셀 행, 폭, 높이, 칸 수, 한 줄 칸 수)."""
        cw_ = max(20, min(40, self.w // 3))
        x0, top = self.w - cw_ - 2, self.gpx - 9
        per_row = (cw_ - 2) // 7
        return x0, top, cw_, 10, per_row * 2, per_row

    def cage_slot(self, i):
        x0, top, cw_, ch, cap, per_row = self.cage_box()
        i %= cap
        return x0 + 2 + (i % per_row) * 7, top + 2 + (i // per_row) * 4

    def draw_trophy_cage(self, cv, top):
        """처치한 쥐는 뒤쪽 우리에, 각각 무슨 버그였는지는 하늘의 게시판에."""
        w = self.w
        x0, ctop, cw_, ch, cap, per_row = self.cage_box()
        fixed = [r for r in self.history if r["outcome"] == "fixed"]
        landed = len(fixed) - len(self.flying)
        cv.rect(x0, ctop, cw_, ch, 187)  # 우리 안: 짚 바닥 (회색 쥐가 잘 보이게)
        for i in range(x0, x0 + cw_, 2):
            cv.set(i, ctop + ch - 2, 180)
        shown = fixed[:max(0, landed)][-cap:]
        base = max(0, landed - cap)
        for k, _ in enumerate(shown):
            sx, sy = self.cage_slot(base + k)
            cv.blit(RAT_MINI, sx, sy, RAT_PAL)
        for i in range(0, cw_, 7):  # 창살 (쥐 칸 사이마다)
            cv.rect(x0 + i, ctop, 1, ch, 136)
        cv.rect(x0, ctop, cw_, 1, 136)
        cv.rect(x0, ctop + ch - 1, cw_, 1, 136)
        cv.rect(x0 - 1, ctop - 1, cw_ + 2, 1, 255)  # 지붕 눈
        label = "🏆 잡은 쥐 %d" % len(fixed) + (" (+%d)" % (landed - cap) if landed > cap else "")
        cv.put(x0 + max(0, (cw_ - dw(label)) // 2), max(0, (ctop - 2) // 2), label, 226, 52, True)
        self.scene_hits.append((x0, x0 + cw_, max(0, (ctop - 2) // 2), (ctop + ch) // 2, lambda: self.open_history()))
        # 날아가는 쥐: 처치한 자리에서 포물선을 그리며 우리 안 자리로
        for fx, fy, slot, t in self.flying:
            k = min(1.0, t / 1.2)
            tx, ty = self.cage_slot(slot)
            x = fx + (tx - fx) * k
            y = fy + (ty - fy) * k - 14 * math.sin(math.pi * k)
            cv.blit(RAT_MINI, x, y, RAT_PAL)

        # 게시판: SENTRY 위병소 오른쪽 ~ 우리 왼쪽, 지평선 위 하늘에 최근 처리한 쥐들
        bx0, bx1 = 11, x0 - 2
        rows = list(range(2, max(2, top // 2 - 1)))
        if bx1 - bx0 < 24 or len(rows) < 2:
            return
        bw = bx1 - bx0
        cv.put(bx0, rows[0], pad(" 📋 잡은 쥐 게시판 · 클릭하면 기록", bw), 230, 238, True)
        recent = list(reversed(self.history))[:len(rows) - 1]
        if not recent:
            cv.put(bx0, rows[1], pad("  아직 없음 - 병정이 처리하면 여기에 붙는다", bw), 244, 234)
        for r, row in zip(recent, rows[1:]):
            tag, col = OUTCOME_TAG[r["outcome"]]
            if r["mr"]:
                tag = "✔ !%d" % r["mr"]
            text = " %s %s %d%s %s" % (pad(tag, 8), pad(r["id"], 10), r["soldier"],
                                         pad(SOLDIER_NAMES[(r["soldier"] - 1) % len(SOLDIER_NAMES)], 7), r["title"])
            sel = r["id"] == self.hist_sel
            cv.put(bx0, row, fit_text(text, bw), col if not sel else 231, 24 if sel else 234, sel)
            self.scene_hits.append((bx0, bx1, row, row, lambda iid=r["id"]: self.open_history(iid)))

    def draw_market(self, cv, top):
        w, gpx, t = self.w, self.gpx, self.t
        # 노점
        for x, name, (c1, c2) in self.booths:
            bw = len(name) + 4
            cv.rect(x + 1, top, bw - 2, 1, 255)  # 지붕 눈
            for i in range(bw):
                c = c1 if (i // 2) % 2 == 0 else c2
                cv.set(x + i, top + 1, c)
                cv.set(x + i, top + 2, c)
                if (i // 2) % 2 == 0:
                    cv.set(x + i, top + 3, c)
            cv.rect(x, top + 4, bw, 2, 94)
            cv.put(x, (top + 4) // 2, name.center(bw), 230, 94, True)
            cv.rect(x, top + 6, bw, 1, 52)
            for i in range(1, bw - 1, 2):
                cv.set(x + i, top + 6, LIGHTS[(i + x) % len(LIGHTS)] if name in ("ORNAMENTS", "TOYS", "CANDLES") else 230)
            cv.rect(x, top + 7, bw, 1, 180)
            cv.rect(x, top + 8, bw, 1, 130)
            cv.set(x, top + 9, 94)
            cv.set(x + bw - 1, top + 9, 94)

        # 트리
        th = min(20, gpx - 2)
        cx, tb = self.tree_cx, gpx + 1
        cone = th - 3
        for i in range(cone):
            y = tb - 2 - cone + 1 + i
            half = int((i + 1) * 0.62)
            for dx in range(-half, half + 1):
                h = (dx * 7 + i * 13) % 11
                c = 22 if abs(dx) == half else (34 if h == 0 else 28)
                if abs(dx) < half and (dx * 3 + i * 7) % 6 == 0:
                    c = LIGHTS[(dx + i + int(t * 3)) % len(LIGHTS)]
                cv.set(cx + dx, y, c)
        star_y = tb - 2 - cone
        sc = 226 if int(t * 3) % 2 else 220
        for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1)):
            cv.set(cx + dx, star_y + dy, sc)
        cv.rect(cx - 1, tb - 1, 3, 2, 94)

        # 상점: 줄 맨 앞 손님이 카운터에서 계산하고 가게 안으로 들어간다
        gx, gy, bottom = self.gate_x, self.bottom - 5, self.bottom
        sx, sw = gx - 1, w - gx + 1  # 가게 왼쪽 끝 · 폭
        cv.rect(sx, gy - 6, sw, bottom - gy + 7, 52)  # 안쪽 벽
        for k, yy in enumerate((gy - 4, gy - 1)):  # 선반 + 상품
            cv.rect(sx + 1, yy + 1, sw - 2, 1, 94)
            for i in range(sx + 1, w - 1):
                if (i + k) % 2 == 0:
                    cv.set(i, yy, LIGHTS[(i * 3 + k) % len(LIGHTS)])
        kx = sx + sw // 2  # 산타 모자를 쓴 점원
        cv.set(kx, gy - 1, 255)
        cv.rect(kx - 1, gy, 3, 1, 196)
        cv.rect(kx - 1, gy + 1, 3, 2, 223)
        cv.set(kx - 1, gy + 1, 16)
        cv.set(kx + 1, gy + 1, 16)
        for x, y, pal, st in self.passing:  # 계산 마친 손님 (카운터 뒤로 들어간다)
            cv.blit(VISITOR[:4] + (VISITOR_STEP if int(st * 6) % 2 else VISITOR[4:]), x, y, pal)
        cv.rect(sx, gy + 3, sw, bottom - gy - 2, 130)  # 카운터
        cv.rect(sx, gy + 3, sw, 1, 180)
        for i in range(sw):  # 줄무늬 차양 + 물결 끝
            c = 196 if (i // 2) % 2 == 0 else 255
            cv.rect(sx + i, gy - 9, 1, 3, c)
            if i % 2 == 0:
                cv.set(sx + i, gy - 6, c)
        cv.rect(sx, gy - 6, 1, bottom - gy + 7, 94)  # 기둥
        cv.rect(w - 1, gy - 6, 1, bottom - gy + 7, 94)
        sign = "SHOP"
        cv.rect(sx, gy - 12, sw, 3, 22)  # 간판 판자: 차양 바로 위에 붙인다
        cv.put(sx + (sw - len(sign)) // 2, (gy - 11) // 2, sign, 226, 22, True)
        for p in self.particles:
            cv.set(p[0], p[1], p[4])

        # 대기열 (위 줄 먼저 그려 앞줄이 위에 겹친다)
        people = [(self.qpos(p.d), p) for p in self.queue]
        for (x, y), p in sorted(people, key=lambda e: e[0][1]):
            if x < -3:
                continue
            cv.blit(VISITOR[:4] + (VISITOR_STEP if p.walking else VISITOR[4:]), x, y, p.pal)
        off = self.queue_offscreen()
        if off:
            tail_y = self.qpts[-1][1]
            cv.put(0, tail_y // 2 - 1, "◀ +%d명" % off, 231, 160, True)


# ------------------------------------------------------------------ 화면 구성

SPARK = " ▁▂▃▄▅▆▇█"


def spark(vals, n, top):
    vals = vals[-n:]
    s = "".join(SPARK[min(8, int(v / top * 8))] if top else " " for v in vals)
    return s.rjust(n)


def bars(vals, n, top, rows):
    """여러 줄 막대 그래프. 위 줄부터 문자열 리스트. 표본이 적으면 가로로 늘린다."""
    k = max(1, n // max(1, len(vals))) if vals else 1
    cols = [v for v in vals for _ in range(k)][-n:]
    cols = [None] * (n - len(cols)) + cols
    out = []
    for r in range(rows - 1, -1, -1):
        line = ""
        for v in cols:
            lv = 0 if v is None else int(min(1.0, v / top) * rows * 8)
            line += SPARK[max(0, min(8, lv - r * 8))]
        out.append(line)
    return out


VIEWS = [("overview", "0 개요"), ("traffic", "1 🎄 트래픽"), ("sentry", "2 🐭 Sentry")]


def header(world, w, paused):
    """맨 윗줄: 제목 + 탭(클릭 가능). 탭 위치를 world.hits 에 기록한다."""
    title = [("  🎄 MERRY BUGMAS " if w >= 100 else " 🎄 ", 226, True), (" %s " % world.svc[0], 16, True, 223), ("  ", 7)]
    segs, x = list(title), seg_width(title) + 1
    label, _ = world.crowd_label()
    blink = int(world.t * 2) % 2
    live = sum(1 for m in world.mice if m.state in ("roam", "held"))
    menu = " ◀ 메뉴 "
    segs += [(menu, 230, False, 88), (" ", 7)]
    world.hits.append((x, x + dw(menu) - 1, 1, 1, lambda: world.set_view("menu")))
    x += dw(menu) + 1
    for v, name in VIEWS:
        text = " %s " % name
        if v == "sentry":
            text = " %s %d%s " % (name, live, " NEW+%d" % world.unseen if world.unseen else "")
        if v == world.view:
            st = (16, True, 230)
        elif v == "sentry" and world.unseen and blink or v == "traffic" and label == "인파 경보!" and blink:
            st = (231, True, 160)
        else:
            st = (230, False, 238)
        segs.append((text, st[0], st[1], st[2]))
        world.hits.append((x, x + dw(text) - 1, 1, 1, lambda v=v: world.set_view(v)))
        x += dw(text)
        segs.append((" ", 7))
        x += 1
    right = [("%s%s  %s  " % ("|| PAUSED  " if paused else "", "AUTO" if world.auto else "MANUAL",
                              "LIVE SENTRY" if world.live is not None else "MOCK"), 223)]
    gap = w - seg_width(segs) - seg_width(right)
    return seg_line(segs + [(" " * max(0, gap), 7)] + right, w, 52)


def log_lines(world, w, h, cats, keys):
    lines = [seg_line([("  작전 로그 ", 230, True), ("- " + keys, 244)], w, 238)]
    logs = [l for l in world.logs if l[3] in cats][-(h - 1):]
    for ts, text, col, _ in logs:
        mm, ss = divmod(int(ts), 60)
        lines.append(seg_line([("  %02d:%02d " % (mm, ss), 242), (text, col)], w, 233))
    while len(lines) < h:
        lines.append(seg_line([], w, 233))
    return lines


def card(width, height, title, hint, body, bc):
    """테두리 카드 하나를 seg 리스트의 줄들로."""
    left = [("┌─ ", bc)] + title + [(" ", bc)]
    right = [(" %s " % hint, 244), ("─┐", bc)]
    fill = width - seg_width(left) - seg_width(right)
    if fill < 1:
        right, fill = [("─┐", bc)], width - seg_width(left) - 2
    rows = [fit(left + [("─" * max(0, fill), bc)] + right, width)]
    iw = width - 4
    for i in range(height - 2):
        segs = body[i] if i < len(body) else []
        rows.append([("│ ", bc)] + fit(segs, iw) + [(" │", bc)])
    rows.append([("└" + "─" * (width - 2) + "┘", bc)])
    return rows


def traffic_card(world, width, height):
    label, lcol = world.crowd_label()
    peak = max(world.hist[-60:] or [world.rps])
    alert = label == "인파 경보!"
    iw = width - 4
    body = [
        [],
        [("  %s" % format(int(world.rps), ","), 231, True), (" rps    ", 245), (label, lcol, True)],
        [("  상점 대기열 %d명" % len(world.queue), 250),
         ("  (화면 밖 +%d)" % world.queue_offscreen() if world.queue_offscreen() else "", 203)],
        [("  30초 최고 %s rps" % format(int(peak), ","), 245)],
        [],
    ]
    rows = max(2, min(12, height - 2 - len(body) - 2))
    body += [[("  " + r, 51)] for r in bars(world.hist, iw - 4, world.peak, rows)]
    body.append([("  " + pad("최근 30초", iw - 15) + "max " + format(int(world.peak), ","), 242)])
    title = [("1  🎄 트래픽", 231, True), (" · Grafana", 245)]
    return card(width, height, title, "클릭 ▸ 대기열", body, 196 if alert else 37)


def sentry_card(world, width, height):
    live = [m for m in world.mice if m.state in ("roam", "held")]
    roam = sum(1 for m in live if m.state == "roam")
    caged = sum(1 for m in world.mice if m.state == "caged")
    out = sum(1 for s in world.soldiers if s.busy)
    st = world.stats
    body = [
        [],
        [("  미해결 쥐 ", 250), (str(len(live)), 203, True), ("    우리(사람) ", 250), (str(caged), 226, True),
         ("    병정 출동 ", 250), ("%d/%d" % (out, len(world.soldiers)), 117, True)],
        [("  MR ", 250), (str(st["fixed"]), 46, True), ("    noise %d    block %d" % (st["noise"], st["blocked"]), 245)],
        [],
    ]
    body += [[("  " + r, 203)] for r in bars(world.ehist, width - 8, 8, 2)]
    body += [[], [(ISSUE_HEADER, 242)]]
    items = world.live_issues()
    room = height - 2 - len(body)
    if not items:
        body.append([("   쥐 없음 - 병정은 위병소에서 대기 중", 46)])
    shown = items if len(items) <= room else items[:max(0, room - 1)]
    body += [world.issue_segs(m) for m in shown]
    if len(shown) < len(items):
        body.append([("   … 외 %d건" % (len(items) - len(shown)), 244)])
    bc = 196 if roam >= 3 else (208 if live else 34)
    title = [("2  🐭 Sentry", 231, True), (" · sentry-bot", 245)]
    return card(width, height, title, "클릭 ▸ 전투", body, bc)


def compose_overview(world, w, h, paused):
    """정적 개요(1단계). 움직이는 그림 없이 숫자·그래프·목록만."""
    lines = [header(world, w, paused)]
    bh = h - 1 - LOG_H
    if w >= 100:
        lw = (w - 3) * 2 // 5
        rw = w - 3 - lw
        L, Rr = traffic_card(world, lw, bh), sentry_card(world, rw, bh)
        for a, b in zip(L, Rr):
            lines.append(seg_line([(" ", 7)] + a + [(" ", 7)] + b, w, 233))
        world.hits += [(2, 1 + lw, 2, 1 + bh, lambda: world.set_view("traffic")),
                       (3 + lw, 2 + lw + rw, 2, 1 + bh, lambda: world.set_view("sentry"))]
    else:
        th = max(8, bh * 2 // 5)
        for row in traffic_card(world, w - 2, th) + sentry_card(world, w - 2, bh - th):
            lines.append(seg_line([(" ", 7)] + row, w, 233))
        world.hits += [(1, w, 2, 1 + th, lambda: world.set_view("traffic")),
                       (1, w, 2 + th, 1 + bh, lambda: world.set_view("sentry"))]
    keys = "[Esc]메뉴 [1]/클릭 트래픽 [2]/클릭 Sentry [t]급증 [b]쥐 떼 [space]정지 [q]종료"
    return lines + log_lines(world, w, LOG_H, ("all", "traffic", "sentry"), keys)


def title_canvas(world, w, h, density=1.0):
    """타이틀·메뉴 배경: 밤하늘 + 눈발 + 전구 가랜드 + 아래 눈밭(6px)."""
    if world.tsize != (w, h):
        world.tsize = (w, h)
        world.tsnow = [[R.uniform(0, w), R.uniform(0, 2 * h), R.uniform(3, 8), R.uniform(-1.5, 1.5)]
                       for _ in range(w * h // 6)]
    cv = PixelCanvas(w, h)
    H = 2 * h
    ground = H - 6
    for y in range(H):
        c = 17 if y < H * 0.55 else 18
        if y >= ground:
            c = 255 if y == ground else 254
        cv.px[y] = [c] * w
    for f in world.tsnow[:int(len(world.tsnow) * density)]:
        if f[1] < ground:
            cv.set(f[0], f[1], 255 if f[2] > 5 else 252)
    t = world.intro_t
    for x in range(w):
        cv.set(x, 1 + (1 if x % 8 in (3, 4, 5) else 0), 22)
        if x % 8 == 4:
            cv.set(x, 3, LIGHTS[(x // 8 + int(t * 2)) % len(LIGHTS)])
    return cv, ground


def compose_intro(world, w, h):
    """게임 타이틀: 눈발이 점점 굵어지다가 가운데가 걷히며 MERRY BUGMAS 가 나온다. 아래에선 병정이 쥐를 쫓는다."""
    t = world.intro_t
    cv, ground = title_canvas(world, w, h, 1.0 if t >= INTRO_SNOW else (t / INTRO_SNOW) ** 2)
    H = 2 * h

    px = 2 if w >= 76 else 1
    rows = logo_rows("MERRY", px, px) + [""] + logo_rows("BUGMAS", px, px)
    colors = LOGO_COLORS[0] + [0] + LOGO_COLORS[1]
    lw = max(len(r) for r in rows)
    top = max(1, (h - 8 - len(rows)) // 2)
    rv = (t - INTRO_SNOW) / INTRO_REVEAL  # 0 → 1: 가운데부터 좌우로 걷힌다
    if rv > 0:
        cx, half = w / 2.0, lw / 2.0 + 3
        opened = [x for x in range(w) if abs(x - cx) <= min(1.0, rv) * half]
        for y in range(2 * (top - 1), 2 * (top + len(rows) + 1)):
            for x in opened:
                cv.set(x, y, 16)
        for i, row in enumerate(rows):
            left = (w - len(row)) // 2
            for j, ch in enumerate(row):
                if ch == " " or abs(left + j - cx) > min(1.0, rv) * half:
                    continue
                col = 231 if rv < 1 else colors[i]
                if rv >= 1 and (j * 7 + i * 13 + int(t * 5)) % 41 == 0:  # 반짝
                    col = 226
                cv.put(left + j, top + i, ch, col, 16)

    y = top + len(rows) + 2
    if rv >= 1:
        for k in range(2):  # 부제는 어두운 띠 위에: 눈발이 글자 사이로 지나가지 않게
            for x in range(w):
                cv.set(x, 2 * (y + k), 16)
                cv.set(x, 2 * (y + k) + 1, 16)
        for k, (text, col) in enumerate((("🎄 Ops Christmas Market 🎄", 223),
                                         ("Sentry 쥐를 잡고, 트래픽 광장을 지켜라!", 250))):
            cv.put((w - dw(text)) // 2, y + k, text, col)
        if int(t * 2) % 2 == 0:
            press = "▶  PRESS ENTER  ◀"
            cv.put((w - dw(press)) // 2, y + 3, press, 226, None, True)

    # 아래 눈밭: 쥐가 도망가고 병정이 쫓는다
    mx = (t * 16) % (w + 40) - 20
    cv.blit(RAT_WALK[int(t * 8) % 2], mx, H - 7, RAT_PAL)
    spr = SOLDIER[:12] + (SOLDIER_STEP if int(t * 8) % 2 else SOLDIER[12:])
    cv.blit(spr, mx - 13, H - 16, SOLDIER_PAL)
    foot = " Enter 시작 · q 종료 · mock 데이터 PoC "
    cv.put(w - dw(foot) - 1, h - 1, foot, 244, 254)
    world.hits.append((1, w, 1, h, lambda: world.set_view("menu")))
    return cv.lines()


def hl(segs, width, bg):
    """줄 전체를 bg 로 칠한 seg 목록."""
    out = [(s[0], s[1], len(s) > 2 and s[2], bg) for s in fit(segs, width)]
    return out


def compose_menu(world, w, h):
    """스테이지 고르기: ① 서비스 → ② 시나리오 → START."""
    BG = 17
    blink = int(world.intro_t * 2) % 2
    lines = [seg_line([("  🎄 MERRY BUGMAS ", 226, True), (" ·  SELECT STAGE", 223)], w, 52)]
    if h >= 30 and w >= 72:  # 작은 로고
        m, b = logo_rows("MERRY", 1, 1), logo_rows("BUGMAS", 1, 1)
        left = (w - len(m[0]) - 3 - len(b[0])) // 2
        lines.append(seg_line([], w, BG))
        for i in range(5):
            lines.append(seg_line([(" " * left, 7), (m[i], LOGO_COLORS[0][i]), ("   ", 7), (b[i], LOGO_COLORS[1][i])], w, BG))
    lines.append(seg_line([], w, BG))

    svc, scn = SERVICES[world.svc_i], SCENARIOS[world.scn_i]
    side = w >= 96
    lw = (w - 3) // 2 if side else w - 2
    rw = w - 3 - lw if side else w - 2
    focus = (220, 240) if world.menu_col == 0 else (240, 220)

    def pick_row(sel, focused, segs, width):
        mark = [("▶ " if sel else "  ", 226, True)]
        return hl(mark + segs, width, 24) if sel and focused else mark + segs

    # ① 서비스
    lbody, lhits = [[]], []
    for i, s in enumerate(SERVICES):
        sel = i == world.svc_i
        lhits.append((len(lbody), i))
        lbody.append(pick_row(sel, world.menu_col == 0, [
            (pad(s[0], 9), 231 if sel else 250, sel), (pad(s[1], 11), 223 if sel else 245),
            ("🎄", 7), (stars(s[3]), 214), ("  🐭", 7), (stars(s[4] / 1.4), 203)], lw - 4))
    if side:
        lbody += [[], [("  %s · %s" % (svc[1], svc[2]), 250)], [("  🎄 트래픽 규모  🐭 버그 빈도", 242)]]
    else:  # 위아래로 쌓는 좁은 화면: 설명 줄을 빼서 START 까지 들어가게
        lbody, lhits = lbody[1:], [(k - 1, i) for k, i in lhits]

    # ② 시나리오
    rbody, rhits = ([[]] if side else []), []
    for j, c in enumerate(SCENARIOS):
        sel = j == world.scn_i
        rhits.append((len(rbody), j))
        rbody.append(pick_row(sel, world.menu_col == 1, [
            (pad(c[1], 17), 231 if sel else 250, sel), (c[2], 223 if sel else 245)], rw - 4))
    rbody.append([])
    rbody += [[("  " + d, 250)] for d in scn[3]]
    rbody.append([])
    start_row = len(rbody)
    on = world.menu_col == 1
    rbody.append([("  ", 7), (" ▶ START  %s · %s " % (svc[0], scn[1]), 16 if on else 250, True,
                              (226 if blink else 220) if on else 238)])

    L = card(lw, len(lbody) + 2, [("① 서비스", 231, True)], "↑↓  Enter", lbody, focus[0])
    Rr = card(rw, len(rbody) + 2, [("② 시나리오", 231, True)], "Enter 시작", rbody, focus[1])
    y0 = len(lines) + 1  # 카드 첫 줄의 화면 행(1부터)

    def pick_svc(i):
        world.svc_i, world.menu_col = i, 1

    def pick_scn(j):
        world.scn_i, world.menu_col = j, 1
        world.menu_enter()

    if side:
        rows = max(len(L), len(Rr))
        L += [[]] * (rows - len(L))
        Rr += [[]] * (rows - len(Rr))
        for a, b in zip(L, Rr):
            lines.append(seg_line([(" ", 7)] + fit(a, lw) + [(" ", 7)] + b, w, BG))
        lx, rx, ry0 = (2, 1 + lw), (3 + lw, 2 + lw + rw), y0
    else:
        for row in L + Rr:
            lines.append(seg_line([(" ", 7)] + row, w, BG))
        lx = rx = (2, 1 + lw)
        ry0 = y0 + len(L)
    for k, i in lhits:
        world.hits.append((lx[0], lx[1], y0 + 1 + k, y0 + 1 + k, lambda i=i: pick_svc(i)))
    for k, j in rhits:
        world.hits.append((rx[0], rx[1], ry0 + 1 + k, ry0 + 1 + k, lambda j=j: pick_scn(j)))
    world.hits.append((rx[0], rx[1], ry0 + 1 + start_row, ry0 + 1 + start_row,
                       lambda: world.start_stage(world.svc_i, SCENARIOS[world.scn_i][0])))

    keys = "  [↑↓] 고르기  [←→·Tab] 서비스↔시나리오  [Enter] 다음/시작  [Esc] 뒤로  [q] 종료"
    gap = (h - 1 - len(lines)) // 2  # 가운데 맞춤: 제목 줄 아래에 빈 줄을 끼운다
    if gap > 0:
        lines[1:1] = [seg_line([], w, BG)] * gap
        world.hits = [(a, b, c + gap, d + gap, f) for a, b, c, d, f in world.hits]
    while len(lines) < h - 1:
        lines.append(seg_line([], w, BG))
    return lines[:h - 1] + [seg_line([(keys, 244)], w, 238)]


def issue_lines(world, w, h):
    """전투 장면 아래 이슈 목록."""
    items = world.live_issues()
    lines = [seg_line([(ISSUE_HEADER, 242)], w, 236)]
    room = h - 1
    if not items:
        lines.append(seg_line([("   쥐 없음 - 병정은 위병소에서 대기 중", 46)], w, 234))
    shown = items if len(items) <= room else items[:room - 1]
    lines += [seg_line(world.issue_segs(m), w, 234) for m in shown]
    if len(shown) < len(items):
        lines.append(seg_line([("   … 외 %d건" % (len(items) - len(shown)), 244)], w, 234))
    # 남는 줄엔 최근에 잡은 쥐가 무슨 버그였는지
    for r in list(reversed(world.history))[:max(0, h - len(lines))]:
        tag, col = OUTCOME_TAG[r["outcome"]]
        if r["mr"]:
            tag = "✔ MR !%d" % r["mr"]
        lines.append(seg_line([(" ", 7), (pad(r["id"], 12), 245), (pad(r["kind"], 13), 240),
                               (pad("%s · %d" % (tag, r["soldier"]), 15), col), (r["title"], 240)], w, 233))
    while len(lines) < h:
        lines.append(seg_line([], w, 234))
    return lines[:h]


OUTCOME_TAG = {"fixed": ("✔ MR", 46), "noise": ("✖ noise", 245), "human": ("! 사람", 226)}


def history_lines(world, w, n):
    """잡은 쥐 기록 창 n 줄: 지금까지 병정들이 처리한 Sentry 이슈 (최근 것부터)."""
    BG = 235
    hist = world.history
    cnt = {k: sum(1 for r in hist if r["outcome"] == k) for k in OUTCOME_TAG}
    per = "  ".join("%d %s %d건" % (s.idx + 1, s.name, sum(1 for r in hist if r["soldier"] == s.idx + 1))
                    for s in world.soldiers)
    lines = [seg_line([("  🏆 잡은 쥐 기록 ", 226, True), ("총 %d건 " % len(hist), 231, True),
                       ("· MR %d · noise %d · 사람 %d" % (cnt["fixed"], cnt["noise"], cnt["human"]), 250),
                       ("   │ " + per, 117), ("   [r·x·클릭] 닫기", 242)], w, 52)]
    lines.append(seg_line([(" " + pad("결과", 10) + pad("ID", 12) + pad("판정", 13) + pad("병정", 12) + pad("시각", 7)
                            + "AI 판정 근거 │ Sentry 제목", 242)], w, 237))
    live = [m for m in world.mice if m.state == "held" and m.issue["id"] == world.hist_sel]
    rows = []
    for m in live:  # 클릭한 쥐가 아직 처리 중이면 맨 위에
        s = world.soldiers[m.claimed]
        rows.append(([(" " + pad("… 처리 중", 10), 117, True), (pad(m.issue["id"], 12), 231, True),
                      (pad(kind_word(m.issue), 13), KIND_COL.get(kind_word(m.issue), 141)),
                      (pad("%d %s" % (s.idx + 1, s.name), 12), 117), (pad("지금", 7), 250), (m.issue["title"], 250)], True))
    for r in reversed(hist):
        tag, col = OUTCOME_TAG[r["outcome"]]
        if r["mr"]:
            tag = "✔ !%d" % r["mr"]
        mm, ss = divmod(int(r["t"]), 60)
        sel = r["id"] == world.hist_sel
        rows.append(([(" " + pad(tag, 10), col, True), (pad(r["id"], 12), 231, sel),
                      (pad(r["kind"], 13), KIND_COL.get(r["kind"], 250)),
                      (pad("%d %s" % (r["soldier"], SOLDIER_NAMES[(r["soldier"] - 1) % len(SOLDIER_NAMES)]), 12), 250),
                      (pad("%02d:%02d" % (mm, ss), 7), 244),
                      (("🧠 %s  │ " % r["note"] if r.get("note") else "") + r["title"], 250 if sel else 244)], sel))
    if not rows:
        rows.append(([("   아직 잡은 쥐가 없습니다 - 병정이 처리하면 여기에 쌓입니다", 244)], False))
    room = n - len(lines)
    # 강조한 줄이 창 밖이면 그 줄이 보이도록
    first = next((i for i, (_, sel) in enumerate(rows) if sel), 0)
    start = max(0, first - room + 1)
    for segs, sel in rows[start:start + room]:
        lines.append(seg_line(segs, w, 24 if sel else BG))
    while len(lines) < n:
        lines.append(seg_line([], w, BG))
    return lines[:n]


def compose(world, w, h, paused=False):
    world.hits = []
    if w < 60 or h < 22:
        msg = "60x22 이상 필요 (현재 %dx%d)" % (w, h)
        return [seg_line([(msg, 231)], w, 52)] + [" " * w] * (h - 1)
    if world.phase == "intro":
        return compose_intro(world, w, h)
    if world.phase == "menu":
        return compose_menu(world, w, h)
    log_h = LOG_H if h >= 28 else 4
    if world.view == "overview":
        world.layout(w, h - 3 - LOG_H)
        return compose_overview(world, w, h, paused)[:h]

    lines = [header(world, w, paused)]
    n = max(10, w - 24)
    if world.view == "traffic":
        world.layout(w, h - 3 - log_h)
        label, lcol = world.crowd_label()
        off = world.queue_offscreen()
        lines.append(seg_line([
            ("  🎄 상점 대기열 ", 250), ("%d명" % len(world.queue), 231, True),
            ("  화면 밖 +%d" % off if off else "", 203, True),
            ("  │ ", 240), ("%s rps  " % format(int(world.rps), ","), 231, True), (label, lcol, True),
            ("  │ 계산 완료 %d명" % world.admitted, 245), ("   (rps가 오르면 줄이 길어진다)", 242)], w, 235))
        lines.append(seg_line([("  트래픽 30초 ", 250), (spark(world.hist, n, world.peak), 51)], w, 234))
        lines += world.draw("traffic").lines()
        keys = "[Esc]메뉴 [0]개요 [2]Sentry [t]트래픽 급증 [space]정지 [q]종료"
        cats = ("all", "traffic")
    else:
        list_h = 6 if h >= 30 else 4
        world.layout(w, h - 2 - list_h - log_h)
        live = [m for m in world.mice if m.state in ("roam", "held")]
        caged = sum(1 for m in world.mice if m.state == "caged")
        out = sum(1 for s in world.soldiers if s.busy)
        st = world.stats
        lines.append(seg_line([
            ("  🐭 남은 쥐 ", 250), (str(len(live)), 203, True), ("  우리 %d" % caged, 226),
            ("  │ 💂 출동 ", 250), ("%d/%d" % (out, len(world.soldiers)), 117, True),
            ("  │ ⭐ 처치(MR) ", 250), (str(st["fixed"]), 46, True),
            ("  noise %d  block %d" % (st["noise"], st["blocked"]), 245)], w, 235))
        off = len(lines)
        lines += world.draw("sentry").lines()
        world.hits += [(x0 + 1, x1 + 1, off + r0 + 1, off + r1 + 1, act) for x0, x1, r0, r1, act in world.scene_hits]
        lines += issue_lines(world, w, list_h)
        keys = "[Esc]메뉴 [0]개요 [1]트래픽 [b]쥐 떼 [n]쥐 1마리 [h]사람이 처리 [r]/쥐 클릭 잡은 쥐 기록 [q]종료"
        cats = ("all", "sentry")
    lines += log_lines(world, w, log_h, cats, keys)
    if world.show_hist:  # 잡은 쥐 기록 창: 머리줄 아래를 덮는다
        n = min(h - 3, max(8, h * 2 // 3))
        lines[1:1 + n] = history_lines(world, w, n)
        world.hits = [hh for hh in world.hits if hh[2] == 1 or hh[2] > 1 + n]
        world.hits.append((1, w, 2, 1 + n, lambda: world.open_history()))
    return lines[:h]


RUNTIME_DIR = os.environ.get("XMAS_MARKET_RUNTIME", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime"))


class Bridge:
    """채팅 쪽(Codex)과 주고받는 파일. cmd 는 한 줄 한 명령(append), state.json 은 1초마다 덮어쓴다."""

    def __init__(self, d):
        import json
        self.json = json
        os.makedirs(d, exist_ok=True)
        self.cmd = os.path.join(d, "cmd")
        self.state = os.path.join(d, "state.json")
        open(self.cmd, "a").close()
        self.pos = os.path.getsize(self.cmd)  # 시작 전에 쌓인 명령은 무시
        self.last = 0.0

    def read_commands(self):
        try:
            size = os.path.getsize(self.cmd)
            if size < self.pos:
                self.pos = 0
            if size == self.pos:
                return []
            with open(self.cmd, encoding="utf-8", errors="ignore") as f:
                f.seek(self.pos)
                data = f.read()
            self.pos = size
            return data.splitlines()
        except OSError:
            return []

    def write_state(self, world, paused):
        now = time.time()
        if now - self.last < 1.0:
            return
        self.last = now
        st = world.snapshot_state()
        st["paused"] = paused
        tmp = self.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            self.json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state)


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ------------------------------------------------------------------ 실행


def handle_key(world, ch, state):
    """ch 는 글자 하나 또는 방향키 이름(up/down/left/right)."""
    ch = ch.lower()
    if ch == "q":
        state["quit"] = True
        return
    if world.phase == "intro":  # 아무 키나 누르면 메뉴로
        world.set_view("menu")
        return
    if world.phase == "menu":
        if ch in ("up", "k", "w"):
            world.menu_move(-1)
        elif ch in ("down", "j", "s"):
            world.menu_move(1)
        elif ch == "left":
            world.menu_col = 0
        elif ch == "right":
            world.menu_col = 1
        elif ch == "\t":
            world.menu_col = 1 - world.menu_col
        elif ch in ("\r", "\n", " "):
            world.menu_enter()
        elif ch == "\x1b":
            world.menu_back()
        return
    if world.show_hist and ch in ("\x1b", "x", "r"):
        world.show_hist, world.hist_sel = False, None
        return
    if ch == "r":
        world.open_history()
    elif ch in ("\x1b", "m"):
        world.set_view("menu")
    elif ch == "0":
        world.set_view("overview")
    elif ch in ("1", "2"):
        world.set_view("traffic" if ch == "1" else "sentry")
    elif ch == "\t":
        order = [v for v, _ in VIEWS]
        world.set_view(order[(order.index(world.view) + 1) % len(order)])
    elif ch == " ":
        state["paused"] = not state["paused"]
    elif ch == "t":
        world.traffic_spike()
    elif ch == "b":
        world.outbreak()
    elif ch == "n":
        world.spawn_one()
    elif ch == "h":
        world.human_clears()
    elif ch == "a":
        world.auto = not world.auto
        world.log("자동 연출 %s" % ("ON" if world.auto else "OFF"), 245, "all")


MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
CSI = re.compile(r"\x1b\[[0-9;?<]*[A-Za-z~]")
ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}


def handle_input(world, buf, state):
    """키·마우스(SGR) 입력을 처리하고, 덜 들어온 이스케이프 시퀀스는 돌려준다."""
    i = 0
    while i < len(buf):
        if buf[i] != "\x1b":
            handle_key(world, buf[i], state)
            i += 1
            continue
        m = MOUSE.match(buf, i)
        if m:
            b, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if m.group(4) == "M" and b == 0:  # 왼쪽 클릭
                for x0, x1, y0, y1, action in list(world.hits):
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        action()
                        break
            i = m.end()
            continue
        c = CSI.match(buf, i)
        if c:
            seq = c.group(0)
            if len(seq) == 3 and seq[2] in ARROWS:  # 방향키 ESC [ A
                handle_key(world, ARROWS[seq[2]], state)
            i = c.end()
            continue
        if buf[i:i + 2] == "\x1bO" and i + 2 < len(buf):  # 방향키 (application 모드) ESC O A
            if buf[i + 2] in ARROWS:
                handle_key(world, ARROWS[buf[i + 2]], state)
            i += 3
            continue
        if i == len(buf) - 1:  # Esc 단독
            handle_key(world, "\x1b", state)
            i += 1
            continue
        if re.fullmatch(r"\x1b\[[0-9;<?]*", buf[i:]):  # 시퀀스가 잘려 들어옴
            return buf[i:]
        i += 1
    return ""


REPLAY_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings", "ai.jsonl")


def attach_ai(world, use_ai=True, replay=None):
    """OPENAI_API_KEY 가 있으면 병정에게 두뇌(OpenAI API)를 붙인다. replay 면 녹화된 실제 응답을 재생한다."""
    if use_ai and replay and ai is not None:
        try:
            world.ai = ai.ReplayBrain(replay)
            world.log("🧠 AI 녹화 재생 (%s) - 트리아지만 녹화본, 명령은 키워드" % world.ai.model, 141, "all")
            return
        except (OSError, ValueError) as e:
            world.log("녹화 재생 실패 (%s) - 키워드 모드" % type(e).__name__, 245, "all")
    if use_ai and ai is not None and ai.available():
        world.ai = ai.Brain()
        world.log("🧠 AI 병정 모드 (%s) - 명령 해석·트리아지·브리핑" % world.ai.model, 141, "all")
    else:
        world.log("키워드 모드 - OPENAI_API_KEY 를 설정하면 AI 병정이 된다", 245, "all")


def attach_live(world, feed=None):
    """--live: Sentry 미해결 이슈를 쥐로 쓴다. 설정이 없거나 첫 읽기가 실패하면 mock 으로 돈다."""
    if sentry_live is None or (feed is None and not sentry_live.configured()):
        world.live_fail = ".env 에 SENTRY_AUTH_TOKEN · SENTRY_ORG · SENTRY_PROJECT 필요"
        world.log("Sentry 실연동 실패 (%s) - mock 으로 진행" % world.live_fail, 203, "all")
        return False
    feed = feed or sentry_live.SentryFeed()
    try:
        n = feed.fetch()
    except Exception:  # noqa: BLE001
        world.live_fail = feed.last_error
        world.log("Sentry 실연동 실패 (%s) - mock 으로 진행" % world.live_fail, 203, "all")
        return False
    world.live = feed
    feed.start()
    world.log("🔌 Sentry 실연동 (%s/%s) - 미해결 이슈 %d건 읽음 · 읽기 전용 · 트래픽은 mock" % (
        feed.org, feed.project, n), 141, "all")
    return True


def run_interactive(skip_intro=False, use_ai=True, replay=None, live=False):
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    out = sys.stdout
    world = World()
    attach_ai(world, use_ai, replay)
    if live:
        attach_live(world)
    if skip_intro:
        world.set_view("menu")
    state = {"quit": False, "paused": False}
    last_size = None
    bridge = Bridge(RUNTIME_DIR)
    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h")  # 대체 화면 + 마우스 클릭 보고
        prev = time.time()
        pending, cache_key, frame = "", None, []
        while not state["quit"]:
            now = time.time()
            dt, prev = min(0.2, now - prev), now
            while select.select([fd], [], [], 0)[0]:
                data = os.read(fd, 1024).decode("utf-8", "ignore")
                if not data:
                    break
                pending = handle_input(world, pending + data, state)
            size = os.get_terminal_size()
            if size != last_size:
                out.write("\x1b[2J")
                last_size = size
            for line in bridge.read_commands():
                if line.strip().lower() in ("pause", "resume"):
                    state["paused"] = line.strip().lower() == "pause"
                else:
                    world.command(line)
            if not state["paused"]:
                world.update(dt)
            bridge.write_state(world, state["paused"])
            # 개요는 정적 화면: 1초에 한 번만 다시 그린다
            key = (world.view, size, state["paused"], int(world.t), len(world.logs), world.unseen)
            if world.phase != "play" or world.view != "overview" or key != cache_key:
                cache_key = key
                frame = compose(world, size.columns, size.lines, state["paused"])
                out.write("\x1b[H" + "\r\n".join(frame))
                out.flush()
            time.sleep(max(0.0, 1.0 / FPS - (time.time() - now)))
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?1000l\x1b[?1006l\x1b[0m\x1b[?25h\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


SGR = re.compile(r"\x1b\[([0-9;]*)m")
PANE_KEYS = {"return": "\r", "enter": "\r", "tab": "\t", "space": " ", "backspace": "\x1b", "delete": "\x1b"}


def frame_runs(lines):
    """compose() 가 만든 ANSI 줄을 [[글자, 전경, 배경, 굵게], ...] 줄 목록으로 (256색 번호)."""
    rows = []
    for line in lines:
        runs, st, pos = [], (7, 16, 0), 0
        for m in list(SGR.finditer(line)) + [None]:
            text = line[pos:m.start() if m else len(line)]
            if text:
                if runs and tuple(runs[-1][1:]) == st:
                    runs[-1][0] += text
                else:
                    runs.append([text, st[0], st[1], st[2]])
            if m is None:
                break
            p = m.group(1).split(";")
            if "38" in p and "48" in p:
                st = (int(p[p.index("38") + 2]), int(p[p.index("48") + 2]), 1 if "1" in p[:2] else 0)
            pos = m.end()
        rows.append(runs)
    return rows


def xterm_rgb(n):
    """xterm 256색 번호 → 0xRRGGBB."""
    if n < 16:
        return [0x000000, 0x800000, 0x008000, 0x808000, 0x000080, 0x800080, 0x008080, 0xC0C0C0,
                0x808080, 0xFF0000, 0x00FF00, 0xFFFF00, 0x0000FF, 0xFF00FF, 0x00FFFF, 0xFFFFFF][n]
    if n < 232:
        lv = (0, 95, 135, 175, 215, 255)
        k = n - 16
        return lv[k // 36] << 16 | lv[k // 6 % 6] << 8 | lv[k % 6]
    v = 8 + (n - 232) * 10
    return v << 16 | v << 8 | v


RGB = [xterm_rgb(n) for n in range(256)]


def frame_blocks(lines):
    """패널용 프레임: 폭 1 글자만 있는 줄은 이어 붙여 Raster 칸(base64 u32 [글자, 전경, 배경])으로,
    한글·이모지가 섞인 줄은 색 조각 [글자, #전경, #배경, 굵게] 로."""
    import base64
    import struct
    blocks, buf, nrows = [], [], 0

    def flush():
        if buf:
            blocks.append({"cells": base64.b64encode(b"".join(buf)).decode(), "rows": nrows})
        del buf[:]

    for runs in frame_runs(lines):
        text = "".join(r[0] for r in runs)
        if all(ord(c) < 0x10000 and cw(c) == 1 and c.isprintable() for c in text):
            if not buf:
                nrows = 0
            buf.append(b"".join(struct.pack("<III", ord(c), RGB[r[1]], RGB[r[2]]) for r in runs for c in r[0]))
            nrows += 1
        else:
            flush()
            blocks.append({"runs": [[r[0], "#%06x" % RGB[r[1]], "#%06x" % RGB[r[2]], r[3]] for r in runs]})
    flush()
    return blocks


def run_stream(w, h, skip_intro=False, use_ai=True, replay=None, live=False):
    """옆 패널(Claude Code mod)용: 화면 없이 돌며 프레임을 한 줄 JSON 으로 stdout 에 쓴다.

    입력은 .runtime/pane_cmd (한 줄에 '번호<TAB>명령', 번호가 커진 것만 처리):
      key <이름>   up/down/left/right/return/tab/backspace 또는 글자
      click X Y    패널 안 칸 좌표(0부터)
      size WxH     패널 크기
    그 밖의 명령은 ./market 과 같다 (.runtime/cmd 도 그대로 읽는다).
    """
    import json
    world = World()
    attach_ai(world, use_ai, replay)
    if live:
        attach_live(world)
    if skip_intro:
        world.set_view("menu")
    state = {"quit": False, "paused": False}
    bridge = Bridge(RUNTIME_DIR)
    pane_in = os.path.join(RUNTIME_DIR, "pane_cmd")

    def pane_lines():
        try:
            with open(pane_in, encoding="utf-8", errors="ignore") as f:
                out = []
                for line in f.read().splitlines():
                    n, _, cmd = line.partition("\t")
                    if n.isdigit():
                        out.append((int(n), cmd))
                return out
        except OSError:
            return []

    seen = max([n for n, _ in pane_lines()] or [0])  # 시작 전에 쌓인 명령은 무시
    prev, last = time.time(), None
    while not state["quit"]:
        now = time.time()
        dt, prev = min(0.2, now - prev), now
        for n, cmd in pane_lines():
            if n <= seen:
                continue
            seen = n
            op, _, arg = cmd.partition(" ")
            if op == "key":
                k = PANE_KEYS.get(arg.lower(), arg)
                if k.lower() != "q":  # 패널에서는 q 로 끄지 않는다 (패널을 닫으면 끝난다)
                    handle_key(world, k, state)
            elif op == "click":
                x, y = (int(v) + 1 for v in arg.split()[:2])
                for x0, x1, y0, y1, action in list(world.hits):
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        action()
                        break
            elif op == "size":
                try:
                    w, h = (max(20, int(v)) for v in arg.lower().split("x"))
                except ValueError:
                    pass
            elif cmd.strip():
                world.command(cmd)
        for line in bridge.read_commands():
            if line.strip().lower() in ("pause", "resume"):
                state["paused"] = line.strip().lower() == "pause"
            else:
                world.command(line)
        if not state["paused"]:
            world.update(dt)
        bridge.write_state(world, state["paused"])
        caught = []
        for r in reversed(world.history):
            if r["id"] not in caught:
                caught.append(r["id"])
        out = json.dumps({"cols": w, "blocks": frame_blocks(compose(world, w, h, state["paused"])),
                          "caught": caught[:8], "hist": world.show_hist},
                         ensure_ascii=False, separators=(",", ":"))
        if out != last:
            last = out
            try:
                sys.stdout.write(out + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                return
        time.sleep(max(0.0, 1.0 / 15 - (time.time() - now)))  # 패널은 초당 15장 (그리기는 프레임당 3ms 안팎)


def main():
    ap = argparse.ArgumentParser(description="Ops Christmas Market - 터미널 메타버스 대시보드 PoC")
    ap.add_argument("--seed", type=int, help="난수 시드(같은 연출 재현)")
    ap.add_argument("--snapshot", type=float, metavar="SEC", help="SEC초 시뮬레이션 뒤 한 프레임만 출력하고 종료")
    ap.add_argument("--size", default="100x30", help="--snapshot 화면 크기 (기본 100x30)")
    ap.add_argument("--plain", action="store_true", help="--snapshot 출력에서 색 제거")
    ap.add_argument("--view", default="overview", choices=["intro", "menu", "overview", "traffic", "sentry"],
                    help="--snapshot 화면")
    ap.add_argument("--service", default="ALL", help="--snapshot 스테이지 서비스 (%s)" % ", ".join(s[0] for s in SERVICES))
    ap.add_argument("--skip-intro", action="store_true", help="타이틀 없이 메뉴부터")
    ap.add_argument("--stream", action="store_true", help="옆 패널용: 프레임을 JSON 줄로 stdout 에 (크기는 --size)")
    ap.add_argument("--no-ai", action="store_true", help="OPENAI_API_KEY 가 있어도 AI 병정을 끈다 (키워드·mock 판정)")
    ap.add_argument("--replay", nargs="?", const=REPLAY_DEFAULT, metavar="JSONL",
                    help="API 키 없이 녹화된 실제 AI 트리아지 결과를 재생 (기본 recordings/ai.jsonl)")
    ap.add_argument("--live", action="store_true",
                    help="쥐를 실제 Sentry 미해결 이슈로 (읽기 전용, .env 의 SENTRY_AUTH_TOKEN·SENTRY_ORG·SENTRY_PROJECT)")
    args = ap.parse_args()
    if args.seed is not None:
        R.seed(args.seed)
    if args.stream:
        w, h = (int(v) for v in args.size.lower().split("x"))
        run_stream(w, h, args.skip_intro, not args.no_ai, args.replay, args.live)
        return
    if args.snapshot is not None:
        w, h = (int(v) for v in args.size.lower().split("x"))
        world = World()
        if args.view in ("intro", "menu"):
            world.set_view(args.view)
        else:
            idx = world.find_service(args.service)
            if idx is None:
                sys.exit("알 수 없는 서비스: %s" % args.service)
            world.start_stage(idx, args.view)
        compose(world, w, h)
        for _ in range(int(args.snapshot * FPS)):
            world.update(1.0 / FPS)
        frame = compose(world, w, h)
        print("\n".join(ANSI.sub("", l) if args.plain else l for l in frame))
        return
    if not sys.stdin.isatty():
        sys.exit("터미널(TTY)에서 실행하세요. 미리보기는 --snapshot 옵션을 쓰세요.")
    run_interactive(args.skip_intro, not args.no_ai, args.replay, args.live)


if __name__ == "__main__":
    main()
