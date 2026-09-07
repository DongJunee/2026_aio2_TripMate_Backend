"""API 요청 모델 모음이다. DB가 만드는 ID와 시간은 UI에서 받지 않는다."""

from datetime import date, datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator


class SignupRequest(BaseModel):
    """회원가입에 필요한 사용자 이름, 이메일, 비밀번호 입력값이다."""

    username: str = Field(min_length=1, max_length=30)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    """로그인에 필요한 이메일과 비밀번호 입력값이다."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class DemoPasswordResetRequest(BaseModel):
    """학원 실습용 비밀번호 재설정 요청이다.

    실제 서비스에서는 이메일 소유 인증 후 비밀번호를 변경해야 한다. 이 프로젝트는
    로컬 실습용이므로 가입 때 입력한 사용자 이름을 추가로 확인한다.
    """

    username: str = Field(min_length=1, max_length=30)
    email: EmailStr
    new_password: str = Field(min_length=6, max_length=128)


class TokenResponse(BaseModel):
    """로그인 또는 회원가입 뒤 프론트엔드로 돌려주는 세션 정보이다."""

    access_token: str | None
    user_id: str
    email: str


class MessageResponse(BaseModel):
    """성공 또는 안내 문구만 반환하는 공통 응답 형식이다."""

    message: str


class ProfileUpdate(BaseModel):
    """현재 사용자의 표시 이름을 변경할 때 쓰는 입력값이다."""

    username: str = Field(min_length=1, max_length=30)


TravelParty = Literal[
    "unspecified", "solo", "couple", "friends", "family",
    "family_with_children", "with_parents", "senior_couple", "other",
]


class TravelPreferenceFields(BaseModel):
    """여행마다 저장하는 동행 구성, 여행 강도와 상대적인 경비 수준이다."""

    travel_party: TravelParty = "unspecified"
    travel_intensity: int = Field(default=3, ge=1, le=5, strict=True)
    budget_level: int = Field(default=3, ge=1, le=5, strict=True)


class TripCreate(TravelPreferenceFields):
    """새 여행을 만들 때 입력하는 기본 정보이다."""

    title: str = Field(min_length=1, max_length=100)
    destination: str | None = Field(default=None, max_length=100)
    # 화면에서는 입력받지 않는다. 생략하면 AI 초안의 여행지 시간대를 검증해 저장한다.
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    start_date: date | None = None
    end_date: date | None = None

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

    title: str | None = Field(default=None, min_length=1, max_length=100)
    destination: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    start_date: date | None = None
    end_date: date | None = None
    status: Literal["planning", "ongoing", "completed"] | None = None
    travel_party: TravelParty | None = None
    travel_intensity: int | None = Field(default=None, ge=1, le=5, strict=True)
    budget_level: int | None = Field(default=None, ge=1, le=5, strict=True)

    @model_validator(mode="after")
    def validate_preferences(self):
        """생략한 조건은 유지하되 DB의 필수 칼럼에 명시적으로 null을 넣지 못하게 한다."""
        for field in ("travel_party", "travel_intensity", "budget_level"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError("여행 구성, 강도, 경비는 빈 값으로 변경할 수 없습니다.")
        return self


class TripDateRangeUpdate(BaseModel):
    """여행 일차가 모호해지지 않도록 시작일과 종료일을 함께 받는다."""

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_dates(self):
        """종료일이 시작일보다 앞서지 않도록 확인한다."""
        if self.end_date < self.start_date:
            raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
        return self


class TripPinUpdate(BaseModel):
    """여행을 사이드바 고정 목록에 넣거나 해제할 때 쓰는 입력값이다."""

    pinned: bool


class TripDayCreate(BaseModel):
    """여행 안의 DAY 1, DAY 2 같은 하루 일정을 만드는 입력값이다."""

    day_number: int = Field(gt=0)
    travel_date: date
    title: str | None = Field(default=None, max_length=150)
    area: str | None = Field(default=None, max_length=100)
    memo: str | None = None


# 일정 항목의 종류, 생성 경로, 이동 수단은 자유 입력 대신 정해진 값만 사용한다.
ItemType = Literal[
    "place", "cafe", "restaurant", "hotel", "flight", "train", "transit", "activity", "note"
]
ItemSource = Literal["manual_entry", "google_search", "ai_recommendation"]
TravelMode = Literal["walk", "transit", "drive", "bicycle", "flight"]


class ItineraryItemCreate(BaseModel):
    """특정 여행 일차에 일정 한 건을 추가하는 입력값이다."""

    trip_day_id: UUID | None = None
    place_id: UUID | None = None
    item_type: ItemType = "place"
    source: ItemSource = "manual_entry"
    title: str = Field(min_length=1, max_length=150)
    start_at: datetime | None = None
    end_at: datetime | None = None
    estimated_stay_minutes: int | None = Field(default=None, ge=0)
    is_fixed: bool = False
    travel_mode: TravelMode | None = None
    notes: str | None = None

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

    trip_day_id: UUID | None = None
    item_type: ItemType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=150)
    start_at: datetime | None = None
    end_at: datetime | None = None
    estimated_stay_minutes: int | None = Field(default=None, ge=0)
    is_fixed: bool | None = None
    travel_mode: TravelMode | None = None
    notes: str | None = None
    sort_order: int | None = Field(default=None, ge=0)


class ItineraryItemTimeUpdate(BaseModel):
    """일정 한 칸의 시작·종료 시각을 사용자가 직접 바꿀 때 쓰는 입력값이다."""

    start_time: time
    end_time: time

    @model_validator(mode="after")
    def validate_time_order(self):
        """같은 DAY 안에서 종료 시각이 시작 시각보다 빠르지 않게 한다."""

        if self.end_time <= self.start_time:
            raise ValueError("종료 시간은 시작 시간보다 늦어야 합니다.")
        return self


class ItineraryPlaceSwap(BaseModel):
    """일정 시간 칸의 장소를 바로 앞 또는 뒤 칸과 교환하는 입력값이다."""

    direction: Literal["previous", "next"]


class GooglePlaceItineraryCreate(BaseModel):
    """검증된 Google 장소 하나를 선택한 DAY의 일정 항목으로 저장하는 입력값이다.

    브라우저는 Google 장소 ID와 일정 선택값만 보낸다. 백엔드가 현재 장소 상세를
    직접 조회하므로, 클라이언트가 지도 좌표·평점·내부 ``places.id`` 값을 위조할 수
    없다.
    """

    google_place_id: str = Field(min_length=1, max_length=255)
    item_type: ItemType = "place"
    start_at: datetime
    estimated_stay_minutes: int = Field(default=60, ge=0, le=1440)
    is_fixed: bool = False
    travel_mode: TravelMode | None = "walk"
    notes: str | None = None


class ChatRequest(BaseModel):
    """여행 채팅방에 보낼 사용자 메시지 입력값이다."""

    content: str = Field(min_length=1, max_length=4000)
