"""여행별 동행 구성, 강도, 경비 수준과 서버의 하루 일정 규칙이다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time
from typing import Any


TRAVEL_PARTY_LABELS = {
    "unspecified": "미선택",
    "solo": "혼자",
    "couple": "커플",
    "friends": "친구",
    "family": "가족",
    "family_with_children": "아이 동반 가족",
    "with_parents": "부모님 동반",
    "senior_couple": "시니어 부부",
    "other": "기타",
}
INTENSITY_LABELS = {1: "아주 여유롭게", 2: "여유롭게", 3: "보통", 4: "알차게", 5: "아주 알차게"}
BUDGET_LABELS = {1: "최대한 절약", 2: "절약", 3: "보통", 4: "넉넉하게", 5: "아주 넉넉하게"}


@dataclass(frozen=True)
class DailyScheduleSlot:
    """AI가 바꿀 수 없는 하루 일정의 종류와 현지 시작·종료 시각이다."""

    slot_id: str
    item_type: str
    start_time: time
    end_time: time
    title: str = ""
    notes: str = ""

    @property
    def needs_place(self) -> bool:
        """Google Places에서 실제 장소를 확인해야 하는 식사·활동인지 반환한다."""

        return self.item_type in {"place", "restaurant"}


def _level(value: Any, name: str) -> int:
    """이전 여행의 비어 있는 설정에는 기본값을 적용하고 잘못된 값은 거절한다."""

    level = 3 if value is None else value
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5:
        raise ValueError(f"{name}는 1~5단계여야 합니다.")
    return level


def is_departure_day(travel_date: date, trip_end_date: Any) -> bool:
    """일부 DAY만 생성해도 여행의 실제 종료일과 같은 날만 출국일로 판단한다."""

    if trip_end_date is None:
        return False
    try:
        end_date = date.fromisoformat(str(trip_end_date))
    except (TypeError, ValueError) as error:
        raise ValueError("여행 종료일 형식이 올바르지 않습니다.") from error
    return travel_date == end_date


def daily_schedule_slots(
    intensity: int = 3, *, departure_day: bool = False
) -> tuple[DailyScheduleSlot, ...]:
    """일반날은 강도별 활동·식사·휴식을, 출국일은 18시 출국 준비 시간을 정한다.

    첫날 체크인은 예약 시각을 알 수 없어 여기의 고정 시각표에 포함하지 않는다.
    일정 생성기가 출국일이 아닌 첫날에만 시간 미정 안내 항목을 별도로 붙인다.
    출국일은 강도보다 공항 이동·수속 여유 시간을 우선하고 오후 관광을 만들지 않는다.
    """

    intensity = _level(intensity, "여행 강도")
    lunch = DailyScheduleSlot("lunch", "restaurant", time(12), time(13))
    dinner = DailyScheduleSlot("dinner", "restaurant", time(18), time(19))

    if departure_day:
        morning = (
            DailyScheduleSlot(
                "morning_leisure", "note", time(9), time(11), "오전 여유 시간",
                "현지 오전 9시부터 여유롭게 출국일을 시작하세요. 짐 정리 등 출발 준비에 활용하세요.",
            )
            if intensity <= 2
            else DailyScheduleSlot("activity_1", "place", time(9), time(11))
        )
        return (
            morning,
            lunch,
            DailyScheduleSlot(
                "airport_transfer", "note", time(13), time(15), "공항 이동 예비 시간",
                "18시 출국을 가정해 확보한 준비·이동 예비 시간이며 Routes로 계산한 이동시간이 아닙니다. "
                "실제 공항·교통편·이동시간에 따라 더 일찍 출발해야 할 수 있습니다.",
            ),
            DailyScheduleSlot(
                "departure_preparation", "note", time(15), time(18), "출국 수속·탑승 준비",
                "현지 18시 출국을 위한 예비 시간입니다. 실제 항공사 체크인·탑승 마감과 공항 안내를 확인하세요.",
            ),
            DailyScheduleSlot(
                "flight_departure", "note", time(18), time(18), "18:00 출국 · 예약 확인 필요",
                "여행지 현지 18시 출발을 가정한 일정 안내이며 실제 항공편 예약을 확인한 정보가 아닙니다. "
                "예약한 항공편 시각과 출발 공항을 확인하세요.",
            ),
        )

    if intensity <= 2:
        slots = [
            DailyScheduleSlot(
                "morning_leisure", "note", time(9), time(11), "오전 여유 시간",
                "현지 오전 9시부터 여유롭게 하루를 시작하세요. 체크인 시각은 숙소 예약을 확인한 뒤 정하세요.",
            ),
            lunch,
            DailyScheduleSlot("activity_1", "place", time(14), time(15, 30)),
            DailyScheduleSlot(
                "rest_afternoon", "hotel", time(16), time(17), "호텔 휴식 · 숙소 미정",
                "선택한 여행 강도에 맞춘 휴식 시간입니다. 예약한 숙소를 연결하고 이동·체크인 가능 시간을 확인하세요.",
            ),
            dinner,
        ]
        if intensity == 1:
            slots.append(DailyScheduleSlot(
                "rest_evening", "hotel", time(20), time(21), "호텔 휴식 · 숙소 미정",
                "저녁 식사 뒤 숙소에서 쉬는 시간입니다. 예약한 숙소를 연결하세요.",
            ))
        else:
            slots.append(DailyScheduleSlot("activity_2", "place", time(20), time(21)))
        return tuple(slots)

    slots = [DailyScheduleSlot("activity_1", "place", time(9), time(11)), lunch]
    if intensity == 3:
        slots.extend([
            DailyScheduleSlot("activity_2", "place", time(14), time(16)),
            dinner,
            DailyScheduleSlot("activity_3", "place", time(20), time(21)),
        ])
    else:
        slots.extend([
            DailyScheduleSlot("activity_2", "place", time(14), time(15)),
            DailyScheduleSlot("activity_3", "place", time(16), time(17)),
            dinner,
        ])
        if intensity == 4:
            slots.append(DailyScheduleSlot("activity_4", "place", time(20), time(21)))
        else:
            slots.extend([
                DailyScheduleSlot("activity_4", "place", time(19, 30), time(20, 30)),
                DailyScheduleSlot("activity_5", "place", time(21), time(22)),
            ])
    return tuple(slots)


def travel_preferences_text(trip: Mapping[str, Any]) -> str:
    """저장된 여행 조건을 일정 생성·채팅에서 함께 사용할 짧은 설명으로 바꾼다."""

    party = str(trip.get("travel_party") or "unspecified")
    if party not in TRAVEL_PARTY_LABELS:
        raise ValueError("동행 구성 값이 올바르지 않습니다.")
    intensity = _level(trip.get("travel_intensity"), "여행 강도")
    budget = _level(trip.get("budget_level"), "여행 경비 수준")
    return (
        f"동행 구성: {TRAVEL_PARTY_LABELS[party]}\n"
        f"여행 강도: {intensity}/5 ({INTENSITY_LABELS[intensity]}), "
        f"하루 관광·활동 {intensity}개 + 점심·저녁, 강도에 맞는 휴식\n"
        "단, 실제 여행 종료일은 현지 18시 출국을 가정해 13시까지 오전 일정·점심만 배치하고, "
        "오후는 공항 이동과 출국 수속 예비 시간으로 확보한다. 강도보다 출국 제약을 우선한다. "
        "18시는 확인된 항공편 시각이 아니며 공항 이동시간도 실측값이 아니다.\n"
        f"여행 경비 수준: {budget}/5 ({BUDGET_LABELS[budget]})\n"
        "경비는 총예산이나 확정 가격이 아닌 상대적인 소비 수준이다. "
        "가격·입장료·예약 가능 여부가 확인됐다고 단정하지 않는다.\n"
        "동행 구성은 장소 선택의 참고 조건이다. 나이만으로 취향·신체 능력을 단정하지 않고, "
        "아이의 연령이나 접근성 요구가 없으면 임의로 가정하지 않는다. "
        "동행 구성 때문에 사용자가 선택한 강도의 활동 개수를 바꾸지 않는다."
    )
