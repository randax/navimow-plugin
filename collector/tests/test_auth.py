"""Authentication as an operator and transport user observes it."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from mower_sdk.http import HTTPClientError

from navimow_collector.auth import (
    AuthState,
    Credential,
    LoopbackListener,
    TokenClient,
    TokenManager,
    TokenStore,
)
from navimow_collector.cli import main

# Vendor prose gathered by the token research (docs/research/token-flow.md, sections 1 and 5).
REJECTED_REFRESH = "Refresh token is invalid or server rejected the request"
TOO_FREQUENT = "Request too frequent. Please retry after 1 minute."
CIRCUIT_BREAKER = "url Circuit Breaker"


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
    ],
)
def test_refresh_failures_are_classified_by_vendor_prose(
    tmp_path: Path, status: int, body: str, expected: AuthState
) -> None:
    tokens = manager(logged_in(tmp_path), Response(status, body))

    assert run(tokens.access_token()) == "access"  # the existing token keeps serving
    assert tokens.state is expected
    assert tokens.next_attempt_at == (3360 if expected is AuthState.RETRY_PENDING else None)


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

    assert run(tokens.on_unauthorized()) == "replacement"
    assert tokens.state is AuthState.FRESH


def test_refreshes_immediately_after_the_mqtt_oauth_error(tmp_path: Path) -> None:
    tokens = manager(logged_in(tmp_path), Response(200, token("replacement")), now=1)

    assert run(tokens.on_mqtt_error("CODE_OAUTH_INFO_ILLEGAL")) == "replacement"


def test_a_rejected_token_gets_one_attempt_then_waits_for_the_ladder(tmp_path: Path) -> None:
    now = [3300.0]
    session = FakeSession([Response(503, CIRCUIT_BREAKER) for _ in range(2)])
    tokens = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: now[0]
    )
    run(tokens.access_token())  # proactive refresh fails; retry scheduled for 3360

    now[0] += 10
    run(tokens.on_unauthorized())  # the token is now known dead: try at once
    run(tokens.on_unauthorized())  # a burst of rejections must not hammer the endpoint

    assert len(session.forms) == 2
    assert tokens.state is AuthState.RETRY_PENDING


def test_concurrent_callers_share_one_refresh(tmp_path: Path) -> None:
    session = FakeSession([Response(200, token("new", "rotated"))])
    tokens = TokenManager(
        TokenClient(session, "id", "secret"), logged_in(tmp_path), clock=lambda: 3300
    )

    async def both() -> list[str | None]:
        return list(await asyncio.gather(tokens.access_token(), tokens.on_unauthorized()))

    assert run(both()) == ["new", "new"]
    assert len(session.forms) == 1  # a second refresh would spend a rotated-away token


def test_relogin_required_does_not_exit_and_clears_after_a_new_login(tmp_path: Path) -> None:
    store = logged_in(tmp_path)
    tokens = manager(store, Response(400, REJECTED_REFRESH))
    run(tokens.access_token())
    assert tokens.state.value == "relogin-required"  # .value: mypy would narrow the attribute
    assert run(tokens.on_unauthorized()) == "access"  # still answering, never raising

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
