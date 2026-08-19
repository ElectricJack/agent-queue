"""Message commands mixin — the inter-agent message queue surface.

Substrate placeholder (Wave 0) — this module is intentionally empty.  The
mixin is already registered in ``CommandHandler``'s bases so Wave 2 lane 2D can add
its ``_cmd_*`` methods here without touching ``handler.py``.

Planned commands:

    ``_cmd_message_send``, ``_cmd_message_inbox``, ``_cmd_message_read``,
    ``_cmd_message_reply`` — see
    docs/specs/implementation/supervisor-agent.md §4–§5.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class MessageCommandsMixin:
    """Message command methods mixed into CommandHandler."""
