"""InfluxDB storage: the measurement-and-tag shape of docs/adr/0002-data-schema.md.

One adapter for InfluxDB 1, 2 and 3. Each takes line protocol at the older `/write` and
at the newer `/api/v2/write`, and answers InfluxQL at `/query`; the address says which of
the two a row is written through. Nothing else of InfluxDB is used, so nothing is needed
to speak to it but HTTP.

InfluxDB has no rows held to a key, only points: one written again at the same time under
the same tags is written over the first, field by field. So a reading delivered twice is
stored once, with the later receipt; an older telling of a Job is written over a later
one; and a reading stored again under another Job or Zone is a second point. A field, once
written, cannot be unset either, which is why a Job that is not ended says so with an
`end_time` of 0.
"""

from __future__ import annotations

import base64
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from ..config import StorageConfig
from ..records import Gap, Job, Mower, MowerState, Progress, Row, TrailPoint
from .base import RejectedError, StorageError

# As for PostgreSQL: a server that does not answer within seconds is an outage.
TIMEOUT_SECONDS = 5
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MEASUREMENTS: dict[type[Row], str] = {
    TrailPoint: "trail_point",
    Progress: "job_progress",
    MowerState: "mower_state",
    Job: "job",
    Gap: "collector_gap",
    Mower: "mower",
}
# What a point is of, besides its mower: the time it is at, and what else names it.
# InfluxQL groups by tags alone, and progress by Zone is a dashboard panel.
_TIMES = {Job: "start_time", Gap: "start_time", Mower: "updated_time"}
_TAGS: dict[type[Row], tuple[str, ...]] = {
    TrailPoint: ("job_id", "zone"),
    Progress: ("job_id", "zone"),
    MowerState: ("job_id",),
    Job: ("job_id",),
}


def _alternatives(hint: Any) -> tuple[Any, ...]:
    """What a value may be other than unset: of `int | None`, the whole number."""
    kinds = get_args(hint) if isinstance(hint, UnionType) else (hint,)
    return tuple(kind for kind in kinds if kind is not type(None))


# What each value of a row is, whatever it happens to be written as: a field keeps the type
# of its first point, and an area of 120.0 read back as 120 must not be written as a whole
# number the next time.
_KINDS: dict[type[Row], dict[str, type]] = {
    row: {
        name: next(get_origin(kind) or kind for kind in _alternatives(hint))
        for name, hint in get_type_hints(row).items()
    }
    for row in MEASUREMENTS
}
# The answers of InfluxDB which say that it will never take a row, for what the row holds:
# a field of another type than the points before it, a time older than its bucket keeps.
# The first is what lines 1 and 3 answer, the second line 2.
_REFUSED = (400, 422)


def _nanoseconds(time: datetime) -> int:
    """The time a point is at, as line protocol and InfluxQL have it."""
    return (time - _EPOCH) // timedelta(microseconds=1) * 1000


def _milliseconds(time: datetime) -> int:
    """Any other time a point holds, as the field it is: whole milliseconds."""
    return (time - _EPOCH) // timedelta(milliseconds=1)


def _tag(value: object) -> str:
    """A tag's value as line protocol takes it."""
    return str(value).translate({ord(char): f"\\{char}" for char in ",= "})


def _quoted(value: str) -> str:
    """A tag's value as InfluxQL takes a string."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _field(value: Any, kind: type) -> str:
    """A value as line protocol writes a field of its kind: a time as whole milliseconds,
    the Zones of a Job as text such as `1,6,7`."""
    if kind is datetime:
        return f"{_milliseconds(value)}i"
    if kind is bool:
        return "true" if value else "false"
    if kind is int:
        return f"{int(value)}i"
    if kind is float:
        return repr(float(value))
    text = ",".join(map(str, value)) if kind is tuple else str(value)
    return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))


def _line(row: Row) -> str | None:
    """One row as a point: its measurement, its tags, its fields and its time. None for a
    row with nothing to say, which a point cannot be: a mower nothing is known of."""
    kind = type(row)
    at, tags = _TIMES.get(kind, "device_time"), ("mower_id", *_TAGS.get(kind, ()))
    values = {field.name: getattr(row, field.name) for field in fields(row)}
    if isinstance(row, Job) and row.end_time is None:
        values["end_time"] = _EPOCH  # not ended: a field once written cannot be unset
    names = ",".join(f"{tag}={_tag(values[tag])}" for tag in tags if values[tag] is not None)
    says = ",".join(
        f"{name}={_field(value, _KINDS[kind][name])}"
        for name, value in values.items()
        if name not in (at, *tags) and value is not None
    )
    return f"{MEASUREMENTS[kind]},{names} {says} {_nanoseconds(values[at])}" if says else None


def _job(point: Mapping[str, Any]) -> Job:
    """The Job a `job` point holds, each value as the kind it is: a time from its
    milliseconds, an `end_time` of 0 as none, the Zones from their text."""
    values: dict[str, Any] = {"start_time": _EPOCH + timedelta(microseconds=point["time"] // 1000)}
    for name, kind in _KINDS[Job].items():
        value = point.get(name)
        if name == "start_time" or value is None:
            continue
        if kind is datetime:
            value = _EPOCH + timedelta(milliseconds=value) if value else None
        elif kind is tuple:
            value = tuple(map(int, value.split(","))) if value else None
        else:
            value = kind(value)
        values[name] = value
    return Job(**values)


class InfluxStorage:
    """Persist rows as points in InfluxDB 1, 2 or 3."""

    def __init__(self, config: StorageConfig) -> None:
        if config.dsn is None:
            raise StorageError("storage.dsn is required for the influxdb backend")
        address = urlsplit(config.dsn.reveal())
        said = {key: values[-1] for key, values in parse_qs(address.query).items()}
        self._database = unquote(address.path.strip("/"))
        if address.scheme not in ("http", "https") or not address.hostname or not self._database:
            raise StorageError(
                "storage.dsn for influxdb must be http(s)://host:port/database, with"
                " user:password@ before the host, or ?token=... and &org=... after the bucket"
            )
        host = address.hostname if ":" not in address.hostname else f"[{address.hostname}]"
        self._server = f"{address.scheme}://{host}{f':{address.port}' if address.port else ''}"
        self._headers = {}
        if "token" in said:
            self._headers["Authorization"] = f"Token {said['token']}"
        elif address.username:
            login = f"{unquote(address.username)}:{unquote(address.password or '')}".encode()
            self._headers["Authorization"] = f"Basic {base64.b64encode(login).decode()}"
        # The newer way in is the one that knows of an organisation and of tokens.
        if "token" in said or "org" in said:
            where = {"org": said.get("org", ""), "bucket": self._database, "precision": "ns"}
            self._write = f"/api/v2/write?{urlencode(where)}"
        else:
            self._write = f"/write?{urlencode({'db': self._database, 'precision': 'ns'})}"

    def __enter__(self) -> InfluxStorage:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def migrate(self) -> None:
        """Nothing to make: InfluxDB has no schema, and the database is the owner's to
        make. Only that it is there, and takes what the collector is to write."""
        self.check_schema()

    def check_schema(self) -> None:
        """That the database answers a question, and that the way in takes a write: a
        write with nothing in it, which stores nothing. A wrong address must fail here,
        where it is an outage, and not later as a row refused and dropped."""
        self._ask("SELECT * FROM mower LIMIT 1")
        try:
            self._request(self._write, b"")
        except RejectedError as error:
            if "empty" not in str(error):  # line 3 says so of a write with nothing in it
                raise StorageError(str(error)) from error

    def keep_for(self, days: int | None) -> None:
        """Nothing: InfluxDB keeps points for as long as their bucket or retention policy
        says, which is the owner's to set there. A number of days set here is refused when
        the configuration is read."""

    def remove_older_than(self, before: datetime, batch: int) -> int:
        return 0

    def latest_jobs(self) -> Sequence[Job]:
        found = self._ask("SELECT * FROM job GROUP BY mower_id ORDER BY time DESC LIMIT 1")
        return [_job(point) for point in found]

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        return self._send(points)

    def write_progress(self, reports: Sequence[Progress]) -> int:
        return self._send(reports)

    def write_states(self, states: Sequence[MowerState]) -> int:
        return self._send(states)

    def write_jobs(self, jobs: Sequence[Job]) -> int:
        return self._send(jobs)

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        """Store each gap that is new, or ends later than the same gap as it is stored."""
        written = 0
        for gap in gaps:
            stored = self._ask(
                "SELECT end_time FROM collector_gap WHERE mower_id = "
                f"{_quoted(gap.mower_id)} AND time = {_nanoseconds(gap.start_time)}"
            )
            if not stored or stored[0]["end_time"] < _milliseconds(gap.end_time):
                written += self._send([gap])
        return written

    def write_mowers(self, mowers: Sequence[Mower]) -> int:
        """Store each mower that is new, or described differently and later than it is: a
        point of its own, at the time it was so described."""
        written = 0
        for mower in mowers:
            stored = self._ask(
                f"SELECT * FROM mower WHERE mower_id = {_quoted(mower.mower_id)}"
                " ORDER BY time DESC LIMIT 1"
            )
            was = stored and tuple(stored[0].get(name) for name in ("name", "model", "firmware"))
            later = stored and stored[0]["time"] < _nanoseconds(mower.updated_time)
            if not stored or (later and was != (mower.name, mower.model, mower.firmware)):
                written += self._send([mower])
        return written

    def _send(self, rows: Sequence[Row]) -> int:
        """Write rows as points; return how many, though some may be points already there."""
        try:
            lines = [line for line in map(_line, rows) if line is not None]
        except (TypeError, ValueError) as error:  # a value that is not of its field's kind
            raise RejectedError(f"influxdb: {error}") from error
        if lines:
            self._request(self._write, "\n".join(lines).encode())
        return len(lines)

    def _ask(self, question: str) -> list[dict[str, Any]]:
        """Every point InfluxQL answers with, as its columns and tags by name."""
        asked = urlencode({"db": self._database, "epoch": "ns", "q": question}, quote_via=quote)
        answer = self._request(f"/query?{asked}")
        points = []
        try:
            for result in json.loads(answer)["results"]:
                if "error" in result:
                    raise StorageError(f"influxdb: {result['error']}")
                for series in result.get("series", []):
                    tags = series.get("tags", {})
                    for row in series["values"]:
                        points.append({**tags, **dict(zip(series["columns"], row, strict=True))})
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise StorageError(f"influxdb: not an answer to InfluxQL: {answer[:200]!r}") from error
        return points

    def _request(self, path: str, body: bytes | None = None) -> bytes:
        request = urllib.request.Request(self._server + path, data=body, headers=self._headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as answer:
                content: bytes = answer.read()
                return content
        except urllib.error.HTTPError as error:
            said = error.read().decode(errors="replace").strip()
            refused = RejectedError if error.code in _REFUSED else StorageError
            raise refused(f"influxdb: {error.code} {said}") from error
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise StorageError(f"influxdb: {error}") from error

    def close(self) -> None:
        """Nothing is held open between one request and the next."""
