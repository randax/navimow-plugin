"""The storage boundary every backend implements."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, Self

from ..records import Gap, TrailPoint


class StorageError(Exception):
    """The database could not be reached or refused a write."""


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


class Storage(Writer, Protocol):
    """A database backend: a writer which also owns its schema and connection."""

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def migrate(self) -> None: ...

    def check_schema(self) -> None: ...

    def close(self) -> None: ...
