"""ClickHouse storage: the tables of docs/adr/0002-data-schema.md in a column store.

ClickHouse holds no row to a key: one told of again is another row, until it merges the
two. So this asks what is stored before it writes and writes only what is new, as the
PostgreSQL adapter has the database decide. The tables are ReplacingMergeTree all the
same, for the row that slips past, and are to be read with `FINAL`.

Its client is not small, and is loaded only where ClickHouse is the backend chosen.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import astuple, fields, replace
from datetime import UTC, datetime
from typing import Any, TypeVar

import clickhouse_connect
from clickhouse_connect.driver.exceptions import ClickHouseError, DataError, OperationalError

from ..config import StorageConfig
from ..records import Gap, Job, Mower, MowerState, Progress, Row, TrailPoint
from .base import RejectedError, SchemaError, StorageError, lifetime

_LOGGER = logging.getLogger(__name__)
# As for PostgreSQL: a connection attempt and a statement each fail within seconds, which
# the live buffer treats as an outage. The server ends a statement itself; the client waits
# a little longer than that, so as to hear it said.
CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_SECONDS = 5
ANSWER_TIMEOUT_SECONDS = STATEMENT_TIMEOUT_SECONDS + 5
# How many keys one question asks after: ClickHouse takes a question only so long.
KEYS_A_QUESTION = 1000
_HERE = " WHERE database = currentDatabase()"


@contextmanager
def _translated() -> Iterator[None]:
    """Keep the client's exceptions behind the storage boundary. A row it cannot write for
    what the row holds is refused, as a row PostgreSQL would not take is."""
    try:
        yield
    except (DataError, TypeError, ValueError) as error:
        raise RejectedError(f"clickhouse: {error}".strip()) from error
    except ClickHouseError as error:
        raise StorageError(f"clickhouse: {error}".strip()) from error


_TIME = "DateTime64(6, 'UTC')"
# A reading is of its mower and its time. Kept by the month, so that old rows leave whole.
_BY_MONTH = (
    "ENGINE = ReplacingMergeTree PARTITION BY toYYYYMM(device_time)"
    " ORDER BY (mower_id, device_time)"
)
# Migrations are additive only, as PostgreSQL's are: the same tables and columns. Of two
# rows with one key, ClickHouse keeps the one with the later of the time its engine names.
MIGRATIONS = (
    f"""
    CREATE TABLE IF NOT EXISTS trail_point (
        mower_id String,
        device_time {_TIME},
        received_time {_TIME},
        x Float64,
        y Float64,
        theta Float64,
        vehicle_state Nullable(Int32),
        job_id Nullable(String),
        zone Nullable(Int32)
    ) {_BY_MONTH}
    """,
    f"""
    CREATE TABLE IF NOT EXISTS collector_gap (
        mower_id String,
        start_time {_TIME},
        end_time {_TIME},
        reason String
    ) ENGINE = ReplacingMergeTree(end_time) ORDER BY (mower_id, start_time)
    """,
    # No array may be unset in ClickHouse: a Job whose Zones are not yet known has none.
    f"""
    CREATE TABLE IF NOT EXISTS job (
        mower_id String,
        job_id String,
        start_time {_TIME},
        end_time Nullable({_TIME}),
        completed Bool,
        mowing_percentage Nullable(Int32),
        area Nullable(Float64),
        arrival_x Nullable(Float64),
        arrival_y Nullable(Float64),
        arrival_theta Nullable(Float64),
        updated_time {_TIME},
        zones Array(Int32)
    ) ENGINE = ReplacingMergeTree(updated_time) ORDER BY (mower_id, job_id)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS job_progress (
        mower_id String,
        device_time {_TIME},
        received_time {_TIME},
        zone Nullable(Int32),
        zone_progress Nullable(Float64),
        mowing_percentage Nullable(Int32),
        area Nullable(Float64),
        week_area Nullable(Float64),
        job_id Nullable(String)
    ) {_BY_MONTH}
    """,
    f"""
    CREATE TABLE IF NOT EXISTS mower_state (
        mower_id String,
        device_time {_TIME},
        received_time {_TIME},
        state String,
        battery Nullable(Int32),
        job_id Nullable(String)
    ) {_BY_MONTH}
    """,
    f"""
    CREATE TABLE IF NOT EXISTS mower (
        mower_id String,
        name Nullable(String),
        model Nullable(String),
        firmware Nullable(String),
        updated_time {_TIME}
    ) ENGINE = ReplacingMergeTree(updated_time) ORDER BY mower_id
    """,
)
TABLES = ("trail_point", "collector_gap", "job", "job_progress", "mower_state", "mower")
# The tables whose rows an owner may have removed once old, and the time that says how old
# a row is: a gap is as old as its end. A Job and a mower are kept whatever their age.
EXPIRING = {
    "trail_point": "device_time",
    "job_progress": "device_time",
    "mower_state": "device_time",
    "collector_gap": "end_time",
}
_RULE = re.compile(r" TTL (.+?)(?: SETTINGS |$)")

_Reading = TypeVar("_Reading", TrailPoint, Progress, MowerState)


def _columns(row: type[Row]) -> list[str]:
    """Every field of the row goes into the column of the same name."""
    return [field.name for field in fields(row)]


def _utc(value: Any) -> Any:
    """ClickHouse answers a time in UTC without saying so."""
    return value.replace(tzinfo=UTC) if isinstance(value, datetime) else value


def _value(value: object) -> object:
    """A value as the client writes it: the word of an enumeration and not its name, and
    a list for the Zones of a Job."""
    if isinstance(value, str):
        return str(value)
    return list(value) if isinstance(value, tuple) else value


class ClickHouseStorage:
    """Persist rows in ClickHouse, each stored once though ClickHouse would store it twice."""

    def __init__(self, config: StorageConfig) -> None:
        if config.dsn is None:
            raise StorageError("storage.dsn is required for the clickhouse backend")
        with _translated():
            self._client = clickhouse_connect.get_client(
                dsn=config.dsn.reveal(),
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                send_receive_timeout=ANSWER_TIMEOUT_SECONDS,
                settings={"max_execution_time": STATEMENT_TIMEOUT_SECONDS},
            )

    def __enter__(self) -> ClickHouseStorage:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def migrate(self) -> None:
        self._tell(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version Int32, applied_at DateTime DEFAULT now()"
            ") ENGINE = ReplacingMergeTree ORDER BY version"
        )
        applied = {row[0] for row in self._ask("SELECT version FROM schema_version")}
        for version, statement in enumerate(MIGRATIONS, 1):
            if version not in applied:
                self._tell(statement)
                noted = "INSERT INTO schema_version (version) VALUES ({version:Int32})"
                self._tell(noted, version=version)

    def check_schema(self) -> None:
        found = {row[0] for row in self._ask("SELECT name FROM system.tables" + _HERE)}
        if not found.issuperset(TABLES):
            raise SchemaError(
                "collector schema is missing; enable storage.migrate or apply migrations"
            )

    def keep_for(self, days: int | None) -> None:
        """Have ClickHouse remove the rows older than so many days as it merges, by a TTL
        on each table that expires, or keep them all by none."""
        keeps = lifetime(days) is not None
        try:
            for table, rule in self._rules().items():
                wanted = f"{EXPIRING[table]} + toIntervalDay({days})" if keeps else None
                if rule != wanted:
                    change = "REMOVE TTL" if wanted is None else f"MODIFY TTL {wanted}"
                    self._client.command(f"ALTER TABLE {table} {change}")
                    _LOGGER.info("ClickHouse now keeps %s %s", table, wanted or "for ever")
        except OperationalError as error:  # the connection, which is an outage like any other
            raise StorageError(f"clickhouse: {error}".strip()) from error
        except ClickHouseError as error:
            _LOGGER.warning("ClickHouse was not told how long to keep rows: %s", error)

    def _rules(self) -> dict[str, str | None]:
        """Each table that expires, and the TTL it has."""
        made = self._ask("SELECT name, create_table_query FROM system.tables" + _HERE)
        rules = {table: _RULE.search(query) for table, query in made if table in EXPIRING}
        return {table: rule and rule.group(1) for table, rule in rules.items()}

    def remove_older_than(self, before: datetime, batch: int) -> int:
        """Nothing: what ClickHouse was given the rule for is its own to remove. A table it
        has no rule for (it took none, or its schema is not the collector's to change) is
        named, since nothing removes old rows from it."""
        if unruled := sorted(table for table, rule in self._rules().items() if rule is None):
            _LOGGER.warning(
                "Nothing removes old rows from ClickHouse's %s: it has no TTL on them, and the"
                " collector removes none from ClickHouse itself",
                ", ".join(unruled),
            )
        return 0

    def latest_jobs(self) -> Sequence[Job]:
        found = self._ask(
            f"SELECT {', '.join(_columns(Job))} FROM job FINAL"
            " ORDER BY mower_id, start_time DESC LIMIT 1 BY mower_id"
        )
        jobs = [Job(*map(_utc, row)) for row in found]
        return [replace(job, zones=tuple(job.zones) if job.zones else None) for job in jobs]

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        return self._write_readings("trail_point", points)

    def write_progress(self, reports: Sequence[Progress]) -> int:
        return self._write_readings("job_progress", reports)

    def write_states(self, states: Sequence[MowerState]) -> int:
        return self._write_readings("mower_state", states)

    def _write_readings(self, table: str, readings: Sequence[_Reading]) -> int:
        """Store the readings not stored already: of two of one mower and time, the first."""
        stored = self._stored(table, "device_time", "device_time", readings)
        new: dict[tuple[str, datetime], _Reading] = {}
        for reading in readings:
            key = (reading.mower_id, reading.device_time)
            if key not in stored:
                new.setdefault(key, reading)
        return self._insert(table, list(new.values()))

    def write_jobs(self, jobs: Sequence[Job]) -> int:
        """Store each Job unless a later telling of it is stored."""
        told = self._stored("job", "job_id", "updated_time", jobs)
        new = []
        for job in jobs:
            key = (job.mower_id, job.job_id)
            if key not in told or told[key] <= job.updated_time:
                told[key] = job.updated_time
                new.append(job)
        # A Job whose Zones are not known is given none, and read back as such.
        return self._insert("job", [replace(job, zones=job.zones or ()) for job in new])

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        """Store each gap that is new, or ends later than the same gap as it is stored."""
        ends = self._stored("collector_gap", "start_time", "end_time", gaps)
        new = []
        for gap in gaps:
            key = (gap.mower_id, gap.start_time)
            if key not in ends or ends[key] < gap.end_time:
                ends[key] = gap.end_time
                new.append(gap)
        return self._insert("collector_gap", new)

    def write_mowers(self, mowers: Sequence[Mower]) -> int:
        """Store each mower that is new, or described differently and later than it is."""
        found = self._ask(
            f"SELECT {', '.join(_columns(Mower))} FROM mower FINAL"
            " WHERE mower_id IN {mowers:Array(String)}",
            mowers=sorted({mower.mower_id for mower in mowers}),
        )
        known = {mower.mower_id: mower for mower in (Mower(*map(_utc, row)) for row in found)}
        new = []
        for mower in mowers:
            was = known.get(mower.mower_id)
            if was is None or (
                was.updated_time < mower.updated_time and astuple(was)[:4] != astuple(mower)[:4]
            ):
                known[mower.mower_id] = mower
                new.append(mower)
        return self._insert("mower", new)

    def _stored(
        self, table: str, key: str, value: str, rows: Iterable[Row]
    ) -> dict[tuple[str, Any], Any]:
        """Of rows about to be written, what is stored already: by the mower and the rest
        of each one's key, the value the table goes by."""
        keys = defaultdict(set)
        for row in rows:
            keys[row.mower_id].add(getattr(row, key))
        kind = "String" if key == "job_id" else _TIME
        question = (
            f"SELECT {key}, {value} FROM {table} FINAL"
            f" WHERE mower_id = {{mower:String}} AND {key} IN {{keys:Array({kind})}}"
        )
        stored = {}
        for mower, wanted in keys.items():
            asked = sorted(wanted)
            for first in range(0, len(asked), KEYS_A_QUESTION):
                some = asked[first : first + KEYS_A_QUESTION]
                found = self._ask(question, mower=mower, keys=some)
                stored.update({(mower, _utc(row[0])): _utc(row[1]) for row in found})
        return stored

    def _insert(self, table: str, rows: Sequence[Row]) -> int:
        if rows:
            values = [[_value(value) for value in astuple(row)] for row in rows]
            with _translated():
                self._client.insert(table, values, column_names=_columns(type(rows[0])))
        return len(rows)

    def _ask(self, question: str, **values: object) -> Sequence[Sequence[Any]]:
        with _translated():
            return self._client.query(question, parameters=values).result_rows

    def _tell(self, statement: str, **values: object) -> None:
        with _translated():
            self._client.command(statement, parameters=values)

    def close(self) -> None:
        self._client.close()
