#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ops Christmas Market — 터미널 메타버스 대시보드 PoC (mock 데이터, 픽셀아트)

  트래픽(Grafana 자리)      → 마켓 방문객 수. 트래픽이 오르면 광장이 북적인다.
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
RAT_DEAD = [row.replace("k", "x") for row in RAT_WALK[0][::-1]]  # 뒤집힌 쥐, 눈은 x

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

    def poll_issues(self, t, dt, rps):
        """새로 생긴 미해결 이슈. 트래픽이 높을수록 자주 생긴다."""
        lam = 1 / 16.0 + 0.25 * max(0.0, (rps - 1600) / PEAK_RPS)
        return [self.make_issue()] if R.random() < lam * dt else []

    def make_issue(self):
        kind = R.choices(list(KIND_WEIGHTS), weights=list(KIND_WEIGHTS.values()))[0]
        proj, title, _ = R.choice([c for c in ISSUE_CATALOG if c[2] == kind])
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


class Visitor:
    def __init__(self, world):
        self.x = -3.0 if R.random() < 0.5 else float(world.w)
        self.y = R.randint(world.walk_top, world.walk_bot)
        self.pal = {"H": R.choice(HATS), "S": 223, "C": R.choice(COATS), "P": R.choice(PANTS), "k": 16}
        self.speed = R.uniform(4, 9)
        self.target = R.choice(world.targets)
        self.linger = 0.0
        self.leaving = False
        self.gone = False
        self.step = 0.0

    def update(self, world, dt):
        if self.linger > 0:
            self.linger -= dt
            return
        dx = self.target - self.x
        if abs(dx) < 0.6:
            if self.leaving:
                self.gone = True
            elif R.random() < 0.12:
                self.leave(world)
            else:
                self.linger = R.uniform(1, 4)
                self.target = R.choice(world.targets) + R.uniform(-3, 3)
            return
        self.step += dt
        self.x += math.copysign(min(abs(dx), self.speed * dt), dx)

    def leave(self, world):
        self.leaving = True
        self.linger = 0
        self.target = -4.0 if self.x < world.w / 2 else world.w + 1.0

    @property
    def walking(self):
        return self.linger <= 0 and int(self.step * 6) % 2 == 1


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


class Soldier:
    W, H = 7, 14

    def __init__(self, idx, world):
        self.idx = idx
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

    def claim_next(self, world):
        free = [m for m in world.mice if m.state == "roam" and m.claimed is None and 10 <= m.x <= world.w - 16]
        if not free:
            return False
        m = min(free, key=lambda m: (not m.priority, m.born, abs(m.x - self.x)))
        m.claimed, m.state = self.idx, "held"
        self.target = m
        self.state = "walk"
        self.bubble = ""
        world.log("병정%d 출동 -> %s 발견" % (self.idx + 1, m.issue["id"]), 117)
        return True

    def update(self, world, dt):
        self.phase += dt
        m = self.target
        if self.state == "home":
            backlog = sum(1 for mm in world.mice if mm.state == "roam" and mm.claimed is None)
            if backlog >= 1 + 3 * self.idx:
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

        iid = m.issue["id"]
        if self.state == "triage":
            kind = m.issue["kind"]
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
                m.state, m.ttl = "dead", 1.2
                world.poof(m.x + 7, m.y - 2)
                world.log("MR !%d 생성 - fix: %s 처치 완료" % (world.mr, iid), 46)
                self.bubble, self.state, self.timer = "MR !%d" % world.mr, "after", 1.0
        elif self.state == "shoo":
            m.state, m.dir = "flee", (1 if m.x > self.x else -1)
            world.stats["noise"] += 1
            world.log("%s 은(는) noise - 레포 밖 출처, 쫓아냄" % iid, 245)
            self.state, self.timer = "after", 0.3
        elif self.state == "flag":
            m.state, m.ttl = "caged", 30.0
            world.stats["human"] += 1
            world.log("%s needs-human - 우리에 가두고 담당자 호출" % iid, 226)
            self.state, self.timer = "after", 0.4
        elif self.state == "after":
            self.target = None
            self.bubble = ""
            if not self.claim_next(world):
                self.state = "return"


# ------------------------------------------------------------------ 세계

BOOTH_NAMES = ["GLUHWEIN", "COOKIES", "ORNAMENTS", "BRATWURST", "HOT CHOCO", "CANDLES", "PRETZEL", "TOYS"]
BOOTH_STRIPES = [(196, 255), (28, 255), (196, 220), (25, 255)]


class World:
    def __init__(self):
        self.src = MockSource()
        self.t = 0.0
        self.w = self.sh = 0
        self.visitors, self.mice, self.particles = [], [], []
        self.soldiers = []
        self.logs = []
        self.rps = 0.0
        self.hist, self.ehist, self.spawn_times = [], [], []
        self.hist_acc = 0.0
        self.mr = 1400
        self.stats = {"fixed": 0, "noise": 0, "human": 0, "blocked": 0}
        self.auto = True
        self.fired = set()
        self.view = "overview"  # overview(정적 개요) / traffic(광장) / sentry(전투)
        self.unseen = 0  # 광장을 보는 동안 새로 나온 쥐
        self.hits = []  # 클릭 영역 (x0, x1, y0, y1, view), 1부터 세는 칸 좌표
        self.log("크리스마스 마켓 개장! (mock 데이터)", 220, "all")

    # ---- 레이아웃
    def layout(self, w, sh):
        if (w, sh) == (self.w, self.sh):
            return
        self.w, self.sh = w, sh
        self.gt = max(5, int(sh * 0.42))  # 광장(지면) 시작 칸 행
        self.gpx = 2 * self.gt  # 광장 시작 픽셀 행
        self.bottom = 2 * sh - 1  # 맨 아래 픽셀 행
        self.walk_top, self.walk_bot = self.gpx + 2, self.bottom - 6
        self.lanes = [self.bottom, self.bottom - 3]
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
        self.targets = [x + (len(n) + 4) / 2.0 for x, n, _ in self.booths] + [w / 2.0]
        self.snow = [[R.uniform(0, w), R.uniform(0, self.gpx), R.uniform(3, 7), R.uniform(-1.5, 1.5)]
                     for _ in range(w * self.gt // 10)]
        self.stars = [(R.randrange(w), R.randrange(4, max(5, self.gpx - 10))) for _ in range(w // 4)]
        self.ground_tex = {(R.randrange(w), R.randrange(self.gpx + 2, 2 * sh)): R.choice((252, 253, 251))
                           for _ in range(w * (sh - self.gt) // 6)}
        if not self.soldiers:
            self.soldiers = [Soldier(i, self) for i in range(3)]
        for v in self.visitors:
            v.y = min(max(v.y, self.walk_top), self.walk_bot)
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
        issue = issue or self.src.make_issue()
        self.mice.append(Mouse(self, issue))
        if self.view == "traffic":
            self.unseen += 1
        self.spawn_times.append(self.t)

    # ---- 이벤트(키 입력 · 자동 연출 공용)
    def traffic_spike(self):
        self.src.add_spike(self.t)
        self.log("크리스마스 특가 오픈! 트래픽 급증 - 광장이 붐빈다", 213, "traffic")

    def outbreak(self, n=5):
        self.log("release-261225 배포 직후 쥐 떼 출몰! (신규 이슈 %d건)" % n, 203)
        for _ in range(n):
            self.spawn_mouse()

    def human_clears(self):
        caged = [m for m in self.mice if m.state == "caged"]
        for m in caged:
            m.state, m.ttl = "dead", -1
        self.log("담당자가 우리 속 쥐 %d마리 처리 완료" % len(caged), 159)

    def set_view(self, v):
        if v in ("overview", "traffic", "sentry"):
            self.view = v
            if v != "traffic":
                self.unseen = 0

    # ---- 갱신
    def update(self, dt):
        if not self.w:  # 첫 compose()가 layout을 잡기 전
            return
        self.t += dt
        if self.auto:
            cyc, ct = divmod(self.t, 90.0)
            for at, name, fn in ((12, "spike", self.traffic_spike), (45, "outbreak", self.outbreak)):
                if ct >= at and (cyc, name) not in self.fired:
                    self.fired.add((cyc, name))
                    fn()

        self.rps = self.src.rps(self.t)
        for issue in self.src.poll_issues(self.t, dt, self.rps):
            self.log("Sentry 신규 이슈 %s: %s" % (issue["id"], issue["title"][:40]), 203)
            self.spawn_mouse(issue)

        self.hist_acc += dt
        if self.hist_acc >= 0.5:
            self.hist_acc = 0.0
            self.spawn_times = [s for s in self.spawn_times if self.t - s < 20]
            self.hist = (self.hist + [self.rps])[-60:]
            self.ehist = (self.ehist + [len(self.spawn_times)])[-60:]

        # 방문객 수 = 트래픽
        cap = max(10, int(self.w * 0.85))
        want = 3 + int((cap - 3) * min(1.0, self.rps / PEAK_RPS))
        staying = [v for v in self.visitors if not v.leaving]
        if len(staying) < want and R.random() < 8 * dt:
            self.visitors.append(Visitor(self))
        elif len(staying) > want and R.random() < 5 * dt:
            R.choice(staying).leave(self)
        for v in self.visitors:
            v.update(self, dt)
        self.visitors = [v for v in self.visitors if not v.gone]

        for m in self.mice:
            m.update(self, dt)
            if m.state == "caged" and m.ttl <= 0 and self.auto:
                self.log("담당자가 %s 확인 완료 (우리 해제)" % m.issue["id"], 159)
                m.state, m.ttl = "dead", -1
        self.mice = [m for m in self.mice if not (m.state in ("dead", "flee") and m.ttl < 0)]

        for s in self.soldiers:
            s.update(self, dt)

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
        parts = line.strip().split(None, 1)
        if not parts:
            return
        op, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if op in ("spike", "traffic"):
            self.traffic_spike()
        elif op in ("outbreak", "bugs"):
            self.outbreak(int(arg) if arg.isdigit() else 5)
        elif op in ("mouse", "bug"):
            self.spawn_mouse()
            self.log("Sentry 신규 이슈 %s" % self.mice[-1].issue["id"], 203)
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
        elif op == "say":
            self.log("💬 codex: " + arg, 183, "all")
        else:
            self.log("알 수 없는 명령: %s" % line.strip()[:40], 245, "all")

    def snapshot_state(self):
        label, _ = self.crowd_label()
        return {
            "t": round(self.t, 1),
            "traffic": {"rps": int(self.rps), "visitors": len(self.visitors), "crowd": label,
                        "history_rps": [int(v) for v in self.hist[-20:]]},
            "mice": [{"id": m.issue["id"], "title": m.issue["title"], "kind": m.issue["kind"],
                      "state": m.state, "chasedBy": None if m.claimed is None else m.claimed + 1}
                     for m in self.mice if m.state != "dead"],
            "soldiers": [{"id": s.idx + 1, "state": s.state,
                          "target": s.target.issue["id"] if s.target else None} for s in self.soldiers],
            "stats": dict(self.stats, mrs=self.mr - 1400),
            "auto": self.auto,
            "view": self.view,
            "log": ["%02d:%02d %s" % (divmod(int(ts), 60) + (text,)) for ts, text, _, _ in self.logs[-12:]],
        }

    # ---- 그리기
    def crowd_label(self):
        r = self.rps
        if r < 600:
            return "한산함", 250
        if r < 1300:
            return "활기참", 120
        if r < 2200:
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
                (pad(m.issue["kind"], 13), KIND_COL[m.issue["kind"]]), (pad(st, 15), sc), (m.issue["title"], 244)]

    def draw(self, view="traffic"):
        market, bugs = view != "sentry", view != "traffic"
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
        for f in self.snow if market else ():
            cv.set(f[0], f[1], 255 if f[2] > 5 else 252)
        # 전구 가랜드 (처지는 줄 + 반짝이는 전구)
        for x in range(w if market else 0):
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
            self.draw_bugs(cv, top, table=not market)
        return cv

    def draw_bugs(self, cv, top, table):
        w, gpx, bottom = self.w, self.gpx, self.bottom
        if table:  # 하늘 자리에 이슈 목록
            n = max(2, top // 2)
            tw = w - 16
            items = self.live_issues()
            put_segs(cv, 2, 0, fit([(ISSUE_HEADER, 242)], tw), 234)
            if not items:
                put_segs(cv, 2, 1, fit([("   쥐 없음 - 병정은 위병소에서 대기 중", 46)], tw), 234)
            shown = items if len(items) <= n - 1 else items[:n - 2]
            for i, m in enumerate(shown):
                put_segs(cv, 2, 1 + i, fit(self.issue_segs(m), tw), 234)
            if len(shown) < len(items):
                put_segs(cv, 2, n - 1, fit([("   … 외 %d건" % (len(items) - len(shown)), 244)], tw), 234)

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
            cv.put(x + 3, row, str(s.idx + 1), 25, None, True)

        for p in self.particles:
            cv.set(p[0], p[1], p[4])

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

        # 방문객 (뒤에 선 사람부터)
        for v in sorted(self.visitors, key=lambda v: v.y):
            spr = VISITOR[:4] + (VISITOR_STEP if v.walking else VISITOR[4:])
            cv.blit(spr, v.x, v.y, v.pal)


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
    title = [("  🎄 OPS CHRISTMAS MARKET  " if w >= 100 else " 🎄 OPS ", 226, True)]
    segs, x = list(title), seg_width(title) + 1
    label, _ = world.crowd_label()
    blink = int(world.t * 2) % 2
    live = sum(1 for m in world.mice if m.state in ("roam", "held"))
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
        world.hits.append((x, x + dw(text) - 1, 1, 1, v))
        x += dw(text)
        segs.append((" ", 7))
        x += 1
    right = [("%s%s  MOCK  " % ("|| PAUSED  " if paused else "", "AUTO" if world.auto else "MANUAL"), 223)]
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
        [("  방문객 %d명" % len(world.visitors), 250)],
        [("  30초 최고 %s rps" % format(int(peak), ","), 245)],
        [],
    ]
    rows = max(2, min(12, height - 2 - len(body) - 2))
    body += [[("  " + r, 51)] for r in bars(world.hist, iw - 4, PEAK_RPS, rows)]
    body.append([("  " + pad("최근 30초", iw - 15) + "max 3,000", 242)])
    title = [("1  🎄 트래픽", 231, True), (" · Grafana", 245)]
    return card(width, height, title, "클릭 ▸ 광장", body, 196 if alert else 37)


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
        world.hits += [(2, 1 + lw, 2, 1 + bh, "traffic"), (3 + lw, 2 + lw + rw, 2, 1 + bh, "sentry")]
    else:
        th = max(8, bh * 2 // 5)
        for row in traffic_card(world, w - 2, th) + sentry_card(world, w - 2, bh - th):
            lines.append(seg_line([(" ", 7)] + row, w, 233))
        world.hits += [(1, w, 2, 1 + th, "traffic"), (1, w, 2 + th, 1 + bh, "sentry")]
    keys = "[1]/클릭 트래픽 [2]/클릭 Sentry [t]급증 [b]쥐 떼 [space]정지 [q]종료"
    return lines + log_lines(world, w, LOG_H, ("all", "traffic", "sentry"), keys)


def compose(world, w, h, paused=False):
    world.hits = []
    if w < 60 or h < 22:
        msg = "60x22 이상 필요 (현재 %dx%d)" % (w, h)
        return [seg_line([(msg, 231)], w, 52)] + [" " * w] * (h - 1)
    sh = h - 3 - LOG_H
    world.layout(w, sh)
    if world.view == "overview":
        return compose_overview(world, w, h, paused)[:h]

    lines = [header(world, w, paused)]
    n = max(10, w - 24)
    if world.view == "traffic":
        label, lcol = world.crowd_label()
        lines.append(seg_line([
            ("  방문객 ", 250), ("%5s" % format(int(world.rps), ","), 231, True),
            (" rps  %d명  " % len(world.visitors), 245), (label, lcol, True),
            ("   (rps가 오르면 광장이 붐빈다)", 242)], w, 235))
        lines.append(seg_line([("  트래픽 30초 ", 250), (spark(world.hist, n, PEAK_RPS), 51)], w, 234))
        keys = "[0/Esc]개요 [2]Sentry [t]트래픽 급증 [space]정지 [q]종료"
        cats = ("all", "traffic")
    else:
        live = [m for m in world.mice if m.state in ("roam", "held")]
        caged = sum(1 for m in world.mice if m.state == "caged")
        out = sum(1 for s in world.soldiers if s.busy)
        st = world.stats
        lines.append(seg_line([
            ("  🐭 쥐 ", 250), (str(len(live)), 203, True), (" 우리 %d" % caged, 226),
            ("  | 💂 출동 ", 250), ("%d/%d" % (out, len(world.soldiers)), 117, True),
            ("  | MR ", 250), (str(st["fixed"]), 46, True),
            ("  noise %d  block %d" % (st["noise"], st["blocked"]), 245)], w, 235))
        lines.append(seg_line([("  신규 쥐 20초 ", 250), (spark(world.ehist, n, 8), 203)], w, 234))
        keys = "[0/Esc]개요 [1]트래픽 [b]쥐 떼 [n]쥐 1마리 [h]사람이 처리 [space]정지 [q]종료"
        cats = ("all", "sentry")
    lines += world.draw(world.view).lines()
    lines += log_lines(world, w, LOG_H, cats, keys)
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
        if now - self.last < 1.0 or not world.w:
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
    ch = ch.lower()
    if ch in ("0", "\x1b"):
        world.set_view("overview")
    elif ch in ("1", "2"):
        world.set_view("traffic" if ch == "1" else "sentry")
    elif ch == "\t":
        order = [v for v, _ in VIEWS]
        world.set_view(order[(order.index(world.view) + 1) % len(order)])
    elif ch == "q":
        state["quit"] = True
    elif ch == " ":
        state["paused"] = not state["paused"]
    elif ch == "t":
        world.traffic_spike()
    elif ch == "b":
        world.outbreak()
    elif ch == "n":
        world.spawn_mouse()
        world.log("Sentry 신규 이슈 %s" % world.mice[-1].issue["id"], 203)
    elif ch == "h":
        world.human_clears()
    elif ch == "a":
        world.auto = not world.auto
        world.log("자동 연출 %s" % ("ON" if world.auto else "OFF"), 245, "all")


MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
CSI = re.compile(r"\x1b\[[0-9;?<]*[A-Za-z~]")


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
                for x0, x1, y0, y1, v in world.hits:
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        world.set_view(v)
                        break
            i = m.end()
            continue
        c = CSI.match(buf, i)
        if c:
            i = c.end()
            continue
        if i == len(buf) - 1:  # Esc 단독
            handle_key(world, "\x1b", state)
            i += 1
            continue
        if re.fullmatch(r"\x1b\[[0-9;<?]*", buf[i:]):  # 시퀀스가 잘려 들어옴
            return buf[i:]
        i += 1
    return ""


def run_interactive():
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    out = sys.stdout
    world = World()
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
            if world.view != "overview" or key != cache_key:
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


def main():
    ap = argparse.ArgumentParser(description="Ops Christmas Market - 터미널 메타버스 대시보드 PoC")
    ap.add_argument("--seed", type=int, help="난수 시드(같은 연출 재현)")
    ap.add_argument("--snapshot", type=float, metavar="SEC", help="SEC초 시뮬레이션 뒤 한 프레임만 출력하고 종료")
    ap.add_argument("--size", default="100x30", help="--snapshot 화면 크기 (기본 100x30)")
    ap.add_argument("--plain", action="store_true", help="--snapshot 출력에서 색 제거")
    ap.add_argument("--view", default="overview", choices=["overview", "traffic", "sentry"], help="--snapshot 화면")
    args = ap.parse_args()
    if args.seed is not None:
        R.seed(args.seed)
    if args.snapshot is not None:
        w, h = (int(v) for v in args.size.lower().split("x"))
        world = World()
        compose(world, w, h)
        for _ in range(int(args.snapshot * FPS)):
            world.update(1.0 / FPS)
        world.set_view(args.view)
        frame = compose(world, w, h)
        print("\n".join(ANSI.sub("", l) if args.plain else l for l in frame))
        return
    if not sys.stdin.isatty():
        sys.exit("터미널(TTY)에서 실행하세요. 미리보기는 --snapshot 옵션을 쓰세요.")
    run_interactive()


if __name__ == "__main__":
    main()
