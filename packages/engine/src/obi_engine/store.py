"""Writable entities (ADR-012): the tables the platform owns, and the one write path.

`migrate` creates and alters tables from the model, with audit columns and a history table.
`Writer` is the only way rows change: form, sheet, paste and CSV import all call it, so every
path enforces the same rules (types, required, choices, references, `check:`, `per`, workflow).
Concurrency is last-write-wins; every prior version of a row is kept in `<table>_history`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from obi_formats.colexpr import parse as parse_colexpr
from obi_formats.expr import ExprError, render as render_expr
from obi_formats.modelindex import AUDIT_COLUMNS, ModelIndex, RefError

from .engine import STORE, Project, _qualify, _q, _lit

SQL_TYPES = {"id": "BIGINT", "string": "VARCHAR", "text": "VARCHAR", "number": "DOUBLE", "integer": "BIGINT",
             "date": "DATE", "timestamp": "TIMESTAMP", "boolean": "BOOLEAN"}
HISTORY_COLUMNS = {"history_op": "VARCHAR", "history_at": "TIMESTAMP", "history_by": "VARCHAR"}


class WriteError(Exception):
    """The row was not written. `errors` maps a column (or "" for the row) to messages."""

    def __init__(self, errors: dict[str, list[str]]):
        super().__init__("; ".join(f"{c}: {m}" if c else m for c, ms in errors.items() for m in ms))
        self.errors = errors


@dataclass
class Writable:
    model: str
    idx: ModelIndex
    entity: str

    @property
    def spec(self) -> dict:
        return self.idx.entities[self.entity]

    @property
    def table(self) -> str:
        return self.spec["table"]

    @property
    def columns(self) -> dict[str, dict]:
        cols = self.spec.get("columns") or {}
        return {k: (v if isinstance(v, dict) else {}) for k, v in cols.items()}

    @property
    def key(self) -> list[str]:
        return self.idx.key_columns(self.entity)

    @property
    def types(self) -> dict[str, str]:
        return self.idx.table_columns(self.entity)

    @property
    def workflow(self) -> dict | None:
        wf = self.spec.get("workflow")
        return wf if isinstance(wf, dict) else None


def writables(project: Project) -> list[Writable]:
    out = []
    for model in project.models():
        idx = project.engine(model).idx
        out += [Writable(model, idx, e) for e in idx.entities if idx.is_writable(e)]
    return out


def find(project: Project, entity: str) -> Writable:
    for w in writables(project):
        if w.entity == entity:
            return w
    raise WriteError({"": [f"entity `{entity}` is not a writable entity of this project"]})


def _qt(table: str) -> str:
    return f"{STORE}.main.{_q(table)}"


# =========================================================================== migrate


def _existing(project: Project, table: str) -> dict[str, str] | None:
    rows = project.con.sql(f"SELECT column_name, data_type FROM duckdb_columns() WHERE database_name = '{STORE}' "
                           f"AND table_name = {_lit(table)}").fetchall()
    return {c: t for c, t in rows} or None


def plan(project: Project) -> list[str]:
    """What `migrate` would do, in words. Empty when the database matches the model."""
    if project.database is None:
        return []
    steps = []
    for w in writables(project):
        have = _existing(project, w.table)
        if have is None:
            steps.append(f"create table `{w.table}`")
            continue
        for c in w.types:
            if c not in have:
                steps.append(f"add column `{w.table}.{c}`")
    return steps


def migrate(project: Project, reseed: tuple[str, ...] = ()) -> list[str]:
    """Create or alter every writable table, its history table, and load seeds into new tables."""
    if project.database is None:
        if writables(project):
            raise WriteError({"": ["writable entities need `duckdb: { database: <file> }` in project.yaml"]})
        return []
    seeds = project.connection.get("seeds") or {}
    done = []
    for w in writables(project):
        have = _existing(project, w.table)
        cols = w.types
        if have is None:
            body = ", ".join(f"{_q(c)} {SQL_TYPES.get(t, 'VARCHAR')}" for c, t in cols.items())
            key = ", ".join(_q(k) for k in w.key)
            project.con.execute(f"CREATE TABLE {_qt(w.table)} ({body}, PRIMARY KEY ({key}))")
            hist = body + ", " + ", ".join(f"{c} {t}" for c, t in HISTORY_COLUMNS.items())
            project.con.execute(f"CREATE TABLE IF NOT EXISTS {_qt(w.table + '_history')} ({hist})")
            done.append(f"created table `{w.table}` and `{w.table}_history`")
            if w.table in seeds:
                done.append(_seed(project, w, seeds[w.table]))
            continue
        for c, t in cols.items():
            if c not in have:
                for table in (w.table, w.table + "_history"):
                    project.con.execute(f"ALTER TABLE {_qt(table)} ADD COLUMN {_q(c)} {SQL_TYPES.get(t, 'VARCHAR')}")
                done.append(f"added column `{w.table}.{c}`")
        if w.table in reseed:
            if w.table not in seeds:
                raise WriteError({"": [f"`{w.table}` has no seed in project.yaml"]})
            _to_history(project, w, "1 = 1", "reseed", "obi migrate")
            project.con.execute(f"DELETE FROM {_qt(w.table)}")
            done.append(_seed(project, w, seeds[w.table]))
    return done


def _seed(project: Project, w: Writable, rel: str) -> str:
    path = (project.root / rel).as_posix().replace("'", "''")
    head = [r[0] for r in project.con.sql(f"DESCRIBE SELECT * FROM read_csv_auto('{path}')").fetchall()]
    cols = [c for c in head if c in w.types and c not in AUDIT_COLUMNS]
    now = datetime.now().replace(microsecond=0)
    project.con.execute(
        f"INSERT INTO {_qt(w.table)} ({', '.join(_q(c) for c in cols)}, created_by, created_at, updated_by, updated_at) "
        f"SELECT {', '.join(_q(c) for c in cols)}, 'seed', {_lit(now)}, 'seed', {_lit(now)} "
        f"FROM read_csv_auto('{path}')")
    n = project.con.sql(f"SELECT COUNT(*) FROM {_qt(w.table)}").fetchall()[0][0]
    return f"seeded `{w.table}` from {rel} ({n} rows)"


def _lit(v) -> str:  # noqa: F811 - dates and timestamps as typed literals
    if isinstance(v, datetime):
        return f"TIMESTAMP '{v.isoformat(sep=' ')}'"
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'"
    if v is None:
        return "NULL"
    from sqlglot import exp
    return exp.convert(v).sql("duckdb")


def _to_history(project: Project, w: Writable, where: str, op: str, by: str):
    cols = ", ".join(_q(c) for c in w.types)
    project.con.execute(f"INSERT INTO {_qt(w.table + '_history')} ({cols}, history_op, history_at, history_by) "
                        f"SELECT {cols}, {_lit(op)}, {_lit(datetime.now().replace(microsecond=0))}, {_lit(by)} "
                        f"FROM {_qt(w.table)} WHERE {where}")


# =========================================================================== writes


@dataclass
class Writer:
    project: Project
    user: dict = field(default_factory=dict)

    @property
    def who(self) -> str:
        return str(self.user.get("name") or "anonymous")

    # ---- reading

    def get(self, entity: str, key: dict) -> dict | None:
        w = find(self.project, entity)
        rows = self._rows(w, self._key_where(w, key))
        return rows[0] if rows else None

    def rows(self, entity: str, where: dict | None = None, sort: str | None = None) -> list[dict]:
        w = find(self.project, entity)
        cond = " AND ".join(f"{_q(c)} = {_lit(v)}" for c, v in (where or {}).items()) or "1 = 1"
        # The key breaks ties, so a row does not move after it is saved.
        order = " ORDER BY " + ", ".join(
            ([f"{_q(sort.lstrip('-'))} {'DESC' if sort.startswith('-') else 'ASC'}"] if sort else [])
            + [_q(k) for k in w.key])
        return self._rows(w, cond, order)

    def history(self, entity: str, key: dict) -> list[dict]:
        w = find(self.project, entity)
        rel = self.project.con.sql(f"SELECT * FROM {_qt(w.table + '_history')} WHERE {self._key_where(w, key)} "
                                   f"ORDER BY history_at DESC, rowid DESC")
        return [dict(zip(rel.columns, r)) for r in rel.fetchall()]

    def _rows(self, w: Writable, where: str, order: str = "") -> list[dict]:
        rel = self.project.con.sql(f"SELECT * FROM {_qt(w.table)} WHERE {where}{order}")
        return [dict(zip(rel.columns, r)) for r in rel.fetchall()]

    def _key_where(self, w: Writable, key: dict) -> str:
        return " AND ".join(f"{_q(k)} = {_lit(key.get(k))}" if key.get(k) is not None else f"{_q(k)} IS NULL"
                            for k in w.key)

    # ---- choices for a `references` column: (key, display) pairs

    def options(self, entity: str, column: str) -> list[tuple]:
        w = find(self.project, entity)
        ref = w.columns.get(column, {}).get("references")
        if not ref:
            return []
        eng = self.project.engine(w.model)
        d = eng.idx.resolve(ref, ("dimension",)).field
        key = eng.idx.key_columns(d.entity)[0]
        disp = d.spec.get("column", d.name)
        return self.project.con.sql(f"SELECT DISTINCT {_q(key)}, {_q(disp)} FROM {eng.table(d.entity)} "
                                    f"ORDER BY 2").fetchall()

    # ---- writing

    def save(self, entity: str, values: dict, row_key: dict | None = None) -> dict:
        """Insert or update one row and return its key. `values` may be strings (from HTML);
        `row_key` identifies the row being edited when the key columns are not editable."""
        w = find(self.project, entity)
        errors: dict[str, list[str]] = {}
        typed = {}
        for c, v in values.items():
            if c not in w.types or c in AUDIT_COLUMNS:
                continue
            try:
                typed[c] = self._coerce(v, w.types[c])
            except ValueError:
                errors.setdefault(c, []).append(f"`{v}` is not a valid {w.types[c]}")
        current = None
        key_src = row_key or typed
        if all(key_src.get(k) is not None for k in w.key):
            current = self.get(entity, {k: self._coerce(key_src[k], w.types[k]) for k in w.key})
        row = dict(current or {})
        status = (w.workflow or {}).get("column")
        if status and status in typed and current is not None and typed[status] != current.get(status):
            errors.setdefault(status, []).append(f"`{status}` changes only through a transition")
        row.update({k: v for k, v in typed.items() if not (status and k == status and current is not None)})
        if current is None:
            for c, spec in w.columns.items():
                if row.get(c) is None and spec.get("type") == "id":
                    row[c] = self.project.con.sql(f"SELECT COALESCE(MAX({_q(c)}), 0) + 1 "
                                                  f"FROM {_qt(w.table)}").fetchall()[0][0]
                elif row.get(c) is None and "default" in spec:
                    row[c] = self._coerce(self._default(spec["default"]), w.types[c])
        self._references(w, row, errors)
        self._rules(w, row, errors)
        self._parent(w, row, errors)
        if errors:
            raise WriteError(errors)
        now = datetime.now().replace(microsecond=0)
        key = {k: row[k] for k in w.key}
        if current is not None:
            _to_history(self.project, w, self._key_where(w, {k: current[k] for k in w.key}), "update", self.who)
            sets = ", ".join(f"{_q(c)} = {_lit(row.get(c))}" for c in w.types if c not in AUDIT_COLUMNS)
            self.project.con.execute(f"UPDATE {_qt(w.table)} SET {sets}, updated_by = {_lit(self.who)}, "
                                     f"updated_at = {_lit(now)} WHERE {self._key_where(w, {k: current[k] for k in w.key})}")
        else:
            row.update(created_by=self.who, created_at=now, updated_by=self.who, updated_at=now)
            cols = [c for c in w.types if c in row]
            self.project.con.execute(f"INSERT INTO {_qt(w.table)} ({', '.join(_q(c) for c in cols)}) VALUES "
                                     f"({', '.join(_lit(row[c]) for c in cols)})")
        return key

    def delete(self, entity: str, key: dict):
        w = find(self.project, entity)
        where = self._key_where(w, {k: self._coerce(key.get(k), w.types[k]) for k in w.key})
        _to_history(self.project, w, where, "delete", self.who)
        self.project.con.execute(f"DELETE FROM {_qt(w.table)} WHERE {where}")

    def transitions(self, entity: str, row: dict | None) -> list[str]:
        """Transitions the current user may perform on this row now."""
        w = find(self.project, entity)
        if not w.workflow or row is None:
            return []
        out = []
        for name, tr in (w.workflow.get("transitions") or {}).items():
            frm = tr.get("from") if isinstance(tr.get("from"), list) else [tr.get("from")]
            if row.get(w.workflow["column"]) in frm and self._allowed(tr):
                out.append(name)
        return out

    def transition(self, entity: str, key: dict, name: str) -> str:
        w = find(self.project, entity)
        row = self.get(entity, {k: self._coerce(key.get(k), w.types[k]) for k in w.key})
        tr = ((w.workflow or {}).get("transitions") or {}).get(name)
        if row is None or tr is None:
            raise WriteError({"": [f"no row or no transition `{name}`"]})
        col = w.workflow["column"]
        frm = tr.get("from") if isinstance(tr.get("from"), list) else [tr.get("from")]
        if row.get(col) not in frm:
            raise WriteError({col: [f"`{name}` needs status {' or '.join(frm)}; it is `{row.get(col)}`"]})
        if not self._allowed(tr):
            raise WriteError({col: [f"`{name}` is not allowed for {self.who} (`{tr.get('by')}`)"]})
        where = self._key_where(w, {k: row[k] for k in w.key})
        _to_history(self.project, w, where, f"transition:{name}", self.who)
        now = datetime.now().replace(microsecond=0)
        self.project.con.execute(f"UPDATE {_qt(w.table)} SET {_q(col)} = {_lit(tr['to'])}, updated_by = "
                                 f"{_lit(self.who)}, updated_at = {_lit(now)} WHERE {where}")
        return tr["to"]

    # ---- rules

    def _allowed(self, tr: dict) -> bool:
        if not tr.get("by"):
            return True
        try:
            return bool(render_expr(tr["by"], {"user": self.user, "filters": {}, "row": {}, "value": None}))
        except ExprError:
            return False

    def _default(self, text):
        if isinstance(text, str) and "${" in text:
            return render_expr(text, {"user": self.user, "filters": {}, "row": {}, "value": None})
        return text

    @staticmethod
    def _coerce(v, t: str):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        if isinstance(v, str):
            v = v.strip() if t != "text" else v
        if t in ("id", "integer"):
            return int(float(v)) if isinstance(v, str) else int(v)
        if t == "number":
            return float(v)
        if t == "date":
            return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])
        if t == "timestamp":
            return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
        if t == "boolean":
            return v if isinstance(v, bool) else str(v).lower() in ("true", "on", "1", "yes")
        return str(v)

    def _references(self, w: Writable, row: dict, errors: dict):
        """A `references` value may be the key or the referenced dimension's display value;
        a display value is resolved to the key (ADR-012), and must be unambiguous."""
        for c, spec in w.columns.items():
            ref, v = spec.get("references"), row.get(c)
            if not ref or v is None:
                continue
            pairs = self.options(w.entity, c)
            keys = {str(k) for k, _ in pairs}
            if str(v) in keys:
                continue
            matches = [k for k, d in pairs if str(d) == str(v)]
            if len(matches) == 1:
                row[c] = matches[0]
            elif len(matches) > 1:
                errors.setdefault(c, []).append(f"`{v}` matches several {ref} values; choose one from the list")
            else:
                errors.setdefault(c, []).append(f"`{v}` is not a known {ref}")

    def _rules(self, w: Writable, row: dict, errors: dict):
        for c, spec in w.columns.items():
            v = row.get(c)
            if spec.get("required") and v is None and spec.get("type") != "id" and not errors.get(c):
                errors.setdefault(c, []).append("is required")
            if v is not None and spec.get("choices") and v not in spec["choices"]:
                errors.setdefault(c, []).append(f"must be one of {', '.join(map(str, spec['choices']))}")
            if isinstance(spec.get("check"), str) and not errors.get(c):
                values = ", ".join(f"{_lit(row.get(k))}::{SQL_TYPES.get(t, 'VARCHAR')} AS {_q(k)}"
                                   for k, t in w.types.items())
                expr = _qualify(parse_colexpr(spec["check"]), "r")
                ok = self.project.con.sql(f"SELECT ({expr}) IS NOT FALSE FROM (SELECT {values}) AS r").fetchall()[0][0]
                if not ok:
                    errors.setdefault(c, []).append(f"fails the rule `{spec['check']}`")

    def _parent(self, w: Writable, row: dict, errors: dict):
        parent = w.spec.get("per")
        if not isinstance(parent, str):
            return
        eng = self.project.engine(w.model)
        keys = eng.idx.key_columns(parent)
        if any(row.get(k) is None for k in keys):
            errors.setdefault("", []).append(f"the {parent} key ({', '.join(keys)}) is required")
            return
        where = " AND ".join(f"{_q(k)} = {_lit(row[k])}" for k in keys)
        if not self.project.con.sql(f"SELECT 1 FROM {eng.table(parent)} WHERE {where} LIMIT 1").fetchall():
            errors.setdefault("", []).append(f"there is no {parent} row with {', '.join(f'{k} = {row[k]}' for k in keys)}")


def auto_migrate(project: Project) -> list[str]:
    """`obi serve` and `obi validate --execute`: bring the database up to the model when it is
    missing or behind; nothing to do otherwise."""
    if project.database is None or not writables(project):
        return []
    if project.database.exists() and not plan(project):
        return []
    return migrate(project)
