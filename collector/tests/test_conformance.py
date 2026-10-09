"""What every storage backend must do alike, asked of each where they differ.

The replay suite says what a recorded Job becomes, against PostgreSQL alone. This one
writes through the storage adapter, as live collection opens it, and reads back as a
dashboard would, in the backend's own query language.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from navimow_collector.cli import main
from navimow_collector.config import Secret, StorageConfig
from navimow_collector.records import Job, Mower, TrailPoint
from navimow_collector.storage import SchemaError, Storage, open_for_collection, write_rows
from navimow_collector.storage.retention import BATCH_ROWS

from .conftest import FIXTURE, fresh_database
from .test_retention import EXPIRING_TABLES, rows_at


@dataclass(frozen=True)
class Postgres:
    """A PostgreSQL database, TimescaleDB or not, reached as its dashboards reach it."""

    dsn: str

    @property
    def config(self) -> StorageConfig:
        return StorageConfig(backend="postgres", dsn=Secret(self.dsn))

    def open(self, **settings: object) -> Storage:
        """The storage as `collect` opens it, with these settings of the owner's."""
        return open_for_collection(replace(self.config, **settings))  # type: ignore[arg-type]

    def stored(self, table: str, time: str = "device_time") -> list[datetime]:
        """When each row of a table is from, oldest first."""
        with psycopg.connect(self.dsn) as conn:
            return [row[0] for row in conn.execute(f"SELECT {time} FROM {table} ORDER BY {time}")]

    def a_day_passes(self, storage: Storage, keep_days: int) -> None:
        """Have what is older than the owner keeps removed now, as it is once a day: by the
        collector, and by the engine wherever the collector gave it the rule to apply."""
        storage.remove_older_than(datetime.now(UTC) - timedelta(days=keep_days), BATCH_ROWS)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            for policy in self.policies():
                conn.execute("CALL run_job(%s)", (policy,))

    def policies(self) -> dict[int, tuple[str, timedelta]]:
        """TimescaleDB's retention policies by their job: the table each is of, and how
        long it keeps. No policies where there is no TimescaleDB to have any."""
        with psycopg.connect(self.dsn) as conn:
            installed = "SELECT FROM pg_extension WHERE extname = 'timescaledb'"
            if conn.execute(installed).fetchone() is None:  # a row of no columns, if it is
                return {}
            return {
                policy: (table, keep)
                for policy, table, keep in conn.execute(
                    "SELECT job_id, hypertable_name, (config->>'drop_after')::interval"
                    " FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention'"
                )
            }

    def hypertables(self) -> set[str]:
        """The tables TimescaleDB holds as hypertables."""
        with psycopg.connect(self.dsn) as conn:
            found = conn.execute("SELECT hypertable_name FROM timescaledb_information.hypertables")
            return {row[0] for row in found}

    def expiring(self) -> dict[str, list[datetime]]:
        """When the rows of each table that expires are from."""
        return {table: self.stored(table, time) for table, time in EXPIRING_TABLES.items()}

    def trail(self, mower_id: str, job_id: str) -> list[tuple[datetime, float]]:
        """One Job's Trail as the bundled dashboard asks for it: when, and how far along x."""
        with psycopg.connect(self.dsn) as conn:
            return conn.execute(
                "SELECT device_time, x FROM trail_point WHERE mower_id = %s AND job_id = %s"
                " ORDER BY device_time",
                (mower_id, job_id),
            ).fetchall()


@pytest.fixture(params=["postgres", "timescaledb"])
def backend(request: pytest.FixtureRequest) -> Iterator[Postgres]:
    """Each backend in turn, empty."""
    servers = {"postgres": "postgres_server", "timescaledb": "timescale_server"}
    server: str = request.getfixturevalue(servers[request.param])
    with fresh_database(server) as dsn:
        yield Postgres(dsn)


@pytest.fixture
def timescale(timescale_server: str) -> Iterator[Postgres]:
    """An empty TimescaleDB database, for what only TimescaleDB does."""
    with fresh_database(timescale_server) as dsn:
        yield Postgres(dsn)


READINGS = {"trail_point", "job_progress", "mower_state"}


def old_and_kept() -> tuple[datetime, datetime]:
    """Two times as of now, which an engine goes by: one older than 30 days, one not."""
    now = datetime.now(UTC).replace(microsecond=0)
    return now - timedelta(days=40), now - timedelta(days=1)


def test_an_empty_database_is_given_its_schema(backend: Postgres) -> None:
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
    backend: Postgres,
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


def test_a_batch_of_rows_is_stored_whole_and_once(backend: Postgres) -> None:
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    batch = [point(start + timedelta(seconds=2 * i), float(i), "job") for i in range(1200)]

    with backend.open() as storage:
        assert storage.write_trail(batch) == 1200
        assert storage.write_trail(batch[400:800]) == 0  # delivered again: stored already

    assert len(backend.trail("DEVICE_1", "job")) == 1200


def test_a_job_reads_back_as_it_was_written(backend: Postgres) -> None:
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

    further = replace(under_way, updated_time=start + timedelta(minutes=10), mowing_percentage=3)

    with backend.open() as storage:
        assert storage.write_jobs([under_way, earlier, finished]) == 3
        assert sorted(storage.latest_jobs(), key=lambda job: job.mower_id) == [under_way, finished]
        assert storage.write_jobs([further]) == 1  # the same Job, as it is known later

    with backend.open() as storage:
        assert sorted(storage.latest_jobs(), key=lambda job: job.mower_id) == [further, finished]


def test_rows_older_than_the_owner_keeps_are_removed_and_jobs_are_not(backend: Postgres) -> None:
    old, kept = old_and_kept()

    with backend.open(retention_days=30) as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        write_rows(storage, [Job("DEVICE_1", "old", old, old, end_time=old, completed=True)])
        write_rows(storage, [Mower("DEVICE_1", "Mower", "X420", "005D", old)])
        backend.a_day_passes(storage, keep_days=30)

    assert backend.expiring() == dict.fromkeys(EXPIRING_TABLES, [kept])
    assert backend.stored("job", "start_time") == [old]
    assert backend.stored("mower", "updated_time") == [old]


def test_timescaledb_holds_the_readings_in_hypertables(timescale: Postgres) -> None:
    with timescale.open():
        pass
    with timescale.open():  # again changes nothing
        pass

    assert timescale.hypertables() == READINGS


def test_timescaledb_is_given_the_rule_of_what_to_keep(timescale: Postgres) -> None:
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


def test_in_timescaledb_it_is_the_engine_that_removes_old_readings(timescale: Postgres) -> None:
    # Were the rule not given, or not applied, the readings would stay: the collector itself
    # removes nothing from a hypertable.
    old, kept = old_and_kept()

    with timescale.open(retention_days=30) as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        storage.remove_older_than(datetime.now(UTC) - timedelta(days=30), BATCH_ROWS)

        assert timescale.stored("trail_point") == [old, kept]
        assert timescale.stored("collector_gap", "start_time") == [kept]  # no hypertable


def test_tables_that_held_rows_before_timescaledb_stay_as_they_are_and_still_expire(
    timescale: Postgres,
) -> None:
    old, kept = old_and_kept()
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION timescaledb")
    with timescale.open() as storage:  # a PostgreSQL like any other, for now
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION timescaledb")

    with timescale.open(retention_days=30) as storage:
        timescale.a_day_passes(storage, keep_days=30)

    assert timescale.hypertables() == set()
    assert timescale.expiring() == dict.fromkeys(EXPIRING_TABLES, [kept])


def test_timescaledb_installed_out_of_the_way_is_used_all_the_same(timescale: Postgres) -> None:
    # An extension kept in a schema of its own, which the collector's connection does not
    # look in: its functions are not found by their bare names.
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION timescaledb")
        conn.execute("CREATE SCHEMA extensions")
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION timescaledb SCHEMA extensions")

    with timescale.open(retention_days=30):
        pass

    assert timescale.hypertables() == READINGS
    assert dict(timescale.policies().values()) == dict.fromkeys(READINGS, timedelta(days=30))


def test_two_collectors_starting_at_once_on_timescaledb_both_start(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # Live collection opens the database again for the day's removal of old rows.
    with timescale.open():
        pass
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        # The Trail's table as it is the moment before either has made a hypertable of it.
        conn.execute("DROP TABLE trail_point")
        conn.execute(
            "CREATE TABLE trail_point (mower_id text, device_time timestamptz,"
            " received_time timestamptz, x float8, y float8, theta float8, vehicle_state integer,"
            " job_id text, zone integer, PRIMARY KEY (mower_id, device_time))"
        )
    with ThreadPoolExecutor() as other, psycopg.connect(timescale.dsn) as first:
        first.execute("SELECT create_hypertable('trail_point', 'device_time')")
        second = other.submit(lambda: timescale.open().close())
        deadline = time.monotonic() + 4
        while not waiting_on_a_lock(timescale):
            assert time.monotonic() < deadline, "the second never waited for the first"
            time.sleep(0.01)
        first.commit()
        second.result()

    assert timescale.hypertables() == READINGS
    assert "TimescaleDB" not in caplog.text  # and the second had nothing to complain of


def waiting_on_a_lock(database: Postgres) -> bool:
    with psycopg.connect(database.dsn) as conn:
        waiting = conn.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() AND wait_event_type = 'Lock'"
        ).fetchone()
    return waiting is not None and waiting[0] > 0


def test_where_timescaledb_takes_no_rule_the_collector_removes_old_rows_itself(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # A TimescaleDB that makes no policies for this user, as one under its Apache licence
    # makes none for anyone: the collector starts all the same.
    old, kept = old_and_kept()
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP ROLE IF EXISTS navimow_no_policies")
        conn.execute("CREATE ROLE navimow_no_policies LOGIN")
        conn.execute("GRANT ALL ON SCHEMA public TO navimow_no_policies")
        conn.execute("REVOKE EXECUTE ON FUNCTION add_retention_policy FROM PUBLIC")
    limited = Postgres(make_conninfo(timescale.dsn, user="navimow_no_policies"))

    try:
        with limited.open(retention_days=30) as storage:
            write_rows(storage, [*rows_at(old), *rows_at(kept)])
            limited.a_day_passes(storage, keep_days=30)
        policies, hypertables = timescale.policies(), timescale.hypertables()
        stored = timescale.expiring()
    finally:
        with psycopg.connect(timescale.dsn, autocommit=True) as conn:
            conn.execute("DROP OWNED BY navimow_no_policies")
            conn.execute("DROP ROLE navimow_no_policies")

    assert "TimescaleDB was not told how long to keep readings" in caplog.text
    assert (hypertables, policies) == (READINGS, {})
    assert stored == dict.fromkeys(EXPIRING_TABLES, [kept])


def test_replay_changes_no_rule_of_timescaledbs(timescale: Postgres, tmp_path: Path) -> None:
    # `replay` removes nothing, whatever the setting: neither does it have the engine do so.
    config = tmp_path / "collector.toml"
    config.write_text(f'[storage]\ndsn = "{timescale.dsn}"\nretention_days = 7\n')

    assert main(["--config", str(config), "replay", str(FIXTURE)]) == 0
    assert timescale.policies() == {}

    with timescale.open(retention_days=30):
        rule = timescale.policies()
    assert main(["--config", str(config), "replay", str(FIXTURE)]) == 0
    assert timescale.policies() == rule
