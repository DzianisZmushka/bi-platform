"""Zero-diff round trips (ADR-003) and comment preservation (ADR-006)."""

import json
from pathlib import Path

import pytest

from obi_formats.artifacts import canonicalize, dump, parse
from obi_formats.schema import artifact_type

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = sorted(p for p in (ROOT / "examples").rglob("*.yaml") if artifact_type(p))


def test_examples_exist():
    assert len(EXAMPLES) >= 4


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_example_is_canonical(path):
    text = path.read_text(encoding="utf-8")
    assert canonicalize(text, artifact_type(path)) == text


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_builder_save_through_json_is_zero_diff(path):
    """A builder works on the JSON form: YAML -> JSON -> YAML must be byte-identical."""
    text = path.read_text(encoding="utf-8")
    doc = parse(text)
    as_json = json.loads(json.dumps(doc.data))
    assert dump(as_json, artifact_type(path), doc.comments, doc.footer) == text


def canon(src: str, kind: str = "dashboard") -> str:
    out = canonicalize(src, kind)
    assert canonicalize(out, kind) == out, "canonical form must be stable"
    return out


def test_comment_moves_with_key_when_keys_are_reordered():
    src = """\
dashboard: demo
model: sales
pages:
  - page: p
    items:
      - chart: c
        # the measure
        y: net_revenue  # eol on y
        mark: line  # eol on mark
"""
    assert canon(src) == """\
dashboard: demo
model: sales

pages:
  - page: p
    items:
      - chart: c
        mark: line  # eol on mark
        # the measure
        y: net_revenue  # eol on y
"""


def test_comments_inside_flow_sequences_force_block_style():
    src = """\
dashboard: demo
model: sales
pages:
  - page: p
    items:
      - grid: g
        columns: [2,   # wide
                  1]   # narrow
        items:
          - table: t
            values: [
              # money first
              net_revenue,
              orders]
"""
    assert canon(src) == """\
dashboard: demo
model: sales

pages:
  - page: p
    items:
      - grid: g
        columns:
          - 2  # wide
          - 1  # narrow
        items:
          - table: t
            values:
              # money first
              - net_revenue
              - orders
"""


def test_trailing_comment_after_single_line_flow_stays_on_the_key():
    src = "dashboard: demo\nmodel: sales\npages:\n  - page: p\n    items:\n" \
          "      - table: t\n        values: [net_revenue, orders]  # two columns\n"
    out = canon(src)
    assert "        values: [net_revenue, orders]  # two columns\n" in out


def test_header_footer_and_item_comments():
    src = """\
# header
dashboard: demo
model: sales
pages:
  # first page
  - page: p  # eol on item
    items:
      - text: t
        content: hi
# footer
"""
    assert canon(src) == """\
# header
dashboard: demo
model: sales

pages:
  # first page
  - page: p  # eol on item
    items:
      - text: t
        content: hi
# footer
"""


def test_hash_inside_strings_and_literal_blocks_is_not_a_comment():
    src = """\
dashboard: demo
model: sales
pages:
  - page: p
    title: "Item #1 # not a comment"
    items:
      - text: t
        content: |  # a real comment after the header
          # literal text, not a comment
          C# and F# stay
"""
    out = canon(src)
    assert '    title: "Item #1 # not a comment"\n' in out
    assert "        content: |  # a real comment after the header\n" in out
    assert "          # literal text, not a comment\n          C# and F# stay\n" in out
    assert parse(out).data["pages"][0]["items"][0]["content"] == \
        "# literal text, not a comment\nC# and F# stay\n"


def test_comment_follows_element_moved_by_a_builder():
    src = """\
dashboard: demo
model: sales
pages:
  - page: p
    items:
      # revenue first
      - kpi: revenue
        measure: net_revenue  # main number
      - kpi: orders
        measure: orders
"""
    doc = parse(src)
    items = doc.data["pages"][0]["items"]
    items.reverse()  # the builder moves `orders` above `revenue`
    assert dump(doc.data, "dashboard", doc.comments, doc.footer) == """\
dashboard: demo
model: sales

pages:
  - page: p
    items:
      - kpi: orders
        measure: orders
      # revenue first
      - kpi: revenue
        measure: net_revenue  # main number
"""


def test_comment_of_deleted_element_disappears():
    src = "dashboard: demo\nmodel: sales\npages:\n  - page: p\n    items:\n" \
          "      # gone soon\n      - text: a\n        content: x\n      - text: b\n        content: y\n"
    doc = parse(src)
    del doc.data["pages"][0]["items"][0]
    assert "gone soon" not in dump(doc.data, "dashboard", doc.comments, doc.footer)


def test_defaults_are_dropped_and_all_default_entries_become_empty():
    model = """\
model: m
entities:
  facts:
    table: facts
    key: id
    dimensions:
      channel: { type: string }
      kind: { label: Kind, type: string }
    measures:
      total: { sum: amount, format: number }
"""
    assert canon(model, "model") == """\
model: m

entities:
  facts:
    table: facts
    key: id
    dimensions:
      channel:
      kind: { label: Kind }
    measures:
      total: { sum: amount }
"""


def test_quoting_rules():
    src = """\
dashboard: demo
model: sales
filters:
  - filter: f
    dimension: channel
    default: "yes"
  - filter: g
    dimension: date
    default: "2026-01-31"
pages:
  - page: p
    items:
      - table: t
        values: [net_revenue]
        sort: -net_revenue
        cells:
          net_revenue: { color: "${value < 0 ? 'danger' : 'ok'}" }
"""
    out = canon(src)
    assert '    default: "yes"\n' in out          # YAML 1.1 boolean
    assert '    default: "2026-01-31"\n' in out   # date-like text stays a string
    assert "        sort: -net_revenue\n" in out    # plain is safe
    assert "net_revenue: { color: \"${value < 0 ? 'danger' : 'ok'}\" }" in out


def test_flow_style_switches_to_block_when_the_line_is_too_wide():
    many = ", ".join(f"measure_number_{i}" for i in range(8))
    src = f"dashboard: demo\nmodel: sales\npages:\n  - page: p\n    items:\n" \
          f"      - table: t\n        values: [{many}]\n"
    out = canon(src)
    assert "        values:\n          - measure_number_0\n" in out
    assert max(len(line) for line in out.splitlines()) <= 100
