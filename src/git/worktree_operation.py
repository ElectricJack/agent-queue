"""Merges, rebases, cherry-picks and reverts a worktree is part-way through."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: Per-worktree state files (``git rev-parse --git-path``) of operations a
#: branch switch cannot carry. A merge is reported with its heads instead.
_OPERATIONS = (
    ("rebase", "rebase-merge"),
    ("rebase", "rebase-apply"),
    ("cherry_pick", "CHERRY_PICK_HEAD"),
    ("revert", "REVERT_HEAD"),
)


async def operation_in_progress(git, path: str) -> dict[str, Any] | None:
    """The operation the worktree at *path* is part-way through, or None.

    A merge carries its ``merge_heads`` and whether any path is still
    ``unmerged``; every other operation reports ``unmerged`` as None.
    """
    names = ["MERGE_HEAD", *(name for _, name in _OPERATIONS)]
    args = ["rev-parse", "--path-format=absolute"]
    for name in names:
        args += ["--git-path", name]
    found = dict(zip(names, (await git._arun(args, cwd=path)).splitlines(), strict=True))
    for operation, name in _OPERATIONS:
        if os.path.exists(found[name]):
            return {"worktree": path, "operation": operation, "unmerged": None}
    if not os.path.exists(found["MERGE_HEAD"]):
        return None
    heads = Path(found["MERGE_HEAD"]).read_text().split()
    unmerged = await git._arun(["ls-files", "--unmerged"], cwd=path)
    return {
        "worktree": path, "operation": "merge", "merge_heads": heads,
        "unmerged": bool(unmerged.strip()),
    }


def unkeepable(operation: dict[str, Any]) -> str | None:
    """Why a snapshot commit cannot keep *operation*: all but a resolved merge."""
    if operation["operation"] != "merge":
        return f"a {operation['operation'].replace('_', '-')} in progress"
    return "a merge with unmerged paths" if operation["unmerged"] else None
