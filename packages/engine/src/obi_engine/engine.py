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
from datetime import date, timedelta
from pathlib import Path

import re
import threading

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp

from obi_formats.artifacts import parse
from obi_formats.colexpr import ColExprError, parse as parse_colexpr, references
from obi_formats.modelindex import FISCAL_GRAINS, MEASURE_KINDS, Field, ModelIndex, Ref, RefError

AGG_SQL = {"sum": "SUM({})", "count": "COUNT({})", "count_distinct": "COUNT(DISTINCT {})",
           "avg": "AVG({})", "min": "MIN({})", "max": "MAX({})"}
DOUBLE_AGGS = ("sum", "avg")  # cast so integer columns do not come back as DECIMAL(38)


# Model and project properties that have runtime behaviour, per schema definition: used by
# this engine, by the model index (joins, hierarchies) or by the validator (grain).
# packages/cli/tests/test_coverage.py checks these against the schemas.
HANDLED_MODEL: dict[str, set[str]] = {
    "model": {"model", "title", "currency", "entities", "hierarchies"},
    "currency": {"target", "rates"},
    "rates": {"table", "currency", "month", "rate"},
    "entity": {"table", "key", "grain", "currency", "writable", "per", "columns", "workflow", "joins", "rollups",
               "dimensions", "measures"},
    "column": {"label", "description", "type", "required", "default", "choices", "references", "check"},
    "workflow": {"column", "transitions"},
    "transition": {"from", "to", "by"},
    "rollup": {"sum", "count", "min", "max", "from"},
    "entity_currency": {"code", "date"},
    "dimension": {"label", "column", "type", "fiscal", "lookup", "key", "match"},
    "fiscal": {"year", "period"},
    "measure": {"sum", "count", "count_distinct", "avg", "min", "max", "calc", "label", "where", "grain",
                "date", "convert", "format"},
}
HANDLED_PROJECT: dict[str, set[str]] = {
    "project": {"project", "title", "description", "default_connection", "currency", "connections"},
    "connection": {"duckdb", "tables", "seeds"},
    "connection.duckdb": {"database"},
}
# Read by people and LLMs; no runtime behaviour by design, so not "silently ignored".
DOCUMENTATION_ONLY: dict[str, set[str]] = {
    "model": {"description"},
    "entity": {"description"},
    "dimension": {"description"},
    "column": {"description"},
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


# One in-memory DuckDB per project directory and process: file tables are loaded once (and
# reloaded when a file changes); the project's database file is attached as `store` and holds
# the writable tables (ADR-012). Each Project gets its own cursor on it, so `obi serve` sees a
# saved row on the next request without a restart.
_BASES: dict[str, dict] = {}
_BASES_LOCK = threading.Lock()


def _base(root: Path, conn: dict, database: Path | None) -> duckdb.DuckDBPyConnection:
    key = str(root.resolve())
    with _BASES_LOCK:
        state = _BASES.get(key)
        if state is None or state["database"] != database:
            if state is not None:
                state["con"].close()
            state = {"con": duckdb.connect(), "mtimes": {}, "database": database}
            if database is not None:
                database.parent.mkdir(parents=True, exist_ok=True)
                state["con"].execute(f"ATTACH '{database.as_posix()}' AS {STORE}")
            _BASES[key] = state
        con = state["con"]
        for table, rel in (conn.get("tables") or {}).items():
            path = root / rel
            mtime = path.stat().st_mtime if path.is_file() else None
            if state["mtimes"].get(table) == mtime:
                continue
            reader = "read_parquet" if str(rel).endswith(".parquet") else "read_csv_auto"
            # A table, not a view: a view would re-read the file on every query.
            con.execute(f"CREATE OR REPLACE TABLE memory.main.{_q(table)} AS SELECT * FROM "
                        f"{reader}('{path.as_posix().replace(chr(39), chr(39) * 2)}')")
            state["mtimes"][table] = mtime
        return con


def forget(root: str | Path):
    """Close the project's database (tests, `obi migrate --reseed` from a fresh state)."""
    with _BASES_LOCK:
        state = _BASES.pop(str(Path(root).resolve()), None)
        if state is not None:
            state["con"].close()


STORE = "store"


class Project:
    """A project directory: connection plus its models."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.config = parse((self.root / "project.yaml").read_text(encoding="utf-8")).data
        self.currency = self.config.get("currency", "USD")
        self.connection = self.config["connections"][self.config["default_connection"]]
        duck = self.connection.get("duckdb")
        db = duck.get("database") if isinstance(duck, dict) else None
        self.database: Path | None = (self.root / db) if db else None
        self.con = _base(self.root, self.connection, self.database).cursor()
        self.con.execute("USE memory.main")
        self._models: dict[str, Engine] = {}

    def describe(self) -> dict:
        """The live catalogue of the default connection: table -> column -> type."""
        conn = self.config["connections"][self.config["default_connection"]]
        tables = {}
        for table in conn.get("tables") or {}:
            rows = self.con.sql(f"DESCRIBE memory.main.{_q(table)}").fetchall()
            tables[table] = {r[0]: r[1] for r in rows}
        return {"connection": self.config["default_connection"], "tables": tables}

    def engine(self, model: str) -> "Engine":
        if model not in self._models:
            path = self.root / "model" / f"{model}.model.yaml"
            data = parse(path.read_text(encoding="utf-8")).data
            self._models[model] = Engine(self.con, ModelIndex(model, data),
                                         store=STORE if self.database is not None else None)
        return self._models[model]

    def models(self) -> list[str]:
        return sorted(p.name.removesuffix(".model.yaml") for p in (self.root / "model").glob("*.model.yaml"))


class Engine:
    def __init__(self, con: duckdb.DuckDBPyConnection, idx: ModelIndex, store: str | None = None):
        self.con, self.idx, self.store = con, idx, store

    def table(self, entity: str) -> str:
        """SQL name of an entity's table: writable tables live in the project's database file."""
        name = _q(self.idx.entities[entity]["table"])
        return f"{self.store}.main.{name}" if self.store and self.idx.is_writable(entity) else name

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
        lk = self.idx.lookup(ref.field)
        if lk:
            table, value, key, match = lk
            col = f"l.{_q(value)}"
            sql = (f"SELECT DISTINCT {col} AS v FROM {self.table(ref.field.entity)} AS t JOIN {_q(table)} AS l "
                   f"ON l.{_q(key)} = {_qualify(parse_colexpr(match), 't')} WHERE {col} IS NOT NULL "
                   f"ORDER BY 1 LIMIT {limit}")
            return [r[0] for r in self.con.sql(sql).fetchall()]
        col = f"t.{_q(self._column(ref.field))}"
        sql = (f"SELECT DISTINCT {col} AS v FROM {self.table(ref.field.entity)} AS t WHERE {col} IS NOT NULL "
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
        if not q.measures and not q.dimensions:
            raise EngineError("a query needs a measure or a dimension")
        dims = [self._dim(d) for d in q.dimensions]
        names = _output_names(dims)
        filters = [(self._dim(f.dimension), f) for f in q.filters]

        base: dict[Field, str] = {}  # base measure -> internal column alias
        outputs = []
        for m in q.measures:
            ref = self._resolve(m, ("measure",))
            outputs.append((ref.field.name, self._measure_expr(ref.field, base, ())))

        by_fact: dict[tuple, list[Field]] = {}
        for f in base:  # measures with their own date route get their own query (ADR-009)
            by_fact.setdefault((f.entity, self.idx.date_route(f)), []).append(f)
        if not by_fact:  # dimensions only: distinct rows of the first dimension's entity and its joins
            by_fact[(dims[0].field.entity, None)] = []
        ctes = [self._fact_cte(fact, ms, base, dims, names, filters, route)
                for (fact, route), ms in by_fact.items()]

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
                  names: list[str], filters: list, route: tuple[str, str] | None = None) -> str:
        aliases = {fact: "t0"}
        joins: list[str] = []
        lookups: dict[tuple[str, str], str] = {}

        def alias_of(entity: str, why: str) -> str:
            if entity in aliases:
                return aliases[entity]
            path = self.idx.path(fact, entity)
            if route and entity == route[1]:  # `date: <dimension>` on the measure
                via = self.idx.path(fact, route[0])
                path = None if via is None else via + self.idx.path(route[0], entity)
            if path is None:
                raise EngineError(f"{why} cannot be reached from entity `{fact}`; check the measure's grain")
            for src, dst, fk in path:
                if dst not in aliases:
                    aliases[dst] = f"t{len(aliases)}"
                    fks = fk if isinstance(fk, list) else [fk]
                    on = " AND ".join(
                        f"{_qualify(_checked_join(x, src, dst), aliases[src])} = {aliases[dst]}.{_q(k)}"
                        for x, k in zip(fks, self.idx.key_columns(dst)))
                    joins.append(f"LEFT JOIN {self.table(dst)} AS {aliases[dst]} ON {on}")
            return aliases[entity]

        def dim_sql(d: Ref, why: str) -> str:
            alias = alias_of(d.field.entity, why)
            lk = self.idx.lookup(d.field)
            if lk:  # lookup dimension: one join to the lookup table per dimension (ADR-009)
                table, value, key, match = lk
                k = (alias, d.field.name)
                if k not in lookups:
                    lookups[k] = f"lk{len(lookups)}"
                    on = _qualify(parse_colexpr(match), alias)
                    joins.append(f"LEFT JOIN {_q(table)} AS {lookups[k]} ON {lookups[k]}.{_q(key)} = {on}")
                return f"{lookups[k]}.{_q(value)}"
            col = f"{alias}.{_q(self._column(d.field))}"
            if d.grain in FISCAL_GRAINS:  # read from the calendar table, never computed (ADR-009)
                fiscal = d.field.spec["fiscal"]
                year = f"CAST({alias}.{_q(fiscal['year'])} AS VARCHAR)"
                if d.grain == "fiscal_year":
                    return f"'FY' || {year}"
                return f"'FY' || {year} || ' P' || lpad(CAST({alias}.{_q(fiscal['period'])} AS VARCHAR), 2, '0')"
            if d.grain and d.grain != "day":
                return f"CAST(date_trunc('{d.grain}', {col}) AS DATE)"
            return col

        select = []
        for name, d in zip(names, dims):
            select.append(f"{dim_sql(d, f'dimension `{d.text}`')} AS {_q(name)}")
        rate = self._rate_join(fact, measures, dim_sql, joins)
        rollup_sql = self._rollup_joins(fact, measures, joins)
        for m in measures:
            kind = next(k for k in MEASURE_KINDS if k in m.spec)
            expr = _qualify(_checked(m.spec[kind], m, kind), "t0", rollup_sql)
            if self.idx.converts(m):
                expr = f"({expr}) * {rate}"
            agg = AGG_SQL[kind].format(expr)
            if m.spec.get("where"):
                agg += f" FILTER (WHERE {self._where_sql(m, dim_sql)})"
            if kind in DOUBLE_AGGS:
                agg = f"CAST({agg} AS DOUBLE)"
            select.append(f"{agg} AS {_q(base[m])}")

        where = []
        for d, f in filters:
            col = dim_sql(Ref(d.text, field=d.field), f"filter on `{f.dimension}`")
            if f.range:
                where.append(f"{col} BETWEEN {_lit(f.range[0])} AND {_lit(f.range[1])}")
            elif f.values is not None:
                vals = ", ".join(_lit(v) for v in f.values)
                where.append(f"{col} IN ({vals})" if f.values else "FALSE")

        sql = f"SELECT {', '.join(select)} FROM {self.table(fact)} AS t0"
        if joins:
            sql += " " + " ".join(joins)
        if where:
            sql += " WHERE " + " AND ".join(where)
        if dims:
            sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims)))
        return sql

    def _rate_join(self, fact: str, measures: list[Field], dim_sql, joins: list[str]) -> str | None:
        """One join to the rate table per fact query, on the row's currency and month (ADR-009)."""
        if not any(self.idx.converts(m) for m in measures):
            return None
        cur = self.idx.entities[fact].get("currency") or {}
        rates = self.idx.currency.get("rates") or {}
        code = dim_sql(self._resolve(cur["code"], ("dimension",)), f"currency code `{cur['code']}`")
        day = dim_sql(self._resolve(cur["date"], ("dimension",)), f"currency date `{cur['date']}`")
        joins.append(f"LEFT JOIN {_q(rates['table'])} AS fx ON fx.{_q(rates['currency'])} = {code} "
                     f"AND fx.{_q(rates['month'])} = CAST(date_trunc('month', {day}) AS DATE)")
        return f"fx.{_q(rates['rate'])}"

    def check_rates(self) -> list[tuple[str, str]]:
        """Rows whose currency and month have no rate: converted sums would silently miss them.
        Grouped by currency and day, a converted total is empty exactly when its rate is missing."""
        problems = []
        for en, e in self.idx.entities.items():
            converted = [f for f in self.idx.fields(en) if self.idx.converts(f)]
            cur = e.get("currency")
            if not converted or not isinstance(cur, dict):
                continue
            m = converted[0]
            code, day = (self._resolve(cur[k], ("dimension",)).field.name for k in ("code", "date"))
            inner = self.sql(SemanticQuery([m.qualified], [cur["code"], cur["date"]]))
            check = (f"SELECT {_q(code)}, CAST(date_trunc('month', {_q(day)}) AS DATE) FROM ({inner}) "
                     f"WHERE {_q(m.name)} IS NULL GROUP BY 1, 2 ORDER BY 1, 2")
            try:
                rows = self.con.sql(check).fetchall()
            except duckdb.Error as err:
                problems.append((en, translate_db_error(err)))
                continue
            if rows:
                months = ", ".join(f"{c} {mo:%Y-%m}" for c, mo in rows[:5])
                more = f" and {len(rows) - 5} more" if len(rows) > 5 else ""
                problems.append((en, f"no exchange rate for {months}{more}; converted amounts would leave "
                                     f"those rows out"))
        return problems

    def fiscal_range(self, dimension: str, preset: str, as_of: date) -> tuple[date, date]:
        """A fiscal preset as a date range, read from the calendar table (ADR-009)."""
        d, alias_sql = self._fiscal_dim(dimension)
        y, pcol, col = alias_sql
        row = self.con.sql(f"SELECT {y}, {pcol} FROM {d} WHERE {col} = {_lit(as_of)}").fetchall()
        if not row:
            raise EngineError(f"the calendar has no row for {as_of.isoformat()}")
        fy, fp = row[0]
        where = {"this_fiscal_year": f"{y} = {fy}", "last_fiscal_year": f"{y} = {fy - 1}",
                 "fiscal_year_to_date": f"{y} = {fy} AND {col} <= {_lit(as_of)}",
                 "this_fiscal_period": f"{y} = {fy} AND {pcol} = {fp}"}.get(preset)
        if preset == "last_fiscal_period":
            prev = self.con.sql(f"SELECT {y}, {pcol} FROM {d} WHERE ({y} * 100 + {pcol}) < {fy * 100 + fp} "
                                f"ORDER BY {y} DESC, {pcol} DESC LIMIT 1").fetchall()
            if not prev:
                raise EngineError("the calendar has no earlier fiscal period")
            where = f"{y} = {prev[0][0]} AND {pcol} = {prev[0][1]}"
        if where is None:
            raise EngineError(f"`{preset}` is not a fiscal preset")
        lo, hi = self.con.sql(f"SELECT MIN({col}), MAX({col}) FROM {d} WHERE {where}").fetchall()[0]
        return lo, hi

    def fiscal_shift(self, dimension: str, r: tuple[date, date], periods: int) -> tuple[date, date]:
        """Move a range by whole fiscal periods using the calendar table: each end keeps its
        offset inside its period. `periods=-12` is the same span one fiscal year earlier when
        the year has twelve periods; the calendar decides, nothing is computed from dates."""
        d, (y, pcol, col) = self._fiscal_dim(dimension)
        periods_sql = (f"SELECT {y} AS y, {pcol} AS p, MIN({col}) AS s, MAX({col}) AS e, "
                       f"ROW_NUMBER() OVER (ORDER BY {y}, {pcol}) AS i FROM {d} GROUP BY 1, 2")
        out = []
        for day in r:
            row = self.con.sql(f"WITH ps AS ({periods_sql}) SELECT ps.i, {_lit(day)} - ps.s FROM ps "
                               f"WHERE {_lit(day)} BETWEEN ps.s AND ps.e").fetchall()
            if not row:
                raise EngineError(f"the calendar has no fiscal period for {day.isoformat()}")
            i, offset = row[0]
            target = self.con.sql(f"WITH ps AS ({periods_sql}) SELECT s, e FROM ps WHERE i = {i + periods}").fetchall()
            if not target:
                raise EngineError("the calendar does not reach back far enough for this comparison")
            s, e = target[0]
            out.append(min(s + timedelta(days=offset), e))
        return out[0], out[1]

    def fiscal_periods_in(self, dimension: str, r: tuple[date, date]) -> int:
        d, (y, pcol, col) = self._fiscal_dim(dimension)
        row = self.con.sql(f"SELECT COUNT(DISTINCT ({y}, {pcol})) FROM {d} WHERE {col} BETWEEN "
                           f"{_lit(r[0])} AND {_lit(r[1])}").fetchall()
        return int(row[0][0] or 1)

    def fiscal_shift_years(self, dimension: str, r: tuple[date, date], years: int) -> tuple[date, date]:
        """The same fiscal periods `years` fiscal years away, read from the calendar table: each end
        keeps its fiscal period and its offset inside that period. Works for any fiscal calendar
        (4-4-5, 53-week years) and for a calendar table that ends mid-year."""
        d, (y, pcol, col) = self._fiscal_dim(dimension)
        out = []
        for day in r:
            row = self.con.sql(f"SELECT {y}, {pcol}, {_lit(day)} - MIN({col}) OVER (PARTITION BY {y}, {pcol}) "
                               f"FROM {d} QUALIFY {col} = {_lit(day)}").fetchall()
            if not row:
                raise EngineError(f"the calendar has no fiscal period for {day.isoformat()}")
            fy, fp, offset = row[0]
            target = self.con.sql(f"SELECT MIN({col}), MAX({col}) FROM {d} WHERE {y} = {fy + years} "
                                  f"AND {pcol} = {fp}").fetchall()[0]
            if target[0] is None:
                raise EngineError(f"the calendar has no fiscal year {fy + years}, period {fp}")
            out.append(min(target[0] + timedelta(days=offset), target[1]))
        return out[0], out[1]

    def _fiscal_dim(self, dimension: str):
        r = self._resolve(dimension.split(".")[0] if dimension.count(".") == 1 and
                          dimension.split(".")[1] in FISCAL_GRAINS else dimension, ("dimension",))
        fiscal = r.field.spec.get("fiscal")
        if not isinstance(fiscal, dict):
            raise EngineError(f"dimension `{r.field.name}` has no fiscal calendar")
        table = self.table(r.field.entity)
        return table, (_q(fiscal["year"]), _q(fiscal["period"]), _q(self._column(r.field)))

    def _rollup_joins(self, fact: str, measures: list[Field], joins: list[str]) -> dict[str, str]:
        """Rollups used by these measures: each child entity's rows added up per row of the fact,
        joined on the child's foreign key. Returns rollup name -> SQL for the value (ADR-009)."""
        out: dict[str, str] = {}
        rollups = self.idx.rollups(fact)
        used = {r for m in measures for k in MEASURE_KINDS if isinstance(m.spec.get(k), str)
                for r, _ in references(parse_colexpr(m.spec[k])) if r in rollups}
        for i, name in enumerate(sorted(used)):
            spec = rollups[name]
            kind = self.idx.rollup_kind(spec)
            child = spec["from"]
            fk = self.idx.joins(child)[fact]
            fks = fk if isinstance(fk, list) else [fk]
            keys = self.idx.key_columns(fact)
            body = _qualify(parse_colexpr(spec[kind]), "c")
            cols = ", ".join(f"{_qualify(parse_colexpr(x), 'c')} AS k{j}" for j, x in enumerate(fks))
            alias = f"ru{i}"
            joins.append(f"LEFT JOIN (SELECT {cols}, {AGG_SQL[kind].format(body)} AS v "
                         f"FROM {self.table(child)} AS c GROUP BY ALL) AS {alias} ON "
                         + " AND ".join(f"{alias}.k{j} = t0.{_q(k)}" for j, k in enumerate(keys)))
            out[name] = f"COALESCE({alias}.v, 0)" if kind in ("sum", "count") else f"{alias}.v"
        return out

    def check_lookups(self) -> list[tuple[Field, str]]:
        """A lookup key that is not unique would multiply rows (SAP text tables are keyed by
        code and language, for example)."""
        problems = []
        seen = set()
        for f in self.idx.fields():
            lk = self.idx.lookup(f)
            if not lk or lk[:3:2] in seen:
                continue
            seen.add(lk[:3:2])
            table, _, key, _ = lk
            rows = self.con.sql(f"SELECT {_q(key)}, COUNT(*) FROM {_q(table)} GROUP BY 1 HAVING COUNT(*) > 1 "
                                f"ORDER BY 1 LIMIT 3").fetchall()
            if rows:
                examples = ", ".join(f"`{k}` ({n} rows)" for k, n in rows)
                problems.append((f, f"lookup key `{key}` is not unique in table `{table}`: {examples}; "
                                    f"a lookup must find at most one row"))
        return problems

    def _where_sql(self, m: Field, dim_sql) -> str:
        """`where:` names model dimensions (ADR-008): each is resolved and joined like a
        chart axis, so a filtered measure can use attributes of joined entities."""
        tree = _checked(m.spec["where"], m, "where")
        for text, col in references(tree):
            r = self._resolve(text, ("dimension",))
            sql = dim_sql(r, f"dimension `{text}` in the `where` of measure `{m.name}`")
            col.replace(sqlglot.parse_one(sql, read="duckdb"))
        return tree.sql("duckdb")


def _checked_join(text, src: str, dst: str) -> exp.Expression:
    try:
        return parse_colexpr(str(text))
    except ColExprError as e:
        raise EngineError(f"join from `{src}` to `{dst}`: {e.message}") from None


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


def _qualify(tree: exp.Expression, alias: str, replace: dict[str, str] | None = None) -> str:
    """Prefix bare column names with `alias`; names in `replace` (rollups) become that SQL."""
    if replace and isinstance(tree, exp.Column) and not tree.table and tree.name in replace:
        return replace[tree.name]  # the whole body is one rollup
    for col in list(tree.find_all(exp.Column)):
        if replace and not col.table and col.name in replace:
            col.replace(sqlglot.parse_one(replace[col.name], read="duckdb"))
            continue
        if not col.table:
            col.set("table", exp.to_identifier(alias))
    return tree.sql("duckdb")


def _output_names(dims: list[Ref]) -> list[str]:
    names = []
    for d in dims:
        n = d.field.name
        names.append(n if n not in names else f"{d.field.entity}_{n}")
    return names


_CODES = {"usd", "eur", "gbp", "jpy", "chf", "cny", "aed", "inr", "cad", "aud"}


def humanize(name: str) -> str:
    """Default label from an id (ADR-002 §5): `net_revenue` -> "Net revenue",
    `order_value_usd` -> "Order value USD", `margin_pct` -> "Margin %"."""
    words = ["%" if w == "pct" else w.upper() if w in _CODES else w for w in name.split("_")]
    s = " ".join(words)
    return s[:1].upper() + s[1:]
