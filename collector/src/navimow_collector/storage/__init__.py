"""Storage boundary and backend selection for collector ingestion."""

from __future__ import annotations

from ..config import StorageConfig
from .base import RejectedError, SchemaError, Storage, StorageError, Writer, write_rows
from .postgres import PostgresStorage

__all__ = [
    "STORAGE_BACKENDS",
    "RejectedError",
    "SchemaError",
    "Storage",
    "StorageError",
    "Writer",
    "open_for_collection",
    "open_storage",
    "write_rows",
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


def open_for_collection(config: StorageConfig) -> Storage:
    """Open the configured backend as live collection uses it: the database is also told
    how long the owner keeps rows, wherever it removes old ones itself. `replay` opens
    without, and so neither removes anything nor changes what the database was told. Nor
    is a database told anything whose schema is not the collector's to change."""
    storage = open_storage(config)
    try:
        if config.migrate:
            storage.keep_for(config.retention_days)
    except BaseException:
        storage.close()
        raise
    return storage
