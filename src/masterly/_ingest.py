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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

from masterly._extract import RowPages
from masterly._jobs import poll
from masterly._paging import all_items
from masterly._precondition import Precondition

if TYPE_CHECKING:
    from masterly._client import Client

_BATCH_SIZE = 500

#: How a batch relates to what the source already holds. ``incremental`` applies only what
#: the batch names; ``full`` declares it the source's complete snapshot.
IngestMode = Literal["incremental", "full"]

#: The statuses a full load does not leave on its own.
_LOAD_TERMINAL = frozenset({"completed", "failed", "abandoned"})


@dataclass(frozen=True)
class IngestReceipt:
    """The platform's answer to one batch: the job that will apply it.

    Follow it with :meth:`~masterly.Client.jobs.get` or :meth:`~masterly.Client.jobs.wait`.
    ``records`` is how many records the batch carried, as sent.
    """

    job_id: str
    records: int


@dataclass(frozen=True)
class IngestReport:
    """What was handed to the platform. Acceptance is asynchronous: invalid records land in
    the source's quarantine with a reason rather than failing the call.

    ``receipts`` holds each batch's receipt, in the order the batches were sent. ``load`` is
    set only on the report of a :meth:`SourcesApi.full_load`: the load as read just after it
    was completed — usually still ``completing``, with the reconciliation's ``job_id``.
    :meth:`SourcesApi.wait_for_load` waits for it to end and returns how many records it
    deleted, in ``reconciled``.
    """

    source_id: str
    records: int
    batches: int
    receipts: tuple[IngestReceipt, ...] = ()
    load: dict[str, Any] | None = field(default=None, hash=False)

    @property
    def job_ids(self) -> tuple[str, ...]:
        """Each batch's job id, in the order the batches were sent."""
        return tuple(receipt.job_id for receipt in self.receipts)


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
        if_match: Precondition | str | int | None = None,
        name: str | None = None,
        system_type: str | None = None,
        target_model: str | None = None,
        mapping: Mapping[str, Any] | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Edit a Source.

        ``if_match`` is the ``version`` of the Source you read. Pass it: someone else may have
        edited the Source since, and a governed write says which revision it replaces rather
        than overwriting whatever it finds (ADR 0070). Leaving it out sends no ``If-Match``,
        which is the deprecated unguarded form — the server still accepts it today and answers
        with a ``Deprecation`` header.

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
        above. The platform reconciles each call on its own, so a full snapshot here is sent as
        exactly one batch — this refuses, before sending anything, one that does not fit
        ``batch_size``. Raise ``batch_size`` up to the install's per-call record cap (5,000
        unless the install set its own), or send a larger snapshot with :meth:`full_load`, in
        as many batches as it takes. Over the cap a call is refused with
        :class:`~masterly.ApiError` code ``INGEST_BATCH_TOO_LARGE``, and nothing is deleted.

        **What comes back.** :class:`IngestReport` counts the records and batches and keeps
        each batch's receipt, in the order the batches were sent: the ``job_id`` to follow with
        :meth:`~masterly.Client.jobs.wait`. Every batch is sent with an ``Idempotency-Key``;
        when the connection fails before an answer arrives, the client re-sends the batch with
        the same key, and the platform replays its first answer rather than applying it twice.

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
            # every chunk delete the records the other chunks carry. A full load is the way to
            # split one.
            if not records:
                raise ValueError(
                    "a full snapshot carries at least one record — an empty one would say the "
                    "source holds nothing, and the platform does not accept it"
                )
            if len(records) > batch_size:
                raise ValueError(
                    f"a full snapshot is one call: {len(records)} records do not fit "
                    f"batch_size={batch_size}. Raise batch_size (up to the install's record "
                    "cap), or send it in batches with sources.full_load()"
                )
        source_id = self._resolve(source)
        receipts = [
            self._send_batch(source_id, chunk, mode=mode) for chunk in _chunks(records, batch_size)
        ]
        return IngestReport(
            source_id=source_id,
            records=len(records),
            batches=len(receipts),
            receipts=tuple(receipts),
        )

    def full_load(self, source: str, *, batch_size: int = _BATCH_SIZE) -> FullLoad:
        """Send a full snapshot larger than one call, as one **full load**.

        Use it as a context manager. Inside, :meth:`FullLoad.send` delivers records — all at
        once or a piece at a time, as your pipeline produces them — chunked into batches of
        ``batch_size``. Leaving the block normally completes the load, and the platform then
        deletes every live record of the Source that no batch of the load carried, once, over
        all of them together::

            with client.sources.full_load("src_7f3c9a", batch_size=5000) as load:
                for frame in snapshot_frames:
                    load.send(frame)
            print(load.report.batches, "batches;", load.report.load["status"])

        **A half-sent snapshot never reconciles.** If anything raises inside the block — a
        refused batch, a dropped connection, your own code — the load is abandoned and the
        error is raised: nothing is deleted, and what the batches already sent upserted stays.
        A load that completes with no records sent is abandoned too, since an empty snapshot
        would say the Source holds nothing.

        Opening the load and every batch are sent with an ``Idempotency-Key``. When the
        connection fails before an answer arrives the client re-sends with the same key, so a
        retry never opens a second load or applies a batch twice. A Source takes one load at a
        time: while another is open, opening this one raises :class:`~masterly.ApiError` code
        ``INGEST_LOAD_IN_FLIGHT``, whose ``details`` name the load in the way — complete it or
        abandon it first.

        Completion is asynchronous. ``load.report.load`` is the load as read just after it was
        completed, usually still ``completing``; :meth:`wait_for_load` waits until it has
        ended and says how many records it deleted. Both token personas, under the same scope
        rule as :meth:`ingest`.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        return FullLoad(self, self._resolve(source), batch_size)

    def get_load(self, source: str, load_id: str) -> dict[str, Any]:
        """A full load: its ``status`` (``open``, ``completing``, ``completed``, ``failed`` or
        ``abandoned``), how many ``batches`` and ``records`` it took, and — once completed —
        how many records the reconciliation deleted, in ``reconciled``. Both token personas.
        """
        load: dict[str, Any] = self._client._request(
            "GET", f"/v1/sources/{self._resolve(source)}/loads/{load_id}"
        )
        return load

    def wait_for_load(
        self, source: str, load_id: str, *, timeout: float = 300.0, interval: float = 2.0
    ) -> dict[str, Any]:
        """Read the load every ``interval`` seconds until it has ended — ``completed`` (with
        ``reconciled``, the number of records it deleted), ``failed`` (a batch of the load
        failed, so the snapshot was incomplete and nothing was deleted) or ``abandoned`` — and
        return that read.

        Raises :class:`TimeoutError` when it has not ended within ``timeout`` seconds. The
        load carries on; waiting is only a read. Both token personas.
        """
        source_id = self._resolve(source)
        return poll(
            lambda: self.get_load(source_id, load_id),
            terminal=_LOAD_TERMINAL,
            timeout=timeout,
            interval=interval,
            what=f"load {load_id}",
        )

    def _send_batch(
        self,
        source_id: str,
        chunk: Sequence[dict[str, Any]],
        *,
        mode: IngestMode,
        load_id: str | None = None,
    ) -> IngestReceipt:
        body: dict[str, Any] = {"source_id": source_id, "records": list(chunk)}
        if mode != "incremental":
            body["mode"] = mode
        if load_id is not None:
            body["load_id"] = load_id
        accepted = self._client._keyed_request("POST", "/v1/ingest", json=body)
        return IngestReceipt(job_id=accepted["job_id"], records=len(chunk))

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


def _chunks(records: Sequence[dict[str, Any]], size: int) -> list[Sequence[dict[str, Any]]]:
    return [records[start : start + size] for start in range(0, len(records), size)]


class FullLoad:
    """A full load in progress — see :meth:`SourcesApi.full_load`, which is how you get one."""

    def __init__(self, sources: SourcesApi, source_id: str, batch_size: int) -> None:
        self._sources = sources
        self._client = sources._client
        self._path = f"/v1/sources/{source_id}/loads"
        self._batch_size = batch_size
        self._receipts: list[IngestReceipt] = []
        self._records = 0
        self._report: IngestReport | None = None
        #: The Source the load replaces the contents of.
        self.source_id = source_id
        #: The load's id, once it is open — the block has been entered.
        self.load_id: str | None = None

    @property
    def report(self) -> IngestReport:
        """What the load delivered, with ``load`` set to the load as read after completion.
        Available once the block has been left normally."""
        if self._report is None:
            raise RuntimeError("the load has not been completed — read the report after the block")
        return self._report

    def __enter__(self) -> FullLoad:
        if self.load_id is not None or self._report is not None:
            raise RuntimeError("a full load is sent once; open a new one for the next snapshot")
        opened = self._client._keyed_request("POST", self._path, json=None)
        self.load_id = opened["load_id"]
        return self

    def send(self, records: Sequence[dict[str, Any]]) -> tuple[IngestReceipt, ...]:
        """Deliver part of the snapshot (or all of it), chunked into ``batch_size`` batches,
        each marked as part of this load. Returns this call's receipts; the report keeps them
        all."""
        if self.load_id is None or self._report is not None:
            raise RuntimeError("send() belongs inside the `with` block of an open load")
        sent = tuple(
            self._sources._send_batch(self.source_id, chunk, mode="full", load_id=self.load_id)
            for chunk in _chunks(records, self._batch_size)
        )
        self._receipts.extend(sent)
        self._records += len(records)
        return sent

    def __exit__(self, exc_type: type[BaseException] | None, *rest: object) -> None:
        load_id = self.load_id
        if load_id is None:  # pragma: no cover - __enter__ raised before the load opened
            return
        if exc_type is not None or not self._receipts:
            # A snapshot that did not arrive whole must never reconcile: it would delete every
            # record the missing batches carry. Abandoning also frees the Source for the next
            # load. Best effort — when the block raised, that error is the one worth seeing.
            self._abandon(load_id, quiet=exc_type is not None)
            if exc_type is None:
                raise ValueError(
                    "a full load carries at least one record — an empty snapshot would say "
                    "the source holds nothing, so the load was abandoned"
                )
            return
        try:
            # Not keyed: the platform takes no Idempotency-Key here, because a repeat cannot
            # complete a load twice — it is refused with INGEST_LOAD_NOT_OPEN.
            self._client._request("POST", f"{self._path}/{load_id}:complete")
        except BaseException:
            self._abandon(load_id, quiet=True)
            raise
        self._report = IngestReport(
            source_id=self.source_id,
            records=self._records,
            batches=len(self._receipts),
            receipts=tuple(self._receipts),
            load=self._sources.get_load(self.source_id, load_id),
        )

    def _abandon(self, load_id: str, *, quiet: bool) -> None:
        try:
            self._client._request("POST", f"{self._path}/{load_id}:abandon")
        except Exception:
            if not quiet:
                raise
