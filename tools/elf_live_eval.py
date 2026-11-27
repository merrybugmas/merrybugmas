#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""엘프(트래픽 대응)의 실제 AI 판단 측정: 실제 서버 /metrics · 실제 레버 · 실제 OpenAI API 로 엘프만 돌린다.

쥐(Sentry)는 만들지 않아 트리아지 호출이 없다. OpenAI 호출은 엘프 예산(XMAS_AI_TRAFFIC_MAX, 기본 5)까지만.
데모 서버(demo-web)에 부하를 따로 준다:  cd ~/demo-web && python3 loadgen.py 25 --spike 110 --at 15 --for 40

  python3 tools/elf_live_eval.py [초, 기본 120]   → 1초마다 상태 한 줄, 끝에 엘프 조치 기록(JSON)
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai  # noqa: E402
import xmas_market as xm  # noqa: E402

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 120
ai.load_dotenv()
w = xm.World()
w.auto = False
w.src.poll_issues = lambda *a, **k: []  # 쥐 없음 → 트리아지 호출 0
if not ai.available():
    sys.exit("OPENAI_API_KEY 가 없다")
w.ai = ai.Brain()
if not xm.attach_traffic(w):
    sys.exit("트래픽 실연동 실패: .env 의 TRAFFIC_METRICS_URL 확인")
w.start_stage(w.find_service("WEB"), "traffic")
xm.compose(w, 100, 30)

t0, last, seen = time.time(), 0, set()
while time.time() - t0 < secs:
    time.sleep(1 / 12)
    w.update(1 / 12)
    if int(time.time() - t0) != last:
        last = int(time.time() - t0)
        snap = w.elf.snapshot()
        pr = snap.get("pressure", {})
        print("%3ds rps %5.0f  5xx %5s%%  응답 %5sms  대기 %3s  상태 %-5s 레버 %s  AI %d/%d" % (
            last, w.snapshot_state()["traffic"]["rps"], pr.get("error_pct"), pr.get("latency_ms"), pr.get("waiting"),
            snap["state"], snap["levers"], snap["ai"]["used"], snap["ai"]["max"]), flush=True)
        for t, text, _, _ in w.logs:
            if (t, text) not in seen and any(k in text for k in ("엘프", "🧝", "🧠", "🧹", "✅", "🚨", "레버")):
                print("      │ " + text, flush=True)
            seen.add((t, text))

snap = w.elf.snapshot()
print(json.dumps({"ai": snap["ai"], "openai_calls": w.ai.calls if hasattr(w.ai, "calls") else None,
                  "actions": snap["actions"], "levers_end": snap["levers"]}, ensure_ascii=False, indent=1))
w.elf.command(w, "원복")  # 끝나면 레버를 처음 값으로
for _ in range(24):
    time.sleep(1 / 12)
    w.update(1 / 12)
