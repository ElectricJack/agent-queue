"""Read-only, bounded inventory of explicit roots and an offline vector export.

``RootSpec`` declares source scope and category (notes, memory, facts, reviews,
system, orchestrator or agent-type). These are historical labels, not runtime
authorization. Optional relative_paths makes missing/deleted selections visible.
No title-based task classification is performed. Archives are retained but never
expanded. Symlinks, traversal, special files and ambiguous source paths cannot
become candidates. All filesystem work runs outside the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .legacy_memory import (
    MalformedSource,
    ScanLimits,
    check_metadata,
    parse_export,
    parse_file,
    vector_identity,
)
from .manifest import Artifact, Inventory, InventoryItem, Source, canonical_json, sha256


@dataclass(frozen=True)
class RootSpec:
    root_id: str
    path: Path
    source_scope: str
    source_kind: str = "memory"
    relative_paths: tuple[str, ...] | None = None


def _relative(path: str) -> bool:
    return (
        bool(path)
        and not PurePosixPath(path).is_absolute()
        and not any(part in ("", ".", "..") for part in path.split("/"))
        and "\\" not in path
    )


def _open_path(path: Path, *, directory: bool = False) -> int:
    """Open each ancestor with NOFOLLOW, including the explicitly supplied root."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise MalformedSource("path_traversal")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(absolute.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory or index < len(absolute.parts) - 2:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_fd(fd: int, maximum: int) -> bytes:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise MalformedSource("special_file")
    if os.fstat(fd).st_size > maximum:
        raise MalformedSource("source_size_limit")
    with os.fdopen(os.dup(fd), "rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise MalformedSource("source_size_limit")
    return content


def _read_relative(root_fd: int, relative: str, maximum: int) -> bytes:
    if not _relative(relative):
        raise MalformedSource("path_traversal")
    fd = os.dup(root_fd)
    try:
        parts = relative.split("/")
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return _read_fd(fd, maximum)
    finally:
        os.close(fd)


def _issue(exc: OSError | MalformedSource) -> str:
    if isinstance(exc, MalformedSource):
        return str(exc)
    return {
        errno.ENOENT: "missing_source",
        errno.EACCES: "inaccessible_source",
        errno.ELOOP: "symlink_source",
        errno.ENOTDIR: "unsafe_path_component",
    }.get(exc.errno, "unreadable_source")


def _walk(
    fd: int, limits: ScanLimits, count_entry: Callable[[], None], prefix: str = "", depth: int = 0
):
    if depth > limits.max_depth:
        raise MalformedSource("directory_depth_limit")
    with os.scandir(fd) as entries:
        names = []
        for entry in entries:
            count_entry()
            names.append(entry.name)
    for name in sorted(names):
        relative = f"{prefix}/{name}" if prefix else name
        try:
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    yield from _walk(child, limits, count_entry, relative, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISLNK(mode):
                yield relative, "symlink_source"
            else:
                yield relative, None
        except (OSError, MalformedSource) as exc:
            if isinstance(exc, MalformedSource) and str(exc) == "entry_count_limit":
                raise
            yield relative, _issue(exc)


class _Scanner:
    def __init__(self, limits: ScanLimits):
        self.limits = limits
        self.artifacts: dict[str, Artifact] = {}
        self.total_bytes = 0
        self.entries = 0

    def count_entry(self) -> None:
        self.entries += 1
        if self.entries > self.limits.max_entries:
            raise MalformedSource("entry_count_limit")

    def retain(self, content: bytes) -> str:
        self.total_bytes += len(content)
        if self.total_bytes > self.limits.max_total_bytes:
            raise MalformedSource("total_size_limit")
        artifact = Artifact(content)
        self.artifacts[artifact.sha256] = artifact
        return artifact.sha256

    def files(self, root: RootSpec) -> list[InventoryItem]:
        real_root = str(root.path.absolute())
        items = []
        try:
            root_fd = _open_path(root.path, directory=True)
        except (OSError, MalformedSource) as exc:
            source = Source(
                root.source_kind,
                root.source_scope,
                root.root_id,
                None,
                {
                    "root_id": root.root_id,
                    "real_root": real_root,
                },
            )
            return [InventoryItem((source,), "unavailable_root", "unavailable", (_issue(exc),))]
        try:
            paths = (
                ((path, None) for path in sorted(set(root.relative_paths)))
                if root.relative_paths is not None
                else _walk(root_fd, self.limits, self.count_entry)
            )
            for relative, error in paths:
                if root.relative_paths is not None:
                    self.count_entry()
                metadata: dict[str, Any] = {
                    "root_id": root.root_id,
                    "real_root": real_root,
                    "relative_path": relative,
                }
                digest = None
                try:
                    if error:
                        raise MalformedSource(error)
                    content = _read_relative(root_fd, relative, self.limits.max_file_bytes)
                    digest = self.retain(content)
                    if Path(relative).suffix.lower() not in (".md", ".txt"):
                        raise MalformedSource("unsupported_file_type")
                    document = parse_file(content, self.limits)
                    metadata.update(
                        {
                            "frontmatter": document.frontmatter,
                            "body": document.body,
                            "original": document.original,
                            "summary": document.summary,
                            "representation": document.representation,
                        }
                    )
                    if root.source_kind == "facts":
                        metadata["facts"] = document.facts
                    source = Source(
                        root.source_kind,
                        root.source_scope,
                        f"{root.root_id}/{relative}",
                        digest,
                        metadata,
                    )
                    items.append(
                        InventoryItem(
                            (source,),
                            "file_only",
                            "candidate",
                            (),
                            document.original,
                            document.summary,
                        )
                    )
                except (OSError, MalformedSource) as exc:
                    issue = _issue(exc)
                    if issue == "total_size_limit":
                        raise
                    disposition = "quarantined" if digest else "unavailable"
                    if issue in ("symlink_source", "path_traversal", "unsupported_file_type"):
                        disposition = "excluded"
                    source = Source(
                        root.source_kind,
                        root.source_scope,
                        f"{root.root_id}/{relative}",
                        digest,
                        metadata,
                    )
                    items.append(InventoryItem((source,), "malformed_file", disposition, (issue,)))
        finally:
            os.close(root_fd)
        return items

    def vectors(self, path: Path | None, aliases: Mapping[str, Sequence[str]]):
        if path is None:
            return "not_observed", []
        digest = None
        try:
            fd = _open_path(path)
            try:
                content = _read_fd(fd, self.limits.max_export_bytes)
            finally:
                os.close(fd)
            digest = self.retain(content)
            export = parse_export(content, self.limits)
        except (OSError, MalformedSource) as exc:
            if str(exc) == "total_size_limit":
                raise
            issue = _issue(exc)
            source = Source("vector_export", "inventory", str(path.absolute()), digest, {})
            item = InventoryItem(
                (source,),
                "unavailable_export" if digest is None else "malformed_export",
                "unavailable" if digest is None else "quarantined",
                (issue,),
            )
            return "not_observed" if issue == "missing_source" else "unavailable", [item]

        # Grouping does not discard repeated/conflicting rows or temporal versions.
        groups = defaultdict(list)
        for collection in export["collections"]:
            header = {key: value for key, value in collection.items() if key != "entries"}
            alias = collection.get("scope_alias")
            candidates = sorted(set(aliases.get(alias, ()))) if isinstance(alias, str) else []
            scope = candidates[0] if len(candidates) == 1 else f"unresolved:{alias}"
            for row in collection["entries"]:
                self.count_entry()
                issues = []
                if not candidates:
                    issues.append("unknown_scope_alias")
                elif len(candidates) > 1:
                    issues.append("scope_alias_collision")
                try:
                    if not isinstance(row, dict):
                        raise MalformedSource("malformed_vector_row")
                    key = vector_identity(collection["name"], row)
                except MalformedSource as exc:
                    key = canonical_json(
                        [collection["name"], "malformed", sha256(canonical_json(row))]
                    ).decode()
                    issues.append(str(exc))
                try:
                    check_metadata(header, self.limits)
                    check_metadata(row, self.limits)
                except MalformedSource as exc:
                    issues.append(str(exc))
                groups[(scope, key)].append((header, row, candidates, issues))
        items = []
        for (scope, key), entries in sorted(groups.items()):
            entries.sort(key=canonical_json)
            issues = sorted({issue for entry in entries for issue in entry[3]})
            rows = [entry[1] for entry in entries]
            metadata = {
                "collections": [entry[0] for entry in entries],
                "rows": rows,
                "scope_candidates": entries[0][2],
            }
            # Oversized/deep evidence stays in the full export artifact, never expanded here.
            if any(issue in issues for issue in ("metadata_size_limit", "metadata_depth_limit")):
                metadata = {"row_hashes": [sha256(canonical_json(row)) for row in rows]}
            source = Source("vector", scope, key, digest, metadata)
            kind = rows[0].get("entry_type") if isinstance(rows[0], dict) else None
            original = summary = None
            classification = "vector_only"
            if kind == "document":
                original, summary = rows[0].get("original"), rows[0].get("content")
                if original in (None, ""):
                    original = None
                    classification = "summary_only"
                elif not isinstance(original, str):
                    issues.append("malformed_original")
                    original = None
                if summary is not None and not isinstance(summary, str):
                    issues.append("malformed_content")
                    summary = None
                if len(entries) > 1:
                    issues.append("duplicate_vector_identity")
                if len({canonical_json(row.get("original")) for row in rows}) > 1:
                    issues.append("conflicting_originals")
                    classification = "conflicting_originals"
                    original = summary = None
            elif kind in ("kv", "temporal"):
                classification = "temporal_history" if kind == "temporal" else "kv"
                if kind == "kv" and len(entries) > 1:
                    issues.append("duplicate_vector_identity")
            if classification == "summary_only":
                issues.append("missing_original")
            items.append(
                InventoryItem(
                    (source,),
                    classification,
                    "quarantined" if issues else "candidate",
                    tuple(issues),
                    original,
                    summary,
                )
            )
        return "observed", items


def _target(source: Source, roots: Sequence[RootSpec]) -> tuple[str, str] | None:
    rows = source.metadata.get("rows", [])
    if not rows or not isinstance(rows[0], dict) or rows[0].get("entry_type") != "document":
        return None
    row = rows[0]
    if "source_root_id" in row or "relative_path" in row:
        root_id, relative = row.get("source_root_id"), row.get("relative_path")
        if not isinstance(root_id, str) or not isinstance(relative, str) or not _relative(relative):
            raise MalformedSource("unsafe_source_path")
        matches = [root for root in roots if root.root_id == root_id]
        if not matches:
            raise MalformedSource("unknown_source_root")
        if matches[0].source_scope != source.source_scope:
            raise MalformedSource("source_scope_mismatch")
        return source.source_scope, f"{root_id}/{relative}"
    path = row.get("source")
    if not isinstance(path, str) or not path:
        return None
    if ".." in PurePosixPath(path).parts or "\\" in path:
        raise MalformedSource("unsafe_source_path")
    matches = []
    for root in roots:
        try:
            relative = PurePosixPath(path).relative_to(str(root.path.absolute())).as_posix()
        except ValueError:
            continue
        if _relative(relative):
            matches.append((source.source_scope, f"{root.root_id}/{relative}"))
    if len(matches) > 1:
        raise MalformedSource("ambiguous_source_path")
    return matches[0] if matches else None


def _reconcile(files, vectors, roots):
    file_index = {
        (item.sources[0].source_scope, item.sources[0].source_key): index
        for index, item in enumerate(files)
    }
    pairs = defaultdict(list)
    remaining = []
    for item in vectors:
        try:
            target = _target(item.sources[0], roots)
        except MalformedSource as exc:
            remaining.append(
                replace(item, disposition="quarantined", issues=(*item.issues, str(exc)))
            )
            continue
        if target in file_index:
            pairs[file_index[target]].append(item)
        else:
            if target is not None:
                item = replace(item, issues=(*item.issues, "missing_file"))
            remaining.append(item)
    for index, file in enumerate(files):
        if index not in pairs:
            remaining.append(file)
            continue
        group = [file, *pairs[index]]
        issues = {issue for item in group for issue in item.issues}
        originals = {item.original for item in group if item.original is not None}
        if len(originals) > 1:
            issues.add("conflicting_originals")
        elif any(item.original is None for item in group):
            issues.add("incomplete_originals")
        classification = (
            "matched"
            if not issues
            else "conflicting_originals"
            if ("conflicting_originals" in issues)
            else "paired_incomplete"
        )
        remaining.append(
            InventoryItem(
                tuple(source for item in group for source in item.sources),
                classification,
                "quarantined" if issues else "candidate",
                tuple(sorted(issues)),
                file.original if not issues else None,
                next((item.summary for item in group if item.summary is not None), None),
            )
        )
    return tuple(sorted(remaining, key=lambda item: item.item_key))


async def scan_inventory(
    roots: Sequence[RootSpec],
    *,
    vector_export: Path | None = None,
    scope_aliases: Mapping[str, Sequence[str]] | None = None,
    limits: ScanLimits | None = None,
) -> Inventory:
    """Read only supplied inputs. Exceeding global limits fails the whole scan."""
    roots = tuple(roots)
    aliases = {key: tuple(value) for key, value in (scope_aliases or {}).items()}
    if len({root.root_id for root in roots}) != len(roots) or any(
        not isinstance(root.root_id, str)
        or not _relative(root.root_id)
        or "/" in root.root_id
        or not root.source_scope
        or not root.source_kind
        for root in roots
    ):
        raise ValueError("root IDs must be unique path-free names and identity fields nonempty")
    if len(roots) > (limits or ScanLimits()).max_entries:
        raise MalformedSource("entry_count_limit")
    if any(
        isinstance(value, str) or any(not isinstance(s, str) or not s for s in value)
        for value in (scope_aliases or {}).values()
    ):
        raise ValueError("scope aliases must map to sequences of explicit nonempty scopes")

    def scan():
        scanner = _Scanner(limits or ScanLimits())
        files = [
            item for root in sorted(roots, key=lambda r: r.root_id) for item in scanner.files(root)
        ]
        observation, vectors = scanner.vectors(vector_export, aliases)
        return Inventory(
            tuple(
                {
                    "root_id": root.root_id,
                    "real_root": str(root.path.absolute()),
                    "source_scope": root.source_scope,
                    "source_kind": root.source_kind,
                    "relative_paths": sorted(set(root.relative_paths))
                    if root.relative_paths is not None
                    else None,
                }
                for root in roots
            ),
            observation,
            _reconcile(files, vectors, roots),
            tuple(scanner.artifacts.values()),
        )

    return await asyncio.to_thread(scan)
