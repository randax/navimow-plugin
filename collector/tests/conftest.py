"""Real databases for the suites.

CI provides a PostgreSQL through NAVIMOW_TEST_POSTGRES_DSN (a DSN to a server whose
user may create databases). Locally, when that is unset, a throwaway cluster is
started from whatever `initdb`/`pg_ctl` can be found, and the suite is skipped
only if none can. The other backends of the conformance suite are each named by
a variable of their own, and skipped where it is unset.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import socket
import subprocess
import urllib.request
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qs, urlencode, urlsplit

import clickhouse_connect
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from navimow_collector.ingest import Ingestor
from navimow_collector.records import Gap, Job, Mower, MowerState, Progress, TrailPoint

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "synthetic-job.jsonl.gz"


class Clock:
    """An injected clock, which a test moves by hand."""

    def __init__(self, now: float = 0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Told:
    """A writer that keeps the Jobs the ingestion core hands it, in the order handed."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []

    def write_jobs(self, jobs: Sequence[Job]) -> int:
        self.jobs.extend(jobs)
        return len(jobs)

    def write_trail(self, points: Sequence[TrailPoint]) -> int:
        return len(points)

    def write_gaps(self, gaps: Sequence[Gap]) -> int:
        return len(gaps)

    def write_progress(self, reports: Sequence[Progress]) -> int:
        return len(reports)

    def write_states(self, states: Sequence[MowerState]) -> int:
        return len(states)

    def write_mowers(self, mowers: Sequence[Mower]) -> int:
        return len(mowers)


def told_at_once(*areas: float) -> list[Job]:
    """Every telling of a Job, oldest first, as the ingestion core tells them of a mower
    that leaves the dock and then reports so many square metres mowed, all in the same
    millisecond."""
    sent, topic = 1_790_769_600_000, "/downlink/vehicle/DEVICE_1/realtimeDate/"
    told = Told()
    ingestor = Ingestor(told)
    state = {"state": "isRunning"}
    ingestor.feed({"recv_ms": sent, "kind": "mqtt", "topic": f"{topic}state", "payload": state})
    for area in areas:
        report = {"type": 2, "time": sent, "mowStartType": 1, "subtotalArea": area}
        location = {"recv_ms": sent, "kind": "mqtt", "topic": f"{topic}location"}
        ingestor.feed({**location, "payload": [report]})
    ingestor.flush()
    return told.jobs


def gaps(dsn: str) -> list[tuple[str, datetime, datetime, str]]:
    """Every stored gap, in the order they began."""
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT mower_id, start_time, end_time, reason FROM collector_gap"
            " ORDER BY start_time, mower_id"
        ).fetchall()


def _pg_bindir() -> Path | None:
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    candidates = sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin")) + sorted(
        glob.glob("/usr/lib/postgresql/*/bin")
    )
    return Path(candidates[-1]) if candidates else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def unavailable(backend: str, how: str) -> NoReturn:
    """A backend the conformance suite was not given is skipped, unless this run is the one
    which is there to test it: CI names that backend in NAVIMOW_TEST_REQUIRE, so that a
    job set up without it fails rather than passing it by."""
    if backend in os.environ.get("NAVIMOW_TEST_REQUIRE", "").split(","):
        pytest.fail(f"no {backend} to test against: {how}")
    pytest.skip(f"no {backend}: {how}")


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """DSN of a server the tests may create databases on."""
    dsn = os.environ.get("NAVIMOW_TEST_POSTGRES_DSN")
    if dsn:
        yield dsn
        return

    bindir = _pg_bindir()
    if bindir is None:
        unavailable("postgres", "set NAVIMOW_TEST_POSTGRES_DSN or install initdb/pg_ctl")
    data = tmp_path_factory.mktemp("pgdata")
    port = free_port()
    subprocess.run(
        [bindir / "initdb", "-D", data, "-U", "postgres", "-A", "trust"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            bindir / "pg_ctl",
            "-D",
            data,
            "-w",
            "-l",
            data / "log",
            "-o",
            # Reached over TCP only: a socket in the data directory has a path too long
            # to bind once pytest's temporary directories are numbered in the hundreds.
            f"-p {port} -c unix_socket_directories= -c listen_addresses=127.0.0.1",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(
            [bindir / "pg_ctl", "-D", data, "-m", "immediate", "stop"], capture_output=True
        )


@contextmanager
def fresh_database(server: str) -> Iterator[str]:
    """DSN of a new, empty database on a server, dropped afterwards."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    params = {
        key: str(value) for key, value in conninfo_to_dict(server).items() if value is not None
    }
    params["dbname"] = name
    try:
        yield make_conninfo(**params)
    finally:
        with psycopg.connect(server, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def database(postgres_server: str) -> Iterator[str]:
    """DSN of a fresh, empty database, dropped after the test."""
    with fresh_database(postgres_server) as dsn:
        yield dsn


@pytest.fixture(scope="session")
def timescale_server() -> str:
    """DSN of a TimescaleDB server the tests may create databases on."""
    dsn = os.environ.get("NAVIMOW_TEST_TIMESCALE_DSN")
    if not dsn:
        unavailable("timescaledb", "set NAVIMOW_TEST_TIMESCALE_DSN")
    return dsn


@pytest.fixture(scope="session")
def clickhouse_server() -> str:
    """URL of a ClickHouse server the tests may create databases on, with no database in it:
    `http://user:password@host:8123`."""
    url = os.environ.get("NAVIMOW_TEST_CLICKHOUSE_URL")
    if not url:
        unavailable("clickhouse", "set NAVIMOW_TEST_CLICKHOUSE_URL")
    return url.rstrip("/")


@contextmanager
def fresh_clickhouse(server: str) -> Iterator[str]:
    """URL of a new, empty database on a ClickHouse server, dropped afterwards."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    client = clickhouse_connect.get_client(dsn=server)
    client.command(f"CREATE DATABASE {name}")
    try:
        yield f"{server}/{name}"
    finally:
        client.command(f"DROP DATABASE {name}")
        client.close()


def influxdb_server(line: int) -> str:
    """Address of an InfluxDB server of one major line, with the login of one who may make
    databases on it: `http://user:password@host:8086` for the older way in, or
    `http://host:8086?org=...&token=...` for the newer."""
    url = os.environ.get(f"NAVIMOW_TEST_INFLUXDB{line}_URL")
    if not url:
        unavailable(f"influxdb{line}", f"set NAVIMOW_TEST_INFLUXDB{line}_URL")
    return url


def influxdb_ways(server: str) -> dict[str, str]:
    """The same InfluxDB server by each of its two ways in, with the login the tests were
    given put as that way takes it: `older` for `/write`, `newer` for `/api/v2/write`."""
    address = urlsplit(server)
    said = {key: values[-1] for key, values in parse_qs(address.query).items()}
    host = f"{address.hostname}:{address.port}"
    user, secret = address.username or "navimow", said.get("token") or address.password
    older = f"{user}:{secret}@{host}" if secret else host
    token = said.get("token") or (f"{user}:{secret}" if secret else None)
    newer = {"org": said.get("org", "home"), **({"token": token} if token else {})}
    return {
        "older": f"{address.scheme}://{older}",
        "newer": f"{address.scheme}://{host}?{urlencode(newer)}",
    }


def influxdb_asked(server: str, method: str, path: str, body: object = None) -> Any:
    """What an InfluxDB server answers one who administers it. A text is sent as it is,
    anything else as JSON."""
    address = urlsplit(server)
    named = {key: values[-1] for key, values in parse_qs(address.query).items()}
    headers = {"Content-Type": "application/json"}
    if "token" in named:
        headers["Authorization"] = f"Token {named['token']}"
    elif address.username:
        login = f"{address.username}:{address.password or ''}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(login).decode()}"
    sent = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{address.scheme}://{address.hostname}:{address.port}{path}",
        data=None if body is None else sent,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=10) as answer:
        said = answer.read()
    return json.loads(said) if said.strip() else None


@contextmanager
def fresh_influxdb(line: int, server: str) -> Iterator[str]:
    """Address of a new, empty database on an InfluxDB server, dropped afterwards. Each line
    has its own way of making one."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    made = urlencode({"q": f'CREATE DATABASE "{name}"'})
    dropped = urlencode({"q": f'DROP DATABASE "{name}"'})
    if line == 1:
        influxdb_asked(server, "POST", f"/query?{made}")
        drop = ("POST", f"/query?{dropped}")
    elif line == 2:
        org = parse_qs(urlsplit(server).query)["org"][-1]
        [found] = influxdb_asked(server, "GET", f"/api/v2/orgs?{urlencode({'org': org})}")["orgs"]
        bucket = influxdb_asked(
            server, "POST", "/api/v2/buckets", {"orgID": found["id"], "name": name}
        )
        drop = ("DELETE", f"/api/v2/buckets/{bucket['id']}")
    else:
        influxdb_asked(server, "POST", "/api/v3/configure/database", {"db": name})
        drop = ("DELETE", f"/api/v3/configure/database?db={name}")
    try:
        yield urlsplit(server)._replace(path=f"/{name}").geturl()
    finally:
        influxdb_asked(server, *drop)


@pytest.fixture
def config_file(tmp_path: Path, database: str) -> Path:
    path = tmp_path / "collector.toml"
    path.write_text(f'[storage]\nbackend = "postgres"\ndsn = "{database}"\n')
    return path


BACKENDS = ("postgres", "timescaledb", "clickhouse", "influxdb1", "influxdb2", "influxdb3")
# The fixtures that are one backend each, and those that are each of several in turn.
ONE_BACKEND = {"timescale": "timescaledb", "clickhouse": "clickhouse"}
SEVERAL_BACKENDS = ("backend", "postgresql", "relational", "influx")


def pytest_configure(config: pytest.Config) -> None:
    for backend in BACKENDS:
        config.addinivalue_line("markers", f"{backend}: a conformance test that needs {backend}")


def pytest_collection_modifyitems(items: list[pytest.Function]) -> None:
    """Mark each conformance test with the backend it needs, by the fixtures it asks for.
    CI gives each backend a job of its own, which picks its tests by this and not by what
    they happen to be called."""
    for item in items:
        given = getattr(getattr(item, "callspec", None), "params", {})
        # A line of InfluxDB is one backend, by whichever of its ways in: `influxdb2-newer`.
        needed = [
            given[fixture].partition("-")[0] for fixture in SEVERAL_BACKENDS if fixture in given
        ]
        needed += [
            backend for fixture, backend in ONE_BACKEND.items() if fixture in item.fixturenames
        ]
        for backend in needed:
            item.add_marker(backend)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may pick up the developer's own NAVIMOW_* settings."""
    for key in list(os.environ):
        if key.startswith("NAVIMOW_") and not key.startswith("NAVIMOW_TEST_"):
            monkeypatch.delenv(key)
