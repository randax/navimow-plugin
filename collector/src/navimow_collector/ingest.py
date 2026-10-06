"""Feed capture records into storage without depending on a live transport."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from .jobs import JobTracker
from .records import Job, Row, TrailPoint, parse_record
from .storage import Writer, write_rows

BATCH_SIZE = 500


class Ingestor:
    """Decide what raw capture records mean, and batch the rows for a storage adapter."""

    def __init__(
        self, storage: Writer, batch_size: int = BATCH_SIZE, *, jobs: Iterable[Job] = ()
    ) -> None:
        """`jobs` are the Jobs to carry on from: each mower's latest, as last stored."""
        self._storage = storage
        self._batch_size = batch_size
        self._trackers = {job.mower_id: JobTracker(job.mower_id, job) for job in jobs}
        self._rows: list[Row] = []
        self.points_written = 0
        self.placeholders_discarded = 0

    def feed(self, record: Mapping[str, object]) -> None:
        parsed = parse_record(record)
        self.placeholders_discarded += parsed.placeholders_discarded
        for gap in parsed.gaps:
            self._tracker(gap.mower_id).gap()
            self._rows.append(gap)
        for state in parsed.polled:
            self._rows.extend(self._tracker(state.mower_id).polled(state))
        for state in parsed.states:
            self._rows.extend(self._tracker(state.mower_id).state(state))
        for report in parsed.progress:
            self._rows.extend(self._tracker(report.mower_id).progress(report))
        for point in parsed.points:
            self._rows.extend(self._tracker(point.mower_id).point(point))
        if len(self._rows) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        rows, self._rows = self._rows, []
        self.points_written += write_rows(self._storage, rows).get(TrailPoint, 0)

    def _tracker(self, mower_id: str) -> JobTracker:
        return self._trackers.setdefault(mower_id, JobTracker(mower_id))


def read_capture(path: Path) -> Iterator[dict[str, object]]:
    """Yield JSONL capture records, transparently handling gzip-compressed fixtures."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as capture:
        for line_number, line in enumerate(capture, 1):
            if not line.strip():
                continue
            try:
                value: object = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid JSON on capture line {line_number}: {error.msg}"
                ) from error
            if not isinstance(value, dict):
                raise ValueError(f"capture line {line_number} is not an object")
            yield {str(key): item for key, item in value.items()}
