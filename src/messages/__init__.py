"""Inter-agent messaging surface — public exports.

The messages package ships in phases (supervisor-agent implementation
§11):

* Phase 0-1 (already merged): the ``messages`` table, models, queries,
  and CommandHandler commands.
* Phase 3 (this wave): :class:`SessionManagerProto` — the narrow adapter
  the ``MessageDeliveryEngine`` consumes to observe and drive named
  sessions — plus :class:`SessionLens`, the production implementation
  over the session runtime.

Only the delivery-engine-facing adapter and its typing live here. The
row model is :class:`src.models.Message`, the queries are
:mod:`src.database.queries.message_queries`, and the CommandHandler mixin
is :mod:`src.commands.message_commands`.
"""

from __future__ import annotations

from src.messages.delivery import PARK_AFTER_SECONDS, MessageDeliveryEngine
from src.messages.session_lens import Activity, SessionLens, SessionManagerProto

__all__ = [
    "Activity",
    "MessageDeliveryEngine",
    "PARK_AFTER_SECONDS",
    "SessionLens",
    "SessionManagerProto",
]
