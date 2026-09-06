"""Durable, coalescing project integration sweep schedules."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    integration_batches,
    integration_outbox,
    project_integration_schedules,
)
from src.integration.scheduler import IntegrationScheduler
from src.models import Project
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.profiles.capabilities import CapabilityPolicy


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "integration-schedule.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="integration project"))
    yield database
    await database.close()


async def _schedule_row(db):
    async with db._engine.connect() as conn:
        return (
            (
                await conn.execute(
                    select(project_integration_schedules).where(
                        project_integration_schedules.c.project_id == "p"
                    )
                )
            )
            .mappings()
            .one()
        )


async def test_missed_windows_and_manual_calls_coalesce_until_release(db):
    scheduler = IntegrationScheduler(db)
    configured = await scheduler.configure(
        project_id="p", now=0.0, enabled=True, interval_seconds=300
    )
    assert configured["next_due_at"] == 300.0

    first = await scheduler.mark_due(project_id="p", now=3600.0, trigger="periodic")
    assert first == {
        "outcome": "due",
        "project_id": "p",
        "request_id": first["request_id"],
        "trigger": "periodic",
        "requested_at": 3600.0,
        "request_sequence": 1,
        "next_due_at": 3900.0,
    }

    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch-1",
                project_id="p",
                repository_id="repo",
                request_id=first["request_id"],
                trigger=first["trigger"],
                source_manifest_digest="manifest",
                base_sha="a" * 40,
                lifecycle="sealing",
                integration_branch="refs/heads/aq/integration/p/1",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=3600.0,
                updated_at=3600.0,
            )
        )

    for now, trigger in ((3601.0, "manual"), (3602.0, "manual"), (3700.0, "periodic")):
        repeated = await scheduler.mark_due(project_id="p", now=now, trigger=trigger)
        assert repeated["outcome"] == "coalesced"
        assert repeated["request_id"] == first["request_id"]
        assert repeated["trigger"] == "periodic"
        assert repeated["requested_at"] == 3600.0
        assert repeated["request_sequence"] == 1

    row = await _schedule_row(db)
    assert row["catchup_trigger"] == "manual"
    assert row["catchup_requested_at"] == 3601.0
    assert row["catchup_after_sequence"] == 1

    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_outbox))).all()) == 1

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch-1")
            .values(lifecycle="promoted", updated_at=3899.0)
        )
        await conn.execute(
            update(project_integration_schedules)
            .where(project_integration_schedules.c.project_id == "p")
            .values(
                outstanding_request_id=None,
                outstanding_trigger=None,
                outstanding_requested_at=None,
                last_completed_sweep_at=3899.0,
                updated_at=3899.0,
            )
        )

    second = await scheduler.mark_due(project_id="p", now=3900.0, trigger="periodic")
    assert second["outcome"] == "due"
    assert second["request_id"] != first["request_id"]
    assert second["request_sequence"] == 2
    assert second["next_due_at"] == 4200.0


@pytest.mark.parametrize(
    ("catchup_trigger", "catchup_at", "expected_next_due"),
    (("manual", 20.0, 300.0), ("periodic", 600.0, 900.0)),
)
async def test_promoted_pending_release_preserves_first_catchup(
    db, catchup_trigger, catchup_at, expected_next_due
):
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(
        project_id="p", now=0.0, enabled=True, interval_seconds=300
    )
    first = await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="promoted-batch",
                project_id="p",
                repository_id="repo",
                request_id=first["request_id"],
                trigger=first["trigger"],
                source_manifest_digest="manifest",
                base_sha="a" * 40,
                lifecycle="promoted",
                current_revision=0,
                integration_branch="refs/heads/aq/integration/promoted",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                final_main_sha="b" * 40,
                created_at=10.0,
                updated_at=10.0,
            )
        )

    caught_up = await scheduler.mark_due(
        project_id="p", now=catchup_at, trigger=catchup_trigger
    )
    assert caught_up["outcome"] == "coalesced"
    assert caught_up["request_id"] == first["request_id"]
    assert caught_up["request_sequence"] == first["request_sequence"] == 1
    assert caught_up["trigger"] == first["trigger"] == "manual"
    assert caught_up["requested_at"] == first["requested_at"] == 10.0
    assert caught_up["next_due_at"] == expected_next_due

    later_trigger = "periodic" if catchup_trigger == "manual" else "manual"
    later_at = 600.0 if later_trigger == "periodic" else catchup_at + 1.0
    await scheduler.mark_due(project_id="p", now=later_at, trigger=later_trigger)
    row = await _schedule_row(db)
    assert row["catchup_trigger"] == catchup_trigger
    assert row["catchup_requested_at"] == catchup_at
    assert row["catchup_after_sequence"] == 1
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_outbox))).all()) == 1


async def test_disabled_periodic_retains_state_but_manual_is_permitted(db):
    scheduler = IntegrationScheduler(db)

    disabled = await scheduler.mark_due(project_id="p", now=0.0, trigger="periodic")
    assert disabled["outcome"] == "disabled"
    assert disabled["request_id"] is None
    assert disabled["next_due_at"] == 300.0

    manual = await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")
    assert manual["outcome"] == "due"
    assert manual["trigger"] == "manual"
    disabled_again = await scheduler.mark_due(
        project_id="p", now=600.0, trigger="periodic"
    )
    assert disabled_again["outcome"] == "disabled"
    row = await _schedule_row(db)
    assert row["next_due_at"] == 300.0
    assert row["outstanding_request_id"] == manual["request_id"]
    assert row["request_sequence"] == 1


async def test_interval_edit_resets_boundary_and_preserves_first_request(db):
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(
        project_id="p", now=0.0, enabled=True, interval_seconds=300
    )
    first = await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")

    configured = await scheduler.configure(
        project_id="p", now=50.0, enabled=True, interval_seconds=90
    )
    assert configured["next_due_at"] == 140.0
    assert configured["outstanding_request_id"] == first["request_id"]
    coalesced = await scheduler.mark_due(
        project_id="p", now=140.0, trigger="periodic"
    )
    assert coalesced["outcome"] == "coalesced"
    assert coalesced["trigger"] == "manual"
    assert coalesced["requested_at"] == 10.0
    assert coalesced["request_sequence"] == 1
    assert coalesced["next_due_at"] == 230.0


async def test_concurrent_duplicate_delivery_allocates_one_request(db):
    scheduler = IntegrationScheduler(db)
    first, second = await asyncio.gather(
        scheduler.mark_due(project_id="p", now=10.0, trigger="manual"),
        scheduler.mark_due(project_id="p", now=10.0, trigger="manual"),
    )

    assert {first["outcome"], second["outcome"]} == {"due", "coalesced"}
    assert first["request_id"] == second["request_id"]
    assert first["request_sequence"] == second["request_sequence"] == 1
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_outbox))).all()) == 1


async def test_not_due_and_restart_duplicate_delivery_are_durable(tmp_path):
    path = tmp_path / "restart-schedule.db"
    first_db = Database(str(path))
    await first_db.initialize()
    await first_db.create_project(Project(id="p", name="integration project"))
    first_scheduler = IntegrationScheduler(first_db)
    await first_scheduler.configure(
        project_id="p", now=0.0, enabled=True, interval_seconds=300
    )
    not_due = await first_scheduler.mark_due(
        project_id="p", now=299.0, trigger="periodic"
    )
    assert not_due["outcome"] == "not_due"
    first = await first_scheduler.mark_due(
        project_id="p", now=300.0, trigger="periodic"
    )
    await first_db.close()

    restarted_db = Database(str(path))
    await restarted_db.initialize()
    try:
        restarted = IntegrationScheduler(restarted_db)
        replay = await restarted.mark_due(
            project_id="p", now=300.0, trigger="periodic"
        )
        assert replay["outcome"] == "coalesced"
        assert replay["request_id"] == first["request_id"]
        assert replay["requested_at"] == first["requested_at"]
        assert replay["request_sequence"] == first["request_sequence"]
        async with restarted_db._engine.connect() as conn:
            events = (await conn.execute(select(integration_outbox))).mappings().all()
        assert len(events) == 1
        assert events[0]["event_type"] == "integration.sweep_due"
        assert events[0]["payload"]["operation_id"] == first["request_id"]
    finally:
        await restarted_db.close()


def _due_result():
    return {
        "outcome": "due",
        "project_id": "p",
        "request_id": "integration-sweep:p:1",
        "trigger": "manual",
        "requested_at": 10.0,
        "request_sequence": 1,
        "next_due_at": 300.0,
    }


async def test_schedule_command_delegates_for_trusted_local(command_handler_factory):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="integration project"))
    scheduler = AsyncMock()
    scheduler.mark_due.return_value = _due_result()
    handler.orchestrator.integration_scheduler = scheduler

    result = await handler.execute(
        "integration_schedule_due",
        {"project_id": "p", "now": 10.0, "trigger": "manual"},
    )

    assert result == {"success": True, **_due_result()}
    scheduler.mark_due.assert_awaited_once_with("p", 10.0, "manual")
    await handler.db.close()


async def test_schedule_command_denies_sessions_and_scopes_playbooks(
    command_handler_factory,
):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="integration project"))
    scheduler = AsyncMock()
    scheduler.mark_due.return_value = _due_result()
    handler.orchestrator.integration_scheduler = scheduler
    args = {"project_id": "p", "now": 10.0, "trigger": "manual"}

    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_schedule_due"]
        ),
        project_id="p",
        session_id="session",
    )
    with principal_context(session):
        denied_session = await handler.execute("integration_schedule_due", args)
    assert denied_session["outcome"] == "unauthorized"

    wrong_project = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_schedule_due"]
        ),
        project_id="other",
    )
    with principal_context(wrong_project):
        denied_project = await handler.execute("integration_schedule_due", args)
    assert denied_project["outcome"] == "unauthorized"

    capable = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_schedule_due"]
        ),
        project_id="p",
    )
    with principal_context(capable):
        allowed = await handler.execute("integration_schedule_due", args)
    assert allowed == {"success": True, **_due_result()}
    scheduler.mark_due.assert_awaited_once_with("p", 10.0, "manual")
    await handler.db.close()
