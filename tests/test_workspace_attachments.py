"""effective_requirements + acquire_for_task. See spec §6 + §14."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    Project,
    RepoSourceType,
    SYSTEM_KIND_SCOPE,
    Task,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator.workspace_attachments import (
    AcquisitionFailed,
    acquire_for_task,
    effective_requirements,
)
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="test"))
    # In production, the workspaces-v2 migration provisions a vault workspace
    # for every existing project.  Mirror that here so auto-attach behaves
    # like a real install.
    vault_ws = Workspace(
        id="ws-vault-p1",
        project_id="p1",
        workspace_path="/tmp/vault-p1",
        source_type=RepoSourceType.LINK,
        name="vault",
        kind_id="vault",
    )
    await database.create_workspace(vault_ws)
    yield database
    await database.close()


def _now() -> float:
    return time.time()


async def _mktask(db, *, task_id="t1", project_id="p1", preferred=None) -> Task:
    task = Task(
        id=task_id,
        project_id=project_id,
        title=task_id,
        description="",
        preferred_workspace_id=preferred,
        created_at=_now(),
        updated_at=_now(),
    )
    await db.create_task(task)
    return task


async def _mkagent(db, *, agent_id="a1") -> Agent:
    # Profile required so AgentReconciler doesn't trip; minimal one will do.
    profile = AgentProfile(
        id="test-profile",
        name="test",
        model="claude-haiku-4-5-20251001",
        permission_mode="bypassPermissions",
    )
    await db.upsert_profile(profile)
    agent = Agent(id=agent_id, name=agent_id, profile_id="test-profile")
    await db.create_agent(agent)
    return agent


async def _add_workspace(db, *, ws_id, project_id, path, kind_id, name=None):
    ws = Workspace(
        id=ws_id,
        project_id=project_id,
        workspace_path=path,
        source_type=RepoSourceType.LINK,
        name=name,
        kind_id=kind_id,
    )
    await db.create_workspace(ws)
    return ws


async def _add_kind(db, *, project_id=SYSTEM_KIND_SCOPE, kind_id, **flags):
    kind = WorkspaceKind(
        project_id=project_id,
        id=kind_id,
        created_at=_now(),
        updated_at=_now(),
        **flags,
    )
    await db.upsert_workspace_kind(kind)
    return kind


# ───────────────────────────────────────────────── effective_requirements ──


class TestEffectiveRequirements:
    async def test_synthesizes_project_repo_when_no_explicit(self, db):
        # Migration seeds project-repo system kind, so resolve will succeed.
        task = await _mktask(db)
        reqs = await effective_requirements(db, task)
        # vault auto-attaches; expect [project-repo, vault]
        assert [r.kind_id for r in reqs] == ["project-repo", "vault"]
        # Synthesized project-repo carries preferred_workspace_id (None here).
        pr = next(r for r in reqs if r.kind_id == "project-repo")
        assert pr.preferred_workspace_id is None

    async def test_carries_preferred_workspace_id(self, db):
        # Create a real workspace so the FK on tasks.preferred_workspace_id
        # is satisfied.
        await _add_workspace(
            db,
            ws_id="ws-pref",
            project_id="p1",
            path="/repo-pref",
            kind_id="project-repo",
        )
        task = await _mktask(db, preferred="ws-pref")
        reqs = await effective_requirements(db, task)
        pr = next(r for r in reqs if r.kind_id == "project-repo")
        assert pr.preferred_workspace_id == "ws-pref"

    async def test_explicit_requirements_wins_over_synthesis(self, db):
        await _add_kind(
            db,
            kind_id="game-repo",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )
        task = await _mktask(db)
        await db.add_task_workspace_requirements("t1", [("game-repo", None)])
        reqs = await effective_requirements(db, task)
        kind_ids = [r.kind_id for r in reqs]
        # No synthesized project-repo when explicit rows exist; vault still auto.
        assert "project-repo" not in kind_ids
        assert "game-repo" in kind_ids
        assert "vault" in kind_ids
        # preferred_workspace_id is NOT applied to explicit requirements.
        gr = next(r for r in reqs if r.kind_id == "game-repo")
        assert gr.preferred_workspace_id is None

    async def test_auto_attach_appended_only_if_not_explicit(self, db):
        task = await _mktask(db)
        await db.add_task_workspace_requirements("t1", [("vault", None)])
        reqs = await effective_requirements(db, task)
        # Should appear once, not twice.
        vault_count = sum(1 for r in reqs if r.kind_id == "vault")
        assert vault_count == 1

    async def test_canonical_lock_order(self, db):
        await _add_kind(
            db,
            kind_id="alpha",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )
        await _add_kind(
            db,
            kind_id="zeta",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )
        task = await _mktask(db)
        # Insert in non-canonical order.
        await db.add_task_workspace_requirements("t1", [("zeta", None), ("alpha", None)])
        reqs = await effective_requirements(db, task)
        kind_ids = [r.kind_id for r in reqs]
        # Sorted by (kind_id, position); auto-attached vault has high position
        # so it sorts within its kind_id alphabetically.
        assert kind_ids.index("alpha") < kind_ids.index("zeta")

    async def test_input_order_does_not_affect_canonical_order(self, db):
        """Spec §6.3: canonical order is independent of input list order."""
        await _add_kind(
            db,
            kind_id="alpha",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )
        await _add_kind(
            db,
            kind_id="beta",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )
        task1 = await _mktask(db, task_id="t1")
        task2 = await _mktask(db, task_id="t2")
        await db.add_task_workspace_requirements("t1", [("alpha", None), ("beta", None)])
        await db.add_task_workspace_requirements("t2", [("beta", None), ("alpha", None)])
        reqs1 = await effective_requirements(db, task1)
        reqs2 = await effective_requirements(db, task2)
        assert [r.kind_id for r in reqs1] == [r.kind_id for r in reqs2]


# ────────────────────────────────────────────────────── acquire_for_task ──


class TestAcquireForTask:
    async def test_concurrent_opposite_requirement_orders_complete_without_partial_locks(self, db):
        """Canonical ordering means a two-kind race has one complete winner.

        ORC-1 follow-up: the winner also receives the auto-attached vault
        workspace, so the expected lockable set is asserted over lockable
        attachments only, mirroring the PostgreSQL twin below.
        """
        for kind in ("alpha", "beta"):
            await _add_kind(
                db,
                kind_id=kind,
                writable=True,
                lockable=True,
                is_git_repo=True,
                default_lock_mode="exclusive",
            )
            await _add_workspace(
                db, ws_id=f"ws-{kind}", project_id="p1", path=f"/{kind}", kind_id=kind
            )
        first, second = await _mktask(db, task_id="first"), await _mktask(db, task_id="second")
        await db.add_task_workspace_requirements(first.id, [("alpha", None), ("beta", None)])
        await db.add_task_workspace_requirements(second.id, [("beta", None), ("alpha", None)])
        first_agent, second_agent = (
            await _mkagent(db, agent_id="a-first"),
            await _mkagent(db, agent_id="a-second"),
        )
        results = await asyncio.wait_for(
            asyncio.gather(
                acquire_for_task(db, first, first_agent.id),
                acquire_for_task(db, second, second_agent.id),
                return_exceptions=True,
            ),
            timeout=10,
        )
        winners = [result for result in results if not isinstance(result, Exception)]
        losers = [result for result in results if isinstance(result, AcquisitionFailed)]
        assert len(winners) == 1 and len(losers) == 1
        winner_task = first.id if results[0] is winners[0] else second.id
        assert {
            attachment.workspace.id
            for attachment in winners[0].attachments
            if attachment.lockable
        } == {"ws-alpha", "ws-beta"}
        for workspace_id in ("ws-alpha", "ws-beta"):
            assert (await db.get_workspace(workspace_id)).locked_by_task_id == winner_task

    async def test_second_task_acquires_both_kinds_after_first_releases(self, db):
        for kind in ("alpha", "beta"):
            await _add_kind(
                db,
                kind_id=kind,
                writable=True,
                lockable=True,
                is_git_repo=True,
                default_lock_mode="exclusive",
            )
            await _add_workspace(
                db, ws_id=f"ws-{kind}", project_id="p1", path=f"/{kind}", kind_id=kind
            )
        first, second = await _mktask(db, task_id="first"), await _mktask(db, task_id="second")
        for task in (first, second):
            await db.add_task_workspace_requirements(task.id, [("alpha", None), ("beta", None)])
        await acquire_for_task(db, first, (await _mkagent(db, agent_id="a-first")).id)
        assert await db.release_workspaces_for_task(first.id) == 2
        attachments = await acquire_for_task(
            db, second, (await _mkagent(db, agent_id="a-second")).id
        )
        assert {
            attachment.workspace.locked_by_task_id
            for attachment in attachments.attachments
            if attachment.lockable
        } == {second.id}

    async def test_two_positions_of_same_kind_receive_distinct_instances(self, db):
        await _add_kind(
            db,
            kind_id="package",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )
        for number in (1, 2):
            await _add_workspace(
                db,
                ws_id=f"package-{number}",
                project_id="p1",
                path=f"/package-{number}",
                kind_id="package",
            )
        task, agent = await _mktask(db), await _mkagent(db)
        await db.add_task_workspace_requirements(task.id, [("package", "one"), ("package", "two")])
        attachments = (await acquire_for_task(db, task, agent.id)).by_kind("package")
        assert [attachment.alias for attachment in attachments] == ["one", "two"]
        assert {attachment.workspace.id for attachment in attachments} == {"package-1", "package-2"}

    async def test_preferred_workspace_falls_back_to_another_unlocked_same_kind_instance(self, db):
        await _add_workspace(
            db, ws_id="preferred", project_id="p1", path="/preferred", kind_id="project-repo"
        )
        await _add_workspace(
            db, ws_id="fallback", project_id="p1", path="/fallback", kind_id="project-repo"
        )
        blocked, task = (
            await _mktask(db, task_id="blocked"),
            await _mktask(db, task_id="wanted", preferred="preferred"),
        )
        other_agent = await _mkagent(db, agent_id="other-agent")
        await db.acquire_workspace(
            "p1", other_agent.id, blocked.id, preferred_workspace_id="preferred"
        )
        attachment = (await acquire_for_task(db, task, (await _mkagent(db)).id)).first_of_kind(
            "project-repo"
        )
        assert attachment.workspace.id == "fallback"
        assert (await db.get_workspace("preferred")).locked_by_task_id == blocked.id

    async def test_mixed_requirements_lock_only_the_lockable_kinds(self, db):
        """Non-lockable kinds attach without a lock; lockable ones are locked.

        ``acquire_for_task`` no longer has a read-only mode: skipping the
        lock is what used to hand read-only agents the base checkout.
        """
        await _add_workspace(
            db, ws_id="repo", project_id="p1", path="/repo", kind_id="project-repo"
        )
        await _add_kind(db, kind_id="reference", writable=False, lockable=False, is_git_repo=False)
        await _add_workspace(
            db, ws_id="reference", project_id="p1", path="/reference", kind_id="reference"
        )
        task, agent = await _mktask(db), await _mkagent(db)
        await db.add_task_workspace_requirements(
            task.id, [("project-repo", None), ("reference", None)]
        )
        attachments = await acquire_for_task(db, task, agent.id)
        assert {attachment.kind_id for attachment in attachments.attachments} == {
            "project-repo",
            "reference",
            "vault",
        }
        locked = {
            attachment.kind_id: (await db.get_workspace(attachment.workspace.id)).locked_by_task_id
            for attachment in attachments.attachments
        }
        assert locked["project-repo"] == task.id
        assert locked["reference"] is None
        assert locked["vault"] is None

    async def test_back_compat_single_workspace(self, db):
        """Spec §14 #7: existing tasks with no requirements get a project-repo
        attachment via synthesis."""
        await _add_workspace(
            db,
            ws_id="ws-pr",
            project_id="p1",
            path="/repo",
            kind_id="project-repo",
        )
        task = await _mktask(db)
        agent = await _mkagent(db)

        att_set = await acquire_for_task(db, task, agent.id)
        kind_ids = [a.kind_id for a in att_set.attachments]
        assert kind_ids == ["project-repo", "vault"]
        # project-repo is locked
        pr = att_set.first_of_kind("project-repo")
        assert pr.workspace.locked_by_task_id == task.id
        # vault is not (lockable=False)
        v = att_set.first_of_kind("vault")
        assert v.workspace.locked_by_task_id is None

    async def test_partial_failure_rolls_back_acquired_locks(self, db):
        """Spec §14 #3: task wants [A, B], B unavailable, A is released."""
        await _add_workspace(
            db,
            ws_id="ws-pr",
            project_id="p1",
            path="/repo",
            kind_id="project-repo",
        )
        await _add_kind(
            db,
            kind_id="package-foo",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )
        # No package-foo workspace exists — second acquisition will fail.
        task = await _mktask(db)
        agent = await _mkagent(db)
        await db.add_task_workspace_requirements(
            "t1", [("project-repo", None), ("package-foo", None)]
        )

        with pytest.raises(AcquisitionFailed) as exc:
            await acquire_for_task(db, task, agent.id)
        assert exc.value.kind_id == "package-foo"

        # project-repo lock must be released after rollback.
        pr_ws = await db.get_workspace("ws-pr")
        assert pr_ws.locked_by_task_id is None
        assert pr_ws.locked_by_agent_id is None

    async def test_concurrent_same_kind_one_winner(self, db):
        """Spec §14 #1: two tasks racing for one instance — exactly one wins."""
        await _add_workspace(
            db,
            ws_id="ws-pr",
            project_id="p1",
            path="/repo",
            kind_id="project-repo",
        )
        task1 = await _mktask(db, task_id="t1")
        task2 = await _mktask(db, task_id="t2")
        agent1 = await _mkagent(db, agent_id="a1")
        agent2 = await _mkagent(db, agent_id="a2")

        # Race them.
        results = await asyncio.gather(
            acquire_for_task(db, task1, agent1.id),
            acquire_for_task(db, task2, agent2.id),
            return_exceptions=True,
        )
        succeeded = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, AcquisitionFailed)]
        assert len(succeeded) == 1
        assert len(failed) == 1
        assert failed[0].kind_id == "project-repo"

    async def test_auto_attach_appears_without_being_declared(self, db):
        """Spec §14 #6: vault attaches without declaration."""
        await _add_workspace(
            db,
            ws_id="ws-pr",
            project_id="p1",
            path="/repo",
            kind_id="project-repo",
        )
        # Vault workspace already provisioned by migration via init.
        task = await _mktask(db)
        agent = await _mkagent(db)

        att_set = await acquire_for_task(db, task, agent.id)
        assert att_set.first_of_kind("vault") is not None
        assert att_set.primary_path == "/repo"

    async def test_unknown_kind_raises(self, db):
        await _add_workspace(
            db,
            ws_id="ws-pr",
            project_id="p1",
            path="/repo",
            kind_id="project-repo",
        )
        task = await _mktask(db)
        agent = await _mkagent(db)
        # Reference a kind that doesn't exist.
        await db.add_task_workspace_requirements("t1", [("nonexistent", None)])

        with pytest.raises(AcquisitionFailed) as exc:
            await acquire_for_task(db, task, agent.id)
        assert exc.value.kind_id == "nonexistent"


# ─────────────────────────────────────────────── real-PostgreSQL contracts ──


@pytest.fixture
async def pg_db():
    """The multi-kind acquisition contracts on a real PostgreSQL backend.

    Each per-kind lock is its own transaction, so the interesting failure
    modes — a concurrent second backend, rollback of already-taken locks,
    recovery from a crash between per-kind transactions — only mean
    anything against a server that runs the two acquirers genuinely
    concurrently (the SQLite twin of the race test runs above and relies on
    WAL + busy_timeout serialization instead).
    """
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

    database = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
    await database.initialize()
    await database.reset_for_tests()
    await database.create_project(Project(id="p1", name="test"))
    yield database
    await database.close()


async def _two_kind_world(db):
    """Two lockable kinds with one instance each, two tasks, two agents."""
    for kind in ("alpha", "beta"):
        await _add_kind(
            db,
            kind_id=kind,
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )
        await _add_workspace(
            db, ws_id=f"ws-{kind}", project_id="p1", path=f"/{kind}", kind_id=kind
        )
    first, second = await _mktask(db, task_id="first"), await _mktask(db, task_id="second")
    await db.add_task_workspace_requirements(first.id, [("alpha", None), ("beta", None)])
    await db.add_task_workspace_requirements(second.id, [("beta", None), ("alpha", None)])
    agents = (await _mkagent(db, agent_id="a-first"), await _mkagent(db, agent_id="a-second"))
    return first, second, agents


class TestAcquireForTaskOnPostgres:
    async def test_concurrent_opposite_orders_one_complete_winner_no_partial_locks(self, pg_db):
        """Canonical (kind_id, position) ordering on a genuinely concurrent
        backend: one racer gets *both* kinds, the loser gets neither,
        nothing hangs."""
        first, second, (a1, a2) = await _two_kind_world(pg_db)
        results = await asyncio.wait_for(
            asyncio.gather(
                acquire_for_task(pg_db, first, a1.id),
                acquire_for_task(pg_db, second, a2.id),
                return_exceptions=True,
            ),
            timeout=10,
        )
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, AcquisitionFailed)]
        assert len(winners) == 1 and len(losers) == 1
        winner_task = first.id if results[0] is winners[0] else second.id
        assert {a.workspace.id for a in winners[0].attachments if a.lockable} == {
            "ws-alpha",
            "ws-beta",
        }
        for ws_id in ("ws-alpha", "ws-beta"):
            ws = await pg_db.get_workspace(ws_id)
            assert ws.locked_by_task_id == winner_task  # loser holds nothing

    async def test_partial_failure_rolls_back_acquired_locks(self, pg_db):
        """All-or-nothing on PG: [alpha, missing] releases the alpha lock."""
        await _add_kind(
            pg_db,
            kind_id="alpha",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )
        await _add_workspace(
            pg_db, ws_id="ws-alpha", project_id="p1", path="/alpha", kind_id="alpha"
        )
        await _add_kind(
            pg_db,
            kind_id="package-foo",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
        )  # kind exists, but no instance
        task = await _mktask(pg_db)
        agent = await _mkagent(pg_db)
        await pg_db.add_task_workspace_requirements(
            task.id, [("alpha", None), ("package-foo", None)]
        )
        with pytest.raises(AcquisitionFailed) as exc:
            await acquire_for_task(pg_db, task, agent.id)
        assert exc.value.kind_id == "package-foo"
        ws = await pg_db.get_workspace("ws-alpha")
        assert (ws.locked_by_task_id, ws.locked_by_agent_id) == (None, None)

    async def test_crash_between_kind_transactions_recovers_via_task_release(self, pg_db):
        """Each kind locks in its own transaction, so a crash between them
        leaves the first lock committed — that partial state must be fully
        recoverable by ``release_workspaces_for_task`` (what the orchestrator
        runs when it reaps the dead claim)."""
        first, _second, (a1, _a2) = await _two_kind_world(pg_db)
        # The crashed acquirer took alpha and died before beta: reproduce the
        # exact committed state its first per-kind transaction left behind.
        taken = await pg_db.acquire_one_unlocked(
            project_id="p1",
            kind_id="alpha",
            mode="exclusive",
            locked_by_task_id=first.id,
            locked_by_agent_id=a1.id,
        )
        assert taken is not None and taken.id == "ws-alpha"
        assert (await pg_db.get_workspace("ws-alpha")).locked_by_task_id == first.id

        released = await pg_db.release_workspaces_for_task(first.id)
        assert released == 1
        for ws_id in ("ws-alpha", "ws-beta"):
            ws = await pg_db.get_workspace(ws_id)
            assert (ws.locked_by_task_id, ws.locked_by_agent_id) == (None, None)
        # The world is clean again: a fresh acquirer gets both kinds.
        att = await acquire_for_task(pg_db, first, a1.id)
        assert {a.workspace.id for a in att.attachments if a.lockable} == {
            "ws-alpha",
            "ws-beta",
        }
