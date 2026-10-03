"""Core access remains fail closed with legacy capability enforcement disabled."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update

from src.commands.principal import ExecutionPrincipal, TRUSTED_LOCAL
from src.config import AppConfig, KnowledgeConfig, load_config
from src.database.tables import knowledge_revisions, projects, record_requests, sessions, tasks
from src.knowledge.service import KnowledgeService
from src.profiles.capabilities import DENY_ALL
from src.records.models import RecordError
from src.records.service import RecordService
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal


@pytest.fixture
async def context(reuse_database):
    db = await reuse_database()
    worker = await worker_principal(db)
    other = await worker_principal(db, "other")
    supervisor = await worker_principal(db, "supervisor", elevated=True)
    await seed_project(db, "q")
    service = KnowledgeService(db, knowledge_config())
    return service, worker, other, supervisor


async def create(service, worker, key="new", **changes):
    return await service.create(
        snapshot=snapshot(**changes),
        principal=worker,
        project_id="p",
        claim_epoch=1,
        idempotency_key=key,
    )


def ident(result):
    return f"record:{result['record_id']}"


async def test_workers_read_project_knowledge_but_only_edit_own(context):
    service, worker, other, supervisor = context
    record = await create(service, worker)
    assert (await service.show(identity=ident(record), principal=other, project_id="p"))["success"]
    with pytest.raises(RecordError) as exc:
        await service.update(
            identity=ident(record),
            patch={"body": "hijack"},
            principal=other,
            project_id="p",
            claim_epoch=1,
            if_revision=record["revision_id"],
            idempotency_key="hijack",
        )
    assert exc.value.code == "record.forbidden"
    assert (
        await service.update(
            identity=ident(record),
            patch={"body": "supervisor edit"},
            principal=supervisor,
            project_id="p",
            if_revision=record["revision_id"],
            idempotency_key="supervisor",
        )
    )["sequence"] == 2


async def test_denied_unknown_and_global_have_no_private_tokens(context):
    service, worker, _, supervisor = context
    foreign = await service.create(
        snapshot=snapshot(title="secret"),
        principal=TRUSTED_LOCAL,
        project_id="q",
        idempotency_key="q",
    )
    errors = []
    for identity in [ident(foreign), f"record:{uuid4()}"]:
        with pytest.raises(RecordError) as exc:
            await service.show(identity=identity, principal=worker, project_id="p")
        errors.append(exc.value.result())
    assert (
        errors[0]
        == errors[1]
        == {"success": False, "error_code": "record.not_found", "error": "record.not_found"}
    )
    with pytest.raises(RecordError) as exc:
        await service.search(principal=supervisor, project_id="q")
    assert exc.value.code == "record.not_found"
    with pytest.raises(RecordError) as exc:
        await service.create(
            snapshot=snapshot(), principal=TRUSTED_LOCAL, project_id=None, idempotency_key="global"
        )
    assert exc.value.code == "knowledge.operation_unavailable"


@pytest.mark.parametrize("change", ["no-grant", "service", "bad-instance", "bad-claim", "ended"])
async def test_no_dispatch_bypass_and_live_identity_required(context, change):
    service, worker, _, _ = context
    claim = 1
    if change == "no-grant":
        worker = replace(worker, policy=DENY_ALL)
    elif change == "service":
        worker = ExecutionPrincipal.service("extractor")
    elif change == "bad-instance":
        worker = replace(worker, session_instance_token="old")
    elif change == "bad-claim":
        claim = 0
    else:
        async with service.db.immediate() as conn:
            await conn.execute(
                update(sessions).where(sessions.c.id == worker.session_id).values(ended_at=100)
            )
    with pytest.raises(RecordError) as exc:
        await service.create(
            snapshot=snapshot(),
            principal=worker,
            project_id="p",
            claim_epoch=claim,
            idempotency_key="denied",
        )
    assert exc.value.code in {"record.forbidden", "record.stale_claim"}
    async with service.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(record_requests)) == 0


async def test_replay_rechecks_claim_project_and_grant(context):
    service, worker, _, _ = context
    await create(service, worker)
    with pytest.raises(RecordError) as exc:
        await create(service, replace(worker, policy=DENY_ALL))
    assert exc.value.code == "record.forbidden"
    async with service.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == worker.task_id).values(claim_epoch=2))
        await conn.execute(
            update(sessions).where(sessions.c.id == worker.session_id).values(last_claim_epoch=2)
        )
    with pytest.raises(RecordError) as exc:
        await create(service, worker)
    assert exc.value.code == "record.stale_claim"
    # A new claim cannot replay an earlier claim's create receipt.
    new = await service.create(
        snapshot=snapshot(), principal=worker, project_id="p", claim_epoch=2, idempotency_key="new"
    )
    assert new["outcome"] == "created"


async def test_generic_task_resolution_and_link_targets_preserve_task_scope(context):
    service, worker, other, _ = context
    records = RecordService(service.db, service.config)
    local = dict(principal=TRUSTED_LOCAL, project_id="p")
    foreign_task = await records.show(identity=f"task:{other.task_id}", **local)
    record = await create(service, worker)
    for identity in [f"task:{other.task_id}", ident(foreign_task)]:
        with pytest.raises(RecordError) as exc:
            await records.show(identity=identity, principal=worker, project_id="p")
        assert exc.value.code == "record.not_found"
        with pytest.raises(RecordError) as exc:
            await records.mutate_links(
                identity=ident(record),
                operations=[dict(action="add", link_type="references", target=identity)],
                principal=worker,
                project_id="p",
                claim_epoch=1,
                if_revision=record["revision_id"],
                idempotency_key="bad-target",
            )
        assert exc.value.code == "record.not_found"


async def test_historical_link_and_source_filtering_under_current_access(context):
    service, worker, other, supervisor = context
    records = RecordService(service.db, service.config)
    record = await create(
        service,
        supervisor,
        sources=[{"source_id": "task", "kind": "task", "task_id": other.task_id}],
    )
    linked = await records.mutate_links(
        identity=ident(record),
        operations=[dict(action="add", link_type="references", target=f"task:{other.task_id}")],
        principal=supervisor,
        project_id="p",
        if_revision=record["revision_id"],
        idempotency_key="link",
    )
    shown = await service.show(
        identity=ident(record), revision_id=linked["revision_id"], principal=worker, project_id="p"
    )
    assert shown["snapshot"]["sources"] == shown["snapshot"]["outgoing_links"] == []
    assert other.task_id not in str(shown)
    allowed = await service.show(identity=ident(record), principal=supervisor, project_id="p")
    assert len(allowed["snapshot"]["sources"]) == len(allowed["snapshot"]["outgoing_links"]) == 1


async def test_private_sources_cannot_be_attached_or_read_by_cross_project(context):
    service, worker, other, _ = context
    with pytest.raises(RecordError) as exc:
        await create(
            service,
            worker,
            sources=[{"source_id": "stolen", "kind": "task", "task_id": other.task_id}],
        )
    assert exc.value.code == "record.not_found"


async def test_feature_off_never_calls_provider_or_affects_tasks(context, monkeypatch):
    service, worker, _, _ = context
    from src.plugins.registry import PluginRegistry

    fail = AsyncMock(side_effect=AssertionError("optional plugin initialization"))
    monkeypatch.setattr(PluginRegistry, "load_plugin", fail)
    service.config.enabled = False
    with pytest.raises(RecordError) as exc:
        await create(service, worker)
    assert exc.value.code == "knowledge.disabled"
    task = await service.db.get_task(worker.task_id)
    assert task.status.value == "IN_PROGRESS"
    assert len(await service.db.list_tasks(project_id="p")) == 2
    service.config.enabled = True
    record = await create(service, worker)
    assert (await service.search(principal=worker, project_id="p"))["items"][0][
        "record_id"
    ] == record["record_id"]
    fail.assert_not_awaited()


async def test_legacy_conflict_never_imports_or_initializes_plugin(context):
    service, worker, _, _ = context
    from src.plugins.registry import PluginRegistry

    registry = PluginRegistry(db=MagicMock(), bus=MagicMock(), config=AppConfig())
    registry._db.get_plugin = AsyncMock(side_effect=AssertionError("must refuse before loading"))
    for name in ("memory", "aq-memory"):
        with pytest.raises(RecordError) as exc:
            await registry.load_plugin(name)
        assert exc.value.code == "knowledge.legacy_writer_conflict"
    registry._db.get_plugin.assert_not_awaited()
    service.active_legacy_scopes = lambda: frozenset({"project:p"})
    with pytest.raises(RecordError) as exc:
        await create(service, worker)
    assert exc.value.code == "knowledge.legacy_writer_conflict"
    assert (await service.db.get_task(worker.task_id)).status.value == "IN_PROGRESS"


def test_config_defaults_and_nested_yaml(tmp_path):
    config = AppConfig()
    assert config.knowledge == KnowledgeConfig()
    assert not config.memory.enabled
    path = tmp_path / "config.yaml"
    path.write_text(
        "messaging_platform: none\ndatabase:\n  url: postgresql://test@localhost/test\n"
        "knowledge:\n  enabled: true\n  enabled_projects: [p]\n  writes_enabled: true\n"
        "  export:\n    enabled: true\n",
        encoding="utf-8",
    )
    loaded = load_config(str(path))
    assert loaded.knowledge.enabled and loaded.knowledge.export.enabled
    assert loaded.knowledge.enabled_projects == ["p"]
    assert not loaded.knowledge.context.enabled and not loaded.memory.enabled


async def test_missing_project_denies_existing_knowledge(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    service = KnowledgeService(db, knowledge_config())
    record = await service.create(
        snapshot=snapshot(), principal=TRUSTED_LOCAL, project_id="p", idempotency_key="new"
    )
    async with db.immediate() as conn:
        await conn.execute(delete(projects).where(projects.c.id == "p"))
    with pytest.raises(RecordError) as exc:
        await service.show(identity=ident(record), principal=TRUSTED_LOCAL, project_id="p")
    assert exc.value.code == "record.not_found"


async def test_claim_revoked_during_write_rolls_back_without_task_lock(context, monkeypatch):
    service, worker, _, _ = context
    record = await create(service, worker)
    original = service._outbox

    async def revoke(*args, **kwargs):
        # Independent connection completes while the knowledge record is locked.
        async with service.db.immediate() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == worker.task_id).values(claim_epoch=2)
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_outbox", revoke)
    with pytest.raises(RecordError) as exc:
        await service.update(
            identity=ident(record),
            patch={"body": "must roll back"},
            principal=worker,
            project_id="p",
            claim_epoch=1,
            if_revision=record["revision_id"],
            idempotency_key="revoked",
        )
    assert exc.value.code == "record.stale_claim"
    async with service.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_revisions)) == 1
        assert await conn.scalar(select(func.count()).select_from(record_requests)) == 1


async def test_link_remove_requires_current_target_access(context):
    service, worker, other, supervisor = context
    record = await create(service, worker)
    records = RecordService(service.db, service.config)
    linked = await records.mutate_links(
        identity=ident(record),
        operations=[dict(action="add", link_type="references", target=f"task:{other.task_id}")],
        principal=supervisor,
        project_id="p",
        if_revision=record["revision_id"],
        idempotency_key="link",
    )
    links = await records.list_links(identity=ident(record), principal=supervisor, project_id="p")
    with pytest.raises(RecordError) as exc:
        await records.mutate_links(
            identity=ident(record),
            operations=[dict(action="remove", link_id=links["links"][0]["link_id"])],
            principal=worker,
            project_id="p",
            claim_epoch=1,
            if_revision=linked["revision_id"],
            idempotency_key="remove-hidden",
        )
    assert exc.value.code == "record.not_found"


async def test_no_protected_mutation_before_k05(context):
    from tests.record_helpers import create_knowledge

    service, worker, _, supervisor = context
    doc = snapshot(
        verification="verified",
        last_verified_at="2026-10-01T00:00:00.000000Z",
        last_verified_by="operator",
        sources=[{"source_id": "evidence", "kind": "task", "task_id": worker.task_id}],
    )
    with pytest.raises(RecordError) as exc:
        await create(service, worker, **doc)
    assert exc.value.code == "knowledge.operation_unavailable"
    async with service.db.immediate() as conn:
        record_id, revision_id = await create_knowledge(service.db, conn, doc=doc)
    for principal in (worker, supervisor, TRUSTED_LOCAL):
        with pytest.raises(RecordError) as exc:
            await service.update(
                identity=f"record:{record_id}",
                patch={"body": "overwrite"},
                principal=principal,
                project_id="p",
                claim_epoch=1,
                if_revision=revision_id,
                idempotency_key="protected",
            )
        assert exc.value.code == "knowledge.operation_unavailable"
