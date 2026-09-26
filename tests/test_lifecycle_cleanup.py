"""Lifecycle cleanup: stale containers, stale-open tasks, obsolete closes, doctor.

On 2026-09-26 finished and obsolete work lingered open for 10-13 hours.  Two
epics whose children were all delivered stayed BLOCKED (sharp-impact) and
PAUSED (agile-torrent) because settlement only moved IN_PROGRESS containers.
Two superseded tasks (solid-cascade, crisp-apex) could not be deleted after
closing: each still held a branch-owner row and sat in a development batch.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import insert, select, update

from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.database.tables import development_deliveries, events, projects, tasks
from src.models import (
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskCompletion,
    TaskStatus,
)
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"
DEV_PROJECT = "p-dev"
SHA = "c9fe0f80500894c94ecefd2b7dd92d5928e034db"


@pytest.fixture
async def db():
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=lease_dsn("test.db")),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def orch(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    o.bus = MagicMock()
    o.bus.emit = AsyncMock()
    o._emit_text_notify = AsyncMock()
    o._check_workflow_stage_completion = AsyncMock()
    o.register_settlement_listener()
    return o


async def mktask(db, tid, status=TaskStatus.DEFINED, *, project_id=PROJECT_ID, **kw):
    await db.create_task(
        Task(id=tid, project_id=project_id, title=tid, description=tid, status=status, **kw)
    )


async def epic(db, *, status, kids=2, project_id=PROJECT_ID, **kid_kw):
    """An epic container ``e`` with *kids* READY children, then moved to *status*."""
    await mktask(db, "e", status=TaskStatus.IN_PROGRESS, project_id=project_id)
    names = []
    for i in range(kids):
        await mktask(db, f"e.{i}", status=TaskStatus.READY, project_id=project_id, **kid_kw)
        await db.add_dependency(f"e.{i}", "e", "parent-child")
        names.append(f"e.{i}")
    if status == TaskStatus.PAUSED:
        await db.pause_task("e")
    elif status != TaskStatus.IN_PROGRESS:
        await db.transition_task("e", status, context="restart_recovery", force=True)
    return names


async def dev_project(db) -> None:
    """A development-mode project that delivers to ``dev-repo``'s ``main``."""
    await db.create_project(Project(id=DEV_PROJECT, name="Development"))
    await db.create_repo(
        RepoConfig(
            id="dev-repo", project_id=DEV_PROJECT, source_type=RepoSourceType.CLONE,
            url="https://example.test/dev.git",
        )
    )
    async with db._engine.begin() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == DEV_PROJECT)
            .values(integration_repository_id="dev-repo", hierarchical_integration_mode="development")
        )


async def close_with_commit(db, tid, sha=SHA):
    await db.save_task_completion(
        TaskCompletion(
            id=f"close-{tid}", task_id=tid, outcome="pass", commits=[sha],
            completed_at=time.time(),
        )
    )
    await db.transition_task(tid, TaskStatus.COMPLETED)


async def delivery(db, row_id, *, state, members, target_ref="refs/heads/main"):
    now = time.time()
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(development_deliveries).values(
                id=row_id, project_id=DEV_PROJECT, repository_id="dev-repo",
                target_ref=target_ref, expected_sha=None, prepared_sha=None, state=state,
                manifest=[{"task_id": tid, "source_sha": sha} for tid, sha in members],
                evidence={}, reason="development batch", created_at=now, updated_at=now,
            )
        )


async def status(db, tid):
    return (await db.get_task(tid)).status


# ---------------------------------------------------------------------------
# 1. Stale BLOCKED/PAUSED containers settle once their children are delivered
# ---------------------------------------------------------------------------


class TestStaleContainerSettlement:
    async def test_blocked_epic_completes_when_its_last_child_completes(self, db):
        kids = await epic(db, status=TaskStatus.BLOCKED)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert await status(db, "e") == TaskStatus.BLOCKED
        await db.transition_task(kids[1], TaskStatus.COMPLETED)
        assert await status(db, "e") == TaskStatus.COMPLETED

    async def test_operator_paused_epic_completes_and_its_hold_is_cleared(self, db):
        kids = await epic(db, status=TaskStatus.PAUSED, kids=1)
        assert await db.get_task_meta("e", "manual_pause") is not None
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert await status(db, "e") == TaskStatus.COMPLETED
        assert await db.get_task_meta("e", "manual_pause") is None
        async with db._engine.connect() as conn:
            audit = (
                await conn.execute(
                    select(events.c.payload).where(
                        events.c.task_id == "e",
                        events.c.event_type == "task.stale_container_settled",
                    )
                )
            ).scalars().all()
        assert len(audit) == 1 and '"from_status": "PAUSED"' in audit[0]

    async def test_failed_child_keeps_the_epic_open(self, db):
        kids = await epic(db, status=TaskStatus.BLOCKED)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        await db.transition_task(kids[1], TaskStatus.FAILED)
        assert await status(db, "e") == TaskStatus.BLOCKED
        assert "e" not in await db.stale_container_candidates()

    async def test_childless_blocked_container_is_left_alone(self, db):
        await mktask(db, "lonely", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            await db.mark_container("lonely", conn=conn)
        await db.transition_task("lonely", TaskStatus.BLOCKED, force=True)
        assert "lonely" not in await db.stale_container_candidates()

    async def test_live_session_keeps_a_stale_container_open(self, db):
        kids = await epic(db, status=TaskStatus.BLOCKED, kids=1)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1", task_id="e", project_id=PROJECT_ID, profile_id="worker",
                harness="claude", provider="fake", name="s-e", lifecycle="task",
                state="running", work_dir="/tmp", epoch="e", instance_token="t",
                started_at=now, last_activity=now,
            )
        )
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert await status(db, "e") == TaskStatus.BLOCKED

    async def test_stale_grandparent_settles_through_an_in_progress_parent(self, db):
        await mktask(db, "g", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "g.1", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "g.1.1", status=TaskStatus.READY)
        await db.add_dependency("g.1", "g", "parent-child")
        await db.add_dependency("g.1.1", "g.1", "parent-child")
        await db.transition_task("g", TaskStatus.BLOCKED, force=True)
        await db.transition_task("g.1.1", TaskStatus.COMPLETED)
        assert await status(db, "g.1") == TaskStatus.COMPLETED
        assert await status(db, "g") == TaskStatus.COMPLETED

    async def test_undelivered_child_waits_for_delivery_then_the_sweep_settles(self, db, orch):
        await dev_project(db)
        kids = await epic(
            db, status=TaskStatus.BLOCKED, kids=1, project_id=DEV_PROJECT,
            repo_id="dev-repo", branch_name="aq/e.0",
        )
        await close_with_commit(db, kids[0])
        # COMPLETED is not delivered: the publisher has not landed it on main.
        assert await status(db, "e") == TaskStatus.BLOCKED
        assert await orch.reconcile_stale_containers() == []

        await delivery(db, "batch-1", state="delivered", members=[(kids[0], SHA)])

        orch._last_container_sweep = 0.0
        await orch._sweep_container_completion()
        assert await status(db, "e") == TaskStatus.COMPLETED
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert "task.completed" in emitted

    async def test_delivery_to_another_ref_is_not_delivery(self, db, orch):
        await dev_project(db)
        kids = await epic(
            db, status=TaskStatus.PAUSED, kids=1, project_id=DEV_PROJECT,
            repo_id="dev-repo", branch_name="aq/e.0",
        )
        await close_with_commit(db, kids[0])
        await delivery(
            db, "batch-x", state="delivered", members=[(kids[0], SHA)],
            target_ref="refs/heads/release",
        )
        assert await orch.reconcile_stale_containers() == []
        assert await status(db, "e") == TaskStatus.PAUSED

    async def test_startup_reconciliation_settles_a_container_stranded_by_a_restart(
        self, db, orch
    ):
        kids = await epic(db, status=TaskStatus.BLOCKED, kids=1)
        # The child finished while the daemon was down: its status landed
        # without the transition that would have seeded the epic.
        async with db._engine.begin() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == kids[0]).values(status="COMPLETED")
            )
        assert await status(db, "e") == TaskStatus.BLOCKED
        assert await orch.reconcile_stale_containers() == ["e"]
        assert await status(db, "e") == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 2. Stale BLOCKED/PAUSED tasks are re-checked: unblocked, else flagged
# ---------------------------------------------------------------------------

HOURS = 3600.0


async def backdate(db, tid, hours=7.0):
    async with db._engine.begin() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == tid).values(updated_at=time.time() - hours * HOURS)
        )


async def blocked_on(db, tid="t", dep="d"):
    """``tid`` BLOCKED by a ``blocks`` edge on READY ``dep``, stale for 7 hours."""
    await mktask(db, dep, status=TaskStatus.READY)
    await mktask(db, tid, status=TaskStatus.READY)
    await db.add_dependency(tid, dep, "blocks")
    await db.transition_task(tid, TaskStatus.BLOCKED)
    await backdate(db, tid)


async def supervisor_messages(db, tid):
    from src.database.tables import messages

    async with db._engine.connect() as conn:
        return (
            await conn.execute(
                select(messages.c.id, messages.c.to_id, messages.c.body).where(
                    messages.c.body_kind == "stale_open", messages.c.id.like(f"msg-stale-open-{tid}-%")
                )
            )
        ).all()


class TestStaleOpenReevaluation:
    async def test_satisfied_blocker_with_a_stale_projection_is_unblocked(self, db, orch):
        await blocked_on(db)
        # The dependency completed behind the projection's back (a restart
        # between the status write and the recompute).
        async with db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "d").values(status="COMPLETED"))
        assert (await db.get_task("t")).is_blocked

        result = await orch.reevaluate_stale_open()

        assert result["unblocked"] == ["t"]
        assert await status(db, "t") == TaskStatus.READY
        assert await db.get_task_meta("t", "needs_attention") is None

    async def test_open_blocker_is_flagged_once_and_the_supervisor_told(self, db, orch):
        await blocked_on(db)

        first = await orch.reevaluate_stale_open()
        second = await orch.reevaluate_stale_open()

        assert first["flagged"] == ["t"] and second["flagged"] == []
        assert await status(db, "t") == TaskStatus.BLOCKED
        assert await db.get_task_meta("t", "needs_attention") == "stale_open"
        detail = await db.get_task_meta("t", "stale_open_detail")
        assert detail["blockers"] == [{"task_id": "d", "status": "READY", "dep_type": "blocks"}]
        assert detail["stale_hours"] >= 7
        sent = await supervisor_messages(db, "t")
        assert len(sent) == 1
        assert sent[0].to_id == f"supervisor-{PROJECT_ID}"
        assert "--obsolete" in sent[0].body
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert emitted.count("task.needs_attention") == 1

    async def test_the_flag_is_advisory_and_clears_when_the_task_moves(self, db, orch):
        await blocked_on(db)
        await orch.reevaluate_stale_open()
        assert await db.get_task_meta("t", "needs_attention") == "stale_open"

        await db.transition_task("d", TaskStatus.COMPLETED)
        await orch._check_defined_tasks()

        assert await status(db, "t") == TaskStatus.READY
        assert await db.get_task_meta("t", "needs_attention") is None
        assert await db.get_task_meta("t", "stale_open_detail") is None

    async def test_terminal_close_is_flagged_not_reopened(self, db, orch):
        from src.database.queries.task_recovery_queries import incident_reason

        await mktask(db, "t", status=TaskStatus.IN_PROGRESS)
        await db.transition_task("t", TaskStatus.BLOCKED, context="stop_task")
        await db.set_task_meta("t", "blocked_terminal", "stop_task")
        await backdate(db, "t")

        result = await orch.reevaluate_stale_open()

        assert result["flagged"] == ["t"]
        assert await status(db, "t") == TaskStatus.BLOCKED
        detail = await db.get_task_meta("t", "stale_open_detail")
        assert detail["terminal"] == "stop_task"
        # The flag is not a failure: recovery still reads the terminal close.
        meta = await db.get_all_task_meta("t")
        assert incident_reason(meta) is None  # stop_task is not an incident
        meta["blocked_terminal"] = "hard_failure"
        assert incident_reason(meta) == "hard_failure"

    async def test_operator_hold_is_flagged_and_resume_clears_it(self, db, orch):
        await mktask(db, "t", status=TaskStatus.READY)
        await db.pause_task("t")
        await backdate(db, "t")

        result = await orch.reevaluate_stale_open()

        assert result["flagged"] == ["t"]
        detail = await db.get_task_meta("t", "stale_open_detail")
        assert detail["hold"] == {"kind": "manual_pause", "paused_from": "READY"}
        await db.resume_task("t")
        assert await db.get_task_meta("t", "needs_attention") is None
        assert await db.get_task_meta("t", "stale_open_detail") is None

    async def test_pause_whose_hold_is_gone_is_resumed(self, db, orch):
        await mktask(db, "t", status=TaskStatus.READY)
        await db.pause_task("t")
        await db.delete_task_meta("t", "manual_pause")
        await backdate(db, "t")

        result = await orch.reevaluate_stale_open()

        assert result["unblocked"] == ["t"]
        assert await status(db, "t") == TaskStatus.READY

    async def test_other_attention_codes_and_fresh_tasks_are_left_alone(self, db, orch):
        await blocked_on(db, "owned", "d1")
        await db.set_task_meta("owned", "needs_attention", "session_not_live")
        await blocked_on(db, "fresh", "d2")
        await backdate(db, "fresh", hours=1)

        result = await orch.reevaluate_stale_open()

        assert result == {"settled": [], "unblocked": [], "flagged": []}
        assert await db.get_task_meta("owned", "needs_attention") == "session_not_live"
        assert await db.get_task_meta("fresh", "needs_attention") is None

    async def test_threshold_zero_disables_the_re_evaluation(self, db, orch):
        await blocked_on(db)
        orch.config.work_graph.stale_open_after_seconds = 0
        assert await orch.reevaluate_stale_open() == {"settled": [], "unblocked": [], "flagged": []}
        assert await db.get_task_meta("t", "needs_attention") is None

    async def test_the_sweep_runs_on_its_own_cadence(self, db, orch):
        await blocked_on(db)
        orch.retry_obsolete_cleanup = AsyncMock(return_value=[])
        await orch._sweep_lifecycle()
        assert await db.get_task_meta("t", "needs_attention") == "stale_open"
        orch.retry_obsolete_cleanup.assert_awaited_once()
        await orch._sweep_lifecycle()  # inside the interval: no second pass
        orch.retry_obsolete_cleanup.assert_awaited_once()
