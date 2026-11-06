# ADR-010: Snapshot measures

- Status: Proposed. Design only; to be implemented in Phase 1.
- Date: 2026-11-06

## Context

Every measure today is a **flow**: a sum, count or average over the rows of a period. "Order value in FY2026 P03" adds up the orders placed in P03.

Some measures are a **state at a date**: open orders, shipped-not-billed, inventory on hand, headcount, receivables. Such a value is not a sum over a period. "Open orders in P03" means the open order book *as of the end of P03*, not the total of something that happened during P03.

The third LLM test made this concrete. `shipped_not_billed_usd` with `compare: previous_year` showed +107%. The number was arithmetically correct and meaningless: last year's orders have long since been billed, so the comparison measured how much time had passed. The open-orders page already works around this with `ignore_filters: [period]`. That workaround hides the date instead of using it.

## Decision

A measure gets an optional `kind:`. `flow` is the default and is never written; `snapshot` marks a state at a date:

```yaml
open_order_value_usd:
  sum: (KWMENG - billed_quantity) * NETWR / KWMENG
  kind: snapshot
  convert: USD
  format: currency
```

A snapshot measure is evaluated **as of a date**. For a given as-of date, only the rows that existed by then count, and only the events that had happened by then count. For open orders, that means items ordered on or before the date, minus quantities billed on or before it. The model must say which date governs each part. The intended shape is that `rollups` take the child's date (billing date), and the measure's own entity uses its date (order date); the exact syntax is a Phase 1 task.

Its behaviour with the other dashboard features:

| Situation | Flow measure (today) | Snapshot measure |
|---|---|---|
| Period filter `[a, b]` | rows with dates in `[a, b]` | the state **as of `b`**, the end of the period |
| `compare: previous_year` | the same period a year earlier | the state as of the **same date a year earlier** (for a fiscal filter, the same fiscal period end a year earlier, from the calendar table) |
| `compare: previous_period` | the preceding period of equal length | the state as of the end of the preceding period |
| Chart with a time grain on an axis (`x: date.fiscal_period`) | sum per period | **validator error** if the chart would add snapshots across the grain. A snapshot is shown per period as its end-of-period value, never summed over days |
| Table with time rows | sum per row | each row shows the **end-of-period value** |
| Totals row in a table over time | sum of rows | the value as of the **last** period's end, not the sum of rows |
| Non-time dimensions (sales org, product) | additive | additive. Snapshots of different items add up at one date |
| KPI without a period filter | all rows | as of today, or the dashboard's as-of date |

Validator errors to specify in Phase 1 (exact messages written before implementation, per CLAUDE.md):
- a snapshot measure summed over a time grain in a chart;
- a `calc` that mixes flow and snapshot measures, except a ratio whose meaning is defined (for example days sales outstanding);
- `kind: snapshot` on an entity with no date dimension to evaluate "as of".

## Until then

- Snapshot-like measures are marked in their `description`. Dashboards use them with `ignore_filters: [period]` and no `compare`, so they show the current state honestly.
- The `shipped_not_billed` KPI on the order-to-cash dashboard is changed to that form.
- Nothing about `kind:` is in the schema yet. A schema property without runtime behaviour would violate the "accepted means handled" rule (ADR-007, CLAUDE.md).

## Consequences

- Snapshot evaluation needs a date per row and per child event, so models of stock-like facts must declare them. That is more modelling, in exchange for numbers that mean what they say.
- As-of evaluation is costlier than a period sum (one evaluation per period end on a time axis). Pre-aggregated snapshot tables (daily inventory) are the usual answer, and are among the Cube re-evaluation criteria in ADR-001.
- Semi-additivity (additive across items, not across time) is the textbook name for this. The table above is our definition of it for this format.
