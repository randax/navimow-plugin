"""What every storage backend must do alike, asked of each where they differ.

The replay suite says what a mowing session becomes, against PostgreSQL alone. This one
writes through the storage adapter and reads back as a dashboard would, in the backend's
own query language.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from navimow_collector.config import Secret, StorageConfig
from navimow_collector.records import Job, Mower, TrailPoint
from navimow_collector.storage import SchemaError, Storage, open_storage, write_rows
from navimow_collector.storage.retention import BATCH_ROWS

from .conftest import fresh_database
from .test_retention import rows_at


@dataclass(frozen=True)
class Relational:
    """A database of the relational shape, reached as its dashboards reach it: by SQL."""

    name: str
    dsn: str

    @property
    def config(self) -> StorageConfig:
        return StorageConfig(backend="postgres", dsn=Secret(self.dsn))

    def open(self, **settings: object) -> Storage:
        return open_storage(replace(self.config, **settings))  # type: ignore[arg-type]

    def stored(self, table: str, time: str = "device_time") -> list[datetime]:
        """When each row of a table is from, oldest first."""
        with psycopg.connect(self.dsn) as conn:
            return [row[0] for row in conn.execute(f"SELECT {time} FROM {table} ORDER BY {time}")]

    def a_day_passes(self, storage: Storage, keep_days: int) -> None:
        """Have what is older than the owner keeps removed now, as it is once a day: by the
        collector, and by the engine wherever the collector gave it the rule to apply."""
        storage.remove_older_than(datetime.now(UTC) - timedelta(days=keep_days), BATCH_ROWS)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            for job in self.policies():
                conn.execute("CALL run_job(%s)", (job,))

    def policies(self) -> dict[int, tuple[str, timedelta]]:
        """TimescaleDB's retention policies: the table each is of, and how long it keeps.
        None where there is no TimescaleDB to have any."""
        with psycopg.connect(self.dsn) as conn:
            installed = "SELECT FROM pg_extension WHERE extname = 'timescaledb'"
            if conn.execute(installed).fetchone() is None:  # a row of no columns, if it is
                return {}
            return {
                job: (table, keep)
                for job, table, keep in conn.execute(
                    "SELECT job_id, hypertable_name, (config->>'drop_after')::interval"
                    " FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention'"
                )
            }

    def hypertables(self) -> set[str]:
        with psycopg.connect(self.dsn) as conn:
            found = conn.execute("SELECT hypertable_name FROM timescaledb_information.hypertables")
            return {row[0] for row in found}

    def trail(self, mower_id: str, job_id: str) -> list[tuple[datetime, float]]:
        """One Job's Trail as the bundled dashboard asks for it: when, and how far along x."""
        with psycopg.connect(self.dsn) as conn:
            return conn.execute(
                "SELECT device_time, x FROM trail_point WHERE mower_id = %s AND job_id = %s"
                " ORDER BY device_time",
                (mower_id, job_id),
            ).fetchall()


@pytest.fixture(params=["postgres", "timescaledb"])
def backend(request: pytest.FixtureRequest) -> Iterator[Relational]:
    """Each backend in turn, empty."""
    servers = {"postgres": "postgres_server", "timescaledb": "timescale_server"}
    server: str = request.getfixturevalue(servers[request.param])
    with fresh_database(server) as dsn:
        yield Relational(request.param, dsn)


@pytest.fixture
def timescale(timescale_server: str) -> Iterator[Relational]:
    """An empty TimescaleDB database, for what only TimescaleDB does."""
    with fresh_database(timescale_server) as dsn:
        yield Relational("timescaledb", dsn)


# The tables whose rows expire, and the column that says when each row is from.
EXPIRING_TABLES = {
    "trail_point": "device_time",
    "job_progress": "device_time",
    "mower_state": "device_time",
    "collector_gap": "start_time",
}
READINGS = {"trail_point", "job_progress", "mower_state"}


def test_an_empty_database_is_given_its_schema(backend: Relational) -> None:
    with pytest.raises(SchemaError):
        backend.open(migrate=False)  # nothing is there, and nothing was put there

    with backend.open():
        pass
    with backend.open(), backend.open(migrate=False):  # again changes nothing
        pass

    assert backend.stored("trail_point") == []


def point(
    when: datetime, x: float, job_id: str | None = None, mower: str = "DEVICE_1"
) -> TrailPoint:
    return TrailPoint(mower, when, when + timedelta(seconds=1), x, 2.0, 0.5, 4, job_id, 3)


def test_a_jobs_trail_reads_back_in_order_however_and_whenever_it_was_written(
    backend: Relational,
) -> None:
    # A Job of years ago, as a capture replayed brings it, and one of today; each written
    # latest point first, the two mixed, and a late point of the old Job on its own.
    then, today = datetime(2019, 6, 1, 12, tzinfo=UTC), datetime.now(UTC).replace(microsecond=0)
    old = [point(then + timedelta(seconds=2 * i), float(i), "old") for i in range(5)]
    new = [point(today + timedelta(seconds=2 * i), 10.0 + i, "new") for i in range(5)]
    late, old = old[2], old[:2] + old[3:]
    mixed = [row for pair in zip(reversed(new), reversed(old), strict=False) for row in pair]

    with backend.open() as storage:
        assert storage.write_trail([*mixed, new[0]]) == 9
        assert storage.write_trail([late]) == 1
        assert storage.write_trail([point(today, 99.0, "other", mower="DEVICE_2")]) == 1

    assert backend.trail("DEVICE_1", "old") == [
        (then + timedelta(seconds=2 * i), float(i)) for i in range(5)
    ]
    assert backend.trail("DEVICE_1", "new") == [
        (today + timedelta(seconds=2 * i), 10.0 + i) for i in range(5)
    ]


def test_a_batch_of_rows_is_stored_whole_and_once(backend: Relational) -> None:
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    batch = [point(start + timedelta(seconds=2 * i), float(i), "job") for i in range(1200)]

    with backend.open() as storage:
        assert storage.write_trail(batch) == 1200
        assert storage.write_trail(batch[400:800]) == 0  # delivered again: stored already

    assert len(backend.trail("DEVICE_1", "job")) == 1200


def test_a_job_reads_back_as_it_was_written(backend: Relational) -> None:
    # A collector starting up carries on from each mower's latest Job as the database has it.
    start = datetime(2026, 9, 30, 12, 0, 0, 250000, tzinfo=UTC)
    under_way = Job("DEVICE_1", "1790769600", start, start + timedelta(minutes=5))
    finished = Job(
        "DEVICE_2",
        "1790769601",
        start,
        start + timedelta(hours=4),
        end_time=start + timedelta(hours=4),
        completed=True,
        mowing_percentage=100,
        area=412.5,
        arrival_x=-0.288,
        arrival_y=-0.446,
        arrival_theta=1.039,
        zones=(1, 6, 7, 9, 10, 11),
    )
    earlier = replace(finished, job_id="1790000000", start_time=start - timedelta(days=9))

    with backend.open() as storage:
        assert storage.write_jobs([under_way, earlier, finished]) == 3

    with backend.open() as storage:
        assert sorted(storage.latest_jobs(), key=lambda job: job.mower_id) == [under_way, finished]


def test_rows_older_than_the_owner_keeps_are_removed_and_jobs_are_not(backend: Relational) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    old, kept = now - timedelta(days=40), now - timedelta(days=1)

    with backend.open(retention_days=30) as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        write_rows(storage, [Job("DEVICE_1", "old", old, old, end_time=old, completed=True)])
        write_rows(storage, [Mower("DEVICE_1", "Mower", "X420", "005D", old)])
        backend.a_day_passes(storage, keep_days=30)

    assert {table: backend.stored(table, time) for table, time in EXPIRING_TABLES.items()} == (
        dict.fromkeys(EXPIRING_TABLES, [kept])
    )
    assert backend.stored("job", "start_time") == [old]
    assert backend.stored("mower", "updated_time") == [old]


def test_timescaledb_holds_the_readings_in_hypertables(timescale: Relational) -> None:
    with timescale.open():
        pass
    with timescale.open():  # again changes nothing
        pass

    assert timescale.hypertables() == READINGS


def test_timescaledb_is_given_the_rule_of_what_to_keep(timescale: Relational) -> None:
    def kept() -> dict[str, timedelta]:
        return dict(timescale.policies().values())

    with timescale.open():
        assert kept() == {}  # everything, unless the owner says otherwise
    with timescale.open(retention_days=30):
        assert kept() == dict.fromkeys(READINGS, timedelta(days=30))
        rule = timescale.policies()
    with timescale.open(retention_days=30):
        assert timescale.policies() == rule  # the same rule is not made anew
    with timescale.open(retention_days=365):
        assert kept() == dict.fromkeys(READINGS, timedelta(days=365))
    with timescale.open(retention_days=365, migrate=False):
        pass
    with timescale.open(migrate=False):
        assert kept() == dict.fromkeys(READINGS, timedelta(days=365))  # not this one's to change
    with timescale.open(retention_days=10**9):
        assert kept() == {}  # for longer than dates go back, which is to keep everything
    with timescale.open(retention_days=7), timescale.open():
        assert kept() == {}  # the owner keeps everything again


def test_in_timescaledb_it_is_the_engine_that_removes_old_readings(timescale: Relational) -> None:
    # Were the rule not given, or not applied, the readings would stay: the collector itself
    # removes nothing from a hypertable.
    now = datetime.now(UTC).replace(microsecond=0)
    old, kept = now - timedelta(days=40), now - timedelta(days=1)

    with timescale.open(retention_days=30) as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        storage.remove_older_than(now - timedelta(days=30), BATCH_ROWS)

        assert timescale.stored("trail_point") == [old, kept]
        assert timescale.stored("collector_gap", "start_time") == [kept]  # no hypertable


def test_tables_that_held_rows_before_timescaledb_stay_as_they_are_and_still_expire(
    timescale: Relational,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    old, kept = now - timedelta(days=40), now - timedelta(days=1)
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION timescaledb")
    with timescale.open() as storage:  # a PostgreSQL like any other, for now
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION timescaledb")

    with timescale.open(retention_days=30) as storage:
        timescale.a_day_passes(storage, keep_days=30)

    assert timescale.hypertables() == set()
    assert {table: timescale.stored(table, time) for table, time in EXPIRING_TABLES.items()} == (
        dict.fromkeys(EXPIRING_TABLES, [kept])
    )
