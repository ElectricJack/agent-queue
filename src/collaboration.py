"""Fixed limits and pointer bodies for bounded task collaboration."""

from __future__ import annotations

import uuid

MIN_MEMBERS = 2
MAX_MEMBERS = 4
MIN_DEADLINE_SECONDS = 60
DEFAULT_DEADLINE_SECONDS = 7200
MAX_DEADLINE_SECONDS = 7200
MAX_MESSAGES = 40
MAX_BODY_BYTES = 4096
MAX_SENDS_PER_MINUTE = 5
RATE_WINDOW_SECONDS = 60
MAX_READ_MESSAGES = 20
MAX_READ_BYTES = 32768
PARTNER_GRACE_SECONDS = 120
CONTENT_RETENTION_SECONDS = 30 * 86400
TOMBSTONE_RETENTION_SECONDS = 90 * 86400
MAX_GOAL_CHARS = 1000
THREAD_PREFIX = "collab-"
WAIT_REASONS = ("thread_closed", "peer_failed", "peer_gone", "partner_not_running")
CLOSE_REASONS = ("closed", "budget_exhausted", "expired", "members_below_two")


class CollaborationError(ValueError):
    """Typed refusal, with a retry delay for sender rate limits."""

    def __init__(self, code: str, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.code = code if code.startswith("collaboration.") else f"collaboration.{code}"
        self.retry_after = retry_after


def new_thread_id() -> str:
    return THREAD_PREFIX + uuid.uuid4().hex[:16]


def is_collaboration_thread(thread_id: str | None) -> bool:
    return bool(thread_id and thread_id.startswith(THREAD_PREFIX))


def render_invite(thread: dict, members: list) -> str:
    names = ", ".join(
        member["task_id"] if isinstance(member, dict) else member for member in members
    )
    goal = " ".join((thread.get("goal") or "Collaboration").split())
    return (
        f"Invited to {thread['id']} with {names}: {goal}. "
        f"Run `aq collaboration accept {thread['id']}` to join; "
        f"read `aq collaboration show {thread['id']}`."
    )


def render_closed(thread: dict) -> str:
    return (
        f"Collaboration {thread['id']} ended: {thread.get('close_reason') or 'closed'}. "
        f"Read the final result with `aq collaboration show {thread['id']}`."
    )
