# sales-demo

A project that needs no setup. DuckDB reads the CSV files in `data/`. Regenerate them with `uv run python examples/sales-demo/data/generate.py`.

The YAML here is the spec. Until the JSON Schemas exist (Phase 0, step 4), this page summarises the vocabulary the examples use. Conventions: [ADR-002](../../docs/adr/002-artifact-format-conventions.md). Canonical form: [ADR-003](../../docs/adr/003-canonical-serialization.md).

## Model (`model/*.model.yaml`)

| Element | Shape |
|---|---|
| entity | `table`, `key`, `joins: { <entity>: <fk column> }` (many-to-one; `{ column, grain }` when the target is coarser), `dimensions`, `measures` |
| dimension | `<name>:` with optional `{ column, label, type: string\|number\|date\|boolean }` |
| measure | aggregation as the key: `sum` / `count` / `count_distinct` / `avg` / `min` / `max: <column expr>`, plus optional `where: <boolean column expr>`, or `calc: <expression over measures>`. Other keys: `label`, `format: currency\|percent\|integer\|number` |
| hierarchy | `<name>: [level, level, ...]`. Usable wherever a dimension is; the chart drills down a level on click |

Measure and dimension names form one namespace across the whole model. Date dimensions take a grain suffix: `date.month`.

## Dashboard (`dashboards/<id>/dashboard.yaml`)

| Element | Properties |
|---|---|
| `filter: <id>` | `dimension`, `default` (date presets such as `this_year`, `last_12_months`), `pages` (default: all) |
| `page: <id>` | `title`, `items` |
| containers | `row`, `grid` (`columns: [2, 1]` fractions or a count), `tabs` → `tab`, `card`, `section` (`collapsed`). All take `title` and `items` |
| `kpi: <id>` | `measure`, `target` (measure), `compare: previous_period\|previous_year`, `sparkline` (time dimension), `color` |
| `chart: <id>` | `mark: line\|bar\|area\|point\|arc`, `x`, `y` (one or a list), `series`, `detail`, `stack`, `where`, `sort`, `limit`, `color`. The orientation of a bar follows whichever axis holds the measure |
| `table: <id>` | `rows`, `columns` (pivot), `values`, `where`, `sort` (`-` = descending), `limit`, `totals`, `cells: { <field>: { color, bar } }` |
| `text: <id>` | `content` (Markdown) |
| `button: <id>` | `label`, `on_click` |
| any node | `title`, `visible: "${...}"`, `on_click: { navigate: <page> \| <dashboard>/<page>, set: { <filter>: "${row.x}" } }` |

Defaults that are never written: clicking a chart or table cross-filters the other widgets on the page. Titles derive from ids or bindings. KPI colour follows the sign of the comparison delta, or attainment when there is a `target`. Colour tokens are `ok`, `warning`, `danger`, `neutral`, `accent`.
