"""Hold rows through a database outage, so restarting the database does not cost the Job."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict
from datetime import datetime
from itertools import islice
from pathlib import Path
from time import monotonic
from typing import Any

from ..records import Gap, TrailPoint
from .base import Storage, StorageError

RETRY_SECONDS = 10
# About half an hour of one mower's Trail; a longer outage continues on disk.
MEMORY_ROWS = 1000
# Roughly ten days of continuous mowing, so a full disk is never the collector's doing.
DISK_BYTES = 64 * 1024 * 1024
_BATCH_ROWS = 500
_ROWS: dict[str, type[TrailPoint] | type[Gap]] = {"TrailPoint": TrailPoint, "Gap": Gap}

Row = TrailPoint | Gap
_LOGGER = logging.getLogger(__name__)


class BufferedStorage:
    """The writer live collection uses: rows the database cannot take now are kept for later.

    Rows wait in memory, spill to a file once memory holds `memory_rows`, and are written
    when the database returns. Every insert is idempotent and every row carries its own
    time, so neither a repeated row nor the order of a flush can corrupt a Trail.
    """

    def __init__(
        self,
        opener: Callable[[], Storage],
        spill: Path,
        *,
        memory_rows: int = MEMORY_ROWS,
        disk_bytes: int = DISK_BYTES,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._open = opener
        self._spill = spill
        self._memory_rows = memory_rows
        self._disk_bytes = disk_bytes
        self._clock = clock
        self._storage: Storage | None = None
        self._memory: list[Row] = []
        self._spilled = _count_lines(spill)  # left by a previous process
        self._retry_at = 0.0
        self.dropped = 0

    @property
    def reachable(self) -> bool:
        return self._storage is not None

    @property
    def buffered(self) -> int:
        """How many rows are waiting for the database."""
        return len(self._memory) + self._spilled

    def connect(self) -> None:
        """Open the database now, so a misconfiguration fails at startup, not into the buffer."""
        self._storage = self._open()

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        return self._write(points)

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        return self._write(gaps)

    def flush(self) -> None:
        """Write what is waiting, unless the database was found unreachable moments ago."""
        if not self.buffered or self._clock() < self._retry_at:
            return
        waiting = self.buffered
        try:
            for batch in self._spilled_batches():
                self._send(batch)
            self._send(self._memory)
        except StorageError as error:
            self._outage(error)
            return
        self._spill.unlink(missing_ok=True)
        self._spilled = 0
        self._memory.clear()
        _LOGGER.info("Database reachable again; wrote %d buffered rows", waiting)

    def close(self) -> None:
        """Make one last attempt, then leave what is still waiting on disk for the next start."""
        self._retry_at = 0.0
        self.flush()
        if self._memory:
            self._spill_memory()
        if self._storage is not None:
            self._storage.close()
            self._storage = None

    def _write(self, rows: Sequence[Row]) -> int:
        self.flush()
        if not self.buffered:
            try:
                return self._send(rows)
            except StorageError as error:
                self._outage(error)
        self._memory.extend(rows)
        if len(self._memory) > self._memory_rows:
            self._spill_memory()
        return 0

    def _send(self, rows: Sequence[Row]) -> int:
        if self._storage is None:
            self._storage = self._open()
        points = [row for row in rows if isinstance(row, TrailPoint)]
        gaps = [row for row in rows if isinstance(row, Gap)]
        written = self._storage.write_trail(points) if points else 0
        return written + (self._storage.write_gaps(gaps) if gaps else 0)

    def _outage(self, error: StorageError) -> None:
        if self._storage is not None:
            self._storage.close()
            self._storage = None
        if not self.buffered:  # said once per outage, not once per retry
            _LOGGER.warning("Database unreachable; buffering until it returns: %s", error)
        self._retry_at = self._clock() + RETRY_SECONDS

    def _spill_memory(self) -> None:
        """Move memory to disk, or drop it once the disk holds its limit: newest rows lose."""
        try:
            full = self._spill.stat().st_size >= self._disk_bytes
        except FileNotFoundError:
            full = False
        if full:
            if not self.dropped:
                _LOGGER.error(
                    "Buffer %s is full; dropping rows until the database returns", self._spill
                )
            self.dropped += len(self._memory)
        else:
            self._spill.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._spill.open("a", encoding="utf-8") as spill:
                spill.writelines(_encode(row) + "\n" for row in self._memory)
            self._spilled += len(self._memory)
        self._memory.clear()

    def _spilled_batches(self) -> Iterator[list[Row]]:
        if not self._spilled:
            return
        with self._spill.open(encoding="utf-8") as spill:
            while lines := list(islice(spill, _BATCH_ROWS)):
                # A line cut short by a crash is skipped rather than blocking the rest.
                yield [row for row in map(_decode, lines) if row is not None]


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as spill:
            return sum(1 for _ in spill)
    except FileNotFoundError:
        return 0


def _encode(row: Row) -> str:
    values = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in asdict(row).items()
    }
    return json.dumps({"row": type(row).__name__, **values})


def _decode(line: str) -> Row | None:
    try:
        values: dict[str, Any] = json.loads(line)
        kind = _ROWS[values.pop("row")]
        for key in values:
            if key.endswith("_time"):
                values[key] = datetime.fromisoformat(values[key])
        return kind(**values)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
