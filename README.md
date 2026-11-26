# merrybugmas — Ops Christmas Market (PoC)

터미널에서 보는 운영 대시보드. 지표를 크리스마스 마켓 장면으로 보여준다. **지금은 mock 데이터만 쓴다.**

```bash
./run-with-codex.sh                             # 왼쪽 Codex 채팅 + 오른쪽 대시보드 (tmux 분할, 없으면 Terminal 창 2개)
python3 xmas_market.py                           # 대시보드만 (60x22 이상, 256색, 의존성 없음)
```

## 채팅 ↔ 대시보드
Codex는 이 폴더에서 실행되고 `AGENTS.md`를 읽는다. 둘은 `.runtime/` 파일로 이어진다.
- 대시보드는 1초마다 `.runtime/state.json`에 현황(트래픽·쥐·병정·통계·로그)을 쓴다. Codex는 `./market status`로 읽고 답한다.
- Codex가 `./market focus WEB-2B1`, `./market say ...`, `./market outbreak 3` 등을 실행하면 `.runtime/cmd`를 거쳐 대시보드에 바로 반영된다.
- 사람도 같은 명령을 쓸 수 있다. 목록은 `./market help`.
- 예: "지금 쥐 몇 마리야?" → status를 읽고 답한 뒤 `say`로 한 줄 띄움. "WEB-xxx 먼저 잡아" → `focus`.

## 화면 구성 (게임처럼 진행)
1. **타이틀**: 눈발이 굵어지다가 가운데가 걷히며 `MERRY BUGMAS` 가 나온다. 아래 눈밭에서 병정이 쥐를 쫓는다. 아무 키(또는 클릭) → 메뉴.
2. **메뉴(SELECT STAGE)**: ① 서비스(ALL·WEB·SEARCH·RESUME·DISPLAY·MOBILE) → ② 시나리오(🎄 트래픽 / 🐭 Sentry 버그 / 📊 개요) → `▶ START`.
   `↑↓` 고르기, `←→`/`Tab` 열 전환, `Enter` 다음·시작, `Esc` 뒤로. 마우스 클릭도 된다. 서비스마다 트래픽 규모·버그 빈도가 다르다.
3. **스테이지**
   - **🎄 트래픽**: 방문객이 오른쪽 입구(`ENTRY`) 앞에 한 줄로 선다. 길면 뱀처럼 접혀 올라가고, 넘치면 왼쪽 화면 밖으로 밀려 `◀ +N명`. rps가 오르면 줄이 길어진다.
   - **🐭 Sentry 버그**: 위쪽 장면에서 쥐가 돌아다니고 병정이 출동해 잡는다. 이슈 목록은 장면 아래.
   - **📊 개요**: 트래픽 카드와 Sentry 카드(숫자·그래프·목록, 1초에 한 번 갱신). 카드를 클릭하면 그 장면으로.
- 스테이지 맨 윗줄: 서비스 이름, `◀ 메뉴` 탭, 화면 탭(클릭 가능). `Esc`/`m` 메뉴, `0` 개요, `1` 트래픽, `2` Sentry, `Tab` 순환.
  대기열을 보는 동안 새 쥐가 나오면 Sentry 탭이 `NEW+n`으로 깜빡인다.
- 채팅에서는 `./market start WEB sentry`, `./market view menu|traffic|sentry|overview`.

장면은 반블록 문자 `▀`로 그린 픽셀아트다. 한 칸에 위아래 픽셀 2개가 들어간다. 터미널 설정에서 "애매한 폭 문자를
2칸으로" 옵션이 켜져 있으면 화면이 깨지니 끈다(macOS 기본값은 꺼짐).

| 운영 지표 | 마켓 장면 |
|---|---|
| 트래픽 rps (Grafana 자리) | 입구 앞 대기열 길이. 한산함 → 활기참 → 북적북적 → 인파 경보 |
| Sentry 미해결 이슈 | 호두까기 인형의 쥐 (머리 위 라벨이 이슈 ID) |
| sentry-bot 에이전트 | 호두까기 병정. `SENTRY` 위병소에서 출동해 triage → fix → review → MR |

병정이 쥐를 처리하는 방식은 sentry-bot 판정을 그대로 옮겼다.
- **fixable**: 칼싸움(fix 진행바) → review → `MR !14xx` 생성, 쥐 처치
- **noise**: "shoo!" 하고 쫓아냄 (레포 밖 출처)
- **needs-human**: 왼쪽 `HUMAN` 우리에 가두고 담당자 호출
- **review block**(약 15%): 수정 반려, 쥐가 다시 풀려나고 다음 런에 재시도
- 쌓인 쥐가 많으면 병정이 2·3번까지 추가 출동

자동 연출은 90초 주기로 돈다. 12초에 특가 오픈으로 트래픽이 급증하고, 45초에 배포 직후 쥐 떼가 나온다.
키로 직접 일으킬 수도 있다.

| 키 | 동작 |
|---|---|
| `t` | 트래픽 급증 |
| `b` | 쥐 떼(이슈 5건) |
| `n` | 쥐 1마리 |
| `h` | 담당자가 우리 속 쥐 처리 |
| `a` | 자동 연출 on/off |
| `Esc` `m` | 메뉴로 |
| `0` `1` `2` / 클릭 | 개요 / 대기열 / 전투 화면 |
| `Tab` | 화면 순환 |
| `space` | 일시정지 |
| `q` | 종료 |

미리보기(비대화형): `python3 xmas_market.py --snapshot 50 --size 100x30 [--plain] [--view intro|menu|overview|traffic|sentry] [--service WEB]`
타이틀 건너뛰기: `python3 xmas_market.py --skip-intro`

## 실연동할 때
`MockSource`만 바꾸면 된다.
- `rps(t)`: Grafana(또는 Prometheus) 쿼리 결과
- `poll_issues(...)`: Sentry 신규 미해결 이슈. `kind`는 sentry-bot state의 triage 결과
- 병정 상태는 지금 시뮬레이션이다. 실연동에서는 sentry-bot 런 로그와 state를 읽어 움직이게 한다
