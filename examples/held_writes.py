#!/usr/bin/env python3
"""Author a record and tell an applied write from one a change gate holds for approval.

Where a customer has configured a change gate on a model, ``POST /v1/records`` may answer
``202`` with ``status: pending_approval`` instead of applying, and a second change to the same
cell is refused ``409 CHANGE_PENDING`` until the first is decided. Where no gate is configured,
the write applies and this script simply says so.

    # from this checkout: uv run examples/held_writes.py …
    # anywhere else:      uv run --with 'masterly>=0.7.0' held_writes.py …
    uv run examples/held_writes.py --base-url http://localhost:8001 --token "$TOKEN" \
        --environment env_prod_eu --model Supplier --values '{"supplier_no": "S1"}'
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from masterly import ChangePending, Client, WriteResult


def author(client: Client, model: str, values: dict[str, Any]) -> str:
    """Write one record and say what became of it: ``applied``, ``held`` or ``waiting``."""
    try:
        body = client.request(
            "POST",
            "/v1/records",
            if_none_match=True,
            json={"model_name": model, "values": values},
        )
    except ChangePending as waiting:
        # A change to one of these cells is already waiting for approval. It names that change,
        # never its values; retry once it is decided.
        print(f"a change to these cells is already waiting: {waiting.pending_change_id}")
        return "waiting"
    result = WriteResult.of(body)
    if not result.held:
        print("applied")
        return "applied"
    # Nothing has applied yet. Keep the ids: the pending change says what was proposed, and
    # the workflow instance decides it.
    assert result.pending_change_id is not None  # a held result always names its change
    change = client.pending_changes.get(result.pending_change_id)
    workflows = [w["key"] for w in client.workflows.list(model=model)]
    print(
        f"held for approval as {result.pending_change_id} "
        f"(instance {result.workflow_instance_id}, change {change['status']}, "
        f"gated by {', '.join(workflows) or 'a workflow'})"
    )
    return "held"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", required=True, help="a session token")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--values", required=True, help="the record's values, as JSON")
    args = parser.parse_args(argv)
    with Client(args.base_url, args.token, args.environment) as client:
        author(client, args.model, json.loads(args.values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
