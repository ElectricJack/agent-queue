"""Worktree commands mixin — workspace slot inspection and repair.

Substrate placeholder (Wave 0) — this module is intentionally empty.  The
mixin is already registered in ``CommandHandler``'s bases so Wave 2 lane 2B can add
its ``_cmd_*`` methods here without touching ``handler.py``.

Planned commands:

    ``_cmd_workspace_doctor``, ``_cmd_workspace_reap`` — see
    docs/specs/implementation/worktree-execution.md §6.8.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class WorktreeCommandsMixin:
    """Worktree command methods mixed into CommandHandler."""
