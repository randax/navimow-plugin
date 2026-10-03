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
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "synthetic-job.jsonl.gz"


def _pg_bindir() -> Path | None:
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    candidates = sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin")) + sorted(
        glob.glob("/usr/lib/postgresql/*/bin")
    )
    return Path(candidates[-1]) if candidates else None


def _free_port() -> int:
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
        pytest.skip("no PostgreSQL: set NAVIMOW_TEST_POSTGRES_DSN or install initdb/pg_ctl")
    data = tmp_path_factory.mktemp("pgdata")
    port = _free_port()
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
            f"-p {port} -k {data} -c listen_addresses=127.0.0.1",
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


@pytest.fixture
def database(postgres_server: str) -> Iterator[str]:
    """DSN of a fresh, empty database, dropped after the test."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    params = {
        key: str(value)
        for key, value in conninfo_to_dict(postgres_server).items()
        if value is not None
    }
    params["dbname"] = name
    yield make_conninfo(**params)
    with psycopg.connect(postgres_server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def config_file(tmp_path: Path, database: str) -> Path:
    path = tmp_path / "collector.toml"
    path.write_text(f'[storage]\nbackend = "postgres"\ndsn = "{database}"\n')
    return path


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may pick up the developer's own NAVIMOW_* settings."""
    for key in list(os.environ):
        if key.startswith("NAVIMOW_") and key != "NAVIMOW_TEST_POSTGRES_DSN":
            monkeypatch.delenv(key)
