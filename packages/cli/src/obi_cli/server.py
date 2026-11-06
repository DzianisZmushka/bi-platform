"""`obi serve`: dashboards and forms over HTTP (ADR-007, ADR-012, ADR-014).

Two kinds of site:
- **local** (`obi serve PROJECT`): re-validate and re-render on every request. Edit the YAML,
  refresh the browser, and see the change, or see the validation errors when it is broken.
- **Git** (`obi serve --git URL`): serve a deployed release, validated once at deploy.
  `POST /_deploy` (`obi deploy`, a webhook) answers 202 at once and deploys in the background
  (fetch, validate, migrate, swap; result at `GET /_deploy/status`); a failed
  validation keeps the previous release live and shows its errors to admins only.

Who is asking: the signed-in user (users.json in the data directory, a session cookie), or, for
local development and tests, whoever `--user` / `--role` / `--attr` say (loopback only).

A form or sheet posts to `/_write`; the write runs the model's rules and the browser is sent
back to the page, which then shows the new data. `GET /_health` answers without sign-in.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import socket
import subprocess
import sys
import time
import traceback
from datetime import date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from obi_formats.artifacts import parse

from obi_engine import Grants, Project
from obi_engine.store import auto_migrate
from obi_runtime.deploy import GitSite, Site
from obi_runtime.users import SESSION_HOURS, UserStore, make_session, read_session, secret_key

from .checks import validate_all as validate_project
from .forms import load_forms, handle_post, parse_body
from .render import dashboard_ids, filter_ids, render_dashboard, render_form_page

COOKIE = "obi_session"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def git_commit(root: Path) -> str | None:
    """Short commit of the checkout holding `root`, with `+changes` when files under it differ."""
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5)
        if head.returncode:
            return None
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "."],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return head.stdout.strip() + (" +changes" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return None


def provenance(root: Path, started: str, commit: str | None) -> str:
    """Footer line: which project, which code, since when; warns when the code is stale."""
    now = git_commit(root)
    text = (f"Served by obi serve from <code>{html.escape(str(root.resolve()))}</code>, "
            f"started {started} at commit <code>{html.escape(commit or 'unknown')}</code>.")
    if commit and now and now.split()[0] != commit.split()[0]:
        text += (f" <b>The repository is now at <code>{html.escape(now)}</code>: this server runs older "
                 f"code. Restart <code>obi serve</code>.</b>")
    return f'<div class="provenance">{text}</div>'


def pending_banner(pending: list[str]) -> str:
    """Shown on every page while a property marked `x-pending` is in use: accepted by the
    validator, not enforced yet (ADR-013). Never silent."""
    if not pending:
        return ""
    return ('<div class="pending-banner"><b>Access rules declared but not enforced.</b> '
            + html.escape(" ".join(pending)) + "</div>")


def failure_banner(site: Site) -> str:
    """Admins only (ADR-014): a deploy is running, or the last one failed and an older release
    is still live."""
    running = ""
    st = getattr(site, "status", None)
    if st is not None and st.state == "running":
        running = f'<div class="pending-banner">A deploy is running (started {html.escape(st.started or "")}).</div>'
    f = site.failure
    if f is None:
        return running
    live = site.release()
    still = f"still serving <code>{html.escape(live.commit)}</code>" if live and live.commit else "nothing is served"
    what = f"Deploy of <code>{html.escape(f.commit)}</code>" if f.commit else "The last deploy"
    return (f'<div class="pending-banner"><b>{what} failed at {f.at}; '
            f'{still}.</b> Only admins see this.<ul>'
            + "".join(f"<li><code>{html.escape(e)}</code></li>" for e in f.errors) + "</ul></div>" + running)


class App:
    """The request logic, independent of the socket (tests drive it through `make_server`)."""

    def __init__(self, site: Site, as_of: date | None = None, user: dict | None = None):
        self.site, self.as_of = site, as_of
        self.fixed_user = user  # --user: local development and tests, no sign-in
        self.started = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.local_commit = None if site.git else git_commit(site.root)
        self.pending: list[str] = []  # local: "not enforced yet" warnings of the last validation
        self.store = UserStore.at(site.data)
        self._key: bytes | None = None
        if site.git:
            deploy_token(site)  # exists from the start, so `obi deploy --home` can read it

    @property
    def key(self) -> bytes:
        if self._key is None:
            self._key = secret_key(self.site.data)
        return self._key

    # ---- who is asking

    def identify(self, cookie_header: str | None) -> tuple[dict | None, bool]:
        """(user, admin). None: nobody is signed in."""
        if self.fixed_user is not None:
            return self.fixed_user, True  # the developer on their own machine
        jar = SimpleCookie()
        try:
            jar.load(cookie_header or "")
        except Exception:
            return None, False
        name = read_session(self.key, self.store, jar[COOKIE].value if COOKIE in jar else None)
        if name is None:
            return None, False
        return self.store.identity(name), self.store.is_admin(name)

    # ---- pages

    def footer(self, user: dict, admin: bool, root: Path) -> str:
        def show(v):
            return ",".join(map(str, v)) if isinstance(v, list) else str(v)
        rel = self.site.release()
        extra = {k: v for k, v in user.items() if k != "name" and v not in ([], None, "")}
        who = f"Signed in as <code>{html.escape(str(user.get('name', 'anonymous')))}</code>" \
              + (f" ({html.escape('; '.join(f'{k}={show(v)}' for k, v in extra.items()))})" if extra else "") + "."
        if self.fixed_user is None:
            who += (' <form method="post" action="/_signout" style="display:inline">'
                    '<button class="link">Sign out</button></form>')
        if self.site.git and rel:
            where = (f'<div class="provenance">Served from <code>{html.escape(self.site.shown_url)}</code> branch '
                     f'<code>{html.escape(self.site.branch)}</code> at commit <code>{html.escape(rel.commit)}</code>, '
                     f'deployed {rel.at}.</div>')
        else:
            where = provenance(root, self.started, self.local_commit)
        pending = rel.pending if self.site.git and rel else self.pending
        return (failure_banner(self.site) if admin else "") + pending_banner(pending) + where \
            + f'<div class="provenance">{who}</div>'

    def page(self, path_qs: str, user: dict, admin: bool, form_state: dict | None = None) -> tuple[int, str]:
        rel = self.site.release()
        if rel is None:
            body = "No release is deployed yet." + (failure_banner(self.site) if admin else "")
            return 503, _page("Not deployed", body)
        root = rel.root
        url = urlparse(path_qs)
        parts = url.path.strip("/").split("/")
        if not self.site.git:  # local: edit, refresh, see the change (or the errors)
            checked = validate_project(root, execute=True)
            self.pending[:] = [i.text for i in checked if i.severity == "warning" and "is not enforced yet" in i.text]
            issues = [i for i in checked if i.severity == "error"]
            if issues:
                return 400, _page("Validation failed", footer=self.footer(user, admin, root), body=
                                  "<ul>" + "".join(f"<li><code>{html.escape(str(i))}</code></li>" for i in issues)
                                  + "</ul>")
        footer = self.footer(user, admin, root)
        qs = parse_qs(url.query, keep_blank_values=True)
        if parts[0] == "forms" and len(parts) == 2:  # a form or sheet on its own page
            return 200, render_form_page(root, parts[1], user, path_qs, form_state, qs, footer, self.as_of)
        did = parts[0]
        known = dashboard_ids(root)
        grants = Grants.load(root, user)  # the server decides what is listed and opened
        if not did:
            links = "".join(f'<li><a href="/{d}">{html.escape(t)}</a> <code>{d}</code></li>' for d, t in known
                            if grants.can_view_dashboard(d))
            forms = "".join(f'<li><a href="/forms/{f}">{html.escape(d.get("title") or f)}</a> <code>{f}</code></li>'
                            for f, d in load_forms(root).items() if grants.can_view_form(f))
            proj = parse((root / "project.yaml").read_text(encoding="utf-8")).data
            desc = f"<p>{html.escape(proj.get('description') or '')}</p>"
            body = f"{desc}<h2>Dashboards</h2><ul>{links}</ul>" + (f"<h2>Forms</h2><ul>{forms}</ul>" if forms else "")
            return 200, _page(proj.get("title") or f"Project {root.name}", body, footer)
        if did not in {d for d, _ in known}:
            return 404, _page("Not found", f"No dashboard <code>{html.escape(did)}</code>.", footer)
        if not grants.can_view_dashboard(did):
            return 403, _page("No access", f"No access to dashboard <code>{html.escape(did)}</code>: access.yaml "
                                           f"gives <code>view</code> on it to none of your roles.", footer)
        fids = filter_ids(root, did)
        if "_set" in qs:
            # Complete filter state (form submit, in-dashboard link): a filter that is missing
            # was cleared, not reset to its default.
            inputs = {fid: qs.get(fid, []) for fid in fids}
        else:
            # Partial (link from another dashboard): given filters on top of defaults.
            inputs = {fid: qs[fid] for fid in fids if fid in qs} or None
        drill = {k[len("drill."):]: v for k, v in qs.items() if k.startswith("drill.")}
        result = render_dashboard(root, did, inputs, self.as_of, mode="serve", drill=drill, footer=footer,
                                  user=user, here=path_qs, form_state=form_state, query=qs)
        return 200, result.html

    def write(self, body: dict, user: dict, admin: bool) -> tuple[int, str, str | None, str | None]:
        """(status, page, redirect, refusal) for a POST to /_write. `refusal` is every message of
        a refused write on one line, for the `X-Obi-Error` header."""
        rel = self.site.release()
        if rel is None:
            return 503, _page("Not deployed", "No release is deployed yet."), None, "no release is deployed yet"
        project = Project(rel.root)
        auto_migrate(project)
        redirect, state = handle_post(project, user, load_forms(rel.root), body)
        if redirect:
            return 303, "", redirect, None
        back = (body.get("_return") or ["/"])[0]
        status, page = self.page(back, user, admin, state)
        return (422 if status == 200 else status), page, None, refusal(state)

    def deploy_allowed(self, headers, raw: bytes) -> bool:
        """`Authorization: Bearer <OBI_DEPLOY_TOKEN>` (obi deploy), or GitHub's signature of the body."""
        token = deploy_token(self.site)
        if not token:
            return False
        auth = headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
            return True
        sig = headers.get("X-Hub-Signature-256") or ""
        want = "sha256=" + hmac.new(token.encode(), raw, hashlib.sha256).hexdigest()
        return bool(sig) and hmac.compare_digest(sig, want)

    def no_users_hint(self) -> str:
        """On the sign-in page while the store is empty: how to add the first user."""
        if self.store.names():
            return ""
        where = f"--home {self.site.home}" if self.site.git else f"--project {self.site.root}"
        return (f"No users yet. Add one on the server: <code>obi users add &lt;name&gt; {html.escape(where)} "
                f"--role &lt;role&gt;</code>, or serve with <code>--user</code> for local development.")

    def health(self) -> tuple[int, dict]:
        rel = self.site.release()
        if rel is None:
            return 503, {"status": "not deployed"}
        commit = rel.commit if self.site.git else (self.local_commit or "")
        return 200, {"status": "ok", "commit": commit}


def refusal(state: dict | None) -> str | None:
    """Every message of a refused write, on one line: `field: message; ...` (a form's errors are
    per field, `""` for the whole row; a sheet's are already lines)."""
    msgs = []
    for st in (state or {}).values():
        errors = st.get("errors") or {}
        if isinstance(errors, dict):
            msgs += [f"{c}: {m}" if c else m for c, ms in errors.items() for m in ms]
        else:
            msgs += list(errors)
    return "; ".join(msgs) or None


def header_text(text: str) -> str:
    """A header value: printable ASCII as is (so a grep finds the message), everything else,
    and `%`, percent-encoded as UTF-8 (HTTP headers are Latin-1)."""
    return "".join(c if " " <= c <= "~" and c != "%" else quote(c, safe="") for c in text.replace("\n", " "))


def deploy_token(site: Site) -> str | None:
    """`OBI_DEPLOY_TOKEN`, else one generated into the Git site's home (`obi deploy` there reads it)."""
    import os
    import secrets
    env = os.environ.get("OBI_DEPLOY_TOKEN")
    if env:
        return env
    if not site.git:
        return None
    path = site.home / "deploy.token"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(24), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def make_server(app: App, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if app.fixed_user is not None and host not in LOOPBACK:
        raise SystemExit(f"obi: --user signs every visitor in as `{app.fixed_user.get('name')}`; it is for "
                         f"local development only. Use it with --host 127.0.0.1, or add users with "
                         f"`obi users add` and serve without --user")

    class Handler(BaseHTTPRequestHandler):
        user_name = "-"

        def do_GET(self):
            started = time.perf_counter()
            path = urlparse(self.path).path
            try:
                if path == "/_health":
                    return self._json(*app.health())
                if path == "/_deploy/status":
                    _, admin = app.identify(self.headers.get("Cookie"))
                    if not (admin or app.deploy_allowed(self.headers, b"")):
                        return self._json(401, {"message": "a valid deploy token or an admin session is required"})
                    if not app.site.git:
                        return self._json(404, {"message": "this server serves a local directory; no deploys"})
                    return self._json(200, app.site.status.as_dict())
                if path == "/_signin":
                    nxt = (parse_qs(urlparse(self.path).query).get("next") or ["/"])[0]
                    return self._send(200, signin_page(nxt, hint=app.no_users_hint()))
                user, admin = app.identify(self.headers.get("Cookie"))
                if user is None:
                    return self._redirect("/_signin?next=" + quote(self.path, safe=""))
                self.user_name = user.get("name", "-")
                self._send(*app.page(self.path, user, admin))
            except Exception:
                self._send(500, _page("Render failed", f"<pre>{html.escape(traceback.format_exc())}</pre>"))
            finally:
                self._log(started)

        def do_POST(self):
            started = time.perf_counter()
            path = urlparse(self.path).path
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if path == "/_deploy":
                    if not app.deploy_allowed(self.headers, raw):
                        return self._json(401, {"ok": False, "message": "a valid deploy token is required"})
                    if not app.site.git:
                        return self._json(400, {"message": "this server serves a local directory; deploy "
                                                           "needs `obi serve --git`"})
                    # Answer at once (a webhook waits seconds at most); the result goes to
                    # GET /_deploy/status and the admin banner.
                    run = app.site.start_deploy()
                    return self._json(202, {"accepted": True, "run": run, "status": "/_deploy/status"})
                if path == "/_signin":
                    return self._signin(parse_body(raw))
                if path == "/_signout":
                    self.send_response(303)
                    self.send_header("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
                    self.send_header("Location", "/_signin")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                if path != "/_write":
                    return self._send(404, _page("Not found", "Writes go to <code>/_write</code>."))
                user, admin = app.identify(self.headers.get("Cookie"))
                if user is None:
                    return self._send(401, _page("Sign in", 'Your session has ended. <a href="/_signin">Sign in</a> '
                                                            'and send the form again.'),
                                      error="not signed in: POST /_signin first and send the session cookie")
                self.user_name = user.get("name", "-")
                status, page, redirect, error = app.write(parse_body(raw), user, admin)
                if redirect:
                    return self._redirect(redirect)
                self._send(status, page, error=error)
            except Exception:
                self._send(500, _page("Write failed", f"<pre>{html.escape(traceback.format_exc())}</pre>"))
            finally:
                self._log(started)

        def _signin(self, body: dict):
            name = (body.get("name") or [""])[0].strip()
            nxt = (body.get("next") or ["/"])[0]
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/"  # never a redirect to another site
            if app.fixed_user is not None or app.store.authenticate(name, (body.get("password") or [""])[0]) is None:
                return self._send(401, signin_page(nxt, name, "Wrong name or password.", app.no_users_hint()))
            secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
            self.user_name = name
            self.send_response(303)
            self.send_header("Set-Cookie", f"{COOKIE}={make_session(app.key, app.store, name)}; Path=/; "
                                           f"Max-Age={SESSION_HOURS * 3600}; HttpOnly; SameSite=Lax{secure}")
            self.send_header("Location", nxt)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _redirect(self, location: str):
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _json(self, status: int, data: dict):
            self._raw(status, json.dumps(data).encode("utf-8"), "application/json")

        def _send(self, status: int, body: str, error: str | None = None):
            self._raw(status, body.encode("utf-8"), "text/html; charset=utf-8", error)

        def _raw(self, status: int, data: bytes, ctype: str, error: str | None = None):
            self.status = status
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if error:  # the page shows it inside the form; curl and scripts read it here
                self.send_header("X-Obi-Error", header_text(error))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_response(self, code, message=None):
            self.status = code
            super().send_response(code, message)

        def _log(self, started: float):
            """One line per request on stdout: method, path (never the query: it holds filter
            values), status, user, milliseconds."""
            ms = (time.perf_counter() - started) * 1000
            print(f"{datetime.now().isoformat(timespec='seconds')} {self.command} {urlparse(self.path).path} "
                  f"{getattr(self, 'status', '-')} {self.user_name} {ms:.0f}ms", flush=True)

        def log_message(self, fmt, *args):
            pass

    try:
        return _ExclusiveServer((host, port), Handler)
    except OSError:
        raise SystemExit(f"obi: port {port} is already in use (another `obi serve`?); "
                         f"stop it or pass --port, e.g. --port {port + 1}") from None


def serve(site: Site, host: str, port: int, as_of: date | None, user: dict | None = None):
    app = App(site, as_of, user)
    server = make_server(app, host, port)
    rel = site.release()
    what = f"{site.shown_url} ({site.branch}) at {rel.commit if rel else 'no release'}" if site.git \
        else f"{site.root.resolve()} (commit {app.local_commit or 'unknown'})"
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    print(f"obi: serving {what} at http://{shown}:{port}/" + ("" if user is not None else
          f"  (sign-in; users: {len(app.store.names())})") + "  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


class _ExclusiveServer(ThreadingHTTPServer):
    """http.server sets SO_REUSEADDR, which on Windows lets a second server bind a port that
    is already serving, and requests then reach either one. Refuse instead."""

    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def signin_page(nxt: str, name: str = "", error: str = "", hint: str = "") -> str:
    err = f'<p style="color:#b91c1c">{html.escape(error)}</p>' if error else ""
    if hint:
        err += f'<p style="color:#6b7280">{hint}</p>'
    return _page("Sign in", f"""{err}<form method="post" action="/_signin" style="display:grid;gap:8px;max-width:320px">
<input type="hidden" name="next" value="{html.escape(nxt)}">
<label>Name<br><input name="name" value="{html.escape(name)}" autocomplete="username" required autofocus></label>
<label>Password<br><input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button></form>""")


def _page(title: str, body: str, footer: str = "") -> str:
    return (f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<body style='font:14px system-ui;margin:32px;max-width:960px'>"
            f"<h1>{html.escape(title)}</h1>{body}<p style='color:#6b7280;font-size:12px'>{footer}</p></body>")


def git_site(home: Path, url: str, branch: str, path: str) -> GitSite:
    """A Git site validated like `obi validate --execute`. A start deploys the branch head, with
    full validation; only when that fails (no network, a broken commit) does it serve the release
    that was live before the restart."""
    site = GitSite(home, url, branch, path, validate=lambda root: validate_project(root, execute=True))
    if not site.deploy().ok:
        site.restore()
    return site
