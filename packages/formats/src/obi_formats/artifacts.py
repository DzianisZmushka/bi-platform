"""Read and write artifacts in canonical form."""

from __future__ import annotations

from typing import Any

from .schema import all_element_keys, get_schema
from .yamlio import Comments, Document, Writer, load


def parse(text: str) -> Document:
    return load(text, all_element_keys())


def dump(data: Any, artifact_type: str, comments: dict[tuple, Comments] | None = None,
         footer: list[str] | None = None) -> str:
    """Canonical text for `data`. Pass the comments of a parsed document to keep them."""
    schema = get_schema(artifact_type)
    writer = Writer(schema, comments, all_element_keys())
    return writer.write(schema.normalize(data), footer)


def canonicalize(text: str, artifact_type: str) -> str:
    doc = parse(text)
    return dump(doc.data, artifact_type, doc.comments, doc.footer)
