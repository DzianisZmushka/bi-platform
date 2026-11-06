"""Users and sessions (ADR-014).

Users live in `users.json` in the data directory, next to the project database, never in Git:
name, scrypt password hash, roles, attributes, and `admin` (sees failed deploys). A user becomes
the dictionary access rules read, `{"name", "roles", <attribute>: value}`, exactly what
`obi serve --user/--role/--attr` produced in Phase 2a, so an identity provider can replace this
store without touching access.yaml.

A session is a signed cookie: name, expiry and a fingerprint of the password hash, under
HMAC-SHA256. The identity behind it is read from the store on every request, so a role change or
a removal applies at once, and a new password ends the user's sessions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from obi_formats.artifacts import parse
from obi_formats.modelindex import did_you_mean

STORE_FILE = "users.json"
SECRET_FILE = "secret.key"
SESSION_HOURS = 12
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class UserError(Exception):
    """A message for the person running `obi users`."""


# =========================================================================== passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${hash}".format(
        **_SCRYPT, salt=base64.b64encode(salt).decode(), hash=base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(want)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(want))


_DUMMY = hash_password(secrets.token_hex(8))  # an unknown name costs as much as a wrong password


# =========================================================================== access.yaml shape


def access_shape(root: Path) -> tuple[list[str], dict]:
    """Roles and declared attributes of the project's access.yaml."""
    path = Path(root) / "access.yaml"
    if not path.is_file():
        return [], {}
    data = parse(path.read_text(encoding="utf-8")).data
    roles = list(data.get("roles") or {}) if isinstance(data.get("roles"), dict) else []
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    return roles, attrs


def parse_attributes(root: Path, items: list[str], strict: bool = False) -> dict:
    """`key=value` items to attribute values; a `type: list` attribute takes comma-separated values.
    `strict`: every key must be declared in access.yaml (`obi users`; `--attr` stays lenient)."""
    _, declared = access_shape(root)
    out: dict = {}
    for item in items:
        k, sep, v = item.partition("=")
        k, v = k.strip(), v.strip()
        if strict and (not sep or not k):
            raise UserError(f"`{item}` is not KEY=VALUE, e.g. sales_orgs=DE01,PL01")
        if strict and k not in declared:
            raise UserError(f"attribute `{k}` is not declared under `attributes` in access.yaml; declared: "
                            f"{', '.join(declared) or 'none'}" + did_you_mean(k, list(declared)))
        kind = (declared.get(k) or {}).get("type") if isinstance(declared.get(k), dict) else None
        out[k] = [x.strip() for x in v.split(",") if x.strip()] if kind == "list" else v
    return out


def check_roles(root: Path, roles: list[str]):
    known, _ = access_shape(root)
    for r in roles:
        if r not in known:
            raise UserError(f"role `{r}` is not in access.yaml; roles are {', '.join(known) or 'none'}"
                            + did_you_mean(r, known))


# =========================================================================== the store


@dataclass
class UserStore:
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cache: tuple = field(default=(None, {}), repr=False)

    @classmethod
    def at(cls, data: Path) -> "UserStore":
        return cls(Path(data) / STORE_FILE)

    def _load(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return {}
        if self._cache[0] != mtime:  # `obi users` may change the file while the server runs
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._cache = (mtime, data.get("users") or {})
        return self._cache[1]

    def _save(self, users: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        text = json.dumps({"users": dict(sorted(users.items()))}, indent=2, ensure_ascii=False) + "\n"
        tmp.write_text(text, encoding="utf-8", newline="\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        self._cache = (None, {})

    def names(self) -> list[str]:
        return sorted(self._load())

    def get(self, name: str) -> dict | None:
        u = self._load().get(name)
        return dict(u) if isinstance(u, dict) else None

    def add(self, name: str, password: str, roles: list[str], attributes: dict, admin: bool = False):
        if not NAME.match(name or ""):
            raise UserError(f"`{name}` is not a user name: letters, digits and . _ @ -, at most 64")
        if len(password) < 8:
            raise UserError("the password needs at least 8 characters")
        with self._lock:
            users = dict(self._load())
            if name in users:
                raise UserError(f"user `{name}` exists; change it with `obi users set {name}`")
            users[name] = {"password": hash_password(password), "roles": list(roles),
                           "attributes": attributes, "admin": bool(admin)}
            self._save(users)

    def update(self, name: str, roles: list[str] | None = None, attributes: dict | None = None,
               admin: bool | None = None, password: str | None = None):
        with self._lock:
            users = dict(self._load())
            if name not in users:
                raise UserError(f"no user `{name}`" + did_you_mean(name, list(users)))
            u = dict(users[name])
            if roles is not None:
                u["roles"] = list(roles)
            if attributes is not None:
                u["attributes"] = attributes
            if admin is not None:
                u["admin"] = bool(admin)
            if password is not None:
                if len(password) < 8:
                    raise UserError("the password needs at least 8 characters")
                u["password"] = hash_password(password)
            users[name] = u
            self._save(users)

    def remove(self, name: str):
        with self._lock:
            users = dict(self._load())
            if users.pop(name, None) is None:
                raise UserError(f"no user `{name}`" + did_you_mean(name, list(self._load())))
            self._save(users)

    # ---- the identity access rules read

    def identity(self, name: str) -> dict | None:
        """`{"name", "roles", <attribute>: value}`: the shape `--user/--role/--attr` produce."""
        u = self.get(name)
        if u is None:
            return None
        attrs = {k: v for k, v in (u.get("attributes") or {}).items() if k not in ("name", "roles")}
        return {"name": name, "roles": list(u.get("roles") or []), **attrs}

    def is_admin(self, name: str) -> bool:
        return bool((self.get(name) or {}).get("admin"))

    def authenticate(self, name: str, password: str) -> dict | None:
        u = self.get(name)
        if not verify_password(password, u["password"] if u else _DUMMY) or u is None:
            return None
        return self.identity(name)

    def fingerprint(self, name: str) -> str | None:
        u = self.get(name)
        return hashlib.sha256(u["password"].encode()).hexdigest()[:16] if u else None


# =========================================================================== sessions


def secret_key(data: Path) -> bytes:
    """`OBI_SECRET_KEY`, else a key generated once into the data directory (never in Git)."""
    env = os.environ.get("OBI_SECRET_KEY")
    if env:
        return env.encode("utf-8")
    path = Path(data) / SECRET_FILE
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip().encode("utf-8")


def _sign(key: bytes, payload: str, fingerprint: str) -> str:
    return hmac.new(key, f"{payload}|{fingerprint}".encode(), hashlib.sha256).hexdigest()


def make_session(key: bytes, store: UserStore, name: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_HOURS * 3600)
    payload = f"{name}|{exp}"
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{token}.{_sign(key, payload, store.fingerprint(name) or '')}"


def read_session(key: bytes, store: UserStore, value: str | None, now: float | None = None) -> str | None:
    """The user name a session cookie carries, or None when it is missing, forged, expired, or
    its user was removed or changed password."""
    if not value or "." not in value:
        return None
    token, sig = value.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        name, exp = payload.rsplit("|", 1)
        expires = int(exp)
    except (ValueError, UnicodeDecodeError):
        return None
    fp = store.fingerprint(name)
    if fp is None or not hmac.compare_digest(sig, _sign(key, payload, fp)):
        return None
    return name if expires > (now or time.time()) else None
