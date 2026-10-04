"""The operations surface: what an operator, a health checker and a scraper are told.

A `Snapshot` is read from the live objects on request and rendered here, so everything
below is testable without a broker, a database or a socket.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .auth import AuthState

# How long the collection loop may go without completing a tick before it counts as stuck.
# A tick completes every ten seconds or less. A hung vendor call cannot hold one past
# `live.VENDOR_WAIT_SECONDS`, because that work carries on in the background, and each
# database wait is capped at 5 s. So only a loop that has stopped stays silent this long.
STALL_SECONDS = 120
PROMETHEUS_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_PREFIX = "navimow_collector_"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    """Every signal the collector reports, as it stood at one moment."""

    seconds_since_tick: float
    broker_connected: bool
    # Per discovered mower: seconds since its last broker message, None until the first.
    last_message_ages: Mapping[str, float | None]
    database_reachable: bool
    buffered_rows: int
    auth_state: AuthState
    login_command: str
    rows_written: int = 0
    rows_dropped: int = 0
    rows_rejected: int = 0

    @property
    def live(self) -> bool:
        return self.seconds_since_tick <= STALL_SECONDS

    @property
    def reauth_required(self) -> bool:
        return self.auth_state is AuthState.RELOGIN_REQUIRED


Probe = Callable[[], Snapshot]


def status_code(snapshot: Snapshot) -> int:
    """503 only when the collection loop has stopped: the one fault a restart can fix.

    A broker that is down, a database that is away and a login that needs renewing are
    reported, never failed: reconnection and the buffer already deal with the first two,
    and restarting over a credential problem would only lose what is buffered in memory.
    """
    return 200 if snapshot.live else 503


def to_json(snapshot: Snapshot) -> str:
    return json.dumps(
        {
            "status": "live" if snapshot.live else "stalled",
            "seconds_since_tick": snapshot.seconds_since_tick,
            "broker": {"connected": snapshot.broker_connected},
            "mowers": {
                mower: {"last_message_age_seconds": age}
                for mower, age in snapshot.last_message_ages.items()
            },
            "database": {
                "reachable": snapshot.database_reachable,
                "buffered_rows": snapshot.buffered_rows,
                "rows_written": snapshot.rows_written,
                "rows_dropped": snapshot.rows_dropped,
                "rows_rejected": snapshot.rows_rejected,
            },
            "auth": {
                "state": snapshot.auth_state.value,
                "reauth_required": snapshot.reauth_required,
                "login_command": snapshot.login_command,
            },
        }
    )


def to_prometheus(snapshot: Snapshot) -> str:
    """The Prometheus text exposition format, version 0.0.4."""
    return "".join(
        [
            _metric(
                "seconds_since_tick",
                "gauge",
                "Seconds since the collection loop last completed a tick.",
                [("", snapshot.seconds_since_tick)],
            ),
            _metric(
                "broker_connected",
                "gauge",
                "Whether the broker connection is up.",
                [("", snapshot.broker_connected)],
            ),
            _metric(
                "last_message_age_seconds",
                "gauge",
                "Seconds since the last broker message from each mower; NaN until the first.",
                [
                    (_labels(mower_id=mower), age)
                    for mower, age in snapshot.last_message_ages.items()
                ],
            ),
            _metric(
                "database_reachable",
                "gauge",
                "Whether the database answered the last attempt to reach it.",
                [("", snapshot.database_reachable)],
            ),
            _metric(
                "buffered_rows",
                "gauge",
                "Rows waiting for the database.",
                [("", snapshot.buffered_rows)],
            ),
            _metric(
                "reauth_required",
                "gauge",
                "Whether only `navimow-collector login` can restore Navimow access.",
                [("", snapshot.reauth_required)],
            ),
            _metric(
                "auth_state",
                "gauge",
                "The Navimow authentication state, one-hot.",
                [(_labels(state=state.value), state is snapshot.auth_state) for state in AuthState],
            ),
            _metric(
                "rows_written_total",
                "counter",
                "Rows the database stored.",
                [("", snapshot.rows_written)],
            ),
            _metric(
                "rows_dropped_total",
                "counter",
                "Rows lost because no buffer could hold them.",
                [("", snapshot.rows_dropped)],
            ),
            _metric(
                "rows_rejected_total",
                "counter",
                "Rows the database refused for what they hold.",
                [("", snapshot.rows_rejected)],
            ),
        ]
    )


def _metric(
    name: str, kind: str, help_text: str, samples: Iterable[tuple[str, float | None]]
) -> str:
    name = _PREFIX + name
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    lines += [f"{name}{labels} {_number(value)}" for labels, value in samples]
    return "\n".join(lines) + "\n"


def _labels(**labels: str) -> str:
    pairs = (f'{key}="{_escape(value)}"' for key, value in labels.items())
    return "{" + ",".join(pairs) + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float | None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "NaN"
    if isinstance(value, bool | int):
        return str(int(value))
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(value)


class HealthServer:
    """`GET /health` and `GET /metrics`, answered from a thread of their own.

    A thread rather than the collector's event loop: a loop that has stalled, the one fault
    worth a restart, could not answer a check served from it, and a checker's timeout says
    less than a 503 with the full report. The probe only reads values the loop writes, each
    a single attribute or a copied dict, so no lock is needed.
    """

    def __init__(self, probe: Probe, host: str, port: int) -> None:
        """Bind now, so an address that cannot be had fails startup; OSError if so."""
        self._server = _Server((host, port), probe)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health", daemon=True
        )

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def __enter__(self) -> HealthServer:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], probe: Probe) -> None:
        self.probe = probe
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)

    def handle_error(self, request: object, client_address: object) -> None:
        _LOGGER.warning("Health request from %s failed", client_address, exc_info=True)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._answer(with_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._answer(with_body=False)

    def _answer(self, *, with_body: bool) -> None:
        path = urlsplit(self.path).path
        if path not in ("/health", "/metrics"):
            self._send(404, "text/plain; charset=utf-8", "not found\n", with_body)
            return
        try:
            snapshot = self.server.probe()
        except Exception:
            _LOGGER.exception("Could not read the collector's health")
            self._send(500, "text/plain; charset=utf-8", "health unavailable\n", with_body)
            return
        if path == "/health":
            self._send(status_code(snapshot), "application/json", to_json(snapshot), with_body)
        else:
            self._send(200, PROMETHEUS_TYPE, to_prometheus(snapshot), with_body)

    def _send(self, status: int, content_type: str, body: str, with_body: bool) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if with_body:
            self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return None  # a checker every few seconds would drown the log
