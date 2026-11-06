# Forms and writable entities

Business users enter data into tables the platform owns, and the model reads it back like any other entity. Decisions are in [ADR-012](../adr/012-forms-and-writable-entities.md). The examples are `kpi-comment` and `targets` in [sales-demo](../../examples/sales-demo/) and `order-note` in [erp-sales](../../examples/erp-sales/).

## Writable entities (in the model)

```yaml
kpi_comments:
  table: kpi_comments
  key: comment_id
  writable: true
  columns:
    comment_id: { type: id }
    kpi: { type: string, required: true, choices: [revenue, attainment, margin, returns] }
    region_id: { label: Region, type: string, references: region }
    month: { type: date, required: true }
    comment: { type: text, required: true, check: length(comment) <= 500 }
  joins: { calendar: month, regions: region_id }
  dimensions:
    commented_by: { column: created_by }
    commented_at: { column: created_at, type: timestamp }
```

**Entity-level properties:**

| Property | Meaning |
|---|---|
| `writable: true` | The platform owns the table: `obi migrate` creates and alters it, and forms may write to it. |
| `columns` | The table's typed columns. Names are column names, so SAP-style `VKORG` is fine. **Each column is also a dimension of the same name**, unless the entity already has a field of that name. `dimensions:` on a writable entity is only for renames (`note_status: { column: status }`) and audit columns. |
| `per: <entity>` | One row per row of another entity: a note per order item. The key is that entity's key, its key columns are created automatically, that entity joins this one (LEFT, so rows without a note still show), and every write checks that the parent row exists. Use it instead of `key:`. |
| `workflow` | A status column that changes only through declared transitions (below). |

**Column properties.** These rules apply on every write path: form, sheet, paste, CSV import, later the API.

| Property | Meaning |
|---|---|
| `type` | `id` (a number the platform assigns on insert), `string`, `text` (long, shown as a textarea), `number`, `integer`, `date`, `timestamp`, `boolean` |
| `label`, `description` | Shown in forms and as the dimension's label |
| `required` | Must have a value |
| `default` | A literal, or `${user.*}` (column defaults have no dashboard filters) |
| `choices` | The allowed values; forms show a select |
| `references: <dimension>` | The column stores the key of that dimension's entity; forms show the dimension's values. The entity must also join that entity on this column. A value given as the display text (such as a default of `${filters.region.first}` = `EMEA`) is resolved to the key on save; an ambiguous display value is an error. |
| `check` | A column expression ([expressions.md](expressions.md#column-expressions-in-models)) that must not be false, such as `revenue_target >= 0` |

**Audit and history.** Every writable table also has `created_by`, `created_at`, `updated_by` and `updated_at`, and a `<table>_history` table that keeps every earlier version of each row, with `history_op`, `history_at` and `history_by`. Concurrent edits are last-write-wins; the history shows who wrote what.

## Workflow

```yaml
workflow:
  column: status  # a column with choices: the states
  transitions:
    submit: { from: draft, to: submitted }
    approve: { from: submitted, to: approved, by: "${user.roles.includes('sales_manager')}" }
    reopen:
      from: [submitted, approved]
      to: draft
      by: "${user.roles.includes('sales_manager')}"
```

- The workflow column is never a form field; it changes only through `transitions`.
- `from` is a state or a list of states, and `to` is a state.
- `by` is an expression over `user`; absent means anyone.
- A form in edit mode shows one button per transition the user may perform from the current state, and the rule is enforced again on write.

Still open: other fields of an approved row stay editable by anyone (ADR-012).

## Form files: `forms/<id>.form.yaml`

### One record: `form: <id>`

```yaml
form: order-note
title: Order item note
entity: order_notes

fields:
  - field: VBELN
    label: Sales order
    default: "${filters.order.first}"
    readonly: true
  - field: note
    widget: textarea
on_submit: { navigate: open_orders }
```

**Properties:**
- `entity`: a writable entity.
- `fields`: in order. Each `field` names a column, including key columns created by `per`. Audit columns and the workflow column cannot be fields.
- `title`, `description`.
- `on_submit: { navigate, set }`: where to go after a successful save, as in dashboards. Absent, the form stays and the page around it refreshes.

**Field properties:**
- `label`, `help`.
- `widget`: `text`, `number`, `date`, `select`, `checkbox` or `textarea`. It defaults from the column: choices or references → select, `text` → textarea, number/integer → number, date → date, boolean → checkbox, otherwise text. A widget that does not fit the column type is a validation error.
- `default`: a literal or `${...}` over `filters` (of the dashboard that embeds the form) and `user`.
- `required`: may make an optional column required here, never the reverse.
- `readonly`: shown, not editable. On an existing row the stored value stays. On a new row the value is the field's default, or, when the default reads dashboard filters, the filter's value.
- `visible`.

**Edit mode.** When every key column of the entity has a value (from defaults, for example from filters set by a click in a table) and that row exists, the form loads it for editing and shows the workflow buttons. Otherwise it creates a new row. A form whose key is an `id` column always creates.

### Many rows: `sheet: <id>`

```yaml
sheet: targets
title: Revenue targets
entity: targets
columns: [month, region_id, revenue_target]

filters:
  - filter: period
    column: month
    default: this_year
sort: month
```

- `columns`: the columns shown, in order. Key columns are read-only on existing rows: to change a key, delete the row and add a new one. Pasted rows fill the columns that are not `readonly`, in this order.
- `readonly`: columns that cannot be edited. New rows (add, paste, import) cannot set them either; they take the column default.
- `filters`: each with `filter` (id), `column`, `default` (a value, a date preset, or `${...}`) and an optional `title`. On a date column, fiscal presets such as `fiscal_year_to_date` come from the fiscal calendar the column joins. A value that is not a date, range or preset shows an error above the sheet.
- `sort`: a column, `-` for descending.
- `allow`: the subset of `edit`, `add`, `delete`, `paste` (tab-separated rows from a spreadsheet) and `import` (a CSV file whose header names the columns). Absent means all of them. The server refuses an action that is not allowed, and key and `readonly` columns cannot change on save. Pasted and imported rows are checked like form entries, and nothing is saved if one row fails.
- Every row has a history view.

## Where the rules hold

The page only reflects the rules; the server enforces them. A request built by hand is refused the same way, and nothing is written:
- an action outside `allow`;
- a value for a column the form or sheet does not show;
- a changed key, `readonly` or workflow column;
- a missing `required` field;
- a column rule, a `per` parent that does not exist, or a transition from the wrong state or by the wrong user.

## Writing over HTTP: the `/_write` protocol

Every save from a page is one form-encoded `POST /_write`. Until a real API exists, this is the API: a script or `curl` sends the same fields and gets the same checks and refusals as the page.

**Who:** the session cookie from `POST /_signin` (fields `name`, `password`; the answer sets `obi_session`). With `obi serve --user` no sign-in is needed.

**Fields:**

| Field | Meaning |
|---|---|
| `_form` | The form or sheet id. |
| `_action` | What to do; see below. |
| `_return` | The page to come back to, such as `/order-to-cash` (default `/`). |
| `_next` | A form only: where to go after a successful save (`on_submit: { navigate }` fills it on the page). |
| `<field>` | One per form field or sheet column, by its name (`note`, `VBELN`). A form's key fields pick the row: new row or edit. |
| `_key` | Sheets only, for `sheet_save` and `sheet_delete`: the row's key as JSON, `{"month": "2026-01-01", "region_id": "emea"}`. |
| `_rows` | Sheets only. For `sheet_paste`: tab-separated lines, in the sheet's column order without `readonly` and workflow columns. For `sheet_import`: CSV with a header row. |

**`_action` values:**
- A form takes `save` (the default) or `transition:<name>`, such as `transition:submit` or `transition:approve` (the model's workflow).
- A sheet takes `sheet_add`, `sheet_save`, `sheet_delete`, `sheet_paste` or `sheet_import`, each only when the sheet's `allow` lists it.

**Answers:**
- `303` to `_next` or `_return`: written.
- `422`: refused, nothing written. The page shows the reasons inside the form. The `X-Obi-Error` header holds them all on one line (`note: is required; next_step: must be one of ...`). Printable ASCII is sent as is; other characters and `%` are percent-encoded UTF-8.
- `401`: not signed in (with an `X-Obi-Error` saying so).
- `503`: no release is deployed yet.

```bash
curl -s -c jar -d name=mia --data-urlencode password='the password' http://127.0.0.1:8765/_signin
```

```bash
curl -s -b jar -o /dev/null -D - -d _form=order-note -d _action=transition:submit -d VBELN=0001005963 -d POSNR=000040 -d note=x http://127.0.0.1:8765/_write
```

## In dashboards

```yaml
- form: kpi-comment  # embeds a form or a sheet; its id is the form's id
- text: revenue_comment  # "record text": the first row of a dimensions-only query
  rows: [comment, commented_by, commented_at]
  where: { kpi: revenue }
  ignore_filters: [channel, segment]
  sort: -commented_at
  content: |
    ${row.comment}
    — ${row.commented_by}, ${row.commented_at}
  empty: No comment yet for this period.
```

- A `form:` widget embeds a form or sheet; its `${filters.*}` defaults read the host dashboard's filters, which the validator checks.
- Record text takes `rows` (dimensions only), `where`, `ignore_filters`, `sort`, `content` with `${row.*}` from the first row, and `empty` for when nothing matches.
- In `obi serve`, a form is also available on its own at `/forms/<id>`.

## The database

- `project.yaml` names a DuckDB file: `duckdb: { database: .obi/<project>.duckdb }`. It is never in Git, because `.obi/` is ignored.
- `seeds: { <table>: <csv> }` gives initial rows, loaded once when the table is created; `obi migrate --reseed <table>` loads them again, moving current rows to history.
- `obi serve` and `obi validate --execute` migrate automatically when the database is missing or behind the model. `obi migrate` runs it explicitly.
- `user.*` is the signed-in user ([access.md](access.md)); `obi serve --user <name> --role <role> --attr <name>=<value>` sets it in local development.
