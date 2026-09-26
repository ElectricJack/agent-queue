"""The producer of ``ChildTaskCompleted`` — Package 4 child plan §4.5 step 5.

``tests/test_agent_task_executor.py`` proves what the engine does *with* a
child completion by handing it one.  Nothing in ``src/`` ever did: the cause
had no producer, so a suspended ``agent_task`` step resumed only when its
deadline expired.  Every test here therefore refuses to construct the cause.
The child is a real ``tasks`` row, it settles through the real
``transition_task`` (or a real delete), and the only thing that may turn that
into a resume is the scan under test.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import insert

from src.commands.principal import ExecutionPrincipal
from src.database import Database
from src.database.tables import playbook_artifacts
from src.models import Project, Task, TaskStatus
from src.playbooks.definition import PlaybookDefinition
from src.playbooks.engine import ChildTaskReconciler, PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.run_state import RunLifecycle
from tests.db_fixtures import lease_dsn
from tests.fixtures.contracts.engine_contracts import registry_with
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.playbook_v2_engine_helpers import StubActivations, artifact_ref_for
from tests.test_agent_task_executor import (
    CREATE_TASK,
    SOURCE,
    StubProfile,
    agent_task_artifact,
    agent_task_step,
    created,
    parent_principal,
)

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()
NOW = 1_000_000.0
CHILD = "child-3"


class ProfiledDatabase:
    """The real database, plus the one profile the executor resolves."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_profile(self, profile_id: str) -> StubProfile | None:
        return StubProfile(aq_commands=["a"]) if profile_id == "reviewer" else None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._database, name)


@pytest.fixture
async def db():
    database = Database(lease_dsn("child_task_reconciler"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    yield database
    await database.close()


async def seed_artifact(database: Database, ref: Any) -> None:
    """``playbook_v2_runs.artifact_sha256`` is a real FK on PostgreSQL."""
    async with database.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=ref.artifact_sha256,
                playbook_id=ref.playbook_id,
                scope="system",
                scope_identifier="",
                schema_generation=ref.schema_generation,
                version=ref.version,
                source_digest=ref.source_digest,
                contract_fingerprint=ref.contract_fingerprint,
                profile_fingerprint="",
                compiler_build=ref.compiler_build,
                path=f"artifacts/{ref.digest}.json",
                size_bytes=10,
                validation="{}",
                compiled_at=None,
                created_at=NOW,
            )
        )


async def child_task(database: Database, status: TaskStatus = TaskStatus.IN_PROGRESS) -> None:
    await database.create_task(
        Task(id=CHILD, project_id="p", title="Review", description="Review", status=status)
    )


async def engine_over(database: Database, artifact: PlaybookDefinition):
    ref = artifact_ref_for(artifact)
    await seed_artifact(database, ref)
    registry, adapter = registry_with(CREATE_TASK)

    class Store:
        def load(self, sha: str) -> PlaybookDefinition:
            return artifact

        def exists(self, sha: str) -> bool:
            return True

    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry,
            clock=lambda: NOW,
            artifact_store=Store(),
            db=ProfiledDatabase(database),
        ),
        runs=database,
        waits=database,
        activations=StubActivations([ref]),
    )
    return engine, ref, adapter


async def suspended_on_child(database: Database, **step_kwargs: Any):
    """A live run paused on ``CHILD``, which is a real, still-running task."""
    await child_task(database)
    engine, ref, adapter = await engine_over(
        database, agent_task_artifact(agent_task_step(**step_kwargs))
    )
    adapter.queue.append(created(CHILD))
    principal = parent_principal(aq_commands={"a"})
    outcome = await engine.run_rule(ref, "r", {"event_id": "e1"}, principal)
    assert outcome.lifecycle is RunLifecycle.PAUSED
    assert outcome.snapshot.wait.match == {"task_id": CHILD}
    reconciler = ChildTaskReconciler(
        engine, database, ExecutionPrincipal.service("playbook-child-task")
    )
    return reconciler, outcome.run_id, adapter


def delegate_transitions(receipts: list[Any]) -> list[str]:
    return [
        r.selected_transition for r in receipts if r.step_id == "delegate" and r.selected_transition
    ]


@pytest.mark.asyncio
async def test_a_running_child_resumes_nothing(db):
    reconciler, run_id, _ = await suspended_on_child(db)

    assert await db.settled_child_waits() == []
    assert await reconciler.tick() == ()
    assert (await db.load_run(run_id)).lifecycle is RunLifecycle.PAUSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "edge", "lifecycle"),
    [
        (TaskStatus.COMPLETED, "completed", RunLifecycle.COMPLETED),
        (TaskStatus.FAILED, "failed", RunLifecycle.FAILED),
        (TaskStatus.BLOCKED, "failed", RunLifecycle.FAILED),
    ],
)
async def test_a_real_task_transition_resumes_the_run(db, status, edge, lifecycle):
    reconciler, run_id, adapter = await suspended_on_child(db, save_result_as="review")

    await db.transition_task(CHILD, status, context="test", force=True)

    assert await reconciler.tick() == (run_id,)
    stored = await db.load_run(run_id)
    assert stored.lifecycle is lifecycle
    assert stored.wait is None
    assert await db.list_active(run_id) == []
    assert stored.bindings["review"] == {"task_id": CHILD, "status": status.value.lower()}
    assert delegate_transitions(await db.list_receipts(run_id)) == [f"r::delegate::{edge}"]
    assert adapter.names.count("create_task") == 1


@pytest.mark.asyncio
async def test_a_second_tick_delivers_nothing(db):
    reconciler, run_id, _ = await suspended_on_child(db)
    await db.transition_task(CHILD, TaskStatus.COMPLETED, context="test", force=True)

    assert await reconciler.tick() == (run_id,)
    receipts = len(await db.list_receipts(run_id))

    assert await reconciler.tick() == ()
    assert len(await db.list_receipts(run_id)) == receipts


@pytest.mark.asyncio
async def test_a_completion_missed_while_the_daemon_was_down_is_still_found(db):
    """The point of scanning durable state: there is no delivery to lose."""
    first, run_id, _ = await suspended_on_child(db)
    await db.transition_task(CHILD, TaskStatus.COMPLETED, context="test", force=True)
    del first

    # A fresh engine and a fresh reconciler: nothing survived but the rows.
    engine, _ref, adapter = await engine_over_existing(db)
    restarted = ChildTaskReconciler(engine, db, ExecutionPrincipal.service("playbook-child-task"))

    assert await restarted.tick() == (run_id,)
    assert (await db.load_run(run_id)).lifecycle is RunLifecycle.COMPLETED
    assert "create_task" not in adapter.names


async def engine_over_existing(database: Database):
    artifact = agent_task_artifact(agent_task_step())
    ref = artifact_ref_for(artifact)
    registry, adapter = registry_with(CREATE_TASK)

    class Store:
        def load(self, sha: str) -> PlaybookDefinition:
            return artifact

        def exists(self, sha: str) -> bool:
            return True

    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry,
            clock=lambda: NOW,
            artifact_store=Store(),
            db=ProfiledDatabase(database),
        ),
        runs=database,
        waits=database,
        activations=StubActivations([ref]),
    )
    return engine, ref, adapter


@pytest.mark.asyncio
async def test_a_deleted_child_takes_the_cancelled_edge(db):
    reconciler, run_id, _ = await suspended_on_child(db)

    await db.delete_task(CHILD)

    assert await reconciler.tick() == (run_id,)
    assert delegate_transitions(await db.list_receipts(run_id)) == ["r::delegate::cancelled"]


@pytest.mark.asyncio
async def test_a_task_wait_step_resumes_on_the_task_it_names(db):
    """``WaitStep(wait_kind="task")`` stores the same ``agent_task`` wait.

    ``resume`` sent every child completion to the ``AgentTaskStep`` reconciler,
    which refused this step as a duplicate — so the scan would have redelivered
    to it forever.
    """
    await child_task(db)
    artifact = PlaybookDefinition.model_validate(
        agent_task_artifact().model_dump(mode="json")
        | {
            "steps": {
                "delegate": {
                    "type": "wait",
                    "rule": "r",
                    "title": "Await the review",
                    "source": SOURCE,
                    "wait_kind": "task",
                    # ``awaited`` is the task ref; the correlation key says
                    # nothing about which task, as an author's need not.
                    "awaited": {"type": "literal", "value": CHILD},
                    "correlation_key": {
                        "type": "object",
                        "fields": {"review_of": {"type": "literal", "value": "spec-1"}},
                    },
                    "save_result_as": "review",
                    "transitions": {"completed": "done", "failed": "bad", "cancelled": "bad"},
                },
                **{
                    name: step
                    for name, step in agent_task_artifact().model_dump(mode="json")["steps"].items()
                    if name != "delegate"
                },
            }
        }
    )
    engine, ref, _ = await engine_over(db, artifact)
    outcome = await engine.run_rule(
        ref, "r", {"event_id": "e1"}, parent_principal(aq_commands={"a"})
    )
    assert outcome.lifecycle is RunLifecycle.PAUSED
    reconciler = ChildTaskReconciler(engine, db, ExecutionPrincipal.service("playbook-child-task"))

    await db.transition_task(CHILD, TaskStatus.COMPLETED, context="test", force=True)

    assert await reconciler.tick() == (outcome.run_id,)
    stored = await db.load_run(outcome.run_id)
    assert stored.lifecycle is RunLifecycle.COMPLETED
    assert stored.bindings["review"]["status"] == "completed"


# --------------------------------------------------------------------------
# Wiring — a producer nobody ticks is the bug this file exists for
# --------------------------------------------------------------------------


class _Config:
    class playbooks:  # mirrors ``config.playbooks.enabled``
        enabled = True


@pytest.mark.asyncio
async def test_the_orchestrator_cycle_step_delivers_a_real_completion(db):
    """``run_one_cycle`` → mixin step → ``CommandHandler`` → scan → resume."""
    from src.commands.playbook_commands import PlaybookCommandsMixin
    from src.orchestrator.monitoring import MonitoringMixin

    reconciler, run_id, _ = await suspended_on_child(db)
    engine = reconciler._engine

    class Handler(PlaybookCommandsMixin):
        def __init__(self) -> None:
            self.db = db

        def _v2_engine(self):
            return engine

    class Cycle(MonitoringMixin):
        config = _Config
        _command_handler = Handler()

    await db.transition_task(CHILD, TaskStatus.COMPLETED, context="test", force=True)
    await Cycle()._reconcile_playbook_child_tasks()

    assert (await db.load_run(run_id)).lifecycle is RunLifecycle.COMPLETED


def test_run_one_cycle_reconciles_children_before_it_expires_waits():
    """A child that settled in the same tick as its deadline is a completion."""
    import inspect

    from src.orchestrator.core import Orchestrator

    source = inspect.getsource(Orchestrator.run_one_cycle)
    reconcile = source.index("await self._reconcile_playbook_child_tasks()")
    expire = source.index("await self._check_paused_playbook_timeouts()")
    assert reconcile < expire
