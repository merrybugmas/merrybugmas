#!/usr/bin/env bash
# Codex 자율 운영 모드: Codex 가 운영자가 되어 N초마다 대시보드 현황을 읽고 병정에게 직접 지시한다.
#   사람 없이 "AI 운영자(Codex) → AI 병정(sentry-bot)" 이 돌아가는 모습을 보여 준다.
#   대시보드를 먼저 띄운 뒤 다른 창에서:  tools/codex-autopilot.sh [초, 기본 30] [횟수, 기본 무한]
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
INTERVAL="${1:-30}"
ROUNDS="${2:-0}"
case "$INTERVAL$ROUNDS" in *[!0-9]*) echo "사용법: $0 [초] [횟수]  (숫자)"; exit 2;; esac
command -v codex >/dev/null 2>&1 || { echo "codex CLI 가 필요하다 (npm i -g @openai/codex, codex login)"; exit 1; }

read -r -d '' PROMPT <<'P'
너는 지금 Ops Christmas Market 대시보드의 자율 운영자다. AGENTS.md 의 규칙을 따른다. 사람은 지켜보기만 한다.
1. `./market status` 를 읽는다. phase 가 play 가 아니면 `./market start ALL sentry` 로 스테이지를 연다.
2. 숫자를 보고 지금 가장 급한 것 하나를 판단한다. 예:
   - 오래 배회(roam) 중인 쥐가 있는데 노는 병정(home)이 있으면 → `./market tell <병정> <ID> 먼저 잡아`
   - 제목이 광고·확장·서드파티면 → `./market tell <병정> <ID> noise로 처리`
   - 5xx·보안처럼 사람이 봐야 하면 → `./market tell <병정> <ID> 사람에게`
   - rps 가 peak_rps 의 70% 이상이면 → `./market tell 엘프 보고` 로 엘프(트래픽 대응 에이전트)에게 상황을 묻는다
   - traffic.elf.pending_approval 이 있으면 → 번호표는 사람이 승인한다. 승인하지 말고 say 로 "번호표 승인 필요 - 사람 확인" 을 띄운다
   - traffic.elf.state 가 flag 면 → 레버로 안 풀리는 문제다. say 로 담당자 확인이 필요하다고 띄운다
   - 급한 게 없으면 아무 명령도 내리지 않는다
3. 명령은 최대 2개만 실행한다. 마지막으로 `./market say "🤖 운영자 판단: <근거 한 줄, 40자 이내>"` 를 실행한다.
4. 코드나 파일은 고치지 않는다. ./market 외의 명령은 쓰지 않는다. 답은 한 줄로 끝낸다.
P

RUN="${XMAS_MARKET_RUNTIME:-$HERE/.runtime}"; mkdir -p "$RUN"
OUT="$RUN/autopilot.last"; LOG="$RUN/autopilot.log"
n=0
while :; do
  # state.json 은 대시보드가 1초마다 쓴다. 10초 넘게 그대로면 대시보드가 꺼진 것
  mt="$(stat -f %m "$RUN/state.json" 2>/dev/null || stat -c %Y "$RUN/state.json" 2>/dev/null || echo 0)"
  if [ $(( $(date +%s) - mt )) -gt 10 ]; then
    echo "[autopilot] 대시보드가 안 떠 있다 - python3 xmas_market.py 를 먼저 실행 (5초 뒤 재시도)"
    sleep 5; continue
  fi
  n=$((n + 1))
  echo "[autopilot] #$n $(date +%H:%M:%S) Codex 판단 중…"
  "$HERE/market" say "🤖 운영자(Codex)가 상황을 보는 중… (#$n)" >/dev/null
  if codex exec --skip-git-repo-check --ephemeral -s workspace-write -C "$HERE" -o "$OUT" "$PROMPT" </dev/null >"$LOG" 2>&1; then
    echo "[autopilot] $(tail -n 3 "$OUT" 2>/dev/null)"
  else
    err="$(grep -m1 -E '^(ERROR|Error)' "$LOG" || tail -n 1 "$LOG")"
    echo "[autopilot] codex 실패: $err"
    "$HERE/market" say "🤖 운영자(Codex) 호출 실패 - ${err:0:40}" >/dev/null
  fi
  [ "$ROUNDS" -gt 0 ] && [ "$n" -ge "$ROUNDS" ] && break
  sleep "$INTERVAL"
done
