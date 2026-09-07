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


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "doctor.db"))
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
