#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$HERE/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$HERE/../.venv/bin/python"
fi
if [ ! -x "$PYTHON" ]; then
  echo 'Python 3.11 이상으로 .venv를 만들고 requirements.lock을 설치하세요.'
  exit 1
fi
cd "$HERE"
mkdir -p data
"$PYTHON" -B - <<'PORT_CHECK'
import os
import socket
import sys

for port in (8000, int(os.getenv('MBM_TARGET_PORT', '8001'))):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            sys.exit(f'{port}번 포트가 사용 중입니다. 기존 마켓을 q로 종료한 뒤 다시 실행하세요.')
PORT_CHECK
"$PYTHON" -B -m backend serve > data/control-process.log 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT
echo '백엔드 준비를 기다리고 있습니다...'
if ! "$PYTHON" -B - <<'READY_CHECK'
import json
import time
import urllib.error
import urllib.request

for _ in range(300):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=1) as response:
            if json.load(response).get('status') == 'ok':
                break
    except (OSError, ValueError):
        pass
    time.sleep(.1)
else:
    raise SystemExit('백엔드 시작에 실패했습니다. data/control-process.log를 확인하세요.')
READY_CHECK
then
  tail -n 10 data/control-process.log
  exit 1
fi
"$PYTHON" -B -m frontend
