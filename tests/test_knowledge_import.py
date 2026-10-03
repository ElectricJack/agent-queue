"""Real PostgreSQL import receipts, source cutover and restore rehearsal."""

import asyncio
import base64
import shutil
from uuid import UUID

import pytest
from sqlalchemy import func, select

from src.commands.principal import TRUSTED_LOCAL
from src.config import KnowledgeFeatureConfig
from src.database.tables import (
    knowledge_revisions,
    record_import_items,
    record_import_runs,
    record_legacy_mappings,
    record_source_artifacts,
)
from src.knowledge.imports.apply import ImportService
from src.knowledge.imports.manifest import Artifact, Inventory, InventoryItem, Source, seal_manifest
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project

LOCAL = dict(principal=TRUSTED_LOCAL, project_id="p")


@pytest.fixture
async def importer(reuse_database, tmp_path):
    db = await reuse_database()
    await seed_project(db)
    vault = tmp_path / "vault"
    vault.mkdir()
    return ImportService(
        db, knowledge_config(import_apply=KnowledgeFeatureConfig(enabled=True)), vault
    )


def manifest(
    *,
    original="Original α\r\n",
    summary="Retained summary",
    scope="project:p",
    root="/legacy/notes",
    snapshot_id="s1",
    count=1,
    disposition="candidate",
):
    raw = original.encode()
    artifact = Artifact(raw)
    items = tuple(
        InventoryItem(
            (
                Source(
                    "notes",
                    scope,
                    f"notes/item-{i}.md",
                    artifact.sha256,
                    {"real_root": root, "relative_path": f"item-{i}.md"},
                ),
            ),
            "file_only",
            disposition,
            original=original,
            summary=summary,
        )
        for i in range(count)
    )
    return seal_manifest(
        Inventory((), "not_observed", items, (artifact,)),
        source_installation_id="legacy1",
        snapshot_id=snapshot_id,
        snapshot_timestamp="2026-10-01T00:00:00Z",
    )


def args(sealed, key="import1", **changes):
    from src.knowledge.imports.manifest import verify_manifest

    doc = verify_manifest(sealed.content, sealed.sha256)
    return {
        **LOCAL,
        "manifest_content_base64": base64.b64encode(sealed.content).decode(),
        "manifest_sha256": sealed.sha256,
        "selected_item_ids": [i["item_key"] for i in doc["items"]],
        "idempotency_key": key,
        "backup_receipt": "synthetic-backup-1",
        **changes,
    }


async def revisions(importer):
    async with importer.db.immediate() as conn:
        return await conn.scalar(select(func.count()).select_from(knowledge_revisions))


async def test_replay_and_second_run_reuse_zero_revisions_and_preserve_original(importer):
    sealed = manifest()
    first = await importer.apply(**args(sealed))
    assert first["state"] == "succeeded" and first["counts"]["created"] == 1
    item = first["items"][0]
    shown = await importer.show(identity=f"record:{item['record_id']}", **LOCAL)
    assert shown["snapshot"]["body"] == "Original α\r\n"
    assert shown["snapshot"]["summary"] == "Retained summary"
    assert shown["snapshot"]["verification"] == "unverified"
    assert shown["authority"] is None
    replay = await importer.apply(**args(sealed))
    assert replay["replay"] and replay["counts"]["reused"] == 1
    assert replay["counts"]["created"] == 0
    assert replay["items"][0]["record_id"] == first["items"][0]["record_id"]
    second = await importer.apply(**args(sealed, "another-run"))
    assert second["counts"]["reused"] == 1
    assert await revisions(importer) == 1
    async with importer.db.immediate() as conn:
        artifacts = (await conn.execute(select(record_source_artifacts))).mappings().all()
    contents = [importer._artifact_io(a["storage_key"]) for a in artifacts]
    assert sealed.content in contents and "Original α\r\n".encode() in contents


async def test_interruption_cancel_restart_resumes_receipts_and_reconciles(importer):
    sealed = manifest(count=3)
    first = await importer.apply(**args(sealed, limit=1))
    assert first["state"] == "applying" and first["counts"]["pending"] == 2
    common = {**LOCAL, "run_id": first["run_id"], "manifest_sha256": sealed.sha256}
    cancelled = await importer.resume(**common, cancel=True)
    assert cancelled["state"] == "cancelled" and await revisions(importer) == 1
    restarted = ImportService(importer.db, importer.config, importer.vault_root)
    final = await restarted.resume(**common)
    assert final["state"] == "succeeded"
    assert (
        final["counts"]["selected"]
        == sum(
            final["counts"][k]
            for k in ("created", "revised", "reused", "excluded", "quarantined", "failed")
        )
        == 3
    )
    async with importer.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(record_import_items)) == 3
    assert await revisions(importer) == 3


async def test_changed_manifest_or_selection_cannot_mutate_pinned_run(importer):
    first = await importer.apply(**args(manifest()))
    with pytest.raises(RecordError) as exc:
        await importer.apply(**args(manifest(original="Changed source", snapshot_id="s2")))
    assert exc.value.code == "record.idempotency_conflict"
    with pytest.raises(RecordError) as exc:
        await importer.resume(**LOCAL, run_id=first["run_id"], manifest_sha256="a" * 64)
    assert exc.value.code == "knowledge_import.manifest_changed"
    assert await revisions(importer) == 1


async def test_changed_path_and_hash_need_exact_base_and_keep_identity(importer):
    first = await importer.apply(**args(manifest()))
    before = first["items"][0]
    changed = manifest(original="Changed original", root="/relocated/notes", snapshot_id="s2")
    refused = await importer.apply(**args(changed, "missing-base"))
    assert refused["items"][0]["error_code"] == "record.precondition_required"
    result = await importer.apply(
        **args(
            changed,
            "with-base",
            expected_revisions={
                before["item_key"]: before["revision_id"],
            },
            expected_source_hashes=before["source_hashes"],
        )
    )
    assert result["counts"]["revised"] == 1
    assert result["items"][0]["record_id"] == before["record_id"]
    assert await revisions(importer) == 2
    async with importer.db.immediate() as conn:
        paths = (
            (
                await conn.execute(
                    select(record_legacy_mappings.c.source_key).where(
                        record_legacy_mappings.c.source_kind == "path"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(paths) == {"/legacy/notes/item-0.md", "/relocated/notes/item-0.md"}


async def test_concurrent_imports_create_once_and_edit_wins_conflict(importer):
    sealed = manifest()
    results = await asyncio.gather(
        importer.apply(**args(sealed, "a")), importer.apply(**args(sealed, "b"))
    )
    assert sorted(r["items"][0]["outcome"] for r in results) == ["created", "reused"]
    before = results[0]["items"][0]
    changed = manifest(original="New snapshot", snapshot_id="s2")
    changed_args = args(
        changed,
        "c",
        expected_revisions={
            before["item_key"]: before["revision_id"],
        },
        expected_source_hashes=before["source_hashes"],
    )
    updated = await KnowledgeService(importer.db, importer.config).update(
        identity=f"record:{before['record_id']}",
        patch={"body": "Concurrent edit"},
        if_revision=before["revision_id"],
        idempotency_key="edit",
        **LOCAL,
    )
    conflict = await importer.apply(**changed_args)
    assert conflict["counts"]["failed"] == 1
    assert conflict["items"][0]["error_code"] == "record.revision_conflict"
    shown = await importer.show(identity=f"record:{before['record_id']}", **LOCAL)
    assert shown["revision_id"] == updated["revision_id"]
    assert shown["snapshot"]["body"] == "Concurrent edit"


@pytest.mark.parametrize(
    "disposition,outcome",
    [
        ("excluded", "excluded"),
        ("quarantined", "quarantined"),
        ("unavailable", "failed"),
    ],
)
async def test_non_candidates_receive_explicit_accounting(importer, disposition, outcome):
    result = await importer.apply(**args(manifest(disposition=disposition)))
    assert result["counts"][outcome] == 1
    assert result["counts"]["selected"] == result["counts"]["processed"] == 1
    assert await revisions(importer) == 0


async def test_scope_flags_and_backup_are_required_before_any_write(importer):
    with pytest.raises(RecordError):
        await importer.apply(**args(manifest(), limit=101))
    assert await revisions(importer) == 0
    with pytest.raises(RecordError) as exc:
        await importer.apply(**args(manifest(scope="project:q")))
    assert exc.value.code == "record.forbidden"
    with pytest.raises(RecordError):
        await importer.apply(**args(manifest(), backup_receipt=""))
    importer.config.import_apply.enabled = False
    with pytest.raises(RecordError) as exc:
        await importer.apply(**args(manifest()))
    assert exc.value.code == "knowledge_import.disabled"


async def test_temporal_versions_keep_effective_order_and_private_export(importer):
    from src.knowledge.imports.manifest import canonical_json

    rows = [
        {
            "entry_type": "temporal",
            "value": value,
            "namespace": "history",
            "key": "x",
            "valid_from": date,
            "original_source_time": date,
        }
        for value, date in (("Latest", "2026-10-01T00:00:00Z"), ("Earlier", "2026-09-01T00:00:00Z"))
    ]
    export = Artifact(b"Private export also includes another project secret")
    source = Source("vector", "project:p", "legacy/history/x", export.sha256, {"rows": rows})
    sealed = seal_manifest(
        Inventory(
            (),
            "observed",
            (InventoryItem((source,), "temporal_history", "candidate", original="Latest"),),
            (export,),
        ),
        source_installation_id="vector1",
        snapshot_id="temporal1",
        snapshot_timestamp="2026-10-02T00:00:00Z",
    )
    result = await importer.apply(**args(sealed))
    assert result["counts"]["created"] == 1 and await revisions(importer) == 2
    record = result["items"][0]
    history = await importer.history(identity=f"record:{record['record_id']}", **LOCAL)
    versions = sorted(history["revisions"], key=lambda r: r["sequence"])
    snapshots = [
        (
            await importer.show(
                identity=f"record:{record['record_id']}", revision_id=r["revision_id"], **LOCAL
            )
        )["snapshot"]
        for r in versions
    ]
    assert [s["body"] for s in snapshots] == ["Earlier", "Latest"]
    shown = await importer.show(identity=f"record:{record['record_id']}", **LOCAL)
    assert shown["snapshot"]["valid_from"] == "2026-10-01T00:00:00.000000Z"
    async with importer.db.immediate() as conn:
        evidence = (
            (
                await conn.execute(
                    select(record_source_artifacts).where(
                        record_source_artifacts.c.media_type == "application/octet-stream",
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(evidence) == 1
    scoped = importer._artifact_io(evidence[0]["storage_key"])
    assert b"another project secret" not in scoped and canonical_json(rows[0]) in scoped


async def test_import_erasure_denies_retained_manifest_and_future_replay(importer):
    from tests.test_knowledge_redaction import redact_args

    sealed = manifest()
    result = await importer.apply(**args(sealed))
    record = result["items"][0]
    await importer.redact(**redact_args(record))
    for operation in (
        importer.resume(**LOCAL, run_id=result["run_id"], manifest_sha256=sealed.sha256),
        importer.apply(**args(sealed, "new-key-after-erasure")),
    ):
        with pytest.raises(RecordError, match="record.revision_redacted"):
            await operation
    async with importer.db.immediate() as conn:
        artifacts = (await conn.execute(select(record_source_artifacts))).mappings().all()
    assert artifacts and all(a["redacted_at"] for a in artifacts)
    assert await revisions(importer) == 1


async def test_stale_old_source_hash_refuses_even_with_current_revision(importer):
    first = (await importer.apply(**args(manifest())))["items"][0]
    second_manifest = manifest(original="Second source", snapshot_id="s2")
    without_hash = await importer.apply(
        **args(
            second_manifest,
            "missing-hash",
            expected_revisions={first["item_key"]: first["revision_id"]},
        )
    )
    assert without_hash["items"][0]["error_code"] == "record.precondition_required"
    second = (
        await importer.apply(
            **args(
                second_manifest,
                "second",
                expected_revisions={first["item_key"]: first["revision_id"]},
                expected_source_hashes=first["source_hashes"],
            )
        )
    )["items"][0]
    assert second["source_hashes"] != first["source_hashes"]
    third_manifest = manifest(original="Third source", snapshot_id="s3")
    stale = await importer.apply(
        **args(
            third_manifest,
            "third-stale",
            expected_revisions={second["item_key"]: second["revision_id"]},
            expected_source_hashes=first["source_hashes"],
        )
    )
    assert stale["items"][0]["error_code"] == "knowledge_import.source_conflict"
    with pytest.raises(RecordError, match="record.idempotency_conflict"):
        await importer.apply(
            **args(
                third_manifest,
                "third-stale",
                expected_revisions={second["item_key"]: second["revision_id"]},
                expected_source_hashes=second["source_hashes"],
            )
        )
    assert await revisions(importer) == 2


async def test_file_vector_aliases_keep_distinct_old_hash_preconditions(importer):
    artifact = Artifact(b"Original alias")

    def aliases(root, sid):
        sources = (
            Source(
                "notes",
                "project:p",
                "notes/item-0.md",
                artifact.sha256,
                {"real_root": root, "relative_path": "item-0.md"},
            ),
            Source(
                "vector",
                "project:p",
                "vector/chunk-1",
                artifact.sha256,
                {"original": "Original alias"},
            ),
        )
        return seal_manifest(
            Inventory(
                (),
                "observed",
                (
                    InventoryItem(
                        sources, "matching_file_vector", "candidate", original="Original alias"
                    ),
                ),
                (artifact,),
            ),
            source_installation_id="alias1",
            snapshot_id=sid,
            snapshot_timestamp="2026-10-01T00:00:00Z",
        )

    first = (await importer.apply(**args(aliases("/aliases", "s1"))))["items"][0]
    assert len(set(first["source_hashes"].values())) == 2
    changed = aliases("/moved-aliases", "s2")
    incomplete = await importer.apply(
        **args(
            changed,
            "incomplete",
            expected_revisions={first["item_key"]: first["revision_id"]},
            expected_source_hashes=dict(list(first["source_hashes"].items())[:1]),
        )
    )
    assert incomplete["items"][0]["error_code"] == "record.precondition_required"
    result = await importer.apply(
        **args(
            changed,
            "complete",
            expected_revisions={first["item_key"]: first["revision_id"]},
            expected_source_hashes=first["source_hashes"],
        )
    )
    assert result["counts"]["revised"] == 1
    assert result["items"][0]["record_id"] == first["record_id"]


async def test_parallel_edit_and_new_snapshot_have_one_winner(importer):
    first = (await importer.apply(**args(manifest())))["items"][0]
    changed = manifest(original="Imported winner", snapshot_id="s2")
    imported, edited = await asyncio.gather(
        importer.apply(
            **args(
                changed,
                "race",
                expected_revisions={first["item_key"]: first["revision_id"]},
                expected_source_hashes=first["source_hashes"],
            )
        ),
        importer.update(
            identity=f"record:{first['record_id']}",
            patch={"body": "Edited winner"},
            if_revision=first["revision_id"],
            idempotency_key="race-edit",
            **LOCAL,
        ),
        return_exceptions=True,
    )
    assert not isinstance(imported, Exception), imported
    if imported["counts"]["revised"]:
        assert isinstance(edited, RecordError) and edited.code == "record.revision_conflict"
    else:
        assert imported["items"][0]["error_code"] == "record.revision_conflict"
        assert edited["success"]
    assert await revisions(importer) == 2


async def test_global_import_needs_explicit_scope_and_cannot_promote_private_labels(importer):
    from src.records.auth import GLOBAL_SCOPE

    global_args = args(manifest(scope="global"), project_id=GLOBAL_SCOPE)
    with pytest.raises(RecordError):
        await importer.apply(**global_args)
    assert await revisions(importer) == 0
    importer.config.global_enabled = True
    with pytest.raises(RecordError) as exc:
        await importer.apply(**args(manifest(scope="project:p"), project_id=GLOBAL_SCOPE))
    assert exc.value.code == "record.forbidden"
    result = await importer.apply(**global_args)
    shown = await importer.show(
        identity=f"record:{result['items'][0]['record_id']}",
        principal=TRUSTED_LOCAL,
        project_id=GLOBAL_SCOPE,
    )
    assert shown["snapshot"]["verification"] == "unverified"
    async with importer.db.immediate() as conn:
        record = await importer.db.get_record_on(record_id=UUID(shown["record_id"]), conn=conn)
    assert record["scope_key"] == "global"
    assert shown["authority"] is None


async def test_retained_snapshot_ignores_human_edits_preserves_source(importer, tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    source = root / "item-0.md"
    source.write_text("Human edit after sealing")
    result = await importer.apply(**args(manifest(root=str(root))))
    shown = await importer.show(identity=f"record:{result['items'][0]['record_id']}", **LOCAL)
    assert shown["snapshot"]["body"] == "Original α\r\n"
    assert source.read_text() == "Human edit after sealing"


async def test_backup_restore_keeps_exact_history_mappings_and_receipts(importer, tmp_path):
    """Actual PostgreSQL dump/restore plus the confined artifact directory."""
    from src.database.engine import create_postgres_engine
    from tests.pg_dsn import create_scratch_database

    sealed = manifest(count=2)
    first = await importer.apply(**args(sealed, limit=1))
    restored_vault = tmp_path / "restored-vault"
    shutil.copytree(importer.vault_root, restored_vault)
    engine = create_postgres_engine(await create_scratch_database("knowledge-import-restore"))
    try:
        # Restore a full SQL dump, retaining installation identity and all domain
        # tables. Only disposable test DSNs from the fixture are used.
        dump_url = importer.db._engine.url.set(drivername="postgresql")
        target_url = engine.url.set(drivername="postgresql")

        # The repository's disposable cluster ships matching client tools in
        # its container. Other test servers use the installed native clients.
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

        dump = await asyncio.create_subprocess_exec(
            *pg_command("pg_dump", dump_url),
            "--no-owner",
            "--no-acl",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        sql, errors = await dump.communicate()
        assert dump.returncode == 0, errors.decode()
        restore = await asyncio.create_subprocess_exec(
            *pg_command("psql", target_url),
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, errors = await restore.communicate(sql)
        assert restore.returncode == 0, errors.decode()
        from src.database import Database

        restored_db = Database.__new__(Database)
        restored_db._engine = engine
        restored = ImportService(restored_db, importer.config, restored_vault)
        final = await restored.resume(
            **LOCAL, run_id=first["run_id"], manifest_sha256=sealed.sha256
        )
        assert final["counts"]["created"] == 2
        replay = await restored.apply(**args(sealed, "restored-replay"))
        assert replay["counts"]["reused"] == 2
        for item in final["items"]:
            shown = await restored.show(identity=f"record:{item['record_id']}", **LOCAL)
            assert shown["snapshot"]["body"] == "Original α\r\n"
        assert await revisions(restored) == 2
        async with engine.begin() as conn:
            assert await conn.scalar(
                select(record_import_runs.c.run_id).where(
                    record_import_runs.c.run_id == UUID(first["run_id"])
                )
            )
    finally:
        await engine.dispose()
