"""Test session setup: tests run against a fresh copy of `examples/`.

The examples' database files (`.obi/*.duckdb`) hold whatever someone entered in `obi serve`.
Tests must neither depend on that nor overwrite it, so each session copies `examples/` without
`.obi/` to a temporary folder and points `OBI_EXAMPLES` at it. Writable tables there are
migrated and seeded on first use.
"""

import os
import shutil
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config):
    if os.environ.get("OBI_EXAMPLES"):
        return
    target = Path(tempfile.mkdtemp(prefix="obi-examples-")) / "examples"
    shutil.copytree(_ROOT / "examples", target, ignore=shutil.ignore_patterns(".obi"))
    os.environ["OBI_EXAMPLES"] = str(target)
    from obi_engine import Project
    from obi_engine.store import migrate

    for project in ("sales-demo", "erp-sales"):  # fresh databases, seeded
        migrate(Project(target / project))


def examples() -> Path:
    """The examples folder tests should read and write."""
    return Path(os.environ.get("OBI_EXAMPLES") or _ROOT / "examples")
