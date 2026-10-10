"""Authentication as an operator and transport user observes it."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from mower_sdk.http import HTTPClientError, UrllibSession

from navimow_collector.auth import (
    AuthState,
    Credential,
    LoopbackListener,
    TokenClient,
    TokenManager,
    TokenRequestError,
    TokenStore,
    maintain,
)
from navimow_collector.cli import main

from .conftest import Clock, unavailable

# Vendor prose gathered by the token research (docs/research/token-flow.md, sections 1 and 5).
REJECTED_REFRESH = "Refresh token is invalid or server rejected the request"
TOO_FREQUENT = "Request too frequent. Please retry after 1 minute."
CIRCUIT_BREAKER = "url Circuit Breaker"


def has_ipv6_loopback() -> bool:
    """Whether this host has a ::1 to listen on, which a container often has not."""
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError as error:
        if error.errno not in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
            raise  # a fault of some other kind, which a skip would hide
        return False
    return True


def address_on_the_network(family: socket.AddressFamily) -> str | None:
    """An address others on its network reach this machine by, where it has one."""
    elsewhere = {socket.AF_INET: "192.0.2.1", socket.AF_INET6: "2001:db8::1"}[family]
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((elsewhere, 9))  # routed, never sent: a datagram socket only aims
            address: str = probe.getsockname()[0]
    except OSError:
        return None
    return None if ip_address(address).is_loopback else address


def need_ipv6() -> None:
    """Pass by a test of ::1 on a host without it, unless CI says this one has it."""
    if not has_ipv6_loopback():
        unavailable("ipv6", "this host has no ::1 to listen on")


def held(host: str, port: int) -> bool:
    """Whether anyone still listens on a port at one address of this machine."""
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as taker:
            # As the listener binds: past a connection lately closed, not past a listener.
            taker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            taker.bind((host, port))
    except OSError:
        return True
    return False


@contextmanager
def stranger_on_ipv6() -> Iterator[int]:
    """The port of another process listening on ::1, which no one holds on 127.0.0.1."""
    while True:
        with socket.socket(socket.AF_INET6) as stranger:
            stranger.bind(("::1", 0))
            stranger.listen()
            port: int = stranger.getsockname()[1]
            if held("127.0.0.1", port):
                continue  # by someone else again, and the listener would stop at that
            yield port
            return


def port_of(listener: LoopbackListener) -> int:
    port = urlsplit(listener.redirect_uri).port
    assert port is not None
    return port


def fail_ipv6(monkeypatch: pytest.MonkeyPatch, number: int) -> None:
    """Have the system refuse every IPv6 socket, as on a host this one is not."""

    class Socket(socket.socket):
        def __init__(self, family: int = -1, *args: Any, **kwargs: Any) -> None:
            if family == socket.AF_INET6:
                raise OSError(number, os.strerror(number))
            super().__init__(family, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", Socket)


def redirect_to(address: str) -> None:
    """Follow the login redirect to one address of this machine, past any proxy the
    environment names: `no_proxy` seldom lists ::1."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{address}/callback?code=from-browser&state=expected", timeout=5):
        pass


class Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def json(self) -> Any:
        return json.loads(self._body)


class Request:
    def __init__(self, response: Response) -> None:
        self.response = response

    async def __aenter__(self) -> Response:
        await asyncio.sleep(0)  # a real transport yields to other callers mid-request
        return self.response

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeSession:
    """Canned SDK transport which records the OAuth wire contract."""

    closed = False

    def __init__(self, responses: list[Response]) -> None:
        self.responses = iter(responses)
        self.forms: list[dict[str, str] | None] = []

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
        assert (method, url) == (
            "POST",
            "https://navimow-fra.ninebot.com/openapi/oauth/getAccessToken",
        )
        self.forms.append(data)
        return Request(next(self.responses))


class FailingSession(FakeSession):
    def __init__(self, error: Exception) -> None:
        super().__init__([])
        self.error = error

    def request(self, method: str, url: str, **kwargs: Any) -> Request:
        raise self.error


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def token(access: str = "access", refresh: str | None = "refresh") -> str:
    body: dict[str, Any] = {"access_token": access, "token_type": "Bearer", "expires_in": 3600}
    if refresh is not None:
        body["refresh_token"] = refresh
    return json.dumps(body)


def logged_in(tmp_path: Path, access: str = "access", at: float = 0) -> TokenStore:
    """A state file as `navimow-collector login` leaves it."""
    store = TokenStore(tmp_path / "token.json")
    store.save(Credential(access, "refresh", 3600, at))
    return store


def manager(store: TokenStore, *responses: Response, now: float = 3300) -> TokenManager:
    return TokenManager(
        TokenClient(FakeSession(list(responses)), "id", "secret"), store, clock=lambda: now
    )


def stored(store: TokenStore) -> Credential:
    credential = store.load()
    assert credential is not None
    return credential


def test_exchanges_a_code_over_the_sdk_session_as_a_form() -> None:
    session = FakeSession([Response(200, token())])

    credential = run(
        TokenClient(session, "client", "secret").exchange(
            "one-time-code", "http://localhost:1/callback", now=10
        )
    )

    assert credential == Credential("access", "refresh", 3600, 10)
    assert session.forms == [
        {
            "grant_type": "authorization_code",
            "code": "one-time-code",
            "client_id": "client",
            "client_secret": "secret",
            "redirect_uri": "http://localhost:1/callback",
        }
    ]


def test_refreshes_before_expiry_and_persists_a_rotated_refresh_token(tmp_path: Path) -> None:
    store = logged_in(tmp_path)
    session = FakeSession([Response(200, token("new", "rotated"))])
    tokens = TokenManager(TokenClient(session, "id", "secret"), store, clock=lambda: 3300)

    assert run(tokens.access_token()) == "new"
    assert tokens.state is AuthState.FRESH
    assert stored(store) == Credential("new", "rotated", 3600, 3300)
    assert session.forms == [
        {
            "grant_type": "refresh_token",
            "refresh_token": "refresh",
            "client_id": "id",
            "client_secret": "secret",
        }
    ]


def test_keeps_the_previous_refresh_token_when_the_response_omits_one(tmp_path: Path) -> None:
    store = logged_in(tmp_path)

    assert run(manager(store, Response(200, token("new", refresh=None))).access_token()) == "new"
    assert stored(store).refresh_token == "refresh"


def test_does_not_refresh_before_the_margin(tmp_path: Path) -> None:
    assert run(manager(logged_in(tmp_path), now=3299).access_token()) == "access"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (503, TOO_FREQUENT, AuthState.RETRY_PENDING),
        (503, CIRCUIT_BREAKER, AuthState.RETRY_PENDING),
        (200, json.dumps({"code": 0, "desc": CIRCUIT_BREAKER}), AuthState.RETRY_PENDING),
        (502, "", AuthState.RETRY_PENDING),
        (400, REJECTED_REFRESH, AuthState.RELOGIN_REQUIRED),
        (401, "", AuthState.RELOGIN_REQUIRED),
        (401, "Authentication required", AuthState.RELOGIN_REQUIRED),
        (403, CIRCUIT_BREAKER, AuthState.RETRY_PENDING),
        # Digits inside numbers are not a status: an epoch containing "401", a hex ray id.
        (
            500,
            json.dumps({"code": 500, "desc": "system busy", "ts": 1759540113}),
            AuthState.RETRY_PENDING,
        ),
        (502, "Bad gateway, ray id 8a4013f2", AuthState.RETRY_PENDING),
        (200, json.dumps({"code": 401, "desc": "token has expired"}), AuthState.RELOGIN_REQUIRED),
        # Vendor prose naming the field is still prose...
        (
            200,
            json.dumps({"code": 401, "desc": "access_token invalid"}),
            AuthState.RELOGIN_REQUIRED,
        ),
        (200, "access_token invalid", AuthState.RELOGIN_REQUIRED),
        # A gateway's own error page is transient whatever it says (Apache's stock 502 here).
        (
            502,
            "The proxy server received an invalid response from an upstream server.",
            AuthState.RETRY_PENDING,
        ),
        (429, "Too many requests: token invalid", AuthState.RETRY_PENDING),
        # A request id with a numeric segment is not a status.
        (
            200,
            json.dumps({"code": 0, "desc": "system busy", "requestId": "a1b2-401-c3d4"}),
            AuthState.RETRY_PENDING,
        ),
        # The vendor's own OAuth error family.
        (
            400,
            json.dumps({"code": 0, "desc": "CODE_OAUTH_INFO_ILLEGAL"}),
            AuthState.RELOGIN_REQUIRED,
        ),
        (200, '{"desc": "access_token invalid', AuthState.RELOGIN_REQUIRED),
        # ...but a credential cut short (here a JSON error reporting "char 401") is transient.
        (200, " " * 384 + '{"access_token": "abc', AuthState.RETRY_PENDING),
    ],
)
def test_refresh_failures_are_classified_by_vendor_prose(
    tmp_path: Path, status: int, body: str, expected: AuthState
) -> None:
    tokens = manager(logged_in(tmp_path), Response(status, body))

    assert run(tokens.access_token()) == "access"  # the existing token keeps serving
    assert tokens.state is expected
    assert tokens.next_attempt_at == (3360 if expected is AuthState.RETRY_PENDING else 6900)


def test_a_network_error_is_transient_whatever_its_wording(tmp_path: Path) -> None:
    tokens = TokenManager(
        TokenClient(FailingSession(HTTPClientError("certificate has expired")), "id", "secret"),
        logged_in(tmp_path),
        clock=lambda: 3300,
    )

    assert run(tokens.access_token()) == "access"
    assert tokens.state is AuthState.RETRY_PENDING


def test_transient_failures_retry_on_an_escalating_ladder(tmp_path: Path) -> None:
    times = iter([3300, 3360, 3660, 4560, 8160, 11760])
    session = FakeSession([Response(503, CIRCUIT_BREAKER) for _ in range(6)])
    tokens = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: next(times)
    )

    for expected in [3360, 3660, 4560, 8160, 11760, 15360]:
        assert run(tokens.access_token()) == "access"
        assert tokens.next_attempt_at == expected


def test_refreshes_immediately_after_a_rest_401(tmp_path: Path) -> None:
    tokens = manager(logged_in(tmp_path), Response(200, token("replacement")), now=1)

    assert run(tokens.on_unauthorized("access")) == "replacement"
    assert tokens.state is AuthState.FRESH


def test_refreshes_immediately_after_the_mqtt_oauth_error(tmp_path: Path) -> None:
    tokens = manager(logged_in(tmp_path), Response(200, token("replacement")), now=1)

    assert run(tokens.on_mqtt_error("CODE_OAUTH_INFO_ILLEGAL", "access")) == "replacement"


def test_a_rejected_token_gets_one_attempt_then_waits_for_the_ladder(tmp_path: Path) -> None:
    now = [3300.0]
    session = FakeSession([Response(503, CIRCUIT_BREAKER) for _ in range(2)])
    tokens = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: now[0]
    )
    run(tokens.access_token())  # proactive refresh fails; retry scheduled for 3360

    now[0] += 10
    run(tokens.on_unauthorized("access"))  # the token is now known dead: try at once
    run(tokens.on_unauthorized("access"))  # a burst of rejections must not hammer the endpoint

    assert len(session.forms) == 2
    assert tokens.state is AuthState.RETRY_PENDING


def test_concurrent_callers_share_one_refresh(tmp_path: Path) -> None:
    session = FakeSession([Response(200, token("new", "rotated"))])
    tokens = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: 3300
    )

    async def both() -> list[str | None]:
        return list(await asyncio.gather(tokens.access_token(), tokens.on_unauthorized("access")))

    assert run(both()) == ["new", "new"]
    assert len(session.forms) == 1  # a second refresh would spend a rotated-away token


def test_relogin_required_does_not_exit_and_clears_after_a_new_login(tmp_path: Path) -> None:
    store = logged_in(tmp_path)
    tokens = manager(store, Response(400, REJECTED_REFRESH))
    run(tokens.access_token())
    assert tokens.state.value == "relogin-required"  # .value: mypy would narrow the attribute
    assert run(tokens.on_unauthorized("access")) == "access"  # still answering, never raising

    logged_in(tmp_path, access="relogged", at=3300)

    assert run(tokens.access_token()) == "relogged"
    assert tokens.state is AuthState.FRESH


def test_atomic_save_preserves_the_prior_file_when_a_write_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = logged_in(tmp_path, access="old")

    def crash(source: str, destination: str) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="simulated crash"):
        store.save(Credential("new", "refresh", 3600, 1))

    assert stored(store).access_token == "old"
    assert list(tmp_path.glob("*.tmp")) == []
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_a_refreshed_credential_keeps_serving_when_it_cannot_be_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = manager(logged_in(tmp_path), Response(200, token("new", "rotated")))

    def crash(source: str, destination: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)

    assert run(tokens.access_token()) == "new"
    assert tokens.state is AuthState.FRESH


def test_loopback_listener_captures_and_validates_the_redirect() -> None:
    with LoopbackListener("expected") as listener:
        assert listener.redirect_uri.startswith("http://localhost:")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{listener.redirect_uri}?code=forged&state=wrong")
        with urllib.request.urlopen(f"{listener.redirect_uri}?code=from-browser&state=expected"):
            pass

        assert listener.wait(timeout=1) == "from-browser"


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]"])
def test_loopback_listener_takes_the_redirect_on_either_address_of_localhost(host: str) -> None:
    # The redirect names `localhost`, which a browser may resolve to ::1 and never retry.
    if host == "[::1]":
        need_ipv6()

    with LoopbackListener("expected") as listener:
        redirect_to(f"{host}:{port_of(listener)}")

        assert listener.wait(timeout=1) == "from-browser"


def test_loopback_listener_once_left_holds_no_address_and_no_thread() -> None:
    need_ipv6()
    threads = set(threading.enumerate())

    with LoopbackListener("expected") as listener:
        port = port_of(listener)

    assert not held("127.0.0.1", port)
    assert not held("::1", port)
    assert set(threading.enumerate()) <= threads


def test_loopback_listener_entered_a_second_time_refuses_and_serves_on() -> None:
    with LoopbackListener("expected") as listener:
        with pytest.raises(RuntimeError, match="entered once"):
            listener.__enter__()
        redirect_to(f"127.0.0.1:{port_of(listener)}")

        assert listener.wait(timeout=1) == "from-browser"


def test_loopback_listener_that_cannot_start_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    need_ipv6()
    listener = LoopbackListener("expected")
    threads = set(threading.enumerate())
    start = threading.Thread.start
    started: list[threading.Thread] = []

    def start_only_one(thread: threading.Thread) -> None:
        if started:
            raise RuntimeError("can't start new thread")  # what a process out of them is told
        started.append(thread)
        start(thread)

    monkeypatch.setattr(threading.Thread, "start", start_only_one)

    with pytest.raises(RuntimeError, match="can't start new thread"), listener:
        pass

    assert not held("127.0.0.1", port_of(listener))
    assert not held("::1", port_of(listener))
    assert set(threading.enumerate()) <= threads


@pytest.mark.parametrize(
    "family", [socket.AF_INET, socket.AF_INET6], ids=lambda family: family.name
)
def test_loopback_listener_is_out_of_reach_from_the_network(family: socket.AddressFamily) -> None:
    address = address_on_the_network(family)
    if address is None:
        pytest.skip(f"this host has no {family.name} address on a network")

    host = f"[{address}]" if family is socket.AF_INET6 else address

    with LoopbackListener("expected") as listener:
        # Refused, or answered by whoever else holds the port at that address: either way
        # the redirect is not the listener's.
        with suppress(Exception):
            redirect_to(f"{host}:{port_of(listener)}")

        with pytest.raises(TimeoutError):
            listener.wait(timeout=0)


@pytest.mark.parametrize("missing", [errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL])
def test_loopback_listener_serves_ipv4_alone_on_a_host_without_ipv6(
    missing: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_ipv6(monkeypatch, missing)

    with LoopbackListener("expected") as listener:
        redirect_to(f"127.0.0.1:{port_of(listener)}")

        assert listener.wait(timeout=1) == "from-browser"


def test_loopback_listener_does_not_take_a_failing_ipv6_for_a_missing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Out of file descriptors is not a host without IPv6: on 127.0.0.1 alone, a browser
    # that takes `localhost` to be ::1 would wait out the timeout with nothing to say why.
    fail_ipv6(monkeypatch, errno.EMFILE)

    with pytest.raises(OSError) as refused:
        LoopbackListener("expected")

    assert refused.value.errno == errno.EMFILE


def test_loopback_listener_never_shares_its_port_with_a_stranger_on_ipv6() -> None:
    # Listening on 127.0.0.1 alone would leave a browser that resolves `localhost` to ::1
    # handing the redirect, and the code in it, to whoever holds the port there.
    need_ipv6()

    with stranger_on_ipv6() as port:
        with pytest.raises(
            OSError,
            match=r"cannot listen on \[::1\]:\d+: Address already in use",
        ) as refused:  # kept, and with it whatever the listener still holds
            LoopbackListener("expected", port=port)

        assert not held("127.0.0.1", port)
        assert refused.value.errno == errno.EADDRINUSE


def test_a_login_no_browser_ever_finishes_points_to_the_headless_way_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "collector.toml"
    config.write_text(f'[auth]\nstate_file = "{tmp_path / "tokens.json"}"\n')
    monkeypatch.setattr("webbrowser.open", lambda url: False)  # no browser on this machine

    assert main(["--config", str(config), "login", "--timeout", "0.05"]) == 2

    assert "navimow-collector login --no-browser" in capsys.readouterr().err


def test_a_login_that_cannot_listen_points_to_the_headless_way_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "collector.toml"
    config.write_text(f'[auth]\nstate_file = "{tmp_path / "tokens.json"}"\n')
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    fail_ipv6(monkeypatch, errno.EMFILE)

    assert main(["--config", str(config), "login", "--timeout", "0.05"]) == 2

    err = capsys.readouterr().err
    assert "no listener for the login redirect" in err
    assert "cannot listen on [::1]" in err
    assert "navimow-collector login --no-browser" in err


def test_login_command_exchanges_a_pasted_redirect_url_and_saves_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_file = tmp_path / "tokens.json"
    config = tmp_path / "collector.toml"
    config.write_text(f'[auth]\nstate_file = "{state_file}"\n')
    session = FakeSession([Response(200, token("secret-token"))])
    pasted = "http://localhost:1/callback?code=pasted&state=x"

    status = main(
        ["--config", str(config), "login", "--code", pasted], session_factory=lambda: session
    )

    assert status == 0
    assert stored(TokenStore(state_file)).access_token == "secret-token"
    assert session.forms[0] is not None
    assert session.forms[0]["code"] == "pasted"
    assert session.forms[0]["redirect_uri"] == "http://localhost:1/callback"
    assert "secret-token" not in capsys.readouterr().out


def test_login_no_browser_prints_the_manual_url_for_a_headless_machine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "collector.toml"
    config.write_text('[auth]\nclient_id = "custom"\n')

    assert main(["--config", str(config), "login", "--no-browser"]) == 0

    out = capsys.readouterr().out
    assert "client_id=custom" in out
    assert "localhost%3A1%2Fcallback" in out


def test_a_failed_reactive_refresh_is_retried_on_schedule_without_spinning(
    tmp_path: Path,
) -> None:
    clock = Clock(100)
    session = FakeSession([Response(503, CIRCUIT_BREAKER), Response(200, token("new"))])
    tokens = TokenManager(TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=clock)
    run(tokens.on_unauthorized("access"))  # long before the proactive margin
    sleeps: list[float] = []

    class Stop(Exception):
        pass

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop
        clock.now += seconds

    with pytest.raises(Stop):
        run(maintain(tokens, sleep))

    assert all(seconds > 0 for seconds in sleeps)
    assert run(tokens.access_token()) == "new"
    assert len(session.forms) == 2


def test_a_late_rejection_of_an_already_replaced_token_is_ignored(tmp_path: Path) -> None:
    session = FakeSession([Response(200, token("new", "rotated"))])
    tokens = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: 1
    )

    assert run(tokens.on_unauthorized("access")) == "new"
    assert run(tokens.on_unauthorized("access")) == "new"  # a slow response still using "access"
    assert len(session.forms) == 1


def test_an_unsaved_refresh_does_not_resurrect_the_spent_credential_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = logged_in(tmp_path)
    tokens = manager(store, Response(200, token("new", "rotated")), Response(400, REJECTED_REFRESH))
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", lambda source, destination: (_ for _ in ()).throw(OSError()))
        assert run(tokens.access_token()) == "new"

    assert run(tokens.on_unauthorized("new")) == "new"
    assert tokens.state.value == "relogin-required"
    assert run(tokens.access_token()) == "new"  # the old file on disk is not a new login
    assert tokens.state.value == "relogin-required"


def test_an_unreadable_state_file_never_escapes_while_awaiting_a_login(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.path.mkdir()  # reading it raises IsADirectoryError

    tokens = manager(store)

    assert tokens.state is AuthState.RELOGIN_REQUIRED
    assert run(tokens.access_token()) is None


def test_a_corrupt_state_file_starts_in_relogin_required(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.path.write_text("{not json")

    assert manager(store).state is AuthState.RELOGIN_REQUIRED


def test_loopback_listener_keeps_the_first_redirect() -> None:
    with LoopbackListener("expected") as listener:
        with urllib.request.urlopen(f"{listener.redirect_uri}?code=first&state=expected"):
            pass
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{listener.redirect_uri}?code=second&state=expected")

        assert listener.wait(timeout=1) == "first"


def test_a_new_login_is_picked_up_while_retrying(tmp_path: Path) -> None:
    store = logged_in(tmp_path)
    clock = Clock(3300)
    tokens = TokenManager(
        TokenClient(FakeSession([Response(503, "some unrecognised failure")]), "id", "secret"),
        store,
        clock=clock,
    )
    run(tokens.access_token())
    assert tokens.state.value == "retry-pending"

    logged_in(tmp_path, access="relogged", at=3300)

    assert run(tokens.access_token()) == "relogged"
    assert tokens.state is AuthState.FRESH


def test_an_adopted_login_already_past_its_margin_is_refreshed_before_use(tmp_path: Path) -> None:
    store = logged_in(tmp_path)
    tokens = manager(store, Response(400, REJECTED_REFRESH), Response(200, token("fresh")))
    run(tokens.access_token())
    assert tokens.state.value == "relogin-required"

    logged_in(tmp_path, access="relogged", at=0)  # written long ago, e.g. restored from a backup

    assert run(tokens.access_token()) == "fresh"


def test_a_short_lived_token_is_not_refreshed_in_a_tight_loop(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(Credential("access", "refresh", 60, 0))

    assert manager(store, now=0).seconds_until_next_action() == 30


def test_a_refresh_that_echoes_the_rejected_token_backs_off(tmp_path: Path) -> None:
    session = FakeSession([Response(200, token("access", "rotated"))])
    tokens = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: 1
    )

    run(tokens.on_unauthorized("access"))
    run(tokens.on_unauthorized("access"))

    assert len(session.forms) == 1
    assert tokens.state is AuthState.RETRY_PENDING


def test_a_login_written_during_a_refresh_is_not_overwritten(tmp_path: Path) -> None:
    store = logged_in(tmp_path)

    class LoginMidRefresh(FakeSession):
        def request(self, method: str, url: str, **kwargs: Any) -> Request:
            logged_in(tmp_path, access="relogged", at=3300)  # the operator runs `login` now
            return super().request(method, url, **kwargs)

    session = LoginMidRefresh([Response(200, token("refreshed-old-grant"))])
    tokens = TokenManager(TokenClient(session, "id", "secret"), store, clock=lambda: 3300)

    assert run(tokens.access_token()) == "relogged"
    assert stored(store).access_token == "relogged"


def test_tokens_rejected_on_first_use_go_onto_the_retry_ladder(tmp_path: Path) -> None:
    session = FakeSession([Response(200, token(f"minted-{n}")) for n in range(5)])
    tokens = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: 1
    )

    current = "access"
    for _ in range(5):  # REST rejects every token the token endpoint mints
        current = run(tokens.on_unauthorized(current))

    assert len(session.forms) == 1
    assert tokens.state is AuthState.RETRY_PENDING


def test_a_login_written_just_before_a_refresh_is_saved_is_kept(tmp_path: Path) -> None:
    class LoginBeforeSave(TokenStore):
        def replace(self, expected: Credential | None, credential: Credential) -> bool:
            TokenStore(self.path).save(Credential("relogged", "refresh", 3600, 3300))
            return super().replace(expected, credential)

    store = LoginBeforeSave(logged_in(tmp_path).path)
    tokens = manager(store, Response(200, token("refreshed-old-grant")))

    assert run(tokens.access_token()) == "relogged"
    assert stored(store).access_token == "relogged"


def test_repeated_first_use_rejections_climb_the_retry_ladder(tmp_path: Path) -> None:
    clock = Clock(1)
    session = FakeSession([Response(200, token(f"minted-{n}")) for n in range(20)])
    tokens = TokenManager(TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=clock)
    current = run(tokens.on_unauthorized("access"))
    delays = []
    for _ in range(4):
        current = run(tokens.on_unauthorized(current))  # rejected on first use, again
        assert tokens.next_attempt_at is not None
        delays.append(tokens.next_attempt_at - clock.now)
        clock.now = tokens.next_attempt_at
        current = run(tokens.access_token())  # the scheduled retry mints another

    assert delays == [60, 300, 900, 3600]


def test_login_no_browser_keeps_the_config_in_the_follow_up_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "my collector.toml"
    config.write_text("[auth]\n")

    assert main(["--config", str(config), "login", "--no-browser"]) == 0

    assert f"navimow-collector --config '{config}' login --code" in capsys.readouterr().out


def test_login_no_browser_keeps_a_config_named_by_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fresh shell has no NAVIMOW_CONFIG: the follow-up must name the file itself.
    config = tmp_path / "my collector.toml"
    config.write_text("[auth]\n")
    monkeypatch.setenv("NAVIMOW_CONFIG", str(config))

    assert main(["login", "--no-browser"]) == 0

    assert f"navimow-collector --config '{config}' login --code" in capsys.readouterr().out


def test_a_refresh_is_saved_even_after_the_state_file_was_deleted(tmp_path: Path) -> None:
    store = logged_in(tmp_path)
    tokens = manager(store, Response(200, token("new", "rotated")))
    store.path.unlink()

    assert run(tokens.access_token()) == "new"
    assert stored(store) == Credential("new", "rotated", 3600, 3300)


def test_an_unreadable_state_file_is_reported_once_not_per_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.path.mkdir()
    tokens = manager(store)

    for _ in range(5):
        run(tokens.access_token())

    assert len([r for r in caplog.records if "Could not read" in r.getMessage()]) == 1


@pytest.mark.parametrize(
    ("pasted", "code"),
    [
        ("localhost:1/callback?code=abc&state=x", "abc"),  # some address bars drop the scheme
        ("bare-code-with-padding==", "bare-code-with-padding=="),
        ("code=abc&state=x", "abc"),  # just the query string
    ],
)
def test_login_accepts_pasted_codes_in_the_forms_people_copy(
    tmp_path: Path, pasted: str, code: str
) -> None:
    state_file = tmp_path / "tokens.json"
    config = tmp_path / "collector.toml"
    config.write_text(f'[auth]\nstate_file = "{state_file}"\n')
    session = FakeSession([Response(200, token())])
    assert (
        main(["--config", str(config), "login", "--code", pasted], session_factory=lambda: session)
        == 0
    )

    assert session.forms[0] is not None
    assert session.forms[0]["code"] == code


def test_tokens_that_serve_a_while_before_rejection_keep_the_ladder_flat(tmp_path: Path) -> None:
    clock = Clock(1)
    session = FakeSession([Response(200, token(f"minted-{n}")) for n in range(10)])
    tokens = TokenManager(TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=clock)

    current = "access"
    for _ in range(6):  # the vendor kills each token after half an hour
        current = run(tokens.on_unauthorized(current))
        clock.now += 1800

    assert tokens.state is AuthState.FRESH
    assert len(session.forms) == 6


@pytest.mark.parametrize("cadence", [61, 300])  # MQTT reconnect backoff, REST poll floor
def test_first_use_rejections_climb_the_ladder_whatever_the_caller_cadence(
    tmp_path: Path, cadence: int
) -> None:
    clock = Clock(1)
    session = FakeSession([Response(200, token(f"minted-{n}")) for n in range(20)])
    tokens = TokenManager(TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=clock)

    current = run(tokens.on_unauthorized("access"))
    clock.now += cadence
    run(tokens.on_unauthorized(current))  # the replacement fails on its first use

    assert tokens.state is AuthState.RETRY_PENDING
    assert len(session.forms) == 1


@pytest.mark.parametrize(
    "pasted",
    [
        "http://localhost:54321/callback?code=abc&state=x",  # after the browser flow timed out
        "localhost:54321/callback?code=abc&state=x",  # the same, copied without its scheme
    ],
)
def test_a_pasted_loopback_redirect_is_exchanged_with_its_own_redirect_uri(
    tmp_path: Path, pasted: str
) -> None:
    config = tmp_path / "collector.toml"
    config.write_text(f'[auth]\nstate_file = "{tmp_path / "tokens.json"}"\n')
    session = FakeSession([Response(200, token())])

    assert (
        main(["--config", str(config), "login", "--code", pasted], session_factory=lambda: session)
        == 0
    )

    assert session.forms[0] is not None
    assert session.forms[0]["redirect_uri"] == "http://localhost:54321/callback"


def test_an_exchange_without_a_refresh_token_names_the_missing_field() -> None:
    body = json.dumps({"access_token": "secret-access", "expires_in": 3600})
    client = TokenClient(FakeSession([Response(200, body)]), "id", "secret")

    with pytest.raises(TokenRequestError, match="refresh_token") as raised:
        run(client.exchange("code", "http://localhost:1/callback"))

    assert "secret-access" not in str(raised.value)


def test_relogin_required_still_probes_hourly_and_heals_if_it_was_transient(
    tmp_path: Path,
) -> None:
    clock = Clock(3300)
    session = FakeSession([Response(403, "Attention Required!"), Response(200, token("healed"))])
    tokens = TokenManager(TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=clock)
    run(tokens.access_token())
    assert tokens.state.value == "relogin-required"

    clock.now += 3599
    run(tokens.access_token())
    assert len(session.forms) == 1  # no hammering a grant that is probably dead

    clock.now += 1
    assert run(tokens.access_token()) == "healed"
    assert tokens.state is AuthState.FRESH


def test_vendor_prose_never_logs_a_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(Credential("ACCESS-SECRET", "REFRESH-SECRET", 3600, 0))
    echoed = json.dumps({"desc": "refresh_token REFRESH-SECRET is invalid"})
    half_formed = json.dumps({"access_token": None, "refresh_token": "LIVE-SECRET"})
    tokens = manager(store, Response(400, echoed))
    run(tokens.access_token())
    client = TokenClient(FakeSession([Response(200, half_formed)]), "id", "secret")
    with pytest.raises(TokenRequestError) as raised:
        run(client.exchange("code", "http://localhost:1/callback"))

    logged = " ".join(record.getMessage() for record in caplog.records) + str(raised.value)
    assert "SECRET" not in logged


@pytest.mark.parametrize(
    ("expires_in", "lifetime"), [("7200", 7200), (7200.0, 7200), (None, 3600), ("soon", 3600)]
)
def test_a_usable_credential_with_an_odd_lifetime_is_kept(
    tmp_path: Path, expires_in: str | float | None, lifetime: int
) -> None:
    body: dict[str, Any] = {"access_token": "new", "refresh_token": "rotated"}
    if expires_in is not None:
        body["expires_in"] = expires_in
    store = logged_in(tmp_path)

    assert run(manager(store, Response(200, json.dumps(body))).access_token()) == "new"
    assert stored(store) == Credential("new", "rotated", lifetime, 3300)


def test_a_transient_failure_while_probing_is_not_reported_as_a_rejection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock(3300)
    session = FakeSession([Response(400, REJECTED_REFRESH), Response(503, CIRCUIT_BREAKER)])
    tokens = TokenManager(TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=clock)
    run(tokens.access_token())
    caplog.clear()

    clock.now += 3600
    run(tokens.access_token())

    assert tokens.state is AuthState.RELOGIN_REQUIRED
    assert len(session.forms) == 2
    assert not any("rejected the stored login" in r.getMessage() for r in caplog.records)


def test_the_hourly_rung_of_the_ladder_is_logged_as_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock(3300)
    session = FakeSession([Response(503, CIRCUIT_BREAKER) for _ in range(4)])
    tokens = TokenManager(TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=clock)

    levels = []
    for _ in range(4):
        caplog.clear()
        run(tokens.access_token())
        levels.append(caplog.records[-1].levelname)
        assert tokens.next_attempt_at is not None
        clock.now = tokens.next_attempt_at

    assert levels == ["WARNING", "WARNING", "WARNING", "ERROR"]


def test_a_plain_text_body_echoing_the_refresh_token_is_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(Credential("access", "REFRESH-SECRET", 3600, 0))

    run(manager(store, Response(200, "bad refresh_token REFRESH-SECRET")).access_token())

    assert "REFRESH-SECRET" not in " ".join(r.getMessage() for r in caplog.records)


CONFIGURED_LOGIN = "navimow-collector --config '/etc/navimow/my collector.toml' login"


def test_a_required_relogin_is_logged_once_with_the_exact_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock(3300)
    rejections = [Response(400, REJECTED_REFRESH), Response(400, REJECTED_REFRESH)]
    tokens = TokenManager(
        TokenClient(FakeSession(rejections), "id", "secret"),
        logged_in(tmp_path),
        clock=clock,
        login_command=CONFIGURED_LOGIN,
    )

    for _ in range(30):  # every tick for five minutes
        run(tokens.access_token())
        clock.now += 10
    assert tokens.next_attempt_at is not None
    clock.now = tokens.next_attempt_at
    run(tokens.access_token())  # the hourly probe is rejected too

    [line] = [r for r in caplog.records if r.levelname == "ERROR"]
    assert f"run `{CONFIGURED_LOGIN}`" in line.getMessage()
    assert line.__dict__["command"] == CONFIGURED_LOGIN
    reminder = caplog.records[-1]
    assert reminder.levelname == "WARNING"
    assert reminder.__dict__["command"] == CONFIGURED_LOGIN
    assert tokens.login_command == CONFIGURED_LOGIN


def test_starting_without_a_login_names_the_exact_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    TokenManager(
        TokenClient(FakeSession([]), "id", "secret"),
        TokenStore(tmp_path / "token.json"),
        login_command=CONFIGURED_LOGIN,
    )

    [line] = caplog.records
    assert f"run `{CONFIGURED_LOGIN}`" in line.getMessage()
    assert line.__dict__["command"] == CONFIGURED_LOGIN


def test_the_login_command_is_the_plain_one_by_default(tmp_path: Path) -> None:
    assert manager(logged_in(tmp_path)).login_command == "navimow-collector login"


def test_an_mqtt_error_that_is_not_oauth_does_not_blame_the_token(tmp_path: Path) -> None:
    session = FakeSession([])
    tokens = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: 1
    )

    assert run(tokens.on_mqtt_error("connection reset by peer", "access")) == "access"

    assert session.forms == []  # no rejection was reported, so nothing to refresh yet
    assert tokens.state is AuthState.FRESH


def test_an_mqtt_error_that_is_not_oauth_still_refreshes_a_token_that_is_due(
    tmp_path: Path,
) -> None:
    tokens = manager(logged_in(tmp_path), Response(200, token("new")))  # past the margin

    assert run(tokens.on_mqtt_error("connection reset by peer", "access")) == "new"


# The tests above drive fakes; the ones below pin the contracts those fakes assume onto the
# SDK's real UrllibSession, exercised against a local stand-in for the token endpoint.


@contextmanager
def vendor_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    response: tuple[int, str] = (200, ""),
    *,
    stall: threading.Event | None = None,
) -> Iterator[list[tuple[str | None, str]]]:
    """A local token endpoint answering `response`, yielding what it received.

    With `stall`, the endpoint holds every request until the event is set at teardown, so
    only the client's own timeout can end the request.
    """
    received: list[tuple[str | None, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            length = int(self.headers.get("Content-Length") or 0)
            received.append((self.headers.get("Content-Type"), self.rfile.read(length).decode()))
            if stall is not None:
                stall.wait(30)  # far beyond any session timeout under test
                return  # the client gave up long ago; there is nobody to answer
            status, body = response
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # Polling often for shutdown: the default half second would be spent at every teardown.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setattr(
        "navimow_collector.auth.TOKEN_URL",
        f"http://127.0.0.1:{server.server_port}/openapi/oauth/getAccessToken",
    )
    try:
        yield received
    finally:
        if stall is not None:
            stall.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_the_real_session_posts_the_token_request_as_a_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A code and a secret with characters a form must escape, or they arrive as other fields.
    with vendor_endpoint(monkeypatch, (200, token())) as received:
        credential = run(
            TokenClient(UrllibSession(), "client", "s3cr&t+%2F").exchange(
                "one+time&code==", "http://localhost:1/callback", now=10
            )
        )

    assert credential == Credential("access", "refresh", 3600, 10)
    [(content_type, body)] = received
    # The media type, whatever parameters (a charset, say) a session adds to it.
    assert content_type is not None
    assert content_type.partition(";")[0].strip() == "application/x-www-form-urlencoded"
    assert parse_qs(body, strict_parsing=True) == {
        "grant_type": ["authorization_code"],
        "code": ["one+time&code=="],
        "client_id": ["client"],
        "client_secret": ["s3cr&t+%2F"],
        "redirect_uri": ["http://localhost:1/callback"],
    }


@pytest.mark.parametrize(
    ("status", "body", "relogin"),
    [
        # No re-login word in the body: only the status digits can classify it as dead.
        (401, "Authentication required", True),
        # "invalid" in a gateway's page would mean re-login as prose: only the 5xx status
        # rule keeps it transient, so this pins that rule through the real session too.
        (503, "The proxy server received an invalid response from an upstream server.", False),
        # The error's body must arrive too: here only the vendor's prose says the grant is
        # dead, and only its prose says a 403 is the gateway throttling.
        (400, REJECTED_REFRESH, True),
        (403, CIRCUIT_BREAKER, False),
    ],
)
def test_the_real_session_returns_vendor_errors_as_responses_for_classification(
    monkeypatch: pytest.MonkeyPatch, status: int, body: str, relogin: bool
) -> None:
    # urllib raises on 4xx/5xx; the session must hand them back as responses with a status
    # and a body, or every rejection would look like a transport error and re-login would
    # never be seen.
    with vendor_endpoint(monkeypatch, (status, body)):
        client = TokenClient(UrllibSession(), "id", "secret")
        with pytest.raises(TokenRequestError) as raised:
            run(client.refresh(Credential("access", "refresh", 3600, 0), now=1))

    assert raised.value.relogin is relogin


def test_a_hung_token_request_is_bounded_by_the_session_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The refresh runs under the manager's lock, so a hang without a bound would block every
    # caller for good.
    stall = threading.Event()
    with vendor_endpoint(monkeypatch, stall=stall) as received:
        tokens = TokenManager(
            TokenClient(UrllibSession(timeout=0.5), "id", "secret"),
            logged_in(tmp_path),
            clock=lambda: 3300,
        )

        mid_request: list[AuthState] = []

        async def onlooker() -> None:
            deadline = time.monotonic() + 5
            while not received and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            mid_request.append(tokens.state)  # the request is with the endpoint, hanging

        async def concurrent_callers() -> list[str | None]:
            first, second, _ = await asyncio.gather(
                tokens.access_token(), tokens.access_token(), onlooker()
            )
            return [first, second]

        started = time.monotonic()
        assert run(concurrent_callers()) == ["access", "access"]
        elapsed = time.monotonic() - started

    assert len(received) == 1  # the request reached the endpoint, and was not repeated
    assert 0.5 <= elapsed < 5  # it hung until the session's timeout, and no longer
    # The hang was the request's alone: the event loop went on running everything else.
    assert mid_request == [AuthState.REFRESHING]
    assert tokens.state is AuthState.RETRY_PENDING


def test_login_left_to_build_its_session_gives_a_token_request_thirty_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hang test proves a session's timeout bounds a hung request; this pins the bound a
    # request really gets from the session `navimow-collector` builds when handed none.
    # Shown on `login`; `collect` is handed its session from that same default.
    config = tmp_path / "collector.toml"
    config.write_text(f'[auth]\nstate_file = "{tmp_path / "tokens.json"}"\n')
    timeouts: list[float | None] = []
    open_url = urllib.request.OpenerDirector.open

    def recording_open(
        opener: urllib.request.OpenerDirector,
        fullurl: Any,
        data: Any = None,
        timeout: float | None = None,
    ) -> Any:
        timeouts.append(timeout)
        return open_url(opener, fullurl, data, timeout)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", recording_open)
    with vendor_endpoint(monkeypatch, (200, token())) as received:
        assert main(["--config", str(config), "login", "--code", "pasted"]) == 0

    assert len(received) == 1
    assert timeouts == [30.0]


# The real `login` command, run as a second process with the network stubbed out. It
# touches its marker as it starts, from where one canned exchange separates it from the
# state-file lock. Given a third path, it touches the marker only once it holds that lock
# and is about to replace the state file, and goes no further until the third path exists.
LOGIN_IN_A_SECOND_PROCESS = """\
import json
import os
import sys
import time
from pathlib import Path

from navimow_collector.cli import main


class Response:
    status = 200

    async def text(self):
        return json.dumps(
            {"access_token": "relogged", "refresh_token": "refresh", "expires_in": 3600}
        )


class Request:
    async def __aenter__(self):
        return Response()

    async def __aexit__(self, *exceptions):
        return None


class Session:
    closed = False

    def request(self, method, url, **kwargs):
        return Request()


config, marker = sys.argv[1], Path(sys.argv[2])
if len(sys.argv) > 3:
    release, replacing = Path(sys.argv[3]), os.replace

    def replace_once_released(source, destination):
        marker.touch()
        deadline = time.monotonic() + 30
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        replacing(source, destination)

    os.replace = replace_once_released
else:
    marker.touch()
sys.exit(main(["--config", config, "login", "--code", "relogin-code"], session_factory=Session))
"""


def login_in_a_second_process(tmp_path: Path, store: TokenStore, *markers: Path) -> list[str]:
    """The command line running `login` against `store` in another process."""
    config = tmp_path / "collector.toml"
    config.write_text(f'[auth]\nstate_file = "{store.path}"\n')
    script = tmp_path / "login_in_a_second_process.py"
    script.write_text(LOGIN_IN_A_SECOND_PROCESS)
    return [sys.executable, str(script), str(config), *map(str, markers)]


def test_a_login_in_a_second_process_waits_for_the_refresh_lock_and_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = logged_in(tmp_path)
    marker = tmp_path / "login-started"
    command = login_in_a_second_process(tmp_path, store, marker)
    logins: list[subprocess.Popen[bytes]] = []
    # What the refresh found just before it wrote: whether the login had started, its exit
    # code a second on (none while it still waits), and what the state file held.
    found_under_the_lock: list[tuple[bool, int | None, Credential | None]] = []
    replacing = os.replace

    def replace_while_a_login_runs(source: str, destination: Path) -> None:
        """The refresh putting its state file in place, which it does under the lock, with
        `login` run in a second process before that goes ahead."""
        login = subprocess.Popen(command)
        logins.append(login)
        deadline = time.monotonic() + 30
        while not marker.exists() and login.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        # Waiting can only be shown by its lasting: the login needs milliseconds to reach
        # the lock, and is given a full second in which to finish were nothing holding it.
        try:
            login.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        found_under_the_lock.append((marker.exists(), login.returncode, store.load()))
        replacing(source, destination)

    monkeypatch.setattr(os, "replace", replace_while_a_login_runs)
    tokens = TokenManager(
        TokenClient(FakeSession([Response(200, token("refreshed-old-grant"))]), "id", "secret"),
        store,
        clock=lambda: 3300,
    )

    try:
        assert run(tokens.access_token()) == "refreshed-old-grant"
        # The login had started, was still waiting a second later, and had written nothing.
        assert found_under_the_lock == [(True, None, Credential("access", "refresh", 3600, 0))]
        assert logins[0].wait(timeout=30) == 0
    finally:
        for login in logins:  # never left running, whatever failed above
            login.kill()
            login.wait()

    assert stored(store).access_token == "relogged"  # the blocked login wrote last and wins
    assert run(tokens.access_token()) == "relogged"  # and the manager adopts it


def test_a_refresh_waits_for_a_login_saving_in_a_second_process_and_yields_to_it(
    tmp_path: Path,
) -> None:
    # The refresh must read the state file under the lock it writes under: reading any
    # sooner, it would overwrite a login saved in between with the grant that login replaced.
    store = logged_in(tmp_path)
    saving, release = tmp_path / "login-is-saving", tmp_path / "let-the-login-save"
    requested = threading.Event()

    class Signalling(FakeSession):
        def request(self, method: str, url: str, **kwargs: Any) -> Request:
            requested.set()
            return super().request(method, url, **kwargs)

    tokens = TokenManager(
        TokenClient(Signalling([Response(200, token("refreshed-old-grant"))]), "id", "secret"),
        store,
        clock=lambda: 3300,
    )
    login = subprocess.Popen(login_in_a_second_process(tmp_path, store, saving, release))
    refreshing = ThreadPoolExecutor(max_workers=1)
    try:
        deadline = time.monotonic() + 30
        while not saving.exists() and login.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert saving.exists(), "the login never came to save its credential"

        refresh = refreshing.submit(run, tokens.access_token())
        assert requested.wait(30), "the refresh never asked for a token"
        # Waiting can only be shown by its lasting: from its token request the refresh needs
        # microseconds to reach the state file, and is given half a second.
        time.sleep(0.5)
        assert not refresh.done(), "the refresh did not wait for the login to finish saving"

        release.touch()
        assert login.wait(timeout=30) == 0
        assert refresh.result(timeout=30) == "relogged"  # found under the lock, and adopted
    finally:
        release.touch()  # nothing is left waiting, whatever failed above
        login.kill()
        login.wait()
        refreshing.shutdown()

    assert stored(store).access_token == "relogged"  # the refresh did not write over it
