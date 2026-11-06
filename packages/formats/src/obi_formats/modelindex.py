"""Semantic index of a model: name resolution, join paths and grain (ADR-001, ADR-005)."""

from __future__ import annotations

import difflib
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Callable

from .colexpr import ColExprError, parse as parse_colexpr, references

CALENDAR_GRAINS = ("day", "week", "month", "quarter", "year")
FISCAL_GRAINS = ("fiscal_period", "fiscal_year")  # read from the calendar entity (ADR-009)
TIME_GRAINS = CALENDAR_GRAINS + FISCAL_GRAINS
_GRAIN_RANK = {None: 0, "day": 1, "week": 2, "month": 3, "fiscal_period": 3, "quarter": 4, "year": 5,
               "fiscal_year": 5}
MEASURE_KINDS = ("sum", "count", "count_distinct", "avg", "min", "max", "calc")
ROLLUP_KINDS = ("sum", "count", "min", "max")

Report = Callable[[tuple, "str | None", str], None]


@dataclass(frozen=True)
class Field:
    entity: str
    name: str
    kind: str  # "measure" | "dimension"
    spec: dict = dc_field(compare=False, hash=False, repr=False)

    @property
    def qualified(self) -> str:
        return f"{self.entity}.{self.name}"

    @property
    def dtype(self) -> str:
        return self.spec.get("type", "string") if self.kind == "dimension" else "number"


@dataclass(frozen=True)
class Ref:
    text: str
    field: Field | None = None
    hierarchy: str | None = None
    grain: str | None = None

    @property
    def kind(self) -> str:
        return self.field.kind if self.field else "hierarchy"

    @property
    def name(self) -> str:
        return self.field.name if self.field else self.hierarchy


class RefError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


Grain = dict  # (entity, dimension) -> coarsest time grain, None = full detail


def did_you_mean(word: str, options) -> str:
    match = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return f"; did you mean `{match[0]}`?" if match else ""


def _what(want: tuple[str, ...]) -> str:
    if want == ("measure",):
        return "measure"
    if set(want) <= {"dimension", "hierarchy"}:
        return "dimension"
    return "field"


class ModelIndex:
    def __init__(self, name: str, data: dict):
        self.name = name
        self.title = data.get("title") if isinstance(data.get("title"), str) else None
        cur = data.get("currency")
        self.currency: dict = cur if isinstance(cur, dict) else {}
        ents = data.get("entities")
        self.entities: dict[str, dict] = {k: v for k, v in (ents or {}).items() if isinstance(v, dict)} \
            if isinstance(ents, dict) else {}
        hier = data.get("hierarchies")
        self.hierarchies: dict[str, list] = {k: v for k, v in (hier or {}).items() if isinstance(v, list)} \
            if isinstance(hier, dict) else {}
        self.by_name: dict[str, list[Field]] = defaultdict(list)
        for en, e in self.entities.items():
            for kind, key in (("dimension", "dimensions"), ("measure", "measures")):
                block = e.get(key)
                if isinstance(block, dict):
                    for fname, spec in block.items():
                        self.by_name[fname].append(Field(en, fname, kind, spec if isinstance(spec, dict) else {}))
        self._grain_cache: dict[Field, Grain] = {}

    # ------------------------------------------------------------------ references

    def fields(self, entity: str | None = None):
        for lst in self.by_name.values():
            for f in lst:
                if entity is None or f.entity == entity:
                    yield f

    def resolve(self, text: str, want: tuple[str, ...] = ("measure", "dimension", "hierarchy")) -> Ref:
        parts = text.split(".")
        entity = grain = None
        if len(parts) == 3:
            entity, name, grain = parts
        elif len(parts) == 2:
            a, b = parts
            if a in self.entities and any(f.name == b for f in self.fields(a)):
                entity, name = a, b
            elif b in TIME_GRAINS:
                name, grain = a, b
            elif a in self.entities:
                raise RefError(f"entity `{a}` has no field `{b}`"
                               + did_you_mean(b, {f.name for f in self.fields(a)}))
            elif a in self.by_name:
                raise RefError(f"`{b}` in `{text}` is not a time grain; use one of {', '.join(TIME_GRAINS)}"
                               + did_you_mean(b, TIME_GRAINS))
            else:
                raise RefError(f"`{a}` in `{text}` is not an entity or field of model `{self.name}`"
                               + did_you_mean(a, list(self.entities) + list(self.by_name)))
        else:
            name = parts[0]

        if entity is not None:
            if entity not in self.entities:
                raise RefError(f"`{entity}` in `{text}` is not an entity of model `{self.name}`"
                               + did_you_mean(entity, self.entities))
            cands = [f for f in self.by_name.get(name, []) if f.entity == entity]
            if not cands:
                raise RefError(f"entity `{entity}` has no field `{name}`"
                               + did_you_mean(name, {f.name for f in self.fields(entity)}))
        else:
            cands = list(self.by_name.get(name, []))
            if name in self.hierarchies:
                cands.append(name)

        def kind(c):
            return "hierarchy" if isinstance(c, str) else c.kind

        ok = [c for c in cands if kind(c) in want]
        if not ok:
            if cands:
                raise RefError(f"`{text}` is a {kind(cands[0])}, but a {_what(want)} is required here")
            pool = {f.name for f in self.fields() if f.kind in want}
            if "hierarchy" in want:
                pool |= set(self.hierarchies)
            raise RefError(f"{_what(want)} `{text}` not found in model `{self.name}`" + did_you_mean(name, pool))
        if len(ok) > 1:
            names = ", ".join(f"`{c.qualified}`" if isinstance(c, Field) else f"`{c}` (hierarchy)" for c in ok)
            raise RefError(f"`{name}` is ambiguous in model `{self.name}`; write one of {names}")
        c = ok[0]
        if grain is not None:
            if grain not in TIME_GRAINS:
                raise RefError(f"`{grain}` in `{text}` is not a time grain; use one of {', '.join(TIME_GRAINS)}")
            if not (isinstance(c, Field) and c.kind == "dimension" and c.dtype == "date"):
                what = f"{c.dtype} {c.kind}" if isinstance(c, Field) else "hierarchy"
                raise RefError(f"`{text}`: a time grain needs a date dimension; `{name}` is a {what}")
            if grain in FISCAL_GRAINS and not isinstance(c.spec.get("fiscal"), dict):
                raise RefError(f"`{text}`: dimension `{name}` has no fiscal calendar; add "
                               f"`fiscal: {{ year: <column>, period: <column> }}` to it")
        if isinstance(c, str):
            return Ref(text, hierarchy=c)
        return Ref(text, field=c, grain=grain)

    def levels(self, ref: Ref) -> list[Ref]:
        """Dimension refs behind a ref: the ref itself, or a hierarchy's levels."""
        if ref.field:
            return [ref]
        out = []
        for level in self.hierarchies.get(ref.hierarchy, []):
            try:
                out.append(self.resolve(level, ("dimension",)))
            except RefError:
                pass
        return out

    # ------------------------------------------------------------------ joins and grain

    def joins(self, entity: str) -> dict[str, str]:
        """Usable joins: single-column foreign key to an entity with a single-column key.
        Composite-key joins are not supported yet (ADR-008) and stay out of the join graph."""
        j = self.entities.get(entity, {}).get("joins")
        if not isinstance(j, dict):
            return {}
        return {t: c for t, c in j.items() if t in self.entities and self._join_ok(t, c)}

    def key_columns(self, entity: str) -> list[str]:
        key = self.entities[entity].get("key")
        return [str(k) for k in key] if isinstance(key, list) else [str(key)]

    def _join_ok(self, target: str, fk) -> bool:
        """A join's foreign key: one column or expression per column of the target's key."""
        fks = fk if isinstance(fk, list) else [fk]
        return all(isinstance(x, str) for x in fks) and len(fks) == len(self.key_columns(target))

    def path(self, start: str, target: str) -> list[tuple[str, str, object]] | None:
        """The shortest join path (ADR-009 amends ADR-001: a shorter path wins; only paths of
        equal length are ambiguous), as (source, target, foreign key) steps."""
        todo, seen = [(start, [])], {start}
        while todo:
            ent, steps = todo.pop(0)
            if ent == target:
                return steps
            for dst, fk in self.joins(ent).items():
                if dst not in seen:
                    seen.add(dst)
                    todo.append((dst, steps + [(ent, dst, fk)]))
        return None

    def reachable(self, entity: str) -> list[str]:
        seen, todo = [entity], [entity]
        while todo:
            for t in self.joins(todo.pop(0)):
                if t not in seen:
                    seen.append(t)
                    todo.append(t)
        return seen

    def entity_grain(self, entity: str) -> Grain:
        e = self.entities.get(entity, {})
        if isinstance(e.get("grain"), list):
            return self._grain_from(e["grain"])
        return {(x, f.name): None for x in self.reachable(entity) for f in self.fields(x)
                if f.kind == "dimension"}

    def measure_grain(self, f: Field, _stack: tuple = ()) -> Grain:
        if f in self._grain_cache:
            return self._grain_cache[f]
        if isinstance(f.spec.get("grain"), list):
            g = self._grain_from(f.spec["grain"])
        elif "calc" in f.spec and f not in _stack:
            g = None
            for r in self.calc_refs(f):
                try:
                    m = self.resolve(r, ("measure",)).field
                except RefError:
                    continue
                mg = self.measure_grain(m, _stack + (f,))
                g = mg if g is None else _intersect(g, mg)
            g = g if g is not None else self.entity_grain(f.entity)
        else:
            g = self.entity_grain(f.entity)
        self._grain_cache[f] = g
        return g

    def _grain_from(self, refs: list) -> Grain:
        g: Grain = {}
        for text in refs:
            try:
                r = self.resolve(str(text), ("dimension",))
            except RefError:
                continue
            g[(r.field.entity, r.field.name)] = r.grain
        return g

    def grain_problem(self, g: Grain, dim: Ref) -> str | None:
        """None if the dimension can slice a measure of grain g, else 'outside' or 'finer'."""
        key = (dim.field.entity, dim.field.name)
        if key not in g:
            return "outside"
        if g[key] and _GRAIN_RANK[dim.grain or "day"] < _GRAIN_RANK[g[key]]:
            return "finer"
        return None

    def grain_text(self, g: Grain) -> str:
        parts = []
        for (entity, name), tg in g.items():
            label = name if len([f for f in self.by_name[name]]) == 1 else f"{entity}.{name}"
            parts.append(f"{label}.{tg}" if tg else label)
        return ", ".join(parts) or "none"

    def calc_refs(self, f: Field) -> list[str]:
        return self._refs(f.spec.get("calc"))

    def where_refs(self, f: Field) -> list[str]:
        return self._refs(f.spec.get("where"))

    @staticmethod
    def _refs(text) -> list[str]:
        if not isinstance(text, str):
            return []
        try:
            return [r for r, _ in references(parse_colexpr(text))]
        except ColExprError:
            return []

    # ------------------------------------------------------------------ checks

    def check(self, report: Report):
        for f in self.fields():
            if f.kind == "measure":
                self._check_measure(f, report)
        for en, e in self.entities.items():
            self._check_joins(en, e, report)
            if isinstance(e.get("grain"), list):
                self._check_grain_list(en, e["grain"], ("entities", en, "grain"), f"entity `{en}`", report)
        self._check_paths(report)
        self._check_currency(report)
        self._check_lookups_rollups_dates(report)
        for h, levels in self.hierarchies.items():
            for i, level in enumerate(levels):
                try:
                    self.resolve(str(level), ("dimension",))
                except RefError as e:
                    report(("hierarchies", h, i), f"hierarchy `{h}`", e.message)
        for name, lst in self.by_name.items():
            if name in self.hierarchies:
                report(("hierarchies", name), f"hierarchy `{name}`",
                       f"`{name}` is also the name of {lst[0].kind} `{lst[0].qualified}`; rename the hierarchy")

    def _check_measure(self, f: Field, report: Report):
        path = ("entities", f.entity, "measures", f.name)
        label = f"measure `{f.name}`"
        kinds = [k for k in MEASURE_KINDS if k in f.spec]
        if not kinds:
            report(path, label, f"needs exactly one of {', '.join(MEASURE_KINDS)}")
            return
        if len(kinds) > 1:
            report(path + (kinds[1],), label,
                   f"has both `{kinds[0]}` and `{kinds[1]}`; keep exactly one")
        for key in (kinds[0], "where"):
            if not isinstance(f.spec.get(key), str):
                continue
            try:
                tree = parse_colexpr(f.spec[key])
            except ColExprError as e:
                report(path + (key,), label, f"`{key}`: {e.message}")
                return
            if key == "where":
                self._check_where(f, tree, path, label, report)
            elif key != "calc":
                for text, _ in references(tree):
                    if "." in text:
                        report(path + (key,), label,
                               f"`{key}` uses `{text}`: aggregations read columns of the entity's own table "
                               f"only; to filter on a joined dimension use `where:`")
        if kinds[0] == "calc":
            for r in self.calc_refs(f):
                try:
                    self.resolve(r, ("measure",))
                except RefError as e:
                    report(path + ("calc",), label, e.message)
            cycle = self._calc_cycle(f, (f,))
            if cycle:
                report(path + ("calc",), label,
                       "calc refers to itself: " + " → ".join(c.name for c in cycle))
        if isinstance(f.spec.get("grain"), list):
            self._check_grain_list(f.entity, f.spec["grain"], path + ("grain",), label, report)

    def _check_where(self, f: Field, tree, path: tuple, label: str, report: Report):
        """`where:` names model dimensions, resolved through joins like chart axes (ADR-008)."""
        reach = self.reachable(f.entity)
        for text, _ in references(tree):
            try:
                r = self.resolve(text, ("dimension",))
            except RefError as e:
                hint = self._join_column_hint(f.entity, text)
                msg = f"`{text}` is not a model dimension; {hint}" if hint else e.message
                report(path + ("where",), label, f"`where`: {msg}")
                continue
            if r.field.entity not in reach:
                report(path + ("where",), label,
                       f"`where`: dimension `{text}` is not reachable from entity `{f.entity}` through its joins")

    def _join_column_hint(self, entity: str, text: str) -> str | None:
        """`order_date` in `where:` usually means the dimension the join column leads to."""
        joins = self.entities.get(entity, {}).get("joins")
        if not isinstance(joins, dict):
            return None
        for target, fk in joins.items():
            if fk == text and target in self.entities:
                key = self.entities[target].get("key")
                for f in self.fields(target):
                    if f.kind == "dimension" and f.spec.get("column", f.name) == key:
                        return f"`{text}` is the join column to `{target}`, so use its dimension `{f.name}`"
                return f"`{text}` is the join column to `{target}`; use a dimension of `{target}`"
        return None

    def _calc_cycle(self, f: Field, stack: tuple):
        for r in self.calc_refs(f):
            try:
                m = self.resolve(r, ("measure",)).field
            except RefError:
                continue
            if m == stack[0]:
                return stack + (m,)
            if m not in stack and "calc" in m.spec:
                found = self._calc_cycle(m, stack + (m,))
                if found:
                    return found
        return None

    def _check_grain_list(self, entity: str, refs: list, path: tuple, label: str, report: Report):
        reach = self.reachable(entity)
        for i, text in enumerate(refs):
            try:
                r = self.resolve(str(text), ("dimension",))
            except RefError as e:
                report(path + (i,), label, f"grain: {e.message}")
                continue
            if r.field.entity not in reach:
                report(path + (i,), label,
                       f"grain: dimension `{text}` is not reachable from entity `{entity}` through its joins")

    def _check_joins(self, en: str, e: dict, report: Report):
        joins = e.get("joins")
        if not isinstance(joins, dict):
            return
        for target, fk in joins.items():
            path = ("entities", en, "joins", target)
            if target not in self.entities:
                report(path, f"entity `{en}`",
                       f"joins unknown entity `{target}`" + did_you_mean(target, self.entities))
                continue
            fks = fk if isinstance(fk, list) else [fk]
            keys = self.key_columns(target)
            if len(fks) != len(keys):
                hint = (f"write `{target}: [{', '.join('<column for ' + k + '>' for k in keys)}]`"
                        if len(keys) > 1 else f"write `{target}: <column>`")
                report(path, f"entity `{en}`",
                       f"the key of `{target}` has {len(keys)} column{'s' if len(keys) > 1 else ''} "
                       f"({', '.join(keys)}), but the join gives {len(fks)}; {hint}")
                continue
            for x in fks:
                try:
                    tree = parse_colexpr(str(x))
                except ColExprError as e:
                    report(path, f"entity `{en}`", f"join to `{target}`: {e.message}")
                    continue
                for text, _ in references(tree):
                    if "." in text:
                        report(path, f"entity `{en}`",
                               f"join to `{target}` uses `{text}`: a join reads columns of the entity's "
                               f"own table only")

    def _check_lookups_rollups_dates(self, report: Report):
        for f in self.fields():
            if f.kind == "dimension" and "lookup" in f.spec:
                path = ("entities", f.entity, "dimensions", f.name)
                label = f"dimension `{f.name}`"
                if "column" in f.spec:
                    report(path, label, "has both `column` and `lookup`; a lookup dimension takes its value "
                                        "from the lookup table")
                if not isinstance(f.spec["lookup"], str) or "." not in f.spec["lookup"]:
                    report(path + ("lookup",), label, "`lookup` must be `<table>.<value column>`, e.g. `t179t.VTEXT`")
                for part in ("key", "match"):
                    if not isinstance(f.spec.get(part), str):
                        report(path, label, f"a lookup needs `{part}`: `key` is the lookup table's key column, "
                                            f"`match` the expression on this entity's columns that equals it")
                if isinstance(f.spec.get("match"), str):
                    self._own_columns_only(f.spec["match"], path + ("match",), label, "`match`", report)
            if f.kind == "measure" and "date" in f.spec:
                path = ("entities", f.entity, "measures", f.name, "date")
                label = f"measure `{f.name}`"
                try:
                    d = self.resolve(str(f.spec["date"]), ("dimension",))
                except RefError as e:
                    report(path, label, f"`date`: {e.message}")
                    continue
                if d.field.dtype != "date":
                    report(path, label, f"`date`: `{d.text}` must be a date dimension, not {d.field.dtype}")
                elif d.field.entity not in self.reachable(f.entity):
                    report(path, label, f"`date`: dimension `{d.text}` is not reachable from entity `{f.entity}`")
                elif self.date_route(f) is None:
                    report(path, label, f"`date: {d.text}` needs entity `{d.field.entity}` to join a calendar on "
                                        f"the column of `{d.text}`")
        for en in self.entities:
            for name, spec in self.rollups(en).items():
                path = ("entities", en, "rollups", name)
                label = f"rollup `{name}`"
                kind = self.rollup_kind(spec)
                if kind is None:
                    report(path, label, f"needs exactly one of {', '.join(ROLLUP_KINDS)}")
                    continue
                child = spec.get("from")
                if child not in self.entities:
                    report(path + ("from",), label, f"`from`: unknown entity `{child}`" + did_you_mean(str(child), self.entities))
                    continue
                if en not in self.joins(child):
                    report(path + ("from",), label, f"`from: {child}`: `{child}` must join `{en}` (a rollup "
                                                    f"adds up the rows of `{child}` that point to each `{en}` row)")
                    continue
                if isinstance(spec.get(kind), str):
                    self._own_columns_only(spec[kind], path + (kind,), label, f"`{kind}`", report)

    def _own_columns_only(self, text: str, path: tuple, label: str, what: str, report: Report):
        try:
            tree = parse_colexpr(text)
        except ColExprError as e:
            report(path, label, f"{what}: {e.message}")
            return
        for ref, _ in references(tree):
            if "." in ref:
                report(path, label, f"{what} uses `{ref}`: it reads columns of one table only, without prefix")

    # ------------------------------------------------------------------ currency (ADR-009)

    # ------------------------------------------------------------------ lookups, rollups, date routes

    @staticmethod
    def lookup(f: Field) -> tuple[str, str, str, str] | None:
        """(table, value column, key column, match expression) of a lookup dimension."""
        spec = f.spec
        if f.kind != "dimension" or not isinstance(spec.get("lookup"), str) or "." not in spec["lookup"]:
            return None
        table, value = spec["lookup"].split(".", 1)
        return table, value, str(spec.get("key")), str(spec.get("match"))

    def rollups(self, entity: str) -> dict[str, dict]:
        r = self.entities.get(entity, {}).get("rollups")
        return {k: v for k, v in r.items() if isinstance(v, dict)} if isinstance(r, dict) else {}

    def rollup_kind(self, spec: dict) -> str | None:
        return next((k for k in ROLLUP_KINDS if k in spec), None)

    def date_route(self, f: Field) -> tuple[str, str] | None:
        """For a measure with `date: <dimension>`: (entity owning that dimension, entity it joins
        on that dimension's column). The measure reaches the latter through the former."""
        text = f.spec.get("date") if f.kind == "measure" else None
        if not isinstance(text, str):
            return None
        try:
            d = self.resolve(text, ("dimension",)).field
        except RefError:
            return None
        col = d.spec.get("column", d.name)
        for target, fk in self.joins(d.entity).items():
            if fk == col:
                return d.entity, target
        return None

    def converts(self, f: Field) -> bool:
        return f.kind == "measure" and isinstance(f.spec.get("convert"), str)

    def per_currency(self, f: Field, _stack: tuple = ()) -> bool:
        """A money measure still in document currency: it must not be added up across currencies."""
        if f.kind != "measure" or f in _stack:
            return False
        if "calc" in f.spec:
            # A calc is in document currency when one of its parts is and none is converted.
            # A calc that mixes both is a model error (reported once, in the model).
            parts = []
            for r in self.calc_refs(f):
                try:
                    parts.append(self.resolve(r, ("measure",)).field)
                except RefError:
                    pass
            local = any(self.per_currency(m, _stack + (f,)) for m in parts)
            converted = any(self.converts(m) or self._converted_calc(m, _stack + (f,)) for m in parts)
            return local and not converted
        e = self.entities.get(f.entity, {})
        e = self.entities.get(f.entity, {})
        return (isinstance(e.get("currency"), dict) and f.spec.get("format") == "currency"
                and not self.converts(f))

    def _converted_calc(self, f: Field, stack: tuple) -> bool:
        if "calc" not in f.spec or f in stack:
            return False
        for r in self.calc_refs(f):
            try:
                m = self.resolve(r, ("measure",)).field
            except RefError:
                continue
            if self.converts(m) or self._converted_calc(m, stack + (f,)):
                return True
        return False

    def converted_counterpart(self, f: Field) -> Field | None:
        """For an amount in document currency, the same aggregation with `convert:`."""
        kind = next((k for k in MEASURE_KINDS if k in f.spec), None)
        for g in self.fields(f.entity):
            if g is not f and self.converts(g) and kind and g.spec.get(kind) == f.spec.get(kind)                     and g.spec.get("where") == f.spec.get("where"):
                return g
        return None

    def currency_code_dimension(self, f: Field) -> str | None:
        cur = self.entities.get(f.entity, {}).get("currency")
        return cur.get("code") if isinstance(cur, dict) else None

    def _check_currency(self, report: Report):
        target = self.currency.get("target")
        for en, e in self.entities.items():
            cur = e.get("currency")
            if not isinstance(cur, dict):
                continue
            path = ("entities", en, "currency")
            if not self.currency:
                report(path, f"entity `{en}`", "has a `currency` block, but the model has no top-level "
                       "`currency: { target, rates }` to convert with")
            reach = self.reachable(en)
            for key, dtype in (("code", None), ("date", "date")):
                text = cur.get(key)
                if not isinstance(text, str):
                    continue
                try:
                    r = self.resolve(text, ("dimension",))
                except RefError as err:
                    report(path + (key,), f"entity `{en}`", f"`currency.{key}`: {err.message}")
                    continue
                if r.field.entity not in reach:
                    report(path + (key,), f"entity `{en}`", f"`currency.{key}`: dimension `{text}` is not "
                           f"reachable from entity `{en}` through its joins")
                elif dtype and r.field.dtype != dtype:
                    report(path + (key,), f"entity `{en}`", f"`currency.{key}`: `{text}` must be a date "
                           f"dimension, not {r.field.dtype}")
        for f in self.fields():
            if f.kind != "measure":
                continue
            path = ("entities", f.entity, "measures", f.name)
            label = f"measure `{f.name}`"
            conv = f.spec.get("convert")
            if conv is not None:
                kind = next((k for k in MEASURE_KINDS if k in f.spec), None)
                if not isinstance(self.entities.get(f.entity, {}).get("currency"), dict):
                    report(path + ("convert",), label, f"`convert` needs `currency: {{ code, date }}` on entity "
                           f"`{f.entity}`: which currency and date are its amounts in?")
                elif conv != target:
                    report(path + ("convert",), label, f"`convert: {conv}`: rates convert to `{target}` only; "
                           f"use `convert: {target}`")
                if kind in ("count", "count_distinct", "calc"):
                    report(path + ("convert",), label, f"`convert` applies to amounts; `{kind}` measures "
                           f"are not converted")
            if "calc" in f.spec:
                parts = []
                for r in self.calc_refs(f):
                    try:
                        parts.append(self.resolve(r, ("measure",)).field)
                    except RefError:
                        pass
                local = [m for m in parts if self.per_currency(m)]
                conv = [m for m in parts if self.converts(m) or self._converted_calc(m, (f,))]
                if local and conv:
                    report(path + ("calc",), label, f"mixes `{conv[0].name}` (converted to {target}) with "
                           f"`{local[0].name}` (document currency); use converted measures throughout")

    # ------------------------------------------------------------------ data source

    def check_catalog(self, catalog: dict, connection: str, report: Report):
        """Tables and columns the model names must exist in the data source.
        `catalog` maps table -> list of columns, or None when the columns cannot be read."""
        rates = self.currency.get("rates")
        if isinstance(rates, dict) and isinstance(rates.get("table"), str):
            table = rates["table"]
            if table not in catalog:
                report(("currency", "rates", "table"), "currency rates",
                       f"table `{table}` not found in connection `{connection}`" + did_you_mean(table, catalog))
            elif catalog[table] is not None:
                for part in ("currency", "month", "rate"):
                    col = rates.get(part)
                    if isinstance(col, str) and col not in catalog[table]:
                        report(("currency", "rates", part), "currency rates",
                               f"column `{col}` not found in table `{table}`" + did_you_mean(col, catalog[table]))
        for en, e in self.entities.items():
            table = e.get("table")
            if not isinstance(table, str):
                continue
            if table not in catalog:
                report(("entities", en, "table"), f"entity `{en}`",
                       f"table `{table}` not found in connection `{connection}`"
                       + (did_you_mean(table, catalog) or ".") + " If it is new, add it under `tables` in project.yaml.")
                continue
            cols = catalog[table]
            if cols is None:
                continue

            def need(column, path, label):
                if isinstance(column, str) and column not in cols:
                    report(path, label, f"column `{column}` not found in table `{table}`"
                           + did_you_mean(column, cols))

            key = e.get("key")
            for k in key if isinstance(key, list) else [key]:
                need(k, ("entities", en, "key"), f"entity `{en}`")
            for target, fk in (e.get("joins") or {}).items() if isinstance(e.get("joins"), dict) else []:
                for c in fk if isinstance(fk, list) else [fk]:
                    try:
                        refs = [r for r, _ in references(parse_colexpr(str(c)))]
                    except ColExprError:
                        continue  # reported by the join check
                    for r in refs:
                        if "." not in r:  # prefixed names are reported by the join check
                            need(r, ("entities", en, "joins", target), f"entity `{en}`")
            for name, spec in self.rollups(en).items():
                if spec.get("from") not in self.entities or en not in self.joins(spec["from"]):
                    continue  # a wrong `from` is reported once, by the rollup check
                child = self.entities.get(spec.get("from"), {})
                ccols = catalog.get(child.get("table"))
                kind = self.rollup_kind(spec)
                if ccols is None or not kind or not isinstance(spec.get(kind), str):
                    continue
                try:
                    refs = [r for r, _ in references(parse_colexpr(spec[kind]))]
                except ColExprError:
                    continue
                for r in refs:
                    if "." not in r and r not in ccols:
                        report(("entities", en, "rollups", name, kind), f"rollup `{name}`",
                               f"column `{r}` not found in table `{child.get('table')}`" + did_you_mean(r, ccols))
                if name in cols:
                    report(("entities", en, "rollups", name), f"rollup `{name}`",
                           f"`{name}` is also a column of table `{table}`; give the rollup another name")
            for f in self.fields(en):
                if f.kind == "dimension" and self.lookup(f):
                    lt, value, key, match = self.lookup(f)
                    path = ("entities", en, "dimensions", f.name)
                    if lt not in catalog:
                        report(path + ("lookup",), f"dimension `{f.name}`", f"lookup table `{lt}` not found in "
                               f"connection `{connection}`" + did_you_mean(lt, catalog))
                    elif catalog[lt] is not None:
                        for c in (value, key):
                            if c not in catalog[lt]:
                                report(path, f"dimension `{f.name}`", f"column `{c}` not found in lookup table "
                                       f"`{lt}`" + did_you_mean(c, catalog[lt]))
                    try:
                        for r, _ in references(parse_colexpr(match)):
                            if "." not in r:
                                need(r, path + ("match",), f"dimension `{f.name}`")
                    except ColExprError:
                        pass
                    continue
                if f.kind == "dimension":
                    need(f.spec.get("column", f.name), ("entities", en, "dimensions", f.name),
                         f"dimension `{f.name}`")
                    fiscal = f.spec.get("fiscal")
                    if isinstance(fiscal, dict):
                        for part in ("year", "period"):
                            need(fiscal.get(part), ("entities", en, "dimensions", f.name, "fiscal"),
                                 f"dimension `{f.name}`")
                    continue
                kind = next((k for k in MEASURE_KINDS if k in f.spec), None)
                if kind and kind != "calc" and isinstance(f.spec[kind], str):
                    try:
                        tree = parse_colexpr(f.spec[kind])
                    except ColExprError:
                        continue  # reported by the measure check
                    for text, _ in references(tree):
                        if "." not in text and text not in self.rollups(en):
                            need(text, ("entities", en, "measures", f.name, kind), f"measure `{f.name}`")

    def _check_paths(self, report: Report):
        """Cycles are errors. Two join paths to the same entity are an error only when they
        have the same length; otherwise the shorter one is used (ADR-009)."""
        reported = set()
        for start in self.entities:
            def walk(entity, trail):
                for t in self.joins(entity):
                    path = trail + [t]
                    if t in trail:
                        if ("cycle", frozenset(path)) not in reported:
                            reported.add(("cycle", frozenset(path)))
                            report(("entities", start, "joins"), f"entity `{start}`",
                                   "join cycle: " + " → ".join(path[path.index(t):]))
                        continue
                    walk(t, path)

            walk(start, [start])
            # Every simple route; where an entity is reachable by more than one, say which is used.
            routes: dict[str, list[list[str]]] = {}

            def collect(entity, trail):
                for t in self.joins(entity):
                    if t in trail:
                        continue
                    routes.setdefault(t, []).append(trail + [t])
                    collect(t, trail + [t])

            collect(start, [start])
            for t, rs in routes.items():
                rs = sorted(rs, key=len)
                if len(rs) > 1 and len(rs[0]) < len(rs[1]):
                    others = "; ".join(" → ".join(r) for r in rs[1:])
                    report(("entities", start, "joins"), f"entity `{start}`",
                           f"reaches `{t}` by {len(rs)} routes; using the shortest, {' → '.join(rs[0])} "
                           f"(not {others}). A measure can choose another date route with `date:`", "info")
            # Breadth first: every entity's shortest paths, and whether there is more than one.
            level, seen = {start: [[start]]}, {start}
            while level:
                nxt: dict[str, list[list[str]]] = {}
                for ent, paths in level.items():
                    for t in self.joins(ent):
                        if t in seen:
                            continue
                        nxt.setdefault(t, []).extend(p + [t] for p in paths)
                for t, paths in nxt.items():
                    if len(paths) > 1 and (start, t) not in reported:
                        reported.add((start, t))
                        report(("entities", start, "joins"), f"entity `{start}`",
                               f"reaches `{t}` through two join paths of the same length: "
                               f"{' → '.join(paths[0])} and {' → '.join(paths[1])}; keep one (ADR-009)")
                seen |= set(nxt)
                level = {t: paths[:1] for t, paths in nxt.items()}


def _intersect(a: Grain, b: Grain) -> Grain:
    out = {}
    for k, tg in a.items():
        if k in b:
            out[k] = tg if _GRAIN_RANK[tg] >= _GRAIN_RANK[b[k]] else b[k]
    return out
