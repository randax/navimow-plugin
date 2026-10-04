"""Buffering as an operator observes it: a database outage delays rows, it does not lose them."""

from __future__ import annotations

import errno
import os
import socket
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from navimow_collector.config import Secret, StorageConfig
from navimow_collector.records import Gap, GapReason, TrailPoint
from navimow_collector.storage import Storage, StorageError, open_storage, postgres
from navimow_collector.storage.buffered import REPLAY_ROWS, RETRY_SECONDS, BufferedStorage

from .conftest import Clock, gaps

START = datetime(2026, 9, 30, 12, tzinfo=UTC)


class Database:
    """The real PostgreSQL, which a test can take down and bring back."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.down = False
        self.attempts = 0
        self.sent = 0  # rows the database took, repeats included
        self.writes = 0
        self.drop_at_write: int | None = None  # the connection is lost at this write
        self.crash: BaseException | None = None  # raised by the next write, as a dying process

    def open(self) -> Storage:
        self.attempts += 1
        if self.down:
            raise StorageError("postgres: connection refused")
        return Observed(open_storage(StorageConfig(dsn=Secret(self.dsn))), self)

    def go_down(self) -> None:
        """Refuse new connections and kill the open one, as a restarting server does."""
        self.down = True
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )

    def trail(self) -> list[int]:
        """The stored points, as the seconds after START they were recorded at."""
        with psycopg.connect(self.dsn) as conn:
            rows = conn.execute("SELECT device_time FROM trail_point ORDER BY device_time")
            return [int((row[0] - START).total_seconds()) for row in rows]


class Observed:
    """The real storage, through which a test can interrupt a write."""

    def __init__(self, storage: Storage, db: Database) -> None:
        self._storage = storage
        self._db = db

    def __enter__(self) -> Observed:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def migrate(self) -> None:
        self._storage.migrate()

    def check_schema(self) -> None:
        self._storage.check_schema()

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        if self._db.crash:
            raise self._db.crash
        self._db.writes += 1
        if self._db.writes == self._db.drop_at_write:
            raise StorageError("postgres: server closed the connection unexpectedly")
        written = self._storage.write_trail(points)
        self._db.sent += len(points)
        return written

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        if self._db.crash:
            raise self._db.crash
        return self._storage.write_gaps(gaps)

    def close(self) -> None:
        self._storage.close()


def points(*seconds: int) -> list[TrailPoint]:
    return [
        TrailPoint("DEVICE_1", at, at, 1.5, -2.5, 0.25, 4)
        for at in (START + timedelta(seconds=second) for second in seconds)
    ]


def buffered(db: Database, tmp_path: Path, clock: Clock, **limits: int) -> BufferedStorage:
    return BufferedStorage(db.open, tmp_path / "buffer.jsonl", clock=clock, **limits)


def finishes(work: Callable[[], object], within: float) -> bool:
    """Whether `work` returns in time; work that stalls is left behind on its thread."""
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(within)
    return not thread.is_alive()


def test_a_locked_table_delays_rows_rather_than_stalling_the_collector(
    database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(postgres, "STATEMENT_TIMEOUT_MS", 200)
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    storage.write_trail(points(1))

    with psycopg.connect(database) as maintenance:
        maintenance.execute("LOCK TABLE trail_point IN ACCESS EXCLUSIVE MODE")
        assert finishes(lambda: storage.write_trail(points(2)), within=5)
        assert storage.buffered == 1

    clock.now += RETRY_SECONDS
    storage.flush()
    assert db.trail() == [1, 2]


def test_a_statement_timeout_the_operator_set_is_not_shortened(
    database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(postgres, "STATEMENT_TIMEOUT_MS", 200)
    db = Database(make_conninfo(database, options="-c statement_timeout=30000"))
    storage = buffered(db, tmp_path, Clock())
    storage.write_trail(points(1))
    slow = threading.Thread(target=lambda: storage.write_trail(points(2)), daemon=True)

    with psycopg.connect(database) as maintenance:
        maintenance.execute("LOCK TABLE trail_point IN ACCESS EXCLUSIVE MODE")
        slow.start()
        slow.join(1)  # five times the collector's own limit
        assert slow.is_alive()
    slow.join(5)

    assert db.trail() == [1, 2]
    assert storage.buffered == 0


def test_a_database_host_that_never_answers_is_given_up_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postgres, "CONNECT_TIMEOUT_SECONDS", 2)  # the shortest libpq honours
    errors: list[StorageError] = []

    def connect() -> None:
        try:
            open_storage(StorageConfig(dsn=Secret(f"postgresql://nobody@127.0.0.1:{port}/none")))
        except StorageError as error:
            errors.append(error)

    with socket.socket() as silent:  # accepts the connection, then says nothing
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        port = silent.getsockname()[1]
        assert finishes(connect, within=8)

    assert "timeout" in str(errors[0])


def test_rows_written_during_an_outage_arrive_when_the_database_returns(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    gap = Gap("DEVICE_1", START, START + timedelta(minutes=4), GapReason.RECONNECT)

    assert storage.write_trail(points(1)) == 1
    db.go_down()
    storage.write_trail(points(2, 3))
    storage.write_gaps([gap])

    assert db.trail() == [1]
    assert (storage.buffered, storage.reachable) == (3, False)

    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()

    assert db.trail() == [1, 2, 3]
    assert gaps(database) == [(gap.mower_id, gap.start_time, gap.end_time, gap.reason)]
    assert (storage.buffered, storage.reachable) == (0, True)


def test_an_unreachable_database_is_retried_no_more_often_than_the_retry_interval(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    storage.write_trail(points(1))
    db.go_down()
    storage.write_trail(points(2))  # discovers the outage
    attempts = db.attempts

    for second in range(3, 3 + RETRY_SECONDS - 1):  # a mower reports every couple of seconds
        clock.now += 1
        storage.write_trail(points(second))
    assert db.attempts == attempts

    clock.now += 1
    storage.flush()
    assert db.attempts == attempts + 1


def test_a_brief_outage_never_touches_the_disk(database: str, tmp_path: Path) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=3)
    db.go_down()
    for second in (1, 2, 3):
        storage.write_trail(points(second))

    assert not (tmp_path / "buffer.jsonl").exists()


def test_a_long_outage_spills_to_disk_where_it_survives_a_crash(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=3)
    db.go_down()
    for second in (1, 2, 3, 4, 5):  # the fourth row exceeds what memory may hold
        storage.write_trail(points(second))
    assert storage.buffered == 5
    del storage  # the process dies; only the disk remembers

    db.down = False
    restarted = buffered(db, tmp_path, clock, memory_rows=3)
    assert restarted.buffered == 4
    restarted.flush()

    assert db.trail() == [1, 2, 3, 4]
    assert restarted.buffered == 0
    assert not (tmp_path / "buffer.jsonl").exists()


def test_shutting_down_during_an_outage_keeps_what_memory_held(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    gap = Gap("DEVICE_1", START, START + timedelta(minutes=4), GapReason.RESTART)
    db.go_down()
    storage.write_trail(points(1, 2))
    storage.write_gaps([gap])
    storage.close()

    db.down = False
    restarted = buffered(db, tmp_path, clock)
    restarted.flush()

    assert db.trail() == [1, 2]
    assert gaps(database) == [(gap.mower_id, gap.start_time, gap.end_time, gap.reason)]


def test_a_gap_held_during_an_outage_is_on_disk_at_once(database: str, tmp_path: Path) -> None:
    # The collector forgets when a gap started as soon as it has handed the gap over.
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    gap = Gap("DEVICE_1", START, START + timedelta(minutes=4), GapReason.RECONNECT)
    db.go_down()
    storage.write_gaps([gap])
    del storage  # the process dies with the database still away

    db.down = False
    buffered(db, tmp_path, clock).flush()

    assert gaps(database) == [(gap.mower_id, gap.start_time, gap.end_time, gap.reason)]


def test_the_disk_buffer_never_exceeds_its_limit(database: str, tmp_path: Path) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=10, disk_bytes=1000)
    db.go_down()
    storage.write_trail(points(*range(1500)))  # one batch far larger than the limit
    size = (tmp_path / "buffer.jsonl").stat().st_size
    for second in range(1500, 3000):
        storage.write_trail(points(second))

    assert 0 < size <= 1000
    assert (tmp_path / "buffer.jsonl").stat().st_size == size
    assert storage.buffered + storage.dropped == 3000
    assert storage.buffered < 20

    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()
    # The oldest rows are the ones kept: what fitted in the file, then what memory may hold.
    assert db.trail() == list(range(len(db.trail())))
    assert 10 < len(db.trail()) < 20


def full(db: Database, tmp_path: Path, clock: Clock) -> BufferedStorage:
    """A buffer after a long outage: its file at the limit and its memory at its own."""
    storage = buffered(db, tmp_path, clock, memory_rows=100, disk_bytes=100_000)
    db.go_down()
    for second in range(1000):
        storage.write_trail(points(second))
    assert storage.dropped > 0 and REPLAY_ROWS + 100 < storage.buffered < 1000
    return storage


def drained(db: Database, tmp_path: Path, clock: Clock) -> list[int]:
    """What the database holds once the next process has written the buffer file out."""
    restarted = buffered(db, tmp_path, clock)
    while restarted.buffered:
        restarted.flush()
    return db.trail()


def test_a_clean_stop_while_a_full_buffer_drains_saves_what_memory_holds(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = full(db, tmp_path, clock)
    held = storage.buffered

    db.down = False
    storage.close()  # stopped as soon as the database is back, one slice written

    assert len(db.trail()) == REPLAY_ROWS
    assert drained(db, tmp_path, clock) == list(range(held))


def test_rows_arriving_while_a_full_buffer_drains_are_not_dropped(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = full(db, tmp_path, clock)
    held, dropped = storage.buffered, storage.dropped

    db.down = False
    clock.now += RETRY_SECONDS
    for second in range(2000, 2005):
        storage.write_trail(points(second))
    storage.close()

    assert storage.dropped == dropped
    assert drained(db, tmp_path, clock) == [*range(held), *range(2000, 2005)]


def test_the_buffer_file_stays_within_twice_its_limit_however_often_it_is_half_drained(
    database: str, tmp_path: Path
) -> None:
    # Rows already written stay in the file until all of it is: they must not let it grow
    # for ever under a database that keeps coming and going.
    db, clock = Database(database), Clock()
    storage = full(db, tmp_path, clock)
    for outage in range(1, 12):
        db.down = False
        clock.now += RETRY_SECONDS
        storage.flush()  # one slice, then the database is away again
        db.go_down()
        for second in range(outage * 1000, outage * 1000 + 300):
            storage.write_trail(points(second))

    assert 100_000 < (tmp_path / "buffer.jsonl").stat().st_size <= 200_000


def test_rows_a_stop_cannot_save_are_counted_as_dropped(database: str, tmp_path: Path) -> None:
    db = Database(database)
    (tmp_path / "state").write_text("a file where the state directory should be")
    storage = BufferedStorage(db.open, tmp_path / "state" / "buffer.jsonl", clock=Clock())
    db.go_down()
    storage.write_trail(points(1, 2, 3))

    storage.close()

    assert storage.dropped == 3


def test_when_rows_must_be_dropped_a_gap_is_the_last_to_go(database: str, tmp_path: Path) -> None:
    # The collector has already forgotten when a gap began, so nothing can record it again.
    db = Database(database)
    (tmp_path / "state").write_text("a file where the state directory should be")
    storage = BufferedStorage(
        db.open, tmp_path / "state" / "buffer.jsonl", memory_rows=10, clock=Clock()
    )
    gap = Gap("DEVICE_1", START, START + timedelta(seconds=30), GapReason.RECONNECT)
    db.go_down()
    storage.write_trail(points(*range(10)))
    storage.write_gaps([gap])
    storage.write_trail(points(10))
    assert (storage.buffered, storage.dropped) == (10, 2)

    db.down = False
    storage.close()

    assert db.trail() == list(range(9))
    assert gaps(database) == [(gap.mower_id, gap.start_time, gap.end_time, gap.reason)]


def test_a_rejection_is_forgotten_once_its_slice_is_written(database: str, tmp_path: Path) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    impossible = replace(points(5000)[0], vehicle_state=2**40)
    db.go_down()
    storage.write_trail([impossible, *points(*range(3 * REPLAY_ROWS))])
    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()  # the first slice, with the rejected row, is done with
    assert (storage.rejected, storage.buffered) == (1, 2 * REPLAY_ROWS + 1)

    storage.write_trail([impossible])  # delivered again while the backlog is still draining
    while storage.buffered:
        storage.flush()

    assert storage.rejected == 2
    assert db.trail() == list(range(3 * REPLAY_ROWS))


def test_a_gap_the_buffer_file_cannot_take_does_not_cost_the_rows_in_memory(
    database: str, tmp_path: Path
) -> None:
    db = Database(database)
    (tmp_path / "state").write_text("a file where the state directory should be")
    storage = BufferedStorage(db.open, tmp_path / "state" / "buffer.jsonl", clock=Clock())
    gap = Gap("DEVICE_1", START, START + timedelta(seconds=30), GapReason.RECONNECT)
    db.go_down()
    storage.write_trail(points(*range(30)))
    storage.write_gaps([gap])  # goes to disk at once, which fails
    assert (storage.buffered, storage.dropped) == (31, 0)

    db.down = False
    storage.close()

    assert db.trail() == list(range(30))
    assert gaps(database) == [(gap.mower_id, gap.start_time, gap.end_time, gap.reason)]


def test_a_row_the_database_rejects_is_dropped_and_the_rest_arrive(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    impossible = replace(points(2)[0], vehicle_state=2**40)  # no integer column holds it
    db.go_down()
    storage.write_trail(points(1))
    storage.write_trail([impossible])
    storage.write_trail(points(3))

    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()

    assert db.trail() == [1, 3]
    assert (storage.buffered, storage.rejected, storage.reachable) == (0, 1, True)


def test_a_rejected_row_does_not_start_an_outage(database: str, tmp_path: Path) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    impossible = replace(points(2)[0], vehicle_state=2**40)

    assert storage.write_trail([*points(1), impossible, *points(3)]) == 2

    assert db.trail() == [1, 3]
    assert (storage.buffered, storage.rejected, storage.reachable) == (0, 1, True)


def test_a_rejected_row_is_counted_once_however_often_its_batch_is_retried(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    impossible = replace(points(2)[0], vehicle_state=2**40)
    # The batch is rejected, then tried row by row: 1 is written, 2 rejected, and 3 meets
    # an outage, so all three wait and are tried again.
    db.drop_at_write = 4
    storage.write_trail([*points(1), impossible, *points(3)])
    assert (storage.buffered, storage.rejected) == (3, 1)

    clock.now += RETRY_SECONDS
    storage.flush()

    assert db.trail() == [1, 3]
    assert (storage.buffered, storage.rejected) == (0, 1)


def test_rows_spilled_after_a_torn_line_are_not_lost_with_it(database: str, tmp_path: Path) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=1)
    db.go_down()
    storage.write_trail(points(1, 2))
    with (tmp_path / "buffer.jsonl").open("a") as spill:  # a write the disk cut short
        spill.write('{"row": "TrailPoint", "mower_id": "DEVICE_1", "device_ti')
    storage.write_trail(points(3, 4))

    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()

    assert db.trail() == [1, 2, 3, 4]


def test_a_backlog_is_written_a_slice_at_a_time(database: str, tmp_path: Path) -> None:
    # Live collection writes from its event loop, which a long outage's backlog written
    # in one go would hold for minutes.
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=100)
    backlog = 2 * REPLAY_ROWS + 150
    db.go_down()
    for second in range(backlog):
        storage.write_trail(points(second))
    db.down = False
    clock.now += RETRY_SECONDS

    storage.flush()
    assert len(db.trail()) == REPLAY_ROWS
    assert (storage.buffered, storage.draining) == (backlog - REPLAY_ROWS, True)

    storage.flush()
    storage.flush()
    assert db.trail() == list(range(backlog))
    assert (storage.buffered, storage.draining) == (0, False)
    assert not (tmp_path / "buffer.jsonl").exists()


def read_only(self: Path, missing_ok: bool = False) -> None:
    raise OSError(errno.EROFS, "Read-only file system", str(self))


@pytest.mark.parametrize("reclaim", [lambda spill: spill.write_bytes(b""), os.remove])
def test_a_buffer_file_emptied_behind_the_collector_is_read_from_its_start(
    database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reclaim: Callable[[Path], object],
) -> None:
    monkeypatch.setattr(Path, "unlink", read_only)
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=1)
    db.go_down()
    storage.write_trail(points(1, 2))
    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()  # written, but the file could not be removed

    reclaim(tmp_path / "buffer.jsonl")  # so the operator makes room by hand
    db.go_down()
    storage.write_trail(points(3, 4))
    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()

    assert db.trail() == [1, 2, 3, 4]


def test_a_buffer_file_that_cannot_be_removed_neither_stops_collection_nor_repeats(
    database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "unlink", read_only)
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=1)
    for outage in ((1, 2), (3, 4)):
        db.go_down()
        storage.write_trail(points(*outage))
        db.down = False
        clock.now += RETRY_SECONDS
        storage.flush()
        assert storage.buffered == 0

    assert db.trail() == [1, 2, 3, 4]
    assert db.sent == 4


def test_a_buffer_file_that_cannot_be_read_does_not_stop_collection(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=1)
    db.go_down()
    storage.write_trail(points(1, 2))
    (tmp_path / "buffer.jsonl").unlink()
    (tmp_path / "buffer.jsonl").mkdir()  # whatever now sits there, it cannot be read as a file
    storage.write_trail(points(3))

    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()
    storage.write_trail(points(4))
    storage.flush()  # behind a file that still owes rows, a row waits for the next tick

    assert db.trail() == [3, 4]


def test_a_buffer_file_unreadable_for_a_while_is_written_once_it_can_be_read(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=1)
    spill, elsewhere = tmp_path / "buffer.jsonl", tmp_path / "elsewhere"
    storage.write_trail(points(0))
    db.go_down()
    storage.write_trail(points(1, 2))
    spill.rename(elsewhere)
    spill.mkdir()  # for the moment, nothing that can be read as a file
    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()
    assert (storage.buffered, storage.draining) == (2, False)  # still owed, not retried at once

    spill.rmdir()
    elsewhere.rename(spill)
    storage.flush()
    assert db.trail() == [0]  # not before the retry interval has passed

    clock.now += RETRY_SECONDS
    storage.flush()
    assert db.trail() == [0, 1, 2]
    assert storage.buffered == 0


def unreadable(spill: Path) -> Callable[[], None]:
    """Put something that cannot be read as a file where the buffer file is; return how to
    put the file back."""
    aside = spill.with_name("aside")
    spill.rename(aside)
    spill.mkdir()

    def restore() -> None:
        spill.rmdir()
        aside.rename(spill)

    return restore


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def test_a_buffer_file_unreadable_at_startup_still_owes_its_rows(
    database: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    db.go_down()
    storage.write_trail(points(1, 2))
    storage.close()  # the previous process leaves two rows in the file
    restore = unreadable(tmp_path / "buffer.jsonl")
    db.down = False

    restarted = buffered(db, tmp_path, clock)
    assert restarted.buffered > 0
    restarted.flush()
    assert any("cannot be read" in message for message in messages(caplog))

    restore()
    restarted.write_trail(points(3))
    clock.now += RETRY_SECONDS
    restarted.flush()
    restarted.flush()

    assert db.trail() == [1, 2, 3]
    assert restarted.buffered == 0


def test_a_database_outage_is_reported_while_a_buffer_file_is_waiting_to_be_read(
    database: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=1)
    storage.write_trail(points(0))
    db.go_down()
    storage.write_trail(points(1, 2))
    unreadable(tmp_path / "buffer.jsonl")
    db.down = False
    clock.now += RETRY_SECONDS
    storage.write_trail(points(3))
    storage.flush()  # the database is back and takes rows; the file still owes its own
    assert (db.trail(), storage.buffered) == ([0, 3], 2)
    caplog.clear()

    db.go_down()
    clock.now += RETRY_SECONDS
    storage.write_trail(points(4))
    storage.flush()
    clock.now += RETRY_SECONDS
    storage.flush()
    assert len([m for m in messages(caplog) if "Database unreachable" in m]) == 1

    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()
    assert len([m for m in messages(caplog) if "Database reachable again" in m]) == 1
    assert db.trail() == [0, 3, 4]


def test_every_spell_of_an_unreadable_buffer_file_is_reported(
    database: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=1)
    db.go_down()
    storage.write_trail(points(1, 2))
    restore = unreadable(tmp_path / "buffer.jsonl")
    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()  # the first spell, reported
    restore()
    db.go_down()
    clock.now += RETRY_SECONDS
    storage.flush()  # readable again, but now the database is away
    db.down = False
    unreadable(tmp_path / "buffer.jsonl")
    caplog.clear()

    clock.now += RETRY_SECONDS
    storage.flush()

    assert len([m for m in messages(caplog) if "cannot be read" in m]) == 1


def test_a_clean_stop_leaves_only_unsent_rows_for_the_next_process(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=10)
    impossible = replace(points(5000)[0], vehicle_state=2**40)
    db.go_down()
    storage.write_trail([impossible, *points(*range(2 * REPLAY_ROWS))])
    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()  # the first slice, where the rejected row is counted
    storage.close()  # a second slice, then the process stops with one row unsent
    assert (storage.rejected, db.sent) == (1, 2 * REPLAY_ROWS - 1)

    restarted = buffered(db, tmp_path, clock)
    assert restarted.buffered == 1
    while restarted.buffered:
        restarted.flush()

    assert (restarted.rejected, db.sent) == (0, 2 * REPLAY_ROWS)
    assert db.trail() == list(range(2 * REPLAY_ROWS))


def test_a_rejection_is_remembered_while_its_row_still_waits_in_a_later_slice(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    rows = points(*range(2 * REPLAY_ROWS))
    rows[300] = replace(rows[300], vehicle_state=2**40)
    # The batch is rejected and tried row by row: row 300 is rejected and the last row
    # meets an outage, so all of them wait, to be written in two slices.
    db.drop_at_write = 1 + len(rows)
    storage.write_trail(rows)
    assert (storage.buffered, storage.rejected) == (len(rows), 1)

    clock.now += RETRY_SECONDS
    while storage.buffered:
        storage.flush()

    assert storage.rejected == 1
    assert len(db.trail()) == len(rows) - 1


def test_a_line_cut_short_by_a_crash_does_not_block_the_rest(database: str, tmp_path: Path) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=1)
    db.go_down()
    storage.write_trail(points(1, 2))
    with (tmp_path / "buffer.jsonl").open("a") as spill:
        spill.write('{"row": "TrailPoint", "mower_id": "DEVICE_1", "device_ti')

    db.down = False
    restarted = buffered(db, tmp_path, clock)
    restarted.flush()

    assert db.trail() == [1, 2]
    assert restarted.buffered == 0


def test_a_disk_that_cannot_be_written_costs_rows_not_memory(database: str, tmp_path: Path) -> None:
    db = Database(database)
    (tmp_path / "state").write_text("a file where the state directory should be")
    storage = BufferedStorage(
        db.open, tmp_path / "state" / "buffer.jsonl", memory_rows=10, clock=Clock()
    )
    db.go_down()
    for second in range(100):
        storage.write_trail(points(second))

    assert storage.buffered <= 10
    assert storage.dropped >= 90
