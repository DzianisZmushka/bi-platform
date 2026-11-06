# ADR-013: Roles and row-level security before deploy (Phase 2a / 2b)

- Status: **Accepted** (2026-11-06), after review of the format; enforced in the engine and on the server. The format reference is [docs/formats/access.md](../formats/access.md).
- Date: 2026-11-06
- Splits CLAUDE.md's Phase 2 into 2a (roles and RLS) and 2b (deploy, Postgres, sign-in, React).

## Context

Phase 1 (forms) closed at 5c7c0d8. The runtime phase bundled two kinds of work:
- **Changes to the format:** access rules, row-level security on model entities, restrictions on forms and columns. Every project's files are affected, and so is what an LLM must write.
- **Infrastructure:** Git-backed deploy from a branch, Postgres, sign-in with an identity provider, the React runtime. These change how artifacts are served, not what they say.

## Decision

### 1. Split Phase 2; roles come first

- **Roles change the format; Git deploy does not.** Format changes are the expensive ones, because every example, schema, validator message and LLM test depends on them.
- **RLS is query-engine logic, independent of the database.** The engine adds the conditions to the SQL it generates, so DuckDB proves it as well as Postgres would. Postgres waits for 2b.
- **2a runs with the Phase 0/1 method:** format, validator, engine on DuckDB, `obi serve --user <name> --role <role> --attr <name>=<value>`, then an LLM round trip.

### 2. `access.yaml`: roles are permission sets; users are never in Git

The file is `access.yaml` and starts with `access: <project id>`: the kind key matches the file name, like every other artifact. Users and role membership come from `obi serve` options now and an identity provider later. Nothing in the file names a user, so a `/users` admin page or an IdP mapping can be added without changing it.

**Permissions per role:**
- `view` and `edit` of dashboards and forms (sheets are forms), by id or `*` glob;
- `read` and `write` of entities;
- `rows: all`;
- `deny` with the same shape.

**Combining roles:**
- Allow is the union of `default` and the user's roles, minus every `deny` of those roles: deny wins.
- In both examples `default` views every dashboard and form and reads every entity. The other roles then declare only `edit`, `write`, `rows` and `deny`, and restricting what someone sees is done with `deny`.

**Implied permissions:**
- `edit` on a form implies `view` of it and `write` on its entity.
- `write:` is for writes that bypass forms (the store today, an API later), and implies `read`.
- A role cannot `edit` a form whose entity it, or `default`, denies for writing: validator error.

**Column restrictions are column-centric:** `columns: { <entity>.<column>: { read: [roles], write: [roles] } }`. With deny-wins, "only HR reads salaries" cannot be written as per-role denies: a user who is both a sales rep and in HR would lose the column.

**`attributes` declare every `user.*` the model uses**, so the IdP mapping is explicit.
- They carry **keys, never display values**: `sales_orgs=DE01`, `regions=emea`. Display names change; keys do not.
- `user.name` and `user.roles` are built in.

### 3. Row-level security on the entity

```yaml
sales_orgs:
  access: { where: sales_org_code in user.sales_orgs }
```

**The condition** compares dimensions **of the entity itself** with `user.*`; a dimension of a joined entity is a validator error. Joined entities are covered by their own `access:`, and an own-dimension rule needs no join: the engine turns it into one `WHERE` on the entity's table, and chains entities by keys. A condition that also reached through joins would need its own join planning inside every restricted subquery, and two rules could then disagree about the same row.

**The pattern** is to put it on the smallest master-data entity every fact joins (`regions`, `sales_orgs`), not on the facts. A fact with measures and no join path to an entity with `access:` gets the warning "not covered by any access rule".

**How it is enforced:** every read goes through `Engine.table(entity)`, which returns the user's rows as a subquery.
- The subquery holds the entity's own `access:` condition, plus "the foreign key is among the parent's visible keys" for each join toward a covered entity (recursively) and for a `per` parent.
- So the rule limits the entity and every entity that joins it, whether or not the query names the restricted entity.
- It applies to queries, rollups, filter choices, record text, form edit-mode loads and sheet rows.
- `per`'s reverse join (order item → note) carries no condition: a note does not limit its item.

**Writes:** the store reads through the same subquery. A hidden row cannot be edited, deleted, transitioned or shown in history. A new or changed row must satisfy the same condition, evaluated on its values.

**A hidden row is "not found", never "outside your rows".** The store answers a key the user cannot see exactly as it answers a key that does not exist: the parent check says there is no such row, a `references` value is not a known choice, an edit or delete finds nothing. Saying "outside your rows" would confirm that the row exists, which is itself information (an order number in another sales org). The message "this row is outside the rows <user> may write" appears only where the row is already the user's own: a visible key whose new values would move it outside.

**Join columns that access runs through are required.** On a writable entity, a join column toward a covered entity must be `required: true` (validator error). A row without it joins none of anyone's rows, so it would be visible to nobody but `rows: all`. `kpi_comments.region_id` is required for this reason.

**No rows, never all rows:**
- A missing attribute, or an empty list, gives no rows.
- So does an empty join column.
- `rows: all` on a role, and the engine without a user (`obi render`, `obi validate --execute`), see every row. `obi serve` always has a user.

### 4. One `user.*`

- Workflow `by:`, column defaults and `access:` read the same user. `by: "${user.roles.includes('sales_manager')}"`.
- `obi serve` takes `--role` (repeatable) and `--attr`. List attributes are comma-separated.

### 5. The server decides, the page reflects

- A dashboard the user may not view is not listed, and opening it returns 403.
- A form widget the user may not view is omitted.
- A form the user may view but not edit is read-only.
- A widget that needs an entity or column the user may not read shows "No access" (the engine raises `NoAccess` before running the query).
- Every POST checks `edit` on the form, then `write`, column grants and rows in the store, whatever the page showed. Each `x-write-rule` property has a refusal test in `test_write_rules.py`.

### 6. Accepted before enforced: `x-pending`

**The conflict:** CLAUDE.md requires every accepted property to have runtime behaviour, or to be marked `x-unsupported`, which is an error. The format review came before the engine, and "accepted but ignored" is the worst failure for access rules.

**The third state:** `x-pending` means accepted and checked, not enforced yet.
- Every use gets a validator warning.
- `obi serve` shows the banner "Access rules declared but not enforced" on every page while one is in use.
- `test_coverage.py` accepts it in place of a registry entry.

**Lifecycle:** `x-pending` is removed when the subsystem it marks closes. It was removed from every access property when 2a's enforcement landed in this round, so no property is pending today. A test injects one to prove that the warning and the banner still work.

## Consequences

- **Size:** `access.yaml` is 29 lines in sales-demo and 32 in erp-sales. RLS costs one model line plus one key dimension per project.
- **Tests:** four users per example (admin, regional rep, manager, viewer) in `packages/cli/tests/test_access_users.py`.
  - Totals by region or sales org equal hand-written SQL with the user's `WHERE`, for facts reached directly and through other entities, and for a writable entity.
  - Writes outside a user's rows are refused.
  - The history records who wrote.
  - `--role sales_rep --attr sales_orgs=DE01` sees only DE01.
- **Folded strings fixed:** writing the samples exposed a loader bug. A folded YAML string (`>-`) kept ruamel's fold markers as control characters. Fixed and tested.

**Not done in 2a (non-goals):** sign-in, SCIM, column masking, audit-log export, roles edited in a UI.
