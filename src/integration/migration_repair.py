"""Prove a metadata-only repair of one frozen Alembic sibling collision.

Read Git objects, never import migration code or consult the operator database.
The returned path mapping is an exception to candidate path/blob equality only;
the caller still enforces frozen lineage, authority, and guarded publication.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from src.git.manager import GitManager

_DIRECTORY = "migrations/versions/"
_METADATA = {"revision", "down_revision", "branch_labels", "depends_on"}


@dataclass(frozen=True)
class _Migration:
    revision: str
    parent: str | None
    normalized: str


def _parse(text: str) -> _Migration | None:
    """Accept literal, unbranched metadata and fingerprint the reviewed code."""
    try:
        module = ast.parse(text)
        values = {}
        literal_targets = set()
        for node in module.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if not names & _METADATA:
                continue
            if len(targets) != 1 or len(names) != 1:
                return None
            name = next(iter(names))
            if name in values:
                return None
            literal_targets.add(targets[0])
            values[name] = ast.literal_eval(node.value)
            if name in {"revision", "down_revision"}:
                node.value = ast.Constant(value="<rechain>")
        # A conditional, augmented, destructured, or nested reassignment is
        # ambiguous metadata even when a literal declaration precedes it.
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Name)
                and node.id in _METADATA
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and node not in literal_targets
            ):
                return None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in _METADATA:
                    return None
            if isinstance(node, ast.alias) and (node.asname or node.name) in _METADATA:
                return None
        revision = values.get("revision")
        parent = values.get("down_revision")
        if (
            not isinstance(revision, str)
            or not re.fullmatch(r"[A-Za-z0-9_]+", revision)
            or "down_revision" not in values
            or (parent is not None and not isinstance(parent, str))
            or values.get("branch_labels") is not None
            or values.get("depends_on") is not None
        ):
            return None
        if module.body and isinstance(module.body[0], ast.Expr):
            doc = module.body[0].value
            if isinstance(doc, ast.Constant) and isinstance(doc.value, str):
                doc.value = re.sub(
                    r"(?m)^(Revision ID:|Revises:)[^\n]*$", r"\1 <rechain>", doc.value
                )
        return _Migration(revision, parent, ast.dump(module, include_attributes=False))
    except (SyntaxError, ValueError, TypeError, RecursionError):
        return None


def _single_head(migrations: dict[str, _Migration]) -> str | None:
    parents = {item.revision: item.parent for item in migrations.values()}
    if len(parents) != len(migrations):
        return None
    heads = set(parents) - set(parents.values())
    if len(heads) != 1:
        return None
    head = next(iter(heads))
    seen = set()
    cursor = head
    while cursor is not None:
        if cursor in seen or cursor not in parents:
            return None
        seen.add(cursor)
        cursor = parents[cursor]
    return head if seen == set(parents) else None


async def migration_rechain_paths(
    git: GitManager,
    store: Path,
    *,
    source_head: str,
    partial_head: str,
    resolved_head: str,
    source_changes: dict[str, str],
) -> dict[str, str]:
    """Return the one proved path substitution, or no exception on ambiguity."""
    changes = {
        path: status for path, status in source_changes.items() if path.startswith(_DIRECTORY)
    }
    if len(changes) != 1:
        return {}
    source_path, status = next(iter(changes.items()))
    if status != "A" or PurePosixPath(source_path).parent.as_posix() != _DIRECTORY.rstrip("/"):
        return {}
    if not source_path.endswith(".py") or PurePosixPath(source_path).name == "__init__.py":
        return {}

    async def entries(commit: str) -> dict[str, tuple[str, str]] | None:
        result = await git.arun_git_result(
            ["ls-tree", "-r", "-z", commit, "--", _DIRECTORY], cwd=str(store)
        )
        if result.returncode:
            return None
        found = {}
        for entry in result.stdout.split("\0"):
            if not entry:
                continue
            metadata, path = entry.split("\t", 1)
            mode, kind, oid = metadata.split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                return None
            found[path] = (mode, oid)
        return found

    async def migration(entry: tuple[str, str]) -> _Migration | None:
        result = await git.arun_git_result(["cat-file", "blob", entry[1]], cwd=str(store))
        return _parse(result.stdout) if result.returncode == 0 else None

    source = await entries(source_head)
    partial = await entries(partial_head)
    resolved = await entries(resolved_head)
    if source is None or partial is None or resolved is None or source_path not in source:
        return {}
    # Existing migrations, including their modes, are immutable under this proof.
    if source_path in partial or any(
        resolved.get(path) != entry for path, entry in partial.items()
    ):
        return {}
    additions = set(resolved) - set(partial)
    if len(additions) != 1:
        return {}
    resolved_path = next(iter(additions))
    original = await migration(source[source_path])
    replacement = await migration(resolved[resolved_path])
    if original is None or replacement is None or original.normalized != replacement.normalized:
        return {}
    if source[source_path][0] != resolved[resolved_path][0]:
        return {}
    prefix = _DIRECTORY + original.revision + "_"
    renamed = (
        _DIRECTORY + replacement.revision + "_" + source_path[len(prefix) :]
        if source_path.startswith(prefix)
        else source_path
    )
    if resolved_path not in {source_path, renamed}:
        return {}

    migrations = {}
    for path, entry in partial.items():
        if not path.endswith(".py") or PurePosixPath(path).name == "__init__.py":
            continue
        parsed = await migration(entry)
        if parsed is None:
            return {}
        migrations[path] = parsed
    head = _single_head(migrations)
    collision = next(
        (item for item in migrations.values() if item.revision == original.revision), None
    )
    if (
        head is None
        or collision is None
        or collision.parent != original.parent
        or replacement.parent != head
        or replacement.revision in {item.revision for item in migrations.values()}
    ):
        return {}
    return {source_path: resolved_path}
