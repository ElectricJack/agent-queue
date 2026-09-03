"""Durable Playbook V2 run state — the snapshot, its limits, its error family.

Package 3 child plan §8 and §14.  A V2 run is a frozen :class:`RunSnapshot`
plus an integer ``version``; every durable advance is a compare-and-set on
that version inside one transaction (§4.4).  Nothing in this module talks to
the database — it is the value layer the repository persists and Package 4's
engine computes over.

Two properties are load-bearing and easy to lose:

* the snapshot is **frozen**.  ``commit_boundary`` returns a new object with
  an incremented version rather than mutating its argument, so a caller that
  keeps the stale one cannot accidentally win the next CAS.
* ``bindings`` holds **only** validated declared step outputs.  A raw handler
  dictionary never reaches it (design spec, "Run-state persistence"), which
  is what keeps a receipt projection meaningful.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from src.playbooks.waits import EMPTY_WAIT_CHANGES, WaitChangeSet, WaitClaim, WaitSpec

if TYPE_CHECKING:
    from src.playbooks.receipts import StepReceipt

#: Defaults for ``playbooks.v2_max_result_bytes`` / ``v2_max_snapshot_bytes``.
DEFAULT_MAX_RESULT_BYTES = 262_144
DEFAULT_MAX_SNAPSHOT_BYTES = 4_194_304


# --------------------------------------------------------------------------
# Errors (§14).  Every failure an operator can reach is a named type carrying
# a ``code``, and the code is what lands in ``error_code`` columns — "why did
# this run fail?" is a query, not a log grep.
# --------------------------------------------------------------------------


class PlaybookStorageError(Exception):
    """Base of every Package 3 storage failure."""

    code = "playbook_storage_error"


class ArtifactTooLarge(PlaybookStorageError):
    code = "artifact_too_large"


class ArtifactHashCollision(PlaybookStorageError):
    code = "artifact_hash_collision"


class ArtifactVerificationFailed(PlaybookStorageError):
    code = "artifact_verification_failed"


class SnapshotVersionConflict(PlaybookStorageError):
    """Another writer advanced the run between load and commit."""

    code = "snapshot_version_conflict"

    def __init__(self, run_id: str, expected: int, actual: int | None) -> None:
        super().__init__(
            f"run {run_id} is at snapshot_version {actual!r}, expected {expected}"
        )
        self.run_id = run_id
        self.expected = expected
        self.actual = actual


class RunIdentityMismatch(PlaybookStorageError, ValueError):
    """A boundary disagreed with the identity its run is pinned to.

    ``playbook_id``, ``artifact_sha256`` and ``rule_id`` are fixed when the
    run is created and a historical overlay renders *that* artifact (design
    spec: "A run reads its graph from its pinned artifact hash").  A snapshot
    that keeps the version but swaps any of them would silently rewrite which
    graph a finished run ran, and a receipt naming another artifact, rule or
    snapshot version would do the same to the receipt history, so the
    boundary refuses both and writes nothing.

    ``ValueError`` stays in the bases because ``commit_boundary`` has raised
    one for a receipt from another run since §4.4 landed.
    """

    code = "run_identity_mismatch"

    def __init__(self, run_id: str, field: str, expected: object, actual: object) -> None:
        super().__init__(
            f"boundary for run {run_id} carries {field}={actual!r}, "
            f"but the run is pinned to {expected!r}"
        )
        self.run_id = run_id
        self.field = field
        self.expected = expected
        self.actual = actual


class DuplicateAttempt(PlaybookStorageError):
    """The database rejected a replay of an already-recorded attempt."""

    code = "duplicate_attempt"

    def __init__(self, run_id: str, step_id: str, iteration: int, attempt: int) -> None:
        super().__init__(
            f"attempt {attempt} of step {step_id} (iteration {iteration}) "
            f"is already recorded for run {run_id}"
        )
        self.run_id = run_id
        self.step_id = step_id
        self.iteration = iteration
        self.attempt = attempt


class DuplicateRun(PlaybookStorageError):
    """A run already exists for this dispatch and rule.

    ``uq_playbook_v2_runs_dispatch_rule`` is what makes "one matching event
    creates at most one run per rule" a database property rather than a
    convention.  It is named here so the engine can act on the collision —
    reporting the existing run as deduplicated — instead of pattern-matching
    a driver-specific ``IntegrityError`` message.
    """

    code = "duplicate_run"

    def __init__(self, dispatch_id: str, rule_id: str) -> None:
        super().__init__(f"rule {rule_id} already has a run for dispatch {dispatch_id}")
        self.dispatch_id = dispatch_id
        self.rule_id = rule_id


class DuplicateWait(PlaybookStorageError):
    """A second active wait was registered for a step instance."""

    code = "duplicate_wait"

    def __init__(self, run_id: str, step_id: str, iteration: int) -> None:
        super().__init__(
            f"run {run_id} already has an active wait for step {step_id} "
            f"(iteration {iteration})"
        )
        self.run_id = run_id
        self.step_id = step_id
        self.iteration = iteration


class StateLimitExceeded(PlaybookStorageError):
    """A bound result or a whole snapshot exceeded its configured cap.

    The oversized value is *rejected*, never externalized and never truncated
    (§8.2): the exception carries the size, the caller stores the size, and
    the payload does not reach the database.
    """

    code = "state_limit_exceeded"

    def __init__(self, run_id: str, step_id: str | None, kind: str, size: int, limit: int) -> None:
        subject = f"step {step_id}" if step_id else "snapshot"
        super().__init__(
            f"{kind} for {subject} of run {run_id} is {size} bytes, over the {limit}-byte limit"
        )
        self.run_id = run_id
        self.step_id = step_id
        self.kind = kind
        self.size = size
        self.limit = limit


class IllegalLifecycleTransition(PlaybookStorageError):
    code = "illegal_lifecycle_transition"

    def __init__(self, run_id: str, current: str, target: str) -> None:
        super().__init__(f"run {run_id} cannot move from {current} to {target}")
        self.run_id = run_id
        self.current = current
        self.target = target


class UndeclaredBinding(PlaybookStorageError):
    """A step tried to bind keys its contract does not declare."""

    code = "undeclared_binding"

    def __init__(self, run_id: str, step_id: str, keys: Sequence[str]) -> None:
        super().__init__(
            f"step {step_id} of run {run_id} bound undeclared output(s): {', '.join(keys)}"
        )
        self.run_id = run_id
        self.step_id = step_id
        self.keys = tuple(keys)


class WaitVersionMismatch(PlaybookStorageError):
    code = "wait_version_mismatch"

    def __init__(self, wait_id: str, run_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"wait {wait_id} of run {run_id} records snapshot_version {actual}, "
            f"but the run is at {expected}"
        )
        self.wait_id = wait_id
        self.run_id = run_id
        self.expected = expected
        self.actual = actual


class WaitOwnershipViolation(PlaybookStorageError):
    """A boundary touched a wait that belongs to another run.

    ``commit_boundary`` applies its ``WaitChangeSet`` on the run's own locked
    connection.  A registration or an explicit ``clear_wait_ids`` naming a
    different run would therefore mutate that run's suspension under this
    run's CAS — outside the other run's fence entirely — so the boundary
    rejects it and rolls back.
    """

    code = "wait_ownership_violation"

    def __init__(self, run_id: str, wait_id: str, owner_run_id: str | None) -> None:
        super().__init__(
            f"wait {wait_id} belongs to run {owner_run_id!r}, "
            f"not to boundary run {run_id}"
        )
        self.run_id = run_id
        self.wait_id = wait_id
        self.owner_run_id = owner_run_id


class PendingEventQuotaExceeded(PlaybookStorageError):
    code = "pending_event_quota_exceeded"

    def __init__(self, playbook_id: str, limit: int) -> None:
        super().__init__(
            f"playbook {playbook_id} already holds {limit} unresolved pending events"
        )
        self.playbook_id = playbook_id
        self.limit = limit


class PendingEventIntegrityError(PlaybookStorageError):
    """The database rejected a pending event for a reason other than deduplication."""

    code = "pending_event_integrity_error"

    def __init__(self, playbook_id: str) -> None:
        super().__init__(f"database rejected a pending event for playbook {playbook_id}")
        self.playbook_id = playbook_id


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


class RunLifecycle(str, Enum):
    """The design spec's single lifecycle enum, in its order.

    ``cancelling`` is the one V1 lacks, and it is what makes "signal, then
    acknowledge" survive a restart: the intent is durable before the engine
    has done anything about it.
    """

    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


TERMINAL_LIFECYCLES: frozenset[RunLifecycle] = frozenset(
    {
        RunLifecycle.COMPLETED,
        RunLifecycle.FAILED,
        RunLifecycle.TIMED_OUT,
        RunLifecycle.CANCELLED,
    }
)

#: A terminal state has no outgoing transition at all, which is what stops a
#: late boundary from resurrecting a finished run.  ``paused -> cancelled`` is
#: direct, per the design spec's "a paused run cancels immediately".
LEGAL_TRANSITIONS: dict[RunLifecycle, frozenset[RunLifecycle]] = {
    RunLifecycle.RUNNING: frozenset(
        {
            RunLifecycle.RUNNING,
            RunLifecycle.PAUSED,
            RunLifecycle.CANCELLING,
            RunLifecycle.COMPLETED,
            RunLifecycle.FAILED,
            RunLifecycle.TIMED_OUT,
        }
    ),
    RunLifecycle.PAUSED: frozenset(
        {
            RunLifecycle.PAUSED,
            RunLifecycle.RUNNING,
            RunLifecycle.FAILED,
            RunLifecycle.TIMED_OUT,
            RunLifecycle.CANCELLED,
        }
    ),
    RunLifecycle.CANCELLING: frozenset(
        {
            RunLifecycle.CANCELLING,
            RunLifecycle.CANCELLED,
            RunLifecycle.FAILED,
            RunLifecycle.TIMED_OUT,
        }
    ),
    RunLifecycle.COMPLETED: frozenset(),
    RunLifecycle.FAILED: frozenset(),
    RunLifecycle.TIMED_OUT: frozenset(),
    RunLifecycle.CANCELLED: frozenset(),
}

RUN_MODES: frozenset[str] = frozenset({"live", "dry_run", "shadow"})


def validate_transition(run_id: str, current: RunLifecycle, target: RunLifecycle) -> None:
    """Raise :class:`IllegalLifecycleTransition` unless the move is allowed."""
    if target not in LEGAL_TRANSITIONS[current]:
        raise IllegalLifecycleTransition(run_id, current.value, target.value)


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunBudget:
    """LLM consumption accumulated by a run.  Field-for-field ``RunBudgetDTO``."""

    llm_calls: int = 0
    total_tokens: int = 0
    max_total_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class OperatorDecision:
    """A non-retry-safe step that was interrupted ambiguously (§9.3)."""

    step_id: str
    attempt: int
    reason: str
    raised_at: float
    options: tuple[str, ...] = ("accept_outcome", "retry", "fail", "cancel")


@dataclass(frozen=True, slots=True)
class LoopFrame:
    """Where a ``ForEachStep`` is, pinned to the collection it started on.

    ``last_step_id`` / ``last_outcome`` / ``last_failed`` record how the body
    of the *current* iteration ended.  They are durable rather than passed in
    memory because the frame is committed on both sides of every body
    transition: a crash after the body's last step transitions back into the
    loop node must leave the restarted engine knowing whether that iteration
    succeeded, and the body's own receipt is not enough — the classification
    depends on the producing step's contract, which the loop executor must
    not have to re-derive (Package 4 child plan §4.7).
    """

    step_id: str
    item_binding: str
    collection_digest: str
    index: int
    total: int
    partial: tuple[Any, ...] = ()
    resume_step_id: str | None = None
    last_step_id: str | None = None
    last_outcome: str | None = None
    last_failed: bool | None = None


@dataclass(frozen=True, slots=True)
class StateLimits:
    """The two size caps, resolved from ``playbooks`` config or defaulted."""

    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES
    max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES

    @classmethod
    def from_config(cls, playbooks_config: Any) -> StateLimits:
        return cls(
            max_result_bytes=int(
                getattr(playbooks_config, "v2_max_result_bytes", DEFAULT_MAX_RESULT_BYTES)
            ),
            max_snapshot_bytes=int(
                getattr(playbooks_config, "v2_max_snapshot_bytes", DEFAULT_MAX_SNAPSHOT_BYTES)
            ),
        )


DEFAULT_STATE_LIMITS = StateLimits()


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """Everything a restarted process needs to continue one run.

    ``version`` is the optimistic-concurrency token, not part of the body's
    meaning: it is what ``commit_boundary`` compares and advances.
    """

    run_id: str
    playbook_id: str
    artifact_sha256: str
    rule_id: str
    lifecycle: RunLifecycle = RunLifecycle.RUNNING
    mode: str = "live"
    version: int = 0
    current_step_id: str | None = None
    event: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    bindings: Mapping[str, Any] = field(default_factory=dict)
    sensitive: Mapping[str, Any] = field(default_factory=dict)
    #: ``"<step_id>:<iteration>" -> attempts already receipted``.  Attempt
    #: identity is four-part (§9.1) and the database enforces it through
    #: ``uq_playbook_step_receipts_attempt``, so a step the walk reaches
    #: twice — a wait suspending and then resuming, a loop node between
    #: iterations, an author routing an edge back to an earlier step —
    #: has to know how many attempts it has already recorded.  Counting
    #: here rather than re-reading the receipt table keeps the boundary a
    #: single write and survives a restart with the snapshot.
    attempts: Mapping[str, int] = field(default_factory=dict)
    loop: LoopFrame | None = None
    wait: WaitSpec | None = None
    pending_wait_claims: tuple[WaitClaim, ...] = ()
    budget: RunBudget = field(default_factory=RunBudget)
    agent_task_ids: tuple[str, ...] = ()
    llm_turns: tuple[Mapping[str, Any], ...] = ()
    operator_decision: OperatorDecision | None = None
    cancel_requested_at: float | None = None
    deadline_at: float | None = None
    event_type: str = ""
    event_id: str | None = None
    dispatch_id: str | None = None
    parent_run_id: str | None = None
    parent_step_id: str | None = None
    summary: str = ""
    error: str | None = None
    error_code: str | None = None
    started_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id is required")
        if self.mode not in RUN_MODES:
            raise ValueError(f"mode must be one of {sorted(RUN_MODES)}, got {self.mode!r}")
        if self.version < 0:
            raise ValueError("version must be >= 0")
        if not isinstance(self.lifecycle, RunLifecycle):
            object.__setattr__(self, "lifecycle", RunLifecycle(self.lifecycle))

    @property
    def is_terminal(self) -> bool:
        return self.lifecycle in TERMINAL_LIFECYCLES

    def redacted(self) -> RunSnapshot:
        """Drop plaintext sensitive values.  Any projection must use this."""
        return replace(self, sensitive={})

    def to_body(self) -> dict[str, Any]:
        """The dict persisted as ``playbook_v2_runs.snapshot``."""
        body = {
            "run_id": self.run_id,
            "playbook_id": self.playbook_id,
            "artifact_sha256": self.artifact_sha256,
            "rule_id": self.rule_id,
            "lifecycle": self.lifecycle.value,
            "mode": self.mode,
            "version": self.version,
            "current_step_id": self.current_step_id,
            "event": dict(self.event),
            "context": dict(self.context),
            "bindings": dict(self.bindings),
            "sensitive": dict(self.sensitive),
            "attempts": dict(self.attempts),
            "loop": asdict(self.loop) if self.loop is not None else None,
            "wait": self.wait.as_dict() if self.wait is not None else None,
            "pending_wait_claims": [asdict(claim) for claim in self.pending_wait_claims],
            "budget": asdict(self.budget),
            "agent_task_ids": list(self.agent_task_ids),
            "llm_turns": [dict(turn) for turn in self.llm_turns],
            "operator_decision": (
                asdict(self.operator_decision) if self.operator_decision is not None else None
            ),
            "cancel_requested_at": self.cancel_requested_at,
            "deadline_at": self.deadline_at,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "dispatch_id": self.dispatch_id,
            "parent_run_id": self.parent_run_id,
            "parent_step_id": self.parent_step_id,
            "summary": self.summary,
            "error": self.error,
            "error_code": self.error_code,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }
        if self.loop is not None:
            body["loop"]["partial"] = list(self.loop.partial)
        if self.operator_decision is not None:
            body["operator_decision"]["options"] = list(self.operator_decision.options)
        return body

    @classmethod
    def from_body(cls, body: Mapping[str, Any], *, version: int | None = None) -> RunSnapshot:
        loop = body.get("loop")
        wait = body.get("wait")
        decision = body.get("operator_decision")
        return cls(
            run_id=body["run_id"],
            playbook_id=body["playbook_id"],
            artifact_sha256=body["artifact_sha256"],
            rule_id=body["rule_id"],
            lifecycle=RunLifecycle(body.get("lifecycle", RunLifecycle.RUNNING.value)),
            mode=body.get("mode", "live"),
            version=int(body.get("version", 0)) if version is None else int(version),
            current_step_id=body.get("current_step_id"),
            event=dict(body.get("event") or {}),
            context=dict(body.get("context") or {}),
            bindings=dict(body.get("bindings") or {}),
            sensitive=dict(body.get("sensitive") or {}),
            attempts={str(k): int(v) for k, v in (body.get("attempts") or {}).items()},
            loop=(
                LoopFrame(
                    step_id=loop["step_id"],
                    item_binding=loop["item_binding"],
                    collection_digest=loop["collection_digest"],
                    index=int(loop["index"]),
                    total=int(loop["total"]),
                    partial=tuple(loop.get("partial") or ()),
                    resume_step_id=loop.get("resume_step_id"),
                    last_step_id=loop.get("last_step_id"),
                    last_outcome=loop.get("last_outcome"),
                    last_failed=loop.get("last_failed"),
                )
                if loop
                else None
            ),
            wait=WaitSpec.from_dict(wait) if wait else None,
            pending_wait_claims=tuple(
                WaitClaim(
                    wait_id=claim["wait_id"],
                    run_id=claim["run_id"],
                    step_id=claim["step_id"],
                    iteration=int(claim["iteration"]),
                    kind=claim["kind"],
                    snapshot_version=int(claim["snapshot_version"]),
                    claimed_event_id=claim.get("claimed_event_id"),
                    claimed_at=float(claim["claimed_at"]),
                    expired=bool(claim.get("expired", False)),
                    event_type=claim.get("event_type", ""),
                    event_fields=dict(claim.get("event_fields") or {}),
                )
                for claim in (body.get("pending_wait_claims") or ())
            ),
            budget=RunBudget(**(body.get("budget") or {})),
            agent_task_ids=tuple(body.get("agent_task_ids") or ()),
            llm_turns=tuple(dict(turn) for turn in (body.get("llm_turns") or ())),
            operator_decision=(
                OperatorDecision(
                    step_id=decision["step_id"],
                    attempt=int(decision["attempt"]),
                    reason=decision["reason"],
                    raised_at=float(decision["raised_at"]),
                    options=tuple(decision.get("options") or ()),
                )
                if decision
                else None
            ),
            cancel_requested_at=body.get("cancel_requested_at"),
            deadline_at=body.get("deadline_at"),
            event_type=body.get("event_type", ""),
            event_id=body.get("event_id"),
            dispatch_id=body.get("dispatch_id"),
            parent_run_id=body.get("parent_run_id"),
            parent_step_id=body.get("parent_step_id"),
            summary=body.get("summary", ""),
            error=body.get("error"),
            error_code=body.get("error_code"),
            started_at=float(body.get("started_at") or 0.0),
            updated_at=float(body.get("updated_at") or 0.0),
            completed_at=body.get("completed_at"),
        )


# --------------------------------------------------------------------------
# Serialization and the two size limits (§8.2)
# --------------------------------------------------------------------------


def canonical_json(value: Any) -> bytes:
    """Deterministic bytes for a snapshot body or a bound result.

    This is *not* the artifact canonicalizer (Package 2 owns that, §5.1); it
    only has to be stable so a round trip is byte-identical and a size check
    measures what is stored.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def check_result_size(
    run_id: str,
    step_id: str,
    value: Any,
    *,
    limits: StateLimits = DEFAULT_STATE_LIMITS,
) -> int:
    """Measure a bound result *before* it enters the snapshot.

    Running here rather than at write time is what keeps an oversized payload
    out of the database entirely — the failure path stores the size, not the
    value.
    """
    size = len(canonical_json(value))
    if size > limits.max_result_bytes:
        raise StateLimitExceeded(run_id, step_id, "result", size, limits.max_result_bytes)
    return size


def serialize_snapshot(
    snapshot: RunSnapshot, *, limits: StateLimits = DEFAULT_STATE_LIMITS
) -> bytes:
    """Canonical bytes for ``playbook_v2_runs.snapshot``, size-checked."""
    payload = canonical_json(snapshot.to_body())
    if len(payload) > limits.max_snapshot_bytes:
        raise StateLimitExceeded(
            snapshot.run_id, None, "snapshot", len(payload), limits.max_snapshot_bytes
        )
    return payload


def deserialize_snapshot(payload: str | bytes, *, version: int | None = None) -> RunSnapshot:
    body = json.loads(payload)
    return RunSnapshot.from_body(body, version=version)


def bind_step_output(
    snapshot: RunSnapshot,
    *,
    step_id: str,
    value: Mapping[str, Any],
    declared: Collection[str],
    limits: StateLimits = DEFAULT_STATE_LIMITS,
) -> RunSnapshot:
    """Bind one step's validated declared output onto the snapshot.

    The declared-output check is the storage half of the design spec's "a
    bound result contains only the step's validated declared output, not an
    arbitrary handler dictionary".  Without it a handler could smuggle an
    unprojected field into durable state, where no receipt redaction sees it.
    """
    if not isinstance(value, Mapping):
        raise UndeclaredBinding(snapshot.run_id, step_id, ("<non-mapping result>",))
    undeclared = sorted(set(value) - set(declared))
    if undeclared:
        raise UndeclaredBinding(snapshot.run_id, step_id, undeclared)
    check_result_size(snapshot.run_id, step_id, value, limits=limits)
    bindings = dict(snapshot.bindings)
    bindings[step_id] = dict(value)
    return replace(snapshot, bindings=bindings)


# --------------------------------------------------------------------------
# The repository shape Package 4 depends on (§4.4)
# --------------------------------------------------------------------------


class RunRepository(Protocol):
    """Declared here so Package 4 never imports the database package."""

    async def create_run(self, snapshot: RunSnapshot) -> RunSnapshot: ...

    async def load_run(self, run_id: str) -> RunSnapshot | None: ...

    async def find_run_for_dispatch(
        self, dispatch_id: str, rule_id: str
    ) -> RunSnapshot | None: ...

    async def commit_boundary(
        self,
        snapshot: RunSnapshot,
        receipt: StepReceipt,
        wait_changes: WaitChangeSet = EMPTY_WAIT_CHANGES,
    ) -> RunSnapshot: ...

    async def request_cancel(
        self, run_id: str, *, expected_version: int, reason: str, requested_by: str
    ) -> RunSnapshot: ...

    async def list_runs(
        self,
        *,
        playbook_id: str | None = None,
        lifecycle: str | None = None,
        artifact_sha256: str | None = None,
        limit: int = 50,
    ) -> list[RunSnapshot]: ...

    async def list_receipts(
        self, run_id: str, *, limit: int = 500, offset: int = 0
    ) -> list[StepReceipt]: ...
