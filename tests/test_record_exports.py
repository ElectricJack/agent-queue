"""Confined files, exact recovery bytes, stale events and authorization."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from src.commands.principal import TRUSTED_LOCAL
from src.config import KnowledgeFeatureConfig
from src.database.tables import record_export_state, record_outbox
from src.knowledge.service import KnowledgeService
from src.records.export import ManagedFile, RecordExporter, managed_parts
from src.records.models import RecordError
from src.records.outbox import RecordOutbox
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal


@pytest.fixture
async def setup(reuse_database, tmp_path):
    db = await reuse_database()
    await seed_project(db)
    cfg = knowledge_config(export=KnowledgeFeatureConfig(enabled=True))
    service = KnowledgeService(db, cfg)
    created = await service.create(
        snapshot=snapshot(body="Original\r\nα"),
        principal=TRUSTED_LOCAL,
        project_id="p",
        idempotency_key="create",
    )
    now = [datetime.now(UTC) + timedelta(minutes=1)]
    exporter = RecordExporter(db, cfg, tmp_path, clock=lambda: now[0])
    outbox = RecordOutbox(db, cfg, exporter=exporter, clock=lambda: now[0])
    return db, cfg, service, created, now, exporter, outbox


def path_for(root, result):
    return root.joinpath(*managed_parts("project:p", result["knowledge_alias"]))


async def export_event(outbox):
    return next(
        event for event in await outbox.claim_due(limit=50) if event["destination"] == "export"
    )


async def test_managed_export_and_manual_bytes_match_without_plugins(setup, tmp_path):
    db, _, _, created, _, exporter, outbox = setup
    await outbox.tick()
    value = path_for(tmp_path, created).read_bytes()
    assert b"Original\r\n\xce\xb1" in value
    assert f'record_id: "{created["record_id"]}"'.encode() in value
    manual = await exporter.manual(
        identity=f"record:{created['record_id']}", principal=TRUSTED_LOCAL, project_id="p"
    )
    assert value == manual["content"].encode()
    async with db.immediate() as conn:
        state = (await conn.execute(select(record_export_state))).mappings().one()
        assert state["export_sha256"] == hashlib.sha256(value).hexdigest()
        assert state["sequence"] == 1


async def test_stale_event_cannot_replace_newer_export(setup, tmp_path):
    db, _, service, created, _, exporter, outbox = setup
    old = await export_event(outbox)
    updated = await service.update(
        identity=f"record:{created['record_id']}",
        patch={"body": "Newer export"},
        principal=TRUSTED_LOCAL,
        project_id="p",
        if_revision=created["revision_id"],
        idempotency_key="update",
    )
    await outbox.tick()
    newer = path_for(tmp_path, created).read_bytes()
    assert b"Newer export" in newer
    assert await exporter.deliver(old) == {"state": "superseded"}
    assert path_for(tmp_path, created).read_bytes() == newer
    async with db.immediate() as conn:
        assert (await conn.execute(select(record_export_state))).mappings().one()[
            "revision_id"
        ] == UUID(updated["revision_id"])


async def test_crash_after_rename_before_checkpoint_hash_recovers(setup, tmp_path, monkeypatch):
    db, _, _, created, now, exporter, outbox = setup
    event = await export_event(outbox)
    original = exporter._load
    calls = 0

    async def crash(event, *, conn):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("crash after rename")
        return await original(event, conn=conn)

    monkeypatch.setattr(exporter, "_load", crash)
    with pytest.raises(RuntimeError):
        await exporter.deliver(event)
    value = path_for(tmp_path, created).read_bytes()
    async with db.immediate() as conn:
        assert (await conn.execute(select(record_export_state))).first() is None
    monkeypatch.setattr(exporter, "_load", original)
    now[0] += timedelta(seconds=31)

    def no_rewrite(*args):
        raise AssertionError("recovery rewrote exact existing bytes")

    monkeypatch.setattr(ManagedFile, "publish", no_rewrite)
    replay = await export_event(outbox)
    assert await outbox.deliver(replay)
    assert path_for(tmp_path, created).read_bytes() == value


async def test_new_revision_recovers_prior_unacknowledged_rename(setup, tmp_path, monkeypatch):
    _, _, service, created, _, exporter, outbox = setup
    event = await export_event(outbox)
    original = exporter._load
    calls = 0

    async def crash(event, *, conn):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("lost checkpoint")
        return await original(event, conn=conn)

    monkeypatch.setattr(exporter, "_load", crash)
    with pytest.raises(RuntimeError):
        await exporter.deliver(event)
    monkeypatch.setattr(exporter, "_load", original)
    await service.update(
        identity=f"record:{created['record_id']}",
        patch={"body": "Second"},
        principal=TRUSTED_LOCAL,
        project_id="p",
        if_revision=created["revision_id"],
        idempotency_key="new-after-crash",
    )
    await outbox.tick()
    assert b"Second" in path_for(tmp_path, created).read_bytes()


async def test_human_edit_is_retained_and_marked_diverged(setup, tmp_path):
    db, _, service, created, _, _, outbox = setup
    await outbox.tick()
    path = path_for(tmp_path, created)
    edited = path.read_bytes() + b"\nHuman correction\n"
    path.write_bytes(edited)
    await service.update(
        identity=f"record:{created['record_id']}",
        patch={"body": "Database edit"},
        principal=TRUSTED_LOCAL,
        project_id="p",
        if_revision=created["revision_id"],
        idempotency_key="update",
    )
    await outbox.tick()
    assert path.read_bytes() == edited
    async with db.immediate() as conn:
        assert (await conn.execute(select(record_export_state))).mappings().one()["diverged_at"]
        event = (
            (
                await conn.execute(
                    select(record_outbox).where(
                        record_outbox.c.destination == "export",
                        record_outbox.c.delivered_at.is_(None),
                    )
                )
            )
            .mappings()
            .one()
        )
        assert event["last_error_code"] == "record.export_diverged"


@pytest.mark.parametrize("location", ["root", "parent", "leaf"])
async def test_symlinks_never_write_outside_vault(setup, tmp_path, location):
    _, _, _, created, _, exporter, outbox = setup
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"private")
    if location == "root":
        link = tmp_path / "linked-vault"
        link.symlink_to(outside, target_is_directory=True)
        exporter._vault_root = link
    elif location == "parent":
        (tmp_path / "projects").symlink_to(outside, target_is_directory=True)
    else:
        path = path_for(tmp_path, created)
        path.parent.mkdir(parents=True)
        path.symlink_to(sentinel)
    event = await export_event(outbox)
    assert not await outbox.deliver(event)
    assert sentinel.read_bytes() == b"private"
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel"]


async def test_edit_between_stage_and_rename_is_not_overwritten(setup, tmp_path, monkeypatch):
    _, _, _, created, _, _, outbox = setup
    original = ManagedFile.publish

    def edit_then_publish(managed, expected):
        path_for(tmp_path, created).write_bytes(b"Human edit racing publish")
        original(managed, expected)

    monkeypatch.setattr(ManagedFile, "publish", edit_then_publish)
    event = await export_event(outbox)
    assert not await outbox.deliver(event)
    assert path_for(tmp_path, created).read_bytes() == b"Human edit racing publish"


async def test_revision_advancing_during_stage_skips_old_rename(setup, tmp_path, monkeypatch):
    _, _, service, created, _, _, outbox = setup
    original = ManagedFile.stage
    staged = asyncio.Event()
    proceed = asyncio.Event()
    from src.records import export as module

    io = module._io

    async def pause(callback, *args):
        result = await io(callback, *args)
        if getattr(callback, "__func__", None) is original:
            staged.set()
            await proceed.wait()
        return result

    monkeypatch.setattr(module, "_io", pause)
    event = await export_event(outbox)
    delivery = asyncio.create_task(outbox.deliver(event))
    await asyncio.wait_for(staged.wait(), timeout=2)
    await service.update(
        identity=f"record:{created['record_id']}",
        patch={"body": "Advanced"},
        principal=TRUSTED_LOCAL,
        project_id="p",
        if_revision=created["revision_id"],
        idempotency_key="stage-race",
    )
    proceed.set()
    assert await delivery
    assert not path_for(tmp_path, created).exists()
    await outbox.tick()
    assert b"Advanced" in path_for(tmp_path, created).read_bytes()


async def test_read_only_manual_export_authorizes_exact_revision_and_hides_sources(setup):
    db, cfg, service, created, _, exporter, _ = setup
    other = await worker_principal(db, "reader", grants=["knowledge_export"])
    await service.update(
        identity=f"record:{created['record_id']}",
        patch={"body": "Current"},
        principal=TRUSTED_LOCAL,
        project_id="p",
        if_revision=created["revision_id"],
        idempotency_key="update",
    )
    cfg.writes_enabled = cfg.export.enabled = False
    exact = await exporter.manual(
        identity=f"record:{created['record_id']}",
        principal=other,
        project_id="p",
        revision_id=created["revision_id"],
    )
    assert "Original" in exact["content"] and "Current" not in exact["content"]
    with pytest.raises(RecordError, match="not_found"):
        await exporter.manual(
            identity=f"record:{created['record_id']}", principal=other, project_id="q"
        )
    with pytest.raises(RecordError, match="revision_unavailable"):
        await exporter.manual(
            identity=f"record:{created['record_id']}",
            principal=other,
            project_id="p",
            revision_id="00000000-0000-0000-0000-000000000001",
        )


async def test_manual_export_removes_inaccessible_task_evidence(setup):
    db, _, service, _, _, exporter, _ = setup
    source = await worker_principal(db, "source-worker")
    reader = await worker_principal(db, "other-reader", grants=["knowledge_export"])
    created = await service.create(
        snapshot=snapshot(
            sources=[
                {
                    "source_id": "private-evidence",
                    "kind": "task",
                    "task_id": source.task_id,
                }
            ]
        ),
        principal=TRUSTED_LOCAL,
        project_id="p",
        idempotency_key="private-source",
    )
    result = await exporter.manual(
        identity=f"record:{created['record_id']}", principal=reader, project_id="p"
    )
    assert source.task_id not in result["content"]
    assert "private-evidence" not in result["content"]


@pytest.mark.parametrize("scope", ["project:../p", "project:a/b", "project:a\\b", "project:.."])
def test_managed_path_rejects_traversal(scope):
    with pytest.raises(RecordError):
        managed_parts(scope, "kn-" + "a" * 32)


async def test_missing_vault_keeps_committed_record_readable(setup, tmp_path):
    db, _, service, created, _, exporter, outbox = setup
    exporter._vault_root = tmp_path / "missing-vault"
    await outbox.tick()
    shown = await service.show(
        identity=f"record:{created['record_id']}", principal=TRUSTED_LOCAL, project_id="p"
    )
    assert shown["revision_id"] == created["revision_id"]
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
    assert row["delivered_at"] is None and row["last_error_code"] == "record.export_filesystem"


async def test_cancellation_during_file_acquisition_releases_lock(tmp_path):
    import threading

    from src.records.export import _io

    alias = "kn-" + "a" * 32
    acquired = threading.Event()
    release = threading.Event()

    def acquire():
        managed = ManagedFile(tmp_path, "project:p", alias)
        acquired.set()
        release.wait(2)
        return managed

    task = asyncio.create_task(_io(acquire))
    assert await asyncio.to_thread(acquired.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # A new exporter can acquire the same lock after the interrupted open.
    managed = ManagedFile(tmp_path, "project:p", alias)
    managed.close()


async def test_expired_lease_after_staging_cannot_rename(setup, tmp_path, monkeypatch):
    _, _, _, created, now, _, outbox = setup
    from src.records import export as module

    io = module._io

    async def expire(callback, *args):
        result = await io(callback, *args)
        if getattr(callback, "__name__", "") == "stage":
            now[0] += timedelta(seconds=31)
        return result

    monkeypatch.setattr(module, "_io", expire)
    event = await export_event(outbox)
    assert not await outbox.deliver(event)
    assert not path_for(tmp_path, created).exists()


async def test_managed_delivery_keeps_database_transactions_out_of_file_io(setup, monkeypatch):
    db, _, _, _, _, _, outbox = setup
    from contextlib import asynccontextmanager
    from src.records import export as module

    immediate = db.immediate
    io = module._io
    active = 0

    @asynccontextmanager
    async def observe():
        nonlocal active
        async with immediate() as conn:
            active += 1
            try:
                yield conn
            finally:
                active -= 1

    async def assert_outside(callback, *args):
        assert active == 0, "filesystem operation inside a record transaction"
        return await io(callback, *args)

    monkeypatch.setattr(db, "immediate", observe)
    monkeypatch.setattr(module, "_io", assert_outside)
    event = await export_event(outbox)
    assert await outbox.deliver(event)


async def test_export_command_rejects_destinations_and_returns_bytes(command_handler_factory):
    handler = await command_handler_factory()
    await seed_project(handler.db)
    handler.config.knowledge = knowledge_config()
    created = await handler.execute(
        "knowledge_create",
        {
            "project_id": "p",
            "title": "T",
            "body": "B",
            "category": "note",
            "idempotency_key": "export-command",
        },
    )
    result = await handler.execute(
        "knowledge_export",
        {
            "project_id": "p",
            "identity": f"record:{created['record_id']}",
        },
    )
    assert result["success"] and result["content"].startswith("---\n")
    from pydantic import ValidationError
    from src.commands.contracts.knowledge import KnowledgeExportArgs

    with pytest.raises(ValidationError):
        KnowledgeExportArgs(
            project_id="p", identity=f"record:{created['record_id']}", destination="../../notes.md"
        )
