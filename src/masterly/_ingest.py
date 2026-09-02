"""Sources and ingest: register where records come from, then deliver them.

Batches are chunked client-side and accepted asynchronously by the platform (202) —
mapping, validation, quarantine, matching, and golden resolution run exactly as for every
other channel.

A Source is the delivering system, and it carries the two pieces of configuration that
decide what happens to everything it sends: the field map that conforms its vocabulary to
the model's attribute names, and the source key that says which attributes form the
record's stable natural key. The key is what makes re-delivery an upsert instead of a
duplicate — a source registered without one quarantines every record it is ever handed,
which is why :meth:`SourcesApi.create` will not let you leave it out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from masterly._paging import all_items

if TYPE_CHECKING:
    from masterly._client import Client

_BATCH_SIZE = 500


@dataclass(frozen=True)
class IngestReport:
    """What was handed to the platform. Acceptance is asynchronous: invalid records land in
    the source's quarantine with a reason rather than failing the call."""

    source_id: str
    records: int
    batches: int


class SourcesApi:
    def __init__(self, client: Client) -> None:
        self._client = client

    def list(self) -> list[dict[str, Any]]:
        """Every Source registered in this Environment."""
        return all_items(self._client, "/v1/sources")

    def get(self, source: str) -> dict[str, Any]:
        """One Source by id or name, with its mapping, drift and pull state."""
        got: dict[str, Any] = self._client._request("GET", f"/v1/sources/{self._resolve(source)}")
        return got

    def create(
        self,
        name: str,
        *,
        target_model: str,
        source_key: str | Sequence[str],
        field_map: Mapping[str, str] | None = None,
        system_type: str = "rest",
        mode: str = "push",
    ) -> dict[str, Any]:
        """Register a Source that delivers into a model.

        ``source_key`` names the model attribute (or attributes) that form this system's
        natural key, AFTER the field map has been applied — it is required because a source
        without one quarantines everything it delivers. ``field_map`` maps this system's own
        field names to the model's attribute names (``{"NAME1": "name"}``); leave it out when
        the system already speaks the model's names and every field passes through untouched.
        """
        key = [source_key] if isinstance(source_key, str) else list(source_key)
        if not key:
            raise ValueError("source_key names the attribute(s) forming the record's natural key")
        body: dict[str, Any] = {
            "name": name,
            "system_type": system_type,
            "mode": mode,
            "target_model": target_model,
            "mapping": {"field_map": dict(field_map or {}), "source_key": key},
        }
        created: dict[str, Any] = self._client._request("POST", "/v1/sources", json=body)
        return created

    def update(
        self,
        source: str,
        *,
        name: str | None = None,
        system_type: str | None = None,
        target_model: str | None = None,
        mapping: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Edit a Source.

        ``mapping`` REPLACES the mapping document, so pass the one you read from
        :meth:`get` with your edit applied — assembling a partial drops whatever else it
        held, such as code translations.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if system_type is not None:
            body["system_type"] = system_type
        if target_model is not None:
            body["target_model"] = target_model
        if mapping is not None:
            body["mapping"] = dict(mapping)
        updated: dict[str, Any] = self._client._request(
            "PATCH", f"/v1/sources/{self._resolve(source)}", json=body
        )
        return updated

    def stats(self, source: str) -> dict[str, Any]:
        """How much this Source has delivered: ``records`` accepted, ``quarantined`` rejected.

        Ingest is asynchronous, so poll this after delivering rather than reading it straight
        back — the counts move as the pipeline works through the batch.
        """
        stats: dict[str, Any] = self._client._request(
            "GET", f"/v1/sources/{self._resolve(source)}/stats"
        )
        return stats

    def _resolve(self, source: str) -> str:
        """Accept a source id (``src_…``) or its exact name."""
        if source.startswith("src_"):
            return source
        matches = [s for s in self.list() if s.get("name") == source]
        if not matches:
            raise LookupError(f"no source named '{source}'")
        source_id: str = matches[0]["source_id"]
        return source_id

    def ingest(
        self,
        source: str,
        records: Sequence[dict[str, Any]],
        *,
        batch_size: int = _BATCH_SIZE,
    ) -> IngestReport:
        """Deliver records to a source, chunked into accepted batches.

        Re-delivery is safe: records upsert by their source key, so running the same
        notebook twice never duplicates data.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        source_id = self._resolve(source)
        batches = 0
        for start in range(0, len(records), batch_size):
            chunk = records[start : start + batch_size]
            self._client._request(
                "POST", "/v1/ingest", json={"source_id": source_id, "records": chunk}
            )
            batches += 1
        return IngestReport(source_id=source_id, records=len(records), batches=batches)
