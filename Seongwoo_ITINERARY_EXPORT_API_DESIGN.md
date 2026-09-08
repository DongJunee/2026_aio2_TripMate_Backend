# 일정표 이미지 다운로드 — 수정 범위와 적용 방법 (SCR-007)

저장된 일정을 프롬프트와 함께 LLM 에 보내고 이미지를 받아 내려받게 하는 기능이다.
**심플형과 일러스트형 모두 같은 코드 경로**를 지나고, 바뀌는 것은 프롬프트뿐이다.

> **적용 상태: 완료 (2026-09-08).** §1~§11 의 계획은 양쪽 저장소에 모두 반영됐다.
> 적용 과정에서 붙여넣기 손상 5건이 발생해 서버·화면이 뜨지 않았고, 전부 고쳤다.
> **무엇이 실제로 들어갔고 무엇이 남았는지는 §12 를 먼저 볼 것.**

- **1~6장** — 어디를 어떻게 고치는지 (범위 · 설명 · 삽입 위치)
- **부록** — 어느 설명이 어느 코드를 말하는지 찾아가는 위치 지도
- **12장** — 적용 결과 · 붙여넣기 손상 수정 이력 · 검증 결과 · 남은 항목

```
GET /trips/{trip_id}/itinerary/export?style=simple|illustrated
        │
        ├─ trip_dashboard()  → trip + days + items  (소유권 확인 겸용)
        ├─ EXPORT_IMAGE_PROMPTS[style].format(title=, period=, party=, plan=)
        ├─ Gemini 이미지 모델 호출 (그림 없이 오면 1회 재시도)
        └─ PNG bytes → Content-Disposition: attachment
```

---

## 1. 수정 범위 한눈에 보기

| 파일                                        | 작업           | 어디를                           | 규모            | 적용 결과 (2026-09-08)      |
| ------------------------------------------- | -------------- | -------------------------------- | --------------- | --------------------------- |
| `app/services/itinerary_export_prompt.py` | **신규** | 파일 전체                        | 951줄 | ✅ 251줄 — 파일명 수정 §12-3 |
| `app/services/itinerary_export.py`        | **신규** | 파일 전체                        | 390줄 | ✅ 295줄                     |
| `app/routers/trips.py`                    | **수정** | ① 상단 import 3줄 ② 파일 맨 끝 | +78줄           | ✅ 1,717줄 — 들여쓰기 §12-2  |
| `.env`                                    | **수정** | `GEMINI_MODEL` 아래            | +4줄            | ⬜ 미적용 (선택 — §12-5)     |

**프론트엔드** `2026_aio2_TripMate_Frontend` — §11

| 파일                 | 작업           | 어디를                                                         | 규모  | 적용 결과 (2026-09-08)     |
| -------------------- | -------------- | -------------------------------------------------------------- | ----- | -------------------------- |
| `common.py`        | **수정** | `api_bytes()` 교체                                           | +14줄 | ✅ 195줄                    |
| `streamlit_app.py` | **수정** | ① import ② 세션 기본값 ③ 모달 함수 ④`render_dashboard()` | +95줄 | ✅ 4,541줄 — 4곳 손상 §12-2 |

~~**프론트에는 지금 다운로드 버튼이 없다.** 백엔드만 만들면 누를 곳이 없다 (§11-1).~~
→ **적용 완료.** `render_dashboard()` 안 DAY 탭 아래에 `[일정표 다운로드]` 버튼과
`@st.dialog` 모달(`render_export_dialog`)이 들어갔다 (§12-1).

**기존 코드를 고치는 곳은 한 군데도 없다.** `trips.py` 도 import 3줄 추가와 파일
끝 블록 추가뿐이고, 기존 함수·엔드포인트는 그대로 둔다.

### 손대지 않아도 되는 것 (대조 확인 완료 — §8)

| 파일                       | 확인 결과                                                                       |
| -------------------------- | ------------------------------------------------------------------------------- |
| `app/cache.py`           | `cache_get(key)` / `cache_set(key, value, ttl)` 시그니처가 그대로 맞는다    |
| `app/gemini_client.py`   | 새 서비스가`genai.Client` 를 자체 생성한다                                    |
| `app/main.py`            | `trips.router` 가 이미 등록돼 있다                                            |
| `pyproject.toml`         | `google-genai` 가 이미 있다 — **새 의존성 없음**                       |
| `supabase/*.sql`         | `activity_logs.event_type` 에 값 CHECK 가 없다 — **마이그레이션 없음** |
| `app/request_logging.py` | 응답 body 를 읽지 않아 PNG 응답에 안전하다                                      |
| `app/schemas.py`         | GET + 쿼리 파라미터라 요청 모델이 필요 없다                                     |

---

## 2. 작업 순서

**이 순서대로 해야 중간에 import 에러가 나지 않는다.** 서비스 파일이 없는 상태에서
`trips.py` 부터 고치면 서버가 뜨지 않는다.

| 순서 | 할 일                                             | 검증                                                                                                                     |
| ---- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| 1    | `.env` 에 `GEMINI_IMAGE_MODEL` 추가 (§6)     | —                                                                                                                       |
| 2    | `itinerary_export_prompt.py` 생성 (§3) | `python -c "from app.services.itinerary_export_prompt import EXPORT_IMAGE_PROMPTS; print(list(EXPORT_IMAGE_PROMPTS))"` |
| 3    | `itinerary_export.py` 생성 (§4)                | `python -c "from app.services import itinerary_export"`                                                                |
| 4    | `trips.py` 수정 (§5)                           | 서버 기동 +`/docs` 에 엔드포인트 2개 노출                                                                              |
| 5    | 실제 호출 (§10)                                  | PNG 다운로드                                                                                                             |
| 6    | 프론트`common.py` 수정 (§11-3)                 | —                                                                                                                       |
| 7    | 프론트`streamlit_app.py` 수정 (§11-4)          | 화면에서 다운로드 (§11-6)                                                                                               |

## 3. [신규] `app/services/itinerary_export_prompt.py`

### 3-1. 이 파일이 하는 일

프롬프트 문자열만 담는다. **로직을 넣지 않는다.** 프롬프트가 서비스 코드와 같은
파일에 있으면, 문구 한 줄 고치는 일과 호출 로직 고치는 일이 같은 파일에서 섞인다.

### 3-2. 담을 것

| 상수                           | 내용                                         | 비고                              |
| ------------------------------ | -------------------------------------------- | --------------------------------- |
| `_SHARED_TAIL`               | `[Data]` + `[Output]`                    | **두 스타일이 공유**        |
| `_SIMPLE_BODY`               | 심플형의 Role/Task/Layout/Visual/Constraints | 신규 작성                         |
| `_ILLUSTRATED_BODY`          | 일러스트형의 같은 블록들                     | 원본 이식 + 1곳 수정              |
| `EXPORT_IMAGE_PROMPTS`       | `{"simple": …, "illustrated": …}`        | 몸통 + 공통 꼬리                  |
| `EXPORT_IMAGE_STYLES`        | `("simple", "illustrated")`                | 라우터·서비스가 같은 목록을 본다 |
| `DEFAULT_EXPORT_IMAGE_STYLE` | `"simple"`                                 | 시안의 [기본 선택]                |

```
EXPORT_IMAGE_PROMPTS = {
    "simple":      _SIMPLE_BODY      + _SHARED_TAIL
    "illustrated": _ILLUSTRATED_BODY + _SHARED_TAIL
}
                     └ 스타일마다 다름  └ 두 스타일 공유
                       Role/Task/Layout   [Data] · [Output]
                       Visual/Constraints (데이터 계약 · 글자 규칙)
```

### 3-3. 왜 꼬리를 공유하는가

`[Data]` 와 `[Output]` 은 코드가 채우는 `{title} {period} {party} {plan}` 자리이자
**프롬프트 인젝션 경계**다. 장소명은 사용자와 모델이 만든 문자열이라, 지시문 자리에
그대로 두면 그 안의 문장이 지시로 읽힐 수 있다.

스타일마다 따로 적어 두면 한쪽만 고치고 다른 쪽을 잊는다. 그러면 그 스타일만 규칙
없이 나가고, 그 사실은 이상한 그림을 받고 나서야 드러난다.

### 3-4. 넣는 방법

원본(`이전/backend/app/services/render.py` 의 `IMAGE_PROMPT`)에서 옮길 때 반드시
고쳐야 할 곳이 **한 군데** 있었다.

| 위치                  | 원본                                                              | 이 프로젝트                                                                 | 왜                                                                                                                                                                                                                        |
| --------------------- | ----------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `[Character Rules]` | "반드시 입력된 실제**인원 수**·관계·연령 구성을 기준으로" | "입력된**동행 구성(관계)만** 기준으로 … 인원 수는 입력되지 않습니다" | 원본에는`partySize` 가 있었지만 이 프로젝트의 `trips` 테이블에는 **인원 수 컬럼이 없다**. 그대로 두면 모델이 인원을 지어내고, 혼자 가는 여행에 네 사람이 그려진 일정표는 귀여운 게 아니라 **틀린 것**이다 |

이 수정은 `_ILLUSTRATED_BODY` 에 이미 반영돼 있다.

---

## 4. [신규] `app/services/itinerary_export.py`

### 4-1. 이 파일이 하는 일

일정 dict 를 프롬프트 문자열로 조립하고, Gemini 를 불러 PNG bytes 를 받고,
캐시에 넣는다. **실패는 전부 `None` 이고 예외를 올리지 않는다** — 예외를 올리면
다운로드 화면 전체가 멎는데, 그림은 없어도 일정은 있으므로 텍스트로 안내할 수 있다.

### 4-2. 함수 목록

| 함수                                            | 역할                          | 주의                                                 |
| ----------------------------------------------- | ----------------------------- | ---------------------------------------------------- |
| `_clock_text(value, tz)`                      | UTC → 여행지 현지`HH:MM`   | §4-4 ① —**가장 조용히 틀리는 곳**           |
| `_plan_fingerprint(trip, days)`               | 일정 내용 해시                | §4-4 ②                                             |
| `_cache_key(trip_id, style, fp)`              | 캐시 키                       | **`style` 필수** (§4-4 ②)                  |
| `_party_text(trip)`                           | `travel_party` → 한글 라벨 | `TRAVEL_PARTY_LABELS` 를 import, 중복 정의 금지    |
| `_period_text(trip)`                          | 기간 문구                     | `start_date` 없으면 "날짜 미정 (시각 대신 순서로)" |
| `_plan_text(trip, days)`                      | 일정 본문                     | §4-4 ③ 정렬 규칙                                   |
| `build_prompt(trip, days, style)`             | 프롬프트 완성                 | **스타일 분기가 일어나는 유일한 지점**         |
| `_first_image(response)`                      | 응답에서 첫 이미지 파트 추출  | 모델은 그림과 글을 섞어 준다                         |
| `_chunk_days(days)`                          | 일정을 페이지로 자름          | §13-2 — 장수는 코드가 정한다                      |
| `_generate_one(...)`                         | **한 장** 그리기              | 캐시 → 호출 → 1회 재시도 → 캐시 저장              |
| `generate_itinerary_images(trip, days, style)` | 본체 — 페이지 분할 + 병렬 호출 | §13-2 · 길이 N 리스트를 돌려준다                  |
| `itinerary_as_text(trip, days)`               | 텍스트 대체 수단              | 이미지 실패 시 유일한 길                             |

### 4-3. 데이터 매핑 — 원본과 필드명이 전부 다르다

**복사가 아니라 다시 써야 하는 이유가 이 표다.**

| 원본 백엔드                          | 이 프로젝트                                      | 비고                                       |
| ------------------------------------ | ------------------------------------------------ | ------------------------------------------ |
| `trip["constraints"]["startDate"]` | `trip["start_date"]`                           |                                            |
| `constraints["endDate"]`           | `trip["end_date"]`                             |                                            |
| `constraints["destination"]`       | `trip["destination"]`                          | nullable                                   |
| `constraints["dateUndecided"]`     | `trip["start_date"] is None`                   | 파생값                                     |
| `constraints["companionType"]`     | `trip["travel_party"]`                         | **값 집합이 다름**                   |
| `constraints["partySize"]`         | **없음**                                   | §3-4 프롬프트 수정으로 대응               |
| `itinerary["version"]`             | **없음**                                   | 내용 해시로 대체 (§4-4 ②)                |
| `day["day_no"]`                    | `day["day_number"]`                            |                                            |
| `day["date"]`                      | `day["travel_date"]`                           |                                            |
| `day["weather"]`                   | **없음**                                   | 대시보드가 날씨를 붙이지 않음 → 제거      |
| `item["place_name"]`               | `item["title"]`                                |                                            |
| `item["category"]`                 | `item["item_type"]`                            | **영문 enum** → 한글 라벨 매핑 필요 |
| `item["start_time"]` (`"09:00"`) | `item["start_at"]` (**UTC timestamptz**) | §4-4 ①                                   |
| `item["stay_minutes"]`             | `item["estimated_stay_minutes"]`               | nullable                                   |
| `item["order_no"]`                 | `item["sort_order"]`                           |                                            |
| `item["data_status"]`              | **없음**                                   | 제거                                       |

### 4-4. 반드시 지킬 것 3가지

#### ① `start_at` 은 UTC 다 — 현지 시각으로 바꿔야 한다

원본의 `start_time` 은 이미 현지 `"09:00"` 문자열이었지만 여기는 `timestamptz`
(UTC)다. 그대로 앞 5글자를 자르면 **일본 여행 09:00 일정이 00:00 으로 그려진다.**
이미지는 멀쩡해 보이므로 눈으로는 안 잡힌다.

`routers/trips.py` 의 `_clock_text()` 가 정확히 이 변환을 한다. 그러나
**import 하면 순환 참조가 된다** (`trips.py` → `itinerary_export.py` → `trips.py`).
같은 규칙 12줄을 새 파일에 복제한다 (`itinerary_export._clock_text`).

#### ② 캐시 키는 `trip_id : style : 내용해시`

| 후보                     | 쓸 수 있나 | 왜                                                                                             |
| ------------------------ | ---------- | ---------------------------------------------------------------------------------------------- |
| `itinerary["version"]` | ✗         | 이 프로젝트에 버전 개념이 없다                                                                 |
| `trips.updated_at`     | ✗         | `touch_trip()` 이 **채팅 한 줄에도** 갱신한다. 대화할 때마다 애써 그린 그림이 버려진다 |
| 일정 내용 해시           | ✓         | DB 변경 0, 일정이 그대로면 지문도 그대로                                                       |

**`style` 을 키에 반드시 넣는다.** 빠뜨리면 심플형을 먼저 받은 사용자가
일러스트형을 눌렀을 때 캐시에 있던 심플형 그림을 그대로 받는다. 화면은 성공했다고
표시하는데 그림만 다르므로, 사용자는 자기가 무엇을 잘못 눌렀는지 알 수 없다.

#### ③ 항목 정렬을 화면과 맞춘다

`trip_dashboard()` 는 `.order("sort_order")` 로만 정렬해 돌려주지만, 화면·지도는
`_ordered_day_items()` 의 **시간 우선** 기준을 쓴다. 프롬프트를 만들 때 같은 규칙으로
다시 정렬하지 않으면 그림이 화면과 다른 순서로 나온다.

정렬 키: `(start_at 이 None 인가, start_at 문자열, sort_order)`

### 4-5. 넣는 방법

`app/services/itinerary_export.py` 에 이 프로젝트에 맞춰 다음이 반영돼 있다.

- `_clock_text` 복제 (§4-4 ①)
- 캐시 키에 `style` 포함 (§4-4 ②)
- 시간 우선 정렬 (§4-4 ③)
- `TRAVEL_PARTY_LABELS` import
- `item_type` 영문 enum → 한글 라벨
- 예외 원문 대신 `type(error).__name__` 만 로깅 (이 저장소의 기존 관례)

---

## 5. [수정] `app/routers/trips.py`

### 5-1. 어디를 고치는가

| #  | 위치                           | 무엇을                              |
| -- | ------------------------------ | ----------------------------------- |
| ① | 7행`from fastapi import …`  | `Response` 추가                   |
| ② | 3~5행 사이                     | `from typing import Literal` 추가 |
| ③ | 17행 근처 services import 블록 | 새 서비스 import 추가               |
| ④ | 파일 맨 끝 (1618행 뒤)         | 헬퍼 1개 + 엔드포인트 2개 추가      |

### 5-2. ① ② ③ — import 3줄

현재 7행:

```python
from fastapi import APIRouter, Depends, HTTPException, status
```

세 곳을 이렇게 바꾼다.

```diff
  import logging
  from collections import Counter
  from datetime import date, datetime, time, timedelta, timezone
+ from typing import Literal
  from uuid import UUID, uuid4
  from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

- from fastapi import APIRouter, Depends, HTTPException, status
+ from fastapi import APIRouter, Depends, HTTPException, Response, status
```

```diff
  from app.services.itinerary_generation import generate_daily_itinerary_drafts
  from app.services.itinerary_routing import group_nearby_itinerary_places
+ from app.services.itinerary_export import generate_itinerary_image, itinerary_as_text
  from app.services.destination_scope import resolve_destination_scope
```

| 추가         | 왜                                                                                                                                                                                                                            |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Literal`  | `style` 을 `Literal["simple", "illustrated"]` 로 받으면 FastAPI 가 **잘못된 값을 422 로 먼저 막고** Swagger 에 선택지로 표시한다. `maps.py` 가 `RouteTravelMode` 에 이미 같은 방식을 쓰고 있어 새 관례가 아니다 |
| `Response` | `maps.py` 가 `/days/{day_id}/map/image` 에서 `from fastapi import …, Response` 로 최상단에서 받아 같은 방식으로 이미지를 반환한다. 관례를 맞춘다                                                                       |
| 새 서비스    | 순환 참조 없음 —`itinerary_export.py` 는 라우터를 import 하지 않는다 (§4-4 ① 에서 `_clock_text` 를 복제한 이유가 그것이다)                                                                                             |

`Query` 는 필요 없다. `style: Literal[…] = "simple"` 만으로 쿼리 파라미터가 된다.

### 5-3. ④ — 파일 맨 끝에 추가

`delete_itinerary_item()` 끝 뒤에 이어 붙인다. 들어가는 것은 셋이다.

| 이름                                           | 역할                   |
| ---------------------------------------------- | ---------------------- |
| `_itinerary_export_zip(trip, images)`        | 장별 PNG → ZIP + 한글 파일명 헤더 (§13-3) |
| `GET /trips/{trip_id}/itinerary/export`      | 이미지 다운로드        |
| `GET /trips/{trip_id}/itinerary/export/text` | 텍스트 대체 수단       |

설명이 필요한 부분만 짚는다.

**한글 파일명** — HTTP 헤더는 latin-1 만 담을 수 있어 한글 파일명을 그대로 넣으면
응답 자체가 터진다. RFC 5987 로 UTF-8 이름을 주고, 그것을 못 읽는 클라이언트를 위해
ASCII 이름도 함께 둔다. 두 스타일이 **같은 이름 규칙**을 써야 한다 — 사용자에게는
같은 [다운로드] 의 결과이므로, 파일 이름이 다르면 어느 쪽으로 받았는지를 사용자가
알아야 하는 셈이 된다.

**`trip_dashboard()` 재사용** — 소유권 확인(RLS)과 trip/days 로딩이 한 번에 끝나고,
**화면과 완전히 같은 데이터**를 그린다. `_attach_cached_places()` 가 `places` 를 한 번
더 조회하는 낭비가 있지만, 뒤에 20~40초짜리 LLM 호출이 붙는 API 라 쿼리 한 번보다
화면과 같은 데이터를 그리는 것이 중요하다.

**`metadata` 키 이름 주의** — `routers/console.py` 가 `activity_logs` 를 metadata
**키 이름으로 분류**한다. `rating` · `sentiment` · `feedback` · `pace` ·
`intensity` · `travel_intensity` 를 쓰면 이 로그가 운영 콘솔의 피드백·페이스 집계에
잘못 섞인다. `style` · `format` · `size_bytes` 는 안전하다.

### 5-4. 건드리지 않는 것

기존 함수·엔드포인트·헬퍼는 **읽기만 하고 고치지 않는다.** 새 코드가 쓰는 것은
`trip_dashboard()` · `record_activity()` · `get_user_client()` 뿐이고, 셋 다 이미
이 파일 안에 있거나 import 돼 있다.

---

## 6. [수정] `.env`

`GEMINI_MODEL` 줄 아래에 붙인다.

```diff
  GEMINI_API_KEY =...
  GEMINI_MODEL=gemini-3.5-flash-lite
+
+ # 일정표 이미지 생성 모델. 미설정이면 코드 기본값(gemini-3-pro-image)을 쓴다.
+ # 키 이름 앞뒤에 공백을 넣지 말 것. 이 파일의 GEMINI_API_KEY 등에는 공백이
+ # 섞여 있고 python-dotenv 가 지워 주므로 지금은 동작하지만, 따라 하지는 말 것.
+ GEMINI_IMAGE_MODEL=gemini-3-pro-image
```

> **확인 완료.** 이 `.env` 의 키로 `models.list()` 를 조회하니 이미지 모델이 6종
> 보인다 — `gemini-3-pro-image`, `gemini-3-pro-image-preview`,
> `gemini-3.1-flash-image`, `gemini-3.1-flash-lite-image`,
> `gemini-3.1-flash-image-preview`, `gemini-2.5-flash-image`.
>
> 다만 **목록에 보이는 것과 실제 생성 호출이 통과하는 것은 다를 수 있다.**
> §10 의 1번(실제 생성 1회)까지 해야 확인이 끝난다.
>
> 모델 선택은 품질/속도 트레이드오프다. 일정표는 글자가 많아 `-pro-image` 를
> 기본으로 뒀지만 `-flash-image` 계열이 더 빠르다. 같은 일정으로 두 모델을 한 장씩
> 뽑아 **장소명이 정확한지** 비교한 뒤 정할 것.

`load_dotenv()` 는 `app/db.py` 에서만 호출된다. 새 서비스는 `os.getenv` 를
**모듈 로드 시점이 아니라 함수 호출 시점**에 읽으므로 순서 문제가 없다.

---

## 7. 남는 결정 사항

### ① 프론트 타임아웃 — 기본 30초로는 반드시 끊긴다

이미지 생성 20~40초 + 재시도까지 최대 80초다. 여기서 먼저 끊으면 다 그린 그림을
버리고 "만들 수 없어요" 를 띄우게 된다. 이 API 호출만 **150초 이상**으로 따로 잡을 것.

### ② 심플형 글자 정확도

원본 프로젝트는 심플형을 Pillow 로 남겼다. 인쇄용이라 시각·장소명이 정확해야 하는데
이미지 모델이 긴 한글을 정확히 그리지 못한다는 것이 이유였다.

이번에는 심플형도 LLM 이 그린다. 얻는 것과 잃는 것은 이렇다.

| 얻는 것                    | 잃는 것                        | 대응                                                   |
| -------------------------- | ------------------------------ | ------------------------------------------------------ |
| `pillow` 의존성 0        | 글자 정확도 보장 없음          | 프롬프트에서**긴 문장 금지 · 시각/장소명 우선** |
| 한글 TTF 번들(~2MB) 불필요 | 같은 일정도 매번 미묘하게 다름 | **내용 해시 캐시**로 같은 일정은 같은 그림 고정  |
| 렌더 코드 400줄 불필요     | 폰트 누락 사고 대신 모델 사고  | 실패는 전부`None` → 텍스트 대체 경로                |
| 두 스타일이 한 코드 경로   | 인쇄 시 글자 깨질 여지         | `/export/text` 를 선택이 아니라 **상시 제공**  |

적용 후 실제 일정으로 몇 장 뽑아 **장소명과 시각이 맞는지** 눈으로 확인하고, 품질이
부족하면 그때 심플형만 Pillow 로 되돌리는 선택지가 남아 있다. 그 경우 필요한 것은
`pillow` 1개와 `app/assets/fonts/` 한글 TTF 이며, 지금의 프롬프트 레지스트리 구조는
그대로 둔 채 심플형만 다른 경로로 보내면 된다.

### ③ `trip_dashboard()` 재사용 vs 직접 조회

지금 설계는 `trip_dashboard()` 를 쓴다 (§5-3). 성능을 우선하면 `_owned_trip()` +
`trip_days`/`itinerary_items` 직접 조회로 바꾼다(+15줄).

---

## 8. 코드 대조 점검 결과

이 문서의 주장을 실제 저장소 코드·설치된 패키지·`.env` 에 대조한 기록이다.

### 8-1. 확인 완료

| #  | 확인 항목                     | 근거                                            | 결과                                                                                                                                     |
| -- | ----------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| 1  | 라우트 경로 충돌              | 전체 라우터 경로 36개 전수 조회                 | `/trips/{id}/itinerary/…` 없음. `itinerary-items` · `itinerary-changes` 와 겹치지 않음                                           |
| 2  | `cache_get` / `cache_set` | `app/cache.py`                                | 원본과 동일, 수정 불필요                                                                                                                 |
| 3  | `trip_dashboard()` 반환     | `app/routers/trips.py`                        | `{"trip", "days"}`, 각 `day["items"]` 를 항상 채움                                                                                   |
| 4  | `trips.py` 기존 import      | 7행 및 9~30행                                   | `UUID` `HTTPException` `Depends` `status` `CurrentUser` `get_current_user` `get_user_client` `record_activity` 모두 있음 |
| 5  | `activity_logs.event_type`  | `supabase/20260907_dashboard_logs_merged.sql` | `not blank` 만 있고 값 CHECK 없음 → **마이그레이션 불필요**                                                                     |
| 6  | 요청 로그 미들웨어            | `app/request_logging.py`                      | 응답 body 를 읽지 않음 → PNG 에 안전                                                                                                    |
| 7  | 의존성                        | `pyproject.toml`                              | `google-genai` 있음, `pillow` 없음 → **새 의존성 없음**                                                                       |
| 8  | `TRAVEL_PARTY_LABELS`       | `app/services/travel_preferences.py`          | 9종 라벨 존재 → 그대로 import                                                                                                           |
| 9  | `.env` 키 공백              | `dotenv_values('.env')` 실행                  | `GEMINI_API_KEY =` 처럼 공백이 있어도 키는 정상 인식 — **문제 없음**                                                            |
| 10 | 이미지 모델 접근              | `models.list()` 조회                          | 이미지 모델 6종 확인 (§6)                                                                                                               |
| 11 | `response_modalities`       | `google/genai/types.py`                       | `GenerateContentConfig.response_modalities: list[str]` 존재                                                                            |
| 12 | `http_options` timeout      | `types.py` · `client.py`                   | `HttpOptions.timeout` 존재, **단위 밀리초**, `Client` 가 dict 를 받아 변환                                                     |
| 13 | `Part.inline_data`          | `google/genai/types.py`                       | 존재.`Blob.data` 는 `bytes` 로 선언                                                                                                  |

설치된 `google-genai` 는 **2.22.0** 이다 (원본 프로젝트와 동일).

### 8-2. 점검으로 바뀐 것 (이 문서에 반영됨)

| # | 무엇이                     | 어떻게                                                             | 왜                                                                            |
| - | -------------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------- |
| 1 | 기본 이미지 모델           | `gemini-3-pro-image-preview` → **`gemini-3-pro-image`** | 이 키에서 GA 모델이 조회된다. preview 이름 변경 위험을 감수할 이유가 없다     |
| 2 | `genai.Client` 생성 위치 | 재시도 루프**안 → 밖**                                      | 재시도마다 새로 만들 이유가 없다                                              |
| 3 | `Response` import        | 지역 →**파일 상단**                                         | `maps.py` 가 이미 같은 방식을 쓴다                                          |
| 4 | `Literal` import 근거    | "이 파일에 없다" →**"`maps.py` 의 기존 관례"**            | 새 방식 도입이 아니다                                                         |
| 5 | `.env` 안내              | 모델명 +**공백 주의** 추가                                   | 기존 줄에 공백이 섞여 있어 따라 쓰기 쉽다                                     |
| 6 | `inline_data` str 분기   | 주석에**"방어 코드"** 명시                                         | 2.22.0 에서는`bytes` 라 실행되지 않는다. 지우고 싶어질 코드라 이유를 남긴다 |
| 7 | 키 권한 경고               | "가장 흔한 실패 원인" →**확인 완료 + 남은 확인**            | 목록 조회는 통과했다                                                          |

### 8-3. 이미 있는 선례

`app/routers/maps.py` 에 **`GET /trips/{trip_id}/days/{day_id}/map/image`** 가 있다.
Static Maps 이미지를 프록시해 `Response(content=…, media_type=…)` 로 내려보낸다.

즉 이 저장소는 **이미 "바이너리 이미지를 반환하는 GET 엔드포인트" 패턴을 갖고 있고**,
이번 작업은 새 패턴 도입이 아니라 그 패턴을 한 번 더 쓰는 것이다. 다른 점은 헤더뿐 —
`map/image` 는 화면에 그리는 이미지라 `Cache-Control` 만 붙이지만, 일정표는 **파일로
저장**하는 것이라 `Content-Disposition: attachment` 와 파일명이 필요하다.

### 8-4. 아직 확인하지 못한 것

| 항목                              | 왜 확인 못 했나                               | 언제 드러나나                      |
| --------------------------------- | --------------------------------------------- | ---------------------------------- |
| 실제 이미지 생성 성공             | 생성 호출은 과금·시간이 들어 실행하지 않았다 | §10 의 1번                        |
| 심플형 결과물 품질                | 위와 같음                                     | §10 의 1번                        |
| **한글 장소명 렌더 정확도** | 위와 같음                                     | §10 의 1번 —**가장 중요**  |
| 생성 1회 소요 시간                | 위와 같음                                     | §10 의 1번 (프론트 타임아웃 근거) |

---

## 9. 테스트 추가 제안 (선택)

`tests/` 의 기존 8개는 대부분 LLM 호출 없이 순수 함수를 검증한다
(`test_itinerary_local_time.py`, `test_travel_preferences.py` 등). 같은 결로
**LLM 을 부르지 않는 부분만** 테스트하면 회귀를 싸게 막을 수 있다.
네트워크를 타는 것은 `generate_itinerary_image` 뿐이다.

| 테스트                                             | 무엇을 막는가                                                              |
| -------------------------------------------------- | -------------------------------------------------------------------------- |
| `test_plan_text_uses_trip_timezone`              | UTC → 현지 변환 누락. 09:00 이 00:00 으로 그려져도 이미지는 멀쩡해 보인다 |
| `test_cache_key_differs_by_style`                | 스타일이 캐시 키에서 빠지는 것 (§4-4 ②)                                  |
| `test_fingerprint_ignores_unrelated_trip_fields` | 채팅 뒤에도 캐시가 살아 있는지                                             |
| `test_build_prompt_rejects_unknown_style`        | 없는 스타일에 조용히 빈 프롬프트를 만드는 것                               |

---

## 10. 적용 후 확인 순서

| #  | 확인                                              | 기대 결과                                                    |
| -- | ------------------------------------------------- | ------------------------------------------------------------ |
| 1  | `GET /trips/{id}/itinerary/export?style=simple` | 200 + PNG (첫 호출 20~40초)                                  |
| 2  | 같은 요청 재호출                                  | **즉시** 응답 (캐시 적중)                              |
| 3  | `?style=illustrated`                            | **다시 20~40초** + 다른 그림 (스타일별 캐시 분리)      |
| 4  | 일정 항목 하나 수정 후 재호출                     | **다시 20~40초** (지문 변경)                           |
| 5  | 채팅 메시지만 보낸 뒤 재호출                      | **즉시** 응답 (`updated_at` 이 아니라 지문을 쓰는지) |
| 6  | `?style=none`                                   | 422 (Literal 검증)                                           |
| 7  | `GEMINI_IMAGE_MODEL` 을 없는 이름으로           | 503 + 로그 경고,**다른 API 는 정상**                   |
| 8  | 일정이 없는 여행                                  | 404                                                          |
| 9  | 다른 사용자의`trip_id`                          | 404 (RLS)                                                    |
| 10 | 한글 여행지로 다운로드                            | 파일명`tripmate_오사카_20261010.png`                       |

**5번이 가장 놓치기 쉽다.** 캐시 키를 `trips.updated_at` 으로 만들면 여기서 매번 다시
그리게 되고, 그 사실은 요금 고지서에서야 드러난다.

---

## 11. [수정] 프론트엔드 `2026_aio2_TripMate_Frontend`

### 11-1. 현재 상태 — 버튼이 없다

| 확인                                      | 결과                                                                        |
| ----------------------------------------- | --------------------------------------------------------------------------- |
| `st.download_button` 사용               | **0회**                                                               |
| "다운로드" · "export" · "일정표" 문자열 | **0회** (`streamlit_app.py` 4351줄 · `common.py` · `test.py`) |
| `/itinerary/export` 호출                | **0회**                                                               |

시안 SCR-005 의 상단 `[일정표 다운로드]` 버튼과 SCR-007 의 스타일 선택 모달은
아직 구현돼 있지 않다. **백엔드 API 를 만들어도 누를 곳이 없다.**

다만 필요한 조각은 이미 있다.

| 이미 있는 것               | 위치                                                     | 상태                                                          |
| -------------------------- | -------------------------------------------------------- | ------------------------------------------------------------- |
| 인증 붙은 이진 데이터 요청 | `common.py` 의 `api_bytes()`                         | 있지만**아무 데서도 안 쓰인다** (지도는 JS 지도로 갔다) |
| 모달 패턴                  | `@st.dialog` 3곳 (Mate 설정 · 계정 관리 · 여행 삭제) | 그대로 따라 쓰면 된다                                         |
| 세션 기본값 관리           | 726행`st.session_state.setdefault` 블록                | 키 3개만 추가                                                 |

### 11-2. 수정 범위

| 파일                 | 작업           | 어디를                                                              | 규모  |
| -------------------- | -------------- | ------------------------------------------------------------------- | ----- |
| `common.py`        | **수정** | `api_bytes()` 를 `api_binary()` 로 감싸기                       | +14줄 |
| `streamlit_app.py` | **수정** | ① import ② 세션 기본값 ③ 모달 함수 ④`render_dashboard()` 버튼 | +95줄 |

### 11-3. `common.py` — 두 가지를 고쳐야 한다

#### ① 타임아웃이 하드코딩돼 있다

```python
# common.py:129 — 현재
response = httpx.request(method, f"{BACKEND_URL}{path}", timeout=HTTP_TIMEOUT, **kwargs)
#                                                                └─ HTTP_TIMEOUT = 60
```

**60초로는 반드시 끊긴다.** 이미지 생성 20~40초 + 빈 응답 재시도까지 최대 80초다
(백엔드 §7 ①). 바로 위 `api()` 는 `timeout` kwarg 를 받는데(99행) `api_bytes` 만
빠져 있다.

#### ② 헤더를 돌려주지 않아 파일 이름을 읽을 수 없다

`api_bytes()` 는 `response.content` 만 반환한다. 그런데 서버는 파일 이름을
`Content-Disposition` **헤더**로 준다 (백엔드 §5-3). 헤더를 못 읽으면 선택지는
둘뿐인데, 둘 다 나쁘다.

| 방법                                 | 왜 안 되나                                                                                  |
| ------------------------------------ | ------------------------------------------------------------------------------------------- |
| 화면에서 파일명을 직접 조립          | 서버와**같은 규칙을 두 곳에 두는** 것이다. 한쪽만 고치면 두 경로의 파일 이름이 갈린다 |
| 이름을 포기하고`tripmate.png` 고정 | 여행을 여러 개 받아 두면 어느 여행인지 알 수 없다                                           |

그래서 **응답 전체를 돌려주는 `api_binary()` 를 만들고 `api_bytes()` 는 그것을
감싸게** 한다. `api_bytes()` 의 동작은 그대로라 기존 호출부(현재 0곳)에 영향이 없다.

**넣는 방법**: `api_binary()` 를 만들고 `api_bytes()` 가 그것을 감싸게 한다.

### 11-4. `streamlit_app.py` — 네 군데

| #  | 위치                                  | 무엇을                        |
| -- | ------------------------------------- | ----------------------------- |
| ① | 16행`from common import (…)`       | `api_binary` 추가           |
| ② | 726행 근처 세션 기본값 dict           | 키 3개 추가                   |
| ③ | `render_dashboard()` 앞 (3661행 위) | 모달 함수 3개 + 상수 2개 추가 |
| ④ | `render_dashboard()` 안             | 버튼 1개 + 모달 호출 1줄      |

**넣는 방법**: 부록의 위치 지도에서 각 이름을 찾아 확인한다.

### 11-5. 설명이 필요한 부분

#### 팝업이 뜰 때가 아니라 **스타일을 고를 때** 만든다

모달이 열리자마자 그리면, 사용자가 스타일을 고르기도 전에 이미 다른 스타일을 그리고
있는 셈이 된다. 일정을 확인하려다 실수로 눌러도 요금이 나간다.

카드를 누른 그 순간이 "만들기" 다. 상태를 두 개로 나눠 둔다.

| 세션 키 | 뜻 |
|---|---|
| `export_style` | 화면에서 고른 스타일 |
| `export_requested_style` | **실제로 만들라고 누른** 스타일 |

`export_requested_style` 이 `None` 이면 안내만 띄우고 **`return` 으로 빠져나온다** —
생성 호출에 도달하지 않는다. 모달을 열 때와 닫을 때 이 값을 비우므로, 다시 열어도
지난번 선택으로 자동 생성되지 않는다.

카드에는 상태를 글자로도 적는다. 색만으로 가르지 않는 것은 이 프로젝트의 공통
규칙이다.

| 상태 | 배지 |
|---|---|
| 방금 만든 스타일 | `✓ 만들었어요` |
| 세션에 남아 있는 스타일 | `만들어 둠` — 눌러도 즉시 나온다 |
| 아직 안 만든 스타일 | 없음 |

#### 기다리는 동안 문구를 갈아 끼운다

**남은 시간을 적지 않는다.** 일정 길이와 참조 이미지 방식(§13-5) 때문에 20초에서
80초까지 벌어지는데, "20~40초" 라고 적어 두면 40초가 지난 순간부터는 안내가 아니라
거짓말이 된다. 무엇을 하고 있는지를 대신 보여 준다.

`EXPORT_PROGRESS_PHRASES` 7개를 `EXPORT_PROGRESS_INTERVAL_SECONDS`(2.8초)마다 바꾼다.
한 바퀴가 19.6초라 가장 짧은 대기에도 서너 개가 보이고, 가장 긴 대기에도 같은 문구가
연달아 나오지 않는다. **시작 문구는 매번 무작위**다 — 늘 같은 문구로 시작하면 다시
받을 때 멈춘 것처럼 보인다.

#### 그래서 다운로드를 워커 스레드가 한다

`st.spinner` 는 **실행 중에 문구를 바꿀 수 없다.** 한 번 띄우면 블로킹 호출이 끝날
때까지 그대로다. 그래서 구조를 이렇게 바꿨다.

```
전:  with st.spinner("…"):            메인 스레드가 멈춰 있어 못 바꾼다
         pages = _fetch_export_pages(…)

후:  워커 스레드가 내려받고,
     메인 스레드는 st.empty() 자리를 갈아 끼운다
```

| 함수 | 어느 스레드 | 하는 일 |
|---|---|---|
| `_fetch_export_pages` | **메인** | 캐시 읽기·쓰기, 인증 헤더 만들기, 문구 갈아 끼우기 |
| `_download_export_pages` | **워커** | HTTP 요청 + ZIP 풀기 **만** |

**`st.session_state` 와 `auth_headers()` 는 워커에서 부르면 안 된다.** 세션 컨텍스트가
없어 실패한다. 그래서 헤더는 메인 스레드에서 미리 만들어 인자로 넘긴다.

기다리는 방식은 `future.result(timeout=…)` 이다. `sleep` 뒤에 확인하는 방식으로 짜면
작업이 끝나도 남은 간격만큼 더 기다린다.

#### 받아 둔 그림을 세션에 캐시한다

`@st.dialog` 안은 **위젯을 누를 때마다 스크립트 전체가 재실행된다.** 캐시하지 않으면
스타일 카드를 눌러 보는 것만으로 생성이 다시 돌아간다. 서버에도 캐시가 있지만
(§4-4 ②) 그것은 **같은 스타일**일 때만 즉시 응답이고, 스타일을 오가면 매번 새로 든다.

`st.session_state.export_images` 에 `{trip_id}:{style}` 로 담는다. 캐시에 있으면
**문구도 띄우지 않고** 곧바로 돌려준다. 세션 캐시라 새로고침하면 사라지고, 그때는
서버 캐시가 받아 준다.

#### 여러 장이면 장별로 저장한다

`len(pages) > 1` 이면 2열 격자에 썸네일과 `[N장 저장]` 버튼을 놓고, 왜 나뉘었는지
한 줄로 알린다 — 설명이 없으면 사용자는 일정이 잘려 나간 줄 안다. 썸네일을 함께
보여 주는 것은 버튼만 있으면 어느 장이 며칠치인지 알 수 없기 때문이다.

한 장이면 격자를 만들지 않고 `[다운로드]` 하나로 끝낸다.

#### 버튼 위치

시안 SCR-005 는 상단 우측에 `[일정표 다운로드]` 를 둔다. 현재 화면 구조에서 그에
대응하는 자리는 **DAY 탭 줄 아래, "오늘의 일정" 위**다 — 일정을 보고 나서 누르는
동작이므로 일정 위에 있는 편이 자연스럽다.

`@st.dialog` 는 컬럼 안에서 열면 레이아웃이 깨진다. 사이드바가 이미 쓰는 규칙대로
**컬럼 바깥에서** 연다 (`streamlit_app.py` 의 "dialog는 sidebar 컨테이너 바깥에서
열어" 주석 참고).

> **텍스트 대체 수단은 화면에서 뺐다.** `/itinerary/export/text` 엔드포인트는 백엔드에
> 남아 있으므로(§5-3) 필요하면 다시 붙일 수 있다.

### 11-6. 적용 후 확인

| # | 확인 | 기대 결과 |
|---|---|---|
| 1 | `[일정표 다운로드]` 클릭 | 모달이 열리고 **아무것도 만들지 않는다** · 카드 2장 + 안내 |
| 2 | 심플형 카드 클릭 | 그때 만들기 시작 · **문구가 2.8초마다 바뀐다** |
| 3 | 완료 | 문구가 사라지고 저장 버튼이 나온다 |
| 4 | 저장 클릭 | `tripmate_오사카_20261010_1of2.png` |
| 5 | 모달 닫았다 다시 열기 | **또 카드 선택 대기** (자동 생성 안 함) |
| 6 | 아까 만든 스타일 다시 클릭 | 카드에 `만들어 둠` 배지 · **문구 없이 즉시** |
| 7 | 다른 스타일 클릭 | 다시 문구가 돌고 새 그림 |
| 8 | 백엔드 끄고 클릭 | 경고 문구 (화면이 멈추지 않는다) |
| 9 | 4일 이상 여행 | 2열 격자 · 썸네일 + `[N장 저장]` 버튼 |

**1번과 5번이 핵심이다.** 모달을 열기만 해도 생성이 돌면 요금이 새고, 사용자가
고르기도 전에 엉뚱한 스타일을 그린다.

**2번에서 문구가 안 바뀌면** 워커 스레드가 아니라 메인 스레드에서 내려받고 있다는
뜻이다 (§11-5).

## 12. 적용 결과 · 수정 이력 (2026-09-08)

§1~§11 의 계획을 양쪽 저장소에 적용했다. 이 장은 **계획이 아니라 실제로 들어간 것**을
적는다. 적용 도중 서버와 화면이 모두 뜨지 않았고, 원인은 전부 붙여넣기 손상이었다.

### 12-1. 실제로 들어간 것

| 파일                                        | 최종     | 상태                                                 |
| ------------------------------------------- | -------- | ---------------------------------------------------- |
| `app/services/itinerary_export_prompt.py` | 251줄    | 부록 A 반영 · 파일명 수정 (§12-3)                  |
| `app/services/itinerary_export.py`        | 295줄    | 부록 B 반영                                          |
| `app/routers/trips.py`                    | 1,717줄  | 엔드포인트 2개 · import 3줄 · 들여쓰기 수정 (§12-2) |
| `common.py`                               | 195줄    | `api_binary()` 추가, `api_bytes()` 가 이를 감쌈  |
| `streamlit_app.py`                        | 4,502줄  | 버튼 · 모달 · 세션 키 3개 (§12-2, §12-4)           |

등록된 엔드포인트 (OpenAPI 40개 경로 중):

```
GET /trips/{trip_id}/itinerary/export        ← 이미지
GET /trips/{trip_id}/itinerary/export/text   ← 호출부 없음 (§12-4)
```

### 12-2. 붙여넣기 손상 — diff 를 편집기에 그대로 붙였다

**서버·화면이 뜨지 않은 원인은 전부 이것 하나다.** 이 문서의 부록은 `diff` 블록으로
적혀 있어 `+` 마커와 1칸 오프셋이 딸려 온다. 그대로 붙이면 문법 오류가 된다.

| # | 파일                 | 위치                             | 증상                          |
| - | -------------------- | -------------------------------- | ----------------------------- |
| 1 | `streamlit_app.py` | import 블록 (16행)               | 전체 2칸 들여쓰기 +`+` 마커 |
| 2 | `streamlit_app.py` | 세션 기본값 (725행)              | `+` 마커 + 7칸 들여쓰기      |
| 3 | `streamlit_app.py` | `render_dashboard()` 버튼      | `+` 마커 + `if` 만 어긋남   |
| 4 | `streamlit_app.py` | 모달 호출 (3915행)               | 3칸 — 컬럼 안에서 열릴 뻔함  |
| 5 | `trips.py`         | `export_itinerary_image()` 3줄 | 5칸 · 7칸 · 10칸 들여쓰기    |

5번은 마커가 남지 않아 눈으로는 안 보였다. `IndentationError` 의 줄 번호만이 단서였다.

```
IndentationError: unexpected indent
  app/routers/trips.py, line 1686:  dashboard = trip_dashboard(client, trip_id)
```

> **다음부터.** 부록을 손으로 붙이지 말고 `git apply` 를 쓰거나, 붙인 직후
> `python -m compileall .` 로 확인할 것. 두 저장소 모두 **한 번에 한 파일씩** 확인하면
> 다섯 곳을 찾는 데 걸린 시간이 필요 없다.

### 12-3. 파일명에 보이지 않는 문자가 섞였다

`itinerary_export_prompt.py` 의 파일명 끝에 **U+200B (zero-width space)** 가 붙어 있었다.
탐색기·편집기 모두 정상으로 보이지만 import 는 실패한다.

```
ModuleNotFoundError: No module named 'app.services.itinerary_export_prompt'
```

파일명을 `itinerary_export_prompt.py` 로 다시 지정해 해결했다. 파일명을 문서에서
복사해 만들 때 생기는 문제이므로, **파일명은 손으로 타이핑할 것.**

### 12-4. 계획에서 **뺀** 것 — 텍스트 대체 수단

§11-5 · 부록 D 의 `_render_export_text_fallback()` 은 적용했다가 **제거했다.**
이미지 실패 시 일정 텍스트를 화면에 보여 주고(`st.code`) `[텍스트로 저장]` 버튼을
띄우는 부분 전체다.

| 제거한 것                             | 남은 것                              |
| ------------------------------------- | ------------------------------------ |
| 텍스트 미리보기 (`st.code`)         | 실패 시 경고 문구만                  |
| `[텍스트로 저장]` 다운로드 버튼     | —                                   |
| `/itinerary/export/text` 프론트 호출  | 백엔드 엔드포인트는 남아 있음 (미사용) |

`trips.py` 의 503 메시지도 함께 고쳤다. 없어진 기능을 안내하고 있었다.

```diff
- detail="지금은 일정표 이미지를 만들 수 없습니다. 일정 텍스트로 저장해 주세요.",
+ detail="지금은 일정표 이미지를 만들 수 없습니다. 잠시 후 다시 시도해 주세요.",
```

### 12-5. 지금 이 기능은 **동작하지 않는다** — 무료 등급 할당량 0

기능은 완성됐지만 실제 호출은 503 으로 끝난다. **코드 문제가 아니다.**

```
429 RESOURCE_EXHAUSTED
Quota exceeded for metric: generate_content_free_tier_requests,
  limit: 0, model: gemini-3-pro-image
```

핵심은 `limit: 0` 이다. 일시적 속도 제한이 아니라 **무료 등급에 할당량 자체가 없다.**
§6 이 "models.list() 로 6종이 보인다" 고 적은 것은 맞지만, 그 경고("목록에 보이는 것과
실제 생성 호출이 통과하는 것은 다를 수 있다")가 그대로 현실이 됐다.

| 모델                            | 결과            |
| ------------------------------- | --------------- |
| `gemini-3-pro-image` (기본값) | 429 · limit 0  |
| `gemini-3.1-flash-image`      | 429 · limit 0  |
| `gemini-3.1-flash-lite-image` | 429 · limit 0  |
| `gemini-2.5-flash-image`      | 429 · limit 0  |
| `-preview` 2종                | 429 · limit 0  |
| `gemini-3.5-flash-lite` (채팅) | **정상** |

**`GEMINI_IMAGE_MODEL` 을 바꿔도 해결되지 않는다.** Google AI Studio 프로젝트에 결제를
연결하는 것이 유일한 방법이다. 채팅이 되는데 일정표만 안 되는 이유가 이것이다 —
텍스트 모델은 무료 등급이 열려 있고 이미지 생성만 결제가 필요하다.

### 12-6. 남은 항목

| # | 항목                                                                                            | 판단                          |
| - | ----------------------------------------------------------------------------------------------- | ----------------------------- |
| 1 | `.env` 에 `GEMINI_IMAGE_MODEL` 미설정 (§6)                                                  | 선택 — 코드 기본값과 같다    |
| 2 | 결제 연결 (§12-5)                                                                              | **이것 없이는 못 쓴다** |
| 3 | `itinerary_export.py:255` 가 예외 종류만 로그에 남겨 429 를 구분 못 함                        | 고치면 진단이 빨라진다        |
| 4 | `/itinerary/export/text` 엔드포인트 · `itinerary_as_text()` 가 미사용                        | 유지 여부 미정                |
| 5 | §9 테스트 미추가 — 프론트 venv 에 `pytest` 자체가 없다                                       | 미결                          |

### 12-7. 검증한 것과 하지 않은 것

| 검증                                            | 결과                            |
| ----------------------------------------------- | ------------------------------- |
| 백엔드 전 `.py` 구문 검사                     | 오류 0건                        |
| `import app.main`                             | 성공 · OpenAPI 경로 40개       |
| `streamlit magic.add_magic()` + `compile()` | 성공                            |
| `import common` · `api_binary` 호출 가능    | 성공                            |
| Gemini 이미지 모델 실제 호출                    | **429 (§12-5)**           |
| **§10 · §11-6 의 런타임 확인표**    | **미실행** — 위 429 때문 |

§10 과 §11-6 은 결제를 연결한 뒤 처음부터 다시 밟아야 한다. 이 문서에서 아직 아무도
확인하지 않은 유일한 부분이다.

---

## 13. [확장] 일정이 길면 여러 장으로 나누기

> **적용 완료.** 이 절의 변경은 실제 코드에 이미 반영돼 있다. 아래는 무엇을 왜
> 그렇게 했는지의 기록이다. 되돌리거나 값을 조정할 때 이 문서를 본다.

### 13-1. 왜 프롬프트만으로는 안 되나

프롬프트의 `[Output]` 에 "4일 이상이면 페이지를 나눠 그리세요" 를 넣어도 장수가
늘지 않는다. 두 군데가 막고 있다.

| 위치 | 무엇이 막나 |
|---|---|
| `itinerary_export.py` `_first_image()` | 응답에서 **첫 이미지 파트 하나만** 꺼낸다 |
| `trips.py` `_itinerary_png_response()` | `image/png` **한 장만** 내려보낸다 |

그리고 더 근본적인 문제가 있다. **Gemini 이미지 모델은 한 번 호출에 보통 이미지를
하나 만든다.** "여러 페이지로 나눠 그려" 라고 하면 대개 이렇게 된다.

| 실제로 오는 것 | 빈도 |
|---|---|
| 한 장 안에 페이지 두 개를 나란히 그림 (여전히 1장) | 흔함 |
| 그냥 한 장에 다 우겨넣음 | 흔함 |
| 진짜로 이미지 파트를 2개 반환 | 드묾 · 통제 불가 |

**장수를 모델에게 맡기면 통제가 안 된다.** 7일 여행이 1장으로 올지 3장으로 올지
알 수 없고, 알 수 없으면 화면이 버튼을 몇 개 그릴지도 정할 수 없다.

### 13-2. 방식 — 서버가 일자를 잘라 페이지마다 따로 부른다

```
days (7일)
  └ _chunk_days()  →  [1-2일] [3-4일] [5-6일] [7일]
                         │       │       │      │
                         └───────┴───────┴──────┘  ThreadPoolExecutor 로 병렬 호출
                                     ↓
                         PNG 4장  →  ZIP 한 개로 응답
                                     ↓
                         화면이 ZIP 을 풀어 장별 [저장] 버튼 4개
```

**장수는 코드가 정한다.** 모델은 "이 2일치를 한 장에 그려라" 만 받으므로 통제된다.
덤으로 한 장에 2일치만 담기니 **글자가 훨씬 정확해진다** — 심플형 인쇄 품질 문제도
함께 나아진다.

### 13-3. 왜 ZIP 으로 보내나

장별 버튼을 만들려면 화면이 N장을 다 갖고 있어야 한다. 전송 방법은 셋이다.

| 방법 | 첫 생성 대기 (8일=4장) | 평가 |
|---|---|---|
| `?page=N` 으로 장별 PNG 요청 | **~160초** (프론트가 4번 순차 대기) | 타임아웃 초과 |
| JSON + base64 배열 | ~40초 | 크기가 33% 부풀고, `api_binary` 를 못 쓴다 |
| **ZIP 한 개** | **~40초** | 서버가 병렬로 만들어 한 번에 보낸다 |

ZIP 은 **전송 형식일 뿐**이다. 사용자는 ZIP 을 저장하지 않는다 — 화면이 풀어서
장별 PNG 버튼을 보여 준다.

압축은 `ZIP_STORED`(무압축)로 한다. PNG 는 이미 압축돼 있어 다시 압축해도 크기가
거의 안 줄고 시간만 든다.

### 13-4. 수정 범위

**백엔드**

| 파일 | 어디를 | 무엇을 |
|---|---|---|
| `itinerary_export_prompt.py` | `_SHARED_TAIL` 의 `[Output]` | "4일 이상이면 페이지를 나눠" → **"아래 일자만 한 장에"** |
| `itinerary_export.py` | 상수 블록 | `DAYS_PER_PAGE` · `MAX_PAGES` · `MAX_PARALLEL_PAGES` 추가 |
| `itinerary_export.py` | `_cache_key()` | 인자에 **`page` 추가** |
| `itinerary_export.py` | `build_prompt()` | `page` · `total_pages` 인자 추가 |
| `itinerary_export.py` | 신규 | `_chunk_days()` |
| `itinerary_export.py` | `generate_itinerary_image()` | → **`_generate_one()`** + **`generate_itinerary_images()`** |
| `itinerary_export.py` | 신규 | `generate_itinerary_images()` — 병렬 호출 |
| `trips.py` | `_itinerary_png_response()` | → **`_itinerary_export_zip()`** |
| `trips.py` | `export_itinerary_image()` | 반환을 ZIP 으로 |

**프론트엔드**

| 파일 | 어디를 | 무엇을 |
|---|---|---|
| `streamlit_app.py` | `_export_filename()` | **삭제** (ZIP 엔트리 이름이 곧 파일명) |
| `streamlit_app.py` | `_fetch_export_image()` | → **`_fetch_export_pages()`** — ZIP 을 풀어 장 목록 반환 |
| `streamlit_app.py` | `render_export_dialog()` | 장별 썸네일 + [저장] 버튼 |

> `common.py` 의 `api_binary()` 는 **그대로 쓴다.** ZIP 도 이진 응답이라 이미 넣어 둔
> 것이 그대로 맞는다. JSON 방식을 골랐다면 이 함수가 다시 죽은 코드가 됐을 것이다.

### 13-5. 장마다 톤이 갈리는 문제 — 첫 장을 기준으로 삼는다

페이지를 나눠 부르면 **각 호출이 서로를 모른다.** 모델이 매번 처음부터 해석하므로
종이색 · 선 굵기 · 글씨체 · 캐릭터 그림체가 장마다 달라진다. 나란히 놓으면 같은
일정표로 보이지 않는다.

**첫 장을 그린 뒤, 그 그림을 나머지 장에 참조 이미지로 함께 보낸다.**

```
전:  [1장] [2장] [3장]        3개 동시 · 서로 모름 · 40초
후:  [1장] -> [2장] [3장]     1장 완성 후 그것을 보며 동시 · 80초
```

`types.Part.from_bytes()` 로 첫 장 PNG 를 `contents` 맨 앞에 넣고 뒤에 프롬프트를
둔다 — 모델이 "이것을 보고 저렇게 하라" 로 읽는다.

지시문은 `_STYLE_REFERENCE_NOTE` 상수로 따로 둔다. **첫 장에는 붙으면 안 되기
때문이다** — 참조할 그림이 없는데 "함께 보낸 이미지와 같게" 라고 하면 모델이 없는
것을 찾는다.

| 얻는 것 | 잃는 것 |
|---|---|
| 종이색 · 선 · 글씨체 · 그림체가 장마다 같아진다 | 첫 장을 기다려야 해 **40초 → 80초** |

순차로 네 번 부르는 160초보다는 짧고 화면 타임아웃(150초) 안이다.

#### 참조 이미지가 캐시 키에 들어가야 한다

첫 장이 다시 그려지면 색과 그림체가 달라지는데, 2~4장의 캐시 키가 그대로면 **옛 첫
장에 맞춰 그린 그림**이 나온다. 통일하려던 것이 오히려 어긋난다.

그래서 2장부터는 지문에 첫 장의 해시를 덧붙인다 — `{지문}-{sha256(1장)[:8]}`.

#### 첫 장이 실패하면

기준이 없으므로 통일은 포기하고 **나머지라도 참조 없이 그린다.** 첫 장이 실패했다고
2~4장까지 버리면 사용자는 아무것도 못 받는다.

#### 아직 남은 흔들림

이것으로도 부족하면 다음 두 가지가 남아 있다. 둘 다 시간 손해가 없다.

| 방법 | 무엇 |
|---|---|
| `image_config=types.ImageConfig(aspect_ratio="3:4")` | 장마다 캔버스 비율이 달라지는 것을 막는다 |
| 프롬프트의 "또는" 제거 | "크림색 **또는** 아이보리", "Serif **또는** Sans Serif" 처럼 선택지를 주면 장마다 다른 선택을 한다 |

---

### 13-6. 결정한 값과 이유
| 상수 | 값 | 왜 |
|---|---|---|
| `DAYS_PER_PAGE` | **2** | 하루 최대 7항목 × 2일 = 14줄. A4 세로 한 장의 한계선이다. 원본 프롬프트도 "2~3일씩" 이라고 했다 |
| `MAX_PAGES` | **4** | 장수만큼 호출·비용이 는다. 10일 여행에 5장을 그리지 않고, 넘치면 마지막 장에 몰아넣는다 |
| `MAX_PARALLEL_PAGES` | **4** | 동시에 4건이면 쿼터를 한꺼번에 태우지 않으면서 벽시계 시간은 1장 수준으로 줄어든다 |

일수별 결과는 이렇다.

| 여행 일수 | 장수 | 각 장 |
|---|---|---|
| 1~2일 | 1장 | 전체 |
| 3~4일 | 2장 | 1-2 / 3-4 |
| 5~6일 | 3장 | 1-2 / 3-4 / 5-6 |
| 7~8일 | 4장 | 1-2 / 3-4 / 5-6 / 7-8 |
| 9일 이상 | 4장 | 1-2 / 3-4 / 5-6 / **7일차~끝** |

### 13-7. 반드시 지킬 것 3가지

#### ① 캐시 키에 페이지 번호가 들어가야 한다

지금은 `trip_id : style : 지문` 이다. `page` 를 넣지 않으면 **모든 장이 같은 키를
쓰고**, 1장을 그린 뒤 2장을 요청하면 캐시에 있는 1장이 그대로 나온다. 4장짜리
일정표가 같은 그림 4장이 된다.

`itinerary_export_image:{trip_id}:{style}:p{page}:{지문}`

지문은 **전체 일정**으로 계산한다. 페이지 내용만으로 계산하면, 3일차를 고쳤을 때
1-2일차 장의 지문이 그대로라 헤더의 기간 표기가 낡은 채로 남는다.

#### ② 한 장이 실패해도 나머지는 보낸다

4장 중 2장만 성공하면 그 2장을 보낸다. 전부 버리면 사용자는 아무것도 못 받는다.
`generate_itinerary_images()` 는 실패한 자리에 `None` 을 남긴 **길이 N 리스트**를
돌려주고, 라우터가 `None` 을 빼면서 **원래 페이지 번호는 유지**한다. 그래야
파일 이름의 `2of4` 가 실제 순서와 맞는다.

#### ③ 모델에게 "다른 장 일자를 끌어오지 말라" 고 말한다

페이지를 나눠 부르면 모델은 자기가 받은 2일치가 전부인 줄 안다. 그런데 헤더에는
전체 기간(`2026-10-10 ~ 2026-10-16`)이 들어가므로, **없는 일자를 지어내 채우는**
일이 생긴다. `[Output]` 과 `{plan}` 머리말 양쪽에서 못박는다.

### 13-8. 비용 — 알고 넘어갈 것

**장수만큼 호출이 늘고 비용도 그만큼 는다.** 8일 여행 1회 다운로드 = 이미지 생성
4회다. 병렬 호출이라 시간은 1장 수준이지만 **요금은 4배**다.

캐시가 이걸 막는다 — 같은 일정을 다시 받으면 4장 모두 캐시에서 나온다(7일 TTL).
일정을 고치면 지문이 바뀌어 다시 4회다. 일정을 자주 고치는 사용자가 다운로드를
반복하면 비용이 빠르게 는다. `MAX_PAGES = 4` 상한이 그 최악을 묶어 두는 장치다.

### 13-9. 적용 후 확인

| # | 확인 | 기대 결과 |
|---|---|---|
| 1 | 2일 여행 다운로드 | **1장**, 버튼 1개 |
| 2 | 4일 여행 다운로드 | **2장**, 버튼 2개, 각각 1-2일차 / 3-4일차 |
| 3 | 각 장을 저장 | `tripmate_오사카_20261010_1of2.png` · `_2of2.png` |
| 4 | 2장의 내용 | **3-4일차만** 있고 1-2일차가 섞여 있지 않을 것 |
| 5 | 첫 생성 소요 시간 | 4일 기준 40초 안팎 (병렬이 도는지) |
| 6 | 같은 요청 재호출 | **즉시** (장별 캐시 적중) |
| 7 | 3일차 일정 하나 수정 후 | **네 장 모두** 다시 그림 (지문이 전체 기준) |
| 8 | 10일 여행 | **4장**, 마지막 장에 7일차~10일차 |

**4번이 핵심이다.** 페이지를 나눠 불렀는데 각 장이 전체 일정을 그리고 있으면
`{plan}` 머리말이나 `[Output]` 수정이 빠진 것이다.

---

# 부록. 코드 위치 지도

**코드 전문은 이 문서에 두지 않는다.** 예전에는 부록 A~D 에 전문을 실었는데,
§13(여러 장 분할)을 적용하고 프롬프트를 확장한 순간 문서의 코드와 저장소의 코드가
갈라졌다 — 문서는 `generate_itinerary_image` 를, 저장소는 `generate_itinerary_images`
를 갖고 있었다. **같은 코드가 두 곳에 살면 반드시 갈라진다.**

그래서 코드는 저장소가 유일한 진실이고, 이 문서는 **왜 그렇게 했는지**(§1~§13)를
맡는다. 아래는 "그 설명이 어느 코드를 말하는지" 찾아가는 지도다.

> 줄 번호는 작성 시점 기준이라 편집하면 밀린다. **이름으로 찾는 것이 확실하다.**

## 백엔드 `2026_aio2_TripMate_Backend`

### `app/services/itinerary_export_prompt.py` — 프롬프트 (951줄)

| 이름 | 줄 | 무엇 | 설명 |
|---|---|---|---|
| `_SHARED_TAIL` | 28 | **두 스타일 공통** `[Data]` + `[Output]` | §3-3 · §13-7 ③ |
| `_SIMPLE_BODY` | 62 | 심플형 | §3-4 |
| `_ILLUSTRATED_BODY` | 813 | 일러스트형 | §3-4 |
| `EXPORT_IMAGE_PROMPTS` | 943 | 몸통 + 꼬리를 합치는 곳 | §3-2 |
| `EXPORT_IMAGE_STYLES` · `DEFAULT_EXPORT_IMAGE_STYLE` | 950 | 라우터·서비스가 함께 보는 목록 | §3-2 |

**고칠 때**: 심플형만 → `_SIMPLE_BODY` / 일러스트형만 → `_ILLUSTRATED_BODY` /
**두 스타일 모두** → `_SHARED_TAIL`.
몸통에 `{ }` 를 넣지 말 것 — `.format()` 이 치환하려다 `KeyError` 가 난다.

### `app/services/itinerary_export.py` — 생성 (390줄)

| 이름 | 줄 | 무엇 | 설명 |
|---|---|---|---|
| `CACHE_TTL_SECONDS` · `MAX_CACHE_BYTES` | 35 · 39 | 캐시 정책 | §4-1 |
| `MAX_ITEMS_PER_DAY` | 43 | 하루당 프롬프트에 싣는 항목 수 | §4-1 |
| **`DAYS_PER_PAGE`** | **47** | **한 장에 담는 일수 (2)** | **§13-5** |
| **`MAX_PAGES`** | **51** | **장수 상한 (4) = 비용 상한** | **§13-5 · §13-7** |
| **`MAX_PARALLEL_PAGES`** | **55** | **동시 호출 수 (4)** | **§13-5** |
| `DEFAULT_IMAGE_MODEL` | 59 | `gemini-3-pro-image` | §6 |
| `LLM_TIMEOUT_MS` | 63 | 120초 (밀리초) | §8-1 ⑫ |
| `_clock_text` | 74 | **UTC → 여행지 현지** | §4-4 ① |
| `_ordered_items` | 92 | 화면과 같은 시간 우선 정렬 | §4-4 ③ |
| `_plan_fingerprint` | 108 | 캐시 지문 (**전체 일정** 기준) | §4-4 ② · §13-7 ① |
| `_cache_key` | 135 | `trip:style:p{page}:지문` | §4-4 ② · §13-7 ① |
| **`_chunk_days`** | **152** | **일정을 페이지로 자름** | **§13-2** |
| `_party_text` · `_period_text` · `_plan_text` | 169 · 177 · 186 | 프롬프트에 넣을 문구 | §4-2 |
| `build_prompt` | 206 | **스타일 분기가 일어나는 유일한 지점** | §4-2 · §13-7 ③ |
| `_first_image` | — | 응답에서 이미지 파트 추출 | §4-2 |
| **`_STYLE_REFERENCE_NOTE`** | **69** | **2장부터 붙이는 톤 통일 지시** | **§13-5** |
| **`_generate_one`** | **269** | **한 장 그리기** (캐시 → 호출 → 1회 재시도) | **§13-2** |
| **`generate_itinerary_images`** | **325** | **페이지 분할 + 병렬 호출** | **§13-2 · §13-7 ②** |
| `itinerary_as_text` | 368 | 텍스트 대체 수단 | §4-2 |

### `app/routers/trips.py` — 엔드포인트 (1756줄)

| 이름 | 줄 | 무엇 | 설명 |
|---|---|---|---|
| **`_itinerary_export_zip`** | **1648** | **장별 PNG → ZIP 한 개** | **§13-3** |
| `export_itinerary_image` | 1696 | `GET /trips/{id}/itinerary/export?style=` | §5-3 · §13-2 |
| `export_itinerary_text` | 1747 | `GET …/export/text` | §5-3 |

import 3줄(`Literal` · `Response` · 새 서비스)은 파일 상단 — §5-2.

## 프론트엔드 `2026_aio2_TripMate_Frontend`

### `common.py` (195줄)

| 이름 | 줄 | 무엇 | 설명 |
|---|---|---|---|
| `api_binary` | 120 | 이진 응답을 **헤더까지** 반환 · `timeout` 지원 | §11-3 |
| `api_bytes` | 151 | `api_binary` 를 감싼 기존 함수 | §11-3 |

### `streamlit_app.py` (4615줄)

| 이름 | 줄 | 무엇 | 설명 |
|---|---|---|---|
| `EXPORT_STYLES` · `EXPORT_TIMEOUT_SECONDS` | `_fetch_export_pages` 위 | 스타일 라벨 · 150초 | §11-5 · §7 ① |
| **`EXPORT_PROGRESS_PHRASES`** | **3686** | **대기 롤링 문구 7개** | **§11-5** |
| `EXPORT_PROGRESS_INTERVAL_SECONDS` | 3698 | 교체 간격 2.8초 | §11-5 |
| `_fetch_export_pages` | 3707 | **메인 스레드** — 캐시 · 헤더 · 문구 갈아 끼우기 | §11-5 |
| **`_download_export_pages`** | **3768** | **워커 스레드** — HTTP + ZIP 풀기만 | **§11-5** |
| `render_export_dialog` | 3806 | 모달 · 카드를 눌러야 생성 · 장별 저장 버튼 | §11-5 · §13-2 |
| `render_dashboard` | — | `[일정표 다운로드]` 버튼 · 모달 호출 | §11-5 |

세션 키 `export_dialog_trip_id` · `export_style` · **`export_requested_style`** ·
`export_images` 는 세션 기본값 dict — §11-4 ② · §11-5.

## 되돌리는 법

§13(여러 장 분할)만 되돌리려면 이렇게 한다.

| 파일 | 되돌릴 것 |
|---|---|
| `itinerary_export.py` | `DAYS_PER_PAGE = 999` 로 두면 항상 1장이 된다. 코드를 지울 필요가 없다 |
| `itinerary_export_prompt.py` | `_SHARED_TAIL` 의 `[Output]` 페이지 문구 |
| 나머지 | 그대로 둬도 동작한다 — 1장짜리 ZIP 이 오고 화면은 `[다운로드]` 하나를 그린다 |

**`DAYS_PER_PAGE` 한 값이 장수를 정한다.** 되돌리기도 조정도 이 한 줄이다.