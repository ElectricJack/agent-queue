"""Logical denial, one-way erasure, purge recovery and disposable migration checks."""

import importlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from src.commands.principal import TRUSTED_LOCAL
from src.database.tables import (
    knowledge_redactions,
    knowledge_revision_payloads,
    knowledge_search,
    record_source_artifacts,
)
from src.knowledge.service import KnowledgeService
from src.records.export import RecordExporter, read_managed
from src.records.models import RecordError
from src.records.outbox import RecordOutbox
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal

LOCAL = dict(principal=TRUSTED_LOCAL, project_id="p")


@pytest.fixture
async def service(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    return KnowledgeService(db, knowledge_config())


async def create(service):
    return await service.create(
        snapshot=snapshot(body="Sensitive evidence"), idempotency_key="create", **LOCAL
    )


def redact_args(record, **changes):
    return dict(
        identity=f"record:{record['record_id']}",
        if_revision=record["revision_id"],
        idempotency_key="redact",
        reason_code="sensitive",
        dry_run=False,
        **LOCAL,
        **changes,
    )


async def test_redaction_denies_read_replay_restore_and_duplicate_import_bytes(service):
    original = await create(service)
    result = await service.redact(**redact_args(original))
    assert result["affected_revisions"] == 1
    assert (await service.redact(**redact_args(original)))["outcome"] == "replayed"
    for operation in (
        service.show(identity=f"record:{original['record_id']}", **LOCAL),
        service.create(
            snapshot=snapshot(body="Sensitive evidence"), idempotency_key="create", **LOCAL
        ),
        service.create(
            snapshot=snapshot(body="Sensitive evidence"), idempotency_key="import-again", **LOCAL
        ),
        service.restore(
            identity=f"record:{original['record_id']}",
            revision_id=original["revision_id"],
            if_revision=original["revision_id"],
            reason="Restore",
            idempotency_key="restore",
            **LOCAL,
        ),
    ):
        with pytest.raises(RecordError, match="record.revision_redacted"):
            await operation
    assert (await service.search(**LOCAL))["items"] == []
    history = await service.history(identity=f"record:{original['record_id']}", **LOCAL)
    assert history["revisions"][0]["availability"] == "record.revision_redacted"
    async with service.db.immediate() as conn:
        payload = (await conn.execute(select(knowledge_revision_payloads))).mappings().one()
        assert payload["snapshot"] is None
    with pytest.raises(DBAPIError):
        async with service.db.immediate() as conn:
            await conn.execute(
                update(knowledge_revision_payloads).values(
                    snapshot=snapshot(), redacted_at=None, redaction_id=None
                )
            )


async def test_preview_and_operator_only_exact_token(service):
    record = await create(service)
    preview = await service.redact(**{**redact_args(record), "dry_run": True})
    assert preview["affected_revisions"] == 1
    assert (await service.show(identity=f"record:{record['record_id']}", **LOCAL))["snapshot"]
    supervisor = await worker_principal(service.db, elevated=True, grants=["knowledge_redact"])
    with pytest.raises(RecordError, match="record.forbidden"):
        await service.redact(**{**redact_args(record), "principal": supervisor})
    with pytest.raises(RecordError, match="if_revision is required"):
        await service.redact(
            **{**redact_args(record), "if_revision": None, "idempotency_key": "missing-token"}
        )


async def test_purge_is_priority_replayable_and_runs_when_features_are_disabled(service, tmp_path):
    service.config.export.enabled = True
    record = await create(service)
    exporter = RecordExporter(service.db, service.config, tmp_path)
    outbox = RecordOutbox(service.db, service.config, exporter=exporter)
    events = await outbox.claim_due(limit=10)
    export_event = next(e for e in events if e["destination"] == "export")
    assert await outbox.deliver(export_event)
    assert read_managed(tmp_path, "project:p", record["knowledge_alias"])
    erased = await service.redact(**redact_args(record))
    service.config.enabled = service.config.export.enabled = False
    purges = await outbox.claim_due()
    assert purges and all(e["destination"] == "purge" for e in purges)
    assert await outbox.deliver(purges[0])
    assert read_managed(tmp_path, "project:p", record["knowledge_alias"]) is None
    assert not await outbox.deliver(export_event)
    async with service.db.immediate() as conn:
        ledger = (
            (
                await conn.execute(
                    select(knowledge_redactions).where(
                        knowledge_redactions.c.redaction_id == UUID(erased["redaction_id"]),
                    )
                )
            )
            .mappings()
            .one()
        )
        assert ledger["completed_at"]
    assert not (await outbox.replay(event_id=export_event["event_id"], dry_run=False))["eligible"]


async def test_redaction_scrubs_derived_link_revisions_proposals_and_artifacts(service, tmp_path):
    aid = uuid4()
    (tmp_path / "record-artifacts").mkdir()
    (tmp_path / "record-artifacts" / str(aid)).write_text("secret")
    async with service.db.immediate() as conn:
        await service.db.ensure_record_scope_on(project_id="p", conn=conn)
        await conn.execute(
            insert(record_source_artifacts).values(
                artifact_id=aid,
                scope_key="project:p",
                content_sha256="a" * 64,
                byte_size=6,
                media_type="text/plain",
                storage_key=f"record-artifacts/{aid}",
            )
        )
    root = await service.create(
        snapshot=snapshot(
            sources=[
                dict(source_id="secret", kind="artifact", artifact_id=str(aid), sha256="a" * 64)
            ]
        ),
        idempotency_key="root",
        **LOCAL,
    )
    source = await service.create(
        snapshot=snapshot(title="Derived"), idempotency_key="source", **LOCAL
    )
    linked = await service.mutate_links(
        identity=f"record:{source['record_id']}",
        operations=[
            dict(
                action="add",
                target=f"record:{root['record_id']}",
                link_type="references",
                metadata={"private.snippet": "secret"},
            )
        ],
        if_revision=source["revision_id"],
        idempotency_key="link",
        **LOCAL,
    )
    proposed = await service.propose(
        identity=f"record:{root['record_id']}",
        if_revision=root["revision_id"],
        snapshot=snapshot(body="copied secret"),
        idempotency_key="proposal",
        **LOCAL,
    )
    erased = await service.redact(**redact_args(root))
    assert erased["affected_revisions"] == 2
    with pytest.raises(RecordError, match="record.revision_redacted"):
        await service.proposal_show(proposal_id=proposed["proposal_id"], **LOCAL)
    with pytest.raises(RecordError, match="record.revision_redacted"):
        await service.show(identity=f"record:{linked['record_id']}", **LOCAL)
    outbox = RecordOutbox(
        service.db, service.config, exporter=RecordExporter(service.db, service.config, tmp_path)
    )
    for event in await outbox.claim_due(limit=10):
        assert await outbox.deliver(event)
    assert not (tmp_path / "record-artifacts" / str(aid)).exists()


async def test_redact_old_revision_preserves_unrelated_current_and_refuses_restore(service):
    original = await create(service)
    current = await service.update(
        identity=f"record:{original['record_id']}",
        patch={"body": "Public correction"},
        if_revision=original["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    await service.redact(**{**redact_args(current), "revision_id": original["revision_id"]})
    assert (await service.show(identity=f"record:{current['record_id']}", **LOCAL))["snapshot"][
        "body"
    ] == "Public correction"
    with pytest.raises(RecordError, match="record.revision_redacted"):
        await service.restore(
            identity=f"record:{current['record_id']}",
            revision_id=original["revision_id"],
            if_revision=current["revision_id"],
            reason="Restore",
            idempotency_key="restore",
            **LOCAL,
        )
    async with service.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_search)) == 1


@pytest.mark.migration
async def test_protection_migration_idempotence_and_nonempty_refusal(service):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = importlib.import_module("migrations.versions.a00000000059_knowledge_protection")

    def run(conn, method):
        with Operations.context(MigrationContext.configure(conn)):
            getattr(migration, method)()

    async with service.db.immediate() as conn:
        await conn.run_sync(lambda sync: run(sync, "downgrade"))
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE tgname = 'tr_knowledge_global_shares_scope_v1'"
                )
            )
            == 1
        )
    await create(service)
    with pytest.raises(RuntimeError, match="read-only rollback"):
        async with service.db.immediate() as conn:
            await conn.run_sync(lambda sync: run(sync, "downgrade"))


async def test_redaction_scrubs_historical_link_versions_after_source_edit(service):
    root = await create(service)
    source = await service.create(
        snapshot=snapshot(title="Citation"), idempotency_key="source", **LOCAL
    )
    linked = await service.mutate_links(
        identity=f"record:{source['record_id']}",
        operations=[
            dict(
                action="add",
                target=f"record:{root['record_id']}",
                link_type="references",
                metadata={"private.snippet": "sensitive"},
            )
        ],
        if_revision=source["revision_id"],
        idempotency_key="link",
        **LOCAL,
    )
    await service.update(
        identity=f"record:{source['record_id']}",
        patch={"title": "New citation title"},
        if_revision=linked["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    erased = await service.redact(**redact_args(root))
    assert erased["affected_revisions"] == 3
    from src.database.tables import record_link_versions

    async with service.db.immediate() as conn:
        assert (await conn.execute(select(record_link_versions.c.metadata))).scalar_one() == {}


async def test_purge_removes_crash_left_staged_bytes(service, tmp_path):
    from src.records.export import ManagedFile

    root = await create(service)
    managed = ManagedFile(tmp_path, "project:p", root["knowledge_alias"])
    managed.stage(b"private staged bytes")
    staged = managed.temp_name
    managed.temp_name = None  # simulate a crashed process that never removed the file
    managed.close()
    assert (tmp_path / "projects/p/knowledge/records" / staged).exists()
    await service.redact(**redact_args(root))
    outbox = RecordOutbox(
        service.db, service.config, exporter=RecordExporter(service.db, service.config, tmp_path)
    )
    for event in await outbox.claim_due(limit=10):
        assert await outbox.deliver(event)
    assert not (tmp_path / "projects/p/knowledge/records" / staged).exists()


async def test_accept_racing_erasure_never_resurrects_a_redacted_base(service):
    import asyncio

    root = await create(service)
    proposed = await service.propose(
        identity=f"record:{root['record_id']}",
        if_revision=root["revision_id"],
        snapshot=snapshot(body="correction"),
        idempotency_key="proposal",
        **LOCAL,
    )
    results = await asyncio.gather(
        service.redact(**redact_args(root)),
        service.proposal_decide(
            proposal_id=proposed["proposal_id"],
            proposal_sha256=proposed["proposal_sha256"],
            if_revision=root["revision_id"],
            decision="accept",
            reason="Reviewed",
            idempotency_key="accept",
            **LOCAL,
        ),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, RecordError)]
    assert len(errors) == 1
    assert errors[0].code in {"record.revision_conflict", "record.revision_redacted"}
    if isinstance(results[0], dict):
        with pytest.raises(RecordError, match="record.revision_redacted"):
            await service.show(identity=f"record:{root['record_id']}", **LOCAL)
