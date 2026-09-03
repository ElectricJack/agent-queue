"""The V2 agent-task executor — Package 4 child plan T-8, §4.5, §7.2 and §7.4.

Every assertion here is about *authority* or about *durability*, because
those are the two things an ``AgentTaskStep`` does that no other step does:
it hands a slice of the run's permissions to something else, and it survives
a restart while that something else works.

The two shapes worth reading twice:

* the fail-closed tests assert ``adapter.calls == []`` as well as the
  outcome.  A rule that refuses *after* creating the child is not fail-closed,
  and only the call count can tell the two apart;
* the duplicate-delivery test asserts one receipt and one transition, not
  merely a stable end state.  Re-entering the executor would create a second
  child task, which is the expensive form of a duplicate side effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
)
from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.playbooks.definition import AgentTaskStep, CapabilityNarrowing, PlaybookDefinition
from src.playbooks.engine import ChildTaskCompleted, PlaybookEngine
from src.playbooks.executors import executor_for
from src.playbooks.executors.agent_task import (
    AWAITING_OUTCOME,
    child_outcome_for_status,
)
from src.playbooks.executors.base import (
    EngineServices,
    ExecutionMode,
    StepContext,
    StepControl,
)
from src.playbooks.expressions import ResolutionScope
from src.playbooks.run_state import RunLifecycle
from src.profiles.capabilities import CapabilityPolicy
from tests.fixtures.contracts.engine_contracts import registry_with
from tests.playbook_v2_engine_helpers import (
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)

# --------------------------------------------------------------------------
# Contract doubles: the child create and the child stop
# --------------------------------------------------------------------------


class CreateTaskArgs(CommandArgs):
    title: str
    project_id: str | None = None
    profile_id: str | None = None


class CreateTaskResult(CommandValue):
    task_id: str
    created: bool = True


CREATE_TASK = CommandContract(
    execution=ExecutionContract(
        name="create_task",
        args_model=CreateTaskArgs,
        result_model=CreateTaskResult,
        outcomes=(
            OutcomeSpec(name="created", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
        ),
        capability="create_task",
        side_effect=SideEffectClass.CREATE,
        idempotency=IdempotencySpec(mode="none"),
        retry_safe=False,
        receipt_projection=("task_id", "created"),
    ),
    presentation=CommandPresentation(title="Create a task", summary="Create the child task"),
)


class StopTaskArgs(CommandArgs):
    task_id: str


class StopTaskResult(CommandValue):
    stopped: bool = True


STOP_TASK = CommandContract(
    execution=ExecutionContract(
        name="stop_task",
        args_model=StopTaskArgs,
        result_model=StopTaskResult,
        outcomes=(OutcomeSpec(name="stopped", classification=OutcomeClass.SUCCESS),),
        capability="stop_task",
        side_effect=SideEffectClass.UPDATE,
        idempotency=IdempotencySpec(mode="natural"),
        retry_safe=True,
    ),
    presentation=CommandPresentation(title="Stop a task", summary="Stop the child task"),
)


def created(task_id: str = "child-1") -> CommandResult[Any]:
    return CommandResult(
        outcome="created", value=CreateTaskResult(task_id=task_id), summary="created"
    )


# --------------------------------------------------------------------------
# Profile store double
# --------------------------------------------------------------------------


@dataclass
class StubProfile:
    """Just enough of an ``AgentProfile`` for ``capability_policy_for``."""

    harness_tools: list[str] | None = field(default_factory=list)
    aq_commands: list[str] | None = field(default_factory=list)
    plugin_tools: list[str] | None = field(default_factory=list)


class StubDatabase:
    """The profile store the executor resolves the child profile against."""

    def __init__(self, profiles: dict[str, StubProfile] | None = None) -> None:
        self.profiles = profiles or {}
        self.lookups: list[str] = []

    async def get_profile(self, profile_id: str) -> StubProfile | None:
        self.lookups.append(profile_id)
        return self.profiles.get(profile_id)


def policy(**namespaces: set[str] | list[str]) -> CapabilityPolicy:
    return CapabilityPolicy.from_namespaces(
        **{name: sorted(values) for name, values in namespaces.items()}
    )


def parent_principal(
    *,
    kind: PrincipalKind = PrincipalKind.PLAYBOOK,
    profile_id: str | None = None,
    **namespaces: set[str] | list[str],
) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        kind=kind, policy=policy(**namespaces), profile_id=profile_id
    )


# --------------------------------------------------------------------------
# Artifact and step fixtures
# --------------------------------------------------------------------------

SOURCE = {"path": "x.md", "start_line": 1, "end_line": 1}


def agent_task_step(**overrides: Any) -> AgentTaskStep:
    payload: dict[str, Any] = {
        "rule": "r",
        "title": "Delegate the review",
        "source": SOURCE,
        "profile_id": "reviewer",
        "objective": {"type": "literal", "value": "Review the change"},
        "inputs": {"project_id": {"type": "literal", "value": "p"}},
        "transitions": {
            "dispatched": "done",
            "completed": "done",
            "failed": "bad",
            "timed_out": "bad",
            "cancelled": "bad",
        },
    }
    payload.update(overrides)
    return AgentTaskStep.model_validate(payload)


def agent_task_artifact(step: AgentTaskStep | None = None) -> PlaybookDefinition:
    """One rule: delegate, then terminate either way."""
    step = step or agent_task_step()
    payload = {
        "schema_version": 2,
        "id": "delegating",
        "version": 1,
        "scope": {"type": "system"},
        "source_hash": "sha256:" + "2" * 64,
        "compiled_at": "2026-09-01T00:00:00Z",
        "purpose": "routine",
        "rules": [
            {
                "id": "r",
                "name": "Rule",
                "trigger": {"event_type": "task.completed"},
                "entry_step": "delegate",
                "source": SOURCE,
            }
        ],
        "steps": {
            "delegate": step.model_dump(mode="json"),
            "done": {
                "type": "terminal",
                "rule": "r",
                "title": "Done",
                "outcome": "completed",
                "source": SOURCE,
            },
            "bad": {
                "type": "terminal",
                "rule": "r",
                "title": "Bad",
                "outcome": "failed",
                "source": SOURCE,
            },
        },
    }
    return PlaybookDefinition.model_validate(payload)


def context(
    registry: Any,
    *,
    principal: ExecutionPrincipal,
    db: StubDatabase,
    inputs: dict[str, Any] | None = None,
    mode: ExecutionMode = ExecutionMode.LIVE,
    clock: float = 100.0,
) -> StepContext:
    artifact = agent_task_artifact()
    return StepContext(
        run_id="run-1",
        dispatch_id="d-1",
        artifact_ref=artifact_ref_for(artifact),
        artifact=artifact,
        rule_id="r",
        step_id="delegate",
        principal=principal,
        scope=ResolutionScope(),
        services=EngineServices(contracts=registry, clock=lambda: clock, db=db),
        mode=mode,
        inputs=inputs if inputs is not None else {"project_id": "p"},
    )


async def run(step: AgentTaskStep, ctx: StepContext, mode: ExecutionMode = ExecutionMode.LIVE):
    return await executor_for(step.type, mode).execute(step, ctx)


# --------------------------------------------------------------------------
# §4.5 step 1 / §7.2 — delegation narrows three ways
# --------------------------------------------------------------------------


class TestDelegationNarrowing:
    @pytest.mark.asyncio
    async def test_child_policy_is_the_three_way_intersection(self):
        """parent {a,b,c} ∩ profile {b,c,d} ∩ step {c,d} == {c}."""
        registry, adapter = registry_with(CREATE_TASK)
        adapter.queue.append(created())
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["b", "c", "d"])})
        step = agent_task_step(
            capability_narrowing={"aq_commands": ["c", "d"]}, wait_for_completion=False
        )
        ctx = context(
            registry,
            principal=parent_principal(aq_commands={"a", "b", "c"}),
            db=db,
        )

        result = await run(step, ctx)

        assert result.outcome == "dispatched"
        (_, _, child) = adapter.calls[0]
        assert child.policy.aq_commands == frozenset({"c"})
        assert child.provenance[-1] == "agent_task:delegate"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("namespace", ["harness_tools", "aq_commands", "plugin_tools"])
    async def test_child_cannot_widen_in_any_namespace(self, namespace: str):
        """A broader child profile yields the intersection, never the union."""
        registry, adapter = registry_with(CREATE_TASK)
        adapter.queue.append(created())
        parent_names = {"harness_tools": "Read", "aq_commands": "list_tasks", "plugin_tools": "read_file"}
        keep = parent_names[namespace]
        extra = "mcp__evil__exfiltrate" if namespace == "plugin_tools" else "Bash" if namespace == "harness_tools" else "delete_task"
        db = StubDatabase({"reviewer": StubProfile(**{namespace: [keep, extra]})})
        ctx = context(
            registry,
            principal=parent_principal(**{namespace: {keep}}),
            db=db,
        )

        result = await run(agent_task_step(wait_for_completion=False), ctx)

        assert result.outcome == "dispatched"
        (_, _, child) = adapter.calls[0]
        assert getattr(child.policy, namespace) == frozenset({keep})

    @pytest.mark.asyncio
    async def test_narrowing_a_namespace_it_does_not_name_is_the_identity(self):
        """``None`` narrows nothing; an explicit ``[]`` means none."""
        registry, adapter = registry_with(CREATE_TASK)
        adapter.queue.append(created())
        db = StubDatabase(
            {"reviewer": StubProfile(harness_tools=["Read"], aq_commands=["list_tasks"])}
        )
        step = agent_task_step(
            capability_narrowing=CapabilityNarrowing(aq_commands=[]),
            wait_for_completion=False,
        )
        ctx = context(
            registry,
            principal=parent_principal(
                harness_tools={"Read"}, aq_commands={"list_tasks"}
            ),
            db=db,
        )

        await run(step, ctx)

        (_, _, child) = adapter.calls[0]
        assert child.policy.harness_tools == frozenset({"Read"})
        assert child.policy.aq_commands == frozenset()

    @pytest.mark.asyncio
    async def test_ai_parent_requires_the_child_profile_to_be_a_subset(self):
        """A too-broad child profile is refused, not clamped — and never created."""
        registry, adapter = registry_with(CREATE_TASK)
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["list_tasks", "delete_task"])})
        ctx = context(
            registry,
            principal=parent_principal(
                profile_id="supervisor", aq_commands={"list_tasks"}
            ),
            db=db,
        )

        result = await run(agent_task_step(), ctx)

        assert result.outcome == "unauthorized"
        assert adapter.calls == []
        assert "delete_task" in result.diagnostics[0]

    @pytest.mark.asyncio
    async def test_unknown_profile_fails_closed_without_creating_a_task(self):
        registry, adapter = registry_with(CREATE_TASK)
        ctx = context(
            registry, principal=parent_principal(aq_commands={"a"}), db=StubDatabase()
        )

        result = await run(agent_task_step(), ctx)

        assert result.outcome == "unauthorized"
        assert adapter.calls == []

    @pytest.mark.asyncio
    async def test_missing_profile_store_fails_closed(self):
        """No identity source is "we could not find out", never "anything goes"."""
        registry, adapter = registry_with(CREATE_TASK)
        artifact = agent_task_artifact()
        ctx = StepContext(
            run_id="run-1",
            dispatch_id="d-1",
            artifact_ref=artifact_ref_for(artifact),
            artifact=artifact,
            rule_id="r",
            step_id="delegate",
            principal=parent_principal(aq_commands={"a"}),
            scope=ResolutionScope(),
            services=EngineServices(contracts=registry, clock=lambda: 100.0),
            inputs={"project_id": "p"},
        )

        result = await run(agent_task_step(), ctx)

        assert result.outcome == "unauthorized"
        assert adapter.calls == []


# --------------------------------------------------------------------------
# §4.5 steps 3 and 4 — dispatch, suspend, persist
# --------------------------------------------------------------------------


class TestDispatch:
    @pytest.mark.asyncio
    async def test_wait_for_completion_false_advances_on_dispatched(self):
        registry, adapter = registry_with(CREATE_TASK)
        adapter.queue.append(created("child-9"))
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["a"])})
        ctx = context(registry, principal=parent_principal(aq_commands={"a"}), db=db)

        result = await run(agent_task_step(wait_for_completion=False), ctx)

        assert result.control is StepControl.ADVANCE
        assert result.outcome == "dispatched"
        assert result.child_task_id == "child-9"
        assert result.wait is None

    @pytest.mark.asyncio
    async def test_waiting_suspends_on_the_child_with_the_step_deadline(self):
        registry, adapter = registry_with(CREATE_TASK)
        adapter.queue.append(created("child-2"))
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["a"])})
        ctx = context(
            registry, principal=parent_principal(aq_commands={"a"}), db=db, clock=100.0
        )

        result = await run(agent_task_step(timeout_seconds=600), ctx)

        assert result.control is StepControl.SUSPEND
        assert result.outcome == AWAITING_OUTCOME
        assert result.child_task_id == "child-2"
        assert result.wait is not None
        assert result.wait.kind == "agent_task"
        assert result.wait.match == {"task_id": "child-2"}
        assert result.wait.deadline_at == 700.0
        assert result.receipt_result["child_task_id"] == "child-2"

    @pytest.mark.asyncio
    async def test_a_refused_creation_takes_the_failed_edge(self):
        registry, adapter = registry_with(CREATE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="rejected", value=CreateTaskResult(task_id=""), summary="no"
            )
        )
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["a"])})
        ctx = context(registry, principal=parent_principal(aq_commands={"a"}), db=db)

        result = await run(agent_task_step(), ctx)

        assert result.outcome == "failed"
        assert result.wait is None

    @pytest.mark.asyncio
    async def test_an_uncontracted_create_is_a_contract_violation(self):
        registry, _ = registry_with(STOP_TASK)
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["a"])})
        ctx = context(registry, principal=parent_principal(aq_commands={"a"}), db=db)

        result = await run(agent_task_step(), ctx)

        assert result.outcome == "contract_violation"


class TestSymbolicExecutor:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.SHADOW])
    async def test_no_task_is_created_and_the_path_forks(self, mode: ExecutionMode):
        registry, adapter = registry_with(CREATE_TASK)
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["a"])})
        ctx = context(
            registry, principal=parent_principal(aq_commands={"a"}), db=db, mode=mode
        )

        result = await run(agent_task_step(), ctx, mode)

        assert result.control is StepControl.UNRESOLVED
        assert adapter.calls == []
        assert result.possible_outcomes == (
            "cancelled",
            "completed",
            "dispatched",
            "failed",
            "timed_out",
        )

    def test_the_same_object_serves_dry_run_and_shadow(self):
        assert executor_for("agent_task", ExecutionMode.DRY_RUN) is executor_for(
            "agent_task", ExecutionMode.SHADOW
        )


# --------------------------------------------------------------------------
# The engine seams: persistence order, reconciliation, cancellation
# --------------------------------------------------------------------------


class OrderingRepository(RecordingRunRepository):
    """Records what each boundary wrote, in order.

    The ordering claim T-8 makes is not "two writes in the right sequence" —
    it is that there is exactly *one* write, and that the paused lifecycle and
    the child's identity are both in it.  A repository that saw a paused
    snapshot without the child id would be the bug.
    """

    def __init__(self) -> None:
        super().__init__()
        self.boundaries: list[tuple[str, tuple[str, ...], str | None]] = []
        self.wait_changes: list[Any] = []

    async def commit_boundary(self, snapshot, receipt, wait_changes=None):
        self.boundaries.append(
            (
                snapshot.lifecycle.value,
                snapshot.agent_task_ids,
                snapshot.wait.wait_id if snapshot.wait else None,
            )
        )
        self.wait_changes.append(wait_changes)
        return await super().commit_boundary(snapshot, receipt, wait_changes)


def engine_for(
    artifact: PlaybookDefinition,
    registry: Any,
    db: StubDatabase,
    repository: RecordingRunRepository,
    *,
    clock: float = 100.0,
) -> tuple[PlaybookEngine, Any]:
    ref = artifact_ref_for(artifact)

    class Store:
        def load(self, sha: str) -> PlaybookDefinition:
            return artifact

        def exists(self, sha: str) -> bool:
            return True

    services = EngineServices(
        contracts=registry, clock=lambda: clock, artifact_store=Store(), db=db
    )
    engine = PlaybookEngine(
        services=services, runs=repository, activations=StubActivations([ref])
    )
    return engine, ref


class TestDurableChildIdentity:
    @pytest.mark.asyncio
    async def test_child_task_id_is_persisted_before_the_run_is_paused(self):
        registry, adapter = registry_with(CREATE_TASK)
        adapter.queue.append(created("child-7"))
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["a"])})
        artifact = agent_task_artifact(agent_task_step(timeout_seconds=60))
        repository = OrderingRepository()
        engine, ref = engine_for(artifact, registry, db, repository)

        outcome = await engine.run_rule(
            ref, "r", {"event_id": "e1"}, parent_principal(aq_commands={"a"})
        )

        assert outcome.lifecycle is RunLifecycle.PAUSED
        assert repository.boundaries == [("paused", ("child-7",), outcome.snapshot.wait.wait_id)]
        assert outcome.snapshot.agent_task_ids == ("child-7",)
        assert repository.receipts[-1].result["child_task_id"] == "child-7"
        assert repository.receipts[-1].wait_id == outcome.snapshot.wait.wait_id
        assert repository.wait_changes[-1].register == (outcome.snapshot.wait,)


class TestReconciliation:
    async def _paused_run(self, repository: RecordingRunRepository, **step_kwargs: Any):
        registry, adapter = registry_with(CREATE_TASK, STOP_TASK)
        adapter.queue.append(created("child-3"))
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["a"])})
        artifact = agent_task_artifact(agent_task_step(**step_kwargs))
        engine, ref = engine_for(artifact, registry, db, repository)
        principal = parent_principal(aq_commands={"a"})
        outcome = await engine.run_rule(ref, "r", {"event_id": "e1"}, principal)
        assert outcome.lifecycle is RunLifecycle.PAUSED
        return engine, outcome.run_id, principal, adapter

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "lifecycle"),
        [
            ("COMPLETED", RunLifecycle.COMPLETED),
            ("FAILED", RunLifecycle.FAILED),
            ("cancelled", RunLifecycle.FAILED),
        ],
    )
    async def test_child_status_maps_onto_the_declared_edge(self, status, lifecycle):
        repository = RecordingRunRepository()
        engine, run_id, principal, _ = await self._paused_run(repository)

        outcome = await engine.resume(run_id, ChildTaskCompleted("child-3", status), principal)

        assert outcome.lifecycle is lifecycle
        assert outcome.receipts[0].selected_transition.endswith(
            child_outcome_for_status(status)
        )

    @pytest.mark.asyncio
    async def test_child_timeout_takes_the_timed_out_edge(self):
        repository = RecordingRunRepository()
        engine, run_id, principal, _ = await self._paused_run(
            repository, timeout_seconds=30
        )

        outcome = await engine.resume(
            run_id, ChildTaskCompleted("child-3", "timed_out"), principal
        )

        assert outcome.receipts[0].selected_transition == "r::delegate::timed_out"
        assert outcome.receipts[0].outcome == "timeout"
        assert outcome.snapshot.current_step_id == "bad"

    @pytest.mark.asyncio
    async def test_duplicate_child_completion_is_a_noop(self):
        repository = RecordingRunRepository()
        engine, run_id, principal, adapter = await self._paused_run(repository)
        commits_after_pause = repository.commit_calls

        first = await engine.resume(
            run_id, ChildTaskCompleted("child-3", "COMPLETED"), principal
        )
        commits_after_first = repository.commit_calls
        second = await engine.resume(
            run_id, ChildTaskCompleted("child-3", "COMPLETED"), principal
        )

        assert first.lifecycle is RunLifecycle.COMPLETED
        # Either guard is a no-op; which one answers depends only on whether
        # the first delivery happened to finish the run.  What must hold in
        # both cases is below: no second commit, no second child, one edge.
        assert second.outcome in {"duplicate_child_completion", "already_terminal"}
        assert repository.commit_calls == commits_after_first
        # One reconciliation receipt plus the terminal step's, and no second
        # child: re-entering the executor would have called create_task again.
        assert commits_after_first - commits_after_pause == 2
        assert adapter.names.count("create_task") == 1
        transitions = [
            r.selected_transition
            for r in repository.receipts
            if r.step_id == "delegate" and r.selected_transition
        ]
        assert transitions == ["r::delegate::completed"]

    @pytest.mark.asyncio
    async def test_a_completion_for_another_task_is_ignored(self):
        repository = RecordingRunRepository()
        engine, run_id, principal, _ = await self._paused_run(repository)

        outcome = await engine.resume(
            run_id, ChildTaskCompleted("someone-elses-child", "COMPLETED"), principal
        )

        assert outcome.outcome == "duplicate_child_completion"
        assert outcome.lifecycle is RunLifecycle.PAUSED

    @pytest.mark.asyncio
    async def test_an_unmapped_child_status_is_never_guessed(self):
        repository = RecordingRunRepository()
        engine, run_id, principal, _ = await self._paused_run(repository)

        outcome = await engine.resume(
            run_id, ChildTaskCompleted("child-3", "WAITING_INPUT"), principal
        )

        assert outcome.lifecycle is RunLifecycle.FAILED
        assert outcome.receipts[0].error_code == "runtime_error"


class TestCancellation:
    async def _paused_run(self, repository: RecordingRunRepository, **step_kwargs: Any):
        registry, adapter = registry_with(CREATE_TASK, STOP_TASK)
        adapter.queue.append(created("child-5"))
        adapter.queue.append(
            CommandResult(outcome="stopped", value=StopTaskResult(), summary="stopped")
        )
        db = StubDatabase({"reviewer": StubProfile(aq_commands=["a", "b"])})
        artifact = agent_task_artifact(agent_task_step(**step_kwargs))
        engine, ref = engine_for(artifact, registry, db, repository)
        principal = parent_principal(aq_commands={"a"})
        outcome = await engine.run_rule(ref, "r", {"event_id": "e1"}, principal)
        assert outcome.lifecycle is RunLifecycle.PAUSED
        return engine, outcome.run_id, principal, adapter

    @pytest.mark.asyncio
    async def test_cancel_child_defaults_to_false(self):
        """Cancelling a parent leaves shared or reused child work running."""
        repository = RecordingRunRepository()
        engine, run_id, principal, adapter = await self._paused_run(repository)

        await engine.cancel(run_id, principal)

        assert adapter.names == ["create_task"]

    @pytest.mark.asyncio
    async def test_cancel_child_true_stops_the_child(self):
        repository = RecordingRunRepository()
        engine, run_id, principal, adapter = await self._paused_run(
            repository, cancel_child=True
        )

        await engine.cancel(run_id, principal)

        assert adapter.names == ["create_task", "stop_task"]
        assert adapter.args_for("stop_task")[0].task_id == "child-5"

    @pytest.mark.asyncio
    async def test_an_explicit_override_can_cancel_a_default_child(self):
        repository = RecordingRunRepository()
        engine, run_id, principal, adapter = await self._paused_run(repository)

        await engine.cancel(run_id, principal, cancel_children=True)

        assert adapter.names == ["create_task", "stop_task"]

    @pytest.mark.asyncio
    async def test_cancellation_grants_no_new_authority(self):
        """The stop is dispatched as the narrowed child, not as the parent."""
        repository = RecordingRunRepository()
        engine, run_id, principal, adapter = await self._paused_run(
            repository, cancel_child=True
        )

        await engine.cancel(run_id, principal)

        (_, _, cancelling) = adapter.calls[-1]
        assert cancelling.policy.aq_commands == frozenset({"a"})
        assert cancelling.provenance[-1] == "agent_task:delegate"
        assert cancelling.policy.is_subset_of(principal.policy)


class TestStatusMapping:
    @pytest.mark.parametrize(
        ("status", "outcome"),
        [
            ("COMPLETED", "completed"),
            ("completed", "completed"),
            ("FAILED", "failed"),
            ("cancelled", "cancelled"),
            ("timed_out", "timed_out"),
            ("READY", "runtime_error"),
            ("", "runtime_error"),
        ],
    )
    def test_status_mapping_is_exhaustive_not_defaulted(self, status, outcome):
        assert child_outcome_for_status(status) == outcome
