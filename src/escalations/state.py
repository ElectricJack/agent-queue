"""The §5.2 state machine, as a pure function of one incident.

Spec §5.2 wants each escalation to be **one** channel post that is edited in
place as the incident moves, so the channel stops being an append-only log of
every incident the daemon ever had::

    open ──reply/button──▶ answered ──supervisor ack──▶ resolved
      ├──source gone (task terminal, gate resolved,
      │  notice delivered)────────────────────────▶ obsolete
      └──stale timer───────────────────────────▶ stale ──▶ obsolete after 7d

This module is the whole of that decision, with no database, clock or Discord
object involved, so every row of §5.2's state table is a table test rather than
a channel observation.

Two vocabularies meet here, and keeping them apart is the point:

* the **stored** ``escalations.state`` -- ``needs_human``, ``reply_received``,
  ``resolving``, ``resolved``, ``cancelled``, ``stale``.  It is a core record
  with its own history, its own immutability rule and its own API, and this
  phase does not widen it;
* the **display** state above -- what the one post currently says.  It is a
  rendering concern: several stored states share one form, and the stale timer
  is a *presentation* of an open incident rather than a stored state.

The mappings are therefore explicit and total rather than string coincidence,
and :func:`display_state` is the only thing that decides a form.

:func:`is_collapsed` marks the two forms Discord cannot show as an open
question any more (resolved, obsolete).  §5.2 keeps those posts forever as one
lines; ``escalations.collapsed_at`` records when a post reached one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: §5.2's five display states.
STATE_OPEN = "open"
STATE_ANSWERED = "answered"
STATE_RESOLVED = "resolved"
STATE_OBSOLETE = "obsolete"
STATE_STALE = "stale"

DISPLAY_STATES: tuple[str, ...] = (
    STATE_OPEN,
    STATE_ANSWERED,
    STATE_RESOLVED,
    STATE_OBSOLETE,
    STATE_STALE,
)

#: §5.2's terminal rows.  ``stale`` is deliberately absent: the stale form is
#: §5.2's "still open since" reminder for an incident that still wants a
#: human, so it keeps its thread open.  What *does* collapse a post is an
#: incident that no longer needs one.
COLLAPSED_STATES = frozenset({STATE_RESOLVED, STATE_OBSOLETE})

#: Stored states, keyed to the display form an incident in them shows.  A
#: stored state that is absent from this table is a bug, not a fallback: the
#: query layer's own vocabulary is imported so the two cannot drift silently.
DISPLAY_FOR_STATE: Mapping[str, str] = {
    "needs_human": STATE_OPEN,
    # A verified human reply is §5.2's ``answered``, and the supervisor picking
    # the incident up (``resolving``) is still the same form: it is the same
    # sentence, and §5.2 has no third one for "acting on it".
    "reply_received": STATE_ANSWERED,
    "resolving": STATE_ANSWERED,
    "resolved": STATE_RESOLVED,
    # ``cancelled`` is the stored spelling of "this question is no longer being
    # asked".  §5.2 calls that form obsolete.
    "cancelled": STATE_OBSOLETE,
    # A stored ``stale`` incident was closed by a stale sweep long before this
    # phase; it already collapsed, so it reads as obsolete rather than as
    # §5.2's still-open reminder.
    "stale": STATE_OBSOLETE,
}

#: Delivery kind for the in-place root edit that carries a display state.
KIND_STATE = "state"


def display_state(state: str, *, stale: bool = False) -> str:
    """The §5.2 form an incident in stored ``state`` shows.

    ``stale`` is §5.2's stale timer having fired: the incident is still open
    and still wants a human, it has just been sitting long enough that the post
    should say so.  It applies to ``needs_human`` only -- an ``answered``
    incident is not idle, it is being acted on -- and never to a terminal one.
    """
    try:
        form = DISPLAY_FOR_STATE[state]
    except KeyError:
        raise ValueError(f"no display state for escalation state {state!r}") from None
    if stale and form == STATE_OPEN:
        return STATE_STALE
    return form


def incident_display_state(row: Mapping[str, Any], *, stale: bool = False) -> str:
    """:func:`display_state` for an ``escalations`` row."""
    return display_state(str(row.get("state") or ""), stale=stale)


def is_collapsed(display: str) -> bool:
    """Does this form stop asking for a human (§5.2's collapsed rows)?"""
    if display not in DISPLAY_STATES:
        raise ValueError(f"unknown display state {display!r}; known: {list(DISPLAY_STATES)}")
    return display in COLLAPSED_STATES


def state_dedup_key(escalation_id: str, generation: int, display: str) -> str:
    """The key that makes an edit-in-place exactly once per distinct form.

    Keyed on the *form*, not the revision: two revisions that render the same
    sentence are one edit, and a replay of either re-derives the same key so the
    unique ``dedup_key`` turns it into a no-op.  That is what makes
    edit-in-place idempotent (§5.1: "Every state change edits that post;
    nothing appends a second root").
    """
    return f"{escalation_id}:{KIND_STATE}:{generation}:{display}"


def is_stale_due(
    state: str,
    updated_at: float | None,
    *,
    now: float,
    reminder_minutes: int,
) -> bool:
    """Has this incident sat untouched long enough to say so (§5.2's ``stale``)?

    ``reminder_minutes`` is the existing timer knob: 0 disables it, so a box
    that never asked for a reminder never gets the form either.  ``updated_at``
    is the last thing anyone did to the incident, which is what "no activity"
    means here, and the timer applies to ``needs_human`` only — an ``answered``
    incident is not idle, it is being acted on.
    """
    if reminder_minutes <= 0:
        return False
    if state != "needs_human" or updated_at is None:
        return False
    try:
        elapsed = now - float(updated_at)
    except (TypeError, ValueError):
        return False
    return elapsed >= reminder_minutes * 60.0


def stale_since_seconds(updated_at: float | None, *, now: float) -> float:
    """How long the incident has gone without activity (0 when unknown)."""
    if updated_at is None:
        return 0.0
    try:
        return max(0.0, now - float(updated_at))
    except (TypeError, ValueError):
        return 0.0


def thread_archived(display: str) -> bool:
    """§5.2's thread column: archived once and only once the post is collapsed."""
    return is_collapsed(display)


__all__ = [
    "COLLAPSED_STATES",
    "DISPLAY_FOR_STATE",
    "DISPLAY_STATES",
    "KIND_STATE",
    "STATE_ANSWERED",
    "STATE_OBSOLETE",
    "STATE_OPEN",
    "STATE_RESOLVED",
    "STATE_STALE",
    "display_state",
    "incident_display_state",
    "is_collapsed",
    "is_stale_due",
    "stale_since_seconds",
    "state_dedup_key",
    "thread_archived",
]