"""Session commands mixin — agent session lifecycle and task handoff.

Substrate placeholder (Wave 0) — this module is intentionally empty.  The
mixin is already registered in ``CommandHandler``'s bases so Wave 2 lane 2A can add
its ``_cmd_*`` methods here without touching ``handler.py``.

Planned commands:

    ``_cmd_session_list``, ``_cmd_session_show``, ``_cmd_session_stop``,
    ``_cmd_task_close``, ``_cmd_task_heartbeat`` — see
    docs/specs/implementation/session-runtime.md §3–§4.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SessionCommandsMixin:
    """Session command methods mixed into CommandHandler."""
