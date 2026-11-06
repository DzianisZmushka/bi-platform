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


def test_y_key_stays_plain():
    """ADR-002 §1a: `y` is a deliberate exception to the YAML 1.1 boolean-key ban.
    Do not change this test to expect `"y":`; read the ADR first."""
    src = "dashboard: demo\nmodel: sales\npages:\n  - page: p\n    items:\n" \
          "      - chart: c\n        mark: bar\n        x: region\n        y: net_revenue\n"
    out = canon(src)
    assert "        y: net_revenue\n" in out and '"y"' not in out
    # Values are not exempt: a string value `y` must still be quoted.
    assert canon(src.replace("x: region", "title: y")).count('title: "y"') == 1


def test_comments_on_list_valued_join_move_with_it():
    """CLAUDE.md: a new property shape (a join on several columns) ships with a comment test."""
    src = """\
model: m
entities:
  returns:
    table: returns
    key: return_id
    joins:
      # composite key, not supported yet
      order_lines: [order_id, line_no]  # VBELN + POSNR style
      calendar: return_date
"""
    out = canon(src, "model")
    assert "    joins:\n      # composite key, not supported yet\n" \
           "      order_lines: [order_id, line_no]  # VBELN + POSNR style\n" in out


@pytest.mark.parametrize("before, line", [
    ("currency:\n", "currency:  # c\n"),
    ("  target: USD\n", "  target: USD  # c\n"),
    ("  rates: { table: tcurr, currency: FCURR, month: GDATU, rate: UKURS }\n",
     "  rates: { table: tcurr, currency: FCURR, month: GDATU, rate: UKURS }  # c\n"),
    ("    currency: { code: document_currency, date: order_date }\n",
     "    currency: { code: document_currency, date: order_date }  # c\n"),
    ('      product_line: { lookup: t179t.VTEXT, key: PRODH, match: "left(PRDHA, 5)" }\n',
     '      product_line: { lookup: t179t.VTEXT, key: PRODH, match: "left(PRDHA, 5)" }  # c\n'),
    ("      billed_quantity: { sum: FKIMG, from: billing_items }\n",
     "      billed_quantity: { sum: FKIMG, from: billing_items }  # c\n"),
    ("      billed_by_order_date_usd: { sum: NETWR, date: order_date, convert: USD, format: currency }\n",
     "      billed_by_order_date_usd: { sum: NETWR, date: order_date, convert: USD, format: currency }  # c\n"),
    ("      order_value_usd: { sum: NETWR, convert: USD, format: currency }\n",
     "      order_value_usd: { sum: NETWR, convert: USD, format: currency }  # c\n"),
    ("        fiscal: { year: FISCAL_YEAR, period: FISCAL_PERIOD }\n",
     "        fiscal: { year: FISCAL_YEAR, period: FISCAL_PERIOD }  # c\n"),
])
def test_comments_on_erp_properties(before, line):
    """CLAUDE.md: each new property (currency, rates, entity currency, expression join, convert,
    fiscal) keeps a full-line comment above it and a trailing comment on its line."""
    path = ROOT / "examples/erp-sales/model/erp.model.yaml"
    text = path.read_text(encoding="utf-8")
    assert before in text
    indent = before[:len(before) - len(before.lstrip())]
    commented = text.replace(before, f"{indent}# above\n{line}", 1)  # first occurrence
    out = canon(commented, "model")
    assert f"{indent}# above\n{line}" in out


def test_nested_mapping_keeps_short_lists_inline():
    """ADR-003 rule 5, amended: a nested mapping whose values are scalars or short lists of
    scalars stays on one line; a top-level section never does; a comment inside forces block."""
    src = """\
model: m
entities:
  e:
    table: t
    key: [a, b]
    joins:
      x: k
      y: [a, b]
    measures:
      m1: { count: a }
hierarchies:
  h: [a, b]
"""
    out = canon(src, "model")
    assert "    joins: { x: k, y: [a, b] }\n" in out
    assert "hierarchies:\n  h: [a, b]\n" in out
    commented = src.replace("      y: [a, b]\n", "      y: [a,  # first\n          b]\n")
    assert "      y:\n        - a  # first\n        - b\n" in canon(commented, "model")
