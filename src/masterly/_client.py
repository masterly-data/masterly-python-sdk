"""The client shell: connection, auth, per-request headers, error mapping. Every call carries the
bearer token and the Environment header; errors surface the server's error envelope verbatim —
code, message and `details`, which is where a governed write's refusal explains itself."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

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
        token: A session or service-account bearer token.
        environment: The Environment id, e.g. ``env_prod_eu``.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        environment: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        from masterly import __version__

        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "X-Masterly-Environment": environment,
                "User-Agent": f"masterly-python/{__version__}",
            },
            timeout=_TIMEOUT,
            transport=transport,
        )
        self.products = ProductsApi(self)
        self.sources = SourcesApi(self)
        self.golden = GoldenApi(self)
        self.workspaces = WorkspacesApi(self)
        self.domains = DomainsApi(self)
        self.data_models = DataModelsApi(self)

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
