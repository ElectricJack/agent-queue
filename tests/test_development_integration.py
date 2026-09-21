"""Real Git + private PostgreSQL coverage for the development delivery path."""

import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.queries.blocked_state import _development_delivery_pending
from src.database.tables import gates, projects, task_gates, tasks
from src.event_bus import EventBus
from src.integration.development import DevelopmentBusy, DevelopmentIntegration, DevelopmentPolicy
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskCompletion, TaskStatus
from tests.db_fixtures import lease_dsn


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture
async def setup(tmp_path):
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
    service = DevelopmentIntegration(db, data_dir=tmp_path / "data")
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
        other = DevelopmentIntegration(db, data_dir=service.data_dir)
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


async def test_archived_repair_source_does_not_wedge_the_publisher(setup, monkeypatch):
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
    async def _no_hold(ids, project_id, *, conn):
        return None

    monkeypatch.setattr(db, "_development_integration_hold", _no_hold)
    assert await db.archive_task(previous)
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
    assert await db.archive_task("original")
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
    assert await db.archive_task(identity)
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
    assert holds["aq/discarding"] == "branch discard is pending"
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
