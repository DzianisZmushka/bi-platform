"""The `obi` command.

    obi validate [PROJECT] [--execute]
    obi catalog  [PROJECT]
    obi migrate  [PROJECT] [--reseed TABLE]
    obi render   [PROJECT] [--dashboard ID] [--out DIR] [--filter ID=V1,V2] [--as-of DATE]
    obi serve    [PROJECT] [--host 127.0.0.1] [--port 8765] [--as-of DATE] [--user NAME --role ROLE --attr KEY=VALUE]
    obi serve    --git URL [--branch main] [--path DIR] [--home DIR]       (ADR-014)
    obi deploy   [--url http://127.0.0.1:8765] [--token TOKEN]
    obi users    add|set|password|remove|list NAME [--role ROLE] [--attr KEY=VALUE] [--admin] [--project DIR | --home DIR]

`obi serve` without `--user` asks for sign-in; users live in `users.json` in the data directory
(`PROJECT/.obi`, or `HOME/data` for a Git site), never in Git. Options default from the
environment: OBI_GIT_URL, OBI_GIT_BRANCH, OBI_GIT_PATH, OBI_HOME, OBI_HOST, OBI_PORT,
OBI_DEPLOY_TOKEN, OBI_SECRET_KEY.

`obi validate` is static: schemas, references, expressions, and columns from the committed
catalog.json. It needs no data access, so it is what CI runs. `--execute` also checks
catalog.json against the live source and runs every measure on the data. render and serve
always validate with --execute, because they read the data anyway (ADR-008).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .checks import validate_all as validate_project


def _validate(root: Path, execute: bool) -> int:
    issues = validate_project(root, execute=execute)
    for issue in issues:
        print(f"{root.as_posix()}/{issue}", file=sys.stderr)
    return sum(i.severity == "error" for i in issues)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # messages contain → and ▲; Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="obi", description="Open BI Platform command line (Phase 0).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate every artifact of a project")
    v.add_argument("project", nargs="?", default=".")
    v.add_argument("--execute", action="store_true",
                   help="also run every measure on the data and check catalog.json against it")

    c = sub.add_parser("catalog", help="write catalog.json: tables and columns of the data source")
    c.add_argument("project", nargs="?", default=".")

    r = sub.add_parser("render", help="render dashboards to static HTML")
    r.add_argument("project", nargs="?", default=".")
    r.add_argument("--dashboard", "-d", action="append", help="dashboard id (default: all)")
    r.add_argument("--out", "-o", help="output directory (default: PROJECT/.obi/render)")
    r.add_argument("--filter", "-f", action="append", default=[], metavar="ID=V1,V2",
                   help="set a filter, e.g. region=EMEA,APAC or period=2026-01-01..2026-03-31; "
                        "an empty value clears the default")
    r.add_argument("--as-of", type=date.fromisoformat, help="date that presets such as this_year use")

    env = os.environ.get
    s = sub.add_parser("serve", help="serve dashboards and forms; sign-in unless --user")
    s.add_argument("project", nargs="?", default=".")
    s.add_argument("--host", default=env("OBI_HOST", "127.0.0.1"), help="address to bind (0.0.0.0 in a container)")
    s.add_argument("--port", type=int, default=int(env("OBI_PORT", "8765")))
    s.add_argument("--as-of", type=date.fromisoformat)
    s.add_argument("--git", default=env("OBI_GIT_URL"), metavar="URL",
                   help="serve a branch of this repository (URL or path) and re-deploy on `obi deploy`")
    s.add_argument("--branch", default=env("OBI_GIT_BRANCH", "main"))
    s.add_argument("--path", default=env("OBI_GIT_PATH", ""), help="the project's directory inside the repository")
    s.add_argument("--home", default=env("OBI_HOME"), help="releases and data of a Git site (default: ./.obi-site)")
    s.add_argument("--user", help="local development only: act as this user, no sign-in (loopback only)")
    s.add_argument("--role", action="append", default=[], metavar="ROLE",
                   help="a role from access.yaml, repeatable; read as `user.roles`")
    s.add_argument("--attr", "--user-attr", dest="attr", action="append", default=[], metavar="KEY=VALUE",
                   help="a user attribute declared in access.yaml, e.g. regions=EMEA (list attributes: comma-separated)")

    g = sub.add_parser("migrate", help="create or alter the writable tables in the project's database file")
    g.add_argument("project", nargs="?", default=".")
    g.add_argument("--reseed", action="append", default=[], metavar="TABLE",
                   help="empty a writable table (rows go to its history) and load its seed again")

    d = sub.add_parser("deploy", help="tell a running Git-backed `obi serve` to fetch, validate and swap")
    d.add_argument("--url", default=env("OBI_DEPLOY_URL") or f"http://127.0.0.1:{env('OBI_PORT', '8765')}")
    d.add_argument("--token", default=env("OBI_DEPLOY_TOKEN"),
                   help="the server's deploy token (default: OBI_DEPLOY_TOKEN, or HOME/deploy.token)")
    d.add_argument("--home", default=env("OBI_HOME"))
    d.add_argument("--wait", type=int, default=900, help="seconds to wait for the result (default 900)")

    u = sub.add_parser("users", help="manage the users who may sign in (never stored in Git)")
    u.add_argument("action", choices=["add", "set", "password", "remove", "list"])
    u.add_argument("name", nargs="?")
    u.add_argument("--project", default=".", help="a local project (users in PROJECT/.obi)")
    u.add_argument("--home", default=env("OBI_HOME"), help="a Git site's home: users of its deployed project")
    u.add_argument("--role", action="append", metavar="ROLE", help="a role from access.yaml, repeatable")
    u.add_argument("--attr", action="append", metavar="KEY=VALUE",
                   help="an attribute declared in access.yaml, e.g. sales_orgs=DE01,PL01")
    u.add_argument("--admin", action=argparse.BooleanOptionalAction, default=None,
                   help="sees failed deploys (platform administration, not a project role)")
    u.add_argument("--password-stdin", action="store_true", help="read the password from stdin, not a prompt")

    args = ap.parse_args(argv)
    if args.cmd == "deploy":
        return _deploy(args)
    if args.cmd == "users":
        return _users(args)
    if args.cmd == "serve" and args.git:
        return _serve_git(args)
    root = Path(args.project)
    if not (root / "project.yaml").is_file():
        print(f"obi: {root.as_posix()} is not a project (no project.yaml)", file=sys.stderr)
        return 2

    if args.cmd == "catalog":
        from obi_engine import Project
        from obi_formats import catalog
        cat = Project(root).describe()
        path = catalog.save(root, cat)
        cols = sum(len(c) for c in cat["tables"].values())
        print(f"{path.as_posix()}: {len(cat['tables'])} tables, {cols} columns", file=sys.stderr)
        return 0

    if args.cmd == "validate":
        errors = _validate(root, args.execute)
        print(f"{errors} error(s)" if errors else "valid", file=sys.stderr)
        return 1 if errors else 0

    if args.cmd == "serve":
        from obi_runtime.deploy import Site
        from .server import serve
        user = serve_user(root, args.user, args.role, args.attr) if args.user else None
        if user is None and (args.role or args.attr):
            print("obi: --role and --attr need --user; without it, users sign in", file=sys.stderr)
            return 2
        serve(Site(root), args.host, args.port, args.as_of, user)
        return 0

    if args.cmd == "migrate":
        from obi_engine import Project
        from obi_engine.store import WriteError, migrate
        errors = _validate(root, execute=False)
        if errors:
            print(f"{errors} error(s); fix them before migrating", file=sys.stderr)
            return 1
        try:
            done = migrate(Project(root), tuple(args.reseed))
        except WriteError as e:
            print(f"obi: {e}", file=sys.stderr)
            return 1
        for line in done or ["the database already matches the model"]:
            print(line, file=sys.stderr)
        return 0

    errors = _validate(root, execute=True)
    if errors:
        print(f"{errors} error(s); fix them before rendering", file=sys.stderr)
        return 1

    from .render import dashboard_ids, filter_ids, render_dashboard
    inputs: dict[str, list[str]] = {}
    for item in args.filter:
        fid, _, values = item.partition("=")
        inputs[fid.strip()] = [x.strip() for x in values.split(",") if x.strip()] if values else []
    out = Path(args.out) if args.out else root / ".obi" / "render"
    out.mkdir(parents=True, exist_ok=True)
    ids = args.dashboard or [d for d, _ in dashboard_ids(root)]
    known = {d for d, _ in dashboard_ids(root)}
    failed = 0
    for did in ids:
        if did not in known:
            print(f"obi: no dashboard `{did}`; available: {', '.join(sorted(known))}", file=sys.stderr)
            return 2
        unknown = set(inputs) - set(filter_ids(root, did))
        result = render_dashboard(root, did, {k: v for k, v in inputs.items() if k not in unknown} or None,
                                  args.as_of, mode="static")
        path = out / f"{did}.html"
        path.write_text(result.html, encoding="utf-8", newline="\n")
        print(path.as_posix())
        for e in result.errors:
            print(f"  problem: {e}", file=sys.stderr)
        failed += bool(result.errors)
    return 1 if failed else 0


def serve_user(root: Path, name: str, roles: list[str], attrs: list[str]) -> dict:
    """The user `obi serve --user` acts as (local development and tests): name, roles, and
    attributes, the same shape a signed-in user from the store has. A list attribute in
    access.yaml (`type: list`) takes comma-separated values."""
    from obi_runtime.users import parse_attributes

    return {"name": name, "roles": list(roles), **parse_attributes(root, attrs)}


def _serve_git(args) -> int:
    from .server import git_site, serve
    if args.user:
        print("obi: --user is for a local project; a Git site signs users in", file=sys.stderr)
        return 2
    site = git_site(Path(args.home or ".obi-site"), args.git, args.branch, args.path)
    serve(site, args.host, args.port, args.as_of)
    return 0


def _deploy(args) -> int:
    import json
    import urllib.error
    import urllib.request

    token = args.token
    if not token and args.home and (Path(args.home) / "deploy.token").is_file():
        token = (Path(args.home) / "deploy.token").read_text(encoding="utf-8").strip()
    if not token:
        print("obi: no deploy token: pass --token, set OBI_DEPLOY_TOKEN, or --home of the site", file=sys.stderr)
        return 2
    import time

    base, auth = args.url.rstrip("/"), {"Authorization": f"Bearer {token}"}

    def call(method: str, path: str) -> tuple[int, dict]:
        req = urllib.request.Request(base + path, data=b"" if method == "POST" else None, method=method, headers=auth)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        code, accepted = call("POST", "/_deploy")
        if code != 202:
            print(f"obi: {accepted.get('message')}", file=sys.stderr)
            return 1
        # The server deploys in the background; wait for a deploy that covers this request.
        deadline = time.monotonic() + args.wait
        while True:
            code, status = call("GET", "/_deploy/status")
            if code != 200:
                print(f"obi: {status.get('message')}", file=sys.stderr)
                return 1
            if status.get("state") == "finished" and status.get("run", 0) >= accepted["run"]:
                break
            if time.monotonic() > deadline:
                print(f"obi: the deploy is still running after {args.wait}s; see {base}/_deploy/status",
                      file=sys.stderr)
                return 1
            time.sleep(0.5)
    except OSError as e:
        print(f"obi: cannot reach {args.url}: {e}", file=sys.stderr)
        return 2
    print(f"obi: {status.get('message')} (started {status.get('started')}, finished {status.get('finished')})",
          file=sys.stderr)
    for err in status.get("errors") or []:
        print(f"  {err}", file=sys.stderr)
    return 0 if status.get("ok") else 1


def _users(args) -> int:
    import getpass

    from obi_engine import data_dir
    from obi_runtime.deploy import GitSite
    from obi_runtime.users import UserError, UserStore, check_roles, parse_attributes

    if args.home:  # a Git site: the deployed project's access.yaml, the site's data
        site = GitSite(Path(args.home), "", "", os.environ.get("OBI_GIT_PATH", ""))
        rel = site.restore()
        if rel is None:
            print(f"obi: no release is deployed in {args.home} yet; users are checked against the deployed "
                  f"access.yaml", file=sys.stderr)
            return 2
        root, data = rel.root, site.data
    else:
        root = Path(args.project)
        if not (root / "project.yaml").is_file():
            print(f"obi: {root.as_posix()} is not a project (no project.yaml)", file=sys.stderr)
            return 2
        data = data_dir(root)
    store = UserStore.at(data)
    if args.action == "list":
        for n in store.names():
            u = store.get(n) or {}
            attrs = "; ".join(f"{k}={','.join(v) if isinstance(v, list) else v}"
                              for k, v in (u.get("attributes") or {}).items())
            print(f"{n}  roles={','.join(u.get('roles') or []) or '-'}  {attrs or '-'}"
                  + ("  admin" if u.get("admin") else ""))
        return 0
    if not args.name:
        print(f"obi: `obi users {args.action}` needs a user name", file=sys.stderr)
        return 2

    def password() -> str:
        if args.password_stdin:
            return sys.stdin.readline().rstrip("\r\n")
        first = getpass.getpass(f"Password for {args.name}: ")
        if first != getpass.getpass("Again: "):
            raise UserError("the passwords differ")
        return first

    try:
        if args.role:
            check_roles(root, args.role)
        attrs = parse_attributes(root, args.attr, strict=True) if args.attr else None
        if args.action == "add":
            store.add(args.name, password(), args.role or [], attrs or {}, bool(args.admin))
        elif args.action == "set":
            store.update(args.name, roles=args.role, attributes=attrs, admin=args.admin)
        elif args.action == "password":
            store.update(args.name, password=password())
        else:
            store.remove(args.name)
    except UserError as e:
        print(f"obi users: {e}", file=sys.stderr)
        return 1
    print(f"obi users: {args.action} {args.name}: done ({store.path.as_posix()})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
