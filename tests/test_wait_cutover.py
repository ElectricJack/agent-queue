"""Cutover flags gate admission while durable outcomes and recovery keep working."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.config import AppConfig
from src.config_wait_policy import WaitProjectOverrides
from src.database.tables import continuations, sessions, task_context, tasks, wait_records
from src.doctor.models import DoctorContext, Severity
from src.doctor.wait_checks import _check_schema
from src.models import TaskStatus
from src.handoffs import latest_note, render_note
from src.waits.policy import enabled
from src.waits.recovery import orphan_report, repair_orphans
from tests.test_wait_dispatch import runtime
from tests.test_wait_hibernation import env as _checkpoint_env
from tests.test_wait_queries import NOW, env as _runtime_env, records, register

checkpoint_env = _checkpoint_env
runtime_env = _runtime_env


def test_canary_activation_and_project_disable():
    config = AppConfig()
    config.wait_policy.v2_enabled = False
    config.wait_policy.scheduler_waits_enabled = False
    config.wait_policy.projects = {"canary": WaitProjectOverrides(
        v2_enabled=True, scheduler_waits_enabled=True,
    )}
    assert enabled(config) and enabled(config, scheduler=True)
    assert enabled(config, "canary", scheduler=True)
    assert not enabled(config, "other")
    config.wait_policy.v2_enabled = True
    config.wait_policy.projects["canary"].v2_enabled = False
    assert not enabled(config, "canary") and enabled(config, "other")


async def test_disabled_runtime_finishes_an_existing_wait(runtime_env):
    env = runtime_env
    receipt = await register(env)
    service = runtime(env, enabled=False)
    service.orch.wait_dispatcher = service._dispatcher
    service.orch.config.wait_policy.hibernation_enabled = False
    assert await service.processing_enabled()
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    row = (await records(env))[0]
    assert row["id"] == receipt["wait_id"] and row["outcome"] == "satisfied"


async def test_dormant_service_starts_after_canary_hot_reload(runtime_env):
    service = runtime(runtime_env, enabled=False)
    service.listener.renewal_seconds = 0.01
    service.listener.run = AsyncMock()
    background = asyncio.create_task(service.run())
    try:
        await asyncio.sleep(0)
        service.listener.run.assert_not_awaited()
        service.orch.config.wait_policy.projects["p"] = WaitProjectOverrides(v2_enabled=True)
        await asyncio.wait_for(background, 5)
        service.listener.run.assert_awaited_once()
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)


async def test_schema_inventory_includes_flags_revisions_and_counts(checkpoint_env):
    result = await _check_schema(DoctorContext(config=AppConfig(), db=checkpoint_env.db))
    assert result.severity == Severity.OK
    assert result.data["flags"]["v1_bodies"] == "refuse"
    assert result.data["row_counts"]["continuations"] == 1
    assert "revisions" in result.data and not result.data["missing_tables"]


@pytest.mark.parametrize("outcome", ["pending", "satisfied"])
async def test_orphan_repair_holds_live_owner_then_preserves_checkpoint(checkpoint_env, outcome):
    env = checkpoint_env
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(status="READY"))
        await conn.execute(update(continuations).values(summary="Finish the checked change",
                                                       saved_head="abc123"))
        await conn.execute(update(wait_records).values(outcome=outcome))
        assert len(await orphan_report(conn, project_id="p")) == 1
    held = await repair_orphans(env.db, project_id="p", now=NOW + 1)
    assert not held["repaired"] and held["held"][0]["hold"] == "ownership_not_released"
    async with env.db._engine.begin() as conn:
        await conn.execute(update(sessions).values(state="stopped", desired_state="stopped"))
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(assigned_agent_id=None))
    result = await repair_orphans(env.db, project_id="p", now=NOW + 2)
    assert len(result["repaired"]) == 1 and not result["held"]
    async with env.db._engine.connect() as conn:
        row = (await conn.execute(select(continuations))).mappings().one()
        assert row["summary"] == "Finish the checked change" and row["saved_head"] == "abc123"
        assert row["resume_state"] == "cancelled"
        wait = (await conn.execute(select(wait_records))).mappings().one()
        assert wait["outcome"] == ("source_unavailable" if outcome == "pending" else outcome)
        note = dict((await conn.execute(select(task_context))).mappings().one())
        saved, payload = latest_note([note])
        assert "Finish the checked change" in render_note(saved, payload, {"task_id": "owner"})
        assert payload["facts"]["head"] == "abc123"
        assert not await orphan_report(conn, project_id="p")
    assert not (await repair_orphans(env.db, now=NOW + 3))["repaired"]


@pytest.mark.parametrize("missing", ["summary", "saved_head"])
@pytest.mark.parametrize("status", ["READY", "IN_PROGRESS"])
async def test_orphan_repair_holds_missing_checkpoint_evidence(checkpoint_env, missing, status):
    env = checkpoint_env
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
            status=status, assigned_agent_id=None))
        await conn.execute(update(sessions).values(state="stopped", desired_state="stopped"))
        await conn.execute(update(continuations).values(
            **{"summary": "Preserve my work", "saved_head": "abc123", missing: None}))
    result = await repair_orphans(env.db, project_id="p", now=NOW + 1)
    assert not result["repaired"]
    assert result["held"][0]["hold"] == "summary_or_saved_head_missing"
    async with env.db._engine.connect() as conn:
        row = (await conn.execute(select(continuations))).mappings().one()
        assert row[missing] is None and row["resume_state"] == "sleeping"
        assert not list((await conn.execute(select(task_context))).mappings())
