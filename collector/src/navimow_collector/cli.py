"""Command-line operations for replaying Navimow captures."""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields
from pathlib import Path

from .config import Config, ConfigError, Secret, load_config
from .ingest import Ingestor, read_capture
from .storage import StorageError, open_storage


def main(argv: list[str] | None = None) -> int:
    """Run the collector command and return a shell-compatible status code."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        config = load_config(args.config)
        if args.command == "config":
            print(_format_config(config))
            return 0
        return _replay(config, args.capture)
    except (ConfigError, StorageError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay Navimow captures into storage.")
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config", help="print resolved configuration")
    replay = commands.add_parser("replay", help="replay one JSONL capture")
    replay.add_argument("capture", type=Path)
    return parser


def _format_config(config: Config) -> str:
    lines: list[str] = []
    for section in fields(config):
        lines.append(f"[{section.name}]")
        values = getattr(config, section.name)
        for field in fields(values):
            lines.append(f"{field.name} = {_display(getattr(values, field.name))}")
    return "\n".join(lines)


def _display(value: object) -> str:
    if isinstance(value, Secret):
        return f'"{value.display()}"'
    if isinstance(value, bool):
        return str(value).lower()
    return "# unset" if value is None else f'"{value}"'


def _replay(config: Config, capture: Path) -> int:
    with open_storage(config.storage) as storage:
        ingestor = Ingestor(storage)
        for record in read_capture(capture):
            ingestor.feed(record)
        ingestor.flush()
    print(
        f"points written: {ingestor.points_written}; "
        f"placeholders discarded: {ingestor.placeholders_discarded}"
    )
    return 0
