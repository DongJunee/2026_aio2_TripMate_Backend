# TripMate 운영콘솔 API 설계서

## 목적

운영자가 로그인한 사용자의 기본 정보와 여행·활동·API 요청 현황을 조회할 수
있도록 한다. 운영콘솔은 기존 TripMate Streamlit 화면 안에서 열리며 별도 호스트를
사용하지 않는다.

관리자 메뉴는 화면별로 분리한다. `ADM-002 사용자 관리`는 사용자 목록과 상세
패널만 표시하고, `ADM-003 피드백·페이스`는 집계 결과만 표시하며,
`ADM-004 시스템 상태`는 최근 1시간 상태만 표시한다.

## 인증

- API prefix: `/admin/console`
- method: `GET`
- 현재 로그인 세션의 Bearer 토큰을 사용한다.
- 백엔드의 관리자 권한 정책은 대시보드와 공유한다.
- `public.profiles.is_admin = true`인 로그인 사용자만 접근할 수 있다.

## 사용자 목록

```http
GET /admin/console/users?search=박동준&limit=50&offset=0
```

| 필드 | 설명 |
| --- | --- |
| `search` | 사용자명 또는 이메일 검색어 |
| `limit` | 한 번에 조회할 사용자 수, 최대 100 |
| `offset` | 목록 시작 위치 |

목록 항목에는 사용자명, 이메일, 가입일, 최근 활동일, 여행 수, API 요청 수,
활동 로그 수를 포함한다.

## 사용자 상세

```http
GET /admin/console/users/{user_id}
```

사용자 기본 정보와 함께 다음 내용을 반환한다.

- 해당 사용자의 여행 목록
- 최근 API 요청 로그 50건
- 최근 활동 로그 50건

## 피드백·페이스 집계

```http
GET /admin/console/feedback
```

ADM-003 화면 전용 조회 API다. 원문이나 개인 식별 정보는 반환하지 않고,
`activity_logs`에서 피드백·페이스 관련 이벤트를 분류해 건수만 반환한다.

- `feedback_count`: 전체 피드백 수
- `positive_count`: 긍정 피드백 수
- `negative_count`: 부정 피드백 수
- `pace_count`: 페이스 기록 수
- `feedback_breakdown`: 피드백 유형별 집계
- `pace_breakdown`: 페이스 유형별 집계

## 시스템 상태

```http
GET /admin/console/system-status
```

ADM-004 화면 전용 조회 API다. 최근 1시간의 `api_request_logs`를 기준으로
전체 요청 수, 실패 수, 에러율, 서비스별 요청 수·실패율·P95 응답시간을 집계한다.
서비스 분류는 LLM 응답, 장소·영업시간 API, 지도 SDK, 일정표 렌더 서버로 나눈다.

## 데이터 원천

- Supabase Auth: 이메일, 가입일, 최근 로그인일
- `public.profiles`: 사용자명
- `public.trips`: 여행 목록
- `public.api_request_logs`: API 이용 현황
- `public.activity_logs`: 사용자 행동 현황

시스템 상태는 최근 1시간의 `public.api_request_logs`만 사용하며, 대시보드·운영콘솔
조회 자체가 서비스 상태 통계를 오염시키지 않도록 관리자 조회 요청은 제외한다.

`activity_logs`가 아직 배포되지 않은 환경에서는 해당 목록을 빈 배열로 반환해
운영콘솔의 나머지 조회 기능이 중단되지 않도록 한다.

## 범위

현재 버전은 조회 전용이다. 사용자 삭제, 여행 수정, 권한 변경과 같은 데이터 변경
기능은 운영 정책과 별도 승인 후 추가한다.
