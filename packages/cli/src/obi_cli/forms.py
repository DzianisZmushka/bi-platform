"""Forms and sheets in the Phase 1 renderer (ADR-012).

Rendering is server-side HTML like dashboards (ADR-007). Every write goes through
`obi_engine.store.Writer`, so forms, sheets, paste and CSV import share one set of rules.
"""

from __future__ import annotations

import csv
import html
import io
import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from obi_engine import Project, dates
from obi_engine.store import NOT_FOUND, Writer, WriteError, find
from obi_formats.artifacts import parse
from obi_formats.expr import ExprError, render as render_expr

esc = html.escape

# Every property of the form schema, per definition, that this module acts on (CLAUDE.md;
# packages/cli/tests/test_coverage.py checks it against form.schema.json).
FORMS_RENDERED: dict[str, set[str]] = {
    "form": {"form", "title", "description", "entity", "fields", "on_submit"},
    "field": {"field", "label", "help", "widget", "default", "required", "readonly", "visible"},
    "on_submit": {"navigate", "set"},
    "sheet": {"sheet", "title", "description", "entity", "columns", "readonly", "filters", "sort", "allow"},
    "sheet_filter": {"filter", "title", "column", "default"},
}
WIDGET_FOR = {"text": "textarea", "number": "number", "integer": "number", "id": "number", "date": "date",
              "timestamp": "text", "boolean": "checkbox"}
ALL_ACTIONS = ("edit", "add", "delete", "paste", "import")
SHEET_ACTIONS = {"sheet_save": "edit", "sheet_delete": "delete", "sheet_add": "add", "sheet_paste": "paste",
                 "sheet_import": "import"}  # enforced on write too, not only by hiding buttons


def load_forms(root: str | Path) -> dict[str, dict]:
    out = {}
    for p in sorted(Path(root).glob("forms/*.form.yaml")):
        d = parse(p.read_text(encoding="utf-8")).data
        fid = d.get("form") or d.get("sheet")
        if isinstance(fid, str):
            out[fid] = d
    return out


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class FormContext:
    """What a form needs from its host: filters for `${filters.*}`, the user, the page URL."""

    def __init__(self, project: Project, user: dict, selections: dict, mode: str, here: str,
                 state: dict | None = None, query: dict | None = None, as_of: date | None = None):
        self.project, self.user, self.selections, self.mode = project, user, selections, mode
        self.here, self.state, self.query = here or "/", state or {}, query or {}
        self.as_of = as_of or date.today()
        self.user = user or {}
        self.writer = Writer(project, project.user)  # the project's user; none: the system

    def editable(self, fid: str) -> bool:
        """Whether this form saves here: in `obi serve`, for a user whose roles may edit it."""
        return self.mode == "serve" and self.writer.grants.can_edit_form(fid)

    def expr(self, text):
        if not isinstance(text, str) or "${" not in text:
            return text
        try:
            return render_expr(text, {"user": self.user, "filters": self.selections, "row": {}, "value": None})
        except ExprError:
            return None


# =========================================================================== single-record form


def render_form(ctx: FormContext, fid: str, form: dict, navigate_href=None) -> str:
    w = find(ctx.project, form["entity"])
    state = ctx.state.get(fid) or {}
    defaults = {}
    for f in form["fields"]:
        default = f.get("default", w.columns.get(f["field"], {}).get("default"))
        defaults[f["field"]] = ctx.expr(default)
    # Edit mode: every key column has a value and the row exists (ADR-012).
    key = {k: defaults.get(k) for k in w.key}
    row = None
    if all(v not in (None, "") for v in key.values()):
        try:
            row = ctx.writer.get(form["entity"], {k: ctx.writer._coerce(v, w.types[k]) for k, v in key.items()})
        except (ValueError, WriteError):
            row = None
    values = dict(defaults)
    if row:
        values.update({c: row.get(c) for c in w.types})
    values.update(state.get("values") or {})
    errors = state.get("errors") or {}
    live = ctx.editable(fid)
    title = form.get("title") or fid
    parts = [f'<form class="obi-form" method="post" action="/_write">'
             f'<input type="hidden" name="_form" value="{esc(fid)}">'
             f'<input type="hidden" name="_return" value="{esc(ctx.here)}">']
    if navigate_href:
        parts.append(f'<input type="hidden" name="_next" value="{esc(navigate_href)}">')
    head = f'<div class="widget-title">{esc(title)}</div>'
    if form.get("description"):
        head += f'<p class="desc">{esc(form["description"])}</p>'
    status_col = (w.workflow or {}).get("column")
    if row is not None:
        badge = f' · status <b>{esc(_text(row.get(status_col)))}</b>' if status_col else ""
        head += f'<p class="form-mode">Editing the existing record{badge}, last changed by ' \
                f'{esc(_text(row.get("updated_by")))} at {esc(_text(row.get("updated_at")))}.</p>'
    if state.get("saved"):
        head += '<p class="form-ok">Saved.</p>'
    if errors.get(""):
        head += "".join(f'<p class="form-error">{esc(m)}</p>' for m in errors[""])
    for f in form["fields"]:
        name = f["field"]
        if f.get("visible") is not None and not ctx.expr(f["visible"]):
            continue
        spec = w.columns.get(name, {})
        ctype = w.types.get(name, "string")
        label = f.get("label") or spec.get("label") or name.replace("_", " ").capitalize()
        widget = f.get("widget") or ("select" if spec.get("choices") or spec.get("references")
                                     else WIDGET_FOR.get(ctype, "text"))
        required = bool(f.get("required", spec.get("required"))) and ctype != "id"
        readonly = bool(f.get("readonly")) or not live
        control = _control(ctx, form["entity"], name, widget, spec, values.get(name), required, readonly)
        err = "".join(f'<span class="form-error">{esc(m)}</span>' for m in errors.get(name, []))
        help_ = f'<span class="help">{esc(f["help"])}</span>' if f.get("help") else ""
        parts.append(f'<label class="field"><span>{esc(label)}{" *" if required else ""}</span>{control}{help_}{err}</label>')
    if live:
        buttons = ['<button type="submit" name="_action" value="save">Save</button>']
        if row is not None:
            for tname in ctx.writer.transitions(form["entity"], row):
                buttons.append(f'<button type="submit" name="_action" value="transition:{esc(tname)}" '
                               f'class="secondary">{esc(tname.capitalize())}</button>')
        parts.append(f'<div class="form-buttons">{"".join(buttons)}</div>')
    else:
        parts.append('<p class="desc">' + ("You may view this form, but none of your roles may edit it."
                                            if ctx.mode == "serve" else "Forms save in <code>obi serve</code>.")
                     + "</p>")
    parts.append("</form>")
    return head + "".join(parts)


def _control(ctx, entity, name, widget, spec, value, required, readonly, form_id=None) -> str:
    attrs = (' required' if required else '') + (' disabled' if readonly else '') + \
        (f' form="{esc(form_id)}"' if form_id else '')
    hidden = f'<input type="hidden" name="{esc(name)}" value="{esc(_text(value))}"' \
             f'{f" form={chr(34)}{esc(form_id)}{chr(34)}" if form_id else ""}>' if readonly else ""
    if widget == "select":
        opts = [(c, c) for c in spec.get("choices") or []] or ctx.writer.options(entity, name)
        sel = _text(value)
        options = ['<option value=""></option>'] + [
            f'<option value="{esc(_text(k))}"{" selected" if sel in (_text(k), _text(d)) else ""}>{esc(_text(d))}</option>'
            for k, d in opts]
        return f'<select name="{esc(name)}"{attrs}>{"".join(options)}</select>{hidden}'
    if widget == "textarea":
        return f'<textarea name="{esc(name)}" rows="3"{attrs}>{esc(_text(value))}</textarea>{hidden}'
    if widget == "checkbox":
        checked = " checked" if value in (True, "true", "on", "1") else ""
        return f'<input type="checkbox" name="{esc(name)}" value="true"{checked}{attrs}>{hidden}'
    kind = {"number": "number", "date": "date"}.get(widget, "text")
    step = ' step="any"' if kind == "number" else ""
    return f'<input type="{kind}" name="{esc(name)}" value="{esc(_text(value))}"{step}{attrs}>{hidden}'


# =========================================================================== sheet


def render_sheet(ctx: FormContext, sid: str, sheet: dict) -> str:
    w = find(ctx.project, sheet["entity"])
    allow = set(sheet.get("allow") or ALL_ACTIONS)
    live = ctx.editable(sid)
    cols = list(sheet["columns"])
    readonly = set(sheet.get("readonly") or [])
    if w.workflow:
        readonly.add(w.workflow["column"])  # the status changes only through transitions
    rows = ctx.writer.rows(sheet["entity"], sort=sheet.get("sort"))
    # Sheet filters: from the URL (`sf.<sheet>.<filter>`), else their defaults.
    fparts, active, filter_errors = [], [], []
    for f in sheet.get("filters") or []:
        param = f"sf.{sid}.{f['filter']}"
        given = ctx.query.get(param)
        value = given[0] if given else _text(ctx.expr(f.get("default")))
        ctype = w.types.get(f["column"], "string")
        if value and ctype in ("date", "timestamp"):
            try:
                value_range = _date_range(ctx, w, f["column"], value)
            except ValueError as e:
                filter_errors.append(f"filter `{f['filter']}`: {e}")
            else:
                active.append((f["column"], ctype, value_range))
        elif value:
            active.append((f["column"], ctype, value))
        fparts.append(f'<label class="filter"><b>{esc(f.get("title") or f["filter"].capitalize())}</b>'
                      f'<input name="{esc(param)}" value="{esc(value)}" size="12"></label>')
    rows = [r for r in rows if all(_match(r.get(c), t, v) for c, t, v in active)]
    if filter_errors:  # a filter that cannot be evaluated shows nothing, never everything
        rows = []
    state = ctx.state.get(sid) or {}
    out = [f'<div class="widget-title">{esc(sheet.get("title") or sid)}</div>']
    if sheet.get("description"):
        out.append(f'<p class="desc">{esc(sheet["description"])}</p>')
    if fparts and ctx.mode == "serve":  # filtering is reading, not editing
        keep = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, vs in ctx.query.items()
                       for v in vs if not k.startswith(f"sf.{sid}."))
        out.append(f'<form class="filters" method="get">{keep}{"".join(fparts)}<button>Filter</button></form>')
    if state.get("saved"):
        out.append(f'<p class="form-ok">{esc(state["saved"])}</p>')
    for m in filter_errors + list(state.get("errors") or []):
        out.append(f'<p class="form-error">{esc(m)}</p>')
    head = "".join(f"<th>{esc(w.columns.get(c, {}).get('label') or c)}</th>" for c in cols) + "<th></th>"
    body = []
    for i, r in enumerate(rows):
        fid = f"row-{sid}-{i}"
        keyjson = json.dumps({k: _text(r.get(k)) for k in w.key})
        cells = "".join(
            f"<td>{_control(ctx, sheet['entity'], c, _sheet_widget(w, c), w.columns.get(c, {}), r.get(c), False, not live or 'edit' not in allow or c in readonly or c in w.key, fid)}</td>"
            for c in cols)
        hist = urlencode({**{k: v[0] for k, v in ctx.query.items()}, "history": f"{sid}:{keyjson}"})
        actions = ""
        if live:
            actions = (f'<form id="{esc(fid)}" method="post" action="/_write" class="row-form">'
                       f'<input type="hidden" name="_form" value="{esc(sid)}">'
                       f'<input type="hidden" name="_return" value="{esc(ctx.here)}">'
                       f'<input type="hidden" name="_key" value="{esc(keyjson)}">'
                       + ('<button name="_action" value="sheet_save">Save</button>' if "edit" in allow else "")
                       + ('<button name="_action" value="sheet_delete" class="secondary" '
                          'onclick="return confirm(\'Delete this row?\')">Delete</button>' if "delete" in allow else "")
                       + f'</form><a class="history-link" href="?{esc(hist)}">History</a>')
        body.append(f"<tr>{cells}<td class='row-actions'>{actions}</td></tr>")
    if live and "add" in allow:
        fid = f"row-{sid}-new"
        cells = "".join(f"<td>{_control(ctx, sheet['entity'], c, _sheet_widget(w, c), w.columns.get(c, {}), (state.get('values') or {}).get(c), False, c in readonly, fid)}</td>"
                        for c in cols)
        body.append(f'<tr class="new-row">{cells}<td class="row-actions"><form id="{esc(fid)}" method="post" '
                    f'action="/_write"><input type="hidden" name="_form" value="{esc(sid)}">'
                    f'<input type="hidden" name="_return" value="{esc(ctx.here)}">'
                    f'<button name="_action" value="sheet_add">Add</button></form></td></tr>')
    out.append(f'<div class="table-scroll"><table class="sheet"><thead><tr>{head}</tr></thead>'
               f'<tbody>{"".join(body)}</tbody></table></div>')
    if live and ({"paste", "import"} & allow):
        modes = [m for m in ("paste", "import") if m in allow]
        paste_cols = _sheet_new_columns(sheet, w)  # pasted rows fill these, in this order
        out.append(
            f'<details class="sheet-bulk"><summary>Paste or import rows</summary>'
            f'<form method="post" action="/_write"><input type="hidden" name="_form" value="{esc(sid)}">'
            f'<input type="hidden" name="_return" value="{esc(ctx.here)}">'
            f'<p class="desc">Paste rows copied from a spreadsheet (tab-separated, columns: {esc(", ".join(paste_cols))}), '
            f'or choose a CSV file whose header names the columns. Every row is checked like a form; '
            f'nothing is saved if one row fails.</p>'
            f'<textarea name="_rows" rows="5" cols="80"></textarea>'
            + ('<input type="file" accept=".csv,text/csv" onchange="obiReadFile(this)">' if "import" in allow else "")
            + "".join(f'<button name="_action" value="sheet_{m}">{"Paste rows" if m == "paste" else "Import CSV"}</button>'
                      for m in modes)
            + "</form></details>")
    hist = ctx.query.get("history")
    if hist and hist[0].startswith(f"{sid}:"):
        key = json.loads(hist[0][len(sid) + 1:])
        typed = {k: ctx.writer._coerce(v, w.types[k]) for k, v in key.items()}
        hrows = ctx.writer.history(sheet["entity"], typed)
        hcols = cols + ["history_op", "history_by", "history_at"]
        trs = "".join("<tr>" + "".join(f"<td>{esc(_text(h.get(c)))}</td>" for c in hcols) + "</tr>" for h in hrows)
        out.append(f'<div class="sheet-history"><b>History of {esc(", ".join(f"{k} {v}" for k, v in key.items()))}</b>'
                   f'<table><thead><tr>{"".join(f"<th>{esc(c)}</th>" for c in hcols)}</tr></thead>'
                   f'<tbody>{trs or "<tr><td>No earlier versions.</td></tr>"}</tbody></table></div>')
    return "".join(out)


def _sheet_widget(w, c) -> str:
    spec = w.columns.get(c, {})
    if spec.get("choices") or spec.get("references"):
        return "select"
    widget = WIDGET_FOR.get(w.types.get(c, "string"), "text")
    return "text" if widget == "textarea" else widget


def _date_range(ctx: FormContext, w, column: str, value: str) -> tuple[date, date]:
    """A sheet filter on a date column: a preset, a date range, or a fiscal preset read from the
    calendar the column joins (ADR-009). Raises ValueError with the reason."""
    if value in dates.FISCAL_PRESETS:
        eng = ctx.project.engine(w.model)
        for target, fk in eng.idx.joins(w.entity).items():
            if fk != column:
                continue
            for f in eng.idx.fields(target):
                if f.kind == "dimension" and isinstance(f.spec.get("fiscal"), dict):
                    return eng.fiscal_range(f.qualified, value, ctx.as_of)
        raise ValueError(f"`{value}` needs a fiscal calendar, and `{column}` does not join an entity with a "
                         f"`fiscal:` date dimension")
    try:
        return dates.resolve(value, ctx.as_of)
    except ValueError:
        raise ValueError(f"`{value}` is not a date, a date range (`2026-01-01..2026-03-31`) or a date preset")             from None


def _match(value, ctype, wanted) -> bool:
    if ctype in ("date", "timestamp"):
        lo, hi = wanted
        v = value.date() if isinstance(value, datetime) else value
        return v is not None and lo <= v <= hi
    return _text(value) == wanted


# =========================================================================== writes


def handle_post(project: Project, user: dict, forms: dict[str, dict], body: dict[str, list[str]]):
    """Run one POST from a form or sheet. Returns (redirect URL or None, state for re-render).

    The page only reflects the write rules; this is where they hold (CLAUDE.md). A request built
    by hand gets the same refusals as the page: actions outside `allow`, values for columns the
    form or sheet does not show, changes to key, `readonly` or workflow columns, missing
    `required` fields, plus the column rules, `per` and workflow `by` that `Writer` enforces."""
    one = {k: v[0] for k, v in body.items()}
    fid, action, back = one.get("_form"), one.get("_action", "save"), one.get("_return") or "/"
    spec = forms.get(fid)
    if spec is None:
        return None, {"": {"errors": {"": [f"no form `{fid}`"]}}}
    writer = Writer(project, user)
    entity = spec["entity"]
    values = {k: v for k, v in one.items() if not k.startswith("_")}
    try:
        if not writer.grants.can_edit_form(fid):  # decided here, whatever the page showed
            raise WriteError({"": [f"{writer.who} may not edit {'form' if 'form' in spec else 'sheet'} `{fid}`; "
                                   f"access.yaml gives `edit` on it to none of their roles"]})
        w = find(project, entity)
        if "form" in spec:
            data = _form_values(spec, w, writer, values)
            if action.startswith("transition:"):
                writer.transition(entity, {k: data.get(k) for k in w.key}, action.split(":", 1)[1], via_form=fid)
            elif action == "save":
                writer.save(entity, data, via_form=fid)
            else:
                raise WriteError({"": [f"unknown action `{action}`"]})
            return one.get("_next") or back, None
        needed = SHEET_ACTIONS.get(action)
        if needed is None:
            raise WriteError({"": [f"unknown action `{action}`"]})
        allow = set(spec.get("allow") or ALL_ACTIONS)
        if needed not in allow:
            raise WriteError({"": [f"`{needed}` is not allowed on sheet `{fid}` (allow: {', '.join(sorted(allow))})"]})
        if action in ("sheet_save", "sheet_delete"):
            key = json.loads(one.get("_key") or "{}")
            if action == "sheet_delete":
                writer.delete(entity, key, via_form=fid)
            else:
                writer.save(entity, _sheet_edit(spec, w, writer, key, values), row_key=key, via_form=fid)
            return back, None
        editable = _sheet_new_columns(spec, w)
        if action == "sheet_add":
            writer.save(entity, _sheet_new(spec, w, values, editable), via_form=fid)
            return back, None
        text = one.get("_rows") or ""
        if action == "sheet_paste":
            records = []
            for n, ln in enumerate([ln for ln in text.splitlines() if ln.strip()], 1):
                cells = ln.split("\t")
                if len(cells) > len(editable):
                    raise WriteError({"": [f"row {n} has {len(cells)} values; expected {len(editable)} "
                                           f"({', '.join(editable)})"]})
                records.append(dict(zip(editable, cells)))
        else:
            records = list(csv.DictReader(io.StringIO(text)))
        problems = []
        project.con.execute("BEGIN TRANSACTION")
        try:
            for n, rec in enumerate(records, 1):
                try:
                    clean = {c: v for c, v in rec.items() if c is not None}
                    writer.save(entity, _sheet_new(spec, w, clean, editable), via_form=fid)
                except WriteError as e:
                    problems.append(f"row {n}: " + "; ".join(f"{c}: {m}" if c else m
                                                              for c, ms in e.errors.items() for m in ms))
            if problems:
                raise WriteError({"": problems})
            project.con.execute("COMMIT")
        except Exception:
            project.con.execute("ROLLBACK")
            raise
        return back, None
    except WriteError as e:
        if "form" in spec:
            return None, {fid: {"values": values, "errors": e.errors}}
        msgs = [f"{c}: {m}" if c else m for c, ms in e.errors.items() for m in ms]
        return None, {fid: {"errors": msgs, "values": values}}


def _foreign(values: dict, shown: list[str], w, where: str) -> dict[str, list[str]]:
    """Values for columns the form or sheet does not show: refused, never dropped."""
    status = (w.workflow or {}).get("column")
    out: dict[str, list[str]] = {}
    for c in values:
        if c in shown:
            continue
        if c == status:
            out.setdefault(c, []).append(f"`{c}` changes only through a transition")
        else:
            out.setdefault(c, []).append(f"`{c}` is not a field of {where}")
    return out


def _same(writer, value, stored, ctype) -> bool:
    try:
        return writer._coerce(value, ctype) == writer._coerce(stored, ctype)
    except ValueError:
        return False


def _form_values(spec: dict, w, writer, values: dict) -> dict:
    fields = {f["field"]: f for f in spec["fields"]}
    errors = _foreign(values, list(fields), w, f"form `{spec['form']}`")
    data = {k: values.get(k) for k in fields}
    for name in fields:  # an unchecked checkbox sends nothing
        if w.types.get(name) == "boolean" and name not in values:
            data[name] = "false"
    key = {k: data.get(k) for k in w.key}
    current = None
    if all(v not in (None, "") for v in key.values()):
        try:
            current = writer.get(spec["entity"], {k: writer._coerce(v, w.types[k]) for k, v in key.items()})
        except ValueError:
            current = None
    for name, f in fields.items():
        given = data.get(name)
        if f.get("required") and (given is None or str(given).strip() == ""):
            errors.setdefault(name, []).append("is required")
        if not f.get("readonly") or name in w.key:
            continue  # key fields pick the row; they cannot change it
        if current is not None:
            if name in values and not _same(writer, given, current.get(name), w.types[name]):
                errors.setdefault(name, []).append(f"`{name}` is read-only on this form")
        elif "filters." not in str(f.get("default", "")):
            # Without dashboard filters the server knows the value: the default, or nothing.
            expected = writer._default(f.get("default", w.columns.get(name, {}).get("default")))
            if given not in (None, "") and not _same(writer, given, expected, w.types[name]):
                errors.setdefault(name, []).append(f"`{name}` is read-only on this form")
    if errors:
        raise WriteError(errors)
    return data


def _sheet_locked(spec: dict, w) -> set[str]:
    return (set(spec.get("readonly") or []) | set(w.key) | {(w.workflow or {}).get("column")}) - {None}


def _sheet_edit(spec: dict, w, writer, key: dict, values: dict) -> dict:
    errors = _foreign(values, list(spec["columns"]), w, f"sheet `{spec['sheet']}`")
    current = writer.get(spec["entity"], {k: writer._coerce(key.get(k), w.types[k]) for k in w.key})
    if current is None:  # hidden or gone: the same answer, never "outside your rows" (ADR-013)
        raise WriteError({"": [NOT_FOUND]})
    locked = _sheet_locked(spec, w)
    for c in locked:
        if c in values and not _same(writer, values[c], current.get(c), w.types.get(c, "string")):
            if c in w.key:
                errors.setdefault(c, []).append(f"`{c}` is a key column and cannot change; "
                                                f"delete the row and add a new one")
            else:
                errors.setdefault(c, []).append(f"`{c}` is read-only on this sheet")
    if errors:
        raise WriteError(errors)
    return {**key, **{c: v for c, v in values.items() if c not in locked}}


def _sheet_new_columns(spec: dict, w) -> list[str]:
    """Columns a new row may set: the sheet's columns except `readonly` and workflow ones."""
    locked = set(spec.get("readonly") or []) | {(w.workflow or {}).get("column")}
    return [c for c in spec["columns"] if c not in locked]


def _sheet_new(spec: dict, w, values: dict, editable: list[str]) -> dict:
    given = {c: v for c, v in values.items() if c in editable or v not in (None, "")}
    errors = _foreign(given, editable, w, f"sheet `{spec['sheet']}`")
    for c in set(spec.get("readonly") or []) & set(errors):
        errors[c] = [f"`{c}` is read-only on this sheet; new rows take its default"]
    if errors:
        raise WriteError(errors)
    return {c: values.get(c) for c in editable if c in values}


def parse_body(raw: bytes) -> dict[str, list[str]]:
    return parse_qs(raw.decode("utf-8"), keep_blank_values=True)
