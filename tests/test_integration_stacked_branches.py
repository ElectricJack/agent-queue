"""Stacked source admission and refresh over real Git and disposable PostgreSQL."""

import asyncio
import json
import logging
import subprocess

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    archived_tasks,
    events,
    integration_batches,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError, RemoteRefResult, RemoteRefState
from src.integration.batches import SupersedeMemberUnavailable, ejection_instruction
from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
from src.integration.provenance import CompletionIdentity, GitProvenance
from src.integration.stacked_branches import (
    StackPrerequisitesConflict,
    StackView,
    StackedBranches,
    merge_heads,
    observe_stacks,
    shared_stack_inputs,
    stacked_policy,
)
from src.integration.train import TrainTarget
from src.integration.train_controls import TrainControls
from src.integration.train_sources import DatabaseBatches, _never_trusted
from src.models import Project, Task, TaskCompletion, TaskStatus
from tests.test_delivery_consumers import close, git
from tests.test_integration_gitops import LocalGit
from tests.test_integration_train_sources import completed, lane, snapshot, world  # noqa: F401


async def generation(db, tid):
    async with db._engine.connect() as conn:
        return await conn.scalar(
            select(task_integration_checkpoints.c.generation).where(
                task_integration_checkpoints.c.task_id == tid
            )
        )


async def test_child_conflict_without_origin_keeps_plain_feedback(stack, monkeypatch):
    branches = StackedBranches(stack.db)
    monkeypatch.setattr(branches, "_origin", AsyncMock(return_value=None))
    assert await branches.reserve_child_conflict("child", None, "unused") is None


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


async def stack_record(stack):
    async with stack.db._engine.connect() as conn:
        return await conn.scalar(select(task_branch_origins.c.stack_snapshot).where(
            task_branch_origins.c.task_id == "child",
        ))


@pytest.fixture
async def stack(world):  # noqa: F811 - imported pytest fixture
    db, origin = world.db, world.origin
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
    assert stacked_policy({"id": "other"})
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
    assert stack.overlay["stack_snapshot"]["base_sha"] == stack.first
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


@pytest.mark.parametrize("mutation", ["deleted", "moved"])
async def test_retained_completion_stacks_without_live_source_branch(stack, mutation):
    if mutation == "deleted":
        git(stack.origin.clone, "push", "origin", ":aq/first")
    else:
        git(stack.origin.clone, "push", "--force", "origin", f"{stack.base}:aq/first")
    view = await observe_stacks(stack.service.observer, "p", task_id="child")
    assert view.proofs["first"]["checkpoint_sha"] == stack.first
    assert await view.fresh()
    assert await stack.service.current("child")
    assert (await stack.service.prepare("child"))["base_sha"] == stack.first
    assert await stack.service.source_contains_stack(
        "child", stack.child, await snapshot(stack.world),
    )
    assert git(stack.origin.url, "rev-parse", "aq/epic") == stack.base


async def test_cross_epic_and_root_sources_stack_on_current_parent(stack):
    await completed(stack.world, "other-epic", done=False, head=stack.base, source_base=stack.base)
    cross = await completed(stack.world, "cross", parent="other-epic")
    root = await completed(stack.world, "root")
    await checkpoint(stack.db, "cross", cross)
    await checkpoint(stack.db, "root", root)
    await stack.db.add_dependency("child", "cross")
    await stack.db.add_dependency("child", "root")
    git(stack.origin.clone, "push", "origin", ":aq/cross", ":aq/root")
    await completed(stack.world, "consumer", parent="epic", done=False,
                    head=stack.base, source_base=stack.base, needs=("first", "cross", "root"))
    await checkpoint(stack.db, "consumer", stack.base)
    prepared = await stack.service.prepare("consumer")
    assert set(prepared.proofs) == {"first", "cross", "root"}
    for head in (stack.base, stack.first, cross, root):
        git(prepared["stack_store"], "merge-base", "--is-ancestor", head, prepared["base_sha"])
    assert git(stack.origin.url, "rev-parse", "aq/epic") == stack.base
    assert git(stack.origin.url, "rev-parse", "main") == stack.base


@pytest.mark.parametrize("contained", [False, True])
@pytest.mark.parametrize("decision", ["label", "pause", "gate", "rejection", "operator"])
async def test_cross_epic_parent_decisions_travel_through_transitive_sources(
    stack, decision, contained, monkeypatch,
):
    # The foreign epic is still open; no synthetic epic approval is required.
    from src.database.tables import gates, integration_review_evidence, task_gates, task_labels
    from src.operator_decisions import OperatorDecisions

    db = stack.db
    await completed(stack.world, "other-epic", done=False, head=stack.base, source_base=stack.base)
    await checkpoint(db, "other-epic", stack.base)
    cross = await completed(stack.world, "cross", parent="other-epic")
    await checkpoint(db, "cross", cross)
    await db.add_dependency("first", "cross")
    if contained:
        git(stack.origin.clone, "push", "origin", f"{stack.first}:aq/epic", f"{stack.first}:main")
        import src.integration.stacked_branches as module

        # Exercise an expired ref cache without depending on machine speed.
        now = module.time.monotonic() + 3
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now))
    # Preparation observed the old target before the completions/push above.
    # Refresh both the snapshot and refs before testing the explicit decisions.
    view = await observe_stacks(stack.service.observer, "p", task_id="child")
    assert view.proofs
    assert await view.fresh()
    assert await db.is_hierarchy_task_runnable("child")
    async with db.immediate() as conn:
        if decision == "label":
            await conn.execute(insert(task_labels).values(task_id="other-epic", label="hold:human"))
        elif decision == "pause":
            await db._upsert_meta("other-epic", "manual_pause", True, conn=conn)
        elif decision == "gate":
            await conn.execute(insert(gates).values(id="human-decision", project_id="p",
                gate_type="human", title="Human decision", status="open", created_at=1))
            await conn.execute(insert(task_gates).values(task_id="other-epic", gate_id="human-decision"))
        elif decision == "rejection":
            await conn.execute(insert(integration_review_evidence).values(
                id="rejection", source_task_id="other-epic", repository_id="r",
                source_base=stack.base, reviewed_head_sha=stack.base,
                reviewed_tree_sha=git(stack.origin.clone, "rev-parse", stack.base + "^{tree}"),
                generation=0, verdict="rejected", review_kind="root", evidence={}, created_at=1))
    if decision == "operator":
        await OperatorDecisions(db).record({
            "object_kind": "task", "object_id": "other-epic", "effect": "hold",
            "source": "cli", "decision": "explicit whole epic hold",
            "operator": "human:test", "source_ref": "test:hold",
            "idempotency_key": "cross-epic-hold",
        }, recorded_by="human:test")
    assert not (await observe_stacks(stack.service.observer, "p", task_id="child")).proofs
    assert not await stack.service.current("child")
    assert not await db.is_hierarchy_task_runnable("child")
    assert git(stack.origin.url, "rev-parse", "aq/child") == stack.child


async def test_multiple_sources_use_topology_and_deduplicate_contained_heads(stack):
    from src.integration.stacked_branches import ordered_sources

    zulu = await completed(stack.world, "zulu", parent="epic")
    await checkpoint(stack.db, "zulu", zulu)
    git(stack.origin.clone, "checkout", "-b", "aq/alpha", zulu)
    (stack.origin.clone / "alpha.txt").write_text("alpha\n")
    git(stack.origin.clone, "add", ".")
    git(stack.origin.clone, "commit", "-qm", "alpha")
    alpha = git(stack.origin.clone, "rev-parse", "HEAD")
    git(stack.origin.clone, "push", "origin", "aq/alpha")
    await completed(stack.world, "alpha", parent="epic", needs=("zulu",), head=alpha)
    await checkpoint(stack.db, "alpha", alpha)
    async with stack.db._engine.connect() as conn:
        assert await ordered_sources(conn, {"alpha", "zulu"}) == ["zulu", "alpha"]
    merged = await merge_heads(stack.service.observer.git, stack.origin.clone, stack.first,
                               [zulu, alpha, zulu], stamp=1)
    assert git(stack.origin.clone, "show", "-s", "--format=%P", merged).split() == [stack.first, alpha]
    for head in (stack.first, zulu, alpha):
        git(stack.origin.clone, "merge-base", "--is-ancestor", head, merged)


@pytest.mark.parametrize("relation", [
    "behind", "equal", "ahead", "divergent", "unknown-forward", "unknown-reverse", "fetch-error",
])
async def test_prepare_preserves_child_history_against_fresh_publication(
    stack, tmp_path, monkeypatch, relation,
):
    await stack.db.transition_task("child", TaskStatus.READY, force=True)
    workspace = stack.origin.clone
    published = stack.child
    if relation != "equal":
        # Publish from a separate clone so neither the local child nor its
        # cached origin ref observes the preserved publication.
        publisher = tmp_path / "publisher"
        git(tmp_path, "clone", stack.origin.url, str(publisher))
        git(publisher, "checkout", "aq/child")
        (publisher / "published.txt").write_text("preserved publication\n")
        git(publisher, "add", ".")
        git(publisher, "commit", "-qm", "preserved published child")
        published = git(publisher, "rev-parse", "HEAD")
        git(publisher, "push", "origin", "aq/child")
    if relation == "ahead":
        git(workspace, "fetch", "origin")
        git(workspace, "merge", "--ff-only", published)
    if relation in {"ahead", "divergent"}:
        (workspace / "unpublished.txt").write_text("unique local work\n")
        git(workspace, "add", ".")
        git(workspace, "commit", "-qm", "unpublished child")
    local = git(workspace, "rev-parse", "aq/child")
    cached = git(workspace, "rev-parse", "origin/aq/child")
    if relation in {"behind", "unknown-forward", "unknown-reverse", "fetch-error"}:
        assert cached == stack.child and cached != published
    (workspace / "child.txt").write_text("dirty child edits\n")
    git(workspace, "add", "child.txt")
    (workspace / "untracked.txt").write_text("untracked child edits\n")
    index = git(workspace, "write-tree")
    status = git(workspace, "status", "--porcelain")
    origin = await stack.db.get_task_branch_origin_for_promotion("child", "r")
    manager = stack.service.observer.git
    ancestor = manager.ais_ancestor
    if relation.startswith("unknown"):
        async def unknown_probe(store, first, second, **kwargs):
            unknown_pair = ((published, local) if relation == "unknown-forward"
                            else (local, published))
            if (first, second) == unknown_pair:
                return None
            return await ancestor(store, first, second, **kwargs)

        monkeypatch.setattr(manager, "ais_ancestor", unknown_probe)
    if relation == "fetch-error":
        run = manager._arun

        async def failed_fetch(args, **kwargs):
            if args[:3] == ["fetch", "--no-tags", str(workspace)]:
                raise GitError("local object unavailable")
            return await run(args, **kwargs)

        monkeypatch.setattr(manager, "_arun", failed_fetch)
    if relation in {"divergent", "unknown-forward", "unknown-reverse", "fetch-error"}:
        with pytest.raises(GitError):
            await stack.service.prepare("child", workspace=str(workspace))
    else:
        overlay = await stack.service.prepare("child", workspace=str(workspace))
        expected = local if relation == "ahead" else published
        assert overlay["base_sha"] == expected
        assert git(overlay["stack_store"], "show", f"{expected}:child.txt") == "preserved child"
        if relation != "equal":
            assert git(overlay["stack_store"], "show", f"{expected}:published.txt") == (
                "preserved publication"
            )
        if relation == "ahead":
            assert git(overlay["stack_store"], "show", f"{expected}:unpublished.txt") == (
                "unique local work"
            )
        assert await overlay.view.fresh()
    assert git(workspace, "rev-parse", "aq/child") == local
    assert git(workspace, "rev-parse", "origin/aq/child") == cached
    assert git(workspace, "write-tree") == index
    assert git(workspace, "status", "--porcelain") == status
    assert (workspace / "child.txt").read_text() == "dirty child edits\n"
    assert (workspace / "untracked.txt").read_text() == "untracked child edits\n"
    assert git(stack.origin.url, "rev-parse", "aq/child") == published
    assert await stack.db.get_task_branch_origin_for_promotion("child", "r") == origin
@pytest.mark.parametrize("relationship", ["behind", "equal", "ahead", "diverged"])
async def test_prepare_local_child_ancestry_at_published_head(stack, tmp_path, relationship):
    await stack.db.transition_task("child", TaskStatus.IN_PROGRESS, force=True)
    clone = stack.origin.clone
    git(clone, "switch", "--detach")
    if relationship in {"behind", "diverged"}:
        git(clone, "branch", "-f", "aq/child", stack.first)
    workspace = tmp_path / "child-slot"
    git(clone, "worktree", "add", str(workspace), "aq/child")
    if relationship in {"ahead", "diverged"}:
        (workspace / "unpublished.txt").write_text("local child work\n")
        git(workspace, "add", "unpublished.txt")
        git(workspace, "commit", "-qm", "unpublished child")
    local_head = git(workspace, "rev-parse", "HEAD")
    recorded_stack = await stack_record(stack)

    if relationship == "diverged":
        with pytest.raises(GitError, match="local child does not descend"):
            await stack.service.prepare("child", workspace=str(workspace))
        assert git(workspace, "rev-parse", "HEAD") == local_head
        assert await stack_record(stack) == recorded_stack
        return

    overlay = await stack.service.prepare("child", workspace=str(workspace))
    expected = local_head if relationship == "ahead" else stack.child
    assert overlay["base_sha"] == expected
    # Observation must leave the branch untouched until the fenced checkout step.
    assert git(workspace, "rev-parse", "HEAD") == local_head
    transport = stack.service.observer.git
    await transport._arun(
        ["fetch", "--no-tags", str(overlay["stack_store"]), overlay["base_sha"]],
        cwd=str(workspace),
    )
    # Slot reset uses this same preparation step for preserved worker branches.
    await transport.aprepare_child_branch(str(workspace), "aq/child", overlay["base_sha"])
    assert git(workspace, "rev-parse", "HEAD") == expected
    assert git(workspace, "status", "--porcelain") == ""
    assert git(workspace, "show", "HEAD:child.txt") == "preserved child"
    if relationship == "ahead":
        assert (workspace / "unpublished.txt").read_text() == "local child work\n"
    assert git(stack.origin.url, "rev-parse", "aq/child") == stack.child
    assert await stack_record(stack) == recorded_stack


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


async def test_prepare_regenerates_two_real_prerequisite_catalogues(
    world, tmp_path, caplog,  # noqa: F811 - imported pytest fixture
):
    from tests.test_generated_artifacts import _catalogue_branches

    fixture = tmp_path / "catalogue-inputs"
    fixture.mkdir()
    repo, base, first, second = _catalogue_branches(fixture)
    db, origin = world.db, world.origin
    await db.update_project("p", hierarchical_integration_policy={"prerequisite_branches": "stacked"})
    git(origin.clone, "fetch", str(repo), base, first, second)
    git(origin.clone, "push", "origin", f"{base}:refs/heads/aq/epic", f"{first}:refs/heads/aq/first",
        f"{second}:refs/heads/aq/second", f"{base}:refs/heads/aq/child")
    await db.create_task(Task(id="epic", project_id="p", repo_id="r", title="epic",
                              description="", branch_name="aq/epic", status=TaskStatus.IN_PROGRESS))
    await checkpoint(db, "epic", base)
    for tid, head in (("first", first), ("second", second)):
        await completed(world, tid, parent="epic", head=head, source_base=base)
        await checkpoint(db, tid, head)
    await completed(world, "child", parent="epic", needs=("first", "second"),
                    head=base, source_base=base, done=False)
    service = StackedBranches(db)
    with caplog.at_level(logging.INFO, logger="src.integration.regeneration"):
        overlay = await service.prepare("child")
    store, head = overlay["stack_store"], overlay["base_sha"]
    for prerequisite in (base, first, second):
        git(store, "merge-base", "--is-ancestor", prerequisite, head)
    catalogue = json.loads(git(store, "show", f"{head}:tests/selection_catalogue.json"))
    assert set(catalogue["modules"]) == {"tests/test_a.py", "tests/test_b.py", "tests/test_c.py"}
    assert "regeneration: rebuilt" in caplog.text
    recorded = (await db.get_task_branch_origin_for_promotion("child", "r"))["stack_snapshot"]
    assert set(recorded["prerequisites"]) == {"first", "second"}
    assert recorded["regenerations"][0]["files"] == ["tests/selection_catalogue.json"]
    assert recorded["regenerations"][0]["commit"] == head
    assert recorded["regenerations"][0]["prerequisites"] == recorded["prerequisites"]
    assert await service.prepare("child") == overlay
    assert await db.get_task_meta("child", StackPrerequisitesConflict.code) is None
    assert git(origin.url, "rev-parse", "aq/child") == base


@pytest.mark.parametrize("repair_result", ["pass", "failed", "unrelated", "moved"])
async def test_prepare_conflict_files_once_and_requires_published_resolution(stack, repair_result):
    from src.database.queries.claim_queries import _claim_preparation_predicates

    db, origin = stack.db, stack.origin

    async def admission_open():
        async with db._engine.connect() as conn:
            return await conn.scalar(select(
                _claim_preparation_predicates()[StackPrerequisitesConflict.code],
            ).select_from(tasks).where(tasks.c.id == "child"))

    git(origin.clone, "checkout", "-b", "aq/second", stack.base)
    (origin.clone / "first-work.txt").write_text("conflicting second prerequisite\n")
    git(origin.clone, "add", ".")
    git(origin.clone, "commit", "-qm", "second prerequisite")
    second = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", "origin", "aq/second")
    await completed(stack.world, "second", parent="epic", head=second, source_base=stack.base)
    await checkpoint(db, "second", second)
    await db.add_dependency("child", "second")
    await db.transition_task("child", TaskStatus.READY, force=True)
    before_generation = await generation(db, "epic")
    with pytest.raises(StackPrerequisitesConflict) as refusal:
        await stack.service.prepare("child")
    detail = refusal.value.detail
    assert detail["files"] == ["first-work.txt"]
    assert set(detail["prerequisites"]) == {"first", "second"}
    repair = await db.get_task(detail["repair_task_id"])
    assert repair.parent_task_id == "epic" and repair.status is TaskStatus.DEFINED
    for required in ("first", "second", stack.first, second, "first-work.txt", "never hand-merge"):
        assert required in repair.description
    assert (await db.get_task("child")).status is TaskStatus.READY
    assert not await admission_open()
    for _ in range(4):
        with pytest.raises(StackPrerequisitesConflict) as repeated:
            await stack.service.prepare("child")
        assert repeated.value.detail == detail
    assert await generation(db, "epic") == before_generation + 1
    assert git(origin.url, "rev-parse", "aq/child") == stack.child
    from src.integration.branch_materialization import BranchMaterializationService
    from src.integration.hierarchy import HierarchyIntegration

    def materialize(repository, branch, head):
        git(origin.clone, "push", "origin", f"{head}:refs/heads/{branch}")
        return head

    scanner = BranchMaterializationService(
        db, hierarchy_service_factory=lambda: HierarchyIntegration(db, branch_materializer=materialize),
        legacy_container_collection=False,
    )
    assert await scanner.drain_due()
    assert (await db.get_task(repair.id)).status is TaskStatus.READY
    assert git(origin.url, "rev-parse", repair.branch_name) == detail["starting_head"]
    # Simulate a worker resolving the source conflict on the materialized branch.
    git(origin.clone, "checkout", "-b", repair.branch_name,
        stack.base if repair_result == "unrelated" else detail["starting_head"])
    if repair_result != "unrelated":
        conflict = subprocess.run(["git", "merge", "--no-commit", second], cwd=origin.clone,
                                  capture_output=True)
        assert conflict.returncode == 1
        (origin.clone / "first-work.txt").write_text("resolved prerequisites\n")
    else:
        (origin.clone / "unrelated.txt").write_text("unrelated history\n")
    git(origin.clone, "add", ".")
    git(origin.clone, "commit", "-qm", "resolve prerequisites")
    resolved = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", *(["--force"] if repair_result == "unrelated" else []),
        "origin", repair.branch_name)
    await db.save_task_completion(TaskCompletion(
        id="prepare-repair", task_id=repair.id,
        outcome="fail" if repair_result == "failed" else "pass", commits=[resolved],
    ))
    await db.transition_task(repair.id, TaskStatus.COMPLETED, force=True)
    assert await admission_open() is (repair_result != "failed")
    if repair_result == "moved":
        (origin.clone / "unreviewed.txt").write_text("moved after close\n")
        git(origin.clone, "add", ".")
        git(origin.clone, "commit", "-qm", "unrecorded movement")
        git(origin.clone, "push", "origin", repair.branch_name)
    if repair_result != "pass":
        with pytest.raises(StackPrerequisitesConflict):
            await stack.service.prepare("child")
        updated = await db.get_task_meta("child", StackPrerequisitesConflict.code)
        if repair_result != "failed":
            assert updated["repair_task_id"] != repair.id
            assert updated["superseded_repair_task_id"] == repair.id
            assert not await admission_open()
        return
    overlay = await stack.service.prepare("child")
    for head in (stack.child, stack.first, second, resolved):
        git(overlay["stack_store"], "merge-base", "--is-ancestor", head, overlay["base_sha"])
    # Preparation keeps the repair reservation until the final writer handoff.
    assert overlay["stack_snapshot"]["preparation_conflict"] == detail
    from src.integration.stacked_branches import verify_preparation

    await verify_preparation({
        **overlay, "preparation_view": overlay.view,
        "preparation_proofs": overlay.proofs, "preparation_repair": overlay.repair,
    }, finalize=True)
    adopted = await stack_record(stack)
    assert "hold" not in adopted and "preparation_conflict" not in adopted
    assert not (await db.get_task("child")).is_blocked
    repeated = await stack.service.prepare("child")
    assert repeated["base_sha"] == overlay["base_sha"]
    assert repeated["stack_snapshot"] == adopted
    assert await merge_heads(stack.gitops.git, origin.clone, resolved,
                             [stack.first, second], stamp=0) == resolved


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
    await stack.service.prepare("child")
    assert (await stack_record(stack))["refreshed_head"] == head
    assert await stack.service.refresh("child", stack.gitops) == "unchanged"


@pytest.fixture
async def empty_stack(stack):
    # Parent-only preparation records a stack even with no sibling prerequisites.
    head = await completed(stack.world, "empty", parent="epic")
    await checkpoint(stack.db, "empty", head)
    overlay = await stack.service.prepare("empty", parent_base=stack.base)
    assert overlay["stack_snapshot"]["prerequisites"] == {}
    return SimpleNamespace(**vars(stack), empty=head)


@pytest.mark.parametrize("selection", [False, True], ids=["refresh", "selection"])
async def test_empty_prerequisite_stack_keeps_original_completion(empty_stack, selection):
    stack = empty_stack
    original = await stack.db.get_task_completion("empty")
    origin = await stack.db.get_task_branch_origin_for_promotion("empty", "r")
    original_generation = await generation(stack.db, "empty")
    if selection:
        target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
        train, _, _ = lane(stack.world, stack.gitops.git, target=target)
        selected = await train.batches.open_batch(
            target, await snapshot(stack.world, target), (await train.lane_for(target)).service,
        )
        assert selected.blockers == ()
        assert any(member.task_id == "empty" and member.source_sha == stack.empty
                   for member in selected.members)
    else:
        assert await stack.service.refresh("empty", stack.gitops) == "unchanged"
    assert (await stack.db.get_task_completion("empty")).id == original.id
    assert await generation(stack.db, "empty") == original_generation
    assert await stack.db.get_task_branch_origin_for_promotion("empty", "r") == origin
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/empty") == stack.empty
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/epic") == stack.base


@pytest.mark.parametrize("selection", [False, True], ids=["refresh", "frozen-selection"])
@pytest.mark.parametrize("evidence", [
    "repository_missing", "snapshot_error", "target_moved", "refs_unreadable",
])
async def test_empty_stack_requires_available_current_git(empty_stack, monkeypatch,
                                                        evidence, selection):
    stack = empty_stack
    original = await stack.db.get_task_completion("empty")
    original_generation = await generation(stack.db, "empty")
    origin = await stack.db.get_task_branch_origin_for_promotion("empty", "r")
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    observed = await snapshot(stack.world, target)
    if selection:
        train, _, _ = lane(stack.world, stack.gitops.git, target=target)
        service = (await train.lane_for(target)).service
        frozen = await train.batches.open_batch(target, observed, service)
        assert any(member.task_id == "empty" for member in frozen.members)
    if evidence == "repository_missing":
        await stack.db.update_project("p", hierarchical_integration_mode="disabled",
                                      integration_repository_id=None)
    elif evidence == "snapshot_error":
        observed = replace(observed, observation=replace(
            observed.observation, error="snapshot_git_error: fetch failed",
        ))
    elif evidence == "target_moved":
        git(stack.origin.clone, "push", "origin", f"{stack.first}:aq/epic")
    else:
        monkeypatch.setattr(stack.service.observer.git, "als_remote_refs",
                            AsyncMock(side_effect=GitError("remote unreadable")))
    if selection:
        selected = await train.batches.open_batch(target, observed, service)
        # Lost evidence withholds publication without replacing frozen inputs.
        assert selected.batch.id == frozen.batch.id
        assert selected.members == frozen.members
        assert not await train.batches.eligible(selected.batch, selected.members)
    else:
        assert await stack.service.refresh("empty", stack.gitops,
                                           snapshot=observed) == "waiting_prerequisite"
    held = await stack.db.get_task_branch_origin_for_promotion("empty", "r")
    assert held == {**origin, "stack_snapshot": {
        **origin["stack_snapshot"], "hold": "prerequisite_reopened_or_unavailable",
    }}
    assert (await stack.db.get_task_completion("empty")).id == original.id
    assert await generation(stack.db, "empty") == original_generation
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/empty") == stack.empty


@pytest.mark.parametrize("selection", [False, True], ids=["refresh", "selection"])
@pytest.mark.parametrize("change", ["source_missing", "source_changed", "writer_active"])
async def test_empty_stack_preserves_source_and_writer_fences(empty_stack, change, selection):
    stack = empty_stack
    original = await stack.db.get_task_completion("empty")
    original_generation = await generation(stack.db, "empty")
    source = stack.empty
    if change == "source_missing":
        git(stack.origin.clone, "push", "origin", ":aq/empty")
        source = None
    elif change == "source_changed":
        source = stack.origin.work("empty", "unreviewed post-close work")
    else:
        await stack.db.transition_task("empty", TaskStatus.IN_PROGRESS, force=True)
    if selection:
        target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
        train, _, _ = lane(stack.world, stack.gitops.git, target=target)
        selected = await train.batches.open_batch(
            target, await snapshot(stack.world, target), (await train.lane_for(target)).service,
        )
        assert "empty" not in {member.task_id for member in selected.members}
        if change != "writer_active":
            assert any(blocker["code"] == "stack_" + change
                       and blocker["task_id"] == "empty" for blocker in selected.blockers)
    else:
        assert await stack.service.refresh("empty", stack.gitops) == change
    assert (await stack.db.get_task_completion("empty")).id == original.id
    assert await generation(stack.db, "empty") == original_generation
    remote = git(stack.origin.clone, "ls-remote", "origin", "refs/heads/aq/empty")
    assert (remote.split()[0] if remote else None) == source
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/epic") == stack.base


@pytest.mark.parametrize("cancel_visit", [False, True])
async def test_selection_finishes_pushed_stack_refresh_and_records_completion(
    stack, monkeypatch, cancel_visit,
):
    from src.integration.batches import BatchStore

    next_head = stack.origin.work("first", "revision")
    await close(stack.db, "first", [next_head], close_id="first-revised", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    entered, release = asyncio.Event(), asyncio.Event()
    write = GitProvenance.write_completion

    async def paused_completion(self, *args, **kwargs):
        entered.set()  # The fenced branch push has already completed.
        await release.wait()
        return await write(self, *args, **kwargs)

    monkeypatch.setattr(GitProvenance, "write_completion", paused_completion)
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    batches = DatabaseBatches(stack.db)
    observed = await snapshot(stack.world, target)
    selecting = asyncio.create_task(batches.open_batch(
        target, observed, SimpleNamespace(store=BatchStore(stack.db), gitops=stack.gitops),
    ))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        pushed = git(stack.origin.clone, "ls-remote", "origin", "refs/heads/aq/child").split()[0]
        assert pushed != stack.child
        if cancel_visit:
            selecting.cancel()
            await asyncio.sleep(0)
            assert not selecting.done()
            selecting.cancel()  # A second cancellation must still join the write.
    finally:
        release.set()
        if cancel_visit:
            with pytest.raises(asyncio.CancelledError):
                await selecting
        else:
            result = await selecting
            assert result.blockers[0]["code"] == "stack_refreshed"
    completion = await stack.db.get_task_completion("child")
    assert completion.id.startswith("stack-") and completion.commits == [pushed]
    assert (await stack_record(stack))["refreshed_head"] == pushed
    assert await generation(stack.db, "child") == 1
    record = await GitProvenance(
        stack.gitops.git, str(stack.origin.clone), repository_url=stack.origin.url,
    ).read_completion(CompletionIdentity("p", "r", "child", completion.id))
    assert record["source_oid"] == pushed


@pytest.mark.parametrize("unavailable", [
    "missing", "ambiguous", "foreign_project", "foreign_project_archived",
])
async def test_unavailable_refreshed_member_preserves_batch_and_other_targets_progress(
    stack, unavailable,
):
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    train, _, _ = lane(stack.world, LocalGit(stack.origin.url), target=target)
    visit = await train.visit(target)
    assert visit.state == "testing"
    service = (await train.lane_for(target)).service
    store = service.store
    batch = await store.get(visit.batch_id)
    frozen = await store.members(batch.id)
    assert {member.task_id for member in frozen} == {"first", "child"}

    next_head = stack.origin.work("first", "revision")
    await close(stack.db, "first", [next_head], close_id="first-revised", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    assert await stack.service.refresh("child", stack.gitops) == "refreshed"
    refreshed = (await stack_record(stack))["refreshed_head"]
    if unavailable.startswith("foreign_project"):
        await stack.db.create_project(Project(id="foreign", name="Foreign"))
    async with stack.db.immediate() as conn:
        task_row = dict((await conn.execute(select(tasks).where(
            tasks.c.id == "child",
        ))).mappings().one())
        if unavailable == "missing":
            await stack.db._delete_one("child", conn=conn)
        elif unavailable == "foreign_project":
            await conn.execute(update(tasks).where(tasks.c.id == "child")
                               .values(project_id="foreign"))
        else:
            if unavailable == "foreign_project_archived":
                await stack.db._delete_one("child", conn=conn)
                task_row["project_id"] = "foreign"
            await conn.execute(insert(archived_tasks).values(
                **{key: value for key, value in task_row.items() if key in archived_tasks.c},
                archived_at=1.0,
            ))
        original = dict((await conn.execute(select(integration_batches).where(
            integration_batches.c.id == batch.id,
        ))).mappings().one())
        assert await conn.scalar(select(task_branch_origins.c.stack_snapshot).where(
            task_branch_origins.c.task_id == "child",
        ))

    code = "stack_member_" + unavailable.removesuffix("_archived")
    with pytest.raises(SupersedeMemberUnavailable) as refusal:
        await store.supersede(batch, "child", reason="stacked source was refreshed")
    assert refusal.value.code == code and refusal.value.task_id == "child"
    for _ in range(2):
        blocked = await train.visit(target)
        assert blocked.state == "blocked" and blocked.batch_id is None
        [blocker] = blocked.detail["blockers"]
        assert blocker["code"] == code
        assert blocker["task_id"] == "child" and blocker["batch_id"] == batch.id
        assert f"aq integration abort-batch {batch.id}" in blocker["detail"]
        assert "frozen inputs withheld" in blocker["detail"]

    # A daemon tick can keep visiting the held target while batching another.
    await completed(stack.world, "independent")
    root = TrainTarget("p", "r", "refs/heads/main", "root")
    root_train, _, _ = lane(stack.world, LocalGit(stack.origin.url))
    epic_lane, root_lane = train.lane_for, root_train.lane_for

    async def lane_for(requested):
        return await (epic_lane(requested) if requested == target else root_lane(requested))

    train.lane_for = lane_for
    await train.tick()
    await train.drain()
    status = {row["target_ref"]: row for row in train.status()}
    assert status[target.target_ref]["state"] == "blocked"
    assert status[root.target_ref]["state"] == "testing"
    assert all(row["errors"] == 0 for row in status.values())
    assert [member.task_id for member in await store.members(
        status[root.target_ref]["batch_id"],
    )] == ["independent"]
    async with stack.db._engine.connect() as conn:
        unchanged = dict((await conn.execute(select(integration_batches).where(
            integration_batches.c.id == batch.id,
        ))).mappings().one())
        assert unchanged == original
        assert not await conn.scalar(select(ejection_instruction(batch.id)))
        assert not await conn.scalar(select(events.c.id).where(
            events.c.event_type == "integration.batch_superseded",
        ))
    assert await store.members(batch.id) == frozen
    assert git(stack.origin.url, "rev-parse", target.target_ref) == stack.base

    if unavailable.startswith("foreign_project"):
        controls = TrainControls(stack.db, snapshot=lambda db, requested: snapshot(
            stack.world, requested,
        ))
        aborted = await controls.abort_batch(batch.id, dry_run=False,
            operator_id="human:local-operator", reason="withhold foreign-project inputs")
        assert aborted["outcome"] == "aborted"
        recovered = await train.visit(target)
        assert recovered.state == "testing" and recovered.batch_id != batch.id
        assert all(blocker["code"] != code for blocker in (recovered.detail or {}).get("blockers", []))
        assert [member.task_id for member in await store.members(recovered.batch_id)] == ["first"]
        assert (await store.get(batch.id)).intent == "aborted"
        async with stack.db._engine.connect() as conn:
            assert not await conn.scalar(select(ejection_instruction(batch.id)))
            assert not await conn.scalar(select(events.c.id).where(
                events.c.event_type == "integration.batch_superseded",
            ))
        assert await store.members(batch.id) == frozen
        return

    # Restoring an unambiguous identity lets a later visit audit the release.
    async with stack.db.immediate() as conn:
        if unavailable == "missing":
            await conn.execute(insert(tasks).values(**task_row))
        else:
            await conn.execute(delete(archived_tasks).where(archived_tasks.c.id == "child"))
    if unavailable == "missing":
        await stack.db.add_dependency("child", "first")
        await close(stack.db, "child", [refreshed], close_id="child-restored", origin=stack.origin)
    recovered = await train.visit(target)
    assert recovered.state == "testing" and recovered.batch_id != batch.id
    assert {member.task_id for member in await store.members(recovered.batch_id)} == {
        "first", "child",
    }
    assert (await store.get(batch.id)).intent == "aborted"
    async with stack.db._engine.connect() as conn:
        assert await conn.scalar(select(ejection_instruction(batch.id)))
        record = await conn.scalar(select(integration_batches.c.ejection_record).where(
            integration_batches.c.id == batch.id,
        ))
    assert record["task_id"] == "child" and record["operator_id"] == "service:integration-train"


async def test_reclosed_dependent_retains_its_new_recorded_completion(stack):
    next_head = stack.origin.work("first", "revision")
    await close(stack.db, "first", [next_head], close_id="first-revised", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    assert await stack.service.refresh("child", stack.gitops) == "refreshed"
    previous = (await stack_record(stack))["refreshed_head"]
    await stack.db.transition_task("child", TaskStatus.IN_PROGRESS, force=True)
    await stack.service.prepare("child")
    assert (await stack_record(stack))["refreshed_head"] == previous
    head = stack.origin.work("child", "reviewed rework")
    await close(stack.db, "child", [head], close_id="child-reclosed", origin=stack.origin)
    await checkpoint(stack.db, "child", head)
    child_generation = await generation(stack.db, "child")
    assert await stack.service.refresh("child", stack.gitops) == "unchanged"
    assert await stack.service.current("child", source_sha=head)
    assert (await stack.db.get_task_completion("child")).id == "child-reclosed"
    assert await generation(stack.db, "child") == child_generation
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/child") == head


async def test_dependent_tip_moved_after_completion_is_held_without_approval(stack):
    original = await stack.db.get_task_completion("child")
    child_generation = await generation(stack.db, "child")
    extra = stack.origin.work("child", "unreviewed post-close work")
    next_head = stack.origin.work("first", "revision")
    await close(stack.db, "first", [next_head], close_id="first-revised", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    assert await stack.service.refresh("child", stack.gitops) == "source_changed"
    assert (await stack_record(stack))["hold"] == "source_changed"
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/child") == extra
    assert (await stack.db.get_task_completion("child")).id == original.id
    assert await generation(stack.db, "child") == child_generation
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    blockers = []
    members, _, _ = await DatabaseBatches(stack.db).pending(
        target, await snapshot(stack.world, target), blockers=blockers,
    )
    assert [member.task_id for member in members] == ["first"]
    assert any(blocker["code"] == "stack_source_changed" for blocker in blockers)


async def test_checkpoint_must_match_current_completion_before_refresh(stack):
    original = await stack.db.get_task_completion("child")
    extra = stack.origin.work("child", "unrecorded checkpoint")
    await checkpoint(stack.db, "child", extra)
    assert await stack.service.refresh("child", stack.gitops) == "source_unrecorded"
    assert (await stack_record(stack))["hold"] == "source_unrecorded"
    assert (await stack.db.get_task_completion("child")).id == original.id
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/child") == extra


async def test_undelivered_dependent_with_missing_ref_is_held(stack):
    original = await stack.db.get_task_completion("child")
    git(stack.origin.clone, "push", "origin", ":aq/child")
    assert await stack.service.refresh("child", stack.gitops) == "source_missing"
    assert (await stack_record(stack))["hold"] == "source_missing"
    assert (await stack.db.get_task_completion("child")).id == original.id
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    blockers = []
    members, _, _ = await DatabaseBatches(stack.db).pending(
        target, await snapshot(stack.world, target), blockers=blockers,
    )
    assert [member.task_id for member in members] == ["first"]
    assert any(blocker["code"] == "stack_source_missing" for blocker in blockers)


async def test_non_descendant_prerequisite_rework_is_held(stack):
    original = await stack.db.get_task_completion("child")
    git(stack.origin.clone, "checkout", "-B", "aq/first", stack.base)
    (stack.origin.clone / "replacement.txt").write_text("replacement prerequisite\n")
    git(stack.origin.clone, "add", ".")
    git(stack.origin.clone, "commit", "-qm", "replace discarded prerequisite")
    replacement = git(stack.origin.clone, "rev-parse", "HEAD")
    git(stack.origin.clone, "push", "--force", "origin", "aq/first")
    await close(stack.db, "first", [replacement], close_id="first-replaced", origin=stack.origin)
    await checkpoint(stack.db, "first", replacement)
    assert await stack.service.refresh("child", stack.gitops) == "prerequisite_superseded"
    assert (await stack_record(stack))["hold"] == "prerequisite_superseded"
    assert not await stack.service.current("child")
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/child") == stack.child
    assert (await stack.db.get_task_completion("child")).id == original.id


async def test_transient_freshness_hold_clears_without_new_completion(stack, monkeypatch):
    from src.integration.stacked_branches import StackView

    original = await stack.db.get_task_completion("child")
    child_generation = await generation(stack.db, "child")
    with monkeypatch.context() as patch:
        patch.setattr(StackView, "fresh", AsyncMock(return_value=False))
        assert await stack.service.refresh("child", stack.gitops) == "waiting_prerequisite"
    assert not await stack.service.current("child")
    await stack.service.prepare("child")
    assert (await stack_record(stack))["hold"] == "prerequisite_reopened_or_unavailable"
    assert await stack.service.refresh("child", stack.gitops) == "unchanged"
    assert "hold" not in await stack_record(stack)
    assert await stack.service.current("child")
    assert (await stack.db.get_task_completion("child")).id == original.id
    assert await generation(stack.db, "child") == child_generation


@pytest.mark.parametrize("delivered_to", ["aq/epic", "main"])
async def test_already_delivered_dependent_keeps_its_completion(stack, delivered_to):
    original = await stack.db.get_task_completion("child")
    child_generation = await generation(stack.db, "child")
    git(stack.origin.clone, "push", "origin", f"{stack.child}:{delivered_to}")
    next_head = stack.origin.work("first", "revision after dependent delivery")
    await close(stack.db, "first", [next_head], close_id="first-revised", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    assert await stack.service.refresh("child", stack.gitops) == "already_delivered"
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/child") == stack.child
    assert (await stack.db.get_task_completion("child")).id == original.id
    assert await generation(stack.db, "child") == child_generation


@pytest.mark.parametrize("prerequisite_change", ["updated_at", "reopen", "reclose"])
@pytest.mark.parametrize("pending_dependent", [False, True], ids=["settle", "batch"])
async def test_delivered_stale_stack_does_not_block_epic_pending(
    stack, prerequisite_change, pending_dependent,
):
    original = await stack.db.get_task_completion("child")
    child_generation = await generation(stack.db, "child")
    git(stack.origin.clone, "push", "origin", f"{stack.child}:aq/epic")
    if pending_dependent:
        git(stack.origin.clone, "push", "origin", f"{stack.child}:refs/heads/aq/after")
        after = stack.origin.work("after")
        await completed(stack.world, "after", parent="epic", needs=("child",), head=after)
        await checkpoint(stack.db, "after", after)
        await stack.service.prepare("after")
    if prerequisite_change == "reopen":
        # Inject the stale generation from an earlier writer; the current
        # public mutation fence refuses reopening a contained prerequisite.
        async with stack.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "first")
                               .values(status="IN_PROGRESS", updated_at=tasks.c.updated_at + 1))
    elif prerequisite_change == "reclose":
        # Inject the stale generation from an earlier writer; the current
        # public mutation fence refuses reopening a contained prerequisite.
        async with stack.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "first")
                               .values(status="IN_PROGRESS", updated_at=tasks.c.updated_at + 1))
        await close(stack.db, "first", [stack.first], close_id="first-reclosed", origin=stack.origin)
        await checkpoint(stack.db, "first", stack.first)
    else:
        async with stack.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "first").values(
                updated_at=tasks.c.updated_at + 1,
            ))
    assert not await stack.service.current("child")
    assert await stack.service.refresh("child", stack.gitops) == "already_delivered"

    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    blockers = []
    pending = await DatabaseBatches(stack.db).pending(
        target, await snapshot(stack.world, target), blockers=blockers,
    )
    assert blockers == []
    if pending_dependent:
        members, requests, dependencies = pending
        assert [(member.task_id, member.source_sha) for member in members] == [("after", after)]
        assert set(requests) == {"after"}
        assert dependencies == {"after": set()}
    else:
        assert pending is None
    assert (await stack.db.get_task_completion("child")).id == original.id
    assert await generation(stack.db, "child") == child_generation


async def test_delivery_during_refresh_does_not_create_a_completion(stack):
    original = await stack.db.get_task_completion("child")
    next_head = stack.origin.work("first", "revision")
    await close(stack.db, "first", [next_head], close_id="first-revised", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)

    async def deliver_after_observation():
        git(stack.origin.clone, "push", "origin", f"{stack.child}:aq/epic")
        stack.gitops.git.read_hook = None

    stack.gitops.git.read_hook = deliver_after_observation
    assert await stack.service.refresh("child", stack.gitops) == "changed"
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/child") == stack.child
    assert (await stack.db.get_task_completion("child")).id == original.id


async def test_stack_freshness_batches_refs_and_caches_only_advisory_reads(stack, monkeypatch):
    import src.integration.stacked_branches as module

    second = await completed(stack.world, "second", parent="epic")
    await checkpoint(stack.db, "second", second)
    await stack.db.add_dependency("child", "second")
    observer = stack.service.observer
    observer._recent.clear()
    observer._stack_ref_cache.clear()
    batched = AsyncMock(wraps=observer.git.als_remote_refs)
    monkeypatch.setattr(observer.git, "als_remote_refs", batched)
    monkeypatch.setattr(observer.git, "als_remote_ref", AsyncMock(
        side_effect=AssertionError("per-prerequisite ref lookup"),
    ))
    now = module.time.monotonic()
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now))
    view = await observe_stacks(observer, "p", task_id="child", max_age=30)
    assert await view.fresh()
    assert batched.await_count == 1
    assert set(batched.call_args.args[1]) == {"main"}
    another = await observe_stacks(observer, "p", task_id="child", max_age=30)
    assert await another.fresh()
    assert batched.await_count == 1
    # A guarded writer bypasses the advisory cache even inside its TTL.
    strict = await observe_stacks(observer, "p", task_id="child")
    git(stack.origin.clone, "push", "origin", f"{second}:main")
    assert not await strict.fresh()
    assert batched.await_count == 2
    now += 3
    assert not await view.fresh()
    assert batched.await_count == 3


async def test_open_batch_refreshes_only_its_targets_candidate_window(stack, monkeypatch):
    from src.integration.batches import BatchService, BatchStore

    child_stack = await stack_record(stack)
    await completed(stack.world, "other-epic")
    await completed(stack.world, "foreign-child", parent="other-epic")
    async with stack.db.immediate() as conn:
        from sqlalchemy import update

        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "foreign-child",
        ).values(stack_snapshot=child_stack))
    refresh = AsyncMock(return_value="unchanged")
    monkeypatch.setattr(StackedBranches, "refresh", refresh)
    batches = DatabaseBatches(stack.db, limit=1)
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    observed = await snapshot(stack.world, target)
    service = BatchService(BatchStore(stack.db), stack.gitops, publish=AsyncMock(),
                           eligible=batches.eligible, gate=AsyncMock(), require_attestation=False)
    await batches.open_batch(target, observed, service)
    assert refresh.await_count == 1
    assert refresh.call_args.args == ("child", stack.gitops)
    assert refresh.call_args.kwargs == {"snapshot": observed}


async def test_stack_refresh_quantum_preserves_guards_and_admits_independent_work(stack, monkeypatch):
    original = await stack_record(stack)
    await completed(stack.world, "later-child", parent="epic", needs=("first",),
                    head=stack.child, source_base=stack.first)
    git(stack.origin.clone, "push", "origin", f"{stack.child}:refs/heads/aq/later-child")
    await checkpoint(stack.db, "later-child", stack.child)
    held = {**original, "hold": "prerequisite_reopened_or_unavailable"}
    async with stack.db.immediate() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "later-child",
        ).values(stack_snapshot=held))
    independent = await completed(stack.world, "z-independent", parent="epic")
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    train, _, _ = lane(stack.world, LocalGit(stack.origin.url), target=target)
    batches, calls, elapsed = train.batches, [], 0.0
    batches._selection_clock = lambda: elapsed
    refresh = StackedBranches.refresh

    async def slow_refresh(self, task_id, *args, **kwargs):
        nonlocal elapsed
        calls.append(task_id)
        result = await refresh(self, task_id, *args, **kwargs)
        elapsed += 61
        return result

    monkeypatch.setattr(StackedBranches, "refresh", slow_refresh)
    selected = await batches.open_batch(
        target, await snapshot(stack.world, target), (await train.lane_for(target)).service,
    )
    assert calls == ["child"]
    assert selected.batch is not None
    assert {m.task_id for m in selected.members} == {"child", "first", "z-independent"}
    assert next(m.source_sha for m in selected.members if m.task_id == "z-independent") == independent
    assert any(b["code"] == "stack_prerequisite_reopened_or_unavailable"
               and b["task_id"] == "later-child" for b in selected.blockers)
    assert await stack_record(stack) == original
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/child") == stack.child
    assert await generation(stack.db, "child") == 0


@pytest.mark.parametrize("operation", ["reopen", "abandon", "provenance_missing"])
async def test_reopened_abandoned_or_unproved_prerequisite_pauses_delivery(stack, operation):
    if operation == "reopen":
        await stack.db.transition_task("first", TaskStatus.READY, context="reopen", force=True)
    elif operation == "abandon":
        await stack.db.set_task_meta("first", "work_outcome", "abandoned")
    else:
        await stack.db.save_task_completion(TaskCompletion(
            id="unretained-first", task_id="first", outcome="pass", commits=[stack.first],
            completed_at=(await stack.db.get_task_completion("first")).completed_at + 1,
        ))
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
    assert await view.fresh()  # the frozen source is independent of its branch tip


@pytest.mark.parametrize("interruption", [None, RuntimeError, asyncio.CancelledError])
async def test_shared_stack_inputs_are_per_project_and_reset_after_exit(monkeypatch, interruption):
    read = AsyncMock(return_value=())
    monkeypatch.setattr("src.integration.stacked_branches._inputs", read)
    conn = object()
    first, second = StackView(None, "p"), StackView(None, "q")
    first.proofs = second.proofs = {"source": {"task_id": "source"}}

    async def scoped():
        async with shared_stack_inputs():
            for _ in range(3):
                await first.verified_on(conn)
                await second.verified_on(conn)
            assert read.await_count == 2
            if interruption:
                raise interruption()

    if interruption:
        with pytest.raises(interruption):
            await scoped()
    else:
        await scoped()
    for _ in range(2):
        await first.verified_on(conn)
    assert read.await_args_list == [((conn, "p"),), ((conn, "q"),),
                                   ((conn, "p"), {"source_ids": {"source"}}),
                                   ((conn, "p"), {"source_ids": {"source"}})]


async def test_shared_stack_inputs_isolate_overlapping_passes(monkeypatch):
    read = AsyncMock(return_value=())
    monkeypatch.setattr("src.integration.stacked_branches._inputs", read)
    entered, finished = asyncio.Event(), asyncio.Event()
    conn = object()
    view = StackView(None, "p")
    view.proofs = {"source": {"task_id": "source"}}

    async def first_pass():
        async with shared_stack_inputs():
            await view.verified_on(conn)
            entered.set()
            await finished.wait()
            await view.verified_on(conn)

    async def second_pass():
        await entered.wait()
        async with shared_stack_inputs():
            await view.verified_on(conn)
            await view.verified_on(conn)
        finished.set()

    await asyncio.gather(first_pass(), second_pass())
    assert read.await_count == 2


async def test_stack_verification_reads_only_requested_proof_sources(monkeypatch):
    proof = {"task_id": "source", "retained": True}
    read = AsyncMock(return_value=({"task_id": "source"},))
    monkeypatch.setattr("src.integration.stacked_branches._inputs", read)
    view = StackView(None, "p", proofs={"source": proof, "other": {"task_id": "other"}})
    conn = object()
    assert await view.verified_on(conn, {"source"}) == {"source": proof}
    read.assert_awaited_once_with(conn, "p", source_ids={"source"})


async def test_stack_withholds_prerequisite_when_local_object_validation_fails(stack, monkeypatch):
    observer = stack.service.observer
    view = await observe_stacks(observer, "p", task_id="child")
    assert set(view.proofs) == {"first"}
    observer.truth._objects.clear()
    run = AsyncMock(side_effect=OSError("observer store is unreadable"))
    monkeypatch.setattr(observer.git, "arun_git_result", run)
    unavailable = await observe_stacks(observer, "p", task_id="child", snapshot=view.snapshot)
    assert unavailable.proofs == {}
    run.assert_awaited()


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

    revision = import_module("migrations.versions.a00000000082_stacked_branch_origins")

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
    assert await view.fresh()
    assert await stack.service.refresh("child", stack.gitops) == "unchanged"


@pytest.mark.parametrize("target_ref", ["main", "aq/epic"])
async def test_cleaned_legacy_prerequisite_uses_exact_contained_completion(stack, target_ref):
    git(stack.origin.clone, "push", "origin", f"{stack.first}:{target_ref}")
    identity = CompletionIdentity("p", "r", "first", "close-first")
    git(stack.origin.clone, "push", "origin", ":aq/first", ":" + identity.ref)

    view = await observe_stacks(stack.service.observer, "p", task_id="child")
    assert view.proofs["first"]["checkpoint_sha"] == stack.first
    assert await view.fresh()
    assert await stack.service.refresh("child", stack.gitops) == "unchanged"
    assert await stack.service.current("child")
    if target_ref == "aq/epic":
        async with stack.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "epic").values(
                branch_name="aq/replaced-epic",
            ))
        async with stack.db._engine.connect() as conn:
            assert await view.verified_on(conn) == {}
    # The proof follows the delivery target, not the deleted task ref.
    git(stack.origin.clone, "push", "--force", "origin", f"{stack.base}:{target_ref}")
    assert not await view.fresh()


@pytest.mark.parametrize("target_ref", ["main", "aq/child"])
@pytest.mark.parametrize("reported", [True, False])
async def test_recompleted_cleaned_prerequisite_remains_current_when_contained(
    stack, target_ref, reported,
):
    if target_ref == "main":
        git(stack.origin.clone, "push", "origin", f"{stack.first}:main")
    git(stack.origin.clone, "push", "origin", ":aq/first")
    git(stack.origin.clone, "push", "origin", ":" + CompletionIdentity(
        "p", "r", "first", "close-first",
    ).ref)
    # An operator reclose changes the completion identity without changing
    # the source already present in the child's published head and default.
    await close(stack.db, "first", [stack.first] if reported else [], close_id="first-recompleted")
    async with stack.db.immediate() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "first",
        ).values(retired_at=2))
    observed = await snapshot(stack.world, TrainTarget("p", "r", "refs/heads/aq/epic", "epic"))
    assert await stack.service.current("child", source_sha=stack.child, snapshot=observed)
    async with stack.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "first").values(status="READY"))
    assert not await stack.service.current("child", source_sha=stack.child, snapshot=observed)


@pytest.mark.parametrize("reported", [True, False])
async def test_preparation_accepts_cleaned_retired_prerequisite_delivered_to_default(stack, reported):
    git(stack.origin.clone, "push", "origin", f"{stack.first}:main", ":aq/first")
    git(stack.origin.clone, "push", "origin", ":" + CompletionIdentity(
        "p", "r", "first", "close-first",
    ).ref)
    await close(stack.db, "first", [stack.first] if reported else [], close_id="first-recompleted")
    async with stack.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "child").values(status="READY"))
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "first",
        ).values(retired_at=2))
    # The parent branch predates the delivered source. Preparation can use its
    # retained origin and the verified stack instead of demanding parent delivery.
    assert await stack.db.hierarchy_prerequisite_delivery_head("child") is None
    assert await stack.db.is_hierarchy_task_runnable("child")
    prepared = await stack.service.prepare("child")
    assert prepared["stack_snapshot"]["base_sha"] == stack.first
    assert prepared["prerequisite_head"] == stack.child  # retain the child's existing commits
    if not reported:
        view = await observe_stacks(stack.service.observer, "p", task_id="child")
        await checkpoint(stack.db, "first", stack.child)
        async with stack.db._engine.connect() as conn:
            assert await view.verified_on(conn) == {}  # the fallback locator is also fenced


async def test_recompleted_stack_cannot_override_conflicting_immutable_source(stack):
    git(stack.origin.clone, "push", "origin", f"{stack.first}:main", ":aq/first")
    await close(stack.db, "first", [stack.child], close_id="first-recompleted", origin=stack.origin)
    from src.database.tables import task_completion_records

    # The delivered checkpoint/report must not override the different source
    # retained for this current completion, even though both are in the child.
    async with stack.db.immediate() as conn:
        await conn.execute(update(task_completion_records).where(
            task_completion_records.c.id == "first-recompleted",
        ).values(commits=json.dumps([stack.first])))
    observed = await snapshot(stack.world, TrainTarget("p", "r", "refs/heads/aq/epic", "epic"))
    assert not await stack.service.current("child", source_sha=stack.child, snapshot=observed)


@pytest.mark.parametrize("retained", [True, False])
async def test_recompleted_source_delivered_to_default_satisfies_older_child(stack, retained):
    source = stack.origin.work("first", "hand-landed revision")
    git(stack.origin.clone, "push", "origin", f"{source}:main", ":aq/first")
    await close(stack.db, "first", [source], close_id="hand-landed",
                origin=stack.origin if retained else None)
    # The old checkpoint and child lack the current source. Default delivery
    # satisfies the dependency; the old checkpoint is never substituted for it.
    observed = await snapshot(stack.world, TrainTarget("p", "r", "refs/heads/aq/epic", "epic"))
    assert await stack.service.current("child", source_sha=stack.child, snapshot=observed)
    git(stack.origin.clone, "push", "--force", "origin", f"{stack.base}:main")
    assert not await stack.service.current("child", source_sha=stack.child, snapshot=observed)


async def test_cleaned_legacy_prerequisite_preserves_batch_publication_authority(stack):
    target = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    git(stack.origin.clone, "push", "origin", f"{stack.first}:aq/epic")
    train, _, _ = lane(stack.world, stack.gitops.git, target=target)
    service = (await train.lane_for(target)).service
    observed = await snapshot(stack.world, target)
    members, requests, dependencies = await train.batches.pending(target, observed)
    assert [member.task_id for member in members] == ["child"]
    from src.integration.batches import Batch
    from src.integration.fixer_runtime import DaemonFixerRuntime
    from src.integration.lock import PublishWithdrawn
    from src.models import SessionRecord

    batch = await service.freeze(Batch("legacy-stack", "p", "r", target.target_ref), members,
                                 requests=requests, snapshot=observed, dependencies=dependencies)
    identity = CompletionIdentity("p", "r", "first", "close-first")
    git(stack.origin.clone, "push", "origin", ":aq/first", ":" + identity.ref)
    assert await stack.service.refresh("child", stack.gitops) == "unchanged"
    assert await train.batches.eligible(batch, members, snapshot=await snapshot(stack.world, target))
    assert await service._authorized(batch, members)
    session = SessionRecord(
        id="legacy-stack-fixer", project_id="p", profile_id="fixer", harness="codex",
        provider="fake", name="legacy-stack-fixer", lifecycle="named", work_dir="/repair",
        epoch="test", instance_token="legacy-stack-instance", started_at=1, state="running",
    )
    await stack.db.create_session(session)
    state = {"status": "active", "session_id": session.id,
             "instance_token": session.instance_token, "work_dir": session.work_dir,
             "target_ref": target.target_ref, "starting_sha": stack.child,
             "members": [{"task_id": member.task_id} for member in members]}
    async with stack.db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == batch.id,
        ).values(fixer_state=state))
    runtime = DaemonFixerRuntime(SimpleNamespace(db=stack.db, git=stack.gitops.git),
                                lanes=train.lane_for)

    async def publication_authority():
        current = await stack.db.get_integration_batch(batch.id)
        async with stack.db.immediate() as conn:
            return await runtime._check_publication_authority(
                conn, current, state, session, await train.lane_for(target), train.batches.eligible,
            )

    assert await publication_authority() == state
    # Simulate an already committed reopen; the lifecycle API separately
    # refuses reopening delivered work without its recovery authorization.
    async with stack.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "first").values(status="READY"))
    assert await stack.service.refresh("child", stack.gitops) == "waiting_prerequisite"
    assert not await service._authorized(batch, members)
    with pytest.raises(PublishWithdrawn, match="batch source or policy authority changed"):
        await publication_authority()


@pytest.mark.parametrize("case", ["reopened", "undelivered", "mismatched", "conflicting_marker"])
async def test_cleaned_prerequisite_requires_current_exact_delivered_source(stack, case):
    identity = CompletionIdentity("p", "r", "first", "close-first")
    git(stack.origin.clone, "push", "origin", f"{stack.first}:main", ":aq/first")
    if case != "conflicting_marker":
        git(stack.origin.clone, "push", "origin", ":" + identity.ref)
    if case == "reopened":
        async with stack.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "first").values(status="READY"))
    elif case == "undelivered":
        git(stack.origin.clone, "push", "--force", "origin", f"{stack.base}:main")
    elif case in {"mismatched", "conflicting_marker"}:
        # The checkpoint still names delivered work, but the immutable close
        # reports another source. Neither locator can answer that completion.
        from src.database.tables import task_completion_records

        async with stack.db.immediate() as conn:
            await conn.execute(update(task_completion_records).where(
                task_completion_records.c.id == "close-first",
            ).values(commits=json.dumps([stack.child])))
    view = await observe_stacks(stack.service.observer, "p", task_id="child")
    assert "first" not in view.proofs
    assert await stack.service.refresh("child", stack.gitops) == "waiting_prerequisite"
    assert not await stack.service.current("child")


async def test_live_child_writer_withholds_refresh_without_losing_commits(stack):
    next_head = stack.origin.work("first", "revision")
    await close(stack.db, "first", [next_head], close_id="first-revised", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    await stack.db.transition_task("child", TaskStatus.IN_PROGRESS, force=True)
    assert await stack.service.refresh("child", stack.gitops) == "writer_active"
    assert git(stack.origin.url, "rev-parse", "refs/heads/aq/child") == stack.child
    assert not await stack.service.current("child")


async def test_running_consumer_merges_replacement_before_close_without_reset(stack):
    await stack.db.transition_task("child", TaskStatus.IN_PROGRESS, force=True)
    workspace = stack.origin.clone
    git(workspace, "checkout", "aq/child")
    (workspace / "author-work").write_text("keep this work")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "author work")
    author_head = git(workspace, "rev-parse", "HEAD")
    await stack.db.transition_task("first", TaskStatus.READY, context="reopen", force=True)
    assert (await stack.service.running_blocker("child"))["code"] == "stack_stale_input"
    assert (await stack.service.running_blocker("child", workspace=workspace))["code"] == (
        "stack_stale_input"
    )
    next_head = stack.origin.work("first", "revision")
    await close(stack.db, "first", [next_head], close_id="replacement", origin=stack.origin)
    await checkpoint(stack.db, "first", next_head)
    git(workspace, "checkout", "aq/child")
    blocker = await stack.service.running_blocker("child", workspace=workspace)
    assert blocker["required_sources"] == {"first": next_head}
    assert git(workspace, "rev-parse", "HEAD") == author_head
    git(workspace, "merge", "--no-ff", "-m", "merge replacement prerequisite", next_head)
    assert await stack.service.running_blocker("child", workspace=workspace) is None
    assert await stack.service.current("child")
    git(workspace, "merge-base", "--is-ancestor", author_head, "HEAD")
    git(workspace, "merge-base", "--is-ancestor", next_head, "HEAD")
    assert (workspace / "author-work").read_text() == "keep this work"


async def test_changed_stack_replaces_frozen_inputs_and_tests_exact_combined_candidate(stack):
    from src.integration.batches import BatchService, BatchStore, ejection_instruction
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
    # The replaced batch gives up the target and releases its inputs.
    assert (await service.store.get(old.batch_id)).intent == "aborted"
    async with stack.db._engine.connect() as conn:
        assert await conn.scalar(select(ejection_instruction(old.batch_id)))
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
    # Workspace preparation must not erase the pending repair or its delivery hold.
    await stack.service.prepare("child")
    prepared = await stack_record(stack)
    assert prepared["repair_task_id"] == repair.id and prepared["hold"] == "stack_conflict"
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


_TARGET, _MOVED, _PROOF = "a" * 40, "b" * 40, "c" * 40


def _stack_view(refs=None, *, error=None, raises=None, proofs=None):
    async def als_remote_refs(store, branches):
        if raises:
            raise raises
        return {branch: refs[branch] for branch in branches if branch in refs}

    git = SimpleNamespace(aget_remote_url=AsyncMock(return_value="u"),
                          als_remote_refs=als_remote_refs)
    snapshot = SimpleNamespace(error=error, target_oid=_TARGET, target_ref="refs/heads/main",
                               store="s", repository_url="u", git=git)
    return StackView(db=None, project_id="p", rows=({"task_id": "x"},), proofs=proofs or {},
                     snapshot=snapshot)


def _ref(oid=None, state=None):
    return RemoteRefResult(state or (RemoteRefState.PRESENT if oid else RemoteRefState.ABSENT),
                           oid)


async def test_stack_currentness_separates_moved_refs_from_missing_git_evidence():
    proof = {"x": {"task_id": "x", "branch_name": "refs/heads/aq/x", "checkpoint_sha": _PROOF}}
    current = _stack_view({"main": _ref(_TARGET), "aq/x": _ref(_PROOF)}, proofs=proof)
    assert (await current.currentness()).state == "current"
    assert await current.fresh()

    moved = await _stack_view({"main": _ref(_TARGET), "aq/x": _ref(_MOVED)},
                              proofs=proof).currentness()
    assert moved.evidence() == {"state": "changed", "reason": "stack_ref_moved", "ref": "aq/x",
                                "expected": _PROOF, "observed": f"present:{_MOVED}",
                                "task_id": "x"}
    target = await _stack_view({"main": _ref(_MOVED)}).currentness()
    assert (target.state, target.ref, target.expected) == ("changed", "main", _TARGET)

    # No Git answer is never "current", and never claims a ref moved.
    for view, reason in (
        (_stack_view(error="snapshot_git_error: fetch failed"), "snapshot_git_error: fetch failed"),
        (_stack_view(raises=GitError("ls-remote failed")), "stack_refs_unreadable: GitError"),
        (_stack_view({"main": _ref(_TARGET), "aq/x": _ref(state=RemoteRefState.ERROR)},
                     proofs=proof), "stack_ref_unreadable"),
        (_stack_view({"main": _ref(_TARGET)}, proofs=proof), "stack_ref_unreadable"),
        (StackView(db=None, project_id="p", rows=({"task_id": "x"},)),
         "stack_repository_unavailable"),
    ):
        state = await view.currentness()
        assert (state.state, state.reason) == ("unavailable", reason)
        assert not await view.fresh()
    assert await StackView(db=None, project_id="p").fresh()
    unproven = _stack_view({"main": _ref(_TARGET)}, proofs=proof).without_proofs()
    assert (unproven.proofs, unproven.delivered_refs) == ({}, {})


@pytest.mark.parametrize("projection", ["missing", "stale", "prerequisite_missing"])
@pytest.mark.parametrize("constraint", ["none", "hold", "generation"])
async def test_stack_publication_recovers_checkpoint_from_current_git_completion(
    stack, projection, constraint,
):
    task_id = "first" if projection == "prerequisite_missing" else "child"
    async with stack.db._engine.begin() as conn:
        query = (update(task_integration_checkpoints).values(checkpoint_sha=stack.base)
                 if projection == "stale" else delete(task_integration_checkpoints))
        await conn.execute(query.where(task_integration_checkpoints.c.task_id == task_id))
    if constraint == "hold":
        old = await stack_record(stack)
        async with stack.db._engine.begin() as conn:
            await conn.execute(update(task_branch_origins).where(
                task_branch_origins.c.task_id == "child",
            ).values(stack_snapshot={**old, "hold": "operator_hold"}))
    elif constraint == "generation":
        changed = stack.origin.work("first", "changed")
        await close(stack.db, "first", [changed], close_id="changed", origin=stack.origin)
    observed = await snapshot(stack.world)
    assert await stack.service.current(
        "child", source_sha=stack.child, snapshot=observed,
    ) is (constraint == "none")


@pytest.mark.parametrize("changed", [False, True])
async def test_lost_stack_projection_still_proves_current_prerequisite_ancestry(stack, changed):
    async with stack.db._engine.begin() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "child",
        ).values(stack_snapshot=None))
    if changed:
        head = stack.origin.work("first", "new-prerequisite")
        await close(stack.db, "first", [head], close_id="new-prerequisite", origin=stack.origin)
    assert await stack.service.current(
        "child", source_sha=stack.child, snapshot=await snapshot(stack.world),
    ) is (not changed)


@pytest.mark.parametrize("locator", ["missing", "moved", "generation"])
async def test_current_source_rejection_survives_checkpoint_drift(stack, locator):
    from src.database.tables import integration_review_evidence

    async with stack.db.immediate() as conn:
        await conn.execute(insert(integration_review_evidence).values(
            id="explicit-source-rejection", source_task_id="first", repository_id="r",
            source_base=stack.base, reviewed_head_sha=stack.first,
            reviewed_tree_sha=git(stack.origin.clone, "rev-parse", stack.first + "^{tree}"),
            generation=0, verdict="rejected", review_kind="root",
            reviewer_identity="human:test", evidence={"decision_path": "tree_review"},
            created_at=1,
        ))
        source = task_integration_checkpoints.c.task_id == "first"
        if locator == "missing":
            await conn.execute(delete(task_integration_checkpoints).where(source))
        else:
            values = ({"checkpoint_sha": stack.base} if locator == "moved"
                      else {"generation": 99, "version": 99})
            await conn.execute(update(task_integration_checkpoints).where(source).values(**values))
        # Exercise the lost-stack recovery, where fresh Git otherwise proves
        # the child's exact source includes this completed prerequisite.
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "child",
        ).values(stack_snapshot=None))
    observed = await snapshot(stack.world)
    assert not await stack.service.current("child", source_sha=stack.child, snapshot=observed)
    # A later explicit approval of that exact source clears the veto even
    # when the checkpoint remains stale or absent.
    async with stack.db.immediate() as conn:
        await conn.execute(insert(integration_review_evidence).values(
            id="explicit-source-approval", source_task_id="first", repository_id="r",
            source_base=stack.base, reviewed_head_sha=stack.first,
            reviewed_tree_sha=git(stack.origin.clone, "rev-parse", stack.first + "^{tree}"),
            generation=0, verdict="approved", review_kind="root",
            reviewer_identity="human:test", evidence={"decision_path": "tree_review"},
            created_at=2,
        ))
    assert await stack.service.current("child", source_sha=stack.child, snapshot=observed)


async def test_inherited_repository_rejection_uses_project_when_checkpoint_is_missing(stack):
    from src.database.tables import integration_review_evidence
    from src.integration.stacked_branches import _unauthorized_sources

    async with stack.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "first").values(repo_id=None))
        await conn.execute(delete(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "first",
        ))
        await conn.execute(insert(integration_review_evidence).values(
            id="inherited-source-rejection", source_task_id="first", repository_id="r",
            source_base=stack.base, reviewed_head_sha=stack.first,
            reviewed_tree_sha=git(stack.origin.clone, "rev-parse", stack.first + "^{tree}"),
            generation=0, verdict="rejected", review_kind="root",
            reviewer_identity="human:test", evidence={"decision_path": "tree_review"},
            created_at=1,
        ))
        assert await _unauthorized_sources(conn, {"first"}) == {"first"}
