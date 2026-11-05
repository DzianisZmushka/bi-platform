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

- `value` is the number being drawn: the KPI value, the bar, the cell.
- `row.<field>` is any field the widget shows. The field name is the model name, without entity prefix or time grain.
- `user.<attribute>` is a user attribute supplied at runtime.

## Filters

`filters.<id>` is always a list. It is empty when the filter is not set, and that holds for single-select filters too.

| | |
|---|---|
| `filters.region.label` | `EMEA, APAC`, or empty text |
| `filters.region.count` | number of selected values |
| `filters.region.first` | first selected value, or null |
| `filters.region.includes('EMEA')` | true or false |

## Colors

A colour is a theme token: `ok`, `warning`, `danger`, `neutral`, `accent`. The validator checks every literal a colour expression can return:

```yaml
color: "${value < 0.95 ? 'danger' : value < 1 ? 'warning' : 'ok'}"
```
