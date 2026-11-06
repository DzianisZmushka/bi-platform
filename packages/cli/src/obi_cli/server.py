"""`obi serve`: re-validate and re-render on every request.

Edit the YAML, refresh the browser, and see the change, or see the validation errors
when it is broken. Local use only: binds to 127.0.0.1.
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

from .checks import validate_all as validate_project

from .render import dashboard_ids, filter_ids, render_dashboard


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


def serve(root: Path, port: int, as_of: date | None):
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit = git_commit(root)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            did = url.path.strip("/")
            try:
                issues = [i for i in validate_project(root, execute=True) if i.severity == "error"]
                if issues:
                    return self._send(400, _page("Validation failed", footer=provenance(root, started, commit), body=
                                      "<ul>" + "".join(f"<li><code>{html.escape(str(i))}</code></li>"
                                                       for i in issues) + "</ul>"))
                known = dashboard_ids(root)
                if not did:
                    links = "".join(f'<li><a href="/{d}">{html.escape(t)}</a> <code>{d}</code></li>'
                                    for d, t in known)
                    proj = parse((root / "project.yaml").read_text(encoding="utf-8")).data
                    desc = f"<p>{html.escape(proj.get('description') or '')}</p>"
                    return self._send(200, _page(proj.get("title") or f"Project {root.name}",
                                                 f"{desc}<ul>{links}</ul>", provenance(root, started, commit)))
                if did not in {d for d, _ in known}:
                    return self._send(404, _page("Not found", f"No dashboard <code>{html.escape(did)}</code>."))
                qs = parse_qs(url.query, keep_blank_values=True)
                fids = filter_ids(root, did)
                if "_set" in qs:
                    # Complete filter state (form submit, in-dashboard link): a filter that is
                    # missing was cleared, not reset to its default.
                    inputs = {fid: qs.get(fid, []) for fid in fids}
                else:
                    # Partial (link from another dashboard): given filters on top of defaults.
                    inputs = {fid: qs[fid] for fid in fids if fid in qs} or None
                drill = {k[len("drill."):]: v for k, v in qs.items() if k.startswith("drill.")}
                result = render_dashboard(root, did, inputs, as_of, mode="serve", drill=drill,
                                          footer=provenance(root, started, commit))
                self._send(200, result.html)
            except Exception:
                self._send(500, _page("Render failed", f"<pre>{html.escape(traceback.format_exc())}</pre>"))

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
