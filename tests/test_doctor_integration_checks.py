"""``integration.unreviewed_prs`` — the alarm for silently unreviewed work.

The failure it watches for produces no error anywhere: tasks go COMPLETED,
PRs stay open, and nothing reports a problem.  These tests pin the three
judgments the check makes — has a PR, has no review task, PR is not already
merged — and the offline behaviour, since doctor has to work with no ``gh``.
"""
from __future__ import annotations

import time

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database import Database
from src.doctor import default_registry
from src.doctor.integration_checks import run_check
from src.doctor.models import Severity
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
