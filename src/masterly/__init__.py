"""Masterly Python client — extract governed data products, ingest records, build the
configuration both run on.

A thin, typed wrapper over the stable ``/v1`` REST contract (the same API the GUI, MCP,
and stream channels ride). Built for notebook and pipeline use on Databricks, Microsoft
Fabric, and plain Python: sync calls, cursor paging handled for you, optional pandas.

``client.workspaces``, ``client.domains``, ``client.data_models`` and ``client.sources``
cover enough configuration to stand an Environment up from a script, and ``client.config``
previews and applies the whole of it at once — a pull from Git, an imported file set, a
promotion from another Environment; ``client.products`` and ``client.golden`` read out of it.
``client.jobs`` follows the asynchronous work an ingest batch or a full load starts.
Where a change gate holds a write for approval, :meth:`WriteResult.of` says so — or says
``under_review`` where a review-mode gate applied it and a workflow may still reject it — and
``client.pending_changes`` and ``client.workflows`` read what it is waiting on.
``client.workflows.start`` starts a manual-start workflow on records, and
:meth:`QuarantineReason.of` tells a source record a workflow holds in quarantine
(``workflow:<key>``) from one it already rejected (``workflow-rejected:<key>``).

Two token personas connect. :class:`Client` is a signed-in person's session, scoped to the
Environment it names. :meth:`Client.for_service_account` is a machine: pinned to its own
Environment, confined to the Sources its ``ingest`` scope names and the products its access
principal may consume — the persona a scheduled load should hold. The README's "Two token
personas" says how to get either one.
"""

from masterly._client import ApiError, ChangePending, Client, Persona
from masterly._extract import ChangeFeed, RowPages
from masterly._ingest import FullLoad, IngestMode, IngestReceipt, IngestReport, SourceTarget
from masterly._pending import (
    QuarantineReason,
    WorkflowStartItem,
    WorkflowStartResult,
    WriteResult,
)
from masterly._precondition import Conflict, Precondition

__version__ = "0.9.0"

__all__ = [
    "ApiError",
    "ChangeFeed",
    "ChangePending",
    "Client",
    "Conflict",
    "FullLoad",
    "IngestMode",
    "IngestReceipt",
    "IngestReport",
    "Persona",
    "Precondition",
    "QuarantineReason",
    "RowPages",
    "SourceTarget",
    "WorkflowStartItem",
    "WorkflowStartResult",
    "WriteResult",
    "__version__",
]
