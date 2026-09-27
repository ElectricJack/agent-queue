"""Real Git + private PostgreSQL coverage for the development delivery path."""

import itertools
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, insert, select, update

from src.database import Database
from src.git.manager import GitManager
from src.database.queries.blocked_state import _development_delivery_pending
from src.database.tables import development_deliveries, gates, messages, projects
from src.database.tables import task_dependencies, task_gates, tasks
from src.doctor.integration_checks import run_check as run_doctor_check
from src.doctor.models import Severity
from src.event_bus import EventBus
from src.integration.development import (
    EMPTY_SOURCE_KEY, PUBLISHER_SKIP_KEY, DevelopmentBusy, DevelopmentIntegration, DevelopmentPolicy,
)
from src.integration.development_validation import (
    DEFERRAL_KIND, FAILED, INFRA_ALERT_AFTER, INFRASTRUCTURE, PASSED,
)
from src.integration.development_stalls import (
    DEFAULT_STALL_AFTER, STALL_MESSAGE_KIND, PublisherStalls, SweepObservation,
)
from src.models import (
    Project, RepoConfig, RepoSourceType, Task, TaskCompletion, TaskStatus, TaskType, Workspace,
)
from tests.db_fixtures import lease_dsn


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    source = tmp_path / "source"
    git(tmp_path, "clone", str(remote), str(source))
    git(source, "config", "user.name", "Tester")
    git(source, "config", "user.email", "tester@example.test")
    (source / "base.txt").write_text("base\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    git(source, "push", "origin", "main")
    db = Database(lease_dsn("development.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Development"))
    repo = RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote))
    await db.create_repo(repo)
    async with db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(integration_repository_id="r")
        )
    # These publisher scenarios use tiny shell fixtures, executed exclusively
    # by the real detached job runner. Production translation remains preset-only.
    from types import SimpleNamespace
    from src.commands import CommandHandler
    from src.config import AppConfig
    from src.jobs.adapters import PublisherJobs
    from src.jobs.policy import Preset

    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    monkeypatch.setattr("src.integration.development_validation.finite_command",
                        lambda command: ("lint", [command]))
    monkeypatch.setattr("src.jobs.service.presets",
                        lambda root: {"lint": Preset("lint", ("/bin/bash", "-c"))})
    handler = CommandHandler(SimpleNamespace(db=db, config=config), config)
    service = DevelopmentIntegration(db, data_dir=tmp_path / "data", git=GitManager(),
                                     job_client=PublisherJobs(handler))
    service.validation_poll_seconds = 0.05
    await service.configure(
        "p",
        {"validation": "focused", "commands": ["test -f base.txt"]},
        reason="test development policy",
        operator_id="local",
    )
    yield db, service, source, remote, repo
    await db.close()


async def feature(setup, task_id, *, filename=None, content="new\n"):
    db, _service, source, _remote, _repo = setup
    git(source, "checkout", "-B", task_id, "main")
    (source / (filename or task_id + ".txt")).write_text(content)
    git(source, "add", ".")
    git(source, "commit", "-m", task_id)
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", task_id)
    await db.create_task(
        Task(
            id=task_id,
            project_id="p",
            repo_id="r",
            title=task_id,
            description="",
            branch_name=task_id,
            status=TaskStatus.COMPLETED,
        )
    )
    return head


async def repair_cycle(setup, *, source_contract):
    db, service, source, _remote, _repo = setup
    older = "development-repair-older"
    older_sha = await feature(setup, older, filename="older.txt")
    newer = service._repair_identity([{"task_id": older, "source_sha": older_sha}])
    newer_sha = await feature(setup, newer, filename="newer.txt")
    git(source, "checkout", newer)
    (source / "older.txt").write_text("new\n")
    git(source, "add", "older.txt")
    git(source, "commit", "-m", "carry older repair content")
    newer_sha = git(source, "rev-parse", "HEAD")
    for task_id in (older, newer):
        git(source, "push", "origin", f"{task_id}:aq/{task_id}")
    async with db.immediate() as conn:
        for task_id in (older, newer):
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
                branch_name=f"aq/{task_id}"
            ))
        await conn.execute(insert(task_dependencies).values(
            task_id=older, depends_on_task_id=newer, dep_type="blocks",
        ))
    await db.set_task_meta(newer, "development_repair_sources", [
        {"task_id": older, "source_sha": older_sha if source_contract else "invalid"}
    ])
    await db.save_task_completion(TaskCompletion(
        id="older-close", task_id=older, outcome="pass", commits=[older_sha],
        completed_at=time.time(),
    ))
    await db.save_task_completion(TaskCompletion(
        id="newer-close", task_id=newer, outcome="pass", commits=[newer_sha],
        completed_at=time.time(),
    ))
    return older, newer, older_sha, newer_sha


async def _live_parent_operation(db, parent_task_id, *, operation_id="live-parent-operation"):
    """Seed the minimal hierarchy operation that a development switch must drain."""
    from src.database.tables import integration_parent_episodes, integration_repair_operations

    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id=f"episode-{operation_id}",
                parent_task_id=parent_task_id,
                repository_id="r",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id=operation_id,
                target_kind="parent",
                parent_task_id=parent_task_id,
                episode_id=f"episode-{operation_id}",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=2.0,
            )
        )


async def test_batch_publishes_exact_validated_sha_and_replay_is_idle(setup):
    _db, service, _source, remote, _repo = setup
    head = await feature(setup, "one")
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert result["head_sha"] == head
    assert git(remote, "rev-parse", "main") == head
    rows = await service.rows("p")
    root = [r for r in rows if r["state"] == "delivered" and r["target_ref"] == "refs/heads/main"]
    assert root[0]["evidence"]["conclusion"] == "passed"
    assert root[0]["evidence"]["head_sha"] == head
    assert (await service.sweep("p"))["outcome"] == "idle"


async def test_idle_sweep_does_not_open_git_transport(setup):
    _db, service, _source, _remote, _repo = setup
    with patch.object(service, "store", new_callable=AsyncMock) as store:
        assert (await service.sweep("p"))["outcome"] == "idle"
    store.assert_not_awaited()


async def test_undelivered_completion_fetches_and_publishes(setup):
    _db, service, _source, remote, _repo = setup
    head = await feature(setup, "pending")
    with patch.object(service.git, "afetch_origin", wraps=service.git.afetch_origin) as fetch:
        assert (await service.sweep("p"))["outcome"] == "delivered"
    fetch.assert_awaited()
    assert git(remote, "rev-parse", "main") == head


async def test_parked_row_fetches_without_completed_candidate(setup):
    db, service, _source, _remote, _repo = setup
    head = await feature(setup, "parked-source")
    now = time.time()
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "parked-source").values(status="FAILED")
        )
        await conn.execute(insert(development_deliveries).values(
            id="parked-retry", project_id="p", repository_id="r",
            target_ref="refs/heads/main", expected_sha=None, prepared_sha=None,
            state="parked", manifest=[{"task_id": "parked-source", "source_sha": head}],
            evidence={}, reason="retry parked source", created_at=now, updated_at=now,
        ))
    with (
        patch.object(service.git, "afetch_origin", wraps=service.git.afetch_origin) as fetch,
        patch.object(service, "reconcile_parked", new_callable=AsyncMock),
    ):
        assert (await service.sweep("p"))["outcome"] == "idle"
    fetch.assert_awaited()


async def test_completion_wakes_delivery_before_periodic_deadline(setup):
    db, service, _source, remote, _repo = setup
    head = await feature(setup, "one")
    await db.create_task(Task(id="two", project_id="p", title="successor", description=""))
    await db.add_dependency("two", "one", "blocks")
    assert (await db.get_task("two")).is_blocked
    bus = EventBus()
    bus.subscribe("task.completed", service.on_task_completed)
    now = time.time()
    service.next_due.update(p=now + 300, other=now + 300)
    await service.tick(now)
    assert git(remote, "rev-parse", "main") != head

    # Duplicate events coalesce; unrelated projects keep their deadlines.
    await bus.emit("task.completed", {"project_id": "p", "task_id": "one"})
    await bus.emit("task.completed", {"project_id": "p", "task_id": "one"})
    assert service.next_due["other"] == now + 300
    await service.tick(now + 5)
    assert git(remote, "rev-parse", "main") == head
    assert not (await db.get_task("two")).is_blocked
    assert service.next_due["p"] == now + 305


async def test_completion_during_sweep_is_not_lost_and_timer_remains_backstop(setup):
    from unittest.mock import AsyncMock

    _db, service, _source, _remote, _repo = setup
    now = time.time()
    service.next_due.clear()
    service.preserve_stopped_owners = AsyncMock()

    async def completing_sweep(project_id):
        await service.on_task_completed({"project_id": project_id})

    service.sweep = AsyncMock(side_effect=completing_sweep)
    await service.tick(now)
    assert "p" not in service.next_due
    service.sweep.side_effect = None
    await service.tick(now + 5)
    assert service.sweep.await_count == 2
    await service.tick(now + 10)
    assert service.sweep.await_count == 2
    # An absent event still gets a periodic reconciliation.
    await service.tick(now + 305)
    assert service.sweep.await_count == 3


@pytest.mark.parametrize("short_sha", [False, True])
async def test_successor_waits_for_default_branch_delivery(setup, short_sha):
    db, service, _source, remote, _repo = setup
    head = await feature(setup, "prerequisite")
    if short_sha:
        await db.save_task_completion(TaskCompletion(
            id="abbreviated-close", task_id="prerequisite", outcome="pass",
            commits=[head[:9]], completed_at=time.time(),
        ))
    await db.create_task(Task(
        id="successor", project_id="p", title="successor", description="",
        status=TaskStatus.READY,
    ))
    await db.add_dependency("successor", "prerequisite")
    assert (await db.get_task("successor")).is_blocked
    notifications = []

    async def on_ready(entries):
        notifications.extend(entries)

    db.set_ready_listener(on_ready)

    await service.configure(
        "p", {"commands": ["exit 7"]}, reason="test candidate only", operator_id="local"
    )
    assert (await service.sweep("p"))["outcome"] == "parked"
    assert head in git(remote, "show-ref"), "candidate was preserved"
    assert (await db.get_task("successor")).is_blocked

    await service.configure(
        "p", {"commands": ["test -f prerequisite.txt"]},
        reason="publish prerequisite", operator_id="local",
    )
    assert (await service.sweep("p", retry=True))["outcome"] == "delivered"
    assert git(remote, "rev-parse", "main") == head
    assert not (await db.get_task("successor")).is_blocked
    assert notifications == [("successor", "unblocked")]

    await db.save_task_completion(TaskCompletion(
        id="same-revision", task_id="prerequisite", outcome="pass",
        commits=[head], completed_at=time.time(),
    ))
    assert not (await db.get_task("successor")).is_blocked
    await db.save_task_completion(TaskCompletion(
        id="new-revision", task_id="prerequisite", outcome="pass",
        commits=["a" * 40], completed_at=time.time(),
    ))
    assert (await db.get_task("successor")).is_blocked


async def test_historical_delivery_resolves_short_completion_without_mutating_close(setup):
    db, service, _source, _remote, _repo = setup
    head = await feature(setup, "prerequisite")
    await service.sweep("p")
    await db.create_task(Task(id="successor", project_id="p", title="successor", description=""))
    await db.add_dependency("successor", "prerequisite")
    await db.save_task_completion(TaskCompletion(
        id="short-close", task_id="prerequisite", outcome="pass",
        commits=[head[:9]], completed_at=time.time(),
    ))
    assert (await db.get_task("successor")).is_blocked
    await service.sweep("p")
    assert not (await db.get_task("successor")).is_blocked
    assert (await db.get_task_completion("prerequisite")).commits == [head[:9]]
    # A newer unresolved close cannot borrow the old completion's proof.
    await db.save_task_completion(TaskCompletion(
        id="unresolved-close", task_id="prerequisite", outcome="pass",
        commits=["abcdef123"], completed_at=time.time(),
    ))
    await service.sweep("p")
    assert (await db.get_task("successor")).is_blocked


@pytest.mark.parametrize("short_sha", [False, True])
async def test_deleted_delivered_branch_does_not_strand_later_batches(setup, short_sha):
    db, service, source, remote, _repo = setup
    previous = await feature(setup, "previous")
    await db.save_task_completion(TaskCompletion(
        id="previous-close", task_id="previous", outcome="pass",
        commits=[previous[:9] if short_sha else previous], completed_at=time.time(),
    ))
    # Simulate a historical merge and normal source-branch cleanup, before
    # this publisher had a delivery receipt for it.
    git(source, "push", "origin", f"{previous}:main")
    git(source, "push", "origin", "--delete", "previous")
    later = await feature(setup, "later")
    await db.add_dependency("later", "previous")
    assert (await db.get_task("later")).is_blocked
    # An already-completed chain is assembled in one dependency-ordered pass.
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert not (await db.get_task("later")).is_blocked
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert git(remote, "merge-base", "--is-ancestor", later, "main") == ""


async def test_deleted_delivered_branch_with_no_commits_uses_delivery_receipt(setup):
    """Branch cleanup cannot turn a commits-less delivery into an unavailable dependency."""
    db, service, source, remote, _repo = setup
    previous = await feature(setup, "previous")
    await db.save_task_completion(TaskCompletion(
        id="previous-close", task_id="previous", outcome="pass", commits=[], completed_at=time.time(),
    ))
    assert (await service.sweep("p"))["outcome"] == "delivered"
    # The publisher binds its delivered receipt to the empty close.
    delivery = next(
        row
        for row in await service.rows("p")
        if row["state"] == "delivered" and row["target_ref"] == "refs/heads/main"
    )
    assert delivery["evidence"]["completion_sources"] == [
        {"task_id": "previous", "completion_id": "previous-close", "reported_sha": None,
         "source_sha": previous}
    ]
    # Existing receipts predate the binding. The manifest fallback must still
    # rescue them after their source branch is cleaned up.
    await service.change(
        delivery["id"],
        evidence={key: value for key, value in delivery["evidence"].items()
                  if key != "completion_sources"},
    )

    git(source, "push", "origin", "--delete", "previous")
    later = await feature(setup, "later")
    await db.add_dependency("later", "previous")

    result = await service.sweep("p")

    assert result["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", later, "main") == ""


async def test_docs_only_plan_dependency_stays_satisfied_after_branch_cleanup(setup):
    """The nimble-bridge shape: a plan task writes its plan outside the repository,
    so its branch is its base and its close lists no commits. Once that is delivered,
    cleaning up the branch must neither re-block a waiting dependent nor strand one
    whose edge is written afterwards, at readiness or at publication."""
    db, service, source, remote, _repo = setup
    git(source, "push", "origin", "main:refs/heads/aq/plan")
    await db.create_task(Task(
        id="plan", project_id="p", repo_id="r", title="plan", description="",
        branch_name="aq/plan", status=TaskStatus.COMPLETED, task_type=TaskType.PLAN,
    ))
    await db.save_task_completion(TaskCompletion(
        id="plan-close", task_id="plan", outcome="pass", commits=[], completed_at=time.time(),
    ))
    await db.create_task(Task(id="waiting", project_id="p", title="waiting", description=""))
    await db.add_dependency("waiting", "plan")
    assert (await db.get_task("waiting")).is_blocked

    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert not (await db.get_task("waiting")).is_blocked

    await service.collect_delivered_branches("p")
    assert "aq/plan" not in remote_branches(remote)
    assert not await _delivery_pending(db, "plan")
    assert await db.get_blocking_dependencies("waiting") == []
    # An edge written after cleanup recomputes against the receipt, not the branch.
    await db.create_task(Task(id="filed-later", project_id="p", title="f", description=""))
    await db.add_dependency("filed-later", "plan")
    assert not (await db.get_task("filed-later")).is_blocked

    head = await aq_feature(setup, "implementation")
    await db.add_dependency("implementation", "plan")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", head, "main") == ""
    assert await db.get_task_meta("implementation", PUBLISHER_SKIP_KEY) is None


async def test_branchless_epic_dependencies_are_satisfied_without_manifest_members(setup):
    """The fair-bridge.1 shape publishes after three completed epics settle."""
    db, service, _source, remote, _repo = setup
    for epic_id in ("sharp-stone", "first-epic", "second-epic"):
        await db.create_task(Task(
            id=epic_id, project_id="p", title=epic_id, description="",
            status=TaskStatus.COMPLETED,
        ))
        assert not await _delivery_pending(db, epic_id)
    await db.create_task(Task(
        id="fair-bridge", project_id="p", title="fair-bridge", description="",
        status=TaskStatus.PAUSED,
    ))
    head = await feature(setup, "fair-bridge.1")
    async with db.immediate() as conn:
        await db.set_parent("fair-bridge.1", "fair-bridge", conn=conn)
    for epic_id in ("sharp-stone", "first-epic", "second-epic"):
        await db.add_dependency("fair-bridge.1", epic_id, "blocks")
    assert not (await db.get_task("fair-bridge.1")).is_blocked

    result = await service.sweep("p")

    assert result["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", head, "main") == ""
    assert await db.get_task_meta("fair-bridge.1", PUBLISHER_SKIP_KEY) is None
    delivered = [
        row for row in await service.rows("p")
        if row["state"] == "delivered" and row["target_ref"] == "refs/heads/main"
    ]
    assert [member["task_id"] for member in delivered[0]["manifest"]] == ["fair-bridge.1"]


async def test_recover_child_publishes_past_delivered_dependency_without_ref(setup):
    """An older parked attempt cannot override a later delivered receipt."""
    db, service, source, remote, _repo = setup
    previous = await feature(setup, "previous")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    await _park(service, "old-attempt", [{"task_id": "previous", "source_sha": previous}])
    git(source, "push", "origin", "--delete", "previous")
    # A completed dependency need not be a current publication candidate.
    # Its durable receipt, rather than a live task branch, proves delivery.
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "previous").values(branch_name=None))
    later = await feature(setup, "later")
    await db.add_dependency("later", "previous")

    result = await service.recover_child("p", "later")

    assert result["outcome"] == "delivered"
    assert result["recovered_task_id"] == "later"
    assert result["source_sha"] == later
    assert git(remote, "merge-base", "--is-ancestor", later, "main") == ""
    assert await db.get_task_meta("later", PUBLISHER_SKIP_KEY) is None


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("child_already_on_main", [False, True])
async def test_delivered_dependency_survives_later_conflict_and_adoption_rows(
    setup, recover, child_already_on_main,
):
    db, service, source, remote, _repo = setup
    previous = await feature(setup, "previous")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    await db.save_task_completion(TaskCompletion(
        id="previous-close", task_id="previous", outcome="pass",
        commits=[previous], completed_at=time.time(),
    ))
    # Recovery attempts may leave a newer, uncontained conflict revision and
    # an adopted conflict row. Neither revokes the original delivery on main.
    git(source, "checkout", "previous")
    (source / "conflict-attempt.txt").write_text("unpublished\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "conflict attempt")
    conflict = git(source, "rev-parse", "HEAD")
    await _park(service, "later-conflict", [
        {"task_id": "previous", "source_sha": conflict}
    ], reason="source conflict; independent work may continue")
    now = time.time()
    await service.save({
        "id": "later-adoption", "project_id": "p", "repository_id": "r",
        "target_ref": "refs/heads/aq/development/parent/old",
        "expected_sha": None, "prepared_sha": conflict,
        "state": "adopted", "manifest": [
            {"task_id": "previous", "source_sha": conflict}
        ], "evidence": {"kind": "source_conflict"},
        "reason": "source conflict; independent work may continue",
        "created_at": now, "updated_at": now,
    })
    git(source, "push", "origin", "--delete", "previous")
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "previous").values(branch_name=None))
    later = await feature(setup, "later")
    await db.add_dependency("later", "previous")
    if child_already_on_main:
        git(source, "fetch", "origin", "main")
        git(source, "merge", "--no-edit", "origin/main")
        later = git(source, "rev-parse", "HEAD")
        git(source, "push", "origin", "later")
        await db.save_task_completion(TaskCompletion(
            id="later-close", task_id="later", outcome="pass",
            commits=[later], completed_at=time.time(),
        ))
        git(source, "push", "origin", f"{later}:main")
        git(source, "push", "origin", "--delete", "later")

    result = await service.recover_child("p", "later") if recover else await service.sweep("p")

    assert result["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", later, "main") == ""
    assert await db.get_task_meta("later", PUBLISHER_SKIP_KEY) is None
    assert any(
        row["target_ref"] == "refs/heads/main"
        and row["state"] == "delivered"
        and any(member["task_id"] == "later" for member in row["manifest"])
        for row in await service.rows("p")
    )


@pytest.mark.parametrize("receipt_state", ["delivered", "adopted"])
@pytest.mark.parametrize("recover", [False, True])
async def test_non_default_receipt_releases_deleted_dependency_ref(setup, receipt_state, recover):
    """A manifest on a cleaned-up assembly or candidate ref proves target ancestry."""
    db, service, source, remote, _repo = setup
    previous = await feature(setup, "previous")
    git(source, "push", "origin", f"{previous}:main")
    now = time.time()
    await service.save({
        "id": "previous-receipt", "project_id": "p", "repository_id": "r",
        "target_ref": "refs/heads/aq/development/parent/old",
        "expected_sha": None, "prepared_sha": previous,
        "state": receipt_state,
        "manifest": [{"task_id": "previous", "source_sha": previous}],
        "evidence": {"kind": "assembly"}, "reason": "historical assembly",
        "created_at": now, "updated_at": now,
    })
    git(source, "push", "origin", "--delete", "previous")
    later = await feature(setup, "later")
    await db.add_dependency("later", "previous")

    result = (
        await service.recover_child("p", "later") if recover else await service.sweep("p")
    )

    assert result["outcome"] == "delivered"
    if recover:
        assert result["recovered_task_id"] == "later"
    assert git(remote, "merge-base", "--is-ancestor", later, "main") == ""
    assert await db.get_task_meta("later", PUBLISHER_SKIP_KEY) is None
    assert any(
        member["task_id"] == "previous"
        for row in await service.rows("p")
        if row["target_ref"] == "refs/heads/main" and row["state"] == "delivered"
        for member in row["manifest"]
    )


async def test_recover_child_isolated_from_conflicting_sibling(setup):
    db, service, _source, remote, _repo = setup
    await feature(setup, "main-change", filename="base.txt", content="on main\n")
    await service.sweep("p")
    await db.create_task(Task(
        id="epic", project_id="p", title="epic", description="", status=TaskStatus.PAUSED,
        branch_name="epic-source",
    ))
    await feature(setup, "conflict", filename="base.txt", content="conflicts\n")
    clean = await feature(setup, "clean")
    async with db.immediate() as conn:
        await db.set_parent("conflict", "epic", conn=conn)
        await db.set_parent("clean", "epic", conn=conn)

    assert (await service.sweep("p"))["outcome"] == "idle"
    assert (await db.get_task_meta("clean", PUBLISHER_SKIP_KEY))["reason"] == (
        "parent_unavailable"
    )

    result = await service.recover_child("p", "clean")

    assert result["outcome"] == "delivered"
    assert result["source_sha"] == clean
    assert result["sibling_conflicts"] == ["conflict"]
    assert git(remote, "merge-base", "--is-ancestor", clean, "main") == ""
    assert await db.get_task_meta("clean", PUBLISHER_SKIP_KEY) is None
    assert any(
        row["state"] == "parked" and any(
            member["task_id"] == "conflict" for member in row["manifest"]
        ) for row in await service.rows("p")
    )


async def test_branchless_parent_does_not_skip_clean_child_after_sibling_conflict(setup):
    db, service, _source, remote, _repo = setup
    await feature(setup, "main-change", filename="base.txt", content="on main\n")
    await service.sweep("p")
    await db.create_task(Task(
        id="epic", project_id="p", title="epic", description="", status=TaskStatus.PAUSED,
    ))
    await feature(setup, "conflict", filename="base.txt", content="conflicts\n")
    clean = await feature(setup, "clean")
    async with db.immediate() as conn:
        await db.set_parent("conflict", "epic", conn=conn)
        await db.set_parent("clean", "epic", conn=conn)

    result = await service.sweep("p")

    assert result["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", clean, "main") == ""
    assert await db.get_task_meta("clean", PUBLISHER_SKIP_KEY) is None
    assert any(
        row["state"] == "parked" and any(
            member["task_id"] == "conflict" for member in row["manifest"]
        ) for row in await service.rows("p")
    )


async def test_branchless_code_dependency_keeps_parked_newer_attempt(setup):
    db, service, source, _remote, _repo = setup
    await feature(setup, "previous")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    git(source, "checkout", "previous")
    (source / "new-revision.txt").write_text("new revision\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "new revision")
    newer = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "previous")
    await db.save_task_completion(TaskCompletion(
        id="new-revision-close", task_id="previous", outcome="pass",
        commits=[newer], completed_at=time.time(),
    ))
    await _park(service, "newer-attempt", [{"task_id": "previous", "source_sha": newer}])
    git(source, "push", "origin", "--delete", "previous")
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "previous").values(branch_name=None))
    await feature(setup, "later")
    await db.add_dependency("later", "previous")

    # SQL projection migrates separately; publication already obeys git truth.
    assert not await _delivery_pending(db, "previous")
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert (await db.get_task_meta("later", PUBLISHER_SKIP_KEY))["dependency_id"] == "previous"


async def test_branchful_undelivered_dependency_still_blocks_publication(setup, caplog):
    db, service, source, remote, _repo = setup
    head = await feature(setup, "unpublished")
    await db.save_task_completion(TaskCompletion(
        id="unpublished-close", task_id="unpublished", outcome="pass",
        commits=[head], completed_at=time.time(),
    ))
    git(source, "push", "origin", "--delete", "unpublished")
    await feature(setup, "dependent")
    await db.add_dependency("dependent", "unpublished")
    assert await _delivery_pending(db, "unpublished")
    assert (await db.get_task("dependent")).is_blocked

    assert (await service.sweep("p"))["outcome"] == "idle"
    skip = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
    assert skip["reason"] == "missing_ref"
    assert skip["dependency_id"] == "unpublished"
    assert any("dependent" in row.getMessage() and "unpublished" in row.getMessage()
               and "missing ref" in row.getMessage() for row in caplog.records)
    assert git(remote, "rev-parse", "main") != git(source, "rev-parse", "dependent")


@pytest.mark.parametrize("completion_record", [False, True])
@pytest.mark.parametrize("container", [False, True])
async def test_missing_source_with_empty_payload_never_releases_dependents(
    setup, completion_record, container,
):
    db, service, _source, remote, _repo = setup
    branch = "aq/epic/empty" if container else "refs/heads/aq/empty"
    await db.create_task(Task(
        id="empty", project_id="p", title="empty", description="",
        branch_name=branch, status=TaskStatus.COMPLETED,
    ))
    if container:
        await db.set_task_meta("empty", "container", True)
    if completion_record:
        await db.save_task_completion(TaskCompletion(
            id="empty-close", task_id="empty", outcome="pass", commits=[],
            completed_at=time.time(),
        ))
    await db.set_task_meta("empty", PUBLISHER_SKIP_KEY, {
        "reason": "missing_ref", "dependency_id": "empty", "consecutive_ticks": 479,
    })
    head = await feature(setup, "dependent")
    await db.add_dependency("dependent", "empty")
    await db.create_task(Task(
        id="ready-after-empty", project_id="p", title="ready after empty", description="",
        status=TaskStatus.READY,
    ))
    await db.add_dependency("ready-after-empty", "empty")
    assert (await db.get_task("ready-after-empty")).is_blocked
    assert await _delivery_pending(db, "empty")

    before = git(remote, "rev-parse", "main")
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert (await db.get_task("empty")).status == TaskStatus.COMPLETED
    assert (await db.get_task("empty")).branch_name == branch
    assert await _delivery_pending(db, "empty")
    assert (await db.get_task("ready-after-empty")).is_blocked
    assert (await db.get_task("dependent")).is_blocked
    assert (await db.get_task_meta("empty", PUBLISHER_SKIP_KEY))["reason"] == "missing_ref"
    assert await db.get_task_meta("empty", EMPTY_SOURCE_KEY) is None
    assert git(remote, "rev-parse", "main") == before != head
    assert all(member["task_id"] != "empty" for row in await service.rows("p")
               for member in row["manifest"])
    # A missing ref remains an unresolved artifact on subsequent fresh checks.
    with patch.object(service.git, "afetch_origin", wraps=service.git.afetch_origin) as fetch:
        assert (await service.sweep("p"))["outcome"] == "idle"
        assert (await service.sweep("p"))["outcome"] == "idle"
    assert fetch.await_count == 2


async def test_missing_source_keeps_commits_from_earlier_completions(setup):
    db, service, source, _remote, _repo = setup
    head = await feature(setup, "unpublished")
    await db.save_task_completion(TaskCompletion(
        id="earlier-close", task_id="unpublished", outcome="pass",
        commits=[head], completed_at=time.time() - 1,
    ))
    await db.save_task_completion(TaskCompletion(
        id="latest-close", task_id="unpublished", outcome="pass",
        commits=[], completed_at=time.time(),
    ))
    git(source, "push", "origin", "--delete", "unpublished")

    assert (await service.sweep("p"))["outcome"] == "idle"

    assert (await db.get_task("unpublished")).branch_name == "unpublished"
    assert await _delivery_pending(db, "unpublished")
    assert await db.get_task_meta("unpublished", EMPTY_SOURCE_KEY) is None
    assert (await db.get_task_meta("unpublished", PUBLISHER_SKIP_KEY))["reason"] == "missing_ref"


async def test_empty_missing_source_alone_becomes_idle_without_a_delivery(setup):
    db, service, _source, _remote, _repo = setup
    history = await service.rows("p")
    await db.create_task(Task(
        id="empty", project_id="p", title="empty", description="",
        branch_name="aq/epic/empty", status=TaskStatus.COMPLETED,
    ))

    assert (await service.sweep("p"))["outcome"] == "idle"

    assert (await db.get_task("empty")).branch_name == "aq/epic/empty"
    assert await service.rows("p") == history
    with patch.object(service.git, "afetch_origin", wraps=service.git.afetch_origin) as fetch:
        assert (await service.sweep("p"))["outcome"] == "idle"
    fetch.assert_awaited_once()


async def test_missing_empty_canonical_branch_retains_its_identity(setup):
    from src.database.tables import task_integration_checkpoints

    db, service, _source, _remote, _repo = setup
    await db.create_task(Task(
        id="canonical", project_id="p", title="canonical", description="",
        branch_name="aq/epic/canonical", status=TaskStatus.COMPLETED,
    ))
    async with db.immediate() as conn:
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="canonical", repository_id="r", branch="aq/epic/canonical",
            updated_at=time.time(),
        ))

    assert (await service.sweep("p"))["outcome"] == "idle"

    assert (await db.get_task("canonical")).branch_name == "aq/epic/canonical"
    assert await _delivery_pending(db, "canonical")
    assert await db.get_task_meta("canonical", EMPTY_SOURCE_KEY) is None
    with patch.object(service.git, "afetch_origin", wraps=service.git.afetch_origin) as fetch:
        assert (await service.sweep("p"))["outcome"] == "idle"
    fetch.assert_awaited_once()


@pytest.mark.parametrize("new_completion", [False, True])
async def test_empty_source_observation_does_not_hide_later_work(setup, new_completion):
    db, service, source, remote, _repo = setup
    await db.create_task(Task(
        id="empty", project_id="p", title="empty", description="",
        branch_name="aq/empty", status=TaskStatus.COMPLETED,
    ))
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert await _delivery_pending(db, "empty")
    git(source, "checkout", "-b", "aq/empty")
    (source / "later.txt").write_text("later work\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "later work")
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "aq/empty")
    if new_completion:
        await db.save_task_completion(TaskCompletion(
            id="later-close", task_id="empty", outcome="pass", commits=[head],
            completed_at=time.time(),
        ))
    else:
        await db.update_task("empty", status=TaskStatus.READY)
        await db.update_task("empty", status=TaskStatus.COMPLETED)

    assert await _delivery_pending(db, "empty")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert not await _delivery_pending(db, "empty")
    assert git(remote, "merge-base", "--is-ancestor", head, "main") == ""


async def test_origin_fetch_failure_does_not_retire_empty_source(setup):
    from src.git.manager import GitError

    db, service, _source, _remote, _repo = setup
    await db.create_task(Task(
        id="empty", project_id="p", title="empty", description="",
        branch_name="aq/empty", status=TaskStatus.COMPLETED,
    ))
    with patch.object(service.git, "afetch_origin", side_effect=GitError("fetch failed")):
        with pytest.raises(GitError, match="fetch failed"):
            await service.sweep("p")

    assert (await db.get_task("empty")).branch_name == "aq/empty"
    assert await _delivery_pending(db, "empty")
    assert await db.get_task_meta("empty", EMPTY_SOURCE_KEY) is None


async def test_empty_source_observation_is_bound_to_the_delivery_repository(setup):
    db, service, _source, remote, _repo = setup
    await db.create_task(Task(
        id="empty", project_id="p", title="empty", description="",
        branch_name="aq/empty", status=TaskStatus.COMPLETED,
    ))
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert await _delivery_pending(db, "empty")
    await db.create_repo(RepoConfig(
        id="other-delivery", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote),
    ))
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            integration_repository_id="other-delivery",
        ))

    assert await _delivery_pending(db, "empty")
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert await _delivery_pending(db, "empty")
    assert await db.get_task_meta("empty", EMPTY_SOURCE_KEY) is None


async def test_parked_blocker_holds_dependent_and_names_both_tasks_in_log(setup, caplog):
    """A parked source stays unavailable; only delivered receipts release a chain."""
    import logging

    db, service, source, _remote, _repo = setup
    blocked = await feature(setup, "blocked")
    await _park(service, "blocked-batch", [{"task_id": "blocked", "source_sha": blocked}])
    git(source, "push", "origin", "--delete", "blocked")
    await feature(setup, "dependent")
    await db.add_dependency("dependent", "blocked")

    with caplog.at_level(logging.WARNING, logger="src.integration.development"):
        assert (await service.sweep("p"))["outcome"] == "idle"

    assert any(
        "dependent" in record.getMessage() and "blocked" in record.getMessage()
        and "undelivered dependency" in record.getMessage()
        for record in caplog.records
    )
    assert all(member["task_id"] != "dependent" for row in await service.rows("p")
               for member in row["manifest"])

    await service.sweep("p")
    await service.sweep("p")
    skip = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
    assert skip["dependency_id"] == "blocked"
    assert skip["reason"] == "undelivered_dependency"
    # The parked blocker has a live repair: the dependent waits on it rather
    # than counting unsuccessful evaluations toward a stall.
    (repair,) = await _repairs(db)
    assert skip["state"] == "waiting"
    assert skip["waiting_on"] == repair.id
    assert skip["consecutive_ticks"] == 0


async def test_repair_cycle_publishes_newest_and_satisfies_older(setup):
    db, service, _source, remote, _repo = setup
    older, newer, older_sha, newer_sha = await repair_cycle(setup, source_contract=True)
    await _park(service, "older-failed-delivery", [
        {"task_id": older, "source_sha": older_sha}
    ])
    await db.create_task(Task(id="successor", project_id="p", title="successor", description=""))
    await db.add_dependency("successor", older, "blocks")

    result = await service.sweep("p")

    assert result["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", newer_sha, "main") == ""
    assert git(remote, "show", "main:older.txt") == "new"
    assert not (await db.get_task("successor")).is_blocked
    delivery = next(row for row in await service.rows("p") if row["state"] == "delivered"
                    and row["target_ref"] == "refs/heads/main")
    assert {member["task_id"] for member in delivery["manifest"]} == {older, newer}
    assert next(member for member in delivery["manifest"] if member["task_id"] == older) == {
        "task_id": older, "source_sha": older_sha, "superseded_by": newer,
    }
    parked = next(row for row in await service.rows("p") if row["id"] == "older-failed-delivery")
    assert parked["state"] == "adopted"
    assert parked["evidence"]["resolved_by_delivered_repair"]["task_id"] == newer
    assert (await service.sweep("p"))["outcome"] == "idle"


async def test_recover_older_repair_collects_cycle_peer(setup):
    db, service, _source, remote, _repo = setup
    older, newer, older_sha, _newer_sha = await repair_cycle(setup, source_contract=True)
    unrelated_sha = await feature(setup, "unrelated")

    result = await service.recover_child("p", older)

    assert result["outcome"] == "delivered"
    assert result["recovered_task_id"] == older
    assert result["source_sha"] == older_sha
    assert git(remote, "show", "main:older.txt") == "new"
    with pytest.raises(subprocess.CalledProcessError):
        git(remote, "merge-base", "--is-ancestor", unrelated_sha, "main")
    assert await db.get_task_meta(older, PUBLISHER_SKIP_KEY) is None
    assert await db.get_task_meta(newer, PUBLISHER_SKIP_KEY) is None


async def test_unproven_repair_cycle_reports_both_tasks(setup, caplog):
    import logging

    db, service, _source, _remote, _repo = setup
    older, newer, _older_sha, _newer_sha = await repair_cycle(setup, source_contract=False)

    with caplog.at_level(logging.WARNING, logger="src.integration.development"):
        assert (await service.sweep("p"))["outcome"] == "idle"

    assert any(older in row.getMessage() and newer in row.getMessage()
               and "cycle" in row.getMessage() for row in caplog.records)
    for task_id, dependency_id in ((older, newer), (newer, older)):
        skip = await db.get_task_meta(task_id, PUBLISHER_SKIP_KEY)
        assert skip["reason"] == "dependency_cycle"
        assert skip["dependency_id"] == dependency_id


async def test_adopted_repair_cycle_drops_out_of_candidate_evaluation(setup, caplog):
    """development-repair-528d…/-7c535…, 2026-09-26: after ``adopt --accept-equivalent``
    recorded both repairs as delivered, every later tick still logged "skipping …
    dependency cycle with …" for them.  Delivered and adopted completions leave
    candidate evaluation, cycle detection included, and their stale skip records
    go with them."""
    import logging

    db, service, source, remote, _repo = setup
    older, newer, older_sha, newer_sha = await repair_cycle(setup, source_contract=False)
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert (await db.get_task_meta(older, PUBLISHER_SKIP_KEY))["reason"] == "dependency_cycle"
    now = time.time()
    await service.save({
        "id": "accepted-equivalent", "project_id": "p", "repository_id": "r",
        "target_ref": "refs/heads/main", "expected_sha": None, "prepared_sha": None,
        "state": "adopted",
        "manifest": [
            {"task_id": older, "source_sha": older_sha},
            {"task_id": newer, "source_sha": newer_sha},
        ],
        "evidence": {"kind": "adopted"}, "reason": "accepted as equivalent",
        "created_at": now, "updated_at": now,
    })
    assert not await _delivery_pending(db, older) and not await _delivery_pending(db, newer)
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert (await db.get_task_meta(older, PUBLISHER_SKIP_KEY))["reason"] == "dependency_cycle"
    # Receipt state is insufficient. An external ancestry-preserving merge
    # supplies the exact proof, even with the invalid repair contract/cycle.
    git(source, "checkout", newer)
    git(source, "merge", "--no-edit", older)
    git(source, "push", "origin", "HEAD:main")
    unrelated = await feature(setup, "unrelated")
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="src.integration.development"):
        result = await service.sweep("p")

    assert result["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", unrelated, "main") == ""
    assert not any("cycle" in record.getMessage() for record in caplog.records)
    assert await db.get_task_meta(older, PUBLISHER_SKIP_KEY) is None
    assert await db.get_task_meta(newer, PUBLISHER_SKIP_KEY) is None


async def test_idle_sweep_clears_skip_records_nothing_evaluates(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "done-elsewhere")
    await db.set_task_meta("done-elsewhere", PUBLISHER_SKIP_KEY, {"reason": "missing_ref"})
    await db.update_task("done-elsewhere", branch_name=None)  # nothing left to publish

    assert (await service.sweep("p"))["outcome"] == "idle"
    assert await db.get_task_meta("done-elsewhere", PUBLISHER_SKIP_KEY) is None


# --------------------------------------------------------------------------
# Bounded skips: a repeated identical skip ends as a stall, once.
# --------------------------------------------------------------------------


async def _lost_source_pair(setup):
    """'unpublished' lost its ref before delivery and 'dependent' waits on it.

    Neither can be published and nothing else will change that: both are
    skipped identically on every evaluation.
    """
    db, _service, source, _remote, _repo = setup
    head = await feature(setup, "unpublished")
    await db.save_task_completion(TaskCompletion(
        id="unpublished-close", task_id="unpublished", outcome="pass",
        commits=[head], completed_at=time.time(),
    ))
    git(source, "push", "origin", "--delete", "unpublished")
    await feature(setup, "dependent")
    await db.add_dependency("dependent", "unpublished")
    return head


async def _stall_messages(db):
    async with db._engine.connect() as conn:
        return (await conn.execute(
            select(messages)
            .where(messages.c.to_id == "supervisor-p",
                   messages.c.body_kind == STALL_MESSAGE_KIND)
            .order_by(messages.c.created_at)
        )).mappings().all()


async def _stall_to_threshold(service, db):
    for _tick in range(DEFAULT_STALL_AFTER):
        assert (await service.sweep("p"))["outcome"] == "idle"
    for task_id in ("unpublished", "dependent"):
        assert (await db.get_task_meta(task_id, PUBLISHER_SKIP_KEY))["state"] == "stalled"


async def test_fifth_identical_skip_stalls_once_at_error_with_one_supervisor_message(
    setup, caplog,
):
    import logging

    db, service, _source, _remote, _repo = setup
    await _lost_source_pair(setup)

    with caplog.at_level(logging.INFO, logger="src.integration.development"):
        for tick in range(1, DEFAULT_STALL_AFTER):
            assert (await service.sweep("p"))["outcome"] == "idle"
            skip = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
            assert skip["state"] == "observing"
            assert skip["consecutive_ticks"] == tick
            doctor = await run_doctor_check(db, "integration.development_publisher_stalled")
            # Progress observations: quiet, then a warning, never an error.
            assert doctor.severity is (Severity.OK if tick < 3 else Severity.WARN)
            assert await _stall_messages(db) == []

        assert (await service.sweep("p"))["outcome"] == "idle"

    dependent = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
    unpublished = await db.get_task_meta("unpublished", PUBLISHER_SKIP_KEY)
    for skip in (dependent, unpublished):
        assert skip["state"] == "stalled"
        assert skip["consecutive_ticks"] == DEFAULT_STALL_AFTER
    assert dependent["reason"] == "missing_ref"
    assert dependent["dependency_id"] == "unpublished"
    assert dependent["evidence"]["completion_id"] is None
    assert unpublished["evidence"]["completion_id"] == "unpublished-close"
    assert unpublished["evidence"]["source_sha"] is None  # the ref is gone
    doctor = await run_doctor_check(db, "integration.development_publisher_stalled")
    assert doctor.severity is Severity.ERROR
    assert {s["cause"] for s in doctor.data["stalls"]} == {"candidate_stalled"}
    assert "--recover-child" in doctor.detail
    (message,) = await _stall_messages(db)
    assert dependent["notified_message_id"] == unpublished["notified_message_id"] == message["id"]
    body = message["body"]
    for evidence in ("- dependent: missing_ref (unpublished)", "- unpublished: missing_ref",
                     "Repository r", "refs/heads/main", "unpublished-close",
                     "aq integration sweep p --recover-child dependent"):
        assert evidence in body
    stalled_logs = [r for r in caplog.records if "stalled after" in r.getMessage()]
    assert len(stalled_logs) == 2 and all(r.levelno == logging.ERROR for r in stalled_logs)

    # Further unchanged ticks, and a restarted daemon, change nothing.
    caplog.clear()
    restarted = DevelopmentIntegration(
        db, data_dir=service.data_dir.parent, git=GitManager(), job_client=service.job_client,
    )
    with caplog.at_level(logging.INFO, logger="src.integration.development"):
        for publisher in (service, service, restarted, restarted):
            assert (await publisher.sweep("p"))["outcome"] == "idle"
    assert len(await _stall_messages(db)) == 1
    assert not [r for r in caplog.records if "stalled after" in r.getMessage()]
    again = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
    assert again["state"] == "stalled"
    assert again["attempt_id"] == dependent["attempt_id"]
    assert again["consecutive_ticks"] == DEFAULT_STALL_AFTER  # bounded: not counted on
    assert again["last_skipped_at"] > dependent["last_skipped_at"]  # but still checked
    doctor = await run_doctor_check(db, "integration.development_publisher_stalled")
    assert doctor.severity is Severity.ERROR


async def test_a_new_completion_generation_starts_a_fresh_attempt(setup):
    db, service, _source, _remote, _repo = setup
    head = await _lost_source_pair(setup)
    await _stall_to_threshold(service, db)
    stalled = await db.get_task_meta("unpublished", PUBLISHER_SKIP_KEY)

    await db.save_task_completion(TaskCompletion(
        id="unpublished-reclose", task_id="unpublished", outcome="pass",
        commits=[head], completed_at=time.time() + 1,
    ))
    assert (await service.sweep("p"))["outcome"] == "idle"

    fresh = await db.get_task_meta("unpublished", PUBLISHER_SKIP_KEY)
    assert fresh["state"] == "observing" and fresh["consecutive_ticks"] == 1
    assert fresh["attempt_id"] != stalled["attempt_id"]
    assert fresh["evidence"]["completion_id"] == "unpublished-reclose"
    # The dependent's own evidence did not change: its attempt stays over.
    assert (await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY))["state"] == "stalled"

    for _tick in range(DEFAULT_STALL_AFTER - 1):
        assert (await service.sweep("p"))["outcome"] == "idle"
    first, second = await _stall_messages(db)
    assert first["id"] != second["id"]
    assert "- unpublished: missing_ref" in second["body"]
    assert "- dependent:" not in second["body"]


async def test_an_explicit_retry_starts_a_fresh_attempt(setup):
    db, service, _source, _remote, _repo = setup
    await _lost_source_pair(setup)
    await _stall_to_threshold(service, db)
    stalled = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)

    assert (await service.sweep("p", retry=True))["outcome"] == "idle"

    retried = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
    assert retried["state"] == "observing" and retried["consecutive_ticks"] == 1
    assert retried["attempt_id"] != stalled["attempt_id"]
    assert len(await _stall_messages(db)) == 1
    for _tick in range(DEFAULT_STALL_AFTER - 1):
        assert (await service.sweep("p"))["outcome"] == "idle"
    assert len(await _stall_messages(db)) == 2


async def test_a_moved_target_is_not_new_evidence_until_it_contains_the_work(setup):
    """Unrelated delivery must not reset a stall; delivery of the work clears it."""
    db, service, source, remote, _repo = setup
    head = await _lost_source_pair(setup)
    await _stall_to_threshold(service, db)
    stalled = await db.get_task_meta("unpublished", PUBLISHER_SKIP_KEY)

    git(source, "checkout", "-B", "main", "origin/main")
    (source / "unrelated.txt").write_text("unrelated\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "unrelated delivery")
    git(source, "push", "origin", "main")
    assert (await service.sweep("p"))["outcome"] == "idle"

    moved = await db.get_task_meta("unpublished", PUBLISHER_SKIP_KEY)
    assert moved["state"] == "stalled" and moved["attempt_id"] == stalled["attempt_id"]
    assert moved["evidence"]["target_sha"] == git(remote, "rev-parse", "main")
    assert moved["evidence"]["target_sha"] != stalled["evidence"]["target_sha"]
    assert len(await _stall_messages(db)) == 1

    # Git now proves the lost source delivered: both candidates publish and
    # their stale diagnostics clear, with no further message.
    git(source, "merge", "--no-edit", head)
    git(source, "push", "origin", "main")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert await db.get_task_meta("unpublished", PUBLISHER_SKIP_KEY) is None
    assert await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY) is None
    doctor = await run_doctor_check(db, "integration.development_publisher_stalled")
    assert doctor.severity is Severity.OK
    assert len(await _stall_messages(db)) == 1


async def test_a_new_configured_target_starts_a_fresh_attempt(setup):
    db, service, source, _remote, _repo = setup
    await _lost_source_pair(setup)
    await _stall_to_threshold(service, db)
    stalled = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
    git(source, "push", "origin", "origin/main:refs/heads/trunk")
    from src.database.tables import repos

    async with db._engine.begin() as conn:
        await conn.execute(update(repos).where(repos.c.id == "r").values(default_branch="trunk"))

    assert (await service.sweep("p"))["outcome"] == "idle"

    fresh = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
    assert fresh["state"] == "observing" and fresh["consecutive_ticks"] == 1
    assert fresh["evidence"]["target_ref"] == "refs/heads/trunk"
    assert fresh["attempt_id"] != stalled["attempt_id"]


async def test_waiting_on_a_live_repair_does_not_stall_until_the_repair_fails(setup):
    db, service, source, _remote, _repo = setup
    blocked = await feature(setup, "blocked")
    await _park(service, "blocked-batch", [{"task_id": "blocked", "source_sha": blocked}])
    git(source, "push", "origin", "--delete", "blocked")
    await feature(setup, "dependent")
    await db.add_dependency("dependent", "blocked")

    for _tick in range(DEFAULT_STALL_AFTER + 2):
        assert (await service.sweep("p"))["outcome"] == "idle"

    (repair,) = await _repairs(db)
    for task_id in ("blocked", "dependent"):
        skip = await db.get_task_meta(task_id, PUBLISHER_SKIP_KEY)
        assert skip["state"] == "waiting" and skip["waiting_on"] == repair.id
        assert skip["consecutive_ticks"] == 0
    doctor = await run_doctor_check(db, "integration.development_publisher_stalled")
    assert doctor.severity is Severity.OK
    assert await _stall_messages(db) == []

    # A failed repair ends the wait; from then on the skip counts.
    await db.update_task(repair.id, status=TaskStatus.FAILED)
    for tick in range(1, DEFAULT_STALL_AFTER + 1):
        assert (await service.sweep("p"))["outcome"] == "idle"
        skip = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
        assert "waiting_on" not in skip and skip["consecutive_ticks"] == tick
    assert skip["state"] == "stalled"
    (message,) = await _stall_messages(db)
    assert "- dependent: undelivered_dependency (blocked)" in message["body"]
    doctor = await run_doctor_check(db, "integration.development_publisher_stalled")
    assert doctor.severity is Severity.ERROR


async def test_a_skip_counted_before_bounded_stalls_stalls_on_the_next_evaluation(setup):
    """An upgrade keeps an indefinite skip's count and reports it at once."""
    db, service, _source, _remote, _repo = setup
    await _lost_source_pair(setup)
    await db.set_task_meta("dependent", PUBLISHER_SKIP_KEY, {
        "reason": "missing_ref", "dependency_id": "unpublished", "consecutive_ticks": 35,
        "first_skipped_at": time.time() - 3 * 3600, "last_skipped_at": time.time() - 300,
    })

    assert (await service.sweep("p"))["outcome"] == "idle"

    skip = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
    assert skip["state"] == "stalled" and skip["consecutive_ticks"] == 36
    (message,) = await _stall_messages(db)
    assert "- dependent: missing_ref (unpublished)" in message["body"]
    assert "- unpublished:" not in message["body"]  # its first skip


async def test_the_stall_threshold_is_configurable_and_positive(setup, tmp_path):
    from src.config import IntegrationConfig, load_config

    assert IntegrationConfig().publisher_stall_after == DEFAULT_STALL_AFTER == 5
    for bad in (0, -1, True, "5", None):
        errors = IntegrationConfig(publisher_stall_after=bad).validate()
        assert [e.field for e in errors] == ["publisher_stall_after"], bad
    assert IntegrationConfig(publisher_stall_after=1).validate() == []
    path = tmp_path / "config.yaml"
    path.write_text(
        "discord:\n  bot_token: t\n  guild_id: '1'\n"
        "database:\n  url: postgresql+asyncpg://test:test@localhost/test\n"
        "integration:\n  publisher_stall_after: 2\n"
    )
    assert load_config(str(path)).integration.publisher_stall_after == 2
    with pytest.raises(ValueError, match="positive"):
        PublisherStalls(None, stall_after=0, repair_identity=DevelopmentIntegration._repair_identity)

    db, service, _source, _remote, _repo = setup
    await _lost_source_pair(setup)
    eager = DevelopmentIntegration(
        db, data_dir=service.data_dir.parent, git=GitManager(),
        job_client=service.job_client, stall_after=2,
    )
    assert (await eager.sweep("p"))["outcome"] == "idle"
    assert (await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY))["state"] == "observing"
    assert (await eager.sweep("p"))["outcome"] == "idle"
    skip = await db.get_task_meta("dependent", PUBLISHER_SKIP_KEY)
    assert skip["state"] == "stalled" and skip["stall_after"] == 2
    assert len(await _stall_messages(db)) == 1


def test_only_merge_outcomes_treat_a_moved_target_as_new_evidence():
    stalls = PublisherStalls(None, repair_identity=DevelopmentIntegration._repair_identity)

    def observe(target_sha="1" * 40, source_sha="a" * 40, target_ref="refs/heads/main"):
        return SweepObservation(
            project_id="p", repository_id="r", target_ref=target_ref, target_sha=target_sha,
            branches={"t": "aq/t"}, source_heads={"refs/remotes/origin/aq/t": source_sha},
        )

    def step(old, kind, observation, *, completion_id="c1", fresh=False):
        return stalls.advance("t", old, kind, None, observation, completion_id=completion_id,
                              waiting_on=None, fresh=fresh, now=time.time())

    first = step(None, "missing_ref", observe())
    moved_target = step(first, "missing_ref", observe(target_sha="2" * 40))
    assert moved_target["attempt_id"] == first["attempt_id"]
    assert moved_target["consecutive_ticks"] == 2
    conflict = step(None, "merge_conflict", observe())
    moved_conflict = step(conflict, "merge_conflict", observe(target_sha="2" * 40))
    assert moved_conflict["attempt_id"] != conflict["attempt_id"]
    assert moved_conflict["consecutive_ticks"] == 1
    for changed in (
        step(moved_target, "missing_ref", observe(source_sha="b" * 40)),
        step(moved_target, "missing_ref", observe(target_ref="refs/heads/trunk")),
        step(moved_target, "missing_ref", observe(), completion_id="c2"),
        step(moved_target, "dependency_cycle", observe()),
        step(moved_target, "missing_ref", observe(), fresh=True),
    ):
        assert changed["attempt_id"] != first["attempt_id"]
        assert changed["consecutive_ticks"] == 1


async def test_recover_child_command_reports_empty_exception_type():
    from src.commands.integration_commands import IntegrationCommandsMixin

    handler = IntegrationCommandsMixin()
    handler.db = object()
    service = AsyncMock()
    service.recover_child.side_effect = RuntimeError()
    handler._development_integration = lambda: service
    with patch("src.commands.integration_commands.integration_operator", return_value=("user", None)):
        result = await handler._cmd_integration_development_sweep({
            "project_id": "p", "recover_child": "child",
        })
    assert result["success"] is False
    assert result["error"] == "RuntimeError()"


@pytest.mark.parametrize("blocker", ["human_gate", "unfinished_dependency"])
async def test_batch_keeps_non_delivery_blockers(setup, blocker):
    db, service, _source, remote, _repo = setup
    base = git(remote, "rev-parse", "main")
    await feature(setup, "held")
    if blocker == "human_gate":
        async with db._engine.begin() as conn:
            await conn.execute(insert(gates).values(
                id="review", project_id="p", gate_type="human", title="Review",
                question="Approve?", status="open", created_at=time.time(),
            ))
            await conn.execute(insert(task_gates).values(task_id="held", gate_id="review"))
            await db.recompute_blocked({"held"}, conn=conn)
    else:
        await db.create_task(Task(
            id="unfinished", project_id="p", title="unfinished", description="",
            status=TaskStatus.READY,
        ))
        await db.add_dependency("held", "unfinished")
    assert (await db.get_task("held")).is_blocked
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert git(remote, "rev-parse", "main") == base


async def test_failed_validation_parks_and_preserves_candidate(setup):
    _db, service, _source, remote, _repo = setup
    before = git(remote, "rev-parse", "main")
    head = await feature(setup, "one")
    await service.configure(
        "p", {"commands": ["exit 7"]}, reason="require failing test", operator_id="local"
    )
    result = await service.sweep("p")
    assert result["outcome"] == "parked"
    assert result["evidence"]["checks"][0]["exit_code"] == 7
    assert git(remote, "rev-parse", "main") == before
    assert head in git(remote, "show-ref")
    assert (await service.sweep("p"))["outcome"] == "idle"
    await service.configure("p", {"commands": ["true"]}, reason="fixed test", operator_id="local")
    assert (await service.sweep("p", retry=True))["outcome"] == "delivered"


async def test_conflicting_member_does_not_block_independent_work(setup):
    _db, service, _source, remote, _repo = setup
    await feature(setup, "one", filename="base.txt", content="one\n")
    await feature(setup, "two", filename="base.txt", content="two\n")
    await feature(setup, "three")
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert {m["task_id"] for m in result["manifest"]} == {"one", "three"}
    assert git(remote, "show", "main:three.txt") == "new"
    assert any(
        r["state"] == "parked" and r["manifest"][0]["task_id"] == "two"
        for r in await service.rows("p")
    )


async def test_adopt_requires_ancestry_or_explicit_operator_equivalence(setup):
    db, service, _source, remote, _repo = setup
    await feature(setup, "one")
    main = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="not an ancestor"):
        await service.adopt(
            project_id="p",
            task_ids=["one"],
            target_ref="refs/heads/main",
            head_sha=main,
            reason="manual delivery",
            operator_id="local",
        )
    await db.transition_task("one", TaskStatus.READY, context="test")
    result = await service.adopt(
        project_id="p",
        task_ids=["one"],
        target_ref="refs/heads/main",
        head_sha=main,
        reason="report delivered by operator edit",
        operator_id="local",
        accept_equivalent=True,
    )
    assert result["outcome"] == "adopted"
    assert (await db.get_task("one")).status == TaskStatus.COMPLETED
    assert (await service.rows("p"))[-1]["evidence"]["conclusion"] == "not_ci_attested"


async def test_adopt_rejects_stale_target_and_open_children(setup):
    db, service, _source, remote, _repo = setup
    await feature(setup, "parent")
    await db.create_task(
        Task(
            id="child",
            project_id="p",
            title="child",
            description="",
            parent_task_id="parent",
            status=TaskStatus.READY,
        )
    )
    main = git(remote, "rev-parse", "main")
    with pytest.raises(ValueError, match="no longer"):
        await service.adopt(
            project_id="p",
            task_ids=["parent"],
            target_ref="refs/heads/main",
            head_sha="a" * 40,
            reason="test",
            operator_id="local",
            accept_equivalent=True,
        )
    with pytest.raises(ValueError, match="still open"):
        await service.adopt(
            project_id="p",
            task_ids=["parent"],
            target_ref="refs/heads/main",
            head_sha=main,
            reason="test",
            operator_id="local",
            accept_equivalent=True,
        )


async def _delivery_pending(db, task_id):
    async with db._engine.connect() as conn:
        return await conn.scalar(
            select(_development_delivery_pending(tasks)).where(tasks.c.id == task_id)
        )


@pytest.mark.parametrize("journaled_before_proof", [False, True])
async def test_adopting_an_open_task_delivers_the_completion_it_records(
    setup, journaled_before_proof
):
    """adopt() closes an open task with a completion reporting the adopted head,
    while its manifest names the branch head. That completion is delivered, so a
    'blocks' dependent is released; a row journaled before adopt() bound the two
    is backfilled by the next sweep."""
    db, service, source, remote, _repo = setup
    branch_head = await feature(setup, "adopted")
    await db.transition_task("adopted", TaskStatus.READY, context="test")
    git(source, "checkout", "main")
    git(source, "merge", "--no-ff", "--no-edit", "adopted")
    git(source, "push", "origin", "main")
    main = git(remote, "rev-parse", "main")
    assert main != branch_head, "the operator's merge moved main past the branch"
    await db.create_task(Task(
        id="successor", project_id="p", title="successor", description="",
        status=TaskStatus.READY,
    ))
    await db.add_dependency("successor", "adopted")
    assert (await db.get_task("successor")).is_blocked

    result = await service.adopt(
        project_id="p", task_ids=["adopted"], target_ref="refs/heads/main",
        head_sha=main, reason="merged by hand", operator_id="local",
    )
    completion = await db.get_task_completion("adopted")
    assert completion.commits == [main]
    proof = {"task_id": "adopted", "completion_id": completion.id,
             "reported_sha": main, "source_sha": branch_head}
    assert result["manifest"] == [
        {"task_id": "adopted", "source_sha": branch_head, "acceptance": "ancestry"}
    ]
    if journaled_before_proof:
        row = next(r for r in await service.rows("p") if r["id"] == result["id"])
        assert row["evidence"]["completion_sources"] == [proof]
        legacy = {k: v for k, v in row["evidence"].items() if k != "completion_sources"}
        await service.change(result["id"], evidence=legacy)
        assert await _delivery_pending(db, "adopted")
        assert (await db.get_task("successor")).is_blocked
    else:
        assert not await _delivery_pending(db, "adopted")
        assert not (await db.get_task("successor")).is_blocked

    # The sweep agrees the adopted task needs no publication, and backfills.
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert not await _delivery_pending(db, "adopted")
    assert not (await db.get_task("successor")).is_blocked
    row = next(r for r in await service.rows("p") if r["id"] == result["id"])
    assert row["evidence"]["completion_sources"] == [proof]

    # The adoption delivered that close, not whatever the task reports next.
    await db.save_task_completion(TaskCompletion(
        id="later-close", task_id="adopted", outcome="pass",
        commits=["a" * 40], completed_at=time.time(),
    ))
    await service.sweep("p")
    assert await _delivery_pending(db, "adopted")
    assert (await db.get_task("successor")).is_blocked


async def test_repository_exclusion_prevents_second_publisher(setup):
    db, service, _source, _remote, _repo = setup
    async with service.exclusion("r"):
        other = DevelopmentIntegration(db, data_dir=service.data_dir, git=service.git)
        with pytest.raises(DevelopmentBusy):
            async with other.exclusion("r"):
                pytest.fail("second publisher acquired exclusion")


async def test_crash_after_push_reconciles_without_republishing(setup):
    _db, service, _source, _remote, _repo = setup
    await feature(setup, "one")
    result = await service.sweep("p")
    await service.change(result["id"], state="publishing")
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert (
        next(r for r in await service.rows("p") if r["id"] == result["id"])["state"] == "delivered"
    )


def test_focused_policy_requires_commands():
    with pytest.raises(ValueError, match="at least one"):
        DevelopmentPolicy().checked()


async def test_parent_assembly_does_not_require_parent_verifier(setup):
    db, service, _source, remote, _repo = setup
    await db.create_task(
        Task(id="epic", project_id="p", title="epic", description="", status=TaskStatus.PAUSED)
    )
    await feature(setup, "child")
    async with db.immediate() as conn:
        await db.set_parent("child", "epic", conn=conn)
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert "refs/heads/aq/development/parent/" in git(remote, "show-ref")
    assert result["manifest"][0]["parent_task_id"] == "epic"


async def test_development_delivery_is_a_receipt_observe_readiness_accepts(setup):
    """A child the development publisher delivered never blocks a train cutover.

    Its finished parent has no train collection and never will, so the
    delivery row itself is the receipt integration status accepts.
    """
    from src.integration.status import IntegrationStatusService

    db, service, _source, _remote, _repo = setup
    await db.create_task(
        Task(id="epic", project_id="p", title="epic", description="", status=TaskStatus.PAUSED)
    )
    await feature(setup, "child")
    async with db.immediate() as conn:
        await db.set_parent("child", "epic", conn=conn)
    assert (await service.sweep("p"))["outcome"] == "delivered"
    async with db.immediate() as conn:
        # The parent finishes before the cutover (it is manually paused here
        # only so the publisher leaves it alone).
        await conn.execute(update(tasks).where(tasks.c.id == "epic").values(status="COMPLETED"))
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                hierarchical_integration_mode="observe",
                hierarchical_integration_desired_mode="observe",
            )
        )

    projection = await IntegrationStatusService(db).task_blockers("epic")

    assert projection["integration_active"] is True
    assert [b for b in projection["blockers"] if b["code"] == "missing_receipt"] == []


async def test_conflict_parks_dependent_but_delivers_independent(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "one", filename="base.txt", content="one\n")
    await feature(setup, "two", filename="base.txt", content="two\n")
    await feature(setup, "dependent")
    await feature(setup, "independent")
    await db.add_dependency("dependent", "two", dep_type="blocks")
    result = await service.sweep("p")
    assert {m["task_id"] for m in result["manifest"]} == {"one", "independent"}


async def test_development_status_does_not_report_strict_policy_missing(setup):
    from src.integration.status import IntegrationStatusService

    db, _service, _source, _remote, _repo = setup
    status = await IntegrationStatusService(db).status("p")
    assert status["effective_mode"] == "development"
    assert status["blockers"] == []
    assert status["policy"]["validation"] == "focused"


async def test_development_status_reports_a_requested_drain(setup):
    """A drain requested from development keeps development effective until it ends.

    Status used to print ``desired_mode: development`` whatever the project row
    said, so a drain in progress read as no drain at all.
    """
    from src.integration.controls import IntegrationControlService

    db, _service, _source, _remote, _repo = setup
    status = await IntegrationControlService(db).status("p")
    assert (status["effective_mode"], status["desired_mode"], status["draining"]) == (
        "development",
        "development",
        False,
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                hierarchical_integration_desired_mode="disabled",
                hierarchical_integration_draining=True,
            )
        )
    status = await IntegrationControlService(db).status("p")
    assert (status["effective_mode"], status["desired_mode"], status["draining"]) == (
        "development",
        "disabled",
        True,
    )


async def test_development_status_lists_live_hierarchy_operations(setup):
    from src.integration.status import IntegrationStatusService

    db, _service, _source, _remote, _repo = setup
    await feature(setup, "parent")
    await _live_parent_operation(db, "parent")

    status = await IntegrationStatusService(db).status("p")

    assert status["live_operations"] == [
        {
            "id": "live-parent-operation",
            "target": {"kind": "parent", "id": "parent"},
            "state": "active",
            "active_stage": 0,
            "created_at": 1.0,
            "updated_at": 2.0,
        }
    ]


async def test_configure_refuses_live_hierarchy_operation_with_safe_command(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "parent")
    await _live_parent_operation(db, "parent")
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                hierarchical_integration_mode="train",
                hierarchical_integration_desired_mode="train",
            )
        )

    with pytest.raises(DevelopmentBusy, match="live-parent-operation") as error:
        await service.configure(
            "p", {"validation": "advisory"}, reason="switch publishers", operator_id="local"
        )

    assert "parent parent" in str(error.value)
    assert "aq integration cancel-preserving live-parent-operation --reason" in str(error.value)
    assert (await db.get_project("p")).hierarchical_integration_mode == "train"


@pytest.mark.parametrize("pending", [False, True])
async def test_control_status_uses_development_publication_blockers(setup, pending):
    from unittest.mock import AsyncMock

    from src.database.tables import development_deliveries
    from src.integration.controls import IntegrationControlService

    db, service, _source, _remote, _repo = setup
    await feature(setup, "done")
    await service.sweep("p")
    if pending:
        async with db.immediate() as conn:
            await conn.execute(update(development_deliveries).where(
                development_deliveries.c.project_id == "p",
                development_deliveries.c.state == "delivered",
            ).values(state="publishing"))
    strict_preflight = AsyncMock(return_value=("repository_binding_failed",))
    controls = IntegrationControlService(db, external_preflight=strict_preflight)
    status = await controls.status("p")
    strict_preflight.assert_not_awaited()
    assert status["ready"] is not pending
    assert {b["code"] for b in status["blockers"]} == (
        {"publication_pending"} if pending else set()
    )
    assert status["deliveries"]
    assert status["blocker_digest"].startswith("sha256:")


async def test_control_status_keeps_strict_preflight_outside_development(setup):
    from unittest.mock import AsyncMock

    from src.integration.controls import IntegrationControlService

    db, _service, _source, _remote, _repo = setup
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_mode="disabled",
            hierarchical_integration_desired_mode="disabled",
        ))
    strict_preflight = AsyncMock(return_value=("repository_binding_failed",))
    status = await IntegrationControlService(db, external_preflight=strict_preflight).status("p")
    strict_preflight.assert_awaited_once_with("p", "r")
    assert "repository_binding_failed" in {b["code"] for b in status["blockers"]}


@pytest.mark.parametrize("repository", [None, "r", "other"])
async def test_development_task_explanation_uses_publisher_repository_rules(setup, repository):
    from src.integration.status import IntegrationStatusService

    db, _service, _source, _remote, _repo = setup
    await db.create_repo(RepoConfig(
        id="other", project_id="p", source_type=RepoSourceType.CLONE, url="/other.git",
    ))
    await db.create_task(Task(
        id="task", project_id="p", title="task", description="", repo_id=repository,
    ))
    result = await IntegrationStatusService(db).task_blockers("task")
    codes = {item["code"] for item in result["blockers"]}
    assert ("repository_not_designated" in codes) == (repository == "other")


async def _second_project(db, *, mode="development"):
    """Project ``web`` with its own repository, as agent-queue-web is to agent-queue."""
    await db.create_project(Project(id="web", name="Web"))
    await db.create_repo(RepoConfig(
        id="web-repo", project_id="web", source_type=RepoSourceType.CLONE, url="/web.git",
    ))
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "web")
            .values(integration_repository_id="web-repo", hierarchical_integration_mode=mode)
        )


async def _completed_elsewhere(setup, task_id, *, project_id, repo_id):
    """A completed task whose branch is on ``p``'s remote but which names *repo_id*."""
    db, _service, source, _remote, _repo = setup
    git(source, "checkout", "-B", task_id, "main")
    (source / (task_id + ".txt")).write_text("moved\n")
    git(source, "add", ".")
    git(source, "commit", "-m", task_id)
    git(source, "push", "origin", task_id)
    await db.create_task(Task(
        id=task_id, project_id=project_id, repo_id=repo_id, title=task_id, description="",
        branch_name=task_id, status=TaskStatus.COMPLETED,
    ))
    return git(source, "rev-parse", "HEAD")


async def test_a_task_moved_between_projects_is_delivered_by_its_new_project(setup):
    """fleet-meadow: filed in agent-queue-web, re-filed into agent-queue by a project
    move that kept agent-queue-web's repository.  The sweep skipped it and readiness
    counted it delivered.  The move now takes the destination's repository."""
    db, service, _source, remote, _repo = setup
    await _second_project(db)
    head = await _completed_elsewhere(setup, "moved", project_id="web", repo_id="web-repo")
    await db.update_task("moved", project_id="p")
    assert (await db.get_task("moved")).repo_id == "r"
    assert await _delivery_pending(db, "moved"), "readiness now waits for its delivery"
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", head, "main") == ""
    assert not await _delivery_pending(db, "moved")


@pytest.mark.parametrize(
    ("destination_mode", "start_repo", "expected"),
    [
        # A repository the destination does not own gives way to the one
        # creation would have chosen there: its designated repository in a
        # delivery mode, none otherwise.
        ("development", "web-repo", "r"),
        ("disabled", "web-repo", None),
        # A repository the destination owns is a deliberate binding.
        ("development", "other", "other"),
        ("development", None, "r"),
    ],
)
async def test_project_move_binds_the_destinations_repository(
    setup, destination_mode, start_repo, expected
):
    db, _service, _source, _remote, _repo = setup
    await _second_project(db)
    await db.create_repo(RepoConfig(
        id="other", project_id="p", source_type=RepoSourceType.CLONE, url="/other.git",
    ))
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(hierarchical_integration_mode=destination_mode)
        )
    await db.create_task(Task(
        id="moved", project_id="web", repo_id=start_repo, title="moved", description="",
    ))
    await db.update_task("moved", project_id="p")
    assert (await db.get_task("moved")).repo_id == expected


async def test_project_move_keeps_an_explicit_repository_and_same_project_edits(setup):
    db, _service, _source, _remote, _repo = setup
    await _second_project(db)
    await db.create_task(Task(
        id="moved", project_id="web", repo_id="web-repo", title="moved", description="",
    ))
    await db.update_task("moved", project_id="web", title="renamed")
    assert (await db.get_task("moved")).repo_id == "web-repo", "not a move"
    await db.update_task("moved", project_id="p", repo_id=None)
    assert (await db.get_task("moved")).repo_id is None, "the caller chose the repository"


async def test_sweep_collects_and_reports_a_task_naming_another_projects_repository(setup):
    """Rows a project move left behind before the move rebound are healed by the
    sweep: rebound to the publisher's repository, delivered, and reported."""
    db, service, _source, remote, _repo = setup
    await _second_project(db)
    head = await _completed_elsewhere(setup, "stranded", project_id="p", repo_id="web-repo")
    assert not await _delivery_pending(db, "stranded"), "the defect: counted as delivered"
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", head, "main") == ""
    assert (await db.get_task("stranded")).repo_id == "r"
    comments = (await db.list_task_comments("stranded"))["comments"]
    assert [c["author_id"] for c in comments] == ["development-integration"]
    assert "`web-repo` to `r`" in comments[0]["body"]
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert len((await db.list_task_comments("stranded"))["comments"]) == 1, "reported once"


async def test_sweep_leaves_a_task_on_another_repository_of_its_own_project(setup):
    db, service, _source, remote, _repo = setup
    await db.create_repo(RepoConfig(
        id="other", project_id="p", source_type=RepoSourceType.CLONE, url="/other.git",
    ))
    before = git(remote, "rev-parse", "main")
    await _completed_elsewhere(setup, "pinned", project_id="p", repo_id="other")
    assert await service.rebind_foreign_repositories("p", _repo) == []
    assert (await service.sweep("p"))["outcome"] == "idle"
    assert (await db.get_task("pinned")).repo_id == "other"
    assert git(remote, "rev-parse", "main") == before


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("attachment", ["attached", "detached", "missing_attempt", "successor"])
async def test_stopped_writer_preserves_dirty_checkout_before_unlock(setup, confirmed, attachment):
    from unittest.mock import AsyncMock

    from sqlalchemy import insert

    from src.database.tables import (
        agents,
        integration_branch_owners,
        sessions,
        task_session_attempts,
        workspaces,
    )

    db, service, source, _remote, _repo = setup
    await feature(setup, "old")
    # A manually paused task is intentionally not requeued by preservation.
    await db.transition_task("old", TaskStatus.PAUSED, context="test", resume_after=None)
    dirty = source / "unpublished.txt"
    dirty.write_text("irreplaceable work\n")
    async with db.immediate() as conn:
        await conn.execute(
            insert(workspaces).values(
                id="w",
                project_id="p",
                workspace_path=str(source),
                locked_by_task_id="old" if attachment == "attached" else None,
                enabled=True,
                created_at=1,
            )
        )
        await conn.execute(
            insert(sessions).values(
                id="s",
                task_id="old" if attachment == "attached" else None,
                project_id="p",
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="old",
                lifecycle="pool",
                state="stopped",
                desired_state="stopped",
                work_dir=str(source),
                epoch="e",
                instance_token="old-instance",
                started_at=1,
                last_claim_epoch=0,
            )
        )
        await conn.execute(
            insert(integration_branch_owners).values(
                id="o",
                repository_id="r",
                ref="old",
                owner_id="old",
                owner_role="worker",
                fence_token=1,
                handoff_state="attached",
                session_id="s",
                workspace_id="w",
                created_at=1,
                updated_at=1,
            )
        )
    async with db.immediate() as conn:
        await conn.execute(insert(agents).values(
            id="a", name="worker", profile_id="worker", state="BUSY", current_task_id="old", created_at=1,
        ))
        await conn.execute(update(sessions).where(sessions.c.id == "s").values(agent_id="a"))
        if attachment in {"detached", "successor"}:
            await conn.execute(insert(task_session_attempts).values(
                id="attempt", session_id="s", task_id="old", project_id="p", agent_id="a",
                profile_id="worker", name="old", lifecycle="pool", harness="codex",
                provider="fake", state="stopped", work_dir=str(source), started_at=1,
                session_started_at=1, ended_at=2,
            ))
        if attachment == "successor":
            await conn.execute(insert(sessions).values(
                id="successor", project_id="p", profile_id="worker", harness="codex",
                provider="fake", name="successor", lifecycle="pool", state="running",
                desired_state="running", work_dir=str(source), epoch="e2",
                instance_token="new-instance", started_at=3,
            ))
    service.confirm_stopped = AsyncMock(return_value=confirmed)
    recovered = confirmed and attachment in {"attached", "detached"}
    result = await service.preserve_stopped_owners("p")
    assert result == (["w"] if recovered else [])
    agent = await db.get_agent("a")
    assert agent.state.value == ("IDLE" if recovered and attachment == "detached" else "BUSY")
    assert dirty.read_text() == "irreplaceable work\n"
    assert git(source, "branch", "--show-current") == ("" if recovered else "old")
    async with db._engine.connect() as conn:
        workspace = (
            (await conn.execute(select(workspaces).where(workspaces.c.id == "w"))).mappings().one()
        )
        owner = (
            (
                await conn.execute(
                    select(integration_branch_owners).where(integration_branch_owners.c.id == "o")
                )
            )
            .mappings()
            .one()
        )
    assert workspace["enabled"] is not recovered
    assert owner["handoff_state"] == ("released" if recovered else "attached")
    assert (await db.get_task("old")).status == TaskStatus.PAUSED


async def test_new_commands_reject_session_principals_without_mutation():
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    handler = IntegrationCommandsMixin()
    with principal_context(
        ExecutionPrincipal(
            kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="s", project_id="p"
        )
    ):
        for command in ("develop", "adopt", "cancel_preserving", "development_sweep"):
            result = await getattr(handler, "_cmd_integration_" + command)({})
            assert result["outcome"] == "unauthorized"


@pytest.mark.parametrize("operation_state", ["active", "cancelled"])
@pytest.mark.parametrize("verifier_writer", ["detached", "running", "unconfirmed", "confirmed"])
async def test_cancel_retires_verifier_with_stop_proof(setup, operation_state, verifier_writer):
    from unittest.mock import AsyncMock

    from sqlalchemy import insert

    from src.database.tables import integration_branch_owners as owners
    from src.database.tables import (
        integration_parent_episodes,
        sessions,
    )
    from src.database.tables import (
        integration_repair_operations as operations,
    )
    from src.database.tables import integration_repair_stages as stages

    db, service, _source, remote, _repo = setup
    await feature(setup, "parent")
    await db.create_task(Task(id="repair", project_id="p", title="repair", description=""))
    await db.create_task(Task(
        id="verifier", project_id="p", title="obsolete verifier", description="",
        status=TaskStatus.BLOCKED,
    ))
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode",
                parent_task_id="parent",
                repository_id="r",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1,
            )
        )
        await conn.execute(
            insert(operations).values(
                id="op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state=operation_state,
                verifier_task_id="verifier",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="old",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(stages).values(
                operation_id="op",
                ordinal=0,
                policy={},
                state="active",
                starting_sha="a" * 40,
                repair_task_id="repair",
                writer_kind="repair_delegate",
                attempts=0,
            )
        )
        await conn.execute(
            insert(owners).values(
                id="o",
                repository_id="r",
                ref="parent",
                owner_id="repair",
                owner_role="repair",
                fence_token=1,
                handoff_state="reserved",
                created_at=1,
                updated_at=1,
            )
        )
    before = git(remote, "show-ref")
    if verifier_writer != "detached":
        async with db.immediate() as conn:
            await conn.execute(insert(sessions).values(
                id="verifier-session", task_id="verifier", project_id="p",
                profile_id="worker", harness="codex", provider="fake", name="verifier",
                lifecycle="task", state="running" if verifier_writer == "running" else "stopped",
                desired_state="stopped", work_dir=str(_source), epoch="e",
                instance_token="verifier-instance", started_at=1, last_claim_epoch=0,
            ))
        service.confirm_stopped = AsyncMock(return_value=verifier_writer == "confirmed")
        if verifier_writer in {"running", "unconfirmed"}:
            with pytest.raises(DevelopmentBusy):
                await service.cancel_preserving("op", reason="must retain writer")
            assert (await db.get_task("verifier")).status == TaskStatus.BLOCKED
            async with db._engine.connect() as conn:
                assert await conn.scalar(select(operations.c.state)) == operation_state
                assert await conn.scalar(select(owners.c.handoff_state)) == "reserved"
            assert git(remote, "show-ref") == before
            return
    result = await service.cancel_preserving("op", reason="already manually delivered")
    assert result["outcome"] == "cancelled"
    assert git(remote, "show-ref") == before
    async with db._engine.connect() as conn:
        assert (
            await conn.scalar(select(owners.c.handoff_state).where(owners.c.id == "o"))
            == "released"
        )
        assert (
            await conn.scalar(select(operations.c.state).where(operations.c.id == "op"))
            == "cancelled"
        )
    assert (await service.rows("p"))[-1]["evidence"]["kind"] == "cancel_preserving"
    assert sorted(result["released_delegates"]) == ["repair", "verifier"]
    # Cancelling settles the delegates in its own transaction rather than
    # leaving them PAUSED for a later tick: terminal, non-success, and never
    # runnable again.  The hold it placed is kept as evidence on the release.
    for task_id in ("repair", "verifier"):
        task = await db.get_task(task_id)
        assert task.status == TaskStatus.FAILED
        assert task.resume_after is None
        assert await db.get_task_meta(task_id, "manual_pause") is None
        record = await db.get_task_meta(task_id, "integration_retirement")
        assert record["disposition"] == "cancelled"
        assert record["previous_hold"]["cleanup_pending"] is False
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(operations.c.verifier_task_id)) == "verifier"
    before_rows = await service.rows("p")
    assert (await service.cancel_preserving("op", reason="replay"))["outcome"] == "already_terminal"
    assert await service.rows("p") == before_rows


async def test_cancel_preserving_recovers_attached_operation_owners(setup):
    from unittest.mock import AsyncMock

    from src.database.tables import (
        integration_branch_owners,
        integration_owner_recoveries,
        integration_parent_episodes,
        integration_repair_operations,
        integration_repair_stages,
    )
    from src.git.manager import GitManager
    from src.integration.owner_recovery import OwnerRecovery

    db, development, source, _remote, _repo = setup
    await feature(setup, "parent")
    await db.create_task(
        Task(
            id="delegate",
            project_id="p",
            title="delegate",
            description="",
            status=TaskStatus.BLOCKED,
        )
    )
    await db.create_workspace(
        Workspace(
            id="base",
            project_id="p",
            workspace_path=str(source),
            source_type=RepoSourceType.CLONE,
            enabled=True,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode",
                parent_task_id="parent",
                repository_id="r",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="operation",
                ordinal=0,
                policy={},
                starting_sha="a" * 40,
                repair_task_id="delegate",
                writer_kind="repair_delegate",
                attempts=0,
                state="active",
            )
        )
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="r",
                ref="parent",
                owner_id="delegate",
                owner_role="repair",
                fence_token=1,
                handoff_state="attached",
                created_at=1.0,
                updated_at=1.0,
            )
        )

    development.owner_recovery = OwnerRecovery(
        db, GitManager(), None, confirm_stopped=AsyncMock(return_value=True)
    )
    result = await development.cancel_preserving("operation", reason="operator cancellation")

    assert result["outcome"] == "cancelled"
    async with db._engine.connect() as conn:
        owner = (
            await conn.execute(
                select(integration_branch_owners).where(integration_branch_owners.c.id == "owner")
            )
        ).mappings().one()
        audit = (
            await conn.execute(
                select(integration_owner_recoveries).where(
                    integration_owner_recoveries.c.owner_row_id == "owner"
                )
            )
        ).mappings().one()
    assert owner["handoff_state"] == "released"
    assert audit["principal"] == "cancel_preserving"


async def test_repair_generation_budget_stops_recursive_dispatch(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    previous = "original"
    for generation in range(1, 4):
        previous = await service.ensure_repair(
            "p",
            "r",
            [{"task_id": previous, "source_sha": "a" * 40}],
            "b" * 40,
            reason="validation failed",
        )
        assert (
            f"Development repair generation: {generation}"
            in (await db.get_task(previous)).description
        )
    assert (
        await service.ensure_repair(
            "p",
            "r",
            [{"task_id": previous, "source_sha": "a" * 40}],
            "b" * 40,
            reason="validation failed",
        )
        is None
    )


async def test_shared_repair_is_required_by_every_parked_source(setup):
    """A shared repair keeps provenance separate from its delivery holds."""
    db, service, _source, _remote, _repo = setup
    one = await feature(setup, "one")
    two = await feature(setup, "two")

    identity = await service.ensure_repair(
        "p",
        "r",
        [
            {"task_id": "one", "source_sha": one},
            {"task_id": "two", "source_sha": two},
        ],
        "b" * 40,
        reason="shared conflict",
    )

    repair = await db.get_task(identity)
    assert repair.parent_task_id is None
    assert set(await db.get_typed_dependencies(identity)) == {
        ("one", "discovered-from"),
        ("two", "discovered-from"),
    }
    for source_id in ("one", "two"):
        assert (identity, "blocks") in await db.get_typed_dependencies(source_id)
        assert (await db.get_task(source_id)).is_blocked


async def test_single_source_repair_is_a_child_without_a_reverse_cycle(setup):
    db, service, _source, _remote, _repo = setup
    source_sha = await feature(setup, "source")

    identity = await service.ensure_repair(
        "p", "r", [{"task_id": "source", "source_sha": source_sha}], "b" * 40,
        reason="single conflict",
    )

    repair = await db.get_task(identity)
    assert repair.parent_task_id == "source"
    assert ("source", "parent-child") in await db.get_typed_dependencies(identity)
    assert (identity, "blocks") not in await db.get_typed_dependencies("source")


@pytest.mark.parametrize("source_depth,target_branch", [(2, "main"), (3, "main"), (3, "release")])
async def test_completed_child_conflict_repair_delivery_releases_dependents(
    setup, source_depth, target_branch,
):
    """A completed child's conflict must dispatch once and release on delivery."""
    db, service, source, remote, _repo = setup
    parents = ["epic", "epic.1"][:source_depth - 1]
    for parent_id in parents:
        await db.create_task(Task(
            id=parent_id, project_id="p", title=parent_id, description="",
            status=TaskStatus.IN_PROGRESS,
        ))
    original = await feature(setup, "conflicted-child", filename="base.txt", content="child\n")
    async with db.immediate() as conn:
        if len(parents) > 1:
            await db.set_parent(parents[1], parents[0], conn=conn)
        await db.set_parent("conflicted-child", parents[-1], conn=conn)
    await db.save_task_completion(TaskCompletion(
        id="child-close", task_id="conflicted-child", outcome="pass",
        commits=[original], completed_at=time.time(),
    ))
    await db.create_task(Task(id="next", project_id="p", title="next", description=""))
    await db.add_dependency("next", "conflicted-child")
    await db.update_repo("r", default_branch=target_branch)
    git(source, "checkout", "main")
    (source / "base.txt").write_text("main\n")
    git(source, "commit", "-am", "advance target with conflicting change")
    git(source, "push", "origin", f"main:{target_branch}")

    assert (await service.sweep("p"))["outcome"] == "idle"
    parked = next(row for row in await service.rows("p") if row["state"] == "parked")
    identity = service._repair_identity(parked["manifest"])
    repair = await db.get_task(identity)
    assert repair is not None, parked["evidence"]
    assert repair.status == TaskStatus.READY
    assert repair.parent_task_id == ("conflicted-child" if source_depth == 2 else None)
    assert ("conflicted-child", "discovered-from") in await db.get_typed_dependencies(identity)
    if source_depth == 3:
        assert (identity, "blocks") in await db.get_typed_dependencies("conflicted-child")
    assert "base.txt" in repair.description
    assert "conflicted-child: conflicted-child" in repair.description
    assert f"starts from origin/{target_branch}" in repair.description
    assert "must stay an ancestor of your branch" in repair.description
    assert f"Publication target: refs/heads/{target_branch}" in repair.description
    assert parked["evidence"]["conflicting_files"] == ["base.txt"]
    dossier = await db.get_task_meta(identity, "development_repair_evidence")
    assert dossier["conflicting_files"] == ["base.txt"]
    assert "publisher_diagnostic" not in parked["evidence"]
    assert (await db.get_task("next")).is_blocked
    await service.sweep("p")
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(tasks.c.id).where(
            tasks.c.id.like("development-repair-%")
        ))).scalars().all() == [identity]

    # The description asks for a merge.  A repair that rebased anyway still
    # releases the source: a new source SHA is expected, and the journal must
    # prove the replacement.
    git(source, "fetch", "origin")
    git(source, "checkout", "-b", repair.branch_name, "conflicted-child")
    with pytest.raises(subprocess.CalledProcessError):
        git(source, "rebase", f"origin/{target_branch}")
    (source / "base.txt").write_text("main and child resolved\n")
    git(source, "add", "base.txt")
    git(source, "-c", "core.editor=true", "rebase", "--continue")
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", repair.branch_name)
    await db.transition_task(identity, TaskStatus.COMPLETED, context="test", force=True)
    await db.save_task_completion(TaskCompletion(
        id="repair-close", task_id=identity, outcome="pass",
        commits=[head], completed_at=time.time(),
    ))
    assert (await db.get_task("next")).is_blocked, "a repair close is not publication"
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert not (await db.get_task("next")).is_blocked
    adopted = next(row for row in await service.rows("p") if row["id"] == parked["id"])
    assert adopted["state"] == "adopted"
    assert adopted["evidence"]["resolved_by_delivered_repair"]["task_id"] == identity
    assert git(remote, "show", f"{target_branch}:base.txt") == "main and child resolved"
    with pytest.raises(subprocess.CalledProcessError):
        git(remote, "merge-base", "--is-ancestor", original, target_branch)


UNREPAIRED = "integration.development_conflicts_unrepaired"


async def _conflicted_source(setup, task_id="conflicted"):
    """A completed source whose change to base.txt conflicts with main, and a successor."""
    db, _service, source, _remote, _repo = setup
    original = await feature(setup, task_id, filename="base.txt", content="child\n")
    await db.save_task_completion(TaskCompletion(
        id=f"{task_id}-close", task_id=task_id, outcome="pass",
        commits=[original], completed_at=time.time(),
    ))
    await db.create_task(Task(id="next", project_id="p", title="next", description=""))
    await db.add_dependency("next", task_id)
    await _advance_main(source, "main\n")
    return original


async def _advance_main(source, content):
    git(source, "checkout", "main")
    (source / "base.txt").write_text(content)
    git(source, "commit", "-am", "advance main with a conflicting change")
    git(source, "push", "origin", "main")


async def _age_parked_rows(db):
    """Move parked rows past the doctor's dispatch grace."""
    async with db.immediate() as conn:
        await conn.execute(
            update(development_deliveries)
            .where(development_deliveries.c.state == "parked")
            .values(created_at=development_deliveries.c.created_at - 3600)
        )


async def _parked(service):
    return [row for row in await service.rows("p") if row["state"] == "parked"]


async def _merge_repair(setup, identity, sources, resolution):
    """Close *identity* the way its description asks: merge each exact source SHA."""
    db, _service, source, _remote, _repo = setup
    git(source, "fetch", "origin")
    git(source, "checkout", "-B", "aq/" + identity, "origin/main")
    for sha in sources:
        try:
            git(source, "merge", "--no-edit", sha)
        except subprocess.CalledProcessError:
            (source / "base.txt").write_text(resolution)
            git(source, "add", "base.txt")
            git(source, "-c", "core.editor=true", "commit", "--no-edit")
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "--force", "origin", "aq/" + identity)
    await db.transition_task(identity, TaskStatus.COMPLETED, context="test", force=True)
    await db.save_task_completion(TaskCompletion(
        id=identity + "-close", task_id=identity, outcome="pass",
        commits=[head], completed_at=time.time(),
    ))
    return head


async def test_parked_conflict_keeps_one_repair_through_source_parked_sweeps(setup):
    """A conflict source skipped as ``source_parked`` keeps exactly one repair.

    ``wise-bridge``: repeated sweeps and an explicit recovery must neither
    duplicate the repair nor the parked row, and must say which repair is
    carrying the source instead of answering only ``merge_conflict``.
    """
    db, service, _source, _remote, _repo = setup
    await _conflicted_source(setup)

    assert (await service.sweep("p"))["outcome"] == "idle"
    [parked] = await _parked(service)
    assert parked["evidence"]["kind"] == "merge_conflict"
    identity = service._repair_identity(parked["manifest"])
    repair = await db.get_task(identity)
    assert repair.status == TaskStatus.READY
    assert "merge each listed source revision by its exact SHA" in repair.description
    assert "Do not rebase, squash or cherry-pick" in repair.description
    for _ in range(2):
        await service.sweep("p")
        skip = await db.get_task_meta("conflicted", PUBLISHER_SKIP_KEY)
        assert (skip["reason"], skip["state"], skip["waiting_on"]) == (
            "source_parked", "waiting", identity
        )
    assert [task.id for task in await _repairs(db)] == [identity]
    assert [row["id"] for row in await _parked(service)] == [parked["id"]]
    await _age_parked_rows(db)
    result = await run_doctor_check(db, UNREPAIRED)
    assert result.severity == Severity.OK, result.detail

    # An explicit recovery merges the parked source again.  It conflicts
    # again, refreshes the same row and names the repair carrying it.
    with pytest.raises(ValueError, match=f"carried by repair {identity} \\(open"):
        await service.recover_child("p", "conflicted")
    [row] = await _parked(service)
    assert row["id"] == parked["id"] and "reconflicted_at" in row["evidence"]
    assert [task.id for task in await _repairs(db)] == [identity]
    assert (await db.get_task("next")).is_blocked

    # A repair that ended without completing leaves the source with none.
    await db.update_task(identity, status=TaskStatus.FAILED.value)
    await service.sweep("p")
    assert [task.id for task in await _repairs(db)] == [identity], "never a second repair"
    assert "waiting_on" not in await db.get_task_meta("conflicted", PUBLISHER_SKIP_KEY)
    result = await run_doctor_check(db, UNREPAIRED)
    assert result.severity == Severity.ERROR
    [finding] = result.data["conflicts"]
    assert finding["task_ids"] == ["conflicted"]
    assert finding["state"] == "finished"
    assert finding["conflicting_files"] == ["base.txt"]
    assert f"{identity} (FAILED)" in result.detail
    with pytest.raises(ValueError, match="has no open repair"):
        await service.recover_child("p", "conflicted")


async def test_reconflicted_repair_chain_carries_the_source_to_delivery(setup):
    """A repair whose own publication conflicts is carried by the next one.

    The first repair of fresh-ember.2 closed pass and then conflicted again
    because main moved; its generation-2 repair was the live work, but the
    source's view named neither.  Merged repairs keep every source an
    ancestor, so the final delivery lands the original revision itself.
    """
    db, service, source, remote, _repo = setup
    original = await _conflicted_source(setup)
    await service.sweep("p")
    [parked] = await _parked(service)
    first = service._repair_identity(parked["manifest"])
    first_head = await _merge_repair(setup, first, [original], "main and child\n")
    await _advance_main(source, "main again\n")

    await service.sweep("p")
    [second_row] = [row for row in await _parked(service) if row["id"] != parked["id"]]
    assert [m["task_id"] for m in second_row["manifest"]] == [first]
    assert second_row["evidence"]["kind"] == "merge_conflict"
    second = service._repair_identity(second_row["manifest"])
    assert "Development repair generation: 2" in (await db.get_task(second)).description
    # The source waits on the repair actually carrying it, not the closed one.
    await service.sweep("p")
    assert (await db.get_task_meta("conflicted", PUBLISHER_SKIP_KEY))["waiting_on"] == second
    await _age_parked_rows(db)
    result = await run_doctor_check(db, UNREPAIRED)
    assert result.severity == Severity.OK, result.detail
    with pytest.raises(
        ValueError, match=f"{first} \\(COMPLETED\\) -> {second} \\(READY\\)"
    ):
        await service.recover_child("p", "conflicted")

    await _merge_repair(setup, second, [first_head], "main again, and child\n")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    await service.sweep("p")
    assert not await _parked(service)
    assert not (await db.get_task("next")).is_blocked
    git(remote, "merge-base", "--is-ancestor", original, "main")
    assert git(remote, "show", "main:base.txt") == "main again, and child"
    assert {task.id for task in await _repairs(db)} == {first, second}


async def test_exhausted_repair_generations_are_named_and_reported(setup):
    """Past the generation budget the batch stays parked, named, and listed."""
    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    last = await _repair_chain(db, service, 3)
    await db.update_task(last, status=TaskStatus.COMPLETED.value)
    diagnostics = []
    assert await service.ensure_repair(
        "p", "r", [{"task_id": last, "source_sha": "a" * 40}], "b" * 40,
        reason="source conflict", diagnostics=diagnostics,
    ) is None
    assert [(d["kind"], d["task_ids"]) for d in diagnostics] == [
        ("repair_generation_exhausted", [last])
    ]

    now = time.time()
    await service.save({
        "id": "exhausted", "project_id": "p", "repository_id": "r",
        "target_ref": "refs/heads/main", "expected_sha": None, "prepared_sha": None,
        "state": "parked", "manifest": [{"task_id": last, "source_sha": "a" * 40}],
        "evidence": {"kind": "merge_conflict", "detail": "CONFLICT",
                     "conflicting_files": ["base.txt"]},
        "reason": "source conflict; independent work may continue",
        "created_at": now, "updated_at": now,
    })
    await service.sweep("p")
    row = next(r for r in await service.rows("p") if r["id"] == "exhausted")
    assert row["evidence"]["publisher_diagnostic"]["kind"] == "repair_generation_exhausted"
    await _age_parked_rows(db)
    result = await run_doctor_check(db, UNREPAIRED)
    assert result.severity == Severity.ERROR
    [finding] = result.data["conflicts"]
    assert (finding["task_ids"], finding["state"], finding["diagnostic"]) == (
        [last], "missing", "repair_generation_exhausted"
    )


def test_repair_chain_states_follow_the_journal():
    from src.integration.development import repair_chain

    identity = DevelopmentIntegration._repair_identity
    source = [{"task_id": "s", "source_sha": "a" * 40}]
    first = identity(source)
    carried = [{"task_id": first, "source_sha": "b" * 40}]
    second = identity(carried)

    def row(state, manifest, target="refs/heads/main", created=1.0):
        return {"id": state + str(created), "repository_id": "r", "target_ref": target,
                "state": state, "manifest": manifest, "created_at": created}

    def chain(history, statuses):
        return repair_chain(source, history, statuses, repository_id="r",
                            target_ref="refs/heads/main")

    parked = [row("parked", source)]
    assert chain(parked, {})["state"] == "missing"
    assert chain(parked, {first: "READY"})["open_repair"] == first
    assert chain(parked, {first: "COMPLETED"})["state"] == "awaiting_publication"
    assert chain(parked + [row("delivered", carried, created=2.0)],
                 {first: "COMPLETED"})["state"] == "delivered"
    # A parent assembly ref is not the target.
    assembly = row("delivered", carried, target="refs/heads/aq/development/parent/x", created=2.0)
    assert chain(parked + [assembly], {first: "COMPLETED"})["state"] == "awaiting_publication"
    reparked = parked + [row("parked", carried, created=2.0)]
    result = chain(reparked, {first: "COMPLETED", second: "IN_PROGRESS"})
    assert (result["open_repair"], [link["task_id"] for link in result["chain"]]) == (
        second, [first, second]
    )
    assert chain(reparked, {first: "COMPLETED", second: "BLOCKED"})["state"] == "finished"
    assert chain(reparked, {first: "COMPLETED"})["state"] == "missing"


def test_development_prime_omits_strict_review_protocol():
    from src.prime.sections import build_completion_protocol_section

    body = build_completion_protocol_section("example", development=True).body
    assert "no squash, PR, hosted CI, or parent verifier is required" in body
    assert "squash its" not in body
    assert "Name that PR" not in body


async def test_untracked_test_output_does_not_block_publication(setup):
    _db, service, _source, _remote, _repo = setup
    await feature(setup, "artifact")
    await service.configure(
        "p",
        {"commands": ["mkdir -p test-cache; echo generated > test-cache/output"]},
        reason="test artifacts are not candidate source",
        operator_id="operator",
    )
    assert (await service.sweep("p"))["outcome"] == "delivered"


async def test_later_consolidation_resolves_conflict_without_starting_repair(setup):
    from sqlalchemy import select

    from src.database.tables import tasks

    db, service, source, _remote, _repo = setup
    await feature(setup, "one", filename="shared", content="one")
    await feature(setup, "two", filename="shared", content="two")
    await feature(setup, "consolidation")
    git(source, "merge", "--no-edit", "one")
    git(source, "merge", "-s", "ours", "--no-edit", "two")
    git(source, "push", "origin", "consolidation")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    async with db._engine.connect() as conn:
        assert (
            await conn.scalar(select(tasks.c.id).where(tasks.c.id.like("development-repair-%")))
            is None
        )
    assert not [row for row in await service.rows("p") if row["state"] == "parked"]


@pytest.mark.parametrize("proof", ["recorded", "short_sha", "legacy", "wrong_contract", "failed_close", "no_close"])
async def test_delivered_rewritten_repair_unblocks_exact_source(setup, proof):
    """A validated replacement can resolve a source without its Git ancestry."""
    from src.database.tables import task_metadata

    db, service, source, remote, _repo = setup
    await feature(setup, "one", filename="shared", content="one")
    original = await feature(setup, "two", filename="shared", content="two")
    await db.create_task(Task(id="next", project_id="p", title="next", description=""))
    await db.add_dependency("next", "two")
    await service.sweep("p")
    parked = next(r for r in await service.rows("p") if r["state"] == "parked")
    identity = service._repair_identity(parked["manifest"])
    repair = await db.get_task(identity)
    assert repair is not None
    if proof == "legacy":
        async with db.immediate() as conn:
            from sqlalchemy import delete
            await conn.execute(delete(task_metadata).where(
                task_metadata.c.task_id == identity,
                task_metadata.c.key == "development_repair_sources",
            ))
    elif proof == "wrong_contract":
        await db.set_task_meta(identity, "development_repair_sources", [])
    git(source, "fetch", "origin")
    git(source, "checkout", "-B", repair.branch_name, "origin/main")
    (source / "shared").write_text("one and two resolved\n")
    git(source, "add", "shared")
    git(source, "commit", "-m", "resolve source without retaining its ancestry")
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", repair.branch_name)
    await db.transition_task(identity, TaskStatus.COMPLETED, context="test", force=True)
    if proof != "no_close":
        await db.save_task_completion(TaskCompletion(
            id="repair-close", task_id=identity,
            outcome="fail" if proof == "failed_close" else "pass",
            commits=[head[:9] if proof == "short_sha" else head], completed_at=time.time(),
        ))
    assert (await db.get_task("next")).is_blocked, "completion alone is not delivery"
    assert (await service.sweep("p"))["outcome"] == "delivered"
    with pytest.raises(subprocess.CalledProcessError):
        git(remote, "merge-base", "--is-ancestor", original, "main")
    row = next(r for r in await service.rows("p") if r["id"] == parked["id"])
    if proof in {"recorded", "short_sha", "legacy"}:
        assert row["state"] == "adopted"
        assert row["evidence"]["resolved_by_delivered_repair"]["completion_id"] == "repair-close"
        assert not (await db.get_task("next")).is_blocked
        assert (await service.sweep("p"))["outcome"] == "idle"
        # A stale close, candidate-only publication, different source revision,
        # or head absent from main must never establish replacement delivery.
        history = await service.rows("p")
        delivery = next(r for r in history if r["state"] == "delivered"
                        and any(m["task_id"] == identity for m in r["manifest"]))
        accepted = git(remote, "rev-parse", "main")
        store = await service.store(_repo)
        for invalid in (
            {"created_at": 0},
            {"target_ref": "refs/heads/candidate"},
            {"state": "prepared"},
            {"manifest": [{"task_id": identity, "source_sha": original}]},
            {"prepared_sha": original},
        ):
            assert await service._delivered_repair(
                _repo, store, accepted, parked["manifest"], [{**delivery, **invalid}],
            ) is None, invalid
    else:
        assert row["state"] == "parked"
        assert (await db.get_task("next")).is_blocked


async def test_delivered_repair_resolves_rewritten_repair_chain(setup):
    db, service, source, remote, _repo = setup
    await feature(setup, "one", filename="shared", content="one")
    await feature(setup, "two", filename="shared", content="two")
    await db.create_task(Task(id="next", project_id="p", title="next", description=""))
    await db.add_dependency("next", "two")
    await service.sweep("p")
    original = next(r for r in await service.rows("p") if r["state"] == "parked")

    async def complete_repair(row, content):
        identity = service._repair_identity(row["manifest"])
        repair = await db.get_task(identity)
        git(source, "fetch", "origin")
        git(source, "checkout", "-B", repair.branch_name, "origin/main")
        (source / "shared").write_text(content)
        git(source, "add", "shared")
        git(source, "commit", "-m", "rewritten repair")
        head = git(source, "rev-parse", "HEAD")
        git(source, "push", "origin", repair.branch_name)
        await db.transition_task(identity, TaskStatus.COMPLETED, context="test", force=True)
        await db.save_task_completion(TaskCompletion(
            id=identity + "-close", task_id=identity, outcome="pass", commits=[head],
            completed_at=time.time(),
        ))
        return identity

    first = await complete_repair(original, "resolved one and two\n")
    git(source, "checkout", "-B", "main", "origin/main")
    (source / "shared").write_text("main changed concurrently\n")
    git(source, "add", "shared")
    git(source, "commit", "-m", "concurrent main change")
    git(source, "push", "origin", "main")
    await service.sweep("p")
    nested = next(r for r in await service.rows("p") if r["state"] == "parked"
                  and r["manifest"][0]["task_id"] == first)
    second = await complete_repair(nested, "resolved one, two and concurrent main\n")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    rows = {r["id"]: r for r in await service.rows("p")}
    assert rows[nested["id"]]["evidence"]["resolved_by_delivered_repair"]["task_id"] == second
    assert rows[original["id"]]["evidence"]["resolved_by_delivered_repair"]["task_id"] == first
    assert not (await db.get_task("next")).is_blocked
    assert git(remote, "show", "main:shared") == "resolved one, two and concurrent main"


@pytest.mark.parametrize("has_origin", [True, False, "relative"])
async def test_configure_discovers_origin_added_after_local_onboarding(setup, has_origin):
    from src.models import Workspace

    db, service, source, remote, _repo = setup
    await db.create_project(Project(id="local", name="Local initialized project"))
    await db.create_workspace(Workspace(
        id="local-base", project_id="local", workspace_path=str(source),
        source_type=RepoSourceType.INIT,
    ))
    if not has_origin:
        git(source, "remote", "remove", "origin")
        with pytest.raises(ValueError, match="designate a repository"):
            await service.configure(
                "local", {"validation": "advisory"}, reason="enable delivery", operator_id="local"
            )
        assert await db.list_repos("local") == []
        assert not (await db.get_project("local")).repo_url
        return
    if has_origin == "relative":
        git(source, "remote", "set-url", "origin", "../remote.git")
    result = await service.configure(
        "local", {"validation": "advisory"}, reason="enable delivery", operator_id="local"
    )
    repository = await db.get_repo(result["repository_id"])
    assert repository.url == str(remote)
    assert (await db.get_project("local")).repo_url == str(remote)
    head = await feature(setup, "local-delivery")
    await db.create_task(Task(
        id="local-source", project_id="local", title="local delivery", description="",
        status=TaskStatus.COMPLETED, branch_name="local-delivery",
    ))
    assert (await service.sweep("local"))["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", head, "main") == ""


async def _park(service, identity, manifest, *, reason="selected validation failed"):
    """Persist a parked batch row exactly as a failed sweep would."""
    now = time.time()
    await service.save(
        {
            "id": identity,
            "project_id": "p",
            "repository_id": "r",
            "target_ref": "refs/heads/main",
            "expected_sha": None,
            "prepared_sha": None,
            "state": "parked",
            "manifest": manifest,
            "evidence": {"kind": "validation", "conclusion": "failed"},
            "reason": reason,
            "created_at": now,
            "updated_at": now,
        }
    )


async def _repair_chain(db, service, depth):
    """Return the id of a generation-*depth* repair, as the incident's chain grew."""
    previous = "original"
    for _ in range(depth):
        previous = await service.ensure_repair(
            "p", "r", [{"task_id": previous, "source_sha": "a" * 40}], "b" * 40,
            reason="validation failed",
        )
    assert f"Development repair generation: {depth}" in (await db.get_task(previous)).description
    return previous


async def test_archived_repair_source_does_not_wedge_the_publisher(setup):
    """The exact incident: manifest -> source closes -> source archived -> tick.

    ``development-repair-8d6e0c872accc17c1d65`` was a generation-3 repair that
    closed pass and was archived.  Every later tick resolved it through the
    live ``tasks`` table only, lost the generation, tried to file a fourth
    repair and raised ``repair source ... is not in project``, which aborted
    the whole sweep for five months of ticks.
    """
    from src.database.queries.hierarchy_queries import HierarchyError

    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    previous = await _repair_chain(db, service, 3)

    await _park(service, "wedged", [{"task_id": previous, "source_sha": "a" * 40}])

    # The generation-3 repair closes pass.  Archive now refuses a task an
    # unsettled batch still names ...
    await db.update_task(previous, status=TaskStatus.COMPLETED.value)
    with pytest.raises(HierarchyError, match="development batch wedged"):
        await db.archive_task(previous)

    # ... but the incident's repair was archived by a daemon that predated that
    # guard, and such rows are still in installs, so the publisher must survive
    # them.  Archive it the way that daemon did.
    # Go straight through the historical row move. The current public
    # archive path additionally refuses this task as undelivered, which is
    # the deliberate D2 protection; monkeypatching only the old development
    # manifest hold would no longer recreate a pre-guard archive.
    async with db.immediate() as conn:
        task = await db._get_task_conn(previous, conn=conn)
        await db._archive_one(task, conn=conn)
    assert await db.get_task(previous) is None
    assert (await db.get_archived_task(previous))["status"] == TaskStatus.COMPLETED.value

    # The next tick must survive it.
    await service.sweep("p")

    # The generation budget is resolved through the archive, so no fourth
    # repair is filed and the archived repair is not resurrected as READY.
    assert await db.get_task(previous) is None
    row = next(r for r in await service.rows("p") if r["id"] == "wedged")
    assert row["state"] == "parked"
    async with db._engine.connect() as conn:
        from src.database.tables import tasks as tasks_table

        live_repairs = set(
            (
                await conn.execute(
                    select(tasks_table.c.id).where(
                        tasks_table.c.id.like("development-repair-%")
                    )
                )
            ).scalars()
        )
    assert previous not in live_repairs


async def test_archived_terminal_source_is_satisfied_without_a_schema_impossible_edge(setup, caplog):
    """An archived terminal source still yields a repair, minus its FK-bound edges.

    ``task_dependencies.depends_on_task_id`` references ``tasks.id``, so no
    provenance edge to an archived source can exist.  The publisher records
    that as a structured log line and carries on; it never raises.
    """
    import logging

    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    await db.update_task("original", status=TaskStatus.COMPLETED.value)
    # This models legacy data written before D2 made undelivered development
    # work unarchivable through the public archive command.
    async with db.immediate() as conn:
        task = await db._get_task_conn("original", conn=conn)
        await db._archive_one(task, conn=conn)
    assert await db.get_task("original") is None

    with caplog.at_level(logging.INFO, logger="src.integration.development"):
        identity = await service.ensure_repair(
            "p", "r", [{"task_id": "original", "source_sha": "a" * 40}], "b" * 40,
            reason="validation failed",
        )

    repair = await db.get_task(identity)
    assert repair is not None and repair.project_id == "p"
    assert repair.parent_task_id is None
    assert await db.get_typed_dependencies(identity) == []
    assert any(
        "original" in record.getMessage() and "archived" in record.getMessage()
        for record in caplog.records
    )


async def test_absent_repair_source_is_a_named_batch_diagnostic(setup):
    """A source that exists nowhere is reported on the batch, not raised."""
    db, service, _source, _remote, _repo = setup
    await _park(service, "ghost", [{"task_id": "never-existed", "source_sha": "a" * 40}])

    await service.sweep("p")

    row = next(r for r in await service.rows("p") if r["id"] == "ghost")
    assert row["state"] == "parked"
    diagnostic = row["evidence"]["publisher_diagnostic"]
    assert diagnostic["kind"] == "repair_source_missing"
    assert diagnostic["task_ids"] == ["never-existed"]
    assert diagnostic["consecutive_ticks"] == 1
    assert await db.get_task(service._repair_identity(row["manifest"])) is None

    # A second tick counts the repetition instead of raising or duplicating.
    await service.sweep("p")
    row = next(r for r in await service.rows("p") if r["id"] == "ghost")
    assert row["evidence"]["publisher_diagnostic"]["consecutive_ticks"] == 2


async def test_one_unresolvable_batch_does_not_abort_the_sweep_for_the_rest(setup):
    """Isolation: the first bad row must not starve every later parked row."""
    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    await feature(setup, "later")

    await _park(service, "ghost", [{"task_id": "never-existed", "source_sha": "a" * 40}])
    await _park(service, "healthy", [{"task_id": "later", "source_sha": "c" * 40}])

    await service.sweep("p")

    healthy = next(r for r in await service.rows("p") if r["id"] == "healthy")
    identity = service._repair_identity(healthy["manifest"])
    repair = await db.get_task(identity)
    assert repair is not None, "a later parked row still gets its repair"
    assert ("later", "parent-child") in await db.get_typed_dependencies(identity)


async def test_tick_isolates_one_failing_project_and_logs_one_warning(setup, caplog):
    """A failing project logs one structured warning, not a rich traceback."""
    import logging

    db, service, _source, _remote, _repo = setup
    await db.create_project(Project(id="q", name="Second"))
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "q")
            .values(
                hierarchical_integration_mode="development",
                hierarchical_integration_policy={"validation": "focused", "commands": ["true"]},
                integration_repository_id=None,
            )
        )
    swept = []
    original = service.sweep

    async def sweep(project_id, **kwargs):
        swept.append(project_id)
        return await original(project_id, **kwargs)

    service.sweep = sweep
    service.next_due.clear()  # ``configure`` already armed p's interval
    with caplog.at_level(logging.WARNING, logger="src.integration.development"):
        await service.tick(time.time())

    assert "p" in swept, "a broken project must not stop the rest of the fleet"
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("q" in message for message in messages)
    assert not any(record.exc_info for record in caplog.records), "no rich traceback"


async def test_archived_repair_is_not_resurrected_as_a_fresh_ready_task(setup):
    """An archived repair has been filed; re-filing it loses its completion.

    ``development-repair-dfda02e25d80e0d1ab2d`` was found in ``tasks`` as READY
    and in ``archived_tasks`` as COMPLETED at the same time — one row per tick
    that read the live table alone.
    """
    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    manifest = [{"task_id": "original", "source_sha": "a" * 40}]
    identity = await service.ensure_repair(
        "p", "r", manifest, "b" * 40, reason="validation failed"
    )
    await db.update_task(identity, status=TaskStatus.COMPLETED.value)
    # Existing archives must never be recreated as READY, even though the
    # current D2 archive guard would retain this undelivered repair.
    async with db.immediate() as conn:
        task = await db._get_task_conn(identity, conn=conn)
        await db._archive_one(task, conn=conn)
    assert await db.get_task(identity) is None

    assert (
        await service.ensure_repair("p", "r", manifest, "b" * 40, reason="validation failed")
        == identity
    )
    assert await db.get_task(identity) is None, "the archived repair stays archived"


async def test_invalid_policy_arms_its_own_deadline_instead_of_spinning(setup):
    """A project whose stored policy no longer validates is skipped, not retried at 5s."""
    db, service, _source, _remote, _repo = setup
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(hierarchical_integration_policy={"validation": "focused", "commands": []})
        )
    now = time.time()
    service.next_due.clear()
    await service.tick(now)
    assert service.next_due["p"] > now, "the deadline is armed even though validation failed"


# -- branches a delivery made obsolete (quick-ridge) -----------------------


async def aq_feature(setup, task_id, *, filename=None, content="new\n"):
    """A COMPLETED task on ``aq/<task_id>`` whose close reports its head."""
    db, _service, source, _remote, _repo = setup
    branch = "aq/" + task_id
    git(source, "checkout", "-B", branch, "main")
    (source / (filename or task_id + ".txt")).write_text(content)
    git(source, "add", ".")
    git(source, "commit", "-m", task_id)
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", branch)
    await db.create_task(
        Task(
            id=task_id, project_id="p", repo_id="r", title=task_id, description="",
            branch_name=branch, status=TaskStatus.COMPLETED,
        )
    )
    await db.save_task_completion(
        TaskCompletion(
            id=f"close-{task_id}", task_id=task_id, outcome="pass", commits=[head],
            completed_at=time.time(),
        )
    )
    return head


def remote_branches(remote):
    return set(git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())


def candidate_branch(head):
    import hashlib

    return f"aq/development/{hashlib.sha256(b'p').hexdigest()[:12]}/{head}"


def assert_restorable(remote, bundle, branch, sha):
    """Restore *branch* at *sha* from *bundle* the way the deletion log says to."""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        clone = Path(scratch) / "restore"
        git(Path(scratch), "clone", "-q", str(remote), str(clone))
        git(clone, "bundle", "unbundle", bundle)
        git(clone, "push", "-q", "origin", f"{sha}:refs/heads/{branch}")
    assert git(remote, "rev-parse", branch) == sha


async def journal_row(service, identity):
    return next(r for r in await service.rows("p") if r["id"] == identity)


async def test_delivery_deletes_task_candidate_and_parent_branches(setup):
    db, service, _source, remote, _repo = setup
    await db.create_task(
        Task(id="epic", project_id="p", title="epic", description="", status=TaskStatus.PAUSED)
    )
    await aq_feature(setup, "one")
    await aq_feature(setup, "child")
    async with db.immediate() as conn:
        await db.set_parent("child", "epic", conn=conn)
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    before = remote_branches(remote)
    parents = {b for b in before if b.startswith("aq/development/parent/")}
    assert {"aq/one", "aq/child", candidate_branch(result["head_sha"])} <= before
    assert len(parents) == 1
    armed = await journal_row(service, result["id"])
    assert armed["evidence"]["branch_cleanup"] == {"state": "pending", "attempts": 0}

    collected = await service.collect_delivered_branches("p")

    assert remote_branches(remote) == {"main"}
    [record] = collected["rows"]
    assert record["state"] == "complete"
    assert sorted((d["branch"], d["kind"]) for d in record["deleted"]) == sorted(
        [("aq/child", "task"), ("aq/one", "task"),
         (candidate_branch(result["head_sha"]), "assembly"), (parents.pop(), "assembly")]
    )
    stored = (await journal_row(service, result["id"]))["evidence"]["branch_cleanup"]
    assert stored["deleted"] == record["deleted"]
    assert stored["kept"] == [] and stored["missing"] == []
    # Idempotent, and delivery itself is unaffected by the missing sources.
    assert (await service.collect_delivered_branches("p"))["outcome"] == "idle"
    assert (await service.sweep("p"))["outcome"] == "idle"
    events = [e for e in await db.get_recent_events(20)
              if e["event_type"] == "development.branches_deleted"]
    assert len(events) == 1


async def test_tick_collects_right_after_the_sweep_delivers(setup):
    _db, service, _source, remote, _repo = setup
    await aq_feature(setup, "one")
    service.next_due.clear()  # configure() armed the periodic deadline
    await service.tick(time.time())
    assert remote_branches(remote) == {"main"}
    # A later tick rechecks git: a receipt cannot hide external target movement.
    service.next_due.clear()
    with patch.object(service, "store", wraps=service.store) as store:
        await service.tick(time.time())
    store.assert_awaited_once()


async def test_due_branch_cleanup_runs_after_an_earlier_delivery(setup):
    _db, service, _source, remote, _repo = setup
    await aq_feature(setup, "one")
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert "aq/one" in remote_branches(remote)
    service.next_due.clear()
    await service.tick(time.time())
    assert remote_branches(remote) == {"main"}


async def test_a_branch_that_moved_past_its_delivery_is_kept(setup):
    _db, service, source, remote, _repo = setup
    await aq_feature(setup, "one")
    result = await service.sweep("p")
    git(source, "checkout", "aq/one")
    (source / "later.txt").write_text("later\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "work after delivery")
    git(source, "push", "origin", "aq/one")

    [record] = (await service.collect_delivered_branches("p"))["rows"]

    assert remote_branches(remote) == {"main", "aq/one"}
    assert record["state"] == "complete"
    assert record["kept"] == [{"branch": "aq/one", "reason": "has commits not on main"}]
    assert [d["branch"] for d in record["deleted"]] == [candidate_branch(result["head_sha"])]


@pytest.mark.parametrize("hold", ["reopened", "owner", "namesake"])
async def test_a_branch_something_still_references_is_kept(setup, hold):
    from src.database.tables import integration_branch_owners

    db, service, _source, remote, _repo = setup
    await aq_feature(setup, "one")
    await service.sweep("p")
    if hold == "reopened":
        await db.transition_task("one", TaskStatus.READY, context="test", force=True)
    elif hold == "owner":
        async with db.immediate() as conn:
            await conn.execute(insert(integration_branch_owners).values(
                id="owner-one", repository_id="r", ref="refs/heads/aq/one", owner_id="one",
                owner_role="worker", fence_token=1, handoff_state="reserved",
                created_at=1.0, updated_at=1.0,
            ))
    else:
        await db.create_task(Task(
            id="redeliver", project_id="p", title="re-deliver one", description="",
            branch_name="aq/one", status=TaskStatus.IN_PROGRESS,
        ))

    [record] = (await service.collect_delivered_branches("p"))["rows"]

    assert "aq/one" in remote_branches(remote)
    [kept] = [k for k in record["kept"] if k["branch"] == "aq/one"]
    assert {
        "reopened": "task is READY",
        "owner": "integration owner one is reserved",
        "namesake": "task redeliver is IN_PROGRESS",
    }[hold] == kept["reason"]


async def test_parked_candidate_waits_then_goes_with_its_repair(setup):
    """A superseded candidate is deleted once every member it carries landed."""
    db, service, source, remote, _repo = setup
    await service.configure(
        "p", {"commands": ["test ! -f bad.txt"]}, reason="reject bad.txt", operator_id="local"
    )
    await aq_feature(setup, "bad", filename="bad.txt")
    await aq_feature(setup, "good")
    parked = await service.sweep("p")
    assert parked["outcome"] == "parked"
    held_candidate = candidate_branch(parked["head_sha"])

    # Unrelated work lands while the batch is parked: only its own refs go.
    await aq_feature(setup, "free")
    free = await service.sweep("p")
    assert free["outcome"] == "delivered"
    await service.collect_delivered_branches("p")
    assert remote_branches(remote) == {"main", "aq/bad", "aq/good", held_candidate}

    # The repair worker resolves the parked batch on its own branch.
    row = next(r for r in await service.rows("p") if r["state"] == "parked")
    identity = service._repair_identity(row["manifest"])
    repair = await db.get_task(identity)
    git(source, "fetch", "origin")
    git(source, "checkout", "-B", repair.branch_name, "origin/main")
    git(source, "merge", "--no-edit", "origin/aq/bad", "origin/aq/good")
    git(source, "rm", "-q", "bad.txt")
    git(source, "commit", "-m", "repair: drop bad.txt")
    repaired = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", repair.branch_name)
    await db.transition_task(identity, TaskStatus.COMPLETED, context="test", force=True)
    await db.save_task_completion(TaskCompletion(
        id="repair-close", task_id=identity, outcome="pass", commits=[repaired],
        completed_at=time.time(),
    ))
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert (await journal_row(service, row["id"]))["state"] == "adopted"

    await service.collect_delivered_branches("p")

    assert remote_branches(remote) == {"main"}


async def test_a_failed_repair_is_moot_once_its_source_lands_on_its_own(setup):
    db, service, source, remote, _repo = setup
    await service.configure(
        "p", {"commands": ["test ! -f bad.txt"]}, reason="reject bad.txt", operator_id="local"
    )
    await aq_feature(setup, "bad", filename="bad.txt")
    parked = await service.sweep("p")
    assert parked["outcome"] == "parked"
    row = next(r for r in await service.rows("p") if r["state"] == "parked")
    repair = await db.get_task(service._repair_identity(row["manifest"]))
    # The repair pushed a partial attempt and then failed.
    git(source, "fetch", "origin")
    git(source, "checkout", "-B", repair.branch_name, "origin/main")
    (source / "attempt.txt").write_text("attempt\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "partial repair")
    git(source, "push", "origin", repair.branch_name)
    await db.transition_task(repair.id, TaskStatus.FAILED, context="test", force=True)
    # An operator lands the source on main by hand and relaxes the policy.
    git(source, "checkout", "-B", "main", "origin/main")
    git(source, "merge", "--no-edit", "origin/aq/bad")
    git(source, "push", "origin", "main")
    await service.configure(
        "p", {"commands": ["true"]}, reason="accept bad.txt", operator_id="local"
    )
    assert (await service.sweep("p"))["outcome"] == "delivered"
    resolved = await journal_row(service, row["id"])
    assert "resolved_by_main_ancestry" in resolved["evidence"]

    attempt = git(remote, "rev-parse", repair.branch_name)

    result = await service.collect_delivered_branches("p")

    assert remote_branches(remote) == {"main"}
    deleted = {d["branch"]: d for r in result["rows"] for d in r["deleted"]}
    assert deleted[repair.branch_name]["kind"] == "moot_repair"
    assert deleted["aq/bad"]["kind"] == "task"
    # Only the unmerged tip needed a bundle; it restores into a fresh clone.
    bundle = deleted[repair.branch_name]["backup"]
    assert "backup" not in deleted["aq/bad"]
    assert_restorable(remote, bundle, repair.branch_name, attempt)
    assert remote_branches(remote) == {"main", repair.branch_name}
    [log] = {r["log"] for r in result["rows"]}
    logged = {line.split("\t")[0]: line.split("\t") for line in Path(log).read_text().splitlines()}
    assert logged[repair.branch_name][1] == attempt
    assert logged[repair.branch_name][3] == bundle
    assert logged["aq/bad"][3] == "-"


async def test_unconfirmed_deletes_retry_with_backoff_then_give_up(setup, monkeypatch):
    from src.git.manager import GitError
    from src.integration import development

    _db, service, _source, remote, _repo = setup
    await aq_feature(setup, "one")
    result = await service.sweep("p")

    async def unreachable(*_args, **_kwargs):
        raise GitError("remote hung up")

    monkeypatch.setattr(development, "delete_branches", unreachable)
    now = time.time()
    [record] = (await service.collect_delivered_branches("p", now=now))["rows"]
    assert record["state"] == "pending"
    assert record["attempts"] == 1
    assert record["last_error"] == "GitError: remote hung up"
    assert record["next_attempt_at"] == now + development.BRANCH_CLEANUP_RETRY_SECONDS
    # Not due yet: nothing is attempted.
    assert (await service.collect_delivered_branches("p", now=now + 1))["outcome"] == "idle"

    for attempt in range(2, development.BRANCH_CLEANUP_MAX_ATTEMPTS + 1):
        now += development.BRANCH_CLEANUP_RETRY_MAX_SECONDS
        [record] = (await service.collect_delivered_branches("p", now=now))["rows"]
        assert record["attempts"] == attempt
    assert record["state"] == "exhausted"
    assert record["next_attempt_at"] is None
    assert (await service.collect_delivered_branches("p", now=now * 2))["outcome"] == "idle"
    assert "aq/one" in remote_branches(remote)
    stored = (await journal_row(service, result["id"]))["evidence"]["branch_cleanup"]
    assert stored["state"] == "exhausted"


async def test_a_delete_the_remote_did_not_apply_is_retried(setup, monkeypatch):
    from src.integration import development

    _db, service, _source, remote, _repo = setup
    await aq_feature(setup, "one")
    await service.sweep("p")
    real = development.delete_branches

    async def ignored(git_, run_git, store, targets, **_kwargs):
        outcomes = {branch: "failed" for branch in targets}
        return {"outcomes": outcomes, "bundle": None, "bundled": [], "log": None}

    monkeypatch.setattr(development, "delete_branches", ignored)
    now = time.time()
    [record] = (await service.collect_delivered_branches("p", now=now))["rows"]
    assert record["state"] == "pending"
    assert record["last_error"].startswith("2 delete(s) not confirmed")
    monkeypatch.setattr(development, "delete_branches", real)
    [record] = (
        await service.collect_delivered_branches(
            "p", now=now + development.BRANCH_CLEANUP_RETRY_SECONDS
        )
    )["rows"]
    assert record["state"] == "complete"
    assert record["attempts"] == 2
    assert remote_branches(remote) == {"main"}


async def test_rows_from_before_branch_cleanup_are_left_to_doctor(setup):
    _db, service, _source, remote, _repo = setup
    await aq_feature(setup, "one")
    result = await service.sweep("p")
    row = await journal_row(service, result["id"])
    evidence = {k: v for k, v in row["evidence"].items() if k != "branch_cleanup"}
    await service.change(result["id"], evidence=evidence)

    assert (await service.collect_delivered_branches("p"))["outcome"] == "idle"
    assert "aq/one" in remote_branches(remote)


async def test_adoption_and_reconciled_publish_arm_branch_cleanup(setup):
    _db, service, _source, remote, _repo = setup
    head = await aq_feature(setup, "one")
    published = await service.sweep("p")
    # A crash between push and journal: reconciliation confirms the publish.
    await service.change(
        published["id"], state="publishing",
        evidence={k: v for k, v in (await journal_row(service, published["id"]))[
            "evidence"].items() if k != "branch_cleanup"},
    )
    await service.sweep("p")
    reconciled = await journal_row(service, published["id"])
    assert reconciled["state"] == "delivered"
    assert reconciled["evidence"]["branch_cleanup"]["state"] == "pending"

    await aq_feature(setup, "two")
    main = git(remote, "rev-parse", "main")
    adopted = await service.adopt(
        project_id="p", task_ids=["two"], target_ref="refs/heads/main", head_sha=main,
        reason="landed elsewhere", operator_id="local", accept_equivalent=True,
    )
    assert (await journal_row(service, adopted["id"]))["evidence"]["branch_cleanup"] == {
        "state": "pending", "attempts": 0,
    }
    await service.collect_delivered_branches("p")
    assert remote_branches(remote) == {"main"}
    assert head


async def test_live_branch_references_names_every_hold(setup):
    from src.database.tables import (
        development_deliveries,
        integration_branch_owners,
        task_branch_origins,
    )
    from src.integration.delivery_branches import live_branch_references

    db, _service, _source, _remote, _repo = setup
    await db.create_task(Task(
        id="running", project_id="p", title="t", description="", branch_name="aq/running",
        status=TaskStatus.IN_PROGRESS,
    ))
    await aq_feature(setup, "undelivered")
    await db.create_task(Task(
        id="development-repair-x", project_id="p", title="t", description="",
        branch_name="aq/development-repair-x", status=TaskStatus.READY,
    ))
    await db.set_task_meta(
        "development-repair-x", "development_repair_sources",
        [{"task_id": "repair-src", "source_sha": "c" * 40}],
    )
    await _live_parent_operation(db, "running")
    now = time.time()
    async with db.immediate() as conn:
        manifest = [{"task_id": "gone-src", "source_sha": "a" * 40}]
        journal = {"project_id": "p", "repository_id": "r", "manifest": manifest,
                   "evidence": {}, "reason": "t", "created_at": now, "updated_at": now}
        await conn.execute(insert(development_deliveries).values([
            {**journal, "id": "parked", "target_ref": "refs/heads/main", "state": "parked"},
            {**journal, "id": "cand", "state": "delivered",
             "target_ref": "refs/heads/aq/development/abc/" + "a" * 40},
        ]))
        owner = {"repository_id": "r", "owner_role": "worker", "fence_token": 0,
                 "created_at": now, "updated_at": now}
        await conn.execute(insert(integration_branch_owners).values([
            {**owner, "id": "o1", "ref": "aq/owned", "owner_id": "owned",
             "handoff_state": "attached"},
            {**owner, "id": "o2", "ref": "aq/released", "owner_id": "released",
             "handoff_state": "released"},
        ]))
        await conn.execute(insert(task_branch_origins).values(
            id="origin", task_id="discarding", repository_id="r", base_sha="b" * 40,
            branch_name="aq/epic/discarding",
            creation_generation=0, reserved=True, materialized=True, retired_at=now,
            created_at=now, discard_state="pending",
        ))
        holds = await live_branch_references(conn)

    assert holds["aq/running"] == "task running is IN_PROGRESS"
    assert holds["aq/running-wip"] == "task running is IN_PROGRESS"
    assert holds["aq/undelivered"] == "task undelivered is not delivered yet"
    # A member no table resolves still holds its conventional branch.
    assert holds["aq/gone-src"] == "development batch parked is parked"
    assert holds["aq/development/abc/" + "a" * 40] == (
        "assembly carries an undelivered batch member"
    )
    assert holds["aq/repair-src"] == "open development repair development-repair-x"
    assert holds["aq/owned"] == "integration owner owned is attached"
    assert holds["aq/epic/discarding"] == "branch discard is pending"
    assert "aq/released" not in holds
    assert "main" not in holds  # never a candidate: deleters skip the default branch


# Branch-name shapes from the supervisor's 2026-09-21 cleanup
# (~/.agent-queue/backups/deleted-branches-2026-09-21.tsv): the deletion log's
# first two columns keep that file's ``branch<TAB>sha`` layout.
PRECEDENT_TASK = "aq/agile-glacier.3"
PRECEDENT_REPAIR = "aq/development-repair-011da3eb42dd37b0c591"
PRECEDENT_INTEGRATION = (
    "aq/integration/p-aeacda21cbc3fe134dede52ddb8a7a63/r-23902b8d33d997d489c44edb309aa723"
)
PRECEDENT_INTEGRATION_HELD = (
    "aq/integration/p-aeacda21cbc3fe134dede52ddb8a7a63/r-27eff558cea9166e9a0a6a3f58540a1e"
)
PRECEDENT_NON_AQ = (
    "fix/workflow-startup-policy", "worktree-harness-id-class-slice",
    "integrate/provider-usage", "gh-pages",
)


def commit_on(source, branch, base, filename, *, message=None, date=None):
    git(source, "checkout", "-B", branch, base)
    (source / filename).write_text(branch + "\n")
    git(source, "add", ".")
    git(source, "commit", "-m", message or filename, *(["--date", date] if date else []))
    return git(source, "rev-parse", "HEAD")


def stale_ctx(db, tmp_path):
    from types import SimpleNamespace

    from src.doctor.models import DoctorContext

    return DoctorContext(config=SimpleNamespace(data_dir=str(tmp_path / "doctor")), db=db)


async def seed_integration_owner(db, branch, *, state, operation_state):
    from src.database.tables import (
        integration_batches,
        integration_branch_owners,
        integration_repair_operations,
    )

    suffix = branch.rsplit("-", 1)[-1][:8]
    now = time.time()
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(
            id=f"batch-{suffix}", project_id="p", repository_id="r", request_id=f"req-{suffix}",
            source_manifest_digest="d", lifecycle="aborted", base_sha="b" * 40,
            integration_branch=f"refs/heads/{branch}",
            policy_snapshot={}, artifact_snapshot={}, cleanup_state="complete",
            created_at=now, updated_at=now,
        ))
        await conn.execute(insert(integration_repair_operations).values(
            id=f"repair-batch-{suffix}", target_kind="batch", batch_id=f"batch-{suffix}",
            episode_id=f"batch-{suffix}", active_stage=0, state=operation_state, policy_snapshot={}, artifact_snapshot={},
            required_check_version="checks-v1", created_at=now, updated_at=now,
        ))
        await conn.execute(insert(integration_branch_owners).values(
            id=f"owner-{suffix}", repository_id="r", ref=f"refs/heads/{branch}",
            owner_id=f"repair-batch-{suffix}", owner_role="collector", fence_token=1,
            handoff_state=state, created_at=now, updated_at=now,
        ))


async def test_stale_branches_doctor_applies_the_branch_policy(setup, tmp_path):
    from src.doctor.git_checks import CHECKS
    from src.doctor.models import Severity
    from src.doctor.runner import apply_fix

    db, service, source, remote, _repo = setup
    # Delivered before branch cleanup existed: the task and its candidate linger.
    await aq_feature(setup, "old")
    delivered = await service.sweep("p")
    row = await journal_row(service, delivered["id"])
    await service.change(delivered["id"], evidence={
        k: v for k, v in row["evidence"].items() if k != "branch_cleanup"
    })
    git(source, "fetch", "origin")
    # Landed by subject: a rebased copy with the same author stamp is on main.
    commit_on(source, PRECEDENT_TASK, "origin/main", "rebased.txt", message="rebased work")
    git(source, "push", "origin", PRECEDENT_TASK)
    git(source, "checkout", "-B", "main", "origin/main")
    commit_on(source, "main", "main", "main-only.txt")
    git(source, "cherry-pick", PRECEDENT_TASK)
    git(source, "push", "origin", "main")
    main = git(source, "rev-parse", "HEAD")
    # Same subject, different author time: different work, kept.
    commit_on(source, "aq/lookalike", main, "lookalike.txt", message="rebased work",
              date="2020-01-01T00:00:00")
    # A twin commit plus a hand-resolved merge carrying work of its own: kept.
    git(source, "checkout", "-B", "aq/evil", PRECEDENT_TASK)
    git(source, "merge", "--no-commit", "--no-ff", f"{main}~1")
    (source / "only-in-the-merge.txt").write_text("resolution work\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "merge main")
    # Rule (a): a released owner and a finished operation; and one still reserved.
    released = commit_on(source, PRECEDENT_INTEGRATION, main, "aborted-batch.txt")
    commit_on(source, PRECEDENT_INTEGRATION_HELD, main, "reserved-batch.txt")
    await seed_integration_owner(db, PRECEDENT_INTEGRATION, state="released",
                                 operation_state="cancelled")
    await seed_integration_owner(db, PRECEDENT_INTEGRATION_HELD, state="reserved",
                                 operation_state="completed")
    # Rule (b): a FAILED repair terminal for 15 days, and one for 13 days.
    expired = commit_on(source, PRECEDENT_REPAIR, main, "failed-repair.txt")
    commit_on(source, "aq/recent-failure", main, "recent.txt")
    for task_id, branch, days in (
        ("development-repair-011da3eb42dd37b0c591", PRECEDENT_REPAIR, 15),
        ("recent-failure", "aq/recent-failure", 13),
    ):
        await db.create_task(Task(
            id=task_id, project_id="p", repo_id="r", title=task_id, description="",
            branch_name=branch, status=TaskStatus.FAILED,
        ))
        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
                updated_at=time.time() - days * 86400
            ))
    # Held (a live task at main's head), and branches outside aq/.
    await db.create_task(Task(
        id="fresh", project_id="p", title="fresh", description="", branch_name="aq/fresh",
        status=TaskStatus.READY,
    ))
    git(source, "push", "-q", "origin", "aq/lookalike", "aq/evil", PRECEDENT_INTEGRATION,
        PRECEDENT_INTEGRATION_HELD, PRECEDENT_REPAIR, "aq/recent-failure",
        f"{main}:refs/heads/aq/fresh", *(f"{main}:refs/heads/{name}" for name in PRECEDENT_NON_AQ))
    [check] = [c for c in CHECKS if c.id == "git.stale_branches"]
    ctx = stale_ctx(db, tmp_path)

    found = await check.run(ctx)

    assert found.severity == Severity.WARN and found.fixable
    [project] = found.data["projects"]
    rules = {e["branch"]: e["rule"] for e in project["branches"]}
    assert rules == {
        "aq/old": "landed", candidate_branch(delivered["head_sha"]): "landed",
        PRECEDENT_TASK: "landed", PRECEDENT_INTEGRATION: "integration",
        PRECEDENT_REPAIR: "expired",
    }
    assert project["by_subject"] == 1
    held = {e["branch"]: e["held_by"] for e in project["held_examples"]}
    assert held == {"aq/fresh": "task fresh is READY"}
    # No rule applies to lookalike, evil, the 13-day failure or the integration
    # ref whose owner is still reserved: they are simply kept.
    assert project["kept"] == 4
    assert project["out_of_scope"] == len(PRECEDENT_NON_AQ)

    fixed = await apply_fix(check, ctx)

    assert fixed.severity == Severity.OK
    assert remote_branches(remote) == {
        "main", "aq/fresh", "aq/lookalike", "aq/evil", "aq/recent-failure",
        PRECEDENT_INTEGRATION_HELD, *PRECEDENT_NON_AQ,
    }
    # Every deletion is restorable: unmerged tips from the bundle, the rest by sha.
    backups = tmp_path / "doctor" / "backups" / "branch-deletions"
    [log] = backups.glob("*.tsv")
    lines = [line.split("\t") for line in log.read_text().splitlines()]
    logged = {fields[0]: fields for fields in lines}
    assert set(logged) == set(rules)
    assert {len(fields) for fields in lines} == {6}
    # A rebased copy's own commits are not on main either, so it is bundled too.
    for branch in (PRECEDENT_INTEGRATION, PRECEDENT_REPAIR, PRECEDENT_TASK):
        assert logged[branch][3].endswith(".bundle")
    for branch in ("aq/old", candidate_branch(delivered["head_sha"])):
        assert logged[branch][3] == "-"
    assert "cancelled" in logged[PRECEDENT_INTEGRATION][2]
    assert "FAILED since" in logged[PRECEDENT_REPAIR][2]
    assert_restorable(remote, logged[PRECEDENT_REPAIR][3], PRECEDENT_REPAIR, expired)
    assert_restorable(remote, logged[PRECEDENT_INTEGRATION][3], PRECEDENT_INTEGRATION, released)
    events = [e for e in await db.get_recent_events(20)
              if e["event_type"] == "git.stale_branches_deleted"]
    assert len(events) == 1


async def test_abandoned_work_waits_fourteen_days_like_a_failure(setup):
    from src.database.tables import task_completion_records
    from src.integration.delivery_branches import expired_task_branches

    db, _service, _source, _remote, _repo = setup
    now = time.time()
    for task_id, status in (("abandoned-meta", TaskStatus.COMPLETED),
                            ("abandoned-close", TaskStatus.FAILED),
                            ("reopened", TaskStatus.READY)):
        await db.create_task(Task(
            id=task_id, project_id="p", title=task_id, description="",
            branch_name=f"aq/{task_id}", status=status,
        ))
    await db.set_task_meta("abandoned-meta", "work_outcome", "abandoned")
    await db.set_task_meta("reopened", "work_outcome", "abandoned")
    await db.save_task_completion(TaskCompletion(
        id="close", task_id="abandoned-close", outcome="fail", completed_at=now - 20 * 86400,
    ))
    async with db.immediate() as conn:
        await conn.execute(update(task_completion_records).where(
            task_completion_records.c.id == "close").values(work_outcome="abandoned"))
        await conn.execute(update(tasks).values(updated_at=now - 20 * 86400))
        early = await expired_task_branches(conn, now=now - 7 * 86400)
        due = await expired_task_branches(conn, now=now)

    assert early == {}
    assert due["aq/abandoned-meta"].startswith("task abandoned-meta abandoned since")
    assert due["aq/abandoned-meta-wip"] == due["aq/abandoned-meta"]
    assert due["aq/abandoned-close"].startswith("task abandoned-close abandoned since")
    assert "aq/reopened" not in due  # it can run again


async def test_an_archived_failure_expires_too(setup):
    from src.integration.delivery_branches import expired_task_branches

    db, _service, _source, _remote, _repo = setup
    await db.create_task(Task(
        id="gone", project_id="p", title="gone", description="", branch_name="aq/gone",
        status=TaskStatus.FAILED,
    ))
    async with db.immediate() as conn:
        await conn.execute(update(tasks).values(updated_at=time.time() - 15 * 86400))
    assert await db.archive_task("gone")
    async with db._engine.connect() as conn:
        due = await expired_task_branches(conn, now=time.time())
    assert due["aq/gone"].startswith("task gone (archived) FAILED since")


@pytest.mark.parametrize("state,operation_state,expected", [
    ("released", "cancelled", True),
    ("released", "completed", True),
    ("released", "active", False),
    ("reserved", "cancelled", False),
    ("attached", "completed", False),
    ("handoff_pending", "completed", False),
])
async def test_integration_refs_go_only_when_released_and_finished(
    setup, state, operation_state, expected
):
    from src.integration.delivery_branches import released_integration_refs

    db, _service, _source, _remote, _repo = setup
    await seed_integration_owner(db, PRECEDENT_INTEGRATION, state=state,
                                 operation_state=operation_state)
    async with db._engine.connect() as conn:
        released = await released_integration_refs(conn)
    assert (PRECEDENT_INTEGRATION in released) is expected


async def test_the_delete_primitive_refuses_anything_outside_aq(setup, tmp_path):
    from src.integration.delivery_branches import delete_branches

    _db, service, _source, remote, repo = setup
    store = await service.store(repo)
    main = git(remote, "rev-parse", "main")
    backups = tmp_path / "backups"
    for branch in ("main", "gh-pages", "fix/workflow-startup-policy", "trunk"):
        with pytest.raises(ValueError, match="refusing to delete"):
            await delete_branches(
                service.git, service.run_git, store, {branch: {"head": main, "reason": "t"}},
                default_branch="trunk", main_head=main, backup_dir=backups, repository_id="r",
            )
    assert not backups.exists()  # nothing logged, nothing bundled
    assert "main" in remote_branches(remote)


async def test_doctor_reports_a_project_it_cannot_reach(setup, tmp_path):
    from src.database.tables import repos
    from src.doctor.git_checks import run_check
    from src.doctor.models import Severity

    db, _service, _source, _remote, _repo = setup
    async with db.immediate() as conn:
        await conn.execute(update(repos).where(repos.c.id == "r").values(
            url=str(tmp_path / "gone.git")
        ))
    result = await run_check(db, "git.stale_branches", config=stale_ctx(db, tmp_path).config)
    assert result.severity == Severity.INFO
    assert result.data["errors"][0]["project_id"] == "p"


# -- validation outcomes: "tests failed" is not "could not finish validating" --
#
# See tests/test_development_validation.py for the runner and classifier.

async def _policy(service, **policy):
    # Supervise at test speed: poll every 50 ms, no grace past the slot bound.
    service.validation_poll_seconds = 0.05
    await service.configure("p", policy, reason="validation test", operator_id="local")


async def _repairs(db):
    return [t for t in await db.list_tasks(project_id="p") if t.id.startswith("development-repair-")]


async def _deferrals(service):
    return [
        r for r in await service.rows("p")
        if r["state"] == "cancelled" and (r["evidence"] or {}).get("kind") == DEFERRAL_KIND
    ]


async def test_a_slot_wait_longer_than_the_timeout_still_delivers(setup):
    db, service, _source, remote, _repo = setup
    head = await feature(setup, "one")
    # The incident's proportions at test scale: queued longer than the whole
    # budget, then a run that fits in it.
    await _policy(
        service,
        commands=["sleep 0.2"],
        timeout_seconds=1,
        slot_wait_seconds=30,
    )
    import asyncio
    from src.resources.box_lock import BoxLock

    box = BoxLock(service.data_dir.parent / "locks/test-slots", 4)
    with box.acquire(exclusive=True, timeout=1):
        pending = asyncio.create_task(service.sweep("p"))
        # Wait for a submitted runner before measuring a blocked admission.
        async def queued():
            while not await db.list_jobs(active=True):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(queued(), 10)
        await asyncio.sleep(1.5)
    result = await pending
    assert result["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", head, "main") == ""
    check = next(
        r for r in await service.rows("p") if r["reason"] == "development batch"
    )["evidence"]["checks"][0]
    assert check["outcome"] == PASSED and check["slot_wait_seconds"] >= 1.4
    assert await _repairs(db) == []


async def test_a_slot_wait_past_its_bound_defers_without_parking_or_a_repair(setup):
    db, service, _source, remote, _repo = setup
    before = git(remote, "rev-parse", "main")
    await feature(setup, "one")
    await _policy(
        service,
        commands=["true"],
        timeout_seconds=60,
        slot_wait_seconds=1,
    )
    started = time.monotonic()
    from src.resources.box_lock import BoxLock

    box = BoxLock(service.data_dir.parent / "locks/test-slots", 4)
    with box.acquire(exclusive=True, timeout=1):
        result = await service.sweep("p")
    assert time.monotonic() - started < 20
    assert result["outcome"] == "deferred"
    assert result["reason"] == "queue_timeout"
    assert git(remote, "rev-parse", "main") == before
    rows = await service.rows("p")
    assert not [r for r in rows if r["state"] == "parked"]
    assert await _repairs(db) == []
    # Deferred, not held: the next tick with a slot free delivers it.
    await _policy(service, commands=["true"])
    assert (await service.sweep("p"))["outcome"] == "delivered"


async def test_a_real_test_failure_parks_and_the_repair_names_the_failing_test(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "one")
    failing = "tests/test_development_integration.py::test_batch_publishes"
    await _policy(
        service,
        commands=[
            (
                f"echo 'FAILED {failing} - AssertionError: assert 1 == 2'; "
                "echo '1 failed, 118 passed in 209.00s'; exit 1"
            )
        ],
    )
    result = await service.sweep("p")
    assert result["outcome"] == "parked"
    assert result["evidence"]["conclusion"] == FAILED
    parked = next(r for r in await service.rows("p") if r["state"] == "parked")
    assert parked["evidence"]["failing_tests"][0]["id"] == failing
    (repair,) = await _repairs(db)
    assert failing in repair.description
    assert "AssertionError: assert 1 == 2" in repair.description
    assert parked["id"] in repair.description
    evidence = await db.get_task_meta(repair.id, "development_repair_evidence")
    assert evidence["delivery_id"] == parked["id"]
    assert evidence["failing_tests"][0]["id"] == failing
    assert evidence["checks"][0]["exit_code"] == 1


async def test_a_timeout_mid_run_defers_as_infrastructure_with_its_output(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "one")
    await _policy(service, commands=["echo collected 119 items; sleep 30"], timeout_seconds=1)
    result = await service.sweep("p")
    assert result["outcome"] == "deferred"
    assert result["reason"] == "run_timeout"
    assert not [r for r in await service.rows("p") if r["state"] == "parked"]
    assert await _repairs(db) == []
    (deferral,) = await _deferrals(service)
    assert deferral["id"] == result["deferral"]["id"]
    run = deferral["evidence"]["runs"][-1]
    assert run["reason"] == "run_timeout"
    assert run["members"] == ["one"]
    assert run["checks"][0]["exit_code"] < 0
    assert "collected 119 items" in run["checks"][0]["output"]


async def test_consecutive_infrastructure_outcomes_surface_once_not_as_repairs(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "one")
    await _policy(service, commands=["echo collected 119 items; sleep 30"], timeout_seconds=1)
    for tick in range(1, INFRA_ALERT_AFTER + 2):
        result = await service.sweep("p")
        assert result["outcome"] == "deferred"
        assert result["deferral"]["consecutive"] == tick
        doctor = await run_doctor_check(db, "integration.development_publisher_stalled")
        assert (doctor.severity is Severity.OK) == (tick < INFRA_ALERT_AFTER)
    (deferral,) = await _deferrals(service)
    assert deferral["evidence"]["consecutive"] == INFRA_ALERT_AFTER + 1
    assert await _repairs(db) == []
    stall = doctor.data["stalls"][0]
    assert stall["cause"] == "validation_infrastructure"
    assert stall["task_ids"] == ["one"]
    assert "run_timeout" in doctor.detail
    async with db._engine.connect() as conn:
        sent = (await conn.execute(
            select(messages).where(messages.c.to_id == "supervisor-p")
        )).mappings().all()
    assert len(sent) == 1, "one supervisor message per streak, not one per tick"
    assert "run_timeout" in sent[0]["body"] and "one" in sent[0]["body"]

    # A run that reaches a real conclusion ends the streak and clears doctor.
    await _policy(service, commands=["true"])
    assert (await service.sweep("p"))["outcome"] == "delivered"
    (deferral,) = await _deferrals(service)
    assert deferral["evidence"]["open"] is False
    assert deferral["evidence"]["closed_by"] == PASSED
    doctor = await run_doctor_check(db, "integration.development_publisher_stalled")
    assert doctor.severity is Severity.OK


async def test_a_batch_parked_for_a_timeout_before_this_fix_is_released_not_repaired(setup):
    db, service, _source, remote, _repo = setup
    head = await feature(setup, "one")
    base = git(remote, "rev-parse", "main")
    now = time.time()
    async with db._engine.begin() as conn:
        await conn.execute(insert(development_deliveries).values(
            id="legacy-timeout",
            project_id="p",
            repository_id="r",
            target_ref="refs/heads/main",
            expected_sha=base,
            prepared_sha=head,
            state="parked",
            manifest=[{"task_id": "one", "source_sha": head}],
            evidence={
                "kind": "local", "validation": "focused", "conclusion": "failed",
                "checks": [{"command": "x", "exit_code": 124, "output": "validation timed out"}],
            },
            reason="selected validation failed",
            created_at=now,
            updated_at=now,
        ))
    await _policy(service, commands=["true"])
    assert (await service.sweep("p"))["outcome"] == "delivered"
    legacy = next(r for r in await service.rows("p") if r["id"] == "legacy-timeout")
    assert legacy["state"] == "cancelled"
    assert legacy["evidence"]["released"]["conclusion"] == INFRASTRUCTURE
    assert await _repairs(db) == []


async def test_publisher_queue_replay_uses_one_snapshot_job_and_immutable_result(setup):
    db, service, _source, _remote, repo = setup
    await feature(setup, "queued-source")
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    job, = await db.list_jobs(project_id="p")
    assert job["owner_kind"] == "integration" and job["task_id"] is None
    assert job["integration_operation_id"] == job["owner_id"]
    assert job["priority_band"] == 0 and job["input_mode"] == "snapshot"
    assert job["result"]["input_ref"] == git(_remote, "rev-parse", "main")
    assert job["result"]["input_stability"] == "stable"
    snapshot = Path(job["contract"]["cwd"])
    assert snapshot != await service.store(repo)
    assert git(snapshot, "rev-parse", "HEAD") == job["input_ref"]
    assert not (await db.get_workspace(job["workspace_id"])).enabled
    request = dict(
        project_id="p", operation_id=job["owner_id"], store=str(await service.store(repo)),
        input_ref=job["input_ref"], preset="lint", argv=["test -f base.txt"],
        idempotency_key=job["idempotency_key"], queue_seconds=600, run_seconds=300,
    )
    # A lost response followed by publisher restart attaches to the same row,
    # even when the completed snapshot has generated untracked output.
    (snapshot / "test-output").write_text("generated")
    from src.jobs.adapters import PublisherJobs
    restarted = PublisherJobs(service.job_client.handler)
    replay = await restarted.submit(**request)
    assert replay["id"] == job["id"] and replay["result"] == job["result"]
    assert len(await db.list_jobs(project_id="p")) == 1
    from src.jobs.policy import JobError
    with pytest.raises(JobError, match="jobs.idempotency_conflict"):
        await restarted.submit(**{**request, "run_seconds": 301})


async def test_publisher_caller_cancellation_preserves_execution_and_pin(setup):
    import asyncio
    db, service, _source, _remote, repo = setup
    store = await service.store(repo)
    head = git(store, "rev-parse", "HEAD")
    job = await service.job_client.submit(
        project_id="p", operation_id="operation", store=str(store), input_ref=head,
        preset="lint", argv=["sleep 30"], idempotency_key="caller-cancel",
        queue_seconds=30, run_seconds=60,
    )
    waiter = asyncio.create_task(service.job_client.wait(job, poll_seconds=0.02))
    async def launched():
        while not (Path(service.data_dir.parent) / "runs" / job["id"] / "started.json").exists():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(launched(), 20)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert await db.workspace_has_job_pin(job["workspace_id"])
    assert (await db.get_job(job["id"]))["state"] not in {"cancelled", "failed", "lost"}
    await service.job_client.handler._cmd_job_cancel({"job_id": job["id"]})
    finished = await asyncio.wait_for(service.job_client.wait(job, poll_seconds=0.02), 20)
    assert finished["state"] == "cancelled"
    assert not await db.workspace_has_job_pin(job["workspace_id"])


async def test_snapshot_source_changes_before_execution_cannot_attest(setup):
    db, service, _source, _remote, repo = setup
    store = await service.store(repo)
    job = await service.job_client.submit(
        project_id="p", operation_id="operation", store=str(store),
        input_ref=git(store, "rev-parse", "HEAD"), preset="lint", argv=["true"],
        idempotency_key="changed-snapshot", queue_seconds=30, run_seconds=60,
    )
    (Path(job["contract"]["cwd"]) / "base.txt").write_text("changed source")
    finished = await service.job_client.wait(job, poll_seconds=0.02)
    assert finished["result"]["outcome"] == "infrastructure"
    assert finished["result"]["infra_reason"] == "snapshot_modified"
    assert not await db.workspace_has_job_pin(job["workspace_id"])


async def test_publisher_does_not_replace_a_job_with_uncertain_cleanup(setup):
    db, service, _source, _remote, repo = setup
    store = await service.store(repo)
    request = dict(
        project_id="p", operation_id="quarantined-operation", store=str(store),
        input_ref=git(store, "rev-parse", "HEAD"), preset="lint", argv=["true"],
        idempotency_key="first-attempt", queue_seconds=30, run_seconds=60,
    )
    job = await service.job_client.submit(**request)
    job = await db.transition_job(job["id"], 0, "starting", launch_at=time.time() - 31)
    await service.job_client.handler._jobs().reconcile(job)
    lost = await db.get_job(job["id"])
    assert lost["state"] == "lost" and lost["cleanup_blocked"]
    from src.jobs.policy import JobError
    with pytest.raises(JobError, match="jobs.cleanup_blocked"):
        await service.job_client.submit(**{**request, "idempotency_key": "retry-attempt"})
    assert len(await db.list_jobs(project_id="p")) == 1
    assert await db.workspace_has_job_pin(job["workspace_id"])


async def test_result_retention_removes_only_verified_terminal_snapshot(setup, monkeypatch):
    from src.database.tables import jobs
    db, service, _source, _remote, repo = setup
    store = await service.store(repo)
    job = await service.job_client.submit(
        project_id="p", operation_id="retention", store=str(store),
        input_ref=git(store, "rev-parse", "HEAD"), preset="lint", argv=["true"],
        idempotency_key="retention", queue_seconds=30, run_seconds=60,
    )
    await service.job_client.wait(job, poll_seconds=0.02)
    snapshot = Path(job["contract"]["cwd"])
    async with db._engine.begin() as conn:
        await conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(
            ended_at=time.time() - 91 * 86400,
        ))
    import shutil
    original = shutil.rmtree

    def refuse_snapshot(path, *args, **kwargs):
        if Path(path) == snapshot:
            raise OSError("snapshot cleanup unavailable")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", refuse_snapshot)
        with pytest.raises(OSError, match="snapshot cleanup unavailable"):
            await service.job_client.handler._jobs().sweep()
    assert await db.get_job(job["id"]) is not None and snapshot.exists()
    await service.job_client.handler._jobs().sweep()
    assert await db.get_job(job["id"]) is None
    assert await db.get_workspace(job["workspace_id"]) is None
    assert not snapshot.exists() and store.exists()


async def truth_snapshot(setup, *, legacy_rows=(), target_ref="refs/heads/main"):
    from src.integration.delivery_truth import delivery_snapshot

    _db, service, _source, _remote, repo = setup
    store = await service.store(repo, fetch=False)
    return await delivery_snapshot(
        service.git, store, project_id="p", repository_id=repo.id,
        repository_url=repo.url, target_ref=target_ref, legacy_rows=legacy_rows,
    )


@pytest.mark.parametrize("receipt_state", [None, "delivered", "adopted"])
async def test_eleven_contained_candidates_ignore_missing_ancestor_and_receipts(setup, receipt_state):
    """The eleven-way incident: inspect own git truth before ancestor gates."""
    db, service, source, remote, _repo = setup
    await db.create_task(Task(
        id="missing-ancestor", project_id="p", title="missing ancestor", description="",
        status=TaskStatus.COMPLETED, branch_name="aq/missing-ancestor",
    ))
    heads = {}
    for index in range(11):
        task_id = f"contained-{index}"
        heads[task_id] = await feature(setup, task_id)
        await db.save_task_completion(TaskCompletion(
            id=f"close-{index}", task_id=task_id, outcome="pass",
            commits=[heads[task_id]], completed_at=time.time(),
        ))
        await db.add_dependency(task_id, "missing-ancestor")
        await db.set_task_meta(task_id, PUBLISHER_SKIP_KEY, {
            "reason": "missing_ref", "dependency_id": "missing-ancestor",
        })
    git(source, "checkout", "main")
    for task_id in heads:
        git(source, "merge", "--no-edit", task_id)
    git(source, "push", "origin", "main")
    for task_id in heads:
        git(source, "push", "origin", "--delete", task_id)
    if receipt_state:
        now = time.time()
        await service.save({
            "id": "misleading-receipt", "project_id": "p", "repository_id": "r",
            "target_ref": "refs/heads/main", "expected_sha": None, "prepared_sha": None,
            "state": receipt_state, "manifest": [
                {"task_id": task_id, "source_sha": head} for task_id, head in heads.items()
            ], "evidence": {}, "reason": "test receipt state is not authority",
            "created_at": now, "updated_at": now,
        })
    before = git(remote, "rev-parse", "main")
    result = await service.sweep("p")
    assert result["outcome"] in {"idle", "delivered"}
    assert git(remote, "rev-parse", "main") == before
    assert (await db.get_task_meta("missing-ancestor", PUBLISHER_SKIP_KEY))["reason"] == "missing_ref"
    for task_id in heads:
        assert await db.get_task_meta(task_id, PUBLISHER_SKIP_KEY) is None
    assert await db.get_task_meta("missing-ancestor", EMPTY_SOURCE_KEY) is None


@pytest.mark.parametrize("receipt_state", ["delivered", "adopted"])
async def test_receipt_cannot_exclude_uncontained_candidate(setup, receipt_state):
    _db, service, _source, remote, _repo = setup
    head = await feature(setup, "not-delivered")
    now = time.time()
    await service.save({
        "id": "false-delivery", "project_id": "p", "repository_id": "r",
        "target_ref": "refs/heads/main", "expected_sha": None, "prepared_sha": head,
        "state": receipt_state, "manifest": [{"task_id": "not-delivered", "source_sha": head}],
        "evidence": {}, "reason": "misleading state", "created_at": now, "updated_at": now,
    })
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert git(remote, "merge-base", "--is-ancestor", head, "main") == ""


async def test_snapshot_scopes_generation_and_fences_target_movement(setup):
    from dataclasses import replace

    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, _service, source, _remote, _repo = setup
    old_head = await feature(setup, "reopened")
    await db.save_task_completion(TaskCompletion(
        id="old-close", task_id="reopened", outcome="pass", commits=[old_head],
        completed_at=time.time(),
    ))
    git(source, "push", "origin", f"{old_head}:main")
    old_requests = await load_delivery_requests(
        db, ["reopened"], repository_id="r", target_ref="refs/heads/main",
    )
    snapshot = await truth_snapshot(setup)
    request = old_requests["reopened"]
    assert (await snapshot.evaluate(request)).state is DeliveryState.CONTAINED
    assert snapshot.matches([request], graph_inputs=("edge",), current_graph_inputs=("edge",))
    assert not snapshot.matches([request], graph_inputs=("edge",), current_graph_inputs=())
    assert await snapshot.is_fresh()
    assert not await snapshot.is_fresh(repository_url="wrong-repository")
    assert not await snapshot.is_fresh(target_ref="refs/heads/elsewhere")
    for wrong in (replace(request, project_id="other"), replace(request, repository_id="other"),
                  replace(request, target_ref="refs/heads/elsewhere")):
        evidence = await snapshot.evaluate(wrong)
        assert evidence.state is DeliveryState.UNKNOWN and evidence.reason == "scope_mismatch"
    git(source, "checkout", "reopened")
    (source / "new-generation.txt").write_text("new generation\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "new completion")
    newer = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "reopened")
    await db.save_task_completion(TaskCompletion(
        id="new-close", task_id="reopened", outcome="pass", commits=[newer],
        completed_at=time.time(),
    ))
    current = await load_delivery_requests(
        db, ["reopened"], repository_id="r", target_ref="refs/heads/main",
    )
    fresh = await truth_snapshot(setup)
    evidence = await fresh.evaluate(current["reopened"])
    assert evidence.state is DeliveryState.PENDING and evidence.source_oid == newer
    assert not snapshot.matches(current.values())
    # The old snapshot never changes its pinned answer when main advances.
    git(source, "push", "origin", f"{newer}:main")
    assert not await snapshot.is_fresh()
    assert snapshot.target_oid == old_head
    latest = await truth_snapshot(setup)
    assert (await latest.evaluate(current["reopened"])).state is DeliveryState.CONTAINED


async def test_snapshot_archived_completion_survives_ref_cleanup(setup):
    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, service, source, _remote, _repo = setup
    head = await feature(setup, "archived-source")
    await db.save_task_completion(TaskCompletion(
        id="archived-close", task_id="archived-source", outcome="pass", commits=[head],
        completed_at=time.time(),
    ))
    git(source, "push", "origin", f"{head}:main")
    git(source, "push", "origin", "--delete", "archived-source")
    # Archive still uses the legacy readiness projection until consumers
    # migrate. Retire that journal after archiving; git is sufficient here.
    await service.sweep("p")
    await db.archive_task("archived-source")
    async with db._engine.begin() as conn:
        await conn.execute(delete(development_deliveries))
    requests = await load_delivery_requests(
        db, ["archived-source", "missing-task"], repository_id="r", target_ref="refs/heads/main",
    )
    assert "missing-task" not in requests
    request = requests["archived-source"]
    assert request.archived
    evidence = await (await truth_snapshot(setup)).evaluate(request)
    assert evidence.state is DeliveryState.CONTAINED
    assert evidence.source_oid == head and evidence.request.completion_id == "archived-close"


@pytest.mark.parametrize("failure", ["fetch", "ancestry", "invalid", "missing", "abbreviated"])
async def test_snapshot_git_errors_and_unresolved_sources_are_unknown(setup, failure):
    from dataclasses import replace

    from src.git.manager import GitError
    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, service, source, _remote, _repo = setup
    head = await feature(setup, "probe")
    await db.save_task_completion(TaskCompletion(
        id="probe-close", task_id="probe", outcome="pass", commits=[head],
        completed_at=time.time(),
    ))
    git(source, "push", "origin", f"{head}:main")
    requests = await load_delivery_requests(
        db, ["probe"], repository_id="r", target_ref="refs/heads/main",
    )
    request = requests["probe"]
    if failure == "fetch":
        with patch.object(service.git, "afetch_origin", side_effect=GitError("offline")):
            snapshot = await truth_snapshot(setup)
        assert snapshot.error and not await snapshot.is_fresh()
        evidence = await snapshot.evaluate(request)
    elif failure == "ancestry":
        snapshot = await truth_snapshot(setup)
        with patch.object(service.git, "ais_ancestor", return_value=None):
            evidence = await snapshot.evaluate(request)
    else:
        source = {"invalid": "HEAD~1", "missing": "a" * 40, "abbreviated": "abcdef123"}[failure]
        evidence = await (await truth_snapshot(setup)).evaluate(replace(request, reported_source=source))
    assert evidence.state is DeliveryState.UNKNOWN and not evidence.satisfied


async def test_legacy_locator_is_generation_repo_target_scoped_and_requires_git(setup):
    from dataclasses import replace

    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, _service, source, _remote, _repo = setup
    head = await feature(setup, "legacy")
    await db.save_task_completion(TaskCompletion(
        id="legacy-close", task_id="legacy", outcome="pass", commits=[], completed_at=time.time(),
    ))
    git(source, "push", "origin", f"{head}:main")
    git(source, "push", "origin", "--delete", "legacy")
    requests = await load_delivery_requests(
        db, ["legacy"], repository_id="r", target_ref="refs/heads/main",
    )
    request = requests["legacy"]
    row = {
        "project_id": "p", "repository_id": "r", "target_ref": "refs/heads/main",
        "state": "parked", "created_at": time.time(), "manifest": [],
        "evidence": {"completion_sources": [
            {"task_id": "legacy", "completion_id": "legacy-close", "source_sha": head}
        ]},
    }
    snapshot = await truth_snapshot(setup, legacy_rows=[row])
    assert (await snapshot.evaluate(request)).state is DeliveryState.CONTAINED
    assert snapshot.legacy_inventory == (("legacy", "legacy-close", head, "legacy_completion_source"),)
    for wrong in ({**row, "repository_id": "other"}, {**row, "target_ref": "refs/heads/other"}):
        other = await truth_snapshot(setup, legacy_rows=[wrong])
        assert (await other.evaluate(request)).state is DeliveryState.UNKNOWN
    reopened = replace(request, completion_id="new-close", completed_at=time.time())
    assert (await snapshot.evaluate(reopened)).state is DeliveryState.UNKNOWN


async def test_snapshot_distinguishes_organization_from_branchless_code(setup):
    from dataclasses import replace

    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, _service, _source, _remote, _repo = setup
    await db.create_task(Task(
        id="organization", project_id="p", title="organization", description="",
        status=TaskStatus.COMPLETED,
    ))
    requests = await load_delivery_requests(
        db, ["organization"], repository_id="r", target_ref="refs/heads/main",
    )
    snapshot = await truth_snapshot(setup)
    request = requests["organization"]
    assert (await snapshot.evaluate(request)).state is DeliveryState.NO_ARTIFACT
    code = replace(request, task_id="code", has_recorded_source=True)
    assert (await snapshot.evaluate(code)).state is DeliveryState.UNKNOWN
    missing = replace(request, task_id="missing", branch_name="aq/deleted")
    assert (await snapshot.evaluate(missing)).state is DeliveryState.UNKNOWN


async def test_legacy_repair_bridge_checks_exact_current_replacement_and_git(setup):
    from dataclasses import replace

    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, service, _source, remote, _repo = setup
    older, newer, older_sha, _newer_sha = await repair_cycle(setup, source_contract=True)
    await _park(service, "original-park", [{"task_id": older, "source_sha": older_sha}])
    assert (await service.sweep("p"))["outcome"] == "delivered"
    assert subprocess.run(
        ["git", "-C", str(remote), "merge-base", "--is-ancestor", older_sha, "main"]
    ).returncode == 1
    requests = await load_delivery_requests(
        db, [older, newer], repository_id="r", target_ref="refs/heads/main",
    )
    rows = await service.rows("p")
    snapshot = await truth_snapshot(setup, legacy_rows=rows)
    results = await snapshot.evaluate_many(requests.values())
    assert results[older].state is DeliveryState.CONTAINED
    assert results[older].reason == "legacy_repair_replacement"
    # A reopened replacement cannot release the original using its old proof.
    current = {**requests, newer: replace(requests[newer], completion_id="new-generation")}
    results = await snapshot.evaluate_many(current.values())
    assert results[older].state is DeliveryState.PENDING
    # Reopening the replacement before its next close also invalidates proof.
    current = {**requests, newer: replace(requests[newer], task_status="READY")}
    results = await (await truth_snapshot(setup, legacy_rows=rows)).evaluate_many(current.values())
    assert results[older].state is DeliveryState.PENDING
    # An original's newer completion is not covered by its earlier repair map.
    current = {**requests, older: replace(
        requests[older], completion_id="new-original", completed_at=time.time()
    )}
    results = await (await truth_snapshot(setup, legacy_rows=rows)).evaluate_many(current.values())
    assert results[older].state is DeliveryState.PENDING
    # An adopted state without exact replacement proof never suffices.
    without_proof = [{**row, "evidence": {}} for row in rows]
    results = await (await truth_snapshot(setup, legacy_rows=without_proof)).evaluate_many(
        requests.values()
    )
    assert results[older].state is DeliveryState.PENDING


async def test_operator_adoption_bridge_is_exact_generation_fenced_and_requires_git(setup):
    """An operator adoption of content that landed rebased is delivered, and nothing more."""
    from dataclasses import replace

    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, service, source, remote, _repo = setup
    head = await feature(setup, "rebased")
    await db.save_task_completion(TaskCompletion(
        id="rebased-close", task_id="rebased", outcome="pass", commits=[head],
        completed_at=time.time(),
    ))
    # The same content lands on main as a different commit.
    git(source, "checkout", "main")
    (source / "rebased.txt").write_text("new\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "rebased content")
    git(source, "push", "origin", "main")
    main = git(remote, "rev-parse", "main")
    requests = await load_delivery_requests(
        db, ["rebased"], repository_id="r", target_ref="refs/heads/main",
    )
    request = requests["rebased"]
    assert (await (await truth_snapshot(setup)).evaluate(request)).state is DeliveryState.PENDING
    await service.adopt(
        project_id="p", task_ids=["rebased"], target_ref="refs/heads/main", head_sha=main,
        reason="content landed rebased", operator_id="supervisor", accept_equivalent=True,
    )
    rows = await service.rows("p")
    evidence = await (await truth_snapshot(setup, legacy_rows=rows)).evaluate(request)
    assert evidence.state is DeliveryState.CONTAINED
    assert evidence.reason == "legacy_operator_adoption" and evidence.source_oid == head
    # A bare adopted state, another target, a head git no longer contains, a
    # later generation, or a proof bound to another generation never suffices.
    adopted = rows[-1]
    for wrong in (
        {**adopted, "evidence": {}},
        {**adopted, "target_ref": "refs/heads/elsewhere"},
        {**adopted, "prepared_sha": head},
        {**adopted, "manifest": [{**adopted["manifest"][0], "source_sha": main}]},
        {**adopted, "evidence": {**adopted["evidence"], "completion_sources": [
            {"task_id": "rebased", "completion_id": "other-close", "source_sha": head}
        ]}},
    ):
        snapshot = await truth_snapshot(setup, legacy_rows=[wrong])
        assert (await snapshot.evaluate(request)).state is DeliveryState.PENDING
    reopened = replace(request, completion_id="new-close", completed_at=time.time())
    snapshot = await truth_snapshot(setup, legacy_rows=rows)
    assert (await snapshot.evaluate(reopened)).state is DeliveryState.PENDING


@pytest.mark.parametrize("adoption", ["operator", "bare_receipt"])
async def test_operator_adopted_repair_cycle_is_delivered_not_held(setup, adoption):
    """Production shape: three chained repairs adopted after their content landed rebased.

    Each repair names its predecessor, and each predecessor waits on its
    repair, so the newest contract cannot supersede the whole component. Before
    the adoption bridge every tick held all three as a dependency cycle.
    """
    db, service, source, remote, _repo = setup
    chain = ["development-repair-a", "development-repair-b", "development-repair-c"]
    heads = {}
    for task_id in chain:
        heads[task_id] = await feature(setup, task_id)
        git(source, "push", "origin", f"{task_id}:aq/{task_id}")
        await db.update_task(task_id, branch_name=f"aq/{task_id}")
        # Legacy closes: two report nothing, as the adopted production repairs did.
        await db.save_task_completion(TaskCompletion(
            id=f"{task_id}-close", task_id=task_id, outcome="pass",
            commits=[heads[task_id]] if task_id.endswith("b") else [], completed_at=time.time(),
        ))
    for older, newer in itertools.pairwise(chain):
        await db.set_task_meta(newer, "development_repair_sources",
                               [{"task_id": older, "source_sha": heads[older]}])
        async with db.immediate() as conn:
            await conn.execute(insert(task_dependencies).values(
                task_id=older, depends_on_task_id=newer, dep_type="blocks",
            ))
    git(source, "checkout", "main")
    for task_id in chain:
        (source / f"{task_id}.txt").write_text("new\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "chain content, rebased")
    git(source, "push", "origin", "main")
    main = git(remote, "rev-parse", "main")
    if adoption == "operator":
        for task_id in chain:
            await service.adopt(
                project_id="p", task_ids=[task_id], target_ref="refs/heads/main", head_sha=main,
                reason="content landed rebased", operator_id="supervisor", accept_equivalent=True,
            )
    else:
        now = time.time()
        for task_id in chain:
            await service.save({
                "id": f"bare-{task_id}", "project_id": "p", "repository_id": "r",
                "target_ref": "refs/heads/main", "expected_sha": main, "prepared_sha": main,
                "state": "adopted", "manifest": [{"task_id": task_id, "source_sha": heads[task_id]}],
                "evidence": {}, "reason": "receipt state is not authority",
                "created_at": now, "updated_at": now,
            })
    other = await feature(setup, "independent")
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert {m["task_id"] for m in result["manifest"]} == {"independent"}
    assert git(remote, "merge-base", "--is-ancestor", other, "main") == ""
    skips = {task_id: await db.get_task_meta(task_id, PUBLISHER_SKIP_KEY) for task_id in chain}
    if adoption == "operator":
        assert skips == dict.fromkeys(chain)
        assert (await service.sweep("p"))["outcome"] == "idle"
    else:
        assert {skip["reason"] for skip in skips.values()} == {"dependency_cycle"}


# -- generated artifacts: regenerated at merge, never a conflict -------------

#: A miniature scripts/regenerate-generated.sh: the inventory and catalogue are
#: pure functions of commands/ and tests/, with a count line that every added
#: command changes on both sides of a merge.
GENERATOR = """#!/bin/sh
set -e
render() {
    printf 'count: %s\\n#\\n#\\n#\\n' "$(ls "$1" | wc -l | tr -d ' ')"
    ls "$1" | LC_ALL=C sort
}
if [ "$1" = "--check" ]; then
    render commands | cmp -s - inventory.txt && render tests | cmp -s - catalogue.txt
    exit $?
fi
render commands > inventory.txt
render tests > catalogue.txt
"""
GENERATED_ATTRIBUTES = (
    "inventory.txt merge=aq-generated\n"
    "catalogue.txt merge=aq-generated\n"
    "areas.txt merge=union\n"
)
CHECK_GENERATED = "./regenerate.sh --check"


def _rendered(*names):
    return f"count: {len(names)}\n#\n#\n#\n" + "".join(f"{n}\n" for n in sorted(names))


def _regenerate(source):
    subprocess.run(["sh", "regenerate.sh"], cwd=source, check=True)


def _publish_generator(source):
    git(source, "checkout", "main")
    (source / "regenerate.sh").write_text(GENERATOR)
    (source / "regenerate.sh").chmod(0o755)
    (source / ".gitattributes").write_text(GENERATED_ATTRIBUTES)
    for directory, name in (("commands", "base"), ("tests", "test_base.py")):
        (source / directory).mkdir()
        (source / directory / name).write_text("")
    (source / "areas.txt").write_text("area:\n  - tests/test_base.py\n")
    _regenerate(source)
    git(source, "add", "-A")
    git(source, "commit", "-m", "generated artifacts")
    git(source, "push", "origin", "main")


async def command_feature(setup, task_id, *, source_edit=None):
    """A branch adding one command and its test, regenerated like a worker would."""
    db, _service, source, _remote, _repo = setup
    git(source, "checkout", "-B", task_id, "main")
    (source / "commands" / task_id).write_text("")
    (source / "tests" / f"test_{task_id}.py").write_text("")
    with (source / "areas.txt").open("a") as areas:
        areas.write(f"  - tests/test_{task_id}.py\n")
    if source_edit:
        (source / "base.txt").write_text(source_edit)
    _regenerate(source)
    git(source, "add", "-A")
    git(source, "commit", "-m", task_id)
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", task_id)
    await db.create_task(Task(
        id=task_id, project_id="p", repo_id="r", title=task_id, description="",
        branch_name=task_id, status=TaskStatus.COMPLETED,
    ))
    return head


def _batch_evidence(rows):
    return next(r for r in rows if r["reason"] == "development batch")["evidence"]


async def test_generated_only_conflict_parks_without_a_regenerate_policy(setup):
    _db, service, source, _remote, _repo = setup
    _publish_generator(source)
    await command_feature(setup, "one")
    await command_feature(setup, "two")
    await _policy(service, commands=[CHECK_GENERATED])
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    assert len(result["manifest"]) == 1
    parked = next(r for r in await service.rows("p") if r["state"] == "parked")
    assert sorted(parked["evidence"]["conflicting_files"]) == ["catalogue.txt", "inventory.txt"]
    assert "regenerate" not in parked["evidence"]


async def test_branches_that_both_add_commands_merge_with_regenerated_artifacts(setup):
    """Two branches each regenerate the inventory and catalogue for their own command.

    Both generated files conflict, and only in generated files; the union
    attribute merges the hand-maintained area list.  The publisher rebuilds the
    generated files from the merged sources instead of parking the second branch.
    """
    _db, service, source, remote, _repo = setup
    _publish_generator(source)
    one = await command_feature(setup, "one")
    two = await command_feature(setup, "two")
    await _policy(service, commands=[CHECK_GENERATED], regenerate="./regenerate.sh")
    result = await service.sweep("p")
    assert result["outcome"] == "delivered", result
    assert {m["task_id"] for m in result["manifest"]} == {"one", "two"}
    for head in (one, two):
        assert git(remote, "merge-base", "--is-ancestor", head, "main") == ""
    assert git(remote, "show", "main:inventory.txt") + "\n" == _rendered("base", "one", "two")
    assert git(remote, "show", "main:catalogue.txt") + "\n" == _rendered(
        "test_base.py", "test_one.py", "test_two.py"
    )
    areas = git(remote, "show", "main:areas.txt")
    assert "tests/test_one.py" in areas and "tests/test_two.py" in areas
    rows = await service.rows("p")
    assert not [r for r in rows if r["state"] == "parked"]
    regenerated = _batch_evidence(rows)["regenerated"]
    assert list(regenerated.values()) == [["catalogue.txt", "inventory.txt"]]
    # The rebuilt files are folded into the member's merge commit.
    merge = git(remote, "log", "-1", "--format=%P", "main").split()
    assert len(merge) == 2


async def test_a_clean_but_stale_generated_merge_is_regenerated(setup):
    """Both sides changed the count identically, so the text merge is clean and wrong."""
    _db, service, source, remote, _repo = setup
    _publish_generator(source)
    await command_feature(setup, "aaa")
    await command_feature(setup, "zzz")
    await _policy(service, commands=[CHECK_GENERATED], regenerate="./regenerate.sh")
    result = await service.sweep("p")
    assert result["outcome"] == "delivered", result
    assert git(remote, "show", "main:inventory.txt") + "\n" == _rendered("aaa", "base", "zzz")


async def test_a_source_conflict_still_parks_naming_only_the_source(setup):
    db, service, source, remote, _repo = setup
    _publish_generator(source)
    await command_feature(setup, "one", source_edit="one\n")
    await command_feature(setup, "two", source_edit="two\n")
    await _policy(service, commands=[CHECK_GENERATED], regenerate="./regenerate.sh")
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    (delivered,) = result["manifest"]
    parked = next(r for r in await service.rows("p") if r["state"] == "parked")
    assert parked["evidence"]["conflicting_files"] == ["base.txt"]
    assert parked["evidence"]["regenerate"] == "./regenerate.sh"
    assert "regeneration" not in parked["evidence"]
    assert git(remote, "show", "main:inventory.txt") + "\n" == _rendered(
        "base", delivered["task_id"]
    )
    (repair,) = await _repairs(db)
    assert "never hand-merge them" in repair.description
    assert "run `./regenerate.sh`" in repair.description
    assert "- base.txt" in repair.description

    # Recovery retries the source conflict under the regeneration policy and
    # must refresh its existing park, preserving the repair and rebuild advice.
    [member] = parked["manifest"]
    with pytest.raises(ValueError, match=f"carried by repair {repair.id}"):
        await service.recover_child("p", member["task_id"])
    [reparked] = await _parked(service)
    assert reparked["id"] == parked["id"]
    assert "reconflicted_at" in reparked["evidence"]
    assert reparked["evidence"]["conflicting_files"] == ["base.txt"]
    assert reparked["evidence"]["regenerate"] == "./regenerate.sh"
    assert [task.id for task in await _repairs(db)] == [repair.id]


async def test_a_failed_regeneration_parks_and_restores_the_candidate(setup):
    db, service, source, remote, _repo = setup
    _publish_generator(source)
    await command_feature(setup, "one")
    await command_feature(setup, "two")
    await feature(setup, "three")
    await _policy(
        service, commands=["test -f three.txt"], regenerate="sh -c 'echo boom; exit 3'"
    )
    result = await service.sweep("p")
    assert result["outcome"] == "delivered", result
    delivered = {m["task_id"] for m in result["manifest"]}
    assert "three" in delivered and len(delivered) == 2
    parked = next(r for r in await service.rows("p") if r["state"] == "parked")
    regeneration = parked["evidence"]["regeneration"]
    assert regeneration["exit_code"] == 3
    assert regeneration["detail"] == "`sh -c 'echo boom; exit 3'` exited 3"
    assert "boom" in parked["evidence"]["detail"]
    assert parked["evidence"]["conflicting_files"] == []
    (repair,) = await _repairs(db)
    assert "Regenerating the generated files with `sh -c 'echo boom; exit 3'` failed" in (
        repair.description
    )
    assert "boom" in repair.description
    evidence = await db.get_task_meta(repair.id, "development_repair_evidence")
    assert evidence["regeneration"]["exit_code"] == 3
    # Nothing the failed merge wrote reached the published tree.
    assert git(remote, "show", "main:three.txt") == "new"


async def test_a_regeneration_that_writes_a_source_file_is_refused(setup):
    _db, service, source, remote, _repo = setup
    _publish_generator(source)
    await command_feature(setup, "one")
    await command_feature(setup, "two")
    await _policy(
        service, commands=[CHECK_GENERATED],
        regenerate="sh -c './regenerate.sh && echo rewritten > base.txt'",
    )
    result = await service.sweep("p")
    assert result["outcome"] == "delivered"
    parked = next(r for r in await service.rows("p") if r["state"] == "parked")
    assert parked["evidence"]["regeneration"]["detail"] == (
        "the regeneration changed files that are not generated: base.txt"
    )
    assert git(remote, "show", "main:base.txt") == "base"


@pytest.mark.parametrize("command", ["", "  ", "'unterminated"])
def test_regenerate_policy_must_be_a_command_line(command):
    with pytest.raises(ValueError, match="regenerate"):
        DevelopmentPolicy(commands=["true"], regenerate=command).checked()


def test_development_prime_names_the_regenerate_command_only_when_configured():
    from src.prime.sections import build_completion_protocol_section

    plain = build_completion_protocol_section("example", development=True).body
    assert "hand-merge" not in plain
    body = build_completion_protocol_section(
        "example", development=True, regenerate="scripts/regenerate-generated.sh"
    ).body
    assert "Never hand-merge generated files" in body
    assert "run `scripts/regenerate-generated.sh`" in body
    assert "merge=aq-generated" in body
