"""Configuration as an operator sees it: one TOML file, env overrides, secret files."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from navimow_collector.cli import main

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
