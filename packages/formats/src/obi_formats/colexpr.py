"""Column expressions in models: `sum:`/`count:`/... bodies, `where:` and `calc:` (ADR-008).

They are parsed with sqlglot and checked against an explicit allowed set. Nothing goes to
the database unchecked. What a name means depends on where the expression is:

- aggregation bodies (`sum:` ...): columns of the entity's own table, unprefixed;
- `where:`: model dimensions, bare or entity-prefixed, with an optional time grain;
- `calc:`: measures.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

# Every syntactic feature the parser accepts. packages/engine/tests/test_column_expressions.py
# runs each one on DuckDB, and test_coverage.py checks that no feature lacks such a test.
FEATURES: dict[str, tuple[type[exp.Expression], ...]] = {
    "arithmetic": (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg),
    "parentheses": (exp.Paren,),
    "comparison": (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE),
    "in": (exp.In,),
    "between": (exp.Between,),
    "is_null": (exp.Is,),
    "boolean": (exp.And, exp.Or, exp.Not),
    "literal": (exp.Literal, exp.Null, exp.Boolean),
    "year": (exp.Year,),
    "month": (exp.Month,),
    "quarter": (exp.Quarter,),
    "date_diff": (exp.DateDiff,),
    "coalesce": (exp.Coalesce,),
    "case": (exp.Case, exp.If),
    "reference": (exp.Column, exp.Identifier),
}
FUNCTIONS = ("year", "month", "quarter", "date_diff", "coalesce", "case")
DATE_DIFF_UNITS = ("day", "week", "month", "quarter", "year")
_ALLOWED = tuple(cls for classes in FEATURES.values() for cls in classes)


class ColExprError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def parse(text: str) -> exp.Expression:
    """Parse and check against the allowed set. Raises ColExprError with a readable message."""
    try:
        tree = sqlglot.parse_one(str(text), read="duckdb")
    except Exception as e:  # sqlglot ParseError / TokenError
        col = re.search(r"Col: (\d+)", str(e))
        where = f" (near character {col.group(1)})" if col else ""
        raise ColExprError(f"`{text}` is not a valid expression{where}") from None
    for node in tree.walk():
        if isinstance(node, exp.Var) and isinstance(node.parent, exp.DateDiff) and node.arg_key == "unit":
            if node.name.lower() not in DATE_DIFF_UNITS:
                raise ColExprError(f"date_diff unit `{node.name.lower()}` is not supported; use one of "
                                   f"{', '.join(DATE_DIFF_UNITS)}")
            continue
        if isinstance(node, exp.Literal) and isinstance(node.parent, exp.DateDiff) and node.arg_key == "unit":
            continue
        if not isinstance(node, _ALLOWED):
            if isinstance(node, exp.Func):
                name = node.sql_name().lower() if hasattr(node, "sql_name") else type(node).__name__.lower()
                raise ColExprError(f"function `{name}` is not supported in column expressions; "
                                   f"use {', '.join(FUNCTIONS)}, arithmetic, comparisons and and/or/not")
            raise ColExprError(f"`{node.sql('duckdb')}` is not supported in column expressions; "
                               f"use {', '.join(FUNCTIONS)}, arithmetic, comparisons and and/or/not")
    return tree


def references(tree: exp.Expression) -> list[tuple[str, exp.Column]]:
    """Every name used, as written: `segment`, `customers.segment`, `date.month`."""
    out = []
    for col in tree.find_all(exp.Column):
        parts = [p for p in (col.args.get("catalog"), col.args.get("db"), col.args.get("table"), col.this)
                 if p is not None]
        out.append((".".join(p.name for p in parts), col))
    return out


def features_used(tree: exp.Expression) -> set[str]:
    used = set()
    for node in tree.walk():
        for feature, classes in FEATURES.items():
            if isinstance(node, classes):
                used.add(feature)
    return used
