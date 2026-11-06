"""Every write restriction holds on the server; the page only reflects it (CLAUDE.md).

Each test sends a request the page would never produce: a hidden action, a disabled field, an
extra column, a changed key. Each one must be refused with a message, and nothing may be written.
`@refuses("<kind>:<definition>.<property>")` names the schema property a test proves.
`test_coverage.py` fails when a property marked `x-write-rule` in a schema has no such test."""

import copy
from datetime import date
import os
import shutil
from pathlib import Path

import pytest

from obi_cli.forms import handle_post, load_forms
from obi_engine import Project, SemanticQuery
from obi_engine.store import Writer, migrate

EXAMPLES = Path(os.environ["OBI_EXAMPLES"])
BOB = {"name": "bob", "role": "sales"}
MIA = {"name": "mia", "role": "sales_manager"}
PLAN_KEY = '{"VKORG": "DE01", "week_start": "2026-04-06"}'


def refuses(*properties):
    def mark(fn):
        fn.refuses = properties
        return fn
    return mark


@pytest.fixture
def erp(tmp_path):
    root = tmp_path / "erp-sales"
    shutil.copytree(EXAMPLES / "erp-sales", root, ignore=shutil.ignore_patterns(".obi"))
    migrate(Project(root))
    return root


@pytest.fixture
def demo(tmp_path):
    root = tmp_path / "sales-demo"
    shutil.copytree(EXAMPLES / "sales-demo", root, ignore=shutil.ignore_patterns(".obi"))
    migrate(Project(root))
    return root


def post(root, user, forms=None, **fields):
    """One POST as the server receives it. Returns the errors, or None when it was written."""
    forms = forms or load_forms(root)
    nxt, state = handle_post(Project(root), user, forms, {k: [v] for k, v in fields.items()})
    if state is None:
        return None
    return next(iter(state.values()))["errors"]


def snapshot(root, entity) -> list[dict]:
    return Writer(Project(root), BOB).rows(entity)


def open_item(root) -> dict:
    row = Project(root).engine("erp").query(SemanticQuery(["open_order_value_usd"], ["sales_order", "item"],
                                                          sort=["-open_order_value_usd"], limit=1)).to_pylist()[0]
    return {"VBELN": row["sales_order"], "POSNR": row["item"]}


def with_change(root, fid, change) -> dict:
    forms = copy.deepcopy(load_forms(root))
    change(forms[fid])
    return forms


# --------------------------------------------------------------------------- sheets


@refuses("form:sheet.allow")
def test_action_outside_allow(erp):
    before = snapshot(erp, "shipment_plans")
    assert post(erp, BOB, _form="shipment-plan", _action="sheet_delete", _key=PLAN_KEY) == [
        "`delete` is not allowed on sheet `shipment-plan` (allow: add, edit, import, paste)"]
    assert snapshot(erp, "shipment_plans") == before


@refuses("model:entity.key")
def test_sheet_key_column_cannot_change(erp):
    before = snapshot(erp, "shipment_plans")
    assert post(erp, BOB, _form="shipment-plan", _action="sheet_save", _key=PLAN_KEY, VKORG="PL01",
                week_start="2026-04-06", planned_value_usd="5") == [
        "VKORG: `VKORG` is a key column and cannot change; delete the row and add a new one"]
    assert snapshot(erp, "shipment_plans") == before
    # Unchanged keys, as the page sends them, are fine.
    assert post(erp, BOB, _form="shipment-plan", _action="sheet_save", _key=PLAN_KEY, VKORG="DE01",
                week_start="2026-04-06", planned_value_usd="5") is None


@refuses("form:sheet.readonly")
def test_sheet_readonly_column(erp):
    forms = with_change(erp, "shipment-plan", lambda s: s.update(readonly=["planned_value_usd"]))
    before = snapshot(erp, "shipment_plans")
    assert post(erp, BOB, forms, _form="shipment-plan", _action="sheet_save", _key=PLAN_KEY,
                planned_value_usd="1") == ["planned_value_usd: `planned_value_usd` is read-only on this sheet"]
    assert post(erp, BOB, forms, _form="shipment-plan", _action="sheet_add", VKORG="DE01",
                week_start="2027-01-04", planned_value_usd="1") == [
        "planned_value_usd: `planned_value_usd` is read-only on this sheet; new rows take its default"]
    assert snapshot(erp, "shipment_plans") == before


@refuses("form:sheet.columns")
def test_sheet_column_it_does_not_show(erp):
    before = snapshot(erp, "shipment_plans")
    assert post(erp, BOB, _form="shipment-plan", _action="sheet_save", _key=PLAN_KEY, planned_value_usd="1",
                updated_by="someone else") == [
        "updated_by: `updated_by` is not a field of sheet `shipment-plan`"]
    assert post(erp, BOB, _form="shipment-plan", _action="sheet_import",
                _rows="VKORG,week_start,planned_value_usd,created_by\nDE01,2027-01-04,1,eve\n") == [
        "row 1: created_by: `created_by` is not a field of sheet `shipment-plan`"]
    assert post(erp, BOB, _form="shipment-plan", _action="sheet_paste", _rows="DE01\t2027-01-04\t1\teve\n") == [
        "row 1 has 4 values; expected 3 (VKORG, week_start, planned_value_usd)"]
    assert snapshot(erp, "shipment_plans") == before


# --------------------------------------------------------------------------- forms


@refuses("form:form.fields")
def test_form_value_for_a_column_it_does_not_show(demo):
    assert post(demo, BOB, _form="kpi-comment", _action="save", kpi="revenue", month="2026-01-01",
                comment="x", created_by="eve") == {"created_by": ["`created_by` is not a field of form `kpi-comment`"]}
    assert snapshot(demo, "kpi_comments") == []


@refuses("model:workflow.column")
def test_status_posted_to_a_form(erp):
    key = open_item(erp)
    assert post(erp, BOB, _form="order-note", _action="save", **key, note="x", status="approved") == {
        "status": ["`status` changes only through a transition"]}
    assert snapshot(erp, "order_notes") == []


@refuses("form:field.required")
def test_form_required_field(erp):
    forms = with_change(erp, "order-note", lambda f: f["fields"][3].update(required=True))  # next_step
    assert post(erp, BOB, forms, _form="order-note", _action="save", **open_item(erp), note="x") == {
        "next_step": ["is required"]}
    assert snapshot(erp, "order_notes") == []


@refuses("form:field.readonly")
def test_form_readonly_field(demo, erp):
    forms = with_change(demo, "kpi-comment", lambda f: f["fields"][0].update(readonly=True))  # kpi: revenue
    assert post(demo, BOB, forms, _form="kpi-comment", _action="save", kpi="margin", month="2026-01-01",
                comment="x") == {"kpi": ["`kpi` is read-only on this form"]}
    assert snapshot(demo, "kpi_comments") == []
    # Editing: a read-only field keeps the stored value.
    key = open_item(erp)
    assert post(erp, BOB, _form="order-note", _action="save", **key, note="first") is None
    forms = with_change(erp, "order-note", lambda f: f["fields"][2].update(readonly=True))  # note
    assert post(erp, BOB, forms, _form="order-note", _action="save", **key, note="changed") == {
        "note": ["`note` is read-only on this form"]}
    assert snapshot(erp, "order_notes")[0]["note"] == "first"


# --------------------------------------------------------------------------- column rules


@refuses("model:column.type", "model:column.required", "model:column.choices", "model:column.check")
def test_column_rules(demo):
    assert post(demo, BOB, _form="kpi-comment", _action="save", kpi="profit", month="someday", comment="") == {
        "month": ["`someday` is not a valid date"],
        "kpi": ["must be one of revenue, attainment, margin, returns"],
        "comment": ["is required"]}
    assert post(demo, BOB, _form="kpi-comment", _action="save", kpi="revenue", month="2026-01-01",
                comment="x" * 501) == {"comment": ["fails the rule `length(comment) <= 500`"]}
    assert snapshot(demo, "kpi_comments") == []


@refuses("model:column.references")
def test_column_references(demo):
    assert post(demo, BOB, _form="kpi-comment", _action="save", kpi="revenue", region_id="Atlantis",
                month="2026-01-01", comment="x") == {"region_id": ["`Atlantis` is not a known region"]}
    assert snapshot(demo, "kpi_comments") == []


@refuses("model:entity.per")
def test_per_parent_must_exist(erp):
    assert post(erp, BOB, _form="order-note", _action="save", VBELN="9999999999", POSNR="000010", note="x") == {
        "": ["there is no order_items row with VBELN = 9999999999, POSNR = 000010"]}
    assert snapshot(erp, "order_notes") == []


# --------------------------------------------------------------------------- workflow


@refuses("model:transition.by", "model:transition.from")
def test_transition_rules(erp):
    key = open_item(erp)
    assert post(erp, BOB, _form="order-note", _action="save", **key, note="x") is None
    assert post(erp, BOB, _form="order-note", _action="transition:approve", **key, note="x") == {
        "status": ["`approve` needs status submitted; it is `draft`"]}
    assert post(erp, BOB, _form="order-note", _action="transition:submit", **key, note="x") is None
    assert post(erp, BOB, _form="order-note", _action="transition:approve", **key, note="x") == {
        "status": ["`approve` is not allowed for bob (`${user.role == 'sales_manager'}`)"]}
    assert snapshot(erp, "order_notes")[0]["status"] == "submitted"
    assert post(erp, MIA, _form="order-note", _action="transition:approve", **key, note="x") is None
