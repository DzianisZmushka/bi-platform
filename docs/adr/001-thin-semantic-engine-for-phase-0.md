# ADR-001: Thin semantic engine on sqlglot + DuckDB for Phase 0

- Status: Accepted
- Date: 2026-11-05

## Context

Widgets bind to model measures and dimensions by name, so something has to turn "net_revenue by region, month, where channel = Online" into SQL. CLAUDE.md says to evaluate Cube before writing our own engine. Phase 0 only needs to prove the loop: YAML → validate → render on DuckDB over local files. Cube would bring a Node service, its own model format that we would have to generate, and an operational footprint. That is a lot of weight for proving a loop.

## Decision

Phase 0 gets a thin engine in `packages/engine`, built on `sqlglot` (SQL building and dialect output) and DuckDB (execution). Results are Apache Arrow tables from end to end.

The engine sits behind one narrow interface that any replacement must also implement:

```
query(model, SemanticQuery{measures, dimensions, filters, sort, limit}, context) -> pyarrow.Table
```

Phase 0 scope covers exactly what `examples/sales-demo` needs:

- Star schemas: each fact entity reaches each dimension entity through **exactly one** declared many-to-one join path.
- Aggregations `sum`, `count`, `count_distinct`, `avg`, `min`, `max`. Filtered measures (`where`) and calculated measures (`calc`, which reference other measures).
- Time grains on date dimensions (`day`, `week`, `month`, `quarter`, `year`).
- Drill-across between two facts on shared dimension entities. Each fact is aggregated in its own CTE, then the CTEs are joined on the requested dimensions. Targets vs. actuals need this.
- Sort, limit, simple dimension filters (equals, in, range, relative date presets).

Out of scope for Phase 0: hierarchies beyond "one level at a time", row-level security, caching and pre-aggregations.

The **model format is ours whatever engine runs it**. If Cube is adopted later, its schema is a build artifact compiled from `*.model.yaml` and is never committed or edited by hand.

## Re-evaluate Cube in Phase 1 when any of these become required

1. **Multi-table queries with ambiguous join paths.** Two or more paths between entities (role-playing dimensions such as order date vs. ship date against one calendar, bridge tables, many-to-many), or fan-out/chasm-trap protection on sums across one-to-many joins.
2. **Hierarchies.** Drill-down/up with level-aware aggregation, ragged or parent-child hierarchies (cost centres, org charts), and totals that must stay correct per level.
3. **Row-level security.** User-attribute filters added to every query and every join path, where a bug becomes a data leak. Cube's `queryRewrite`/security context is a mature reference.
4. **Caching.** A result cache keyed by query plus security context, invalidated on data refresh or deploy.
5. **Pre-aggregations.** Rollup tables built and refreshed on schedule, with queries routed to them transparently.

The re-evaluation is a timeboxed spike (one week or less) at the start of Phase 1. It compiles `examples/sales-demo` to a Cube schema and checks each criterion above with a concrete query. Decision rule: adopt Cube as a backend behind the `query()` interface if it covers the criteria that Phase 1 actually needs and the compile step is lossless. Otherwise extend the thin engine and write a superseding ADR.

## Consequences

- No extra service in Phase 0. `uv sync` plus local CSV files is the whole setup.
- We will write join planning and SQL generation ourselves. The single-path rule keeps it small. The validator rejects models that break it, with a message naming both paths.
- Engine tests are semantic: (model, query) → expected Arrow table. They become the acceptance suite for any replacement engine.
- Risk: the thin engine grows quietly into a full one. The criteria above are the tripwire. Any PR that starts implementing one of them must reference this ADR and trigger the spike.
