"""What every storage backend must do alike, asked of each where they differ.

The replay suite says what a recorded Job becomes, against PostgreSQL alone. This one
writes through the storage adapter, as live collection opens it, and reads back as a
dashboard would, in the backend's own query language.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import clickhouse_connect
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from navimow_collector.cli import main
from navimow_collector.config import Secret, StorageConfig
from navimow_collector.records import Gap, GapReason, Job, Mower, TrailPoint
from navimow_collector.storage import (
    RejectedError,
    SchemaError,
    Storage,
    StorageError,
    open_for_collection,
    write_rows,
)
from navimow_collector.storage.retention import BATCH_ROWS

from .conftest import (
    FIXTURE,
    fresh_clickhouse,
    fresh_database,
    fresh_influxdb,
    influxdb_asked,
    influxdb_server,
    influxdb_ways,
    told_of,
)
from .test_live import Live, stopped_at_its_first_request
from .test_retention import EXPIRING_TABLES, removal_ended, rows_at


class Database(Protocol):
    """A backend as the suite needs it: opened as `collect` opens it, and read as its
    dashboards read it."""

    def open(self, **settings: object) -> Storage: ...

    def stored(self, table: str, time: str = "device_time") -> list[datetime]: ...

    def trail(self, mower_id: str, job_id: str) -> list[tuple[datetime, float]]: ...


class Relational(Database, Protocol):
    """A backend of tables and keys, which the collector tells how long to keep rows."""

    def a_day_passes(self, storage: Storage, keep_days: int) -> None: ...

    def expiring(self) -> dict[str, list[datetime]]: ...


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


@dataclass(frozen=True)
class ClickHouse:
    """A ClickHouse database. A row told of again is another row until ClickHouse merges
    the two, so it is read as its dashboards must read it: `FINAL`."""

    url: str

    @property
    def config(self) -> StorageConfig:
        return StorageConfig(backend="clickhouse", dsn=Secret(self.url))

    def open(self, **settings: object) -> Storage:
        """The storage as `collect` opens it, with these settings of the owner's."""
        return open_for_collection(replace(self.config, **settings))  # type: ignore[arg-type]

    def ask(self, question: str, **values: object) -> list[Sequence[Any]]:
        client = clickhouse_connect.get_client(dsn=self.url)
        try:
            return list(client.query(question, parameters=values).result_rows)
        finally:
            client.close()

    def a_day_passes(self, storage: Storage, keep_days: int) -> None:
        """Have what is older than the owner keeps removed now: by the collector, of which
        ClickHouse asks nothing, and by ClickHouse as it next merges each table."""
        storage.remove_older_than(datetime.now(UTC) - timedelta(days=keep_days), BATCH_ROWS)
        for table in EXPIRING_TABLES:
            self.ask(f"OPTIMIZE TABLE {table} FINAL")

    def rules(self) -> dict[str, str]:
        """The tables ClickHouse removes old rows of itself, and each one's rule."""
        made = self.ask("SELECT name, create_table_query FROM system.tables" + HERE)
        return {
            table: query.split(" TTL ")[1].split(" SETTINGS ")[0]
            for table, query in made
            if " TTL " in query
        }

    def stored(self, table: str, time: str = "device_time") -> list[datetime]:
        """When each row of a table is from, oldest first."""
        found = self.ask(f"SELECT {time} FROM {table} FINAL ORDER BY {time}")
        return [row[0].replace(tzinfo=UTC) for row in found]

    def expiring(self) -> dict[str, list[datetime]]:
        """When the rows of each table that expires are from."""
        return {table: self.stored(table, time) for table, time in EXPIRING_TABLES.items()}

    def trail(self, mower_id: str, job_id: str) -> list[tuple[datetime, float]]:
        """One Job's Trail as a dashboard asks for it: when, and how far along x."""
        found = self.ask(
            "SELECT device_time, x FROM trail_point FINAL"
            " WHERE mower_id = {mower:String} AND job_id = {job:String} ORDER BY device_time",
            mower=mower_id,
            job=job_id,
        )
        return [(when.replace(tzinfo=UTC), x) for when, x in found]


@dataclass(frozen=True)
class Influx:
    """An InfluxDB database of whichever line, asked as its dashboards ask: in InfluxQL."""

    address: str

    @property
    def config(self) -> StorageConfig:
        return StorageConfig(backend="influxdb", dsn=Secret(self.address))

    def open(self, **settings: object) -> Storage:
        """The storage as `collect` opens it, with these settings of the owner's."""
        return open_for_collection(replace(self.config, **settings))  # type: ignore[arg-type]

    def ask(self, question: str) -> list[dict[str, Any]]:
        """Every point the question is answered with, by the names of its columns."""
        database = urlsplit(self.address).path.strip("/")
        asked = urlencode({"db": database, "epoch": "ns", "q": question})
        [result] = influxdb_asked(self.address, "GET", f"/query?{asked}")["results"]
        assert "error" not in result, result
        return [
            dict(zip(series["columns"], row, strict=True))
            for series in result.get("series", [])
            for row in series["values"]
        ]

    def write(self, line: str) -> None:
        """A point written by another than the collector, as line protocol."""
        database = urlsplit(self.address).path.strip("/")
        influxdb_asked(self.address, "POST", f"/write?db={database}&precision=ns", line)

    def stored(self, table: str, time: str = "device_time") -> list[datetime]:
        """When each point of a measurement is from, oldest first: by the time it is at, or
        by another time it holds, which is a field of whole milliseconds."""
        at = {"job": "start_time", "collector_gap": "start_time", "mower": "updated_time"}
        points = self.ask(f"SELECT * FROM {table} ORDER BY time")
        if time == at.get(table, "device_time"):
            return [EPOCH + timedelta(microseconds=point["time"] // 1000) for point in points]
        return sorted(EPOCH + timedelta(milliseconds=point[time]) for point in points)

    def trail(self, mower_id: str, job_id: str) -> list[tuple[datetime, float]]:
        """One Job's Trail as a dashboard asks for it: when, and how far along x."""
        mower, job = (name.replace("'", "\\'") for name in (mower_id, job_id))
        points = self.ask(
            f"SELECT x FROM trail_point WHERE mower_id = '{mower}' AND job_id = '{job}'"
            " ORDER BY time"
        )
        return [(EPOCH + timedelta(microseconds=p["time"] // 1000), p["x"]) for p in points]


# PostgreSQL without TimescaleDB and with it, which one adapter serves.
POSTGRESQL = ["postgres", "timescaledb"]
RELATIONAL = [*POSTGRESQL, "clickhouse"]
# InfluxDB's three lines, each by both of its ways in.
INFLUX = [f"influxdb{line}-{way}" for line in (1, 2, 3) for way in ("older", "newer")]


@pytest.fixture(params=POSTGRESQL)
def postgresql(request: pytest.FixtureRequest) -> Iterator[Postgres]:
    """PostgreSQL without TimescaleDB and with it, each in turn, empty: for what is asked
    of the one adapter that serves both."""
    for database in _empty(request):
        assert isinstance(database, Postgres)
        yield database


@pytest.fixture(params=RELATIONAL)
def relational(request: pytest.FixtureRequest) -> Iterator[Relational]:
    """Each backend of the relational shape in turn, empty."""
    for database in _empty(request):
        assert isinstance(database, Postgres | ClickHouse)
        yield database


@pytest.fixture(params=[*RELATIONAL, *INFLUX])
def backend(request: pytest.FixtureRequest) -> Iterator[Database]:
    """Each backend in turn, empty."""
    yield from _empty(request)


@pytest.fixture(params=INFLUX)
def influx(request: pytest.FixtureRequest) -> Iterator[Influx]:
    """Each line of InfluxDB in turn, empty, for what only InfluxDB does."""
    for database in _empty(request):
        assert isinstance(database, Influx)
        yield database


def _empty(request: pytest.FixtureRequest) -> Iterator[Database]:
    name: str = request.param
    if name.startswith("influxdb"):
        line, way = int(name[8]), name[10:]
        server = influxdb_server(line)
        with fresh_influxdb(line, server) as address:
            database = urlsplit(address).path
            yield Influx(urlsplit(influxdb_ways(server)[way])._replace(path=database).geturl())
    elif name == "clickhouse":
        with fresh_clickhouse(request.getfixturevalue("clickhouse_server")) as url:
            yield ClickHouse(url)
    else:
        servers = {"postgres": "postgres_server", "timescaledb": "timescale_server"}
        with fresh_database(request.getfixturevalue(servers[name])) as dsn:
            if name == "timescaledb":
                with_timescaledb(dsn)
            yield Postgres(dsn)


@pytest.fixture
def clickhouse(clickhouse_server: str) -> Iterator[ClickHouse]:
    """An empty ClickHouse database, for what only ClickHouse does."""
    with fresh_clickhouse(clickhouse_server) as url:
        yield ClickHouse(url)


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
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HERE = " WHERE database = currentDatabase()"


def old_and_kept() -> tuple[datetime, datetime]:
    """Two times as of now, which an engine goes by: one older than 30 days, one not."""
    now = datetime.now(UTC).replace(microsecond=0)
    return now - timedelta(days=40), now - timedelta(days=1)


def test_an_empty_database_is_given_its_schema(relational: Relational) -> None:
    with pytest.raises(SchemaError):
        relational.open(migrate=False)  # nothing is there, and nothing was put there

    with relational.open():
        pass
    with relational.open(), relational.open(migrate=False):  # again changes nothing
        pass

    assert relational.stored("trail_point") == []


def test_an_empty_influxdb_database_needs_nothing_made_in_it(influx: Influx) -> None:
    # InfluxDB has no schema; the database, or bucket, is the owner's to make.
    with influx.open(migrate=False), influx.open():
        pass
    assert influx.stored("trail_point") == []

    nowhere = urlsplit(influx.address)._replace(path="/not_made").geturl()
    with pytest.raises(StorageError, match="not_made"):
        Influx(nowhere).open()


def point(
    when: datetime, x: float, job_id: str | None = None, mower: str = "DEVICE_1"
) -> TrailPoint:
    return TrailPoint(mower, when, when + timedelta(seconds=1), x, 2.0, 0.5, 4, job_id, 3)


def test_a_jobs_trail_reads_back_in_order_however_and_whenever_it_was_written(
    backend: Database,
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


def test_a_batch_of_rows_is_stored_whole_and_once(backend: Database) -> None:
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    batch = [point(start + timedelta(seconds=2 * i), float(i), "job") for i in range(1200)]

    with backend.open() as storage:
        assert storage.write_trail(batch) == 1200
        # Delivered again, and stored already. InfluxDB takes them all the same, each in the
        # place of the point it is, and cannot say that none was new.
        again = storage.write_trail(batch[400:800])
        assert again == (400 if isinstance(backend, Influx) else 0)

    assert len(backend.trail("DEVICE_1", "job")) == 1200


def test_a_job_reads_back_as_it_was_written(backend: Database) -> None:
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


def test_a_job_begun_again_after_a_charge_reads_back_as_not_ended(backend: Database) -> None:
    # A return to the dock to charge ends the Job for the time being, and leaving it again
    # takes that back.
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    docked = Job(
        "DEVICE_1",
        "1790769600",
        start,
        start + timedelta(hours=1),
        end_time=start + timedelta(hours=1),
        mowing_percentage=40,
        arrival_x=-0.288,
    )
    away_again = replace(docked, updated_time=start + timedelta(hours=2), end_time=None)

    with backend.open() as storage:
        assert [storage.write_jobs([job]) for job in (docked, away_again)] == [1, 1]
        assert list(storage.latest_jobs()) == [away_again]


def test_an_older_telling_of_a_job_does_not_replace_a_later_one(relational: Relational) -> None:
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    earlier = Job("DEVICE_1", "1790769600", start, start + timedelta(minutes=5))
    later = replace(earlier, updated_time=start + timedelta(minutes=10), mowing_percentage=3)

    with relational.open() as storage:
        assert storage.write_jobs([later]) == 1
        assert storage.write_jobs([earlier]) == 0  # as a buffer written out of order brings it

    with relational.open() as storage:
        assert list(storage.latest_jobs()) == [later]


@pytest.mark.parametrize("newest_first", [False, True])
@pytest.mark.parametrize(
    "reports",
    [
        pytest.param([(0, 9)], id="progress-as-the-mower-leaves"),
        # The Job given up is told of in the millisecond it was last told of in.
        pytest.param([(60, 50), (120, 0)], id="given-up-for-another"),
    ],
)
def test_of_the_tellings_of_a_job_the_last_stands_in_either_order(
    relational: Relational, reports: list[tuple[int, float]], newest_first: bool
) -> None:
    # A message sent no later than the one before can still change a Job. Written out of
    # order, as a write given up on and answered late writes it, a telling before must not
    # replace it.
    tellings = told_of(*reports)
    first = [job for job in tellings if job.job_id == tellings[0].job_id]

    with relational.open() as storage:
        for job in reversed(first) if newest_first else first:
            storage.write_jobs([job])

    with relational.open() as storage:
        assert list(storage.latest_jobs()) == [first[-1]]


def test_in_influxdb_an_older_telling_of_a_job_is_written_over_a_later_one(influx: Influx) -> None:
    # What ADR 0002 says InfluxDB cannot do otherwise: a point is not refused. It is written
    # over the one there field by field, so what the older telling does not say stays.
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    earlier = Job("DEVICE_1", "1790769600", start, start + timedelta(minutes=5))
    later = replace(earlier, updated_time=start + timedelta(minutes=10), mowing_percentage=3)

    with influx.open() as storage:
        assert [storage.write_jobs([job]) for job in (later, earlier)] == [1, 1]
        assert list(storage.latest_jobs()) == [replace(earlier, mowing_percentage=3)]


def test_a_job_whose_numbers_are_whole_is_told_of_again_after_a_restart(backend: Database) -> None:
    # A collector starting up carries on from the Job as the database has it, and tells of
    # it again: an area of 120 square metres is no whole number for having no fraction.
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    job = Job("DEVICE_1", "1790769600", start, start, area=120.0, arrival_x=-1.0)

    with backend.open() as storage:
        assert storage.write_jobs([job]) == 1
    with backend.open() as storage:
        [stored] = storage.latest_jobs()
        further = replace(stored, updated_time=start + timedelta(minutes=5), area=150.0)
        assert storage.write_jobs([further]) == 1
        assert list(storage.latest_jobs()) == [further]
        assert isinstance(stored.area, float)


def test_names_are_stored_as_they_are_however_awkward(backend: Database) -> None:
    # A mower is named by its owner, and its identifier by Navimow: neither is the
    # collector's to tidy, in a line of text or in a question put to the database.
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    device, name = "DEVICE 1,a=b o'clock", 'Robo, the 2nd = "big" one\\ o\'clock'
    job = Job(device, "job 1,a=b", start, start + timedelta(minutes=5))

    with backend.open() as storage:
        assert storage.write_jobs([job]) == 1
        assert storage.write_mowers([Mower(device, name, "X420", None, start)]) == 1
        assert storage.write_mowers([Mower(device, name, "X420", None, start)]) == 0
        assert storage.write_trail([point(start, 1.0, "job 1,a=b", mower=device)]) == 1
        assert list(storage.latest_jobs()) == [job]

    assert backend.trail(device, "job 1,a=b") == [(start, 1.0)]


def test_a_gap_recorded_again_is_made_longer_and_never_shorter(backend: Database) -> None:
    began = datetime(2026, 9, 30, 12, tzinfo=UTC)
    short = Gap("DEVICE_1", began, began + timedelta(minutes=4), GapReason.RECONNECT)
    long = Gap("DEVICE_1", began, began + timedelta(minutes=12), GapReason.RESTART)

    with backend.open() as storage:
        assert [storage.write_gaps([gap]) for gap in (short, long, short)] == [1, 1, 0]

    assert backend.stored("collector_gap", "end_time") == [began + timedelta(minutes=12)]


def test_a_mower_is_described_anew_only_when_it_is_described_differently(
    backend: Database,
) -> None:
    first = datetime(2026, 9, 30, 12, tzinfo=UTC)
    mower = Mower("DEVICE_1", "Mower", "X420", "005D", first)
    same_later = replace(mower, updated_time=first + timedelta(hours=1))
    updated = replace(mower, firmware="005E", updated_time=first + timedelta(hours=2))

    with backend.open() as storage:
        told = [storage.write_mowers([each]) for each in (mower, same_later, updated, mower)]

    assert told == [1, 0, 1, 0]
    # A point is at the time its mower was so described: in InfluxDB the description
    # before stays, as the mower's history, where a row is rewritten.
    history = [first] if isinstance(backend, Influx) else []
    assert backend.stored("mower", "updated_time") == [*history, first + timedelta(hours=2)]


def test_rows_older_than_the_owner_keeps_are_removed_and_jobs_are_not(
    relational: Relational,
) -> None:
    old, kept = old_and_kept()

    with relational.open(retention_days=30) as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        write_rows(storage, [Job("DEVICE_1", "old", old, old, end_time=old, completed=True)])
        write_rows(storage, [Mower("DEVICE_1", "Mower", "X420", "005D", old)])
        relational.a_day_passes(storage, keep_days=30)

    assert relational.expiring() == dict.fromkeys(EXPIRING_TABLES, [kept])
    assert relational.stored("job", "start_time") == [old]
    assert relational.stored("mower", "updated_time") == [old]


def test_how_long_influxdb_keeps_points_is_not_the_collectors_to_say(
    influx: Influx, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Its bucket, or retention policy, says: a number of days set here would be a promise
    # the collector cannot keep, and is refused when the configuration is read.
    config = tmp_path / "collector.toml"
    config.write_text(
        f'[storage]\nbackend = "influxdb"\ndsn = "{influx.address}"\nretention_days = 30\n'
    )

    assert main(["--config", str(config), "replay", str(FIXTURE)]) == 2

    assert "storage.retention_days is not for InfluxDB" in capsys.readouterr().err
    assert influx.stored("trail_point") == []


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


def test_of_a_database_made_in_part_only_the_tables_now_made_are_hypertables(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # The Trail's table from before TimescaleDB, and two tables the collector has yet to make.
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION timescaledb")
    with timescale.open():
        pass
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE job_progress, mower_state")
        conn.execute("DELETE FROM schema_version WHERE version IN (5, 6)")
        conn.execute("CREATE EXTENSION timescaledb")

    with timescale.open(retention_days=30):
        pass

    assert timescale.hypertables() == READINGS - {"trail_point"}
    assert "TimescaleDB" not in caplog.text


def test_ordinary_tables_are_not_taken_for_hypertables_of_their_names_elsewhere(
    timescale: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION timescaledb")
    with timescale.open():  # ordinary tables, from before TimescaleDB
        pass
    with psycopg.connect(timescale.dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION timescaledb")
        conn.execute("CREATE SCHEMA other")
    with timescale.looking_in("other").open(retention_days=7):  # another's, as hypertables
        pass

    with timescale.open(retention_days=30):
        pass

    assert timescale.policies() == {}
    assert dict(timescale.policies("other").values()) == dict.fromkeys(READINGS, timedelta(days=7))
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
    postgresql: Postgres, caplog: pytest.LogCaptureFixture
) -> None:
    # Each would make the tables, and one be told they are there: they take their turn,
    # and see what the one before did, even where a transaction reads as of its start.
    with psycopg.connect(postgresql.dsn, autocommit=True) as conn:
        conn.execute(
            f'ALTER DATABASE "{conn.info.dbname}"'
            " SET default_transaction_isolation = 'repeatable read'"
        )
    with ThreadPoolExecutor(max_workers=8) as collectors:
        opened = [collectors.submit(lambda: postgresql.open().close()) for _ in range(8)]
        for each in opened:
            each.result()

    assert postgresql.stored("trail_point") == []
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
    postgresql: Postgres,
) -> None:
    # A statement may take as long as the owner says. Waiting for another collector may
    # not: a start that hangs says nothing of why.
    with postgresql.open():
        pass
    patient = Postgres(make_conninfo(postgresql.dsn, options="-cstatement_timeout=0"))
    # The one holding its turn lets go before the wait for the other is given up on here,
    # so that a collector which would wait for ever fails this test and does not hang it.
    with ThreadPoolExecutor() as waiting, psycopg.connect(postgresql.dsn) as holding:
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


def test_clickhouse_is_given_the_rule_of_what_to_keep(clickhouse: ClickHouse) -> None:
    def rule(days: int) -> dict[str, str]:
        return {
            table: f"{'end_time' if table == 'collector_gap' else 'device_time'}"
            f" + toIntervalDay({days})"
            for table in EXPIRING_TABLES
        }

    def rewritten() -> int:
        """How many times ClickHouse has been set to rewriting a table to a new rule."""
        return int(clickhouse.ask("SELECT count() FROM system.mutations" + HERE)[0][0])

    with clickhouse.open():
        assert clickhouse.rules() == {}  # everything, unless the owner says otherwise
    with clickhouse.open(retention_days=30):
        assert clickhouse.rules() == rule(30)
        times = rewritten()
    with clickhouse.open(retention_days=30):
        assert rewritten() == times  # the same rule is not given anew
    with clickhouse.open(retention_days=365):
        assert clickhouse.rules() == rule(365)
    with clickhouse.open(migrate=False):
        assert clickhouse.rules() == rule(365)  # not this one's to change
    with clickhouse.open(retention_days=10**9):
        assert clickhouse.rules() == {}  # for longer than dates go back: everything
    with clickhouse.open(retention_days=7), clickhouse.open():
        assert clickhouse.rules() == {}  # the owner keeps everything again


def test_clickhouse_given_no_rule_says_that_nothing_removes_old_rows(
    clickhouse: ClickHouse, caplog: pytest.LogCaptureFixture
) -> None:
    # With migration off the tables are not the collector's to change, and it removes no
    # rows from ClickHouse itself: an owner who set a number of days is told as much.
    old, kept = old_and_kept()
    with clickhouse.open():
        pass

    with clickhouse.open(retention_days=30, migrate=False) as storage:
        write_rows(storage, [*rows_at(old), *rows_at(kept)])
        clickhouse.a_day_passes(storage, keep_days=30)

    assert "Nothing removes old rows from ClickHouse" in caplog.text
    assert clickhouse.expiring() == dict.fromkeys(EXPIRING_TABLES, [old, kept])


def test_in_clickhouse_a_row_told_of_again_leaves_no_second_row(clickhouse: ClickHouse) -> None:
    # ClickHouse would store it twice, and merge the two when it came to: a dashboard that
    # forgot `FINAL` would meanwhile count the Trail double.
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    points = [point(start + timedelta(microseconds=7 * i), float(i), "job") for i in range(50)]

    with clickhouse.open() as storage:
        assert storage.write_trail(points) == 50
        assert storage.write_trail([*points, *points]) == 0

    assert clickhouse.ask("SELECT count() FROM trail_point") == [(50,)]


def test_clickhouse_takes_a_batch_of_any_size(clickhouse: ClickHouse) -> None:
    # Longer than ClickHouse lets one question be: what is stored already is asked in parts.
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    batch = [point(start + timedelta(seconds=2 * i), float(i), "job") for i in range(6000)]

    with clickhouse.open() as storage:
        assert storage.write_trail(batch) == 6000
        assert storage.write_trail(batch) == 0


def test_whatever_goes_wrong_in_clickhouse_stays_behind_the_storage_boundary(
    clickhouse: ClickHouse,
) -> None:
    # Live collection waits out a StorageError, and drops the row of a RejectedError: any
    # other error of the client's would stop it.
    when = datetime(2026, 9, 30, 12, tzinfo=UTC)
    with clickhouse.open() as storage:
        with pytest.raises(RejectedError):
            storage.write_trail([replace(point(when, 1.0), x="far")])  # type: ignore[arg-type]
        clickhouse.ask("DROP TABLE job")
        with pytest.raises(StorageError):
            storage.latest_jobs()
        with pytest.raises(SchemaError):
            storage.check_schema()


def test_replay_changes_no_rule_of_clickhouses(clickhouse: ClickHouse, tmp_path: Path) -> None:
    config = tmp_path / "collector.toml"
    config.write_text(
        f'[storage]\nbackend = "clickhouse"\ndsn = "{clickhouse.url}"\nretention_days = 7\n'
    )

    assert main(["--config", str(config), "replay", str(FIXTURE)]) == 0
    assert clickhouse.rules() == {}
    assert len(clickhouse.stored("trail_point")) > 1000

    with clickhouse.open(retention_days=30):
        rules = clickhouse.rules()
    assert main(["--config", str(config), "replay", str(FIXTURE)]) == 0
    assert clickhouse.rules() == rules


def test_a_clickhouse_that_is_not_there_is_an_outage_like_any_other() -> None:
    # What the live buffer waits out, and what `collect` refuses to start without.
    nowhere = StorageConfig(backend="clickhouse", dsn=Secret("http://127.0.0.1:1/navimow"))

    with pytest.raises(StorageError, match="clickhouse"):
        open_for_collection(nowhere)


def test_clickhouse_must_be_told_where_it_is() -> None:
    with pytest.raises(StorageError, match="storage.dsn is required"):
        open_for_collection(StorageConfig(backend="clickhouse"))


def test_an_influxdb_that_is_not_there_is_an_outage_like_any_other() -> None:
    nowhere = StorageConfig(backend="influxdb", dsn=Secret("http://127.0.0.1:1/navimow"))

    with pytest.raises(StorageError, match="influxdb"):
        open_for_collection(nowhere)


@pytest.mark.parametrize(
    "dsn", [None, "http://influxdb:8086", "influxdb:8086/navimow", "udp://influxdb:8089/navimow"]
)
def test_influxdb_must_be_told_where_it_is_and_which_database(dsn: str | None) -> None:
    told = StorageConfig(backend="influxdb", dsn=None if dsn is None else Secret(dsn))

    with pytest.raises(StorageError, match="storage.dsn"):
        open_for_collection(told)


def test_a_point_influxdb_will_never_take_is_refused_and_not_tried_for_ever(influx: Influx) -> None:
    # Live collection drops the row of a RejectedError, and waits out any other StorageError
    # with every row behind it: a field of another kind than the points before it is the
    # first, whatever number the line of InfluxDB answers it with.
    when = datetime(2026, 9, 30, 12, tzinfo=UTC)
    influx.write(f'trail_point,mower_id=DEVICE_2 x="here" {int(when.timestamp())}000000000')

    with influx.open() as storage:
        with pytest.raises(RejectedError):
            storage.write_trail([point(when, 1.0)])
        with pytest.raises(RejectedError):
            storage.write_trail([replace(point(when, 1.0), y="far")])  # type: ignore[arg-type]


def test_a_mower_nothing_is_known_of_is_no_point_in_influxdb(influx: Influx) -> None:
    when = datetime(2026, 9, 30, 12, tzinfo=UTC)

    with influx.open() as storage:
        assert storage.write_mowers([Mower("DEVICE_1", None, None, None, when)]) == 0
        assert storage.write_mowers([Mower("DEVICE_1", None, "X420", None, when)]) == 1

    assert influx.stored("mower", "updated_time") == [when]


def test_a_database_that_was_never_made_is_found_when_influxdb_is_opened(influx: Influx) -> None:
    # Found here it is an outage, which loses no row; found at the first write it could
    # look like a row InfluxDB will not take, and the row be dropped for it.
    with pytest.raises(StorageError) as refusal:
        Influx(urlsplit(influx.address)._replace(path="/not_made").geturl()).open()
    assert not isinstance(refusal.value, RejectedError)


def test_a_token_without_its_organisation_is_found_when_influxdb_2_is_opened(
    influx: Influx, request: pytest.FixtureRequest
) -> None:
    # InfluxDB 2 answers any question so asked, and then asks for the organisation at
    # every write, in the words it has for a row it will not take.
    if request.node.callspec.id != "influxdb2-newer":
        pytest.skip("only InfluxDB 2 wants an organisation named, and only by its newer way in")
    address = urlsplit(influx.address)
    token = parse_qs(address.query)["token"][-1]

    with pytest.raises(StorageError) as refusal:
        Influx(address._replace(query=urlencode({"token": token})).geturl()).open()
    assert not isinstance(refusal.value, RejectedError)


def test_a_password_with_marks_in_it_is_given_as_an_address_writes_them(influx: Influx) -> None:
    address = urlsplit(influx.address)
    if not address.password:
        pytest.skip("this way in has no password to it")
    # Its first letter as an address may write any letter: by its number.
    marked = f"%{ord(address.password[0]):02X}{quote(address.password[1:], safe='')}"
    host = address.netloc.rpartition("@")[2]

    with Influx(address._replace(netloc=f"{address.username}:{marked}@{host}").geturl()).open():
        pass


def test_an_answer_that_is_not_influxdbs_stays_behind_the_storage_boundary() -> None:
    # A proxy's page of apology, say, where InfluxDB was expected.
    class Apology(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>Back soon</html>")

        do_POST = do_GET

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Apology) as proxy:
        answering = threading.Thread(target=proxy.serve_forever, daemon=True)
        answering.start()
        try:
            elsewhere = Secret(f"http://127.0.0.1:{proxy.server_address[1]}/navimow")
            with pytest.raises(StorageError, match="not an answer"):
                open_for_collection(StorageConfig(backend="influxdb", dsn=elsewhere))
        finally:
            proxy.shutdown()
