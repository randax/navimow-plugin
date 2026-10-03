"""OAuth credentials kept durable while Navimow's access token rotates."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from mower_sdk.http import HTTPSession

AUTHORIZE_URL = "https://navimow-h5-fra.willand.com/smartHome/login?channel=homeassistant"
TOKEN_URL = "https://navimow-fra.ninebot.com/openapi/oauth/getAccessToken"
MANUAL_REDIRECT_URI = "http://localhost:1/callback"
REFRESH_EARLY_SECONDS = 300
RETRY_DELAYS = (60, 300, 900, 3600)
MQTT_OAUTH_ERROR = "CODE_OAUTH_INFO_ILLEGAL"
RELOGIN_POLL_SECONDS = 60

_LOGGER = logging.getLogger(__name__)
_DETERMINISTIC_WORDS = ("401", "403", "invalid", "expired", "unauthorized", "forbidden")


@dataclass(frozen=True)
class Credential:
    """The whole rotating credential pair, including the point expiry started."""

    access_token: str
    refresh_token: str
    expires_in: int
    obtained_at: float

    def refresh_at(self) -> float:
        """Refresh with a safety margin so ordinary calls do not see expiry."""
        return self.obtained_at + max(0, self.expires_in - REFRESH_EARLY_SECONDS)


class TokenStore:
    """Persist credentials atomically so an interruption never leaves partial JSON."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> Credential | None:
        """Return the stored credential, or no credential before the first login."""
        try:
            contents = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            value = json.loads(contents)
            if not isinstance(value, dict):
                raise ValueError("token file must contain an object")
            return Credential(
                access_token=_required_string(value, "access_token"),
                refresh_token=_required_string(value, "refresh_token"),
                expires_in=_required_int(value, "expires_in"),
                obtained_at=_required_number(value, "obtained_at"),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid token file {self.path}: {error}") from error

    def save(self, credential: Credential) -> None:
        """Replace the state file only after its complete, private contents hit disk."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                os.chmod(temporary_name, 0o600)
                json.dump(
                    {
                        "access_token": credential.access_token,
                        "refresh_token": credential.refresh_token,
                        "expires_in": credential.expires_in,
                        "obtained_at": credential.obtained_at,
                    },
                    temporary,
                    separators=(",", ":"),
                )
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
            _fsync_directory(self.path.parent)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)


class TokenRequestError(Exception):
    """The vendor declined a token request and supplied human-readable text."""


class TokenClient:
    """The narrow token endpoint boundary, deliberately using the SDK session protocol."""

    def __init__(self, session: HTTPSession, client_id: str, client_secret: str) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret

    async def exchange(
        self, code: str, redirect_uri: str, *, now: float | None = None
    ) -> Credential:
        """Exchange the one-time browser code without retaining it in configuration."""
        return await self._request(
            {
                "grant_type": "authorization_code",
                "code": authorization_code(code),
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
            },
            now if now is not None else time(),
        )

    async def refresh(self, credential: Credential, *, now: float) -> Credential:
        """Request a new access token and retain the existing refresh token if omitted."""
        refreshed = await self._request(
            {
                "grant_type": "refresh_token",
                "refresh_token": credential.refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            now,
        )
        return Credential(
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token or credential.refresh_token,
            expires_in=refreshed.expires_in,
            obtained_at=refreshed.obtained_at,
        )

    async def _request(self, form: dict[str, str], now: float) -> Credential:
        async with self._session.request("POST", TOKEN_URL, data=form) as response:
            body = await response.text()
        if response.status < 200 or response.status >= 300:
            raise TokenRequestError(body or f"HTTP {response.status}")
        try:
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError("token response is not an object")
            return Credential(
                access_token=_required_string(parsed, "access_token"),
                refresh_token=_optional_string(parsed, "refresh_token"),
                expires_in=_required_int(parsed, "expires_in"),
                obtained_at=now,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            # Vendor errors can arrive with status 200, so classification needs their prose;
            # the wording here must not itself match a deterministic word, and must not log
            # a token from a half-formed credential.
            detail = "malformed credential" if "access_token" in body else body
            raise TokenRequestError(f"unexpected token response: {detail}") from error


class AuthState(StrEnum):
    """An observable state a future health endpoint can report verbatim."""

    FRESH = "fresh"
    REFRESHING = "refreshing"
    RETRY_PENDING = "retry-pending"
    RELOGIN_REQUIRED = "relogin-required"


class TokenManager:
    """Decide when to refresh without sleeping, allowing callers to own their event loop."""

    def __init__(
        self,
        client: TokenClient,
        store: TokenStore,
        *,
        clock: Callable[[], float] = time,
    ) -> None:
        self._client = client
        self._store = store
        self._clock = clock
        # One refresh at a time: the refresh token may rotate, so a second concurrent refresh
        # would spend a token the first has already replaced.
        self._lock = asyncio.Lock()
        self._credential = store.load()
        self.state = AuthState.FRESH if self._credential is not None else AuthState.RELOGIN_REQUIRED
        self.next_attempt_at: float | None = None
        self._failures = 0

    async def access_token(self) -> str | None:
        """Return the current token, proactively refreshing once it reaches its margin."""
        async with self._lock:
            now = self._clock()
            if self._credential is not None and self.state is not AuthState.RELOGIN_REQUIRED:
                if self._credential.refresh_at() <= now and self._may_attempt(now):
                    await self._refresh(now)
            return self._current()

    async def on_unauthorized(self) -> str | None:
        """Refresh after a REST HTTP 401 response."""
        return await self._refresh_on_rejection(self._credential)

    async def on_mqtt_error(self, error: str) -> str | None:
        """Refresh when MQTT says its OAuth data is no longer valid."""
        if MQTT_OAUTH_ERROR in error:
            return await self._refresh_on_rejection(self._credential)
        return await self.access_token()

    def seconds_until_next_action(self) -> float:
        """How long a scheduling loop may sleep before calling `access_token` again."""
        now = self._clock()
        if self.state is AuthState.RELOGIN_REQUIRED or self._credential is None:
            return RELOGIN_POLL_SECONDS
        due = self._credential.refresh_at()
        if self.state is AuthState.RETRY_PENDING and self.next_attempt_at is not None:
            due = self.next_attempt_at
        return max(0.0, due - now)

    async def _refresh_on_rejection(self, rejected: Credential | None) -> str | None:
        async with self._lock:
            now = self._clock()
            # Another caller may already have replaced the rejected token, and a retry
            # already scheduled must not be brought forward by a burst of rejections.
            if (
                rejected is not None
                and rejected is self._credential
                and self.state is not AuthState.RELOGIN_REQUIRED
                and self._may_attempt(now)
            ):
                await self._refresh(now)
            return self._current()

    def _current(self) -> str | None:
        if self.state is AuthState.RELOGIN_REQUIRED:
            self._adopt_new_login()
        return self._credential.access_token if self._credential else None

    def _adopt_new_login(self) -> None:
        """Pick up `navimow-collector login` run beside the service, so no restart is needed."""
        try:
            stored = self._store.load()
        except ValueError:
            return
        if stored is not None and stored != self._credential:
            self._credential = stored
            self._failures = 0
            self.next_attempt_at = None
            self.state = AuthState.FRESH
            _LOGGER.info("Navimow login found in %s; authentication restored", self._store.path)

    def _may_attempt(self, now: float) -> bool:
        return self.next_attempt_at is None or now >= self.next_attempt_at

    async def _refresh(self, now: float) -> None:
        assert self._credential is not None
        self.state = AuthState.REFRESHING
        try:
            refreshed = await self._client.refresh(self._credential, now=now)
        except TokenRequestError as error:
            self._record_failure(str(error), now, deterministic=_is_deterministic(str(error)))
            return
        except Exception as error:  # Transport errors, whatever their wording, are transient.
            self._record_failure(str(error), now, deterministic=False)
            return
        # Adopt before saving: the old refresh token may already be spent, so the new pair
        # must keep serving even if the disk refuses it.
        self._credential = refreshed
        self._failures = 0
        self.next_attempt_at = None
        self.state = AuthState.FRESH
        try:
            self._store.save(refreshed)
        except OSError as error:
            _LOGGER.error("Could not save refreshed Navimow credentials: %s", error)

    def _record_failure(self, detail: str, now: float, *, deterministic: bool) -> None:
        if deterministic:
            self.state = AuthState.RELOGIN_REQUIRED
            self.next_attempt_at = None
            _LOGGER.error(
                "Navimow rejected the stored login (%s); run `navimow-collector login`", detail
            )
            return
        delay = RETRY_DELAYS[min(self._failures, len(RETRY_DELAYS) - 1)]
        self._failures += 1
        self.next_attempt_at = now + delay
        self.state = AuthState.RETRY_PENDING
        _LOGGER.warning("Navimow token refresh failed; retrying in %s seconds: %s", delay, detail)


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the vendor login link while preserving its required channel parameter."""
    parameters = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}&{urlencode(parameters)}"


def authorization_code(value: str) -> str:
    """Accept either a copied code or the complete redirect URL from a failed browser load."""
    parsed = urlparse(value.strip())
    if parsed.scheme and parsed.netloc:
        code = parse_qs(parsed.query).get("code", [""])[0]
    else:
        code = value.strip()
    if not code:
        raise ValueError("login code is missing")
    return code


class LoopbackListener:
    """A short-lived local callback server which rejects a redirect with the wrong state."""

    def __init__(self, state: str, port: int = 0) -> None:
        self._state = state
        self._code: str | None = None
        self._received = threading.Event()
        listener = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
                parsed = urlparse(self.path)
                parameters = parse_qs(parsed.query)
                valid_state = parameters.get("state", [None])[0] == listener._state
                if parsed.path != "/callback" or not valid_state:
                    self.send_error(400, "invalid OAuth callback")
                    return
                code = parameters.get("code", [""])[0]
                if not code:
                    self.send_error(400, "missing OAuth code")
                    return
                listener._code = code
                listener._received.set()
                body = b"Login complete. You can close this tab."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", port), CallbackHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/callback"

    def __enter__(self) -> LoopbackListener:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def wait(self, timeout: float) -> str:
        """Wait for exactly one valid redirect instead of trusting arbitrary local traffic."""
        if not self._received.wait(timeout):
            raise TimeoutError("timed out waiting for the Navimow login redirect")
        assert self._code is not None
        return self._code


@contextmanager
def loopback_listener(state: str, port: int = 0) -> Iterator[LoopbackListener]:
    """Make listener lifetime explicit at the CLI boundary and easy to exercise in tests."""
    with LoopbackListener(state, port) as listener:
        yield listener


async def maintain(
    manager: TokenManager, sleep: Callable[[float], Awaitable[object]] = asyncio.sleep
) -> None:
    """Keep the credential fresh for the life of the process; a re-login is awaited, not fatal."""
    while True:
        await sleep(manager.seconds_until_next_action())
        await manager.access_token()


def _required_string(value: dict[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{name} must be a non-empty string")
    return result


def _optional_string(value: dict[str, Any], name: str) -> str:
    result = value.get(name, "")
    if result is None:
        return ""
    if not isinstance(result, str):
        raise ValueError(f"{name} must be a string")
    return result


def _required_int(value: dict[str, Any], name: str) -> int:
    result = value.get(name)
    if not isinstance(result, int) or isinstance(result, bool) or result <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _required_number(value: dict[str, Any], name: str) -> float:
    result = value.get(name)
    if not isinstance(result, (int, float)) or isinstance(result, bool):
        raise ValueError(f"{name} must be a number")
    return float(result)


def _is_deterministic(detail: str) -> bool:
    return any(word in detail.lower() for word in _DETERMINISTIC_WORDS)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
