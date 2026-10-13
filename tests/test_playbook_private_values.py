"""Confidentiality boundaries exercised against disposable PostgreSQL."""

import base64
import json
from dataclasses import replace

import pytest
from sqlalchemy import func, select, update

from src.commands.principal import ExecutionPrincipal, PrincipalKind, TRUSTED_LOCAL
from src.config import PlaybooksConfig
from src.database.tables import (
    playbook_pending_events,
    playbook_private_values,
    playbook_v2_runs,
    playbook_waits,
)
from src.playbooks.executors.command import LiveCommandExecutor, PreviewCommandExecutor
from src.playbooks.graph_projection import project_value
from src.playbooks.invocation import _invocation_context
from src.playbooks.private_values import (
    PrivateOwner,
    PrivateValueError,
    PrivateValueService,
    is_reference,
)
from src.playbooks.run_state import LoopFrame, SnapshotVersionConflict
from src.playbooks.waits import WaitChangeSet, WaitSpec
from src.profiles.capabilities import DENY_ALL
from tests.fixtures.contracts.engine_contracts import GATE_CREATE, registry_with
from tests.test_command_executor import command_step, context
from tests.test_playbook_run_repository import (
    Event,
    db,
    make_receipt,
    make_snapshot,
)

__all__ = ["db"]
KEY = b"test-only-private-value-key-32!!!"[:32].ljust(32, b"!")
OWNER = PrivateOwner("run", "run-1")
SECRET = "never-publish-this-value"


@pytest.fixture
def service(db):
    service = PrivateValueService(db._engine, active_key_id="one", keys={"one": KEY})
    db.set_playbook_private_values(service)
    return service


async def save(db, service, value=SECRET, owner=OWNER, **kwargs):
    batch = service.batch(owner)
    ref = batch.protect(value, **kwargs)
    async with db.immediate() as conn:
        await batch.persist(conn)
    return ref


async def test_ciphertext_round_trip_rotation_and_no_plaintext(db, service):
    ref = await save(db, service, {"body": SECRET})
    assert SECRET not in str(ref)
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(playbook_private_values))).mappings().one()
    assert SECRET.encode() not in row["ciphertext"]
    assert len(row["nonce"]) == 12
    rotated = PrivateValueService(
        db._engine, active_key_id="two", keys={"one": KEY, "two": b"2" * 32}
    )
    assert await rotated.read(ref, owner=OWNER, principal=TRUSTED_LOCAL) == {"body": SECRET}
    assert await save(db, rotated) != ref
    missing = PrivateValueService(db._engine, active_key_id="two", keys={"two": b"2" * 32})
    with pytest.raises(PrivateValueError, match="key_unavailable"):
        await missing.read(ref, owner=OWNER, principal=TRUSTED_LOCAL)


@pytest.mark.parametrize(
    "column,value",
    [
        ("ciphertext", b"bad"),
        ("digest", "0" * 64),
        ("byte_count", 123),
        ("content_type", "application/octet-stream"),
        ("nonce", b"0" * 12),
    ],
)
async def test_authenticated_metadata_and_ciphertext(db, service, column, value):
    ref = await save(db, service)
    async with db.immediate() as conn:
        await conn.execute(update(playbook_private_values).values(**{column: value}))
    with pytest.raises(PrivateValueError, match="integrity_failed"):
        await service.read(ref, owner=OWNER, principal=TRUSTED_LOCAL)


async def test_handle_knowledge_does_not_authorize_another_run_namespace_or_session(db, service):
    ref = await save(db, service)
    principal = ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, policy=DENY_ALL)
    registry, _ = registry_with(GATE_CREATE)
    with pytest.raises(PrivateValueError, match="unauthorized"):
        await service.read(ref, owner=OWNER, principal=principal)
    with _invocation_context(context(registry, principal=principal)):
        assert await service.read(ref, owner=OWNER, principal=principal) == SECRET
        with pytest.raises(PrivateValueError, match="unauthorized"):
            await service.read(ref, owner=PrivateOwner("run", "run-2"), principal=principal)
        with pytest.raises(PrivateValueError, match="unauthorized"):
            await service.read(ref, owner=PrivateOwner("namespace", "ns"), principal=principal)
    with _invocation_context(context(registry, principal=principal, run_id="run-2")):
        with pytest.raises(PrivateValueError, match="unavailable"):
            await service.read(ref, owner=PrivateOwner("run", "run-2"), principal=principal)
    with pytest.raises(PrivateValueError, match="unauthorized"):
        await service.read(
            ref, owner=OWNER, principal=replace(principal, kind=PrincipalKind.SESSION)
        )


async def test_namespace_values_and_script_bytes_share_protection(db, service):
    owner = PrivateOwner("namespace", "state-1", "project", "p")
    ref = await save(
        db, service, SECRET.encode(), owner=owner, content_type="application/octet-stream"
    )
    assert await service.read(ref, owner=owner, principal=TRUSTED_LOCAL) == SECRET.encode()
    expired = await save(db, service, expires_at=1.0)
    with pytest.raises(PrivateValueError, match="expired"):
        await service.read(expired, owner=OWNER, principal=TRUSTED_LOCAL)


def test_configuration_has_only_environment_names_and_never_generates_keys(monkeypatch):
    config = PlaybooksConfig(
        private_value_active_key_id="one", private_value_key_env={"one": "AQ_TEST_PRIVATE_KEY"}
    )
    monkeypatch.delenv("AQ_TEST_PRIVATE_KEY", raising=False)
    with pytest.raises(PrivateValueError, match="key_unavailable"):
        PrivateValueService.from_config(object(), config).require_ready()
    monkeypatch.setenv("AQ_TEST_PRIVATE_KEY", base64.b64encode(KEY).decode())
    assert PrivateValueService.from_config(object(), config).require_ready() == ("one", KEY)
    assert base64.b64encode(KEY).decode() not in repr(config)


async def test_snapshot_loop_wait_and_inspection_store_references(db, service):
    snapshot = make_snapshot(
        bindings={"result": {"body": SECRET, "count": 1}},
        private_bindings={"result": ("body",)},
        sensitive={"old-handle": SECRET},
        event={"body": SECRET},
        context={"data": SECRET},
        loop=LoopFrame("loop", "item", SECRET, 0, 1, partial=({"body": SECRET},)),
    )
    with pytest.raises(PrivateValueError, match="storage_required"):
        snapshot.to_body()
    await db.create_run(snapshot)
    loaded = await db.load_run(snapshot.run_id)
    assert loaded.bindings["result"]["count"] == 1
    assert is_reference(loaded.bindings["result"]["body"])
    assert SECRET not in json.dumps(loaded.to_body())
    assert SECRET not in json.dumps(snapshot.redacted().to_body())
    assert project_value(loaded.bindings)["redacted"]
    assert await db.resolve_private_snapshot(loaded, TRUSTED_LOCAL) == snapshot
    wait = WaitSpec(
        "wait-1",
        snapshot.run_id,
        "wait",
        kind="event",
        event_type="reply",
        match={"body": SECRET},
        created_at=100,
    )
    snapshot = await db.commit_boundary(
        replace(snapshot, wait=wait), make_receipt(snapshot), WaitChangeSet(register=(wait,))
    )
    async with db._engine.connect() as conn:
        wait_row = (await conn.execute(select(playbook_waits))).mappings().one()
        body = await conn.scalar(select(playbook_v2_runs.c.snapshot))
    assert SECRET not in str(wait_row)
    assert SECRET not in body
    claims = await db.claim_for_event(Event("reply", "e1", {"body": SECRET}), now=101)
    assert len(claims) == 1
    async with db._engine.connect() as conn:
        assert SECRET not in str((await conn.execute(select(playbook_pending_events))).all())


async def test_snapshot_cas_failure_rolls_back_private_rows(db, service):
    snapshot = make_snapshot(
        bindings={"result": {"body": SECRET}}, private_bindings={"result": ("body",)}
    )
    await db.create_run(snapshot)
    await db.commit_boundary(snapshot, make_receipt(snapshot))
    async with db._engine.connect() as conn:
        before = await conn.scalar(select(func.count()).select_from(playbook_private_values))
    with pytest.raises(SnapshotVersionConflict):
        await db.commit_boundary(snapshot, make_receipt(snapshot, receipt_id="stale"))
    async with db._engine.connect() as conn:
        assert (
            await conn.scalar(select(func.count()).select_from(playbook_private_values)) == before
        )


async def test_missing_key_refuses_before_effect_and_dry_run_never_invokes():
    registry, adapter = registry_with(GATE_CREATE)
    step = command_step(command="gate_create", inputs={}, transitions={"created": "done"})
    ctx = context(registry, inputs={"task_id": "t", "title": SECRET})
    ctx = replace(ctx, services=replace(ctx.services, private_values=None))
    result = await LiveCommandExecutor().execute(step, ctx)
    assert result.outcome == "unavailable"
    assert not adapter.calls
    preview = await PreviewCommandExecutor().execute(step, ctx)
    assert preview.control.value == "unresolved"
    assert not adapter.calls


async def test_key_missing_persistence_has_no_plaintext_fallback(db):
    snapshot = make_snapshot(sensitive={"old-handle": SECRET})
    with pytest.raises(PrivateValueError, match="key_unavailable"):
        await db.create_run(snapshot)
    assert await db.load_run(snapshot.run_id) is None


async def test_storage_readiness_failure_precedes_handler_invocation(db, monkeypatch):
    from unittest.mock import AsyncMock

    registry, adapter = registry_with(GATE_CREATE)
    storage = PrivateValueService(db._engine, active_key_id="one", keys={"one": KEY})
    monkeypatch.setattr(
        storage,
        "check_ready",
        AsyncMock(side_effect=PrivateValueError("private_storage_unavailable")),
    )
    ctx = context(registry, inputs={"task_id": "t", "title": SECRET})
    ctx = replace(ctx, services=replace(ctx.services, private_values=storage))
    result = await LiveCommandExecutor().execute(command_step(command="gate_create"), ctx)
    assert result.outcome == "unavailable"
    assert not adapter.calls


async def test_generated_live_dispatch_preserves_principal_and_preview_never_dispatches(
    db,
    service,
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.commands.contracts.generated import generated_registration
    from src.commands.contracts.registry import ContractRegistry
    from src.commands.principal import current_principal
    from src.playbooks.executors.command import ShadowCommandExecutor
    from tests.test_generated_command_contracts import descriptor

    caller = ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, policy=DENY_ALL)
    observed = []

    async def execute(name, args):
        observed.append((name, args, current_principal()))
        return {"success": True, "body": SECRET}

    handler = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    monkeypatch.setattr("src.commands.contracts.builtin._handler", lambda: handler)
    registry = ContractRegistry()
    registry.register(generated_registration(descriptor()))
    ctx = context(registry, principal=caller, inputs={"query": SECRET})
    ctx = replace(ctx, services=replace(ctx.services, private_values=service))
    step = command_step(command="new_command", transitions={"success": "done"})
    preview = await PreviewCommandExecutor().execute(step, ctx)
    shadow = await ShadowCommandExecutor().execute(step, ctx)
    assert preview.control.value == shadow.control.value == "unresolved"
    assert SECRET not in str(shadow.recorded_command_args)
    assert not observed
    result = await LiveCommandExecutor().execute(step, ctx)
    assert result.value == {"success": True, "body": SECRET}
    assert "body" in result.private_fields
    assert observed == [("new_command", {"query": SECRET}, caller)]
    assert SECRET not in str(result.receipt_result)


@pytest.mark.parametrize("raises", [False, True])
async def test_playbook_handler_logs_and_events_never_publish_values(caplog, raises):
    import logging
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.commands.handler import CommandHandler
    from src.commands.principal import principal_context
    from src.config import AppConfig

    bus = SimpleNamespace(emit=AsyncMock())
    handler = CommandHandler(SimpleNamespace(db=None, bus=bus, plugin_registry=None), AppConfig())
    caller = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK, policy=DENY_ALL, provenance=("profile-not-found",)
    )

    async def invoke(args):
        if raises:
            raise RuntimeError(SECRET)
        return {"success": False, "error": SECRET, "body": SECRET}

    handler._cmd_get_schema = invoke
    with caplog.at_level(logging.DEBUG, logger="src.commands.handler"), principal_context(caller):
        await handler.execute("get_schema", {"query": SECRET})
    assert SECRET not in caplog.text
    assert SECRET not in str(bus.emit.call_args_list)
    assert bus.emit.await_count


async def test_restart_restores_private_binding_only_for_owning_run(db, service):
    from src.playbooks.engine import PlaybookEngine, InterruptedByRestart
    from src.playbooks.expressions import BindingRef
    from src.playbooks.executors.base import EngineServices
    from tests.playbook_v2_engine_helpers import (
        InMemoryArtifactStore,
        StubActivations,
        artifact_ref_for,
        minimal_artifact,
    )
    from tests.test_v2_engine_repository import seed_artifact

    artifact = minimal_artifact()
    artifact = artifact.model_copy(
        update={
            "steps": {
                **artifact.steps,
                "done": artifact.steps["done"].model_copy(
                    update={
                        "value": BindingRef(binding="review", path="task_id"),
                    }
                ),
            }
        }
    )
    ref = artifact_ref_for(artifact)
    await seed_artifact(db, ref)
    registry, _ = registry_with(GATE_CREATE)
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry,
            clock=lambda: 100.0,
            artifact_store=InMemoryArtifactStore({"test": artifact}),
            private_values=service,
        ),
        runs=db,
        activations=StubActivations([ref]),
    )
    snapshot = make_snapshot(
        playbook_id=ref.playbook_id,
        artifact_sha256=ref.artifact_sha256,
        rule_id="r",
        current_step_id="done",
        bindings={"review": {"task_id": SECRET}},
        private_bindings={"review": ("task_id",)},
    )
    await db.create_run(snapshot)
    stored = await db.load_run(snapshot.run_id)
    caller = ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, policy=DENY_ALL)
    restored = await engine._resolve_private_run(stored, caller, db)
    assert restored.bindings["review"]["task_id"] == SECRET
    # Removing a read key returns a safe error without overwriting resumable data.
    db.set_playbook_private_values(PrivateValueService(db._engine))
    result = await engine.resume(snapshot.run_id, InterruptedByRestart(2_000_000), caller)
    assert result.outcome == "private_storage_key_unavailable"
    assert SECRET not in str(result.snapshot)
    assert (await db.load_run(snapshot.run_id)).version == stored.version
    db.set_playbook_private_values(service)
    result = await engine.resume(snapshot.run_id, InterruptedByRestart(2_000_000), caller)
    assert result.lifecycle.value == "completed"
    assert SECRET not in str(await db.list_receipts(snapshot.run_id))
    assert SECRET not in json.dumps((await db.load_run(snapshot.run_id)).to_body())


async def test_reference_metadata_cannot_smuggle_plaintext(db, service):
    ref = await save(db, service)
    forged = {**ref, "body": SECRET, "byte_count": SECRET}
    sanitized = service.batch(OWNER).protect(forged)
    assert SECRET not in str(sanitized)
    assert await service.read(sanitized, owner=OWNER, principal=TRUSTED_LOCAL) == SECRET
    with pytest.raises(PrivateValueError, match="reference_invalid"):
        await service.read(
            {"__private_value__": {"body": SECRET}}, owner=OWNER, principal=TRUSTED_LOCAL
        )


async def test_namespace_read_requires_server_membership_and_current_scope(db, service):
    owner = PrivateOwner("namespace", "ns-1", "project", "p")
    ref = await save(db, service, owner=owner)
    caller = ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, policy=DENY_ALL, project_id="p")
    with pytest.raises(PrivateValueError, match="unauthorized"):
        await service.read(ref, owner=owner, principal=caller)

    async def member(check_owner, check_principal, invocation):
        return check_owner == owner and check_principal == caller and invocation is not None

    authorized = PrivateValueService(
        db._engine, active_key_id="one", keys={"one": KEY}, namespace_authorizer=member
    )
    registry, _ = registry_with(GATE_CREATE)
    with _invocation_context(context(registry, principal=caller)):
        assert await authorized.read(ref, owner=owner, principal=caller) == SECRET
        with pytest.raises(PrivateValueError, match="unauthorized"):
            await authorized.read(ref, owner=owner, principal=replace(caller, project_id="other"))


async def test_private_pinned_collection_stays_encrypted_after_source_binding_is_reused(db, service):
    snapshot = make_snapshot(loop_stack=(
        LoopFrame("outer", "row", "digest", 0, 1, collection=[[SECRET]], private=True),
        LoopFrame("inner", "cell", "digest", 0, 1, collection=[SECRET], private=True),
    ), iteration_paths=True, iteration_entries=2)
    await db.create_run(snapshot)
    loaded = await db.load_run(snapshot.run_id)
    assert all(is_reference(frame.collection) for frame in loaded.loop_stack)
    assert SECRET not in json.dumps(loaded.to_body())
    assert await db.resolve_private_snapshot(loaded, TRUSTED_LOCAL) == snapshot
    wait = WaitSpec("private-loop-wait", snapshot.run_id, "body", kind="event",
                    event_type="reply", match={"body": SECRET}, created_at=100,
                    iteration_path=snapshot.iteration_path)
    await db.register(wait, snapshot.version + 1)
    await db.claim_for_event(Event("reply", "nested-arrival", {"body": SECRET}), now=101)
    async with db._engine.connect() as conn:
        assert SECRET not in str((await conn.execute(select(playbook_waits))).all())
        assert SECRET not in str((await conn.execute(select(playbook_pending_events))).all())
