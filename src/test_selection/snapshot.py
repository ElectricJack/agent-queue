"""A complete, bounded inventory of what a workspace changed since its base.

The snapshot is the selection's only view of the change (spec §4.1): the
merge-base of the base ref and ``HEAD``, every tracked change from that commit
through the working tree (committed, staged and unstaged edits, deletions and
both sides of a rename) and every untracked, non-ignored file, each with a
content hash and, when asked for, the function context and first lines of its
diff.

A snapshot that cannot be complete says so instead of guessing: an unknown
base, a git failure, an unreadable path, a path that resolves outside the
workspace or more changed paths than :data:`MAX_CHANGED_PATHS` each make it
incomplete with a reason from :data:`INCOMPLETE_REASONS`, and carries no
changes. The caller answers an incomplete snapshot with the full fallback.
Resolving the base never fetches.

:func:`snapshot_fingerprint` is the cheap recheck run before execution: it is
exactly the fingerprint of a snapshot taken without excerpts, so any edit
between planning and execution changes it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from src.git.manager import GitError

if TYPE_CHECKING:
    from src.git.manager import GitManager

MAX_CHANGED_PATHS = 2000
MAX_HASH_BYTES = 8 * 1024 * 1024
DEFAULT_EXCERPT_LINES = 40
INCOMPLETE_REASONS = (
    "unknown_base",
    "git_error",
    "unreadable_path",
    "path_escapes_workspace",
    "inventory_unbounded",
)

ChangeStatus = Literal["added", "modified", "deleted", "renamed", "untracked"]

#: Rename detection threshold, shared by the inventory and the excerpt diff so
#: both see the same pairs.
_RENAMES = "-M50%"
#: Flags that keep a diff's text independent of the user's git configuration.
_PLAIN_DIFF = ("--no-color", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/")
#: ``git diff --raw`` status letters. A copy is only reported when copy
#: detection is configured; its destination is a new file.
_STATUSES: dict[str, ChangeStatus] = {
    "A": "added",
    "M": "modified",
    "T": "modified",
    "U": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "added",
}
#: Bytes git inspects for a NUL to decide a file is binary.
_BINARY_SNIFF_BYTES = 8000


@dataclass(frozen=True)
class ChangedPath:
    """One changed path, relative to the workspace, in posix form."""

    path: str  # the NEW side for renames
    status: ChangeStatus
    old_path: str | None = None  # renames only
    old_blob: str | None = None  # "git:<sha1>" of the base side; None for added/untracked
    new_blob: str | None = None  # "sha256:<hex>" of the working tree, "oversize"; None if deleted
    hunks: tuple[str, ...] = ()  # function context of each hunk header, code-derived
    excerpt: str = ""  # <= excerpt_lines lines of unified diff (-U0); "" for binary and oversize


@dataclass(frozen=True)
class ChangeSnapshot:
    workspace: str
    base_ref: str
    base_sha: str
    head_sha: str
    changes: tuple[ChangedPath, ...]
    dirty_fingerprint: str
    complete: bool
    incomplete_reason: str | None  # one of INCOMPLETE_REASONS
    incomplete_detail: str | None  # the offending path or ref; never file content
    taken_at: float

    @property
    def paths(self) -> frozenset[str]:
        """Every new path plus every old path (deleted and renamed-from)."""
        return frozenset(c.path for c in self.changes) | frozenset(
            c.old_path for c in self.changes if c.old_path is not None
        )

    def fingerprint(self) -> str:
        text = f"{self.base_sha}\n{self.head_sha}\n{self.dirty_fingerprint}"
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def hash_file(path: Path, *, limit: int = MAX_HASH_BYTES) -> str:
    """Streaming sha256 of *path*'s bytes, or ``"oversize"`` once past *limit*."""
    digest, seen = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while chunk := handle.read(65536):
            seen += len(chunk)
            if seen > limit:
                return "oversize"
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


async def snapshot_fingerprint(git: GitManager, workspace: str, *, base_ref: str) -> str:
    """The fingerprint :func:`take_snapshot` would record, without reading any excerpt."""
    snapshot = await take_snapshot(git, workspace, base_ref=base_ref, excerpt_lines=0)
    return snapshot.fingerprint()


class _Incomplete(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail


@dataclass
class _Shas:
    """What is known about the two ends of the change when a step gives up."""

    base: str = ""
    head: str = ""


async def take_snapshot(
    git: GitManager,
    workspace: str,
    *,
    base_ref: str = "origin/main",
    excerpt_lines: int = DEFAULT_EXCERPT_LINES,
    clock: Callable[[], float] = time.time,
) -> ChangeSnapshot:
    """Inventory everything *workspace* changed since its merge-base with *base_ref*."""
    taken_at = clock()
    shas = _Shas()
    try:
        changes = await _inventory(git, str(workspace), base_ref, excerpt_lines, shas)
    except _Incomplete as gap:
        reason, detail, changes = gap.reason, gap.detail, ()
    else:
        reason = detail = None
    return ChangeSnapshot(
        workspace=str(workspace),
        base_ref=base_ref,
        base_sha=shas.base,
        head_sha=shas.head,
        changes=changes,
        dirty_fingerprint=_dirty_fingerprint(changes, reason, detail),
        complete=reason is None,
        incomplete_reason=reason,
        incomplete_detail=detail,
        taken_at=taken_at,
    )


async def _inventory(
    git: GitManager, workspace: str, base_ref: str, excerpt_lines: int, shas: _Shas
) -> tuple[ChangedPath, ...]:
    root = Path(workspace).resolve()
    toplevel = await _git(git, workspace, "rev-parse", "--show-toplevel")
    if toplevel is None or Path(toplevel.rstrip("\n")).resolve() != root:
        # Every path below is read relative to the repository's top level.
        raise _Incomplete("git_error", workspace)
    head = await _git(git, workspace, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if head is None:
        raise _Incomplete("git_error", "HEAD")
    shas.head = head.strip()

    # A ref shaped like an option would be read as one; it names no base.
    if not base_ref or base_ref.startswith("-"):
        raise _Incomplete("unknown_base", base_ref)
    base = await _git(git, workspace, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
    if base is None:
        raise _Incomplete("unknown_base", base_ref)
    shas.base = await git.amerge_base(workspace, base.strip(), "HEAD")
    if not shas.base:
        raise _Incomplete("unknown_base", base_ref)

    raw, others = await asyncio.gather(
        _git(git, workspace, "diff", "--raw", "-z", "--no-abbrev", _RENAMES, shas.base, "--"),
        _git(git, workspace, "ls-files", "--others", "--exclude-standard", "-z"),
    )
    if raw is None:
        raise _Incomplete("git_error", "diff")
    if others is None:
        raise _Incomplete("git_error", "ls-files")
    records = _parse_raw(raw)
    records += [ChangedPath(path=p, status="untracked") for p in others.split("\0") if p]
    if len(records) > MAX_CHANGED_PATHS:
        raise _Incomplete("inventory_unbounded", str(len(records)))

    records = await asyncio.to_thread(_read_worktree, root, records, excerpt_lines)
    if excerpt_lines > 0:
        records = await _with_diff_excerpts(git, workspace, shas.base, records, excerpt_lines)
    return tuple(sorted(records, key=lambda c: c.path))


async def _git(git: GitManager, workspace: str, *args: str) -> str | None:
    """Git's stdout, or ``None`` when it exits nonzero or cannot run."""
    try:
        result = await git.arun_git_result(list(args), cwd=workspace)
    except GitError:
        return None
    return result.stdout if result.returncode == 0 else None


def _parse_raw(output: str) -> list[ChangedPath]:
    """Parse ``git diff --raw -z``: a header field, then one path (two for R/C)."""
    fields = output.split("\0")
    if fields.pop() != "":  # every field, the last included, is NUL-terminated
        raise _Incomplete("git_error", "diff")
    records: list[ChangedPath] = []
    index = 0
    while index < len(fields):
        header = fields[index].split(" ")
        letter = header[-1][:1] if len(header) == 5 and header[0].startswith(":") else ""
        if letter not in _STATUSES:
            raise _Incomplete("git_error", "diff")
        paired = letter in "RC"
        paths = fields[index + 1 : index + (3 if paired else 2)]
        if len(paths) != (2 if paired else 1) or not all(paths):
            raise _Incomplete("git_error", "diff")
        index += len(paths) + 1
        old_sha = header[2]
        old_blob = None if not old_sha.strip("0") or letter in "AC" else f"git:{old_sha}"
        records.append(
            ChangedPath(
                path=paths[-1],
                status=_STATUSES[letter],
                old_path=paths[0] if letter == "R" else None,
                old_blob=old_blob,
            )
        )
    return records


def _read_worktree(root: Path, records: list[ChangedPath], excerpt_lines: int) -> list[ChangedPath]:
    """Confine every path to the workspace, then hash what the working tree holds."""
    for change in records:
        for path in (change.path, change.old_path):
            if path is not None and _escapes(root, path):
                raise _Incomplete("path_escapes_workspace", path)
    limit = MAX_HASH_BYTES
    read: list[ChangedPath] = []
    for change in records:
        if change.status == "deleted":
            read.append(change)
            continue
        target = root / change.path
        try:
            new_blob = hash_file(target, limit=limit)
            excerpt = ""
            if change.status == "untracked" and excerpt_lines > 0 and new_blob != "oversize":
                excerpt = _text_head(target, excerpt_lines)
        except OSError:
            # Includes a file that vanished after git listed it, and a directory
            # (an untracked nested repository is listed as ``name/``).
            raise _Incomplete("unreadable_path", change.path) from None
        read.append(replace(change, new_blob=new_blob, excerpt=excerpt))
    return read


def _escapes(root: Path, path: str) -> bool:
    if not path or os.path.isabs(path) or ".." in PurePosixPath(path).parts:
        return True
    try:
        resolved = (root / path).resolve()
    except (OSError, RuntimeError):  # a symlink loop cannot be shown to stay inside
        return True
    return not resolved.is_relative_to(root)


def _text_head(path: Path, lines: int) -> str:
    """The first *lines* lines of a UTF-8 text file; ``""`` for anything binary."""
    data = path.read_bytes()
    if b"\0" in data[:_BINARY_SNIFF_BYTES]:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return "\n".join(text.splitlines()[:lines])


async def _with_diff_excerpts(
    git: GitManager, workspace: str, base_sha: str, records: list[ChangedPath], excerpt_lines: int
) -> list[ChangedPath]:
    """Attach each tracked change's hunk contexts and first diff lines, from one diff."""
    # An oversize file's diff is as unbounded as the file; leave it out.
    skipped = [f":(exclude,literal){c.path}" for c in records if c.new_blob == "oversize"]
    patch = await _git(
        git,
        workspace,
        "-c",
        "core.quotePath=false",
        "diff",
        "-U0",
        _RENAMES,
        *_PLAIN_DIFF,
        base_sha,
        "--",
        *skipped,
    )
    if patch is None:
        raise _Incomplete("git_error", "diff")
    blocks = _parse_patch(patch, excerpt_lines)
    return [
        replace(change, hunks=blocks[change.path][0], excerpt=blocks[change.path][1])
        if change.status != "untracked" and change.path in blocks
        else change
        for change in records
    ]


def _parse_patch(patch: str, excerpt_lines: int) -> dict[str, tuple[tuple[str, ...], str]]:
    """Split a ``-U0`` diff into ``{path: (hunk contexts, excerpt)}``.

    A block is keyed by its ``+++ b/<path>``, by ``rename to <path>`` for a
    pure rename and by ``--- a/<path>`` for a deletion. A binary block and a
    path git had to quote (a control character in its name) keep no excerpt.
    """
    blocks: dict[str, tuple[tuple[str, ...], str]] = {}
    for block in _split_blocks(patch.splitlines()):
        body_at = next((i for i, line in enumerate(block) if line.startswith("@@")), len(block))
        headers, body = block[:body_at], block[body_at:]
        key = _block_path(headers)
        if key is None or any(line.startswith("Binary files ") for line in headers):
            continue
        hunks: list[str] = []
        for line in body:
            if line.startswith("@@"):
                parts = line.split("@@", 2)
                context = parts[2].strip() if len(parts) == 3 else ""
                if context and context not in hunks:
                    hunks.append(context)
        blocks[key] = (tuple(hunks), "\n".join(body[:excerpt_lines]))
    return blocks


def _split_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    for line in lines:
        if line.startswith("diff --git "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return blocks


def _block_path(headers: list[str]) -> str | None:
    old = new = renamed = None
    for line in headers:
        if line.startswith("--- "):
            old = _patch_name(line[4:], "a/")
        elif line.startswith("+++ "):
            new = _patch_name(line[4:], "b/")
        elif line.startswith("rename to "):
            renamed = line[len("rename to ") :]
    return new or renamed or old


def _patch_name(name: str, prefix: str) -> str | None:
    # git appends a tab to a ---/+++ name containing a space.
    name = name.removesuffix("\t")
    return name[len(prefix) :] if name.startswith(prefix) else None


def _dirty_fingerprint(
    changes: tuple[ChangedPath, ...], reason: str | None, detail: str | None
) -> str:
    """sha256 over the sorted (path, status, old_path, old_blob, new_blob) rows.

    An incomplete snapshot carries no rows, so its reason and detail are mixed
    in: a recheck must never mistake an incomplete snapshot for a clean tree.
    """
    rows = sorted(
        ((c.path, c.status, c.old_path, c.old_blob, c.new_blob) for c in changes),
        key=lambda row: tuple(value or "" for value in row),
    )
    payload = {"changes": rows, "incomplete": [reason, detail] if reason else None}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
