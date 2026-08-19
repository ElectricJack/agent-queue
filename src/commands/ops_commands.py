"""Ops commands mixin — doctor checks and cost/usage rollups.

Substrate placeholder (Wave 0) — this module is intentionally empty.  The
mixin is already registered in ``CommandHandler``'s bases so Wave 1 lane 1C can add
its ``_cmd_*`` methods here without touching ``handler.py``.

Planned commands:

    ``_cmd_doctor``, ``_cmd_cost_report`` — see
    docs/specs/implementation/trust-and-ops.md §5–§6.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class OpsCommandsMixin:
    """Ops command methods mixed into CommandHandler."""
