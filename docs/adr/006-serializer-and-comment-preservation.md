# ADR-006: Serializer and comment preservation

- Status: Accepted. Refines ADR-003 §7 (comments).
- Date: 2026-11-05

## Context

Comments must survive a builder save. A comment belongs to its key and moves with the key when canonical ordering or a builder reorders things. Comments inside flow sequences must not be lost. The fallback, if ruamel.yaml could not do this, was to keep only full-line comments before elements and to record that limitation here.

We tested ruamel.yaml 0.19 round-trip mode directly:

- It stores a full-line comment *above* key B as a trailing comment of the *previous* key A. After reordering, the comment stays under A instead of moving with B.
- It silently drops comments inside flow collections (`columns: [2,  # wide`).

So ruamel cannot meet the requirement. Its own "comments before elements only" mode has the same reordering bug, so the fallback would not work either.

## Decision

`obi_formats.yamlio` uses ruamel.yaml only to parse (YAML 1.2) and to report exact source positions. Comment attachment and the canonical writer are our own code, about 500 lines. The full requirement is met, so no limitation applies.

**Attachment rules.** Every map entry and every list item is a *slot*.
- A **full-line comment** belongs to the next slot below it (the outermost one that starts on that line). It moves with that slot. At the end of the file it becomes the file footer.
- A **trailing comment** belongs to the slot that ends on that line, choosing the outermost such slot that contains the text just before the `#`. `values: [a, b]  # note` attaches to `values`. `2,  # wide` inside a multi-line flow list attaches to item `2`.
- List items that are elements (`- kpi: revenue`) are addressed by kind and id, not position, so their comments follow them when a builder moves them. Deleting an element deletes its comments.
- A `#` inside quotes, inside a plain scalar (`C#`), or inside a literal block is text, not a comment. A comment after a literal header (`content: |  # note`) is kept.

**Comments inside flow collections.** A collection that has comments attached to its items is written in block style. Comments therefore change the canonical shape, but deterministically: the result is stable, and canonicalizing it again changes nothing.

**Blank lines.** These are canonical, not preserved. A blank line goes before every top-level key whose value is a block collection, and between the entries of collections marked `x-blank-lines` in the schema (model `entities`, dashboard `pages`). Other blank lines are dropped.

**`x` and `y` keys.** The YAML 1.1 spec (and parsers such as go-yaml v2) read a bare `y` or `n` as a boolean. PyYAML, ruamel, js-yaml and go-yaml v3 do not. Chart channels keep the Vega-Lite names `x`/`y`, and the writer leaves the key `y` unquoted (ADR-002 §1a defines the exception and the supported parsers). Every other YAML 1.1 boolean or null word (`on`, `yes`, `off`, ...) is quoted wherever it appears, and none may be used as a key name (ADR-002).

## Consequences

- Tests check, byte for byte: all examples, the YAML → JSON → YAML round trip, and comment cases including reordered keys, moved elements, flow sequences, literal blocks, quoted `#`, header and footer.
- The writer is ours to maintain. It is small and covered by those tests, and it gives exact control over the canonical form that ruamel's emitter does not.
- A builder that renames an element id should carry the old id's comments over to the new id. That is a builder concern (Phase 5).
