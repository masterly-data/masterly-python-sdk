"""Held writes: a change a workflow's gate holds for approval, and the reads that follow it.

Where a customer has configured a change gate, a governed write — ``POST /v1/records``, a pin,
an unpin — may not apply when it is sent. The server stages it as a *pending change*, answers
``202`` with ``status: pending_approval``, and a workflow instance decides it; nothing
downstream sees the change until it applies. A second change to the same cell meanwhile is
refused ``409 CHANGE_PENDING`` (raised as :class:`~masterly.ChangePending`). Where no gate is
configured, none of this happens and the write answers as it always has.

A gate in *review* mode applies the change at once and reviews it afterwards: the write answers
as an applied one, with the ``workflow_instance_id`` that confirms or rejects it set, and
:meth:`WriteResult.of` reads it as ``under_review``.

``client.request`` returns the body either way, unchanged. :meth:`WriteResult.of` is the
opt-in typed view over it, and ``client.pending_changes`` and ``client.workflows`` read what a
held write points at. ``client.workflows.start`` starts a manual-start workflow on records.

A source gate holds an arriving source record in the Source's quarantine with ``reason_code``
``workflow:<key>``, and quarantines one an approver already rejected with
``workflow-rejected:<key>``; :meth:`QuarantineReason.of` reads either from a quarantine row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import quote

from masterly._paging import all_items

if TYPE_CHECKING:
    from masterly._client import Client

#: The 409 code a write gets when a change to one of its cells is already waiting.
CHANGE_PENDING = "CHANGE_PENDING"
PENDING_APPROVAL: Final = "pending_approval"
APPLIED: Final = "applied"
UNDER_REVIEW: Final = "under_review"

_SESSION_REMEDY = (
    "Pending changes and workflows are a person's to read and start: connect with a session "
    "token (`Client(base_url, token, environment)`)."
)


@dataclass(frozen=True)
class WriteResult:
    """Whether a write applied or is waiting for approval — a typed view over the body
    ``client.request`` returned, which is kept as :attr:`body`.

    ::

        result = WriteResult.of(client.request("POST", "/v1/records", json=...))
        if result.held:
            assert result.pending_change_id is not None  # a held result always names its change
            change = client.pending_changes.get(result.pending_change_id)

    A held result occurs only where a customer has configured a change gate, and an
    ``under_review`` one only where that gate is in review mode; everywhere else every write
    reads as applied.
    """

    status: Literal["applied", "pending_approval", "under_review"]
    """``pending_approval``: held, nothing applied. ``under_review``: applied, and a workflow
    instance may still reject it. ``applied``: everything else."""
    pending_change_id: str | None
    """The change a gate staged — read it with ``client.pending_changes.get``. None when no
    gate held the change."""
    workflow_instance_id: str | None
    """The workflow instance deciding or reviewing the change. None when no gate was
    involved."""
    body: Any = field(default=None, compare=False, repr=False)
    """The body as the server sent it."""

    @property
    def held(self) -> bool:
        """True when the write is waiting for approval and nothing of it has applied."""
        return self.status == PENDING_APPROVAL

    @property
    def under_review(self) -> bool:
        """True when the write applied and :attr:`workflow_instance_id` confirms or rejects it
        afterwards."""
        return self.status == UNDER_REVIEW

    @classmethod
    def of(cls, body: Any) -> WriteResult:
        """Read a write's answer. Tolerant by intent: any body that is not a gate's
        ``HeldChange`` and names no reviewing instance — a created record, a pin's recompute
        receipt, None for a 204 — is an applied write with no pending change.

        A ``HeldChange`` with ``status: applied`` is a gate whose instance finished at once:
        the change applied, and the ids still name the change and the instance that applied
        it. Any other body with a ``workflow_instance_id`` — a created record, a pin or a batch
        pin under a review-mode gate — applied and is ``under_review``. A curation route's own
        staged op (``status: pending``) is not a gate's hold and reads as applied here; its
        body says what it is.
        """
        if isinstance(body, Mapping):
            status = body.get("status")
            change_id = body.get("pending_change_id")
            instance_id = body.get("workflow_instance_id")
            # A HeldChange first: one with status applied names its instance too, and stays
            # applied.
            if status in (PENDING_APPROVAL, APPLIED) and isinstance(change_id, str):
                return cls(
                    status=PENDING_APPROVAL if status == PENDING_APPROVAL else APPLIED,
                    pending_change_id=change_id,
                    workflow_instance_id=instance_id if isinstance(instance_id, str) else None,
                    body=body,
                )
            if isinstance(instance_id, str):
                return cls(
                    status=UNDER_REVIEW,
                    pending_change_id=None,
                    workflow_instance_id=instance_id,
                    body=body,
                )
        return cls(status=APPLIED, pending_change_id=None, workflow_instance_id=None, body=body)


_StartOutcome = Literal["started", "queued", "refused"]


@dataclass(frozen=True)
class WorkflowStartItem:
    """One record of a :meth:`WorkflowsApi.start` call and what became of it."""

    record: str
    """The record's reference as the request wrote it."""
    outcome: _StartOutcome
    """``started``: an instance runs. ``queued``: the workflow is at its cap of running
    instances and the start waits. ``refused``: see :attr:`code`."""
    instance_id: str | None
    """The instance started or, for ``WORKFLOW_ALREADY_RUNNING``, the one already running."""
    position: int | None
    """1 for the next waiting start; set on ``queued`` and ``WORKFLOW_START_ALREADY_WAITING``."""
    code: str | None
    """The stable refusal code, such as ``WORKFLOW_ALREADY_RUNNING`` or ``RECORD_NOT_FOUND``."""
    reason: str | None
    """The refusal in words, for a person."""
    details: Mapping[str, Any] | None
    """The refusal's details."""

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> WorkflowStartItem:
        position = row.get("position")
        details = row.get("details")
        return cls(
            record=str(row.get("record", "")),
            outcome=cast(_StartOutcome, row.get("outcome")),  # as the server sent it
            instance_id=_str_or_none(row.get("instance_id")),
            position=position if isinstance(position, int) else None,
            code=_str_or_none(row.get("code")),
            reason=_str_or_none(row.get("reason")),
            details=details if isinstance(details, Mapping) else None,
        )


@dataclass(frozen=True)
class WorkflowStartResult:
    """What :meth:`WorkflowsApi.start` did: the counts, and one row per record in request
    order. The body is kept as :attr:`body`."""

    total: int
    started: int
    queued: int
    refused: int
    results: tuple[WorkflowStartItem, ...]
    """One row per record, in the order the request listed them."""
    body: Any = field(default=None, compare=False, repr=False)
    """The body as the server sent it."""

    @classmethod
    def of(cls, body: Mapping[str, Any]) -> WorkflowStartResult:
        rows = body.get("results")
        return cls(
            total=_count(body.get("total")),
            started=_count(body.get("started")),
            queued=_count(body.get("queued")),
            refused=_count(body.get("refused")),
            results=tuple(
                WorkflowStartItem.of(row)
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, Mapping)
            ),
            body=body,
        )


_WORKFLOW_HOLD = "workflow"
_WORKFLOW_REJECTED = "workflow-rejected"


@dataclass(frozen=True)
class QuarantineReason:
    """A typed reading of a quarantine row's value-free ``reason_code``, telling a source
    record a workflow holds from one it already rejected.

    ::

        for row in client.sources.quarantine(source, reason_code="workflow:supplier-bank-change"):
            reason = QuarantineReason.of(row)  # reason.held, reason.workflow_key

    ``workflow:<key>``: a source gate holds the row while that workflow decides it.
    ``workflow-rejected:<key>``: an approver already rejected this payload for that workflow.
    Any other code is not workflow-related. Only the code is read, never ``payload`` or
    ``reason``.
    """

    code: str | None
    """The ``reason_code`` as the server sent it."""
    workflow_key: str | None
    """The workflow's key, for a workflow-related code; None otherwise."""
    outcome: Literal["held", "rejected"] | None
    """``held`` for ``workflow:<key>``, ``rejected`` for ``workflow-rejected:<key>``, None for
    any other code."""

    @property
    def held(self) -> bool:
        """True when a workflow holds the row for approval."""
        return self.outcome == "held"

    @property
    def rejected(self) -> bool:
        """True when an approver already rejected this payload for the workflow."""
        return self.outcome == "rejected"

    @property
    def workflow_related(self) -> bool:
        return self.outcome is not None

    @classmethod
    def of(cls, row_or_code: Mapping[str, Any] | str | None) -> QuarantineReason:
        """Read a quarantine row, or its ``reason_code`` alone. Tolerant: anything that is not
        a workflow code reads as not workflow-related."""
        code = row_or_code.get("reason_code") if isinstance(row_or_code, Mapping) else row_or_code
        if not isinstance(code, str):
            return cls(code=None, workflow_key=None, outcome=None)
        # The key is everything after the first colon; a workflow key may hold none itself.
        prefix, colon, key = code.partition(":")
        if colon and key and prefix == _WORKFLOW_HOLD:
            return cls(code=code, workflow_key=key, outcome="held")
        if colon and key and prefix == _WORKFLOW_REJECTED:
            return cls(code=code, workflow_key=key, outcome="rejected")
        return cls(code=code, workflow_key=None, outcome=None)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _filters(**given: str | None) -> dict[str, Any]:
    return {name: value for name, value in given.items() if value is not None}


class PendingChangesApi:
    """The changes a change gate holds, and what became of them (``GET /v1/pending-changes``).

    A session route: on a service-account connection every call is refused here with
    :class:`PermissionError`, before anything is sent.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def list(
        self,
        *,
        model: str | None = None,
        global_id: str | None = None,
        status: str | None = None,
        maker: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every pending change matching the filters, following the cursor to the end.

        ``model`` is a model name, ``global_id`` a golden record's id, ``status`` one of
        ``pending``, ``applied``, ``discarded``, ``stale`` or ``failed`` for a held change, or
        ``under_review``, ``confirmed`` or ``rejected`` for a change a review-mode gate applied
        and reviews (every status when left out), and ``maker`` the principal who made the
        change. A filter left out is not sent.
        """
        self._client._require_session("listing pending changes", _SESSION_REMEDY)
        return all_items(
            self._client,
            "/v1/pending-changes",
            _filters(model=model, global_id=global_id, status=status, maker=maker),
        )

    def get(self, pending_change_id: str) -> dict[str, Any]:
        """One pending change: its ``status``, ``kind``, ``operation``, the ``cells`` it holds,
        what it ``proposed``, and the ``workflow_instance_id`` deciding it."""
        self._client._require_session("reading a pending change", _SESSION_REMEDY)
        change: dict[str, Any] = self._client._request(
            "GET", f"/v1/pending-changes/{quote(pending_change_id, safe='')}"
        )
        return change


class WorkflowsApi:
    """The Environment's workflow definitions (``GET /v1/workflows``), and a manual start on
    records (``POST /v1/workflows/{key}:start``).

    Session routes, refused on a service-account connection like ``client.pending_changes``.
    The refusal here is early only: who may start a workflow is the server's to decide.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def list(self, *, model: str | None = None) -> list[dict[str, Any]]:
        """Every workflow, following the cursor to the end; ``model`` keeps those whose trigger
        names that model."""
        self._client._require_session("listing workflows", _SESSION_REMEDY)
        return all_items(self._client, "/v1/workflows", _filters(model=model))

    def get(self, key: str) -> dict[str, Any]:
        """One workflow by its ``key``: ``name``, ``enabled``, ``revision`` and its
        ``definition`` (trigger and steps)."""
        self._client._require_session("reading a workflow", _SESSION_REMEDY)
        workflow: dict[str, Any] = self._client._request(
            "GET", f"/v1/workflows/{quote(key, safe='')}"
        )
        return workflow

    def start(
        self, key: str, records: Sequence[str], *, note: str | None = None
    ) -> WorkflowStartResult:
        """Start the workflow ``key`` — a ``manual`` one, or an ``on_record`` one with a manual
        start — on each of ``records`` (global ids, 1 to 100), with an optional ``note``.

        Each record is started, queued behind the workflow's cap of running instances, or
        refused on its own row with a stable ``code`` (``WORKFLOW_ALREADY_RUNNING``,
        ``RECORD_NOT_FOUND``, …). The server refuses the whole call ``403`` when this person
        may not start it, ``409`` when the workflow takes no manual start or is paused, and
        ``422`` when the trigger needs a note and none is given.
        """
        self._client._require_session("starting a workflow", _SESSION_REMEDY)
        if isinstance(records, str):
            raise TypeError("records is a list of record ids, not one string")
        body: dict[str, Any] = {"records": list(records)}
        if note is not None:
            body["note"] = note
        answer: dict[str, Any] = self._client._request(
            "POST", f"/v1/workflows/{quote(key, safe='')}:start", json=body
        )
        return WorkflowStartResult.of(answer)
