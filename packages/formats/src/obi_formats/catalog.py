"""The data-source catalogue: `catalog.json` at the project root (ADR-008).

Table and column names with their types, written by `obi catalog` from the live data source.
It holds schema, never data, so it is committed (CLAUDE.md: schemas belong in Git). Static
validation reads columns from it and needs no database access. `obi validate --execute`
compares it with the live source and reports drift.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

CATALOG_FILE = "catalog.json"

# {"connection": "local", "tables": {"order_lines": {"order_id": "VARCHAR", ...}, ...}}
Catalog = dict


def load(root: str | Path) -> Catalog | None:
    path = Path(root) / CATALOG_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save(root: str | Path, catalog: Catalog) -> Path:
    path = Path(root) / CATALOG_FILE
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return path


def columns(catalog: Catalog) -> dict[str, list[str]]:
    return {t: list(cols) for t, cols in (catalog.get("tables") or {}).items()}


def from_files(root: str | Path, connection: dict) -> dict[str, list[str] | None]:
    """Column names straight from local data files (CSV header, Parquet schema), for projects
    without a catalogue yet. None when a file is missing or unreadable."""
    out: dict[str, list[str] | None] = {}
    for table, rel in (connection.get("tables") or {}).items():
        path = Path(root) / str(rel)
        out[table] = None
        if not path.is_file():
            continue
        if path.suffix.lower() == ".csv":
            with open(path, newline="", encoding="utf-8") as f:
                out[table] = next(csv.reader(f), [])
        elif path.suffix.lower() == ".parquet":
            try:
                import pyarrow.parquet as pq
                out[table] = list(pq.read_schema(path).names)
            except ImportError:
                pass
    return out


def drift(cached: Catalog, live: Catalog) -> list[str]:
    """Differences between the committed catalogue and the live source, in plain words."""
    out = []
    if cached.get("connection") != live.get("connection"):
        out.append(f"it describes connection `{cached.get('connection')}`, the project uses "
                   f"`{live.get('connection')}`")
    ct, lt = cached.get("tables") or {}, live.get("tables") or {}
    for t in lt:
        if t not in ct:
            out.append(f"table `{t}` is missing")
            continue
        added = [c for c in lt[t] if c not in ct[t]]
        removed = [c for c in ct[t] if c not in lt[t]]
        changed = [c for c in lt[t] if c in ct[t] and ct[t][c] != lt[t][c]]
        if added:
            out.append(f"table `{t}` has new columns {', '.join(f'`{c}`' for c in added)}")
        if removed:
            out.append(f"table `{t}` no longer has {', '.join(f'`{c}`' for c in removed)}")
        if changed:
            out.append(f"table `{t}` changed type of {', '.join(f'`{c}`' for c in changed)}")
    for t in ct:
        if t not in lt:
            out.append(f"table `{t}` no longer exists")
    return out
