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
            db, ws_id="ws-pref", project_id="p1", path="/repo-pref",
            kind_id="project-repo",
        )
        task = await _mktask(db, preferred="ws-pref")
        reqs = await effective_requirements(db, task)
        pr = next(r for r in reqs if r.kind_id == "project-repo")
        assert pr.preferred_workspace_id == "ws-pref"

    async def test_explicit_requirements_wins_over_synthesis(self, db):
        await _add_kind(
            db, kind_id="game-repo",
            writable=True, lockable=True, is_git_repo=True,
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
            db, kind_id="alpha",
            writable=True, lockable=True, is_git_repo=True,
            default_lock_mode="exclusive",
        )
        await _add_kind(
            db, kind_id="zeta",
            writable=True, lockable=True, is_git_repo=True,
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
            db, kind_id="alpha",
            writable=True, lockable=True, is_git_repo=True,
            default_lock_mode="exclusive",
        )
        await _add_kind(
            db, kind_id="beta",
            writable=True, lockable=True, is_git_repo=True,
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
    async def test_back_compat_single_workspace(self, db):
        """Spec §14 #7: existing tasks with no requirements get a project-repo
        attachment via synthesis."""
        await _add_workspace(
            db, ws_id="ws-pr", project_id="p1", path="/repo", kind_id="project-repo",
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
            db, ws_id="ws-pr", project_id="p1", path="/repo", kind_id="project-repo",
        )
        await _add_kind(
            db, kind_id="package-foo",
            writable=True, lockable=True, is_git_repo=True,
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
            db, ws_id="ws-pr", project_id="p1", path="/repo", kind_id="project-repo",
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
            db, ws_id="ws-pr", project_id="p1", path="/repo", kind_id="project-repo",
        )
        # Vault workspace already provisioned by migration via init.
        task = await _mktask(db)
        agent = await _mkagent(db)

        att_set = await acquire_for_task(db, task, agent.id)
        assert att_set.first_of_kind("vault") is not None
        assert att_set.primary_path == "/repo"

    async def test_unknown_kind_raises(self, db):
        await _add_workspace(
            db, ws_id="ws-pr", project_id="p1", path="/repo", kind_id="project-repo",
        )
        task = await _mktask(db)
        agent = await _mkagent(db)
        # Reference a kind that doesn't exist.
        await db.add_task_workspace_requirements("t1", [("nonexistent", None)])

        with pytest.raises(AcquisitionFailed) as exc:
            await acquire_for_task(db, task, agent.id)
        assert exc.value.kind_id == "nonexistent"
