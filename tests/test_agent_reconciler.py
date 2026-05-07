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


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
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
            created_at=_time.time(),
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
