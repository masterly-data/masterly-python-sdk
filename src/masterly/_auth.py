"""Token providers: a bearer token fetched when it is needed, not fixed when the client is built.

A long-running client — a pipeline, a notebook session — outlives the access token it started
with. Hand :class:`~masterly.Client` a :class:`TokenProvider` instead of a string and every
request asks it for a token; the last one is kept and reused until it is within
``_REFRESH_SKEW_SECONDS`` of expiring, then a new one is fetched before the request is sent.

A token value never reaches a repr, an exception message or a log record from here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Protocol

import httpx

#: Seconds before ``expires_at`` at which a cached token is treated as expired and replaced, so a
#: request never leaves with a token that lapses on the way.
_REFRESH_SKEW_SECONDS = 60.0


@dataclass(frozen=True)
class AccessToken:
    """A bearer access token and when it expires.

    Args:
        token: The access token itself. It is left out of the repr.
        expires_at: When the token expires, in seconds since the epoch (``time.time()``).
    """

    token: str = field(repr=False)
    expires_at: float


class TokenProvider(Protocol):
    """Anything with a ``get_token()`` that returns an :class:`AccessToken`.

    No base class to inherit: an object with that one method is a provider. The client calls it
    when it holds no token yet, or when the one it holds is about to expire — so it may fetch
    from an identity provider each time it is called. A credential from ``azure-identity``
    adapts in a few lines::

        class EntraTokens:
            def __init__(self, credential, scope):
                self._credential, self._scope = credential, scope

            def get_token(self) -> masterly.AccessToken:
                issued = self._credential.get_token(self._scope)
                return masterly.AccessToken(issued.token, float(issued.expires_on))

    If ``get_token()`` raises, the request is not sent and the exception reaches the caller.
    """

    def get_token(self) -> AccessToken: ...


class _ProviderAuth(httpx.Auth):
    """Attaches the provider's bearer to every request, re-sends included, fetching a new token
    when the cached one is within the refresh skew of expiring. Safe across threads: concurrent
    requests on one client wait for a single fetch."""

    def __init__(self, provider: TokenProvider, *, clock: Callable[[], float] = time.time) -> None:
        self._provider = provider
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: AccessToken | None = None

    def _bearer(self) -> str:
        with self._lock:
            cached = self._cached
            if cached is None or self._clock() >= cached.expires_at - _REFRESH_SKEW_SECONDS:
                cached = self._provider.get_token()
                self._cached = cached
            return cached.token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._bearer()}"
        yield request

    def __repr__(self) -> str:
        return f"_ProviderAuth(provider={type(self._provider).__name__})"
