"""`mapping.ignored_fields`: a source's own fields dropped before the field map, so a field
named like a model attribute but not meant for it lands on nothing.

Replayed over a mock transport in the shape the `/v1` contract records for `SourceMapping`
(`docs/contract/openapi.json` of masterly-application-backend at a19228c): an optional array
of field names, stored with every default spelled out, so a mapping read back carries
`ignored_fields` even when it is empty. The assertions are on what the client sent, which is
the half of the contract the client owns, and on what it hands back.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from masterly import Client, SourceTarget


def _client(handler: Any) -> Client:
    return Client(
        "https://masterly.test",
        token="tok",
        environment="env_prod_eu",
        transport=httpx.MockTransport(handler),
    )


def _mapping(**extra: Any) -> dict[str, Any]:
    """A stored mapping: every default spelled out, as the server returns it."""
    return {
        "field_map": {"KUNNR": "customer_number"},
        "source_key": ["customer_number"],
        "ignored_fields": [],
        "translations": [],
        **extra,
    }


def _source(mapping: dict[str, Any], version: int = 1) -> dict[str, Any]:
    target = {"model_name": "Customer", "mapping": mapping, "version": str(version)}
    return {
        "source_id": "src_1",
        "name": "erp",
        "targets": [target],
        "target_model": "Customer",
        "mapping": mapping,
        "version": str(version),
    }


def test_a_source_is_created_ignoring_a_field_and_reads_it_back() -> None:
    seen: list[httpx.Request] = []
    stored = _mapping(ignored_fields=["NAME2"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = 201 if request.method == "POST" else 200
        return httpx.Response(status, json=_source(stored))

    client = _client(handler)
    client.sources.create(
        "erp",
        target_model="Customer",
        source_key="customer_number",
        field_map={"KUNNR": "customer_number"},
        ignored_fields=["NAME2"],
    )

    assert json.loads(seen[0].content)["mapping"] == {
        "field_map": {"KUNNR": "customer_number"},
        "source_key": ["customer_number"],
        "ignored_fields": ["NAME2"],
    }
    source = client.sources.get("src_1")
    assert source["mapping"]["ignored_fields"] == ["NAME2"]
    assert source["targets"][0]["mapping"]["ignored_fields"] == ["NAME2"]


def test_a_mapping_without_ignored_fields_is_sent_exactly_as_before() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=_source(_mapping()))

    _client(handler).sources.create("erp", target_model="Customer", source_key="customer_number")

    assert "ignored_fields" not in json.loads(seen[0].content)["mapping"]


def test_a_target_ignores_fields_on_create_and_on_add() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=_source(_mapping()))

    client = _client(handler)
    client.sources.create(
        "crm",
        targets=[
            SourceTarget("Customer", source_key="customer_number", ignored_fields=("NAME2",)),
        ],
    )
    client.sources.targets.add("src_1", "Address", source_key="address_id", ignored_fields=["FAX"])

    created = json.loads(seen[0].content)["targets"][0]["mapping"]
    assert created["ignored_fields"] == ["NAME2"]  # any sequence goes on the wire as a list
    added = json.loads(seen[1].content)["mapping"]
    assert added == {"field_map": {}, "source_key": ["address_id"], "ignored_fields": ["FAX"]}


def test_ignoring_one_more_field_edits_the_read_mapping_and_replaces_it() -> None:
    seen: list[httpx.Request] = []
    current = _mapping(ignored_fields=["NAME2"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_source(current, version=3))
        return httpx.Response(200, json=_source(json.loads(request.content)["mapping"], 4))

    client = _client(handler)
    source = client.sources.get("src_1")
    mapping = source["mapping"]
    mapping["ignored_fields"] = [*mapping.get("ignored_fields", []), "FAX"]
    updated = client.sources.update("src_1", mapping=mapping, if_match=source["version"])

    sent = json.loads(seen[1].content)["mapping"]
    assert sent["ignored_fields"] == ["NAME2", "FAX"]
    assert sent["translations"] == [], "the rest of the read mapping travels with the edit"
    assert updated["mapping"]["ignored_fields"] == ["NAME2", "FAX"]


def test_a_bare_string_is_refused_rather_than_ignoring_each_character() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("should not reach the API")

    client = _client(handler)
    with pytest.raises(TypeError, match="list of field names"):
        client.sources.create(
            "erp",
            target_model="Customer",
            source_key="customer_number",
            ignored_fields="NAME2",
        )
    with pytest.raises(TypeError, match="list of field names"):
        client.sources.targets.add("src_1", "Address", source_key="id", ignored_fields="FAX")


def test_ignored_fields_with_targets_is_refused_as_mixing_the_two_forms() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("should not reach the API")

    with pytest.raises(ValueError, match="not both"):
        _client(handler).sources.create(
            "crm",
            targets=[SourceTarget("Customer", source_key="customer_number")],
            ignored_fields=["NAME2"],
        )
