"""Record commits and isolated at-least-once delivery survive lost workers."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.commands.principal import TRUSTED_LOCAL
from src.config import KnowledgeConfig, KnowledgeFeatureConfig
from src.database.tables import record_consumer_receipts, record_outbox
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError
from src.records.outbox import RecordOutbox
from tests.record_helpers import knowledge_config, seed_project, snapshot


@pytest.fixture
async def db(reuse_database):
    return await reuse_database()


@pytest.fixture
async def setup(db):
    await seed_project(db)
    cfg = knowledge_config(export=KnowledgeFeatureConfig(enabled=True))
    service = KnowledgeService(db, cfg)
    result = await service.create(
        snapshot=snapshot(), principal=TRUSTED_LOCAL, project_id="p", idempotency_key="create"
    )
    return cfg, service, result


async def test_intents_commit_atomically_and_contain_no_private_content(db, setup):
    cfg, service, _ = setup
    with pytest.raises(RuntimeError):
        async with db.immediate() as conn:
            await service.create_on(
                snapshot=snapshot(title="Secret title"),
                principal=TRUSTED_LOCAL,
                project_id="p",
                idempotency_key="rollback",
                conn=conn,
            )
            raise RuntimeError("crash before commit")
    async with db.immediate() as conn:
        rows = (await conn.execute(select(record_outbox))).mappings().all()
    assert len(rows) == 2
    assert {row["destination"] for row in rows} == {"audit", "export"}
    for row in rows:
        assert not {"body", "title", "snapshot", "sources"} & row["payload"].keys()
    assert cfg.export.enabled


async def test_crash_expired_lease_replay_and_fenced_ack(db, setup):
    cfg, _, _ = setup
    now = [datetime.now(UTC)]
    worker = RecordOutbox(db, cfg, clock=lambda: now[0])
    first = await worker.claim_due(limit=50)
    assert len(first) == 2
    assert all(event["lease_until"] == now[0] + timedelta(seconds=30) for event in first)
    assert await worker.claim_due() == []
    now[0] += timedelta(seconds=31)
    replay = await RecordOutbox(db, cfg, clock=lambda: now[0]).claim_due()
    assert {r["event_id"] for r in replay} == {r["event_id"] for r in first}
    for old, new in zip(first, replay, strict=True):
        assert old["lease_token"] != new["lease_token"]
        assert new["attempts"] == 2
        assert not await worker.acknowledge(old, {"state": "old"})
        assert await worker.acknowledge(new, {"state": "processed"})
        assert not await worker.acknowledge(new, {"state": "duplicate"})
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(record_consumer_receipts)) == 2


async def test_two_connections_claim_disjoint_bounded_pages(db, setup):
    cfg, service, _ = setup
    for ordinal in range(3):
        await service.create(
            snapshot=snapshot(),
            principal=TRUSTED_LOCAL,
            project_id="p",
            idempotency_key=str(ordinal),
        )
    left, right = await asyncio.gather(
        RecordOutbox(db, cfg).claim_due(limit=4), RecordOutbox(db, cfg).claim_due(limit=4)
    )
    assert len(left) + len(right) == 8
    assert not {r["event_id"] for r in left} & {r["event_id"] for r in right}


async def test_destination_failure_does_not_block_audit(db, setup):
    cfg, _, _ = setup

    async def fail(event):
        raise RecordError("record.export_filesystem", "private path must not be retained")

    worker = RecordOutbox(db, cfg, exporter=SimpleNamespace(deliver=fail))
    await worker.tick()
    async with db.immediate() as conn:
        rows = {r["destination"]: r for r in (await conn.execute(select(record_outbox))).mappings()}
    assert rows["audit"]["delivered_at"] is not None
    assert rows["export"]["delivered_at"] is None
    assert rows["export"]["last_error_code"] == "record.export_filesystem"
    assert rows["export"]["lease_token"] is None


async def test_failed_intents_are_retained_and_explicitly_replayed(db, setup):
    cfg, _, _ = setup
    now = [datetime.now(UTC)]

    async def fail(event):
        raise RuntimeError("private body")

    worker = RecordOutbox(db, cfg, exporter=SimpleNamespace(deliver=fail), clock=lambda: now[0])
    for attempt in range(10):
        await worker.tick()
        async with db.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(record_outbox).where(record_outbox.c.destination == "export")
                    )
                )
                .mappings()
                .one()
            )
        assert row["attempts"] == attempt + 1
        assert row["available_at"] == now[0] + timedelta(seconds=min(300, 2**attempt))
        now[0] = row["available_at"]
    assert await worker.claim_due() == []
    assert (await worker.replay(event_id=row["event_id"]))["eligible"]
    assert await worker.claim_due() == []  # default is dry-run
    await worker.replay(event_id=row["event_id"], dry_run=False)
    assert (await worker.claim_due())[0]["attempts"] == 1
    assert (await worker.replay(event_id=row["event_id"], dry_run=False))["eligible"] is False


async def test_live_configuration_pause_retains_export_intents(db, setup):
    cfg, _, _ = setup
    worker = RecordOutbox(db, lambda: cfg)
    cfg.export.enabled = False
    await worker.tick()
    cfg.enabled = False
    assert await worker.claim_due() == []
    cfg.enabled = True
    cfg.export.enabled = True
    assert [r["destination"] for r in await worker.claim_due()] == ["export"]


async def test_disabled_lifecycle_does_not_open_database_or_plugins():
    class NoDatabase:
        def immediate(self):
            raise AssertionError("disabled record worker acquired database")

    worker = RecordOutbox(NoDatabase(), KnowledgeConfig(), interval=0.001)
    worker.start()
    task = worker._task
    worker.start()
    assert task is worker._task
    await asyncio.sleep(0.01)
    assert not task.done()
    await worker.stop()
    assert worker._task is None


async def test_operator_repair_dispatch_defaults_to_dry_run(command_handler_factory):
    handler = await command_handler_factory()
    await seed_project(handler.db)
    handler.config.knowledge = knowledge_config()
    result = await handler.execute("record_repair", {"operation": "backfill-task-mappings"})
    assert result["success"] and result["dry_run"]
    handler.config.knowledge.writes_enabled = False
    result = await handler.execute(
        "record_repair",
        {
            "operation": "backfill-task-mappings",
            "dry_run": False,
        },
    )
    assert result["success"] and result["dry_run"] is False
    handler.config.knowledge.enabled = False
    result = await handler.execute(
        "record_repair", {"operation": "backfill-task-mappings", "dry_run": False}
    )
    assert result["error_code"] == "knowledge.disabled"


def test_repair_contract_is_closed_and_off_agent_surface():
    from pydantic import ValidationError

    from src.api.scope import AGENT_COMMAND_SET
    from src.commands.contracts.records import RecordRepairArgs
    from src.commands.contracts.registry import CONTRACTS

    assert CONTRACTS.get("record_repair")
    assert "record_repair" not in AGENT_COMMAND_SET
    assert RecordRepairArgs(operation="replay-outbox").dry_run
    with pytest.raises(ValidationError):
        RecordRepairArgs(operation="replay-outbox", destination="/tmp/arbitrary")
