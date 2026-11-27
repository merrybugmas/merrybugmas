#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 구조도 PNG (README · 프로젝트 설명서용): python3 tools/diagram.py → docs/images/architecture.png

Pillow 필요. 글꼴은 macOS 기본(Apple SD Gothic Neo)을 쓴다.
"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "images", "architecture.png")
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"

W, H = 1600, 900
BG = (18, 22, 38)
INK = (236, 238, 245)
DIM = (150, 158, 180)
GREEN, RED, GOLD, BLUE, PURPLE = (46, 140, 87), (178, 48, 52), (214, 170, 60), (58, 110, 190), (130, 90, 200)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)


def f(size, bold=False):
    return ImageFont.truetype(FONT, size, index=6 if bold else 0)


def box(x, y, w, h, color, title, lines, tag=None):
    d.rounded_rectangle([x, y, x + w, y + h], 18, fill=tuple(int(c * 0.35) + 10 for c in color), outline=color, width=4)
    if tag:
        tw = d.textlength(tag, font=f(18, True)) + 20
        d.rounded_rectangle([x + 16, y - 16, x + 16 + tw, y + 16], 10, fill=color)
        d.text((x + 26, y - 13), tag, font=f(18, True), fill=INK)
    d.text((x + 22, y + 26), title, font=f(28, True), fill=INK)
    for i, line in enumerate(lines):
        d.text((x + 22, y + 72 + i * 30), line, font=f(20), fill=DIM if line.startswith("·") is False else INK)
    return (x, y, w, h)


def arrow(x1, y1, x2, y2, color=INK, label=None, both=False, dy=-30):
    d.line([(x1, y1), (x2, y2)], fill=color, width=4)

    def head(ax, ay, bx, by):
        import math
        a = math.atan2(by - ay, bx - ax)
        for s in (2.6, -2.6):
            d.line([(bx, by), (bx + 16 * math.cos(a + s), by + 16 * math.sin(a + s))], fill=color, width=4)
    head(x1, y1, x2, y2)
    if both:
        head(x2, y2, x1, y1)
    if label:
        lw = d.textlength(label, font=f(18))
        d.text(((x1 + x2) / 2 - lw / 2, (y1 + y2) / 2 + dy), label, font=f(18), fill=GOLD)


d.text((40, 28), "MERRY BUGMAS — AI 구조", font=f(36, True), fill=INK)
d.text((40, 76), "운영자(Codex)가 채팅으로 병정·엘프(OpenAI API)를 지휘하고, 대시보드가 실제 데이터와 에이전트의 판단을 한 장면으로 보여 준다",
       font=f(22), fill=DIM)

user = box(40, 250, 190, 150, DIM, "사람", ["자연어로 묻고", "지시·승인한다"])
codex = box(330, 170, 330, 310, GREEN, "Codex  운영자", [
    "· AGENTS.md = 도구 사용 규약", "· status 를 먼저 읽고", "  숫자로 답한다", "· 병정·엘프에게 tell / focus",
    "· brief · say 로 대시보드 표시", "· auto: 30초마다 자율 판단"], tag="OpenAI Codex CLI")
bridge = box(760, 200, 260, 250, GOLD, "./market", [
    "명령 → .runtime/cmd", "현황 ← .runtime/state.json", "(1초마다 갱신)", "", "사람도 같은 명령 사용"],
    tag="파일 브리지")
dash = box(1120, 140, 440, 300, RED, "대시보드  xmas_market.py", [
    "트래픽 = 상점 대기열", "Sentry 이슈 = 쥐", "병정 셋 = sentry-bot (이슈 처리)",
    "엘프 = 트래픽 대응 (레버)", "판단 근거 → 작전 로그·기록 창", "키 없으면 키워드·규칙으로 동작"], tag="터미널 픽셀아트")
oai = box(1120, 530, 440, 270, PURPLE, "OpenAI API  에이전트의 두뇌", [
    "① 명령 해석  ② 트리아지 + 근거", "③ 운영 브리핑  ④ 엘프 레버 판단", "⑤ 수정 패치 (fixer.py)",
    "Responses API · strict JSON Schema", "백그라운드 스레드 · 호출 상한"],
    tag="ai.py · fixer.py")

d.text((40, 538), "--live 실연동 (.env 에 설정된 것만, 없으면 mock)", font=f(20, True), fill=GOLD)
box(40, 590, 310, 210, BLUE, "Sentry API", ["미해결 이슈 → 쥐", "읽기 전용", "(15초마다)"], tag="읽기")
box(380, 590, 310, 210, BLUE, "서버 /metrics", ["rps · 5xx · 응답 시간", "Web Vitals", "+ /admin/levers 레버"], tag="읽기 + 레버")
box(720, 590, 300, 210, BLUE, "GitHub PR", ["fixable → 새 브랜치", "→ gh pr create", "머지는 사람이"], tag="쓰기")

arrow(230, 325, 330, 325, label="채팅", both=True)
arrow(660, 280, 760, 280, label="tell · brief", dy=-32)
arrow(760, 370, 660, 370, label="status", dy=8)
arrow(1020, 300, 1120, 280, both=True, label="cmd / state", dy=-44)
arrow(1480, 440, 1480, 515, both=True)
d.text((1180, 462), "명령·이슈·부하 → 구조화된 판단", font=f(18), fill=GOLD)
arrow(1020, 660, 1120, 420, both=True)
d.text((40, 846), "외부 호출은 모두 백그라운드 스레드: 실패하면 로그 한 줄 + mock·키워드·규칙으로 이어지고 화면은 멈추지 않는다. 번호표·PR 머지는 사람 승인.",
       font=f(19), fill=DIM)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
img.save(OUT)
print(OUT)
