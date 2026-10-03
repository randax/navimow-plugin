"""Authentication as an operator and transport user observes it."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from navimow_collector.auth import (
    AuthState,
    TokenClient,
    TokenManager,
    TokenStore,
    authorization_code,
    loopback_listener,
)
from navimow_collector.cli import main


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
    """Canned SDK transport which retains the OAuth wire contract."""

    closed = False

    def __init__(self, responses: list[Response]) -> None:
        self.responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

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
        self.requests.append({"method": method, "url": url, "data": data})
        return Request(next(self.responses))


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def token(access: str = "access", refresh: str = "refresh", expires_in: int = 3600) -> str:
    return json.dumps({"access_token": access, "refresh_token": refresh, "expires_in": expires_in})


def test_exchanges_a_manual_redirect_url_using_the_sdk_session(tmp_path: Path) -> None:
    session = FakeSession([Response(200, token())])
    client = TokenClient(session, "client", "secret")

    credential = run(
        client.exchange(
            authorization_code("http://localhost:1/callback?code=one-time-code&state=state"),
            "http://localhost:1/callback",
            now=10,
        )
    )

    assert credential.access_token == "access"
    assert session.requests == [
        {
            "method": "POST",
            "url": "https://navimow-fra.ninebot.com/openapi/oauth/getAccessToken",
            "data": {
                "grant_type": "authorization_code",
                "code": "one-time-code",
                "client_id": "client",
                "client_secret": "secret",
                "redirect_uri": "http://localhost:1/callback",
            },
        }
    ]


def test_refreshes_before_expiry_and_keeps_the_previous_refresh_token(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(
        run(
            TokenClient(FakeSession([Response(200, token())]), "id", "secret").exchange(
                "code", "uri", now=0
            )
        )
    )
    session = FakeSession([Response(200, json.dumps({"access_token": "new", "expires_in": 3600}))])
    manager = TokenManager(TokenClient(session, "id", "secret"), store, clock=lambda: 3300)

    assert run(manager.access_token()) == "new"
    assert manager.state is AuthState.FRESH
    assert store.load() is not None
    assert store.load().refresh_token == "refresh"  # type: ignore[union-attr]
    assert session.requests[0]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh",
        "client_id": "id",
        "client_secret": "secret",
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Request too frequent. Please retry after 1 minute.", AuthState.RETRY_PENDING),
        ("url Circuit Breaker", AuthState.RETRY_PENDING),
        ("network unavailable", AuthState.RETRY_PENDING),
        ("401 refresh credential has expired", AuthState.RELOGIN_REQUIRED),
        ("Forbidden by account policy", AuthState.RELOGIN_REQUIRED),
    ],
)
def test_refresh_failure_state_uses_vendor_prose(
    tmp_path: Path, body: str, expected: AuthState
) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(
        run(
            TokenClient(FakeSession([Response(200, token())]), "id", "secret").exchange(
                "code", "uri", now=0
            )
        )
    )
    manager = TokenManager(
        TokenClient(FakeSession([Response(503, body)]), "id", "secret"),
        store,
        clock=lambda: 3300,
    )

    assert run(manager.access_token()) == "access"
    assert manager.state is expected
    assert manager.next_attempt_at == (3360 if expected is AuthState.RETRY_PENDING else None)


def test_retries_on_the_escalating_ladder_and_on_mqtt_auth_error(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(
        run(
            TokenClient(FakeSession([Response(200, token())]), "id", "secret").exchange(
                "code", "uri", now=0
            )
        )
    )
    times = iter([1, 61, 361, 1261, 4861])
    session = FakeSession([Response(503, "network") for _ in range(5)])
    manager = TokenManager(TokenClient(session, "id", "secret"), store, clock=lambda: next(times))

    for expected in [61, 361, 1261, 4861, 8461]:
        assert run(manager.on_mqtt_error("CODE_OAUTH_INFO_ILLEGAL")) == "access"
        assert manager.state is AuthState.RETRY_PENDING
        assert manager.next_attempt_at == expected


def test_refreshes_immediately_after_a_rest_401(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(
        run(
            TokenClient(FakeSession([Response(200, token())]), "id", "secret").exchange(
                "code", "uri", now=0
            )
        )
    )
    manager = TokenManager(
        TokenClient(FakeSession([Response(200, token("replacement"))]), "id", "secret"),
        store,
        clock=lambda: 1,
    )

    assert run(manager.on_unauthorized()) == "replacement"
    assert manager.state is AuthState.FRESH


def test_atomic_save_preserves_prior_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TokenStore(tmp_path / "token.json")
    old = run(
        TokenClient(FakeSession([Response(200, token("old"))]), "id", "secret").exchange(
            "code", "uri", now=0
        )
    )
    new = run(
        TokenClient(FakeSession([Response(200, token("new"))]), "id", "secret").exchange(
            "code", "uri", now=1
        )
    )
    store.save(old)

    def crash(source: str, destination: str) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="simulated crash"):
        store.save(new)

    assert store.load() == old
    assert list(tmp_path.glob("*.tmp")) == []
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_loopback_listener_captures_and_validates_the_redirect() -> None:
    import urllib.request

    try:
        listener_context = loopback_listener("expected")
        listener = listener_context.__enter__()
    except PermissionError:
        pytest.skip("the execution sandbox does not permit a loopback listener")
    try:
        callback = f"{listener.redirect_uri}?code=from-browser&state=expected"
        with urllib.request.urlopen(callback) as response:
            assert response.status == 200
        assert listener.wait(timeout=1) == "from-browser"
    finally:
        listener_context.__exit__(None, None, None)


def test_login_command_exchanges_a_pasted_code_and_saves_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_file = tmp_path / "tokens.json"
    config = tmp_path / "collector.toml"
    config.write_text(f'[auth]\nstate_file = "{state_file}"\n')
    session = FakeSession([Response(200, token("secret-token"))])

    assert (
        main(
            ["--config", str(config), "login", "--code", "http://localhost:1/callback?code=pasted"],
            session_factory=lambda: session,
        )
        == 0
    )

    assert TokenStore(state_file).load() is not None
    assert TokenStore(state_file).load().access_token == "secret-token"  # type: ignore[union-attr]
    assert session.requests[0]["data"] is not None
    assert session.requests[0]["data"]["redirect_uri"] == "http://localhost:1/callback"
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


def logged_in(tmp_path: Path, at: float = 0) -> TokenStore:
    store = TokenStore(tmp_path / "token.json")
    store.save(
        run(
            TokenClient(FakeSession([Response(200, token())]), "id", "secret").exchange(
                "code", "uri", now=at
            )
        )
    )
    return store


class FailingSession(FakeSession):
    def __init__(self, error: Exception) -> None:
        super().__init__([])
        self.error = error

    def request(self, method: str, url: str, **kwargs: Any) -> Request:
        raise self.error


def test_a_vendor_error_body_with_status_200_is_transient(tmp_path: Path) -> None:
    body = json.dumps({"code": 0, "desc": "url Circuit Breaker"})
    manager = TokenManager(
        TokenClient(FakeSession([Response(200, body)]), "id", "secret"),
        logged_in(tmp_path),
        clock=lambda: 3300,
    )

    assert run(manager.access_token()) == "access"
    assert manager.state is AuthState.RETRY_PENDING


def test_a_network_error_is_transient_whatever_its_wording(tmp_path: Path) -> None:
    from mower_sdk.http import HTTPClientError

    manager = TokenManager(
        TokenClient(FailingSession(HTTPClientError("certificate has expired")), "id", "secret"),
        logged_in(tmp_path),
        clock=lambda: 3300,
    )

    assert run(manager.access_token()) == "access"
    assert manager.state is AuthState.RETRY_PENDING


def test_a_refreshed_credential_keeps_serving_when_it_cannot_be_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = logged_in(tmp_path)
    manager = TokenManager(
        TokenClient(FakeSession([Response(200, token("new", "rotated"))]), "id", "secret"),
        store,
        clock=lambda: 3300,
    )

    def crash(source: str, destination: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)

    assert run(manager.access_token()) == "new"
    assert manager.state is AuthState.FRESH


def test_concurrent_callers_share_one_refresh(tmp_path: Path) -> None:
    session = FakeSession([Response(200, token("new", "rotated"))])
    manager = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: 3300
    )

    async def both() -> list[str | None]:
        return list(await asyncio.gather(manager.access_token(), manager.on_unauthorized()))

    assert run(both()) == ["new", "new"]
    assert len(session.requests) == 1


def test_a_rejection_waits_for_the_retry_ladder(tmp_path: Path) -> None:
    now = [3300.0]
    session = FakeSession([Response(503, "url Circuit Breaker")])
    manager = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: now[0]
    )
    run(manager.access_token())

    now[0] += 30
    assert run(manager.on_unauthorized()) == "access"
    assert len(session.requests) == 1
    assert manager.state is AuthState.RETRY_PENDING


def test_a_fresh_login_is_picked_up_without_a_restart(tmp_path: Path) -> None:
    store = logged_in(tmp_path)
    manager = TokenManager(
        TokenClient(FakeSession([Response(400, "refresh token is invalid")]), "id", "secret"),
        store,
        clock=lambda: 3300,
    )
    run(manager.access_token())
    assert manager.state.value == "relogin-required"  # .value: mypy would narrow the attribute

    store.save(
        run(
            TokenClient(FakeSession([Response(200, token("relogged"))]), "id", "secret").exchange(
                "code", "uri", now=3300
            )
        )
    )

    assert run(manager.access_token()) == "relogged"
    assert manager.state is AuthState.FRESH
