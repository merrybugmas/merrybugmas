# 아키텍처 — 코드를 고치러 온 AI·개발자용 지도

> 이 문서는 **코드를 읽고 고치는 사람/AI** 를 위한 것이다.
> 대시보드를 옆에 띄워 두고 채팅으로 **운영(지휘)** 하는 규칙은 [`AGENTS.md`](../AGENTS.md) 다 (그 문서의 "코드를 고치지 않는다"는 운영 채팅에만 해당한다).
> 실행 모드는 [`run-modes.md`](run-modes.md), 트래픽 에이전트 상세는 [`traffic-agent.md`](traffic-agent.md).

## 1. 30초 요약

- **무엇**: 운영 지표를 크리스마스 마켓 게임으로 그리는 터미널 대시보드. 트래픽 = 상점 앞 줄, Sentry 미해결 이슈 = 쥐,
  sentry-bot = 쥐를 잡는 병정 셋(`Soldier`), 트래픽 대응 에이전트 = 레버를 당기는 엘프(`elf.Elf`)
- **언어·의존성**: Python 3.8+ **표준 라이브러리만**. 화면은 `▀` 반블록 픽셀아트(256색). Pillow 는 `tools/` 의 이미지 생성에만
- **루프**: 12 FPS. `World.update(dt)` 로 상태를 진행 → `compose(world, w, h)` 가 ANSI 문자열 줄 목록을 만든다 → 터미널 또는 옆 패널(JSON 프레임)로 출력
- **외부와의 연결**은 전부 **백그라운드 스레드 + 큐**다. 메인 루프는 매 프레임 `poll()` 로 결과만 가져가고, 실패해도 멈추지 않는다
- **테스트**: `python3 -m unittest discover -s tests` (76개, 약 20초, 네트워크·OpenAI·Codex 없음)

## 2. 파일 지도

| 파일 | 역할 | 주요 이름 |
|---|---|---|
| `xmas_market.py` | 본체 (약 2,700줄): 그리기 유틸 · 스프라이트 · mock 데이터 · 등장인물 · `World` · 화면 구성 · 브리지 · 실행 | 아래 3절 |
| `elf.py` | 🧝 트래픽 대응 에이전트 (판단·상태 기계만, 그리기는 `World.draw_market`·`elf_segs`) | `Elf.update` · `pressure` · `decide` · `rule` · `act` · `command` · `snapshot` |
| `ai.py` | OpenAI 두뇌. Responses API + strict JSON Schema. 백그라운드 작업 큐 | `Brain`(`order`·`triage`·`brief`·`traffic`·`ask`) · `ReplayBrain` · `available()` · `load_dotenv()` |
| `sentry_live.py` | Sentry 미해결 이슈 읽기 (읽기 전용, `--live`). 새 이슈마다 최신 이벤트를 1회 읽어 스택 프레임(`frames`)을 붙인다 | `SentryFeed`(`fetch`·`fetch_frames`·`poll`·`reset`) · `to_issue` · `frames_from_event` |
| `traffic_live.py` | 서버 `/metrics`(Prometheus) 읽기 → rps·5xx·응답 시간·Web Vitals·레버 게이지. 레버 쓰기 클라이언트 | `TrafficFeed`(`sample`·`rps`·`metrics`·`levers`) · `LeverClient` · `admin_url()` |
| `fixer.py` | 실연동 fixable 이슈 → OpenAI 패치 → Codex 리뷰(`codex exec -s read-only`, 반려면 사람에게) → git worktree 브랜치 → `gh pr create` (머지 안 함) | `Fixer`(`submit`·`poll`·`fix`, `review` 주입) · `codex_review` · `ReviewRejected` · `apply_patch` · `check_syntax` |
| `market` | 채팅·사람용 CLI. `status` 는 `.runtime/state.json` 출력, 나머지는 `.runtime/cmd` 에 한 줄 추가 | |
| `run-with-codex.sh` | tmux 좌우 분할: 왼쪽 Codex, 오른쪽 대시보드(mock) | |
| `AGENTS.md` | **운영 채팅** AI 의 규칙 (명령 목록, status 필드, 답하는 법) | |
| `claude-mod/merrybugmas/hooks/register.tsx` | Claude Code 옆 패널 mod (`hooks.json` 이 등록): `--stream` 자식 프로세스를 띄워 JSON 프레임을 Raster 로 그림. `/merrybugmas [mock\|live] [noai] \| stop` | `startChild` · `send` · `ui.render` |
| `claude-mod/merrybugmas/hooks/pad.tsx` · `screen.tsx` | 패널 입력: `pad` 는 게임 화면 위 투명 입력판(키·클릭 → 게임), `screen` 은 맨 아래 한 줄(잡은 쥐 ID 클릭 → 기록 창) | |
| `tools/screenshots.py` · `diagram.py` | README 이미지·구조도 생성 (Pillow) | |
| `tools/eval_triage.py` | 트리아지 정확도 평가 (실제 OpenAI, 결과 `docs/eval/`) | |
| `tools/sentry_seed.py` · `codex-autopilot.sh` | Sentry 테스트 이벤트 넣기 / Codex 자율 운영 루프 (`XMAS_MARKET_RUNTIME` 으로 다른 대시보드에 붙일 수 있음) | |
| `tools/elf_live_eval.py` | 엘프만 실서버 `/metrics`·레버·실제 OpenAI 로 돌려 조치 기록을 남김 (쥐 없음 → 트리아지 0회) | |
| `recordings/ai.jsonl` | 실제 AI 트리아지 녹화 (`--replay`) | |
| `tests/test_smoke.py` · `test_elf.py` · `test_stack.py` | 43 + 17 + 16 테스트. 가짜 OpenAI(`FakeOpenAI`)·가짜 Codex 리뷰·가짜 `/metrics`·가짜 시계·가짜 Sentry 이벤트(`test_stack.EVENT`) | `new_world` · `step` · `wait_ai` |
| (별도 레포) [demo-web](https://github.com/merrybugmas/demo-web) | 실연동 시연 서버: 버그 심은 페이지, `/metrics`, 처리 슬롯 제한, `/admin/levers`, `loadgen.py` | |

## 3. `xmas_market.py` 안의 구역 (위에서 아래 순서)

| 구역 (주석 `# ----` 제목) | 내용 |
|---|---|
| 출력 유틸 | `cw`/`dw`(한글 폭 2칸) · `seg_line`(색 구간 → 한 줄) · `fit` · `PixelCanvas`(픽셀 버퍼 + 글자 오버레이, `set`·`rect`·`blit`·`put`·`lines`) |
| 스프라이트 | `RAT_*` · `SOLDIER*` · `ELF_SPR` · `VISITOR*` (문자 = 팔레트 키, `.` = 투명) |
| 타이틀 · 메뉴 | `FONT` · `SERVICES`(키·이름·트래픽 배율·버그 배율) · `SCENARIOS`(traffic/sentry/overview/agents) · `VIEW_NAMES` |
| 데이터 소스 | `ISSUE_CATALOG` · `MockSource`(`rps(t)`·`add_spike`·`poll_issues`·`make_issue`) — 실연동은 이 자리를 `traffic_live`·`sentry_live` 가 대신한다 |
| 등장인물 | `Queuer`(줄 선 손님) · `Mouse`(쥐: roam/held/caged/flee/dead) · `Soldier`(home/walk/triage/fix/review/shoo/flag/return) |
| 세계 `World` | 아래 표 |
| 화면 구성 | `header` · `log_lines` · `card`/`traffic_card`/`sentry_card`/`elf_segs` · `compose_overview` · `soldier_card`/`elf_card`/`compose_agents`(에이전트 오버뷰) · `compose_intro` · `compose_menu` · `issue_lines` · `history_lines` · **`compose()`**(진입점) |
| 브리지 | `Bridge`: `.runtime/cmd` 읽기(추가된 줄만) · `.runtime/state.json` 쓰기(1초마다, 원자적 교체) |
| 실행 | `handle_key`/`handle_input`(키·마우스) · `attach_ai`/`attach_live`/`attach_traffic`/`attach_fixer`/`attach_live_all` · `run_interactive`(TTY) · `run_stream`(패널: JSON 프레임, `.runtime/pane_cmd` 입력) · `main`(argparse) |

`World` 메서드 묶음:

| 묶음 | 메서드 |
|---|---|
| 스테이지 | `start_stage`(서비스가 바뀌면 새 판: 쥐·통계·엘프 초기화, `stage_gen += 1`) · `set_view` · `menu_*` |
| 병정 명령 | `order`(엘프 앞말이면 `elf.command`, AI 면 `ai.submit("order")`, 아니면 `order_keywords`) · `apply_ai_order` · `dispatch` · `do_order` |
| 쥐 | `spawn_mouse`(AI 면 `triage` 제출) · `poll_new_issues` · `outbreak` · `record`(처리 기록 `history`) · `start_real_fix`/`pump_fixer` |
| 트래픽 | `traffic_spike` · `queue_target`/`update_queue`(줄 길이 = rps ÷ peak, 계산 속도 × `elf.relief()`) · `web_metrics` |
| 갱신 | **`update(dt)`**: `pump_ai` → `pump_fixer` → rps 읽기 → 새 이슈 → `update_queue` → `elf.update` → 쥐 → 병정 → 파티클 |
| AI 결과 | `pump_ai` → `_apply_ai(kind, gen, obj, res, err)`: `triage`·`order`·`brief`·`traffic`(→ `elf.on_ai`). `gen != stage_gen` 이면 버림 |
| 채팅 | `command`(예외를 로그로 삼킴) → `_command`(op 분기) · **`snapshot_state()`**(= `./market status` JSON) · `report_data`/`report`/`write_report`(사후 보고서: 표는 기록, AI 는 요약만) |
| 그리기 | `draw(view)` → `draw_bugs`(Sentry 전투) / `draw_market`(트래픽: 노점·트리·상점·점원·엘프·줄) · `crowd_label` |

## 4. 데이터 흐름

```
           mock                       --live (.env 설정분만)
 트래픽  MockSource.rps(t)      또는  TrafficFeed ──(스레드, 1초)── GET  서버 /metrics
 이슈    MockSource.poll_issues 또는  SentryFeed  ──(스레드, 15초)── GET  Sentry API (읽기 전용)
                    │
                    ▼
            World.update(dt)  ── 12 FPS ──▶ compose() ──▶ 터미널(ANSI)  /  run_stream: JSON 프레임 → 패널 mod
             │   │   │    ▲
             │   │   │    └── pump_ai ◀── ai.Brain 결과 큐 ◀──(스레드)── OpenAI Responses API
             │   │   └─ elf.update ──▶ LeverClient ──(스레드)── POST 서버 /admin/levers   (--live + 레버 있는 서버만)
             │   └──── Soldier.update ──▶ fixer.Fixer ──(스레드)── OpenAI 패치 → codex exec 리뷰 → git worktree · gh pr create  (--live + XMAS_FIX_REPO)
             ▼
   Bridge.write_state ──▶ .runtime/state.json ──▶ ./market status ──▶ 채팅 AI (AGENTS.md)
   Bridge.read_commands ◀── .runtime/cmd ◀── ./market <명령> ◀── 채팅 AI / 사람
   run_stream ◀── .runtime/pane_cmd ('번호<TAB>key x|click X Y|size WxH|명령') ◀── 패널 mod
```

## 5. 지켜야 할 규칙 (불변식)

1. **표준 라이브러리만** (`tools/` 의 Pillow 제외). 새 의존성을 넣지 않는다
2. **대시보드는 멈추지 않는다**: 네트워크·OpenAI·git 은 스레드에서, 결과는 큐로. 실패는 로그 한 줄 + mock/키워드/규칙으로 폴백. `World.command` 는 예외를 삼킨다
3. **world 는 메인 스레드에서만 바꾼다** (`pump_*` 에서 결과 반영)
4. **판이 바뀐 뒤 도착한 결과는 버린다** (`stage_gen` 비교)
5. **OpenAI 비용**: mock 모드(`--live` 없음)는 `--ai` 없으면 0회 (`main` 에서 `args.no_ai` 결정). 엘프는 경보당 1회(규칙이 먼저 당기고 AI 는 다음 조치), 실행당 `XMAS_AI_TRAFFIC_MAX`. 엘프 채팅 명령은 키워드만
6. **사람 우선**: 사람이 정한 판정을 늦게 온 AI 가 덮지 않는다(`issue["ai"]="manual"`). 번호표(요청 거절)는 사람 승인 없이 켜지 않는다. PR 머지는 사람이 한다
7. **지어내지 않는다**: 실연동 중에는 가짜 쥐·가짜 급증을 만들지 않는다. 서버가 안 준 지표는 `None`(표시 안 함). 레버 없는 서버에서 레버를 당긴 척하지 않는다. mock 추정값은 `mock: true` 로 표시
8. **읽기 전용**: Sentry 에는 쓰지 않는다. 쓰기는 demo-web 레버(localhost)와 fixer 의 PR 브랜치뿐. Codex 리뷰어는 `-s read-only` 샌드박스로만 돈다
9. 비밀값은 `.env`(git 제외). 코드·문서·로그에 키를 넣지 않는다

## 6. 바꿀 때 같이 고칠 곳 (체크리스트)

| 하려는 일 | 고칠 곳 |
|---|---|
| `./market` 명령 추가 | `World._command` 분기 → `market` 도움말 → `AGENTS.md` 명령 목록 → 테스트 |
| `status` 필드 추가 | `World.snapshot_state` (엘프는 `Elf.snapshot`) → `AGENTS.md` 의 필드 설명 → 테스트 |
| 새 AI 작업 | `ai.py` 에 스키마·프롬프트·`Brain.<kind>` → `World._apply_ai` 에 분기 → 테스트는 `FakeOpenAI({"<schema name>": ...})` → 호출 횟수 상한을 정하고 `docs/run-modes.md` 에 적기 |
| 엘프 레버 추가 | `elf.BASE`·`LEVER_WORD`·`rule`·`can`·`command` → `ai.LEVERS`·`TRAFFIC_PROMPT` → demo-web `server.py`(`LEVERS`·`set_levers`·게이지) → `traffic_live.LEVER_GAUGES` → 그리기(`draw_market`·`elf_segs`) |
| 새 실연동 소스 | `*_live.py` 에 Feed(스레드 + `poll`/값 읽기, `transport` 주입 가능하게) → `attach_*` 와 `attach_live_all` → mock 폴백 로그(`live_failed`) |
| 화면 요소 추가 | `PixelCanvas` 에 그리기(`draw_market`/`draw_bugs`) → 글자 줄은 `seg_line`(폭 계산은 `dw`) → `RenderTest` 가 24개 화면 × 작은 터미널을 그려 본다 |
| 패널 동작 | `claude-mod/.../register.tsx` (세션 시작 때 읽힘: 바꾸면 Claude Code 재시작) · 입력은 `run_stream` 의 `pane_cmd` 처리 |

## 7. 테스트 쓰는 법

```python
from test_smoke import FakeOpenAI, new_world, step, wait_ai   # tests/ 안에서
w = new_world("WEB", "traffic")          # 시드 고정, 자동 연출 끔, 100x30 으로 한 번 그림
step(w, 3)                               # 3초 진행 (12 FPS)
w.ai = ai.Brain(key="k", transport=FakeOpenAI({"traffic_lever": {...}}), workers=1)
wait_ai(w, lambda: w.elf.state == "watch")   # 백그라운드 결과가 반영될 때까지
```
- mock rps 고정: `w.src.rps = lambda t: 3300.0`
- 실연동은 `TrafficFeed(transport=..., clock=...)`, `LeverClient(url=..., transport=...)`, `SentryFeed(transport=...)` 로 가짜를 끼운다
- 네트워크·실제 OpenAI 를 부르는 테스트는 넣지 않는다 (`tools/eval_triage.py` 처럼 별도 도구로)

## 8. 자주 하는 실행

```bash
python3 xmas_market.py                         # mock, OpenAI 0회
python3 xmas_market.py --snapshot 20 --view traffic --service ALL --plain   # TTY 없이 20초 뒤 한 프레임
python3 xmas_market.py --stream --size 100x30  # 패널용 JSON 프레임 (stdout)
python3 -m unittest discover -s tests -v
./market status | python3 -m json.tool | head  # 대시보드가 떠 있을 때
```
