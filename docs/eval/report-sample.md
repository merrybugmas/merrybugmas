# WEB 서비스 단기 에러 대응 보고

2026-11-27 18:27 · 서비스 WEB · 1.8분 · 데이터 mock

## 요약 (AI)

- 단기간 Sentry 알림으로 오류 대응을 수행했습니다.
- 자동 레버(cache/capacity)로 트래픽 완화 후 정상화됐습니다.
- 고정(fixed) 이슈 11건, 잡음(noise) 7건, 사람이필요 4건이 남아있습니다.

## 원인별 (AI)

- 시간 파싱 관련 RangeError 다수 — 관련 MR(1401·1402·1403·1404·1407·1409·1410·1411)로 수정되어 해결(총 고정 일부 포함).
- undefined 참조(TypeError: .map) 일부 — MR(1405·1406·1408)로 수정되어 해결.
- ChunkLoadError 및 MetaMask inpage.js 오류는 외부/클라이언트 잡음으로 판정되어 noise로 처리(총 7건).
- <obscura:bootstrap> globalThis.fetch override 및 API 503은 사람이 확인해야 하는 이슈로 분류되어 caged 상태로 보류(사람 필요 4건).

## 사람이 할 일 (AI)

- [ ] 검토/조치 필요: WEB-JK (<obscura:bootstrap> globalThis.fetch override) — needs-human, caged
- [ ] 검토/조치 필요: WEB-L2 (<obscura:bootstrap> globalThis.fetch override) — needs-human, caged
- [ ] 검토/조치 필요: WEB-LL (API Error: 503 /api/member/info) — needs-human, caged
- [ ] 검토/조치 필요: WEB-MD (<obscura:bootstrap> globalThis.fetch override) — needs-human, caged

## 숫자 (기록 그대로)

| 처리한 이슈 | 수정 | noise | 사람에게 | review 반려 | 열린 PR | 엘프 조치 |
|---|---|---|---|---|---|---|
| 22 | 11 | 7 | 4 | 3 | 0 | 2 |

## 처리한 이슈

| 결과 | 이슈 | 판정 | 병정 | PR·MR | 근거 | 제목 |
|---|---|---|---|---|---|---|
| ! 사람 | WEB-JK | needs-human | 진저 | - |  | <obscura:bootstrap> globalThis.fetch override |
| ✔ 수정 | WEB-JC | fixable | 루돌프 | !1401 |  | RangeError: Invalid time value |
| ✖ noise | WEB-J6 | noise | 진저 | - |  | ChunkLoadError: Loading chunk 7967 failed |
| ✖ noise | WEB-IW | noise | 루돌프 | - |  | ChunkLoadError: Loading chunk 7967 failed |
| ✖ noise | WEB-J1 | noise | 진저 | - |  | MetaMask inpage.js: Object.connect failed |
| ✔ 수정 | WEB-JS | fixable | 루돌프 | !1402 |  | RangeError: Invalid time value |
| ✖ noise | WEB-JU | noise | 진저 | - |  | ChunkLoadError: Loading chunk 7967 failed |
| ✖ noise | WEB-K2 | noise | 루돌프 | - |  | ChunkLoadError: Loading chunk 7967 failed |
| ✔ 수정 | WEB-K7 | fixable | 진저 | !1403 |  | RangeError: Invalid time value |
| ✔ 수정 | WEB-KJ | fixable | 진저 | !1404 |  | RangeError: Invalid time value |
| ✔ 수정 | WEB-KG | fixable | 루돌프 | !1405 |  | TypeError: Cannot read properties of undefined (reading 'map |
| ✔ 수정 | WEB-KP | fixable | 루돌프 | !1406 |  | TypeError: Cannot read properties of undefined (reading 'map |
| ✔ 수정 | WEB-KK | fixable | 진저 | !1407 |  | RangeError: Invalid time value |
| ✔ 수정 | WEB-KU | fixable | 루돌프 | !1408 |  | TypeError: Cannot read properties of undefined (reading 'map |
| ! 사람 | WEB-L2 | needs-human | 진저 | - |  | <obscura:bootstrap> globalThis.fetch override |
| ✖ noise | WEB-LE | noise | 진저 | - |  | MetaMask inpage.js: Object.connect failed |
| ✔ 수정 | WEB-LA | fixable | 루돌프 | !1409 |  | RangeError: Invalid time value |
| ! 사람 | WEB-LL | needs-human | 진저 | - |  | API Error: 503 /api/member/info |
| ✔ 수정 | WEB-LS | fixable | 진저 | !1410 |  | RangeError: Invalid time value |
| ✖ noise | WEB-M4 | noise | 루돌프 | - |  | MetaMask inpage.js: Object.connect failed |
| ✔ 수정 | WEB-LZ | fixable | 진저 | !1411 |  | RangeError: Invalid time value |
| ! 사람 | WEB-MD | needs-human | 루돌프 | - |  | <obscura:bootstrap> globalThis.fetch override |

## 🧝 트래픽 대응 (엘프)

| 초 | 레버 | 누가 | 근거 | 결과 | AI 의견 |
|---|---|---|---|---|---|
| 22.1 | cache | 규칙 | 5xx 12.0%, 응답 320ms | 부족: 5xx 12.0→1.0% · 응답 320→192ms · 부하 1.10→0.92 | - |
| 32.1 | capacity | 규칙 | 부하 0.92 | 효과 있음: 5xx 1.0→0.0% · 응답 192→40ms · 부하 0.92→0.46 | - |
| 69.9 | capacity | 원복 | 잠잠해짐 | 원래대로 | - |
| 74.9 | cache | 원복 | 잠잠해짐 | 원래대로 | - |

## 다음에 (AI 제안)

- 장애 발생 시 자동 레버 동작 로그와 결과를 대시보드에 명확히 남겨 원인 추적을 용이하게 합니다.
- needs-human으로 분류된 fetch override·API 503 이슈 우선 조사 후 재발 조건·완화 방안(예: 서킷브레이커) 결정합니다.
- 고정 처리된 MR 목록(MR 1401~1411)을 바탕으로 릴리스 노트에 반영하고 관련 예외 처리 케이스를 보강합니다.

---
숫자와 표는 대시보드 기록 그대로다. '(AI)' 절은 그 기록만 넘겨 OpenAI 가 쓴 초안이니 사람이 확인한다.
