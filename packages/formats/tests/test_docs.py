"""Every YAML snippet in docs/formats/*.md is in canonical form, so the docs never teach a form that
`obi-formats fmt` would rewrite (LLM test 5 copied an inline `deny:` from access.md).

A snippet is either a whole artifact (it starts with its kind key) or a fragment. A fragment is
canonicalized inside the smallest artifact that holds it, then cut out again."""

import re
from pathlib import Path

import pytest

from obi_formats.artifacts import canonicalize

ROOT = Path(__file__).resolve().parents[3]
DOCS = sorted((ROOT / "docs" / "formats").glob("*.md"))
KINDS = {"access": "access", "model": "model", "dashboard": "dashboard", "form": "form", "sheet": "form",
         "project": "project"}


def snippets():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for n, m in enumerate(re.finditer(r"^```yaml\n(.*?)^```", text, re.S | re.M), 1):
            line = text[:m.start()].count("\n") + 2
            yield pytest.param(m.group(1), id=f"{doc.name}:{line}")


def wrapper(snippet: str) -> tuple[str, str, int] | None:
    """(kind, the lines that hold the fragment, its indent), or None when no wrapper fits."""
    first = snippet.splitlines()[0]
    key = first.split(":", 1)[0]
    if key in KINDS:
        return KINDS[key], "", 0
    model, dashboard = "model: m\n\nentities:\n", "dashboard: d\nmodel: m\n\npages:\n  - page: p\n    items:\n"
    if first.startswith("- "):  # dashboard items
        return "dashboard", dashboard, 6
    if key == "workflow":
        return "model", model + "  e:\n", 4
    if re.search(r"\{ from: .*, to: ", first):  # a workflow transition
        return "model", model + "  e:\n    workflow:\n      transitions:\n", 8
    if re.match(r"\s+table: ", snippet.splitlines()[1] if "\n" in snippet.strip() else ""):  # an entity
        return "model", model, 2
    if key == "color":  # a widget property
        return "dashboard", dashboard + "      - kpi: k\n", 8
    return None


@pytest.mark.parametrize("snippet", list(snippets()))
def test_docs_snippets_are_canonical(snippet):
    found = wrapper(snippet)
    assert found, f"no wrapper for a snippet starting `{snippet.splitlines()[0]}`; add one to wrapper() in test_docs.py"
    kind, head, indent = found
    pad = " " * indent
    body = "".join(pad + ln if ln.strip() else ln for ln in snippet.splitlines(keepends=True))
    out = canonicalize(head + body, kind)
    assert out.startswith(head), out
    got = "".join(ln[indent:] if ln.strip() else ln for ln in out[len(head):].splitlines(keepends=True))
    assert got == snippet


def test_a_non_canonical_snippet_is_caught():
    bad = "access: p\nroles:\n  auditor:\n    deny: { edit: { forms: [\"*\"] }, write: [\"*\"] }\n"
    kind, head, _ = wrapper(bad)
    assert canonicalize(head + bad, kind) != bad
