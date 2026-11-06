"""Validator error cases with exact message text (CLAUDE.md: at least three per format).

Each test takes the sales-demo example, breaks one thing, and checks the full list
of messages for the broken file.
"""

from pathlib import Path

from obi_formats.validate import validate_project

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "examples" / "sales-demo"
MODEL = "model/sales.model.yaml"
OVERVIEW = "dashboards/revenue-overview/dashboard.yaml"
REVIEW = "dashboards/commercial-review/dashboard.yaml"


def edit(rel: str, old: str, new: str) -> dict[str, str]:
    text = (DEMO / rel).read_text(encoding="utf-8")
    assert text.count(old) == 1, f"edit anchor must be unique in {rel}: {old!r}"
    return {rel: text.replace(old, new)}


def messages(overrides: dict[str, str], file: str | None = None) -> list[str]:
    issues = validate_project(DEMO, overrides)
    # Info lines (route notices) are tested separately; these lists hold errors and warnings.
    return [i.text for i in issues if (file is None or i.file == file) and i.severity != "info"]


def lines(overrides: dict[str, str]) -> list[int]:
    return [i.line for i in validate_project(DEMO, overrides) if i.severity != "info"]


def test_example_project_is_valid():
    assert messages({}) == []


# --------------------------------------------------------------------------- project


def test_project_unknown_default_connection():
    o = edit("project.yaml", "default_connection: local", "default_connection: locl")
    assert messages(o) == [
        "project `sales-demo`: default_connection `locl` is not one of the connections; did you mean `local`?",
    ]


def test_project_missing_table_file():
    o = edit("project.yaml", "regions: data/regions.csv", "regions: data/region.csv")
    assert messages(o) == [
        "project `sales-demo`, connection `local`: table `regions` points to `data/region.csv`, "
        "which does not exist",
    ]


def test_project_bad_currency_and_anchor():
    o = edit("project.yaml", "currency: USD", "currency: usd")
    assert messages(o) == ["project `sales-demo`: `usd` must be an ISO 4217 code such as `USD`"]
    o = edit("project.yaml", "currency: USD", "currency: &cur USD")
    assert messages(o) == [
        "project `project`: anchors and aliases (&, *) are not allowed; repeat the value",
    ]


# --------------------------------------------------------------------------- model


def test_model_calc_refers_to_unknown_measure():
    o = edit(MODEL, "aov: { calc: net_revenue / orders,", "aov: { calc: net_revenu / orders,")
    assert messages(o, MODEL) == [
        "model `sales`, measure `aov`: measure `net_revenu` not found in model `sales`; "
        "did you mean `net_revenue`?",
    ]


def test_model_measure_with_two_kinds():
    o = edit(MODEL, "units: { sum: quantity, format: integer }",
             "units: { sum: quantity, count: quantity, format: integer }")
    assert messages(o, MODEL) == ["model `sales`, measure `units`: has both `sum` and `count`; keep exactly one"]


def test_model_two_join_paths():
    # Two routes of the same length to `regions`: through customers and through products.
    o = edit(MODEL, "    key: product_id\n", "    key: product_id\n    joins: { regions: brand }\n")
    assert messages(o, MODEL) == [
        "model `sales`, entity `order_lines`: reaches `regions` through two join paths of the same length: "
        "order_lines → customers → regions and order_lines → products → regions; keep one (ADR-009)",
        "model `sales`, entity `returns`: reaches `regions` through two join paths of the same length: "
        "returns → order_lines → customers → regions and returns → order_lines → products → regions; "
        "keep one (ADR-009)",
    ]
    # A shorter route is not ambiguous: the direct join wins (ADR-009).
    o = edit(MODEL, "joins: { calendar: order_date, customers: customer_id, products: product_id }",
             "joins: { calendar: order_date, customers: customer_id, products: product_id, "
             "regions: region_id }")
    assert messages(o, MODEL) == [
        "model `sales`, entity `order_lines`: column `region_id` not found in table `order_lines`",
    ]


def test_model_unknown_property_and_unreachable_grain():
    o = edit(MODEL, "order_id: { label: Order }", "order_id: { lable: Order }")
    assert messages(o, MODEL) == [
        "model `sales`, dimension `order_id`: unknown property `lable`; did you mean `label`?",
    ]
    o = edit(MODEL, "grain: [date.month, region]", "grain: [date.month, channel]")
    assert "model `sales`, entity `targets`: grain: dimension `channel` is not reachable from entity " \
           "`targets` through its joins" in messages(o, MODEL)


def test_ambiguous_bare_name_lists_candidates():
    # A second `country` (on regions) makes the bare name ambiguous everywhere it is used.
    o = edit(MODEL, "      region: { column: region_name }\n",
             "      region: { column: region_name }\n      country: { column: region_name }\n")
    assert messages(o, MODEL) == [
        "model `sales`, hierarchy `geography`: `country` is ambiguous in model `sales`; "
        "write one of `customers.country`, `regions.country`",
    ]
    assert messages(o, REVIEW)[0] == (
        "dashboard `commercial-review`, filter `country`: `country` is ambiguous in model `sales`; "
        "write one of `customers.country`, `regions.country`")


# --------------------------------------------------------------------------- dashboard


def test_dashboard_unknown_measure_matches_claude_md_example():
    o = edit(OVERVIEW, "          - kpi: revenue\n            measure: net_revenue",
             "          - kpi: revenue\n            measure: net_rev")
    assert messages(o) == [
        "dashboard `revenue-overview`, widget `revenue`: measure `net_rev` not found in model `sales`; "
        "did you mean `net_revenue`?",
    ]
    assert lines(o) == [21]


def test_dashboard_kind_is_detected_first_and_errors_name_the_property():
    o = edit(OVERVIEW, "            mark: line\n", "            mark: lines\n            stacked: true\n")
    assert messages(o) == [
        "dashboard `revenue-overview`, widget `revenue_trend`: `mark` must be one of line, bar, area, "
        "point, arc; got `lines`; did you mean `line`?",
        "dashboard `revenue-overview`, widget `revenue_trend`: unknown property `stacked`; "
        "did you mean `stack`?",
    ]


def test_dashboard_unknown_kind_and_misplaced_tab():
    o = edit(OVERVIEW, "          - kpi: basket\n", "          - kip: basket\n")
    assert messages(o) == [
        "dashboard `revenue-overview`, container `headline`: unknown element kind `kip`; the first key "
        "must be one of row, grid, tabs, card, section, kpi, chart, table, text, button; "
        "did you mean `kpi`?",
    ]
    o = edit(OVERVIEW, "      - row: headline\n", "      - tab: headline\n")
    assert messages(o) == [
        "dashboard `revenue-overview`, page `overview`: `tab` is not allowed in `items` here; use one of "
        "row, grid, tabs, card, section, kpi, chart, table, text, button",
    ]


def test_dashboard_filter_outside_measure_grain_requires_ignore_filters():
    o = edit(REVIEW, "            sparkline: date.month\n            ignore_filters: [channel, segment]\n",
             "            sparkline: date.month\n")
    assert messages(o) == [
        "dashboard `commercial-review`, widget `revenue`: filters `channel`, `segment` do not apply to "
        "measure `revenue_target` (grain: date.month, region); add `ignore_filters: [channel, segment]` "
        "to the widget, or remove the measure",
    ]


def test_dashboard_dimension_outside_or_finer_than_grain():
    o = edit(REVIEW, "                rows: [region]\n", "                rows: [region, country]\n")
    assert messages(o) == [
        "dashboard `commercial-review`, widget `lowest_regions`: dimension `country` is outside the grain "
        "of measure `revenue_target` (grain: date.month, region); remove one of them",
        "dashboard `commercial-review`, widget `lowest_regions`: dimension `country` is outside the grain "
        "of measure `target_gap` (grain: date.month, region); remove one of them",
        "dashboard `commercial-review`, widget `lowest_regions`: dimension `country` is outside the grain "
        "of measure `target_attainment` (grain: date.month, region); remove one of them",
    ]
    o = edit(REVIEW, "            x: date.month\n            y: [net_revenue, revenue_target]",
             "            x: date.day\n            y: [net_revenue, revenue_target]")
    assert messages(o) == [
        "dashboard `commercial-review`, widget `revenue_vs_target`: `date.day` is finer than the grain of "
        "measure `revenue_target` (grain: date.month, region); use `date.month` or coarser",
    ]


def test_dashboard_expression_errors():
    o = edit(REVIEW, 'visible: "${filters.region.count == 0}"', 'visible: "${filters.region == null}"')
    assert messages(o) == [
        "dashboard `commercial-review`, widget `region_hint`: `visible`: `filters.region` is a list and "
        "is never null; use `filters.region.count == 0`",
    ]
    o = edit(OVERVIEW, "{ color: \"${value < 0.3 ? 'danger' : 'ok'}\" }",
             "{ color: \"${row.margin < 0.3 && value > 0 ? 'dangr' : 'ok'}\" }")
    assert messages(o) == [
        "dashboard `revenue-overview`, widget `top_customers`: `cells.margin_pct.color`: invalid "
        "expression: use `and` instead of `&&`",
    ]
    o = edit(OVERVIEW, "{ color: \"${value < 0.3 ? 'danger' : 'ok'}\" }",
             "{ color: \"${row.margin < 0.3 ? 'dangr' : filters.regin.label}\" }")
    assert messages(o) == [
        "dashboard `revenue-overview`, widget `top_customers`: `cells.margin_pct.color`: `row.margin` is not "
        "a field of this widget; available: customer_name, margin_pct, net_revenue, orders, region, "
        "segment; did you mean `margin_pct`?",
        "dashboard `revenue-overview`, widget `top_customers`: `cells.margin_pct.color`: `filters.regin`: "
        "no filter `regin` in this dashboard; did you mean `region`?",
        "dashboard `revenue-overview`, widget `top_customers`: `cells.margin_pct.color`: `dangr` is not a "
        "theme color; use one of ok, warning, danger, neutral, accent; did you mean `danger`?",
    ]


def test_dashboard_navigation_and_duplicate_ids():
    o = edit(REVIEW, "on_click: { navigate: revenue-overview/overview }",
             "on_click: { navigate: revenue-overview/summary }")
    assert messages(o) == [
        "dashboard `commercial-review`, widget `open_overview`: `on_click`: no page `summary` in "
        "dashboard `revenue-overview`",
    ]
    o = edit(REVIEW, "              set: { region: \"${row.region}\" }\n            color",
             "              set: { regoin: \"${row.region}\" }\n            color")
    assert messages(o) == [
        "dashboard `commercial-review`, widget `attainment_by_region`: `on_click.set`: `regoin` is not a "
        "filter of this dashboard; did you mean `region`?",
    ]
    o = edit(OVERVIEW, "          - kpi: basket\n", "          - kpi: revenue\n")
    assert messages(o) == [
        "dashboard `revenue-overview`, widget `revenue`: id `revenue` is already used by widget `revenue` "
        "on line 20; ids must be unique within a dashboard",
    ]


def test_unknown_chart_axis_reports_only_the_name():
    o = edit(OVERVIEW, "            y: net_revenue\n            series: channel",
             "            y: net_rev\n            series: channel")
    assert messages(o) == [
        "dashboard `revenue-overview`, widget `revenue_trend`: field `net_rev` not found in model `sales`; "
        "did you mean `net_revenue`?",
    ]
    o = edit(OVERVIEW, "            y: net_revenue\n            series: channel",
             "            y: segment\n            series: channel")
    assert messages(o) == ["dashboard `revenue-overview`, widget `revenue_trend`: a chart needs a measure on `x` or `y`"]


# --------------------------------------------------------------------------- column expressions (ADR-008)


def test_where_uses_dimensions_not_raw_columns():
    o = edit(MODEL, "where: year(signup_date) = year(date)", "where: year(signup_date) = year(order_date)")
    assert messages(o, MODEL) == [
        "model `sales`, measure `new_customer_revenue`: `where`: `order_date` is not a model dimension; "
        "`order_date` is the join column to `calendar`, so use its dimension `date`",
    ]
    o = edit(MODEL, "where: year(signup_date) = year(date)", "where: segmnt = 'SMB'")
    assert messages(o, MODEL) == [
        "model `sales`, measure `new_customer_revenue`: `where`: dimension `segmnt` not found in model "
        "`sales`; did you mean `segment`?",
    ]


def test_where_dimension_must_be_reachable():
    o = edit(MODEL, "revenue_target: { sum: revenue_target,",
             "revenue_target: { sum: revenue_target, where: segment = 'SMB',")
    assert messages(o, MODEL) == [
        "model `sales`, measure `revenue_target`: `where`: dimension `segment` is not reachable from entity "
        "`targets` through its joins",
    ]


def test_unsupported_function_and_prefixed_aggregation():
    o = edit(MODEL, "where: year(signup_date) = year(date)", "where: upper(segment) = 'SMB'")
    assert messages(o, MODEL) == [
        "model `sales`, measure `new_customer_revenue`: `where`: function `upper` is not supported in column "
        "expressions; use year, month, quarter, date_diff, coalesce, left, case, arithmetic, comparisons and "
        "and/or/not",
    ]
    o = edit(MODEL, "units: { sum: quantity, format: integer }",
             "units: { sum: products.list_price, format: integer }")
    assert messages(o, MODEL) == [
        "model `sales`, measure `units`: `sum` uses `products.list_price`: aggregations read columns of the "
        "entity's own table only; to filter on a joined dimension use `where:`",
    ]


def test_composite_key_join():
    # The example links returns to order lines on both key columns (ADR-009).
    o = edit(MODEL, "order_lines: [order_id, line_no] }", "order_lines: [order_id] }")
    assert messages(o, MODEL) == [
        "model `sales`, entity `returns`: the key of `order_lines` has 2 columns (order_id, line_no), but the "
        "join gives 1; write `order_lines: [<column for order_id>, <column for line_no>]`",
    ]


def test_tables_and_columns_must_exist_in_the_data():
    o = edit(MODEL, "    table: returns\n", "    table: return_lines\n")
    assert messages(o, MODEL) == [
        "model `sales`, entity `returns`: table `return_lines` not found in connection `local`; did you mean "
        "`order_lines`? If it is new, add it under `tables` in project.yaml.",
    ]
    o = edit(MODEL, "refunds: { sum: refund_amount,", "refunds: { sum: refund_amt,")
    assert messages(o, MODEL) == [
        "model `sales`, measure `refunds`: column `refund_amt` not found in table `returns`; "
        "did you mean `refund_amount`?",
    ]
    o = edit(MODEL, "      reason: { label: Return reason }", "      reasons: { label: Return reason }")
    assert "model `sales`, dimension `reasons`: column `reasons` not found in table `returns`; " \
           "did you mean `reason`?" in messages(o, MODEL)


def test_model_pasted_into_a_dashboard_file_gives_one_error():
    model_text = (DEMO / MODEL).read_text(encoding="utf-8")
    assert messages({OVERVIEW: model_text}) == [
        "dashboard `revenue-overview`: this file looks like a model, not a dashboard (it has `entities` or "
        "`hierarchies`); models live in `model/<id>.model.yaml`",
    ]


# --------------------------------------------------------------------------- erp-sales (ADR-009)

ERP = ROOT / "examples" / "erp-sales"
ERP_MODEL = "model/erp.model.yaml"
O2C = "dashboards/order-to-cash/dashboard.yaml"


def erp_messages(rel: str, old: str, new: str) -> list[str]:
    text = (ERP / rel).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    return [i.text for i in validate_project(ERP, {rel: text.replace(old, new)}) if i.severity != "info"]


def test_erp_example_is_valid():
    assert [i.text for i in validate_project(ERP) if i.severity != "info"] == []


def test_document_currency_cannot_be_added_across_currencies():
    assert erp_messages(O2C, "            measure: order_value_usd\n            compare: previous_year\n          - kpi: billed",
                        "            measure: order_value\n            compare: previous_year\n          - kpi: billed") == [
        "dashboard `order-to-cash`, widget `ordered`: measure `order_value` is in document currency and cannot "
        "be added up across currencies; a KPI shows one number, so use `order_value_usd`",
    ]
    assert erp_messages(O2C, "            x: order_value_usd\n            y: organisation\n            sort: -order_value_usd",
                        "            x: order_value\n            y: organisation\n            sort: -order_value") == [
        "dashboard `order-to-cash`, widget `org_orders`: measure `order_value` is in document currency and cannot "
        "be added up across currencies; show `document_currency` in this widget (rows, columns or series), or "
        "use `order_value_usd`",
    ]


def test_currency_conversion_errors():
    assert erp_messages(ERP_MODEL, "cost_usd: { sum: WAVWR, convert: USD,", "cost_usd: { sum: WAVWR, convert: EUR,") == [
        "model `erp`, measure `cost_usd`: `convert: EUR`: rates convert to `USD` only; use `convert: USD`",
    ]
    assert erp_messages(ERP_MODEL, "margin: { calc: order_value_usd - cost_usd,", "margin: { calc: order_value - cost_usd,") == [
        "model `erp`, measure `margin`: mixes `cost_usd` (converted to USD) with `order_value` (document "
        "currency); use converted measures throughout",
    ]
    assert erp_messages(ERP_MODEL, "    currency: { code: document_currency, date: order_date }\n    joins: { orders",
                        "    currency: { code: document_currency, date: order_type }\n    joins: { orders") == [
        "model `erp`, entity `order_items`: `currency.date`: `order_type` must be a date dimension, not string",
    ]


def test_lookup_rollup_and_date_route_errors():
    assert erp_messages(ERP_MODEL, 'match: "left(PRDHA, 5)"', 'match: "left(materials.PRDHA, 5)"') == [
        "model `erp`, dimension `product_line`: `match` uses `materials.PRDHA`: it reads columns of one table "
        "only, without prefix",
    ]
    assert erp_messages(ERP_MODEL, "product_family: { lookup: t179t.VTEXT,", "product_family: { lookup: t179t.TEXT,") == [
        "model `erp`, dimension `product_family`: column `TEXT` not found in lookup table `t179t`; "
        "did you mean `VTEXT`?",
    ]
    assert erp_messages(ERP_MODEL, "billed_quantity: { sum: FKIMG, from: billing_items }",
                        "billed_quantity: { sum: FKIMG, from: customers }") == [
        "model `erp`, rollup `billed_quantity`: `from: customers`: `customers` must join `order_items` (a rollup "
        "adds up the rows of `customers` that point to each `order_items` row)",
    ]
    assert erp_messages(ERP_MODEL, "billed_by_order_date_usd: { sum: NETWR, date: order_date,",
                        "billed_by_order_date_usd: { sum: NETWR, date: billing_document,") == [
        "model `erp`, measure `billed_by_order_date_usd`: `date`: dimension `billing_document` not found in model "
        "`erp`; did you mean `billing_date`?",
    ]


def test_route_notice_names_the_chosen_path_and_changes_with_a_new_join():
    def infos(overrides=None):
        return [i.text for i in validate_project(ERP, overrides or {}) if i.severity == "info"]

    assert infos() == [
        "model `erp`, entity `deliveries`: reaches `calendar` by 2 routes; using the shortest, deliveries → "
        "calendar (not deliveries → order_items → orders → calendar). A measure can choose another date route "
        "with `date:`",
        "model `erp`, entity `billing_items`: reaches `calendar` by 2 routes; using the shortest, billing_items → "
        "calendar (not billing_items → order_items → orders → calendar). A measure can choose another date route "
        "with `date:`",
    ]
    # A direct join from billing items to order headers makes a new, shorter route to `orders` and
    # everything behind it. The validator says so instead of switching silently.
    text = (ERP / ERP_MODEL).read_text(encoding="utf-8")
    changed = text.replace("joins: { calendar: FKDAT, order_items: [AUBEL, AUPOS] }",
                           "joins: { calendar: FKDAT, order_items: [AUBEL, AUPOS], orders: AUBEL }")
    assert changed != text
    after = infos({ERP_MODEL: changed})
    assert ("model `erp`, entity `billing_items`: reaches `orders` by 2 routes; using the shortest, billing_items → "
            "orders (not billing_items → order_items → orders). A measure can choose another date route with "
            "`date:`") in after
    assert len(after) > 1


def test_fiscal_presets_and_grains_need_a_fiscal_calendar():
    o = edit(OVERVIEW, "default: last_12_months", "default: this_fiscal_year")
    assert messages(o) == [
        "dashboard `revenue-overview`, filter `period`: `default: this_fiscal_year` needs a fiscal calendar on "
        "dimension `date`; add `fiscal: { year: <column>, period: <column> }` to it in the model",
    ]
    o = edit(OVERVIEW, "            x: date.month", "            x: date.fiscal_period")
    assert messages(o) == [
        "dashboard `revenue-overview`, widget `revenue_trend`: `date.fiscal_period`: dimension `date` has no "
        "fiscal calendar; add `fiscal: { year: <column>, period: <column> }` to it",
    ]


def test_rollup_names_are_local_to_their_entity():
    """ADR-009: a rollup is only referenced inside its own entity's aggregations, so a measure
    elsewhere may use the same name. The LLM avoided `delivered_quantity` because the README did
    not say this; it is valid."""
    assert erp_messages(ERP_MODEL, "      delivered_quantity_total: {", "      delivered_quantity: {") == []
