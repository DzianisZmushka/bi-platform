# ADR-002: Artifact format conventions

- Status: Accepted. §4 and §6 amended by [ADR-005](005-references-grain-and-filter-scope.md); the y/n key exception is in [ADR-006](006-serializer-and-comment-preservation.md).
- Date: 2026-11-05

## Context

Every artifact has to be readable by a person, editable by an LLM as a whole file, and editable by a builder with zero diff. Each format could invent its own style. If the conventions differ, every format costs readers and LLMs extra effort, so they are fixed once here.

## Decision

**1. YAML on disk, JSON internally, same data model.** The parsed YAML *is* the JSON document: no renaming or reshaping between the two. JSON Schema validates it directly. The allowed YAML is a strict subset: no anchors or aliases, no tags, one document per file, string keys only, YAML 1.2 scalars (`yes`/`no` are strings). Many tools still parse YAML 1.1, so no format may define a key or enum value that YAML 1.1 reads as a boolean or null: `on`, `off`, `yes`, `no`, `true`, `false`, `null`, `~` (the chart key `y` is the one exception, ADR-006). (PyYAML reads `on:` as `True:`. The first draft of the model's join syntax hit this.)

**2. The kind is a key, not a `type:` field.** An element whose kind matters puts the kind first as a key, and the value of that key is the element's id:

```yaml
- kpi: revenue          # a KPI widget with id "revenue"
  measure: net_revenue
- page: overview
- filter: region
```

Measures follow the same pattern, but the aggregation is the key and its value is the expression: `net_revenue: { sum: quantity * unit_price }`, `aov: { calc: net_revenue / orders }`. A reader sees what an element is in its first token. One line per element is saved compared with `id:` + `type:`.

**3. Ids.** Artifact ids (file and folder names) are `kebab-case`. Element ids (widgets, containers, pages, filters, measures, dimensions, entities) are `snake_case`, matching `[a-z][a-z0-9_]*`, so they work as names inside expressions (`row.net_revenue`). Element ids are unique within their artifact, and every container has one too. Ids are never generated or rewritten by tools.

**4. References are names only.** Dashboards reference model measures and dimensions by name. A model's measures and dimensions share one flat namespace that is unique across the model, so a reference never needs an entity prefix. Time grain is a suffix: `order_date.month`. Navigation targets are `page_id` within the same dashboard, or `dashboard-id/page_id` across dashboards.

**5. Defaults are never written.** This includes labels and titles. They derive from ids (`net_revenue` → "Net revenue"). A chart's default title is "<y label> by <x label>". An element whose properties are *all* defaults is written as a key with an empty value:

```yaml
dimensions:
  channel:                  # string column "channel", label "Channel"
  segment: { label: Customer segment }
```

**6. Expressions, not programs.** A property that accepts an expression takes a literal or a `${...}` string. The language has literals, comparisons, `and`/`or`/`not`, arithmetic, the ternary `a ? b : c`, string concatenation, `null`, and member access on `value`, `row`, `user`, `filters`. It has no function definitions, loops or assignment. A separate spec will define it before the validator is built.

**7. No styling in artifacts.** Colours in expressions are theme tokens: `ok`, `warning`, `danger`, `neutral`, `accent`. Layout is a tree of semantic containers: `stack`, `row`, `grid`, `tabs`/`tab`, `card`, `section`. The renderer decides pixels. A whitelisted `style:` escape hatch will be added only when an example needs it.

## Consequences

- One rule set to learn. An LLM that has seen one artifact can read the rest.
- JSON Schema has to discriminate list items by which kind key is present (`oneOf` + `required`). Builders and the validator normalise to `{kind, id, ...}` in memory only, never on disk.
- The flat model namespace means models must avoid name clashes (`customer_region`, not `region` on two entities). The validator reports a clash with both locations.
