#!/usr/bin/env python3
"""Deliver a supplier's bank account from an ERP and see whether a workflow holds it.

Where a customer has configured a source gate (a workflow that reviews arriving source records,
such as a supplier's changed IBAN), a delivered record does not land at once: it waits in the
Source's quarantine with ``reason_code`` ``workflow:<key>`` until an approver decides it, and a
payload an approver already rejected is quarantined again with ``workflow-rejected:<key>``.
Where no source gate is configured, both lists stay empty.

The script connects as a service account — the persona a scheduled integration holds — and
prints only counts, quarantine ids and the workflow key: a quarantine row carries the payload
as it arrived and a ``reason`` that names the value, and neither belongs in a log.

    # from this checkout: uv run examples/source_hold.py …
    # anywhere else:      uv run --with 'masterly>=0.9.0' source_hold.py …
    # GB82WEST12345698765432 is the IBAN standard's own example account, not a real one.
    uv run examples/source_hold.py --base-url http://localhost:8001 \
        --token "$MASTERLY_SERVICE_ACCOUNT_TOKEN" --source src_erp --model Supplier \
        --supplier-no S1 --iban GB82WEST12345698765432 --workflow supplier-bank-change
"""

from __future__ import annotations

import argparse

from masterly import Client, QuarantineReason


def deliver(
    client: Client,
    source: str,
    record: dict[str, str],
    *,
    workflow: str,
    model: str | None = None,
) -> dict[str, list[str]]:
    """Deliver ``record``, then list the Source's open quarantine rows the workflow holds or
    already rejected. Returns the quarantine ids under ``waiting`` and ``rejected``."""
    report = client.sources.ingest(source, [record], model=model)
    print(f"delivered {report.records} record(s): job {report.receipts[0].job_id}")
    # Ingest is asynchronous: a record delivered just now may reach the quarantine a moment
    # later, so a scheduled run reads what earlier runs left waiting as well.
    found: dict[str, list[str]] = {"waiting": [], "rejected": []}
    for code in (f"workflow:{workflow}", f"workflow-rejected:{workflow}"):
        for row in client.sources.quarantine(source, status="open", reason_code=code):
            reason = QuarantineReason.of(row)  # reads reason_code only, never payload or reason
            if reason.held:
                found["waiting"].append(row["quarantine_id"])
            elif reason.rejected:
                found["rejected"].append(row["quarantine_id"])
    print(f"waiting for approval ({workflow}): {', '.join(found['waiting']) or 'none'}")
    print(f"previously rejected ({workflow}): {', '.join(found['rejected']) or 'none'}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", required=True, help="a service-account token")
    parser.add_argument("--source", required=True, help="the source id (src_…) to deliver to")
    parser.add_argument("--model", help="the Source's target, when it has several")
    parser.add_argument("--supplier-no", required=True)
    parser.add_argument("--iban", required=True)
    parser.add_argument("--workflow", default="supplier-bank-change", help="the gate's key")
    args = parser.parse_args(argv)
    with Client.for_service_account(args.base_url, args.token) as client:
        deliver(
            client,
            args.source,
            {"supplier_no": args.supplier_no, "iban": args.iban},
            workflow=args.workflow,
            model=args.model,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
