"""The §8 eligibility table, as a pure function.

One rule decides whether an hourly window speaks at all::

    completed / started / meaningful progress in a selected category  -> send
    at least one live current attempt                                 -> send
    anything else                                                     -> silent

Everything else in this module exists to make that rule honest: visibility is
applied *before* the decision (a fact the destination may not see cannot make
it eligible), facts already reported in an earlier window are dropped (a
re-evaluated or replayed window says nothing new), and per-task duplicates are
collapsed so one completion recorded twice is one highlight.

The suppression reason is part of the contract, not a debug string: the
dashboard's dry preview shows it when it explains why nothing would be sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.digest.facts import (
    KIND_COMPLETED,
    ActiveTask,
    DigestInputs,
    WorkFact,
)

#: Why a window stayed silent.  Stable identifiers -- the dashboard renders
#: them and tests assert on them.
NO_ACTIVITY = "no_activity"
IDLE_ONLY = "idle_only"
ALL_FILTERED = "all_filtered"
ALREADY_REPORTED = "already_reported"

_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Comparison form for "have we already said this?"."""
    return _WHITESPACE.sub(" ", text.strip().casefold())


def highlight_key(fact: WorkFact) -> str:
    """Identity of a highlight's *wording*, scoped to its task.

    A worker that records the same note in two windows produces two distinct
    rows -- distinct fact keys -- saying the same thing.  §8 forbids reposting
    that as new progress, so the rendered wording is remembered per task as
    well as the row identity.  Two different tasks may legitimately report the
    same sentence, which is why the task id is part of the key.
    """
    return f"{fact.task_id}\x1f{_normalise(fact.detail or fact.title)}"


@dataclass(frozen=True, slots=True)
class Eligibility:
    """Verdict for one window."""

    send: bool
    reason: str
    facts: tuple[WorkFact, ...]
    active: tuple[ActiveTask, ...]
    #: Facts dropped because an earlier window already reported them.  Kept
    #: for the preview's explanation; never rendered.
    duplicates: tuple[WorkFact, ...] = ()

    @property
    def completed_count(self) -> int:
        return sum(1 for fact in self.facts if fact.kind == KIND_COMPLETED)

    @property
    def active_count(self) -> int:
        return len(self.active)


def _visible(
    facts: tuple[WorkFact, ...],
    *,
    project_ids: frozenset[str] | None,
    categories: frozenset[str] | None,
) -> tuple[WorkFact, ...]:
    return tuple(
        fact
        for fact in facts
        if (project_ids is None or fact.project_id in project_ids)
        and (categories is None or fact.category in categories)
    )


def _dedupe(facts: tuple[WorkFact, ...]) -> tuple[WorkFact, ...]:
    """Collapse facts that describe the same thing about the same task.

    Two shapes show up in practice: a task closed twice (a reopen and a second
    close, or a lifecycle notification recorded alongside the completion
    record) leaves two completion rows, and a worker that repeats a note
    verbatim leaves two identical progress rows.  Both are one highlight.
    """
    seen_keys: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    kept: list[WorkFact] = []
    for fact in sorted(facts, key=lambda f: (-f.at, f.key)):
        if fact.key in seen_keys:
            continue
        seen_keys.add(fact.key)
        identity = (fact.task_id, fact.kind, _normalise(fact.detail or fact.title))
        if identity in seen:
            continue
        seen.add(identity)
        kept.append(fact)
    kept.sort(key=lambda f: (f.rank, -f.at, f.key))
    return tuple(kept)


def evaluate_eligibility(
    inputs: DigestInputs,
    *,
    project_ids: frozenset[str] | None = None,
    categories: frozenset[str] | None = None,
) -> Eligibility:
    """Decide whether ``inputs`` produces a message, and with what content.

    ``project_ids`` / ``categories`` are the configured destination's
    visibility (``None`` means "everything this destination may see").  They
    are applied here as well as in the query so a caller that assembled inputs
    by hand -- the preview does -- cannot leak another project's work.
    """
    visible = _visible(inputs.facts, project_ids=project_ids, categories=categories)
    def already_said(fact: WorkFact) -> bool:
        return (
            fact.key in inputs.reported_keys
            or highlight_key(fact) in inputs.reported_highlights
        )

    fresh = _dedupe(tuple(fact for fact in visible if not already_said(fact)))
    duplicates = tuple(fact for fact in visible if already_said(fact))

    active = tuple(
        task
        for task in inputs.active
        if project_ids is None or task.project_id in project_ids
    )
    # Ordered for a stable "active count" line and stable tests.
    active = tuple(sorted(active, key=lambda t: (t.started_at, t.task_id)))

    if fresh or active:
        return Eligibility(
            send=True,
            reason="activity" if fresh else "active_execution",
            facts=fresh,
            active=active,
            duplicates=duplicates,
        )

    if duplicates:
        reason = ALREADY_REPORTED
    elif inputs.facts:
        # Something happened, but none of it belongs to this destination.
        reason = ALL_FILTERED
    elif inputs.active or inputs.open_escalations or inputs.idle_tasks:
        # Idle or live-but-invisible work, or nothing but open escalations:
        # an open escalation is delivered on its own path and never speaks
        # here, and waiting is not progress.
        reason = IDLE_ONLY
    else:
        reason = NO_ACTIVITY
    return Eligibility(
        send=False, reason=reason, facts=(), active=(), duplicates=duplicates
    )
