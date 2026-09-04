"""The client shell: connection, auth, per-request headers, error mapping. Every call carries the
bearer token, and an Environment header when the connection names one; errors surface the
server's error envelope verbatim — code, message and `details`, which is where a governed write's
refusal explains itself.

Two token personas connect here (see the README). A **session** token belongs to a signed-in
person and must name its Environment. A **service-account** token belongs to a machine: it is
pinned to one Environment by the account itself, so it names none, and it reaches only what its
account holds — ingest into the sources its `ingest` scope lists, and the published products its
linked access principal may consume. Anything else on that connection is a session route, and the
few places where the difference is invisible until the server answers 401 are refused here
instead, with the remedy in the message.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any, Literal

import httpx

from masterly._config import DataModelsApi, DomainsApi, WorkspacesApi
from masterly._extract import GoldenApi, ProductsApi
from masterly._ingest import SourcesApi
from masterly._precondition import (
    OBJECT_REMOVED,
    VERSION_CONFLICT,
    Conflict,
    Precondition,
)
from masterly._precondition import coerce as _coerce_precondition

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_IF_MATCH = "If-Match"

Persona = Literal["session", "service-account"]


class ApiError(Exception):
    """A non-2xx answer from the API, carrying the server's error code, message and details.

    ``details`` is the envelope's own ``details`` object, passed through as the server sent it.
    It is where a refusal explains itself — most sharply on a governed write's 409, where it
    names who moved the object, when, and which fields; see :attr:`conflict` for the typed view.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details) if details else {}
        super().__init__(f"{code} ({status_code}): {message}")

    @property
    def conflict(self) -> Conflict | None:
        """The typed refusal when a governed write was refused, else None.

        Read ``conflict.may_auto_merge`` before merging anything — the gate is
        ``changed_fields_complete``, not a zero ``undisclosed_changes``.
        """
        if self.status_code != 409 or self.code not in (VERSION_CONFLICT, OBJECT_REMOVED):
            return None
        return Conflict.from_details(self.code, self.details)


class Client:
    """Connection to one Masterly Environment.

    Args:
        base_url: The install's URL, e.g. ``https://app.example.com``.
        token: A session bearer token. For a machine credential use
            :meth:`for_service_account` instead — it carries what that persona can and
            cannot do.
        environment: The Environment id, e.g. ``env_prod_eu``. A session token must name
            one; leaving it out sends no ``X-Masterly-Environment`` header, which only the
            Organization-scoped endpoints (``/v1/environments``) and the service-account
            routes accept.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        environment: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        from masterly import __version__

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"masterly-python/{__version__}",
        }
        if environment:
            headers["X-Masterly-Environment"] = environment
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=_TIMEOUT,
            transport=transport,
        )
        #: Which token persona this connection holds — see :meth:`for_service_account`.
        self.persona: Persona = "session"
        #: The Environment named on the connection, or None (a service account is pinned to
        #: its own, and an Organization-scoped call names none).
        self.environment: str | None = environment or None
        self.products = ProductsApi(self)
        self.sources = SourcesApi(self)
        self.golden = GoldenApi(self)
        self.workspaces = WorkspacesApi(self)
        self.domains = DomainsApi(self)
        self.data_models = DataModelsApi(self)

    @classmethod
    def for_service_account(
        cls,
        base_url: str,
        token: str,
        *,
        environment: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> Client:
        """Connect as a **service account** — the machine persona, for a scheduled load.

        A service account is created by an administrator against one Environment, with an
        ``ingest`` scope naming exactly the Sources it may push into (and, through its linked
        access principal, the published products it may consume). It is pinned to that
        Environment, so this connection names none: the account decides where its records
        land, and a token that leaks cannot be pointed somewhere else.

        ``token`` is the OAuth2 client-credentials access token your identity provider issues
        for the account's client (on an install running the dev identity binding, the token the
        create call handed back). What it opens::

            client = Client.for_service_account("https://app.example.com", token)
            client.sources.ingest("src_7f3c9a", records)   # a source its `ingest` scope names
            client.products.read("dp_a1b2c3")              # a product its principal may consume

        Sources and products are addressed **by id**: naming one means listing the
        Environment, which is a session route. A source the scope does not name is refused
        with ``ApiError`` code ``SERVICE_ACCOUNT_SCOPE_DENIED`` — scopes are fixed when the
        account is created, so a new account is the remedy, not a grant. Everything else on
        this connection (configuration, golden records, stewardship) answers 401: those are
        session routes, and this token is not a session.

        Args:
            base_url: The install's URL.
            token: The service account's access token.
            environment: Optional assertion. Leave it out and the account's own Environment is
                used; pass one and the server refuses the call with
                ``SERVICE_ACCOUNT_SCOPE_DENIED`` if the account is pinned elsewhere — useful
                when a job must not silently load a different Environment than intended.
            transport: An httpx transport, for tests.
        """
        client = cls(base_url, token, environment, transport=transport)
        client.persona = "service-account"
        return client

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        if_match: Precondition | str | int | None = None,
    ) -> Any:
        """Call a ``/v1`` endpoint this client has no typed method for, on this connection.

        The typed surface is deliberately narrow: extract, ingest, and the configuration those
        two are defined against. Everything else is reached through here — including every
        governed write that has not earned a method of its own, which is why ``if_match``
        exists. (The one typed method that takes it is
        :meth:`~masterly.Client.data_models.update`.)

        ``if_match`` is the revision this write replaces: pass the ``version`` field of the
        object you read, or a :class:`~masterly.Precondition`. Without it the write still lands
        today and answers with a ``Deprecation`` header, and the day that operation's grace ends
        it answers 428 PRECONDITION_REQUIRED instead — so send it now rather than discovering it
        then. Read, edit, write::

            policy = client.request("GET", "/v1/access-policies/pol_1")
            policy["rules"] = edited
            client.request("PUT", "/v1/access-policies/pol_1", json=policy,
                           if_match=policy["version"])

        A 409 raises :class:`~masterly.ApiError` whose ``conflict`` says what moved. Returns the
        decoded body, or None for a 204 or an empty one.
        """
        return self._request(
            method, path, params=params, json=json, headers=headers, if_match=if_match
        )

    # --- internal ---------------------------------------------------------------------

    def _require_session(self, what: str, remedy: str) -> None:
        """Refuse a session-only call on a machine connection here, where the message can say
        why, rather than letting it come back as an unexplained 401 from a route that never
        saw a session."""
        if self.persona == "service-account":
            raise PermissionError(
                f"{what} needs a session token — this connection is a service account. {remedy}"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        if_match: Precondition | str | int | None = None,
    ) -> Any:
        sent = dict(headers) if headers else {}
        if if_match is not None:
            # Fail closed on two preconditions rather than picking one: which revision this write
            # replaces is not a thing to resolve by precedence.
            if any(name.lower() == _IF_MATCH.lower() for name in sent):
                raise ValueError(
                    "pass the precondition once — either if_match= or an If-Match header"
                )
            sent[_IF_MATCH] = _coerce_precondition(if_match).header_value
        response = self._http.request(method, path, params=params, json=json, headers=sent or None)
        if response.status_code >= 400:
            code, message = "HTTP_ERROR", response.text[:500]
            details: Any = None
            with contextlib.suppress(Exception):  # a non-JSON error body stays a plain HTTP error
                envelope = response.json().get("error", {})
                code = envelope.get("code", code)
                message = envelope.get("message", message)
                details = envelope.get("details")
            raise ApiError(
                response.status_code,
                code,
                message,
                details if isinstance(details, Mapping) else None,
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()
