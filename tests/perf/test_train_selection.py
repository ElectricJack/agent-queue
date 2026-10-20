"""Production-shaped root selection over real Git and disposable PostgreSQL.

Run serially with AQ_PERF_STRICT=1 aq test -m perf -p no:xdist -s
tests/perf/test_train_selection.py. GitHub uses recorded-shape fixture responses
with 20 ms per request; no external service or operator database is accessed.
"""

import asyncio
import json
import time

import pytest
from sqlalchemy import event, func, insert, select

from src.database.tables import task_branch_origins, tasks
from src.integration.git_truth import GitTruth
from src.integration.selection_metrics import SelectionMetrics, selection_metrics_scope
from src.models import Task, TaskStatus
from tests.test_integration_train_sources import (
    MAIN,
    completed,
    git,
    hosted_train,
    world,  # noqa: F401 - imported pytest fixture
)

pytestmark = pytest.mark.perf


async def test_cold_root_selection_after_main_move(world, monkeypatch, perf_strict):  # noqa: F811
    db, origin = world.db, world.origin
    base = git(origin.clone, "rev-parse", "main")
    sources = {}
    for index in range(200):
        tid = f"root-{index:03}"
        sources[tid] = await completed(world, tid)
        # Distinct PR identities; the usual tiny-fixture helper hashes short names.
        await db.update_task(tid, pr_url=f"https://github.com/acme/widgets/pull/{index + 1}")
    for index in range(10):
        await db.create_task(Task(
            id=f"epic-{index}", project_id="p", repo_id="r", title="epic", description="",
            branch_name=f"aq/epic-{index}", status=TaskStatus.IN_PROGRESS,
        ))
    children = [{
        "id": f"child-{index:04}", "project_id": "p", "repo_id": "r",
        "title": "historical child", "description": "", "branch_name": f"aq/child-{index:04}",
        "status": "COMPLETED", "parent_task_id": f"epic-{index % 10}",
        "created_at": 1, "updated_at": 2,
    } for index in range(4790)]
    async with db.immediate() as conn:
        await conn.execute(insert(tasks), children)
        await conn.execute(insert(task_branch_origins), [{
            "id": child["id"] + "-origin", "task_id": child["id"], "repository_id": "r",
            "branch_name": child["branch_name"], "parent_task_id": child["parent_task_id"],
            "parent_repository_id": "r", "parent_ref": "aq/" + child["parent_task_id"],
            "base_sha": base, "creation_generation": 0, "reserved": True,
            "materialized": True, "created_at": 1,
        } for child in children])
    # Exactly 360 aq/* source refs (200 roots, 10 epics, 150 children), plus
    # retained provenance refs. Retired/missing historical refs must not add
    # per-task Git work at the unrelated root boundary.
    updates = [f"create refs/heads/aq/epic-{i} {base}" for i in range(10)]
    updates += [f"create refs/heads/aq/child-{i:04} {base}" for i in range(150)]
    updated = await world.truth.git.arun_git_result(
        ["update-ref", "--stdin"], cwd=origin.url, stdin="\n".join(updates) + "\n")
    assert updated.returncode == 0, updated.stderr
    git(origin.clone, "checkout", "main")
    (origin.clone / "main-moved.txt").write_text("new default head\n")
    git(origin.clone, "add", ".")
    git(origin.clone, "commit", "-qm", "move main before cold selection")
    git(origin.clone, "push", "origin", "main")
    train, github, _ = await hosted_train(world)
    service = (await train.lane_for(MAIN)).service
    transport = train.lane_for.git
    truth = GitTruth(transport)
    observed = await truth.snapshot(
        str(origin.clone), project_id="p", repository_id="r", repository_url=origin.url,
        target_ref=MAIN.target_ref,
    )
    assert observed.target_oid != base
    assert not any((truth._completions, truth._proofs, truth._objects, truth._cache))
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(tasks)) == 5000
    assert sum(ref.startswith("refs/remotes/origin/aq/")
               for ref in observed.observation.source_heads) == 360

    requests, git_commands, statements = 0, 0, 0
    for name in ("pull_request", "paged_items", "paged_list", "request_json"):
        original = getattr(github, name)

        def delayed(read):
            async def call(*args, **kwargs):
                nonlocal requests
                requests += 1
                await asyncio.sleep(0.02)
                return await read(*args, **kwargs)
            return call

        monkeypatch.setattr(github, name, delayed(original))
    original_git = transport.arun_git_result

    async def counted_git(*args, **kwargs):
        nonlocal git_commands
        git_commands += 1
        return await original_git(*args, **kwargs)

    monkeypatch.setattr(transport, "arun_git_result", counted_git)

    def counted_sql(*args):
        nonlocal statements
        statements += 1

    event.listen(db._engine.sync_engine, "before_cursor_execute", counted_sql)
    metrics = SelectionMetrics()
    started = time.monotonic()
    try:
        with selection_metrics_scope(metrics):
            selection = await train.batches.open_batch(MAIN, observed, service, seal_now=True)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", counted_sql)
    elapsed = time.monotonic() - started
    profile = metrics.as_dict()
    print(json.dumps({
        "tasks": 5000, "aq_refs": 360, "root_candidates": 200, "cache": "cold_after_main_move",
        "seconds": elapsed, "git_commands": git_commands, "sql_statements": statements,
        "github_requests": requests, "fixture_request_latency_seconds": 0.02,
        "selection": profile,
    }, sort_keys=True))
    assert selection.batch is not None, selection.blockers
    assert {member.task_id: member.source_sha for member in selection.members} == sources
    assert not selection.blockers
    assert profile["counts"]["candidate_scans"] == 1
    assert profile["counts"]["repository_ids"] == 4990
    assert profile["counts"]["routed_ids"] == 200
    # The incident visit spent 197 s here. Includes ordinary eligibility,
    # provenance rechecks and freeze.
    assert elapsed < 60
