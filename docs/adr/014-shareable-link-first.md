# ADR-014: A link to send a colleague first (Phase 2b, slice 1)

- Status: **Accepted** (2026-11-06)
- Date: 2026-11-06
- Slices CLAUDE.md's Phase 2b (deploy, Postgres, sign-in, React; ADR-013). The runbook is [docs/deploy.md](../deploy.md).

## Context

After 2a the platform enforces roles and rows, but only on one machine: `obi serve --user rob` signs everyone in as rob, on 127.0.0.1. Phase 2b bundles four things: Git-backed deploy, sign-in, Postgres and the React runtime. Only the first two make the product shareable. Postgres and React change how it runs, not who can use it.

## Decision

### 1. Slice 2b: deploy and sign-in first

- **Slice 1** is "a link I can send to a colleague": sign-in, a Git-backed project, deploy, one container. Still DuckDB, still the server-side renderer (ADR-007).
- **Postgres and React wait** until someone other than the owner has written through that link. Until then, nobody knows which of their problems are real (concurrency, latency, layout) and which are guesses.

### 2. Users live in a store next to the project database, never in Git

- **The store** is `users.json` in the data directory, next to the project database: name, scrypt password hash, `roles`, `attributes`, and an `admin` flag.
- **Same shape as access.yaml:** roles and attributes are exactly what access.yaml declares. A user becomes `{name, roles, <attribute>: value}`, the dictionary `--user/--role/--attr` produced in 2a. So an identity provider can replace the store later without touching access.yaml.
- **`obi users add|set|password|remove|list`** manages it.
  - Roles must exist in access.yaml.
  - Attributes must be declared there, and list attributes are split on commas, as for `--attr`.
  - The password comes from a prompt or `--password-stdin`, never from an argument, which would be visible in the process list.
- **`admin` is platform administration, not a project role:** it shows failed deploys. A role in access.yaml says what a user may do in the project. Being an admin is about the server, so it lives with the identity.
- **The identity is read from the store on every request,** so a role change or a removal applies at once, without signing out.
- **`users.json` is an interim store, not a public contract.** Its file format may change without notice, and nothing outside `obi_runtime.users` and `obi users` may read or write it. Sign-in through an identity provider replaces it in a later slice. That changes where a user's roles and attributes come from, not their shape, so access.yaml stays as it is.

### 3. Sessions: a signed cookie

- **Contents:** the cookie carries the user's name, an expiry (12 hours) and a fingerprint of the password hash, signed with HMAC-SHA256.
  - The key is `OBI_SECRET_KEY`, or `secret.key` generated in the data directory.
  - A password change ends every session of that user.
- **Flags:** `HttpOnly`, `SameSite=Lax`, and `Secure` when the reverse proxy says the request came over HTTPS (`X-Forwarded-Proto: https`). `SameSite=Lax` keeps a cross-site POST to `/_write` from carrying the cookie.
- **A wrong name and a wrong password** get the same answer: "Wrong name or password."
- **`--user` stays for local development and tests,** and only on a loopback address: `obi serve --host 0.0.0.0 --user rob` is refused, because it would sign every visitor in as rob.

### 4. Data lives outside the checkout

- **A deploy replaces the checkout,** so a Git site keeps its data in `<home>/data` (`--home`, or `OBI_HOME`), beside the releases.
- **`database: .obi/erp-sales.duckdb` in project.yaml** then resolves to `<home>/data/erp-sales.duckdb`: the file name is kept and the directory is the data directory. A local `obi serve` keeps `<project>/.obi`.
- **A deploy never writes the data directory** except through `migrate`, which only creates tables and adds columns.

### 5. Deploy runs inside the server

- **Why inside:** DuckDB allows one process per database file, so a second process could not validate or migrate while the server runs. `obi deploy` and a Git webhook both `POST /_deploy` with a token (`OBI_DEPLOY_TOKEN`). GitHub's signature header (`X-Hub-Signature-256`) is accepted with the same secret.
- **It runs in the background:** `POST /_deploy` answers 202 at once, because a webhook waits a few seconds at most (GitHub: 10) and a validation with `--execute` can take longer.
  - **The result goes to `GET /_deploy/status`** (the last commit, started and finished, ok, message, errors) and to the admin banner. The status needs the deploy token or an admin session.
  - **`obi deploy` from the CLI waits** for the status and prints it.
  - **One deploy at a time:** requests that arrive while one runs are folded into one more deploy after it. Each request gets a number, and the status says up to which request it is done.
- **The steps:**
  1. **Fetch** the branch into a bare mirror (`<home>/repo.git`).
  2. **Check out** the commit as a release (`<home>/releases/<commit>`).
  3. **Validate** with `--execute`, which brings the database up to the model.
  4. **Swap:** new requests use the new release. Requests in flight finish on the old one.
- **A failed validation keeps the previous release live.** Its errors are shown only to admins, on every page, with the commit. Every other user sees the previous version with no banner.
- **Migration happens before the measures are run on the data,** and a commit can fail at that point. That is safe because migration is additive: the old release ignores a new column or table.
- **Old releases:** the current and the previous one are kept, so requests still running on the previous one finish. Older ones are deleted.
- **Two releases share the database.** One DuckDB connection per database file serves both, and each release has its own schema in memory for its source tables.
- **A restart deploys the branch head,** with full validation. Only when that fails (no network, a broken head) does it serve the release recorded in `<home>/current`, the last one that went live. `current` also records the project path, so `obi users --home` needs no `OBI_GIT_PATH`.
- **In Git mode the served release is validated once, at deploy.** A local `obi serve` still validates on every request, so editing YAML and refreshing shows the change.

### 6. One container

- **The image:** one Dockerfile (`uv sync`, then `obi serve`). The settings are environment variables: `OBI_GIT_URL`, `OBI_GIT_BRANCH`, `OBI_GIT_PATH` (the project inside the repository), `OBI_HOME` (releases and data, one volume), `OBI_SECRET_KEY` and `OBI_DEPLOY_TOKEN`.
- **`GET /_health`** answers without sign-in: status and the served commit, never errors or user names.
- **Logs** go to stdout: one line per request (method, path, status, user, milliseconds) and one per deploy.
- **HTTPS is the reverse proxy's job** (Caddy in the runbook).

## Consequences

- **Not in this slice (non-goals):** Postgres, React, SSO/SCIM, a user admin UI, several projects per server, HTTPS termination.
- **No database DDL on sign-in:** the user store is a JSON file, written atomically, so `obi users add` works while the server runs. DuckDB could not offer that, because the server holds its lock.
- **Where the code goes:** user store and deploy are in `packages/runtime` (`obi_runtime`), as CLAUDE.md's layout says (auth, scheduler, API). The HTTP server stays in `obi_cli/server.py` until React replaces the presentation layer (ADR-007).
- **Tests** (`packages/cli/tests/test_signin.py`, `test_deploy.py`):
  - sign-in is required;
  - a wrong password is refused;
  - store users drive the same four-user behaviour as `--user`;
  - a broken commit leaves the previous one serving;
  - a deploy never alters the data directory's rows.
