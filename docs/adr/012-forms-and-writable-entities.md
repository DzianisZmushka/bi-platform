# ADR-012: Forms and writable entities

- Status: **Accepted** (2026-11-06), after review of the proposal. The format reference is [docs/formats/forms.md](../formats/forms.md).
- Date: 2026-11-06

## Context

Phase 1 (ADR-011) adds write-back: business users enter targets, comments and notes into tables the platform owns, and the same model reads them back. Scope is "forms that write to model entities, nothing more".

Non-goals:
- planning (versions, allocations, spreading);
- computed fields beyond defaults;
- file uploads;
- concurrency control beyond last-write-wins with history.

## Decision

### 1. Writable entities live in the model

A writable entity is an ordinary entity (joins, dimensions, measures, grain) whose table the platform owns:

```yaml
kpi_comments:
  table: kpi_comments
  key: comment_id
  writable: true
  columns:
    comment_id: { type: id }                                         # number assigned on insert
    kpi: { type: string, required: true, choices: [revenue, attainment, margin, returns] }
    region_id: { label: Region, type: string, references: region }   # stores the regions key, shows `region`
    month: { type: date, required: true }
    comment: { type: text, required: true, check: length(comment) <= 500 }
  joins: { calendar: month, regions: region_id }
  dimensions:
    commented_by: { column: created_by }
    commented_at: { column: created_at, type: timestamp }
```

**Columns:**
- **Column properties:** `label`, `description`, `type` (`id`, `string`, `text`, `number`, `integer`, `date`, `timestamp`, `boolean`), `required`, `default` (a literal or `${user.*}`), `choices`, `references`, `check`.
- **Ids:** `id` is a platform-assigned integer (max + 1 on insert).
- **`timestamp`** is also a dimension type.
- **Every column is a dimension of the same name**, typed from the column. `dimensions:` on a writable entity is only for renames (`note_status: { column: status }`) and audit columns. Repeating a column there is an error.
- **Name clashes:** a column whose name the entity already uses for a measure (the `targets` column `revenue_target` and the measure of that name) gets no implicit dimension.

**References:**
- **`references: <dimension>`** on a column means: the column stores the key of that dimension's entity, and forms offer the dimension's values. The entity must also join that entity on the column (validator error otherwise).
- **Display values:** a value given as the display text (a default of `${filters.region.first}` = `EMEA`) is resolved to the key on save. An ambiguous display value is an error.

**`per: <entity>`** replaces the entity-level `references:` of the proposal. `order_notes: { per: order_items }` means:
- the key is the parent's key, and the key columns (`VBELN`, `POSNR`) are created automatically;
- the parent joins this entity (the reverse join is synthesized, so the model declares no join line);
- every write checks that the parent row exists.

`per` and `key` together are an error.

**Rules on columns apply to every write path:** form, sheet, paste, CSV import, and later the API. Forms add no validation of their own; they may only tighten `required`.

**The database:**
- **`obi migrate`** creates or alters the tables in the project's DuckDB **file** (`duckdb: { database: .obi/<project>.duckdb }`, never in Git).
- **Audit and history:** migrate adds `created_by`, `created_at`, `updated_by` and `updated_at`, and a `<table>_history` table holding every prior version of each row (`history_op`, `history_at`, `history_by`).
- **Automatic migrate:** `obi serve` and `obi validate --execute` run migrate when the database is missing or behind the model. `obi migrate` stays for explicit use.
- **Seeds:** initial rows come from `seeds:` in `project.yaml`, loaded only into a table migrate has just created. `obi migrate --reseed <table>` loads them again, moving the current rows to history.
- **Static validation needs no database.** Writable tables are not in `tables:` or `catalog.json`; their columns are the declared `columns` plus the audit columns.

**All joins are LEFT joins**, as they already were. An order item without a note still appears in `open_items`, with empty note columns (tested).

### 2. Workflow: one status column, declared transitions

```yaml
workflow:
  column: status            # a column with choices
  transitions:
    submit: { from: draft, to: submitted }
    approve: { from: submitted, to: approved, by: "${user.role == 'sales_manager'}" }
    reopen: { from: [submitted, approved], to: draft, by: "${user.role == 'sales_manager'}" }
```

- The status column changes **only** through a transition, so it is never a form field and a save that changes it is refused.
- A form in edit mode shows one button per transition allowed from the current status for the current user.
- `by:` is an expression over `user`; absent means anyone. It is enforced on write.
- No notifications and no approval inbox.

### 3. Form artifacts: `forms/<id>.form.yaml`

**`form: <id>` enters or edits one record.** Field properties are `label`, `help`, `widget`, `default`, `required`, `readonly` and `visible`.
- The widget defaults from the column type, so most fields need no `widget:`.
- `${filters.*}` are the filters of the dashboard hosting the form; standalone, they are empty.

**Edit mode:**
- When every key field of the form has a value and that row exists, the form loads the row for editing and shows the workflow transition buttons.
- Otherwise it creates a row.
- A form on an `id`-keyed entity therefore always creates.

`on_submit: { navigate, set }` works as in dashboards. Absent, the form stays and the dashboard around it refreshes.

**`sheet: <id>` edits many rows (master data).** Inline edit, add, delete, paste of TSV and CSV import are on by default.
- `allow: [...]` narrows them.
- `readonly: [...]` locks columns.
- Every row has a history view.
- A paste or import is one transaction: nothing is saved if any row fails, and the error names the row.

**Naming:** the brief said `grid:`, but `grid` is the dashboard layout container, so the artifact is `sheet:` (accepted in review).

### 4. Embedding and reading back

- **A dashboard embeds either kind with one widget kind:** `- form: <id>`. The widget's id is the form's id, so it is the one widget id that may be kebab-case.
- **Standalone:** `obi serve` opens forms at `/forms/<id>`.
- **Reading back:** writable entities feed widgets like any entity. The project database is attached to each request's connection, so a saved row shows on the next render without a restart.
- **"Record text":** the text widget takes an optional query (`rows`, `where`, `ignore_filters`, `sort`, `empty`). `rows` may hold dimensions only, and `${row.*}` reads the first row. The engine supports dimension-only queries for this.

### 5. Users before the runtime, roles after it

- **Phase 1 users:** `obi serve --user alice --user-attr role=sales_manager` sets `user.*`. That feeds defaults, workflow `by:` rules and the audit columns.
- **`roles.yaml` (Phase 2) will refer to existing names only:** form ids, entity names, column names and transition names. For example, `forms: { targets: [finance] }` or `columns: { targets.revenue_target: { write: [finance] } }`. Forms and models will not change when roles arrive.

### 6. Expression additions

- **`check:`** may use `length(text)` and `day(date)`. Both joined the column-expression allowed set, each with its DuckDB test (ADR-008).
- **Date filters:** dashboard expressions gain `filters.<id>.start` and `.end` for date filters (the form default `month: "${filters.period.start}"`). `.first` or `.count` on a date filter is a validator error that points to `.start`/`.end`.

## Consequences

- **Size:** a writable entity costs about 15–25 lines of model (kpi_comments 18, order_notes 25 with workflow), and a form 13–18 lines.
- **One process per database.** DuckDB locks the database file to one process, so one `obi serve` per project at a time. The second gets a clear error. To try two users at once, restart with another `--user`.
- **Last-write-wins:** two people editing one row both succeed; the history shows both versions.

**Open: field locking by workflow status.** Today an approved note is still editable by anyone; only its status is protected. Options:
- a `locked_in: [approved]` list on the workflow;
- per-column `editable_in:`;
- leaving it to `roles.yaml` in Phase 2.

To decide before Phase 1 closes.
