"""Git-backed serve and deploy (ADR-014): fetch, validate, migrate, swap. A broken commit leaves
the previous release serving; a deploy never alters the data directory."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import time
import subprocess
from datetime import date
from pathlib import Path

import pytest

from httpsite import Client, running
from obi_cli.server import App, git_site
from obi_engine import Project
from obi_runtime.deploy import GitSite
from obi_runtime.users import UserStore

EXAMPLES = Path(os.environ["OBI_EXAMPLES"])
AS_OF = date(2026, 11, 5)
PASSWORD = "correct horse"
TOKEN = "test-deploy-token"
MODEL = "examples/erp-sales/model/erp.model.yaml"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "--short=12", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A repository like this one: the project in examples/erp-sales."""
    r = tmp_path / "repo"
    shutil.copytree(EXAMPLES / "erp-sales", r / "examples" / "erp-sales", ignore=shutil.ignore_patterns(".obi"))
    git(r.parent, "init", "-q", "-b", "main", str(r))
    commit(r, "first")
    return r


@pytest.fixture
def site(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("OBI_DEPLOY_TOKEN", TOKEN)
    s = git_site(tmp_path / "home", str(repo), "main", "examples/erp-sales")
    s.log = lambda line: None
    UserStore.at(s.data).add("ada", PASSWORD, ["admin"], {}, admin=True)
    UserStore.at(s.data).add("mia", PASSWORD, ["sales_manager"], {"sales_orgs": ["DE01", "PL01"]})
    return s


def snapshot(site: GitSite) -> dict:
    """Everything a deploy must not alter: every row of every writable table, the user store and
    the session key. Rows are read through the live release (one database file, one process)."""
    con = Project(site.release().root).con
    tables = [t for (t,) in con.sql("SELECT table_name FROM duckdb_tables() WHERE database_name = 'store' "
                                    "ORDER BY 1").fetchall()]
    rows = {t: con.sql(f'SELECT * FROM store.main."{t}" ORDER BY ALL').fetchall() for t in tables}
    files = {p.name: p.read_bytes() for p in site.data.iterdir() if p.suffix in (".json", ".key")}
    return {"rows": rows, "files": files}


def test_git_site_serves_the_branch_with_data_outside_the_checkout(site, repo):
    head = git(repo, "rev-parse", "--short=12", "HEAD")
    rel = site.release()
    assert rel.commit == head and rel.root == site.home / "releases" / head / "examples" / "erp-sales"
    assert (site.data / "erp-sales.duckdb").is_file()
    assert not (rel.root / ".obi").exists()  # nothing of the data lives in the checkout
    with running(App(site, AS_OF)) as port:
        c = Client(port)
        assert c.get("/")[0] == 303  # sign-in, as for a local site
        c.sign_in("mia", PASSWORD)
        status, _, text = c.get("/order-to-cash")
        assert status == 200 and f"at commit <code>{head}</code>" in text and "branch <code>main</code>" in text
        assert json.loads(c.get("/_health")[2]) == {"status": "ok", "commit": head}


def test_broken_commit_keeps_the_previous_release_and_tells_admins_only(site, repo):
    good = site.release().commit
    with running(App(site, AS_OF)) as port:
        mia, ada = Client(port), Client(port)
        mia.sign_in("mia", PASSWORD)
        ada.sign_in("ada", PASSWORD)
        # mia writes a note through the live release
        item = Project(site.release().root).con.sql(
            "SELECT p.VBELN, p.POSNR FROM vbap p JOIN vbak k USING (VBELN) WHERE k.VKORG = 'DE01' "
            "ORDER BY 1, 2 LIMIT 1").fetchall()[0]
        assert mia.post("/_write", {"_form": "order-note", "_action": "save", "note": "Before the deploy",
                                    "VBELN": item[0], "POSNR": item[1], "_return": "/order-to-cash"})[0] == 303
        before = snapshot(site)

        model = repo / MODEL
        model.write_text(model.read_text(encoding="utf-8").replace("order_value_usd:", "order_value_eur:", 1),
                         encoding="utf-8")
        bad = commit(repo, "rename a measure the dashboard uses")
        status, result = mia.deploy(TOKEN)
        assert status == 202 and result["ok"] is False and result["commit"] == bad
        assert result["message"] == f"{bad} failed validation; still serving {good}"
        assert result["errors"][0] == (
            "dashboards/order-to-cash/dashboard.yaml:28: error: dashboard `order-to-cash`, widget `ordered`: measure "
            "`order_value_usd` not found in model `erp`; did you mean `order_value_eur`?")

        assert site.release().commit == good
        assert json.loads(mia.get("/_health")[2])["commit"] == good
        assert not (site.home / "releases" / bad).exists()
        status, _, page = mia.get("/order-to-cash")
        assert status == 200 and f"at commit <code>{good}</code>" in page and "failed" not in page
        status, _, page = ada.get("/order-to-cash")
        assert status == 200 and f"Deploy of <code>{bad}</code> failed" in page
        assert f"still serving <code>{good}</code>" in page and "Only admins see this." in page
        assert snapshot(site) == before

        # the fix goes live; mia's note and the users are untouched
        model.write_text(model.read_text(encoding="utf-8").replace("order_value_eur:", "order_value_usd:", 1),
                         encoding="utf-8")
        fixed = commit(repo, "restore the measure")
        status, result = mia.deploy(TOKEN)
        assert (status, result["ok"], result["message"]) == (202, True, f"deployed {fixed}")
        assert site.release().commit == fixed and site.failure is None
        assert "Deploy of" not in ada.get("/order-to-cash")[2]
        assert snapshot(site) == before
        assert "Before the deploy" in mia.get(f"/order-to-cash?order={item[0]}&order_item={item[1]}")[2]
        assert sorted(p.name for p in (site.home / "releases").iterdir()) == sorted([good, fixed])


def test_deploy_never_alters_rows_when_the_model_adds_a_column(site, repo):
    """Migration is the only write a deploy makes, and it is additive: a new column, rows unchanged."""
    before = snapshot(site)
    model = repo / MODEL
    text = model.read_text(encoding="utf-8")
    anchor = "      next_step:"
    assert text.count(anchor) == 1
    model.write_text(text.replace(anchor, "      follow_up: { type: date }\n" + anchor), encoding="utf-8")
    commit(repo, "add a column to order notes")
    assert site.deploy().ok
    after = snapshot(site)
    assert after["files"] == before["files"]
    assert {t: [r[: len(before["rows"][t][0])] if r else r for r in rows] for t, rows in after["rows"].items()
            if t in before["rows"]} == before["rows"]


def test_deploy_needs_the_token_or_githubs_signature(site, repo):
    with running(App(site, AS_OF)) as port:
        c = Client(port)
        assert c.deploy("wrong")[0] == 401
        status, _, _ = c.request("POST", "/_deploy", b"{}")
        assert status == 401
        body = b'{"ref": "refs/heads/main"}'
        sig = "sha256=" + hmac.new(TOKEN.encode(), body, hashlib.sha256).hexdigest()
        status, _, text = c.request("POST", "/_deploy", body, {"X-Hub-Signature-256": sig})
        assert status == 202 and json.loads(text)["accepted"] is True


def test_obi_deploy_talks_to_the_running_server(site, repo, capsys):
    from obi_cli.__main__ import main
    (repo / "examples" / "erp-sales" / "README.md").write_text("# changed\n", encoding="utf-8")
    new = commit(repo, "docs")
    with running(App(site, AS_OF)) as port:
        assert main(["deploy", "--url", f"http://127.0.0.1:{port}", "--token", TOKEN]) == 0
    assert re.fullmatch(rf"obi: deployed {new} \(started [\d:T-]+, finished [\d:T-]+\)\n", capsys.readouterr().err)


def test_a_restart_serves_the_last_live_release(site, tmp_path, repo):
    live = site.release().commit
    again = GitSite(site.home, str(repo), "main", "examples/erp-sales")
    assert again.restore().commit == live and again.release().root == site.release().root
    # `obi users --home` needs no OBI_GIT_PATH: `current` records the project path
    assert GitSite(site.home, "", "", "").restore().root == site.release().root


def test_a_restart_on_a_broken_head_serves_the_last_good_release(site, repo):
    from obi_engine import forget
    live = site.release().commit
    forget(site.release().root)  # the old process is gone
    model = repo / MODEL
    model.write_text(model.read_text(encoding="utf-8").replace("order_value_usd:", "order_value_eur:", 1),
                     encoding="utf-8")
    bad = commit(repo, "broken")
    restarted = git_site(site.home, str(repo), "main", "examples/erp-sales")
    assert restarted.release().commit == live and restarted.failure.commit == bad


def test_a_missing_project_path_is_a_failed_deploy(tmp_path, repo):
    s = GitSite(tmp_path / "home2", str(repo), "main", "examples/nothing-here")
    s.log = lambda line: None
    r = s.deploy()
    head = git(repo, "rev-parse", "--short=12", "HEAD")
    assert not r.ok and r.errors == [f"commit {head} has no project.yaml at `examples/nothing-here` (OBI_GIT_PATH)"]
    assert s.release() is None


def test_credentials_in_the_git_url_never_reach_pages_or_logs():
    from obi_runtime.deploy import shown_url, shown_url_in
    assert shown_url("https://x-access-token:ghp_secret@github.com/acme/bi.git") == "https://github.com/acme/bi.git"
    assert shown_url("git@github.com:acme/bi.git") == "git@github.com:acme/bi.git"  # SSH: no secret in it
    assert shown_url_in("fatal: unable to access 'https://u:tok@github.com/a/b.git/': 403") == \
        "fatal: unable to access 'https://github.com/a/b.git/': 403"


def test_a_relative_local_path_is_the_repository_seen_from_where_obi_runs(tmp_path, repo, monkeypatch):
    """`obi serve --git . ...` (found in the live check: git read `.` relative to the mirror)."""
    monkeypatch.chdir(repo)
    s = GitSite(tmp_path / "home3", ".", "main", "examples/erp-sales")
    s.log = lambda line: None
    assert s.url == str(repo.resolve())
    assert s.deploy().ok and s.release().commit == git(repo, "rev-parse", "--short=12", "HEAD")


def test_obi_users_and_obi_deploy_need_only_the_home(site, repo, capsys, monkeypatch):
    """In the container: `obi users add ... --home` without OBI_GIT_PATH, `obi deploy --home`
    without OBI_DEPLOY_TOKEN (both found in the live check)."""
    import io
    from obi_cli.__main__ import main
    monkeypatch.delenv("OBI_GIT_PATH", raising=False)
    monkeypatch.delenv("OBI_DEPLOY_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["users", "add", "rob", "--home", str(site.home), "--role", "sales_rep",
                 "--attr", "sales_orgs=DE01", "--password-stdin"]) == 0
    assert UserStore.at(site.data).identity("rob") == {"name": "rob", "roles": ["sales_rep"], "sales_orgs": ["DE01"]}
    with running(App(site, AS_OF)) as port:
        assert (site.home / "deploy.token").is_file()
        capsys.readouterr()
        assert main(["deploy", "--url", f"http://127.0.0.1:{port}", "--home", str(site.home)]) == 0
    assert capsys.readouterr().err.startswith(f"obi: already serving {site.release().commit} (started ")


def test_a_webhook_during_a_slow_validation_answers_at_once(site, repo):
    """GitHub waits 10 s for a webhook; the deploy runs in the background and its result goes to
    GET /_deploy/status and the admin banner."""
    validate, calls = site._validate, []

    def slow(root):
        calls.append(root)
        time.sleep(3)
        return validate(root)

    site._validate = slow
    (repo / "examples" / "erp-sales" / "README.md").write_text("# changed\n", encoding="utf-8")
    new = commit(repo, "docs")
    old = site.release().commit
    body = b'{"ref": "refs/heads/main"}'
    sig = "sha256=" + hmac.new(TOKEN.encode(), body, hashlib.sha256).hexdigest()
    with running(App(site, AS_OF)) as port:
        hook, ada = Client(port), Client(port)
        ada.sign_in("ada", PASSWORD)
        t0 = time.monotonic()
        status, _, text = hook.request("POST", "/_deploy", body, {"X-Hub-Signature-256": sig})
        assert time.monotonic() - t0 < 1.0
        assert status == 202 and json.loads(text) == {"accepted": True, "run": 1, "status": "/_deploy/status"}
        # while it validates: the old release serves, admins see that a deploy is running
        status, _, text = ada.get("/_deploy/status")  # an admin session may read the status
        st = json.loads(text)
        assert status == 200 and st["state"] == "running" and st["started"]
        assert site.release().commit == old
        assert "A deploy is running (started " in ada.get("/")[2]
        # two more pushes while it runs fold into one more deploy
        for _ in range(2):
            assert hook.request("POST", "/_deploy", body, {"X-Hub-Signature-256": sig})[0] == 202
        assert Client(port).get("/_deploy/status")[0] == 401  # nobody else reads it
        deadline = time.monotonic() + 120
        while (st := json.loads(ada.get("/_deploy/status")[2]))["run"] < 3:
            assert time.monotonic() < deadline
            time.sleep(0.1)
    assert st["state"] == "finished" and st["ok"] is True and st["run"] == 3
    assert st["commit"] == new and st["message"] == f"already serving {new}" and st["finished"] >= st["started"]
    assert site.release().commit == new and len(calls) == 1  # the second deploy found nothing new


def test_a_failed_background_deploy_reports_its_errors_in_the_status(site, repo):
    model = repo / MODEL
    model.write_text(model.read_text(encoding="utf-8").replace("order_value_usd:", "order_value_eur:", 1),
                     encoding="utf-8")
    bad = commit(repo, "broken")
    with running(App(site, AS_OF)) as port:
        status, st = Client(port).deploy(TOKEN)
    assert (status, st["state"], st["ok"], st["commit"]) == (202, "finished", False, bad)
    assert st["message"].startswith(f"{bad} failed validation; still serving ") and len(st["errors"]) == 9
