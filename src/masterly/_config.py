"""Configuration: the Workspaces, Domains and Data Models a delivery is defined against.

Extract and ingest move data through a configuration someone already built. These methods
build it — enough of it to stand up an Environment from a script: a Workspace, a Domain
inside it, a Data Model with its attributes and keys, and the publish that makes that model
the contract every channel serves.

Two things about this surface are worth knowing before you use it.

**It is configuration, not data.** Nothing here writes master data, and the objects it
creates are versioned, JSON-Schema-validated documents the product promotes between
Environments. An Environment in GitOps config mode applies its configuration from a repo,
and a Click-Ops write against it is refused — as is any write to a config-protected
Environment. That refusal is the system working; do not route around it.

**Editing a Data Model is a governed write.** More than one principal edits models, and an
edit is derived from a read of the current definition, so :meth:`DataModelsApi.update`
requires the revision it replaces (ADR 0070). Read, edit, write::

    model = client.data_models.get("Customer")
    definition = model["definition"]
    definition["attributes"].append({"name": "segment", "type": "string"})
    client.data_models.update("Customer", definition=definition, if_match=model["version"])

A data model's ``version`` is both its published version number and its revision token —
one counter, published in the body and as the read's ``ETag``. When someone else has edited
in the meantime the write is refused with a 409 whose
:attr:`~masterly.ApiError.conflict` names the fields that moved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from masterly._paging import all_items, resolve
from masterly._precondition import Precondition

if TYPE_CHECKING:
    from masterly._client import Client


class WorkspacesApi:
    """Workspaces — the top level a person navigates inside an Environment."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def list(self) -> list[dict[str, Any]]:
        """Every Workspace in this Environment."""
        return all_items(self._client, "/v1/workspaces")

    def create(self, name: str, *, description: str | None = None) -> dict[str, Any]:
        """Create a Workspace. Plans cap how many an Environment may hold; at the cap the
        API refuses with 409 rather than silently trimming."""
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        created: dict[str, Any] = self._client._request("POST", "/v1/workspaces", json=body)
        return created

    def _resolve(self, workspace: str) -> str:
        return resolve(
            self.list(), workspace, id_field="workspace_id", prefix="ws_", kind="workspace"
        )


class DomainsApi:
    """Domains — the subject areas a Workspace is divided into."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def list(self, *, workspace: str | None = None) -> list[dict[str, Any]]:
        """Every Domain in this Environment, or only those in one Workspace."""
        domains = all_items(self._client, "/v1/domains")
        if workspace is None:
            return domains
        workspace_id = WorkspacesApi(self._client)._resolve(workspace)
        return [d for d in domains if d.get("workspace_id") == workspace_id]

    def create(
        self, name: str, *, workspace: str, description: str | None = None
    ) -> dict[str, Any]:
        """Create a Domain inside a Workspace, named by id or by name."""
        body: dict[str, Any] = {
            "workspace_id": WorkspacesApi(self._client)._resolve(workspace),
            "name": name,
        }
        if description is not None:
            body["description"] = description
        created: dict[str, Any] = self._client._request("POST", "/v1/domains", json=body)
        return created

    def _resolve(self, domain: str) -> str:
        return resolve(self.list(), domain, id_field="domain_id", prefix="dom_", kind="domain")


class DataModelsApi:
    """Data Models — the canonical shape a Domain masters, and the contract every channel
    serves once it is published.

    A model's ``definition`` carries its attributes (types, required, enum values, format and
    range constraints, PII classification), its business keys, and its relationships. Those
    constraints are not documentation: ingest validates every record against them and
    quarantines what fails, so widening or tightening one changes what the pipeline accepts.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def list(self) -> list[dict[str, Any]]:
        """Every Data Model in this Environment, draft and published."""
        return all_items(self._client, "/v1/data-models")

    def get(self, model: str) -> dict[str, Any]:
        """One Data Model by id or name — the read an edit starts from.

        Its ``version`` is the revision to send back as ``if_match`` when you write.
        """
        got: dict[str, Any] = self._client._request(
            "GET", f"/v1/data-models/{self._resolve(model)}"
        )
        return got

    def create(
        self,
        name: str,
        *,
        domain: str,
        definition: Mapping[str, Any] | None = None,
        kind: str = "entity",
        description: str | None = None,
        owner: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Create a Data Model as a draft. It masters nothing until it is published.

        ``kind`` is ``entity`` — flows through identity resolution and golden resolution —
        or ``reference`` for a controlled list.
        """
        body: dict[str, Any] = {
            "domain_id": DomainsApi(self._client)._resolve(domain),
            "name": name,
            "kind": kind,
        }
        if definition is not None:
            body["definition"] = dict(definition)
        if description is not None:
            body["description"] = description
        if owner is not None:
            body["owner"] = owner
        if tags is not None:
            body["tags"] = list(tags)
        created: dict[str, Any] = self._client._request("POST", "/v1/data-models", json=body)
        return created

    def update(
        self,
        model: str,
        *,
        if_match: Precondition | str | int,
        definition: Mapping[str, Any] | None = None,
        name: str | None = None,
        description: str | None = None,
        owner: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Edit the draft. Publishing is the separate, gated :meth:`publish`.

        ``if_match`` is the ``version`` of the model you read — required, because someone
        else may have edited it since, and a governed write says which revision it replaces
        rather than overwriting whatever it finds. ``definition`` REPLACES the definition
        document wholesale, so edit the one you read rather than assembling a partial.
        """
        body: dict[str, Any] = {}
        if definition is not None:
            body["definition"] = dict(definition)
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if owner is not None:
            body["owner"] = owner
        if tags is not None:
            body["tags"] = list(tags)
        updated: dict[str, Any] = self._client._request(
            "PATCH", f"/v1/data-models/{self._resolve(model)}", json=body, if_match=if_match
        )
        return updated

    def publish(self, model: str, *, allow_breaking: bool = False) -> dict[str, Any]:
        """Publish the draft, snapshotting a version — the act that makes the definition the
        contract consumers read.

        A change that breaks existing consumers (dropping an attribute, tightening a type)
        must be acknowledged with ``allow_breaking``, and a production Environment refuses it
        outright. Answers the published model plus the classified list of changes; publishing
        an unchanged draft is not an error.
        """
        published: dict[str, Any] = self._client._request(
            "POST",
            f"/v1/data-models/{self._resolve(model)}:publish",
            json={"allow_breaking": allow_breaking},
        )
        return published

    def _resolve(self, model: str) -> str:
        return resolve(self.list(), model, id_field="model_id", prefix="dm_", kind="data model")
