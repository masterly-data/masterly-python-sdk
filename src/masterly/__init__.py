"""Masterly Python client — extract governed data products, ingest records, build the
configuration both run on.

A thin, typed wrapper over the stable ``/v1`` REST contract (the same API the GUI, MCP,
and stream channels ride). Built for notebook and pipeline use on Databricks, Microsoft
Fabric, and plain Python: sync calls, cursor paging handled for you, optional pandas.

``client.workspaces``, ``client.domains``, ``client.data_models`` and ``client.sources``
cover enough configuration to stand an Environment up from a script; ``client.products``
and ``client.golden`` read out of it.

Two token personas connect. :class:`Client` is a signed-in person's session, scoped to the
Environment it names. :meth:`Client.for_service_account` is a machine: pinned to its own
Environment, confined to the Sources its ``ingest`` scope names and the products its access
principal may consume — the persona a scheduled load should hold. The README's "Two token
personas" says how to get either one.
"""

from masterly._client import ApiError, Client, Persona
from masterly._extract import ChangeFeed, RowPages
from masterly._ingest import IngestReport
from masterly._precondition import Conflict, Precondition

__version__ = "0.3.0"

__all__ = [
    "ApiError",
    "ChangeFeed",
    "Client",
    "Conflict",
    "IngestReport",
    "Persona",
    "Precondition",
    "RowPages",
    "__version__",
]
