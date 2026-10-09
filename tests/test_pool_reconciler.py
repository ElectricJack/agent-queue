"""_reconcile_pools / _launch_pool_session — spec §11 (fake provider)."""

from __future__ import annotations

import dataclasses
import logging
import os
import time
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import insert

from src.commands.claim_commands import CLAIM_FILE, write_claim_file
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.database.tables import integration_branch_owners
from src.intelligence_classes import IntelligenceClass
from src.models import (
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.sessions.harness_parser import Harness
from tests.assignment_routing_helpers import route_source_for
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"


@pytest.fixture
async def git_first_frontier(orch, db, tmp_path):
    """A completed sibling with retained provenance, but no delivery receipts."""
    from types import SimpleNamespace

    from src.database.tables import task_branch_origins, task_integration_checkpoints
    from src.integration.delivery_observer import DeliveryObserver
    from src.integration.git_truth import GitTruth
    from src.integration.models import BranchKey
    from src.integration.ownership import BranchOwnership
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
    from src.models import RepoConfig, TaskCompletion
    from tests.test_delivery_consumers import Origin, git
    from tests.test_integration_gitops import LocalGit

    origin = Origin(tmp_path)
    base = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", "origin", "main:aq/epic")
    source = origin.work("first")
    # Claim preparation imports the observed stack base into the assigned
    # checkout. These Git-backed frontier tests need actual clone slots even
    # though reset_slot_for_task remains a spy for its argument assertions.
    for workspace in await db.list_workspaces(PROJECT_ID):
        git(tmp_path, "clone", "--quiet", origin.url, workspace.workspace_path)
    transport = LocalGit(tmp_path / "origin.git")
    orch.git = transport
    await db.update_profile("worker", default_class="standard-medium")
    orch.config.worktrees.enabled = False
    orch._worktree_slots = MagicMock(return_value=MagicMock(
        reset_slot_for_task=AsyncMock(return_value="aq/second"),
    ))
    await db.create_repo(RepoConfig(id="repo", project_id=PROJECT_ID,
                                   source_type=RepoSourceType.CLONE, url=origin.url))
    await db.create_task(Task(id="epic", project_id=PROJECT_ID, title="epic", description="",
                              status=TaskStatus.IN_PROGRESS, repo_id="repo", branch_name="aq/epic"))
    for tid in ("first", "second"):
        await ready(db, tid, intelligence_class="standard-medium")
        await db.update_task(tid, repo_id="repo", branch_name=f"aq/{tid}")
        await db.add_dependency(tid, "epic", "parent-child")
        async with db.immediate() as conn:
            await conn.execute(insert(task_branch_origins).values(
                id=f"origin-{tid}", task_id=tid, repository_id="repo", branch_name=f"aq/{tid}",
                parent_task_id="epic", parent_repository_id="repo", parent_ref="aq/epic",
                base_sha=base, creation_generation=0, reserved=True, materialized=True,
                created_at=time.time(),
            ))
            await conn.execute(insert(task_integration_checkpoints).values(
                task_id=tid, repository_id="repo", branch=f"aq/{tid}",
                checkpoint_sha=source if tid == "first" else base, updated_at=time.time(),
            ))
    await db.add_dependency("second", "first")
    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train",
                            integration_repository_id="repo", repo_url=origin.url)
    await db.save_task_completion(TaskCompletion(
        id="close-first", task_id="first", outcome="pass", commits=[source], completed_at=time.time(),
    ))
    await GitProvenance(transport, str(origin.clone), repository_url=origin.url).write_completion(
        CompletedSource(CompletionIdentity(PROJECT_ID, "repo", "first", "close-first"), source),
    )
    await db.transition_task("first", TaskStatus.COMPLETED)
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/second"), "second", "worker"
    )
    observer = DeliveryObserver(db, git=transport, data_dir=tmp_path / "observer",
                                truth=GitTruth(transport))
    db.set_delivery_observer(observer)
    return SimpleNamespace(origin=origin, base=base, source=source, observer=observer, git=git)


async def _deliver_first_by_train(db, env):
    """Run the actual reduced train and managed Git publication on the epic target."""
    from types import SimpleNamespace

    from src.git.github_contracts import GitHubRepositoryBinding
    from src.integration.batches import Batch, BatchMember, BatchService, BatchStore
    from src.integration.git_truth import GitTruth
    from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
    from src.integration.train import (
        BatchSelection,
        CandidateChecks,
        IntegrationTrain,
        TrainLane,
        TrainTarget,
    )
    from src.integration.train_sources import LeasedPublish, _never_trusted

    target = TrainTarget(PROJECT_ID, "repo", "refs/heads/aq/epic", kind="epic")
    batch = Batch("sibling-batch", PROJECT_ID, "repo", target.target_ref)
    members = (BatchMember("first", env.source, env.base),)
    store = BatchStore(db)
    await store.freeze(batch, members, trees={
        "first": env.git(env.origin.clone, "rev-parse", f"{env.source}^{{tree}}"),
    })
    transport = env.observer.git
    repo = RetainedRepository("repo", env.origin.clone, GitHubRepositoryBinding(123, "test/repo"),
                              "main")
    checks = CandidateChecks(AsyncMock(return_value=None))
    service = BatchService(
        store, GitOperations(db, git=transport, repository=AsyncMock(return_value=repo),
                             authority=SubjectGitAuthority(db, trusted_green=_never_trusted)),
        publish=LeasedPublish(db, transport), eligible=AsyncMock(return_value=True),
        gate=checks.gate, require_attestation=False,
    )

    async def snapshot():
        return await GitTruth(transport).snapshot(
            str(env.origin.clone), project_id=PROJECT_ID, repository_id="repo",
            repository_url=env.origin.url, target_ref=target.target_ref,
        )

    train = IntegrationTrain(
        targets=SimpleNamespace(targets=AsyncMock(return_value=[target])),
        batches=SimpleNamespace(open_batch=AsyncMock(return_value=BatchSelection(batch, members)),
                                settle=AsyncMock()),
        lane_for=AsyncMock(return_value=TrainLane(snapshot, service, checks)),
        repair=SimpleNamespace(
            allocate=AsyncMock(side_effect=AssertionError("unexpected repair")),
            settle_green=AsyncMock(),
        ),
    )
    await train.tick()
    await train.drain()
    [visit] = train.status()
    assert visit["state"] == "delivered", visit


@pytest.fixture
def frontier_clock(monkeypatch):
    from types import SimpleNamespace

    from src.integration import delivery_observer

    clock = MagicMock(return_value=100.0)
    # Keep asyncio's real monotonic clock unchanged.
    monkeypatch.setattr(delivery_observer, "time", SimpleNamespace(monotonic=clock))
    return clock


@pytest.mark.parametrize("cache_state", ["cold", "warm", "expired"])
@pytest.mark.parametrize("stacked", [False, True])
async def test_pool_diagnostics_never_fetch_hierarchy_or_stack_prerequisites(
    orch, db, git_first_frontier, frontier_clock, monkeypatch, cache_state, stacked,
):
    from types import SimpleNamespace

    from src.commands.handler import CommandHandler
    from src.integration import stacked_branches
    from src.integration.delivery_observer import hierarchy_frontier_modes

    env = git_first_frontier
    monkeypatch.setattr(stacked_branches, "time", SimpleNamespace(monotonic=frontier_clock))
    if stacked:
        await db.update_project(PROJECT_ID, hierarchical_integration_policy={
            "prerequisite_branches": "stacked",
        })
    if cache_state != "cold":
        await hierarchy_frontier_modes(db)
    if cache_state == "expired":
        env.observer._recent = {
            target: (stamp - env.observer.READ_MAX_AGE - 1, snapshot)
            for target, (stamp, snapshot) in env.observer._recent.items()
        }
        env.observer._stack_ref_cache.clear()
    forbidden = {}
    for name in ("afetch_origin", "acreate_checkout", "als_remote_ref", "als_remote_refs"):
        forbidden[name] = AsyncMock(side_effect=AssertionError(f"diagnostic called {name}"))
        monkeypatch.setattr(env.observer.git, name, forbidden[name])
    handler = CommandHandler(orch, orch.config)
    explained = await handler._cmd_explain_task({"task_id": "second"})
    # A retained sibling may be stacked when its current refs are already observed.
    stackable = stacked and cache_state == "warm"
    unavailable = cache_state != "warm"
    assert ("frontier_sibling_prerequisite_not_delivered" in explained["reason_codes"]) is (
        not stackable and not unavailable
    )
    assert ("delivery_evidence_unavailable" in explained["reason_codes"]) is unavailable
    if unavailable:
        reason = next(reason for reason in explained["reasons"]
                      if reason["code"] == "delivery_evidence_unavailable")
        assert "has not loaded yet" in reason["detail"]
    status = await handler._cmd_pool_status({})
    row = next(row for row in status["pools"] if row["profile_id"] == "worker")
    assert row["ready"] == int(stackable)
    for call in forbidden.values():
        call.assert_not_awaited()


async def test_git_first_delivery_releases_scheduler_pool_and_claim(
    orch, db, git_first_frontier, frontier_clock,
):
    from sqlalchemy import select

    from src.commands.handler import CommandHandler
    from src.database.tables import task_delivery_receipts
    from src.doctor.models import DoctorContext
    from src.doctor.task_checks import _check_ready_frontier_exclusions
    from src.integration.delivery_observer import hierarchy_frontier_modes
    from src.scheduler import PoolKey

    env = git_first_frontier
    handler = CommandHandler(orch, orch.config)

    async def frontier(expected, *, evidence_unavailable=False):
        # These are the real scheduler/reconciler and pool consumers of one tick's view.
        modes = await hierarchy_frontier_modes(db)
        env.observer.prerequisite_view = AsyncMock(wraps=env.observer.prerequisite_view)
        await orch._schedule(hierarchy_modes=modes)
        assert ("second" in orch._last_scheduler_state.hierarchy_runnable_task_ids) is expected
        measurement = await orch._measure_pools(hierarchy_modes=modes)
        assert measurement.demand[PoolKey("worker")] == int(expected)
        env.observer.prerequisite_view.assert_not_awaited()
        assert await db.is_hierarchy_task_runnable("second") is expected
        explained = await handler._cmd_explain_task({"task_id": "second"})
        assert any(r["code"] == "frontier_sibling_prerequisite_not_delivered"
                   for r in explained["reasons"]) is (not expected and not evidence_unavailable)
        assert ("delivery_evidence_unavailable" in explained["reason_codes"]) is evidence_unavailable
        diagnosis = await _check_ready_frontier_exclusions(DoctorContext(config=orch.config, db=db))
        assert any(
            row["task_id"] == "second" and "sibling_prerequisite_not_delivered" in row["reasons"]
            for row in diagnosis.data.get("tasks", [])
        ) is not expected
        return modes

    await frontier(False)
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    assert await db.list_sessions(lifecycle="pool") == []

    await _deliver_first_by_train(db, env)
    # The old advisory observation may withhold newly delivered work until expiry.
    await frontier(False, evidence_unavailable=True)
    frontier_clock.return_value += env.observer.READ_MAX_AGE + 0.01
    modes = await frontier(True)
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(task_delivery_receipts))).first()
    await orch._reconcile_pools(hierarchy_modes=modes)
    await orch.wait_for_pool_launches()
    [session] = await db.list_sessions(lifecycle="pool")
    assert session.state == "running"
    handler._current_scope = {"kind": "session", "session_id": session.id,
                              "project_id": PROJECT_ID, "task_id": None, "elevated": False}
    result = await handler._cmd_task_claim({"next": True})
    assert result["result"] == "claimed", result
    assert result["task"]["id"] == "second"


async def test_stacked_frontier_agrees_for_scheduler_demand_explain_and_claim(
    orch, db, git_first_frontier, frontier_clock, monkeypatch,
):
    from types import SimpleNamespace

    from src.commands.handler import CommandHandler
    from src.integration import stacked_branches
    from src.integration.delivery_observer import hierarchy_frontier_modes
    from src.scheduler import PoolKey

    env = git_first_frontier
    # Explain reads cached stack refs only; a busy runner must not age them out.
    monkeypatch.setattr(stacked_branches, "time", SimpleNamespace(monotonic=frontier_clock))
    handler = CommandHandler(orch, orch.config)
    await db.update_project(PROJECT_ID, hierarchical_integration_policy={
        "prerequisite_branches": "stacked",
    })
    modes = await hierarchy_frontier_modes(db)
    assert modes[PROJECT_ID].stackable_prerequisite_ids == {"first"}
    assert not modes[PROJECT_ID].delivered_prerequisite_ids
    await orch._schedule(hierarchy_modes=modes)
    assert "second" in orch._last_scheduler_state.hierarchy_runnable_task_ids
    assert (await orch._measure_pools(hierarchy_modes=modes)).demand[PoolKey("worker")] == 1
    assert await db.count_ready_by_profile(PROJECT_ID) == {"worker": 1}
    assert await db.is_hierarchy_task_runnable("second")
    explained = await handler._cmd_explain_task({"task_id": "second"})
    assert not any(r["code"] == "frontier_sibling_prerequisite_not_delivered"
                   for r in explained["reasons"])
    await db.update_project(PROJECT_ID, hierarchical_integration_policy={
        "prerequisite_branches": "wait-for-parent",
    })
    assert not await db.is_hierarchy_task_runnable("second")
    await db.update_project(PROJECT_ID, hierarchical_integration_policy={
        "prerequisite_branches": "stacked",
    })
    await orch._reconcile_pools(hierarchy_modes=modes)
    await orch.wait_for_pool_launches()
    [session] = await db.list_sessions(lifecycle="pool")
    handler._current_scope = {"kind": "session", "session_id": session.id,
                              "project_id": PROJECT_ID, "task_id": None, "elevated": False}
    result = await handler._cmd_task_claim({"next": True})
    assert result["result"] == "claimed", result
    assert result["task"]["id"] == "second"
    prepare = orch._worktree_slots().reset_slot_for_task
    assert prepare.await_args.kwargs["base_branch"] == env.source
    assert env.git(env.origin.clone, "rev-parse", "origin/aq/epic") == env.base


async def test_git_first_advisory_cycles_reuse_fetch_until_bound(
    orch, db, git_first_frontier, frontier_clock, monkeypatch,
):
    from src.commands.handler import CommandHandler
    from src.doctor.models import DoctorContext
    from src.doctor.task_checks import _check_ready_frontier_exclusions
    from src.scheduler import PoolKey

    env = git_first_frontier
    await _deliver_first_by_train(db, env)
    fetch = AsyncMock(wraps=env.observer.git.afetch_origin)
    monkeypatch.setattr(env.observer.git, "afetch_origin", fetch)
    handler = CommandHandler(orch, orch.config)

    async def advisory_cycle():
        await orch._schedule()
        assert "second" in orch._last_scheduler_state.hierarchy_runnable_task_ids
        assert (await orch._measure_pools()).demand[PoolKey("worker")] == 1
        assert await db.count_ready_by_profile(PROJECT_ID) == {"worker": 1}
        assert await db.is_hierarchy_task_runnable("second")
        explained = await handler._cmd_explain_task({"task_id": "second"})
        assert not any(r["code"] == "frontier_sibling_prerequisite_not_delivered"
                       for r in explained["reasons"])
        diagnosis = await _check_ready_frontier_exclusions(DoctorContext(config=orch.config, db=db))
        assert not any(row["task_id"] == "second" for row in diagnosis.data.get("tasks", []))

    await advisory_cycle()
    assert fetch.await_count == 1
    for age in (5.0, 15.0, env.observer.READ_MAX_AGE):
        frontier_clock.return_value = 100.0 + age
        await advisory_cycle()
        assert fetch.await_count == 1
    frontier_clock.return_value += 0.01
    await advisory_cycle()
    assert fetch.await_count == 2
    await advisory_cycle()
    assert fetch.await_count == 2


async def test_git_first_expired_advisory_snapshot_fails_closed_on_fetch_error(
    orch, db, git_first_frontier, frontier_clock, monkeypatch,
):
    from src.git.manager import GitError
    from src.integration.delivery_observer import hierarchy_frontier_modes
    from src.scheduler import PoolKey

    env = git_first_frontier
    await _deliver_first_by_train(db, env)
    assert await db.count_ready_by_profile(PROJECT_ID) == {"worker": 1}
    fetch = AsyncMock(side_effect=GitError("remote unavailable"))
    monkeypatch.setattr(env.observer.git, "afetch_origin", fetch)
    # A successful observation can be reused only inside the bound.
    frontier_clock.return_value += env.observer.READ_MAX_AGE
    assert await db.count_ready_by_profile(PROJECT_ID) == {"worker": 1}
    fetch.assert_not_awaited()
    frontier_clock.return_value += 0.01
    modes = await hierarchy_frontier_modes(db)
    assert modes[PROJECT_ID].delivered_prerequisite_ids == frozenset()
    fetch.assert_awaited_once()
    await orch._schedule(hierarchy_modes=modes)
    assert "second" not in orch._last_scheduler_state.hierarchy_runnable_task_ids
    assert (await orch._measure_pools(hierarchy_modes=modes)).demand[PoolKey("worker")] == 0


async def test_git_first_claim_refetches_after_cached_advisory_target_rewinds(
    orch, db, git_first_frontier, frontier_clock, monkeypatch,
):
    from src.commands.handler import CommandHandler
    from src.integration.delivery_observer import hierarchy_frontier_modes

    env = git_first_frontier
    await _deliver_first_by_train(db, env)
    fetch = AsyncMock(wraps=env.observer.git.afetch_origin)
    monkeypatch.setattr(env.observer.git, "afetch_origin", fetch)
    modes = await hierarchy_frontier_modes(db)
    assert modes[PROJECT_ID].delivered_prerequisite_ids == {"first"}
    await orch._reconcile_pools(hierarchy_modes=modes)
    await orch.wait_for_pool_launches()
    [session] = await db.list_sessions(lifecycle="pool")
    fetch.assert_awaited_once()
    handler = CommandHandler(orch, orch.config)
    handler._current_scope = {"kind": "session", "session_id": session.id,
                              "project_id": PROJECT_ID, "task_id": None, "elevated": False}

    env.git(env.origin.clone, "push", "--force", "origin", f"{env.base}:aq/epic")
    # A tick already holding its advisory view can still see runnable work.
    await orch._schedule(hierarchy_modes=modes)
    assert "second" in orch._last_scheduler_state.hierarchy_runnable_task_ids
    assert fetch.await_count == 1
    # A new advisory read detects the rewind without fetching again.
    assert not await db.is_hierarchy_task_runnable("second")
    assert fetch.await_count == 1
    for expected_fetches in (2, 3):
        result = await handler._cmd_task_claim({"next": True})
        assert result["result"] == "no_ready_work", result
        assert fetch.await_count == expected_fetches
        assert (await db.get_task("second")).status is TaskStatus.READY
    prepare = orch._worktree_slots().reset_slot_for_task
    prepare.assert_not_awaited()
    # Even the immediately preceding failed claim's observation is not reused.
    env.git(env.origin.clone, "push", "origin", f"{env.source}:aq/epic")
    delivery_head = AsyncMock(wraps=db.hierarchy_prerequisite_delivery_head)
    monkeypatch.setattr(db, "hierarchy_prerequisite_delivery_head", delivery_head)
    result = await handler._cmd_task_claim({"next": True})
    assert result["result"] == "claimed", result
    assert result["task"]["id"] == "second"
    # Admission refetches once; workspace preparation separately observes the
    # current parent tip and retained prerequisite stack before activation.
    assert fetch.await_count == 6
    delivery_head.assert_awaited_once_with("second")
    prepare.assert_awaited_once()
    assert prepare.await_args.args[1].id == "second"
    assert prepare.await_args.kwargs == {
        "base_branch": env.source,
        "target_branch": "aq/second",
        "preserve_branch": True,
    }


@pytest.mark.parametrize("movement", ["rewind", "retarget", "generation", "unstable", "shadow"])
async def test_git_first_frontier_withholds_invalid_or_shadow_proof(
    orch, db, git_first_frontier, movement, monkeypatch,
):
    from src.commands.handler import CommandHandler
    from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY
    from src.database.tables import task_delivery_receipts
    from src.models import TaskCompletion
    from src.scheduler import PoolKey

    env = git_first_frontier
    await _deliver_first_by_train(db, env)
    assert await db.count_ready_by_profile(PROJECT_ID) == {"worker": 1}
    if movement == "rewind":
        env.git(env.origin.clone, "push", "--force", "origin", f"{env.base}:aq/epic")
    elif movement == "retarget":
        await db.update_task("epic", branch_name="aq/another-epic")
    elif movement == "generation":
        # Model a new completed generation without provenance. The prior one proves nothing.
        await db.save_task_completion(TaskCompletion(
            id="close-first-again", task_id="first", outcome="pass", commits=[env.source],
            completed_at=time.time(),
        ))
        await db.set_task_meta("first", DEVELOPMENT_COMPLETION_ID_KEY, "close-first-again")
    elif movement == "unstable":
        view = await env.observer.prerequisite_view(PROJECT_ID)
        monkeypatch.setattr(type(view), "fresh", AsyncMock(return_value=False))
        env.observer.prerequisite_view = AsyncMock(return_value=view)
    else:
        env.observer.truth = None

    if movement in {"rewind", "unstable"}:
        # A perfectly matching old receipt must not override current Git truth.
        async with db.immediate() as conn:
            await conn.execute(insert(task_delivery_receipts).values(
                id="misleading-receipt", domain_key="first-delivery", source_task_id="first",
                target_task_id="epic", repository_id="repo", target_branch="aq/epic",
                reviewed_head_sha=env.source, before_sha=env.base, squash_sha=env.source,
                after_sha=env.source, disposition="code", created_at=time.time(),
            ))

    assert not await db.is_hierarchy_task_runnable("second")
    assert await db.count_ready_by_profile(PROJECT_ID) == {}
    await orch._schedule()
    assert "second" not in orch._last_scheduler_state.hierarchy_runnable_task_ids
    assert (await orch._measure_pools()).demand[PoolKey("worker")] == 0
    explained = await CommandHandler(orch, orch.config)._cmd_explain_task({"task_id": "second"})
    assert ("frontier_sibling_prerequisite_not_delivered" in explained["reason_codes"]) is (
        movement not in {"rewind", "unstable"}
    )
    assert ("delivery_evidence_unavailable" in explained["reason_codes"]) is (movement == "rewind")
    if movement == "unstable":
        # The mocked freshness failure checks no refs, so it must not poison
        # otherwise valid cached display evidence. Decisions above still withhold.
        assert all(snapshot._freshness.get(snapshot.target_ref, True)
                   for _, snapshot in env.observer._recent.values())


class _FakeSlotManager:
    """Stubs the git-level slot creation ``WorktreeSlotManager`` normally does.

    ``ensure_slots`` just writes the DB rows a real slot would end up with —
    no git, no filesystem — so worktree-mode tests exercise
    ``_ensure_worktree_slots`` / ``_launch_pool_session`` without a real repo.
    """

    def __init__(self, db):
        self.db = db

    async def ensure_slots(self, project, base_ws, kind, count):
        slots = await self.db.list_slots_for_base(base_ws.id)
        for idx in range(len(slots), count):
            ws = Workspace(
                id=f"{base_ws.id}-slot{idx}",
                project_id=base_ws.project_id,
                workspace_path=f"{base_ws.workspace_path}-slot{idx}",
                source_type=RepoSourceType.WORKTREE,
                kind_id=base_ws.kind_id,
                slot_index=idx,
                base_workspace_id=base_ws.id,
            )
            await self.db.create_workspace(ws)
            slots.append(ws)
        return slots


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    # These fixtures model independent clones; slot provisioning has its own tests.
    kind = await database.resolve_workspace_kind("__system__", "project-repo")
    await database.upsert_workspace_kind(replace(kind, mode="exclusive-clone"))
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(
        AgentProfile(
            id="worker", name="w", lifecycle="pool", min_active=0, max_active=2, harness="claude"
        )
    )
    for i in range(2):
        await database.create_workspace(
            Workspace(
                id=f"ws{i}",
                project_id=PROJECT_ID,
                workspace_path=str(tmp_path / f"ws{i}"),
                source_type=RepoSourceType.LINK,
                kind_id="project-repo",
            )
        )
    yield database
    await database.close()


@pytest.fixture
async def orch(db, tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database=DatabaseConfig(url=lease_dsn("test.db")),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    cfg.swarm.max_starts_per_tick = 5
    o = Orchestrator(cfg)
    o.session_spec_builder._intelligence_classes = {
        "standard-medium": IntelligenceClass(
            "standard-medium",
            "Standard",
            "",
            {"anthropic": {"model": "claude-sonnet-5"}},
        ),
    }
    o.db = db
    # ``AgentReconciler`` was built in ``__init__`` against the (real,
    # uninitialized) db the constructor saw -- point it at the test db too
    # so ``_schedule()`` (which reconciles agents first) works end to end.
    o._agent_reconciler._db = db
    o.git = MagicMock()
    o._ensure_control_files_excluded = AsyncMock(return_value=True)
    o.bus.emit = AsyncMock()
    o.harness_registry.upsert(
        Harness(
            id="claude",
            name="claude",
            command="claude",
            prompt_mode="arg",
            session_id_flag="--session-id",
            process_names=("claude",),
        )
    )
    yield o
    await o.wait_for_pool_launches(cancel=True)


async def ready(db, tid, *, profile_id="worker", intelligence_class=None):
    await db.create_task(
        Task(
            id=tid,
            project_id=PROJECT_ID,
            title=tid,
            description=tid,
            status=TaskStatus.READY,
            profile_id=profile_id, route_source=route_source_for(profile_id),
            intelligence_class=intelligence_class,
        )
    )


class TestReconcilePools:
    async def test_pool_handoff_detects_git_without_literal_dot_git(
        self, orch, db, tmp_path
    ):
        """Pool launch protects its checkout before the session receives it."""
        from src.api.auth import SessionTokenStore

        orch.config.worktrees.enabled = False
        orch.token_store = SessionTokenStore(db)
        workspace = tmp_path / "ws0"
        workspace.mkdir(parents=True)

        session_id = await orch._launch_pool_session(
            await db.get_project(PROJECT_ID), await db.get_profile("worker")
        )

        assert session_id is not None
        orch._ensure_control_files_excluded.assert_awaited_once_with(str(workspace))
        row = await db.get_session(session_id)
        spec = orch.session_providers.create("fake").starts[0]
        scope = await orch.token_store.validate(spec.env["AQ_API_TOKEN"])
        assert scope is not None
        assert scope.session_instance_token == row.instance_token

    async def test_pool_handoff_refuses_unverifiable_daemon_excludes(
        self, orch, db, tmp_path
    ):
        """A pool session must not receive a checkout without the managed block."""
        orch.config.worktrees.enabled = False
        workspace = tmp_path / "ws0"
        (workspace / ".git").mkdir(parents=True)
        orch._ensure_control_files_excluded = AsyncMock(
            side_effect=OSError("exclude is read-only")
        )

        session_id = await orch._launch_pool_session(
            await db.get_project(PROJECT_ID), await db.get_profile("worker")
        )

        assert session_id is None
        assert await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID) == []
        assert all(ws.locked_by_agent_id is None for ws in await db.list_workspaces(PROJECT_ID))

    async def test_starts_sessions_for_ready_work(self, orch, db):
        for t in ("t1", "t2", "t3"):
            await ready(db, t)
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        pool = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        assert len(pool) == 2  # max_active
        assert all(s.agent_id for s in pool)
        assert all(str(uuid.UUID(s.id)) == s.id and s.name.startswith("p-worker--proj--") for s in pool)
        assert all(s.state == "running" for s in pool)
        provider = orch.session_providers.create("fake", orch.config)
        assert {spec.session_name for spec in provider.starts} == {s.name for s in pool}
        sessions_by_name = {session.name: session for session in pool}
        for spec in provider.starts:
            argv = list(spec.command)
            assert argv[argv.index("--session-id") + 1] == sessions_by_name[spec.session_name].id
        agents = await db.list_agents()
        assert sorted(a.state.value for a in agents) == [
            AgentState.IDLE.value,
            AgentState.IDLE.value,
        ]
        for s in pool:
            assert (await db.get_workspace_for_agent(s.agent_id)) is not None
        kinds = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert kinds.count("pool.scaled") == 1
        # The bus is in-process and its WebSocket forward is live-only, so
        # the audit row is the only trace an operator surface
        # (`aq system get-recent-events --event-type pool.scaled`) can read
        # back after the fact.
        rows = await db.get_recent_events(event_type="pool.scaled")
        assert len(rows) == 1
        assert rows[0]["project_id"] == PROJECT_ID
        assert rows[0]["payload"] == "start 2 worker"

    async def test_no_audit_row_when_nothing_scales(self, orch, db):
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.get_recent_events(event_type="pool.scaled") == []

    async def test_no_starts_when_disabled(self, orch, db):
        orch.config.swarm.enabled = False
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.list_sessions(lifecycle="pool") == []

    async def test_starved_pool_starts_nothing_when_no_workspace(self, orch, db):
        for ws in await db.list_workspaces(PROJECT_ID):
            await db.delete_workspace(ws.id)
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.list_sessions(lifecycle="pool") == []
        assert await db.list_agents() == []
        # A starved pool is expected, not exceptional -- it must not
        # quarantine the key (unlike a genuine launch failure, R3).
        assert orch._pool_quarantine == {}

    async def test_quarantined_key_starts_nothing(self, orch, db):
        orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() + 60
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.list_sessions(lifecycle="pool") == []

    async def test_drain_marks_idle_sessions_after_grace(self, orch, db):
        # Sessions are created running (R1) -- nothing promotes
        # starting -> running for a pool row, so no hand-edit is needed
        # (or possible) to make the session count as idle supply.
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session_id = (await db.list_sessions(lifecycle="pool"))[0].id
        await db.delete_task("t1")
        orch.config.swarm.scale_down_grace = 0
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert (await db.get_session(session_id)).desired_state == "stopped"

    async def test_disabled_profile_starts_nothing_and_drains_idle_workers(self, orch, db):
        """The operator switch sizes the pool to zero without deleting it."""
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session_id = (await db.list_sessions(lifecycle="pool"))[0].id

        await db.update_profile("worker", enabled=False)
        await ready(db, "t2")
        orch.config.swarm.scale_down_grace = 0
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        # No new worker for the newly ready task, and the idle one is drained.
        assert len(await db.list_sessions(lifecycle="pool")) == 1
        assert (await db.get_session(session_id)).desired_state == "stopped"

    async def test_disabled_profile_leaves_a_worker_on_a_task_alone(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = (await db.list_sessions(lifecycle="pool"))[0]
        await db.update_session(session.id, task_id="t1")

        await db.update_profile("worker", enabled=False)
        orch.config.swarm.scale_down_grace = 0
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        # Busy supply floors ``desired``: the task it holds runs to completion.
        assert (await db.get_session(session.id)).desired_state == "running"

    async def test_codex_pool_launch_keeps_uuid_id_without_session_id_flag(self, orch, db):
        await db.update_profile("worker", harness="codex")
        orch.harness_registry.upsert(Harness(id="codex", name="codex", command="codex"))

        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        session = (await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID))[0]
        assert str(uuid.UUID(session.id)) == session.id
        assert session.name.startswith("p-worker--proj--")
        provider = orch.session_providers.create("fake", orch.config)
        assert provider.starts[0].session_name == session.name
        assert "--session-id" not in provider.starts[0].command

    async def test_push_scheduler_ignores_pool_profile_tasks(self, orch, db):
        await ready(db, "t1")
        assert await orch._pool_profile_ids(PROJECT_ID) == {"worker"}
        task = await db.get_task("t1")
        profile = await orch._resolve_profile(task)
        assert orch._is_session_routed(profile) is False

    async def test_launch_failure_releases_resources_and_preserves_definition(self, orch, db, monkeypatch):
        async def _boom(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(db, "acquire_one_unlocked", _boom)
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        workers = await db.list_agents()
        assert len(workers) == 1 and workers[0].state == AgentState.IDLE
        assert workers[0].profile_id == "worker"
        assert await db.list_sessions(lifecycle="pool") == []
        for ws in await db.list_workspaces(PROJECT_ID):
            assert ws.locked_by_agent_id is None
        until = orch._pool_quarantine.get((PROJECT_ID, "worker"))
        assert until is not None and until > time.time()

    async def test_failed_acquisition_yields_next_tick_to_other_project(self, orch, db, tmp_path):
        orch.config.swarm.max_starts_per_tick = 1
        await db.create_project(Project(id="other", name="other"))
        await db.create_workspace(Workspace(
            id="other-ws", project_id="other", workspace_path=str(tmp_path / "other"),
            source_type=RepoSourceType.LINK, kind_id="project-repo",
        ))
        for i in range(3):
            await ready(db, f"backlog-{i}")
        await db.create_task(Task(
            id="other-task", project_id="other", title="other", description="",
            status=TaskStatus.READY, profile_id="worker", route_source="legacy",
        ))
        acquire = db.acquire_one_unlocked

        async def fail_busy_project(**kwargs):
            if kwargs["project_id"] == PROJECT_ID:
                return None
            return await acquire(**kwargs)

        db.acquire_one_unlocked = fail_busy_project
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.list_sessions(lifecycle="pool") == []
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        sessions = await db.list_sessions(lifecycle="pool")
        assert len(sessions) == 1
        assert sessions[0].project_id == "other"

    @pytest.mark.parametrize("workers", [1, 2])
    async def test_worktree_mode_grows_a_slot_and_starts(self, orch, db, tmp_path, workers):
        orch.config.worktrees.enabled = True
        orch._worktree_slot_manager = _FakeSlotManager(db)

        # Swap the two exclusive-clone workspaces for a worktree-mode base
        # with no pre-existing slots.
        for ws in await db.list_workspaces(PROJECT_ID):
            await db.delete_workspace(ws.id)
        system_kind = await db.resolve_workspace_kind(PROJECT_ID, "project-repo")
        await db.upsert_workspace_kind(
            dataclasses.replace(system_kind, project_id=PROJECT_ID, mode="worktree")
        )
        base = Workspace(
            id="base0",
            project_id=PROJECT_ID,
            workspace_path=str(tmp_path / "base0"),
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
        )
        await db.create_workspace(base)

        for i in range(workers):
            await ready(db, f"task-{i}")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        pool = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        assert len(pool) == workers
        slots = await db.list_slots_for_base("base0")
        assert len(slots) == workers
        assert {slot.locked_by_agent_id for slot in slots} == {row.agent_id for row in pool}

    async def test_terminate_pool_session_full_teardown(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = (await db.list_sessions(lifecycle="pool"))[0]
        claim_path = os.path.join(session.work_dir, CLAIM_FILE)
        write_claim_file(session.work_dir, {"task_id": "t1"})
        assert os.path.exists(claim_path)

        await orch._terminate_pool_session(session, reason="test_teardown")

        agent = await db.get_agent(session.agent_id)
        assert agent.state == AgentState.IDLE
        assert await db.get_workspace_for_agent(session.agent_id) is None
        updated = await db.get_session(session.id)
        assert updated.state == "stopped"
        assert not os.path.exists(claim_path)

    @pytest.mark.parametrize("stop_confirmed", [False, True])
    async def test_terminate_pool_session_retains_attached_integration_owner(
        self, orch, db, monkeypatch, stop_confirmed
    ):
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = (await db.list_sessions(lifecycle="pool"))[0]
        claim_path = os.path.join(session.work_dir, CLAIM_FILE)
        await db.update_session(session.id, task_id="t1", claim_phase="active")
        await db.update_task("t1", status=TaskStatus.IN_PROGRESS, assigned_agent_id=session.agent_id)
        await db.update_agent(session.agent_id, current_task_id="t1")
        workspace = await db.get_workspace_for_agent(session.agent_id)
        await db.update_workspace(workspace.id, locked_by_task_id="t1")
        write_claim_file(session.work_dir, {"task_id": "t1", "claim_epoch": 0})
        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_branch_owners).values(
                    id="owner-1", repository_id="repo-1", ref="aq/t1", owner_id="t1",
                    owner_role="worker", fence_token=1, handoff_state="attached",
                    session_id=session.id, workspace_id=workspace.id,
                    created_at=time.time(), updated_at=time.time(),
                )
            )

        if not stop_confirmed:
            # The fake confirms a stop from its registry; a provider that
            # cannot prove the exit must leave the session unstopped.
            provider = orch.session_providers.create(session.provider, orch.config)
            monkeypatch.setattr(provider, "confirm_stopped", AsyncMock(return_value=False))

        await orch._terminate_pool_session(session, reason="integration_owner")

        updated = await db.get_session(session.id)
        agent = await db.get_agent(session.agent_id)
        # A confirmed exit is recorded (recovery needs it) without releasing
        # anything the attached owner still holds.
        assert (updated.state == "stopped") is stop_confirmed
        assert updated.task_id == "t1"
        assert agent.state is not AgentState.IDLE
        assert (await db.get_workspace(workspace.id)).locked_by_agent_id == session.agent_id
        assert os.path.exists(claim_path)

    async def test_startup_warns_when_pool_profiles_but_swarm_disabled(self, orch, caplog):
        """I5 / ruling P2-17: say out loud that the flag strands pool work."""
        orch.config.swarm.enabled = False
        with caplog.at_level(logging.WARNING, logger="src.orchestrator.core"):
            await orch._warn_if_pools_disabled()
        assert any("swarm.enabled is false" in r.message for r in caplog.records)

    async def test_startup_silent_when_swarm_enabled(self, orch, caplog):
        orch.config.swarm.enabled = True
        with caplog.at_level(logging.WARNING, logger="src.orchestrator.core"):
            await orch._warn_if_pools_disabled()
        assert not any("swarm.enabled is false" in r.message for r in caplog.records)

    async def test_schedule_skips_snapshot_emit_with_no_subscribers(self, orch, db):
        """I7: the only listener is a ``task_claim`` long-poll (none here)."""
        await ready(db, "t1")
        assert orch.bus.subscriber_count("snapshot.refreshed") == 0
        await orch._schedule()
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list if c.args]
        assert "snapshot.refreshed" not in emitted

    async def test_schedule_emits_snapshot_when_someone_listens(self, orch, db):
        await ready(db, "t1")

        async def _noop(_event):
            return None

        orch.bus.subscribe("snapshot.refreshed", _noop)
        await orch._schedule()
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list if c.args]
        assert "snapshot.refreshed" in emitted

    async def test_schedule_excludes_pool_profile_task_and_pool_agent(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        pool_sessions = await db.list_sessions(lifecycle="pool")
        assert len(pool_sessions) == 1
        pool_agent_id = pool_sessions[0].agent_id

        await db.create_profile(AgentProfile(id="reviewer", name="r", harness="claude"))
        await ready(
            db, "t2", profile_id="reviewer", intelligence_class="standard-medium"
        )

        actions = await orch._schedule()

        assigned_agent_ids = {a.agent_id for a in actions}
        assigned_task_ids = {a.task_id for a in actions}
        assert pool_agent_id not in assigned_agent_ids
        assert "t1" not in assigned_task_ids
        assert "t2" in assigned_task_ids


async def test_durable_pool_reuses_definition_after_teardown(orch, db):
    from src.models import Agent
    await db.create_agent(Agent(id="configured-worker", name="Keeper", profile_id="worker", model="fixed-model"))
    await ready(db, "task-a")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    first = (await db.list_sessions(lifecycle="pool"))[0]
    assert first.agent_id == "configured-worker"
    assert first.model == "fixed-model" and first.llm_provider == "anthropic"
    await orch._terminate_pool_session(first, reason="rotation")
    assert (await db.get_agent("configured-worker")).state == AgentState.IDLE
    assert (await db.get_agent("configured-worker")).model == "fixed-model"
    await db.create_project(Project(id="second", name="Second"))
    await db.create_workspace(Workspace(id="second-ws", project_id="second", workspace_path="/tmp/second-ws", source_type=RepoSourceType.LINK, kind_id="project-repo"))
    new_id = await orch._launch_pool_session(await db.get_project("second"), await db.get_profile("worker"))
    second = await db.get_session(new_id)
    assert second.agent_id == "configured-worker" and second.project_id == "second"
    assert first.id != second.id
    assert len(await db.list_agents()) == 1


async def test_pool_stop_failure_keeps_worker_and_workspace_unavailable(orch, db, monkeypatch):
    await ready(db, "task-a")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    record = (await db.list_sessions(lifecycle="pool"))[0]
    provider = orch.session_providers.create(record.provider, orch.config)
    monkeypatch.setattr(provider, "stop", AsyncMock(side_effect=RuntimeError("cannot confirm exit")))
    await orch._terminate_pool_session(record, reason="test")
    assert (await db.get_session(record.id)).state != "stopped"
    assert (await db.get_agent(record.agent_id)).state != AgentState.IDLE
    assert (await db.get_workspace_for_agent(record.agent_id)) is not None


async def test_stopped_pool_worker_can_take_push_task_without_reprofile(orch, db):
    await ready(db, "pooled")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    row = (await db.list_sessions(lifecycle="pool"))[0]
    await orch._terminate_pool_session(row, reason="rotate")
    await db.create_profile(AgentProfile(id="reviewer", name="Review", harness="claude"))
    await ready(
        db, "push", profile_id="reviewer", intelligence_class="standard-medium"
    )
    actions = await orch._schedule()
    assert [(a.task_id, a.agent_id) for a in actions] == [("push", row.agent_id)]
    assert (await db.get_agent(row.agent_id)).profile_id == "worker"


async def test_repeated_pool_teardown_does_not_steal_new_launch_reservation(orch, db):
    await ready(db, "pooled")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    row = (await db.list_sessions(lifecycle="pool"))[0]
    await orch._terminate_pool_session(row, reason="rotate")
    assert await db.reserve_idle_agent(row.agent_id)
    await orch._terminate_pool_session(row, reason="old-history")
    assert (await db.get_agent(row.agent_id)).state == AgentState.BUSY


async def test_first_pool_claim_survives_launch_completion(orch, db, monkeypatch):
    await ready(db, "pooled")
    original = db.create_session
    visible_states = []

    async def claim_immediately_after_insert(row, **kwargs):
        await original(row, **kwargs)
        visible_states.append((await db.get_agent(row.agent_id)).state)
        async with db.immediate() as conn:
            await db.record_holder(
                conn,
                session_id=row.id,
                task_id="pooled",
                claim_epoch=0,
                agent_id=row.agent_id,
                work_dir=row.work_dir,
                now=time.time(),
            )

    monkeypatch.setattr(db, "create_session", claim_immediately_after_insert)
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    row = (await db.list_sessions(lifecycle="pool"))[0]
    agent = await db.get_agent(row.agent_id)
    assert agent.state == AgentState.BUSY and agent.current_task_id == "pooled"
    # Registration is observable while the agent remains reserved; launch
    # acknowledgement must preserve the claim made during that window.
    assert visible_states == [AgentState.BUSY]


async def test_concurrent_pool_teardown_stops_and_releases_only_once(orch, db, monkeypatch):
    import asyncio
    await ready(db, "pooled")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    row = (await db.list_sessions(lifecycle="pool"))[0]
    provider = orch.session_providers.create(row.provider, orch.config)
    original_stop = provider.stop

    async def slow_stop(*args, **kwargs):
        await asyncio.sleep(0.1)
        await original_stop(*args, **kwargs)

    stop = AsyncMock(side_effect=slow_stop)
    monkeypatch.setattr(provider, "stop", stop)
    await asyncio.gather(
        orch._terminate_pool_session(row, reason="reconciler"),
        orch._terminate_pool_session(row, reason="operator"),
    )
    assert stop.await_count == 1
    assert (await db.get_agent(row.agent_id)).state == AgentState.IDLE


# ---------------------------------------------------------------------------
# Global keying: one pool per profile, placement decides the project.
# ---------------------------------------------------------------------------


async def second_project(db, *, path="/tmp/second-ws"):
    """A second ACTIVE project with one free ``project-repo`` workspace."""
    await db.create_project(Project(id="second", name="Second"))
    await db.create_workspace(
        Workspace(
            id="second-ws",
            project_id="second",
            workspace_path=path,
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
    )


async def test_measure_pools_aggregates_projects_under_one_profile_key(orch, db, tmp_path):
    """Two projects, one profile, one pool — with the per-project breakdown and
    a placement candidate each kept alongside the aggregate."""
    from src.scheduler import PoolKey

    await second_project(db, path=str(tmp_path / "second-ws"))
    await ready(db, "t1")
    await ready(db, "t2")
    await db.update_task("t2", project_id="second")

    measurement = await orch._measure_pools()

    key = PoolKey("worker")
    assert set(measurement.supply) == {key}
    assert set(measurement.bounds) == {key}
    assert measurement.bounds[key] == (0, 2)  # the profile's own bounds, once
    assert measurement.demand[key] == 2  # summed across both projects
    assert set(measurement.supply[key].by_project) == {PROJECT_ID, "second"}
    assert {c.project_id for c in measurement.candidates[key]} == {PROJECT_ID, "second"}
    assert set(measurement.projects) == {PROJECT_ID, "second"}


async def test_idle_worker_whose_claim_loop_went_silent_is_not_supply(orch, db):
    """swift-dune: a worker parked on a usage-limit screen must not absorb demand.

    Two such sessions counted as idle supply on 2026-09-21, so the sizer saw
    its demand met and a READY task waited behind workers that never claim.
    """
    from src.pool_claims import pool_claim_loop_stall_seconds
    from src.scheduler import PoolKey

    await ready(db, "t1")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    stuck = (await db.list_sessions(lifecycle="pool"))[0]
    sup = (await orch._measure_pools()).supply[PoolKey("worker")]
    assert (sup.running_idle, sup.unresponsive) == (1, 0)

    # Nothing has entered ``task_claim`` from it for two long-poll windows.
    silent_since = time.time() - pool_claim_loop_stall_seconds(orch.config.swarm) - 1
    await db.update_session(stuck.id, last_activity=silent_since, started_at=silent_since)
    sup = (await orch._measure_pools()).supply[PoolKey("worker")]
    assert (sup.running_idle, sup.unresponsive, sup.idle_session_ids) == (0, 1, [])
    assert sup.by_project[PROJECT_ID].unresponsive == 1
    assert sup.by_project[PROJECT_ID].idle_session_ids == []

    # ...so the ready task gets a working session of its own.
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    live = [s for s in await db.list_sessions(lifecycle="pool") if s.id != stuck.id]
    assert len(live) == 1 and live[0].state == "running"
    assert (await db.get_session(stuck.id)).desired_state == "running"


async def test_draining_idle_worker_is_not_supply_and_the_ready_task_gets_a_new_one(orch, db):
    """fair-grove-86: a worker retired after its task never absorbs new demand.

    ``fresh_context_per_task`` leaves a closed worker idle at its prompt with
    ``desired_state='stopped'`` until the session reconciler tears it down.
    Until then it is draining: not idle, not offered to a drain, not part of
    placement's ``live`` -- so READY work behind it still gets a worker.
    """
    from src.scheduler import PoolKey

    await ready(db, "t1")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    retired = (await db.list_sessions(lifecycle="pool"))[0]
    await db.update_session(retired.id, desired_state="stopped")

    measurement = await orch._measure_pools()
    sup = measurement.supply[PoolKey("worker")]
    assert (sup.draining, sup.running_idle, sup.idle_session_ids) == (1, 0, [])
    (cand,) = measurement.candidates[PoolKey("worker")]
    assert (cand.live, cand.project_live_total, cand.idle_session_ids) == (0, 0, ())

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    live = [s for s in await db.list_sessions(lifecycle="pool") if s.id != retired.id]
    assert len(live) == 1 and (live[0].state, live[0].desired_state) == ("running", "running")


async def test_measure_pools_records_placement_inputs_per_project(orch, db, tmp_path):
    from src.scheduler import PoolKey

    await second_project(db, path=str(tmp_path / "second-ws"))
    orch._pool_quarantine[("second", "worker")] = time.time() + 60
    await ready(db, "t1")

    measurement = await orch._measure_pools()
    by_project = {c.project_id: c for c in measurement.candidates[PoolKey("worker")]}

    assert by_project["second"].quarantined is True
    assert by_project[PROJECT_ID].quarantined is False
    assert by_project[PROJECT_ID].workspace_capacity == 2  # the fixture's two links
    assert by_project[PROJECT_ID].ready == 1
    assert by_project["second"].ready == 0
    # ``max_concurrent_agents`` is a placement input now, not a pool bound.
    project = await db.get_project(PROJECT_ID)
    assert by_project[PROJECT_ID].project_cap == project.max_concurrent_agents


async def test_quarantined_project_does_not_burn_the_fleets_start_budget(orch, db, tmp_path):
    """Quarantine is an eligibility predicate, checked before a start is spent.

    It used to be checked *after* the sizer had already picked a key, so a
    single broken project could consume the tick's whole start budget and
    leave a healthy one with nothing.
    """
    await second_project(db, path=str(tmp_path / "second-ws"))
    orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() + 60
    await ready(db, "t1")
    await ready(db, "t2")
    await db.update_task("t2", project_id="second")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    sessions = await db.list_sessions(lifecycle="pool")
    assert [s.project_id for s in sessions] == ["second"]


async def test_every_project_quarantined_starves_rather_than_starting(orch, db, tmp_path):
    await second_project(db, path=str(tmp_path / "second-ws"))
    now = time.time()
    orch._pool_quarantine[(PROJECT_ID, "worker")] = now + 60
    orch._pool_quarantine[("second", "worker")] = now + 60
    await ready(db, "t1")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    assert await db.list_sessions(lifecycle="pool") == []
    assert await db.get_recent_events(event_type="pool.scaled") == []


async def test_pool_scaled_payload_carries_the_placement_reason(orch, db):
    await ready(db, "t1")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    scaled = [
        call.args[1] for call in orch.bus.emit.await_args_list if call.args[0] == "pool.scaled"
    ]
    assert len(scaled) == 1
    assert scaled[0]["project_id"] == PROJECT_ID
    assert scaled[0]["profile_id"] == "worker"
    assert scaled[0]["kind"] == "start"
    assert scaled[0]["placement_reason"] == "deficit"


async def test_drain_event_reports_the_project_the_worker_actually_left(orch, db):
    await ready(db, "t1")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    await db.delete_task("t1")
    orch.config.swarm.scale_down_grace = 0
    orch.bus.emit.reset_mock()
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    drains = [
        call.args[1]
        for call in orch.bus.emit.await_args_list
        if call.args[0] == "pool.scaled" and call.args[1]["kind"] == "drain"
    ]
    assert [(d["project_id"], d["count"]) for d in drains] == [(PROJECT_ID, 1)]
    # A drain has no placement reason: nothing was placed.
    assert "placement_reason" not in drains[0]


async def test_global_cap_bounds_the_whole_fleet(orch, db, tmp_path):
    """``global_cap`` was hardcoded ``None`` at the call site, so the fleet had no
    box-wide bound at all.  It now resolves to ``resources.max_concurrent_agents``
    unless ``swarm.global_max_active`` overrides it."""
    await second_project(db, path=str(tmp_path / "second-ws"))
    orch.config.resources.max_concurrent_agents = 1
    await ready(db, "t1")
    await ready(db, "t2")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    assert len(await db.list_sessions(lifecycle="pool")) == 1


async def test_swarm_global_max_active_overrides_the_resource_cap(orch, db):
    orch.config.resources.max_concurrent_agents = 8
    orch.config.swarm.global_max_active = 1
    await ready(db, "t1")
    await ready(db, "t2")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    assert len(await db.list_sessions(lifecycle="pool")) == 1


async def test_starvation_is_reported_once_per_condition_not_once_per_tick(orch, db, caplog):
    """The pool step runs every five seconds, and a fleet with nowhere to put a
    worker stays that way until an operator acts.  One warning per condition,
    not one per tick — the same restraint the quarantine window buys."""
    for ws in await db.list_workspaces(PROJECT_ID):
        await db.delete_workspace(ws.id)
    await ready(db, "t1")

    with caplog.at_level(logging.WARNING, logger="src.orchestrator.pools"):
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        first = [r.getMessage() for r in caplog.records if r.name == "src.orchestrator.pools"]
        caplog.clear()
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        second = [r.getMessage() for r in caplog.records if r.name == "src.orchestrator.pools"]

    assert len(first) == 1
    assert "no eligible project" in first[0] and "no workspace capacity" in first[0]
    assert second == []


async def test_all_quarantined_starvation_does_not_re_warn_over_the_quarantine(orch, db, caplog):
    """``_quarantine_pool`` already said this, once, with the startup output
    attached; a second warning per tick is the wall of noise it exists to stop."""
    orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() + 60
    await ready(db, "t1")

    with caplog.at_level(logging.WARNING, logger="src.orchestrator.pools"):
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

    assert [r for r in caplog.records if r.name == "src.orchestrator.pools"] == []
    assert await db.list_sessions(lifecycle="pool") == []


async def test_min_per_project_parks_a_warm_worker_in_every_eligible_project(
    orch, db, tmp_path
):
    """The knob that buys back what global sizing costs: a stated per-project
    reservation raises the fleet floor *and* is placed where it was promised,
    with no ready work anywhere."""
    await second_project(db, path=str(tmp_path / "second-ws"))
    await db.update_profile("worker", min_active=0, max_active=4, min_per_project=1)

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    sessions = await db.list_sessions(lifecycle="pool")
    assert sorted(s.project_id for s in sessions) == ["proj", "second"]
    reasons = {
        call.args[1]["project_id"]: call.args[1].get("placement_reason")
        for call in orch.bus.emit.await_args_list
        if call.args[0] == "pool.scaled"
    }
    assert reasons == {"proj": "warm_floor", "second": "warm_floor"}


async def test_a_quarantined_project_holds_no_warm_reservation_open(orch, db, tmp_path):
    """``min_per_project`` raises the effective floor only for projects that could
    actually host the worker — otherwise a broken project would park a slot of
    the fleet's floor against a project that cannot use it."""
    from src.scheduler import PoolKey

    await second_project(db, path=str(tmp_path / "second-ws"))
    await db.update_profile("worker", min_active=0, max_active=4, min_per_project=1)
    orch._pool_quarantine[("second", "worker")] = time.time() + 60

    measurement = await orch._measure_pools()

    assert measurement.bounds[PoolKey("worker")] == (1, 4)


async def test_reconcile_relocates_idle_capacity_without_raising_global_cap(orch, db, tmp_path):
    """A full pool can serve a new project's queue after its old queue empties."""
    await second_project(db, path=str(tmp_path / "second-ws"))
    await db.update_profile("worker", max_active=1)
    orch.config.swarm.global_max_active = 1
    orch.config.swarm.scale_down_grace = 0
    await ready(db, "old-demand")
    await db.update_task("old-demand", project_id="second")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    sessions = await db.list_sessions(lifecycle="pool")
    assert len(sessions) == 1
    old = sessions[0]
    assert old.project_id == "second"
    await db.update_session(old.id, state="running")
    await db.delete_task("old-demand")
    await ready(db, "new-demand")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    assert (await db.get_session(old.id)).desired_state == "stopped"
    assert len(await db.list_sessions(lifecycle="pool")) == 1
    # A later sizing pass, after retirement, may launch in the demanding project.
    await db.update_session(old.id, state="stopped")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    successors = [s for s in await db.list_sessions(lifecycle="pool") if s.id != old.id]
    assert len(successors) == 1
    assert successors[0].project_id == PROJECT_ID
    assert (await db.get_task("new-demand")).status is TaskStatus.READY


async def test_slow_launch_does_not_block_other_project_or_oversubscribe(orch, db, tmp_path, monkeypatch):
    import asyncio

    from src.scheduler import PoolKey

    await ready(db, "slow-task")
    await db.create_project(Project(id="second", name="Second"))
    await db.create_workspace(Workspace(id="second-ws", project_id="second",
        workspace_path=str(tmp_path / "second"), source_type=RepoSourceType.LINK,
        kind_id="project-repo"))
    await db.create_task(Task(id="fast-task", project_id="second", title="Fast",
        description="", status=TaskStatus.READY, profile_id="worker", route_source="legacy"))
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    entered, release, fast_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def start(spec):
        if "--proj--" in spec.session_name:
            entered.set()
            await release.wait()
        result = await original(spec)
        if "--second--" in spec.session_name:
            fast_started.set()
        return result

    monkeypatch.setattr(provider, "start", start)
    await asyncio.wait_for(orch._reconcile_pools(), timeout=5)
    await asyncio.wait_for(entered.wait(), timeout=5)
    await asyncio.wait_for(fast_started.wait(), timeout=5)
    # The slow launch has a committed STARTING row and reserved capacity.
    measurement = await orch._measure_pools()
    supply = measurement.supply[PoolKey("worker")]
    assert supply.starting + supply.running_idle + supply.running_busy == 2
    for _ in range(3):
        await orch._reconcile_pools()
    assert len(provider.starts) == 1
    assert len(orch._pool_launches) == 1
    release.set()
    await orch.wait_for_pool_launches()
    rows = await db.list_sessions(lifecycle="pool")
    assert {row.project_id for row in rows} == {PROJECT_ID, "second"}
    assert len({row.agent_id for row in rows}) == 2
    assert not orch._pool_launches


async def test_cancelled_background_start_stops_process_and_releases_resources(orch, db, monkeypatch):
    import asyncio

    await ready(db, "task")
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    entered = asyncio.Event()

    async def start(spec):
        result = await original(spec)
        entered.set()
        await asyncio.Event().wait()
        return result

    monkeypatch.setattr(provider, "start", start)
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert any(ws.locked_by_agent_id for ws in await db.list_workspaces(PROJECT_ID))
    await orch.wait_for_pool_launches(cancel=True)
    assert not orch._pool_launches
    rows = await db.list_sessions(lifecycle="pool")
    assert len(rows) == 1
    assert rows[0].state == rows[0].desired_state == "stopped"
    assert rows[0].ended_at and rows[0].end_reason == "launch_failed"
    assert all(ws.locked_by_agent_id is None for ws in await db.list_workspaces(PROJECT_ID))
    assert all(agent.state == AgentState.IDLE for agent in await db.list_agents())
    assert provider.sessions == {}


async def test_cancelled_reservation_waits_for_commit_before_releasing(orch, db, monkeypatch):
    import asyncio

    from src.models import Agent

    await db.create_agent(Agent(id="existing", name="Existing", profile_id="worker"))
    original = db.reserve_idle_agent
    entered, release = asyncio.Event(), asyncio.Event()

    async def reserve(agent_id):
        result = await original(agent_id)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(db, "reserve_idle_agent", reserve)
    task = asyncio.create_task(orch._launch_pool_session(
        await db.get_project(PROJECT_ID), await db.get_profile("worker")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await db.get_agent("existing")).state == AgentState.IDLE
    assert await db.list_sessions(lifecycle="pool") == []


async def test_live_slow_launch_identity_survives_reconciler_reservation_timeout(orch, db, monkeypatch):
    import asyncio

    await ready(db, "task")
    entered, release = asyncio.Event(), asyncio.Event()
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start

    async def start(spec):
        entered.set()
        await release.wait()
        return await original(spec)

    monkeypatch.setattr(provider, "start", start)
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    agent_id = next(iter(orch._launching_pool_agent_ids()))
    await db.update_agent(agent_id, last_heartbeat=time.time() - 300)
    await orch._schedule()
    assert (await db.get_agent(agent_id)).state == AgentState.BUSY
    release.set()
    await orch.wait_for_pool_launches()


@pytest.mark.parametrize("constraint", ["global", "project", "workspace"])
async def test_pending_launch_reserves_shared_capacity_across_profiles(orch, db, monkeypatch, constraint):
    import asyncio

    await db.create_profile(AgentProfile(id="other", name="Other", lifecycle="pool",
        min_active=0, max_active=2, harness="claude"))
    await ready(db, "task-a")
    await ready(db, "task-b", profile_id="other")
    if constraint == "global":
        orch.config.resources.max_concurrent_agents = 1
    elif constraint == "project":
        await db.update_project(PROJECT_ID, max_concurrent_agents=1)
    else:
        await db.delete_workspace("ws1")
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    entered, release = asyncio.Event(), asyncio.Event()
    attempted = []

    async def start(spec):
        attempted.append(spec)
        entered.set()
        await release.wait()
        return await original(spec)

    monkeypatch.setattr(provider, "start", start)
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    for _ in range(3):
        await orch._reconcile_pools()
    assert len(attempted) == 1
    assert len(orch._pool_launches) == 1
    release.set()
    await orch.wait_for_pool_launches()
    assert len(await db.list_sessions(lifecycle="pool")) == 1


async def test_slow_provider_does_not_hide_another_free_workspace(orch, db, monkeypatch):
    import asyncio

    await ready(db, "first")
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    first, second, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = []

    async def start(spec):
        attempts.append(spec)
        (first if len(attempts) == 1 else second).set()
        await release.wait()
        return await original(spec)

    monkeypatch.setattr(provider, "start", start)
    await orch._reconcile_pools()
    await asyncio.wait_for(first.wait(), timeout=5)
    await ready(db, "second")
    await orch._reconcile_pools()
    await asyncio.wait_for(second.wait(), timeout=5)
    assert len(orch._pool_launches) == 2
    release.set()
    await orch.wait_for_pool_launches()
    assert len(await db.list_sessions(lifecycle="pool")) == 2


async def test_cancelled_start_keeps_resources_when_process_stop_is_unconfirmed(orch, db, monkeypatch):
    import asyncio

    await ready(db, "task")
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    entered = asyncio.Event()

    async def start(spec):
        await original(spec)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "start", start)
    monkeypatch.setattr(provider, "stop", AsyncMock(side_effect=RuntimeError("unknown process state")))
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    await orch.wait_for_pool_launches(cancel=True)
    assert provider.sessions
    assert any(ws.locked_by_agent_id for ws in await db.list_workspaces(PROJECT_ID))
    # The registered writer retains its BUSY reservation until reconciliation
    # proves termination; it no longer needs an untracked ERROR placeholder.
    rows = await db.list_sessions(lifecycle="pool")
    assert len(rows) == 1 and rows[0].state == "starting"
    assert all(agent.state == AgentState.BUSY for agent in await db.list_agents())
    assert not orch._pool_launches


async def test_cancellation_after_session_insert_preserves_durable_owner(orch, db, monkeypatch):
    import asyncio

    await ready(db, "task")
    entered = asyncio.Event()

    async def emit(event, *args, **kwargs):
        if event == "pool.session_started":
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(orch.bus, "emit", emit)
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    before = (await db.list_sessions(lifecycle="pool"))[0]
    await orch.wait_for_pool_launches(cancel=True)
    after = await db.get_session(before.id)
    assert after.state == "running"
    assert (await db.get_workspace_for_agent(after.agent_id)) is not None
    provider = orch.session_providers.create("fake", orch.config)
    assert len(provider.sessions) == 1
    assert not orch._pool_launches


async def test_timeout_pool_cleanup_respects_wait_but_operator_stop_wins(orch, db, monkeypatch):
    from src.models import TaskStatus

    await ready(db, "waiting")
    sid = await orch._launch_pool_session(await db.get_project(PROJECT_ID), await db.get_profile("worker"))
    row = await db.get_session(sid)
    await db.transition_task("waiting", TaskStatus.IN_PROGRESS, assigned_agent_id=row.agent_id)
    task = await db.get_task("waiting")
    await db.update_agent(row.agent_id, state=AgentState.BUSY, current_task_id="waiting")
    await db.update_session(sid, task_id="waiting", claim_phase="active", last_claim_epoch=task.claim_epoch)
    now = time.time()
    wait = await db.register_agent_wait(
        identity={"session_id": sid, "instance_token": row.instance_token,
                  "project_id": PROJECT_ID, "claim_epoch": task.claim_epoch, "elevated": False},
        kind="timer", match={"due_at": now + 1000}, deadline_at=now + 2000,
        idempotency_key="pool-cleanup", now=now,
    )
    row = await db.get_session(sid)
    for reason in ("stalled", "stuck_timeout", "prepare_timeout", "claim_loop_stalled"):
        await orch._terminate_pool_session(row, reason=reason)
        current = await db.get_session(sid)
        assert current.state == "running" and current.task_id == "waiting"
        assert (await db.get_agent(row.agent_id)).state == AgentState.BUSY
        assert (await db.get_agent_wait(wait["id"]))["state"] == "active"
    await orch._terminate_pool_session(row, reason="operator")
    assert (await db.get_session(sid)).state == "stopped"
    await db.reconcile_agent_waits(now=now + 1)
    assert (await db.get_agent_wait(wait["id"]))["state"] == "cancelled"
