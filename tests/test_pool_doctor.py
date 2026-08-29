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
        "claims.holder_consistency",
    }
    assert all(c.owner == "swarm-work-model" for c in pool_checks.CHECKS)


async def test_orphan_agent_detected_and_repaired(db):
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.IDLE))
    # ``create_agent`` always stamps ``created_at=time.time()`` server-side
    # regardless of the dataclass's own field -- backdate it past the
    # "still mid-launch" staleness window (2x prepare_timeout, default
    # config gives 240s) so this orphan is old enough to flag.
    await db.update_agent("a1", created_at=time.time() - 10_000)
    await db.create_workspace(
        Workspace(
            id="ws",
            project_id=PROJECT_ID,
            workspace_path="/w",
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
            locked_by_agent_id="a1",
        )
    )
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.data["count"] == 1
    await pool_checks.run_check(db, "pools.orphan_agents", config=None, repair=True)
    assert await db.get_agent("a1") is None
    assert (await db.get_workspace("ws")).locked_by_agent_id is None


async def test_fresh_orphan_agent_not_flagged(db):
    """A pool launch creates the agent row before the session row -- a
    freshly created agent with no session yet is a healthy in-flight
    launch, not an orphan (swarm-work-model §11's ``_launch_pool_session``
    ordering)."""
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.IDLE))
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.data.get("count", 0) == 0
    assert finding.severity is Severity.OK


async def test_old_orphan_agent_flagged_with_explicit_config(db):
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.swarm.prepare_timeout = 5
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.IDLE))
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
