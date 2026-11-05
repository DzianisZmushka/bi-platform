"""`obi serve`: re-validate and re-render on every request.

Edit the YAML, refresh the browser, and see the change, or see the validation errors
when it is broken. Local use only: binds to 127.0.0.1.
"""

from __future__ import annotations

import html
import traceback
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from obi_formats.validate import validate_project

from .render import dashboard_ids, filter_ids, render_dashboard


def serve(root: Path, port: int, as_of: date | None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            did = url.path.strip("/")
            try:
                issues = [i for i in validate_project(root) if i.severity == "error"]
                if issues:
                    return self._send(400, _page("Validation failed",
                                      "<ul>" + "".join(f"<li><code>{html.escape(str(i))}</code></li>"
                                                       for i in issues) + "</ul>"))
                known = dashboard_ids(root)
                if not did:
                    links = "".join(f'<li><a href="/{d}">{html.escape(t)}</a> <code>{d}</code></li>'
                                    for d, t in known)
                    return self._send(200, _page(f"Project {root.name}", f"<ul>{links}</ul>"))
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
                result = render_dashboard(root, did, inputs, as_of, mode="serve")
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

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"obi: serving {root.as_posix()} at http://127.0.0.1:{port}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<body style='font:14px system-ui;margin:32px;max-width:960px'>"
            f"<h1>{html.escape(title)}</h1>{body}</body>")
