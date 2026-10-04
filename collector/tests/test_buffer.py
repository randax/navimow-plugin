"""Buffering as an operator observes it: a database outage delays rows, it does not lose them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from navimow_collector.config import Secret, StorageConfig
from navimow_collector.records import Gap, TrailPoint
from navimow_collector.storage import Storage, StorageError, open_storage
from navimow_collector.storage.buffered import RETRY_SECONDS, BufferedStorage

START = datetime(2026, 9, 30, 12, tzinfo=UTC)


class Clock:
    def __init__(self, now: float = 0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Database:
    """The real PostgreSQL, which a test can take down and bring back."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.down = False
        self.attempts = 0

    def open(self) -> Storage:
        self.attempts += 1
        if self.down:
            raise StorageError("postgres: connection refused")
        return open_storage(StorageConfig(dsn=Secret(self.dsn)))

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

    def gaps(self) -> list[tuple[str, datetime, datetime, str]]:
        with psycopg.connect(self.dsn) as conn:
            return conn.execute(
                "SELECT mower_id, start_time, end_time, reason FROM collector_gap"
            ).fetchall()


def points(*seconds: int) -> list[TrailPoint]:
    return [
        TrailPoint("DEVICE_1", at, at, 1.5, -2.5, 0.25, 4)
        for at in (START + timedelta(seconds=second) for second in seconds)
    ]


def buffered(db: Database, tmp_path: Path, clock: Clock, **limits: int) -> BufferedStorage:
    return BufferedStorage(db.open, tmp_path / "buffer.jsonl", clock=clock, **limits)


def test_rows_written_during_an_outage_arrive_when_the_database_returns(
    database: str, tmp_path: Path
) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock)
    gap = Gap("DEVICE_1", START, START + timedelta(minutes=4), "reconnect")

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
    assert db.gaps() == [(gap.mower_id, gap.start_time, gap.end_time, gap.reason)]
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
    gap = Gap("DEVICE_1", START, START + timedelta(minutes=4), "restart")
    db.go_down()
    storage.write_trail(points(1, 2))
    storage.write_gaps([gap])
    storage.close()

    db.down = False
    restarted = buffered(db, tmp_path, clock)
    restarted.flush()

    assert db.trail() == [1, 2]
    assert db.gaps() == [(gap.mower_id, gap.start_time, gap.end_time, gap.reason)]


def test_the_disk_buffer_is_bounded_too(database: str, tmp_path: Path) -> None:
    db, clock = Database(database), Clock()
    storage = buffered(db, tmp_path, clock, memory_rows=1, disk_bytes=1000)
    db.go_down()
    for second in range(100):
        storage.write_trail(points(second))
    size, held = (tmp_path / "buffer.jsonl").stat().st_size, storage.buffered
    for second in range(100, 200):
        storage.write_trail(points(second))

    assert (tmp_path / "buffer.jsonl").stat().st_size == size < 2000
    assert storage.buffered <= held + 1  # at most the row still in memory
    assert storage.dropped >= 100

    db.down = False
    clock.now += RETRY_SECONDS
    storage.flush()
    kept = db.trail()
    assert kept[:4] == [0, 1, 2, 3] and len(kept) < 100  # the oldest rows are the ones kept


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
