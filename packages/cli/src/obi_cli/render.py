"""Render a dashboard to one self-contained HTML page (Phase 0 renderer, ADR-007).

The page is a pure function of (project files, filter values, as-of date). `obi render`
writes it to disk. `obi serve` re-runs it per request, which makes filters and
`on_click` navigation work without a client-side runtime.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from markdown_it import MarkdownIt

from obi_engine import EngineError, Filter, Project, SemanticQuery, humanize
from obi_engine import dates
from obi_formats.artifacts import parse
from obi_formats.expr import ExprError, FilterSelection, render as render_expr

COLORS = {"ok": "#1a7f37", "warning": "#b26a00", "danger": "#c62828", "neutral": "#6b7280",
          "accent": "#2563eb"}
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
TIME_UNITS = {"day": "yearmonthdate", "week": "yearweek", "month": "yearmonth",
              "quarter": "yearquarter", "year": "year"}
_md = MarkdownIt("commonmark", {"html": False})
esc = html.escape


# Every property of the dashboard schema, per definition, that this renderer acts on.
# tests/test_coverage.py fails if the schema has a property that is neither listed here
# nor marked `x-unsupported` (which the validator reports as "not supported yet").
RENDERED: dict[str, set[str]] = {
    "dashboard": {"dashboard", "title", "description", "model", "filters", "pages"},
    "filter": {"filter", "title", "dimension", "default", "multiple", "pages"},
    "page": {"page", "title", "description", "visible", "items"},
    "row": {"row", "title", "description", "visible", "items"},
    "grid": {"grid", "title", "description", "columns", "visible", "items"},
    "tabs": {"tabs", "visible", "items"},
    "tab": {"tab", "title", "visible", "items"},
    "card": {"card", "title", "description", "visible", "items"},
    "section": {"section", "title", "description", "visible", "collapsed", "items"},
    "kpi": {"kpi", "title", "description", "measure", "target", "compare", "sparkline",
            "ignore_filters", "on_click", "visible", "color"},
    "chart": {"chart", "title", "description", "mark", "x", "y", "series", "detail", "stack", "where",
              "ignore_filters", "sort", "limit", "on_click", "visible", "color"},
    "table": {"table", "title", "description", "rows", "columns", "values", "where", "ignore_filters",
              "sort", "limit", "totals", "on_click", "visible", "cells"},
    "text": {"text", "title", "content", "visible"},
    "button": {"button", "label", "on_click", "visible"},
    "cell": {"color", "bar"},
    "on_click": {"navigate", "set"},
}


@dataclass
class RenderResult:
    html: str
    errors: list[str] = field(default_factory=list)  # widget-level problems, also shown on the page


class _Page:
    """Everything a widget needs while one dashboard is rendered."""

    def __init__(self, project: Project, dash_id: str, dash: dict, dashboards: dict[str, dict],
                 inputs: dict[str, list[str]] | None, as_of: date, mode: str,
                 drill: dict[str, list[str]] | None = None):
        self.project, self.dash_id, self.dash, self.dashboards = project, dash_id, dash, dashboards
        self.drill = drill or {}  # widget id -> values chosen at each hierarchy level
        self.eng = project.engine(dash["model"])
        self.as_of, self.mode = as_of, mode
        self.errors: list[str] = []
        self.charts: dict[str, dict] = {}
        self.filter_specs = {f["filter"]: f for f in dash.get("filters") or []}
        self.inputs: dict[str, list[str]] = {}
        for fid, f in self.filter_specs.items():
            if inputs is not None and fid in inputs:
                self.inputs[fid] = [v for v in inputs[fid] if v != ""]
            else:
                d = f.get("default")
                self.inputs[fid] = [] if d is None else [str(x) for x in (d if isinstance(d, list) else [d])]
        self.engine_filters: dict[str, Filter] = {}
        self.selections: dict[str, FilterSelection] = {}
        self.ranges: dict[str, tuple[date, date]] = {}
        for fid, f in self.filter_specs.items():
            self._prepare_filter(fid, f)

    def _prepare_filter(self, fid: str, f: dict):
        values = self.inputs[fid]
        ref = self.eng.idx.resolve(f["dimension"], ("dimension",))
        if not values:
            self.selections[fid] = FilterSelection()
            return
        if ref.field.dtype == "date":
            r = dates.resolve(values[0], self.as_of)
            self.ranges[fid] = r
            self.engine_filters[fid] = Filter(f["dimension"], range=r)
            self.selections[fid] = FilterSelection([r[0].isoformat(), r[1].isoformat()],
                                                   display=dates.label(values[0], r))
            return
        typed = [_typed(v, ref.field.dtype) for v in values]
        self.engine_filters[fid] = Filter(f["dimension"], values=typed)
        self.selections[fid] = FilterSelection(typed)

    # ---- filters per widget

    def applicable(self, page: str, ignore: list[str]) -> list[str]:
        out = []
        for fid, f in self.filter_specs.items():
            pages = f.get("pages")
            if pages and page not in pages:
                continue
            if fid in self.engine_filters and fid not in ignore:
                out.append(fid)
        return out

    def ignored_active(self, page: str, ignore: list[str]) -> list[str]:
        return [fid for fid in ignore if fid in self.engine_filters
                and (not self.filter_specs[fid].get("pages") or page in self.filter_specs[fid]["pages"])]

    def filter_title(self, fid: str) -> str:
        f = self.filter_specs[fid]
        return f.get("title") or self.eng.label(f["dimension"])

    # ---- expressions

    def ctx(self, value=None, row=None) -> dict:
        return {"value": value, "row": row or {}, "user": {}, "filters": self.selections}

    def expr(self, text, value=None, row=None, default=None):
        if text is None:
            return default
        try:
            return render_expr(str(text), self.ctx(value, row))
        except ExprError as e:
            self.errors.append(f"expression `{text}`: {e}")
            return default

    # ---- navigation

    def href(self, on_click: dict | None, row: dict | None = None) -> str | None:
        if not on_click:
            return None
        nav = on_click["navigate"]
        did, pid = nav.split("/", 1) if "/" in nav else (self.dash_id, nav)
        sets = {k: self.expr(v, row=row) for k, v in (on_click.get("set") or {}).items()}
        if self.mode == "static":
            return f"#{pid}" if did == self.dash_id else f"{did}.html#{pid}"
        # Same dashboard: the full filter state (`_set` marks it complete, so an empty filter
        # stays cleared). Another dashboard: only the `set` values, on top of its defaults.
        same = did == self.dash_id
        params: dict[str, list[str]] = dict(self.inputs) if same else {}
        for k, v in sets.items():
            params[k] = [] if v is None else [_plain(v)]
        pairs = ([("_set", "1")] if same else []) + [(k, v) for k, vs in params.items() for v in vs]
        pairs += [(k, "") for k, vs in params.items() if not vs]
        return f"/{did}?{urlencode(pairs)}#{pid}" if pairs else f"/{did}#{pid}"

    def drill_href(self, wid: str, path: list[str], page_id: str) -> str | None:
        """Link that re-renders with widget `wid` drilled to `path` (serve mode only)."""
        if self.mode == "static":
            return None
        pairs = [("_set", "1")] + [(k, v) for k, vs in self.inputs.items() for v in vs]
        pairs += [(k, "") for k, vs in self.inputs.items() if not vs]
        pairs += [(f"drill.{w}", v) for w, p in self.drill.items() if w != wid for v in p]
        pairs += [(f"drill.{wid}", v) for v in path]
        return f"/{self.dash_id}?{urlencode(pairs)}#{page_id}"


# =========================================================================== entry point


def render_dashboard(root: str | Path, dash_id: str, inputs: dict[str, list[str]] | None = None,
                     as_of: date | None = None, mode: str = "static",
                     drill: dict[str, list[str]] | None = None) -> RenderResult:
    root = Path(root)
    project = Project(root)
    dashboards = {}
    for p in sorted(root.glob("dashboards/*/dashboard.yaml")):
        d = parse(p.read_text(encoding="utf-8")).data
        dashboards[d["dashboard"]] = d
    dash = dashboards[dash_id]
    page = _Page(project, dash_id, dash, dashboards, inputs, as_of or date.today(), mode, drill)
    body = []
    for p in dash["pages"]:
        if p.get("visible") is not None and not page.expr(p["visible"], default=True):
            continue
        items = "".join(_node(page, n, p["page"]) for n in p["items"])
        desc = f'<p class="desc">{esc(p["description"])}</p>' if p.get("description") else ""
        body.append(f'<section class="page" id="{esc(p["page"])}" data-title="{esc(_title_of(p))}">'
                    f'{desc}{items}</section>')
    html_text = _document(page, "".join(body))
    return RenderResult(html_text, page.errors)


def dashboard_ids(root: str | Path) -> list[tuple[str, str]]:
    out = []
    for p in sorted(Path(root).glob("dashboards/*/dashboard.yaml")):
        d = parse(p.read_text(encoding="utf-8")).data
        out.append((d["dashboard"], d.get("title") or humanize(d["dashboard"])))
    return out


def filter_ids(root: str | Path, dash_id: str) -> list[str]:
    d = parse((Path(root) / "dashboards" / dash_id / "dashboard.yaml").read_text(encoding="utf-8")).data
    return [f["filter"] for f in d.get("filters") or []]


# =========================================================================== layout


def _title_of(node: dict) -> str:
    kind = next(iter(node))
    return node.get("title") or humanize(node[kind])


def _node(page: _Page, node: dict, page_id: str) -> str:
    kind = next(iter(node))
    nid = node[kind]
    if node.get("visible") is not None and not page.expr(node["visible"], default=True):
        return ""
    title = page.expr(node.get("title")) if node.get("title") else None
    children = lambda: "".join(_node(page, c, page_id) for c in node.get("items") or [])  # noqa: E731
    head = f"<h3>{esc(title)}</h3>" if title else ""
    if node.get("description") and kind in ("row", "grid", "card"):
        head += f'<p class="desc">{esc(node["description"])}</p>'
    if kind == "row":
        return f'<div class="row" id="w-{esc(nid)}">{head}<div class="row-items">{children()}</div></div>'
    if kind == "grid":
        cols = node["columns"]
        tpl = f"repeat({cols}, minmax(0, 1fr))" if isinstance(cols, int) else \
            " ".join(f"minmax(0, {c}fr)" for c in cols)
        return (f'<div class="grid-wrap" id="w-{esc(nid)}">{head}'
                f'<div class="grid" style="grid-template-columns: {tpl}">{children()}</div></div>')
    if kind == "card":
        return f'<div class="card" id="w-{esc(nid)}">{head}{children()}</div>'
    if kind == "section":
        open_ = "" if node.get("collapsed") else " open"
        desc_html = f'<p class="desc">{esc(node["description"])}</p>' if node.get("description") else ""
        return (f'<details class="section" id="w-{esc(nid)}"{open_}><summary>{esc(title or humanize(nid))}'
                f'</summary>{desc_html}{children()}</details>')
    if kind == "tabs":
        tabs = [t for t in node["items"] if t.get("visible") is None or page.expr(t["visible"], default=True)]
        buttons = "".join(f'<button type="button" data-tab="{esc(t["tab"])}">{esc(_title_of(t))}</button>'
                          for t in tabs)
        panels = "".join(f'<div class="tab-panel" data-tab="{esc(t["tab"])}">'
                         + "".join(_node(page, c, page_id) for c in t["items"]) + "</div>" for t in tabs)
        return f'<div class="tabs" id="w-{esc(nid)}"><div class="tab-bar">{buttons}</div>{panels}</div>'
    try:
        inner = {"kpi": _kpi, "chart": _chart, "table": _table, "text": _text, "button": _button}[kind](
            page, node, page_id)
    except Exception as e:  # one broken widget must not hide the rest of the dashboard
        msg = f"{kind} `{nid}`: {e}"
        page.errors.append(msg)
        inner = f'<div class="widget-error">Could not render {esc(kind)} <code>{esc(nid)}</code>: {esc(str(e))}</div>'
    info = (f'<span class="info" title="{esc(node["description"])}">&#9432;</span>'
            if node.get("description") else "")
    return f'<div class="widget {kind}" id="w-{esc(nid)}">{info}{_marker(page, node, page_id)}{inner}</div>'


def _marker(page: _Page, node: dict, page_id: str) -> str:
    """ADR-005: show which active filters this widget ignores."""
    ignored = page.ignored_active(page_id, node.get("ignore_filters") or [])
    if not ignored:
        return ""
    names = ", ".join(page.filter_title(f) for f in ignored)
    return f'<div class="ignored" title="These filters cannot apply to this widget\'s measures">' \
           f'Not filtered by: {esc(names)}</div>'


def _query(page: _Page, node: dict, page_id: str, measures, dims, sort=None, limit=None,
           extra: list[Filter] | None = None) -> list[dict]:
    fids = page.applicable(page_id, node.get("ignore_filters") or [])
    filters = [page.engine_filters[f] for f in fids] + (extra or [])
    for k, v in (node.get("where") or {}).items():
        filters.append(Filter(k, values=v if isinstance(v, list) else [v]))
    q = SemanticQuery(list(measures), list(dims), filters, [sort] if sort else [], limit)
    return page.eng.query(q).to_pylist()


# =========================================================================== widgets


def _kpi(page: _Page, w: dict, page_id: str) -> str:
    m, target = w["measure"], w.get("target")
    rows = _query(page, w, page_id, [m] + ([target] if target else []), [])
    row = rows[0] if rows else {}
    mname = _name(page, m)
    value = row.get(mname)
    fmt = _fmt_of(page, m)
    sub, color = "", "neutral"
    if target:
        t = row.get(_name(page, target))
        if t:
            sub = f"Target {_format(page, t, _fmt_of(page, target), compact=True)} · {value / t:.0%}" \
                if value is not None else ""
            color = "ok" if value is not None and value >= t else "danger"
    elif w.get("compare"):
        prev = _compare_value(page, w, page_id, m)
        if prev is not None and value is not None:
            word = "previous year" if w["compare"] == "previous_year" else "previous period"
            if fmt == "percent":
                delta = (value - prev) * 100
                sub = f"{'▲' if delta >= 0 else '▼'} {abs(delta):.1f} pp vs {word}"
            elif prev:
                delta = (value - prev) / abs(prev)
                sub = f"{'▲' if delta >= 0 else '▼'} {abs(delta):.1%} vs {word}"
            else:
                delta = 0
            color = "ok" if delta >= 0 else "danger"
        else:
            sub = "no comparison: set a date filter" if not page.ranges else "no data for comparison"
    if w.get("color"):
        color = page.expr(w["color"], value=value, row=row, default=color)
    title = page.expr(w.get("title")) if w.get("title") else page.eng.label(m)
    spark = ""
    if w.get("sparkline"):
        srows = _query(page, w, page_id, [m], [w["sparkline"]])
        spec = _spark_spec(page, srows, w["sparkline"], mname, color)
        spark = _chart_div(page, f"{w['kpi']}-spark", spec, height=36)
    body = (f'<div class="kpi-title">{esc(title)}</div>'
            f'<div class="kpi-value" style="color:{COLORS.get(color, COLORS["neutral"])}">'
            f'{esc(_format(page, value, fmt, compact=True))}</div>'
            f'<div class="kpi-sub">{esc(sub)}</div>')
    # `row` for a KPI holds its measure and target values, so `set` may use `${row.<measure>}`.
    href = page.href(w.get("on_click"), row)
    if href:
        body = f'<a class="kpi-link" href="{esc(href)}">{body}</a>'
    return body + spark


def _compare_value(page: _Page, w: dict, page_id: str, m: str):
    fids = page.applicable(page_id, w.get("ignore_filters") or [])
    date_f = [f for f in fids if f in page.ranges]
    if not date_f:
        return None
    fid = date_f[0]
    r = page.ranges[fid]
    shifted = dates.shift_year(r) if w["compare"] == "previous_year" else dates.shift_period(r)
    others = [page.engine_filters[f] for f in fids if f != fid]
    q = SemanticQuery([m], [], others + [Filter(page.filter_specs[fid]["dimension"], range=shifted)])
    rows = page.eng.query(q).to_pylist()
    return rows[0].get(_name(page, m)) if rows else None


def _drill(page: _Page, node: dict, refs: list[str], page_id: str):
    """Replace the first hierarchy among `refs` by its current drill level (ADR-007).
    Returns (refs, extra filters, breadcrumb html, drill info or None)."""
    wid = node[next(iter(node))]
    for i, ref in enumerate(refs):
        if not ref or not page.eng.idx.resolve(ref).hierarchy:
            continue
        levels = page.eng.idx.levels(page.eng.idx.resolve(ref))
        path = page.drill.get(wid, [])[:len(levels) - 1]
        extra = [Filter(levels[j].text, values=[_typed(v, levels[j].field.dtype)]) for j, v in enumerate(path)]
        level = levels[len(path)]
        crumbs = ""
        if path:
            links = [f'<a href="{esc(page.drill_href(wid, [], page_id) or "")}">All</a>']
            links += [f'<a href="{esc(page.drill_href(wid, path[:j + 1], page_id) or "")}">{esc(v)}</a>'
                      for j, v in enumerate(path)]
            crumbs = f'<div class="crumbs">{" &rsaquo; ".join(links)}</div>'
        info = {"wid": wid, "path": path, "level": level.field.name, "deeper": len(path) + 1 < len(levels)}
        return refs[:i] + [level.text] + refs[i + 1:], extra, crumbs, info
    return refs, [], "", None


def _drill_click(page: _Page, info, row: dict, page_id: str) -> str | None:
    if not info or not info["deeper"] or row.get(info["level"]) is None:
        return None
    return page.drill_href(info["wid"], info["path"] + [_plain(row[info["level"]])], page_id)


def _chart(page: _Page, c: dict, page_id: str) -> str:
    raw_ys = c["y"] if isinstance(c["y"], list) else [c["y"]]
    refs, extra, crumbs, info = _drill(page, c, [c.get("x") or ""] + raw_ys, page_id)
    x, ys = refs[0] or None, refs[1:]
    c = {**c, "x": x, "y": ys}
    fields = ([x] if x else []) + ys
    measures = [f for f in fields if _kind(page, f) == "measure"]
    dims = [f for f in fields if _kind(page, f) != "measure"]
    dims += [d for d in (c.get("series"), c.get("detail")) if d]
    rows = _query(page, c, page_id, measures, dims, _sort(page, c.get("sort")), c.get("limit"), extra)
    first_measure = _name(page, measures[0])
    for r in rows:
        if c.get("color"):
            r["_color"] = COLORS.get(page.expr(c["color"], value=r.get(first_measure), row=r), COLORS["neutral"])
        href = page.href(c.get("on_click"), r) or _drill_click(page, info, r, page_id)
        if href:
            r["_href"] = href
    spec = _chart_spec(page, c, rows, x, ys, measures)
    title = page.expr(c.get("title")) if c.get("title") else _default_title(page, c, x, ys)
    return f'<div class="widget-title">{esc(title)}</div>{crumbs}' + _chart_div(page, c["chart"], spec)


def _default_title(page: _Page, c: dict, x, ys) -> str:
    """ADR-002: '<y label> by <x label>'."""
    ylab = " and ".join(page.eng.label(y) for y in ys)
    by = x if x else c.get("series")
    if not by:
        return ylab
    if _kind(page, by) != "measure" and _dim_ref(page, by).grain:
        return f"{ylab} by {_dim_ref(page, by).grain}"   # "Net revenue by month"
    return f"{ylab} by {page.eng.label(by).lower()}"


def _chart_spec(page: _Page, c: dict, rows: list[dict], x, ys, measures) -> dict:
    data = [_json_row(r) for r in rows]
    enc: dict[str, Any] = {}
    mark: dict[str, Any] = {"type": c["mark"], "tooltip": True}
    transform: list = []
    folded = len([y for y in ys if y in measures]) > 1
    if c["mark"] == "arc":
        enc["theta"] = {"field": _name(page, ys[0]), "type": "quantitative", "stack": True}
        if c.get("series"):
            enc["color"] = {"field": _name(page, c["series"]), "type": "nominal",
                            "title": page.eng.label(c["series"]), "sort": None}
        mark["innerRadius"] = 50
    else:
        if folded:
            names = [_name(page, y) for y in ys]
            transform.append({"fold": names, "as": ["measure", "value"]})
            enc["y"] = {"field": "value", "type": "quantitative", "title": None,
                        "axis": {"format": _d3(page, ys[0])}}
            enc["color"] = {"field": "measure", "type": "nominal", "title": None,
                            "legend": {"labelExpr": _label_expr(page, ys)}}
        else:
            enc["y"] = _axis(page, ys[0])
        if x:
            enc["x"] = _axis(page, x)
        if c.get("series"):
            enc["color"] = {"field": _name(page, c["series"]), "type": "nominal",
                            "title": page.eng.label(c["series"]), "sort": None}
            if not c.get("stack") and c["mark"] in ("bar", "area"):
                dim_axis = "x" if x and _kind(page, x) != "measure" else "y"
                if c["mark"] == "bar":
                    enc[f"{dim_axis}Offset"] = {"field": _name(page, c["series"])}
                meas_axis = "y" if dim_axis == "x" else "x"
                enc[meas_axis]["stack"] = None
        if c.get("detail"):
            enc["detail"] = {"field": _name(page, c["detail"]), "type": "nominal"}
        if c["mark"] == "line":
            mark["point"] = True
    if c.get("color"):
        enc["color"] = {"field": "_color", "type": "nominal", "scale": None, "legend": None}
    enc["tooltip"] = [_tooltip(page, k) for k in ([x] if x else []) + ys + [c.get("series"), c.get("detail")]
                      if k and not (folded and k in measures)]
    if folded:
        enc["tooltip"] += [{"field": "measure", "title": "Measure"}, {"field": "value", "format": _d3(page, ys[0])}]
    spec = {"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "data": {"values": data},
            "mark": mark, "encoding": enc, "width": "container", "height": 260,
            "config": _vl_config()}
    if transform:
        spec["transform"] = transform
    return spec


def _axis(page: _Page, ref: str) -> dict:
    name = _name(page, ref)
    kind = _kind(page, ref)
    if kind == "measure":
        return {"field": name, "type": "quantitative", "title": page.eng.label(ref),
                "axis": {"format": _d3(page, ref)}}
    r = _dim_ref(page, ref)
    if r.field.dtype == "date":
        return {"field": name, "type": "temporal", "timeUnit": TIME_UNITS[r.grain or "day"],
                "title": page.eng.label(ref)}
    return {"field": name, "type": "nominal", "sort": None, "title": page.eng.label(ref)}


def _tooltip(page: _Page, ref: str) -> dict:
    t = _axis(page, ref)
    out = {"field": t["field"], "type": t["type"], "title": t.get("title")}
    if "timeUnit" in t:
        out["timeUnit"] = t["timeUnit"]
    if t["type"] == "quantitative":
        out["format"] = t["axis"]["format"]
    return out


def _label_expr(page: _Page, ys: list[str]) -> str:
    parts = " : ".join(f"datum.label == '{_name(page, y)}' ? '{page.eng.label(y)}'" for y in ys)
    return f"{parts} : datum.label"


def _spark_spec(page: _Page, rows, dim, mname, color) -> dict:
    return {"$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "data": {"values": [_json_row(r) for r in rows]},
            "mark": {"type": "area", "line": {"color": COLORS.get(color, COLORS["neutral"])},
                     "color": COLORS.get(color, COLORS["neutral"]), "opacity": 0.15},
            "encoding": {"x": {"field": _name(page, dim), "type": "temporal", "axis": None},
                         "y": {"field": mname, "type": "quantitative", "axis": None}},
            "width": "container", "height": 36, "config": {**_vl_config(), "view": {"stroke": None}}}


def _table(page: _Page, t: dict, page_id: str) -> str:
    rows_dims, extra, crumbs, info = _drill(page, t, list(t.get("rows") or []), page_id)
    col_dims = list(t.get("columns") or [])
    values = list(t["values"])
    # With pivot columns, `limit` counts output rows (row keys), so it applies after grouping.
    limit = None if col_dims else t.get("limit")
    data = _query(page, t, page_id, values, rows_dims + col_dims, _sort(page, t.get("sort")), limit, extra)
    rnames = [_name(page, d) for d in rows_dims]
    cnames = [_name(page, d) for d in col_dims]
    vnames = [_name(page, v) for v in values]
    cells_cfg = t.get("cells") or {}
    cell_of = {(_name(page, k)): v for k, v in cells_cfg.items()}

    # Pivot: one output row per row-key; one column per (column-key, value).
    col_keys = list(dict.fromkeys(tuple(r[c] for c in cnames) for r in data)) if cnames else [()]
    grouped: dict[tuple, dict] = {}
    for r in data:
        rk = tuple(r[n] for n in rnames)
        g = grouped.setdefault(rk, {"_row": {n: r[n] for n in rnames}, "_cells": {}})
        ck = tuple(r[c] for c in cnames)
        for v in vnames:
            g["_cells"][(ck, v)] = r.get(v)
        g["_row"].update({v: r.get(v) for v in vnames} if not cnames else {})
    if col_dims and t.get("limit"):
        grouped = dict(list(grouped.items())[:t["limit"]])
    columns = [(ck, v) for ck in col_keys for v in vnames]
    maxabs = {col: max((abs(g["_cells"].get(col) or 0) for g in grouped.values()), default=0) or 1
              for col in columns}

    def col_label(ck, v) -> str:
        parts = [_dim_text(page, col_dims[i], x) for i, x in enumerate(ck)]
        vlabel = page.eng.label(values[vnames.index(v)])
        return " · ".join(parts + ([vlabel] if len(vnames) > 1 or not parts else []))

    head = "".join(f"<th>{esc(page.eng.label(d))}</th>" for d in rows_dims)
    head += "".join(f'<th class="num">{esc(col_label(ck, v))}</th>' for ck, v in columns)
    body = []
    for rk, g in grouped.items():
        href = page.href(t.get("on_click"), g["_row"]) or _drill_click(page, info, g["_row"], page_id)
        attrs = f' class="link" data-href="{esc(href)}"' if href else ""
        tds = "".join(f"<td>{esc(_dim_text(page, rows_dims[i], x))}</td>" for i, x in enumerate(rk))
        for ck, v in columns:
            val = g["_cells"].get((ck, v))
            tds += _cell(page, val, _fmt_of(page, values[vnames.index(v)]), cell_of.get(v),
                         {**g["_row"], **{c: ck[i] for i, c in enumerate(cnames)}, v: val}, maxabs[(ck, v)])
        body.append(f"<tr{attrs}>{tds}</tr>")
    foot = ""
    if t.get("totals"):
        tot = _query(page, t, page_id, values, col_dims, extra=extra)
        totals = {(tuple(r[c] for c in cnames), v): r.get(v) for r in tot for v in vnames}
        tds = f'<td colspan="{len(rows_dims)}">Total</td>' if rows_dims else ""
        tds += "".join(f'<td class="num">{esc(_format(page, totals.get(col), _fmt_of(page, values[vnames.index(col[1])])))}</td>'
                       for col in columns)
        foot = f"<tfoot><tr>{tds}</tr></tfoot>"
    title = page.expr(t.get("title")) if t.get("title") else humanize(t["table"])
    return (f'<div class="widget-title">{esc(title)}</div>{crumbs}<div class="table-scroll"><table>'
            f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody>{foot}</table></div>')


def _cell(page: _Page, val, fmt: str, cfg: dict | None, row: dict, maxabs: float) -> str:
    style = ""
    if cfg and cfg.get("color"):
        token = page.expr(cfg["color"], value=val, row=row)
        if token in COLORS:
            style += f"color:{COLORS[token]};font-weight:600;"
    if cfg and cfg.get("bar") and isinstance(val, (int, float)):
        pct = max(0.0, min(100.0, abs(val) / maxabs * 100))
        style += (f"background:linear-gradient(90deg, rgba(37,99,235,.14) {pct:.1f}%, transparent {pct:.1f}%);")
    return f'<td class="num" style="{style}">{esc(_format(page, val, fmt))}</td>'


def _text(page: _Page, t: dict, page_id: str) -> str:
    content = page.expr(t["content"], default=t["content"])
    title = f'<div class="widget-title">{esc(page.expr(t["title"]))}</div>' if t.get("title") else ""
    return title + f'<div class="markdown">{_md.render(str(content))}</div>'


def _button(page: _Page, b: dict, page_id: str) -> str:
    href = page.href(b["on_click"])
    return f'<a class="button" href="{esc(href)}">{esc(page.expr(b["label"], default=b["label"]))}</a>'


# =========================================================================== helpers


def _sort(page: _Page, sort: str | None) -> str | None:
    """A sort on a hierarchy name sorts by the level on screen, i.e. the default order."""
    if not sort or page.eng.idx.resolve(sort.lstrip("-")).hierarchy:
        return None
    return sort


def _kind(page: _Page, ref: str) -> str:
    return page.eng.idx.resolve(ref).kind


def _dim_ref(page: _Page, ref: str):
    r = page.eng.idx.resolve(ref, ("dimension", "hierarchy"))
    return page.eng.idx.levels(r)[0] if r.hierarchy else r


def _name(page: _Page, ref: str) -> str:
    """Column name of a field in query results (hierarchies show their top level)."""
    r = page.eng.idx.resolve(ref)
    if r.hierarchy:
        return page.eng.idx.levels(r)[0].field.name
    return r.field.name


def _fmt_of(page: _Page, ref: str) -> str:
    r = page.eng.idx.resolve(ref)
    return r.field.spec.get("format", "number") if r.field and r.kind == "measure" else "text"


def _d3(page: _Page, ref: str) -> str:
    fmt = _fmt_of(page, ref)
    return {"percent": ".0%", "currency": "$~s", "integer": ",.0f"}.get(fmt, "~s")


def _format(page: _Page, v, fmt: str, compact: bool = False) -> str:
    if v is None:
        return "–"
    if fmt == "text" or not isinstance(v, (int, float)) or isinstance(v, bool):
        return str(v)
    if fmt == "percent":
        return f"{v * 100:.1f}%"
    sym = SYMBOLS.get(page.project.currency, page.project.currency + " ") if fmt == "currency" else ""
    if compact and abs(v) >= 1000:
        for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if abs(v) >= div:
                return f"{'-' if v < 0 else ''}{sym}{abs(v) / div:.1f}{suffix}"
    if fmt in ("integer", "currency") or float(v).is_integer():
        return f"{'-' if v < 0 else ''}{sym}{abs(v):,.0f}"
    return f"{v:,.2f}"


def _dim_text(page: _Page, ref: str, v) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (date, datetime)):
        grain = _dim_ref(page, ref).grain or "day"
        return {"year": f"{v.year}", "quarter": f"{v.year} Q{(v.month - 1) // 3 + 1}",
                "month": v.strftime("%b %Y"), "week": f"Week of {v.isoformat()}"}.get(grain, v.isoformat())
    return str(v)


def _json_row(r: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in r.items()}


def _typed(v: str, dtype: str):
    if dtype == "number":
        return float(v)
    if dtype == "boolean":
        return v.lower() in ("true", "yes", "1")
    return v


def _plain(v) -> str:
    return "true" if v is True else "false" if v is False else str(v)


def _vl_config() -> dict:
    return {"font": "Inter, system-ui, sans-serif", "axis": {"labelColor": "#4b5563", "titleColor": "#4b5563",
            "gridColor": "#eef0f3", "domainColor": "#d1d5db"}, "view": {"stroke": None},
            "range": {"category": ["#2563eb", "#0d9488", "#d97706", "#9333ea", "#dc2626", "#64748b"]},
            "legend": {"orient": "bottom"}}


def _chart_div(page: _Page, cid: str, spec: dict, height: int = 260) -> str:
    key = f"vl-{cid}"
    page.charts[key] = spec
    return f'<div class="vl" id="{esc(key)}" style="min-height:{height}px"></div>'


# =========================================================================== document


def _filter_bar(page: _Page) -> str:
    items = []
    for fid, f in page.filter_specs.items():
        pages = " ".join(f.get("pages") or [])
        title = esc(page.filter_title(fid))
        if page.mode == "static":
            label = page.selections[fid].label or "All"
            items.append(f'<span class="chip" data-pages="{esc(pages)}"><b>{title}</b> {esc(label)}</span>')
            continue
        ref = page.eng.idx.resolve(f["dimension"], ("dimension",))
        if ref.field.dtype == "date":
            current = page.inputs[fid][0] if page.inputs[fid] else ""
            opts = ['<option value="">All dates</option>'] + [
                f'<option value="{p}"{" selected" if p == current else ""}>{esc(dates.label(p, dates.resolve(p, page.as_of)))}</option>'
                for p in dates.PRESETS]
            if current and current not in dates.PRESETS:
                opts.append(f'<option value="{esc(current)}" selected>{esc(current)}</option>')
            control = f'<select name="{esc(fid)}">{"".join(opts)}</select>'
        else:
            chosen = set(page.inputs[fid])
            values = [_plain(v) for v in page.eng.distinct(f["dimension"])]
            if f.get("multiple", True):
                # Checkbox list in a dropdown: the summary always shows the real selection.
                boxes = "".join(f'<label><input type="checkbox" name="{esc(fid)}" value="{esc(v)}"'
                                f'{" checked" if v in chosen else ""}> {esc(v)}</label>' for v in values)
                summary = esc(page.selections[fid].label or "All")
                items.append(f'<details class="filter multi" data-pages="{esc(pages)}"><summary><b>{title}</b> '
                             f'{summary}</summary><div class="options">{boxes}</div></details>')
                continue
            opts = "".join(f'<option value="{esc(v)}"{" selected" if v in chosen else ""}>{esc(v)}</option>'
                           for v in values)
            control = f'<select name="{esc(fid)}"><option value="">All</option>{opts}</select>'
        items.append(f'<label class="filter" data-pages="{esc(pages)}"><b>{title}</b>{control}</label>')
    if page.mode == "static":
        return f'<div class="filters">{"".join(items)}</div>'
    return (f'<form class="filters" method="get"><input type="hidden" name="_set" value="1">{"".join(items)}'
            f'<button type="submit">Apply</button><a href="/{esc(page.dash_id)}">Reset</a></form>')


def _document(page: _Page, body: str) -> str:
    d = page.dash
    tabs = "".join(f'<a href="#{esc(p["page"])}" data-page="{esc(p["page"])}">{esc(_title_of(p))}</a>'
                   for p in d["pages"])
    css = files("obi_cli").joinpath("assets/style.css").read_text("utf-8")
    js = files("obi_cli").joinpath("assets/app.js").read_text("utf-8")
    charts = json.dumps(page.charts, default=str).replace("</", "<\\/")
    errors = ""
    if page.errors:
        errors = '<div class="render-errors"><b>Render problems</b><ul>' + \
            "".join(f"<li>{esc(e)}</li>" for e in page.errors) + "</ul></div>"
    mode_note = ("Static render: filters are fixed. Run <code>obi serve</code> to change them."
                 if page.mode == "static" else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(d.get("title") or d["dashboard"])}</title>
<style>{css}</style>
<script src="https://cdn.jsdelivr.net/npm/vega@5.30.0"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5.21.0"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6.26.0"></script>
</head><body>
<header><div class="title"><h1>{esc(d.get("title") or d["dashboard"])}</h1>
<p>{esc(d.get("description") or "")}</p></div>
<div class="meta">As of {page.as_of.isoformat()} · model {esc(page.eng.idx.title or "")} <code>{esc(d["model"])}</code></div></header>
{_filter_bar(page)}
<nav class="pages">{tabs}</nav>
{errors}<main>{body}</main>
<footer>{mode_note}</footer>
<script id="charts" type="application/json">{charts}</script>
<script>{js}</script>
</body></html>
"""
