# TripMate

> **Plan less. Travel better. Adapt as you go.**
> 실시간 상황 · 컨디션 기반 AI 여행 일정 운영 챗봇

여행자의 조건과 실시간 외부 상황(날씨 · 영업시간 · 이동시간)을 종합해 일정을 만들고
**여행이 끝날 때까지 옆에서 상황을 보고 계속 일정을 관리해주는 AI 챗봇**입니다.

| 항목 | 내용 |
| --- | --- |
| 팀 | 성관현 · 이성우 · 박동준 · 백승준 |
| 기간 | 2026.09.07 – 09.09 (3일) |
| 형태 | 백엔드 / 프론트엔드 2-리포지토리 구성 (REST + SSE 통신) |

---

## 1. 기획 배경과 문제 정의

대부분의 여행 계획 서비스는 **"일정을 생성"하는 데서 끝납니다.**
그런데 여행에서 실제로 어려운 일은 계획을 세우는 것이 아니라 **계획대로 되지 않을 때 계획을 고치는 것**입니다.

**계획 단계의 문제**

- 입력 부담이 크다 — 빈 채팅창 앞에서 무엇을 말해야 할지 모른다
- 생성된 일정이 실현 가능하지 않다 — 이동시간 · 영업시간을 고려하지 않은 일정은 지킬 수 없다
- 같은 3명이어도 여행의 성격이 다르다 — 서비스는 '3명'이라는 숫자만 받는다

**여행 중의 문제**

- 날씨가 바뀌어도 일정은 그대로다 — 시스템이 상황 변화 자체를 알지 못한다
- 한 곳이 취소되면 이후 전체가 꼬인다
- 이미 예약한 항공편 · 저녁 예약까지 재생성에 휩쓸린다

**원인**

| 원인 | 설명 |
| --- | --- |
| 일정을 결과물로 다룬다 | 한 번 생성하고 끝나는 산출물로 설계하면 상황 변화 시 할 수 있는 일은 재생성뿐 |
| 변경 단위가 '전체'뿐이다 | 부분 수정 개념이 없으면 사용자가 확정한 것까지 사라진다 |
| 실행 가능성 검증 단계가 없다 | 이동시간 · 영업시간 고려 없이 일정이 세워진다 |
| 개인화 신호가 다음 일정에 닿지 않는다 | 피드백을 저장만 하고 생성 프롬프트로 되돌리지 않는다 |

**Reframe** — 일정은 *한 번 만들고 끝나는 결과물(Output)* 이 아니라, **여행이 끝날 때까지 계속 관리하는 대상**이다.

---

## 2. 타겟 사용자와 사용 시나리오

### 페르소나

| | Persona A — 즉흥형 여행자 | Persona B — 계획형 여행자 | Persona C — 운영 관리자 |
| --- | --- | --- | --- |
| 유형 | 20~40대, 웹·모바일에 익숙 | 가고 싶은 곳이 많고 사전 검증 중시 | 서비스 운영 담당자 (admin) |
| 상황 | 큰 틀만 정하고 현지에서 결정 | 여행 전 일정을 상세히 구성 | 일일 운영 점검 |
| Pain Point | 상황이 바뀌면 일정을 다시 짜야 함 | 만든 일정이 실제로 가능한지 알 수 없음 | 사용량 · LLM 비용 · 오류를 볼 곳이 없음 |
| Needs | "큰 틀만 정해주고 오늘 상황에 맞춰 조정해줘" | "원하는 곳을 최대한 넣되 가능한지 검증해줘" | "누가 얼마나 쓰고 어디서 실패하는지 한 화면에서" |

### 동행 유형 — 인원을 숫자가 아니라 '유형'으로 받는다

| 유형 | 코드 | 기본 인원 | 일정 생성 보정 | 기본 페이스 |
| --- | --- | --- | --- | --- |
| 혼자 | `solo` | 1 | 보정 없음 | 60 |
| 커플 | `couple` | 2 | 야경 · 카페 · 전망 가중 | 55 |
| 친구 | `friends` | 3 | 액티비티 · 맛집 가중 | 65 |
| 가족(아이) | `family_child` | 3 | 이동 1구간 40분 상한 · 하루 4곳 · 실내 대안 확보 | 45 |
| 가족(어르신) | `family_senior` | 3 | 하루 3곳 · 일 이동 120분 상한 · 휴식 슬롯 삽입 | 35 |

> 모든 보정값은 제안일 뿐 강제가 아니며, **사용자가 바꾼 값이 항상 우선**합니다.

### 핵심 시나리오

| # | 시나리오 | 흐름 |
| --- | --- | --- |
| S1 | 조건 입력 | 여행지 · 기간 · 인원(동행 유형) · 강도를 고르고 [여행 만들기] → 여행 생성 + 조건 저장 + 일정 생성이 한 번에 실행 |
| S2 | 악천후 부분 재조정 | 강수확률 60% 이상 감지 → 실내 대안 절충안 2~3개 제시, **18:00 확정 저녁은 어느 안에서도 유지** |
| S3 | 컨디션 기반 강도 축소 | "오늘 너무 피곤해서 오후 일정 하나 빼줘" → locked 항목을 불변 제약으로 최소 변경 재제안 + [되돌리기] |
| S4 | 순서 변경 · 시간 재배치 | 항목 이동 시 재배치 3안(앞으로 당겨 잇기 / 시간 맞바꾸기 / 시작 시각 지정) 중 선택 |
| S5 | 페이스 학습 | 학습값이 설정값과 벌어지면 자동 반영이 아니라 **[이 값으로 맞추기] 제안**으로만 노출 |
| S6 | 외부 API 실패 | 장소 조회 실패 시 생성 중단 및 오류 안내 (아래 [알려진 제한사항](#12-알려진-제한사항) 참고) |

---

## 3. 프로젝트 개요

### 목표

| 구분 | 목표 | 판단 근거 |
| --- | --- | --- |
| G1 | 조건 입력의 부담을 없앤다 | 필수 4칸(여행지 · 일정 · 인원 · 목적)만으로 생성이 시작된다 |
| G2 | 실행 가능한 일정을 만든다 | 항목이 영업시간 안에 배치되고 이동시간이 명시된다 |
| G3 | 변경이 필요한 부분만 고친다 | 수정 요청 시 요청 범위 밖 항목이 바뀌지 않는다 |
| G4 | 사용자가 확정한 일정을 보호한다 | `locked` 항목은 모든 자동 처리에서 불변 |
| G5 | 여행 강도를 하나의 눈금으로 통일한다 | 설정 · 표시 · 피드백 모두 0~100 페이스 |
| G6 | 변경 이유를 설명한다 | 절충안 · 변경 요약(diff) 각 줄에 사유가 붙는다 |
| G7 | 페이스를 학습해 다음 일정에 반영한다 | 단, 시스템이 사용자 설정을 몰래 덮어쓰지 않는다 |
| G8 | 운영자가 상태와 AI 비용을 한 화면에서 본다 | 관리자 대시보드 + 추이 차트 + 에러 목록 |

### 핵심 정책 (절대 규칙)

| # | 규칙 |
| --- | --- |
| R1 | 확정(`locked`) 항목의 장소 · 시각은 자동으로 바뀌지 않는다 |
| R2 | 전체 재생성보다 최소 수정을 우선한다 |
| R3 | 일정 버전은 불변 — 변경은 항상 새 버전을 만들고 이전 버전은 남는다 |
| R4 | 학습값은 사용자 설정을 자동으로 덮어쓰지 않는다 |
| R5 | 별점은 페이스 강도를 직접 바꾸지 않는다 (강도 보정은 태그가 담당) |
| R6 | 충돌이 있어도 적용을 막지 않는다 |
| R7 | 추천에는 반드시 이유가 있어야 한다 |
| R8 | Mate · 페이스를 바꿔도 이미 만들어진 일정은 재생성하지 않는다 |
| R9 | 같은 동작을 두 곳에 두지 않는다 (상세 모달은 읽는 곳, 일정 목록은 고치는 곳) |

### 일정 항목 상태

| 상태 | 값 | 의미 |
| --- | --- | --- |
| 확정 | `locked = true` | 사용자가 잠근 항목. 모든 자동 처리에서 보존 |
| 수정 가능 | `locked = false` | 기본 상태 |
| 정보 미확인 | `dataStatus = unavailable` | 외부 데이터 조회에 실패한 항목 |

### 진행 과정

`문제 · 사용자 정의` → `기획 문서 작성(PRD · 요구사항 · 기능명세 · 흐름도 · 화면설계)` → `역할 분담 · 병렬 구현` → `통합 · 검증`

### 성공 지표

| 지표 | 목표 |
| --- | --- |
| 조건 확정 도달률 | 80% 이상 |
| 일정 생성 완료율 | 90% 이상 |
| 변경 제안 수락률 | 40% 이상 |
| 부분 수정 비율 | 70% 이상 |
| 일정 확정(Lock) 사용률 | 50% 이상 |
| 피드백 입력률 | 20% 이상 |

---

## 4. 통합 아키텍처

```
[브라우저]
  │  Streamlit UI (8501)
  ▼
2026_aio2_TripMate_Frontend            httpx + Bearer 토큰
  │
  │  REST / SSE  (기본 http://127.0.0.1:8000)
  ▼
2026_aio2_TripMate_Backend             FastAPI (uvicorn, 8000)
  │
  ├── Supabase    : Auth(JWT) + PostgreSQL(RLS) — 원본 데이터
  ├── Gemini      : 여행 챗 응답 / 일정 초안 생성 / 일정표 이미지
  ├── Google Maps : Places(New) 검색, Routes 경로, Static Map
  ├── Open-Meteo  : 날짜별 예보 (API 키 불필요)
  └── Redis       : 선택적 캐시 (미설정 시 캐시 없이 동작)
```

**설계 원칙**

- **키 격리** — 브라우저에는 Google Maps 브라우저용 키만 내려간다. Supabase 서비스 롤 키 · Gemini 키는 백엔드 `.env` 전용
- **권한은 DB가 판정** — 여행 소유권은 사용자 JWT + Supabase RLS(`deps.require_own_trip`), 관리자 여부는 `public.profiles.is_admin` 컬럼으로만 판정
- **라우터–서비스 분리** — HTTP 처리(`app/routers/`) · 업무 규칙(`app/services/`) · 외부 API 호출(`*_client.py`)을 계층으로 분리

### 데이터베이스 (Supabase PostgreSQL)

| 테이블 | 역할 |
| --- | --- |
| `profiles` | 사용자 프로필, `is_admin` 관리자 플래그 |
| `trips` | 여행(기간 · 목적지 · 동행 · 강도 · 경비 · 숙소 · 고정 순서) |
| `trip_days` | 여행별 DAY 1..N |
| `itinerary_items` | DAY별 일정 항목, `place_id`로 캐시된 장소 참조 |
| `places` | Google Place 정보 공용 캐시 |
| `messages` | 여행 1개당 채팅 이력 1벌 |
| `itinerary_change_logs` | 일정 변경 전/후 스냅샷 (되돌리기용) |
| `activity_logs` | 사용자 행동 로그 |
| `api_request_logs` | API 요청 · 지연 · 오류 로그 (대시보드 지표 원천) |

### 저장소 간 계약

| 항목 | 내용 |
| --- | --- |
| 통신 | REST(JSON) + 채팅은 SSE 스트리밍 |
| 인증 | Supabase JWT를 `Authorization: Bearer`로 전달 |
| 명세 | 루트 `API스펙.md` + FastAPI 자동 생성 `/docs` |
| CORS | 백엔드가 `localhost:8501`, `127.0.0.1:8501`만 허용 |
| 검증값 동기화 | 필수 방문지 최대 5개 — 프론트 `MAX_MUST_VISIT`, 백엔드 `TripCreate.must_visit(max_length=5)` |
| 비밀 관리 | 양쪽 모두 `.env` gitignore. 서비스 롤 · Gemini 키는 백엔드 전용 |

---

## 5. 실시간 로그 모니터링

운영 지표를 나중에 붙이지 않고 **개발 단계부터 미들웨어로 내재화**했습니다.

### 수집

- `ApiRequestLoggingMiddleware`(ASGI)가 **모든 요청의 응답 전송이 끝난 뒤** `api_request_logs`에 적재
- 기록 항목: `request_id` · method · endpoint · status_code · latency_ms · error_type · user_id · trip_id · model
- `x-request-id` 헤더를 존중하고 없으면 새로 발급 — 프론트/백엔드 로그를 한 요청 단위로 추적
- `/docs`, `/redoc`, `/openapi.json`은 운영 지표에서 제외
- 사용자 행동은 `activity_logs`(`activity_logging.py`)에 별도 기록
- **API 키나 요청 · 응답 본문 전체는 기록하지 않음**

### 조회 (관리자 전용, `profiles.is_admin`)

| 엔드포인트 | 내용 |
| --- | --- |
| `GET /admin/dashboard/summary` | KPI(가입 수 · 총 요청 · 성공/실패 · 에러율 · 평균 지연) + 시간대별 추이 + 엔드포인트별 사용량 + LLM 요약 |
| `GET /admin/dashboard/endpoints` | 엔드포인트별 요청 수 · 고유 사용자 수 · 평균 지연 · 에러율 |
| `GET /admin/dashboard/errors` | 실패 요청 목록 (발생 시각 · request_id · 상태 코드 · 오류 유형) |
| `GET /console/users`, `/console/users/{id}` | 사용자 목록 / 사용자별 여행 · 활동 · API 요청 (조회 전용) |
| `GET /console/feedback`, `/console/system-status` | 피드백 요약, 시스템 상태 |

LLM 요약은 모델별 요청 수 · 실패 수 · 평균 지연에 더해 **timeout · Gemini 오류 · 빈 응답 건수**를 따로 집계해,
비용 추이와 실패 지점을 같은 화면에서 볼 수 있게 했습니다.
관리자 화면은 별도 호스트가 아니라 **같은 로그인 세션 안에서 사이드바로 전환**합니다.

---

## 6. 핵심 기능

**MVP 17개 기능 · 4개 영역**

| 영역 | 기능 |
| --- | --- |
| 대화 · 조건 (4) | 멀티턴 대화(SSE 스트리밍) · 여행 조건 파악 · 조건 유지 대화 · 응답 톤 설정(Mate 3종) |
| 일정 생성 · 수정 (5) | 일정 생성 · 일정 강도 표시 · 최소 수정 재제안 · 확정/수정가능 구분 · 일정 항목 직접 수정 |
| 데이터 · 시각화 (4) | 외부 API(날씨 · 영업 · 평점) · 일정 화면/타임라인 · 동선 지도 + 교통정보 · 일정표 이미지 다운로드 |
| 계정 · 운영 (4) | 인증(가입 · 로그인 · 찾기) · 여행 목록 관리 · 로그 관리 · 운영 대시보드 |

### FEATURE 01 — 실행 가능한 일정

외부 데이터를 **참고 정보가 아니라 생성 · 검토의 제약 조건**으로 사용합니다.

| 축 | 소스 | 사용 방식 |
| --- | --- | --- |
| 장소 | Google Places (New) | 영업시간 · 휴무 · 평점 → 문 닫은 곳은 일정에 넣지 않음 |
| 날씨 | Open-Meteo | 시간대별 강수확률 → 야외 일정 위험을 먼저 감지 |
| 이동 | Google Routes | 구간 이동시간 반영 → 하루에 소화 가능한 동선인지 검토 |

> *"내일 오후에 비가 온다는데, 저녁 예약은 그대로 두고 야외 일정만 바꿔줘"*
> AI에게 모든 결정을 다시 맡기지 않고, **사용자가 결정한 것은 지키고 필요한 부분만 수정**합니다.

### FEATURE 02 — 페이스 · 동행자 반영

동행 유형 7종(혼자 · 커플 · 친구 · 가족 · 가족(아이 동반) · 가족(부모님 동반) · 시니어 부부)을 고르면
기본 페이스 · 하루 방문 수 · 이동 상한 · 휴식 슬롯이 함께 조정됩니다.
**설정 · 표시 · 피드백이 모두 같은 0~100 눈금**을 씁니다.

### FEATURE 03 — Mate 3종 (같은 일정, 다른 설명)

| Mate | 코드 | 문체 | 예시 |
| --- | --- | --- | --- |
| 비서 | `assistant` | 결정에 필요한 정보만, 3문장 이내 | "10:00 츠키지 · 11:00 하마리큐 · 이동 25분" |
| 가이드 (기본) | `guide` | 이유와 현지 팁, 구어체 5~7문장 | "츠키지는 아침 일찍이 가장 활기차요. 문 여는 시간에 맞춰 넣었어요." |
| 어르신 | `senior` | 쉬운 말 · 짧은 문장 · 존대 | "아침에 시장 구경하시고, 점심 드신 뒤엔 정원에서 쉬시면 좋겠어요." |

### FEATURE 04 — 순서 변경 · 시간 재배치 3안

| 방식 | 코드 | 동작 |
| --- | --- | --- |
| 앞으로 당겨 이어 붙이기 (기본) | `shift_up` | 그날 첫 항목 시각을 앵커로 고정하고 새 순서대로 체류 + 이동을 쌓아 채운다 |
| 시간만 맞바꾸기 | `swap_time` | 두 항목의 시각을 교환하고 나머지는 불변 |
| 시작 시각 직접 지정 | `anchor` | 옮긴 항목만 지정 시각에 두고 앞뒤를 자동 보정 |

### FEATURE 05 — 운영자를 위한 한 화면

AI 비용 추이 · 일정 생성 성공률 · 시스템 건강도 · API 에러 로그를 한 화면에서 확인합니다.

---

## 7. 팀원별 담당 기능

| 팀원 | 역할 | 담당 |
| --- | --- | --- |
| 이성우 | 팀장 · 기획 · UI/UX | PRD · 요구사항 정의서 · 기능명세서 · 사용자 흐름도 · 화면설계서, Streamlit 화면 구성 |
| 박동준 | 백엔드 · LLM · 일정 | Gemini 연동, 일정 초안 생성 · 검증 · 정규화, 최소 변경 재제안, 일정 항목 CRUD |
| 성관현 | 백엔드 · 인증 · 대시보드 | Supabase Auth · RLS · 권한, 운영 대시보드 / 운영콘솔 API, 일정표 내보내기 |
| 백승준 | 데이터 · 외부 API · 로그 | Google Places · Routes · Open-Meteo 연동, 장소 캐시, 요청 · 활동 로그 적재 |

---

## 8. 기술 스택

| 구분 | 사용 기술 |
| --- | --- |
| 프론트엔드 | Python 3.11+, Streamlit 1.63+, httpx, Maps JavaScript API |
| 백엔드 | Python 3.11+, FastAPI 0.139+, uvicorn, Pydantic v2 |
| DB · 인증 | Supabase (PostgreSQL + RLS, Auth JWT) |
| LLM | Google Gemini (`google-genai`) — 챗 응답 · 일정 초안 · 일정표 이미지 |
| 외부 API | Google Places (New) · Google Routes · Open-Meteo |
| 캐시 | Redis (선택 — 미설정 시 캐시 없이 동작) |
| 패키지 관리 | uv |
| 테스트 | 백엔드 92개 · 프론트 11개 |
| 협업 | Figma · Notion · Google Sheets · GitHub |

**저장소 구성**

```
첫 프로젝트/                              (로컬 작업 루트, 저장소 아님)
├── 2026_aio2_TripMate_Backend/          ← 저장소 1 : FastAPI API 서버
├── 2026_aio2_TripMate_Frontend/         ← 저장소 2 : Streamlit 화면
├── API스펙.md                            공통 API 명세 (양쪽 계약서)
├── TripMate_ERD_논리물리.md               ERD 문서
└── TripMate_시스템구성도_simple.svg        시스템 구성도
```

두 저장소는 코드를 공유하지 않고 **HTTP REST API로만** 통신하며, 모든 외부 서비스 키는 백엔드에만 존재합니다.

---

## 9. 필수 산출물 링크

| 산출물 | 링크 |
| --- | --- |
| 사용자 흐름도 | [Figma](https://www.figma.com/design/R7NAZN36pbp1pEnHmnG4pj/Tripmate?node-id=1235-7142) |
| 화면설계서 | [Figma](https://www.figma.com/design/R7NAZN36pbp1pEnHmnG4pj/Tripmate?node-id=1235-7140) |
| 화면 설계 (상세) | [1212-7056](https://www.figma.com/design/R7NAZN36pbp1pEnHmnG4pj/Tripmate?node-id=1212-7056) · [1349-28318](https://www.figma.com/design/R7NAZN36pbp1pEnHmnG4pj/Tripmate?node-id=1349-28318) · [1312-26888](https://www.figma.com/design/R7NAZN36pbp1pEnHmnG4pj/Tripmate?node-id=1312-26888) · [1295-16200](https://www.figma.com/design/R7NAZN36pbp1pEnHmnG4pj/Tripmate?node-id=1295-16200) |
| 요구사항 정의서 | [Google Sheets](https://docs.google.com/spreadsheets/d/1rcpv-IAffgWSR5yVkbKSypKlu-oU2v0e7apmufv_AEI/edit) |
| 기능명세서 | [Google Sheets](https://docs.google.com/spreadsheets/d/1yJjLRHlRioatx11Ktwu2CZycpyjKQvFmy8oyNvg1W4U/edit) |
| 참고 시트 | [1B0dYIwP…](https://docs.google.com/spreadsheets/d/1B0dYIwPYMURq8zYvUPELZq_ixeXBgppA165jBZpVXU8/edit) · [1QndHyOj…](https://docs.google.com/spreadsheets/d/1QndHyOjKVeIVhQt6HN8IUX7Otw_2WPaJ73nSMLaTVUU/edit) |
| 데이터 명세서 | [Notion](https://app.notion.com/p/yleeylee/88a2b0d658a6827aa9b9015e7c315566) |
| 리스크 관리 | [Notion](https://app.notion.com/p/yleeylee/TripMate-50e2b0d658a68239a78a81f3c8a07576) |
| 프로젝트 문서 | [Notion](https://app.notion.com/p/yleeylee/8322b0d658a6839cac2a013b7c6b32a5) |
| PRD | `TripMate_prd.pdf` |
| API 스펙 · 설계서 | `API스펙.md` · `API설계서.md` · FastAPI `/docs` |
| ERD | `TripMate_ERD_논리물리.md` · `TripMate_ERD.pdf` |
| 저장소 구조 | `개발코드_저장소_구조.md` |
| 발표 자료 | `TripMate_발표T.pptx` |

---

## 10. 시연 순서

| # | 단계 | 내용 |
| --- | --- | --- |
| 01 | 여행 조건 입력 | 여행지 · 기간 · 동행 유형 · 강도를 선택해 일정 생성 시작 |
| 02 | 생성된 일정 확인 | DAY 탭 일정표 · 동선 지도 · 페이스 강도 확인 |
| 03 | 상황 변화 대응 | 확정(Lock) 일정은 유지한 채 영향받는 일정만 재제안 → 변경 요약 + 되돌리기 |
| 04 | Mate · 페이스 변경 | 응답 톤 3종 전환, 페이스 눈금 조정 |
| 05 | 일정표 내보내기 | 일정표 이미지 다운로드 |
| 06 | 운영 대시보드 | 같은 세션에서 사이드바 전환 → KPI · 추이 · 엔드포인트 · 에러 로그 |

---

## 11. 로컬 실행 방법

### 사전 준비

1. Supabase 프로젝트 생성 후 `2026_aio2_TripMate_Backend/supabase/`의 SQL을 **파일명 순서대로** SQL Editor에서 실행
   (`20260904_google_places_cache` → `20260906_trip_preferences` → `20260907_trip_accommodation`
   → `20260907_itinerary_change_logs` → `20260907_dashboard_logs_merged` → `20260908_profile_admin`)
2. 관리자 계정은 `public.profiles.is_admin = true`로 설정 (관리자 토큰 · 환경변수는 사용하지 않음)

### 백엔드 (포트 8000)

```powershell
cd 2026_aio2_TripMate_Backend
uv sync
uv run uvicorn app.main:app --reload
# http://127.0.0.1:8000/docs
```

`.env` (gitignore, 백엔드 전용)

```dotenv
SUPABASE_URL=""
SUPABASE_ANON_KEY=""
SUPABASE_SERVICE_ROLE_KEY=""   # 비밀번호 재설정 · 로그 적재용
GEMINI_API_KEY=""              # AI 챗 · 일정 생성
GOOGLE_MAPS_API_KEY=""         # Places(New) · Routes
# Redis 값은 비워두면 캐시 없이 동작
```

### 프론트엔드 (포트 8501)

```powershell
cd 2026_aio2_TripMate_Frontend
uv sync
uv run streamlit run streamlit_app.py
```

`.env` (gitignore, 프론트 전용)

```dotenv
BACKEND_URL=""                 # 비우면 http://127.0.0.1:8000
GOOGLE_MAPS_API_KEY=""         # 브라우저용 키만 (서비스 롤 · Gemini 키 금지)
```

### 테스트

```powershell
# 백엔드 — 외부 서비스 없이 실행
cd 2026_aio2_TripMate_Backend
uv run python -m unittest discover -s tests

# 프론트엔드
cd 2026_aio2_TripMate_Frontend
uv run python -m pytest tests
```

---

## 12. 알려진 제한사항

### 구현 상태와 기획의 차이

- **외부 API 실패 시 일정 생성이 중단됩니다.** PRD의 `dataStatus: "unavailable"` + "정보 미확인" 배지는 미구현이며,
  현재는 502 오류("Google Places에서 AI 일정 장소를 찾지 못했습니다")로 생성이 멈추고 여행이 만들어지지 않습니다.
- **일정 생성에 실패하면 여행 자체가 생성되지 않습니다.** (부분 성공 상태 없음)
- 조건 입력 양식에서 **여행 목적 대신 여행 강도 1~5**를 선택합니다.
- 생성 후 전환되는 화면은 대화 화면이 아니라 **DAY 탭 일정표 + 하단 대화가 함께 있는 대시보드**입니다.
- **운영콘솔은 조회 전용**입니다. (사용자 상태 변경 등 쓰기 기능 없음)

### 이번 버전 범위 밖 (의도적 제외)

| 제외 항목 | 사유 |
| --- | --- |
| 예약 연동 (항공 · 숙소 · 식당) | 결제 · 취소 정책 · 파트너 계약이 따르는 별개 제품 영역. "이미 예약된 일정을 보호"까지만 다룸 |
| 일정 스냅샷 (사용자 명명 저장) | 향후 확장 기능으로 관리 |
| 일정 항목 [교체] 버튼 | 버튼 후보 목록은 조건 · 동선 · 영업시간 맥락을 담지 못해 결국 대화로 되돌아옴 |
| 예산 기반 일정 생성 | 입장료 · 식비에 신뢰할 외부 데이터가 없어 추정치. 추정으로 만든 "60만원 일정"은 지킬 수 없는 약속 |
| 동행자 공동 편집 · 일정 공유 | 권한 · 충돌 해결 모델이 별도로 필요 |
| 모바일 전용 UI | 데스크톱 우선, 모바일은 향후 대응 |

### 기술적 제약

- **도시 단위 입력만 지원** — '오사카, 일본'처럼 도시를 입력해야 하며 광역 지역 · 국가 · 복수 도시는 자동 허용하지 않습니다.
  도시별 주소 구조 차이로 검증하지 못하는 경우가 있습니다.
- **출국일은 18시 출국을 가정**해 관광 · 점심을 13시까지 배치하고 이후를 이동 · 수속 예비 시간으로 둡니다.
  공항 · 항공편이 연동되지 않아 출국 안내에는 실제 좌표가 붙지 않으며, 이동 예비 2시간은 도착 보장이 아닙니다.
- 자동 일정 규칙은 **새로 생성되는 일정에만** 적용됩니다. 기존 여행이나 사용자가 직접 추가한 일정은 덮어쓰지 않습니다.
- Redis는 선택 사항이며, 미설정 시 캐시 없이 매번 외부 API를 호출합니다.
- CORS는 `localhost:8501` · `127.0.0.1:8501`만 허용하므로, 다른 호스트에 배포하려면 `app/main.py` 수정이 필요합니다.

---

## 13. 마무리

> 단순히 '여행 일정을 잘 만들어주는 AI'가 아니라,
> **사용자가 결정한 일정은 지키면서, 변화가 생기면 필요한 부분만 함께 고쳐주는 AI 여행 동반자**
