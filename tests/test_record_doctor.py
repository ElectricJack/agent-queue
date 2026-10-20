"""Read-only diagnostics remain useful without optional memory plugins."""

from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text, update

from src.commands.principal import TRUSTED_LOCAL
from src.config import AppConfig, KnowledgeFeatureConfig
from src.database.tables import knowledge_search, record_export_state, record_outbox, sessions
from src.doctor import default_registry
from src.doctor.models import DoctorContext, Severity
from src.doctor.record_checks import record_checks
from src.doctor.runner import DoctorRegistry, run_doctor
from src.knowledge.service import KnowledgeService
from src.models import AgentProfile
from src.records.export import RecordExporter, managed_parts
from src.records.outbox import RecordOutbox
from tests.record_helpers import (
    create_knowledge,
    knowledge_config,
    seed_project,
    seed_task,
    snapshot,
    worker_principal,
)


@pytest.fixture
async def ctx(reuse_database, tmp_path):
    db = await reuse_database()
    cfg = AppConfig(data_dir=str(tmp_path))
    from pathlib import Path

    Path(cfg.vault_root).mkdir()
    cfg.knowledge = knowledge_config(export=KnowledgeFeatureConfig(enabled=True))
    await seed_project(db)

    # Any accidental optional-plugin access is a test failure.
    class OfflinePlugins:
        def __getattr__(self, name):
            raise AssertionError(f"doctor touched optional plugin: {name}")

    return DoctorContext(config=cfg, db=db, handler=SimpleNamespace(orchestrator=OfflinePlugins()))


async def report(ctx, name):
    return await next(c for c in record_checks() if c.id == f"records.{name}").run(ctx)


async def create(ctx):
    return await KnowledgeService(ctx.db, ctx.config.knowledge).create(
        snapshot=snapshot(), principal=TRUSTED_LOCAL, project_id="p", idempotency_key="create"
    )


def test_checks_registered_report_only():
    registry = default_registry()
    for check in record_checks():
        assert registry.get(check.id)
        assert check.fix is None


async def test_offline_plugins_and_feature_off_checks_do_not_write(ctx):
    await create(ctx)
    ctx.config.knowledge.enabled = False
    registry = DoctorRegistry()
    for check in record_checks():
        registry.register(check)
    result = await run_doctor(registry, ctx, fix=True)
    assert result["exit_code"] == 0, result
    assert result["summary"]["fixes_applied"] == 0
    async with ctx.db.immediate() as conn:
        events = (await conn.execute(select(record_outbox))).mappings().all()
        assert len(events) == 2 and all(row["attempts"] == 0 for row in events)


async def test_missing_and_deleted_task_domains_are_distinct(ctx):
    from src.models import Task

    await ctx.db.create_task(Task(id="unmapped", project_id="p", title="Work", description=""))
    assert (await report(ctx, "task_mappings")).data["count"] == 1
    await seed_task(ctx.db, "mapped")
    await ctx.db.delete_task("mapped")
    result = await report(ctx, "domains")
    assert result.severity is Severity.WARN and result.data["count"] == 1


async def test_revision_hash_divergence_reports_exact_retained_bytes(ctx):
    async with ctx.db.immediate() as conn:
        await create_knowledge(ctx.db, conn, doc=snapshot())  # intentional synthetic wrong hash
    result = await report(ctx, "revision_hashes")
    assert result.severity is Severity.ERROR and result.data["count"] == 1


async def test_export_divergence_and_missing_file_report_without_repair(ctx):
    created = await create(ctx)
    exporter = RecordExporter(ctx.db, ctx.config.knowledge, ctx.config.vault_root)
    await RecordOutbox(ctx.db, ctx.config.knowledge, exporter=exporter).tick()
    from pathlib import Path

    path = Path(ctx.config.vault_root).joinpath(
        *managed_parts("project:p", created["knowledge_alias"])
    )
    path.write_bytes(b"human edit")
    assert (await report(ctx, "exports")).data["count"] == 1
    assert path.read_bytes() == b"human edit"
    async with ctx.db.immediate() as conn:
        assert (await conn.execute(select(record_export_state))).mappings().one()[
            "diverged_at"
        ] is None
    path.unlink()
    assert (await report(ctx, "exports")).data["count"] == 1


async def test_lexical_lag_does_not_initialize_semantic_provider(ctx):
    await create(ctx)
    async with ctx.db.immediate() as conn:
        # Simulate a damaged restored projection on this disposable database;
        # restore the guard before checking, with no production schema edits.
        await conn.execute(text("ALTER TABLE knowledge_search DISABLE TRIGGER USER"))
        await conn.execute(delete(knowledge_search))
        await conn.execute(text("ALTER TABLE knowledge_search ENABLE TRIGGER USER"))
    result = await report(ctx, "index_lag")
    assert result.data["count"] == 1
    assert result.data["optional_provider_initialized"] is False


async def test_dangling_task_link_diagnosed_without_execution_rewrites(ctx):
    from src.records.service import RecordService

    source = await seed_task(ctx.db, "source")
    await seed_task(ctx.db, "target")
    service = RecordService(ctx.db, ctx.config.knowledge)
    shown = await service.show(identity="task:source", principal=TRUSTED_LOCAL, project_id="p")
    await service.mutate_links(
        identity="task:source",
        operations=[
            {
                "action": "add",
                "target": "task:target",
                "link_type": "references",
            }
        ],
        principal=TRUSTED_LOCAL,
        project_id="p",
        idempotency_key="link",
        if_link_token=shown["link_token"],
    )
    await ctx.db.delete_task("target")
    assert (await report(ctx, "links")).data["count"] == 1
    async with ctx.db.immediate() as conn:
        assert await ctx.db.get_record_on(record_id=source["record_id"], conn=conn)


async def test_missing_database_is_info_not_false_health():
    ctx = DoctorContext(config=AppConfig())
    for check in record_checks():
        assert (await check.run(ctx)).severity is Severity.INFO


async def test_pool_access_probes_real_scope_and_reports_missing_grants(command_handler_factory):
    handler = await command_handler_factory()
    handler.config.knowledge = knowledge_config()
    worker = await worker_principal(handler.db, grants=["knowledge_list"])
    await handler.db.create_profile(AgentProfile(
        id=worker.profile_id, name="Pool probe", harness_tools=[], plugin_tools=[],
        aq_commands=["knowledge_list", "knowledge_show", "knowledge_create", "knowledge_cite"],
    ))
    async with handler.db.immediate() as conn:
        await conn.execute(update(sessions).where(sessions.c.id == worker.session_id)
                           .values(claim_phase="active"))
    ctx = DoctorContext(config=handler.config, db=handler.db, handler=handler)
    result = await report(ctx, "pool_access")
    assert result.severity is Severity.OK
    assert result.data["sampled"] == 1
    assert worker.session_instance_token not in str(result.to_dict())

    await handler.db.update_profile(worker.profile_id, aq_commands=["knowledge_show"])
    handler._invalidate_principal_cache()
    result = await report(ctx, "pool_access")
    assert result.severity is Severity.WARN
    failure, = result.data["failures"]
    assert failure["error_code"] == "record.forbidden"
    assert "knowledge_list" in failure["missing_grants"]
    assert worker.session_instance_token not in str(result.to_dict())


async def test_pool_access_no_live_claims_is_not_false_health(ctx):
    ctx.handler = SimpleNamespace(_principal_from_scope=lambda _: None)
    result = await report(ctx, "pool_access")
    assert result.severity is Severity.INFO
    assert result.detail == "no active pool claims to probe"
