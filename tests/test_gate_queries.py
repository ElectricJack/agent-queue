"""Work-graph WG-3: gate query layer.

Covers docs/specs/implementation/work-graph.md §4.3:

* ``create_gate`` inserts gate + ``task_gates`` waiter rows and recomputes.
* ``resolve_gate`` is idempotent, returns waiters that unblocked.
* ``expire_open_gates`` only touches ``open`` rows past ``timeout_at``.
* ``list_gates`` filter combinations.
"""

from __future__ import annotations

import time

import pytest

from src.database import SQLiteDatabaseAdapter
from src.models import DepType, Project, Task, TaskStatus


PROJECT = "p-wg"


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "wg.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="wg"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT, title=tid, description=tid, status=status, **kw)
    )
    return tid


# ── create_gate ──────────────────────────────────────────────────────────


class TestCreateGate:
    async def test_creates_open_gate_and_blocks_waiters(self, db):
        await mktask(db, "t1")
        await mktask(db, "t2")
        # Both waiters unblocked before gate.
        assert (await db.get_task("t1")).is_blocked is False

        gate_id, _ = await db.create_gate(
            PROJECT, "human", "review", waiter_task_ids=["t1", "t2"]
        )
        assert gate_id.startswith("gate-")

        # Waiters flipped to blocked.
        assert (await db.get_task("t1")).is_blocked is True
        assert (await db.get_task("t2")).is_blocked is True

        # Gate row present.
        gate = await db.get_gate(gate_id)
        assert gate["status"] == "open"
        assert gate["gate_type"] == "human"
        assert gate["project_id"] == PROJECT

        # Waiters visible through get_gate_waiters.
        assert await db.get_gate_waiters(gate_id) == {"t1", "t2"}

    async def test_no_waiters_still_creates_gate(self, db):
        gate_id, was_created = await db.create_gate(PROJECT, "timer", "wait 5s")
        assert was_created is True
        assert (await db.get_gate(gate_id))["status"] == "open"
        assert await db.get_gate_waiters(gate_id) == set()

    async def test_dedup_returns_existing_open_gate(self, db):
        # Same (project_id, gate_type, await_id) + identical waiter set → reuse.
        await mktask(db, "t1")
        await mktask(db, "t2")
        gid1, created1 = await db.create_gate(
            PROJECT, "human", "r", await_id="a-1", waiter_task_ids=["t1", "t2"]
        )
        gid2, created2 = await db.create_gate(
            PROJECT, "human", "r-different-title",
            await_id="a-1", waiter_task_ids=["t2", "t1"],
        )
        assert gid1 == gid2
        assert created1 is True
        assert created2 is False
        # Only one row in gates.
        rows = await db.list_gates(project_id=PROJECT)
        assert len([g for g in rows if g["id"] == gid1]) == 1

    async def test_dedup_different_waiter_set_creates_new(self, db):
        await mktask(db, "t1")
        await mktask(db, "t2")
        gid1, _ = await db.create_gate(
            PROJECT, "human", "r", await_id="a-1", waiter_task_ids=["t1"]
        )
        gid2, created2 = await db.create_gate(
            PROJECT, "human", "r", await_id="a-1", waiter_task_ids=["t1", "t2"]
        )
        assert gid1 != gid2
        assert created2 is True

    async def test_dedup_null_await_id_matches(self, db):
        # Both gates have await_id=None — should still dedup.
        gid1, c1 = await db.create_gate(PROJECT, "timer", "a")
        gid2, c2 = await db.create_gate(PROJECT, "timer", "a")
        assert gid1 == gid2
        assert c1 is True and c2 is False

    async def test_dedup_ignores_resolved_gates(self, db):
        # A resolved gate with identical shape must NOT swallow a new one.
        gid1, _ = await db.create_gate(PROJECT, "timer", "a", await_id="x")
        await db.resolve_gate(gid1, resolved_by="jack")
        gid2, created2 = await db.create_gate(PROJECT, "timer", "a", await_id="x")
        assert gid1 != gid2
        assert created2 is True


# ── resolve_gate ─────────────────────────────────────────────────────────


class TestResolveGate:
    async def test_flips_waiters_and_returns_them(self, db):
        await mktask(db, "t1")
        gate_id, _ = await db.create_gate(PROJECT, "human", "r", waiter_task_ids=["t1"])
        assert (await db.get_task("t1")).is_blocked is True

        flipped = await db.resolve_gate(gate_id, resolved_by="jack", resolution="ok")
        assert flipped == {"t1"}
        assert (await db.get_task("t1")).is_blocked is False
        gate = await db.get_gate(gate_id)
        assert gate["status"] == "resolved"
        assert gate["resolved_by"] == "jack"
        assert gate["resolution"] == "ok"

    async def test_idempotent(self, db):
        await mktask(db, "t1")
        gate_id, _ = await db.create_gate(PROJECT, "human", "r", waiter_task_ids=["t1"])
        first = await db.resolve_gate(gate_id, resolved_by="a")
        second = await db.resolve_gate(gate_id, resolved_by="b")
        assert first == {"t1"}
        assert second == set()  # already resolved: no-op
        # First resolver stays authoritative.
        assert (await db.get_gate(gate_id))["resolved_by"] == "a"

    async def test_unknown_gate_returns_empty(self, db):
        assert await db.resolve_gate("gate-xxxxxxxxxxxx", resolved_by="a") == set()

    async def test_resolving_gate_with_other_blockers_no_flip(self, db):
        # Waiter also depends on an incomplete task — resolving the gate
        # doesn't flip is_blocked because the other blocker remains.
        await mktask(db, "dep")
        await mktask(db, "t1")
        await db.add_dependency("t1", "dep", DepType.BLOCKS.value)
        gate_id, _ = await db.create_gate(PROJECT, "human", "r", waiter_task_ids=["t1"])
        assert (await db.get_task("t1")).is_blocked is True

        flipped = await db.resolve_gate(gate_id, resolved_by="a")
        assert flipped == set()
        # Still blocked because of the un-COMPLETED dep.
        assert (await db.get_task("t1")).is_blocked is True


# ── expire_open_gates ────────────────────────────────────────────────────


class TestExpireOpenGates:
    async def test_only_expires_past_timeout_open_gates(self, db):
        now = time.time()
        # Distinct await_ids so create-gate dedup does not collapse them.
        g1, _ = await db.create_gate(PROJECT, "human", "past", await_id="a1", timeout_at=now - 10)
        g2, _ = await db.create_gate(PROJECT, "human", "future", await_id="a2", timeout_at=now + 3600)
        g3, _ = await db.create_gate(PROJECT, "human", "no-timeout", await_id="a3")
        # Pre-resolve one that would otherwise expire.
        g4, _ = await db.create_gate(PROJECT, "human", "already", await_id="a4", timeout_at=now - 10)
        await db.resolve_gate(g4, resolved_by="a")

        expired = await db.expire_open_gates(now)
        assert set(expired) == {g1}
        assert (await db.get_gate(g1))["status"] == "expired"
        assert (await db.get_gate(g2))["status"] == "open"
        assert (await db.get_gate(g3))["status"] == "open"
        assert (await db.get_gate(g4))["status"] == "resolved"

    async def test_expiry_does_not_unblock_waiters(self, db):
        await mktask(db, "t1")
        now = time.time()
        await db.create_gate(
            PROJECT, "human", "will-expire", timeout_at=now - 1, waiter_task_ids=["t1"]
        )
        assert (await db.get_task("t1")).is_blocked is True
        await db.expire_open_gates(now)
        # Design §5.4: expired keeps blocking.
        assert (await db.get_task("t1")).is_blocked is True


# ── list_gates / get_gates_for_task / list_open_gates_by_type ────────────


class TestListing:
    async def test_filter_combinations(self, db):
        p2 = "p-other"
        await db.create_project(Project(id=p2, name="other"))
        # Distinct await_ids so create-gate dedup does not collapse them.
        g_open_h, _ = await db.create_gate(PROJECT, "human", "a", await_id="a")
        g_open_t, _ = await db.create_gate(PROJECT, "timer", "b", await_id="b")
        g_other, _ = await db.create_gate(p2, "human", "c", await_id="c")
        g_resolved, _ = await db.create_gate(PROJECT, "human", "d", await_id="d")
        await db.resolve_gate(g_resolved, resolved_by="jack")

        all_p = await db.list_gates(project_id=PROJECT)
        assert {g["id"] for g in all_p} == {g_open_h, g_open_t, g_resolved}

        open_p = await db.list_gates(project_id=PROJECT, status="open")
        assert {g["id"] for g in open_p} == {g_open_h, g_open_t}

        human_open = await db.list_gates(status="open", gate_type="human")
        assert {g["id"] for g in human_open} == {g_open_h, g_other}

    async def test_get_gates_for_task(self, db):
        await mktask(db, "t1")
        await mktask(db, "t2")
        g1, _ = await db.create_gate(PROJECT, "human", "a", waiter_task_ids=["t1", "t2"])
        g2, _ = await db.create_gate(PROJECT, "timer", "b", waiter_task_ids=["t1"])
        gates_t1 = await db.get_gates_for_task("t1")
        assert {g["id"] for g in gates_t1} == {g1, g2}
        gates_t2 = await db.get_gates_for_task("t2")
        assert {g["id"] for g in gates_t2} == {g1}

    async def test_list_open_gates_by_type(self, db):
        g1, _ = await db.create_gate(PROJECT, "timer", "a", await_id="a")
        g2, _ = await db.create_gate(PROJECT, "timer", "b", await_id="b")
        await db.create_gate(PROJECT, "human", "h", await_id="h")
        await db.resolve_gate(g2, resolved_by="a")
        open_timers = await db.list_open_gates_by_type("timer")
        assert {g["id"] for g in open_timers} == {g1}
