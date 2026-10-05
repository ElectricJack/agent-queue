"""Adversarial lease expiry and publication races on real PostgreSQL and Git."""

from __future__ import annotations

import asyncio
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.database.tables import integration_branch_owners as owners
from src.database.tables import tasks
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError, GitManager
from src.integration.lock import BranchLock, RemoteMoved
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus

pytestmark = pytest.mark.asyncio
TARGET = BranchKey(repository_id="repo", branch="aq/epic")
BINDING = GitHubRepositoryBinding(repository_id=1, full_name="test/repo")
OLD, NEW = "a" * 40, "b" * 40


@pytest.fixture
async def env(reuse_database):
    db = await reuse_database("ref-lease")
    await db.create_project(Project(id="p", name="p"))
    env = SimpleNamespace(db=db, now=1000.0)
    env.lock = BranchLock(db, clock=lambda: env.now)
    return env


async def push(lock, grant, transport):
    return await lock.fenced_push(
        grant,
        git=transport,
        checkout_path="/retained",
        repository=BINDING,
        tip_oid=NEW,
        expected_old_oid=OLD,
    )


async def test_dead_worker_expiry_frees_ref_without_handoff(env):
    old = await env.lock.acquire(TARGET, "dead", ttl_seconds=10)
    async with env.db.immediate() as conn:
        await conn.execute(
            update(owners).values(
                handoff_state="handoff_pending",
                session_id="dead-session",
                workspace_id="reused-slot",
            )
        )
    with pytest.raises(BranchBusy):
        await env.lock.acquire(TARGET, "new")
    env.now += 10
    new = await env.lock.acquire(TARGET, "new")
    assert new.token == old.token + 1
    assert not await env.lock.release(old)
    with pytest.raises(StaleFence):
        await env.lock.renew(old)
    assert (await env.lock.get(TARGET)).holder == "new"


async def test_expiry_cannot_be_renewed_and_same_holder_gets_new_fence(env):
    old = await env.lock.acquire(TARGET, "worker", ttl_seconds=10)
    env.now += 10
    with pytest.raises(StaleFence, match="expired"):
        await env.lock.renew(old)
    new = await env.lock.acquire(TARGET, "worker")
    assert new.token == old.token + 1
    with pytest.raises(StaleFence):
        await env.lock.renew(old)


async def test_acquire_replay_does_not_extend_but_heartbeat_does(env):
    first = await env.lock.acquire(TARGET, "worker", ttl_seconds=10)
    env.now += 5
    assert await env.lock.acquire(TARGET, "worker") == first
    assert (await env.lock.get(TARGET)).expires_at == 1010
    assert await env.lock.renew(first, ttl_seconds=20) == 1025
    assert await env.lock.renew(first, ttl_seconds=1) == 1025


async def test_release_keeps_monotonic_fence_and_is_identity_bound(env):
    first = await env.lock.acquire(TARGET, "worker")
    wrong = first.model_copy(update={"owner_id": "other"})
    assert not await env.lock.release(wrong)
    assert await env.lock.release(first)
    assert not await env.lock.release(first)
    released = await env.lock.get(TARGET)
    assert released.holder is None and released.fence == first.token
    second = await env.lock.acquire(TARGET, "worker")
    assert second.token == first.token + 1


async def test_simultaneous_first_acquire_and_aliases_have_one_winner(env):
    alias = TARGET.model_copy(update={"branch": "refs/heads/aq/epic"})
    results = await asyncio.gather(
        env.lock.acquire(TARGET, "a"), env.lock.acquire(alias, "b"), return_exceptions=True
    )
    assert sum(isinstance(result, Fence) for result in results) == 1
    assert sum(isinstance(result, BranchBusy) for result in results) == 1
    assert await env.lock.get(TARGET) == await env.lock.get(alias)


async def test_different_repositories_and_refs_do_not_block_each_other(env):
    first = await env.lock.acquire(TARGET, "a")
    async with env.lock.exclusion(first):
        other_repo = TARGET.model_copy(update={"repository_id": "other"})
        other_ref = TARGET.model_copy(update={"branch": "aq/other"})
        await asyncio.wait_for(env.lock.acquire(other_repo, "b"), 2)
        await asyncio.wait_for(env.lock.acquire(other_ref, "c"), 2)


@pytest.mark.parametrize("wrong", ["non_holder", "stale_fence", "expired", "released"])
async def test_managed_push_rejects_bad_authority_before_transport(env, wrong):
    first = await env.lock.acquire(TARGET, "worker", ttl_seconds=10)
    grant = first
    if wrong == "non_holder":
        grant = first.model_copy(update={"owner_id": "other"})
    elif wrong == "stale_fence":
        grant = first.model_copy(update={"token": first.token - 1})
    elif wrong == "expired":
        env.now += 10
    else:
        await env.lock.release(first)
    transport = SimpleNamespace(apush_repository_oid=AsyncMock(return_value=NEW))
    with pytest.raises(StaleFence):
        await push(env.lock, grant, transport)
    transport.apush_repository_oid.assert_not_awaited()


async def test_publish_and_expired_reacquisition_share_critical_section(env):
    first = await env.lock.acquire(TARGET, "worker", ttl_seconds=10)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def transport(*args, **kwargs):
        assert kwargs["expected_old_oid"] == OLD
        assert kwargs["branch"] == TARGET.branch
        assert kwargs["authority_deadline"] > asyncio.get_running_loop().time()
        entered.set()
        await finish.wait()
        return NEW

    publication = asyncio.create_task(
        push(env.lock, first, SimpleNamespace(apush_repository_oid=transport))
    )
    await asyncio.wait_for(entered.wait(), 2)
    env.now += 10
    takeover = asyncio.create_task(env.lock.acquire(TARGET, "new"))
    # A second connection waits on the same advisory/row lock through the push.
    await asyncio.sleep(0.05)
    assert not takeover.done()
    finish.set()
    assert await publication == NEW
    second = await asyncio.wait_for(takeover, 2)
    assert second.token == first.token + 1
    with pytest.raises(StaleFence):
        await push(env.lock, first, SimpleNamespace(apush_repository_oid=AsyncMock()))


async def test_reacquisition_winning_before_push_rejects_old_fence(env):
    first = await env.lock.acquire(TARGET, "worker", ttl_seconds=10)
    env.now += 10
    await env.lock.acquire(TARGET, "new")
    transport = SimpleNamespace(apush_repository_oid=AsyncMock())
    with pytest.raises(StaleFence):
        await push(env.lock, first, transport)
    transport.apush_repository_oid.assert_not_awaited()


async def test_publication_timeout_releases_exclusion_and_cancels_transport(env):
    lock = env.lock
    first = await lock.acquire(TARGET, "worker", ttl_seconds=0.15)
    stopped = asyncio.Event()

    async def hanging(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(TimeoutError):
        await push(lock, first, SimpleNamespace(apush_repository_oid=hanging))
    assert stopped.is_set()
    env.now += 1
    second = await asyncio.wait_for(lock.acquire(TARGET, "new"), 2)
    assert second.token > first.token


async def test_legacy_shadow_owner_remains_unchanged_until_explicit_adoption(env):
    legacy = BranchOwnership(env.db, clock=lambda: env.now)
    original = await legacy.acquire(TARGET, "old", "worker")
    assert await env.lock.get(TARGET) is None
    with pytest.raises(BranchBusy):
        await legacy.acquire(TARGET, "new", "worker")
    adopted = await env.lock.acquire(TARGET, "new")
    assert adopted.token > original.token
    with pytest.raises(StaleFence):
        await legacy.assert_current(original)
    await legacy.assert_current(adopted)
    async with env.db.immediate() as conn:
        row = (await conn.execute(select(owners))).mappings().one()
    assert row["owner_id"] == row["holder"] == "new"
    assert row["fence_token"] == row["fence"] == adopted.token


async def test_managed_attachment_transfer_and_expiry_ignore_workspace_proofs(env):
    first = await env.lock.acquire(TARGET, "worker")
    bridge = BranchOwnership(env.db, clock=lambda: env.now)
    await bridge.attach(first, "session", "no-workspace")
    async with env.db.immediate() as conn:
        await conn.execute(update(owners).values(handoff_state="handoff_pending"))
    await bridge.assert_current(first)
    async with bridge.mutation_exclusion(first, state="attached"):
        pass
    second = await bridge.transfer(first, "collector", "collector")
    assert second.token == first.token + 1
    env.now += 480
    with pytest.raises(StaleFence):
        await bridge.assert_current(second)


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True])
async def test_non_finite_or_non_positive_leases_refused(env, ttl):
    with pytest.raises(ValueError):
        await env.lock.acquire(TARGET, "worker", ttl_seconds=ttl)


def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


async def test_external_push_is_remote_movement_and_requires_rebuild(env, tmp_path):
    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "clone", str(remote), str(checkout))
    git(checkout, "config", "user.name", "Lease test")
    git(checkout, "config", "user.email", "lease@example.test")
    git(checkout, "commit", "--allow-empty", "-m", "old")
    old = git(checkout, "rev-parse", "HEAD")
    git(checkout, "push", "origin", f"HEAD:refs/heads/{TARGET.branch}")
    git(checkout, "commit", "--allow-empty", "-m", "candidate")
    candidate = git(checkout, "rev-parse", "HEAD")
    git(checkout, "commit", "--allow-empty", "-m", "external")
    external = git(checkout, "rev-parse", "HEAD")
    # Raw credentials are outside managed DB fencing. This push succeeds.
    git(checkout, "push", "origin", f"HEAD:refs/heads/{TARGET.branch}")

    class LocalTransport(GitManager):
        async def apush_repository_oid(
            self, path, *, repository, tip_oid, branch, expected_old_oid, authority_deadline=None
        ):
            return await self._apush_oid(path, tip_oid, branch, expected_old_oid=expected_old_oid)

        async def als_remote_ref(self, path, branch, **kwargs):
            return await super().als_remote_ref(path, branch)

    fence = await env.lock.acquire(TARGET, "managed")
    with pytest.raises(RemoteMoved) as moved:
        await env.lock.fenced_push(
            fence,
            git=LocalTransport(),
            checkout_path=str(checkout),
            repository=BINDING,
            tip_oid=candidate,
            expected_old_oid=old,
        )
    assert moved.value.observed_oid == external
    assert git(remote, "rev-parse", f"refs/heads/{TARGET.branch}") == external
    assert (await env.lock.get(TARGET)).holder == "managed"
    # Rebuild captures the new base; the same valid holder can publish it.
    git(checkout, "commit", "--allow-empty", "-m", "rebuild")
    rebuilt = git(checkout, "rev-parse", "HEAD")
    assert (
        await env.lock.fenced_push(
            fence,
            git=LocalTransport(),
            checkout_path=str(checkout),
            repository=BINDING,
            tip_oid=rebuilt,
            expected_old_oid=external,
        )
        == rebuilt
    )
    assert git(remote, "rev-parse", f"refs/heads/{TARGET.branch}") == rebuilt


async def test_lost_push_response_is_read_back_under_same_exclusion(env):
    from src.git.manager import RemoteRefResult, RemoteRefState

    fence = await env.lock.acquire(TARGET, "managed")
    transport = SimpleNamespace(
        apush_repository_oid=AsyncMock(side_effect=GitError("response lost")),
        als_remote_ref=AsyncMock(return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=NEW)),
    )
    assert await push(env.lock, fence, transport) == NEW
    transport.als_remote_ref.assert_awaited_once()


async def test_session_activity_renews_only_bound_current_unexpired_claim(env):
    await env.db.create_agent(
        Agent(id="agent", name="agent", profile_id="worker", state=AgentState.BUSY)
    )
    await env.db.create_task(
        Task(
            id="task",
            project_id="p",
            title="task",
            description="",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="agent",
            claim_epoch=1,
        )
    )
    # Task creation deliberately starts epoch zero; simulate claim admission.
    async with env.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "task").values(claim_epoch=1))
    await env.db.create_session(
        SessionRecord(
            id="s",
            task_id="task",
            project_id="p",
            agent_id="agent",
            profile_id="worker",
            harness="codex",
            name="session",
            provider="tmux",
            lifecycle="pool",
            work_dir="/slot",
            epoch="epoch",
            instance_token="instance",
            state="running",
            started_at=time.time(),
            last_claim_epoch=1,
        )
    )
    lock = BranchLock(env.db)
    fence = await lock.acquire(TARGET, "task", ttl_seconds=5)
    await BranchOwnership(env.db).attach(fence, "s", "no-workspace")
    await env.db.touch_session_activity("s", time.time())
    renewed = (await lock.get(TARGET)).expires_at
    assert renewed > time.time() + 470
    # Delayed liveness and displaced epochs cannot extend this lease.
    await env.db.touch_session_activity("s", 1.0)
    assert (await lock.get(TARGET)).expires_at == renewed
    from src.database.tables import sessions

    async with env.db.immediate() as conn:
        await conn.execute(update(sessions).where(sessions.c.id == "s").values(last_claim_epoch=0))
    await env.db.touch_session_activity("s", time.time() + 1)
    assert (await lock.get(TARGET)).expires_at == renewed
    async with env.db.immediate() as conn:
        await conn.execute(update(sessions).where(sessions.c.id == "s").values(last_claim_epoch=1))
        await conn.execute(update(owners).values(expires_at=time.time() - 1))
    await env.db.touch_session_activity("s", time.time() + 2)
    with pytest.raises(StaleFence):
        await lock.renew(fence)


async def test_claim_release_releases_managed_authority_without_workspace_proof(env):
    from src.integration.lock import release_session_leases_on

    fence = await env.lock.acquire(TARGET, "task")
    await BranchOwnership(env.db, clock=lambda: env.now).attach(fence, "s", "no-workspace")
    async with env.db.immediate() as conn:
        await release_session_leases_on(env.db, conn, "s", "task")
    assert (await env.lock.get(TARGET)).holder is None
    next_grant = await env.lock.acquire(TARGET, "other")
    async with env.db.immediate() as conn:
        await release_session_leases_on(env.db, conn, "s", "task")
    assert (await env.lock.get(TARGET)).grant() == next_grant
