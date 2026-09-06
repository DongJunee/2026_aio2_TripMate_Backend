# TripMate backend

FastAPI backend for the TripMate travel planner.

## Before running

1. Run the Supabase SQL from this chat, including `trip_days`. Before using
   Google place search and map routes, also run
   `supabase/20260904_google_places_cache.sql` in the Supabase SQL Editor.
2. Put your own values in `.env`. The required values are `SUPABASE_URL`,
   `SUPABASE_ANON_KEY`, and `GEMINI_API_KEY` if you want AI chat.
   The classroom password-reset feature additionally needs
   `SUPABASE_SERVICE_ROLE_KEY` in this backend-only file. Never put that key
   in the frontend `.env` or commit it to Git.
   To use place search and route data, add `GOOGLE_MAPS_API_KEY` here for
   backend calls.
   A Maps Demo Key supports Places API (New) and Compute Routes for local
   prototyping; a standard key needs the corresponding APIs enabled in Google
   Cloud. For this classroom prototype, the interactive frontend map can reuse
   this same key value through its own `GOOGLE_MAPS_API_KEY` secret.
3. Redis is optional. Leave all Redis values blank to run without caching.

## Run

```powershell
uv sync
uv run uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/docs> to test the API.

## Current API scope

- Supabase email/password sign-up and login
- classroom-only password reset after matching the profile name and email
- profile lookup and username editing
- trip creation and automatic `DAY 1` to `DAY N` plus editable AI itinerary-draft generation
- trip pinning with a gap-free sidebar display order
- trip period editing with automatic DAY extension and safe shortening
- itinerary item creation, editing, and deletion
- one chat history per trip
- Gemini travel-planner response streamed to the chat screen when `GEMINI_API_KEY` is configured
- backend-only Google Places (New) search, selected-place persistence, and
  Google Routes path/duration for the interactive browser map

The backend retains an optional Maps Static API image proxy for a standard
billed key, but the Streamlit screen now uses Maps JavaScript API so local
prototyping can use Google Maps Demo Keys without Maps Static API access.

## 자동 일정의 지역·출국일 규칙

- 숙소 입력 없이 선택한 도시 안에서 첫 일정을 생성한다. 도시 조회는 여행 생성당
  한 번이며, 기존 Places API (New) 키를 사용한다. 새 DB 칼럼이나 API 서비스는 필요 없다.
- `app/services/destination_scope.py`는 Google이 반환한 도시·국가·상위 행정구역을
  장소 응답과 비교한다. 도시 조회의 `viewport`는 검색을 좁히는 사각형일 뿐
  정확한 행정구역 경계 다각형이 아니다. 사각형과 주소 정보를 모두 확인하며,
  정보가 없거나 도시가 모호하면 다른 도시로 확대하지 않고 생성 오류를 반환한다.
- 같은 한국어 요청에도 도시 응답은 `후쿠오카시`, 장소의 주소는 `Fukuoka`로
  반환될 수 있다. 이름 비교에서 탈락했을 때만 같은 Google 도시 ID의 영문 주소를
  한 번 더 조회해 검증된 별칭으로 추가한다. 여행 생성당 추가 조회는 최대 한 번이며,
  좌표 범위·국가·도시 유형·필수 주소 검사는 그대로 유지한다.
- '오사카, 일본'처럼 도시를 입력한다. 광역 지역·국가·여러 도시를 도시 대신
  자동 허용하지 않는다. 도시별 주소 구조 차이로 확인하지 못하는 경우도 있다.
- Google 후보 최대 5개 중 지역 검사를 통과한 첫 장소를 쓴다. 하나도 없으면
  여행 생성은 중단된다. 같은 생성 요청의 동일 검색어는 재사용하며 무한 재검색하지 않는다.
  실패 로그에는 검색 후보 수와 `city_name_mismatch`, `outside_viewport` 같은
  거절 사유별 개수가 남는다. API 키나 전체 요청·응답은 기록하지 않는다.
- 일반 날짜는 현지 오전 9시부터 강도에 맞춰 생성한다. 실제 종료일은 강도보다
  18시 출국 가정이 우선하여 관광·점심을 13시까지 배치한다. 이후 13~15시는
  공항 이동 예비 시간, 15~18시는 수속 준비, 18시는 출국 안내다. 1일 여행도 같다.
- 공항·항공편은 아직 선택하지 않으므로 출국 안내에는 가짜 장소·좌표를 붙이지
  않는다. 이동 예비 2시간은 Routes 계산 결과나 도착 보장이 아니며, 실제 예약과
  공항이 정해지면 이동시간·체크인 마감을 다시 확인해야 한다.
- 위 규칙은 새 자동 일정에 적용된다. 기존 여행이나 사용자가 직접 추가한 일정은
  덮어쓰지 않는다. 기간 변경도 기존 항목을 보존하며 출국 일정을 자동 재생성하지 않는다.

Google 공식 문서: [검색 영역 제한](https://developers.google.com/maps/documentation/places/web-service/text-search),
[주소 구성요소와 viewport](https://developers.google.com/maps/documentation/places/web-service/reference/rest/v1/places).

외부 서비스 없이 회귀 테스트:

```powershell
uv run python -m unittest discover -s tests
```
