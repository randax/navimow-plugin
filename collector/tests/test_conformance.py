"""What every storage backend must do alike, asked of each where they differ.

The replay suite says what a recorded Job becomes, against PostgreSQL alone. This one
writes through the storage adapter, as live collection opens it, and reads back as a
dashboard would, in the backend's own query language.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from navimow_collector.cli import main
from navimow_collector.config import Secret, StorageConfig
from navimow_collector.records import Job, Mower, TrailPoint
from navimow_collector.storage import (
    SchemaError,
    Storage,
    StorageError,
    open_for_collection,
    write_rows,
)
from navimow_collector.storage.retention import BATCH_ROWS

from .conftest import FIXTURE, fresh_database
from .test_live import Live, stopped_at_its_first_request
from .test_retention import EXPIRING_TABLES, removal_ended, rows_at


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

    def policies(self, schema: str = "public") -> dict[int, tuple[str, timedelta]]:
        """TimescaleDB's retention policies on a schema's tables, by their job: the table
        each is of, and how long it keeps. None where there is no TimescaleDB to have any."""
        with psycopg.connect(self.dsn) as conn:
            installed = "SELECT FROM pg_extension WHERE extname = 'timescaledb'"
            if conn.execute(installed).fetchone() is None:  # a row of no columns, if it is
                return {}
            return {
                policy: (table, timedelta(seconds=float(seconds)))
                for policy, table, seconds in conn.execute(
                    "SELECT job_id, hypertable_name,"
                    " extract(epoch FROM (config->>'drop_after')::interval)"
                    " FROM timescaledb_information.jobs"
                    " WHERE proc_name = 'policy_retention' AND hypertable_schema = %s",
                    (schema,),
                )
            }

    def rules(self) -> dict[str, list[str | None]]:
        """Each table's retention policies as they were written, where one is by age."""
        rules: dict[str, list[str | None]] = {}
        with psycopg.connect(self.dsn) as conn:
            for table, rule in conn.execute(
                "SELECT hypertable_name, config->>'drop_after' FROM timescaledb_information.jobs"
                " WHERE proc_name = 'policy_retention' ORDER BY job_id"
            ):
                rules.setdefault(table, []).append(rule)
        return rules

    def hypertables(self, schema: str = "public") -> set[str]:
        """The tables of a schema that TimescaleDB holds as hypertables."""
        with psycopg.connect(self.dsn) as conn:
            found = conn.execute(
                "SELECT hypertable_name FROM timescaledb_information.hypertables"
                " WHERE hypertable_schema = %s",
                (schema,),
            )
            return {row[0] for row in found}

    def looking_in(self, *schemas: str) -> Postgres:
        """The same database, by a connection that looks for tables in these schemas."""
        return Postgres(make_conninfo(self.dsn, options=f"-csearch_path={','.join(schemas)}"))

    @contextmanager
    def another_user(self) -> Iterator[Postgres]:
        """The same database as a user who is no superuser, gone again afterwards with
        whatever is theirs."""
        name = f"navimow_{uuid.uuid4().hex[:12]}"  # users are the server's, not the database's
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(f"CREATE ROLE {name} LOGIN")
            conn.execute(f"GRANT ALL ON SCHEMA public TO {name}")
        try:
            yield Postgres(make_conninfo(self.dsn, user=name))
        finally:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(f"DROP OWNED BY {name}")
                conn.execute(f"DROP ROLE {name}")

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
        if request.param == "timescaledb":
            with_timescaledb(dsn)
        yield Postgres(dsn)


@pytest.fixture
def timescale(timescale_server: str) -> Iterator[Postgres]:
    """An empty TimescaleDB database, for what only TimescaleDB does."""
    with fresh_database(timescale_server) as dsn:
        with_timescaledb(dsn)
        yield Postgres(dsn)


def with_timescaledb(dsn: str) -> None:
    """Install TimescaleDB in a database, unless the server put it there as it made it."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")


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
    # removes nothing from a table TimescaleDB keeps. Shown with readings its rule keeps and
    # the collector, asked to, would not: TimescaleDB applies its rule when it pleases.
    week_old = datetime.now(UTC).replace(microsecond=0) - timedelta(days=7)

    with timescale.open(retention_days=30) as storage:
        write_rows(storage, rows_at(week_old))
        storage.remove_older_than(datetime.now(UTC) - timedelta(days=1), BATCH_ROWS)

    assert timescale.stored("trail_point") == [week_old]
    assert timescale.stored("collector_gap", "start_time") == []  # no table of TimescaleDB's


@pytest.mark.parametrize("holding_rows", [True, False], ids=["holding rows", "empty"])
def test_tables_made_before_timescaledb_was_there_stay_as_they_are_and_still_expire(
    timescale: Postgres, caplog: pytest.LogCaptureFixture, holding_rows: bool
) -> None:
    # Only a table the collector is making is made a hypertable. One that is there may be
    # written to by a collector at work, and a row arriving as TimescaleDB takes the table
    # over would be lost to every reader: so not even an empty one is touched.
    old, kept = old_and_kept()
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION timescaledb")
    with timescale.open() as storage:  # a PostgreSQL like any other, for now
        write_rows(storage, [*rows_at(old), *rows_at(kept)] if holding_rows else [])
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION timescaledb")

    with timescale.open(retention_days=30) as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        timescale.a_day_passes(storage, keep_days=30)

    assert timescale.hypertables() == set()
    assert "TimescaleDB" not in caplog.text  # left alone, not tried and refused
    assert timescale.expiring() == dict.fromkeys(EXPIRING_TABLES, [kept])


def test_a_table_that_was_there_and_is_migrated_further_stays_as_it_is(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # A database from before the Trail's table had its Job and Zone: the migration that adds
    # them changes the table, and does not make it.
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION timescaledb")
    with timescale.open():
        pass
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("ALTER TABLE trail_point DROP COLUMN job_id, DROP COLUMN zone")
        conn.execute("DELETE FROM schema_version WHERE version = 3")
        conn.execute("CREATE EXTENSION timescaledb")

    with timescale.open(retention_days=30) as storage:
        assert storage.write_trail([point(datetime.now(UTC), 1.0, "job")]) == 1

    assert timescale.hypertables() == set()
    assert "TimescaleDB" not in caplog.text


def test_a_table_its_owner_makes_a_hypertable_of_is_given_the_rule_like_any(
    timescale: Postgres,
) -> None:
    # As the README has an owner do it, with the collector stopped.
    old, kept = old_and_kept()
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION timescaledb")
    with timescale.open() as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION timescaledb")
        for table in sorted(READINGS):
            conn.execute(
                "SELECT create_hypertable(%s, 'device_time', migrate_data => true,"
                " create_default_indexes => false)",
                (table,),
            )

    with timescale.open(retention_days=30) as storage:
        timescale.a_day_passes(storage, keep_days=30)

    assert timescale.rules() == dict.fromkeys(READINGS, ["30 days"])
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
        assert timescale.hypertables() == READINGS
        assert timescale.rules() == dict.fromkeys(READINGS, ["30 days"])
    with timescale.open(retention_days=60):
        assert timescale.rules() == dict.fromkeys(READINGS, ["60 days"])
    with timescale.open():
        assert timescale.rules() == {}


def test_collectors_starting_at_once_on_an_empty_database_all_start(
    backend: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # Each would make the tables, and one be told they are there: they take their turn,
    # and see what the one before did, even where a transaction reads as of its start.
    with psycopg.connect(backend.dsn, autocommit=True) as conn:
        conn.execute(
            f'ALTER DATABASE "{conn.info.dbname}"'
            " SET default_transaction_isolation = 'repeatable read'"
        )
    with ThreadPoolExecutor(max_workers=8) as collectors:
        opened = [collectors.submit(lambda: backend.open().close()) for _ in range(8)]
        for each in opened:
            each.result()

    assert backend.stored("trail_point") == []
    assert "TimescaleDB" not in caplog.text


def test_collectors_starting_at_once_on_an_empty_timescaledb_make_each_hypertable_once(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    with ThreadPoolExecutor(max_workers=8) as collectors:
        opened = [
            collectors.submit(lambda: timescale.open(retention_days=30).close()) for _ in range(8)
        ]
        for each in opened:
            each.result()

    assert timescale.hypertables() == READINGS
    assert sorted(table for table, _ in timescale.policies().values()) == sorted(READINGS)
    assert "TimescaleDB" not in caplog.text


def test_where_timescaledb_takes_no_rule_the_collector_removes_old_rows_itself(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # A TimescaleDB that makes no policies for this user, as one under its Apache licence
    # makes none for anyone: the collector starts all the same.
    old, kept = old_and_kept()
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("REVOKE EXECUTE ON FUNCTION add_retention_policy FROM PUBLIC")

    with timescale.another_user() as limited:
        with limited.open(retention_days=30) as storage:
            write_rows(storage, [*rows_at(old), *rows_at(kept)])
            limited.a_day_passes(storage, keep_days=30)
        policies, hypertables = timescale.policies(), timescale.hypertables()
        stored = timescale.expiring()

    assert "TimescaleDB's retention policies could not be made" in caplog.text
    assert (hypertables, policies) == (READINGS, {})
    assert stored == dict.fromkeys(EXPIRING_TABLES, [kept])


def test_where_timescaledb_makes_no_hypertable_the_collector_starts_all_the_same(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # A user TimescaleDB makes no hypertables for: the tables are made and stay ordinary.
    old, kept = old_and_kept()
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        makers = conn.execute(
            "SELECT oid::regprocedure::text FROM pg_proc WHERE proname = 'create_hypertable'"
        ).fetchall()
        for (maker,) in makers:
            conn.execute(f"REVOKE EXECUTE ON FUNCTION {maker} FROM PUBLIC")

    with timescale.another_user() as limited:
        with limited.open(retention_days=30) as storage:
            write_rows(storage, [*rows_at(old), *rows_at(kept)])
            limited.a_day_passes(storage, keep_days=30)
        hypertables, stored = timescale.hypertables(), timescale.expiring()

    assert "TimescaleDB made no hypertables of the readings" in caplog.text
    assert hypertables == set()
    assert stored == dict.fromkeys(EXPIRING_TABLES, [kept])


def test_where_timescaledbs_policies_cannot_be_read_old_rows_are_removed_all_the_same(
    timescale: Postgres,
) -> None:
    old, kept = old_and_kept()
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("REVOKE ALL ON SCHEMA timescaledb_information FROM PUBLIC")

    with timescale.another_user() as limited:
        with limited.open(retention_days=30) as storage:
            write_rows(storage, [*rows_at(old), *rows_at(kept)])
            storage.remove_older_than(datetime.now(UTC) - timedelta(days=30), BATCH_ROWS)
        stored = timescale.expiring()

    assert stored == dict.fromkeys(EXPIRING_TABLES, [kept])


def test_two_collectors_starting_at_once_leave_timescaledb_one_rule_a_table(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    with timescale.open(), psycopg.connect(timescale.dsn, autocommit=True) as conn:
        # The harder case: each sees the database as it was when its transaction began.
        conn.execute(
            f'ALTER DATABASE "{conn.info.dbname}"'
            " SET default_transaction_isolation = 'repeatable read'"
        )

    with ThreadPoolExecutor(max_workers=8) as collectors:
        opened = [
            collectors.submit(lambda: timescale.open(retention_days=30).close()) for _ in range(8)
        ]
        for each in opened:
            each.result()

    assert sorted(table for table, _ in timescale.policies().values()) == sorted(READINGS)
    assert "TimescaleDB" not in caplog.text


def test_rules_timescaledb_was_given_twice_over_are_put_right(timescale: Postgres) -> None:
    # As two collectors of an earlier version could leave them, each having made its own
    # before either had finished: every one of them is replaced, not the first found.
    with timescale.open(), ExitStack() as both:
        makers = [both.enter_context(psycopg.connect(timescale.dsn)) for _ in range(2)]
        for maker in makers:
            maker.execute("SELECT add_retention_policy('trail_point', INTERVAL '30 days')")
    assert timescale.rules()["trail_point"] == ["30 days", "30 days"]

    with timescale.open(retention_days=30):  # the very age both were made for
        assert timescale.rules() == dict.fromkeys(READINGS, ["30 days"])
    with timescale.open(retention_days=60):
        assert timescale.rules() == dict.fromkeys(READINGS, ["60 days"])
    with timescale.open():
        assert timescale.rules() == {}


def test_a_rule_of_timescaledbs_that_is_not_the_owners_number_of_days_is_replaced(
    timescale: Postgres,
) -> None:
    # A month is not 30 days to TimescaleDB, which counts by the calendar; and a rule by
    # when a chunk was made is no rule by age at all.
    with timescale.open(), psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("SELECT add_retention_policy('trail_point', INTERVAL '1 month')")
        conn.execute(
            "SELECT add_retention_policy('job_progress', drop_created_before => INTERVAL '30 days')"
        )
    assert timescale.rules() == {"trail_point": ["1 mon"], "job_progress": [None]}

    with timescale.open(retention_days=30):
        assert timescale.rules() == dict.fromkeys(READINGS, ["30 days"])

    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("SELECT remove_retention_policy('job_progress')")
        conn.execute(
            "SELECT add_retention_policy('job_progress', drop_created_before => INTERVAL '30 days')"
        )
    with timescale.open():
        assert timescale.rules() == {}  # everything is kept, by whatever rule it was not


def test_a_rule_of_timescaledbs_that_was_set_aside_is_put_back_to_work(
    timescale: Postgres,
) -> None:
    # A policy that is there and does nothing: the collector leaves the table to it, so
    # nobody would remove old rows.
    def at_work() -> list[bool]:
        with psycopg.connect(timescale.dsn) as conn:
            asked = conn.execute(
                "SELECT scheduled FROM timescaledb_information.jobs"
                " WHERE proc_name = 'policy_retention'"
            )
            return [row[0] for row in asked]

    with timescale.open(retention_days=30), psycopg.connect(timescale.dsn) as conn:
        for policy in timescale.policies():
            conn.execute("SELECT alter_job(%s, scheduled => false)", (policy,))
    assert at_work() == [False, False, False]

    with timescale.open(retention_days=30):
        assert at_work() == [True, True, True]
        assert timescale.rules() == dict.fromkeys(READINGS, ["30 days"])


def test_what_else_timescaledb_does_with_the_collectors_tables_is_left_to_it(
    timescale: Postgres,
) -> None:
    # A policy of the owner's that is not about keeping: the collector neither removes it
    # with its own, nor takes it for one and leaves old rows to it.
    def compressing() -> int:
        with psycopg.connect(timescale.dsn) as conn:
            jobs = "SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name = %s"
            found = conn.execute(jobs, ("policy_compression",)).fetchone()
            return int(found[0]) if found else 0

    old, kept = old_and_kept()
    with timescale.open(), psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("ALTER TABLE trail_point SET (timescaledb.compress)")
        conn.execute("SELECT add_compression_policy('trail_point', INTERVAL '90 days')")

    for days in (30, 60, None):
        with timescale.open(retention_days=days):
            assert compressing() == 1
    with timescale.open() as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        storage.remove_older_than(datetime.now(UTC) - timedelta(days=30), BATCH_ROWS)

    assert timescale.stored("trail_point") == [kept]


def test_old_gaps_are_the_collectors_to_remove_whatever_timescaledb_holds_them_in(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # Its rule is for the readings. A gap is as old as its end, which is no time TimescaleDB
    # keeps a table by.
    old, kept = old_and_kept()
    with timescale.open(), psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("SELECT create_hypertable('collector_gap', 'start_time')")
        conn.execute("SELECT add_retention_policy('collector_gap', INTERVAL '400 days')")

    with timescale.open(retention_days=30) as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        timescale.a_day_passes(storage, keep_days=30)

    assert timescale.expiring() == dict.fromkeys(EXPIRING_TABLES, [kept])
    assert timescale.rules() == {"collector_gap": ["400 days"]} | dict.fromkeys(
        READINGS, ["30 days"]
    )
    assert "TimescaleDB" not in caplog.text


def test_timescaledb_set_to_write_intervals_another_way_is_opened_all_the_same(
    timescale: Postgres,
) -> None:
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute(
            "ALTER DATABASE current SET IntervalStyle = 'iso_8601'".replace(
                "current", conn.info.dbname
            )
        )

    with timescale.open(retention_days=30), timescale.open(retention_days=30):
        rules = timescale.policies()
    with timescale.open(retention_days=30) as storage:
        assert storage.remove_older_than(datetime.now(UTC), BATCH_ROWS) == 0

    assert timescale.policies() == rules and len(rules) == 3


def test_timescaledb_is_told_of_the_collectors_tables_wherever_it_finds_them(
    timescale: Postgres,
) -> None:
    # A connection that looks in a schema of its own first, and finds the collector's
    # tables in the next: they are its tables all the same, and so are their rules.
    old, kept = old_and_kept()
    with timescale.open(retention_days=365), psycopg.connect(timescale.dsn) as conn:
        conn.execute("CREATE SCHEMA mine")
    elsewhere = timescale.looking_in("mine", "public")

    with elsewhere.open(retention_days=30, migrate=False) as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        storage.remove_older_than(datetime.now(UTC) - timedelta(days=30), BATCH_ROWS)
    assert timescale.stored("trail_point") == [old, kept]  # the engine's, by its own rule
    assert timescale.stored("collector_gap", "start_time") == [kept]  # and the collector's


def test_tables_made_beside_others_of_their_names_are_made_hypertables_all_the_same(
    timescale: Postgres,
) -> None:
    # A connection that makes its tables in a schema of its own, and can see in another
    # the tables an earlier collector made: these are new, whatever is in sight.
    with timescale.open(retention_days=7), psycopg.connect(timescale.dsn) as conn:
        conn.execute("CREATE SCHEMA mine")

    with timescale.looking_in("mine", "public").open(retention_days=30):
        pass

    assert timescale.hypertables("mine") == READINGS
    in_mine = dict(timescale.policies("mine").values())
    assert in_mine == dict.fromkeys(READINGS, timedelta(days=30))
    assert dict(timescale.policies().values()) == dict.fromkeys(READINGS, timedelta(days=7))


def test_a_table_that_went_missing_is_no_table_to_make_a_hypertable_of(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # Dropped by hand, its migration long applied: not the collector's to make again, and
    # nothing for TimescaleDB to be asked about.
    with timescale.open(), psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE mower_state")

    with pytest.raises(SchemaError):
        timescale.open(migrate=False)
    with timescale.open(retention_days=30):
        pass

    assert "TimescaleDB" not in caplog.text


def test_a_collector_kept_waiting_for_its_turn_gives_up_whatever_the_owner_set(
    backend: Postgres,
) -> None:
    # A statement may take as long as the owner says. Waiting for another collector may
    # not: a start that hangs says nothing of why.
    with backend.open():
        pass
    patient = Postgres(make_conninfo(backend.dsn, options="-cstatement_timeout=0"))
    # The one holding its turn lets go before the wait for the other is given up on here,
    # so that a collector which would wait for ever fails this test and does not hang it.
    with ThreadPoolExecutor() as waiting, psycopg.connect(backend.dsn) as holding:
        holding.execute("SELECT pg_advisory_xact_lock(hashtext('navimow_collector.schema'))")
        opening = waiting.submit(lambda: patient.open().close())
        with pytest.raises(StorageError):
            opening.result(timeout=40)

    with patient.open():
        pass


def test_the_collect_command_gives_timescaledb_the_owners_rule(
    timescale: Postgres, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = Live(timescale.dsn, tmp_path)
    config = tmp_path / "collector.toml"
    config.write_text(f'[storage]\ndsn = "{timescale.dsn}"\nretention_days = 30\n')
    monkeypatch.setenv("NAVIMOW_AUTH_STATE_FILE", str(live.store.path))
    monkeypatch.setenv("NAVIMOW_COLLECTOR_STATE_DIR", str(live.state))
    monkeypatch.setenv("NAVIMOW_HEALTH_LISTEN", "")
    session = stopped_at_its_first_request(live)

    assert main(["--config", str(config), "collect"], session_factory=session) == 0

    removal_ended()
    assert timescale.rules() == dict.fromkeys(READINGS, ["30 days"])


def test_hypertables_that_are_not_the_collectors_do_not_come_between_it_and_timescaledb(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # TimescaleDB lists every hypertable in the database to whoever asks: one in a schema
    # the collector may not look in, one named as no table of its own could be, and one
    # that keeps rows by a count and not by an age.
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA metrics")
        conn.execute("CREATE TABLE metrics.cpu (at timestamptz NOT NULL, load float8)")
        conn.execute("SELECT create_hypertable('metrics.cpu', 'at')")
        conn.execute('CREATE TABLE "a.b.c" (at timestamptz NOT NULL)')
        conn.execute("""SELECT create_hypertable('"a.b.c"', 'at')""")
        conn.execute("CREATE TABLE counts (n bigint NOT NULL)")
        conn.execute("SELECT create_hypertable('counts', 'n', chunk_time_interval => 1000)")
        conn.execute(
            "CREATE FUNCTION counts_now() RETURNS bigint LANGUAGE sql STABLE AS 'SELECT 0::bigint'"
        )
        conn.execute("SELECT set_integer_now_func('counts', 'counts_now')")
        conn.execute(
            "SELECT add_retention_policy('counts', drop_after => BIGINT '1000000000000000000')"
        )

    with timescale.another_user() as among_others:
        with among_others.open(retention_days=30) as storage:
            assert timescale.rules() == {"counts": ["1000000000000000000"]} | dict.fromkeys(
                READINGS, ["30 days"]
            )
            assert storage.remove_older_than(datetime.now(UTC), BATCH_ROWS) == 0
        with among_others.open(retention_days=365):
            pass
        rules, hypertables = timescale.rules(), timescale.hypertables()

    assert rules == {"counts": ["1000000000000000000"]} | dict.fromkeys(READINGS, ["365 days"])
    assert hypertables == {*READINGS, "a.b.c", "counts"}
    assert "TimescaleDB" not in caplog.text


def test_timescaledb_is_told_nothing_of_tables_that_are_not_the_collectors(
    timescale: Postgres,
) -> None:
    # Another schema with tables of the same names and rules of their own, and another
    # hypertable beside the collector's.
    with timescale.open(retention_days=7), psycopg.connect(timescale.dsn) as conn:
        conn.execute("CREATE SCHEMA navimow")
        conn.execute("CREATE TABLE weather (at timestamptz NOT NULL, degrees float8)")
        conn.execute("SELECT create_hypertable('weather', 'at')")

    with timescale.looking_in("navimow").open(retention_days=30):
        pass
    with timescale.open(retention_days=7):
        pass

    assert timescale.hypertables("navimow") == READINGS
    in_navimow = dict(timescale.policies("navimow").values())
    assert in_navimow == dict.fromkeys(READINGS, timedelta(days=30))
    assert dict(timescale.policies().values()) == dict.fromkeys(READINGS, timedelta(days=7))


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
