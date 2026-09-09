"""Transport-neutral facts the escalation renderer and planner work from.

Everything here is a plain value: one incident as the delivery layer needs to
see it, the configured mention policy, and the transport identity a previous
send established.  Nothing in this module reads the database, a clock or a
Discord object, which is what lets §7's rules ("one root and one thread per
incident", "only configured mentions", "no replacement for a resolved
incident") be tested as a table rather than against a live gateway.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Delivery kinds.  ``root`` owns the channel post *and* its thread: they are
#: one logical operation, so a partial success is retried as one unit rather
#: than leaving an orphan post with no thread behind it.
KIND_ROOT = "root"
KIND_ACK = "ack"
KIND_RELAY = "relay"
KIND_RESOLUTION = "resolution"

DELIVERY_KINDS: tuple[str, ...] = (KIND_ROOT, KIND_ACK, KIND_RELAY, KIND_RESOLUTION)

#: Lower number wins in :meth:`claim_escalation_deliveries`' ordering.  A root
#: post goes out before its own follow-ups, and every escalation delivery
#: outranks a digest (§7: "escalations take priority over digests").  The
#: digest worker uses its own table, so this is a documented contract for it
#: rather than a shared queue position.
PRIORITY_ROOT = 0
PRIORITY_RESOLUTION = 2
PRIORITY_FOLLOWUP = 5
PRIORITY_DIGEST = 20

#: States in which the incident still wants a human in the channel.
OPEN_STATES = frozenset({"needs_human", "reply_received", "resolving"})
#: States in which nothing may be re-opened; a late reply is answered, not acted on.
TERMINAL_STATES = frozenset({"resolved", "cancelled", "stale"})

_SEVERITY_LABEL = {
    "critical": "🚨 Critical",
    "high": "🔴 High",
    "medium": "🟠 Medium",
    "low": "🟡 Low",
}


@dataclass(frozen=True)
class EscalationFacts:
    """The subset of an ``escalations`` row the transport is allowed to see."""

    id: str
    project_id: str
    state: str
    revision: int
    severity: str
    summary: str
    investigation: str
    decision_requested: str
    task_id: str | None = None
    task_title: str | None = None
    task_status: str | None = None
    choices: Sequence[str] = field(default_factory=tuple)
    terminal_outcome: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def severity_label(self) -> str:
        return _SEVERITY_LABEL.get(self.severity, self.severity)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> EscalationFacts:
        raw_choices = row.get("choices") or ()
        if isinstance(raw_choices, Mapping):  # tolerate {"a": "..."} shaped choices
            choices = tuple(str(value) for value in raw_choices.values())
        elif isinstance(raw_choices, str):
            choices = (raw_choices,)
        else:
            choices = tuple(str(value) for value in raw_choices)
        return cls(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            state=str(row["state"]),
            revision=int(row.get("revision") or 0),
            severity=str(row.get("severity") or "medium"),
            summary=str(row.get("summary") or ""),
            investigation=str(row.get("investigation") or ""),
            decision_requested=str(row.get("decision_requested") or ""),
            task_id=(str(row["task_id"]) if row.get("task_id") else None),
            task_title=(str(row["task_title"]) if row.get("task_title") else None),
            task_status=(str(row["task_status"]) if row.get("task_status") else None),
            choices=choices,
            terminal_outcome=(
                str(row["terminal_outcome"]) if row.get("terminal_outcome") else None
            ),
        )


@dataclass(frozen=True)
class MentionPolicy:
    """The only mentions this feature may ever emit.

    They come from configuration and are attached to the *initial* root post
    only.  No text that a human, a worker or a supervisor authored can add
    one: :func:`src.escalations.render.sanitise` strips mention syntax from
    every interpolated string.
    """

    user_ids: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, escalation_config: Any) -> MentionPolicy:
        return cls(
            user_ids=tuple(str(v) for v in getattr(escalation_config, "mention_user_ids", ())),
            role_ids=tuple(str(v) for v in getattr(escalation_config, "mention_role_ids", ())),
        )

    def render(self) -> str:
        parts = [f"<@{uid}>" for uid in self.user_ids]
        parts += [f"<@&{rid}>" for rid in self.role_ids]
        return " ".join(parts)


@dataclass(frozen=True)
class TransportBinding:
    """Where an incident already lives in the channel, if anywhere.

    Recovered from the durable delivery rows rather than from any in-memory
    map or task-thread heuristic, so a daemon restart rebinds to exactly the
    post and thread it created before (§7).
    """

    channel_id: str | None = None
    root_message_id: str | None = None
    thread_id: str | None = None
    generation: int = 0

    @property
    def has_root(self) -> bool:
        return bool(self.root_message_id)

    @property
    def has_thread(self) -> bool:
        return bool(self.thread_id)
