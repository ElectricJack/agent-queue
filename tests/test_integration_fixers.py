"""Task-free in-place repair survives replay, budget exhaustion and green."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import pytest
from sqlalchemy import func, select, update

from src.commands.principal import ExecutionPrincipal
from src.database.tables import integration_batches, messages, tasks
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


async def test_ambiguous_launch_is_retained_and_never_retried(db, fixer):
    service, runtime, now = fixer
    runtime.start.side_effect = RuntimeError("process registration succeeded; provider unavailable")
    assert (await allocate(service))["outcome"] == "deferred"
    now[0] += 2000
    await allocate(service)
    runtime.start.assert_awaited_once()
    assert len(await notices(db, supervisor=True)) == 1
    await service.settle_green("batch-fix", HEAD)
    assert (await state(db))["status"] == "green"


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
async def live_fixer(db, tmp_path, monkeypatch):
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
    registry.upsert(Harness(id="codex", command="codex", model_flag="--model"))
    profile = AgentProfile(id="fixer-profile", name="Fixer", harness="codex",
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

    async def fetch(destination, *, repository, oid, destination_ref):
        return await transport._arun(["fetch", str(remote), f"{oid}:{destination_ref}"],
                                     cwd=destination)

    monkeypatch.setattr(transport, "afetch_repository_oid", fetch)
    service = BatchFixerService(db, runtime)
    result = await service.allocate("real", target_ref=candidate_ref("real"), head_sha=source,
                                    authorize=AsyncMock(return_value=True))
    assert result["outcome"] == "filed", result
    row = await db.get_session(result["session_id"])
    return service, runtime, orch, row, base, source, lane


def principal_for(row):
    from src.commands.principal import PrincipalKind
    from src.profiles.capabilities import DENY_ALL

    return ExecutionPrincipal(PrincipalKind.SESSION, DENY_ALL, session_id=row.id,
                              session_instance_token=row.instance_token, project_id=row.project_id)


async def test_registered_named_launch_has_no_claim_and_preserves_checkout(db, live_fixer):
    service, runtime, orch, row, _, source, _ = live_fixer
    assert row.lifecycle == "named" and row.task_id is None and row.state == "running"
    provider = orch.session_providers.create("fake")
    spec = provider.sessions[row.name].spec
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
    current = (await db.get_integration_batch(successor.id))["fixer_state"]
    assert current["red_heads"] == [NEXT]
    assert current["starting_sha"] == NEXT
    assert current["deadline"] > now[0]


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
