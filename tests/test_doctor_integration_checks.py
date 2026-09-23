"""``integration.unreviewed_prs`` — the alarm for silently unreviewed work.

The failure it watches for produces no error anywhere: tasks go COMPLETED,
PRs stay open, and nothing reports a problem.  These tests pin the three
judgments the check makes — has a PR, has no review task, PR is not already
merged — and the offline behaviour, since doctor has to work with no ``gh``.
"""
from __future__ import annotations

import time

from types import SimpleNamespace

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database import Database
from src.doctor import default_registry
from src.doctor.integration_checks import run_check
from src.doctor.models import Severity
from src.doctor.models import DoctorContext
from src.doctor.runner import run_doctor
from src.git.github_contracts import GitHubRepositoryBinding
from src.models import Project, SessionRecord, Task, TaskStatus
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    d = Database(lease_dsn("doctor.db"))
    await d.initialize()
    await d.create_project(Project(id="p", name="P", repo_url="https://github.com/o/r"))
    return d


async def _age(db, task_id: str, age_s: float) -> None:
    """Rewind ``updated_at`` past ``update_task``, which always stamps *now*."""
    import sqlalchemy as sa

    async with db._engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE tasks SET updated_at = :ts WHERE id = :id"),
            {"ts": time.time() - age_s, "id": task_id},
        )


async def _completed(db, task_id: str) -> None:
    await db.create_task(
        Task(id=task_id, project_id="p", title=f"T {task_id}", description="")
    )
    # Straight to COMPLETED: the check reads the row, and walking the real
    # DEFINED -> READY -> ... ladder would only test ``transition_task``.
    await db.update_task(task_id, status=TaskStatus.COMPLETED)


async def _completed_with_pr(db, task_id: str, *, pr_url: str, age_s: float = 60.0):
    await _completed(db, task_id)
    await db.update_task(task_id, branch_name=f"aq/{task_id}", pr_url=pr_url)
    await _age(db, task_id, age_s)


async def _orphaned_batch_operation(db):
    from sqlalchemy import insert, update

    from src.database.tables import integration_batches, integration_repair_operations, projects

    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                hierarchical_integration_mode="development",
                hierarchical_integration_desired_mode="development",
            )
        )
        await conn.execute(
            insert(integration_batches).values(
                id="legacy-batch",
                project_id="p",
                repository_id="repo",
                request_id="legacy-request",
                source_manifest_digest="sha256:" + "d" * 64,
                base_sha="a" * 40,
                lifecycle="repairing",
                integration_branch="aq/integration/legacy",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="legacy-operation",
                target_kind="batch",
                batch_id="legacy-batch",
                episode_id="legacy-batch",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=2.0,
            )
        )


def _handler_with_pr_state(db, merged):
    """A CommandHandler stand-in whose bound PR probe returns *merged*."""
    git = MagicMock()
    git.bind_github_repository = AsyncMock(
        return_value=GitHubRepositoryBinding(1, "o/r")
    )
    git.acheck_pr_merged = AsyncMock(return_value=merged)
    orchestrator = MagicMock()
    orchestrator.git = git
    handler = MagicMock()
    handler.orchestrator = orchestrator
    return handler


@pytest.mark.asyncio
async def test_warns_on_completed_task_with_open_pr_and_no_review(db):
    await _completed_with_pr(db, "stranded", pr_url="https://github.com/o/r/pull/1")

    result = await run_check(
        db, "integration.unreviewed_prs", handler=_handler_with_pr_state(db, False)
    )

    assert result.severity is Severity.WARN
    assert result.data["count"] == 1
    finding = result.data["tasks"][0]
    assert finding["task_id"] == "stranded"
    assert finding["pr_url"] == "https://github.com/o/r/pull/1"
    assert finding["pr_open"] is True


@pytest.mark.asyncio
async def test_orphaned_operations_warns_until_the_operation_is_cancelled(db):
    from sqlalchemy import update

    from src.database.tables import integration_repair_operations

    await _orphaned_batch_operation(db)

    result = await run_check(db, "integration.orphaned_operations")

    assert result.severity is Severity.WARN
    assert result.data["count"] == 1
    finding = result.data["operations"][0]
    assert finding["project_id"] == "p"
    assert finding["id"] == "legacy-operation"
    assert finding["target"] == {"kind": "batch", "id": "legacy-batch"}
    assert finding["cancel_preserving"].startswith(
        "aq integration cancel-preserving legacy-operation --reason"
    )
    assert "legacy-operation" in result.detail

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "legacy-operation")
            .values(state="cancelled")
        )

    resolved = await run_check(db, "integration.orphaned_operations")

    assert resolved.severity is Severity.OK
    assert resolved.data == {"count": 0, "operations": []}


@pytest.mark.asyncio
async def test_ok_when_the_review_task_exists(db):
    await _completed_with_pr(db, "reviewed", pr_url="https://github.com/o/r/pull/2")
    # The row ``per-task-review``'s ``ensure_task`` would have written.
    await db.create_task(
        Task(
            id="review-1",
            project_id="p",
            title="Review: reviewed",
            description="",
            dedup_key="review:task:reviewed",
        )
    )

    result = await run_check(
        db, "integration.unreviewed_prs", handler=_handler_with_pr_state(db, False)
    )

    assert result.severity is Severity.OK, result.detail


@pytest.mark.asyncio
async def test_merged_pr_is_not_stranded(db):
    """A merged PR landed, so it is not the failure this check is looking for."""
    await _completed_with_pr(db, "landed", pr_url="https://github.com/o/r/pull/3")

    result = await run_check(
        db, "integration.unreviewed_prs", handler=_handler_with_pr_state(db, True)
    )

    assert result.severity is Severity.OK, result.detail


@pytest.mark.asyncio
async def test_ignores_completions_without_a_pr_and_older_than_the_window(db):
    await _completed(db, "no-pr")

    await _completed_with_pr(
        db, "ancient", pr_url="https://github.com/o/r/pull/4", age_s=48 * 3600
    )

    result = await run_check(
        db, "integration.unreviewed_prs", handler=_handler_with_pr_state(db, False)
    )

    assert result.severity is Severity.OK, result.detail


@pytest.mark.asyncio
async def test_reports_when_gh_is_unavailable(db):
    """Offline, an unverifiable PR still warns — silence is the bug being hunted."""
    await _completed_with_pr(db, "offline", pr_url="https://github.com/o/r/pull/5")

    # No handler at all: nothing to probe ``gh`` with.
    result = await run_check(db, "integration.unreviewed_prs", handler=None)

    assert result.severity is Severity.WARN
    assert result.data["tasks"][0]["pr_open"] is None


@pytest.mark.asyncio
async def test_check_is_registered_in_the_default_registry():
    registry = default_registry()
    ids = {c.id for c in registry.checks()}
    assert "integration.unreviewed_prs" in ids


@pytest.mark.asyncio
async def test_operational_check_reports_modes_blockers_and_human_attention_read_only(db):
    handler = MagicMock()
    handler.execute = AsyncMock(
        return_value={
            "outcome": "status",
            "project_id": "p",
            "effective_mode": "train",
            "desired_mode": "disabled",
            "generation": 8,
            "draining": True,
            "ready": False,
            "rollout_ready": False,
            "blockers": [
                {
                    "code": "hosted_workflow_variables_unavailable",
                    "detail": "functional integration dependency is unavailable",
                    "ref": "repo",
                }
            ],
            "blocker_digest": "sha256:" + "c" * 64,
            "certification": {"status": "not_performed", "deferred": ["security"]},
            "repair": [{"id": "op-1", "state": "human_required"}],
            "cleanup_pending": [
                {
                    "batch_id": "batch-1",
                    "identity": "refs/heads/aq/integration/batch-1",
                    "state": "conflict",
                    "irreversible": False,
                }
            ],
        }
    )

    result = await run_check(db, "integration.operational", handler=handler)

    assert result.severity is Severity.WARN
    assert result.fixable is False
    project = result.data["projects"][0]
    assert project["effective_mode"] == "train"
    assert project["desired_mode"] == "disabled"
    assert project["draining"] is True
    assert project["generation"] == 8
    assert project["blockers"][0]["code"] == "hosted_workflow_variables_unavailable"
    assert project["human_required"] == [{"operation_id": "op-1", "state": "human_required"}]
    assert project["cleanup_attention"][0]["batch_id"] == "batch-1"
    assert project["certification"]["status"] == "not_performed"
    handler.execute.assert_awaited_once_with("integration_status", {"project_id": "p"})


@pytest.mark.asyncio
async def test_operational_check_is_info_for_disabled_unconfigured_projects(db):
    handler = MagicMock()
    handler.execute = AsyncMock(
        return_value={
            "outcome": "status",
            "project_id": "p",
            "effective_mode": "disabled",
            "desired_mode": "disabled",
            "generation": 0,
            "draining": False,
            "ready": False,
            "blockers": [{"code": "repository_not_designated", "detail": "missing", "ref": "p"}],
            "certification": {"status": "not_performed"},
            "repair": [],
            "cleanup_pending": [],
        }
    )

    result = await run_check(db, "integration.operational", handler=handler)

    assert result.severity is Severity.INFO
    assert "disabled" in result.detail
    assert result.data["projects"][0]["blockers"][0]["code"] == "repository_not_designated"


@pytest.mark.asyncio
async def test_operational_doctor_fix_mode_never_mutates_or_calls_control_commands(db):
    handler = MagicMock()
    handler.execute = AsyncMock(
        return_value={
            "outcome": "status",
            "project_id": "p",
            "effective_mode": "observe",
            "desired_mode": "observe",
            "generation": 2,
            "draining": False,
            "ready": True,
            "blockers": [],
            "certification": {"status": "not_performed"},
            "repair": [],
            "cleanup_pending": [],
        }
    )
    checks = [check for check in default_registry().checks() if check.id == "integration.operational"]
    assert len(checks) == 1
    assert checks[0].fix is None

    outcome = await run_doctor(
        default_registry(),
        DoctorContext(config=SimpleNamespace(), db=db, handler=handler),
        fix=True,
        only=["integration.operational"],
    )

    assert outcome["exit_code"] == 0
    assert outcome["summary"]["fixes_applied"] == 0
    handler.execute.assert_awaited_once_with("integration_status", {"project_id": "p"})


@pytest.mark.asyncio
async def test_operational_check_surfaces_schema_or_status_failure(db):
    handler = MagicMock()
    handler.execute = AsyncMock(side_effect=RuntimeError("no such table: integration_batches"))

    result = await run_check(db, "integration.operational", handler=handler)

    assert result.severity is Severity.ERROR
    assert "db.migrations" in result.detail
    assert "integration_batches" in result.data["errors"][0]["error"]


# -- integration.branch_discards ---------------------------------------------
#
# A discard that parks is invisible otherwise: the task it belonged to is
# already deleted, so no surface still shows the branch.  Doctor is the only
# place the leftover ref gets named.


async def _parked_origin(db, *, task_id: str, state: str, error: str) -> str:
    import uuid

    from sqlalchemy import insert

    from src.database.tables import task_branch_origins

    origin_id = str(uuid.uuid4())
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id=origin_id,
                task_id=task_id,
                repository_id="repo",
                parent_ref="main",
                base_sha="a" * 40,
                creation_generation=0,
                reserved=True,
                materialized=True,
                materialized_at=1.0,
                created_at=1.0,
                retired_at=2.0,
                discard_state=state,
                discard_requested_at=2.0,
                discard_attempts=3,
                discard_last_error=error,
            )
        )
    return origin_id


@pytest.mark.asyncio
async def test_branch_discards_is_ok_when_nothing_is_parked(db):
    result = await run_check(db, "integration.branch_discards")

    assert result.severity is Severity.OK


@pytest.mark.asyncio
async def test_branch_discards_names_the_leftover_ref(db):
    await _parked_origin(db, task_id="gone", state="conflict", error="branch has an active owner")

    result = await run_check(db, "integration.branch_discards")

    assert result.severity is Severity.WARN
    assert result.data["count"] == 1
    assert result.data["discards"][0]["branch"] == "aq/gone"
    assert "aq/gone" in result.detail


@pytest.mark.asyncio
async def test_branch_discards_fix_re_arms_rather_than_deleting(db):
    """The repair is another attempt, never doctor deleting a ref itself."""
    from sqlalchemy import select

    from src.database.tables import task_branch_origins

    origin_id = await _parked_origin(
        db, task_id="gone", state="failed", error="network down"
    )

    result = await run_check(db, "integration.branch_discards")
    assert result.severity is Severity.WARN

    from src.doctor.integration_checks import _fix_branch_discards

    fixed = await _fix_branch_discards(DoctorContext(config=SimpleNamespace(), db=db))

    assert fixed.fix_applied is True
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(
                select(task_branch_origins).where(task_branch_origins.c.id == origin_id)
            )
        ).mappings().one()
    assert row["discard_state"] == "pending"
    assert row["discard_attempts"] == 0
    assert row["discard_last_error"] is None


# -- integration.stranded_fences ---------------------------------------------
#
# The wedge this names is silent and self-repeating: the task stays READY, so
# the scheduler keeps offering it, and every claim of it dies in
# ``_integration_owner_fence`` with "canonical branch is not reserved by this
# task".  Nothing surfaces except a pool worker burning a claim each time.
#
# The check reports candidates from the database snapshot, but its fix delegates
# every release decision to ``OwnerRecovery``, which takes the provider and
# checkout proofs doctor itself cannot.


async def _held_owner(
    db,
    *,
    task_id: str,
    owner_role: str = "verifier",
    handoff_state: str = "attached",
    ref: str = "aq/keen-harbor",
) -> str:
    from sqlalchemy import insert

    from src.database.tables import integration_branch_owners

    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id=f"owner-{task_id}",
                repository_id="repo",
                ref=ref,
                owner_id=task_id,
                owner_role=owner_role,
                fence_token=27,
                handoff_state=handoff_state,
                session_id="dead-session",
                workspace_id="dead-workspace",
                created_at=1.0,
                updated_at=2.0,
            )
        )
    return f"owner-{task_id}"


@pytest.mark.asyncio
async def test_stranded_fences_is_ok_when_nothing_is_held(db):
    result = await run_check(db, "integration.stranded_fences")

    assert result.severity is Severity.OK


@pytest.mark.asyncio
async def test_stranded_fences_names_a_requeued_task_holding_its_branch(db):
    await db.create_task(
        Task(id="verify-1", project_id="p", title="Verify", description="", status=TaskStatus.READY)
    )
    await _held_owner(db, task_id="verify-1")

    result = await run_check(db, "integration.stranded_fences")

    assert result.severity is Severity.WARN
    assert result.data["count"] == 1
    assert result.data["fences"][0]["ref"] == "aq/keen-harbor"
    assert result.data["fences"][0]["owner_role"] == "verifier"
    assert "aq/keen-harbor" in result.detail


@pytest.mark.asyncio
async def test_stranded_fences_leaves_a_live_writer_alone(db):
    """An IN_PROGRESS owner is a writer at work, not a stranded row."""
    await db.create_task(
        Task(
            id="verify-2",
            project_id="p",
            title="Verify",
            description="",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await _held_owner(db, task_id="verify-2")

    result = await run_check(db, "integration.stranded_fences")

    assert result.severity is Severity.OK


@pytest.mark.asyncio
async def test_stranded_fences_leaves_a_held_workspace_alone(db):
    """A workspace still locked by the owner may be a real checkout."""
    from src.models import RepoSourceType, Workspace

    await db.create_task(
        Task(id="verify-3", project_id="p", title="Verify", description="", status=TaskStatus.READY)
    )
    await db.create_workspace(
        Workspace(
            id="ws",
            project_id="p",
            workspace_path="/tmp/ws-verify-3",
            source_type=RepoSourceType.CLONE,
            locked_by_task_id="verify-3",
        )
    )
    await _held_owner(db, task_id="verify-3")

    result = await run_check(db, "integration.stranded_fences")

    assert result.severity is Severity.OK


@pytest.mark.asyncio
async def test_stranded_fences_registers_the_guarded_recovery_fix(db):
    """Doctor delegates every actual release to the guarded recovery service."""
    from importlib import import_module

    from src.doctor.integration_checks import integration_checks

    check = next(c for c in integration_checks() if c.id == "integration.stranded_fences")

    assert check.fix is import_module("src.doctor.integration_checks")._fix_stranded_fences
    assert check.timeout_s == 60.0


@pytest.mark.asyncio
async def test_stranded_fences_check_never_writes_the_ownership_row(db):
    """The check phase leaves the fence exactly as it found it.

    Bumping the fence or clearing the attachment on a snapshot alone would
    hand the branch to the next claim while a stopped-looking writer may still
    hold a dirty or unpublished checkout, and would silently undo a guarded
    rebind that changes the attachment without changing the owner fields.
    """
    from sqlalchemy import select

    from src.database.tables import integration_branch_owners

    await db.create_task(
        Task(id="verify-4", project_id="p", title="Verify", description="", status=TaskStatus.READY)
    )
    owner_id = await _held_owner(db, task_id="verify-4")

    async def _row():
        async with db._engine.connect() as conn:
            return dict(
                (
                    await conn.execute(
                        select(integration_branch_owners).where(
                            integration_branch_owners.c.id == owner_id
                        )
                    )
                ).mappings().one()
            )

    before = await _row()
    result = await run_check(db, "integration.stranded_fences")
    after = await _row()

    assert result.severity is Severity.WARN
    assert result.fixable is True
    assert after == before
    assert after["handoff_state"] == "attached"
    assert int(after["fence_token"]) == 27
    assert after["session_id"] == "dead-session"


@pytest.mark.asyncio
async def test_stranded_fences_keeps_a_stopping_writer_that_is_still_wanted(db):
    """A stopped process with desired_state=running can still be restarted to write."""
    await db.create_task(
        Task(id="verify-5", project_id="p", title="Verify", description="", status=TaskStatus.READY)
    )
    await db.create_session(
        SessionRecord(
            id="stopping-writer",
            project_id="p",
            task_id="verify-5",
            profile_id="test",
            harness="test",
            provider="test",
            name="stopping-writer",
            lifecycle="task",
            work_dir="/tmp/stopping-writer",
            epoch="epoch",
            instance_token="token",
            started_at=time.time(),
            state="stopped",
            desired_state="running",
        )
    )
    await _held_owner(db, task_id="verify-5")

    result = await run_check(db, "integration.stranded_fences")

    assert result.severity is Severity.OK


@pytest.mark.asyncio
async def test_stranded_fences_ignores_a_collector_row(db):
    """A collector's owner is an operation, so none of the checks apply."""
    await _held_owner(db, task_id="operation-1", owner_role="collector")

    result = await run_check(db, "integration.stranded_fences")

    assert result.severity is Severity.OK


# --------------------------------------------------------------------------
# integration.stranded_dependents
# --------------------------------------------------------------------------


async def test_stranded_dependents_reports_cleaned_commitsless_blocker(db):
    import sqlalchemy as sa

    from src.database.tables import projects
    from src.models import RepoConfig, RepoSourceType, TaskCompletion

    await _development_project(db)
    await db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE, url="/repo"
    ))
    async with db._engine.begin() as conn:
        await conn.execute(
            sa.update(projects).where(projects.c.id == "p").values(integration_repository_id="r")
        )
    source_sha = "a" * 40
    await db.create_task(Task(
        id="blocker", project_id="p", repo_id="r", title="blocker", description="",
        branch_name="aq/blocker", status=TaskStatus.COMPLETED,
    ))
    await db.create_task(Task(
        id="dependent", project_id="p", repo_id="r", title="dependent", description="",
        branch_name="aq/dependent", status=TaskStatus.COMPLETED,
    ))
    await db.add_dependency("dependent", "blocker")
    await db.save_task_completion(TaskCompletion(
        id="blocker-close", task_id="blocker", outcome="pass", commits=[], completed_at=time.time(),
    ))
    await _batch(
        db,
        "delivered-blocker",
        state="delivered",
        manifest=[{"task_id": "blocker", "source_sha": source_sha}],
        branch_cleanup={
            "state": "complete",
            "deleted": [{"branch": "aq/blocker", "sha": source_sha, "kind": "task"}],
        },
    )

    result = await run_check(db, "integration.stranded_dependents")

    assert result.severity is Severity.ERROR
    assert "dependent" in result.detail and "blocker" in result.detail
    assert "Restore the blocker's branch" in result.detail
    assert result.data["dependents"] == [{
        "project_id": "p",
        "dependent_task_id": "dependent",
        "blocker_task_id": "blocker",
        "delivery_id": "delivered-blocker",
        "source_sha": source_sha,
    }]


def test_stranded_dependents_is_registered():
    registry = default_registry()
    assert "integration.stranded_dependents" in registry.ids()


# --------------------------------------------------------------------------
# integration.development_publisher_stalled
# --------------------------------------------------------------------------


async def _development_project(db, project_id="p"):
    import sqlalchemy as sa

    from src.database.tables import projects

    async with db._engine.begin() as conn:
        await conn.execute(
            sa.update(projects)
            .where(projects.c.id == project_id)
            .values(hierarchical_integration_mode="development")
        )


async def _batch(
    db,
    batch_id,
    *,
    manifest,
    state="parked",
    diagnostic=None,
    branch_cleanup=None,
    project_id="p",
):
    import sqlalchemy as sa

    from src.database.tables import development_deliveries

    now = time.time()
    evidence = {"kind": "validation"}
    if diagnostic is not None:
        evidence["publisher_diagnostic"] = diagnostic
    if branch_cleanup is not None:
        evidence["branch_cleanup"] = branch_cleanup
    async with db._engine.begin() as conn:
        await conn.execute(
            sa.insert(development_deliveries).values(
                id=batch_id,
                project_id=project_id,
                repository_id="r",
                target_ref="refs/heads/main",
                expected_sha=None,
                prepared_sha=None,
                state=state,
                manifest=manifest,
                evidence=evidence,
                reason="selected validation failed",
                created_at=now,
                updated_at=now,
            )
        )


async def test_publisher_stalled_names_the_batch_and_the_cause(db):
    """The agent-queue incident: the same fault on tick after tick."""
    await _development_project(db)
    await _batch(
        db,
        "b4f49c0c",
        manifest=[{"task_id": "development-repair-8d6e", "source_sha": "a" * 40}],
        diagnostic={
            "kind": "repair_dispatch_failed",
            "task_ids": ["development-repair-8d6e"],
            "detail": "ValueError: repair source 'development-repair-8d6e' is not in project 'p'",
            "consecutive_ticks": 42,
            "first_failed_at": time.time() - 86_400,
            "last_failed_at": time.time(),
        },
    )

    result = await run_check(db, "integration.development_publisher_stalled")

    assert result.severity is Severity.ERROR
    assert "b4f49c0c" in result.detail
    assert "repair_dispatch_failed" in result.detail
    assert result.data["stalls"][0]["consecutive_ticks"] == 42
    assert result.data["stalls"][0]["task_ids"] == ["development-repair-8d6e"]


async def test_publisher_stalled_lets_one_failing_tick_pass(db):
    await _development_project(db)
    await _batch(
        db,
        "fresh",
        manifest=[{"task_id": "one", "source_sha": "a" * 40}],
        diagnostic={
            "kind": "repair_source_missing",
            "task_ids": ["one"],
            "detail": "one repair source resolves nowhere",
            "consecutive_ticks": 1,
            "first_failed_at": time.time(),
        },
    )

    result = await run_check(db, "integration.development_publisher_stalled")

    assert result.severity is Severity.OK


async def test_publisher_stalled_warns_after_repeated_candidate_skip(db):
    await _development_project(db)
    await db.create_task(Task(
        id="child", project_id="p", title="child", description="",
        branch_name="aq/child", status=TaskStatus.COMPLETED,
    ))
    evidence = {
        "reason": "dependency_unavailable", "dependency_id": "parent",
        "consecutive_ticks": 2, "first_skipped_at": time.time() - 600,
    }
    await db.set_task_meta("child", "development_publisher_skip", evidence)
    assert (await run_check(db, "integration.development_publisher_stalled")).severity is Severity.OK

    await db.set_task_meta("child", "development_publisher_skip", {
        **evidence, "consecutive_ticks": 3,
    })
    result = await run_check(db, "integration.development_publisher_stalled")
    assert result.severity is Severity.WARN
    assert "child" in result.detail and "parent" in result.detail
    assert "dependency_unavailable" in result.detail
    assert result.data["stalls"][0]["dependency_id"] == "parent"


async def test_publisher_stalled_flags_a_pushed_but_uncollected_repair(db):
    """A repair closed pass whose branch no batch ever picked up."""
    from src.models import TaskCompletion

    await _development_project(db)
    repair = "development-repair-uncollected"
    await db.create_task(
        Task(
            id=repair,
            project_id="p",
            title="repair",
            description="",
            branch_name=f"aq/{repair}",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.save_task_completion(
        TaskCompletion(
            id="c1", task_id=repair, outcome="pass", completed_at=time.time() - 7_200
        )
    )

    result = await run_check(db, "integration.development_publisher_stalled")

    assert result.severity is Severity.ERROR
    stall = result.data["stalls"][0]
    assert stall["cause"] == "repair_branch_uncollected"
    assert stall["task_ids"] == [repair]
    assert f"aq/{repair}" in stall["detail"]


async def test_publisher_stalled_accepts_a_repair_the_publisher_did_pick_up(db):
    """Collected-then-parked is the publisher working, not stalling."""
    from src.models import TaskCompletion

    await _development_project(db)
    repair = "development-repair-collected"
    await db.create_task(
        Task(
            id=repair,
            project_id="p",
            title="repair",
            description="",
            branch_name=f"aq/{repair}",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.save_task_completion(
        TaskCompletion(
            id="c1", task_id=repair, outcome="pass", completed_at=time.time() - 7_200
        )
    )
    await _batch(db, "picked-up", manifest=[{"task_id": repair, "source_sha": "a" * 40}])

    result = await run_check(db, "integration.development_publisher_stalled")

    assert result.severity is Severity.OK


async def test_publisher_stalled_is_quiet_without_a_development_project(db):
    repair = "development-repair-other-mode"
    await db.create_task(
        Task(id=repair, project_id="p", title="r", description="",
             branch_name=f"aq/{repair}", status=TaskStatus.COMPLETED)
    )
    result = await run_check(db, "integration.development_publisher_stalled")
    assert result.severity is Severity.OK


def test_publisher_stalled_is_registered():
    registry = default_registry()
    assert "integration.development_publisher_stalled" in registry.ids()
