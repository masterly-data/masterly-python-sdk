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
from typing import TYPE_CHECKING, Any, Literal, get_args

from masterly._extract import RowPages
from masterly._paging import all_items
from masterly._precondition import Precondition

if TYPE_CHECKING:
    from masterly._client import Client

_BATCH_SIZE = 500

#: How a batch relates to what the source already holds. ``incremental`` applies only what
#: the batch names; ``full`` declares it the source's complete snapshot.
IngestMode = Literal["incremental", "full"]


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
        """Every Source registered in this Environment. Session persona only."""
        self._client._require_session(
            "listing Sources",
            "A service account is told which Sources it may push into by its `ingest` scope; "
            "keep those ids in the job's configuration.",
        )
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
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Register a Source that delivers into a model.

        ``name`` is the Source's identity and never changes once it exists: a slug of lowercase
        letters, digits and single hyphens (``crm``, ``erp-suppliers``), at most 63 characters.
        ``display_name`` is the label people read in the product — free text that can be
        changed later with :meth:`update`; it defaults to ``name``.

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
        if display_name is not None:
            body["display_name"] = display_name
        created: dict[str, Any] = self._client._request("POST", "/v1/sources", json=body)
        return created

    def update(
        self,
        source: str,
        *,
        if_match: Precondition | str | int,
        name: str | None = None,
        system_type: str | None = None,
        target_model: str | None = None,
        mapping: Mapping[str, Any] | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Edit a Source.

        ``if_match`` is the ``version`` of the Source you read — required, because someone
        else may have edited it since, and a governed write says which revision it replaces
        rather than overwriting whatever it finds.

        ``display_name`` relabels the Source. Its ``name`` never changes: the server refuses a
        different one with 409 ``SOURCE_NAME_IMMUTABLE`` and accepts the current one, so the
        parameter is only there for a caller that sends the whole object back.

        ``mapping`` REPLACES the mapping document, so pass the one you read from
        :meth:`get` with your edit applied — assembling a partial drops whatever else it
        held, such as code translations.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if display_name is not None:
            body["display_name"] = display_name
        if system_type is not None:
            body["system_type"] = system_type
        if target_model is not None:
            body["target_model"] = target_model
        if mapping is not None:
            body["mapping"] = dict(mapping)
        updated: dict[str, Any] = self._client._request(
            "PATCH", f"/v1/sources/{self._resolve(source)}", json=body, if_match=if_match
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
        """Accept a source id (``src_…``) or its exact name (a name costs a listing, which is
        a session route — so a service-account connection must pass the id)."""
        if source.startswith("src_"):
            return source
        self._client._require_session(
            f"addressing Source '{source}' by name",
            "Pass the source id (`src_…`) that the account's `ingest` scope names instead.",
        )
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
        mode: IngestMode = "incremental",
    ) -> IngestReport:
        """Deliver records to a source, chunked into accepted batches.

        Re-delivery is safe: records upsert by their source key, so running the same
        notebook twice never duplicates data.

        **Deleting.** A record that carries ``"op": "delete"`` together with its key field(s)
        deletes the record with that key rather than upserting it —
        ``{"op": "delete", "customer_number": "C-1001"}``. The record is tombstoned, not
        erased: its history closes, it stays readable, and :meth:`restore` undoes it. A delete
        for a key the source does not hold is a no-op. ``op`` is read by the platform and never
        stored as data.

        **Modes.** ``mode="incremental"`` (the default) applies only what the records name.
        ``mode="full"`` declares ``records`` the source's COMPLETE snapshot: after the upserts,
        every live record of the source whose key the snapshot does not carry is deleted, as
        above. The platform reconciles per call, so a full snapshot is sent as exactly one
        batch — this refuses, before sending anything, one that does not fit ``batch_size``.
        Raise ``batch_size`` up to the install's per-call record cap (5,000 unless the install
        set its own) for a larger snapshot; over that cap the call is refused with
        :class:`~masterly.ApiError` code ``INGEST_BATCH_TOO_LARGE``, and nothing is deleted.

        Both token personas ingest (:meth:`~masterly.Client.for_service_account`): a session
        token holding ``ingest:run`` may target any Source in its Environment, and a service
        account only the ids its ``ingest`` scope names — anything else raises
        :class:`~masterly.ApiError` with code ``SERVICE_ACCOUNT_SCOPE_DENIED``. A machine
        connection must pass ``source`` as an id, since resolving a name means listing.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if mode not in get_args(IngestMode):
            raise ValueError(f"mode is 'incremental' or 'full', not {mode!r}")
        if mode == "full":
            # Each call is reconciled on its own, so a snapshot split across calls would have
            # every chunk delete the records the other chunks carry.
            if not records:
                raise ValueError(
                    "a full snapshot carries at least one record — an empty one would say the "
                    "source holds nothing, and the platform does not accept it"
                )
            if len(records) > batch_size:
                raise ValueError(
                    f"a full snapshot is one call: {len(records)} records do not fit "
                    f"batch_size={batch_size}. Raise batch_size (up to the install's record "
                    "cap), or deliver incrementally"
                )
        source_id = self._resolve(source)
        batches = 0
        for start in range(0, len(records), batch_size):
            chunk = records[start : start + batch_size]
            body: dict[str, Any] = {"source_id": source_id, "records": chunk}
            if mode != "incremental":
                body["mode"] = mode
            self._client._request("POST", "/v1/ingest", json=body)
            batches += 1
        return IngestReport(source_id=source_id, records=len(records), batches=batches)

    def history(self, source: str, record_id: str) -> RowPages:
        """Every state a source record has been in, newest first, paged lazily.

        One row per state (``version_id``, ``version``, ``data``, and ``valid_from`` /
        ``valid_to``): the current state has no ``valid_to``, and a deleted record's last
        state is closed with no successor. ``record_id`` is the record's own id (``rec_…``),
        not its source key. The values are shaped by the same field masks as any other read
        of the record. Session persona only.
        """
        return RowPages(
            self._client, f"/v1/sources/{self._resolve(source)}/records/{record_id}/history"
        )

    def restore(self, source: str, record_id: str) -> dict[str, Any]:
        """Undo a delete: reopen the record and return it to its entity, which recomputes.

        Asynchronous like ingest — the answer is the job receipt (``{"job_id": ...}``), and
        golden resolution runs after it. A record that is not deleted is refused with
        :class:`~masterly.ApiError` code ``RECORD_NOT_DELETED``. Needs the ``record:author``
        permission; session persona only.
        """
        accepted: dict[str, Any] = self._client._request(
            "POST", f"/v1/sources/{self._resolve(source)}/records/{record_id}:restore"
        )
        return accepted
