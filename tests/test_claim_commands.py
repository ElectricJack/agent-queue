"""aq task claim / close --claim-next / epoch fence — spec §10, §14."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import asyncpg
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.database.tables import (
    integration_branch_owners,
    integration_repair_stages,
    projects,
    sessions,
    task_branch_origins,
    task_integration_checkpoints,
    task_metadata,
    tasks,
)
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.intelligence_classes import IntelligenceClass
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.sessions import SessionProviderRegistry
from src.sessions.reconciler import SessionReconciler
from tests.db_fixtures import lease_dsn
from tests.assignment_routing_helpers import route_source_for

PROJECT_ID = "proj"
NOW = time.time()


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(
        AgentProfile(id="worker", name="w", lifecycle="pool", needs_workspace=False)
    )
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database=DatabaseConfig(url=lease_dsn("test.db")),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    cfg.swarm.claim_wait_max = 5
    return cfg


@pytest.fixture
async def handler(db, config):
    orch = Orchestrator(config)
    orch.session_spec_builder._intelligence_classes = {
        "standard-medium": IntelligenceClass(
            "standard-medium",
            "Standard",
            "",
            {"anthropic": {"model": "claude-sonnet-5"}},
        ),
    }
    orch.db = db
    orch.git = MagicMock()
    orch._worktree_slots = MagicMock(
        return_value=MagicMock(reset_slot_for_task=AsyncMock(return_value="aq/t"))
    )
    orch._last_scheduler_state = None  # no snapshot yet → admissible
    # ``orch.git`` is a bare MagicMock (no real checkout exists under
    # ``tmp_path``) — stub the completion pipeline so ``task_close(outcome=
    # "pass")`` doesn't try to ``await`` a non-async git call.  Mirrors the
    # stub in ``tests/test_session_commands.py``.
    orch._run_completion_pipeline = AsyncMock(return_value=(None, True))
    # The ready listener is what lets a blocked ``task_claim`` long-poll
    # wake on ``task.ready`` — normally wired by ``Orchestrator.initialize()``
    # via ``monitoring.register_settlement_listener``, which this fixture
    # skips (no daemon loop in these tests).
    orch.register_settlement_listener()
    return CommandHandler(orch, config)


async def mktask(db, tid, status=TaskStatus.READY, **kw):
    kw.setdefault("intelligence_class", "standard-medium")
    kw.setdefault("route_source", route_source_for(kw.get("profile_id")))
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def pool_session(db, tmp_path, sid="s1", agent_id="agent-1"):
    work_dir = tmp_path / agent_id
    work_dir.mkdir()
    await db.create_agent(
        Agent(id=agent_id, name=agent_id, profile_id="worker", state=AgentState.IDLE)
    )
    await db.create_workspace(
        Workspace(
            id=f"ws-{agent_id}",
            project_id=PROJECT_ID,
            workspace_path=str(work_dir),
            kind_id="project-repo",
            source_type=RepoSourceType.LINK,
            locked_by_agent_id=agent_id,
        )
    )
    await db.create_session(
        SessionRecord(
            id=sid,
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name=f"p-worker--proj--{sid}",
            lifecycle="pool",
            work_dir=str(work_dir),
            epoch="e",
            instance_token="t",
            started_at=NOW,
            state="running",
            agent_id=agent_id,
            llm_provider="anthropic",
            model="claude-sonnet-5",
            intelligence_class="standard-medium",
        )
    )
    return sid, work_dir


async def stopped_slot_reset_claim(handler, db, tmp_path, *, detached=False, stopped=True):
    """The retained claim left by a stopped worker after a reset incident."""
    await mktask(db, "t1", profile_id="worker")
    sid, wd = await pool_session(db, tmp_path)
    assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "claimed"
    await db.transition_task("t1", TaskStatus.BLOCKED, force=True)
    await db.update_workspace("ws-agent-1", locked_by_task_id=None, locked_by_agent_id=None)
    if stopped:
        await db.update_session(
            sid, state="stopped", desired_state="stopped", ended_at=time.time(), end_reason="drained",
            **({"task_id": None, "claim_phase": None} if detached else {}),
        )
    await db.set_task_meta("t1", "slot_reset_failure", {"reason": "old reset failure"})
    await db.set_task_meta("t1", "needs_attention", "slot_reset_failed")
    await db.set_task_meta("t1", "claim_prepare_backoff_until", time.time() + 300)
    await db.set_task_meta("t1", "claim_prepare_backoff_attempts", 3)
    provider = SimpleNamespace(confirm_stopped=AsyncMock(return_value=True))
    handler.orchestrator.session_providers = SimpleNamespace(create=lambda *_: provider)
    handler._current_scope = None
    return sid, wd, provider


def scoped(handler, sid):
    handler._current_scope = {
        "kind": "session",
        "session_id": sid,
        "task_id": None,
        "project_id": PROJECT_ID,
        "elevated": False,
    }
    return handler


async def ordinary_pool_repair(handler, db, tmp_path):
    from src.integration.batches import Batch, BatchMember, BatchStore, candidate_ref
    from src.integration.lock import BranchLock
    from src.integration.repair import OrdinaryRepairService

    await db.create_repo(RepoConfig(id="repo", project_id=PROJECT_ID,
                                   source_type=RepoSourceType.LINK, source_path=str(tmp_path)))
    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train",
                            integration_repository_id="repo")
    store = BatchStore(db)
    await store.freeze(Batch("repair-batch", PROJECT_ID, "repo", "refs/heads/main"),
                       (BatchMember("source", "b" * 40, "a" * 40),),
                       trees={"source": "c" * 40})
    service = OrdinaryRepairService(db)
    ref = candidate_ref("repair-batch")
    allocated = await service.allocate("repair-batch", target_ref=ref, head_sha="b" * 40,
                                       authorize=AsyncMock(return_value=True))
    task_id = allocated["task_id"]
    await db.update_task(task_id, status=TaskStatus.READY, profile_id="worker",
                         intelligence_class="standard-medium", route_source=route_source_for("worker"))
    sid, wd = await pool_session(db, tmp_path)
    handler.orchestrator._hierarchy_repair_start = AsyncMock(return_value="b" * 40)
    handler.orchestrator._worktree_slots.return_value.reset_slot_for_task.return_value = (
        ref.removeprefix("refs/heads/")
    )
    locks = BranchLock(db)
    target = BranchKey(repository_id="repo", branch=ref)
    return SimpleNamespace(handler=scoped(handler, sid), sid=sid, wd=wd, task_id=task_id,
                           service=service, locks=locks, target=target, store=store,
                           lease=await locks.get(target))


@pytest.mark.parametrize("error", [
    asyncpg.DeadlockDetectedError("deadlock detected"),
    asyncpg.SerializationError("could not serialize access"),
    DBAPIError("activation", {}, asyncpg.DeadlockDetectedError("deadlock detected")),
])
async def test_repair_prepare_retries_rolled_back_database_conflict(
    handler, db, tmp_path, monkeypatch, error,
):
    env = await ordinary_pool_repair(handler, db, tmp_path)
    activate = db.activate_claim
    calls = 0

    async def activate_with_conflict(*args, **kwargs):
        nonlocal calls
        calls += 1
        conn = kwargs["conn"]
        if calls == 1:
            # A real SQL write inside preparation must disappear before retry.
            await conn.execute(task_metadata.insert().values(
                task_id=env.task_id, key="rolled_back_prepare", value="true",
            ))
            raise error
        assert await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == env.task_id,
            task_metadata.c.key == "rolled_back_prepare",
        )) is None
        return await activate(*args, **kwargs)

    monkeypatch.setattr(db, "activate_claim", activate_with_conflict)
    result = await env.handler._cmd_task_claim({"next": True})

    assert result["result"] == "claimed", result
    assert calls == 2
    assert result["claim_epoch"] == 1
    lease = await env.locks.get(env.target)
    assert (lease.holder, lease.fence) == (env.task_id, env.lease.fence)
    assert (await env.store.get("repair-batch")).repair_attempt_count == 1
    assert await db.get_task_meta(env.task_id, "needs_attention") is None
    assert await db.get_task_meta(env.task_id, "slot_reset_failure") is None
    assert "pool.prepare_failed" not in handler.orchestrator.bus.seen_event_types
    assert json.loads((env.wd / ".aq" / "claim.json").read_text())["claim_epoch"] == 1


async def test_repair_prepare_contention_is_bounded_and_resumes_same_claim(
    handler, db, tmp_path, monkeypatch,
):
    env = await ordinary_pool_repair(handler, db, tmp_path)
    activate = db.activate_claim
    injected = AsyncMock(side_effect=[
        asyncpg.DeadlockDetectedError("deadlock detected") for _ in range(3)
    ])
    monkeypatch.setattr(db, "activate_claim", injected)
    result = await env.handler._cmd_task_claim({"next": True})
    assert result["result"] == "no_ready_work", result
    assert injected.await_count == 3
    held = await db.get_session(env.sid)
    assert (held.claim_phase, held.last_claim_epoch, held.claims) == ("preparing", 1, 0)
    assert (await env.locks.get(env.target)).fence == env.lease.fence
    assert await db.get_task_meta(env.task_id, "needs_attention") is None
    assert not (env.wd / ".aq" / "claim.json").exists()

    monkeypatch.setattr(db, "activate_claim", activate)
    resumed = await env.handler._cmd_task_claim({"next": True})
    assert (resumed["result"], resumed["claim_epoch"]) == ("claimed", 1)
    assert (await env.locks.get(env.target)).fence == env.lease.fence
    assert (await env.store.get("repair-batch")).repair_attempt_count == 1


async def test_conflict_after_activation_does_not_retry_preparation(handler, db, tmp_path):
    env = await ordinary_pool_repair(handler, db, tmp_path)
    handler.orchestrator._emit_task_event = AsyncMock(
        side_effect=asyncpg.DeadlockDetectedError("event write")
    )
    result = await env.handler._cmd_task_claim({"next": True})
    assert (result["result"], result["claim_epoch"]) == ("claimed", 1)
    assert (await db.get_session(env.sid)).claims == 1
    handler.orchestrator._worktree_slots.return_value.reset_slot_for_task.assert_awaited_once()
    assert (await env.locks.get(env.target)).fence == env.lease.fence


async def test_real_repair_prepare_error_keeps_attention_and_train_restores_lease(
    handler, db, tmp_path,
):
    env = await ordinary_pool_repair(handler, db, tmp_path)
    reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
    reset.side_effect = RuntimeError("checkout unavailable")
    failed = await env.handler._cmd_task_claim({"next": True})
    assert failed["result"] == "prepare_failed"
    assert await db.get_task_meta(env.task_id, "needs_attention") == "integration_prepare_failed"
    assert (await db.get_task(env.task_id)).status is TaskStatus.READY
    assert (await env.locks.get(env.target)).holder is None
    assert "frontier_origin_not_materialized" in {
        item["code"] for item in await db.claim_frontier_exclusions(env.task_id)
    }

    restored = await env.service.allocate(
        "repair-batch", target_ref=env.target.branch, head_sha="b" * 40,
        authorize=AsyncMock(return_value=True),
    )
    assert (restored["outcome"], restored["task_id"], restored["attempt_count"]) == (
        "exists", env.task_id, 1,
    )
    await db.set_task_meta(env.task_id, "claim_prepare_backoff_until", time.time() - 1)
    assert await db.claim_frontier_exclusions(env.task_id) == []
    reset.side_effect = None
    resumed = await env.handler._cmd_task_claim({"next": True})
    assert resumed["result"] == "claimed", resumed
    assert await db.get_task_meta(env.task_id, "needs_attention") is None


@pytest.mark.parametrize("held_row", ["session", "task"])
async def test_managed_prepare_does_not_hold_ref_while_waiting_for_activation(
    handler, db, tmp_path, monkeypatch, held_row,
):
    """Interleave heartbeat/recovery with the final activation transaction."""
    env = await ordinary_pool_repair(handler, db, tmp_path)
    attach = BranchOwnership.attach
    task_locked, start_activation = asyncio.Event(), asyncio.Event()

    async def pause_after_attachment(self, *args, **kwargs):
        result = await attach(self, *args, **kwargs)
        start_activation.set()
        await task_locked.wait()
        return result

    monkeypatch.setattr(BranchOwnership, "attach", pause_after_attachment)

    async def recover():
        await start_activation.wait()
        async with db.immediate() as conn:
            table, row_id = (sessions, env.sid) if held_row == "session" else (tasks, env.task_id)
            await conn.execute(select(table.c.id).where(
                table.c.id == row_id,
            ).with_for_update())
            task_locked.set()
            # Let preparation wait for the task. Recovery must still be able
            # to acquire the ref, with no deadlock/retry necessary.
            await asyncio.sleep(0.05)
            await env.locks.lock_on(conn, env.target)

    with monkeypatch.context() as patch:
        patch.setattr("src.commands.claim_commands._PREPARATION_RETRY_DELAYS", ())
        async with asyncio.timeout(5):
            claimed, _ = await asyncio.gather(
                env.handler._cmd_task_claim({"next": True}), recover(),
            )
    assert claimed["result"] == "claimed", claimed


async def test_managed_prepare_allows_reset_salvage_on_its_own_connection(handler, db, tmp_path):
    env = await ordinary_pool_repair(handler, db, tmp_path)

    async def reset_with_salvage(*args, **kwargs):
        # The real reset archives dirty/unpushed work through independent DB
        # transactions. Activation's task lock must allow their FK checks.
        await db.add_task_context(env.task_id, type="worktree_salvage", label="dirty", content="patch")
        await db.set_task_meta(env.task_id, "unmerged_branch", env.target.branch)
        return env.target.branch.removeprefix("refs/heads/")

    handler.orchestrator._worktree_slots.return_value.reset_slot_for_task.side_effect = (
        reset_with_salvage
    )
    async with asyncio.timeout(5):
        result = await env.handler._cmd_task_claim({"next": True})
    assert result["result"] == "claimed", result
    assert await db.get_task_meta(env.task_id, "unmerged_branch") == env.target.branch


@pytest.mark.parametrize("delete_ref", [None, "reserved", "attached"])
async def test_conflicting_batch_publishes_before_real_repair_claim(
    handler, db, tmp_path, caplog, monkeypatch, delete_ref,
):
    from src.integration.batches import Batch, BatchMember, BatchService, BatchStore, candidate_ref
    from src.integration.git_truth import GitTruth
    from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
    from src.integration.repair import OrdinaryRepairService
    from src.integration.status import IntegrationStatusService
    from src.integration.train import BatchSelection, CandidateChecks, IntegrationTrain, TrainLane, TrainTarget
    from src.integration.train_sources import LeasedPublish, _never_trusted
    from src.git.github_contracts import GitHubRepositoryBinding
    from src.integration.lock import BranchLock
    from tests.test_delivery_consumers import Origin
    from tests.test_integration_gitops import LocalGit, commit, git

    origin = Origin(tmp_path)
    base = git(origin.clone, "rev-parse", "HEAD")
    first = commit(origin.clone, {"base.txt": "first\n"}, base=base)
    second = commit(origin.clone, {"base.txt": "second\n"}, base=base)
    await db.create_repo(RepoConfig(id="repo", project_id=PROJECT_ID,
                                   source_type=RepoSourceType.CLONE, url=origin.url))
    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train",
                            integration_repository_id="repo", repo_url=origin.url)
    frozen = Batch("conflicting", PROJECT_ID, "repo", "refs/heads/main", created_at=1700000000)
    members = (BatchMember("first", first, base), BatchMember("second", second, base, order=1))
    store = BatchStore(db)
    await store.freeze(frozen, members, trees={
        m.task_id: git(origin.clone, "rev-parse", f"{m.source_sha}^{{tree}}") for m in members
    })
    transport = LocalGit(tmp_path / "origin.git")
    retained = RetainedRepository("repo", origin.clone, GitHubRepositoryBinding(123, "test/repo"), "main")

    async def repository(batch):
        return retained

    async def eligible(batch, members):
        return True

    async def gate(batch, sha, tree):
        return False

    service = BatchService(store, GitOperations(
        db, git=transport, repository=repository,
        authority=SubjectGitAuthority(db, trusted_green=_never_trusted),
    ), publish=LeasedPublish(db, transport), eligible=eligible, gate=gate)
    target = TrainTarget(PROJECT_ID, "repo", "refs/heads/main")

    async def snapshot():
        return await GitTruth(transport).snapshot(
            str(origin.clone), project_id=PROJECT_ID, repository_id="repo",
            repository_url=origin.url, target_ref=target.target_ref,
        )

    async def no_checks(batch, sha):
        raise AssertionError("a conflicting build must not request candidate checks")

    async def lane_for(target):
        return TrainLane(snapshot, service, CandidateChecks(no_checks))

    repair = OrdinaryRepairService(db)
    allocate = repair.allocate

    async def published_before_filing(batch_id, **args):
        # This assertion runs before any ordinary task or worker lease exists.
        assert git(origin.url, "rev-parse", args["target_ref"]) == args["head_sha"]
        assert (await store.get(batch_id)).repair_attempt_count == 0
        return await allocate(batch_id, **args)

    repair.allocate = published_before_filing
    train = IntegrationTrain(
        targets=SimpleNamespace(targets=AsyncMock(return_value=[target])),
        batches=SimpleNamespace(open_batch=AsyncMock(return_value=BatchSelection(frozen, members))),
        lane_for=lane_for, repair=repair,
    )
    await train.tick()
    await train.drain()
    [visit] = train.status()
    assert visit["state"] == "repair", visit
    assert visit["detail"]["member"] == "second"
    assert visit["detail"]["reason"] == "merge_conflict"
    assert visit["detail"]["files"] == ["base.txt"]
    assert "conflicting build conflict" in caplog.text
    ref = candidate_ref(frozen.id)
    task_id = visit["repair"]["task_id"]
    original = await repair.input(task_id)
    assert original["starting_sha"] == visit["candidate_sha"]
    assert original["starting_sha"] != base  # preserve the successfully merged first member
    git(origin.clone, "merge-base", "--is-ancestor", first, original["starting_sha"])
    status = await IntegrationStatusService(db, git_first="active", train=train).train_status(PROJECT_ID)
    assert status["batches"][0]["detail"] == visit["detail"]
    assert any(b["code"] == "merge_conflict" and b["evidence"]["member"] == "second"
               for b in status["blockers"])

    await db.update_task(task_id, status=TaskStatus.READY, profile_id="worker",
                         intelligence_class="standard-medium", route_source=route_source_for("worker"))
    sid, wd = await pool_session(db, tmp_path)
    git(wd, "clone", origin.url, ".")
    handler.orchestrator.git = transport
    del handler.orchestrator._worktree_slots  # exercise real checkout preparation
    h = scoped(handler, sid)
    if delete_ref == "attached":
        # A failed earlier preparation can retain its attachment and epoch.
        # The later missing-ref diagnosis must still avoid slot retry policy.
        with monkeypatch.context() as patch:
            patch.setattr(handler.orchestrator._worktree_slots(), "reset_slot_for_task",
                          AsyncMock(side_effect=RuntimeError("checkout unavailable")))
            patch.setattr(handler.orchestrator, "arelease_integration_writer_for_retry",
                          AsyncMock(return_value=False))
            failed = await h._cmd_task_claim({"next": True})
            assert failed["result"] == "prepare_failed"
            assert (await db.get_session(sid)).claim_phase == "preparing"
    if delete_ref:
        git(origin.clone, "push", "origin", f":{ref}")
    result = await h._cmd_task_claim({"next": True})
    if delete_ref:
        assert result["result"] == "prepare_failed", result
        assert "train defect: unpublished repair target" in result["reason"]
        assert (await db.get_task(task_id)).status == TaskStatus.BLOCKED
        assert await db.get_task_meta(task_id, "needs_attention") == "repair_target_unpublished"
        assert await db.get_task_meta(task_id, "slot_reset_failure") is None
        assert await db.get_task_meta(task_id, "claim_prepare_backoff_attempts") is None
        assert (await db.get_session(sid)).claim_phase is None
        assert not (wd / ".aq" / "claim.json").exists()
        assert (await BranchLock(db).get(BranchKey(repository_id="repo", branch=ref))).holder is None
    else:
        assert result["result"] == "claimed", result
        assert git(wd, "rev-parse", "HEAD") == original["starting_sha"]
        assert git(wd, "branch", "--show-current") == ref.removeprefix("refs/heads/")
        claim = json.loads((wd / ".aq" / "claim.json").read_text())
        assert claim["task_id"] == task_id
        assert (await db.get_task(task_id)).status == TaskStatus.IN_PROGRESS


class _CheckpointGit:
    """Just enough Git for ``resolve_workspace_checkpoint``: clean, pushed."""

    def __init__(self, branch: str, head: str):
        self.branch = branch
        self.head = head

    async def aget_current_branch(self, checkout, strict=False):
        return self.branch

    async def _arun(self, args, cwd=None, **kw):
        if args[:1] == ["status"]:
            return ""
        if args[:1] == ["rev-parse"]:
            return self.head
        raise AssertionError(f"unexpected git call: {args}")

    async def als_remote_ref(self, checkout, branch):
        from src.git.manager import RemoteRefState

        assert branch == self.branch
        return SimpleNamespace(state=RemoteRefState.PRESENT, oid=self.head, error=None)


def emitted(handler):
    return [c.args[0] for c in handler.orchestrator.bus.emit.await_args_list]


class TestClaim:
    async def test_reparent_cannot_turn_a_claimed_leaf_into_a_container(
        self, handler, db, tmp_path
    ):
        await mktask(db, "epic", profile_id="worker")
        await mktask(db, "child", status=TaskStatus.DEFINED)
        await mktask(db, "old-parent", status=TaskStatus.IN_PROGRESS)
        await db.add_dependency("child", "old-parent", "parent-child")
        sid, work_dir = await pool_session(db, tmp_path)
        scoped(handler, sid)
        claim = await handler._cmd_task_claim({"next": True})
        assert claim["result"] == "claimed"
        before = await db.get_task("epic")
        claim_file = (work_dir / ".aq" / "claim.json").read_text()

        handler._current_scope = None
        result = await handler._cmd_reparent_task({"task_id": "child", "parent_id": "epic"})

        assert result["code"] == "hierarchy.live_parent"
        assert (await db.get_task("child")).parent_task_id == "old-parent"
        assert await db.get_typed_dependencies("child") == [("old-parent", "parent-child")]
        after = await db.get_task("epic")
        assert (after.status, after.assigned_agent_id, after.claim_epoch) == (
            before.status, before.assigned_agent_id, before.claim_epoch
        )
        assert (await db.get_session(sid)).task_id == "epic"
        assert (work_dir / ".aq" / "claim.json").read_text() == claim_file
        async with db.immediate() as conn:
            assert not await db.is_container("epic", conn=conn)

    async def test_claim_cas_rechecks_the_container_flag(self, db, tmp_path):
        await mktask(db, "epic", profile_id="worker")
        await pool_session(db, tmp_path)
        async with db.immediate() as conn:
            candidate = await db.select_ready_for_profile(
                conn, project_id=PROJECT_ID, profile_id="worker", agent_id="agent-1",
            )
            assert candidate == "epic"
            await db.mark_container("epic", conn=conn)
            assert await db.take_task(conn, candidate, agent_id="agent-1", now=NOW) is None
        task = await db.get_task("epic")
        assert task.status == TaskStatus.READY
        assert task.assigned_agent_id is None and task.claim_epoch == 0
        agent = await db.get_agent("agent-1")
        assert agent.state == AgentState.IDLE and agent.current_task_id is None

    async def test_reparent_destination_is_unclaimable_during_and_after_the_move(
        self, db, tmp_path
    ):
        await mktask(db, "epic", profile_id="worker")
        await mktask(db, "child", status=TaskStatus.DEFINED)
        await pool_session(db, tmp_path)

        async def candidate():
            async with db.immediate() as other:
                return await db.select_ready_for_profile(
                    other, project_id=PROJECT_ID, profile_id="worker", agent_id="agent-1",
                )

        async with db.immediate() as conn:
            await db.set_parent("child", "epic", conn=conn, reject_live_parent=True)
            # SKIP LOCKED bypasses the destination even while the other
            # connection's snapshot cannot yet see its container metadata.
            assert await asyncio.wait_for(candidate(), timeout=5) is None
        assert await candidate() is None
        assert (await db.get_task("epic")).claim_epoch == 0

    async def test_reparent_waits_for_concurrent_claim_before_checking_holder(
        self, handler, db, tmp_path
    ):
        await mktask(db, "epic", profile_id="worker")
        await mktask(db, "child", status=TaskStatus.DEFINED)
        await pool_session(db, tmp_path)
        started = asyncio.Event()

        async def reparent():
            started.set()
            return await handler._cmd_reparent_task({"task_id": "child", "parent_id": "epic"})

        racer = None
        try:
            async with db.immediate() as conn:
                assert await db.take_task(conn, "epic", agent_id="agent-1", now=NOW)
                racer = asyncio.create_task(reparent())
                await asyncio.wait_for(started.wait(), timeout=5)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(racer), timeout=0.2)
            result = await asyncio.wait_for(racer, timeout=5)
            assert result["code"] == "hierarchy.live_parent"
            assert (await db.get_task("child")).parent_task_id is None
            async with db.immediate() as conn:
                assert not await db.is_container("epic", conn=conn)
        finally:
            if racer is not None and not racer.done():
                racer.cancel()
                await asyncio.gather(racer, return_exceptions=True)

    async def _hierarchy_task(self, db, tmp_path, task_id="child"):
        await db.create_repo(
            RepoConfig(
                id="repo",
                project_id=PROJECT_ID,
                source_type=RepoSourceType.LINK,
                source_path=str(tmp_path),
            )
        )
        await db.update_project(
            PROJECT_ID,
            hierarchical_integration_mode="hierarchy",
            integration_repository_id="repo",
        )
        await mktask(
            db,
            task_id,
            profile_id="worker",
            repo_id="repo",
            branch_name=f"aq/{task_id}",
        )
        async with db.immediate() as conn:
            await conn.execute(
                task_branch_origins.insert().values(
                    id=f"origin-{task_id}",
                    task_id=task_id,
                    repository_id="repo",
                    parent_task_id="parent",
                    parent_repository_id="repo",
                    parent_ref="aq/parent",
                    base_sha="a" * 40,
                    creation_generation=1,
                    reserved=True,
                    materialized=True,
                    created_at=time.time(),
                    materialized_at=time.time(),
                )
            )
            await conn.execute(
                task_integration_checkpoints.insert().values(
                    task_id=task_id,
                    repository_id="repo",
                    branch=f"aq/{task_id}",
                    checkpoint_sha="a" * 40,
                    updated_at=time.time(),
                )
            )
        ownership = BranchOwnership(db)
        fence = await ownership.acquire(
            BranchKey(repository_id="repo", branch=f"aq/{task_id}"),
            task_id,
            "worker",
        )
        return ownership, fence

    async def test_hierarchy_pool_claim_resets_from_exact_origin_and_attaches(
        self, handler, db, tmp_path
    ):
        ownership, _fence = await self._hierarchy_task(db, tmp_path)
        sid, _wd = await pool_session(db, tmp_path)

        result = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert result["result"] == "claimed"
        reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
        assert reset.await_args.kwargs["base_branch"] == "a" * 40
        assert reset.await_args.kwargs["target_branch"] == "aq/child"
        owner = await ownership.get_owner(
            BranchKey(repository_id="repo", branch="aq/child")
        )
        assert owner["handoff_state"] == "attached"
        assert owner["session_id"] == sid
        assert owner["workspace_id"] == "ws-agent-1"

    async def test_pool_repair_claim_passes_repository_url_to_exact_fetch(
        self, handler, db, tmp_path
    ):
        ownership, fence = await self._hierarchy_task(db, tmp_path)
        repair_fence = await ownership.transfer(fence, "child", "repair")
        handler.orchestrator._hierarchy_origin_and_fence = AsyncMock(
            return_value=({"base_sha": "a" * 40}, repair_fence, "repair")
        )
        handler.orchestrator._hierarchy_repair_start = AsyncMock(
            return_value="a" * 40
        )
        sid, _wd = await pool_session(db, tmp_path)

        result = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert result["result"] == "claimed"
        assert handler.orchestrator._hierarchy_repair_start.await_args.kwargs == {
            "repository_url": ""
        }

    async def test_pool_repair_claim_starts_the_stage_budget(
        self, handler, db, tmp_path, monkeypatch
    ):
        """A repair claim starts the stage clock it could not hold while queued.

        A stage's deadline runs from activation, so a delegate the pool cannot
        staff in time is claimed with its budget already spent -- and its close
        is then refused for a repair attempt that never began, leaving the
        worker holding a claim it could neither complete nor release. The claim
        re-arms the stage instead (see
        ``RepairService.start_claimed_stage_budget``).
        """
        from src.integration.repair import RepairService

        ownership, fence = await self._hierarchy_task(db, tmp_path)
        repair_fence = await ownership.transfer(fence, "child", "repair")
        handler.orchestrator._hierarchy_origin_and_fence = AsyncMock(
            return_value=({"base_sha": "a" * 40}, repair_fence, "repair")
        )
        handler.orchestrator._hierarchy_repair_start = AsyncMock(return_value="a" * 40)
        start_budget = AsyncMock(return_value={"outcome": "started"})
        sid, _wd = await pool_session(db, tmp_path)

        monkeypatch.setattr(RepairService, "start_claimed_stage_budget", start_budget)
        result = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert result["result"] == "claimed" and result["task"]["id"] == "child"
        # Runs before the ownership exclusion, so the pool claim never holds a
        # row lock ahead of the repair rows it re-arms.
        assert start_budget.await_args_list == [call("child")]

    async def test_non_repair_pool_claim_does_not_touch_a_repair_stage(
        self, handler, db, tmp_path, monkeypatch
    ):
        from src.integration.repair import RepairService

        await self._hierarchy_task(db, tmp_path)
        start_budget = AsyncMock(return_value={"outcome": "no_stage"})
        sid, _wd = await pool_session(db, tmp_path)

        monkeypatch.setattr(RepairService, "start_claimed_stage_budget", start_budget)
        result = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert result["result"] == "claimed" and result["task"]["id"] == "child"
        start_budget.assert_not_awaited()

    async def test_collector_owned_hierarchy_branch_cannot_reset_or_activate_pool_claim(
        self, handler, db, tmp_path
    ):
        ownership, fence = await self._hierarchy_task(db, tmp_path)
        sid, _wd = await pool_session(db, tmp_path)
        await ownership.transfer(fence, "collector", "collector")

        result = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert result["result"] == "prepare_failed"
        reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
        reset.assert_not_awaited()
        assert (await db.get_session(sid)).claim_phase is None

    async def test_claim_backs_off_a_fenced_branch_instead_of_blocking(
        self, handler, db, tmp_path
    ):
        """A fence the owner sweep clears is a wait, not a workspace fault.

        A ``BranchBusy`` out of the hierarchy fence used to fall into the
        slot-reset ladder, which escalates to BLOCKED on the third attempt --
        so a stage-13 repair delegate whose branch was still fenced by a dead
        writer ended BLOCKED, on a branch the periodic owner-recovery sweep
        releases on its own (2026-10-05).  The claim must back off and retry,
        and must not record the slot-reset fault whose ``aq task resume``
        recovery is the wrong repair.
        """
        await self._hierarchy_task(db, tmp_path)
        sid, wd = await pool_session(db, tmp_path)
        # An ``attached`` row naming a writer that is gone: exactly what
        # ``reconcile_ready_integration_owners`` sweeps for a READY task.
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.owner_id == "child")
                .values(
                    handoff_state="attached", session_id="gone-session",
                    workspace_id="gone-workspace",
                )
            )
        h = scoped(handler, sid)
        for attempt in range(1, 4):
            assert (await h._cmd_task_claim({"next": True}))["result"] == "prepare_failed"
            task = await db.get_task("child")
            assert task.status is TaskStatus.READY, f"attempt {attempt} blocked the task"
            assert task.assigned_agent_id is None
            assert await db.get_task_meta("child", "needs_attention") == "branch_fenced"
            assert await db.get_task_meta("child", "claim_prepare_backoff_attempts") == attempt
            assert await db.get_task_meta("child", "claim_prepare_backoff_until") > time.time()
            # Not a slot-reset fault: ``aq task resume`` is not the repair.
            assert await db.get_task_meta("child", "slot_reset_failure") is None
            fenced = await db.get_task_meta("child", "branch_fenced")
            assert fenced["branch"] == "aq/child"
            assert "not reserved by this task" in fenced["reason"]
            assert (await db.get_session(sid)).claim_phase is None
            assert not (wd / ".aq" / "claim.json").exists()
            await db.set_task_meta("child", "claim_prepare_backoff_until", time.time() - 1)

        # The row is untouched by a refused claim; the sweep owns it.
        owner = await BranchOwnership(db).get_owner(
            BranchKey(repository_id="repo", branch="aq/child")
        )
        assert owner["handoff_state"] == "attached"
        handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = AsyncMock(
            return_value="aq/child"
        )
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.owner_id == "child")
                .values(handoff_state="released", session_id=None, workspace_id=None)
            )
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        # A successful activation clears the fence evidence with the ladder.
        assert await db.get_task_meta("child", "branch_fenced") is None
        assert await db.get_task_meta("child", "needs_attention") is None
        assert await db.get_task_meta("child", "claim_prepare_backoff_attempts") is None

    async def test_fenced_branch_keeps_the_operator_ladder_without_the_sweep(
        self, handler, db, tmp_path
    ):
        """With the sweep off nothing frees the row, so BLOCK is the answer.

        The retryable-backoff path is only correct while something is going to
        release the fenced owner.  ``integration.owner_recovery_sweep: false``
        is exactly the operator statement that it will not be, so the ordinary
        preparation ladder still escalates and a human is asked.
        """
        await self._hierarchy_task(db, tmp_path)
        sid, _wd = await pool_session(db, tmp_path)
        handler.config.integration.owner_recovery_sweep = False
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.owner_id == "child")
                .values(
                    handoff_state="attached", session_id="gone-session",
                    workspace_id="gone-workspace",
                )
            )
        h = scoped(handler, sid)
        for attempt in range(1, 4):
            assert (await h._cmd_task_claim({"next": True}))["result"] == "prepare_failed"
            assert (await db.get_task_meta("child", "slot_reset_failure"))["attempt"] == attempt
            assert await db.get_task_meta("child", "branch_fenced") is None
            await db.set_task_meta("child", "claim_prepare_backoff_until", time.time() - 1)
        assert (await db.get_task("child")).status == TaskStatus.BLOCKED

    async def test_failed_attached_prepare_retries_same_claim_and_fence(self, handler, db, tmp_path):
        ownership, fence = await self._hierarchy_task(db, tmp_path)
        sid, _wd = await pool_session(db, tmp_path)
        reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
        reset.side_effect = [RuntimeError("stale predecessor checkout"), "aq/child"]
        handler.orchestrator.arelease_integration_writer_for_retry = AsyncMock(return_value=False)
        h = scoped(handler, sid)
        failed = await h._cmd_task_claim({"next": True})
        assert failed["result"] == "prepare_failed"
        held = await db.get_session(sid)
        assert held.claim_phase == "preparing"
        epoch = held.last_claim_epoch
        retried = await h._cmd_task_claim({"next": True})
        assert retried["result"] == "claimed"
        assert (await db.get_session(sid)).last_claim_epoch == epoch
        owner = await ownership.get_owner(fence.target)
        assert owner["fence_token"] == fence.token
        assert owner["session_id"] == sid
        assert reset.await_count == 2

    async def test_reconciler_release_honors_fresh_context_drain(self, handler, db, config, tmp_path, monkeypatch):
        """A reconciler-won close release keeps fresh-context drain semantics."""
        await mktask(db, "t1", profile_id="worker")
        sid, work_dir = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claim = await h._cmd_task_claim({"next": True})
        assert (work_dir / ".aq" / "claim.json").exists()

        release_started = asyncio.Event()
        allow_close_release = asyncio.Event()
        real_release_claim = db.release_claim
        release_calls = 0

        async def gate_close_release(*args, **kwargs):
            nonlocal release_calls
            release_calls += 1
            if release_calls == 1:
                release_started.set()
                await allow_close_release.wait()
            return await real_release_claim(*args, **kwargs)

        monkeypatch.setattr(db, "release_claim", gate_close_release)
        close = asyncio.create_task(
            h._cmd_task_close(
                {
                    "task_id": "t1",
                    "outcome": "pass",
                    "summary": "done",
                    "claim_epoch": claim["claim_epoch"],
                }
            )
        )
        try:
            await asyncio.wait_for(release_started.wait(), timeout=5)
            reconciler = SessionReconciler(
                db,
                config,
                SessionProviderRegistry({}),
                bus=handler.orchestrator.bus,
                orchestrator=handler.orchestrator,
                epoch="test",
            )
            await reconciler._step_orphans(await db.list_sessions(live_only=True), time.time())
            session = await db.get_session(sid)
            assert (session.desired_state, session.task_id) == ("stopped", None)
        finally:
            allow_close_release.set()
            await close

        assert not (work_dir / ".aq" / "claim.json").exists()

    async def test_close_release_race_keeps_live_pool_worker_available(
        self, handler, db, config, tmp_path, monkeypatch
    ):
        """A terminal task is a normal intermediate state before pool release.

        Hold the close path immediately before its ``release_claim`` and run
        the orphan sweep in that window.  The sweep must release only the
        task hold; terminating the worker would bypass normal pool
        scale-down grace and an explicit drain acknowledgement.
        """
        handler.config.swarm.fresh_context_per_task = False
        await db.update_profile("worker", max_claims_per_session=None)
        await mktask(db, "t1", profile_id="worker")
        await mktask(db, "t2", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claim = await h._cmd_task_claim({"next": True})

        release_started = asyncio.Event()
        allow_close_release = asyncio.Event()
        real_release_claim = db.release_claim
        release_calls = 0

        async def gate_close_release(*args, **kwargs):
            nonlocal release_calls
            release_calls += 1
            if release_calls == 1:
                release_started.set()
                await allow_close_release.wait()
            return await real_release_claim(*args, **kwargs)

        monkeypatch.setattr(db, "release_claim", gate_close_release)
        close = asyncio.create_task(
            h._cmd_task_close(
                {
                    "task_id": "t1",
                    "outcome": "pass",
                    "summary": "done",
                    "claim_epoch": claim["claim_epoch"],
                }
            )
        )
        try:
            await asyncio.wait_for(release_started.wait(), timeout=5)
            assert (await db.get_task("t1")).status == TaskStatus.COMPLETED

            reconciler = SessionReconciler(
                db,
                config,
                SessionProviderRegistry({}),
                bus=handler.orchestrator.bus,
                orchestrator=handler.orchestrator,
                epoch="test",
            )
            await reconciler._step_orphans(await db.list_sessions(live_only=True), time.time())

            session = await db.get_session(sid)
            assert (session.state, session.desired_state, session.task_id) == (
                "running",
                "running",
                None,
            )
            assert (await db.get_agent("agent-1")).state == AgentState.IDLE

            reclaimed = await h._cmd_task_claim({"task_id": "t2"})
            assert reclaimed["result"] == "claimed"
        finally:
            allow_close_release.set()
            await close

        session = await db.get_session(sid)
        task = await db.get_task("t2")
        assert (session.desired_state, session.task_id) == ("running", "t2")
        assert (task.status, task.assigned_agent_id) == (TaskStatus.IN_PROGRESS, "agent-1")

    @pytest.mark.parametrize("status", [TaskStatus.PAUSED, TaskStatus.BLOCKED, TaskStatus.FAILED])
    async def test_reclaimed_pool_claim_retains_its_slot_for_the_next_claim(
        self, handler, db, tmp_path, status
    ):
        """A live worker can claim again after the reconciler releases its slot."""
        handler.config.swarm.fresh_context_per_task = False
        await db.update_profile("worker", max_claims_per_session=None)
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await db.create_project(Project(id="other-project", name="other"))
        await db.create_workspace(
            Workspace(
                id="ws-other-project",
                project_id="other-project",
                workspace_path=str(tmp_path / "agent-1"),
                kind_id="project-repo",
                source_type=RepoSourceType.LINK,
            )
        )
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})

        await db.transition_task("t1", status, context="test", force=True)
        reconciler = SessionReconciler(
            db,
            handler.config,
            SessionProviderRegistry({}),
            bus=handler.orchestrator.bus,
            orchestrator=handler.orchestrator,
            epoch="test",
        )
        await reconciler._step_orphans(await db.list_sessions(live_only=True), time.time())
        assert (await db.get_session(sid)).task_id is None
        assert (await db.get_workspace_for_agent("agent-1")).locked_by_task_id is None

        await mktask(db, "t2", profile_id="worker")
        next_claim = await h._cmd_task_claim({"next": True})

        assert next_claim["result"] == "claimed", next_claim
        assert next_claim["task"]["id"] == "t2"
        workspace = await db.get_workspace("ws-agent-1")
        assert (workspace.locked_by_agent_id, workspace.locked_by_task_id) == ("agent-1", "t2")
        assert (await db.get_workspace("ws-other-project")).locked_by_agent_id is None
        assert (await db.get_session(sid)).claim_phase == "active"

    async def test_claim_next_returns_task_epoch_and_writes_file(self, handler, db, tmp_path):
        handler.orchestrator.bus.emit = AsyncMock()
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["task"]["id"], res["claim_epoch"]) == ("claimed", "t1", 1)
        data = json.loads((wd / ".aq" / "claim.json").read_text())
        assert (data["task_id"], data["claim_epoch"], data["session_id"]) == ("t1", 1, sid)
        assert (await db.get_session(sid)).claim_phase == "active"
        assert "task.claimed" in emitted(handler) and "task.started" in emitted(handler)

    @pytest.mark.parametrize("bad_value", ['"0"', '"1788823522.8"', '""', "null", "not-a-number"])
    async def test_malformed_backoff_metadata_does_not_break_the_work_query(
        self, handler, db, tmp_path, bad_value
    ):
        """One unparseable backoff value must not take the whole project down.

        ``task_metadata.value`` is free-form JSON text, so a caller that
        stores the *string* ``"0"`` where the number ``0`` was meant leaves a
        row that ``cast(value, Float)`` cannot read.  The cast sits in a
        correlated EXISTS over every candidate task, so before
        :func:`numeric_meta_value` that single row raised
        ``invalid input syntax for type double precision`` for the entire
        statement -- every claim in the project failed for ~18 hours on
        2026-09-07 while the queue reported no ready work.

        A malformed deadline reads as expired: failing open to claimable is
        right for a value whose only job is to *withhold* a task briefly.
        """
        await mktask(db, "t1", profile_id="worker")
        async with db.immediate() as conn:
            await conn.execute(
                task_metadata.insert().values(
                    task_id="t1", key="claim_prepare_backoff_until", value=bad_value
                )
            )
        sid, _wd = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "claimed"
        assert res["task"]["id"] == "t1"

    async def test_a_well_formed_backoff_deadline_still_withholds_the_task(
        self, handler, db, tmp_path
    ):
        """The guard must not defeat the backoff it is guarding.

        Companion to the test above: reading a malformed value as expired is
        only safe if a *valid* future deadline is still honoured, otherwise
        the fix would have quietly removed the hot-loop protection that
        ``prepare_failed`` relies on.
        """
        await mktask(db, "t1", profile_id="worker")
        await db.set_task_meta("t1", "claim_prepare_backoff_until", time.time() + 300)
        sid, _wd = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"

    @pytest.mark.parametrize("live_class,live_model", [
        ("fast-low", "gpt-5.6-luna"),
        ("deep-high", "gpt-5.6-luna"),
        (None, None),
    ])
    async def test_pool_claim_never_downgrades_explicit_task_class(
        self, handler, db, tmp_path, live_class, live_model
    ):
        from src.intelligence_classes import IntelligenceClass

        handler.orchestrator.session_spec_builder._intelligence_classes = {
            "deep-high": IntelligenceClass("deep-high", "Deep", "", {"codex": {"model": "gpt-5.6-sol"}}),
        }
        await mktask(db, "deep", profile_id="worker", intelligence_class="deep-high")
        sid, wd = await pool_session(db, tmp_path)
        await db.update_session(sid, harness="codex", intelligence_class=live_class, model=live_model)
        # Edits affect the next launch, not the already-running pool's model.
        await db.update_agent("agent-1", harness="codex", intelligence_class="deep-high", model="gpt-5.6-sol")
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"
        task = await db.get_task("deep")
        assert task.status == TaskStatus.READY and task.claim_epoch == 0
        assert task.assigned_agent_id is None
        assert (await db.get_session(sid)).claim_phase is None
        assert not (wd / ".aq" / "claim.json").exists()

    async def test_pool_claim_uses_live_sol_even_after_next_launch_settings_change(self, handler, db, tmp_path):
        from src.intelligence_classes import IntelligenceClass

        handler.orchestrator.session_spec_builder._intelligence_classes = {
            "deep-high": IntelligenceClass("deep-high", "Deep", "", {"codex": {"model": "gpt-5.6-sol"}}),
        }
        await mktask(db, "deep", profile_id="worker", intelligence_class="deep-high")
        sid, _ = await pool_session(db, tmp_path)
        await db.update_session(sid, harness="codex", intelligence_class="deep-high", model="gpt-5.6-sol")
        await db.update_agent("agent-1", harness="codex", intelligence_class="fast-low", model="gpt-5.6-luna")
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "claimed" and res["task"]["id"] == "deep"
        assert res["task"]["intelligence_class"] == "deep-high"
        assert (await db.get_task("deep")).assigned_agent_id == "agent-1"

    async def test_waiting_pool_rechecks_updated_profile_before_claiming(self, handler, db, tmp_path, monkeypatch):
        from src.intelligence_classes import IntelligenceClass

        handler.orchestrator.session_spec_builder._intelligence_classes = {
            "deep-high": IntelligenceClass("deep-high", "Deep", "", {"codex": {"model": "gpt-5.6-sol"}}),
            "fast-low": IntelligenceClass("fast-low", "Fast", "", {"codex": {"model": "gpt-5.6-luna"}}),
        }
        sid, _ = await pool_session(db, tmp_path)
        await db.update_session(sid, harness="codex", intelligence_class="deep-high", model="gpt-5.6-sol")
        waiting = asyncio.Event()
        attempt = handler._attempt_claim

        async def observe_attempt(*args, **kwargs):
            result = await attempt(*args, **kwargs)
            if result["result"] == "no_ready_work":
                waiting.set()
            return result

        monkeypatch.setattr(handler, "_attempt_claim", observe_attempt)
        claim = asyncio.create_task(scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 1}))
        try:
            await asyncio.wait_for(waiting.wait(), timeout=5)
            await db.update_profile("worker", default_class="fast-low")
            await mktask(
                db,
                "new-fast",
                status=TaskStatus.DEFINED,
                profile_id="worker",
                intelligence_class=None,
            )
            await db.transition_task("new-fast", TaskStatus.READY, context="profile_changed")
            result = await claim
            assert result["result"] == "no_ready_work"
            assert (await db.get_task("new-fast")).status == TaskStatus.READY
        finally:
            if not claim.done():
                claim.cancel()
                await asyncio.gather(claim, return_exceptions=True)

    async def test_pool_claim_skips_incompatible_higher_priority_task(self, handler, db, tmp_path):
        from src.intelligence_classes import IntelligenceClass

        handler.orchestrator.session_spec_builder._intelligence_classes = {
            "deep-high": IntelligenceClass("deep-high", "Deep", "", {"codex": {"model": "gpt-5.6-sol"}}),
            "fast-low": IntelligenceClass("fast-low", "Fast", "", {"codex": {"model": "gpt-5.6-luna"}}),
        }
        await mktask(db, "deep", profile_id="worker", intelligence_class="deep-high", priority=1)
        await mktask(db, "fast", profile_id="worker", intelligence_class="fast-low", priority=100)
        sid, _ = await pool_session(db, tmp_path)
        await db.update_session(sid, harness="codex", intelligence_class="fast-low", model="gpt-5.6-luna")
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "claimed" and res["task"]["id"] == "fast"
        assert (await db.get_task("deep")).status == TaskStatus.READY

    async def test_pool_claim_takes_an_integration_repair_delegate(
        self, handler, db, tmp_path
    ):
        """A repair delegate is ordinary claimable work for a pool session.

        Both shipped repair profiles are ``lifecycle: pool``, so excluding
        delegates from the frontier would strand every repair dispatch.  The
        ownership half of the ladder is what has to accommodate the pull
        model (``aconfirm_integration_pool_owner_handoff``), not the queue.
        """
        await mktask(
            db,
            "repair-operation-0",
            profile_id="worker",
            priority=1,
            created_by_kind="integration_repair",
            created_by_id="operation",
        )
        await mktask(db, "ordinary", profile_id="worker", priority=100)
        sid, _ = await pool_session(db, tmp_path)

        res = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert res["result"] == "claimed" and res["task"]["id"] == "repair-operation-0"

    async def test_pool_claim_skips_an_expired_repair_delegate(
        self, handler, db, tmp_path
    ):
        """A stage can expire while its never-started delegate is still READY."""
        await mktask(
            db,
            "repair-operation-0",
            profile_id="worker",
            priority=1,
            created_by_kind="integration_repair",
            created_by_id="operation",
        )
        await mktask(db, "ordinary", profile_id="worker", priority=100)
        async with db.immediate() as conn:
            await conn.execute(
                integration_repair_stages.insert().values(
                    operation_id="operation",
                    ordinal=0,
                    policy={},
                    repair_task_id="repair-operation-0",
                    writer_kind="repair_delegate",
                    starting_sha="a" * 40,
                    attempts=0,
                    state="expired",
                )
            )
        sid, _ = await pool_session(db, tmp_path)

        res = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert res["result"] == "claimed" and res["task"]["id"] == "ordinary"
        assert (await db.get_task("repair-operation-0")).status is TaskStatus.READY

    async def test_no_ready_work_without_wait(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"
        assert (await db.get_session(sid)).claim_phase is None

    async def test_wait_wakes_on_task_ready(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)

        async def promote():
            await asyncio.sleep(0.05)
            await mktask(db, "late", status=TaskStatus.DEFINED, profile_id="worker")
            await db.transition_task("late", TaskStatus.READY, context="promotion")

        asyncio.create_task(promote())
        t0 = time.monotonic()
        res = await scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 3})
        assert (res["result"], res["task"]["id"]) == ("claimed", "late")
        assert time.monotonic() - t0 < 2.0  # woke on the event, not the deadline

    async def test_wait_clamped_and_times_out(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        handler.config.swarm.claim_wait_max = 0
        res = await scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 100})
        assert res["result"] == "no_ready_work"

    async def test_development_pool_claim_records_the_delivery_branch(
        self, handler, db, tmp_path
    ):
        """A development-mode claim records the branch the slot reset produced.

        Nothing else writes ``tasks.branch_name`` on this path, and both the
        development close pipeline and development batch collection require it,
        so a task claimed without it could never close pass or be delivered:
        discarding the reset's return value left development-mode tasks with a
        NULL ``branch_name``, and ``resolve_workspace_checkpoint`` then refused
        their close forever (task fleet-willow).
        """
        await db.create_repo(
            RepoConfig(
                id="repo",
                project_id=PROJECT_ID,
                source_type=RepoSourceType.LINK,
                source_path=str(tmp_path),
            )
        )
        await db.update_project(
            PROJECT_ID,
            hierarchical_integration_mode="development",
            integration_repository_id="repo",
        )
        await mktask(db, "t1", profile_id="worker", repo_id="repo")
        assert (await db.get_task("t1")).branch_name is None
        sid, _wd = await pool_session(db, tmp_path)
        reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
        reset.return_value = "aq/t1"

        result = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert result["result"] == "claimed"
        assert (await db.get_task("t1")).branch_name == "aq/t1"
        assert result["task"]["branch_name"] == "aq/t1"

    async def test_failed_activation_records_no_branch(self, handler, db, tmp_path):
        """Branch and activation are one fact: neither lands without the other."""
        await mktask(db, "t1", profile_id="worker")
        sid, _wd = await pool_session(db, tmp_path)
        handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = AsyncMock(
            side_effect=RuntimeError("git exploded")
        )

        assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))[
            "result"
        ] == "prepare_failed"
        assert (await db.get_task("t1")).branch_name is None

    async def test_hierarchy_claim_keeps_its_origin_chain_branch(self, handler, db, tmp_path):
        """The origin chain already named the branch; the claim must agree."""
        await self._hierarchy_task(db, tmp_path)
        sid, _wd = await pool_session(db, tmp_path)
        handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = AsyncMock(
            return_value="aq/child"
        )

        result = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert result["result"] == "claimed"
        assert (await db.get_task("child")).branch_name == "aq/child"

    async def test_prepare_failed_releases_and_reports(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = AsyncMock(
            side_effect=RuntimeError("git exploded")
        )
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "prepare_failed"
        assert not (wd / ".aq" / "claim.json").exists()
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        s = await db.get_session(sid)
        assert (s.claims, s.last_claim_result, s.claim_phase) == (0, "prepare_failed", None)
        assert await db.get_task_meta("t1", "claim_prepare_backoff_attempts") == 1
        assert await db.get_task_meta("t1", "claim_prepare_backoff_until") > time.time()
        # READY is intentionally not enough to re-offer a task whose slot
        # preparation just failed; this prevents a claim --wait hot loop.
        again = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert again["result"] == "no_ready_work"
        assert "pool.prepare_failed" in handler.orchestrator.bus.seen_event_types


    async def test_pool_claim_persists_the_branch_the_slot_reset_returned(
        self, handler, db, tmp_path
    ):
        """A pool-claimed task carries the branch its slot reset created.

        The pool prepare path used to discard ``reset_slot_for_task``'s
        return value, so ``tasks.branch_name`` stayed NULL where the
        push-assignment path writes it.  In a ``development`` project that
        surfaced only at close, as ``resolve_workspace_checkpoint``'s
        ``branch_not_recorded`` refusal.
        """
        await db.update_project(PROJECT_ID, hierarchical_integration_mode="development")
        await mktask(db, "t1", profile_id="worker")
        sid, _wd = await pool_session(db, tmp_path)

        res = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert res["result"] == "claimed"
        assert (await db.get_task("t1")).branch_name == "aq/t"

    async def test_hierarchy_claim_leaves_its_canonical_branch_alone(
        self, handler, db, tmp_path
    ):
        """A hierarchy claim's branch write agrees with the fence target.

        Hierarchy mode pins ``branch_name`` at filing — the origin guard
        refuses a claim whose task branch differs — so the reset's return
        value must land on exactly that branch and never rewrite it.
        """
        await self._hierarchy_task(db, tmp_path)
        reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
        reset.return_value = "aq/child"
        sid, _wd = await pool_session(db, tmp_path)

        res = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert res["result"] == "claimed"
        assert (await db.get_task("child")).branch_name == "aq/child"

    async def test_failed_prepare_writes_no_branch(self, handler, db, tmp_path):
        """A prepare that dies after the reset leaves no half-written branch."""
        await mktask(db, "t1", profile_id="worker")
        sid, _wd = await pool_session(db, tmp_path)
        handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = AsyncMock(
            side_effect=RuntimeError("git exploded")
        )

        res = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert res["result"] == "prepare_failed"
        assert (await db.get_task("t1")).branch_name is None

    async def test_development_close_clears_the_owned_workspace_guard(
        self, handler, db, tmp_path
    ):
        """The claim leaves exactly what a development-mode close demands.

        ``_run_completion_pipeline`` hands ``resolve_workspace_checkpoint``
        the task's ``branch_name``; the whole refusal in this bug was its
        ``not task["branch_name"]`` clause.  Drive that helper with the
        state the claim actually left behind.
        """
        from src.integration.hierarchy import resolve_workspace_checkpoint

        await db.create_repo(
            RepoConfig(
                id="repo",
                project_id=PROJECT_ID,
                source_type=RepoSourceType.LINK,
                source_path=str(tmp_path),
            )
        )
        await db.update_project(
            PROJECT_ID,
            hierarchical_integration_mode="development",
            integration_repository_id="repo",
        )
        await mktask(db, "t1", profile_id="worker", repo_id="repo")
        sid, _wd = await pool_session(db, tmp_path)
        assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "claimed"
        task = await db.get_task("t1")

        head = "b" * 40
        checkpoint = await resolve_workspace_checkpoint(
            db,
            _CheckpointGit(task.branch_name, head),
            {"id": task.id, "repo_id": "repo", "branch_name": task.branch_name},
            await db.get_repo("repo"),
        )

        assert checkpoint == head

    @pytest.mark.parametrize("proof", ["ended", "missing", "successor"])
    async def test_retire_stopped_slot_claim_after_binding_cleared(self, handler, db, tmp_path, proof):
        from sqlalchemy import delete, update

        from src.claim_file import read_claim_file
        from src.database.tables import sessions, task_session_attempts, workspaces
        from src.orchestrator.workspace_claim_recovery import retire_stopped_slot_claim

        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        await db.create_workspace(Workspace(
            id="base", project_id=PROJECT_ID, workspace_path=str(tmp_path),
            source_type=RepoSourceType.LINK,
        ))
        async with db.immediate() as conn:
            await conn.execute(update(workspaces).where(workspaces.c.id == "ws-agent-1").values(
                base_workspace_id="base",
            ))
        assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "claimed"
        await db.release_claim(sid, task_status=TaskStatus.READY, context="test", now=time.time(),
                               release_workspace_lock=True)
        await db.update_session(sid, state="stopped", desired_state="stopped")
        if proof == "missing":
            async with db.immediate() as conn:
                await conn.execute(delete(task_session_attempts).where(task_session_attempts.c.session_id == sid))
        if proof == "successor":
            other, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
            async with db.immediate() as conn:
                await conn.execute(update(sessions).where(sessions.c.id == other).values(work_dir=str(wd)))
        assert read_claim_file(wd) is not None
        assert await retire_stopped_slot_claim(db, str(wd)) is (proof == "ended")
        assert (read_claim_file(wd) is None) is (proof == "ended")

    async def test_slot_reset_retry_preserves_unrelated_attention(self, db):
        await mktask(db, "t1", profile_id="worker")
        await db.set_task_meta("t1", "slot_reset_failure", {"reason": "old reset failure"})
        await db.set_task_meta("t1", "needs_attention", "operator_investigation")
        await db.set_task_meta("t1", "claim_prepare_backoff_until", time.time() + 300)
        await db.resume_task("t1")
        assert await db.get_task_meta("t1", "needs_attention") == "operator_investigation"
        assert await db.get_task_meta("t1", "claim_prepare_backoff_until") is None

    @pytest.mark.parametrize("detached", [False, True])
    @pytest.mark.parametrize("assigned", [False, True])
    async def test_slot_reset_resume_releases_a_proven_stopped_claim(
        self, handler, db, tmp_path, detached, assigned
    ):
        sid, _wd, provider = await stopped_slot_reset_claim(handler, db, tmp_path, detached=detached)
        if not assigned:
            await db.update_task("t1", assigned_agent_id=None)
        before = await db.get_task("t1")
        result = await handler.execute("resume_task", {"task_id": "t1"})
        assert "error" not in result, result
        task = await db.get_task("t1")
        assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)
        assert (task.claim_epoch, task.branch_name, task.retry_count) == (
            before.claim_epoch, before.branch_name, before.retry_count,
        )
        session = await db.get_session(sid)
        assert (session.state, session.task_id, session.claim_phase) == ("stopped", None, None)
        assert (await db.get_agent("agent-1")).current_task_id is None
        assert await db.get_task_meta("t1", "claimed_by_session") is None
        assert await db.get_task_meta("t1", "needs_attention") is None
        assert await db.get_task_meta("t1", "claim_prepare_backoff_until") is None
        assert await db.get_task_meta("t1", "claim_prepare_backoff_attempts") is None
        assert await db.get_task_meta("t1", "slot_reset_failure") is not None
        provider.confirm_stopped.assert_awaited_once()
        # The old session's historical row and claim record no longer wedge retry.
        next_sid, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        assert (await scoped(handler, next_sid)._cmd_task_claim({"next": True}))["result"] == "claimed"

    @pytest.mark.parametrize("unsafe", [
        "live", "restarting", "provider_alive", "probe_error", "changed_token",
        "changed_epoch", "new_holder", "workspace_lock", "no_proof",
    ])
    async def test_slot_reset_resume_keeps_an_unproven_claim(
        self, handler, db, tmp_path, unsafe
    ):
        sid, _wd, provider = await stopped_slot_reset_claim(
            handler, db, tmp_path, stopped=unsafe != "live",
        )
        if unsafe == "restarting":
            await db.update_session(sid, desired_state="running")
        elif unsafe == "provider_alive":
            provider.confirm_stopped.return_value = False
        elif unsafe == "probe_error":
            provider.confirm_stopped.side_effect = RuntimeError("probe unavailable")
        elif unsafe in {"changed_token", "changed_epoch", "new_holder"}:
            async def changed(_handle):
                if unsafe == "new_holder":
                    successor, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
                    await db.update_session(successor, task_id="t1")
                else:
                    updates = {"instance_token": "successor"} if unsafe == "changed_token" else {
                        "last_claim_epoch": (await db.get_session(sid)).last_claim_epoch + 1,
                    }
                    await db.update_session(sid, **updates)
                return True
            provider.confirm_stopped.side_effect = changed
        elif unsafe == "workspace_lock":
            await db.update_workspace("ws-agent-1", locked_by_task_id="t1")
        if unsafe == "no_proof":
            with pytest.raises(ValueError, match="old claim to release"):
                await db.resume_task("t1")
        else:
            result = await handler.execute("resume_task", {"task_id": "t1"})
            assert "error" in result, result
        assert (await db.get_task("t1")).status == TaskStatus.BLOCKED
        assert (await db.get_session(sid)).task_id == "t1"
        assert await db.get_task_meta("t1", "claimed_by_session") == sid
        assert await db.get_task_meta("t1", "needs_attention") == "slot_reset_failed"
        assert await db.get_task_meta("t1", "claim_prepare_backoff_attempts") == 3

    async def test_slot_reset_resume_preserves_reused_agent_and_workspace(
        self, handler, db, tmp_path
    ):
        sid, _wd, _provider = await stopped_slot_reset_claim(handler, db, tmp_path)
        await mktask(db, "peer", profile_id="worker")
        await db.update_agent("agent-1", current_task_id="peer", state=AgentState.BUSY)
        await db.update_workspace("ws-agent-1", locked_by_task_id="peer", locked_by_agent_id="agent-1")
        assert "error" not in await handler.execute("resume_task", {"task_id": "t1"})
        agent = await db.get_agent("agent-1")
        assert (agent.state, agent.current_task_id) == (AgentState.BUSY, "peer")
        workspace = await db.get_workspace("ws-agent-1")
        assert (workspace.locked_by_task_id, workspace.locked_by_agent_id) == ("peer", "agent-1")
        assert (await db.get_session(sid)).task_id is None

    async def test_slot_reset_resume_preserves_gates_and_dependency_blockers(
        self, handler, db, tmp_path
    ):
        await stopped_slot_reset_claim(handler, db, tmp_path)
        await mktask(db, "upstream", status=TaskStatus.DEFINED)
        await db.add_dependency("t1", "upstream")
        gate_id, _ = await db.create_gate(
            project_id=PROJECT_ID, gate_type="human", title="Approval", waiter_task_ids=["t1"],
        )
        assert "error" not in await handler.execute("resume_task", {"task_id": "t1"})
        assert (await db.get_task("t1")).is_blocked
        assert (await db.get_gate(gate_id))["status"] == "open"

    async def test_slot_reset_resume_retains_an_attached_integration_owner(
        self, handler, db, tmp_path
    ):
        ownership, fence = await self._hierarchy_task(db, tmp_path)
        sid, _wd = await pool_session(db, tmp_path)
        assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "claimed"
        await db.transition_task("child", TaskStatus.BLOCKED, force=True)
        await db.update_session(sid, state="stopped", desired_state="stopped", ended_at=time.time())
        await db.update_workspace("ws-agent-1", locked_by_task_id=None, locked_by_agent_id=None)
        await db.set_task_meta("child", "slot_reset_failure", {"reason": "old reset failure"})
        await db.set_task_meta("child", "needs_attention", "slot_reset_failed")
        provider = SimpleNamespace(confirm_stopped=AsyncMock(return_value=True))
        handler.orchestrator.session_providers = SimpleNamespace(create=lambda *_: provider)
        handler._current_scope = None
        before = await ownership.get_owner(fence.target)
        result = await handler.execute("resume_task", {"task_id": "child"})
        assert "old claim to release" in result.get("error", ""), result
        assert await ownership.get_owner(fence.target) == before
        assert (await db.get_session(sid)).task_id == "child"
        assert (await db.get_task("child")).status == TaskStatus.BLOCKED

    async def test_claim_consumes_operator_handoff_checkpoint(self, handler, db, tmp_path):
        """A prepared claim consumes the handoff, so no later prepare replays it."""
        await mktask(db, "t1", profile_id="worker")
        await db.set_task_meta("t1", "supervisor_recovery_checkpoint", {"sha": "abc"})
        sid, _wd = await pool_session(db, tmp_path)
        assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "claimed"
        assert await db.get_task_meta("t1", "supervisor_recovery_checkpoint") is None

    async def test_slot_reset_recovery_is_bounded_and_resume_retries(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _wd = await pool_session(db, tmp_path)
        reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
        reset.side_effect = RuntimeError("branch held by stopped slot")
        for attempt in range(1, 4):
            result = await scoped(handler, sid)._cmd_task_claim({"next": True})
            assert result["result"] == "prepare_failed"
            failure = await db.get_task_meta("t1", "slot_reset_failure")
            assert failure["attempt"] == attempt
            assert failure["reason"] == "branch held by stopped slot"
            assert failure["retry"] == ("manual" if attempt == 3 else "automatic")
            await db.set_task_meta("t1", "claim_prepare_backoff_until", time.time() - 1)
        assert (await db.get_task("t1")).status == TaskStatus.BLOCKED
        assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "no_ready_work"
        reset.side_effect = None
        await db.resume_task("t1")
        assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "claimed"
        assert await db.get_task_meta("t1", "slot_reset_failure") is None
        assert await db.get_task_meta("t1", "needs_attention") is None
        assert await db.get_task_meta("t1", "claim_prepare_backoff_attempts") is None

    async def test_claim_file_write_failure_releases_and_reports(
        self, handler, db, tmp_path, monkeypatch
    ):
        """Any failure after ``record_holder`` committed — including an OSError
        writing the claim file — must release the claim and leave no claim
        file behind, same as a slot-reset failure (review finding #1)."""
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        import src.commands.claim_commands as claim_commands

        monkeypatch.setattr(
            claim_commands,
            "write_claim_file",
            MagicMock(side_effect=OSError("disk full")),
        )
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "prepare_failed"
        assert not (wd / ".aq" / "claim.json").exists()
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        s = await db.get_session(sid)
        assert (s.claims, s.last_claim_result, s.claim_phase) == (0, "prepare_failed", None)

    async def test_pool_close_restores_its_slot_before_releasing_claim(
        self, handler, db, tmp_path
    ):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        slot = MagicMock()
        handler.orchestrator._slot_workspace_at = AsyncMock(return_value=slot)
        restore = AsyncMock()
        handler.orchestrator._worktree_slots.return_value.restore_slot_after_task = restore

        closed = await h._cmd_task_close(
            {"outcome": "pass", "summary": "done", "claim_epoch": claimed["claim_epoch"]}
        )

        assert closed["success"] is True
        restore.assert_awaited_once_with(slot, task_id="t1")
        assert (await db.get_session(sid)).task_id is None

    async def test_pool_close_skips_the_slot_restore_the_release_already_ran(
        self, handler, db, tmp_path
    ):
        """A failing close is restored inside ``complete_session_task``.

        Its integration-writer release needs a clean tree to prove the
        handoff, so it cannot wait for this call.  Salvaging the same slot a
        second time re-walks the archive path for nothing.
        """
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        handler.orchestrator.complete_session_task = AsyncMock(
            return_value={"status": TaskStatus.READY.value, "slot_restored": True}
        )
        slot = MagicMock()
        handler.orchestrator._slot_workspace_at = AsyncMock(return_value=slot)
        restore = AsyncMock()
        handler.orchestrator._worktree_slots.return_value.restore_slot_after_task = restore

        closed = await h._cmd_task_close(
            {"outcome": "fail", "summary": "no", "claim_epoch": claimed["claim_epoch"]}
        )

        assert closed["success"] is True
        restore.assert_not_awaited()
        assert (await db.get_session(sid)).task_id is None

    async def test_pool_close_reports_a_claim_release_it_could_not_make(
        self, handler, db, tmp_path
    ):
        """``_release_claim_on`` declines silently; the close must not.

        An integration owner still attached to this session makes the release
        a no-op, which left ``sessions.task_id`` and ``claim_phase='active'``
        set on a task already back on the frontier — with the claim file
        deleted and every surface reporting ``success: true`` (brisk-delta).
        """
        from src.database.queries.task_queries import TransitionResult

        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        handler.orchestrator._slot_workspace_at = AsyncMock(return_value=None)
        db.release_claim = AsyncMock(return_value=TransitionResult())

        closed = await h._cmd_task_close(
            {"outcome": "fail", "summary": "no", "claim_epoch": claimed["claim_epoch"]}
        )

        assert closed["claim_released"] is False
        assert closed["needs_attention"] == "claim_release_declined"
        assert await db.get_task_meta("t1", "needs_attention") == "claim_release_declined"
        # The session really does still hold the task, so its proof of that
        # must survive: deleting the claim file here is what made the stall
        # invisible to the worker loop as well.
        assert (wd / ".aq" / "claim.json").exists()

    async def test_pool_close_stays_quiet_when_a_reconciler_already_released_it(
        self, handler, db, tmp_path
    ):
        """The other reason ``_release_claim_on`` declines is benign.

        A pool reconciler can release the hold and the session can claim again
        before an old close resumes; the guarded write skipping that close is
        exactly what it is for.  The session no longer holds this task, so
        this is not the stuck hold above and must not be reported as one.
        """
        from src.database.queries.task_queries import TransitionResult

        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        handler.orchestrator._slot_workspace_at = AsyncMock(return_value=None)

        async def release_but_report_nothing(*args, **kwargs):
            await db.update_session(sid, task_id=None)
            return TransitionResult()

        db.release_claim = release_but_report_nothing

        closed = await h._cmd_task_close(
            {"outcome": "fail", "summary": "no", "claim_epoch": claimed["claim_epoch"]}
        )

        assert "claim_released" not in closed
        assert closed.get("needs_attention") != "claim_release_declined"
        assert await db.get_task_meta("t1", "needs_attention") is None
        assert not (wd / ".aq" / "claim.json").exists()

    async def test_duplicate_claim_is_idempotent_once_active(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        second = await h._cmd_task_claim({"next": True})
        assert (second["result"], second["claim_epoch"]) == ("claimed", first["claim_epoch"])

    async def test_specific_task_held_by_other_is_conflict(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        s1, _ = await pool_session(db, tmp_path, sid="s1", agent_id="agent-1")
        s2, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        await scoped(handler, s1)._cmd_task_claim({"task_id": "t1"})
        res = await scoped(handler, s2)._cmd_task_claim({"task_id": "t1"})
        assert res["result"] == "claim_conflict"

    @pytest.mark.parametrize("cap", [None, 1, 8])
    @pytest.mark.parametrize("claim_next", [False, True])
    async def test_default_close_retires_context_without_claiming_next_task(
        self, handler, db, tmp_path, cap, claim_next
    ):
        await db.update_profile("worker", max_claims_per_session=cap)
        await mktask(db, "t1", profile_id="worker")
        await mktask(db, "t2", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        # Reading/retrying the currently held task must not clear its context.
        again = await h._cmd_task_claim({"next": True})
        assert again["claim_epoch"] == first["claim_epoch"]
        assert (await db.get_session(sid)).desired_state == "running"
        closed = await h._cmd_task_close({
            "task_id": "t1", "outcome": "pass", "summary": "done",
            "claim_epoch": first["claim_epoch"], "claim_next": claim_next,
        })
        assert closed["success"] is True
        assert (await db.get_session(sid)).desired_state == "stopped"
        second = await db.get_task("t2")
        assert second.status == TaskStatus.READY and second.assigned_agent_id is None
        if claim_next:
            assert closed["next"]["result"] == "drain_requested"
        assert (await h._cmd_task_claim({"task_id": "t2"}))["result"] == "drain_requested"

    async def test_disabled_pool_profile_is_refused_new_work(self, handler, db, tmp_path):
        """The operator switch stops the *next* claim, not the current task."""
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        held = await h._cmd_task_claim({"next": True})
        assert held["result"] == "claimed"

        await db.update_profile("worker", enabled=False)

        # The task already held is untouched and still assigned to this session.
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS
        refused = await h._cmd_task_claim({"next": True})
        assert (refused["result"], refused["reason"]) == ("drain_requested", "pool is disabled")

        await mktask(db, "t2", profile_id="worker")
        # A long poll ends on the switch rather than waiting out its deadline.
        waited = await h._cmd_task_claim({"next": True, "wait": 5})
        assert waited["result"] == "drain_requested"
        assert (await db.get_task("t2")).status == TaskStatus.READY

        # Enabling restores eligibility for the pool's next worker.
        await db.update_profile("worker", enabled=True)
        s2, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        assert (await scoped(handler, s2)._cmd_task_claim({"next": True}))["result"] == "claimed"

    async def test_disabling_a_pool_wakes_a_pending_long_poll(
        self, handler, db, tmp_path, monkeypatch
    ):
        """The switch reaches a claim that is *already* parked in a long poll.

        Without ``pool.enabled_changed`` among the wake events the disable is
        only noticed when the wait expires, so a worker keeps asking for work
        for up to ``swarm.claim_wait_max`` seconds after its pool was turned
        off.  Work that became ready in the meantime stays unclaimed, proving
        the wake-up re-checks the switch before attempting another claim.
        """
        sid, _ = await pool_session(db, tmp_path)
        waiting = asyncio.Event()
        attempt = handler._attempt_claim

        async def observe_attempt(*args, **kwargs):
            result = await attempt(*args, **kwargs)
            if result["result"] == "no_ready_work":
                waiting.set()
            return result

        monkeypatch.setattr(handler, "_attempt_claim", observe_attempt)
        t0 = time.monotonic()
        claim = asyncio.create_task(
            scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 5})
        )
        try:
            await asyncio.wait_for(waiting.wait(), timeout=5)
            # Ready work exists by the time the switch flips; creating the row
            # emits no ``task.ready``, so the poll is still parked on it.
            await mktask(db, "late", profile_id="worker")
            flip = await handler._cmd_pool_set_enabled(
                {"profile_id": "worker", "enabled": False}
            )
            assert flip["success"] is True
            result = await asyncio.wait_for(claim, timeout=5)
        finally:
            if not claim.done():
                claim.cancel()
                await asyncio.gather(claim, return_exceptions=True)
        assert (result["result"], result["reason"]) == ("drain_requested", "pool is disabled")
        assert time.monotonic() - t0 < 4.0  # woke on the event, not the deadline
        assert (await db.get_task("late")).status == TaskStatus.READY

    async def test_old_idle_pool_cannot_reuse_completed_context(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        await db.update_session(sid, claims=3)
        await mktask(db, "t2", profile_id="worker")
        result = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert result["result"] == "session_exhausted"
        assert (await db.get_session(sid)).desired_state == "stopped"
        assert (await db.get_task("t2")).status == TaskStatus.READY

    async def test_session_exhausted_after_cap_via_close_claim_next(self, handler, db, tmp_path):
        handler.config.swarm.fresh_context_per_task = False
        await db.update_profile("worker", max_claims_per_session=1)
        await mktask(db, "t1", profile_id="worker")
        await mktask(db, "t2", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        closed = await h._cmd_task_close(
            {
                "task_id": "t1",
                "outcome": "pass",
                "summary": "done",
                "claim_epoch": first["claim_epoch"],
                "claim_next": True,
            }
        )
        assert closed["success"] is True
        assert closed["next"]["result"] == "session_exhausted"
        assert not (wd / ".aq" / "claim.json").exists()
        assert (await db.get_task("t1")).status == TaskStatus.COMPLETED
        assert (await db.get_workspace_for_agent("agent-1")).locked_by_agent_id == "agent-1"

    async def test_not_admissible_when_project_paused(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await db.update_project(PROJECT_ID, status="PAUSED")
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["reason"]) == ("not_admissible", "project_inactive")

    async def test_not_admissible_when_budget_exhausted(self, handler, db, tmp_path):
        """review finding #2 — ``_admission_reason`` must read ``Project.budget_limit``,
        not the nonexistent ``token_budget``."""
        from src.scheduler import SchedulerState

        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await db.update_project(PROJECT_ID, budget_limit=50)
        handler.orchestrator._last_scheduler_state = SchedulerState(
            projects=[],
            tasks=[],
            agents=[],
            project_token_usage={PROJECT_ID: 100},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["reason"]) == ("not_admissible", "budget_exhausted")

    async def test_task_lifecycle_session_reclaims_own_task_only(self, handler, db, tmp_path):
        await db.create_agent(
            Agent(id="agent-1", name="agent-1", profile_id="worker", state=AgentState.BUSY)
        )
        await mktask(
            db,
            "mine",
            status=TaskStatus.IN_PROGRESS,
            profile_id="worker",
            assigned_agent_id="agent-1",
            claim_epoch=1,
        )
        await mktask(db, "other", profile_id="worker")
        await db.create_session(
            SessionRecord(
                id="s-task",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-task",
                lifecycle="task",
                task_id="mine",
                work_dir="/x",
                epoch="e",
                instance_token="t",
                started_at=NOW,
                state="running",
            )
        )
        h = scoped(handler, "s-task")
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        assert (await h._cmd_task_claim({"task_id": "other"}))["result"] == "out_of_scope"


def elevated(handler):
    handler._current_scope = None
    return handler


class TestSourceRepairDeliveryGate:
    """A queued source-CI repair is withheld only by a proof taken now.

    Delivery truth is request-scoped (``src/integration/source_delivery.py``).
    The command gate retires an unclaimed READY delegate after a fresh proof;
    no recorded delivery answer authorizes retirement and an unreachable Git
    observer withholds nothing. These tests isolate frontier exclusion; the
    real proof and retirement checks live in test_epic_pr_review_evidence.py.
    """

    @staticmethod
    def _train_project(db):
        async def enable():
            async with db.immediate() as conn:
                await conn.execute(
                    update(projects).where(projects.c.id == PROJECT_ID).values(
                        hierarchical_integration_mode="train",
                        hierarchical_integration_desired_mode="train",
                        integration_repository_id="repo",
                    )
                )

        return enable()

    async def test_excluded_task_ids_withholds_one_frontier_row(self, db, tmp_path):
        await mktask(db, "first", profile_id="worker")
        await mktask(db, "second", profile_id="worker")
        await pool_session(db, tmp_path)
        async with db.immediate() as conn:
            assert await db.select_ready_for_profile(
                conn, project_id=PROJECT_ID, profile_id="worker", agent_id="agent-1",
                excluded_task_ids={"first"},
            ) == "second"
            assert await db.select_ready_for_profile(
                conn, project_id=PROJECT_ID, profile_id="worker", agent_id="agent-1",
                excluded_task_ids={"first", "second"},
            ) is None
            assert await db.select_ready_for_profile(
                conn, project_id=PROJECT_ID, profile_id="worker", agent_id="agent-1",
            ) == "first"

    async def test_a_delivered_source_repair_is_not_leased(self, handler, db, tmp_path):
        await mktask(db, "repair", profile_id="worker")
        await pool_session(db, tmp_path)
        await self._train_project(db)
        handler._delivered_source_repairs = AsyncMock(return_value={"repair": object()})
        scoped(handler, "s1")
        # A named claim names the reason rather than reporting a conflict.
        named = await handler._cmd_task_claim({"task_id": "repair"})
        assert named["result"] == "no_ready_work"
        assert named["reason"] == "source_ci_repair_already_delivered"
        result = await handler._cmd_task_claim({"next": True})
        assert result["result"] == "no_ready_work"
        assert result["reason"] == "source_ci_repair_already_delivered"
        assert (await db.get_task("repair")).status is TaskStatus.READY
        assert (await db.get_session("s1")).task_id is None
        # Nothing withheld is an ordinary empty frontier again.
        handler._delivered_source_repairs = AsyncMock(return_value={})
        again = await handler._cmd_task_claim({"next": True})
        assert again["result"] == "no_ready_work" and "reason" not in again

    async def test_the_gate_fails_open_when_git_cannot_be_asked(self, handler, db, tmp_path):
        await mktask(db, "repair", profile_id="worker")
        await pool_session(db, tmp_path)
        await self._train_project(db)
        db.set_delivery_observer(None)
        assert await handler._delivered_source_repairs(PROJECT_ID) == {}
        db.set_delivery_observer(
            SimpleNamespace(observe=AsyncMock(side_effect=OSError("no route to host")))
        )
        assert await handler._delivered_source_repairs(PROJECT_ID) == {}

    async def test_the_gate_observes_before_the_claim_transaction_opens(
        self, handler, db, tmp_path
    ):
        """No git I/O and no lock across it, like every other observer consumer."""
        from contextlib import asynccontextmanager

        await mktask(db, "repair", profile_id="worker")
        await pool_session(db, tmp_path)
        await self._train_project(db)
        open_transactions = 0
        observed = []
        original = db.immediate

        @asynccontextmanager
        async def tracking_immediate(*args, **kw):
            nonlocal open_transactions
            open_transactions += 1
            try:
                async with original(*args, **kw) as conn:
                    yield conn
            finally:
                open_transactions -= 1

        async def gate(project_id):
            observed.append(open_transactions)
            return {}

        db.immediate = tracking_immediate
        handler._delivered_source_repairs = gate
        try:
            scoped(handler, "s1")
            await handler._cmd_task_claim({"next": True})
        finally:
            db.immediate = original
        assert observed == [0], "the gate ran while a claim transaction was open"


class TestContainerClaims:
    """An epic container is never leased, even while it is being filed (bold-flare-35).

    A planner used to file the epic as a plain task and reparent its children
    under it afterwards; a pool worker claimed the epic in between
    (prime-glacier.1, nimble-bridge.1).
    """

    async def _plain_epic(self, db, tid="epic", filed_by="planner-session"):
        """An epic filed the old way: a plain, claimable task from a planner."""
        await mktask(
            db, tid, profile_id="worker", created_by_kind="session", created_by_id=filed_by
        )

    async def _child_of(self, db, parent, child, *, filed_by=None, status=TaskStatus.DEFINED):
        await mktask(
            db, child, status=status, profile_id="worker",
            created_by_kind="session" if filed_by else None, created_by_id=filed_by,
        )
        async with db.immediate() as conn:
            await db.set_parent(child, parent, conn=conn)

    async def test_a_parent_without_the_container_flag_is_off_the_frontier(
        self, handler, db, tmp_path
    ):
        from sqlalchemy import delete

        await mktask(db, "epic", profile_id="worker")
        await self._child_of(db, "epic", "epic.1")
        # A parent row whose flag never landed (a legacy row, a crash between
        # writes): having a child is enough to keep it off the frontier.
        async with db.immediate() as conn:
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == "epic", task_metadata.c.key == "container"
                )
            )
        sid, _ = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"
        codes = [r["code"] for r in await db.claim_frontier_exclusions("epic")]
        assert codes == ["frontier_has_children"]

    async def test_declared_container_is_never_leased_before_or_after_children_arrive(
        self, handler, db, tmp_path
    ):
        handler.orchestrator.bus.emit = AsyncMock()
        created = await elevated(handler).execute("create_task", {
            "project_id": PROJECT_ID, "title": "Epic", "description": "epic",
            "container": True,
        })
        assert "task_id" in created, created
        epic = created["task_id"]
        # A filing names no route (mandatory task routing §5.1); give the epic
        # one anyway, so the claims below prove the container flag alone keeps
        # it off the frontier.
        assert await db.update_task_routing(
            epic, profile_id="worker",
            route_source="legacy", intelligence_class=None, preferred_workspace_id=None
        )
        async with db._engine.connect() as conn:
            assert await db.is_container(epic, conn=conn)
        # The cascade releases it the way it releases any flagged READY task,
        # and a childless *declared* container is held open, not settled.
        await handler.orchestrator._release_ready_containers([epic])
        task = await db.get_task(epic)
        assert (task.status, task.assigned_agent_id) == (TaskStatus.IN_PROGRESS, None)
        assert epic not in await db.settle_candidates()

        sid, _ = await pool_session(db, tmp_path)
        assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == (
            "no_ready_work"
        )
        # The planner's children are filed elsewhere and reparented under it.
        for n in (1, 2):
            await mktask(db, f"work-{n}", profile_id="worker")
            moved = await elevated(handler).execute(
                "reparent_task", {"task_id": f"work-{n}", "parent_id": epic}
            )
            assert moved["success"], moved
        claimed = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert claimed["result"] == "claimed"
        assert claimed["task"]["id"] in {"work-1", "work-2"}
        assert (await db.get_task(epic)).assigned_agent_id is None

    async def test_a_worker_session_cannot_declare_a_container(self, handler, db, tmp_path):
        # An empty declared container is held open with no timeout; a worker
        # files an epic with its children through a graph ``parent:`` block.
        handler.orchestrator.bus.emit = AsyncMock()
        await mktask(db, "plan", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        res = await h._cmd_create_task({
            "project_id": PROJECT_ID, "title": "Epic", "description": "epic",
            "reason": "the plan's epic", "container": True,
        })
        assert res["code"] == "hierarchy.container_not_for_sessions"
        assert {t.id for t in await db.list_tasks(PROJECT_ID)} == {"plan"}

    async def test_a_container_claimed_in_the_filing_window_is_released(
        self, handler, db, tmp_path
    ):
        handler.orchestrator.bus.emit = AsyncMock()
        await self._plain_epic(db)
        sid, _ = await pool_session(db, tmp_path)
        claimed = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert claimed["task"]["id"] == "epic"
        # Another session's planner reparents its children under it afterwards.
        await self._child_of(db, "epic", "epic.1", filed_by="planner-session")

        [row] = await db.list_container_claims()
        assert (row["agent_id"], row["task_id"], row["session_id"]) == ("agent-1", "epic", sid)

        released = await handler.orchestrator._release_container_claims()
        assert released == ["epic"]
        task = await db.get_task("epic")
        assert (task.status, task.assigned_agent_id) == (TaskStatus.IN_PROGRESS, None)
        session = await db.get_session(sid)
        assert session.task_id is None
        assert session.desired_state == "stopped"  # the seat is freed for a fresh worker
        assert session.last_claim_result == "container_released"
        agent = await db.get_agent("agent-1")
        assert (agent.state, agent.current_task_id) == (AgentState.IDLE, None)
        assert await db.list_container_claims() == []
        # Nothing else was disturbed: the child is still waiting for work.
        assert (await db.get_task("epic.1")).status is TaskStatus.DEFINED

    async def test_the_pool_reconcile_tick_releases_it(self, handler, db, tmp_path):
        handler.orchestrator.bus.emit = AsyncMock()
        await self._plain_epic(db)
        sid, _ = await pool_session(db, tmp_path)
        await scoped(handler, sid)._cmd_task_claim({"next": True})
        await self._child_of(db, "epic", "epic.1", filed_by="planner-session")

        await handler.orchestrator._reconcile_pools()
        await handler.orchestrator.wait_for_pool_launches(cancel=True)

        assert (await db.get_session(sid)).task_id is None
        assert (await db.get_task("epic")).assigned_agent_id is None

    async def test_a_released_parent_without_the_flag_is_flagged(self, handler, db, tmp_path):
        # Otherwise it would sit IN_PROGRESS with no agent, off the frontier
        # (``has_children``) and out of settlement (which needs the flag).
        from sqlalchemy import delete

        handler.orchestrator.bus.emit = AsyncMock()
        await self._plain_epic(db)
        sid, _ = await pool_session(db, tmp_path)
        await scoped(handler, sid)._cmd_task_claim({"next": True})
        await self._child_of(db, "epic", "epic.1", filed_by="planner-session")
        async with db.immediate() as conn:
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == "epic", task_metadata.c.key == "container"
                )
            )
        assert await handler.orchestrator._release_container_claims() == ["epic"]
        async with db._engine.connect() as conn:
            assert await db.is_container("epic", conn=conn)

    async def test_a_follow_up_filed_by_someone_else_keeps_the_claim(
        self, handler, db, tmp_path
    ):
        # Real work that picks up a supervisor's follow-up child is not an epic:
        # only children from the task's own filer mark it as one.
        handler.orchestrator.bus.emit = AsyncMock()
        await self._plain_epic(db)
        sid, _ = await pool_session(db, tmp_path)
        await scoped(handler, sid)._cmd_task_claim({"next": True})
        await self._child_of(db, "epic", "epic.1", filed_by="supervisor-session")

        assert await db.list_container_claims() == []
        assert await handler.orchestrator._release_container_claims() == []
        assert (await db.get_session(sid)).task_id == "epic"

    async def test_emergent_work_the_holder_filed_keeps_its_claim(self, handler, db, tmp_path):
        handler.orchestrator.bus.emit = AsyncMock()
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await scoped(handler, sid)._cmd_task_claim({"next": True})
        await self._child_of(db, "t1", "t1.1", filed_by=sid)
        # A child from elsewhere does not turn it into someone else's container
        # while the holder's own finding is under it too.
        await self._child_of(db, "t1", "t1.2", filed_by="other-session")

        assert await db.list_container_claims() == []
        assert await handler.orchestrator._release_container_claims() == []
        assert (await db.get_session(sid)).task_id == "t1"

    async def test_a_released_container_settles_once_its_children_are_done(
        self, handler, db, tmp_path
    ):
        handler.orchestrator.bus.emit = AsyncMock()
        await self._plain_epic(db)
        sid, _ = await pool_session(db, tmp_path)
        await scoped(handler, sid)._cmd_task_claim({"next": True})
        await self._child_of(
            db, "epic", "epic.1", filed_by="planner-session", status=TaskStatus.COMPLETED
        )
        assert await handler.orchestrator._release_container_claims() == ["epic"]
        assert (await db.get_task("epic")).status is TaskStatus.COMPLETED


class TestFence:
    async def test_stale_epoch_rejected_on_close(self, handler, db, tmp_path):
        handler.config.swarm.fresh_context_per_task = False
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})  # epoch 1
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=NOW)
        await h._cmd_task_claim({"next": True})  # epoch 2
        res = await h._cmd_task_close(
            {"task_id": "t1", "outcome": "pass", "summary": "x", "claim_epoch": 1}
        )
        assert res["result"] == "stale_claim"
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS

    async def test_pool_session_must_send_epoch(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        res = await h._cmd_task_close({"task_id": "t1", "outcome": "pass", "summary": "x"})
        assert res["result"] == "stale_claim"

    async def test_heartbeat_set_handoff_require_matching_epoch(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        assert (await h._cmd_task_heartbeat({"task_id": "t1", "claim_epoch": 1}))["success"]
        assert (await h._cmd_task_heartbeat({"task_id": "t1", "claim_epoch": 7}))[
            "result"
        ] == "stale_claim"
        assert (await h._cmd_task_set({"task_id": "t1", "note": "x", "claim_epoch": 7}))[
            "result"
        ] == "stale_claim"
        assert (await h._cmd_task_handoff(            {"task_id": "t1", "subject": "x", "claim_epoch": 7}))[
            "result"
        ] == "stale_claim"

    async def test_other_session_is_out_of_scope(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        s1, _ = await pool_session(db, tmp_path, sid="s1", agent_id="agent-1")
        s2, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        await scoped(handler, s1)._cmd_task_claim({"next": True})
        res = await scoped(handler, s2)._cmd_task_heartbeat({"task_id": "t1", "claim_epoch": 1})
        assert res["result"] == "out_of_scope"

    async def test_prime_resolves_task_from_session(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        res = await h._cmd_prime({})
        assert res["success"] is True
        body = res.get("body") or res.get("prompt") or json.dumps(res)
        assert "t1" in body and "Claim epoch: 1" in body


class TestReadScope:
    """I6: a pool token pins no ``task_id``, so ``project_id`` is the fence."""

    async def setup_other_project(self, db):
        await db.create_project(Project(id="other", name="o"))
        await db.create_task(
            Task(
                id="foreign",
                project_id="other",
                title="foreign",
                description="d",
                status=TaskStatus.READY,
            )
        )

    @pytest.mark.parametrize(
        "command",
        [
            "_cmd_get_task",
            "_cmd_task_show",
            "_cmd_task_children",
            "_cmd_task_progress",
            "_cmd_prime",
        ],
    )
    async def test_cross_project_read_refused(self, handler, db, tmp_path, command):
        await self.setup_other_project(db)
        sid, _ = await pool_session(db, tmp_path)
        res = await getattr(scoped(handler, sid), command)({"task_id": "foreign"})
        assert res["result"] == "out_of_scope"
        assert res["success"] is False

    @pytest.mark.parametrize(
        "command",
        [
            "_cmd_get_task",
            "_cmd_task_show",
            "_cmd_task_children",
            "_cmd_task_progress",
            "_cmd_prime",
        ],
    )
    async def test_same_project_read_allowed(self, handler, db, tmp_path, command):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        res = await getattr(scoped(handler, sid), command)({"task_id": "t1"})
        assert res.get("result") != "out_of_scope"
        assert "error" not in res

    async def test_local_scope_reads_any_project(self, handler, db, tmp_path):
        await self.setup_other_project(db)
        handler._current_scope = None
        res = await handler._cmd_get_task({"task_id": "foreign"})
        assert res["id"] == "foreign"

    async def test_elevated_session_reads_any_project(self, handler, db, tmp_path):
        await self.setup_other_project(db)
        sid, _ = await pool_session(db, tmp_path)
        scoped(handler, sid)
        handler._current_scope["elevated"] = True
        res = await handler._cmd_get_task({"task_id": "foreign"})
        assert res["id"] == "foreign"


class TestImplicitTaskId:
    """I3: the pool worker loop closes/heartbeats without naming a task."""

    async def test_close_resolves_held_task_from_scope(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        res = await h._cmd_task_close(
            {"outcome": "pass", "summary": "done", "claim_epoch": claimed["claim_epoch"]}
        )
        assert res["success"] is True
        assert (await db.get_task("t1")).status == TaskStatus.COMPLETED

    async def test_heartbeat_resolves_held_task_from_scope(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        res = await h._cmd_task_heartbeat({"claim_epoch": claimed["claim_epoch"]})
        assert res["success"] is True

    async def test_close_without_a_held_task_errors_clearly(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_close({"outcome": "pass"})
        assert res == {"success": False, "error": "no task_id and the session holds no task"}

    async def test_heartbeat_without_a_held_task_errors_clearly(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_heartbeat({})
        assert res == {"success": False, "error": "no task_id and the session holds no task"}


class TestSwarmDisabledGate:
    async def test_claim_refused_when_swarm_disabled(self, handler, db, tmp_path):
        """M8: the command stays callable but hands out no work."""
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        handler.config.swarm.enabled = False
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["reason"]) == ("not_admissible", "swarm_disabled")
        assert (await db.get_task("t1")).status == TaskStatus.READY


class TestActiveClaimWithDeletedTask:
    async def test_missing_held_task_returns_out_of_scope(self, handler, db, tmp_path):
        """M4: a held task row gone underneath the session must not AttributeError.

        ``sessions.task_id`` is a plain FK, so a straight ``delete_task``
        is refused while the session still points at it -- the row can only
        vanish through a path that clears the reference first (or on a
        backend/ordering where it does).  The re-claim's ``active`` branch
        has to survive the read coming back ``None`` either way, so the
        read is stubbed rather than the row contrived away.
        """
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        db._get_task_conn = AsyncMock(return_value=None)
        res = await h._cmd_task_claim({"next": True})
        assert res["result"] == "out_of_scope"
        assert "no longer exists" in res["reason"]


class TestStaleClaimBinding:
    """A binding whose task left IN_PROGRESS must never be re-served.

    ``take_claim_slot`` decides "already active" from ``sessions.task_id``
    alone.  A release that did not land (an attached integration owner
    vetoes ``_release_claim_on``) leaves the session naming a task that is
    back on the frontier, and the old ``active`` branch handed it straight
    back as ``claimed`` -- a task the worker could neither comment on nor
    close, re-offered on every claim until the lease reaper killed it.
    """

    @staticmethod
    async def _strand(db, sid, tid):
        """Move the held task off IN_PROGRESS without releasing the session."""
        await db.transition_task(
            tid, TaskStatus.READY, context="test", force=True, assigned_agent_id=None
        )
        assert (await db.get_session(sid)).task_id == tid

    async def test_stale_binding_is_released_and_the_task_reclaimed(
        self, handler, db, tmp_path
    ):
        handler.config.swarm.fresh_context_per_task = False  # let the same session re-claim
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        assert first["result"] == "claimed"
        await self._strand(db, sid, "t1")

        again = await h._cmd_task_claim({"next": True})

        # A real claim, not the old phantom: fresh epoch, IN_PROGRESS task,
        # and a claim file the fence commands can read.
        assert again["result"] == "claimed"
        assert again["claim_epoch"] > first["claim_epoch"]
        task = await db.get_task("t1")
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.assigned_agent_id == "agent-1"
        assert json.loads((wd / ".aq" / "claim.json").read_text())["claim_epoch"] == (
            again["claim_epoch"]
        )

    async def test_stale_binding_reports_no_ready_work_when_nothing_is_ready(
        self, handler, db, tmp_path
    ):
        handler.config.swarm.fresh_context_per_task = False
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        await db.transition_task(
            "t1", TaskStatus.BLOCKED, context="test", force=True, assigned_agent_id=None
        )

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "no_ready_work"
        session = await db.get_session(sid)
        assert (session.task_id, session.claim_phase) == (None, None)

    async def test_stale_binding_retires_a_fresh_context_worker(
        self, handler, db, tmp_path
    ):
        """The default one-claim-per-session cap turns the repair into an exit.

        ``fresh_context_per_task`` caps the session at a single claim, which
        the stranded task already spent.  ``session_exhausted`` is what the
        pool loop is told to exit on, so the worker leaves cleanly instead of
        spinning on a task it cannot close.
        """
        assert handler.config.swarm.fresh_context_per_task is True
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        await self._strand(db, sid, "t1")

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "session_exhausted"
        session = await db.get_session(sid)
        assert (session.task_id, session.desired_state) == (None, "stopped")

    async def test_vetoed_release_drains_instead_of_re_serving(
        self, handler, db, tmp_path
    ):
        """The integration-owner veto is evidence, not something to erase.

        ``release_claim`` refuses while an attached owner still names this
        session/workspace pair, so the binding cannot be unwound here.  The
        claim must stop the loop rather than hand back an unclosable task;
        the stall stays visible on the task and the reconciler owns cleanup.
        """
        from src.database.queries.task_queries import TransitionResult

        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        await self._strand(db, sid, "t1")
        db.release_claim = AsyncMock(return_value=TransitionResult())

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "drain_requested"
        assert "t1" in res["reason"]
        assert (await db.get_session(sid)).desired_state == "stopped"
        assert await db.get_task_meta("t1", "needs_attention") == "claim_binding_stale"
        # The claim file is the handoff's evidence: it stays put.
        assert (wd / ".aq" / "claim.json").exists()

    async def test_a_reclaim_between_observation_and_release_is_left_alone(
        self, handler, db, tmp_path
    ):
        """The repair must not clobber the worker that legitimately took over.

        ``_attempt_claim`` reads the stranded task in one transaction and
        ``_recover_stale_binding`` releases it in a later one.  In between,
        the task is READY on the frontier and any pool worker may claim it.
        The release replays a status/epoch it no longer holds, so it must
        match nothing: the new owner keeps the task IN_PROGRESS, keeps its
        ``assigned_agent_id``, and this session drains instead.
        """
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        await self._strand(db, sid, "t1")

        real_release = db.release_claim

        async def claim_from_under_us(*a, **kw):
            # Exactly the interleaving: the other worker's claim commits
            # between our observation and our release transaction.
            async with db.immediate() as conn:
                assert await db.take_task(conn, "t1", agent_id="agent-2", now=time.time())
            db.release_claim = real_release
            return await real_release(*a, **kw)

        db.release_claim = claim_from_under_us

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "drain_requested"
        task = await db.get_task("t1")
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.assigned_agent_id == "agent-2"
        # The other worker's task is not flagged for a stall that is ours.
        assert await db.get_task_meta("t1", "needs_attention") is None
        assert (await db.get_session(sid)).desired_state == "stopped"

    async def test_a_task_reclaimed_before_the_claim_is_never_re_served(
        self, handler, db, tmp_path
    ):
        """The same binding, one step on: IN_PROGRESS under someone else.

        ``take_claim_slot`` still answers ``active`` from ``sessions.task_id``,
        and the task is IN_PROGRESS again -- so an IN_PROGRESS check alone
        would hand a second session the task agent-2 is working.  Nothing
        about the task may be touched here.
        """
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        await self._strand(db, sid, "t1")
        async with db.immediate() as conn:
            assert await db.take_task(conn, "t1", agent_id="agent-2", now=time.time())
        before = await db.get_task("t1")

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "drain_requested"
        assert "agent-2" in res["reason"]
        after = await db.get_task("t1")
        assert (after.status, after.assigned_agent_id, after.claim_epoch) == (
            TaskStatus.IN_PROGRESS,
            "agent-2",
            before.claim_epoch,
        )
        assert (await db.get_session(sid)).desired_state == "stopped"



class TestEventWaiter:
    async def test_waiter_subscribes_before_check(self):
        from src.event_bus import EventBus

        bus = EventBus()
        w = bus.waiter(["task.ready"], filter={"project_id": "p"})
        await bus.emit("task.ready", {"task_id": "t", "project_id": "p", "title": "t"})
        assert (await w.wait(0.5))["task_id"] == "t"
        w.close()
        assert bus.subscriber_count("task.ready") == 0


class TestTaskShowClaimedBy:
    """``task_show.claimed_by`` — spec §14.

    Assembled from ``task_metadata.claimed_by_session``,
    ``tasks.assigned_agent_id`` and ``tasks.claim_epoch``.
    """

    async def test_unclaimed_task_reports_none(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        res = await handler._cmd_task_show({"task_id": "t1"})
        assert res["claimed_by"] is None

    async def test_claimed_task_reports_holder(self, handler, db, tmp_path):
        handler.orchestrator.bus.emit = AsyncMock()
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        claim = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert claim["result"] == "claimed"

        res = await handler._cmd_task_show({"task_id": "t1"})
        assert res["claimed_by"] == {
            "session_id": sid,
            "agent_id": "agent-1",
            "claim_epoch": claim["claim_epoch"],
        }

    async def test_claim_epoch_matches_the_fence_handed_to_the_worker(self, handler, db, tmp_path):
        handler.orchestrator.bus.emit = AsyncMock()
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        await scoped(handler, sid)._cmd_task_claim({"next": True})

        on_disk = json.loads((wd / ".aq" / "claim.json").read_text())
        res = await handler._cmd_task_show({"task_id": "t1"})
        assert res["claimed_by"]["claim_epoch"] == on_disk["claim_epoch"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_prepare_timeout_waits_for_live_reset(handler, db, config, tmp_path, cancel, monkeypatch):
    await mktask(db, "t1", profile_id="worker")
    sid, _ = await pool_session(db, tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()

    async def reset(*args, **kwargs):
        started.set()
        await finish.wait()
        return "aq/t1"

    handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = reset
    request = asyncio.create_task(scoped(handler, sid)._cmd_task_claim({"next": True}))
    reconciler = SessionReconciler(
        db, config, SessionProviderRegistry({}), bus=handler.orchestrator.bus,
        orchestrator=handler.orchestrator, epoch="test",
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        row = await db.get_session(sid)
        key = (sid, "t1", row.last_claim_epoch)
        assert handler.orchestrator.claim_preparations[key] is request
        # Both the automatic timeout and doctor repair must respect the
        # same actual request, rather than inferring abandonment from age.
        from src.doctor.pool_checks import run_check

        await db.update_session(sid, claim_phase_at=time.time() - 3 * config.swarm.prepare_timeout)
        finding = await run_check(
            db, "pools.preparing_stuck", config=config, handler=handler, repair=True
        )
        assert finding.data.get("count", 0) == 0
        expired = row.claim_phase_at + config.swarm.prepare_timeout + 1
        await reconciler._step_prepare_timeout([row], expired)
        assert (await db.get_session(sid)).task_id == "t1"
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        if cancel:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not handler.orchestrator.claim_preparations
            await reconciler._step_prepare_timeout([row], expired)
            assert (await db.get_task("t1")).status is TaskStatus.READY
            assert (await db.get_session(sid)).task_id is None
        else:
            finish.set()
            assert (await request)["result"] == "claimed"
            assert (await db.get_session(sid)).claim_phase == "active"
            assert not handler.orchestrator.claim_preparations
            # A stale timeout read racing activation cannot release it even
            # after the request leaves the in-memory preparation registry.
            await db.release_claim(
                sid, task_status=TaskStatus.READY, context="prepare_timeout",
                now=expired, expected_task_id="t1", expected_claim_epoch=row.last_claim_epoch,
                preparation_expired_before=expired - config.swarm.prepare_timeout,
            )
            assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
            assert (await db.get_session(sid)).claim_phase == "active"
            real_list = db.list_sessions

            async def stale_observation(**kwargs):
                if kwargs.get("claim_phase") == "preparing":
                    return [row]
                return await real_list(**kwargs)

            monkeypatch.setattr(db, "list_sessions", stale_observation)
            waiter = asyncio.get_running_loop().create_future()
            handler.orchestrator.claim_waiters[(sid, row.last_claim_epoch)] = waiter
            await reconciler._step_prepare_timeout([row], expired)
            assert not waiter.done(), "a rejected stale timeout must not wake claim waiters"
            waiter.cancel()
            handler.orchestrator.claim_waiters.clear()
            assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
    finally:
        finish.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.fixture
async def development_admission(handler, db, tmp_path):
    """Actual remote ancestry, no publisher receipt or live incident tasks."""
    import subprocess
    from src.git.manager import GitManager
    from src.integration.development import DevelopmentPrimitives
    from src.models import TaskCompletion

    def git(path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    remote, source = tmp_path / "remote.git", tmp_path / "source"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "clone", str(remote), str(source))
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.test")
    (source / "base").write_text("base")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "main")
    git(source, "checkout", "-b", "prerequisite")
    (source / "work").write_text("complete source")
    git(source, "add", ".")
    git(source, "commit", "-m", "prerequisite")
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "prerequisite")
    await db.create_repo(
        RepoConfig(
            id="repo",
            project_id=PROJECT_ID,
            url=str(remote),
            source_type=RepoSourceType.CLONE,
        )
    )
    await db.update_project(
        PROJECT_ID, hierarchical_integration_mode="development", integration_repository_id="repo"
    )
    await mktask(
        db, "prerequisite", status=TaskStatus.COMPLETED, repo_id="repo", branch_name="prerequisite"
    )
    await db.save_task_completion(
        TaskCompletion(
            id="close-1",
            task_id="prerequisite",
            outcome="pass",
            commits=[head],
            completed_at=time.time(),
        )
    )
    # As a worker close does: the exact final source is retained in git.
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

    await GitProvenance(GitManager(), str(source), repository_url=str(remote)).write_completion(
        CompletedSource(CompletionIdentity(PROJECT_ID, "repo", "prerequisite", "close-1"), head)
    )
    handler.orchestrator.git = GitManager()
    service = DevelopmentPrimitives(db, data_dir=tmp_path / "truth", git=handler.orchestrator.git)
    handler.orchestrator.development_integration = service
    await mktask(db, "dependent", profile_id="worker", repo_id="repo")
    await db.add_dependency("dependent", "prerequisite")
    return SimpleNamespace(
        git=git, source=source, remote=remote, base=base, head=head, service=service
    )


@pytest.mark.parametrize("child_work", ["none", "local", "remote"])
@pytest.mark.parametrize("preparation", ["pool", "clone"])
@pytest.mark.parametrize("prerequisite_policy", ["wait-for-parent", "stacked"])
async def test_hierarchy_claim_inherits_git_proven_parent_without_resetting_child(
    handler, db, tmp_path, development_admission, child_work, preparation, prerequisite_policy
):
    from src.integration.delivery_observer import DeliveryObserver
    from src.integration.git_truth import GitTruth
    from src.orchestrator.worktree_manager import WorktreeSlotManager

    env = development_admission
    git = env.git
    await mktask(db, "epic", status=TaskStatus.IN_PROGRESS, repo_id="repo",
                 branch_name="aq/epic")
    await db.add_dependency("dependent", "epic", "parent-child")
    await db.add_dependency("prerequisite", "epic", "parent-child")
    await db.update_task("dependent", branch_name="aq/dependent", is_blocked=False)
    # Both branches were created before the prerequisite reached the parent.
    git(env.source, "push", "origin", f"{env.base}:refs/heads/aq/epic",
        f"{env.base}:refs/heads/aq/dependent")
    child_sha = env.base
    if child_work == "remote":
        git(env.source, "checkout", "-b", "aq/dependent", env.base)
        (env.source / "child-work").write_text("already published")
        git(env.source, "add", ".")
        git(env.source, "commit", "-m", "child work")
        child_sha = git(env.source, "rev-parse", "HEAD")
        git(env.source, "push", "origin", "aq/dependent")
    sid, work_dir = await pool_session(db, tmp_path)
    git(tmp_path, "clone", str(env.remote), str(work_dir))
    if child_work == "local":
        git(work_dir, "checkout", "-b", "aq/dependent", env.base)
        git(work_dir, "config", "user.name", "Test")
        git(work_dir, "config", "user.email", "test@example.test")
        (work_dir / "child-work").write_text("local worker commit")
        git(work_dir, "add", ".")
        git(work_dir, "commit", "-m", "child work")
        child_sha = git(work_dir, "rev-parse", "HEAD")
    async with db.immediate() as conn:
        await conn.execute(task_branch_origins.insert().values(
            id="origin-dependent", task_id="dependent", repository_id="repo",
            branch_name="aq/dependent", parent_task_id="epic", parent_repository_id="repo",
            parent_ref="aq/epic", base_sha=env.base, creation_generation=1,
            reserved=True, materialized=True, created_at=time.time(),
        ))
        await conn.execute(task_integration_checkpoints.insert().values(
            task_id="dependent", repository_id="repo", branch="aq/dependent",
            checkpoint_sha=env.base, updated_at=time.time(),
        ))
        await conn.execute(task_integration_checkpoints.insert().values(
            task_id="prerequisite", repository_id="repo", branch="prerequisite",
            checkpoint_sha=env.head, updated_at=time.time(),
        ))
    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train",
                            repo_url=str(env.remote), hierarchical_integration_policy={
                                "prerequisite_branches": prerequisite_policy,
                            })
    ownership = BranchOwnership(db)
    target = BranchKey(repository_id="repo", branch="aq/dependent")
    await ownership.acquire(target, "dependent", "worker")
    transport = handler.orchestrator.git
    db.set_prerequisite_observer(DeliveryObserver(
        db, git=transport, truth=GitTruth(transport), data_dir=tmp_path / "prerequisites",
    ))
    manager = WorktreeSlotManager(
        db=db, git=transport, bus=handler.orchestrator.bus,
        config=handler.config.worktrees, git_mutex=handler.orchestrator._git_mutex,
    )
    handler.orchestrator._worktree_slots = MagicMock(return_value=manager)
    parent_sha = env.head
    if prerequisite_policy == "wait-for-parent":
        result = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert result["result"] == "no_ready_work"
        git(env.source, "checkout", "-b", "aq/epic", env.base)
        git(env.source, "merge", "--no-ff", "-m", "deliver prerequisite", env.head)
        parent_sha = git(env.source, "rev-parse", "HEAD")
        git(env.source, "push", "origin", "aq/epic")
    # Main moves independently. Its new content must never reach this child.
    git(env.source, "checkout", "main")
    (env.source / "main-only").write_text("unrelated")
    git(env.source, "add", ".")
    git(env.source, "commit", "-m", "main only")
    git(env.source, "push", "origin", "main")

    if preparation == "pool":
        result = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert result["result"] == "claimed", result
        assert result["task"]["id"] == "dependent"
    else:
        # The task launcher follows a fresh scheduler observation. Refresh the
        # advisory snapshot used by its guard after the earlier negative claim.
        if prerequisite_policy == "wait-for-parent":
            assert await db.hierarchy_prerequisite_delivery_head("dependent") == parent_sha
        task = await db.get_task("dependent")
        project = await db.get_project(PROJECT_ID)
        origin, fence, _role = await handler.orchestrator._hierarchy_origin_and_fence(task, project)
        workspace = await db.get_workspace_for_agent("agent-1")
        assert await handler.orchestrator._prepare_exact_origin_workspace(
            task, project, SimpleNamespace(workspace=workspace), origin, fence,
        ) == "aq/dependent"
    assert git(work_dir, "branch", "--show-current") == "aq/dependent"
    git(work_dir, "merge-base", "--is-ancestor", parent_sha, "HEAD")
    git(work_dir, "merge-base", "--is-ancestor", child_sha, "HEAD")
    assert (work_dir / "work").read_text() == "complete source"
    assert not (work_dir / "main-only").exists()
    if child_work != "none":
        assert (work_dir / "child-work").exists()
    origin = await db.get_task_branch_origin_for_promotion("dependent", "repo")
    assert origin["base_sha"] == env.base
    if prerequisite_policy == "stacked":
        assert origin["stack_snapshot"]["base_sha"] == env.head
        assert git(env.source, "ls-remote", "origin", "refs/heads/aq/epic").split()[0] == env.base
    owner = await ownership.get_owner(target)
    if preparation == "pool":
        assert owner["handoff_state"] == "attached" and owner["session_id"] == sid
    else:
        assert owner["handoff_state"] == "reserved"


@pytest.mark.parametrize("conflict_kind, repair_result", [
    ("incarnation", "pass"), ("merge", "pass"), ("merge", "moved"),
    ("merge", "unrelated"), ("merge", "no-commit"), ("merge", "same-head"),
    ("merge", "missing"),
    ("generated", "pass"), ("generator-exit", "pass"), ("generator-timeout", "pass"),
])
async def test_conflicting_multiple_stacked_prerequisites_cannot_activate_claim(
    handler, db, tmp_path, development_admission, monkeypatch, conflict_kind, repair_result
):
    from pathlib import Path

    from src.integration.delivery_observer import DeliveryObserver
    from src.integration.git_truth import GitTruth
    from src.models import TaskCompletion
    from src.orchestrator.worktree_manager import WorktreeSlotManager

    env = development_admission
    git = env.git
    generated = conflict_kind in {"generated", "generator-exit", "generator-timeout"}
    if generated:
        from tests.test_generated_artifacts import _catalogue_branches

        fixture = tmp_path / "generated-inputs"
        fixture.mkdir()
        repo, env.base, env.head, second = _catalogue_branches(fixture)
        git(env.source, "fetch", str(repo), env.base, env.head, second)
        git(env.source, "push", "--force", "origin", f"{env.head}:refs/heads/prerequisite",
            f"{second}:refs/heads/second-prerequisite")
        await db.save_task_completion(TaskCompletion(
            id="generated-first", task_id="prerequisite", outcome="pass", commits=[env.head],
            completed_at=time.time(),
        ))
    await mktask(db, "epic", status=TaskStatus.IN_PROGRESS, repo_id="repo",
                 branch_name="aq/epic")
    git(env.source, "push", "origin", f"{env.base}:refs/heads/aq/epic",
        f"{env.base}:refs/heads/aq/dependent")
    if not generated:
        git(env.source, "checkout", "-b", "second-prerequisite", env.base)
        (env.source / ("work" if conflict_kind == "merge" else "second-work")).write_text(
            "second sibling source",
        )
        git(env.source, "add", ".")
        git(env.source, "commit", "-m", "conflicting second prerequisite")
        second = git(env.source, "rev-parse", "HEAD")
        git(env.source, "push", "origin", "second-prerequisite")
    await mktask(db, "second-prerequisite", status=TaskStatus.COMPLETED, repo_id="repo",
                 branch_name="second-prerequisite")
    await db.save_task_completion(TaskCompletion(
        id="close-second", task_id="second-prerequisite", outcome="pass", commits=[second],
    ))
    for tid in ("dependent", "prerequisite", "second-prerequisite"):
        await db.add_dependency(tid, "epic", "parent-child")
    await db.add_dependency("dependent", "second-prerequisite")
    await db.update_task("dependent", branch_name="aq/dependent", is_blocked=False)
    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train",
                            repo_url=str(env.remote), hierarchical_integration_policy={
                                "prerequisite_branches": "stacked",
                            })
    async with db.immediate() as conn:
        await conn.execute(task_branch_origins.insert().values(
            id="origin-dependent", task_id="dependent", repository_id="repo",
            branch_name="aq/dependent", parent_task_id="epic", parent_repository_id="repo",
            parent_ref="aq/epic", base_sha=env.base, creation_generation=1,
            reserved=True, materialized=True, created_at=time.time(),
        ))
        for tid, branch, head in (("epic", "aq/epic", env.base),
                                 ("dependent", "aq/dependent", env.base),
                                 ("prerequisite", "prerequisite", env.head),
                                 ("second-prerequisite", "second-prerequisite", second)):
            await conn.execute(task_integration_checkpoints.insert().values(
                task_id=tid, repository_id="repo", branch=branch,
                checkpoint_sha=head, updated_at=time.time(),
            ))
    sid, work_dir = await pool_session(db, tmp_path)
    git(tmp_path, "clone", str(env.remote), str(work_dir))
    transport = handler.orchestrator.git
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/dependent"), "dependent", "worker",
    )
    if conflict_kind in {"generator-exit", "generator-timeout"}:
        from src.integration import regeneration
        from src.integration.development import GENERATED_MERGE_CONFIG

        run_git = transport.arun_git_result
        merge_generated = regeneration.merge_generated_tree
        clean_merges = []
        regeneration_inputs = []

        async def clean_merge(args, **kwargs):
            if "merge-tree" in args:
                # The observer is a no-checkout clone. Install the fixture's
                # generated attribute so its driver resolves the text overlap.
                attributes = Path(kwargs["cwd"]) / ".git/info/attributes"
                attributes.write_text("tests/selection_catalogue.json merge=aq-generated\n")
                result = await run_git([*GENERATED_MERGE_CONFIG, *args], **kwargs)
                clean_merges.append(result.returncode)
                return result
            return await run_git(args, **kwargs)

        async def failing_generator(*args, **kwargs):
            regeneration_inputs.append(tuple(args[2][-2:]))
            kwargs["command"] = (
                "python3 -c 'raise SystemExit(1)'" if conflict_kind == "generator-exit"
                else "python3 -c 'import time; time.sleep(30)'"
            )
            kwargs["timeout_seconds"] = 1
            return await merge_generated(*args, **kwargs)

        monkeypatch.setattr(transport, "arun_git_result", clean_merge)
        monkeypatch.setattr(regeneration, "merge_generated_tree", failing_generator)
    db.set_prerequisite_observer(DeliveryObserver(
        db, git=transport, truth=GitTruth(transport), data_dir=tmp_path / "prerequisites",
    ))
    manager = WorktreeSlotManager(
        db=db, git=transport, bus=handler.orchestrator.bus,
        config=handler.config.worktrees, git_mutex=handler.orchestrator._git_mutex,
    )
    handler.orchestrator._worktree_slots = MagicMock(return_value=manager)
    if conflict_kind == "incarnation":
        import src.integration.stacked_branches as module

        observe = module.observe_stacks

        async def reopened_after_observation(*args, **kwargs):
            view = await observe(*args, **kwargs)
            await db.transition_task("second-prerequisite", TaskStatus.READY, force=True)
            return view

        monkeypatch.setattr(module, "observe_stacks", reopened_after_observation)
    result = await scoped(handler, sid)._cmd_task_claim({"next": True})
    if conflict_kind == "generated":
        assert result["result"] == "claimed", result
        assert result["task"]["id"] == "dependent"
        for head in (env.base, env.head, second):
            git(work_dir, "merge-base", "--is-ancestor", head, "HEAD")
        catalogue = json.loads((work_dir / "tests/selection_catalogue.json").read_text())
        assert set(catalogue["modules"]) == {"tests/test_a.py", "tests/test_b.py", "tests/test_c.py"}
        origin = await db.get_task_branch_origin_for_promotion("dependent", "repo")
        assert set(origin["stack_snapshot"]["prerequisites"]) == {
            "prerequisite", "second-prerequisite",
        }
        rebuilt = origin["stack_snapshot"]["regenerations"][0]
        assert rebuilt["files"] == ["tests/selection_catalogue.json"]
        assert rebuilt["prerequisites"] == origin["stack_snapshot"]["prerequisites"]
        git(work_dir, "merge-base", "--is-ancestor", rebuilt["commit"], "HEAD")
        assert (await db.get_session(sid)).claim_phase == "active"
        assert (work_dir / ".aq" / "claim.json").exists()
        assert await db.get_task_meta("dependent", "stack_prerequisites_conflict") is None
        return
    assert result["result"] == "no_ready_work", result
    assert (await db.get_task("dependent")).status is TaskStatus.READY
    assert (await db.get_session(sid)).task_id is None
    assert not (work_dir / ".aq" / "claim.json").exists()
    assert git(env.remote, "rev-parse", "refs/heads/aq/dependent") == env.base
    assert git(env.remote, "rev-parse", "refs/heads/prerequisite") == env.head
    assert git(env.remote, "rev-parse", "refs/heads/second-prerequisite") == second
    if conflict_kind != "incarnation":
        assert "stack_prerequisites_conflict" in result["reason"]
        detail = await db.get_task_meta("dependent", "stack_prerequisites_conflict")
        assert detail["files"] == (["tests/selection_catalogue.json"] if generated else ["work"])
        assert set(detail["prerequisites"]) == {"prerequisite", "second-prerequisite"}
        repair = await db.get_task(detail["repair_task_id"])
        assert repair.parent_task_id == "epic" and repair.status is TaskStatus.DEFINED
        assert repair.class_hint == "standard-medium"
        assert repair.prefer_target == "worker"
        for _ in range(4):
            assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "no_ready_work"
        assert (await db.get_task("dependent")).status is TaskStatus.READY
        assert await db.get_task_meta("dependent", "needs_attention") == "stack_prerequisites_conflict"
        assert await db.get_task_meta("dependent", "slot_reset_failure") is None
        assert await db.get_task_meta("dependent", "claim_prepare_backoff_attempts") == 1
        assert await db.get_task_meta("dependent", "claim_prepare_backoff_until") > time.time()
        assert await db.get_task_meta("dependent", "stack_prerequisites_conflict") == detail
        if generated:
            # The first source fast-forwards the parent; only the sibling merge
            # needs merge-tree and canonical regeneration.
            assert clean_merges == [0]
            assert regeneration_inputs == [(env.head, second)]
            assert ("exited 1" if conflict_kind == "generator-exit" else "timed out") in detail[
                "reason"
            ]
            return
        from src.integration.hierarchy import HierarchyIntegration

        def materialize(repository, branch, head):
            git(env.source, "push", "origin", f"{head}:refs/heads/{branch}")
            return head

        async with db._engine.connect() as conn:
            repair_origin = (await conn.execute(select(task_branch_origins).where(
                task_branch_origins.c.task_id == repair.id,
            ))).mappings().one()
        await HierarchyIntegration(db, branch_materializer=materialize).materialize_origin(
            repair_origin["id"],
        )
        git(env.source, "checkout", "-b", repair.branch_name, detail["starting_head"])
        git(env.source, "merge", "--no-ff", "-m", "merge first prerequisite", env.head)
        import subprocess

        conflict = subprocess.run(["git", "merge", "--no-commit", second], cwd=env.source,
                                  capture_output=True)
        assert conflict.returncode == 1
        (env.source / "work").write_text("resolved prerequisites")
        git(env.source, "add", ".")
        git(env.source, "commit", "-m", "resolve prerequisites")
        resolved = git(env.source, "rev-parse", "HEAD")
        git(env.source, "push", "origin", repair.branch_name)
        if repair_result == "unrelated":
            git(env.source, "checkout", "--orphan", "unrelated-repair")
            git(env.source, "rm", "-rf", ".")
            (env.source / "unrelated").write_text("unrelated history")
            git(env.source, "add", ".")
            git(env.source, "commit", "-m", "unrelated repair")
            resolved = git(env.source, "rev-parse", "HEAD")
            git(env.source, "push", "--force", "origin", f"{resolved}:{repair.branch_name}")
        elif repair_result == "same-head":
            resolved = detail["starting_head"]
            git(env.source, "push", "--force", "origin", f"{resolved}:{repair.branch_name}")
        await db.save_task_completion(TaskCompletion(
            id="repair-close", task_id=repair.id, outcome="pass",
            commits=[] if repair_result == "no-commit" else [resolved],
        ))
        await db.transition_task(repair.id, TaskStatus.COMPLETED, force=True)
        if repair_result == "moved":
            (env.source / "unreviewed").write_text("moved after close")
            git(env.source, "add", ".")
            git(env.source, "commit", "-m", "move after close")
            git(env.source, "push", "origin", repair.branch_name)
        elif repair_result == "missing":
            git(env.source, "push", "origin", "--delete", repair.branch_name)
        await db.set_task_meta("dependent", "claim_prepare_backoff_until", time.time() - 1)
        await BranchOwnership(db).acquire(BranchKey(repository_id="repo", branch="aq/dependent"),
                                          "dependent", "worker")
        recovered = await scoped(handler, sid)._cmd_task_claim({"next": True})
        if repair_result != "pass":
            assert recovered["result"] == "no_ready_work", recovered
            replacement = await db.get_task_meta("dependent", "stack_prerequisites_conflict")
            assert replacement["repair_task_id"] != repair.id
            assert replacement["superseded_repair_task_id"] == repair.id
            assert await db.get_task_meta("dependent", "claim_prepare_backoff_until") > time.time()
            epoch = (await db.get_task("dependent")).claim_epoch
            # Even after backoff expires, the new pending repair withholds this
            # task. Repeated long-polls cannot consume more preparations.
            await db.set_task_meta("dependent", "claim_prepare_backoff_until", time.time() - 1)
            for _ in range(3):
                assert (await scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 1}))[
                    "result"
                ] == "no_ready_work"
            assert (await db.get_task("dependent")).claim_epoch == epoch
            assert await db.get_task_meta("dependent", "stack_prerequisites_conflict") == replacement
            assert await db.get_task_meta("dependent", "slot_reset_failure") is None
            await mktask(db, "other-ready", profile_id="worker", repo_id="repo", priority=0,
                         branch_name="aq/other-ready")
            git(env.source, "push", "origin", f"{env.base}:refs/heads/aq/other-ready")
            async with db.immediate() as conn:
                await db.set_parent("other-ready", "epic", integration_authorized=True, conn=conn)
                await conn.execute(task_branch_origins.insert().values(
                    id="origin-other", task_id="other-ready", repository_id="repo",
                    branch_name="aq/other-ready", parent_task_id="epic",
                    parent_repository_id="repo", parent_ref="aq/epic", base_sha=env.base,
                    creation_generation=1, reserved=True, materialized=True, created_at=time.time(),
                ))
                await conn.execute(task_integration_checkpoints.insert().values(
                    task_id="other-ready", repository_id="repo", branch="aq/other-ready",
                    checkpoint_sha=env.base, updated_at=time.time(),
                ))
            await BranchOwnership(db).acquire(
                BranchKey(repository_id="repo", branch="aq/other-ready"),
                "other-ready", "worker",
            )
            other = await scoped(handler, sid)._cmd_task_claim({"next": True})
            assert other["result"] == "claimed", other
            assert other["task"]["id"] == "other-ready"
            assert (await db.get_task("dependent")).claim_epoch == epoch
            return
        assert recovered["result"] == "claimed", recovered
        assert recovered["task"]["id"] == "dependent"
        for prerequisite in (env.head, second, resolved):
            git(work_dir, "merge-base", "--is-ancestor", prerequisite, "HEAD")
        assert await db.get_task_meta("dependent", "needs_attention") is None
        assert await db.get_task_meta("dependent", "stack_prerequisites_conflict") is None
        assert await db.get_task_meta("dependent", "claim_prepare_backoff_until") is None


@pytest.mark.parametrize("completion_kind", [
    "no_change", "commits", "changed_empty", "contained_empty", "missing",
])
async def test_cross_epic_no_change_readiness_demand_explain_and_claim_agree(
    handler, db, tmp_path, development_admission, completion_kind,
):
    from src.integration.delivery_observer import DeliveryObserver
    from src.integration.git_truth import GitTruth
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
    from src.models import TaskCompletion
    from src.orchestrator.worktree_manager import WorktreeSlotManager
    from src.scheduler import PoolKey

    env = development_admission
    source = env.head if completion_kind in {
        "commits", "changed_empty", "contained_empty",
    } else env.base
    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train")
    await db.remove_dependency("dependent", "prerequisite")
    await db.update_task("dependent", branch_name="aq/dependent")
    await mktask(db, "no-change", status=TaskStatus.COMPLETED, repo_id="repo",
                 branch_name="aq/no-change")
    await db.add_dependency("dependent", "no-change")
    env.git(env.source, "push", "origin", f"{source}:refs/heads/aq/no-change",
            f"{env.base}:refs/heads/aq/dependent")
    if completion_kind == "contained_empty":
        env.git(env.source, "push", "origin", f"{source}:refs/heads/main")
    async with db.immediate() as conn:
        await conn.execute(task_branch_origins.insert().values(
            id="origin-dependent", task_id="dependent", repository_id="repo",
            branch_name="aq/dependent", base_sha=env.base, creation_generation=1,
            reserved=True, materialized=True, created_at=time.time(),
        ))
        await conn.execute(task_integration_checkpoints.insert().values(
            task_id="dependent", repository_id="repo", branch="aq/dependent",
            checkpoint_sha=env.base, updated_at=time.time(),
        ))
        await conn.execute(task_branch_origins.insert().values(
            id="origin-no-change", task_id="no-change", repository_id="repo",
            branch_name="aq/no-change", base_sha=env.base, creation_generation=1,
            reserved=True, materialized=True, created_at=time.time(),
        ))
    await db.save_task_completion(TaskCompletion(
        id="close-no-change", task_id="no-change", outcome="pass",
        commits=[source] if completion_kind == "commits" else [], completed_at=time.time(),
    ))
    if completion_kind != "missing":
        await GitProvenance(handler.orchestrator.git, str(env.source),
                            repository_url=str(env.remote)).write_completion(CompletedSource(
            CompletionIdentity(PROJECT_ID, "repo", "no-change", "close-no-change"), source,
        ))
    observer = DeliveryObserver(db, git=handler.orchestrator.git,
        truth=GitTruth(handler.orchestrator.git), data_dir=tmp_path / "prerequisites")
    db.set_prerequisite_observer(observer)
    sid, work_dir = await pool_session(db, tmp_path)
    env.git(tmp_path, "clone", str(env.remote), str(work_dir))
    await BranchOwnership(db).acquire(BranchKey(repository_id="repo", branch="aq/dependent"),
                                      "dependent", "worker")
    handler.orchestrator._worktree_slots = MagicMock(return_value=WorktreeSlotManager(
        db=db, git=handler.orchestrator.git, bus=handler.orchestrator.bus,
        config=handler.config.worktrees, git_mutex=handler.orchestrator._git_mutex,
    ))
    allowed = completion_kind in {"no_change", "contained_empty"}
    assert await db.is_hierarchy_task_runnable("dependent") is allowed
    assert ("dependent" in await db.hierarchy_runnable_task_ids(["dependent"])) is allowed
    assert await db.count_ready_by_profile(PROJECT_ID) == ({"worker": 1} if allowed else {})
    assert (await handler.orchestrator._measure_pools()).demand[PoolKey("worker")] == int(allowed)
    explanation = await handler._cmd_explain_task({"task_id": "dependent"})
    blockers = [reason for reason in explanation["reasons"]
                if reason["code"] == "prerequisite_not_on_default_branch"]
    assert bool(blockers) is not allowed
    if blockers:
        assert blockers[0]["ref"] == "no-change"
    claim = await scoped(handler, sid)._cmd_task_claim({"next": True})
    assert claim["result"] == ("claimed" if allowed else "no_ready_work"), claim
    if allowed:
        assert claim["task"]["id"] == "dependent"
@pytest.mark.parametrize("preparation", ["pool", "clone"])
@pytest.mark.parametrize("kind", [
    "source", "local", "generated", "generated-local", "clean", "proof-moved", "parent",
    "repair-reopened",
])
async def test_existing_child_parent_conflict_recovers_exact_inputs(
    handler, db, tmp_path, development_admission, monkeypatch, preparation, kind,
):
    import subprocess

    from src.integration.delivery_observer import DeliveryObserver
    from src.integration.git_truth import GitTruth
    from src.integration.hierarchy import HierarchyIntegration
    from src.integration.stacked_branches import StackPrerequisitesConflict, StackPreparationChanged
    from src.models import TaskCompletion
    from src.orchestrator.worktree_manager import WorktreeSlotManager

    env, git = development_admission, development_admission.git
    if kind.startswith("generated"):
        from tests.test_generated_artifacts import _catalogue_branches

        fixture = tmp_path / "catalogues"
        fixture.mkdir()
        repo, env.base, env.head, child = _catalogue_branches(fixture)
        git(env.source, "fetch", str(repo), env.base, env.head, child)
        git(env.source, "push", "--force", "origin", f"{env.head}:refs/heads/prerequisite")
        await db.save_task_completion(TaskCompletion(
            id="generated-source", task_id="prerequisite", outcome="pass", commits=[env.head],
        ))
    else:
        git(env.source, "checkout", "-b", "aq/dependent", env.base)
        (env.source / ("child-work" if kind in {"clean", "proof-moved"} else "work")).write_text(
            "existing child review feedback",
        )
        git(env.source, "add", ".")
        git(env.source, "commit", "-m", "existing child source")
        child = git(env.source, "rev-parse", "HEAD")
    published_child = env.base if kind in {"local", "generated-local"} else child
    git(env.source, "push", "origin", f"{env.head}:refs/heads/aq/epic",
        f"{published_child}:refs/heads/aq/dependent")
    await mktask(db, "epic", status=TaskStatus.IN_PROGRESS, repo_id="repo", branch_name="aq/epic")
    for tid in ("dependent", "prerequisite"):
        await db.add_dependency(tid, "epic", "parent-child")
    await db.update_task("dependent", branch_name="aq/dependent", is_blocked=False)
    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train",
                            repo_url=str(env.remote), hierarchical_integration_policy={
                                "prerequisite_branches": "wait-for-parent" if kind == "parent"
                                else "stacked",
                            })
    async with db.immediate() as conn:
        await conn.execute(task_branch_origins.insert().values(
            id="child-origin", task_id="dependent", repository_id="repo",
            branch_name="aq/dependent", parent_task_id="epic", parent_repository_id="repo",
            parent_ref="aq/epic", base_sha=env.base, creation_generation=1,
            reserved=True, materialized=True, created_at=time.time(),
        ))
        for tid, branch, head in (("epic", "aq/epic", env.head),
                                 ("dependent", "aq/dependent", published_child),
                                 ("prerequisite", "prerequisite", env.head)):
            await conn.execute(task_integration_checkpoints.insert().values(
                task_id=tid, repository_id="repo", branch=branch, checkpoint_sha=head,
                updated_at=time.time(),
            ))
    sid, work_dir = await pool_session(db, tmp_path)
    git(tmp_path, "clone", str(env.remote), str(work_dir))
    if kind in {"local", "generated-local"}:
        git(work_dir, "fetch", str(env.source), child)
        git(work_dir, "checkout", "-b", "aq/dependent", child)
    ownership = BranchOwnership(db)
    target = BranchKey(repository_id="repo", branch="aq/dependent")
    await ownership.acquire(target, "dependent", "worker")
    transport = handler.orchestrator.git
    db.set_prerequisite_observer(DeliveryObserver(
        db, git=transport, truth=GitTruth(transport), data_dir=tmp_path / "observer",
    ))
    manager = WorktreeSlotManager(
        db=db, git=transport, bus=handler.orchestrator.bus, config=handler.config.worktrees,
        git_mutex=handler.orchestrator._git_mutex,
    )
    handler.orchestrator._worktree_slots = MagicMock(return_value=manager)
    if kind == "proof-moved":
        reset = (manager.reset_slot_for_task if preparation == "pool"
                 else transport.aprepare_child_branch)

        async def move_after_reset(*args, **kwargs):
            branch = await reset(*args, **kwargs)
            git(env.source, "checkout", "-b", "parent-moved", env.head)
            (env.source / "parent-moved").write_text("changed after observation")
            git(env.source, "add", ".")
            git(env.source, "commit", "-m", "move proven parent")
            git(env.source, "push", "origin", "HEAD:refs/heads/aq/epic")
            return branch

        monkeypatch.setattr(manager if preparation == "pool" else transport,
                            "reset_slot_for_task" if preparation == "pool"
                            else "aprepare_child_branch", move_after_reset)

    async def prepare():
        if preparation == "pool":
            return await scoped(handler, sid)._cmd_task_claim({"next": True})
        task = await db.get_task("dependent")
        origin, fence, _ = await handler.orchestrator._hierarchy_origin_and_fence(
            task, await db.get_project(PROJECT_ID),
        )
        await handler.orchestrator._prepare_exact_origin_workspace(
            task, await db.get_project(PROJECT_ID),
            SimpleNamespace(workspace=await db.get_workspace_for_agent("agent-1")), origin, fence,
        )
        return {"result": "claimed"}

    if preparation == "clone" and not (kind.startswith("generated") or kind == "clean"):
        with pytest.raises(StackPreparationChanged if kind == "proof-moved"
                           else StackPrerequisitesConflict):
            await prepare()
        result = {"result": "no_ready_work"}
    else:
        result = await prepare()
    assert git(work_dir, "status", "--porcelain") == ""
    if kind.startswith("generated") or kind == "clean":
        assert result["result"] == "claimed", result
        for head in (child, env.head):
            git(work_dir, "merge-base", "--is-ancestor", head, "HEAD")
        if kind.startswith("generated"):
            catalogue = json.loads((work_dir / "tests/selection_catalogue.json").read_text())
            assert set(catalogue["modules"]) == {
                "tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
            }
        else:
            assert (work_dir / "child-work").read_text() == "existing child review feedback"
        return
    assert result["result"] == "no_ready_work", result
    assert not (work_dir / ".aq/claim.json").exists()
    assert await db.get_task_meta("dependent", "slot_reset_failure") is None
    if kind == "proof-moved":
        assert await db.get_task_meta("dependent", "stack_prerequisites_conflict") is None
        return
    detail = await db.get_task_meta("dependent", "stack_prerequisites_conflict")
    assert detail["boundary"] == "child_parent"
    assert detail["child_head"] == child and detail["overlay_head"] == env.head
    assert detail["files"] == ["work"]
    repair = await db.get_task(detail["repair_task_id"])
    assert repair.class_hint == "standard-medium" and repair.prefer_target == "worker"
    for _ in range(3):
        if preparation == "pool":
            await db.set_task_meta("dependent", "claim_prepare_backoff_until", time.time() - 1)
            assert (await prepare())["result"] == "no_ready_work"
        else:
            with pytest.raises(StackPrerequisitesConflict):
                await prepare()
        assert await db.get_task_meta("dependent", "stack_prerequisites_conflict") == detail
    assert git(env.remote, "rev-parse", "refs/heads/aq/epic") == env.head
    git(env.remote, "merge-base", "--is-ancestor", published_child, "refs/heads/aq/dependent")

    materializer_checkout = tmp_path / "materializer"
    git(tmp_path, "clone", str(env.remote), str(materializer_checkout))

    def materialize(repository, branch, head):
        # The scanner has only published repository objects; unpublished child
        # work must arrive separately through the retained-input fetch.
        git(materializer_checkout, "push", "origin", f"{head}:refs/heads/{branch}")
        return head

    origin = await db.get_task_branch_origin_for_promotion(repair.id, "repo")
    await HierarchyIntegration(db, branch_materializer=materialize).materialize_origin(origin["id"])
    repair_origin, repair_fence, _ = await handler.orchestrator._hierarchy_origin_and_fence(
        await db.get_task(repair.id), await db.get_project(PROJECT_ID),
    )
    assert repair_origin["base_sha"] == detail["starting_head"]
    assert repair_origin["stack_repair_inputs"]["heads"]
    await handler.orchestrator._prepare_exact_origin_workspace(
        await db.get_task(repair.id), await db.get_project(PROJECT_ID),
        SimpleNamespace(workspace=await db.get_workspace_for_agent("agent-1")),
        repair_origin, repair_fence,
    )
    assert git(work_dir, "rev-parse", "HEAD") == detail["starting_head"]
    assert git(work_dir, "status", "--porcelain") == ""
    for head in repair_origin["stack_repair_inputs"]["heads"]:
        git(work_dir, "cat-file", "-e", head + "^{commit}")
    inputs = await db.get_task_meta(repair.id, "stack_repair_inputs")
    git(env.source, "fetch", inputs["store"], *inputs["heads"])
    git(env.source, "checkout", "-b", repair.branch_name, detail["starting_head"])
    git(env.source, "merge", "--ff-only", child)
    conflict = subprocess.run(["git", "merge", "--no-commit", detail["overlay_head"]],
                              cwd=env.source, capture_output=True)
    assert conflict.returncode == 1
    (env.source / "work").write_text("resolved child and parent")
    git(env.source, "add", ".")
    git(env.source, "commit", "-m", "resolve exact child and parent")
    resolved = git(env.source, "rev-parse", "HEAD")
    git(env.source, "push", "origin", repair.branch_name)
    await db.save_task_completion(TaskCompletion(
        id="child-repair-pass", task_id=repair.id, outcome="pass", commits=[resolved],
    ))
    await db.transition_task(repair.id, TaskStatus.COMPLETED, force=True)
    await db.set_task_meta("dependent", "claim_prepare_backoff_until", time.time() - 1)
    if kind == "repair-reopened":
        reset = (manager.reset_slot_for_task if preparation == "pool"
                 else transport.aprepare_child_branch)

        async def reopen_after_reset(*args, **kwargs):
            branch = await reset(*args, **kwargs)
            await db.transition_task(repair.id, TaskStatus.READY, force=True)
            return branch

        monkeypatch.setattr(manager if preparation == "pool" else transport,
                            "reset_slot_for_task" if preparation == "pool"
                            else "aprepare_child_branch", reopen_after_reset)
        if preparation == "pool":
            assert (await prepare())["result"] == "no_ready_work"
        else:
            with pytest.raises(StackPreparationChanged):
                await prepare()
        saved = await db.get_task_branch_origin_for_promotion("dependent", "repo")
        assert saved["stack_snapshot"]["preparation_conflict"] == detail
        assert await db.get_task_meta("dependent", "slot_reset_failure") is None
        epoch = (await db.get_task("dependent")).claim_epoch
        if preparation == "pool":
            for _ in range(3):
                assert (await prepare())["result"] == "no_ready_work"
            assert (await db.get_task("dependent")).claim_epoch == epoch
        return
    if kind == "source" and preparation == "pool":
        activate = db.activate_claim
        monkeypatch.setattr(db, "activate_claim", AsyncMock(return_value=None))
        withheld = await prepare()
        assert withheld["result"] == "prepare_failed", withheld
        assert withheld["reason"] == "released before activation"
        saved = await db.get_task_branch_origin_for_promotion("dependent", "repo")
        assert saved["stack_snapshot"]["preparation_conflict"] == detail
        assert (await db.get_session(sid)).claim_phase != "active"
        monkeypatch.setattr(db, "activate_claim", activate)
    result = await prepare()
    assert result["result"] == "claimed", result
    for head in (child, env.head, resolved):
        git(work_dir, "merge-base", "--is-ancestor", head, "HEAD")
    assert git(work_dir, "status", "--porcelain") == ""
    assert (await db.get_task_branch_origin_for_promotion("dependent", "repo"))["base_sha"] == env.base
    assert await db.get_task_meta("dependent", "slot_reset_failure") is None


@pytest.mark.parametrize("source_parent", [None, "other-epic"])
async def test_cross_epic_demand_explain_claim_and_refreshed_child_base(
    handler, db, tmp_path, development_admission, source_parent
):
    from pathlib import Path

    from src.integration.delivery_observer import DeliveryObserver
    from src.integration.git_truth import GitTruth
    from src.integration.stacked_branches import EpicRefresh
    from src.integration.train import TrainTarget
    from src.orchestrator.worktree_manager import WorktreeSlotManager
    from src.scheduler import PoolKey
    from tests.test_integration_gitops import LocalGit
    from tests.test_integration_train_sources import lane

    env, git = development_admission, development_admission.git
    await mktask(db, "epic", status=TaskStatus.IN_PROGRESS, repo_id="repo", branch_name="aq/epic")
    await db.add_dependency("dependent", "epic", "parent-child")
    if source_parent:
        await mktask(db, source_parent, status=TaskStatus.IN_PROGRESS, repo_id="repo",
                     branch_name="aq/other-epic")
        await db.add_dependency("prerequisite", source_parent, "parent-child")
        git(env.source, "push", "origin", f"{env.head}:refs/heads/aq/other-epic")
    await db.update_task("dependent", branch_name="aq/dependent", is_blocked=False)
    git(env.source, "push", "origin", f"{env.base}:refs/heads/aq/epic",
        f"{env.base}:refs/heads/aq/dependent")
    sid, work_dir = await pool_session(db, tmp_path)
    git(tmp_path, "clone", str(env.remote), str(work_dir))
    async with db.immediate() as conn:
        await conn.execute(task_branch_origins.insert().values(
            id="origin-dependent", task_id="dependent", repository_id="repo",
            branch_name="aq/dependent", parent_task_id="epic", parent_repository_id="repo",
            parent_ref="aq/epic", base_sha=env.base, creation_generation=1,
            reserved=True, materialized=True, created_at=time.time(),
        ))
        await conn.execute(task_integration_checkpoints.insert().values(
            task_id="dependent", repository_id="repo", branch="aq/dependent",
            checkpoint_sha=env.base, updated_at=time.time(),
        ))
    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train", repo_url=str(env.remote))
    await BranchOwnership(db).acquire(BranchKey(repository_id="repo", branch="aq/dependent"),
                                      "dependent", "worker")
    transport = handler.orchestrator.git
    observer = DeliveryObserver(db, git=transport, truth=GitTruth(transport),
                                data_dir=tmp_path / "prerequisites")
    db.set_prerequisite_observer(observer)
    manager = WorktreeSlotManager(db=db, git=transport, bus=handler.orchestrator.bus,
        config=handler.config.worktrees, git_mutex=handler.orchestrator._git_mutex)
    handler.orchestrator._worktree_slots = MagicMock(return_value=manager)
    target = TrainTarget(PROJECT_ID, "repo", "refs/heads/aq/epic", "epic")
    train, checks, _ = lane(SimpleNamespace(db=db, origin=SimpleNamespace(
        clone=env.source, url=str(env.remote))), LocalGit(Path(env.remote)), target=target)
    handler.orchestrator.integration_train = train

    assert (await handler.orchestrator._measure_pools()).demand[PoolKey("worker")] == 0
    explanation = await handler._cmd_explain_task({"task_id": "dependent"})
    [reason] = [item for item in explanation["reasons"]
                if item["code"] == "prerequisite_not_on_default_branch"]
    assert reason["ref"] == "prerequisite"
    assert source_parent or "prerequisite" in reason["detail"]
    assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "no_ready_work"
    git(env.source, "push", "origin", f"{env.head}:refs/heads/main")
    await observer.prerequisite_view(PROJECT_ID, task_id="dependent")
    assert (await handler.orchestrator._measure_pools()).demand[PoolKey("worker")] == 0
    explanation = await handler._cmd_explain_task({"task_id": "dependent"})
    assert not [item for item in explanation["reasons"] if "prerequisite" in item["code"]]
    assert "frontier_epic_refresh_pending" in {item["code"] for item in explanation["reasons"]}

    # The epic still lacks the source. Admission stays withheld even with an
    # ordinary collection, before workspace preparation or claim epochs churn.
    from src.integration.batches import Batch, BatchMember, BatchStore

    store = BatchStore(db)
    collection = Batch("train-existing-collection", PROJECT_ID, "repo", target.target_ref)
    await store.freeze(collection, (BatchMember("dependent", env.base, env.base),),
                       trees={"dependent": git(env.remote, "rev-parse", f"{env.base}^{{tree}}")})
    before = (await db.get_task("dependent")).claim_epoch
    with patch.object(handler, "_prepare_and_activate", AsyncMock(side_effect=AssertionError(
        "missing epic containment must withhold workspace preparation",
    ))):
        assert (await handler.orchestrator._measure_pools()).demand[PoolKey("worker")] == 0
        for _ in range(2):
            assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == (
                "no_ready_work"
            )
    assert (await db.get_task("dependent")).claim_epoch == before
    await store.set_intent(collection.id, "aborted")

    # Missing parent containment withholds even without a batch. The integration
    # train or an explicit apply starts the refresh while the child stays unclaimed.
    waiting = await scoped(handler, sid)._cmd_task_claim({"next": True})
    assert waiting["result"] == "no_ready_work", waiting
    assert (await handler.orchestrator._measure_pools()).demand[PoolKey("worker")] == 0
    assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "no_ready_work"
    pending = await EpicRefresh(db, train).refresh("epic", dry_run=False)
    explanation = await handler._cmd_explain_task({"task_id": "dependent"})
    [reason] = [item for item in explanation["reasons"]
                if item["code"] == "frontier_epic_refresh_pending"]
    assert reason["batch_id"] == pending["batch_id"]
    assert pending["batch_id"] in reason["detail"]
    checks.green.add(pending["candidate_sha"])
    applied = await handler.execute("integration_refresh_epic", {
        "task_id": "epic", "dry_run": False,
    })
    assert applied["success"] and applied["outcome"] == "refreshed", applied
    claimed = await scoped(handler, sid)._cmd_task_claim({"next": True})
    assert claimed["result"] == "claimed", claimed
    assert claimed["task"]["id"] == "dependent"
    epic_tip = git(env.remote, "rev-parse", "aq/epic")
    git(work_dir, "merge-base", "--is-ancestor", env.head, "HEAD")
    git(work_dir, "merge-base", "--is-ancestor", epic_tip, "HEAD")
    assert (work_dir / "work").read_text() == "complete source"
    origin = await db.get_task_branch_origin_for_promotion("dependent", "repo")
    assert origin["base_sha"] == env.base
    assert origin["base_refresh"]["head_sha"] == epic_tip
    assert origin["base_refresh"]["default_sha"] == env.head


@pytest.mark.parametrize("kind,lifecycle,intent,contained", [
    (None, "sealed", "open", True),
    ("collection", "sealed", "open", True),
    ("collection", "failed", "open", True),
    ("collection", "human_blocked", "paused", True),
    ("retreated_collection", "sealed", "open", True),
    ("refresh", "sealed", "open", True),
    ("refresh", "failed", "open", True),
    ("refresh", "human_blocked", "paused", True),
    ("refresh", "promoted", "open", True),
    ("refresh", "sealed", "aborted", True),
    ("collection", "sealed", "open", False),
    (None, "sealed", "open", False),
])
async def test_cross_epic_frontier_distinguishes_collection_refresh_and_containment(
    handler, db, tmp_path, development_admission, kind, lifecycle, intent, contained,
):
    """Every frontier consumer admits contained sources past unrelated sibling batches."""
    from src.database.tables import integration_batches
    from src.integration.batches import Batch, BatchMember, BatchStore
    from src.integration.delivery_observer import DeliveryObserver
    from src.integration.git_truth import GitTruth
    from src.integration.stacked_branches import EpicRefresh, EpicRefreshPending
    from src.orchestrator.worktree_manager import WorktreeSlotManager
    from src.scheduler import PoolKey

    env, git = development_admission, development_admission.git
    await mktask(db, "epic", status=TaskStatus.IN_PROGRESS, repo_id="repo", branch_name="aq/epic")
    await db.add_dependency("dependent", "epic", "parent-child")
    await mktask(db, "other-epic", status=TaskStatus.IN_PROGRESS, repo_id="repo",
                 branch_name="aq/other-epic")
    await db.add_dependency("prerequisite", "other-epic", "parent-child")
    await db.update_task("dependent", branch_name="aq/dependent", is_blocked=False)
    parent_head = env.head if contained else env.base
    git(env.source, "push", "origin", f"{env.head}:refs/heads/main",
        f"{env.head}:refs/heads/aq/other-epic", f"{parent_head}:refs/heads/aq/epic",
        f"{env.head}:refs/heads/aq/dependent")
    sid, work_dir = await pool_session(db, tmp_path)
    git(tmp_path, "clone", str(env.remote), str(work_dir))
    async with db.immediate() as conn:
        await conn.execute(task_branch_origins.insert().values(
            id="origin-dependent", task_id="dependent", repository_id="repo",
            branch_name="aq/dependent", parent_task_id="epic", parent_repository_id="repo",
            parent_ref="aq/epic", base_sha=env.head, creation_generation=1,
            reserved=True, materialized=True, created_at=time.time(),
        ))
        await conn.execute(task_integration_checkpoints.insert().values(
            task_id="dependent", repository_id="repo", branch="aq/dependent",
            checkpoint_sha=env.head, updated_at=time.time(),
        ))
    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train", repo_url=str(env.remote))
    await BranchOwnership(db).acquire(BranchKey(repository_id="repo", branch="aq/dependent"),
                                      "dependent", "worker")
    transport = handler.orchestrator.git
    observer = DeliveryObserver(db, git=transport, truth=GitTruth(transport),
                                data_dir=tmp_path / "prerequisites")
    db.set_prerequisite_observer(observer)
    manager = WorktreeSlotManager(db=db, git=transport, bus=handler.orchestrator.bus,
        config=handler.config.worktrees, git_mutex=handler.orchestrator._git_mutex)
    handler.orchestrator._worktree_slots = MagicMock(return_value=manager)
    batch_id = "train-epic-refresh-test" if kind == "refresh" else "train-unrelated-collection"
    if kind:
        store = BatchStore(db)
        await store.freeze(Batch(batch_id, PROJECT_ID, "repo", "refs/heads/aq/epic"),
                           (BatchMember("epic", env.head, env.base),),
                           trees={"epic": git(env.remote, "rev-parse", f"{env.head}^{{tree}}")})
        async with db.immediate() as conn:
            await conn.execute(update(integration_batches).where(integration_batches.c.id == batch_id)
                               .values(lifecycle=lifecycle, intent=intent))

    view = await observer.prerequisite_view(PROJECT_ID, task_id="dependent")
    assert view.parent_containment == {"dependent": {"prerequisite": contained}}
    if kind == "retreated_collection":
        # Main and the completion source remain unchanged; a cached epic proof
        # must not admit work after that epic loses the source.
        git(env.source, "push", "-f", "origin", f"{env.base}:refs/heads/aq/epic")
        assert not await view.fresh()
        contained = False
    refresh_open = kind == "refresh" and lifecycle != "promoted" and intent != "aborted"
    claimable = contained and not refresh_open
    assert await db.hierarchy_runnable_task_ids(["dependent"]) == (
        {"dependent"} if claimable else set()
    )
    assert (await handler.orchestrator._measure_pools()).demand[PoolKey("worker")] == int(claimable)
    explanation = await handler._cmd_explain_task({"task_id": "dependent"})
    assert not [item for item in explanation["reasons"] if "prerequisite" in item["code"]]
    refresh_reasons = [item for item in explanation["reasons"]
                       if item["code"] == "frontier_epic_refresh_pending"]
    if claimable:
        assert not refresh_reasons
    else:
        [reason] = refresh_reasons
        if refresh_open:
            assert reason["batch_id"] == batch_id
            assert batch_id in reason["detail"]
            # A refresh opened after selection must also stop fresh preparation,
            # even when the parent already contains the source.
            task = await db.get_task("dependent")
            filing = await db.get_task_branch_origin_for_promotion("dependent", "repo")
            with pytest.raises(EpicRefreshPending, match=batch_id):
                await EpicRefresh(db).child_base(task, filing)
        else:
            assert "batch_id" not in reason
    claimed = await scoped(handler, sid)._cmd_task_claim({"next": True})
    assert claimed["result"] == ("claimed" if claimable else "no_ready_work"), claimed
    if claimable:
        assert claimed["task"]["id"] == "dependent"
        git(work_dir, "merge-base", "--is-ancestor", env.head, "HEAD")


async def test_slow_frontier_view_fails_closed_within_its_bound(
    db, monkeypatch, development_admission,
):
    """A Git view that never answers withholds Git-gated work instead of hanging."""
    import asyncio

    import src.integration.delivery_observer as module

    await db.update_project(PROJECT_ID, hierarchical_integration_mode="train",
                            repo_url=str(development_admission.remote))

    class Hanging:
        truth = object()
        READ_MAX_AGE = 30.0

        async def prerequisite_view(self, *args, **kwargs):
            await asyncio.sleep(3600)

    db.set_prerequisite_observer(Hanging())
    monkeypatch.setattr(module, "FRONTIER_MODE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(module, "FRONTIER_DISPLAY_TIMEOUT_SECONDS", 0.05)
    for cached_only in (False, True):
        modes = await asyncio.wait_for(module.hierarchy_frontier_modes(
            db, project_ids={PROJECT_ID}, cached_only=cached_only), 5)
        mode = modes[PROJECT_ID]
        assert mode.delivered_prerequisite_ids == frozenset()
        assert mode.default_prerequisite_ids == frozenset()
        assert mode.parent_contained_task_ids == frozenset()


@pytest.mark.parametrize("misleading_history", [False, True])
async def test_development_readiness_pool_and_claim_follow_git(
    handler, db, tmp_path, development_admission, misleading_history
):
    import json

    from sqlalchemy import insert
    from src.database.tables import events
    from src.integration.admission import observe_admission

    env = development_admission
    if misleading_history:
        # A finished publisher action and a retired journal row both naming the
        # prerequisite: history, never a delivery answer.
        row = {
            "project_id": PROJECT_ID, "repository_id": "repo", "state": "delivered",
            "target_ref": "refs/heads/main", "created_at": time.time(),
            "updated_at": time.time(), "manifest": [{"task_id": "prerequisite",
                                                     "source_sha": env.head}],
            "evidence": {"completion_sources": [{"task_id": "prerequisite",
                                                  "completion_id": "close-1",
                                                  "source_sha": env.head}]},
            "reason": "deliberately misleading fixture",
        }
        async with db.immediate() as conn:
            for event_type, payload in (
                ("development.operation", {**row, "id": "misleading", "state": "finished"}),
                ("development.legacy_provenance",
                 {**row, "id": "legacy-provenance:old", "legacy_id": "old"}),
            ):
                await conn.execute(insert(events).values(
                    event_type=event_type, project_id=PROJECT_ID,
                    payload=json.dumps(payload), timestamp=time.time(),
                ))
    assert not (await db.get_task("dependent")).is_blocked
    batch = await observe_admission(db, ["dependent"], env.service)
    assert batch.allowed == set()
    assert await db.count_ready_by_profile(PROJECT_ID, allowed_task_ids=batch.allowed) == {}
    from src.scheduler import PoolKey

    assert (await handler.orchestrator._measure_pools()).demand[PoolKey("worker")] == 0
    ready = await handler._cmd_project_ready({"project_id": PROJECT_ID})
    assert not ready["ready"]
    assert any(item["task_id"] == "dependent" for item in ready["withheld"])
    sid, _ = await pool_session(db, tmp_path)
    assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["result"] == "no_ready_work"
    # An external ancestry-preserving merge releases work on the next snapshot,
    # even if no journal was ever written or its state is deliberately wrong.
    env.git(env.source, "push", "origin", "prerequisite:main")
    batch = await observe_admission(db, ["dependent"], env.service)
    assert batch.allowed == {"dependent"}
    assert await db.count_ready_by_profile(PROJECT_ID, allowed_task_ids=batch.allowed) == {
        "worker": 1
    }
    assert (await handler.orchestrator._measure_pools()).demand[PoolKey("worker")] == 1
    ready = await handler._cmd_project_ready({"project_id": PROJECT_ID})
    assert [r["task_id"] for r in ready["ready"]] == ["dependent"]
    assert (await scoped(handler, sid)._cmd_task_claim({"next": True}))["task"]["id"] == "dependent"


@pytest.mark.parametrize("movement", ["reopen", "completion", "edge", "gate", "hold", "target", "config"])
async def test_development_preparation_invalidates_old_admission(
    handler, db, tmp_path, development_admission, movement
):
    from src.models import TaskCompletion
    from src.database.tables import gates, task_gates
    from sqlalchemy import insert

    env = development_admission
    env.git(env.source, "push", "origin", "prerequisite:main")
    sid, work_dir = await pool_session(db, tmp_path)
    resets = 0

    async def reset(*args, **kwargs):
        nonlocal resets
        resets += 1
        if resets == 1:
            if movement == "reopen":
                await db.update_task("prerequisite", status=TaskStatus.READY)
            elif movement == "completion":
                # Latest generation's missing source never borrows the old close.
                await db.save_task_completion(
                    TaskCompletion(
                        id="close-2",
                        task_id="prerequisite",
                        outcome="pass",
                        commits=["a" * 40],
                        completed_at=time.time(),
                    )
                )
            elif movement == "edge":
                await mktask(db, "new-dep", status=TaskStatus.DEFINED)
                await db.add_dependency("dependent", "new-dep")
            elif movement == "gate":
                async with db.immediate() as conn:
                    await conn.execute(
                        insert(gates).values(
                            id="new-gate",
                            project_id=PROJECT_ID,
                            gate_type="human",
                            status="open",
                            title="new admission gate",
                            created_at=time.time(),
                        )
                    )
                    await conn.execute(
                        insert(task_gates).values(task_id="dependent", gate_id="new-gate")
                    )
                    await db.recompute_blocked({"dependent"}, conn=conn)
            elif movement == "hold":
                await db.add_task_label("dependent", "hold:operator")
            elif movement == "target":
                env.git(env.source, "push", "--force", "origin", env.base + ":main")
            else:
                await db.update_repo("repo", default_branch="missing-target")
        return "aq/dependent"

    handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = AsyncMock(
        side_effect=reset
    )
    result = await scoped(handler, sid)._cmd_task_claim({"next": True})
    assert result["result"] == "no_ready_work"
    assert (await db.get_session(sid)).claims == 0
    assert (await db.get_session(sid)).claim_phase is None
    assert not (work_dir / ".aq" / "claim.json").exists()


async def test_development_moving_target_retries_and_claims_new_snapshot(
    handler, db, tmp_path, development_admission
):
    env = development_admission
    env.git(env.source, "push", "origin", "prerequisite:main")
    sid, _ = await pool_session(db, tmp_path)
    resets = 0

    async def reset(*args, **kwargs):
        nonlocal resets
        resets += 1
        if resets == 1:
            (env.source / "advance").write_text("advanced target")
            env.git(env.source, "add", ".")
            env.git(env.source, "commit", "-m", "advance")
            env.git(env.source, "push", "origin", "HEAD:main")
        return "aq/dependent"

    handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = AsyncMock(
        side_effect=reset
    )
    result = await scoped(handler, sid)._cmd_task_claim({"next": True})
    assert result["result"] == "claimed"
    assert resets == 2
    assert (await db.get_session(sid)).claims == 1


async def test_development_withheld_page_does_not_starve_ready_work(
    handler, db, tmp_path, development_admission
):
    from src.integration.admission import structural_candidates

    for i in range(130):
        tid = f"withheld-{i:03}"
        await mktask(db, tid, profile_id="worker", priority=1)
        await db.add_dependency(tid, "prerequisite")
    await mktask(db, "clean", profile_id="worker", priority=100)
    assert len(await structural_candidates(db, PROJECT_ID)) == 132
    sid, _ = await pool_session(db, tmp_path)
    result = await scoped(handler, sid)._cmd_task_claim({"next": True})
    assert result["task"]["id"] == "clean"


async def test_development_parallel_claims_remain_exclusive(
    handler, db, tmp_path, development_admission
):
    env = development_admission
    env.git(env.source, "push", "origin", "prerequisite:main")
    sid1, _ = await pool_session(db, tmp_path, "parallel-1", "agent-p1")
    sid2, _ = await pool_session(db, tmp_path, "parallel-2", "agent-p2")
    # Operator-scoped explicit session IDs avoid sharing mutable worker scope.
    handler._current_scope = None
    results = await asyncio.gather(
        *[handler._cmd_task_claim({"session_id": sid, "next": True}) for sid in [sid1, sid2]]
    )
    assert sorted(r["result"] for r in results) == ["claimed", "no_ready_work"]
    assert sum([(await db.get_session(sid)).claims for sid in [sid1, sid2]]) == 1


async def test_development_unknown_git_withholds_only_sensitive_work(
    handler, db, tmp_path, development_admission
):
    from src.git.manager import GitError

    env = development_admission
    env.service.git.afetch_origin = AsyncMock(side_effect=GitError("fetch failed"))
    await mktask(db, "independent", profile_id="worker")
    sid, _ = await pool_session(db, tmp_path)
    result = await scoped(handler, sid)._cmd_task_claim({"next": True})
    assert result["task"]["id"] == "independent"


async def test_development_parallel_claims_can_skip_locked_peer(
    handler, db, tmp_path, development_admission
):
    env = development_admission
    env.git(env.source, "push", "origin", "prerequisite:main")
    await mktask(db, "second", profile_id="worker")
    await db.add_dependency("second", "prerequisite")
    sid1, _ = await pool_session(db, tmp_path, "peer-1", "peer-agent-1")
    sid2, _ = await pool_session(db, tmp_path, "peer-2", "peer-agent-2")
    handler._current_scope = None
    results = await asyncio.gather(
        *[handler._cmd_task_claim({"session_id": sid, "next": True}) for sid in [sid1, sid2]]
    )
    assert [r["result"] for r in results] == ["claimed", "claimed"]
    assert {r["task"]["id"] for r in results} == {"dependent", "second"}


async def test_development_parallel_admission_reads_serialize_fetches(
    db, development_admission, monkeypatch
):
    from src.integration.admission import observe_admission

    env = development_admission
    env.git(env.source, "push", "origin", "prerequisite:main")
    assert (await observe_admission(db, ["dependent"], env.service)).allowed == {"dependent"}

    original_fetch = env.service.git.afetch_origin
    active = 0
    peak = 0

    async def observed_fetch(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.05)
            return await original_fetch(*args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(env.service.git, "afetch_origin", observed_fetch)
    batches = await asyncio.gather(
        observe_admission(db, ["dependent"], env.service),
        observe_admission(db, ["dependent"], env.service),
    )
    assert [batch.allowed for batch in batches] == [{"dependent"}, {"dependent"}]
    assert peak == 1


async def test_development_waits_for_requires_current_child_delivery(
    handler, db, tmp_path, development_admission
):
    from src.integration.admission import observe_admission

    env = development_admission
    await db.remove_dependency("dependent", "prerequisite")
    await mktask(db, "group", status=TaskStatus.READY)
    await db.add_dependency("prerequisite", "group", dep_type="parent-child")
    await db.add_dependency("dependent", "group", dep_type="waits-for")
    assert not (await db.get_task("dependent")).is_blocked
    assert not (await observe_admission(db, ["dependent"], env.service)).allowed
    env.git(env.source, "push", "origin", "prerequisite:main")
    assert (await observe_admission(db, ["dependent"], env.service)).allowed == {"dependent"}


@pytest.mark.parametrize("wait_seconds", [1, 10])
async def test_development_long_poll_observes_external_merge_without_event(
    handler, db, tmp_path, development_admission, monkeypatch, wait_seconds
):
    env = development_admission
    sid, _ = await pool_session(db, tmp_path)
    handler.config.swarm.claim_wait_max = wait_seconds
    # Git/DB latency must not exhaust the poll before the external merge.
    # Keep the claim clock local so asyncio deadlines and lease time stay real.
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        "src.commands.claim_commands.time",
        SimpleNamespace(time=time.time, monotonic=lambda: clock.now),
    )

    async def merge_on_timeout(timeout):
        assert env.git(env.remote, "rev-parse", "main") == env.base
        env.git(env.source, "push", "origin", "prerequisite:main")
        clock.now += timeout
        return None  # No daemon event wakes the frontier waiter.

    with patch("src.event_bus.EventWaiter.wait", new_callable=AsyncMock) as poll:
        poll.side_effect = merge_on_timeout
        result = await asyncio.wait_for(
            scoped(handler, sid)._cmd_task_claim({"next": True, "wait": wait_seconds}),
            timeout=10,
        )
    poll.assert_awaited_once_with(min(wait_seconds, 5.0))
    assert result["result"] == "claimed"
    assert result["task"]["id"] == "dependent"


async def test_development_initial_snapshot_movement_retries_before_preparation(
    handler, db, tmp_path, development_admission
):
    env = development_admission
    env.git(env.source, "push", "origin", "prerequisite:main")
    sid, _ = await pool_session(db, tmp_path)
    fetch = env.service.git.afetch_origin
    moved = False

    async def moving_fetch(*args, **kwargs):
        nonlocal moved
        await fetch(*args, **kwargs)
        if not moved:
            moved = True
            (env.source / "initial-advance").write_text("move after fetch")
            env.git(env.source, "add", ".")
            env.git(env.source, "commit", "-m", "move between fetch and admission")
            env.git(env.source, "push", "origin", "HEAD:main")

    env.service.git.afetch_origin = AsyncMock(side_effect=moving_fetch)
    result = await scoped(handler, sid)._cmd_task_claim({"next": True})
    assert result["result"] == "claimed"
    assert result["claim_epoch"] == 1
    assert env.service.git.afetch_origin.await_count == 2
    handler.orchestrator._worktree_slots.return_value.reset_slot_for_task.assert_awaited_once()
