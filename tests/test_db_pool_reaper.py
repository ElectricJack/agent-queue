"""Crash recovery for the template-cloned PostgreSQL lease pool.

Pool clones are named under the process owner token from ``tests/pg_dsn.py``,
so the owner lock that vouches for a worker's own databases vouches for its
clones, and the same orphan sweep reaps them after SIGTERM or SIGKILL.
"""

from __future__ import annotations

import pytest

from tests import db_fixtures, pg_dsn

_TOKEN = "0123456789ab"
_RUN = "fedcba9876543210"  # the shape aq test gives AQ_TEST_RUN_ID


@pytest.fixture(autouse=True)
def _isolated_owner(monkeypatch):
    """Never touch this pytest process's real owner token, run token or lock."""
    monkeypatch.setattr(pg_dsn, "_OWNER_TOKEN", _TOKEN)
    monkeypatch.setattr(pg_dsn, "_CACHED_RUN_ID", _RUN)
    monkeypatch.setattr(pg_dsn, "_OWNER", None)


def test_pool_clones_are_named_under_the_process_owner_token():
    first = db_fixtures.LeasePool("postgresql://u:p@h/worker", "gw0")
    second = db_fixtures.LeasePool("postgresql://u:p@h/worker", "gw0")

    for pool in (first, second):
        name = pool._name(0)
        match = pg_dsn._OWNED_NAME_RE.fullmatch(name)
        assert match and match[1] == _TOKEN
        assert len(name) <= 63
        # The operator reaper (src/resources/test_db_reaper.py) keeps a live
        # aq test slot's databases by this run token.
        assert name.startswith(f"aq_test_ownv2_{_TOKEN}_{_RUN}_")
    assert first._name(0) != second._name(0) != first._name(1)


@pytest.mark.asyncio
async def test_pool_confirms_the_owner_lock_before_every_clone(monkeypatch):
    events: list[str] = []

    async def hold(base_dsn):
        assert base_dsn == "postgresql://u:p@h/worker"
        events.append("lock")

    async def clone(_dsn, name):
        events.append(f"clone:{name}")
        return f"postgresql://u:p@h/{name}"

    async def drop(targets):
        events.extend(f"drop:{name}" for _admin, name in targets)
        return []

    monkeypatch.setattr(db_fixtures, "hold_owner_lock", hold)
    monkeypatch.setattr(db_fixtures, "clone_database", clone)
    monkeypatch.setattr(db_fixtures, "drop_databases", drop)
    monkeypatch.setattr(db_fixtures, "_SEED", {})
    pool = db_fixtures.LeasePool("postgresql://u:p@h/worker", "worker name")
    first, second = pool._name(0), pool._name(1)

    await pool.acquire()
    await pool.acquire()
    await pool.dispose()

    assert events[:4] == ["lock", f"clone:{first}", "lock", f"clone:{second}"]
    # dispose() issues every drop at once, so they share one checkpoint.
    assert sorted(events[4:]) == sorted([f"drop:{first}", f"drop:{second}"])


@pytest.mark.asyncio
async def test_pool_without_a_provable_owner_lock_clones_nothing(monkeypatch):
    async def lost(_base_dsn):
        raise RuntimeError("lost this process's PostgreSQL test owner lock")

    async def clone(_dsn, _name):
        raise AssertionError("cloned without an owner lock")

    monkeypatch.setattr(db_fixtures, "hold_owner_lock", lost)
    monkeypatch.setattr(db_fixtures, "clone_database", clone)
    pool = db_fixtures.LeasePool("postgresql://u:p@h/worker", "gw1")

    with pytest.raises(RuntimeError, match="owner lock"):
        await pool.acquire()
    assert pool._created == set()


@pytest.mark.asyncio
async def test_a_dead_pools_clones_are_candidates_for_the_orphan_sweep():
    dead = db_fixtures.LeasePool("postgresql://u:p@h/worker", "gw0")._name(0)
    names = [
        dead,
        "aq_test_poolv2_abc123def456_gw0_0",  # earlier pool shape: no owner lock
        "aq_test_abc123def456_gw0_0",  # legacy pool shape: no owner lock
    ]

    class Connection:
        async def fetch(self, statement):
            assert "pg_database" in statement
            return [{"datname": name} for name in names]

    groups = await pg_dsn._orphan_groups(Connection(), own_token="ffffffffffff")

    assert groups == {_TOKEN: [dead]}
