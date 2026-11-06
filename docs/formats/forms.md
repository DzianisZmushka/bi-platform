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
  column: status          # a column with choices: the states
  transitions:
    submit: { from: draft, to: submitted }
    approve: { from: submitted, to: approved, by: "${user.role == 'sales_manager'}" }
    reopen: { from: [submitted, approved], to: draft, by: "${user.role == 'sales_manager'}" }
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

## In dashboards

```yaml
- form: kpi-comment          # embeds a form or a sheet; its id is the form's id
- text: revenue_comment      # "record text": the first row of a dimensions-only query
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
- `obi serve --user <name> --user-attr role=<role>` sets `user.*` until the Phase 2 runtime has sign-in.
