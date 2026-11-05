"""Standalone formats CLI.

    obi-formats validate <project-dir>
    obi-formats fmt [--check] <file-or-dir>...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .artifacts import canonicalize
from .schema import artifact_type
from .validate import validate_project
from .yamlio import FormatError


def _artifact_files(paths: list[str]) -> list[Path]:
    out = []
    for p in map(Path, paths):
        candidates = sorted(p.rglob("*.yaml")) if p.is_dir() else [p]
        out += [c for c in candidates if artifact_type(c)]
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # messages contain → and ▲; Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="obi-formats")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate every artifact of a project")
    v.add_argument("project", nargs="?", default=".")
    f = sub.add_parser("fmt", help="rewrite artifacts in canonical form")
    f.add_argument("paths", nargs="+")
    f.add_argument("--check", action="store_true", help="only report files that are not canonical")
    args = ap.parse_args(argv)

    if args.cmd == "validate":
        root = Path(args.project)
        issues = validate_project(root)
        for issue in issues:
            print(f"{root.as_posix()}/{issue}")
        errors = sum(i.severity == "error" for i in issues)
        print(f"{errors} error(s)" if errors else "valid", file=sys.stderr)
        return 1 if errors else 0

    changed = 0
    for path in _artifact_files(args.paths):
        text = path.read_text(encoding="utf-8")
        try:
            out = canonicalize(text, artifact_type(path))
        except FormatError as e:
            print(f"{path.as_posix()}:{e.line or 1}: {e.message}")
            changed += 1
            continue
        if out != text:
            changed += 1
            if args.check:
                print(f"{path.as_posix()}: not canonical")
            else:
                path.write_text(out, encoding="utf-8", newline="\n")
                print(f"{path.as_posix()}: reformatted")
    return 1 if changed and args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
