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
"$PYTHON" -B -m backend serve > data/control-process.log 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT
"$PYTHON" -B -m frontend
