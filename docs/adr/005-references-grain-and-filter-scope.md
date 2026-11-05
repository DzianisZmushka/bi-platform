# ADR-005: References, grain and filter scope

- Status: Accepted. Amends ADR-002 §4 (flat namespace) and §6 (expressions).
- Date: 2026-11-05

## Context

Writing the validator raised three questions:
- What happens when two entities have a field with the same name?
- What happens when a dashboard filter cannot reach a measure? Monthly regional targets have no channel, so a channel filter would compare channel revenue against the full target.
- What is `filters.region` inside an expression when a filter can hold several values?

## Decision

**1. Flat namespace by default, entity prefix when needed.** A bare name (`segment`) resolves across the whole model. An optional prefix (`customers.segment`, `customers.signup_date.quarter`) is always accepted. It is *required* only where the bare name is ambiguous, and then the validator lists the candidates:

> `country` is ambiguous in model `sales`; write one of `customers.country`, `regions.country`

The ambiguity check considers only the kinds a property accepts. `measure: x` ignores a dimension `x`.

**2. Grain.** Entities and measures may declare `grain:`, a list of the dimensions they can be sliced and filtered by, optionally with a time grain (`[date.month, region]`).
- Default for an entity: every dimension reachable through its joins, at full detail.
- Default for a measure: its entity's grain.
- Default for a `calc` measure: the intersection of the grains of the measures it uses, keeping the coarsest time grain.

The validator reports an error when:
- a widget shows a dimension outside the grain of one of its measures;
- a widget uses a time grain finer than the measure's (`date.day` on a monthly target);
- a widget's `where` names a dimension outside the grain;
- a dashboard filter that applies to the widget's page targets a dimension outside the grain of one of the widget's measures, unless the widget lists that filter in **`ignore_filters: [...]`**. The error message contains the exact `ignore_filters` line to add.

`ignore_filters` applies to the whole widget, so actuals and targets on one widget are always compared on the same basis.

**At runtime**, a widget with `ignore_filters` shows a visible marker that lists the ignored filters (for example "Not filtered by: Channel, Segment") whenever one of them is set. The renderer (Phase 0, step 6) must implement this. It is part of the format contract, not an optional styling choice.

**3. `filters.<id>` is always a list**, even for single-select filters. An empty list means "not set". It has `.label` (selected values joined for display), `.count`, `.first` (the first value, or null) and `.includes(x)`. Any other use of a bare `filters.<id>` is an error, and comparing it with `null` gets a specific fix:

> `filters.region` is a list and is never null; use `filters.region.count == 0`

The full expression language is in [docs/formats/expressions.md](../formats/expressions.md).

## Consequences

- Most models never need a prefix. A name clash shows up as an error with the fix at every place that uses the bare name, so models can grow without silent misbinding.
- Drill-across between facts (targets vs. actuals) is safe by construction. Mismatched grains cannot reach the renderer silently.
- `ignore_filters` adds lines to target widgets (8 lines in `commercial-review`). That cost is accepted because the alternative is a misleading number.
- Grain is metadata the thin engine needs anyway (ADR-001). It is also one of the Cube re-evaluation criteria (hierarchies and level-aware aggregation).
