#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""README 용 스크린샷: 게임 화면을 그대로(▀ 반블록 픽셀) PNG·GIF 로 뽑는다.

  python3 tools/screenshots.py            # docs/images/ 에 저장 (Pillow 필요: pip install pillow)
  python3 tools/screenshots.py --ai-only  # OPENAI_API_KEY 로 실제 AI 병정 화면만 (ai.png · ai-history.png, 약 1분)

터미널 없이 xmas_market.py 의 World 를 돌려 compose() 결과를 그린다. 칸 하나 = 8x16 px.
글꼴은 macOS 기본(Menlo · Apple SD Gothic Neo · Apple Color Emoji)을 쓴다.
"""
import importlib.util
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "images")
sys.path.insert(0, ROOT)  # xmas_market 이 ai.py 를 찾도록
spec = importlib.util.spec_from_file_location("xm", os.path.join(ROOT, "xmas_market.py"))
xm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xm)

CW, CH = 8, 16
FONTS = "/System/Library/Fonts/"
MONO = ImageFont.truetype(FONTS + "Menlo.ttc", 13)
HANGUL = ImageFont.truetype(FONTS + "AppleSDGothicNeo.ttc", 15)
EMOJI = ImageFont.truetype(FONTS + "Apple Color Emoji.ttc", 20)  # 컬러 이모지는 정해진 크기로만 그려진다
_emoji_cache = {}


def rgb(n):
    v = xm.RGB[n]
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


def emoji(ch):
    if ch not in _emoji_cache:
        im = Image.new("RGBA", (26, 26), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((0, 0), ch, font=EMOJI, embedded_color=True)
        _emoji_cache[ch] = im.crop(im.getbbox() or (0, 0, 1, 1)).resize((14, 14), Image.LANCZOS)
    return _emoji_cache[ch]


def render(lines, w, h):
    img = Image.new("RGB", (w * CW, h * CH))
    dr = ImageDraw.Draw(img)
    for y, runs in enumerate(xm.frame_runs(lines)):
        x = 0
        for text, fg, bg, bold in runs:
            for ch in text:
                k = xm.cw(ch)
                px, py = x * CW, y * CH
                if ch == "▀":
                    dr.rectangle([px, py, px + CW - 1, py + CH // 2 - 1], fill=rgb(fg))
                    dr.rectangle([px, py + CH // 2, px + CW - 1, py + CH - 1], fill=rgb(bg))
                else:
                    dr.rectangle([px, py, px + CW * k - 1, py + CH - 1], fill=rgb(bg))
                    if ord(ch) >= 0x1F000 or ch in "☃❄":
                        img.paste(emoji(ch), (px + 1, py + 1), emoji(ch))
                    elif "▁" <= ch <= "█":  # 아래에서 차오르는 막대: 글꼴 대신 직접 칠한다
                        k8 = ord(ch) - 0x2580
                        dr.rectangle([px, py + CH - CH * k8 // 8, px + CW - 1, py + CH - 1], fill=rgb(fg))
                    elif ch == "✔":  # 체크·엑스는 기본 글꼴에 없어 선으로 그린다
                        dr.line([(px + 1, py + 8), (px + 3, py + 11), (px + 7, py + 4)], fill=rgb(fg), width=2)
                    elif ch == "✖":
                        dr.line([(px + 1, py + 4), (px + 6, py + 11)], fill=rgb(fg), width=2)
                        dr.line([(px + 6, py + 4), (px + 1, py + 11)], fill=rgb(fg), width=2)
                    elif ch.strip():
                        font = HANGUL if k == 2 or ord(ch) > 0x2000 else MONO
                        dr.text((px, py + (0 if font is HANGUL else 1)), ch, fill=rgb(fg), font=font)
                        if bold and k == 1:
                            dr.text((px + 1, py + 1), ch, fill=rgb(fg), font=font)
                x += k
    return img


def run(world, sec, events=()):
    """sec 초 돌리며 (초, 함수) 이벤트를 그 시각에 실행."""
    todo = sorted(events, key=lambda e: e[0])
    for i in range(int(sec * 12)):
        t = i / 12
        while todo and todo[0][0] <= t:
            todo.pop(0)[1]()
        world.update(1 / 12)


def run_live(world, sec, events=()):
    """AI 결과는 실제 시간에 도착하므로 화면 시간과 실제 시간을 맞춰 돌린다."""
    import time
    todo = sorted(events, key=lambda e: e[0])
    for i in range(int(sec * 12)):
        while todo and todo[0][0] <= i / 12:
            todo.pop(0)[1]()
        world.update(1 / 12)
        time.sleep(1 / 12)


def shot_ai(W, H):
    """실제 OpenAI 호출로 AI 병정 장면을 찍는다. 가짜 응답은 쓰지 않는다."""
    if xm.ai is None or not xm.ai.available():
        print("ai.png 건너뜀: OPENAI_API_KEY 가 없다")
        return
    xm.R.seed(5)
    w = xm.World()
    w.ai = xm.ai.Brain()
    w.start_stage(1, "sentry")
    xm.compose(w, W, H)
    run_live(w, 30, [(1, lambda: w.outbreak(5)),
                     (14, lambda: w.order("진저야 제일 오래 돌아다닌 쥐부터 잡아")),
                     (22, w.brief)])
    shot("ai.png", w, W, H)
    w.open_history()
    shot("ai-history.png", w, W, H)
    print("AI 호출 %d회 · 실패 %d회%s" % (w.ai.calls, w.ai.fails, " (%s)" % w.ai.last_error if w.ai.fails else ""))


def shot(name, world, w, h):
    img = render(xm.compose(world, w, h), w, h)
    path = os.path.join(OUT, name)
    img.save(path, optimize=True)
    print("%-22s %4dx%-4d %6.1f KB" % (name, img.width, img.height, os.path.getsize(path) / 1024))


def main():
    os.makedirs(OUT, exist_ok=True)
    W, H = 100, 40
    if "--ai-only" in sys.argv:
        shot_ai(W, H)
        return

    # 1. 타이틀 애니메이션 (GIF)
    xm.R.seed(11)
    w = xm.World()
    xm.compose(w, W, H)
    frames = []
    for i in range(int(4.6 * 10)):
        w.update(0.1)
        frames.append(render(xm.compose(w, W, H), W, H).convert("P", palette=Image.ADAPTIVE, colors=64))
    path = os.path.join(OUT, "intro.gif")
    frames[0].save(path, save_all=True, append_images=frames[1:] + [frames[-1]] * 15, duration=100, loop=0, optimize=True)
    print("%-22s %6.1f KB" % ("intro.gif", os.path.getsize(path) / 1024))
    shot("intro.png", w, W, H)

    # 2. 메뉴
    w = xm.World()
    w.set_view("menu")
    w.svc_i, w.menu_col, w.scn_i = 1, 1, 1
    run(w, 1)
    shot("menu.png", w, W, H)

    # 3. 트래픽: 특가 오픈 급증으로 상점 줄이 길어진 장면
    xm.R.seed(3)
    w = xm.World()
    w.start_stage(1, "traffic")
    xm.compose(w, W, H)
    run(w, 20)
    shot("traffic.png", w, W, H)

    # 4. Sentry: 쥐 떼 + 병정 출동 + 명령에 대답하는 병정 + 뒤쪽 우리·게시판
    xm.R.seed(5)
    w = xm.World()
    w.start_stage(1, "sentry")
    xm.compose(w, W, H)
    run(w, 34, [(2, lambda: w.outbreak(5)), (17, lambda: w.outbreak(5))])
    roam = [m for m in w.mice if m.state == "roam"]
    if roam:
        w.order("진저 %s 먼저 잡아" % roam[0].issue["id"])
    run(w, 0.6)
    shot("sentry.png", w, W, H)

    # 5. 잡은 쥐 기록 창
    run(w, 8)
    w.open_history(w.history[-2]["id"] if len(w.history) > 1 else None)
    shot("history.png", w, W, H)

    # 6. 개요
    w.show_hist = False
    w.set_view("overview")
    shot("overview.png", w, W, H)

    # 7. AI 병정 (키가 있을 때만, 실제 호출)
    shot_ai(W, H)


if __name__ == "__main__":
    sys.exit(main())
