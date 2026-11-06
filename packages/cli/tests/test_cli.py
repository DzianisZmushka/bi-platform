"""`obi validate` / `obi render` end to end on the sales-demo project."""

import html as html_lib
import re
import shutil
from datetime import date
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from obi_cli.__main__ import main
from obi_cli.render import render_dashboard

DEMO = Path(os.environ["OBI_EXAMPLES"]) / "sales-demo"
AS_OF = date(2026, 11, 5)


@pytest.fixture
def project(tmp_path) -> Path:
    dst = tmp_path / "sales-demo"
    shutil.copytree(DEMO, dst, ignore=shutil.ignore_patterns(".obi"))
    return dst


def test_validate_exit_codes(project, capsys):
    assert main(["validate", str(project)]) == 0
    p = project / "dashboards/revenue-overview/dashboard.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("measure: net_revenue", "measure: net_rev"), encoding="utf-8")
    assert main(["validate", str(project)]) == 1
    assert "measure `net_rev` not found in model `sales`; did you mean `net_revenue`?" in capsys.readouterr().err


def test_render_refuses_an_invalid_project(project, capsys):
    p = project / "dashboards/revenue-overview/dashboard.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("mark: line", "mark: lines"), encoding="utf-8")
    assert main(["render", str(project)]) == 1
    assert "fix them before rendering" in capsys.readouterr().err
    assert not (project / ".obi").exists()


def test_render_all_dashboards(project, capsys):
    assert main(["render", str(project), "--as-of", "2026-11-05"]) == 0
    out = capsys.readouterr().out.split()
    assert [Path(p).name for p in out] == ["commercial-review.html", "revenue-overview.html"]
    html = (project / ".obi/render/commercial-review.html").read_text(encoding="utf-8")
    assert 'class="widget-error"' not in html and 'class="render-errors"' not in html
    assert html.count('class="widget kpi"') == 16 and html.count('class="widget chart"') == 15
    assert 'id="regions"' in html and 'href="revenue-overview.html#overview"' in html


@pytest.mark.parametrize("dash", ["revenue-overview", "commercial-review"])
def test_every_widget_renders_in_serve_mode(dash):
    result = render_dashboard(DEMO, dash, None, AS_OF, mode="serve")
    assert result.errors == []


def test_ignored_filter_marker_appears_only_when_the_filter_is_set():
    plain = render_dashboard(DEMO, "commercial-review", None, AS_OF).html
    assert "Not filtered by" not in plain
    html = render_dashboard(DEMO, "commercial-review", {"channel": ["Online"]}, AS_OF).html
    marked = re.findall(r'id="w-(\w+)"><div class="ignored"[^>]*>Not filtered by: ([^<]+)<', html)
    assert ("revenue", "Channel") in marked and ("attainment_trend", "Channel") in marked
    assert len(marked) == 10  # exactly the widgets that declare ignore_filters (incl. record text, Targets page)
    assert not any(w == "region_aov" for w, _ in marked)


def test_navigation_links_in_serve_mode():
    html = render_dashboard(DEMO, "commercial-review", {"channel": ["Online"]}, AS_OF, mode="serve").html
    # Same dashboard: full filter state (`_set`, cleared filters as empty) plus the `set` value.
    href = html_lib.unescape(re.search(r'data-href="(/commercial-review\?[^"]*#products)"', html).group(1))
    q = parse_qs(urlparse(href).query, keep_blank_values=True)
    assert q["_set"] == ["1"] and q["period"] == ["this_year"] and q["channel"] == ["Online"]
    assert q["region"] == [""] and q["country"][0] != ""
    # Another dashboard: its own defaults (no `_set`).
    assert 'href="/revenue-overview#overview"' in html


def test_filter_values_reach_queries_and_expressions():
    html = render_dashboard(DEMO, "commercial-review", {"region": ["EMEA"]}, AS_OF).html
    assert "Showing all regions" not in html  # visible: ${filters.region.count == 0}
    html_all = render_dashboard(DEMO, "commercial-review", None, AS_OF).html
    assert "Showing all regions" in html_all


def test_database_errors_become_file_line_issues(project, capsys):
    """Static checks cannot know `channel` is text; running the measure does, and the error
    names the file, line and measure instead of passing a database message through."""
    m = project / "model/sales.model.yaml"
    m.write_text(m.read_text(encoding="utf-8").replace("units: { sum: quantity,", "units: { sum: year(channel),"),
                 encoding="utf-8")
    assert main(["validate", str(project)]) == 0  # static validation does not touch the data
    capsys.readouterr()
    assert main(["validate", str(project), "--execute"]) == 1
    err = capsys.readouterr().err
    assert re.search(r"model/sales\.model\.yaml:\d+: error: model `sales`, measure `units`: "
                     r"function `year` cannot be used on text values", err)
    assert "Binder" not in err and "VARCHAR" not in err



def test_static_validation_reads_columns_from_the_catalogue(project, capsys):
    """CI runs `obi validate` without data access: columns come from catalog.json."""
    import json
    cat = json.loads((project / "catalog.json").read_text(encoding="utf-8"))
    del cat["tables"]["returns"]["refund_amount"]          # the CSV still has it
    (project / "catalog.json").write_text(json.dumps(cat), encoding="utf-8")
    assert main(["validate", str(project)]) == 1
    assert "column `refund_amount` not found in table `returns`" in capsys.readouterr().err


def test_execute_reports_a_stale_catalogue(project, capsys):
    import json
    cat = json.loads((project / "catalog.json").read_text(encoding="utf-8"))
    cat["tables"]["returns"]["refund_amount"] = "VARCHAR"
    (project / "catalog.json").write_text(json.dumps(cat), encoding="utf-8")
    assert main(["validate", str(project)]) == 0
    capsys.readouterr()
    assert main(["validate", str(project), "--execute"]) == 1
    assert ("catalog.json is out of date: table `returns` changed type of `refund_amount`. "
            "Run `obi catalog` and commit the result") in capsys.readouterr().err
    assert main(["catalog", str(project)]) == 0
    assert main(["validate", str(project), "--execute"]) == 0


def test_without_a_catalogue_columns_come_from_files_with_a_warning(project, capsys):
    (project / "catalog.json").unlink()
    assert main(["validate", str(project)]) == 0
    assert "warning: project `sales-demo`: no catalog.json" in capsys.readouterr().err


def test_erp_dashboard_renders(capsys):
    erp = DEMO.parent / "erp-sales"
    for mode in ("static", "serve"):
        result = render_dashboard(erp, "order-to-cash", None, AS_OF, mode=mode)
        assert result.errors == []
    html = render_dashboard(erp, "order-to-cash", None, AS_OF, mode="serve",
                            drill={"product_margin": ["Pumps"]}).html
    assert '<div class="crumbs">' in html and "Centrifugal" in html
    assert "FY2026 P07" in html


def test_serve_footer_names_project_and_commit(tmp_path):
    """A stale server must be visible: the footer names the project, the start commit, and
    warns when the repository has moved on."""
    from obi_cli.server import git_commit, provenance

    erp = Path(__file__).resolve().parents[3] / "examples" / "erp-sales"  # the repository, not the test copy
    commit = git_commit(erp)
    assert commit and len(commit.split()[0]) >= 7
    footer = provenance(erp, "2026-11-06 10:00", commit)
    assert str(erp.resolve()) in footer and commit.split()[0] in footer and "older code" not in footer
    stale = provenance(erp, "2026-11-05 21:52", "0000000")
    assert "this server runs older code. Restart <code>obi serve</code>" in stale
    html = render_dashboard(DEMO.parent / "erp-sales", "order-to-cash", None, AS_OF, mode="serve", footer=footer).html
    assert 'class="provenance"' in html
