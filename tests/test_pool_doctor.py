"""Doctor checks for pools and claims — spec §16.

Mirrors ``tests/test_hierarchy_checks.py``'s style but against the pool/claim
checks in ``src/doctor/pool_checks.py``.  The real ``CheckResult`` dataclass
(``src/doctor/models.py``) has no ``.count``/``.repairable`` attributes —
counts live in ``result.data["count"]`` and repairability is
``result.fixable`` (set on the check itself, or by ``run_check(repair=True)``
after a fix runs) — so assertions below read those instead of the brief's
placeholder names.
"""

from __future__ import annotations

import sys
import time

import pytest

import src.doctor  # noqa: F401 -- side effect: populates sys.modules
from src.doctor.models import Severity
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)

# ``src/doctor/__init__.py`` does ``from src.doctor.pool_checks import
# pool_checks`` (the factory function) to build ``default_registry()`` --
# symmetric with how it imports ``hierarchy_checks``. That rebinds the
# package's own ``pool_checks`` attribute to the *function*, so any
# ``import src.doctor.pool_checks`` (plain or ``as``) resolves through that
# now-shadowed attribute and hands back the function, not the submodule.
# ``sys.modules`` is unaffected by that rebinding -- ``import src.doctor``
# above (which pulls the submodule in as a side effect) then pulling the
# real submodule out of the module cache is the only way to reach ``.CHECKS``
# / ``.run_check`` unambiguously.
pool_checks = sys.modules["src.doctor.pool_checks"]

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    from src.database import Database

    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(AgentProfile(id="worker", name="w", lifecycle="pool"))
    yield database
    await database.close()


def test_check_names():
    names = {c.id for c in pool_checks.CHECKS}
    assert names == {
        "pools.stuck",
        "pools.orphan_agents",
        "pools.preparing_stuck",
        "pools.disabled",
        "claims.holder_consistency",
    }
    assert all(c.owner == "swarm-work-model" for c in pool_checks.CHECKS)


async def _stale_agent(db, agent_id, **kw):
    """A pool-profile agent old enough to be past the in-flight-launch window.

    ``create_agent`` always stamps ``created_at=time.time()`` server-side
    regardless of the dataclass's own field, so the backdate is a second
    write.
    """
    kw.setdefault("state", AgentState.IDLE)
    kw.setdefault("profile_id", "worker")
    await db.create_agent(Agent(id=agent_id, name=agent_id, **kw))
    await db.update_agent(agent_id, created_at=time.time() - 10_000)


async def _lock_workspace_to(db, agent_id, ws_id="ws"):
    await db.create_workspace(
        Workspace(
            id=ws_id,
            project_id=PROJECT_ID,
            workspace_path="/w",
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
            locked_by_agent_id=agent_id,
        )
    )


async def test_leaked_workspace_lock_released_and_agent_kept_reusable(db):
    """A rolled-back launch leaves the lock behind, not a bad definition.

    The row itself is a perfectly good idle worker the next
    ``_launch_pool_session`` will reuse -- only the workspace it never gave
    back is wrong, so the fix releases the lock and leaves the row IDLE.
    """
    await _stale_agent(db, "a1")
    await _lock_workspace_to(db, "a1")

    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.data["count"] == 1
    assert finding.data["leaked_workspace"] == ["a1"]
    assert finding.severity is Severity.WARN

    repaired = await pool_checks.run_check(db, "pools.orphan_agents", config=None, repair=True)
    assert repaired.severity is Severity.OK
    assert (await db.get_workspace("ws")).locked_by_agent_id is None
    agent = await db.get_agent("a1")
    assert agent is not None and agent.state is AgentState.IDLE


async def test_idle_spare_is_not_an_orphan(db):
    """An idle, unowned, lock-free pool definition is the reuse pool.

    Flagging it was the false positive that made ``pools.orphan_agents``
    fire on every healthy install with a pool profile.
    """
    await _stale_agent(db, "a1")
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.severity is Severity.OK
    assert finding.data["count"] == 0
    assert finding.data["spares"] == ["a1"]


async def test_busy_orphan_is_reported_but_never_touched(db):
    """The push-agent row for a profile that has since become ``lifecycle: pool``.

    No pool session will ever adopt it, but it may still own a task, so
    ``--fix`` must leave both the state and the row alone.
    """
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await _stale_agent(db, "a1", state=AgentState.BUSY, current_task_id="t1")

    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.severity is Severity.ERROR
    assert finding.data["stranded"] == ["a1"]

    # ``run_check(repair=True)`` re-runs the check afterwards, so the
    # post-fix result still reports the same stranded row: the fix declined
    # to touch it, which is the point.
    repaired = await pool_checks.run_check(db, "pools.orphan_agents", config=None, repair=True)
    assert repaired.data["stranded"] == ["a1"]
    assert repaired.severity is Severity.ERROR
    agent = await db.get_agent("a1")
    assert agent is not None
    assert (agent.state, agent.current_task_id) == (AgentState.BUSY, "t1")


async def test_unusable_orphan_is_retired_not_deleted(db):
    await _stale_agent(db, "a1", state=AgentState.ERROR)
    await _lock_workspace_to(db, "a1")

    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.data["retirable"] == ["a1"]

    await pool_checks.run_check(db, "pools.orphan_agents", config=None, repair=True)
    agent = await db.get_agent("a1")
    # Retired, not deleted: the ledger keeps its reference and the row keeps
    # its history.
    assert agent is not None and agent.state is AgentState.RETIRED
    assert (await db.get_workspace("ws")).locked_by_agent_id is None
    events = await db.get_recent_events(event_type="pool.agent_repaired")
    assert len(events) == 1 and "a1" in events[0]["payload"]


async def test_fresh_orphan_agent_not_flagged(db):
    """A pool launch creates the agent row before the session row -- a
    freshly created agent with no session yet is a healthy in-flight
    launch, not an orphan (swarm-work-model §11's ``_launch_pool_session``
    ordering)."""
    await db.create_agent(
        Agent(id="a1", name="a1", profile_id="worker", state=AgentState.ERROR)
    )
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.data.get("count", 0) == 0
    assert finding.severity is Severity.OK


async def test_old_orphan_agent_flagged_with_explicit_config(db):
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.swarm.prepare_timeout = 5
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.ERROR))
    await db.update_agent("a1", created_at=time.time() - (2 * cfg.swarm.prepare_timeout) - 1)
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=cfg)
    assert finding.data["count"] == 1


async def test_stuck_pool_session_released(db):
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.create_session(
        SessionRecord(
            id="s1",
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name="s1",
            lifecycle="pool",
            work_dir="/w",
            epoch="e",
            instance_token="t",
            started_at=time.time(),
            state="running",
            agent_id="a1",
            task_id="t1",
            claim_phase="active",
        )
    )
    finding = await pool_checks.run_check(db, "pools.stuck", config=None)
    assert finding.data["count"] == 1
    assert finding.severity is Severity.ERROR
    await pool_checks.run_check(db, "pools.stuck", config=None, repair=True)
    assert (await db.get_session("s1")).task_id is None
    assert (await db.get_agent("a1")).state == AgentState.IDLE


async def test_preparing_stuck_detected_and_repaired(db):
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.swarm.prepare_timeout = 5
    stale_at = time.time() - (2 * cfg.swarm.prepare_timeout) - 1
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(
        Task(id="t1", project_id=PROJECT_ID, title="t", description="d", status=TaskStatus.READY)
    )
    await db.create_session(
        SessionRecord(
            id="s1",
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name="s1",
            lifecycle="pool",
            work_dir="/w",
            epoch="e",
            instance_token="t",
            started_at=time.time(),
            state="running",
            agent_id="a1",
            task_id="t1",
            claim_phase="preparing",
            claim_phase_at=stale_at,
        )
    )
    finding = await pool_checks.run_check(db, "pools.preparing_stuck", config=cfg)
    assert finding.data["count"] == 1
    await pool_checks.run_check(db, "pools.preparing_stuck", config=cfg, repair=True)
    assert (await db.get_session("s1")).claim_phase is None


async def test_holder_consistency_reports_mismatch(db):
    await db.create_task(
        Task(
            id="other",
            project_id=PROJECT_ID,
            title="other",
            description="d",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await db.create_agent(
        Agent(
            id="a1", name="a1", profile_id="worker", state=AgentState.BUSY, current_task_id="other"
        )
    )
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a1",
        )
    )
    finding = await pool_checks.run_check(db, "claims.holder_consistency", config=None)
    assert finding.data["count"] == 1
    check = next(c for c in pool_checks.CHECKS if c.id == "claims.holder_consistency")
    assert check.fix is None


async def test_holder_consistency_flags_duplicate_session_holders(db):
    """Two sessions both pointing at the same task's ``task_id`` is exactly
    the anomaly this check exists to catch -- ``get_session_for_task``
    would silently rank-pick one and hide it, so the check must count
    every session with a matching ``task_id`` rather than ask for "the"
    one."""
    # Circular FK (agent.current_task_id -> t1, t1.assigned_agent_id -> a1):
    # create the agent with no task yet, then the task (agent already
    # exists to satisfy its FK), then point the agent at it.
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a1",
        )
    )
    await db.update_agent("a1", current_task_id="t1")
    await db.set_task_meta("t1", "claimed_by_session", "s1")
    for sid in ("s1", "s2"):
        await db.create_session(
            SessionRecord(
                id=sid,
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name=sid,
                lifecycle="pool",
                work_dir="/w",
                epoch="e",
                instance_token="t",
                started_at=time.time(),
                state="running",
                agent_id="a1",
                task_id="t1",
                claim_phase="active",
            )
        )
    finding = await pool_checks.run_check(db, "claims.holder_consistency", config=None)
    assert finding.data["count"] == 1


async def test_holder_consistency_ignores_push_launched_holder(db):
    """A ``lifecycle: task`` session holding its task is healthy (I1).

    ``claimed_by_session`` is written only by ``record_holder`` on the claim
    path, so a push-launched session never has one -- which used to make
    this check WARN on every healthy push-launched task.
    """
    await db.create_profile(AgentProfile(id="pusher", name="p", lifecycle="task"))
    await db.create_agent(Agent(id="a1", name="a1", profile_id="pusher", state=AgentState.BUSY))
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a1",
        )
    )
    await db.update_agent("a1", current_task_id="t1")
    await db.create_session(
        SessionRecord(
            id="s1",
            project_id=PROJECT_ID,
            profile_id="pusher",
            harness="claude",
            provider="fake",
            name="s1",
            lifecycle="task",
            work_dir="/w",
            epoch="e",
            instance_token="t",
            started_at=time.time(),
            state="running",
            agent_id="a1",
            task_id="t1",
        )
    )
    finding = await pool_checks.run_check(db, "claims.holder_consistency", config=None)
    assert finding.severity is Severity.OK
    assert finding.data.get("count", 0) == 0


async def test_orphan_agents_sees_project_scoped_pool_profile(db):
    """Project-scoped profile ids are ``<project>:<agent-type>`` (M2).

    ``agents.profile_id`` holds the bare agent-type id, so the profile id
    has to be normalised the same way ``core.py``/``pools.py`` do or the
    check never matches a project-scoped pool agent.
    """
    from src.config import AppConfig

    await db.create_profile(
        AgentProfile(id=f"{PROJECT_ID}:scoped", name="scoped", lifecycle="pool")
    )
    cfg = AppConfig()
    cfg.swarm.prepare_timeout = 5
    await db.create_agent(Agent(id="a9", name="a9", profile_id="scoped", state=AgentState.ERROR))
    await db.update_agent("a9", created_at=time.time() - (2 * cfg.swarm.prepare_timeout) - 1)
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=cfg)
    assert finding.data["count"] == 1
    assert finding.data["retirable"] == ["a9"]


async def test_pools_disabled_warns_when_flag_off(db):
    """I5 / ruling P2-17: pool profiles + ``swarm.enabled=False`` strands work."""
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.swarm.enabled = False
    finding = await pool_checks.run_check(db, "pools.disabled", config=cfg)
    assert finding.severity is Severity.WARN
    assert finding.data["count"] == 1
    assert "worker" in finding.data["profiles"]
    check = next(c for c in pool_checks.CHECKS if c.id == "pools.disabled")
    assert check.fix is None


async def test_pools_disabled_ok_when_flag_on(db):
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.swarm.enabled = True
    finding = await pool_checks.run_check(db, "pools.disabled", config=cfg)
    assert finding.severity is Severity.OK
