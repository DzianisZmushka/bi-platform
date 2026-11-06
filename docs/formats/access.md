# Roles and row-level security

Who may see and write what, declared in code and enforced on the server. Decisions are in [ADR-013](../adr/013-roles-before-deploy.md). The examples are [sales-demo/access.yaml](../../examples/sales-demo/access.yaml) and [erp-sales/access.yaml](../../examples/erp-sales/access.yaml).

Two places hold the rules:
- **`access.yaml`** at the project root: what each role may open, edit, read and write.
- **`access:`** on model entities: which rows a user may see and write.

Users and their roles are never in Git. They sign in with a name and password from the user store next to the project database ([deploy.md](../deploy.md), ADR-014):

```bash
uv run obi users add mia --project examples/erp-sales --role sales_manager --attr sales_orgs=DE01,PL01
```

`obi users` checks every role and attribute against access.yaml. For local development, `obi serve` can skip sign-in and act as one user:

```bash
uv run obi serve examples/erp-sales --user rob --role sales_rep --attr sales_orgs=DE01
```

`--role` can be repeated. A list attribute takes comma-separated values (`--attr sales_orgs=DE01,PL01`). Both give the user the same shape: `user.name`, `user.roles` and the attributes. Without a user (`obi render`, `obi validate --execute`) the engine runs as the system and sees every row. `obi serve` always has a user.

## `access.yaml`

```yaml
access: erp-sales
description: Who may see and write what.

attributes:
  sales_orgs:
    type: list
    description: Sales organisations (VKORG, such as DE01) the user works for.

roles:
  default:
    view: { dashboards: ["*"], forms: ["*"] }
    read: ["*"]
  sales_rep:
    edit: { forms: [order-note] }
  auditor:
    deny:
      edit: { forms: ["*"] }
      write: ["*"]
  admin:
    edit: { forms: ["*"] }
    rows: all

columns:
  order_items.WAVWR: { read: [sales_manager, admin] }
```

**Top level:**
- `access: <project id>`, then an optional `description`.
- `attributes`, `roles` and `columns`, described below.

**`attributes`:** every user attribute the model reads as `user.<name>`, so the mapping from an identity provider is explicit.
- Each takes `type` (`string` by default, `number`, or `list` for several values) and a `description`.
- **Attributes carry keys, never display values:** `sales_orgs` holds `DE01`, not "Germany", and `regions` holds `emea`, not "EMEA". Give the entity a dimension on its key column for `access.where` to compare with (`sales_org_code: { column: VKORG }`, `region_code: { column: region_id }`).
- `user.name` and `user.roles` always exist and are not listed.
- A user without an attribute that an `access:` condition needs sees no rows, never all rows.

**`roles`:** named sets of permissions.
- `default` applies to every user. In both examples it views every dashboard and form and reads every entity, so the other roles declare only what they add (`edit`, `write`, `rows`) or take away (`deny`).
- A user's permissions are the union of `default` and their own roles, minus every `deny` of those roles.

| Property | Meaning |
|---|---|
| `description` | What the role is for. |
| `view` | `{ dashboards: [...], forms: [...] }`: what the role may open. `forms` covers sheets. A form opened without `edit` is read-only. |
| `edit` | `{ forms: [...] }`: forms and sheets the role may submit. Implies `view`, and `write` on the form's entity. |
| `read` | Entities whose data the role may read, by name or `"*"`. A widget that needs an entity the user may not read shows "no access" in place of numbers. |
| `write` | Writable entities the role may write **without a form** (the store, later an API). Writes through a form need only `edit` on it. Implies `read`. |
| `rows: all` | The entities' `access:` conditions do not apply (administrators). |
| `deny` | The same `view`, `edit`, `read` and `write`, taken away even when another role of the user allows it. To keep a dashboard from someone, deny it: `deny: { view: { dashboards: [finance-*] } }`. |

**Hiding a form does not hide its entity's data.** `deny: { view: { forms: [shipment-plan] } }` leaves out the sheet, but every dashboard widget built on `shipment_plans` still shows the numbers. To keep the data from a role, deny the entity as well: `read: [shipment_plans]` under the same `deny`. The validator warns when a role denies view of a form whose entity it may still read.

**Patterns.** Artifact ids may use `*`: `[order-*]`, `["*"]`. Quote a lone `"*"`, because YAML reads a bare `*` as an alias.

**`columns`:** `<entity>.<column>: { read: [roles], write: [roles] }`.
- Only the listed roles may read or write that column.
- Measures and dimensions that use an unreadable column show "no access".
- A write that sets or changes a column the user may not write is refused.
- Columns not listed follow their entity.

## `access:` on an entity (row-level security)

```yaml
sales_orgs:
  table: tvko
  key: VKORG
  access: { where: sales_org_code in user.sales_orgs }
  dimensions:
    sales_org: { column: VTEXT }
    sales_org_code: { column: VKORG }
```

**The pattern: put `access:` on the smallest master-data entity that every fact joins**, here `sales_orgs`, and `regions` in sales-demo. Do not put it on the facts.
- One line then covers orders, items, deliveries, billing, notes and the shipment plan, because each reaches `sales_orgs` through its joins.
- A fact with measures that reaches no entity with `access:` gets a validator warning ("not covered by any access rule").

**`where`** compares dimensions **of the entity itself** with `user.*` attributes. Use `=` for a single value and `in` for a list attribute.

**Reads.** The condition limits:
- the entity's own rows;
- the rows of every entity that joins it, directly or through other entities, whether or not a query names it;
- a `per` entity, through its parent.

It applies to every read: queries, rollups, measures of joined entities, filter choices, record text, form edit-mode loads and sheet rows.

**Writes.** A user may not:
- create a row outside their rows;
- move a row out of them;
- edit, delete, transition or see the history of a row they cannot see.

**Required join columns.** On a writable entity, a join column that access runs through must be `required: true` (validator error). A row without it would join none of anyone's rows and be visible to nobody but `rows: all`. That is why `kpi_comments.region_id` is required.

**Hidden rows are "not found".** A row the user cannot see is answered exactly like a row that does not exist: an edit or delete finds nothing, a parent check says there is no such row, and a `references` value is not a known choice. "Outside the rows you may write" appears only when the user's own values would leave their rows. This avoids revealing that a hidden row exists.

**Lifting it.** `rows: all` on a role lifts the condition.

## The same `user.*` everywhere

Workflow `by:`, column defaults and `access:` read one user:

```yaml
approve: { from: submitted, to: approved, by: "${user.roles.includes('sales_manager')}" }
```

`user.roles` is a list, so it takes `.includes(x)`, as do list attributes.

## On the page

The server decides; the page shows the result.
- A dashboard the user may not view is not listed, and opening it gives "no access".
- A form or sheet widget the user may not view is left out.
- A form the user may view but not edit is read-only.
- A field or sheet column whose column the user may not write (`columns: { <entity>.<column>: { write: [...] } }`) is read-only.
- A widget that needs an entity or column the user may not read shows "No access" in place of numbers.
- Every write is checked again on the server, whatever the page showed (`packages/cli/tests/test_write_rules.py`).

## What the validator checks

- Every role named in `columns` exists.
- Every entity named in `read`, `write` or `deny` exists, and `write` names only writable entities.
- Every dashboard and form pattern matches at least one artifact.
- Every column exists.
- Every `user.*` that `access.where`, `by:` or a column `default:` uses is declared under `attributes`.
- `access.where` uses only the entity's own dimensions and at least one `user.*`.
- On a writable entity, every join column that access runs through is `required: true`.
- A role may not `edit` a form whose entity it, or `default`, denies in `deny.write`.
- Warnings:
  - a writable entity that a form writes, but no role may edit or write;
  - `rows: all` on `default`;
  - a fact not covered by any access rule;
  - a role that denies view of a form while it may still read the form's entity.
