# Agent guidance for Masterly (project-wide)

This file is the **shared context for every Claude session in any Masterly repo**. Claude Code loads it automatically when working inside `/Users/clarsson/projects/masterly/` or any subdirectory.

Per-repo `CLAUDE.md` files (in each repo root) add repo-specific guidance on top of this one. Architectural source of truth is `masterly-framework/docs/`.

## What Masterly is

Masterly is a modern Master Data Management (MDM) SaaS for organizations replacing Excel or no-tool today. Target customers run Databricks, Snowflake, or Microsoft Fabric. Masterly is an **active processing pipeline** (REST/Stream/MCP ingest → Identity Resolution → Golden Resolution → Data Quality → REST/Stream/MCP extract), not just a catalog or metadata layer.

For the full picture: [`masterly-framework/docs/product/overview.md`](masterly-framework/docs/product/overview.md).

## Hard architectural constraints

These are non-negotiable. Every design must satisfy all of them:

1. **Two-axis deployment factoring** — compute tier (`shared` | `dedicated` | `self-hosted`) × data tier (`masterly-db` | `byo-db`) yields 5 valid combinations. Self-hosted is the initial GTM. The legacy term "BYOC" is retired; use "BYO-DB" on the relevant compute tier. See [`masterly-framework/docs/architecture/deployment-models.md`](masterly-framework/docs/architecture/deployment-models.md) and [ADR 0002](masterly-framework/docs/adr/0002-deployment-topology.md).
2. **Region pinning per Environment** — EU and US at minimum. No cross-region data leakage in logs, telemetry, or backups.
3. **DBU instrumentation on every tier; billing only on usage tiers** — every meaningful operation emits a DBU event to the Environment's local `dbu_events` ledger, on all five combinations. `billing_mode` never gates emission ([ADR 0034](masterly-framework/docs/adr/0034-dbu-event-taxonomy-weights-and-usage-reporting.md)). What is gated is *export and invoicing*: only `billing_mode == "usage"` (Shared MT, Dedicated) ships aggregates to Masterly's metering store, via the telemetry receiver. Self-hosted is fixed-price — local usage visibility, no billing pipeline.
4. **Stateless services** — no in-memory session state. Postgres for durable data-plane state, Cosmos NoSQL for control-plane state, Redis for cache/ephemeral.
5. **Local-LLM support** — self-hosted and BYO-DB customers will not ship master data to OpenAI/Anthropic. The AI Router/Gateway abstracts the LLM.
6. **Configuration is managed, not ad-hoc** — customer config (data models, rules, mappings) is versioned, JSON-Schema-validated, and promotable across Environments. Two modes per Environment ([ADR 0026](masterly-framework/docs/adr/0026-config-management-modes.md)): **Managed (Click-Ops, default)** — stored and versioned in the product — or **GitOps** — config-as-code in the customer's repo. GitOps is optional.
7. **Data never lives in configuration** — master data, golden records, and IR decisions stay in Postgres, never in Git or a config snapshot.

## Canonical hierarchy and terminology

```
Organization (paying account, = Stytch Organization)
└── Environment (e.g. prd-eu, prd-us, non-prd-eu — region pinning lives here)
    └── Workspace
        └── Domain
            └── Data Model
                └── Data Product
```

**Use "Environment," not "Tenant."** Earlier brand materials used "Tenant" for what is now called Environment; that term is deprecated.

**Unit of data isolation = Environment.** The data plane is provisioned per-Environment (DB-per-Environment), not per-Organization.

**Deployment grouping = Install** ([ADR 0039](masterly-framework/docs/adr/0039-installs-as-environment-deployment-grouping.md)). An **Install** is one deployment of the stack (one compute unit + control plane, one `terraform apply`) that hosts a set of an Org's Environments: `Organization 1—N Install 1—N Environment`. It is a **deployment-axis** concept, **not** a level in the logical hierarchy above — navigation stays Organization → Environment → Workspace, and the user's Environment switcher is Org-scoped and Install-invisible. It is what lets a customer separate *infrastructure* (e.g. prod vs non-prod in different subscriptions, on different versions). First-class on the `self-hosted` and `dedicated` tiers; degenerate on `shared`.

## Repo map

All repos live under `github.com/masterly-data/`. Local clones expected under `/Users/clarsson/projects/masterly/`.

| Repo | Purpose | Layer(s) |
|---|---|---|
| `masterly-framework` | **Architectural source of truth** — ADRs, architecture docs, design system, this CLAUDE.md, conventions | All — shared |
| `masterly-platform-iac` | Terraform for Masterly's platform infrastructure | Layers 1 (shared) + 2 (regional CP) |
| `masterly-shared-iac` | Terraform for Shared MT data plane | Layer 3 (placeholder until first Shared MT customer) |
| `masterly-dedicated-iac` | Terraform for per-customer Dedicated cells | Layer 4 (placeholder until first Dedicated customer) |
| `terraform-azurerm-masterly` | **Public** (Apache 2.0) Terraform module customers and partners consume at a semver tag. No credential to fetch — the gate is images + licence ([ADR 0067 amendment 2026-09-01](masterly-framework/docs/adr/0067-customer-artifact-distribution.md)). | Layer 5 — the shipped module |
| `masterly-demo-iac` | Private: **Masterly's own demo install only** (`deployments/demo-eu`) plus the workflows that plan/apply/operate it. Renamed from `masterly-self-hosted-iac` on 2026-09-07, when the module copy at its root was deleted (MAS-42) — **the module lives in `terraform-azurerm-masterly` and nowhere else.** Not what customers consume. | Layer 5 — internal |
| `masterly-platform-backend` | FastAPI control-plane services: `license-issuer`, future `tenant-router` + `billing-aggregator`. Masterly-operated; never ships to customers. | Layer 2 (regional CP) |
| `masterly-application-backend` | FastAPI product application: `api`, `workers`, future `ai-router`, plus business modules (IR, golden, DQ). One codebase across all deployment models. | All — application code (data plane) |
| `masterly-application-frontend` | Next.js + Stytch B2B user sessions. Ships with the application backend across all deployment models. | All — application code |
| `masterly-python-sdk` | **Public** (Apache-2.0) **Python client** (`pip install masterly`, v0.2.0 on PyPI) — extract (products/changes/golden) + ingest + configuration (workspaces/domains/data-models/sources) over `/v1` (ADR 0068), plus `examples/demo_data.py` for demo master data. Development history through v0.2.0 lives in the private `masterly-python-sdk-archive`. | Customer-side tooling |
| `masterly-web` | **Public web** — marketing site (`masterlydata.com`) + customer docs (`/docs`). Astro + Starlight, static, on Azure Static Web Apps. Masterly-operated; ships to no customer; shares only design tokens with the frontend. See [ADR 0030](masterly-framework/docs/adr/0030-public-web-marketing-and-docs.md). | Public web (marketing + docs) |
| `masterly-demo01-cac` | GitOps config-as-code for the demo install: Data Models, IR rules, `modules.yaml`. (Named `demo-config` in older docs and in ADRs 0012/0014 — those record history; this is the live name.) | Layer 5 — demo install |

The demo install (subscription `msly-demo-01-eu`) lives in `masterly-demo-iac/deployments/demo-eu/` and consumes the **published** module at a pinned version (`source = "masterly-data/masterly/azurerm"`), exactly as a customer does. It used to consume it by local path, which dogfooded every module change against the live demo for free; that no longer happens, so **plan a live install against the module branch before tagging a module release** (decided 2026-06-11; changed 2026-08-31 when the module was published).

## Tech stack

- **Backend:** Python + FastAPI, stateless containerized microservices, sync + async patterns
- **Frontend:** TypeScript + Next.js
- **Data plane:** PostgreSQL + Redis (per-Environment)
- **Control plane:** Cosmos DB NoSQL with multi-region writes (see [ADR 0016](masterly-framework/docs/adr/0016-cosmos-db-for-layer-2.md))
- **Hosting:** Azure Container Apps
- **Auth:** Stytch B2B (multi-org, multi-connection per org)
- **IaC:** Terraform throughout, OIDC to Azure (see [ADR 0015](masterly-framework/docs/adr/0015-terraform-as-iac.md), [ADR 0017](masterly-framework/docs/adr/0017-github-actions-terraform-cd.md))
- **CD:** GitHub Actions with OIDC federation, plan-on-PR + apply-on-merge with Environment approval

API-first. The REST API contract is the primary interface. MCP, Stream, REST, and GUI are equally first-class channels.

## Design system

The Masterly design system lives in [`masterly-framework/design-system/`](masterly-framework/design-system/). When working on any UI surface (Next.js frontend, marketing pages, decks, prototypes):

- **Tokens:** [`masterly-framework/design-system/colors_and_type.css`](masterly-framework/design-system/colors_and_type.css) is the single source of truth. Never hard-code colors, type sizes, or spacing.
- **Brand fundamentals:** [`masterly-framework/design-system/README.md`](masterly-framework/design-system/README.md) — dark-first, warm-gradient, sharp-typographic. The signature `.` after "Masterly." is non-negotiable.
- **Component reference:** [`masterly-framework/design-system/ui_kits/product/`](masterly-framework/design-system/ui_kits/product/) shows the product surface conventions.
- **No emoji** in Masterly materials. Use Phosphor / Lucide / Heroicons outline sets.
- **Magenta is sparingly used** — 60% dark, 30% gradient, 10% magenta accent.
- **Sentence case** for all UI labels, buttons, and headings except the wordmark "Masterly.".

## Decision-making protocol

| Decision type | Where it lands |
|---|---|
| **New cross-cutting architectural decision** (affects multiple repos, hard to reverse) | New ADR in [`masterly-framework/docs/adr/`](masterly-framework/docs/adr/) using [`template.md`](masterly-framework/docs/adr/template.md). Mark Proposed; the user accepts it — and an acceptance is not complete until a card has carried that status into the ADR and its index row on `origin/main`, opened in the same pass the acceptance is recorded ([lifecycle](masterly-framework/docs/operating-model/lifecycle.md)). |
| **Open question** that doesn't yet have a decision | New or updated file in [`masterly-framework/docs/architecture/open-questions/`](masterly-framework/docs/architecture/open-questions/) |
| **Cross-cutting convention** (naming, error handling, file layout patterns) | New or updated file in [`masterly-framework/docs/conventions/`](masterly-framework/docs/conventions/) |
| **Architecture detail** (how a subsystem actually works) | New or updated file in [`masterly-framework/docs/architecture/`](masterly-framework/docs/architecture/) |
| **Repo-local convention** (file structure inside one repo, build patterns specific to that repo) | That repo's `CLAUDE.md` or `README.md` — not the framework |
| **Operational runbook** | That repo's `RUNBOOK.md` or similar — not the framework |

**Rule of thumb:** if a decision affects how a second engineer would build a feature in a *different* repo, it belongs in `masterly-framework`. If it affects how to wire one feature together in *this* repo, it stays local.

## Naming conventions (cross-repo)

| Surface | Pattern | Example |
|---|---|---|
| Domain (primary) | `masterlydata.com` | |
| Subdomains | `<purpose>.masterlydata.com` | `cp.masterlydata.com`, `app.masterlydata.com`, `api.masterlydata.com` |
| Azure subscriptions | `msly-<layer>-<env>-<geo>[-<slug>]` | `msly-shared-prod`, `msly-cp-prod-eu`, `msly-demo-eu` |
| Azure resource groups (Masterly subs) | `rg-msly-<layer>-<purpose>-<cell>` | `rg-msly-shared-security` (shared = no cell), `rg-msly-cp-aca-eu01` |
| Azure resource groups (customer self-hosted) | `rg-masterly-<install>-<purpose>` ([ADR 0039](masterly-framework/docs/adr/0039-installs-as-environment-deployment-grouping.md)) | `rg-masterly-prod-aca`, `rg-masterly-nonprod-data` |
| ACA Environments | `aca-<layer>-<cell>[-<slug>]` | `aca-cp-eu01`, `aca-demo-eu01` |
| Container Apps | `ca-<app-name>` | `ca-license-issuer`, `ca-frontend`, `ca-api`, `ca-workers` |
| Cosmos DB (control plane) | `masterly-cp-cosmos-<cell>` | `masterly-cp-cosmos-eu01` (serverless, single-region per cell in v1 — ADR 0016) |
| Container Registry | `masterly.azurecr.io` (single registry, geo-replicated) | |

**Geo vs cell vs region** (per [ADR 0022](masterly-framework/docs/adr/0022-cell-based-resource-naming.md)):
- **Geo** (`eu`, `us`) — the data-residency boundary. Lives in the **subscription / Environment** name (`msly-cp-prod-eu`, Environment `prd-eu`).
- **Cell** — a numbered regional instance within a geo: `<geo><NN>` (`eu01`, `eu02`, `us01`), carried on **resources**. Dedicated (Layer 4) cells use a customer slug instead (`msly-cell-<customer>-<region>`). Shared (Layer 1) resources are cross-cell and carry **no** cell suffix.
- **Physical Azure region** (e.g. Sweden Central — [ADR 0021](masterly-framework/docs/adr/0021-eu-control-plane-in-sweden-central.md)) is **never baked into a name** — it is the resource's native `location`, so a cell can move regions without a rename.

Required Azure tags on every RG, now genuinely enforced by Azure Policy (`msly-required-rg-tags`, `Deny`, on both production subscriptions — see `masterly-platform-iac/bootstrap/policy/README.md`): **seven** — `cost-center`, `data-residency`, `deployment-model`, `owner`, `lifecycle`, `environment`, `region`. (This line listed only five until 2026-09-07; `platform-iac-resources.md` §1 is the source of truth.) **Azure reserves tag-name prefixes `azure`, `microsoft`, `windows`** — never start a tag name with them (use e.g. `cloud-region`, not `azure-region`).

## Working style

- **Foundation-first.** Get pluggable data storage, tenant isolation, deployment topology, and DBU instrumentation right *before* shipping features. Lesson from the PoC.
- **Be explicit.** Solo founder + agents today; team later. Write as if a smart engineer (or agent) is picking this up cold next month.
- **Push back on the user when their request conflicts with a hard constraint above.** Don't silently relax constraints.
- **No code or infra changes without an ADR for cross-cutting decisions.** Repo-local changes don't need ADRs.
- **Multi-repo work:** when a change spans repos, decide if the *decision* is shared (ADR in framework) or *implementation only* (per-repo PR). Don't duplicate the decision across repos.
- **Customer docs ship with the product.** A customer-facing change (capability, API, MCP tool, config, deployment, or behavior) isn't done until the public docs in `masterly-web` reflect it — see the [customer-docs convention](masterly-framework/docs/conventions/customer-docs.md). Internal-only changes need none; say which in the PR.

## Reading order for a new session

If you're a Claude session just opening in any Masterly repo, read in this order:

1. **This file** (project-wide guidance) — already auto-loaded
2. **The local repo's `CLAUDE.md`** (per-repo specifics) — already auto-loaded
3. **`masterly-framework/docs/architecture/build-order-and-phases.md`** — current roadmap and where this repo fits in
4. **ADRs relevant to the work at hand** — see references in the local `CLAUDE.md`
5. **The architecture doc(s) for the specific subsystem** — see `masterly-framework/docs/architecture/`
