"""Semantic engine tests (ADR-001): each (model, query) is checked against hand-written SQL."""

from datetime import date
import os
from pathlib import Path

import duckdb
import pytest

from obi_engine import EngineError, Filter, Project, SemanticQuery
from obi_engine import dates

DEMO = Path(os.environ["OBI_EXAMPLES"]) / "sales-demo"
DATA = (DEMO / "data").as_posix()
Q1 = (date(2026, 1, 1), date(2026, 3, 31))


@pytest.fixture(scope="module")
def eng():
    return Project(DEMO).engine("sales")


def oracle(sql: str) -> list[tuple]:
    con = duckdb.connect()
    for t in ("order_lines", "customers", "products", "regions", "targets"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_csv_auto('{DATA}/{t}.csv')")
    return con.sql(sql).fetchall()


def rows(eng, q: SemanticQuery) -> list[tuple]:
    return [tuple(r.values()) for r in eng.query(q).to_pylist()]


def close(a: list[tuple], b: list[tuple]):
    assert len(a) == len(b)
    for ra, rb in zip(a, b):
        for x, y in zip(ra, rb):
            assert x == pytest.approx(y) if isinstance(y, float) else x == y


def test_measure_by_snowflaked_dimension(eng):
    got = rows(eng, SemanticQuery(["net_revenue"], ["region"]))
    want = oracle("""SELECT r.region_name, SUM(o.quantity * o.unit_price * (1 - o.discount))
                     FROM order_lines o JOIN customers c USING (customer_id) JOIN regions r USING (region_id)
                     GROUP BY 1 ORDER BY 1""")
    close(got, want)


def test_drill_across_actuals_and_targets(eng):
    got = rows(eng, SemanticQuery(["net_revenue", "revenue_target", "target_attainment"], ["region"],
                                  [Filter("date", range=Q1)]))
    want = oracle("""
        WITH a AS (SELECT r.region_name AS region, SUM(o.quantity * o.unit_price * (1 - o.discount)) AS rev
                   FROM order_lines o JOIN customers c USING (customer_id) JOIN regions r USING (region_id)
                   WHERE o.order_date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' GROUP BY 1),
             t AS (SELECT r.region_name AS region, SUM(t.revenue_target)::DOUBLE AS tgt
                   FROM targets t JOIN regions r USING (region_id)
                   WHERE t.month BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' GROUP BY 1)
        SELECT region, rev, tgt, rev / tgt FROM a JOIN t USING (region) ORDER BY 1""")
    close(got, want)


def test_time_grain_filtered_measure_and_values_filter(eng):
    got = rows(eng, SemanticQuery(["returned_revenue", "return_rate"], ["date.quarter"],
                                  [Filter("channel", values=["Partner"]), Filter("date", range=(date(2025, 1, 1), date(2025, 12, 31)))]))
    want = oracle("""
        SELECT CAST(date_trunc('quarter', order_date) AS DATE),
               SUM(quantity * unit_price * (1 - discount)) FILTER (WHERE returned),
               SUM(quantity * unit_price * (1 - discount)) FILTER (WHERE returned)
                 / SUM(quantity * unit_price * (1 - discount))
        FROM order_lines WHERE channel = 'Partner' AND order_date BETWEEN '2025-01-01' AND '2025-12-31'
        GROUP BY 1 ORDER BY 1""")
    close(got, want)


def test_calc_over_calc_sort_and_limit(eng):
    got = rows(eng, SemanticQuery(["margin_pct"], ["category"], sort=["-margin_pct"], limit=2))
    want = oracle("""
        SELECT p.category, (SUM(o.quantity*o.unit_price*(1-o.discount)) - SUM(o.quantity*o.unit_cost))
                           / SUM(o.quantity*o.unit_price*(1-o.discount)) AS m
        FROM order_lines o JOIN products p USING (product_id) GROUP BY 1 ORDER BY m DESC LIMIT 2""")
    close(got, want)


def test_hierarchy_shows_top_level(eng):
    got = eng.query(SemanticQuery(["units"], ["product"])).column_names
    assert got == ["category", "units"]


def test_division_by_zero_is_null_not_infinity(eng):
    assert "NULLIF(" in eng.sql(SemanticQuery(["aov"]))
    empty = eng.query(SemanticQuery(["aov"], [], [Filter("channel", values=["No such channel"])])).to_pylist()
    assert empty == [{"aov": None}]


def test_dimension_outside_a_facts_reach_is_an_error(eng):
    with pytest.raises(EngineError, match=r"dimension `channel` cannot be reached from entity `targets`"):
        eng.query(SemanticQuery(["revenue_target"], ["channel"]))


def test_distinct_values(eng):
    assert eng.distinct("channel") == ["Online", "Partner", "Retail"]


@pytest.mark.parametrize("preset, start, end", [
    ("this_year", "2026-01-01", "2026-12-31"),
    ("last_year", "2025-01-01", "2025-12-31"),
    ("this_quarter", "2026-10-01", "2026-12-31"),
    ("last_quarter", "2026-07-01", "2026-09-30"),
    ("last_month", "2026-10-01", "2026-10-31"),
    ("last_12_months", "2025-12-01", "2026-11-30"),
    ("year_to_date", "2026-01-01", "2026-11-05"),
    ("this_week", "2026-11-02", "2026-11-08"),
    ("2026-02-01..2026-02-28", "2026-02-01", "2026-02-28"),
])
def test_date_presets(preset, start, end):
    assert dates.resolve(preset, date(2026, 11, 5)) == (date.fromisoformat(start), date.fromisoformat(end))


def test_date_shifts():
    assert dates.shift_year((date(2024, 2, 29), date(2024, 3, 31))) == (date(2023, 2, 28), date(2023, 3, 31))
    assert dates.shift_period((date(2026, 4, 1), date(2026, 4, 30))) == (date(2026, 3, 2), date(2026, 3, 31))
