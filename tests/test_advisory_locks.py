"""Advisory locks held across slow work never leave a backend idle in a transaction.

On 2026-10-27 (brisk-glacier-94) the source-CI observation held
``pg_advisory_xact_lock`` on one connection while it awaited Git I/O on
others: that backend sat ``idle in transaction`` for ~20 minutes and nobody
could say which daemon work owned the key.  These tests pin the session-lock
helper that replaced it and the ``db.idle_advisory_locks`` doctor control that
names and releases such a holder.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from sqlalchemy import text

from src.commands.integration_commands import IntegrationCommandsMixin
from src.config import AppConfig
from src.database.advisory_locks import held_advisory_locks, session_advisory_lock
from src.doctor.models import DoctorContext, Severity
from src.integration.source_ci import SourceCIObservation

checks = importlib.import_module("src.doctor.advisory_lock_checks")


async def _key(conn, name: str) -> int:
    return int(await conn.scalar(text("SELECT hashtextextended(:name, 0)"), {"name": name}))


async def _holders(conn, key: int) -> list[dict]:
    """Backends holding the int8 advisory lock *key*, with their state."""
    rows = await conn.execute(text("""
        SELECT a.pid, a.state, a.xact_start
        FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
        WHERE l.locktype = 'advisory' AND l.objsubid = 1 AND l.granted
          AND ((l.classid::bigint << 32) | l.objid::bigint) = :key
    """), {"key": key})
    return [dict(row) for row in rows.mappings()]


async def _autocommit(engine):
    conn = await engine.connect()
    return await conn.execution_options(isolation_level="AUTOCOMMIT")


async def test_session_lock_holds_no_transaction_while_held(reuse_database):
    db = await reuse_database()
    probe = await _autocommit(db._engine)
    try:
        key = await _key(probe, "test:idle")
        async with session_advisory_lock(db._engine, "test:idle"):
            holders = await _holders(probe, key)
            assert len(holders) == 1
            assert holders[0]["state"] == "idle"
            assert holders[0]["xact_start"] is None
            assert [name for name, _at in held_advisory_locks().values()] == ["test:idle"]
        assert await _holders(probe, key) == []
        assert held_advisory_locks() == {}
    finally:
        await probe.close()


async def test_session_lock_excludes_the_old_transaction_lock_key(reuse_database):
    """A daemon still running ``pg_advisory_xact_lock`` contends on the same key."""
    db = await reuse_database()
    other = await _autocommit(db._engine)
    try:
        async with session_advisory_lock(db._engine, "test:shared"):
            assert not await other.scalar(text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(:name, 0))"),
                {"name": "test:shared"})
        assert await other.scalar(text(
            "SELECT pg_try_advisory_lock(hashtextextended(:name, 0))"), {"name": "test:shared"})
        await other.execute(text("SELECT pg_advisory_unlock_all()"))
    finally:
        await other.close()


async def test_second_holder_waits_for_release(reuse_database):
    db = await reuse_database()
    order: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with session_advisory_lock(db._engine, "test:serial"):
            order.append("first")
            entered.set()
            await release.wait()
            order.append("first-done")

    async def second():
        await entered.wait()
        async with session_advisory_lock(db._engine, "test:serial"):
            order.append("second")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await entered.wait()
    await asyncio.sleep(0.3)
    assert order == ["first"]
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    assert order == ["first", "first-done", "second"]


@pytest.mark.parametrize("outcome", ["error", "cancel"])
async def test_lock_released_when_the_body_fails(reuse_database, outcome):
    db = await reuse_database()
    entered = asyncio.Event()

    async def body():
        async with session_advisory_lock(db._engine, "test:release"):
            entered.set()
            if outcome == "error":
                raise RuntimeError("boom")
            await asyncio.Event().wait()

    task = asyncio.create_task(body())
    await entered.wait()
    if outcome == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
        await task
    probe = await _autocommit(db._engine)
    try:
        assert await _holders(probe, await _key(probe, "test:release")) == []
    finally:
        await probe.close()
    assert held_advisory_locks() == {}


async def test_source_ci_observation_awaits_its_work_outside_a_transaction(reuse_database):
    """The incident path: the observation's lock holder is idle, never idle in transaction."""
    db = await reuse_database()
    observation = SourceCIObservation(
        task_id="t-1", source={"project_id": "p", "repository_id": "r", "base": "b" * 40,
                               "head": "h" * 40, "generation": 1},
        policy_generation=1, state="success", evidence={},
    )
    seen = {}

    class Handler(IntegrationCommandsMixin):
        def __init__(self):
            self.db = db

        async def _record_integration_source_ci(self, observed):
            # Stands in for the delivery proof's slow Git I/O.
            probe = await _autocommit(db._engine)
            try:
                key = await _key(probe, self._integration_source_ci_identity(observed))
                seen["holders"] = await _holders(probe, key)
            finally:
                await probe.close()
            return {"success": True}

    assert await Handler()._cmd_observe_integration_source_ci(observation) == {"success": True}
    assert [row["state"] for row in seen["holders"]] == ["idle"]
    assert seen["holders"][0]["xact_start"] is None


def _context(db) -> DoctorContext:
    return DoctorContext(config=AppConfig(), db=db, handler=None)


async def test_doctor_names_terminates_only_an_idle_transaction_holder(
    reuse_database, monkeypatch,
):
    db = await reuse_database()
    monkeypatch.setattr(checks, "IDLE_LIMIT_S", 0.0)
    stuck = await db._engine.connect()
    try:
        await stuck.execute(text(
            "SELECT set_config('application_name', 'stuck-holder', false)"))
        await stuck.execute(text(
            "SELECT pg_advisory_xact_lock(hashtextextended('test:stuck', 0))"))
        stuck_pid = await stuck.scalar(text("SELECT pg_backend_pid()"))
        async with session_advisory_lock(db._engine, "test:session"):
            await asyncio.sleep(0.05)
            result = await checks._check_idle_advisory_locks(_context(db))
            assert result.severity == Severity.WARN
            assert result.data["idle_in_transaction"] == [stuck_pid]
            assert f"pid {stuck_pid} (stuck-holder)" in result.detail
            named = [lock["lock"] for backend in result.data["backends"]
                     for lock in backend["held"]]
            assert any(label.startswith("test:session (key ") for label in named)

            fixed = await checks._fix_idle_advisory_locks(_context(db))
            assert fixed.data == {"terminated": [stuck_pid]}
            assert fixed.fix_applied

            after = await checks._check_idle_advisory_locks(_context(db))
            assert after.severity == Severity.OK
            # The session-lock holder is idle by design and survives the fix.
            assert stuck_pid not in [backend["pid"] for backend in after.data["backends"]]
            assert any(lock["lock"].startswith("test:session (key ")
                       for backend in after.data["backends"] for lock in backend["held"])
    finally:
        await stuck.invalidate()
        await stuck.close()


async def test_doctor_ok_when_no_backend_is_idle_in_transaction(reuse_database):
    db = await reuse_database()
    result = await checks._check_idle_advisory_locks(_context(db))
    assert result.severity == Severity.OK
    assert result.data["idle_in_transaction"] == []


async def test_doctor_unavailable_without_a_database():
    result = await checks._check_idle_advisory_locks(_context(None))
    assert result.severity == Severity.INFO
    assert result.execution_status == "unavailable"


def test_int8_key_round_trips_pg_locks_halves():
    for key in (-6212471400645119703, 0, 1, -1, (1 << 63) - 1, -(1 << 63)):
        unsigned = key & ((1 << 64) - 1)
        assert checks._int8_key(unsigned >> 32, unsigned & 0xFFFFFFFF) == key
