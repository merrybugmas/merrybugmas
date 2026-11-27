# AI 트리아지 평가 — gpt-5-mini

- 일시: 2026-11-27 14:58
- 데이터: `tests/data/triage_eval.jsonl` (32건, 정답은 사람이 붙임)
- **정확도: 29 / 32 = 90.6%**
- 응답 시간: 평균 4.16초 · 중앙값 4.14초 · 최대 5.28초
- 호출 실패: 0건

## 혼동 행렬 (행 = 정답, 열 = AI 판정)

| 정답 \ AI | fixable | noise | needs-human |
|---|---|---|---|
| fixable | 12 | 1 | 1 |
| noise | 0 | 9 | 1 |
| needs-human | 0 | 0 | 8 |

## 건별 결과

| | 서비스 | 제목 | 정답 | AI | 근거 | 수정 요약 |
|---|---|---|---|---|---|---|
| ✅ | WEB | TypeError: Cannot read properties of undefined (reading 'map | fixable | fixable | undefined 값에 map 호출 — 방어 로직 필요 | undefined 체크 추가 및 방어 코드 |
| ✅ | WEB | RangeError: Invalid time value | fixable | fixable | 잘못된 날짜 파싱으로 Invalid Date 발생 | 날짜 파싱 유효성 검사 추가 |
| ✅ | SEARCH | Hydration failed: server/client text mismatch | fixable | fixable | SSR과 클라이언트 출력 불일치 | 서버·클라 텍스트 불일치 수정 |
| ✅ | RESUME | TypeError: e.toSorted is not a function | fixable | fixable | 일부 브라우저에서 toSorted 미지원 | toSorted 폴리필 또는 대체 적용 |
| ❌ | DISPLAY | SecurityError: sessionStorage access denied | fixable | noise | 브라우저 환경/프레임 제약으로 세션스토리지 접근 차단 |  |
| ✅ | MOBILE | TypeError: null is not an object (evaluating 'el.scrollTop') | fixable | fixable | DOM 요소 null 접근으로 인한 예외 | 요소 존재 체크 후 scrollTop 사용 |
| ✅ | WEB | ChunkLoadError: Loading chunk 7967 failed | noise | noise | 배포/네트워크로 인한 일시적 청크 로드 실패 |  |
| ✅ | MOBILE | Failed to fetch (ad.doubleclick.net) | noise | noise | 서드파티 광고 도메인 네트워크 오류 |  |
| ✅ | WEB | MetaMask inpage.js: Object.connect failed | noise | noise | 브라우저 확장(MetaMask) 오류, 우리 코드 아님 |  |
| ✅ | MOBILE | API Error: 503 /api/member/info | needs-human | needs-human | 백엔드 503 응답—서버측 장애 가능성 |  |
| ✅ | SEARCH | <obscura:bootstrap> globalThis.fetch override | needs-human | needs-human | globalThis.fetch가 덮어쓰기됨 — 런타임 변조 |  |
| ✅ | WEB | TypeError: Cannot destructure property 'id' of 'user' as it  | fixable | fixable | user가 undefined인 상태에서 구조분해 | user undefined 안전 처리 추가 |
| ✅ | SEARCH | ReferenceError: structuredClone is not defined | fixable | fixable | 브라우저 미지원 API 사용으로 런타임 오류 | structuredClone 폴리필/대체 적용 |
| ✅ | RESUME | Error: Minified React error #31: Objects are not valid as a  | fixable | fixable | 객체를 React 자식으로 렌더링함 | 객체 렌더링 방지 코드 수정 |
| ✅ | DISPLAY | TypeError: Failed to execute 'observe' on 'IntersectionObser | fixable | fixable | observe에 Element 아닌 값 전달 | observe 인자 Element로 검증/수정 |
| ✅ | MOBILE | Unhandled Promise Rejection: TypeError: undefined is not an  | fixable | fixable | API 응답 필드 미검사로 undefined | API 응답 null/필드 체크 추가 |
| ✅ | WEB | RangeError: Maximum call stack size exceeded (useEffect setS | fixable | fixable | useEffect의 setState 무한 루프로 스택 오버플로우 | useEffect의 setState 루프 방지 수정 |
| ✅ | SEARCH | TypeError: Intl.Segmenter is not a constructor | fixable | fixable | 지원하지 않는 브라우저에서 Intl.Segmenter 사용 | Intl.Segmenter 지원 체크 및 폴리필 추가 |
| ❌ | RESUME | SyntaxError: Unexpected token '<', "<!DOCTYPE " is not valid | fixable | needs-human | API가 JSON 대신 HTML 응답 반환 |  |
| ✅ | WEB | ResizeObserver loop completed with undelivered notifications | noise | noise | 브라우저 ResizeObserver 경고(대개 무해) |  |
| ✅ | WEB | Script error. | noise | noise | 교차출처(타사) 스크립트 에러로 추정 |  |
| ✅ | MOBILE | Error: Extension context invalidated. (chrome-extension://gi | noise | noise | 브라우저 확장(외부)에서 발생한 에러 |  |
| ✅ | DISPLAY | TypeError: Load failed (googletagmanager.com/gtag/js) | noise | noise | 외부(gtag) 스크립트 로드 실패 |  |
| ✅ | SEARCH | TypeError: NetworkError when attempting to fetch resource. ( | noise | noise | 사용자 네트워크(오프라인)로 인한 일시적 오류 |  |
| ❌ | WEB | Error: Non-Error promise rejection captured with value: Obje | noise | needs-human | 백엔드에서 객체 미발견(404)으로 보임 |  |
| ✅ | MOBILE | TypeError: Cannot redefine property: googletag | noise | noise | 서드파티 광고 스크립트(global googletag) 충돌 |  |
| ✅ | WEB | API Error: 500 /api/payment/confirm | needs-human | needs-human | 백엔드 5xx 응답: 서버 오류 |  |
| ✅ | RESUME | API Error: 502 Bad Gateway /api/resume/upload | needs-human | needs-human | API 502 백엔드 오류 — 확인 필요 |  |
| ✅ | SEARCH | Content Security Policy violation: script-src 'unsafe-eval'  | needs-human | needs-human | CSP 위반: 스크립트 eval이 차단됨 |  |
| ✅ | DISPLAY | API Error: 401 /api/auth/refresh (token refresh failing for  | needs-human | needs-human | 리프레시 토큰 API가 모든 사용자 401 |  |
| ✅ | MOBILE | API Error: 429 Too Many Requests /api/search/suggest | needs-human | needs-human | API 429은 레이트리밋 원인으로 인프라/정책 확인 필요 |  |
| ✅ | WEB | Error: Personal data (resident registration number) found in | needs-human | needs-human | 개인정보(주민등록번호) 클라이언트 로그 적발 |  |
