"""§5.6: sweep the back-fill pile the channel inherited from create-only posts.

The pile is the price of the pre-phase behaviour: every incident the daemon ever
raised got a root post, and nothing ever edited it, so the human channel is an
append-only log (§1.5's 81 rows over five buckets).  §5.6 is a one-shot,
idempotent pass that closes the ones that stopped being questions:

=========================  ==================================================
§5.6 step                  Rule
=========================  ==================================================
1 resolve resolved gates   :func:`gate_resolved_rule` (§5.5 row 1)
2 obsolete delivery notice :func:`notice_delivered`
3 obsolete a finished task :func:`task_terminal_rule` (§5.5 row 3)
4 triage the task-less     :func:`project_inactive`, :func:`retired_source`,
                           :func:`triage`
=========================  ==================================================

Steps 1 and 3 *are* §5.5 rows 1 and 3, so the sweep calls those rule functions
rather than restating them: a dry run and the periodic
:class:`~src.escalations.autoresolve.EscalationAutoResolver` then cannot
disagree about the same incident.  §5.5's fourth row (idle for seven days) is
deliberately absent -- see :func:`~src.escalations.autoresolve._stale_expired`.

Step 4's triage is the gate Jack answered in §8 Q5: the task-less questions go to
the supervisor's inbox to be triaged, and only the two *provable* cases are
marked obsolete here.  A missing source is never assumed: a source kind this
module cannot resolve simply goes to triage, because the reading of "this
question may still matter" that costs a human a line of text is cheaper than
the one that closes a question somebody still wanted answered.

Two things this module deliberately does **not** do:

* **It does not edit Discord.**  §5.6 step 5 (collapse every affected post at the
  rate guard's pace) is already mechanism: ``src/escalations/plan.py`` plans a
  ``KIND_RESOLUTION`` in-place edit for every terminal incident that has a root
  post, and ``dispatch.py`` records ``collapsed_at`` when one lands.  Closing the
  rows is what the sweep owes; the pump owns the pacing, the retry backoff and
  the thread archive.
* **It does not judge triage.**  Which of the remaining questions still matters
  is the supervisor's, in its own inbox, with ``aq escalation resolve``.

The audit trail is §5.6's own: every row the sweep acts on gets one
``escalation_messages`` row with ``direction=system`` and ``text="sweep: <rule>"``,
written through the idempotent append, so a replay of the sweep adds nothing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.escalations.autoresolve import (
    AutoDecision,
    RuleContext,
    gate_resolved_rule,
    task_terminal_rule,
)
from src.escalations.facts import OPEN_STATES
from src.profiles.parser import parse_profile

logger = logging.getLogger(__name__)

#: How many open incidents one pass reads.  The pile §1.5 counted was 81 and
#: ``escalation_list`` caps at 500, so one pass covers an installation's whole
#: backlog; the reported counts are exact (see
#: :meth:`~src.database.queries.escalation_queries.EscalationQueriesMixin.count_escalations`)
#: and never bounded by this.
SCAN_LIMIT = 500

#: §5.6 step 5's promise as a number an operator surface can judge: "the channel
#: ends with <=10 open items and 70 one-line closed ones".
TARGET_OPEN_ITEMS = 10

#: What the sweep may do to one incident.  ``triage`` is the only read-only
#: action: it neither closes nor edits, it just earns a place in the
#: supervisor's inbox (and the ``sweep: triage`` audit row that makes the
#: listing idempotent).
ACTION_RESOLVE = "resolve"
ACTION_OBSOLETE = "obsolete"
ACTION_TRIAGE = "triage"

#: A sweep audit row is the daemon speaking to itself, not to the channel:
#: ``transport`` names the mechanism, ``verified_actor`` the writer, and
#: ``direction=system`` is what keeps ``src/escalations/plan.py`` from relaying
#: the note into the human thread.
AUDIT_TRANSPORT = "sweep"
AUDIT_ACTOR = "sweep"
AUDIT_DIRECTION = "system"

#: Bounded body for the triage inbox message.  A 40-row pile fits; a pathological
#: one is summarised rather than cut mid-sentence, because every id is in the
#: audit rows too.
TRIAGE_BODY_CHARS = 4000
TRIAGE_LISTED_PER_MESSAGE = 25


@dataclass(frozen=True)
class SweepItem:
    """One incident's planned fate.  A dry run prints these; ``--apply`` does them."""

    escalation_id: str
    project_id: str
    rule: str
    action: str
    outcome: str
    revision: int
    new_state: str | None = None
    terminal_outcome: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    source_kind: str = ""
    supervisor_owner: str = ""
    summary: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "escalation_id": self.escalation_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "source_kind": self.source_kind,
            "rule": self.rule,
            "action": self.action,
            "outcome": self.outcome,
            "new_state": self.new_state,
            "terminal_outcome": self.terminal_outcome,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class SweepFacts:
    """Everything one row's rules may read, resolved once per incident.

    Assembled by :meth:`EscalationSweeper._facts` so the rule table stays pure:
    every rule is ``(row, facts, ctx) -> SweepItem | None`` and the whole table
    can be driven without a database.
    """

    #: The incident's live gate row, or ``None`` when it has no gate source.
    gate: Mapping[str, Any] | None = None
    #: Whether the incident's source record still exists.  Tri-state on purpose:
    #: ``False`` is a *proven* absence (the row this module looked up is gone),
    #: ``None`` means "this module cannot see that source table at all", which is
    #: never the same thing.
    source_found: bool | None = None
    #: The active or archived task's status, or ``""`` when there is no task. The
    #: ``escalations.task_status`` snapshot is deliberately not read: it is a
    #: nullable copy of a fact that changes.
    task_status: str = ""
    #: The owning project's ``ProjectStatus`` value.
    project_status: str = ""
    #: Every delivery status the incident has.  Only read for the one source
    #: kind whose rule needs it.
    delivery_statuses: tuple[str, ...] = ()
    #: An audit row already records that this incident was listed for triage.
    #: That is what makes step 4's listing idempotent, since triage changes no
    #: column on the incident itself.
    triaged: bool = False
    review: Mapping[str, Any] | None = None
    gate_waiters: tuple[str, ...] = ()
    archived_task: Mapping[str, Any] | None = None
    worker_template_grants: tuple[str, ...] = ()


@dataclass(frozen=True)
class SweepPlan:
    """The whole pile, read once.  Applying it writes; printing it does not."""

    items: tuple[SweepItem, ...] = ()
    inspected: int = 0
    open_before: int = 0
    #: Open rows no rule claimed, with the reason, so a dry run can answer "why
    #: are 24 still open?" instead of silently omitting them.
    untouched: tuple[Mapping[str, Any], ...] = ()
    skipped: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        """How many items each rule claims."""
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.rule] = counts.get(item.rule, 0) + 1
        return counts

    @property
    def closable(self) -> tuple[SweepItem, ...]:
        return tuple(item for item in self.items if item.action != ACTION_TRIAGE)

    @property
    def triage(self) -> tuple[SweepItem, ...]:
        return tuple(item for item in self.items if item.action == ACTION_TRIAGE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inspected": self.inspected,
            "open_before": self.open_before,
            "counts": self.counts,
            "closable": len(self.closable),
            "triage": len(self.triage),
            "untouched": [dict(row) for row in self.untouched],
            "items": [item.to_dict() for item in self.items],
            "skipped": self.skipped,
        }


@dataclass
class SweepReport:
    """What one applied plan actually did."""

    inspected: int = 0
    planned: int = 0
    closed: int = 0
    #: Rows the plan wanted closed that are already in exactly that state and
    #: outcome: a replayed or concurrent apply, not a race somebody won.
    already: int = 0
    #: Closures whose revision compare-and-set lost to something else: somebody
    #: answered or moved the incident between the plan and the write.  Never a
    #: failure -- the next run re-reads the truth.
    conflicts: int = 0
    #: Audit rows actually written (``sweep: <rule>``).
    audited: int = 0
    triaged: int = 0
    triage_messages: tuple[str, ...] = ()
    closed_ids: tuple[str, ...] = ()
    open_after: int = 0
    failures: tuple[str, ...] = ()
    skipped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "inspected": self.inspected,
            "planned": self.planned,
            "closed": self.closed,
            "already": self.already,
            "conflicts": self.conflicts,
            "audited": self.audited,
            "triaged": self.triaged,
            "triage_messages": list(self.triage_messages),
            "closed_ids": list(self.closed_ids),
            "open_after": self.open_after,
            "failures": list(self.failures),
            "skipped": self.skipped,
        }


def _item(
    row: Mapping[str, Any],
    *,
    rule: str,
    action: str,
    outcome: str,
    new_state: str | None = None,
    terminal_outcome: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    reason: str = "",
) -> SweepItem:
    return SweepItem(
        escalation_id=str(row["id"]),
        project_id=str(row["project_id"]),
        task_id=row.get("task_id"),
        source_kind=str(row.get("source_kind") or ""),
        supervisor_owner=str(row.get("supervisor_owner") or ""),
        summary=str(row.get("summary") or ""),
        rule=rule,
        action=action,
        outcome=outcome,
        revision=int(row["revision"]),
        new_state=new_state,
        terminal_outcome=terminal_outcome,
        evidence=dict(evidence or {}),
        reason=reason,
    )


def _decision_item(row: Mapping[str, Any], decision: AutoDecision, *, reason: str) -> SweepItem:
    """Project a §5.5 rule's verdict into a sweep item."""
    return _item(
        row,
        rule=decision.outcome,
        action=ACTION_RESOLVE if decision.new_state == "resolved" else ACTION_OBSOLETE,
        outcome=decision.outcome,
        new_state=decision.new_state,
        terminal_outcome=decision.terminal_outcome,
        evidence=decision.evidence,
        reason=reason,
    )


def gate_resolved(
    row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
) -> SweepItem | None:
    """§5.6 step 1: a gate that already resolved needs no question."""
    decision = gate_resolved_rule(row, ctx, facts.gate)
    if decision is None:
        return None
    return _decision_item(row, decision, reason="its gate already resolved")


def task_terminal(
    row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
) -> SweepItem | None:
    """§5.6 step 3: the task the question was about has finished."""
    source = {"status": facts.task_status} if facts.task_status else None
    decision = task_terminal_rule(row, ctx, source)
    if decision is None:
        return None
    return _decision_item(row, decision, reason="its task finished")


def withdrawn_review(
    row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
) -> SweepItem | None:
    """Retire an unanswered notice, preserving the withdrawn review's open gate."""
    gate, review = facts.gate, facts.review
    if (
        row.get("state") != "needs_human"
        or row.get("source_kind") != "gate"
        or not gate or not review or facts.gate_waiters
        or gate.get("gate_type") != "review" or gate.get("status") != "open"
        or review.get("state") != "withdrawn"
        or gate.get("id") != row.get("source_identity")
        or review.get("id") != gate.get("await_id")
        or review.get("gate_id") != gate.get("id")
        or review.get("project_id") != row.get("project_id")
        or gate.get("project_id") != row.get("project_id")
    ):
        return None
    return _item(
        row, rule="withdrawn_review", action=ACTION_OBSOLETE, outcome="sweep",
        new_state="cancelled",
        terminal_outcome="Review withdrawn with no gate waiters; no decision remains pending.",
        evidence={"review_id": review["id"], "revision": review["current_revision"],
                  "review_state": "withdrawn", "gate_id": gate["id"],
                  "gate_status_preserved": "open", "gate_waiters": []},
        reason="its review was withdrawn and no task waits on its preserved gate",
    )


def archived_task(
    row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
) -> SweepItem | None:
    """An archived supervisor task no longer owns an executable recovery request."""
    task = facts.archived_task
    if (
        row.get("state") != "needs_human"
        or row.get("source_kind") not in {"supervisor", "supervisor_decision"}
        or not task or task.get("id") != row.get("task_id")
        or task.get("project_id") != row.get("project_id")
        or task.get("status") not in {"COMPLETED", "FAILED", "BLOCKED"}
        or not task.get("archived_at")
    ):
        return None
    return _item(
        row, rule="archived_task", action=ACTION_OBSOLETE, outcome="sweep",
        new_state="cancelled",
        terminal_outcome=(f"Source task {task['id']} archived ({task['status']}); "
                          "its historical supervisor request is retired, not marked delivered."),
        evidence={"task_id": task["id"], "archived_status": task["status"],
                  "archived_at": task["archived_at"]},
        reason="its source task was archived and has no active task row",
    )


def worker_template_grants(
    row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
) -> SweepItem | None:
    """Read back the installed grants for the legacy missing-knowledge-grants incident."""
    if (
        row.get("state") != "needs_human" or row.get("task_id")
        or row.get("source_kind") != "supervisor"
        or row.get("source_identity") != "supervisor:worker-templates-knowledge-grants"
        or facts.worker_template_grants != ("worker-claude", "worker-codex")
    ):
        return None
    return _item(
        row, rule="worker_template_grants", action=ACTION_OBSOLETE, outcome="sweep",
        new_state="cancelled", terminal_outcome="Both installed worker templates have knowledge grants.",
        evidence={"profiles": list(facts.worker_template_grants),
                  "required_commands": ["knowledge_show", "knowledge_context_deliver"]},
        reason="the installed worker templates now provide the missing knowledge commands",
    )


def notice_delivered(
    row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
) -> SweepItem | None:
    """§5.6 step 2: an operational delivery notice is never a human question.

    The watchdog's ``supervisor_delivery`` incidents (§5.5 row 2) exist because
    the daemon could not reach its own supervisor.  Once the backlog behind one
    has drained -- or once it has simply been overtaken by a supervisor that has
    since started -- nothing is left for a human to decide.  ``outcome`` records
    which of the two it was, so the audit trail never claims a delivery that did
    not happen.
    """
    if str(row.get("source_kind") or "") != "supervisor_delivery":
        return None
    delivered = "sent" in facts.delivery_statuses
    pending = sum(
        1
        for status in facts.delivery_statuses
        if status in {"pending", "sending", "retry", "unknown"}
    )
    if delivered:
        outcome = "notice_delivered"
        terminal = "Supervisor notices delivered; nothing left to decide."
        reason = "the delivery backlog behind this notice has drained"
    else:
        outcome = "sweep"
        terminal = "Operational delivery notice retired; no human decision was owed."
        reason = "an operational notice that never reached a decision point"
    return _item(
        row,
        rule="supervisor_delivery",
        action=ACTION_OBSOLETE,
        outcome=outcome,
        new_state="cancelled",
        terminal_outcome=terminal,
        evidence={
            "delivery_statuses": list(facts.delivery_statuses),
            "pending_deliveries": pending,
        },
        reason=reason,
    )


def project_inactive(
    row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
) -> SweepItem | None:
    """§5.6 step 4: a question with no task in a project nobody is running.

    Provable rather than inferred: the project's own ``status`` is the only
    thing this reads, and it is the same value the scheduler reads to decide
    whether to keep starting work there.
    """
    if row.get("task_id") or not facts.project_status or facts.project_status.upper() == "ACTIVE":
        return None
    return _item(
        row,
        rule="project_inactive",
        action=ACTION_OBSOLETE,
        outcome="sweep",
        new_state="cancelled",
        terminal_outcome="No longer needed: its project is not active.",
        evidence={"project_status": facts.project_status},
        reason=f"its project is {facts.project_status}",
    )


def retired_source(
    row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
) -> SweepItem | None:
    """§5.6 step 4: a question whose own source record is gone.

    Only asserted where the absence was actually observed --
    :attr:`SweepFacts.source_found` is ``False``, meaning the gate or
    ``agent_questions`` row this module looked up is not there.  A kind it cannot
    resolve is never called retired.
    """
    if row.get("task_id") or facts.source_found is not False:
        return None
    kind = str(row.get("source_kind") or "")
    what = "the question it asked" if kind == "question" else "the gate it was waiting on"
    return _item(
        row,
        rule="retired_source",
        action=ACTION_OBSOLETE,
        outcome="sweep",
        new_state="cancelled",
        terminal_outcome=f"No longer needed: {what} is gone.",
        evidence={"source_kind": kind, "source_identity": str(row.get("source_identity") or "")},
        reason="its source record no longer exists",
    )


def triage(
    row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
) -> SweepItem | None:
    """§5.6 step 4: the task-less questions the sweep will not judge.

    Whatever survives the two provable cases above and has no task to finish is
    listed for the supervisor instead of being closed.  A gate incident is
    excluded on purpose: a live gate is not an old question but a decision
    somebody is still owed, and §5.5 row 1 owns it.
    """
    if row.get("task_id") or str(row.get("source_kind") or "") == "gate" or facts.triaged:
        return None
    return _item(
        row,
        rule="triage",
        action=ACTION_TRIAGE,
        outcome="sweep",
        evidence={"source_kind": str(row.get("source_kind") or "")},
        reason="an old question with no live source, listed for the supervisor",
    )


#: §5.6's rules in the order they are tried.  §5.5 rows 1 and 3 lead because they
#: are the two closures that carry an answer rather than an absence; the two
#: task-less rules come next, and triage last because it is the only rule that
#: closes nothing.
SWEEP_RULES: tuple[Callable[..., SweepItem | None], ...] = (
    gate_resolved,
    withdrawn_review,
    task_terminal,
    archived_task,
    worker_template_grants,
    notice_delivered,
    project_inactive,
    retired_source,
    triage,
)


class EscalationSweeper:
    """Plan and apply the §5.6 sweep.  Never raises out of a pass."""

    def __init__(
        self,
        db: Any,
        config: Any,
        *,
        clock: Callable[[], float] = time.time,
        rules: Sequence[Callable[..., SweepItem | None]] = SWEEP_RULES,
        on_close: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.clock = clock
        self.rules = tuple(rules)
        self.on_close = on_close

    @property
    def _discord(self) -> Any:
        return getattr(self.config, "discord", self.config)

    @property
    def enabled(self) -> bool:
        """§7.1's P1 flag.  Off means the pile is left exactly as it is."""
        return bool(getattr(getattr(self._discord, "escalations", None), "stateful", False))

    async def plan(self, *, project_id: str | None = None, limit: int = SCAN_LIMIT) -> SweepPlan:
        """Read the pile and say what would be done.  Writes nothing."""
        if not self.enabled:
            # Still count: an operator asking how big the pile is gets an
            # answer even when the sweep is rolled back, and a plan reporting
            # zero open items would be a lie rather than a refusal.
            return SweepPlan(
                open_before=await self.db.count_escalations(
                    project_id=project_id, states=tuple(OPEN_STATES)
                ),
                skipped="discord.escalations.stateful is off",
            )
        rows = await self.db.list_escalations(
            project_id=project_id, states=tuple(OPEN_STATES), limit=limit
        )
        ctx = RuleContext(now=self.clock())
        items: list[SweepItem] = []
        untouched: list[Mapping[str, Any]] = []
        for row in rows:
            facts = await self._facts(row)
            item = self._decide(row, facts, ctx)
            if item is None:
                untouched.append(
                    {
                        "escalation_id": str(row["id"]),
                        "source_kind": str(row.get("source_kind") or ""),
                        "task_id": row.get("task_id"),
                        "reason": self._untouched_reason(row),
                    }
                )
                continue
            items.append(item)
        return SweepPlan(
            items=tuple(items),
            inspected=len(rows),
            open_before=await self.db.count_escalations(
                project_id=project_id, states=tuple(OPEN_STATES)
            ),
            untouched=tuple(untouched),
        )

    def _decide(
        self, row: Mapping[str, Any], facts: SweepFacts, ctx: RuleContext
    ) -> SweepItem | None:
        for rule in self.rules:
            item = rule(row, facts, ctx)
            if item is not None:
                return item
        return None

    @staticmethod
    def _untouched_reason(row: Mapping[str, Any]) -> str:
        """Why no rule claimed a still-open incident.

        Almost always a gate: §5.6 step 1 resolves gates, and a gate that is
        still pending is a live question rather than a leftover.  The sweep says
        so out loud instead of omitting the row, because "why are 24 still
        open?" is the question an operator runs this to answer.
        """
        if str(row.get("source_kind") or "") == "gate":
            return "a gate that is still open"
        if row.get("task_id"):
            return "its task has not finished"
        return "no rule claims this source kind"

    async def _facts(self, row: Mapping[str, Any]) -> SweepFacts:
        """Resolve everything one row's rules may read, once.

        Each lookup is lazy: a row is charged only for the source its rules
        actually read, so an 80-row pile does not become four queries a row.
        """
        kind = str(row.get("source_kind") or "")
        identity = str(row.get("source_identity") or "")
        gate: Mapping[str, Any] | None = None
        review: Mapping[str, Any] | None = None
        gate_waiters: tuple[str, ...] = ()
        source_found: bool | None = None
        if kind == "gate" and identity:
            gate = await self.db.get_gate(identity)
            source_found = gate is not None
            if gate and gate.get("gate_type") == "review" and gate.get("await_id"):
                review = await self.db.get_review(str(gate["await_id"]))
                gate_waiters = tuple(sorted(await self.db.get_gate_waiters(identity)))
        elif kind == "question" and identity and not row.get("task_id"):
            # Only the task-less shape needs the source looked up: with a task,
            # the row's own source is proven alive by construction and
            # ``create_escalation`` copies the question's task onto it.
            source_found = await self.db.get_agent_question(identity) is not None
        task_status = ""
        archived = None
        if row.get("task_id"):
            task = await self.db.get_task(str(row["task_id"]))
            task_status = str(getattr(getattr(task, "status", None), "value", "") or "")
            if task is None:
                archived = await self.db.get_archived_task(str(row["task_id"]))
                if archived and archived.get("project_id") == row.get("project_id"):
                    task_status = str(archived.get("status") or "")
                else:
                    archived = None
        verified_grants: list[str] = []
        if kind == "supervisor" and identity == "supervisor:worker-templates-knowledge-grants":
            vault = getattr(self.config, "vault_root", None)
            if not vault and getattr(self.config, "data_dir", None):
                vault = Path(self.config.data_dir) / "vault"
            if vault:
                for profile_id in ("worker-claude", "worker-codex"):
                    try:
                        parsed = parse_profile(
                            (Path(vault) / "agent-types" / profile_id / "profile.md").read_text()
                        )
                    except (OSError, ValueError):
                        continue
                    commands = (parsed.capabilities or {}).get("aq_commands", [])
                    tools = (parsed.capabilities or {}).get("harness_tools", [])
                    if (
                        not parsed.errors and parsed.frontmatter.id == profile_id and tools
                        and {"knowledge_show", "knowledge_context_deliver"}.issubset(commands)
                    ):
                        verified_grants.append(profile_id)
        project = await self.db.get_project(str(row["project_id"]))
        deliveries: tuple[str, ...] = ()
        if kind == "supervisor_delivery":
            deliveries = tuple(
                str(row_delivery["status"])
                for row_delivery in await self.db.list_escalation_deliveries(str(row["id"]))
            )
        triaged = False
        if not row.get("task_id") and kind != "gate":
            # Only a triage candidate needs its history read, and this is what
            # makes step 4's listing idempotent across repeat sweeps.
            triaged = any(
                str(message.get("transport")) == AUDIT_TRANSPORT
                and str(message.get("direction")) == AUDIT_DIRECTION
                for message in await self.db.list_escalation_messages(str(row["id"]))
            )
        return SweepFacts(
            gate=gate,
            source_found=source_found,
            task_status=task_status,
            project_status=str(getattr(getattr(project, "status", None), "value", "") or ""),
            delivery_statuses=deliveries,
            triaged=triaged,
            review=review,
            gate_waiters=gate_waiters,
            archived_task=archived,
            worker_template_grants=tuple(verified_grants),
        )

    async def apply(self, plan: SweepPlan, *, project_id: str | None = None) -> SweepReport:
        """Do what ``plan`` says, with a revision compare-and-set per row.

        Idempotent by construction rather than by a run marker: a closed
        incident is no longer in the open set a plan is built from, and an
        incident already listed for triage carries its ``sweep: triage`` audit
        row.  Re-applying an applied plan therefore writes nothing.
        """
        report = SweepReport(
            inspected=plan.inspected, planned=len(plan.items), skipped=plan.skipped
        )
        if plan.skipped is not None:
            return report
        now = self.clock()
        triage_items: list[SweepItem] = []
        for item in plan.items:
            try:
                if item.action == ACTION_TRIAGE:
                    if await self._audit(item, now=now):
                        report.triaged += 1
                        report.audited += 1
                        triage_items.append(item)
                    continue
                closed, audited = await self._close(item, now=now)
                report.audited += int(audited)
                if closed:
                    report.closed += 1
                    report.closed_ids = (*report.closed_ids, item.escalation_id)
                elif await self._already_closed(item):
                    report.already += 1
                else:
                    report.conflicts += 1
            except Exception:
                # One uncooperative row must not cost the pass the other eighty.
                logger.warning("escalation sweep of %s failed", item.escalation_id, exc_info=True)
                report.failures = (*report.failures, item.escalation_id)
        report.triage_messages = await self._notify_triage(triage_items)
        report.open_after = await self.db.count_escalations(
            project_id=project_id, states=tuple(OPEN_STATES)
        )
        return report

    async def run(
        self,
        *,
        project_id: str | None = None,
        apply_changes: bool = False,
        limit: int = SCAN_LIMIT,
    ) -> tuple[SweepPlan, SweepReport]:
        """Plan, and apply only when asked.  The dry run is the default."""
        plan = await self.plan(project_id=project_id, limit=limit)
        report = SweepReport(inspected=plan.inspected, skipped=plan.skipped)
        if apply_changes and plan.skipped is None:
            report = await self.apply(plan, project_id=project_id)
        return plan, report

    async def _close(self, item: SweepItem, *, now: float) -> tuple[bool, bool]:
        """Close one incident; report ``(closed, audit row written)``.

        Two write paths, as in §5.5: ``resolved`` goes through the narrow
        recovery primitive, which is the only route to ``resolved`` that does not
        require a human reply, and ``cancelled`` is an ordinary legal
        transition.  Both compare-and-set on the revision the plan read, so a
        reply that landed mid-sweep is never overwritten -- and a lost race is
        not audited either, because the sweep did not close it.
        """
        if item.rule in {
            "withdrawn_review", "task_terminal", "archived_task", "worker_template_grants",
        }:
            current = await self.db.get_escalation(item.escalation_id)
            if current is None or int(current["revision"]) != item.revision:
                return False, False
            fresh = self._decide(current, await self._facts(current), RuleContext(now=now))
            if fresh is None or fresh.rule != item.rule or fresh.evidence != item.evidence:
                return False, False
        if item.new_state == "resolved":
            closed = await self.db.resolve_escalation_on_recovery(
                item.escalation_id,
                expected_revision=item.revision,
                source_kind=item.source_kind,
                terminal_outcome=item.terminal_outcome or "",
                terminal_evidence=dict(item.evidence),
                outcome=item.outcome,
                now=now,
            )
        else:
            closed = await self.db.transition_escalation(
                item.escalation_id,
                expected_revision=item.revision,
                new_state=item.new_state or "cancelled",
                terminal_outcome=item.terminal_outcome or "",
                terminal_evidence=dict(item.evidence),
                outcome=item.outcome,
                now=now,
            )
        if closed is None:
            return False, False
        audited = await self._audit(item, now=now, revision=int(closed["revision"]))
        if self.on_close is not None:
            try:
                await self.on_close(closed)
            except Exception:
                logger.debug("sweep close hook failed", exc_info=True)
        return True, audited

    async def _already_closed(self, item: SweepItem) -> bool:
        """Is this row already in the state and outcome the plan wanted?

        Distinguishes a replayed apply from a genuine race.  Both lose the
        revision compare-and-set, but only one of them means the pile is fine --
        and an operator reading "66 conflicts" on a re-run would be misled about
        a pile that is already swept.
        """
        current = await self.db.get_escalation(item.escalation_id)
        if current is None:
            return False
        return current["state"] == item.new_state and current.get("outcome") == item.outcome

    async def _audit(
        self, item: SweepItem, *, now: float, revision: int | None = None
    ) -> bool:
        """Write ``sweep: <rule>`` into the incident's history, idempotently.

        The dedup is ``(transport, external_message_id)``, so the same rule on the
        same incident collapses to one row however often the sweep runs.  The
        revision the closure landed on is appended in the free text, because
        there is no column for it and the audit row is the only place it is read.
        """
        text = f"sweep: {item.rule}"
        if revision is not None:
            text = f"{text} (revision {revision})"
        _, created = await self.db.append_escalation_message(
            item.escalation_id,
            direction=AUDIT_DIRECTION,
            transport=AUDIT_TRANSPORT,
            verified_actor=AUDIT_ACTOR,
            text=text,
            external_message_id=f"sweep:{item.rule}:{item.escalation_id}",
            received_at=now,
        )
        return created

    async def _notify_triage(self, items: Sequence[SweepItem]) -> tuple[str, ...]:
        """Send each project's remaining questions to its own supervisor's inbox.

        One message per supervisor, addressed by the incident's own
        ``supervisor_owner`` rather than by a name this module invents.  The list
        is bounded per message and the remainder summarised, because a triage
        list nobody reads is the pile this sweep exists to clear.
        """
        by_owner: dict[str, list[SweepItem]] = {}
        for item in items:
            by_owner.setdefault(item.supervisor_owner, []).append(item)
        sent: list[str] = []
        for owner, pending in by_owner.items():
            if not owner:
                logger.warning("escalation sweep has %s triage item(s) with no owner", len(pending))
                continue
            lines = [
                "The §5.6 escalation sweep did not judge these; they need triage.",
                (
                    f"{len(pending)} open escalation(s) with no live source in "
                    f"{pending[0].project_id}:"
                ),
                "",
            ]
            for item in pending[:TRIAGE_LISTED_PER_MESSAGE]:
                lines.append(f"- {item.escalation_id} ({item.source_kind}): {item.summary}")
            if len(pending) > TRIAGE_LISTED_PER_MESSAGE:
                lines.append(f"- ... and {len(pending) - TRIAGE_LISTED_PER_MESSAGE} more")
            lines.extend(
                (
                    "",
                    (
                        'Close each with `aq escalation resolve <id> --outcome "..."`, '
                        "or leave it open and say so in the next digest."
                    ),
                )
            )
            message = await self.db.create_message(
                project_id=pending[0].project_id,
                from_kind="system",
                from_id="escalation-sweep",
                to_kind="session",
                to_id=owner,
                subject=f"Escalation sweep: {len(pending)} old question(s) to triage",
                body="\n".join(lines)[:TRIAGE_BODY_CHARS],
                body_kind="escalation_sweep",
                priority=60,
                archive_after_inject=True,
            )
            sent.append(str(message.id))
        return tuple(sent)


__all__ = [
    "ACTION_OBSOLETE",
    "ACTION_RESOLVE",
    "ACTION_TRIAGE",
    "AUDIT_ACTOR",
    "AUDIT_DIRECTION",
    "AUDIT_TRANSPORT",
    "SCAN_LIMIT",
    "SWEEP_RULES",
    "TARGET_OPEN_ITEMS",
    "TRIAGE_BODY_CHARS",
    "TRIAGE_LISTED_PER_MESSAGE",
    "EscalationSweeper",
    "SweepFacts",
    "SweepItem",
    "SweepPlan",
    "SweepReport",
    "gate_resolved",
    "notice_delivered",
    "project_inactive",
    "retired_source",
    "task_terminal",
    "triage",
]
