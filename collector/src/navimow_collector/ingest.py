"""Feed capture records into storage without depending on a live transport."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

from .records import TrailPoint, parse_record
from .storage import Storage

BATCH_SIZE = 500


class Ingestor:
    """Batch Trail points from raw capture records for a storage adapter."""

    def __init__(self, storage: Storage, batch_size: int = BATCH_SIZE) -> None:
        self._storage = storage
        self._batch_size = batch_size
        self._points: list[TrailPoint] = []
        self.points_written = 0
        self.placeholders_discarded = 0

    def feed(self, record: Mapping[str, object]) -> None:
        parsed = parse_record(record)
        self.placeholders_discarded += parsed.placeholders_discarded
        self._points.extend(parsed.points)
        if len(self._points) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._points:
            return
        self.points_written += self._storage.write_trail(self._points)
        self._points.clear()


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
