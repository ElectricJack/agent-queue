"""Guardrails for selecting databases for operator cleanup."""

from datetime import datetime, timedelta, timezone
import os
import fcntl

import pytest

from scripts.reap_test_databases import _maintenance_dsn
from src.resources.semaphore import SlotSemaphore
from src.resources.test_db_reaper import (
    DatabaseRecord,
    daemon_database_name,
    decide,
    drop_if_still_eligible,
    reserve_all_test_slots,
)

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=2)


def _candidate(name: str, **overrides) -> DatabaseRecord:
    fields = {"name": name, "oid": 42, "changed_at": OLD, "connections": 0, "invalid": False}
    fields.update(overrides)
    return DatabaseRecord(**fields)


def _decide(candidate: DatabaseRecord, **overrides):
    options = {
        "now": NOW,
        "minimum_age": timedelta(hours=6),
        "protected_names": {"agent_queue"},
        "active_template_slugs": {"0123456789abcdef"},
        "active_run_ids": {"f" * 16},
    }
    options.update(overrides)
    return decide(candidate, **options)


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate("agent_queue"),
        _candidate("aq_test_forgotten", connections=1),
        _candidate("aq_test_forgotten", changed_at=None),
        _candidate("aq_test_forgotten", changed_at=NOW - timedelta(hours=1)),
        _candidate("aq_test_" + "f" * 16 + "_gw0_0"),
        # tests/pg_dsn.py puts its owner token ahead of the run token.
        _candidate("aq_test_ownv2_0123456789ab_" + "f" * 16 + "_gw0"),
        _candidate("aq_test_ownv2_0123456789ab_" + "f" * 16 + "_abcdef012345_gw0_0"),
        _candidate("aq_tmpl_0123456789abcdef"),
        _candidate("aq_tmpl_0123456789abcdef_building"),
        _candidate("aq_manual_database"),
    ],
)
def test_uncertain_or_live_database_is_retained(candidate):
    assert not _decide(candidate).eligible


def test_old_unconnected_test_database_is_eligible():
    decision = _decide(_candidate("aq_test_abcdef012345_gw0_1", invalid=True))
    assert decision.eligible
    assert "invalid" in decision.reason


def test_owned_database_of_a_finished_run_is_eligible():
    decision = _decide(_candidate("aq_test_ownv2_0123456789ab_" + "a" * 16 + "_gw0"))
    assert decision.eligible
    # The run token must lead the tail; appearing later in the name is not ownership.
    assert _decide(_candidate("aq_test_ownv2_0123456789ab_scratch_" + "f" * 16 + "_x")).eligible


def test_unused_schema_template_is_eligible():
    assert _decide(_candidate("aq_tmpl_fedcba9876543210")).eligible


def test_daemon_database_is_read_from_config_not_worker_environment(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("database:\n  url: postgresql+asyncpg://u:p@localhost:5533/aq_test_daemon\n")
    monkeypatch.setenv("AQ_DATABASE_URL", "refuse-worker-database")
    assert daemon_database_name(config) == "aq_test_daemon"
    assert not _decide(
        _candidate("aq_test_daemon"), protected_names={daemon_database_name(config)}
    ).eligible


def test_reaper_requires_a_separate_postgres_maintenance_dsn(monkeypatch):
    monkeypatch.setenv("POSTGRES_TEST_DSN", "postgresql+asyncpg://u:p@localhost/agent_queue")
    with pytest.raises(ValueError, match="maintenance"):
        _maintenance_dsn("agent_queue")
    monkeypatch.setenv("POSTGRES_TEST_DSN", "postgresql+asyncpg://u:p@localhost/postgres")
    assert _maintenance_dsn("agent_queue") == "postgresql://u:p@localhost/postgres"


def test_cleanup_reserves_every_test_slot_and_releases_them(tmp_path):
    semaphore = SlotSemaphore(tmp_path, 2)
    with reserve_all_test_slots(tmp_path, 2):
        assert semaphore.try_acquire() is None
        assert all(slot["held"] for slot in semaphore.snapshot()["slots"])
    acquired = semaphore.try_acquire()
    assert acquired is not None
    os.close(acquired[1])


def test_occupied_slot_releases_partial_reservation(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    slot_one = tmp_path / "slot-1.lock"
    fd = os.open(slot_one, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(RuntimeError, match="slot 1 is occupied"):
            with reserve_all_test_slots(tmp_path, 2):
                pass
        acquired = SlotSemaphore(tmp_path, 2).try_acquire()
        assert acquired is not None and acquired[0] == 0
        os.close(acquired[1])
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_drop_rechecks_identity_before_any_mutation():
    class Connection:
        async def fetchrow(self, *_args):
            return {
                "datname": "aq_test_abcdef012345_gw0_1",
                "oid": 43,
                "changed_at": OLD,
                "connections": 0,
                "datconnlimit": -1,
                "datistemplate": False,
            }

        async def execute(self, *_args):
            pytest.fail("replaced database must not be dropped")

    status = await drop_if_still_eligible(
        Connection(),
        _candidate("aq_test_abcdef012345_gw0_1"),
        now=NOW,
        minimum_age=timedelta(hours=6),
        protected_names={"agent_queue"},
        active_template_slugs=set(),
        active_run_ids=set(),
    )
    assert status == "database identity changed"


@pytest.mark.asyncio
async def test_template_drop_unmarks_template_and_never_forces_connections():
    statements = []

    class Connection:
        async def fetchrow(self, *_args):
            return {
                "datname": "aq_tmpl_fedcba9876543210",
                "oid": 42,
                "changed_at": OLD,
                "connections": 0,
                "datconnlimit": -1,
                "datistemplate": True,
            }

        async def execute(self, statement, *_args):
            statements.append(statement)

    status = await drop_if_still_eligible(
        Connection(),
        _candidate("aq_tmpl_fedcba9876543210"),
        now=NOW,
        minimum_age=timedelta(hours=6),
        protected_names={"agent_queue"},
        active_template_slugs=set(),
        active_run_ids=set(),
    )
    assert status == "dropped"
    assert "datistemplate = false" in statements[0]
    assert statements[1] == 'DROP DATABASE "aq_tmpl_fedcba9876543210"'


@pytest.mark.asyncio
async def test_failed_template_drop_restores_its_original_flag():
    statements = []

    class Connection:
        async def fetchrow(self, *_args):
            return {
                "datname": "aq_tmpl_fedcba9876543210",
                "oid": 42,
                "changed_at": OLD,
                "connections": 0,
                "datconnlimit": -1,
                "datistemplate": True,
            }

        async def execute(self, statement, *_args):
            statements.append(statement)
            if statement.startswith("DROP DATABASE"):
                raise RuntimeError("connection raced with drop")

    with pytest.raises(RuntimeError, match="connection raced"):
        await drop_if_still_eligible(
            Connection(),
            _candidate("aq_tmpl_fedcba9876543210"),
            now=NOW,
            minimum_age=timedelta(hours=6),
            protected_names={"agent_queue"},
            active_template_slugs=set(),
            active_run_ids=set(),
        )
    assert "datistemplate = false" in statements[0]
    assert "datistemplate = true" in statements[-1]
