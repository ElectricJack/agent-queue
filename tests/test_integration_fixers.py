"""Task-free in-place repair survives replay, budget exhaustion and green."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncio
import pytest
from sqlalchemy import func, select, update

from src.commands.principal import ExecutionPrincipal
from src.database.tables import integration_batches, messages, tasks
from src.git.manager import _git_preparation_error
from src.integration.batches import Batch, BatchMember, BatchStore, candidate_ref
from src.integration.fixers import BatchFixerService, split_members
from src.integration.models import RepairPolicy
from src.models import Project, RepoConfig, RepoSourceType

HEAD = "a" * 40
NEXT = "b" * 40
LAST = "c" * 40


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("fixers.db")
    await database.create_project(Project(id="p", name="Project"))
    await database.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.LINK))
    yield database


@pytest.fixture
async def fixer(db):
    batch = Batch(id="batch-fix", project_id="p", repository_id="r", target_ref="refs/heads/dev")
    await BatchStore(db).freeze(batch, (
        BatchMember(task_id="one", source_sha=HEAD, source_base_sha="d" * 40),
        BatchMember(task_id="two", source_sha=NEXT, source_base_sha="d" * 40, order=1),
    ), trees={"one": "e" * 40, "two": "f" * 40})
    runtime = SimpleNamespace(start=AsyncMock(return_value={"work_dir": "/retained/batch"}),
                              alive=AsyncMock(return_value=True), push=AsyncMock())
    now = [100.0]
    service = BatchFixerService(db, runtime, clock=lambda: now[0])
    return service, runtime, now


async def allocate(service, head=HEAD, **kwargs):
    return await service.allocate("batch-fix", target_ref=candidate_ref("batch-fix"),
                                  head_sha=head, authorize=AsyncMock(return_value=True), **kwargs)


async def state(db):
    return (await db.get_integration_batch("batch-fix"))["fixer_state"]


async def notices(db, *, supervisor=False):
    async with db._engine.connect() as conn:
        statement = select(messages).where(messages.c.body_kind == "integration_fixer")
        if supervisor:
            statement = statement.where(messages.c.to_id == "supervisor-p")
        return (await conn.execute(statement)).mappings().all()


async def test_concurrent_replay_and_restart_launch_one_session_without_tasks(db, fixer):
    service, runtime, _ = fixer
    results = await asyncio.gather(*(allocate(service) for _ in range(6)))
    assert len({r["session_id"] for r in results}) == 1
    runtime.start.assert_awaited_once()
    restarted = BatchFixerService(db, runtime, clock=lambda: 101)
    assert (await allocate(restarted))["outcome"] == "exists"
    runtime.start.assert_awaited_once()
    frozen = await state(db)
    assert frozen["starting_sha"] == HEAD
    assert [m["task_id"] for m in frozen["members"]] == ["one", "two"]
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(tasks)) == 0
    assert not await notices(db)


async def test_distinct_red_heads_notify_same_fixer_once(db, fixer):
    service, runtime, _ = fixer
    first = await allocate(service)
    for head in (NEXT, NEXT, LAST, LAST):
        assert (await allocate(service, head))["session_id"] == first["session_id"]
    runtime.start.assert_awaited_once()
    assert len(await notices(db)) == 2
    assert (await state(db))["red_heads"] == [HEAD, NEXT, LAST]


async def test_pending_recovery_does_not_spend_red_round_or_wake(db, fixer):
    service, runtime, _ = fixer
    await allocate(service)
    for _ in range(4):
        await service.recover_reservation("batch-fix", target_ref=candidate_ref("batch-fix"),
                                          head_sha=NEXT, authorize=AsyncMock(return_value=True))
    assert (await state(db))["red_heads"] == [HEAD]
    assert not await notices(db)
    runtime.start.assert_awaited_once()


@pytest.mark.parametrize("budget", ["time", "rounds"])
async def test_exhaustion_reports_once_without_spawning_or_waking_then_green_wins(db, fixer, budget):
    service, runtime, now = fixer
    await allocate(service, policy=RepairPolicy(primary_seconds=20, primary_attempts=1,
                                                debug_intelligence_class="deep-high"))
    if budget == "time":
        now[0] += 21
    for _ in range(4):
        assert (await allocate(service, NEXT))["outcome"] == "deferred"
    runtime.start.assert_awaited_once()
    assert len(await notices(db)) == len(await notices(db, supervisor=True)) == 1
    await service.settle_green("batch-fix", NEXT)
    await service.settle_green("batch-fix", NEXT)
    assert (await state(db))["status"] == "green"
    assert len(await notices(db)) == 2


async def test_stopped_fixer_is_preserved_not_replaced(db, fixer):
    service, runtime, _ = fixer
    await allocate(service)
    original = await state(db)
    runtime.alive.return_value = False
    for _ in range(3):
        assert (await allocate(service))["outcome"] == "deferred"
    assert (await state(db))["session_id"] == original["session_id"]
    assert (await state(db))["work_dir"] == original["work_dir"]
    assert len(await notices(db, supervisor=True)) == 1
    runtime.start.assert_awaited_once()


async def register(db, state, session_state):
    from src.models import SessionRecord

    await db.create_session(SessionRecord(
        id=state["session_id"], project_id="p", profile_id="fixer", harness="claude",
        provider="fake", name=state["session_name"], lifecycle="named", work_dir="/retained/x",
        epoch="test", instance_token=state["instance_token"], started_at=1, task_id=None,
        state=session_state, desired_state="stopped" if session_state == "stopped" else "running",
    ))


@pytest.mark.parametrize("legacy", [False, True])
async def test_train_visit_retries_orphaned_launch_before_repair_budget(db, fixer, legacy):
    from tests import test_integration_train as train_tests

    service, runtime, now = fixer
    runtime.start.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await allocate(service)
    orphan = await state(db)
    assert orphan["status"] == "launching"
    assert await db.get_session(orphan["session_id"]) is None
    if legacy:
        orphan.pop("launch_started_at")
        async with db.immediate() as conn:
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == "batch-fix",
            ).values(fixer_state=orphan))
    # New service instance, with no memory of the interrupted runtime.start.
    restarted = BatchFixerService(db, runtime, clock=lambda: now[0])
    now[0] += 119
    assert (await allocate(restarted))["outcome"] == "exists"
    now[0] += 1
    frozen = Batch.from_row(await db.get_integration_batch("batch-fix"))
    members = await BatchStore(db).members(frozen.id)
    target = train_tests.TrainTarget("p", "r", "refs/heads/dev")
    train = train_tests.train(train_tests.Targets(target),
        train_tests.Batches({target.key: (frozen, members)}), {
            target.key: train_tests.lane(train_tests.Conflicting(
                {"outcome": "conflict", "head": HEAD}, candidate=HEAD)),
        }, repair=restarted)
    visit = await train.visit(target)
    assert visit.repair["outcome"] == "deferred"
    row = await db.get_integration_batch(frozen.id)
    assert row["fixer_queue"]["reason"] == "launch_retry"
    assert row["fixer_state"]["launch_failures"] == 1
    assert row["fixer_state"]["status"] == "waiting_capacity"
    assert orphan["deadline"] is None
    runtime.start.assert_awaited_once()
    assert not await notices(db, supervisor=True)
    await transfer(db, frozen.id, "orphan-retry")
    now[0] += row["fixer_queue"]["revisit_seconds"]
    runtime.start.side_effect = None
    assert (await allocate_on(restarted, "orphan-retry"))["outcome"] == "filed"
    retried = (await db.get_integration_batch("orphan-retry"))["fixer_state"]
    assert retried["session_id"] == orphan["session_id"]
    assert retried["instance_token"] == orphan["instance_token"]
    assert retried["launch_started_at"] == now[0]
    assert retried["members"] == orphan["members"]


@pytest.mark.parametrize("session_state", ["starting", "running", "stopped"])
async def test_stale_launch_retries_only_confirmed_stopped_registration(db, fixer, session_state):
    service, runtime, now = fixer
    runtime.start.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await allocate(service)
    orphan = await state(db)
    await register(db, orphan, session_state)
    now[0] += 120
    result = await allocate(BatchFixerService(db, runtime, clock=lambda: now[0]))
    runtime.start.assert_awaited_once()
    current = await state(db)
    if session_state == "stopped":
        assert result["outcome"] == "deferred"
        assert current["status"] == "waiting_capacity"
        assert current["session_id"] != orphan["session_id"]
        assert current["instance_token"] != orphan["instance_token"]
    else:
        assert result["outcome"] == "exists"
        assert current == orphan
    assert not await notices(db, supervisor=True)


@pytest.mark.parametrize("late_error", [False, True])
async def test_late_launch_result_cannot_overwrite_retry(db, fixer, late_error):
    service, runtime, now = fixer
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed(launch):
        started.set()
        await finish.wait()
        if late_error:
            raise RuntimeError("late launch failure")
        return {"work_dir": "/old/preparation"}

    runtime.start.side_effect = delayed
    pending = asyncio.create_task(allocate(service))
    try:
        await started.wait()
        now[0] += 120
        restarted = BatchFixerService(db, runtime, clock=lambda: now[0])
        assert (await allocate(restarted))["outcome"] == "deferred"
        await transfer(db, "batch-fix", "replacement")
        now[0] += 300
        runtime.start.side_effect = None
        assert (await allocate_on(restarted, "replacement"))["outcome"] == "filed"
        replacement = await db.get_integration_batch("replacement")
        finish.set()
        assert (await pending)["outcome"] == "withdrawn"
        assert (await db.get_integration_batch("replacement"))["fixer_state"] == (
            replacement["fixer_state"])
        assert await state(db) is None
        assert not await notices(db, supervisor=True)
    finally:
        finish.set()
        await pending


async def test_each_launch_retry_gets_a_fresh_grace_period(db, fixer):
    service, runtime, now = fixer
    runtime.start.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await allocate(service)
    now[0] += 120
    assert (await allocate(service))["outcome"] == "deferred"
    await transfer(db, "batch-fix", "retry-grace")
    now[0] += 300
    with pytest.raises(asyncio.CancelledError):
        await allocate_on(service, "retry-grace")
    now[0] += 119
    assert (await allocate_on(service, "retry-grace"))["outcome"] == "exists"
    now[0] += 1
    assert (await allocate_on(service, "retry-grace"))["outcome"] == "deferred"
    current = (await db.get_integration_batch("retry-grace"))["fixer_state"]
    assert current["launch_failures"] == 2
    assert runtime.start.await_count == 2


async def transfer(db, source, successor_id):
    """What the due resume observer does with an unlaunched identity."""
    store = BatchStore(db)
    await store.freeze(Batch(successor_id, "p", "r", "refs/heads/dev"),
                       await store.members(source), trees={"one": "e" * 40, "two": "f" * 40})
    waiting = (await db.get_integration_batch(source))["fixer_state"]
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == source)
                           .values(fixer_state=None))
        await conn.execute(update(integration_batches).where(integration_batches.c.id == successor_id)
                           .values(fixer_state={**waiting, "batch_id": successor_id,
                                                "target_ref": candidate_ref(successor_id)},
                                   fixer_queue={"family_id": "batch-fix"}))


async def allocate_on(service, batch_id, head=NEXT):
    return await service.allocate(batch_id, target_ref=candidate_ref(batch_id), head_sha=head,
                                  authorize=AsyncMock(return_value=True))


async def test_unregistered_launch_failure_retries_with_backoff_and_escalates_once(db, fixer):
    service, runtime, now = fixer
    # Push preparation fails before registration: no process, no session row.
    runtime.start.side_effect = RuntimeError("authenticated Git push preparation failed")
    first = await allocate(service)
    assert first["outcome"] == "deferred"
    batch_id, backoffs = "batch-fix", []
    for attempt in range(1, 5):
        row = await db.get_integration_batch(batch_id)
        assert row["fixer_queue"]["reason"] == "launch_retry"
        backoffs.append(row["fixer_queue"]["revisit_seconds"])
        current = row["fixer_state"]
        assert current["status"] == "waiting_capacity" and "blocker" not in current
        assert current["launch_failures"] == attempt
        assert current["session_id"] == first["session_id"]
        assert len(await notices(db, supervisor=True)) == (0 if attempt < 3 else 1)
        successor = f"retry-{attempt}"
        await transfer(db, batch_id, successor)
        now[0] += 5000
        batch_id = successor
        if attempt < 4:
            assert (await allocate_on(service, batch_id))["outcome"] == "deferred"
    assert backoffs == [300, 600, 1200, 2400]
    runtime.start.side_effect = None
    result = await allocate_on(service, batch_id)
    assert result == {"success": True, "outcome": "filed", "session_id": first["session_id"]}
    assert runtime.start.await_count == 5
    assert (await db.get_integration_batch(batch_id))["fixer_state"]["status"] == "active"
    assert len(await notices(db, supervisor=True)) == 1


async def test_stopped_registered_launch_retries_under_a_fresh_identity(db, fixer):
    from src.sessions.provider import SessionDiedDuringStartup

    service, runtime, now = fixer

    async def died(launch):
        await register(db, launch, "stopped")  # launch cleanup confirmed the stop
        raise SessionDiedDuringStartup("harness exited during startup")

    runtime.start.side_effect = died
    first = await allocate(service)
    assert first["outcome"] == "deferred"
    retry = await state(db)
    assert retry["status"] == "waiting_capacity"
    launched = runtime.start.await_args.args[0]
    assert first["session_id"] == retry["session_id"] != launched["session_id"]
    assert retry["instance_token"] != launched["instance_token"]
    assert retry["session_name"] == launched["session_name"]
    assert retry["session_id"] == str(UUID(retry["session_id"]))
    await transfer(db, "batch-fix", "fresh-next")
    now[0] += 5000
    runtime.start.side_effect = None
    result = await allocate_on(service, "fresh-next")
    assert result["outcome"] == "filed"
    assert result["session_id"] == retry["session_id"]
    assert runtime.start.await_args.args[0]["session_id"] == str(UUID(retry["session_id"]))
    assert not await notices(db, supervisor=True)


async def test_launch_retry_dashes_a_legacy_unregistered_identity_before_relaunch(db, fixer):
    service, runtime, now = fixer
    runtime.start.side_effect = RuntimeError("authenticated Git push preparation failed")
    assert (await allocate(service))["outcome"] == "deferred"
    legacy = uuid4().hex
    waiting = await state(db)
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "batch-fix",
        ).values(fixer_state={**waiting, "session_id": legacy}))
    await transfer(db, "batch-fix", "legacy-launch-retry")
    now[0] += 5000
    runtime.start.side_effect = None
    result = await allocate_on(service, "legacy-launch-retry")
    assert result["outcome"] == "filed"
    assert result["session_id"] == str(UUID(legacy))
    assert runtime.start.await_args.args[0]["session_id"] == str(UUID(legacy))
    assert (await db.get_integration_batch("legacy-launch-retry"))["fixer_state"][
        "session_id"
    ] == str(UUID(legacy))


@pytest.mark.parametrize("owner_status", ["active", "launching"])
@pytest.mark.parametrize("session_state", [None, "stopped"])
async def test_family_blocked_by_legacy_launch_failure_relaunches_on_successor(
        db, fixer, session_state, owner_status):
    from src.integration.fixer_batches import defer_on

    service, runtime, now = fixer
    await allocate(service)
    owner = await state(db)
    # The pre-retry shape: an expired root owner with a permanent launch blocker.
    legacy = UUID(owner["session_id"]).hex
    owner.update(status=owner_status, session_id=legacy,
                 blocker="launch incomplete: push preparation failed")
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "batch-fix").values(fixer_state=owner))
        await defer_on(conn, await db.get_integration_batch("batch-fix"), now=now[0],
                       reason=owner["blocker"], head_sha=HEAD)
    if session_state:
        await register(db, owner, session_state)
    successor = Batch("legacy-next", "p", "r", "refs/heads/dev")
    store = BatchStore(db)
    await store.freeze(successor, await store.members("batch-fix"),
                       trees={"one": "e" * 40, "two": "f" * 40})
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == successor.id)
                           .values(fixer_queue={"family_id": "batch-fix"}))
    now[0] += 5000
    result = await allocate_on(service, successor.id)
    assert result["outcome"] == "filed"
    assert result["session_id"] == str(UUID(result["session_id"]))
    assert (UUID(result["session_id"]) == UUID(legacy)) is (session_state is None)
    assert (await state(db)) is None
    moved = (await db.get_integration_batch(successor.id))["fixer_state"]
    assert moved["batch_id"] == successor.id and moved["starting_sha"] == NEXT
    assert moved["status"] == "active" and "blocker" not in moved
    assert moved["family_id"] == "batch-fix"
    assert moved["deadline"] is None
    assert runtime.start.await_args.args[0]["session_id"] == result["session_id"]
    assert runtime.start.await_count == 2


async def test_family_keeps_launch_blocker_while_its_process_may_exist(db, fixer):
    service, runtime, _ = fixer

    async def uncertain(launch):
        await register(db, launch, "starting")  # stop could not be confirmed
        raise RuntimeError("launch cleanup could not confirm stop")

    runtime.start.side_effect = uncertain
    assert (await allocate(service))["outcome"] == "deferred"
    owner = await state(db)
    assert owner["blocker"].startswith("launch incomplete")
    successor = Batch("uncertain-next", "p", "r", "refs/heads/dev")
    store = BatchStore(db)
    await store.freeze(successor, await store.members("batch-fix"),
                       trees={"one": "e" * 40, "two": "f" * 40})
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == successor.id)
                           .values(fixer_queue={"family_id": "batch-fix"}))
    assert (await allocate_on(service, successor.id))["outcome"] == "deferred"
    runtime.start.assert_awaited_once()
    assert (await state(db))["session_id"] == owner["session_id"]


async def test_ambiguous_launch_is_retained_and_never_retried(db, fixer):
    service, runtime, now = fixer

    async def uncertain(launch):
        await register(db, launch, "starting")
        raise RuntimeError("process registration succeeded; provider unavailable")

    runtime.start.side_effect = uncertain
    assert (await allocate(service))["outcome"] == "deferred"
    now[0] += 2000
    await allocate(service)
    runtime.start.assert_awaited_once()
    assert len(await notices(db, supervisor=True)) == 1
    await service.settle_green("batch-fix", HEAD)
    assert (await state(db))["status"] == "green"


@pytest.mark.parametrize("reason", [
    "lock_wait_timeout", "git_timeout", "git_exit", "import_mismatch",
])
async def test_preparation_reason_reaches_launch_retry_and_train_notice(db, fixer, reason):
    service, runtime, _ = fixer
    runtime.start.side_effect = _git_preparation_error(
        reason, subcommand="fetch", elapsed=120, budget=120,
        returncode=128 if reason == "git_exit" else None,
        diagnostic_kind="fatal" if reason == "git_exit" else "none",
    )
    result = await allocate(service)
    assert result["outcome"] == "deferred"
    assert f"reason={reason}" in result["reason"]
    batch_id = "batch-fix"
    for attempt in range(1, 4):
        row = await db.get_integration_batch(batch_id)
        current = row["fixer_state"]
        assert current["status"] == "waiting_capacity" and "blocker" not in current
        assert row["fixer_queue"]["reason"] == "launch_retry"
        assert f"reason={reason}" in current["launch_error"]
        assert "subcommand=fetch" in current["launch_error"]
        if attempt < 3:
            assert not await notices(db, supervisor=True)
            successor = f"preparation-retry-{attempt}"
            await transfer(db, batch_id, successor)
            batch_id = successor
            result = await allocate_on(service, batch_id)
            assert result["outcome"] == "deferred"
            assert f"reason={reason}" in result["reason"]
    notice, = await notices(db, supervisor=True)
    assert notice["from_id"] == "integration-train"
    assert f"reason={reason}" in notice["body"]
    assert "subcommand=fetch" in notice["body"]
    assert "retries with backoff" in notice["body"]
    assert runtime.start.await_count == 3


@pytest.mark.parametrize("intent", ["paused", "aborted"])
async def test_operator_intent_refuses_launch(db, fixer, intent):
    service, runtime, _ = fixer
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).values(intent=intent))
    assert (await allocate(service))["outcome"] == "held"
    runtime.start.assert_not_awaited()


async def test_withdrawn_candidate_refuses_launch(db, fixer):
    service, runtime, _ = fixer
    result = await service.allocate("batch-fix", target_ref=candidate_ref("batch-fix"),
                                   head_sha=HEAD, authorize=AsyncMock(return_value=False))
    assert result["outcome"] == "withdrawn"
    runtime.start.assert_not_awaited()


async def test_arbitrary_branch_and_operator_identity_cannot_push(fixer):
    service, runtime, _ = fixer
    with pytest.raises(ValueError, match="not this batch"):
        await service.allocate("batch-fix", target_ref="refs/heads/unrelated", head_sha=HEAD)
    result = await service.command(ExecutionPrincipal.service("integration-train"), action="push")
    assert result["outcome"] == "unauthorized"
    runtime.push.assert_not_awaited()


def test_split_keeps_healthy_members_and_defers_transitive_dependents():
    assert split_members(["a", "b", "c", "d"], [{"a"}], {"b": ["a"], "c": ["b"]}) == {
        "retained": ["d"], "deferred": ["a", "b", "c"],
    }


@pytest.mark.parametrize("groups", [[], [None], [set()], [{"a", "b"}], [{"outside"}]])
def test_no_split_without_a_strict_attributed_subset(groups):
    assert split_members(["a", "b"], groups, {}) is None


@pytest.fixture
async def live_fixer(db, tmp_path, monkeypatch, request):
    """Production session launch and real Git; substitute only credentials and routing."""
    from src.api.auth import SessionTokenStore
    from src.config import AppConfig, DiscordConfig
    from src.database.tables import projects
    from src.git.github_contracts import GitHubRepositoryBinding
    from src.integration.fixer_runtime import DaemonFixerRuntime
    from src.integration.gitops import RetainedRepository
    from src.models import AgentProfile
    from src.sessions import SessionProviderRegistry
    from src.sessions.fake import FakeProvider
    from src.sessions.harness_parser import Harness
    from src.sessions.harness_registry import HarnessRegistry
    from src.sessions.spec import SessionSpecBuilder
    from tests.git_seed_helpers import clone_seed, init_bare_seed
    from tests.test_integration_gitops import LocalGit, commit, git

    remote, store = tmp_path / "remote.git", tmp_path / "retained"
    init_bare_seed(remote)
    clone_seed(remote, store)
    git(store, "config", "user.name", "Tester")
    git(store, "config", "user.email", "tester@example.test")
    base = commit(store, {"base.txt": "base\n"})
    git(store, "push", "origin", "HEAD:main")
    source = commit(store, {"tests/test_example.py": "assert True\n"})
    git(store, "push", "origin", "HEAD:" + candidate_ref("real").removeprefix("refs/heads/"))
    batch = Batch(id="real", project_id="p", repository_id="r", target_ref="refs/heads/main")
    await BatchStore(db).freeze(batch, (
        BatchMember(task_id="member", source_sha=source, source_base_sha=base),
    ), trees={"member": git(store, "rev-parse", source + "^{tree}")})
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_mode="development", integration_repository_id="r",
        ))
    config = AppConfig(discord=DiscordConfig(bot_token="test", guild_id="1"),
                       data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "work"))
    config.sessions.enabled, config.sessions.provider = True, "fake"
    registry = HarnessRegistry()
    overrides = dict(getattr(request, "param", {}))
    repair_policy = overrides.pop("repair_policy", None)
    harness_id = overrides.get("id", "codex")
    permission_flag = ("--dangerously-bypass-approvals-and-sandbox" if harness_id == "codex"
                       else "--dangerously-skip-permissions")
    harness = Harness(**{
        "id": harness_id, "command": harness_id, "model_flag": "--model",
        "permission_flag": permission_flag, "supports_hooks": harness_id == "codex",
        "hook_files": ((".codex/hooks.json", "hooks/codex.json"),) if harness_id == "codex" else (),
        "hook_trust_flag": "--dangerously-bypass-hook-trust" if harness_id == "codex" else "",
        **overrides,
    })
    registry.upsert(harness)
    profile = AgentProfile(id="fixer-profile", name="Fixer", harness=harness.id,
                           model="test-model", lifecycle="pool")
    await db.create_profile(profile)
    transport = LocalGit(remote)
    repo = RetainedRepository("r", store, GitHubRepositoryBinding(123, "test/repo"), "main")
    lane = SimpleNamespace(service=SimpleNamespace(
        gitops=SimpleNamespace(repository=AsyncMock(return_value=repo)),
        _authorized=AsyncMock(return_value=True),
    ))
    orch = SimpleNamespace(db=db, git=transport, config=config, daemon_epoch="epoch",
                           harness_registry=registry, token_store=SessionTokenStore(db),
                           session_spec_builder=SessionSpecBuilder(config, registry),
                           session_providers=SessionProviderRegistry({"fake": FakeProvider}),
                           integration_train=None, _paused=False)
    runtime = DaemonFixerRuntime(orch, lanes=AsyncMock(return_value=lane))
    runtime.route = AsyncMock(return_value=(profile, {"profile_id": profile.id}))

    # Only the credential and GitHub URL are substituted: launch and rebind go
    # through the real afetch_repository_oid and its refs/aq/* destination guard.
    real_fetch = transport._afetch_exact_oid_with_app_auth_to_url

    async def file_source_fetch(destination, **kwargs):
        assert kwargs["destination_url"] == "https://github.com/test/repo.git"
        return await real_fetch(destination, **(kwargs | {"destination_url": remote.as_uri()}))

    monkeypatch.setattr(transport, "_atoken_for_repository", AsyncMock(return_value=None))
    monkeypatch.setattr(transport, "_afetch_exact_oid_with_app_auth_to_url", file_source_fetch)
    service = BatchFixerService(db, runtime)
    result = await service.allocate("real", target_ref=candidate_ref("real"), head_sha=source,
                                    authorize=AsyncMock(return_value=True),
                                    policy=(RepairPolicy.model_validate(repair_policy)
                                            if repair_policy is not None else None))
    assert result["outcome"] == "filed", result
    row = await db.get_session(result["session_id"])
    return service, runtime, orch, row, base, source, lane


def principal_for(row):
    from src.commands.principal import PrincipalKind
    from src.profiles.capabilities import DENY_ALL

    return ExecutionPrincipal(PrincipalKind.SESSION, DENY_ALL, session_id=row.id,
                              session_instance_token=row.instance_token, project_id=row.project_id)


@pytest.mark.parametrize("live_fixer", [{}, {"id": "claude", "command": "claude"}],
                         indirect=True)
async def test_registered_named_launch_has_no_claim_and_preserves_checkout(db, live_fixer):
    service, runtime, orch, row, _, source, _ = live_fixer
    assert row.lifecycle == "named" and row.task_id is None and row.state == "running"
    provider = orch.session_providers.create("fake")
    spec = provider.sessions[row.name].spec
    harness = orch.harness_registry.get(row.harness, project_id="p")
    assert spec.command.count(harness.permission_flag) == 1
    if row.harness == "codex":
        assert "--dangerously-bypass-hook-trust" in spec.command
        assert spec.hooks_provisioned
    assert "AQ_TASK_ID" not in spec.env and "AQ_CLAIM_EPOCH" not in spec.env
    assert spec.env["AQ_DB_SCOPE"] == "worker"
    assert spec.env["GIT_AUTHOR_EMAIL"]
    assert await runtime.alive((await db.get_integration_batch("real"))["fixer_state"])
    for _ in range(2):
        assert (await service.allocate("real", target_ref=candidate_ref("real"), head_sha=source,
                                       authorize=AsyncMock(return_value=True)))["outcome"] == "exists"
    assert len(provider.starts) == 1
    assert not await db.get_task("real")
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(tasks)) == 0


@pytest.mark.parametrize("changed", ["instance_token", "launch_started_at"])
async def test_late_registration_refuses_changed_launch_fence(db, live_fixer, monkeypatch, changed):
    from src.integration.lock import PublishWithdrawn

    _, runtime, orch, row, _, _, _ = live_fixer
    original = (await db.get_integration_batch("real"))["fixer_state"]
    current = {**original, "status": "launching",
               changed: "replacement" if changed == "instance_token" else runtime.clock() + 120}
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "real",
        ).values(fixer_state=current))
    monkeypatch.setattr(db, "get_session", AsyncMock(return_value=None))
    provider = orch.session_providers.create("fake")
    starts = len(provider.starts)
    with pytest.raises(PublishWithdrawn, match="registration authority was withdrawn"):
        await runtime.start(original)
    assert len(provider.starts) == starts
    assert (await db.get_integration_batch("real"))["fixer_state"] == current


@pytest.mark.parametrize("checkout", ["unregistered", "primary"])
async def test_fixer_refuses_checkout_without_linked_registration(db, live_fixer, monkeypatch,
                                                                 checkout):
    from pathlib import Path

    _, runtime, orch, row, _, _, _ = live_fixer
    state = (await db.get_integration_batch("real"))["fixer_state"]
    path = Path(row.work_dir)
    if checkout == "unregistered":
        monkeypatch.setattr(runtime.git, "aworktree_list", AsyncMock(return_value=[]))
    else:
        path = runtime.git.remote_path.parent / "retained"
    monkeypatch.setattr("src.warm_workspaces.WarmWorkspacePool.acquire",
                        AsyncMock(return_value=path))
    monkeypatch.setattr(db, "get_session", AsyncMock(return_value=None))
    mint = AsyncMock()
    monkeypatch.setattr(orch.token_store, "mint", mint)
    starts = len(orch.session_providers.create("fake").starts)
    with pytest.raises(ValueError, match="not a registered linked worktree"):
        await runtime._start(state)
    mint.assert_not_awaited()
    assert len(orch.session_providers.create("fake").starts) == starts


@pytest.mark.parametrize("live_fixer", [
    {"id": "claude", "command": "claude", "session_id_flag": "--session-id"},
], indirect=True)
async def test_session_id_flag_launch_carries_a_dashed_uuid(db, live_fixer):
    # Claude rejects a 32-hex --session-id ("Must be a valid UUID") and dies at startup.
    _, _, orch, row, _, _, _ = live_fixer
    command = orch.session_providers.create("fake").sessions[row.name].spec.command
    value = command[command.index("--session-id") + 1]
    assert value == row.id == row.session_key
    assert str(UUID(value)) == value


async def test_launch_pins_heads_target_start_in_daemon_namespace(db, live_fixer):
    from src.git.github_contracts import GitHubRepositoryBinding
    from src.git.manager import GitError
    from tests.test_integration_gitops import git

    _service, runtime, _orch, row, _, source, _ = live_fixer
    state = (await db.get_integration_batch("real"))["fixer_state"]
    assert state["target_ref"] == candidate_ref("real")
    assert state["target_ref"].startswith("refs/heads/")
    store = runtime.git.remote_path.parent / "retained"
    assert git(store, "rev-parse", f"refs/aq/fixer-sources/{source}") == source
    assert git(row.work_dir, "branch", "--show-current") == candidate_ref("real")[11:]
    assert git(row.work_dir, "rev-parse", "HEAD") == source
    with pytest.raises(GitError, match="daemon recovery namespace"):
        await runtime.git.afetch_repository_oid(
            str(store), repository=GitHubRepositoryBinding(123, "test/repo"), oid=source,
            destination_ref=state["target_ref"])


async def test_real_push_is_scoped_cas_idempotent_and_green_revokes_it(db, live_fixer):
    from pathlib import Path
    from tests.test_integration_gitops import commit, git

    service, runtime, orch, row, _, source, _ = live_fixer
    fixed = commit(Path(row.work_dir), {"fix.txt": "fixed\n"})
    principal = principal_for(row)
    result = await service.command(principal, action="push")
    assert result["head_sha"] == fixed and runtime.git.pushes == 1
    assert git(runtime.git.remote_path, "rev-parse", candidate_ref("real")) == fixed
    assert git(runtime.git.remote_path, "rev-parse", "main") != fixed
    assert (await service.command(principal, action="push"))["head_sha"] == fixed
    assert runtime.git.pushes == 1
    await service.settle_green("real", fixed)
    assert (await service.command(principal, action="push"))["outcome"] == "held"
    assert (await db.get_session(row.id)).desired_state == "running"
    await service.settle_delivered("real", fixed)
    assert (await db.get_session(row.id)).desired_state == "stopped"


@pytest.mark.parametrize("failure", ["spawn_or_wait", "timeout", "git_exit", "broker_not_served"])
async def test_authenticated_push_reason_reaches_command_blocker_and_notice(
    db, live_fixer, tmp_path, monkeypatch, failure,
):
    from pathlib import Path
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import principal_context
    from tests.test_integration_gitops import commit, git

    service, runtime, orch, row, _, source, _ = live_fixer
    fixed = commit(Path(row.work_dir), {"fix.txt": "fixed\n"})
    token = "private-installation-token"
    fake_git = tmp_path / "push-git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'Authorization: Bearer private-installation-token' "
        "'https://alice:password@github.com/test/repo.git' "
        "'private-installation-token' 'fatal: upstream unavailable' >&2\n"
        f"exit {7 if failure == 'git_exit' else 0}\n"
    )
    fake_git.chmod(0o700)
    runtime.git._APP_GIT_EXECUTABLE = str(fake_git)
    spawn = runtime.git._spawn_isolated_git

    async def start(*args, **kwargs):
        if "push" in args:
            if failure == "spawn_or_wait":
                raise FileNotFoundError(token)
            if failure == "timeout":
                raise TimeoutError(token)
        return await spawn(*args, **kwargs)

    async def push(path, *, tip_oid, branch, expected_old_oid, **kwargs):
        return await runtime.git._apush_oid_with_app_auth_to_url(
            path, destination_url="https://github.com/test/repo.git", token=token,
            tip_oid=tip_oid, branch=branch, expected_old_oid=expected_old_oid,
        )

    monkeypatch.setattr(runtime.git, "_spawn_isolated_git", start)
    monkeypatch.setattr(runtime.git, "apush_repository_oid", push)
    orch.integration_train = SimpleNamespace(repair=service)
    handler = SimpleNamespace(orchestrator=orch)
    with principal_context(principal_for(row)):
        result = await IntegrationCommandsMixin._cmd_integration_batch_fix(handler, {"action": "push"})
    assert result["outcome"] == "refused"
    bound = (await db.get_integration_batch("real"))["fixer_state"]
    notice, = await notices(db, supervisor=True)
    for output in (result["error"], bound["blocker"], notice["body"]):
        assert f"reason={failure}" in output
        assert token not in output and "password" not in output and "alice:" not in output
        if failure == "git_exit":
            assert "returncode=7" in output and "fatal: upstream unavailable" in output
        elif failure in {"spawn_or_wait", "timeout"}:
            assert f"exception={'TimeoutError' if failure == 'timeout' else 'FileNotFoundError'}" in output
        else:
            assert "credential broker did not serve token" in output
    assert bound["work_dir"] == row.work_dir
    assert git(runtime.git.remote_path, "rev-parse", candidate_ref("real")) == source
    assert git(row.work_dir, "rev-parse", "HEAD") == fixed


@pytest.mark.parametrize("change", ["wrong_branch", "rewrite", "dirty", "source_withdrawn",
                                    "mode_off", "paused", "stale_session"])
async def test_real_publication_refuses_changed_authority_or_history(db, live_fixer, change):
    from dataclasses import replace
    from pathlib import Path
    from src.database.tables import projects
    from src.git.manager import GitError
    from src.integration.lock import PublishWithdrawn
    from tests.test_integration_gitops import commit, git

    service, runtime, orch, row, base, source, lane = live_fixer
    commit(Path(row.work_dir), {"fix.txt": "fixed\n"})
    principal = principal_for(row)
    if change == "wrong_branch":
        git(row.work_dir, "checkout", "-b", "unrelated")
    elif change == "rewrite":
        git(row.work_dir, "reset", "--hard", base)
    elif change == "dirty":
        (Path(row.work_dir) / "dirty.txt").write_text("uncommitted")
    elif change == "source_withdrawn":
        lane.service._authorized.return_value = False
    elif change == "mode_off":
        async with db.immediate() as conn:
            await conn.execute(update(projects).values(hierarchical_integration_mode="disabled"))
    elif change == "paused":
        orch._paused = True
    else:
        principal = replace(principal, session_instance_token="stale")
    if change == "stale_session":
        assert (await service.command(principal, action="push"))["outcome"] == "unauthorized"
    else:
        with pytest.raises((GitError, PublishWithdrawn)):
            await service.command(principal, action="push")
    assert runtime.git.pushes == 0
    assert git(runtime.git.remote_path, "rev-parse", candidate_ref("real")) == source


async def test_same_head_new_check_attempt_is_one_new_round(db, fixer):
    service, runtime, _ = fixer
    runtime.failure_input = AsyncMock(side_effect=lambda _batch, evidence: evidence)
    evidence = {"head": HEAD, "run_id": "run", "attempt": 1}
    await allocate(service, failure_evidence=evidence)
    await allocate(service, failure_evidence=evidence)
    evidence = {**evidence, "attempt": 2}
    await allocate(service, failure_evidence=evidence)
    await allocate(service, failure_evidence=evidence)
    assert len(await notices(db)) == 1
    assert len((await state(db))["red_keys"]) == 2


async def test_failure_input_uses_frozen_file_ownership_and_ignores_observation_clock(live_fixer):
    service, runtime, _, _, _, source, _ = live_fixer
    evidence = {
        "sha": source, "required": {"names": ["unit"], "version": "v1", "producer_id": "app"},
        "checks": [{"repository_id": "r", "sha": source, "name": "unit", "producer_id": "app",
                    "required_check_version": "v1", "conclusion": "failure", "run_id": "run",
                    "detail": {"failing_test_ids": ["tests/test_example.py::test_a"]},
                    "observed_at": 100}],
    }
    first = await runtime.failure_input("real", evidence)
    evidence["checks"][0]["observed_at"] = 999
    assert await runtime.failure_input("real", evidence) == first
    assert len(first["groups"]) == 1
    assert first["groups"][0]["attributed_member"] == "member"
    assert first["groups"][0]["frozen_members"][0]["source_sha"] == source


# These fixtures use real retained repositories and the ordinary publication gate.
from tests.test_integration_batches import batch_env as batch_env  # noqa: E402
from tests.test_integration_gitops import (  # noqa: E402
    git_history_template as git_history_template, setup as setup,
)


@pytest.fixture
async def split_env(batch_env):
    from dataclasses import replace
    from src.integration.fixer_batches import FixerBatches
    from src.integration.lock import BranchLock
    from tests.test_integration_gitops import commit, git

    db, ops, repo, base, head, store, batch, members, green, approved, snapshot, service = batch_env
    # The starting fixture owns the lane; retire it before freezing three sources.
    await store.set_intent(batch.id, "aborted")
    bad = commit(repo.store, {"bad.txt": "red\n"}, base=base)
    dependent = commit(repo.store, {"dependent.txt": "depends on bad\n"}, base=bad)
    batch = replace(batch, id="split-source")
    members = (members[0], BatchMember("bad", bad, base, 1),
               BatchMember("dependent", dependent, base, 2))
    await store.freeze(batch, members, trees={
        m.task_id: git(repo.store, "rev-parse", m.source_sha + "^{tree}") for m in members})
    first = await service.visit(batch, members, await snapshot(batch))
    assert first.state == "testing", first
    fixed = commit(repo.store, {"saved-fix.txt": "must survive split\n"}, base=first.candidate_sha)
    git(repo.store, "push", "origin", fixed + ":" + candidate_ref(batch.id))
    runtime = SimpleNamespace(lane=AsyncMock(return_value=(batch, SimpleNamespace(service=service))),
                              locks=BranchLock(db))
    splitter = FixerBatches(db, clock=lambda: 1000)
    evidence = {"head_sha": fixed, "groups": [{"attributed_member": "bad"}]}
    return (db, ops, repo, base, store, batch, members, green, snapshot, service,
            runtime, splitter, evidence, fixed)


async def test_split_dependency_closure_preserves_fixes_and_green_remainder(split_env):
    from src.integration.batches import ejection_instruction, withheld_sources_query
    from src.integration.train import TrainTarget
    from tests.test_integration_gitops import git

    (db, ops, repo, base, store, batch, members, green, snapshot, service,
     runtime, splitter, evidence, fixed) = split_env
    result = await splitter.split(runtime, batch.id, evidence, policy=None,
                                  authorize=AsyncMock(return_value=True))
    assert result["retained"] == ["task"]
    assert result["deferred"] == ["bad", "dependent"]
    assert await store.members(batch.id) == members
    original = await db.get_integration_batch(batch.id)
    assert original["fixer_queue"]["revisit_at"] == 1300
    assert original["cleanup_state"] == "retained"
    assert git(ops.git.remote_path, "rev-parse", candidate_ref(batch.id)) == fixed
    assert await splitter.split(runtime, batch.id, evidence, policy=None,
                                authorize=AsyncMock(return_value=True)) is None
    target = TrainTarget("p", "r", batch.target_ref)
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(ejection_instruction(batch.id)))
        withheld = (await conn.execute(withheld_sources_query(target))).mappings().all()
        assert {m["task_id"] for m in withheld if m["batch_id"] == batch.id} == {
            "task", "bad", "dependent"}
    reduced = await store.get(result["replacement_batch_id"])
    retained = await store.members(reduced.id)
    observation = await service.visit(reduced, retained, await snapshot(reduced))
    assert observation.state == "testing", observation
    assert git(repo.store, "merge-base", "--is-ancestor", base, observation.candidate_sha) == ""
    assert not await ops.is_ancestor(repo, members[1].source_sha, observation.candidate_sha)
    green.add(observation.candidate_sha)
    delivered = await service.visit(reduced, retained, await snapshot(reduced))
    assert delivered.state == "delivered", delivered
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == reduced.id)
                           .values(lifecycle="promoted"))
    resumed = await splitter.resume(target, await snapshot(batch), service)
    assert resumed.id.startswith(batch.id + "-resume-")
    observation = await service.visit(resumed, members, await snapshot(batch))
    assert observation.state == "testing", observation
    assert await ops.is_ancestor(repo, fixed, observation.candidate_sha)
    assert git(repo.store, "show", observation.candidate_sha + ":saved-fix.txt") == "must survive split"
    green.add(observation.candidate_sha)
    assert (await service.visit(resumed, members, await snapshot(batch))).state == "delivered"


@pytest.mark.parametrize("why", ["shared", "all", "policy", "withdrawn"])
async def test_unsafe_or_disabled_split_keeps_original_batch(split_env, why):
    *_, runtime, splitter, evidence, _fixed = split_env
    db, _, _, _, _, batch = split_env[:6]
    if why == "shared":
        evidence["groups"].append({"attributed_member": None})
    if why == "all":
        evidence["groups"].append({"attributed_member": "task"})
    policy = RepairPolicy(debug_intelligence_class="deep-high", split_attributed=why != "policy")
    assert await splitter.split(runtime, batch.id, evidence, policy=policy,
        authorize=AsyncMock(return_value=why != "withdrawn")) is None
    assert (await db.get_integration_batch(batch.id))["intent"] == "open"


async def test_deferred_unchanged_due_observation_does_not_readmit_or_lose_fixes(split_env):
    from src.integration.fixer_batches import defer_on
    from src.integration.train import TrainTarget

    (db, _, _, _, _, batch, _, green, snapshot, service,
     _, splitter, _, fixed) = split_env
    async with db.immediate() as conn:
        row = await db.get_integration_batch(batch.id)
        await defer_on(conn, row, now=0, reason="budget", head_sha=fixed, revisit_seconds=30)
    target = TrainTarget("p", "r", batch.target_ref)
    assert await splitter.resume(target, await snapshot(batch), service) is None
    row = await db.get_integration_batch(batch.id)
    assert row["fixer_queue"]["revisit_at"] == 1030
    assert row["intent"] == "aborted"
    green.add(fixed)
    splitter.clock = lambda: 1031
    assert (await splitter.resume(target, await snapshot(batch), service)).id.startswith(
        batch.id + "-resume-")


async def test_reduced_red_reuses_one_session_and_requires_clean_fenced_rebind(db, live_fixer):
    from pathlib import Path
    from src.integration.lock import PublishWithdrawn
    from tests.test_integration_gitops import git

    service, runtime, orch, session, base, source, _lane = live_fixer
    reduced = Batch("reduced", "p", "r", "refs/heads/main")
    await BatchStore(db).freeze(reduced, (BatchMember("retained", base, base),),
                               trees={"retained": git(session.work_dir, "rev-parse", base + "^{tree}")})
    git(runtime.git.remote_path, "update-ref", candidate_ref(reduced.id), base)
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == reduced.id)
                           .values(fixer_queue={"family_id": "real"}))
    args = dict(target_ref=candidate_ref(reduced.id), head_sha=base,
                authorize=AsyncMock(return_value=True))
    for _ in range(3):
        result = await service.allocate(reduced.id, **args)
        assert result["session_id"] == session.id and result["rebind_required"]
    assert len(await notices(db)) == 1
    dirty = Path(session.work_dir) / "dirty.txt"
    dirty.write_text("unsaved")
    with pytest.raises(PublishWithdrawn, match="save and publish"):
        await service.command(principal_for(session), action="rebind")
    dirty.unlink()
    result = await service.command(principal_for(session), action="rebind")
    assert result["outcome"] == "bound"
    assert result["fixer"]["batch_id"] == reduced.id
    assert git(session.work_dir, "branch", "--show-current") == candidate_ref(reduced.id)[11:]
    assert (await service.for_session(session.id))["id"] == reduced.id
    assert (await db.get_integration_batch("real"))["fixer_state"] is None
    assert git(runtime.git.remote_path, "rev-parse", candidate_ref("real")) == source
    assert len(orch.session_providers.create("fake").starts) == 1
    assert (await service.command(principal_for(session), action="rebind"))["outcome"] == "already_bound"


async def test_capacity_retry_reuses_unlaunched_identity_without_spending_rounds(db, fixer):
    from src.integration.fixer_runtime import FixerCapacityUnavailable

    service, runtime, now = fixer
    runtime.start.side_effect = FixerCapacityUnavailable("fleet is full")
    first = await allocate(service)
    assert first["outcome"] == "deferred"
    waiting = await state(db)
    assert waiting["status"] == "waiting_capacity"
    assert not await notices(db)
    # The durable due observer transfers this unlaunched identity to a successor.
    successor = Batch("capacity-next", "p", "r", "refs/heads/dev")
    store = BatchStore(db)
    await store.freeze(successor, await store.members("batch-fix"),
                       trees={"one": "e" * 40, "two": "f" * 40})
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == "batch-fix")
                           .values(fixer_state=None))
        await conn.execute(update(integration_batches).where(integration_batches.c.id == successor.id)
                           .values(fixer_state={**waiting, "batch_id": successor.id,
                                                "target_ref": candidate_ref(successor.id)},
                                   fixer_queue={"family_id": "batch-fix"}))
    now[0] += 5000
    runtime.start.side_effect = None
    result = await service.allocate(successor.id, target_ref=candidate_ref(successor.id),
        head_sha=NEXT, authorize=AsyncMock(return_value=True))
    assert result["outcome"] == "filed"
    assert result["session_id"] == first["session_id"]
    assert str(UUID(first["session_id"])) == first["session_id"]
    current = (await db.get_integration_batch(successor.id))["fixer_state"]
    assert current["red_heads"] == [NEXT]
    assert current["starting_sha"] == NEXT
    assert current["deadline"] is None


async def test_deferred_writer_can_save_but_green_family_revokes_it(db, live_fixer):
    from pathlib import Path
    from src.integration.fixer_batches import defer_on
    from tests.test_integration_gitops import commit

    service, runtime, _orch, session, _base, source, lane = live_fixer
    lane.service.eligible = AsyncMock(return_value=True)
    async with db.immediate() as conn:
        await defer_on(conn, await db.get_integration_batch("real"), now=0,
                       reason="budget", head_sha=source)
    fixed = commit(Path(session.work_dir), {"saved.txt": "late fix\n"})
    assert (await service.command(principal_for(session), action="push"))["head_sha"] == fixed
    await service.settle_delivered("real", fixed)
    assert (await service.command(principal_for(session), action="push"))["outcome"] == "held"
    assert (await db.get_session(session.id)).desired_state == "stopped"


async def test_companion_change_withdraws_previously_green_candidate(split_env):
    from src.integration.train import TrainTarget
    from tests.test_integration_gitops import commit, git

    (db, ops, repo, _, store, batch, members, green, snapshot, service,
     runtime, splitter, evidence, fixed) = split_env
    result = await splitter.split(runtime, batch.id, evidence, policy=None,
                                  authorize=AsyncMock(return_value=True))
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == result["replacement_batch_id"])
            .values(lifecycle="promoted"))
    resumed = await splitter.resume(TrainTarget("p", "r", batch.target_ref),
                                     await snapshot(batch), service)
    frozen_snapshot = await snapshot(resumed)
    first = await service.visit(resumed, members, frozen_snapshot)
    green.add(first.candidate_sha)
    frozen_snapshot = await snapshot(resumed)
    later = commit(repo.store, {"late-fix.txt": "also retain me\n"}, base=fixed)
    git(repo.store, "push", "origin", later + ":" + candidate_ref(batch.id))
    # Cached green is insufficient once the source companion has advanced.
    stale = await service.visit(resumed, members, frozen_snapshot)
    assert stale.state == "held", stale
    fresh = await service.visit(resumed, members, await snapshot(resumed))
    assert fresh.state == "testing", fresh
    assert await ops.is_ancestor(repo, later, fresh.candidate_sha)


async def test_deferred_successor_observes_late_fix_on_retained_writer(split_env):
    from src.integration.fixer_batches import defer_on
    from src.integration.train import TrainTarget
    from tests.test_integration_gitops import commit, git

    (db, ops, repo, _, store, batch, members, green, snapshot, service,
     runtime, splitter, evidence, fixed) = split_env
    target = TrainTarget("p", "r", batch.target_ref)
    async with db.immediate() as conn:
        await defer_on(conn, await db.get_integration_batch(batch.id), now=0,
                       reason="budget", head_sha="0" * 40)
    resumed = await splitter.resume(target, await snapshot(batch), service)
    assembled = await service.visit(resumed, members, await snapshot(resumed))
    async with db.immediate() as conn:
        await defer_on(conn, await db.get_integration_batch(resumed.id), now=0,
                       reason="budget", head_sha=assembled.candidate_sha)
    # Only the original fixer branch moves; the deferred successor stays red.
    later = commit(repo.store, {"late-fix.txt": "saved while deferred\n"}, base=fixed)
    git(repo.store, "push", "origin", later + ":" + candidate_ref(batch.id))
    following = await splitter.resume(target, await snapshot(resumed), service)
    assert following is not None
    queue = (await db.get_integration_batch(following.id))["fixer_queue"]
    assert queue["companion_batch_id"] == batch.id
    fresh = await service.visit(following, members, await snapshot(following))
    assert fresh.state == "testing", fresh
    assert await ops.is_ancestor(repo, later, fresh.candidate_sha)


@pytest.mark.parametrize("reason", ["launch_retry", "budget"])
async def test_due_launch_retry_resumes_unchanged_head_with_its_identity(split_env, reason):
    from src.integration.fixer_batches import defer_on
    from src.integration.train import TrainTarget

    (db, _ops, _repo, _, _store, batch, _members, _green, snapshot, service,
     _runtime, splitter, _evidence, fixed) = split_env
    waiting = {"status": "waiting_capacity", "session_id": "retry-me", "family_id": batch.id,
               "batch_id": batch.id, "target_ref": candidate_ref(batch.id)}
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == batch.id)
                           .values(fixer_state=waiting))
        await defer_on(conn, await db.get_integration_batch(batch.id), now=0,
                       reason=reason, head_sha=fixed, revisit_seconds=600)
    resumed = await splitter.resume(TrainTarget("p", "r", batch.target_ref),
                                    await snapshot(batch), service)
    if reason == "budget":  # Unchanged exhausted work never re-admits.
        assert resumed is None
        return
    moved = (await db.get_integration_batch(resumed.id))["fixer_state"]
    assert moved["session_id"] == "retry-me" and moved["batch_id"] == resumed.id
    assert (await db.get_integration_batch(batch.id))["fixer_state"] is None


async def test_capacity_retry_dashes_a_legacy_hex_identity_before_launch(db, fixer):
    from src.integration.fixer_runtime import FixerCapacityUnavailable

    service, runtime, now = fixer
    runtime.start.side_effect = FixerCapacityUnavailable("fleet is full")
    assert (await allocate(service))["outcome"] == "deferred"
    # A row deferred before identities were minted dashed carries a 32-hex id.
    legacy = uuid4().hex
    successor = Batch("legacy-next", "p", "r", "refs/heads/dev")
    store = BatchStore(db)
    await store.freeze(successor, await store.members("batch-fix"),
                       trees={"one": "e" * 40, "two": "f" * 40})
    async with db.immediate() as conn:
        waiting = await state(db)
        await conn.execute(update(integration_batches).where(integration_batches.c.id == "batch-fix")
                           .values(fixer_state=None))
        await conn.execute(update(integration_batches).where(integration_batches.c.id == successor.id)
                           .values(fixer_state={**waiting, "session_id": legacy,
                                                "batch_id": successor.id,
                                                "target_ref": candidate_ref(successor.id)},
                                   fixer_queue={"family_id": "batch-fix"}))
    now[0] += 5000
    runtime.start.side_effect = None
    result = await service.allocate(successor.id, target_ref=candidate_ref(successor.id),
        head_sha=NEXT, authorize=AsyncMock(return_value=True))
    assert result["outcome"] == "filed"
    dashed = str(UUID(legacy))
    assert result["session_id"] == dashed
    assert (await db.get_integration_batch(successor.id))["fixer_state"]["session_id"] == dashed
    assert runtime.start.await_args_list[-1].args[0]["session_id"] == dashed


@pytest.mark.parametrize("policy", [None, RepairPolicy(
    primary_seconds=0, primary_attempts=0, debug_intelligence_class="deep-high",
)])
async def test_unlimited_fixer_survives_many_red_heads_and_elapsed_time(db, fixer, policy):
    service, runtime, now = fixer
    first = await allocate(service, policy=policy)
    for number in range(1, 8):
        now[0] += 10_000
        result = await allocate(service, f"{number:040x}", policy=policy)
        assert result["outcome"] == "exists" and result["session_id"] == first["session_id"]
    current = await state(db)
    assert current["deadline"] is None and current["max_rounds"] == 0
    assert len(current["red_keys"]) == 8
    assert not await notices(db, supervisor=True)
    assert len(await notices(db)) == 7
    runtime.start.assert_awaited_once()


async def test_fixer_honors_configured_round_budget_above_three(db, fixer):
    service, _, _ = fixer
    policy = RepairPolicy(primary_seconds=0, primary_attempts=5,
                          debug_intelligence_class="deep-high")
    await allocate(service, policy=policy)
    for number in range(1, 5):
        assert (await allocate(service, f"{number:040x}"))["outcome"] == "exists"
    result = await allocate(service, "9" * 40)
    assert result["outcome"] == "deferred"
    assert result["reason"] == "fixer CI round budget exhausted"
    assert len(await notices(db, supervisor=True)) == 1
