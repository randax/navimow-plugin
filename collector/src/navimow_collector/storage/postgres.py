"""PostgreSQL storage for the provisional schema."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import astuple, fields

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ..config import StorageConfig
from ..records import Gap, Job, MowerState, Progress, TrailPoint
from .base import RejectedError, SchemaError, StorageError

# Live collection writes from its event loop, so a database call should not wait for long:
# by default a connection attempt, a statement held up by a lock and a server that vanished
# from the network each fail within seconds, which the live buffer treats as an outage.
# These are defaults: whatever the operator set for the same thing is left alone.
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


# The layout is provisional until schema decision #9. Migrations are additive only:
# never drop or rename a column, so newer consumers can tolerate older collectors.
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
)
TABLES = ("trail_point", "collector_gap", "job", "job_progress", "mower_state")


def _upsert(row: type[Job] | type[Progress], table: str, *, key: int) -> str:
    """Insert every field of the row, replacing a stored row with the same leading `key` fields."""
    columns = [field.name for field in fields(row)]
    replaced = ", ".join(f"{name} = EXCLUDED.{name}" for name in columns[key:])
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
        f" ON CONFLICT ({', '.join(columns[:key])}) DO UPDATE SET {replaced}"
    )


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
        return [Job(*row) for row in found]

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        return self._write(
            """
            INSERT INTO trail_point
                (mower_id, device_time, received_time, x, y, theta, vehicle_state, job_id, zone)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
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
            _upsert(Job, "job", key=2) + " WHERE job.updated_time <= EXCLUDED.updated_time",
            [astuple(job) for job in jobs],
        )

    def write_progress(self, reports: Sequence[Progress]) -> int:
        return self._write(
            _upsert(Progress, "job_progress", key=2), [astuple(report) for report in reports]
        )

    def write_states(self, states: Sequence[MowerState]) -> int:
        return self._write(
            """
            INSERT INTO mower_state
                (mower_id, device_time, received_time, state, battery, job_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            [astuple(state) for state in states],
        )

    def _write(self, statement: str, rows: Sequence[tuple[object, ...]]) -> int:
        with _translated(), self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.executemany(statement, rows)
            return max(cursor.rowcount, 0)

    def close(self) -> None:
        self._connection.close()
