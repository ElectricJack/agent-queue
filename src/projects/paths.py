"""Root-relative path validation and safe directory listing.

Project-onboarding design §3.3, §5.1 and §7: the dashboard only ever sends a
configured root id plus a *relative* descendant path, and the backend re-runs
this validation for every browse, preflight and mutation.  This module is the
single implementation of that check.

Contract
--------
* :func:`validate_relative_path` turns ``(root, rel)`` into a
  :class:`ResolvedPath` or raises :class:`ProjectPathError`.  It rejects
  absolute child paths, ``..`` / ``.`` / empty components, NUL bytes and
  platform-invalid names; resolves symlinks; and requires the *real* target
  to stay beneath the root using component-aware containment
  (``Path.is_relative_to`` on resolved paths), never string-prefix matching.
  Any path that traverses or aliases an AQ-managed ``.aq/worktrees`` tree is
  rejected as well.
* :func:`list_directory` returns a bounded, name-ordered listing of
  :class:`DirectoryEntry` records.  Hidden entries are omitted by default; a
  symlink whose real target lies outside the root (or inside a managed
  worktree tree) is listed as *not a directory* and its children are never
  revealed.  ``selectable`` is true only for a valid Git worktree root.
* Failures carry one of four codes — :class:`ProjectPathCode` — matching the
  ``browse_project_root`` API contract.  Syntactic rejections (absolute
  child, ``..``, NUL, invalid names) are reported as ``root_escape``: from
  the caller's point of view they are all "not a root-relative capability".

The module is I/O-light (a handful of ``stat`` calls per path, one
``scandir`` per listing), runs no Git commands, never returns file contents,
and does not import ``src.config`` or the database: it takes an
already-resolved root ``Path``.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

__all__ = [
    "DEFAULT_LIST_LIMIT",
    "MANAGED_WORKTREES_PARTS",
    "DirectoryEntry",
    "DirectoryListing",
    "ProjectPathCode",
    "ProjectPathError",
    "ResolvedPath",
    "invalid_component_reason",
    "is_git_worktree_root",
    "list_directory",
    "validate_relative_path",
]

#: Upper bound applied by :func:`list_directory` when the caller passes none.
DEFAULT_LIST_LIMIT = 500

#: The consecutive path components that mark an AQ-managed worktree tree
#: (``<repo>/.aq/worktrees/slot-N``).  Any path whose lexical *or* real
#: components contain this pair is off-limits to browsing and onboarding.
MANAGED_WORKTREES_PARTS: tuple[str, ...] = (".aq", "worktrees")

_WINDOWS = os.name == "nt"

_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*\\')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


# --------------------------------------------------------------------------
# failure codes
# --------------------------------------------------------------------------


class ProjectPathCode(StrEnum):
    """Structured failure codes shared by validation and listing (design §5.1)."""

    NOT_FOUND = "not_found"
    NOT_DIRECTORY = "not_directory"
    ROOT_ESCAPE = "root_escape"
    ROOT_UNAVAILABLE = "root_unavailable"


class ProjectPathError(ValueError):
    """A relative path was rejected or could not be served.

    ``code`` is the structured reason, ``relative_path`` the caller's input
    verbatim (never a resolved absolute path — the message must stay safe to
    return to the dashboard), ``message`` a short operator-facing sentence.
    """

    def __init__(self, code: ProjectPathCode, relative_path: str, message: str) -> None:
        self.code = ProjectPathCode(code)
        self.relative_path = relative_path
        self.message = message
        super().__init__(f"{self.code.value}: {message}")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code.value,
            "relative_path": self.relative_path,
            "message": self.message,
        }


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """A validated root-relative path.

    ``root`` is the real (symlink-resolved) root; ``relative`` the normalised
    posix-style relative path (``""`` for the root itself); ``path`` the
    lexical ``root / relative``; ``real_path`` the symlink-resolved target,
    which is guaranteed to be ``root`` or beneath it.  ``exists`` / ``is_dir``
    describe ``real_path`` at validation time — mutation callers must re-run
    validation at mutation time (design §3.3, TOCTOU).
    """

    root: Path
    relative: str
    path: Path
    real_path: Path
    exists: bool
    is_dir: bool


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """One browse result row.  Exactly the fields design §3.3 allows — no more."""

    name: str
    relative_path: str
    is_dir: bool
    is_git_repo: bool
    selectable: bool


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """A bounded listing of one directory beneath a root."""

    relative: str
    entries: tuple[DirectoryEntry, ...]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative": self.relative,
            "entries": [asdict(entry) for entry in self.entries],
            "truncated": self.truncated,
        }


# --------------------------------------------------------------------------
# component rules
# --------------------------------------------------------------------------


def invalid_component_reason(component: str, *, windows: bool = _WINDOWS) -> str | None:
    """Why *component* may not appear in a root-relative path, or ``None``.

    Structural components (``""``, ``.``, ``..``) and control characters
    (NUL included) are rejected everywhere.  With ``windows=True`` the
    Windows-invalid characters, reserved device names and trailing dot /
    space are rejected as well; POSIX accepts them as ordinary bytes.
    """
    if component == "":
        return "empty path component"
    if component == ".":
        return "'.' path component"
    if component == "..":
        return "parent-directory ('..') component"
    if "\0" in component:
        return "NUL byte in path component"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in component):
        return "control character in path component"
    if windows:
        if any(ch in _WINDOWS_INVALID_CHARS for ch in component):
            return "character not allowed in a Windows file name"
        if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            return "reserved Windows device name"
        if component != component.rstrip(". "):
            return "trailing dot or space is not allowed in a Windows file name"
    return None


def _is_absolute(text: str) -> bool:
    """True for anything that names an absolute location on any platform.

    Checked on every platform: a ``C:`` drive or a leading backslash is a
    legal POSIX file name, but the dashboard never sends one and rejecting
    it costs nothing.
    """
    if text.startswith("/"):
        return True
    if PurePosixPath(text).is_absolute():
        return True
    win = PureWindowsPath(text)
    return bool(win.drive or win.root)


def _split_components(rel: str) -> tuple[str, ...]:
    """Split *rel* into validated components; ``()`` names the root itself."""
    if "\0" in rel:
        raise ProjectPathError(ProjectPathCode.ROOT_ESCAPE, rel, "NUL byte in path")
    text = rel.replace("\\", "/") if _WINDOWS else rel
    if text in ("", "."):
        return ()
    if _is_absolute(text):
        raise ProjectPathError(
            ProjectPathCode.ROOT_ESCAPE, rel, "absolute paths are not root-relative"
        )
    parts = tuple(text.split("/"))
    for part in parts:
        reason = invalid_component_reason(part, windows=_WINDOWS)
        if reason is not None:
            raise ProjectPathError(ProjectPathCode.ROOT_ESCAPE, rel, reason)
    return parts


def _touches_managed_worktrees(parts: tuple[str, ...]) -> bool:
    """True when *parts* contains the ``.aq``/``worktrees`` pair consecutively."""
    head, tail = MANAGED_WORKTREES_PARTS
    return any(parts[i] == head and parts[i + 1] == tail for i in range(len(parts) - 1))


def _contained(candidate: Path, root: Path) -> bool:
    """Component-aware containment of two already-resolved paths."""
    return candidate == root or candidate.is_relative_to(root)


# --------------------------------------------------------------------------
# root resolution
# --------------------------------------------------------------------------


def _resolve_root(root: Path, rel: str) -> Path:
    try:
        real = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ProjectPathError(
            ProjectPathCode.ROOT_UNAVAILABLE, rel, "project root does not exist"
        ) from None
    if not real.is_dir():
        raise ProjectPathError(
            ProjectPathCode.ROOT_UNAVAILABLE, rel, "project root is not a directory"
        )
    if _touches_managed_worktrees(real.parts):
        raise ProjectPathError(
            ProjectPathCode.ROOT_UNAVAILABLE,
            rel,
            "project root lies inside an AQ-managed worktree tree",
        )
    return real


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def validate_relative_path(
    root: Path, rel: str, *, require_directory: bool = False
) -> ResolvedPath:
    """Validate *rel* as a root-relative path beneath *root*.

    Raises :class:`ProjectPathError` with ``root_unavailable`` when the root
    itself cannot be served, ``root_escape`` for any syntactic rejection or a
    real target outside the root (or inside a managed worktree tree), and —
    only with ``require_directory=True`` — ``not_found`` / ``not_directory``
    for the target's state.  Without it a missing target is legal: create
    destinations must not exist yet.
    """
    if not isinstance(rel, str):
        raise TypeError(f"relative path must be a str, got {type(rel).__name__}")
    root_real = _resolve_root(root, rel)
    components = _split_components(rel)
    if _touches_managed_worktrees(components):
        raise ProjectPathError(
            ProjectPathCode.ROOT_ESCAPE, rel, "path enters an AQ-managed worktree tree"
        )
    lexical = root_real.joinpath(*components)
    try:
        real = lexical.resolve()
    except (OSError, RuntimeError, ValueError):
        raise ProjectPathError(
            ProjectPathCode.ROOT_ESCAPE, rel, "path could not be resolved"
        ) from None
    if not _contained(real, root_real):
        raise ProjectPathError(
            ProjectPathCode.ROOT_ESCAPE, rel, "path resolves outside the project root"
        )
    if _touches_managed_worktrees(real.parts):
        raise ProjectPathError(
            ProjectPathCode.ROOT_ESCAPE, rel, "path aliases an AQ-managed worktree tree"
        )
    try:
        exists = real.exists()
        is_dir = exists and real.is_dir()
    except OSError:
        exists = is_dir = False
    if require_directory:
        if not exists:
            raise ProjectPathError(ProjectPathCode.NOT_FOUND, rel, "directory not found")
        if not is_dir:
            raise ProjectPathError(ProjectPathCode.NOT_DIRECTORY, rel, "not a directory")
    return ResolvedPath(
        root=root_real,
        relative="/".join(components),
        path=lexical,
        real_path=real,
        exists=exists,
        is_dir=is_dir,
    )


def is_git_worktree_root(path: Path) -> bool:
    """True when *path* is a directory holding a Git worktree.

    Accepts the two shapes Git itself produces: a ``.git`` directory that
    carries a ``HEAD`` file, or a ``.git`` *file* starting with ``gitdir:``
    (a linked worktree).  A bare repository is not a worktree root.  Reads at
    most the first bytes of the pointer file; runs no Git command.
    """
    dot_git = Path(path) / ".git"
    try:
        if dot_git.is_dir():
            return (dot_git / "HEAD").is_file()
        if dot_git.is_file():
            with dot_git.open("rb") as fh:
                return fh.read(7) == b"gitdir:"
    except OSError:
        return False
    return False


def list_directory(
    root: Path,
    rel: str,
    *,
    include_hidden: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
) -> DirectoryListing:
    """List the children of ``root / rel`` safely.

    Entries are name-ordered (code-point order) and bounded by *limit*, with
    ``truncated`` set when more were available.  Hidden (dot-prefixed)
    entries are omitted unless ``include_hidden`` is set and never count
    against the bound.  A symlink whose real target escapes the root, or that
    aliases a managed worktree tree, is reported with ``is_dir=False`` so a
    browser never descends into it; browsing it directly raises
    ``root_escape``.  Only a valid Git worktree root is ``selectable``.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit!r}")
    resolved = validate_relative_path(root, rel, require_directory=True)
    dir_real = resolved.real_path
    try:
        with os.scandir(dir_real) as it:
            names = [entry.name for entry in it if include_hidden or not entry.name.startswith(".")]
    except FileNotFoundError:
        raise ProjectPathError(ProjectPathCode.NOT_FOUND, rel, "directory not found") from None
    except NotADirectoryError:
        raise ProjectPathError(ProjectPathCode.NOT_DIRECTORY, rel, "not a directory") from None
    except OSError:
        raise ProjectPathError(
            ProjectPathCode.ROOT_UNAVAILABLE, rel, "directory could not be read"
        ) from None
    names.sort()
    truncated = len(names) > limit
    entries = tuple(
        _describe_entry(dir_real, resolved.root, resolved.relative, name) for name in names[:limit]
    )
    return DirectoryListing(relative=resolved.relative, entries=entries, truncated=truncated)


def _describe_entry(dir_real: Path, root_real: Path, relative: str, name: str) -> DirectoryEntry:
    """Describe one child of an already-validated directory.

    ``dir_real`` is real, so a non-symlink child's real path is simply
    ``dir_real / name`` with no further lookups; only symlinks are resolved.
    Anything that fails validation — an escaping or dangling symlink, an
    alias of a managed worktree tree — is reported as not a directory.
    """
    relative_path = f"{relative}/{name}" if relative else name
    lexical = dir_real / name
    is_dir = False
    try:
        if lexical.is_symlink():
            real = lexical.resolve()
            if _contained(real, root_real) and not _touches_managed_worktrees(real.parts):
                is_dir = real.is_dir()
        else:
            real = lexical
            if not _touches_managed_worktrees(real.parts):
                is_dir = lexical.is_dir()
    except (OSError, RuntimeError, ValueError):
        real = lexical
        is_dir = False
    is_git_repo = is_dir and is_git_worktree_root(real)
    return DirectoryEntry(
        name=name,
        relative_path=relative_path,
        is_dir=is_dir,
        is_git_repo=is_git_repo,
        selectable=is_git_repo,
    )
