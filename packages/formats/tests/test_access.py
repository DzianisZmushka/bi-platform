"""access.yaml and `access:` (ADR-013), the format: exact validator messages, round-trip
comments, and `user.*` in expressions. Enforcement is tested in packages/cli/tests/test_access_users.py."""

import os
from pathlib import Path

import pytest

from obi_formats.artifacts import canonicalize, parse
from obi_formats.expr import render
from obi_formats.validate import validate_project

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = Path(os.environ["OBI_EXAMPLES"])


def messages(project: str, rel: str, old: str, new: str) -> list[str]:
    root = EXAMPLES / project
    text = (root / rel).read_text(encoding="utf-8")
    assert text.count(old) == 1, f"edit anchor must be unique in {rel}: {old!r}"
    return [i.text for i in validate_project(root, {rel: text.replace(old, new)}) if i.severity != "info"]


@pytest.mark.parametrize("project", ["sales-demo", "erp-sales"])
def test_examples_have_no_access_warnings(project):
    assert [i.text for i in validate_project(EXAMPLES / project) if i.severity != "info"] == []


# --------------------------------------------------------------------------- access.yaml messages

REP_EDIT = "    edit: { forms: [kpi-comment] }"
DEFAULT_VIEW = '    view: { dashboards: ["*"], forms: ["*"] }'


def test_roles_name_entities_that_exist_and_write_only_writable_ones():
    assert messages("sales-demo", "access.yaml", REP_EDIT, REP_EDIT + "\n    write: [target]") == [
        "access `sales-demo`, role `sales_rep`: `write`: no entity `target` in any model; did you mean `targets`?"]
    assert messages("sales-demo", "access.yaml", REP_EDIT, REP_EDIT + "\n    write: [regions]") == [
        "access `sales-demo`, role `sales_rep`: `write`: `regions` is not a writable entity; writable: targets, "
        "kpi_comments"]


def test_artifact_patterns_must_match_something():
    assert messages("sales-demo", "access.yaml", DEFAULT_VIEW,
                    '    view: { dashboards: [revenue-overveiw], forms: ["*"] }') == [
        "access `sales-demo`, role `default`: `view.dashboards`: `revenue-overveiw` matches no dashboard; dashboards "
        "are commercial-review, revenue-overview; did you mean `revenue-overview`?"]
    assert messages("erp-sales", "access.yaml", DEFAULT_VIEW, '    view: { dashboards: [orders-*], forms: ["*"] }') == [
        "access `erp-sales`, role `default`: `view.dashboards`: `orders-*` matches no dashboard; dashboards are "
        "order-to-cash"]


def test_edit_on_a_form_whose_entity_is_denied_for_write():
    """`edit` on a form implies `write` on its entity (ADR-013), so the two cannot contradict."""
    assert messages("sales-demo", "access.yaml", REP_EDIT, REP_EDIT + "\n    deny: { write: [kpi_comments] }") == [
        "access `sales-demo`, role `sales_rep`: `edit` on form `kpi-comment` writes `kpi_comments`, which role "
        "`sales_rep` denies in `deny.write`; remove one of them"]


def test_column_rules_name_real_columns_and_roles():
    assert messages("sales-demo", "access.yaml", "order_lines.unit_cost: { read: [sales_manager, admin] }",
                    "order_lines.unit_cots: { read: [sales_manger, admin] }") == [
        "access `sales-demo`, column `order_lines.unit_cots`: entity `order_lines` has no column `unit_cots`; "
        "did you mean `unit_cost`?",
        "access `sales-demo`, column `order_lines.unit_cots`: `read`: no role `sales_manger` in access.yaml; "
        "did you mean `sales_manager`?"]


def test_user_attributes_are_declared():
    assert messages("erp-sales", "model/erp.model.yaml", "where: sales_org_code in user.sales_orgs",
                    "where: sales_org_code in user.sales_org") == [
        "model `erp`, entity `sales_orgs`: `access.where` uses `user.sales_org`, which access.yaml does not declare; "
        "add it under `attributes`; did you mean `sales_orgs`?"]
    assert messages("sales-demo", "access.yaml", "  regions:\n    type: list\n",
                    "  regions:\n    type: list\n  roles:\n    type: list\n") == [
        "access `sales-demo`, attribute `roles`: `user.roles` is built in; remove it from `attributes`"]


def test_access_where_uses_own_dimensions_and_a_user_attribute():
    assert messages("erp-sales", "model/erp.model.yaml", "where: sales_org_code in user.sales_orgs",
                    "where: sales_org_code = 'DE01'") == [
        "model `erp`, entity `sales_orgs`: `access.where` does not use `user.*`, so every user gets the same rows; "
        "compare a dimension with a user attribute, such as `region in user.regions`"]
    assert messages("erp-sales", "model/erp.model.yaml", "where: sales_org_code in user.sales_orgs",
                    "where: company_code in user.sales_orgs") == [
        "model `erp`, entity `sales_orgs`: `access.where` may use only dimensions of entity `sales_orgs` itself "
        "(joined entities are covered by their own `access:`); `company_code` is not one of sales_org, "
        "sales_org_code"]


def test_by_uses_user_roles_includes_with_one_argument():
    assert messages("erp-sales", "model/erp.model.yaml",
                    "approve: { from: submitted, to: approved, by: \"${user.roles.includes('sales_manager')}\" }",
                    "approve: { from: submitted, to: approved, by: \"${user.roles.includes('a', 'b')}\" }") == [
        "model `erp`, transition `approve`: `by`: use `user.<attribute>.includes(x)` with one argument, such as "
        "`user.roles.includes('sales_manager')`"]


def test_join_column_that_access_runs_through_is_required():
    assert messages("sales-demo", "model/sales.model.yaml",
                    "      region_id: { label: Region, type: string, required: true, references: region }\n"
                    "      month: { type: date, required: true }\n      comment:",
                    "      region_id: { label: Region, type: string, references: region }\n"
                    "      month: { type: date, required: true }\n      comment:") == [
        "model `sales`, entity `kpi_comments`: `region_id` must be `required: true`: access reaches `kpi_comments` "
        "through it (the join to `regions`); a row without it would be visible to nobody but `rows: all`"]


def test_fact_without_a_path_to_an_access_rule_is_a_warning():
    assert messages("sales-demo", "model/sales.model.yaml",
                    "    joins: { calendar: return_date, order_lines: [order_id, line_no] }",
                    "    joins: { calendar: return_date }") == [
        "model `sales`, entity `returns`: not covered by any access rule: `returns` has measures but no join path to "
        "an entity with `access:`, so every user sees all of its rows; join it to one, or add `access:` to it"]


def test_warnings_for_unwritable_form_entities_and_rows_all_on_default():
    root = EXAMPLES / "sales-demo"
    text = (root / "access.yaml").read_text(encoding="utf-8")
    nobody = text.replace('    edit: { forms: ["*"] }\n', "    edit: { forms: [kpi-comment] }\n")
    warnings = [i.text for i in validate_project(root, {"access.yaml": nobody}) if i.severity == "warning"]
    assert warnings == ["access `sales-demo`: form `targets` writes `targets`, but no role may edit it or write "
                        "`targets`; add it to a role's `edit`"]
    everyone = text.replace("  default:\n", "  default:\n    rows: all\n")
    warnings = [i.text for i in validate_project(root, {"access.yaml": everyone}) if i.severity == "warning"]
    assert warnings == ["access `sales-demo`, role `default`: `rows: all` on `default` lets every user see every row, "
                        "so no `access:` condition applies; give it to a role instead"]


def test_access_where_without_access_yaml_is_an_error(tmp_path):
    import shutil

    root = tmp_path / "p"
    shutil.copytree(EXAMPLES / "sales-demo", root, ignore=shutil.ignore_patterns(".obi"))
    (root / "access.yaml").unlink()
    assert [i.text for i in validate_project(root) if i.severity == "error"] == [
        "model `sales`, entity `regions`: `access.where` needs access.yaml at the project root, with `regions` under "
        "`attributes`"]


# --------------------------------------------------------------------------- canonical form

SALES_ACCESS = "examples/sales-demo/access.yaml"
ERP_ACCESS = "examples/erp-sales/access.yaml"


@pytest.mark.parametrize("rel, kind, line", [
    (SALES_ACCESS, "access", "access: sales-demo"),
    (SALES_ACCESS, "access", "  regions:"),
    (SALES_ACCESS, "access", "    type: list"),
    (SALES_ACCESS, "access", "  default:"),
    (SALES_ACCESS, "access", DEFAULT_VIEW),
    (SALES_ACCESS, "access", '    read: ["*"]'),
    (SALES_ACCESS, "access", REP_EDIT),
    (SALES_ACCESS, "access", "    rows: all"),
    (SALES_ACCESS, "access", "  order_lines.unit_cost: { read: [sales_manager, admin] }"),
    (ERP_ACCESS, "access", "    deny:"),
    (ERP_ACCESS, "access", '      write: ["*"]'),
    ("examples/erp-sales/model/erp.model.yaml", "model", "    access: { where: sales_org_code in user.sales_orgs }"),
    ("examples/sales-demo/model/sales.model.yaml", "model", "      region_code: { column: region_id }"),
])
def test_comments_on_access_properties(rel, kind, line):
    """CLAUDE.md: each new property keeps a full-line comment above it and a trailing comment on its line."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert line + "\n" in text
    indent = line[:len(line) - len(line.lstrip())]
    commented = text.replace(line + "\n", f"{indent}# above\n{line}  # c\n", 1)
    out = canonicalize(commented, kind)
    assert f"{indent}# above\n{line}  # c\n" in out and canonicalize(out, kind) == out


def test_comment_on_a_role_written_inline():
    src = "access: p\nroles:\n  # above\n  viewer: { description: Reads. }  # c\n"
    out = canonicalize(src, "access")
    assert "  # above\n  viewer: { description: Reads. }  # c\n" in out and canonicalize(out, "access") == out


def test_folded_string_reads_as_plain_spaces():
    """`>-` folds lines into spaces; the loader must not keep ruamel's internal fold markers (found
    while writing access.yaml: the description came out with control characters)."""
    src = "access: p\ndescription: >-\n  one two\n  three\nroles:\n  default:\n"
    assert parse(src).data["description"] == "one two three"
    assert "\a" not in canonicalize(src, "access")


# --------------------------------------------------------------------------- user.* in expressions


@pytest.mark.parametrize("user, expected", [
    ({"roles": ["sales_rep", "sales_manager"]}, True),
    ({"roles": ["sales_rep"]}, False),
    ({}, False),  # no roles: includes nothing
])
def test_user_roles_includes(user, expected):
    assert render("${user.roles.includes('sales_manager')}", {"user": user}) is expected
