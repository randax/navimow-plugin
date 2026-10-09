"""Jobs and Zones as a capture reveals them: replay one, assert what it was recorded as."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from navimow_collector.cli import main
from navimow_collector.ingest import Ingestor, read_capture

from .conftest import FIXTURE, Told, told_at_once

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
Record = dict[str, Any]


def ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def rows(dsn: str, query: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(query).fetchall()


def jobs(dsn: str) -> list[dict[str, Any]]:
    return rows(dsn, "SELECT * FROM job ORDER BY start_time")


def fixture() -> list[Record]:
    """The synthetic capture's records, for a test to cut up or deliver differently."""
    return [dict(record) for record in read_capture(FIXTURE)]


def later(record: Record, by_ms: int) -> Record:
    """The same record as the mower would have sent it `by_ms` later."""
    record = copy.deepcopy(record)
    record["recv_ms"] += by_ms
    payload = record.get("payload")
    for item in payload if isinstance(payload, list) else [payload]:
        for key in ("time", "timestamp"):
            if isinstance(item, dict) and key in item:
                item[key] += by_ms
    return record


class Replay:
    """Replays captures into one database, as `navimow-collector replay` does."""

    def __init__(self, config_file: Path, database: str, tmp_path: Path) -> None:
        self._config_file = config_file
        self._tmp_path = tmp_path
        self.database = database

    def __call__(self, capture: Path | Iterable[Record]) -> str:
        if not isinstance(capture, Path):
            records, capture = capture, self._tmp_path / "capture.jsonl"
            capture.write_text("".join(json.dumps(record) + "\n" for record in records))
        assert main(["--config", str(self._config_file), "replay", str(capture)]) == 0
        return self.database


@pytest.fixture
def replay(config_file: Path, database: str, tmp_path: Path) -> Replay:
    return Replay(config_file, database, tmp_path)


@pytest.fixture
def synthetic(replay: Replay) -> str:
    """The database after replaying the synthetic capture: one Job with a charging break."""
    return replay(FIXTURE)


def test_a_job_runs_from_leaving_the_dock_to_returning_to_it(synthetic: str) -> None:
    # The state channel carries no timestamp in this capture, so its receipt is its time.
    [job] = jobs(synthetic)
    assert job["mower_id"] == "DEVICE_1"
    assert job["start_time"] == ms(1788085293941)  # isRunning
    assert job["end_time"] == ms(1788090811063)  # the last isDocked


def test_a_job_records_how_far_it_got(synthetic: str) -> None:
    [job] = jobs(synthetic)
    assert (job["completed"], job["mowing_percentage"], job["area"]) == (True, 100, 300.0)


def test_the_message_with_a_zeroed_start_type_starts_no_job(synthetic: str) -> None:
    # Sent hours after the Job, with its area at zero as an announcement's is.
    [zeroed] = [r for r in fixture() if is_progress(r) and r["payload"][0]["mowStartType"] == 0]
    sent = ms(zeroed["payload"][0]["time"])
    assert len(jobs(synthetic)) == 1
    assert sent not in {report["device_time"] for report in progress(synthetic)}


def test_leaving_the_dock_after_a_finished_job_starts_a_new_one(replay: Replay) -> None:
    # The same day twice. Each departure announces its Job beside the percentage the last
    # one finished on, which must not finish the new one before it has begun.
    day = fixture()
    first, second = jobs(replay([*day, *(later(record, DAY_MS) for record in day)]))
    assert first["end_time"] == ms(1788090811063)
    assert second["start_time"] == first["start_time"] + timedelta(days=1)
    assert (second["completed"], second["area"]) == (True, 300.0)


def test_leaving_the_dock_again_the_same_afternoon_is_still_a_new_job(replay: Replay) -> None:
    # An hour and a half after finishing: soon enough to be a charging break, were the
    # first Job not finished.
    day = fixture()
    first, second = jobs(replay([*day, *(later(record, 3 * HOUR_MS) for record in day)]))
    assert (first["end_time"], first["completed"]) == (ms(1788090811063), True)
    assert second["start_time"] == ms(LEFT_DOCK + 3 * HOUR_MS)


def test_without_progress_reports_each_departure_from_the_dock_is_a_job(replay: Replay) -> None:
    # Some firmware reports no progress at all on some Jobs. Whether the mower went to
    # charge or was done cannot then be told apart, so no departure is guessed to continue
    # the Job before it.
    silent = [r for r in fixture() if not is_progress(r)]
    first, second = jobs(replay(silent))
    assert (first["start_time"], first["end_time"]) == (ms(LEFT_DOCK), ms(1788087728135))
    assert (second["start_time"], second["end_time"]) == (ms(1788089527836), ms(1788090811063))
    assert (first["completed"], first["mowing_percentage"]) == (False, None)


def is_state(record: Record) -> bool:
    return str(record.get("topic", "")).endswith("/state")


def test_area_falling_to_zero_starts_a_new_job_even_unseen_leaving_the_dock(
    replay: Replay,
) -> None:
    # The second day arrives without its state channel. Its Job is still announced: the
    # area is back at zero, beside the 100 % the first Job finished on.
    day = fixture()
    second_day = [later(record, DAY_MS) for record in day if not is_state(record)]
    first, second = jobs(replay([*day, *second_day]))
    assert first["end_time"] == ms(1788090811063)
    assert second["start_time"] == ms(1788085297268 + DAY_MS)  # the announcement
    assert (second["completed"], second["area"]) == (True, 300.0)


def test_a_job_already_under_way_when_collection_starts_is_recorded_from_then(
    replay: Replay,
) -> None:
    under_way = [r for r in fixture() if r["recv_ms"] > 1788085500000]
    [job] = jobs(replay(under_way))
    assert job["start_time"] == ms(1788085536268)  # the first progress report heard
    assert job["end_time"] == ms(1788090811063)


def test_a_job_is_resumed_however_long_the_mower_stayed_at_the_dock(replay: Replay) -> None:
    # Up to the charging break, then the rest of the day delivered a day late, as after a
    # day of rain: the area carries on from where it stood, so it is the same Job.
    day = fixture()
    before = [r for r in day if r["recv_ms"] < 1788089000000]
    after = [later(r, DAY_MS) for r in day if r["recv_ms"] >= 1788089000000]
    [job] = jobs(replay([*before, *after]))
    assert (job["start_time"], job["end_time"]) == (ms(LEFT_DOCK), ms(1788090811063 + DAY_MS))
    assert (job["completed"], job["area"]) == (True, 300.0)


def test_a_job_called_off_before_a_square_metre_is_mowed_is_not_the_next_one(
    replay: Replay,
) -> None:
    # Started, called back to the dock at once, and started again an hour later. The
    # first Job's area is too small for the second's to fall far below it; but the second
    # is announced with no area at all, as only a new Job is.
    day = fixture()
    called_off = [r for r in day if r["recv_ms"] < 1788085500000]
    [report] = [
        r for r in called_off if is_progress(r) and r["payload"][0]["mowingPercentage"] == 3
    ]
    report["payload"] = [{**report["payload"][0], "subtotalArea": "0.40", "mowingPercentage": 0}]
    again = [later(r, HOUR_MS) for r in day if r["recv_ms"] >= LEFT_DOCK]
    first, second = jobs(replay([*called_off, state(1788085500000, "isDocked"), *again]))
    assert (first["end_time"], first["area"]) == (ms(1788085500000), 0.4)
    assert (second["start_time"], second["area"]) == (ms(1788085297268 + HOUR_MS), 300.0)


def test_area_far_below_an_unfinished_jobs_is_a_new_job_though_its_announcement_was_lost(
    replay: Replay,
) -> None:
    # The mower goes to charge at 190 square metres, and the next day leaves on another
    # Job whose announcement never arrives. Its first report, of 10, is not the first Job's.
    day = fixture()
    charging = [r for r in day if r["recv_ms"] < 1788089000000]
    next_day = [later(r, DAY_MS) for r in day if r["recv_ms"] >= LEFT_DOCK and not announces(r)]
    first, second = jobs(replay([*charging, *next_day]))
    assert (first["end_time"], first["area"]) == (ms(1788087728135), 190.0)
    assert second["start_time"] == ms(1788085422268 + DAY_MS)  # its first report


def announces(record: Record) -> bool:
    return is_progress(record) and float(record["payload"][0]["subtotalArea"]) == 0


def test_progress_from_a_mower_last_seen_docked_with_nothing_left_to_mow_is_a_new_job(
    replay: Replay,
) -> None:
    # The first day reports no progress, so its Jobs are not known to be unfinished. On
    # the second neither the departure nor the announcement is heard, only the reports.
    day = fixture()
    first_day = [r for r in day if not is_progress(r)]
    unseen = [later(r, DAY_MS) for r in day if not is_state(r) and r["recv_ms"] > 1788085300000]
    *_, job = jobs(replay([*first_day, *unseen]))
    assert job["start_time"] == ms(1788085422268 + DAY_MS)  # the first report heard
    assert (job["completed"], job["area"]) == (True, 300.0)


def is_progress(record: Record) -> bool:
    payload = record.get("payload")
    return any(
        isinstance(item, dict) and item.get("type") == 2
        for item in (payload if isinstance(payload, list) else [payload])
    )


def trail(dsn: str) -> list[dict[str, Any]]:
    return rows(dsn, "SELECT * FROM trail_point ORDER BY device_time")


def within(row: dict[str, Any], spans: Iterable[tuple[int, int]]) -> bool:
    """Whether the row falls in a span. A span starts after the message that opens it: a
    position the mower stamped with that very millisecond was delivered before it."""
    return any(ms(start) < row["device_time"] <= ms(end) for start, end in spans)


# The synthetic mower is away from the dock twice: until it goes to charge, and after.
AWAY = [(1788085293941, 1788087728135), (1788089527836, 1788090811063)]


def test_every_position_away_from_the_dock_belongs_to_the_job(synthetic: str) -> None:
    [job] = jobs(synthetic)
    points = trail(synthetic)
    assert {p["job_id"] for p in points} == {job["job_id"], None}
    for point in points:
        assert (point["job_id"] is not None) == within(point, AWAY), point["device_time"]


def test_a_position_delivered_late_belongs_where_the_mower_was(synthetic: str) -> None:
    [late] = [p for p in trail(synthetic) if p["received_time"] - p["device_time"] > MINUTE]
    assert late["job_id"] is not None


MINUTE = timedelta(minutes=1)


def test_a_position_carries_the_zone_being_mowed(synthetic: str) -> None:
    # A Zone runs from the report that first names it. It is unknown before the first
    # report, and again from the moment the mower turns for the dock until the next one.
    zone_10 = [(1788085422268, 1788087370268)]
    zone_11 = [(1788087370268, 1788087712641), (1788089531268, 1788090794059)]
    for point in trail(synthetic):
        expected = 10 if within(point, zone_10) else 11 if within(point, zone_11) else None
        assert point["zone"] == expected, point["device_time"]


def test_a_progress_report_delivered_hours_late_does_not_start_a_job(replay: Replay) -> None:
    # Its area is far below where the Job ended, as a new Job's would be; but it was sent
    # before reports already heard, so it says nothing new.
    day = fixture()
    [stray] = [r for r in day if is_progress(r) and r["payload"][0]["subtotalArea"] == "90.00"]
    day.remove(stray)
    [job] = jobs(replay([*day, {**stray, "recv_ms": day[-1]["recv_ms"] + 1}]))
    assert (job["end_time"], job["area"]) == (ms(1788090811063), 300.0)


def progress(dsn: str) -> list[dict[str, Any]]:
    return rows(dsn, "SELECT * FROM job_progress ORDER BY device_time")


def test_progress_through_the_job_and_through_each_zone_is_recorded(synthetic: str) -> None:
    [job] = jobs(synthetic)
    reports = progress(synthetic)
    # 33 reports from the Job. The announcement, which reports no progress, is not stored;
    # and two were sent in the same millisecond, of which the one delivered first stands,
    # as for every row but a Job: a row delivered again never replaces the one stored.
    assert len(reports) == 31
    assert (reports[-1]["mowing_percentage"], job["mowing_percentage"]) == (99, 100)
    assert {r["job_id"] for r in reports} == {job["job_id"]}
    change = next(i for i, r in enumerate(reports) if r["zone"] == 11)
    before, after = reports[change - 1], reports[change]
    # Zone progress starts over at a Zone change, while the Job's carries on.
    assert (before["zone"], before["zone_progress"], before["mowing_percentage"]) == (10, 100, 50)
    assert (after["zone"], after["zone_progress"], after["mowing_percentage"]) == (11, 6.66, 53)
    assert (after["area"], after["week_area"]) == (160.0, 179.11)
    assert after["device_time"] == ms(1788087370268)


def test_the_state_channel_is_recorded_with_battery_and_the_job_it_fell_in(synthetic: str) -> None:
    states = rows(synthetic, "SELECT * FROM mower_state ORDER BY device_time")
    assert len(states) == 11
    changes = [next(streak) for _, streak in groupby(states, key=lambda s: s["state"])]
    assert [(s["state"], s["battery"], s["job_id"] is not None) for s in changes] == [
        ("isDocked", 64, False),
        ("isRunning", 96, True),
        ("isDocking", 21, True),
        ("isDocked", 20, False),
        ("isRunning", 92, True),
        ("isDocking", 46, True),
        ("isDocked", 45, False),
    ]


def test_the_pose_the_mower_arrived_at_the_dock_in_is_recorded(synthetic: str) -> None:
    # The last pose reported before the state channel says docked: it marks the dock.
    [job] = jobs(synthetic)
    assert (job["arrival_x"], job["arrival_y"], job["arrival_theta"]) == (0.329, 0.042, -2.859)


STATE_TOPIC = "/downlink/vehicle/DEVICE_1/realtimeDate/state"
MID_JOB = 1788087000000  # mowing Zone 10, 40 % of the Job done


def state(at_ms: int, name: str, sent_ms: int | None = None) -> Record:
    """A state message, which names when it was sent only if the two times differ."""
    payload = {"state": name} if sent_ms is None else {"state": name, "timestamp": sent_ms}
    return {"recv_ms": at_ms, "kind": "mqtt", "topic": STATE_TOPIC, "payload": payload}


@pytest.mark.parametrize("interruption", ["isPaused", "isLifted", "Error", "Offline", "isIdle"])
def test_a_job_that_is_interrupted_and_never_completes_ends_at_the_dock(
    replay: Replay, interruption: str
) -> None:
    interrupted = [r for r in fixture() if r["recv_ms"] < MID_JOB]
    carried_back = [state(MID_JOB, interruption), state(MID_JOB + 600_000, "isDocked")]
    [job] = jobs(replay([*interrupted, *carried_back]))
    assert job["end_time"] == ms(MID_JOB + 600_000)
    assert (job["completed"], job["mowing_percentage"], job["area"]) == (False, 40, 120.0)


def test_an_interruption_does_not_split_the_job(replay: Replay) -> None:
    day = fixture()
    cut = next(i for i, r in enumerate(day) if r["recv_ms"] >= MID_JOB)
    paused = [state(MID_JOB - 2, "isPaused"), state(MID_JOB - 1, "isRunning")]
    [job] = jobs(replay([*day[:cut], *paused, *day[cut:]]))
    assert (job["start_time"], job["end_time"]) == (ms(1788085293941), ms(1788090811063))


def test_a_job_the_mower_never_came_back_from_ends_where_it_was_last_heard(
    replay: Replay,
) -> None:
    # Nothing more is heard of the first day's Job, not even that the mower docked. The
    # next day's is announced, with its area back at zero, so the first is over: as of its
    # last progress report.
    day = fixture()
    never_back = [r for r in day if r["recv_ms"] < MID_JOB]
    next_day = [later(r, DAY_MS) for r in day if r["recv_ms"] >= LEFT_DOCK]
    first, second = jobs(replay([*never_back, *next_day]))
    assert (first["end_time"], first["completed"]) == (ms(1788086676268), False)
    assert second["start_time"] == ms(1788085297268 + DAY_MS)  # the announcement
    assert (second["end_time"], second["completed"]) == (ms(1788090811063 + DAY_MS), True)


def test_a_job_given_up_while_charging_ended_when_the_mower_docked(replay: Replay) -> None:
    # The mower goes to charge at 63 %, and an hour and a half later leaves on another Job
    # instead. Until that Job is announced, seconds later, it looks like the first resuming.
    day = fixture()
    charging = [r for r in day if r["recv_ms"] < 1788089000000]
    another = [later(r, 90 * 60_000) for r in day if r["recv_ms"] >= LEFT_DOCK]
    first, second = jobs(replay([*charging, *another]))
    assert (first["end_time"], first["completed"]) == (ms(1788087728135), False)
    assert second["start_time"] == ms(1788085297268 + 90 * 60_000)


LEFT_DOCK = 1788085293941  # the isRunning the synthetic Job starts with


def gap(start_ms: int, end_ms: int) -> Record:
    """What the live transport feeds the core when its connection returns."""
    return {
        "recv_ms": end_ms,
        "kind": "gap",
        "mower_id": "DEVICE_1",
        "start_ms": start_ms,
        "reason": "reconnect",
    }


def status(at_ms: int, name: str) -> Record:
    """The status poll the live transport makes on every connection."""
    devices = [{"id": "DEVICE_1", "vehicleState": name}]
    payload = {"code": 1, "data": {"payload": {"devices": devices}}}
    return {"recv_ms": at_ms, "kind": "rest", "endpoint": "getVehicleStatus", "payload": payload}


def test_an_outage_inside_a_job_is_a_gap_in_it_not_the_end_of_it(replay: Replay) -> None:
    day = fixture()
    cut = next(i for i, r in enumerate(day) if r["recv_ms"] >= MID_JOB)
    outage = [gap(MID_JOB - 240_000, MID_JOB - 2), status(MID_JOB - 1, "isRunning")]
    database = replay([*day[:cut], *outage, *day[cut:]])
    [job] = jobs(database)
    assert (job["start_time"], job["end_time"]) == (ms(LEFT_DOCK), ms(1788090811063))
    assert len(rows(database, "SELECT * FROM collector_gap")) == 1


def test_a_mower_found_docked_after_an_outage_ended_its_job_by_then(replay: Replay) -> None:
    # The state channel only speaks on a change, and the change was missed: the status
    # poll made on reconnecting is all that says where the mower stands.
    before = [r for r in fixture() if r["recv_ms"] < MID_JOB]
    outage = [gap(MID_JOB, MID_JOB + 1_800_000), status(MID_JOB + 1_800_001, "isDocked")]
    [job] = jobs(replay([*before, *outage]))
    assert (job["end_time"], job["completed"]) == (ms(MID_JOB + 1_800_001), False)
    # Its last pose is half an hour old, from out on the lawn: it does not mark the dock.
    assert job["arrival_x"] is None


def test_a_status_poll_is_not_believed_over_the_state_channel(replay: Replay) -> None:
    # The REST API runs a minute or two behind: polled just after docking, it still says
    # the mower is out. The state channel has already said otherwise.
    docked = 1788087728135
    charging = [r for r in fixture() if r["recv_ms"] <= docked]
    [job] = jobs(replay([*charging, status(docked + 30_000, "isRunning")]))
    assert job["end_time"] == ms(docked)


# The first capture from a real mower (an X420 on firmware 005D): one Job across six Zones,
# with a charging break at 76 %. What it is asserted to hold is what the capture's own
# resolution found in it (issue #6), not what the collector makes of it.
REAL_CAPTURE = FIXTURE.with_name("job-2026-09-30.jsonl.gz")
REAL_LEFT, REAL_CHARGING, REAL_RESUMED, REAL_DOCKED = (
    1790775213612,
    1790780821146,
    1790784617715,
    1790786846740,
)


@pytest.fixture
def real(replay: Replay) -> str:
    return replay(REAL_CAPTURE)


def test_the_real_capture_is_one_job_across_its_charging_break(real: str) -> None:
    [job] = jobs(real)
    assert (job["start_time"], job["end_time"]) == (ms(REAL_LEFT), ms(REAL_DOCKED))
    assert (job["completed"], job["mowing_percentage"], job["area"]) == (True, 100, 652.94)
    assert (job["arrival_x"], job["arrival_y"], job["arrival_theta"]) == (-0.255, -0.437, 1.01)
    away = [(REAL_LEFT, REAL_CHARGING), (REAL_RESUMED, REAL_DOCKED)]
    for point in trail(real):
        assert (point["job_id"] == job["job_id"]) == within(point, away), point["device_time"]


def test_the_real_capture_mows_its_six_zones_in_order(real: str) -> None:
    # The Zone its Job was announced with, 1, is not where it began: it drove to 11.
    visited = [p["zone"] for p in trail(real) if p["zone"] is not None]
    in_order = [zone for zone, _ in groupby(visited)]
    assert in_order == [11, 10, 1, 6, 7, 9]
    assert len(progress(real)) == 414  # every type-2 message but the announcement


def test_a_job_records_the_zones_it_was_set_to_mow(real: str) -> None:
    # The mower lists them every five minutes while it is away. Half a second before it
    # turned for the dock it sent a message of that kind with no list in it, which says
    # nothing of the Job.
    [job] = jobs(real)
    assert job["zones"] == [1, 6, 7, 9, 10, 11]


def test_the_synthetic_job_records_its_two_zones(synthetic: str) -> None:
    [job] = jobs(synthetic)
    assert job["zones"] == [10, 11]


def test_a_zone_list_delivered_after_a_later_one_decides_nothing(replay: Replay) -> None:
    # Were the Zones of a Job changed while it was under way, an older list held up on the
    # way must not put back what the Job was set to mow before.
    capture: list[Record] = [dict(r) for r in read_capture(REAL_CAPTURE)]
    stray = {
        "recv_ms": capture[-1]["recv_ms"] + 1,
        "kind": "mqtt",
        "topic": "/downlink/vehicle/DEVICE_1/realtimeDate/location",
        "payload": [{"partitionIds": [1, 6], "time": REAL_LEFT + 60_000, "type": 3}],
    }
    [job] = jobs(replay([*capture, stray]))
    assert job["zones"] == [1, 6, 7, 9, 10, 11]


def test_a_state_delivered_after_a_later_one_decides_nothing(replay: Replay) -> None:
    # The mower says isRunning twice when it resumes. Were the second delivered after it
    # has finished and docked, it would look like leaving the dock on a new Job.
    capture: list[Record] = [dict(r) for r in read_capture(REAL_CAPTURE)]
    [stray] = [r for r in capture if is_state(r) and r["payload"]["timestamp"] == 1790784617755]
    capture.remove(stray)
    [job] = jobs(replay([*capture, {**stray, "recv_ms": capture[-1]["recv_ms"] + 1}]))
    assert job["end_time"] == ms(REAL_DOCKED)


WENT_TO_CHARGE = (1788087712641, 1788087728135)  # the synthetic isDocking, and its isDocked


def delivered_after(moment_ms: int) -> list[Record]:
    """The synthetic capture up to its charging break, with the progress report the mower
    sent as it turned for the dock held up until just after `moment_ms`."""
    day = [r for r in fixture() if r["recv_ms"] <= WENT_TO_CHARGE[1]]
    [held_up] = [r for r in day if is_progress(r) and r["payload"][0]["subtotalArea"] == "190.00"]
    day.remove(held_up)
    cut = sum(r["recv_ms"] <= moment_ms for r in day)
    return [*day[:cut], {**held_up, "recv_ms": moment_ms + 1}, *day[cut:]]


def test_a_report_sent_before_the_mower_turned_for_the_dock_does_not_put_it_back_in_the_zone(
    replay: Replay,
) -> None:
    turned, _ = WENT_TO_CHARGE
    on_the_way_back = [
        p for p in trail(replay(delivered_after(turned))) if p["device_time"] > ms(turned)
    ]
    assert len(on_the_way_back) > 5
    assert {p["zone"] for p in on_the_way_back} == {None}


def test_a_report_sent_before_the_mower_docked_does_not_send_it_out_again(
    replay: Replay,
) -> None:
    _, docked = WENT_TO_CHARGE
    [job] = jobs(replay(delivered_after(docked)))
    assert job["end_time"] == ms(docked)
    assert (job["mowing_percentage"], job["area"]) == (63, 190.0)  # and still counts


def test_a_state_from_before_the_job_began_says_nothing_of_it(replay: Replay) -> None:
    # The first day's Job is last heard mid-lawn. The next day's is announced, unseen
    # leaving the dock; and only then does the first day's isDocked turn up. It is not the
    # new Job that docked ten minutes into the day before.
    day = fixture()
    never_back = [r for r in day if r["recv_ms"] < MID_JOB]
    next_day = [later(r, DAY_MS) for r in day if r["recv_ms"] > LEFT_DOCK]
    announced = next(i for i, r in enumerate(next_day) if announces(r)) + 1
    stray = state(next_day[announced]["recv_ms"], "isDocked", sent_ms=MID_JOB + 600_000)
    first, second = jobs(replay([*never_back, *next_day[:announced], stray, *next_day[announced:]]))
    assert second["start_time"] == ms(1788085297268 + DAY_MS)
    assert (second["end_time"], second["completed"]) == (ms(1788090811063 + DAY_MS), True)


def test_a_job_given_up_ends_at_its_last_report_even_one_that_reported_nothing_new(
    replay: Replay,
) -> None:
    # Stuck where it stood, the mower reports the same area and percentage a minute on.
    day = fixture()
    never_back = [r for r in day if r["recv_ms"] < MID_JOB]
    [last] = [r for r in never_back if is_progress(r) and r["payload"][0]["time"] == 1788086676268]
    next_day = [later(r, DAY_MS) for r in day if r["recv_ms"] >= LEFT_DOCK]
    first, _ = jobs(replay([*never_back, later(last, 60_000), *next_day]))
    assert first["end_time"] == ms(1788086676268 + 60_000)


def without_area(record: Record) -> Record:
    """The progress report as some firmware sends it: with its area left empty."""
    if not is_progress(record) or announces(record):
        return record
    return {**record, "payload": [{**record["payload"][0], "subtotalArea": ""}]}


def test_a_report_without_its_area_leaves_the_jobs_area_as_last_known(replay: Replay) -> None:
    day = fixture()
    blank = [without_area(r) if r["recv_ms"] > 1788090000000 else r for r in day]
    [job] = jobs(replay(blank))
    assert (job["completed"], job["mowing_percentage"]) == (True, 100)
    assert job["area"] == 230.0  # the last area reported before they went empty


def test_an_announcement_starts_a_job_though_the_one_before_never_reported_an_area(
    replay: Replay,
) -> None:
    # The mower goes to charge at 63 %, never having said how much it mowed, and leaves on
    # another Job. That one is announced with its area at zero all the same.
    day = fixture()
    charging = [without_area(r) for r in day if r["recv_ms"] < 1788089000000]
    another = [later(r, 90 * 60_000) for r in day if r["recv_ms"] >= LEFT_DOCK]
    first, second = jobs(replay([*charging, *another]))
    assert (first["mowing_percentage"], first["area"]) == (63, None)
    assert second["start_time"] == ms(1788085297268 + 90 * 60_000)  # the announcement


def test_a_report_from_before_the_job_began_says_nothing_of_it(replay: Replay) -> None:
    # One of the first day's last reports turns up only after the mower has left the dock
    # the next day. Its 99 % and 300 square metres are not the new Job's.
    day = fixture()
    [stray] = [r for r in day if is_progress(r) and r["payload"][0]["mowingPercentage"] == 99]
    day.remove(stray)
    next_day = [later(r, DAY_MS) for r in day]
    left = next(i for i, r in enumerate(next_day) if r["recv_ms"] == LEFT_DOCK + DAY_MS) + 1
    held_up = {**stray, "recv_ms": LEFT_DOCK + DAY_MS + 1}
    first, second = jobs(replay([*day, *next_day[:left], held_up, *next_day[left:]]))
    assert (first["completed"], first["end_time"]) == (True, ms(1788090811063))
    assert (second["start_time"], second["area"]) == (ms(LEFT_DOCK + DAY_MS), 300.0)


def test_a_docking_from_before_the_latest_report_does_not_end_the_job(replay: Replay) -> None:
    # The isDocked turns up late, stamped a minute before a progress report already heard.
    # Whatever dock visit it tells of, the mower has reported mowing since.
    mowing = [r for r in fixture() if r["recv_ms"] < MID_JOB]
    stray = state(MID_JOB, "isDocked", sent_ms=1788086676268 - 60_000)
    [job] = jobs(replay([*mowing, stray]))
    assert (job["end_time"], job["area"]) == (None, 120.0)


def test_a_report_held_up_from_before_the_charging_break_does_not_confirm_a_resume(
    replay: Replay,
) -> None:
    # As when a Job is given up while charging; but between the departure and the new Job's
    # announcement, a report the mower sent on its way to the dock is delivered. It is not
    # progress since the break, so the first Job still ended when the mower docked.
    day = fixture()
    charging = [r for r in day if r["recv_ms"] < 1788089000000]
    [last] = [r for r in charging if is_progress(r) and r["payload"][0]["subtotalArea"] == "190.00"]
    another = [later(r, 90 * 60_000) for r in day if r["recv_ms"] >= LEFT_DOCK]
    held_up = {**later(last, 5_000), "recv_ms": another[0]["recv_ms"] + 1}
    first, second = jobs(replay([*charging, another[0], held_up, *another[1:]]))
    assert (first["end_time"], first["completed"]) == (ms(1788087728135), False)
    assert second["start_time"] == ms(1788085297268 + 90 * 60_000)


def test_a_collector_carrying_on_from_a_job_told_of_anew_still_hears_that_millisecond() -> None:
    # A telling moved on to after the one before is still of the message it was sent in:
    # what else the mower sent in that millisecond is no older than what was heard.
    stored = told_at_once(9)[-1]
    told = Told()
    ingestor = Ingestor(told, jobs=[stored])
    sent = int(stored.start_time.timestamp() * 1000)
    report = {"type": 2, "time": sent, "mowStartType": 1, "subtotalArea": 20}
    topic = "/downlink/vehicle/DEVICE_1/realtimeDate/location"
    ingestor.feed({"recv_ms": sent, "kind": "mqtt", "topic": topic, "payload": [report]})
    ingestor.flush()
    [job] = told.jobs
    assert job.area == 20 and job.updated_time > stored.updated_time
