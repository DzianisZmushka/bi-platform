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

# Every region and sales org: these tests are about the write rules, not about access (test_access_*.py).
ALL_ROWS = {"regions": ["emea", "na", "apac", "latam"], "sales_orgs": ["DE01", "PL01", "AE01", "US01", "US02"]}
EXAMPLES = Path(os.environ["OBI_EXAMPLES"])
BOB = {"name": "bob", "roles": ["sales_rep"], **ALL_ROWS}
MIA = {"name": "mia", "roles": ["sales_manager"], **ALL_ROWS}
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
    assert post(erp, MIA, _form="shipment-plan", _action="sheet_delete", _key=PLAN_KEY) == [
        "`delete` is not allowed on sheet `shipment-plan` (allow: add, edit, import, paste)"]
    assert snapshot(erp, "shipment_plans") == before


@refuses("model:entity.key")
def test_sheet_key_column_cannot_change(erp):
    before = snapshot(erp, "shipment_plans")
    assert post(erp, MIA, _form="shipment-plan", _action="sheet_save", _key=PLAN_KEY, VKORG="PL01",
                week_start="2026-04-06", planned_value_usd="5") == [
        "VKORG: `VKORG` is a key column and cannot change; delete the row and add a new one"]
    assert snapshot(erp, "shipment_plans") == before
    # Unchanged keys, as the page sends them, are fine.
    assert post(erp, MIA, _form="shipment-plan", _action="sheet_save", _key=PLAN_KEY, VKORG="DE01",
                week_start="2026-04-06", planned_value_usd="5") is None


@refuses("form:sheet.readonly")
def test_sheet_readonly_column(erp):
    forms = with_change(erp, "shipment-plan", lambda s: s.update(readonly=["planned_value_usd"]))
    before = snapshot(erp, "shipment_plans")
    assert post(erp, MIA, forms, _form="shipment-plan", _action="sheet_save", _key=PLAN_KEY,
                planned_value_usd="1") == ["planned_value_usd: `planned_value_usd` is read-only on this sheet"]
    assert post(erp, MIA, forms, _form="shipment-plan", _action="sheet_add", VKORG="DE01",
                week_start="2027-01-04", planned_value_usd="1") == [
        "planned_value_usd: `planned_value_usd` is read-only on this sheet; new rows take its default"]
    assert snapshot(erp, "shipment_plans") == before


@refuses("form:sheet.columns")
def test_sheet_column_it_does_not_show(erp):
    before = snapshot(erp, "shipment_plans")
    assert post(erp, MIA, _form="shipment-plan", _action="sheet_save", _key=PLAN_KEY, planned_value_usd="1",
                updated_by="someone else") == [
        "updated_by: `updated_by` is not a field of sheet `shipment-plan`"]
    assert post(erp, MIA, _form="shipment-plan", _action="sheet_import",
                _rows="VKORG,week_start,planned_value_usd,created_by\nDE01,2027-01-04,1,eve\n") == [
        "row 1: created_by: `created_by` is not a field of sheet `shipment-plan`"]
    assert post(erp, MIA, _form="shipment-plan", _action="sheet_paste", _rows="DE01\t2027-01-04\t1\teve\n") == [
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
    assert post(demo, BOB, _form="kpi-comment", _action="save", kpi="profit", month="someday", region_id="emea",
                comment="") == {
        "month": ["`someday` is not a valid date"],
        "kpi": ["must be one of revenue, attainment, margin, returns"],
        "comment": ["is required"]}
    assert post(demo, BOB, _form="kpi-comment", _action="save", kpi="revenue", month="2026-01-01", region_id="emea",
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
        "status": ["`approve` is not allowed for bob (`${user.roles.includes('sales_manager')}`)"]}
    assert snapshot(erp, "order_notes")[0]["status"] == "submitted"
    assert post(erp, MIA, _form="order-note", _action="transition:approve", **key, note="x") is None


# --------------------------------------------------------------------------- access (ADR-013)

REP_EMEA = {"name": "rob", "roles": ["sales_rep"], "regions": ["emea"]}
MANAGER_EMEA = {"name": "mia", "roles": ["sales_manager"], "regions": ["emea"]}


@refuses("model:entity.access", "model:row_access.where")
def test_write_outside_the_users_rows(demo):
    """Editing a hidden row, adding one outside the user's rows, or moving one out: all refused."""
    before = snapshot(demo, "targets")
    sheet = dict(_form="targets", revenue_target="1")
    assert post(demo, MANAGER_EMEA, **sheet, _action="sheet_save", _key='{"month": "2026-01-01", "region_id": "na"}') \
        == ["the row does not exist; reload the page"]
    assert post(demo, MANAGER_EMEA, **sheet, _action="sheet_delete", _key='{"month": "2026-01-01", "region_id": "na"}') \
        == ["the row does not exist; reload the page"]
    assert snapshot(demo, "targets") == before
    # A display value outside the user's rows is not even a known choice.
    assert post(demo, MANAGER_EMEA, **sheet, _action="sheet_add", month="2027-01-01", region_id="na") == [
        "region_id: `na` is not a known region"]
    # Below the page: the store refuses a row outside the user's rows on its own.
    from obi_engine.store import WriteError
    w = Writer(Project(demo), MANAGER_EMEA)
    w._references = lambda *a: None  # skip the choice check, which would refuse first
    with pytest.raises(WriteError) as e:
        w.save("targets", {"month": "2027-01-01", "region_id": "na", "revenue_target": "1"}, via_form="targets")
    assert e.value.errors == {"": ["this row is outside the rows mia may write (`regions`: `region_code in user.regions`)"]}
    assert snapshot(demo, "targets") == before


@refuses("access:role.edit")
def test_form_without_edit(demo):
    viewer = {"name": "vic", "roles": ["viewer"], "regions": ["emea"]}
    assert post(demo, viewer, _form="kpi-comment", _action="save", kpi="revenue", month="2026-01-01",
                comment="x") == {"": ["vic may not edit form `kpi-comment`; access.yaml gives `edit` on it to none of "
                                      "their roles"]}
    assert post(demo, REP_EMEA, _form="targets", _action="sheet_add", month="2027-01-01", region_id="emea",
                revenue_target="1") == [
        "rob may not edit sheet `targets`; access.yaml gives `edit` on it to none of their roles"]
    assert snapshot(demo, "kpi_comments") == []


@refuses("access:role.write")
def test_write_without_a_form_needs_write(demo):
    """Writes that bypass forms (the store, later the API) need `write`, or `edit` on a form of the entity."""
    from obi_engine.store import WriteError
    with pytest.raises(WriteError) as e:
        Writer(Project(demo), REP_EMEA).save("targets", {"month": "2027-01-01", "region_id": "emea",
                                                        "revenue_target": "1"})
    assert e.value.errors == {"": ["rob may not write `targets`; access.yaml grants it to no role of theirs"]}
    access = (demo / "access.yaml").read_text(encoding="utf-8")
    (demo / "access.yaml").write_text(access.replace("    edit: { forms: [kpi-comment] }",
                                                     "    edit: { forms: [kpi-comment] }\n    write: [targets]"),
                                      encoding="utf-8")
    Writer(Project(demo), REP_EMEA).save("targets", {"month": "2027-01-01", "region_id": "emea", "revenue_target": "1"})


@refuses("access:role.deny")
def test_deny_wins_over_another_role(erp):
    both = {"name": "aud", "roles": ["sales_manager", "auditor"], "sales_orgs": ALL_ROWS["sales_orgs"]}
    assert post(erp, both, _form="order-note", _action="save", note="x", **open_item(erp)) == {
        "": ["aud may not edit form `order-note`; access.yaml gives `edit` on it to none of their roles"]}
    from obi_engine.store import WriteError
    with pytest.raises(WriteError):
        Writer(Project(erp), both).save("order_notes", {**open_item(erp), "note": "x"})
    assert snapshot(erp, "order_notes") == []


@refuses("access:column_rule.write")
def test_column_writable_only_by_listed_roles(demo):
    access = (demo / "access.yaml").read_text(encoding="utf-8")
    (demo / "access.yaml").write_text(access + "  kpi_comments.kpi: { write: [sales_manager] }\n", encoding="utf-8")
    assert post(demo, REP_EMEA, _form="kpi-comment", _action="save", kpi="margin", month="2026-01-01",
                region_id="emea", comment="x") == {"kpi": ["`kpi` may be written only by sales_manager"]}
    assert snapshot(demo, "kpi_comments") == []
    assert post(demo, MANAGER_EMEA, _form="kpi-comment", _action="save", kpi="margin", month="2026-01-01",
                region_id="emea", comment="x") is None
    # The region is required: access reaches comments through it (ADR-013).
    assert post(demo, MANAGER_EMEA, _form="kpi-comment", _action="save", kpi="margin", month="2026-01-01",
                comment="x") == {"region_id": ["is required"]}
