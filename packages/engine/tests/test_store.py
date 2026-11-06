"""Writable entities (ADR-012): migrate, the write rules, `per`, workflow and history.

Each test gets its own copy of an example project with a fresh database, so writes here never
reach other tests or the examples."""

from datetime import date
import os
import shutil
from pathlib import Path

import pytest

from obi_engine import Project, SemanticQuery
from obi_engine.store import WriteError, Writer, auto_migrate, migrate, plan

EXAMPLES = Path(os.environ["OBI_EXAMPLES"])
MANAGER = {"name": "mia", "role": "sales_manager"}
SALES = {"name": "bob", "role": "sales"}


def fresh(tmp_path, name) -> Project:
    shutil.copytree(EXAMPLES / name, tmp_path / name, ignore=shutil.ignore_patterns(".obi"))
    project = Project(tmp_path / name)
    migrate(project)
    return project


@pytest.fixture
def erp(tmp_path):
    return fresh(tmp_path, "erp-sales")


@pytest.fixture
def demo(tmp_path):
    return fresh(tmp_path, "sales-demo")


def open_items(project) -> list[dict]:
    eng = project.engine("erp")
    return eng.query(SemanticQuery(["open_order_value_usd"], ["sales_order", "item", "note_status", "note"],
                                   sort=["-open_order_value_usd"])).to_pylist()


# --------------------------------------------------------------------------- migrate


def test_migrate_creates_tables_with_audit_and_history_and_is_idempotent(tmp_path):
    shutil.copytree(EXAMPLES / "sales-demo", tmp_path / "p", ignore=shutil.ignore_patterns(".obi"))
    project = Project(tmp_path / "p")
    assert plan(project) == ["create table `targets`", "create table `kpi_comments`"]
    done = migrate(project)
    assert "created table `targets` and `targets_history`" in done
    assert any(d.startswith("seeded `targets` from data/targets.csv") for d in done)
    cols = [r[0] for r in project.con.sql("DESCRIBE store.main.kpi_comments").fetchall()]
    assert cols[-4:] == ["created_by", "created_at", "updated_by", "updated_at"]
    hist = [r[0] for r in project.con.sql("DESCRIBE store.main.kpi_comments_history").fetchall()]
    assert hist[-3:] == ["history_op", "history_at", "history_by"]
    assert plan(project) == [] and migrate(project) == [] and auto_migrate(project) == []


def test_reseed_moves_current_rows_to_history(demo):
    w = Writer(demo, SALES)
    w.save("targets", {"month": "2026-01-01", "region_id": "emea", "revenue_target": "1"})
    done = migrate(demo, reseed=("targets",))
    assert any(d.startswith("seeded `targets`") for d in done)
    assert w.get("targets", {"month": date(2026, 1, 1), "region_id": "emea"})["revenue_target"] != 1
    ops = {r[0] for r in demo.con.sql("SELECT history_op FROM store.main.targets_history").fetchall()}
    assert {"update", "reseed"} <= ops


# --------------------------------------------------------------------------- write rules


def test_write_rules_name_the_column_and_the_rule(demo):
    w = Writer(demo, SALES)
    with pytest.raises(WriteError) as e:
        w.save("targets", {"month": "2026-01-15", "region_id": "Atlantis", "revenue_target": "-5"})
    assert e.value.errors == {
        "region_id": ["`Atlantis` is not a known region"],
        "month": ["fails the rule `day(month) = 1`"],
        "revenue_target": ["fails the rule `revenue_target >= 0`"],
    }
    with pytest.raises(WriteError) as e:
        w.save("kpi_comments", {"kpi": "profit", "month": "2026-11-01", "comment": ""})
    assert e.value.errors == {"kpi": ["must be one of revenue, attainment, margin, returns"],
                              "comment": ["is required"]}
    with pytest.raises(WriteError) as e:
        w.save("targets", {"month": "soon", "region_id": "emea", "revenue_target": "1"})
    assert e.value.errors["month"] == ["`soon` is not a valid date"]


def test_reference_display_value_is_saved_as_the_key(demo):
    """A default such as `${filters.region.first}` gives `EMEA`; the row stores `emea`."""
    w = Writer(demo, {"name": "alice"})
    key = w.save("kpi_comments", {"kpi": "revenue", "region_id": "EMEA", "month": "2026-11-01", "comment": "ok"})
    row = w.get("kpi_comments", key)
    assert row["region_id"] == "emea" and row["created_by"] == "alice" and key == {"comment_id": 1}


def test_update_keeps_the_prior_version_in_history(demo):
    w = Writer(demo, SALES)
    key = {"month": date(2026, 1, 1), "region_id": "emea"}
    before = w.get("targets", key)["revenue_target"]
    w.save("targets", {**key, "revenue_target": before * 3})
    assert w.get("targets", key)["revenue_target"] == before * 3
    hist = w.history("targets", key)
    assert hist[0]["history_op"] == "update" and hist[0]["revenue_target"] == before
    assert hist[0]["history_by"] == "bob"


def test_edited_target_changes_attainment(demo):
    eng = demo.engine("sales")
    q = SemanticQuery(["target_attainment"], ["region"])
    before = {r["region"]: r["target_attainment"] for r in eng.query(q).to_pylist()}
    Writer(demo, SALES).save("targets", {"month": "2026-01-01", "region_id": "emea", "revenue_target": "948000"})
    after = {r["region"]: r["target_attainment"] for r in eng.query(q).to_pylist()}
    assert after["EMEA"] < before["EMEA"] and after["APAC"] == before["APAC"]


# --------------------------------------------------------------------------- per + LEFT join


def test_open_item_without_a_note_still_appears_with_empty_note_columns(erp):
    """Review item 5: all joins are LEFT joins; a note never hides an order item."""
    without = open_items(erp)
    assert without and all(r["note"] is None and r["note_status"] is None for r in without)
    first = without[0]
    Writer(erp, SALES).save("order_notes", {"VBELN": first["sales_order"], "POSNR": first["item"],
                                            "note": "Call the customer"})
    rows = open_items(erp)
    assert len(rows) == len(without)
    noted = [r for r in rows if r["note"] is not None]
    assert [(r["sales_order"], r["item"], r["note_status"]) for r in noted] == [
        (first["sales_order"], first["item"], "draft")]
    assert sum(r["open_order_value_usd"] or 0 for r in rows) == pytest.approx(
        sum(r["open_order_value_usd"] or 0 for r in without))


def test_per_checks_that_the_parent_row_exists(erp):
    with pytest.raises(WriteError) as e:
        Writer(erp, SALES).save("order_notes", {"VBELN": "9999999999", "POSNR": "000010", "note": "x"})
    assert e.value.errors == {"": ["there is no order_items row with VBELN = 9999999999, POSNR = 000010"]}
    with pytest.raises(WriteError) as e:
        Writer(erp, SALES).save("order_notes", {"VBELN": "9999999999", "note": "x"})
    assert e.value.errors == {"": ["the order_items key (VBELN, POSNR) is required"]}


# --------------------------------------------------------------------------- workflow


def test_workflow_submit_then_approve_by_manager_only(erp):
    item = open_items(erp)[0]
    key = {"VBELN": item["sales_order"], "POSNR": item["item"]}
    bob, mia = Writer(erp, SALES), Writer(erp, MANAGER)
    bob.save("order_notes", {**key, "note": "Check stock"})
    assert bob.transitions("order_notes", bob.get("order_notes", key)) == ["submit"]
    assert bob.transition("order_notes", key, "submit") == "submitted"
    assert bob.transitions("order_notes", bob.get("order_notes", key)) == []
    with pytest.raises(WriteError) as e:
        bob.transition("order_notes", key, "approve")
    assert e.value.errors == {"status": ["`approve` is not allowed for bob (`${user.role == 'sales_manager'}`)"]}
    assert mia.transitions("order_notes", mia.get("order_notes", key)) == ["approve", "reopen"]
    assert mia.transition("order_notes", key, "approve") == "approved"
    with pytest.raises(WriteError) as e:
        mia.transition("order_notes", key, "submit")
    assert e.value.errors == {"status": ["`submit` needs status draft; it is `approved`"]}
    ops = [h["history_op"] for h in mia.history("order_notes", key)]
    assert ops == ["transition:approve", "transition:submit"]


def test_status_changes_only_through_a_transition(erp):
    item = open_items(erp)[0]
    key = {"VBELN": item["sales_order"], "POSNR": item["item"]}
    w = Writer(erp, SALES)
    w.save("order_notes", {**key, "note": "a"})
    with pytest.raises(WriteError) as e:
        w.save("order_notes", {**key, "note": "b", "status": "approved"})
    assert e.value.errors == {"status": ["`status` changes only through a transition"]}
