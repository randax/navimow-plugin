"""Hold rows through a database outage, so restarting the database does not cost the Job."""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import os
import shutil
import threading
from collections.abc import Callable, Coroutine, Iterator, Sequence
from concurrent.futures import Future, InvalidStateError
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from time import monotonic
from typing import Any, BinaryIO, TypeVar, cast, get_args

from ..records import Gap, GapReason, Job, Mower, MowerState, Progress, Row, TrailPoint
from .base import RejectedError, Storage, StorageError, write_rows

RETRY_SECONDS = 10
# About half an hour of one mower's Trail; a longer outage continues on disk.
MEMORY_ROWS = 1000
# Roughly ten days of continuous mowing, so a full disk is never the collector's doing.
DISK_BYTES = 64 * 1024 * 1024
# How many waiting rows one attempt writes, so that a backlog is never one long statement.
REPLAY_ROWS = 200
# How long live collection waits for the database to answer one attempt before giving up on
# the connection. Its own clock decides, not the server: well past what a server that is
# answering takes to refuse (the connection and statement timeouts of `postgres`).
WRITE_SECONDS = 30
# How many attempts given up on may be left unanswered before no new connection is opened.
# One fresh connection gets past a backend suspended under the old one; more would only
# crowd a database that is answering nobody.
UNANSWERED_ATTEMPTS = 2
_ROWS: dict[str, type[Row]] = {kind.__name__: kind for kind in get_args(Row)}
_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass(frozen=True)
class _Attempt:
    """What one attempt to write rows came to."""

    storage: Storage | None  # the connection it leaves open
    written: int = 0
    refused: Sequence[tuple[Row, RejectedError]] = ()
    outage: StorageError | None = None  # why the database could not be reached, if so


def _make_attempt(
    storage: Storage | None, opener: Callable[[], Storage], rows: Sequence[Row]
) -> _Attempt:
    """Write rows over the connection given, or a new one. Nothing of the buffer is touched,
    so this may run on a thread of its own."""
    refused: list[tuple[Row, RejectedError]] = []
    try:
        if storage is None:
            storage = opener()
        return _Attempt(storage, _store(storage, rows, refused), refused)
    except StorageError as error:
        if storage is not None:
            storage.close()
        return _Attempt(None, refused=refused, outage=error)


def _store(storage: Storage, rows: Sequence[Row], refused: list[tuple[Row, RejectedError]]) -> int:
    try:
        return sum(write_rows(storage, rows).values())
    except RejectedError as error:
        # Retrying cannot help a row the database refuses for what it holds, and it would
        # keep every row behind it waiting: find it, drop it, keep the rest.
        if len(rows) > 1:
            return sum(_store(storage, [row], refused) for row in rows)
        refused.append((rows[0], error))
        return 0


def _at_once(steps: Coroutine[Any, Any, _T]) -> _T:
    """Run to its end a coroutine with nothing to wait for: the buffer's steps are written
    once, for a caller who waits on the database itself and for one with a loop to keep free."""
    try:
        steps.send(None)
    except StopIteration as done:
        return cast(_T, done.value)
    steps.close()
    raise RuntimeError("the database is waited on elsewhere; use drain()")


class BufferedStorage:
    """A writer for which rows the database cannot take now are kept for later.

    This one waits on the database itself; live collection uses `BackgroundStorage`, which
    does not. Rows wait in memory, spill to a file once memory holds `memory_rows`, and are
    written when the database returns. Every insert is idempotent and every row carries its
    own time, so neither a repeated row nor the order of a flush can corrupt a Trail.

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
        self.written = 0  # rows the database stored, backlog included
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

    def latest_jobs(self) -> Sequence[Job]:
        """Each mower's most recent Job, as the database will have it once what is waiting
        has been written. None from a database that is away, which costs a Job under way
        its continuity, never the collection."""
        try:
            jobs = list(self._storage.latest_jobs()) if self._storage is not None else []
        except StorageError as error:
            self._outage(error)
            jobs = []
        latest: dict[str, Job] = {}
        for job in (*jobs, *(row for row in self._waiting() if isinstance(row, Job))):
            known = latest.get(job.mower_id)
            if known is None or _version(known) <= _version(job):
                latest[job.mower_id] = job
        return list(latest.values())

    def _waiting(self) -> Iterator[Row]:
        """Every row waiting for the database, oldest first. Rows of the buffer file that
        were already sent come too; none do from a file that cannot be read."""
        with suppress(OSError), self._spill.open("rb") as spill:
            yield from (row for row in map(_decode, spill) if row is not None)
        yield from self._memory

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        return self._write(points)

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        return self._write(gaps)

    def write_jobs(self, jobs: Sequence[Job]) -> int:
        return self._write(jobs)

    def write_progress(self, reports: Sequence[Progress]) -> int:
        return self._write(reports)

    def write_states(self, states: Sequence[MowerState]) -> int:
        return self._write(states)

    def write_mowers(self, mowers: Sequence[Mower]) -> int:
        return self._write(mowers)

    def flush(self) -> None:
        """Write some of what is waiting, unless the database was found unreachable just now.

        At most REPLAY_ROWS rows a call, oldest first: the backlog of a long outage written
        in one go would hold the caller for minutes. `draining` says whether to call again.
        """
        _at_once(self._flush())

    async def _flush(self) -> None:
        if not self.draining:
            return
        room = REPLAY_ROWS
        try:
            if self._file_is_due():
                room -= await self._replay_spill(room)
            if not self._file_is_due() and self._memory and room > 0:
                await self._send_memory(room)
        except StorageError as error:
            self._outage(error)
            return
        if not self.buffered:
            self._dropping = False

    def close(self) -> None:
        """Write one last slice, then leave what is still waiting on disk for the next start."""
        self._retry_at = 0.0
        self.flush()
        self._save()

    def _save(self) -> None:
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
                return _at_once(self._send(rows))
            except StorageError as error:
                self._outage(error)
        self._admit(rows)
        return 0

    def _admit(self, rows: Sequence[Row]) -> None:
        """Keep rows for a later flush: in memory, and beyond what it may hold in the file."""
        self._memory.extend(rows)
        # A gap goes to disk at once: its writer forgets when it started as soon as it has
        # handed it over, so it must not be lost with this process.
        if len(self._memory) > self._memory_rows or any(isinstance(row, Gap) for row in rows):
            self._spill_memory()

    async def _send_memory(self, room: int) -> None:
        """Send the oldest rows in memory. They stay there until the database has taken
        them, ahead of rows admitted meanwhile and counted with them. Those may move what
        memory holds to the file, or drop some of it: the file sends again what it took of
        these rows, so whatever is left of them stays to be sent again after it, in order."""
        rows = self._memory[:room]
        await self._send(rows)
        front = self._memory[: len(rows)]
        # The very rows, not their like: one admitted meanwhile may equal one that was sent.
        if len(front) == len(rows) and all(map(operator.is_, front, rows)):
            del self._memory[: len(rows)]

    async def _send(self, rows: Sequence[Row]) -> int:
        """Write rows now, raising StorageError only when the database cannot be reached."""
        attempt = await self._ask_database(rows)
        self._storage = attempt.storage
        for row, error in attempt.refused:
            if row not in self._refused:
                self._refused.add(row)
                self.rejected += 1
                _LOGGER.error("Dropping a row the database rejected (%s): %r", error, row)
        if attempt.outage is not None:
            raise attempt.outage
        if self._away:
            _LOGGER.info("Database reachable again; writing what was buffered")
            self._away = False
        self.written += attempt.written
        self._refused.difference_update(rows)  # done with: these will not be tried again
        return attempt.written

    async def _ask_database(self, rows: Sequence[Row]) -> _Attempt:
        """Have the database take rows, the caller waiting for as long as that takes."""
        return _make_attempt(self._storage, self._open, rows)

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
            others: list[Row] = [row for row in self._memory if not isinstance(row, Gap)]
            others = others[: max(self._memory_rows - len(gaps), 0)]
            self._memory = [*others, *gaps][: self._memory_rows]

    async def _replay_spill(self, limit: int) -> int:
        """Send up to `limit` lines of the buffer file, from where the last call stopped,
        and remove the file once all of it is sent; return how many lines were taken.

        A file that cannot be read still owes its rows and is tried again after the retry
        interval; one that cannot be removed (a filesystem gone read-only) is remembered
        as sent up to where it ends.
        """
        sent = 0  # lines of this call which the database has taken
        try:
            with self._spill.open("rb") as spill:
                self._forget_position_in_another_file(spill)
                if self._uncounted:
                    self._spilled, self._uncounted = sum(1 for _ in spill), False
                start = spill.seek(self._replayed)
                lines = list(islice(spill, limit))
                self._unreadable_until = None
                # A line cut short by a crash is skipped rather than blocking the rest.
                await self._send([row for row in map(_decode, lines) if row is not None])
                sent = len(lines)
                # Still open, so that no file begun meanwhile can pass for this one.
                more = self._more_in_file_after(spill, start, b"".join(lines))
            self._spilled = max(self._spilled - sent, 0)
            if more:
                self._spilled = max(self._spilled, 1)  # more for a later call
                return sent
        except (FileNotFoundError, NotADirectoryError):
            # A file never read was never counted: its loss is said, not given a number.
            # One removed while the last of it was being sent took nothing with it.
            unsent = max(self._spilled - sent, 0)
            if lost := "an unknown number of" if self._uncounted else unsent:
                _LOGGER.error(
                    "Buffer file %s is gone, and %s unsent rows with it", self._spill, lost
                )
            if not self._uncounted:
                self.dropped += unsent
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
        return sent

    def _more_in_file_after(self, read: BinaryIO, start: int, sent: bytes) -> bool:
        """Note that the lines `read` from `start` are sent; return whether the file holds
        more.

        The file is looked at afresh: while they were being sent, rows may have been added
        to it, or it may have been emptied or replaced. Only lines still where they were
        read, in the file they were read from, move the position past them; otherwise all
        the file holds now is unsent. A file that is gone, or cannot be read, is for the
        caller to account for: nothing is noted of it.
        """
        with self._spill.open("rb") as spill:
            spill.seek(start)
            if _identity(spill) != _identity(read) or spill.read(len(sent)) != sent:
                self._replayed = 0
            elif sent:
                self._replayed, self._replayed_file = spill.tell(), _identity(spill)
            return _size(spill) > self._replayed

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


class BackgroundStorage(BufferedStorage):
    """The buffer for a writer with an event loop to keep free: rows are written by a
    thread, one attempt at a time, and nobody else waits on the database for them. Opening
    it and reading the latest Jobs still wait where they are called, before collection.

    Writing a row only admits it to the buffer, bounded as ever by memory and then the file.
    `drain` writes what waits, and gives up on an attempt the database has not answered
    within `deadline` seconds: a backend suspended under an open connection is cut short by
    neither a statement timeout nor TCP.

    An attempt given up on may still land, after rows admitted since. Rows carry their own
    time, and each telling of a Job is later than the one before, so that is harmless.
    """

    def __init__(
        self,
        opener: Callable[[], Storage],
        spill: Path,
        *,
        deadline: float = WRITE_SECONDS,
        memory_rows: int = MEMORY_ROWS,
        disk_bytes: int = DISK_BYTES,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        super().__init__(opener, spill, memory_rows=memory_rows, disk_bytes=disk_bytes, clock=clock)
        self._deadline = deadline
        self._unanswered: list[threading.Thread] = []  # attempts given up on, still waiting

    async def drain(self) -> None:
        """Write what is waiting, a slice at a time, until none is or the database is away.
        One drain at a time, which is the caller's to see to."""
        while self.draining:
            await self._flush()

    def flush(self) -> None:
        raise RuntimeError("the database is waited on by a thread; use drain()")

    def close(self) -> None:
        """Leave what is waiting on disk for the next start, without a word to the database:
        a stop must not wait on one that has stopped answering."""
        self._save()

    def _write(self, rows: Sequence[Row]) -> int:
        self._admit(rows)
        return 0

    async def _ask_database(self, rows: Sequence[Row]) -> _Attempt:
        """Have the database take rows on a thread of its own, waiting no longer than the
        deadline for its answer."""
        storage, done = self._storage, Future[_Attempt]()
        self._unanswered = [thread for thread in self._unanswered if thread.is_alive()]
        if storage is None and len(self._unanswered) >= UNANSWERED_ATTEMPTS:
            return _Attempt(None, outage=StorageError("earlier attempts are still unanswered"))

        def work() -> None:
            try:
                attempt = _make_attempt(storage, self._open, rows)
            except BaseException as error:  # raised where the answer is awaited
                with suppress(InvalidStateError):
                    done.set_exception(error)
                return
            try:
                done.set_result(attempt)
            except InvalidStateError:  # given up on: nobody is left to take the connection
                if attempt.storage is not None:
                    attempt.storage.close()

        thread = threading.Thread(target=work, name="database", daemon=True)
        thread.start()
        answer = asyncio.wrap_future(done)
        try:
            await asyncio.wait([answer], timeout=self._deadline)
        finally:
            if done.cancel():
                # Given up on, by the deadline or by a stop. The thread cannot be woken: it
                # is left with the connection, to close it should the database ever let go.
                self._storage = None
                self._unanswered.append(thread)
            elif done.exception() is None:
                # Answered, even if a stop means nobody waits to hear it: the connection
                # the attempt left open is the buffer's again, to use or to close.
                self._storage = done.result().storage
        if done.cancelled():
            return _Attempt(
                None, outage=StorageError(f"no answer within {self._deadline:g} seconds")
            )
        return await answer


def _version(job: Job) -> tuple[datetime, datetime]:
    """Orders a mower's Jobs, and the versions of one Job, oldest first."""
    return job.start_time, job.updated_time


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
        for key, value in values.items():
            if key.endswith("_time") and value is not None:
                values[key] = datetime.fromisoformat(value)
        if kind is Gap:
            values["reason"] = GapReason(values["reason"])
        if kind is Job and values.get("zones") is not None:
            values["zones"] = tuple(values["zones"])
        return kind(**values)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
