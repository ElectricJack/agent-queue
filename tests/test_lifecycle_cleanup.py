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


# ---------------------------------------------------------------------------
# 3. Obsolete close: COMPLETED, never published, owners and batches released
# ---------------------------------------------------------------------------


async def owner_row(db, task_id, *, row_id="own-1", state="attached", ref="refs/heads/aq/dup"):
    from src.database.tables import integration_branch_owners

    now = time.time()
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id=row_id, repository_id="dev-repo", ref=ref, owner_id=task_id,
                owner_role="worker", fence_token=1, handoff_state=state, session_id=None,
                workspace_id=None, confirmed_workspace_id=None, expires_at=None,
                created_at=now, updated_at=now,
            )
        )


async def owner_state(db, row_id="own-1"):
    from src.database.tables import integration_branch_owners

    async with db._engine.connect() as conn:
        return await conn.scalar(
            select(integration_branch_owners.c.handoff_state).where(
                integration_branch_owners.c.id == row_id
            )
        )


async def batch_row(db, row_id):
    async with db._engine.connect() as conn:
        return (
            await conn.execute(
                select(development_deliveries).where(development_deliveries.c.id == row_id)
            )
        ).mappings().one()


def fake_release(db, *, outcome="released", reason=None):
    """Stand-in for the release-owner proof: releases the row, or refuses."""
    from src.database.tables import integration_branch_owners
    from src.integration.owner_recovery import RecoveryOutcome

    calls: list[tuple[str, str]] = []

    async def release(row_id, principal):
        calls.append((row_id, principal))
        if outcome == "released":
            async with db._engine.begin() as conn:
                await conn.execute(
                    update(integration_branch_owners)
                    .where(integration_branch_owners.c.id == row_id)
                    .values(handoff_state="released")
                )
        return RecoveryOutcome(row_id, outcome, reason, {"detail": f"fake {reason}"})

    release.calls = calls
    return release


async def superseded(db, *, batch_state="parked", status_=TaskStatus.BLOCKED):
    """The solid-cascade shape: a BLOCKED dev task with an owner row and a batch."""
    await dev_project(db)
    await mktask(
        db, "dup", status=status_, project_id=DEV_PROJECT, repo_id="dev-repo",
        branch_name="aq/dup",
    )
    await db.save_task_completion(
        TaskCompletion(id="close-dup", task_id="dup", outcome="fail", commits=[SHA],
                       completed_at=time.time())
    )
    await mktask(
        db, "other", status=TaskStatus.COMPLETED, project_id=DEV_PROJECT, repo_id="dev-repo",
        branch_name="aq/other",
    )
    await owner_row(db, "dup")
    await delivery(db, "batch-p", state=batch_state, members=[("dup", SHA), ("other", SHA)])


def service(db, release=None):
    from src.integration.obsolete_close import ObsoleteClose

    return ObsoleteClose(db, release_owner=release)


class TestObsoleteClose:
    async def test_superseded_task_closes_releases_everything_and_can_be_deleted(self, db):
        from src.database.queries.hierarchy_queries import HierarchyError

        await superseded(db)
        await mktask(db, "next", status=TaskStatus.DEFINED, project_id=DEV_PROJECT)
        await db.add_dependency("next", "dup", "blocks")
        assert (await db.get_task("next")).is_blocked
        with pytest.raises(HierarchyError):
            await db.delete_task("dup")

        release = fake_release(db)
        result = await service(db, release).close(
            "dup", reason="superseded by PR #639", principal="human:local-operator"
        )

        assert result["outcome"] == "closed" and result["previous_status"] == "BLOCKED"
        assert result["cleanup"]["state"] == "done"
        assert result["cleanup"]["dropped_batches"] == [{"batch_id": "batch-p", "state": "parked"}]
        assert release.calls == [("own-1", "human:local-operator")]
        assert await status(db, "dup") == TaskStatus.COMPLETED
        assert await db.get_task_meta("dup", "work_outcome") == "abandoned"
        marker = await db.get_task_meta("dup", "obsolete")
        assert marker["reason"] == "superseded by PR #639"
        assert marker["cleanup"]["state"] == "done"
        # Superseded work satisfies its dependents without being delivered.
        assert not (await db.get_task("next")).is_blocked
        parked = await batch_row(db, "batch-p")
        assert parked["state"] == "cancelled"
        assert parked["evidence"]["released"]["conclusion"] == "obsolete_member"
        assert await owner_state(db) == "released"

        await db.delete_task("dup")
        assert await db.get_task("dup") is None

    async def test_obsolete_work_is_never_published(self, db, tmp_path):
        from src.integration.development import DevelopmentIntegration

        await dev_project(db)
        await mktask(
            db, "dup", status=TaskStatus.COMPLETED, project_id=DEV_PROJECT, repo_id="dev-repo",
            branch_name="aq/dup",
        )
        publisher = DevelopmentIntegration(db, data_dir=tmp_path, git=MagicMock())
        repo = await db.get_repo("dev-repo")
        assert await publisher._has_pending_work(DEV_PROJECT, repo, now=time.time())

        await service(db).close("dup", reason="duplicate", principal="human:local-operator")

        assert not await publisher._has_pending_work(DEV_PROJECT, repo, now=time.time())
        assert await db.obsolete_task_ids(["dup", "missing"]) == {"dup"}

    async def test_publishing_batch_is_left_alone_and_retried_after_it_parks(self, db):
        await superseded(db, batch_state="publishing")
        closer = service(db, fake_release(db))

        result = await closer.close("dup", reason="duplicate", principal="human:local-operator")

        pending = result["cleanup"]["pending"]
        assert [(p["kind"], p["reason"]) for p in pending] == [("development_batch", "publishing")]
        assert (await batch_row(db, "batch-p"))["state"] == "publishing"
        assert (await closer.retry_pending())[0]["state"] == "pending"

        async with db._engine.begin() as conn:
            await conn.execute(
                update(development_deliveries)
                .where(development_deliveries.c.id == "batch-p")
                .values(state="parked")
            )
        retried = await closer.retry_pending()

        assert retried[0]["state"] == "done"
        assert (await batch_row(db, "batch-p"))["state"] == "cancelled"
        assert await closer.retry_pending() == []

    async def test_open_repair_keeps_the_parked_batch(self, db):
        await superseded(db)
        await mktask(db, "development-repair-abc", status=TaskStatus.READY, project_id=DEV_PROJECT)
        await db.set_task_meta(
            "development-repair-abc", "development_repair_sources",
            [{"task_id": "dup", "source_sha": SHA}],
        )

        result = await service(db, fake_release(db)).close(
            "dup", reason="duplicate", principal="human:local-operator"
        )

        reasons = {p["reason"] for p in result["cleanup"]["pending"]}
        assert reasons == {"repair_in_flight"}
        assert (await batch_row(db, "batch-p"))["state"] == "parked"

    async def test_refused_owner_proof_stays_pending_with_its_reason(self, db):
        await superseded(db, batch_state="delivered")
        release = fake_release(db, outcome="not_eligible", reason="writer_live")

        result = await service(db, release).close(
            "dup", reason="duplicate", principal="human:local-operator"
        )

        assert result["cleanup"]["pending"] == [
            {
                "kind": "branch_owner", "owner_row_id": "own-1", "ref": "refs/heads/aq/dup",
                "handoff_state": "attached", "reason": "writer_live", "detail": "fake writer_live",
            }
        ]
        assert await owner_state(db) == "attached"

    async def test_closing_again_only_reruns_the_cleanup(self, db):
        await superseded(db, batch_state="delivered")
        closer = service(db, fake_release(db))
        first = await closer.close("dup", reason="duplicate", principal="human:local-operator")
        again = await closer.close("dup", reason="other words", principal="human:local-operator")
        assert first["outcome"] == "closed"
        assert again["outcome"] == "already_obsolete"
        assert again["reason"] == "duplicate" and again["cleanup"]["state"] == "done"

    async def test_operator_hold_is_superseded_by_the_close(self, db):
        await mktask(db, "held", status=TaskStatus.READY)
        await db.pause_task("held")
        await service(db).close("held", reason="plan changed", principal="human:local-operator")
        assert await status(db, "held") == TaskStatus.COMPLETED
        assert await db.get_task_meta("held", "manual_pause") is None

    async def test_refusals(self, db):
        from src.integration.obsolete_close import ObsoleteCloseRefused

        closer = service(db)
        await mktask(db, "live", status=TaskStatus.IN_PROGRESS)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s-live", task_id="live", project_id=PROJECT_ID, profile_id="worker",
                harness="claude", provider="fake", name="s-live", lifecycle="pool",
                state="running", work_dir="/tmp", epoch="e", instance_token="t",
                started_at=now, last_activity=now,
            )
        )
        await mktask(db, "parent", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "parent.1", status=TaskStatus.READY)
        await db.add_dependency("parent.1", "parent", "parent-child")
        await db.create_project(Project(id="p-train", name="Train"))
        async with db._engine.begin() as conn:
            await conn.execute(
                update(projects).where(projects.c.id == "p-train")
                .values(hierarchical_integration_mode="hierarchy")
            )
        await mktask(db, "train-task", status=TaskStatus.READY, project_id="p-train")

        cases = {
            ("live", "why"): "obsolete.live_session",
            ("parent", "why"): "obsolete.open_children",
            ("train-task", "why"): "obsolete.unsupported_mode",
            ("parent.1", "  "): "obsolete.reason_required",
            ("missing", "why"): "obsolete.not_found",
        }
        for (task_id, reason), code in cases.items():
            with pytest.raises(ObsoleteCloseRefused) as refused:
                await closer.close(task_id, reason=reason, principal="human:local-operator")
            assert refused.value.code == code, task_id
        assert await status(db, "live") == TaskStatus.IN_PROGRESS

    async def test_release_owner_proof_takes_a_reserved_row(self, db):
        from src.integration.obsolete_close import ObsoleteOwnerRelease
        from src.integration.owner_recovery import (
            NOT_RECOVERABLE_STATE,
            RELEASED,
            OwnerRecovery,
            _Plan,
        )

        await dev_project(db)
        await mktask(db, "dup", status=TaskStatus.COMPLETED, project_id=DEV_PROJECT)
        await owner_row(db, "dup", state="reserved")

        base = OwnerRecovery(db, MagicMock(), None, confirm_stopped=AsyncMock())
        refused = await base.recover("own-1", principal="human:local-operator")
        assert refused.reason == NOT_RECOVERABLE_STATE

        proof = ObsoleteOwnerRelease(db, MagicMock(), None, confirm_stopped=AsyncMock())
        proof._secure_branch = AsyncMock(return_value=_Plan(preserved=False, workspace_clean=True))
        released = await proof.recover("own-1", principal="human:local-operator")

        assert released.outcome == RELEASED
        proof._secure_branch.assert_awaited_once()
        assert await owner_state(db) == "released"


class TestObsoleteCloseCommand:
    def handler(self, db):
        from types import SimpleNamespace

        from src.commands.session_commands import SessionCommandsMixin

        class Handler(SessionCommandsMixin):
            pass

        handler = Handler()
        handler.db = db
        handler.orchestrator = SimpleNamespace(db=db, git=None)
        handler._emit_task_graph_change = AsyncMock()
        return handler

    async def test_operator_closes_obsolete_and_the_task_is_commented(self, db):
        await mktask(db, "t", status=TaskStatus.BLOCKED)
        handler = self.handler(db)

        result = await handler._cmd_task_close(
            {"task_id": "t", "obsolete": True, "reason": "superseded by main"}
        )

        assert result["success"] is True and result["outcome"] == "closed"
        assert await status(db, "t") == TaskStatus.COMPLETED
        comments = (await db.list_task_comments("t"))["comments"]
        assert any("Closed as obsolete" in c["body"] for c in comments)
        handler._emit_task_graph_change.assert_awaited_once()

    async def test_worker_session_is_refused(self, db):
        from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
        from src.profiles.capabilities import DENY_ALL

        await mktask(db, "t", status=TaskStatus.BLOCKED)
        worker = ExecutionPrincipal(
            kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="w", project_id=PROJECT_ID
        )
        with principal_context(worker):
            result = await self.handler(db)._cmd_task_close(
                {"task_id": "t", "obsolete": True, "reason": "mine now"}
            )
        assert result["success"] is False and result["code"] == "obsolete.not_authorized"
        assert await status(db, "t") == TaskStatus.BLOCKED

    async def test_obsolete_and_outcome_do_not_mix(self, db):
        await mktask(db, "t", status=TaskStatus.BLOCKED)
        result = await self.handler(db)._cmd_task_close(
            {"task_id": "t", "obsolete": True, "reason": "x", "outcome": "pass"}
        )
        assert result["code"] == "obsolete.outcome_conflict"


# ---------------------------------------------------------------------------
# 4. Doctor: finished work still holding lifecycle state
# ---------------------------------------------------------------------------


class TestDanglingLifecycleDoctor:
    async def check(self, db):
        from src.doctor.task_checks import run_check

        return await run_check(db, "tasks.dangling_lifecycle")

    async def test_clean_install_is_ok(self, db):
        from src.doctor.models import Severity

        await mktask(db, "done", status=TaskStatus.COMPLETED)
        result = await self.check(db)
        assert result.severity == Severity.OK

    async def test_lists_owners_batches_and_finished_open_containers(self, db):
        from src.doctor.models import Severity

        await superseded(db, status_=TaskStatus.COMPLETED)
        kids = await epic(db, status=TaskStatus.BLOCKED)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        await db.transition_task(kids[1], TaskStatus.FAILED)

        result = await self.check(db)

        assert result.severity == Severity.WARN
        assert result.data["owners"] == [
            {"task_id": "dup", "owner_row_id": "own-1", "ref": "refs/heads/aq/dup",
             "handoff_state": "attached"}
        ]
        assert {(b["task_id"], b["state"]) for b in result.data["batches"]} == {
            ("dup", "parked"), ("other", "parked"),
        }
        assert result.data["containers"] == [
            {"task_id": "e", "status": "BLOCKED", "failed_children": 1}
        ]
        assert "--obsolete" in result.detail

    async def test_obsolete_close_clears_what_it_reported(self, db):
        from src.doctor.models import Severity

        await superseded(db, status_=TaskStatus.COMPLETED)
        await service(db, fake_release(db)).close(
            "dup", reason="duplicate", principal="human:local-operator"
        )
        result = await self.check(db)
        # The parked batch is cancelled and the owner released; "other" went
        # back to the publisher, so nothing is left to report.
        assert result.severity == Severity.OK, result.data

    async def test_publishing_membership_alone_is_informational(self, db):
        from src.doctor.models import Severity

        await dev_project(db)
        await mktask(db, "t", status=TaskStatus.COMPLETED, project_id=DEV_PROJECT)
        await delivery(db, "b-pub", state="publishing", members=[("t", SHA)])
        result = await self.check(db)
        assert result.severity == Severity.INFO
        assert result.data["counts"]["publishing_batches"] == 1

    async def test_pending_obsolete_cleanup_is_reported(self, db):
        await superseded(db, batch_state="publishing", status_=TaskStatus.COMPLETED)
        await service(db, fake_release(db)).close(
            "dup", reason="duplicate", principal="human:local-operator"
        )
        result = await self.check(db)
        assert result.data["obsolete_pending"] == [{"task_id": "dup", "pending": ["publishing"]}]


class TestStaleOpenScope:
    async def test_a_paused_project_is_not_flagged(self, db, orch):
        from src.models import ProjectStatus

        await blocked_on(db)
        await db.update_project(PROJECT_ID, status=ProjectStatus.PAUSED)
        assert (await orch.reevaluate_stale_open())["flagged"] == []
        assert await db.get_task_meta("t", "needs_attention") is None
