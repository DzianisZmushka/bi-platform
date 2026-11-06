import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from obi_formats.artifacts import parse
from obi_formats.expr import ExprError, FilterSelection, parse as parse_expr, render
from obi_formats.schema import get_schema
from obi_formats.validate import check_expression

ROOT = Path(__file__).resolve().parents[3]
DEMO = Path(os.environ["OBI_EXAMPLES"]) / "sales-demo"


# --------------------------------------------------------------------------- expressions

CTX = {
    "value": 0.97,
    "row": {"region": "EMEA", "net_revenue": 1200},
    "user": {"region": "EMEA"},
    "filters": {"region": FilterSelection(["EMEA", "APAC"]), "channel": FilterSelection()},
}


@pytest.mark.parametrize("src, expected", [
    ("${value < 0.95 ? 'danger' : value < 1 ? 'warning' : 'ok'}", "warning"),
    ("${filters.region.count}", 2),
    ("${filters.channel.count == 0}", True),
    ("${filters.channel.first}", None),
    ("${filters.region.first}", "EMEA"),
    ("${filters.region.includes(row.region) and not value > 1}", True),
    ("${row.net_revenue / 0}", None),
    ("${row.missing + 1}", None),
    ("${-row.net_revenue * 2 + 400 % 7}", -2399),
    ("Regions: ${filters.region.label}", "Regions: EMEA, APAC"),
    ("Channel: ${filters.channel.label}.", "Channel: ."),
    ("${'a' + 1}", "a1"),
    ("${user.region == 'EMEA'}", True),
])
def test_evaluate(src, expected):
    assert render(src, CTX) == expected


@pytest.mark.parametrize("src, message", [
    ("a && b", "use `and` instead of `&&`"),
    ("a || b", "use `or` instead of `||`"),
    ("!a", "use `not` instead of `!`"),
    ("a = 1", "use `==` instead of `=`"),
    ("a < b < c", "comparisons cannot be chained; combine them with `and`"),
    ("max(a, b)", "`max(...)` is not available; expressions have no functions"),
    ("(a", "expected `)` at end of expression"),
    ("a ? b", "expected `:` at end of expression"),
    ("a #", "unexpected character `#`"),
])
def test_parse_errors(src, message):
    with pytest.raises(ExprError) as e:
        parse_expr(src)
    assert str(e.value) == message


def test_static_checks():
    names = ("value", "row", "filters", "user")
    kw = dict(names=names, row_fields=["net_revenue"], filter_ids=["region"])
    assert check_expression("${value > 0 ? 'ok' : 'danger'}", color=True, **kw) == []
    assert check_expression("danger", color=True, **kw) == []
    assert check_expression("${filters.region.label}", **kw) == []
    assert check_expression("${filters.region}", **kw) == [
        "`filters.region` is a list; use `filters.region.label`, `.count`, `.first` or `.includes(x)`"]
    assert check_expression("${filters.region != null}", **kw) == [
        "`filters.region` is a list and is never null; use `filters.region.count > 0`"]
    assert check_expression("${filters.region.size}", **kw) == [
        "`filters.region.size` is not available; use `.label`, `.count`, `.first`, `.includes(x)`, or "
        "`.start`/`.end` for date filters"]
    assert check_expression("${value}", names=("filters", "user"), filter_ids=["region"]) == [
        "`value` is not available here; use filters, user"]
    assert check_expression("${row}", **kw) == ["`row` needs a name after it, such as `row.net_revenue`"]
    assert check_expression("${value > 1 ? 'red' : 'ok'}", color=True, **kw) == [
        "`red` is not a theme color; use one of ok, warning, danger, neutral, accent"]


# --------------------------------------------------------------------------- published schemas


@pytest.mark.parametrize("rel, kind", [
    ("project.yaml", "project"),
    ("model/sales.model.yaml", "model"),
    ("dashboards/revenue-overview/dashboard.yaml", "dashboard"),
    ("dashboards/commercial-review/dashboard.yaml", "dashboard"),
])
def test_published_schema_accepts_examples(rel, kind):
    """Editors use the published schema directly; it must agree with the examples."""
    validator = Draft202012Validator(get_schema(kind).raw)
    data = parse((DEMO / rel).read_text(encoding="utf-8")).data
    assert list(validator.iter_errors(data)) == []


def test_published_schema_dispatches_on_kind_key():
    validator = Draft202012Validator(get_schema("dashboard").raw)
    doc = {"dashboard": "d", "model": "m", "pages": [{"page": "p", "items": [
        {"kpi": "k", "measure": "x", "mark": "line"},   # `mark` belongs to charts
        {"kip": "typo"},                                # unknown kind
    ]}]}
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    paths = [list(e.absolute_path) for e in errors]
    assert ["pages", 0, "items", 0] in paths and ["pages", 0, "items", 1] in paths
    assert len(get_schema("dashboard").raw["$defs"]["node"]["allOf"]) == 12
    assert not any("oneOf" in str(d) for d in get_schema("dashboard").raw["$defs"].values())
