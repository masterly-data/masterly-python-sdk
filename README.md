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
print(report.records, "records in", report.batches, "batches:", report.job_ids)
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
| A session route — a listing, configuration, golden records, a Source's counters, a job | `ApiError`, 401 `UNAUTHENTICATED` |
| A Source or product addressed by name | `PermissionError`, before anything is sent |

Scopes are fixed when the account is created. Widening one is not a permission someone grants
after the fact — it is a new account, and the old one is revoked.

## Deletes, full snapshots, receipts and record history

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
record of the target that the snapshot does not carry is deleted after the upserts (a source
that [feeds several models](#a-source-that-feeds-several-models) names the target with
`model=`; its other targets are untouched).

```python
client.sources.ingest("crm", every_customer, mode="full", batch_size=5000)
```

The platform reconciles each call on its own, so `ingest(mode="full")` sends the snapshot as
one call, and refuses one that does not fit `batch_size` before it sends anything. The install
caps how many records one call may carry (5,000 unless it set its own); over that cap the call
is refused with `INGEST_BATCH_TOO_LARGE`, and nothing is deleted.

A snapshot larger than one call goes as a **full load**: one load on the Source, any number of
batches in it, and the deletes happen once, when the load completes, over everything its
batches carried. Send it all at once or a piece at a time, as your pipeline produces it:

```python
with client.sources.full_load("crm", batch_size=5000) as load:
    for frame in snapshot_frames:
        load.send(frame)                     # chunked into batches of batch_size

done = client.sources.wait_for_load("crm", load.load_id)
print(done["status"], done["reconciled"], "records deleted")
```

Leaving the block normally completes the load. If anything raises inside it — a refused
batch, a dropped connection, your own code — the load is **abandoned** and the error is raised:
a snapshot that did not arrive whole never reconciles, so nothing is deleted, and what the sent
batches upserted stays. A Source takes one load at a time; opening a second while one is open
raises `ApiError` `INGEST_LOAD_IN_FLIGHT`, with the open load's id in `details`.

### Following a batch to its outcome

Ingest is accepted asynchronously, and every batch is answered with the job that will apply it.
The report keeps each batch's receipt, in the order the batches were sent:

```python
report = client.sources.ingest("crm", records, batch_size=5000)
for receipt in report.receipts:
    print(receipt.job_id, receipt.records)

job = client.jobs.wait(report.job_ids[-1], timeout=300)   # returns on succeeded or failed
if job["status"] == "failed":
    print(job["error"]["code"], job["error"]["message"])
```

`client.jobs.get(job_id)` reads a job once; `client.jobs.wait` reads it until it has
`succeeded` or `failed` and raises `TimeoutError` if it has not within `timeout` seconds. Reading
a job is a session route today: on a service-account connection the server refuses it, and the
refusal is raised as `ApiError`.

Every batch, and the opening of a full load, is sent with an `Idempotency-Key`. When the
connection fails before an answer arrives, the client re-sends it with the same key, and the
platform replays its first answer instead of applying the batch twice or opening a second load.

### Record history

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

### Applying a whole configuration states the revision its preview read

Three operations move an Environment's *entire* promotable configuration at once — pulling it
from the bound Git repository, importing a file set, and promoting it from another Environment —
and each is two calls: a **preview** (the default, which writes nothing) and an **apply**.
`client.config` wraps them, and the apply is a governed write like any other: the preview's
`version` is one revision for everything the preview was computed from — the Environment's whole
configuration *and* what the apply would write (the other Environment's configuration, the commit
the ref resolved to, the files you submitted). Pass it as `if_match`; if any of it moved in
between, the apply is refused and nothing is written. The recovery is a new preview.

```python
preview = client.config.pull()                              # dry run: the diff, no writes
for area, diff in preview["diff"].items():
    print(area, "created", diff["created"], "updated", diff["updated"])
client.config.pull(if_match=preview["version"])             # applies exactly what you reviewed

preview = client.config.import_files({"workspaces/sales.yaml": "name: Sales\n"})
client.config.import_files({"workspaces/sales.yaml": "name: Sales\n"}, if_match=preview["version"])

preview = client.config.promote("env_prod_eu", source="env_stage_eu")
client.config.promote("env_prod_eu", source="env_stage_eu", if_match=preview["version"])
```

`if_match` is required on an apply — a bundle applied without one may land over changes nobody
previewed. A job that means to apply whatever is there says so with
`Precondition.unconditional()`, which the audit trail records as an unconditional write. A
refused apply raises `ApiError` whose `conflict` names the configuration objects that moved
(`changed_fields`, as `<type>/<id>`), never their values. These three take `if_match` from a
release that is not published yet; an install on an earlier release ignores the header and
answers the preview without a `version`.

## Errors

The client is a thin, typed wrapper over the stable `/v1` REST contract — the same API
every other channel uses. Errors raise `masterly.ApiError` carrying the server's error
code, message, and `details` — the envelope's own payload, passed through as sent, which is
where a refusal explains itself. Re-delivery is idempotent: records upsert by their source key,
so running the same notebook twice never duplicates data.

`pandas` is an optional dependency: `pip install masterly[pandas]`.

## Configuration

Extract and ingest run on a configuration someone built. These build it — enough to stand
an Environment up from a script (and `client.config`, above, previews and applies the whole of
it at once):

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

source = client.sources.get("erp")
client.sources.update("erp", display_name="SAP ERP (Sweden)", if_match=source["version"])
```

Everything takes an id or an exact name, so a script reads the way the domain is discussed.
A model's `definition` carries its attributes, keys and constraints — ingest validates every
record against them, so widening or tightening one changes what the pipeline accepts.

### A source that feeds several models

A source feeds one or more models — its **targets** — and each target has its own source
key and field map. A CRM export that carries customers and their addresses is one source
with two targets, registered with `targets=` instead of `target_model=`:

```python
from masterly import SourceTarget

client.sources.create(
    "crm",
    display_name="Salesforce CRM",
    targets=[
        SourceTarget("Customer", source_key="customer_number"),
        SourceTarget("Address", source_key="address_id", field_map={"ADDR_ID": "address_id"}),
    ],
)
```

A batch lands in **one** target, so a push to such a source names it with `model=` — one call
per model. On a source with one target `model` may be left out, and the call sends exactly
what it always sent; on a source with several, an unnamed batch is refused with
`SOURCE_TARGET_REQUIRED` before anything is queued, `error.details["targets"]` listing them:

```python
client.sources.ingest("crm", customers, model="Customer")
client.sources.ingest("crm", addresses, model="Address")
client.sources.upload_csv("crm", open("addresses.csv").read(), model="Address", delimiter=";")

with client.sources.full_load("crm", model="Address") as load:   # one target's snapshot
    load.send(every_address)
```

Records, quarantine and counts are per target too — a source key names one record within a
target, so the same key under two targets is two records:

```python
for row in client.sources.quarantine("crm", model="Address", status="open"):
    print(row["model_name"], row["reason_code"], row["payload"])

stats = client.sources.stats("crm")           # the whole source, plus `targets[]` per model
stats = client.sources.stats("crm", model="Address")
```

`client.sources.targets` lists, adds, reads, edits and removes a source's targets. A target
has a revision of its own, stated on its governed writes; adding one needs a model with a
published version, and only a target that holds no records — deleted ones included — can be
removed, never the last one:

```python
client.sources.targets.add("crm", "Address", source_key="address_id")
target = client.sources.targets.get("crm", "Address")
target["mapping"]["field_map"]["STREET"] = "street"
client.sources.targets.update("crm", "Address", mapping=target["mapping"], if_match=target["version"])
client.sources.targets.remove("crm", "Address", if_match=target["version"])   # only while empty
```

On what `client.sources.get` returns, `targets[]` carries every target; the top-level
`target_model`, `mapping`, `profile` and `drift` are **deprecated** and describe the first
target — on a one-target source, exactly what they always did. The same goes for `target_model=`
and `mapping=` on `client.sources.update`, which write the first target; edit any target through
`client.sources.targets.update`. These calls talk to routes that are not in a published release
of the API yet; an install on an earlier release answers them `404 NOT_FOUND`, and sends every
batch to its one target.

Editing a model or a source is a **governed write**: more than one person edits them, and the
edit is derived from a read, so `update` states the revision it replaces (ADR 0070) and a stale
one is refused rather than silently overwriting:

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

### Deactivate, reactivate or delete a source

A source has a `status`: `active`, or `inactive` once deactivated. An **inactive** source admits
nothing — a pushed batch, a CSV upload, a full load's next batch and a quarantine retry are all
refused with `409 SOURCE_INACTIVE` and nothing is queued. Everything it already landed stays,
and **its records keep contributing to golden records**: nothing is recomputed, in either
direction. Reactivating admits records again with nothing lost. Both are governed writes, so
they take the source's `version` as `if_match`:

```python
source = client.sources.get("erp")
client.sources.deactivate("erp", if_match=source["version"])   # status: "inactive"

source = client.sources.get("erp")
client.sources.reactivate("erp", if_match=source["version"])   # status: "active"
```

A **delete** removes the source and everything it landed — its records in every target and
their history, quarantine, profile, drift, run history, schedule and sealed connection — and
recomputes the golden records it contributed to without it; an entity left with no live
record clears. It is heavy work, so it runs as a job: the source is marked `deleting` at once,
the answer is the job receipt, and `client.jobs.wait` follows the removal to its end. Read
what it would touch first, and ask a person to type the source's `name` before you send it:

```python
impact = client.sources.deletion_impact("erp")
print(impact["targets"], impact["golden"]["would_change"], impact["golden"]["would_clear"])
if impact["deletable"] and typed_name == impact["name"]:
    source = client.sources.get("erp")
    receipt = client.sources.delete("erp", if_match=source["version"])
    client.jobs.wait(receipt["job_id"])
```

The delete is refused with `409 SOURCE_IN_USE` while a data product reads the source through a
raw relation — `error.details["dependents"]` names each one; change or delete the product
first — and the model's built-in manual source can be neither deactivated nor deleted
(`409 SOURCE_BUILT_IN`). The four calls are session routes. They talk to routes that are not
in a published release yet; an install on an earlier release answers them `404 NOT_FOUND`.

## Command line: `masterly config migrate`

The configuration files in a GitOps repository follow a schema that Masterly versions. A
change within a version is additive — an old repository keeps validating. A removal or a
rename is announced first: the linter (`config:lint`, which the in-product editor runs as you
edit) warns on the old shape, naming what replaces it and the date after which it stops
validating, and `masterly config migrate` makes the edit, as a diff you review and commit
rather than one you author.

```bash
pip install 'masterly[cli]'            # the command and the YAML round-trip parser it needs

cd my-masterly-config                  # the repository root
masterly config migrate                # rewrite to the newest schema version; print the diff
masterly config migrate --dry-run      # print the diff, write nothing
masterly config migrate --check        # exit 1 when a migration is pending — for CI
```

The repository's schema version is declared in `masterly.yaml` at its root
(`schema_version: 1`); a repository from before that file existed is on version 1, and the
first run adds the file. Only the files a migration step changes are rewritten, keeping their
comments, key order, quoting and indentation; what a step cannot do mechanically — a value
that could belong in more than one place — is listed under the diff for you to place by hand.
Nothing is committed: review the diff, then commit it. The warning's trailing id, such as
`(data_models.attribute.historized)`, names the migration step that performs the change.

## Examples

[`examples/demo_data.py`](examples/README.md) generates realistic demo master data —
customers, suppliers and products as three source systems would actually deliver them,
duplicates and defects included, linked to a Country code list, to each other and to an
embedded contact — and ingests it into an Environment.

## Releases

Versions are published to PyPI from a `vX.Y.Z` tag in this repository. How a release is
cut — and which commit each published version was built from — is
[RELEASING.md](RELEASING.md).

## License

Copyright 2026 Masterly. Apache-2.0 — see [LICENSE](LICENSE).
