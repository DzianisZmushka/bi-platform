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
| amounts in document currency | model `currency: { target: USD, rates: { table, currency, month, rate } }` (`rate` × amount = amount in `target`), entity `currency: { code, date }` (each entity converts at its own date), measure `convert: USD` |
| fiscal periods | `fiscal: { year, period }` on the date dimension; `date.fiscal_period`; presets such as `fiscal_year_to_date` |
| a note per order item | `order_notes: { writable: true, per: order_items, columns: ... }`. The key is `VBELN`, `POSNR`; order items join it automatically (LEFT, so items without a note still show) |
| planned shipments per sales org and week | writable `shipment_plans` (`VKORG`, `week_start`) seeded from `data/shipment_plans.csv`, edited in the `shipment-plan` sheet (no delete) and charted against deliveries by `date.week` on the page `shipment_plan`. Weeks are Monday-based, so the week of 2026-03-30 falls in the old fiscal year (see the ADR-009 follow-up) |
| draft → submitted → approved | `workflow: { column: status, transitions: { approve: { from: submitted, to: approved, by: "${user.roles.includes('sales_manager')}" } } }` |

`order_value` is in document currency, so it may only appear in a widget that also shows `document_currency`. Use `order_value_usd` everywhere else.

Orders convert at the order date and billing at the billing date. `billable_value_usd − billed_value_usd` therefore includes FX differences. `open_order_value_usd` is computed per item from quantities, so it is exact.

Click a row in **Largest open order items** to open the `order-note` form for that item. Save it as a draft, then submit it. The note and its status appear in the table. A sales manager then approves or reopens it:

```bash
uv run obi validate examples/erp-sales --execute
uv run obi serve examples/erp-sales --user bob --role sales_rep --attr sales_orgs=DE01
uv run obi serve examples/erp-sales --user mia --role sales_manager --attr sales_orgs=DE01,PL01
```

Run one server at a time: DuckDB lets one process open `.obi/erp-sales.duckdb`.

**Who sees what.** `access:` sits on `sales_orgs`, the smallest master-data entity every fact joins, not on the facts: `access: { where: sales_org_code in user.sales_orgs }` ([access.md](../../docs/formats/access.md)).
- Orders, items, deliveries, billing, notes (through their order item) and the shipment plan all reach it, so one line limits them all.
- The attribute holds VKORG keys (`DE01`), compared with the key dimension `sales_org_code`.
- `--role sales_rep --attr sales_orgs=DE01` sees only Germany's numbers, and may write notes only on DE01 items.
- An `auditor` never writes, whatever other roles they hold (`deny`).
- Cost (`order_items.WAVWR`) is readable only by managers and admins, so others see "No access" on margin.
