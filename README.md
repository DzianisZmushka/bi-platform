# Open BI Platform (working name)

A data platform where every artifact — semantic models, dashboards, forms, reports, pipelines, roles — is a readable text file in Git. See [CLAUDE.md](CLAUDE.md) for the product principles and roadmap, and [docs/adr/](docs/adr/) for the decisions made so far.

**Status:** Phase 0 is complete: formats, validator, thin engine on DuckDB, and `obi validate` / `render` / `serve`. Phase 0 renderer scope and limits: [ADR-007](docs/adr/007-phase-0-renderer.md).

## Tooling: uv only

Python 3.12 and every dependency are managed with [uv](https://docs.astral.sh/uv/). The repo has no `requirements.txt`, `pip install`, Poetry or Pipenv, and none should be added ([ADR-004](docs/adr/004-uv-only-python-tooling.md)).

```bash
uv sync                      # create .venv with Python 3.12 and install all workspace packages
uv run obi validate examples/sales-demo           # static: no data access (CI)
uv run obi validate examples/sales-demo --execute # also run every measure on the data
uv run obi catalog examples/sales-demo            # refresh catalog.json from the data source
uv run obi render examples/sales-demo             # write .obi/render/<dashboard>.html
uv run obi render examples/sales-demo -f channel=Online -f period=last_year
uv run obi serve examples/sales-demo              # http://127.0.0.1:8765, live filters
uv run pytest                # run tests
uv add --package obi-engine sqlglot   # add a dependency to one package
uv run scripts/format_stats.py       # line counts of example dashboards
uv run obi-formats fmt --check examples           # check canonical form (drop --check to rewrite)
```

`uv.lock` is committed. `.python-version` pins 3.12.

## Repository layout

```
packages/
  formats/   obi-formats  — JSON Schemas, canonical serializer, validator (usable standalone)
  engine/    obi-engine   — thin semantic engine: sqlglot + DuckDB, Arrow end-to-end (ADR-001)
  cli/       obi-cli      — `obi` command: validate, render; later download/upload/deploy, MCP
  runtime/                — API, scheduler, auth (Phase 1)
  web/                    — TypeScript/React renderer and builders
examples/
  sales-demo/             — the executable spec: a project with a model and two dashboards
docs/adr/                 — architecture decision records
scripts/                  — repo maintenance scripts
```

## Example project

[examples/sales-demo](examples/sales-demo/) is a complete user project with no setup needed. It reads local CSV files through DuckDB.

| Artifact | Purpose |
|---|---|
| `model/sales.model.yaml` | Star schema: order lines, customers, products, regions, calendar, monthly targets |
| `dashboards/revenue-overview` | Small dashboard: one page with the basics |
| `dashboards/commercial-review` | Deliberately large dashboard: 3 pages, 30+ widgets, nested containers, cross-page navigation, conditional formatting. It checks that the format stays readable at scale. |

Regenerate the sample data (deterministic, standard library only):

```bash
uv run python examples/sales-demo/data/generate.py
```

## The Phase 0 loop: hand a dashboard to any LLM

1. Start the live view: `uv run obi serve examples/sales-demo` and open http://127.0.0.1:8765.
2. Give the LLM the dashboard file plus the model it binds to, for example
   `examples/sales-demo/dashboards/commercial-review/dashboard.yaml` and `examples/sales-demo/model/sales.model.yaml`.
   For best results also include [examples/sales-demo/README.md](examples/sales-demo/README.md) (the vocabulary)
   and [docs/formats/expressions.md](docs/formats/expressions.md). Ask for the whole file back.
3. Paste the answer over the file, refresh the browser. If it is invalid, the page lists each error with the
   element and the fix; paste those back to the LLM.
4. Optionally run `uv run obi-formats fmt examples` to put the file in canonical form, then `git diff`.
