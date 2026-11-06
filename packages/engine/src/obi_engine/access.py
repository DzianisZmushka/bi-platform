"""Who may see and write what (ADR-013): access.yaml resolved for one user.

A user's permissions are the union of the `default` role and their own roles, minus every
`deny` of those roles. `edit` on a form implies `view` of it and `write` on its entity; `write`
implies `read`. Users and role membership are never in Git: `obi serve --user/--role/--attr`
supplies them now, an identity provider later.

`Grants.system()` is the engine without a user (`obi render`, `obi validate --execute`): it
reads every row. `obi serve` always has a user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from obi_formats.artifacts import parse

ACCESS_FILE = "access.yaml"

# Every access.yaml property and what enforces it (test_coverage.py walks the schema against this).
HANDLED_ACCESS = {
    "access": {"access", "description", "attributes", "roles", "columns"},
    "attribute": {"type", "description"},           # the `obi serve --attr` parser and the validator
    "role": {"description", "view", "edit", "read", "write", "rows", "deny"},
    "deny": {"view", "edit", "read", "write"},
    "artifacts": {"dashboards", "forms"},
    "form_artifacts": {"forms"},
    "column_rule": {"read", "write"},
}


class NoAccess(Exception):
    """The user may not read what a widget needs. The page shows "no access", not an error."""


def _glob(pattern: str, name: str) -> bool:
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), name) is not None


def _match(patterns, name: str) -> bool:
    return any(isinstance(p, str) and _glob(p, name) for p in patterns or [])


@dataclass
class Grants:
    user: dict | None                      # None: the system, sees and may do everything
    spec: dict = field(default_factory=dict)
    forms: dict[str, str] = field(default_factory=dict)  # form id -> entity it writes

    @classmethod
    def system(cls) -> "Grants":
        return cls(None)

    @classmethod
    def load(cls, root: str | Path, user: dict | None) -> "Grants":
        """Grants for `user` in the project at `root`. Without access.yaml every user may do
        everything; a model with `access:` rules then fails validation instead."""
        root = Path(root)
        spec = {}
        if (root / ACCESS_FILE).is_file():
            spec = parse((root / ACCESS_FILE).read_text(encoding="utf-8")).data or {}
        forms = {}
        for p in sorted((root / "forms").glob("*.form.yaml")) if (root / "forms").is_dir() else []:
            d = parse(p.read_text(encoding="utf-8")).data or {}
            if isinstance(d.get("entity"), str):
                forms[p.name.removesuffix(".form.yaml")] = d["entity"]
        return cls(user, spec, forms)

    # ---- the user's roles

    @property
    def is_system(self) -> bool:
        return self.user is None

    @property
    def configured(self) -> bool:
        return bool(self.spec.get("roles"))

    @property
    def role_names(self) -> list[str]:
        if self.user is None:
            return []
        return ["default"] + [r for r in self.user.get("roles") or [] if r != "default"]

    def _roles(self) -> list[dict]:
        roles = self.spec.get("roles") or {}
        return [roles[r] for r in self.role_names if isinstance(roles.get(r), dict)]

    def _allowed(self, verb: str, kind: str | None, name: str) -> bool:
        def pats(block):
            if kind is None:
                return block if isinstance(block, list) else []
            return (block or {}).get(kind) if isinstance(block, dict) else []
        allow = any(_match(pats(r.get(verb)), name) for r in self._roles())
        deny = any(_match(pats((r.get("deny") or {}).get(verb)), name) for r in self._roles())
        return allow and not deny

    def _denied(self, verb: str, kind: str | None, name: str) -> bool:
        for r in self._roles():
            block = (r.get("deny") or {}).get(verb)
            pats = block if kind is None else ((block or {}).get(kind) if isinstance(block, dict) else [])
            if _match(pats, name):
                return True
        return False

    # ---- artifacts

    def can_view_dashboard(self, did: str) -> bool:
        return self.is_system or not self.configured or self._allowed("view", "dashboards", did)

    def can_edit_form(self, fid: str) -> bool:
        if self.is_system or not self.configured:
            return True
        entity = self.forms.get(fid)
        return self._allowed("edit", "forms", fid) and not (entity and self._denied("write", None, entity))

    def can_view_form(self, fid: str) -> bool:
        if self.is_system or not self.configured:
            return True
        return (self._allowed("view", "forms", fid) or self.can_edit_form(fid)) \
            and not self._denied("view", "forms", fid)

    # ---- entities and columns

    def can_read(self, entity: str) -> bool:
        if self.is_system or not self.configured:
            return True
        return (self._allowed("read", None, entity) or self.can_write(entity)) \
            and not self._denied("read", None, entity)

    def can_write(self, entity: str, via_form: str | None = None) -> bool:
        """`write`, or `edit` on a form that writes the entity (the one used, when given)."""
        if self.is_system or not self.configured:
            return True
        if self._denied("write", None, entity):
            return False
        if self._allowed("write", None, entity):
            return True
        forms = [via_form] if via_form else [f for f, e in self.forms.items() if e == entity]
        return any(self.forms.get(f) == entity and self._allowed("edit", "forms", f) for f in forms)

    def _column_rule(self, entity: str, column: str) -> dict:
        rule = (self.spec.get("columns") or {}).get(f"{entity}.{column}")
        return rule if isinstance(rule, dict) else {}

    def can_read_column(self, entity: str, column: str) -> bool:
        roles = self._column_rule(entity, column).get("read")
        return self.is_system or not roles or bool(set(roles) & set(self.role_names))

    def can_write_column(self, entity: str, column: str) -> bool:
        roles = self._column_rule(entity, column).get("write")
        return self.is_system or not roles or bool(set(roles) & set(self.role_names))

    def column_readers(self, entity: str, column: str) -> list[str]:
        return list(self._column_rule(entity, column).get("read") or [])

    def column_writers(self, entity: str, column: str) -> list[str]:
        return list(self._column_rule(entity, column).get("write") or [])

    # ---- rows

    @property
    def all_rows(self) -> bool:
        """The system, and roles with `rows: all`, are not limited by `access:` conditions."""
        if self.is_system:
            return True
        return any(r.get("rows") == "all" for r in self._roles())
