# Project: Open BI Platform (working name — TBD)

## What we are building

A web-based data platform where **every artifact is a text file in a Git repository**, and every artifact can be edited in two equivalent ways: a visual builder for business users, and plain code for developers and LLMs. The two are strict projections of the same file — nothing exists in the builder that is not in the file, and nothing in the file is hidden from the builder.

The platform contains **no AI inside the product**. The design goal is that a user can download any artifact (or the whole project) as readable text, hand it to any LLM ("here is the code, return the code"), upload the result, and see it work. Everything an LLM needs is a public, compact, human-readable format with a published schema.

Artifact types (in priority order):
1. **Semantic models** — measures, dimensions, hierarchies, relations, row-level security.
2. **Dashboards** — web-app-grade layouts (not a 12-column grid), widgets bound to model measures/dimensions, cross-filtering, navigation, actions.
3. **Input forms & master-data forms** — write-back into ordinary SQL tables declared as writable in the model. Not tied to planning.
4. **Scheduled Excel reports** — cell-level bindings of model queries into Excel templates, generated on schedule, bursted to user groups.
5. **Pipelines (ETL)** — DAG of SQL/Python steps; step bodies are code only, the DAG is declarative.
6. **Roles & permissions** — declarative RBAC + row-level rules in code.

## Non-negotiable principles

- **File is the source of truth.** The runtime is a materialized index of a Git branch, never the other way around. Builder saves == Git commit.
- **Round-trip is lossless and canonical.** One state → exactly one serialization. Open-and-save in the builder must produce zero diff. Fixed key order, defaults never serialized, stable ordering of collections.
- **Compact and readable.** A serious dashboard with ~40 widgets should be a few hundred lines of YAML, not thousands. No GUIDs, no pixel coordinates, no inline SQL, no inline styles — use human-readable ids, semantic layout, model references and theme references.
- **Whole-artifact editing.** The unit of exchange with an LLM is an entire artifact or project folder, not a widget. Keep formats small enough for that.
- **Validator with human-readable errors.** On upload/deploy, errors must name the artifact, the element id, and the fix (e.g. "dashboard `revenue-overview`, widget `revenue_trend`: measure `net_rev` not found in model `sales`; did you mean `net_revenue`?").
- **Format before builder.** Every subsystem first exists as format + runtime (code-only). A visual builder is added only once the format has stabilized.
- **Buy, don't build, for commodity.** SQL execution → warehouse. ETL sources → `dlt`. SQL transforms → dbt-compatible runner. Dialect translation → `sqlglot`. Chart grammar → Vega-Lite-inspired subset. Semantic query engine: evaluate Cube before writing our own.
- **Never in Git:** users, passwords, connection secrets, form data, run logs, delivery history. Only rules, mappings and schemas.

## Repository layout of a user project (the thing we render)

```
project/
  project.yaml               # name, default connection, environments
  model/
    <name>.model.yaml        # entities, measures, dimensions, hierarchies, relations, RLS, writable entities
  dashboards/
    <id>/
      dashboard.yaml         # pages, layout tree, widgets, interactions
      filters.yaml           # global filters and bindings (optional, may be inlined)
  forms/
    <id>.form.yaml           # fields, validation, target writable entity, workflow (draft/submitted/approved)
  reports/
    <id>/
      report.yaml            # bindings: cell/range → model query, layout direction, formatting
      template.xlsx          # optional binary template (Git LFS); textual representation preferred
      schedule.yaml          # cron, recipients (groups), channel, bursting filter
  pipelines/
    <id>/
      pipeline.yaml          # DAG: steps, dependencies, schedule, params
      steps/*.sql|*.py       # step bodies — code only
  themes/
    <name>.theme.yaml        # colors, typography, spacing, widget defaults
  components/
    <name>.component.yaml    # reusable composites of base widgets
  access.yaml                # roles, artifact/entity/column permissions, user attributes (never users)
```

## Format design rules

- **YAML on disk, JSON internally.** Every format has a JSON Schema. Validation, indexing and the builder work on the JSON form; YAML is the human/LLM representation. Round-trip YAML↔JSON must be lossless.
- **Expressions, not programs.** Properties accept literals or small expressions in `${...}` (e.g. `color: "${value < 0 ? 'danger' : 'ok'}"`). Define a minimal expression language (comparisons, arithmetic, string ops, references to `value`, `row`, `user`, `filters`). No loops, no arbitrary code in YAML.
- **Semantic layout.** Containers: `stack`, `row`, `grid` (fractional columns), `tabs`, `card`, `section`, with nesting. Pixels are the renderer's job. Provide a constrained `style:` escape hatch (whitelisted CSS subset) for the rare case.
- **Stable ids.** Every widget, page, filter, field, step has a `id` chosen by the author, never regenerated. Ids are the addresses LLMs and users use.
- **Data binding by reference only.** Widgets reference `measure:` / `dimension:` names from a model; never SQL, never column names. The semantic engine generates the SQL.
- **Charts.** A single `chart` widget with a Vega-Lite-inspired grammar (`mark`, `x`, `y`, `series`, `stack`, `facet`) rather than many preset chart types.
- **Forms.** A `form` references a `writable` entity in the model. The platform owns the DDL (creates/migrates the table in a dedicated schema, adds audit columns, keeps a history table). Form data lives in the customer's database, reachable by any external tool.
- **Excel reports.** Support two authoring modes: (a) `report.yaml` bindings (cell/range → query), (b) in-cell formulas in the template like `=BI.VALUE("net_revenue", "region=EMEA")` resolved at generation time. Rendering via `openpyxl` preserving template styles.

## Tech stack (initial decision — revisit with evidence)

- **Backend:** Python 3.12, FastAPI. Semantic engine on `sqlglot` + Apache Arrow for all in-process data. Pipelines runner: `dlt` for extraction, dbt-compatible SQL execution, plain Python step executor. Scheduler: APScheduler initially; design for replacement.
- **Query backends (in order):** DuckDB (zero-setup dev/demo and small prod; reads CSV/Parquet/Excel; extensions for Postgres/S3) → PostgreSQL (also the default write-back store) → SAP HANA / Datasphere (via `hdbcli`, Datasphere Open SQL schema) → Snowflake, Databricks SQL, BigQuery.
- **Frontend:** TypeScript, React, Vite. Rendering engine consumes the JSON form of artifacts. Widget library implemented as React components with a typed props contract and a property schema that drives the builder's property panel. Charts via a Vega-Lite-compatible renderer or ECharts behind the grammar.
  - **Deliberate deviation for Phase 0 ([ADR-007](docs/adr/007-phase-0-renderer.md)).** The Phase 0 renderer is server-side HTML built in Python (`packages/cli/src/obi_cli/render.py`, used by `obi render` and `obi serve`), with Vega-Lite charts. Phase 1 (forms) extends the same server-side renderer. React work starts with the Phase 2 runtime and replaces only the presentation layer. Do not "fix" this by porting the renderer to React, or by adding a JS build to `packages/web`, before Phase 2 starts. Changing this needs a new ADR that supersedes ADR-007.
- **Git:** `pygit2`/`dulwich` or shelling out to `git`; projects are cloned per deployment; branches = environments (`dev` → `staging` → `main`). Builder commits to the working branch; "Publish" = merge/deploy.
- **Monorepo:** `packages/formats` (JSON Schemas, canonical serializer, validator — shared, must be usable standalone as a CLI), `packages/engine` (semantic/query), `packages/runtime` (API, scheduler, auth), `packages/web` (builder + renderer), `packages/cli` (download/upload/validate/deploy; later an MCP server).

## Delivery phases

**Phase 0 — prove the loop (weeks, not months).**
Model format + dashboard format + JSON Schemas + canonical serializer + validator + CLI (`validate`, `render`). Renderer that draws a dashboard from YAML on DuckDB over local Parquet/CSV. No builder, no auth, no scheduler. Success test: take a sample dashboard, send the YAML to an LLM, paste back the result, `validate`, render, see the change.

**Phase 1 — forms (write-back)** ([ADR-011](docs/adr/011-forms-before-runtime.md)). Writable entities in the model, `obi migrate` (DDL, audit columns, history table), form and sheet artifacts, embedding in dashboards, a minimal status workflow, validation rules on model columns. Proven like Phase 0: YAML → validator → DuckDB (a local database file) → `obi serve` → external-LLM round trip. This is the first major differentiator. Non-goals: planning (versions, allocations, spreading), computed fields beyond defaults, file uploads, concurrency control beyond last-write-wins with history.

**Phase 2 — runtime**, in two parts ([ADR-013](docs/adr/013-roles-before-deploy.md)). ADR-001…010 say "Phase 1" for this runtime phase; read it as Phase 2. (Composite-key joins were pulled forward into Phase 0, ADR-009.)

- **2a — roles and row-level security**, format first, still on DuckDB and `obi serve --user/--role/--attr`. `access.yaml` (roles as named sets of permissions on dashboards, forms, entities and columns, plus a `default` role, with `deny` over allow) and `access: { where: ... }` on model entities. RLS applies to every read and write through an entity. Users and role membership stay out of Git. Non-goals: sign-in, SCIM, column masking, audit-log export, roles edited in a UI.
- **2b — deploy and the web app**: Git-backed projects, deploy from branch, Postgres backend, sign-in with an IdP, React runtime (replaces ADR-007's presentation layer), cross-filtering, navigation, themes, snapshot measures (ADR-010). Sliced by [ADR-014](docs/adr/014-shareable-link-first.md): slice 1 is "a link I can send to a colleague" (sign-in with a user store outside Git, `obi serve --git`, `obi deploy`, one container), still DuckDB and server-rendered. Postgres and React come only after someone other than the owner has written through that link.

**Phase 3 — Excel reports + scheduling + bursting.**

**Phase 4 — pipelines.** `dlt` sources, SQL/Python steps, DAG runner, lineage from pipeline → model → widget.

**Phase 5 — visual builders**, one per stabilized format: model, dashboard, form, report, pipeline DAG. Each builder is a thin editor over the JSON form and must pass the zero-diff round-trip test.

**Phase 6 — CLI/MCP for agents**, SAP HANA/Datasphere connector, cloud warehouses.

## Working rules for Claude Code in this repo

- Before implementing a format, write (1) a sample artifact in YAML that a human would be happy to read, (2) its JSON Schema, (3) round-trip tests, (4) at least three validator error cases with the exact message text. Only then write the runtime.
- Keep sample projects under `examples/` and treat them as the executable spec. Every feature adds or changes an example.
- Prefer small, composable modules over frameworks. No ORMs for the engine path; Arrow tables end-to-end.
- Never add an LLM call to the product code. If a feature "needs AI", the answer is a better format or a CLI/MCP surface for an external agent.
- When unsure whether something belongs in Git or in the runtime database, apply the rule: *rules, schemas and mappings → Git; identities, secrets, data, logs → database.*
- Measure the format: track line count of the example dashboards. Growth without new capability is a regression.
- Every new format property ships with a round-trip test (`packages/formats/tests/test_roundtrip.py`) that puts a comment next to it: a full-line comment above it and a trailing comment on its line. The test asserts that `canonicalize` keeps both and that a second pass is a no-op. Comment attachment is our own code (ADR-006), not the YAML library's, so each new shape needs its own proof.
- Every property the validator accepts must have runtime behaviour. Something the renderer or engine does not support yet must be marked `x-unsupported` in the JSON Schema, so the validator reports "`<property>` is not supported yet: <reason>". It must never be silently ignored. A property whose format is accepted before its runtime exists (as access rules were, ADR-013) may be marked `x-pending` instead: the validator warns "not enforced yet" on every use, `obi serve` shows a banner on every page, and the marker is removed when that subsystem closes. The registries are `RENDERED` in `packages/cli/src/obi_cli/render.py`, `FORMS_RENDERED` in `packages/cli/src/obi_cli/forms.py`, `HANDLED_MODEL` / `HANDLED_PROJECT` / `DOCUMENTATION_ONLY` in `packages/engine/src/obi_engine/engine.py`, and `HANDLED_ACCESS` in `packages/engine/src/obi_engine/access.py`. `packages/cli/tests/test_coverage.py` walks the schemas and fails on any property that is not covered. Docs (README, examples) must not describe behaviour that the code lacks.
- Every format capability that exists in code must be documented in a README under `examples/` or in `docs/formats/`, or demonstrated in an example. `packages/cli/tests/test_coverage.py` fails when a schema property name appears in none of those documents.
- Every write restriction is enforced on the server; the page only reflects it. This covers `allow`, `readonly`, key immutability, workflow `by` and `from`, `check`, `required`, `choices`, `references`, `per`, and columns a form or sheet does not show. Mark each such schema property `x-write-rule`. Each one needs a test in `packages/cli/tests/test_write_rules.py` that posts around the page (a hidden action, a disabled field, an extra column, a changed key), is refused, and writes nothing. `test_coverage.py` fails on a marked property without one. A request built by hand gets the same refusal as the page.
- A filter that cannot be evaluated is an error, never "match everything". This applies to a bad URL value, an unknown preset, or a fiscal preset without a fiscal calendar. The validator reports it when it can. At runtime the affected widgets and sheets show the error instead of unfiltered data.
- Document decisions in `docs/adr/` as short ADRs (context, decision, consequences).

## Explicit non-goals (for now)

- Not an Airflow/Dagster replacement; not an Airbyte replacement.
- Not a planning/budgeting system (forms write to tables; no allocations, no versions of plans).
- Not a general low-code app builder (forms are "write to a model entity", nothing more).
- No embedded chat/assistant, no "ask your data" feature.
- No desktop client.
