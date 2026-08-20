"""Mode-aware acquisition and capacity counting.

Worktree-execution implementation spec §6.3 (``acquire_one_unlocked``
candidate filter) and §6.7 (``count_available_workspaces`` as capacity).
Companion to ``tests/test_workspace_attachments.py``, which owns the
mode-blind behavior these must not disturb.

The invariant under test throughout: with ``worktrees.enabled`` false —
the shipped default — every one of these rows behaves exactly as it does
today.  The flag is the rollout gate (§5); the markdown ``mode`` is the
steady-state truth.
"""

from __future__ import annotations

import time

import pytest

from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    KIND_MODE_EXCLUSIVE_CLONE,
    KIND_MODE_WORKTREE,
    Project,
    RepoSourceType,
    SYSTEM_KIND_SCOPE,
    Task,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator.workspace_attachments import AcquisitionFailed, acquire_for_task


def _now() -> float:
    return time.time()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="test"))
    # Mirror a real install: the workspaces-v2 migration provisions a vault
    # workspace per project, and vault auto-attaches to every task.
    await database.create_workspace(
        Workspace(
            id="ws-vault-p1",
            project_id="p1",
            workspace_path="/tmp/vault-p1",
            source_type=RepoSourceType.LINK,
            name="vault",
            kind_id="vault",
        )
    )
    yield database
    await database.close()


async def _mktask(db, *, task_id="t1", preferred=None) -> Task:
    task = Task(
        id=task_id,
        project_id="p1",
        title=task_id,
        description="",
        preferred_workspace_id=preferred,
        created_at=_now(),
        updated_at=_now(),
    )
    await db.create_task(task)
    return task


async def _mkagent(db, *, agent_id="a1") -> Agent:
    await db.upsert_profile(
        AgentProfile(
            id="test-profile",
            name="test",
            model="claude-haiku-4-5-20251001",
            permission_mode="bypassPermissions",
        )
    )
    agent = Agent(id=agent_id, name=agent_id, profile_id="test-profile")
    await db.create_agent(agent)
    return agent


async def _add_kind(db, *, kind_id, **flags):
    await db.upsert_workspace_kind(
        WorkspaceKind(
            project_id=SYSTEM_KIND_SCOPE,
            id=kind_id,
            created_at=_now(),
            updated_at=_now(),
            **flags,
        )
    )


async def _add_ws(db, *, ws_id, path, kind_id, slot_index=None, base_id=None):
    ws = Workspace(
        id=ws_id,
        project_id="p1",
        workspace_path=path,
        source_type=(
            RepoSourceType.WORKTREE if slot_index is not None else RepoSourceType.CLONE
        ),
        kind_id=kind_id,
        slot_index=slot_index,
        base_workspace_id=base_id,
    )
    await db.create_workspace(ws)
    return ws


async def _worktree_project(db, slots=2):
    """A worktree-mode project-repo kind: one base plus *slots* slot rows."""
    await _add_kind(
        db,
        kind_id="project-repo",
        is_git_repo=True,
        lockable=True,
        mode=KIND_MODE_WORKTREE,
        default_lock_mode="exclusive",
    )
    await _add_ws(db, ws_id="ws-base", path="/repo", kind_id="project-repo")
    for i in range(slots):
        await _add_ws(
            db,
            ws_id=f"ws-slot-{i}",
            path=f"/repo/.aq/worktrees/slot-{i}",
            kind_id="project-repo",
            slot_index=i,
            base_id="ws-base",
        )


def _primary(att):
    return att.first_of_kind("project-repo").workspace


# ───────────────────────────── candidate filter (§6.3) ───────────────────


class TestWorktreeModeAcquisition:
    async def test_slot_is_acquired_not_the_base(self, db):
        """The base is for fetch and worktree bookkeeping, never an agent cwd."""
        await _worktree_project(db)
        att = await acquire_for_task(
            db, await _mktask(db), (await _mkagent(db)).id, worktrees_enabled=True
        )
        ws = _primary(att)
        assert ws.id == "ws-slot-0", "lowest free slot index first"
        assert ws.is_slot

    async def test_base_with_no_slots_is_not_acquirable(self, db):
        """A base with no slots yet has nothing to hand out.

        ``_prepare_workspace`` provisions slots *before* acquiring; if that
        failed, acquisition must fail cleanly rather than leak the base to an
        agent, which would put the whole repo under one task's control.
        """
        await _add_kind(
            db, kind_id="project-repo", is_git_repo=True, lockable=True,
            mode=KIND_MODE_WORKTREE,
        )
        await _add_ws(db, ws_id="ws-base", path="/repo", kind_id="project-repo")

        with pytest.raises(AcquisitionFailed) as exc:
            await acquire_for_task(
                db, await _mktask(db), (await _mkagent(db)).id, worktrees_enabled=True
            )
        assert exc.value.kind_id == "project-repo"

    async def test_flag_off_restores_clone_behavior(self, db):
        """The rollout gate (§5): same rows, today's behavior."""
        await _worktree_project(db)
        att = await acquire_for_task(
            db, await _mktask(db), (await _mkagent(db)).id, worktrees_enabled=False
        )
        assert _primary(att).id == "ws-base"

    async def test_two_tasks_take_two_slots(self, db):
        await _worktree_project(db)
        got = set()
        for i in (1, 2):
            att = await acquire_for_task(
                db,
                await _mktask(db, task_id=f"t{i}"),
                (await _mkagent(db, agent_id=f"a{i}")).id,
                worktrees_enabled=True,
            )
            got.add(_primary(att).id)
        assert got == {"ws-slot-0", "ws-slot-1"}

    async def test_exhausted_slots_fail_cleanly(self, db):
        await _worktree_project(db)
        for i in (1, 2):
            await acquire_for_task(
                db,
                await _mktask(db, task_id=f"t{i}"),
                (await _mkagent(db, agent_id=f"a{i}")).id,
                worktrees_enabled=True,
            )
        with pytest.raises(AcquisitionFailed):
            await acquire_for_task(
                db,
                await _mktask(db, task_id="t3"),
                (await _mkagent(db, agent_id="a3")).id,
                worktrees_enabled=True,
            )

    async def test_preferred_workspace_cannot_smuggle_in_the_base(self, db):
        """``preferred_workspace_id`` is a hint, not a mode override."""
        await _worktree_project(db)
        att = await acquire_for_task(
            db,
            await _mktask(db, preferred="ws-base"),
            (await _mkagent(db)).id,
            worktrees_enabled=True,
        )
        assert _primary(att).id == "ws-slot-0"

    async def test_multi_kind_all_or_nothing_still_rolls_back(self, db):
        """A taken slot must be released when a later kind cannot be had."""
        await _worktree_project(db)
        await _add_kind(
            db, kind_id="package-foo", is_git_repo=True, lockable=True,
            mode=KIND_MODE_EXCLUSIVE_CLONE,
        )
        task = await _mktask(db)
        agent = await _mkagent(db)
        # package-foo resolves as a kind but has no workspace for this project.
        await db.add_task_workspace_requirements(
            task.id, [("project-repo", None), ("package-foo", None)]
        )

        with pytest.raises(AcquisitionFailed) as exc:
            await acquire_for_task(db, task, agent.id, worktrees_enabled=True)
        assert exc.value.kind_id == "package-foo"
        assert (await db.get_workspace("ws-slot-0")).locked_by_agent_id is None

    async def test_vault_auto_attach_is_unaffected(self, db):
        """Non-git kinds ignore ``mode`` entirely (§6.3)."""
        await _worktree_project(db)
        att = await acquire_for_task(
            db, await _mktask(db), (await _mkagent(db)).id, worktrees_enabled=True
        )
        assert att.first_of_kind("vault") is not None


# ──────────────────────── capacity, not inventory (§6.7) ─────────────────


class TestWorktreeCapacityCount:
    async def test_base_with_no_slots_still_reports_capacity(self, db):
        """Without this the reconciler never creates the first agent.

        A fresh worktree-mode project has one base and zero slots: no
        unlocked git row at all.  Counting inventory would report zero and
        the project would never start, because slots are only created once a
        task is being dispatched.
        """
        await _add_kind(
            db, kind_id="project-repo", is_git_repo=True, lockable=True,
            mode=KIND_MODE_WORKTREE,
        )
        await _add_ws(db, ws_id="ws-base", path="/repo", kind_id="project-repo")

        assert await db.count_available_workspaces("p1") == 2  # base + vault
        assert await db.count_available_workspaces("p1", worktree_slot_cap=3) == 4

    async def test_locked_slots_reduce_capacity(self, db):
        await _worktree_project(db)
        await acquire_for_task(
            db, await _mktask(db), (await _mkagent(db)).id, worktrees_enabled=True
        )
        # cap 2 with one slot busy -> 1 slot of capacity, + the vault row.
        assert await db.count_available_workspaces("p1", worktree_slot_cap=2) == 2

    async def test_capacity_never_goes_negative(self, db):
        """Slots above a shrunk cap (not yet reaped) cannot subtract."""
        await _worktree_project(db)
        for i in (1, 2):
            await acquire_for_task(
                db,
                await _mktask(db, task_id=f"t{i}"),
                (await _mkagent(db, agent_id=f"a{i}")).id,
                worktrees_enabled=True,
            )
        assert await db.count_available_workspaces("p1", worktree_slot_cap=1) == 1

    async def test_redundant_clone_does_not_double_count(self, db):
        """Only the first non-slot row of a kind is its base (§7.3)."""
        await _worktree_project(db)
        await _add_ws(db, ws_id="ws-zzz", path="/repo2", kind_id="project-repo")
        # One base worth of capacity (2) + vault — not two bases worth.
        assert await db.count_available_workspaces("p1", worktree_slot_cap=2) == 3

    async def test_exclusive_clone_kind_counts_inventory(self, db):
        await _add_kind(
            db, kind_id="project-repo", is_git_repo=True, lockable=True,
            mode=KIND_MODE_EXCLUSIVE_CLONE,
        )
        await _add_ws(db, ws_id="ws-a", path="/repo-a", kind_id="project-repo")
        await _add_ws(db, ws_id="ws-b", path="/repo-b", kind_id="project-repo")
        assert await db.count_available_workspaces("p1", worktree_slot_cap=9) == 3

    async def test_no_cap_is_the_legacy_count(self, db):
        await _worktree_project(db)
        # base + 2 slots + vault, all unlocked.
        assert await db.count_available_workspaces("p1") == 4


# ─────────────────── one rule for "which row is the base" ────────────────


class TestBaseDesignationIsSingleSourced:
    """F2: capacity and acquisition must agree on the base.

    ``find_worktree_base`` orders by (clone-before-link, id).  A second
    definition that sorted by id alone lived in
    ``count_available_workspaces``; with a LINK row whose id sorts before the
    CLONE row the two picked *different* rows, so capacity was measured
    against a base owning no slots — permanently reporting a full cap while
    acquisition could hand out nothing.  Ids are generated, so which way it
    fell was a coin flip.
    """

    async def _link_before_clone(self, db):
        await _add_kind(
            db,
            kind_id="project-repo",
            is_git_repo=True,
            lockable=True,
            mode=KIND_MODE_WORKTREE,
            default_lock_mode="exclusive",
        )
        # 'ws-aaa-link' sorts before 'ws-zzz-clone' by id, but the base rule
        # prefers clones.
        await db.create_workspace(
            Workspace(
                id="ws-aaa-link",
                project_id="p1",
                workspace_path="/repo-link",
                source_type=RepoSourceType.LINK,
                kind_id="project-repo",
            )
        )
        await _add_ws(db, ws_id="ws-zzz-clone", path="/repo", kind_id="project-repo")
        for i in (0, 1):
            await _add_ws(
                db,
                ws_id=f"ws-slot-{i}",
                path=f"/repo/.aq/worktrees/slot-{i}",
                kind_id="project-repo",
                slot_index=i,
                base_id="ws-zzz-clone",
            )

    async def test_capacity_agrees_with_acquisition_when_a_link_sorts_first(self, db):
        await self._link_before_clone(db)
        assert (await db.find_worktree_base("p1", "project-repo")).id == "ws-zzz-clone"

        # Fill both slots.
        for i in (1, 2):
            await acquire_for_task(
                db,
                await _mktask(db, task_id=f"t{i}"),
                (await _mkagent(db, agent_id=f"a{i}")).id,
                worktrees_enabled=True,
            )
        # Nothing left to acquire...
        assert (
            await db.acquire_one_unlocked(
                project_id="p1",
                kind_id="project-repo",
                mode="exclusive",
                locked_by_task_id="t9",
                locked_by_agent_id="a9",
                kind_mode=KIND_MODE_WORKTREE,
            )
            is None
        )
        # ...so capacity for the git kind must be 0 (the vault row is the
        # only thing left).  It used to report a full cap of 2 on top.
        assert await db.count_available_workspaces("p1", worktree_slot_cap=2) == 1


# ─────────────── out-of-cap slots: one bound, both directions ────────────


class TestSlotCapBoundIsSymmetric:
    """F5: acquisition and capacity must apply the same cap bound.

    ``count_available_workspaces`` and ``_ensure_worktree_slots_for_task``
    both bound slots at ``slot_index < cap``; ``acquire_one_unlocked`` filtered
    only on ``slot_index IS NOT NULL``.  After a cap shrink, capacity read 0
    while acquisition still handed out a slot above the cap.
    """

    async def _shrunk_cap(self, db):
        await _worktree_project(db, slots=4)
        for i in (1, 2):
            await acquire_for_task(
                db,
                await _mktask(db, task_id=f"t{i}"),
                (await _mkagent(db, agent_id=f"a{i}")).id,
                worktrees_enabled=True,
            )  # takes slots 0 and 1

    async def test_out_of_cap_slot_is_not_acquired(self, db):
        await self._shrunk_cap(db)
        assert await db.count_available_workspaces("p1", worktree_slot_cap=2) == 1

        got = await db.acquire_one_unlocked(
            project_id="p1",
            kind_id="project-repo",
            mode="exclusive",
            locked_by_task_id="t3",
            locked_by_agent_id="a3",
            kind_mode=KIND_MODE_WORKTREE,
            worktree_slot_cap=2,
        )
        assert got is None, "slot-2 and slot-3 are above the cap"

    async def test_without_a_cap_every_slot_is_still_a_candidate(self, db):
        """The bound is opt-in; the caller passes it only under worktree mode."""
        await self._shrunk_cap(db)
        await _mktask(db, task_id="t3")
        await _mkagent(db, agent_id="a3")
        got = await db.acquire_one_unlocked(
            project_id="p1",
            kind_id="project-repo",
            mode="exclusive",
            locked_by_task_id="t3",
            locked_by_agent_id="a3",
            kind_mode=KIND_MODE_WORKTREE,
        )
        assert got is not None and got.slot_index == 2

    async def test_in_cap_slot_is_still_acquirable(self, db):
        await _worktree_project(db, slots=4)
        await _mktask(db, task_id="t1")
        await _mkagent(db, agent_id="a1")
        got = await db.acquire_one_unlocked(
            project_id="p1",
            kind_id="project-repo",
            mode="exclusive",
            locked_by_task_id="t1",
            locked_by_agent_id="a1",
            kind_mode=KIND_MODE_WORKTREE,
            worktree_slot_cap=2,
        )
        assert got is not None and got.slot_index == 0
