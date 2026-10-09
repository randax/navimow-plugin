"""Configuration as an operator sees it: one TOML file, env overrides, secret files."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from navimow_collector.cli import main
from navimow_collector.config import load_config

DSN = "postgresql://mower:hunter2@db.example/navimow"


def show(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    assert main([*argv, "config"]) == 0
    return capsys.readouterr().out


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "collector.toml"
    path.write_text(text)
    return path


def test_prints_the_resolved_configuration_with_secrets_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write(tmp_path, f'[storage]\nbackend = "postgres"\ndsn = "{DSN}"\nmigrate = false\n')
    out = show(capsys, "--config", str(config))
    assert "hunter2" not in out
    assert "db.example" not in out
    assert "postgres" in out
    assert "false" in out.lower()


def test_the_config_path_can_come_from_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write(tmp_path, "[storage]\nmigrate = false\n")
    monkeypatch.setenv("NAVIMOW_CONFIG", str(config))
    assert "false" in show(capsys).lower()


def test_environment_overrides_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write(tmp_path, "[storage]\nmigrate = true\n")
    monkeypatch.setenv("NAVIMOW_STORAGE_MIGRATE", "false")
    assert "false" in show(capsys, "--config", str(config)).lower()


def test_a_secret_can_be_read_from_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "dsn"
    secret.write_text(DSN + "\n")
    config = write(tmp_path, f'[storage]\ndsn_file = "{secret}"\n')
    out = show(capsys, "--config", str(config))
    assert "hunter2" not in out
    assert str(secret) in out  # the path is not secret, and shows where it came from


def test_a_secret_file_can_be_named_by_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "dsn"
    secret.write_text(DSN)
    monkeypatch.setenv("NAVIMOW_STORAGE_DSN_FILE", str(secret))
    out = show(capsys, "--config", str(write(tmp_path, "")))
    assert "hunter2" not in out
    assert str(secret) in out


def test_an_invalid_value_is_rejected_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NAVIMOW_STORAGE_MIGRATE", "sometimes")
    assert main(["--config", str(write(tmp_path, "")), "config"]) != 0
    assert "migrate" in capsys.readouterr().err.lower()


def test_an_unknown_backend_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write(tmp_path, '[storage]\nbackend = "mongodb"\n')
    assert main(["--config", str(config), "config"]) != 0
    assert "mongodb" in capsys.readouterr().err


def test_the_console_command_is_installed() -> None:
    exe = shutil.which("navimow-collector")
    assert exe, "navimow-collector is not on PATH; install the package"
    result = subprocess.run([exe, "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "replay" in result.stdout


def test_the_environment_overrides_a_secret_file_given_in_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "dsn"
    secret.write_text(DSN)
    monkeypatch.setenv("NAVIMOW_STORAGE_DSN", "postgresql://other")
    out = show(capsys, "--config", str(write(tmp_path, f'[storage]\ndsn_file = "{secret}"\n')))
    assert str(secret) not in out  # the inline env value won
    assert "<redacted>" in out


def test_auth_defaults_are_visible_but_its_client_secret_is_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = show(capsys, "--config", str(write(tmp_path, '[auth]\nclient_id = "mine"\n')))
    assert "[auth]" in out
    assert 'client_id = "mine"' in out
    assert "57056e15" not in out
    assert "state_file" in out


def test_the_state_directory_of_live_collection_is_configurable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write(tmp_path, '[collector]\nstate_dir = "/var/lib/navimow-collector"\n')
    assert 'state_dir = "/var/lib/navimow-collector"' in show(capsys, "--config", str(config))
    monkeypatch.setenv("NAVIMOW_COLLECTOR_STATE_DIR", "/srv/mower")
    assert 'state_dir = "/srv/mower"' in show(capsys, "--config", str(config))


def test_health_listens_on_localhost_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert load_config(write(tmp_path, "")).health.address() == ("127.0.0.1", 9477)
    assert 'listen = "127.0.0.1:9477"' in show(capsys, "--config", str(write(tmp_path, "")))


@pytest.mark.parametrize(
    ("listen", "address"),
    [
        ("0.0.0.0:9100", ("0.0.0.0", 9100)),
        ("[::]:9477", ("::", 9477)),
        ("localhost:0", ("localhost", 0)),
        ("", None),  # no health endpoint at all
    ],
)
def test_the_health_address_is_configurable(
    tmp_path: Path, listen: str, address: tuple[str, int] | None
) -> None:
    config = write(tmp_path, f'[health]\nlisten = "{listen}"\n')
    assert load_config(config).health.address() == address


def test_the_health_address_can_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NAVIMOW_HEALTH_LISTEN", "0.0.0.0:9000")
    assert load_config(write(tmp_path, "")).health.address() == ("0.0.0.0", 9000)


@pytest.mark.parametrize("listen", ["9477", "host:", ":9477", "host:http", "host:70000", "[::"])
def test_an_unusable_health_address_is_rejected_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], listen: str
) -> None:
    config = write(tmp_path, f'[health]\nlisten = "{listen}"\n')
    assert main(["--config", str(config), "config"]) == 2
    assert "health.listen" in capsys.readouterr().err


def test_nothing_is_removed_unless_the_owner_says_how_long_to_keep_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert load_config(write(tmp_path, "")).storage.retention_days is None
    config = write(tmp_path, "[storage]\nretention_days = 365\n")
    assert load_config(config).storage.retention_days == 365
    assert "retention_days = 365\n" in show(capsys, "--config", str(config))
    monkeypatch.setenv("NAVIMOW_STORAGE_RETENTION_DAYS", "30")
    assert load_config(config).storage.retention_days == 30
    monkeypatch.setenv("NAVIMOW_STORAGE_RETENTION_DAYS", "0" * 5000 + "30")
    assert load_config(config).storage.retention_days == 30


@pytest.mark.parametrize("days", ["0", "-7", "1.5", '"a year"', "true", '""'])
def test_a_retention_that_is_not_a_positive_whole_number_of_days_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], days: str
) -> None:
    config = write(tmp_path, f"[storage]\nretention_days = {days}\n")
    assert main(["--config", str(config), "config"]) == 2
    assert "storage.retention_days must be a positive whole number" in capsys.readouterr().err


@pytest.mark.parametrize("days", ["0", "-7", "1.5", "a year", "", "３０", " 30", "9" * 5000])
def test_a_retention_from_the_environment_is_held_to_the_same(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    days: str,
) -> None:
    monkeypatch.setenv("NAVIMOW_STORAGE_RETENTION_DAYS", days)
    assert main(["--config", str(write(tmp_path, "")), "config"]) == 2
    assert "storage.retention_days must be a positive whole number" in capsys.readouterr().err
