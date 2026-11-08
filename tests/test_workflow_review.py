"""Review mode, a manual workflow start and the source hold over a mock transport (ADR 0099).

A write a review-mode gate applied reads ``under_review``; ``client.workflows.start`` sends a
start and reads its rows; ``QuarantineReason`` reads a quarantine row's workflow code; the
source-hold example prints ids and codes, never a payload value; and the held-writes example
says a reviewed write is under review.

Every fixture follows a schema of the backend's v0.136.0 contract (``docs/contract/openapi.json``),
named in the comment above it with the version that brought the field or route.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from masterly import Client, QuarantineReason, WorkflowStartItem, WorkflowStartResult, WriteResult
from masterly import __all__ as public_names

_ROOT = Path(__file__).resolve().parents[1]

# AuthorRecordResult — POST /v1/records, 201, v0.136.0 (workflow_instance_id since 0.135.0):
# a review-mode gate matched, the record applied and the instance reviews it.
_CREATED_UNDER_REVIEW = {
    "record_id": "rec_01JB3A",
    "model_name": "Supplier",
    "source_key": "S1",
    "job_id": "job_01JB3B",
    "workflow_instance_id": "wfi_01JB3C",
}
# CellWriteAccepted — POST /v1/golden/{global_id}/override, 202, v0.136.0 (workflow_instance_id
# since 0.135.0): the pin applied under review.
_PIN_UNDER_REVIEW = {"job_id": "job_01JB3D", "workflow_instance_id": "wfi_01JB3E"}
# BatchOverrideAccepted — POST /v1/golden/overrides, 202, v0.136.0 (workflow_instance_id since
# 0.135.0): one instance reviews the whole batch. `results` holds GoldenCellResult rows.
_BATCH_UNDER_REVIEW = {
    "total": 1,
    "committed": 1,
    "refused": 0,
    "results": [{"outcome": "committed", "global_id": "ent_01JB3F", "attribute": "iban"}],
    "job_ids": ["job_01JB3G"],
    "workflow_instance_id": "wfi_01JB3H",
}

# What read as applied on main 25723703d6 and still does, v0.136.0 schemas.
# AuthorRecordResult — 201, no gate: workflow_instance_id absent, and null.
_CREATED = {
    "record_id": "rec_01JB3J",
    "model_name": "Supplier",
    "source_key": "S2",
    "job_id": "job_01JB3K",
}
_CREATED_NULL = {**_CREATED, "workflow_instance_id": None}
# CellWriteAccepted — 202, no gate: the pin's recompute receipt.
_PIN = {"job_id": "job_01JB3L"}
# HeldChange — 202, v0.136.0: held, and a gate whose instance finished at once.
_HELD = {
    "status": "pending_approval",
    "pending_change_id": "pch_01JB3M",
    "workflow_instance_id": "wfi_01JB3N",
}
_HELD_APPLIED = {
    "status": "applied",
    "pending_change_id": "pch_01JB3P",
    "workflow_instance_id": "wfi_01JB3Q",
}

# PendingChangeView — GET /v1/pending-changes, v0.136.0: a reviewed change (status under_review
# since 0.135.0) beside a held one; every required field.
_REVIEWED = {
    "pending_change_id": "pch_01JB3R",
    "model_name": "Supplier",
    "operation": "update",
    "kind": "contribution",
    "status": "under_review",
    "attributes": ["iban"],
    "cells": [{"subject": "ent_01JB3F", "attribute": "iban"}],
    "based_on": {"iban": "7"},
    "maker": "usr_alice",
    "origin": "manual",
    "workflow_instance_id": "wfi_01JB3C",
    "created_at": "2026-11-06T08:00:00Z",
    "updated_at": "2026-11-06T08:00:00Z",
    "proposed": [{"attribute": "iban", "would_win": True}],
}
_HELD_VIEW = {**_REVIEWED, "pending_change_id": "pch_01JB3M", "status": "pending"}

# WorkflowStartResult of WorkflowStartItem rows — POST /v1/workflows/{key}:start, 200, v0.136.0
# (the route since 0.135.0): one started, one queued, one refused, in request order.
_STARTED = {
    "total": 3,
    "started": 1,
    "queued": 1,
    "refused": 1,
    "results": [
        {"record": "ent_01JB3S", "outcome": "started", "instance_id": "wfi_01JB3T"},
        {"record": "ent_01JB3U", "outcome": "queued", "position": 1},
        {
            "record": "ent_01JB3V",
            "outcome": "refused",
            "instance_id": "wfi_01JB3W",
            "code": "WORKFLOW_ALREADY_RUNNING",
            "reason": "This record is already running the workflow",
            "details": {"instance_id": "wfi_01JB3W"},
        },
    ],
}

# The IBAN standard's own example account, not a real one.
_IBAN = "GB82WEST12345698765432"
_REASON = f"The IBAN {_IBAN} waits for approval"


def _quarantined(quarantine_id: str, reason_code: str) -> dict[str, Any]:
    """QuarantineRecordView — GET /v1/sources/{source_id}/quarantine, v0.136.0 (the workflow
    codes documented since 0.136.0); every required field."""
    return {
        "quarantine_id": quarantine_id,
        "source_id": "src_01JB3X",
        "model_name": "Supplier",
        "ingest_job_id": "job_01JB3Y",
        "payload": {"supplier_no": "S1", "iban": _IBAN},
        "reason": _REASON,
        "reason_code": reason_code,
        "created_at": "2026-11-06T08:00:00Z",
        "status": "open",
    }


def _page(items: list[Any], next_cursor: str | None) -> dict[str, Any]:
    """Page_PendingChangeView_ / Page_QuarantineRecordView_, v0.136.0: items, next_cursor,
    total."""
    return {"items": items, "next_cursor": next_cursor, "total": None}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> Client:
    return Client(
        "https://masterly.test", "tok", "env_test", transport=httpx.MockTransport(handler)
    )


# --- WriteResult ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "status", "body", "instance"),
    [
        ("POST", "/v1/records", 201, _CREATED_UNDER_REVIEW, "wfi_01JB3C"),
        ("POST", "/v1/golden/ent_01JB3F/override", 202, _PIN_UNDER_REVIEW, "wfi_01JB3E"),
        ("POST", "/v1/golden/overrides", 202, _BATCH_UNDER_REVIEW, "wfi_01JB3H"),
    ],
)
def test_a_write_a_review_gate_applied_is_under_review(
    method: str, path: str, status: int, body: dict[str, Any], instance: str
) -> None:
    sent = _client(lambda _: httpx.Response(status, json=body)).request(method, path, json={})
    result = WriteResult.of(sent)
    assert result.status == "under_review" and result.under_review is True
    assert result.held is False
    assert result.pending_change_id is None and result.workflow_instance_id == instance
    assert result.body is sent


@pytest.mark.parametrize(
    ("body", "status", "change", "instance"),
    [
        (_CREATED, "applied", None, None),
        (_CREATED_NULL, "applied", None, None),
        (_PIN, "applied", None, None),
        (None, "applied", None, None),
        ([], "applied", None, None),
        (_HELD, "pending_approval", "pch_01JB3M", "wfi_01JB3N"),
        (_HELD_APPLIED, "applied", "pch_01JB3P", "wfi_01JB3Q"),
    ],
)
def test_what_read_as_applied_or_held_before_still_does(
    body: Any, status: str, change: str | None, instance: str | None
) -> None:
    result = WriteResult.of(body)
    assert (result.status, result.pending_change_id, result.workflow_instance_id) == (
        status,
        change,
        instance,
    )
    assert result.under_review is False


# --- client.workflows.start -------------------------------------------------------------


def _starting(seen: list[httpx.Request]) -> Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_STARTED)

    return _client(handler)


def test_start_posts_the_records_and_the_note() -> None:
    seen: list[httpx.Request] = []
    records = ["ent_01JB3S", "ent_01JB3U", "ent_01JB3V"]
    _starting(seen).workflows.start("request-a-review", records=records, note="Quarterly check")
    assert [(r.method, r.url.raw_path) for r in seen] == [
        ("POST", b"/v1/workflows/request-a-review:start")
    ]
    assert json.loads(seen[0].content) == {"records": records, "note": "Quarterly check"}


def test_start_quotes_the_key_and_leaves_an_absent_note_out() -> None:
    seen: list[httpx.Request] = []
    _starting(seen).workflows.start("bank change/eu", records=("ent_01JB3S",))
    assert seen[0].url.raw_path == b"/v1/workflows/bank%20change%2Feu:start"
    assert json.loads(seen[0].content) == {"records": ["ent_01JB3S"]}


def test_start_refuses_one_string_for_its_records() -> None:
    with pytest.raises(TypeError):
        _starting([]).workflows.start("request-a-review", records="ent_01JB3S")


def test_start_reads_the_counts_and_rows_in_request_order() -> None:
    result = _starting([]).workflows.start("request-a-review", records=["ent_01JB3S"])
    assert isinstance(result, WorkflowStartResult)
    assert (result.total, result.started, result.queued, result.refused) == (3, 1, 1, 1)
    assert result.results == (
        WorkflowStartItem("ent_01JB3S", "started", "wfi_01JB3T", None, None, None, None),
        WorkflowStartItem("ent_01JB3U", "queued", None, 1, None, None, None),
        WorkflowStartItem(
            "ent_01JB3V",
            "refused",
            "wfi_01JB3W",
            None,
            "WORKFLOW_ALREADY_RUNNING",
            "This record is already running the workflow",
            {"instance_id": "wfi_01JB3W"},
        ),
    )
    assert result.body == _STARTED
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.results[0].outcome = "refused"  # type: ignore[misc]


def test_a_service_account_cannot_start_a_workflow() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not be sent: {request.url}")

    client = Client.for_service_account(
        "https://masterly.test", "m2m:dev:svc", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PermissionError, match="needs a session token"):
        client.workflows.start("request-a-review", records=["ent_01JB3S"], note="n")


# --- pending changes under review -------------------------------------------------------


def _listing(seen: list[httpx.Request]) -> Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_page([_REVIEWED, _HELD_VIEW], None))

    return _client(handler)


def test_pending_changes_lists_reviews_beside_held_changes() -> None:
    seen: list[httpx.Request] = []
    assert _listing(seen).pending_changes.list() == [_REVIEWED, _HELD_VIEW]
    assert dict(seen[0].url.params) == {"limit": "200"}


@pytest.mark.parametrize("status", ["under_review", "confirmed", "rejected"])
def test_pending_changes_sends_a_review_status_alone(status: str) -> None:
    seen: list[httpx.Request] = []
    _listing(seen).pending_changes.list(status=status)
    assert dict(seen[0].url.params) == {"status": status, "limit": "200"}


# --- QuarantineReason -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "key", "outcome"),
    [
        ("workflow:supplier-bank-change", "supplier-bank-change", "held"),
        ("workflow-rejected:supplier-bank-change", "supplier-bank-change", "rejected"),
        ("workflow:ns:with-colon", "ns:with-colon", "held"),
        ("dq-gate:iban-format", None, None),
        ("translation-miss:country:countries", None, None),
        ("workflow:", None, None),
        ("workflow", None, None),
        ("unknown", None, None),
        (None, None, None),
    ],
)
def test_a_quarantine_reason_code_reads_typed(
    code: str | None, key: str | None, outcome: str | None
) -> None:
    reason = QuarantineReason.of(code)
    assert (reason.workflow_key, reason.outcome) == (key, outcome)
    assert reason.workflow_related is (outcome is not None)
    assert reason.held is (outcome == "held") and reason.rejected is (outcome == "rejected")


def test_the_quarantine_yields_every_row_and_each_reads_its_reason() -> None:
    rows = [
        _quarantined("qr_01JB41", "workflow:supplier-bank-change"),
        _quarantined("qr_01JB42", "workflow-rejected:supplier-bank-change"),
        _quarantined("qr_01JB43", "dq-gate:iban-format"),
        _quarantined("qr_01JB44", "translation-miss:country:countries"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor"):
            return httpx.Response(200, json=_page(rows[2:], None))
        return httpx.Response(200, json=_page(rows[:2], "p2"))

    yielded = list(_client(handler).sources.quarantine("src_01JB3X"))
    assert yielded == rows
    reasons = [QuarantineReason.of(row) for row in yielded]
    assert [(r.workflow_key, r.held, r.rejected) for r in reasons] == [
        ("supplier-bank-change", True, False),
        ("supplier-bank-change", False, True),
        (None, False, False),
        (None, False, False),
    ]


def test_the_new_names_are_public() -> None:
    assert {"QuarantineReason", "WorkflowStartItem", "WorkflowStartResult"} <= set(public_names)


# --- the source-hold example ------------------------------------------------------------


def _example() -> Any:
    path = _ROOT / "examples" / "source_hold.py"
    spec = importlib.util.spec_from_file_location("source_hold", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["source_hold"] = module
    spec.loader.exec_module(module)
    return module


def _erp(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """A Source behind a source gate: the delivery is accepted, and the quarantine holds one
    row waiting for supplier-bank-change and one it already rejected."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/ingest":
            # JobAccepted — POST /v1/ingest, 202, v0.136.0.
            return httpx.Response(202, json={"job_id": "job_01JB45"})
        code = request.url.params["reason_code"]
        held = "qr_01JB46" if code.startswith("workflow:") else "qr_01JB47"
        return httpx.Response(200, json=_page([_quarantined(held, code)], None))

    return handler


def test_the_source_hold_example_reports_ids_never_a_payload_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    module = _example()
    real = module.Client
    transport = httpx.MockTransport(_erp(seen))
    monkeypatch.setattr(
        module,
        "Client",
        types.SimpleNamespace(
            for_service_account=lambda *a: real.for_service_account(*a, transport=transport)
        ),
    )
    argv = ["--base-url", "https://masterly.test", "--token", "m2m:dev:svc"]
    argv += ["--source", "src_01JB3X", "--supplier-no", "S1", "--iban", _IBAN]
    assert module.main(argv) == 0

    assert json.loads(seen[0].content)["records"] == [{"supplier_no": "S1", "iban": _IBAN}]
    assert [r.url.params.get("reason_code") for r in seen[1:]] == [
        "workflow:supplier-bank-change",
        "workflow-rejected:supplier-bank-change",
    ]
    assert all(r.url.params.get("status") == "open" for r in seen[1:])
    printed = capsys.readouterr()
    assert printed.out.splitlines() == [
        "delivered 1 record(s): job job_01JB45",
        "waiting for approval (supplier-bank-change): qr_01JB46",
        "previously rejected (supplier-bank-change): qr_01JB47",
    ]
    for value in (_IBAN, "GB82", _REASON, "S1"):
        assert value not in printed.out and value not in printed.err
    assert all(_IBAN not in str(r.url) for r in seen)


# --- the held-writes example ------------------------------------------------------------


def test_the_held_writes_example_says_a_reviewed_write_is_under_review(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(201, json=_CREATED_UNDER_REVIEW)

    path = _ROOT / "examples" / "held_writes.py"
    spec = importlib.util.spec_from_file_location("held_writes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["held_writes"] = module
    spec.loader.exec_module(module)

    assert module.author(_client(handler), "Supplier", {"supplier_no": "S1"}) == "under_review"
    assert seen == ["POST /v1/records"]
    assert capsys.readouterr().out == "applied, under review by instance wfi_01JB3C\n"
