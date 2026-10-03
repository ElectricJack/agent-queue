"""§5.5's auto-resolution rules: close an incident whose source went away.

An escalation exists to ask a human one question.  Most of the time the
question stops mattering without anybody answering it: the gate it was waiting
on resolved through another path, the task it was about finished, or nobody has
touched it in a week.  Spec §5.5 makes those three closures *mechanism*, so the
channel stops being an append-only log of every incident the daemon ever had:

===============================  =============================================
Source kind                     Rule
===============================  =============================================
``gate``                        resolves when the gate resolves; outcome = the
                                gate decision
``supervisor_delivery``         low severity, never the human channel,
                                auto-resolves when the notice delivers or the
                                supervisor session starts (owned by
                                :class:`~src.escalations.supervisor
                                .SupervisorDeliveryWatchdog`, coalesced per
                                ``(project, body_kind)``)
``question`` / ``needs_human``  obsolete when the task reaches a terminal status
with a task
any ``stale``                   obsolete after 7 days with no activity
===============================  =============================================

Every rule here is a pure decision over durable rows -- no clock, no gateway, no
judgment about what a human *meant*.  Which facts to mention stays in a
playbook; this only notices that a question has stopped being a question.  A
rule fires at most once per incident because the closing write is a
compare-and-set on the revision, and an incident it already closed is no longer
in the open set it scans.

``discord.escalations.stateful`` gates the whole pass: with the flag off the
ticks return immediately and every incident keeps whatever state it had, which
is what §7.1 means by "off: today's create-only posts".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.escalations.facts import OPEN_STATES
from src.escalations.state import stale_since_seconds

logger = logging.getLogger(__name__)

#: §5.5's last row: an incident nobody has touched in a week is obsolete.  It is
#: also what retires the §5.2 ``stale`` form: that form is the still-open
#: reminder, and a reminder that has been showing for seven days is the thing
#: §5.5 says to retire.
STALE_OBSOLETE_SECONDS = 7 * 86400.0

#: §5.5's third row read as §5.6 rule 3 states it concretely: a question over a
#: task that reached ``COMPLETED`` needs no answer.  It is deliberately *not*
#: the wider ``TERMINAL_TASK_STATUSES``: ``BLOCKED`` is exactly the state a task
#: sits in *because* the question is unanswered, and ``FAILED`` is the state a
#: recovery escalation is raised against, so retiring on either would close a
#: question that is still live.
AUTO_OBSOLETE_TASK_STATUSES = frozenset({"COMPLETED"})

#: How long to wait before re-reading the same incident.  The rules are cheap
#: and idempotent, so this only bounds how much work one long-open incident can
#: ask of a tick.
SCAN_LIMIT = 200


@dataclass(frozen=True)
class AutoDecision:
    """One rule's verdict on one incident."""

    #: The §5.5 rule that fired; stored in ``escalations.outcome`` so the
    #: collapsed post and the §5.6 sweep can both read why.
    outcome: str
    #: The stored terminal state this rule closes into.  ``cancelled`` is the
    #: stored spelling of §5.2's ``obsolete``.
    new_state: str
    #: The one line a reader of the channel sees after the collapse.
    terminal_outcome: str
    #: The machine-readable facts behind it, recorded with the transition.
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class AutoResolveReport:
    """What one pass did."""

    inspected: int = 0
    closed: int = 0
    skipped: str | None = None
    closed_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RuleContext:
    """Everything a rule may read.  Nothing here is a decision."""

    now: float


def _source_status(source: Any) -> str:
    """A source row's status as a string, whatever shape it arrives in.

    ``get_task`` returns a :class:`~src.models.Task` whose ``status`` is an enum
    and ``get_gate`` returns a dict, so a rule reads one name for both rather
    than two nearly identical accessors that can disagree.
    """
    raw = getattr(source, "status", None)
    if raw is None and isinstance(source, Mapping):
        raw = source.get("status")
    return str(getattr(raw, "value", raw) or "")


def gate_resolved_rule(
    row: Mapping[str, Any], ctx: RuleContext, source: Mapping[str, Any] | None
) -> AutoDecision | None:
    """§5.5 row 1: the gate resolved, so the question has its answer.

    ``gate.resolved.v1`` is what makes the gate row terminal; this reads that
    row rather than subscribing to the event, which keeps the rule idempotent
    across restarts and replay -- an event that arrives twice, or a gate
    resolved by a path that never emits, both close the incident exactly once.
    """
    if str(row.get("source_kind") or "") != "gate" or not source:
        return None
    if str(source.get("status") or "") != "resolved":
        return None
    resolution = str(source.get("resolution") or "").strip()
    if not resolution:
        return None
    return AutoDecision(
        outcome="gate_resolved",
        new_state="resolved",
        terminal_outcome=f"Gate {source.get('id')} resolved: {resolution}",
        evidence={
            "gate_id": str(source.get("id")),
            "gate_type": str(source.get("gate_type") or ""),
            "resolved_by": source.get("resolved_by"),
        },
    )


def task_terminal_rule(
    row: Mapping[str, Any], ctx: RuleContext, source: Mapping[str, Any] | None
) -> AutoDecision | None:
    """§5.5 row 3: the task reached a terminal status, so nobody needs to decide."""
    task_id = row.get("task_id")
    if not task_id or not source:
        return None
    if str(row.get("source_kind") or "") not in {"question", "task_attempt", "task_recovery"}:
        return None
    status = _source_status(source)
    if status not in AUTO_OBSOLETE_TASK_STATUSES:
        return None
    return AutoDecision(
        outcome="task_terminal",
        new_state="cancelled",
        terminal_outcome=f"Task {task_id} reached {status}; no decision is needed.",
        evidence={"task_id": str(task_id), "task_status": status},
    )


def _stale_expired(
    row: Mapping[str, Any], ctx: RuleContext, source: Mapping[str, Any] | None
) -> AutoDecision | None:
    """§5.5 row 4: a week with no activity retires the question.

    Measured from ``updated_at`` -- the last time anybody or anything touched
    the incident -- so a reply, a supervisor turn or a severity change all reset
    the clock.  A gate incident is exempt: a gate that is still open is still
    being waited on, and §5.5 row 1 already owns its closure.

    Deliberately *not* part of the §5.6 sweep's rule table (it is private for
    that reason): a sweep run over the back-fill pile would retire every
    month-old task-less question on this row alone, which is the "mark all
    obsolete" reading of §8 Q5 that Jack rejected in favour of §5.6 step 4's
    supervisor triage.
    """
    if str(row.get("source_kind") or "") == "gate":
        return None
    idle = stale_since_seconds(row.get("updated_at"), now=ctx.now)
    if idle < STALE_OBSOLETE_SECONDS:
        return None
    days = int(idle // 86400)
    return AutoDecision(
        outcome="stale_expired",
        new_state="cancelled",
        terminal_outcome=(
            f"No activity for {days} day{'s' if days != 1 else ''}; retired unanswered."
        ),
        evidence={"idle_seconds": round(idle, 3), "updated_at": row.get("updated_at")},
    )


#: §5.5's rules in the order they are tried.  ``gate_resolved`` is first because
#: a resolved gate is the one closure that carries an *answer*, and an answered
#: incident should never be recorded as merely abandoned.
AUTO_RESOLVE_RULES: tuple[Callable[..., AutoDecision | None], ...] = (
    gate_resolved_rule,
    task_terminal_rule,
    _stale_expired,
)


class EscalationAutoResolver:
    """Tick the §5.5 rules over the open incidents.  Never raises."""

    def __init__(
        self,
        db: Any,
        config: Any,
        *,
        clock: Callable[[], float] = time.time,
        on_close: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None,
        rules: Sequence[Callable[..., AutoDecision | None]] = AUTO_RESOLVE_RULES,
    ) -> None:
        self.db = db
        self.config = config
        self.clock = clock
        self.on_close = on_close
        self.rules = tuple(rules)

    @property
    def _discord(self) -> Any:
        return getattr(self.config, "discord", self.config)

    @property
    def enabled(self) -> bool:
        """§7.1's P1 flag.  Off means no incident is closed by a rule."""
        return bool(getattr(getattr(self._discord, "escalations", None), "stateful", False))

    async def tick(self, *, limit: int = SCAN_LIMIT) -> AutoResolveReport:
        report = AutoResolveReport()
        if not self.enabled:
            report.skipped = "discord.escalations.stateful is off"
            return report
        rows = await self.db.list_escalations(states=tuple(OPEN_STATES), limit=limit)
        report.inspected = len(rows)
        ctx = RuleContext(now=self.clock())
        for row in rows:
            if await self._apply(row, ctx, report):
                report.closed += 1
        return report

    async def _apply(
        self, row: Mapping[str, Any], ctx: RuleContext, report: AutoResolveReport
    ) -> bool:
        """Close ``row`` if a rule fires for it.  Idempotent per incident."""
        decision = await self.decide(row, ctx)
        if decision is None:
            return False
        try:
            closed = await self._close(row, decision, ctx)
        except Exception:
            logger.warning("auto-resolution of %s failed", row.get("id"), exc_info=True)
            return False
        if closed is None:
            # Someone moved the incident underneath the scan (a reply, a
            # supervisor turn).  The revision CAS lost on purpose; the next tick
            # re-reads whatever the truth is now.
            return False
        report.closed_ids = (*report.closed_ids, str(closed["id"]))
        if self.on_close is not None:
            try:
                await self.on_close(closed)
            except Exception:
                logger.debug("auto-resolution status hook failed", exc_info=True)
        return True

    async def decide(
        self, row: Mapping[str, Any], ctx: RuleContext | None = None
    ) -> AutoDecision | None:
        """The first §5.5 rule that fires for this incident, or ``None``.

        Split from the write so the rule table is testable as a table: every
        row of §5.5 is one incident row plus its source, and no database
        mutation is needed to see which rule claims it.
        """
        ctx = ctx or RuleContext(now=self.clock())
        source = await self._source(row)
        for rule in self.rules:
            decision = rule(row, ctx, source)
            if decision is not None:
                return decision
        return None

    async def _source(self, row: Mapping[str, Any]) -> Any | None:
        """The incident's live source: its gate, or its task.  Never both needed."""
        if str(row.get("source_kind") or "") == "gate" and row.get("source_identity"):
            return await self.db.get_gate(str(row["source_identity"]))
        if row.get("task_id"):
            return await self.db.get_task(str(row["task_id"]))
        return None

    async def _close(
        self, row: Mapping[str, Any], decision: AutoDecision, ctx: RuleContext
    ) -> Mapping[str, Any] | None:
        """Apply the decision with a revision compare-and-set.

        Two write paths because §5.5's rows end in two different states: a
        ``resolved`` closure goes through the narrow recovery primitive, which
        is the only path to ``resolved`` that does not require a human reply,
        and an ``obsolete`` one is an ordinary legal transition.
        """
        if decision.new_state == "resolved":
            return await self.db.resolve_escalation_on_recovery(
                str(row["id"]),
                expected_revision=int(row["revision"]),
                source_kind=str(row["source_kind"]),
                terminal_outcome=decision.terminal_outcome,
                terminal_evidence=dict(decision.evidence),
                outcome=decision.outcome,
                now=ctx.now,
            )
        return await self.db.transition_escalation(
            str(row["id"]),
            expected_revision=int(row["revision"]),
            new_state=decision.new_state,
            terminal_outcome=decision.terminal_outcome,
            terminal_evidence=dict(decision.evidence),
            outcome=decision.outcome,
            now=ctx.now,
        )


__all__ = [
    "AUTO_OBSOLETE_TASK_STATUSES",
    "AUTO_RESOLVE_RULES",
    "SCAN_LIMIT",
    "STALE_OBSOLETE_SECONDS",
    "AutoDecision",
    "AutoResolveReport",
    "EscalationAutoResolver",
    "RuleContext",
    "gate_resolved_rule",
    "task_terminal_rule",
]
