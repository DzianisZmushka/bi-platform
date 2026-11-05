# ADR-007: Phase 0 renderer: server-side HTML from the CLI

- Status: Accepted. Amended after the Phase 0 LLM test (KPI clicks, drill-down, coverage rule).
- Date: 2026-11-05

## Context

Phase 0 must prove the loop: YAML → validate → render on DuckDB → see the change. That includes the round trip through an external LLM. CLAUDE.md plans a TypeScript/React renderer (`packages/web`) that consumes the JSON form. Building that now means a JS toolchain, an API between browser and engine, and a client-side state model for filters. Phase 0 needs none of that to prove the formats.

## Decision

`obi render` produces one self-contained HTML page per dashboard, built in Python (`packages/cli/src/obi_cli/render.py`):

- **The renderer does all the data work.** It runs the queries, applies filters, evaluates `${...}` expressions (colours, visibility, templates, `on_click.set`), formats numbers and builds the navigation links. The page's JavaScript only switches pages and tabs, draws charts and follows links. It holds no data logic.
- **Charts are Vega-Lite** (the grammar charts were modelled on), loaded from cdn.jsdelivr.net at pinned versions. The chart grammar maps almost one to one: `mark`, `x`/`y`, `series` → color, `detail`, `stack`, folding for several `y` measures. Pages render without internet, but charts then show a message instead.
- **`obi render`** is static. Filters take their defaults or `--filter id=v1,v2` values. Links move between pages, but `on_click.set` cannot apply in a static file.
- **`obi serve`** runs the same function per request. It validates first, and a broken project shows the validation errors instead. Filter values travel in the URL: `_set=1` marks a complete filter state, and its absence means "these values on top of the defaults". So the filter bar, `on_click.set` and cross-dashboard links work, and edits to the YAML show on refresh.
- **Clicks.** `on_click` works on KPIs, charts, tables and buttons. A KPI's `row` is its measure and target values. A chart or table bound to a hierarchy drills down one level per click in `obi serve` (`drill.<widget>=<value>` in the URL, with a breadcrumb back). `on_click` takes precedence over drilling.
- **Accepted means handled.** Every schema property is in the renderer's `RENDERED` registry, the engine's `HANDLED_*`/`DOCUMENTATION_ONLY` registries, or marked `x-unsupported`, which the validator reports as "not supported yet". `test_coverage.py` enforces this. The Phase 0 LLM test found `kpi.on_click` accepted but ignored, and the audit that followed found page/widget `description`, pivot `limit`, hierarchy drill-down and `duckdb.database` in the same state.
- **The ignored-filters marker (ADR-005)** is rendered: "Not filtered by: Channel, Segment" appears on a widget when one of its `ignore_filters` is set and applies to its page.

## Not in Phase 0 (Phase 1 runtime)

- **Default cross-filtering.** Clicking a chart element or table row without `on_click` does nothing yet.
- **Drill-down in static renders.** A static file shows a hierarchy's top level.
- **Themes.** Colours are the built-in token palette.
- **`user.*` attributes.** There is no auth; `user` is empty.

## Consequences

- The Phase 0 test needs one command and a browser.
- Rendering is a pure function of (project files, filter values, as-of date), so tests cover it end to end without a browser.
- The React renderer in Phase 1 replaces only the presentation layer. Query planning, expression evaluation and link building move behind the runtime API, and their current tests stay valid.
- Each `Project` loads its CSV/Parquet files into in-memory DuckDB tables once. Views re-read the files on every query and made a render take about 20 s instead of 0.6 s.
