"""`obi validate` / `obi render` end to end on the sales-demo project."""

import html as html_lib
import re
import shutil
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from obi_cli.__main__ import main
from obi_cli.render import render_dashboard

DEMO = Path(__file__).resolve().parents[3] / "examples" / "sales-demo"
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
    assert html.count('class="widget kpi"') == 15 and html.count('class="widget chart"') == 14
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
    assert len(marked) == 8  # exactly the widgets that declare ignore_filters
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
