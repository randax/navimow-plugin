"""Turn raw capture records into the Trail and gap values this slice can persist."""

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
class Gap:
    """A period one mower went unrecorded; nothing can backfill it, so it is stored as a gap."""

    mower_id: str
    start_time: datetime
    end_time: datetime
    reason: str


@dataclass(frozen=True)
class ParsedRecord:
    points: tuple[TrailPoint, ...]
    placeholders_discarded: int = 0
    gaps: tuple[Gap, ...] = ()


def parse_record(record: Mapping[str, object]) -> ParsedRecord:
    """Parse location poses and gaps; all other capture records are intentionally ignored."""
    if record.get("kind") == "gap":
        gap = _gap(record)
        return ParsedRecord((), gaps=(gap,) if gap else ())
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


def _gap(record: Mapping[str, object]) -> Gap | None:
    """A gap record is written by the live transport: it ends when it is received."""
    mower_id, reason = record.get("mower_id"), record.get("reason")
    start, end = _timestamp(record.get("start_ms")), _timestamp(record.get("recv_ms"))
    if not isinstance(mower_id, str) or not isinstance(reason, str) or not start or not end:
        return None
    return Gap(mower_id, start, end, reason)


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
