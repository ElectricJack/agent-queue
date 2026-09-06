"""Bounded durable integration reconciliation service."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select

from src.database import Database
from src.database.tables import (
    integration_batches,
    integration_candidate_revisions,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    project_integration_schedules,
    projects,
)
from src.integration.scheduler import IntegrationScheduler
from src.integration.service import IntegrationService
from src.integration.models import HierarchicalIntegrationPolicy


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "integration-service.db"))
    await database.initialize()
    yield database
    await database.close()


async def test_due_schedule_keyset_pages_every_row_once_past_two_hundred(db):
    count = 205
    async with db.immediate() as conn:
        await conn.execute(
            insert(projects),
            [
                {
                    "id": f"p-{ordinal:03d}",
                    "name": f"Project {ordinal}",
                    "status": "ACTIVE",
                    "hierarchical_integration_mode": "train",
                    "created_at": 1.0,
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(project_integration_schedules),
            [
                {
                    "project_id": f"p-{ordinal:03d}",
                    "enabled": True,
                    "interval_seconds": 30,
                    "next_due_at": float(ordinal % 7),
                    "updated_at": 1.0,
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(projects).values(
                id="disabled-project",
                name="Disabled",
                status="ACTIVE",
                hierarchical_integration_mode="disabled",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(project_integration_schedules).values(
                project_id="disabled-project",
                enabled=True,
                interval_seconds=30,
                next_due_at=0.0,
                updated_at=1.0,
            )
        )

    rows: list[dict] = []
    after = None
    while True:
        page = await db.due_integration_schedule_page(now=10.0, after=after, limit=7)
        if not page:
            break
        rows.extend(page)
        after = (page[-1]["next_due_at"], page[-1]["project_id"])

    assert len(rows) == count
    assert len({row["project_id"] for row in rows}) == count
    assert [(row["next_due_at"], row["project_id"]) for row in rows] == sorted(
        (row["next_due_at"], row["project_id"]) for row in rows
    )
    assert "disabled-project" not in {row["project_id"] for row in rows}
    with pytest.raises(ValueError, match="positive"):
        await db.due_integration_schedule_page(now=10.0, after=None, limit=0)


def test_policy_uses_compatible_rebuild_and_cleanup_defaults():
    artifact = {
        "playbook_id": "root-integration",
        "artifact_sha256": "sha256:" + "a" * 64,
        "schema_generation": 2,
        "contract_fingerprint": "sha256:" + "b" * 64,
        "source_digest": "sha256:" + "c" * 64,
        "compiler_build": "test",
        "compiled_at": "2026-09-05T00:00:00Z",
        "version": 1,
    }
    boundary = {
        "required_checks": {"version": "v1", "names": ["unit"], "producer_id": "forge"},
        "repair": {"debug_intelligence_class": "debug-high"},
        "route": {
            "playbook_id": "root-integration",
            "scope": "project",
            "scope_identifier": "p",
            "artifact": artifact,
        },
    }
    values = {
        "parent": boundary,
        "root": boundary,
        "branchless_parent": "verifier",
        "on_failed_child": "block",
    }
    policy = HierarchicalIntegrationPolicy.model_validate(values)

    assert policy.on_main_moved == "rebuild"
    assert policy.cleanup.model_dump() == {
        "max_attempts": 5,
        "retry_base_seconds": 30.0,
        "retry_max_seconds": 3600.0,
    }
    with pytest.raises(ValueError, match="retry_max_seconds"):
        HierarchicalIntegrationPolicy.model_validate(
            {
                **values,
                "cleanup": {"retry_base_seconds": 31.0, "retry_max_seconds": 30.0},
            }
        )


async def test_reconciliation_pages_select_only_current_work_and_keep_intent_kind(db):
    async with db.immediate() as conn:
        for ordinal, lifecycle in enumerate(("testing", "testing", "promoted")):
            batch_id = f"batch-{ordinal}"
            await conn.execute(
                insert(integration_batches).values(
                    id=batch_id,
                    project_id=f"p-{ordinal}",
                    repository_id="repo",
                    request_id=f"request-{ordinal}",
                    source_manifest_digest=f"manifest-{ordinal}",
                    base_sha="a" * 40,
                    lifecycle=lifecycle,
                    current_revision=0,
                    integration_branch=f"refs/heads/integration/{ordinal}",
                    policy_snapshot={},
                    artifact_snapshot={},
                    cleanup_state="pending",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_candidate_revisions).values(
                    batch_id=batch_id,
                    revision=0,
                    construction_base_sha="a" * 40,
                    head_sha=chr(ord("b") + ordinal) * 40,
                    state="testing",
                    created_at=1.0,
                    updated_at=float(ordinal),
                )
            )
            await conn.execute(
                insert(integration_repair_operations).values(
                    id=f"operation-{ordinal}",
                    target_kind="batch",
                    batch_id=batch_id,
                    episode_id=batch_id,
                    active_stage=0,
                    state="active",
                    policy_snapshot={},
                    artifact_snapshot={},
                    required_check_version="v1",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_repair_stages).values(
                    operation_id=f"operation-{ordinal}",
                    ordinal=0,
                    policy={},
                    starting_sha="a" * 40,
                    deadline_at=float(ordinal + 1),
                    state="active" if ordinal < 2 else "passed",
                )
            )

        common = {
            "receipt_id": "receipt",
            "source_head": "b" * 40,
            "source_base": "a" * 40,
            "repository_id": "repo",
            "target_branch": "main",
            "expected_target": "a" * 40,
            "fence_owner_id": "owner",
            "fence_token": 1,
            "state": "prepared",
            "created_at": 1.0,
        }
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="child-intent",
                domain_key="child-domain",
                intent_kind="child",
                updated_at=1.0,
                **common,
            )
        )
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="root-intent",
                domain_key="root-domain",
                intent_kind="root",
                root_batch_id="batch-0",
                root_candidate_revision=0,
                project_id="p-0",
                project_lease_owner_id="owner",
                project_lease_fence_token=1,
                branch_fence_owner_id="owner",
                branch_fence_token=1,
                ci_evidence_id="ci",
                updated_at=2.0,
                **{**common, "target_branch": "release"},
            )
        )

    repairs = await db.due_integration_repair_stage_page(now=10.0, after=None, limit=1)
    assert [(row["operation_id"], row["stage"]) for row in repairs] == [
        ("operation-0", 0)
    ]
    repairs += await db.due_integration_repair_stage_page(
        now=10.0,
        after=(repairs[-1]["deadline_at"], repairs[-1]["operation_id"], 0),
        limit=1,
    )
    assert [row["operation_id"] for row in repairs] == ["operation-0", "operation-1"]

    candidates = await db.pending_candidate_ci_page(after=None, limit=1)
    candidates += await db.pending_candidate_ci_page(
        after=(
            candidates[-1]["updated_at"],
            candidates[-1]["batch_id"],
            candidates[-1]["revision"],
        ),
        limit=2,
    )
    assert [row["batch_id"] for row in candidates] == ["batch-0", "batch-1"]

    intents = await db.unresolved_integration_intent_page(after=None, limit=1)
    intents += await db.unresolved_integration_intent_page(
        after=(intents[-1]["updated_at"], intents[-1]["id"]), limit=1
    )
    assert [(row["id"], row["intent_kind"]) for row in intents] == [
        ("child-intent", "child"),
        ("root-intent", "root"),
    ]


async def test_all_reconciliation_keysets_page_past_two_hundred_rows(db):
    count = 205
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches),
            [
                {
                    "id": f"bulk-batch-{ordinal:03d}",
                    "project_id": f"bulk-project-{ordinal:03d}",
                    "repository_id": "repo",
                    "request_id": f"bulk-request-{ordinal:03d}",
                    "source_manifest_digest": f"bulk-manifest-{ordinal:03d}",
                    "base_sha": "a" * 40,
                    "lifecycle": "testing",
                    "current_revision": 0,
                    "integration_branch": f"refs/heads/integration/bulk-{ordinal:03d}",
                    "policy_snapshot": {},
                    "artifact_snapshot": {},
                    "cleanup_state": "pending",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(integration_candidate_revisions),
            [
                {
                    "batch_id": f"bulk-batch-{ordinal:03d}",
                    "revision": 0,
                    "construction_base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                    "state": "testing",
                    "created_at": 1.0,
                    "updated_at": float(ordinal % 11),
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(integration_repair_operations),
            [
                {
                    "id": f"bulk-operation-{ordinal:03d}",
                    "target_kind": "batch",
                    "batch_id": f"bulk-batch-{ordinal:03d}",
                    "episode_id": f"bulk-batch-{ordinal:03d}",
                    "active_stage": 0,
                    "state": "active",
                    "policy_snapshot": {},
                    "artifact_snapshot": {},
                    "required_check_version": "v1",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(integration_repair_stages),
            [
                {
                    "operation_id": f"bulk-operation-{ordinal:03d}",
                    "ordinal": 0,
                    "policy": {},
                    "starting_sha": "a" * 40,
                    "deadline_at": float(ordinal % 13),
                    "state": "active",
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(integration_promotion_intents),
            [
                {
                    "id": f"bulk-intent-{ordinal:03d}",
                    "domain_key": f"bulk-domain-{ordinal:03d}",
                    "receipt_id": f"bulk-receipt-{ordinal:03d}",
                    "source_head": "b" * 40,
                    "source_base": "a" * 40,
                    "repository_id": "repo",
                    "target_branch": f"refs/heads/bulk-{ordinal:03d}",
                    "expected_target": "a" * 40,
                    "fence_owner_id": "owner",
                    "fence_token": 1,
                    "state": "prepared",
                    "intent_kind": "child",
                    "created_at": 1.0,
                    "updated_at": float(ordinal % 17),
                }
                for ordinal in range(count)
            ],
        )

    repair_rows = []
    after_repair = None
    candidate_rows = []
    after_candidate = None
    intent_rows = []
    after_intent = None
    while True:
        page = await db.due_integration_repair_stage_page(
            now=20.0, after=after_repair, limit=9
        )
        if not page:
            break
        repair_rows.extend(page)
        last = page[-1]
        after_repair = (last["deadline_at"], last["operation_id"], last["stage"])
    while True:
        page = await db.pending_candidate_ci_page(after=after_candidate, limit=9)
        if not page:
            break
        candidate_rows.extend(page)
        last = page[-1]
        after_candidate = (last["updated_at"], last["batch_id"], last["revision"])
    while True:
        page = await db.unresolved_integration_intent_page(after=after_intent, limit=9)
        if not page:
            break
        intent_rows.extend(page)
        last = page[-1]
        after_intent = (last["updated_at"], last["id"])

    assert len({row["operation_id"] for row in repair_rows}) == count
    assert len({row["batch_id"] for row in candidate_rows}) == count
    assert len({row["id"] for row in intent_rows}) == count


async def test_tick_is_bounded_nonoverlapping_and_isolates_sources():
    entered = asyncio.Event()
    release = asyncio.Event()

    class FakeDB:
        async def due_integration_schedule_page(self, **kwargs):
            return [{"project_id": "p", "next_due_at": 1.0}]

        async def due_integration_repair_stage_page(self, **kwargs):
            return [{"operation_id": "op", "stage": 0, "deadline_at": 1.0}]

        async def pending_candidate_ci_page(self, **kwargs):
            return []

        async def unresolved_integration_intent_page(self, **kwargs):
            return []

    scheduler = SimpleNamespace(mark_due=AsyncMock())

    async def expire(*args, **kwargs):
        entered.set()
        await release.wait()

    repair = SimpleNamespace(expire=AsyncMock(side_effect=expire))
    outbox = SimpleNamespace(dispatch_due=AsyncMock(return_value=0))
    service = IntegrationService(FakeDB(), scheduler, repair, outbox, page_size=1)

    first = asyncio.create_task(service.tick(10.0))
    await entered.wait()
    await service.tick(10.0)
    release.set()
    await first

    scheduler.mark_due.assert_awaited_once_with("p", 10.0, "periodic")
    repair.expire.assert_awaited_once_with("op", 0, now=10.0)
    outbox.dispatch_due.assert_awaited_once_with(10.0)


async def test_tick_keeps_work_without_later_phase_handlers_retryable():
    class FakeDB:
        async def due_integration_schedule_page(self, **kwargs):
            return []

        async def due_integration_repair_stage_page(self, **kwargs):
            return []

        async def pending_candidate_ci_page(self, **kwargs):
            return [{"batch_id": "b", "revision": 1, "updated_at": 1.0}]

        async def unresolved_integration_intent_page(self, **kwargs):
            return [{"id": "i", "intent_kind": "root", "updated_at": 1.0}]

    outbox = SimpleNamespace(dispatch_due=AsyncMock(return_value=0))
    service = IntegrationService(
        FakeDB(),
        SimpleNamespace(mark_due=AsyncMock()),
        SimpleNamespace(expire=AsyncMock()),
        outbox,
        page_size=2,
    )

    await service.tick(10.0)

    outbox.dispatch_due.assert_awaited_once_with(10.0)


async def test_tick_isolates_item_failure_but_propagates_cancellation():
    class FakeDB:
        async def due_integration_schedule_page(self, **kwargs):
            return [{"project_id": "p", "next_due_at": 1.0}]

        async def due_integration_repair_stage_page(self, **kwargs):
            return [{"operation_id": "op", "stage": 0, "deadline_at": 1.0}]

        async def pending_candidate_ci_page(self, **kwargs):
            return [{"batch_id": "b", "revision": 0, "updated_at": 1.0}]

        async def unresolved_integration_intent_page(self, **kwargs):
            return []

    repair = SimpleNamespace(expire=AsyncMock())
    outbox = SimpleNamespace(dispatch_due=AsyncMock(return_value=0))
    service = IntegrationService(
        FakeDB(),
        SimpleNamespace(mark_due=AsyncMock(side_effect=RuntimeError("schedule failed"))),
        repair,
        outbox,
        candidate_ci_handler=AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await service.tick(10.0)

    repair.expire.assert_awaited_once_with("op", 0, now=10.0)
    outbox.dispatch_due.assert_not_awaited()


async def test_two_services_coalesce_the_same_durable_schedule(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(projects).values(
                id="p",
                name="Project",
                status="ACTIVE",
                hierarchical_integration_mode="train",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(project_integration_schedules).values(
                project_id="p",
                enabled=True,
                interval_seconds=30,
                next_due_at=1.0,
                updated_at=1.0,
            )
        )

    scheduler = IntegrationScheduler(db)
    entered = 0
    both_entered = asyncio.Event()

    class CoordinatedScheduler:
        async def mark_due(self, project_id, now, trigger):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=1.0)
            return await scheduler.mark_due(project_id, now, trigger)

    empty_repair = SimpleNamespace(expire=AsyncMock())
    empty_outbox = SimpleNamespace(dispatch_due=AsyncMock(return_value=0))
    first = IntegrationService(db, CoordinatedScheduler(), empty_repair, empty_outbox)
    second = IntegrationService(db, CoordinatedScheduler(), empty_repair, empty_outbox)

    await asyncio.gather(first.tick(10.0), second.tick(10.0))

    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(integration_outbox)) == 1


async def test_service_start_and_stop_owns_one_named_loop():
    class EmptyDB:
        async def due_integration_schedule_page(self, **kwargs):
            return []

        async def due_integration_repair_stage_page(self, **kwargs):
            return []

        async def pending_candidate_ci_page(self, **kwargs):
            return []

        async def unresolved_integration_intent_page(self, **kwargs):
            return []

    service = IntegrationService(
        EmptyDB(),
        SimpleNamespace(mark_due=AsyncMock()),
        SimpleNamespace(expire=AsyncMock()),
        SimpleNamespace(dispatch_due=AsyncMock(return_value=0)),
        interval_seconds=60.0,
    )

    service.start()
    first_task = service._task
    service.start()
    assert service._task is first_task
    assert first_task is not None
    assert first_task.get_name() == "integration-reconciliation-service"
    await asyncio.sleep(0)
    await service.stop()
    assert service._task is None
    assert first_task.done()
