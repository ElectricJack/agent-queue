"""Ownership stays fenced across disabled writes and filesystem divergence."""

from unittest.mock import AsyncMock

from src.knowledge.imports.compatibility import CompatibilityFence, managed_export_path
from tests.test_knowledge_import import args, importer as importer_fixture, manifest

importer = importer_fixture  # pytest registers imported fixtures in this module


async def test_mapped_unguarded_writes_refuse_and_guarded_calls_use_command_handler(importer):
    result = await importer.apply(**args(manifest()))
    fence = CompatibilityFence(importer.db)
    identity = dict(
        source_installation_id="legacy1",
        source_kind="notes",
        source_scope="project:p",
        source_key="notes/item-0.md",
    )
    execute = AsyncMock(return_value={"success": True})
    refused = await fence.write(execute, **identity)
    assert refused["error_code"] == "memory.migrated_read_only"
    execute.assert_not_awaited()
    await fence.write(
        execute,
        **identity,
        if_revision=result["items"][0]["revision_id"],
        idempotency_key="edit1",
        patch={"body": "Edit"},
    )
    call = execute.await_args
    assert call.args[0] == "knowledge_update"
    assert call.args[1]["identity"] == f"record:{result['items'][0]['record_id']}"
    assert call.args[1]["project_id"] == "p"
    assert await fence.write(execute, **{**identity, "source_key": "unselected"}) is None


async def test_compatibility_read_resolves_canonical_record(importer):
    await importer.apply(**args(manifest()))
    execute = AsyncMock(return_value={"success": True, "snapshot": {"body": "Canonical"}})
    read = await CompatibilityFence(importer.db).read(
        execute,
        source_installation_id="legacy1",
        source_kind="notes",
        source_scope="project:p",
        source_key="notes/item-0.md",
    )
    assert read["deprecation"]["code"] == "memory.migrated"
    assert execute.await_args.args[0] == "knowledge_show"


async def test_note_write_append_delete_promote_refuse_after_cutover_even_when_disabled(
    importer,
    internal_plugins_handler,
):
    handler = await internal_plugins_handler()
    from tests.record_helpers import seed_project

    await seed_project(handler._db)
    created = await handler.execute(
        "write_note",
        {
            "project_id": "p",
            "title": "Item 0",
            "content": "Original note",
        },
    )
    from pathlib import Path
    from src.knowledge.imports.apply import ImportService

    path = Path(created["path"])
    service = ImportService(handler._db, importer.config, importer.vault_root)
    # The notes command slug is item-0, matching the source snapshot identity.
    result = await service.apply(**args(manifest(root=str(path.parent))))
    assert result["counts"]["created"] == 1
    handler.config.knowledge = service.config
    handler._plugin_registry.set_execute_command_callback(handler.execute)
    read = await handler.execute("read_note", {"project_id": "p", "title": "Item 0"})
    assert read["content"] == "Original α\r\n" and read["source_diverged"], read
    assert read["deprecation"]["canonical_command"] == "knowledge_show"
    listed = await handler.execute("list_notes", {"project_id": "p"})
    assert listed["notes"][0]["record_id"] == result["items"][0]["record_id"]
    assert listed["notes"][0]["title"] == "notes/item-0.md"
    from tests.test_knowledge_redaction import redact_args

    await service.redact(**redact_args(result["items"][0]))
    assert (await handler.execute("list_notes", {"project_id": "p"}))["notes"] == []
    denied = await handler.execute("read_note", {"project_id": "p", "title": "Item 0"})
    assert denied["error_code"] == "record.revision_redacted"
    handler.config.knowledge.enabled = False
    service.config.enabled = False
    for command in ("write_note", "append_note", "delete_note", "promote_note"):
        refused = await handler.execute(
            command,
            {
                "project_id": "p",
                "title": "Item 0",
                "content": "Unsafe write",
            },
        )
        assert refused["success"] is False, (command, refused)
        assert refused["error_code"] == "memory.migrated_read_only"
    assert path.read_text() == "Original note"
    alias = path.with_name("alias.md")
    alias.symlink_to(path)
    refused = await handler.execute(
        "append_note",
        {
            "project_id": "p",
            "title": "Alias",
            "content": "Unsafe alias write",
        },
    )
    assert refused["error_code"] == "memory.migrated_read_only"
    assert path.read_text() == "Original note"
    path.unlink()
    refused = await handler.execute(
        "write_note",
        {
            "project_id": "p",
            "title": "Item 0",
            "content": "Recreate",
        },
    )
    assert refused["error_code"] == "memory.migrated_read_only"
    assert not path.exists()


async def test_path_guard_serializes_legacy_write_and_import_cutover(importer):
    import asyncio

    fence = CompatibilityFence(importer.db)
    entered = asyncio.Event()
    async with fence.note_path("p", "/legacy/notes/item-0.md") as mapping:
        assert mapping is None

        async def cutover():
            entered.set()
            return await importer.apply(**args(manifest()))

        pending = asyncio.create_task(cutover())
        await entered.wait()
        # Verify the engine is waiting on the path lock through a second
        # connection, rather than using a wall-clock budget assertion.
        from sqlalchemy import text

        for _ in range(100):
            async with importer.db._engine.connect() as conn:
                blocked = await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event = 'advisory'"
                    )
                )
            if blocked:
                break
            await asyncio.sleep(0.01)
        assert blocked and not pending.done()
    assert (await pending)["counts"]["created"] == 1
    async with fence.note_path("p", "/legacy/notes/item-0.md") as mapping:
        assert mapping["ownership"] == "managed"
    async with fence.note_path("other-label", "/legacy/notes/item-0.md") as mapping:
        assert mapping["ownership"] == "managed"


def test_watchers_exclude_managed_export_namespace():
    assert managed_export_path("/vault/projects/p/knowledge/records/kn-test.md")
    assert not managed_export_path("/vault/projects/p/notes/knowledge.md")


async def test_physical_path_cannot_be_remapped_by_another_installation(importer):
    from src.knowledge.imports.manifest import canonical_json, sha256, verify_manifest
    from src.knowledge.imports.manifest import SealedManifest

    sealed = manifest()
    first = await importer.apply(**args(sealed))
    document = verify_manifest(sealed.content, sealed.sha256)
    document["source_installation_id"] = "second-installation"
    content = canonical_json(document)
    changed = SealedManifest(content, sha256(content))
    result = await importer.apply(**args(changed, "second-installation"))
    assert result["items"][0]["error_code"] == "knowledge_import.mapping_conflict"
    assert first["state"] == "succeeded"
