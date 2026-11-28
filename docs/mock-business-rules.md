# mock 비즈니스 규칙 (예시)

mock 이슈 제목(`ISSUE_CATALOG`)에 맞춘 예시 규칙이다. 실제 규칙은 고칠 레포에 `BUSINESS_RULES.md` 로 두고 담당 팀이 관리한다.

```bash
python3 xmas_market.py --rules docs/mock-business-rules.md    # mock 모드: OpenAI 0회
```

| id | 대상 | 규칙 | 조치 | 담당 |
|---|---|---|---|---|
| DLV-01 | app.js:showDelivery, 제목:Invalid time value | 배송 예정일은 물류팀 영업일 기준(SLA)으로 내려온다. 표시 형식·계산을 바꾸면 고객 안내 문구와 어긋난다 | 문의 | 물류팀 |
| GIFT-01 | app.js:renderGifts, 제목:reading 'map' | 품절 상품도 목록에서 숨기지 않는다 (품절 표시 유지). 빈 목록 처리 때 품절 상품을 거르지 않는다 | 주의 | 상품팀 |
| CAREER-01 | 제목:toSorted | 경력은 시작일 최신순이 기준이다. 정렬 기준을 바꾸지 않는다 | 주의 | 이력서팀 |

- 대상: `파일` · `파일:함수` · `제목:키워드` (쉼표로 여러 개, 키워드에는 쉼표를 쓰지 않는다)
  - 파일·함수: 실연동 이슈의 culprit(Sentry 가 주는 에러 위치 한 줄)과, 패치가 바꾸는 파일·diff 에 비교한다. 파일은 경로 끝이 같으면 맞는다 (`app.js` ↔ `src/app.js`)
  - 제목:키워드: 이슈 제목에 그 글자가 있으면 맞는다 (대소문자 무시). mock 이슈는 위치 정보가 없어서 이것으로만 걸린다
- 조치 `문의`: 담당 팀 확인 전에는 고치지 않는다. 쥐는 "팀 문의" 우리에 남고 질문 초안이 생긴다. 확인되면 `./market tell 루돌프 WEB-xxx 확인 완료 진행`
- 조치 `주의`: 고치되 규칙을 패치 요청과 Codex 리뷰에 같이 넘긴다
