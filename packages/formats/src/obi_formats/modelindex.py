"""Semantic index of a model: name resolution, join paths and grain (ADR-001, ADR-005)."""

from __future__ import annotations

import difflib
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Callable

import sqlglot
from sqlglot import exp

TIME_GRAINS = ("day", "week", "month", "quarter", "year")
_GRAIN_RANK = {None: 0, "day": 1, "week": 2, "month": 3, "quarter": 4, "year": 5}
MEASURE_KINDS = ("sum", "count", "count_distinct", "avg", "min", "max", "calc")

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
        j = self.entities.get(entity, {}).get("joins")
        return {t: c for t, c in j.items() if t in self.entities} if isinstance(j, dict) else {}

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
        try:
            tree = sqlglot.parse_one(f.spec["calc"])
        except Exception:
            return []
        return [".".join(p for p in (c.table, c.name) if p) for c in tree.find_all(exp.Column)]

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
            if isinstance(f.spec.get(key), str):
                try:
                    sqlglot.parse_one(f.spec[key])
                except Exception as e:
                    first = str(e).splitlines()[0]
                    report(path + (key,), label, f"`{key}` is not a valid SQL expression: {first}")
                    return
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
        for target in joins:
            path = ("entities", en, "joins", target)
            if target not in self.entities:
                report(path, f"entity `{en}`",
                       f"joins unknown entity `{target}`" + did_you_mean(target, self.entities))
                continue
            key = self.entities[target].get("key")
            if isinstance(key, list) and len(key) > 1:
                report(path, f"entity `{en}`",
                       f"cannot join to `{target}`: its key has several columns ({', '.join(key)})")

    def _check_paths(self, report: Report):
        reported = set()
        for start in self.entities:
            first: dict[str, list[str]] = {}

            def walk(entity, trail):
                for t in self.joins(entity):
                    path = trail + [t]
                    if t in trail:
                        if ("cycle", frozenset(path)) not in reported:
                            reported.add(("cycle", frozenset(path)))
                            report(("entities", start, "joins"), f"entity `{start}`",
                                   "join cycle: " + " → ".join(path[path.index(t):]))
                        continue
                    if t in first:
                        if (start, t) not in reported:
                            reported.add((start, t))
                            report(("entities", start, "joins"), f"entity `{start}`",
                                   f"reaches `{t}` through two join paths: {' → '.join(first[t])} and "
                                   f"{' → '.join(path)}; keep exactly one (ADR-001)")
                        continue
                    first[t] = path
                    walk(t, path)

            walk(start, [start])


def _intersect(a: Grain, b: Grain) -> Grain:
    out = {}
    for k, tg in a.items():
        if k in b:
            out[k] = tg if _GRAIN_RANK[tg] >= _GRAIN_RANK[b[k]] else b[k]
    return out
