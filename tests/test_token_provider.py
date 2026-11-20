"""Token providers: a bearer fetched per request and refreshed before it expires, the
Organization header, and a token value that never leaks into a repr, an error or a log."""

from __future__ import annotations

import functools
import logging
import threading
from collections.abc import Callable

import httpx
import pytest

import masterly
from masterly import AccessToken, ApiError, Client
from masterly import _auth as auth_module
from masterly import _client as client_module
from masterly._auth import _REFRESH_SKEW_SECONDS, _ProviderAuth

ISSUED = "issued-bearer-7f3c9a"
SECOND = "second-bearer-a1b2c3"
FAR_FUTURE = 4_102_444_800.0  # 2100-01-01


class _Provider:
    """Hands out the given tokens in order; an exception in the list is raised instead."""

    def __init__(self, *issued: AccessToken | Exception) -> None:
        self._issued = list(issued)
        self.calls = 0

    def get_token(self) -> AccessToken:
        self.calls += 1
        issued = self._issued.pop(0) if len(self._issued) > 1 else self._issued[0]
        if isinstance(issued, Exception):
            raise issued
        return issued


def _recording(
    seen: list[httpx.Request], status: int = 200, body: object | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={} if body is None else body)

    return handler


def _fake_clock(monkeypatch: pytest.MonkeyPatch, start: float = 0.0) -> list[float]:
    """Inject a settable clock into every `_ProviderAuth` the client builds."""
    now = [start]
    monkeypatch.setattr(
        client_module, "_ProviderAuth", functools.partial(_ProviderAuth, clock=lambda: now[0])
    )
    return now


def test_the_provider_types_are_exported() -> None:
    assert "TokenProvider" in masterly.__all__
    assert "AccessToken" in masterly.__all__
    assert masterly.TokenProvider is auth_module.TokenProvider
    assert masterly.AccessToken is auth_module.AccessToken


def test_a_session_client_sends_the_provider_token_as_bearer() -> None:
    seen: list[httpx.Request] = []
    provider = _Provider(AccessToken(ISSUED, FAR_FUTURE))
    client = Client(
        "https://x", provider, "env_test", transport=httpx.MockTransport(_recording(seen))
    )
    client.request("GET", "/v1/x")
    assert seen[-1].headers["authorization"] == f"Bearer {ISSUED}"
    assert provider.calls == 1


def test_a_service_account_client_sends_the_provider_token_as_bearer() -> None:
    seen: list[httpx.Request] = []
    provider = _Provider(AccessToken(ISSUED, FAR_FUTURE))
    client = Client.for_service_account(
        "https://x", provider, transport=httpx.MockTransport(_recording(seen))
    )
    client.request("GET", "/v1/x")
    assert seen[-1].headers["authorization"] == f"Bearer {ISSUED}"


def test_a_static_token_is_still_a_connection_header() -> None:
    seen: list[httpx.Request] = []
    client = Client("https://x", "tok", transport=httpx.MockTransport(_recording(seen)))
    client.request("GET", "/v1/x")
    assert client._http.headers["authorization"] == "Bearer tok"
    assert not isinstance(client._http.auth, _ProviderAuth)
    assert seen[-1].headers["authorization"] == "Bearer tok"


def test_a_token_is_reused_until_the_skew_and_fetched_anew_from_then_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _fake_clock(monkeypatch)
    seen: list[httpx.Request] = []
    provider = _Provider(AccessToken(ISSUED, 1000.0), AccessToken(SECOND, 5000.0))
    client = Client("https://x", provider, transport=httpx.MockTransport(_recording(seen)))

    client.request("GET", "/v1/x")
    now[0] = 1000.0 - _REFRESH_SKEW_SECONDS - 1
    client.request("GET", "/v1/x")
    assert provider.calls == 1

    now[0] = 1000.0 - _REFRESH_SKEW_SECONDS
    client.request("GET", "/v1/x")
    assert provider.calls == 2
    assert [r.headers["authorization"] for r in seen] == [
        f"Bearer {ISSUED}",
        f"Bearer {ISSUED}",
        f"Bearer {SECOND}",
    ]


def test_a_keyed_resend_after_a_connection_failure_asks_the_provider_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _fake_clock(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    provider = _Provider(AccessToken(ISSUED, 1000.0), AccessToken(SECOND, 5000.0))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            now[0] = 1000.0 - _REFRESH_SKEW_SECONDS + 1
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(202, json={"job_id": "job_1"})

    client = Client("https://x", provider, "env_test", transport=httpx.MockTransport(handler))
    client.sources.ingest("src_1", [{"key": "k1"}])

    assert sleeps == [client_module._KEYED_RETRY_BACKOFF[0]]
    assert provider.calls == 2
    assert [r.headers["authorization"] for r in seen] == [f"Bearer {ISSUED}", f"Bearer {SECOND}"]
    assert seen[0].headers["idempotency-key"] == seen[1].headers["idempotency-key"]


def test_concurrent_requests_on_one_client_fetch_the_token_once() -> None:
    issued = threading.Event()

    class SlowProvider:
        calls = 0

        def get_token(self) -> AccessToken:
            SlowProvider.calls += 1
            issued.wait(0.05)
            return AccessToken(ISSUED, FAR_FUTURE)

    seen: list[httpx.Request] = []
    client = Client("https://x", SlowProvider(), transport=httpx.MockTransport(_recording(seen)))
    threads = [threading.Thread(target=client.request, args=("GET", "/v1/x")) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert SlowProvider.calls == 1
    assert len(seen) == 8


@pytest.mark.parametrize("static", [True, False])
@pytest.mark.parametrize("service_account", [False, True])
def test_organization_rides_on_every_request_including_keyed_writes(
    static: bool, service_account: bool
) -> None:
    seen: list[httpx.Request] = []
    token = "tok" if static else _Provider(AccessToken(ISSUED, FAR_FUTURE))
    transport = httpx.MockTransport(_recording(seen, 202, {"job_id": "job_1"}))
    if service_account:
        client = Client.for_service_account(
            "https://x", token, organization="org_1", transport=transport
        )
    else:
        client = Client("https://x", token, "env_test", organization="org_1", transport=transport)

    assert client.organization == "org_1"
    client.request("GET", "/v1/x")
    client.sources.ingest("src_1", [{"key": "k1"}])
    assert len(seen) == 2
    assert all(r.headers["x-masterly-organization"] == "org_1" for r in seen)


@pytest.mark.parametrize("organization", [None, ""])
@pytest.mark.parametrize("service_account", [False, True])
def test_no_organization_sends_no_organization_header(
    organization: str | None, service_account: bool
) -> None:
    seen: list[httpx.Request] = []
    provider = _Provider(AccessToken(ISSUED, FAR_FUTURE))
    transport = httpx.MockTransport(_recording(seen, 202, {"job_id": "job_1"}))
    if service_account:
        client = Client.for_service_account(
            "https://x", provider, organization=organization, transport=transport
        )
    else:
        client = Client(
            "https://x", provider, "env_test", organization=organization, transport=transport
        )

    assert client.organization is None
    client.request("GET", "/v1/x")
    client.sources.ingest("src_1", [{"key": "k1"}])
    assert len(seen) == 2
    assert all("x-masterly-organization" not in r.headers for r in seen)


def test_a_provider_service_account_keeps_its_persona_and_its_environment_rules() -> None:
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(_recording(seen))
    pinned = Client.for_service_account(
        "https://x", _Provider(AccessToken(ISSUED, FAR_FUTURE)), transport=transport
    )
    asserted = Client.for_service_account(
        "https://x",
        _Provider(AccessToken(ISSUED, FAR_FUTURE)),
        environment="env_prod",
        transport=transport,
    )

    assert pinned.persona == "service-account"
    assert pinned.environment is None
    pinned.request("GET", "/v1/x")
    assert "x-masterly-environment" not in seen[-1].headers

    assert asserted.persona == "service-account"
    assert asserted.environment == "env_prod"
    asserted.request("GET", "/v1/x")
    assert seen[-1].headers["x-masterly-environment"] == "env_prod"


def test_a_provider_session_client_keeps_its_persona_and_environment() -> None:
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(_recording(seen))
    named = Client(
        "https://x", _Provider(AccessToken(ISSUED, FAR_FUTURE)), "env_test", transport=transport
    )
    unnamed = Client("https://x", _Provider(AccessToken(ISSUED, FAR_FUTURE)), transport=transport)

    assert named.persona == "session"
    assert named.environment == "env_test"
    named.request("GET", "/v1/x")
    assert seen[-1].headers["x-masterly-environment"] == "env_test"

    assert unnamed.persona == "session"
    assert unnamed.environment is None
    unnamed.request("GET", "/v1/x")
    assert "x-masterly-environment" not in seen[-1].headers


def test_the_token_never_appears_in_a_repr_an_error_or_a_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    now = _fake_clock(monkeypatch)
    provider = _Provider(AccessToken(ISSUED, 1000.0), RuntimeError("identity provider is down"))
    unauthorized = {"error": {"code": "UNAUTHENTICATED", "message": "token rejected"}}
    client = Client(
        "https://x",
        provider,
        "env_test",
        transport=httpx.MockTransport(_recording([], 401, unauthorized)),
    )

    with pytest.raises(ApiError) as rejected:
        client.request("GET", "/v1/x")
    now[0] = 2000.0
    with pytest.raises(RuntimeError) as failed:
        client.request("GET", "/v1/x")

    assert provider.calls == 2
    exposed = [
        repr(client),
        repr(client._http),
        repr(client._http.auth),
        repr(AccessToken(ISSUED, 1000.0)),
        str(AccessToken(ISSUED, 1000.0)),
        str(rejected.value),
        repr(rejected.value),
        str(failed.value),
        repr(failed.value),
        caplog.text,
    ]
    assert all(ISSUED not in text for text in exposed)


def test_a_failing_provider_sends_nothing_and_raises_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        nonlocal calls
        calls += 1
        return httpx.Response(202, json={"job_id": "job_1"})

    class Down(Exception):
        pass

    provider = _Provider(Down("identity provider is down"))
    client = Client("https://x", provider, "env_test", transport=httpx.MockTransport(handler))

    with pytest.raises(Down):
        client.request("GET", "/v1/x")
    with pytest.raises(Down):
        client.sources.ingest("src_1", [{"key": "k1"}])
    assert calls == 0
    assert sleeps == []
    assert provider.calls == 2
