"""추천 장소의 좌표로 날짜별 군집과 하루 방문 순서를 개선한다."""

from datetime import datetime
from math import asin, cos, radians, sin, sqrt


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """두 위경도의 대권 거리(km)를 계산한다. 도로 이동시간은 아니다."""
    lat1, lat2 = radians(a[0]), radians(b[0])
    value = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin(radians(b[1] - a[1]) / 2) ** 2
    return 6371 * 2 * asin(sqrt(min(1.0, max(0.0, value))))


def group_nearby_itinerary_places(
    rows: list[dict], coordinates: dict[str, tuple[float, float]],
) -> list[dict]:
    """시간표는 유지하면서 교환 가능한 장소를 가까운 날짜·순서로 재배치한다.

    식당은 점심끼리 또는 저녁끼리, 관광지는 관광지끼리 교환한다. 숙소 미정
    안내·출국 준비·고정 일정·좌표 없는 행은 이동하지 않는다. 원본을 수정하지
    않으며 장소 ID·이름·추천 이유를 함께 옮기고 날짜와 체류시간은 슬롯에 둔다.
    직선 이동거리 우선, 같은 거리에서는 일별 밀집도를 비교하는 제한된 탐색이다.
    도로·대중교통 최단 경로나 영업시간 준수를 보장하는 알고리즘은 아니다.
    """
    result = [dict(row) for row in rows]
    by_day: dict[str, list[int]] = {}
    groups: dict[tuple, list[int]] = {}
    for index, row in enumerate(rows):
        place_id = str(row.get("place_id") or "")
        # 일부 단위 테스트나 이전 데이터에는 DAY 연결값이 없을 수 있다. 그런 행은
        # 기존 순서를 그대로 유지하며 전체 일정 생성을 실패시키지 않는다.
        if place_id not in coordinates or not row.get("start_at") or not row.get("trip_day_id"):
            continue
        by_day.setdefault(str(row["trip_day_id"]), []).append(index)
        if row.get("is_fixed") or row.get("item_type") not in {"place", "restaurant"}:
            continue
        start = datetime.fromisoformat(str(row["start_at"]).replace("Z", "+00:00"))
        # 초기 생성 시각은 현지 오프셋을 포함한다. 점심과 저녁을 섞지 않는다.
        meal = ("lunch" if start.hour < 16 else "dinner") if row["item_type"] == "restaurant" else None
        groups.setdefault((row["item_type"], meal), []).append(index)
    for indexes in by_day.values():
        indexes.sort(key=lambda i: datetime.fromisoformat(str(rows[i]["start_at"]).replace("Z", "+00:00")))

    assigned = [str(row.get("place_id") or "") for row in rows]
    distances: dict[tuple[str, str], float] = {}

    def distance(left: int, right: int) -> float:
        """반복해서 비교하는 장소 간 거리를 재사용한다."""
        key = tuple(sorted((assigned[left], assigned[right])))
        if key not in distances:
            distances[key] = _distance(coordinates[key[0]], coordinates[key[1]])
        return distances[key]

    def score(day: str) -> tuple[float, float]:
        """하루 방문 순서의 거리와 모든 장소 쌍의 평균 거리를 구한다."""
        indexes = by_day[day]
        route = sum(distance(a, b) for a, b in zip(indexes, indexes[1:]))
        pairs = [distance(a, b) for offset, a in enumerate(indexes) for b in indexes[offset + 1:]]
        return route, sum(pairs) / max(1, len(pairs))

    day_scores = {day: score(day) for day in by_day}
    # 가장 개선 폭이 큰 교환부터 적용한다. 같은 날 교환은 방문 순서를 줄이고,
    # 다른 날 교환은 먼 지역이 섞인 일정을 가까운 지역끼리 모은다.
    for _ in range(40):
        best = None
        best_delta = (0.0, -1e-7)
        for indexes in groups.values():
            for offset, left in enumerate(indexes):
                for right in indexes[offset + 1:]:
                    days = {str(rows[left]["trip_day_id"]), str(rows[right]["trip_day_id"])}
                    before = tuple(sum(day_scores[day][axis] for day in days) for axis in (0, 1))
                    assigned[left], assigned[right] = assigned[right], assigned[left]
                    after_scores = {day: score(day) for day in days}
                    after = tuple(sum(after_scores[day][axis] for day in days) for axis in (0, 1))
                    assigned[left], assigned[right] = assigned[right], assigned[left]
                    delta = (round(after[0] - before[0], 6), after[1] - before[1])
                    if delta < best_delta:
                        best_delta, best = delta, (left, right, after_scores)
        if best is None:
            break
        left, right, after_scores = best
        assigned[left], assigned[right] = assigned[right], assigned[left]
        # 시간 칸이 아니라 실제 장소에 속한 정보만 함께 교환한다.
        for field in ("place_id", "title", "notes", "source"):
            left_value, right_value = result[left].pop(field, None), result[right].pop(field, None)
            if right_value is not None:
                result[left][field] = right_value
            if left_value is not None:
                result[right][field] = left_value
        day_scores.update(after_scores)
    return result
