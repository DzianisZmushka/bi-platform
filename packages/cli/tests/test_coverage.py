"""Every property the validator accepts has runtime behaviour (CLAUDE.md working rules).

A schema property must be one of:
- handled: listed in the renderer's `RENDERED` (dashboard) or the engine's `HANDLED_*` (model, project);
- documentation-only: listed in `DOCUMENTATION_ONLY` (no runtime behaviour by design);
- marked `x-unsupported`: the validator then reports "not supported yet";
- marked `x-pending` (on the property, or on the whole schema): accepted and checked, but its
  runtime is still being built; the validator warns "not enforced yet" wherever it is used.
Nothing the validator accepts may be silently ignored.

A property marked `x-write-rule` restricts writes. The server enforces it, and a test in
`test_write_rules.py` posts around the page and is refused (`@refuses("<kind>:<def>.<prop>")`).
"""

import json
import re
from datetime import date
from importlib.resources import files
import os
from pathlib import Path

import pytest

from obi_cli.forms import FORMS_RENDERED
from obi_cli.render import RENDERED, render_dashboard
from obi_engine import DOCUMENTATION_ONLY, HANDLED_MODEL, HANDLED_PROJECT
from obi_engine.access import HANDLED_ACCESS
from obi_formats.schema import get_schema
from obi_formats.validate import validate_project

DEMO = Path(os.environ["OBI_EXAMPLES"]) / "sales-demo"
AS_OF = date(2026, 11, 5)


def shown(issue) -> bool:
    """Errors and warnings, without info notices and without the "not enforced yet" warnings of
    pending properties (ADR-013), which test_access.py checks on their own."""
    return issue.severity != "info" and "is not enforced yet" not in issue.text
REGISTRY = {"dashboard": RENDERED, "model": HANDLED_MODEL, "project": HANDLED_PROJECT, "form": FORMS_RENDERED,
            "access": HANDLED_ACCESS}


def pending(kind: str, sub: dict) -> bool:
    return "x-pending" in sub or "x-pending" in get_schema(kind).raw


def schema_properties(kind: str):
    """(owner, property, subschema) for every property, including inline nested objects
    such as `connection.duckdb`."""
    schema = get_schema(kind)

    def walk(owner: str, s: dict):
        for prop, sub in (s.get("properties") or {}).items():
            yield owner, prop, sub
            if "properties" in sub:  # inline object, not a $ref to another definition
                yield from walk(f"{owner}.{prop}", sub)

    for name, d in schema.defs.items():
        yield from walk(name, schema.resolve(d))


def cases():
    for kind in REGISTRY:
        for owner, prop, sub in schema_properties(kind):
            yield pytest.param(kind, owner, prop, sub, id=f"{kind}:{owner}.{prop}")


@pytest.mark.parametrize("kind, owner, prop, sub", list(cases()))
def test_every_schema_property_is_handled_or_marked(kind, owner, prop, sub):
    handled = prop in REGISTRY[kind].get(owner, set())
    documented = prop in DOCUMENTATION_ONLY.get(owner, set()) if kind == "model" else False
    unsupported = "x-unsupported" in sub or pending(kind, sub)
    assert handled or documented or unsupported, (
        f"{kind} schema `{owner}.{prop}` is accepted by the validator but nothing handles it. "
        f"Implement it and add it to the registry, or mark it `x-unsupported` in the schema.")
    assert not (unsupported and handled), f"`{owner}.{prop}` is both handled and x-unsupported"


@pytest.mark.parametrize("kind", list(REGISTRY))
def test_registries_have_no_stale_entries(kind):
    props = {(o, p) for o, p, _ in schema_properties(kind)}
    stale = [(o, p) for o, ps in REGISTRY[kind].items() for p in ps if (o, p) not in props]
    assert stale == []


def test_rendered_properties_are_read_by_the_renderer():
    """Guard against a registry that claims more than the code does: each property the
    renderer lists must be read somewhere in render.py outside the registry itself."""
    src = files("obi_cli").joinpath("render.py").read_text("utf-8")
    start = src.index("RENDERED: dict")
    code = src[:start] + src[src.index("\n}\n", start):]
    missing = sorted({p for ps in RENDERED.values() for p in ps if f'"{p}"' not in code})
    assert missing == []


# --------------------------------------------------------------------------- behaviour


def test_unsupported_property_is_a_validator_error():
    """No live property is marked `x-unsupported` today; mark one for the test and check the
    message the validator would give."""
    from obi_formats.schema import get_schema

    schema = get_schema("project")
    prop = schema.defs["connection"]["properties"]["duckdb"]["properties"]["database"]
    prop["x-unsupported"] = "test reason"
    try:
        msgs = [i.text for i in validate_project(DEMO) if shown(i)]
    finally:
        del prop["x-unsupported"]
    assert msgs == ["project `sales-demo`, connection `local`: `duckdb.database` is not supported yet: test reason"]


def test_kpi_on_click_navigates_with_row_values():
    html = render_dashboard(DEMO, "commercial-review", None, AS_OF, mode="serve").html
    link = re.search(r'id="w-customers">.*?<a class="kpi-link" href="([^"]+)"', html, re.S)
    assert link and link.group(1).endswith("#top_customers")
    static = render_dashboard(DEMO, "commercial-review", None, AS_OF).html
    assert re.search(r'id="w-customers">.*?<a class="kpi-link" href="#top_customers"', static, re.S)


def test_hierarchy_drill_in_serve_mode():
    top = render_dashboard(DEMO, "commercial-review", None, AS_OF, mode="serve").html
    assert "drill.category_drill=Hardware" in top
    deeper = render_dashboard(DEMO, "commercial-review", None, AS_OF, mode="serve",
                              drill={"category_drill": ["Hardware"]}).html
    assert '<div class="crumbs">' in deeper and "drill.category_drill=Laptops" in deeper
    # At the last level there is nowhere deeper to go.
    last = render_dashboard(DEMO, "commercial-review", None, AS_OF, mode="serve",
                            drill={"category_drill": ["Hardware", "Laptops"]}).html
    assert "drill.category_drill=Laptops&amp;drill.category_drill=" not in last


def test_pivot_table_respects_limit(tmp_path):
    import shutil
    dst = tmp_path / "p"
    shutil.copytree(DEMO, dst, ignore=shutil.ignore_patterns(".obi"))
    f = dst / "dashboards/commercial-review/dashboard.yaml"
    text = f.read_text(encoding="utf-8")
    new = text.replace("                    rows: [segment]\n                    columns: [channel]\n",
                       "                    rows: [country]\n                    columns: [channel]\n")
    new = new.replace("                    values: [net_revenue]\n                    totals: true\n",
                      "                    values: [net_revenue]\n                    limit: 3\n                    totals: true\n")
    assert new.count("limit: 3") == 1
    f.write_text(new, encoding="utf-8")
    assert [i for i in validate_project(dst) if shown(i)] == []
    html = render_dashboard(dst, "commercial-review", None, AS_OF).html
    table = re.search(r'id="w-segment_table">.*?<tbody>(.*?)</tbody>', html, re.S).group(1)
    assert table.count("<tr") == 3


def test_descriptions_are_rendered(tmp_path):
    import shutil
    dst = tmp_path / "p"
    shutil.copytree(DEMO, dst, ignore=shutil.ignore_patterns(".obi"))
    f = dst / "dashboards/revenue-overview/dashboard.yaml"
    text = f.read_text(encoding="utf-8")
    new = text.replace("  - page: overview\n", "  - page: overview\n    description: Page note.\n")
    new = new.replace("      - table: top_customers\n", "      - table: top_customers\n"
                      "        description: Ranked by net revenue.\n")
    f.write_text(new, encoding="utf-8")
    assert [i for i in validate_project(dst) if shown(i)] == []
    html = render_dashboard(dst, "revenue-overview", None, AS_OF).html
    assert '<p class="desc">Page note.</p>' in html
    assert 'class="info" title="Ranked by net revenue."' in html


def test_every_expression_feature_has_a_duckdb_test():
    """ADR-008: every syntactic feature the column-expression parser accepts runs on DuckDB in
    packages/engine/tests/test_column_expressions.py."""
    import importlib.util

    from obi_formats.colexpr import FEATURES

    path = Path(__file__).resolve().parents[2] / "engine" / "tests" / "test_column_expressions.py"
    spec = importlib.util.spec_from_file_location("engine_column_expression_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sorted(FEATURES) == sorted(module.FEATURE_CASES)


# --------------------------------------------------------------------------- documentation (CLAUDE.md)

ROOT = Path(__file__).resolve().parents[3]
DOC_FILES = sorted(ROOT.glob("examples/*/README.md")) + sorted(ROOT.glob("docs/formats/*.md"))


def _documented(name: str, text: str) -> bool:
    """Named as code: inside backticks, or as a YAML key (`name:`)."""
    n = re.escape(name)
    return re.search(rf"`[^`\n]*\b{n}\b[^`\n]*`|(^|[\s{{,]){n}:", text, re.M) is not None


@pytest.mark.parametrize("kind, owner, prop, sub", list(cases()))
def test_every_schema_property_is_documented(kind, owner, prop, sub):
    docs = "\n".join(p.read_text(encoding="utf-8") for p in DOC_FILES)
    assert DOC_FILES, "no documentation found"
    assert _documented(prop, docs), (
        f"{kind} schema `{owner}.{prop}` exists in code but no README under examples/ or file in docs/formats/ "
        f"mentions `{prop}`. Document it there (CLAUDE.md).")


def test_documentation_check_is_not_trivially_true():
    assert not _documented("no_such_property_xyz", "`no_such_property_x` and other: text")
    assert _documented("convert", "measure `convert: USD`") and _documented("rate", "  rate: UKURS")


def test_every_write_rule_has_a_refusal_test():
    """CLAUDE.md: every write restriction holds on the server, proved by a request that bypasses
    the page and is refused."""
    import ast

    rules = {f"{kind}:{owner}.{prop}" for kind in REGISTRY for owner, prop, sub in schema_properties(kind)
             if sub.get("x-write-rule") and not pending(kind, sub)}  # pending rules get theirs with the engine
    assert len(rules) >= 15, rules
    tree = ast.parse((Path(__file__).parent / "test_write_rules.py").read_text(encoding="utf-8"))
    proved = {arg.value for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name.startswith("test_")
              for d in fn.decorator_list if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "refuses"
              for arg in d.args if isinstance(arg, ast.Constant)}
    assert sorted(rules - proved) == [], "write rules without a test that posts around the page and is refused"
    assert sorted(proved - rules) == [], "@refuses names a property that is not marked x-write-rule"


def test_pending_property_warns_and_obi_serve_shows_a_banner():
    """`x-pending`: accepted and checked, not enforced yet. Every use is a warning, and `obi serve`
    shows a banner on every page while one is in use (ADR-013). No property is pending today."""
    from obi_cli.server import pending_banner

    assert not any("x-pending" in json.dumps(get_schema(k).raw) for k in REGISTRY)
    schema = get_schema("model")
    prop = schema.defs["entity"]["properties"]["access"]
    prop["x-pending"] = "test reason"
    try:
        pending = [i.text for i in validate_project(DEMO) if "is not enforced yet" in i.text]
    finally:
        del prop["x-pending"]
    assert pending == ["model `sales`, entity `regions`: `access` is not enforced yet: test reason"]
    banner = pending_banner(pending)
    assert "Access rules declared but not enforced." in banner and "test reason" in banner
    assert pending_banner([]) == ""
