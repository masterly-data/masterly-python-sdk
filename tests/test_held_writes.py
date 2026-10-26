"""Held writes over a mock transport: WriteResult tells an applied write from one a change gate
holds, a 409 CHANGE_PENDING raises ChangePending, and pending changes and workflows read back.

Every fixture follows a schema of the backend's v0.134.0 contract (``docs/contract/openapi.json``),
named in the comment above it: field names, required fields and enum values as there.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import masterly
from masterly import ApiError, ChangePending, Client, WriteResult

_ROOT = Path(__file__).resolve().parents[1]

# AuthorRecordResult — POST /v1/records, 201: the write applied, no gate involved.
_CREATED = {
    "record_id": "rec_01JA2B",
    "model_name": "Supplier",
    "source_key": "S1",
    "job_id": "job_01JA2C",
}
# JobAccepted — POST /v1/golden/{global_id}/override, 202: the pin's recompute receipt.
_PIN_ACCEPTED = {"job_id": "job_01JA2D"}
# HeldChange — 202, status pending_approval: staged, nothing applied.
_HELD = {
    "status": "pending_approval",
    "pending_change_id": "pch_01JA2E",
    "workflow_instance_id": "wfi_01JA2F",
}
# HeldChange — 202, status applied: the gate's instance finished at once, the change applied.
_HELD_APPLIED = {
    "status": "applied",
    "pending_change_id": "pch_01JA2G",
    "workflow_instance_id": "wfi_01JA2H",
}
# The 409 CHANGE_PENDING under ConflictEnvelope ({"error": {...}}): the contract documents
# details.pending_change_id, never the change's values. The rest of `details` is what v0.134.0's
# `change_pending()` (modules/workflows/pending.py) sends beside it, not a ConflictDetails.
_CHANGE_PENDING = {
    "error": {
        "code": "CHANGE_PENDING",
        "message": "A change to ent_01JA2K/iban is already waiting for approval as pending "
        "change 'pch_01JA2E' (workflow instance 'wfi_01JA2F')",
        "details": {
            "pending_change_id": "pch_01JA2E",
            "workflow_instance_id": "wfi_01JA2F",
            "cells": [{"subject": "ent_01JA2K", "attribute": "iban"}],
            "request_id": "req_01JA2L",
        },
    }
}
# ConflictEnvelope, code VERSION_CONFLICT — 409, ConflictDetails.
_VERSION_CONFLICT = {
    "error": {
        "code": "VERSION_CONFLICT",
        "message": "The record changed since you read it",
        "details": {
            "object_type": "record",
            "object_id": "rec_01JA2B",
            "base_version": "3",
            "current_version": "4",
            "changed_by": "usr_bob",
            "changed_at": "2026-10-19T08:00:00Z",
            "changed_fields": ["iban"],
            "changed_fields_complete": True,
            "undisclosed_changes": 0,
            "request_id": "req_01JA2M",
        },
    }
}
# PendingChangeView — GET /v1/pending-changes/{pending_change_id}; every required field.
_PENDING_CHANGE = {
    "pending_change_id": "pch_01JA2E",
    "model_name": "Supplier",
    "operation": "update",
    "kind": "contribution",
    "status": "pending",
    "attributes": ["iban"],
    "cells": [{"subject": "ent_01JA2K", "attribute": "iban"}],
    "based_on": {"iban": "7"},
    "maker": "usr_alice",
    "origin": "manual",
    "workflow_instance_id": "wfi_01JA2F",
    "created_at": "2026-10-19T08:00:00Z",
    "updated_at": "2026-10-19T08:00:00Z",
    "proposed": [{"attribute": "iban", "would_win": True, "proposed_value": "SE45..."}],
    "global_id": "ent_01JA2K",
    "key_subject": None,
    "outcome_note": None,
    "applied_at": None,
}
# WorkflowView — GET /v1/workflows/{key}; every required field, and `builtin`.
_WORKFLOW = {
    "key": "iban-approval",
    "name": "IBAN approval",
    "description": None,
    "definition": {
        "trigger": {"on_change": {"model": "Supplier", "operations": ["update"]}},
        "steps": [{"id": "steward", "kind": "approval", "approvers": {"role": "steward"}}],
    },
    "enabled": True,
    "builtin": False,
    "revision": 2,
    "created_by": "usr_owner",
    "created_at": "2026-10-18T08:00:00Z",
    "updated_at": "2026-10-19T08:00:00Z",
}


def _page(items: list[Any], next_cursor: str | None) -> dict[str, Any]:
    """Page_PendingChangeView_ / Page_WorkflowView_: items, next_cursor, total."""
    return {"items": items, "next_cursor": next_cursor, "total": None}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> Client:
    return Client(
        "https://masterly.test", "tok", "env_test", transport=httpx.MockTransport(handler)
    )


def _answering(status: int, body: Any) -> Client:
    return _client(lambda _: httpx.Response(status, json=body))


def _refused(body: dict[str, Any]) -> ApiError:
    client = _answering(409, body)
    with pytest.raises(ApiError) as caught:
        client.request("POST", "/v1/records", json={"model_name": "Supplier", "values": {}})
    return caught.value


# --- WriteResult ------------------------------------------------------------------------


def test_a_created_record_answers_as_before_and_is_not_held() -> None:
    body = _answering(201, _CREATED).request("POST", "/v1/records", json={})
    assert body == _CREATED  # client.request still returns the decoded body
    result = WriteResult.of(body)
    assert result.held is False and result.status == "applied"
    assert result.pending_change_id is None and result.workflow_instance_id is None
    assert result.body is body


def test_a_pin_recompute_receipt_answers_202_and_is_not_held() -> None:
    body = _answering(202, _PIN_ACCEPTED).request("POST", "/v1/golden/ent_01JA2K/override")
    assert body == _PIN_ACCEPTED
    result = WriteResult.of(body)
    assert result.held is False
    assert result.pending_change_id is None and result.workflow_instance_id is None


def test_a_held_write_names_its_pending_change_and_instance() -> None:
    body = _answering(202, _HELD).request("POST", "/v1/records", json={})
    result = WriteResult.of(body)
    assert result.held is True and result.status == "pending_approval"
    assert result.pending_change_id == "pch_01JA2E"
    assert result.workflow_instance_id == "wfi_01JA2F"
    assert result.body == _HELD


def test_a_gate_that_finished_at_once_is_applied_and_keeps_its_ids() -> None:
    result = WriteResult.of(_answering(202, _HELD_APPLIED).request("POST", "/v1/records"))
    assert result.held is False and result.status == "applied"
    assert result.pending_change_id == "pch_01JA2G"
    assert result.workflow_instance_id == "wfi_01JA2H"


def test_an_empty_answer_is_not_held() -> None:
    body = _client(lambda _: httpx.Response(204)).request("DELETE", "/v1/golden/x/override/iban")
    assert body is None
    for empty in (None, body, {}, [], "text"):
        result = WriteResult.of(empty)
        assert result.held is False and result.pending_change_id is None


def test_a_curation_staged_op_is_not_read_as_a_gate_hold() -> None:
    assert WriteResult.of({"status": "pending", "op_id": "op_1"}).held is False


# --- ChangePending ----------------------------------------------------------------------


def test_change_pending_is_an_api_error_naming_the_waiting_change() -> None:
    error = _refused(_CHANGE_PENDING)
    assert isinstance(error, ChangePending) and isinstance(error, masterly.ApiError)
    assert error.status_code == 409 and error.code == "CHANGE_PENDING"
    assert error.pending_change_id == _CHANGE_PENDING["error"]["details"]["pending_change_id"]
    assert error.workflow_instance_id == "wfi_01JA2F"
    assert error.conflict is None


def test_a_version_conflict_is_not_change_pending() -> None:
    error = _refused(_VERSION_CONFLICT)
    assert type(error) is ApiError
    assert error.conflict is not None and error.conflict.changed_fields == ("iban",)


def test_change_pending_on_another_status_stays_a_plain_api_error() -> None:
    client = _answering(400, _CHANGE_PENDING)
    with pytest.raises(ApiError) as caught:
        client.request("POST", "/v1/records")
    assert type(caught.value) is ApiError


# --- pending changes and workflows ------------------------------------------------------


def _paged(path: str, item: dict[str, Any], seen: list[httpx.Request]) -> Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == path:
            cursor = request.url.params.get("cursor")
            return httpx.Response(200, json=_page([item], None if cursor else "p2"))
        if request.url.path.startswith(path + "/"):
            return httpx.Response(200, json=item)
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "no"}})

    return _client(handler)


@pytest.mark.parametrize(
    ("filters", "sent"),
    [
        ({"model": "Customer"}, {"model": "Customer"}),
        ({"global_id": "ent_1"}, {"global_id": "ent_1"}),
        ({"status": "pending", "maker": "usr_alice"}, {"status": "pending", "maker": "usr_alice"}),
    ],
)
def test_pending_changes_list_sends_its_filters_and_follows_the_cursor(
    filters: dict[str, str], sent: dict[str, str]
) -> None:
    seen: list[httpx.Request] = []
    items = _paged("/v1/pending-changes", _PENDING_CHANGE, seen).pending_changes.list(**filters)
    assert items == [_PENDING_CHANGE, _PENDING_CHANGE]
    assert [r.method for r in seen] == ["GET", "GET"]
    assert [dict(r.url.params) for r in seen] == [
        {**sent, "limit": "200"},
        {**sent, "limit": "200", "cursor": "p2"},
    ]


def test_pending_change_get_reads_one() -> None:
    seen: list[httpx.Request] = []
    change = _paged("/v1/pending-changes", _PENDING_CHANGE, seen).pending_changes.get("pch_01JA2E")
    assert change == _PENDING_CHANGE
    assert seen[0].url.path == "/v1/pending-changes/pch_01JA2E"


def test_workflows_list_follows_its_cursor() -> None:
    seen: list[httpx.Request] = []
    assert _paged("/v1/workflows", _WORKFLOW, seen).workflows.list() == [_WORKFLOW, _WORKFLOW]
    assert [dict(r.url.params) for r in seen] == [
        {"limit": "200"},
        {"limit": "200", "cursor": "p2"},
    ]


def test_workflows_list_sends_its_model_filter() -> None:
    seen: list[httpx.Request] = []
    _paged("/v1/workflows", _WORKFLOW, seen).workflows.list(model="Supplier")
    assert dict(seen[0].url.params) == {"model": "Supplier", "limit": "200"}


def test_workflow_get_reads_one_by_key() -> None:
    seen: list[httpx.Request] = []
    assert _paged("/v1/workflows", _WORKFLOW, seen).workflows.get("iban-approval") == _WORKFLOW
    assert seen[0].method == "GET" and seen[0].url.path == "/v1/workflows/iban-approval"


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.pending_changes.list(model="Customer"),
        lambda c: c.pending_changes.get("pch_01JA2E"),
        lambda c: c.workflows.list(),
        lambda c: c.workflows.get("iban-approval"),
    ],
)
def test_a_service_account_cannot_read_pending_changes_or_workflows(
    call: Callable[[Client], Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not be sent: {request.url}")

    client = Client.for_service_account(
        "https://masterly.test", "m2m:dev:svc", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PermissionError, match="needs a session token") as caught:
        call(client)
    assert "connect with a session token" in str(caught.value)


def test_the_held_write_names_are_public() -> None:
    assert {"WriteResult", "ChangePending"} <= set(masterly.__all__)


# --- the README and the example ---------------------------------------------------------


def _gated(seen: list[str], write: tuple[int, dict[str, Any]]) -> Client:
    """An install with a gate on Supplier: the write answers as given, the reads as recorded."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/records":
            return httpx.Response(write[0], json=write[1])
        if request.url.path == "/v1/workflows":
            return httpx.Response(200, json=_page([_WORKFLOW], None))
        return httpx.Response(200, json=_PENDING_CHANGE)

    return _client(handler)


def _readme_example() -> str:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    found = re.search(
        r"<!-- example:held-write -->\s*```python\n(.*?)```\s*<!-- /example -->", readme, re.S
    )
    assert found, "README.md has no held-write example"
    return found.group(1)


@pytest.mark.parametrize(
    ("write", "reads", "printed"),
    [
        ((202, _HELD), ["GET /v1/pending-changes/pch_01JA2E"], "held as pch_01JA2E"),
        ((201, _CREATED), [], ""),
        ((409, _CHANGE_PENDING), [], "already waiting for approval: pch_01JA2E"),
    ],
)
def test_the_readme_held_write_example_runs(
    write: tuple[int, dict[str, Any]],
    reads: list[str],
    printed: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []
    exec(_readme_example(), {"client": _gated(seen, write)})  # noqa: S102 — our own README
    assert seen == ["POST /v1/records", *reads]
    assert printed in capsys.readouterr().out


def _example() -> Any:
    path = _ROOT / "examples" / "held_writes.py"
    spec = importlib.util.spec_from_file_location("held_writes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["held_writes"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("write", "outcome", "reads"),
    [
        ((202, _HELD), "held", ["GET /v1/pending-changes/pch_01JA2E", "GET /v1/workflows"]),
        ((202, _HELD_APPLIED), "applied", []),
        ((201, _CREATED), "applied", []),
        ((409, _CHANGE_PENDING), "waiting", []),
    ],
)
def test_the_example_script_tells_the_answers_apart(
    write: tuple[int, dict[str, Any]], outcome: str, reads: list[str]
) -> None:
    seen: list[str] = []
    client = _gated(seen, write)
    assert _example().author(client, "Supplier", {"supplier_no": "S1"}) == outcome
    assert seen == ["POST /v1/records", *reads]


def test_the_example_script_sends_the_record_it_was_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(201, json=_CREATED)

    module = _example()
    real = module.Client
    monkeypatch.setattr(
        module, "Client", lambda *a: real(*a, transport=httpx.MockTransport(handler))
    )
    argv = ["--base-url", "https://masterly.test", "--token", "tok", "--environment", "env_test"]
    argv += ["--model", "Supplier", "--values", '{"supplier_no": "S1"}']
    assert module.main(argv) == 0
    assert sent == [{"model_name": "Supplier", "values": {"supplier_no": "S1"}}]
    assert capsys.readouterr().out.strip() == "applied"
