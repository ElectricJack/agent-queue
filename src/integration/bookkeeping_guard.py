"""Refuse legacy reviewed evidence that has not been transferred into shards.

Checks inspect each source tree before generated outputs are discarded. Git's
low-level driver has only blobs on older Git versions; callers can supply exact
side refs, otherwise the driver conservatively refuses changed legacy evidence.
"""

from __future__ import annotations

import json
import os
from difflib import SequenceMatcher
from pathlib import Path

from src.git.manager import GitError, GitManager

SOURCE = "tests/fixtures/generated_sources"
LEGACY_SOURCES = {
    f"{SOURCE}/ownership/integration_ownership.json": (
        f"{SOURCE}/ownership/modules/integration_ownership/", ".json",
    ),
    f"{SOURCE}/ownership/integration_ownership_report.json": (
        f"{SOURCE}/ownership/modules/integration_ownership_report/", ".json",
    ),
    "tests/fixtures/contracts/generated-command-audit.json": (
        "tests/fixtures/contracts/command_audit_modules/", ".json",
    ),
    "tests/test_command_descriptors.py": (f"{SOURCE}/command_descriptors/", ".py.inc"),
}
RECIPE = "docs/contributing/codegen.md#legacy-bookkeeping-migration"


class LegacyBookkeepingConflict(ValueError):
    def __init__(self, paths):
        self.paths = tuple(sorted(paths))
        super().__init__(
            "Unmigrated legacy bookkeeping evidence: " + ", ".join(self.paths)
            + f". Transfer the reviewed aggregate deltas into authored shards; see {RECIPE}."
        )


def _combine(target, incoming):
    for key, value in incoming.items():
        if key not in target:
            target[key] = value
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _combine(target[key], value)
        elif isinstance(value, list) and isinstance(target[key], list):
            if any(item in target[key] for item in value):
                raise ValueError(f"duplicate evidence for {key}")
            target[key].extend(value)
        else:
            raise ValueError(f"duplicate evidence for {key}")


def projection(path: str, files: dict[str, bytes]):
    prefix, suffix = LEGACY_SOURCES[path]
    fragments = [data for name, data in sorted(files.items())
                 if name.startswith(prefix) and name.endswith(suffix)
                 and (suffix != ".py.inc" or "/" not in name.removeprefix(prefix))]
    if not fragments:
        raise ValueError("missing authored bookkeeping fragments")
    if suffix == ".py.inc":
        return b"".join(fragments)
    assembled = {}
    for data in fragments:
        _combine(assembled, json.loads(data))
    for field in ("owned_fingerprints", "owned_file_fingerprints"):
        if field in assembled:
            assembled[field].sort()
    return assembled


def represented(path: str, aggregate: bytes | None, files: dict[str, bytes]) -> bool:
    if aggregate is None:
        return False
    try:
        assembled = projection(path, files)
        return (aggregate == assembled if isinstance(assembled, bytes) else
                json.dumps(json.loads(aggregate), sort_keys=True) == json.dumps(assembled, sort_keys=True))
    except (ValueError, TypeError, AttributeError):
        return False


def same_evidence(path: str, left: bytes | None, right: bytes | None) -> bool:
    if left == right:
        return True
    if left is None or right is None or path.endswith(".py"):
        return False
    try:
        return json.dumps(json.loads(left), sort_keys=True) == json.dumps(json.loads(right), sort_keys=True)
    except ValueError:
        return False


def represented_delta(path, before, after, files):
    """Permit stale outputs only where every reviewed delta is still in sources.

    Intermediate batch trees may retain old outputs while accumulating shard
    edits. Comparing the changed evidence avoids treating that lag as new work.
    """
    if represented(path, after, files):
        return True
    if before is None or after is None:
        return False
    try:
        expected = projection(path, files)
        if isinstance(expected, bytes):
            original = before.splitlines(keepends=True)
            edited = after.splitlines(keepends=True)
            projected = expected.splitlines(keepends=True)
            expected_edits = SequenceMatcher(None, original, projected, autojunk=False).get_opcodes()
            for tag, start, end, a, b in SequenceMatcher(
                None, original, edited, autojunk=False,
            ).get_opcodes():
                if tag == "equal":
                    continue
                if not any(
                    kind == tag and lo == start and hi == end
                    and (edited[a:b] == projected[x:y] or (
                        tag == "insert" and b"".join(edited[a:b]) in b"".join(projected[x:y])
                    )) for kind, lo, hi, x, y in expected_edits
                ):
                    return False
            return True
        missing = object()

        def matches(old, new, source):
            if old is not missing and new is not missing and (
                json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True)
            ):
                return True
            if new is missing:
                return source is missing
            if isinstance(new, dict) and isinstance(source, dict):
                old = old if isinstance(old, dict) else {}
                return all(matches(old.get(key, missing), new.get(key, missing),
                                   source.get(key, missing)) for key in old.keys() | new.keys())
            if isinstance(old, list) and isinstance(new, list) and isinstance(source, list):
                prior, edited, projected = (
                    {json.dumps(item, sort_keys=True) for item in values}
                    for values in (old, new, source)
                )
                if prior != edited:
                    return edited - prior <= projected and not (prior - edited) & projected
            return source is not missing and (
                json.dumps(new, sort_keys=True) == json.dumps(source, sort_keys=True)
            )

        return matches(json.loads(before), json.loads(after), expected)
    except (ValueError, TypeError, AttributeError):
        return False


async def tree_files(git: GitManager, store: Path, ref: str) -> dict[str, bytes]:
    """Read evidence in two Git calls, without checking out or executing a side."""
    paths = sorted(set(LEGACY_SOURCES) | {value[0] for value in LEGACY_SOURCES.values()})
    listed = await git.arun_git_result(
        ["ls-tree", "-r", "--name-only", "-z", ref, "--", *paths], cwd=str(store),
    )
    if listed.returncode:
        raise GitError(listed.stderr or f"cannot inspect bookkeeping tree {ref}")
    names = sorted(set(listed.stdout.split("\0")) - {""})
    if not names:
        return {}
    result = await git.arun_git_result(
        ["cat-file", "--batch"], cwd=str(store),
        stdin="".join(f"{ref}:{name}\n" for name in names),
    )
    if result.returncode:
        raise GitError(result.stderr or f"cannot read bookkeeping tree {ref}")
    data = result.stdout.encode()
    files = {}
    offset = 0
    for name in names:
        end = data.index(b"\n", offset)
        header = data[offset:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise GitError(f"cannot read bookkeeping blob {ref}:{name}")
        size = int(header[2])
        files[name] = data[end + 1:end + 1 + size]
        offset = end + size + 2
    return files


async def check_merge(git: GitManager, store: Path, base: str, *sides: str) -> None:
    snapshots = [await tree_files(git, store, ref) for ref in (base, *sides)]
    if not any(any(name.startswith(prefix) for name in files)
               for files in snapshots for prefix, _ in LEGACY_SOURCES.values()):
        return
    before, *branches = snapshots
    introductions = set()
    cache = dict(zip((base, *sides), snapshots, strict=True))
    for ref, files in zip(sides, branches, strict=True):
        prefixes = {prefix for prefix, _ in LEGACY_SOURCES.values()
                    if any(name.startswith(prefix) for name in files)
                    and not any(name.startswith(prefix) for name in before)}
        if not prefixes:
            continue
        parents = await git.arun_git_result(["rev-list", "--parents", "-n", "1", ref], cwd=str(store))
        if parents.returncode:
            raise GitError(parents.stderr or "cannot inspect migration ancestry")
        inherited = set()
        for parent in parents.stdout.split()[1:]:
            if parent not in cache:
                cache[parent] = await tree_files(git, store, parent)
            inherited.update(prefix for prefix in prefixes
                             if any(name.startswith(prefix) for name in cache[parent]))
        introductions.update((ref, prefix) for prefix in prefixes - inherited)

    def unsafe(ref, files, path):
        prefix, _ = LEGACY_SOURCES[path]
        if (ref, prefix) in introductions:
            return not represented(path, files.get(path), files)
        return (not same_evidence(path, files.get(path), before.get(path))
                and not represented_delta(path, before.get(path), files.get(path), files))

    refused = {path for ref, files in zip(sides, branches, strict=True)
               for path in LEGACY_SOURCES if unsafe(ref, files, path)}
    if refused:
        raise LegacyBookkeepingConflict(refused)


async def check_worktree(root: Path) -> None:
    """Check committed evidence plus edited aggregates before any generator runs.

    Unchanged outputs may lag dirty shards: regeneration will project those
    authored edits. An edited aggregate must already be represented, however.
    """
    git = GitManager()
    # The train restores generated outputs before its final sweep. Revalidate
    # the exact frozen source inputs, rather than interpreting those temporary
    # restored projections as newly authored legacy changes.
    provenance = os.environ.get("AQ_GENERATED_MERGE_INPUTS")
    committed = await tree_files(git, root, "HEAD")
    files = {}
    for path, (prefix, suffix) in LEGACY_SOURCES.items():
        target = root / path
        if target.is_file():
            files[path] = target.read_bytes()
        for source in (root / prefix).rglob(f"*{suffix}"):
            files[source.relative_to(root).as_posix()] = source.read_bytes()
    if provenance:
        inputs = json.loads(provenance)
        if not inputs:
            raise ValueError("empty generated merge provenance")
        snapshots = []
        for base, current, other in inputs:
            await check_merge(git, root, base, current, other)
            snapshots.extend([await tree_files(git, root, ref) for ref in (base, current, other)])
        refused = {
            path for path in LEGACY_SOURCES
            if not represented(path, files.get(path), files)
            and not any(same_evidence(path, files.get(path), source.get(path)) for source in snapshots)
        }
        if refused:
            raise LegacyBookkeepingConflict(refused)
        return
    refused = set()
    for path in LEGACY_SOURCES:
        if path in committed and not represented(path, committed[path], committed):
            refused.add(path)
        if files.get(path) != committed.get(path) and not represented(path, files.get(path), files):
            refused.add(path)
    unmerged = await git.arun_git_result(
        ["diff", "--name-only", "--diff-filter=U", "-z"], cwd=str(root),
    )
    if unmerged.returncode:
        raise GitError(unmerged.stderr or "cannot inspect unresolved bookkeeping evidence")
    refused.update(set(unmerged.stdout.split("\0")) & LEGACY_SOURCES.keys())
    if refused:
        raise LegacyBookkeepingConflict(refused)
