# Demo data generator

[`demo_data.py`](demo_data.py) fills a Masterly Environment with realistic master data —
customers, suppliers and products as they would actually arrive from a CRM, an ERP and a
webshop. Use it to demo the product, to have something to click through while building a
feature, or to load-test an install.

```bash
# bootstrap + generate + ingest against a local install on the dev identity binding
uv run examples/demo_data.py \
    --base-url http://localhost:8001 \
    --dev-login you@example.com \
    --environment env_prod_eu \
    --wait
```

From this checkout, `uv run` picks up the project's own `masterly`. Anywhere else, bring the
client along: `uv run --with 'masterly>=0.2.0' demo_data.py …`, or `pip install masterly`
and `python demo_data.py …`. It needs **0.2.0 or later** — that is the release that carries
the configuration surface it bootstraps with.

The script itself is thin: `client.workspaces`, `client.domains`, `client.data_models` and
`client.sources` build the configuration, `client.sources.ingest` delivers the records. The
only raw HTTP left in it is the pair of calls that happen before a connection exists —
minting a token and asking which Environments you may use.

## What it creates

Against an empty Environment the script is self-sufficient: it creates a Workspace, a
Domain, three published Data Models, and one Source per originating system. Every step is
find-or-create, so running it twice changes nothing about the configuration.

Find-or-create matches on name, and a name is not proof of identity: an Environment can
already hold someone else's `Customer`. So before delivering into a model or source it
found rather than created, the script checks that it is the one it defines — same
attributes, same target model, same natural key — and stops with an explanation instead of
ingesting a batch that would quarantine wholesale. Use a clean Environment, remove the
stale object, or generate for the existing model on its own terms with `--model`.

| Data Model | Sources | Field names on the wire |
|---|---|---|
| Customer | `crm`, `erp`, `webshop` | Salesforce-style, SAP-style, and passthrough |
| Supplier | `erp-suppliers`, `procurement-portal` | SAP-style and portal-style |
| Product | `pim`, `erp-products` | PIM-style and SAP-style |

Each Source carries the field map that conforms its own vocabulary to the model
(`Account_Name`, `NAME1` and `name` all land in `name`), and declares its natural key.
`webshop` is deliberately left as passthrough, so both mapping styles are represented.

## What makes the data worth ingesting

The population is generated once, then delivered *through* each system — so the same
company arrives from the CRM, the ERP and the webshop under three different customer
numbers, formatted the way that system formats things:

- **Legal-form drift** — `Nordwind Logistik AB`, `NORDWIND LOGISTIK AB`, `Nordwind
  Logistik Aktiebolag`, `Nordwind Logistik`
- **Address drift** — `Storgatan 12` vs `Storg. 12`, `111 22` vs `11122`
- **Phone formats** — `+46 8 123 45 67`, `08123 45 67`, `4681234567`
- **Keying slips** — transposed, dropped and doubled characters; trailing whitespace from
  a spreadsheet paste
- **Coverage gaps** — the ERP holds no website or employee count, the webshop holds no
  registration number, and optional fields go missing at random
- **Intra-source duplicates** — the same company entered twice in one system under two
  keys, never byte-identical (`--duplicate-rate`)
- **Structural defects** — malformed e-mail, country spelled out, unknown enum value,
  empty required field, negative amount, a number delivered as text, and a record with no
  natural key at all (`--defect-rate`)

The defects are the ones the platform actually rejects, so they land in the source's
quarantine with a real reason rather than passing through unnoticed. `tests/test_demo_data.py`
holds the generator to that: clean records must satisfy the model's own constraints, and
every defect must break one.

Everything is seeded — the same `--seed` produces the same records, and re-delivery
upserts by source key, so running twice never doubles the mastered data. (Quarantine rows
do accumulate: each delivery of a bad record is its own quarantine entry.)

## Common runs

```bash
# look at the records without an install
uv run examples/demo_data.py --dry-run --customers 5

# write them to a file instead
uv run examples/demo_data.py --dry-run --out /tmp/records.json

# a bigger population, customers only
uv run examples/demo_data.py --base-url … --dev-login … --models customer --customers 5000

# messier data, for the stewardship queues
uv run examples/demo_data.py --base-url … --dev-login … --defect-rate 0.2 --duplicate-rate 0.3

# configuration already exists — just deliver more records
uv run examples/demo_data.py --base-url … --token "$MASTERLY_TOKEN" --no-bootstrap

# ingest into a model of your own, generated from its live definition
uv run examples/demo_data.py --base-url … --token "$MASTERLY_TOKEN" \
    --model "Customer 360" --source crm --records 500
```

`--model` reads the model's definition from the install and invents values that fit it —
types, enum values, ranges, and a guess from each attribute's name (`*_email` gets an
e-mail, `*_city` a city, `*_date` a date). The natural key comes from the Source's own
`source_key`. Where an attribute has a format constraint the generator cannot satisfy, it
says so and leaves the attribute empty rather than quarantining every record.

## Authenticating

- `--dev-login EMAIL` mints a session on installs running the **dev identity binding**
  (local development, the demo install). It sends the credential as `dev:<email>`.
- `--token` (or `MASTERLY_TOKEN`) takes a session token from any install — use this on
  Stytch or OIDC bindings. `--base-url` and `--environment` also read
  `MASTERLY_BASE_URL` / `MASTERLY_ENVIRONMENT`.

Bootstrapping needs `workspace:create`, `domain:create`, `data-model:create`,
`data-model:update`, `source:create` and `ingest:run` in the target Environment — the
**Modeler** preset role covers all six. With `--no-bootstrap` or `--model`, an
**Integrator** is enough.

## After the run

`--wait` polls until the pipeline stops moving and prints where the records landed:

```
source                  accepted  quarantined
---------------------------------------------
crm                           36            4
erp                           26            8
webshop                       14            2
...

model                   source records  golden entities
-------------------------------------------------------
Customer                            76               76
```

One golden entity per source record means **no match rules are configured yet** — the
duplicates this script generates on purpose are all still sitting there unlinked. Set up
matching for a model — `PUT /v1/match/config/{model_name}`, or the matching editor in the
app — and saving the config re-resolves the model, dropping the entity count below the
source-record count. That is the demo worth showing.

Ingest is asynchronous. Without `--wait` the script returns as soon as the batches are
accepted, and the records appear a few seconds later.
