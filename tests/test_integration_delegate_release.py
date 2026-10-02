"""Releasing the delegates of an integration operation that has ended.

Replays the seven rows the operator could neither delete nor archive on
2026-09-20 (epic ``keen-crest``): a verifier of a cancelled operation, a repair
delegate of an expired batch operation, and five completed roots carrying
integration episodes.  Every one of them was refused by a foreign key rather
than by a guard, so the dashboard reported nothing an operator could act on.

Design: ``docs/superpowers/specs/2026-09-20-integration-delegate-release-design.md``.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import insert, select, update

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    archived_tasks,
    gates,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_delegate_releases,
    integration_parent_episodes,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    sessions,
    task_comments,
    tasks,
)
from src.models import (
    Agent,
    AgentOutput,
    AgentResult,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)

SHA = "a" * 40
TREE = "b" * 40


@pytest.fixture
async def db(tmp_path, reuse_database):
    database = await reuse_database("delegate-release.db")
    await database.create_project(Project(id="p", name="Project"))
    await database.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK)
    )
    await database.update_project(
        "p", hierarchical_integration_mode="hierarchy", integration_repository_id="repo"
    )
    yield database


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
    active_stage: int = 0,
) -> None:
    await conn.execute(
        insert(integration_repair_operations).values(
            id=operation_id,
            target_kind="parent" if parent_task_id else "batch",
            parent_task_id=parent_task_id,
            batch_id=batch_id,
            episode_id=episode_id,
            active_stage=active_stage,
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
    ordinal: int = 0,
) -> None:
    await conn.execute(
        insert(integration_repair_stages).values(
            operation_id=operation_id,
            ordinal=ordinal,
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


async def _obsolete_stage_delegate(db, *, operation_state="escalated", active_stage=1):
    await _cancelled_operation_with_delegate(db, state=operation_state)
    await _task(db, "current", TaskStatus.BLOCKED)
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id.in_(["delegate", "current"]))
                           .values(created_by_kind="integration_repair", created_by_id="operation"))
        await conn.execute(update(integration_repair_operations).values(active_stage=active_stage))
        await _stage(conn, repair_task_id="current", state="active", ordinal=1)
    await db.set_task_meta("delegate", "integration_retirement", {"reason": "prior budget expired"})


@pytest.mark.parametrize("operation_state", ["escalated", "completed", "cancelled"])
async def test_archive_obsolete_delegate_preserves_status_history_and_current_incident(
    db, operation_state
):
    from src.integration.delegate_release import archive_obsolete_delegates

    await _obsolete_stage_delegate(db, operation_state=operation_state)
    if operation_state in {"completed", "cancelled"}:
        # The active-stage reference of an ended operation is obsolete too.
        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "current")
                               .values(created_by_kind="human"))
    before = await db.get_integration_operation("operation")
    result = await archive_obsolete_delegates(db, operation_ids=["operation"])
    assert result["archived_delegates"] == ["delegate"] and not result["blockers"]
    assert await db.get_task("delegate") is None
    assert await db.get_task("current") is not None
    assert await db.get_integration_operation("operation") == before
    async with db._engine.connect() as conn:
        archived = (await conn.execute(select(archived_tasks).where(
            archived_tasks.c.id == "delegate",
        ))).mappings().one()
        assert archived["status"] == "BLOCKED" and archived["branch_name"] == "aq/delegate"
        assert (await conn.execute(select(integration_repair_stages.c.repair_task_id).where(
            integration_repair_stages.c.ordinal == 0,
        ))).scalar_one() == "delegate"
        note = (await conn.execute(select(task_comments.c.body).where(
            task_comments.c.task_id == "delegate",
        ))).scalar_one()
        assert "prior budget expired" in note and '"ordinal": 0' in note
    assert not (await archive_obsolete_delegates(db))["archived_delegates"]


@pytest.mark.parametrize("blocker", [
    "owner", "workspace", "claim", "session", "gate", "dependency", "verifier",
    "current_stage", "nonterminal_stage", "child",
])
async def test_obsolete_delegate_archive_refuses_retained_authority_and_gates(db, blocker):
    from src.integration.delegate_release import archive_obsolete_delegates

    await _obsolete_stage_delegate(db, active_stage=0 if blocker == "current_stage" else 1)
    if blocker == "owner":
        async with db.immediate() as conn:
            await conn.execute(insert(integration_branch_owners).values(
                id="owner", repository_id="repo", ref="aq/delegate", owner_id="delegate",
                owner_role="repair", fence_token=1, handoff_state="reserved",
                created_at=1.0, updated_at=1.0,
            ))
    elif blocker == "workspace":
        await db.create_workspace(Workspace(id="retained", project_id="p",
            workspace_path="/tmp/retained", source_type=RepoSourceType.LINK,
            locked_by_task_id="delegate", enabled=True))
    elif blocker in {"claim", "session"}:
        await db.create_session(SessionRecord(id="holder", task_id="delegate", project_id="p",
            profile_id="repairer", harness="fake", provider="fake", name="holder",
            lifecycle="pool", state="stopped" if blocker == "claim" else "running",
            desired_state="stopped" if blocker == "claim" else "running",
            claim_phase="active" if blocker == "claim" else None,
            work_dir="/tmp/retained", epoch="epoch", instance_token="token", started_at=1.0))
    elif blocker == "gate":
        await db.create_gate("p", "human", "Preserve decision", waiter_task_ids=["delegate"])
    elif blocker == "dependency":
        await _task(db, "dependent", TaskStatus.READY)
        await db.add_dependency("dependent", "delegate")
    elif blocker == "child":
        await db.create_task(Task(id="delegate.1", project_id="p", title="Child", description="",
                                 parent_task_id="delegate", status=TaskStatus.BLOCKED))
    else:
        async with db.immediate() as conn:
            if blocker == "verifier":
                await conn.execute(update(integration_repair_operations)
                                   .values(verifier_task_id="delegate"))
            elif blocker == "current_stage":
                pass  # Seeded as the current stage; stage identities cannot decrease.
            else:
                await conn.execute(update(integration_repair_stages)
                                   .where(integration_repair_stages.c.ordinal == 0)
                                   .values(state="active"))
    result = await archive_obsolete_delegates(db)
    assert not result["archived_delegates"]
    assert await db.get_task("delegate") is not None
    if blocker not in {"current_stage", "nonterminal_stage"}:
        assert result["blockers"]
    if blocker == "gate":
        async with db._engine.connect() as conn:
            assert (await conn.execute(select(gates.c.status))).scalar_one() == "open"
            assert not (await conn.execute(select(task_comments.c.id))).first()


async def test_obsolete_delegate_archive_keeps_unresolved_candidate_reservation(db):
    from src.integration.delegate_release import archive_obsolete_delegates

    await _candidate_resolution(db, operation_state="cancelled", resolution_state="reserved")
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "delegate")
                           .values(created_by_kind="integration_repair", created_by_id="operation"))
    result = await archive_obsolete_delegates(db)
    assert not result["archived_delegates"]
    assert result["blockers"][0]["blockers"][0]["code"] == "candidate_resolution_retained"


async def test_archive_obsolete_control_does_not_cancel_live_operation(db):
    from src.integration.controls import IntegrationControlService

    await _obsolete_stage_delegate(db)
    controls = IntegrationControlService(db)
    assert (await controls.release_delegates("operation"))["outcome"] == "invalid_state"
    result = await controls.release_delegates("operation", archive_obsolete=True)
    assert result["outcome"] == "released" and result["archived_delegates"] == ["delegate"]
    assert (await db.get_integration_operation("operation"))["state"] == "escalated"


async def test_automatic_delegate_archive_retains_large_metadata_snapshot(db):
    from src.integration.repair import RepairService

    await _obsolete_stage_delegate(db)
    marker = "exact preserved failure context " * 1800
    await db.set_task_meta("delegate", "failure_history", {"context": marker})
    await db.create_agent(Agent(id="history-agent", name="Prior worker", profile_id="worker"))
    await db.save_task_result("delegate", "history-agent", AgentOutput(
        result=AgentResult.FAILED, summary="frozen stage timed out", error_message="exact failure",
    ))
    await RepairService(db).retire_terminal_delegates(100.0)
    assert await db.get_task("delegate") is None
    assert await db.get_task("current") is not None
    async with db._engine.connect() as conn:
        notes = (await conn.execute(select(task_comments.c.body).where(
            task_comments.c.task_id == "delegate",
        ).order_by(task_comments.c.created_at))).scalars().all()
    assert len(notes) > 1 and all(len(note) <= 16000 for note in notes)
    snapshot = json.loads("".join(note.split(": ", 1)[1] for note in notes))
    assert json.loads(snapshot["metadata"]["failure_history"])["context"] == marker
    assert snapshot["results"][0]["error_message"] == "exact failure"
    assert snapshot["results"][0]["result"] == "failed"


# -- §3 history refuses deletion but permits archive, by name -----------------


def test_the_audit_table_names_tasks_by_id_only():
    """The release's audit row must outlive the task it released.

    ``integration_delegate_releases`` exists to answer "why did this task end,
    and who ended it" after the task itself is gone, so a foreign key onto
    ``tasks`` or onto the operation would defeat it.
    """
    assert [fk.target_fullname for fk in integration_delegate_releases.foreign_keys] == []


@pytest.mark.parametrize("state", ["cancelled", "completed"])
async def test_finished_operation_history_refuses_delete_but_permits_archive(db, state):
    """Audit history pins an id against deletion, not a live queue row.

    The approved archive-history spec makes the four references soft: delete
    has the named ``integration_history_retained`` refusal, while archive
    preserves the id in ``archived_tasks`` and lets the live row leave.
    """
    await _task(db, "parent")
    await _task(db, "verifier")
    async with db.immediate() as conn:
        await _episode(conn, parent_task_id="parent")
        await _operation(conn, state=state, verifier_task_id="verifier")
        await _stage(conn, repair_task_id="stage-writer")
    await db.transition_task("verifier", TaskStatus.FAILED, force=True)

    references = (
        ("parent", "integration_parent_episodes"),
        ("verifier", "integration_repair_operations"),
    )
    for task_id, table in references:
        with pytest.raises(HierarchyError) as refusal:
            await db.delete_task(task_id)
        assert refusal.value.code == "integration_history_retained"
        assert table in refusal.value.detail
        assert "integration_operation" not in refusal.value.context
        assert await db.get_task(task_id) is not None

    for task_id, _table in references:
        await db.archive_task(task_id)
        assert await db.get_task(task_id) is None


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
        "task_id": "delegate",
    }
    assert "aq integration cancel-preserving operation" in refusal.value.detail
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


async def test_a_stopped_writer_that_kept_its_claim_does_not_hold_the_delegate(db):
    """Production's ``repair-repair-batch-…-1``: BLOCKED, cancelled, and never released.

    ``_terminate_pool_session_locked`` confirms a pool writer's process is gone
    and marks the row stopped, but keeps ``task_id``/``claim_phase`` while an
    integration owner still retains its checkout -- that claim is handoff
    evidence, not a writer.  ``stopped`` is terminal (nothing revives the row)
    and a claim only activates on a running session, so the delegate is
    listed, settled, and its owner/checkout stay named as cleanup.
    """
    from src.doctor.integration_checks import run_check
    from src.doctor.models import Severity
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
            lifecycle="pool",
            state="stopped",
            desired_state="stopped",
            claim_phase="active",
            claim_phase_at=110.0,
            last_claim_epoch=1,
            work_dir="/tmp/retained",
            epoch="epoch",
            instance_token="writer",
            started_at=100.0,
            ended_at=200.0,
        )
    )
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
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="retained-owner", repository_id="repo", ref="aq/delegate",
                owner_id="delegate", owner_role="repair", fence_token=3,
                handoff_state="attached", session_id="writer", workspace_id="retained",
                created_at=1.0, updated_at=1.0,
            )
        )

    reported = await stranded_delegates(db)
    assert [row["task_id"] for row in reported] == ["delegate"]
    assert [b["code"] for b in reported[0]["cleanup"]] == [
        "branch_owner_retained", "workspace_locked",
    ]
    check = await run_check(db, "integration.stranded_delegates")
    assert check.severity is Severity.WARN
    assert check.data["count"] == 1

    released = await release_delegates(db, now=500.0, released_by="doctor")
    assert [row["task_id"] for row in released] == ["delegate"]
    assert (await db.get_task("delegate")).status == TaskStatus.FAILED
    assert released[0]["cleanup"]["state"] == "blocked"
    assert [b["code"] for b in released[0]["cleanup"]["blockers"]] == [
        "branch_owner_retained", "workspace_locked",
    ]

    # Retirement settles the ticket only: the stopped row keeps its claim, the
    # owner stays attached to it and the checkout stays locked.
    writer = await db.get_session("writer")
    assert (writer.state, writer.task_id, writer.claim_phase) == ("stopped", "delegate", "active")
    assert (await db.get_workspace("retained")).locked_by_task_id == "delegate"
    async with db._engine.connect() as conn:
        owner = (
            await conn.execute(
                select(integration_branch_owners).where(
                    integration_branch_owners.c.id == "retained-owner"
                )
            )
        ).mappings().one()
    assert (owner["handoff_state"], owner["session_id"]) == ("attached", "writer")
    assert (await run_check(db, "integration.stranded_delegates")).severity is Severity.OK


@pytest.mark.parametrize(
    ("state", "desired_state"), [("running", "running"), ("stopped", "running")]
)
async def test_a_session_not_fully_stopped_still_defers_the_release(db, state, desired_state):
    """Only a fully stopped row is history; anything else may still be a writer."""
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
            lifecycle="pool",
            state=state,
            desired_state=desired_state,
            claim_phase="active",
            work_dir="/tmp/retained",
            epoch="epoch",
            instance_token="writer",
            started_at=100.0,
        )
    )
    assert await stranded_delegates(db) == []
    assert await release_delegates(db, now=500.0, released_by="doctor") == []
    assert (await db.get_task("delegate")).status == TaskStatus.BLOCKED
    assert [b["code"] for b in await db.get_integration_delegate_cleanup("delegate")] == [
        "session_attached",
    ]


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
    assert refusal.value.code == "integration_history_retained"
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


# -- a writer seat that can never be used again (bold-impact-53) --------------
#
# ``get_retired_integration_writer`` is the proof the session reconciler needs
# before it stops a drain-acknowledged pool worker that still holds a delegate
# its close can never settle.  It answers only from durable integration state,
# and anything a live operation could still hand back to the writer is not
# retired.


async def _writer_seat(
    db,
    *,
    operation_state: str,
    stage_state: str,
    active_stage: int = 0,
    delegate_status: TaskStatus = TaskStatus.IN_PROGRESS,
) -> None:
    await _task(db, "parent", TaskStatus.IN_PROGRESS)
    await _task(db, "delegate", delegate_status)
    async with db.immediate() as conn:
        await _episode(conn, parent_task_id="parent")
        await _operation(conn, state=operation_state, active_stage=active_stage)
        await _stage(conn, repair_task_id="delegate", state=stage_state)
        for ordinal in range(1, active_stage + 1):
            await _stage(conn, ordinal=ordinal, state="active")


@pytest.mark.parametrize(
    ("operation_state", "active_stage", "stage_state", "disposition"),
    [
        # Delivered: the stage passed with its operation; the delegate never closed.
        ("completed", 0, "passed", "superseded"),
        ("completed", 0, "awaiting_completion", "superseded"),
        ("cancelled", 0, "active", "cancelled"),
        # Expired or failed, and the operation escalated to a successor stage.
        ("escalated", 1, "expired", "superseded"),
        ("escalated", 1, "failed", "superseded"),
        ("active", 1, "cancelled", "cancelled"),
    ],
)
async def test_a_writer_seat_no_transition_can_restore_is_proven_retired(
    db, operation_state, active_stage, stage_state, disposition
):
    await _writer_seat(
        db, operation_state=operation_state, stage_state=stage_state, active_stage=active_stage
    )

    proof = await db.get_retired_integration_writer("delegate")

    assert proof is not None
    assert (proof["operation_id"], proof["operation_state"]) == ("operation", operation_state)
    assert (proof["role"], proof["stage"], proof["stage_state"]) == (
        "repair_stage", 0, stage_state
    )
    assert proof["disposition"] == disposition
    assert "integration operation operation" in proof["reason"]


@pytest.mark.parametrize(
    ("operation_state", "active_stage", "stage_state"),
    [
        # The current writer, before and after CI accepted its candidate.
        ("active", 0, "active"),
        ("active", 0, "awaiting_completion"),
        ("escalated", 0, "pending"),
        # Human gate: ``integration resume`` revives a failed/expired/cancelled
        # stage of a human_required operation and may keep this exact writer.
        ("human_required", 0, "expired"),
        ("human_required", 0, "failed"),
        ("human_required", 0, "cancelled"),
        # Terminal but still the operation's current stage: not proven yet.
        ("escalated", 0, "expired"),
    ],
)
async def test_a_writer_seat_a_live_operation_can_still_use_is_not_retired(
    db, operation_state, active_stage, stage_state
):
    await _writer_seat(
        db, operation_state=operation_state, stage_state=stage_state, active_stage=active_stage
    )

    assert await db.get_retired_integration_writer("delegate") is None


async def test_a_task_that_never_held_an_integration_seat_is_not_retired(db):
    await _writer_seat(db, operation_state="completed", stage_state="passed")
    await _task(db, "unrelated", TaskStatus.IN_PROGRESS)

    assert await db.get_retired_integration_writer("unrelated") is None
    # The parent of an ended operation is its target, never one of its writers.
    assert await db.get_retired_integration_writer("parent") is None


@pytest.mark.parametrize(("operation_state", "retired"), [
    ("completed", True), ("cancelled", True), ("active", False), ("human_required", False),
])
async def test_a_verifier_seat_is_retired_only_with_its_operation(db, operation_state, retired):
    await _task(db, "parent", TaskStatus.IN_PROGRESS)
    await _task(db, "verifier", TaskStatus.IN_PROGRESS)
    async with db.immediate() as conn:
        await _episode(conn, parent_task_id="parent")
        await _operation(conn, state=operation_state, verifier_task_id="verifier")

    proof = await db.get_retired_integration_writer("verifier")

    if not retired:
        assert proof is None
        return
    assert (proof["role"], proof["stage"], proof["operation_state"]) == (
        "verifier", None, operation_state
    )


async def test_a_seat_in_another_live_operation_keeps_the_writer(db):
    """One retired seat proves nothing while a running operation still names the task."""
    await _writer_seat(db, operation_state="completed", stage_state="passed")
    await _task(db, "parent-2", TaskStatus.IN_PROGRESS)
    async with db.immediate() as conn:
        await _episode(conn, parent_task_id="parent-2", episode_id="episode-2")
        await _operation(
            conn, operation_id="operation-2", state="active", parent_task_id="parent-2",
            episode_id="episode-2",
        )
        await _stage(
            conn, operation_id="operation-2", repair_task_id="delegate", state="active"
        )

    assert await db.get_retired_integration_writer("delegate") is None


async def test_a_live_candidate_reservation_keeps_its_writer(db):
    await _candidate_resolution(db, operation_state="escalated", resolution_state="reserved")
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(state="expired"))
        await conn.execute(update(integration_repair_operations).values(active_stage=1))
        await _stage(conn, ordinal=1, state="active")

    assert await db.get_retired_integration_writer("delegate") is None

    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(state="completed"))
    assert (await db.get_retired_integration_writer("delegate"))["disposition"] == "superseded"


@pytest.mark.parametrize("operation_state", ["completed", "escalated"])
async def test_an_accepted_candidate_repair_is_never_retired(db, operation_state):
    """Its pass close, or the stopped-writer recovery, still completes it truthfully."""
    await _candidate_resolution(db, operation_state=operation_state, resolution_state="accepted")
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(state="passed"))
        if operation_state == "escalated":
            await conn.execute(update(integration_repair_operations).values(active_stage=1))
            await _stage(conn, ordinal=1, state="active")

    assert await db.get_retired_integration_writer("delegate") is None

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_candidate_resolutions).values(
                state="rejected", rejection_evidence={"reason": "stale candidate"}
            )
        )
    proof = await db.get_retired_integration_writer("delegate")
    if operation_state == "completed":
        assert proof["disposition"] == "superseded"
    else:
        # A rejected reservation under a running operation is still its seat.
        assert proof is None
