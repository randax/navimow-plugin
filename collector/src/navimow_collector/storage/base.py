"""The storage boundary every backend implements."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, Self

from ..records import Gap, Job, Mower, MowerState, Progress, Row, TrailPoint


class StorageError(Exception):
    """The database could not be reached or refused a write."""


class RejectedError(StorageError):
    """The database was reached but refuses these rows for what they hold; a retry cannot help."""


class SchemaError(StorageError):
    """The configured database does not have the collector schema."""


class Writer(Protocol):
    """The persistent boundary used by the transport-free ingestion core."""

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        """Store points, skipping any already stored; return how many were new."""
        ...

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        """Store gaps; one already stored is extended if this one ends later, and skipped
        otherwise. Return how many were new or extended."""
        ...

    def write_jobs(self, jobs: Sequence[Job]) -> int:
        """Store Jobs; a version of one already stored replaces it unless it is older.
        Return how many were new or replaced."""
        ...

    def write_progress(self, reports: Sequence[Progress]) -> int:
        """Store progress reports, skipping any already stored; return how many were new."""
        ...

    def write_states(self, states: Sequence[MowerState]) -> int:
        """Store states, skipping any already stored; return how many were new."""
        ...

    def write_mowers(self, mowers: Sequence[Mower]) -> int:
        """Store mowers; one already stored is replaced by a later description that differs.
        Return how many were new or replaced."""
        ...


def write_rows(writer: Writer, rows: Sequence[Row]) -> dict[type[Row], int]:
    """Hand each kind of row to its writer, a Job before the rows which name it; return how
    many rows of each kind were stored."""
    stored: dict[type[Row], int] = {}
    if jobs := [row for row in rows if isinstance(row, Job)]:
        stored[Job] = writer.write_jobs(jobs)
    if points := [row for row in rows if isinstance(row, TrailPoint)]:
        stored[TrailPoint] = writer.write_trail(points)
    if reports := [row for row in rows if isinstance(row, Progress)]:
        stored[Progress] = writer.write_progress(reports)
    if states := [row for row in rows if isinstance(row, MowerState)]:
        stored[MowerState] = writer.write_states(states)
    if gaps := [row for row in rows if isinstance(row, Gap)]:
        stored[Gap] = writer.write_gaps(gaps)
    if mowers := [row for row in rows if isinstance(row, Mower)]:
        stored[Mower] = writer.write_mowers(mowers)
    return stored


class Storage(Writer, Protocol):
    """A database backend: a writer which also owns its schema and connection."""

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def migrate(self) -> None: ...

    def latest_jobs(self) -> Sequence[Job]:
        """Each mower's most recent Job, for a collector starting up to carry on from."""
        ...

    def remove_older_than(self, before: datetime, batch: int) -> int:
        """Remove the positions, progress reports, states and gaps from before then, never
        a Job or a mower, at most `batch` of them a statement; return how many were removed."""
        ...

    def check_schema(self) -> None: ...

    def close(self) -> None: ...
