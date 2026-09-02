# masterly — Python client

Extract governed data products and ingest records from Python — notebooks on Databricks,
Microsoft Fabric, or anywhere else your pipelines run.

```python
from masterly import Client

client = Client(
    base_url="https://app.example.com",       # your Masterly install
    token="<session or service-account token>",
    environment="env_prod_eu",
)

# Extract: page through a data product (cursor-managed for you)
for row in client.products.read("Customer 360"):
    ...

# Or straight into a DataFrame
df = client.products.read("Customer 360").to_pandas()

# Follow the change feed with a resumable cursor
feed = client.products.changes("Customer 360", cursor=saved_cursor)
for change in feed:
    ...
save(feed.cursor)  # persist for the next run

# Ingest: deliver records to a source (chunked automatically)
report = client.sources.ingest("crm", records)
print(report.records, "records in", report.batches, "batches")
```

## Tokens: two personas

- **Consumer** (extract in a Databricks/Fabric job): use a **service-account token**
  (minted under Users → Service accounts). It pins its Environment and consumes published
  products — reference them **by product id** (`dp_…`), since listing products needs a
  session. Access policies (row rules, masks) apply per consumer automatically.
- **Integrator** (ingest + golden reads): use a **session token**. Sources and products can
  be referenced by name.

## Governed writes: state the revision you are replacing

Extract and ingest are ungoverned — a read is not a write, and an ingest batch is not derived
from a read of what it lands in. A **governed** write is one where more than one principal may
write the object and your request was composed from an earlier read of it: a rule set, an access
policy, a shared view, a record you edited by hand. Those writes state which revision they
replace, and the server refuses rather than letting the other editor's work disappear.

Read, edit, write — the version comes back with the object:

```python
from masterly import ApiError

policy = client.request("GET", "/v1/access-policies/pol_1")
policy["rules"] = edited

try:
    client.request(
        "PUT", "/v1/access-policies/pol_1", json=policy, if_match=policy["version"],
    )
except ApiError as error:
    conflict = error.conflict
    if conflict is None:
        raise
    print(f"{conflict.changed_by} changed {', '.join(conflict.changed_fields)}")
    if conflict.undisclosed_changes:
        print(f"and {conflict.undisclosed_changes} further changes you cannot see")
    if not conflict.may_auto_merge:
        ...  # re-read and decide by hand; the change set cannot be trusted as complete
```

- `if_match` takes the object's `version` field, or a `Precondition` — `Precondition.from_etag`
  if you kept the response header instead, `Precondition.unconditional()` for `If-Match: *`
  ("it must exist; I do not care which revision"), which is recorded in the audit trail as an
  unconditional write.
- **A conflict names what moved, never what it moved to.** There is no "theirs" column in the
  refusal, by design: it would be a read you may not be entitled to, arriving by the error path.
  Re-read the object to compare — that runs the ordinary authorization and access-policy path.
- **Gate any automatic merge on `conflict.may_auto_merge`, never on a zero
  `undisclosed_changes`.** A zero count means nothing was withheld from *you*; it is also what
  you get when the writer recorded no field list at all — unknown, not empty.
- Omitting the precondition still works today and answers with a `Deprecation` header. Governed
  operations move to `428 PRECONDITION_REQUIRED` as their grace ends, and unconditionally at
  `/v2`.

`client.request(...)` is the escape hatch for endpoints the typed surface has not reached, on
the same connection: auth, Environment, timeouts, and error mapping. It also takes `headers=`
for anything per-request, such as an `Idempotency-Key`.

## Errors

The client is a thin, typed wrapper over the stable `/v1` REST contract — the same API
every other channel uses. Errors raise `masterly.ApiError` carrying the server's error
code, message, and `details` — the envelope's own payload, passed through as sent, which is
where a refusal explains itself. Re-delivery is idempotent: records upsert by their source key,
so running the same notebook twice never duplicates data.

`pandas` is an optional dependency: `pip install masterly[pandas]`.

## Configuration

Extract and ingest run on a configuration someone built. These build it — enough to stand
an Environment up from a script:

```python
client.workspaces.create("Demo")
client.domains.create("Sales", workspace="Demo")
client.data_models.create("Customer", domain="Sales", definition={...})
client.data_models.publish("Customer")

client.sources.create(
    "erp",
    target_model="Customer",
    source_key="customer_number",              # required: without it every record quarantines
    field_map={"KUNNR": "customer_number", "NAME1": "name"},
)
```

Everything takes an id or an exact name, so a script reads the way the domain is discussed.
A model's `definition` carries its attributes, keys and constraints — ingest validates every
record against them, so widening or tightening one changes what the pipeline accepts.

Editing a model is a **governed write**: more than one person edits models, and the edit is
derived from a read, so `update` states the revision it replaces (ADR 0070) and a stale one
is refused rather than silently overwriting:

```python
model = client.data_models.get("Customer")
model["definition"]["attributes"].append({"name": "segment", "type": "string"})
client.data_models.update(
    "Customer", definition=model["definition"], if_match=model["version"]
)
```

A refused write raises `ApiError`; `error.conflict` names the fields that moved. Anything
without a typed method is reached through `client.request(...)`, which takes the same
`if_match`.

## Examples

[`examples/demo_data.py`](examples/README.md) generates realistic demo master data —
customers, suppliers and products as three source systems would actually deliver them,
duplicates and defects included — and ingests it into an Environment.

## License

Copyright 2026 Masterly. Apache-2.0 — see [LICENSE](LICENSE).
