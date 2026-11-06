# Deploying a project: a link to send a colleague

One container serves one project from a Git branch, behind Caddy for HTTPS. Decisions are in [ADR-014](adr/014-shareable-link-first.md). Still DuckDB, still server-rendered.

## What runs where

| Thing | Where | In Git? |
|---|---|---|
| The project: models, dashboards, forms, `access.yaml` | your repository, branch `main` | yes |
| Releases (one checkout per deployed commit, the current and the previous) | `/srv/obi/releases/` on the volume | no |
| Project database (form data, history) | `/srv/obi/data/<project>.duckdb` | never |
| Users (name, password hash, roles, attributes) | `/srv/obi/data/users.json`: an interim store, not a public format; sign-in through an identity provider replaces it later (ADR-014) | never |
| Session key, deploy token | `OBI_SECRET_KEY`, `OBI_DEPLOY_TOKEN`, or generated into `/srv/obi` | never |

A deploy replaces the release, never the data. `obi users` and `obi deploy` work while the server runs.

## Ten minutes to a VPS

You need a Linux VPS with a public IP (1 vCPU and 1 GB RAM are enough for the examples), a DNS name pointing at it (`bi.example.com`), and a repository that holds the project.

**1. Docker** (2 minutes):

```bash
curl -fsSL https://get.docker.com | sh
```

**2. This repository on the server, and the settings** (2 minutes):

```bash
git clone https://github.com/<owner>/bi-platform.git && cd bi-platform
```

```bash
cp deploy/obi.env.example deploy/obi.env && chmod 600 deploy/obi.env
```

Edit `deploy/obi.env`:
- `OBI_GIT_URL`: the project repository. For a private one, use an HTTPS URL with a read-only token. It never appears on pages or in logs.
- `OBI_GIT_PATH`: the project's folder inside it (`examples/erp-sales` to try this repository's example).
- Fill `OBI_SECRET_KEY` and `OBI_DEPLOY_TOKEN` with long random strings.

**3. Start it** (3 minutes, mostly the image build):

```bash
OBI_DOMAIN=bi.example.com docker compose -f deploy/compose.yaml up -d --build
```

Caddy gets a certificate for the domain and forwards to `obi serve` on port 8765. The first start clones the branch, validates it with `--execute`, migrates the database and serves it. Watch it:

```bash
docker compose -f deploy/compose.yaml logs -f obi
```

```text
obi: deployed 3f2a9c1d4e5b from https://github.com/<owner>/bi-platform.git (main)
obi: serving https://github.com/<owner>/bi-platform.git (main) at 3f2a9c1d4e5b at http://127.0.0.1:8765/  (sign-in; users: 0)
```

**4. Users** (1 minute). Roles and attributes are checked against the deployed `access.yaml`:

```bash
docker compose -f deploy/compose.yaml exec obi obi users add ada --role admin --admin
```

```bash
docker compose -f deploy/compose.yaml exec obi obi users add mia --role sales_manager --attr sales_orgs=DE01,PL01
```

Each command asks for the password twice. `--admin` is platform administration: an admin sees a failed deploy's errors on every page. It is not a project role.

There is no `--password` option, because a password on the command line ends up in the shell history and the process list. In a script, pipe it in with `--password-stdin` (`-T`: no terminal, so stdin reaches the command):

```bash
echo 'the password' | docker compose -f deploy/compose.yaml exec -T obi obi users add mia --role sales_manager --attr sales_orgs=DE01 --password-stdin
```

Without Docker the same line is `echo 'the password' | obi users add mia --role sales_manager --attr sales_orgs=DE01 --password-stdin` (add `--project <dir>` or `--home <dir>`).

**5. Send the link** `https://bi.example.com/` (1 minute). Colleagues sign in and see the dashboards and forms their roles allow.

## Deploying a new commit

Push to the branch, then:

```bash
docker compose -f deploy/compose.yaml exec obi obi deploy
```

The server answers at once and deploys in the background; the command waits and prints `deployed <commit>` with the start and finish times, or the validation errors and `still serving <old commit>`. A failed deploy changes nothing that users see. Admins see a banner while a deploy runs, and the errors until a deploy succeeds. `GET /_deploy/status` (with the deploy token, or signed in as an admin) shows the last deploy: commit, started, finished, ok, errors.

**Webhook instead:** in GitHub, add a webhook on push:
- **Payload URL:** `https://bi.example.com/_deploy`
- **Content type:** `application/json`
- **Secret:** the value of `OBI_DEPLOY_TOKEN`

The server checks the `X-Hub-Signature-256` signature and answers 202 within a second, so GitHub never times out; the result is in `/_deploy/status`. From anywhere else, `obi deploy --url https://bi.example.com --token <OBI_DEPLOY_TOKEN>` does the same.

## Operating

- **Scripts and curl:** forms write through `POST /_write`, described in [forms.md](formats/forms.md#writing-over-http-the-_write-protocol): fields, `_action` values (`save`, `transition:submit`, `sheet_add`, ...), and answers. A refused write is a 422 page with the reasons in the `X-Obi-Error` header.

- **Health:** `GET /_health` answers `{"status": "ok", "commit": "<served commit>"}`, or 503 before the first successful deploy. It needs no sign-in, and Docker's `HEALTHCHECK` uses it.
- **Logs:** stdout, one line per request (`2026-11-06T10:15:02 GET /order-to-cash 200 mia 84ms`). The query string is never logged, because it holds filter values. There is one line per deploy.
- **Users:**
  - `obi users list`;
  - `obi users set mia --attr sales_orgs=DE01` (applies at once, without signing out);
  - `obi users password mia` (ends mia's sessions);
  - `obi users remove mia`.
- **Backup:** the `obi` volume, which holds `data/` (database, users). Everything else can be rebuilt from Git.
- **A new platform version:** `git pull && docker compose -f deploy/compose.yaml up -d --build`. The volume stays.

## Locally

The same sign-in works without Docker:

```bash
uv run obi users add mia --project examples/erp-sales --role sales_manager --attr sales_orgs=DE01,PL01
```

```bash
uv run obi serve examples/erp-sales
```

For a Git-backed site on your machine:

```bash
uv run obi serve --git . --branch main --path examples/erp-sales --home .obi-site
```

`--user rob --role sales_rep --attr sales_orgs=DE01` skips sign-in for development and tests. It binds only to 127.0.0.1, and `--host 0.0.0.0` with `--user` is refused.

## Not in this slice

Postgres, the React runtime, SSO/SCIM, a user admin page, several projects per server, and HTTPS inside the container (Caddy does it). See ADR-014.
