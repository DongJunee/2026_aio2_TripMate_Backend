# TripMate 관리자 대시보드 API 설계서

## 1. 목적

관리자가 지정한 기간의 사용자 가입 현황과 API 운영 상태를 확인할 수 있도록 한다.
프론트엔드는 Supabase에 직접 접근하지 않고 백엔드 API만 호출한다.

대상 지표:

- 기간 필터
- 사용자 가입 수
- 전체 요청 수
- 에러율
- 평균 응답시간
- 성공·실패 수
- 시간별 요청량
- 최근 오류 로그
- LLM 요청 요약

## 2. 데이터 출처

| 지표 | 테이블·컬럼 |
| --- | --- |
| 사용자 가입 수 | `public.profiles.created_at` |
| API 요청 수·성공·실패 | `public.api_request_logs.status_code` |
| 에러율 | `api_request_logs`에서 계산 |
| 평균 응답시간 | `public.api_request_logs.latency_ms` |
| 시간별 요청량 | `public.api_request_logs.created_at` |
| 최근 오류 | `api_request_logs`의 400 이상, 상태 코드 없음, `error_type` 존재 행 |
| LLM 요약 | `api_request_logs.model`이 있는 행 |

요청 기간은 `start_at` 이상, `end_at` 미만으로 계산한다. 시간은 ISO 8601 형식을 사용하며,
화면 표시 기준은 한국 시간(KST)으로 한다.

## 3. 공통 규칙

### 기본 정보

- Base URL: 백엔드 주소
- API Prefix: `/admin/dashboard`
- Method: `GET`
- 응답 형식: `application/json`
- 기간 기본값: 오늘 00:00:00부터 현재 시각까지
- 권장 조회 기간: 최대 31일

### 관리자 인증

프론트엔드는 관리자용 토큰을 `X-Admin-Token` 헤더로 전달한다.

```http
X-Admin-Token: <DASHBOARD_ADMIN_TOKEN>
```

`DASHBOARD_ADMIN_TOKEN`의 실제 값은 프론트엔드에 넣지 않고 백엔드 환경변수에만 둔다.
백엔드는 토큰을 확인한 뒤 service-role Supabase 클라이언트로 로그를 조회한다.

### 기간 파라미터

```text
start_at=2026-09-07T00:00:00+09:00
end_at=2026-09-08T00:00:00+09:00
```

- `start_at`, `end_at` 모두 생략하면 기본 기간을 사용한다.
- `start_at >= end_at`이면 `400 Bad Request`를 반환한다.
- 허용 범위를 초과하는 기간은 `400 Bad Request`를 반환한다.

## 4. API 목록

### 4.1 대시보드 전체 요약

```http
GET /admin/dashboard/summary
```

#### Query parameters

| 이름 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `start_at` | datetime | 아니오 | 조회 시작 시각 |
| `end_at` | datetime | 아니오 | 조회 종료 시각 |

#### Response `200 OK`

```json
{
  "period": {
    "start_at": "2026-09-07T00:00:00+09:00",
    "end_at": "2026-09-08T00:00:00+09:00"
  },
  "kpis": {
    "user_signup_count": 12,
    "total_requests": 240,
    "success_count": 228,
    "failure_count": 12,
    "error_rate_percent": 5.0,
    "average_latency_ms": 348
  },
  "hourly_requests": [
    {
      "hour": "2026-09-07T09:00:00+09:00",
      "request_count": 32,
      "success_count": 30,
      "failure_count": 2
    }
  ],
  "endpoint_usage": [
    {
      "endpoint": "/health",
      "request_count": 32,
      "unique_user_count": 8,
      "success_count": 30,
      "failure_count": 2,
      "average_latency_ms": 120,
      "error_rate_percent": 6.25
    }
  ],
  "llm_summary": [
    {
      "model": "gemini-3.5-flash-lite",
      "request_count": 80,
      "success_count": 76,
      "failure_count": 4,
      "error_rate_percent": 5.0,
      "average_latency_ms": 920
    }
  ]
}
```

### 4.2 엔드포인트별 사용량

```http
GET /admin/dashboard/endpoints
```

기간 파라미터는 요약 API와 동일하게 사용한다. 응답은 엔드포인트별 요청 수,
고유 사용자 수, 성공·실패 수, 평균 응답시간, 에러율을 요청량 내림차순으로 반환한다.

### 4.3 최근 오류 로그

```http
GET /admin/dashboard/errors
```

#### Query parameters

| 이름 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `start_at` | datetime | 아니오 | 조회 시작 시각 |
| `end_at` | datetime | 아니오 | 조회 종료 시각 |
| `limit` | integer | 아니오 | 최대 100, 기본 100 |

#### Response `200 OK`

```json
{
  "items": [
    {
      "occurred_at": "2026-09-07T14:22:11+09:00",
      "request_id": "request-id-value",
      "method": "POST",
      "endpoint": "/trips/{trip_id}/chat",
      "status_code": 500,
      "latency_ms": 1830,
      "error_type": "http_5xx",
      "user_id": "user-uuid",
      "trip_id": "trip-uuid",
      "model": "gemini-3.5-flash-lite"
    }
  ],
  "count": 1
}
```

## 5. 집계 기준

- 성공: HTTP 상태 코드 `200~399`
- 실패: HTTP 상태 코드 `400 이상` 또는 상태 코드 없음
- 에러율: `실패 수 / 전체 요청 수 * 100`
- 평균 응답시간: `latency_ms`가 있는 요청만 평균 계산
- 가입 수: 기간 내 `profiles.created_at` 행 수
- 시간별 차트: KST 기준으로 `created_at`을 1시간 단위로 묶음
- 오류 목록: 실패 조건에 해당하는 최신 로그부터 최대 100건
- LLM 요약: `model`이 기록된 요청만 집계

## 6. 오류 응답

```json
{
  "detail": "관리자 인증이 필요합니다."
}
```

| 상태 코드 | 상황 |
| --- | --- |
| `400` | 기간 형식 오류, 기간 역전, 잘못된 limit |
| `401` | 관리자 토큰 없음 또는 불일치 |
| `500` | 대시보드 집계 또는 Supabase 조회 실패 |

## 7. 프론트엔드 사용 흐름

1. 날짜 선택 위젯에서 `start_at`, `end_at`을 만든다.
2. `GET /admin/dashboard/summary`를 호출한다.
3. `kpis`를 카드 6개에 표시한다.
4. `hourly_requests`를 시간별 차트에 표시한다.
5. `llm_summary`를 LLM 요약 영역에 표시한다.
6. `GET /admin/dashboard/errors`를 호출해 최근 오류 테이블에 표시한다.
7. 날짜를 바꾸면 두 API를 같은 기간으로 다시 호출한다.

프론트엔드는 `SUPABASE_SERVICE_ROLE_KEY`를 사용하지 않는다. 관리자 토큰도 프론트엔드 코드에
하드코딩하지 않고, 운영 환경의 안전한 비밀 설정으로 전달한다.

로컬 테스트 기간에는 백엔드 환경변수 `DASHBOARD_AUTH_DISABLED=true`로 관리자 인증을
임시 해제할 수 있다. 배포나 공유 전에는 반드시 `false`로 바꾸거나 해당 설정을 제거한다.

## 8. 현재 구현 상태

- `api_request_logs` 테이블: Supabase에 생성 완료
- API 요청 자동 기록 미들웨어: 백엔드에 연결 완료
- `/docs`·`/redoc`·`/openapi.json`: 운영 로그에서 제외
- `/docs`의 `Try it out`으로 실행한 실제 API 요청: 로그 기록 대상
- 관리자 대시보드 조회 API: 백엔드 구현 완료(`/summary`, `/endpoints`, `/errors`)
- Streamlit 관리자 대시보드 화면: 별도 `admin_dashboard.py`로 연결 완료

이 문서는 API 조회 규칙을 정의하는 문서이며, 문서 자체를 실행해 테이블을 변경하지 않는다.
