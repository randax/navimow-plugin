"""Live collection as an operator observes it: what a mower sends becomes rows, unattended.

The broker and the vendor's REST API are faked at the transport's two seams (the broker
connection and the SDK's HTTP session); the clock is injected and the database is real.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import signal
import socket
import threading
import time
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from mower_sdk.http import HTTPClientError

from navimow_collector import live as live_module
from navimow_collector.auth import (
    RETRY_DELAYS,
    AuthState,
    Credential,
    TokenClient,
    TokenManager,
)
from navimow_collector.cli import main
from navimow_collector.health import STALL_SECONDS, Snapshot, status_code
from navimow_collector.live import (
    KEEPALIVE_SECONDS,
    TICK_SECONDS,
    Broker,
    BrokerCredentials,
    Collector,
    connect_broker,
)
from navimow_collector.storage import postgres
from navimow_collector.storage.buffered import (
    REPLAY_ROWS,
    RETRY_SECONDS,
    WRITE_SECONDS,
    BackgroundStorage,
    BufferedStorage,
)

from .conftest import FIXTURE, Clock, free_port, gaps
from .test_auth import Request, Response, logged_in, token
from .test_buffer import Database, points

NOW = 1_790_000_000.0
LOCATION = "/downlink/vehicle/{}/realtimeDate/location"
STATE = "/downlink/vehicle/{}/realtimeDate/state"
CHANNELS = ("event", "attributes")  # the two which carry nothing that is stored
CREDENTIALS = "mqtt/userInfo/get/v2"
STATUS = "smarthome/getVehicleStatus"
# Vendor prose gathered by the token research (docs/research/token-flow.md, section 5).
TOO_FREQUENT = "Request too frequent. Please retry after 1 minute."


class Vendor:
    """Navimow's REST API behind the SDK's HTTP session interface."""

    closed = False

    def __init__(self, *mowers: str) -> None:
        self.mowers = mowers or ("DEVICE_1",)
        self.token = "access"
        self.broker_host = "mqtt.example"
        self.broker_password: str | None = None  # by default, derived from the token
        self.state = "isDocked"  # what a status poll says of every mower
        self.offline = False
        self.clock: Clock | None = None
        self.takes: dict[str, float] = {}  # seconds an endpoint takes to answer
        self.asked_at: dict[str, list[float]] = {}
        self.calls: list[str] = []
        self.failing: dict[str, Response] = {}

    def request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Request:
        host, _, endpoint = url.partition("/openapi/")
        assert host == "https://navimow-fra.ninebot.com"
        self.calls.append(endpoint)
        if self.clock:
            self.asked_at.setdefault(endpoint, []).append(self.clock.now)
            self.clock.now += self.takes.get(endpoint, 0)
        if self.offline:
            raise HTTPClientError("<urlopen error [Errno 8] nodename nor servname provided>")
        if endpoint in self.failing:
            return Request(self.failing[endpoint])
        if endpoint == "oauth/getAccessToken":
            self.token = f"access-{self.calls.count(endpoint)}"
            return Request(Response(200, token(self.token)))
        if (headers or {}).get("Authorization") != f"Bearer {self.token}":
            return Request(Response(401, "Unauthorized"))
        return Request(Response(200, _json(self._answer(method, endpoint, json))))

    def _answer(self, method: str, endpoint: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if (method, endpoint) == ("GET", "smarthome/authList"):
            described = {"model": "X420", "firmware": "005D"}
            devices = [
                {"id": mower, "name": f"Mower {mower}", **described} for mower in self.mowers
            ]
            return {"payload": {"devices": devices}}
        if (method, endpoint) == ("GET", CREDENTIALS):
            return {
                "mqttHost": self.broker_host,
                "mqttUrl": "/mqtt",
                "userName": "user",
                "pwdInfo": self.broker_password or f"password-for-{self.token}",
            }
        assert (method, endpoint) == ("POST", "smarthome/getVehicleStatus")
        assert body == {"devices": [{"id": mower} for mower in self.mowers]}
        states = [{"id": mower, "vehicleState": self.state} for mower in self.mowers]
        return {"payload": {"devices": states}}

    def count(self, endpoint: str) -> int:
        return self.calls.count(endpoint)


def _json(data: dict[str, Any]) -> str:
    return json.dumps({"code": 1, "desc": "Operation successful", "data": data})


class FakeBroker:
    """The broker connection: a test plays the network by accepting, dropping and delivering."""

    on_connected: Callable[[], Awaitable[None]] | None = None
    on_disconnected: Callable[[], Awaitable[None]] | None = None
    on_raw: Callable[[str, bytes], Awaitable[None]] | None = None

    def __init__(
        self,
        credentials: BrokerCredentials,
        mowers: Sequence[str],
        written: Callable[[], Awaitable[None]],
    ) -> None:
        self.credentials = credentials
        self.mowers = list(mowers)
        self.connecting = False
        self.is_connected = False
        # The collector writes off its loop: a test waits here for what its message became.
        self.written = written

    def connect_async(self) -> None:
        self.connecting = True

    def disconnect(self) -> None:
        self.connecting = self.is_connected = False

    def update_credentials(
        self,
        username: str | None = None,
        password: str | None = None,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        assert username and password and auth_headers
        bearer = auth_headers["Authorization"].removeprefix("Bearer ")
        self.credentials = replace(
            self.credentials, username=username, password=password, access_token=bearer
        )

    async def accept(self) -> None:
        assert self.connecting and self.on_connected
        self.is_connected = True
        await self.on_connected()
        await self.written()

    async def drop(self) -> None:
        assert self.on_disconnected
        self.is_connected = False
        await self.on_disconnected()

    async def deliver(self, topic: str, payload: Any, *, written: bool = True) -> None:
        assert self.on_raw
        await self.on_raw(topic, json.dumps(payload).encode())
        if written:
            await self.written()


class Live:
    """One installation: its state directory, database, vendor account and clock."""

    def __init__(self, database: str, tmp_path: Path, *mowers: str) -> None:
        self.clock = Clock(NOW)
        self.vendor = Vendor(*mowers)
        self.vendor.clock = self.clock
        self.db = Database(database)
        self.state = tmp_path / "state"
        self.store = logged_in(self.state, at=NOW)
        self.brokers: list[FakeBroker] = []

    def start(
        self,
        connect: Callable[[BrokerCredentials, Sequence[str]], Broker] | None = None,
        deadline: float = WRITE_SECONDS,
    ) -> Collector:
        """A new collector process over whatever state the last one left behind."""
        self.storage = BackgroundStorage(
            self.db.open, self.state / "buffer.jsonl", clock=self.clock, deadline=deadline
        )
        self.storage.connect()
        tokens = TokenManager(
            TokenClient(self.vendor, "id", "secret"), self.store, clock=self.clock
        )
        self.collector = Collector(
            self.vendor,
            tokens,
            self.storage,
            self.state,
            connect=connect or self._connect,
            clock=self.clock,
        )
        return self.collector

    def _connect(self, credentials: BrokerCredentials, mowers: Sequence[str]) -> FakeBroker:
        self.brokers.append(FakeBroker(credentials, mowers, self.collector.written))
        return self.broker

    @property
    def broker(self) -> FakeBroker:
        return self.brokers[-1]

    async def connected(self) -> Collector:
        collector = self.start()
        await collector.tick()
        await self.broker.accept()
        return collector

    def trail(self) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.dsn) as conn:
            return conn.execute(
                "SELECT mower_id, device_time, received_time, x, y, theta, vehicle_state,"
                " job_id, zone FROM trail_point ORDER BY mower_id, device_time"
            ).fetchall()

    def jobs(self) -> dict[str, list[tuple[Any, ...]]]:
        """Every Job, and what was recorded of each besides its Trail."""
        tables = {"job": "start_time", "job_progress": "device_time", "mower_state": "device_time"}
        with psycopg.connect(self.db.dsn) as conn:
            return {
                table: conn.execute(f"SELECT * FROM {table} ORDER BY mower_id, {key}").fetchall()
                for table, key in tables.items()
            }


@pytest.fixture
def live(database: str, tmp_path: Path) -> Live:
    return Live(database, tmp_path)


def pose(time: datetime, x: float = 1.0) -> list[dict[str, Any]]:
    """One location message as the real mower sends it: an array, numbers as strings."""
    return [
        {
            "postureTheta": "0.5",
            "postureX": str(x),
            "postureY": "2.0",
            "time": int(time.timestamp() * 1000),
            "type": 1,
            "vehicleState": 4,
        }
    ]


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


@pytest.mark.parametrize(
    "fixture", [FIXTURE, FIXTURE.with_name("job-2026-09-30.jsonl.gz")], ids=["synthetic", "real"]
)
def test_live_messages_produce_exactly_the_rows_replay_produces(
    live: Live, config_file: Path, fixture: Path
) -> None:
    assert main(["--config", str(config_file), "replay", str(fixture)]) == 0
    replayed, jobs = live.trail(), live.jobs()
    with psycopg.connect(live.db.dsn) as conn:
        conn.execute("TRUNCATE trail_point, job, job_progress, mower_state")

    async def scenario() -> None:
        await live.connected()
        with gzip.open(fixture, "rt", encoding="utf-8") as capture:
            for record in map(json.loads, capture):
                if record["kind"] == "mqtt":
                    live.clock.now = record["recv_ms"] / 1000
                    await live.broker.deliver(record["topic"], record["payload"])

    asyncio.run(scenario())

    assert len(replayed) > 1000
    assert live.trail() == replayed
    assert len(jobs["job"]) == 1 and len(jobs["job_progress"]) > 30
    assert live.jobs() == jobs


def test_one_process_records_every_mower_on_the_account(database: str, tmp_path: Path) -> None:
    live = Live(database, tmp_path, "DEVICE_1", "DEVICE_2")

    async def scenario() -> None:
        await live.connected()
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW), x=1.0))
        await live.broker.deliver(LOCATION.format("DEVICE_2"), pose(at(NOW), x=7.0))

    asyncio.run(scenario())

    assert live.broker.mowers == ["DEVICE_1", "DEVICE_2"]
    assert [(row[0], row[3]) for row in live.trail()] == [("DEVICE_1", 1.0), ("DEVICE_2", 7.0)]


def test_every_mower_is_recorded_as_the_account_describes_it(database: str, tmp_path: Path) -> None:
    live = Live(database, tmp_path, "DEVICE_1", "DEVICE_2")

    asyncio.run(live.connected())

    with psycopg.connect(live.db.dsn) as conn:
        mowers = conn.execute("SELECT * FROM mower ORDER BY mower_id").fetchall()
    assert mowers == [
        ("DEVICE_1", "Mower DEVICE_1", "X420", "005D", at(NOW)),
        ("DEVICE_2", "Mower DEVICE_2", "X420", "005D", at(NOW)),
    ]


def test_a_reconnection_writes_a_gap_with_its_start_end_and_reason(
    database: str, tmp_path: Path
) -> None:
    live = Live(database, tmp_path, "DEVICE_1", "DEVICE_2")

    async def scenario() -> None:
        await live.connected()
        live.clock.now = NOW + 100
        await live.broker.drop()
        live.clock.now = NOW + 340
        await live.broker.accept()

    asyncio.run(scenario())

    assert gaps(live.db.dsn) == [
        ("DEVICE_1", at(NOW + 100), at(NOW + 340), "reconnect"),
        ("DEVICE_2", at(NOW + 100), at(NOW + 340), "reconnect"),
    ]


def test_a_gap_starts_when_the_connection_was_lost_not_at_a_failed_attempt_to_regain_it(
    live: Live,
) -> None:
    async def scenario() -> None:
        await live.connected()
        live.clock.now = NOW + 100
        await live.broker.drop()
        live.clock.now = NOW + 130
        await live.broker.drop()  # the broker refused a reconnection
        live.clock.now = NOW + 340
        await live.broker.accept()

    asyncio.run(scenario())

    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 100), at(NOW + 340), "reconnect")]


def test_a_reconnection_is_recorded_even_when_no_time_is_seen_to_pass(live: Live) -> None:
    # A stalled process sees the drop and the reconnection in the same instant.
    async def scenario() -> None:
        await live.connected()
        live.clock.now = NOW + 100
        await live.broker.drop()
        await live.broker.accept()

    asyncio.run(scenario())

    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 100), at(NOW + 100), "reconnect")]


def test_a_crash_while_recording_a_gap_does_not_lose_the_outage(live: Live) -> None:
    class PowerCut(BaseException):
        pass

    async def scenario() -> None:
        await live.connected()
        live.clock.now = NOW + 100
        await live.broker.drop()
        live.clock.now = NOW + 340
        live.db.crash = PowerCut()
        with pytest.raises(PowerCut):
            await live.broker.accept()

        live.db.crash = None
        live.clock.now = NOW + 3600
        await live.connected()

    asyncio.run(scenario())

    # The gap was in the buffer file before its start was forgotten: the next start writes
    # it, and the time from there to itself as a gap of its own.
    assert gaps(live.db.dsn) == [
        ("DEVICE_1", at(NOW + 100), at(NOW + 340), "reconnect"),
        ("DEVICE_1", at(NOW + 340), at(NOW + 3600), "restart"),
    ]


def test_a_restart_writes_a_gap_from_when_the_stream_last_flowed(live: Live) -> None:
    async def scenario() -> None:
        collector = await live.connected()
        live.clock.now = NOW + 600
        await collector.tick()  # still connected ten minutes in, then the process dies
        live.clock.now = NOW + 3600
        await live.connected()

    asyncio.run(scenario())

    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 600), at(NOW + 3600), "restart")]


def report(time: datetime, area: float, percentage: int) -> list[dict[str, Any]]:
    """One progress message as the real mower sends it while mowing Zone 11."""
    return [
        {
            "action": 8,
            "currentMowBoundary": 11,
            "currentMowProgress": 100 * percentage,
            "mowStartType": 1,
            "mowingPercentage": percentage,
            "mowingWeekArea": str(654.3 + area),
            "subAction": 6,
            "subtotalArea": str(area),
            "time": int(time.timestamp() * 1000),
            "type": 2,
        }
    ]


def test_a_restart_inside_a_job_does_not_split_it(live: Live) -> None:
    location, state = LOCATION.format("DEVICE_1"), STATE.format("DEVICE_1")

    async def scenario() -> None:
        collector = await live.connected()
        await live.broker.deliver(state, {"state": "isRunning", "battery": 90})
        live.clock.now = NOW + 600
        await live.broker.deliver(location, report(at(NOW + 600), area=40.0, percentage=6))
        collector.stop()
        # The mower mows on while the collector is away, and the status poll on its return
        # fails: nothing but the stored Job says the mower is out on one.
        live.vendor.failing[STATUS] = Response(200, json.dumps({"code": 0, "desc": TOO_FREQUENT}))
        live.clock.now = NOW + 900
        await live.connected()
        live.clock.now = NOW + 1000
        await live.broker.deliver(location, pose(at(NOW + 1000)))
        live.clock.now = NOW + 1200
        await live.broker.deliver(location, report(at(NOW + 1200), area=80.0, percentage=12))

    asyncio.run(scenario())

    with psycopg.connect(live.db.dsn) as conn:
        jobs = conn.execute("SELECT job_id, start_time, end_time, area FROM job").fetchall()
    assert jobs == [("2026-09-21T14:13:20Z", at(NOW), None, 80.0)]
    # A position that arrives before anything else is said of the Job belongs to it too.
    assert [row[7] for row in live.trail()] == ["2026-09-21T14:13:20Z"]


def test_a_report_from_before_a_restart_delivered_after_it_decides_nothing(live: Live) -> None:
    location, state = LOCATION.format("DEVICE_1"), STATE.format("DEVICE_1")

    async def scenario() -> None:
        collector = await live.connected()
        await live.broker.deliver(state, {"state": "isRunning", "battery": 90})
        live.clock.now = NOW + 600
        await live.broker.deliver(location, report(at(NOW + 600), area=40.0, percentage=6))
        collector.stop()
        live.vendor.state = "isRunning"
        live.clock.now = NOW + 900
        await live.connected()
        # Sent five minutes before the report already stored: its smaller area is not a
        # new Job's, though the new process never heard the later one itself.
        await live.broker.deliver(location, report(at(NOW + 300), area=20.0, percentage=3))

    asyncio.run(scenario())

    with psycopg.connect(live.db.dsn) as conn:
        jobs = conn.execute("SELECT start_time, end_time, area FROM job").fetchall()
    assert jobs == [(at(NOW), None, 40.0)]


def test_a_report_delivered_again_after_a_restart_keeps_the_job_it_was_stored_with(
    live: Live,
) -> None:
    location, state = LOCATION.format("DEVICE_1"), STATE.format("DEVICE_1")

    async def scenario() -> None:
        collector = await live.connected()
        await live.broker.deliver(state, {"state": "isRunning", "battery": 90})
        live.clock.now = NOW + 600
        await live.broker.deliver(location, report(at(NOW + 600), area=40.0, percentage=6))
        live.clock.now = NOW + 700
        await live.broker.deliver(state, {"state": "isDocked", "battery": 15})
        collector.stop()
        live.clock.now = NOW + 900
        await live.connected()
        await live.broker.deliver(location, report(at(NOW + 600), area=40.0, percentage=6))

    asyncio.run(scenario())

    with psycopg.connect(live.db.dsn) as conn:
        reports = conn.execute("SELECT device_time, job_id FROM job_progress").fetchall()
    assert reports == [(at(NOW + 600), "2026-09-21T14:13:20Z")]


def test_a_clean_shutdown_ends_the_recorded_stream_at_the_shutdown(live: Live) -> None:
    async def scenario() -> None:
        collector = await live.connected()
        live.clock.now = NOW + 45
        collector.stop()
        live.clock.now = NOW + 3600
        await live.connected()

    asyncio.run(scenario())

    assert not live.brokers[0].connecting
    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 45), at(NOW + 3600), "restart")]


def test_a_restart_while_disconnected_keeps_the_start_of_the_gap(live: Live) -> None:
    async def scenario() -> None:
        collector = await live.connected()
        live.clock.now = NOW + 100
        await live.broker.drop()
        live.clock.now = NOW + 200
        await collector.tick()
        collector.stop()
        live.clock.now = NOW + 3600
        await live.connected()

    asyncio.run(scenario())

    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 100), at(NOW + 3600), "restart")]


def test_a_first_start_has_no_gap_to_record(live: Live) -> None:
    asyncio.run(live.connected())

    assert gaps(live.db.dsn) == []


def test_every_connection_polls_status_once(live: Live) -> None:
    async def scenario() -> None:
        await live.connected()
        assert live.vendor.count("smarthome/getVehicleStatus") == 1
        await live.broker.drop()
        live.clock.now = NOW + 30
        await live.broker.accept()

    asyncio.run(scenario())

    assert live.vendor.count("smarthome/getVehicleStatus") == 2


def test_reconnections_reuse_the_cached_broker_credentials(live: Live) -> None:
    async def scenario() -> None:
        collector = await live.connected()
        for attempt in range(1, 6):
            live.clock.now = NOW + attempt * 20
            await live.broker.drop()
            await collector.tick()
            live.clock.now += 5
            await live.broker.accept()
            await collector.tick()

    asyncio.run(scenario())

    assert len(gaps(live.db.dsn)) == 5
    assert live.vendor.count(CREDENTIALS) == 1
    assert len(live.brokers) == 1


def test_a_rate_limited_credential_fetch_is_not_repeated_within_a_minute(live: Live) -> None:
    live.vendor.failing[CREDENTIALS] = Response(200, json.dumps({"code": 0, "desc": TOO_FREQUENT}))

    async def scenario() -> None:
        collector = live.start()
        for second in range(60):
            live.clock.now = NOW + second
            await collector.tick()
        assert live.vendor.count(CREDENTIALS) == 1

        del live.vendor.failing[CREDENTIALS]
        live.clock.now = NOW + 60
        await collector.tick()
        await live.broker.accept()
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW + 60)))

    asyncio.run(scenario())

    assert live.vendor.count(CREDENTIALS) == 2
    assert len(live.trail()) == 1


def test_a_credential_fetch_that_keeps_failing_backs_off_further_each_time(live: Live) -> None:
    live.vendor.failing[CREDENTIALS] = Response(200, json.dumps({"code": 0, "desc": TOO_FREQUENT}))
    attempts: list[float] = []

    async def scenario() -> None:
        collector = live.start()
        for second in range(0, 3000, 10):
            live.clock.now = NOW + second
            before = live.vendor.count(CREDENTIALS)
            await collector.tick()
            if live.vendor.count(CREDENTIALS) > before:
                attempts.append(second)

    asyncio.run(scenario())

    assert RETRY_DELAYS[:3] == (60, 300, 900)
    assert attempts == [0, 60, 360, 1260]
    assert live.vendor.count("smarthome/authList") == 1  # the mowers were already known


def test_a_new_login_is_tried_within_a_minute_however_far_the_back_off_had_grown(
    live: Live,
) -> None:
    live.vendor.failing[CREDENTIALS] = Response(200, json.dumps({"code": 0, "desc": TOO_FREQUENT}))

    async def scenario() -> None:
        collector = live.start()
        for second in range(0, 400, 10):  # attempts at 0, 60 and 360; the next is due at 1260
            live.clock.now = NOW + second
            await collector.tick()
        assert live.vendor.count(CREDENTIALS) == 3

        del live.vendor.failing[CREDENTIALS]
        live.vendor.token = "from-a-new-login"
        live.store.save(Credential("from-a-new-login", "refresh", 3600, NOW + 400))
        live.clock.now = NOW + 410
        await collector.tick()
        assert live.brokers == []  # still inside the minute since the last attempt

        live.clock.now = NOW + 420
        await collector.tick()
        await live.broker.accept()

    asyncio.run(scenario())

    assert live.broker.credentials.access_token == "from-a-new-login"


def test_a_rotated_access_token_brings_the_broker_new_credentials_without_a_reconnect(
    live: Live,
) -> None:
    async def scenario() -> Collector:
        collector = await live.connected()
        live.clock.now = NOW + 3300  # the hourly token refresh falls due
        await collector.tick()
        return collector

    collector = asyncio.run(scenario())

    assert live.broker.credentials.access_token == "access-1"
    assert live.broker.credentials.password == "password-for-access-1"
    assert collector.connected and len(live.brokers) == 1 and gaps(live.db.dsn) == []


def test_a_connection_that_stays_down_is_given_fresh_credentials(live: Live) -> None:
    async def scenario() -> None:
        collector = await live.connected()
        live.clock.now = NOW + 100
        await live.broker.drop()
        live.vendor.broker_password = "reissued"  # the cached ones no longer open the broker
        live.clock.now = NOW + 130
        await collector.tick()
        assert live.vendor.count(CREDENTIALS) == 1  # a brief drop reconnects on the cached ones

        live.clock.now = NOW + 160
        await collector.tick()
        assert live.broker.credentials.password == "reissued"
        await live.broker.accept()
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW + 161)))

    asyncio.run(scenario())

    assert live.vendor.count(CREDENTIALS) == 2
    assert len(live.trail()) == 1
    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 100), at(NOW + 160), "reconnect")]


def test_a_broker_that_moved_is_followed(live: Live) -> None:
    async def scenario() -> None:
        collector = await live.connected()
        live.clock.now = NOW + 100
        await live.broker.drop()
        live.vendor.broker_host = "mqtt-2.example"
        live.clock.now = NOW + 160
        await collector.tick()
        await live.broker.accept()

    asyncio.run(scenario())

    assert [broker.credentials.host for broker in live.brokers] == [
        "mqtt.example",
        "mqtt-2.example",
    ]
    assert [broker.connecting for broker in live.brokers] == [False, True]
    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 100), at(NOW + 160), "reconnect")]


def test_a_working_connection_is_kept_when_the_broker_moves_and_followed_once_it_drops(
    live: Live,
) -> None:
    async def scenario() -> None:
        collector = await live.connected()
        live.vendor.broker_host = "mqtt-2.example"
        live.clock.now = NOW + 3300  # the hourly token refresh fetches credentials again
        await collector.tick()
        assert collector.connected and len(live.brokers) == 1

        live.clock.now = NOW + 3400
        await live.broker.drop()
        live.clock.now = NOW + 3460
        await collector.tick()
        await live.broker.accept()

    asyncio.run(scenario())

    assert live.broker.credentials.host == "mqtt-2.example"
    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 3400), at(NOW + 3460), "reconnect")]


def test_a_retired_broker_connection_cannot_speak_for_its_replacement(live: Live) -> None:
    async def scenario() -> Collector:
        collector = await live.connected()
        live.clock.now = NOW + 100
        await live.broker.drop()
        retired = live.broker
        live.vendor.broker_host = "mqtt-2.example"
        live.clock.now = NOW + 160
        await collector.tick()
        assert live.broker is not retired and retired.on_connected and retired.on_raw

        # Callbacks the SDK had queued before the connection was retired still run. The
        # collector never saw that connection, so nothing from it is recorded: its
        # position lies inside the gap the replacement's connection closes.
        position = json.dumps(pose(at(NOW + 150))).encode()
        await retired.on_connected()
        await retired.on_raw(LOCATION.format("DEVICE_1"), position)
        assert not collector.connected
        assert live.vendor.count("smarthome/getVehicleStatus") == 1

        live.clock.now = NOW + 200
        await live.broker.accept()
        return collector

    collector = asyncio.run(scenario())

    assert collector.connected
    assert live.trail() == []
    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 100), at(NOW + 200), "reconnect")]


def test_nothing_is_recorded_once_the_collector_has_stopped(live: Live) -> None:
    async def scenario() -> None:
        collector = await live.connected()
        live.clock.now = NOW + 45
        collector.stop()
        live.clock.now = NOW + 50
        assert live.broker.on_connected and live.broker.on_raw
        await live.broker.on_raw(LOCATION.format("DEVICE_1"), json.dumps(pose(at(NOW))).encode())
        await live.broker.on_connected()
        assert not collector.connected

        live.clock.now = NOW + 3600
        await live.connected()

    asyncio.run(scenario())

    assert live.trail() == []
    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 45), at(NOW + 3600), "restart")]


def test_a_slow_mower_discovery_does_not_shorten_the_wait_between_credential_fetches(
    live: Live,
) -> None:
    live.vendor.takes["smarthome/authList"] = 25
    live.vendor.failing[CREDENTIALS] = Response(200, json.dumps({"code": 0, "desc": TOO_FREQUENT}))

    async def scenario() -> None:
        collector = live.start()
        for _ in range(12):
            await collector.tick()
            live.clock.now += 10

    asyncio.run(scenario())

    first, second = live.vendor.asked_at[CREDENTIALS][:2]
    assert first == NOW + 25
    assert second - first >= 60


def test_an_access_token_the_vendor_rejects_is_refreshed_and_collection_starts(live: Live) -> None:
    live.vendor.token = "replaced-elsewhere"  # the stored access token now earns a 401

    async def scenario() -> None:
        collector = live.start()
        await collector.tick()
        assert live.brokers == []
        live.clock.now = NOW + 60
        await collector.tick()
        await live.broker.accept()

    asyncio.run(scenario())

    assert live.broker.credentials.access_token == "access-1"


def test_a_network_that_is_down_at_start_is_waited_out(live: Live) -> None:
    live.vendor.offline = True

    async def scenario() -> None:
        collector = live.start()
        await collector.tick()
        live.vendor.offline = False
        live.clock.now = NOW + 60
        await collector.tick()
        await live.broker.accept()
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW + 61)))

    asyncio.run(scenario())

    assert len(live.trail()) == 1


def test_the_collector_waits_for_a_login_instead_of_failing(live: Live) -> None:
    live.store.path.unlink()

    async def scenario() -> None:
        collector = live.start()
        await collector.tick()
        assert live.vendor.calls == [] and live.brokers == []

        live.store.save(Credential("access", "refresh", 3600, NOW))  # `navimow-collector login`
        await collector.tick()
        await live.broker.accept()

    asyncio.run(scenario())

    assert live.broker.connecting


def test_points_survive_a_database_outage_while_live(live: Live) -> None:
    async def scenario() -> None:
        collector = await live.connected()
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW + 1)))
        live.db.go_down()
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW + 3)))
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW + 5)))
        assert len(live.trail()) == 1

        live.db.down = False
        live.clock.now = NOW + RETRY_SECONDS
        await collector.tick()  # no message needed: the database is retried on a timer
        await collector.written()

    asyncio.run(scenario())

    assert [row[1] for row in live.trail()] == [at(NOW + 1), at(NOW + 3), at(NOW + 5)]


async def until(happened: Callable[[], object]) -> None:
    """Let the loop run until something has happened, which it must within a few seconds."""
    async with asyncio.timeout(5):
        while not happened():
            await asyncio.sleep(0.01)


def test_a_database_that_stops_answering_holds_up_neither_collection_nor_a_stop(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A backend suspended mid-statement keeps its connection open and says nothing: neither
    # the statement timeout nor TCP ends the wait for it.
    monkeypatch.setattr(postgres, "STATEMENT_TIMEOUT_MS", 0)
    monkeypatch.setattr(live_module, "STOP_SECONDS", 0.05)
    location = LOCATION.format("DEVICE_1")
    ticks = 0

    async def wait(stop: asyncio.Event, seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        await asyncio.sleep(0.01)

    async def scenario() -> Snapshot:
        stop = asyncio.Event()
        collector = live.start()
        collecting = asyncio.create_task(collector.collect(stop, wait))
        await until(lambda: live.brokers)
        await live.broker.accept()
        with live.db.suspended():
            await live.broker.deliver(location, pose(at(NOW + 1)), written=False)
            await until(live.db.held)
            await live.broker.deliver(location, pose(at(NOW + 3)), written=False)
            live.clock.now = NOW + 3400  # the token falls due for its refresh
            await until(lambda: live.vendor.count("oauth/getAccessToken"))
            ticked = ticks
            await until(lambda: ticks > ticked + 1)
            snapshot = collector.snapshot()
            stop.set()
            async with asyncio.timeout(2):
                await collecting
            live.storage.close()  # as the command does once collection has ended
        return snapshot

    snapshot = asyncio.run(scenario())

    assert (snapshot.buffered_rows, status_code(snapshot)) == (2, 200)
    assert not live.broker.connecting
    asyncio.run(live.connected())  # the next start finds both points waiting on disk
    assert [row[1] for row in live.trail()] == [at(NOW + 1), at(NOW + 3)]


def test_a_write_the_database_never_answers_is_given_up_on_and_made_again(
    live: Live, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(postgres, "STATEMENT_TIMEOUT_MS", 0)

    async def scenario() -> list[Snapshot]:
        collector = live.start(deadline=0.2)
        await collector.tick()
        await live.broker.accept()
        with live.db.suspended():
            async with asyncio.timeout(5):  # the collector's own clock ends the wait
                await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW + 1)))
            seen = [collector.snapshot()]
        live.clock.now = NOW + RETRY_SECONDS
        await collector.tick()
        await collector.written()
        return [*seen, collector.snapshot()]

    given_up, recovered = asyncio.run(scenario())

    assert (given_up.database_reachable, given_up.buffered_rows) == (False, 1)
    assert "no answer within 0.2 seconds" in caplog.text
    assert (recovered.database_reachable, recovered.buffered_rows) == (True, 0)
    assert [row[1] for row in live.trail()] == [at(NOW + 1)]


def test_health_reports_each_mowers_last_message_age(database: str, tmp_path: Path) -> None:
    live = Live(database, tmp_path, "DEVICE_1", "DEVICE_2", "DEVICE_3")

    async def scenario() -> Collector:
        collector = await live.connected()
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW)))
        live.clock.now = NOW + 40
        await live.broker.deliver("/downlink/vehicle/DEVICE_2/realtimeDate/state", {"s": 1})
        live.clock.now = NOW + 65
        return collector

    snapshot = asyncio.run(scenario()).snapshot()

    # DEVICE_3 is on the account but has said nothing yet: no age, rather than a fresh one.
    assert snapshot.last_message_ages == {"DEVICE_1": 65.0, "DEVICE_2": 25.0, "DEVICE_3": None}


def test_health_reports_the_broker_the_database_and_the_buffer(live: Live) -> None:
    async def scenario() -> list[Snapshot]:
        collector = await live.connected()
        seen = [collector.snapshot()]
        live.db.go_down()
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW + 1)))
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW + 2)))
        await live.broker.drop()
        seen.append(collector.snapshot())
        live.db.down = False
        live.clock.now = NOW + RETRY_SECONDS
        await collector.tick()
        await collector.written()
        seen.append(collector.snapshot())
        return seen

    healthy, outage, recovered = asyncio.run(scenario())

    assert (healthy.broker_connected, healthy.database_reachable, healthy.buffered_rows) == (
        True,
        True,
        0,
    )
    assert (outage.broker_connected, outage.database_reachable, outage.buffered_rows) == (
        False,
        False,
        2,
    )
    assert status_code(outage) == 200  # reported, not failed: a restart would not help
    assert (recovered.database_reachable, recovered.buffered_rows) == (True, 0)
    # The mower itself, written on connecting; the backlog counts once it is written.
    assert recovered.rows_written == 1 + 2


def test_a_message_on_a_channel_nothing_stores_is_counted_and_logged(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    # No capture has yet held a message on the event or attributes channels, so nothing is
    # stored of them: the first one a mower sends has to be noticed, and readable.
    event, attributes = (f"/downlink/vehicle/DEVICE_1/realtimeDate/{c}" for c in CHANNELS)

    async def scenario() -> Snapshot:
        collector = await live.connected()
        await live.broker.deliver(event, {"level": "error", "message": "Mower lifted"})
        await live.broker.deliver(event, {"level": "error", "message": "Mower stuck"})
        await live.broker.deliver(attributes, {"signal_strength": -67})
        await live.broker.deliver(LOCATION.format("DEVICE_1"), pose(at(NOW)))
        return collector.snapshot()

    with caplog.at_level(logging.INFO):
        snapshot = asyncio.run(scenario())

    assert snapshot.unstored_messages == {
        ("DEVICE_1", "event"): 2,
        ("DEVICE_1", "attributes"): 1,
    }
    assert "Mower lifted" in caplog.text and "signal_strength" in caplog.text
    assert "Mower stuck" not in caplog.text  # one line a channel says what it carries
    assert len(live.trail()) == 1


def test_health_fails_once_the_collection_loop_stops_ticking(live: Live) -> None:
    collector = live.start()
    asyncio.run(collector.tick())
    live.clock.now = NOW + STALL_SECONDS + 1

    stalled = collector.snapshot()
    asyncio.run(collector.tick())

    assert (stalled.seconds_since_tick, status_code(stalled)) == (STALL_SECONDS + 1, 503)
    assert (collector.snapshot().seconds_since_tick, status_code(collector.snapshot())) == (0, 200)


def test_a_hung_vendor_call_neither_stalls_the_loop_nor_is_abandoned(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick waits only briefly on the vendor, so a hung token refresh cannot fail the health
    check; the refresh carries on meanwhile, since cutting it short could spend the rotated
    refresh token it is about to return."""
    monkeypatch.setattr(live_module, "VENDOR_WAIT_SECONDS", 0.01)
    answered = asyncio.Event()
    asked = live.vendor.request

    class Held(Request):
        async def __aenter__(self) -> Response:
            await answered.wait()
            return self.response

    def request(method: str, url: str, **kwargs: Any) -> Request:
        sent = asked(method, url, **kwargs)
        return Held(sent.response) if url.endswith("oauth/getAccessToken") else sent

    monkeypatch.setattr(live.vendor, "request", request)
    live.clock.now = NOW + 3400  # the token is due for its refresh

    async def scenario() -> tuple[Snapshot, Collector]:
        collector = live.start()
        live.clock.now += STALL_SECONDS + 60
        await collector.tick()  # the refresh hangs; the tick completes all the same
        hung = collector.snapshot()
        assert live.brokers == []
        answered.set()
        await collector.tick()
        return hung, collector

    hung, collector = asyncio.run(scenario())

    assert (hung.seconds_since_tick, status_code(hung)) == (0, 200)
    assert live.brokers  # the refresh finished and collection went on with its token
    stored = live.store.load()
    assert stored is not None and stored.access_token == "access-1"


def test_a_required_login_is_reported_apart_from_liveness(live: Live) -> None:
    live.store.path.unlink()
    collector = live.start()
    asyncio.run(collector.tick())

    snapshot = collector.snapshot()

    assert snapshot.auth_state is AuthState.RELOGIN_REQUIRED
    assert snapshot.reauth_required
    assert status_code(snapshot) == 200  # a credential problem never restarts the collector


def test_collecting_continues_until_stopped_then_disconnects(live: Live) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        collecting = asyncio.create_task(live.start().collect(stop))
        while not live.brokers:
            await asyncio.sleep(0)
        await live.broker.accept()
        live.clock.now = NOW + 45
        stop.set()
        await collecting

        live.clock.now = NOW + 3600
        await live.connected()

    asyncio.run(scenario())

    assert not live.brokers[0].connecting
    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW + 45), at(NOW + 3600), "restart")]


def test_a_backlog_is_drained_promptly_without_holding_the_loop(live: Live) -> None:
    # The mower is written as the collector starts: a write like any other, which takes a
    # slice of the backlog with it.
    backlog = 3 * REPLAY_ROWS + 150
    live.db.go_down()
    outage = BufferedStorage(live.db.open, live.state / "buffer.jsonl", clock=live.clock)
    outage.write_trail(points(*range(backlog)))
    outage.close()  # the collector stopped with the database away
    live.db.down = False
    pauses: list[float] = []

    async def wait(stop: asyncio.Event, seconds: float) -> None:
        pauses.append(seconds)
        await live.collector.written()
        stop.set()

    asyncio.run(live.start().collect(asyncio.Event(), wait))

    # One tick sets the writing off; it carries on a slice at a time, off the loop, with
    # no further tick to wait for.
    assert pauses == [TICK_SECONDS]
    assert len(live.trail()) == backlog


def test_the_broker_connection_keeps_alive_well_inside_the_idle_drop() -> None:
    credentials = BrokerCredentials("wss://mqtt.example", "/mqtt", "user", "password", "access")

    mqtt: Any = connect_broker(credentials, ["DEVICE_1", "DEVICE_2"])

    # The broker drops a connection idle for about ten minutes; the SDK defaults to forty.
    assert mqtt.keepalive_seconds == KEEPALIVE_SECONDS == 60
    assert mqtt.subscribe_location
    assert [record.id for record in mqtt.records] == ["DEVICE_1", "DEVICE_2"]
    assert mqtt.auth_headers == {"Authorization": "Bearer access"}


def undialled(sdk: list[Any]) -> Callable[[BrokerCredentials, Sequence[str]], Broker]:
    """The SDK's own client, never dialled: a test invokes paho's callbacks as paho would."""

    def connect(credentials: BrokerCredentials, mowers: Sequence[str]) -> Broker:
        mqtt: Any = connect_broker(credentials, mowers)
        mqtt.loop = asyncio.get_running_loop()
        mqtt.connect_async = lambda: None
        mqtt.client.is_connected = lambda: True  # as paho says before it calls on_connect
        sdk.append(mqtt)
        broker: Broker = mqtt
        return broker

    return connect


async def settled() -> None:
    for _ in range(20):  # the SDK hands each callback to the loop as its own task
        await asyncio.sleep(0)


def test_the_sdk_connection_drives_the_collector_as_the_fake_broker_does(live: Live) -> None:
    sdk: list[Any] = []

    async def scenario() -> None:
        collector = live.start(undialled(sdk))
        await collector.tick()
        paho = sdk[0].client

        paho.on_connect(paho, None, {}, 0, None)
        await settled()
        assert collector.connected
        assert live.vendor.count("smarthome/getVehicleStatus") == 1

        message = SimpleNamespace(
            topic=LOCATION.format("DEVICE_1"), payload=json.dumps(pose(at(NOW))).encode()
        )
        paho.on_message(paho, None, message)
        await settled()

        paho.on_disconnect(paho, None, {}, 0, None)
        await settled()
        assert not collector.connected
        collector.stop()

    asyncio.run(scenario())

    assert [(row[0], row[1]) for row in live.trail()] == [("DEVICE_1", at(NOW))]


def test_a_connection_the_sdk_tore_down_before_its_callback_ran_does_not_count(
    live: Live,
) -> None:
    sdk: list[Any] = []

    async def scenario() -> Collector:
        collector = live.start(undialled(sdk))
        await collector.tick()
        # paho has the broker's answer but has not yet said it is connected when new
        # credentials make the SDK replace its client; the old client's callback follows.
        paho = sdk[0].client
        paho.is_connected = lambda: False
        sdk[0].update_credentials("user", "reissued")
        assert sdk[0].client is not paho
        paho.on_connect(paho, None, {}, 0, None)
        await settled()
        return collector

    collector = asyncio.run(scenario())

    assert not collector.connected
    assert live.vendor.count("smarthome/getVehicleStatus") == 0


def test_a_connection_that_came_and_went_unseen_still_starts_the_next_gap(live: Live) -> None:
    # A first start, so nothing yet says when the stream last flowed. While the loop is
    # busy the connection is made, delivers a position and drops; the callbacks run after.
    sdk: list[Any] = []
    message = SimpleNamespace(
        topic=LOCATION.format("DEVICE_1"), payload=json.dumps(pose(at(NOW))).encode()
    )

    async def scenario() -> Collector:
        collector = live.start(undialled(sdk))
        await collector.tick()
        paho = sdk[0].client
        paho.on_connect(paho, None, {}, 0, None)
        paho.on_message(paho, None, message)
        paho.is_connected = lambda: False
        paho.on_disconnect(paho, None, {}, 7, None)
        await settled()
        assert not collector.connected

        live.clock.now = NOW + 300
        paho.is_connected = lambda: True
        paho.on_connect(paho, None, {}, 0, None)
        await settled()
        return collector

    collector = asyncio.run(scenario())

    assert collector.connected
    assert [(row[0], row[1]) for row in live.trail()] == [("DEVICE_1", at(NOW))]
    assert gaps(live.db.dsn) == [("DEVICE_1", at(NOW), at(NOW + 300), "reconnect")]


def test_a_broker_address_the_sdk_cannot_use_is_a_failed_fetch_not_a_crash(live: Live) -> None:
    live.vendor.broker_host = "wss://mqtt.example:abc"  # no port the SDK can parse
    sdk: list[Any] = []

    async def scenario() -> None:
        collector = live.start(undialled(sdk))
        await collector.tick()
        live.vendor.broker_host = "wss://mqtt.example"
        live.clock.now = NOW + 30
        await collector.tick()
        assert live.vendor.count(CREDENTIALS) == 1  # retried on the ladder, not at once

        live.clock.now = NOW + 60
        await collector.tick()
        collector.stop()

    asyncio.run(scenario())

    assert live.vendor.count(CREDENTIALS) == 2
    assert [mqtt.broker for mqtt in sdk] == ["mqtt.example"]


def test_an_sdk_callback_still_queued_at_shutdown_is_ignored(live: Live) -> None:
    sdk: list[Any] = []

    async def scenario() -> Collector:
        collector = live.start(undialled(sdk))
        await collector.tick()
        paho = sdk[0].client
        paho.on_connect(paho, None, {}, 0, None)  # queued for the loop, which has not run it
        collector.stop()
        await settled()
        return collector

    collector = asyncio.run(scenario())

    assert not collector.connected
    assert live.vendor.count("smarthome/getVehicleStatus") == 0


def stopped_at_its_first_request(live: Live) -> Callable[[], Vendor]:
    """A session for the `collect` command: the service manager stops the collector as soon
    as it asks the vendor anything, so the command returns and no real broker is dialled."""
    live.vendor.offline = True

    class Terminating(Vendor):
        def request(self, method: str, url: str, **kwargs: Any) -> Request:
            os.kill(os.getpid(), signal.SIGTERM)
            return live.vendor.request(method, url, **kwargs)

    return Terminating


def test_the_collect_command_collects_until_it_is_signalled(
    live: Live, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NAVIMOW_AUTH_STATE_FILE", str(live.store.path))
    monkeypatch.setenv("NAVIMOW_COLLECTOR_STATE_DIR", str(live.state))
    monkeypatch.setenv("NAVIMOW_HEALTH_LISTEN", "127.0.0.1:0")
    session = stopped_at_its_first_request(live)

    assert main(["--config", str(config_file), "collect"], session_factory=session) == 0
    assert "smarthome/authList" in live.vendor.calls  # it got as far as looking for mowers


@pytest.mark.parametrize("named_by", ["option", "environment"])
def test_the_collect_command_serves_health_and_names_the_login_to_run(
    live: Live,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    named_by: str,
) -> None:
    # A systemd unit or container names its configuration in NAVIMOW_CONFIG, which the
    # operator's shell lacks: the login to run must name the file either way.
    argv = ["--config", str(config_file), "collect"]
    if named_by == "environment":
        monkeypatch.setenv("NAVIMOW_CONFIG", str(config_file))
        argv = ["collect"]
    live.store.path.unlink()  # no login yet: the collector waits for one, healthy all along
    port = free_port()
    monkeypatch.setenv("NAVIMOW_AUTH_STATE_FILE", str(live.store.path))
    monkeypatch.setenv("NAVIMOW_COLLECTOR_STATE_DIR", str(live.state))
    monkeypatch.setenv("NAVIMOW_HEALTH_LISTEN", f"127.0.0.1:{port}")
    answers: list[tuple[int, dict[str, Any], str]] = []

    def check_then_stop() -> None:
        """A container health check and a scraper, then the service manager's SIGTERM."""
        deadline = time.monotonic() + 10
        while not answers and time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as health:
                    status, body = health.status, json.load(health)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics") as metrics:
                    answers.append((status, body, metrics.read().decode()))
            except OSError:
                time.sleep(0.05)  # not serving yet
        os.kill(os.getpid(), signal.SIGTERM)

    checker = threading.Thread(target=check_then_stop)
    checker.start()
    assert main(argv, session_factory=lambda: live.vendor) == 0
    checker.join()

    [(status, body, metrics)] = answers
    assert status == 200
    assert body["auth"]["state"] == "relogin-required"
    assert "\nnavimow_collector_reauth_required 1\n" in metrics
    command = f"navimow-collector --config {config_file} login"
    assert body["auth"]["login_command"] == command
    assert any(r.__dict__.get("command") == command for r in caplog.records)


def test_the_collect_command_refuses_to_start_if_health_cannot_listen(
    live: Live,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NAVIMOW_AUTH_STATE_FILE", str(live.store.path))
    monkeypatch.setenv("NAVIMOW_COLLECTOR_STATE_DIR", str(live.state))
    session = stopped_at_its_first_request(live)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        monkeypatch.setenv("NAVIMOW_HEALTH_LISTEN", f"127.0.0.1:{taken.getsockname()[1]}")

        assert main(["--config", str(config_file), "collect"], session_factory=session) == 2

    assert "health.listen" in capsys.readouterr().err
    assert live.vendor.calls == []


def test_the_collect_command_refuses_to_start_without_a_writable_state_directory(
    live: Live,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Unable to note when the stream last flowed, a later restart would record a gap
    # across Trail that was in fact collected.
    occupied = live.state.parent / "occupied"
    occupied.write_text("a file where the state directory should go")
    monkeypatch.setenv("NAVIMOW_AUTH_STATE_FILE", str(live.store.path))
    monkeypatch.setenv("NAVIMOW_COLLECTOR_STATE_DIR", str(occupied / "state"))
    session = stopped_at_its_first_request(live)

    assert main(["--config", str(config_file), "collect"], session_factory=session) == 2
    assert "collector.state_dir" in capsys.readouterr().err
    assert live.vendor.calls == []


def test_the_collect_command_refuses_to_start_without_its_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "collector.toml"
    config.write_text(
        '[storage]\ndsn = "postgresql://nobody@127.0.0.1:1/none"\n'
        f'[collector]\nstate_dir = "{tmp_path / "state"}"\n'
    )

    assert main(["--config", str(config), "collect"]) == 2
    assert "postgres" in capsys.readouterr().err
