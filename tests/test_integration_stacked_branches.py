"""Stacked source admission and refresh over real Git and disposable PostgreSQL."""

import subprocess

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import task_branch_origins, task_integration_checkpoints
from src.git.github_contracts import GitHubRepositoryBinding
from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
from src.integration.stacked_branches import StackedBranches, observe_stacks, stacked_policy
from src.integration.train import TrainTarget
from src.integration.train_sources import DatabaseBatches, _never_trusted
from src.models import Task, TaskCompletion, TaskStatus
from tests.test_delivery_consumers import close, git
from tests.test_integration_gitops import LocalGit
from tests.test_integration_train_sources import completed, snapshot, world  # noqa: F401


async def generation(db, tid):
    async with db._engine.connect() as conn:
        return await conn.scalar(
            select(task_integration_checkpoints.c.generation).where(
                task_integration_checkpoints.c.task_id == tid
            )
        )


async def checkpoint(db, tid, head):
    row = pg_insert(task_integration_checkpoints).values(
        task_id=tid,
        repository_id="r",
        branch=f"aq/{tid}",
        checkpoint_sha=head,
        updated_at=1,
    )
    async with db.immediate() as conn:
        await conn.execute(
            row.on_conflict_do_update(
                index_elements=["task_id"],
                set_={
                    "checkpoint_sha": head,
                    "version": task_integration_checkpoints.c.version + 1,
                },
            )
        )


@pytest.fixture
async def stack(world):  # noqa: F811 - imported pytest fixture
    db, origin = world.db, world.origin
    await db.update_project(
        "p", hierarchical_integration_policy={"prerequisite_branches": "stacked"}
    )
    base = git(origin.clone, "rev-parse", "main")
    git(origin.clone, "push", "origin", "main:aq/epic")
    await db.create_task(
        Task(
            id="epic",
            project_id="p",
            repo_id="r",
            title="epic",
            description="",
            branch_name="aq/epic",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await checkpoint(db, "epic", base)
    first = await completed(world, "first", parent="epic")
    await checkpoint(db, "first", first)
    git(origin.clone, "checkout", "-b", "aq/child", first)
    (origin.clone / "child.txt").write_text("preserved child\n")
    git(origin.clone, "add", ".")
    git(origin.clone, "commit", "-qm", "child")
    child = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", "origin", "aq/child")
    await completed(world, "child", parent="epic", needs=("first",), head=child)
    await checkpoint(db, "child", child)
    transport = LocalGit(origin.url)
    repo = RetainedRepository("r", origin.clone, GitHubRepositoryBinding(123, "test/repo"), "main")
    gitops = GitOperations(
        db,
        git=transport,
        repository=AsyncMock(return_value=repo),
        authority=SubjectGitAuthority(db, trusted_green=_never_trusted),
    )
    service = StackedBranches(db)
    overlay = await service.prepare("child")
    return SimpleNamespace(
        world=world,
        db=db,
        origin=origin,
        first=first,
        child=child,
        base=base,
        service=service,
        gitops=gitops,
        overlay=overlay,
    )


def test_project_policy_defaults_and_override():
    assert stacked_policy({"id": "agent-queue"})
    assert not stacked_policy({"id": "other"})
    assert not stacked_policy(
        {
            "id": "agent-queue",
            "hierarchical_integration_policy": {"prerequisite_branches": "wait-for-parent"},
        }
    )
    assert stacked_policy(
        {"id": "other", "hierarchical_integration_policy": {"prerequisite_branches": "stacked"}}
    )


async def test_single_stack_starts_at_completed_source_without_epic_delivery(stack):
    assert stack.overlay["base_sha"] == stack.first
    assert git(stack.origin.clone, "rev-parse", "origin/aq/epic") == stack.base
    assert await stack.service.current("child")
    async with stack.db._engine.connect() as conn:
        origin = (
            (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id == "child",
                    )
                )
            )
            .mappings()
            .one()
        )
    assert origin["base_sha"] == stack.first  # filing base remains immutable
    assert origin["stack_snapshot"]["prerequisites"]["first"]["checkpoint_sha"] == stack.first


async def test_multiple_sources_merge_on_epic_and_pin_exact_base(stack):
    second = await completed(stack.world, "second", parent="epic")
    await checkpoint(stack.db, "second", second)
    await stack.db.add_dependency("child", "second")
    overlay = await stack.service.prepare("child")
    store, base = overlay["stack_store"], overlay["base_sha"]
    for head in (stack.base, stack.first, second):
        git(store, "merge-base", "--is-ancestor", head, base)
    assert git(store, "show", f"{base}:first-work.txt") == "work"
    assert git(store, "show", f"{base}:second-work.txt") == "work"


async def test_changed_prerequisite_merges_preserves_child_and_rotates_completion(stack):
    next_head = stack.origin.work("first", "revision")
    await close(stack.db, "first", [next_head], close_id="first-revised", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    assert not await stack.service.current("child")
    assert await stack.service.refresh("child", stack.gitops) == "refreshed"
    head = git(stack.origin.clone, "ls-remote", "origin", "refs/heads/aq/child").split()[0]
    git(stack.origin.clone, "merge-base", "--is-ancestor", stack.child, head)
    git(stack.origin.clone, "merge-base", "--is-ancestor", next_head, head)
    assert git(stack.origin.clone, "show", f"{head}:child.txt") == "preserved child"
    completion = await stack.db.get_task_completion("child")
    assert completion.id.startswith("stack-") and completion.commits == [head]
    assert await stack.service.current("child", source_sha=head)
    assert not await stack.service.current("child", source_sha=stack.child)
    assert await stack.service.refresh("child", stack.gitops) == "unchanged"


@pytest.mark.parametrize("operation", ["reopen", "abandon", "missing", "rewind"])
async def test_reopened_abandoned_or_unproved_prerequisite_pauses_delivery(stack, operation):
    if operation == "reopen":
        await stack.db.transition_task("first", TaskStatus.READY, context="reopen", force=True)
    elif operation == "abandon":
        await stack.db.set_task_meta("first", "work_outcome", "abandoned")
    elif operation == "missing":
        git(stack.origin.clone, "push", "origin", ":aq/first")
    else:
        git(stack.origin.clone, "push", "--force", "origin", f"{stack.base}:aq/first")
    assert await stack.service.refresh("child", stack.gitops) == "waiting_prerequisite"
    assert not await stack.service.current("child")
    assert (
        git(stack.origin.clone, "ls-remote", "origin", "refs/heads/aq/child").split()[0]
        == stack.child
    )


async def test_stack_view_revalidates_incarnation_and_remote(stack):
    observer = stack.service.observer
    view = await observe_stacks(observer, "p", task_id="child")
    assert await view.fresh()
    async with stack.db._engine.connect() as conn:
        assert set(await view.verified_on(conn)) == {"first"}
    await stack.db.transition_task("first", TaskStatus.READY, context="reopen", force=True)
    async with stack.db._engine.connect() as conn:
        assert await view.verified_on(conn) == {}
    git(stack.origin.clone, "push", "--force", "origin", f"{stack.base}:aq/first")
    assert not await view.fresh()


async def test_batch_orders_prerequisite_before_dependent_and_checks_stale_stack(stack):
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    batches = DatabaseBatches(stack.db)
    members, requests, dependencies = await batches.pending(
        target, await snapshot(stack.world, target)
    )
    from src.integration.batches import Batch, ordered_members

    ordered = ordered_members(members, dependencies)
    assert [member.task_id for member in ordered] == ["first", "child"]
    batch = Batch("test-stack", "p", "r", target.target_ref)
    assert await batches.eligible(batch, ordered)
    await stack.db.transition_task("first", TaskStatus.READY, context="reopen", force=True)
    assert not await batches.eligible(batch, ordered)
    assert await batches.pending(target, await snapshot(stack.world, target)) is None


async def test_batch_cap_cannot_drop_an_undelivered_prerequisite(stack):
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    assert (
        await DatabaseBatches(stack.db, limit=1).pending(
            target,
            await snapshot(stack.world, target),
        )
        is None
    )  # newest child cannot carry its older prerequisite outside the cap


async def test_batch_cap_allows_an_already_delivered_prerequisite(stack):
    git(stack.origin.clone, "push", "origin", f"{stack.first}:aq/epic")
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    members, _, _ = await DatabaseBatches(stack.db, limit=1).pending(
        target,
        await snapshot(stack.world, target),
    )
    assert [member.task_id for member in members] == ["child"]


async def test_stack_snapshot_migration_adds_jsonb_to_existing_schema_idempotently(world):  # noqa: F811
    from importlib import import_module

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import inspect

    revision = import_module("migrations.versions.a00000000081_stacked_branch_origins")

    def migrate(bind):
        bind.exec_driver_sql("ALTER TABLE task_branch_origins DROP COLUMN stack_snapshot")
        with Operations.context(MigrationContext.configure(bind)):
            revision.upgrade()
            revision.upgrade()
            column = next(
                column
                for column in inspect(bind).get_columns("task_branch_origins")
                if column["name"] == "stack_snapshot"
            )
            assert str(column["type"]) == "JSONB" and column["nullable"]
            revision.downgrade()
            revision.downgrade()
            assert "stack_snapshot" not in {
                column["name"] for column in inspect(bind).get_columns("task_branch_origins")
            }
            revision.upgrade()

    async with world.db._engine.begin() as conn:
        await conn.run_sync(migrate)


async def test_cleaned_delivered_source_keeps_dependent_admissible(stack):
    git(stack.origin.clone, "push", "origin", f"{stack.first}:aq/epic")
    git(stack.origin.clone, "push", "origin", ":aq/first")
    assert await stack.service.refresh("child", stack.gitops) == "unchanged"
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    members, _, _ = await DatabaseBatches(stack.db).pending(
        target, await snapshot(stack.world, target)
    )
    assert [member.task_id for member in members] == ["child"]
    view = await observe_stacks(stack.service.observer, "p", task_id="child")
    assert await view.fresh()
    git(stack.origin.clone, "push", "--force", "origin", f"{stack.base}:aq/epic")
    assert not await view.fresh()
    assert await stack.service.refresh("child", stack.gitops) == "waiting_prerequisite"


async def test_live_child_writer_withholds_refresh_without_losing_commits(stack):
    next_head = stack.origin.work("first", "revision")
    await close(stack.db, "first", [next_head], close_id="first-revised", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    await stack.db.transition_task("child", TaskStatus.IN_PROGRESS, force=True)
    assert await stack.service.refresh("child", stack.gitops) == "writer_active"
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/child") == stack.child
    assert not await stack.service.current("child")


async def test_changed_stack_replaces_frozen_inputs_and_tests_exact_combined_candidate(stack):
    from src.integration.batches import BatchService, BatchStore
    from src.integration.train import CandidateChecks, IntegrationTrain, TrainLane
    from src.integration.train_sources import LeasedPublish
    from tests.test_integration_train_sources import GreenWhen

    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    batches, checks = DatabaseBatches(stack.db), GreenWhen()
    candidates = CandidateChecks.fixed(checks)
    service = BatchService(
        BatchStore(stack.db),
        stack.gitops,
        publish=LeasedPublish(stack.db, stack.gitops.git),
        eligible=batches.eligible,
        gate=candidates.gate,
        require_attestation=False,
    )

    async def observe():
        return await snapshot(stack.world, target)

    train = IntegrationTrain(
        targets=SimpleNamespace(targets=AsyncMock(return_value=[target])),
        batches=batches,
        lane_for=AsyncMock(return_value=TrainLane(observe, service, candidates)),
        repair=SimpleNamespace(
            allocate=AsyncMock(side_effect=AssertionError("unexpected repair")),
            settle_green=AsyncMock(),
        ),
    )
    old = await train.visit(target)
    assert old.state == "testing", old
    await service.store.set_intent(old.batch_id, "paused")
    next_head = stack.origin.work("first", "reworked before publication")
    await close(stack.db, "first", [next_head], close_id="first-next", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    checks.green.add(old.candidate_sha)
    await train.visit(target)  # merges the new prerequisite, asks for a fresh observation
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/epic") == stack.base
    paused = await batches.current(target)
    assert paused.id == old.batch_id and paused.intent == "paused"
    await service.store.set_intent(old.batch_id, "open")
    new = await train.visit(target)
    assert new.state == "testing" and new.batch_id != old.batch_id, new
    assert new.candidate_sha != old.candidate_sha
    assert (
        git(
            stack.origin.clone, "show", f"{new.candidate_sha}:first-reworked before publication.txt"
        )
        == "reworked before publication"
    )
    assert git(stack.origin.clone, "show", f"{new.candidate_sha}:child.txt") == "preserved child"
    checks.green.add(new.candidate_sha)
    delivered = await train.visit(target)
    assert delivered.state == "delivered", delivered
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/epic") == new.candidate_sha


@pytest.mark.parametrize("successor", [False, True])
async def test_conflict_dispatches_isolated_repairs_and_adopts_resolution(stack, successor):
    origin = stack.origin
    # Both children alter the same file starting from the old prerequisite.
    git(origin.clone, "checkout", "aq/child")
    (origin.clone / "first-work.txt").write_text("child version\n")
    git(origin.clone, "add", ".")
    git(origin.clone, "commit", "-qm", "child edits shared file")
    child = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", "origin", "aq/child")
    await close(stack.db, "child", [child], close_id="child-edit", origin=origin)
    await checkpoint(stack.db, "child", child)
    git(origin.clone, "checkout", "aq/first")
    (origin.clone / "first-work.txt").write_text("new prerequisite version\n")
    git(origin.clone, "add", ".")
    git(origin.clone, "commit", "-qm", "rework prerequisite")
    first = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", "origin", "aq/first")
    await close(stack.db, "first", [first], close_id="first-edit", origin=origin)
    await checkpoint(stack.db, "first", first)
    assert await stack.service.refresh("child", stack.gitops) == "repair_filed"
    async with stack.db._engine.connect() as conn:
        record = await conn.scalar(
            select(task_branch_origins.c.stack_snapshot).where(
                task_branch_origins.c.task_id == "child",
            )
        )
    repair = await stack.db.get_task(record["repair_task_id"])
    assert repair.parent_task_id == "epic" and repair.status is TaskStatus.READY
    assert await generation(stack.db, "epic") == 1
    assert await stack.service.refresh("child", stack.gitops) == "repair_pending"
    git(origin.clone, "fetch", "origin")
    git(origin.clone, "checkout", "-b", repair.branch_name, f"origin/{repair.branch_name}")
    # Simulate the worker's conflict resolution while retaining both histories.
    result = subprocess.run(
        ["git", "merge", "--no-commit", first], cwd=origin.clone, capture_output=True
    )
    assert result.returncode == 1
    (origin.clone / "first-work.txt").write_text("resolved shared file\n")
    git(origin.clone, "add", ".")
    git(origin.clone, "commit", "-qm", "resolve stack conflict")
    resolved = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", "origin", repair.branch_name)
    await stack.db.save_task_completion(
        TaskCompletion(id="repair-close", task_id=repair.id, outcome="pass", commits=[resolved])
    )
    await stack.db.transition_task(repair.id, TaskStatus.COMPLETED)
    if successor:
        git(origin.clone, "checkout", "aq/first")
        (origin.clone / "first-work.txt").write_text("prerequisite changed during repair\n")
        git(origin.clone, "add", ".")
        git(origin.clone, "commit", "-qm", "prerequisite moved again")
        first = git(origin.clone, "rev-parse", "HEAD")
        git(origin.clone, "push", "origin", "aq/first")
        await close(stack.db, "first", [first], close_id="first-again", origin=origin)
        await checkpoint(stack.db, "first", first)
        assert await stack.service.refresh("child", stack.gitops) == "repair_filed"
        async with stack.db._engine.connect() as conn:
            record = await conn.scalar(
                select(task_branch_origins.c.stack_snapshot).where(
                    task_branch_origins.c.task_id == "child"
                )
            )
        next_repair = await stack.db.get_task(record["repair_task_id"])
        assert next_repair.id != repair.id
        assert await generation(stack.db, "epic") == 2
        assert git(origin.url, "rev-parse", f"refs/heads/{next_repair.branch_name}") == resolved
        assert await stack.service.refresh("child", stack.gitops) == "repair_pending"
        git(origin.clone, "fetch", "origin")
        git(origin.clone, "checkout", "-b", next_repair.branch_name, resolved)
        result = subprocess.run(
            ["git", "merge", "--no-commit", first], cwd=origin.clone, capture_output=True
        )
        assert result.returncode == 1
        (origin.clone / "first-work.txt").write_text("second resolution preserves child\n")
        git(origin.clone, "add", ".")
        git(origin.clone, "commit", "-qm", "resolve successor stack conflict")
        resolved = git(origin.clone, "rev-parse", "HEAD")
        git(origin.clone, "push", "origin", next_repair.branch_name)
        await stack.db.save_task_completion(
            TaskCompletion(
                id="successor-close", task_id=next_repair.id, outcome="pass", commits=[resolved]
            )
        )
        await stack.db.transition_task(next_repair.id, TaskStatus.COMPLETED)
    assert await stack.service.refresh("child", stack.gitops) == "refreshed"
    assert await stack.service.current("child", source_sha=resolved)
