# erp-sales

SAP-style order-to-cash data:
- sales orders (VBAK/VBAP) and billing (VBRP) with composite document keys;
- three document currencies with monthly rates to USD (TCURR);
- a fiscal year starting 1 April;
- a product hierarchy keyed by code prefixes (T179T).

Regenerate the data with `uv run python examples/erp-sales/data/generate.py`.

To add a source table (for example LIPS for deliveries): add the CSV under `data/`, register it in `project.yaml` under `tables`, and run `uv run obi catalog examples/erp-sales`.

The model hides the technical column names. Its decisions are in [ADR-009](../../docs/adr/009-erp-sources-joins-currency-fiscal.md). The vocabulary is the same as [sales-demo](../sales-demo/README.md), plus:

| Need | Format |
|---|---|
| billing item → order item on two columns | `joins: { order_items: [AUBEL, AUPOS] }` |
| text table keyed by a code prefix | `product_line: { lookup: t179t.VTEXT, key: PRODH, match: "left(PRDHA, 5)" }` |
| billed or delivered quantity per order item | `rollups: { billed_quantity: { sum: FKIMG, from: billing_items } }`. Rollup names are local to their entity |
| billing counted in the order's period | `date: order_date` on the measure |
| amounts in document currency | model `currency: { target, rates }`, entity `currency: { code, date }` (each entity converts at its own date), measure `convert: USD` |
| fiscal periods | `fiscal: { year, period }` on the date dimension; `date.fiscal_period`; presets such as `fiscal_year_to_date` |

`order_value` is in document currency, so it may only appear in a widget that also shows `document_currency`. Use `order_value_usd` everywhere else.

Orders convert at the order date and billing at the billing date. `billable_value_usd − billed_value_usd` therefore includes FX differences. `open_order_value_usd` is computed per item from quantities, so it is exact.

```bash
uv run obi validate examples/erp-sales --execute
uv run obi serve examples/erp-sales
```
