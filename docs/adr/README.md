# Architecture decision records

Short records: context, decision, consequences. A record is never edited after acceptance except for its status. A change of mind gets a new ADR that supersedes the old one.

| # | Title | Status |
|---|---|---|
| [001](001-thin-semantic-engine-for-phase-0.md) | Thin semantic engine on sqlglot + DuckDB for Phase 0 | Accepted, join-path rule amended by 009 |
| [002](002-artifact-format-conventions.md) | Artifact format conventions | Accepted, amended by 005, 006 |
| [003](003-canonical-serialization.md) | Canonical YAML serialization | Accepted, §7 refined by 006 |
| [004](004-uv-only-python-tooling.md) | uv as the only Python tool | Accepted |
| [005](005-references-grain-and-filter-scope.md) | References, grain and filter scope | Accepted |
| [006](006-serializer-and-comment-preservation.md) | Serializer and comment preservation | Accepted |
| [007](007-phase-0-renderer.md) | Phase 0 renderer: server-side HTML from the CLI | Accepted |
| [008](008-findings-of-the-llm-model-edit-test.md) | Findings of the LLM model-edit test | Accepted |
| [009](009-erp-sources-joins-currency-fiscal.md) | ERP sources: joins, currency, fiscal calendar, rollups, lookups | Accepted |
| [010](010-snapshot-measures.md) | Snapshot measures (state at a date) | Proposed, Phase 2 (runtime) |
| [011](011-forms-before-runtime.md) | Forms before the runtime (Phase 1 = forms) | Accepted |
| [012](012-forms-and-writable-entities.md) | Forms and writable entities | Accepted |
| [013](013-roles-before-deploy.md) | Roles and row-level security before deploy (Phase 2a / 2b) | Accepted |
| [014](014-shareable-link-first.md) | A link to send a colleague first (Phase 2b, slice 1) | Accepted |
