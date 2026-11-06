"""What `obi serve` serves: a local project directory, or a branch of a Git repository (ADR-014).

A Git site keeps everything under one home directory, one volume in a container:

    <home>/repo.git            bare mirror of the branch
    <home>/releases/<commit>   a checkout per deployed commit (current and previous)
    <home>/current             the commit being served, for a restart
    <home>/data/               project database, users.json, secret.key: never touched by a deploy

A deploy runs fetch, check out, validate (with --execute, which migrates the database: additive
only), swap. A failed validation keeps the previous release live; its errors go to admins.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from obi_engine import data_dir, forget, use_data_dir

GIT_TIMEOUT = 300


class DeployError(Exception):
    pass


@dataclass
class Release:
    commit: str  # short
    root: Path   # the project directory inside the checkout
    at: str      # when it went live
    pending: list[str] = field(default_factory=list)  # "not enforced yet" warnings (x-pending)


@dataclass
class Failure:
    commit: str
    errors: list[str]
    at: str


@dataclass
class DeployResult:
    ok: bool
    commit: str | None
    message: str
    errors: list[str] = field(default_factory=list)


@dataclass
class DeployStatus:
    """The latest background deploy, for `GET /_deploy/status` and `obi deploy`. `run` is the
    number of the last deploy request it covers: requests that arrive while one runs are folded
    into one more deploy after it."""
    run: int = 0
    state: str = "idle"  # idle | running | finished
    commit: str | None = None
    started: str | None = None
    finished: str | None = None
    ok: bool | None = None
    message: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise DeployError(f"git {args[0]} failed: {e}") from None
    if p.returncode:
        sub = next(a for i, a in enumerate(args) if not a.startswith("-") and (i == 0 or args[i - 1] != "-C"))
        lines = (p.stderr or p.stdout).strip().splitlines()
        raise DeployError(f"git {sub} failed: {shown_url_in(lines[-1]) if lines else f'exit {p.returncode}'}")
    return p.stdout.strip()


def _rmtree(path: Path):
    def unlock(func, p, _exc):  # Git marks pack files read-only (Windows refuses to delete them)
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onexc=unlock)


def shown_url_in(text: str) -> str:
    """Every URL in `text` without its credentials (git error lines may echo the remote)."""
    return re.sub(r"([a-z][a-z0-9+.-]*://)[^/@\s]*@", r"\1", text)


def shown_url(url: str) -> str:
    """`https://user:token@host/x.git` -> `https://host/x.git`, for pages and logs."""
    return shown_url_in(url or "")


Validate = Callable[[Path], list]  # root -> issues with .severity, .text and str()


class Site:
    """Local: one project directory, validated on every request (edit, refresh, see it)."""

    git = False

    def __init__(self, root: Path):
        self.root = Path(root)
        self.failure: Failure | None = None

    @property
    def data(self) -> Path:
        return data_dir(self.root)

    def release(self) -> Release | None:
        return Release(commit="", root=self.root, at="")

    def deploy(self) -> DeployResult:
        return DeployResult(False, None, "this server serves a local directory; deploy needs `obi serve --git`")


class GitSite(Site):
    """A branch of a repository, validated once per deploy."""

    git = True

    def __init__(self, home: Path, url: str, branch: str = "main", path: str = "", validate: Validate | None = None):
        if url and "://" not in url and not re.match(r"^[\w.-]+@[\w.-]+:", url) and Path(url).exists():
            url = str(Path(url).resolve())  # a local path: git would read it relative to the mirror
        self.home, self.url, self.branch, self.path = Path(home), url, branch, path.strip("/")
        self.shown_url = shown_url(url)  # for pages and logs: never the credentials in the URL
        self.failure = None
        self._validate = validate
        self._current: Release | None = None
        self._previous: Release | None = None
        self._lock = threading.Lock()
        self.log: Callable[[str], None] = lambda line: print(f"obi: {line}", flush=True)
        self.status = DeployStatus()
        self._slock = threading.Lock()
        self._wanted = 0  # deploy requests so far
        self._worker: threading.Thread | None = None

    @property
    def data(self) -> Path:
        return self.home / "data"

    def release(self) -> Release | None:
        return self._current

    # ---- restart: serve what was live before

    def restore(self) -> Release | None:
        """The release that was live before a restart, from `<home>/current` (commit, then the
        project path on a second line, so `obi users --home` needs no OBI_GIT_PATH)."""
        f = self.home / "current"
        if self._current is None and f.is_file():
            commit, _, path = f.read_text(encoding="utf-8").strip().partition("\n")
            if path.strip() and not self.path:
                self.path = path.strip()
            root = self._root(commit.strip())
            commit = commit.strip()
            if (root / "project.yaml").is_file():
                use_data_dir(root, self.data)
                self._current = Release(commit, root, _now())
        return self._current

    def _root(self, commit: str) -> Path:
        checkout = self.home / "releases" / commit
        return checkout / self.path if self.path else checkout

    # ---- deploy in the background (webhooks, `obi deploy`)

    def start_deploy(self) -> int:
        """Ask for a deploy and return at once; the request's number. `status.run` reaches it
        when a deploy that began after this request has finished."""
        with self._slock:
            self._wanted += 1
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="obi-deploy", daemon=True)
                self._worker.start()
            return self._wanted

    def _work(self):
        while True:
            with self._slock:
                if self.status.run >= self._wanted:
                    self._worker = None  # under the lock: a new request starts a new worker
                    return
                upto = self._wanted
                self.status = DeployStatus(run=self.status.run, state="running", started=_stamp())
            try:
                r = self.deploy()
            except Exception as e:  # never leave the status "running"
                r = DeployResult(False, None, f"deploy failed: {e}", [str(e)])
            with self._slock:
                self.status = DeployStatus(run=upto, state="finished", commit=r.commit,
                                           started=self.status.started, finished=_stamp(), ok=r.ok,
                                           message=r.message, errors=r.errors)

    # ---- deploy

    def deploy(self) -> DeployResult:
        with self._lock:  # one deploy at a time; requests keep being served meanwhile
            try:
                return self._deploy()
            except DeployError as e:
                self.failure = Failure("", [str(e)], _now())  # before a commit was known
                self.log(f"deploy failed: {e}")
                return DeployResult(False, None, str(e), [str(e)])

    def _deploy(self) -> DeployResult:
        repo = self.home / "repo.git"
        if not (repo / "HEAD").is_file():
            self.home.mkdir(parents=True, exist_ok=True)
            _git("init", "--quiet", "--bare", str(repo))
            _git("-C", str(repo), "remote", "add", "origin", self.url)
        _git("-C", str(repo), "remote", "set-url", "origin", self.url)  # OBI_GIT_URL may have changed
        _git("-C", str(repo), "fetch", "--quiet", "--prune", "origin",
             f"+refs/heads/{self.branch}:refs/heads/{self.branch}")
        commit = _git("-C", str(repo), "rev-parse", "--short=12", f"refs/heads/{self.branch}")
        if self._current is not None and self._current.commit == commit:
            self.failure = None
            self._mark(commit)
            return DeployResult(True, commit, f"already serving {commit}")
        checkout = self.home / "releases" / commit
        forget(self._root(commit))
        _rmtree(checkout)  # a leftover of an earlier failed attempt
        checkout.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--quiet", "--shared", "--no-checkout", str(repo), str(checkout))
        _git("-C", str(checkout), "checkout", "--quiet", "--detach", commit)
        root = self._root(commit)
        if not (root / "project.yaml").is_file():
            return self._fail(commit, checkout, [f"commit {commit} has no project.yaml at "
                                                 f"`{self.path or '.'}` (OBI_GIT_PATH)"])
        use_data_dir(root, self.data)
        issues = self._validate(root) if self._validate else []
        errors = [str(i) for i in issues if i.severity == "error"]
        if errors:
            return self._fail(commit, checkout, errors)
        pending = [i.text for i in issues if i.severity == "warning" and "is not enforced yet" in i.text]
        # swap: new requests read the new release; requests in flight finish on the old one
        self._previous, self._current = self._current, Release(commit, root, _now(), pending)
        self._mark(commit)
        self.failure = None
        self._prune()
        self.log(f"deployed {commit} from {self.shown_url} ({self.branch})")
        return DeployResult(True, commit, f"deployed {commit}")

    def _mark(self, commit: str):
        (self.home / "current").write_text(f"{commit}\n{self.path}\n", encoding="utf-8", newline="\n")

    def _fail(self, commit: str, checkout: Path, errors: list[str]) -> DeployResult:
        root = self._root(commit)
        forget(root)
        use_data_dir(root, None)
        _rmtree(checkout)
        self.failure = Failure(commit, errors, _now())
        live = self._current.commit if self._current else "nothing"
        self.log(f"deploy of {commit} failed validation ({len(errors)} error(s)); still serving {live}")
        for e in errors:
            self.log(f"  {e}")
        return DeployResult(False, commit, f"{commit} failed validation; still serving {live}", errors)

    def _prune(self):
        keep = {r.commit for r in (self._current, self._previous) if r is not None}
        for d in (self.home / "releases").iterdir():
            if d.is_dir() and d.name not in keep:
                root = d / self.path if self.path else d
                forget(root)
                use_data_dir(root, None)
                _rmtree(d)
