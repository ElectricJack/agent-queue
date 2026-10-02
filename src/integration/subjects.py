"""Integration subjects and the typed ports of the level-triggered reconciler.

Revision 2 of the integration-train review (``rev-agile-ridge``, §3) replaces
edge-triggered playbook rules with one reconciler that visits *subjects*: a
root batch, a parent episode or a source.  A subject has one durable row
(``integration_subjects``), one owner (its ``engine``), one due time and one
pinned policy artifact.  Every visit observes the subject, asks the pinned
policy for exactly one decision, performs that one primitive and schedules the
next visit::

    facts    = observe_subject(subject)               # primitive 1, read-only
    decision = policy(subject.policy, facts)           # the decision table
    outcome  = ports.invoke(subject, decision.request) # one fenced primitive
    schedule = SubjectSchedule.<progress|backoff|wait|hold|close>(...)

This module is mechanism and has no I/O.  It is the stable contract the
observer, the reconciler loop, the policy compiler and the primitive adapters
share, so each of them can be built against it independently:

* :class:`Subject` with its exact head/generation identity
  (:class:`HeadIdentity`), :class:`WriterLease`, :class:`WriterBudget`,
  pinned :class:`PolicyArtifactPin` and :class:`SubjectSchedule`;
* :class:`SubjectFacts`, the complete fact set primitive 1 returns, and its
  :meth:`~SubjectFacts.binding` (the ``s`` a decision table reads);
* the twenty primitives of §3.4 (:class:`Primitive`), their typed arguments
  (:data:`PrimitiveArgs`) and closed outcome sets (:data:`PRIMITIVE_OUTCOMES`);
* :class:`Decision`, :class:`PrimitiveOutcome` and the :class:`PrimitivePorts`
  registry that turns a missing or misbehaving adapter into ``unknown(reason)``.

Waiting and held are not phases of their own: a subject keeps the phase it
will resume (a ``testing`` batch waiting for CI is still ``testing``) and its
:class:`SubjectSchedule` says whether it is progressing, waiting with a bound or
held by a gate (:class:`SubjectState`).  The database enforces the same rule as
``ck_integration_subjects_never_blocked``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.integration.models import ArtifactSnapshot, Fence

SHA_PATTERN = r"^[0-9a-f]{40}$"
ARTIFACT_PATTERN = r"^sha256:[0-9a-f]{64}$"

#: The shipped bound on any wait (§3.6 "the shipped default could be one
#: hour").  A subject pins its own ``max_wait_seconds`` when it is created.
DEFAULT_MAX_WAIT_SECONDS = 3600

#: The outcome every primitive may answer in place of the states it expects
#: (§3.1 principle 4, §5.4).  It always carries a reason; policy maps it to a
#: bounded wait plus, at most, a message.
UNKNOWN = "unknown"


class SubjectKind(StrEnum):
    ROOT_BATCH = "root_batch"
    PARENT_EPISODE = "parent_episode"
    SOURCE = "source"


class SubjectPhase(StrEnum):
    """Where a subject is in its walk (§3.2); shared by every kind."""

    ADMITTING = "admitting"
    BUILDING = "building"
    TESTING = "testing"
    REPAIRING = "repairing"
    PROMOTABLE = "promotable"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    CLEANING = "cleaning"
    DONE = "done"


class SubjectState(StrEnum):
    """The three states a visit may leave a live subject in (§3.6), plus done."""

    PROGRESSING = "progressing"
    WAITING = "waiting"
    HELD = "held"
    DONE = "done"


class SubjectEngine(StrEnum):
    """Which engine owns a subject's mutations; exactly one at a time."""

    LEGACY = "legacy"
    RECONCILER = "reconciler"


class WriterStatus(StrEnum):
    """What the observer can prove about a subject's writer (§3.2)."""

    NONE = "none"
    FILED = "filed"  # task exists, unclaimed
    CLAIMED = "claimed"  # session alive, no push yet
    WORKING = "working"  # pushed at least once
    STOPPED = "stopped"  # proof of stop
    UNKNOWN = "unknown"


class WriterRole(StrEnum):
    """One filing path for every kind of writer (primitive 11)."""

    REPAIR = "repair"
    VERIFIER = "verifier"
    SOURCE_REPAIR = "source_repair"


class CIState(StrEnum):
    """Trusted exact-head CI classification (primitive 9)."""

    NONE = "none"
    PENDING = "pending"
    GREEN = "green"
    RED = "red"
    INFRA = "infra"
    UNTRUSTED = "untrusted"


class ReceiptKind(StrEnum):
    CODE = "code"
    NOOP = "noop"
    SKIPPED = "skipped"


class JournalKind(StrEnum):
    DECISION = "decision"
    ACTION = "action"
    ATTEMPT = "attempt"
    RECEIPT = "receipt"


class JournalMode(StrEnum):
    """``shadow`` records a decision and performs nothing; ``active`` acts."""

    SHADOW = "shadow"
    ACTIVE = "active"


class Primitive(StrEnum):
    """The twenty mechanism primitives of §3.4, in table order."""

    OBSERVE_SUBJECT = "integration_observe_subject"
    SEAL = "integration_seal"
    GIT_MATERIALIZE_REF = "git_materialize_ref"
    GIT_MERGE_MEMBERS = "git_merge_members"
    GIT_PRESERVE = "git_preserve"
    GIT_PUBLISH = "git_publish"
    GIT_ANCESTRY = "git_ancestry"
    CI_REQUEST = "ci_request"
    CI_OBSERVE = "ci_observe"
    CI_ATTEST = "ci_attest"
    WRITER_FILE = "writer_file"
    WRITER_LEASE = "writer_lease"
    WRITER_STOP_PROOF = "writer_stop_proof"
    RECORD_RECEIPT = "record_receipt"
    RECORD_ATTEMPT = "record_attempt"
    RECORD_DECISION = "record_decision"
    WAIT = "wait"
    GATE = "gate"
    EJECT = "eject"
    CLEANUP = "cleanup"

    @property
    def mutates(self) -> bool:
        """Whether the primitive changes anything beyond audit and due bookkeeping.

        Shadow mode may only invoke primitives for which this is false.
        """
        return self not in _NON_MUTATING_PRIMITIVES


_NON_MUTATING_PRIMITIVES = frozenset(
    {
        Primitive.OBSERVE_SUBJECT,
        Primitive.GIT_ANCESTRY,
        Primitive.RECORD_DECISION,
        Primitive.WAIT,
    }
)

#: Closed outcome sets, verbatim from §3.4.  Every primitive may also answer
#: :data:`UNKNOWN`; a policy naming any other outcome fails compilation.
PRIMITIVE_OUTCOMES: Mapping[Primitive, frozenset[str]] = {
    Primitive.OBSERVE_SUBJECT: frozenset({"observed", "not_found"}),
    Primitive.SEAL: frozenset({"sealed", "empty", "busy"}),
    Primitive.GIT_MATERIALIZE_REF: frozenset({"created", "exists_exact", "exists_other"}),
    Primitive.GIT_MERGE_MEMBERS: frozenset({"merged", "conflict", "source_moved", "base_moved"}),
    Primitive.GIT_PRESERVE: frozenset({"preserved", "exists"}),
    Primitive.GIT_PUBLISH: frozenset({"published", "target_moved", "unknown_after_push"}),
    Primitive.GIT_ANCESTRY: frozenset({"facts"}),
    Primitive.CI_REQUEST: frozenset({"requested", "already_running", "unavailable"}),
    Primitive.CI_OBSERVE: frozenset({"green", "red", "pending", "infra", "none", "untrusted"}),
    Primitive.CI_ATTEST: frozenset({"attested", "already", "refused"}),
    Primitive.WRITER_FILE: frozenset({"filed", "exists", "configuration_blocked"}),
    Primitive.WRITER_LEASE: frozenset({"leased", "busy", "stale"}),
    Primitive.WRITER_STOP_PROOF: frozenset(
        {"released", "preserved_and_released", "live", "unknown"}
    ),
    Primitive.RECORD_RECEIPT: frozenset({"recorded", "exists"}),
    Primitive.RECORD_ATTEMPT: frozenset({"counted", "not_an_attempt", "stale"}),
    Primitive.RECORD_DECISION: frozenset({"recorded"}),
    Primitive.WAIT: frozenset({"waiting"}),
    Primitive.GATE: frozenset({"created", "reused", "answered"}),
    Primitive.EJECT: frozenset({"ejected", "not_a_member"}),
    Primitive.CLEANUP: frozenset({"clean", "pending", "irreversible_marker"}),
}

#: Detail keys a parameterised outcome must carry (``merged(head)``,
#: ``conflict(member, files)``, ``busy(holder)``, ``answered(choice)`` ...).
OUTCOME_DETAILS: Mapping[tuple[Primitive, str], frozenset[str]] = {
    (Primitive.GIT_MERGE_MEMBERS, "merged"): frozenset({"head"}),
    (Primitive.GIT_MERGE_MEMBERS, "conflict"): frozenset({"member", "files"}),
    (Primitive.GIT_MERGE_MEMBERS, "source_moved"): frozenset({"member"}),
    (Primitive.WRITER_LEASE, "busy"): frozenset({"holder"}),
    (Primitive.GATE, "answered"): frozenset({"choice"}),
}

#: The existing command contracts each phase-1 adapter wraps "as they are"
#: (§6 phase 1).  The ports add no command surface of their own: these
#: commands, their arguments and their outcomes stay the public contract until
#: phase 4 consolidates them.
PHASE1_ADAPTED_COMMANDS: Mapping[Primitive, tuple[str, ...]] = {
    Primitive.SEAL: ("integration_seal",),
    Primitive.GIT_MERGE_MEMBERS: ("integration_build_candidate",),
    Primitive.CI_OBSERVE: ("integration_ci_evidence",),
    Primitive.GIT_PUBLISH: ("integration_promote_main",),
    Primitive.WRITER_FILE: ("integration_repair_start", "integration_repair_dispatch"),
    Primitive.EJECT: ("integration_eject",),
    Primitive.CLEANUP: ("integration_cleanup",),
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------- identity


class HeadIdentity(_Frozen):
    """The exact head a subject's phase refers to, with its generation.

    ``generation`` is the domain generation of the head: a source's review
    generation, a parent's collection generation, a root batch's candidate
    revision.  Two observations name the same head only if every field agrees.
    """

    repository_id: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    sha: str = Field(pattern=SHA_PATTERN)
    generation: int = Field(ge=0)
    base_sha: str | None = Field(default=None, pattern=SHA_PATTERN)


class PolicyArtifactPin(_Frozen):
    """The compiled policy playbook a subject runs under for its whole life."""

    playbook_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=ARTIFACT_PATTERN)

    @classmethod
    def from_snapshot(cls, snapshot: ArtifactSnapshot) -> PolicyArtifactPin:
        return cls(playbook_id=snapshot.playbook_id, artifact_sha256=snapshot.artifact_sha256)


class WriterLease(_Frozen):
    """The writer a subject's target is leased to, and what it has done."""

    status: WriterStatus = WriterStatus.NONE
    task_id: str | None = Field(default=None, min_length=1)
    fence_token: int | None = Field(default=None, ge=0)
    session_id: str | None = None
    claimed_at: float | None = None
    last_push_at: float | None = None
    stop_proof: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _writer_shape(self) -> WriterLease:
        if self.status is WriterStatus.NONE:
            if self.task_id is not None or self.fence_token is not None or self.session_id:
                raise ValueError("a subject without a writer carries no task, fence or session")
        elif self.task_id is None:
            raise ValueError(f"writer status {self.status.value} requires a task id")
        return self


class WriterBudget(_Frozen):
    """The current ordinal's budget: its clock and its counted attempts (§3.2).

    An attempt is one conclusive, trusted, exact-head CI observation of a head
    the writer published; queue time is not progress and never an attempt.
    """

    ordinal: int = Field(ge=0)
    intelligence_class: str = Field(min_length=1)
    started_at: float
    deadline_at: float
    attempts: int = Field(default=0, ge=0)
    attempt_limit: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _clock(self) -> WriterBudget:
        if self.deadline_at < self.started_at:
            raise ValueError("a budget deadline cannot precede its start")
        return self

    def expired(self, now: float) -> bool:
        return now >= self.deadline_at

    @property
    def exhausted(self) -> bool:
        return self.attempt_limit is not None and self.attempts >= self.attempt_limit


# --------------------------------------------------------------------- schedule


class SubjectSchedule(_Frozen):
    """When a subject is next visited and why; never unbounded (§3.3, §3.6).

    Built through the constructors, which are the only legal outcomes of a
    visit: :meth:`progress`, :meth:`backoff`, :meth:`wait`, :meth:`hold` and
    :meth:`close`.  The validator mirrors ``ck_integration_subjects_never_blocked``
    so an illegal schedule fails here, naming the rule, before it reaches SQL.
    """

    next_due_at: float | None
    due_set_at: float
    max_wait_seconds: int = Field(gt=0)
    wait_reason: str | None = Field(default=None, min_length=1)
    gate_id: str | None = Field(default=None, min_length=1)
    refusal_streak: int = Field(default=0, ge=0)
    closed_reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _never_blocked(self) -> SubjectSchedule:
        if self.closed_reason is not None:
            if self.next_due_at is not None or self.gate_id or self.wait_reason:
                raise ValueError("a closed subject has no due time, wait or gate")
            return self
        if self.wait_reason is not None and self.next_due_at is None:
            raise ValueError("a wait must carry an until")
        if self.gate_id is None:
            if self.next_due_at is None:
                raise ValueError("a live subject needs a due time or an explicit gate")
            if self.next_due_at > self.due_set_at + self.max_wait_seconds:
                raise ValueError(
                    f"due time {self.next_due_at} exceeds the {self.max_wait_seconds}s "
                    f"bound from {self.due_set_at}"
                )
        return self

    @property
    def state(self) -> SubjectState:
        if self.closed_reason is not None:
            return SubjectState.DONE
        if self.gate_id is not None:
            return SubjectState.HELD
        if self.wait_reason is not None:
            return SubjectState.WAITING
        return SubjectState.PROGRESSING

    @classmethod
    def progress(cls, *, now: float, max_wait_seconds: int) -> SubjectSchedule:
        """An action was taken; visit again at once."""
        return cls(next_due_at=now, due_set_at=now, max_wait_seconds=max_wait_seconds)

    @classmethod
    def backoff(
        cls,
        *,
        now: float,
        max_wait_seconds: int,
        refusal_streak: int,
        base_seconds: float,
        ceiling_seconds: float,
    ) -> SubjectSchedule:
        """A transient refusal (``busy``, ``stale``, ``unknown_after_push``...).

        ``refusal_streak`` counts the refusals before this one; the delay
        doubles with it and is capped by the ceiling and the subject's bound.
        """
        if base_seconds <= 0 or ceiling_seconds <= 0:
            raise ValueError("backoff base and ceiling must be positive")
        delay = min(base_seconds * 2 ** min(refusal_streak, 32), ceiling_seconds, max_wait_seconds)
        return cls(
            next_due_at=now + delay,
            due_set_at=now,
            max_wait_seconds=max_wait_seconds,
            refusal_streak=refusal_streak + 1,
        )

    @classmethod
    def wait(
        cls, *, now: float, until: float, reason: str, max_wait_seconds: int
    ) -> SubjectSchedule:
        """Idle until ``until`` for ``reason``, clamped to the subject's bound.

        A clamped wait is still a wait: the next visit re-decides, and an
        overdue wait is itself a fact the policy must handle.
        """
        return cls(
            next_due_at=max(now, min(until, now + max_wait_seconds)),
            due_set_at=now,
            max_wait_seconds=max_wait_seconds,
            wait_reason=reason,
        )

    @classmethod
    def hold(
        cls,
        *,
        now: float,
        gate_id: str,
        max_wait_seconds: int,
        revisit_at: float | None = None,
    ) -> SubjectSchedule:
        """Held by an explicit gate; ``revisit_at`` is its default timeout, if any."""
        return cls(
            next_due_at=revisit_at,
            due_set_at=now,
            max_wait_seconds=max_wait_seconds,
            gate_id=gate_id,
        )

    @classmethod
    def close(cls, *, now: float, reason: str, max_wait_seconds: int) -> SubjectSchedule:
        return cls(
            next_due_at=None,
            due_set_at=now,
            max_wait_seconds=max_wait_seconds,
            closed_reason=reason,
        )


# ---------------------------------------------------------------------- subject


class Subject(_Frozen):
    """One durable integration subject (one ``integration_subjects`` row)."""

    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    kind: SubjectKind
    subject_key: str = Field(min_length=1)
    engine: SubjectEngine = SubjectEngine.LEGACY
    phase: SubjectPhase
    policy: PolicyArtifactPin
    task_id: str | None = Field(default=None, min_length=1)
    batch_id: str | None = Field(default=None, min_length=1)
    # Episode identity survives a reopened collection's generation increment.
    parent_episode_id: str | None = Field(default=None, min_length=1)
    target_ref: str | None = Field(default=None, min_length=1)
    head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    base_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    generation: int = Field(default=0, ge=0)
    schedule: SubjectSchedule
    writer: WriterLease = WriterLease()
    budget: WriterBudget | None = None
    wake_requested_at: float | None = None
    last_visit_at: float | None = None
    last_journal_seq: int | None = None
    version: int = Field(default=0, ge=0)
    created_at: float
    updated_at: float

    @model_validator(mode="after")
    def _identity(self) -> Subject:
        if self.parent_episode_id is not None and self.kind is not SubjectKind.PARENT_EPISODE:
            raise ValueError("only a parent subject can bind a parent episode")
        if self.kind is SubjectKind.ROOT_BATCH:
            if self.task_id is not None:
                raise ValueError("a root batch subject is not bound to a task")
        else:
            if self.task_id is None:
                raise ValueError(f"a {self.kind.value} subject requires its task id")
            if self.batch_id is not None:
                raise ValueError("only a root batch subject maps onto a batch")
        if self.head_sha is not None and self.target_ref is None:
            raise ValueError("an exact head needs the ref it is the head of")
        done = self.phase is SubjectPhase.DONE
        if done != (self.schedule.state is SubjectState.DONE):
            raise ValueError("a subject is done exactly when its schedule is closed")
        return self

    @property
    def head(self) -> HeadIdentity | None:
        if self.head_sha is None or self.target_ref is None:
            return None
        return HeadIdentity(
            repository_id=self.repository_id,
            ref=self.target_ref,
            sha=self.head_sha,
            generation=self.generation,
            base_sha=self.base_sha,
        )

    @property
    def state(self) -> SubjectState:
        return self.schedule.state

    @property
    def is_live(self) -> bool:
        return self.phase is not SubjectPhase.DONE

    def wait_overdue(self, now: float, *, grace_seconds: float = 0.0) -> bool:
        """A wait past its due time is a fact the policy must handle (§3.6)."""
        due = self.schedule.next_due_at
        return (
            self.schedule.state is SubjectState.WAITING
            and due is not None
            and now > due + grace_seconds
        )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Subject:
        budget = None
        if row.get("budget_ordinal") is not None:
            budget = WriterBudget(
                ordinal=row["budget_ordinal"],
                intelligence_class=row["budget_class"],
                started_at=row["budget_started_at"],
                deadline_at=row["budget_deadline_at"],
                attempts=row.get("budget_attempts") or 0,
                attempt_limit=row.get("budget_attempt_limit"),
            )
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            repository_id=row["repository_id"],
            kind=row["kind"],
            subject_key=row["subject_key"],
            engine=row.get("engine") or SubjectEngine.LEGACY,
            phase=row["phase"],
            policy=PolicyArtifactPin(
                playbook_id=row["policy_playbook_id"],
                artifact_sha256=row["policy_artifact_sha256"],
            ),
            task_id=row.get("task_id"),
            batch_id=row.get("batch_id"),
            parent_episode_id=row.get("parent_episode_id"),
            target_ref=row.get("target_ref"),
            head_sha=row.get("head_sha"),
            base_sha=row.get("base_sha"),
            generation=row.get("generation") or 0,
            schedule=SubjectSchedule(
                next_due_at=row.get("next_due_at"),
                due_set_at=row["due_set_at"],
                max_wait_seconds=row["max_wait_seconds"],
                wait_reason=row.get("wait_reason"),
                gate_id=row.get("gate_id"),
                refusal_streak=row.get("refusal_streak") or 0,
                closed_reason=row.get("closed_reason"),
            ),
            writer=WriterLease(
                status=row.get("writer_status") or WriterStatus.NONE,
                task_id=row.get("writer_task_id"),
                fence_token=row.get("writer_fence_token"),
                session_id=row.get("writer_session_id"),
                claimed_at=row.get("writer_claimed_at"),
                last_push_at=row.get("writer_last_push_at"),
                stop_proof=row.get("writer_stop_proof"),
            ),
            budget=budget,
            wake_requested_at=row.get("wake_requested_at"),
            last_visit_at=row.get("last_visit_at"),
            last_journal_seq=row.get("last_journal_seq"),
            version=row.get("version") or 0,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_row(self) -> dict[str, Any]:
        """Column values for ``integration_subjects`` (the inverse of :meth:`from_row`)."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "repository_id": self.repository_id,
            "kind": self.kind.value,
            "subject_key": self.subject_key,
            "engine": self.engine.value,
            "phase": self.phase.value,
            "policy_playbook_id": self.policy.playbook_id,
            "policy_artifact_sha256": self.policy.artifact_sha256,
            "task_id": self.task_id,
            "batch_id": self.batch_id,
            "parent_episode_id": self.parent_episode_id,
            "target_ref": self.target_ref,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "generation": self.generation,
            **schedule_values(self.schedule),
            **writer_values(self.writer),
            **budget_values(self.budget),
            "wake_requested_at": self.wake_requested_at,
            "last_visit_at": self.last_visit_at,
            "last_journal_seq": self.last_journal_seq,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def schedule_values(schedule: SubjectSchedule) -> dict[str, Any]:
    """The ``integration_subjects`` columns a :class:`SubjectSchedule` sets."""
    return {
        "next_due_at": schedule.next_due_at,
        "due_set_at": schedule.due_set_at,
        "max_wait_seconds": schedule.max_wait_seconds,
        "wait_reason": schedule.wait_reason,
        "gate_id": schedule.gate_id,
        "refusal_streak": schedule.refusal_streak,
        "closed_reason": schedule.closed_reason,
    }


def writer_values(writer: WriterLease) -> dict[str, Any]:
    return {
        "writer_status": writer.status.value,
        "writer_task_id": writer.task_id,
        "writer_fence_token": writer.fence_token,
        "writer_session_id": writer.session_id,
        "writer_claimed_at": writer.claimed_at,
        "writer_last_push_at": writer.last_push_at,
        "writer_stop_proof": writer.stop_proof,
    }


def budget_values(budget: WriterBudget | None) -> dict[str, Any]:
    if budget is None:
        return {
            "budget_ordinal": None,
            "budget_class": None,
            "budget_started_at": None,
            "budget_deadline_at": None,
            "budget_attempts": 0,
            "budget_attempt_limit": None,
        }
    return {
        "budget_ordinal": budget.ordinal,
        "budget_class": budget.intelligence_class,
        "budget_started_at": budget.started_at,
        "budget_deadline_at": budget.deadline_at,
        "budget_attempts": budget.attempts,
        "budget_attempt_limit": budget.attempt_limit,
    }


def subject_key(kind: SubjectKind, repository_id: str, *parts: str | int) -> str:
    """Canonical natural key: ``<kind>:<repository>:<parts...>``.

    Root batch: ``(request_id,)``; parent episode: ``(parent_task_id,
    collection_generation)``; source: ``(task_id, review_generation)``.
    """
    if not repository_id or not parts or any(part == "" for part in parts):
        raise ValueError("a subject key needs a repository and at least one non-empty part")
    return ":".join([kind.value, repository_id, *(str(part) for part in parts)])


# ------------------------------------------------------------------------ facts


class MemberFacts(_Frozen):
    """One member (root) or child (parent) of a subject, as observed."""

    task_id: str = Field(min_length=1)
    head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    base_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    generation: int = Field(default=0, ge=0)
    review: Literal["approved", "rejected", "pending", "none"] = "none"
    # Relation of the member head to the subject's target: already contained,
    # ahead of it, diverged, or not established.
    ancestry: Literal["contained", "ahead", "diverged", "unknown"] = "unknown"
    ci: CIState = CIState.NONE
    held: bool = False
    ejected: bool = False


class RemoteHead(_Frozen):
    ref: str = Field(min_length=1)
    state: Literal["present", "absent", "unknown"]
    sha: str | None = Field(default=None, pattern=SHA_PATTERN)

    @model_validator(mode="after")
    def _sha_when_present(self) -> RemoteHead:
        if (self.state == "present") != (self.sha is not None):
            raise ValueError("a remote head has a sha exactly when it is present")
        return self


class CIEvidence(_Frozen):
    """Trusted CI evidence for one exact head."""

    head_sha: str = Field(pattern=SHA_PATTERN)
    state: CIState
    evidence_id: str | None = None
    producer: str | None = None
    requested_at: float | None = None
    observed_at: float | None = None
    age_seconds: float | None = Field(default=None, ge=0)


class ConflictFacts(_Frozen):
    member_task_id: str = Field(min_length=1)
    files: tuple[str, ...] = ()


class UnresolvedWrite(_Frozen):
    """A journalled write whose remote outcome is not yet finalised."""

    journal_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    expected_old_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    desired_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    state: str = Field(min_length=1)
    started_at: float | None = None


class GateFacts(_Frozen):
    gate_id: str = Field(min_length=1)
    status: Literal["open", "answered", "expired", "missing"]
    question: str = ""
    choices: tuple[str, ...] = ()
    answer: str | None = None
    default_choice: str | None = None
    timeout_at: float | None = None
    no_default: bool = False


class HoldFacts(_Frozen):
    """A binding human decision the policy may only wait on (§3.7)."""

    kind: Literal["manual_pause", "project_inactive", "review_rejected", "operator_hold"]
    reason: str = ""
    task_id: str | None = None


class SubjectFacts(_Frozen):
    """The complete, read-only fact set for one subject (primitive 1).

    ``unknown`` lists the facts the observer could not establish, each as a
    reason string; a fact it could not read is reported, never guessed.
    """

    subject_id: str = Field(min_length=1)
    subject_version: int = Field(ge=0)
    kind: SubjectKind
    phase: SubjectPhase
    observed_at: float
    head: HeadIdentity | None = None
    candidate: HeadIdentity | None = None
    default_branch_head: str | None = Field(default=None, pattern=SHA_PATTERN)
    remote_heads: tuple[RemoteHead, ...] = ()
    members: tuple[MemberFacts, ...] = ()
    ci: tuple[CIEvidence, ...] = ()
    conflicts: tuple[ConflictFacts, ...] = ()
    writer: WriterLease = WriterLease()
    budget: WriterBudget | None = None
    unresolved_writes: tuple[UnresolvedWrite, ...] = ()
    gate: GateFacts | None = None
    holds: tuple[HoldFacts, ...] = ()
    base_moved: bool = False
    wait_overdue: bool = False
    ladder_exhausted: bool = False
    no_progress: bool = False
    unknown: tuple[str, ...] = ()

    def ci_for(self, sha: str | None) -> CIState:
        """The evidence state for one exact head; ``none`` when there is none."""
        if sha is None:
            return CIState.NONE
        for evidence in self.ci:
            if evidence.head_sha == sha:
                return evidence.state
        return CIState.NONE

    @property
    def tested_head(self) -> HeadIdentity | None:
        """The head CI is judged on: the candidate when there is one."""
        return self.candidate or self.head

    @property
    def ci_state(self) -> CIState:
        tested = self.tested_head
        return self.ci_for(tested.sha if tested else None)

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)

    def binding(self) -> dict[str, Any]:
        """The JSON object a decision table evaluates as ``s``."""
        value = self.model_dump(mode="json")
        value["ci_state"] = self.ci_state.value
        value["conflict_count"] = self.conflict_count
        value["writer_status"] = self.writer.status.value
        value["budget_expired"] = (
            self.budget.expired(self.observed_at) if self.budget is not None else False
        )
        value["budget_exhausted"] = self.budget.exhausted if self.budget is not None else False
        value["held"] = bool(self.holds)
        return value

    def digest(self) -> str:
        """``sha256:`` of the canonical binding; recorded with every decision."""
        canonical = json.dumps(self.binding(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- primitive arguments


class AdmissionPredicate(_Frozen):
    """Which admission facts must hold for a source to be sealed (primitive 2)."""

    require_review: bool = True
    task_kinds: tuple[str, ...] = ()  # empty admits every kind
    require_source_ci: bool = False
    include_authorized: bool = True  # the admission list the observer reads
    max_members: int | None = Field(default=None, gt=0)


class MemberRef(_Frozen):
    task_id: str = Field(min_length=1)
    head_sha: str = Field(pattern=SHA_PATTERN)
    base_sha: str | None = Field(default=None, pattern=SHA_PATTERN)


class AncestryQuery(_Frozen):
    ancestor: str = Field(min_length=1)  # sha or ref
    descendant: str = Field(min_length=1)


class ObserveSubjectArgs(_Frozen):
    primitive: Literal[Primitive.OBSERVE_SUBJECT] = Primitive.OBSERVE_SUBJECT
    include_remote: bool = True


class SealArgs(_Frozen):
    primitive: Literal[Primitive.SEAL] = Primitive.SEAL
    admission: AdmissionPredicate = AdmissionPredicate()


class MaterializeRefArgs(_Frozen):
    primitive: Literal[Primitive.GIT_MATERIALIZE_REF] = Primitive.GIT_MATERIALIZE_REF
    repository_id: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    base_sha: str = Field(pattern=SHA_PATTERN)


class MergeMembersArgs(_Frozen):
    primitive: Literal[Primitive.GIT_MERGE_MEMBERS] = Primitive.GIT_MERGE_MEMBERS
    target_ref: str = Field(min_length=1)
    base_sha: str = Field(pattern=SHA_PATTERN)
    members: tuple[MemberRef, ...] = Field(min_length=1)
    regenerate_generated: bool = True


class PreserveArgs(_Frozen):
    primitive: Literal[Primitive.GIT_PRESERVE] = Primitive.GIT_PRESERVE
    repository_id: str = Field(min_length=1)
    sha: str = Field(pattern=SHA_PATTERN)
    retention_ref: str = Field(min_length=1)


class PublishArgs(_Frozen):
    """Journal first, then a fenced expected-old push, then read back (primitive 6)."""

    primitive: Literal[Primitive.GIT_PUBLISH] = Primitive.GIT_PUBLISH
    fence: Fence
    expected_old_sha: str = Field(pattern=SHA_PATTERN)
    new_sha: str = Field(pattern=SHA_PATTERN)
    # A default-branch publish refuses a head without a counted green attempt.
    require_green: bool = True


class AncestryArgs(_Frozen):
    primitive: Literal[Primitive.GIT_ANCESTRY] = Primitive.GIT_ANCESTRY
    repository_id: str = Field(min_length=1)
    queries: tuple[AncestryQuery, ...] = Field(min_length=1)


class CIRequestArgs(_Frozen):
    primitive: Literal[Primitive.CI_REQUEST] = Primitive.CI_REQUEST
    head: HeadIdentity


class CIObserveArgs(_Frozen):
    primitive: Literal[Primitive.CI_OBSERVE] = Primitive.CI_OBSERVE
    head: HeadIdentity
    required_check_version: str | None = None


class CIAttestArgs(_Frozen):
    primitive: Literal[Primitive.CI_ATTEST] = Primitive.CI_ATTEST
    head: HeadIdentity


class WriterFileArgs(_Frozen):
    """File one writer for (subject, ordinal); idempotent (primitive 11)."""

    primitive: Literal[Primitive.WRITER_FILE] = Primitive.WRITER_FILE
    role: WriterRole
    ordinal: int = Field(ge=0)
    intelligence_class: str = Field(min_length=1)
    budget_seconds: int = Field(gt=0)
    attempt_limit: int | None = Field(default=None, gt=0)
    brief: str = ""
    # Members the writer is given; empty means the whole subject.
    scope_members: tuple[str, ...] = ()


class WriterLeaseArgs(_Frozen):
    primitive: Literal[Primitive.WRITER_LEASE] = Primitive.WRITER_LEASE
    ref: str = Field(min_length=1)
    owner_task_id: str = Field(min_length=1)
    ttl_seconds: int = Field(gt=0)


class WriterStopProofArgs(_Frozen):
    primitive: Literal[Primitive.WRITER_STOP_PROOF] = Primitive.WRITER_STOP_PROOF
    task_id: str = Field(min_length=1)
    fence_token: int | None = Field(default=None, ge=0)
    preserve_ref: str | None = Field(default=None, min_length=1)


class RecordReceiptArgs(_Frozen):
    primitive: Literal[Primitive.RECORD_RECEIPT] = Primitive.RECORD_RECEIPT
    kind: ReceiptKind
    source_task_id: str = Field(min_length=1)
    source_head_sha: str = Field(pattern=SHA_PATTERN)
    target: HeadIdentity


class RecordAttemptArgs(_Frozen):
    primitive: Literal[Primitive.RECORD_ATTEMPT] = Primitive.RECORD_ATTEMPT
    head: HeadIdentity
    ordinal: int = Field(ge=0)
    evidence_id: str = Field(min_length=1)
    conclusion: Literal["green", "red"]


class RecordDecisionArgs(_Frozen):
    primitive: Literal[Primitive.RECORD_DECISION] = Primitive.RECORD_DECISION
    rule: str = Field(min_length=1)
    facts_digest: str = Field(pattern=ARTIFACT_PATTERN)
    decided: Primitive
    mode: JournalMode


class WaitArgs(_Frozen):
    primitive: Literal[Primitive.WAIT] = Primitive.WAIT
    seconds: int = Field(gt=0)
    reason: str = Field(min_length=1)


class GateArgs(_Frozen):
    """One human gate; a default after a timeout, or an explicit ``no_default``."""

    primitive: Literal[Primitive.GATE] = Primitive.GATE
    question: str = Field(min_length=1)
    choices: tuple[str, ...] = Field(min_length=1)
    default_choice: str | None = None
    default_after_seconds: int | None = Field(default=None, gt=0)
    no_default: bool = False

    @model_validator(mode="after")
    def _default_or_explicit_none(self) -> GateArgs:
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("gate choices must be distinct")
        has_default = self.default_choice is not None or self.default_after_seconds is not None
        if self.no_default:
            if has_default:
                raise ValueError("a no-default gate carries no default choice or timeout")
            return self
        if self.default_choice is None or self.default_after_seconds is None:
            raise ValueError("a gate needs a default choice and timeout, or no_default")
        if self.default_choice not in self.choices:
            raise ValueError("the default choice must be one of the gate's choices")
        return self


class EjectArgs(_Frozen):
    primitive: Literal[Primitive.EJECT] = Primitive.EJECT
    member_task_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class CleanupArgs(_Frozen):
    primitive: Literal[Primitive.CLEANUP] = Primitive.CLEANUP
    delete_successful_sources: bool = True
    retain_failed_seconds: int = Field(default=7 * 86400, ge=0)
    max_tries: int = Field(default=5, gt=0)


PrimitiveArgs = Annotated[
    ObserveSubjectArgs
    | SealArgs
    | MaterializeRefArgs
    | MergeMembersArgs
    | PreserveArgs
    | PublishArgs
    | AncestryArgs
    | CIRequestArgs
    | CIObserveArgs
    | CIAttestArgs
    | WriterFileArgs
    | WriterLeaseArgs
    | WriterStopProofArgs
    | RecordReceiptArgs
    | RecordAttemptArgs
    | RecordDecisionArgs
    | WaitArgs
    | GateArgs
    | EjectArgs
    | CleanupArgs,
    Field(discriminator="primitive"),
]

#: The argument model of each primitive, for adapters and the policy compiler.
PRIMITIVE_ARGS: Mapping[Primitive, type[BaseModel]] = {
    Primitive.OBSERVE_SUBJECT: ObserveSubjectArgs,
    Primitive.SEAL: SealArgs,
    Primitive.GIT_MATERIALIZE_REF: MaterializeRefArgs,
    Primitive.GIT_MERGE_MEMBERS: MergeMembersArgs,
    Primitive.GIT_PRESERVE: PreserveArgs,
    Primitive.GIT_PUBLISH: PublishArgs,
    Primitive.GIT_ANCESTRY: AncestryArgs,
    Primitive.CI_REQUEST: CIRequestArgs,
    Primitive.CI_OBSERVE: CIObserveArgs,
    Primitive.CI_ATTEST: CIAttestArgs,
    Primitive.WRITER_FILE: WriterFileArgs,
    Primitive.WRITER_LEASE: WriterLeaseArgs,
    Primitive.WRITER_STOP_PROOF: WriterStopProofArgs,
    Primitive.RECORD_RECEIPT: RecordReceiptArgs,
    Primitive.RECORD_ATTEMPT: RecordAttemptArgs,
    Primitive.RECORD_DECISION: RecordDecisionArgs,
    Primitive.WAIT: WaitArgs,
    Primitive.GATE: GateArgs,
    Primitive.EJECT: EjectArgs,
    Primitive.CLEANUP: CleanupArgs,
}


# --------------------------------------------------------- outcomes and decisions


class PrimitiveOutcome(_Frozen):
    """One primitive's answer: an outcome from its closed set, or ``unknown``."""

    primitive: Primitive
    outcome: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _closed(self) -> PrimitiveOutcome:
        if self.outcome == UNKNOWN:
            if self.reason is None:
                raise ValueError("unknown(reason) requires its reason")
            return self
        allowed = PRIMITIVE_OUTCOMES[self.primitive]
        if self.outcome not in allowed:
            raise ValueError(
                f"{self.primitive.value} has no outcome {self.outcome!r}; "
                f"expected one of {sorted(allowed | {UNKNOWN})}"
            )
        missing = OUTCOME_DETAILS.get((self.primitive, self.outcome), frozenset()) - set(
            self.detail
        )
        if missing:
            raise ValueError(
                f"{self.primitive.value}.{self.outcome} requires detail {sorted(missing)}"
            )
        return self

    @classmethod
    def unknown(cls, primitive: Primitive, reason: str, **detail: Any) -> PrimitiveOutcome:
        return cls(primitive=primitive, outcome=UNKNOWN, reason=reason, detail=detail)

    @property
    def is_unknown(self) -> bool:
        return self.outcome == UNKNOWN


class Decision(_Frozen):
    """What the pinned policy decided for one visit: exactly one primitive call.

    ``rule`` names the decision-table line that matched; ``facts_digest`` binds
    the decision to the observation it was made from.  Waiting and holding are
    decisions too (:class:`WaitArgs`, :class:`GateArgs`), so every visit ends
    in one recorded primitive.
    """

    subject_id: str = Field(min_length=1)
    subject_version: int = Field(ge=0)
    policy: PolicyArtifactPin
    rule: str = Field(min_length=1)
    facts_digest: str = Field(pattern=ARTIFACT_PATTERN)
    request: PrimitiveArgs
    messages: tuple[str, ...] = ()

    @property
    def primitive(self) -> Primitive:
        return self.request.primitive


# --------------------------------------------------------------------------- ports


class PrimitivePort(Protocol):
    """An adapter for one primitive: one fenced, idempotent call."""

    async def __call__(self, subject: Subject, args: Any, /) -> PrimitiveOutcome: ...


class PrimitivePorts:
    """The adapters bound to each primitive, answering closed outcomes only.

    A primitive with no adapter answers ``unknown(primitive_unavailable)``; an
    adapter that answers another primitive's outcome, or one outside the closed
    set, answers ``unknown(contract_violation: ...)``.  Any other exception is
    the caller's to isolate.
    """

    def __init__(self, ports: Mapping[Primitive, PrimitivePort] | None = None) -> None:
        self._ports: dict[Primitive, PrimitivePort] = {}
        for primitive, port in (ports or {}).items():
            self.bind(primitive, port)

    def bind(self, primitive: Primitive, port: PrimitivePort) -> None:
        primitive = Primitive(primitive)
        if primitive in self._ports:
            raise ValueError(f"{primitive.value} already has an adapter")
        self._ports[primitive] = port

    @property
    def bound(self) -> frozenset[Primitive]:
        return frozenset(self._ports)

    async def invoke(self, subject: Subject, args: BaseModel) -> PrimitiveOutcome:
        primitive = Primitive(args.primitive)
        if not isinstance(args, PRIMITIVE_ARGS[primitive]):
            raise TypeError(f"{type(args).__name__} is not the argument model of {primitive}")
        port = self._ports.get(primitive)
        if port is None:
            return PrimitiveOutcome.unknown(primitive, "primitive_unavailable")
        try:
            result = await port(subject, args)
        except ValidationError as exc:
            return PrimitiveOutcome.unknown(
                primitive, f"contract_violation: {exc.errors()[0].get('msg', 'invalid')}"
            )
        if not isinstance(result, PrimitiveOutcome):
            return PrimitiveOutcome.unknown(
                primitive, f"contract_violation: answered {type(result).__name__}"
            )
        if result.primitive is not primitive:
            return PrimitiveOutcome.unknown(
                primitive, f"contract_violation: answered for {result.primitive.value}"
            )
        return result
