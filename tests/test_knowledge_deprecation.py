"""K14 synthetic reconciliation, honest telemetry and disposable restore proof."""

import asyncio
import importlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, inspect, select, update

from src.database.tables import metadata, record_legacy_mappings
from src.knowledge.deprecation import (
    DeprecationEvidence,
    adapter_removal_guard,
    compatibility_report,
    reconcile_manifest,
)
from src.knowledge.imports.compatibility import CompatibilityFence
from src.knowledge.imports.manifest import Inventory, seal_manifest, verify_manifest
from tests.test_knowledge_import import LOCAL, args, importer as importer_fixture, manifest

importer = importer_fixture
NOW = datetime(2026, 11, 2, tzinfo=UTC)


def observed_manifest(**changes):
    original = manifest(**changes)
    doc = verify_manifest(original.content, original.sha256)
    doc["vector_observation"] = "observed"
    from src.knowledge.imports.manifest import SealedManifest, canonical_json, sha256

    content = canonical_json(doc)
    return SealedManifest(content, sha256(content))


def evidence(**changes):
    return replace(
        DeprecationEvidence(
            announced_at=NOW - timedelta(days=30),
            release_receipts=("release-1", "release-2"),
            restore_receipt="synthetic-disposable-restore",
            replacement_acceptance_receipt="synthetic-replacement-acceptance",
            compatibility_coverage_receipt="synthetic-coverage-attestation",
            unsafe_compatibility_writes=0,
            g7_approved_by="synthetic-operator",
            g7_decision_receipt="synthetic-g7-decision",
        ),
        **changes,
    )


async def reconciliation(importer, sealed):
    return await reconcile_manifest(
        importer.db, content=sealed.content, manifest_sha256=sealed.sha256
    )


async def test_complete_inventory_requires_every_source_and_observed_vectors(importer):
    sealed = observed_manifest(count=3)
    partial = await importer.apply(**args(sealed, limit=1))
    report = await reconciliation(importer, sealed)
    assert report["managed"] == 1 and report["unresolved"] == 2 and not report["complete"]
    await importer.resume(**LOCAL, run_id=partial["run_id"], manifest_sha256=sealed.sha256)
    report = await reconciliation(importer, sealed)
    assert report["complete"] and report["sources"] == report["managed"] == 3
    # No original, source identity or record body leaks into the summary.
    assert "Original" not in json.dumps(report) and "item-" not in json.dumps(report)
    assert not (await reconciliation(importer, manifest(count=3)))["complete"]
    changed = observed_manifest(original="Changed retained evidence", snapshot_id="s2", count=3)
    assert not (await reconciliation(importer, changed))["complete"]


@pytest.mark.parametrize("disposition", ["excluded", "quarantined", "unavailable"])
async def test_explicit_exclusions_close_accounting_but_unresolved_sources_do_not(
    importer, disposition
):
    sealed = observed_manifest(disposition=disposition)
    await importer.apply(**args(sealed))
    report = await reconciliation(importer, sealed)
    assert report["complete"] == (disposition == "excluded")
    if disposition == "excluded":
        async with importer.db.immediate() as conn:
            await conn.execute(update(record_legacy_mappings).values(decision_reason=None))
        assert not (await reconciliation(importer, sealed))["complete"]


async def test_empty_observed_inventory_and_corrupt_seal(importer):
    sealed = seal_manifest(
        Inventory((), "observed", (), ()),
        source_installation_id="empty-synthetic",
        snapshot_id="empty",
        snapshot_timestamp="2026-10-01T00:00:00Z",
    )
    assert (await reconciliation(importer, sealed))["complete"]
    with pytest.raises(ValueError, match="hash mismatch"):
        await reconcile_manifest(importer.db, content=sealed.content, manifest_sha256="0" * 64)


@pytest.mark.parametrize(
    "changes,blocker",
    [
        ({"announced_at": NOW - timedelta(days=29)}, "announced_30_day_window_required"),
        ({"announced_at": NOW.replace(tzinfo=None)}, "announced_30_day_window_required"),
        ({"release_receipts": ("r1", "r1")}, "two_release_receipts_required"),
        ({"restore_receipt": ""}, "restore_receipt_required"),
        ({"compatibility_coverage_receipt": ""}, "compatibility_coverage_receipt_required"),
        ({"unsafe_compatibility_writes": None}, "zero_unsafe_compatibility_writes_required"),
        ({"unsafe_compatibility_writes": 1}, "zero_unsafe_compatibility_writes_required"),
        ({"unsafe_compatibility_writes": False}, "zero_unsafe_compatibility_writes_required"),
        ({"g7_decision_receipt": ""}, "g7_decision_receipt_required"),
    ],
)
def test_removal_guard_requires_each_operator_gate(changes, blocker):
    result = adapter_removal_guard({"complete": True}, evidence(**changes), now=NOW)
    assert not result["g7_evidence_complete"] and blocker in result["blockers"]
    assert not result["removal_authorized"]


def test_gate_closed_by_default_and_g7_does_not_authorize_deletion():
    closed = adapter_removal_guard({"complete": False}, DeprecationEvidence(), now=NOW)
    assert "complete_reconciliation_required" in closed["blockers"]
    assert not closed["g7_evidence_complete"]
    approved = adapter_removal_guard({"complete": True}, evidence(), now=NOW)
    assert approved["g7_evidence_complete"] and not approved["removal_authorized"]


async def test_atomic_content_free_attempts_survive_flags_off_and_concurrent_calls(importer):
    first = await importer.apply(**args(observed_manifest()))
    fence = CompatibilityFence(importer.db)
    identity = dict(
        source_installation_id="legacy1",
        source_kind="notes",
        source_scope="project:p",
        source_key="notes/item-0.md",
    )
    execute = AsyncMock(return_value={"success": False, "error_code": "knowledge.disabled"})
    importer.config.enabled = False
    await asyncio.gather(*(fence.write(execute, **identity) for _ in range(8)))
    execute.assert_not_awaited()
    await fence.read(execute, **identity)
    await fence.write(
        execute,
        **identity,
        if_revision=first["items"][0]["revision_id"],
        idempotency_key="guarded",
        patch={"body": "Private edit"},
    )
    report = await compatibility_report(importer.db)
    counts = {(r["operation"], r["outcome"]): r["calls"] for r in report["attempts"]}
    assert counts == {
        ("read", "canonical_read"): 1,
        ("write", "refused_write"): 8,
        ("write", "guarded_write"): 1,
    }
    assert report["unsafe_writes"] is None and not report["removal_authorized"]
    assert "Private edit" not in json.dumps(report) and "item-0" not in json.dumps(report)


async def test_older_transaction_cannot_move_last_observation_backwards(importer):
    from src.knowledge.deprecation import record_usage_on

    usage = dict(scope_key="project:p", operation="write", outcome="refused_write")
    async with importer.db.immediate() as older:
        await older.scalar(select(func.now()))
        async with importer.db.immediate() as newer:
            await record_usage_on(newer, **usage)
        await record_usage_on(older, **usage)
    report = await compatibility_report(importer.db)
    assert report["attempts"][0]["calls"] == 2
    assert report["last_seen_at"] >= report["first_seen_at"]


@pytest.mark.migration
async def test_telemetry_migration_is_idempotent_and_preserves_nonempty_evidence(importer):
    module = importlib.import_module("migrations.versions.a00000000066_knowledge_deprecation_usage")

    def migrate(conn, action):
        with Operations.context(MigrationContext.configure(conn)):
            getattr(module, action)()

    async with importer.db.immediate() as conn:
        await conn.run_sync(lambda sync: migrate(sync, "downgrade"))
        assert not await conn.run_sync(
            lambda sync: inspect(sync).has_table("record_compatibility_usage")
        )
        await conn.run_sync(lambda sync: migrate(sync, "upgrade"))
        await conn.run_sync(lambda sync: migrate(sync, "upgrade"))
        from src.knowledge.deprecation import record_usage_on

        await record_usage_on(
            conn, scope_key="project:p", operation="write", outcome="refused_write"
        )
    with pytest.raises(RuntimeError, match="read-only rollback"):
        async with importer.db.immediate() as conn:
            await conn.run_sync(lambda sync: migrate(sync, "downgrade"))
    assert (await compatibility_report(importer.db))["attempts"][0]["calls"] == 1


async def test_synthetic_full_database_and_vault_restore_preserves_exact_evidence(
    importer, tmp_path
):
    """Real dump/restore of exclusively test-owned DBs; never a live inventory."""
    from src.database import Database
    from src.database.engine import create_postgres_engine
    from src.knowledge.imports.apply import ImportService
    from src.knowledge.models import content_hash
    from tests.pg_dsn import create_scratch_database

    sealed = observed_manifest(count=2)
    first = await importer.apply(**args(sealed))
    item = first["items"][0]
    await importer.update(
        identity=f"record:{item['record_id']}",
        patch={"body": "New evidence"},
        if_revision=item["revision_id"],
        idempotency_key="edited",
        **LOCAL,
    )
    await CompatibilityFence(importer.db).write(
        AsyncMock(),
        source_installation_id="legacy1",
        source_kind="notes",
        source_scope="project:p",
        source_key="notes/item-0.md",
    )
    before = await reconciliation(importer, sealed)
    assert before["complete"]

    async def retained_rows(db):
        async with db.immediate() as conn:
            result = {}
            for name, table in metadata.tables.items():
                if name.startswith(("record_", "knowledge_")) or name == "records":
                    rows = (await conn.execute(select(table))).mappings().all()
                    result[name] = sorted(
                        json.dumps(dict(r), sort_keys=True, default=str) for r in rows
                    )
            return result

    retained = await retained_rows(importer.db)
    restored_vault = tmp_path / "restored-vault"
    shutil.copytree(importer.vault_root, restored_vault)
    engine = create_postgres_engine(await create_scratch_database("knowledge-deprecation-restore"))

    def pg_command(tool, url):
        if url.host in {"localhost", "127.0.0.1"} and url.port == 5534:
            return [
                "docker",
                "exec",
                "-i",
                "aq-postgres-test",
                tool,
                "-U",
                "agent_queue_test",
                "-d",
                url.database,
            ]
        return [tool, "-d", url.render_as_string(hide_password=False)]

    try:
        dump = await asyncio.create_subprocess_exec(
            *pg_command("pg_dump", importer.db._engine.url.set(drivername="postgresql")),
            "--no-owner",
            "--no-acl",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        sql, errors = await dump.communicate()
        assert dump.returncode == 0, errors.decode()
        # Capture an actual erasure after the backup, then reapply its request
        # on the restored copy before any delivery is enabled.
        from tests.test_knowledge_redaction import redact_args

        current = await importer.show(identity=f"record:{item['record_id']}", **LOCAL)
        erasure = redact_args(current)
        await importer.redact(**erasure)
        restore = await asyncio.create_subprocess_exec(
            *pg_command("psql", engine.url.set(drivername="postgresql")),
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, errors = await restore.communicate(sql)
        assert restore.returncode == 0, errors.decode()
        restored_db = Database.__new__(Database)
        restored_db._engine = engine
        assert await retained_rows(restored_db) == retained
        for path in importer.vault_root.rglob("*"):
            if path.is_file():
                assert (
                    restored_vault / path.relative_to(importer.vault_root)
                ).read_bytes() == path.read_bytes()
        restored = ImportService(restored_db, importer.config, restored_vault)
        assert await reconciliation(restored, sealed) == before
        original = await restored.show(
            identity=f"record:{item['record_id']}", revision_id=item["revision_id"], **LOCAL
        )
        assert original["snapshot"]["body"] == "Original α\r\n"
        assert content_hash(original["snapshot"]) == original["content_sha256"]
        replay = await restored.apply(**args(sealed, "restore-replay"))
        assert replay["counts"]["reused"] == 2 and replay["counts"]["created"] == 0
        # Reapply a post-backup erasure before allowing delivery. The older
        # manifest and exact historical revision must then remain unavailable.
        await restored.redact(**erasure)
        assert not (await reconciliation(restored, sealed))["complete"]
        from src.records.models import RecordError

        with pytest.raises(RecordError, match="record.revision_redacted"):
            await restored.show(
                identity=f"record:{item['record_id']}", revision_id=item["revision_id"], **LOCAL
            )
        assert (await compatibility_report(restored_db))["attempts"][0]["calls"] == 1
    finally:
        await engine.dispose()
