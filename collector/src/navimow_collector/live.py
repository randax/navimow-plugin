"""Live collection: the transport that feeds a real mower's stream to the ingestion core."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Protocol

from mower_sdk.http import HTTPSession
from mower_sdk.models import Device
from mower_sdk.mqtt import NavimowMQTT

from .auth import RETRY_DELAYS, TokenManager
from .ingest import Ingestor
from .storage.buffered import BufferedStorage

API_URL = "https://navimow-fra.ninebot.com"
# The SDK's default of 40 minutes lets the broker drop an idle connection after about ten,
# silently: nothing notices until the mower next has something to say.
KEEPALIVE_SECONDS = 60
# How often a live connection is noted on disk; a restart's gap starts at the last note.
HEARTBEAT_SECONDS = 60
TICK_SECONDS = 10

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerCredentials:
    """Where and how to reach the broker; the vendor binds them to one access token."""

    host: str
    ws_path: str
    username: str
    password: str
    access_token: str

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


class Broker(Protocol):
    """The part of the SDK's `NavimowMQTT` the collector drives, and tests replace."""

    on_connected: Callable[[], Awaitable[None]] | None
    on_disconnected: Callable[[], Awaitable[None]] | None
    on_raw: Callable[[str, bytes], Awaitable[None]] | None

    def connect_async(self) -> None: ...

    def disconnect(self) -> None: ...

    def update_credentials(
        self,
        username: str | None = None,
        password: str | None = None,
        auth_headers: dict[str, str] | None = None,
    ) -> None: ...


def connect_broker(credentials: BrokerCredentials, mowers: Sequence[str]) -> Broker:
    """The real broker connection, subscribed to every channel of every mower."""
    broker: Broker = NavimowMQTT(
        broker=credentials.host,
        port=443,
        username=credentials.username,
        password=credentials.password,
        records=[Device.from_dict({"id": mower}) for mower in mowers],
        ws_path=credentials.ws_path,
        auth_headers=credentials.auth_headers(),
        keepalive_seconds=KEEPALIVE_SECONDS,
        subscribe_location=True,
    )
    return broker


class RestError(Exception):
    """A REST call failed; `status` is the HTTP status when the vendor answered with one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Collector:
    """Record every mower on one account, from the broker into the ingestion core."""

    def __init__(
        self,
        session: HTTPSession,
        tokens: TokenManager,
        storage: BufferedStorage,
        state_dir: Path,
        *,
        connect: Callable[[BrokerCredentials, Sequence[str]], Broker] = connect_broker,
        clock: Callable[[], float] = time,
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._storage = storage
        self._ingestor = Ingestor(storage)
        self._connect = connect
        self._clock = clock
        self._mowers: tuple[str, ...] = ()
        self._credentials: BrokerCredentials | None = None
        self._broker: Broker | None = None
        self._address: tuple[str, str] | None = None  # where the broker connection points
        # The credential endpoint rate-limits aggressively. No two fetches come within a
        # minute, and the wait grows with every fetch for the same access token since the
        # broker last accepted a connection.
        self._fetched_at = float("-inf")
        self._fetched_for: str | None = None
        self._fetches = 0
        self.connected = False
        self._down_since: float | None = None
        # When the stream last flowed, kept on disk so that a restart knows it too. While
        # disconnected it is the start of the gap the next connection will record.
        self._marker = state_dir / "connected-until"
        self._flowed_until = _read_marker(self._marker)
        self._gap_reason = "restart"

    async def collect(self, stop: asyncio.Event) -> None:
        """Collect until `stop` is set."""
        try:
            while not stop.is_set():
                await self.tick()
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), TICK_SECONDS)
        finally:
            self.stop()

    async def tick(self) -> None:
        """Do whatever is due; called every few seconds for the life of the process."""
        token = await self._tokens.access_token()
        if token is not None:
            await self._keep_credentials(token)
        now = self._clock()
        if self.connected and now - (self._flowed_until or 0) >= HEARTBEAT_SECONDS:
            self._mark(now)
        self._storage.flush()

    def stop(self) -> None:
        """Disconnect; the next start records the time from here as a gap."""
        broker, self._broker = self._broker, None
        if broker is not None:
            broker.disconnect()
        if self.connected:
            self.connected = False
            self._mark(self._clock())

    async def _keep_credentials(self, token: str) -> None:
        """Fetch broker credentials only when the cached ones cannot be right.

        They are bound to their access token, so a refreshed token needs new ones. The SDK
        reconnects on the cached ones by itself; only a connection that stays down suggests
        the broker stopped honouring them.
        """
        now = self._clock()
        held = self._credentials
        down_long = self._down_since is not None and now - self._down_since >= RETRY_DELAYS[0]
        if held is not None and held.access_token == token and not down_long:
            return
        if token != self._fetched_for:
            self._fetches = 0  # a new access token has not failed yet
        wait = RETRY_DELAYS[min(max(self._fetches - 1, 0), len(RETRY_DELAYS) - 1)]
        if now < self._fetched_at + wait:
            return
        self._fetched_at, self._fetched_for = now, token
        self._fetches += 1
        try:
            if not self._mowers:
                self._mowers = await self._discover(token)
                self._fetched_at = self._clock()  # the wait is owed to the credential endpoint
            credentials = await self._fetch_credentials(token)
        except RestError as error:
            _LOGGER.warning("Could not fetch broker credentials; will retry: %s", error)
            await self._rejected(error, token)
            return
        if self.connected:
            self._fetches = 0
        self._credentials = credentials
        self._use(credentials, now)

    def _use(self, credentials: BrokerCredentials, now: float) -> None:
        """Hand fresh credentials to the broker connection, opening it if there is none."""
        address = (credentials.host, credentials.ws_path)
        if self._broker is not None and address != self._address and not self.connected:
            # The broker moved, and only a new connection can follow it. A connection that
            # still works is kept until it drops.
            self._broker.disconnect()
            self._broker = None
        if self._broker is not None:
            # Applied by the SDK on its next reconnect; a live connection is left alone.
            self._broker.update_credentials(
                credentials.username, credentials.password, credentials.auth_headers()
            )
            return
        _LOGGER.info("Connecting to the broker for mowers %s", ", ".join(self._mowers))
        broker = self._broker = self._connect(credentials, self._mowers)
        self._address = address
        broker.on_connected = self._while_current(broker, self._on_connected)
        broker.on_disconnected = self._while_current(broker, self._on_disconnected)
        broker.on_raw = self._while_current(broker, self._on_raw)
        broker.connect_async()
        self._down_since = self._down_since or now

    def _while_current(
        self, broker: Broker, handler: Callable[..., Awaitable[None]]
    ) -> Callable[..., Awaitable[None]]:
        """A callback the SDK had already queued when its connection was retired or the
        collector stopped must not act: it would speak for a connection that is not its own."""

        async def guarded(*args: Any) -> None:
            if broker is self._broker:
                await handler(*args)

        return guarded

    async def _rejected(self, error: RestError, token: str) -> None:
        """Let the token manager judge a refusal; an access token found dead is refreshed."""
        if error.status == 401:
            await self._tokens.on_unauthorized(token)
        else:
            await self._tokens.on_mqtt_error(str(error), token)

    async def _on_connected(self) -> None:
        """Record the gap this connection ends, then ask where every mower stands."""
        now = self._clock()
        gap_start = None if self.connected else self._flowed_until
        self.connected = True
        self._down_since = None
        self._fetches = 0
        _LOGGER.info("Broker connected")
        if gap_start is not None:
            # Written even when no time is seen to pass: a stalled loop runs the drop and
            # the reconnection back to back, and messages were lost all the same.
            for mower in self._mowers:
                self._feed(
                    {
                        "recv_ms": round(now * 1000),
                        "kind": "gap",
                        "mower_id": mower,
                        "start_ms": round(min(gap_start, now) * 1000),
                        "reason": self._gap_reason,
                    }
                )
        # Only now may the start of that gap be forgotten: a crash before this line finds
        # it still on disk, and records the same gap again, extended.
        self._mark(now)
        await self._poll_status()

    async def _on_disconnected(self) -> None:
        if not self.connected:
            return  # a failed attempt to reconnect: the gap is already open
        self.connected = False
        self._gap_reason = "reconnect"
        self._down_since = self._clock()
        self._mark(self._down_since)
        _LOGGER.warning("Broker connection lost; reconnecting")

    def _mark(self, now: float) -> None:
        self._flowed_until = now
        try:
            self._marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self._marker.with_name(f".{self._marker.name}.tmp")
            temporary.write_text(repr(now), encoding="utf-8")
            os.replace(temporary, self._marker)
        except OSError as error:
            _LOGGER.warning("Could not note the connection in %s: %s", self._marker, error)

    async def _poll_status(self) -> None:
        """One status request, so current state is known without waiting for the next push."""
        token = await self._tokens.access_token()
        if token is None:
            return
        devices = [{"id": mower} for mower in self._mowers]
        try:
            answer = await self._call(
                "POST", "/openapi/smarthome/getVehicleStatus", token, {"devices": devices}
            )
        except RestError as error:
            _LOGGER.warning("Status poll failed: %s", error)
            await self._rejected(error, token)
            return
        self._feed({"kind": "rest", "endpoint": "getVehicleStatus", "payload": answer})

    async def _on_raw(self, topic: str, payload: bytes) -> None:
        self._feed({"kind": "mqtt", "topic": topic, "payload": _parse(payload)})

    def _feed(self, record: dict[str, object]) -> None:
        """Hand the core a record in the capture format, exactly as replay does."""
        self._ingestor.feed({"recv_ms": round(self._clock() * 1000), **record})
        self._ingestor.flush()

    async def _discover(self, token: str) -> tuple[str, ...]:
        answer = await self._call("GET", "/openapi/smarthome/authList", token)
        try:
            mowers = tuple(str(device["id"]) for device in answer["data"]["payload"]["devices"])
        except (KeyError, TypeError) as error:
            raise RestError(f"malformed mower list: {error!r}") from error
        if not mowers:
            raise RestError("this Navimow account has no mowers")
        return mowers

    async def _fetch_credentials(self, token: str) -> BrokerCredentials:
        answer = await self._call("GET", "/openapi/mqtt/userInfo/get/v2", token)
        try:
            data = answer["data"]
            values = [data[key] for key in ("mqttHost", "mqttUrl", "userName", "pwdInfo")]
        except (KeyError, TypeError) as error:
            raise RestError(f"malformed broker credentials: {error!r}") from error
        if not all(isinstance(value, str) and value for value in values):
            raise RestError("malformed broker credentials")
        host, ws_path, username, password = values
        return BrokerCredentials(host, ws_path, username, password, token)

    async def _call(
        self, method: str, endpoint: str, token: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One vendor REST call, returning the whole of a successful answer."""
        headers = {"Authorization": f"Bearer {token}", "requestId": str(uuid.uuid4())}
        try:
            request = self._session.request(
                method, f"{API_URL}{endpoint}", json=body, headers=headers
            )
            async with request as response:
                text = await response.text()
        except Exception as error:  # Transport errors, whatever their type, are transient.
            raise RestError(str(error)) from error
        if response.status >= 400:
            raise RestError(_prose(f"HTTP {response.status}: {text}", token), response.status)
        try:
            answer = json.loads(text)
        except ValueError:
            answer = None
        if not isinstance(answer, dict) or answer.get("code") != 1:
            # The vendor reports errors in prose, often with status 200.
            desc = answer.get("desc") if isinstance(answer, dict) else None
            raise RestError(_prose(str(desc or text), token))
        return answer


def _prose(text: str, token: str) -> str:
    """Vendor error text is logged: keep it short and free of the token it was sent."""
    return text.replace(token, "<redacted>").rstrip(": ")[:300]


def _read_marker(path: Path) -> float | None:
    try:
        return float(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse(payload: bytes) -> object:
    """The payload as the capture tool records it: parsed JSON, or the raw text."""
    text = payload.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text
