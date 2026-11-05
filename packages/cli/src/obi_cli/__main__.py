"""The `obi` command.

    obi validate [PROJECT] [--execute]
    obi catalog  [PROJECT]
    obi render   [PROJECT] [--dashboard ID] [--out DIR] [--filter ID=V1,V2] [--as-of DATE]
    obi serve    [PROJECT] [--port 8765] [--as-of DATE]

`obi validate` is static: schemas, references, expressions, and columns from the committed
catalog.json. It needs no data access, so it is what CI runs. `--execute` also checks
catalog.json against the live source and runs every measure on the data. render and serve
always validate with --execute, because they read the data anyway (ADR-008).
"""

from __future__ import annotations

import argparse
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

    s = sub.add_parser("serve", help="serve dashboards locally; filters and navigation work")
    s.add_argument("project", nargs="?", default=".")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--as-of", type=date.fromisoformat)

    args = ap.parse_args(argv)
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
        from .server import serve
        serve(root, args.port, args.as_of)
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


if __name__ == "__main__":
    raise SystemExit(main())
