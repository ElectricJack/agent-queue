"""Git witnesses survive squashes/GC; projection repair is bounded and fenced."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select, update

from src.database import tables as t
from src.git.manager import RemoteRefState
from src.integration.delivery_observer import source_bases_on
from src.integration.delivery_truth import DeliveryRequest, DeliveryState
from src.integration.git_truth import GitDeliveryEvidence
from src.integration.truth_reconciler import GitTruthReconciler
from src.integration.truth_retention import retain_delivery
from src.models import Project, RepoConfig, RepoSourceType
from src.models import WorktreeSentinel
from src.orchestrator.worktree_manager import WorktreeSlotManager
from tests.test_integration_git_truth import repository as repository


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("truth-reconciler")
    await database.create_project(Project(id="p", name="P"))
    yield database


async def seed(
    db, *, count=1, origins=1, owners=1, checkpoint=True,
    branch=None, base="b" * 40, materialized=True,
):
    async with db.immediate() as conn:
        await conn.execute(insert(t.tasks), [{
            "id": f"task-{i:04}", "project_id": "p", "title": "Work", "description": "",
            "status": "COMPLETED", "branch_name": f"aq/task-{i:04}",
            "created_at": 1.0, "updated_at": 2.0,
        } for i in range(count)])
        if origins:
            await conn.execute(insert(t.task_branch_origins), [{
                "id": f"origin-{i:04}", "task_id": f"task-{i:04}", "repository_id": "r",
                "branch_name": branch or f"aq/task-{i:04}", "base_sha": base,
                "creation_generation": 0, "reserved": True, "materialized": materialized,
                "created_at": 1.0,
            } for i in range(origins)])
        if owners:
            await conn.execute(insert(t.integration_branch_owners), [{
                "id": f"owner-{i:04}", "repository_id": "r", "ref": f"aq/task-{i:04}",
                "owner_id": f"task-{i:04}", "owner_role": "worker", "fence_token": 3,
                "handoff_state": "reserved", "created_at": 1.0, "updated_at": 2.0,
            } for i in range(owners)])
        if checkpoint:
            await conn.execute(insert(t.task_integration_checkpoints), [{
                "task_id": f"task-{i:04}", "repository_id": "r", "branch": f"aq/task-{i:04}",
                "checkpoint_sha": "a" * 40, "state": "working", "updated_at": 2.0,
            } for i in range(count)])


def view_for(ids, *, state=DeliveryState.CONTAINED):
    evidence = {tid: GitDeliveryEvidence(
        DeliveryRequest("p", "r", "refs/heads/main", tid, 2.0, "legacy",
                        branch_name="aq/" + tid),
        state, "c" * 40, "a" * 40, "ancestor", source_base="b" * 40,
    ) for tid in ids}
    snapshot = SimpleNamespace(
        project_id="p", repository_id="r", target_ref="refs/heads/main",
        repository_url="remote", store="store",
        target_oid="c" * 40, source_heads={
            "refs/remotes/origin/aq/" + tid: "a" * 40 for tid in ids
        },
    )
    snapshot.git = SimpleNamespace(als_remote_refs=AsyncMock(
        side_effect=lambda store, branches, **kw: {branch: SimpleNamespace(
            state=RemoteRefState.PRESENT, oid=snapshot.source_heads[
                "refs/remotes/origin/" + branch],
        ) for branch in branches},
    ))
    return SimpleNamespace(
        get=evidence.get, snapshots=[snapshot], fresh=AsyncMock(return_value=True),
        verified_on=AsyncMock(side_effect=lambda conn, names:
                              {name: evidence[name] for name in names}),
    )


def reconciler(db, tmp_path, monkeypatch, *, page_size=50, state=DeliveryState.CONTAINED):
    service = GitTruthReconciler(db, SimpleNamespace(), data_dir=tmp_path, page_size=page_size)
    service.observer.observe = AsyncMock(side_effect=lambda ids, **kw: view_for(ids, state=state))
    monkeypatch.setattr("src.integration.truth_reconciler.retain_delivery",
                        AsyncMock(return_value={"ref": "refs/aq/provenance/delivery/proof"}))
    return service


async def test_retire_all_three_retrospective_populations_without_starvation(
    db, tmp_path, monkeypatch,
):
    # The actual retrospective populations, not a single convenient stale row.
    await seed(db, count=1274, origins=597, owners=326)
    service = reconciler(db, tmp_path, monkeypatch, page_size=100)
    for _ in range(13):
        await service.tick(100.0)
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(
            t.task_integration_checkpoints)) == 0
        assert await conn.scalar(select(func.count()).select_from(t.task_branch_origins).where(
            t.task_branch_origins.c.retired_at.is_(None))) == 0
        assert await conn.scalar(select(func.count()).select_from(t.integration_branch_owners).where(
            t.integration_branch_owners.c.handoff_state != "released")) == 0
        assert await conn.scalar(select(func.count()).select_from(t.task_delivery_receipts)) == 1274
        assert await source_bases_on(conn, ["task-0000"], "r") == {"task-0000": "b" * 40}
    again = await service.tick(101.0)
    assert again["repaired"] == 0


@pytest.mark.parametrize("state", [DeliveryState.UNKNOWN, DeliveryState.PENDING])
async def test_unknown_or_unlanded_work_keeps_every_projection(db, tmp_path, monkeypatch, state):
    await seed(db)
    service = reconciler(db, tmp_path, monkeypatch, state=state)
    result = await service.tick(100.0)
    assert result["repaired"] == 0
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(t.task_branch_origins.c.retired_at)) is None
        assert await conn.scalar(select(t.task_integration_checkpoints.c.state)) == "working"
        assert await conn.scalar(select(t.integration_branch_owners.c.handoff_state)) == "reserved"


@pytest.mark.parametrize("race", ["reopen", "identity", "remote", "retention"])
async def test_changed_identity_or_unconfirmed_proof_prevents_retirement(
    db, tmp_path, monkeypatch, race,
):
    await seed(db)
    service = reconciler(db, tmp_path, monkeypatch)
    view = view_for(["task-0000"])
    if race == "reopen":
        async with db.immediate() as conn:
            await conn.execute(update(t.tasks).values(status="READY"))
    elif race == "identity":
        view.verified_on.return_value = {}
        view.verified_on.side_effect = None
    elif race == "remote":
        view.fresh.return_value = False
    else:
        monkeypatch.setattr("src.integration.truth_reconciler.retain_delivery",
                            AsyncMock(side_effect=RuntimeError("publication failed")))
    if race == "retention":
        with pytest.raises(RuntimeError, match="publication"):
            await service._repair(view, "task-0000", 100.0)
    else:
        assert not await service._repair(view, "task-0000", 100.0)
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(t.task_integration_checkpoints.c.task_id)) == "task-0000"
        assert await conn.scalar(select(t.task_branch_origins.c.retired_at)) is None


async def test_live_lease_and_later_branch_tip_are_not_released(db, tmp_path, monkeypatch):
    await seed(db, count=2, origins=2, owners=2)
    service = reconciler(db, tmp_path, monkeypatch)
    async with db.immediate() as conn:
        await conn.execute(update(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner-0000",
        ).values(holder="publisher", fence=10, expires_at=1000.0))
    view = view_for(["task-0000", "task-0001"])
    view.snapshots[0].source_heads["refs/remotes/origin/aq/task-0001"] = "d" * 40
    await service._repair(view, "task-0000", 100.0)
    await service._repair(view, "task-0001", 100.0)
    async with db._engine.connect() as conn:
        assert list((await conn.execute(select(
            t.integration_branch_owners.c.handoff_state,
        ))).scalars()) == ["reserved", "reserved"]


async def test_refused_owner_page_does_not_starve_later_attached_owners(db, tmp_path):
    await seed(db, count=4, origins=0, owners=4, checkpoint=False)
    async with db.immediate() as conn:
        await conn.execute(update(t.integration_branch_owners).values(handoff_state="attached"))
    recovery = SimpleNamespace(recover_many=AsyncMock(return_value=[]))
    service = GitTruthReconciler(
        db, SimpleNamespace(), data_dir=tmp_path, owner_recovery=recovery, page_size=2,
    )
    for _ in range(3):
        await service._owners()
    assert [call.args[0] for call in recovery.recover_many.await_args_list] == [
        ["owner-0000", "owner-0001"], ["owner-0002", "owner-0003"], [],
    ]


async def test_failed_delivery_source_does_not_prevent_other_repairs(db, tmp_path, monkeypatch):
    service = reconciler(db, tmp_path, monkeypatch)
    service._completed = AsyncMock(side_effect=RuntimeError("remote unavailable"))
    service._sentinels = AsyncMock(return_value=1)
    service._origins = AsyncMock(return_value=2)
    result = await service.tick(100.0)
    assert result["sentinels"] == 1
    assert result["origins"] == 2


async def test_reconcile_command_requires_daemon_principal():
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, principal_context

    service = SimpleNamespace(tick=AsyncMock(return_value={"repaired": 1}))
    handler = IntegrationCommandsMixin()
    handler.orchestrator = SimpleNamespace(git_truth_reconciler=service)
    with principal_context(None):
        assert not (await handler._cmd_integration_reconcile_git_truth({}))["success"]
    service.tick.assert_not_awaited()
    with principal_context(ExecutionPrincipal.service("git-truth-reconciler")):
        assert await handler._cmd_integration_reconcile_git_truth({"now": 0.0}) == {
            "success": True, "repaired": 1,
        }
    service.tick.assert_awaited_once_with(0.0)


async def test_real_delivery_remains_provable_after_projection_retirement(
    db, tmp_path, repository,
):
    repo = repository
    source = await repo.commit("completed")
    await repo.retain(source, task="task-0000")
    await repo.run("checkout", "main")
    await repo.run("merge", "--squash", "source")
    await repo.run("commit", "-m", "delivery")
    await repo.publish()
    await db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE,
        url=str(repo.remote), default_branch="main",
    ))
    await seed(db, owners=0, branch="source", base=repo.base)
    async with db.immediate() as conn:
        await conn.execute(update(t.projects).values(integration_repository_id="r"))
        await conn.execute(update(t.tasks).values(branch_name="source", repo_id="r"))
        await conn.execute(update(t.task_integration_checkpoints).values(checkpoint_sha=source))
        await conn.execute(insert(t.task_completion_records).values(
            id="close-1", task_id="task-0000", outcome="pass", branch="source",
            commits=json.dumps([source]), completed_at=2.0,
        ))
    service = GitTruthReconciler(db, repo.git, data_dir=tmp_path)
    assert (await service.tick(100.0))["repaired"] == 1
    assert (await service.tick(101.0))["repaired"] == 0
    view = await service.observer.observe(["task-0000"])
    assert view.get("task-0000").state is DeliveryState.CONTAINED
    async with db._engine.connect() as conn:
        assert "task-0000" in await view.verified_on(conn, ["task-0000"])
        assert await conn.scalar(select(t.task_branch_origins.c.retired_at)) == 100.0
        receipt = (await conn.execute(select(t.task_delivery_receipts))).mappings().one()
        assert receipt["reviewed_head_sha"] == source
        assert receipt["resolution_evidence"]["first_observed_at"] == 100.0


async def test_delivery_witness_survives_squash_branch_deletion_and_gc(repository):
    repo = repository
    source = await repo.commit("work")
    request = await repo.retain(source)
    await repo.run("checkout", "main")
    await repo.run("merge", "--squash", "source")
    await repo.run("commit", "-m", "squashed work")
    await repo.publish()
    snapshot = await repo.snapshot()
    evidence = await snapshot.is_delivered(request, source_base=repo.base)
    assert evidence.state is DeliveryState.CONTAINED
    witness = await retain_delivery(snapshot.observation, evidence)
    assert await retain_delivery(snapshot.observation, evidence) == witness
    # Remove even the completion locator to demonstrate the witness itself
    # retains the source. Its metadata is not used as an ancestry shortcut.
    identity_ref = next(ref for ref in snapshot.observation.source_heads
                        if "/completions/" in ref)
    await repo.run("push", "origin", ":" + identity_ref)
    await repo.git._arun(["gc", "--prune=now"], cwd=str(repo.remote))
    assert await repo.git._arun(["cat-file", "-t", source], cwd=str(repo.remote)) == "commit"
    parents = await repo.git._arun(
        ["show", "-s", "--format=%P", witness["oid"]], cwd=str(repo.remote),
    )
    assert source in parents.split()
    assert evidence.target_oid in parents.split()
    with pytest.raises(ValueError, match="containment"):
        await retain_delivery(snapshot.observation, replace(evidence, state=DeliveryState.UNKNOWN))


@pytest.mark.parametrize("with_checkpoint", [True, False])
async def test_deleted_task_history_is_retained_without_fabricating_delivery(
    db, tmp_path, repository, with_checkpoint,
):
    repo = repository
    source = await repo.commit("orphan")
    await repo.run("push", "origin", "source")
    await db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE,
        url=str(repo.remote), default_branch="main",
    ))
    async with db.immediate() as conn:
        await conn.execute(insert(t.task_branch_origins), [{
            "id": f"origin-{i}", "task_id": f"gone-{i}", "repository_id": "r",
            "branch_name": "source", "base_sha": repo.base,
            "creation_generation": 0, "reserved": True, "materialized": True, "created_at": 1.0,
        } for i in range(25)])
        if with_checkpoint:
            await conn.execute(insert(t.task_integration_checkpoints), [{
                "task_id": f"gone-{i}", "repository_id": "r", "branch": "source",
                "checkpoint_sha": source, "state": "working", "updated_at": 1.0,
            } for i in range(25)])
    service = GitTruthReconciler(db, repo.git, data_dir=tmp_path, page_size=10)
    repair = service._orphans if with_checkpoint else service._origins
    assert [await repair(100.0) for _ in range(3)] == [10, 10, 5]
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(
            t.task_integration_checkpoints)) == 0
        assert await conn.scalar(select(func.count()).select_from(t.task_delivery_receipts)) == 0
        assert await conn.scalar(select(func.count()).select_from(t.task_branch_origins).where(
            t.task_branch_origins.c.retired_at.is_(None))) == 0
    await repo.run("push", "origin", ":source")
    await repo.git._arun(["gc", "--prune=now"], cwd=str(repo.remote))
    assert await repo.git._arun(["cat-file", "-t", source], cwd=str(repo.remote)) == "commit"


@pytest.mark.parametrize("locked", [False, True])
async def test_sentinel_follows_idle_git_checkout_but_preserves_locked_slot(
    db, tmp_path, repository, locked,
):
    repo = repository
    slot = tmp_path / "slot"
    await repo.run("worktree", "add", "-b", "slot-branch", str(slot), "main")
    await seed(db, origins=0, owners=0, checkpoint=False)
    async with db.immediate() as conn:
        await conn.execute(insert(t.workspaces).values(
            id="base", project_id="p", workspace_path=str(repo.path), created_at=1.0,
        ))
        await conn.execute(insert(t.workspaces).values(
            id="slot", project_id="p", workspace_path=str(slot), base_workspace_id="base",
            slot_index=0, source_type="worktree", created_at=1.0,
            locked_by_task_id="task-0000" if locked else None,
        ))
    old = WorktreeSentinel(
        slot="wrong", slot_index=9, base_workspace_id="foreign", project_id="wrong",
        workspace_id="wrong", task_id="old-task", branch="old-branch", setup_hash="setup",
    )
    WorktreeSlotManager.write_sentinel(slot, old)
    (slot / "unsent").write_text("preserve me\n")
    service = GitTruthReconciler(db, repo.git, data_dir=tmp_path)
    assert await service._sentinels(100.0) == (0 if locked else 1)
    actual = WorktreeSlotManager.read_sentinel(slot)
    if locked:
        assert actual == old
    else:
        assert actual.branch == "slot-branch"
        assert actual.task_id is None
        assert actual.workspace_id == "slot"
        assert actual.setup_hash == "setup"
        assert await service._sentinels(101.0) == 0
        WorktreeSlotManager.sentinel_path(slot).write_text('{"slot_index": "invalid"}')
        assert await service._sentinels(102.0) == 1
        assert WorktreeSlotManager.read_sentinel(slot).branch == "slot-branch"
    assert (slot / "unsent").read_text() == "preserve me\n"


@pytest.mark.parametrize("materialized", [False, True])
async def test_origin_materialization_is_derived_from_git_without_changing_filing_base(
    db, tmp_path, repository, materialized,
):
    repo = repository
    source = await repo.commit("work")
    await repo.run("push", "origin", "source")
    await db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(repo.remote),
    ))
    await seed(db, owners=0, checkpoint=False, branch="stale", base=repo.base,
               materialized=materialized)
    async with db.immediate() as conn:
        await conn.execute(update(t.tasks).values(status="READY", branch_name="source"))
    service = GitTruthReconciler(db, repo.git, data_dir=tmp_path)
    assert await service._origins(100.0) == 1
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(t.task_branch_origins).where(
            t.task_branch_origins.c.retired_at.is_(None),
        ))).mappings().one()
        assert row["base_sha"] == repo.base
        assert row["branch_name"] == "source"
        assert row["materialized"] is True
        if materialized:
            old = (await conn.execute(select(t.task_branch_origins).where(
                t.task_branch_origins.c.id == "origin-0000",
            ))).mappings().one()
            assert old["branch_name"] == "stale"
            assert old["base_sha"] == repo.base
            assert old["retired_at"] == 100.0
    assert await service._origins(101.0) == 0
    assert await repo.run("rev-parse", "source") == source
