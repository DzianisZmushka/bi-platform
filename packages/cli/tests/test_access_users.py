"""Roles and row-level security, enforced (ADR-013). Four users per example: an admin, a regional
sales rep, a sales manager over several regions or sales orgs, and a viewer who writes nothing.

For each: the totals equal hand-written SQL with the user's WHERE; a write outside the user's rows
is refused on the server; the history records who wrote; and the page reflects what the server
decided ("no access", read-only forms, forbidden dashboards)."""

from datetime import date
import os
import shutil
from pathlib import Path

import pytest

from obi_cli.__main__ import serve_user
from obi_cli.forms import handle_post, load_forms
from obi_cli.render import render_dashboard
from obi_engine import NoAccess, Project, SemanticQuery
from obi_engine.store import Writer, migrate

EXAMPLES = Path(os.environ["OBI_EXAMPLES"])
AS_OF = date(2026, 11, 5)


def fresh(tmp_path, name) -> Path:
    root = tmp_path / name
    shutil.copytree(EXAMPLES / name, root, ignore=shutil.ignore_patterns(".obi"))
    migrate(Project(root))
    return root


def post(root, user, **fields):
    _, state = handle_post(Project(root), user, load_forms(root), {k: [v] for k, v in fields.items()})
    return None if state is None else next(iter(state.values()))["errors"]


def close(got: dict, want: dict):
    assert got.keys() == want.keys()
    for k in want:
        assert got[k] == pytest.approx(want[k]), k


def oracle(root, sql) -> list[tuple]:
    return Project(root).con.sql(sql).fetchall()  # the system: every row


def in_list(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# =========================================================================== sales-demo: rows by region

DEMO_USERS = {
    "admin": {"name": "ada", "roles": ["admin"]},
    "rep": {"name": "rob", "roles": ["sales_rep"], "regions": ["emea"]},
    "manager": {"name": "mia", "roles": ["sales_manager"], "regions": ["emea", "na"]},
    "viewer": {"name": "vic", "roles": ["viewer"], "regions": ["apac"]},
}
ALL_REGIONS = ["emea", "na", "apac", "latam"]


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    return fresh(tmp_path_factory.mktemp("demo"), "sales-demo")


def demo_regions(who: str) -> list[str]:
    return ALL_REGIONS if who == "admin" else DEMO_USERS[who]["regions"]


@pytest.mark.parametrize("who", list(DEMO_USERS))
def test_demo_totals_match_sql_with_the_users_where(demo, who):
    eng = Project(demo, DEMO_USERS[who]).engine("sales")
    regions = in_list(demo_regions(who))
    got = {r["region"]: r["net_revenue"] for r in eng.query(SemanticQuery(["net_revenue"], ["region"])).to_pylist()}
    want = dict(oracle(demo, f"""
        SELECT r.region_name, SUM(o.quantity * o.unit_price * (1 - o.discount)) FROM order_lines o
        JOIN customers c USING (customer_id) JOIN regions r USING (region_id)
        WHERE r.region_id IN {regions} GROUP BY 1"""))
    close(got, want)
    # Facts that reach regions through other entities, and a writable entity, are limited too.
    refunds = eng.query(SemanticQuery(["refunds"], [])).to_pylist()[0]["refunds"]
    assert refunds == pytest.approx(oracle(demo, f"""
        SELECT SUM(x.refund_amount) FROM returns x JOIN order_lines o USING (order_id, line_no)
        JOIN customers c USING (customer_id) WHERE c.region_id IN {regions}""")[0][0])
    target = eng.query(SemanticQuery(["revenue_target"], [])).to_pylist()[0]["revenue_target"]
    assert target == pytest.approx(oracle(demo, f"""
        SELECT SUM(revenue_target) FROM store.main.targets WHERE region_id IN {regions}""")[0][0])
    # Filter choices are the user's rows too.
    assert eng.distinct("region") == sorted(r for (r,) in oracle(
        demo, f"SELECT region_name FROM regions WHERE region_id IN {regions}"))


def test_demo_missing_attribute_means_no_rows(demo):
    eng = Project(demo, {"name": "nobody", "roles": ["viewer"]}).engine("sales")
    assert eng.query(SemanticQuery(["net_revenue"], ["region"])).to_pylist() == []
    assert eng.query(SemanticQuery(["net_revenue"], [])).to_pylist() == [{"net_revenue": None}]


@pytest.mark.parametrize("who, readable", [("admin", True), ("manager", True), ("rep", False), ("viewer", False)])
def test_demo_cost_column_is_readable_only_by_listed_roles(demo, who, readable):
    eng = Project(demo, DEMO_USERS[who]).engine("sales")
    if readable:
        assert eng.query(SemanticQuery(["margin_pct"], [])).to_pylist()[0]["margin_pct"] is not None
    else:
        with pytest.raises(NoAccess, match=r"`order_lines.unit_cost` is readable only by sales_manager, admin"):
            eng.query(SemanticQuery(["margin_pct"], []))
    html = render_dashboard(demo, "commercial-review", None, AS_OF, mode="serve", user=DEMO_USERS[who]).html
    assert ('class="no-access"' in html) is (not readable)


def test_demo_writes_inside_and_outside_the_users_rows(tmp_path):
    root = fresh(tmp_path, "sales-demo")
    rep, manager, viewer = DEMO_USERS["rep"], DEMO_USERS["manager"], DEMO_USERS["viewer"]
    comment = dict(_form="kpi-comment", _action="save", kpi="revenue", month="2026-01-01", comment="ok")
    assert post(root, rep, **comment, region_id="emea") is None
    assert post(root, rep, **comment, region_id="na") == {"region_id": ["`na` is not a known region"]}
    assert post(root, viewer, **comment, region_id="apac") == {
        "": ["vic may not edit form `kpi-comment`; access.yaml gives `edit` on it to none of their roles"]}
    target = dict(_form="targets", _action="sheet_save", revenue_target="1")
    assert post(root, manager, **target, _key='{"month": "2026-01-01", "region_id": "na"}') is None
    # A hidden row is answered exactly like a row that does not exist (ADR-013).
    hidden = post(root, manager, **target, _key='{"month": "2026-01-01", "region_id": "apac"}')
    missing = post(root, manager, **target, _key='{"month": "1999-01-01", "region_id": "emea"}')
    assert hidden == missing == ["the row does not exist; reload the page"]
    assert post(root, rep, **target, _key='{"month": "2026-01-01", "region_id": "emea"}') == [
        "rob may not edit sheet `targets`; access.yaml gives `edit` on it to none of their roles"]
    system = Writer(Project(root))
    assert system.rows("kpi_comments")[0]["created_by"] == "rob"
    na = {"month": date(2026, 1, 1), "region_id": "na"}
    assert system.get("targets", na)["updated_by"] == "mia"
    assert system.history("targets", na)[0]["history_op"] == "update"
    assert system.get("targets", {"month": date(2026, 1, 1), "region_id": "apac"})["updated_by"] == "seed"


def test_demo_page_reflects_permissions(demo):
    rep = render_dashboard(demo, "commercial-review", None, AS_OF, mode="serve", user=DEMO_USERS["rep"]).html
    assert 'value="sheet_save"' not in rep  # the targets sheet is read-only for a rep
    assert "You may view this form, but none of your roles may edit it." not in rep  # kpi-comment: rep edits it
    viewer = render_dashboard(demo, "commercial-review", None, AS_OF, mode="serve", user=DEMO_USERS["viewer"]).html
    assert "You may view this form, but none of your roles may edit it." in viewer
    manager = render_dashboard(demo, "commercial-review", None, AS_OF, mode="serve", user=DEMO_USERS["manager"]).html
    assert 'value="sheet_save"' in manager


def test_denied_view_omits_the_form_widget_and_refuses_the_dashboard(tmp_path):
    root = fresh(tmp_path, "sales-demo")
    access = (root / "access.yaml").read_text(encoding="utf-8")
    (root / "access.yaml").write_text(access.replace(
        "  viewer: { description: Reads every dashboard and sheet for their regions; writes nothing. }",
        "  viewer:\n    deny: { view: { dashboards: [revenue-overview], forms: [kpi-comment] } }"), encoding="utf-8")
    viewer = DEMO_USERS["viewer"]
    html = render_dashboard(root, "commercial-review", None, AS_OF, mode="serve", user=viewer).html
    assert 'id="w-kpi-comment"' not in html and 'class="widget-error"' not in html
    from obi_engine import Grants
    grants = Grants.load(root, viewer)
    assert not grants.can_view_dashboard("revenue-overview") and grants.can_view_dashboard("commercial-review")


# =========================================================================== erp-sales: rows by sales org

ERP_USERS = {
    "admin": {"name": "ada", "roles": ["admin"]},
    "rep": {"name": "rob", "roles": ["sales_rep"], "sales_orgs": ["DE01"]},
    "manager": {"name": "mia", "roles": ["sales_manager"], "sales_orgs": ["DE01", "PL01"]},
    "viewer": {"name": "vic", "roles": ["viewer"], "sales_orgs": ["US01"]},
}
ALL_ORGS = ["DE01", "PL01", "AE01", "US01", "US02"]
ORDER = ("FROM vbap p JOIN vbak k ON p.VBELN = k.VBELN "
         "JOIN tcurr fx ON fx.FCURR = k.WAERK AND fx.GDATU = date_trunc('month', k.ERDAT)")


@pytest.fixture(scope="module")
def erp(tmp_path_factory):
    return fresh(tmp_path_factory.mktemp("erp"), "erp-sales")


def erp_orgs(who: str) -> list[str]:
    return ALL_ORGS if who == "admin" else ERP_USERS[who]["sales_orgs"]


@pytest.mark.parametrize("who", list(ERP_USERS))
def test_erp_totals_match_sql_with_the_users_where(erp, who):
    eng = Project(erp, ERP_USERS[who]).engine("erp")
    orgs = in_list(erp_orgs(who))
    got = {r["sales_org"]: r["order_value_usd"]
           for r in eng.query(SemanticQuery(["order_value_usd"], ["sales_org"])).to_pylist()}
    want = dict(oracle(erp, f"""SELECT o.VTEXT, SUM(p.NETWR * fx.UKURS) {ORDER}
        JOIN tvko o ON k.VKORG = o.VKORG WHERE k.VKORG IN {orgs} GROUP BY 1"""))
    close(got, want)
    # Billing reaches sales_orgs through order items and orders; rollups and materials follow.
    billed = eng.query(SemanticQuery(["billed_value_usd"], [])).to_pylist()[0]["billed_value_usd"]
    assert billed == pytest.approx(oracle(erp, f"""
        SELECT SUM(b.NETWR * fx.UKURS) FROM vbrp b JOIN vbap p ON b.AUBEL = p.VBELN AND b.AUPOS = p.POSNR
        JOIN vbak k ON p.VBELN = k.VBELN
        JOIN tcurr fx ON fx.FCURR = k.WAERK AND fx.GDATU = date_trunc('month', b.FKDAT)
        WHERE k.VKORG IN {orgs}""")[0][0])
    by_material = {r["material"]: r["delivered_value_usd"] for r in
                   eng.query(SemanticQuery(["delivered_value_usd"], ["material"])).to_pylist()}
    want = dict(oracle(erp, f"""
        SELECT m.MAKTX, SUM(COALESCE(d.q, 0) * p.NETWR / p.KWMENG * fx.UKURS) {ORDER}
        JOIN mara m ON p.MATNR = m.MATNR
        LEFT JOIN (SELECT VGBEL, VGPOS, SUM(LFIMG) AS q FROM lips GROUP BY ALL) d
          ON d.VGBEL = p.VBELN AND d.VGPOS = p.POSNR
        WHERE k.VKORG IN {orgs} GROUP BY 1"""))
    close(by_material, want)
    plans = eng.query(SemanticQuery(["planned_shipments_usd"], [])).to_pylist()[0]["planned_shipments_usd"]
    assert plans == pytest.approx(oracle(erp, f"""
        SELECT SUM(planned_value_usd) FROM store.main.shipment_plans WHERE VKORG IN {orgs}""")[0][0])


def test_rep_started_like_obi_serve_sees_only_de01(erp):
    """`obi serve --role sales_rep --attr sales_orgs=DE01`: only DE01 numbers, equal to SQL."""
    user = serve_user(erp, "rob", ["sales_rep"], ["sales_orgs=DE01"])
    eng = Project(erp, user).engine("erp")
    rows = eng.query(SemanticQuery(["order_value_usd", "order_count"], ["sales_org"])).to_pylist()
    want = oracle(erp, f"""SELECT o.VTEXT, SUM(p.NETWR * fx.UKURS), COUNT(DISTINCT p.VBELN) {ORDER}
        JOIN tvko o ON k.VKORG = o.VKORG WHERE k.VKORG = 'DE01' GROUP BY 1""")
    assert [(r["sales_org"], pytest.approx(r["order_value_usd"]), r["order_count"]) for r in rows] == \
        [(w[0], pytest.approx(w[1]), w[2]) for w in want]
    html = render_dashboard(erp, "order-to-cash", None, AS_OF, mode="serve", user=user).html
    assert "Germany" in html and not any(org in html for org in ("Poland", "UAE", "USA East", "USA West"))


@pytest.mark.parametrize("who, readable", [("admin", True), ("manager", True), ("rep", False), ("viewer", False)])
def test_erp_cost_is_readable_only_by_listed_roles(erp, who, readable):
    eng = Project(erp, ERP_USERS[who]).engine("erp")
    if readable:
        eng.query(SemanticQuery(["margin"], []))
    else:
        with pytest.raises(NoAccess, match=r"`order_items.WAVWR` is readable only by sales_manager, admin"):
            eng.query(SemanticQuery(["margin"], []))


def item_of(root, org: str) -> dict:
    row = oracle(root, f"""SELECT p.VBELN, p.POSNR FROM vbap p JOIN vbak k ON p.VBELN = k.VBELN
        WHERE k.VKORG = '{org}' ORDER BY 1, 2 LIMIT 1""")[0]
    return {"VBELN": row[0], "POSNR": row[1]}


def test_erp_writes_inside_and_outside_the_users_rows(tmp_path):
    root = fresh(tmp_path, "erp-sales")
    rep, manager, viewer = ERP_USERS["rep"], ERP_USERS["manager"], ERP_USERS["viewer"]
    de, pl = item_of(root, "DE01"), item_of(root, "PL01")
    note = dict(_form="order-note", _action="save", note="Call the customer")
    assert post(root, rep, **note, **de) is None
    assert post(root, rep, **note, **pl) == {"": [
        f"there is no order_items row with VBELN = {pl['VBELN']}, POSNR = {pl['POSNR']}"]}
    assert post(root, viewer, **note, **de) == {
        "": ["vic may not edit form `order-note`; access.yaml gives `edit` on it to none of their roles"]}
    # Workflow: the rep submits, the manager of DE01 approves.
    assert post(root, rep, _form="order-note", _action="transition:submit", **de, note="x") is None
    assert post(root, manager, _form="order-note", _action="transition:approve", **de, note="x") is None
    plan = dict(_form="shipment-plan", _action="sheet_add", planned_value_usd="5")
    assert post(root, manager, **plan, VKORG="PL01", week_start="2027-01-04") is None
    assert post(root, manager, **plan, VKORG="AE01", week_start="2027-01-04") == [
        "VKORG: `AE01` is not a known sales_org"]
    system = Writer(Project(root))
    key = {"VBELN": de["VBELN"], "POSNR": de["POSNR"]}
    row = system.get("order_notes", key)
    assert (row["created_by"], row["updated_by"], row["status"]) == ("rob", "mia", "approved")
    assert [h["history_by"] for h in system.history("order_notes", key)] == ["mia", "rob"]
    # A manager of other orgs does not even see the note.
    assert Writer(Project(root), {"name": "eve", "roles": ["sales_manager"], "sales_orgs": ["US01"]}).get(
        "order_notes", key) is None


def test_auditor_never_writes_whatever_other_roles_say(tmp_path):
    root = fresh(tmp_path, "erp-sales")
    both = {"name": "aud", "roles": ["sales_manager", "auditor"], "sales_orgs": ["DE01"]}
    assert post(root, both, _form="order-note", _action="save", note="x", **item_of(root, "DE01")) == {
        "": ["aud may not edit form `order-note`; access.yaml gives `edit` on it to none of their roles"]}
    html = render_dashboard(root, "order-to-cash", None, AS_OF, mode="serve", user=both).html
    assert "You may view this form, but none of your roles may edit it." in html
