"""`obi serve`: re-validate and re-render on every request; forms write through POST.

Edit the YAML, refresh the browser, and see the change, or see the validation errors
when it is broken. A form or sheet posts to `/_write`; the write runs the model's rules
and the browser is sent back to the page, which then shows the new data. Local use only:
binds to 127.0.0.1. The user is whoever `--user` / `--role` / `--attr` say (no sign-in before the
Phase 2 runtime).
"""

from __future__ import annotations

import html
import socket
import subprocess
import sys
import traceback
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from obi_formats.artifacts import parse

from obi_engine import Grants, Project
from obi_engine.store import auto_migrate

from .checks import validate_all as validate_project
from .forms import load_forms, handle_post, parse_body
from .render import dashboard_ids, filter_ids, render_dashboard, render_form_page


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


def serve(root: Path, port: int, as_of: date | None, user: dict | None = None):
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit = git_commit(root)
    user = user or {}

    pending: list[str] = []  # "not enforced yet" warnings of the last validation (x-pending)

    def footer() -> str:
        def show(v):
            return ",".join(map(str, v)) if isinstance(v, list) else str(v)
        extra = {k: v for k, v in user.items() if k != "name" and v not in ([], None, "")}
        who = f" Signed in as <code>{html.escape(str(user.get('name', 'anonymous')))}</code>" \
              + (f" ({html.escape('; '.join(f'{k}={show(v)}' for k, v in extra.items()))})" if extra else "") + "."
        return pending_banner(pending) + provenance(root, started, commit) + f'<div class="provenance">{who}</div>'

    def dashboard_page(path_qs: str, form_state: dict | None = None) -> tuple[int, str]:
        url = urlparse(path_qs)
        parts = url.path.strip("/").split("/")
        checked = validate_project(root, execute=True)
        pending[:] = [i.text for i in checked if i.severity == "warning" and "is not enforced yet" in i.text]
        issues = [i for i in checked if i.severity == "error"]
        if issues:
            return 400, _page("Validation failed", footer=footer(), body=
                              "<ul>" + "".join(f"<li><code>{html.escape(str(i))}</code></li>" for i in issues) + "</ul>")
        qs = parse_qs(url.query, keep_blank_values=True)
        if parts[0] == "forms" and len(parts) == 2:  # a form or sheet on its own page
            return 200, render_form_page(root, parts[1], user, path_qs, form_state, qs, footer(), as_of)
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
            return 200, _page(proj.get("title") or f"Project {root.name}", body, footer())
        if did not in {d for d, _ in known}:
            return 404, _page("Not found", f"No dashboard <code>{html.escape(did)}</code>.")
        if not grants.can_view_dashboard(did):
            return 403, _page("No access", f"No access to dashboard <code>{html.escape(did)}</code>: access.yaml "
                                           f"gives <code>view</code> on it to none of your roles.", footer())
        fids = filter_ids(root, did)
        if "_set" in qs:
            # Complete filter state (form submit, in-dashboard link): a filter that is missing
            # was cleared, not reset to its default.
            inputs = {fid: qs.get(fid, []) for fid in fids}
        else:
            # Partial (link from another dashboard): given filters on top of defaults.
            inputs = {fid: qs[fid] for fid in fids if fid in qs} or None
        drill = {k[len("drill."):]: v for k, v in qs.items() if k.startswith("drill.")}
        result = render_dashboard(root, did, inputs, as_of, mode="serve", drill=drill, footer=footer(),
                                  user=user, here=path_qs, form_state=form_state, query=qs)
        return 200, result.html

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                self._send(*dashboard_page(self.path))
            except Exception:
                self._send(500, _page("Render failed", f"<pre>{html.escape(traceback.format_exc())}</pre>"))

        def do_POST(self):
            """Writes from forms and sheets (ADR-012): one write path, then back to the page."""
            try:
                if urlparse(self.path).path != "/_write":
                    return self._send(404, _page("Not found", "Writes go to <code>/_write</code>."))
                body = parse_body(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                project = Project(root)
                auto_migrate(project)
                redirect, state = handle_post(project, user, load_forms(root), body)
                if redirect:
                    self.send_response(303)
                    self.send_header("Location", redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                back = (body.get("_return") or ["/"])[0]
                status, page = dashboard_page(back, state)
                self._send(422 if status == 200 else status, page)
            except Exception:
                self._send(500, _page("Write failed", f"<pre>{html.escape(traceback.format_exc())}</pre>"))

        def _send(self, status: int, body: str):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            pass

    try:
        server = _ExclusiveServer(("127.0.0.1", port), Handler)
    except OSError:
        raise SystemExit(f"obi: port {port} is already in use (another `obi serve`?); "
                         f"stop it or pass --port, e.g. --port {port + 1}") from None
    print(f"obi: serving {root.resolve()} (commit {commit or 'unknown'}) at http://127.0.0.1:{port}/"
          f"  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


class _ExclusiveServer(ThreadingHTTPServer):
    """http.server sets SO_REUSEADDR, which on Windows lets a second server bind a port that
    is already serving, and requests then reach either one. Refuse instead."""

    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _page(title: str, body: str, footer: str = "") -> str:
    return (f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<body style='font:14px system-ui;margin:32px;max-width:960px'>"
            f"<h1>{html.escape(title)}</h1>{body}<p style='color:#6b7280;font-size:12px'>{footer}</p></body>")
