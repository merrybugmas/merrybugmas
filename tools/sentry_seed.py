#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sentry 테스트 프로젝트에 시연용 에러 이벤트를 넣는다 (--live 시연 준비).

  python3 tools/sentry_seed.py            # 12건 (fixable·noise·needs-human 4건씩)
  python3 tools/sentry_seed.py --n 32     # 평가셋 전부
  python3 tools/sentry_seed.py --dry      # 보내지 않고 무엇을 보낼지만 출력

제목은 tests/data/triage_eval.jsonl 에서 가져온다. 직접 만든 테스트 이벤트이고 실제 서비스 에러가 아니다.
SENTRY_DSN 필요 (.env 가능). 본인 소유의 테스트 프로젝트 DSN 만 쓴다. 표준 라이브러리만 쓴다.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ai  # noqa: E402,F401 — .env 를 읽는다


def pick(cases, n):
    """판정 종류가 고르게 섞이도록 n건."""
    by = {}
    for c in cases:
        by.setdefault(c["kind"], []).append(c)
    out, i = [], 0
    while len(out) < min(n, len(cases)):
        for kind in sorted(by):
            if i < len(by[kind]) and len(out) < n:
                out.append(by[kind][i])
        i += 1
    return out


def event(case):
    title = case["title"]
    ev = {"event_id": uuid.uuid4().hex, "timestamp": time.time(), "platform": "javascript", "level": "error",
          "environment": "demo", "fingerprint": [title], "tags": {"service": case["service"], "seeded": "true"}}
    if ": " in title:  # "TypeError: ..." → Sentry 가 제목을 "type: value" 로 만든다
        typ, value = title.split(": ", 1)
        ev["exception"] = {"values": [{"type": typ, "value": value}]}
    else:
        ev["logentry"] = {"formatted": title}
    return ev


def send(dsn, ev):
    u = urllib.parse.urlparse(dsn)
    project_id = u.path.strip("/").split("/")[-1]
    url = "%s://%s/api/%s/envelope/" % (u.scheme, u.hostname + (":%d" % u.port if u.port else ""), project_id)
    body = "\n".join([json.dumps({"event_id": ev["event_id"], "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
                      json.dumps({"type": "event"}), json.dumps(ev, ensure_ascii=False)]) + "\n"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers={
        "Content-Type": "application/x-sentry-envelope",
        "X-Sentry-Auth": "Sentry sentry_version=7, sentry_key=%s, sentry_client=merrybugmas-seed/1.0" % u.username,
        "User-Agent": "merrybugmas-seed/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--data", default=os.path.join(ROOT, "tests", "data", "triage_eval.jsonl"))
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    cases = pick([json.loads(l) for l in open(args.data, encoding="utf-8") if l.strip()], args.n)
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not args.dry and not dsn:
        sys.exit("SENTRY_DSN 이 없다 (.env 에 SENTRY_DSN=... 를 넣는다. 프로젝트 Settings → Client Keys)")
    ok = 0
    for c in cases:
        if args.dry:
            print("[dry] %-11s %s" % (c["kind"], c["title"][:70]))
            continue
        try:
            send(dsn, event(c))
            ok += 1
            print("보냄  %s" % c["title"][:70])
        except Exception as e:  # noqa: BLE001
            print("실패  %s  (%s: %s)" % (c["title"][:50], type(e).__name__, str(e)[:60]))
    if not args.dry:
        print("%d / %d 건 전송. Sentry 에 이슈로 뜨기까지 수 초~1분 걸린다." % (ok, len(cases)))


if __name__ == "__main__":
    main()
