"""A real PostgreSQL for the replay suite.

CI provides one through NAVIMOW_TEST_POSTGRES_DSN (a DSN to a server whose user
may create databases). Locally, when that is unset, a throwaway cluster is
started from whatever `initdb`/`pg_ctl` can be found, and the suite is skipped
only if none can.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "synthetic-job.jsonl.gz"


class Clock:
    """An injected clock, which a test moves by hand."""

    def __init__(self, now: float = 0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


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
    assert bindir
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


def unavailable(backend: str, how: str) -> None:
    """A backend the conformance suite cannot reach is skipped, unless this run is the one
    which is there to test it: CI names that backend in NAVIMOW_TEST_REQUIRE, so that a
    service which did not come up fails the run rather than passing it by."""
    if backend in os.environ.get("NAVIMOW_TEST_REQUIRE", "").split(","):
        pytest.fail(f"no {backend} to test against: {how}")
    pytest.skip(f"no {backend}: {how}")


@pytest.fixture(scope="session")
def timescale_server() -> str:
    """DSN of a TimescaleDB server the tests may create databases on."""
    dsn = os.environ.get("NAVIMOW_TEST_TIMESCALE_DSN")
    if not dsn:
        unavailable("timescaledb", "set NAVIMOW_TEST_TIMESCALE_DSN")
    assert dsn
    return dsn


@pytest.fixture
def config_file(tmp_path: Path, database: str) -> Path:
    path = tmp_path / "collector.toml"
    path.write_text(f'[storage]\nbackend = "postgres"\ndsn = "{database}"\n')
    return path


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may pick up the developer's own NAVIMOW_* settings."""
    for key in list(os.environ):
        if key.startswith("NAVIMOW_") and not key.startswith("NAVIMOW_TEST_"):
            monkeypatch.delenv(key)
