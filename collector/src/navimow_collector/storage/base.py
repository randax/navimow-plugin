"""The storage boundary every backend implements."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, Self

from ..records import TrailPoint


class StorageError(Exception):
    """The database could not be reached or refused a write."""


class SchemaError(StorageError):
    """The configured database does not have the collector schema."""


class Storage(Protocol):
    """The persistent boundary used by the transport-free ingestion core."""

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def migrate(self) -> None: ...

    def check_schema(self) -> None: ...

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        """Store points, skipping any already stored; return how many were new."""
        ...

    def close(self) -> None: ...
