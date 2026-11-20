"""Entra client credentials: the token request each sends, where it may send it, and no secret
in a repr, an error or a log. The test certificate is generated here; none is committed."""

from __future__ import annotations

import base64
import datetime
import hashlib
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
from masterly import _entra as entra_module

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
CLIENT_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
SCOPE = "api://masterly-test/.default"
HOST = "login.microsoftonline.com"
ISSUER = f"https://{HOST}/{TENANT}/v2.0"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
TOKEN_ENDPOINT = f"https://{HOST}/{TENANT}/oauth2/v2.0/token"
API = "https://app.example.com"
ISSUED = "issued-access-token-9b8a7c"
FEDERATED = "federated-token-4d2e6f"
PASSWORD = b"key-password-5e1f"
JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
NOW = 1_800_000_000.0


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


def _federated_credential(entra: _Entra, **kwargs: Any) -> EntraFederatedCredential:
    options: dict[str, Any] = {"assertion": lambda: FEDERATED, "clock": lambda: NOW, **kwargs}
    return EntraFederatedCredential(TENANT, CLIENT_ID, SCOPE, transport=entra.transport, **options)


def test_the_entra_credentials_are_exported() -> None:
    assert "EntraCertificateCredential" in masterly.__all__
    assert "EntraFederatedCredential" in masterly.__all__
    assert masterly.EntraCertificateCredential is entra_module.EntraCertificateCredential
    assert masterly.EntraFederatedCredential is entra_module.EntraFederatedCredential


def test_the_base_install_gains_no_dependency_and_the_entra_extra_adds_cryptography() -> None:
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^dependencies = \["httpx>=0\.27"\]$', pyproject, re.MULTILINE)
    assert re.search(r'^entra = \["cryptography>=[0-9.]+"\]$', pyproject, re.MULTILINE)


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


def test_an_unreadable_federated_token_file_does_not_echo_its_bytes(tmp_path: Path) -> None:
    token_file = tmp_path / "azure-identity-token"
    token_file.write_bytes(b"\xff" + FEDERATED.encode("ascii"))
    entra = _Entra()
    credential = _federated_credential(entra, assertion=None, token_file=token_file)
    with pytest.raises(RuntimeError, match="could not be read") as raised:
        credential.get_token()
    assert entra.token_requests == []
    for error in _chain(raised.value):
        assert FEDERATED not in str(error) + repr(error)


def test_the_certificate_credential_without_cryptography_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "cryptography", None)
    with pytest.raises(ImportError, match=re.escape("pip install 'masterly[entra]'")):
        EntraCertificateCredential(TENANT, CLIENT_ID, SCOPE, b"-----BEGIN CERTIFICATE-----")


@pytest.mark.parametrize("expires_in", [3599, "3599"])
def test_expires_at_is_the_clock_plus_expires_in(expires_in: int | str) -> None:
    entra = _Entra(token_body=lambda form: {"access_token": ISSUED, "expires_in": expires_in})
    issued = _federated_credential(entra, clock=lambda: 1_000.0).get_token()
    assert issued.token == ISSUED
    assert issued.expires_at == 1_000.0 + 3599


def test_the_federated_token_file_is_reread_on_every_acquisition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "azure-identity-token"
    token_file.write_text("first-federated-token\n", encoding="utf-8")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token_file))
    monkeypatch.setitem(sys.modules, "cryptography", None)  # works without the extra
    entra = _Entra()
    credential = EntraFederatedCredential(TENANT, CLIENT_ID, SCOPE, transport=entra.transport)

    credential.get_token()
    token_file.write_text("second-federated-token\n", encoding="utf-8")
    credential.get_token()

    forms = entra.forms()
    assert [form["client_assertion"] for form in forms] == [
        "first-federated-token",
        "second-federated-token",
    ]
    for form in forms:
        assert form["grant_type"] == "client_credentials"
        assert form["client_id"] == CLIENT_ID
        assert form["scope"] == SCOPE
        assert form["client_assertion_type"] == JWT_BEARER


def test_an_explicit_token_file_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(tmp_path / "not-this-one"))
    token_file = tmp_path / "explicit"
    token_file.write_text(FEDERATED, encoding="utf-8")
    entra = _Entra()
    EntraFederatedCredential(
        TENANT, CLIENT_ID, SCOPE, token_file=token_file, transport=entra.transport
    ).get_token()
    assert entra.forms()[0]["client_assertion"] == FEDERATED


def test_a_callable_supplies_the_federated_token_on_every_acquisition() -> None:
    issued = iter(["callable-token-1", "callable-token-2"])
    entra = _Entra()
    credential = _federated_credential(entra, assertion=lambda: next(issued))
    credential.get_token()
    credential.get_token()
    assert [form["client_assertion"] for form in entra.forms()] == [
        "callable-token-1",
        "callable-token-2",
    ]
    assert all(form["client_assertion_type"] == JWT_BEARER for form in entra.forms())


def test_no_federated_token_source_is_a_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
    with pytest.raises(ValueError, match="AZURE_FEDERATED_TOKEN_FILE"):
        EntraFederatedCredential(TENANT, CLIENT_ID, SCOPE)


@pytest.mark.parametrize("kind", ["certificate", "federated"])
def test_a_service_account_client_sends_the_entra_token_and_the_organization(
    kind: str, request: pytest.FixtureRequest
) -> None:
    entra = _Entra()
    credential = (
        _certificate_credential(request.getfixturevalue("certificate"), entra)
        if kind == "certificate"
        else _federated_credential(entra)
    )
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


@pytest.mark.parametrize("kind", ["certificate", "federated"])
def test_no_secret_reaches_a_repr_an_error_or_a_log(
    kind: str, request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture
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
    secrets = [ISSUED, FEDERATED]
    if kind == "certificate":
        certificate: _Certificate = request.getfixturevalue("certificate")
        credential: Any = _certificate_credential(certificate, entra)
        key_pem = certificate.key_pem.decode("ascii")
        secrets += [PASSWORD.decode(), key_pem, key_pem.splitlines()[1]]
    else:
        credential = _federated_credential(entra)
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
        f"{type(credential).__name__}(tenant_id={TENANT!r}, client_id={CLIENT_ID!r})"
    )


@pytest.mark.parametrize(
    ("issuer", "token_endpoint"),
    [
        pytest.param(
            f"https://{HOST}/11111111-2222-3333-4444-555555555555/v2.0",
            TOKEN_ENDPOINT,
            id="another-tenants-issuer",
        ),
        pytest.param(
            ISSUER, f"http://{HOST}/{TENANT}/oauth2/v2.0/token", id="http-token-endpoint"
        ),
        pytest.param(
            ISSUER,
            f"https://evil.example/{TENANT}/oauth2/v2.0/token",
            id="foreign-host-token-endpoint",
        ),
        pytest.param(f"{ISSUER}/", TOKEN_ENDPOINT, id="issuer-with-trailing-slash"),
        pytest.param(
            ISSUER, f"https://{HOST}@evil.example/token", id="user-info-token-endpoint"
        ),
    ],
)
def test_a_discovery_document_that_is_not_the_tenants_sends_no_credential(
    issuer: str, token_endpoint: str
) -> None:
    asked: list[int] = []
    entra = _Entra(document={"issuer": issuer, "token_endpoint": token_endpoint})
    credential = _federated_credential(entra, assertion=lambda: asked.append(1) or FEDERATED)
    with pytest.raises(RuntimeError, match="no credential was sent"):
        credential.get_token()
    assert len(entra.discoveries) == 1
    assert entra.token_requests == []
    assert asked == []  # the federated token was not even read


@pytest.mark.parametrize(
    ("tenant_id", "authority_host"),
    [
        ("common", HOST),
        ("organizations", HOST),
        ("consumers", HOST),
        ("contoso.onmicrosoft.com", HOST),
        ("not-a-guid", HOST),
        (TENANT, f"http://{HOST}"),
        (TENANT, f"https://{HOST}/{TENANT}"),
    ],
)
def test_a_tenant_or_authority_that_cannot_be_pinned_is_refused_at_construction(
    tenant_id: str, authority_host: str
) -> None:
    entra = _Entra()
    with pytest.raises(ValueError):
        EntraFederatedCredential(
            tenant_id,
            CLIENT_ID,
            SCOPE,
            assertion=lambda: FEDERATED,
            authority_host=authority_host,
            transport=entra.transport,
        )
    assert entra.discoveries == entra.token_requests == []


def test_an_https_authority_host_url_is_accepted() -> None:
    entra = _Entra()
    credential = _federated_credential(entra, authority_host=f"https://{HOST}/")
    assert credential.get_token().token == ISSUED
