"""API 요청 모델 모음이다. DB가 만드는 ID와 시간은 UI에서 받지 않는다."""

from datetime import date, datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator


class SignupRequest(BaseModel):
    """회원가입에 필요한 사용자 이름, 이메일, 비밀번호 입력값이다."""

    username: str = Field(min_length=1, max_length=30, description="TripMate에 표시할 사용자 이름", examples=["여행메이트"])
    email: EmailStr = Field(description="로그인에 사용할 이메일 주소", examples=["traveler@example.com"])
    password: str = Field(min_length=6, max_length=128, description="6자 이상 비밀번호", examples=["tripmate123"])

    model_config = {"json_schema_extra": {"examples": [{"username": "여행메이트", "email": "traveler@example.com", "password": "tripmate123"}]}}


class LoginRequest(BaseModel):
    """로그인에 필요한 이메일과 비밀번호 입력값이다."""

    email: EmailStr = Field(description="가입한 이메일 주소", examples=["traveler@example.com"])
    password: str = Field(min_length=1, max_length=128, description="가입 시 설정한 비밀번호", examples=["tripmate123"])

    model_config = {"json_schema_extra": {"examples": [{"email": "traveler@example.com", "password": "tripmate123"}]}}


class PasswordResetRequest(BaseModel):
    """사용자 이름과 이메일을 확인한 뒤 새 비밀번호를 설정하는 요청이다.

    운영 환경에서는 이메일 소유 인증 또는 추가 본인 인증 절차를 함께 적용해야 한다.
    """

    username: str = Field(min_length=1, max_length=30, description="가입 시 설정한 표시 이름", examples=["여행메이트"])
    email: EmailStr = Field(description="가입한 이메일 주소", examples=["traveler@example.com"])
    new_password: str = Field(min_length=6, max_length=128, description="새로 설정할 6자 이상 비밀번호", examples=["newtripmate123"])

    model_config = {"json_schema_extra": {"examples": [{"username": "여행메이트", "email": "traveler@example.com", "new_password": "newtripmate123"}]}}


class TokenResponse(BaseModel):
    """로그인 또는 회원가입 뒤 프론트엔드로 돌려주는 세션 정보이다."""

    access_token: str | None
    user_id: str
    email: str


class MessageResponse(BaseModel):
    """성공 또는 안내 문구만 반환하는 공통 응답 형식이다."""

    message: str


MateType = Literal["assistant", "guide", "senior"]


class ProfileUpdate(BaseModel):
    """현재 사용자의 표시 이름과 Mate 대화 방식을 변경할 때 쓰는 입력값이다."""

    username: str | None = Field(default=None, min_length=1, max_length=30, description="변경할 표시 이름", examples=["오사카여행자"])
    mate_type: MateType | None = Field(default=None, description="AI Mate 답변 방식: assistant, guide, senior", examples=["guide"])

    model_config = {"json_schema_extra": {"examples": [{"username": "오사카여행자", "mate_type": "guide"}]}}

    @model_validator(mode="after")
    def validate_any_change(self):
        """빈 PATCH 요청으로 프로필을 갱신하지 못하게 한다."""

        if self.username is None and self.mate_type is None:
            raise ValueError("변경할 프로필 정보를 입력하세요.")
        return self


class PasswordChangeRequest(BaseModel):
    """로그인 중인 사용자가 현재 비밀번호 확인 뒤 새 비밀번호를 정할 때 쓰는 값이다."""

    current_password: str = Field(min_length=1, max_length=128, description="현재 사용 중인 비밀번호", examples=["tripmate123"])
    new_password: str = Field(min_length=6, max_length=128, description="새로 설정할 6자 이상 비밀번호", examples=["newtripmate123"])

    model_config = {"json_schema_extra": {"examples": [{"current_password": "tripmate123", "new_password": "newtripmate123"}]}}


TravelParty = Literal[
    "unspecified", "solo", "couple", "friends", "family",
    "family_with_children", "with_parents", "senior_couple", "other",
]


class TravelPreferenceFields(BaseModel):
    """여행마다 저장하는 동행 구성, 여행 강도와 상대적인 경비 수준이다."""

    travel_party: TravelParty = Field(default="unspecified", description="여행 인원 구성: solo, couple, friends, family 등", examples=["couple"])
    travel_intensity: int = Field(default=3, ge=1, le=5, strict=True, description="여행 일정 강도. 1은 여유롭게, 5는 촘촘하게", examples=[3])
    budget_level: int = Field(default=3, ge=1, le=5, strict=True, description="여행 경비 수준. 1은 절약형, 5는 여유형", examples=[3])

#lsw 0908
class MustVisitPlace(BaseModel):
    """화면 '가고 싶은 장소'에서 고른 곳 하나이다.

    [변경 사유] 이름만 받지 않는다. 사용자는 검색 결과에서 특정 지점을 골랐고
    그 google_place_id 를 이미 알고 있다. 이름만 넘기면 나중에 다시 검색할 때
    같은 이름의 다른 지점이 잡힐 수 있다 — '스타벅스'가 대표적이다.

    [변경 사유] google_place_id 는 선택으로 둔다. 대화나 자유 입력으로 들어온
    장소명도 같은 칸을 쓰게 해서, 입력 경로마다 모델이 갈라지지 않게 한다.
    """

    name: str = Field(min_length=1, max_length=150, description="여행 중 반드시 방문하고 싶은 장소명", examples=["오사카성"])
    google_place_id: str | None = Field(default=None, max_length=255, description="Google Places에서 선택한 장소 ID. 검색 결과를 선택했다면 함께 전달", examples=["ChIJ4x2b7pJpAGARo1KX6o0c5T8"])


class TripCreate(TravelPreferenceFields):
    """새 여행을 만들 때 입력하는 기본 정보이다."""

    title: str = Field(min_length=1, max_length=100, description="여행 목록에 표시할 제목", examples=["오사카 3박 4일"])
    destination: str | None = Field(default=None, max_length=100, description="여행 도시", examples=["오사카, 일본"])
    # 화면에서는 입력받지 않는다. 생략하면 AI 초안의 여행지 시간대를 검증해 저장한다.
    timezone: str | None = Field(default=None, min_length=1, max_length=64, description="IANA 시간대. 생략하면 여행지 기준으로 설정", examples=["Asia/Tokyo"])
    start_date: date | None = Field(default=None, description="여행 시작일", examples=["2026-09-07"])
    end_date: date | None = Field(default=None, description="여행 종료일", examples=["2026-09-10"])

#lsw 0908
    must_visit: list[MustVisitPlace] = Field(default_factory=list, max_length=5)

    model_config = {"json_schema_extra": {"examples": [{"title": "오사카 3박 4일", "destination": "오사카, 일본", "start_date": "2026-09-07", "end_date": "2026-09-10", "travel_party": "couple", "travel_intensity": 3, "budget_level": 3, "must_visit": [{"name": "오사카성"}]}]}}

    @model_validator(mode="after")
    def validate_dates(self):
        """여행 시작일과 종료일이 함께 있고 올바른 순서인지 확인한다."""
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("여행 시작일과 종료일을 함께 입력하세요.")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
        return self


class TripUpdate(BaseModel):
    """기존 여행의 제목, 여행지, 상태 등 일부 정보를 수정하는 입력값이다."""

    title: str | None = Field(default=None, min_length=1, max_length=100, description="변경할 여행 제목", examples=["오사카 가을 여행"])
    destination: str | None = Field(default=None, max_length=100, description="변경할 여행지", examples=["오사카, 일본"])
    timezone: str | None = Field(default=None, min_length=1, max_length=64, description="여행지 IANA 시간대", examples=["Asia/Tokyo"])
    start_date: date | None = Field(default=None, description="변경할 시작일", examples=["2026-09-07"])
    end_date: date | None = Field(default=None, description="변경할 종료일", examples=["2026-09-10"])
    status: Literal["planning", "ongoing", "completed"] | None = Field(default=None, description="여행 상태", examples=["planning"])
    travel_party: TravelParty | None = Field(default=None, description="변경할 여행 인원 구성", examples=["couple"])
    travel_intensity: int | None = Field(default=None, ge=1, le=5, strict=True, description="변경할 여행 강도", examples=[3])
    budget_level: int | None = Field(default=None, ge=1, le=5, strict=True, description="변경할 경비 수준", examples=[3])

    model_config = {"json_schema_extra": {"examples": [{"title": "오사카 가을 여행", "destination": "오사카, 일본", "travel_party": "couple", "travel_intensity": 3, "budget_level": 3}]}}

    @model_validator(mode="after")
    def validate_preferences(self):
        """생략한 조건은 유지하되 DB의 필수 칼럼에 명시적으로 null을 넣지 못하게 한다."""
        for field in ("travel_party", "travel_intensity", "budget_level"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError("여행 구성, 강도, 경비는 빈 값으로 변경할 수 없습니다.")
        return self


class TripDateRangeUpdate(BaseModel):
    """여행 일차가 모호해지지 않도록 시작일과 종료일을 함께 받는다."""

    start_date: date = Field(description="변경할 여행 시작일", examples=["2026-09-07"])
    end_date: date = Field(description="변경할 여행 종료일", examples=["2026-09-10"])

    model_config = {"json_schema_extra": {"examples": [{"start_date": "2026-09-07", "end_date": "2026-09-10"}]}}

    @model_validator(mode="after")
    def validate_dates(self):
        """종료일이 시작일보다 앞서지 않도록 확인한다."""
        if self.end_date < self.start_date:
            raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
        return self


class TripPinUpdate(BaseModel):
    """여행을 사이드바 고정 목록에 넣거나 해제할 때 쓰는 입력값이다."""

    pinned: bool = Field(description="true면 사이드바 고정, false면 고정 해제", examples=[True])

    model_config = {"json_schema_extra": {"examples": [{"pinned": True}]}}


class TripDayCreate(BaseModel):
    """여행 안의 DAY 1, DAY 2 같은 하루 일정을 만드는 입력값이다."""

    day_number: int = Field(gt=0, description="여행의 몇 번째 날인지 나타내는 번호", examples=[1])
    travel_date: date = Field(description="해당 일차의 날짜", examples=["2026-09-07"])
    title: str | None = Field(default=None, max_length=150, description="일차 제목", examples=["오사카 도심 탐방"])
    area: str | None = Field(default=None, max_length=100, description="주요 여행 지역", examples=["난바"])
    memo: str | None = Field(default=None, description="일차에 남길 메모", examples=["오후에는 자유 일정"])

    model_config = {"json_schema_extra": {"examples": [{"day_number": 1, "travel_date": "2026-09-07", "title": "오사카 도심 탐방", "area": "난바"}]}}


# 일정 항목의 종류, 생성 경로, 이동 수단은 자유 입력 대신 정해진 값만 사용한다.
ItemType = Literal[
    "place", "cafe", "restaurant", "hotel", "flight", "train", "transit", "activity", "note"
]
ItemSource = Literal["manual_entry", "google_search", "ai_recommendation"]
TravelMode = Literal["walk", "transit", "drive", "bicycle", "flight"]


class ItineraryItemCreate(BaseModel):
    """특정 여행 일차에 일정 한 건을 추가하는 입력값이다."""

    trip_day_id: UUID | None = Field(default=None, description="추가할 일차의 ID. URL의 여행에 속해야 함", examples=["11111111-1111-1111-1111-111111111111"])
    place_id: UUID | None = Field(default=None, description="저장된 장소 ID. 직접 입력 일정이면 생략 가능", examples=["22222222-2222-2222-2222-222222222222"])
    item_type: ItemType = Field(default="place", description="일정 종류", examples=["cafe"])
    source: ItemSource = Field(default="manual_entry", description="일정 생성 경로", examples=["manual_entry"])
    title: str = Field(min_length=1, max_length=150, description="화면에 표시할 일정 제목", examples=["도톤보리 카페 방문"])
    start_at: datetime | None = Field(default=None, description="일정 시작 시각(ISO 8601)", examples=["2026-09-07T13:00:00+09:00"])
    end_at: datetime | None = Field(default=None, description="일정 종료 시각(ISO 8601)", examples=["2026-09-07T14:00:00+09:00"])
    estimated_stay_minutes: int | None = Field(default=None, ge=0, description="예상 체류 시간(분)", examples=[60])
    is_fixed: bool = Field(default=False, description="시간을 고정한 일정인지 여부", examples=[False])
    travel_mode: TravelMode | None = Field(default=None, description="다음 장소로 이동할 수단", examples=["walk"])
    notes: str | None = Field(default=None, description="일정 메모", examples=["웨이팅 시간을 고려해 방문"])

    model_config = {"json_schema_extra": {"examples": [{"trip_day_id": "11111111-1111-1111-1111-111111111111", "item_type": "cafe", "title": "도톤보리 카페 방문", "start_at": "2026-09-07T13:00:00+09:00", "end_at": "2026-09-07T14:00:00+09:00", "estimated_stay_minutes": 60, "travel_mode": "walk"}]}}

    @model_validator(mode="after")
    def validate_times(self):
        """시작·종료 시각이 함께 있고 시간 순서가 맞는지 확인한다."""
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("시작과 종료 시각을 함께 입력하세요.")
        if self.start_at and self.end_at and self.end_at < self.start_at:
            raise ValueError("종료 시각은 시작 시각보다 빠를 수 없습니다.")
        return self


class ItineraryItemUpdate(BaseModel):
    """기존 일정의 변경할 항목만 전달하는 입력값이다."""

    trip_day_id: UUID | None = Field(default=None, description="일정을 옮길 대상 일차 ID", examples=["11111111-1111-1111-1111-111111111111"])
    item_type: ItemType | None = Field(default=None, description="변경할 일정 종류", examples=["restaurant"])
    title: str | None = Field(default=None, min_length=1, max_length=150, description="변경할 일정 제목", examples=["난바 저녁 식사"])
    start_at: datetime | None = Field(default=None, description="변경할 시작 시각(ISO 8601)", examples=["2026-09-07T18:00:00+09:00"])
    end_at: datetime | None = Field(default=None, description="변경할 종료 시각(ISO 8601)", examples=["2026-09-07T19:30:00+09:00"])
    estimated_stay_minutes: int | None = Field(default=None, ge=0, description="변경할 예상 체류 시간(분)", examples=[90])
    is_fixed: bool | None = Field(default=None, description="시간 고정 여부", examples=[True])
    travel_mode: TravelMode | None = Field(default=None, description="변경할 이동 수단", examples=["transit"])
    notes: str | None = Field(default=None, description="변경할 메모", examples=["예약 시간 18시"])
    sort_order: int | None = Field(default=None, ge=0, description="같은 일차 안에서의 표시 순서", examples=[3])

    model_config = {"json_schema_extra": {"examples": [{"title": "난바 저녁 식사", "start_at": "2026-09-07T18:00:00+09:00", "end_at": "2026-09-07T19:30:00+09:00", "travel_mode": "transit"}]}}


class ItineraryItemTimeUpdate(BaseModel):
    """일정 한 칸의 시작·종료 시각을 사용자가 직접 바꿀 때 쓰는 입력값이다."""

    start_time: time = Field(description="변경할 시작 시각(HH:MM:SS)", examples=["14:00:00"])
    end_time: time = Field(description="변경할 종료 시각(HH:MM:SS)", examples=["15:00:00"])

    model_config = {"json_schema_extra": {"examples": [{"start_time": "14:00:00", "end_time": "15:00:00"}]}}

    @model_validator(mode="after")
    def validate_time_order(self):
        """같은 DAY 안에서 종료 시각이 시작 시각보다 빠르지 않게 한다."""

        if self.end_time <= self.start_time:
            raise ValueError("종료 시간은 시작 시간보다 늦어야 합니다.")
        return self


class ItineraryPlaceSwap(BaseModel):
    """일정 시간 칸의 장소를 바로 앞 또는 뒤 칸과 교환하는 입력값이다."""

    direction: Literal["previous", "next"] = Field(description="교체할 이웃 일정 방향: previous 또는 next", examples=["next"])

    model_config = {"json_schema_extra": {"examples": [{"direction": "next"}]}}


class GooglePlaceItineraryCreate(BaseModel):
    """검증된 Google 장소 하나를 선택한 DAY의 일정 항목으로 저장하는 입력값이다.

    브라우저는 Google 장소 ID와 일정 선택값만 보낸다. 백엔드가 현재 장소 상세를
    직접 조회하므로, 클라이언트가 지도 좌표·평점·내부 ``places.id`` 값을 위조할 수
    없다.
    """

    google_place_id: str = Field(min_length=1, max_length=255, description="Google Places 검색 결과에서 받은 장소 ID", examples=["ChIJ4x2b7pJpAGARo1KX6o0c5T8"])
    item_type: ItemType = Field(default="place", description="추가할 일정 종류", examples=["cafe"])
    start_at: datetime = Field(description="일정 시작 시각(ISO 8601)", examples=["2026-09-07T13:00:00+09:00"])
    estimated_stay_minutes: int = Field(default=60, ge=0, le=1440, description="예상 체류 시간(분)", examples=[60])
    is_fixed: bool = Field(default=False, description="시간 고정 여부", examples=[False])
    travel_mode: TravelMode | None = Field(default="walk", description="이동 수단", examples=["walk"])
    source: ItemSource = Field(default="google_search", description="장소 선택 경로", examples=["google_search"])
    notes: str | None = Field(default=None, description="일정 메모", examples=["대표 메뉴 확인"])

    model_config = {"json_schema_extra": {"examples": [{"google_place_id": "ChIJ4x2b7pJpAGARo1KX6o0c5T8", "item_type": "cafe", "start_at": "2026-09-07T13:00:00+09:00", "estimated_stay_minutes": 60, "travel_mode": "walk"}]}}


class AccommodationPlaceUpdate(BaseModel):
    """Google에서 검증한 장소 하나를 여행의 숙소로 지정할 때 쓰는 입력값이다."""

    google_place_id: str = Field(min_length=1, max_length=255, description="숙소로 지정할 Google Places 장소 ID", examples=["ChIJ4x2b7pJpAGARo1KX6o0c5T8"])

    model_config = {"json_schema_extra": {"examples": [{"google_place_id": "ChIJ4x2b7pJpAGARo1KX6o0c5T8"}]}}


class ChatRequest(BaseModel):
    """여행 채팅방에 보낼 사용자 메시지 입력값이다."""

    content: str = Field(min_length=1, max_length=4000, description="현재 여행 일정에 관해 AI에게 보낼 질문 또는 변경 요청", examples=["오전 여유 시간에 갈 만한 카페를 추천해줘"])

    model_config = {"json_schema_extra": {"examples": [{"content": "오전 여유 시간에 갈 만한 카페를 추천해줘"}]}}
