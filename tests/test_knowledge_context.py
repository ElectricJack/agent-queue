"""Shared selection, access rechecks, exact pins and conservative budgets."""

import logging
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update

from src.commands.principal import TRUSTED_LOCAL
from src.config import AppConfig
from src.database.tables import knowledge_citations, knowledge_context_bundles, tasks
from src.knowledge.budget import ContextBudget
from src.knowledge.context import (
    ContextService,
    live_bootstrap_principal,
    supervisor_bootstrap_principal,
)
from src.knowledge.models import ContextBundle
from src.knowledge.service import KnowledgeService
from src.models import AgentProfile, SessionRecord
from src.profiles.capabilities import CapabilityPolicy
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal


@pytest.fixture
async def context_setup(reuse_database):
    db = await reuse_database()
    config = AppConfig()
    config.knowledge = knowledge_config(global_enabled=True)
    config.memory.enabled = True
    config.knowledge.context.enabled = True
    config.knowledge.context.discovery_max_tokens = 4000
    await seed_project(db)
    service = ContextService(db, config)
    grants = ["knowledge_show", "knowledge_search", "knowledge_cite", "knowledge_context_deliver"]
    worker = await worker_principal(db, grants=grants)
    supervisor = await worker_principal(db, "supervisor", elevated=True, grants=grants)
    created = await KnowledgeService(db, config.knowledge).create(
        principal=TRUSTED_LOCAL, project_id="p", idempotency_key="create",
        snapshot=snapshot(summary="Observed incident evidence."),
    )
    return db, config, service, worker, supervisor, created


async def prepare(service, principal, **kwargs):
    return await service.prepare(principal=principal, required="Required instructions",
                                 project_ids=["p"], query="incident",
                                 claim_epoch=None if principal.elevated else 1, **kwargs)


async def test_same_worker_supervisor_payload_and_json_markdown_roundtrip(context_setup):
    db, _, service, worker, supervisor, created = context_setup
    a, b = await prepare(service, worker), await prepare(service, supervisor)
    assert a.items == b.items and a.to_markdown() == b.to_markdown()
    assert a.items[0].revision_id == created["revision_id"]
    assert a.items[0].content_sha256 == created["content_sha256"]
    assert ContextBundle.from_dict(a.to_dict()) == a
    assert "not session instructions or approval" in a.to_markdown()
    assert a.budget["fits"]
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_citations)) == 0


async def test_repeated_prime_inputs_reuse_but_refresh_and_scope_changes_do_not(context_setup):
    _, _, service, worker, _, _ = context_setup
    first = await prepare(service, worker)
    assert (await prepare(service, worker)).bundle_id == first.bundle_id
    assert (await prepare(service, worker, refresh=True)).bundle_id != first.bundle_id
    with pytest.raises(RecordError, match="record.not_found"):
        await service.prepare(principal=worker, required="required", project_ids=["q"],
                              claim_epoch=1, query="incident")


async def test_stale_claim_refuses_context_before_selection(context_setup):
    db, _, service, worker, _, _ = context_setup
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == worker.task_id).values(claim_epoch=2))
    with pytest.raises(RecordError, match="record.stale_claim"):
        await prepare(service, worker)


async def test_no_query_no_filler_and_disabled_master_never_retrieves(context_setup, monkeypatch):
    _, config, service, worker, _, _ = context_setup
    empty = await service.prepare(principal=worker, required="Required", project_ids=["p"],
                                  claim_epoch=1)
    assert not empty.items
    async def forbidden(*args, **kwargs):
        raise AssertionError("retrieval while disabled")
    monkeypatch.setattr("src.knowledge.context.lexical_search_on", forbidden)
    config.memory.enabled = False
    with pytest.raises(RecordError, match="context.disabled"):
        await prepare(service, worker)


async def test_required_overflow_persists_empty_diagnostic_and_refuses_delivery(context_setup):
    _, config, service, worker, _, _ = context_setup
    config.knowledge.context.input_max_tokens = 100
    bundle = await prepare(service, worker)
    assert not bundle.items and not bundle.budget["fits"]
    assert bundle.budget["diagnostic"] == "context.required_over_budget"
    with pytest.raises(RecordError, match="context.required_over_budget"):
        await service.observe_delivery(bundle_id=bundle.bundle_id, principal=worker,
                                       claim_epoch=1, transport="test", transport_key="overflow",
                                       rendered_sha256=bundle.content_sha256)


async def test_lower_budget_drops_whole_items_with_omissions(context_setup):
    _, config, service, worker, _, _ = context_setup
    full = await prepare(service, worker)
    low = await prepare(service, worker, budget=ContextBudget(
        input_tokens=8000, knowledge_tokens=100, knowledge_bytes=100,
    ))
    assert full.items and not low.items
    assert low.omissions[0]["reason"] == "knowledge_budget"
    assert low.budget["fits"]
    config.memory.context_max_tokens = 10
    assert not (await prepare(service, worker)).items


async def test_exact_old_pin_after_edit_is_never_substituted(context_setup):
    _, config, service, worker, _, created = context_setup
    identity = f"record:{created['record_id']}"
    await KnowledgeService(service.db, config.knowledge).update(
        principal=TRUSTED_LOCAL, project_id="p", identity=identity,
        patch={"body": "new body", "summary": "new summary"},
        if_revision=created["revision_id"], idempotency_key="edit",
    )
    bundle = await service.prepare(principal=worker, required="required", project_ids=["p"],
                                   claim_epoch=1, pins=[dict(identity=identity,
                                                            revision_id=created["revision_id"])])
    assert bundle.items[0].revision_id == created["revision_id"]
    assert bundle.items[0].text == "Observed incident evidence."


async def test_cross_project_pin_does_not_leak_private_metadata(context_setup):
    _, config, service, worker, _, _ = context_setup
    await seed_project(service.db, "q")
    secret = await KnowledgeService(service.db, config.knowledge).create(
        principal=TRUSTED_LOCAL, project_id="q", idempotency_key="secret",
        snapshot=snapshot(title="Private secret", body="Forbidden body"),
    )
    with pytest.raises(RecordError) as denied:
        await service.prepare(principal=worker, required="required", project_ids=["p"],
                              claim_epoch=1, pins=[dict(identity=f"record:{secret['record_id']}",
                                                       revision_id=secret["revision_id"])])
    assert denied.value.code == "record.not_found"
    assert "Private" not in str(denied.value) and "Forbidden" not in str(denied.value)


async def test_redaction_scrubs_stored_excerpt_and_refuses_delivery(context_setup):
    db, config, service, worker, _, created = context_setup
    bundle = await prepare(service, worker)
    await KnowledgeService(db, config.knowledge).redact(
        identity=f"record:{created['record_id']}", principal=TRUSTED_LOCAL, project_id="p",
        if_revision=created["revision_id"], idempotency_key="redact", reason_code="privacy",
        dry_run=False,
    )
    async with db.immediate() as conn:
        row = (await conn.execute(select(knowledge_context_bundles).where(
            knowledge_context_bundles.c.request_fingerprint == bundle.request_fingerprint,
        ))).mappings().one()
        assert row["selection"] == {} and row["redacted_at"] is not None
    with pytest.raises(RecordError, match="record.not_found"):
        await service.observe_delivery(bundle_id=bundle.bundle_id, principal=worker,
                                       claim_epoch=1, transport="test", transport_key="redacted",
                                       rendered_sha256=bundle.content_sha256)


async def test_bootstrap_is_narrow_and_global_has_no_default_private_scan(context_setup):
    db, _, service, worker, _, _ = context_setup
    profile = AgentProfile(id="supervisor", name="Supervisor",
                               aq_commands=["knowledge_show", "knowledge_search", "knowledge_share",
                                            "knowledge_context_deliver"])
    global_boot = supervisor_bootstrap_principal(
        profile, project_id=None, session_id="new-global", instance_token="global-instance",
    )
    empty = await service.prepare(principal=global_boot, required="bootstrap", query="incident")
    assert empty.items == () and empty.scope_keys == ()
    selected = await service.prepare(principal=global_boot, required="bootstrap",
                                     project_ids=["p"], query="incident")
    assert selected.items == (await prepare(service, worker)).items
    await db.create_session(SessionRecord(
        id="new-global", project_id=None, profile_id="supervisor", harness="codex", provider="fake",
        name="global", lifecycle="named", work_dir="/tmp", epoch="test",
        instance_token="global-instance", state="running", started_at=1,
    ))
    result = await service.observe_delivery(
        bundle_id=selected.bundle_id, principal=live_bootstrap_principal(global_boot),
        transport="start", transport_key="global-start", rendered_sha256=selected.content_sha256,
    )
    assert result["state"] == "delivered"
    project_boot = supervisor_bootstrap_principal(
        profile, project_id="p", session_id="new-project", instance_token="project-instance",
    )
    with pytest.raises(RecordError, match="record.not_found"):
        await service.prepare(principal=project_boot, required="bootstrap", project_ids=["q"])


async def test_grant_change_invalidates_replay(context_setup):
    _, _, service, worker, _, _ = context_setup
    bundle = await prepare(service, worker)
    narrowed = replace(worker, policy=CapabilityPolicy.from_namespaces(
        aq_commands=["knowledge_show", "knowledge_context_deliver"],
    ))
    with pytest.raises(RecordError, match="context.execution_changed"):
        await service.observe_delivery(bundle_id=bundle.bundle_id, principal=narrowed,
                                       claim_epoch=1, transport="test", transport_key="changed",
                                       rendered_sha256=bundle.content_sha256)


async def test_pool_token_without_fixed_task_binds_to_current_held_attempt(context_setup):
    _, _, service, worker, _, _ = context_setup
    pool_token = replace(worker, task_id=None)
    bundle = await prepare(service, pool_token)
    assert bundle.owner["task_id"] == worker.task_id
    await service.observe_delivery(bundle_id=bundle.bundle_id, principal=pool_token,
                                   claim_epoch=1, transport="cli", transport_key="pool",
                                   rendered_sha256=bundle.content_sha256)
    with pytest.raises(RecordError, match="record.stale_claim"):
        await prepare(service, pool_token, task_id="some-other-task")


async def test_revoked_global_share_is_absent_on_refresh_and_rejected_on_delivery(context_setup):
    db, config, service, worker, _, _ = context_setup
    from src.records.auth import GLOBAL_SCOPE

    knowledge = KnowledgeService(db, config.knowledge)
    created = await knowledge.create(
        principal=TRUSTED_LOCAL, project_id=GLOBAL_SCOPE, idempotency_key="global",
        snapshot=snapshot(title="Global incident", summary="Globally shared evidence"),
    )
    args = dict(principal=TRUSTED_LOCAL, project_id=GLOBAL_SCOPE,
                identity=f"record:{created['record_id']}", target_project_id="p",
                if_revision=created["revision_id"], reason="share synthetic evidence", dry_run=False)
    await knowledge.share(**args, idempotency_key="share")
    bundle = await prepare(service, worker)
    assert created["revision_id"] in {i.revision_id for i in bundle.items}
    await knowledge.share(**args, revoke=True, idempotency_key="revoke")
    with pytest.raises(RecordError, match="context.invalidated"):
        await service.observe_delivery(bundle_id=bundle.bundle_id, principal=worker,
                                       claim_epoch=1, transport="cli", transport_key="revoked",
                                       rendered_sha256=bundle.content_sha256)
    fresh = await prepare(service, worker)
    assert created["revision_id"] not in {i.revision_id for i in fresh.items}
    assert "Globally shared" not in fresh.to_markdown()


async def test_renderer_override_and_prompt_builder_use_same_bounded_bundle(context_setup):
    _, config, service, worker, _, _ = context_setup
    from src.prime.models import PrimeDocument, PrimeSection
    from src.prime.renderer import PrimeRenderer
    from src.prompt_builder import PromptBuilder

    bundle = await prepare(service, worker)
    doc = PrimeDocument(task_id=worker.task_id, session_id=worker.session_id,
                        rendered_at=datetime(2026, 10, 1, tzinfo=UTC),
                        work_dir="/tmp", source="override:.aq/PRIME.md",
                        sections=(PrimeSection("l2_context", "Topic context", ""),),
                        override_markdown="Required instructions")
    rendered = PrimeRenderer.with_context(doc, bundle)
    builder = PromptBuilder()
    builder.add_context("instructions", "Required instructions")
    builder.set_l1_facts("Legacy facts")
    builder.set_l1_guidance("Legacy guidance")
    builder.set_l2_context("Legacy topic")
    builder.set_context_bundle(bundle)
    assert builder.build_task_prompt() == rendered.to_markdown()
    assert rendered.to_markdown().count("Observed incident evidence.") == 1
    assert "Legacy" not in builder.build_task_prompt()
    builder.set_core_tools([{"description": "tool schema" * config.knowledge.context.input_max_tokens}])
    with pytest.raises(RecordError, match="context.required_over_budget"):
        builder.build()


@pytest.mark.parametrize("launch", ["success", "failure", "overflow"])
async def test_named_supervisor_launch_injects_worker_selection_only_after_success(
    context_setup, tmp_path, launch,
):
    db, config, service, worker, _, _ = context_setup
    from src.messages import SessionLens
    from src.sessions import SessionProviderRegistry
    from src.sessions.fake import FakeProvider
    from src.sessions.harness_parser import Harness
    from src.sessions.harness_registry import HarnessRegistry
    from src.sessions.spec import SessionSpecBuilder

    config.data_dir = str(tmp_path)
    config.sessions.provider = "fake"
    config.knowledge.context.supervisor_query = "incident"
    profile = AgentProfile(id="supervisor", name="Supervisor", harness="codex", lifecycle="named",
                           aq_commands=["knowledge_show", "knowledge_search",
                                        "knowledge_context_deliver"])
    async def load_profile(_):
        return profile
    harnesses = HarnessRegistry()
    harnesses.upsert(Harness(id="codex", name="Codex", command="codex", prompt_mode="arg"))
    providers = SessionProviderRegistry({"fake": FakeProvider}, config=config)
    provider = providers.create("fake")
    if launch == "failure":
        provider.script_start_error("n-supervisor--p", RuntimeError("synthetic startup failure"))
    if launch == "overflow":
        config.knowledge.context.input_max_tokens = 100
    lens = SessionLens(db=db, config=config, profiles_loader=load_profile, providers=providers,
                       harness_registry=harnesses, spec_builder=SessionSpecBuilder(config, harnesses),
                       epoch="context-test")
    ok = await lens.ensure_started(kind="session", target_id="supervisor-p", project_id="p")
    assert ok is (launch == "success")
    worker_bundle = await prepare(service, worker)
    async with db.immediate() as conn:
        rows = (await conn.execute(select(knowledge_context_bundles).where(
            knowledge_context_bundles.c.owner_kind == "supervisor_session",
        ))).mappings().all()
        assert len(rows) == 1
        supervisor_bundle = ContextBundle.from_dict(rows[0]["selection"])
        assert supervisor_bundle.items == worker_bundle.items
        count = await conn.scalar(select(func.count()).select_from(knowledge_citations))
        assert count == (len(worker_bundle.items) if launch == "success" else 0)
        if launch == "overflow":
            assert supervisor_bundle.budget["diagnostic"] == "context.required_over_budget"
            assert provider.starts == []
    if launch == "success":
        assert provider.starts[0].prompt.endswith(worker_bundle.to_markdown())


async def test_prime_degrades_to_the_document_when_the_profile_cannot_read_the_corpus(
    command_handler_factory, caplog,
):
    """``aq prime`` is a delivery path: the corpus is an optional extra.

    A profile without the ``knowledge_show`` grant — the shipped
    ``worker-opencode`` template shape — used to get
    ``Error: Command 'prime' failed: record.forbidden`` and therefore no role,
    rules, deliverables or completion protocol. It gets the whole document
    minus the knowledge section, and the refusal is logged, not returned.
    """
    from src.commands.principal import principal_context

    handler = await command_handler_factory()
    db, config = handler.db, handler.config
    config.knowledge = knowledge_config()
    config.knowledge.context.enabled = config.memory.enabled = True
    config.knowledge.context.discovery_max_tokens = 4000
    # ``worker_principal`` seeds the project record scope the corpus writes into.
    ungranted = await worker_principal(db, "ungranted", grants=["prime"])
    granted = await worker_principal(db, "granted", grants=[
        "prime", "knowledge_show", "knowledge_search", "knowledge_context_deliver",
    ])
    created = await KnowledgeService(db, config.knowledge).create(
        principal=TRUSTED_LOCAL, project_id="p", idempotency_key="prime-record",
        snapshot=snapshot(title="Held task", summary="Common retained knowledge"),
    )

    def scope_for(principal):
        return {"kind": "session", "session_id": principal.session_id, "project_id": "p",
                "session_instance_token": principal.session_instance_token,
                "task_id": None, "elevated": False}

    with (caplog.at_level(logging.WARNING, logger="src.commands.surface_commands"),
          principal_context(replace(ungranted, task_id=None))):
        refused = await handler.execute("prime", {"_scope": scope_for(ungranted)})
    assert refused["success"], refused
    assert "context_bundle" not in refused and "context_state" not in refused
    # The document is intact: same section set as a granted prime, and the
    # withheld evidence appears nowhere in it.
    assert [s["key"] for s in refused["sections"]]
    assert next(s for s in refused["sections"] if s["key"] == "l2_context")["body"] == ""
    assert created["record_id"] not in refused["body"]
    assert "Common retained knowledge" not in refused["body"]
    assert "Held task" in refused["body"]
    logged = [r for r in caplog.records if r.name == "src.commands.surface_commands"]
    assert len(logged) == 1
    assert "record.forbidden" in logged[0].getMessage()
    assert "knowledge_show" in logged[0].getMessage()
    assert f"profile={ungranted.profile_id}" in logged[0].getMessage()

    with principal_context(replace(granted, task_id=None)):
        served = await handler.execute("prime", {"_scope": scope_for(granted)})
    assert served["success"], served
    assert served["context_state"] == "prepared"
    assert [s["key"] for s in served["sections"]] == [s["key"] for s in refused["sections"]]
    assert "Common retained knowledge" in served["body"]


async def test_prime_still_fails_a_malformed_bundle_request_or_a_claim_fence(
    command_handler_factory, monkeypatch,
):
    """Degrading is for corpus refusals only — not for a fence on the claim."""
    from src.records.models import RecordError

    handler = await command_handler_factory()
    db, config = handler.db, handler.config
    config.knowledge = knowledge_config()
    config.knowledge.context.enabled = config.memory.enabled = True
    worker = await worker_principal(db, "fenced", grants=[
        "prime", "knowledge_show", "knowledge_search",
    ])
    worker = replace(worker, task_id=None)
    scope = {"kind": "session", "session_id": worker.session_id, "project_id": "p",
             "session_instance_token": worker.session_instance_token, "task_id": None,
             "elevated": False}

    from src.commands.principal import principal_context

    class Refusing:
        async def prepare(self, **kwargs):
            raise RecordError("record.stale_claim")

    monkeypatch.setattr(handler, "_knowledge_context_service", lambda: Refusing())
    with principal_context(worker):
        refused = await handler.execute("prime", {"_scope": scope})
    assert refused["error_code"] == "record.stale_claim", refused


async def test_prime_command_returns_prepared_bundle_and_delivery_command_records_usage(
    command_handler_factory,
):
    from src.commands.principal import principal_context

    handler = await command_handler_factory()
    db, config = handler.db, handler.config
    config.knowledge = knowledge_config()
    config.knowledge.context.enabled = config.memory.enabled = True
    config.knowledge.context.discovery_max_tokens = 4000
    worker = await worker_principal(db)
    worker = replace(worker, task_id=None, policy=CapabilityPolicy.from_namespaces(
        aq_commands=[*worker.policy.aq_commands, "prime", "knowledge_context_deliver"],
    ))
    await KnowledgeService(db, config.knowledge).create(
        principal=TRUSTED_LOCAL, project_id="p", idempotency_key="prime-record",
        snapshot=snapshot(title="Held task", summary="Common retained knowledge"),
    )
    scope = dict(kind="session", session_id=worker.session_id, project_id="p",
                 session_instance_token=worker.session_instance_token, task_id=None, elevated=False)
    with principal_context(worker):
        result = await handler.execute("prime", {"_scope": scope})
        assert result["success"], result
        bundle = ContextBundle.from_dict(result["context_bundle"])
        assert bundle.items and result["body"].endswith(bundle.to_markdown())
        assert result["context_state"] == "prepared"
        repeated = await handler.execute("prime", {"_scope": scope})
        assert repeated["context_bundle"]["bundle_id"] == bundle.bundle_id
        async with db.immediate() as conn:
            assert await conn.scalar(select(func.count()).select_from(knowledge_citations)) == 0
        acknowledged = await handler.execute("knowledge_context_deliver", dict(
            _scope=scope, bundle_id=bundle.bundle_id, transport="cli", idempotency_key="prime",
            rendered_sha256=bundle.content_sha256, claim_epoch=1,
        ))
        assert acknowledged["success"], acknowledged
        config.knowledge.context.input_max_tokens = 100
        refused = await handler.execute("prime", {"_scope": scope})
        assert refused["error_code"] == "context.required_over_budget"
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_citations)) == 1
