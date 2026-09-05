"""SQLite/PostgreSQL parity tests for durable onboarding request storage."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import EventsConfig, load_config
from src.database import Database, DatabaseBackend, SQLiteDatabaseAdapter
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


def test_onboarding_retention_config_round_trips_and_validates(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "database_path: test.db\n"
        "discord:\n"
        "  bot_token: test-token\n"
        "  guild_id: '1'\n"
        "events:\n"
        "  onboarding_request_retention_days: 14\n",
        encoding="utf-8",
    )
    assert load_config(str(path)).events.onboarding_request_retention_days == 14
    assert EventsConfig(onboarding_request_retention_days=0).validate()[0].field == (
        "onboarding_request_retention_days"
    )


def test_database_protocol_exposes_onboarding_queries():
    assert issubclass(SQLiteDatabaseAdapter, DatabaseBackend)


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "onboarding.db"))
        await database.initialize()
    yield database
    await database.close()


async def test_create_replay_phase_ledger_and_terminal_finish(db):
    created, fingerprint = await db.create_onboarding_request(
        "request-1", "fingerprint-a", phase="preflight", now=100.0
    )
    assert (created, fingerprint) == (False, "fingerprint-a")

    replayed, stored_fingerprint = await db.create_onboarding_request(
        "request-1", "other-input", phase="wrong", now=101.0
    )
    assert (replayed, stored_fingerprint) == (True, "fingerprint-a")

    assert await db.update_onboarding_phase("request-1", "prepared", now=102.0)
    assert await db.append_onboarding_resource(
        "request-1", {"kind": "staging_dir", "path": "/tmp/.aq-request-1"}, now=103.0
    )
    assert await db.finish_onboarding_request(
        "request-1", "succeeded", result={"project_id": "new-project"}, now=104.0
    )

    row = await db.get_onboarding_request("request-1")
    assert row is not None
    assert row["phase"] == "prepared"
    assert row["created_resources"] == [{"kind": "staging_dir", "path": "/tmp/.aq-request-1"}]
    assert row["status"] == "succeeded"
    assert row["result"] == {"project_id": "new-project"}
    assert row["finished_at"] == 104.0
    assert not await db.update_onboarding_phase("request-1", "changed-after-finish", now=105.0)
    assert not await db.append_onboarding_resource("request-1", {"kind": "late"}, now=105.0)
    assert not await db.finish_onboarding_request(
        "request-1", "failed", error={"code": "late"}, now=105.0
    )


async def test_purge_only_removes_old_terminal_requests(db):
    for request_id, fingerprint in (("old", "a"), ("fresh", "b"), ("pending", "c")):
        await db.create_onboarding_request(request_id, fingerprint, now=10.0)
    assert await db.finish_onboarding_request("old", "failed", error={"code": "old"}, now=20.0)
    assert await db.finish_onboarding_request("fresh", "succeeded", result={"ok": True}, now=90.0)

    assert await db.purge_finished_onboarding_requests(50.0) == 1
    assert await db.get_onboarding_request("old") is None
    assert await db.get_onboarding_request("fresh") is not None
    pending = await db.get_onboarding_request("pending")
    assert pending is not None
    assert pending["status"] == "pending"


async def test_operational_retention_sweeps_onboarding_records_once_per_hour(monkeypatch):
    """Onboarding cleanup remains live while Playbook V2 storage is paused."""
    from src.orchestrator.monitoring import MonitoringMixin

    now = 2_000_000.0
    monkeypatch.setattr("src.orchestrator.monitoring.time.time", lambda: now)

    class Harness(MonitoringMixin):
        pass

    harness = Harness()
    harness._last_operational_event_retention_sweep = 0.0
    harness.config = SimpleNamespace(events=SimpleNamespace(onboarding_request_retention_days=30))
    harness.db = SimpleNamespace(purge_finished_onboarding_requests=AsyncMock(return_value=2))

    await harness._sweep_operational_event_retention()
    await harness._sweep_operational_event_retention()

    harness.db.purge_finished_onboarding_requests.assert_awaited_once_with(now - 30 * 86_400.0)
