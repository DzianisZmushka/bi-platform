# ADR-007: Phase 0 renderer: server-side HTML from the CLI

- Status: Accepted
- Date: 2026-11-05

## Context

Phase 0 must prove the loop: YAML → validate → render on DuckDB → see the change. That includes the round trip through an external LLM. CLAUDE.md plans a TypeScript/React renderer (`packages/web`) that consumes the JSON form. Building that now means a JS toolchain, an API between browser and engine, and a client-side state model for filters. Phase 0 needs none of that to prove the formats.

## Decision

`obi render` produces one self-contained HTML page per dashboard, built in Python (`packages/cli/src/obi_cli/render.py`):

- **The renderer does all the data work.** It runs the queries, applies filters, evaluates `${...}` expressions (colours, visibility, templates, `on_click.set`), formats numbers and builds the navigation links. The page's JavaScript only switches pages and tabs, draws charts and follows links. It holds no data logic.
- **Charts are Vega-Lite** (the grammar charts were modelled on), loaded from cdn.jsdelivr.net at pinned versions. The chart grammar maps almost one to one: `mark`, `x`/`y`, `series` → color, `detail`, `stack`, folding for several `y` measures. Pages render without internet, but charts then show a message instead.
- **`obi render`** is static. Filters take their defaults or `--filter id=v1,v2` values. Links move between pages, but `on_click.set` cannot apply in a static file.
- **`obi serve`** runs the same function per request. It validates first, and a broken project shows the validation errors instead. Filter values travel in the URL: `_set=1` marks a complete filter state, and its absence means "these values on top of the defaults". So the filter bar, `on_click.set` and cross-dashboard links work, and edits to the YAML show on refresh.
- **The ignored-filters marker (ADR-005)** is rendered: "Not filtered by: Channel, Segment" appears on a widget when one of its `ignore_filters` is set and applies to its page.

## Not in Phase 0 (Phase 1 runtime)

- **Default cross-filtering.** Clicking a chart element or table row without `on_click` does nothing yet.
- **Hierarchy drill-down.** A hierarchy shows its top level only (ADR-001 scope).
- **Themes.** Colours are the built-in token palette.
- **`user.*` attributes.** There is no auth; `user` is empty.

## Consequences

- The Phase 0 test needs one command and a browser.
- Rendering is a pure function of (project files, filter values, as-of date), so tests cover it end to end without a browser.
- The React renderer in Phase 1 replaces only the presentation layer. Query planning, expression evaluation and link building move behind the runtime API, and their current tests stay valid.
- Each `Project` loads its CSV/Parquet files into in-memory DuckDB tables once. Views re-read the files on every query and made a render take about 20 s instead of 0.6 s.
