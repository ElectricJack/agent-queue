"""``integration.finished_branch_owners`` — owner rows a finished task never let go.

A project switched from the hierarchy modes to ``development`` kept every
``integration_branch_owners`` row it had, and nothing in development mode
releases one: 201 rows sat ``reserved`` for COMPLETED, archived and deleted
tasks on the operator's database, pinning 181 delivered branches that branch
cleanup must treat as live.  These tests pin what the release may and may not
touch: only task-owned rows of a finished or vanished task, only outside the
hierarchy modes, never while anything live names the task or the branch, an
attached writer only with the provider's stop proof, and never on a snapshot
that changed before the write.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy import insert, select, update

from src.database.tables import (
    archived_tasks,
    events,
    integration_batches,
    integration_branch_owners,
    integration_candidate_ref_mutations,
    integration_candidate_revisions,
    integration_repair_operations,
    integration_repair_stages,
    projects,
    sessions,
    tasks,
    workspaces,
)
from src.doctor import default_registry
from src.doctor.integration_checks import run_check
from src.doctor.models import DoctorContext, Severity
from src.doctor.runner import run_doctor
from src.integration.finished_owners import (
    RELEASE_EVENT,
    finished_branch_owners,
    release_finished_branch_owners,
)
from src.models import Project, Task, TaskStatus

CHECK = "integration.finished_branch_owners"


@pytest.fixture
async def db(reuse_database):
    d = await reuse_database("finished-owners.db")
    await d.create_project(Project(id="p", name="P"))
    await _mode(d, "development")
    yield d


async def _mode(db, mode: str, *, desired: str | None = None) -> None:
    async with db._engine.begin() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                integration_repository_id="repo",
                hierarchical_integration_mode=mode,
                hierarchical_integration_desired_mode=desired or mode,
            )
        )


async def _task(db, task_id: str, status: TaskStatus = TaskStatus.COMPLETED) -> None:
    await db.create_task(Task(id=task_id, project_id="p", title=task_id, description=""))
    await db.update_task(task_id, status=status)


async def _archived(db, task_id: str) -> None:
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(archived_tasks).values(
                id=task_id,
                project_id="p",
                title=task_id,
                description="",
                status="COMPLETED",
                created_at=1.0,
                updated_at=1.0,
                archived_at=2.0,
            )
        )


async def _owner(
    db,
    owner_id: str,
    *,
    handoff_state: str = "reserved",
    role: str = "worker",
    session_id: str | None = None,
    workspace_id: str | None = None,
    repository_id: str = "repo",
) -> str:
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id=f"owner-{owner_id}",
                repository_id=repository_id,
                ref=f"aq/{owner_id}",
                owner_id=owner_id,
                owner_role=role,
                fence_token=3,
                handoff_state=handoff_state,
                session_id=session_id,
                workspace_id=workspace_id,
                confirmed_workspace_id="earlier-checkout",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    return f"owner-{owner_id}"


async def _session(
    db,
    session_id: str,
    *,
    state: str = "stopped",
    desired_state: str = "stopped",
    task_id: str | None = None,
) -> None:
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id=session_id,
                task_id=task_id,
                project_id="p",
                profile_id="worker",
                harness="claude",
                provider="tmux",
                name=f"s-{session_id}",
                lifecycle="task",
                state=state,
                desired_state=desired_state,
                work_dir=f"/slots/{session_id}",
                epoch="e",
                instance_token=f"token-{session_id}",
                started_at=1.0,
            )
        )


async def _workspace(db, workspace_id: str, *, locked_by: str | None = None) -> None:
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(workspaces).values(
                id=workspace_id,
                project_id="p",
                workspace_path=f"/slots/{workspace_id}",
                locked_by_task_id=locked_by,
                created_at=1.0,
            )
        )


async def _attached(db, owner_id: str, *, state: str = "attached") -> str:
    """A stopped writer whose slot a later task has since reused."""
    await _task(db, "successor", TaskStatus.IN_PROGRESS)
    await _session(db, f"{owner_id}-session")
    await _workspace(db, f"{owner_id}-slot", locked_by="successor")
    return await _owner(
        db,
        owner_id,
        handoff_state=state,
        session_id=f"{owner_id}-session",
        workspace_id=f"{owner_id}-slot",
    )


async def _row(db, row_id: str) -> dict:
    async with db._engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    select(integration_branch_owners).where(
                        integration_branch_owners.c.id == row_id
                    )
                )
            )
            .mappings()
            .one()
        )


async def _release_events(db) -> list[dict]:
    async with db._engine.connect() as conn:
        rows = (
            (await conn.execute(select(events).where(events.c.event_type == RELEASE_EVENT)))
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _confirmer(result: bool = True) -> AsyncMock:
    return AsyncMock(return_value=result)


def _handler(confirm_stopped) -> SimpleNamespace:
    development = SimpleNamespace(confirm_stopped=confirm_stopped)
    return SimpleNamespace(orchestrator=SimpleNamespace(development_integration=development))


# -- what counts as finished --------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "expected_status"),
    [
        ("completed", "COMPLETED"),
        ("failed", "FAILED"),
        ("archived", "archived"),
        ("missing", "missing"),
    ],
)
async def test_a_reserved_row_of_a_finished_or_vanished_task_is_released(
    db, setup, expected_status
):
    if setup == "completed":
        await _task(db, "gone")
    elif setup == "failed":
        await _task(db, "gone", TaskStatus.FAILED)
    elif setup == "archived":
        await _archived(db, "gone")
    row_id = await _owner(db, "gone")

    [finding] = await finished_branch_owners(db)
    assert finding["owner_status"] == expected_status
    assert finding["project_id"] == "p"
    assert finding["blocker"] is None

    [release] = await release_finished_branch_owners(db, released_by="test", now=50.0)

    row = await _row(db, row_id)
    assert row["handoff_state"] == "released"
    # A detached reservation is released as is: same owner, same fence.
    assert row["fence_token"] == 3
    assert row["owner_id"] == "gone"
    assert row["updated_at"] == 50.0
    assert release["ref"] == "aq/gone"
    assert release["released_by"] == "test"
    [event] = await _release_events(db)
    assert event["project_id"] == "p"
    assert event["task_id"] == "gone"
    assert json.loads(event["payload"])["owner_status"] == expected_status


@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.READY,
        TaskStatus.IN_PROGRESS,
        TaskStatus.PAUSED,
        TaskStatus.BLOCKED,
        TaskStatus.DEFINED,
    ],
)
async def test_an_unfinished_owner_is_not_even_listed(db, status):
    await _task(db, "open", status)
    row_id = await _owner(db, "open")

    assert await finished_branch_owners(db) == []
    assert await release_finished_branch_owners(db, released_by="test") == []
    assert (await _row(db, row_id))["handoff_state"] == "reserved"


async def test_a_collector_row_is_never_listed(db):
    """A collector's owner is an operation, not a task."""
    row_id = await _owner(db, "some-operation", role="collector")

    assert await finished_branch_owners(db) == []
    await release_finished_branch_owners(db, released_by="test")
    assert (await _row(db, row_id))["handoff_state"] == "reserved"


# -- what keeps a row ---------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "desired"), [("hierarchy", None), ("train", None), ("development", "hierarchy")]
)
async def test_a_hierarchy_project_keeps_its_rows(db, mode, desired):
    """There, a finished child's branch is still its parent's to transfer."""
    await _mode(db, mode, desired=desired)
    await _task(db, "child")
    row_id = await _owner(db, "child")

    [finding] = await finished_branch_owners(db)
    assert "hierarchy recovery path owns this row" in finding["blocker"]
    assert await release_finished_branch_owners(db, released_by="test") == []
    assert (await _row(db, row_id))["handoff_state"] == "reserved"


async def test_a_repository_no_project_integrates_is_kept(db):
    row_id = await _owner(db, "stray", repository_id="unknown-repo")

    [finding] = await finished_branch_owners(db)
    assert finding["owner_status"] == "missing"
    assert "no project integrates repository unknown-repo" in finding["blocker"]
    assert await release_finished_branch_owners(db, released_by="test") == []
    assert (await _row(db, row_id))["handoff_state"] == "reserved"


async def test_a_live_session_for_the_owner_keeps_the_row(db):
    await _task(db, "done")
    await _session(db, "live", state="running", desired_state="running", task_id="done")
    row_id = await _owner(db, "done")

    [finding] = await finished_branch_owners(db)
    assert finding["blocker"] == "session live is still live for done"
    assert await release_finished_branch_owners(db, released_by="test") == []
    assert (await _row(db, row_id))["handoff_state"] == "reserved"


async def test_a_workspace_still_locked_by_the_owner_keeps_the_row(db):
    await _task(db, "done")
    await _workspace(db, "held", locked_by="done")
    row_id = await _owner(db, "done")

    [finding] = await finished_branch_owners(db)
    assert finding["blocker"] == "workspace held is still locked by done"
    assert await release_finished_branch_owners(db, released_by="test") == []
    assert (await _row(db, row_id))["handoff_state"] == "reserved"


async def _batch_operation(db, *, repair_task_id: str, state: str, mutation_branch: str | None):
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request",
                trigger="manual",
                source_manifest_digest="sha256:" + "a" * 64,
                base_sha="b" * 40,
                lifecycle="repairing",
                integration_branch="refs/heads/aq/integration/batch",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch",
                revision=0,
                construction_base_sha="b" * 40,
                state="red",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation",
                target_kind="batch",
                batch_id="batch",
                episode_id="episode",
                active_stage=1,
                state=state,
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="operation",
                ordinal=1,
                policy={},
                starting_sha="b" * 40,
                state="active",
                repair_task_id=repair_task_id,
                writer_kind="repair_delegate",
            )
        )
        if mutation_branch is not None:
            await conn.execute(
                insert(integration_candidate_ref_mutations).values(
                    id="mutation",
                    batch_id="batch",
                    revision=0,
                    purpose="candidate_final",
                    repository_id="repo",
                    branch=mutation_branch,
                    target_branch="main",
                    expected_old_sha="b" * 40,
                    desired_sha="c" * 40,
                    operation_id="operation",
                    operation_episode_id="episode",
                    operation_stage=1,
                    lease_owner_id="lease",
                    lease_fence_token=1,
                    branch_owner_id="operation",
                    branch_owner_role="collector",
                    branch_fence_token=1,
                    nonce="nonce",
                    state="reserved",
                    expires_at=100.0,
                    created_at=1.0,
                    updated_at=1.0,
                )
            )


async def test_a_running_operation_that_owns_the_task_keeps_the_row(db):
    await _task(db, "repair-1")
    row_id = await _owner(db, "repair-1", role="repair")
    await _batch_operation(db, repair_task_id="repair-1", state="active", mutation_branch=None)

    [finding] = await finished_branch_owners(db)
    assert finding["blocker"] == (
        "integration operation operation is active and owns repair-1 as its repair_stage"
    )
    assert await release_finished_branch_owners(db, released_by="test") == []

    # Once the operation has ended, nothing live owns the repair any more.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "operation")
            .values(state="completed")
        )
    [released] = await release_finished_branch_owners(db, released_by="test")
    assert released["owner_role"] == "repair"
    assert (await _row(db, row_id))["handoff_state"] == "released"


async def test_an_in_flight_ref_mutation_on_the_branch_keeps_the_row(db):
    await _task(db, "done")
    row_id = await _owner(db, "done")
    await _batch_operation(
        db, repair_task_id="someone-else", state="cancelled", mutation_branch="aq/done"
    )

    [finding] = await finished_branch_owners(db)
    assert finding["blocker"] == "candidate ref mutation mutation is still in flight on aq/done"
    assert await release_finished_branch_owners(db, released_by="test") == []
    assert (await _row(db, row_id))["handoff_state"] == "reserved"


async def test_a_reserved_row_that_still_names_a_writer_is_kept(db):
    await _task(db, "done")
    row_id = await _owner(db, "done", session_id="s", workspace_id="w")

    [finding] = await finished_branch_owners(db)
    assert "not detached" in finding["blocker"]
    assert await release_finished_branch_owners(db, released_by="test") == []
    assert (await _row(db, row_id))["handoff_state"] == "reserved"


# -- attached writers need the provider's stop proof --------------------------


@pytest.mark.parametrize("state", ["attached", "handoff_pending"])
async def test_a_stopped_writer_is_released_with_a_fresh_fence(db, state):
    row_id = await _attached(db, "deleted-task", state=state)
    confirm = _confirmer(True)

    [finding] = await finished_branch_owners(db, confirm_stopped=confirm)
    assert finding["blocker"] is None
    assert finding["owner_status"] == "missing"
    [released] = await release_finished_branch_owners(
        db, confirm_stopped=confirm, released_by="test"
    )

    probed = confirm.await_args.args[0]
    assert (probed["name"], probed["instance_token"]) == (
        "s-deleted-task-session",
        "token-deleted-task-session",
    )
    row = await _row(db, row_id)
    assert row["handoff_state"] == "released"
    assert row["fence_token"] == 4
    assert row["session_id"] is None and row["workspace_id"] is None
    assert row["confirmed_workspace_id"] == "deleted-task-slot"
    assert released["session_id"] == "deleted-task-session"
    # The reused slot is someone else's now: the release never touches it.
    async with db._engine.connect() as conn:
        slot = (
            (await conn.execute(select(workspaces).where(workspaces.c.id == "deleted-task-slot")))
            .mappings()
            .one()
        )
        session = (
            (await conn.execute(select(sessions).where(sessions.c.id == "deleted-task-session")))
            .mappings()
            .one()
        )
    assert slot["locked_by_task_id"] == "successor"
    assert slot["enabled"] is True
    assert session["state"] == "stopped"


@pytest.mark.parametrize(
    ("confirm", "blocker"),
    [
        (None, "needs the daemon's session provider"),
        (AsyncMock(return_value=False), "the session provider still reports s-gone-session"),
        (
            AsyncMock(side_effect=RuntimeError("socket unreachable")),
            "could not probe s-gone-session: socket unreachable",
        ),
    ],
)
async def test_an_unproven_stop_keeps_the_writer(db, confirm, blocker):
    row_id = await _attached(db, "gone")

    [finding] = await finished_branch_owners(db, confirm_stopped=confirm)
    assert blocker in finding["blocker"]
    assert await release_finished_branch_owners(db, confirm_stopped=confirm, released_by="t") == []
    row = await _row(db, row_id)
    assert (row["handoff_state"], row["fence_token"]) == ("attached", 3)


async def test_a_writer_session_that_is_not_stopped_is_never_probed(db):
    await _session(db, "still-going", state="running", desired_state="running")
    await _workspace(db, "slot")
    row_id = await _owner(
        db, "gone", handoff_state="attached", session_id="still-going", workspace_id="slot"
    )
    confirm = _confirmer(True)

    [finding] = await finished_branch_owners(db, confirm_stopped=confirm)
    assert finding["blocker"] == "writer session still-going is running (desired running)"
    assert await release_finished_branch_owners(db, confirm_stopped=confirm, released_by="t") == []
    confirm.assert_not_awaited()
    assert (await _row(db, row_id))["handoff_state"] == "attached"


async def test_a_writer_session_with_no_record_cannot_be_proven_stopped(db):
    row_id = await _owner(
        db, "gone", handoff_state="attached", session_id="vanished", workspace_id="slot"
    )

    [finding] = await finished_branch_owners(db, confirm_stopped=_confirmer(True))
    assert (
        finding["blocker"] == "writer session vanished has no record, so its stop cannot be proven"
    )
    assert (await _row(db, row_id))["handoff_state"] == "attached"


# -- never on a stale snapshot ------------------------------------------------


async def test_a_row_that_moved_after_the_proof_is_not_released(db):
    row_id = await _attached(db, "gone")

    async def confirm_then_move(_session):
        async with db._engine.begin() as conn:
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.id == row_id)
                .values(fence_token=9)
            )
        return True

    assert (
        await release_finished_branch_owners(
            db, confirm_stopped=confirm_then_move, released_by="test"
        )
        == []
    )
    row = await _row(db, row_id)
    assert (row["handoff_state"], row["fence_token"]) == ("attached", 9)


async def test_a_session_restarted_after_the_proof_is_not_released(db):
    row_id = await _attached(db, "gone")

    async def confirm_then_restart(_session):
        async with db._engine.begin() as conn:
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == "gone-session")
                .values(instance_token="new-token")
            )
        return True

    assert (
        await release_finished_branch_owners(
            db, confirm_stopped=confirm_then_restart, released_by="test"
        )
        == []
    )
    assert (await _row(db, row_id))["handoff_state"] == "attached"


async def test_a_task_reopened_before_the_write_keeps_its_row(db, monkeypatch):
    await _task(db, "done")
    row_id = await _owner(db, "done")
    real_lock = db.lock_hierarchy_project

    async def reopen_then_lock(conn, project_id):
        async with db._engine.begin() as other:
            await other.execute(update(tasks).where(tasks.c.id == "done").values(status="READY"))
        await real_lock(conn, project_id)

    monkeypatch.setattr(db, "lock_hierarchy_project", reopen_then_lock)

    assert await release_finished_branch_owners(db, released_by="test") == []
    assert (await _row(db, row_id))["handoff_state"] == "reserved"


async def test_the_release_is_idempotent(db):
    await _task(db, "done")
    await _owner(db, "done")

    assert len(await release_finished_branch_owners(db, released_by="test")) == 1
    assert await release_finished_branch_owners(db, released_by="test") == []
    assert len(await _release_events(db)) == 1


# -- the doctor check ---------------------------------------------------------


async def test_check_is_ok_when_no_row_is_held(db):
    result = await run_check(db, CHECK)

    assert result.severity is Severity.OK


async def test_check_warns_and_fix_releases_through_the_daemon_stop_probe(db):
    await _task(db, "done")
    await _owner(db, "done")
    attached_id = await _attached(db, "deleted")
    await _task(db, "open", TaskStatus.READY)
    await _owner(db, "open")
    handler = _handler(_confirmer(True))

    before = await run_check(db, CHECK, handler=handler)
    assert before.severity is Severity.WARN
    assert before.fixable is True
    assert before.data["count"] == 2
    assert "--fix" in before.detail

    registry = default_registry()
    ctx = DoctorContext(config=None, db=db, handler=handler)
    report = await run_doctor(registry, ctx, fix=True, only=[CHECK])

    [after] = report["checks"]
    assert after["severity"] == "ok"
    assert after["fix_applied"] is True
    assert (await _row(db, attached_id))["handoff_state"] == "released"
    assert (await _row(db, "owner-open"))["handoff_state"] == "reserved"
    assert len(await _release_events(db)) == 2


async def test_check_is_info_when_every_row_is_kept_on_purpose(db):
    await _attached(db, "deleted")

    # Outside the daemon there is no provider to prove the writer stopped.
    result = await run_check(db, CHECK)

    assert result.severity is Severity.INFO
    assert result.data["kept_count"] == 1
    assert "needs the daemon's session provider" in result.detail


async def test_check_names_kept_rows_alongside_releasable_ones(db):
    await _task(db, "done")
    await _owner(db, "done")
    await _attached(db, "deleted")

    result = await run_check(db, CHECK)

    assert result.severity is Severity.WARN
    assert (result.data["count"], result.data["kept_count"]) == (1, 1)
    assert "1 more are kept" in result.detail


def test_check_is_registered_with_a_fix():
    check = next(c for c in default_registry().checks() if c.id == CHECK)

    assert check.fix is not None
    assert check.timeout_s >= 30


async def test_released_rows_no_longer_hold_a_branch_for_cleanup(db):
    """The point of the release: the cleanup's liveness question now says no."""
    await _task(db, "done")
    await _owner(db, "done")
    await release_finished_branch_owners(db, released_by="test")

    async with db._engine.connect() as conn:
        live = (
            await conn.execute(
                select(sa.func.count())
                .select_from(integration_branch_owners)
                .where(
                    integration_branch_owners.c.ref == "aq/done",
                    integration_branch_owners.c.handoff_state != "released",
                )
            )
        ).scalar_one()
    assert live == 0
