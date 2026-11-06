"""A real `obi serve` on an ephemeral port, and a browser-like client that keeps the session
cookie and does not follow redirects (tests assert where they point)."""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from http.cookies import SimpleCookie
from urllib.parse import urlencode

from obi_cli.server import App, make_server


@contextmanager
def running(app: App):
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


class Client:
    def __init__(self, port: int):
        self.port = port
        self.cookie: str | None = None

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        h = dict(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        con.request(method, path, body=body, headers=h)
        r = con.getresponse()
        text = r.read().decode("utf-8")
        jar = SimpleCookie()
        for v in r.headers.get_all("Set-Cookie") or []:
            jar.load(v)
            if "obi_session" in jar:
                value = jar["obi_session"].value
                self.cookie = f"obi_session={value}" if value else None
        con.close()
        return r.status, r.headers, text

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, fields: dict | None = None, headers: dict | None = None):
        data = urlencode(fields or {}, doseq=True).encode()
        return self.request("POST", path, data, {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})

    def sign_in(self, name: str, password: str):
        return self.post("/_signin", {"name": name, "password": password, "next": "/"})

    def deploy(self, token: str, wait: float = 300):
        """POST /_deploy, then wait as `obi deploy` does: (POST status, final deploy status)."""
        import time
        auth = {"Authorization": f"Bearer {token}"}
        status, _, text = self.request("POST", "/_deploy", b"", auth)
        accepted = json.loads(text)
        if status != 202:
            return status, accepted
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            _, _, text = self.request("GET", "/_deploy/status", None, auth)
            st = json.loads(text)
            if st["state"] == "finished" and st["run"] >= accepted["run"]:
                return status, st
            time.sleep(0.1)
        raise AssertionError("the deploy did not finish")
