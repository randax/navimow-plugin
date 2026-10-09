"""Retention as an owner observes it: rows older than they keep are removed, and only those."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from navimow_collector.cli import main
from navimow_collector.records import Gap, GapReason, Job, Mower, MowerState, Progress, TrailPoint
from navimow_collector.storage import Storage, postgres, retention, write_rows
from navimow_collector.storage.retention import Retention

from .conftest import FIXTURE, Clock
from .test_live import NOW, STATE, Live, at, stopped_at_its_first_request, until

HOUR = 3600
# The tables whose rows expire, and the column that says when each row is from.
EXPIRING_TABLES = {
    "trail_point": "device_time",
    "job_progress": "device_time",
    "mower_state": "device_time",
    "collector_gap": "start_time",
}


@pytest.fixture
def live(database: str, tmp_path: Path) -> Live:
    return Live(database, tmp_path)


def rows_at(when: datetime) -> list[TrailPoint | Progress | MowerState | Gap]:
    """One row of every kind that expires, as of then."""
    received = when + timedelta(seconds=1)
    return [
        TrailPoint("DEVICE_1", when, received, 1.0, 2.0, 0.5, 4),
        Progress("DEVICE_1", when, received, 1, 50.0, 25, 12.0, 40.0),
        MowerState("DEVICE_1", when, received, "isRunning", 80),
        Gap("DEVICE_1", when, received, GapReason.RECONNECT),
    ]


def stored(live: Live, table: str, column: str) -> list[datetime]:
    """When each row of a table is from, oldest first."""
    with psycopg.connect(live.db.dsn) as conn:
        return [row[0] for row in conn.execute(f"SELECT {column} FROM {table} ORDER BY {column}")]


def expiring(live: Live) -> dict[str, list[datetime]]:
    """When the rows of each table that expires are from."""
    return {table: stored(live, table, column) for table, column in EXPIRING_TABLES.items()}


def days_ago(days: float) -> datetime:
    return at(NOW) - timedelta(days=days)


def removed_as_of(live: Live, *times: float, retention_days: int | None = 30) -> None:
    """A collector keeping rows for so many days, ticking at each of these times; whatever
    removal a tick starts has ended before the next."""

    async def scenario() -> None:
        collector = live.start(retention_days=retention_days)
        for now in times:
            live.clock.now = now
            await collector.tick()
            if live.retention is not None:
                live.retention.join()

    asyncio.run(scenario())


def test_rows_older_than_the_owner_keeps_are_removed_and_the_history_of_jobs_is_not(
    live: Live,
) -> None:
    old, kept = days_ago(31), days_ago(29)
    with live.db.open() as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        write_rows(storage, [Job("DEVICE_1", "old", old, old, end_time=old, completed=True)])
        write_rows(storage, [Mower("DEVICE_2", "Sold", "X420", "005D", days_ago(400))])

    removed_as_of(live, NOW)

    assert expiring(live) == dict.fromkeys(EXPIRING_TABLES, [kept])
    assert stored(live, "job", "start_time") == [old]
    assert days_ago(400) in stored(live, "mower", "updated_time")


def test_old_rows_are_removed_a_few_at_a_time_until_none_is_left(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retention, "BATCH_ROWS", 2)
    kept = days_ago(29)
    with live.db.open() as storage:
        for days in (31, 32, 33, 34, 35):
            write_rows(storage, rows_at(days_ago(days)))
        write_rows(storage, rows_at(kept))
    with psycopg.connect(live.db.dsn) as conn:  # the database notes what each statement removes
        conn.execute("CREATE TABLE removal (points integer)")
        conn.execute(
            "CREATE FUNCTION note_removal() RETURNS trigger LANGUAGE plpgsql AS"
            " $$ BEGIN INSERT INTO removal SELECT count(*) FROM gone; RETURN NULL; END $$"
        )
        conn.execute(
            "CREATE TRIGGER note_removal AFTER DELETE ON trail_point REFERENCING OLD TABLE AS gone"
            " FOR EACH STATEMENT EXECUTE FUNCTION note_removal()"
        )

    removed_as_of(live, NOW)

    assert expiring(live) == dict.fromkeys(EXPIRING_TABLES, [kept])
    with psycopg.connect(live.db.dsn) as conn:
        statements = [row[0] for row in conn.execute("SELECT points FROM removal")]
    assert sum(statements) == 5 and max(statements) == 2


def test_rows_are_removed_about_once_a_day(live: Live) -> None:
    # Rows turn 30 days old all the time: these two do so 1 and 25 hours after the start.
    first, second = days_ago(30) + timedelta(hours=1), days_ago(30) + timedelta(hours=25)
    with live.db.open() as storage:
        write_rows(storage, [*rows_at(first), *rows_at(second)])

    removed_as_of(live, NOW, NOW + 2 * HOUR, NOW + 23 * HOUR)
    assert stored(live, "trail_point", "device_time") == [first, second]

    removed_as_of(live, NOW, NOW + 2 * HOUR, NOW + 24 * HOUR)
    assert stored(live, "trail_point", "device_time") == [second]


def test_everything_is_kept_unless_the_owner_says_otherwise(live: Live) -> None:
    ancient = days_ago(3650)
    with live.db.open() as storage:
        write_rows(storage, rows_at(ancient))

    removed_as_of(live, NOW, NOW + 24 * HOUR, retention_days=None)

    assert expiring(live) == dict.fromkeys(EXPIRING_TABLES, [ancient])


def test_replay_removes_nothing_whatever_the_owner_keeps(
    live: Live, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A capture replayed is history put in on purpose, and every row of it may be old.
    ancient = days_ago(3650)
    with live.db.open() as storage:
        write_rows(storage, rows_at(ancient))
    monkeypatch.setenv("NAVIMOW_STORAGE_RETENTION_DAYS", "1")

    assert main(["--config", str(config_file), "replay", str(FIXTURE)]) == 0

    assert all(times[0] == ancient for times in expiring(live).values())
    assert len(stored(live, "trail_point", "device_time")) > 1000


def test_the_collect_command_removes_what_is_older_than_the_owner_keeps(
    live: Live, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, kept = datetime.now(UTC) - timedelta(days=31), datetime.now(UTC) - timedelta(days=29)
    with live.db.open() as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
    monkeypatch.setenv("NAVIMOW_AUTH_STATE_FILE", str(live.store.path))
    monkeypatch.setenv("NAVIMOW_COLLECTOR_STATE_DIR", str(live.state))
    monkeypatch.setenv("NAVIMOW_HEALTH_LISTEN", "")
    monkeypatch.setenv("NAVIMOW_STORAGE_RETENTION_DAYS", "30")
    session = stopped_at_its_first_request(live)

    assert main(["--config", str(config_file), "collect"], session_factory=session) == 0

    removal_ended()
    assert expiring(live) == dict.fromkeys(EXPIRING_TABLES, [kept])


def removal_ended() -> None:
    """Wait for whatever removal the `collect` command left under way when it stopped."""
    for thread in threading.enumerate():
        if thread.name == "retention":
            thread.join()


def test_the_collect_command_removes_nothing_unless_the_owner_says_how_long_to_keep_rows(
    live: Live, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancient = datetime.now(UTC).replace(microsecond=0) - timedelta(days=3650)
    with live.db.open() as storage:
        write_rows(storage, rows_at(ancient))
    monkeypatch.setenv("NAVIMOW_AUTH_STATE_FILE", str(live.store.path))
    monkeypatch.setenv("NAVIMOW_COLLECTOR_STATE_DIR", str(live.state))
    monkeypatch.setenv("NAVIMOW_HEALTH_LISTEN", "")
    session = stopped_at_its_first_request(live)

    assert main(["--config", str(config_file), "collect"], session_factory=session) == 0

    removal_ended()
    assert expiring(live) == dict.fromkeys(EXPIRING_TABLES, [ancient])


def test_a_removal_that_fails_is_left_to_the_next_day(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    old = days_ago(31)
    with live.db.open() as storage:
        write_rows(storage, rows_at(old))

    async def scenario() -> None:
        collector = live.start(retention_days=30)
        assert live.retention is not None
        live.db.down = True  # to new connections, which a removal makes
        await collector.tick()
        live.retention.join()
        live.db.down = False
        live.clock.now = NOW + 23 * HOUR
        await collector.tick()
        live.retention.join()
        assert stored(live, "trail_point", "device_time") == [old]
        live.clock.now = NOW + 24 * HOUR
        await collector.tick()
        live.retention.join()

    asyncio.run(scenario())

    assert "Could not remove rows older than" in caplog.text
    assert stored(live, "trail_point", "device_time") == []


def test_a_removal_the_database_keeps_waiting_holds_up_no_collection(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(postgres, "STATEMENT_TIMEOUT_MS", 0)
    with live.db.open() as storage:
        write_rows(storage, rows_at(days_ago(31)))

    async def scenario() -> None:
        collector = live.start(retention_days=30)
        assert live.retention is not None
        with live.db.suspended():  # for the Trail, which the removal starts with
            await collector.tick()
            await until(live.db.held)
            await live.broker.accept()
            await live.broker.deliver(
                STATE.format("DEVICE_1"), {"state": "isDocked", "battery": 90}
            )
            assert stored(live, "mower_state", "device_time")[-1] == at(NOW)
            assert live.db.held()
        live.retention.join()

    asyncio.run(scenario())

    assert stored(live, "trail_point", "device_time") == []


def test_a_gap_is_as_old_as_its_end(live: Live) -> None:
    # A winter with the collector off is one gap, written when it comes back: it is of
    # that day, not of the autumn it began in.
    began, ended = days_ago(200), days_ago(29)
    with live.db.open() as storage:
        write_rows(storage, [Gap("DEVICE_1", began, ended, GapReason.RESTART)])

    removed_as_of(live, NOW)
    assert stored(live, "collector_gap", "start_time") == [began]

    removed_as_of(live, NOW + 2 * 24 * HOUR)
    assert stored(live, "collector_gap", "start_time") == []


def test_rows_kept_for_longer_than_dates_go_back_are_simply_kept(live: Live) -> None:
    ancient = days_ago(3650)
    with live.db.open() as storage:
        write_rows(storage, rows_at(ancient))

    removed_as_of(live, NOW, retention_days=10**9)

    assert expiring(live) == dict.fromkeys(EXPIRING_TABLES, [ancient])


def test_each_mowers_old_rows_are_removed(database: str, tmp_path: Path) -> None:
    live = Live(database, tmp_path, "DEVICE_1", "DEVICE_2")
    old, kept = days_ago(31), days_ago(29)
    with live.db.open() as storage:
        for mower in ("DEVICE_1", "DEVICE_2", "SOLD"):  # the last no longer on the account
            write_rows(storage, [replace(row, mower_id=mower) for row in rows_at(old)])
            write_rows(storage, [replace(row, mower_id=mower) for row in rows_at(kept)])

    removed_as_of(live, NOW)

    assert expiring(live) == dict.fromkeys(EXPIRING_TABLES, [kept] * 3)


def test_a_gap_made_longer_while_old_rows_are_removed_is_kept(live: Live) -> None:
    # A collector that died before noting its reconnection records the same gap again when
    # it restarts, as ending now: just when a removal is first made.
    first, second, third = days_ago(60), days_ago(50), days_ago(40)
    with live.db.open() as storage, ThreadPoolExecutor() as removal:
        write_rows(
            storage,
            [
                Gap("DEVICE_1", began, began + timedelta(hours=1), GapReason.RECONNECT)
                for began in (first, second, third)
            ],
        )
        with psycopg.connect(live.db.dsn) as recording:
            recording.execute(
                "UPDATE collector_gap SET end_time = %s WHERE start_time = %s", (at(NOW), first)
            )
            # Two rows a statement: the first finds the gap being recorded again, and waits.
            removed = removal.submit(storage.remove_older_than, days_ago(30), 2)
            deadline = time.monotonic() + 4
            while not live.db.held():
                assert time.monotonic() < deadline, "the removal never waited for the gap"
                time.sleep(0.01)

        assert removed.result() == 2  # the statement that removed one fewer was not the last
    assert stored(live, "collector_gap", "end_time") == [at(NOW)]


def test_a_removal_the_database_never_answers_is_said_and_not_made_again(
    live: Live, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(postgres, "STATEMENT_TIMEOUT_MS", 0)
    with live.db.open() as storage:
        write_rows(storage, rows_at(days_ago(31)))

    async def scenario() -> None:
        collector = live.start(retention_days=30)
        assert live.retention is not None
        with live.db.suspended():
            await collector.tick()
            await until(live.db.held)
            connections = live.db.attempts
            for hours, said in ((23, 0), (24, 1), (24.1, 1), (47, 1), (48, 2)):
                live.clock.now = NOW + hours * HOUR
                await collector.tick()
                assert caplog.text.count("still waiting") == said
            assert live.db.attempts == connections
        live.retention.join()

    asyncio.run(scenario())


def test_a_clock_set_back_does_not_put_the_next_removal_off(live: Live) -> None:
    # The schedule goes by time elapsed; only what counts as old goes by the date.
    elapsed, old = Clock(), days_ago(45)
    removal = Retention(live.db.open, 30, clock=elapsed)
    with live.db.open():
        removal.remove_if_due(NOW)
        removal.join()
    with live.db.open() as storage:
        write_rows(storage, rows_at(old))

    elapsed.now = 24 * HOUR
    removal.remove_if_due(NOW - 7 * 24 * HOUR)  # a day on, the clock has been set back a week
    removal.join()

    assert stored(live, "trail_point", "device_time") == []


def test_a_removal_that_fails_in_a_way_nobody_foresaw_is_logged_and_tried_again_the_next_day(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    elapsed, opened = Clock(), 0

    def opener() -> Storage:
        nonlocal opened
        opened += 1
        if opened == 1:
            raise RuntimeError("a fault in the driver")
        return live.db.open()

    with live.db.open() as storage:
        write_rows(storage, rows_at(days_ago(31)))
    removal = Retention(opener, 30, clock=elapsed)

    removal.remove_if_due(NOW)
    removal.join()
    assert "Could not remove rows older than" in caplog.text

    elapsed.now = 24 * HOUR
    removal.remove_if_due(NOW + 24 * HOUR)
    removal.join()
    assert stored(live, "trail_point", "device_time") == []
