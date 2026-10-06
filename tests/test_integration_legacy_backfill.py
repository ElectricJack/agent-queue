"""Git-proven backfill of pre-provenance completions (src/integration/legacy_backfill.py)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import insert, select

from src.database.tables import integration_legacy_deliveries, task_branch_origins
from src.integration.legacy_backfill import (
    CONTAINED_PROOF,
    EQUIVALENT_PROOF,
    backfill_legacy_deliveries,
)
from src.integration.train_sources import DatabaseBatches
from src.models import Task, TaskCompletion, TaskStatus
from tests import test_integration_train_sources as _sources
from tests.test_delivery_consumers import git
from tests.test_integration_train_sources import MAIN, completed, snapshot

pytestmark = pytest.mark.asyncio
world = _sources.world  # the shared train-sources fixture


async def legacy(world, tid, *, record_commits=True, land=False, squash=False,
                 delete_branch=False, close_row=True) -> str:
    """A completed task closed before provenance existed: no retained record."""
    db, origin = world.db, world.origin
    head = origin.work(tid)
    base = git(origin.clone, "rev-parse", f"{head}^")
    await db.create_task(Task(id=tid, project_id="p", repo_id="r", title=tid, description="",
                              branch_name=f"aq/{tid}", status=TaskStatus.IN_PROGRESS))
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id=f"{tid}-origin", task_id=tid, repository_id="r", branch_name=f"aq/{tid}",
            base_sha=base, creation_generation=0, reserved=True, materialized=True,
            created_at=time.time(),
        ))
    if close_row:
        await db.save_task_completion(TaskCompletion(
            id=f"close-{tid}", task_id=tid, outcome="pass",
            commits=[head] if record_commits else [], completed_at=time.time(),
        ))
    await db.transition_task(tid, TaskStatus.COMPLETED)
    if land:
        origin.land(tid)
    if squash:
        git(origin.clone, "fetch", "-q", "origin")
        git(origin.clone, "checkout", "-q", "-B", "main", "origin/main")
        git(origin.clone, "merge", "-q", "--squash", f"origin/aq/{tid}")
        git(origin.clone, "commit", "-q", "-m", f"squash {tid}")
        git(origin.clone, "push", "-q", "origin", "main")
    if delete_branch:
        git(origin.clone, "push", "-q", "origin", "--delete", f"aq/{tid}")
    return head


async def blocker_codes(world):
    blockers: list[dict] = []
    await DatabaseBatches(world.db).pending(MAIN, await snapshot(world), blockers=blockers)
    return {item["task_id"]: item["code"] for item in blockers}


async def test_backfill_proves_only_git_delivered_legacy_work(world):
    db = world.db
    merged = await legacy(world, "merged", land=True)
    tip = await legacy(world, "tip", record_commits=False, land=True)
    squashed = await legacy(world, "squashed", squash=True)
    await legacy(world, "undelivered")
    await legacy(world, "gone", record_commits=False, delete_branch=True)
    await completed(world, "modern")  # retained provenance: the train answers it

    before = await blocker_codes(world)
    assert {"merged", "tip", "squashed", "undelivered", "gone"} <= set(before)
    assert set(before.values()) == {"missing_git_provenance"}

    preview = await backfill_legacy_deliveries(db, "p")
    by_task = {item["task_id"]: item for item in preview["results"]}
    assert by_task["merged"]["proof"] == CONTAINED_PROOF
    assert (by_task["merged"]["via"], by_task["merged"]["delivered_sha"]) == (
        "completion_commit", merged)
    assert (by_task["tip"]["via"], by_task["tip"]["delivered_sha"]) == ("branch_tip", tip)
    assert by_task["squashed"]["proof"] == EQUIVALENT_PROOF
    assert by_task["squashed"]["delivered_sha"] == squashed
    assert {item["task_id"] for item in preview["unproven"]} == {"undelivered", "gone"}
    assert "modern" not in by_task
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_legacy_deliveries))).all() == []

    with pytest.raises(ValueError, match="nonblank reason"):
        await backfill_legacy_deliveries(db, "p", dry_run=False)
    applied = await backfill_legacy_deliveries(db, "p", dry_run=False, operator_id="op",
                                               reason="cutover backfill")
    assert applied["recorded"] == 3
    async with db._engine.connect() as conn:
        rows = {row.task_id: row for row in
                (await conn.execute(select(integration_legacy_deliveries))).all()}
    assert set(rows) == {"merged", "tip", "squashed"}
    assert rows["squashed"].proof == EQUIVALENT_PROOF
    assert "cutover backfill" in rows["tip"].reason and "branch_tip" in rows["tip"].reason

    after = await blocker_codes(world)
    assert set(after) == {"undelivered", "gone"}
    again = await backfill_legacy_deliveries(db, "p", dry_run=False, operator_id="op",
                                             reason="again")
    assert again["recorded"] == 0 and again["examined"] == 2


async def test_backfill_skips_reopened_generation(world):
    db = world.db
    await legacy(world, "merged", land=True)
    await backfill_legacy_deliveries(db, "p", dry_run=False, operator_id="op", reason="r")
    # A new completion generation owes new work; the old row no longer answers it.
    await db.transition_task("merged", TaskStatus.IN_PROGRESS)
    await db.save_task_completion(TaskCompletion(
        id="close-merged-2", task_id="merged", outcome="pass", commits=[],
        completed_at=time.time() + 5,
    ))
    await db.transition_task("merged", TaskStatus.COMPLETED)
    assert "merged" in await blocker_codes(world)


async def test_backfill_records_a_legacy_close_without_a_completion_row(world):
    db = world.db
    await legacy(world, "rowless", close_row=False, land=True)
    assert "rowless" in await blocker_codes(world)
    applied = await backfill_legacy_deliveries(db, "p", dry_run=False, operator_id="op",
                                               reason="r")
    assert [(r["task_id"], r["outcome"], r["via"]) for r in applied["results"]] == [
        ("rowless", "recorded", "branch_tip")]
    assert "rowless" not in await blocker_codes(world)
