"""Only observed, authorized delivery becomes persistent injected evidence."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from src.commands.principal import TRUSTED_LOCAL
from src.database.tables import (
    knowledge_citations,
    knowledge_context_deliveries,
    sessions,
    task_session_attempts,
    tasks,
)
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError
from tests.test_knowledge_context import context_setup as _context_setup
from tests.test_knowledge_context import prepare

context_setup = _context_setup


async def deliver(service, worker, bundle, key="delivery", state="delivered", **kwargs):
    return await service.observe_delivery(
        principal=worker, claim_epoch=1, bundle_id=bundle.bundle_id,
        transport="test", transport_key=key, state=state,
        rendered_sha256=bundle.content_sha256, **kwargs,
    )


async def citations(db):
    async with db.immediate() as conn:
        return (await conn.execute(select(knowledge_citations))).mappings().all()


async def test_failed_unknown_and_prepared_are_not_injected_then_retry_is_idempotent(context_setup):
    db, _, service, worker, _, created = context_setup
    bundle = await prepare(service, worker)
    for state in ("prepared", "failed", "unknown"):
        await deliver(service, worker, bundle, state=state)
        assert await citations(db) == []
    first = await deliver(service, worker, bundle)
    assert await deliver(service, worker, bundle) == first
    await deliver(service, worker, bundle, key="second-transport")
    assert (await deliver(service, worker, bundle, state="unknown"))["state"] == "delivered"
    rows = await citations(db)
    assert len(rows) == 1
    row = rows[0]
    assert str(row["revision_id"]) == created["revision_id"]
    assert row["task_id"] == worker.task_id and row["attempt_id"] == bundle.owner["attempt_id"]
    assert row["supervisor_session_id"] is None and row["claim_epoch"] == 1
    assert row["kind"] == "injected" and str(row["bundle_id"]) == bundle.bundle_id


async def test_delivery_key_cannot_be_reused_for_another_bundle(context_setup):
    db, _, service, worker, _, _ = context_setup
    first = await prepare(service, worker)
    await deliver(service, worker, first)
    second = await prepare(service, worker, refresh=True)
    with pytest.raises(RecordError, match="record.idempotency_conflict"):
        await deliver(service, worker, second)
    assert len(await citations(db)) == 1


async def test_changed_head_before_delivery_invalidates_discovery(context_setup):
    db, config, service, worker, _, created = context_setup
    bundle = await prepare(service, worker)
    await KnowledgeService(db, config.knowledge).update(
        principal=TRUSTED_LOCAL, project_id="p", identity=f"record:{created['record_id']}",
        if_revision=created["revision_id"], patch={"summary": "Updated evidence"},
        idempotency_key="update",
    )
    with pytest.raises(RecordError, match="context.invalidated"):
        await deliver(service, worker, bundle)
    assert await citations(db) == []


@pytest.mark.parametrize("change", ["claim", "instance", "attempt"])
async def test_slot_reuse_or_attempt_loss_cannot_attribute_to_prior_execution(context_setup, change):
    db, _, service, worker, _, _ = context_setup
    bundle = await prepare(service, worker)
    async with db.immediate() as conn:
        if change == "claim":
            await conn.execute(update(tasks).where(tasks.c.id == worker.task_id)
                               .values(claim_epoch=2))
        elif change == "instance":
            await conn.execute(update(sessions).where(sessions.c.id == worker.session_id)
                               .values(instance_token="replacement-instance"))
        else:
            await conn.execute(update(task_session_attempts).where(
                task_session_attempts.c.id == bundle.owner["attempt_id"],
            ).values(state="stopped", ended_at=2))
    with pytest.raises(RecordError):
        await deliver(service, worker, bundle)
    assert await citations(db) == []
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_context_deliveries)) == 0


async def test_wrong_rendered_digest_has_no_receipt_or_citation(context_setup):
    db, _, service, worker, _, _ = context_setup
    bundle = await prepare(service, worker)
    with pytest.raises(RecordError, match="context.digest_mismatch"):
        await service.observe_delivery(
            principal=worker, claim_epoch=1, bundle_id=bundle.bundle_id,
            transport="test", transport_key="wrong", rendered_sha256="0" * 64,
        )
    assert await citations(db) == []


async def test_tracking_grants_are_enforced_by_core_with_transport_gate_off(context_setup):
    db, _, service, worker, _, created = context_setup
    from src.profiles.capabilities import CapabilityPolicy

    bundle = await prepare(service, worker)
    reader = replace(worker, policy=CapabilityPolicy.from_namespaces(
        aq_commands=["knowledge_show", "knowledge_search"],
    ))
    with pytest.raises(RecordError, match="record.forbidden"):
        await deliver(service, reader, bundle)
    with pytest.raises(RecordError, match="record.forbidden"):
        await service.cite(principal=reader, project_id="p", claim_epoch=1,
                           identity=f"record:{created['record_id']}",
                           revision_id=created["revision_id"], kind="explicit_read",
                           idempotency_key="denied")
    assert await citations(db) == []


async def test_explicit_reads_and_attachments_pin_exact_revision_independent_of_injection(context_setup):
    db, config, service, worker, _, created = context_setup
    config.memory.enabled = False
    config.knowledge.context.enabled = False
    args = dict(principal=worker, project_id="p", identity=f"record:{created['record_id']}",
                revision_id=created["revision_id"], idempotency_key="explicit", claim_epoch=1)
    first = await service.cite(**args, kind="explicit_read")
    assert await service.cite(**args, kind="explicit_read") == first
    await service.cite(**args, kind="attached")
    rows = await citations(db)
    assert {row["kind"] for row in rows} == {"attached", "explicit_read"}
    assert all(row["revision_id"] == UUID(created["revision_id"]) for row in rows)
    assert all(row["bundle_id"] is None for row in rows)
    with pytest.raises(RecordError, match="record.invalid_input"):
        await service.cite(**args, kind="injected")


async def test_supervisor_delivery_belongs_to_exact_supervisor_instance(context_setup):
    db, _, service, _, supervisor, _ = context_setup
    bundle = await prepare(service, supervisor)
    await service.observe_delivery(
        principal=supervisor, bundle_id=bundle.bundle_id, transport="start",
        transport_key="supervisor", rendered_sha256=bundle.content_sha256,
    )
    row = (await citations(db))[0]
    assert row["owner_kind"] == "supervisor_session"
    assert row["supervisor_session_id"] == supervisor.session_id
    assert row["task_id"] is None and row["attempt_id"] is None and row["claim_epoch"] is None
    changed = replace(supervisor, session_instance_token="another-instance")
    with pytest.raises(RecordError):
        await service.observe_delivery(
            principal=changed, bundle_id=bundle.bundle_id, transport="start",
            transport_key="wrong-supervisor", rendered_sha256=bundle.content_sha256,
        )
    assert len(await citations(db)) == 1


@pytest.mark.parametrize("corruption", ["mixed_owner", "wrong_attempt", "wrong_record"])
async def test_schema_rejects_ambiguous_execution_or_mismatched_revision(context_setup, corruption):
    db, _, service, worker, _, _ = context_setup
    bundle = await prepare(service, worker)
    await deliver(service, worker, bundle)
    row = dict((await citations(db))[0])
    row.update(citation_id=uuid4(), idempotency_key="invalid")
    if corruption == "mixed_owner":
        row["supervisor_session_id"] = "another-supervisor"
    elif corruption == "wrong_attempt":
        row["owner_id"] = "another-attempt"
    else:
        row["record_id"] = uuid4()
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(insert(knowledge_citations).values(**row))
    assert len(await citations(db)) == 1
