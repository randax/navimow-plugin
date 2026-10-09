"""Real databases for the suites.

CI provides a PostgreSQL through NAVIMOW_TEST_POSTGRES_DSN (a DSN to a server whose
user may create databases). Locally, when that is unset, a throwaway cluster is
started from whatever `initdb`/`pg_ctl` can be found, and the suite is skipped
only if none can. The other backends of the conformance suite are each named by
a variable of their own, and skipped where it is unset.
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
from typing import NoReturn

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


@pytest.fixture
def config_file(tmp_path: Path, database: str) -> Path:
    path = tmp_path / "collector.toml"
    path.write_text(f'[storage]\nbackend = "postgres"\ndsn = "{database}"\n')
    return path


BACKENDS = ("postgres", "timescaledb")


def pytest_configure(config: pytest.Config) -> None:
    for backend in BACKENDS:
        config.addinivalue_line("markers", f"{backend}: a conformance test that needs {backend}")


def pytest_collection_modifyitems(items: list[pytest.Function]) -> None:
    """Mark each conformance test with the backend it needs, by the fixtures it asks for.
    CI gives each backend a job of its own, which picks its tests by this and not by what
    they happen to be called."""
    for item in items:
        asked = getattr(getattr(item, "callspec", None), "params", {}).get("backend")
        needed = "timescaledb" if "timescale" in item.fixturenames else asked
        if needed in BACKENDS:
            item.add_marker(needed)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may pick up the developer's own NAVIMOW_* settings."""
    for key in list(os.environ):
        if key.startswith("NAVIMOW_") and not key.startswith("NAVIMOW_TEST_"):
            monkeypatch.delenv(key)
