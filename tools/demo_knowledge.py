#!/usr/bin/env python3
"""API 키 없이 실제 검색·대시보드를 시연. 분류/수정 모델 성능 시연은 아니다."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge
import xmas_market as xm


def demo_world(enabled=True):
    xm.R.seed(7)
    world = xm.World()
    world.auto = False
    world.start_stage(world.find_service("WEB"), "sentry")
    if enabled:
        world.kb = knowledge.KnowledgeBase()
    xm.compose(world, 120, 40)
    world.spawn_mouse({"id": "DEMO-KB1", "title": "TypeError: Cannot read properties of null (reading 'rate')",
                       "culprit": "cartTotal (app.js)", "kind": "fixable", "review_block": False})
    world.command("kb DEMO-KB1")
    world.log("데모 데이터 · 실제 사례 검색 · OpenAI 0회 · PR 생성 없음", 220, "all")
    return world


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-knowledge", action="store_true")
    ap.add_argument("--plain", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    world = demo_world(not args.no_knowledge)
    if args.json:
        print(json.dumps(world.snapshot_state(), ensure_ascii=False, indent=2))
    else:
        lines = xm.compose(world, 120, 40)
        print("\n".join(xm.ANSI.sub("", x) if args.plain else x for x in lines))


if __name__ == "__main__":
    main()
