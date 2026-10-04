"""Hold rows through a database outage, so restarting the database does not cost the Job."""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable, Sequence
from contextlib import suppress
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
# How many waiting rows one call writes; live collection calls from its event loop.
REPLAY_ROWS = 200
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
        # What a previous process left. A file that is there but cannot be read owes rows
        # all the same: it stands for one until the first read can count them.
        waiting = _count_lines(spill)
        self._uncounted = waiting is None
        self._spilled = 1 if waiting is None else waiting
        # How far into the file this process has sent, and which file that was: a backlog
        # is sent a slice at a time, and a file that was sent cannot always be removed.
        self._replayed = 0
        self._replayed_file: tuple[int, int] | None = None
        self._unreadable_until: float | None = None  # the file could not be read just now
        self._retry_at = 0.0
        self._away = False  # whether the database has been reported unreachable
        self._dropping = False  # whether this outage's loss has been reported yet
        self.dropped = 0
        self.rejected = 0
        # Rows rejected in an attempt an outage then interrupted: its rows will be tried
        # again, and a rejection among them is said and counted once.
        self._refused: set[Row] = set()

    @property
    def reachable(self) -> bool:
        return self._storage is not None

    @property
    def buffered(self) -> int:
        """How many rows are waiting for the database."""
        return len(self._memory) + self._spilled

    @property
    def draining(self) -> bool:
        """Whether rows are waiting which the database is believed able to take now."""
        waiting = bool(self._memory) or self._file_is_due()
        return waiting and self._clock() >= self._retry_at

    def _file_is_due(self) -> bool:
        """Whether the buffer file holds rows to send, and may be read."""
        return bool(self._spilled) and self._clock() >= (self._unreadable_until or 0)

    def connect(self) -> None:
        """Open the database now, so a misconfiguration fails at startup, not into the buffer."""
        self._storage = self._open()

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        return self._write(points)

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        return self._write(gaps)

    def flush(self) -> None:
        """Write some of what is waiting, unless the database was found unreachable just now.

        At most REPLAY_ROWS rows a call, oldest first: the backlog of a long outage written
        in one go would hold the caller for minutes. `draining` says whether to call again.
        """
        if not self.draining:
            return
        room = REPLAY_ROWS
        try:
            if self._file_is_due():
                room -= self._replay_spill(room)
            if not self._file_is_due() and self._memory and room > 0:
                self._send(self._memory[:room])
                del self._memory[:room]
        except StorageError as error:
            self._outage(error)
            return
        if not self.buffered:
            self._dropping = False

    def close(self) -> None:
        """Write one last slice, then leave what is still waiting on disk for the next start."""
        self._retry_at = 0.0
        self.flush()
        if self._memory:
            self._spill_memory()
        if self._memory:
            _LOGGER.error("%d buffered rows could not be saved and are lost", len(self._memory))
            self.dropped += len(self._memory)
            self._memory.clear()
        self._trim_spill()
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
        # A gap goes to disk at once: its writer forgets when it started as soon as it has
        # handed it over, so it must not be lost with this process.
        if len(self._memory) > self._memory_rows or any(isinstance(row, Gap) for row in rows):
            self._spill_memory()
        return 0

    def _send(self, rows: Sequence[Row]) -> int:
        """Write rows now, raising StorageError only when the database cannot be reached."""
        written = self._attempt(rows)
        self._refused.difference_update(rows)  # done with: these will not be tried again
        return written

    def _attempt(self, rows: Sequence[Row]) -> int:
        if self._storage is None:
            self._storage = self._open()
            if self._away:
                _LOGGER.info("Database reachable again; writing what was buffered")
                self._away = False
        points = [row for row in rows if isinstance(row, TrailPoint)]
        gaps = [row for row in rows if isinstance(row, Gap)]
        try:
            written = self._storage.write_trail(points) if points else 0
            return written + (self._storage.write_gaps(gaps) if gaps else 0)
        except RejectedError as error:
            # Retrying cannot help a row the database refuses for what it holds, and it
            # would keep every row behind it waiting: find it, drop it, keep the rest.
            if len(rows) > 1:
                return sum(self._attempt([row]) for row in rows)
            if rows[0] not in self._refused:
                self._refused.add(rows[0])
                self.rejected += 1
                _LOGGER.error("Dropping a row the database rejected (%s): %r", error, rows[0])
            return 0

    def _outage(self, error: StorageError) -> None:
        if self._storage is not None:
            self._storage.close()
            self._storage = None
        if not self._away:  # said once per outage, not once per retry
            _LOGGER.warning("Database unreachable; buffering until it returns: %s", error)
        self._away = True
        self._retry_at = self._clock() + RETRY_SECONDS

    def _spill_memory(self) -> None:
        """Move memory to disk. Rows the file has no room for, or cannot take, stay in memory
        up to its limit; beyond that the newest Trail points are dropped, so memory is
        bounded whatever happens to the disk.

        The limit is on rows waiting: what a draining file holds that was already sent does
        not count, or nothing could be saved until all of it was. Since those rows stay in
        the file until it is finished, the file itself may reach twice the limit.
        """
        kept, problem = 0, "it has reached its size limit"
        try:
            self._spill.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._spill.open("ab+", buffering=0) as spill:
                self._forget_position_in_another_file(spill)
                size = _end_torn_line(spill)
                room = min(self._disk_bytes - (size - self._replayed), 2 * self._disk_bytes - size)
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
            # A gap goes last: its writer has already forgotten when it began, so unlike a
            # Trail point it leaves no trace of having been lost.
            gaps: list[Row] = [row for row in self._memory if isinstance(row, Gap)]
            points: list[Row] = [row for row in self._memory if isinstance(row, TrailPoint)]
            points = points[: max(self._memory_rows - len(gaps), 0)]
            self._memory = [*points, *gaps][: self._memory_rows]

    def _replay_spill(self, limit: int) -> int:
        """Send up to `limit` lines of the buffer file, from where the last call stopped,
        and remove the file once all of it is sent; return how many lines were taken.

        A file that cannot be read still owes its rows and is tried again after the retry
        interval; one that cannot be removed (a filesystem gone read-only) is remembered
        as sent up to where it ends.
        """
        try:
            with self._spill.open("rb") as spill:
                self._forget_position_in_another_file(spill)
                if self._uncounted:
                    self._spilled, self._uncounted = sum(1 for _ in spill), False
                spill.seek(self._replayed)
                lines = list(islice(spill, limit))
                self._unreadable_until = None
                # A line cut short by a crash is skipped rather than blocking the rest.
                self._send([row for row in map(_decode, lines) if row is not None])
                self._replayed, self._replayed_file = spill.tell(), _identity(spill)
                if spill.read(1):
                    self._spilled = max(self._spilled - len(lines), 1)  # more for a later call
                    return len(lines)
        except (FileNotFoundError, NotADirectoryError):
            # A file never read was never counted: its loss is said, not given a number.
            lost = "an unknown number of" if self._uncounted else self._spilled
            _LOGGER.error("Buffer file %s is gone, and %s unsent rows with it", self._spill, lost)
            if not self._uncounted:
                self.dropped += self._spilled
            self._spilled, self._replayed, self._uncounted = 0, 0, False
            return 0
        except OSError as error:
            if self._unreadable_until is None:  # said once, not once per retry
                _LOGGER.error("Buffer file %s cannot be read; will retry: %s", self._spill, error)
            self._unreadable_until = self._clock() + RETRY_SECONDS
            return 0
        self._spilled = 0
        try:
            self._spill.unlink(missing_ok=True)
            self._replayed = 0
        except OSError as error:
            _LOGGER.error("Buffer file %s is sent but cannot be removed: %s", self._spill, error)
        return len(lines)

    def _trim_spill(self) -> None:
        """On a clean stop, cut the rows already sent off the front of the buffer file: the
        next process starts the file from its first row. After a crash it sends them again,
        which costs time, not correctness."""
        if not self._replayed:
            return
        unsent = self._spill.with_name(f".{self._spill.name}.tmp")
        try:
            with self._spill.open("rb") as spill, unsent.open("wb") as rest:
                self._forget_position_in_another_file(spill)
                spill.seek(self._replayed)
                shutil.copyfileobj(spill, rest)
                rest.flush()
                os.fsync(rest.fileno())
            os.replace(unsent, self._spill)
            self._replayed = 0
        except OSError as error:
            _LOGGER.warning(
                "Could not trim %s; the next start re-sends rows: %s", self._spill, error
            )
            with suppress(OSError):
                unsent.unlink(missing_ok=True)

    def _forget_position_in_another_file(self, spill: BinaryIO) -> None:
        """A file emptied or replaced behind the collector (an operator making room) is not
        the file that was sent: how far that one was sent says nothing about this one."""
        if _identity(spill) != self._replayed_file or _size(spill) < self._replayed:
            self._replayed = 0


def _count_lines(path: Path) -> int | None:
    """How many rows the file holds: none if it is not there, unknown if it cannot be read."""
    try:
        with path.open("rb") as spill:
            return sum(1 for _ in spill)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    except OSError:
        return None


def _identity(spill: BinaryIO) -> tuple[int, int]:
    status = os.fstat(spill.fileno())
    return status.st_dev, status.st_ino


def _size(spill: BinaryIO) -> int:
    return os.fstat(spill.fileno()).st_size


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
