# ADR-009: ERP sources: composite and expression joins, currency conversion, fiscal calendar, lookups

- Status: Accepted. Amends ADR-001 (join paths) and pulls composite-key joins forward from Phase 1 (ADR-008). Revised once after review: each entity now converts at its own date, open orders are exact, route notices and `date:` routes were added, and lookup dimensions replace the repeated text-table entities.
- Date: 2026-11-05

## Context

`examples/erp-sales` is SAP-style SD data:
- order headers and items (VBAK/VBAP) and billing items (VBRP), with composite document keys;
- three document currencies with monthly rates to USD (TCURR);
- a fiscal year that starts on 1 April;
- a product hierarchy whose level texts (T179T) are keyed by prefixes of one code.

The example was designed to need three things the engine did not have. Building it surfaced more.

## Decisions

### 1. Joins on several columns, and on expressions

```yaml
joins:
  calendar: FKDAT
  order_items: [AUBEL, AUPOS]          # one column per column of the target's key, in key order
```

- The engine joins with `ON a.AUBEL = b.VBELN AND a.AUPOS = b.POSNR`.
- A wrong number of columns is an error that spells out the expected list.
- A foreign key may also be a column expression of the entity's own table, such as `left(PRDHA, 5)`. `left(text, n)` is in the allowed function set and has its own DuckDB test.

### 2. The shortest join path wins, visibly (amends ADR-001)

VBRP joins the calendar directly on its billing date, and also reaches the calendar through VBAP → VBAK (order date). ADR-001 forbade any second path. Now:

- **The shortest route is used.** Routes of equal length are an error.
- **The choice is never silent.** `obi validate` prints an info line for every entity that has more than one route, naming the route chosen and the ones not taken: "billing_items reaches `calendar` by 2 routes; using the shortest, billing_items → calendar (not billing_items → order_items → orders → calendar)". When a new join changes the shortest route, a new notice appears, and a test pins this.
- **A measure can choose its date route** with `date: <date dimension>`:

  ```yaml
  billed_by_order_date_usd: { sum: NETWR, date: order_date, convert: USD, format: currency }
  ```

  The measure then reaches the calendar through the entity that owns `order_date`, on that dimension's column. Measures with different routes run as separate queries and are joined on the shown dimensions, like any two facts.

This is a narrow form of role-playing dates: one calendar per measure. It is not the general role-playing support that ADR-001 lists as a Cube trigger.

### 3. Currency conversion: a first-class block (format change)

A join on an expression (currency, month of the date) would have worked. But every converted measure would then multiply by a column of another entity, breaking the rule that aggregation bodies read only their own table (ADR-008). So conversion is a model concept:

```yaml
currency:                                # once per model
  target: USD
  rates: { table: tcurr, currency: FCURR, month: GDATU, rate: UKURS }

entities:
  order_items:
    currency: { code: document_currency, date: order_date }    # what the amounts are in, and when
    measures:
      order_value: { sum: NETWR, format: currency }                       # document currency
      order_value_usd: { sum: NETWR, convert: USD, format: currency }     # converted row by row
  billing_items:
    currency: { code: document_currency, date: billing_date }
```

That's three lines per model, one per entity, and one key per converted measure.

- **Each entity converts at its own date,** the `date` named in its currency line. Orders convert at the order date and billing at the billing date. The difference between them is real FX, so it is shown, not hidden. An earlier version converted billing at the order month's rate so that "billable − billed" would reconcile. That hid the FX effect, and was dropped.
- **The engine** joins the rate table once per fact query, on the row's currency code and the first day of its date's month. It multiplies before aggregating.
- **`obi validate --execute`** reports rows without a rate ("no exchange rate for EUR 2025-06…"), because a converted sum would silently miss them. (This check existed in the engine but wasn't wired into validation until this revision.)
- **Amounts in document currency are not additive.** A money measure on an entity with a `currency:` block and no `convert:` may only appear in a widget that also shows the currency-code dimension. Otherwise it's an error that names the converted counterpart: "a KPI shows one number, so use `order_value_usd`".
- **A calc that mixes** converted and document-currency amounts is a model error.

### 4. Open orders are exact per item: rollups (format change)

Open order value is defined per order item: (ordered quantity − billed quantity) × unit value, converted at the order date. It is not "billable USD − billed USD", which would mix exchange rates. The billed quantity per item comes from the child entity, so an entity can declare **rollups**: values added up from a child entity's rows that point to each of its rows. Rollups can be used in its aggregations like columns.

```yaml
order_items:
  rollups:
    billed_quantity: { sum: FKIMG, from: billing_items }   # billing_items must join order_items
  measures:
    open_order_value_usd:
      sum: (KWMENG - billed_quantity) * NETWR / KWMENG
      where: rejection_reason is null
      convert: USD
      format: currency
```

The engine pre-aggregates the child per parent key, using the child's join columns, and joins that once. So there is no fan-out, and an item with no billing counts 0. A fully billed item has exactly 0 open value, whatever the exchange rate did in between, and a test pins this.

### 5. Lookup dimensions (format change)

The three T179T levels had first been three entities over the same table, 17 lines with blank lines. A dimension can now take its value from another table:

```yaml
product_line: { lookup: t179t.VTEXT, key: PRODH, match: "left(PRDHA, 5)" }
```

- `match` is an expression on this entity's columns that equals the lookup table's `key`.
- The engine adds one LEFT JOIN per lookup dimension used.
- `obi validate --execute` checks that the lookup key is unique. SAP text tables are often keyed by code *and* language, and a duplicate key would multiply rows.
- The key is called `match`, not `on` as first sketched, because `on` is a YAML 1.1 boolean (ADR-002).

### 6. Fiscal calendar: fiscal grains and presets from the calendar table (format change)

```yaml
date:
  column: DATUM
  type: date
  fiscal: { year: FISCAL_YEAR, period: FISCAL_PERIOD }
```

- **Grains** `date.fiscal_year` and `date.fiscal_period` read these columns and are never computed. Labels look like `FY2025` and `FY2025 P07`, which also sort correctly.
- **Date filters** gain the presets `this_fiscal_year`, `last_fiscal_year`, `fiscal_year_to_date`, `this_fiscal_period` and `last_fiscal_period`. They're resolved by querying the calendar table, and are valid only on a dimension with `fiscal:`.
- **`compare: previous_year`** on a fiscal filter maps each end of the range to the same fiscal period one fiscal year earlier, keeping its offset in the period. That works for 4-4-5 calendars and for a calendar table that ends mid-year. The first implementation counted the periods in the current year instead. The example's calendar ends in December, so that gave 9 and compared against the wrong range (+5.9% instead of +15.2%); a test now pins the correct behaviour.
- **`compare: previous_period`** moves back by the number of fiscal periods the range covers.

### 7. Default labels

`order_value_usd` reads "Order value USD" and `billed_pct` reads "Billed %", without a `label:`.

## Consequences

- `erp.model.yaml` is 111 lines (101 non-blank), down from 123: 8 entities, 15 measures, 17 dimensions, 1 rollup and 3 hierarchies. Lookups saved 15 lines. Billing's own date, the rollup and the `date:`-routed measure added 4.
- "Open orders" ignores the period filter on its dashboard page, because the open order book is not a period quantity.
- Rollups, lookups and `date:` routes are small, targeted versions of what a full semantic layer offers (child aggregates, text joins, role-playing dimensions). If more of each is needed, that's evidence for the Cube re-evaluation (ADR-001).
