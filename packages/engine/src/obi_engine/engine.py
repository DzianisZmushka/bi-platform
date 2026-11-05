"""Thin semantic engine (ADR-001): semantic query -> DuckDB SQL -> Arrow table.

Plan per query:
- Each base measure belongs to one fact entity. Each fact gets one CTE: the fact
  table joined along its single join path to every entity a requested dimension or
  filter needs, grouped by the requested dimensions.
- Facts are combined with FULL OUTER JOIN ... USING (dimensions): drill-across.
- Calculated measures are computed in the outer SELECT from the base measures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import re

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp

from obi_formats.artifacts import parse
from obi_formats.colexpr import ColExprError, parse as parse_colexpr, references
from obi_formats.modelindex import MEASURE_KINDS, Field, ModelIndex, Ref, RefError

AGG_SQL = {"sum": "SUM({})", "count": "COUNT({})", "count_distinct": "COUNT(DISTINCT {})",
           "avg": "AVG({})", "min": "MIN({})", "max": "MAX({})"}
DOUBLE_AGGS = ("sum", "avg")  # cast so integer columns do not come back as DECIMAL(38)


# Model and project properties that have runtime behaviour, per schema definition: used by
# this engine, by the model index (joins, hierarchies) or by the validator (grain).
# packages/cli/tests/test_coverage.py checks these against the schemas.
HANDLED_MODEL: dict[str, set[str]] = {
    "model": {"model", "title", "entities", "hierarchies"},
    "entity": {"table", "key", "grain", "joins", "dimensions", "measures"},
    "dimension": {"label", "column", "type"},
    "measure": {"sum", "count", "count_distinct", "avg", "min", "max", "calc", "label", "where", "grain",
                "format"},
}
HANDLED_PROJECT: dict[str, set[str]] = {
    "project": {"project", "title", "description", "default_connection", "currency", "connections"},
    "connection": {"duckdb", "tables"},
}
# Read by people and LLMs; no runtime behaviour by design, so not "silently ignored".
DOCUMENTATION_ONLY: dict[str, set[str]] = {
    "model": {"description"},
    "entity": {"description"},
    "dimension": {"description"},
    "measure": {"description"},
}


class EngineError(Exception):
    pass


@dataclass
class Filter:
    """A filter on a dimension: a list of values, or an inclusive date range."""
    dimension: str
    values: list | None = None
    range: tuple[date, date] | None = None


@dataclass
class SemanticQuery:
    measures: list[str]
    dimensions: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    sort: list[str] = field(default_factory=list)   # "-net_revenue" for descending
    limit: int | None = None


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(v) -> str:
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'"
    return exp.convert(v).sql("duckdb")


class Project:
    """A project directory: connection plus its models."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.config = parse((self.root / "project.yaml").read_text(encoding="utf-8")).data
        self.currency = self.config.get("currency", "USD")
        self.con = duckdb.connect()
        conn = self.config["connections"][self.config["default_connection"]]
        for table, rel in (conn.get("tables") or {}).items():
            path = (self.root / rel).as_posix().replace("'", "''")
            reader = "read_parquet" if rel.endswith(".parquet") else "read_csv_auto"
            # Loaded once per Project: a view would re-read the file on every query.
            self.con.execute(f"CREATE TABLE {_q(table)} AS SELECT * FROM {reader}('{path}')")
        self._models: dict[str, Engine] = {}

    def engine(self, model: str) -> "Engine":
        if model not in self._models:
            path = self.root / "model" / f"{model}.model.yaml"
            data = parse(path.read_text(encoding="utf-8")).data
            self._models[model] = Engine(self.con, ModelIndex(model, data))
        return self._models[model]


class Engine:
    def __init__(self, con: duckdb.DuckDBPyConnection, idx: ModelIndex):
        self.con, self.idx = con, idx

    # ------------------------------------------------------------------ public

    def query(self, q: SemanticQuery) -> pa.Table:
        sql = self.sql(q)
        try:
            return self.con.sql(sql).to_arrow_table()
        except duckdb.Error as e:  # never surface a raw database error (ADR-008)
            raise EngineError(translate_db_error(e)) from None

    def check_measures(self) -> list[tuple[Field, str]]:
        """Run every measure once against the data (no rows returned) and report the ones
        the database cannot compute. `obi validate` maps these to file:line."""
        problems = []
        for f in self.idx.fields():
            if f.kind != "measure":
                continue
            try:
                self.con.sql(self.sql(SemanticQuery([f.qualified], limit=1))).fetchall()
            except EngineError as e:
                problems.append((f, e.args[0]))
            except duckdb.Error as e:
                problems.append((f, translate_db_error(e)))
        return problems

    def distinct(self, dimension: str, limit: int = 500) -> list:
        ref = self._dim(dimension)
        e = self.idx.entities[ref.field.entity]
        col = f"t.{_q(self._column(ref.field))}"
        sql = (f"SELECT DISTINCT {col} AS v FROM {_q(e['table'])} AS t WHERE {col} IS NOT NULL "
               f"ORDER BY 1 LIMIT {limit}")
        return [r[0] for r in self.con.sql(sql).fetchall()]

    def label(self, ref_text: str) -> str:
        r = self.idx.resolve(ref_text)
        if r.field and r.field.spec.get("label"):
            return r.field.spec["label"]
        return humanize(r.name)

    # ------------------------------------------------------------------ planning

    def _resolve(self, text: str, want) -> Ref:
        try:
            return self.idx.resolve(text, want)
        except RefError as e:
            raise EngineError(e.message) from None

    def _dim(self, text: str) -> Ref:
        r = self._resolve(text, ("dimension", "hierarchy"))
        if r.hierarchy:  # Phase 0 shows one level at a time: the top one
            levels = self.idx.levels(r)
            if not levels:
                raise EngineError(f"hierarchy `{r.hierarchy}` has no valid levels")
            return levels[0]
        return r

    @staticmethod
    def _column(f: Field) -> str:
        return f.spec.get("column", f.name)

    def sql(self, q: SemanticQuery) -> str:
        if not q.measures:
            raise EngineError("a query needs at least one measure")
        dims = [self._dim(d) for d in q.dimensions]
        names = _output_names(dims)
        filters = [(self._dim(f.dimension), f) for f in q.filters]

        base: dict[Field, str] = {}  # base measure -> internal column alias
        outputs = []
        for m in q.measures:
            ref = self._resolve(m, ("measure",))
            outputs.append((ref.field.name, self._measure_expr(ref.field, base, ())))

        by_fact: dict[str, list[Field]] = {}
        for f in base:
            by_fact.setdefault(f.entity, []).append(f)
        ctes = [self._fact_cte(fact, ms, base, dims, names, filters) for fact, ms in by_fact.items()]

        cte_names = [f"f{i}" for i in range(len(ctes))]
        sql = "WITH " + ", ".join(f"{n} AS ({c})" for n, c in zip(cte_names, ctes)) + " "
        frm = cte_names[0]
        for n in cte_names[1:]:
            frm += (f" FULL OUTER JOIN {n} USING ({', '.join(_q(x) for x in names)})" if names
                    else f" CROSS JOIN {n}")
        select = [_q(n) for n in names] + [f"{e} AS {_q(name)}" for name, e in outputs]
        sql += f"SELECT {', '.join(select)} FROM {frm}"

        order = []
        for s in q.sort:
            key = s.lstrip("-")
            short = key.split(".")[-2] if key.split(".")[-1] in ("day", "week", "month", "quarter", "year") \
                and "." in key else key.split(".")[-1]
            if short not in names and short not in [o[0] for o in outputs]:
                raise EngineError(f"cannot sort by `{key}`: it is not part of the query")
            order.append(f"{_q(short)} {'DESC' if s.startswith('-') else 'ASC'} NULLS LAST")
        order += [f"{_q(n)} ASC NULLS LAST" for n in names if f"{_q(n)} " not in " ".join(order)]
        if order:
            sql += " ORDER BY " + ", ".join(order)
        if q.limit:
            sql += f" LIMIT {int(q.limit)}"
        return sql

    def _measure_expr(self, f: Field, base: dict, stack: tuple) -> str:
        """SQL for a measure in the outer SELECT, registering the base measures it needs."""
        kinds = [k for k in MEASURE_KINDS if k in f.spec]
        if not kinds:
            raise EngineError(f"measure `{f.name}` has no aggregation")
        if kinds[0] != "calc":
            base.setdefault(f, f"__m{len(base)}")
            return _q(base[f])
        if f in stack:
            raise EngineError(f"calc measure `{f.name}` refers to itself")
        def rewrite(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column):
                text = ".".join(p for p in (node.table, node.name) if p)
                inner = self._measure_expr(self._resolve(text, ("measure",)).field, base, stack + (f,))
                return exp.Paren(this=sqlglot.parse_one(inner, read="duckdb"))  # inner is our own SQL
            for key, child in list(node.args.items()):
                if isinstance(child, exp.Expression):
                    node.set(key, rewrite(child))
                elif isinstance(child, list):
                    node.set(key, [rewrite(c) if isinstance(c, exp.Expression) else c for c in child])
            if isinstance(node, exp.Div):  # x / 0 is NULL, not infinity (DuckDB 2 returns inf)
                node.set("expression", exp.Nullif(this=node.expression, expression=exp.Literal.number(0)))
            return node

        return rewrite(_checked(f.spec["calc"], f, "calc")).sql("duckdb")

    def _fact_cte(self, fact: str, measures: list[Field], base: dict, dims: list[Ref],
                  names: list[str], filters: list) -> str:
        aliases = {fact: "t0"}
        joins: list[str] = []

        def alias_of(entity: str, why: str) -> str:
            if entity in aliases:
                return aliases[entity]
            path = self._path(fact, entity)
            if path is None:
                raise EngineError(f"{why} cannot be reached from entity `{fact}`; check the measure's grain")
            for src, dst, fk in path:
                if dst not in aliases:
                    aliases[dst] = f"t{len(aliases)}"
                    key = self.idx.entities[dst]["key"]
                    key = key[0] if isinstance(key, list) else key
                    joins.append(f"LEFT JOIN {_q(self.idx.entities[dst]['table'])} AS {aliases[dst]} "
                                 f"ON {aliases[src]}.{_q(fk)} = {aliases[dst]}.{_q(key)}")
            return aliases[entity]

        select = []
        for name, d in zip(names, dims):
            col = f"{alias_of(d.field.entity, f'dimension `{d.text}`')}.{_q(self._column(d.field))}"
            if d.grain and d.grain != "day":
                col = f"CAST(date_trunc('{d.grain}', {col}) AS DATE)"
            select.append(f"{col} AS {_q(name)}")
        for m in measures:
            kind = next(k for k in MEASURE_KINDS if k in m.spec)
            expr = _qualify(_checked(m.spec[kind], m, kind), "t0")
            agg = AGG_SQL[kind].format(expr)
            if m.spec.get("where"):
                agg += f" FILTER (WHERE {self._where_sql(m, alias_of)})"
            if kind in DOUBLE_AGGS:
                agg = f"CAST({agg} AS DOUBLE)"
            select.append(f"{agg} AS {_q(base[m])}")

        where = []
        for d, f in filters:
            col = f"{alias_of(d.field.entity, f'filter on `{f.dimension}`')}.{_q(self._column(d.field))}"
            if f.range:
                where.append(f"{col} BETWEEN {_lit(f.range[0])} AND {_lit(f.range[1])}")
            elif f.values is not None:
                vals = ", ".join(_lit(v) for v in f.values)
                where.append(f"{col} IN ({vals})" if f.values else "FALSE")

        sql = f"SELECT {', '.join(select)} FROM {_q(self.idx.entities[fact]['table'])} AS t0"
        if joins:
            sql += " " + " ".join(joins)
        if where:
            sql += " WHERE " + " AND ".join(where)
        if dims:
            sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims)))
        return sql

    def _where_sql(self, m: Field, alias_of) -> str:
        """`where:` names model dimensions (ADR-008): each is resolved and joined like a
        chart axis, so a filtered measure can use attributes of joined entities."""
        tree = _checked(m.spec["where"], m, "where")
        for text, col in references(tree):
            r = self._resolve(text, ("dimension",))
            sql = f"{alias_of(r.field.entity, f'dimension `{text}` in the `where` of measure `{m.name}`')}" \
                  f".{_q(self._column(r.field))}"
            if r.grain and r.grain != "day":
                sql = f"CAST(date_trunc('{r.grain}', {sql}) AS DATE)"
            col.replace(sqlglot.parse_one(sql, read="duckdb"))
        return tree.sql("duckdb")

    def _path(self, start: str, target: str):
        """The single join path from `start` to `target` (ADR-001), as (src, dst, fk) steps."""
        todo = [(start, [])]
        seen = {start}
        while todo:
            ent, path = todo.pop(0)
            if ent == target:
                return path
            for dst, fk in self.idx.joins(ent).items():
                if dst not in seen:
                    seen.add(dst)
                    todo.append((dst, path + [(ent, dst, fk)]))
        return None


def _checked(text: str, m: Field, key: str) -> exp.Expression:
    try:
        return parse_colexpr(text)
    except ColExprError as e:
        raise EngineError(f"measure `{m.name}`, `{key}`: {e.message}") from None


_TYPE_WORDS = {"VARCHAR": "text", "BIGINT": "number", "INTEGER": "number", "DOUBLE": "number",
               "DECIMAL": "number", "HUGEINT": "number", "DATE": "date", "TIMESTAMP": "date-time",
               "BOOLEAN": "true/false"}


def translate_db_error(e: Exception) -> str:
    """A database error in model terms. The validator should have caught the cause first;
    this is the last line of defence, so it never passes database jargon through."""
    msg = str(e).splitlines()[0]
    msg = re.sub(r"^[A-Za-z ]*(Error|Exception): ", "", msg).strip()
    if m := re.search(r'Referenced column "([^"]+)" not found', msg):
        return f"column `{m.group(1)}` does not exist in the data"
    if m := re.search(r'Referenced table "?([^"!]+?)"? not found', msg):
        return f"table `{m.group(1)}` is not available to this query"
    if m := re.search(r"No function matches the given name and argument types '(\w+)\(([^)]*)\)'", msg):
        types = ", ".join(dict.fromkeys(_TYPE_WORDS.get(x.strip().upper(), x.strip().lower())
                                        for x in m.group(2).split(",") if x.strip()))
        return f"function `{m.group(1).lower()}` cannot be used on {types or 'these'} values"
    if m := re.search(r"Could not convert string '([^']*)' to (\w+)", msg):
        return f"the value `{m.group(1)}` cannot be read as {m.group(2).lower()}"
    if "Conversion Error" in str(e) or "Cannot compare" in msg or "Cannot mix" in msg:
        return "values of different types are compared or combined; check the column types"
    return "the database could not compute this; run `obi validate` for details"


def _qualify(tree: exp.Expression, alias: str) -> str:
    for col in tree.find_all(exp.Column):
        if not col.table:
            col.set("table", exp.to_identifier(alias))
    return tree.sql("duckdb")


def _output_names(dims: list[Ref]) -> list[str]:
    names = []
    for d in dims:
        n = d.field.name
        names.append(n if n not in names else f"{d.field.entity}_{n}")
    return names


def humanize(name: str) -> str:
    s = name.replace("_", " ")
    return s[:1].upper() + s[1:]
