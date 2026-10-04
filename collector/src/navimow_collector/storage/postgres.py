"""PostgreSQL storage for the provisional Trail and gap schema."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import psycopg

from ..config import StorageConfig
from ..records import Gap, TrailPoint
from .base import SchemaError, StorageError


@contextmanager
def _translated() -> Iterator[None]:
    """Keep psycopg's exceptions behind the storage boundary."""
    try:
        yield
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
    # One row per mower and outage: a gap starts once, so a redelivered gap is skipped.
    """
    CREATE TABLE IF NOT EXISTS collector_gap (
        mower_id text NOT NULL,
        start_time timestamptz NOT NULL,
        end_time timestamptz NOT NULL,
        reason text NOT NULL,
        PRIMARY KEY (mower_id, start_time)
    )
    """,
)
TABLES = ("trail_point", "collector_gap")


class PostgresStorage:
    """Persist Trail points and gaps with idempotent inserts."""

    def __init__(self, config: StorageConfig) -> None:
        if config.dsn is None:
            raise StorageError("storage.dsn is required for the postgres backend")
        with _translated():
            self._connection = psycopg.connect(config.dsn.reveal(), autocommit=True)

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

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        with _translated(), self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO trail_point
                    (mower_id, device_time, received_time, x, y, theta, vehicle_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                [
                    (
                        point.mower_id,
                        point.device_time,
                        point.received_time,
                        point.x,
                        point.y,
                        point.theta,
                        point.vehicle_state,
                    )
                    for point in points
                ],
            )
            return max(cursor.rowcount, 0)

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        with _translated(), self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO collector_gap (mower_id, start_time, end_time, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                [(gap.mower_id, gap.start_time, gap.end_time, gap.reason) for gap in gaps],
            )
            return max(cursor.rowcount, 0)

    def close(self) -> None:
        self._connection.close()
