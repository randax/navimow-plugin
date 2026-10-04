"""Hold rows through a database outage, so restarting the database does not cost the Job."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime
from itertools import islice
from pathlib import Path
from time import monotonic
from typing import Any, BinaryIO

from ..records import Gap, GapReason, TrailPoint
from .base import RejectedError, Storage, StorageError

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

    Nothing here stops collection: a database that is away is waited for, a row it rejects
    is dropped, and a buffer file that misbehaves costs rows, never the process.
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
        # How far into the file this process has already sent, when it could not remove it.
        self._replayed = 0
        self._retry_at = 0.0
        self._dropping = False  # whether this outage's loss has been reported yet
        self.dropped = 0
        self.rejected = 0
        # Rejected rows whose batch may yet be retried after an outage: said and counted once.
        self._refused: set[Row] = set()

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
            if self._spilled:
                self._replay_spill()
            self._send(self._memory)
        except StorageError as error:
            self._outage(error)
            return
        self._memory.clear()
        self._dropping = False
        self._refused.clear()
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
                written = self._send(rows)
                self._refused.clear()
                return written
            except StorageError as error:
                self._outage(error)
        self._memory.extend(rows)
        # A gap goes to disk at once: its writer forgets when it started as soon as it has
        # handed it over, so it must not be lost with this process.
        if len(self._memory) > self._memory_rows or any(isinstance(row, Gap) for row in rows):
            self._spill_memory()
        return 0

    def _send(self, rows: Sequence[Row]) -> int:
        """Write rows now, raising StorageError only when the database cannot be reached."""
        if self._storage is None:
            self._storage = self._open()
        points = [row for row in rows if isinstance(row, TrailPoint)]
        gaps = [row for row in rows if isinstance(row, Gap)]
        try:
            written = self._storage.write_trail(points) if points else 0
            return written + (self._storage.write_gaps(gaps) if gaps else 0)
        except RejectedError as error:
            # Retrying cannot help a row the database refuses for what it holds, and it
            # would keep every row behind it waiting: find it, drop it, keep the rest.
            if len(rows) > 1:
                return sum(self._send([row]) for row in rows)
            if rows[0] not in self._refused:
                self._refused.add(rows[0])
                self.rejected += 1
                _LOGGER.error("Dropping a row the database rejected (%s): %r", error, rows[0])
            return 0

    def _outage(self, error: StorageError) -> None:
        if self._storage is not None:
            self._storage.close()
            self._storage = None
        if not self.buffered:  # said once per outage, not once per retry
            _LOGGER.warning("Database unreachable; buffering until it returns: %s", error)
        self._retry_at = self._clock() + RETRY_SECONDS

    def _spill_memory(self) -> None:
        """Move memory to disk. Rows the file has no room for, or cannot take, stay in memory
        up to its limit; beyond that the newest are dropped, so memory is bounded whatever
        happens to the disk."""
        kept, problem = 0, "it has reached its size limit"
        try:
            self._spill.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._spill.open("ab+", buffering=0) as spill:
                room = self._disk_bytes - _end_torn_line(spill)
                lines: list[bytes] = []
                for row in self._memory:
                    line = _encode(row)
                    if len(line) > room:
                        break
                    lines.append(line)
                    room -= len(line)
                if data := b"".join(lines):
                    # Unbuffered, so the count is what reached the file: a disk that fills
                    # up mid-write keeps the rows that fitted; the torn one is ended next time.
                    written = spill.write(data) or 0
                    kept = data[:written].count(b"\n")
                    os.fsync(spill.fileno())
                    if written < len(data):
                        problem = "the write was cut short"
        except OSError as error:
            problem = str(error)
        self._spilled += kept
        del self._memory[:kept]
        lost = len(self._memory) - self._memory_rows
        if lost > 0:
            if not self._dropping:
                _LOGGER.error(
                    "Cannot buffer to %s (%s); dropping rows until the database returns",
                    self._spill,
                    problem,
                )
            self._dropping = True
            self.dropped += lost
            del self._memory[self._memory_rows :]

    def _replay_spill(self) -> None:
        """Send what the buffer file holds, then remove it.

        A file that cannot be read keeps its rows for the next start; one that cannot be
        removed (a filesystem gone read-only) is remembered as sent up to where it ends.
        """
        try:
            with self._spill.open("rb") as spill:
                spill.seek(self._replayed)
                while lines := list(islice(spill, _BATCH_ROWS)):
                    # A line cut short by a crash is skipped rather than blocking the rest.
                    self._send([row for row in map(_decode, lines) if row is not None])
                self._replayed = spill.tell()
            self._spill.unlink()
            self._replayed = 0
        except OSError as error:
            _LOGGER.error("Buffer file %s could not be read and removed: %s", self._spill, error)
        self._spilled = 0


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as spill:
            return sum(1 for _ in spill)
    except OSError:
        return 0


def _end_torn_line(spill: BinaryIO) -> int:
    """End a last line that an interrupted write left unfinished, so that the next row
    starts a line of its own; return the size of the file."""
    size = spill.seek(0, os.SEEK_END)
    if size:
        spill.seek(size - 1)
        if spill.read(1) != b"\n":
            size += spill.write(b"\n")
    return size


def _encode(row: Row) -> bytes:
    values = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in asdict(row).items()
    }
    return json.dumps({"row": type(row).__name__, **values}).encode() + b"\n"


def _decode(line: bytes) -> Row | None:
    try:
        values: dict[str, Any] = json.loads(line)
        kind = _ROWS[values.pop("row")]
        for key in values:
            if key.endswith("_time"):
                values[key] = datetime.fromisoformat(values[key])
        if kind is Gap:
            values["reason"] = GapReason(values["reason"])
        return kind(**values)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
