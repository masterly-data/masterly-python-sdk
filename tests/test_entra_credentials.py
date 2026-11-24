"""The Entra credentials, certificate and federated: the token request each sends, where it may
send it, and no secret in a repr, an error or a log. The test certificate is generated here; none
is committed."""

from __future__ import annotations

import base64
import datetime
import functools
import hashlib
import importlib
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

import masterly
from masterly import (
    Client,
    EntraCertificateCredential,
    EntraFederatedCredential,
    TokenProviderError,
)
from masterly import _client as client_module
from masterly import _entra as entra_module
from masterly._auth import _REFRESH_SKEW_SECONDS, _ProviderAuth

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
CLIENT_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
SCOPE = "api://masterly-test/.default"
HOST = "login.microsoftonline.com"
ISSUER = f"https://{HOST}/{TENANT}/v2.0"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
TOKEN_ENDPOINT = f"https://{HOST}/{TENANT}/oauth2/v2.0/token"
API = "https://app.example.com"
ISSUED = "issued-access-token-9b8a7c"
PASSWORD = b"key-password-5e1f"
JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
NOW = 1_800_000_000.0
FEDERATED_A = "federated-assertion-a-3c2b1a"
FEDERATED_B = "federated-assertion-b-6f5e4d"
FEDERATED_FILE_ENV = "AZURE_FEDERATED_TOKEN_FILE"


@dataclass
class _Certificate:
    cert_pem: bytes
    key_pem: bytes  # encrypted with PASSWORD
    der: bytes
    public_key: Any


@pytest.fixture(scope="module")
def certificate() -> _Certificate:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "masterly-test-service")])
    start = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return _Certificate(
        cert_pem=cert.public_bytes(serialization.Encoding.PEM),
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(PASSWORD),
        ),
        der=cert.public_bytes(serialization.Encoding.DER),
        public_key=key.public_key(),
    )


class _Entra:
    """One transport for discovery, the token endpoint (any other identity-provider URL) and
    the Masterly API, recording what each received."""

    def __init__(
        self,
        *,
        document: dict[str, Any] | None = None,
        token_status: int = 200,
        token_body: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self.document = document or {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT}
        self.token_status = token_status
        self.token_body = token_body or (
            lambda form: {"token_type": "Bearer", "access_token": ISSUED, "expires_in": 3599}
        )
        self.discoveries: list[httpx.Request] = []
        self.token_requests: list[httpx.Request] = []
        self.api_requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(API):
            self.api_requests.append(request)
            return httpx.Response(200, json={})
        if request.method == "GET" and str(request.url) == DISCOVERY:
            self.discoveries.append(request)
            return httpx.Response(200, json=self.document)
        self.token_requests.append(request)
        return httpx.Response(self.token_status, json=self.token_body(_form(request)))

    def forms(self) -> list[dict[str, str]]:
        return [_form(request) for request in self.token_requests]


def _form(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode("ascii"), strict_parsing=True)
    assert all(len(values) == 1 for values in parsed.values())
    return {name: values[0] for name, values in parsed.items()}


def _unpadded(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _verified(assertion: str, certificate: _Certificate) -> tuple[dict[str, Any], dict[str, Any]]:
    """The assertion's header and claims, once its RS256 signature checks out."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    header, claims, signature = assertion.split(".")
    certificate.public_key.verify(
        _unpadded(signature),
        f"{header}.{claims}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return json.loads(_unpadded(header)), json.loads(_unpadded(claims))


def _certificate_credential(
    certificate: _Certificate, entra: _Entra, **kwargs: Any
) -> EntraCertificateCredential:
    options: dict[str, Any] = {
        "private_key": certificate.key_pem,
        "password": PASSWORD,
        "clock": lambda: NOW,
        **kwargs,
    }
    return EntraCertificateCredential(
        TENANT, CLIENT_ID, SCOPE, certificate.cert_pem, transport=entra.transport, **options
    )


def test_the_entra_credential_is_exported() -> None:
    assert "EntraCertificateCredential" in masterly.__all__
    assert masterly.EntraCertificateCredential is entra_module.EntraCertificateCredential


def test_the_base_install_gains_no_dependency_and_the_entra_extra_adds_cryptography() -> None:
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^dependencies = \["httpx>=0\.27"\]$', pyproject, re.MULTILINE)
    assert re.search(r'^entra = \["cryptography>=[0-9.]+"\]$', pyproject, re.MULTILINE)
    assert re.search(r'^dev = \[[^\]]*"cryptography>=[0-9.]+"', pyproject, re.MULTILINE)


def test_the_certificate_credential_posts_one_client_credentials_form(
    certificate: _Certificate,
) -> None:
    entra = _Entra()
    _certificate_credential(certificate, entra).get_token()

    assert [str(r.url) for r in entra.discoveries] == [DISCOVERY]
    assert len(entra.token_requests) == 1
    request = entra.token_requests[0]
    assert request.method == "POST"
    assert str(request.url) == TOKEN_ENDPOINT
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = _form(request)
    assert set(form) == {
        "grant_type",
        "client_id",
        "scope",
        "client_assertion_type",
        "client_assertion",
    }
    assert form["grant_type"] == "client_credentials"
    assert form["client_id"] == CLIENT_ID
    assert form["scope"] == SCOPE
    assert form["client_assertion_type"] == JWT_BEARER


def test_the_client_assertion_is_rs256_signed_by_the_certificate_and_names_the_endpoint(
    certificate: _Certificate,
) -> None:
    entra = _Entra()
    credential = _certificate_credential(certificate, entra)
    credential.get_token()
    credential.get_token()

    assert len(entra.discoveries) == 1  # discovery is fetched once per credential
    first, second = (form["client_assertion"] for form in entra.forms())
    header, claims = _verified(first, certificate)
    thumbprint = base64.urlsafe_b64encode(hashlib.sha256(certificate.der).digest()).rstrip(b"=")
    assert header == {"alg": "RS256", "typ": "JWT", "x5t#S256": thumbprint.decode("ascii")}
    assert claims["aud"] == TOKEN_ENDPOINT
    assert claims["iss"] == claims["sub"] == CLIENT_ID
    assert claims["nbf"] == claims["iat"] == int(NOW)
    assert 0 < claims["exp"] - claims["iat"] <= 600
    assert claims["jti"] != _verified(second, certificate)[1]["jti"]


def test_the_certificate_and_key_may_be_one_file_on_disk(
    certificate: _Certificate, tmp_path: Path
) -> None:
    combined = tmp_path / "service.pem"
    combined.write_bytes(certificate.cert_pem + certificate.key_pem)
    entra = _Entra()
    credential = EntraCertificateCredential(
        TENANT, CLIENT_ID, SCOPE, combined, password=PASSWORD.decode(), transport=entra.transport
    )
    assert credential.get_token().token == ISSUED
    _verified(entra.forms()[0]["client_assertion"], certificate)


def test_a_wrong_key_password_is_refused_without_echoing_it(certificate: _Certificate) -> None:
    with pytest.raises(ValueError, match="could not be loaded") as raised:
        _certificate_credential(certificate, _Entra(), password=b"wrong-password-0a1b")
    assert "wrong-password-0a1b" not in str(raised.value)
    assert raised.value.__context__ is None


def test_key_text_taken_for_a_path_is_not_echoed(certificate: _Certificate) -> None:
    # PEM text with no BEGIN line (a pasted body) is taken for a path; the error must not hold it.
    body = "\n".join(certificate.key_pem.decode("ascii").splitlines()[1:-1])
    with pytest.raises(ValueError, match="neither PEM text nor a readable file") as raised:
        _certificate_credential(certificate, _Entra(), private_key=body)
    for error in _chain(raised.value):
        assert body.splitlines()[0] not in str(error) + repr(error)


def test_the_certificate_credential_without_cryptography_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "cryptography", None)
    with pytest.raises(ImportError, match=re.escape("pip install 'masterly[entra]'")):
        EntraCertificateCredential(TENANT, CLIENT_ID, SCOPE, b"-----BEGIN CERTIFICATE-----")


@pytest.mark.parametrize("expires_in", [3599, "3599"])
def test_expires_at_is_the_clock_plus_expires_in(
    expires_in: int | str, certificate: _Certificate
) -> None:
    entra = _Entra(token_body=lambda form: {"access_token": ISSUED, "expires_in": expires_in})
    issued = _certificate_credential(certificate, entra, clock=lambda: 1_000.0).get_token()
    assert issued.token == ISSUED
    assert issued.expires_at == 1_000.0 + 3599


def test_a_service_account_client_sends_the_entra_token_and_the_organization(
    certificate: _Certificate,
) -> None:
    entra = _Entra()
    credential = _certificate_credential(certificate, entra)
    client = Client.for_service_account(
        API, credential, organization="org_x", transport=entra.transport
    )
    client.request("GET", "/v1/x")

    assert len(entra.token_requests) == 1
    sent = entra.api_requests[-1]
    assert sent.headers["authorization"] == f"Bearer {ISSUED}"
    assert sent.headers["x-masterly-organization"] == "org_x"


def _chain(error: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def test_no_secret_reaches_a_repr_an_error_or_a_log(
    certificate: _Certificate, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def refusal(form: dict[str, str]) -> dict[str, Any]:
        # Entra's body is not ours to repeat: plant every value the request carried in it.
        return {
            "error": "invalid_client",
            "error_description": f"AADSTS700027: assertion {form['client_assertion']} rejected",
            "access_token": ISSUED,
        }

    entra = _Entra(token_status=400, token_body=refusal)
    credential = _certificate_credential(certificate, entra)
    key_pem = certificate.key_pem.decode("ascii")
    secrets = [ISSUED, PASSWORD.decode(), key_pem, key_pem.splitlines()[1]]
    client = Client.for_service_account(API, credential, transport=entra.transport)

    with pytest.raises(TokenProviderError) as raised:
        client.request("GET", "/v1/x")

    assert entra.api_requests == []
    assertion = entra.forms()[0]["client_assertion"]
    secrets.append(assertion)
    chain = _chain(raised.value)
    assert len(chain) >= 2
    assert "invalid_client" in str(chain[1])
    assert "AADSTS700027" not in str(chain[1])
    exposed = [repr(credential), caplog.text]
    exposed += [str(error) for error in chain] + [repr(error) for error in chain]
    exposed += [str(record.args) for record in caplog.records]
    for secret in secrets:
        for text in exposed:
            assert secret not in text
    assert repr(credential) == (
        f"EntraCertificateCredential(tenant_id={TENANT!r}, client_id={CLIENT_ID!r})"
    )


_FOREIGN_DISCOVERY = [
    pytest.param(
        f"https://{HOST}/11111111-2222-3333-4444-555555555555/v2.0",
        TOKEN_ENDPOINT,
        id="another-tenants-issuer",
    ),
    pytest.param(ISSUER, f"http://{HOST}/{TENANT}/oauth2/v2.0/token", id="http-token-endpoint"),
    pytest.param(
        ISSUER,
        f"https://evil.example/{TENANT}/oauth2/v2.0/token",
        id="foreign-host-token-endpoint",
    ),
    pytest.param(f"{ISSUER}/", TOKEN_ENDPOINT, id="issuer-with-trailing-slash"),
    pytest.param(ISSUER, f"https://{HOST}@evil.example/token", id="user-info-token-endpoint"),
]

_UNPINNABLE = [
    ("common", HOST),
    ("organizations", HOST),
    ("consumers", HOST),
    ("contoso.onmicrosoft.com", HOST),
    ("not-a-guid", HOST),
    (TENANT, f"http://{HOST}"),
    (TENANT, f"https://{HOST}/{TENANT}"),
]


@pytest.mark.parametrize(("issuer", "token_endpoint"), _FOREIGN_DISCOVERY)
def test_a_discovery_document_that_is_not_the_tenants_sends_no_credential(
    issuer: str, token_endpoint: str, certificate: _Certificate
) -> None:
    entra = _Entra(document={"issuer": issuer, "token_endpoint": token_endpoint})
    credential = _certificate_credential(certificate, entra)
    signed: list[bytes] = []
    sign = credential._sign

    def recording_sign(data: bytes) -> bytes:
        signed.append(data)
        return sign(data)

    credential._sign = recording_sign
    with pytest.raises(RuntimeError, match="no credential was sent"):
        credential.get_token()
    assert len(entra.discoveries) == 1
    assert entra.token_requests == []
    assert signed == []  # no client assertion was even signed


@pytest.mark.parametrize(("tenant_id", "authority_host"), _UNPINNABLE)
def test_a_tenant_or_authority_that_cannot_be_pinned_is_refused_at_construction(
    tenant_id: str, authority_host: str, certificate: _Certificate
) -> None:
    entra = _Entra()
    with pytest.raises(ValueError):
        EntraCertificateCredential(
            tenant_id,
            CLIENT_ID,
            SCOPE,
            certificate.cert_pem,
            private_key=certificate.key_pem,
            password=PASSWORD,
            authority_host=authority_host,
            transport=entra.transport,
        )
    assert entra.discoveries == entra.token_requests == []


def test_an_https_authority_host_url_is_accepted(certificate: _Certificate) -> None:
    entra = _Entra()
    credential = _certificate_credential(certificate, entra, authority_host=f"https://{HOST}/")
    assert credential.get_token().token == ISSUED


# --- the federated credential ------------------------------------------------------------


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A federated token file, named by AZURE_FEDERATED_TOKEN_FILE as AKS workload identity does."""
    path = tmp_path / "azure-identity-token"
    path.write_text(f"  {FEDERATED_A}\n", encoding="utf-8")
    monkeypatch.setenv(FEDERATED_FILE_ENV, str(path))
    return path


def _federated_credential(entra: _Entra, **kwargs: Any) -> EntraFederatedCredential:
    options: dict[str, Any] = {"clock": lambda: NOW, **kwargs}
    return EntraFederatedCredential(TENANT, CLIENT_ID, SCOPE, transport=entra.transport, **options)


def _counting(value: object = FEDERATED_A) -> tuple[Callable[[], Any], list[int]]:
    calls: list[int] = []

    def assertion() -> Any:
        calls.append(1)
        return value

    return assertion, calls


def test_the_federated_credential_is_exported() -> None:
    assert "EntraFederatedCredential" in masterly.__all__
    assert masterly.EntraFederatedCredential is entra_module.EntraFederatedCredential


def test_the_federated_credential_posts_the_token_file_as_its_client_assertion(
    token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entra = _Entra()
    credential = _federated_credential(entra)
    monkeypatch.delenv(FEDERATED_FILE_ENV)  # the path was resolved at construction
    issued = credential.get_token()

    assert issued.token == ISSUED
    assert issued.expires_at == NOW + 3599
    assert [str(r.url) for r in entra.discoveries] == [DISCOVERY]
    assert len(entra.token_requests) == 1
    request = entra.token_requests[0]
    assert request.method == "POST"
    assert str(request.url) == TOKEN_ENDPOINT
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert _form(request) == {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "client_assertion_type": JWT_BEARER,
        "client_assertion": FEDERATED_A,  # the file's content, surrounding whitespace stripped
    }


def test_a_callable_assertion_is_asked_on_every_request_and_the_file_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FEDERATED_FILE_ENV, str(tmp_path / "does-not-exist"))
    calls: list[int] = []

    def assertion() -> str:
        calls.append(1)
        return f" federated-assertion-{len(calls)}\n"

    entra = _Entra()
    credential = _federated_credential(entra, assertion=assertion)
    assert calls == []  # not asked at construction
    credential.get_token()
    credential.get_token()

    assert len(calls) == 2
    assert [form["client_assertion"] for form in entra.forms()] == [
        "federated-assertion-1",
        "federated-assertion-2",
    ]
    assert len(entra.discoveries) == 1

    monkeypatch.delenv(FEDERATED_FILE_ENV)
    assert _federated_credential(_Entra(), assertion=assertion).get_token().token == ISSUED


def test_a_rewritten_token_file_is_sent_when_the_client_refreshes_its_token(
    token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [NOW]
    monkeypatch.setattr(
        client_module, "_ProviderAuth", functools.partial(_ProviderAuth, clock=lambda: now[0])
    )
    entra = _Entra()
    credential = _federated_credential(entra, clock=lambda: now[0])
    client = Client.for_service_account(API, credential, transport=entra.transport)

    client.request("GET", "/v1/x")
    token_file.write_text(f"{FEDERATED_B}\n", encoding="utf-8")  # rotated in place
    client.request("GET", "/v1/x")  # the cached access token is still good: no token request
    assert [form["client_assertion"] for form in entra.forms()] == [FEDERATED_A]

    now[0] = NOW + 3599 - _REFRESH_SKEW_SECONDS  # expires_at - the refresh skew
    client.request("GET", "/v1/x")

    assert [form["client_assertion"] for form in entra.forms()] == [FEDERATED_A, FEDERATED_B]
    assert [r.headers["authorization"] for r in entra.api_requests] == [f"Bearer {ISSUED}"] * 3


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_no_assertion_and_no_token_file_variable_is_refused_at_construction(
    value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if value is None:
        monkeypatch.delenv(FEDERATED_FILE_ENV, raising=False)
    else:
        monkeypatch.setenv(FEDERATED_FILE_ENV, value)
    entra = _Entra()
    with pytest.raises(ValueError, match=FEDERATED_FILE_ENV):
        EntraFederatedCredential(TENANT, CLIENT_ID, SCOPE, transport=entra.transport)
    assert entra.discoveries == entra.token_requests == []


@pytest.mark.parametrize(("tenant_id", "authority_host"), _UNPINNABLE)
def test_a_federated_credential_that_cannot_be_pinned_is_refused_at_construction(
    tenant_id: str, authority_host: str, token_file: Path
) -> None:
    entra = _Entra()
    assertion, calls = _counting()
    for options in ({}, {"assertion": assertion}):
        with pytest.raises(ValueError):
            EntraFederatedCredential(
                tenant_id,
                CLIENT_ID,
                SCOPE,
                authority_host=authority_host,
                transport=entra.transport,
                **options,
            )
    assert entra.discoveries == entra.token_requests == []
    assert calls == []


@pytest.mark.parametrize(("issuer", "token_endpoint"), _FOREIGN_DISCOVERY)
def test_a_discovery_document_that_is_not_the_tenants_asks_for_no_federated_assertion(
    issuer: str, token_endpoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entra = _Entra(document={"issuer": issuer, "token_endpoint": token_endpoint})
    assertion, calls = _counting()
    with pytest.raises(RuntimeError, match="no credential was sent"):
        _federated_credential(entra, assertion=assertion).get_token()
    assert len(entra.discoveries) == 1
    assert entra.token_requests == []
    assert calls == []  # the assertion was not even asked for

    # Nor is the token file read: a missing one would raise "could not be read" instead.
    monkeypatch.setenv(FEDERATED_FILE_ENV, str(tmp_path / "does-not-exist"))
    with pytest.raises(RuntimeError, match="no credential was sent"):
        _federated_credential(entra).get_token()
    assert entra.token_requests == []


def test_no_federated_token_reaches_a_repr_an_error_or_a_log(
    token_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def refusal(form: dict[str, str]) -> dict[str, Any]:
        # Entra's body is not ours to repeat: plant the assertion the request carried in it.
        return {
            "error": "invalid_client",
            "error_description": f"AADSTS700213: assertion {form['client_assertion']} rejected",
            "access_token": ISSUED,
        }

    entra = _Entra(token_status=400, token_body=refusal)
    credential = _federated_credential(entra)
    client = Client.for_service_account(API, credential, transport=entra.transport)

    with pytest.raises(TokenProviderError) as raised:
        client.request("GET", "/v1/x")

    assert entra.api_requests == []
    assert entra.forms()[0]["client_assertion"] == FEDERATED_A
    chain = _chain(raised.value)
    assert len(chain) >= 2
    assert "invalid_client" in str(chain[1])
    assert "AADSTS700213" not in str(chain[1])
    exposed = [repr(credential), caplog.text]
    exposed += [str(error) for error in chain] + [repr(error) for error in chain]
    exposed += [str(record.args) for record in caplog.records]
    for secret in (FEDERATED_A, ISSUED):
        for text in exposed:
            assert secret not in text
    assert repr(credential) == (
        f"EntraFederatedCredential(tenant_id={TENANT!r}, client_id={CLIENT_ID!r})"
    )


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param(lambda path: path.write_text("", encoding="utf-8"), id="empty"),
        pytest.param(lambda path: path.write_text(" \n\t ", encoding="utf-8"), id="blank"),
        pytest.param(
            lambda path: path.write_bytes(b"\xff" + FEDERATED_A.encode("ascii")), id="not-utf-8"
        ),
        pytest.param(lambda path: path.unlink(), id="missing"),
        pytest.param(lambda path: (path.unlink(), path.mkdir()), id="a-directory"),
    ],
)
def test_an_empty_or_unreadable_token_file_is_refused_without_its_content(
    spoil: Callable[[Path], object], token_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    spoil(token_file)
    entra = _Entra()
    credential = _federated_credential(entra)
    with pytest.raises((ValueError, RuntimeError)) as raised:
        credential.get_token()
    assert entra.token_requests == []
    assert raised.value.__context__ is None  # the error holding the content is not kept
    assert raised.value.__cause__ is None
    for error in _chain(raised.value):
        assert FEDERATED_A not in str(error) + repr(error)
    assert FEDERATED_A not in caplog.text


@pytest.mark.parametrize(
    ("value", "error"),
    [
        pytest.param(" \n", ValueError, id="blank"),
        pytest.param(42, TypeError, id="not-a-string"),
        pytest.param(FEDERATED_A.encode("ascii"), TypeError, id="bytes"),
    ],
)
def test_a_callable_that_returns_no_token_is_refused_without_repeating_it(
    value: object, error: type[Exception]
) -> None:
    entra = _Entra()
    assertion, calls = _counting(value)
    with pytest.raises(error) as raised:
        _federated_credential(entra, assertion=assertion).get_token()
    assert calls == [1]
    assert entra.token_requests == []
    assert FEDERATED_A not in str(raised.value) + repr(raised.value)


def test_the_federated_credential_needs_no_cryptography(
    token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "cryptography", None)
    for name in [n for n in sys.modules if n == "masterly" or n.startswith("masterly.")]:
        monkeypatch.delitem(sys.modules, name)  # restored when the test ends
    fresh = importlib.import_module("masterly")
    assert fresh is not masterly
    assert "EntraFederatedCredential" in fresh.__all__

    entra = _Entra()
    credential = fresh.EntraFederatedCredential(TENANT, CLIENT_ID, SCOPE, transport=entra.transport)
    assert credential.get_token().token == ISSUED
    assert entra.forms()[0]["client_assertion"] == FEDERATED_A
