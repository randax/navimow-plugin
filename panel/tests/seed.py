"""Fill the test database as a collector would, for the bundled dashboards to read.

The real capture (fixtures/job-2026-09-30.jsonl.gz) is replayed twice, moved in time so that a
dashboard opened on its default range has something to show whenever the tests run: once ending
an hour ago, and once a week before that. The earlier one has an error and a gap in collection
put into it. Both are invented, since no capture holds either, and are only there so that the
panels which show them have something to show.

Whatever the database held is dropped first, so filling it again on a later day leaves the same
two Jobs, placed by that day.
"""

from __future__ import annotations

import gzip
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import psycopg
from navimow_collector.cli import main

CAPTURE = Path(__file__).resolve().parents[2] / "fixtures" / "job-2026-09-30.jsonl.gz"
DSN = "postgresql://postgres@postgres/navimow"
HOUR_MS = 3_600_000
WEEK_MS = 7 * 24 * HOUR_MS
STATE_TOPIC = "/downlink/vehicle/DEVICE_1/realtimeDate/state"
MINUTE_MS = 60_000
LEFT_DOCK_MS = 1790775213612  # when the capture's Job began
# What is put into the earlier Job: four minutes lost from twenty minutes in, and two minutes
# in error from forty-five minutes in.
GAP_MS = (LEFT_DOCK_MS + 20 * MINUTE_MS, LEFT_DOCK_MS + 24 * MINUTE_MS)
ERROR_MS = (LEFT_DOCK_MS + 45 * MINUTE_MS, LEFT_DOCK_MS + 47 * MINUTE_MS)

Record = dict[str, Any]


def moved(record: Record, by_ms: int) -> Record:
    """The record as it would have been captured `by_ms` later."""
    record = json.loads(json.dumps(record))
    for key in ("recv_ms", "start_ms"):
        if key in record:
            record[key] += by_ms
    payload = record.get("payload")
    for item in payload if isinstance(payload, list) else [payload]:
        for key in ("time", "timestamp"):
            if isinstance(item, dict) and key in item:
                item[key] += by_ms
    return record


def state(at_ms: int, name: str) -> Record:
    payload = {"battery": 70, "device_id": "DEVICE_1", "state": name, "timestamp": at_ms}
    return {"recv_ms": at_ms, "kind": "mqtt", "topic": STATE_TOPIC, "payload": payload}


def troubled(capture: list[Record]) -> list[Record]:
    """The capture with four minutes of it lost to a reconnection, and two minutes in error."""
    lost_from, lost_to = GAP_MS
    kept = [r for r in capture if r["kind"] != "mqtt" or not lost_from < r["recv_ms"] < lost_to]
    gap = {"kind": "gap", "mower_id": "DEVICE_1", "start_ms": lost_from, "reason": "reconnect"}
    invented = [
        {"recv_ms": lost_to, **gap},
        state(ERROR_MS[0], "Error"),
        state(ERROR_MS[1], "isRunning"),
    ]
    return sorted([*kept, *invented], key=lambda record: record["recv_ms"])


def replay(records: list[Record], directory: Path) -> None:
    config, capture = directory / "collector.toml", directory / "capture.jsonl"
    config.write_text(f'[storage]\nbackend = "postgres"\ndsn = "{DSN}"\n')
    capture.write_text("".join(json.dumps(record) + "\n" for record in records))
    if main(["--config", str(config), "replay", str(capture)]) != 0:
        raise SystemExit("replay failed")


def seed() -> None:
    with gzip.open(CAPTURE, "rt", encoding="utf-8") as lines:
        capture = [json.loads(line) for line in lines]
    to_an_hour_ago = round(time.time() * 1000) - HOUR_MS - capture[-1]["recv_ms"]
    with psycopg.connect(DSN, autocommit=True) as database:
        database.execute("DROP SCHEMA public CASCADE")
        database.execute("CREATE SCHEMA public")
    with tempfile.TemporaryDirectory() as directory:
        replay([moved(r, to_an_hour_ago - WEEK_MS) for r in troubled(capture)], Path(directory))
        replay([moved(r, to_an_hour_ago) for r in capture], Path(directory))


if __name__ == "__main__":
    seed()
