---
name: performance-repair
description: 검색 서버에 유입이 몰린 뒤 고객이 검색 결과를 제때 받는지 확인하고 필요하면 고칠 때 쓴다. 트래픽 유입, 응답 지연, 대기열 초과, 검색 실패, 성능 개선 요청에 쓴다. 측정으로 상태를 판정하고 근거가 있을 때만 한 가지 원인을 고친 뒤 같은 요청으로 다시 검증한다. 화면 수정이나 새 기능 추가에는 쓰지 않는다.
---

# 성능 문제 대응

고객이 검색 결과를 제때 받지 못한다는 요청을 받았을 때의 절차다. 판정은 도구가 내고 진단과 수정은 직접 한다.

## 도구

`traffic-scenario/`에서 `.venv/bin/python -m agent_bridge <명령>`으로 실행한다. 결과는 JSON이다. 실패하면 오류 JSON을 내고 0이 아닌 코드로 끝난다.

| 명령 | 하는 일 |
|---|---|
| `snapshot` | 수정 전 코드와 DB 구조를 저장한다 |
| `adopt` | 화면에서 방금 보낸 실험을 넘겨받아 채점한다 |
| `replay <run_id> --repeat N` | 같은 요청 목록을 다시 보낸다 |
| `run --random` | 새 요청 목록을 보낸다 |
| `verify <run_id>` `status <run_id>` `logs <run_id>` | 실험 한 번을 다시 채점하거나 조회한다 |
| `scope` | 허용한 파일만 바뀌었는지 확인한다 |
| `restart <run_id>` | 검색 서버를 재시작해 수정을 반영한다 |
| `compare <기준 run_id> <후보 run_id>` | 수정 전후를 비교해 판정한다 |
| `rollback` | 코드와 DB 구조를 저장한 상태로 되돌린다 |
| `progress <run_id> <단계> <메시지>` | 작업 단계를 화면에 알린다 |

`progress`의 단계는 `analyzing` `modifying` `restarting` `testing` `comparing` `finished` `failed` 중 하나만 쓴다. 단계가 바뀔 때마다 알린다.

## 수정 범위

- 고칠 수 있는 파일은 `backend/catalog.py`와 `backend/target.py`다. DB에는 구조만 추가할 수 있다.
- 설정값과 상품 데이터와 실험 기록과 `agent_bridge/`와 `tests/`와 `backend/`의 다른 파일은 고치지 않는다.
- 검색 결과의 내용과 순서를 바꾸지 않는다. 빨라져도 결과가 달라지면 기각된다.
- `agent_bridge/faults/`는 읽지 않는다.
- DB 구조를 바꿀 때는 서버가 켜질 때 적용되도록 코드에도 넣는다. 채택된 수정은 PR로 올라가므로 DB에만 한 변경은 반영되지 않는다.
- 범위 밖의 파일이 바뀌면 `restart`와 `compare`가 거절한다.
- 커밋과 push와 PR 생성은 하지 않는다. 사람이 `pr` 명령으로 올린다.
- 코드를 고칠 때는 `AGENTS.md`의 주석 작성 규칙을 따른다.

## 절차

1. `snapshot`을 처음 한 번만 실행한다. 중간에 다시 실행하면 그때까지의 실험 기록이 비워진다.
2. `adopt`로 화면에서 보낸 실험을 넘겨받는다. 유입을 새로 보내지 않는다. 결과의 `judgement`에 따라 갈린다.
   - `measurement_invalid`: 측정을 믿을 수 없다. 서버 문제로 보지 않는다. 코드를 고치지 않고 `invalid_reason`을 보고한 뒤 끝낸다.
   - `healthy`: 고객 목표를 충족한다. 아무것도 고치지 않고 근거가 된 수치를 보고한 뒤 끝낸다.
   - `degraded` 또는 `wrong_results`: 다음 단계로 간다.
3. `replay <run_id> --repeat 2`로 기준 3회를 채운다. 3회 값이 얼마나 흔들리는지 적는다.
4. 증거를 모은다. 무엇을 어디서 볼 수 있는지는 [references/observing.md](references/observing.md)에 있다.
<!-- study-rule:start -->
5. 고치기 전에 `progress`로 세 가지를 남긴다. 원인에 대한 가설, 그 가설이 맞다면 수정 뒤에 관측될 결과, 그 가설이 틀렸다고 볼 조건이다.
<!-- study-rule:end -->
6. 한 번에 한 가지 원인만 고친다. 가설을 확인하는 데 필요한 가장 작은 변경을 한다.
7. `scope`로 허용한 파일만 바뀌었는지 확인한 뒤 `restart <run_id>`로 반영한다.
8. `replay <기준 run_id> --repeat 3`으로 같은 요청을 다시 보낸다.
9. `compare <기준 run_id> <후보 run_id>`의 `decision`을 따른다.
   - `accept`: `run --random`으로 새 요청 목록에서 한 번 더 확인한다. `healthy`면 변경을 유지하고 끝낸다. 아니면 `reject`처럼 처리한다.
   - `reject`: `rollback` 후 `restart`하고 다른 가설로 4번부터 다시 한다.
   - `hold` 또는 `measurement_invalid`: 개선됐다고 말하지 않는다. `reasons`를 읽는다. 다시 재려면 `rollback` 후 `restart`하고 `run --random`으로 새 요청 목록을 만들어 3번부터 다시 한 뒤 같은 수정을 다시 넣는다. 같은 요청 목록에 실험을 더 보태면 다시 무효가 된다. 다시 재도 같으면 `rollback` 후 `restart`하고 수정 시도 한 번으로 센다.

## 멈추는 조건

- 수정 시도는 세 번까지 한다. 세 번 안에 `accept`가 없으면 `rollback` 후 `restart`하고 확인한 사실만 보고한다.
- `healthy`와 `measurement_invalid`에서는 고치지 않는다.

## 보고

- 판정은 `agent_bridge`의 결과만 근거로 삼는다. 직접 계산한 수치로 개선을 주장하지 않는다.
- 숫자를 쓸 때 그 숫자가 무엇을 잰 것인지 함께 적는다.
- 수정했다면 바꾼 파일과 수정 전후의 `customer_success_rate`와 `decision`을 적는다.
