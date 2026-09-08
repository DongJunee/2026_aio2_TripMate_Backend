# AI 일정 장소 지역 검증 실패(422) 원인 분석과 수정

> **상태: 적용 완료**
> 대상: `2026_aio2_TripMate_Backend`
> 발생 경로: `POST /me/trips` (첫 여행 만들기)
> 작성 기준: 2026-09-08 실패 로그 2건 + Google Places 실측 응답

---

## 0. 요약

여행 만들기에서 두 번의 422가 났고, **로그 메시지는 같았지만 원인은 서로 달랐다.**

```
# 1건: 오사카 (가고 싶은 장소 비움)
AI 일정 장소 지역 검증 실패: 후보 5개, 사유 {'administrative_name_mismatch': 3, 'city_name_mismatch': 2}

# 2건: 나트랑
AI 일정 장소 지역 검증 실패: 후보 10개, 사유 {'administrative_name_mismatch': 10}
```

| | 1건 (오사카) | 2건 (나트랑) |
| --- | --- | --- |
| 원인 | AI가 **실제로 도시 밖** 장소를 추천 | 검증 로직의 **오탐** — 같은 성인데 발음부호 표기가 달랐음 |
| 성격 | 검증이 제 일을 한 것 | 버그 |
| 대응 | 예방(프롬프트) + 여지 확대(후보 수) | 비교 로직 수정 |

두 원인을 구분하는 데 코드 역추적이 필요했다는 점 자체가 문제라, **진단 로그**도 함께 넣었다.

### 변경 파일 한눈에

| # | 파일 | 성격 | 단계 |
| - | ---- | ---- | ---- |
| 1 | `app/services/destination_scope.py` | 발음부호 표기 차이 흡수 (`_comparable_forms`) | **버그 수정** |
| 2 | `app/routers/trips.py` | 검색 후보 수 5 → 10 (상수로 분리) | 완화 |
| 3 | `app/routers/trips.py` | 실패 진단 DEBUG 로그 + 요약 헬퍼 | 운영 |
| 4 | `app/services/itinerary_generation.py` | 목록이 빌 때만 붙는 도시 앵커 지시문 | 예방 |
| 5 | `tests/test_destination_scope.py` | 회귀 테스트 6개 + 상수 참조 | 필수 |
| 6 | `tests/test_travel_preferences.py` | 상수 참조 | 필수 |

**하지 않은 것**: 도시 별칭 사전 병합(§2.4), 실패 정책 완화(§6).

---

## 1. 실패가 난 경로

여행 생성은 `trips` 행을 만들기 **전에** AI 초안과 Google 검증을 모두 끝낸다.
덕분에 실패해도 사용자 목록에 빈 여행이 남지 않지만, 대신 **여행 자체가 만들어지지 않는다.**

```
POST /me/trips
  └ create_my_trip                        app/routers/trips.py
      ├ generate_daily_itinerary_drafts    → slot 마다 place_query 생성 (Gemini)
      └ _resolve_initial_itinerary_places
          ├ resolve_destination_scope       → 여행 도시 1곳 확정 (Google 1회)
          └ 검색어마다:
              ├ maps.search_places(...)     → 도시 viewport 안에서 후보 검색
              ├ scope.accepts(후보)         → 도시·국가·상위 행정구역 이름 재검증
              └ 통과 후보 0개 → 422 (전체 중단)   ← 여기
```

핵심은 **2단 검증**이라는 점이다.

1. **1차 — Google 검색 범위 제한**: `location_restriction=destination_scope.viewport`
   viewport 는 실제 행정 경계가 아니라 **사각형**이다. 오사카시 사각형에는 사카이시·교토 일부가 들어온다.
2. **2차 — 주소 구성요소 대조**: `DestinationScope.rejection_reason()`
   좌표가 사각형 안이어도 `locality` / `administrative_area_level_1` 이름이 다르면 거절한다.

즉 **1차를 통과하고 2차에서 떨어지는 후보가 정상적으로 존재한다.** 두 실패 모두 `outside_viewport` 가 0개였던 이유다.

`rejection_reason` 의 검사 순서도 진단에 중요하다.

```
좌표 없음 → 사각형 밖 → 도시 없음 → 도시 이름 불일치 → 나라 없음 → 나라 불일치 → 상위 지역 없음 → 상위 지역 불일치
                                    (city_name_mismatch)                              (administrative_name_mismatch)
```

**도시 이름을 상위 지역보다 먼저 본다.** 그래서 `administrative_name_mismatch` 가 나왔다는 것은
**도시 이름은 이미 통과했다**는 뜻이다. 나트랑 건이 10개 전부 이 사유였다는 점이 원인 판별의 출발점이었다.

---

## 2. 원인 판별

### 2.1 나트랑 — 성(省) 이름의 발음부호 차이 (버그)

실제 Google 응답을 받아 대조했다.

| 응답 | `administrative_area_level_1` |
| ---- | ----------------------------- |
| 도시 조회 (`languageCode=ko`) | `Khanh Hoa` — 발음부호 없는 ASCII |
| 도시 조회 (`languageCode=en`) | `Khanh Hoa` — 마찬가지 |
| 장소 후보 일부 | `Khánh Hòa` — 베트남어 원표기 |

같은 검색어의 후보 5개 실측 (수정 전):

```
사유=None                          Bai Tranh Beach    admin='Khanh Hoa'
사유=None                          레이비치            admin='Khanh Hoa'
사유=administrative_name_mismatch  Robinson Beach     admin='Khánh Hòa'        ← 오탐
사유=administrative_name_mismatch  탑쩜흐엉            admin='Khánh Hòa'        ← 오탐
사유=city_name_mismatch            혼총 곶            locality='Bac NHA Trang' ← 정상 거절 (북나트랑)
```

`_names()` 는 NFKC 정규화와 casefold 만 하고 **발음부호는 그대로 둔다.**
그래서 `khánh hòa` 와 `khanh hoa` 가 서로 다른 지역으로 판정됐다.

**언어별 별칭 보완으로 덮이지 않는 이유**: 기존 보완 장치는 같은 도시 ID의 `en` 주소를 가져와 병합한다.
그런데 나트랑은 `en` 응답도 `Khanh Hoa`(ASCII) 라서, 병합해도 `Khánh Hòa` 를 커버하지 못한다.

### 2.2 오사카 — 실제로 도시 밖 (버그 아님)

이쪽은 사유가 `administrative_name_mismatch` 3 + `city_name_mismatch` 2 로 섞여 있었다.
`city_name_mismatch` 가 섞였다는 것은 **도시 이름 자체가 다른 후보**가 있었다는 뜻이고,
`must_visit` 가 빈 상태에서 모델이 근교 도시(교토·나라·고베)를 집어넣는 알려진 패턴과 맞는다.

### 2.3 "요청당 1회 제한" 때문은 아니다

`city_aliases_checked` 는 요청당 1회지만,

- 별칭 병합이 **성공하면** 결과가 `destination_scope` 에 영구 반영되어 이후 검색어가 모두 혜택을 본다.
- 별칭 병합이 **실패하면** 그 자리에서 502로 끝난다.
- 검증 실패는 **즉시 422로 중단**된다.

세 경우 모두, "앞선 검색어가 1회를 소진해서 이번 검색어가 손해를 본다"는 상황이 성립하지 않는다.
또한 로그의 사유 집계는 **병합이 끝난 스코프**로 계산된다.

```python
place = next((item for item in candidates if destination_scope.accepts(item)), None)
if place is None and not city_aliases_checked and any(...mismatch...):
    city_aliases_checked = True
    destination_scope = destination_scope.with_city_aliases(city_aliases)   # ← 병합
    place = next((item for item in candidates if destination_scope.accepts(item)), None)
if place is None:
    rejected = Counter(destination_scope.rejection_reason(item) for item in candidates)   # ← 병합된 스코프
```

즉 두 실패 모두 **영문 별칭 보완이 이미 돌고 난 뒤의 결과**다.

### 2.4 그래서 하지 않은 수정

> **도시 스코프 생성 시점에 영문 별칭을 미리 병합하는 안** — 채택하지 않음.

§2.3 에 따라 같은 `get_city_details` 호출을 앞으로 당길 뿐 판정 결과가 바뀌지 않는다.
나트랑은 `en` 응답도 ASCII 라 애초에 별칭으로 풀 수 없는 문제였고(§2.1), 오사카는 진짜 도시 밖이었다(§2.2).
API 호출만 늘고 얻는 것이 없다.

---

## 3. 수정 1 — 발음부호 표기 차이 흡수 (버그 수정)

**파일**: `app/services/destination_scope.py` — `_comparable_forms` 신설, `_names` 가 이를 사용

### 변경 전

```python
def _names(place: PlaceResult, component_type: str) -> frozenset[str]:
    return frozenset(
        normalized
        for component in place.address_components
        if component_type in component.types
        for value in (component.long_text, component.short_text)
        if (normalized := " ".join(unicodedata.normalize("NFKC", value).casefold().split()))
    )
```

### 변경 후

```python
def _comparable_forms(value: str) -> tuple[str, ...]:
    """이름 하나를 대조 가능한 표기들로 편다. 번역은 하지 않는다.

    [변경 사유] 같은 베트남 성인데 Google 이 도시 조회에는 'Khanh Hoa' 를,
    일부 장소 응답에는 'Khánh Hòa' 를 준다. NFKC 와 casefold 는 발음부호를
    남기므로 두 값이 다른 지역으로 판정돼 나트랑 여행 생성이 통째로 막혔다.
    도시 조회는 ko·en 둘 다 ASCII 표기를 주기 때문에 언어별 별칭 보완으로도
    덮이지 않는다.

    [변경 사유] 발음부호를 떼어 낸 표기는 **원본을 대체하지 않고 함께** 담는다.
    정확히 일치하던 이름은 그대로 일치하고, 표기 차이만 추가로 흡수한다.

    [변경 사유] 발음부호를 뗀 결과가 ASCII 일 때만 더한다. 라틴 문자 밖에서는
    같은 처리가 뜻을 바꾼다 — 가나의 탁점을 떼면 'が' 가 'か' 가 되어 서로 다른
    이름이 같은 이름이 된다. 한자·한글 표기는 애초에 영향을 받지 않는다.
    """
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not normalized:
        return ()
    folded = "".join(
        char for char in unicodedata.normalize("NFD", normalized)
        if not unicodedata.combining(char)
    )
    if folded != normalized and folded.isascii():
        return (normalized, folded)
    return (normalized,)


def _names(place: PlaceResult, component_type: str) -> frozenset[str]:
    """Google의 같은 종류 주소 구성요소에서 긴 이름·짧은 이름만 비교한다."""
    return frozenset(
        form
        for component in place.address_components
        if component_type in component.types
        for value in (component.long_text, component.short_text)
        for form in _comparable_forms(value)
    )
```

### 설계 근거

**원본을 대체하지 않고 함께 담는다.**
`Khánh Hòa` → `{'khánh hòa', 'khanh hoa'}`, `Khanh Hoa` → `{'khanh hoa'}` 가 되어 교집합이 생긴다.
기존에 정확히 일치하던 이름은 하나도 영향을 받지 않는다.

**ASCII 결과일 때만 더한다.**
발음부호 제거는 라틴 문자 밖에서 뜻을 바꾼다.

| 입력 | NFD 후 결합문자 제거 | ASCII? | 결과 |
| ---- | -------------------- | ------ | ---- |
| `Khánh Hòa` | `khanh hoa` | 예 | 추가 ✓ |
| `が` | `か` | 아니오 | **추가 안 함** — 'がっこう' 와 'かっこう' 가 같아지는 사고 방지 |
| `오사카시` | 변화 없음 | — | 해당 없음 |
| `大阪府` | 변화 없음 | — | 해당 없음 |

**검증이 느슨해지지 않는다.**
같은 이름의 표기 차이만 흡수한다. 좌표·나라·도시·상위 지역을 모두 확인하는 구조는 그대로이고,
실측된 북나트랑(`Bac Nha Trang`)은 수정 후에도 계속 `city_name_mismatch` 로 거절된다.

### 수정 후 실측

```
사유=None  Bai Tranh Beach    admin='Khanh Hoa'
사유=None  레이비치            admin='Khanh Hoa'
사유=None  Robinson Beach     admin='Khánh Hòa'        ← 통과로 전환
사유=None  탑쩜흐엉            admin='Khánh Hòa'        ← 통과로 전환
사유=city_name_mismatch  혼총 곶  locality='Bac NHA Trang' ← 여전히 거절 (의도대로)
```

---

## 4. 수정 2 — 검색 후보 수 5 → 10

**파일**: `app/routers/trips.py:366` (상수), `app/routers/trips.py:460-471` (호출)

2차 검증에서 인접 도시 후보가 빠지는 만큼 목록에 여유가 필요하다. 후보가 5개면 도시 안 장소가
아예 목록에 들지 못해 여행 생성이 통째로 막힌다.

```python
# 검색어 하나당 Google 에 요청할 후보 수. 도시 검증에서 인접 도시 후보가 빠지는
# 만큼 여유를 둔다. Places 텍스트 검색의 상한은 20이다.
_ITINERARY_PLACE_CANDIDATES = 10
```

```python
        try:
            # [변경 사유] Google 의 도시 범위는 사각형이라 인접 도시 장소가 함께
            # 돌아온다. 아래 도시 검증에서 그런 후보를 걸러내므로, 후보가 적으면
            # 도시 안 장소가 목록에 들지 못해 여행 생성 전체가 막힌다. 후보 수를
            # 늘려도 Places 텍스트 검색은 요청 단위로 과금되어 호출 수는 그대로다.
            candidates = maps.search_places(
                full_query,
                max_results=_ITINERARY_PLACE_CANDIDATES,   # 이전: 5
                language_code="ko",
                location_restriction=destination_scope.viewport,
                include_region_metadata=True,
            )
```

**비용**: 없음. Places 텍스트 검색은 **요청 단위**로 과금되고 결과 수는 과금 대상이 아니다.
호출 횟수와 필드 마스크(SKU)는 그대로다.

**안전성**: 검증 기준은 완화되지 않았다. 후보가 늘어나도 §1 의 2차 검증을 통과해야 저장된다.

---

## 5. 수정 3 — 실패 원인 진단 로그

**파일**: `app/routers/trips.py:369-390` (헬퍼), `app/routers/trips.py:501-515` (로그)

기존 WARNING 은 사유별 **개수만** 남겨서, "모델이 다른 도시를 추천했다"(§2.2)와
"주소 표기가 어긋났다"(§2.1)를 구분할 수 없었다. 이번 분석에도 코드를 역추적하고
Google 응답을 직접 받아 봐야 했다. 개수 로그는 그대로 두고 DEBUG 를 추가한다.

```python
def _candidate_origin(scope, place) -> dict[str, str]:
    """검증에 떨어진 후보가 어느 도시 소속인지만 짧게 요약한다.

    Google 응답 전체를 남기지 않으려고 판정에 쓰인 주소 구성요소 이름과 사유만
    모은다. 좌표·평점·장소 ID 같은 나머지 필드는 기록하지 않는다.
    """

    def component(kind: str) -> str:
        return next(
            (
                part.long_text or part.short_text
                for part in place.address_components
                if kind in part.types
            ),
            "",
        )

    return {
        "name": place.display_name,
        "city": component(scope.city_type),
        "area": component("administrative_area_level_1"),
        "reason": scope.rejection_reason(place) or "",
    }
```

```python
        if place is None:
            # 인증값·사용자 질문·전체 응답은 기록하지 않고 실패 원인별 개수만 남긴다.
            rejected = Counter(destination_scope.rejection_reason(item) for item in candidates)
            LOGGER.warning("AI 일정 장소 지역 검증 실패: 후보 %d개, 사유 %s", len(candidates), dict(rejected))
            # [변경 사유] 개수만으로는 '모델이 다른 도시를 추천했다' 와 '주소 표기가
            # 어긋났다' 를 구분할 수 없어 원인 확인이 불가능했다. 검색어와 후보의
            # 도시·상위 지역 이름만 DEBUG 로 남긴다. 인증값과 Google 전체 응답은
            # 그대로 기록하지 않는다.
            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug(
                    "AI 일정 장소 지역 검증 실패 상세: 검색어=%r 도시=%r 후보=%s",
                    full_query,
                    destination_scope.display_name,
                    [_candidate_origin(destination_scope, item) for item in candidates],
                )
```

**기록 범위**: 검색어 · 도시 이름 · 후보별 `이름/도시/상위지역/사유` 뿐이다.
API 키, Google 전체 응답, 좌표, 사용자 입력 원문은 기존 방침대로 남기지 않는다.

**출력 예시**

```
AI 일정 장소 지역 검증 실패 상세: 검색어='기요미즈데라 오사카' 도시='Osaka' 후보=[
  {'name': '기요미즈데라', 'city': 'Kyoto', 'area': 'Kyoto Prefecture', 'reason': 'city_name_mismatch'}, ...]
```

이 한 줄이면 §2.1 과 §2.2 가 바로 갈린다.

| 로그에 보이는 것 | 결론 | 다음 조치 |
| ---------------- | ---- | --------- |
| `city` 가 실제로 다른 도시 이름 | 모델이 도시 밖을 추천 | §6 정책 완화 검토 |
| `city`/`area` 가 같은 지역인데 표기만 다름 | 비교 로직 오탐 | §3 과 같은 정규화 보강 |

### DEBUG 로그 켜는 법

```python
import logging
logging.getLogger("app.routers.trips").setLevel(logging.DEBUG)
```

또는 앱 전역 로깅 설정에서 `app.routers.trips` 로거 레벨을 `DEBUG` 로 내린다.

---

## 6. 수정 4 — "가고 싶은 장소"가 빌 때만 붙는 도시 앵커

**파일**: `app/services/itinerary_generation.py:197-209` (지시문), `:283` (삽입 위치)

§2.2 예방책이다. 목록이 비면 기존 `must_visit_instruction` 이 빈 문자열이 되어
프롬프트에 앵커가 하나도 남지 않는다. 그 자리를 대신할 **형식 제약**을 건다.

```python
    # [변경 사유] '가고 싶은 장소'가 비면 위 안내문이 통째로 빠지고, 모델은 모든
    # 검색어를 스스로 지어낸다. 그때 유명 관광지 위주로 뽑으면서 근교 도시로 새는
    # 일이 생기고(오사카 -> 교토·나라·고베), 서버의 도시 검증에서 후보가 전부
    # 떨어지면 여행 생성이 통째로 실패한다. 앵커가 없는 이 경우에만 검색어 형식을
    # 제약해 도시를 벗어날 여지를 줄인다. 도시 제한 자체는 아래 본문에 이미 있으며
    # 이 문장은 그 제한을 형식으로 한 번 더 묶는 것이다.
    city_anchor_instruction = (
        "\n"
        f'모든 place_query 는 도시 이름 "{destination}" 으로 시작하고, 그 뒤에 '
        "그 도시 안의 구역이나 랜드마크 이름을 붙이세요. 다른 도시 이름이나 "
        "그 도시에 없는 장소 이름은 검색어에 넣지 마세요."
        if not must_visit else ""
    )
```

삽입 위치 (프롬프트 본문):

```
모든 item은 Google Places 텍스트 검색으로 실제 장소를 찾기 위한 후보여야 합니다.{city_anchor_instruction}
`place_query`에는 장소 종류와 지역을 포함한 구체적인 검색어를 넣으세요. 예를 들어
```

### 설계 근거

- **목록이 있을 때는 넣지 않는다.** 기존 안내문이 이미 앵커 역할을 하고, 지시문이 겹치면 서로 간섭한다.
- **줄바꿈을 값 안에 둔다.** 빈 문자열일 때 프롬프트에 빈 줄이 생기지 않는다.
  기존 `must_visit_instruction` 과 같은 방식 — 조건 없는 안내문은 모델이 없는 제약을 지어내는 원인이 된다.
- **도시 제한 자체는 새로 만든 것이 아니다.** 프롬프트 본문에 이미 "선택한 여행 도시 안에 있는 장소만
  추천하세요"가 있다. 이 문장은 그 의미 제약을 **검색어 형식 제약으로 한 번 더 묶는 것**이다.

---

## 7. 테스트

### 7.1 추가한 회귀 테스트

**발음부호 처리** — `tests/test_destination_scope.py` · `DiacriticNotationTests`

| 테스트 | 확인 내용 |
| ------ | --------- |
| `test_same_area_with_vietnamese_diacritics_is_accepted` | `Khanh Hoa` 도시 + `Khánh Hòa` 장소가 통과한다 (실측 재현) |
| `test_diacritics_on_both_sides_still_match` | 반대 방향(도시에 발음부호, 장소에 ASCII)도 통과한다 |
| `test_different_city_in_same_area_is_still_rejected` | 실측된 북나트랑은 계속 `city_name_mismatch` 로 거절된다 |
| `test_kana_voicing_marks_are_not_folded_away` | `がっこう` / `かっこう` 가 같아지지 않는다 (라틴 밖 미적용 확인) |

**진단 로그** — `tests/test_destination_scope.py`

이 로그는 **후보가 전멸했을 때만** 실행되는 코드다. 여기서 예외가 나면 422로 끝났을 요청이 500이 된다.
평소에 안 도는 경로일수록 테스트가 필요하다.

| 테스트 | 확인 내용 |
| ------ | --------- |
| `test_rejected_candidates_are_summarised_for_debug_logging` | 헬퍼가 교토 후보에 대해 `city` / `area` / `reason` 을 올바로 뽑는다 |
| `test_debug_logging_runs_when_every_candidate_is_rejected` | 실제 실패 경로에서 DEBUG 로그가 나오고, 상태 코드가 422로 유지되며, 장소 저장이 일어나지 않는다 |

### 7.2 기존 테스트 수정

후보 수를 하드코딩 대신 상수로 참조하도록 바꿔 다시 어긋나지 않게 했다.

```python
# tests/test_destination_scope.py, tests/test_travel_preferences.py
"카페 오사카", max_results=trips._ITINERARY_PLACE_CANDIDATES, language_code="ko",
```

### 7.3 실행 결과

```
$ .venv/Scripts/python.exe -m unittest tests.test_destination_scope tests.test_travel_preferences \
    tests.test_itinerary_local_time tests.test_itinerary_routing tests.test_trip_timezone_storage \
    tests.test_dashboard_api tests.test_dashboard_route_plan tests.test_console_api tests.test_request_logging

Ran 98 tests in 0.210s
OK
```

프롬프트 분기와 나트랑 실측은 별도로 확인했다.

- 목록이 비면 앵커 문장이 들어가고, 목록이 있으면 들어가지 않는다. 어느 쪽도 연속 빈 줄을 만들지 않는다.
- 실제 Google 응답으로 나트랑 후보 5개를 재검증해 §3 의 전/후 표를 얻었다.

---

## 8. 남은 것 — 실패 정책

**현행 유지**: 검색어 하나가 도시 검증에 전멸하면 여행 생성 전체가 422로 실패한다.
"다른 도시 장소로 대체하지 않는다"는 기존 원칙을 그대로 두었다.

§3(오탐 제거)과 §6(예방) 이후에도 실패가 이어지면, DEBUG 로그를 켜고 재현한 뒤 아래 중 하나를 고르면 된다.

| 안 | 동작 | 장점 | 비용 |
| -- | ---- | ---- | ---- |
| **A. 실패 slot 비우고 생성** | 막힌 칸을 `item_type="note"` "장소 미정"으로 저장하고 진행 | 추가 API 호출 없음, 생성 실패가 사라짐 | 일정에 빈 칸이 생김 |
| **B. 대체 검색어 1회 재시도** | 도시명을 앞세운 일반 검색어로 한 번 더 검색 | 일정이 채워짐 | 실패 건수만큼 Places 호출 증가, 추천 품질 하락 |
| **C. 현행 유지** | 지금 그대로 | 도시 밖 장소가 절대 저장되지 않음 | 여행이 안 만들어질 수 있음 |

### 별개로 발견된 Google 데이터 품질 이슈

실측 중 Google 이 `Bắc Nha Trang` 의 `locality` 를 다음과 같이 돌려주는 것을 확인했다.

```
long='Bac Natural Heritage Area Trang'   short='Bac NHA Trang'
```

`NHA` 를 "Natural Heritage Area" 로 오확장한 Google 쪽 문제다.
현재 구현은 `long_text` 와 `short_text` 를 **모두** 비교하므로 판정에는 영향이 없다.
다만 이 후보가 화면에 노출될 경우 이름이 이상하게 보일 수 있어 기록해 둔다.
