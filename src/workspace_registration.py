"""Read-only checks for workspace registrations whose directories disappeared."""

from __future__ import annotations

from pathlib import Path


def path_is_missing(path: str) -> bool:
    """Only confirmed absence counts; an inaccessible mount is not stale inventory."""
    if not path:
        return False
    try:
        Path(path).stat()
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


def workspace_missing_detail(workspace) -> str:
    """Give an operator a concrete repair, preserving locks and registration state."""
    detail = f"{workspace.workspace_path} is missing; restore the directory"
    if workspace.locked_by_agent_id or workspace.locked_by_task_id:
        return detail + "; verify the holder and release its lock before removing the registration"
    return detail + f" or run aq project remove-workspace --workspace-id {workspace.id}"
