# ADR-011: Forms before the runtime

- Status: Accepted
- Date: 2026-11-06
- Changes the delivery order in CLAUDE.md: Phase 1 = forms (write-back), Phase 2 = runtime.

## Context

Phase 0 is closed (ADR-001 to ADR-010). It proved the core method for models and dashboards: a readable YAML format, a validator with exact messages, execution on DuckDB, `obi serve`, and a round trip through an external LLM. The original plan put the runtime next (web app, Git-backed deploys, Postgres, roles), with forms after it.

Forms that write into model entities are the platform's main differentiator. Dashboards over a semantic model exist in many products. Business users entering targets, comments and master data into ordinary tables, which the same model reads back immediately, mostly do not. That makes forms the riskiest part of the format, and the most valuable to get right early.

## Decision

Forms come first, as **Phase 1**. The runtime moves to **Phase 2**.

Forms are proven with the Phase 0 method, unchanged:
- format first: sample YAML, JSON Schema, round-trip tests, exact validator messages;
- then `obi migrate` and the renderer;
- on DuckDB, now a local database file, because written data must survive restarts;
- in `obi serve`;
- then an external-LLM round trip on the form and model files.

Scope is "forms that write to model entities, nothing more". The design is in ADR-012.

## Consequences

- **What Phase 1 can reuse:** the validator, the engine, the server-side renderer (ADR-007) and the LLM test harness. Forms need no web app.
- **What waits for the runtime:** authentication, real users and `roles.yaml`. Phase 1 takes the user from `obi serve` options, which is enough to exercise defaults from `${user.*}` and workflow rules that name user attributes. The form format must not need to change when `roles.yaml` arrives (ADR-012).
- **Write path:** last-write-wins with full history. Real concurrency control is out of scope.
- **Old phase numbers:** ADR-001…010 say "Phase 1" for the runtime. Read that as Phase 2; those records are not rewritten.
