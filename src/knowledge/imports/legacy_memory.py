"""Pure parsers for supplied legacy files and offline vector-export bytes.

Export contract v1: ``{"export_version": 1, "collections": [{"name": str,
"scope_alias": str, "schema": object, "entries": [object, ...], ...}], ...}``.
Collections and rows are retained whole, including embeddings, retrieval
counters, original/content, namespace/key, temporal validity and unknown fields.
The export MUST include all original and temporal fields; a search-result dump
with omitted originals is classified incomplete, never treated as a full export.
No exporter that accesses Milvus is included. K06's operator connector supplies
this format; collection scope_alias is resolved by explicit scanner input only.

Documents use chunk_hash as mapping identity. KV and temporal rows use
collection/namespace/key; temporal versions remain one source with all rows.
Rows can specify source_root_id + relative_path for a relocated snapshot, or
the legacy absolute ``source`` path. Paths are evidence, never read requests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import yaml
from yaml.events import AliasEvent

from .manifest import canonical_json


@dataclass(frozen=True)
class ScanLimits:
    max_file_bytes: int = 1_048_576
    max_export_bytes: int = 16_777_216
    max_total_bytes: int = 67_108_864
    max_entries: int = 10_000
    max_metadata_bytes: int = 65_536
    max_depth: int = 16

    def __post_init__(self) -> None:
        if any(value <= 0 for value in self.__dict__.values()):
            raise ValueError("scan limits must be positive")


class MalformedSource(ValueError):
    """A retained source is not safely parseable; message is a stable issue code."""


class _FrontmatterLoader(yaml.SafeLoader):
    # Preserve temporal strings, rather than turning them into Python dates.
    yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in values if tag != "tag:yaml.org,2002:timestamp"]
        for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            raise MalformedSource("frontmatter_alias")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise MalformedSource("frontmatter_duplicate_or_invalid_key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _check_depth(value: Any, maximum: int, depth: int = 0) -> None:
    if depth > maximum:
        raise MalformedSource("metadata_depth_limit")
    if isinstance(value, dict):
        for child in value.values():
            _check_depth(child, maximum, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_depth(child, maximum, depth + 1)


def check_metadata(value: Any, limits: ScanLimits) -> None:
    _check_depth(value, limits.max_depth)
    try:
        size = len(canonical_json(value))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MalformedSource("non_json_metadata") from exc
    if size > limits.max_metadata_bytes:
        raise MalformedSource("metadata_size_limit")


@dataclass(frozen=True)
class FileDocument:
    frontmatter: dict[str, Any]
    body: str
    original: str
    summary: str | None
    facts: tuple[dict[str, str], ...]
    representation: str


def parse_file(content: bytes, limits: ScanLimits) -> FileDocument:
    """Keep raw body; decode only explicit section and known legacy-writer framing.

    The old writer joins ``[content, '\\n\\n## Original\\n', original]`` with
    newlines and adds a final newline. Its frontmatter identifies that envelope.
    Remove those framing bytes only; source newlines and Unicode remain exact.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise MalformedSource("invalid_utf8") from exc
    frontmatter = {}
    body = text
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
        if end is None:
            raise MalformedSource("unclosed_frontmatter")
        raw = "".join(lines[1:end])
        if len(raw.encode("utf-8")) > limits.max_metadata_bytes:
            raise MalformedSource("metadata_size_limit")
        try:
            parsed = yaml.load(raw, Loader=_FrontmatterLoader)
            frontmatter = {} if parsed is None else parsed
        except (yaml.YAMLError, RecursionError) as exc:
            raise MalformedSource("malformed_frontmatter") from exc
        if not isinstance(frontmatter, dict):
            raise MalformedSource("frontmatter_not_object")
        check_metadata(frontmatter, limits)
        body = "".join(lines[end + 1 :])
    markers = []
    fence = None
    offset = 0
    for line in body.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            delimiter = match[1]
            if fence is None:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence):
                fence = None
        elif fence is None and line in ("## Original\n", "## Original\r\n"):
            markers.append((offset, offset + len(line)))
        offset += len(line)
    if len(markers) > 1:
        raise MalformedSource("ambiguous_original_sections")
    summary = None
    original = body
    representation = "plain"
    legacy = all(
        key in frontmatter
        for key in ("tags", "created", "updated", "last_retrieved", "retrieval_count")
    )
    if markers:
        summary, original = body[: markers[0][0]], body[markers[0][1] :]
        representation = "original_section"
        if (
            legacy
            and summary.endswith("\n\n\n")
            and original.startswith("\n")
            and (original.endswith("\n"))
        ):
            summary, original = summary[:-3], original[1:-1]
            representation = "legacy_writer_v1"
    elif legacy and body.endswith("\n"):
        original = body[:-1]
        representation = "legacy_writer_v1"
    # Preserve duplicates and values instead of the old parser's last-key-wins map.
    facts = []
    namespace = None
    for line in body.splitlines():
        if line.startswith("## "):
            namespace = line[3:].strip()
        elif namespace and not line.startswith("#"):
            match = re.match(r"(?:[-*+]\s+)?([^:]+):\s?(.*)$", line)
            if match:
                facts.append({"namespace": namespace, "key": match[1].strip(), "value": match[2]})
    return FileDocument(frontmatter, body, original, summary, tuple(facts), representation)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MalformedSource("duplicate_json_key")
        result[key] = value
    return result


def parse_export(content: bytes, limits: ScanLimits) -> dict[str, Any]:
    """Parse only supplied JSON bytes; never initialize or call the legacy plugin."""
    if len(content) > limits.max_export_bytes:
        raise MalformedSource("export_size_limit")
    try:
        data = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(MalformedSource("nonfinite_json")),
        )
        _check_depth(data, limits.max_depth)
        canonical_json(data)
    except MalformedSource:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise MalformedSource("malformed_export") from exc
    if (
        not isinstance(data, dict)
        or type(data.get("export_version")) is not int
        or (data["export_version"] != 1)
    ):
        raise MalformedSource("unsupported_export_version")
    collections = data.get("collections")
    if not isinstance(collections, list):
        raise MalformedSource("malformed_collections")
    count = 0
    for collection in collections:
        if not isinstance(collection, dict) or not isinstance(collection.get("name"), str):
            raise MalformedSource("malformed_collection")
        if not collection["name"] or not isinstance(collection.get("entries"), list):
            raise MalformedSource("malformed_collection")
        count += len(collection["entries"])
    if count > limits.max_entries or len(collections) > limits.max_entries:
        raise MalformedSource("entry_count_limit")
    return data


def vector_identity(collection: str, row: dict[str, Any]) -> str:
    """Stable legacy key. Temporal validity is retained, not used to change identity."""
    kind = row.get("entry_type")
    if kind == "document" and isinstance(row.get("chunk_hash"), str) and row["chunk_hash"]:
        return canonical_json([collection, "document", row["chunk_hash"]]).decode()
    if kind in ("kv", "temporal") and all(
        isinstance(row.get(key), str) and row[key] for key in ("namespace", "key")
    ):
        return canonical_json([collection, kind, row["namespace"], row["key"]]).decode()
    raise MalformedSource("missing_vector_identity")
