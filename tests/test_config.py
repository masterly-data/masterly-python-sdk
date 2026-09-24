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


# --- the bundle applies: a preview's version is the revision the apply states ---------------


def _bundle_handler(seen: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["method"] = request.url.path, request.method
        seen["body"] = __import__("json").loads(request.content)
        seen["if_match"] = request.headers.get("if-match")
        dry_run = seen["body"].get("dry_run")
        return httpx.Response(
            200,
            json={
                "dry_run": dry_run,
                "diff": {
                    "workspaces": {
                        "created": ["Sales"],
                        "updated": [],
                        "removed": [],
                        "unchanged": 0,
                    }
                },
                "snapshot_id": None if dry_run else "cs_1",
                "version": "7.abc" if dry_run else "8.abc",
            },
        )

    return handler


def test_a_pull_without_a_precondition_is_the_preview_and_states_none() -> None:
    seen: dict[str, Any] = {}
    preview = _client(_bundle_handler(seen)).config.pull(ref="staging")
    assert (seen["method"], seen["path"]) == ("POST", "/v1/config:pull")
    assert seen["body"] == {"dry_run": True, "ref": "staging"}
    assert seen["if_match"] is None
    assert preview["version"] == "7.abc"


def test_a_pull_with_the_previews_version_applies_and_states_it() -> None:
    seen: dict[str, Any] = {}
    client = _client(_bundle_handler(seen))
    preview = client.config.pull()
    applied = client.config.pull(if_match=preview["version"])
    assert seen["body"] == {"dry_run": False}
    assert seen["if_match"] == '"7.abc"'  # the bare token, quoted for the wire, never parsed
    assert applied["snapshot_id"] == "cs_1"


def test_an_import_sends_the_files_as_a_set_and_the_preview_version_on_apply() -> None:
    seen: dict[str, Any] = {}
    client = _client(_bundle_handler(seen))
    files = {"workspaces/sales.yaml": "name: Sales\n"}
    preview = client.config.import_files(files)
    assert seen["body"] == {
        "files": [{"path": "workspaces/sales.yaml", "content": "name: Sales\n"}],
        "dry_run": True,
    }
    client.config.import_files(files, if_match=preview["version"])
    assert (seen["path"], seen["body"]["dry_run"], seen["if_match"]) == (
        "/v1/config:import",
        False,
        '"7.abc"',
    )


def test_a_promotion_is_addressed_to_the_target_and_names_the_source() -> None:
    seen: dict[str, Any] = {}
    client = _client(_bundle_handler(seen))
    preview = client.config.promote("env_prod", source="env_stage")
    assert seen["path"] == "/v1/environments/env_prod/config:promote-from"
    assert seen["body"] == {"source_environment_id": "env_stage", "dry_run": True}
    client.config.promote("env_prod", source="env_stage", if_match=preview["version"])
    assert (seen["body"]["dry_run"], seen["if_match"]) == (False, '"7.abc"')


def test_a_stale_bundle_apply_surfaces_the_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-match"] == '"7.abc"'
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "VERSION_CONFLICT",
                    "message": "This environment-config moved to version 9.abc.",
                    "details": {
                        "object_type": "environment-config",
                        "object_id": "env_test",
                        "base_version": "7.abc",
                        "current_version": "9.abc",
                        "changed_by": "maria@example.com",
                        "changed_fields": ["workspace/ws_1"],
                        "undisclosed_changes": 0,
                        "changed_fields_complete": True,
                    },
                }
            },
        )

    with pytest.raises(ApiError) as refused:
        _client(handler).config.pull(if_match="7.abc")
    conflict = refused.value.conflict
    assert conflict is not None
    assert conflict.object_type == "environment-config"
    assert conflict.changed_fields == ("workspace/ws_1",)
    assert conflict.may_auto_merge is True


def test_the_unconditional_precondition_is_a_spelled_overwrite() -> None:
    seen: dict[str, Any] = {}
    _client(_bundle_handler(seen)).config.pull(if_match=Precondition.unconditional())
    assert (seen["body"]["dry_run"], seen["if_match"]) == (False, "*")


def test_configuration_is_a_session_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("refused before the wire")

    machine = Client.for_service_account(
        "https://masterly.test", "tok", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PermissionError, match="session token"):
        machine.config.pull()
