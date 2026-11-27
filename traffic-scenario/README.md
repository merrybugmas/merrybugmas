# Merry Bug Mas 트래픽 부하 시나리오

트래픽 프론트와 백엔드를 함께 실행하려면 이 폴더에서 `./run.sh`를 실행한다. 프론트 코드와 조작 방법은 [트래픽 화면 안내](docs/traffic-frontend.md)를 참고한다. 기존 공통 코드는 수정 없이 재사용한다.

엑셀 API 명세서 v0.2의 API 10개와 내부 수집 API 2개를 구현한 로컬 FastAPI 백엔드다. 크리스마스마켓 안내소의 실제 상품 검색을 대상으로 요청 대기와 처리 시간을 기록한다. 검색 응답을 만들기 위한 AI 호출이나 고정 `sleep`은 없다.

## 코드 구조

| 파일 | 역할 |
| --- | --- |
| `backend/target.py` | 실제 부하를 받는 안내소 검색 서버 |
| `backend/catalog.py` | 상품 데이터 생성과 검색 쿼리 |
| `backend/control.py` | 실험 제어와 화면 조회 API |
| `backend/manager.py` | 검색 서버와 부하 실행기 시작 및 종료 |
| `backend/loadgen.py` | 팀원 스크립트로 교체할 수 있는 참조 부하 실행기 |
| `backend/store.py` | 실험 상태와 이벤트 저장 및 집계 |
| `backend/comparison.py` | 실험 전후 정확성과 성능 판정 |
| `backend/models.py` | 요청과 응답 모델 및 입력 검증 |
| `backend/config.py` | 실행 설정과 데이터 경로 |
| `backend/common.py` | 공통 오류 응답과 JSON 및 해시 처리 |
| `backend/__main__.py` | 실행 명령 |

검색 서버와 제어 서버는 별도 프로세스다. 수정할 때는 검색 성능은 `catalog.py`부터 화면에 전달할 정보는 `control.py`와 `models.py`부터 확인한다.

## 실행

Python 3.11 이상을 사용한다. 이 폴더에서 실행한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
python -m backend serve
```

- 제어 서버: http://127.0.0.1:8000
- 제어 API 문서: http://127.0.0.1:8000/docs
- 검색 서버: http://127.0.0.1:8001
- 검색 API 문서: http://127.0.0.1:8001/docs

제어 서버가 검색 서버를 자식 프로세스로 시작하고 종료한다. 처음 실행할 때 합성 상품 10만 건을 생성하며, 기본 상태에는 검색 인덱스가 없다. 상품 데이터와 기록 DB는 `data/` 아래 분리된다. 두 서버 모두 로컬 주소만 사용한다. 포트가 이미 사용 중이면 다른 설정으로 실행한다.

```bash
MBM_TARGET_PORT=18001 MBM_TARGET_URL=http://127.0.0.1:18001 python -m backend serve --port 18000
```

## 첫 실험

별도 터미널에서 실행한다. 예시의 유입 기간과 시간 초과는 확정된 성능 조건이 아니라 테스트 입력이다.

```bash
curl -s http://127.0.0.1:8000/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"scenario_id":"surge","request_count":1000,"arrival_duration_s":20,"seed":42,"client_timeout_ms":5000}'
```

응답의 `run_id`를 아래 `RUN_ID` 자리에 넣는다.

```bash
curl -s http://127.0.0.1:8000/api/runs/RUN_ID
curl -s 'http://127.0.0.1:8000/api/runs/RUN_ID/events?after_seq=0&limit=200'
curl -s 'http://127.0.0.1:8000/api/runs/RUN_ID/logs?limit=20'
curl -s -X POST http://127.0.0.1:8000/api/runs/RUN_ID/stop
```

`status=completed`는 발생기의 실행이 끝났다는 뜻이다. 개별 요청이 실패해도 실험은 completed가 될 수 있다. 서버 이벤트까지 회수됐는지는 `telemetry_complete=true`로 확인한다. 다음 실험은 활성 실험과 이전 서버 요청이 없을 때 시작할 수 있다.

동일 조건 재실험은 기준 실험이 완료된 뒤 실행한다.

```bash
curl -s http://127.0.0.1:8000/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"source_run_id":"RUN_ID"}'
```

`source_run_id`와 다른 실험 설정을 함께 보내면 422다. 기준 계획의 고객 ID, 검색 조건, 도착 일정과 설정을 그대로 사용하고 실제 호출의 요청 ID는 새로 만든다.

## 구현된 API

| ID | Method | Path | 역할 |
| --- | --- | --- | --- |
| API-01 | GET | /api/guide/search | 카테고리·가격 검색, 상위 10개 |
| API-02 | POST | /api/runs | 신규 실험·동일 조건 재실험 |
| API-03 | POST | /api/runs/{run_id}/stop | 발생기 전송 중지, 기존 결과 회수 |
| API-04 | GET | /api/runs/{run_id} | 집계·실험 상태·실행 조건 |
| API-05 | GET | /api/runs/{run_id}/events | 순번 기반 이벤트 페이지 |
| API-06 | GET | /api/runs/{run_id}/requests/{request_id} | 고객 요청 상세 |
| API-07 | GET | /api/runs/{run_id}/logs | 수준 필터·커서 로그 조회 |
| API-08 | POST | /api/comparisons | 고정 정책에 따른 전후 비교 |
| API-09 | POST | /api/agent/progress | Codex의 작업 단계 전달 |
| API-10 | POST | /api/service/restart | 고정 명령으로 검색 서버 재시작 |
| INT-01 | POST | /internal/runs/{run_id}/events | 검색 서버 이벤트 수집 |
| INT-02 | POST | /internal/runs/{run_id}/measurements | 고객 관점 측정 원본 수집 |

운영용 보조 API로 양쪽 서버의 `GET /health`, 검색 서버의 `POST /internal/seal/{run_id}`가 추가되어 있다. seal은 참조 실행기가 서버 작업 종료를 확인한 뒤 마지막 이벤트 배치를 보내는 데 사용한다.

요청·응답 모델과 공통 오류 형식은 `/docs` 및 `/openapi.json`에서 확인할 수 있다. 입력 오류도 `error.code/message/request_id/details` 형식으로 반환한다. 검색 결과가 없으면 HTTP 200과 빈 `items` 배열을 반환한다.

## 검색 서버와 제어 서버

```text
왼쪽 Codex 도구 ── HTTP ── 실험 제어 서버 8000
                              ├─ 부하 실행기 별도 프로세스
                              │        └─ 검색 서버 8001 ── 상품 DB
                              ├─ 이벤트·측정 원본 수집
                              ├─ 실험 DB·원본 파일 보관
오른쪽 Textual ── HTTP 조회 ────┘
```

검색은 SQLite에서 실제로 조회·정렬하며 전용 스레드 풀에서 실행한다. 대기열에는 상한과 대기 시간 제한이 있다. 검색 서버만 재시작해도 제어 서버와 실험 기록은 유지된다.

검색 서버는 상태 전환을 짧은 배치로 모아 내부 수집 API에 전송한다. 디스크에 저장된 미전송 배치는 재시도하고, 수집 API는 동일 배치와 이벤트를 중복 반영하지 않는다. 다른 본문으로 같은 ID를 재사용하면 409다. 상태 이벤트에는 검색 조건, 최종 대기·처리 시간과 반환 결과가 남는다.

## 부하 스크립트 담당자와 연결

`backend/loadgen.py`는 백엔드 연결을 검증하기 위한 **교체 가능한 참조 실행기**다. 팀원의 최종 부하 스크립트와 별개다. `backend/manager.py`의 고정 실행 명령을 변경해 교체할 수 있다. API가 임의의 셸 명령이나 파일 경로를 받아 실행하지는 않는다.

실행기는 다음 파일을 입력받는다.

- `data/runs/{run_id}/config.json`
- `data/runs/{run_id}/request-plan.json`

입력 계획에는 `customer_id`, `query`, `scheduled_offset_ms`가 있다. 각 검색 호출에 `X-Run-ID`, `X-Customer-ID`, 호출별 고유 `X-Request-ID`를 보낸다. 응답 완료를 기다린 뒤 다음 요청을 보내는 폐쇄형 루프가 아니라, 도착 일정에 따라 요청을 보낸다. 최대 동시 요청 때문에 보내지 못한 건수는 `dropped`로 남긴다.

측정 결과는 INT-02로 최대 500건씩 전송한다. 마지막 배치에는 `final=true`와 집계 `summary`를 보낸다. `planned=sent+dropped`이고 `sent`는 저장된 요청별 최종 결과 수와 같아야 한다. 최종 결과와 검색 서버의 최종 이벤트 수집은 별개다.

참조 실행기는 표시용 `live-progress.json`도 원자적으로 갱신한다. 다른 실행기가 이 파일을 제공하지 않으면 진행 중 `client_counts.sent`는 회수된 결과 수를 기준으로 한 잠정값이다. 최종 집계는 INT-02의 원본과 summary로 확정한다. 마켓의 실시간 대기·처리 수는 서버 상태를 사용한다.

## Codex와 프런트 연결 지점

Codex 연결 도구는 API-02, 04, 07, 08, 09, 10을 호출하면 된다. 이 백엔드에는 Codex 자체나 MCP 서버가 포함되어 있지 않다. 코드 수정·스키마 반영·복구·MCP 연결은 에이전트 담당 범위다. `api/agent/progress`는 작업 표시를 위한 것으로 성능 수치나 성공 판정을 쓰는 API가 아니다.

재시작 API는 완료된 기준 실험 ID를 받고 202를 반환한다. 해당 실험의 이벤트에서 같은 `operation_id`의 `service.restarted`를 확인한 뒤 다음 실험을 시작한다. 실패는 `service.restart_failed` 이벤트로 남는다.

Textual은 먼저 API-04의 상태 스냅샷과 `last_seq`를 읽고, 이후 API-05에서 `after_seq=last_seq` 이후 이벤트를 읽는다. `has_more`가 true면 다음 페이지를 바로 회수한다. 접수·대기·처리·성공·실패를 고객 이동에 매핑하고 API-06으로 상세를 보여주면 된다.

`queue_wait_ms`는 검색 서버에서 측정한 최종 대기 시간이다. 대기 중의 `current_wait_ms`는 제어 서버가 queued 이벤트를 받은 뒤의 경과 시간으로 계산하는 **화면용 추정값**이다. 수집 지연까지 포함하는 정확한 실시간 대기 시간으로 사용하지 않는다.

## 비교 정책

`config/policies.json`은 기본적으로 비어 있다. 정책을 팀이 확정하기 전에는 API-08이 409 `POLICY_NOT_READY`를 반환한다. 예시 정책은 `config/policies.example.json`에 있으며 명시적으로 선택한 경우에만 사용한다.

```bash
MBM_POLICIES=config/policies.example.json python -m backend serve
```

예시 정책은 기준·후보 각 3회, 실행당 최소 100건, 최소 5% 개선, 다른 지연·처리량의 2% 초과 악화 금지, 실패율 증가 금지를 사용한다. 이 값은 프로젝트에서 검증된 최종 정책이 아니다.

반복 집단은 요청 계획·데이터·런타임·워밍업과 코드·스키마가 동일한 완료 실험으로 구성한다. API-08은 각 집단의 실행별 지표를 평균해 비교하며, 집단의 run ID와 정책 원문을 비교 결과 파일에 보관한다. 응답의 지표가 선택한 두 실행만의 값은 아닐 수 있다.

서버 최종 이벤트 미회수, 발생기 드롭, 고정 조건 불일치는 비교를 거절한다. 검색 결과는 변경 전 실험에 저장한 기준 정답과 비교한다. 후보 코드가 만든 정답은 개선 판정의 기준으로 사용하지 않는다. 표본·반복이 부족하면 `inconclusive`다. 비교 API는 판정만 반환하며 코드나 DB를 자동으로 반영·복구하지 않는다.

## 인덱스 개선을 수동 확인하는 방법

기준 실험과 결과 저장을 먼저 완료하고 백엔드를 종료한다. 실행 중인 DB에 수동으로 인덱스를 추가하면 실험 시작 시점의 스키마 기록과 어긋날 수 있다.

```bash
python -m backend index plan
python -m backend index add
python -m backend serve
```

기준 `source_run_id`로 재실험한다. 합성 데이터 내용은 같고 스키마 해시와 조회 계획만 달라져야 한다. 인덱스 추가 전 상태로 복원하려면 서버 종료 후 실행한다.

```bash
python -m backend index drop
```

실제 조회 계획과 효과는 데이터·장비·유입 속도에 따라 다르다. 매번 1,000건에서 실패가 발생하도록 결과를 꾸미거나 지연을 추가하지 않는다.

## 기록과 설정

`data/runs/{run_id}/`에 설정, 고객 도착 계획, 측정 원본, 서버 배치, 스키마, 조회 실행 계획, 코드·데이터·환경 해시와 결과 요약이 저장된다. 상품 검색을 시작하기 전에 동일한 기준 조회를 수행하는 `reference-query-warmup-v1`을 사용한다. 콜드 스타트 벤치마크는 아니다.

| 환경 변수 | 기본값 |
| --- | --- |
| MBM_DATA_DIR | 프로젝트의 data 폴더 |
| MBM_PRODUCT_COUNT | 100000. 이미 생성된 DB의 건수를 자동 변경하지 않음 |
| MBM_CONCURRENCY | 4 |
| MBM_QUEUE_LIMIT | 200 |
| MBM_QUEUE_TIMEOUT_MS | 3000 |
| MBM_MAX_INFLIGHT | 1000 |
| MBM_TARGET_PORT | 8001 |
| MBM_TARGET_URL | http://127.0.0.1:8001 |
| MBM_POLICIES | config/policies.json |
| MBM_MANAGE_TARGET | 1. 0이면 외부 서버 사용, 재시작 API 불가 |

환경 설정을 변경하면 고정 실험 조건이 달라지므로 기존 기준 실행과 재실험·비교가 거절될 수 있다. 처음부터 다른 조건으로 시작할 때는 새로운 `MBM_DATA_DIR`를 사용한다.

## 검증

```bash
python -m pytest -q
python tests/live_check.py --base http://127.0.0.1:8000
```

live_check는 서버가 실행 중일 때 실제 1,000건 부하, 이벤트·상세·로그, 작업 단계 중복 처리, 검색 서버 재시작, 동일 조건 재실험을 확인한다. 빈 기본 정책을 기준으로 실행하므로 정책을 별도로 등록한 환경에서는 마지막 검사를 조정해야 한다.

강제 종료한 프로세스는 자동으로 실험을 재개하지 않는다. 제어 서버 재시작 시 진행 중이던 실험은 failed로 보존한다. 회수되지 않은 서버 이벤트가 남으면 다음 실험을 막는다. 중단 원본과 outbox를 먼저 보관·점검하고, 새 검증을 시작하려면 별도 데이터 폴더를 사용한다. 이 버전은 자동 장애 복구를 갖춘 운영 플랫폼이 아니라 로컬 실험용 백엔드다.

프런트엔드, Codex/MCP 실행과 자동 코드·DB 복구는 아직 구현하지 않았다. 성능 수치와 개선율은 환경·조건·반복 횟수를 함께 기록하고, 실제 AI 에이전트가 수행하지 않은 수동 개선은 분리해서 설명한다.
