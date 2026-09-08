# 여행지 검색 · 가고 싶은 장소 검색 적용 계획

> **상태: 검토 대기 (미적용)**
> 대상: `2026_aio2_TripMate_Backend`
> 관련 화면: SCR-004a · F (첫 여행 만들기 — 설명 카드형)
> 작성 기준: 이전 버전(`이전/backend`) 구현 분석 + 현행 코드 대조

---

## 0. 요약

화면 두 기능을 새 백엔드에 붙이기 위한 변경 계획이다.

1. **여행지 검색** — 국가·도시를 검색해서 고르는 입력
2. **가고 싶은 장소 검색** — 위에서 고른 지역 안에서만 장소 검색 (최대 5곳)

새 저장소에는 **필요한 로직이 이미 전부 있다.** 옮겨올 것은 알고리즘이 아니라
**화면이 부를 창구(엔드포인트) 2개**와 **고른 장소를 일정에 반영하는 경로**다.

### 변경 파일 한눈에

| # | 파일                                      | 성격                                     | 규모      | 단계           |
| - | ----------------------------------------- | ---------------------------------------- | --------- | -------------- |
| 1 | `app/services/destination_scope.py`     | 함수 1개 추출 (동작 불변)                | +14 / -12 | 필수           |
| 2 | `app/routers/maps.py`                   | 엔드포인트 2개 + 헬퍼 1개                | +65       | 필수           |
| 3 | `app/schemas.py`                        | `MustVisitPlace` + `TripCreate` 필드 | +18       | 필수           |
| 4 | `app/routers/trips.py`                  | 4줄 수정                                 | +4 / -1   | 필수           |
| 5 | `app/services/itinerary_generation.py`  | 프롬프트 블록 1개                        | +16       | 필수           |
| 6 | `supabase/20260908_trip_must_visit.sql` | 신규 마이그레이션                        | +30       | **선택** |
| 7 | `app/routers/trips.py` (저장 경로)      | 3줄 추가                                 | +3        | **선택** |

프론트엔드 — `2026_aio2_TripMate_Frontend` (전부 `streamlit_app.py` 한 파일)

| #  | 위치                               | 성격                    | 규모     | 단계 |
| -- | ---------------------------------- | ----------------------- | -------- | ---- |
| F1 | 상수 블록                          | `MAX_MUST_VISIT` 추가 | +4       | 필수 |
| F2 | `initialize_session` (L510)      | 세션 기본값 4개         | +12      | 필수 |
| F3 | `open_create_trip_form` (L959)   | 초기화 대상 추가        | +9       | 필수 |
| F4 | 신규`render_destination_picker`  | 여행지 검색 UI          | +64      | 필수 |
| F5 | 신규`render_must_visit_picker`   | 가고 싶은 장소 검색 UI  | +88      | 필수 |
| F6 | `render_create_trip_form` (L973) | 양식 구조 변경          | +24 / -8 | 필수 |

**1~5 + F1~F6 만 적용하면 DB 마이그레이션 없이 동작한다.** 6~7은
"사용자가 무엇을 골랐는지"를 여행에 남기고 싶을 때만 적용한다.

**기존 파일 삭제·이동 없음. `main.py` · `common.py` 수정 없음**
(maps 라우터는 이미 등록되어 있고, `api()` / `auth_headers()`도 그대로 쓴다).

## 1. 변경 상세 (필수 — 1~5)

### 변경 1 — `app/services/destination_scope.py`

**바꾸는 것**: `resolve_destination_scope`(`destination_scope.py:117-139`)를
두 함수로 나눈다.

**변경 사유**: 검색 화면은 후보 **목록**이 필요한데, 현재 함수는 후보가 1개가
아니면 예외를 던지고 목록을 버린다. 목록을 만드는 부분만 떼어내면 검색과 생성이
같은 판정 기준을 공유하게 된다.

```python
def list_destination_candidates(
    maps: GoogleMapsClient, destination: str
) -> list[tuple[PlaceResult, DestinationScope]]:
    """입력한 이름으로 Google이 도시라고 확인한 후보만 중복 없이 돌려준다.

    [변경 사유] 검색 화면은 후보 목록이 필요하고 생성 경로는 하나로 좁혀야 한다.
    두 곳이 각자 목록을 만들면, 검색에서 고를 수 있었던 도시를 생성에서 거절하는
    어긋남이 생긴다. 판정 기준을 한 곳에 두고 좁히는 규칙만 아래에서 따로 건다.

    [변경 사유] 표시에 필요한 원본 응답(PlaceResult)을 함께 돌려준다.
    DestinationScope 에 표시용 칸(주소·나라 원문)을 새로 넣으면 dataclass 의
    동등 비교가 바뀌고, 그 비교는 아래 중복 제거와 with_city_aliases 가 쓰고 있다.
    """
    scopes: dict[str, tuple[PlaceResult, DestinationScope]] = {}
    for candidate in maps.search_city(destination, language_code="ko"):
        try:
            scope = DestinationScope.from_city(candidate)
        except ValueError:
            continue
        # Google ID가 달라도 같은 행정구역과 같은 표시 범위를 가진 별칭 응답은
        # 하나로 취급한다. 이름만 같고 위치·국가·상위 지역이 다르면 합치지 않는다.
        # (기존 resolve_destination_scope 에 있던 규칙을 그대로 옮긴 것이다.)
        if any(
            (scope.city_type, scope.city_names, scope.country_names,
             scope.parent_names, scope.viewport)
            == (existing.city_type, existing.city_names, existing.country_names,
                existing.parent_names, existing.viewport)
            for _, existing in scopes.values()
        ):
            continue
        scopes[scope.google_place_id] = (candidate, scope)
    return list(scopes.values())


def resolve_destination_scope(maps: GoogleMapsClient, destination: str) -> DestinationScope:
    """사용자가 입력한 도시를 한 번 조회하고, 모호하거나 미확인인 도시는 거절한다.

    [변경 사유] 본문을 list_destination_candidates 로 옮겼을 뿐, 시그니처와 예외
    문구는 그대로다. 호출처(maps.py:88 · trips.py:382)와 기존 테스트가 이 함수의
    동작에 의존하고 있으므로 겉보기 동작을 바꾸지 않는다.
    """
    candidates = list_destination_candidates(maps, destination)
    if len(candidates) != 1:
        raise ValueError(
            "여행할 도시 범위를 하나로 확인하지 못했습니다. "
            "'오사카, 일본'처럼 도시와 국가를 입력하세요. 광역 지역이나 여러 도시는 지원하지 않습니다."
        )
    return candidates[0][1]
```

**호환성**: `resolve_destination_scope`의 시그니처·동작·예외 문구가 그대로다.
호출처 2곳과 `tests/test_destination_scope.py` 전부 무수정 통과한다.

> ### ⛔ 적용할 때 가장 많이 틀린 곳
>
> 기존 `resolve_destination_scope`를 주석 처리하고 `list_destination_candidates`만
> 붙이면, **새 `resolve_destination_scope` 본문이 없는 상태**가 된다.
> docstring만 있는 함수는 오류 없이 `None`을 돌려주기 때문에 조용히 망가진다.
>
> - 여행 생성: `AttributeError: 'NoneType' object has no attribute 'viewport'`
> - **장소 검색: 도시 범위 검증이 통째로 꺼져 전 세계 결과가 그대로 나온다** (조용한 실패)
>
> 적용 후 반드시 확인:
>
> ```bash
> .venv/Scripts/python.exe -m unittest tests.test_destination_scope -q   # OK 여야 한다
> ```

---

### 변경 1-B — 광역시를 도시로 인정 (`destination_scope.py`)

**문제**: Google은 **도쿄도·서울특별시를 `locality`가 아니라
`administrative_area_level_1`로** 준다. `_CITY_TYPES`가 `("locality", "postal_town")`
뿐이라 "도쿄"·"서울"은 후보 0건이 되고, 검색은 물론 **여행 생성도 원래부터 실패**했다.

```
검색어    Google 이름     types                          판정
오사카    오사카시        ['locality', 'political']       ✅ 통과
도쿄      도쿄도          ['administrative_area_level_1'] ❌ 거절
서울      서울특별시      ['administrative_area_level_1'] ❌ 거절
```

**왜 크기로 거르지 않는가** (실측값, 2026-09-08):

| 도시로 인정해야 함 | 최대 폭 | 거절해야 함 | 최대 폭 |
| ------------------ | ------- | ----------- | ------- |
| 도쿄도             | 266km   | 경기도      | 154km   |
| 서울특별시         | 37km    | 후쿠오카현  | 139km   |
|                    |         | 오사카부    | 87km    |

**도쿄도가 경기도·후쿠오카현·오사카부보다 크다.** 도쿄를 통과시키는 임계값은 그 셋을
함께 통과시킨다. `primaryType`은 다섯 곳 모두 `null`이고 `addressComponents` 구성도
`[자기이름(admin1), 국가(country)]`로 동일해, 응답만으로 갈라낼 신호가 없다.

**그래서 Google 장소 ID 허용 목록으로 명시한다.** 표시 이름은 언어·표기에 따라
달라지지만 ID는 고정이다.

#### (a) 상수 추가 — `_PARENT_TYPES` 아래

```python
# 광역자치단체 자체가 하나의 도시인 곳.
#
# [변경 사유] 도쿄도·서울특별시는 Google 이 locality 가 아니라
# administrative_area_level_1 로 준다. 그대로 두면 "도쿄" 여행을 아예 만들 수 없다.
#
# [변경 사유] 크기로 거르지 않는다. 도쿄도의 표시 범위는 266km 로 경기도(154km)·
# 후쿠오카현(139km)·오사카부(87km)보다 크다. 도쿄를 통과시키는 임계값은 그 셋을
# 함께 통과시킨다. types·primaryType·addressComponents 구성도 다섯 곳이 모두 같아
# 응답만으로는 갈라낼 신호가 없다.
#
# [변경 사유] 그래서 추측하지 않고 Google 장소 ID 로 명시한다. 표시 이름은 언어와
# 표기에 따라 달라지지만 ID 는 고정이다. 여기 없는 광역구역은 지금처럼 거절된다 —
# 새로 넣을 때는 그 안에서 하루 일정이 성립하는 '도시'인지 확인하고 한 줄 추가한다.
_METROPOLIS_CITY_TYPE = "administrative_area_level_1"
_METROPOLIS_PLACE_IDS = frozenset({
    "ChIJ51cu8IcbXWARiRtXIothAS4",  # 도쿄도 · Tokyo
    "ChIJzzlcLQGifDURm_JbQKHsEX4",  # 서울특별시 · Seoul
})
```

#### (b) `from_city` — `city_type` 판정 뒤에 예외 경로 추가

```python
        city_type = next((kind for kind in _CITY_TYPES if kind in place.types and _names(place, kind)), None)
        if (                                                        # ← 이 블록 추가
            city_type is None
            # [변경 사유] 허용 목록에 있는 광역시만 그 광역구역 이름을 도시 이름으로
            # 삼는다. ID 를 먼저 보므로 오사카부·경기도는 여기 들어오지 못한다.
            and place.google_place_id in _METROPOLIS_PLACE_IDS
            and _METROPOLIS_CITY_TYPE in place.types
            and _names(place, _METROPOLIS_CITY_TYPE)
        ):
            city_type = _METROPOLIS_CITY_TYPE
        countries = _names(place, "country")                        # ← 기존 줄
```

#### (c) `from_city` — `parent_names` 에서 도시 종류 제외

```python
            # [변경 사유] 도시 이름으로 쓴 종류는 상위 지역 검사에서 뺀다. 광역시를
            # 도시로 인정하면 city_type 과 _PARENT_TYPES 가 겹쳐 같은 이름을 두 번
            # 대조하게 된다. locality 도시는 city_type 이 _PARENT_TYPES 에 없으므로
            # 이 조건이 걸리지 않아 기존 동작 그대로다.
            parent_names=tuple(
                (kind, names) for kind in _PARENT_TYPES
                if kind != city_type and (names := _names(place, kind))
            ),
```

**범위 필터는 그대로 동작한다**: 도쿄를 도시로 인정하면 `city_names={"도쿄도"}`가 되고,
`accepts()`는 후보 장소의 `administrative_area_level_1`이 `도쿄도`인지 본다.
요코하마(가나가와현)·사이타마(사이타마현)는 그대로 걸러진다.

**허용 목록에 없는 곳은 계속 거절된다.** 확인된 예: 홍콩(`types=['country']`).
필요해지면 그때 ID를 한 줄 추가한다.

---

### 변경 1-C — 표시용 도시 이름 (`destination_scope.py`)

**문제**: Google이 주는 이름은 행정구역 표기라 화면에 **"도쿄도, 일본"**, **"서울특별시,
대한민국"**으로 뜬다. 고를 때 읽는 이름으로는 어색하다.

**핵심 원칙 — 보내는 값과 보여 주는 값을 나눈다**

| 필드            | 값             | 쓰임                                       |
| --------------- | -------------- | ------------------------------------------ |
| `destination` | "도쿄도, 일본" | **전송용.** 여행 생성이 이 표기로 도시를 다시 찾는다 |
| `label`       | "도쿄"         | **표시용.** 화면에만 쓴다                        |

하나로 합치면 안 된다. 표시 이름을 그대로 보내면 생성 단계의 `search_city` 가
다른 결과를 낼 수 있고, Google 표기를 그대로 보여 주면 읽기 나쁘다.

#### (a) 표시 이름 표 + 헬퍼 — `_METROPOLIS_PLACE_IDS` 아래

```python
# 화면에 보여 줄 도시 이름.
#
# [변경 사유] Google 이 주는 이름은 행정구역 표기라 "도쿄도"·"서울특별시" 다.
# 사용자가 고를 때 읽는 이름으로는 어색하므로 표시용 이름을 따로 둔다.
#
# [변경 사유] 이 이름은 **표시에만** 쓴다. 여행 생성에 보내는 값은 Google 표기
# 그대로여야 도시를 다시 찾을 때 어긋나지 않는다. 두 값을 하나로 합치지 않는
# 이유가 이것이다.
#
# [변경 사유] 허용 목록과 따로 둔다. 표기를 다듬는 일과 도시로 인정하는 일은
# 다른 판단이고, 나중에 "오사카시 → 오사카" 처럼 locality 도시의 표기만 고치고
# 싶을 수 있다. 여기 없는 도시는 Google 이름을 그대로 쓰므로 빠뜨려도 안전하다.
_DISPLAY_LABELS = {
    "ChIJ51cu8IcbXWARiRtXIothAS4": "도쿄",
    "ChIJzzlcLQGifDURm_JbQKHsEX4": "서울",
}


def display_label(place: PlaceResult) -> str:
    """화면에 보여 줄 도시 이름. 정해 둔 표기가 없으면 Google 이름을 그대로 쓴다."""
    return _DISPLAY_LABELS.get(place.google_place_id) or place.display_name
```

동작 확인:

```
도쿄도      -> 도쿄
서울특별시   -> 서울
오사카시     -> 오사카시   (표에 없으면 Google 이름 그대로)
```

#### (b) `maps.py` — import 에 `display_label` 추가

```python
from app.services.destination_scope import (
    DestinationScope,
    # 화면에 보여 줄 도시 이름("도쿄도" 대신 "도쿄")을 서버가 정한다.
    display_label,
    list_destination_candidates,
    resolve_destination_scope,
)
```

#### (c) `maps.py` — 검색 응답에 `label` 한 줄 추가

```python
            "destination": f"{place.display_name}, {country}" if country else place.display_name,
            # 화면에 보여 줄 이름. Google 표기가 "도쿄도"라 그대로 쓰면 어색하다.
            # 위 destination 과 일부러 나눠 둔다 — 보내는 값은 Google 표기여야
            # 생성 단계에서 도시를 다시 찾을 수 있고, 읽는 값은 사람이 쓰는
            # 이름이어야 고르기 쉽다.
            "label": display_label(place),
```

#### (d) 프론트엔드 — 표시 헬퍼 2개 + 사용처 3곳

`render_destination_picker` 바로 앞에 추가한다.

```python
def _city_name(city: dict) -> str:
    """화면에 보여 줄 도시 이름 한 개.

    [변경 사유] 백엔드가 label("도쿄")과 destination("도쿄도, 일본")을 나눠 준다.
    보내는 값은 Google 표기여야 생성 단계에서 도시를 다시 찾을 수 있고, 읽는 값은
    사람이 쓰는 이름이어야 고르기 쉽다. label 이 없는 응답도 그대로 동작하도록
    Google 이름으로 물러선다.
    """
    return str(city.get("label") or city.get("display_name") or "").strip()


def _city_caption(city: dict) -> str:
    """도시 이름에 나라를 붙인 한 줄. 같은 이름의 도시를 나라로 가른다."""
    name = _city_name(city)
    country = str(city.get("country") or "").strip()
    return f"{name}, {country}" if country else name
```

바꿀 곳 3군데:

| 위치                                    | 변경 전                                       | 변경 후                             |
| --------------------------------------- | --------------------------------------------- | ----------------------------------- |
| `render_destination_picker` 선택 칩   | `f"여행지 · {picked['destination']}"`       | `f"여행지 · {_city_caption(picked)}"` |
| `render_destination_picker` 후보 버튼 | `city["destination"]`                       | `_city_caption(city)`             |
| `render_must_visit_picker` placeholder  | `f"{picked_city['display_name']}에서 ..."` | `f"{_city_name(picked_city)}에서 ..."` |

**보내는 값은 바꾸지 않는다.** `render_create_trip_form` 의
`"destination": picked_city["destination"]` 은 그대로 둔다.

#### 남는 한계

여행이 만들어진 뒤 대시보드는 `trip.get("destination")` 을 그대로 그리므로
(`streamlit_app.py:3185`) 거기서는 여전히 **"도쿄도, 일본"** 으로 보인다.
저장된 값 자체가 Google 표기이기 때문이다. 모든 화면에서 "도쿄"로 보이게 하려면
저장하는 값을 바꾸거나 표시용 칼럼을 따로 둬야 하고, 그건 마이그레이션이 필요하다.
지금은 **고르는 화면만** 다듬는 범위로 둔다.

---

### 변경 2 — `app/routers/maps.py`

#### 2-a. import 추가 (`maps.py:36`)

```python
from app.services.destination_scope import (
    DestinationScope,
    # [변경 사유] 여행 만들기 화면이 도시 후보 목록을 받아야 한다.
    # resolve_destination_scope 는 후보가 1개가 아니면 예외라 목록 용도로 못 쓴다.
    list_destination_candidates,
    resolve_destination_scope,
)
```

#### 2-b. 상수 1줄 추가 (`maps.py:51` 아래)

```python
# [변경 사유] 도시 목록은 사람마다 다르지 않고 자주 바뀌지도 않는다.
# 검색어 한 글자마다 유료 Places 호출이 나가지 않도록 도시 범위 캐시와 같은
# 수명(1시간)을 준다.
DESTINATION_SEARCH_CACHE_TTL_SECONDS = 3_600
```

#### 2-c. 표시용 헬퍼 추가 (`maps.py:96` 뒤, `_destination_scope_for_search` 근처)

```python
def _country_name(place) -> str | None:
    """도시 응답에서 나라 이름만 꺼낸다.

    [변경 사유] DestinationScope.country_names 는 비교용이라 casefold 된
    frozenset 이다. 화면에 그대로 쓸 수 없다. formatted_address 에서 나라를
    잘라내는 방법도 쓰지 않는다 — 표기 순서가 언어마다 달라 추측이 된다.
    Google 이 country 로 표시한 구성요소만 읽는다.
    """
    for component in place.address_components:
        if "country" in component.types:
            return component.long_text or component.short_text or None
    return None
```

#### 2-d. [기능 1] 여행지 검색 엔드포인트

**위치**: `maps.py:566` (숙소 검색 엔드포인트 바로 앞)

```python
@router.get("/destinations/search", dependencies=[Depends(get_current_user)])
def search_destinations(query: str = Query(min_length=2, max_length=100)):
    """여행을 만들기 전에 Google이 도시로 확인한 후보만 보여준다.

    [변경 사유] 여행 생성은 도시 범위를 하나로 좁히지 못하면 거절하는데,
    그 판정이 Gemini 호출(최대 180초) 뒤에 일어난다. 여기서 먼저 고르게 하면
    사용자가 생성 시간을 다 기다린 뒤에 422 를 받는 일이 없고, 버려지는
    LLM 호출도 없다.

    [변경 사유] 여행 소유권을 확인할 여행이 아직 없으므로 로그인만 요구한다.
    trip_id 를 요구하는 기존 검색과 달리 여행 만들기 화면에서 부를 수 있어야 한다.
    """
    cleaned = query.strip()
    cache_key = _cache_key("destination_search", {"query": cleaned.casefold(), "v": 1})
    cached = cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            # 형식이 잘못된 선택적 캐시 항목이 실제 검색을 막으면 안 된다.
            pass

    try:
        candidates = list_destination_candidates(_maps_client(), cleaned)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GoogleMapsError as error:
        raise _maps_request_error(error) from error

    destinations = []
    for place, _ in candidates:
        country = _country_name(place)
        destinations.append({
            "google_place_id": place.google_place_id,
            "display_name": place.display_name,
            "country": country,
            "formatted_address": place.formatted_address,
            "latitude": place.coordinates.latitude if place.coordinates else None,
            "longitude": place.coordinates.longitude if place.coordinates else None,
            # [변경 사유] 여행 생성에 그대로 넣을 문자열을 서버가 만든다.
            # 화면이 이름과 나라를 다시 조합하면 표기가 갈리고, 생성 단계의
            # 도시 조회가 후보를 하나로 좁히지 못해 422 가 날 수 있다.
            # 이 표기는 도시 조회 실패 문구가 안내하는 형식과 같다.
            "destination": f"{place.display_name}, {country}" if country else place.display_name,
        })

    response = {"query": cleaned, "destinations": destinations}
    # [변경 사유] 빈 결과는 담지 않는다. 담아 두면 두 가지가 한 시간 동안 고정된다 —
    # 서버를 고쳐 이제 찾을 수 있게 된 도시가 계속 "없음"으로 나오고(실제로
    # 도쿄·서울 허용 목록을 넣은 뒤에도 옛 빈 답이 그대로 나갔다), Google 이
    # 일시적으로 실패해 비어 온 답까지 굳어 버린다.
    # 오타는 사용자가 곧바로 고쳐 다시 치므로 같은 빈 검색이 반복될 일이 적다.
    if destinations:
        cache_set(
            cache_key,
            json.dumps(response, ensure_ascii=False),
            DESTINATION_SEARCH_CACHE_TTL_SECONDS,
        )
    return response
```

**설계 근거 — `destination` 문자열을 서버가 만드는 이유**

화면이 `"도쿄"`만 보내면 생성 단계의 `search_city("도쿄")`가 여러 후보를 돌려줘
422가 날 수 있다. `"도쿄, 일본"` 형식은 `destination_scope.py:126`의 오류 문구가
안내하는 표기이자 `streamlit_app.py:981`의 placeholder(`예: 도쿄, 일본`)와도 같다.
화면은 이 문자열을 **그대로** `POST /me/trips`의 `destination`에 넣으면 된다.

#### 2-e. [기능 2] 여행 생성 전 장소 검색 엔드포인트

**위치**: 2-d 바로 뒤

```python
@router.get("/destinations/places/search", dependencies=[Depends(get_current_user)])
def search_destination_places(
    destination: str = Query(min_length=1, max_length=100),
    query: str = Query(min_length=1, max_length=500),
    max_results: int = Query(default=5, ge=1, le=10),
):
    """여행을 만들기 전에도 고른 여행지 안에서만 장소를 찾는다.

    [변경 사유] 여행 안 검색(/trips/{trip_id}/...)과 같은 _search_response 를 쓴다.
    화면이 필요한 값과 도시 밖 결과를 거르는 기준이 같으므로, 검색 경로를 둘로
    나누면 한쪽만 고치는 어긋남이 생긴다. 다른 점은 소유권을 확인할 여행이
    아직 없다는 것뿐이라 도시 범위 검증만 그대로 적용한다.

    [변경 사유] destination 을 필수로 받는다. 지역 없이 "스타벅스"를 찾으면
    전 세계 결과가 나오고, 그중 무엇을 담아도 이 여행의 일정에 쓸 수 없다.
    화면이 잠금을 빠뜨려도 여기서 422 가 나서 전 세계 검색으로 새지 않는다.
    """
    return _search_response(_maps_client(), query, destination, max_results)
```

`_search_response`(`maps.py:141`)가 이미 전부 처리한다 — `location_bias`가 없으므로
`location_restriction=scope.viewport`가 Google에 직접 전달되고, 응답도
`scope.accepts()`로 재검증된다. **이전 저장소의 문자열 접두 방식보다 엄격하다.**

---

### 변경 3 — `app/schemas.py`

**위치**: `schemas.py:88` (`TripCreate` 정의 바로 앞)에 모델 추가,
`schemas.py:98`(`end_date` 아래)에 필드 추가.

```python
class MustVisitPlace(BaseModel):
    """화면 '가고 싶은 장소'에서 고른 곳 하나이다.

    [변경 사유] 이름만 받지 않는다. 사용자는 검색 결과에서 특정 지점을 골랐고
    그 google_place_id 를 이미 알고 있다. 이름만 넘기면 나중에 다시 검색할 때
    같은 이름의 다른 지점이 잡힐 수 있다 — '스타벅스'가 대표적이다.

    [변경 사유] google_place_id 는 선택으로 둔다. 대화나 자유 입력으로 들어온
    장소명도 같은 칸을 쓰게 해서, 입력 경로마다 모델이 갈라지지 않게 한다.
    """

    name: str = Field(min_length=1, max_length=150)
    google_place_id: str | None = Field(default=None, max_length=255)


class TripCreate(TravelPreferenceFields):
    """새 여행을 만들 때 입력하는 기본 정보이다."""

    title: str = Field(min_length=1, max_length=100)
    destination: str | None = Field(default=None, max_length=100)
    # 화면에서는 입력받지 않는다. 생략하면 AI 초안의 여행지 시간대를 검증해 저장한다.
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    start_date: date | None = None
    end_date: date | None = None
    # [변경 사유] 화면 '가고 싶은 장소'가 보내는 값이다. trips 테이블 칼럼이
    # 아니므로 라우터가 insert 전에 분리한다(변경 4). 상한 5곳은 이전 버전의
    # P-12 규칙과 같다 — 더 넣으면 하루 slot 수를 넘겨 초안이 사용자의 선택을
    # 밀어내게 된다.
    # [변경 사유] default_factory 를 둬서 이 필드를 보내지 않는 기존 요청이
    # 그대로 동작하게 한다.
    must_visit: list[MustVisitPlace] = Field(default_factory=list, max_length=5)
```

---

### 변경 4 — `app/routers/trips.py`

**위치**: `trips.py:748-754`

```python
    client = get_user_client(current_user.token)
    trip_values = payload.model_dump(mode="json")
    # [변경 사유] must_visit 은 trips 테이블에 없는 칼럼이다. trip_values 는
    # 아래(trips.py:776)에서 그대로 insert 되므로, 여기서 빼지 않으면 삽입이
    # 통째로 실패한다. 초안 생성에만 쓰는 값이라 사본으로만 넘긴다.
    must_visit = trip_values.pop("must_visit", [])
    # [변경 사유] 이름만 프롬프트에 넣는다. google_place_id 는 모델이 쓸 값이
    # 아니고, 정확한 지점 반영은 아래 6·7단계(선택)에서 다룬다.
    must_visit_names = [
        text for place in must_visit
        if (text := str(place.get("name") or "").strip())
    ]
    days = _initial_trip_days(payload)

    # Gemini와 Google Places 조회는 trips 행을 만들기 전에 끝낸다. 따라서 둘 중
    # 하나라도 실패하면 사용자의 여행 목록에는 새 여행이 전혀 생기지 않는다.
    try:
        # [변경 사유] trip_values 자체를 오염시키지 않으려고 사본을 만든다.
        # 이 dict 는 프롬프트 입력으로만 흐르고 DB 로는 가지 않는다.
        generated = generate_daily_itinerary_drafts(
            {**trip_values, "must_visit": must_visit_names}, days
        )
```

**중요**: `trip_values`는 `trips.py:776`에서 `client.table("trips").insert(trip_values)`로
그대로 들어간다. `pop`으로 먼저 빼내야 컬럼 없음 오류가 나지 않는다.

---

### 변경 5 — `app/services/itinerary_generation.py`

**위치 A**: `itinerary_generation.py:179` (`destination = ...` 아래)

```python
    # [변경 사유] 사용자가 직접 고른 장소는 모델이 추천한 장소보다 우선한다.
    # 이 목록이 비면 프롬프트에 빈 줄이 들어가지 않도록 문자열 자체를 비운다 —
    # 조건 없는 안내문은 모델이 없는 제약을 지어내는 원인이 된다.
    # [변경 사유] trip.get 으로 읽으므로, 이 키가 없는 다른 호출 경로
    # (DB 에서 읽은 여행 행 등)는 그대로 None 을 받아 영향이 없다.
    must_visit = [
        text for name in trip.get("must_visit") or []
        if (text := str(name).strip())
    ]
    must_visit_instruction = (
        "\n사용자가 직접 고른 '가고 싶은 장소': " + ", ".join(must_visit) + "\n"
        "이 장소들을 어울리는 slot 의 place_query 로 먼저 배치하세요. "
        "여행 도시 밖이거나 slot 종류(식당/활동)와 맞지 않으면 넣지 말고 다른 장소로 채우세요. "
        "이 목록 때문에 칸을 추가하거나 시간을 바꾸지 마세요."
        if must_visit else ""
    )
```

**위치 B**: `itinerary_generation.py:224` (`여행지: {destination}` 줄)

```python
여행지: {destination}{must_visit_instruction}
{timezone_instruction}
```

**변경 사유 (프롬프트 배치)**: 여행지 바로 뒤에 둔다. 도시 제한 문장
(`선택한 여행 도시 안에 있는 장소만 추천하세요`)보다 **앞**에 오면 안 된다 —
사용자가 고른 장소가 도시 밖일 때 어느 규칙이 이기는지가 모호해진다.
현재 위치는 도시 제한 문장보다 위지만, 지시문 안에 "여행 도시 밖이면 넣지 말라"를
명시해 충돌을 없앴다.

---

## 4. 변경 상세 (선택 — 6~7: 고른 장소를 여행에 저장)

> 1~5만 적용해도 기능은 동작한다. 다만 고른 장소는 **일정 항목으로만 남고**
> "사용자가 무엇을 골랐는지"는 여행에 남지 않는다. 여행 수정·재생성에서
> 다시 쓰려면 아래를 적용한다.

### 변경 6 — `supabase/20260908_trip_must_visit.sql` (신규)

```sql
-- 여행마다 사용자가 고른 '가고 싶은 장소'를 남긴다.
--
-- 설계 메모
-- - 이름과 Google 장소 ID를 함께 남긴다. 이름만 남기면 재생성 때 같은 이름의
--   다른 지점이 잡힐 수 있다.
-- - places 테이블을 참조하는 외래 키로 두지 않는다. 이 목록은 '사용자의 선택'
--   기록이고, places 는 Google 응답 캐시라 수명이 다르다. 캐시 행이 정리되어도
--   사용자의 선택은 남아야 한다.
-- - 별도 테이블 대신 jsonb 칼럼을 쓴다. 최대 5개이고 항상 여행과 함께 읽으며,
--   이 목록만 따로 조회하거나 조인할 화면이 없다.
-- - 이 스크립트는 반복 실행할 수 있다.

begin;

alter table public.trips
    add column if not exists must_visit jsonb not null default '[]'::jsonb;

-- 배열이 아닌 값이나 5개 초과가 들어오면 재생성 프롬프트가 조용히 망가진다.
-- 기존 행에는 기본값 '[]' 가 들어가므로 검증을 바로 켤 수 있다.
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.trips'::regclass
           and conname = 'tripmate_trips_must_visit_check'
    ) then
        alter table public.trips
            add constraint tripmate_trips_must_visit_check
            check (
                jsonb_typeof(must_visit) = 'array'
                and jsonb_array_length(must_visit) <= 5
            );
    end if;
end
$$;

commit;
```

### 변경 7 — `app/routers/trips.py` (저장 경로)

변경 4에서 `pop` 한 값을 **다시 넣는다.** 위치는 `pop` 직후가 아니라
`trip_values["timezone"] = generated.timezone`(`trips.py:766`) 근처다.

```python
    trip_values["timezone"] = generated.timezone
    # [변경 사유] 마이그레이션 20260908_trip_must_visit.sql 적용 후에만 켠다.
    # 초안 생성이 성공한 뒤에 넣는 이유: 생성이 실패하면 여행 자체가 만들어지지
    # 않으므로, 실패 경로에서 이 값이 어디에 남는지 신경 쓸 필요가 없다.
    trip_values["must_visit"] = must_visit
```

`TripUpdate`(`schemas.py:110`)에도 같은 필드를 더하면 여행 수정에서 목록을 바꿀 수
있다. 다만 **수정만으로 일정이 다시 만들어지지는 않는다** — 재생성 트리거는 이
계획의 범위 밖이다.

---

## 5. 프론트엔드 변경 상세 (F1~F6)

대상: `2026_aio2_TripMate_Frontend/streamlit_app.py` (한 파일)
`common.py`는 수정하지 않는다 — `api()` / `auth_headers()` / `ApiError`를 그대로 쓴다.

### 5.0 전체 흐름

```
[여행지 칸]      GET /destinations/search?query=도쿄
                 → destinations[].destination = "도쿄, 일본"
                 → 고른 값을 세션에 보관 (칩 1개로 제한)

[가고싶은 장소]  GET /destinations/places/search
                     ?destination=도쿄, 일본&query=도톤보리&max_results=5
                 → display_name / formatted_address / google_rating 표시
                 → { name, google_place_id } 를 최대 5개까지 보관

[여행 만들기]    POST /me/trips
                 {
                   "title": "봄날의 도쿄 여행",
                   "destination": "도쿄, 일본",
                   "start_date": "2026-10-10",
                   "end_date": "2026-10-13",
                   "travel_party": "couple",
                   "travel_intensity": 3,
                   "budget_level": 3,
                   "must_visit": [
                     {"name": "도톤보리", "google_place_id": "ChIJ..."},
                     {"name": "우에노 공원", "google_place_id": "ChIJ..."}
                   ]
                 }
```

### 5.1 ⚠️ 구조 변경이 필요한 이유 — `st.form` 제약

현재 여행지 입력은 `st.form("create_trip")` **안에** 있다(`streamlit_app.py:980`).

**`st.form` 안의 위젯은 제출 버튼을 누르기 전까지 재실행을 일으키지 않는다.**
검색은 입력·버튼마다 결과가 바뀌어야 하므로 **양식 안에 둘 수 없다.**
또 `st.form`은 제출 버튼을 하나만 허용하므로 [검색] 버튼도 넣을 수 없다.

그래서 여행지와 가고 싶은 장소는 **양식 위(밖)로 꺼내고**, 고른 값은 세션에
보관한다. 제목·기간·인원·강도·예산은 양식 안에 그대로 둔다.

```
변경 전                          변경 후
┌─ st.form ──────────────┐      여행지 검색      ← 양식 밖 (F4)
│ 여행 이름              │      가고 싶은 장소   ← 양식 밖 (F5)
│ 여행지        ← 자유입력│      ┌─ st.form ──────────────┐
│ 여행 기간              │      │ 여행 이름              │
│ 인원 구성              │      │ 여행 기간              │
│ 강도 · 예산            │      │ 인원 구성              │
│ [여행 만들기]          │      │ 강도 · 예산            │
└────────────────────────┘      │ [여행 만들기]          │
                                └────────────────────────┘
```

---

### F1 — 상수 추가

**위치**: `streamlit_app.py` 상단 상수 블록 (`TRAVEL_PARTY_LABELS` 근처)

```python
# [변경 사유] 백엔드 TripCreate.must_visit 의 max_length=5 와 같은 값이다.
# 화면에서 먼저 막지 않으면 사용자가 6번째를 고른 뒤 여행 만들기에서 422 를
# 받는다 — 고르는 순간에 알려 주는 편이 낫다.
MAX_MUST_VISIT = 5
```

---

### F2 — `initialize_session()` 세션 기본값

**위치**: `streamlit_app.py:510` `defaults` 딕셔너리 안,
`"dashboard_day_windows": {},` 뒤

```python
        # [변경 사유] 여행 만들기 화면에서 고른 여행지와 '가고 싶은 장소'다.
        # 여행이 아직 없어 백엔드에 저장할 곳이 없으므로, POST /me/trips 에
        # 실을 때까지만 화면이 들고 있는다.
        # [변경 사유] 검색 결과를 None(아직 검색 안 함)과 [](결과 없음)로
        # 구분한다. 둘을 같은 []로 두면 화면을 열자마자 "찾지 못했어요"가 뜬다.
        "create_trip_destination": None,
        "create_trip_destination_results": None,
        "create_trip_must_visit": [],
        "create_trip_place_results": None,
```

---

### F3 — `open_create_trip_form()` 초기화 대상 추가

**위치**: `streamlit_app.py:959`, `st.session_state.show_create_trip = True` 앞

```python
        # [변경 사유] 위 for 문은 위젯 키(create_trip_title 등)만 지운다.
        # 고른 여행지와 장소는 위젯이 아니라 우리가 만든 세션 값이라 따로 지워야
        # 한다. 안 지우면 이전에 만들다 만 여행의 선택이 새 양식에 남는다.
        st.session_state.create_trip_destination = None
        st.session_state.create_trip_destination_results = None
        st.session_state.create_trip_must_visit = []
        st.session_state.create_trip_place_results = None
```

---

### F4 — `render_destination_picker()` 신규

**위치**: `render_create_trip_form` 바로 앞 (`streamlit_app.py:972` 근처)

```python
def render_destination_picker(form_key: str) -> None:
    """여행지를 검색해서 고르게 한다.

    [변경 사유] st.form 밖에 둔다. 양식 안의 위젯은 제출 전까지 재실행을
    일으키지 않아 검색 결과를 그릴 수 없고, 양식은 제출 버튼도 하나만 허용한다.

    [변경 사유] 자유 입력을 받지 않는다. 백엔드는 도시 범위를 하나로 좁히지
    못하면 여행 생성을 거절하는데, 그 판정이 Gemini 호출 뒤에 일어난다.
    여기서 확인된 도시만 고르게 하면 생성 시간을 다 기다린 뒤 422 를 받는 일이 없다.
    """

    picked = st.session_state.create_trip_destination
    if picked:
        chip_column, clear_column = st.columns([4, 1])
        with chip_column:
            st.success(f"여행지 · {picked['destination']}")
        with clear_column:
            if st.button("변경", key=f"{form_key}_destination_clear", use_container_width=True):
                st.session_state.create_trip_destination = None
                st.session_state.create_trip_destination_results = None
                # [변경 사유] 여행지를 바꾸면 그 지역에서 고른 장소는 뜻을 잃는다.
                # 남겨 두면 도쿄 여행에 오사카 장소가 딸려 가고, 백엔드는 도시
                # 밖 장소를 거절하므로 사용자는 이유 없이 빠진 일정을 보게 된다.
                st.session_state.create_trip_must_visit = []
                st.session_state.create_trip_place_results = None
                st.rerun()
        return

    search_column, button_column = st.columns([4, 1])
    with search_column:
        query = st.text_input(
            "여행지",
            placeholder="예: 도쿄",
            key=f"{form_key}_destination_query",
            label_visibility="collapsed",
        )
    with button_column:
        searched = st.button(
            "검색", key=f"{form_key}_destination_search", use_container_width=True
        )

    if searched:
        # [변경 사유] 서버도 min_length=2 다. 여기서 먼저 막아 한 글자마다
        # 유료 Places 호출이 나가지 않게 한다.
        if len(query.strip()) < 2:
            st.warning("도시 이름을 두 글자 이상 입력하세요.")
        else:
            try:
                with st.spinner("도시를 찾고 있어요..."):
                    found = api(
                        "GET",
                        "/destinations/search",
                        params={"query": query.strip()},
                        headers=auth_headers(),
                    )
            except ApiError as error:
                st.error(str(error))
            else:
                st.session_state.create_trip_destination_results = found.get("destinations") or []
                st.rerun()

    results = st.session_state.create_trip_destination_results
    if results is None:
        return
    if not results:
        st.info("도시를 찾지 못했어요. 나라나 넓은 지역 대신 도시 이름을 입력해 보세요.")
        return

    st.caption("여행할 도시를 고르세요. 한 곳만 선택할 수 있어요.")
    for city in results:
        # [변경 사유] 라벨에 destination(=이름, 나라)을 그대로 쓴다. 같은 이름의
        # 도시가 여러 나라에 있을 때 나라가 없으면 무엇을 고르는지 알 수 없다.
        if st.button(
            city["destination"],
            key=f"{form_key}_destination_pick_{city['google_place_id']}",
            use_container_width=True,
        ):
            st.session_state.create_trip_destination = city
            st.session_state.create_trip_destination_results = None
            st.rerun()
```

---

### F5 — `render_must_visit_picker()` 신규

**위치**: F4 바로 뒤

```python
def render_must_visit_picker(form_key: str) -> None:
    """고른 여행지 안에서만 '가고 싶은 장소'를 찾아 최대 5곳까지 담는다.

    [변경 사유] 여행지를 고르기 전에는 검색창을 잠근다. 지역 없이 '스타벅스'를
    찾으면 전 세계 결과가 나오고, 그중 무엇을 담아도 이 여행의 일정에 쓸 수 없다.
    백엔드도 destination 을 필수로 받지만, 화면에서 막아야 이유를 설명할 수 있다.

    [변경 사유] 검색은 선택 사항이다. 아무것도 담지 않아도 여행은 만들어진다 —
    여기서 막으면 장소를 아직 모르는 사용자가 여행을 시작할 수 없다.
    """

    picked_city = st.session_state.create_trip_destination
    if not picked_city:
        st.caption("여행지를 먼저 고르면 그 지역에서 찾아드려요.")
        return

    chosen = st.session_state.create_trip_must_visit
    if len(chosen) >= MAX_MUST_VISIT:
        st.caption(f"가고 싶은 장소는 {MAX_MUST_VISIT}곳까지 담을 수 있어요.")
    else:
        search_column, button_column = st.columns([4, 1])
        with search_column:
            query = st.text_input(
                "가고 싶은 장소",
                placeholder=f"{picked_city['display_name']}에서 가고 싶은 곳",
                key=f"{form_key}_must_visit_query",
                label_visibility="collapsed",
            )
        with button_column:
            searched = st.button(
                "검색", key=f"{form_key}_must_visit_search", use_container_width=True
            )
        if searched:
            if not query.strip():
                st.warning("찾고 싶은 장소 이름을 입력하세요.")
            else:
                try:
                    with st.spinner("Google Places에서 장소를 찾고 있어요..."):
                        found = api(
                            "GET",
                            "/destinations/places/search",
                            params={
                                # [변경 사유] 검색 화면이 만든 문자열을 그대로 보낸다.
                                # 화면이 이름과 나라를 다시 조합하면 백엔드가 도시를
                                # 다시 못 찾을 수 있다.
                                "destination": picked_city["destination"],
                                "query": query.strip(),
                                "max_results": 5,
                            },
                            headers=auth_headers(),
                        )
                except ApiError as error:
                    st.error(str(error))
                else:
                    st.session_state.create_trip_place_results = found.get("places") or []
                    st.rerun()

    results = st.session_state.create_trip_place_results
    if results is not None and not results:
        st.caption("찾지 못했어요. 장소는 여행을 만든 뒤 대화에서 말해 주셔도 돼요.")

    picked_ids = {place["google_place_id"] for place in chosen}
    for place in results or []:
        place_id = str(place.get("google_place_id") or "").strip()
        if not place_id or place_id in picked_ids:
            continue
        with st.container(border=True):
            # [변경 사유] 이름만 쓰면 '스타벅스' 다섯 줄이 나란히 서서 어느
            # 지점인지 알 수 없다. 백엔드가 주소와 평점을 이미 주고 있다.
            st.markdown(f"**{escape(str(place.get('display_name') or '이름 없는 장소'))}**")
            st.caption(
                f"{place.get('formatted_address') or '주소 정보 없음'} · {_place_rating_text(place)}"
            )
            if len(chosen) < MAX_MUST_VISIT and st.button(
                "담기", key=f"{form_key}_must_visit_add_{place_id}", use_container_width=True
            ):
                # [변경 사유] google_place_id 를 함께 담는다. 이름만 보내면
                # 같은 이름의 다른 지점이 잡힐 수 있다.
                chosen.append({
                    "name": place.get("display_name") or "",
                    "google_place_id": place_id,
                })
                st.session_state.create_trip_place_results = None
                st.rerun()

    for index, place in enumerate(chosen):
        name_column, drop_column = st.columns([4, 1])
        with name_column:
            st.markdown(f"· {escape(str(place['name']))}")
        with drop_column:
            if st.button(
                "빼기", key=f"{form_key}_must_visit_drop_{index}", use_container_width=True
            ):
                st.session_state.create_trip_must_visit = [
                    item for position, item in enumerate(chosen) if position != index
                ]
                st.rerun()
```

---

### F6 — `render_create_trip_form()` 수정

**위치**: `streamlit_app.py:973`

#### (a) 양식 **앞에** 두 선택 영역을 그린다

> ### ⛔ 여기서 가장 많이 틀린다
>
> 아래 코드에서 `with st.form(...)` 줄은 **원래 있던 줄**이다.
> 위치를 보여 주려고 적어 둔 것이므로 **새로 추가하면 안 된다.**
>
> 이 줄을 함께 붙여 넣으면 같은 `form_key`로 `st.form`이 **두 개**가 되고,
> 새 코드가 양식 **안**으로 들어간다. 그러면
>
> - `DuplicateWidgetID` 오류로 화면이 죽고,
> - 죽지 않더라도 검색 버튼이 아예 동작하지 않는다 (5.1의 제약 그대로).
>
> 적용 후 `grep -c "st.form(form_key" streamlit_app.py` 가 **1** 이어야 한다.

`ADD`로 표시한 줄만 새로 넣는다. 나머지는 이미 있는 줄이다.

```python
     def render_create_trip_form(form_key: str) -> None:                      # 기존
         """여행과 첫 AI 일정 초안을 만드는 양식을 그리고 제출한다."""              # 기존
ADD      # [변경 사유] 검색은 st.form 밖에서만 동작한다. 양식 안의 위젯은 제출 전까지
ADD      # 재실행을 일으키지 않아 검색 결과를 그릴 수 없고, 양식은 제출 버튼도 하나만
ADD      # 허용한다. 순서도 의미가 있다 — 장소 검색은 여행지가 정해져야 열린다.
ADD      st.markdown("##### 어디로 가시나요")
ADD      render_destination_picker(form_key)
ADD      st.markdown("##### 가고 싶은 장소 (선택)")
ADD      render_must_visit_picker(form_key)
ADD      st.divider()
ADD
         # 제출 직후에는 입력을 초기화하지 않고 API 완료 후에만 대시보드로 이동한다.  # 기존
         with st.form(form_key, clear_on_submit=False):                       # 기존 ← 건드리지 않는다
             title = st.text_input(                                           # 기존
```

적용 결과는 다음과 같아야 한다. **새 8줄은 전부 4칸 들여쓰기**이고,
`with st.form` 아래 줄들만 8칸이다.

```python
def render_create_trip_form(form_key: str) -> None:
    """여행과 첫 AI 일정 초안을 만드는 양식을 그리고 제출한다."""
    # [변경 사유] 검색은 st.form 밖에서만 동작한다. 양식 안의 위젯은 제출 전까지
    # 재실행을 일으키지 않아 검색 결과를 그릴 수 없고, 양식은 제출 버튼도 하나만
    # 허용한다. 순서도 의미가 있다 — 장소 검색은 여행지가 정해져야 열린다.
    st.markdown("##### 어디로 가시나요")
    render_destination_picker(form_key)
    st.markdown("##### 가고 싶은 장소 (선택)")
    render_must_visit_picker(form_key)
    st.divider()

    # 제출 직후에는 입력을 초기화하지 않고 API 완료 후에만 대시보드로 이동한다.
    with st.form(form_key, clear_on_submit=False):
        title = st.text_input(
            "여행 이름", placeholder="예: 봄날의 도쿄 여행", key=f"{form_key}_title"
        )
```

#### (b) 양식 안의 여행지 입력을 **삭제**한다

아래 4줄을 지운다. 새로 넣는 코드가 아니다 —
양식 밖의 `render_destination_picker`가 대신한다.

```python
        # ↓ 이 4줄을 삭제
        destination = st.text_input(
            "여행지", placeholder="예: 도쿄, 일본", key=f"{form_key}_destination"
        )
```

지우지 않으면 `st.form` 안에 쓰이지 않는 여행지 칸이 남아 화면에 두 번 보인다.

#### (c) 제출 검증

```python
    if not submitted:
        return
    # [변경 사유] destination 변수가 없어졌다. 고른 도시는 세션에 있다.
    picked_city = st.session_state.create_trip_destination
    if not title.strip() or not picked_city:
        st.error("여행 이름을 입력하고 여행지를 골라 주세요.")
        return
```

#### (d) 요청 본문

```python
                json={
                    "title": title.strip(),
                    # [변경 사유] 검색 결과가 준 문자열을 그대로 보낸다.
                    # 백엔드가 이 표기로 도시를 다시 찾으므로 화면에서 가공하지 않는다.
                    "destination": picked_city["destination"],
                    # 현지 시간대는 백엔드가 여행지를 기준으로 결정한다.
                    "start_date": selected_dates[0].isoformat(),
                    "end_date": selected_dates[1].isoformat(),
                    "travel_party": travel_party,
                    "travel_intensity": travel_intensity,
                    "budget_level": budget_level,
                    # [변경 사유] 비어 있어도 그대로 보낸다. 백엔드는
                    # default_factory=list 라 빈 배열을 정상으로 받는다.
                    "must_visit": st.session_state.create_trip_must_visit,
                },
```

#### (e) 성공 후 정리

```python
    st.session_state.selected_trip_id = created["trip"]["id"]
    st.session_state.show_create_trip = False
    # [변경 사유] 위젯이 아닌 세션 값이라 show_create_trip 을 내려도 남는다.
    # 안 지우면 다음에 여행을 만들 때 지난번 선택이 그대로 보인다.
    st.session_state.create_trip_destination = None
    st.session_state.create_trip_destination_results = None
    st.session_state.create_trip_must_visit = []
    st.session_state.create_trip_place_results = None
    request_main_scroll_to_top()
```

---

### 5.2 프론트엔드 확인 순서

`render_create_trip_form`은 두 곳에서 불린다 — `streamlit_app.py:3843`(사이드바)과
`:3851`(첫 여행). **두 경로 모두 확인해야 한다.** 세션 키는 공유하고 위젯 키만
`form_key`로 나뉘므로, 한쪽에서 고른 뒤 다른 쪽을 열면 F3의 초기화가 도는지 본다.

먼저 코드 상태를 기계적으로 확인한다. 화면을 띄우기 전에 두 줄이면 끝난다.

```bash
cd 2026_aio2_TripMate_Frontend

# ① 문법 — IndentationError 등을 여기서 잡는다
.venv/Scripts/python.exe -m py_compile streamlit_app.py

# ② st.form 이 하나인지 — 2가 나오면 F6-a 를 잘못 붙인 것이다
grep -c "st.form(form_key" streamlit_app.py
```

그다음 화면에서 확인한다.

- [ ] 여행지 미선택 상태에서 장소 검색창이 잠기는가
- [ ] "도쿄" 검색 → 후보 목록 → 하나 고르면 칩으로 바뀌는가
- [ ] [변경]을 누르면 담아 둔 장소까지 함께 비워지는가
- [ ] 장소를 5개 담으면 검색창이 사라지고 안내가 뜨는가
- [ ] 장소 0개로도 여행이 만들어지는가
- [ ] 여행 생성 성공 후 다시 [새 여행]을 열면 이전 선택이 비어 있는가
- [ ] 사이드바 양식과 첫 여행 양식 둘 다 위 항목이 같은가

---

## 6. 응답 형식

### `GET /destinations/search`

```json
{
  "query": "도쿄",
  "destinations": [
    {
      "google_place_id": "ChIJ...",
      "display_name": "도쿄",
      "country": "일본",
      "formatted_address": "일본 도쿄도",
      "latitude": 35.6762,
      "longitude": 139.6503,
      "destination": "도쿄도, 일본",
      "label": "도쿄"
    }
  ]
}
```

- 후보가 없으면 `destinations: []` (200). 나라 이름이나 광역 지역을 넣으면 비어 있을 수 있다.
- Google 요청 자체가 실패하면 502, 입력이 도시로 해석 불가하면 422.

### `GET /destinations/places/search`

기존 `_search_response`와 **동일한 형식**이다.

```json
{
  "query": "도톤보리",
  "resolved_query": "도톤보리 오사카, 일본",
  "places": [
    {
      "provider_place_id": "ChIJ...",
      "google_place_id": "ChIJ...",
      "display_name": "도톤보리",
      "formatted_address": "일본 오사카부 오사카시...",
      "latitude": 34.6687,
      "longitude": 135.5013,
      "google_rating": 4.4,
      "google_rating_count": 12345,
      "primary_type": "tourist_attraction",
      "types": ["tourist_attraction", "point_of_interest"],
      "google_maps_uri": "https://maps.google.com/..."
    }
  ]
}
```

---

## 7. 검증 방법

```bash
# 기존 테스트가 모두 통과해야 한다 (변경 1이 회귀를 일으키지 않았는지)
uv run pytest tests/ -q

# 특히 이 둘
uv run pytest tests/test_destination_scope.py tests/test_travel_preferences.py -q
```

기존 테스트 중 `generate_daily_itinerary_drafts`의 호출 인자를 검증하는 것은 없고
(`test_travel_preferences.py:67`은 `assert_not_called`만 확인), `trips.insert`
payload 키를 검증하는 것도 없어 변경 3·4로 깨지는 테스트는 없다.

### 수동 확인

```bash
# 토큰은 로그인 응답의 access_token
TOKEN="..."

# ① 여행지 검색
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/destinations/search?query=오사카"

# ② 지역 내 장소 검색
curl -H "Authorization: Bearer $TOKEN" \
  --get "http://localhost:8000/destinations/places/search" \
  --data-urlencode "destination=오사카, 일본" \
  --data-urlencode "query=도톤보리"

# ③ 지역 한정이 실제로 걸리는지 — 교토 장소는 결과에서 빠져야 한다
curl -H "Authorization: Bearer $TOKEN" \
  --get "http://localhost:8000/destinations/places/search" \
  --data-urlencode "destination=오사카, 일본" \
  --data-urlencode "query=금각사"

# ④ 지역 없이 부르면 422 여야 한다
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/destinations/places/search?query=스타벅스"
```

③이 빈 배열이 아니라 교토 장소를 돌려주면 `DestinationScope.accepts`가 동작하지
않는 것이므로, 변경 1의 리팩터링을 먼저 확인한다.

### 프론트엔드 확인

백엔드를 먼저 띄운 뒤 확인한다. 5.2의 7개 항목을 순서대로 본다.

```bash
# 터미널 1 — 백엔드
cd 2026_aio2_TripMate_Backend && uv run uvicorn app.main:app --reload

# 터미널 2 — 프론트엔드
cd 2026_aio2_TripMate_Frontend && uv run streamlit run streamlit_app.py
```

증상별 원인:

| 증상                                 | 확인할 곳                                            |
| ------------------------------------ | ---------------------------------------------------- |
| `IndentationError: unexpected indent` | F6-a 새 8줄은 전부 **4칸**이다. 한 줄만 5칸이어도 걸린다 |
| `DuplicateWidgetID`                  | `st.form`이 2개다. F6-a의 `with st.form(...)`은 기존 줄이라 새로 추가하면 안 된다 |
| [검색]을 눌러도 아무 일이 없다       | 검색 UI가 아직`st.form` 안에 있다 (F6-a)           |
| 여행지 칸이 두 번 보인다             | F6-b 삭제를 안 했다                                  |
| 여행지를 골라도 칩이 안 생긴다       | F2의 세션 기본값 누락                                |
| 새 여행을 열면 지난 선택이 남아 있다 | F3 또는 F6-e 누락                                    |
| 여행 만들기에서 422                  | 보낸`destination`이 검색 결과의 값과 다르다 (F6-d) |

---

## 8. 검토 시 판단이 필요한 지점

1. **다중 여행지**
   설계서는 칩 2개, 백엔드는 단일 도시만 지원. 이 계획은 단일 유지 전제다.
   (2장 참조)
2. **`must_visit` 저장 여부**
   6·7단계를 적용할지. 적용하지 않으면 고른 장소는 일정 항목으로만 남고,
   여행 수정·재생성 때 다시 쓸 수 없다.
3. **고른 장소의 좌표 정확도**
   현 계획은 `google_place_id`를 받아 두지만 **프롬프트에는 이름만 넘긴다.**
   Gemini가 그 이름을 `place_query`로 내면 `_resolve_initial_itinerary_places`가
   다시 검색하므로, 이론상 같은 이름의 다른 지점이 잡힐 수 있다.
   정확히 하려면 `_resolve_initial_itinerary_places`의 `resolved_by_query` 캐시
   (`trips.py:396`)를 고른 장소로 미리 채워 두는 방법이 있다. 다만 검색어와
   장소명을 맞추는 규칙이 필요해 이 계획의 범위 밖으로 뒀다.
4. **요청 제한 없음**
   새 엔드포인트 2개는 로그인만 하면 호출 가능하고, 기존 검색과 달리 여행 소유
   확인이 없다. 유료 Google API를 부르므로 rate limit을 둘지 판단이 필요하다
   (현 저장소에는 rate limit 장치가 없다).

---

## 9. 적용 중 발생한 문제와 수정 이력

2026-09-08 실제 적용에서 나온 문제와 그 수정이다. 같은 실수를 다시 하지 않기 위해
증상 → 원인 → 수정 순서로 남긴다.

---

### 9-1. `resolve_destination_scope` 본문 누락 (백엔드 · 치명적)

**증상**

- 여행 생성: `AttributeError: 'NoneType' object has no attribute 'viewport'`
  (`trips.py` 의 `destination_scope.viewport`)
- 장소 검색: **오류 없이** 도시 밖 결과가 섞여 나옴
- 단위 테스트: `ValueError not raised` 2건 + `AttributeError` 6건

**원인**

변경 1을 적용할 때 기존 `resolve_destination_scope` 를 주석 처리하고
`list_destination_candidates` 만 추가한 결과, 새 `resolve_destination_scope` 가
**docstring 만 있고 본문이 없는 상태**가 됐다.

```python
def resolve_destination_scope(maps: GoogleMapsClient, destination: str) -> DestinationScope:
    """사용자가 입력한 도시를 한 번 조회하고 모호하거나 미확인인 도시는 거절한다."""
    # ← 본문 없음. 파이썬은 오류 없이 None 을 돌려준다.
```

이게 특히 위험한 이유는 **조용히 망가진다**는 점이다. 여행 생성은 예외로 드러나지만,
장소 검색 경로는 `_destination_scope_for_search` 가 `None` 을 받아 그대로 반환하고,
`_search_response` 는 `scope` 가 `None` 이면

- `location_restriction` 을 Google 에 보내지 않고,
- `_places_inside_destination` 이 후보를 **필터 없이 그대로 통과**시킨다.

즉 도시 범위 검증이 통째로 꺼진 채 전 세계 결과가 나오는데, 화면에는 오류가 없다.

**수정**

`resolve_destination_scope` 본문을 채웠다 (변경 1의 코드 그대로).

**재발 방지**

변경 1 절에 ⛔ 경고 블록을 넣었다. 적용 직후 아래를 반드시 확인한다.

```bash
.venv/Scripts/python.exe -m unittest tests.test_destination_scope -q   # OK 여야 한다
```

---

### 9-2. 도쿄·서울 검색 결과 0건 (백엔드)

**증상**: 여행지 검색에서 "도쿄" 를 넣으면 "도시를 찾지 못했어요" 가 뜬다.

**원인**: Google 이 도쿄도·서울특별시를 `locality` 가 아니라
`administrative_area_level_1` 로 준다. 검색 엔드포인트가 만든 문제가 아니라
**적용 전부터 여행 생성도 "도쿄" 로는 실패**하던 상태였다. 검색 엔드포인트 덕분에
Gemini 호출 전에 드러났을 뿐이다.

**수정**: 변경 1-B (Google 장소 ID 허용 목록). 크기 임계값이 왜 불가능한지는
변경 1-B 의 실측표 참조.

---

### 9-3. `st.form` 이 두 개가 됨 (프론트엔드)

**증상**: `IndentationError: unexpected indent` (`streamlit_app.py:1180`)

**원인**: F6-a 를 적용할 때 **위치 표시용으로 적어 둔
`with st.form(form_key, clear_on_submit=False):` 줄까지 함께 붙여 넣어**
같은 `form_key` 로 폼이 두 개가 됐다. 새 코드가 폼 **안**으로 들어가
들여쓰기도 한 칸 어긋났다.

들여쓰기만 고쳤어도 `DuplicateWidgetID` 로 죽고, 죽지 않았더라도 검색 버튼이
동작하지 않았을 것이다 (5.1의 제약 그대로).

**수정**: 첫 번째 `with st.form(...)` 줄을 지우고, 그 안에 있던 5줄을
함수 본문 들여쓰기(4칸)로 내렸다.

**재발 방지**: F6-a 를 `ADD` 표기 방식으로 바꾸고 ⛔ 경고와 적용 후 완성본을 넣었다.
적용 직후 아래 두 줄을 확인한다.

```bash
.venv/Scripts/python.exe -m py_compile streamlit_app.py   # 오류 없어야 한다
grep -c "st.form(form_key" streamlit_app.py               # 1 이어야 한다
```

---

### 9-4. 고친 뒤에도 "도시를 찾지 못했어요" 가 계속 뜸 (백엔드 · 캐시)

**증상**: 9-2를 고쳐 도쿄·서울을 허용 목록에 넣었는데도 검색 결과가 계속 0건.
캐시가 없는 "오사카" 는 정상.

**원인**: `search_destinations` 가 **빈 결과까지 1시간 캐시**하고 있었다.
고치기 전에 검색한 "서울"·"도쿄" 의 `{"destinations": []}` 가 Redis 에 남아,
새 코드가 아예 실행되지 않았다.

```
서울   캐시 있음 · 결과 0건 [] · 남은 TTL 2146초
도쿄   캐시 있음 · 결과 0건 [] · 남은 TTL 2116초
오사카  캐시 없음                        ← 그래서 오사카만 정상이었다
```

**수정**

1. 남아 있던 빈 캐시를 지웠다 (`destination_search:*` 중 결과 0건인 항목).
2. **빈 결과는 캐시하지 않도록** 바꿨다 (변경 2-d 의 `if destinations:`).

**남겨 두는 것**: 장소 검색(`_search_response`, `maps.py:228`)도 빈 결과를
캐시한다. 다만 TTL 이 300초라 영향이 짧고, 여행 안 검색과 공유하는 함수라
이 계획의 범위 밖으로 둔다. 필요해지면 같은 방식으로 바꾼다.

**진단이 필요할 때** (읽기 전용):

```bash
PYTHONPATH="." .venv/Scripts/python.exe -c "
import json
from dotenv import load_dotenv; load_dotenv('.env')
from app.redis_client import get_redis
client = get_redis()
for key in client.scan_iter(match='destination_search:*'):
    body = json.loads(client.get(key) or '{}')
    print(body.get('query'), len(body.get('destinations') or []), client.ttl(key))
"
```

---

### 9-5. 남은 사항 (이 계획과 무관)

- `tests/test_dashboard_route_plan.py::test_weather_outside_ten_days_does_not_call_google`
  가 `_weather_for_day() takes 3 positional arguments but 4 were given` 로 실패한다.
  `git diff` 상 이 계획이 건드리지 않은 날씨 코드이며, **적용 전부터 실패하던
  테스트**다. 나머지 91건은 통과한다.
- 진단 과정에서 Google Places 호출을 많이 써 **HTTP 429** 가 났다. 잠시 뒤 풀린다.
  429 상태에서 검색하면 502 가 뜨는데 코드 문제가 아니다.

---

## 부록 A — 이전 저장소 구현 요약 (참고용)

새 저장소에 **그대로 옮기지 않는다.** 왜 옮기지 않는지 판단 근거로만 둔다.

| 항목        | 이전 구현                                                                                       | 새 저장소 대응                                                 |
| ----------- | ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| 여행지 검색 | `GET /api/context/regions` — `q` 2자 미만이면 하드코딩 인기 도시 8개, 이상이면 Places 검색 | 인기 목록은 미도입. 필요하면 별도 논의                         |
| 국가 정보   | `POPULAR_REGIONS` 상수에만 존재. 검색 결과의 `country`는 항상 `null`                      | `addressComponents`에서 실제 국가명 추출                     |
| 장소 검색   | `GET /api/context/places` — `textQuery = f"{near} {text}"`                                 | `locationRestriction.rectangle` + `scope.accepts()` 재검증 |
| 캐시        | Redis(24h) →`places` 테이블(24h) → API                                                      | Redis(5분, 검색) / Redis(24h, 상세). 테이블 폴백 없음          |
| 쿼터        | `guard.take_quota("places", 5000)`                                                            | 없음 (8장 4번 참조)                                            |
| 저장 형태   | `Constraints.regions: list[str]`, `mustVisit: list[MustVisitPlace]`                         | `trips.destination: str`, `trips.must_visit: jsonb` (선택) |

### 이전 구현에서 발견된 문제 (새 저장소에 옮기면 안 되는 것)

- `_from_table(name, near)` — `near` 인자를 받지만 쿼리에 쓰지 않아, API 실패 시
  다른 지역 동명 장소가 나올 수 있었다.
- 테이블 폴백이 `limit(1)` — 목록 UI인데 폴백 시 후보가 하나로 줄었다.
- 검색 결과의 `country`가 항상 `null` — "국가로 검색"이 사실상 미구현이었다.

---

## 부록 B — 적용 체크리스트

아래 순서로 진행하면 중간 상태에서도 서버가 뜬다.

### 필수

- [ ] `app/services/destination_scope.py` — `list_destination_candidates` 추가,
  `resolve_destination_scope` **재작성 (본문까지 반드시 채울 것)**
- [ ] `app/services/destination_scope.py` — 변경 1-B (a)(b)(c) 광역시 허용 목록
- [ ] 변경 1-C (a)(b)(c) 표시용 이름 `label` / (d) 프론트 표시 헬퍼
- [ ] `uv run pytest tests/test_destination_scope.py -q` (여기서 한 번 확인)
- [ ] `app/routers/maps.py` — import / 상수 / `_country_name` / 엔드포인트 2개
- [ ] `uv run uvicorn app.main:app --reload` 후 `/docs`에서 엔드포인트 2개 확인
- [ ] 7장 수동 확인 ①~④
- [ ] `app/schemas.py` — `MustVisitPlace` + `TripCreate.must_visit`
- [ ] `app/routers/trips.py` — `pop` + `must_visit_names` + 생성 호출 인자
- [ ] `app/services/itinerary_generation.py` — 프롬프트 블록
- [ ] `uv run pytest tests/ -q` (전체 확인)

### 필수 — 프론트엔드 (백엔드가 뜬 뒤에 진행)

- [ ] F1 `MAX_MUST_VISIT = 5` 상수
- [ ] F2 `initialize_session` 세션 기본값 4개
- [ ] F3 `open_create_trip_form` 초기화 4줄
- [ ] F4 `render_destination_picker` 추가
- [ ] F5 `render_must_visit_picker` 추가
- [ ] F6 `render_create_trip_form` — (a) 양식 앞 호출 · (b) 여행지 입력 삭제 ·
  (c) 검증 · (d) 요청 본문 · (e) 성공 후 정리 **5곳 모두**
- [ ] **F6-a는 `with st.form(...)` 줄을 새로 추가하지 않았는지 확인**
  (F6-a 상단 ⛔ 경고 참조)
- [ ] `.venv/Scripts/python.exe -m py_compile streamlit_app.py` → 오류 없음
- [ ] `grep -c "st.form(form_key" streamlit_app.py` → **1**
- [ ] 5.2 프론트엔드 확인 7개 항목 (사이드바 · 첫 여행 양식 둘 다)

### 선택 (고른 장소 저장)

- [ ] Supabase SQL Editor에서 `supabase/20260908_trip_must_visit.sql` 실행
- [ ] `app/routers/trips.py` — `trip_values["must_visit"] = must_visit`
- [ ] 여행 생성 후 `GET /trips/{trip_id}/dashboard`에서 `must_visit` 확인
