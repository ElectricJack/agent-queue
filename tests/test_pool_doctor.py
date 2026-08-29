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

import time

import pytest

from src.doctor import pool_checks
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
    assert finding.fixable is False
    check = next(c for c in pool_checks.CHECKS if c.id == "claims.holder_consistency")
    assert check.fix is None
