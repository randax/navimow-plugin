"""Turn raw capture records into the readings the ingestion core decides on and persists."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

CHANNEL_TOPIC = re.compile(r"^/downlink/vehicle/([^/]+)/realtimeDate/([^/]+)$")
# The channels something is stored of. No capture has yet held a message on another (the
# mower has `event` and `attributes` too), so what those carry is not known.
STORED_CHANNELS = ("location", "state")


@dataclass(frozen=True)
class TrailPoint:
    mower_id: str
    device_time: datetime
    received_time: datetime
    x: float
    y: float
    theta: float
    vehicle_state: int | None
    job_id: str | None = None
    zone: int | None = None


@dataclass(frozen=True)
class MowerState:
    """What the state channel said: the mower's state and battery, when it changed."""

    mower_id: str
    device_time: datetime  # the receipt, where the message carries no time of its own
    received_time: datetime
    state: str
    battery: int | None
    job_id: str | None = None


@dataclass(frozen=True)
class Progress:
    """One progress report: how far the Job, and the Zone being mowed, have come."""

    mower_id: str
    device_time: datetime
    received_time: datetime
    zone: int | None
    zone_progress: float | None  # percent of the Zone
    mowing_percentage: int | None  # percent of the Job
    area: float | None  # square metres mowed in the Job so far
    week_area: float | None
    job_id: str | None = None


@dataclass(frozen=True)
class Job:
    """One Job as far as it is known; a later version of the same Job replaces this one."""

    mower_id: str
    job_id: str
    start_time: datetime
    updated_time: datetime
    end_time: datetime | None = None  # unset while the mower is away from the dock
    completed: bool = False
    mowing_percentage: int | None = None
    area: float | None = None
    # The pose the mower last reported as it docked: where the dock is, on its own axes.
    arrival_x: float | None = None
    arrival_y: float | None = None
    arrival_theta: float | None = None
    # The Zones the mower last said the Job covers; unset until it has said so.
    zones: tuple[int, ...] | None = None


@dataclass(frozen=True)
class ZoneList:
    """The Zones the mower says it has been set to mow, as it lists them while away."""

    mower_id: str
    device_time: datetime
    zones: tuple[int, ...]


@dataclass(frozen=True)
class Mower:
    """A mower as the account's device list describes it; rewritten when that changes."""

    mower_id: str
    name: str | None
    model: str | None
    firmware: str | None
    updated_time: datetime  # when it was first described so


class GapReason(StrEnum):
    """Why a mower went unrecorded: the broker connection dropped, or the collector was down."""

    RECONNECT = "reconnect"
    RESTART = "restart"


@dataclass(frozen=True)
class Gap:
    """A period one mower went unrecorded; nothing can backfill it, so it is stored as a gap."""

    mower_id: str
    start_time: datetime
    end_time: datetime
    reason: GapReason


Row = TrailPoint | Gap | Job | Progress | MowerState | Mower


@dataclass(frozen=True)
class ParsedRecord:
    points: tuple[TrailPoint, ...] = ()
    placeholders_discarded: int = 0
    gaps: tuple[Gap, ...] = ()
    states: tuple[MowerState, ...] = ()
    progress: tuple[Progress, ...] = ()
    polled: tuple[MowerState, ...] = ()  # what a status poll answered, as of its receipt
    zone_lists: tuple[ZoneList, ...] = ()
    mowers: tuple[Mower, ...] = ()


def parse_record(record: Mapping[str, object]) -> ParsedRecord:
    """Parse what the mower streamed and the gaps in it; other records are ignored."""
    if record.get("kind") == "gap":
        gap = _gap(record)
        return ParsedRecord(gaps=(gap,) if gap else ())
    received_time = _timestamp(record.get("recv_ms"))
    if received_time is None:
        return ParsedRecord()
    if record.get("kind") == "rest" and record.get("endpoint") == "getVehicleStatus":
        return ParsedRecord(polled=tuple(_polled(received_time, record.get("payload"))))
    if record.get("kind") == "rest" and record.get("endpoint") == "authList":
        return ParsedRecord(mowers=tuple(_described(received_time, record.get("payload"))))
    topic = record.get("topic")
    if record.get("kind") != "mqtt" or not isinstance(topic, str):
        return ParsedRecord()
    match = CHANNEL_TOPIC.fullmatch(topic)
    if match is None or match.group(2) not in STORED_CHANNELS:
        return ParsedRecord()
    payload = record.get("payload")
    if match.group(2) == "state":
        state = _state(match.group(1), received_time, payload)
        return ParsedRecord(states=(state,) if state else ())
    items: Sequence[object] = payload if isinstance(payload, list) else (payload,)
    points: list[TrailPoint] = []
    progress: list[Progress] = []
    zone_lists: list[ZoneList] = []
    placeholders = 0
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == 2:
            if report := _progress(match.group(1), received_time, item):
                progress.append(report)
        if item.get("type") == 3:
            if listed := _zone_list(match.group(1), item):
                zone_lists.append(listed)
        if item.get("type") != 1:
            continue
        point = _point(match.group(1), received_time, item)
        if point is None:
            continue
        if point.x == point.y == point.theta == 0:
            placeholders += 1
            continue
        points.append(point)
    return ParsedRecord(
        tuple(points), placeholders, progress=tuple(progress), zone_lists=tuple(zone_lists)
    )


def _gap(record: Mapping[str, object]) -> Gap | None:
    """A gap record is written by the live transport: it ends when it is received."""
    mower_id, reason = record.get("mower_id"), record.get("reason")
    start, end = _timestamp(record.get("start_ms")), _timestamp(record.get("recv_ms"))
    if not isinstance(mower_id, str) or not isinstance(reason, str) or not start or not end:
        return None
    try:
        return Gap(mower_id, start, end, GapReason(reason))
    except ValueError:  # a reason this collector does not know
        return None


def _state(mower_id: str, received_time: datetime, payload: object) -> MowerState | None:
    if not isinstance(payload, Mapping) or not isinstance(state := payload.get("state"), str):
        return None
    device_time = _timestamp(payload.get("timestamp")) or received_time
    return MowerState(mower_id, device_time, received_time, state, _int(payload.get("battery")))


def _devices(answer: object) -> Iterator[tuple[str, Mapping[object, object]]]:
    """Each mower a REST answer speaks of, by its identifier; an error speaks of none."""
    for key in ("data", "payload", "devices"):
        answer = answer.get(key) if isinstance(answer, Mapping) else None
    for device in answer if isinstance(answer, list) else ():
        if isinstance(device, Mapping) and isinstance(mower_id := device.get("id"), str):
            yield mower_id, device


def _polled(received_time: datetime, answer: object) -> Iterator[MowerState]:
    """Each mower's state in a status answer."""
    for mower_id, device in _devices(answer):
        if isinstance(state := device.get("vehicleState"), str):
            yield MowerState(mower_id, received_time, received_time, state, battery=None)


def _described(received_time: datetime, answer: object) -> Iterator[Mower]:
    """Each mower in a device list, with whatever of its details the list gives."""
    for mower_id, device in _devices(answer):
        name, model, firmware = (_str(device.get(key)) for key in ("name", "model", "firmware"))
        yield Mower(mower_id, name, model, firmware, received_time)


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


def _progress(
    mower_id: str, received_time: datetime, item: Mapping[object, object]
) -> Progress | None:
    device_time = _timestamp(item.get("time"))
    # A zeroed start type is the mower saying it is on no Job: sent hours after one, with
    # everything else zeroed too, it reports no progress.
    if device_time is None or _int(item.get("mowStartType")) == 0:
        return None
    zone_progress = _float(item.get("currentMowProgress"))
    return Progress(
        mower_id,
        device_time,
        received_time,
        zone=_int(item.get("currentMowBoundary")),
        zone_progress=None if zone_progress is None else zone_progress / 100,
        mowing_percentage=_int(item.get("mowingPercentage")),
        area=_float(item.get("subtotalArea")),
        week_area=_float(item.get("mowingWeekArea")),
    )


def _zone_list(mower_id: str, item: Mapping[object, object]) -> ZoneList | None:
    """A message of this kind sent near the dock carries no list, and so says nothing."""
    device_time, listed = _timestamp(item.get("time")), item.get("partitionIds")
    if device_time is None or not isinstance(listed, list):
        return None
    zones = tuple(zone for zone in map(_int, listed) if zone is not None)
    return ZoneList(mower_id, device_time, zones)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _int(value: object) -> int | None:
    # Numbers on this wire sometimes arrive as strings, so accept "4" as well as 4.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    # Beyond a 32-bit column it is noise, and a row no database accepts would never leave
    # the live buffer.
    return number if -(2**31) <= number < 2**31 else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _float(value: object) -> float | None:
    try:
        return float(value) if isinstance(value, (str, int, float)) else None
    except ValueError:
        return None
