"""Every feature of the column-expression language runs on DuckDB (ADR-008).

FEATURE_CASES has one case per entry of obi_formats.colexpr.FEATURES. Each case adds a
measure to the sales model and compares the engine's result with hand-written SQL.
packages/cli/tests/test_coverage.py fails if a feature has no case here.
"""

from pathlib import Path

import pytest

from obi_engine import Engine, Project, SemanticQuery
from obi_formats.artifacts import parse
from obi_formats.colexpr import FEATURES, features_used, parse as parse_colexpr
from obi_formats.modelindex import ModelIndex

DEMO = Path(__file__).resolve().parents[3] / "examples" / "sales-demo"
NET = "o.quantity * o.unit_price * (1 - o.discount)"
JOINED = ("FROM order_lines o JOIN customers c USING (customer_id) "
          "JOIN calendar k ON o.order_date = k.date")

# feature -> (measure spec on order_lines, oracle SQL returning one number)
FEATURE_CASES: dict[str, tuple[dict, str]] = {
    "arithmetic": (
        {"sum": "quantity * unit_price / 2 - quantity % 2 + -discount"},
        "SELECT SUM(o.quantity * o.unit_price / 2 - o.quantity % 2 + -o.discount) FROM order_lines o"),
    "parentheses": (
        {"sum": "quantity * (unit_price - unit_cost)"},
        "SELECT SUM(o.quantity * (o.unit_price - o.unit_cost)) FROM order_lines o"),
    "comparison": (
        {"sum": "quantity", "where": "month(date) > 3 and month(date) <= 6 and year(date) >= 2026 "
                                     "and quarter(date) < 3 and channel != 'Online' and channel <> 'Retail' "
                                     "and month(date) == month(date) and month(date) = month(date)"},
        "SELECT SUM(o.quantity) FROM order_lines o WHERE month(o.order_date) BETWEEN 4 AND 6 "
        "AND year(o.order_date) >= 2026 AND o.channel = 'Partner'"),
    "in": (
        {"sum": "quantity", "where": "segment in ('SMB', 'Enterprise')"},
        f"SELECT SUM(o.quantity) {JOINED} WHERE c.segment IN ('SMB', 'Enterprise')"),
    "between": (
        {"count": "order_id", "where": "month(date) between 4 and 6"},
        "SELECT COUNT(*) FROM order_lines o WHERE month(o.order_date) BETWEEN 4 AND 6"),
    "is_null": (
        {"count": "order_id", "where": "signup_date is not null and not (channel is null)"},
        f"SELECT COUNT(*) {JOINED} WHERE c.signup_date IS NOT NULL"),
    "boolean": (
        {"sum": "quantity", "where": "returned or not (channel = 'Online') and segment = 'SMB'"},
        f"SELECT SUM(o.quantity) {JOINED} WHERE o.returned OR (o.channel <> 'Online' AND c.segment = 'SMB')"),
    "literal": (
        {"sum": "quantity * 1.5", "where": "returned = false and coalesce(null, channel) = 'Online'"},
        "SELECT SUM(o.quantity * 1.5) FROM order_lines o WHERE NOT o.returned AND o.channel = 'Online'"),
    # A filtered measure on a joined dimension, with a date comparison across entities:
    # customer signup year against the order's calendar year.
    "year": (
        {"sum": "quantity * unit_price * (1 - discount)", "where": "year(signup_date) = year(date)"},
        f"SELECT SUM({NET}) {JOINED} WHERE year(c.signup_date) = year(k.date)"),
    "month": (
        {"sum": "quantity", "where": "month(date) = 12"},
        "SELECT SUM(o.quantity) FROM order_lines o WHERE month(o.order_date) = 12"),
    "quarter": (
        {"sum": "quantity", "where": "quarter(signup_date) = quarter(date)"},
        f"SELECT SUM(o.quantity) {JOINED} WHERE quarter(c.signup_date) = quarter(k.date)"),
    "date_diff": (
        {"sum": "quantity * unit_price * (1 - discount)",
         "where": "date_diff('day', signup_date, date) between 0 and 90"},
        f"SELECT SUM({NET}) {JOINED} WHERE date_diff('day', c.signup_date, k.date) BETWEEN 0 AND 90"),
    "coalesce": (
        {"sum": "coalesce(discount, 0) * quantity"},
        "SELECT SUM(COALESCE(o.discount, 0) * o.quantity) FROM order_lines o"),
    "left": (
        {"sum": "quantity", "where": "left(segment, 3) = 'Ent'"},
        f"SELECT SUM(o.quantity) {JOINED} WHERE c.segment = 'Enterprise'"),
    "case": (
        {"sum": "case when discount > 0 then quantity else 0 end"},
        "SELECT SUM(CASE WHEN o.discount > 0 THEN o.quantity ELSE 0 END) FROM order_lines o"),
    # Entity-prefixed dimension and a time grain inside `where:`.
    "reference": (
        {"sum": "quantity", "where": "customers.segment = 'Enterprise' and date.month = '2026-03-01'"},
        f"SELECT SUM(o.quantity) {JOINED} WHERE c.segment = 'Enterprise' "
        "AND date_trunc('month', k.date) = DATE '2026-03-01'"),
}


@pytest.fixture(scope="module")
def setup():
    project = Project(DEMO)
    data = parse((DEMO / "model/sales.model.yaml").read_text(encoding="utf-8")).data
    for feature, (spec, _) in FEATURE_CASES.items():
        data["entities"]["order_lines"]["measures"][f"case_{feature}"] = spec
    return project, Engine(project.con, ModelIndex("sales", data))


@pytest.mark.parametrize("feature", list(FEATURE_CASES))
def test_feature_runs_on_duckdb(setup, feature):
    project, eng = setup
    spec, oracle = FEATURE_CASES[feature]
    exprs = [v for k, v in spec.items() if k in ("sum", "count", "where")]
    assert feature in set().union(*(features_used(parse_colexpr(e)) for e in exprs)), \
        f"case `{feature}` does not use the feature it is meant to test"
    got = eng.query(SemanticQuery([f"case_{feature}"])).to_pylist()[0][f"case_{feature}"]
    want = project.con.sql(oracle).fetchall()[0][0]
    assert want is not None and want != 0, "the oracle must select some rows"
    assert got == pytest.approx(float(want))


def test_every_feature_has_a_case():
    assert set(FEATURE_CASES) == set(FEATURES)


def test_filtered_measure_on_joined_dimension_by_dimension(setup):
    """The example model's measure, sliced by a dimension from a third entity."""
    project, eng = setup
    got = {r["region"]: r["new_customer_revenue"]
           for r in eng.query(SemanticQuery(["new_customer_revenue"], ["region"])).to_pylist()}
    want = dict(project.con.sql(
        f"SELECT r.region_name, SUM({NET}) {JOINED} JOIN regions r ON c.region_id = r.region_id "
        "WHERE year(c.signup_date) = year(k.date) GROUP BY 1").fetchall())
    assert got.keys() == want.keys()
    for k in want:
        assert got[k] == pytest.approx(want[k])
