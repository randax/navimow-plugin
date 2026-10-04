"""Structured logs: one JSON object per line, for whatever collects the service's output."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

# Every attribute a plain LogRecord has; anything else on a record came from `extra`.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """`time`, `level`, `logger` and `message`, plus the record's `extra` fields.

    Values are rendered with `str` when JSON has no type for them, which keeps a `Secret`
    redacted; a message is logged as composed, so its own redaction is preserved too.
    """

    def format(self, record: logging.LogRecord) -> str:
        time = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        line: dict[str, object] = {
            "time": time.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        line.update((key, value) for key, value in vars(record).items() if key not in _STANDARD)
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)
