"""Watchdog observations for hand landing, project pauses and supervisor withdrawal."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import escalations, integration_batches, projects, tasks, wait_records
from src.git.manager import GitError, GitManager
from src.integration import delivery_truth
from src.integration.delivery_truth import DeliveryRequest, DeliverySnapshot
from src.integration.delivery_watchdog import scan_stuck_deliveries
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus


@pytest.fixture
async def world(reuse_database, monkeypatch):
    db = await reuse_database("delivery-watchdog-regressions")
    await db.create_project(Project(id="p", name="Project"))
    await db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.LINK,
                                   url="unused", default_branch="main"))
    await db.update_project("p", hierarchical_integration_mode="train",
                            integration_repository_id="r")
    await db.create_task(Task(id="t", project_id="p", title="Work", description="",
                             status=TaskStatus.COMPLETED, branch_name="aq/t", repo_id="r"))
    state = SimpleNamespace(db=db, now=10000.0, generation="completion-1")

    async def requests(_db, task_ids, **kwargs):
        return {tid: DeliveryRequest(
            project_id="p", repository_id="r", target_ref="refs/heads/main", task_id=tid,
            task_version=1, completion_id=state.generation, legacy_generation="",
            completed_at=1, has_recorded_source=True,
        ) for tid in task_ids}

    entry = dict(task_id="t", project_id="p", archived=False, root=True,
                 branch="aq/t", head="a" * 40, wip=False, rule="stranded_root",
                 reason="no delivery owner")
    observe = AsyncMock(return_value={"entries": [entry], "unavailable": []})

    async def scan(**kwargs):
        state.now += 1800
        with monkeypatch.context() as patch:
            patch.setattr(delivery_truth, "load_delivery_requests", requests)
            return await scan_stuck_deliveries(db, observer=object(), observe=observe,
                                               now=state.now, **kwargs)

    async def rows(table):
        async with db._engine.connect() as conn:
            return list((await conn.execute(select(table))).mappings())

    state.scan, state.rows, state.observe = scan, rows, observe
    return state


@pytest.mark.parametrize("status", ["PAUSED", "ARCHIVED"])
async def test_inactive_project_does_not_observe_or_page_and_resume_restarts_streak(world, status):
    assert (await world.scan())["deliveries"][0]["stuck_scans"] == 1
    async with world.db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(status=status))
    world.observe.reset_mock()
    for _ in range(3):
        report = await world.scan()
        assert report["project_inactive"] == ["p"] and report["deliveries"] == []
    world.observe.assert_not_awaited()
    (projection,) = await world.rows(wait_records)
    assert projection["decision_inputs"]["stuck_scans"] == 0
    assert projection["decision_reason"] == "project_" + status.lower()
    assert await world.rows(escalations) == []
    assert len(await world.rows(tasks)) == 1
    async with world.db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(status="ACTIVE"))
    delivery = (await world.scan())["deliveries"][0]
    assert delivery["stuck_scans"] == 1 and delivery["redrive"] is None


async def test_supervisor_withdrawal_clears_only_bound_generation(world):
    for _ in range(3):
        await world.scan()
    (incident,) = await world.rows(escalations)
    task_count = len(await world.rows(tasks))
    await world.db.transition_escalation(
        incident["id"], expected_revision=incident["revision"], new_state="cancelled",
        terminal_outcome="False alarm: hand landed", now=world.now,
    )
    for _ in range(3):
        delivery = (await world.scan())["deliveries"][0]
        assert (delivery["state"], delivery["reason"]) == ("cleared", "supervisor_cancelled")
    (projection,) = await world.rows(wait_records)
    assert projection["outcome"] == "cancelled"
    assert projection["decision_inputs"]["stuck_scans"] == 0
    assert len(await world.rows(escalations)) == 1
    assert len(await world.rows(tasks)) == task_count
    # Fresh generation, same branch head: the old supervisor decision grants no waiver.
    world.generation = "completion-2"
    delivery = (await world.scan())["deliveries"][0]
    assert delivery["state"] == "stuck" and delivery["incident_id"] is None
    assert delivery["stuck_scans"] == 1
    assert len(await world.rows(wait_records)) == 2


@pytest.fixture
async def history(world, tmp_path):
    git = GitManager()
    path = tmp_path / "repo"
    await git._arun(["init", "-b", "main", str(path)], cwd=str(tmp_path))

    async def run(*args):
        return await git._arun(list(args), cwd=str(path))

    async def commit(filename, content, message):
        (path / filename).write_text(content)
        await run("add", ".")
        await run("commit", "-m", message)
        return await run("rev-parse", "HEAD")

    await run("config", "user.name", "Tester")
    await run("config", "user.email", "tester@example.test")
    await run("config", "commit.gpgsign", "false")
    base = await commit("base", "base\n", "base")
    await run("checkout", "-b", "aq/t")
    source = await commit("work", "work\n", "original work")
    await run("checkout", "main")
    target = await commit("work", "work\n", "hand landed")
    assert source != target

    async def scan():
        target = await run("rev-parse", "main")
        snapshot = DeliverySnapshot(
            git, str(path), "p", "r", "unused", "refs/heads/main", target,
            {"refs/remotes/origin/main": target, "refs/remotes/origin/aq/t": source},
        )
        observer = SimpleNamespace(git=git, READ_MAX_AGE=30,
                                   snapshot=AsyncMock(return_value=snapshot))
        return await scan_stuck_deliveries(world.db, observer=observer, now=world.now)

    return SimpleNamespace(run=run, commit=commit, scan=scan, base=base, source=source,
                           git=git)


@pytest.mark.parametrize("later_work", [False, True])
async def test_hand_landed_content_without_ancestry_never_redrives(world, history, later_work):
    if later_work:
        await history.commit("later", "later\n", "later work")
    assert await history.run("merge-base", "main", "aq/t") == history.base
    for _ in range(3):
        report = await history.scan()
        assert report["unavailable"] == [] and report["deliveries"] == []
    assert await world.rows(wait_records) == []
    assert await world.rows(escalations) == []
    assert len(await world.rows(tasks)) == 1


async def test_partial_hand_landing_still_owes_work(world, history):
    await history.run("checkout", "aq/t")
    history.source = await history.commit("missing", "missing\n", "unlanded work")
    await history.run("checkout", "main")
    # The inventory captures the new immutable source head on this visit.
    from src.integration.delivery_branches import observe_completed_branches

    target = await history.run("rev-parse", "main")
    path = await history.run("rev-parse", "--show-toplevel")
    snapshot = DeliverySnapshot(history.git, path, "p", "r", "unused", "refs/heads/main", target,
                                {"refs/remotes/origin/aq/t": history.source})
    observer = SimpleNamespace(git=history.git, READ_MAX_AGE=30,
                               snapshot=AsyncMock(return_value=snapshot))
    report = await observe_completed_branches(world.db, observer, now=world.now)
    assert report["unavailable"] == []
    assert report["entries"][0]["rule"] == "unreconciled"


@pytest.mark.parametrize("intent,lifecycle", [
    ("open", "testing"), ("open", "repairing"), ("open", "promoting"),
    ("open", "failed"), ("paused", "testing"),
])
async def test_incoming_batch_prevents_takeover_and_resets_streak(world, intent, lifecycle):
    assert (await world.scan())["deliveries"][0]["stuck_scans"] == 1
    # Inventory has already called this epic stranded. A child batch sealed
    # afterward still owns the target, without the epic being a batch member.
    async with world.db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(
            id="incoming", project_id="p", repository_id="r", target_ref="refs/heads/aq/t",
            intent=intent, lifecycle=lifecycle, request_id="incoming",
            source_manifest_digest="digest", base_sha="a" * 40,
            integration_branch="aq/batches/incoming", policy_snapshot={}, artifact_snapshot={},
            cleanup_state="pending", created_at=1, updated_at=1))
    for _ in range(3):
        delivery = (await world.scan())["deliveries"][0]
        assert (delivery["state"], delivery["reason"], delivery["stuck_scans"]) == (
            "progressing", "batch_queued", 0)
        assert delivery["redrive"] is None
    assert len(await world.rows(tasks)) == 1
    assert await world.rows(escalations) == []

    async with world.db.immediate() as conn:
        await conn.execute(update(integration_batches).values(lifecycle="promoted", intent="open"))
    world.observe.return_value["entries"][0]["head"] = "b" * 40
    assert (await world.scan())["deliveries"][0]["stuck_scans"] == 1
    delivery = (await world.scan())["deliveries"][0]
    assert delivery["redrive"], await world.rows(wait_records)
    landing = next(row for row in await world.rows(tasks) if row["id"] == delivery["redrive"]["task_id"])
    assert "b" * 40 in landing["description"]
    assert "a" * 40 not in landing["description"]
    (projection,) = await world.rows(wait_records)
    assert projection["evidence_ref"] == "b" * 40


async def test_recovery_fetches_current_epic_head_instead_of_cached_tip(world, history):
    await history.run("checkout", "aq/t")
    head = await history.commit("new-child", "new child\n", "collect child batch")
    await history.run("checkout", "main")
    target = await history.run("rev-parse", "main")
    path = await history.run("rev-parse", "--show-toplevel")
    stale = DeliverySnapshot(history.git, path, "p", "r", "unused", "refs/heads/main", target,
                             {"refs/remotes/origin/aq/t": history.source})
    current = DeliverySnapshot(history.git, path, "p", "r", "unused", "refs/heads/main", target,
                               {"refs/remotes/origin/aq/t": head})

    async def snapshot(_target, *, max_age, cached_only):
        return current if max_age == 0 else stale

    observer = SimpleNamespace(git=history.git, READ_MAX_AGE=30,
                               snapshot=AsyncMock(side_effect=snapshot))
    # The old tip's content is on main, but the just-collected child is not.
    for _ in range(3):
        report = await scan_stuck_deliveries(world.db, observer=observer, now=world.now)
        assert not report["errors"] and not report["unavailable"]
        world.now += 3600
    landing = [row for row in await world.rows(tasks) if row["created_by_id"] == "delivery-watchdog"]
    assert len(landing) == 1
    assert head in landing[0]["description"]
    assert history.source not in landing[0]["description"]
    assert all(call.kwargs["max_age"] == 0 for call in observer.snapshot.await_args_list)


async def test_content_present_settles_existing_watchdog_incident(world, history):
    for _ in range(3):
        await world.scan()
    assert (await world.rows(escalations))[0]["state"] == "needs_human"
    assert (await history.scan())["deliveries"] == []
    (projection,) = await world.rows(wait_records)
    assert projection["outcome"] == "satisfied"
    (incident,) = await world.rows(escalations)
    assert incident["state"] == "resolved"


async def test_unavailable_content_check_is_not_delivery(world, history, monkeypatch):
    monkeypatch.setattr(history.git, "atree_sha", AsyncMock(side_effect=GitError("unavailable")))
    report = await history.scan()
    assert report["unavailable"] == ["p"] and report["deliveries"] == []
    assert await world.rows(escalations) == []


async def test_conflicting_content_is_not_treated_as_hand_landed(world, history):
    await history.commit("work", "conflicting work\n", "different target content")
    report = await history.scan()
    assert report["unavailable"] == []
    assert report["deliveries"][0]["state"] == "progressing"
    assert (await world.rows(wait_records))[0]["outcome"] == "pending"
