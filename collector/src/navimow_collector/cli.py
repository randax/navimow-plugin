"""Command-line operations: live collection, capture replay and establishing OAuth access."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import shlex
import signal
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mower_sdk.http import HTTPSession, UrllibSession

from .auth import (
    MANUAL_REDIRECT_URI,
    LoopbackListener,
    TokenClient,
    TokenManager,
    TokenRequestError,
    TokenStore,
    authorization_url,
)
from .config import Config, ConfigError, Secret, load_config
from .ingest import Ingestor, read_capture
from .live import Collector
from .storage import StorageError, open_storage
from .storage.buffered import BufferedStorage


def main(
    argv: list[str] | None = None, *, session_factory: Callable[[], HTTPSession] = UrllibSession
) -> int:
    """Run the collector command and return a shell-compatible status code."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        config = load_config(args.config)
        if args.command == "config":
            print(_format_config(config))
            return 0
        if args.command == "login":
            return _login(config, args, session_factory)
        if args.command == "run":
            return _run(config, session_factory)
        return _replay(config, args.capture)
    except (ConfigError, StorageError, TokenRequestError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Navimow data and establish OAuth access.")
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config", help="print resolved configuration")
    commands.add_parser("run", help="collect from every mower on the account until stopped")
    replay = commands.add_parser("replay", help="replay one JSONL capture")
    replay.add_argument("capture", type=Path)
    login = commands.add_parser("login", help="sign in once and store rotating OAuth credentials")
    source = login.add_mutually_exclusive_group()
    source.add_argument("--code", help="authorization code or the complete redirect URL")
    source.add_argument(
        "--no-browser",
        action="store_true",
        help="print the manual login URL instead of opening a local callback listener",
    )
    login.add_argument(
        "--timeout", type=float, default=300, help="loopback callback timeout in seconds"
    )
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


def _run(config: Config, session_factory: Callable[[], HTTPSession]) -> int:
    """Collect live until SIGINT or SIGTERM; only a database missing at startup is fatal."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    state_dir = Path(config.collector.state_dir).expanduser()
    storage = BufferedStorage(lambda: open_storage(config.storage), state_dir / "buffer.jsonl")
    storage.connect()
    try:
        asyncio.run(_collect(config, session_factory(), storage, state_dir))
    finally:
        storage.close()
    return 0


async def _collect(
    config: Config, session: HTTPSession, storage: BufferedStorage, state_dir: Path
) -> None:
    tokens = TokenManager(
        TokenClient(session, config.auth.client_id, config.auth.client_secret.reveal()),
        TokenStore(config.auth.state_file),
    )
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    await Collector(session, tokens, storage, state_dir).run(stop)


def _login(
    config: Config, args: argparse.Namespace, session_factory: Callable[[], HTTPSession]
) -> int:
    """Complete exactly one authorization-code flow; automatic refresh happens at runtime."""
    client_id = config.auth.client_id
    if args.no_browser:
        print(authorization_url(client_id, MANUAL_REDIRECT_URI, secrets.token_urlsafe(32)))
        config_option = f"--config {shlex.quote(str(args.config))} " if args.config else ""
        print(
            f"After signing in, run navimow-collector {config_option}"
            "login --code '<code-or-redirect-url>'."
        )
        return 0
    if args.code:
        code, redirect_uri = _pasted_login(args.code)
    else:
        code, redirect_uri = _browser_code(client_id, args.timeout)

    client = TokenClient(session_factory(), client_id, config.auth.client_secret.reveal())
    store = TokenStore(config.auth.state_file)
    store.save(asyncio.run(client.exchange(code, redirect_uri)))
    print(f"Navimow login complete; credentials saved to {store.path}")
    return 0


def _browser_code(client_id: str, timeout: float) -> tuple[str, str]:
    """Capture the redirect on a temporary loopback listener; the code is bound to its URI."""
    state = secrets.token_urlsafe(32)
    with LoopbackListener(state) as listener:
        url = authorization_url(client_id, listener.redirect_uri, state)
        print(url)
        webbrowser.open(url)
        try:
            return listener.wait(timeout), listener.redirect_uri
        except TimeoutError as error:
            raise TimeoutError(
                f"{error}; without a local browser, use `navimow-collector login --no-browser`"
            ) from error


def _pasted_login(value: str) -> tuple[str, str]:
    """Accept a copied code or the redirect URL from a failed browser load, with or without
    its scheme (some address bars copy `localhost:1/callback?code=...`).

    The code is bound to the redirect it was issued for, so a full URL (such as the loopback
    listener's, pasted after the browser flow timed out) is exchanged with its own redirect.
    """
    value = value.strip()
    code, redirect_uri = value, MANUAL_REDIRECT_URI
    if "code=" in value:
        if "://" not in value and "/" in value.partition("?")[0]:
            value = f"http://{value}"  # an address bar dropped the scheme, keep host and port
        url = urlparse(value)
        code = parse_qs(url.query or value.partition("?")[2] or value).get("code", [""])[0]
        if url.scheme in ("http", "https") and url.netloc:
            redirect_uri = f"{url.scheme}://{url.netloc}{url.path}"
    if not code:
        raise ValueError("login code is missing")
    return code, redirect_uri
