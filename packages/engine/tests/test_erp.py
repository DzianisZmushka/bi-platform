"""The erp-sales example proves three engine features against hand-written SQL (ADR-009):
composite-key and expression joins, currency conversion, and the fiscal calendar."""

from datetime import date
from pathlib import Path

import pytest

from obi_engine import Engine, Filter, Project, SemanticQuery
from obi_formats.artifacts import parse
from obi_formats.modelindex import ModelIndex

ERP = Path(__file__).resolve().parents[3] / "examples" / "erp-sales"
AS_OF = date(2026, 11, 5)
ORDER = ("FROM vbap p JOIN vbak k ON p.VBELN = k.VBELN "
         "JOIN tcurr fx ON fx.FCURR = k.WAERK AND fx.GDATU = date_trunc('month', k.ERDAT)")


@pytest.fixture(scope="module")
def erp():
    project = Project(ERP)
    return project, project.engine("erp")


def oracle(project, sql):
    return project.con.sql(sql).fetchall()


def close(got: dict, want: dict):
    assert got.keys() == want.keys()
    for k in want:
        assert got[k] == pytest.approx(want[k]), k


def test_composite_key_join_billing_converts_at_billing_date(erp):
    """vbrp -> vbap on (AUBEL, AUPOS) -> vbak -> tvko; billing converts at its own date (FKDAT)."""
    project, eng = erp
    got = {r["sales_org"]: r["billed_value_usd"]
           for r in eng.query(SemanticQuery(["billed_value_usd"], ["sales_org"])).to_pylist()}
    want = dict(oracle(project, """
        SELECT o.VTEXT, SUM(b.NETWR * fx.UKURS) FROM vbrp b
        JOIN vbap p ON b.AUBEL = p.VBELN AND b.AUPOS = p.POSNR
        JOIN vbak k ON p.VBELN = k.VBELN JOIN tvko o ON k.VKORG = o.VKORG
        JOIN tcurr fx ON fx.FCURR = k.WAERK AND fx.GDATU = date_trunc('month', b.FKDAT)
        GROUP BY 1"""))
    close(got, want)
    sql = eng.sql(SemanticQuery(["billed_value_usd"], ["sales_org"]))
    assert "AND" in sql.split('"vbap"')[1].split("LEFT")[0]


def test_open_orders_are_exact_per_item(erp):
    """(ordered quantity - billed quantity) x unit value, converted at the order date."""
    project, eng = erp
    got = {r["sales_org"]: r["open_order_value_usd"]
           for r in eng.query(SemanticQuery(["open_order_value_usd"], ["sales_org"])).to_pylist()}
    want = dict(oracle(project, f"""
        SELECT o.VTEXT, SUM((p.KWMENG - COALESCE(b.q, 0)) * p.NETWR / p.KWMENG * fx.UKURS) {ORDER}
        JOIN tvko o ON k.VKORG = o.VKORG
        LEFT JOIN (SELECT AUBEL, AUPOS, SUM(FKIMG) AS q FROM vbrp GROUP BY ALL) b
          ON b.AUBEL = p.VBELN AND b.AUPOS = p.POSNR
        WHERE p.ABGRU IS NULL GROUP BY 1"""))
    close(got, want)
    # A fully billed item has no open value at all, whatever the exchange rate did in between.
    full = oracle(project, """SELECT p.VBELN, p.POSNR FROM vbap p JOIN vbrp b ON b.AUBEL = p.VBELN
        AND b.AUPOS = p.POSNR WHERE b.FKIMG = p.KWMENG LIMIT 1""")[0]
    item = eng.query(SemanticQuery(["open_order_value_usd"], ["sales_order", "item"],
                                   [Filter("sales_order", values=[full[0]]), Filter("item", values=[full[1]])]))
    assert item.to_pylist()[0]["open_order_value_usd"] == 0


def test_measure_date_route(erp):
    """`date: order_date` counts billing in the fiscal period of its order."""
    project, eng = erp
    rows = eng.query(SemanticQuery(["billed_value_usd", "billed_by_order_date_usd"], ["date.fiscal_year"])).to_pylist()
    got = {r["date"]: (r["billed_value_usd"], r["billed_by_order_date_usd"]) for r in rows}
    by_billing = {f"FY{y}": v for y, v in oracle(project, """
        SELECT c.FISCAL_YEAR, SUM(b.NETWR * fx.UKURS) FROM vbrp b
        JOIN vbap p ON b.AUBEL = p.VBELN AND b.AUPOS = p.POSNR JOIN vbak k ON p.VBELN = k.VBELN
        JOIN tcurr fx ON fx.FCURR = k.WAERK AND fx.GDATU = date_trunc('month', b.FKDAT)
        JOIN calendar c ON c.DATUM = b.FKDAT GROUP BY 1""")}
    by_order = {f"FY{y}": v for y, v in oracle(project, """
        SELECT c.FISCAL_YEAR, SUM(b.NETWR * fx.UKURS) FROM vbrp b
        JOIN vbap p ON b.AUBEL = p.VBELN AND b.AUPOS = p.POSNR JOIN vbak k ON p.VBELN = k.VBELN
        JOIN tcurr fx ON fx.FCURR = k.WAERK AND fx.GDATU = date_trunc('month', b.FKDAT)
        JOIN calendar c ON c.DATUM = k.ERDAT GROUP BY 1""")}
    close({k: v[0] for k, v in got.items()}, by_billing)
    close({k: v[1] for k, v in got.items()}, by_order)


def test_lookup_keys_are_unique(erp):
    _, eng = erp
    assert eng.check_lookups() == []


def test_lookup_dimensions_product_hierarchy_levels(erp):
    """Lookup dimensions read t179t three times: left(PRDHA, 5), left(PRDHA, 10) and PRDHA."""
    project, eng = erp
    rows = eng.query(SemanticQuery(["item_count"], ["product_line", "product_family"])).to_pylist()
    got = {(r["product_line"], r["product_family"]): r["item_count"] for r in rows}
    want = {(a, b): n for a, b, n in oracle(project, """
        SELECT l1.VTEXT, l2.VTEXT, COUNT(*) FROM vbap p JOIN mara m ON p.MATNR = m.MATNR
        JOIN t179t l1 ON l1.PRODH = left(m.PRDHA, 5) JOIN t179t l2 ON l2.PRODH = left(m.PRDHA, 10)
        GROUP BY 1, 2""")}
    assert got == want and len(got) == 7


def test_currency_conversion_uses_document_currency_and_order_month(erp):
    project, eng = erp
    rows = eng.query(SemanticQuery(["order_value", "order_value_usd", "cost_usd"], ["document_currency"])).to_pylist()
    got = {r["document_currency"]: (r["order_value"], r["order_value_usd"], r["cost_usd"]) for r in rows}
    want = {c: (a, b, d) for c, a, b, d in oracle(project, f"""
        SELECT k.WAERK, SUM(p.NETWR), SUM(p.NETWR * fx.UKURS), SUM(p.WAVWR * fx.UKURS) {ORDER} GROUP BY 1""")}
    assert got.keys() == {"USD", "EUR", "AED"} == want.keys()
    for c in want:
        assert got[c] == pytest.approx(want[c])


def test_every_row_has_a_rate(erp):
    _, eng = erp
    assert eng.check_rates() == []


def test_missing_rate_is_reported(erp):
    project, _ = erp
    data = parse((ERP / "model/erp.model.yaml").read_text(encoding="utf-8")).data
    project.con.execute("CREATE OR REPLACE TABLE tcurr_gap AS SELECT * FROM tcurr "
                        "WHERE NOT (FCURR = 'EUR' AND GDATU = DATE '2025-06-01')")
    data["currency"]["rates"]["table"] = "tcurr_gap"
    eng = Engine(project.con, ModelIndex("erp", data))
    problems = eng.check_rates()
    assert [e for e, _ in problems] == ["order_items", "billing_items"]
    assert problems[0][1] == "no exchange rate for EUR 2025-06; converted amounts would leave those rows out"


def test_fiscal_grains_come_from_the_calendar_table(erp):
    project, eng = erp
    rows = eng.query(SemanticQuery(["order_value_usd"], ["date.fiscal_period"])).to_pylist()
    got = {r["date"]: r["order_value_usd"] for r in rows}
    want = {f"FY{y} P{p:02d}": v for y, p, v in oracle(project, f"""
        SELECT c.FISCAL_YEAR, c.FISCAL_PERIOD, SUM(p.NETWR * fx.UKURS) {ORDER}
        JOIN calendar c ON c.DATUM = k.ERDAT GROUP BY 1, 2""")}
    close(got, want)
    assert list(got)[:3] == ["FY2024 P10", "FY2024 P11", "FY2024 P12"]  # Jan-Mar 2025 belong to FY2024
    years = {r["date"] for r in eng.query(SemanticQuery(["order_count"], ["date.fiscal_year"])).to_pylist()}
    assert years == {"FY2024", "FY2025", "FY2026"}


def test_fiscal_presets_and_previous_year(erp):
    _, eng = erp
    assert eng.fiscal_range("date", "this_fiscal_year", AS_OF) == (date(2026, 4, 1), date(2026, 12, 30))
    assert eng.fiscal_range("date", "fiscal_year_to_date", AS_OF) == (date(2026, 4, 1), AS_OF)
    assert eng.fiscal_range("date", "last_fiscal_year", AS_OF) == (date(2025, 4, 1), date(2026, 3, 31))
    assert eng.fiscal_range("date", "this_fiscal_period", AS_OF) == (date(2026, 11, 1), date(2026, 11, 30))
    assert eng.fiscal_range("date", "last_fiscal_period", AS_OF) == (date(2026, 10, 1), date(2026, 10, 31))
    # Previous fiscal year of the year to date: the same periods, read from the calendar.
    # The calendar table ends in December 2026, so FY2026 has only 9 periods in it: shifting by
    # "periods in this year" would be wrong. The same period one fiscal year earlier is right.
    ytd = eng.fiscal_range("date", "fiscal_year_to_date", AS_OF)
    assert eng.fiscal_shift_years("date", ytd, -1) == (date(2025, 4, 1), date(2025, 11, 5))
    # Previous period: whole fiscal periods back, from the calendar.
    p07 = (date(2026, 10, 1), date(2026, 10, 31))
    assert eng.fiscal_shift("date", p07, -eng.fiscal_periods_in("date", p07)) == (date(2026, 9, 1), date(2026, 9, 30))


def test_fiscal_year_compare_in_the_dashboard(erp):
    """compare: previous_year on a fiscal filter compares with the previous fiscal year."""
    from obi_cli.render import render_dashboard
    import re

    project, eng = erp
    html = render_dashboard(ERP, "order-to-cash", None, AS_OF).html
    kpi = re.search(r'id="w-ordered">.*?kpi-sub">([^<]*)<', html, re.S).group(1)
    now = eng.query(SemanticQuery(["order_value_usd"], [], [Filter("date", range=(date(2026, 4, 1), AS_OF))]))
    prev = eng.query(SemanticQuery(["order_value_usd"], [], [Filter("date", range=(date(2025, 4, 1), date(2025, 11, 5)))]))
    change = now.to_pylist()[0]["order_value_usd"] / prev.to_pylist()[0]["order_value_usd"] - 1
    assert kpi == f"{'▲' if change >= 0 else '▼'} {abs(change):.1%} vs previous year"
