"""Unit tests for AgentReconciler — see
docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md §7.
"""
from __future__ import annotations

import time as _time

import pytest

from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    ProjectStatus,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator.agent_reconciler import AgentReconciler
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    yield database
    await database.close()


# ── Test helpers ──────────────────────────────────────────────────────


async def _seed_project_with_profile(
    db,
    *,
    project_id,
    profile_id,
    max_agents=2,
    runtime="claude_sdk",
    workspace_count=1,
):
    """Create a project with a default profile and N enabled workspaces."""
    await db.create_profile(AgentProfile(
        id=profile_id, name=profile_id, runtime=runtime,
    ))
    await db.create_project(Project(
        id=project_id, name=project_id,
        default_profile_id=profile_id,
        max_concurrent_agents=max_agents,
        status=ProjectStatus.ACTIVE,
        credit_weight=1.0, total_tokens_used=0,
    ))
    for i in range(workspace_count):
        await db.create_workspace(Workspace(
            id=f"ws-{project_id}-{i}", project_id=project_id,
            workspace_path=f"/tmp/{project_id}-{i}",
            source_type=RepoSourceType.LINK, enabled=True,
        ))


async def _seed_ready_task(db, *, task_id, project_id, profile_id=None, priority=100):
    """Create a READY task with optional explicit profile_id."""
    await db.create_task(Task(
        id=task_id, project_id=project_id,
        title=task_id, description=task_id,
        status=TaskStatus.READY, priority=priority,
        profile_id=profile_id,
        created_at=_time.time(), updated_at=_time.time(),
    ))


# ── Tests ─────────────────────────────────────────────────────────────


async def test_no_op_when_no_projects(db):
    reconciler = AgentReconciler(db)
    report = await reconciler.reconcile()
    assert report.created == []
    assert report.reassigned == []
    assert report.skipped == []


async def test_creates_one_agent_for_one_ready_task(db):
    await _seed_project_with_profile(db, project_id="p", profile_id="claude-opus")
    await _seed_ready_task(db, task_id="t-1", project_id="p")

    report = await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    assert len(agents) == 1
    assert agents[0].profile_id == "claude-opus"
    assert agents[0].state == AgentState.IDLE
    assert report.created == [("p", "claude-opus")]


async def _seed_bare_project(db, *, project_id="p", max_agents=1):
    """A project with a workspace but no default_profile_id."""
    await db.create_project(Project(
        id=project_id, name=project_id,
        max_concurrent_agents=max_agents,
        status=ProjectStatus.ACTIVE,
        credit_weight=1.0, total_tokens_used=0,
    ))
    await db.create_workspace(Workspace(
        id=f"ws-{project_id}-0", project_id=project_id,
        workspace_path=f"/tmp/{project_id}-0",
        source_type=RepoSourceType.LINK, enabled=True,
    ))


async def test_skips_when_no_profiles_registered_at_all(db, caplog):
    """No default, no task profile, and an empty agent_profiles table →
    nothing to fall back to, so skip and warn once per project."""
    import logging

    caplog.set_level(logging.WARNING)
    await _seed_bare_project(db)
    await _seed_ready_task(db, task_id="t", project_id="p")

    rec = AgentReconciler(db)
    report = await rec.reconcile()
    agents = await db.list_agents()
    assert len(agents) == 0
    assert len(report.skipped) == 1
    assert report.skipped[0][0] == "p"
    assert "no resolvable profile_id" in report.skipped[0][1]
    assert any("no resolvable profile_id" in r.message for r in caplog.records)

    # Dedup: second reconcile pass on the same instance should not re-log.
    caplog.clear()
    await rec.reconcile()
    assert not any("no resolvable profile_id" in r.message for r in caplog.records)


async def test_backfills_project_default_and_creates_agent(db):
    """The regression this guards: a project with READY tasks, no
    default_profile_id, and tasks carrying no profile_id used to stall
    forever.  Now the system default is picked, persisted, and an agent
    is built from it."""
    await _seed_bare_project(db)
    for pid in ("claude-sonnet", "claude-opus", "reviewer"):
        await db.create_profile(AgentProfile(id=pid, name=pid, runtime="claude_sdk"))
    await _seed_ready_task(db, task_id="t", project_id="p")

    report = await AgentReconciler(db).reconcile()

    assert report.defaults_backfilled == [("p", "claude-opus")]
    assert report.skipped == []
    # Persisted, so Orchestrator._resolve_profile agrees with the agent row.
    project = await db.get_project("p")
    assert project.default_profile_id == "claude-opus"
    agents = await db.list_agents()
    assert len(agents) == 1
    assert agents[0].profile_id == "claude-opus"


async def test_backfill_is_idempotent_across_ticks(db):
    """A second pass must not re-pick or re-log — the persisted default
    short-circuits the fallback."""
    await _seed_bare_project(db)
    await db.create_profile(AgentProfile(
        id="claude-opus", name="claude-opus", runtime="claude_sdk",
    ))
    await _seed_ready_task(db, task_id="t", project_id="p")

    rec = AgentReconciler(db)
    await rec.reconcile()
    second = await rec.reconcile()

    assert second.defaults_backfilled == []
    assert second.created == []
    assert len(await db.list_agents()) == 1


async def test_backfill_skipped_when_all_tasks_carry_explicit_profile(db):
    """An explicit task profile_id already resolves — don't stamp a
    default the operator never asked for."""
    await _seed_bare_project(db)
    await db.create_profile(AgentProfile(
        id="claude-opus", name="claude-opus", runtime="claude_sdk",
    ))
    await _seed_ready_task(db, task_id="t", project_id="p", profile_id="claude-opus")

    report = await AgentReconciler(db).reconcile()

    assert report.defaults_backfilled == []
    assert (await db.get_project("p")).default_profile_id is None
    assert [a.profile_id for a in await db.list_agents()] == ["claude-opus"]


async def test_creates_one_agent_per_profile_under_cap(db):
    """Two ready tasks with distinct profiles, capacity=2 → one agent per profile."""
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=2
    )
    await db.create_profile(AgentProfile(
        id="claude-sonnet", name="claude-sonnet", runtime="claude_sdk",
    ))
    await _seed_ready_task(db, task_id="t-opus", project_id="p", profile_id="claude-opus")
    await _seed_ready_task(db, task_id="t-son", project_id="p", profile_id="claude-sonnet")

    await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    assert len(agents) == 2
    assert {a.profile_id for a in agents} == {"claude-opus", "claude-sonnet"}


async def test_preserves_default_profile_at_cap(db):
    """Task specialization must not rewrite a durable worker default."""
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=1
    )
    await db.create_profile(AgentProfile(
        id="claude-sonnet", name="claude-sonnet", runtime="claude_sdk",
    ))
    # Pre-existing idle opus agent locked to the project's only workspace.
    await db.create_agent(Agent(
        id="agent-1", name="opus-1", profile_id="claude-opus",
        state=AgentState.IDLE,
    ))
    workspaces = await db.list_workspaces()
    await db.update_workspace(workspaces[0].id, locked_by_agent_id="agent-1")
    # New ready task needing sonnet.
    await _seed_ready_task(db, task_id="t-son", project_id="p", profile_id="claude-sonnet")

    report = await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    assert len(agents) == 1
    assert agents[0].profile_id == "claude-opus"
    assert report.reassigned == []
    assert report.created == []


async def test_multiple_task_profiles_keep_one_durable_worker(db):
    """Several task profiles share one worker without overwriting its definition."""
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=1
    )
    for pid in ("claude-sonnet", "claude-haiku", "claude-mini"):
        await db.create_profile(AgentProfile(
            id=pid, name=pid, runtime="claude_sdk",
        ))
    await db.create_agent(Agent(
        id="agent-1", name="opus-1", profile_id="claude-opus",
        state=AgentState.IDLE,
    ))
    workspaces = await db.list_workspaces()
    await db.update_workspace(workspaces[0].id, locked_by_agent_id="agent-1")
    for pid in ("claude-sonnet", "claude-haiku", "claude-mini"):
        await _seed_ready_task(
            db, task_id=f"t-{pid}", project_id="p", profile_id=pid
        )

    report = await AgentReconciler(db).reconcile()

    assert report.reassigned == []
    assert report.created == []
    assert (await db.get_agent("agent-1")).profile_id == "claude-opus"


async def test_workspace_required_no_workspace_no_create(db):
    """Profile.runtime requires workspace + 0 available workspaces → no create."""
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus",
        runtime="claude_sdk", workspace_count=0,
    )
    await _seed_ready_task(db, task_id="t-1", project_id="p")

    report = await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    assert len(agents) == 0
    assert any("no available workspace" in s[1] for s in report.skipped)


async def test_supervisor_demand_does_not_create_duplicate_global_worker(db):
    """The canonical named supervisor is seeded separately from task supply.

    With the in-process Supervisor runtime deleted, the supervisor profile is
    an ordinary tmux agent: the workspace gate applies to it like any other
    profile, and task supply never creates a duplicate global worker.
    """
    await _seed_project_with_profile(
        db, project_id="p", profile_id="supervisor",
        workspace_count=0,
    )
    await _seed_ready_task(db, task_id="t-1", project_id="p")

    await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    assert agents == []


async def test_orphan_busy_reset_to_idle(db):
    """Agent state=BUSY but current_task_id missing → reset to IDLE before counting."""
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=1
    )
    # Pre-existing BUSY agent with NULL current_task_id (orphan).
    await db.create_agent(Agent(
        id="agent-1", name="opus-1", profile_id="claude-opus",
        state=AgentState.BUSY, current_task_id=None,
    ))
    workspaces = await db.list_workspaces()
    await db.update_workspace(workspaces[0].id, locked_by_agent_id="agent-1")
    await _seed_ready_task(db, task_id="t-1", project_id="p")

    await AgentReconciler(db).reconcile()

    agents = await db.list_agents()
    # Should be reset to IDLE — no new agent needed since the existing one
    # has the right profile.
    assert len(agents) == 1
    assert agents[0].state == AgentState.IDLE


class _Bus:
    """Records emitted events so resets can be asserted on."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, payload=None):
        self.events.append((event_type, dict(payload or {})))


async def _seed_busy_agent_on_task(db, *, agent_id, profile_id, task_id, task_status):
    """A BUSY agent whose current_task_id points at a real task in *task_status*,
    with no live session for either the agent or the task."""
    await db.create_task(Task(
        id=task_id, project_id="p",
        title=task_id, description=task_id,
        status=task_status, priority=100,
        created_at=_time.time(), updated_at=_time.time(),
    ))
    await db.create_agent(Agent(
        id=agent_id, name=agent_id, profile_id=profile_id,
        state=AgentState.BUSY, current_task_id=task_id,
    ))


async def test_busy_agent_on_completed_task_is_reset_and_announced(db):
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=1, workspace_count=0,
    )
    await _seed_busy_agent_on_task(
        db, agent_id="agent-1", profile_id="claude-opus",
        task_id="t-done", task_status=TaskStatus.COMPLETED,
    )
    before = await db.get_task("t-done")

    bus = _Bus()
    report = await AgentReconciler(db, bus=bus).reconcile()

    agents = await db.list_agents()
    assert len(agents) == 1
    assert agents[0].state == AgentState.IDLE
    assert agents[0].current_task_id is None
    after = await db.get_task("t-done")
    assert after.assigned_agent_id == before.assigned_agent_id
    assert [t for t, _ in bus.events] == ["agent.updated"]
    assert bus.events[0][1]["agent_id"] == "agent-1"
    assert report.created == []


async def test_busy_agent_on_blocked_task_is_reset(db):
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=1, workspace_count=0,
    )
    await _seed_busy_agent_on_task(
        db, agent_id="agent-1", profile_id="claude-opus",
        task_id="t-blocked", task_status=TaskStatus.BLOCKED,
    )

    bus = _Bus()
    await AgentReconciler(db, bus=bus).reconcile()

    agents = await db.list_agents()
    assert agents[0].state == AgentState.IDLE
    assert agents[0].current_task_id is None
    assert [t for t, _ in bus.events] == ["agent.updated"]


async def test_busy_agent_on_in_progress_task_is_untouched(db):
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=1, workspace_count=0,
    )
    await _seed_busy_agent_on_task(
        db, agent_id="agent-1", profile_id="claude-opus",
        task_id="t-running", task_status=TaskStatus.IN_PROGRESS,
    )

    bus = _Bus()
    await AgentReconciler(db, bus=bus).reconcile()

    agents = await db.list_agents()
    assert agents[0].state == AgentState.BUSY
    assert agents[0].current_task_id == "t-running"
    assert bus.events == []


async def test_busy_agent_with_live_session_on_completed_task_is_untouched(db):
    from src.models import SessionRecord

    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=1, workspace_count=0,
    )
    await _seed_busy_agent_on_task(
        db, agent_id="agent-1", profile_id="claude-opus",
        task_id="t-done", task_status=TaskStatus.COMPLETED,
    )
    await db.create_session(SessionRecord(
        id="sess-1", project_id="p", profile_id="claude-opus",
        harness="claude", provider="tmux", name="opus-1", lifecycle="task",
        state="running", work_dir="/tmp", epoch="e", instance_token="tok",
        started_at=_time.time(), task_id="t-done", agent_id="agent-1",
    ))

    bus = _Bus()
    await AgentReconciler(db, bus=bus).reconcile()

    agents = await db.list_agents()
    assert agents[0].state == AgentState.BUSY
    assert agents[0].current_task_id == "t-done"
    assert bus.events == []


async def test_reconcile_reads_current_tasks_by_id_not_the_table(db, monkeypatch):
    """Each tick reads the agents' current tasks and, without a caller-supplied
    ready list, the READY rows -- never every task (wise-ember.16)."""
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=1,
    )
    await _seed_busy_agent_on_task(
        db, agent_id="agent-1", profile_id="claude-opus",
        task_id="t-running", task_status=TaskStatus.IN_PROGRESS,
    )
    await _seed_ready_task(db, task_id="t-ready", project_id="p")
    for index in range(3):
        await db.create_task(Task(
            id=f"t-done-{index}", project_id="p", title="done", description="done",
            status=TaskStatus.COMPLETED, created_at=_time.time(), updated_at=_time.time(),
        ))
    reads = []
    original = db.list_tasks

    async def spy(*args, **kwargs):
        rows = await original(*args, **kwargs)
        reads.append((args, kwargs, sorted(task.id for task in rows)))
        return rows

    monkeypatch.setattr(db, "list_tasks", spy)

    report = await AgentReconciler(db).reconcile()

    # The busy agent's IN_PROGRESS task still fills the project's only slot.
    assert report.created == []
    assert [a.id for a in await db.list_agents()] == ["agent-1"]
    assert reads == [
        ((), {"task_ids": {"t-running"}}, ["t-running"]),
        ((), {"status": TaskStatus.READY}, ["t-ready"]),
    ]

    reads.clear()
    await AgentReconciler(db).reconcile(ready_tasks=[])
    assert reads == [((), {"task_ids": {"t-running"}}, ["t-running"])]


async def test_missing_profile_keeps_durable_definition(db):
    """A missing profile never justifies rewriting a saved worker definition."""
    await _seed_project_with_profile(
        db, project_id="p", profile_id="claude-opus", max_agents=2
    )
    await db.create_profile(AgentProfile(
        id="claude-sonnet", name="claude-sonnet", runtime="claude_sdk",
    ))
    # 2 idle agents, one with valid profile, one with orphan profile.
    await db.create_agent(Agent(
        id="agent-valid", name="valid", profile_id="claude-opus",
        state=AgentState.IDLE,
    ))
    await db.create_agent(Agent(
        id="agent-orphan", name="orphan", profile_id="deleted-profile",
        state=AgentState.IDLE,
    ))
    # Lock both workspaces (need 2 for cap=2 attribution).
    await db.create_workspace(Workspace(
        id="ws-p-1", project_id="p",
        workspace_path="/tmp/p-1",
        source_type=RepoSourceType.LINK, enabled=True,
    ))
    workspaces = await db.list_workspaces()
    await db.update_workspace(workspaces[0].id, locked_by_agent_id="agent-valid")
    await db.update_workspace(workspaces[1].id, locked_by_agent_id="agent-orphan")
    # Task specialization may use the valid worker without changing either definition.
    await _seed_ready_task(db, task_id="t-son", project_id="p", profile_id="claude-sonnet")

    report = await AgentReconciler(db).reconcile()

    # Keep both valid and orphaned definitions unchanged.
    valid = await db.get_agent("agent-valid")
    orphan = await db.get_agent("agent-orphan")
    assert valid.profile_id == "claude-opus"
    assert orphan.profile_id == "deleted-profile"
    assert report.reassigned == []
