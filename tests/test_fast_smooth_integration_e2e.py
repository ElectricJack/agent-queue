"""Connected S4 canaries: real Git, disposable PostgreSQL, controlled CI, no LLM.

The adjacent owner suites cover the section 14 boundary matrix. These canaries
exercise transitions across those boundaries and verify exact retained sources,
including after restart and an attributed split. They do not activate a project.
"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from src.database.tables import integration_batches, projects, repos, task_delivery_receipts, tasks
from src.integration.batches import BatchService, BatchStore
from src.integration.train import TrainTarget
from src.models import Task
from tests import test_integration_batches as batch_fixtures
from tests.test_integration_close_merge import scenario as scenario
from tests.test_integration_fixers import (
    db as db,
    live_fixer as live_fixer,
    notices,
    principal_for,
    split_env as split_env,
)
from tests.test_integration_gitops import (
    LocalGit,
    commit,
    git,
    git_history_template as git_history_template,
    setup as setup,
)
from tests.test_integration_train_sources import completed, lane, world as world

DEV = TrainTarget("p", "r", "refs/heads/dev", "root")
original_batch_env = batch_fixtures.batch_env


@pytest.fixture
async def batch_env(original_batch_env):
    """Run the split owner fixture on a configured dev lane."""
    db, ops, repo, base, head, store, batch, members, *rest = original_batch_env
    await store.set_intent(batch.id, "aborted")
    batch = replace(batch, id="s4-dev", target_ref=DEV.target_ref)
    git(ops.git.remote_path, "update-ref", DEV.target_ref, base)
    await store.freeze(batch, members, trees={
        m.task_id: git(repo.store, "rev-parse", m.source_sha + "^{tree}") for m in members
    })
    return db, ops, repo, base, head, store, batch, members, *rest


async def test_standalone_then_queued_stack_and_parallel_sources_reach_dev_after_restart(
    world, record_property,
):
    origin, db = world.origin, world.db
    base = git(origin.url, "rev-parse", "main")
    git(origin.url, "update-ref", DEV.target_ref, base)
    async with db.immediate() as conn:
        await conn.execute(update(projects).values(repo_default_branch="dev"))
        await conn.execute(update(repos).values(default_branch="dev"))
    transport = LocalGit(Path(origin.url))
    train, checks, _ = lane(world, transport, target=DEV, settling=True)
    standalone = await completed(world, "standalone")
    first = await train.visit(DEV)
    assert first.state == "testing", first
    store = BatchStore(db)
    assert [m.task_id for m in await store.members(first.batch_id)] == ["standalone"]
    assert (await db.get_task("standalone")).parent_task_id is None

    prerequisite = await completed(world, "prerequisite")
    dependent = commit(origin.clone, {"dependent.txt": "needs prerequisite\n"}, base=prerequisite)
    git(origin.clone, "push", "origin", dependent + ":refs/heads/aq/dependent")
    await completed(world, "dependent", needs=("prerequisite",), head=dependent,
                    source_base=prerequisite)
    parallel = await completed(world, "parallel")
    queued = await train.visit(DEV)
    assert queued.batch_id == first.batch_id and queued.state == "testing", queued
    assert {m["task_id"] for m in queued.detail["dispatch"]["queued_members"]} == {
        "prerequisite", "dependent", "parallel",
    }
    # Losing every event and in-memory lane still retains the same frozen CI.
    train, checks, _ = lane(world, transport, target=DEV, settling=True)
    resumed = await train.visit(DEV)
    assert (resumed.batch_id, resumed.candidate_sha) == (first.batch_id, first.candidate_sha)
    checks.green.add(first.candidate_sha)
    successor = await train.visit(DEV)
    assert successor.state == "testing" and successor.batch_id != first.batch_id, successor
    assert git(origin.url, "rev-parse", "dev") == first.candidate_sha
    assert successor.target_sha == first.candidate_sha
    members = await store.members(successor.batch_id)
    assert {m.task_id for m in members} == {"prerequisite", "dependent", "parallel"}
    ids = [m.task_id for m in members]
    assert ids.index("prerequisite") < ids.index("dependent")
    # Green on the predecessor cannot authorize the queued candidate.
    assert (await train.visit(DEV)).state == "testing"
    checks.green.add(successor.candidate_sha)
    delivered = await train.visit(DEV)
    assert delivered.state == "delivered", delivered
    target = git(origin.url, "rev-parse", "dev")
    assert target == successor.candidate_sha
    for source in (standalone, prerequisite, dependent, parallel):
        assert git(origin.url, "merge-base", "--is-ancestor", source, target) == ""
    assert (await train.visit(DEV)).state == "idle"
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(integration_batches)) == 2
        assert await conn.scalar(select(func.count()).select_from(task_delivery_receipts)) == 0
    record_property("manifest", json.dumps({
        "target_ref": DEV.target_ref, "target_oid": target,
        "sources": dict(standalone=standalone, prerequisite=prerequisite,
                        dependent=dependent, parallel=parallel),
        "batches": [first.batch_id, successor.batch_id], "process_stalls": [],
        "checks": "controlled exact-candidate green", "receipts": 0,
    }))


async def test_red_split_lands_healthy_then_all_deferred_sources_and_companion_after_restart(
    split_env, record_property,
):
    (db, ops, repo, base, store, batch, members, green, snapshot, service,
     runtime, splitter, evidence, fixed) = split_env
    before = git(ops.git.remote_path, "rev-parse", batch.target_ref)
    assert before == base
    assert (await service.visit(batch, members, await snapshot(batch))).state == "testing"
    result = await splitter.split(runtime, batch.id, evidence, policy=None,
                                  authorize=AsyncMock(return_value=True))
    assert result["retained"] == ["task"] and result["deferred"] == ["bad", "dependent"]
    assert await store.members(batch.id) == members
    reduced = await store.get(result["replacement_batch_id"])
    retained = await store.members(reduced.id)
    candidate = await service.visit(reduced, retained, await snapshot(reduced))
    assert candidate.state == "testing", candidate
    assert not await ops.is_ancestor(repo, members[1].source_sha, candidate.candidate_sha)
    green.add(candidate.candidate_sha)
    assert (await service.visit(reduced, retained, await snapshot(reduced))).state == "delivered"
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == reduced.id)
                           .values(lifecycle="promoted"))
    # Reconstruct from durable state; no release, refile or manual source merge.
    splitter = type(splitter)(db, clock=lambda: 1000)
    service = BatchService(BatchStore(db), ops, publish=service.publish,
                           eligible=service.eligible, gate=service.gate, attest=service.attest)
    target = TrainTarget("p", "r", batch.target_ref)
    successor = await splitter.resume(target, await snapshot(batch), service)
    assert successor is not None
    successor_members = await store.members(successor.id)
    candidate = await service.visit(successor, successor_members, await snapshot(successor))
    assert candidate.state == "testing", candidate
    assert await ops.is_ancestor(repo, fixed, candidate.candidate_sha)
    assert git(repo.store, "show", candidate.candidate_sha + ":saved-fix.txt") == "must survive split"
    green.add(candidate.candidate_sha)
    assert (await service.visit(successor, successor_members,
                                await snapshot(successor))).state == "delivered"
    final = git(ops.git.remote_path, "rev-parse", batch.target_ref)
    assert final == candidate.candidate_sha
    for source in [fixed, *(m.source_sha for m in members)]:
        assert git(ops.git.remote_path, "merge-base", "--is-ancestor", source, final) == ""
    original = await db.get_integration_batch(batch.id)
    assert original["cleanup_state"] == "retained"
    record_property("manifest", json.dumps({
        "target_ref": batch.target_ref, "target_oid": final,
        "sources": {m.task_id: m.source_sha for m in members}, "companion_oid": fixed,
        "batches": [batch.id, reduced.id, successor.id], "process_stalls": [],
        "checks": "controlled attributed red, then exact-candidate green",
        "deferred": result["deferred"], "original_preserved": True,
    }))


async def test_closing_siblings_race_and_lost_publication_reply_preserve_both_authors(
    scenario, record_property,
):
    s = scenario
    sibling = commit(s.checkout, {"sibling.txt": "parallel author\n"}, base=s.base)
    git(s.checkout, "push", "origin", sibling + ":refs/heads/sibling")
    await s.db.create_task(Task(id="sibling", project_id="p", title="Sibling", description="",
                                parent_task_id="parent", branch_name="sibling"))
    row = await s.runtime.store.proposal(
        task=SimpleNamespace(id="sibling", project_id="p", repo_id="r", claim_epoch=1),
        session=SimpleNamespace(id="sibling-author"), source=sibling,
        parent=SimpleNamespace(id="parent", branch_name="parent"),
        args={"outcome": "pass", "summary": "parallel author"},
    )
    s.manager.ambiguous = True
    results = await asyncio.gather(s.runtime.run(s.row["id"]), s.runtime.run(row["id"]))
    for operation, result in zip((s.row, row), results, strict=True):
        if not result["completed"]:
            result = await s.runtime.run(operation["id"])
        assert result["completed"], result
    head = git(s.remote, "rev-parse", "parent")
    for source in (s.source, sibling):
        assert git(s.remote, "merge-base", "--is-ancestor", source, head) == ""
    pushes = s.manager.pushes
    for operation in (s.row, row):
        assert (await s.runtime.run(operation["id"]))["completed"]
    assert s.manager.pushes == pushes
    record_property("manifest", json.dumps({
        "target_ref": "refs/heads/parent", "target_oid": head,
        "sources": {"child": s.source, "sibling": sibling}, "process_stalls": [],
        "checks": "controlled exact-candidate green", "replay_extra_pushes": 0,
    }))


async def test_conflicted_author_restart_rechecks_fix_against_moved_parent(
    scenario, record_property,
):
    s = scenario
    source = commit(s.checkout, {"base.txt": "author edit\n"}, base=s.source)
    git(s.checkout, "push", "origin", source + ":refs/heads/child")
    parent = commit(s.checkout, {"base.txt": "parent edit\n"}, base=s.base)
    git(s.checkout, "push", "origin", parent + ":refs/heads/parent")
    row = await s.runtime.store.save(s.row, source_oid=source)
    pending = await s.runtime.run(row["id"])
    assert pending["state"] == "needs_author" and not pending["completed"]
    assert git(s.remote, "rev-parse", "parent") == parent
    fixed = commit(Path(pending["candidate_path"]), {
        "base.txt": "resolved by author\n", "retained-fix.txt": "survives restart\n",
    })
    newer = commit(s.checkout, {"parallel.txt": "parent advanced\n"}, base=parent)
    git(s.checkout, "push", "origin", newer + ":refs/heads/parent")
    restarted = type(s.runtime)(s.runtime.orch)
    for port in ("authorize", "policy", "check", "handoff", "observe"):
        setattr(restarted, port, getattr(s.runtime, port))
    s.runtime.check.reset_mock()
    delivered = await restarted.run(row["id"])
    assert delivered["completed"], delivered
    head = git(s.remote, "rev-parse", "parent")
    assert head == delivered["candidate_oid"]
    assert git(s.checkout, "show", "-s", "--format=%P", head).split() == [newer, fixed]
    for retained in (source, newer, fixed):
        assert git(s.remote, "merge-base", "--is-ancestor", retained, head) == ""
    assert git(s.remote, "show", head + ":retained-fix.txt") == "survives restart"
    assert {call.args[0]["candidate_oid"] for call in restarted.check.await_args_list} == {head}
    record_property("manifest", json.dumps({
        "target_ref": "refs/heads/parent", "target_oid": head,
        "sources": {"child": source, "advanced_parent": newer}, "companion_oid": fixed,
        "checks": "controlled exact-candidate green after restart and target advance",
        "process_stalls": [],
    }))


@pytest.mark.parametrize("budget", ["time", "rounds"])
@pytest.mark.parametrize("live_fixer", [{"repair_policy": {
    "primary_seconds": 1800, "primary_attempts": 3, "debug_intelligence_class": "deep-high",
}}], indirect=True)
async def test_fixer_budget_restart_escalates_once_without_refiling_or_waking(
    db, live_fixer, budget, record_property,
):
    """Exercise the implemented batch-family budget, not per-group fan-out."""
    service, runtime, orch, row, _, source, _ = live_fixer
    original = (await db.get_integration_batch("real"))["fixer_state"]
    assert original["deadline"] == original["created_at"] + 1800
    assert original["max_rounds"] == 3
    provider = orch.session_providers.create("fake")
    heads = [source]
    principal = principal_for(row)
    for number in range(1, 4):
        head = commit(Path(row.work_dir), {"fix.txt": f"red round {number}\n"})
        assert (await service.command(principal, action="push"))["head_sha"] == head
        heads.append(head)
        if budget == "time" or number == 3:
            break
        result = await service.allocate(
            "real", target_ref=original["target_ref"], head_sha=head,
            authorize=AsyncMock(return_value=True),
        )
        assert result["outcome"] == "exists"
    before = await notices(db)
    assert len(before) == (0 if budget == "time" else 2)
    # Lose the service object before the exhausting observation, then again
    # before replay. Actual registered session/worktree and DB state survive.
    clock = original["deadline"] + 1 if budget == "time" else original["created_at"] + 1
    for _ in range(3):
        restarted = type(service)(db, runtime, clock=lambda: clock)
        result = await restarted.allocate(
            "real", target_ref=original["target_ref"], head_sha=heads[-1],
            authorize=AsyncMock(return_value=True),
        )
        assert result["outcome"] == "deferred"
        state = (await db.get_integration_batch("real"))["fixer_state"]
        assert state["session_id"] == row.id
        assert state["blocker"] == f"fixer {'time' if budget == 'time' else 'CI round'} budget exhausted"
        assert git(runtime.git.remote_path, "rev-parse", "main") != heads[-1]
    assert len(await notices(db, supervisor=True)) == 1
    # Exhaustion adds only the supervisor incident, with no fixer notification.
    assert len(await notices(db)) == len(before) + 1
    assert len(provider.starts) == 1
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(tasks)) == 0
    await restarted.settle_green("real", heads[-1])
    assert (await db.get_integration_batch("real"))["fixer_state"]["status"] == "green"
    for source_oid in heads:
        assert git(row.work_dir, "merge-base", "--is-ancestor", source_oid, heads[-1]) == ""
    record_property("manifest", json.dumps({
        "budget": budget, "batch_id": "real", "session_id": row.id,
        "sources": {f"round_{i}": head for i, head in enumerate(heads)},
        "candidate_oid": heads[-1], "supervisor_incidents": 1,
        "fixer_wakes_on_exhaustion": 0, "session_launches": 1,
        "task_refilings": 0, "process_stalls": [],
        "checks": "controlled red rounds / deadline, then exact green settlement; no dev publish",
    }))
