# Expressions

Properties such as `color`, `visible`, `title`, `content` and `on_click.set` accept `${...}`. Implementation: [`obi_formats/expr.py`](../../packages/formats/src/obi_formats/expr.py). Decisions: [ADR-002 §6](../adr/002-artifact-format-conventions.md), [ADR-005 §3](../adr/005-references-grain-and-filter-scope.md).

A value that is exactly one `${...}` evaluates to a typed value (number, boolean, text, null). Text with `${...}` inside it is a template, and each expression is converted to text: null becomes empty, and a filter list becomes its `.label`.

## Grammar

| | |
|---|---|
| literals | `12`, `0.95`, `'text'` or `"text"`, `true`, `false`, `null` |
| names | `value`, `row`, `user`, `filters`, depending on the property (below) |
| member | `row.net_revenue`, `user.region`, `filters.region.label` |
| arithmetic | `+ - * / %`. `+` joins text when either side is text |
| comparison | `== != < <= > >=`. Comparisons cannot be chained |
| logic | `and`, `or`, `not` (`&&`, `\|\|` and `!` are rejected, with a hint) |
| choice | `test ? a : b`, right-associative |
| method | only `filters.<id>.includes(x)` |

There are no functions, loops or assignments. Null propagates through arithmetic, and division by zero gives null. A comparison involving null with `<`, `<=`, `>` or `>=` is false.

## Names available per property

| Property | `value` | `row` | `filters` | `user` |
|---|---|---|---|---|
| `color` on kpi and chart, `cells.<field>.color` | yes | yes | yes | yes |
| `on_click.set` on kpi, chart, table | | yes | yes | yes |
| `on_click.set` on button | | | yes | yes |
| `title`, `content`, `visible` | | | yes | yes |
| `content` of record text | | yes | yes | yes |
| form and sheet `default`, field `visible` | | | yes | yes |
| column `default` in a model | | | | yes |
| workflow transition `by` | | | | yes |

- `value` is the number being drawn: the KPI value, the bar, the cell.
- `row.<field>` is any field the widget shows. The field name is the model name, without entity prefix or time grain.
- `user.<attribute>` is a user attribute supplied at runtime. Until the Phase 2 runtime, `obi serve --user <name> --user-attr role=<role>` supplies it: `user.name` and `user.role`.

## Filters

`filters.<id>` is always a list. It is empty when the filter is not set, and that holds for single-select filters too.

| | |
|---|---|
| `filters.region.label` | `EMEA, APAC`, or empty text |
| `filters.region.count` | number of selected values |
| `filters.region.first` | first selected value, or null |
| `filters.region.includes('EMEA')` | true or false |

A date filter holds a range, not a list of values:

| | |
|---|---|
| `filters.period.start` | first day of the range, or null when the filter is not set |
| `filters.period.end` | last day of the range, or null |
| `filters.period.label` | the selection as shown, such as `This year` |

`.first` and `.count` on a date filter are validator errors that point to `.start` and `.end`, and `.start`/`.end` on any other filter is an error too. A form default such as `month: "${filters.period.start}"` puts the month of the dashboard's period into a new comment.

## Colors

A colour is a theme token: `ok`, `warning`, `danger`, `neutral`, `accent`. The validator checks every literal a colour expression can return:

```yaml
color: "${value < 0.95 ? 'danger' : value < 1 ? 'warning' : 'ok'}"
```

## Column expressions in models

Measures use a second, SQL-like language. It is parsed with sqlglot and checked before anything reaches the
database ([ADR-008](../adr/008-findings-of-the-llm-model-edit-test.md), implementation
[`obi_formats/colexpr.py`](../../packages/formats/src/obi_formats/colexpr.py)). A name means something
different in each place:

| Where | Names refer to | Example |
|---|---|---|
| `sum:`, `count:`, `count_distinct:`, `avg:`, `min:`, `max:` | columns of the entity's own table, never prefixed, and the entity's `rollups` | `quantity * unit_price * (1 - discount)`, `(KWMENG - billed_quantity) * NETWR / KWMENG` |
| join foreign keys, lookup `match`, rollup bodies | columns of one table, never prefixed | `left(PRDHA, 5)`, `FKIMG` |
| `where:` | model dimensions, bare or entity-prefixed, optionally with a time grain, from any entity reachable through joins | `segment = 'Enterprise'`, `year(signup_date) = year(date)`, `date.month = '2026-03-01'` |
| `calc:` | measures | `net_revenue / orders` |

**Allowed:**

| Feature | Syntax |
|---|---|
| arithmetic | `+ - * / %`, unary `-`, parentheses |
| comparison | `= == != <> < <= > >=`, `in (...)`, `between a and b`, `is null`, `is not null` |
| boolean | `and`, `or`, `not`, `true`, `false` |
| literals | numbers, `'text'`, `null`. A date is a text literal such as `'2026-03-01'` |
| functions | `year(x)`, `month(x)`, `quarter(x)`, `day(x)`, `date_diff('day'\|'week'\|'month'\|'quarter'\|'year', start, end)`, `coalesce(a, b, ...)`, `left(text, n)`, `length(text)`, `case when ... then ... else ... end` |

Anything else is a validation error: "function `upper` is not supported in column expressions". Every name is
resolved at validation time, with the same "did you mean" help as dashboard references. Every table and column
is also checked against the data files. `obi validate` then runs each measure once on the data, so a type
problem (`year()` on a text column) is reported as `file:line` naming the measure, never as a database error.
