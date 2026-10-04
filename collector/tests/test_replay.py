"""The single seam: replay a capture, assert the rows it produces."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from navimow_collector.cli import main

from .conftest import FIXTURE, gaps

Row = tuple[str, datetime, datetime, float, float, float, int | None]


def fixture_poses() -> list[dict[str, Any]]:
    """Every type-1 pose in the fixture, in delivery order, with its recv_ms."""
    poses = []
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            if record["kind"] != "mqtt" or not record["topic"].endswith("/location"):
                continue
            payload = record["payload"]
            for item in payload if isinstance(payload, list) else [payload]:
                if item.get("type") == 1:
                    poses.append({**item, "recv_ms": record["recv_ms"]})
    return poses


def is_placeholder(pose: dict[str, Any]) -> bool:
    return all(float(pose[k]) == 0 for k in ("postureX", "postureY", "postureTheta"))


def ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def trail(dsn: str) -> list[Row]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT mower_id, device_time, received_time, x, y, theta, vehicle_state"
            " FROM trail_point ORDER BY device_time"
        ).fetchall()


@pytest.fixture
def replayed(config_file: Path, database: str) -> list[Row]:
    assert main(["--config", str(config_file), "replay", str(FIXTURE)]) == 0
    return trail(database)


def test_every_real_pose_becomes_one_trail_point(replayed: list[Row]) -> None:
    real = [p for p in fixture_poses() if not is_placeholder(p)]
    assert len(replayed) == len(real)
    assert {row[1] for row in replayed} == {ms(p["time"]) for p in real}


def test_all_zero_placeholders_are_discarded(replayed: list[Row]) -> None:
    assert any(is_placeholder(p) for p in fixture_poses()), "fixture lost its placeholders"
    assert not [row for row in replayed if row[3] == row[4] == row[5] == 0]


def test_points_carry_the_mower_and_both_clocks(replayed: list[Row]) -> None:
    by_time = {ms(p["time"]): p for p in fixture_poses()}
    for mower_id, device_time, received_time, x, y, theta, vehicle_state in replayed:
        pose = by_time[device_time]
        assert mower_id == "DEVICE_1"
        assert received_time == ms(pose["recv_ms"])
        assert (x, y, theta) == pytest.approx(
            (float(pose["postureX"]), float(pose["postureY"]), float(pose["postureTheta"]))
        )
        assert vehicle_state == pose["vehicleState"]


def test_a_late_point_lands_in_its_chronological_place(
    replayed: list[Row],
) -> None:
    # The fixture delivers one lane-7 point about 90 s late. Lane 7 runs back
    # along y = 1.75 with x falling, so in mower-clock order x must fall
    # throughout the lane, the late point included.
    late = [row for row in replayed if row[2] - row[1] > timedelta(seconds=60)]
    assert len(late) == 1
    lane = [row for row in replayed if row[4] == pytest.approx(1.75)]
    assert late[0] in lane
    xs = [row[3] for row in lane]
    assert xs == sorted(xs, reverse=True)
    assert len(set(xs)) == len(xs)


def test_replaying_twice_changes_nothing(
    config_file: Path,
    database: str,
    replayed: list[Row],
    capsys: pytest.CaptureFixture[str],
) -> None:
    capsys.readouterr()
    assert main(["--config", str(config_file), "replay", str(FIXTURE)]) == 0
    assert trail(database) == replayed
    assert "points written: 0;" in capsys.readouterr().out


def test_migration_records_a_schema_version(config_file: Path, database: str) -> None:
    assert main(["--config", str(config_file), "replay", str(FIXTURE)]) == 0
    with psycopg.connect(database) as conn:
        (version,) = conn.execute("SELECT max(version) FROM schema_version").fetchone() or (None,)
    assert isinstance(version, int) and version >= 1


def test_migration_can_be_disabled(
    config_file: Path,
    database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NAVIMOW_STORAGE_MIGRATE", "false")
    assert main(["--config", str(config_file), "replay", str(FIXTURE)]) != 0
    assert "migrat" in capsys.readouterr().err.lower()
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT to_regclass('trail_point')").fetchone() == (None,)


def test_a_gap_recorded_again_with_a_later_end_is_extended(
    config_file: Path, database: str, tmp_path: Path
) -> None:
    # A collector that died before noting its reconnection records the same gap again,
    # longer, when it restarts.
    first = {"recv_ms": 1788084400000, "start_ms": 1788084160000, "reason": "reconnect"}
    again = {"recv_ms": 1788084900000, "start_ms": 1788084160000, "reason": "restart"}
    for name, gap in (("first.jsonl", first), ("again.jsonl", again), ("stale.jsonl", first)):
        capture = tmp_path / name
        capture.write_text(json.dumps({"kind": "gap", "mower_id": "DEVICE_1", **gap}) + "\n")
        assert main(["--config", str(config_file), "replay", str(capture)]) == 0

    assert gaps(database) == [("DEVICE_1", ms(1788084160000), ms(1788084900000), "restart")]


def test_a_state_the_schema_cannot_hold_is_stored_as_unknown(
    config_file: Path, database: str, tmp_path: Path
) -> None:
    # A row the database can never accept would wait in the live buffer for ever.
    capture = tmp_path / "state.jsonl"
    pose = {"time": 1788084160000, "type": 1, "postureX": 1, "postureY": 2, "postureTheta": 3}
    record = {
        "recv_ms": 1788084160500,
        "kind": "mqtt",
        "topic": "/downlink/vehicle/DEVICE_1/realtimeDate/location",
        "payload": [{**pose, "vehicleState": 2**40}],
    }
    capture.write_text(json.dumps(record) + "\n")

    assert main(["--config", str(config_file), "replay", str(capture)]) == 0

    assert [(row[3], row[6]) for row in trail(database)] == [(1.0, None)]


def test_a_gap_record_becomes_a_gap_row(config_file: Path, database: str, tmp_path: Path) -> None:
    # What the live transport feeds the core on every reconnection or restart.
    capture = tmp_path / "gap.jsonl"
    gap = {
        "recv_ms": 1788084400000,
        "kind": "gap",
        "mower_id": "DEVICE_1",
        "start_ms": 1788084160000,
        "reason": "reconnect",
    }
    capture.write_text(json.dumps(gap) + "\n")

    assert main(["--config", str(config_file), "replay", str(capture)]) == 0
    assert main(["--config", str(config_file), "replay", str(capture)]) == 0  # idempotent

    assert gaps(database) == [("DEVICE_1", ms(1788084160000), ms(1788084400000), "reconnect")]
