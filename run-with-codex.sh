#!/usr/bin/env bash
# 왼쪽 Codex 채팅 + 오른쪽 크리스마스 마켓 대시보드를 한 화면에 띄운다.
#   tmux 가 있으면 한 창을 좌우 분할, 없으면(macOS) Terminal 창 두 개를 좌우로 붙인다.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DASH="python3 '$HERE/xmas_market.py'"
CHAT="cd '$HERE' && codex"

if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t xmas 2>/dev/null || true
  tmux -2 new-session -d -s xmas -x "$(tput cols)" -y "$(tput lines)" "$DASH"
  tmux set-option -t xmas default-terminal screen-256color
  tmux set-option -t xmas mouse on            # 클릭으로 좌우 창 전환
  tmux set-option -t xmas status-style "bg=colour52,fg=colour230"
  tmux set-option -t xmas status-left " 🎄 xmas  "
  tmux set-option -t xmas status-right " 왼쪽: Codex 채팅 | 오른쪽: 대시보드 | 종료: Ctrl-b d "
  tmux set-option -t xmas status-right-length 80
  tmux split-window -h -b -l 40% -t xmas "$CHAT"
  [ -n "${TMUX:-}" ] && exec tmux switch-client -t xmas
  exec tmux -2 attach -t xmas
fi

[ "$(uname)" = Darwin ] || { echo "tmux 를 설치하거나 두 터미널에서 각각 실행: $CHAT  /  $DASH"; exit 1; }
osascript - "$CHAT" "$DASH" <<'OSA'
on run argv
  set chatCmd to item 1 of argv
  set dashCmd to item 2 of argv
  tell application "Finder" to set b to bounds of window of desktop
  set W to item 3 of b
  set H to item 4 of b
  set split to (W * 0.38) as integer
  tell application "Terminal"
    activate
    set chatTab to do script chatCmd
    set bounds of front window to {0, 25, split, H}
    set dashTab to do script "clear; " & dashCmd
    set bounds of front window to {split, 25, W, H}
  end tell
end run
OSA
