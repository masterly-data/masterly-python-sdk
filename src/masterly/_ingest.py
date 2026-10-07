"""Sources and ingest: register where records come from, then deliver them.

Batches are chunked client-side and accepted asynchronously by the platform (202) —
mapping, validation, quarantine, matching, and golden resolution run exactly as for every
other channel.

A Source is the delivering system. It feeds one or more data models — its **targets** —
and each target carries the two pieces of configuration that decide what happens to
everything delivered into it: the field map that conforms the system's vocabulary to that
model's attribute names, and the source key that says which attributes form the record's
stable natural key. The key is what makes re-delivery an upsert instead of a duplicate — a
target registered without one quarantines every record it is ever handed, which is why
:meth:`SourcesApi.create` and :meth:`SourceTargetsApi.add` will not let you leave it out.
Records, quarantine, stats and loads are per target: a batch lands in one target, named by
``model``, and on a Source with one target the name may be left out.

A Source also has a lifecycle. :meth:`SourcesApi.deactivate` stops it admitting records and
keeps everything it landed, still counting toward golden records, until
:meth:`SourcesApi.reactivate`; :meth:`SourcesApi.delete` removes it and everything it landed
as a job, after :meth:`SourcesApi.deletion_impact` has said what that would touch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args
from urllib.parse import quote

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


@dataclass(frozen=True)
class SourceTarget:
    """One data model a Source feeds, as you register it — for :meth:`SourcesApi.create`.

    ``model_name`` is the model; it must have a published version. ``source_key`` names the
    attribute (or attributes) that form this system's natural key within that model, AFTER
    the field map has been applied — required, because a target without one quarantines
    everything delivered into it. ``field_map`` maps this system's own field names to the
    model's attribute names (``{"NAME1": "name"}``); leave it out when the system already
    speaks the model's names. ``translations`` are the target's code translations, in the
    shape the platform stores them, for a target registered with them from the start.
    """

    model_name: str
    source_key: str | Sequence[str] = field(hash=False)
    field_map: Mapping[str, str] | None = field(default=None, hash=False)
    translations: Mapping[str, Any] | None = field(default=None, hash=False)

    def _spec(self) -> dict[str, Any]:
        if not self.model_name:
            raise ValueError("a target names the model it feeds")
        return {
            "model_name": self.model_name,
            "mapping": _mapping(self.source_key, self.field_map, self.translations),
        }


def _mapping(
    source_key: str | Sequence[str],
    field_map: Mapping[str, str] | None,
    translations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    key = [source_key] if isinstance(source_key, str) else list(source_key)
    if not key:
        raise ValueError("source_key names the attribute(s) forming the record's natural key")
    mapping: dict[str, Any] = {"field_map": dict(field_map or {}), "source_key": key}
    if translations is not None:
        mapping["translations"] = dict(translations)
    return mapping


def _model_path(model: str) -> str:
    """A model name as a path segment: a name is free text, so it is encoded whole."""
    if not model:
        raise ValueError("model names the target — one of the Source's targets[].model_name")
    return quote(model, safe="")


class SourcesApi:
    def __init__(self, client: Client) -> None:
        self._client = client
        #: The data models a Source feeds — list, add, read, edit and remove a target.
        self.targets = SourceTargetsApi(self)

    def list(self) -> list[dict[str, Any]]:
        """Every Source registered in this Environment. Session persona only."""
        self._client._require_session(
            "listing Sources",
            "A service account is told which Sources it may push into by its `ingest` scope; "
            "keep those ids in the job's configuration.",
        )
        return all_items(self._client, "/v1/sources")

    def get(self, source: str) -> dict[str, Any]:
        """One Source by id or name, with its targets, status and revision.

        ``targets`` lists the data models the Source feeds, in creation order, each with its
        own ``mapping``, ``connector``, ``pull_state``, ``profile`` and ``drift``
        (:attr:`targets` reads and edits them). The top-level ``target_model``, ``mapping``,
        ``connector``, ``pull_state``, ``profile`` and ``drift`` are deprecated and describe
        the FIRST target — on a Source with one target, exactly what they always described.
        ``status`` is the Source's lifecycle: ``active``, or ``inactive`` after
        :meth:`deactivate`. ``version`` is the revision to state on the next governed write.
        """
        got: dict[str, Any] = self._client._request("GET", f"/v1/sources/{self._resolve(source)}")
        return got

    def create(
        self,
        name: str,
        *,
        target_model: str | None = None,
        source_key: str | Sequence[str] | None = None,
        field_map: Mapping[str, str] | None = None,
        targets: Sequence[SourceTarget] | None = None,
        system_type: str = "rest",
        mode: str = "push",
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Register a Source that delivers into one model, or into several.

        ``name`` is the Source's identity and never changes once it exists: a slug of lowercase
        letters, digits and single hyphens (``crm``, ``erp-suppliers``), at most 63 characters.
        ``display_name`` is the label people read in the product — free text that can be
        changed later with :meth:`update`; it defaults to ``name``.

        **One model.** ``target_model`` names it, ``source_key`` the model attribute (or
        attributes) that form this system's natural key, AFTER the field map has been applied
        — required, because a source without one quarantines everything it delivers — and
        ``field_map`` maps this system's own field names to the model's attribute names
        (``{"NAME1": "name"}``); leave it out when the system already speaks the model's names
        and every field passes through untouched. This form sends ``target_model`` and
        ``mapping`` exactly as it always has.

        **Several models.** Pass ``targets`` instead: one :class:`SourceTarget` per model the
        system holds, each with its own key and field map — a CRM export that carries
        customers and their addresses is one Source with two targets. The Source is sent with
        ``targets`` and neither ``target_model`` nor ``mapping``. Every target's model must
        have a published version, and a model may appear once. One batch then lands in ONE
        target, so :meth:`ingest` is called once per model, with ``model`` naming it. A
        target can be added to an existing Source later with :meth:`SourceTargetsApi.add`.
        """
        body: dict[str, Any] = {"name": name, "system_type": system_type, "mode": mode}
        if targets is not None:
            if target_model is not None or source_key is not None or field_map is not None:
                raise ValueError(
                    "pass either targets= (one SourceTarget per model) or target_model= with "
                    "source_key= and field_map= — not both"
                )
            if not targets:
                raise ValueError("a Source feeds at least one model — targets is empty")
            body["targets"] = [target._spec() for target in targets]
        else:
            if target_model is None or source_key is None:
                raise ValueError(
                    "a Source names what it feeds: target_model= with source_key=, or targets="
                )
            body["target_model"] = target_model
            body["mapping"] = _mapping(source_key, field_map)
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
        held, such as code translations. ``target_model`` and ``mapping`` here write the
        Source's FIRST target and are deprecated: on a Source with several targets, edit any
        target's mapping with :meth:`SourceTargetsApi.update` instead. Both are still sent
        only when passed.
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

    def stats(self, source: str, *, model: str | None = None) -> dict[str, Any]:
        """How much this Source has delivered: ``records`` accepted, ``quarantined`` rejected.

        The two counts cover the whole Source, and ``targets`` carries the same counts per
        target. With ``model`` the two counts are that one target's — the answer then names it
        in ``model_name``. Ingest is asynchronous, so poll this after delivering rather than
        reading it straight back — the counts move as the pipeline works through the batch.
        Both token personas; a service account reads the Sources its ``ingest`` scope names.
        """
        params = {"model_name": model} if model is not None else None
        stats: dict[str, Any] = self._client._request(
            "GET", f"/v1/sources/{self._resolve(source)}/stats", params=params
        )
        return stats

    def quarantine(
        self,
        source: str,
        *,
        model: str | None = None,
        status: str | None = None,
        reason_code: str | None = None,
    ) -> RowPages:
        """The Source's quarantine — every row held with the payload as it arrived, the
        ``reason`` and its value-free ``reason_code`` — paged lazily.

        Each row names the target it was held for in ``model_name``; ``model`` lists one
        target's rows only. ``status`` is ``open``, ``resolved`` or ``discarded``, and
        ``reason_code`` one code of the summary (``translation-miss:country:countries``), to
        review one cause at a time. Both token personas: a service account reads the
        quarantine of the Sources its ``ingest`` scope names, each payload masked exactly as
        it is for a person. Working the quarantine — retrying, reprocessing, discarding — is
        a person's job, through the app or :meth:`~masterly.Client.request`.
        """
        params: dict[str, Any] = {}
        if model is not None:
            params["model_name"] = model
        if status is not None:
            params["status"] = status
        if reason_code is not None:
            params["reason_code"] = reason_code
        return RowPages(self._client, f"/v1/sources/{self._resolve(source)}/quarantine", params)

    # --- lifecycle: deactivate, reactivate, delete --------------------------------------

    def deactivate(
        self, source: str, *, if_match: Precondition | str | int | None = None
    ) -> dict[str, Any]:
        """Stop a Source admitting records, keeping everything it has landed.

        An inactive Source refuses every way in — a pushed batch, a CSV upload, MCP, a full
        load's next batch (opening or completing one too), a pull and a quarantine retry —
        with :class:`~masterly.ApiError` code ``SOURCE_INACTIVE``, and queues nothing; its
        pull schedule is suspended and delivery monitoring stops raising incidents for it.
        Batches accepted before the deactivation still complete, and a load left open can
        still be abandoned. Its records, history, quarantine and runs all stay, and **its
        records keep contributing to golden records** — nothing is recomputed, in either
        direction. :meth:`reactivate` undoes it with nothing lost.

        ``if_match`` is the ``version`` of the Source you read, sent as ``If-Match`` — a
        governed write (ADR 0070), as :meth:`update` is. A Source that is already inactive
        answers with its current view and changes nothing, before the precondition is
        checked, so a retry of a deactivation that landed is absorbed. The model's built-in
        manual source is refused with code ``SOURCE_BUILT_IN``. Returns the Source, with
        ``status`` ``inactive``. Needs the ``source:update`` permission; session persona.
        """
        deactivated: dict[str, Any] = self._client._request(
            "POST", f"/v1/sources/{self._resolve(source)}:deactivate", if_match=if_match
        )
        return deactivated

    def reactivate(
        self, source: str, *, if_match: Precondition | str | int | None = None
    ) -> dict[str, Any]:
        """Let a deactivated Source admit records again.

        Every channel accepts it from now; a pull schedule resumes with a fresh window (no
        catch-up) and delivery monitoring measures lateness from now. Nothing is recomputed.
        ``if_match`` is the ``version`` you read, as on :meth:`deactivate`; a Source that is
        already active answers with its current view and changes nothing. Returns the Source,
        with ``status`` ``active``. Needs the ``source:update`` permission; session persona.
        """
        reactivated: dict[str, Any] = self._client._request(
            "POST", f"/v1/sources/{self._resolve(source)}:reactivate", if_match=if_match
        )
        return reactivated

    def deletion_impact(self, source: str) -> dict[str, Any]:
        """What :meth:`delete` would remove and touch — read it before you delete.

        Per target, the live ``records``, ``tombstoned`` records and open ``quarantined``
        rows that go; ``golden.would_change`` and ``golden.would_clear``, the entities whose
        golden record is recomputed without the Source and the ones left with no live record;
        the ``open_tasks`` and ``open_incidents`` the delete closes; the
        ``service_account_scopes`` it is removed from; and ``dependents``, the data products
        that read the Source through a raw relation — while any exists ``deletable`` is false
        and the delete is refused. ``name`` is what a confirming client asks the user to type
        back. Takes the delete's permission, ``source:delete``; session persona.
        """
        impact: dict[str, Any] = self._client._request(
            "GET", f"/v1/sources/{self._resolve(source)}/deletion-impact"
        )
        return impact

    def delete(
        self, source: str, *, if_match: Precondition | str | int | None = None
    ) -> dict[str, Any]:
        """Delete a Source and everything it landed, as a job.

        Gone with it: its records in every target and their history, its quarantine rows,
        profile, drift, run history, schedule and sealed connection. The entities it
        contributed to have their golden records **recomputed without it**, and one left with
        no live record clears. Its open tasks and delivery incidents close with the reason
        ``source-deleted``, and every service-account ``ingest`` scope naming it is edited.
        Read :meth:`deletion_impact` first; a client that asks a person to confirm asks them
        to type the Source's ``name``.

        Heavy work, so the answer is the job receipt (``{"job_id": ...}``): the Source is
        marked ``deleting`` at once — it admits nothing and is absent from every read — and
        :meth:`~masterly.Client.jobs.wait` follows the removal to its end. Refused with
        :class:`~masterly.ApiError` code ``SOURCE_IN_USE`` while a data product reads the
        Source through a raw relation (``details["dependents"]`` names each one — change or
        delete the product first), and with ``SOURCE_BUILT_IN`` for the model's built-in
        manual source. ``if_match`` is the ``version`` you read, as on :meth:`update`; a
        Source already being deleted answers the pending job's receipt and writes nothing,
        before the precondition is checked. Its ``name`` can be used again once the job has
        run. Needs the ``source:delete`` permission; session persona.
        """
        accepted: dict[str, Any] = self._client._request(
            "POST", f"/v1/sources/{self._resolve(source)}:delete", if_match=if_match
        )
        return accepted

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
        model: str | None = None,
    ) -> IngestReport:
        """Deliver records to a source, chunked into accepted batches.

        Re-delivery is safe: records upsert by their source key, so running the same
        notebook twice never duplicates data.

        **The target.** A batch lands in ONE of the Source's targets, and ``model`` names it —
        one of the Source's ``targets[].model_name``. On a Source with one target leave it out
        and the batch lands there, exactly as before targets existed: nothing extra is sent.
        On a Source with several targets it is required — an unnamed batch is refused before
        anything is queued with :class:`~masterly.ApiError` code ``SOURCE_TARGET_REQUIRED``,
        whose ``details["targets"]`` lists the models — and a system that holds customers and
        addresses makes two calls, one per model. A model the Source does not feed is refused
        with ``SOURCE_TARGET_NOT_FOUND``.

        **Deleting.** A record that carries ``"op": "delete"`` together with its key field(s)
        deletes the record with that key rather than upserting it —
        ``{"op": "delete", "customer_number": "C-1001"}``. The record is tombstoned, not
        erased: its history closes, it stays readable, and :meth:`restore` undoes it. A delete
        for a key the source does not hold is a no-op. ``op`` is read by the platform and never
        stored as data.

        **Modes.** ``mode="incremental"`` (the default) applies only what the records name.
        ``mode="full"`` declares ``records`` the TARGET's COMPLETE snapshot: after the upserts,
        every live record of that target whose key the snapshot does not carry is deleted, as
        above — the Source's other targets are untouched. The platform reconciles each call
        on its own, so a full snapshot here is sent as exactly one batch — this refuses,
        before sending anything, one that does not fit ``batch_size``. Raise ``batch_size``
        up to the install's per-call record cap (5,000 unless the install set its own), or
        send a larger snapshot with :meth:`full_load`, in as many batches as it takes. Over
        the cap a call is refused with :class:`~masterly.ApiError` code
        ``INGEST_BATCH_TOO_LARGE``, and nothing is deleted.

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
            self._send_batch(source_id, chunk, mode=mode, model_name=model)
            for chunk in _chunks(records, batch_size)
        ]
        return IngestReport(
            source_id=source_id,
            records=len(records),
            batches=len(receipts),
            receipts=tuple(receipts),
        )

    def upload_csv(
        self,
        source: str,
        data: str | bytes,
        *,
        model: str | None = None,
        delimiter: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Deliver a CSV file — ``data`` is its text, or its bytes in UTF-8 — as one batch.

        A file is just another way in: header columns map case-insensitively to the target
        model's attributes, unknown columns are ignored and reported back in
        ``ignored_columns``, and every row runs the same mapping, validation and quarantine
        pipeline a pushed record does, so re-uploading a corrected file updates records by
        their source key rather than duplicating them. The file is for ONE target: ``model``
        names it, or the Source's only target, with the same refusals as :meth:`ingest`.
        ``delimiter`` is ``,`` unless you say otherwise (``;`` or a tab); ``filename`` is
        recorded on the run. A file over 5 MB is refused with :class:`~masterly.ApiError` code
        ``CSV_TOO_LARGE``, and one the CSV reader cannot parse whole with ``CSV_MALFORMED``,
        naming the line — nothing of it is ingested.

        Answers the receipt: ``job_id`` to follow with :meth:`~masterly.Client.jobs.wait`,
        ``records`` the file carried, and ``ignored_columns``. Sent with an ``Idempotency-Key``
        and re-sent under the same key when the connection fails before an answer, as a
        batch is. Both token personas, under the same scope rule as :meth:`ingest`.
        """
        params: dict[str, Any] = {}
        if model is not None:
            params["model_name"] = model
        if delimiter is not None:
            params["delimiter"] = delimiter
        if filename is not None:
            params["filename"] = filename
        accepted: dict[str, Any] = self._client._keyed_request(
            "POST",
            f"/v1/sources/{self._resolve(source)}/upload",
            content=data.encode("utf-8") if isinstance(data, str) else data,
            params=params or None,
            headers={"Content-Type": "text/csv; charset=utf-8"},
        )
        return accepted

    def full_load(
        self, source: str, *, batch_size: int = _BATCH_SIZE, model: str | None = None
    ) -> FullLoad:
        """Send a full snapshot larger than one call, as one **full load**.

        Use it as a context manager. Inside, :meth:`FullLoad.send` delivers records — all at
        once or a piece at a time, as your pipeline produces them — chunked into batches of
        ``batch_size``. Leaving the block normally completes the load, and the platform then
        deletes every live record of the target that no batch of the load carried, once, over
        all of them together::

            with client.sources.full_load("src_7f3c9a", batch_size=5000) as load:
                for frame in snapshot_frames:
                    load.send(frame)
            print(load.report.batches, "batches;", load.report.load["status"])

        A load is a snapshot of ONE target of the Source: ``model`` names it — one of the
        Source's ``targets[].model_name`` — and may be left out on a Source with one target,
        with the same refusals as :meth:`ingest`. Every batch of the load lands in that
        target, and the completion retires that target's records only.

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
        return FullLoad(self, self._resolve(source), batch_size, model=model)

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
        model_name: str | None = None,
    ) -> IngestReceipt:
        body: dict[str, Any] = {"source_id": source_id, "records": list(chunk)}
        if mode != "incremental":
            body["mode"] = mode
        if load_id is not None:
            body["load_id"] = load_id
        if model_name is not None:
            body["model_name"] = model_name
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


class SourceTargetsApi:
    """The data models a Source feeds — ``client.sources.targets``.

    A target is one model plus everything specific to it: its ``mapping`` (field map, source
    key, code translations), and for a pull source the connector that reads it with its pull
    state, and what the pipeline produced for it — the latest-batch ``profile`` and the
    ``drift`` finding. Records, quarantine rows, stats and loads are per target too: a source
    key names one record within a target, so the same key under two targets is two records.
    Each target has a revision of its own (``version``), stated on :meth:`update` and
    :meth:`remove`; every target write also moves the Source's. Session persona.
    """

    def __init__(self, sources: SourcesApi) -> None:
        self._sources = sources
        self._client = sources._client

    def _path(self, source: str, model: str) -> str:
        return f"/v1/sources/{self._sources._resolve(source)}/targets/{_model_path(model)}"

    def list(self, source: str) -> list[dict[str, Any]]:
        """Every target of the Source, in creation order — the ``targets`` of
        :meth:`SourcesApi.get`. The first is what the deprecated top-level ``target_model``
        and ``mapping`` describe."""
        targets = self._sources.get(source).get("targets") or []
        return [dict(target) for target in targets]

    def get(self, source: str, model: str) -> dict[str, Any]:
        """One target by its model name, with its ``version`` — the revision to state on
        :meth:`update` or :meth:`remove`. A model the Source does not feed raises
        :class:`~masterly.ApiError` with code ``SOURCE_TARGET_NOT_FOUND``."""
        target: dict[str, Any] = self._client._request("GET", self._path(source, model))
        return target

    def add(
        self,
        source: str,
        model: str,
        *,
        source_key: str | Sequence[str],
        field_map: Mapping[str, str] | None = None,
        translations: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a target: one more model the Source feeds, with the field map, source key and
        code translations deliveries to it are read through — the same three
        :class:`SourceTarget` takes.

        The model must have a published version (:class:`~masterly.ApiError` code
        ``MODEL_NOT_PUBLISHED``) and must not be fed by the Source yet
        (``SOURCE_TARGET_EXISTS``); a ``field_map`` target or ``source_key`` attribute the
        model's published version does not declare is refused with
        ``MAPPING_ATTRIBUTE_UNKNOWN``. The new target holds no records. For a pull source, its
        connector is configured afterwards with ``PUT /v1/sources/{id}/connector?model_name=``
        through :meth:`~masterly.Client.request`. The Source's revision moves: read the
        Source again before its next governed write. Returns the target.
        """
        body = {
            "model_name": model,
            "mapping": _mapping(source_key, field_map, translations),
        }
        added: dict[str, Any] = self._client._request(
            "POST", f"/v1/sources/{self._sources._resolve(source)}/targets", json=body
        )
        return added

    def update(
        self,
        source: str,
        model: str,
        *,
        mapping: Mapping[str, Any],
        if_match: Precondition | str | int | None = None,
    ) -> dict[str, Any]:
        """Replace one target's mapping — a governed write (ADR 0070).

        ``if_match`` is the target's own ``version`` from :meth:`get`, not the Source's; a
        mapping that raced yours is refused rather than overwritten. ``mapping`` REPLACES the
        document, so pass the one you read with your edit applied. A ``source_key`` that
        differs from the current one is refused with :class:`~masterly.ApiError` code
        ``SOURCE_HAS_RECORDS`` while the target holds records, deleted ones included — every
        record it landed is keyed on the current key; a field-map edit or a translation under
        the same key is fine. The mapping is checked against this target's model, as on
        :meth:`add`. Returns the target with its new ``version``.
        """
        updated: dict[str, Any] = self._client._request(
            "PUT",
            self._path(source, model),
            json={"mapping": dict(mapping)},
            if_match=if_match,
        )
        return updated

    def remove(
        self, source: str, model: str, *, if_match: Precondition | str | int | None = None
    ) -> None:
        """Remove a target that holds no records — a governed write; ``if_match`` is the
        target's ``version``.

        Its quarantine rows and ended loads go with it, and the Source's revision moves. A
        target that holds records, live or deleted, is refused with
        :class:`~masterly.ApiError` code ``SOURCE_TARGET_HAS_RECORDS``: removing it is a hard
        delete of that target's data with a golden recompute, which has its own preview and
        confirmation and is not this call. The Source's last target cannot be removed
        (``SOURCE_LAST_TARGET``) — a Source feeds at least one model; delete the Source
        instead.
        """
        self._client._request("DELETE", self._path(source, model), if_match=if_match)


def _chunks(records: Sequence[dict[str, Any]], size: int) -> list[Sequence[dict[str, Any]]]:
    return [records[start : start + size] for start in range(0, len(records), size)]


class FullLoad:
    """A full load in progress — see :meth:`SourcesApi.full_load`, which is how you get one."""

    def __init__(
        self, sources: SourcesApi, source_id: str, batch_size: int, *, model: str | None = None
    ) -> None:
        self._sources = sources
        self._client = sources._client
        self._path = f"/v1/sources/{source_id}/loads"
        self._batch_size = batch_size
        self._model = model
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
        # The target is named once, on the load; every batch of it lands there, so the batches
        # below carry no model of their own.
        params = {"model_name": self._model} if self._model is not None else None
        opened = self._client._keyed_request("POST", self._path, json=None, params=params)
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
