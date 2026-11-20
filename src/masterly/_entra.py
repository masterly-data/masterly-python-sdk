"""Entra ID client credentials for a service identity: a certificate or a federated credential.

Each is a :class:`~masterly.TokenProvider`. It runs the client-credentials grant against the
token endpoint of one Entra tenant and hands the issued token to the client, which keeps it
until shortly before it expires. Neither carries a client secret.

Where a credential goes is pinned before anything is sent. The tenant must be a GUID, because
``common``, ``organizations``, ``consumers`` or a domain name name no single issuer. The
tenant's discovery document is fetched once per credential from the authority host, over
https. Its ``issuer`` must be exactly ``https://<host>/<tenant>/v2.0``, and its
``token_endpoint`` must be https on that same host. A document that fails either check sends
nothing.

No access token, client assertion, private key, password or federated token reaches a repr, an
exception message or a log record from here. Entra's ``error`` code is named when it refuses a
request; its description and the rest of the body are not repeated.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from masterly._auth import AccessToken

_DEFAULT_AUTHORITY_HOST = "login.microsoftonline.com"
_JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
#: A client assertion is good for this long. Short, so a stolen one is soon worthless.
_ASSERTION_LIFETIME_SECONDS = 300
_TIMEOUT_SECONDS = 30.0
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
#: Entra's OAuth error codes (``invalid_client``, ``unauthorized_client``…). Anything else in
#: that field is not repeated.
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")
_PEM_KEY = re.compile(
    rb"-----BEGIN (RSA |ENCRYPTED )?PRIVATE KEY-----.+?-----END (RSA |ENCRYPTED )?PRIVATE KEY-----",
    re.DOTALL,
)
_FEDERATED_FILE_ENV = "AZURE_FEDERATED_TOKEN_FILE"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _tenant(tenant_id: str) -> str:
    """The tenant GUID in canonical lower-case form, or ValueError."""
    try:
        parsed = uuid.UUID(tenant_id)
    except (ValueError, TypeError, AttributeError):
        parsed = None
    if parsed is None or str(parsed) != str(tenant_id).lower():
        raise ValueError(
            "tenant_id must be the tenant's GUID (e.g. 72f988bf-86f1-41af-91ab-2d7cd011db47) — "
            "'common', 'organizations', 'consumers' and domain names name no single issuer"
        )
    return str(parsed)


def _authority_host(authority_host: str) -> str:
    """The bare host name, from a host or an ``https://host`` URL; ValueError otherwise."""
    host = authority_host
    if "://" in authority_host:
        parts = urlsplit(authority_host)
        if parts.scheme != "https":
            raise ValueError("authority_host must be reached over https")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("authority_host must be a host name, with no path or query")
        host = parts.netloc
    host = host.lower()
    if not _HOST.fullmatch(host):
        raise ValueError("authority_host must be a host name, e.g. login.microsoftonline.com")
    return host


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    """The response's JSON object, or None. Never raises, so no exception holding the body is
    left as the context of the one raised next."""
    try:
        value = response.json()
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class _EntraCredential:
    """Discovery, its checks and the token request, shared by both credentials."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        scope: str,
        *,
        authority_host: str,
        transport: httpx.BaseTransport | None,
        clock: Callable[[], float],
    ) -> None:
        self._tenant_id = _tenant(tenant_id)
        self._host = _authority_host(authority_host)
        if not client_id:
            raise ValueError("client_id is required")
        if not scope.endswith("/.default"):
            raise ValueError(
                "scope must be the API's '<application id URI>/.default' — the "
                "client-credentials grant asks for the permissions granted to the app"
            )
        self._client_id = client_id
        self._scope = scope
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._token_endpoint: str | None = None

    @property
    def _issuer(self) -> str:
        return f"https://{self._host}/{self._tenant_id}/v2.0"

    def _client_assertion(self, token_endpoint: str) -> str:
        raise NotImplementedError

    def get_token(self) -> AccessToken:
        with httpx.Client(
            transport=self._transport, timeout=_TIMEOUT_SECONDS, follow_redirects=False
        ) as http:
            token_endpoint = self._discover(http)
            assertion = self._client_assertion(token_endpoint)
            now = self._clock()
            response = http.post(
                token_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "scope": self._scope,
                    "client_assertion_type": _JWT_BEARER,
                    "client_assertion": assertion,
                },
            )
        body = _json_object(response)
        if response.status_code != 200:
            code = body.get("error") if body is not None else None
            named = f" ({code})" if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""
            raise RuntimeError(
                f"Entra refused the token request with HTTP {response.status_code}{named}"
            )
        token = body.get("access_token") if body is not None else None
        expires_in = body.get("expires_in") if body is not None else None
        if isinstance(expires_in, str) and expires_in.isdigit():
            expires_in = int(expires_in)
        if not isinstance(token, str) or not token:
            raise RuntimeError("Entra's token response carries no access_token")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not 0 < expires_in < float("inf")
        ):
            raise RuntimeError("Entra's token response carries no usable expires_in")
        return AccessToken(token, now + expires_in)

    def _discover(self, http: httpx.Client) -> str:
        """The tenant's token endpoint, fetched once and checked before it is trusted."""
        with self._lock:
            if self._token_endpoint is None:
                self._token_endpoint = self._fetch_token_endpoint(http)
            return self._token_endpoint

    def _fetch_token_endpoint(self, http: httpx.Client) -> str:
        response = http.get(f"{self._issuer}/.well-known/openid-configuration")
        refused = "no credential was sent"
        if response.status_code != 200:
            raise RuntimeError(
                f"Entra discovery for tenant {self._tenant_id} answered HTTP "
                f"{response.status_code}; {refused}"
            )
        document = _json_object(response)
        if document is None:
            raise RuntimeError(f"Entra discovery did not answer a JSON object; {refused}")
        if document.get("issuer") != self._issuer:
            raise RuntimeError(
                f"Entra discovery does not name the issuer {self._issuer}; {refused}"
            )
        endpoint = document.get("token_endpoint")
        if not isinstance(endpoint, str) or not self._on_authority_host(endpoint):
            raise RuntimeError(
                f"Entra discovery names a token_endpoint that is not https on {self._host}; "
                f"{refused}"
            )
        return endpoint

    def _on_authority_host(self, url: str) -> bool:
        parts = urlsplit(url)
        # netloc, not hostname: a user-info or port part does not pass for the host.
        return parts.scheme == "https" and parts.netloc.lower() == self._host

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(tenant_id={self._tenant_id!r}, client_id={self._client_id!r})"
        )


def _pem(value: bytes | str | os.PathLike[str], what: str) -> bytes:
    """PEM content as given, or read from a path. A failure names neither: a string that was
    meant as PEM text and taken for a path would put the key in the error."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str) and "-----BEGIN" in value:
        return value.encode("utf-8", errors="replace")
    try:
        return Path(value).read_bytes()
    except (OSError, ValueError):
        pass  # raised below, so the error holding the value is not left as context
    raise ValueError(f"the {what} is neither PEM text nor a readable file")


def _read_text(path: Path) -> str:
    """The file's UTF-8 text. A decode error would carry the file's bytes, so it is not kept."""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        pass
    raise RuntimeError(f"the federated token file {path} could not be read as UTF-8 text")


class EntraCertificateCredential(_EntraCredential):
    """A service identity's token from Entra ID, proven with a certificate.

    Each token request carries a client assertion (RFC 7523): a JWT signed RS256 with the
    certificate's private key, naming the certificate by its SHA-256 thumbprint (``x5t#S256``),
    addressed to the token endpoint and good for five minutes.

    Needs the ``entra`` extra (``pip install 'masterly[entra]'``), which adds ``cryptography``.

    Args:
        tenant_id: The Entra tenant's GUID.
        client_id: The app registration's application (client) id.
        scope: The Masterly API's scope, ``<application id URI>/.default``.
        certificate: The certificate, PEM — as bytes or text, or the path to a file. The file may
            hold the private key as well.
        private_key: The RSA private key, PEM, as bytes or text or a path. Leave it out when
            ``certificate`` holds it.
        password: The private key's password, if it is encrypted.
        authority_host: The Entra authority host. Use the default unless the tenant lives in a
            national cloud.
        transport: An httpx transport, for tests.
        clock: Seconds since the epoch, for tests.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        scope: str,
        certificate: bytes | str | os.PathLike[str],
        *,
        private_key: bytes | str | os.PathLike[str] | None = None,
        password: bytes | str | None = None,
        authority_host: str = _DEFAULT_AUTHORITY_HOST,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError as exc:
            raise ImportError(
                "EntraCertificateCredential needs cryptography — install the extra: "
                "pip install 'masterly[entra]'"
            ) from exc
        super().__init__(
            tenant_id,
            client_id,
            scope,
            authority_host=authority_host,
            transport=transport,
            clock=clock,
        )
        certificate_pem = _pem(certificate, "certificate")
        key_pem = certificate_pem if private_key is None else _pem(private_key, "private_key")
        found = _PEM_KEY.search(key_pem)
        if found is None:
            raise ValueError("no PEM private key was found — pass private_key=")
        secret = password.encode("utf-8") if isinstance(password, str) else password
        loaded = None
        try:
            loaded = serialization.load_pem_private_key(found.group(0), password=secret)
        except (ValueError, TypeError):
            pass  # raised below, so the loader's exception is not left as context
        if loaded is None:
            raise ValueError("the private key could not be loaded — is the password right?")
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise ValueError("the private key must be an RSA key (RS256)")
        cert = x509.load_pem_x509_certificate(certificate_pem)
        spki = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if cert.public_key().public_bytes(*spki) != loaded.public_key().public_bytes(*spki):
            raise ValueError("the private key does not belong to the certificate")
        der = cert.public_bytes(serialization.Encoding.DER)
        self._thumbprint = _b64url(hashlib.sha256(der).digest())
        key = loaded

        def sign(data: bytes) -> bytes:
            return key.sign(data, padding.PKCS1v15(), hashes.SHA256())

        self._sign = sign

    def _client_assertion(self, token_endpoint: str) -> str:
        now = int(self._clock())
        header = {"alg": "RS256", "typ": "JWT", "x5t#S256": self._thumbprint}
        claims = {
            "aud": token_endpoint,
            "iss": self._client_id,
            "sub": self._client_id,
            "jti": str(uuid.uuid4()),
            "nbf": now,
            "iat": now,
            "exp": now + _ASSERTION_LIFETIME_SECONDS,
        }
        signing_input = ".".join(
            _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
            for part in (header, claims)
        )
        signature = self._sign(signing_input.encode("ascii"))
        return f"{signing_input}.{_b64url(signature)}"


class EntraFederatedCredential(_EntraCredential):
    """A service identity's token from Entra ID, proven with a federated token.

    The federated token — issued to the workload by its own platform, e.g. a Kubernetes service
    account token under Azure workload identity, or a GitHub Actions OIDC token — is the client
    assertion. It is read afresh on every token request, because its issuer rotates it.

    Args:
        tenant_id: The Entra tenant's GUID.
        client_id: The app registration's (or managed identity's) client id.
        scope: The Masterly API's scope, ``<application id URI>/.default``.
        token_file: The file the federated token is read from. Defaults to the path in
            ``AZURE_FEDERATED_TOKEN_FILE``.
        assertion: A callable returning the federated token, instead of a file.
        authority_host: The Entra authority host. Use the default unless the tenant lives in a
            national cloud.
        transport: An httpx transport, for tests.
        clock: Seconds since the epoch, for tests.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        scope: str,
        *,
        token_file: str | os.PathLike[str] | None = None,
        assertion: Callable[[], str] | None = None,
        authority_host: str = _DEFAULT_AUTHORITY_HOST,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            tenant_id,
            client_id,
            scope,
            authority_host=authority_host,
            transport=transport,
            clock=clock,
        )
        if token_file is not None and assertion is not None:
            raise ValueError("pass token_file or assertion, not both")
        self._assertion: Callable[[], str]
        if assertion is not None:
            self._assertion = assertion
        else:
            path = token_file if token_file is not None else os.environ.get(_FEDERATED_FILE_ENV)
            if not path:
                raise ValueError(
                    f"no federated token: pass token_file= or assertion=, or set "
                    f"{_FEDERATED_FILE_ENV}"
                )
            self._assertion = functools.partial(_read_text, Path(path))

    def _client_assertion(self, token_endpoint: str) -> str:
        value = self._assertion().strip()
        if not value:
            raise RuntimeError("the federated token is empty")
        return value
