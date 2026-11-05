"""The `${...}` expression language (docs/formats/expressions.md).

Small on purpose: literals, names, member access, arithmetic, comparisons,
`and`/`or`/`not`, the ternary `a ? b : c`, and one method, `filters.<id>.includes(x)`.
No loops, assignments or user-defined functions.

AST nodes are tuples:
  ("lit", value)  ("name", n)  ("member", obj, attr)  ("call", obj, method, [args])
  ("unary", op, x)  ("bin", op, a, b)  ("cond", test, then, else)
"""

from __future__ import annotations

import re
from typing import Any

FILTER_MEMBERS = ("label", "count", "first")
FILTER_METHODS = ("includes",)


class ExprError(Exception):
    pass


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>===|!==|==|!=|<=|>=|&&|\|\||[-+*/%<>?:().,!=])
""", re.X)

_WORDS = {"and", "or", "not", "true", "false", "null"}
_HINTS = {"&&": "and", "||": "or", "!": "not", "===": "==", "!==": "!=", "=": "=="}


def _tokens(src: str) -> list[tuple[str, Any]]:
    out, pos = [], 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise ExprError(f"unexpected character `{src[pos]}`")
        pos = m.end()
        kind = m.lastgroup
        text = m.group()
        if kind == "ws":
            continue
        if kind == "op" and text in _HINTS:
            raise ExprError(f"use `{_HINTS[text]}` instead of `{text}`")
        if kind == "num":
            out.append(("lit", float(text) if "." in text else int(text)))
        elif kind == "str":
            out.append(("lit", _unescape(text[1:-1])))
        elif kind == "name" and text in _WORDS:
            if text in ("true", "false", "null"):
                out.append(("lit", {"true": True, "false": False, "null": None}[text]))
            else:
                out.append(("op", text))
        else:
            out.append((kind, text))
    out.append(("end", None))
    return out


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


class _Parser:
    def __init__(self, src: str):
        self.toks = _tokens(src)
        self.i = 0

    def peek(self, *ops) -> bool:
        k, v = self.toks[self.i]
        return k == "op" and v in ops

    def take(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, op: str):
        if not self.peek(op):
            raise ExprError(f"expected `{op}`{self._at()}")
        self.take()

    def _at(self) -> str:
        k, v = self.toks[self.i]
        return " at end of expression" if k == "end" else f" before `{v}`"

    def parse(self):
        node = self.ternary()
        if self.toks[self.i][0] != "end":
            raise ExprError(f"unexpected `{self.toks[self.i][1]}`")
        return node

    def ternary(self):
        test = self.binary(0)
        if self.peek("?"):
            self.take()
            then = self.ternary()
            self.expect(":")
            return ("cond", test, then, self.ternary())
        return test

    _LEVELS = [("or",), ("and",), ("==", "!=", "<", "<=", ">", ">="), ("+", "-"), ("*", "/", "%")]

    def binary(self, level: int):
        if level == 2 and self.peek("not"):  # `not` binds looser than comparison
            self.take()
            return ("unary", "not", self.binary(2))
        if level == len(self._LEVELS):
            return self.unary()
        node = self.binary(level + 1)
        while self.peek(*self._LEVELS[level]):
            op = self.take()[1]
            right = self.binary(level + 1)
            if level == 2 and self.peek(*self._LEVELS[2]):
                raise ExprError("comparisons cannot be chained; combine them with `and`")
            node = ("bin", op, node, right)
        return node

    def unary(self):
        if self.peek("-"):
            self.take()
            return ("unary", "-", self.unary())
        return self.postfix()

    def postfix(self):
        node = self.primary()
        while self.peek("."):
            self.take()
            k, name = self.take()
            if k != "name":
                raise ExprError("expected a name after `.`")
            if self.peek("("):
                self.take()
                args = []
                if not self.peek(")"):
                    args.append(self.ternary())
                    while self.peek(","):
                        self.take()
                        args.append(self.ternary())
                self.expect(")")
                node = ("call", node, name, args)
            else:
                node = ("member", node, name)
        return node

    def primary(self):
        k, v = self.take()
        if k == "lit":
            return ("lit", v)
        if k == "name":
            if self.peek("("):
                raise ExprError(f"`{v}(...)` is not available; expressions have no functions")
            return ("name", v)
        if k == "op" and v == "(":
            node = self.ternary()
            self.expect(")")
            return node
        if k == "end":
            raise ExprError("expression is incomplete")
        raise ExprError(f"unexpected `{v}`")


def parse(src: str):
    return _Parser(src).parse()


def parse_template(s: str) -> list[str | tuple]:
    """Split text into literal parts and parsed `${...}` expressions."""
    parts: list[str | tuple] = []
    pos = 0
    while True:
        start = s.find("${", pos)
        if start < 0:
            if pos < len(s):
                parts.append(s[pos:])
            return parts
        if start > pos:
            parts.append(s[pos:start])
        end, quote = start + 2, None
        while end < len(s):
            ch = s[end]
            if quote:
                if ch == "\\":
                    end += 1
                elif ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "}":
                break
            end += 1
        else:
            raise ExprError("`${` is not closed with `}`")
        parts.append(parse(s[start + 2:end]))
        pos = end + 1


def is_expression(s: Any) -> bool:
    return isinstance(s, str) and "${" in s


# --------------------------------------------------------------------------- evaluation


class FilterSelection(list):
    """`filters.<id>`: always a list of selected values; empty means not set."""

    @property
    def label(self) -> str:
        return ", ".join(str(v) for v in self)

    @property
    def count(self) -> int:
        return len(self)

    @property
    def first(self):
        return self[0] if self else None

    def includes(self, value) -> bool:
        return value in self


def evaluate(node, ctx: dict[str, Any]):
    t = node[0]
    if t == "lit":
        return node[1]
    if t == "name":
        return ctx.get(node[1])
    if t == "member":
        obj = evaluate(node[1], ctx)
        if isinstance(obj, FilterSelection):
            return getattr(obj, node[2]) if node[2] in FILTER_MEMBERS else None
        if isinstance(obj, dict):
            return obj.get(node[2])
        return None
    if t == "call":
        obj = evaluate(node[1], ctx)
        if isinstance(obj, FilterSelection) and node[2] == "includes" and len(node[3]) == 1:
            return obj.includes(evaluate(node[3][0], ctx))
        return None
    if t == "unary":
        x = evaluate(node[2], ctx)
        if node[1] == "not":
            return not _truthy(x)
        return -x if _is_num(x) else None
    if t == "cond":
        return evaluate(node[2] if _truthy(evaluate(node[1], ctx)) else node[3], ctx)
    op = node[1]
    if op == "and":
        a = evaluate(node[2], ctx)
        return evaluate(node[3], ctx) if _truthy(a) else a
    if op == "or":
        a = evaluate(node[2], ctx)
        return a if _truthy(a) else evaluate(node[3], ctx)
    a, b = evaluate(node[2], ctx), evaluate(node[3], ctx)
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op in ("<", "<=", ">", ">="):
        if a is None or b is None or isinstance(a, str) != isinstance(b, str):
            return False
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
    if op == "+" and (isinstance(a, str) or isinstance(b, str)):
        return f"{'' if a is None else a}{'' if b is None else b}"
    if not (_is_num(a) and _is_num(b)):
        return None  # null and non-numbers propagate as null
    if op in ("/", "%") and b == 0:
        return None
    return {"+": a + b, "-": a - b, "*": a * b, "/": a / b, "%": a % b}[op]


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _truthy(x) -> bool:
    return bool(x)


def render(s: str, ctx: dict[str, Any]):
    """Evaluate a property value: a whole `${...}` returns a typed value, mixed text a string."""
    parts = parse_template(s)
    if len(parts) == 1 and isinstance(parts[0], tuple):
        return evaluate(parts[0], ctx)
    return "".join(p if isinstance(p, str) else _text(evaluate(p, ctx)) for p in parts)


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, FilterSelection):
        return v.label
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
