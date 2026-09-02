"""The configuration surface: workspaces, domains, data models, sources.

What these hold to: names resolve to ids, listings follow their cursors, a model edit
travels with the revision it replaces, and a source cannot be registered without the source
key that decides whether its records upsert or quarantine.
"""

from __future__ import annotations

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


def _page(items: list[dict[str, Any]], cursor: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"items": items, "next_cursor": cursor})


def test_workspace_create_posts_name_and_description() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(201, json={"workspace_id": "ws_1", "name": "Demo"})

    workspace = _client(handler).workspaces.create("Demo", description="Generated")
    assert workspace["workspace_id"] == "ws_1"
    assert (seen["method"], seen["path"]) == ("POST", "/v1/workspaces")
    assert seen["body"] == {"name": "Demo", "description": "Generated"}


def test_listings_follow_their_cursor() -> None:
    """A hundredth source must not vanish from a name lookup because page one ended."""

    def handler(request: httpx.Request) -> httpx.Response:
        if dict(request.url.params).get("cursor") == "p2":
            return _page([{"source_id": "src_2", "name": "erp"}])
        return _page([{"source_id": "src_1", "name": "crm"}], cursor="p2")

    sources = _client(handler).sources.list()
    assert [s["name"] for s in sources] == ["crm", "erp"]


def test_domain_create_resolves_its_workspace_by_name() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/workspaces":
            return _page([{"workspace_id": "ws_9", "name": "Demo"}])
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(201, json={"domain_id": "dom_1", "name": "Sales"})

    domain = _client(handler).domains.create("Sales", workspace="Demo")
    assert domain["domain_id"] == "dom_1"
    assert seen["body"] == {"workspace_id": "ws_9", "name": "Sales"}


def test_domains_can_be_filtered_to_one_workspace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/workspaces":
            return _page([{"workspace_id": "ws_1", "name": "Demo"}])
        return _page(
            [
                {"domain_id": "dom_1", "name": "Sales", "workspace_id": "ws_1"},
                {"domain_id": "dom_2", "name": "Ops", "workspace_id": "ws_2"},
            ]
        )

    domains = _client(handler).domains.list(workspace="Demo")
    assert [d["name"] for d in domains] == ["Sales"]


def test_data_model_create_resolves_its_domain_and_carries_the_definition() -> None:
    seen: dict[str, Any] = {}
    definition = {"attributes": [{"name": "id", "type": "string"}], "keys": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/domains":
            return _page([{"domain_id": "dom_7", "name": "Sales"}])
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(201, json={"model_id": "dm_1", "name": "Customer"})

    model = _client(handler).data_models.create(
        "Customer", domain="Sales", definition=definition, tags=["demo"]
    )
    assert model["model_id"] == "dm_1"
    assert seen["body"]["domain_id"] == "dom_7"
    assert seen["body"]["kind"] == "entity"
    assert seen["body"]["definition"] == definition
    assert seen["body"]["tags"] == ["demo"]


def test_publish_posts_the_action_and_defaults_to_refusing_breaking_changes() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/data-models":
            return _page([{"model_id": "dm_3", "name": "Customer"}])
        seen["path"] = request.url.path
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"model": {"model_id": "dm_3"}, "changes": []})

    result = _client(handler).data_models.publish("Customer")
    assert seen["path"] == "/v1/data-models/dm_3:publish"
    assert seen["body"] == {"allow_breaking": False}
    assert result["changes"] == []


def test_model_edit_travels_with_the_revision_it_replaces() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/data-models" and request.method == "GET":
            return _page([{"model_id": "dm_1", "name": "Customer"}])
        seen["if_match"] = request.headers.get("if-match")
        seen["method"] = request.method
        return httpx.Response(200, json={"model_id": "dm_1", "version": 8})

    updated = _client(handler).data_models.update(
        "Customer", definition={"attributes": []}, if_match=7
    )
    assert seen["method"] == "PATCH"
    assert seen["if_match"] == '"7"'  # the bare version, quoted as a strong entity-tag
    assert updated["version"] == 8


def test_a_precondition_object_is_accepted_as_well_as_a_bare_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/data-models" and request.method == "GET":
            return _page([{"model_id": "dm_1", "name": "Customer"}])
        assert request.headers["if-match"] == '"4"'
        return httpx.Response(200, json={"model_id": "dm_1"})

    _client(handler).data_models.update(
        "dm_1", name="Customer", if_match=Precondition.from_version(4)
    )


def test_a_stale_model_edit_surfaces_the_typed_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "VERSION_CONFLICT",
                    "message": "the model moved",
                    "details": {
                        "changed_fields": ["definition"],
                        "changed_fields_complete": True,
                        "undisclosed_changes": 0,
                    },
                }
            },
        )

    with pytest.raises(ApiError) as raised:
        _client(handler).data_models.update("dm_1", definition={}, if_match=1)
    conflict = raised.value.conflict
    assert conflict is not None
    assert list(conflict.changed_fields) == ["definition"]


def test_source_create_builds_the_mapping_from_key_and_field_map() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(201, json={"source_id": "src_1", "name": "erp"})

    _client(handler).sources.create(
        "erp",
        target_model="Customer",
        source_key="customer_number",
        field_map={"KUNNR": "customer_number", "NAME1": "name"},
        system_type="sap",
    )
    assert seen["body"]["target_model"] == "Customer"
    assert seen["body"]["system_type"] == "sap"
    assert seen["body"]["mode"] == "push"
    assert seen["body"]["mapping"] == {
        "field_map": {"KUNNR": "customer_number", "NAME1": "name"},
        "source_key": ["customer_number"],  # a single attribute is accepted as a bare string
    }


def test_a_source_without_a_source_key_is_refused_before_the_wire() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("should not reach the API")

    with pytest.raises(ValueError, match="natural key"):
        _client(handler).sources.create("erp", target_model="Customer", source_key=[])


def test_source_update_replaces_the_mapping_document_it_is_given() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["method"] = request.url.path, request.method
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"source_id": "src_1"})

    mapping = {"field_map": {}, "source_key": ["sku"], "translations": []}
    _client(handler).sources.update("src_1", mapping=mapping)
    assert (seen["method"], seen["path"]) == ("PATCH", "/v1/sources/src_1")
    assert seen["body"] == {"mapping": mapping}


def test_source_stats_reads_the_counters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sources" and request.method == "GET":
            return _page([{"source_id": "src_5", "name": "crm"}])
        assert request.url.path == "/v1/sources/src_5/stats"
        return httpx.Response(200, json={"source_id": "src_5", "records": 42, "quarantined": 3})

    stats = _client(handler).sources.stats("crm")
    assert (stats["records"], stats["quarantined"]) == (42, 3)


@pytest.mark.parametrize(
    ("attribute", "path", "kind"),
    [
        ("workspaces", "/v1/workspaces", "workspace"),
        ("domains", "/v1/domains", "domain"),
        ("data_models", "/v1/data-models", "data model"),
    ],
)
def test_an_unknown_name_says_which_kind_of_thing_is_missing(
    attribute: str, path: str, kind: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _page([])

    api = getattr(_client(handler), attribute)
    with pytest.raises(LookupError, match=f"no {kind} named 'nope'"):
        api._resolve("nope")
