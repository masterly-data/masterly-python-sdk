"""Extract: data products (paged reads + the resumable change feed) and golden records.

Iterators own the cursor plumbing — a notebook writes ``for row in client.products.read(...)``
and never sees a page. ``to_pandas()`` is available wherever pandas is installed
(``pip install masterly[pandas]``); it is never imported unless asked for.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pandas stays optional at runtime
    import pandas

    from masterly._client import Client

_PAGE_SIZE = 200  # the API caps limit at 200


def _to_pandas(rows: Iterator[dict[str, Any]]) -> pandas.DataFrame:
    try:
        import pandas
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise ImportError(
            "pandas is not installed — install the extra: pip install masterly[pandas]"
        ) from exc
    return pandas.DataFrame(list(rows))


class RowPages:
    """Iterates every row of a product (or golden listing), fetching pages lazily.

    Two modes. A structural read pages over ``GET`` with query parameters. A **data-bearing**
    read — one whose question holds a master-data value, like a search term — pages over
    ``POST`` with the question in the body, per ADR 0069: a query string is written to browser
    history, the client's own access log, the container ingress log and every proxy between,
    and only the last hop is inside the Environment.
    """

    def __init__(
        self,
        client: Client,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        body: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._path = path
        self._params = params or {}
        self._body = body

    def __iter__(self) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            if self._body is not None:
                payload: dict[str, Any] = {**self._body, "limit": _PAGE_SIZE}
                if cursor:
                    payload["cursor"] = cursor
                page = self._client._request("POST", self._path, json=payload)
            else:
                params: dict[str, Any] = {**self._params, "limit": _PAGE_SIZE}
                if cursor:
                    params["cursor"] = cursor
                page = self._client._request("GET", self._path, params=params)
            yield from page.get("items", [])
            cursor = page.get("next_cursor")
            if not cursor:
                return

    def to_pandas(self) -> pandas.DataFrame:
        """All rows as a DataFrame. Loads everything — page manually for huge products."""
        return _to_pandas(iter(self))


class ChangeFeed:
    """The product change feed with a **resumable cursor** (persist ``feed.cursor`` after a
    run and pass it back next time — every change is delivered at least once)."""

    def __init__(self, client: Client, path: str, cursor: str | None) -> None:
        self._client = client
        self._path = path
        self.cursor = cursor

    def __iter__(self) -> Iterator[dict[str, Any]]:
        while True:
            params: dict[str, Any] = {"limit": _PAGE_SIZE}
            if self.cursor:
                params["cursor"] = self.cursor
            page = self._client._request("GET", self._path, params=params)
            items = page.get("items", [])
            yield from items
            next_cursor = page.get("next_cursor")
            if next_cursor:
                self.cursor = next_cursor
            if not next_cursor or not items:
                return


class ProductsApi:
    def __init__(self, client: Client) -> None:
        self._client = client

    def list(self) -> list[dict[str, Any]]:
        """Every data product visible to the caller. Session persona only — the catalogue is a
        session route, while reading a product's rows is what a service account is for."""
        self._client._require_session(
            "listing data products",
            "Keep the product ids (`dp_…`) the account consumes in the job's configuration.",
        )
        page = self._client._request("GET", "/v1/data-products")
        items: list[dict[str, Any]] = page.get("items", [])
        return items

    def _resolve(self, product: str) -> str:
        """Accept a product id (``dp_…``) or its exact name (a name costs a listing, which is a
        session route — so a service-account connection must pass the id)."""
        if product.startswith("dp_"):
            return product
        self._client._require_session(
            f"addressing data product '{product}' by name",
            "Pass the product id (`dp_…`) instead.",
        )
        matches = [p for p in self.list() if p.get("name") == product]
        if not matches:
            raise LookupError(f"no data product named '{product}'")
        product_id: str = matches[0]["product_id"]
        return product_id

    def read(
        self,
        product: str,
        *,
        filters: Sequence[dict[str, Any]] | None = None,
        fields: Sequence[str] | None = None,
        **params: Any,
    ) -> RowPages:
        """Every row of a published product, cursor-paged behind the iterator.

        The consume path authenticates a **service account** and no one else, so this is a
        :meth:`~masterly.Client.for_service_account` connection's read: rows arrive shaped by
        the access policies of the principal the account is linked to, and every read is
        metered and audited against it.

        Shape the read within the product's contract — both arguments take the contract's
        **output** field names, the ones ``GET /v1/data-products/{id}/contract`` lists:

        - ``filters`` — row filters, each ``{"attribute": ..., "op": ..., "values": [...]}``
          with ``op`` one of ``"equals"`` (one value), ``"in"`` (any of the values) or
          ``"contains"`` (case-insensitive substring, one value). Filters are ANDed together
          and with the consumer's row policies, on the server, so a filtered read pages only
          the matching rows and can never widen what the policies allow.
        - ``fields`` — the output fields to return; the others are absent from each row. A
          field the consumer's policy hides is simply left out.

        A name outside the contract raises :class:`~masterly.ApiError` with code
        ``OUT_OF_CONTRACT``; a masked or composed (joined, derived, hierarchy) field can be
        selected but not filtered on (``FIELD_NOT_FILTERABLE``).

        A filter value is master data, so a filtered read travels as ``POST …:read`` with the
        question in the body rather than in a query string, where it would reach access logs
        and every proxy between you and the Environment. Field names are not values, so a
        ``fields``-only read stays a ``GET``, which also keeps it working against an install
        that predates the ``:read`` endpoint.
        """
        product_id = self._resolve(product)
        path = f"/v1/consume/products/{product_id}"
        if filters:
            body: dict[str, Any] = {**params, "filters": filters}
            if fields:
                body["fields"] = fields
            return RowPages(self._client, f"{path}:read", body=body)
        if fields:
            params = {**params, "fields": ",".join(fields)}
        return RowPages(self._client, path, params)

    def changes(self, product: str, cursor: str | None = None) -> ChangeFeed:
        """Changes since ``cursor`` (or from the beginning). Persist ``feed.cursor``."""
        product_id = self._resolve(product)
        return ChangeFeed(self._client, f"/v1/consume/products/{product_id}/changes", cursor)


class GoldenApi:
    def __init__(self, client: Client) -> None:
        self._client = client

    def list(
        self,
        model: str,
        *,
        q: str | None = None,
        filters: Sequence[str] | None = None,
    ) -> RowPages:
        """Golden records of a model — the resolved single view, before any product shaping.

        ``q`` searches across resolved values and ``filters`` takes the API's
        ``attribute:value`` form. Both carry master data, so when either is present the read
        goes over ``POST /v1/golden:search`` with the question in the body rather than in a
        query string (ADR 0069). A model-only listing is structural and stays a ``GET``, which
        also keeps this working against an install that predates the search endpoint.
        """
        if q is None and not filters:
            return RowPages(self._client, "/v1/golden", {"model": model})
        body: dict[str, Any] = {"model": model}
        if q is not None:
            body["q"] = q
        if filters:
            body["filter"] = filters
        return RowPages(self._client, "/v1/golden:search", body=body)
