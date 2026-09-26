# masterly — Python client

Extract governed data products and ingest records from Python — notebooks on Databricks,
Microsoft Fabric, or anywhere else your pipelines run.

```python
from masterly import Client

# A signed-in person: configuration, golden records, ingest.
client = Client(
    base_url="https://app.example.com",       # your Masterly install
    token="<your session token>",
    environment="env_prod_eu",
)

# Ingest: deliver records to a Source, by name or id (chunked automatically)
report = client.sources.ingest("crm", records)
print(report.records, "records in", report.batches, "batches")
```

```python
# A machine running unattended: a service account, pinned to its Environment and confined
# to what it was granted. This is the persona that reads published data products.
job = Client.for_service_account("https://app.example.com", token="<service-account token>")

# Extract: page through a data product (cursor-managed for you)
for row in job.products.read("dp_a1b2c3"):
    ...

# Or straight into a DataFrame
df = job.products.read("dp_a1b2c3").to_pandas()

# A slice, shaped on the server within the product's contract: row filters (ANDed, and
# always inside the account's access policies) and the fields to return
eu = job.products.read(
    "dp_a1b2c3",
    filters=[{"attribute": "country", "op": "equals", "values": ["SE"]}],
    fields=["global_id", "name", "tier"],
)

# Follow the change feed with a resumable cursor
feed = job.products.changes("dp_a1b2c3", cursor=saved_cursor)
for change in feed:
    ...
save(feed.cursor)  # persist for the next run: an opaque token, never parsed

# Ingest, on a schedule: only the Sources this account's `ingest` scope names
job.sources.ingest("src_7f3c9a", records)
```

The change-feed cursor is an opaque token. Store it and pass it back exactly as you received
it; do not parse it, build one, or compare two, because its format is the server's to change.

## Two token personas

Every call carries a bearer token, and Masterly issues two kinds. Which one you hold decides
what you can reach — and, for the machine one, *where*.

|  | Session token | Service-account token |
|---|---|---|
| Belongs to | a signed-in person | a machine: a scheduled job, a notebook that runs unattended |
| Environment | you name it on the connection | pinned to the account; the connection names none |
| Configuration, golden records, listings | yes, as far as your role allows | no — those are session routes |
| Ingest | any Source in the Environment, with the `ingest:run` permission | only the Sources its `ingest` scope names |
| Read a data product's rows (`client.products.read`) | no — the consume path authenticates machines, and a person reads a product in the app instead | yes, shaped by its linked access principal's policies |
| Addressing things | by id **or** by name | by **id** — resolving a name means listing |
| Ends | when the session expires | when an administrator revokes the account |

### A session token

Yours — the same session you hold when you are signed in to the app, issued to you again for
use outside the browser. It is scoped to one Organization, and you tell the client which
Environment you are working in:

```python
client = Client("https://app.example.com", token, environment="env_prod_eu")
client.data_models.publish("Customer")
client.sources.ingest("crm", records)          # needs the `ingest:run` permission
for record in client.golden.list("Customer"):  # the resolved single view
    ...
```

Where it comes from: **issue it from the app you are signed in to.** `POST
/v1/auth/sessions:issue`, called as your signed-in session with no body, answers with a new
session token — once; no later call returns it again. The browser never holds your session
token (the app keeps it on its own server), so the call is made from the app's page, which
attaches your session for you: sign in, switch to the Organization you want the token for,
open your browser's developer tools on any page of the app, and run in the console

```js
await (await fetch("/api/proxy/v1/auth/sessions:issue", { method: "POST" })).json()
```

then copy `session_token` from the answer into your secret store. The route is not in a
published release yet; an install on an earlier release answers it `404 NOT_FOUND`. The
public docs keep the full description under
[get a session token](https://masterlydata.com/docs/reference/python-sdk/#get-a-session-token).

An install running the **dev identity binding** (local development, and demo installs) mints
one from an e-mail address instead, which is what `examples/demo_data.py` does behind
`--dev-login`:

```python
session = httpx.post(
    "http://localhost:8001/v1/auth/sessions", json={"idp_token": "dev:you@example.com"}
).json()
token = session["session_token"]
```

Either way it is a session, not a long-lived credential. It is you — the same roles, checked on
every request, so a deactivation or a removed grant takes effect at once. It expires an hour
after it is issued, and it counts under your Organization's session policy like any other
session: the maximum session length runs from your original sign-in, not from the moment you
issued the token. It is listed among your sessions in the app, where you revoke it like any
other (`GET /v1/auth/sessions/mine` marks it `self_issued`). It does not belong in a scheduled
job — that is what the other persona is for.

### A service-account token

A **service account** is a machine principal created against exactly one Environment. It carries
two things: a link to an *access principal*, which decides which rows and columns of a published
product it may see, and an optional `ingest` **scope** naming exactly the Sources it may push
into. It cannot be pointed anywhere else, so a token that leaks in a job's environment file
cannot load a different Environment or a Source nobody granted it.

```python
from masterly import Client

client = Client.for_service_account("https://app.example.com", token)

client.sources.ingest("src_7f3c9a", records)   # a Source its `ingest` scope names
client.products.read("dp_a1b2c3")              # a product its access principal may consume
```

No Environment id: the account is pinned to its own. Pass `environment="env_prod_eu"` anyway if
the job should *assert* which Environment it believes it is loading — the server refuses the
call rather than loading the other one.

**How to get one.** Someone who can manage service accounts in that Environment (the Integrator
role and above) creates it in the app, under **Access management → Service accounts → New service
account**: name it, pick the **access principal** it consumes as, and, if it will deliver records,
tick **Ingest** and name exactly the Sources it may push into. The credential is shown once, on
creation, together with the Source ids the scope names and the `POST /v1/ingest` endpoint to send
them to. The same screen lists the Environment's accounts and revokes them
(`DELETE /v1/service-accounts/{service_account_id}`).

The equivalent for scripted setup is `POST /v1/service-accounts`:

```python
credential = admin.request("POST", "/v1/service-accounts", json={
    "name": "databricks-nightly-load",
    "linked_principal_id": "prn_9d41f0",       # the access principal it consumes as
    "scopes": [{"kind": "ingest", "source_ids": ["src_7f3c9a"]}],
})
credential["token"]        # shown once — put it straight into your secret manager
```

Leave `scopes` out and the account is consume-only.

What `credential["token"]` holds depends on the install. On the dev identity binding it *is* the
token to present (it looks like `m2m:dev:svc_example`). With a real identity provider the
account is an OAuth2 client there: your job runs the client-credentials grant against the IdP and
presents the access token it issues — Masterly verifies that token and maps the client back to
the account. Either way it goes in the `token` argument above.

**When it is refused.**

| What happened | What comes back |
|---|---|
| A Source the `ingest` scope does not name | `ApiError`, 403 `SERVICE_ACCOUNT_SCOPE_DENIED` |
| An `environment=` the account is not pinned to | `ApiError`, 403 `SERVICE_ACCOUNT_SCOPE_DENIED` |
| The account was revoked, or is unknown | `ApiError`, 401 `SERVICE_ACCOUNT_INVALID` |
| A session route — a listing, configuration, golden records, a Source's counters | `ApiError`, 401 `UNAUTHENTICATED` |
| A Source or product addressed by name | `PermissionError`, before anything is sent |

Scopes are fixed when the account is created. Widening one is not a permission someone grants
after the fact — it is a new account, and the old one is revoked.

## Deletes, full snapshots and record history

A record upserts by its source key. To delete one instead, send its key with `"op": "delete"`
in the same batch as everything else:

```python
client.sources.ingest("crm", [
    {"customer_number": "C-1001", "name": "Acme AB"},   # upsert
    {"op": "delete", "customer_number": "C-0042"},      # delete by source key
])
```

A deleted record is tombstoned, not erased: it leaves its entity, the golden record
recomputes without it, and its history stays readable. A delete for a key the source does not
hold does nothing.

When a system can only hand over everything it has, send it as a **full snapshot**: every live
record of the Source that the snapshot does not carry is deleted after the upserts.

```python
client.sources.ingest("crm", every_customer, mode="full", batch_size=5000)
```

The platform reconciles each call on its own, so a full snapshot travels as one call, and the
client refuses one that does not fit `batch_size` before it sends anything. The install caps
how many records one call may carry (5,000 unless it set its own); over that cap the call is
refused with `INGEST_BATCH_TOO_LARGE`, and nothing is deleted.

Every state a record has been in, newest first, and the undo for a delete (both session routes;
`record_id` is the record's `rec_…` id, not its source key):

```python
for state in client.sources.history("crm", "rec_01J9Z3"):
    print(state["version"], state["valid_from"], state["valid_to"], state["data"])

client.sources.restore("crm", "rec_01J9Z3")   # {"job_id": ...}; needs `record:author`
```

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
- **A create has a precondition of its own.** Some governed operations upsert — `POST /v1/records`
  by business key, a configuration on its first save — and an object nobody has authored has no
  `version` to quote. `if_none_match=True` sends `If-None-Match: *` ("only if it does not exist
  yet"): the write lands when there is nothing there, and is refused with `VERSION_CONFLICT`
  when there is — read the object, then replace it with `if_match`.

  ```python
  client.request("POST", "/v1/records", if_none_match=True,
                 json={"model_name": "Customer", "values": record})
  ```
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
    "erp",                                     # the fixed name: a slug, never changed
    display_name="SAP ERP",                    # the label people read; change it any time
    target_model="Customer",
    source_key="customer_number",              # required: without it every record quarantines
    field_map={"KUNNR": "customer_number", "NAME1": "name"},
)
client.sources.update("erp", display_name="SAP ERP (Sweden)")
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

## Releases

Versions are published to PyPI from a `vX.Y.Z` tag in this repository. How a release is
cut — and which commit each published version was built from — is
[RELEASING.md](RELEASING.md).

## License

Copyright 2026 Masterly. Apache-2.0 — see [LICENSE](LICENSE).
