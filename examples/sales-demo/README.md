# sales-demo

A project that needs no setup. DuckDB reads the CSV files in `data/`. Regenerate them with `uv run python examples/sales-demo/data/generate.py`.

The YAML here is the spec. Every file must pass `uv run obi-formats validate examples/sales-demo` and `uv run obi-formats fmt --check examples`.

The full formats are in the JSON Schemas in [packages/formats/src/obi_formats/schemas](../../packages/formats/src/obi_formats/schemas/). This page is the short version. Related docs:
- Conventions: [ADR-002](../../docs/adr/002-artifact-format-conventions.md)
- Canonical form: [ADR-003](../../docs/adr/003-canonical-serialization.md) and [ADR-006](../../docs/adr/006-serializer-and-comment-preservation.md)
- Names, grain and filters: [ADR-005](../../docs/adr/005-references-grain-and-filter-scope.md)
- Expressions: [expressions.md](../../docs/formats/expressions.md)
- Forms and writable entities: [forms.md](../../docs/formats/forms.md) and [ADR-012](../../docs/adr/012-forms-and-writable-entities.md)
- Roles and row-level security: [access.md](../../docs/formats/access.md) and [ADR-013](../../docs/adr/013-roles-before-deploy.md)

**Key order** (what `obi-formats fmt` produces, so write it this way): the kind key first (`kpi: id`, `sum: ...`), then `title`/`label`/`description`, then bindings (`measure`, `x`, `y`, `rows`, `values`, `where`, `grain`, `ignore_filters`), then behaviour (`sort`, `limit`, `totals`, `on_click`, `visible`), then presentation (`format`, `color`, `cells`), and `items` last.

## Project and model files

`project.yaml`: `project: <id>`, `title`, `description`, `default_connection: <name>`, `currency: USD`, and `connections: { <name>: { duckdb:, tables: { <table>: <file> } } }`. `duckdb:` left empty means an in-memory DuckDB that reads the listed CSV or Parquet files. `duckdb: { database: .obi/sales-demo.duckdb }` is the file that holds the writable tables (never in Git), and `seeds: { targets: data/targets.csv }` gives a writable table its first rows.

`model/<id>.model.yaml`: `model: <id>`, `title`, `description`, `currency` (optional, see erp-sales), `entities: { <name>: <entity> }` and `hierarchies`.

## Model (`model/*.model.yaml`)

| Element | Shape |
|---|---|
| entity | `table`, `key`, `grain` (optional), `currency: { code, date }` (optional, see erp-sales), `joins: { <entity>: <foreign key> }` (many-to-one; the foreign key is a column, a column expression such as `left(PRDHA, 5)`, or a list with one per key column such as `[order_id, line_no]`), `rollups: { <name>: { sum: <child column expr>, from: <child entity> } }` (per-row totals from a child entity that joins this one, usable in aggregations), `dimensions`, `measures`. The table and every column must exist in the data |
| dimension | `<name>:` with optional `{ label, column, type: string\|number\|date\|boolean, fiscal: { year, period } }`. `fiscal` adds the grains `fiscal_year` and `fiscal_period` and the fiscal date presets. Or a lookup: `{ lookup: <table>.<value column>, key: <its key column>, match: <expression on this entity> }` |
| measure | aggregation as the key: `sum` / `count` / `count_distinct` / `avg` / `min` / `max: <expression over columns of the entity's own table>`, plus optional `where: <condition over model dimensions>`, or `calc: <expression over measures>`. `where:` names dimensions, bare or prefixed, from any entity reachable through joins (`segment = 'Enterprise'`, `year(signup_date) = year(date)`), never raw columns. Allowed functions: [expressions.md](../../docs/formats/expressions.md#column-expressions-in-models). Other keys: `label`, `grain`, `date: <date dimension>` (reach the calendar through that date instead of the shortest route), `convert: USD` (converted row by row with the model's `currency:` rates), `format: currency\|percent\|integer\|number` |
| hierarchy | `<name>: [level, level, ...]`. Usable wherever a dimension is. In `obi serve`, clicking a bar or row drills down one level, and a breadcrumb leads back. Static renders show the top level |

**Joins.** When an entity reaches another by more than one route, the shortest route is used, and `obi validate` prints an info line saying which. Routes of the same length are an error.

**Tables.** Every table an entity or lookup uses must be registered in `project.yaml` under the connection's `tables` (name → file). Then run `obi catalog` so validation knows its columns. A table that is not registered is a validation error.

**Names.** Measures, dimensions and hierarchies share one model-wide namespace. Rollups do not: a rollup is only used inside its own entity's aggregations, so it may have the same name as a measure or dimension elsewhere. Fields are referenced by bare name (`segment`). An entity prefix (`customers.segment`) is always allowed and is required only when the bare name is ambiguous. Date dimensions take a grain suffix: `date.month`, or `date.fiscal_period` / `date.fiscal_year` when the dimension has `fiscal:`.

**Grain.** This is the list of dimensions a measure can be sliced and filtered by. It defaults to everything reachable through joins. `targets` declares `grain: [date.month, region]`.

## Dashboard (`dashboards/<id>/dashboard.yaml`)

| Element | Properties |
|---|---|
| `filter: <id>` | `dimension`, `default` (a date or a preset such as `this_year`), `multiple` (default true), `pages` (default: all) |
| `page: <id>` | `title`, `description`, `visible`, `items` |
| containers | `row`, `grid` (`columns: [2, 1]` fractions or a count), `card`, `section` (`collapsed`) take `title`, `description`, `visible`, `items`. `tabs` takes `visible`, `items` of `tab` (`title`, `visible`, `items`) |
| `kpi: <id>` | `measure`, `target` (measure), `compare: previous_period\|previous_year`, `sparkline` (date dimension), `ignore_filters`, `color` |
| `chart: <id>` | `mark: line\|bar\|area\|point\|arc`, `x`, `y` (one or a list), `series`, `detail`, `stack`, `where`, `ignore_filters`, `sort`, `limit`, `color`. The orientation of a bar follows whichever axis holds the measure |
| `table: <id>` | `rows`, `columns` (pivot), `values`, `where`, `ignore_filters`, `sort` (`-` = descending), `limit`, `totals`, `cells: { <field>: { color, bar } }` |
| `text: <id>` | `title`, `content` (Markdown, may contain `${...}`). As record text it also takes `rows` (dimensions only), `where`, `ignore_filters`, `sort` and `empty`, and `${row.*}` reads the first row |
| `form: <form id>` | embeds a form or sheet from `forms/`; its `${filters.*}` defaults read this dashboard's filters |
| `button: <id>` | `label`, `on_click` |
| `kpi`, `chart`, `table` | `title`, `description` (shown as an ⓘ tooltip) |
| any widget | `visible: "${...}"` |
| `kpi`, `chart`, `table`, `button` | `on_click: { navigate: <page> \| <dashboard>/<page>, set: { <filter>: "${row.x}" } }`. A KPI's `row` holds its `measure` and `target` |

**`ignore_filters`.** Required when a filter on the page cannot reach one of the widget's measures, for example `channel` against monthly regional targets. The validator error gives the exact line to add. The renderer marks such widgets with the filters they ignore.

**Defaults that are never written.** Titles derive from ids or bindings. KPI colour follows the sign of the comparison delta, or attainment when there is a `target`. Colour tokens are `ok`, `warning`, `danger`, `neutral`, `accent`.

**Not in Phase 0.** Clicking a chart or table that has no `on_click` and no hierarchy does nothing. Default cross-filtering arrives with the Phase 2 runtime. A property the renderer does not support yet is a validator error ("not supported yet"), never silently ignored.

## Forms (`forms/<id>.form.yaml`)

Two tables are writable (the full format is in [forms.md](../../docs/formats/forms.md)):

- **`targets`:** the CSV seeds it, and the sheet `targets` on the page `target_entry` edits it. Edit a target and the attainment chart next to it changes.
- **`kpi_comments`:** the form `kpi-comment` writes to it, and the record text `revenue_comment` shows the latest comment next to the revenue KPI.

```bash
uv run obi serve examples/sales-demo --user alice
```

`obi serve` creates and seeds `.obi/sales-demo.duckdb` on first start, and migrates it when the model changes. Delete the file to start over. `obi migrate examples/sales-demo --reseed targets` reloads the targets from the CSV, moving the edited rows to history.

## Who sees what (`access.yaml`)

The pattern: **put `access:` on the smallest master-data entity every fact joins, not on the facts.**
- Here that is `regions`: `access: { where: region_code in user.regions }`.
- `order_lines` (through `customers`), `returns`, `targets` and `kpi_comments` all reach `regions`, so this one line limits every number, filter choice, sheet row and write to the user's regions.
- The attribute holds region **keys** (`emea`, `na`), which is why `regions` has `region_code: { column: region_id }`. Display names can change; keys do not.

`access.yaml` gives everyone (`default`) every dashboard and form to view and every entity to read. The other roles add only what they need:
- `sales_rep` edits `kpi-comment`;
- `sales_manager` edits every form, including the `targets` sheet;
- `admin` sees every row (`rows: all`).

`order_lines.unit_cost` is readable only by `sales_manager` and `admin`, so reps and viewers see "No access" on the margin widgets.

```bash
uv run obi serve examples/sales-demo --user rob --role sales_rep --attr regions=emea
uv run obi serve examples/sales-demo --user mia --role sales_manager --attr regions=emea,na
```
