"""Turn raw capture records into the Trail values this slice can persist."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

LOCATION_TOPIC = re.compile(r"^/downlink/vehicle/([^/]+)/realtimeDate/location$")


@dataclass(frozen=True)
class TrailPoint:
    mower_id: str
    device_time: datetime
    received_time: datetime
    x: float
    y: float
    theta: float
    vehicle_state: int | None


@dataclass(frozen=True)
class ParsedRecord:
    points: tuple[TrailPoint, ...]
    placeholders_discarded: int = 0


def parse_record(record: Mapping[str, object]) -> ParsedRecord:
    """Parse location poses; all other capture records are intentionally ignored."""
    topic = record.get("topic")
    if record.get("kind") != "mqtt" or not isinstance(topic, str):
        return ParsedRecord(())
    match = LOCATION_TOPIC.fullmatch(topic)
    if match is None:
        return ParsedRecord(())
    received_time = _timestamp(record.get("recv_ms"))
    if received_time is None:
        return ParsedRecord(())
    payload = record.get("payload")
    items: Sequence[object] = payload if isinstance(payload, list) else (payload,)
    points: list[TrailPoint] = []
    placeholders = 0
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != 1:
            continue
        point = _point(match.group(1), received_time, item)
        if point is None:
            continue
        if point.x == point.y == point.theta == 0:
            placeholders += 1
            continue
        points.append(point)
    return ParsedRecord(tuple(points), placeholders)


def _point(
    mower_id: str, received_time: datetime, item: Mapping[object, object]
) -> TrailPoint | None:
    device_time = _timestamp(item.get("time"))
    x = _float(item.get("postureX"))
    y = _float(item.get("postureY"))
    theta = _float(item.get("postureTheta"))
    if device_time is None or x is None or y is None or theta is None:
        return None
    return TrailPoint(
        mower_id, device_time, received_time, x, y, theta, _int(item.get("vehicleState"))
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _int(value: object) -> int | None:
    # Numbers on this wire sometimes arrive as strings, so accept "4" as well as 4.
    if isinstance(value, bool):
        return None
    try:
        return int(value) if isinstance(value, (int, str)) else None
    except ValueError:
        return None


def _float(value: object) -> float | None:
    try:
        return float(value) if isinstance(value, (str, int, float)) else None
    except ValueError:
        return None
