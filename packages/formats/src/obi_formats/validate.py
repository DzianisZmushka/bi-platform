"""Project validator. Every issue names the artifact, the element and the fix.

Order of work:
1. Parse each file (YAML subset, ADR-002).
2. Structure: walk elements, detect each element's kind by its kind key, and validate
   it against that kind's own schema definition (never a oneOf across kinds).
3. Semantics: model references, join paths and grain, dashboard bindings, filters,
   navigation and expressions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import catalog as catalog_mod
from .artifacts import parse
from .expr import FILTER_MEMBERS, ExprError, is_expression, parse_template
from .modelindex import ModelIndex, Ref, RefError, did_you_mean
from .schema import Schema, artifact_type, get_schema
from .yamlio import Document, FormatError

THEME_COLORS = ("ok", "warning", "danger", "neutral", "accent")
FISCAL_PRESETS = ("this_fiscal_year", "last_fiscal_year", "fiscal_year_to_date", "this_fiscal_period",
                  "last_fiscal_period")
DATE_PRESETS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month",
                "this_quarter", "last_quarter", "this_year", "last_year", "year_to_date",
                "last_7_days", "last_30_days", "last_90_days", "last_12_months")


@dataclass(frozen=True)
class Issue:
    severity: str
    file: str
    line: int | None
    artifact: str
    element: str | None
    message: str

    @property
    def text(self) -> str:
        where = f"{self.artifact}, {self.element}" if self.element else self.artifact
        return f"{where}: {self.message}"

    def __str__(self) -> str:
        return f"{self.file}:{self.line or 1}: {self.severity}: {self.text}"


class _Reporter:
    def __init__(self, issues: list[Issue], file: str, artifact: str, doc: Document | None):
        self.issues, self.file, self.artifact, self.doc = issues, file, artifact, doc

    def __call__(self, path: tuple, element: str | None, message: str, severity: str = "error"):
        line = self.doc.line_of(path) if self.doc else 1
        self.issues.append(Issue(severity, self.file, line, self.artifact, element, message))


@dataclass
class _Artifact:
    type: str
    file: str
    id: str
    doc: Document
    report: _Reporter

    @property
    def data(self) -> Any:
        return self.doc.data


# =========================================================================== entry points


def validate_project(root: str | Path, overrides: dict[str, str] | None = None) -> list[Issue]:
    """Validate every artifact under `root`. `overrides` maps project-relative paths
    to replacement text (used by tests and by editors validating unsaved buffers)."""
    root = Path(root)
    overrides = dict(overrides or {})
    issues: list[Issue] = []

    rels = ["project.yaml"]
    rels += sorted(p.relative_to(root).as_posix() for p in root.glob("model/*.model.yaml"))
    rels += sorted(p.relative_to(root).as_posix() for p in root.glob("dashboards/*/dashboard.yaml"))
    rels += sorted(p.relative_to(root).as_posix() for p in root.glob("forms/*.form.yaml"))
    rels += [r for r in overrides if r not in rels and artifact_type(r)]

    arts: list[_Artifact] = []
    for rel in rels:
        if rel in overrides:
            text = overrides[rel]
        elif (root / rel).is_file():
            text = (root / rel).read_text(encoding="utf-8")
        else:
            issues.append(Issue("error", rel, 1, f"project `{root.name}`", None,
                                "project.yaml not found; every project needs one at its root"))
            continue
        art = _load(rel, text, issues)
        if art:
            arts.append(art)

    for art in arts:
        _check_structure(art)

    project = next((a for a in arts if a.type == "project"), None)
    connection, catalog = _catalog(root, project.data if project else {}, issues)

    models: dict[str, ModelIndex] = {}
    for art in (a for a in arts if a.type == "model"):
        stem = Path(art.file).name.removesuffix(".model.yaml")
        if art.id != stem:
            art.report(("model",), None, f"model id `{art.id}` does not match its file name "
                                          f"`{stem}.model.yaml`; rename one of them")
        idx = ModelIndex(art.id, art.data)
        idx.check(art.report)
        if catalog is not None:
            idx.check_catalog(catalog, connection, art.report)
        models[art.id] = idx

    for art in (a for a in arts if a.type == "project"):
        _check_project(art, root)

    forms = {a.id: a for a in arts if a.type == "form"}
    for art in forms.values():
        stem = Path(art.file).name.removesuffix(".form.yaml")
        if art.id != stem:
            art.report((), None, f"id `{art.id}` does not match its file name `{stem}.form.yaml`; rename one of them")
        _check_form(art, models)
    for art in (a for a in arts if a.type == "project"):
        _check_seeds(art, models, root)
    dashboards = {a.id: a.data for a in arts if a.type == "dashboard"}
    # Dashboards whose file did not load already have their own error; links to them are
    # not reported again.
    loaded = {a.file for a in arts}
    broken = {Path(r).parent.name for r in rels if artifact_type(r) == "dashboard" and r not in loaded}
    for art in (a for a in arts if a.type == "dashboard"):
        folder = Path(art.file).parent.name
        if art.id != folder:
            art.report(("dashboard",), None, f"dashboard id `{art.id}` does not match its folder "
                                              f"`{folder}`; rename one of them")
        _DashboardCheck(art, models, dashboards, broken, forms).run()

    return sorted(issues, key=lambda i: (i.file, i.line or 0))


def _load(rel: str, text: str, issues: list[Issue]) -> _Artifact | None:
    atype = artifact_type(rel)
    fallback = (Path(rel).name.removesuffix(".model.yaml") if atype == "model"
                else Path(rel).name.removesuffix(".form.yaml") if atype == "form"
                else Path(rel).parent.name if atype == "dashboard" else "project")
    try:
        doc = parse(text)
    except FormatError as e:
        issues.append(Issue("error", rel, e.line, f"{atype} `{fallback}`", None, e.message))
        return None
    data = doc.data
    if not isinstance(data, dict):
        issues.append(Issue("error", rel, 1, f"{atype} `{fallback}`", None,
                            f"the file must be a mapping that starts with `{atype}: <id>`"))
        return None
    if atype == "dashboard" and ("entities" in data or "hierarchies" in data) and "pages" not in data:
        # One clear error instead of a list of unknown and missing properties.
        issues.append(Issue("error", rel, 1, f"dashboard `{fallback}`", None,
                            "this file looks like a model, not a dashboard (it has `entities` or "
                            "`hierarchies`); models live in `model/<id>.model.yaml`"))
        return None
    key = ("form" if "form" in data else "sheet") if atype == "form" else atype
    aid = data.get(key) if isinstance(data.get(key), str) else fallback
    label = f"{key} `{aid}`"
    for path, dup, line, first in doc.duplicates:
        issues.append(Issue("error", rel, line, label, _element_of(path),
                            f"`{dup}` appears twice{f' in `{path[-1]}`' if path and isinstance(path[-1], str) else ''} "
                            f"(first on line {first}); the first is used, remove one"))
    return _Artifact(atype, rel, aid, doc, _Reporter(issues, rel, label, doc))


_ELEMENTS = {"entities": "entity", "measures": "measure", "dimensions": "dimension", "columns": "column",
             "rollups": "rollup", "hierarchies": "hierarchy", "transitions": "transition", "connections": "connection",
             "tables": "table"}


def _element_of(path: tuple) -> str | None:
    """`entity `shipment_plans`` for a duplicate inside ("entities", "shipment_plans", "columns")."""
    for i in range(len(path) - 2, -1, -1):
        if path[i] in _ELEMENTS and isinstance(path[i + 1], str):
            return f"{_ELEMENTS[path[i]]} `{path[i + 1]}`"
    return None


def _catalog(root: Path, project: dict, issues: list[Issue]) -> tuple[str, dict[str, list[str] | None] | None]:
    """Columns per table for static validation. From the committed `catalog.json` when there is
    one (no data access, as in CI); otherwise from local data files, with a warning (ADR-008)."""
    name = project.get("default_connection")
    conns = project.get("connections")
    conn = conns.get(name) if isinstance(conns, dict) and isinstance(name, str) else None
    cached = catalog_mod.load(root)
    if cached is not None:
        if isinstance(conn, dict) and cached.get("connection") != name:  # a bad default is reported elsewhere
            issues.append(Issue("warning", catalog_mod.CATALOG_FILE, 1, f"project `{root.name}`", None,
                                f"catalog.json describes connection `{cached.get('connection')}`, not the "
                                f"default `{name}`; run `obi catalog`"))
        return str(name), catalog_mod.columns(cached)
    if not isinstance(conn, dict) or not isinstance(conn.get("tables"), dict):
        return str(name), None
    issues.append(Issue("warning", catalog_mod.CATALOG_FILE, 1, f"project `{root.name}`", None,
                        "no catalog.json: columns were read from the data files; run `obi catalog` so "
                        "validation needs no data access"))
    return name, catalog_mod.from_files(root, conn)


# =========================================================================== structure


def _check_structure(art: _Artifact):
    schema = get_schema(art.type)
    root = schema.root_name_for(art.data)
    if art.type == "form" and root == schema.root_name:
        art.report((), None, "a form file starts with `form: <id>` (one record) or `sheet: <id>` (many rows)")
        return
    _check_element(schema, art.report, art.data, root, (), None)
    _check_unsupported(schema, art.report, art.data, schema.defs[root], ())


def _check_unsupported(schema: Schema, report: _Reporter, value: Any, s: dict, path: tuple):
    """A property marked `x-unsupported` is accepted by the schema (so the message can be
    precise) but has no runtime behaviour yet: using it is an error, never silently ignored."""
    s = schema.resolve(s)
    if isinstance(value, list):
        items = s.get("items", {})
        for i, item in enumerate(value):
            _check_unsupported(schema, report, item, schema.item(items, item) if items else {}, path + (i,))
        return
    if not isinstance(value, dict):
        return
    for k, v in value.items():
        sub = schema.prop(s, k)
        if "x-unsupported" in sub:
            label, rest = _locate(schema, schema.root_name, list(path) + [k])
            report(path + (k,), label, f"`{_path_text(rest)}` is not supported yet: {sub['x-unsupported']}")
            continue
        _check_unsupported(schema, report, v, sub, path + (k,))


def _check_element(schema: Schema, report: _Reporter, value: Any, def_name: str, path: tuple,
                   element: str | None):
    for err in schema.validator(def_name).iter_errors(value):
        for label, rel, message in _describe(schema, err, def_name):
            report(path + tuple(rel), label or element, message)
    if not isinstance(value, dict):
        return
    for prop, kinds in schema.element_arrays(def_name).items():
        items = value.get(prop)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            ipath = path + (prop, i)
            if not isinstance(item, dict):
                continue  # the shallow schema already reported the type
            found = [k for k in item if k in kinds]
            if not found:
                first = next(iter(item), None)
                elsewhere = [k for k in item if k in schema.element_keys]
                if elsewhere:
                    msg = (f"`{elsewhere[0]}` is not allowed in `{prop}` here; "
                           f"use one of {', '.join(kinds)}")
                else:
                    msg = (f"unknown element kind `{first}`; the first key must be one of "
                           f"{', '.join(kinds)}" + did_you_mean(str(first), kinds))
                report(ipath, element, msg)
                continue
            kind = found[0]
            kid = item[kind]
            label = f"{schema.defs[kinds[kind]]['x-element']} `{kid}`"
            if len(found) > 1:
                report(ipath, label, f"has more than one kind key ({', '.join(found)}); keep exactly one")
            _check_element(schema, report, item, kinds[kind], ipath, label)


_JSON_TYPES = {"string": "text", "integer": "a whole number", "number": "a number",
               "boolean": "true or false", "array": "a list", "object": "a mapping", "null": "empty"}


def _type_name(v: Any) -> str:
    if v is None:
        return "empty"
    if isinstance(v, bool):
        return "true/false"
    if isinstance(v, int):
        return "a whole number"
    if isinstance(v, float):
        return "a number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, list):
        return "a list"
    return "a mapping"


def _describe(schema: Schema, err, def_name: str) -> list[tuple[str | None, list, str]]:
    rel = list(err.absolute_path)
    label, rest = _locate(schema, def_name, rel)
    where = _path_text(rest)
    v = err.validator
    if v == "required":
        name = re.match(r"'(.+?)' is a required property", err.message)
        name = name.group(1) if name else err.message
        return [(label, rel, f"missing required property `{name}`" + (f" in `{where}`" if where else ""))]
    if v == "additionalProperties":
        allowed = list(err.schema.get("properties", {}))
        out = []
        for k in err.instance:
            if k in allowed:
                continue
            free = [a for a in allowed if a not in err.instance]
            hint = did_you_mean(k, free) or f"; allowed: {', '.join(allowed)}"
            prefix = f"`{where}`: " if where else ""
            out.append((label, rel + [k], f"{prefix}unknown property `{k}`{hint}"))
        return out
    if v == "enum":
        opts = ", ".join(str(o) for o in err.validator_value)
        return [(label, rel, f"`{where}` must be one of {opts}; got `{err.instance}`"
                 + did_you_mean(str(err.instance), [str(o) for o in err.validator_value]))]
    if v == "type":
        expected = err.validator_value if isinstance(err.validator_value, list) else [err.validator_value]
        exp_text = " or ".join(_JSON_TYPES.get(t, t) for t in expected)
        subject = f"`{where}`" if where else "this element"
        return [(label, rel, f"{subject} must be {exp_text}, got {_type_name(err.instance)}")]
    if v == "pattern":
        text = err.schema.get("x-error") or f"does not match `{err.validator_value}`"
        in_key = "propertyNames" in err.relative_schema_path
        prefix = f"`{where}`: " if where and in_key else ""
        return [(label, rel, f"{prefix}`{err.instance}` {text}")]
    if v in ("minItems", "minProperties", "minLength"):
        if v == "minItems" and err.validator_value > 1:
            return [(label, rel, f"`{where}` needs at least {err.validator_value} entries")]
        return [(label, rel, f"`{where}` must not be empty")]
    if v in ("minimum", "exclusiveMinimum"):
        word = "at least" if v == "minimum" else "greater than"
        return [(label, rel, f"`{where}` must be {word} {err.validator_value}")]
    if v == "uniqueItems":
        return [(label, rel, f"`{where}` lists the same value twice")]
    return [(label, rel, err.message)]


def _locate(schema: Schema, def_name: str, rel: list) -> tuple[str | None, list]:
    """Find the innermost named element (entity, measure, ...) on an error path."""
    s: Any = schema.defs[def_name]
    label, start = None, 0
    for i, seg in enumerate(rel):
        s = schema.resolve(s)
        if isinstance(seg, int):
            s = schema.items(s)
            continue
        props = s.get("properties") or {}
        if seg in props:
            s = props[seg]
            continue
        ap = s.get("additionalProperties")
        if isinstance(ap, dict):
            s = schema.resolve(ap)
            if "x-element" in s:
                label, start = f"{s['x-element']} `{seg}`", i + 1
            continue
        break
    return label, rel[start:]


def _path_text(rest: list) -> str:
    out = ""
    for seg in rest:
        out += f"[{seg}]" if isinstance(seg, int) else (f".{seg}" if out else str(seg))
    return out


# =========================================================================== project


def _check_project(art: _Artifact, root: Path):
    d = art.data
    conns = d.get("connections") if isinstance(d.get("connections"), dict) else {}
    default = d.get("default_connection")
    if isinstance(default, str) and conns and default not in conns:
        art.report(("default_connection",), None,
                   f"default_connection `{default}` is not one of the connections"
                   + did_you_mean(default, conns))
    kinds = get_schema("project").defs["connection"]["x-kind-keys"]
    for cname, c in conns.items():
        if not isinstance(c, dict):
            continue
        present = [k for k in kinds if k in c]
        if len(present) != 1:
            art.report(("connections", cname), f"connection `{cname}`",
                       f"needs exactly one connection kind ({', '.join(kinds)})")
        if not isinstance(c.get("tables"), dict):
            continue
        for table, rel in c["tables"].items():
            if isinstance(rel, str) and not (root / rel).is_file():
                art.report(("connections", cname, "tables", table), f"connection `{cname}`",
                           f"table `{table}` points to `{rel}`, which does not exist")


# =========================================================================== forms (ADR-012)

AUDIT = ("created_by", "created_at", "updated_by", "updated_at")
_WIDGET_TYPES = {"text": ("string", "text"), "textarea": ("string", "text"), "number": ("number", "integer", "id"),
                 "date": ("date", "timestamp"), "checkbox": ("boolean",), "select": None}


class _AnyFilter(list):
    """Filter ids in a form are checked against each dashboard that embeds it, not here."""

    def __contains__(self, item):
        return True


_ANY = _AnyFilter()


def _form_entity(models: dict[str, ModelIndex], name):
    found = [m for m in models.values() if name in m.entities]
    return (found[0], found[0].entities[name]) if len(found) == 1 else (None, None)


def _check_form(art: _Artifact, models: dict[str, ModelIndex]):
    d, report = art.data, art.report
    entity = d.get("entity")
    idx, e = _form_entity(models, entity)
    if idx is None:
        pool = {n for m in models.values() for n in m.entities}
        report(("entity",), None, f"entity `{entity}` not found in any model" + did_you_mean(str(entity), pool))
        return
    if not idx.is_writable(entity):
        report(("entity",), None, f"entity `{entity}` is not writable; forms write only to entities with "
                                  f"`writable: true`")
        return
    cols = idx.table_columns(entity)
    declared = e.get("columns") if isinstance(e.get("columns"), dict) else {}
    status = (e.get("workflow") or {}).get("column") if isinstance(e.get("workflow"), dict) else None

    def column(name, path, label):
        if name in AUDIT:
            report(path, label, f"`{name}` is an audit column; the platform writes it")
            return False
        if name not in cols:
            report(path, label, f"entity `{entity}` has no column `{name}`"
                   + did_you_mean(str(name), [c for c in cols if c not in AUDIT]))
            return False
        return True

    if "form" in d:
        for i, f in enumerate(d.get("fields") or []):
            if not isinstance(f, dict) or not isinstance(f.get("field"), str):
                continue
            name, path, label = f["field"], ("fields", i), f"field `{f['field']}`"
            if not column(name, path, label):
                continue
            if name == status:
                report(path, label, f"`{name}` is the workflow column; it changes only through transitions, "
                                    f"which the form shows as buttons")
                continue
            spec = declared.get(name) if isinstance(declared.get(name), dict) else {"type": "string"}
            ctype = spec.get("type", "string")
            widget = f.get("widget")
            if widget == "select" and not (spec.get("choices") or spec.get("references")):
                report(path + ("widget",), label, f"widget `select` needs `choices` or `references` on column `{name}`")
            elif widget and _WIDGET_TYPES.get(widget) is not None and ctype not in _WIDGET_TYPES[widget]:
                usable = [w for w, ts in _WIDGET_TYPES.items() if ts and ctype in ts]
                report(path + ("widget",), label, f"widget `{widget}` does not fit column type `{ctype}`; "
                                                  f"use {' or '.join(usable)}")
            if f.get("required") is False and spec.get("required") is True:
                report(path + ("required",), label, f"column `{name}` is required; a form may make a column "
                                                    f"required, not optional")
            for msg in check_expression(f.get("default"), names=("filters", "user"), filter_ids=_ANY):
                report(path + ("default",), label, f"`default`: {msg}")
    else:
        shown = [c for c in d.get("columns") or [] if isinstance(c, str)]
        for i, c in enumerate(shown):
            column(c, ("columns", i), None)
        for i, c in enumerate(d.get("readonly") or []):
            if c not in shown:
                report(("readonly", i), None, f"`readonly`: `{c}` is not one of the sheet's `columns`")
        for i, f in enumerate(d.get("filters") or []):
            if isinstance(f, dict) and isinstance(f.get("column"), str):
                if column(f["column"], ("filters", i, "column"), f"filter `{f.get('filter')}`"):
                    _check_sheet_filter_default(f, idx, entity, cols, ("filters", i, "default"), report)
        sort = d.get("sort")
        if isinstance(sort, str) and sort.lstrip("-") not in cols:
            report(("sort",), None, f"`sort`: `{sort.lstrip('-')}` is not a column of entity `{entity}`"
                   + did_you_mean(sort.lstrip("-"), cols))


def _check_sheet_filter_default(f: dict, idx: ModelIndex, entity: str, cols: dict, path: tuple, report):
    """A sheet filter's default on a date column must be something the sheet can evaluate: a
    date, a range, a preset, or a fiscal preset when the column joins a fiscal calendar."""
    default, column = f.get("default"), f["column"]
    if cols.get(column) not in ("date", "timestamp") or not isinstance(default, str) or "${" in default:
        return
    label = f"filter `{f.get('filter')}`"
    fiscal = any(isinstance(fd.spec.get("fiscal"), dict) for target, fk in idx.joins(entity).items()
                 if fk == column for fd in idx.fields(target) if fd.kind == "dimension")
    if default in FISCAL_PRESETS and not fiscal:
        report(path, label, f"`default: {default}` needs a fiscal calendar: column `{column}` must join an entity "
                            f"whose date dimension has `fiscal:`")
    elif default not in DATE_PRESETS + FISCAL_PRESETS \
            and not re.match(r"^\d{4}-\d{2}-\d{2}(\.\.\d{4}-\d{2}-\d{2})?$", default):
        presets = DATE_PRESETS + (FISCAL_PRESETS if fiscal else ())
        report(path, label, f"`default` for a date column must be a date (2026-01-31), a range "
                            f"(2026-01-01..2026-03-31) or one of {', '.join(presets)}; got `{default}`"
               + did_you_mean(default, presets))


def _check_seeds(art: _Artifact, models: dict[str, ModelIndex], root: Path):
    conns = art.data.get("connections") if isinstance(art.data.get("connections"), dict) else {}
    writable = {m.entities[e].get("table") for m in models.values() for e in m.entities if m.is_writable(e)}
    for cname, c in conns.items():
        if not isinstance(c, dict) or not isinstance(c.get("seeds"), dict):
            continue
        db = (c.get("duckdb") or {}).get("database") if isinstance(c.get("duckdb"), dict) else None
        for table, rel in c["seeds"].items():
            path = ("connections", cname, "seeds", table)
            if table not in writable:
                art.report(path, f"connection `{cname}`", f"`seeds`: `{table}` is not the table of a writable "
                                                          f"entity" + did_you_mean(table, writable))
            elif isinstance(rel, str) and not (root / rel).is_file():
                art.report(path, f"connection `{cname}`", f"seed for `{table}` points to `{rel}`, which does not exist")
            if not db:
                art.report(path, f"connection `{cname}`", "`seeds` need `duckdb: { database: <file> }`: writable "
                                                          "tables live in a database file")


# =========================================================================== dashboards

_FIELD_SPEC = {
    # prop: (accepted kinds, list allowed)
    "kpi": {"measure": (("measure",), False), "target": (("measure",), False),
            "sparkline": (("dimension",), False)},
    "chart": {"x": (("measure", "dimension", "hierarchy"), False),
              "y": (("measure", "dimension", "hierarchy"), True),
              "series": (("dimension",), False), "detail": (("dimension",), False)},
    "text": {"rows": (("dimension",), True)},  # record text (ADR-012): dimensions only, first row
    "table": {"rows": (("dimension", "hierarchy"), True), "columns": (("dimension",), True),
              "values": (("measure",), True)},
}
_WIDGETS = ("kpi", "chart", "table", "text", "button", "form")
_CONTAINERS = ("row", "grid", "tabs", "tab", "card", "section")


class _DashboardCheck:
    def __init__(self, art: _Artifact, models: dict[str, ModelIndex], dashboards: dict[str, Any],
                 broken: set[str] = frozenset(), forms: dict[str, _Artifact] | None = None):
        self.art, self.d, self.report = art, art.data, art.report
        self.dashboards, self.broken, self.forms = dashboards, broken, forms or {}
        model = self.d.get("model")
        self.idx = models.get(model) if isinstance(model, str) else None
        if isinstance(model, str) and self.idx is None:
            self.report(("model",), None, f"model `{model}` not found in this project"
                        + did_you_mean(model, models))
        self.ids: dict[str, tuple[str, int | None]] = {}
        self.filters: dict[str, dict] = {}       # id -> spec
        self.filter_refs: dict[str, Ref] = {}    # id -> resolved dimension
        self.page_ids = [p["page"] for p in self._list("pages") if isinstance(p.get("page"), str)]

    def _list(self, key: str) -> list[dict]:
        v = self.d.get(key)
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    def run(self):
        for i, f in enumerate(self.d.get("filters") or []):
            if isinstance(f, dict) and isinstance(f.get("filter"), str):
                self._filter(f, ("filters", i))
        for i, p in enumerate(self.d.get("pages") or []):
            if isinstance(p, dict) and isinstance(p.get("page"), str):
                label = f"page `{p['page']}`"
                self._register(p["page"], label, ("pages", i))
                self._exprs(p, ("pages", i), label, {"visible"}, names=("filters", "user"))
                self._nodes(p.get("items"), ("pages", i, "items"), p["page"])

    def _register(self, id_: str, label: str, path: tuple):
        if id_ in self.ids:
            other, line = self.ids[id_]
            self.report(path, label, f"id `{id_}` is already used by {other} on line {line}; "
                                     f"ids must be unique within a dashboard")
        else:
            self.ids[id_] = (label, self.art.doc.line_of(path))

    def _resolve(self, text, want, path, label) -> Ref | None:
        if self.idx is None or not isinstance(text, str):
            return None
        try:
            return self.idx.resolve(text, want)
        except RefError as e:
            self.report(path, label, e.message)
            return None

    # ------------------------------------------------------------------ filters

    def _filter(self, f: dict, path: tuple):
        fid = f["filter"]
        label = f"filter `{fid}`"
        self._register(fid, label, path)
        self.filters[fid] = f
        ref = self._resolve(f.get("dimension"), ("dimension",), path + ("dimension",), label)
        if ref:
            self.filter_refs[fid] = ref
            default = f.get("default")
            fiscal = isinstance(ref.field.spec.get("fiscal"), dict)
            presets = DATE_PRESETS + (FISCAL_PRESETS if fiscal else ())
            if ref.field.dtype == "date" and isinstance(default, str) and default in FISCAL_PRESETS \
                    and not fiscal:
                self.report(path + ("default",), label,
                            f"`default: {default}` needs a fiscal calendar on dimension `{ref.field.name}`; "
                            f"add `fiscal: {{ year: <column>, period: <column> }}` to it in the model")
            elif ref.field.dtype == "date" and isinstance(default, str) \
                    and default not in presets and not re.match(r"^\d{4}-\d{2}-\d{2}$", default):
                self.report(path + ("default",), label,
                            f"`default` for a date filter must be a date (2026-01-31) or one of "
                            f"{', '.join(presets)}; got `{default}`" + did_you_mean(default, presets))
        for j, pid in enumerate(f.get("pages") or []):
            if isinstance(pid, str) and pid not in self.page_ids:
                self.report(path + ("pages", j), label,
                            f"`pages`: no page `{pid}` in this dashboard" + did_you_mean(pid, self.page_ids))

    # ------------------------------------------------------------------ layout

    def _nodes(self, items, path: tuple, page: str):
        if not isinstance(items, list):
            return
        for i, node in enumerate(items):
            if not isinstance(node, dict):
                continue
            kind = next((k for k in node if k in _WIDGETS + _CONTAINERS), None)
            if kind is None or not isinstance(node[kind], str):
                continue
            npath = path + (i,)
            label = f"{'widget' if kind in _WIDGETS else 'tab' if kind == 'tab' else 'container'} `{node[kind]}`"
            self._register(node[kind], label, npath)
            if kind in _CONTAINERS:
                self._exprs(node, npath, label, {"title", "visible"}, names=("filters", "user"))
                self._nodes(node.get("items"), npath + ("items",), page)
            else:
                self._widget(kind, node, npath, label, page)

    def _date_filters(self) -> set[str]:
        return {fid for fid, r in self.filter_refs.items() if r.field.dtype in ("date", "timestamp")}

    def _form_widget(self, w: dict, path: tuple, label: str):
        fid = w["form"]
        form = self.forms.get(fid)
        if form is None:
            self.report(path, label, f"no form `{fid}` in this project (forms/<id>.form.yaml)"
                        + did_you_mean(fid, self.forms))
            return
        self._exprs(w, path, label, {"title", "visible"}, names=("filters", "user"))
        # Defaults that read `${filters.*}` read this dashboard's filters.
        values = [(f.get("field"), f.get("default")) for f in form.data.get("fields") or [] if isinstance(f, dict)]
        values += [(f.get("filter"), f.get("default")) for f in form.data.get("filters") or [] if isinstance(f, dict)]
        for name, default in values:
            for msg in check_expression(default, names=("filters", "user"), filter_ids=list(self.filters),
                                        date_filters=self._date_filters()):
                self.report(path, label, f"form `{fid}`, `{name}` default: {msg}")
        nav = (form.data.get("on_submit") or {}).get("navigate") if isinstance(form.data.get("on_submit"), dict) else None
        if isinstance(nav, str) and "/" not in nav and nav not in self.page_ids:
            self.report(path, label, f"form `{fid}`: `on_submit` navigates to page `{nav}`, which is not in this "
                                     f"dashboard" + did_you_mean(nav, self.page_ids))

    def _record_text_reach(self, w, path, label, page, refs):
        """Record text (dimensions only): every filter that applies must reach the first row's
        entity, as for measures (ADR-005)."""
        if self.idx is None or not refs:
            return
        base = refs[0][1].field.entity
        reach = set(self.idx.reachable(base))
        ignore = w.get("ignore_filters") if isinstance(w.get("ignore_filters"), list) else []
        for p, r in refs[1:]:
            if r.field.entity not in reach:
                self.report(path + ("rows",), label, f"`{r.text}` cannot be reached from `{refs[0][1].text}`'s "
                                                     f"entity `{base}`; record text reads one entity and its joins")
        bad = [fid for fid, fref in self.filter_refs.items()
               if fid not in ignore and (not self.filters[fid].get("pages") or page in self.filters[fid]["pages"])
               and fref.field.entity not in reach]
        if bad:
            fix = [f for f in self.filters if f in bad or f in ignore]
            self.report(path, label, f"{'filters' if len(bad) > 1 else 'filter'} {', '.join(f'`{b}`' for b in bad)} "
                                     f"cannot reach entity `{base}`; add `ignore_filters: [{', '.join(fix)}]` to the widget")

    def _widget(self, kind: str, w: dict, path: tuple, label: str, page: str):
        if kind == "form":
            self._form_widget(w, path, label)
            return
        refs: list[tuple[str, Ref]] = []     # (prop, ref)
        shown: list[str] = []                # field refs as written, for sort / cells
        for prop, (want, many) in _FIELD_SPEC.get(kind, {}).items():
            v = w.get(prop)
            values = v if isinstance(v, list) else [v] if v is not None else []
            for j, text in enumerate(values):
                p = path + ((prop, j) if isinstance(v, list) else (prop,))
                if isinstance(text, str):
                    shown.append(text)
                ref = self._resolve(text, want, p, label)
                if ref:
                    refs.append((prop, ref))
        if kind == "kpi":
            for prop, r in refs:
                if prop == "sparkline" and r.field.dtype != "date":
                    self.report(path + ("sparkline",), label,
                                f"`sparkline` needs a date dimension; `{r.text}` is a {r.field.dtype} dimension")
        xy_written = [s for p in ("x", "y") for s in (w.get(p) if isinstance(w.get(p), list) else [w.get(p)])
                      if isinstance(s, str)]
        xy_resolved = [r for p, r in refs if p in ("x", "y")]
        # Only when every x/y name resolved; otherwise the unknown name is the real problem.
        if kind == "chart" and self.idx and xy_written and len(xy_resolved) == len(xy_written) \
                and not any(r.kind == "measure" for r in xy_resolved):
            self.report(path, label, "a chart needs a measure on `x` or `y`")

        where_refs: list[tuple[str, Ref]] = []
        if isinstance(w.get("where"), dict):
            for k in w["where"]:
                r = self._resolve(k, ("dimension",), path + ("where", k), label)
                if r:
                    where_refs.append((k, r))

        self._check_sort_and_cells(w, path, label, shown, refs)
        self._check_grain(w, path, label, page, refs, where_refs)
        if kind == "text" and refs:
            self._record_text_reach(w, path, label, page, refs)

        # `row` holds the fields the widget queries; a KPI's sparkline is a separate series.
        row_fields = sorted({n for p, r in refs if p != "sparkline" for n in self._names(r)})
        data_names = ("value", "row", "filters", "user")
        if kind == "text" and w.get("rows"):  # record text: `${row.*}` is the first row
            self._exprs(w, path, label, {"content"}, names=("row", "filters", "user"), row_fields=row_fields)
            self._exprs(w, path, label, {"title", "visible", "empty"}, names=("filters", "user"))
        else:
            self._exprs(w, path, label, {"title", "visible", "content"}, names=("filters", "user"))
        if kind in ("kpi", "chart"):
            self._exprs(w, path, label, {"color"}, names=data_names, row_fields=row_fields, color=True)
        if isinstance(w.get("cells"), dict):
            for field, cell in w["cells"].items():
                if isinstance(cell, dict):
                    self._exprs(cell, path + ("cells", field), label, {"color"}, names=data_names,
                                row_fields=row_fields, color=True, prefix=f"cells.{field}.")
        if isinstance(w.get("on_click"), dict):
            names = ("row", "filters", "user") if kind != "button" else ("filters", "user")
            self._on_click(w["on_click"], path + ("on_click",), label, names, row_fields)

    def _currency_code(self, f) -> Ref | None:
        """The currency-code dimension of a measure in document currency (calc: of its parts)."""
        stack = [f]
        while stack:
            g = stack.pop()
            text = self.idx.currency_code_dimension(g)
            if text:
                try:
                    return self.idx.resolve(text, ("dimension",))
                except RefError:
                    return None
            for r in self.idx.calc_refs(g):
                try:
                    stack.append(self.idx.resolve(r, ("measure",)).field)
                except RefError:
                    pass
        return None

    def _names(self, r: Ref) -> list[str]:
        if r.field:
            return [r.field.name]
        return [r.hierarchy] + [lv.field.name for lv in self.idx.levels(r)]

    def _check_sort_and_cells(self, w, path, label, shown, refs):
        names = set(shown) | {n for _, r in refs for n in self._names(r)}
        sort = w.get("sort")
        if isinstance(sort, str) and self.idx and sort.lstrip("-") not in names:
            self.report(path + ("sort",), label,
                        f"sort field `{sort.lstrip('-')}` is not shown by this widget; use one of "
                        f"{', '.join(shown)}" + did_you_mean(sort.lstrip('-'), shown))
        if isinstance(w.get("cells"), dict) and self.idx:
            for k in w["cells"]:
                if k not in names:
                    self.report(path + ("cells", k), label,
                                f"`cells`: `{k}` is not a column of this table; columns: {', '.join(shown)}"
                                + did_you_mean(k, shown))

    # ------------------------------------------------------------------ grain (ADR-005)

    def _check_grain(self, w, path, label, page, refs, where_refs):
        if self.idx is None:
            return
        measures = [r for _, r in refs if r.kind == "measure"]
        if not measures:
            return
        dims = [(p, lv) for p, r in refs if r.kind != "measure" for lv in self.idx.levels(r)]
        grains = {m.field: self.idx.measure_grain(m.field) for m in measures}

        # ADR-009: an amount in document currency cannot be added up across currencies.
        shown = {(lv.field.entity, lv.field.name) for _, lv in dims}
        for m in measures:
            if not self.idx.per_currency(m.field):
                continue
            code = self._currency_code(m.field)
            if code and (code.field.entity, code.field.name) not in shown:
                twin = self.idx.converted_counterpart(m.field)
                use = f"`{twin.name}`" if twin else "a measure with `convert:`"
                if w.get("kpi"):
                    fix = f"a KPI shows one number, so use {use}"
                else:
                    fix = f"show `{code.field.name}` in this widget (rows, columns or series), or use {use}"
                self.report(path, label, f"measure `{m.name}` is in document currency and cannot be added up "
                                         f"across currencies; {fix}")

        for m in measures:
            g = grains[m.field]
            gt = "grain: " + self.idx.grain_text(g)
            for prop, dref in dims:
                problem = self.idx.grain_problem(g, dref)
                if problem == "outside":
                    self.report(path + (prop,), label,
                                f"dimension `{dref.text}` is outside the grain of measure `{m.name}` "
                                f"({gt}); remove one of them")
                elif problem == "finer":
                    tg = g[(dref.field.entity, dref.field.name)]
                    self.report(path + (prop,), label,
                                f"`{dref.text}` is finer than the grain of measure `{m.name}` ({gt}); "
                                f"use `{dref.field.name}.{tg}` or coarser")
            for key, dref in where_refs:
                if self.idx.grain_problem(g, dref) == "outside":
                    self.report(path + ("where", key), label,
                                f"`where` on `{key}` cannot apply to measure `{m.name}` ({gt}); "
                                f"remove the condition or the measure")

        ignore = w.get("ignore_filters") if isinstance(w.get("ignore_filters"), list) else []
        for j, fid in enumerate(ignore):
            if isinstance(fid, str) and fid not in self.filters:
                self.report(path + ("ignore_filters", j), label,
                            f"`ignore_filters`: no filter `{fid}` in this dashboard"
                            + did_you_mean(fid, self.filters))
        offending: dict[str, list] = {}
        for fid, fref in self.filter_refs.items():
            pages = self.filters[fid].get("pages")
            if fid in ignore or (isinstance(pages, list) and page not in pages):
                continue
            for m in measures:
                if self.idx.grain_problem(grains[m.field], fref) == "outside":
                    offending.setdefault(fid, []).append(m)
        if offending:
            bad = list(offending)
            affected = list(dict.fromkeys(m.field for ms in offending.values() for m in ms))
            mtext = _and([f"`{f.name}` (grain: {self.idx.grain_text(grains[f])})" for f in affected])
            fix = [f for f in self.filters if f in bad or f in ignore]
            noun = "filters" if len(bad) > 1 else "filter"
            verb = "do" if len(bad) > 1 else "does"
            self.report(path, label,
                        f"{noun} {', '.join(f'`{b}`' for b in bad)} {verb} not apply to "
                        f"{'measures' if len(affected) > 1 else 'measure'} {mtext}; add "
                        f"`ignore_filters: [{', '.join(fix)}]` to the widget, or remove the measure")

    # ------------------------------------------------------------------ navigation

    def _on_click(self, oc: dict, path: tuple, label: str, names, row_fields):
        nav = oc.get("navigate")
        target_pages, target_filters, target_name = self.page_ids, list(self.filters), "this dashboard"
        if isinstance(nav, str) and "/" in nav:
            did, pid = nav.split("/", 1)
            other = self.dashboards.get(did)
            if other is None and did in self.broken:
                return
            if other is None:
                self.report(path + ("navigate",), label, f"`on_click`: no dashboard `{did}` in this project"
                            + did_you_mean(did, self.dashboards))
                return
            target_name = f"dashboard `{did}`"
            target_pages = [p.get("page") for p in other.get("pages") or [] if isinstance(p, dict)]
            target_filters = [f.get("filter") for f in other.get("filters") or [] if isinstance(f, dict)]
        else:
            pid = nav
        if isinstance(pid, str) and pid not in target_pages:
            self.report(path + ("navigate",), label, f"`on_click`: no page `{pid}` in {target_name}"
                        + did_you_mean(pid, [p for p in target_pages if p]))
        sets = oc.get("set")
        if isinstance(sets, dict):
            for k, v in sets.items():
                if k not in target_filters:
                    self.report(path + ("set", k), label,
                                f"`on_click.set`: `{k}` is not a filter of {target_name}"
                                + did_you_mean(k, [f for f in target_filters if f]))
                for msg in check_expression(v, names=names, row_fields=row_fields,
                                            filter_ids=list(self.filters)):
                    self.report(path + ("set", k), label, f"`on_click.set.{k}`: {msg}")

    # ------------------------------------------------------------------ expressions

    def _exprs(self, obj: dict, path: tuple, label: str, props: set[str], names, row_fields=(),
               color=False, prefix=""):
        for prop in props:
            v = obj.get(prop)
            if not isinstance(v, str):
                continue
            if prop == "visible" and not is_expression(v):
                self.report(path + (prop,), label,
                            f"`{prefix}visible` must be an expression such as `${{filters.region.count > 0}}`")
                continue
            for msg in check_expression(v, names=names, row_fields=row_fields,
                                        filter_ids=list(self.filters), color=color,
                                        date_filters=self._date_filters()):
                self.report(path + (prop,), label, f"`{prefix}{prop}`: {msg}")


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# =========================================================================== expressions


def check_expression(value: Any, *, names, row_fields=(), filter_ids=(), color=False,
                     date_filters=frozenset()) -> list[str]:
    """Static checks of a property value that may contain `${...}` (ADR-005 §4)."""
    if not isinstance(value, str):
        return []
    if color and not is_expression(value):
        if value in THEME_COLORS:
            return []
        return [f"`{value}` is not a theme color; use one of {', '.join(THEME_COLORS)}"
                + did_you_mean(value, THEME_COLORS)]
    try:
        parts = parse_template(value)
    except ExprError as e:
        return [f"invalid expression: {e}"]
    c = _ExprCheck(names, row_fields, filter_ids, date_filters)
    for part in parts:
        if isinstance(part, tuple):
            c.walk(part)
            if color:
                for lit in _results(part):
                    if lit not in THEME_COLORS:
                        c.msgs.append(f"`{lit}` is not a theme color; use one of {', '.join(THEME_COLORS)}"
                                      + did_you_mean(str(lit), THEME_COLORS))
    return list(dict.fromkeys(c.msgs))


def _results(node) -> list:
    if node[0] == "cond":
        return _results(node[2]) + _results(node[3])
    if node[0] == "lit" and node[1] is not None:
        return [node[1]]
    return []


def _is_filter_list(node) -> bool:
    return node[0] == "member" and node[1] == ("name", "filters")


class _ExprCheck:
    def __init__(self, names, row_fields, filter_ids, date_filters=frozenset()):
        self.names, self.row_fields = tuple(names), list(row_fields)
        self.filter_ids = filter_ids if isinstance(filter_ids, _AnyFilter) else list(filter_ids)
        self.date_filters = set(date_filters)
        self.msgs: list[str] = []

    def walk(self, node, consumed: bool = False):
        t = node[0]
        if t == "lit":
            return
        if t == "name":
            n = node[1]
            if n not in self.names:
                self.msgs.append(f"`{n}` is not available here; use {', '.join(self.names)}")
            elif not consumed and n in ("row", "filters", "user"):
                self.msgs.append(f"`{n}` needs a name after it, such as `{n}.{self._example(n)}`")
            return
        if t == "member":
            obj, attr = node[1], node[2]
            if obj == ("name", "row"):
                self.walk(obj, True)
                if "row" in self.names and attr not in self.row_fields:
                    self.msgs.append(f"`row.{attr}` is not a field of this widget; available: "
                                     f"{', '.join(self.row_fields) or 'none'}" + did_you_mean(attr, self.row_fields))
            elif obj == ("name", "filters"):
                self.walk(obj, True)
                if "filters" in self.names and attr not in self.filter_ids:
                    self.msgs.append(f"`filters.{attr}`: no filter `{attr}` in this dashboard"
                                     + did_you_mean(attr, self.filter_ids))
                elif not consumed:
                    self.msgs.append(f"`filters.{attr}` is a list; use `filters.{attr}.label`, `.count`, "
                                     f"`.first` or `.includes(x)`")
            elif obj == ("name", "user"):
                self.walk(obj, True)
            elif _is_filter_list(obj):
                self.walk(obj, True)
                fid = obj[2]
                if fid in self.date_filters and attr in ("first", "count"):
                    self.msgs.append(f"`filters.{fid}` is a date range; use `filters.{fid}.start` or "
                                     f"`filters.{fid}.end` instead of `.{attr}`")
                elif fid not in self.date_filters and attr in ("start", "end") \
                        and not isinstance(self.filter_ids, _AnyFilter):
                    self.msgs.append(f"`.{attr}` is only for date filters; `filters.{fid}` is a list of values, "
                                     f"use `.first`, `.label`, `.count` or `.includes(x)`")
                elif attr not in FILTER_MEMBERS:
                    self.msgs.append(f"`filters.{fid}.{attr}` is not available; use `.label`, `.count`, "
                                     f"`.first`, `.includes(x)`, or `.start`/`.end` for date filters")
            else:
                self.walk(obj, True)
                self.msgs.append(f"`.{attr}` is not available here")
            return
        if t == "call":
            obj, meth, args = node[1], node[2], node[3]
            if _is_filter_list(obj):
                self.walk(obj, True)
                if meth != "includes" or len(args) != 1:
                    self.msgs.append(f"use `filters.{obj[2]}.includes(x)` with one argument")
            else:
                self.msgs.append("only `filters.<id>.includes(x)` can be called")
            for a in args:
                self.walk(a)
            return
        if t == "bin" and node[1] in ("==", "!="):
            a, b = node[2], node[3]
            for side, other in ((a, b), (b, a)):
                if _is_filter_list(side) and other == ("lit", None):
                    fid = side[2]
                    cmp = "==" if node[1] == "==" else ">"
                    self.walk(side, True)
                    self.msgs.append(f"`filters.{fid}` is a list and is never null; "
                                     f"use `filters.{fid}.count {cmp} 0`")
                    return
        for child in node[2:] if t in ("bin", "unary") else node[1:]:
            if isinstance(child, tuple):
                self.walk(child)

    def _example(self, n: str) -> str:
        if n == "row":
            return self.row_fields[0] if self.row_fields else "net_revenue"
        if n == "filters":
            return f"{self.filter_ids[0]}.label" if self.filter_ids else "region.label"
        return "region"
