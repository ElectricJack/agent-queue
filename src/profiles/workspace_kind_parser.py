"""Parse vault/[projects/<pid>/]workspace-kinds/<id>.md into WorkspaceKind.

See spec §4.1 for the markdown frontmatter format.  Body text is used as
``description`` when the frontmatter ``description`` key is absent.
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from src.models import WORKSPACE_KIND_MODES, WorkspaceKind

# Lock-mode values accepted in the frontmatter.  Match WorkspaceMode.value.
_VALID_LOCK_MODES = frozenset({"exclusive", "branch_isolated", "directory_isolated"})


def parse_workspace_kind_file(path: Path, project_id: str) -> WorkspaceKind:
    """Parse one markdown file into a WorkspaceKind.

    Args:
        path: Markdown file to parse.
        project_id: ``SYSTEM_KIND_SCOPE`` for system kinds, the project id
            otherwise.

    Raises:
        ValueError: If the file is missing required frontmatter (``id``)
            or has an invalid value (e.g. unknown ``default_lock_mode``).
    """
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)

    if not isinstance(fm, dict) or "id" not in fm:
        raise ValueError(f"{path}: frontmatter is missing required key 'id'")

    description = fm.get("description")
    if not description:
        # Strip a leading "# heading" line so the body text is just the prose.
        description = _body_text(body)

    lock_mode = fm.get("default_lock_mode")
    if lock_mode is not None and lock_mode not in _VALID_LOCK_MODES:
        raise ValueError(
            f"{path}: default_lock_mode={lock_mode!r} is not one of "
            f"{sorted(_VALID_LOCK_MODES)}"
        )

    # Git provisioning strategy (worktree-execution §2.1 / §3.6).  The
    # frontmatter is the source of truth (principle #1); ``worktrees.enabled``
    # is only the rollout gate applied at acquisition time.
    #
    # An **absent** ``mode:`` key parses to ``None``, which
    # ``upsert_workspace_kind`` reads as "leave the stored value alone".  It
    # must not default to ``worktree``: ``WorkspaceKindStore.bootstrap`` only
    # writes files that do not already exist, so an install upgrading with a
    # pre-``mode`` ``project-repo.md`` never gains the key — and defaulting
    # would then upsert ``worktree`` over the migration's ``exclusive-clone``
    # backfill on the first daemon start, and on every start after,
    # falsifying §7.1 ("no existing install changes behavior on upgrade") at
    # the data layer.  ``WorkspaceKindStore.backfill_mode_frontmatter``
    # injects the DB's value into such files so the markdown becomes
    # explicit; this parse rule is what keeps the window safe.
    mode = fm.get("mode")
    if mode is not None:
        mode = str(mode)
        if mode not in WORKSPACE_KIND_MODES:
            raise ValueError(
                f"{path}: mode={mode!r} is not one of {sorted(WORKSPACE_KIND_MODES)}"
            )

    setup_raw = fm.get("worktree_setup", [])
    if setup_raw is None:
        setup_raw = []
    if not isinstance(setup_raw, list) or not all(
        isinstance(c, str) for c in setup_raw
    ):
        raise ValueError(
            f"{path}: worktree_setup must be a list of shell command strings"
        )

    now = time.time()
    return WorkspaceKind(
        project_id=project_id,
        id=str(fm["id"]),
        description=description,
        writable=bool(fm.get("writable", True)),
        lockable=bool(fm.get("lockable", True)),
        is_git_repo=bool(fm.get("is_git_repo", True)),
        repo_url=fm.get("repo_url"),
        default_lock_mode=lock_mode,
        auto_attach=bool(fm.get("auto_attach", False)),
        mode=mode,
        worktree_setup=list(setup_raw),
        created_at=now,
        updated_at=now,
    )


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter dict, body text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        # Frontmatter that parses as a non-dict (e.g. a list) is invalid for
        # our schema — surface as empty so the missing-id check fires.
        fm = {}
    return fm, body


def _body_text(body: str) -> str:
    """Return the body prose without a leading ``# heading`` line."""
    stripped = body.strip()
    if stripped.startswith("#"):
        # Drop the first line (the heading), then re-strip.
        first_break = stripped.find("\n")
        if first_break == -1:
            return ""
        stripped = stripped[first_break + 1 :].strip()
    return stripped
