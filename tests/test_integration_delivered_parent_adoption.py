"""Real Git recovery for parents whose children reached main by other routes."""

import json
import time

import pytest
from sqlalchemy import insert, select, update

from src.database import tables as t
from src.doctor.integration_checks import run_check
from src.doctor.models import Severity
from src.integration.delivery_truth import DeliveryState
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.models import RepoSourceType, SessionRecord, Task, TaskCompletion, TaskStatus, Workspace
from tests.test_development_integration import (
    _merge_on_main,
    complete_source,
    feature,
    git,
    setup as setup,
)

PARENT = "agile-harbor-62"
VERIFIER = "verify-stale-aggregate"


@pytest.fixture
async def incident(setup):
    return await build_incident(setup, PARENT)


async def build_incident(setup, parent):
    db, service, source, remote, repo = setup
    base = git(source, "rev-parse", "main")
    children = [f"{parent}.{index}" for index in range(1, 5)]
    heads = {child: await feature(setup, child) for child in children}
    main = await _merge_on_main(source, *children)
    # The obsolete aggregate is deliberately not an ancestor of main.
    git(source, "checkout", "-B", "aq/epic/old-aggregate", base)
    (source / "obsolete.txt").write_text("old aggregate\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "obsolete aggregate")
    stale = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "HEAD:aq/epic/old-aggregate")
    await db.create_task(
        Task(
            id=parent,
            project_id="p",
            repo_id=repo.id,
            title="Train refactor phase 3",
            description="",
            status=TaskStatus.PAUSED,
            branch_name="aq/epic/old-aggregate",
        )
    )
    await db.create_task(
        Task(
            id=VERIFIER,
            project_id="p",
            repo_id=repo.id,
            title="Verify obsolete aggregate",
            description="",
            status=TaskStatus.READY,
            branch_name="aq/epic/old-aggregate",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(t.projects)
            .where(t.projects.c.id == "p")
            .values(
                hierarchical_integration_mode="train",
                hierarchical_integration_desired_mode="train",
            )
        )
        await conn.execute(
            update(t.tasks)
            .where(t.tasks.c.id.in_(children))
            .values(
                parent_task_id=parent,
            )
        )
        await conn.execute(
            insert(t.integration_parent_episodes).values(
                id="episode",
                parent_task_id=parent,
                repository_id=repo.id,
                generation=1,
                pre_collection_checkpoint_sha=base,
                created_at=time.time(),
            )
        )
        await conn.execute(
            insert(t.integration_repair_operations).values(
                id="collection",
                target_kind="parent",
                parent_task_id=parent,
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                verifier_task_id=VERIFIER,
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        await conn.execute(
            insert(t.task_integration_checkpoints).values(
                task_id=parent,
                repository_id=repo.id,
                branch="aq/epic/old-aggregate",
                generation=6,
                checkpoint_sha=stale,
                state="integration_ready",
                version=9,
                episode_id="episode",
                branch_owner_id=VERIFIER,
                updated_at=time.time(),
            )
        )
        await conn.execute(
            insert(t.task_branch_origins).values(
                id="stale-origin",
                task_id=VERIFIER,
                repository_id=repo.id,
                branch_name="aq/epic/old-aggregate",
                base_sha=stale,
                creation_generation=0,
                reserved=True,
                materialized=False,
                created_at=time.time(),
            )
        )
        await conn.execute(
            insert(t.integration_branch_owners).values(
                id="owner",
                repository_id=repo.id,
                ref="aq/epic/old-aggregate",
                owner_id=VERIFIER,
                owner_role="verifier",
                fence_token=4,
                handoff_state="reserved",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
    return setup, children, heads, main


async def adopt(incident, **overrides):
    setup, children, _heads, main = incident
    _db, service, *_ = setup
    args = dict(
        project_id="p",
        task_ids=[children[0].rsplit(".", 1)[0]],
        target_ref="refs/heads/main",
        head_sha=main,
        reason="all four children arrived through other routes",
        operator_id="supervisor",
        settle_delivered_children=True,
    )
    return await service.adopt(**(args | overrides))


async def test_settling_delivered_children_retires_parent_and_child_prs(incident, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tests.test_integration_pr_delivery import _GitHub

    await reserve_children(incident)
    db, service, _source, _remote, _repo = incident[0]
    children, heads = incident[1:3]
    github = _GitHub()
    parent = await db.get_task(PARENT)
    async with db._engine.connect() as conn:
        checkpoint = (await conn.execute(select(t.task_integration_checkpoints).where(
            t.task_integration_checkpoints.c.task_id == PARENT,
        ))).mappings().one()
    github.open(71, parent.branch_name, checkpoint["checkpoint_sha"])
    await db.update_task(PARENT, pr_url="https://github.com/o/r/pull/71")
    for number, child in enumerate(children, 72):
        github.open(number, "aq/" + child, heads[child])
        await db.update_task(child, pr_url=f"https://github.com/o/r/pull/{number}")
    monkeypatch.setattr(service.git, "bind_github_repository", AsyncMock(
        return_value=SimpleNamespace(repository_id=7, full_name="o/r")))
    monkeypatch.setattr(service.git, "_github_client", lambda _binding: github)
    preview = await adopt(incident, dry_run=True)
    assert preview["outcome"] == "would_adopt_parent" and github.closed == []
    result = await adopt(incident)
    assert result["outcome"] == "adopted"
    assert sorted(github.closed) == [71, 72, 73, 74, 75]
    assert all(item["outcome"] == "closed" for item in result["pr_cleanup"])
    assert "Superseded by delivered children" in github.comments[71][0]
    assert (await db.get_task(PARENT)).status == TaskStatus.COMPLETED


async def reserve_children(incident):
    setup, children, _heads, _main = incident
    db, _service, source, _remote, repo = setup
    async with db.immediate() as conn:
        for child in children:
            branch = "aq/" + child
            git(source, "push", "origin", f"{child}:{branch}")
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == child).values(branch_name=branch)
            )
            await conn.execute(
                insert(t.integration_branch_owners).values(
                    id="owner-" + child,
                    repository_id=repo.id,
                    ref=branch,
                    owner_id=child,
                    owner_role="worker",
                    fence_token=7,
                    handoff_state="reserved",
                    created_at=time.time(),
                    updated_at=time.time(),
                )
            )


async def recycled_slot(incident, tmp_path):
    await reserve_children(incident)
    db, _service, source, _remote, _repo = incident[0]
    child = incident[1][0]
    branch = "aq/" + child
    git(source, "branch", branch, incident[2][child])
    slot = tmp_path / "slot-1"
    git(source, "worktree", "add", "--detach", str(slot), incident[3])
    git(slot, "checkout", "-b", "aq/reused-task")
    (slot / "unpublished-other-task.txt").write_text("unrelated work\n")
    git(slot, "add", ".")
    git(slot, "commit", "-m", "other task local work")
    await db.create_task(Task(
        id="reused-task", project_id="p", repo_id="r", title="Next slot user",
        description="", status=TaskStatus.IN_PROGRESS, branch_name="aq/reused-task",
    ))
    await db.create_workspace(Workspace(
        id="base", project_id="p", workspace_path=str(source), source_type=RepoSourceType.CLONE,
    ))
    await db.create_workspace(Workspace(
        id="slot", project_id="p", workspace_path=str(slot), source_type=RepoSourceType.CLONE,
        slot_index=1, base_workspace_id="base",
    ))
    async with db.immediate() as conn:
        await conn.execute(update(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner-" + child,
        ).values(confirmed_workspace_id="slot"))
    return slot, branch


async def historical_collector(incident, *, state="cancelled"):
    db, _service, source, _remote, repo = incident[0]
    async with db.immediate() as conn:
        await conn.execute(insert(t.integration_parent_episodes).values(
            id="old-episode", parent_task_id=PARENT, repository_id=repo.id,
            generation=0, pre_collection_checkpoint_sha=incident[3], created_at=1.0,
        ))
        await conn.execute(insert(t.integration_repair_operations).values(
            id="old-collection", target_kind="parent", parent_task_id=PARENT,
            episode_id="old-episode", state=state, policy_snapshot={}, artifact_snapshot={},
            required_check_version="checks-v1", created_at=1.0, updated_at=2.0,
        ))
        await conn.execute(insert(t.workspaces).values(
            id="historical-slot", project_id="p", workspace_path=str(source), created_at=1.0,
        ))
        await conn.execute(update(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner",
        ).values(owner_id="old-collection", owner_role="collector",
                 confirmed_workspace_id="historical-slot"))


@pytest.mark.parametrize("state", ["cancelled", "completed"])
async def test_adoption_retires_quiet_historical_parent_collector(incident, state):
    await historical_collector(incident, state=state)
    db, _service, _source, remote, _repo = incident[0]
    refs = git(remote, "show-ref")
    preview = await adopt(incident, dry_run=True)
    assert preview["ownership"][0]["owner_id"] == "old-collection"
    assert git(remote, "show-ref") == refs
    assert (await db.get_task(PARENT)).status == TaskStatus.PAUSED
    assert (await adopt(incident))["outcome"] == "adopted"
    assert git(remote, "rev-parse", "main") == incident[3]
    async with db._engine.connect() as conn:
        owner = (await conn.execute(select(t.integration_branch_owners))).mappings().one()
        assert (owner["handoff_state"], owner["fence_token"]) == ("released", 5)
        assert await conn.scalar(select(t.integration_repair_operations.c.state).where(
            t.integration_repair_operations.c.id == "old-collection",
        )) == state


@pytest.mark.parametrize("blocker", [
    "active_operation", "other_parent", "wrong_role", "attached", "locked_checkout",
    "dirty_checkout", "unpublished_ref", "missing_workspace", "historical_write",
])
async def test_historical_parent_collector_preserves_writers_and_unpublished_work(incident, blocker):
    await historical_collector(incident)
    db, _service, source, _remote, _repo = incident[0]
    async with db.immediate() as conn:
        if blocker in {"active_operation", "other_parent"}:
            if blocker == "active_operation":
                await conn.execute(update(t.integration_repair_operations).where(
                    t.integration_repair_operations.c.id == "collection",
                ).values(state="cancelled"))
            else:
                await conn.execute(insert(t.integration_parent_episodes).values(
                    id="unrelated-episode", parent_task_id="another-parent", repository_id="r",
                    generation=0, pre_collection_checkpoint_sha=incident[3], created_at=1.0,
                ))
            await conn.execute(update(t.integration_repair_operations).where(
                t.integration_repair_operations.c.id == "old-collection",
            ).values(**({"state": "active"} if blocker == "active_operation"
                        else {"parent_task_id": "another-parent", "episode_id": "unrelated-episode"})))
        elif blocker in {"wrong_role", "attached", "missing_workspace"}:
            field, value = {
                "wrong_role": ("owner_role", "worker"),
                "attached": ("handoff_state", "attached"),
                "missing_workspace": ("confirmed_workspace_id", "missing"),
            }[blocker]
            await conn.execute(update(t.integration_branch_owners).values(**{field: value}))
        elif blocker == "locked_checkout":
            await conn.execute(update(t.workspaces).values(locked_by_task_id=VERIFIER))
        elif blocker == "historical_write":
            await conn.execute(insert(t.integration_promotion_intents).values(
                id="old-write", domain_key="old-write", operation_key="old-collection",
                receipt_id="missing", project_id="p", repository_id="r",
                target_branch="aq/epic/old-aggregate", source_head=incident[3],
                source_base=incident[3], expected_target=incident[3],
                fence_owner_id="old-collection", fence_token=4, state="pushed",
                created_at=1.0, updated_at=2.0,
            ))
    if blocker == "dirty_checkout":
        (source / "uncommitted.txt").write_text("unpublished parent work\n")
    elif blocker == "unpublished_ref":
        git(source, "commit", "--allow-empty", "-m", "unpublished parent work")
    for dry_run in (True, False):
        with pytest.raises(ValueError):
            await adopt(incident, dry_run=dry_run)
    assert await db.get_task_completion(PARENT) is None


@pytest.mark.parametrize("parent", [PARENT, "agile-impact-14", "vivid-quest-44"])
@pytest.mark.parametrize("reused_by_writer", [False, True])
async def test_confirmed_recycled_slot_does_not_retain_delivered_child(
    setup, tmp_path, parent, reused_by_writer,
):
    incident = await build_incident(setup, parent)
    slot, _branch = await recycled_slot(incident, tmp_path)
    db, _service, _source, remote, _repo = setup
    if reused_by_writer:
        async with db.immediate() as conn:
            await conn.execute(update(t.workspaces).where(t.workspaces.c.id == "slot").values(
                locked_by_task_id="reused-task",
            ))
        await db.create_session(SessionRecord(
            id="next-writer", project_id="p", task_id="reused-task", profile_id="worker",
            harness="codex", provider="fake", name="next-writer", lifecycle="pool",
            work_dir=str(slot), epoch="epoch", instance_token="token", started_at=time.time(),
            state="running",
        ))
    # Dirty, unpublished work on the successor's branch must stay independent.
    (slot / "dirty-other-task.txt").write_text("next task still writing\n")
    refs = git(remote, "show-ref")
    head = git(slot, "rev-parse", "HEAD")
    status = git(slot, "status", "--porcelain")
    preview = await adopt(incident, dry_run=True)
    assert "owner-" + incident[1][0] in {row["id"] for row in preview["ownership"]}
    assert (await db.get_task(parent)).status == TaskStatus.PAUSED
    assert git(remote, "show-ref") == refs
    result = await adopt(incident)
    assert result["outcome"] == "adopted"
    assert (await db.get_task(parent)).status == TaskStatus.COMPLETED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.FAILED
    async with db._engine.connect() as conn:
        owner = (await conn.execute(select(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner-" + incident[1][0],
        ))).mappings().one()
    assert owner["handoff_state"] == "released"
    assert owner["fence_token"] == 8
    assert owner["confirmed_workspace_id"] == "slot"
    assert (await db.get_workspace("slot")).locked_by_task_id == (
        "reused-task" if reused_by_writer else None
    )
    after_refs = set(git(remote, "show-ref").splitlines())
    assert set(refs.splitlines()) < after_refs
    assert all(" refs/heads/aq-provenance/completions/" in ref
               for ref in after_refs - set(refs.splitlines()))
    assert git(slot, "rev-parse", "HEAD") == head
    assert git(slot, "status", "--porcelain") == status


@pytest.mark.parametrize("ref_state", ["missing_local", "remote_removed", "clean_checkout"])
async def test_confirmed_slot_accepts_observable_published_or_absent_child_ref(
    incident, tmp_path, ref_state,
):
    slot, branch = await recycled_slot(incident, tmp_path)
    db, _service, source, _remote, _repo = incident[0]
    if ref_state == "missing_local":
        git(source, "branch", "-D", branch)
    elif ref_state == "remote_removed":
        git(source, "push", "origin", "--delete", branch)
    else:
        git(slot, "checkout", branch)
    assert (await adopt(incident, dry_run=True))["outcome"] == "would_adopt_parent"
    assert (await adopt(incident))["outcome"] == "adopted"
    assert (await db.get_task(PARENT)).status == TaskStatus.COMPLETED


@pytest.mark.parametrize("blocker", [
    "child_lock", "other_writer_lock", "live_session", "dirty_child_checkout",
    "unpublished_ref", "diverged_ref", "wrong_repository", "missing_checkout",
    "unknown_ref", "unknown_worktrees", "unknown_dirty", "parent_workspace",
])
async def test_confirmed_slot_cannot_hide_a_writer_or_unpublished_child_work(
    incident, tmp_path, monkeypatch, blocker,
):
    from unittest.mock import AsyncMock

    slot, branch = await recycled_slot(incident, tmp_path)
    db, service, source, remote, _repo = incident[0]
    child = incident[1][0]
    if blocker in {"child_lock", "other_writer_lock"}:
        if blocker == "other_writer_lock":
            git(slot, "checkout", branch)
        async with db.immediate() as conn:
            await conn.execute(update(t.workspaces).where(t.workspaces.c.id == "slot").values(
                locked_by_task_id=child if blocker == "child_lock" else "reused-task",
            ))
    elif blocker == "live_session":
        git(source, "checkout", branch)
        await db.create_session(SessionRecord(
            id="ref-writer", project_id="p", task_id="reused-task", profile_id="worker",
            harness="codex", provider="fake", name="ref-writer", lifecycle="pool",
            work_dir=str(source), epoch="epoch", instance_token="token", started_at=time.time(),
            state="running",
        ))
    elif blocker == "dirty_child_checkout":
        git(source, "checkout", branch)
        (source / "dirty-child.txt").write_text("child work still owed\n")
    elif blocker in {"unpublished_ref", "diverged_ref"}:
        git(source, "checkout", branch)
        if blocker == "diverged_ref":
            git(source, "reset", "--hard", "main~1")
        git(source, "commit", "--allow-empty", "-m", "unpublished child work")
        git(source, "checkout", "aq/epic/old-aggregate")
    elif blocker == "wrong_repository":
        git(source, "remote", "set-url", "origin", str(tmp_path / "wrong.git"))
    elif blocker == "missing_checkout":
        async with db.immediate() as conn:
            await conn.execute(update(t.workspaces).where(t.workspaces.c.id == "slot").values(
                workspace_path=str(tmp_path / "missing"),
            ))
    elif blocker == "unknown_ref":
        monkeypatch.setattr(service.git, "aref_exists", AsyncMock(return_value=None))
    elif blocker == "unknown_worktrees":
        from src.git.manager import GitError

        monkeypatch.setattr(service.git, "aworktree_list", AsyncMock(side_effect=GitError("probe")))
    elif blocker == "unknown_dirty":
        git(slot, "checkout", branch)
        monkeypatch.setattr(service.git, "aget_dirty_paths", AsyncMock(return_value=None))
    else:
        async with db.immediate() as conn:
            await conn.execute(update(t.integration_branch_owners).where(
                t.integration_branch_owners.c.id == "owner",
            ).values(confirmed_workspace_id="slot"))
    refs = git(remote, "show-ref")
    for dry_run in (True, False):
        with pytest.raises(ValueError):
            await adopt(incident, dry_run=dry_run)
    assert git(remote, "show-ref") == refs
    async with db._engine.connect() as conn:
        owner = (await conn.execute(select(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner-" + child,
        ))).mappings().one()
    assert owner["fence_token"] == 7
    assert owner["handoff_state"] == "reserved"
    assert await db.get_task_completion(PARENT) is None
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY


@pytest.mark.parametrize("change", [
    "workspace_lock", "local_ref", "checkout_writer", "dirty_checkout",
])
async def test_confirmed_slot_is_rechecked_after_delivery_proof(
    incident, tmp_path, monkeypatch, change,
):
    from src.integration.delivered_parent_adoption import DeliveredParentAdoption

    slot, branch = await recycled_slot(incident, tmp_path)
    db, _service, source, _remote, _repo = incident[0]
    prove = DeliveredParentAdoption.prove

    async def changed_slot(self, facts, truth, **kwargs):
        result = await prove(self, facts, truth, **kwargs)
        if change == "workspace_lock":
            async with db.immediate() as conn:
                await conn.execute(update(t.workspaces).where(t.workspaces.c.id == "slot").values(
                    locked_by_task_id=incident[1][0],
                ))
        elif change == "local_ref":
            git(source, "checkout", branch)
            git(source, "commit", "--allow-empty", "-m", "new unpublished child commit")
            git(source, "checkout", "aq/epic/old-aggregate")
        elif change == "dirty_checkout":
            git(slot, "checkout", branch)
            (slot / "new-child-work.txt").write_text("uncommitted child work after proof\n")
        else:
            git(slot, "checkout", branch)
            await db.create_session(SessionRecord(
                id="new-writer", project_id="p", task_id="reused-task", profile_id="worker",
                harness="codex", provider="fake", name="new-writer", lifecycle="pool",
                work_dir=str(slot), epoch="epoch", instance_token="token", started_at=time.time(),
                state="running",
            ))
        return result

    monkeypatch.setattr(DeliveredParentAdoption, "prove", changed_slot)
    with pytest.raises(ValueError):
        await adopt(incident)
    async with db._engine.connect() as conn:
        owner = (await conn.execute(select(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner-" + incident[1][0],
        ))).mappings().one()
    assert owner["handoff_state"] == "reserved"
    assert owner["fence_token"] == 7
    assert await db.get_task_completion(PARENT) is None
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY


@pytest.mark.parametrize("parent", [PARENT, "agile-impact-14", "vivid-quest-44"])
async def test_settlement_retires_proven_writerless_child_reservations(setup, parent):
    incident = await build_incident(setup, parent)
    await reserve_children(incident)
    db, _service, _source, remote, _repo = setup
    refs = git(remote, "show-ref")
    preview = await adopt(incident, dry_run=True)
    owners = preview["ownership"]
    assert {row["owner_id"] for row in owners} == {VERIFIER, *incident[1]}
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(t.integration_branch_owners))).mappings().all()
    assert all(row["handoff_state"] == "reserved" for row in rows)
    assert git(remote, "show-ref") == refs
    result = await adopt(incident)
    assert result["outcome"] == "adopted"
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(t.integration_branch_owners))).mappings().all()
        audit = json.loads(await conn.scalar(select(t.events.c.payload).where(
            t.events.c.event_type == "integration.parent_adopted"
        )))
    assert all(row["handoff_state"] == "released" for row in rows)
    assert {row["owner_id"]: row["fence_token"] for row in rows} == {
        VERIFIER: 5, **dict.fromkeys(incident[1], 8)
    }
    assert audit["ownership"] == owners
    assert (await db.get_task(parent)).status == TaskStatus.COMPLETED
    for child in incident[1]:
        assert (await db.get_task(child)).status == TaskStatus.COMPLETED


@pytest.mark.parametrize("blocker", [
    "attached", "session", "workspace", "confirmed_workspace", "wrong_branch",
    "wrong_role", "wrong_repository", "other_owner", "default_branch", "undelivered",
    "pending_write",
])
async def test_child_reservations_cannot_waive_writer_or_delivery_proof(incident, blocker):
    await reserve_children(incident)
    setup, children, heads, _main = incident
    db, _service, source, remote, _repo = setup
    child = children[0]
    changes = {
        "attached": {"handoff_state": "attached"},
        "session": {"session_id": "retained"},
        "workspace": {"workspace_id": "retained"},
        "confirmed_workspace": {"confirmed_workspace_id": "retained"},
        "wrong_branch": {"ref": "aq/unrelated"},
        "wrong_role": {"owner_role": "repair"},
        "wrong_repository": {"repository_id": "another-repo"},
        "other_owner": {"owner_id": "another-task"},
        "default_branch": {"ref": "main"},
    }
    if blocker in changes:
        async with db.immediate() as conn:
            await conn.execute(update(t.integration_branch_owners).where(
                t.integration_branch_owners.c.id == "owner-" + child
            ).values(**changes[blocker]))
    elif blocker == "undelivered":
        git(source, "checkout", child)
        git(source, "commit", "--allow-empty", "-m", "undelivered generation")
        await complete_source(setup, child, "undelivered", git(source, "rev-parse", "HEAD"))
    else:
        async with db.immediate() as conn:
            await conn.execute(insert(t.integration_promotion_intents).values(
                id="pending-child-write", domain_key="child-write", operation_key="child-write",
                receipt_id="pending-child-receipt", project_id="p", repository_id="r",
                target_branch="refs/heads/aq/" + child, source_head=heads[child],
                source_base=heads[child], expected_target=heads[child],
                fence_owner_id=child, fence_token=7, state="prepared",
                created_at=time.time(), updated_at=time.time(),
            ))
    refs = git(remote, "show-ref")
    for dry_run in (True, False):
        with pytest.raises(ValueError):
            await adopt(incident, dry_run=dry_run)
    assert git(remote, "show-ref") == refs
    async with db._engine.connect() as conn:
        owner = (await conn.execute(select(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner-" + child
        ))).mappings().one()
    assert owner["fence_token"] == 7
    assert owner["handoff_state"] != "released"
    assert await db.get_task_completion(PARENT) is None


async def test_child_reservation_rechecks_the_fence_after_git_proof(incident, monkeypatch):
    from src.integration.delivered_parent_adoption import DeliveredParentAdoption

    await reserve_children(incident)
    db, *_ = incident[0]
    prove = DeliveredParentAdoption.prove

    async def moved_fence(self, facts, truth, **kwargs):
        result = await prove(self, facts, truth, **kwargs)
        async with db.immediate() as conn:
            await conn.execute(update(t.integration_branch_owners).where(
                t.integration_branch_owners.c.id == "owner-" + incident[1][0]
            ).values(fence_token=8))
        return result

    monkeypatch.setattr(DeliveredParentAdoption, "prove", moved_fence)
    with pytest.raises(ValueError, match="generation changed"):
        await adopt(incident)
    assert await db.get_task_completion(PARENT) is None
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY


async def test_incident_dry_run_and_apply_retire_stale_verifier_without_ci(incident):
    setup, children, heads, main = incident
    db, service, _source, remote, _repo = setup
    await db.set_task_meta(
        VERIFIER, "needs_attention", {"reason": "frontier_origin_not_materialized"}
    )
    with pytest.raises(ValueError, match="current verified parent completion"):
        await adopt(incident, settle_delivered_children=False, accept_equivalent=True)
    refs = git(remote, "show-ref")
    assert (await service.delivery_observer.observe([PARENT])).get(
        PARENT
    ).state == DeliveryState.UNKNOWN
    preview = await adopt(incident, dry_run=True)
    assert preview["outcome"] == "would_adopt_parent"
    assert preview["retire_delegates"] == [VERIFIER]
    assert {proof["task_id"]: proof["source_sha"] for proof in preview["children"]} == heads
    assert git(remote, "show-ref") == refs
    assert (await db.get_task(PARENT)).status == TaskStatus.PAUSED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    result = await adopt(incident)
    assert result["outcome"] == "adopted"
    assert (await db.get_task(PARENT)).status == TaskStatus.COMPLETED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.FAILED
    completion = await db.get_task_completion(PARENT)
    assert completion.commits == [main]
    assert "not CI attested" in completion.verification
    proof = (await service.delivery_observer.observe([PARENT])).get(PARENT)
    assert proof.state == DeliveryState.CONTAINED
    assert proof.source_oid == main
    assert proof.request.parent_completion is None
    assert proof.request.parent_adoption.completion_id == completion.id
    from src.integration.epic_delivery import EpicDeliveryProjection

    assert (await EpicDeliveryProjection(db).for_tasks([PARENT]))[PARENT]["state"] == "delivered"
    async with db._engine.connect() as conn:
        operation = (await conn.execute(select(t.integration_repair_operations))).mappings().one()
        assert operation["state"] == "cancelled"
        assert operation["verifier_task_id"] == VERIFIER
        owner = (await conn.execute(select(t.integration_branch_owners))).mappings().one()
        assert (owner["handoff_state"], owner["fence_token"]) == ("released", 5)
        checkpoint = (await conn.execute(select(t.task_integration_checkpoints))).mappings().one()
        assert checkpoint["version"] == 10
        assert checkpoint["verified_sha"] is None
        for table in (
            t.integration_check_evidence,
            t.integration_parent_verifications,
            t.integration_parent_operation_completions,
        ):
            assert (await conn.execute(select(table))).all() == []
        releases = (await conn.execute(select(t.integration_delegate_releases))).mappings().all()
        assert [(row["task_id"], row["disposition"]) for row in releases] == [
            (VERIFIER, "cancelled")
        ]
        audit = json.loads(
            await conn.scalar(
                select(t.events.c.payload).where(
                    t.events.c.event_type == "integration.parent_adopted",
                )
            )
        )
        assert audit["head_sha"] == main
        assert len(audit["children"]) == len(children)
        assert audit["checkpoint_sha"] != main
    assert (await service.rows("p"))[-1]["evidence"]["conclusion"] == "not_ci_attested"


@pytest.mark.parametrize(
    "blocker",
    [
        "open_child",
        "missing_provenance",
        "parent_hold",
        "verifier_hold",
        "attached_owner",
        "assigned_verifier",
        "moved_target",
        "wrong_target",
        "pending_child",
        "settled_child",
    ],
)
async def test_child_adoption_refuses_incomplete_or_unsafe_evidence(incident, blocker):
    setup, children, heads, main = incident
    db, service, source, remote, _repo = setup
    overrides = {"accept_equivalent": True}
    if blocker == "open_child":
        await db.update_task(children[0], status=TaskStatus.READY)
    elif blocker == "missing_provenance":
        await db.save_task_completion(
            TaskCompletion(
                id="unretained",
                task_id=children[0],
                outcome="pass",
                commits=[heads[children[0]]],
                completed_at=time.time(),
            )
        )
    elif blocker in {"parent_hold", "verifier_hold"}:
        await db.set_task_meta(
            PARENT if blocker == "parent_hold" else VERIFIER,
            "manual_pause",
            {"reason": "human decision"},
        )
    elif blocker == "attached_owner":
        async with db.immediate() as conn:
            await conn.execute(update(t.integration_branch_owners).values(handoff_state="attached"))
    elif blocker == "assigned_verifier":
        await db.update_task(VERIFIER, status=TaskStatus.IN_PROGRESS)
    elif blocker == "moved_target":
        overrides["head_sha"] = heads[children[0]]
    elif blocker == "wrong_target":
        overrides["target_ref"] = "refs/heads/" + children[0]
        overrides["head_sha"] = heads[children[0]]
    else:
        git(source, "checkout", children[0])
        (source / "undelivered.txt").write_text("new required work\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "undelivered child generation")
        head = git(source, "rev-parse", "HEAD")
        await complete_source(setup, children[0], "new-child-generation", head)
        if blocker == "settled_child":
            await db.set_task_meta(
                children[0],
                "development_delivery_settlement",
                {
                    "repository_id": "r",
                    "target_ref": "refs/heads/main",
                    "completion_id": "new-child-generation",
                    "reason": "not owed",
                },
            )
    before = git(remote, "show-ref")
    operations = await service.rows("p")
    with pytest.raises(ValueError):
        await adopt(incident, **overrides)
    assert git(remote, "show-ref") == before
    assert (await db.get_task(PARENT)).status == TaskStatus.PAUSED
    assert await db.get_task_completion(PARENT) is None
    assert (await service.rows("p")) == operations


@pytest.mark.parametrize("change", ["child_reclose", "new_child"])
async def test_adoption_rechecks_child_set_and_generations_after_git_observation(
    incident,
    monkeypatch,
    change,
):
    from src.integration.delivered_parent_adoption import DeliveredParentAdoption

    setup, children, heads, _main = incident
    db, _service, *_ = setup
    prove = DeliveredParentAdoption.prove

    async def change_after_proof(self, facts, truth, **kwargs):
        result = await prove(self, facts, truth, **kwargs)
        if change == "child_reclose":
            await complete_source(setup, children[0], "concurrent-close", heads[children[0]])
        else:
            await db.create_task(
                Task(
                    id="new-child",
                    project_id="p",
                    title="new",
                    description="",
                    parent_task_id=PARENT,
                )
            )
        return result

    monkeypatch.setattr(DeliveredParentAdoption, "prove", change_after_proof)
    with pytest.raises(ValueError):
        await adopt(incident)
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    assert await db.get_task_completion(PARENT) is None


@pytest.mark.parametrize("change", ["checkpoint", "reopen"])
async def test_parent_adoption_cannot_be_borrowed_after_binding_changes(incident, change):
    setup, _children, _heads, _main = incident
    db, service, *_ = setup
    await adopt(incident)
    if change == "checkpoint":
        async with db.immediate() as conn:
            await conn.execute(update(t.task_integration_checkpoints).values(version=11))
    else:
        await db.transition_task(PARENT, TaskStatus.READY, context="reopen for new work")
        # Even a bookkeeping reclose cannot reuse the old operator generation.
        async with db.immediate() as conn:
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == PARENT).values(status="COMPLETED")
            )
    proof = (await service.delivery_observer.observe([PARENT])).get(PARENT)
    assert proof.state == DeliveryState.UNKNOWN
    assert proof.reason == "invalid_parent_completion"


async def test_doctor_names_delivered_children_and_stale_verifier(incident):
    setup, _children, _heads, main = incident
    db, _service, *_ = setup
    result = await run_check(db, "integration.delivered_children_unsettled_parent")
    assert result.severity == Severity.WARN
    assert result.data["count"] == 1
    parent = result.data["parents"][0]
    assert parent["state"] == "delivered_children_unsettled_parent"
    assert parent["verifier_task_id"] == VERIFIER
    assert parent["head_sha"] == main
    assert "--settle-delivered-children --dry-run" in parent["command"]
    await adopt(incident)
    assert (
        await run_check(db, "integration.delivered_children_unsettled_parent")
    ).severity == Severity.OK


@pytest.mark.parametrize("proof_kind", ["equivalent", "no_artifact"])
async def test_child_proof_accepts_retained_equivalence_and_explicit_no_artifact(
    incident, proof_kind
):
    setup, children, _heads, main = incident
    db, service, source, _remote, repo = setup
    child, generation = children[0], "replacement-generation"
    git(source, "checkout", child)
    git(source, "commit", "--allow-empty", "-m", "child close reached main by an equivalent route")
    head = git(source, "rev-parse", "HEAD")
    binding = CompletedSource(CompletionIdentity("p", repo.id, child, generation), head)
    provenance = GitProvenance(service.git, str(source), repository_url=repo.url)
    await db.save_task_completion(
        TaskCompletion(
            id=generation,
            task_id=child,
            outcome="pass",
            commits=[head],
            completed_at=time.time(),
        )
    )
    await provenance.write_completion(binding, artifact=proof_kind != "no_artifact")
    if proof_kind == "equivalent":
        await provenance.write_replacement(
            source_oid=main,
            base_oid=git(source, "merge-base", head, main),
            replaces=[binding],
            authority="operator",
            reason="equivalent child already on main",
        )
    if proof_kind == "equivalent":
        with pytest.raises(
            ValueError, match=f"{child}: equivalent child delivery requires --accept-equivalent"
        ):
            await adopt(incident, dry_run=True)
        with pytest.raises(ValueError, match="--accept-equivalent"):
            await adopt(incident)
        assert await db.get_task_completion(PARENT) is None
    result = await adopt(incident, accept_equivalent=proof_kind == "equivalent")
    proof = next(proof for proof in result["children"] if proof["task_id"] == child)
    assert proof["state"] == ("contained" if proof_kind == "equivalent" else "no_artifact")
    assert proof["source_sha"] == head


@pytest.mark.parametrize(
    "holder", ["session", "retained_claim", "workspace", "gate", "human_operation", "hold_label"]
)
async def test_recovery_preserves_live_holders_and_human_decisions(incident, holder):
    setup, *_ = incident
    db, _service, *_ = setup
    if holder in {"session", "retained_claim"}:
        await db.create_session(
            SessionRecord(
                id="holder",
                project_id="p",
                task_id=VERIFIER,
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="holder",
                lifecycle="pool",
                work_dir="/tmp/holder",
                epoch="epoch",
                instance_token="token",
                started_at=time.time(),
                state="running" if holder == "session" else "stopped",
                desired_state="running" if holder == "session" else "stopped",
            )
        )
        if holder == "retained_claim":
            async with db.immediate() as conn:
                await conn.execute(update(t.sessions).values(claim_phase="claimed"))
    else:
        async with db.immediate() as conn:
            if holder == "workspace":
                await conn.execute(
                    insert(t.workspaces).values(
                        id="held-workspace",
                        project_id="p",
                        workspace_path="/tmp/held",
                        locked_by_task_id=VERIFIER,
                        created_at=time.time(),
                    )
                )
            elif holder == "gate":
                await conn.execute(
                    insert(t.gates).values(
                        id="decision",
                        project_id="p",
                        gate_type="human",
                        title="Keep verifier",
                        status="open",
                        created_at=time.time(),
                    )
                )
                await conn.execute(
                    insert(t.task_gates).values(task_id=VERIFIER, gate_id="decision")
                )
            elif holder == "hold_label":
                await conn.execute(
                    insert(t.task_labels).values(task_id=PARENT, label="hold:decision")
                )
            else:
                await conn.execute(
                    update(t.integration_repair_operations).values(state="human_required")
                )
    with pytest.raises(ValueError):
        await adopt(incident)
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    assert await db.get_task_completion(PARENT) is None


async def test_recovery_cannot_bypass_reconciler_ownership(incident):
    from src.integration.parent_subjects import ParentSubjectAdapter
    from src.integration.subjects import PolicyArtifactPin
    from tests.test_integration_parent_completion import _artifact

    setup, *_ = incident
    db, _service, *_ = setup
    artifact = _artifact()
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
        subject, _created = await ParentSubjectAdapter(db).ensure_on(
            conn,
            PARENT,
            policy=PolicyArtifactPin(
                playbook_id=artifact.playbook_id,
                artifact_sha256=artifact.artifact_sha256,
            ),
            max_wait_seconds=300,
        )
        await conn.execute(
            update(t.integration_subjects)
            .where(
                t.integration_subjects.c.id == subject.id,
            )
            .values(engine="reconciler")
        )
    result = await adopt(incident)
    assert result["outcome"] == "blocked"
    assert "reconciler" in result["reason"]
    assert (await db.get_task(PARENT)).status == TaskStatus.PAUSED
    assert await db.get_task_completion(PARENT) is None


async def test_recovery_refuses_unresolved_remote_write(incident):
    setup, _children, _heads, main = incident
    db, _service, *_ = setup
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.integration_promotion_intents).values(
                id="uncertain",
                domain_key="uncertain",
                operation_key="collection",
                receipt_id="unwritten",
                project_id="p",
                repository_id="r",
                target_branch="aq/epic/old-aggregate",
                source_head=main,
                source_base=main,
                expected_target=main,
                fence_owner_id="collection",
                fence_token=4,
                state="pushed",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
    with pytest.raises(ValueError, match="unresolved external write"):
        await adopt(incident)
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    assert await db.get_task_completion(PARENT) is None


async def test_live_incident_refuses_and_names_undelivered_fourth_child(incident):
    setup, children, _heads, _main = incident
    db, _service, source, remote, _repo = setup
    child = children[3]
    git(source, "checkout", child)
    (source / "phase-four.txt").write_text("fourth child is still owed\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "undelivered fourth child")
    await complete_source(setup, child, "live-fourth-generation", git(source, "rev-parse", "HEAD"))
    refs = git(remote, "show-ref")
    for dry_run in (True, False):
        with pytest.raises(ValueError, match=f"{child}: child delivery is pending"):
            await adopt(incident, dry_run=dry_run, accept_equivalent=True)
    assert git(remote, "show-ref") == refs
    assert (await db.get_task(PARENT)).status == TaskStatus.PAUSED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY
    assert await db.get_task_completion(PARENT) is None
