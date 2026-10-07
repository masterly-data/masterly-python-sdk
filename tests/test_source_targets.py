"""A Source that feeds several models, over a mock transport that answers in the shapes the
`/v1` contract records for source targets (ADR 0093 §2, `docs/contract/openapi.json` of
masterly-application-backend at 6134298): `targets[]` on the Source, one target under
`/v1/sources/{id}/targets/{model}`, `model_name` on the push, the load, the upload and the
quarantine, and the refusals each meets.

The contract is replayed here rather than reached: each handler answers what the recorded
operation answers, and the assertions are on what the client sent — the path, the body and
the query — which is the half of the contract the client owns.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from masterly import ApiError, Client, SourceTarget


def _client(handler: Any) -> Client:
    return Client(
        "https://masterly.test",
        token="tok",
        environment="env_prod_eu",
        transport=httpx.MockTransport(handler),
    )


def _target(model: str, key: str, version: int = 1, **mapping: Any) -> dict[str, Any]:
    """A `SourceTargetView` as the contract records it."""
    return {
        "model_name": model,
        "mapping": {"field_map": {}, "source_key": [key], **mapping},
        "connector": None,
        "pull_state": None,
        "profile": None,
        "drift": None,
        "created_at": "2026-10-07T09:00:00Z",
        "updated_at": "2026-10-07T09:00:00Z",
        "version": str(version),
    }


def _source(targets: list[dict[str, Any]], version: int = 1) -> dict[str, Any]:
    """A `SourceView`: `targets[]`, with the deprecated top-level fields describing the first."""
    first = targets[0]
    return {
        "source_id": "src_1",
        "name": "crm",
        "display_name": "Salesforce CRM",
        "system_type": "rest",
        "mode": "push",
        "status": "active",
        "built_in": False,
        "targets": targets,
        "target_model": first["model_name"],
        "mapping": first["mapping"],
        "created_at": "2026-10-07T09:00:00Z",
        "updated_at": "2026-10-07T09:00:00Z",
        "version": str(version),
    }


def _quarantine_row(model: str, qid: str) -> dict[str, Any]:
    """A `QuarantineRecordView`: every row names the target it was held for."""
    return {
        "quarantine_id": qid,
        "source_id": "src_1",
        "model_name": model,
        "ingest_job_id": f"job_{model.lower()}",
        "payload": {"id": "x"},
        "reason": "'id' is required",
        "reason_code": "missing-source-key",
        "status": "open",
        "created_at": "2026-10-07T09:00:01Z",
    }


# --- acceptance criterion 1: two targets, one batch each, quarantine per target ----------


def test_a_two_target_source_takes_one_batch_per_target_and_quarantines_per_target() -> None:
    """Create a Source feeding Customer and Address, push one batch into each, and read the
    quarantine of each — the recorded contract end to end."""
    seen: list[httpx.Request] = []
    customer = _target("Customer", "customer_number")
    address = _target("Address", "address_id", field_map={"ADDR_ID": "address_id"})

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/sources":
            return httpx.Response(201, json=_source([customer, address]))
        if request.url.path == "/v1/ingest":
            body = json.loads(request.content)
            return httpx.Response(202, json={"job_id": f"job_{body['model_name'].lower()}"})
        if request.url.path == "/v1/sources/src_1/quarantine":
            model = request.url.params["model_name"]
            return httpx.Response(
                200, json={"items": [_quarantine_row(model, f"q_{model.lower()}")]}
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    client = _client(handler)

    source = client.sources.create(
        "crm",
        display_name="Salesforce CRM",
        targets=[
            SourceTarget("Customer", source_key="customer_number"),
            SourceTarget("Address", source_key="address_id", field_map={"ADDR_ID": "address_id"}),
        ],
    )
    assert [t["model_name"] for t in source["targets"]] == ["Customer", "Address"]

    customers = client.sources.ingest(
        source["source_id"], [{"customer_number": "C-1", "name": "Acme"}], model="Customer"
    )
    addresses = client.sources.ingest(
        source["source_id"], [{"ADDR_ID": "A-1", "customer_number": "C-1"}], model="Address"
    )
    assert customers.job_ids == ("job_customer",)
    assert addresses.job_ids == ("job_address",)

    held_customers = list(client.sources.quarantine("src_1", model="Customer"))
    held_addresses = list(client.sources.quarantine("src_1", model="Address"))
    assert [row["model_name"] for row in held_customers] == ["Customer"]
    assert [row["model_name"] for row in held_addresses] == ["Address"]

    # What went over the wire, in order: the registration, a batch per target, a read per target.
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/v1/sources"),
        ("POST", "/v1/ingest"),
        ("POST", "/v1/ingest"),
        ("GET", "/v1/sources/src_1/quarantine"),
        ("GET", "/v1/sources/src_1/quarantine"),
    ]
    registration = json.loads(seen[0].content)
    assert registration["targets"] == [
        {"model_name": "Customer", "mapping": {"field_map": {}, "source_key": ["customer_number"]}},
        {
            "model_name": "Address",
            "mapping": {"field_map": {"ADDR_ID": "address_id"}, "source_key": ["address_id"]},
        },
    ]
    assert "target_model" not in registration and "mapping" not in registration, (
        "the several-target form sends targets and not the deprecated pair"
    )
    assert [json.loads(r.content)["model_name"] for r in seen[1:3]] == ["Customer", "Address"]
    assert [r.url.params["model_name"] for r in seen[3:]] == ["Customer", "Address"]


# --- acceptance criterion 2: a call without a model is the call it always was ------------


def test_an_ingest_without_a_model_sends_the_envelope_it_always_sent() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"job_id": "j"})

    _client(handler).sources.ingest("src_1", [{"id": "1"}])
    assert bodies == [{"source_id": "src_1", "records": [{"id": "1"}]}]


def test_a_one_target_create_still_sends_target_model_and_mapping_and_no_targets() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=_source([_target("Customer", "customer_number")]))

    _client(handler).sources.create(
        "crm", target_model="Customer", source_key="customer_number", field_map={"NAME1": "name"}
    )
    body = json.loads(seen[0].content)
    assert body["target_model"] == "Customer"
    assert body["mapping"] == {"field_map": {"NAME1": "name"}, "source_key": ["customer_number"]}
    assert "targets" not in body


def test_a_source_create_names_what_it_feeds_one_way() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("the request must not be sent")

    client = _client(handler)
    with pytest.raises(ValueError, match="not both"):
        client.sources.create(
            "crm",
            target_model="Customer",
            source_key="customer_number",
            targets=[SourceTarget("Address", source_key="address_id")],
        )
    with pytest.raises(ValueError, match="targets="):
        client.sources.create("crm")
    with pytest.raises(ValueError, match="at least one model"):
        client.sources.create("crm", targets=[])
    with pytest.raises(ValueError, match="source_key"):
        client.sources.create("crm", targets=[SourceTarget("Customer", source_key=[])])


def test_a_stats_read_without_a_model_sends_no_query_and_carries_every_target() -> None:
    seen: list[httpx.Request] = []
    stats = {
        "source_id": "src_1",
        "model_name": None,
        "records": 12,
        "quarantined": 1,
        "targets": [
            {"model_name": "Customer", "records": 10, "quarantined": 0},
            {"model_name": "Address", "records": 2, "quarantined": 1},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=stats)

    client = _client(handler)
    whole = client.sources.stats("src_1")
    assert whole["targets"][1]["quarantined"] == 1
    assert "model_name" not in seen[0].url.params

    client.sources.stats("src_1", model="Address")
    assert seen[1].url.params["model_name"] == "Address"


# --- the target the push names, and the refusals ------------------------------------------


def test_an_unnamed_batch_on_a_two_target_source_is_refused_naming_the_targets() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "SOURCE_TARGET_REQUIRED",
                    "message": "The source feeds several models; name the one this batch is for",
                    "details": {"source_id": "src_1", "targets": ["Customer", "Address"]},
                }
            },
        )

    with pytest.raises(ApiError) as refused:
        _client(handler).sources.ingest("src_1", [{"customer_number": "C-1"}])

    assert refused.value.code == "SOURCE_TARGET_REQUIRED"
    assert refused.value.details["targets"] == ["Customer", "Address"]
    assert len(sent) == 1, "a refusal is an answer, never retried under the key"


def test_a_model_the_source_does_not_feed_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "SOURCE_TARGET_NOT_FOUND",
                    "message": "The source does not feed 'Supplier'",
                    "details": {"source_id": "src_1", "model_name": "Supplier"},
                }
            },
        )

    with pytest.raises(ApiError, match="SOURCE_TARGET_NOT_FOUND"):
        _client(handler).sources.ingest("src_1", [{"id": "1"}], model="Supplier")


def test_a_full_load_names_its_target_once_on_the_load_and_not_on_its_batches() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/sources/src_1/loads":
            return httpx.Response(
                201, json={"load_id": "load_1", "model_name": "Address", "status": "open"}
            )
        if request.url.path == "/v1/ingest":
            return httpx.Response(202, json={"job_id": "j"})
        if request.url.path == "/v1/sources/src_1/loads/load_1:complete":
            return httpx.Response(202, json={"job_id": "job_reconcile"})
        return httpx.Response(
            200,
            json={
                "load_id": "load_1",
                "source_id": "src_1",
                "model_name": "Address",
                "status": "completing",
                "batches": 1,
                "records": 2,
                "job_id": "job_reconcile",
            },
        )

    with _client(handler).sources.full_load("src_1", model="Address") as load:
        load.send([{"address_id": "A-1"}, {"address_id": "A-2"}])

    assert seen[0].url.path == "/v1/sources/src_1/loads"
    assert seen[0].url.params["model_name"] == "Address"
    batch = json.loads(seen[1].content)
    assert batch == {
        "source_id": "src_1",
        "records": [{"address_id": "A-1"}, {"address_id": "A-2"}],
        "mode": "full",
        "load_id": "load_1",
    }
    assert load.report.load["model_name"] == "Address"


def test_a_full_load_without_a_model_opens_as_it_always_did() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/sources/src_1/loads":
            return httpx.Response(201, json={"load_id": "load_1", "status": "open"})
        if request.url.path == "/v1/ingest":
            return httpx.Response(202, json={"job_id": "j"})
        if request.url.path.endswith(":complete"):
            return httpx.Response(202, json={"job_id": "job_r"})
        return httpx.Response(200, json={"load_id": "load_1", "status": "completing"})

    with _client(handler).sources.full_load("src_1") as load:
        load.send([{"id": "1"}])

    assert "model_name" not in seen[0].url.params
    assert not seen[0].content
    assert "idempotency-key" in seen[0].headers


def test_a_csv_upload_is_a_keyed_write_naming_its_target_in_the_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            202, json={"job_id": "job_csv", "records": 2, "ignored_columns": ["notes"]}
        )

    receipt = _client(handler).sources.upload_csv(
        "src_1",
        "customer_number;name;notes\nC-1;Acme;x\nC-2;Globex;y\n",
        model="Customer",
        delimiter=";",
        filename="customers.csv",
    )

    assert receipt["job_id"] == "job_csv" and receipt["ignored_columns"] == ["notes"]
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", "/v1/sources/src_1/upload")
    assert parse_qs(request.url.query.decode()) == {
        "model_name": ["Customer"],
        "delimiter": [";"],
        "filename": ["customers.csv"],
    }
    assert request.headers["content-type"].startswith("text/csv")
    assert request.content.decode() == "customer_number;name;notes\nC-1;Acme;x\nC-2;Globex;y\n"
    assert "idempotency-key" in request.headers
    assert "x-masterly-environment" in request.headers


def test_a_csv_upload_without_a_model_sends_no_query_at_all() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"job_id": "job_csv", "records": 1})

    _client(handler).sources.upload_csv("src_1", b"id,name\n1,Acme\n")
    assert not seen[0].url.query
    assert seen[0].content == b"id,name\n1,Acme\n"


# --- client.sources.targets ----------------------------------------------------------------


def test_targets_are_listed_off_the_source_in_creation_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("GET", "/v1/sources/src_1")
        return httpx.Response(
            200,
            json=_source(
                [_target("Customer", "customer_number"), _target("Address", "address_id")]
            ),
        )

    targets = _client(handler).sources.targets.list("src_1")
    assert [t["model_name"] for t in targets] == ["Customer", "Address"]


def test_a_target_is_added_with_its_own_mapping_and_the_answer_is_the_target() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json=_target(
                "Address",
                "address_id",
                field_map={"ADDR_ID": "address_id"},
                translations={"country": {"list": "countries", "scheme": "iso"}},
            ),
        )

    added = _client(handler).sources.targets.add(
        "src_1",
        "Address",
        source_key="address_id",
        field_map={"ADDR_ID": "address_id"},
        translations={"country": {"list": "countries", "scheme": "iso"}},
    )

    assert [(r.method, r.url.path) for r in seen] == [("POST", "/v1/sources/src_1/targets")]
    assert json.loads(seen[0].content) == {
        "model_name": "Address",
        "mapping": {
            "field_map": {"ADDR_ID": "address_id"},
            "source_key": ["address_id"],
            "translations": {"country": {"list": "countries", "scheme": "iso"}},
        },
    }
    assert "idempotency-key" not in seen[0].headers, "a duplicate is refused, never replayed"
    assert added["model_name"] == "Address" and added["version"] == "1"


def test_a_target_is_read_by_its_model_name_encoded_as_one_segment() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_target("Ship-to address", "address_id", version=3))

    target = _client(handler).sources.targets.get("src_1", "Ship-to address")

    assert seen[0].url.raw_path == b"/v1/sources/src_1/targets/Ship-to%20address"
    assert target["version"] == "3"


def test_a_target_mapping_is_replaced_under_the_targets_own_revision() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json=_target("Address", "address_id", version=4, field_map={"STREET": "street"})
        )

    client = _client(handler)
    mapping = {"field_map": {"STREET": "street"}, "source_key": ["address_id"]}
    updated = client.sources.targets.update("src_1", "Address", mapping=mapping, if_match=3)

    assert [(r.method, r.url.path) for r in seen] == [("PUT", "/v1/sources/src_1/targets/Address")]
    assert seen[0].headers["if-match"] == '"3"'
    assert json.loads(seen[0].content) == {"mapping": mapping}
    assert updated["version"] == "4"


def test_a_re_key_of_a_target_holding_records_is_refused_and_is_not_a_merge_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "SOURCE_HAS_RECORDS",
                    "message": "The target holds records keyed on the current source key",
                    "details": {"records": 1200, "change": "source_key"},
                }
            },
        )

    with pytest.raises(ApiError) as refused:
        _client(handler).sources.targets.update(
            "src_1", "Address", mapping={"field_map": {}, "source_key": ["other"]}, if_match=3
        )

    assert refused.value.code == "SOURCE_HAS_RECORDS"
    assert refused.value.details["records"] == 1200
    assert refused.value.conflict is None


def test_an_empty_target_is_removed_under_its_revision_and_a_full_one_is_refused() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/Address"):
            return httpx.Response(204)
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "SOURCE_TARGET_HAS_RECORDS",
                    "message": "The target holds records; removing it is a delete",
                    "details": {"model_name": "Customer", "records": 10, "tombstoned": 1},
                }
            },
        )

    client = _client(handler)
    assert client.sources.targets.remove("src_1", "Address", if_match=4) is None
    assert (seen[0].method, seen[0].url.path) == ("DELETE", "/v1/sources/src_1/targets/Address")
    assert seen[0].headers["if-match"] == '"4"'

    with pytest.raises(ApiError, match="SOURCE_TARGET_HAS_RECORDS") as refused:
        client.sources.targets.remove("src_1", "Customer", if_match=2)
    assert refused.value.details == {"model_name": "Customer", "records": 10, "tombstoned": 1}


def test_a_target_is_addressed_through_the_sources_name_on_a_session_connection() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/sources":
            return httpx.Response(
                200,
                json={
                    "items": [{"source_id": "src_1", "name": "crm"}],
                    "next_cursor": None,
                },
            )
        return httpx.Response(200, json=_target("Customer", "customer_number"))

    _client(handler).sources.targets.get("crm", "Customer")
    assert [r.url.path for r in seen] == ["/v1/sources", "/v1/sources/src_1/targets/Customer"]


def test_a_service_account_is_refused_a_target_by_name_before_anything_is_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("the request must not be sent")

    machine = Client.for_service_account(
        "https://masterly.test", token="m2m:dev:svc_example", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PermissionError, match="source id"):
        machine.sources.targets.get("crm", "Customer")


def test_an_empty_model_name_is_refused_before_the_wire() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("the request must not be sent")

    with pytest.raises(ValueError, match="model names the target"):
        _client(handler).sources.targets.get("src_1", "")
