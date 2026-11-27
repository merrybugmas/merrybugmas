# 트래픽 부하 화면

기존 버그 픽스 화면의 픽셀아트와 렌더러 및 키보드와 마우스 입력 방식을 재사용한 트래픽 전용 화면이다. `main`의 버그 픽스 화면을 새로 구현하지 않는다. 프론트와 실행 파일 및 문서는 모두 `traffic-scenario/` 안에 있다. 이 문서의 명령은 해당 폴더에서 실행한다. 상위 폴더의 기존 픽셀 렌더러는 수정 없이 불러온다.

## 현재 작업 폴더에서 실행

```bash
./run.sh
```

현재 `traffic-scenario/` 폴더에는 `.venv`가 준비돼 있다. 다른 환경에서는 Python 3.11 이상으로 먼저 설치한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
./run.sh
```

이 스크립트는 우리 FastAPI 백엔드와 트래픽 화면을 함께 실행한다. 8000번과 8001번 포트를 사용한다. `q`로 종료하면 스크립트가 시작한 서버도 종료된다. 진행 중 실험은 먼저 중지한 뒤 결과 수집을 기다린다. 로그는 `data/control-process.log`에 남는다.

기존 백엔드가 이미 실행 중이면 프론트만 실행한다.

```bash
python3 -B -m frontend --server http://127.0.0.1:8000
```

처음부터 트래픽 화면으로 들어간다. 이 모드에는 버그 픽스 화면 전환 버튼을 표시하지 않는다. 프론트에는 Textual 등 새 패키지가 필요하지 않다.

## 화면 조작

| 조작 | 동작 |
| --- | --- |
| 평소 방문 클릭 또는 `n` | 고객 1000명을 30초 동안 유입 |
| 고객 급증 클릭 또는 `t` | 고객 1000명을 2초 동안 유입 |
| 중지 클릭 또는 `x` | 추가 요청을 중지하고 전송한 결과 회수 |
| 재실험 클릭 또는 `r` | 같은 요청 계획으로 재실험 |
| 비교 클릭 또는 `c` | `demo-v1` 정책으로 기준 실험과 비교 |
| 고객 클릭 또는 `j/k`와 Enter | 실제 고객 요청과 측정 시간 조회 |
| 개선 안내 클릭 또는 `g` | 외부 성능 개선 에이전트 연결 안내 |
| Space | 화면 연출 정지. 백엔드 실험은 계속 진행 |
| `q` | 화면 종료 |

대기 고객은 실제 서버 이벤트를 사용한다. RPS가 증가했다는 이유로 고객을 만들어내지 않는다. 처리 중 고객은 파란색으로 완료는 초록색으로 실패는 빨간색으로 표현한다. 대기 고객은 안내소 앞에 줄을 서고 화면이 모자라면 화면 밖 인원을 표시한다. 이벤트 조회 주기 사이에 완료된 짧은 요청은 대기 장면 없이 퇴장 장면으로 보일 수 있다.

110열 이상에서는 오른쪽에 지표와 고객 상세를 표시한다. 좁은 화면에서는 마켓 아래에 표시한다. 최소 크기는 기존 화면과 같은 60열 × 22행이다. 오른쪽 상세를 함께 보려면 140열 × 44행을 권장한다.

## 왼쪽 Codex와 연결

기존 `market` 명령 파일과 `.runtime/state.json`을 그대로 사용한다.

```bash
./market experiment normal
./market experiment surge 1000 0.5
./market experiment stop
./market experiment replay
./market experiment compare demo-v1
./market experiment select customer-0001
./market status
```

`state.json`의 기존 필드는 유지하며 `experiment`에 실험 ID와 집계 및 에이전트 단계와 선택 고객을 추가한다. 팀원의 에이전트가 `POST /api/agent/progress`를 호출하면 실제 단계와 메시지를 표시한다. 개선 안내가 에이전트를 실행하거나 코드를 자동 수정하지는 않는다.

현재 프로젝트는 기본 비교 정책이 비어 있다. 비교하면 `POLICY_NOT_READY`가 표시된다. 팀이 정한 정책을 `config/policies.json`에 등록한 뒤 사용한다. 반복 횟수가 부족하면 `inconclusive`다.

## 기존 옆 패널과 연결

원래의 JSON 프레임 프로토콜을 그대로 지원한다.

```bash
python3 -B -m frontend --stream --size 100x30 --server http://127.0.0.1:8000
```

`blocks` 프레임과 키 및 클릭 명령 형식은 바꾸지 않았다. 실제 채팅 앱에 붙이는 설정은 내일 팀과 맞추면 된다. 별도의 웹 프론트는 만들지 않았다.

## 폴더 구조

```text
traffic-scenario/
├── backend/
├── frontend/
│   ├── app.py       # 트래픽 전용 실행과 입력 처리
│   └── session.py   # API 연결과 상태 및 화면 구성
├── tests/
├── docs/
├── market
└── run.sh
```

상위 폴더의 `xmas_market.py`와 `market` 및 README는 원래 상태로 복구했다. `frontend/app.py`에서 기존 `World`를 상속하고 그리기 유틸을 읽기 전용으로 재사용한다. 트래픽 코드가 공통 모듈을 덮어쓰거나 전역 함수를 바꾸지 않는다. 따라서 이 폴더는 공통 렌더러가 있는 merrybugmas 저장소 안에서 실행한다.

## 검증

```bash
.venv/bin/python -B -m pytest -q
```

백엔드 35개와 트래픽 프론트 7개 테스트를 실행한다. 옆 패널은 기존 JSON 프레임 형식을 사용한다. 자동 성능 개선 에이전트 실행은 별도 연결이 필요하다.
