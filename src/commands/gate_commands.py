"""Gate commands mixin — create, list, show, and resolve work-graph gates.

Substrate placeholder (Wave 0) — this module is intentionally empty.  The
mixin is already registered in ``CommandHandler``'s bases so Wave 2 lane 2C can add
its ``_cmd_*`` methods here without touching ``handler.py``.

Planned commands:

    ``_cmd_gate_create``, ``_cmd_gate_list``, ``_cmd_gate_show``,
    ``_cmd_gate_resolve`` — see docs/specs/implementation/work-graph.md §5.
    Discord buttons and the dashboard call ``gate_resolve``; there is no
    other resolution path for ``human`` gates.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class GateCommandsMixin:
    """Gate command methods mixed into CommandHandler."""
