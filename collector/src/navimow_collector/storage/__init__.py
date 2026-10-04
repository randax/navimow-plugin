"""Storage boundary and backend selection for collector ingestion."""

from __future__ import annotations

from ..config import StorageConfig
from .base import RejectedError, SchemaError, Storage, StorageError, Writer
from .postgres import PostgresStorage

__all__ = [
    "STORAGE_BACKENDS",
    "RejectedError",
    "SchemaError",
    "Storage",
    "StorageError",
    "Writer",
    "open_storage",
]

STORAGE_BACKENDS = {"postgres": PostgresStorage}


def open_storage(config: StorageConfig) -> Storage:
    """Open the configured backend with its schema migrated, or checked when migration is off."""
    storage: Storage = STORAGE_BACKENDS[config.backend](config)
    try:
        if config.migrate:
            storage.migrate()
        else:
            storage.check_schema()
    except BaseException:
        storage.close()
        raise
    return storage
