"""A train request whose batch can never release it must not wedge the schedule.

Regression for fleet-apex (2026-09-24): agent-queue's schedule still named
``integration-sweep:agent-queue:53`` two weeks after its batch was aborted, so
every flush answered ``coalesced`` and periodic ticks (gated on an unarmed
settling window) never reached the request at all.

Regression for swift-delta-90 (2026-10-04): a daemon restart dropped the task
executing the sealing playbook run, so an accepted request nobody would ever seal
was held for the full ``UNSEALED_GRACE_SECONDS`` -- 59 minutes of coalesced
flushes while two green roots could not batch.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    events,
    integration_batches,
    integration_outbox,
    integration_repair_operations,
    project_integration_leases,
    project_integration_schedules,
    projects,
)

from src.integration.release import IntegrationReleaseService
from src.integration.release import (
    RELEASED_EVENT,
    classify_outstanding_request_on,
)
from src.models import Project


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("integration-stale-schedule")
    await database.create_project(Project(id="p", name="train project"))
    async with database.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(
                hierarchical_integration_mode="train",
                hierarchical_integration_desired_mode="train",
            )
        )
    yield database


async def _schedule(db) -> dict:
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(
                select(project_integration_schedules).where(
                    project_integration_schedules.c.project_id == "p"
                )
            )
        ).mappings().one()
        return dict(row)


async def _lease(db) -> dict | None:
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(
                select(project_integration_leases).where(
                    project_integration_leases.c.project_id == "p"
                )
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None


async def _released_events(db) -> list[dict]:
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(events.c.payload)
                .where(events.c.event_type == RELEASED_EVENT)
                .order_by(events.c.id)
            )
        ).scalars().all()
    return [json.loads(row) for row in rows]


async def _outbox_ids(db) -> list[str]:
    async with db._engine.connect() as conn:
        return list(
            (
                await conn.execute(select(integration_outbox.c.id).order_by(integration_outbox.c.id))
            ).scalars()
        )


async def _batch(db, request_id: str, lifecycle: str, *, batch_id: str = "batch-1") -> None:
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id=batch_id,
                project_id="p",
                repository_id="repo",
                request_id=request_id,
                trigger="manual",
                source_manifest_digest="manifest",
                base_sha=None if lifecycle == "empty" else "a" * 40,
                lifecycle=lifecycle,
                current_revision=0,
                integration_branch=(
                    None if lifecycle == "empty" else f"refs/heads/aq/integration/{batch_id}"
                ),
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                final_main_sha="b" * 40 if lifecycle == "promoted" else None,
                human_abort_reason="retired" if lifecycle == "aborted" else None,
                created_at=10.0,
                updated_at=10.0,
            )
        )


async def _hold_lease(db, batch_id: str = "batch-1", *, expires_at: float = 310.0) -> None:
    async with db.immediate() as conn:
        await conn.execute(
            insert(project_integration_leases).values(
                project_id="p",
                repository_id="repo",
                batch_id=batch_id,
                owner_id=f"sealer-{batch_id}",
                fence_token=3,
                heartbeat_at=10.0,
                expires_at=expires_at,
            )
        )


async def _operation(db, *, state: str, batch_id: str = "batch-1") -> None:
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_operations).values(
                id=f"repair-{batch_id}",
                target_kind="batch",
                batch_id=batch_id,
                episode_id=batch_id,
                active_stage=0,
                state=state,
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=10.0,
                updated_at=10.0,
            )
        )


async def _classify(db, now: float):
    async with db._engine.connect() as conn:
        schedule = (
            await conn.execute(
                select(project_integration_schedules).where(
                    project_integration_schedules.c.project_id == "p"
                )
            )
        ).mappings().one()
        return await classify_outstanding_request_on(conn, "p", schedule, now=now)


async def _wedged(db, lifecycle: str = "aborted") -> dict:
    """The incident's shape: one manual sweep whose batch then ended unreleased."""
    first = {"request_id": "integration-sweep:p:1"}
    async with db.immediate() as conn:
        await conn.execute(insert(project_integration_schedules).values(
            project_id="p", enabled=True, interval_seconds=300,
            next_due_at=300.0, request_sequence=1,
            outstanding_request_id=first["request_id"], outstanding_trigger="manual",
            outstanding_requested_at=10.0, updated_at=10.0,
        ))
    await _batch(db, first["request_id"], lifecycle)
    return first




@pytest.mark.parametrize("lifecycle", ("aborted", "empty", "failed"))
async def test_ended_lifecycles_are_stale(db, lifecycle):
    await _wedged(db, lifecycle)
    state = await _classify(db, now=20.0)
    assert state.verdict == "stale"
    assert state.lifecycle == lifecycle








async def test_another_batchs_lease_blocks_the_release(db):
    await _wedged(db)
    await _hold_lease(db, "someone-else")
    state = await _classify(db, now=20.0)
    assert state.verdict == "blocked"
    assert state.blockers == ({
        "code": "lease",
        "detail": "the project lease is held by another batch",
        "ref": "someone-else",
    },)








async def test_release_frees_an_aborted_batchs_request_but_reports_stale(db):
    first = await _wedged(db)
    await _operation(db, state="cancelled")
    service = IntegrationReleaseService(db)

    result = await service.release("batch-1", 20.0)

    assert result.outcome == "stale"
    assert result.request_id == first["request_id"]
    assert result.operation_id == "repair-batch-1"
    assert (await _schedule(db))["outstanding_request_id"] is None
    assert (await _released_events(db))[0]["released_by"] == "integration_release"
    replay = await service.release("batch-1", 21.0)
    assert replay.outcome == "stale"
    assert len(await _released_events(db)) == 1


async def test_release_waits_on_an_aborted_batch_with_a_live_operation(db):
    first = await _wedged(db)
    await _operation(db, state="escalated")
    result = await IntegrationReleaseService(db).release("batch-1", 20.0)
    assert result.outcome == "wait"
    assert (await _schedule(db))["outstanding_request_id"] == first["request_id"]


async def test_release_frees_a_promoted_batch_whose_lease_is_gone(db):
    """Previously ``invariant_error`` forever: release needs the lease it consumes."""
    first = await _wedged(db, "promoted")
    await _operation(db, state="completed")
    result = await IntegrationReleaseService(db).release("batch-1", 20.0)
    assert result.outcome == "released"
    assert result.request_id == first["request_id"]
    assert (await _schedule(db))["outstanding_request_id"] is None






































@pytest.fixture(autouse=True)
def reconciler_primitive_authority(monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives
    authorize_root_primitives(monkeypatch)




async def test_retired_release_discards_catchup_without_minting_a_request(db):
    first = await _wedged(db)
    await _operation(db, state="cancelled")
    async with db.immediate() as conn:
        await conn.execute(update(project_integration_schedules).values(
            catchup_trigger="manual", catchup_requested_at=15.0, catchup_after_sequence=1,
        ))
    result = await IntegrationReleaseService(db).release("batch-1", 20.0)
    assert result.outcome == "stale"
    assert result.request_id == first["request_id"]
    assert result.catchup_request_id is None
    schedule = await _schedule(db)
    assert schedule["outstanding_request_id"] is None
    assert schedule["catchup_trigger"] is None
    assert schedule["request_sequence"] == 1
    assert await _outbox_ids(db) == []
