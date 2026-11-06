"""Forms in `obi serve` (ADR-012): create vs edit mode, transition buttons, the POST path, and
sheets. Each test works on its own copy of an example with a fresh database."""

from datetime import date
import html as html_lib
import os
import re
import shutil
from pathlib import Path

import pytest

from obi_cli.forms import handle_post, load_forms
from obi_cli.render import render_dashboard
from obi_engine import Project, SemanticQuery
from obi_engine.store import Writer, migrate

EXAMPLES = Path(os.environ["OBI_EXAMPLES"])
AS_OF = date(2026, 11, 5)
BOB = {"name": "bob", "role": "sales"}
MIA = {"name": "mia", "role": "sales_manager"}


def fresh(tmp_path, name) -> Path:
    root = tmp_path / name
    shutil.copytree(EXAMPLES / name, root, ignore=shutil.ignore_patterns(".obi"))
    migrate(Project(root))
    return root


@pytest.fixture
def erp(tmp_path):
    root = fresh(tmp_path, "erp-sales")
    item = Project(root).engine("erp").query(SemanticQuery(["open_order_value_usd"], ["sales_order", "item"],
                                                           sort=["-open_order_value_usd"], limit=1)).to_pylist()[0]
    return root, {"order": [item["sales_order"]], "order_item": [item["item"]]}


def post(root, user, **fields):
    return handle_post(Project(root), user, load_forms(root), {k: [v] for k, v in fields.items()})


def note_page(root, inputs, user) -> str:
    return render_dashboard(root, "order-to-cash", inputs, AS_OF, mode="serve", user=user,
                            here="/order-to-cash").html


def test_form_creates_until_the_row_exists_then_edits_it_with_transition_buttons(erp):
    root, inputs = erp
    html = note_page(root, inputs, BOB)
    assert "Editing the existing record" not in html and 'value="transition:' not in html
    after = html_lib.unescape(re.search(r'name="_next" value="([^"]*)"', html).group(1))
    assert after.endswith("#open_orders")  # on_submit: { navigate: open_orders }
    key = {"VBELN": inputs["order"][0], "POSNR": inputs["order_item"][0]}
    nxt, state = post(root, BOB, _form="order-note", _action="save", _return="/order-to-cash", _next=after, **key,
                      note="Call the customer", next_step="call customer")
    assert state is None and nxt == after
    html = note_page(root, inputs, BOB)
    assert "Editing the existing record · status <b>draft</b>, last changed by bob" in html
    assert 'value="transition:submit"' in html and 'value="transition:approve"' not in html
    assert "Call the customer" in html


def test_submit_and_approve_through_the_form(erp):
    root, inputs = erp
    key = {"VBELN": inputs["order"][0], "POSNR": inputs["order_item"][0]}
    post(root, BOB, _form="order-note", _action="save", **key, note="Check stock")
    assert post(root, BOB, _form="order-note", _action="transition:submit", **key, note="Check stock")[1] is None
    _, state = post(root, BOB, _form="order-note", _action="transition:approve", **key, note="Check stock")
    assert state["order-note"]["errors"] == {
        "status": ["`approve` is not allowed for bob (`${user.role == 'sales_manager'}`)"]}
    html = note_page(root, inputs, MIA)
    assert 'value="transition:approve"' in html and 'value="transition:reopen"' in html
    post(root, MIA, _form="order-note", _action="transition:approve", **key, note="Check stock")
    assert Writer(Project(root), MIA).get("order_notes", key)["status"] == "approved"


def test_form_errors_re_render_with_the_entered_values(erp):
    root, inputs = erp
    key = {"VBELN": inputs["order"][0], "POSNR": inputs["order_item"][0]}
    nxt, state = post(root, BOB, _form="order-note", _action="save", **key, note="", next_step="dance")
    assert nxt is None
    assert state["order-note"]["errors"] == {"note": ["is required"],
                                             "next_step": ["must be one of call customer, check stock, "
                                                           "wait for payment, cancel item"]}
    html = render_dashboard(root, "order-to-cash", inputs, AS_OF, mode="serve", user=BOB, form_state=state).html
    assert '<span class="form-error">is required</span>' in html


def test_comment_appears_next_to_the_kpi(tmp_path):
    root = fresh(tmp_path, "sales-demo")
    html = render_dashboard(root, "commercial-review", None, AS_OF, mode="serve").html
    assert "No comment yet for this period." in html
    _, state = post(root, {"name": "alice"}, _form="kpi-comment", _action="save", kpi="revenue", region_id="EMEA",
                    month="2026-01-01", comment="Strong January in EMEA")
    assert state is None
    html = render_dashboard(root, "commercial-review", {"period": ["2026-01-01..2026-12-31"]}, AS_OF,
                            mode="serve").html
    assert "Strong January in EMEA" in html and "— alice," in html


def test_sheet_paste_saves_nothing_when_one_row_fails(tmp_path):
    root = fresh(tmp_path, "sales-demo")
    w = Writer(Project(root), BOB)
    before = len(w.rows("targets"))
    _, state = post(root, BOB, _form="targets", _action="sheet_paste",
                    _rows="2027-01-01\temea\t100\n2027-01-15\tapac\t200\n")
    assert state["targets"]["errors"] == ["row 2: month: fails the rule `day(month) = 1`"]
    assert len(w.rows("targets")) == before
    _, state = post(root, BOB, _form="targets", _action="sheet_paste",
                    _rows="2027-01-01\tEMEA\t100\n2027-02-01\tapac\t200\n")
    assert state is None and len(w.rows("targets")) == before + 2
    assert w.get("targets", {"month": date(2027, 1, 1), "region_id": "emea"})["revenue_target"] == 100


def test_sheet_import_csv_by_header(tmp_path):
    root = fresh(tmp_path, "sales-demo")
    _, state = post(root, BOB, _form="targets", _action="sheet_import",
                    _rows="region_id,month,revenue_target\napac,2027-03-01,5\n")
    assert state is None
    assert Writer(Project(root), BOB).get("targets", {"month": date(2027, 3, 1), "region_id": "apac"})


def sheet_weeks(root, query) -> tuple[list[str], list[str]]:
    from obi_cli.render import render_form_page
    html = render_form_page(root, "shipment-plan", BOB, "/forms/shipment-plan", None, query, "", AS_OF)
    weeks = sorted(set(re.findall(r'name="week_start"[^>]*value="(\d{4}-\d\d-\d\d)"', html)))
    return weeks, re.findall(r'class="form-error">(.*?)<', html)


def test_sheet_date_filter_resolves_fiscal_presets_from_the_calendar(tmp_path):
    """`default: fiscal_year_to_date` reads the fiscal year (from 1 April) from the calendar the
    column joins, at --as-of; it is never silently dropped."""
    root = fresh(tmp_path, "erp-sales")
    weeks, errors = sheet_weeks(root, {})
    assert errors == [] and weeks[0] == "2026-04-06" and weeks[-1] == "2026-11-02"
    weeks, _ = sheet_weeks(root, {"sf.shipment-plan.period": ["last_fiscal_year"]})
    assert weeks == ["2026-03-30"]
    weeks, errors = sheet_weeks(root, {"sf.shipment-plan.period": ["soon"]})
    assert errors == ["filter `period`: `soon` is not a date, a date range (`2026-01-01..2026-03-31`) or a date preset"]
    assert weeks == []  # an unusable filter shows nothing, never everything


def test_dashboard_filter_that_cannot_be_evaluated_is_an_error_not_unfiltered(tmp_path):
    root = fresh(tmp_path, "sales-demo")
    result = render_dashboard(root, "commercial-review", {"period": ["soon"]}, AS_OF, mode="serve")
    assert result.errors == [
        "filter `period`: `soon` is not a date, a date range (`2026-01-01..2026-03-31`) or a date preset"]
    boxes = re.findall(r'<div class="widget-error">(.*?)</div>', result.html)
    assert boxes and all("filter `period` cannot be evaluated" in html_lib.unescape(b) for b in boxes)
    assert 'class="kpi-value"' not in result.html  # no KPI numbers that silently drop the period
