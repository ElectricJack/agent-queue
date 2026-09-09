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
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    d = Database(lease_dsn("doctor.db"))
    await d.initialize()
    await d.create_project(Project(id="p", name="P"))
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


def _handler_with_pr_state(db, merged):
    """A CommandHandler stand-in whose ``gh`` probe returns *merged*.

    Also gives the project a checkout path — without one ``_pr_is_open``
    short-circuits to "unknown" and the probe is never reached.
    """
    db.get_project_workspace_path = AsyncMock(return_value="/repo")
    git = MagicMock()
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
    assert result.fixable is True
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
    assert result.fixable is True
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
async def test_stranded_fences_fix_re_reserves_for_the_same_owner(db):
    """The repair is a self-transfer: same owner, same role, fresh token."""
    from sqlalchemy import select

    from src.database.tables import integration_branch_owners
    from src.doctor.integration_checks import _fix_stranded_fences

    await db.create_task(
        Task(id="verify-4", project_id="p", title="Verify", description="", status=TaskStatus.READY)
    )
    owner_id = await _held_owner(db, task_id="verify-4")

    fixed = await _fix_stranded_fences(DoctorContext(config=SimpleNamespace(), db=db))

    assert fixed.fix_applied is True and fixed.severity is Severity.OK
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(
                select(integration_branch_owners).where(
                    integration_branch_owners.c.id == owner_id
                )
            )
        ).mappings().one()
    assert row["handoff_state"] == "reserved"
    assert row["owner_id"] == "verify-4"
    assert row["owner_role"] == "verifier"
    assert int(row["fence_token"]) == 28
    assert row["session_id"] is None and row["workspace_id"] is None

    # Idempotent: the row is no longer stranded, so a second pass finds nothing.
    again = await run_check(db, "integration.stranded_fences")
    assert again.severity is Severity.OK


@pytest.mark.asyncio
async def test_stranded_fences_ignores_a_collector_row(db):
    """A collector's owner is an operation, so none of the checks apply."""
    await _held_owner(db, task_id="operation-1", owner_role="collector")

    result = await run_check(db, "integration.stranded_fences")

    assert result.severity is Severity.OK
