"""PostgreSQL storage: the reference backend for the schema in docs/adr/0002-data-schema.md.

TimescaleDB is this same adapter: where its extension is installed, the readings are held
in hypertables and what the owner keeps is a retention policy of the engine's.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import astuple, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ..config import StorageConfig
from ..records import Gap, Job, Mower, MowerState, Progress, Row, TrailPoint
from .base import RejectedError, SchemaError, StorageError

# A database call should not wait for long: by default a connection attempt, a statement
# held up by a lock and a server that vanished from the network each fail within seconds,
# which the live buffer treats as an outage. These are defaults: whatever the operator set
# for the same thing is left alone. A server that stays connected and says nothing is
# beyond them all; live collection keeps its own deadline for that.
_LOGGER = logging.getLogger(__name__)
CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000
_DEAD_PEER = {
    "keepalives_idle": 5,
    "keepalives_interval": 2,
    "keepalives_count": 3,
    "tcp_user_timeout": 10000,  # milliseconds; libpq ignores it where the system lacks it
}


@contextmanager
def _translated() -> Iterator[None]:
    """Keep psycopg's exceptions behind the storage boundary."""
    try:
        yield
    except (psycopg.DataError, psycopg.IntegrityError) as error:
        raise RejectedError(f"postgres: {error}".strip()) from error
    except psycopg.Error as error:
        raise StorageError(f"postgres: {error}".strip()) from error


# Migrations are additive only: never drop or rename a column, so newer consumers can
# tolerate older collectors.
# A mower reports one pose per instant, so (mower_id, device_time) identifies a point:
# a redelivered point is skipped rather than duplicated, which makes replay idempotent.
MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS trail_point (
        mower_id text NOT NULL,
        device_time timestamptz NOT NULL,
        received_time timestamptz NOT NULL,
        x double precision NOT NULL,
        y double precision NOT NULL,
        theta double precision NOT NULL,
        vehicle_state integer,
        PRIMARY KEY (mower_id, device_time)
    )
    """,
    # One row per mower and outage: a gap starts once, so one recorded again is the same gap.
    """
    CREATE TABLE IF NOT EXISTS collector_gap (
        mower_id text NOT NULL,
        start_time timestamptz NOT NULL,
        end_time timestamptz NOT NULL,
        reason text NOT NULL,
        PRIMARY KEY (mower_id, start_time)
    )
    """,
    # Every row names the Job it belongs to, where it belongs to one.
    "ALTER TABLE trail_point ADD COLUMN IF NOT EXISTS job_id text,"
    " ADD COLUMN IF NOT EXISTS zone integer",
    # The collector names each Job itself, and tells of it again whenever it learns more.
    """
    CREATE TABLE IF NOT EXISTS job (
        mower_id text NOT NULL,
        job_id text NOT NULL,
        start_time timestamptz NOT NULL,
        end_time timestamptz,
        completed boolean NOT NULL,
        mowing_percentage integer,
        area double precision,
        arrival_x double precision,
        arrival_y double precision,
        arrival_theta double precision,
        updated_time timestamptz NOT NULL,
        PRIMARY KEY (mower_id, job_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_progress (
        mower_id text NOT NULL,
        device_time timestamptz NOT NULL,
        received_time timestamptz NOT NULL,
        zone integer,
        zone_progress double precision,
        mowing_percentage integer,
        area double precision,
        week_area double precision,
        job_id text,
        PRIMARY KEY (mower_id, device_time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mower_state (
        mower_id text NOT NULL,
        device_time timestamptz NOT NULL,
        received_time timestamptz NOT NULL,
        state text NOT NULL,
        battery integer,
        job_id text,
        PRIMARY KEY (mower_id, device_time)
    )
    """,
    "ALTER TABLE job ADD COLUMN IF NOT EXISTS zones integer[]",
    # One row per mower, as the account's device list last described it.
    """
    CREATE TABLE IF NOT EXISTS mower (
        mower_id text PRIMARY KEY,
        name text,
        model text,
        firmware text,
        updated_time timestamptz NOT NULL
    )
    """,
)
TABLES = ("trail_point", "collector_gap", "job", "job_progress", "mower_state", "mower")
# The tables whose rows an owner may have removed once old: the time each is keyed by beside
# its mower, and the time that says how old a row is. A gap is as old as its end. A Job and a
# mower are kept whatever their age.
EXPIRING = {
    "trail_point": ("device_time", "device_time"),
    "job_progress": ("device_time", "device_time"),
    "mower_state": ("device_time", "device_time"),
    "collector_gap": ("start_time", "end_time"),
}
# The readings, each keyed by the mower and the time it is of: what TimescaleDB holds in
# hypertables. A gap is not one of them: it is as old as its end, which is not its key.
READINGS = tuple(table for table, (key, age) in EXPIRING.items() if key == age)
# The schema TimescaleDB's functions are in, where it is installed: not always one the
# connection looks in, so they are called by it.
_TIMESCALEDB = """
    SELECT nspname FROM pg_extension JOIN pg_namespace ON pg_namespace.oid = extnamespace
    WHERE extname = 'timescaledb'
"""
_HYPERTABLES = """
    SELECT hypertable_name::text FROM timescaledb_information.hypertables
    WHERE hypertable_schema = current_schema()
"""
# The tables of this schema that TimescaleDB keeps to a retention policy, and for how long.
_POLICIES = """
    SELECT hypertable_name::text, (config->>'drop_after')::interval
    FROM timescaledb_information.jobs
    WHERE proc_name = 'policy_retention' AND hypertable_schema = current_schema()
"""
# Every statement of a removal can go by the key, which begins with the mower: reading
# millions of positions through to find what is old, on the small machine this shares with
# its database, would take longer than a statement is given. What earlier removals took is
# passed over at the start of each, for as long as the database has not cleaned it up:
# quickly, unless something has kept it from cleaning up at all for days.
# The mowers a table has rows of, each found from the one before it.
_MOWERS = """
    WITH RECURSIVE mowers AS (
        (SELECT mower_id FROM {table} ORDER BY mower_id LIMIT 1)
        UNION ALL
        SELECT (SELECT mower_id FROM {table} WHERE mower_id > mowers.mower_id
                ORDER BY mower_id LIMIT 1)
        FROM mowers WHERE mower_id IS NOT NULL
    )
    SELECT mower_id FROM mowers WHERE mower_id IS NOT NULL
"""
# One mower's oldest rows past `after`, where the batch before this one ended, so that the
# rows it removed are not walked over again: how many there are, to a batch at most, and the
# key of the last. A row older than `before` by its age is so by its key as well.
_OLD = """
    SELECT count(*), max({key}) FROM (
        SELECT {key} FROM {table}
        WHERE mower_id = %(mower)s AND {key} > %(after)s AND {key} < %(before)s
            AND {age} < %(before)s
        ORDER BY {key} LIMIT %(batch)s
    ) AS oldest
"""
# The rows as far as that last key, each asked its age once more: a gap recorded again since
# it was counted, as ending later, is no longer the old gap it was.
_REMOVE = """
    DELETE FROM {table}
    WHERE mower_id = %(mower)s AND {key} > %(after)s AND {key} <= %(last)s
        AND {age} < %(before)s
"""
_EARLIEST = datetime.min.replace(tzinfo=UTC)
# For the transaction it is run in, PostgreSQL finds rows by an index wherever one will
# do. What it believes a table holds can be far from it, just after a capture of last year
# is replayed or on a table it has not yet looked at, and it would then read every old row
# through for each batch, or sort them.
_BY_THE_KEY = (
    "SELECT set_config('enable_seqscan', 'off', true),"
    " set_config('enable_bitmapscan', 'off', true), set_config('enable_sort', 'off', true)"
)


def _kept_for(days: int | None) -> timedelta | None:
    """How long rows are kept, of a number of days; None if for ever, which is also to keep
    them for longer than dates go back."""
    if days is None:
        return None
    try:
        keep = timedelta(days=days)
        _ = datetime.now(UTC) - keep  # fails where that is before dates begin
    except OverflowError:
        return None
    return keep


def _insert(row: type[Row], table: str) -> str:
    """Insert every field of the row into the column of the same name."""
    columns = [field.name for field in fields(row)]
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"


def _values(job: Job) -> tuple[object, ...]:
    """A Job's values in column order. psycopg writes a list as an array, a tuple as a record."""
    return tuple(list(value) if isinstance(value, tuple) else value for value in astuple(job))


def _job(row: tuple[Any, ...]) -> Job:
    """The Job a row of its columns holds, with the array of its Zones as the tuple a Job keeps."""
    job = Job(*row)
    return job if job.zones is None else replace(job, zones=tuple(job.zones))


# Whatever may change in a Job once it is stored: everything but its key.
_JOB_CHANGES = ", ".join(f"{field.name} = EXCLUDED.{field.name}" for field in fields(Job)[2:])


class PostgresStorage:
    """Persist Trail points and gaps with idempotent inserts."""

    def __init__(self, config: StorageConfig) -> None:
        if config.dsn is None:
            raise StorageError("storage.dsn is required for the postgres backend")
        with _translated():
            deadlines = {"connect_timeout": CONNECT_TIMEOUT_SECONDS, **_DEAD_PEER}
            # The operator's DSN wins wherever it sets one of these itself.
            parameters = {**deadlines, **conninfo_to_dict(config.dsn.reveal())}
            self._connection = psycopg.connect(make_conninfo("", **parameters), autocommit=True)
            # Only where nothing set it: not the DSN's options, the role, the database or
            # the server's configuration. A statement that legitimately takes longer than
            # this default would otherwise time out on every retry, for ever.
            self._connection.execute(
                "SELECT set_config('statement_timeout', %s, false) FROM pg_settings"
                " WHERE name = 'statement_timeout' AND source = 'default'",
                (str(STATEMENT_TIMEOUT_MS),),
            )

    def __enter__(self) -> PostgresStorage:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def migrate(self) -> None:
        with _translated(), self._connection.transaction():
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version integer PRIMARY KEY, applied_at timestamptz DEFAULT now())"
            )
            applied = {
                row[0] for row in self._connection.execute("SELECT version FROM schema_version")
            }
            for version, statement in enumerate(MIGRATIONS, 1):
                if version not in applied:
                    self._connection.execute(statement)
                    self._connection.execute(
                        "INSERT INTO schema_version (version) VALUES (%s)", (version,)
                    )
            self._hold_in_hypertables()

    def _timescaledb(self) -> sql.Identifier | None:
        """The schema TimescaleDB's functions are in; None where it is not installed."""
        found = self._connection.execute(_TIMESCALEDB).fetchone()
        return None if found is None else sql.Identifier(found[0])

    def _hold_in_hypertables(self) -> None:
        """Where TimescaleDB is installed, make hypertables of the readings.

        Only of a table with no rows. Making one of a table that holds a Trail rewrites it
        under a lock for as long as that takes, which is the owner's to choose a moment for;
        left as it is, its old rows are removed as PostgreSQL's are. And only if TimescaleDB
        will: a table it refuses, as to a user who does not own it, stays as it is.
        """
        timescaledb = self._timescaledb()
        if timescaledb is None:
            return
        # The key already serves every question asked by time: no index besides. Another
        # collector starting may have made the hypertable meanwhile.
        hold = sql.SQL(
            "SELECT {}.create_hypertable(%s, %s, create_default_indexes => false,"
            " if_not_exists => true)"
        ).format(timescaledb)
        try:
            with self._connection.transaction():
                held = {row[0] for row in self._connection.execute(_HYPERTABLES)}
                for table in READINGS:
                    rows = self._connection.execute(f"SELECT FROM {table} LIMIT 1").fetchone()
                    if table not in held and rows is None:
                        self._connection.execute(hold, (table, EXPIRING[table][0]))
        except psycopg.Error as error:
            self._carry_on_without("TimescaleDB made no hypertables of the readings", error)

    def keep_for(self, days: int | None) -> None:
        """Where TimescaleDB holds readings in hypertables, have it keep them for as long as
        the owner does: a retention policy of so many days, or none.

        Whatever it is given no policy for, because it will not take one (under its Apache
        licence it takes none) or because the table is no hypertable, the collector goes on
        removing old rows from itself.
        """
        with _translated():
            timescaledb = self._timescaledb()
            if timescaledb is None:
                return
            wanted = _kept_for(days)
            forget = sql.SQL("SELECT {}.remove_retention_policy(%s, if_exists => true)")
            keep = sql.SQL("SELECT {}.add_retention_policy(%s, drop_after => %s)")
            try:
                with self._connection.transaction():
                    kept = self._policies()
                    held = {row[0] for row in self._connection.execute(_HYPERTABLES)}
                    for table in held.intersection(READINGS):
                        if kept.get(table) != wanted:
                            self._connection.execute(forget.format(timescaledb), (table,))
                            if wanted is not None:
                                self._connection.execute(keep.format(timescaledb), (table, wanted))
            except psycopg.Error as error:
                self._carry_on_without("TimescaleDB was not told how long to keep readings", error)

    def _carry_on_without(self, what: str, error: psycopg.Error) -> None:
        """TimescaleDB refused something the collector does as well without: say so and go
        on, unless it is the connection that failed, which is an outage like any other."""
        if self._connection.broken:
            raise error
        _LOGGER.warning("%s, and the collector removes old rows itself: %s", what, error)

    def _policies(self) -> dict[str, timedelta]:
        """The tables TimescaleDB keeps to a retention policy, and for how long each; none
        where it is not installed."""
        if self._timescaledb() is None:
            return {}
        return dict(self._connection.execute(_POLICIES).fetchall())

    def check_schema(self) -> None:
        for table in TABLES:
            with _translated():
                row = self._connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()
            if row is None or row[0] is None:
                raise SchemaError(
                    "collector schema is missing; enable storage.migrate or apply migrations"
                )

    def latest_jobs(self) -> Sequence[Job]:
        columns = ", ".join(field.name for field in fields(Job))
        with _translated():
            found = self._connection.execute(
                f"SELECT DISTINCT ON (mower_id) {columns} FROM job"
                " ORDER BY mower_id, start_time DESC"
            ).fetchall()
        return [_job(row) for row in found]

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        return self._write(
            _insert(TrailPoint, "trail_point") + " ON CONFLICT DO NOTHING",
            [astuple(point) for point in points],
        )

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        return self._write(
            """
            INSERT INTO collector_gap (mower_id, start_time, end_time, reason)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (mower_id, start_time) DO UPDATE
                SET end_time = EXCLUDED.end_time, reason = EXCLUDED.reason
                WHERE collector_gap.end_time < EXCLUDED.end_time
            """,
            [(gap.mower_id, gap.start_time, gap.end_time, gap.reason.value) for gap in gaps],
        )

    def write_jobs(self, jobs: Sequence[Job]) -> int:
        return self._write(
            _insert(Job, "job") + f" ON CONFLICT (mower_id, job_id) DO UPDATE SET {_JOB_CHANGES}"
            " WHERE job.updated_time <= EXCLUDED.updated_time",
            [_values(job) for job in jobs],
        )

    def write_progress(self, reports: Sequence[Progress]) -> int:
        return self._write(
            _insert(Progress, "job_progress") + " ON CONFLICT DO NOTHING",
            [astuple(report) for report in reports],
        )

    def write_states(self, states: Sequence[MowerState]) -> int:
        return self._write(
            _insert(MowerState, "mower_state") + " ON CONFLICT DO NOTHING",
            [astuple(state) for state in states],
        )

    def write_mowers(self, mowers: Sequence[Mower]) -> int:
        return self._write(
            _insert(Mower, "mower") + " ON CONFLICT (mower_id) DO UPDATE"
            " SET name = EXCLUDED.name, model = EXCLUDED.model, firmware = EXCLUDED.firmware,"
            " updated_time = EXCLUDED.updated_time"
            " WHERE mower.updated_time < EXCLUDED.updated_time"
            " AND (mower.name, mower.model, mower.firmware)"
            " IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.model, EXCLUDED.firmware)",
            [astuple(mower) for mower in mowers],
        )

    def remove_older_than(self, before: datetime, batch: int) -> int:
        removed = 0
        with _translated():
            # What TimescaleDB was given the rule for is its own to remove.
            left_to_the_engine = self._policies()
            for table, (key, age) in EXPIRING.items():
                if table in left_to_the_engine:
                    continue
                names = {"table": table, "key": key, "age": age}
                mowers = self._connection.execute(_MOWERS.format(**names)).fetchall()
                for (mower,) in mowers:
                    bounds = {"mower": mower, "after": _EARLIEST, "before": before}
                    found = batch
                    while found and found == batch:  # a batch that found fewer found the last
                        with self._connection.transaction():
                            self._connection.execute(_BY_THE_KEY)
                            oldest = self._connection.execute(
                                _OLD.format(**names), {**bounds, "batch": batch}
                            ).fetchone()
                            found, last = oldest or (0, None)
                            if found:
                                removed += self._connection.execute(
                                    _REMOVE.format(**names), {**bounds, "last": last}
                                ).rowcount
                                bounds["after"] = last
        return removed

    def _write(self, statement: str, rows: Sequence[tuple[object, ...]]) -> int:
        with _translated(), self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.executemany(statement, rows)
            return max(cursor.rowcount, 0)

    def close(self) -> None:
        self._connection.close()
