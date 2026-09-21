"""Releasing the delegates of an integration operation that has ended.

Replays the seven rows the operator could neither delete nor archive on
2026-09-20 (epic ``keen-crest``): a verifier of a cancelled operation, a repair
delegate of an expired batch operation, and five completed roots carrying
integration episodes.  Every one of them was refused by a foreign key rather
than by a guard, so the dashboard reported nothing an operator could act on.

Design: ``docs/superpowers/specs/2026-09-20-integration-delegate-release-design.md``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_candidate_member_results,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_delegate_releases,
    integration_parent_episodes,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    sessions,
    tasks,
)
from src.models import (
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from tests.db_fixtures import lease_dsn

SHA = "a" * 40
TREE = "b" * 40


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("delegate-release.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    await database.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK)
    )
    await database.update_project(
        "p", hierarchical_integration_mode="hierarchy", integration_repository_id="repo"
    )
    yield database
    await database.close()


async def _task(db, task_id: str, status: TaskStatus = TaskStatus.COMPLETED) -> None:
    await db.create_task(
        Task(
            id=task_id,
            project_id="p",
            title=task_id,
            description="",
            status=status,
            repo_id="repo",
            branch_name=f"aq/{task_id}",
        )
    )


async def _episode(conn, *, parent_task_id: str, episode_id: str = "episode") -> None:
    await conn.execute(
        insert(integration_parent_episodes).values(
            id=episode_id,
            parent_task_id=parent_task_id,
            repository_id="repo",
            generation=3,
            pre_collection_checkpoint_sha=SHA,
            created_at=1.0,
        )
    )


async def _operation(
    conn,
    *,
    operation_id: str = "operation",
    state: str = "cancelled",
    parent_task_id: str | None = "parent",
    batch_id: str | None = None,
    episode_id: str = "episode",
    verifier_task_id: str | None = None,
) -> None:
    await conn.execute(
        insert(integration_repair_operations).values(
            id=operation_id,
            target_kind="parent" if parent_task_id else "batch",
            parent_task_id=parent_task_id,
            batch_id=batch_id,
            episode_id=episode_id,
            active_stage=0,
            state=state,
            policy_snapshot={},
            artifact_snapshot={},
            required_check_version="checks-v1",
            verifier_task_id=verifier_task_id,
            created_at=1.0,
            updated_at=1.0,
        )
    )


async def _stage(
    conn,
    *,
    operation_id: str = "operation",
    repair_task_id: str | None = None,
    state: str = "expired",
) -> None:
    await conn.execute(
        insert(integration_repair_stages).values(
            operation_id=operation_id,
            ordinal=0,
            policy={},
            starting_sha=SHA,
            repair_task_id=repair_task_id,
            writer_kind="repair_delegate" if repair_task_id else None,
            attempts=0,
            state=state,
        )
    )


async def _cancelled_operation_with_delegate(
    db, *, delegate_status: TaskStatus = TaskStatus.BLOCKED, state: str = "cancelled"
) -> None:
    """The shape of ``repair-repair-batch-…-1``: a stage writer with nothing left."""
    await _task(db, "parent", TaskStatus.IN_PROGRESS)
    await _task(db, "delegate", delegate_status)
    async with db.immediate() as conn:
        await _episode(conn, parent_task_id="parent")
        await _operation(conn, state=state)
        await _stage(conn, repair_task_id="delegate")


# -- §3 history still refuses removal, by name --------------------------------


def test_the_audit_table_names_tasks_by_id_only():
    """The release's audit row must outlive the task it released.

    ``integration_delegate_releases`` exists to answer "why did this task end,
    and who ended it" after the task itself is gone, so a foreign key onto
    ``tasks`` or onto the operation would defeat it.
    """
    assert [fk.target_fullname for fk in integration_delegate_releases.foreign_keys] == []


@pytest.mark.parametrize("removal", ["delete", "archive"])
@pytest.mark.parametrize("state", ["cancelled", "completed"])
async def test_finished_operation_history_still_refuses_removal_by_name(db, removal, state):
    """The four history foreign keys are kept (the archive-history spec holds them).

    A finished operation's episode and verifier reference still keep the task
    in the queue, but the refusal is ``integration_owned`` naming the table --
    never a raw ``ForeignKeyViolationError`` -- and nothing is removed.
    """
    await _task(db, "parent")
    await _task(db, "verifier")
    async with db.immediate() as conn:
        await _episode(conn, parent_task_id="parent")
        await _operation(conn, state=state, verifier_task_id="verifier")
        await _stage(conn, repair_task_id="stage-writer")
    await db.transition_task("verifier", TaskStatus.FAILED, force=True)

    for task_id, table in (
        ("parent", "integration_parent_episodes"),
        ("verifier", "integration_repair_operations"),
    ):
        with pytest.raises(HierarchyError) as refusal:
            if removal == "delete":
                await db.delete_task(task_id)
            else:
                await db.archive_task(task_id)
        assert refusal.value.code == "integration_owned"
        assert table in refusal.value.detail
        assert "integration_operation" not in refusal.value.context
        assert await db.get_task(task_id) is not None


async def test_a_live_operation_still_refuses_and_says_what_would_let_go(db):
    await _cancelled_operation_with_delegate(db, state="active")
    await db.transition_task("delegate", TaskStatus.FAILED, force=True)

    with pytest.raises(HierarchyError) as refusal:
        await db.delete_task("delegate")
    assert refusal.value.code == "integration_owned"
    assert refusal.value.context["integration_operation"] == {
        "operation_id": "operation",
        "state": "active",
        "role": "repair_stage",
    }
    assert "aq integration abort operation" in refusal.value.detail
    assert "integration.stranded_delegates" in refusal.value.detail
    assert await db.get_task("delegate") is not None


async def test_an_active_operation_protects_a_repair_delegate_that_had_no_constraint(db):
    """``integration_repair_stages.repair_task_id`` never had a foreign key.

    Before the guard, the repair delegate of a *running* operation could be
    deleted out from under its writer; only the delegates of operations that
    were already over were protected, which is precisely backwards.
    """
    await _cancelled_operation_with_delegate(db, state="escalated")
    await db.transition_task("delegate", TaskStatus.FAILED, force=True)
    with pytest.raises(HierarchyError, match="operation is escalated"):
        await db.archive_task("delegate")


# -- §5 the release ----------------------------------------------------------


async def test_release_settles_the_ticket_and_records_why(db):
    from src.integration.delegate_release import release_delegates, stranded_delegates

    await _cancelled_operation_with_delegate(db)
    await db.set_task_meta("delegate", "needs_attention", "session_exited_open")

    reported = await stranded_delegates(db)
    assert [(row["task_id"], row["role"], row["operation_state"]) for row in reported] == [
        ("delegate", "repair_stage", "cancelled")
    ]

    released = await release_delegates(db, now=500.0, released_by="doctor")
    assert [row["task_id"] for row in released] == ["delegate"]
    assert (await db.get_task("delegate")).status == TaskStatus.FAILED
    assert await db.get_task_meta("delegate", "needs_attention") is None

    async with db._engine.connect() as conn:
        audit = (await conn.execute(select(integration_delegate_releases))).mappings().one()
    assert audit["task_id"] == "delegate"
    assert audit["operation_id"] == "operation"
    assert audit["operation_state"] == "cancelled"
    assert audit["role"] == "repair_stage"
    assert audit["disposition"] == "cancelled"
    assert audit["previous_status"] == "BLOCKED"
    assert audit["released_by"] == "doctor"
    assert audit["released_at"] == 500.0
    assert audit["cleanup"] == {"state": "clear", "blockers": []}

    # Idempotent: a settled delegate is not selected again, so nothing is
    # retried and no second release is written.
    assert await release_delegates(db, now=600.0, released_by="doctor") == []
    assert await stranded_delegates(db) == []


async def test_the_audit_row_outlives_the_delegate_it_released(db):
    from src.integration.delegate_release import release_delegates

    await _cancelled_operation_with_delegate(db)
    await release_delegates(db, now=500.0, released_by="doctor")
    await db.delete_task("delegate")

    assert await db.get_task("delegate") is None
    async with db._engine.connect() as conn:
        audit = (await conn.execute(select(integration_delegate_releases))).mappings().one()
    assert (audit["task_id"], audit["operation_id"]) == ("delegate", "operation")


async def test_a_delegate_with_a_live_writer_is_left_alone(db):
    from src.integration.delegate_release import release_delegates, stranded_delegates

    await _cancelled_operation_with_delegate(db)
    await db.create_session(
        SessionRecord(
            id="writer",
            task_id="delegate",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="writer",
            lifecycle="task",
            state="running",
            desired_state="running",
            work_dir="/tmp/retained",
            epoch="epoch",
            instance_token="writer",
            started_at=100.0,
            last_activity=150.0,
        )
    )
    assert await stranded_delegates(db) == []
    assert await release_delegates(db, now=500.0, released_by="doctor") == []
    assert (await db.get_task("delegate")).status == TaskStatus.BLOCKED


async def test_a_retained_workspace_is_named_not_released(db):
    from src.integration.delegate_release import release_delegates

    await _cancelled_operation_with_delegate(db)
    await db.create_workspace(
        Workspace(
            id="retained",
            project_id="p",
            workspace_path="/tmp/retained",
            source_type=RepoSourceType.LINK,
            locked_by_task_id="delegate",
            enabled=True,
        )
    )
    released = await release_delegates(db, now=500.0, released_by="doctor")
    assert released[0]["cleanup"]["state"] == "blocked"
    assert [b["code"] for b in released[0]["cleanup"]["blockers"]] == ["workspace_locked"]
    # Preserved exactly as found: releasing it needs proof this pass cannot take.
    assert (await db.get_workspace("retained")).locked_by_task_id == "delegate"


async def test_release_can_be_scoped_to_one_operation(db):
    from src.integration.delegate_release import release_delegates

    await _cancelled_operation_with_delegate(db)
    await _task(db, "other-delegate", TaskStatus.BLOCKED)
    async with db.immediate() as conn:
        await _operation(conn, operation_id="other", parent_task_id=None, batch_id="batch")
        await _stage(conn, operation_id="other", repair_task_id="other-delegate")

    released = await release_delegates(
        db, now=500.0, released_by="doctor", operation_ids=["other"]
    )
    assert [row["task_id"] for row in released] == ["other-delegate"]
    assert (await db.get_task("delegate")).status == TaskStatus.BLOCKED


# -- §5.1 cancelling releases ------------------------------------------------


async def test_aborting_an_operation_leaves_no_owned_delegate_behind(db):
    from src.integration.controls import IntegrationControlService

    await _cancelled_operation_with_delegate(db, state="human_required")
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(state="active"))

    result = await IntegrationControlService(db, clock=lambda: 700.0).abort(
        "operation", reason="operator abort"
    )
    assert result["outcome"] == "aborted"
    assert result["released_delegates"] == ["delegate"]
    assert (await db.get_task("delegate")).status == TaskStatus.FAILED

    async with db._engine.connect() as conn:
        audit = (await conn.execute(select(integration_delegate_releases))).mappings().one()
    assert audit["released_by"] == "integration_abort"
    assert audit["disposition"] == "cancelled"
    # And the ticket the cancellation obsoleted can now actually be removed.
    await db.delete_task("delegate")


# -- the scoped operator command ---------------------------------------------


async def test_release_delegates_command_refuses_a_running_operation(db):
    from src.integration.controls import IntegrationControlService

    await _cancelled_operation_with_delegate(db, state="active")
    result = await IntegrationControlService(db, clock=lambda: 700.0).release_delegates(
        "operation"
    )
    assert result["outcome"] == "invalid_state"
    assert (await db.get_task("delegate")).status == TaskStatus.BLOCKED


async def test_release_delegates_command_settles_one_operation_then_reports_nothing(db):
    from src.integration.controls import IntegrationControlService

    await _cancelled_operation_with_delegate(db)
    await _task(db, "other-delegate", TaskStatus.BLOCKED)
    async with db.immediate() as conn:
        await _operation(conn, operation_id="other", parent_task_id=None, batch_id="batch")
        await _stage(conn, operation_id="other", repair_task_id="other-delegate")

    controls = IntegrationControlService(db, clock=lambda: 700.0)
    assert await controls.release_delegates("missing") == {
        "outcome": "not_found",
        "operation_id": "missing",
    }

    released = await controls.release_delegates("operation")
    assert released["outcome"] == "released"
    assert released["released_delegates"] == ["delegate"]
    assert released["project_id"] == "p"
    assert (await db.get_task("delegate")).status == TaskStatus.FAILED
    # Scoped: the other ended operation's delegate is untouched.
    assert (await db.get_task("other-delegate")).status == TaskStatus.BLOCKED

    repeat = await controls.release_delegates("operation")
    assert repeat["outcome"] == "nothing_to_release"
    assert repeat["released_delegates"] == []
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_delegate_releases))).mappings().all()
    assert [row["released_by"] for row in rows] == ["integration_release_delegates"]


# -- §5.2 doctor -------------------------------------------------------------


async def test_doctor_reports_then_releases_then_reports_clean(db):
    from src.doctor.integration_checks import _BY_ID, run_check
    from src.doctor.models import DoctorContext, Severity

    await _cancelled_operation_with_delegate(db)

    reported = await run_check(db, "integration.stranded_delegates")
    assert reported.severity is Severity.WARN
    assert reported.fixable is True
    assert reported.data["count"] == 1
    assert "delegate" in reported.detail and "cancelled" in reported.detail

    fix = _BY_ID["integration.stranded_delegates"].fix
    applied = await fix(DoctorContext(config=None, db=db, handler=None))
    assert applied.severity is Severity.OK
    assert applied.fix_applied is True
    assert applied.data["count"] == 1
    assert (await db.get_task("delegate")).status == TaskStatus.FAILED

    again = await run_check(db, "integration.stranded_delegates")
    assert again.severity is Severity.OK
    # Re-running the fix is a no-op, not a second release.
    assert (await fix(DoctorContext(config=None, db=db, handler=None))).fix_applied is False
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_delegate_releases))).all()) == 1


# -- §6 actionable operator errors -------------------------------------------


@pytest.mark.parametrize("action", ["restart", "resume"])
async def test_operator_commands_name_the_operation_and_the_remedy(db, action):
    from src.integration.delegate_release import release_delegates

    await _cancelled_operation_with_delegate(db, delegate_status=TaskStatus.PAUSED)
    await release_delegates(db, now=500.0, released_by="doctor")

    with pytest.raises(ValueError) as refusal:
        if action == "restart":
            await db.transition_task(
                "delegate", TaskStatus.READY, context="restart_task", force=True
            )
        else:
            await db.resume_task("delegate")
    message = str(refusal.value)
    assert "Integration operation operation is cancelled" in message
    assert "aq doctor --check integration.stranded_delegates --fix" in message


# -- §7 sessions_task_id_fkey ------------------------------------------------


async def test_deleting_a_task_that_owns_a_session_keeps_the_session_row(db):
    """Regression for the ``sessions_task_id_fkey`` crash in the operator's log.

    A session row is the historical record of an agent run -- how long it lived,
    what it cost -- and that stays true after the task is gone.  Deleting the
    task nulls the link rather than failing or destroying the record.
    """
    await _task(db, "solo", TaskStatus.COMPLETED)
    await db.create_session(
        SessionRecord(
            id="attempt",
            task_id="solo",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="attempt",
            lifecycle="task",
            state="stopped",
            desired_state="stopped",
            work_dir="/tmp/solo",
            epoch="epoch",
            instance_token="attempt",
            started_at=100.0,
            last_activity=150.0,
        )
    )
    await db.delete_task("solo")

    assert await db.get_task("solo") is None
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(sessions).where(sessions.c.id == "attempt"))).mappings().one()
    assert row["task_id"] is None


# -- the candidate-member seat ------------------------------------------------


async def _candidate_resolution(db, *, operation_state: str, resolution_state: str) -> None:
    """A pushed candidate-member repair, built the way the batch path leaves it."""
    await _task(db, "member", TaskStatus.COMPLETED)
    await _task(db, "delegate", TaskStatus.BLOCKED)
    await db.create_workspace(
        Workspace(
            id="candidate-workspace",
            project_id="p",
            workspace_path="/tmp/candidate",
            source_type=RepoSourceType.LINK,
            enabled=True,
        )
    )
    await db.create_session(
        SessionRecord(
            id="candidate-session",
            task_id=None,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="candidate-session",
            lifecycle="task",
            state="stopped",
            desired_state="stopped",
            work_dir="/tmp/candidate",
            epoch="epoch",
            instance_token="candidate-session",
            started_at=100.0,
            last_activity=150.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request-batch",
                source_manifest_digest="manifest",
                base_sha=SHA,
                # Membership is only writable while the batch is sealing; the
                # trigger moves it out of reach the moment it is not.
                lifecycle="sealing",
                current_revision=0,
                integration_branch="aq/integration/batch",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                id="review",
                source_task_id="member",
                repository_id="repo",
                source_base=SHA,
                reviewed_head_sha=SHA,
                reviewed_tree_sha=TREE,
                reviewer_task_id="member",
                review_kind="automated",
                generation=0,
                verdict="approved",
                evidence={},
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="batch",
                ordinal=0,
                task_id="member",
                repository_id="repo",
                source_base_sha=SHA,
                reviewed_head_sha=SHA,
                reviewed_tree_sha=TREE,
                review_evidence_id="review",
                review_evidence={},
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="repairing")
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch",
                revision=0,
                construction_base_sha=SHA,
                next_member_ordinal=1,
                state="constructing",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_member_results).values(
                batch_id="batch",
                revision=0,
                member_ordinal=0,
                input_head_sha=SHA,
                input_tree_sha=TREE,
                result="conflict",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await _operation(
            conn, state=operation_state, parent_task_id=None, batch_id="batch"
        )
        # The stage that dispatched the member repair names the same task the
        # reservation does, exactly as the batch path leaves it.
        await _stage(conn, state="active", repair_task_id="delegate")
        await conn.execute(
            insert(integration_candidate_resolutions).values(
                id="resolution",
                batch_id="batch",
                revision=0,
                member_ordinal=0,
                operation_id="operation",
                operation_episode_id="episode",
                stage_ordinal=0,
                stage_deadline_at=999.0,
                project_id="p",
                repair_task_id="delegate",
                repair_session_id="candidate-session",
                repair_session_instance_token="candidate-session",
                repair_workspace_id="candidate-workspace",
                repair_workspace_path="/tmp/candidate",
                repository_id="repo",
                branch="aq/candidate",
                target_branch="aq/integration/batch",
                target_kind="qualified",
                fence_owner_id="operation",
                fence_token=1,
                partial_head_sha=SHA,
                source_base_sha=SHA,
                source_head_sha=SHA,
                resolved_head_sha=SHA,
                resolved_tree_sha=TREE,
                repair_commit_shas=[],
                state=resolution_state,
                created_at=1.0,
                updated_at=1.0,
                # A ``reserved`` row must leave push_evidence SQL NULL; passing
                # None through a JSON column writes the JSON literal ``null``,
                # which the push check constraint rejects.
                **({} if resolution_state == "reserved" else {"push_evidence": {}}),
            )
        )


async def test_a_live_candidate_reservation_refuses_removal(db):
    await _candidate_resolution(db, operation_state="active", resolution_state="reserved")
    with pytest.raises(HierarchyError) as refusal:
        await db.delete_task("delegate")
    assert refusal.value.context["integration_operation"]["role"] == "repair_stage"


async def test_an_unfinished_reservation_of_an_ended_operation_does_not_re_wedge(db):
    """The `repair-repair-batch-…-1` shape: the budget expired, the row did not.

    A reservation left ``reserved`` when its operation was cancelled is moot,
    not a live claim.  Treating it as live is how the original wedge would come
    straight back: the release must settle the ticket, and a later removal must
    be refused as *history* (the resolution still names the task), not as a
    running operation that an abort could never stop.
    """
    from src.integration.delegate_release import release_delegates, stranded_delegates

    await _candidate_resolution(db, operation_state="cancelled", resolution_state="reserved")
    assert [row["task_id"] for row in await stranded_delegates(db)] == ["delegate"]
    released = await release_delegates(db, now=500.0, released_by="doctor")
    assert released[0]["role"] == "repair_stage"
    assert (await db.get_task("delegate")).status == TaskStatus.FAILED

    with pytest.raises(HierarchyError) as refusal:
        await db.delete_task("delegate")
    assert refusal.value.code == "integration_owned"
    assert "integration_operation" not in refusal.value.context
    assert "integration_candidate_resolutions" in refusal.value.detail
    async with db._engine.connect() as conn:
        resolution = (
            await conn.execute(select(integration_candidate_resolutions))
        ).mappings().one()
    assert resolution["repair_task_id"] == "delegate"


async def test_a_candidate_only_delegate_is_released_by_its_resolution(db):
    """No stage names this task; only the reservation does."""
    from src.integration.delegate_release import release_delegates

    await _candidate_resolution(db, operation_state="cancelled", resolution_state="pushed")
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_stages).values(repair_task_id=None, writer_kind=None)
        )
    released = await release_delegates(db, now=500.0, released_by="doctor")
    assert [(row["task_id"], row["role"]) for row in released] == [
        ("delegate", "candidate_member")
    ]
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(select(tasks.c.status).where(tasks.c.id == "delegate"))
        ).scalar_one() == TaskStatus.FAILED.value
