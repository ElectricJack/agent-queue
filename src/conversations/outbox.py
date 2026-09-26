"""The narrow outbox port conversation commands enqueue through (mention-routing spec §4.2).

Every Discord side effect of a conversation -- the thread-open ack, a reply,
a bounded notice -- is a row in the shared ``outbound_deliveries`` outbox
owned by the supervisor narrative spec (§4.1), leased and sent by its
escalation-first dispatcher through a typed ``conversation`` adapter.  That
library is a separate plan, so commands depend only on this port:

* :class:`ConversationOutbox` is what ``supervisor_inbox_post``,
  ``supervisor_inbox_reply`` and the maintenance sweep call.  ``enqueue`` is
  idempotent on ``dedup_key`` and returns the outbox row id.
* :class:`UnboundOutbox` is the daemon's binding until the shared library is
  wired: ``bound`` is ``False``, so the preconditions report
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
