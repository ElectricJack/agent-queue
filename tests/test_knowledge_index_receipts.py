"""Derived index receipts, the chunk manifest and local provider registration."""

from contextlib import contextmanager

import pytest
from sqlalchemy import delete as sql_delete

from src.commands.principal import TRUSTED_LOCAL
from src.config import MemoryConfig
from src.database.tables import record_index_state
from src.knowledge.index_receipts import (
    DerivedIndexReceipts,
    index_state_for_provider,
    manifest_digest,
    provider_lag_counts,
    validate_manifest,
)
from src.knowledge.registration import RetrievalProviderRegistry
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal

LOCAL = {"principal": TRUSTED_LOCAL, "project_id": "p"}
BODY = "Alpha evidence paragraph.\nBeta evidence paragraph.\n"
CORRECTED = "Corrected evidence paragraph.\nSecond paragraph.\n"
RECORD = {
    "record_id": "11111111-1111-4111-8111-111111111111",
    "revision_id": "22222222-2222-4222-8222-222222222222",
}
HASH = "a" * 64


class FakeIndexProvider:
    """A provider that only ranks; it never initializes anything."""

    provider_id = "offline-provider"
    provider_version = "v1"
    deprecated = False
    indexer = None

    def __init__(self, *, available=True):
        self.available = available
        self.calls = 0

    async def search(self, request):
        self.calls += 1
        return []


@pytest.fixture
async def service(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    config = knowledge_config(global_enabled=True)
    config.semantic.enabled = True
    return KnowledgeService(db, config)


def registry_for(provider):
    return RetrievalProviderRegistry(lambda name: provider)


def receipts(service, provider=None, *, memory=True):
    provider = provider if provider is not None else FakeIndexProvider()
    return (
        DerivedIndexReceipts(service, registry_for(provider), MemoryConfig(enabled=memory)),
        provider,
    )


async def create(service, key="record", body=BODY, **changes):
    record = await service.create(
        snapshot=snapshot(**{"body": body, **changes}), idempotency_key=key, **LOCAL
    )
    return record, body


async def correct(service, record):
    current = await service.update(
        identity=f"record:{record['record_id']}",
        patch={"body": CORRECTED},
        if_revision=record["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    return current, CORRECTED


def manifest(record, body=BODY, chunks=None):
    """A contiguous, text-free partition of one exact revision body."""

    split = len(body) // 2 if chunks is None else chunks
    parts, cursor = [], 0
    for ordinal in range(2):
        end = split if ordinal == 0 else len(body)
        parts.append(
            {
                "char_end": end,
                "char_start": cursor,
                "chunk_id": f"chunk-{ordinal}",
                "ordinal": ordinal,
            }
        )
        cursor = end
    return {
        "chunks": parts,
        "content_sha256": record["content_sha256"],
        "hash_version": 1,
        "record_id": record["record_id"],
        "revision_id": record["revision_id"],
    }


@contextmanager
def refuses(code):
    """Assert the stable reason code; messages are advisory, codes are contract."""

    with pytest.raises(RecordError) as excinfo:
        yield
    assert excinfo.value.code == code


def validate(
    value,
    *,
    body=BODY,
    record_id=RECORD["record_id"],
    revision_id=RECORD["revision_id"],
    content_sha256=HASH,
):
    return validate_manifest(
        value,
        record_id=record_id,
        revision_id=revision_id,
        content_sha256=content_sha256,
        body_length=len(body),
    )


def static(**changes):
    return {**RECORD, "content_sha256": HASH, **changes}



# --- the manifest ---------------------------------------------------------


def test_manifest_partitions_the_exact_revision_and_core_computes_the_digest():
    canonical = validate(manifest(static()))
    assert manifest_digest(canonical) == manifest_digest(manifest(static()))
    assert [chunk["char_start"] for chunk in canonical["chunks"]] == [0, len(BODY) // 2]
    assert "body" not in canonical["chunks"][0] and "text" not in canonical["chunks"][0]


@pytest.mark.parametrize(
    "change",
    [
        {"chunks": []},
        {"chunks": [{"char_end": 5, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0}]},
        {"chunks": [{"char_end": 10, "char_start": 5, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": len(BODY), "char_start": 10, "chunk_id": "chunk-1", "ordinal": 1}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 1},
                    {"char_end": len(BODY), "char_start": 10, "chunk_id": "chunk-1", "ordinal": 0}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": len(BODY), "char_start": 4, "chunk_id": "chunk-1", "ordinal": 1}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": 9, "char_start": 10, "chunk_id": "chunk-1", "ordinal": 1}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": 999, "char_start": 10, "chunk_id": "chunk-1", "ordinal": 1}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": len(BODY), "char_start": 10, "chunk_id": "chunk-0", "ordinal": 1}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": len(BODY), "char_start": 10, "chunk_id": "chunk-1 ", "ordinal": 1}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": len(BODY), "char_start": 10, "chunk_id": "x" * 129, "ordinal": 1}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": len(BODY), "char_start": 10, "chunk_id": "chunk-1\n", "ordinal": 1}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": True, "char_start": 10, "chunk_id": "chunk-1", "ordinal": 1}]},
        {"chunks": [{"char_end": 10, "char_start": 0, "chunk_id": "chunk-0", "ordinal": 0},
                    {"char_end": len(BODY), "char_start": 10, "chunk_id": "c1", "ordinal": 1,
                     "text": "excerpt"}]},
    ],
)
def test_manifest_refuses_gaps_overlaps_duplicates_labels_and_excerpts(change):
    with refuses("record.invalid_input"):
        validate({**manifest(static()), **change})


@pytest.mark.parametrize(
    "change",
    [
        {"content_sha256": "c" * 64},
        {"revision_id": "33333333-3333-4333-8333-333333333333"},
        {"record_id": "33333333-3333-4333-8333-333333333333"},
        {"hash_version": 2},
        {"score": 0.5},
        {"chunk_manifest_sha256": "d" * 64},
        {"text": "excerpt"},
        {"body": BODY},
    ],
)
def test_manifest_refuses_other_bytes_and_any_extra_field(change):
    with refuses("record.invalid_input"):
        validate({**manifest(static()), **change})


def test_manifest_body_length_must_match_the_revision():
    with refuses("record.invalid_input"):
        validate(manifest(static()), body=CORRECTED)


# --- receipts -------------------------------------------------------------


async def test_receipt_binds_the_exact_revision_with_a_core_computed_digest(service):
    index, provider = receipts(service)
    record, body = await create(service)
    payload = await index.hydrate_index_payload(
        record_id=record["record_id"], revision_id=record["revision_id"], **LOCAL
    )
    assert set(payload) == {
        "record_id",
        "revision_id",
        "sequence",
        "content_sha256",
        "hash_version",
        "scope_key",
        "title",
        "body",
        "body_length",
    }
    assert payload["body"] == body
    result = await index.acknowledge(
        provider_id=provider.provider_id, manifest=manifest(record, body), **LOCAL
    )
    assert (result["chunk_count"], result["sequence"]) == (2, 1)
    stored = await index_state_for_provider(service.db, provider.provider_id)
    assert str(stored[0]["revision_id"]) == record["revision_id"]
    assert stored[0]["chunk_manifest_sha256"] == result["chunk_manifest_sha256"]
    assert stored[0]["redacted_at"] is None
    assert (await index.lag(provider_id=provider.provider_id))["pending"] == []
    assert provider.calls == 0


async def test_a_stale_acknowledgment_never_moves_the_checkpoint_backwards(service):
    index, provider = receipts(service)
    original, body = await create(service)
    current, corrected = await correct(service, original)
    pending = await index.lag(provider_id=provider.provider_id)
    assert [row["revision_id"] for row in pending["pending"]] == [current["revision_id"]]
    await index.acknowledge(
        provider_id=provider.provider_id, manifest=manifest(current, corrected), **LOCAL
    )
    with refuses("knowledge.index_stale"):
        await index.acknowledge(
            provider_id=provider.provider_id, manifest=manifest(original, body), **LOCAL
        )
    stored = await index_state_for_provider(service.db, provider.provider_id)
    assert str(stored[0]["revision_id"]) == current["revision_id"]


async def test_a_manifest_must_describe_the_revision_it_is_acknowledged_for(service):
    index, provider = receipts(service)
    record, body = await create(service)
    current, _ = await correct(service, record)
    with refuses("record.invalid_input"):
        await index.acknowledge(
            provider_id=provider.provider_id,
            manifest=manifest(record, body),
            revision_id=current["revision_id"],
            **LOCAL,
        )


async def test_erasure_is_acknowledged_and_a_redacted_record_cannot_be_reindexed(service):
    index, provider = receipts(service)
    record, body = await create(service)
    await index.acknowledge(
        provider_id=provider.provider_id, manifest=manifest(record, body), **LOCAL
    )
    unredacted, _ = await create(service, "second")
    with refuses("knowledge.index_not_redacted"):
        await index.acknowledge_erasure(
            provider_id=provider.provider_id,
            record_id=unredacted["record_id"],
            revision_id=unredacted["revision_id"],
            **LOCAL,
        )
    await service.redact(
        identity=f"record:{record['record_id']}",
        reason_code="sensitive",
        if_revision=record["revision_id"],
        idempotency_key="redact",
        dry_run=False,
        **LOCAL,
    )
    assert [
        row["record_id"] for row in (await index.pending_erasures(provider_id=provider.provider_id))["pending"]
    ] == [record["record_id"]]
    erased = await index.acknowledge_erasure(
        provider_id=provider.provider_id,
        record_id=record["record_id"],
        revision_id=record["revision_id"],
        **LOCAL,
    )
    assert erased["state"] == "erased"
    assert (await index.pending_erasures(provider_id=provider.provider_id))["pending"] == []
    # Redaction is one-way: the exact revision a provider would re-index is
    # permanently unreadable, so a new acknowledgment is impossible.
    with refuses("record.revision_redacted"):
        await index.acknowledge(
            provider_id=provider.provider_id, manifest=manifest(record, body), **LOCAL
        )
    assert (await index_state_for_provider(service.db, provider.provider_id))[0]["redacted_at"]


async def test_erasing_a_record_that_was_never_indexed_writes_no_receipt(service):
    index, provider = receipts(service)
    record, _ = await create(service)
    await service.redact(
        identity=f"record:{record['record_id']}",
        reason_code="sensitive",
        if_revision=record["revision_id"],
        idempotency_key="redact",
        dry_run=False,
        **LOCAL,
    )
    result = await index.acknowledge_erasure(
        provider_id=provider.provider_id,
        record_id=record["record_id"],
        revision_id=record["revision_id"],
        **LOCAL,
    )
    assert result["state"] == "never_indexed"
    assert await index_state_for_provider(service.db, provider.provider_id) == []


@pytest.mark.parametrize("change", ["knowledge", "semantic", "memory"])
async def test_receipts_need_core_and_both_optional_switches(service, change):
    index, provider = receipts(service, memory=change != "memory")
    record, body = await create(service)
    if change == "knowledge":
        service.config.enabled = False
    if change == "semantic":
        service.config.semantic.enabled = False
    with refuses("knowledge.disabled"):
        await index.acknowledge(
            provider_id=provider.provider_id, manifest=manifest(record, body), **LOCAL
        )
    assert await index_state_for_provider(service.db, provider.provider_id) == []


@pytest.mark.parametrize(
    ("provider_id", "code"),
    [
        ("missing-provider", "knowledge.provider_unregistered"),
        ("other-provider", "knowledge.provider_unregistered"),
        ("Offline Provider", "knowledge.provider_invalid_id"),
        ("", "knowledge.provider_invalid_id"),
        ("aq-memory", "knowledge.provider_unregistered"),
    ],
)
async def test_only_a_bounded_registered_provider_id_may_write_receipts(service, provider_id, code):
    index, registered = receipts(service)
    record, body = await create(service)
    with refuses(code):
        await index.acknowledge(
            provider_id=provider_id, manifest=manifest(record, body), **LOCAL
        )
    assert registered.provider_id == "offline-provider"
    assert await index_state_for_provider(service.db, registered.provider_id) == []


async def test_unavailable_and_deprecated_providers_cannot_write_receipts(service):
    record, body = await create(service)
    unavailable = FakeIndexProvider(available=False)
    deprecated = FakeIndexProvider()
    deprecated.deprecated = True
    for provider, code in (
        (unavailable, "knowledge.provider_unavailable"),
        (deprecated, "knowledge.provider_deprecated"),
    ):
        index, _ = receipts(service, provider)
        with pytest.raises(RecordError, match=code):
            await index.acknowledge(
                provider_id=provider.provider_id, manifest=manifest(record, body), **LOCAL
            )


async def test_hydration_and_receipts_follow_core_authorization(service):
    worker = await worker_principal(service.db)
    ungranted = await worker_principal(service.db, name="ungranted", grants=["knowledge_search"])
    index, provider = receipts(service)
    record, body = await create(service)
    with refuses("record.forbidden"):
        await index.hydrate_index_payload(
            record_id=record["record_id"],
            revision_id=record["revision_id"],
            principal=ungranted,
            project_id="p",
        )
    with refuses("record.forbidden"):
        await index.acknowledge(
            provider_id=provider.provider_id,
            manifest=manifest(record, body),
            principal=ungranted,
            project_id="p",
        )
    payload = await index.hydrate_index_payload(
        record_id=record["record_id"],
        revision_id=record["revision_id"],
        principal=worker,
        project_id="p",
    )
    assert payload["body"] == body and payload["scope_key"] == "project:p"
    assert "sources" not in payload and "outgoing_links" not in payload


async def test_lag_is_bounded_read_only_and_sees_a_new_revision(service):
    index, provider = receipts(service)
    first, _ = await create(service, "first")
    await create(service, "second")
    for limit in (0, 101, True):
        with refuses("record.invalid_input"):
            await index.lag(provider_id=provider.provider_id, limit=limit)
    one = await index.lag(provider_id=provider.provider_id, limit=1)
    assert len(one["pending"]) == 1 and one["truncated"] is True
    # Derived lag never gates a read: the record is served while unindexed.
    assert (await service.show(identity=f"record:{first['record_id']}", **LOCAL))["success"]
    await correct(service, first)
    assert len((await index.lag(provider_id=provider.provider_id))["pending"]) == 2
    counts = await provider_lag_counts(service.db, [provider.provider_id])
    assert counts[provider.provider_id] == {"lag": 2, "erasure_pending": 0}


# --- migration ------------------------------------------------------------


@pytest.mark.migration


async def test_the_receipt_migration_is_idempotent_and_refuses_a_dataful_downgrade(service):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    migration = importlib.import_module(
        "migrations.versions.a00000000065_knowledge_index_receipts"
    )
    assert migration.down_revision == "a00000000064"

    def run(conn, action):
        with Operations.context(MigrationContext.configure(conn)):
            getattr(migration, action)()

    async with service.db.immediate() as conn:
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        present = await conn.run_sync(
            lambda sync: {
                name
                for name in (
                    "provider_id",
                    "record_id",
                    "revision_id",
                    "sequence",
                    "chunk_manifest_sha256",
                    "indexed_at",
                    "redacted_at",
                )
                if name in {column["name"] for column in inspect(sync).get_columns("record_index_state")}
            }
        )
        assert present == {
            "provider_id",
            "record_id",
            "revision_id",
            "sequence",
            "chunk_manifest_sha256",
            "indexed_at",
            "redacted_at",
        }

    record, body = await create(service)
    index, provider = receipts(service)
    await index.acknowledge(
        provider_id=provider.provider_id, manifest=manifest(record, body), **LOCAL
    )
    async with service.db.immediate() as conn:
        with pytest.raises(RuntimeError, match="read-only rollback"):
            await conn.run_sync(lambda sync: run(sync, "downgrade"))
        assert await conn.run_sync(lambda sync: inspect(sync).has_table("record_index_state"))
    async with service.db.immediate() as conn:
        await conn.execute(sql_delete(record_index_state))
        await conn.run_sync(lambda sync: run(sync, "downgrade"))
        assert not await conn.run_sync(lambda sync: inspect(sync).has_table("record_index_state"))
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        assert await conn.run_sync(lambda sync: inspect(sync).has_table("record_index_state"))


# --- doctor ---------------------------------------------------------------


async def test_doctor_reports_provider_receipt_lag_and_unacknowledged_erasure(service, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from src.config import AppConfig
    from src.doctor.models import DoctorContext
    from src.doctor.record_checks import record_checks

    class OfflinePlugins:
        def __getattr__(self, name):
            raise AssertionError(f"doctor touched optional plugin: {name}")

    config = AppConfig(data_dir=str(tmp_path))
    Path(config.vault_root).mkdir()
    ctx = DoctorContext(
        config=config,
        db=service.db,
        handler=SimpleNamespace(orchestrator=OfflinePlugins()),
    )

    async def report(name):
        return await next(c for c in record_checks() if c.id == f"records.{name}").run(ctx)

    record, body = await create(service)
    index, provider = receipts(service)
    assert (await report("index_lag")).data["provider_receipts"] == {}
    await index.acknowledge(
        provider_id=provider.provider_id, manifest=manifest(record, body), **LOCAL
    )
    acknowledged = await report("index_lag")
    assert acknowledged.data["provider_receipts"] == {
        provider.provider_id: {"lag": 0, "erasure_pending": 0}
    }
    current, _ = await correct(service, record)
    behind = await report("index_lag")
    assert behind.data["provider_receipts"][provider.provider_id]["lag"] == 1
    assert behind.data["count"] >= 1 and behind.severity.value != "ok"
    await service.redact(
        identity=f"record:{record['record_id']}",
        reason_code="sensitive",
        if_revision=current["revision_id"],
        idempotency_key="redact",
        dry_run=False,
        **LOCAL,
    )
    cleanup = await report("redaction_cleanup")
    assert cleanup.data["provider_receipts"] == {
        provider.provider_id: {"lag": 0, "erasure_pending": 1}
    }
    await index.acknowledge_erasure(
        provider_id=provider.provider_id,
        record_id=record["record_id"],
        revision_id=record["revision_id"],
        **LOCAL,
    )
    assert (await report("redaction_cleanup")).data["provider_receipts"][
        provider.provider_id
    ]["erasure_pending"] == 0


# --- registration ---------------------------------------------------------


def service_of(**changes):
    """A loaded plugin's registered service object, without any startup work."""

    return FakeIndexProvider(**{k: v for k, v in changes.items() if k == "available"})


def registry_for_service(service):
    return RetrievalProviderRegistry(lambda name: service)


def test_registration_is_absent_until_a_plugin_registers_a_service():
    assert RetrievalProviderRegistry(lambda name: None).lookup() is None
    assert RetrievalProviderRegistry(lambda name: None).registered() == ()
    raising = RetrievalProviderRegistry(lambda name: (_ for _ in ()).throw(RuntimeError("load")))
    assert raising.lookup() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": "Offline Provider"},
        {"provider_id": ""},
        {"provider_id": "x" * 65},
        {"provider_version": ""},
        {"provider_version": "v" * 129},
        {"available": False},
        {"deprecated": True},
    ],
)
def test_registration_refuses_an_unusable_provider(changes):
    service = service_of()
    for name, value in changes.items():
        setattr(service, name, value)
    assert registry_for_service(service).lookup() is None
    if not changes.get("deprecated"):
        assert registry_for_service(service).usable() == ()


def test_a_provider_without_async_search_is_absent():
    class NotAProvider:
        provider_id = "offline-provider"
        provider_version = "v1"

        def search(self, request):
            return []

    assert registry_for_service(NotAProvider()).lookup() is None


def test_lookup_never_initializes_imports_or_queries_the_provider():
    service = service_of()
    registry = registry_for_service(service)
    assert registry.lookup() is service
    assert service.calls == 0
    assert registry.registered() == (
        registry.registered()[0],
    )
    assert service.calls == 0


def test_deprecation_and_availability_are_reflected_in_require():
    service = service_of()
    registry = registry_for_service(service)
    entry = registry.require("offline-provider")
    assert entry.provider_id == "offline-provider"
    assert entry.handshake() == {
        "adapter": "knowledge-retrieval",
        "handshake_version": 1,
        "provider_id": "offline-provider",
        "provider_version": "v1",
        "deprecated": False,
        "index_capable": False,
        "authoritative_writes": False,
    }
    service.deprecated = True
    with refuses("knowledge.provider_deprecated"):
        registry.require("offline-provider")
    service.deprecated = False
    service.available = False
    with refuses("knowledge.provider_unavailable"):
        registry.require("offline-provider")
    with refuses("knowledge.provider_unregistered"):
        registry.require("absent-provider")
    with refuses("knowledge.provider_invalid_id"):
        registry.require("Offline Provider")


def test_an_index_capable_provider_reports_its_indexer():
    class Indexer:
        async def index(self, payload):
            return {}

        async def erase(self, record_id, revision_id):
            return {}

    service = service_of()
    service.indexer = Indexer()
    entry = registry_for_service(service).require("offline-provider")
    assert entry.handshake()["index_capable"] is True
    service.indexer = object()
    entry = registry_for_service(service).require("offline-provider")
    assert entry.handshake()["index_capable"] is False
