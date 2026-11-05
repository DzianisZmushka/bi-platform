# sales-demo

A project that needs no setup. DuckDB reads the CSV files in `data/`. Regenerate them with `uv run python examples/sales-demo/data/generate.py`.

The YAML here is the spec. Every file must pass `uv run obi-formats validate examples/sales-demo` and `uv run obi-formats fmt --check examples`.

The full formats are in the JSON Schemas in [packages/formats/src/obi_formats/schemas](../../packages/formats/src/obi_formats/schemas/). This page is the short version. Related docs:
- Conventions: [ADR-002](../../docs/adr/002-artifact-format-conventions.md)
- Canonical form: [ADR-003](../../docs/adr/003-canonical-serialization.md) and [ADR-006](../../docs/adr/006-serializer-and-comment-preservation.md)
- Names, grain and filters: [ADR-005](../../docs/adr/005-references-grain-and-filter-scope.md)
- Expressions: [expressions.md](../../docs/formats/expressions.md)

## Model (`model/*.model.yaml`)

| Element | Shape |
|---|---|
| entity | `table`, `key`, `grain` (optional), `joins: { <entity>: <fk column> }` (many-to-one), `dimensions`, `measures` |
| dimension | `<name>:` with optional `{ label, column, type: string\|number\|date\|boolean }` |
| measure | aggregation as the key: `sum` / `count` / `count_distinct` / `avg` / `min` / `max: <column expr>`, plus optional `where: <boolean column expr>`, or `calc: <expression over measures>`. Other keys: `label`, `grain`, `format: currency\|percent\|integer\|number` |
| hierarchy | `<name>: [level, level, ...]`. Usable wherever a dimension is; the chart drills down a level on click |

**Names.** Fields are referenced by bare name (`segment`). An entity prefix (`customers.segment`) is always allowed and is required only when the bare name is ambiguous. Date dimensions take a grain suffix: `date.month`.

**Grain.** This is the list of dimensions a measure can be sliced and filtered by. It defaults to everything reachable through joins. `targets` declares `grain: [date.month, region]`.

## Dashboard (`dashboards/<id>/dashboard.yaml`)

| Element | Properties |
|---|---|
| `filter: <id>` | `dimension`, `default` (a date or a preset such as `this_year`), `multiple` (default true), `pages` (default: all) |
| `page: <id>` | `title`, `items` |
| containers | `row`, `grid` (`columns: [2, 1]` fractions or a count), `tabs` → `tab`, `card`, `section` (`collapsed`). All take `title`, `visible` and `items` |
| `kpi: <id>` | `measure`, `target` (measure), `compare: previous_period\|previous_year`, `sparkline` (date dimension), `ignore_filters`, `color` |
| `chart: <id>` | `mark: line\|bar\|area\|point\|arc`, `x`, `y` (one or a list), `series`, `detail`, `stack`, `where`, `ignore_filters`, `sort`, `limit`, `color`. The orientation of a bar follows whichever axis holds the measure |
| `table: <id>` | `rows`, `columns` (pivot), `values`, `where`, `ignore_filters`, `sort` (`-` = descending), `limit`, `totals`, `cells: { <field>: { color, bar } }` |
| `text: <id>` | `content` (Markdown, may contain `${...}`) |
| `button: <id>` | `label`, `on_click` |
| any widget | `title`, `visible: "${...}"`, `on_click: { navigate: <page> \| <dashboard>/<page>, set: { <filter>: "${row.x}" } }` |

**`ignore_filters`.** Required when a filter on the page cannot reach one of the widget's measures, for example `channel` against monthly regional targets. The validator error gives the exact line to add. The renderer marks such widgets with the filters they ignore.

**Defaults that are never written.** Clicking a chart or table cross-filters the other widgets on the page. Titles derive from ids or bindings. KPI colour follows the sign of the comparison delta, or attainment when there is a `target`. Colour tokens are `ok`, `warning`, `danger`, `neutral`, `accent`.
