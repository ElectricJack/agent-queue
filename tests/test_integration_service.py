"""Bounded durable integration reconciliation service."""

from __future__ import annotations

import asyncio
import json
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select

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
from src.integration import outbox as outbox_module
from src.integration.models import (
    DEFAULT_INTEGRATION_MAX_WAIT_SECONDS,
    HierarchicalIntegrationPolicy,
    integration_max_wait_seconds,
)
from src.integration.scheduler import IntegrationScheduler
from src.integration.service import IntegrationService as _IntegrationService
from src.integration.settling import note_approval


class IntegrationService(_IntegrationService):
    """Existing synthetic-time scenarios explicitly own their operation clock."""

    def __init__(self, *args, **kwargs):
        self._test_now = 0.0
        kwargs.setdefault("clock", lambda: self._test_now)
        super().__init__(*args, **kwargs)

    async def tick(self, now, **kwargs):
        self._test_now = now
        await super().tick(now, **kwargs)


@pytest.fixture
async def db(tmp_path, reuse_database):
    database = await reuse_database("integration-service.db")
    yield database


async def test_tick_retires_terminal_delegates_without_a_new_completion_event(db):
    repair = SimpleNamespace(retire_terminal_delegates=AsyncMock(return_value=[]))
    service = IntegrationService(
        db, SimpleNamespace(), repair, SimpleNamespace(dispatch_due=AsyncMock()),
    )
    await service.tick(100.0)
    repair.retire_terminal_delegates.assert_awaited_once_with(100.0)


async def test_tick_retries_repair_reservations_after_owner_recovery(db):
    calls = []

    async def recovered(now):
        calls.append(("recovery", now))

    async def reserved(now):
        calls.append(("reservation", now))

    service = IntegrationService(
        db, SimpleNamespace(), SimpleNamespace(reconcile_delegate_reservations=reserved),
        SimpleNamespace(dispatch_due=AsyncMock()), owner_recovery_handler=recovered,
    )
    await service.tick(100.0)
    assert calls == [("recovery", 100.0), ("reservation", 100.0)]


async def test_tick_runs_green_continuation_before_outbox_dispatch(db):
    """A continuation enqueued this tick is delivered by the same tick."""
    calls = []

    async def reserved(now):
        calls.append(("reservation", now))

    async def green(now):
        calls.append(("green", now))

    async def dispatch(now):
        calls.append(("outbox", now))

    service = IntegrationService(
        db, SimpleNamespace(), SimpleNamespace(reconcile_delegate_reservations=reserved),
        SimpleNamespace(dispatch_due=dispatch), green_promotion_handler=green,
    )
    await service.tick(100.0)
    assert calls == [("reservation", 100.0), ("green", 100.0), ("outbox", 100.0)]


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
        "successful_source_refs": "delete",
        "failed_work_retention_seconds": 604800,
    }
    with pytest.raises(ValueError, match="retry_max_seconds"):
        HierarchicalIntegrationPolicy.model_validate(
            {
                **values,
                "cleanup": {"retry_base_seconds": 31.0, "retry_max_seconds": 30.0},
            }
        )



def _minimal_policy_values() -> dict:
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
    return {
        "parent": boundary,
        "root": boundary,
        "branchless_parent": "verifier",
        "on_failed_child": "block",
    }


def test_policy_max_wait_defaults_to_one_hour_without_changing_frozen_snapshots():
    values = _minimal_policy_values()
    policy = HierarchicalIntegrationPolicy.model_validate(values)
    dumped = policy.model_dump(mode="json")

    assert DEFAULT_INTEGRATION_MAX_WAIT_SECONDS == 3600.0
    assert outbox_module.DEFAULT_MAX_WAIT_SECONDS == DEFAULT_INTEGRATION_MAX_WAIT_SECONDS
    assert policy.max_wait_seconds == DEFAULT_INTEGRATION_MAX_WAIT_SECONDS
    # Batches, operations and stages compare stored snapshots by dict
    # equality, so a default policy must dump exactly as it did before.
    assert "max_wait_seconds" not in dumped
    assert "max_wait_seconds" not in policy.model_dump()
    assert "max_wait_seconds" not in json.loads(policy.model_dump_json())
    assert HierarchicalIntegrationPolicy.model_validate(dumped).model_dump(mode="json") == dumped
    explicit = HierarchicalIntegrationPolicy.model_validate({**values, "max_wait_seconds": 3600})
    assert explicit == policy
    assert explicit.model_dump(mode="json") == dumped


def test_policy_max_wait_round_trips_a_configured_bound():
    policy = HierarchicalIntegrationPolicy.model_validate(
        {**_minimal_policy_values(), "max_wait_seconds": 120}
    )

    assert policy.max_wait_seconds == 120.0
    dumped = policy.model_dump(mode="json")
    assert dumped["max_wait_seconds"] == 120.0
    assert HierarchicalIntegrationPolicy.model_validate(dumped) == policy


@pytest.mark.parametrize(
    "max_wait", [0, -1, float("inf"), float("-inf"), float("nan"), None, "soon"]
)
def test_policy_max_wait_must_be_finite_and_positive(max_wait):
    with pytest.raises(ValueError, match="max_wait_seconds"):
        HierarchicalIntegrationPolicy.model_validate(
            {**_minimal_policy_values(), "max_wait_seconds": max_wait}
        )


def test_project_max_wait_falls_back_to_the_default_without_a_hierarchical_policy():
    configured = {**_minimal_policy_values(), "max_wait_seconds": 90}

    assert integration_max_wait_seconds(configured) == 90.0
    assert integration_max_wait_seconds(_minimal_policy_values()) == 3600.0
    # Unconfigured and development-mode projects cannot name a bound; a
    # stored policy that no longer validates must not become unbounded.
    assert integration_max_wait_seconds(None) == 3600.0
    assert integration_max_wait_seconds({"validation": "none", "commands": []}) == 3600.0
    assert integration_max_wait_seconds({**configured, "max_wait_seconds": 0}) == 3600.0
    assert integration_max_wait_seconds("not a policy") == 3600.0

@pytest.mark.parametrize(
    ("scope", "scope_identifier"),
    [
        ("system", "not-canonical"),
        ("project", ""),
        ("agent_type", "worker"),
        ("supervisor", "supervisor-p"),
    ],
)
def test_integration_route_rejects_noncanonical_or_unsupported_scope(
    scope, scope_identifier
):
    artifact = {
        "playbook_id": "hierarchical-delivery",
        "artifact_sha256": "sha256:" + "a" * 64,
        "schema_generation": 2,
        "contract_fingerprint": "sha256:" + "b" * 64,
        "source_digest": "sha256:" + "c" * 64,
        "compiler_build": "test",
        "version": 1,
    }

    with pytest.raises(ValueError, match="integration routes"):
        HierarchicalIntegrationPolicy.model_validate(
            {
                "parent": {
                    "required_checks": {
                        "version": "v1",
                        "names": ["unit"],
                        "producer_id": "forge",
                    },
                    "repair": {"debug_intelligence_class": "debug-high"},
                    "route": {
                        "playbook_id": "hierarchical-delivery",
                        "scope": scope,
                        "scope_identifier": scope_identifier,
                        "artifact": artifact,
                    },
                },
                "root": {
                    "required_checks": {
                        "version": "v1",
                        "names": ["unit"],
                        "producer_id": "forge",
                    },
                    "repair": {"debug_intelligence_class": "debug-high"},
                    "route": {
                        "playbook_id": "hierarchical-delivery",
                        "scope": scope,
                        "scope_identifier": scope_identifier,
                        "artifact": artifact,
                    },
                },
                "branchless_parent": "skip",
                "on_failed_child": "block",
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
    drain = AsyncMock(return_value=())
    materialize = AsyncMock(side_effect=RuntimeError("temporary Git outage"))
    collect = AsyncMock()
    service = IntegrationService(
        FakeDB(), scheduler, repair, outbox, drain_handler=drain,
        branch_materialization_handler=materialize, collection_handler=collect, page_size=1
    )

    first = asyncio.create_task(service.tick(10.0))
    await entered.wait()
    await service.tick(10.0)
    release.set()
    await first

    materialize.assert_awaited_once_with(10.0)
    collect.assert_awaited_once_with(10.0)
    scheduler.mark_due.assert_awaited_once_with("p", 10.0, "periodic")
    repair.expire.assert_awaited_once_with("op", 0, now=10.0)
    drain.assert_awaited_once_with(10.0)
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


async def test_tick_observes_candidate_ci_before_expiring_a_due_repair_stage():
    """A conclusive exact CI result at the deadline must get first observation."""
    events = []

    class FakeDB:
        async def due_integration_schedule_page(self, **kwargs):
            return []

        async def due_integration_repair_stage_page(self, **kwargs):
            return [{"operation_id": "op", "stage": 0, "deadline_at": 10.0}]

        async def pending_candidate_ci_page(self, **kwargs):
            return [{"batch_id": "batch", "revision": 1, "updated_at": 1.0}]

        async def unresolved_integration_intent_page(self, **kwargs):
            return []

    async def observe(row, now):
        events.append(("candidate", row["batch_id"], now))

    async def expire(operation_id, stage, *, now):
        events.append(("deadline", operation_id, stage, now))

    service = IntegrationService(
        FakeDB(),
        SimpleNamespace(mark_due=AsyncMock()),
        SimpleNamespace(expire=expire),
        SimpleNamespace(dispatch_due=AsyncMock(return_value=0)),
        candidate_ci_handler=observe,
    )

    await service.tick(10.0)

    assert events == [("candidate", "batch", 10.0), ("deadline", "op", 0, 10.0)]


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

    # Cancellation during CI observation precedes deadline processing.
    repair.expire.assert_not_awaited()
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
        await note_approval(conn, project_id="p", now=1.0)

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

    await asyncio.gather(first.tick(301.0), second.tick(301.0))

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


@pytest.mark.parametrize("handler_kind", ("missing", "declining"))
async def test_tick_advances_persistent_source_cursor_and_wraps_fairly(handler_kind):
    rows = [
        {"batch_id": name, "revision": 0, "updated_at": 1.0}
        for name in ("a", "b", "c", "d", "e")
    ]

    class PagingDB:
        def __init__(self):
            self.scanned = []

        async def due_integration_schedule_page(self, **kwargs):
            return []

        async def due_integration_repair_stage_page(self, **kwargs):
            return []

        async def pending_candidate_ci_page(self, *, after, limit):
            start = 0
            if after is not None:
                start = next(
                    index + 1
                    for index, row in enumerate(rows)
                    if (row["updated_at"], row["batch_id"], row["revision"]) == after
                )
            page = rows[start : start + limit]
            self.scanned.append([row["batch_id"] for row in page])
            return page

        async def unresolved_integration_intent_page(self, **kwargs):
            return []

    database = PagingDB()
    handler = None if handler_kind == "missing" else AsyncMock(return_value=False)
    service = IntegrationService(
        database,
        SimpleNamespace(mark_due=AsyncMock()),
        SimpleNamespace(expire=AsyncMock()),
        SimpleNamespace(dispatch_due=AsyncMock(return_value=0)),
        candidate_ci_handler=handler,
        page_size=2,
    )

    for now in (1.0, 2.0, 3.0, 4.0):
        await service.tick(now)

    assert database.scanned == [["a", "b"], ["c", "d"], ["e"], [], ["a", "b"]]
    if handler is not None:
        assert [call.args[0]["batch_id"] for call in handler.await_args_list] == [
            "a",
            "b",
            "c",
            "d",
            "e",
            "a",
            "b",
        ]


async def test_selector_failure_isolates_source_and_background_loop_recovers_cleanly():
    two_outbox_ticks = asyncio.Event()

    class OneShotFailureDB:
        def __init__(self):
            self.schedule_calls = 0

        async def due_integration_schedule_page(self, **kwargs):
            self.schedule_calls += 1
            if self.schedule_calls == 1:
                raise RuntimeError("one-shot schedule selector failure")
            return [{"project_id": "p", "next_due_at": 1.0}]

        async def due_integration_repair_stage_page(self, **kwargs):
            return [{"operation_id": "op", "stage": 0, "deadline_at": 1.0}]

        async def pending_candidate_ci_page(self, **kwargs):
            return []

        async def unresolved_integration_intent_page(self, **kwargs):
            return []

    outbox_calls = 0

    async def dispatch_due(now):
        nonlocal outbox_calls
        outbox_calls += 1
        if outbox_calls == 2:
            two_outbox_ticks.set()
        return 0

    scheduler = SimpleNamespace(mark_due=AsyncMock())
    repair = SimpleNamespace(expire=AsyncMock())
    service = IntegrationService(
        OneShotFailureDB(),
        scheduler,
        repair,
        SimpleNamespace(dispatch_due=AsyncMock(side_effect=dispatch_due)),
        interval_seconds=0.01,
    )

    service.start()
    await asyncio.wait_for(two_outbox_ticks.wait(), timeout=1.0)
    await service.stop()

    assert repair.expire.await_count == 2
    assert scheduler.mark_due.await_count == 1
    assert scheduler.mark_due.await_args.args[0] == "p"
    assert scheduler.mark_due.await_args.args[2] == "periodic"
    assert outbox_calls == 2


async def test_each_item_observes_clock_after_prior_slow_handler():
    now = [10.0]
    observed = []
    service = IntegrationService(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        clock=lambda: now[0],
    )

    async def slow(row, observed_at):
        observed.append((row["project_id"], observed_at))
        now[0] += 180

    await service._run_optional(
        "candidate CI", [{"project_id": "p"}, {"project_id": "q"}], slow, 10,
    )
    assert observed == [("p", 10), ("q", 190)]


# --- Bounded sources and items (phase-0 item 8) -----------------------------


class _QuietDB:
    async def due_integration_schedule_page(self, **kwargs):
        return []

    async def due_integration_repair_stage_page(self, **kwargs):
        return []

    async def pending_candidate_ci_page(self, **kwargs):
        return []

    async def unresolved_integration_intent_page(self, **kwargs):
        return []


async def test_hung_source_is_cancelled_and_every_other_source_still_runs():
    """One hung GitHub call no longer stops CI, deadlines, intents and drains."""
    cancelled = asyncio.Event()

    async def hung_review_poll(now):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    collect = AsyncMock()
    drain = AsyncMock()
    outbox = SimpleNamespace(dispatch_due=AsyncMock(return_value=0))
    service = IntegrationService(
        _QuietDB(), SimpleNamespace(mark_due=AsyncMock()), SimpleNamespace(), outbox,
        review_handler=hung_review_poll, collection_handler=collect, drain_handler=drain,
        source_timeouts={"GitHub PR reviews": 0.05},
    )

    await asyncio.wait_for(service.tick(10.0), timeout=5.0)

    assert cancelled.is_set()
    collect.assert_awaited_once_with(10.0)
    drain.assert_awaited_once_with(10.0)
    outbox.dispatch_due.assert_awaited_once_with(10.0)


async def test_tick_level_hung_outbox_is_bounded_too():
    async def hung_dispatch(now):
        await asyncio.Event().wait()

    scheduler = SimpleNamespace(mark_due=AsyncMock())
    service = IntegrationService(
        _QuietDB(), scheduler, SimpleNamespace(),
        SimpleNamespace(dispatch_due=hung_dispatch),
        source_timeouts={"integration outbox": 0.05},
    )
    await asyncio.wait_for(service.tick(10.0), timeout=5.0)
    # The next tick is not refused by a lock the hung call still holds.
    await asyncio.wait_for(service.tick(11.0), timeout=5.0)
    assert not service._tick_lock.locked()


async def test_uncancellable_source_is_skipped_until_it_unwinds(monkeypatch):
    """A call that ignores cancellation never runs twice and never holds the pass."""
    import src.integration.service as service_module

    monkeypatch.setattr(service_module, "CANCEL_GRACE_SECONDS", 0.01)
    release = asyncio.Event()
    calls = 0

    async def stubborn(now):
        nonlocal calls
        calls += 1
        while True:
            try:
                await release.wait()
                return
            except asyncio.CancelledError:
                continue  # swallow every cancellation until released

    collect = AsyncMock()
    service = IntegrationService(
        _QuietDB(), SimpleNamespace(mark_due=AsyncMock()), SimpleNamespace(),
        SimpleNamespace(dispatch_due=AsyncMock(return_value=0)),
        review_handler=stubborn, collection_handler=collect,
        source_timeouts={"GitHub PR reviews": 0.02},
    )

    await asyncio.wait_for(service.tick(1.0), timeout=5.0)
    await asyncio.wait_for(service.tick(2.0), timeout=5.0)
    assert calls == 1  # still unwinding: skipped, not started a second time
    assert collect.await_count == 2  # later sources ran on both passes

    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.wait_for(service.tick(3.0), timeout=5.0)
    assert calls == 2
    assert not service._unwinding


async def test_hung_item_is_cancelled_and_later_items_of_the_page_still_run():
    seen = []

    class PageDB(_QuietDB):
        async def pending_candidate_ci_page(self, **kwargs):
            return [
                {"batch_id": name, "revision": 0, "updated_at": 1.0} for name in ("a", "b")
            ]

    async def observe(row, now):
        if row["batch_id"] == "a":
            await asyncio.Event().wait()
        seen.append(row["batch_id"])

    expire = AsyncMock()
    service = IntegrationService(
        PageDB(), SimpleNamespace(mark_due=AsyncMock()), SimpleNamespace(expire=expire),
        SimpleNamespace(dispatch_due=AsyncMock(return_value=0)),
        candidate_ci_handler=observe, item_timeout_seconds=0.05,
    )
    await asyncio.wait_for(service.tick(10.0), timeout=5.0)
    assert seen == ["b"]


async def test_stop_cancels_a_running_bounded_source():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(now):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service = IntegrationService(
        _QuietDB(), SimpleNamespace(mark_due=AsyncMock()), SimpleNamespace(),
        SimpleNamespace(dispatch_due=AsyncMock(return_value=0)),
        review_handler=slow, interval_seconds=60.0,
    )
    service.start()
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    await asyncio.wait_for(service.stop(), timeout=5.0)
    assert cancelled.is_set()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_timeout_seconds": 0},
        {"item_timeout_seconds": -1},
        {"source_timeouts": {"GitHub PR reviews": 0}},
        {"source_timeouts": {"GitHub PR reviews": True}},
    ],
)
def test_service_refuses_nonpositive_budgets(kwargs):
    with pytest.raises(ValueError, match="timeout must be positive"):
        _IntegrationService(None, None, None, None, **kwargs)


def test_integration_config_validates_and_loads_service_budgets(tmp_path):
    from src.config import IntegrationConfig, load_config

    defaults = IntegrationConfig()
    assert defaults.service_source_timeout_seconds == 300.0
    assert defaults.service_item_timeout_seconds == 60.0
    assert defaults.service_source_timeouts == {}
    assert defaults.validate() == []
    bad = IntegrationConfig(
        service_source_timeout_seconds=0,
        service_item_timeout_seconds="1",
        service_source_timeouts={"": 5},
    )
    assert {error.field for error in bad.validate()} == {
        "service_source_timeout_seconds",
        "service_item_timeout_seconds",
        "service_source_timeouts",
    }

    path = tmp_path / "config.yaml"
    path.write_text(
        "discord:\n  bot_token: t\n  guild_id: '1'\n"
        "database:\n  url: postgresql+asyncpg://test:test@localhost/test\n"
        "integration:\n"
        "  service_source_timeout_seconds: 120\n"
        "  service_item_timeout_seconds: 30\n"
        "  service_source_timeouts:\n"
        "    GitHub PR reviews: 600\n"
    )
    loaded = load_config(str(path)).integration
    assert loaded.service_source_timeout_seconds == 120
    assert loaded.service_item_timeout_seconds == 30
    assert loaded.service_source_timeouts == {"GitHub PR reviews": 600}


# --- Refused repair dispatch retried from durable state (phase-0 item 3) ----


class _DispatchDB(_QuietDB):
    def __init__(self, rows):
        self.rows = rows

    async def refused_integration_repair_dispatch_page(self, *, after, limit):
        rows = [
            row for row in self.rows
            if after is None or (row["operation_id"], row["ordinal"]) > tuple(after)
        ]
        return [dict(row) for row in rows[:limit]]


def _dispatch_service(db, dispatcher, clock, **kwargs):
    return _IntegrationService(
        db, SimpleNamespace(mark_due=AsyncMock()), kwargs.pop("repair", SimpleNamespace()),
        SimpleNamespace(dispatch_due=AsyncMock(return_value=0)),
        repair_dispatcher=dispatcher, clock=clock, **kwargs,
    )


async def test_refused_dispatch_is_retried_with_backoff_until_it_dispatches():
    """busy, stale and unknown refusals are revisited without any new event."""
    import src.integration.service as service_module

    row = {
        "operation_id": "op", "ordinal": 1, "repair_task_id": None,
        "writer_kind": None, "task_status": None,
    }
    db = _DispatchDB([row])
    answers = iter(["busy", "stale", "unknown", "dispatched"])
    attempts = []
    now = [100.0]

    async def dispatch(selected):
        attempts.append(now[0])
        outcome = next(answers)
        if outcome == "dispatched":
            db.rows = []  # the launched writer leaves the selection
        return {"success": outcome == "dispatched", "outcome": outcome}

    service = _dispatch_service(db, dispatch, lambda: now[0])
    base = service_module.DISPATCH_RETRY_BASE_SECONDS
    for at in (100.0, 110.0, 100.0 + base, 100.0 + base + 1, 100.0 + 3 * base,
               100.0 + 7 * base, 100.0 + 8 * base):
        now[0] = at
        await service.tick(at)

    assert attempts == [100.0, 100.0 + base, 100.0 + 3 * base, 100.0 + 7 * base]
    assert ("op", 1) not in service._dispatch_backoff


async def test_dispatch_backoff_is_capped_and_resets_when_the_writer_changes():
    import src.integration.service as service_module

    row = {
        "operation_id": "op", "ordinal": 0, "repair_task_id": "repair-op-0",
        "writer_kind": "repair_delegate", "task_status": "PAUSED",
    }
    db = _DispatchDB([row])
    attempts = []
    now = [0.0]

    async def dispatch(selected):
        attempts.append((now[0], selected["repair_task_id"]))
        return {"success": False, "outcome": "busy"}

    service = _dispatch_service(db, dispatch, lambda: now[0])
    ceiling = service_module.DISPATCH_RETRY_MAX_SECONDS
    for _ in range(400):
        await service.tick(now[0])
        now[0] += 10.0
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(attempts)]
    assert max(gaps) <= ceiling + 10.0 and gaps[-1] >= ceiling
    count = len(attempts)

    db.rows = [{**row, "repair_task_id": "repair-op-0-r1"}]  # a refiled delegate
    await service.tick(now[0])
    assert len(attempts) == count + 1 and attempts[-1][1] == "repair-op-0-r1"


async def test_refused_dispatch_that_links_its_first_delegate_keeps_its_pacing():
    """Dispatch links a PAUSED delegate before ownership refuses it; that is not progress."""
    row = {
        "operation_id": "op", "ordinal": 1, "repair_task_id": None,
        "writer_kind": None, "task_status": None,
    }
    db = _DispatchDB([row])
    attempts = []
    now = [0.0]

    async def dispatch(selected):
        attempts.append(now[0])
        db.rows = [{**row, "repair_task_id": "repair-op-1", "writer_kind": "repair_delegate",
                    "task_status": "PAUSED"}]
        return {"success": False, "outcome": "busy"}

    service = _dispatch_service(db, dispatch, lambda: now[0])
    for at in (0.0, 1.0, 2.0):
        now[0] = at
        await service.tick(at)
    assert attempts == [0.0]


async def test_repair_continuations_and_refusals_share_one_paced_dispatch():
    """pending_dispatches keeps its semantics; a stage selected twice dispatches once."""
    shared = {
        "operation_id": "op-a", "ordinal": 2, "repair_task_id": None,
        "writer_kind": None, "task_status": None,
    }
    db = _DispatchDB([shared])
    pending = AsyncMock(return_value=[
        {"operation_id": "op-a", "ordinal": 2},
        {"operation_id": "op-b", "ordinal": 0},
    ])
    dispatched = []

    async def dispatch(selected):
        dispatched.append((selected["operation_id"], selected["ordinal"]))
        return {"success": True, "outcome": "already_dispatched"}

    service = _dispatch_service(
        db, dispatch, lambda: 50.0, repair=SimpleNamespace(pending_dispatches=pending),
    )
    await service.tick(50.0)
    await service.tick(51.0)  # both still selected, both paced

    assert sorted(dispatched) == [("op-a", 2), ("op-b", 0)]
    pending.assert_awaited()


async def test_failed_or_hung_dispatch_counts_as_an_attempt_and_is_retried():
    row = {
        "operation_id": "op", "ordinal": 1, "repair_task_id": None,
        "writer_kind": None, "task_status": None,
    }
    calls = []
    now = [0.0]

    async def dispatch(selected):
        calls.append(now[0])
        if len(calls) == 1:
            raise RuntimeError("dispatch transaction failed")
        if len(calls) == 2:
            await asyncio.Event().wait()
        return {"success": True, "outcome": "dispatched"}

    service = _dispatch_service(
        _DispatchDB([row]), dispatch, lambda: now[0], item_timeout_seconds=0.05
    )
    for at in (0.0, 30.0, 90.0):
        now[0] = at
        await asyncio.wait_for(service.tick(at), timeout=5.0)
    assert calls == [0.0, 30.0, 90.0]


async def test_refused_dispatch_page_selects_unlaunched_writers_under_any_policy(db):
    from src.database.tables import task_metadata
    from src.models import Project, Task, TaskStatus

    await db.create_project(Project(id="p", name="Project"))
    cases = {
        # operation: (operation state, stage, stage state, writer, task status, held, revision)
        "op-none": ("active", 1, "active", None, None, False, "testing"),
        "op-escalated": ("escalated", 1, "active", None, None, False, "testing"),
        "op-paused": ("active", 1, "active", "repair_delegate", "PAUSED", False, "testing"),
        "op-ready": ("active", 1, "active", "repair_delegate", "READY", False, "testing"),
        "op-held": ("active", 1, "active", "repair_delegate", "PAUSED", True, "testing"),
        "op-verifier": ("active", 1, "active", "existing_verifier", "PAUSED", False, "testing"),
        "op-human": ("human_required", 1, "active", None, None, False, "testing"),
        "op-recovery": ("escalated", 1, "expired", None, None, False, "testing"),
        # A built stage-0 candidate still publishing belongs to its collector.
        "op-publishing": ("active", 0, "active", None, None, False, "built"),
    }
    for operation_id, case in cases.items():
        op_state, ordinal, stage_state, writer_kind, status, held, revision_state = case
        batch_id = f"batch-{operation_id}"
        task_id = None
        if status is not None:
            task_id = f"task-{operation_id}"
            await db.create_task(Task(
                id=task_id, project_id="p", title="Repair", description="",
                status=TaskStatus(status),
            ))
        async with db.immediate() as conn:
            if held:
                await conn.execute(insert(task_metadata).values(
                    task_id=task_id, key="manual_pause", value="{}"
                ))
            await conn.execute(insert(integration_batches).values(
                id=batch_id, project_id=f"p-{operation_id}", repository_id="repo",
                request_id=f"request-{operation_id}", source_manifest_digest="manifest",
                base_sha="a" * 40, lifecycle="repairing", current_revision=0,
                integration_branch=f"aq/integration/{operation_id}", policy_snapshot={},
                artifact_snapshot={}, cleanup_state="pending", created_at=1.0, updated_at=1.0,
            ))
            await conn.execute(insert(integration_candidate_revisions).values(
                batch_id=batch_id, revision=0, construction_base_sha="a" * 40,
                head_sha="b" * 40, state=revision_state, created_at=1.0, updated_at=1.0,
            ))
            await conn.execute(insert(integration_repair_operations).values(
                id=operation_id, target_kind="batch", batch_id=batch_id, episode_id=batch_id,
                active_stage=ordinal, state=op_state, policy_snapshot={},
                artifact_snapshot={}, required_check_version="v1",
                created_at=1.0, updated_at=1.0,
            ))
            await conn.execute(insert(integration_repair_stages).values(
                operation_id=operation_id, ordinal=ordinal,
                policy={"on_exhausted": "human"}, starting_sha="a" * 40,
                repair_task_id=task_id, writer_kind=writer_kind, state=stage_state,
            ))

    first = await db.refused_integration_repair_dispatch_page(after=None, limit=2)
    rest = await db.refused_integration_repair_dispatch_page(
        after=(first[-1]["operation_id"], first[-1]["ordinal"]), limit=10
    )
    selected = {row["operation_id"]: row for row in first + rest}
    assert sorted(selected) == ["op-escalated", "op-none", "op-paused"]
    assert selected["op-paused"]["task_status"] == "PAUSED"
    assert selected["op-paused"]["repair_task_id"] == "task-op-paused"


async def test_intent_pass_routes_parent_intents_to_their_own_reconciler():
    class IntentDB(_QuietDB):
        async def unresolved_integration_intent_page(self, **kwargs):
            return [
                {"id": "child", "intent_kind": "child", "updated_at": 1.0},
                {"id": "root", "intent_kind": "root", "updated_at": 2.0},
            ]

    root = AsyncMock(return_value={"outcome": "applied"})
    parent = AsyncMock(return_value={"outcome": "promoted"})
    service = IntegrationService(
        IntentDB(), SimpleNamespace(mark_due=AsyncMock()), SimpleNamespace(),
        SimpleNamespace(dispatch_due=AsyncMock(return_value=0)),
        unresolved_intent_handler=root, parent_intent_handler=parent,
    )
    await service.tick(10.0)
    assert [call.args[0]["id"] for call in parent.await_args_list] == ["child"]
    assert [call.args[0]["id"] for call in root.await_args_list] == ["root"]

    # Without a parent reconciler every row still reaches the root handler.
    legacy = AsyncMock(return_value={"outcome": "declined"})
    service = IntegrationService(
        IntentDB(), SimpleNamespace(mark_due=AsyncMock()), SimpleNamespace(),
        SimpleNamespace(dispatch_due=AsyncMock(return_value=0)),
        unresolved_intent_handler=legacy,
    )
    await service.tick(10.0)
    assert [call.args[0]["id"] for call in legacy.await_args_list] == ["child", "root"]
