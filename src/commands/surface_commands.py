"""Surface commands mixin — the agent-facing context and schema surface.

Substrate placeholder (Wave 0) — this module is intentionally empty.  The
mixin is already registered in ``CommandHandler``'s bases so Wave 2 lane 2E can add
its ``_cmd_*`` methods here without touching ``handler.py``.

Planned commands:

    ``_cmd_prime``, ``_cmd_task_handoff``, ``_cmd_get_schema`` — see
    docs/specs/implementation/aq-surface.md §2–§3.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SurfaceCommandsMixin:
    """Surface command methods mixed into CommandHandler."""
