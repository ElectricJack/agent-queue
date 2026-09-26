"""A train request whose batch can never release it must not wedge the schedule.

Regression for fleet-apex (2026-09-24): agent-queue's schedule still named
``integration-sweep:agent-queue:53`` two weeks after its batch was aborted, so
every flush answered ``coalesced`` and periodic ticks (gated on an unarmed
settling window) never reached the request at all.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert, select, update

from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    events,
    integration_batches,
    integration_candidate_ref_mutations,
    integration_candidate_revisions,
    integration_outbox,
    integration_repair_operations,
    project_integration_leases,
    project_integration_schedules,
    projects,
)
from src.doctor.integration_checks import CHECKS
from src.doctor.models import DoctorContext, Severity
from src.integration.controls import IntegrationControlService
from src.integration.release import IntegrationReleaseService
from src.integration.scheduler import IntegrationScheduler
from src.integration.settling import note_approval
from src.integration.stale_schedule import (
    RELEASED_EVENT,
    UNSEALED_GRACE_SECONDS,
    classify_outstanding_request_on,
)
from src.models import Project
from src.profiles.capabilities import DENY_ALL


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
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(project_id="p", now=0.0, enabled=True, interval_seconds=300)
    first = await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")
    await _batch(db, first["request_id"], lifecycle)
    return first


async def test_flush_after_an_aborted_batch_schedules_a_new_request(db):
    first = await _wedged(db)
    await _hold_lease(db)

    flushed = await IntegrationScheduler(db).mark_due(project_id="p", now=20.0, trigger="manual")

    assert flushed["outcome"] == "due"
    assert flushed["request_id"] == "integration-sweep:p:2"
    assert flushed["request_sequence"] == 2
    assert await _lease(db) is None
    assert await _outbox_ids(db) == [first["request_id"], "integration-sweep:p:2"]
    [released] = await _released_events(db)
    assert released["request_id"] == first["request_id"]
    assert released["batch_id"] == "batch-1"
    assert released["verdict"] == "stale"
    assert released["released_by"] == "integration_scheduler:manual"
    assert released["lease_released"] is True
    assert released["catchup_request_id"] is None


@pytest.mark.parametrize("lifecycle", ("aborted", "empty", "failed"))
async def test_ended_lifecycles_are_stale(db, lifecycle):
    await _wedged(db, lifecycle)
    state = await _classify(db, now=20.0)
    assert state.verdict == "stale"
    assert state.lifecycle == lifecycle


async def test_periodic_tick_frees_a_stale_request_before_the_settling_gate(db):
    """Item 5: an unarmed settling window returns before ``next_due_at`` moves.

    That latch is deliberate (a periodic sweep waits for an approval), so the
    tick that keeps finding the row due is exactly the one that must free a
    request nothing else will end.
    """
    first = await _wedged(db)
    scheduler = IntegrationScheduler(db)

    unarmed = await scheduler.mark_due(project_id="p", now=400.0, trigger="periodic")

    assert unarmed == {"outcome": "not_due", "project_id": "p", "reason": "settling"}
    row = await _schedule(db)
    assert row["outstanding_request_id"] is None
    assert row["next_due_at"] == 300.0
    assert [event["request_id"] for event in await _released_events(db)] == [
        first["request_id"]
    ]

    async with db.immediate() as conn:
        await note_approval(conn, project_id="p", now=400.0)
    due = await scheduler.mark_due(project_id="p", now=700.0, trigger="periodic")
    assert due["outcome"] == "due"
    assert due["request_id"] == "integration-sweep:p:2"
    assert due["next_due_at"] == 900.0


async def test_catchup_recorded_while_active_becomes_the_next_request(db):
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(project_id="p", now=0.0, enabled=True, interval_seconds=300)
    first = await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")
    await _batch(db, first["request_id"], "human_blocked")
    coalesced = await scheduler.mark_due(project_id="p", now=20.0, trigger="manual")
    assert coalesced["outcome"] == "coalesced"
    assert (await _schedule(db))["catchup_trigger"] == "manual"

    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).values(lifecycle="aborted"))
    # Not due and unarmed: only the recorded catch-up can make this tick schedule.
    caught_up = await scheduler.mark_due(project_id="p", now=30.0, trigger="periodic")

    assert caught_up["outcome"] == "due"
    assert caught_up["request_id"] == "integration-sweep:p:2"
    assert caught_up["trigger"] == "manual"
    assert caught_up["requested_at"] == 20.0
    row = await _schedule(db)
    assert row["catchup_trigger"] is None
    assert await _outbox_ids(db) == [first["request_id"], "integration-sweep:p:2"]
    [released] = await _released_events(db)
    assert released["catchup_request_id"] == "integration-sweep:p:2"


async def test_unresolved_write_evidence_blocks_the_release(db):
    first = await _wedged(db)
    await _hold_lease(db)
    await _operation(db, state="active")

    coalesced = await IntegrationScheduler(db).mark_due(project_id="p", now=20.0, trigger="manual")
    assert coalesced["outcome"] == "coalesced"
    assert coalesced["request_id"] == first["request_id"]
    state = await _classify(db, now=20.0)
    assert state.verdict == "blocked"
    assert [blocker["code"] for blocker in state.blockers] == ["live_operation"]

    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(state="cancelled"))
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch-1",
                revision=0,
                construction_base_sha="a" * 40,
                state="constructing",
                created_at=10.0,
                updated_at=10.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_ref_mutations).values(
                id="write", batch_id="batch-1", revision=0,
                purpose="candidate_partial", repository_id="repo",
                branch="refs/heads/aq/integration/batch-1",
                target_branch="refs/heads/aq/integration/batch-1",
                expected_old_sha="a" * 40, desired_sha="c" * 40,
                operation_id="repair-batch-1", operation_episode_id="batch-1",
                operation_stage=0, lease_owner_id="sealer-batch-1", lease_fence_token=3,
                branch_owner_id="repair-batch-1", branch_owner_role="collector",
                branch_fence_token=1, nonce="nonce", state="reserved",
                expires_at=100.0, created_at=10.0, updated_at=10.0,
            )
        )
    assert [b["code"] for b in (await _classify(db, now=20.0)).blockers] == ["ref_mutation"]
    assert (await _lease(db)) is not None

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_candidate_ref_mutations).values(state="applied", remote_sha="c" * 40)
        )
    due = await IntegrationScheduler(db).mark_due(project_id="p", now=30.0, trigger="manual")
    assert due["outcome"] == "due"
    assert await _lease(db) is None


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


async def test_promoted_batch_is_stale_only_once_its_lease_is_gone(db):
    first = await _wedged(db, "promoted")
    await _hold_lease(db)
    assert (await _classify(db, now=20.0)).verdict == "active"
    coalesced = await IntegrationScheduler(db).mark_due(project_id="p", now=20.0, trigger="manual")
    assert coalesced["outcome"] == "coalesced"

    # ``release-stale-owners`` drops an expired lease once cleanup completes;
    # after that only this scheduler can free the request.
    async with db.immediate() as conn:
        await conn.execute(delete(project_integration_leases))
    assert (await _classify(db, now=30.0)).verdict == "stale"
    # The catch-up recorded by the coalesced flush becomes the next request.
    due = await IntegrationScheduler(db).mark_due(project_id="p", now=30.0, trigger="periodic")
    assert due["outcome"] == "due"
    assert due["request_id"] == "integration-sweep:p:2"
    assert (await _released_events(db))[0]["request_id"] == first["request_id"]


async def test_missing_batch_is_in_flight_then_unsealed_then_stale(db):
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(project_id="p", now=0.0, enabled=True, interval_seconds=300)
    first = await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")

    in_flight = await _classify(db, now=20.0)
    assert in_flight.verdict == "in_flight"
    assert in_flight.event == {"delivered_at": None, "attempts": 0, "last_error": None}
    assert (await scheduler.mark_due(project_id="p", now=20.0, trigger="manual"))[
        "outcome"
    ] == "coalesced"

    async with db.immediate() as conn:
        await conn.execute(update(integration_outbox).values(delivered_at=30.0))
    assert (await _classify(db, now=40.0)).verdict == "unsealed"
    assert (await scheduler.mark_due(project_id="p", now=40.0, trigger="manual"))[
        "outcome"
    ] == "coalesced"

    late = 30.0 + UNSEALED_GRACE_SECONDS
    assert (await _classify(db, now=late)).verdict == "stale"
    due = await scheduler.mark_due(project_id="p", now=late, trigger="manual")
    assert due["outcome"] == "due"
    assert due["request_id"] != first["request_id"]


async def test_missing_batch_without_a_sweep_event_is_stale(db):
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(project_id="p", now=0.0, enabled=True, interval_seconds=300)
    await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")
    async with db.immediate() as conn:
        await conn.execute(delete(integration_outbox))
    state = await _classify(db, now=20.0)
    assert state.verdict == "stale"
    assert state.event is None


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


async def test_operator_abort_frees_the_request_and_the_lease(db):
    first = await _wedged(db, "human_blocked")
    await _hold_lease(db)
    await _operation(db, state="human_required")
    service = IntegrationControlService(db, clock=lambda: 50.0)

    aborted = await service.abort("repair-batch-1", reason="operator retired the train")

    assert aborted["outcome"] == "aborted"
    assert aborted["release"]["outcome"] == "cleared"
    assert aborted["release"]["request_id"] == first["request_id"]
    assert aborted["release"]["release"]["lease_released"] is True
    assert (await _schedule(db))["outstanding_request_id"] is None
    assert await _lease(db) is None
    [released] = await _released_events(db)
    assert released["released_by"] == "integration_abort"
    assert released["reason"] == "operator retired the train"


async def test_control_is_dry_run_first_and_compares_the_request(db):
    first = await _wedged(db)
    service = IntegrationControlService(db, clock=lambda: 50.0)

    preview = await service.clear_stale_request(
        "p", dry_run=True, expected_request_id=None, reason=None, operator_id="human"
    )
    assert preview["outcome"] == "would_clear"
    assert preview["state"] == "stale"
    assert preview["request_id"] == first["request_id"]
    assert preview["evidence"]["lifecycle"] == "aborted"
    assert (await _schedule(db))["outstanding_request_id"] == first["request_id"]

    moved = await service.clear_stale_request(
        "p",
        dry_run=False,
        expected_request_id="integration-sweep:p:9",
        reason="incident",
        operator_id="human",
    )
    assert moved["outcome"] == "changed"
    assert (await _schedule(db))["outstanding_request_id"] == first["request_id"]

    cleared = await service.clear_stale_request(
        "p",
        dry_run=False,
        expected_request_id=first["request_id"],
        reason="incident fleet-apex",
        operator_id="supervisor session:s",
    )
    assert cleared["outcome"] == "cleared"
    assert cleared["release"]["outstanding_request_id"] is None
    [released] = await _released_events(db)
    assert released["released_by"] == "supervisor session:s"
    assert released["reason"] == "incident fleet-apex"

    again = await service.clear_stale_request(
        "p", dry_run=True, expected_request_id=None, reason=None, operator_id="human"
    )
    assert (again["outcome"], again["state"]) == ("nothing_to_clear", "none")
    missing = await service.clear_stale_request(
        "nope", dry_run=True, expected_request_id=None, reason=None, operator_id="human"
    )
    assert missing["outcome"] == "not_found"


async def test_control_frees_unsealed_but_refuses_blocked_and_active(db):
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(project_id="p", now=0.0, enabled=True, interval_seconds=300)
    first = await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")
    async with db.immediate() as conn:
        await conn.execute(update(integration_outbox).values(delivered_at=15.0))
    service = IntegrationControlService(db, clock=lambda: 20.0)

    unsealed = await service.clear_stale_request(
        "p",
        dry_run=False,
        expected_request_id=first["request_id"],
        reason="seal failed",
        operator_id="human",
    )
    assert (unsealed["outcome"], unsealed["state"]) == ("cleared", "unsealed")

    second = await scheduler.mark_due(project_id="p", now=30.0, trigger="manual")
    await _batch(db, second["request_id"], "sealed", batch_id="batch-2")
    active = await service.clear_stale_request(
        "p",
        dry_run=False,
        expected_request_id=second["request_id"],
        reason="no",
        operator_id="human",
    )
    assert (active["outcome"], active["state"]) == ("nothing_to_clear", "active")

    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).values(lifecycle="aborted"))
    await _operation(db, state="active", batch_id="batch-2")
    blocked = await service.clear_stale_request(
        "p",
        dry_run=False,
        expected_request_id=second["request_id"],
        reason="no",
        operator_id="human",
    )
    assert blocked["outcome"] == "blocked"
    assert blocked["blockers"][0]["code"] == "live_operation"
    assert (await _schedule(db))["outstanding_request_id"] == second["request_id"]


def _session(session_id: str, project_id: str) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id=session_id,
        project_id=project_id,
        elevated=False,
    )


async def test_command_validates_apply_and_refuses_non_operators(db):
    first = await _wedged(db)
    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.orchestrator = SimpleNamespace(
        integration_control_service=IntegrationControlService(db, clock=lambda: 50.0)
    )

    preview = await handler._cmd_integration_clear_stale_request({"project_id": "p"})
    assert (preview["success"], preview["outcome"], preview["dry_run"]) == (
        True, "would_clear", True,
    )
    unaudited = await handler._cmd_integration_clear_stale_request(
        {"project_id": "p", "dry_run": False, "expected_request_id": first["request_id"]}
    )
    assert (unaudited["success"], unaudited["outcome"]) == (False, "invalid")
    with principal_context(_session("worker", "p")):
        refused = await handler._cmd_integration_clear_stale_request({"project_id": "p"})
    assert refused["outcome"] == "unauthorized"

    applied = await handler._cmd_integration_clear_stale_request(
        {
            "project_id": "p",
            "dry_run": False,
            "expected_request_id": first["request_id"],
            "reason": "incident",
        }
    )
    assert (applied["success"], applied["outcome"]) == (True, "cleared")
    [released] = await _released_events(db)
    assert released["released_by"] == "human:local-operator"


async def test_doctor_reports_and_fixes_only_stale_schedules(db):
    check = next(c for c in CHECKS if c.id == "integration.stale_schedule")
    ctx = DoctorContext(config=None, db=db, handler=None)
    assert (await check.run(ctx)).severity == Severity.OK

    first = await _wedged(db)
    reported = await check.run(ctx)
    assert reported.severity == Severity.WARN
    assert reported.fixable is True
    [finding] = reported.data["schedules"]
    assert (finding["project_id"], finding["request_id"], finding["verdict"]) == (
        "p", first["request_id"], "stale",
    )
    assert finding["lifecycle"] == "aborted"
    assert "coalesced" in reported.detail
    assert (await _schedule(db))["outstanding_request_id"] == first["request_id"]

    fixed = await check.fix(ctx)
    assert fixed.severity == Severity.OK
    assert fixed.fix_applied is True
    assert fixed.data["cleared"][0]["request_id"] == first["request_id"]
    assert (await _released_events(db))[0]["released_by"] == "doctor"
    assert (await check.run(ctx)).severity == Severity.OK


async def test_doctor_reports_an_undeliverable_sweep_without_fixing_it(db):
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(project_id="p", now=0.0, enabled=True, interval_seconds=300)
    await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")
    check = next(c for c in CHECKS if c.id == "integration.stale_schedule")
    ctx = DoctorContext(config=None, db=db, handler=None)
    assert (await check.run(ctx)).severity == Severity.OK

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_outbox).values(
                attempts=4, last_error="no enabled matching playbook durably accepted"
            )
        )
    reported = await check.run(ctx)
    assert reported.severity == Severity.WARN
    assert reported.fixable is False
    assert reported.data["schedules"][0]["verdict"] == "in_flight"


async def test_cancel_preserving_frees_the_request_of_its_batch(db, tmp_path):
    """The live incident's batch was retired through ``cancel_preserving``."""
    from unittest.mock import AsyncMock

    from src.git.manager import GitManager
    from src.integration.development import DevelopmentIntegration

    first = await _wedged(db, "human_blocked")
    await _hold_lease(db)
    await _operation(db, state="human_required")

    cancelled = await DevelopmentIntegration(
        db,
        data_dir=str(tmp_path),
        git=GitManager(),
        confirm_stopped=AsyncMock(return_value=True),
    ).cancel_preserving("repair-batch-1", reason="retire stalled zero-attempt batch")

    assert cancelled["outcome"] == "cancelled"
    assert cancelled["release"]["outcome"] == "cleared"
    assert cancelled["release"]["request_id"] == first["request_id"]
    assert (await _schedule(db))["outstanding_request_id"] is None
    assert await _lease(db) is None
    [released] = await _released_events(db)
    assert released["released_by"] == "integration_cancel_preserving"
