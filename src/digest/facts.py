"""What the digest is allowed to consider evidence.

A :class:`WorkFact` is one durable, already-recorded thing that happened to a
task: a completion, an attempt that started, an explicit progress note, a PR
milestone.  Every fact carries a stable ``key`` derived from the row that
produced it, which is what makes a window idempotent -- a fact seen in an
earlier window is dropped rather than reported twice, and a row that arrives
late (inserted after its window closed) is still reported exactly once when a
later window's lookback picks it up.

Deliberately *not* facts: heartbeats, ``updated_at`` churn, layout rebuilds,
periodic metrics samples and repeated notifications.  Elapsed time is not
progress; §8 is explicit that the digest must never invent it.

:class:`ActiveTask` is the other half: a task with a live, non-stale assigned
attempt right now.  "Active" is a statement about a current attempt, not about
a status column -- an IN_PROGRESS container with no live execution, a stalled
session and a worker waiting on a human answer are all *not* active.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Selectable digest categories.  ``work`` is task lifecycle and progress,
#: ``vcs`` is PR/branch milestones, ``budget`` is spend, ``system`` is daemon
#: health.  The last two have no in-tree producer yet; they exist so the
#: configured category filter has a stable vocabulary to validate against.
CATEGORIES: tuple[str, ...] = ("work", "vcs", "budget", "system")

#: Fact kinds, in the order a highlight list prefers them: a completion says
#: more than a start, and a start says more than a note.
KIND_COMPLETED = "completed"
KIND_PROGRESS = "progress"
KIND_STARTED = "started"

#: Rank used to keep the most informative highlights when truncating.
_KIND_RANK = {KIND_COMPLETED: 0, KIND_PROGRESS: 1, KIND_STARTED: 2}


@dataclass(frozen=True, slots=True)
class DigestWindow:
    """Half-open UTC window ``[since, until)`` the digest evaluates.

    ``catchup`` marks a window that coalesces several missed evaluations after
    a restart or outage: §8 allows at most one labelled catch-up message
    instead of one per missed hour.
    """

    since: float
    until: float
    catchup: bool = False

    def __post_init__(self) -> None:
        if self.until < self.since:
            raise ValueError("digest window ends before it starts")

    @property
    def hours(self) -> float:
        return (self.until - self.since) / 3600.0


@dataclass(frozen=True, slots=True)
class WorkFact:
    """One recorded thing that happened, with a stable identity."""

    key: str
    kind: str
    category: str
    project_id: str
    task_id: str
    title: str
    at: float
    detail: str = ""

    @property
    def rank(self) -> int:
        return _KIND_RANK.get(self.kind, len(_KIND_RANK))


@dataclass(frozen=True, slots=True)
class ActiveTask:
    """A task with a live, non-stale attempt at evaluation time."""

    task_id: str
    project_id: str
    title: str
    started_at: float


@dataclass(frozen=True, slots=True)
class DigestInputs:
    """Everything :func:`~src.digest.aggregate.build_digest` is allowed to see.

    ``reported_keys`` is the set of fact keys already sent in an earlier
    window; ``reported_highlights`` the normalised highlight texts already
    shown, so identical wording is never reposted as if it were new progress.
    ``open_escalations`` is a count supplied by the caller -- escalations own
    their own delivery path, and an open one never on its own makes a digest
    eligible.
    """

    window: DigestWindow
    facts: tuple[WorkFact, ...] = ()
    active: tuple[ActiveTask, ...] = ()
    open_escalations: int = 0
    #: Tasks in the window's projects that exist but did nothing: unchanged
    #: ready/queued, paused, dependency-waiting, human-waiting, or an
    #: IN_PROGRESS container with no live execution.  Never eligibility --
    #: only the difference between "quiet" and "nothing here at all" in the
    #: preview's suppression reason.
    idle_tasks: int = 0
    reported_keys: frozenset[str] = frozenset()
    reported_highlights: frozenset[str] = frozenset()
    project_names: dict[str, str] = field(default_factory=dict)
