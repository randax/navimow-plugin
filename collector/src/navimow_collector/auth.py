"""OAuth credentials kept durable while Navimow's access token rotates."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import re
import socket
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
# The longest the maintenance loop sleeps, so a rejection reported meanwhile or a new login
# written beside the service is acted on within a minute.
POLL_SECONDS = 60
DEFAULT_LIFETIME_SECONDS = 3600
# What binding ::1 fails with on a host without IPv6: none at all, or none on loopback.
_NO_IPV6 = (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL)

_LOGGER = logging.getLogger(__name__)
# Vendor prose meaning the grant itself is dead, so only a new login helps...
_RELOGIN_WORDS = ("invalid", "expired", "unauthorized", "forbidden", "illegal")
# ...as does a 401/403, whether the status line or a code in the body, but not those digits
# inside a longer number or id (an epoch timestamp, a hex ray id, "a1b2-401-c3d4").
_RELOGIN_STATUS = re.compile(r"(?<![\w-])40[13](?![\w-])")
# The gateway's throttling can arrive as a 403 and passes on its own.
_THROTTLED_PHRASES = ("too frequent", "circuit breaker")


@dataclass(frozen=True)
class Credential:
    """The whole rotating credential pair, including the point expiry started."""

    access_token: str
    refresh_token: str
    expires_in: int
    obtained_at: float

    def refresh_at(self) -> float:
        """Refresh with a safety margin so ordinary calls do not see expiry."""
        # Halfway at the latest, so an unexpectedly short lifetime cannot refresh in a loop.
        lead = max(self.expires_in - REFRESH_EARLY_SECONDS, self.expires_in / 2)
        return self.obtained_at + lead


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
        with self._locked():
            self._write(credential)

    def replace(self, expected: Credential | None, credential: Credential) -> bool:
        """Save `credential` only if the file still holds `expected`, so that a login written
        by `navimow-collector login` meanwhile is never overwritten by a refresh."""
        with self._locked():
            try:
                current = self.load()
            except ValueError:
                current = None
            # A missing or corrupt file holds nothing worth keeping; only another login is.
            if current is not None and current != expected:
                return False
            self._write(credential)
            return True

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.path.with_name(f".{self.path.name}.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _write(self, credential: Credential) -> None:
        contents = {
            "access_token": credential.access_token,
            "refresh_token": credential.refresh_token,
            "expires_in": credential.expires_in,
            "obtained_at": credential.obtained_at,
        }
        replace_file(self.path, json.dumps(contents, separators=(",", ":")))


class TokenRequestError(Exception):
    """A token request failed; `relogin` says whether only a new login can help.

    Decided where the facts are: the vendor's own prose is classified, while text we
    compose ourselves (a malformed or truncated credential) never is, since it can contain
    digits such as a byte offset of 401.
    """

    def __init__(self, message: str, *, relogin: bool) -> None:
        super().__init__(message)
        self.relogin = relogin

    @classmethod
    def from_vendor(cls, prose: str) -> TokenRequestError:
        return cls(prose, relogin=_means_relogin(prose))


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
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
            },
            now if now is not None else time(),
        )

    async def refresh(self, credential: Credential, *, now: float) -> Credential:
        """Request a new access token and retain the existing refresh token if omitted."""
        return await self._request(
            {
                "grant_type": "refresh_token",
                "refresh_token": credential.refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            now,
            previous_refresh_token=credential.refresh_token,
        )

    async def _request(
        self, form: dict[str, str], now: float, *, previous_refresh_token: str | None = None
    ) -> Credential:
        async with self._session.request("POST", TOKEN_URL, data=form) as response:
            body = await response.text()
        # Vendor text is logged, so it must not carry anything we sent or it handed back.
        secrets = [form[key] for key in ("code", "refresh_token", "client_secret") if key in form]
        if response.status < 200 or response.status >= 300:
            detail = redact(f"HTTP {response.status}: {body}".rstrip(": "), secrets)
            if response.status >= 500 or response.status in (408, 429):
                # A gateway or overload answer is transient whatever its page says (Apache's
                # stock 502 says "invalid"); only the vendor's own 4xx prose can mean re-login.
                raise TokenRequestError(detail, relogin=False)
            raise TokenRequestError.from_vendor(detail)
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as error:
            # A credential object cut short (never log its token); anything else is prose.
            if body.lstrip().startswith("{") and '"access_token"' in body:
                raise TokenRequestError("truncated token response", relogin=False) from error
            raise TokenRequestError.from_vendor(redact(body, secrets)) from error
        if isinstance(parsed, dict):
            secrets += [str(parsed.get(key)) for key in ("access_token", "refresh_token")]
        if not isinstance(parsed, dict) or not isinstance(parsed.get("access_token"), str):
            # Vendor errors can arrive with status 200: their prose decides.
            raise TokenRequestError.from_vendor(redact(body, secrets))
        try:
            # A refresh response may omit the refresh token; the previous one then still holds.
            refresh_token = parsed.get("refresh_token") or previous_refresh_token
            if not isinstance(refresh_token, str):
                raise ValueError("refresh_token must be a non-empty string")
            return Credential(
                access_token=_required_string(parsed, "access_token"),
                refresh_token=refresh_token,
                expires_in=_lifetime(parsed),
                obtained_at=now,
            )
        except (TypeError, ValueError) as error:
            # Names the bad field, never the token it would otherwise echo.
            raise TokenRequestError(f"malformed credential: {error}", relogin=False) from error


class AuthState(StrEnum):
    """An observable state the health endpoint reports verbatim."""

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
        login_command: str = "navimow-collector login",
    ) -> None:
        self._client = client
        self._store = store
        self._clock = clock
        # What the operator must run to log in again, as this process was configured: the
        # log says it in so many words and the health endpoint repeats it.
        self.login_command = login_command
        # One refresh at a time: the refresh token may rotate, so a second concurrent refresh
        # would spend a token the first has already replaced.
        self._lock = asyncio.Lock()
        self._read_error: str | None = None  # the last unreadable-state warning, said once
        self._credential = self._read_store()
        # What the state file last held, so only a genuinely new login is adopted, never a
        # file left stale by a failed save.
        self._on_disk = self._credential
        self.state = AuthState.FRESH if self._credential else AuthState.RELOGIN_REQUIRED
        if self._credential is None:
            self._ask_for_login(logging.ERROR, "No usable Navimow login in %s", store.path)
        self.next_attempt_at: float | None = None
        self._failures = 0
        # The access token a caller has reported as rejected, until a refresh replaces it.
        self._rejected: str | None = None
        # The token a rejection-triggered refresh produced: if that is rejected too, refreshing
        # on every 401 will not help, so the retry ladder takes over.
        self._minted_for_rejection: str | None = None
        self._minted_at = 0.0

    async def access_token(self) -> str | None:
        """Return the current token, refreshing once it is due."""
        async with self._lock:
            now = self._clock()
            self._adopt_new_login()
            self._note_recovery(now)
            if self._due(now):
                await self._refresh(now)
            return self._token()

    async def on_unauthorized(self, rejected_token: str) -> str | None:
        """Refresh after a REST HTTP 401 for `rejected_token`."""
        return await self._on_rejection(rejected_token)

    async def on_mqtt_error(self, error: str, rejected_token: str) -> str | None:
        """Refresh when MQTT says the OAuth data behind `rejected_token` is no longer valid.

        Pass the access token the MQTT credentials were fetched with, not the current one:
        credentials outlive a proactive refresh, and blaming the new token for the old
        credentials would trigger a needless recovery refresh.
        """
        if MQTT_OAUTH_ERROR in error:
            return await self._on_rejection(rejected_token)
        return await self.access_token()

    def seconds_until_next_action(self) -> float:
        """How long a scheduling loop may sleep before calling `access_token` again."""
        if self.state is AuthState.RELOGIN_REQUIRED or self._credential is None:
            return POLL_SECONDS
        due = self._credential.refresh_at()
        if self.next_attempt_at is not None:
            due = self.next_attempt_at
        return max(0.0, due - self._clock())

    async def _on_rejection(self, rejected_token: str) -> str | None:
        async with self._lock:
            now = self._clock()
            self._adopt_new_login()
            self._note_recovery(now)
            # A rejection of a token already replaced (a slow response) changes nothing. A
            # token newly known to be dead earns one attempt at once; after that a burst of
            # rejections waits for the retry ladder rather than hammering the endpoint.
            if (
                self.state is not AuthState.RELOGIN_REQUIRED
                and self._credential is not None
                and self._credential.access_token == rejected_token
            ):
                newly_rejected = self._rejected != rejected_token
                self._rejected = rejected_token
                if rejected_token == self._minted_for_rejection:
                    self._minted_for_rejection = None
                    self._record_failure(
                        "a freshly refreshed access token was rejected too", now, relogin=False
                    )
                elif newly_rejected or self._may_attempt(now):
                    await self._refresh(now, after_rejection=True)
            return self._token()

    def _note_recovery(self, now: float) -> None:
        """A recovery token that served half its life proves refreshing works: reset the ladder.

        The manager never sees a token's first use, only rejections, and callers present a
        new token at their own cadence (a REST poll every few minutes, an MQTT reconnect
        backoff). So "rejected on first use" means rejected within half the token's life: a
        token the vendor kills after half an hour (another client refreshing on the account,
        say) is simply refreshed again, while tokens that never work climb the retry ladder.
        """
        credential = self._credential
        if not self._minted_for_rejection or credential is None:
            return
        if now - self._minted_at >= (credential.refresh_at() - credential.obtained_at) / 2:
            self._minted_for_rejection = None
            self._failures = 0

    def _due(self, now: float) -> bool:
        if self._credential is None or not self._may_attempt(now):
            return False
        if self.state is AuthState.RELOGIN_REQUIRED:
            return True  # the hourly probe, in case the rejection was a gateway's, not Navimow's
        known_dead = self._rejected == self._credential.access_token
        return known_dead or self._credential.refresh_at() <= now

    def _token(self) -> str | None:
        return self._credential.access_token if self._credential else None

    def _read_store(self) -> Credential | None:
        try:
            credential = self._store.load()
        except (OSError, ValueError) as error:
            # Read on every call while awaiting a login, so say it once, not per call.
            if str(error) != self._read_error:
                _LOGGER.warning("Could not read Navimow login state: %s", error)
            self._read_error = str(error)
            return None
        self._read_error = None
        return credential

    def _adopt_new_login(self) -> bool:
        """Pick up `navimow-collector login` run beside the service, so no restart is needed.

        Checked in every state: a grant can be dead without the vendor saying so in words we
        recognise, leaving the service on the retry ladder while the operator logs in again.
        """
        stored = self._read_store()
        if stored is None or stored == self._on_disk:
            return False
        self._credential = self._on_disk = stored
        self._rejected = None
        self._failures = 0
        self.next_attempt_at = None
        self.state = AuthState.FRESH
        _LOGGER.info("Navimow login found in %s; authentication restored", self._store.path)
        return True

    def _may_attempt(self, now: float) -> bool:
        return self.next_attempt_at is None or now >= self.next_attempt_at

    async def _refresh(self, now: float, *, after_rejection: bool = False) -> None:
        assert self._credential is not None
        # Replacing a token reported dead is an attempt at recovery, not proof of it: the
        # retry ladder keeps climbing until a token survives to its proactive refresh.
        recovering = after_rejection or self._rejected == self._credential.access_token
        probing = self.state is AuthState.RELOGIN_REQUIRED
        self.state = AuthState.REFRESHING
        try:
            refreshed = await self._client.refresh(self._credential, now=now)
        except TokenRequestError as error:
            self._record_failure(str(error), now, relogin=error.relogin, probing=probing)
            return
        except Exception as error:  # Transport errors, whatever their wording, are transient.
            self._record_failure(str(error), now, relogin=False, probing=probing)
            return
        # Adopt before saving: the old refresh token may already be spent, so the new pair
        # must keep serving even if the disk refuses it.
        self._credential = refreshed
        self._rejected = None
        self._minted_for_rejection = refreshed.access_token if recovering else None
        self._minted_at = now
        if not recovering:
            self._failures = 0
        self.next_attempt_at = None
        self.state = AuthState.FRESH
        try:
            saved = self._store.replace(self._on_disk, refreshed)
        except OSError as error:
            _LOGGER.error("Could not save refreshed Navimow credentials: %s", error)
            return
        if saved:
            self._on_disk = refreshed
        else:
            # The operator logged in while the request was in flight: that is newer intent
            # than the grant just refreshed, so it wins.
            self._adopt_new_login()

    def _record_failure(
        self, detail: str, now: float, *, relogin: bool, probing: bool = False
    ) -> None:
        if probing and not relogin:
            # The hourly probe of a rejected login failed for some other reason: still no
            # evidence the login works, so the state stands, but nothing was rejected.
            self.state = AuthState.RELOGIN_REQUIRED
            self.next_attempt_at = now + RETRY_DELAYS[-1]
            _LOGGER.warning("Navimow login still unverified; hourly probe failed: %s", detail)
            return
        if relogin:
            self.state = AuthState.RELOGIN_REQUIRED
            # Still probed hourly: a gateway's 401/403 page can look like a dead grant.
            self.next_attempt_at = now + RETRY_DELAYS[-1]
            # An error once, on entering the state; the hourly probe only reminds.
            self._ask_for_login(
                logging.WARNING if probing else logging.ERROR,
                "Navimow %s the stored login (%s)",
                "still rejects" if probing else "rejected",
                detail,
            )
            return
        delay = RETRY_DELAYS[min(self._failures, len(RETRY_DELAYS) - 1)]
        self._failures += 1
        self.next_attempt_at = now + delay
        self.state = AuthState.RETRY_PENDING
        if delay == RETRY_DELAYS[-1]:
            # Hours of failure may be a dead grant in words we do not recognise.
            self._ask_for_login(
                logging.ERROR,
                "Navimow token refresh keeps failing (%s); retrying hourly, but if this "
                "persists a new login is needed",
                detail,
            )
        else:
            _LOGGER.warning(
                "Navimow token refresh failed; retrying in %s seconds: %s", delay, detail
            )

    def _ask_for_login(self, level: int, message: str, *args: object) -> None:
        """Log `message`, then the exact login to run, in the text and as a `command` field."""
        _LOGGER.log(
            level,
            f"{message}; run `%s`",
            *args,
            self.login_command,
            extra={"command": self.login_command},
        )


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the vendor login link while preserving its required channel parameter."""
    parameters = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}&{urlencode(parameters)}"


class _IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _loopback_servers(
    port: int, handler: type[BaseHTTPRequestHandler]
) -> list[ThreadingHTTPServer]:
    """One port on both loopback addresses: `localhost` may resolve to ::1 first, and not
    every browser then tries 127.0.0.1. A host without IPv6 is served on 127.0.0.1 alone."""
    ipv4 = ThreadingHTTPServer(("127.0.0.1", port), handler)
    try:
        return [ipv4, _IPv6Server(("::1", ipv4.server_port), handler)]
    except OSError as error:
        if error.errno in _NO_IPV6:
            return [ipv4]
        # Listening on 127.0.0.1 alone would leave a browser that takes `localhost` to be
        # ::1 with no one to answer it, or with whoever else holds the port there.
        ipv4.server_close()
        raise OSError(
            error.errno,
            f"cannot listen for the login redirect on [::1]:{ipv4.server_port}: {error.strerror}",
        ) from error


class LoopbackListener:
    """A short-lived local callback server which rejects a redirect with the wrong state."""

    def __init__(self, state: str, port: int = 0) -> None:
        self._state = state
        self._code: str | None = None
        self._received = threading.Event()
        self._accepting = threading.Lock()
        listener = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
                if not listener._accept(self.path):
                    self.send_error(400, "invalid Navimow login callback")
                    return
                body = b"Login complete. You can close this tab."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return None

        self._servers = _loopback_servers(port, CallbackHandler)
        # Polled often: leaving the listener waits out one poll for each address.
        self._threads = [
            threading.Thread(target=server.serve_forever, args=(0.1,), daemon=True)
            for server in self._servers
        ]

    @property
    def redirect_uri(self) -> str:
        # `localhost`, not 127.0.0.1: the vendor's redirect validation is undocumented, and
        # the only loopback form known to be accepted is http://localhost:1/callback.
        return f"http://localhost:{self._servers[0].server_port}/callback"

    def _accept(self, path: str) -> bool:
        parsed = urlparse(path)
        parameters = parse_qs(parsed.query)
        code = parameters.get("code", [""])[0]
        if parsed.path != "/callback" or parameters.get("state") != [self._state] or not code:
            return False
        with self._accepting:  # handler threads may race on a double-loaded redirect
            if self._received.is_set():
                return False  # exactly one login per listener; a replay changes nothing
            self._code = code
            self._received.set()
            return True

    def __enter__(self) -> LoopbackListener:
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        for server, thread in zip(self._servers, self._threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join()

    def wait(self, timeout: float) -> str:
        """Wait for exactly one valid redirect instead of trusting arbitrary local traffic."""
        if not self._received.wait(timeout):
            raise TimeoutError("timed out waiting for the Navimow login redirect")
        assert self._code is not None
        return self._code


async def maintain(
    manager: TokenManager, sleep: Callable[[float], Awaitable[object]] = asyncio.sleep
) -> None:
    """Keep the credential fresh for the life of the process; a re-login is awaited, not fatal."""
    while True:
        await sleep(min(manager.seconds_until_next_action(), POLL_SECONDS))
        await manager.access_token()


def _required_string(value: dict[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{name} must be a non-empty string")
    return result


def _lifetime(value: dict[str, Any]) -> int:
    """A usable credential is never discarded over its lifetime field: that would spend a
    rotated refresh token. An odd or missing value falls back to the documented hour."""
    raw = value.get("expires_in")
    try:
        lifetime = 0
        if isinstance(raw, float) and raw.is_integer():
            lifetime = int(raw)
        elif isinstance(raw, int | str) and not isinstance(raw, bool):
            lifetime = int(raw)
    except ValueError:
        lifetime = 0
    if lifetime > 0:
        return lifetime
    _LOGGER.warning("Navimow token response has no usable expires_in (%r); assuming 3600", raw)
    return DEFAULT_LIFETIME_SECONDS


def replace_file(path: Path, contents: str) -> None:
    """Replace a state file only once its complete, owner-only contents are on disk."""
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            os.chmod(temporary_name, 0o600)
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if len(secret) >= 8:  # long enough to be a credential, not an ordinary word
            text = text.replace(secret, "<redacted>")
    return text


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


def _means_relogin(detail: str) -> bool:
    lowered = detail.lower()
    if any(phrase in lowered for phrase in _THROTTLED_PHRASES):
        return False
    return any(word in lowered for word in _RELOGIN_WORDS) or bool(_RELOGIN_STATUS.search(lowered))


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
