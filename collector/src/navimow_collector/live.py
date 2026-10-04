"""Live collection: the transport that feeds a real mower's stream to the ingestion core."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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

from .auth import RETRY_DELAYS, TokenManager, redact, replace_file
from .health import Snapshot
from .ingest import Ingestor
from .records import GapReason
from .storage.buffered import BufferedStorage

API_URL = "https://navimow-fra.ninebot.com"
# The SDK's default of 40 minutes lets the broker drop an idle connection after about ten,
# silently: nothing notices until the mower next has something to say.
KEEPALIVE_SECONDS = 60
# How often a live connection is noted on disk; a restart's gap starts at the last note.
HEARTBEAT_SECONDS = 60
TICK_SECONDS = 10
# The pause between ticks while buffered rows are being written out a slice per tick.
DRAIN_SECONDS = 0.1
# How long a tick waits on the vendor (token refresh, mower discovery, broker credentials)
# before carrying on. The SDK's 30 s timeout applies to each socket operation, not to a
# request, and a tick can make four requests in a row: unbounded, a hung vendor would stop
# the buffer draining and look like a stalled loop. The work is left running, not cancelled:
# a refresh cut short could spend the rotated refresh token it is about to return.
VENDOR_WAIT_SECONDS = 5
# Every topic the broker delivers for a mower, whatever the channel.
MOWER_TOPIC = re.compile(r"^/downlink/vehicle/([^/]+)/")

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

    @property
    def is_connected(self) -> bool: ...

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


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Pause between ticks, cut short by `stop`."""
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


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
        self._gap_reason = GapReason.RESTART
        # For the health endpoint: when the loop last completed a tick (construction counts,
        # so startup is not a stall), and when each mower was last heard from.
        self._ticked_at = clock()
        self._heard: dict[str, float] = {}
        self._vendor_work: asyncio.Future[None] | None = None

    async def collect(
        self,
        stop: asyncio.Event,
        wait: Callable[[asyncio.Event, float], Awaitable[None]] = _wait,
    ) -> None:
        """Collect until `stop` is set."""
        try:
            while not stop.is_set():
                await self.tick()
                # Each tick writes one slice of a backlog, leaving the loop free in between;
                # while rows remain the next tick follows at once.
                await wait(stop, DRAIN_SECONDS if self._storage.draining else TICK_SECONDS)
        finally:
            if self._vendor_work is not None:
                self._vendor_work.cancel()
            self.stop()

    async def tick(self) -> None:
        """Do whatever is due; called every few seconds for the life of the process."""
        work = self._vendor_work
        if work is None or work.done():
            if work is not None:
                work.result()  # what went wrong in the background is raised here, as inline
            work = self._vendor_work = asyncio.ensure_future(self._keep_access())
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(work), VENDOR_WAIT_SECONDS)
        now = self._clock()
        if self.connected and now - (self._flowed_until or 0) >= HEARTBEAT_SECONDS:
            self._remember_flow(now)
        self._storage.flush()
        self._ticked_at = self._clock()

    async def _keep_access(self) -> None:
        """Keep the access token fresh and the broker supplied with credentials for it."""
        token = await self._tokens.access_token()
        if token is not None:
            await self._keep_credentials(token)

    def snapshot(self) -> Snapshot:
        """Every signal the health endpoint reports, read from another thread.

        Only single attributes are read, and the one dict is copied first, so a value may be
        a moment old but is never torn.
        """
        now = self._clock()
        heard = dict(self._heard)
        mowers = dict.fromkeys(self._mowers) | heard
        return Snapshot(
            seconds_since_tick=_age(now, self._ticked_at),
            broker_connected=self.connected,
            last_message_ages={
                mower: None if at is None else _age(now, at) for mower, at in mowers.items()
            },
            database_reachable=self._storage.reachable,
            buffered_rows=self._storage.buffered,
            auth_state=self._tokens.state,
            login_command=self._tokens.login_command,
            rows_written=self._storage.written,
            rows_dropped=self._storage.dropped,
            rows_rejected=self._storage.rejected,
        )

    def stop(self) -> None:
        """Disconnect; the next start records the time from here as a gap."""
        broker, self._broker = self._broker, None
        if broker is not None:
            broker.disconnect()
        if self.connected:
            self.connected = False
            self._remember_flow(self._clock())

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
            self._apply_credentials(credentials, now)
        except RestError as error:
            _LOGGER.warning("Could not fetch broker credentials; will retry: %s", error)
            await self._rejected(error, token)
            return
        if self.connected:
            self._fetches = 0
        self._credentials = credentials

    def _apply_credentials(self, credentials: BrokerCredentials, now: float) -> None:
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
        try:
            broker = self._broker = self._connect(credentials, self._mowers)
        except Exception as error:  # the SDK refuses an address it cannot parse
            raise RestError(f"unusable broker address {credentials.host!r}: {error}") from error
        self._address = address
        broker.on_connected = self._while_current(broker, self._on_connected)
        broker.on_disconnected = self._while_current(broker, self._on_disconnected)
        broker.on_raw = self._while_current(broker, self._on_raw)
        broker.connect_async()
        self._down_since = self._down_since or now

    def _while_current(
        self, broker: Broker, handler: Callable[..., Awaitable[None]]
    ) -> Callable[..., Awaitable[None]]:
        """A connection is heard only while it is the current one. The SDK may already have
        queued a callback when the connection was retired or the collector stopped; its
        connect and drop would then speak for a connection that is not its own, and without
        them a message of its would be a Trail point with no gap recorded around it."""

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
        if self._broker is None:
            return
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
        self._remember_flow(now)
        if not self._broker.is_connected:
            # The connection came and went before this callback ran (a busy loop, or the
            # SDK replacing its client). The stream was seen all the same, so whatever
            # follows is a gap from now; but nothing is connected.
            await self._on_disconnected()
            return
        await self._poll_status()

    async def _on_disconnected(self) -> None:
        if not self.connected:
            return  # a failed attempt to reconnect: the gap is already open
        self.connected = False
        self._gap_reason = GapReason.RECONNECT
        self._down_since = self._clock()
        self._remember_flow(self._down_since)
        _LOGGER.warning("Broker connection lost; reconnecting")

    def _remember_flow(self, now: float) -> None:
        """Note, on disk too, that the stream flowed until `now`."""
        self._flowed_until = now
        try:
            self._marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            replace_file(self._marker, repr(now))
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
        if match := MOWER_TOPIC.match(topic):
            self._heard[match.group(1)] = self._clock()
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
            raise RestError(_loggable(f"HTTP {response.status}: {text}", token), response.status)
        try:
            answer = json.loads(text)
        except ValueError:
            answer = None
        if not isinstance(answer, dict) or answer.get("code") != 1:
            # The vendor reports errors in prose, often with status 200.
            desc = answer.get("desc") if isinstance(answer, dict) else None
            raise RestError(_loggable(str(desc or text), token))
        return answer


def _loggable(text: str, token: str) -> str:
    """Vendor error text is logged: keep it short and free of the token it was sent."""
    return redact(text, [token]).rstrip(": ")[:300]


def _age(now: float, then: float) -> float:
    """Seconds from `then` to `now`, to the millisecond; never negative if the clock is set back."""
    return round(max(now - then, 0.0), 3)


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
