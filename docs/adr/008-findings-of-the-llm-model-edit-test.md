# ADR-008: Findings of the LLM model-edit test

- Status: Accepted
- Date: 2026-11-05

## Context

In the second Phase 0 LLM test, an external LLM edited `sales.model.yaml`, given only the model, the example README and `expressions.md`. It added a `returns` entity and a "new customer revenue" measure. The test produced three findings about the model format. A fourth problem surfaced alongside them: the validator accepted a table that did not exist.

## Finding 1: joins on composite keys

The LLM wrote `joins: { order_lines: [order_id, line_no] }`. The validator rejected it with three errors: list is not text, key has several columns, two join paths to calendar. The messages were correct, but they read as mistakes rather than as a limitation. The LLM worked around them by leaving `returns` unlinked.

Composite keys are everywhere in SAP sources (VBELN+POSNR, BELNR+BUZEI), so this is a real gap, not an edge case.

**Decision.**
- Composite-key joins are in Phase 1 scope (CLAUDE.md, delivery phases).
- Until then the schema accepts a list of columns, so the validator can give one precise message: "joins on several columns (order_id, line_no) are not supported yet; composite-key joins are planned for Phase 1".
- Joining to an entity whose key has several columns gets the same "not supported yet" wording.
- Such joins stay out of the join graph, so they cause no knock-on "two join paths" errors.

## Finding 2: column expressions went to SQL unchecked

The validator accepted `where: year(customers.signup_date) = year(order_date)`. Render then failed with a raw DuckDB binder error.

**Decision.**
- `sum:`/`count:`/... bodies, `where:` and `calc:` are parsed with sqlglot and checked against an explicit allowed set: arithmetic, comparison, `in`, `between`, `is null`, boolean logic, literals, `year`, `month`, `quarter`, `date_diff`, `coalesce`, `case`. Anything else fails with "function X is not supported in column expressions". The set is `obi_formats.colexpr.FEATURES`.
- Every reference is resolved at validation time.
- Tables and columns are checked against the data files themselves: CSV headers, Parquet schemas.
- `obi validate` runs each measure once on the data. Whatever only the database can detect, such as `year()` on a text column, is translated into `file:line` naming the measure. The engine translates any remaining database error the same way, and a raw database message never reaches the user.
- `test_coverage.py` requires a DuckDB-backed test (`test_column_expressions.py`) for every feature in the allowed set.

## Finding 3: filtered measures could not use joined entities

Both the prefixed and the bare form of a `where:` that referenced `customers.signup_date` failed: the filter only saw the fact table. "Revenue from new customers" was inexpressible, and that class of measure (a filter on an attribute of a joined entity) is roughly half of real-world measures.

**Decision.**
- `where:` names **model dimensions**, bare or entity-prefixed, with an optional time grain. The engine resolves and joins them exactly like chart axes, inside the fact's query: `SUM(...) FILTER (WHERE YEAR(t1.signup_date) = YEAR(t2.date))`.
- Raw columns stay only in aggregation bodies, which always read the entity's own table. A prefixed name there is an error that points to `where:`.
- A raw column in `where:` is an error with a hint. For example, `order_date` is the join column to `calendar`, so the message says to use its dimension `date`. The LLM's measure was corrected to `year(signup_date) = year(date)`.
- A `where:` dimension must be reachable from the measure's entity through its joins.

## Also

- **Missing tables and columns** are validation errors, not render failures. `data/returns.csv` is now generated from `order_lines`: returned lines only, each return after its order, refunds never above the line's net revenue.
- **A model pasted into a dashboard file** gives one error ("this file looks like a model, not a dashboard"), not one per property. Links from other dashboards to it are not reported again.

## Consequences

- The LLM's model, with `returns` and `new_customer_revenue`, is the example. Its output needed `obi-formats fmt`: it had `label` after `where` and no final newline. Unlike the dashboard test, the model-edit output was not canonical on the first attempt.
- Measures are now checked in three stages: static names, the data catalog, and a live run of each measure. Validation now reads the data files, so `obi-formats validate` needs the files present to check columns. Tables whose columns cannot be read are skipped, and the project check already reports missing files.
- Composite-key joins remain the main gap for SAP-style sources until Phase 1.

## Follow-up decisions

**Static and executed validation are separate.**
- `obi validate` is static: schemas, references, expression parsing, and columns from `catalog.json`. That file is the committed data-source catalogue, written by `obi catalog`. It holds table and column names with types, which is schema and never data, so it belongs in Git. Static validation needs no database access, and CI runs it.
- `obi validate --execute` additionally compares `catalog.json` with the live source and runs every measure once on the data. A stale catalogue is an error naming each change ("table `returns` changed type of `refund_amount`. Run `obi catalog` and commit the result").
- `obi serve` and `obi render` always validate with `--execute`, because they read the data anyway.
- A project without `catalog.json` falls back to reading local data files, with a warning to run `obi catalog`.

**Non-canonical input is not an error.** An LLM or a person may write keys in any order or leave out the final newline. Validation checks meaning, not layout. Every upload or commit path in Phase 1 runs `fmt` first, so what lands in Git is always canonical. `obi-formats fmt --check` stays available for CI on repositories that want to enforce it. The example README states the key order, so LLM output is canonical more often.

## Update: composite-key joins shipped

Composite-key joins were pulled forward from Phase 1 for `examples/erp-sales` and are done ([ADR-009](009-erp-sources-joins-currency-fiscal.md)): `joins: { order_items: [AUBEL, AUPOS] }`. The "not supported yet" message is gone. A join with the wrong number of columns now gets an error that spells out the expected list. The sales-demo `returns` entity is linked to `order_lines` on `[order_id, line_no]`.
