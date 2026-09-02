"""PostgreSQL API contracts for the worker-pool management endpoints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import httpx

from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn() or ""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set"),
]


POOL_PROFILE = """---
id: worker
name: Worker
---

## Role

Do pool work.

## Config

```json
{
  "lifecycle": "pool",
  "min_active": 1,
  "max_active": 2
}
```
"""


@pytest.fixture
async def pool_api(tmp_path, monkeypatch):
    """A real typed API over the PostgreSQL adapter, with one pool profile."""
    from src.api import dependencies as deps
    from src.api.app import create_app
    from src.config import AppConfig, DiscordConfig
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.event_bus import EventBus
    from src.models import Project
    from src.orchestrator import Orchestrator
    from src.profiles.sync import sync_profile_text_to_db

    db = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
    await db.initialize()
    await db.reset_for_tests()
    data_dir = tmp_path / "data"
    profile_path = data_dir / "vault" / "agent-types" / "worker" / "profile.md"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(POOL_PROFILE, encoding="utf-8")
    result = await sync_profile_text_to_db(
        POOL_PROFILE, db, source_path=str(profile_path), fallback_id="worker"
    )
    assert result.success, result.errors
    await db.create_project(Project(id="pool-project", name="Pool project", max_concurrent_agents=2))

    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        database_path=str(tmp_path / "unused.db"),
        data_dir=str(data_dir),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.swarm.enabled = True
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.bus = EventBus(env="dev")
    saved = (
        deps._orchestrator,
        deps._command_handler,
        deps._token_store,
        deps._require_session_token,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(orch, config)),
            base_url="http://test",
        ) as client:
            yield client, Path(data_dir)
    finally:
        (
            deps._orchestrator,
            deps._command_handler,
            deps._token_store,
            deps._require_session_token,
        ) = saved
        await db.reset_for_tests()
        await db.close()


async def test_pool_management_routes_round_trip_on_postgres(pool_api):
    """Lifecycle, bounds, and status use the same typed API over Postgres."""
    client, data_dir = pool_api

    lifecycle = await client.post(
        "/api/pool/set-lifecycle",
        json={"project_id": "pool-project", "profile_id": "worker", "lifecycle": "task"},
    )
    assert lifecycle.status_code == 200, lifecycle.text
    assert lifecycle.json()["lifecycle"] == "task"
    assert "\"lifecycle\": \"task\"" in (
        data_dir / "vault" / "projects" / "pool-project" / "agent-types" / "worker" / "profile.md"
    ).read_text(encoding="utf-8")

    enabled = await client.post(
        "/api/pool/set-lifecycle",
        json={"project_id": "pool-project", "profile_id": "worker", "lifecycle": "pool"},
    )
    assert enabled.status_code == 200, enabled.text

    scaled = await client.post(
        "/api/pool/scale",
        json={"project_id": "pool-project", "profile_id": "worker", "min": 0, "max": None},
    )
    assert scaled.status_code == 200, scaled.text
    assert scaled.json()["project_cap"] == scaled.json()["effective_max_active"] == 2

    status = await client.post("/api/pool/status", json={"project_id": "pool-project"})
    assert status.status_code == 200, status.text
    assert status.json()["pools"] == [
        {
            "project_id": "pool-project",
            "profile_id": "worker",
            "min_active": 0,
            "max_active": None,
            "desired": 0,
            "running_idle": 0,
            "running_busy": 0,
            "starting": 0,
            "draining": 0,
            "ready": 0,
            "quarantined_until": None,
            "instances": [],
        }
    ]
