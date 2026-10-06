"""A Source's lifecycle over a mock transport: deactivate and reactivate as governed writes,
the deletion impact read before a delete, the delete as a job — and the refusals each meets,
raised as the typed error."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from masterly import ApiError, Client, Precondition


def _client(handler: Any) -> Client:
    return Client(
        "https://masterly.test",
        token="tok",
        environment="env_prod_eu",
        transport=httpx.MockTransport(handler),
    )


def _source(status: str, version: int) -> dict[str, Any]:
    return {
        "source_id": "src_1",
        "name": "crm",
        "display_name": "Salesforce CRM",
        "status": status,
        "version": str(version),
        "built_in": False,
    }


def _refuses_with(status: int, code: str, details: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": code, "message": "refused", "details": details}}
        )

    return handler


# --- deactivate and reactivate -----------------------------------------------------------


def test_deactivate_posts_the_colon_action_with_the_revision_it_replaces() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_source("inactive", 4))

    source = _client(handler).sources.deactivate("src_1", if_match=3)

    assert [(r.method, r.url.path) for r in seen] == [("POST", "/v1/sources/src_1:deactivate")]
    assert seen[0].headers["if-match"] == '"3"'
    assert not seen[0].content, "the action takes no body"
    assert source["status"] == "inactive"
    assert source["version"] == "4"


def test_reactivate_posts_the_colon_action_and_returns_the_active_source() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_source("active", 5))

    source = _client(handler).sources.reactivate("src_1", if_match=Precondition.from_version("4"))

    assert [(r.method, r.url.path) for r in seen] == [("POST", "/v1/sources/src_1:reactivate")]
    assert seen[0].headers["if-match"] == '"4"'
    assert source["status"] == "active"


def test_the_precondition_is_optional_and_then_no_if_match_is_sent() -> None:
    """The unguarded form the server still accepts (with a Deprecation header), as on
    sources.update — and the form a retry of an action that already landed needs, since the
    server answers the current view before it reads the header."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_source("inactive", 4))

    _client(handler).sources.deactivate("src_1")

    assert "if-match" not in seen[0].headers


def test_a_source_is_addressed_by_name_on_a_session_connection() -> None:
    """A name costs a listing first, exactly as every other typed source method pays it."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/sources":
            return httpx.Response(
                200,
                json={
                    "items": [{"source_id": "src_0", "name": "erp"}, _source("active", 1)],
                    "next_cursor": None,
                },
            )
        return httpx.Response(200, json=_source("inactive", 2))

    source = _client(handler).sources.deactivate("crm", if_match=1)

    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/v1/sources"),
        ("POST", "/v1/sources/src_1:deactivate"),
    ]
    assert source["status"] == "inactive"


def test_a_stale_revision_on_deactivate_is_the_typed_conflict() -> None:
    conflict = {
        "object_type": "source",
        "object_id": "src_1",
        "base_version": "3",
        "current_version": "4",
        "changed_by": "maria.lindqvist@nordkap.test",
        "changed_at": "2026-10-06T09:14:22Z",
        "changed_fields": ["display_name"],
        "changed_fields_complete": True,
        "undisclosed_changes": 0,
    }
    with pytest.raises(ApiError) as refused:
        _client(_refuses_with(409, "VERSION_CONFLICT", conflict)).sources.deactivate(
            "src_1", if_match=3
        )

    assert refused.value.status_code == 409
    typed = refused.value.conflict
    assert typed is not None
    assert typed.current_version == "4"
    assert typed.changed_fields == ("display_name",)


def test_the_built_in_manual_source_is_refused_and_is_not_a_merge_conflict() -> None:
    with pytest.raises(ApiError) as refused:
        _client(_refuses_with(409, "SOURCE_BUILT_IN", {"source_id": "src_m"})).sources.deactivate(
            "src_m", if_match=1
        )

    assert refused.value.code == "SOURCE_BUILT_IN"
    assert refused.value.conflict is None  # structural: nothing to re-read and merge


def test_an_inactive_source_refuses_ingest_and_the_refusal_names_its_status() -> None:
    """The 409 every way in answers while the source is inactive, with nothing queued."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "SOURCE_INACTIVE",
                    "message": "The source has been deactivated and admits nothing",
                    "details": {"source_id": "src_1", "status": "inactive"},
                }
            },
        )

    with pytest.raises(ApiError) as refused:
        _client(handler).sources.ingest("src_1", [{"customer_number": "C-1"}])

    assert refused.value.status_code == 409
    assert refused.value.code == "SOURCE_INACTIVE"
    assert refused.value.details == {"source_id": "src_1", "status": "inactive"}
    assert refused.value.conflict is None
    assert len(sent) == 1, "a refusal is an answer, never retried under the key"


# --- deletion impact and delete ----------------------------------------------------------


_IMPACT = {
    "source_id": "src_1",
    "name": "crm",
    "display_name": "Salesforce CRM",
    "targets": [{"model_name": "Customer", "records": 1200, "tombstoned": 8, "quarantined": 3}],
    "erased_records": 0,
    "golden": {"would_change": 340, "would_clear": 12},
    "open_tasks": 2,
    "open_incidents": 0,
    "dependents": [],
    "service_account_scopes": [{"service_account_id": "svc_1", "name": "lakehouse-loader"}],
    "deletable": True,
}


def test_deletion_impact_is_a_read_of_what_a_delete_would_touch() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_IMPACT)

    impact = _client(handler).sources.deletion_impact("src_1")

    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/sources/src_1/deletion-impact")]
    assert impact["deletable"] is True
    assert impact["golden"] == {"would_change": 340, "would_clear": 12}
    assert impact["targets"][0]["records"] == 1200


def test_delete_posts_the_colon_action_and_returns_the_job_receipt() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"job_id": "job_delete"})

    receipt = _client(handler).sources.delete("src_1", if_match=4)

    assert [(r.method, r.url.path) for r in seen] == [("POST", "/v1/sources/src_1:delete")]
    assert seen[0].headers["if-match"] == '"4"'
    assert "idempotency-key" not in seen[0].headers, (
        "a repeat is absorbed by the server — a source already deleting answers the pending "
        "job's receipt — so the delete carries no key"
    )
    assert receipt == {"job_id": "job_delete"}


def test_the_deprecated_delete_route_is_never_sent() -> None:
    """`DELETE /v1/sources/{id}` is deprecated in favour of the job; the client only ever
    sends the job form, so no call of it can land on the route that deletes in the request."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"job_id": "job_delete"})

    _client(handler).sources.delete("src_1")

    assert all(r.method == "POST" for r in seen)


def test_a_delete_is_followed_to_its_outcome_with_jobs_wait() -> None:
    statuses = iter(["queued", "running", "succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sources/src_1:delete":
            return httpx.Response(202, json={"job_id": "job_delete"})
        assert request.url.path == "/v1/jobs/job_delete"
        return httpx.Response(
            200, json={"job_id": "job_delete", "kind": "source.delete", "status": next(statuses)}
        )

    client = _client(handler)
    receipt = client.sources.delete("src_1", if_match=4)
    job = client.jobs.wait(receipt["job_id"], timeout=5, interval=0)

    assert job["status"] == "succeeded"


def test_a_source_a_product_reads_is_refused_naming_the_dependents() -> None:
    dependents = [
        {"kind": "data-product", "id": "dp_9", "name": "Customer raw", "references": ["crm"]}
    ]
    with pytest.raises(ApiError) as refused:
        _client(_refuses_with(409, "SOURCE_IN_USE", {"dependents": dependents})).sources.delete(
            "src_1", if_match=4
        )

    assert refused.value.status_code == 409
    assert refused.value.code == "SOURCE_IN_USE"
    assert refused.value.details["dependents"] == dependents
    assert refused.value.conflict is None  # not a revision conflict: re-reading changes nothing


def test_a_stale_revision_on_delete_is_refused_before_anything_is_removed() -> None:
    conflict = {
        "object_type": "source",
        "object_id": "src_1",
        "base_version": "3",
        "current_version": "4",
        "changed_fields": [],
        "changed_fields_complete": False,
        "undisclosed_changes": 0,
    }
    with pytest.raises(ApiError) as refused:
        _client(_refuses_with(409, "VERSION_CONFLICT", conflict)).sources.delete(
            "src_1", if_match=3
        )

    typed = refused.value.conflict
    assert typed is not None
    assert typed.base_version == "3" and typed.current_version == "4"
    assert not typed.may_auto_merge


def test_a_service_account_addresses_a_source_by_id_and_the_server_refuses_the_rest() -> None:
    """No client-side persona check on the lifecycle calls: the refusal is the server's, as it
    is for reading a job. A name is still refused here, since resolving it means listing."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            401, json={"error": {"code": "UNAUTHENTICATED", "message": "A session is required"}}
        )

    machine = Client.for_service_account(
        "https://masterly.test", token="m2m:dev:svc_example", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ApiError, match="UNAUTHENTICATED"):
        machine.sources.deactivate("src_1", if_match=1)
    assert [r.url.path for r in seen] == ["/v1/sources/src_1:deactivate"]

    with pytest.raises(PermissionError, match="source id"):
        machine.sources.delete("crm")
    assert len(seen) == 1, "the name was refused before anything was sent"


def test_a_delete_body_is_empty_and_the_receipt_is_passed_through_as_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"job_id": "job_delete", "extra": "kept"})

    receipt = _client(handler).sources.delete("src_1", if_match="4")

    assert not seen[0].content
    assert json.loads(httpx.Response(202, json=receipt).content) == {
        "job_id": "job_delete",
        "extra": "kept",
    }
