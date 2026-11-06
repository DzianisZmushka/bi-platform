"""Canonical YAML reading and writing (ADR-003, ADR-006).

ruamel.yaml is used only to parse and to report source positions. Comments are
attached to elements by this module and the canonical text is written by this
module, because ruamel's own comment model ties a full-line comment to the
*previous* key (so it does not move with its key) and drops comments inside flow
collections.

Comment model: every map entry and sequence item is a *slot* addressed by a path.
A slot holds `before` (full-line comments above it) and `eol` (comment at the end
of its first line). Sequence items that carry an element kind key (`kpi: revenue`)
are addressed by kind and id, not by index, so their comments follow them when
they move.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import MarkedYAMLError
from ruamel.yaml.events import (
    AliasEvent,
    CollectionStartEvent,
    DocumentStartEvent,
    ScalarEvent,
)
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode

WIDTH = 100
INDENT = 2

Path = tuple  # of str | int


class FormatError(Exception):
    """The text is not YAML, or uses YAML outside the allowed subset (ADR-002)."""

    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.message = message
        self.line = line


@dataclass
class Comments:
    before: list[str] = field(default_factory=list)
    eol: str | None = None


@dataclass
class Document:
    data: Any
    comments: dict[Path, Comments] = field(default_factory=dict)  # by slot path
    footer: list[str] = field(default_factory=list)
    lines: dict[Path, int] = field(default_factory=dict)  # by data path (ints for list items)

    def line_of(self, path: Path) -> int | None:
        path = tuple(path)
        while path:
            if path in self.lines:
                return self.lines[path]
            path = path[:-1]
        return 1


# --------------------------------------------------------------------------- reading


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.version = (1, 2)
    return y


def item_segment(item: Any, index: int, element_keys: frozenset[str], used: set[str]) -> str | int:
    """Path segment for a sequence item: `kind:id` for elements, else the index."""
    if isinstance(item, dict):
        for k, v in item.items():
            if k in element_keys and isinstance(v, str):
                seg = f"{k}:{v}"
                if seg not in used:
                    used.add(seg)
                    return seg
                break
    return index


@dataclass
class _Slot:
    path: Path
    line: int
    col: int
    end_line: int
    depth: int


def load(text: str, element_keys: frozenset[str] = frozenset()) -> Document:
    if text.startswith("﻿"):
        text = text[1:]
    y = _yaml()
    try:
        docs = 0
        for ev in y.parse(text):
            line = ev.start_mark.line + 1 if ev.start_mark else None
            if isinstance(ev, DocumentStartEvent):
                docs += 1
                if docs > 1:
                    raise FormatError("only one YAML document per file is allowed", line)
            if isinstance(ev, AliasEvent) or getattr(ev, "anchor", None):
                raise FormatError("anchors and aliases (&, *) are not allowed; repeat the value", line)
            if isinstance(ev, (ScalarEvent, CollectionStartEvent)) and ev.tag is not None:
                raise FormatError("YAML tags (!...) are not allowed", line)
        node = y.compose(text)
    except MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        problem = (e.problem or str(e)).strip()
        raise FormatError(f"not valid YAML: {problem}", mark.line + 1 if mark else None) from None
    if node is None:
        raise FormatError("file is empty", 1)

    b = _Builder(element_keys)
    data = b.build(node, (), (), 0, [])
    doc = Document(data=data, lines=b.lines)
    _attach_comments(doc, text, b)
    return doc


class _Builder:
    def __init__(self, element_keys: frozenset[str]):
        self.element_keys = element_keys
        self.lines: dict[Path, int] = {}
        self.slots: list[_Slot] = []
        # (start_line, start_col, end_line, end_col, slot chain, block indicator width)
        self.scalars: list[tuple[int, int, int, int, tuple[int, ...], int]] = []

    def build(self, node, data_path: Path, slot_path: Path, depth: int, chain: list[int]):
        if isinstance(node, ScalarNode):
            self._scalar_span(node, chain)
            return _scalar_value(node)
        if isinstance(node, MappingNode):
            out: dict[str, Any] = {}
            for knode, vnode in node.value:
                if not isinstance(knode, ScalarNode) or _tag(knode) != "str":
                    raise FormatError("mapping keys must be plain strings", knode.start_mark.line + 1)
                key = knode.value
                if key in out:
                    raise FormatError(f"duplicate key `{key}`", knode.start_mark.line + 1)
                slot = self._slot(slot_path + (key,), knode, vnode, depth)
                self.lines[data_path + (key,)] = knode.start_mark.line + 1
                self._scalar_span(knode, chain + [slot])
                out[key] = self.build(vnode, data_path + (key,), slot_path + (key,), depth + 1, chain + [slot])
            return out
        if isinstance(node, SequenceNode):
            items = []
            used: set[str] = set()
            for i, inode in enumerate(node.value):
                slot = self._slot(None, inode, inode, depth)
                self.lines[data_path + (i,)] = inode.start_mark.line + 1
                value = self.build(inode, data_path + (i,), slot_path + ("?",), depth + 1, chain + [slot])
                seg = item_segment(value, i, self.element_keys, used)
                self._rebase(slot_path + ("?",), slot_path + (seg,), slot)
                items.append(value)
            return items
        raise FormatError("unsupported YAML node", node.start_mark.line + 1)

    def _slot(self, path, start_node, end_node, depth) -> int:
        self.slots.append(_Slot(path, start_node.start_mark.line, start_node.start_mark.column,
                                _last_line(end_node), depth))
        return len(self.slots) - 1

    def _rebase(self, tmp: Path, real: Path, item_slot: int):
        """Replace the placeholder segment used while an item's children were built."""
        n = len(tmp)
        self.slots[item_slot].path = real
        for s in self.slots[item_slot + 1:]:
            if s.path is not None and s.path[:n] == tmp:
                s.path = real + s.path[n:]

    def _scalar_span(self, node: ScalarNode, chain: list[int]):
        s, e = node.start_mark, node.end_mark
        if (s.line, s.column) == (e.line, e.column):
            return  # implicit empty value
        # Block scalars: only the header (`|`, `|-`) occupies the first line, so a
        # comment may follow it. -1 marks them; the header width is read from the text.
        block = -1 if node.style in ("|", ">") else 0
        self.scalars.append((s.line, s.column, e.line, e.column, tuple(chain), block))


def _tag(node) -> str:
    t = node.tag
    t = getattr(t, "value", t) or ""
    return t.rsplit(":", 1)[-1]


def _scalar_value(node: ScalarNode):
    tag, v = _tag(node), node.value
    if tag == "null":
        return None
    if tag == "bool":
        return v.lower() == "true"
    if tag == "int":
        v2 = v.replace("_", "")
        try:
            return int(v2, 0)
        except ValueError:
            return int(v2)
    if tag == "float":
        low = v.lower().replace("_", "")
        if low in (".inf", "+.inf"):
            return float("inf")
        if low == "-.inf":
            return float("-inf")
        if low == ".nan":
            return float("nan")
        return float(low)
    return v  # str, and timestamps stay strings (JSON has no date type)


def _last_line(node) -> int:
    """Last source line holding content of this node (0-based)."""
    if isinstance(node, ScalarNode):
        e = node.end_mark
        if e.column == 0 and e.line > node.start_mark.line:
            return e.line - 1
        return e.line
    if node.flow_style:
        return node.end_mark.line
    children = node.value if isinstance(node, SequenceNode) else [v for _, v in node.value]
    if not children:
        return node.start_mark.line
    return max(_last_line(c) for c in children)


_COMMENT_START = re.compile(r"(?:^|(?<=[ \t]))#")


def _attach_comments(doc: Document, text: str, b: _Builder):
    lines = text.split("\n")
    occupied: dict[int, list[tuple[int, float]]] = {}
    full: set[int] = set()
    for sl, sc, el, ec, _, ind in b.scalars:
        if ind == -1:  # block scalar: header on first line, content on following lines
            header = re.match(r"[|>][-+0-9]*", lines[sl][sc:])
            occupied.setdefault(sl, []).append((sc, sc + (header.end() if header else 1)))
            last = el - 1 if ec == 0 else el
            full.update(range(sl + 1, last + 1))
            if ec and el > sl:
                occupied.setdefault(el, []).append((0, ec))
        elif sl == el:
            occupied.setdefault(sl, []).append((sc, ec))
        else:
            occupied.setdefault(sl, []).append((sc, float("inf")))
            full.update(range(sl + 1, el))
            occupied.setdefault(el, []).append((0, ec))

    slots = sorted((s for s in b.slots if s.path is not None), key=lambda s: (s.line, s.depth))

    def add_before(after_line: int, text_: str):
        for s in slots:
            if s.line > after_line:
                doc.comments.setdefault(s.path, Comments()).before.append(text_)
                return
        doc.footer.append(text_)

    for ln, raw in enumerate(lines):
        if ln in full or "#" not in raw:
            continue
        for m in _COMMENT_START.finditer(raw):
            col = m.start()
            if any(a <= col < z for a, z in occupied.get(ln, ())):
                continue
            comment = raw[col:].rstrip()
            if not raw[:col].strip():
                add_before(ln, comment)
            else:
                _attach_eol(doc, b, ln, col, comment, add_before)
            break


def _attach_eol(doc: Document, b: _Builder, ln: int, col: int, comment: str, add_before):
    # The scalar written last before the comment on this line decides the owner.
    best = None
    for sl, sc, el, ec, chain, _ in b.scalars:
        if sl == ln and sc < col:
            key = (sc,)
        elif el == ln and sl < ln and ec <= col:
            key = (ec,)
        else:
            continue
        if best is None or key > best[0]:
            best = (key, chain)
    if best is None:
        add_before(ln, comment)
        return
    chain = [b.slots[i] for i in best[1] if b.slots[i].path is not None]
    pick = next((s for s in chain if s.line == ln and s.end_line == ln), None)
    if pick is None:
        pick = next((s for s in reversed(chain) if s.line == ln), chain[-1] if chain else None)
    if pick is None:
        add_before(ln, comment)
        return
    c = doc.comments.setdefault(pick.path, Comments())
    c.eol = comment if c.eol is None else f"{c.eol}  {comment}"


# --------------------------------------------------------------------------- writing

_NEEDS_QUOTES = re.compile(
    r"""^(?:
        ~|null|Null|NULL
      | y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF
      | [-+]?\.?[0-9].*                      # number-, date- and time-like text
      | [-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)
      | <<|=
    )$""",
    re.X,
)
_BAD_START = set("!&*{}[]|>'\"%@`#,?:")
_FLOW_CHARS = set(",[]{}")


def needs_quotes(s: str, flow: bool = False) -> bool:
    if s == "" or s != s.strip() or "${" in s:
        return True
    if _NEEDS_QUOTES.match(s):
        return True
    if s[0] in _BAD_START or s.startswith("- ") or s == "-":
        return True
    if ": " in s or " #" in s or s.endswith(":"):
        return True
    if any(not ch.isprintable() for ch in s):
        return True
    if flow and any(ch in _FLOW_CHARS for ch in s):
        return True
    return False


def format_key(k: str) -> str:
    # Deliberate exception (ADR-002 §1a): the chart key `y` stays plain. Only full YAML 1.1
    # parsers (e.g. go-yaml v2) read it as a boolean, and those are not supported.
    if k == "y":
        return k
    return format_scalar(k)


def format_scalar(v: Any, flow: bool = False) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return ".nan"
        if v in (float("inf"), float("-inf")):
            return ".inf" if v > 0 else "-.inf"
        return repr(v)
    s = str(v)
    return json.dumps(s, ensure_ascii=False) if needs_quotes(s, flow) else s


def _literal_ok(s: str) -> bool:
    if "\n" not in s or "\r" in s or s.startswith((" ", "\n")):
        return False
    body = s.rstrip("\n")
    return all(line == line.rstrip() and all(ch.isprintable() or ch == "\t" for ch in line)
               for line in body.split("\n"))


def _literal(s: str, indent: int) -> tuple[str, list[str]]:
    if not s.endswith("\n"):
        header, body = "|-", s
    elif s.endswith("\n\n"):
        header, body = "|+", s[:-1]
    else:
        header, body = "|", s[:-1]
    pad = " " * indent
    return header, [pad + line if line else "" for line in body.split("\n")]


def _is_scalar(v) -> bool:
    return not isinstance(v, (dict, list))


class Writer:
    """Writes data in canonical form. `shape` supplies key order and blank-line rules."""

    def __init__(self, shape, comments: dict[Path, Comments] | None = None,
                 element_keys: frozenset[str] = frozenset()):
        self.shape = shape
        self.comments = comments or {}
        self.element_keys = element_keys
        self.commented_parents: set[Path] = set()
        for p in self.comments:
            for i in range(len(p)):
                self.commented_parents.add(p[:i])

    def write(self, data: dict, footer: list[str] | None = None) -> str:
        out: list[str] = []
        self._mapping(data, self.shape.root(), 0, (), out, top=True)
        out.extend(footer or [])
        return "\n".join(out) + "\n"

    # A collection may be written in flow style only if it holds scalars, no comments
    # are attached inside it, and the whole line fits.
    def _flow(self, v, path: Path) -> str | None:
        """Flow style for a collection of scalars, or a mapping whose values are scalars or
        non-empty lists of scalars (ADR-003 rule 5 as amended by the ADR-012 review)."""
        if not v or path in self.commented_parents:
            return None

        def plain(x) -> bool:
            return _is_scalar(x) and x is not None and not (isinstance(x, str) and "\n" in x)

        if isinstance(v, dict):
            for k, x in v.items():
                if isinstance(x, list):
                    # Lists inline only in nested mappings; top-level sections stay block style.
                    if len(path) < 2 or not x or not all(plain(i) for i in x) \
                            or path + (k,) in self.commented_parents:
                        return None
                elif not plain(x):
                    return None
            inner = ", ".join(f"{format_key(k)}: {self._flow_value(x)}" for k, x in v.items())
            return "{ " + inner + " }"
        if not all(plain(x) for x in v):
            return None
        return "[" + ", ".join(format_scalar(x, True) for x in v) + "]"

    @staticmethod
    def _flow_value(x) -> str:
        if isinstance(x, list):
            return "[" + ", ".join(format_scalar(i, True) for i in x) + "]"
        return format_scalar(x, True)

    def _entry(self, head: str, v, schema, indent: int, path: Path) -> list[str]:
        """Lines for `head` (`key:` or `-`) followed by its value."""
        sep = " " if head.endswith(":") else " "
        if v is None:
            return [head]
        if isinstance(v, (dict, list)):
            if not v:
                return [head + sep + ("{}" if isinstance(v, dict) else "[]")]
            flow = self._flow(v, path)
            if flow is not None and len(head) + 1 + len(flow) <= WIDTH:
                return [head + sep + flow]
            lines: list[str] = []
            if isinstance(v, dict):
                self._mapping(v, schema, indent + INDENT, path, lines,
                              blank_between=bool(schema.get("x-blank-lines")))
            else:
                self._sequence(v, schema, indent + INDENT, path, lines)
            return [head] + lines
        if isinstance(v, str) and "\n" in v and _literal_ok(v):
            header, body = _literal(v, indent + INDENT)
            return [head + sep + header] + body
        return [head + sep + format_scalar(v)]

    def _mapping(self, m: dict, schema, indent: int, path: Path, out: list[str],
                 top: bool = False, blank_between: bool = False):
        pad = " " * indent
        for i, key in enumerate(self.shape.order(schema, m)):
            v = m[key]
            p = path + (key,)
            lines = self._entry(pad + format_key(key) + ":", v, self.shape.prop(schema, key),
                                indent, p)
            if i and (blank_between or (top and isinstance(v, (dict, list)) and len(lines) > 1)):
                out.append("")
            self._emit(out, lines, self.comments.get(p), pad)

    def _sequence(self, seq: list, schema, indent: int, path: Path, out: list[str]):
        pad = " " * indent
        items_schema = self.shape.items(schema)
        used: set[str] = set()
        for i, item in enumerate(seq):
            p = path + (item_segment(item, i, self.element_keys, used),)
            c = self.comments.get(p)
            if i and schema.get("x-blank-lines"):
                out.append("")
            if isinstance(item, dict) and item:
                inner: list[str] = []
                self._mapping(item, self.shape.item(items_schema, item), indent + INDENT, p, inner)
                # The first key goes on the dash line; its own `before` comments move above the dash.
                k = next(j for j, line in enumerate(inner) if not line.lstrip().startswith("#"))
                lines = [pad + "- " + inner[k][indent + INDENT:]] + inner[k + 1:]
                hoisted = [line.strip() for line in inner[:k]]
                c2 = Comments(before=(c.before if c else []) + hoisted, eol=c.eol if c else None)
                self._emit(out, lines, c2, pad)
            else:
                lines = self._entry(pad + "-", item, items_schema, indent, p)
                self._emit(out, lines, c, pad)

    @staticmethod
    def _emit(out: list[str], lines: list[str], c: Comments | None, pad: str):
        if c:
            out.extend(pad + b for b in c.before)
            if c.eol:
                lines = [lines[0] + "  " + c.eol] + lines[1:]
        out.extend(lines)
