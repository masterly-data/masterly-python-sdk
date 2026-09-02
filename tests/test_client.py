"""Unit tests over a mock transport: paging joins cursors, the change feed resumes,
ingest chunks, and the error envelope surfaces as ApiError."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from masterly import ApiError, Client, Precondition


def _client(handler: Any) -> Client:
    return Client(
        base_url="https://masterly.test",
        token="tok",
        environment="env_test",
        transport=httpx.MockTransport(handler),
    )


def test_product_read_pages_through_cursors() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/data-products":
            return httpx.Response(
                200, json={"items": [{"product_id": "dp_1", "name": "Customer 360"}]}
            )
        params = dict(request.url.params)
        calls.append(params)
        assert request.headers["x-masterly-environment"] == "env_test"
        assert request.headers["authorization"] == "Bearer tok"
        if "cursor" not in params:
            return httpx.Response(
                200, json={"items": [{"id": 1}, {"id": 2}], "next_cursor": "c2", "total": 3}
            )
        assert params["cursor"] == "c2"
        return httpx.Response(200, json={"items": [{"id": 3}], "next_cursor": None, "total": 3})

    rows = list(_client(handler).products.read("Customer 360"))
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert len(calls) == 2


def test_change_feed_exposes_resumable_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/data-products":
            return httpx.Response(200, json={"items": [{"product_id": "dp_1", "name": "P"}]})
        params = dict(request.url.params)
        if params.get("cursor") == "start":
            return httpx.Response(
                200,
                json={
                    "items": [{"global_id": "e1", "kind": "upsert"}],
                    "next_cursor": "end",
                },
            )
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    feed = _client(handler).products.changes("P", cursor="start")
    changes = list(feed)
    assert [c["global_id"] for c in changes] == ["e1"]
    assert feed.cursor == "end"  # persist this for the next run


def test_ingest_chunks_batches() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sources":
            return httpx.Response(200, json={"items": [{"source_id": "src_1", "name": "crm"}]})
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"job_id": "j"})

    records = [{"ext_id": str(i)} for i in range(5)]
    report = _client(handler).sources.ingest("crm", records, batch_size=2)
    assert report.records == 5
    assert report.batches == 3
    assert [len(b["records"]) for b in bodies] == [2, 2, 1]
    assert all(b["source_id"] == "src_1" for b in bodies)


def test_error_envelope_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"code": "PERMISSION_DENIED", "message": "sso:manage required"}},
        )

    with pytest.raises(ApiError) as excinfo:
        _client(handler).sources.list()
    assert excinfo.value.code == "PERMISSION_DENIED"
    assert excinfo.value.status_code == 403


def test_error_details_reach_the_caller() -> None:
    """The envelope's `details` is where a refusal explains itself. Dropping it left a caller
    holding a message string and nothing to act on."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "too many requests",
                    "details": {"retry_after_seconds": 30},
                }
            },
        )

    with pytest.raises(ApiError) as excinfo:
        _client(handler).sources.list()
    assert excinfo.value.details["retry_after_seconds"] == 30
    assert excinfo.value.conflict is None  # not a governed write's refusal


def test_unknown_name_raises_lookup_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    with pytest.raises(LookupError):
        _client(handler).products.read("Nope")


def test_to_pandas_builds_a_dataframe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/data-products":
            return httpx.Response(200, json={"items": [{"product_id": "dp_1", "name": "P"}]})
        return httpx.Response(
            200, json={"items": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], "next_cursor": None}
        )

    df = _client(handler).products.read("P").to_pandas()
    assert list(df.columns) == ["a", "b"]
    assert len(df) == 2


def test_golden_search_travels_in_the_body_not_the_url() -> None:
    """ADR 0069: a search term is master data — a name, an email — and a query string is
    written to the client's own logs, the ingress log and every proxy between, none of which
    are inside the Environment. A data-bearing read is a POST."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [{"global_id": "ent_1"}], "next_cursor": None})

    client = _client(handler)
    rows = list(client.golden.list("Account", q="maria.lindqvist@nordkap.test"))

    assert [r["global_id"] for r in rows] == ["ent_1"]
    request = seen[-1]
    assert request.method == "POST"
    assert request.url.path == "/v1/golden:search"
    assert "nordkap" not in str(request.url), "the value must not reach the address"
    assert json.loads(request.content)["q"] == "maria.lindqvist@nordkap.test"


def test_golden_filters_also_travel_in_the_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    client = _client(handler)
    list(client.golden.list("Account", filters=["email:maria@nordkap.test"]))

    request = seen[-1]
    assert request.method == "POST"
    assert "nordkap" not in str(request.url)
    assert json.loads(request.content)["filter"] == ["email:maria@nordkap.test"]


def test_structural_golden_listing_stays_a_get() -> None:
    """A model-only listing carries no master data, so it keeps the plain GET — which also
    keeps the SDK working against an install that predates the search endpoint."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    client = _client(handler)
    list(client.golden.list("Account"))

    assert seen[-1].method == "GET"
    assert seen[-1].url.path == "/v1/golden"
    assert dict(seen[-1].url.params)["model"] == "Account"


def test_post_paging_carries_the_cursor_in_the_body() -> None:
    """The cursor is structural, but it rides the body on a POST read — one request describes
    one query, and the address stays free of the question entirely."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("cursor") is None:
            return httpx.Response(200, json={"items": [{"i": 1}], "next_cursor": "c2"})
        return httpx.Response(200, json={"items": [{"i": 2}], "next_cursor": None})

    client = _client(handler)
    rows = list(client.golden.list("Account", q="x"))

    assert [r["i"] for r in rows] == [1, 2]
    assert bodies[0].get("cursor") is None
    assert bodies[1]["cursor"] == "c2"
    assert all("cursor" not in str(b) or b.get("q") == "x" for b in bodies)


# --- ADR 0070: the precondition on a governed write ------------------------------------------


def _refuses_with(status: int, code: str, details: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": code, "message": "refused", "details": details}}
        )

    return handler


_CONFLICT = {
    "object_type": "access-policy",
    "object_id": "pol_1",
    "base_version": "7",
    "current_version": "8",
    "changed_by": "maria.lindqvist@nordkap.test",
    "changed_at": "2026-08-05T09:14:22Z",
    "changed_fields": ["rules.row_filter"],
    "undisclosed_changes": 1,
    "changed_fields_complete": False,
    "request_id": "req_01J",
}


def test_version_conflict_surfaces_as_a_typed_refusal() -> None:
    with pytest.raises(ApiError) as excinfo:
        _client(_refuses_with(409, "VERSION_CONFLICT", _CONFLICT)).request(
            "PUT", "/v1/access-policies/pol_1", json={}, if_match=7
        )

    conflict = excinfo.value.conflict
    assert conflict is not None
    assert conflict.object_type == "access-policy"
    assert conflict.base_version == "7"
    assert conflict.current_version == "8"
    assert conflict.changed_by == "maria.lindqvist@nordkap.test"
    assert conflict.changed_at == datetime(2026, 8, 5, 9, 14, 22, tzinfo=timezone.utc)
    assert conflict.changed_fields == ("rules.row_filter",)
    assert conflict.undisclosed_changes == 1
    assert not conflict.removed
    assert not conflict.may_auto_merge  # a field was withheld, so disjointness is unknowable


def test_auto_merge_gates_on_completeness_and_not_on_the_count() -> None:
    """The trap this SDK must not walk a caller into. `undisclosed_changes: 0` means nothing was
    withheld from THIS reader — and it is equally what you get when the writer recorded no field
    list at all, which is unknown rather than empty. Gating on the count auto-merges over changes
    nobody enumerated; the gate is `changed_fields_complete`."""
    unknown = {**_CONFLICT, "changed_fields": [], "undisclosed_changes": 0}

    with pytest.raises(ApiError) as excinfo:
        _client(_refuses_with(409, "VERSION_CONFLICT", unknown)).request("PUT", "/v1/x", json={})
    conflict = excinfo.value.conflict
    assert conflict is not None
    assert conflict.undisclosed_changes == 0
    assert not conflict.may_auto_merge

    disclosed = {**unknown, "changed_fields_complete": True}
    with pytest.raises(ApiError) as excinfo:
        _client(_refuses_with(409, "VERSION_CONFLICT", disclosed)).request("PUT", "/v1/x", json={})
    assert excinfo.value.conflict is not None
    assert excinfo.value.conflict.may_auto_merge


def test_a_missing_completeness_gate_reads_as_closed() -> None:
    """An older install, or a field the server has not started sending: absent must not read as
    permission to merge."""
    with pytest.raises(ApiError) as excinfo:
        _client(_refuses_with(409, "VERSION_CONFLICT", {"object_type": "saved-view"})).request(
            "DELETE", "/v1/x"
        )
    conflict = excinfo.value.conflict
    assert conflict is not None
    assert not conflict.changed_fields_complete
    assert not conflict.may_auto_merge
    assert conflict.changed_fields == ()


def test_object_removed_is_structural_and_never_mergeable() -> None:
    removed = {**_CONFLICT, "current_version": None, "changed_fields_complete": True}
    with pytest.raises(ApiError) as excinfo:
        _client(_refuses_with(409, "OBJECT_REMOVED", removed)).request("PUT", "/v1/x", json={})
    conflict = excinfo.value.conflict
    assert conflict is not None
    assert conflict.removed
    assert conflict.current_version is None
    assert not conflict.may_auto_merge  # there is nothing left to merge into


def test_precondition_travels_as_a_strong_entity_tag() -> None:
    """A read model's `version` is a bare token — an integer, in most of them — and `If-Match`
    takes only a quoted strong tag. Echoing the field straight into the header is 400
    PRECONDITION_MALFORMED, one round trip after the mistake."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"version": 8})

    client = _client(handler)
    client.request("PUT", "/v1/access-policies/pol_1", json={}, if_match=7)
    assert seen[-1].headers["if-match"] == '"7"'

    from_header = Precondition.from_etag('"7"')  # the same value, taken from the ETag instead
    client.request("PUT", "/v1/access-policies/pol_1", json={}, if_match=from_header)
    assert seen[-1].headers["if-match"] == '"7"'

    client.request("DELETE", "/v1/access-policies/pol_1", if_match=Precondition.unconditional())
    assert seen[-1].headers["if-match"] == "*"


def test_a_version_that_is_an_entity_tag_is_refused_before_the_wire() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("the request must not be sent")

    with pytest.raises(ValueError, match="entity-tag"):
        _client(handler).request("PUT", "/v1/x", json={}, if_match='"7"')
    with pytest.raises(ValueError, match="weak validator"):
        Precondition.from_version('W/"7"')
    with pytest.raises(ValueError, match="one revision"):
        Precondition.from_version("7,8")


def test_per_request_headers_ride_alongside_the_connection_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    _client(handler).request("POST", "/v1/x", json={}, headers={"Idempotency-Key": "key_1"})
    assert seen[-1].headers["idempotency-key"] == "key_1"
    assert seen[-1].headers["x-masterly-environment"] == "env_test"
    assert seen[-1].headers["authorization"] == "Bearer tok"


def test_two_preconditions_on_one_write_are_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("the request must not be sent")

    with pytest.raises(ValueError, match="once"):
        _client(handler).request("PUT", "/v1/x", json={}, headers={"if-match": '"7"'}, if_match=8)
