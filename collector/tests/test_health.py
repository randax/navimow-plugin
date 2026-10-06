"""The operations surface as a health checker, a scraper and a log reader see it."""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace

import pytest

from navimow_collector.auth import AuthState
from navimow_collector.config import Secret
from navimow_collector.health import (
    STALL_SECONDS,
    HealthServer,
    Snapshot,
    status_code,
    to_json,
    to_prometheus,
)
from navimow_collector.logs import JsonFormatter

HEALTHY = Snapshot(
    seconds_since_tick=2.5,
    broker_connected=True,
    last_message_ages={"DEVICE_1": 12.0, "DEVICE_2": None},
    database_reachable=True,
    buffered_rows=0,
    auth_state=AuthState.FRESH,
    login_command="navimow-collector login",
    rows_written=1500,
    rows_dropped=3,
    rows_rejected=1,
    unstored_messages={("DEVICE_1", "event"): 4},
)


def test_health_reports_every_signal_including_authentication() -> None:
    assert json.loads(to_json(HEALTHY)) == {
        "status": "live",
        "seconds_since_tick": 2.5,
        "broker": {"connected": True},
        "mowers": {
            "DEVICE_1": {"last_message_age_seconds": 12.0, "unstored_messages": {"event": 4}},
            # Discovered, silent so far.
            "DEVICE_2": {"last_message_age_seconds": None, "unstored_messages": {}},
        },
        "database": {
            "reachable": True,
            "buffered_rows": 0,
            "rows_written": 1500,
            "rows_dropped": 3,
            "rows_rejected": 1,
        },
        "auth": {
            "state": "fresh",
            "reauth_required": False,
            "login_command": "navimow-collector login",
        },
    }


def test_a_stalled_loop_is_reported_as_such() -> None:
    stalled = replace(HEALTHY, seconds_since_tick=STALL_SECONDS + 1)
    assert json.loads(to_json(stalled))["status"] == "stalled"


@pytest.mark.parametrize(
    "trouble",
    [
        {"auth_state": AuthState.RELOGIN_REQUIRED},
        {"auth_state": AuthState.RETRY_PENDING},
        {"database_reachable": False, "buffered_rows": 5000},
        {"broker_connected": False},
        {"last_message_ages": {"DEVICE_1": 86_400.0}},
        {"seconds_since_tick": STALL_SECONDS},
    ],
    ids=["reauth", "token-retry", "database", "broker", "silent-mower", "slow-tick"],
)
def test_only_a_stalled_loop_fails_the_health_check(trouble: dict[str, object]) -> None:
    # A restart fixes none of these: reconnection and the buffer already handle them, and a
    # credential problem must never restart the collector.
    assert status_code(replace(HEALTHY, **trouble)) == 200  # type: ignore[arg-type]


def test_a_loop_that_stopped_ticking_fails_the_health_check() -> None:
    assert status_code(replace(HEALTHY, seconds_since_tick=STALL_SECONDS + 0.5)) == 503


def test_metrics_expose_the_same_signals_in_the_scrape_format() -> None:
    assert to_prometheus(HEALTHY) == (
        "# HELP navimow_collector_seconds_since_tick Seconds since the collection loop last"
        " completed a tick.\n"
        "# TYPE navimow_collector_seconds_since_tick gauge\n"
        "navimow_collector_seconds_since_tick 2.5\n"
        "# HELP navimow_collector_broker_connected Whether the broker connection is up.\n"
        "# TYPE navimow_collector_broker_connected gauge\n"
        "navimow_collector_broker_connected 1\n"
        "# HELP navimow_collector_last_message_age_seconds Seconds since the last broker message"
        " from each mower; NaN until the first.\n"
        "# TYPE navimow_collector_last_message_age_seconds gauge\n"
        'navimow_collector_last_message_age_seconds{mower_id="DEVICE_1"} 12.0\n'
        'navimow_collector_last_message_age_seconds{mower_id="DEVICE_2"} NaN\n'
        "# HELP navimow_collector_database_reachable Whether the database answered the last"
        " attempt to reach it.\n"
        "# TYPE navimow_collector_database_reachable gauge\n"
        "navimow_collector_database_reachable 1\n"
        "# HELP navimow_collector_buffered_rows Rows waiting for the database.\n"
        "# TYPE navimow_collector_buffered_rows gauge\n"
        "navimow_collector_buffered_rows 0\n"
        "# HELP navimow_collector_reauth_required Whether only `navimow-collector login` can"
        " restore Navimow access.\n"
        "# TYPE navimow_collector_reauth_required gauge\n"
        "navimow_collector_reauth_required 0\n"
        "# HELP navimow_collector_auth_state The Navimow authentication state, one-hot.\n"
        "# TYPE navimow_collector_auth_state gauge\n"
        'navimow_collector_auth_state{state="fresh"} 1\n'
        'navimow_collector_auth_state{state="refreshing"} 0\n'
        'navimow_collector_auth_state{state="retry-pending"} 0\n'
        'navimow_collector_auth_state{state="relogin-required"} 0\n'
        "# HELP navimow_collector_rows_written_total Rows the database stored.\n"
        "# TYPE navimow_collector_rows_written_total counter\n"
        "navimow_collector_rows_written_total 1500\n"
        "# HELP navimow_collector_rows_dropped_total Rows lost because no buffer could hold"
        " them.\n"
        "# TYPE navimow_collector_rows_dropped_total counter\n"
        "navimow_collector_rows_dropped_total 3\n"
        "# HELP navimow_collector_rows_rejected_total Rows the database refused for what they"
        " hold.\n"
        "# TYPE navimow_collector_rows_rejected_total counter\n"
        "navimow_collector_rows_rejected_total 1\n"
        "# HELP navimow_collector_unstored_messages_total Messages on channels of which"
        " nothing is stored.\n"
        "# TYPE navimow_collector_unstored_messages_total counter\n"
        'navimow_collector_unstored_messages_total{mower_id="DEVICE_1",channel="event"} 4\n'
    )


def test_a_required_reauth_is_a_gauge_grafana_can_alert_on() -> None:
    metrics = to_prometheus(replace(HEALTHY, auth_state=AuthState.RELOGIN_REQUIRED))
    assert "\nnavimow_collector_reauth_required 1\n" in metrics
    assert '\nnavimow_collector_auth_state{state="relogin-required"} 1\n' in metrics
    assert '\nnavimow_collector_auth_state{state="fresh"} 0\n' in metrics


def test_label_values_are_escaped() -> None:
    odd = replace(HEALTHY, last_message_ages={'a"b\\c\nd': 1.0})
    assert '{mower_id="a\\"b\\\\c\\nd"} 1.0\n' in to_prometheus(odd)


@pytest.fixture
def server() -> Iterator[tuple[HealthServer, list[Snapshot]]]:
    """A server on an ephemeral port over a probe the test controls."""
    current = [HEALTHY]
    with HealthServer(lambda: current[0], "127.0.0.1", 0) as running:
        yield running, current


def get(server: HealthServer, path: str, method: str = "GET") -> tuple[int, str, str]:
    host, port = server.address
    request = urllib.request.Request(f"http://{host}:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers["Content-Type"], response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], error.read().decode()


def test_the_health_endpoint_answers_over_http(
    server: tuple[HealthServer, list[Snapshot]],
) -> None:
    running, current = server
    current[0] = replace(HEALTHY, auth_state=AuthState.RELOGIN_REQUIRED, database_reachable=False)

    status, content_type, body = get(running, "/health")

    assert (status, content_type) == (200, "application/json")
    assert json.loads(body)["auth"]["reauth_required"] is True
    assert json.loads(body)["database"]["reachable"] is False


def test_a_stalled_loop_answers_503_with_the_full_report(
    server: tuple[HealthServer, list[Snapshot]],
) -> None:
    running, current = server
    current[0] = replace(HEALTHY, seconds_since_tick=STALL_SECONDS * 2)

    status, _, body = get(running, "/health")

    assert status == 503
    assert json.loads(body)["status"] == "stalled"


def test_the_metrics_endpoint_serves_the_text_exposition(
    server: tuple[HealthServer, list[Snapshot]],
) -> None:
    status, content_type, body = get(server[0], "/metrics")

    assert (status, content_type) == (200, "text/plain; version=0.0.4; charset=utf-8")
    assert body == to_prometheus(HEALTHY)


def test_head_answers_like_get_without_a_body(
    server: tuple[HealthServer, list[Snapshot]],
) -> None:
    assert get(server[0], "/health", "HEAD") == (200, "application/json", "")


def test_unknown_paths_are_not_found(server: tuple[HealthServer, list[Snapshot]]) -> None:
    assert get(server[0], "/")[0] == 404
    assert get(server[0], "/health?verbose=1")[0] == 200  # a query string is ignored


def test_a_failing_probe_answers_500_and_keeps_serving(caplog: pytest.LogCaptureFixture) -> None:
    calls = []

    def probe() -> Snapshot:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return HEALTHY

    with HealthServer(probe, "127.0.0.1", 0) as running:
        assert get(running, "/health")[0] == 500
        assert get(running, "/health")[0] == 200
    assert "boom" in caplog.text


def test_an_address_in_use_is_an_os_error_at_construction() -> None:
    with HealthServer(lambda: HEALTHY, "127.0.0.1", 0) as running:
        with pytest.raises(OSError):
            HealthServer(lambda: HEALTHY, "127.0.0.1", running.address[1])


def log_line(record: logging.LogRecord) -> dict[str, object]:
    line = JsonFormatter().format(record)
    assert "\n" not in line  # one object per line, whatever the message holds
    parsed: dict[str, object] = json.loads(line)
    return parsed


def test_log_lines_are_json_objects_with_their_extra_fields() -> None:
    record = logging.LogRecord(
        "navimow_collector.auth", logging.ERROR, __file__, 1, "run `%s`\nnow", ("cmd",), None
    )
    record.command = "navimow-collector login"

    line = log_line(record)

    assert line["level"] == "ERROR"
    assert line["logger"] == "navimow_collector.auth"
    assert line["message"] == "run `cmd`\nnow"
    assert line["command"] == "navimow-collector login"
    assert isinstance(line["time"], str) and line["time"].endswith("Z")


def test_log_lines_carry_exceptions_and_keep_secrets_redacted() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "failed", None, exc_info)
    record.dsn = Secret("postgresql://mower:hunter2@db/navimow")

    line = log_line(record)

    assert "ValueError: bad" in str(line["exception"])
    assert line["dsn"] == "<redacted>"
