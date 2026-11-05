# ADR-003: Canonical YAML serialization

- Status: Accepted (rules are binding; the serializer that enforces them is Phase 0 step 4)
- Date: 2026-11-05

## Context

"One state → exactly one serialization" is what makes builder saves produce zero diff and keeps Git history readable. YAML allows many spellings of the same data, so the canonical one has to be pinned down. The examples under `examples/` are written by hand to these rules. The serializer must reproduce them byte for byte.

## Decision

1. **Encoding.** UTF-8, LF line endings, trailing newline, no trailing spaces, 2-space indent. List items are indented under their parent key (`pages:\n  - page: overview`).
2. **Key order is fixed per element kind** and declared by property order in the JSON Schema. The general order is: kind key → name and documentation (`title`, `label`, `description`) → data binding (`measure`, `x`, `y`, `rows`, `values`, `where`) → behaviour (`sort`, `limit`, `on_click`, `visible`) → presentation (`format`, `color`, `cells`) → children (`items`) last.
3. **Author-named collections keep author order.** Measures, dimensions, entities, pages and layout items are ordered as written, because order means something to the author: it is display order. Tools never sort them.
4. **Defaults are omitted** (ADR-002 §5). An element whose properties are all defaults is written with an empty value. `null` is never written in any other case.
5. **Flow style is decided by shape and width, not by the author.** A mapping or sequence that contains only scalars is written in flow style (`{ label: Region, type: date }`, `[2, 1]`) if the whole line fits in 100 columns. Otherwise it is written in block style. Exceptions, always block style: anything containing a nested collection, and **a mapping that is an item of a sequence** (pages, filters, layout nodes). That keeps every addressable element on its own lines, so diffs stay per property.
6. **Strings are plain when that is safe**, meaning the text reads back as the same string. Otherwise they use double quotes. `${...}` expressions are always double-quoted. Multi-line strings use literal block style `|`.
7. **Comments** are allowed and preserved. A full-line comment belongs to the key that follows it, and a trailing comment to the key on its line. A comment disappears only when its element is deleted. `description:` is the preferred way to document an element, because builders can show and edit it.

## Consequences

- Round-trip test: for every file in `examples/`, `serialize(parse(file)) == file` byte for byte. The same holds after a builder opens and saves the file without changes.
- Width-based flow style means editing a long label can switch a line to block style. That is deterministic and acceptable.
- The serializer needs a comment-preserving YAML library (`ruamel.yaml` round-trip mode) or a custom emitter. The choice is made in step 4.
