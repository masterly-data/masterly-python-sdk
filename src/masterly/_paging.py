"""Cursor paging for the small listings — the config objects, not the data rows.

Data rows page lazily through :class:`~masterly.RowPages`, because a product read is
unbounded and a notebook wants to start consuming before the last page arrives. A listing of
workspaces, domains, models or sources is bounded by what a human configured, so it is
gathered eagerly into a list: the caller almost always wants to search it by name, and a
generator that has to be fully drained to answer "is it there?" only adds a step.

The limit is the API's maximum (200). A listing that fits in one page therefore costs one
request, which is the common case; the loop exists so that the hundredth source does not
silently vanish from a name lookup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from masterly._client import Client

_PAGE_SIZE = 200


def all_items(
    client: Client, path: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Every item of a cursor-paginated listing, following ``next_cursor`` to the end."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page_params: dict[str, Any] = {**(params or {}), "limit": _PAGE_SIZE}
        if cursor:
            page_params["cursor"] = cursor
        page = client._request("GET", path, params=page_params)
        items.extend(page.get("items", []))
        cursor = page.get("next_cursor")
        if not cursor:
            return items


def resolve(
    items: list[dict[str, Any]], value: str, *, id_field: str, prefix: str, kind: str
) -> str:
    """Accept an id or an exact name, and answer with the id.

    Names are what a person types and what a config document refers to; ids are what the API
    addresses. Every typed method here takes either, so a script reads the way the domain is
    discussed rather than the way it is stored.
    """
    if value.startswith(prefix):
        return value
    for item in items:
        if item.get("name") == value:
            return str(item[id_field])
    raise LookupError(f"no {kind} named '{value}'")
