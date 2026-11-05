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
    return [i.text for i in issues if file is None or i.file == file]


def lines(overrides: dict[str, str]) -> list[int]:
    return [i.line for i in validate_project(DEMO, overrides)]


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
    o = edit(MODEL, "joins: { calendar: order_date, customers: customer_id, products: product_id }",
             "joins: { calendar: order_date, customers: customer_id, products: product_id, "
             "regions: region_id }")
    assert messages(o, MODEL) == [
        "model `sales`, entity `order_lines`: reaches `regions` through two join paths: "
        "order_lines → customers → regions and order_lines → regions; keep exactly one (ADR-001)",
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
