"""JSON Schema access: key order, kind dispatch and per-kind validators.

The published schemas use `if`/`then` on the kind key, so editors validate each
element against its own definition. The obi validator does the same dispatch in
Python (see validate.py) and validates every element against a *shallow* copy of
its kind definition, in which child elements are only checked as objects. Each
element is then validated on its own, and errors name the element they belong to.
"""

from __future__ import annotations

import copy
import json
from functools import cache
from importlib.resources import files
from pathlib import PurePath
from typing import Any

from jsonschema import Draft202012Validator

ARTIFACT_TYPES = ("project", "model", "dashboard", "form", "access")


def artifact_type(path: str | PurePath) -> str | None:
    name = PurePath(path).name
    if name == "project.yaml":
        return "project"
    if name == "access.yaml":
        return "access"
    if name.endswith(".model.yaml"):
        return "model"
    if name.endswith(".form.yaml"):
        return "form"
    if name == "dashboard.yaml":
        return "dashboard"
    return None


class Schema:
    def __init__(self, name: str):
        self.name = name
        text = files("obi_formats.schemas").joinpath(f"{name}.schema.json").read_text("utf-8")
        self.raw: dict = json.loads(text)
        self.defs: dict[str, dict] = self.raw["$defs"]
        self.root_name = _ref_name(self.raw["$ref"])
        self.kind_defs = {d["x-kind"]: n for n, d in self.defs.items() if "x-kind" in d}
        self.element_keys = frozenset(k for k in self.kind_defs if k != self.root_name)
        self._shallow: dict | None = None
        self._validators: dict[str, Draft202012Validator] = {}

    # ---- navigation (used by the writer and the validator)

    def resolve(self, s: Any) -> dict:
        while isinstance(s, dict) and "$ref" in s:
            target = self.defs[_ref_name(s["$ref"])]
            extra = {k: v for k, v in s.items() if k != "$ref"}
            s = {**target, **extra} if extra else target
        return s if isinstance(s, dict) else {}

    def root_name_for(self, data) -> str:
        """The root definition for a document: form files hold either `form:` or `sheet:`."""
        kinds = self.dispatch({"$ref": f"#/$defs/{self.root_name}"}) if "allOf" in self.defs[self.root_name] else None
        if kinds and isinstance(data, dict):
            for k in data:
                if k in kinds:
                    return kinds[k]
        return self.root_name

    def root_for(self, data) -> dict:
        return self.defs[self.root_name_for(data)]

    def root(self) -> dict:
        return self.defs[self.root_name]

    def prop(self, s: dict, key: str) -> dict:
        s = self.resolve(s)
        props = s.get("properties", {})
        if key in props:
            return self.resolve(props[key])
        ap = s.get("additionalProperties")
        return self.resolve(ap) if isinstance(ap, dict) else {}

    def items(self, s: dict) -> dict:
        return self.resolve(self.resolve(s).get("items", {}))

    def order(self, s: dict, m: dict) -> list[str]:
        """Canonical key order: schema property order, then unknown keys as written.
        Maps of author-named entries keep the author's order."""
        props = self.resolve(s).get("properties")
        if not props:
            return list(m)
        known = [k for k in props if k in m]
        return known + [k for k in m if k not in props]

    def dispatch(self, items_schema: dict) -> dict[str, str] | None:
        """For an array of elements: kind key -> definition name. None if not elements."""
        raw = items_schema
        if "$ref" in raw:
            name = _ref_name(raw["$ref"])
            d = self.defs[name]
            if "x-kind" in d:
                return {d["x-kind"]: name}
            raw = d
        kinds = {}
        for clause in raw.get("allOf", []):
            then = clause.get("then")
            if isinstance(then, dict) and "$ref" in then and "required" in clause.get("if", {}):
                name = _ref_name(then["$ref"])
                kinds[self.defs[name]["x-kind"]] = name
        return kinds or None

    def item(self, items_schema: dict, value: Any) -> dict:
        kinds = self.dispatch(items_schema) if items_schema else None
        if kinds and isinstance(value, dict):
            for k in value:
                if k in kinds:
                    return self.defs[kinds[k]]
        return self.resolve(items_schema)

    def element_arrays(self, def_name: str) -> dict[str, dict[str, str]]:
        """Properties of a definition that hold arrays of elements: prop -> kinds."""
        out = {}
        for prop, sub in self.defs[def_name].get("properties", {}).items():
            arr = self.resolve(sub)
            if arr.get("type") == "array" and "items" in arr:
                kinds = self.dispatch(arr["items"])
                if kinds:
                    out[prop] = kinds
        return out

    # ---- validation

    def validator(self, def_name: str) -> Draft202012Validator:
        if def_name not in self._validators:
            if self._shallow is None:
                self._shallow = self._make_shallow()
            schema = {"$schema": self.raw["$schema"], "$defs": self._shallow,
                      "$ref": f"#/$defs/{def_name}"}
            self._validators[def_name] = Draft202012Validator(schema)
        return self._validators[def_name]

    def full_validator(self) -> Draft202012Validator:
        return Draft202012Validator(self.raw)

    def _make_shallow(self) -> dict:
        defs = copy.deepcopy(self.defs)
        for name in self.defs:
            for prop in self.element_arrays(name):
                arr = defs[name]["properties"][prop]
                if "$ref" in arr:  # e.g. items: {$ref: nodes}
                    arr = defs[name]["properties"][prop] = copy.deepcopy(self.resolve(arr))
                arr["items"] = {"type": "object"}
        return defs

    # ---- canonical data

    def normalize(self, data: Any, s: dict | None = None) -> Any:
        """Drop properties equal to their schema default; empty collections are dropped,
        except that an all-default entry of an author-named map becomes empty (None)."""
        s = self.resolve(self.root_for(data) if s is None else s)
        if isinstance(data, dict):
            props = s.get("properties") or {}
            named_map = not props and isinstance(s.get("additionalProperties"), dict)
            required = set(s.get("required", ()))
            out = {}
            for k, v in data.items():
                sub = self.prop(s, k)
                v = self.normalize(v, sub)
                if k in props and "default" in sub and _same(v, sub["default"]):
                    continue
                if isinstance(v, (dict, list)) and not v and not named_map and k not in required:
                    continue
                if named_map and isinstance(v, dict) and not v:
                    v = None
                out[k] = v
            return out
        if isinstance(data, list):
            items = s.get("items", {})
            return [self.normalize(x, self.item(items, x)) for x in data]
        return data


def _same(a, b) -> bool:
    return type(a) is type(b) and a == b


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


@cache
def get_schema(name: str) -> Schema:
    return Schema(name)


@cache
def all_element_keys() -> frozenset[str]:
    keys: set[str] = set()
    for t in ARTIFACT_TYPES:
        keys |= get_schema(t).element_keys
    return frozenset(keys)
