"""The narrow outbox port conversation commands enqueue through (mention-routing spec §4.2).

Every Discord side effect of a conversation -- the thread-open ack, a reply, a
bounded notice, the offline status line -- is a row in the shared
``outbound_deliveries`` outbox owned by the supervisor narrative spec (§4.1),
leased and sent by its escalation-first dispatcher through a typed
``conversation`` adapter. Commands depend only on this port:

* :class:`ConversationOutbox` is what ``supervisor_inbox_post``,
  ``supervisor_inbox_reply`` and the maintenance sweep call.  ``enqueue`` is
  idempotent on ``dedup_key`` and returns the outbox row id.
* The daemon binds ``DurableConversationOutbox`` after Discord cutover completes.
* :class:`UnboundOutbox` is the daemon's binding before transport readiness:
  ``bound`` is ``False``, so the preconditions report
  ``outbox_unbound`` and the route refuses every message before anything
  would be enqueued -- the feature cannot half-work.
* :class:`RecordingOutbox` is the in-memory binding tests use.

No code path outside the delivery adapter ever calls a transport.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: Default priority of a conversation delivery.  Escalations outrank it in the
#: shared dispatcher, which orders escalation-first regardless.
DEFAULT_PRIORITY = 20

#: Every conversation action the daemon may reserve.  A producer cannot invent
#: one: the delivery adapter renders and posts exactly these.
ACTION_THREAD_OPEN = "thread_open"
ACTION_REPLY = "reply"
ACTION_NOTICE = "notice"
#: The one post per conversation saying whether a supervisor is live (§2.4).
#: It is edited in place, never appended.
ACTION_STATUS_LINE = "status_line"
STATUS_LINE_ACTION = ACTION_STATUS_LINE
CONVERSATION_ACTIONS = frozenset(
    {ACTION_THREAD_OPEN, ACTION_REPLY, ACTION_NOTICE, ACTION_STATUS_LINE}
)


@runtime_checkable
class ConversationOutbox(Protocol):
    """Queue one outbound conversation action; idempotent on *dedup_key*."""

    bound: bool

    async def enqueue(
        self,
        *,
        owner_id: str,
        kind: str,
        dedup_key: str,
        payload: dict,
        priority: int = DEFAULT_PRIORITY,
        due_at: float | None = None,
    ) -> str: ...


class UnboundOutbox:
    """The port before the shared outbox library is bound: refuses every enqueue."""

    bound = False

    async def enqueue(
        self,
        *,
        owner_id: str,
        kind: str,
        dedup_key: str,
        payload: dict,
        priority: int = DEFAULT_PRIORITY,
        due_at: float | None = None,
    ) -> str:
        raise RuntimeError("outbox unbound")


class RecordingOutbox:
    """In-memory outbox for tests.

    ``rows`` holds one dict per distinct dedup key, in enqueue order, with
    ids ``out-1``, ``out-2``...  Re-enqueueing a key returns the first row's
    id and records nothing, mirroring the shared outbox's unique dedup key.
    """

    bound = True

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def enqueue(
        self,
        *,
        owner_id: str,
        kind: str,
        dedup_key: str,
        payload: dict,
        priority: int = DEFAULT_PRIORITY,
        due_at: float | None = None,
    ) -> str:
        for row in self.rows:
            if row["dedup_key"] == dedup_key:
                return row["id"]
        row = {
            "id": f"out-{len(self.rows) + 1}",
            "owner_id": owner_id,
            "kind": kind,
            "dedup_key": dedup_key,
            "payload": dict(payload),
            "priority": priority,
            "due_at": due_at,
        }
        self.rows.append(row)
        return row["id"]

    def by_key(self, dedup_key: str) -> dict[str, Any] | None:
        """The recorded row for *dedup_key*, or ``None``."""
        return next((row for row in self.rows if row["dedup_key"] == dedup_key), None)
