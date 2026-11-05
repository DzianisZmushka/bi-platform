"""Grain derivation (ADR-005 §2)."""

from pathlib import Path

from obi_formats.artifacts import parse
from obi_formats.modelindex import ModelIndex

DEMO = Path(__file__).resolve().parents[3] / "examples" / "sales-demo"


def index() -> ModelIndex:
    return ModelIndex("sales", parse((DEMO / "model/sales.model.yaml").read_text(encoding="utf-8")).data)


def grain_names(idx, measure):
    g = idx.measure_grain(idx.resolve(measure, ("measure",)).field)
    return {(f"{e}.{d}", tg) for (e, d), tg in g.items()}


def test_calc_of_unrestricted_measures_stays_unrestricted():
    idx = index()
    full = grain_names(idx, "net_revenue")
    assert full, "base measure must have a non-empty grain"
    # Every dimension reachable from order_lines, at full detail.
    assert ("customers.segment", None) in full and ("calendar.date", None) in full
    # Calcs over unrestricted measures, including a calc over a calc, keep the full grain.
    assert grain_names(idx, "aov") == full            # net_revenue / orders
    assert grain_names(idx, "margin_pct") == full     # gross_margin (calc) / net_revenue
    assert grain_names(idx, "return_rate") == full    # uses a filtered measure


def test_calc_with_a_restricted_measure_takes_the_intersection():
    idx = index()
    assert grain_names(idx, "target_attainment") == {("calendar.date", "month"), ("regions.region", None)}


def test_calc_without_measure_references_falls_back_to_entity_grain():
    data = parse((DEMO / "model/sales.model.yaml").read_text(encoding="utf-8")).data
    data["entities"]["order_lines"]["measures"]["constant"] = {"calc": "1"}
    idx = ModelIndex("sales", data)
    assert grain_names(idx, "constant") == grain_names(idx, "net_revenue")
