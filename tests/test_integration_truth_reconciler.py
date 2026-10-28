"""Git witnesses survive squashes/GC; projection repair is bounded and fenced."""

from dataclasses import replace
import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select, update

from src.database import tables as t
from src.git.manager import GitManager, RemoteRefState
from src.integration.delivery_observer import DeliveryTarget, DeliveryView, source_bases_on
from src.integration.delivery_truth import DeliveryRequest, DeliveryState
from src.integration.development import DevelopmentPrimitives
from src.integration.git_truth import GitDeliveryEvidence, snapshot_scope
from src.integration.service import IntegrationService
from src.integration.train import IntegrationTrain, TrainTarget
from src.integration.truth_reconciler import GitTruthReconciler
from src.integration import truth_reconciler as truth_module
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
            "legacy_completion_id": f"legacy:{i:04}",
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


@pytest.mark.parametrize("change", [
    "none", "origin", "checkpoint", "owner", "foreign", "generation", "receipt", "stale",
])
async def test_repair_fast_path_matches_frozen_predecessor(
    db, reuse_database, tmp_path, monkeypatch, change,
):
    tables = (t.tasks, t.task_branch_origins, t.task_integration_checkpoints,
              t.integration_branch_owners, t.task_delivery_receipts)
    namespace = dict(vars(truth_module))
    namespace["retain_delivery"] = AsyncMock(return_value={
        "ref": "refs/aq/provenance/delivery/proof",
    })
    path = Path(__file__).parent / "fixtures/git_truth_repair_before_bright_torrent.py.txt"
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    legacy_db = await reuse_database("truth-reconciler-predecessor")
    await legacy_db.create_project(Project(id="p", name="P"))
    outcomes = []
    for database, repair in ((legacy_db, namespace["_repair"]), (db, GitTruthReconciler._repair)):
        await seed(database, origins=0 if change == "receipt" else 1,
                   owners=0 if change == "receipt" else 1, checkpoint=change != "receipt")
        service = reconciler(database, tmp_path, monkeypatch)
        view = view_for(["task-0000"])
        if change != "receipt":
            assert await service._repair(view, "task-0000", 100.0)
        async with database.immediate() as conn:
            if change in {"origin", "stale"}:
                await conn.execute(update(t.task_branch_origins).values(retired_at=None))
            elif change == "owner":
                await conn.execute(update(t.integration_branch_owners).values(
                    handoff_state="reserved"))
            elif change in {"checkpoint", "foreign"}:
                await conn.execute(insert(t.task_integration_checkpoints).values(
                    task_id="task-0000", repository_id="other" if change == "foreign" else "r",
                    branch="aq/task-0000", checkpoint_sha="a" * 40, state="working", updated_at=2.0,
                ))
        if change == "generation":
            old = view.get("task-0000")
            changed = replace(old, request=replace(old.request, legacy_generation="new-generation"))
            view.get = lambda _: changed
            view.verified_on = AsyncMock(return_value={"task-0000": changed})
        if change == "stale":
            view.fresh.return_value = False
        view.fresh.reset_mock()
        view.verified_on.reset_mock()
        result = await repair(service, view, "task-0000", 101.0)
        async with database._engine.connect() as conn:
            state = [list((await conn.execute(select(table))).mappings()) for table in tables]
        outcomes.append((result, state))
    assert outcomes[0][0] == outcomes[1][0]
    for table, old_rows, new_rows in zip(tables, outcomes[0][1], outcomes[1][1], strict=True):
        assert len(old_rows) == len(new_rows), table.name
        for old_row, new_row in zip(old_rows, new_rows, strict=True):
            for column in table.c:
                assert old_row[column.name] == new_row[column.name], (table.name, column.name)
    if change in {"none", "foreign"}:
        view.fresh.assert_not_awaited()
        view.verified_on.assert_not_awaited()
    else:
        view.fresh.assert_awaited_once()


@pytest.mark.perf
async def test_warm_completed_repair_profile(db, repository, tmp_path, monkeypatch, perf_strict):
    # Real Git freshness reads and repair SQL both use disposable fixtures.
    store, target = str(repository.path), "main"
    git = GitManager()
    remote = await git.aget_remote_url(store)
    observed = await git.als_remote_ref(store, target)
    assert observed.state is RemoteRefState.PRESENT
    ids = [f"task-{i:04}" for i in range(50)]
    await seed(db, count=50)
    service = reconciler(db, tmp_path, monkeypatch)
    view = view_for(ids)
    for task_id in ids:
        assert await service._repair(view, task_id, 100.0)
    namespace = dict(vars(truth_module))
    path = Path(__file__).parent / "fixtures/git_truth_repair_before_bright_torrent.py.txt"
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    fresh_view = DeliveryView(snapshots=(SimpleNamespace(
        error=None, git=git, store=store, repository_url=remote,
        target_ref="refs/heads/" + target, target_oid=observed.oid, _freshness={},
    ),))
    assert await fresh_view.fresh()
    view.fresh = fresh_view.fresh
    git.als_remote_refs = AsyncMock(wraps=git.als_remote_refs)
    results = []
    for name, repair in (("before", namespace["_repair"]), ("after", GitTruthReconciler._repair)):
        git.als_remote_refs.reset_mock()
        started = time.perf_counter()
        repaired = [await repair(service, view, task_id, 101.0) for task_id in ids]
        assert repaired == [False] * 50
        results.append({"path": name, "seconds": time.perf_counter() - started,
                        "fresh_remote_reads": git.als_remote_refs.await_count})
    print(json.dumps({"scope": "50 warm completed repairs; snapshot/other stages excluded",
                      "results": results}))
    assert [row["fresh_remote_reads"] for row in results] == [50, 0]


async def test_reconciliation_stages_reuse_one_repository_snapshot(db, repository, tmp_path):
    repo = repository
    service = GitTruthReconciler(db, repo.git, data_dir=tmp_path)
    service.observer.truth = repo.truth
    service.observer._store = AsyncMock(return_value=repo.path)
    repo.git.afetch_origin = AsyncMock(wraps=repo.git.afetch_origin)
    observations = []

    async def observe(*_args):
        snapshot = await service.observer.snapshot(DeliveryTarget(
            "p", "r", str(repo.remote), "refs/heads/main"))
        observations.append(snapshot.target_oid)
        return 0

    for name in ("_completed", "_owners", "_sentinels", "_orphans", "_origins", "_repairs"):
        setattr(service, name, observe)
    await service.tick(100.0)
    repo.git.afetch_origin.assert_awaited_once()
    assert observations == [repo.base] * 6
    head = await repo.commit("next-reconciliation")
    await repo.publish()
    await service.tick(200.0)
    assert repo.git.afetch_origin.await_count == 2
    assert observations[-6:] == [head] * 6


@pytest.mark.parametrize("budgeted", [False, True])
async def test_reconciler_joins_inflight_tick_snapshot_without_opening_store(
    db, repository, tmp_path, monkeypatch, budgeted,
):
    from src.git.manager import _REQUEST_GIT_READ_DEADLINE, bounded_git_reads

    repo = repository
    service = GitTruthReconciler(db, repo.git, data_dir=tmp_path)
    service.observer.truth = repo.truth
    service.observer.store_path = lambda _repository: repo.path
    service.observer._store = AsyncMock(side_effect=AssertionError("redundant store open"))
    # Binding lookup is independent of the shared-flight timeout. Keep database
    # latency from exhausting the tiny budget before the reader can join.
    monkeypatch.setattr("src.projects.storage_binding.repository_path",
                        AsyncMock(return_value=repo.path))
    entered, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
    expire = asyncio.Event()
    fetch = repo.git.afetch_origin

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await fetch(*args, **kwargs)

    repo.git.afetch_origin = AsyncMock(side_effect=blocked)
    join = repo.truth.tick_snapshot

    async def joining(*args, **kwargs):
        joined.set()
        if budgeted:
            await expire.wait()
            # Expire both the active timer and the shared deadline in this reader's context.
            deadline = asyncio.get_running_loop().time()
            _REQUEST_GIT_READ_DEADLINE.set(deadline)
            [read_timeout] = read_timeouts
            read_timeout.reschedule(deadline)
        return await join(*args, **kwargs)

    repo.truth.tick_snapshot = joining
    target = DeliveryTarget("p", "r", str(repo.remote), "refs/heads/main")
    observe = service.observer.snapshot
    if budgeted:
        # Exercise timeout while joining the fetch, independently of database lookup latency.
        monkeypatch.setattr("src.projects.storage_binding.repository_path",
                            AsyncMock(return_value=repo.path))
        monkeypatch.setattr("src.git.manager.REQUEST_GIT_READ_SECONDS", 60)
        observe = bounded_git_reads(observe)
    with snapshot_scope():
        train = asyncio.create_task(repo.snapshot())
        await entered.wait()
        if budgeted:
            read_timeouts = []
            timeout_at = asyncio.timeout_at

            def capture_read_timeout(deadline):
                timeout = timeout_at(deadline)
                read_timeouts.append(timeout)
                return timeout

            monkeypatch.setattr(asyncio, "timeout_at", capture_read_timeout)
        reconciliation = asyncio.create_task(observe(target))
        try:
            await asyncio.wait_for(joined.wait(), 2)
            assert not reconciliation.done()
            if budgeted:
                # Expire after joining, independently of database lookup latency.
                expire.set()
                unavailable = await asyncio.wait_for(reconciliation, 2)
                assert unavailable.error == "snapshot_unavailable"
                assert not train.done()  # The timed-out reader cannot cancel the acquisition.
        finally:
            release.set()
            first, second = await asyncio.gather(train, reconciliation)
        if not budgeted:
            assert first.target_oid == second.target_oid == repo.base
            assert first.observation.observation_token == second.observation.observation_token
            first.observation._freshness[target.target_ref] = False
            assert not second.observation._freshness
    repo.git.afetch_origin.assert_awaited_once()
    service.observer._store.assert_not_awaited()


@pytest.mark.parametrize("background", [False, True])
async def test_service_dispatch_shares_train_snapshot_with_git_truth_reconciliation(
    db, repository, tmp_path, background,
):
    repo = repository
    configured = RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE,
        url=str(repo.remote), default_branch="main",
    )
    await db.create_repo(configured)
    primitives = DevelopmentPrimitives(db, git=repo.git, data_dir=tmp_path)
    reconciler = GitTruthReconciler(db, repo.git, data_dir=tmp_path)
    delivery_target = DeliveryTarget("p", "r", str(repo.remote), "refs/heads/main")
    repo.git.acreate_checkout = AsyncMock(wraps=repo.git.acreate_checkout)
    store, observed_store = await asyncio.gather(
        primitives.store(configured, fetch=False), reconciler.observer._store(delivery_target),
    )
    repo.git.acreate_checkout.assert_awaited_once()
    assert observed_store == store
    assert reconciler.observer.store_path("r") == store
    target = TrainTarget("p", "r", "refs/heads/main", "root")
    train = IntegrationTrain(
        targets=SimpleNamespace(targets=AsyncMock(return_value=[target])),
        batches=None, lane_for=AsyncMock(), repair=None,
    )
    observations = []

    async def visit(*_args):
        observations.append(await reconciler.observer.truth.snapshot(
            str(store), project_id="p", repository_id="r", repository_url=str(repo.remote),
            target_ref=target.target_ref,
        ))

    train._bounded = visit

    async def reconcile(now):
        # Make the train acquisition complete first, testing sequential reuse.
        await train._lanes[target.key].task
        return await reconciler.tick(now)

    async def inspect(*_args):
        observations.append(await reconciler.observer.snapshot(DeliveryTarget(
            "p", "r", str(repo.remote), target.target_ref)))
        return 0

    for name in ("_completed", "_owners", "_sentinels", "_orphans", "_origins", "_repairs"):
        setattr(reconciler, name, inspect)
    now = [100.0]
    service = IntegrationService(
        db, SimpleNamespace(dispatch_due=AsyncMock()), train=train,
        maintenance={"git truth": reconcile}, clock=lambda: now[0],
    )
    repo.git.afetch_origin = AsyncMock(wraps=repo.git.afetch_origin)
    repo.git.arun_git_result = AsyncMock(wraps=repo.git.arun_git_result)
    reconciler.observer._store = AsyncMock(wraps=reconciler.observer._store)

    def fsck_count():
        return sum("fsck" in call.args[0] for call in repo.git.arun_git_result.await_args_list)

    try:
        await service.tick(100, background=background)
        await service._reconciliation_task
        assert len(observations) == 7
        assert {snapshot.target_oid for snapshot in observations} == {repo.base}
        repo.git.afetch_origin.assert_awaited_once()
        # Reconciliation joins the train's tick snapshot before any store open
        # or observer flight, including when every stage asks sequentially.
        reconciler.observer._store.assert_not_awaited()
        assert fsck_count() == 1
        # A new service dispatch must not reuse the earlier tick's acquisition.
        head = await repo.commit("later-tick")
        await repo.publish()
        now[0] = 200.0
        await service.tick(200, background=background)
        await service._reconciliation_task
        assert repo.git.afetch_origin.await_count == 2
        assert {snapshot.target_oid for snapshot in observations[-7:]} == {head}
        assert fsck_count() == 1
        # Reconciliation outside a train dispatch also retains the verified baseline.
        await reconciler.tick(300)
        reconciler.observer._store.assert_awaited_once()
        assert len(observations) == 20
        assert {snapshot.target_oid for snapshot in observations[-6:]} == {head}
        assert repo.git.afetch_origin.await_count == 2
        assert fsck_count() == 1
    finally:
        await service.stop()


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
        ["owner-0000"], ["owner-0001"], ["owner-0002"], ["owner-0003"],
    ]


async def test_hung_owner_recovery_does_not_starve_later_owners(db, tmp_path):
    # bold-rapids-90.6: the first recoverable row's fetch hung until the stage
    # timeout every pass, so the stalled worker's owner row was never reached.
    await seed(db, count=3, origins=0, owners=3, checkpoint=False)
    async with db.immediate() as conn:
        await conn.execute(update(t.integration_branch_owners).values(handoff_state="attached"))
    recovered = []

    async def recover_many(ids, *, principal):
        if "owner-0000" in ids:
            await asyncio.Event().wait()
        recovered.extend(ids)
        return []

    service = GitTruthReconciler(
        db, SimpleNamespace(), data_dir=tmp_path,
        owner_recovery=SimpleNamespace(recover_many=recover_many), page_size=10,
    )
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await service._owners()
    async with asyncio.timeout(5):
        assert await service._owners() == 2
    assert recovered == ["owner-0001", "owner-0002"]


async def test_failed_delivery_source_does_not_prevent_other_repairs(db, tmp_path, monkeypatch):
    service = reconciler(db, tmp_path, monkeypatch)
    service._completed = AsyncMock(side_effect=RuntimeError("remote unavailable"))
    service._sentinels = AsyncMock(return_value=1)
    service._origins = AsyncMock(return_value=2)
    result = await service.tick(100.0)
    assert result["sentinels"] == 1
    assert result["origins"] == 2


async def test_phase_profile_preserves_failure_rotation_and_cancellation(tmp_path, monkeypatch):
    from src.logging_config import get_correlation_context
    from src.metrics.operation_profile import profile_duration, profile_repository

    service = GitTruthReconciler(SimpleNamespace(), SimpleNamespace(), data_dir=tmp_path)
    logged = []
    monkeypatch.setattr(truth_module.logger, "info", lambda message: logged.append(
        get_correlation_context()["git_truth_timing"],
    ))

    async def fail(now, counts):
        profile_duration("db.query", 0.25)
        profile_repository("p", "r")
        raise RuntimeError("original failure")

    service._completed = fail
    for name in ("owners", "sentinels", "orphans", "origins", "repairs"):
        monkeypatch.setattr(service, "_" + name, AsyncMock(return_value=0))
    assert (await service.tick(1))["origins"] == 0
    phases = logged[0]["phases"]
    assert list(phases) == ["delivery", "owners", "sentinels", "orphans", "origins", "repairs"]
    assert phases["delivery"]["outcome"] == "error"
    assert phases["delivery"]["operations"]["db.query"]["seconds"] == 0.25
    assert phases["delivery"]["repositories"] == [["p", "r"]]
    assert phases["origins"]["operations"] == {}

    service._owners.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.tick(2)
    assert list(logged[1]["phases"]) == ["owners"]
    assert logged[1]["phases"]["owners"]["outcome"] == "cancelled"
    assert not service._lock.locked()


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


@pytest.mark.parametrize("obstacle", [
    None, "manual_hold", "writer", "unlanded", "moved", "epic", "untouched_candidate",
    "deleted_candidate", "deleted_epic", "live_session", "locked_workspace",
])
async def test_delivered_batch_retires_only_idle_proven_repairs(
    db, tmp_path, repository, obstacle,
):
    from src.integration.repair import ordinary_repair_id
    from src.sessions.obsolete import ObsoleteClose

    repo = repository
    source = await repo.commit("delivered")
    await repo.run("push", "origin", "source")
    if obstacle != "unlanded":
        await repo.publish()
    if obstacle == "moved":
        await repo.commit("unpublished-repair")
        await repo.run("push", "origin", "source")
    if obstacle == "epic":
        await repo.run("push", "origin", repo.base + ":refs/heads/epic")
    starting = source
    if obstacle in {"untouched_candidate", "deleted_candidate"}:
        await repo.run("commit", "--allow-empty", "-m", "disposable candidate")
        starting = await repo.run("rev-parse", "HEAD")
        await repo.run("push", "origin", "source")
        if obstacle == "deleted_candidate":
            await repo.run("push", "origin", ":source")
            await repo.git._arun(["gc", "--prune=now"], cwd=str(repo.remote))
    await db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE,
        url=str(repo.remote), default_branch="main",
    ))
    task_id = ordinary_repair_id("delivered-batch", 1)
    original = {"batch_id": "delivered-batch", "attempt": 1, "repository_id": "r",
                "target_ref": "refs/heads/source", "starting_sha": starting}
    async with db.immediate() as conn:
        await conn.execute(insert(t.tasks).values(
            id=task_id, project_id="p", repo_id="r", title="Repair delivered batch",
            description="", status="IN_PROGRESS" if obstacle == "writer" else "READY",
            branch_name="source", created_by_kind="system", created_by_id="delivered-batch",
            dedup_key="repair:delivered-batch:1", created_at=1.0, updated_at=2.0,
        ))
        await conn.execute(insert(t.task_context).values(
            id=task_id, task_id=task_id, type="snippet", label="Repair input",
            content=json.dumps(original), created_at=1.0,
        ))
        await conn.execute(insert(t.tasks).values(
            id="dependent", project_id="p", title="Waiting work", description="",
            status="READY", is_blocked=1, created_at=1.0, updated_at=2.0,
        ))
        await conn.execute(insert(t.task_dependencies).values(
            task_id="dependent", depends_on_task_id=task_id, dep_type="blocks",
        ))
        if obstacle == "manual_hold":
            await conn.execute(insert(t.task_labels).values(task_id=task_id, label="hold:human"))
        if obstacle == "live_session":
            # Keep the task READY and unassigned: only the actual writer row
            # protects this stale task projection from retirement.
            await conn.execute(insert(t.sessions).values(
                id="repair-writer", task_id=task_id, project_id="p", profile_id="worker",
                harness="codex", provider="fake", name="repair-writer", lifecycle="task",
                state="running", desired_state="running", work_dir=str(repo.path),
                epoch="test", instance_token="writer-instance", started_at=1.0,
            ))
        if obstacle == "locked_workspace":
            # No session or assigned agent exists; the retained checkout alone
            # must remain untouched until its normal recovery releases it.
            await conn.execute(insert(t.workspaces).values(
                id="repair-checkout", project_id="p", workspace_path=str(repo.path),
                locked_by_task_id=task_id, locked_at=1.0, created_at=1.0,
            ))
        await conn.execute(insert(t.integration_batches).values(
            id="delivered-batch", project_id="p", repository_id="r",
            target_ref="refs/heads/epic" if obstacle in {"epic", "deleted_epic"}
            else "refs/heads/main",
            request_id="delivered-batch", source_manifest_digest="manifest", base_sha=repo.base,
            integration_branch="source", lifecycle="sealing", final_main_sha=source,
            policy_snapshot={}, artifact_snapshot={}, cleanup_state="pending",
            repair_attempt_count=1, created_at=1.0, updated_at=2.0,
        ))
        await conn.execute(insert(t.integration_batch_members).values(
            batch_id="delivered-batch", ordinal=0, task_id="original", repository_id="r",
            source_sha=source, source_base_sha=repo.base, reviewed_head_sha=source,
            reviewed_tree_sha=await repo.run("rev-parse", source + "^{tree}"), review_evidence={},
        ))
        await conn.execute(update(t.integration_batches).where(
            t.integration_batches.c.id == "delivered-batch",
        ).values(lifecycle="promoted"))
    close = ObsoleteClose(db, release_owner=None, git_manager=repo.git, clock=lambda: 100.0)
    service = GitTruthReconciler(db, repo.git, data_dir=tmp_path, obsolete_close=close)
    expected = obstacle in {None, "epic", "untouched_candidate", "deleted_candidate", "deleted_epic"}
    assert await service._repairs(100.0) == int(expected)
    async with db._engine.connect() as conn:
        status = await conn.scalar(select(t.tasks.c.status).where(t.tasks.c.id == task_id))
        assert (status == "COMPLETED") == expected
        assert bool(await conn.scalar(select(t.tasks.c.is_blocked).where(
            t.tasks.c.id == "dependent",
        ))) != expected
        if expected:
            marker = await conn.scalar(select(t.task_metadata.c.value).where(
                t.task_metadata.c.task_id == task_id, t.task_metadata.c.key == "obsolete",
            ))
            assert "already delivered" in marker
            assert await conn.scalar(select(func.count()).select_from(t.task_completion_records)) == 0
        elif obstacle == "live_session":
            assert await conn.scalar(select(t.sessions.c.state).where(
                t.sessions.c.id == "repair-writer",
            )) == "running"
        elif obstacle == "locked_workspace":
            assert await conn.scalar(select(t.workspaces.c.locked_by_task_id).where(
                t.workspaces.c.id == "repair-checkout",
            )) == task_id
    assert await service._repairs(101.0) == 0
    if obstacle in {"live_session", "locked_workspace"}:
        async with db.immediate() as conn:
            if obstacle == "live_session":
                await conn.execute(update(t.sessions).where(
                    t.sessions.c.id == "repair-writer",
                ).values(state="stopped", desired_state="stopped"))
            else:
                await conn.execute(update(t.workspaces).where(
                    t.workspaces.c.id == "repair-checkout",
                ).values(locked_by_task_id=None, locked_at=None))
        assert await service._repairs(102.0) == 1
