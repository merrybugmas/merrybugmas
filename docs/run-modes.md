# 실행 모드 — mock · live · AI · 패널

> 요약: **데이터**(mock / live)와 **두뇌**(OpenAI 켬 / 끔 / 녹화 재생)를 따로 고른다.
> 키·로그인 없이도 전부 mock 으로 돈다. 실연동은 `.env` 에 설정된 것만 붙고 나머지는 mock 이다.
> **mock 모드(`--live` 없음)는 OpenAI 를 부르지 않는다** — 키가 있어도 0회. mock 에서 AI 를 보려면 `--ai`.

## 1. 데이터: mock / live

| 실행 | 쥐 (Sentry) | 줄 (트래픽) | 엘프 레버 | 배지 |
|---|---|---|---|---|
| `python3 xmas_market.py` | mock 이슈 (`MockSource.make_issue`) | mock rps (`MockSource.rps`, 12초에 특가 급증) | 대시보드 안에서 흉내 | `MOCK` |
| `python3 xmas_market.py --live` | `.env` 에 `SENTRY_*` 가 있으면 실제 미해결 이슈 (읽기 전용) | `.env` 에 `TRAFFIC_METRICS_URL` 이 있으면 실제 서버 rps·5xx·응답 시간 | 서버에 `/admin/levers` 가 있으면 실제로 바꾼다 | `LIVE SENTRY+TRAFFIC` |

- `--live` 인데 설정이 없거나 첫 읽기가 실패한 쪽은 로그에 이유를 남기고 mock 으로 돈다 (`live.on` · `live.traffic` 로 확인)
- 실연동 중에는 가짜 쥐(`b`·`n`)·가짜 급증(`t`)을 만들지 않는다
- mock 의 엘프 5xx·응답 시간은 부하로 추정한 값이다 (`traffic.elf.pressure.mock: true`). 실측 웹 지표 `traffic.web` 은 실연동일 때만 채워진다

## 2. 두뇌: OpenAI

| 옵션 | 병정 명령 해석 | 쥐 판정 | 엘프 판단 | OpenAI 호출 |
|---|---|---|---|---|
| mock 모드 기본 (`--live` 없음) · 키 없음 · `--no-ai` · `XMAS_AI=off` | 키워드 | mock 판정 | 규칙 | **0** |
| 키 있음 + `--live` (또는 mock 에서 `--ai`) | OpenAI | OpenAI (쥐 1마리당 1회) | 경보마다 1회(다음 조치), 실행당 최대 `XMAS_AI_TRAFFIC_MAX`(5) | 아래 참고 |
| `--replay` | 키워드 | 녹화된 실제 AI 판정 재생 | 규칙 | 0 |

호출 횟수 감 잡기 (키가 있을 때):
- 쥐 판정: mock 이면 쥐가 나올 때마다(몇 분이면 수십 회), live 면 실제 미해결 이슈 수만큼
- 병정에게 말 걸기 1회당 1회, `./market brief` 1회, `./market report` 1회
- 엘프: 실행당 최대 5회. **엘프 채팅 명령은 0회**
- 실제 수정 PR: fixable 실연동 이슈 1건당 패치 1회 (실행당 `XMAS_FIX_MAX`). 그 PR 의 Codex 리뷰는 OpenAI API 가 아니라 Codex 사용량
- Codex 운영자(`tools/codex-autopilot.sh`): Codex 사용량만. 다만 AI 가 켜진 대시보드에 `tell` 을 보내면 그 명령 해석에 OpenAI 1회씩
- mock 은 기본이 0회다. `--live` 로 띄우면서 크레딧을 아끼려면 `--no-ai`

## 3. Claude Code 옆 패널

`claude --plugin-dir claude-mod/merrybugmas` 로 이 폴더에서 열면 옆 패널에 대시보드가 뜬다 (세션 시작 시 자동으로 `--live`).

| 명령 | 데이터 | OpenAI |
|---|---|---|
| `/merrybugmas` | live (`.env` 설정분) | 켬 |
| `/merrybugmas mock` | Sentry·트래픽 **둘 다 mock** (`.env` 실연동 설정 무시) | **끔** (크레딧 0) |
| `/merrybugmas noai` | live | 끔 |
| `/merrybugmas stop` | 끄고 패널 닫기 | |

고른 모드는 다음 `/merrybugmas` 까지 유지된다. 패널 코드(`claude-mod/merrybugmas/hooks/register.tsx`)는 세션 시작 때 읽히므로 바꾼 뒤에는 Claude Code 를 다시 연다.

## 4. 환경변수 (`.env`, git 제외)

| 이름 | 뜻 |
|---|---|
| `OPENAI_API_KEY` · `XMAS_AI_MODEL`(gpt-5-mini) · `XMAS_AI_EFFORT`(low) · `XMAS_AI=off` | OpenAI |
| `XMAS_AI_TRAFFIC_MAX` (5) | 엘프가 한 번 실행에 OpenAI 에 물을 최대 횟수 |
| `SENTRY_AUTH_TOKEN` · `SENTRY_ORG` · `SENTRY_PROJECT` (+ `SENTRY_URL` · `SENTRY_LIMIT` · `SENTRY_POLL`) | Sentry 실연동 (읽기 전용) |
| `TRAFFIC_METRICS_URL` (+ `TRAFFIC_METRIC` · `TRAFFIC_PEAK`(100) · `TRAFFIC_POLL`(1)) | 트래픽 실연동 |
| `TRAFFIC_ADMIN_URL` | 엘프 레버 주소 (없으면 `TRAFFIC_METRICS_URL` 의 경로를 `/admin/levers` 로) |
| `XMAS_FIX_REPO` · `XMAS_FIX_MAX` | 병정의 실제 수정 PR (`--live` + OpenAI 필요) |
| `XMAS_FIX_REVIEW`(codex) · `XMAS_FIX_REVIEW_TIMEOUT`(90) | PR 전에 Codex 리뷰 (`codex` CLI 로그인 필요, `off` 로 끔). 시간 초과면 1회 재시도, 그래도 안 되면 PR 없이 사람에게. API 크레딧이 아니라 Codex 사용량을 쓴다 |
| `XMAS_RULES` (또는 `--rules MD`) | 비즈니스 규칙 표 경로. 없으면 `XMAS_FIX_REPO` 의 `BUSINESS_RULES.md`, 그것도 없으면 규칙 없음(지금과 같음). 규칙 비교는 AI 없이 해서 OpenAI 호출 수는 그대로다. 걸린 규칙이 있으면 패치 요청·Codex 리뷰 입력에 규칙 글이 붙는다. mock 예시: `docs/mock-business-rules.md` |
| `DEMO_CAPACITY` (demo-web) | 데모 서버 동시 처리 슬롯 기본값 (2) |

## 5. 실연동 데모 한 벌

```bash
cd ~/demo-web && python3 server.py              # http://localhost:8765 (+ /metrics, /admin/levers)
python3 loadgen.py 25                            # 평소 손님 25 rps (급증: --spike 110 --at 15 --for 30)
cd ~/merrybugmas && python3 xmas_market.py --live   # .env: TRAFFIC_METRICS_URL=http://localhost:8765/metrics
```
