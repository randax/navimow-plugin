"""Remove the rows an owner no longer keeps, without live collection waiting for it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic

from .base import Storage, StorageError

# How long between removals: a row outlives what the owner keeps by about a day.
INTERVAL_SECONDS = 24 * 60 * 60
# How many rows one statement removes, so that none is long.
BATCH_ROWS = 5000
_LOGGER = logging.getLogger(__name__)


class Retention:
    """Removes rows older than the owner keeps, about once a day.

    Each removal runs on a thread and a connection of its own, a batch at a time: neither
    the collector's loop nor the rows being written wait for it. One that fails, with the
    database away, is logged and left to the next day's. So is one the database has not
    answered by then: no second connection is opened to a database that keeps the first.

    What is old goes by the date; when a removal is due goes by `clock`, the time elapsed,
    which a clock being set does not move.
    """

    def __init__(
        self, opener: Callable[[], Storage], days: int, *, clock: Callable[[], float] = monotonic
    ) -> None:
        self._open = opener
        self._days = days
        self._clock = clock
        self._due = float("-inf")
        self._removing: threading.Thread | None = None

    def remove_if_due(self, now: float) -> None:
        """Start removing what is too old as of `now`, the date in seconds, unless that was
        done within the day."""
        if self._clock() < self._due:
            return
        self._due = self._clock() + INTERVAL_SECONDS
        if self._removing is not None and self._removing.is_alive():
            _LOGGER.warning(
                "Old rows are not being removed: the last removal is still waiting for the database"
            )
            return
        try:
            before = datetime.fromtimestamp(now, tz=UTC) - timedelta(days=self._days)
        except OverflowError:  # kept for longer than dates go back: nothing is that old
            return
        self._removing = threading.Thread(
            target=self._remove, args=(before,), name="retention", daemon=True
        )
        self._removing.start()

    def join(self, timeout: float | None = None) -> None:
        """Wait for a removal under way to end."""
        if self._removing is not None:
            self._removing.join(timeout)

    def _remove(self, before: datetime) -> None:
        removed = 0
        try:
            with self._open() as storage:
                removed = storage.remove_older_than(before, BATCH_ROWS)
        except StorageError as error:
            _LOGGER.warning("Could not remove rows older than %s: %s", before, error)
        except Exception:  # nothing of this thread's may go unsaid, or end in a bare traceback
            _LOGGER.exception("Could not remove rows older than %s", before)
        if removed:
            _LOGGER.info("Removed %d rows older than %s", removed, before)
