"""D1 state boundaries and transaction races against disposable PostgreSQL."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select, update

from src.commands.handler import CommandHandler
from src.commands.playbook_state_commands import STATE_SCHEMAS
from src.commands.principal import (
    TRUSTED_LOCAL, ExecutionPrincipal, PrincipalKind, principal_context,
)
from src.config import AppConfig
from src.database import Database
from src.database import tables as t
from src.database.queries import playbook_state_queries as queries
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.definition import PlaybookDefinition, scope_from_v1, source_digest
from src.playbooks.invocation import _invocation_context
from src.playbooks.private_values import PrivateValueBatch, PrivateValueError, PrivateValueService
from src.playbooks.state import Limits, Namespace, StateError, StateService
from src.profiles.capabilities import CapabilityPolicy
from tests.db_fixtures import lease_dsn
from tests.playbook_v2_engine_helpers import artifact_ref_for, minimal_artifact

NS = Namespace("system", "system", "state-test")
SECRET = {"private": "do-not-persist-me-plaintext"}


@pytest.fixture
async def db():
    database = Database(lease_dsn("playbook-state"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def service(db):
    private = PrivateValueService(db._engine, active_key_id="test", keys={"test": b"x" * 32})
    db.set_playbook_private_values(private)
    return StateService(db._engine, private)


async def put(service, *, key="k", revision=0, operation="op", value=SECRET, namespace=NS, **kw):
    return await service.mutate(namespace, TRUSTED_LOCAL, action="set", key=key,
                                expected_revision=revision, operation_key=operation, value=value, **kw)


async def counts(db):
    async with db._engine.connect() as conn:
        return [await conn.scalar(select(func.count()).select_from(table)) for table in (
            t.playbook_state_namespaces, t.playbook_state_entries,
            t.playbook_state_operations, t.playbook_private_values)]


async def test_cas_tombstones_expiry_and_original_replay(db, service):
    first = await put(service, ttl_seconds=1)
    assert first["revision"] == 1 and first["exists"]
    assert (await service.get(NS, TRUSTED_LOCAL, key="k"))["value"] == SECRET
    # Force logical expiry without sleeping or deleting history.
    async with db.immediate() as conn:
        await conn.execute(update(t.playbook_state_entries).values(expires_at=1))
    expired = await service.get(NS, TRUSTED_LOCAL, key="k")
    assert expired["expired"] and expired["revision"] == 1 and not expired["exists"]
    assert expired["value"] is None
    assert await put(service, ttl_seconds=1) == first
    with pytest.raises(StateError, match="conflict"):
        await put(service, operation="new")
    second = await put(service, revision=1, operation="new")
    assert second["revision"] == 2
    removed = await service.mutate(NS, TRUSTED_LOCAL, action="delete", key="k",
                                   expected_revision=2, operation_key="delete")
    assert removed["revision"] == 3 and removed["tombstone"]
    assert await put(service, ttl_seconds=1) == first
    with pytest.raises(StateError, match="idempotency_conflict"):
        await put(service, value="changed", ttl_seconds=1)
    assert (await put(service, revision=3, operation="recreate"))["revision"] == 4
    missing = await service.mutate(NS, TRUSTED_LOCAL, action="delete", key="missing",
                                   expected_revision=0, operation_key="missing-delete")
    assert missing["revision"] == 1 and missing["tombstone"]
    async with db._engine.connect() as conn:
        for table in (t.playbook_state_namespaces, t.playbook_state_entries,
                      t.playbook_state_operations):
            assert SECRET["private"] not in str((await conn.execute(select(table))).all())
        assert all(SECRET["private"].encode() not in row for row in
                   (await conn.execute(select(t.playbook_private_values.c.ciphertext))).scalars())


async def test_concurrent_missing_entry_cas_and_operation_replay(db, service):
    results = await asyncio.gather(*(put(service, operation=str(i)) for i in range(8)),
                                   return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert all(isinstance(result, dict) or str(result) == "conflict" for result in results)
    assert (await counts(db))[:3] == [1, 1, 1]
    same = await asyncio.gather(*(put(service, key="same", operation="same") for _ in range(8)))
    assert all(result == same[0] for result in same)
    assert (await counts(db))[:3] == [1, 2, 2]


@pytest.mark.parametrize("limits", [Limits(keys=1), Limits(namespace_bytes=45)])
async def test_transactional_quota_race(db, service, limits):
    service.limits = limits
    results = await asyncio.gather(put(service, key="a", operation="a"),
                                   put(service, key="b", operation="b"), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(str(result) == "state.quota_exceeded" for result in results) == 1
    assert (await counts(db))[:3] == [1, 1, 1]


async def test_journal_quota_and_injected_failure_roll_back_everything(db, service, monkeypatch):
    service.limits = Limits(journal_bytes=1)
    with pytest.raises(StateError, match="quota_exceeded"):
        await put(service)
    assert await counts(db) == [0, 0, 0, 0]
    service.limits = Limits()
    original = PrivateValueBatch.persist

    async def fail_after_insert(self, conn):
        await original(self, conn)
        raise RuntimeError("injected rollback")

    with monkeypatch.context() as patch:
        patch.setattr(PrivateValueBatch, "persist", fail_after_insert)
        with pytest.raises(RuntimeError, match="rollback"):
            await put(service)
    assert await counts(db) == [0, 0, 0, 0]
    assert (await put(service))["revision"] == 1
    with pytest.raises(StateError, match="value_oversize"):
        await put(service, key="big", operation="big", value="x" * 262_144)
    with pytest.raises(StateError, match="reference_input_forbidden"):
        await put(service, value={"nested": {"__private_value__": "a" * 32}})


async def test_missing_keys_fail_before_state_write_and_readback(db, service):
    empty = StateService(db._engine, PrivateValueService(db._engine))
    with pytest.raises(PrivateValueError, match="key_unavailable"):
        await put(empty)
    assert await counts(db) == [0, 0, 0, 0]
    await put(service)
    with pytest.raises(PrivateValueError, match="key_unavailable"):
        await empty.get(NS, TRUSTED_LOCAL, key="k")


async def test_snapshot_revision_is_pre_scan_and_namespace_bound(db, service):
    await put(service)
    first = await service.list(NS, TRUSTED_LOCAL, page_size=1)
    assert first["identity_manifest"][0]["revision"] == 1 and first["complete"]
    assert "value" not in first["identity_manifest"][0]
    await put(service, revision=1, operation="two", value="new")
    page_args = dict(page_size=1, snapshot_id=first["snapshot_id"],
                     page_token=first["page_tokens"][0]["page_token"])
    page = await service.list(NS, TRUSTED_LOCAL, **page_args)
    assert page["entries"][0]["revision"] == 1
    assert page["entries"][0]["value"] == SECRET
    assert page["namespace_revision"] == 1
    from src.observations.snapshots import SnapshotError

    with pytest.raises(SnapshotError, match="token_mismatch"):
        await service.list(replace(NS, playbook_id="another"), TRUSTED_LOCAL, **page_args)
    # Literal prefix escaping: '%' must not act as a SQL wildcard.
    assert not (await service.list(NS, TRUSTED_LOCAL, prefix="%"))["identity_manifest"]


async def test_notification_metadata_is_typed_and_reset_preserves_history(db, service):
    original = await put(service)
    async with queries.locked_namespace(db._engine, NS) as (conn, locked):
        await conn.execute(insert(t.playbook_state_entries).values(
            namespace_id=NS.identifier, record_space="notification", key="k", revision=8,
            active=True, episode=3, source_partition={"project_id": "p", "source": "doctor"},
            first_seen=1, last_seen=2, last_notified=2, last_observed_at=2,
            last_observation_id="tick-2", message_id="m", created_at=1, updated_at=2))
        await conn.execute(update(t.playbook_state_namespaces).values(key_count=2))
    notification = await service.list(NS, TRUSTED_LOCAL, record_space="notification")
    manifest = notification["identity_manifest"][0]
    assert manifest["active"] and manifest["source_partition"]["project_id"] == "p"
    assert manifest["revision"] == 8
    reset = await service.reset(NS, TRUSTED_LOCAL, expected_revision=1,
                                reason="reviewed reset", operation_key="reset")
    assert "re-alert" in reset["warning"]
    assert await service.reset(NS, TRUSTED_LOCAL, expected_revision=1,
                                reason="reviewed reset", operation_key="reset") == reset
    assert await put(service) == original  # reset cannot turn an old operation into a fresh write
    tombstone = await service.get(NS, TRUSTED_LOCAL, key="k", record_space="notification")
    assert tombstone["revision"] == 9 and tombstone["tombstone"] and not tombstone["active"]
    assert tombstone["episode"] == 3 and tombstone["last_observation_id"] == "tick-2"
    with pytest.raises(StateError, match="retired_observation"):
        queries.check_observation_floor(reset, tombstone, 2)
    queries.check_observation_floor(reset, tombstone, reset["replay_floor"] + 1)
    async with db._engine.connect() as conn:
        row = await queries.namespace_row(conn, NS)
        assert row["value_bytes"] == 0 and row["key_count"] == 2
        assert row["journal_bytes"] > 0


async def test_schema_versions_require_explicit_provision_and_preserve_old_namespace(db, service):
    await put(service)
    new = replace(NS, state_schema_version=2)
    with pytest.raises(StateError, match="migration_required"):
        await put(service, namespace=new)
    await service.reset(new, TRUSTED_LOCAL, expected_revision=0,
                        operation_key="migration", reason="reviewed v2 migration, suppression resets")
    await put(service, namespace=new)
    assert (await service.get(NS, TRUSTED_LOCAL, key="k"))["revision"] == 1
    assert (await counts(db))[:3] == [2, 2, 3]


async def test_reset_preserves_later_replay_floors(db, service):
    await put(service)
    async with db.immediate() as conn:
        await conn.execute(update(t.playbook_state_namespaces).values(replay_floor=1e11))
        await conn.execute(update(t.playbook_state_entries).values(replay_floor=2e11))
    result = await service.reset(NS, TRUSTED_LOCAL, expected_revision=1,
                                 reason="reviewed", operation_key="reset")
    assert result["replay_floor"] == 1e11
    async with db._engine.connect() as conn:
        assert (await queries.namespace_row(conn, NS))["replay_floor"] == 1e11
        assert (await queries.entry_row(conn, NS, "kv", "k"))["replay_floor"] == 2e11


async def test_switching_back_to_never_created_version_one_requires_review(service):
    await service.reset(replace(NS, state_schema_version=2), TRUSTED_LOCAL, expected_revision=0,
                        reason="reviewed initial v2", operation_key="provision")
    with pytest.raises(StateError, match="migration_required"):
        await put(service)


@pytest.fixture
def command_context(db, service, tmp_path):
    config = AppConfig()
    handler = CommandHandler(SimpleNamespace(db=db, config=config, plugin_registry=None), config)
    source = "---\nid: state-test\nscope: project:p\ntriggers: [task.completed]\n---\nState.\n"
    raw = minimal_artifact().model_dump(mode="json")
    raw.update(id="state-test", scope={"type": "project", "project_id": "p"},
               source_hash=source_digest(source), trusted_execution={"state_schema_version": 1})
    base = raw["steps"]["ensure-review-task"]
    for name in STATE_SCHEMAS:
        raw["steps"][name] = {**base, "command": name}
    artifact = PlaybookDefinition.model_validate(raw)
    store = ArtifactStore(str(tmp_path))
    ref = artifact_ref_for(artifact)
    store.put(artifact, source_digest=ref.source_digest,
              contract_fingerprint=ref.contract_fingerprint, profile_fingerprint="",
              compiler_build=ref.compiler_build)
    store.put_source(ref.artifact_sha256, source)
    handler._v2_engine = lambda: SimpleNamespace(services=SimpleNamespace(artifact_store=store))
    ctx = SimpleNamespace(run_id="run", dispatch_id="dispatch", artifact_ref=ref, artifact=artifact,
                          rule_id="r", step_id="playbook_state_set", attempt=1, iteration_path="",
                          services=SimpleNamespace(private_values=service.private))
    principal = ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, project_id="p",
                                   policy=CapabilityPolicy(aq_commands=frozenset(STATE_SCHEMAS)))
    return handler, ctx, principal, store


async def execute(command_context, name, args, *, caller=None, context=True):
    handler, ctx, principal, _ = command_context
    with principal_context(caller or principal):
        if context:
            with _invocation_context(ctx):
                return await handler.execute(name, args)
        return await handler.execute(name, args)


async def test_commands_derive_namespace_reauthorize_replay_and_protect_readback(db, command_context):
    args = dict(key="x", value=SECRET, expected_revision=0, operation_key="same")
    first = await execute(command_context, "playbook_state_set", args)
    assert first["success"], first
    got = await execute(command_context, "playbook_state_get", {"key": "x"})
    assert got["value"] == SECRET
    assert await execute(command_context, "playbook_state_set", args) == first
    _, ctx, principal, _ = command_context
    for caller in (replace(principal, project_id="other"),
                   replace(principal, policy=CapabilityPolicy()), TRUSTED_LOCAL,
                   replace(principal, kind=PrincipalKind.SESSION, elevated=True)):
        result = await execute(command_context, "playbook_state_set", args, caller=caller)
        assert not result["success"], result
    assert not (await execute(command_context, "playbook_state_set", args, context=False))["success"]
    namespace = Namespace("project", "p", "state-test")
    async with db._engine.connect() as conn:
        row = await queries.entry_row(conn, namespace, "kv", "x")
    with _invocation_context(ctx), principal_context(principal):
        with pytest.raises(PrivateValueError, match="unauthorized"):
            await ctx.services.private_values.read(row["value_reference"], owner=namespace.owner,
                                                   principal=principal)
    assert (await counts(db))[:3] == [1, 1, 1]


@pytest.mark.parametrize("extra", [{"namespace": NS.as_dict()}, {"record_space": "notification"},
                                   {"ttl_seconds": 0}, {"expected_revision": True},
                                   {"value": float("nan")}, {"value": (1, 2)},
                                   {"value": {1: "non-string"}}, {"project_id": "p"}])
async def test_command_arguments_cannot_choose_authority_or_notification_space(db, command_context,
                                                                             extra):
    args = dict(key="x", value=SECRET, expected_revision=0, operation_key="op") | extra
    assert not (await execute(command_context, "playbook_state_set", args))["success"]
    assert await counts(db) == [0, 0, 0, 0]


async def test_unverified_source_and_tampered_metadata_fail_closed(db, command_context):
    handler, ctx, principal, store = command_context
    store.source_path_for(ctx.artifact_ref.artifact_sha256).write_text("changed source")
    result = await execute(command_context, "playbook_state_get", {"key": "k"})
    assert result["error_code"] == "state.invalid_pin"
    assert await counts(db) == [0, 0, 0, 0]


async def test_operator_inspection_and_reset_never_inherit_playbook_grants(db, command_context):
    args = dict(namespace=Namespace("project", "p", "state-test").as_dict(),
                expected_revision=0, reason="reviewed reset", operation_key="reset")
    assert (await execute(command_context, "playbook_state_reset", args))["error_code"] == "operator_only"
    result = await execute(command_context, "playbook_state_reset", args,
                           caller=TRUSTED_LOCAL, context=False)
    assert result["success"] and "re-alert" in result["warning"]
    inspect_args = dict(namespace=args["namespace"])
    assert (await execute(command_context, "playbook_state_inspect", inspect_args,
                           caller=TRUSTED_LOCAL, context=False))["success"]
    assert not (await execute(command_context, "playbook_state_inspect", inspect_args))["success"]


@pytest.mark.parametrize("scope", ["system", "agent-type:worker"])
async def test_system_and_profile_namespaces_follow_verified_scope(db, command_context, scope):
    handler, ctx, principal, store = command_context
    source = store.load_source(ctx.artifact_ref.artifact_sha256).replace("project:p", scope)
    raw = ctx.artifact.model_dump(mode="json")
    raw.update(scope=scope_from_v1(scope).model_dump(mode="json"), source_hash=source_digest(source))
    artifact = PlaybookDefinition.model_validate(raw)
    ref = artifact_ref_for(artifact)
    store.put(artifact, source_digest=ref.source_digest,
              contract_fingerprint=ref.contract_fingerprint, profile_fingerprint="",
              compiler_build=ref.compiler_build)
    store.put_source(ref.artifact_sha256, source)
    ctx.artifact, ctx.artifact_ref = artifact, ref
    principal = replace(principal, project_id=None, profile_id="worker")
    context = handler, ctx, principal, store
    args = dict(key="x", value=SECRET, expected_revision=0, operation_key="op")
    assert (await execute(context, "playbook_state_set", args))["success"]
    assert (await execute(context, "playbook_state_get", {"key": "x"}))["value"] == SECRET
    assert not (await execute(context, "playbook_state_set", args,
                              caller=replace(principal, project_id="p")))["success"]
    if scope != "system":
        assert not (await execute(context, "playbook_state_set", args,
                                  caller=replace(principal, profile_id="other")))["success"]
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(t.playbook_state_namespaces))).mappings().one()
        assert (row["scope_kind"], row["scope_owner"]) == (
            ("system", "system") if scope == "system" else ("profile", "worker"))


async def test_operator_private_values_and_reasons_never_enter_logs_or_bus(command_context, caplog):
    handler, _, _, _ = command_context
    handler.orchestrator.bus = SimpleNamespace(emit=AsyncMock())
    handler.config.events.command_invoked_enabled = True
    caplog.set_level("DEBUG", logger="src.commands.handler")
    args = dict(key="x", value=SECRET, expected_revision=0, operation_key="op")
    await execute(command_context, "playbook_state_set", args)
    namespace = Namespace("project", "p", "state-test").as_dict()
    inspected = await execute(command_context, "playbook_state_inspect",
                              dict(namespace=namespace, key="x"), caller=TRUSTED_LOCAL, context=False)
    assert inspected["value"] == SECRET
    reset = await execute(command_context, "playbook_state_reset",
                          dict(namespace=namespace, expected_revision=1,
                               operation_key="reset", reason="private-reset-reason"),
                          caller=TRUSTED_LOCAL, context=False)
    assert reset["success"]
    for output in (caplog.text, str(handler.orchestrator.bus.emit.await_args_list)):
        assert SECRET["private"] not in output
        assert "private-reset-reason" not in output
    assert handler.orchestrator.bus.emit.await_count == 3
