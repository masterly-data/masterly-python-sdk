"""Held writes: a change a workflow's gate holds for approval, and the reads that follow it.

Where a customer has configured a change gate, a governed write — ``POST /v1/records``, a pin,
an unpin — may not apply when it is sent. The server stages it as a *pending change*, answers
``202`` with ``status: pending_approval``, and a workflow instance decides it; nothing
downstream sees the change until it applies. A second change to the same cell meanwhile is
refused ``409 CHANGE_PENDING`` (raised as :class:`~masterly.ChangePending`). Where no gate is
configured, none of this happens and the write answers as it always has.

``client.request`` returns the body either way, unchanged. :meth:`WriteResult.of` is the
opt-in typed view over it, and ``client.pending_changes`` and ``client.workflows`` read what a
held write points at.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import quote

from masterly._paging import all_items

if TYPE_CHECKING:
    from masterly._client import Client

#: The 409 code a write gets when a change to one of its cells is already waiting.
CHANGE_PENDING = "CHANGE_PENDING"
PENDING_APPROVAL: Final = "pending_approval"
APPLIED: Final = "applied"

_SESSION_REMEDY = (
    "Pending changes and workflows are read by a person: connect with a session token "
    "(`Client(base_url, token, environment)`)."
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

    A held result occurs only where a customer has configured a change gate; everywhere else
    every write reads as applied.
    """

    status: Literal["applied", "pending_approval"]
    """``pending_approval``: held, nothing applied. ``applied``: everything else."""
    pending_change_id: str | None
    """The change a gate staged — read it with ``client.pending_changes.get``. None when no
    gate was involved."""
    workflow_instance_id: str | None
    """The workflow instance deciding the change. None when no gate was involved."""
    body: Any = field(default=None, compare=False, repr=False)
    """The body as the server sent it."""

    @property
    def held(self) -> bool:
        """True when the write is waiting for approval and nothing of it has applied."""
        return self.status == PENDING_APPROVAL

    @classmethod
    def of(cls, body: Any) -> WriteResult:
        """Read a write's answer. Tolerant by intent: any body that is not a gate's
        ``HeldChange`` — a created record, a pin's recompute receipt, None for a 204 — is an
        applied write with no pending change.

        A ``HeldChange`` with ``status: applied`` is a gate whose instance finished at once:
        the change applied, and the ids still name the change and the instance that applied
        it. A curation route's own staged op (``status: pending``) is not a gate's hold and
        reads as applied here; its body says what it is.
        """
        if isinstance(body, Mapping):
            status = body.get("status")
            change_id = body.get("pending_change_id")
            if status in (PENDING_APPROVAL, APPLIED) and isinstance(change_id, str):
                instance_id = body.get("workflow_instance_id")
                return cls(
                    status=PENDING_APPROVAL if status == PENDING_APPROVAL else APPLIED,
                    pending_change_id=change_id,
                    workflow_instance_id=instance_id if isinstance(instance_id, str) else None,
                    body=body,
                )
        return cls(status=APPLIED, pending_change_id=None, workflow_instance_id=None, body=body)


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
        ``pending``, ``applied``, ``discarded``, ``stale`` or ``failed``, and ``maker`` the
        principal who made the change. A filter left out is not sent.
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
    """The Environment's workflow definitions, read-only (``GET /v1/workflows``).

    A session route, refused on a service-account connection like ``client.pending_changes``.
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
