# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Line counts for example dashboards and models (CLAUDE.md: "measure the format").

    uv run scripts/format_stats.py

Growth in lines per widget without new capability is a regression.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WIDGETS = {"kpi", "chart", "table", "text", "button"}
CONTAINERS = {"row", "grid", "tabs", "tab", "card", "section"}


def walk(items, counts):
    for node in items or []:
        kind = next(iter(node))
        counts[kind in WIDGETS and "widgets" or kind in CONTAINERS and "containers" or "other"] += 1
        walk(node.get("items"), counts)


def line_stats(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    content = [l for l in lines if l.strip() and not l.lstrip().startswith("#")]
    return len(lines), len(content), max(map(len, lines))


def main():
    print(f"{'artifact':<56} {'lines':>5} {'content':>7} {'pages':>5} {'widgets':>7} "
          f"{'contain.':>8} {'lines/widget':>12} {'max col':>7}")
    for path in sorted(ROOT.glob("examples/*/dashboards/*/dashboard.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        counts = {"widgets": 0, "containers": 0, "other": 0}
        for page in doc["pages"]:
            walk(page.get("items"), counts)
        total, content, width = line_stats(path)
        print(f"{path.relative_to(ROOT / 'examples').as_posix():<56} {total:>5} {content:>7} "
              f"{len(doc['pages']):>5} {counts['widgets']:>7} {counts['containers']:>8} "
              f"{total / counts['widgets']:>12.1f} {width:>7}")
    for path in sorted(ROOT.glob("examples/*/model/*.model.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        ents = doc["entities"].values()
        n_meas = sum(len(e.get("measures") or {}) for e in ents)
        n_dims = sum(len(e.get("dimensions") or {}) for e in ents)
        total, content, width = line_stats(path)
        print(f"{path.relative_to(ROOT / 'examples').as_posix():<56} {total:>5} {content:>7}   "
              f"{len(doc['entities'])} entities, {n_meas} measures, {n_dims} dimensions, max col {width}")


if __name__ == "__main__":
    main()
