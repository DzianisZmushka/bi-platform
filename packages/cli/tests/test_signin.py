"""Sign-in (ADR-014): users from the store next to the project database, a session cookie,
and the same behaviour as `obi serve --user` for the four users of ADR-013."""

from __future__ import annotations

import os
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from httpsite import Client, running
from obi_cli.__main__ import main, serve_user
from obi_cli.server import App
from obi_engine import Project, data_dir
from obi_engine.store import migrate
from obi_runtime.deploy import Site
from obi_runtime.users import UserStore

EXAMPLES = Path(os.environ["OBI_EXAMPLES"])
AS_OF = date(2026, 11, 5)
PASSWORD = "correct horse"

# The four users of test_access_users.py, as `obi users add` would store them.
USERS = {
    "ada": (["admin"], ["sales_orgs=DE01,PL01,AE01,US01,US02"], True),
    "rob": (["sales_rep"], ["sales_orgs=DE01"], False),
    "mia": (["sales_manager"], ["sales_orgs=DE01,PL01"], False),
    "vic": (["auditor"], ["sales_orgs=US01"], False),
}


@pytest.fixture(scope="module")
def erp(tmp_path_factory):
    root = tmp_path_factory.mktemp("signin") / "erp-sales"
    shutil.copytree(EXAMPLES / "erp-sales", root, ignore=shutil.ignore_patterns(".obi"))
    migrate(Project(root))
    from obi_runtime.users import parse_attributes
    store = UserStore.at(data_dir(root))
    for name, (roles, attrs, admin) in USERS.items():
        store.add(name, PASSWORD, roles, parse_attributes(root, attrs, strict=True), admin)
    return root


@pytest.fixture(scope="module")
def port(erp):
    with running(App(Site(erp), AS_OF)) as p:
        yield p


def item_of(root, org: str) -> dict:
    row = Project(root).con.sql(f"""SELECT p.VBELN, p.POSNR FROM vbap p JOIN vbak k ON p.VBELN = k.VBELN
        WHERE k.VKORG = '{org}' ORDER BY 1, 2 LIMIT 1""").fetchall()[0]
    return {"VBELN": row[0], "POSNR": row[1]}


def notes(root) -> list:
    return Project(root).con.sql("SELECT * FROM store.main.order_notes ORDER BY ALL").fetchall()


# =========================================================================== sign-in required


def test_every_page_needs_sign_in(port, erp):
    c = Client(port)
    for path in ("/", "/order-to-cash?period=this_year", "/forms/shipment-plan"):
        status, headers, _ = c.get(path)
        assert status == 303 and headers["Location"].startswith("/_signin?next=%2F"), path
    before = notes(erp)
    status, headers, text = c.post("/_write", {"_form": "order-note", "_action": "save", "note": "x",
                                               **item_of(erp, "DE01")})
    assert status == 401 and "Your session has ended" in text
    assert headers["X-Obi-Error"] == "not signed in: POST /_signin first and send the session cookie"
    assert notes(erp) == before  # nothing written


def test_health_answers_without_sign_in(port):
    status, headers, text = Client(port).get("/_health")
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert '"status": "ok"' in text and "rob" not in text


def test_wrong_password_and_unknown_user_get_the_same_answer(port):
    c = Client(port)
    for name, pw in (("mia", "wrong password"), ("nobody", PASSWORD)):
        status, _, text = c.sign_in(name, pw)
        assert status == 401 and "Wrong name or password." in text and c.cookie is None
    assert c.get("/")[0] == 303


def test_sign_in_sets_a_session_and_sign_out_ends_it(port):
    c = Client(port)
    status, headers, _ = c.sign_in("mia", PASSWORD)
    assert status == 303 and headers["Location"] == "/" and c.cookie
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie
    status, _, text = c.get("/")
    assert status == 200 and "Signed in as <code>mia</code> (roles=sales_manager; sales_orgs=DE01,PL01)" in text
    c.post("/_signout")
    assert c.cookie is None and c.get("/")[0] == 303


def test_secure_cookie_behind_an_https_proxy(port):
    c = Client(port)
    _, headers, _ = c.post("/_signin", {"name": "mia", "password": PASSWORD, "next": "/"},
                           {"X-Forwarded-Proto": "https"})
    assert "; Secure" in headers["Set-Cookie"]


def test_next_never_leaves_the_site(port):
    _, headers, _ = Client(port).post("/_signin", {"name": "mia", "password": PASSWORD, "next": "//evil.example"})
    assert headers["Location"] == "/"


def test_a_forged_cookie_is_no_session(port):
    c = Client(port)
    c.sign_in("rob", PASSWORD)
    token, sig = c.cookie.split("=", 1)[1].rsplit(".", 1)
    import base64
    forged = base64.urlsafe_b64encode(base64.urlsafe_b64decode(token + "==").replace(b"rob", b"ada")).decode().rstrip("=")
    c.cookie = f"obi_session={forged}.{sig}"
    assert c.get("/")[0] == 303


def test_store_changes_apply_to_running_sessions(erp, port):
    store = UserStore.at(data_dir(erp))
    store.add("tmp", PASSWORD, ["sales_rep"], {"sales_orgs": ["DE01"]})
    c = Client(port)
    c.sign_in("tmp", PASSWORD)
    assert "Germany" in c.get("/order-to-cash")[2]
    store.update("tmp", attributes={"sales_orgs": ["PL01"]})  # a role or attribute change: at once
    text = c.get("/order-to-cash")[2]
    assert "Poland" in text and "Germany" not in text
    store.update("tmp", password="another password")  # a new password ends the session
    assert c.get("/")[0] == 303
    c.sign_in("tmp", "another password")
    store.remove("tmp")  # so does removing the user
    assert c.get("/")[0] == 303


# =========================================================================== same as --user


NUMBER = re.compile(r"\d+\.\d+(?:e[-+]?\d+)?")


def same_page(a: str, b: str) -> bool:
    """Equal pages, apart from the footer (who is signed in, provenance) and float noise.
    DuckDB sums in parallel, so the last digits of a chart value vary between two renders of
    the same page for the same user; rounding is no cure (a value at a rounding boundary flips),
    so numbers are compared with a relative tolerance and everything else exactly."""
    a, b = (re.sub(r'<div class="provenance">.*?</div>', "", h, flags=re.S) for h in (a, b))
    if NUMBER.split(a) != NUMBER.split(b):
        return False
    return all(float(x) == pytest.approx(float(y), rel=1e-9) for x, y in zip(NUMBER.findall(a), NUMBER.findall(b)))


@pytest.mark.parametrize("name", list(USERS))
def test_store_users_see_what_the_same_user_sees_with_dash_dash_user(erp, port, name):
    roles, attrs, _ = USERS[name]
    signed = Client(port)
    signed.sign_in(name, PASSWORD)
    with running(App(Site(erp), AS_OF, serve_user(erp, name, roles, attrs))) as dev_port:
        dev = Client(dev_port)
        for path in ("/", "/order-to-cash", "/order-to-cash?period=last_fiscal_year", "/forms/shipment-plan"):
            a, b = signed.get(path), dev.get(path)
            assert a[0] == b[0], path
            assert same_page(a[2], b[2]), path


def test_store_users_write_inside_their_rows_only(tmp_path):
    root = tmp_path / "erp-sales"
    shutil.copytree(EXAMPLES / "erp-sales", root, ignore=shutil.ignore_patterns(".obi"))
    migrate(Project(root))
    UserStore.at(data_dir(root)).add("rob", PASSWORD, ["sales_rep"], {"sales_orgs": ["DE01"]})
    with running(App(Site(root), AS_OF)) as p:
        c = Client(p)
        c.sign_in("rob", PASSWORD)
        de, pl = item_of(root, "DE01"), item_of(root, "PL01")
        status, headers, _ = c.post("/_write", {"_form": "order-note", "_action": "save", "note": "Call",
                                                "_return": "/order-to-cash", **de})
        assert status == 303
        status, headers, text = c.post("/_write", {"_form": "order-note", "_action": "save", "note": "Call",
                                                   "_return": "/order-to-cash", **pl})
        assert status == 422 and f"there is no order_items row with VBELN = {pl['VBELN']}" in text
        # the reason is also one header line, for curl and scripts (the page shows it in the form)
        assert headers["X-Obi-Error"] == f"there is no order_items row with VBELN = {pl['VBELN']}, POSNR = {pl['POSNR']}"
        # several reasons, by field; a transition sent as `_action=transition:<name>`
        status, headers, _ = c.post("/_write", {"_form": "order-note", "_action": "save", "note": "",
                                                "next_step": "dance", **de})
        assert status == 422 and headers["X-Obi-Error"] == (
            "note: is required; next_step: must be one of call customer, check stock, wait for payment, cancel item; "
            "next_step: `next_step` may be written only by sales_manager, admin")
        assert c.post("/_write", {"_form": "order-note", "_action": "transition:submit", "note": "x", **de})[0] == 303
        status, headers, _ = c.post("/_write", {"_form": "order-note", "_action": "transition:approve", "note": "x",
                                                **de})
        assert status == 422 and headers["X-Obi-Error"] == (
            "status: `approve` is not allowed for rob (`${user.roles.includes('sales_manager')}`)")
    rows = Project(root).con.sql("SELECT VBELN, created_by FROM store.main.order_notes").fetchall()
    assert rows == [(de["VBELN"], "rob")]


# =========================================================================== --user is local only


def test_dash_dash_user_is_refused_on_a_public_address(erp):
    from obi_cli.server import make_server
    with pytest.raises(SystemExit, match="--user signs every visitor in as `rob`; it is for local development"):
        make_server(App(Site(erp), AS_OF, {"name": "rob", "roles": []}), "0.0.0.0", 0)


# =========================================================================== obi users


def test_obi_users_checks_roles_and_attributes_against_access_yaml(tmp_path, capsys, monkeypatch):
    root = tmp_path / "erp-sales"
    shutil.copytree(EXAMPLES / "erp-sales", root, ignore=shutil.ignore_patterns(".obi"))
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["users", "add", "mia", "--project", str(root), "--role", "sales_manager",
                 "--attr", "sales_orgs=DE01,PL01", "--password-stdin"]) == 0
    u = UserStore.at(root / ".obi").get("mia")
    assert u["roles"] == ["sales_manager"] and u["attributes"] == {"sales_orgs": ["DE01", "PL01"]}
    assert u["password"].startswith("scrypt$") and PASSWORD not in (root / ".obi" / "users.json").read_text()
    capsys.readouterr()
    assert main(["users", "set", "mia", "--project", str(root), "--role", "sales_manger"]) == 1
    assert main(["users", "set", "mia", "--project", str(root), "--attr", "sales_org=DE01"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["users", "add", "mia", "--project", str(root), "--password-stdin"]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "obi users: role `sales_manger` is not in access.yaml; roles are default, sales_rep, sales_manager, "
        "planner, finance, auditor, admin; did you mean `sales_manager`?",
        "obi users: attribute `sales_org` is not declared under `attributes` in access.yaml; declared: "
        "sales_orgs; did you mean `sales_orgs`?",
        "obi users: user `mia` exists; change it with `obi users set mia`"]
    assert main(["users", "list", "--project", str(root)]) == 0
    assert capsys.readouterr().out == "mia  roles=sales_manager  sales_orgs=DE01,PL01\n"


def test_users_json_is_never_in_git():
    """The store sits next to the project database, in `.obi/`, which .gitignore excludes."""
    root = Path(__file__).resolve().parents[3]
    assert ".obi/" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_sign_in_page_says_how_to_add_the_first_user(tmp_path):
    root = tmp_path / "sales-demo"
    shutil.copytree(EXAMPLES / "sales-demo", root, ignore=shutil.ignore_patterns(".obi"))
    with running(App(Site(root), AS_OF)) as p:
        text = Client(p).get("/_signin")[2]
        assert f"No users yet. Add one on the server: <code>obi users add &lt;name&gt; --project {root}" in text
        UserStore.at(data_dir(root)).add("mia", PASSWORD, ["sales_manager"], {"regions": ["emea"]})
        assert "No users yet" not in Client(p).get("/_signin")[2]


def test_error_header_is_greppable_ascii_with_the_rest_percent_encoded():
    from obi_cli.server import header_text, refusal
    assert header_text("row 2: month: fails the rule `day(month) = 1`") == "row 2: month: fails the rule `day(month) = 1`"
    assert header_text("planned → 100%\nnext") == "planned %E2%86%92 100%25 next"
    assert refusal({"targets": {"errors": ["row 1: a", "row 2: b"]}}) == "row 1: a; row 2: b"
    assert refusal(None) is None
