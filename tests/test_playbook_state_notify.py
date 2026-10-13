"""D2 authority, atomicity, observation ordering and database-free preview proofs."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from src.commands.contracts import CONTRACTS
from src.commands.principal import TRUSTED_LOCAL
from src.config import MessagesConfig
from src.database import tables as t
from src.database.queries import playbook_state_queries as queries
from src.messages.service import MessageDeliveryService
from src.models import Project, Task
from src.playbooks.definition import PlaybookDefinition
from src.playbooks.executors.base import EngineServices, ExecutionMode, StepContext
from src.playbooks.executors.command import PreviewCommandExecutor, ShadowCommandExecutor
from src.playbooks.expressions import ResolutionScope
from src.playbooks.private_values import PrivateValueBatch
from src.playbooks.state import Limits, Namespace
from src.playbooks.state_notify import NotificationService
from src.profiles.capabilities import CapabilityPolicy
from tests.playbook_v2_engine_helpers import artifact_ref_for
from tests.test_message_delivery import FakeSessionManager, RecordingBus, make_engine
from tests.test_playbook_state import (
    command_context as command_context, db as db, execute, service as service,
)

NS = Namespace("project", "p", "state-test")


@pytest.fixture
async def context(command_context, db):
    handler, ctx, principal, store = command_context
    await db.create_project(Project(id="p", name="p", supervisor_enabled=True))
    await db.create_project(Project(id="other", name="other", supervisor_enabled=True))
    handler.config.messages = MessagesConfig(enabled=True)
    handler.orchestrator.bus = SimpleNamespace(emit=AsyncMock())
    raw = ctx.artifact.model_dump(mode="json")
    raw["steps"]["message_send"] = {**raw["steps"]["playbook_state_notify"], "command": "message_send"}
    artifact = PlaybookDefinition.model_validate(raw)
    ref = artifact_ref_for(artifact)
    source = store.load_source(ctx.artifact_ref.artifact_sha256)
    store.put(artifact, source_digest=ref.source_digest,
              contract_fingerprint=ref.contract_fingerprint, profile_fingerprint="",
              compiler_build=ref.compiler_build)
    store.put_source(ref.artifact_sha256, source)
    ctx.artifact, ctx.artifact_ref, ctx.step_id = artifact, ref, "playbook_state_notify"
    principal = replace(principal, policy=CapabilityPolicy(
        aq_commands=principal.policy.aq_commands | {"message_send"}))
    return handler, ctx, principal, store


def inputs(observation="one", at=1, **kw):
    return dict(key="k", action="present", observed_at=at, observation_id=observation,
                message=dict(to_kind="session", to_id="supervisor-p", body="Private detail"),
                last_value={"evidence": "SECRET-EVIDENCE"},
                source_partition={"project_id": "p", "source": "doctor"}, **kw)


async def notify(context, observation="one", at=1, **kw):
    args = inputs(observation, at)
    args.update(kw)
    return await execute(context, "playbook_state_notify", args)


async def counts(db):
    async with db._engine.connect() as conn:
        return [await conn.scalar(select(func.count()).select_from(table)) for table in (
            t.messages, t.playbook_state_namespaces, t.playbook_state_entries,
            t.playbook_state_operations, t.playbook_private_values)]


async def test_episode_suppression_replay_clear_recurrence_and_provenance(context, db):
    first = await notify(context)
    assert first["success"] and first["enqueued"] and first["episode"] == 1, first
    msg = await db.get_message(first["message_id"])
    assert msg.created_seq > 0
    assert msg.from_kind == "system" and msg.from_id == "playbook:state-test"
    assert msg.provenance == dict(run_id="run", playbook_id="state-test",
                                  namespace=NS.as_dict(), step_id="playbook_state_notify")
    assert await notify(context) == first
    suppressed = await notify(context, "two", 2, last_value={"age": "changed"})
    assert suppressed["success"] and not suppressed["enqueued"] and suppressed["revision"] == 2
    assert suppressed["episode"] == 1
    conflict = await notify(context, message=dict(to_kind="session", to_id="supervisor-p", body="new"))
    assert conflict["error_code"] == "idempotency_conflict"
    cleared = await notify(context, "three", 3, action="cleared", expected_revision=2)
    assert cleared["enqueued"] and cleared["tombstone"] and not cleared["active"]
    assert await notify(context, "three", 3, action="cleared", expected_revision=2) == cleared
    again_clear = await notify(context, "four", 4, action="cleared", expected_revision=3)
    assert not again_clear["enqueued"] and again_clear["revision"] == 4
    recurrence = await notify(context, "five", 5)
    assert recurrence["episode"] == 2 and recurrence["enqueued"]
    assert recurrence["first_seen"] == 5
    assert (await counts(db))[:4] == [3, 1, 1, 5]
    assert await notify(context) == first  # A lost receipt never means a new enqueue.
    async with db._engine.connect() as conn:
        for table in (t.playbook_state_entries, t.playbook_state_operations):
            assert "SECRET-EVIDENCE" not in str((await conn.execute(select(table))).all())


async def test_concurrent_duplicate_and_new_observations_serialize(context, db):
    results = await asyncio.gather(*(notify(context) for _ in range(8)))
    assert all(result == results[0] for result in results), results
    assert results[0]["enqueued"]
    assert (await counts(db))[:4] == [1, 1, 1, 1]
    results = await asyncio.gather(*(notify(context, f"tick-{i}", 2 + i) for i in range(8)))
    assert not any(result.get("enqueued") for result in results)
    assert all(result["success"] or result["error_code"] == "state.stale_observation"
               for result in results)
    assert (await counts(db))[0] == 1


async def test_reminder_boundary_concurrency_and_replay_cannot_remind(context, db, monkeypatch):
    clock = AsyncMock(return_value=1000.0)
    monkeypatch.setattr(queries, "transaction_time", clock)
    first = await notify(context, repeat_after_seconds=86400)
    clock.return_value = 87399.0
    assert not (await notify(context, "before", 2, repeat_after_seconds=86400))["enqueued"]
    clock.return_value = 87400.0
    results = await asyncio.gather(*(notify(context, f"due-{i}", 3 + i, repeat_after_seconds=86400)
                                     for i in range(8)))
    assert sum(bool(result.get("enqueued")) for result in results) == 1, results
    clock.return_value = 999999.0
    assert await notify(context, repeat_after_seconds=86400) == first
    assert (await counts(db))[0] == 2


async def test_stale_clear_saved_snapshot_and_tombstone_watermarks(context, db):
    await notify(context)
    snapshot = await execute(context, "playbook_state_list", {"record_space": "notification"})
    assert snapshot["success"] and snapshot["identity_manifest"][0]["revision"] == 1
    await notify(context, "two", 2)
    page = await execute(context, "playbook_state_list", dict(
        record_space="notification", snapshot_id=snapshot["snapshot_id"],
        page_token=snapshot["page_tokens"][0]["page_token"]))
    assert page["entries"][0]["revision"] == 1 and page["entries"][0]["value"]["evidence"]
    stale = await notify(context, "old-scan-clear", 3, action="cleared", expected_revision=1)
    assert stale["error_code"] == "conflict"
    cleared = await notify(context, "new-scan-clear", 4, action="cleared", expected_revision=2)
    assert cleared["enqueued"]
    assert (await notify(context, "late-present", 3))["error_code"] == "state.stale_observation"
    assert (await notify(context, "equal", 4))["error_code"] == "conflict"
    assert (await notify(context, "wrong-rev", 5, action="cleared",
                          expected_revision=2))["error_code"] == "conflict"
    missing = await notify(context, "missing", 10, key="missing", action="cleared", expected_revision=0)
    assert missing["success"] and not missing["enqueued"] and missing["revision"] == 1
    assert (await notify(context, "old", 9, key="missing"))["error_code"] == "state.stale_observation"
    assert (await notify(context, "later-clear", 11, key="missing", action="cleared",
                          expected_revision=0))["error_code"] == "conflict"
    assert (await counts(db))[0] == 2


async def test_concurrent_present_clear_cannot_apply_old_absence(context):
    await notify(context)
    results = await asyncio.gather(
        notify(context, "present", 3),
        notify(context, "clear", 2, action="cleared", expected_revision=1),
    )
    assert results[0]["success"]
    state = await execute(context, "playbook_state_get", {"key": "k", "record_space": "notification"})
    assert state["active"] and state["last_observed_at"] == 3
    assert results[1]["success"] or results[1]["error_code"] in {"conflict", "state.stale_observation"}


@pytest.mark.parametrize("fault", ["message", "private", "journal", "namespace", "keys"])
async def test_all_failures_roll_back_message_state_journal_and_allow_retry(context, db, monkeypatch,
                                                                          fault):
    with monkeypatch.context() as patch:
        if fault == "message":
            original = db.create_message_in_transaction

            async def failed(conn, **args):
                await original(conn, **args)
                raise RuntimeError("injected after insert")
            patch.setattr(db, "create_message_in_transaction", failed)
        elif fault == "private":
            original = PrivateValueBatch.persist

            async def failed(self, conn):
                await original(self, conn)
                raise RuntimeError("crash before commit")
            patch.setattr(PrivateValueBatch, "persist", failed)
        else:
            limits = {"journal": Limits(journal_bytes=1), "namespace": Limits(namespace_bytes=1),
                      "keys": Limits(keys=0)}[fault]
            original = NotificationService.__init__

            def init(self, engine, private):
                original(self, engine, private, limits=limits)
            patch.setattr(NotificationService, "__init__", init)
        result = await notify(context)
        assert result["success"] is False, result
    assert await counts(db) == [0, 0, 0, 0, 0]
    assert (await notify(context))["enqueued"]
    assert (await counts(db))[:4] == [1, 1, 1, 1]


async def test_failed_wake_commits_and_existing_delivery_service_recovers(context, db):
    handler, _, _, _ = context
    handler.orchestrator.bus.emit.side_effect = RuntimeError("wake lost")
    first = await notify(context)
    assert first["success"] and first["enqueued"]
    assert (await db.get_message(first["message_id"])).delivered_at is None
    assert await notify(context) == first
    sessions = FakeSessionManager(activity_map={("session", "supervisor-p", "p"): "idle"})
    engine = make_engine(db, sessions, bus=RecordingBus())
    bus = SimpleNamespace(subscribe=lambda *_: lambda: None)
    delivery = MessageDeliveryService(engine, lambda: MessagesConfig(enabled=True), bus)
    delivery.start()
    try:
        await asyncio.wait_for(sessions.submitted.wait(), timeout=5)
    finally:
        await delivery.stop()
    assert len(sessions.nudges) == 1
    assert (await counts(db))[0] == 1  # Durable insertion, not a claim of human attention.


@pytest.mark.parametrize("message", [
    dict(to_kind="session", to_id="supervisor-other", body="x"),
    dict(to_kind="session", to_id="supervisor-global", body="x"),
    dict(to_kind="user", to_id="dashboard", body="x"),
    dict(to_kind="task", to_id="missing", body="x"),
])
async def test_recipient_authorized_even_when_no_message_is_due(context, db, message):
    first = await notify(context)
    before = await counts(db)
    result = await notify(context, "suppressed", 2, message=message)
    assert result["success"] is False
    assert await counts(db) == before
    assert await notify(context) == first


async def test_replay_reauthorizes_recipient_and_both_grants(context, db):
    await db.create_task(Task(id="recipient", project_id="p", title="recipient", description="x"))
    message = dict(to_kind="task", to_id="recipient", body="x")
    assert (await notify(context, message=message))["enqueued"]
    async with db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "recipient").values(project_id="other"))
    result = await notify(context, message=message)
    assert result["error_code"] == "unauthorized"
    handler, ctx, principal, store = context
    for grant in ("message_send", "playbook_state_notify"):
        caller = replace(principal, policy=CapabilityPolicy(
            aq_commands=principal.policy.aq_commands - {grant}))
        result = await execute((handler, ctx, caller, store), "playbook_state_notify", inputs())
        assert not result["success"]
    assert (await counts(db))[0] == 1


@pytest.mark.parametrize("extra", [
    {"from_id": "forged"}, {"namespace": NS.as_dict()}, {"observed_at": True},
    {"observed_at": float("nan")}, {"repeat_after_seconds": 0}, {"repeat_after_seconds": True},
    {"action": "cleared"}, {"expected_revision": True}, {"action": "bogus"},
    {"message": dict(to_kind="session", to_id="supervisor-p", body="x", from_id="forged")},
    {"last_value": {"__private_value__": "guess"}},
])
async def test_invalid_input_never_writes(context, db, extra):
    assert not (await notify(context, **extra))["success"]
    assert await counts(db) == [0, 0, 0, 0, 0]


async def test_reset_floor_and_disabled_messages_preserve_retry(context, db):
    handler, _, _, _ = context
    handler.config.messages = MessagesConfig(enabled=False)
    assert not (await notify(context))["success"]
    assert await counts(db) == [0, 0, 0, 0, 0]
    handler.config.messages = MessagesConfig(enabled=True)
    first = await notify(context)
    reset = await execute(context, "playbook_state_reset", dict(
        namespace=NS.as_dict(), expected_revision=1, reason="reviewed reset", operation_key="reset"),
        caller=TRUSTED_LOCAL, context=False)
    assert reset["success"], reset
    assert await notify(context) == first
    assert (await notify(context, "retired", 2))["error_code"] == "state.retired_observation"


async def test_normal_send_shares_sender_validation_and_atomic_insert_can_rollback(context, db):
    handler, ctx, principal, _ = context
    result = await execute(context, "message_send", dict(
        project_id="p", to_kind="session", to_id="supervisor-p", body="normal",
        from_kind="user", from_id="forged", provenance={"run_id": "forged"}))
    assert result["success"], result
    assert result["message"]["from_kind"] == "system"
    assert result["message"]["provenance"]["run_id"] == "run"
    with pytest.raises(RuntimeError):
        async with db.immediate() as conn:
            msg = await db.create_message_in_transaction(
                conn, project_id="p", from_kind="system", from_id="test",
                to_kind="session", to_id="supervisor-alias", body="rollback")
            assert msg.to_id == "supervisor-p" and msg.created_seq > 0
            raise RuntimeError("rollback")
    assert (await counts(db))[0] == 1


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.SHADOW])
async def test_preview_and_shadow_share_transition_logic_without_database(context, db, mode):
    _, invocation, principal, store = context
    services = EngineServices(contracts=CONTRACTS, clock=lambda: 1000, artifact_store=store)
    executor = PreviewCommandExecutor() if mode is ExecutionMode.DRY_RUN else ShadowCommandExecutor()
    step = invocation.artifact.steps["playbook_state_notify"].model_copy(update={
        "transitions": {"success": "done", "failure": "bad"}, "save_result_as": "notification",
    })
    ctx = StepContext(run_id="preview-run", dispatch_id="preview-dispatch",
                      artifact_ref=invocation.artifact_ref, artifact=invocation.artifact,
                      rule_id="r", step_id="playbook_state_notify", principal=principal,
                      scope=ResolutionScope(event={}, context={}, bindings={}, loop={}),
                      services=services, mode=mode, inputs=inputs())
    first = await executor.execute(step, ctx)
    assert first.outcome == "success", first
    assert first.value["preview"] and first.value["message_id"].startswith("preview-message:")
    assert (await executor.execute(step, ctx)).value == first.value
    second = await executor.execute(step, replace(ctx, inputs=inputs("two", 2)))
    assert second.value["revision"] == 2 and not second.value["enqueued"]
    clear_args = {**inputs("clear", 3), "action": "cleared", "expected_revision": 2}
    cleared = await executor.execute(step, replace(ctx, inputs=clear_args))
    assert cleared.value["enqueued"] and not cleared.value["active"]
    fresh = await executor.execute(step, replace(ctx, run_id="fresh", state_preview={}))
    assert fresh.value["revision"] == 1 and fresh.value["enqueued"]
    assert await counts(db) == [0, 0, 0, 0, 0]


def repin(context, raw, source=None):
    handler, ctx, principal, store = context
    source = source or store.load_source(ctx.artifact_ref.artifact_sha256)
    artifact = PlaybookDefinition.model_validate(raw)
    ref = artifact_ref_for(artifact)
    store.put(artifact, source_digest=ref.source_digest,
              contract_fingerprint=ref.contract_fingerprint, profile_fingerprint="",
              compiler_build=ref.compiler_build)
    store.put_source(ref.artifact_sha256, source)
    ctx.artifact, ctx.artifact_ref = artifact, ref
    return handler, ctx, principal, store


async def test_system_sender_and_disabled_supervisor_redirect(context, db):
    from src.playbooks.definition import source_digest

    raw = context[1].artifact.model_dump(mode="json")
    source = context[3].load_source(context[1].artifact_ref.artifact_sha256).replace(
        "scope: project:p", "scope: system")
    raw.update(scope={"type": "system"}, source_hash=source_digest(source))
    handler, ctx, principal, store = repin(context, raw, source)
    context = handler, ctx, replace(principal, project_id=None), store
    system = await notify(context, message=dict(to_kind="user", to_id="dashboard", body="x"))
    assert system["enqueued"]
    msg = await db.get_message(system["message_id"])
    assert msg.project_id is None and msg.provenance["namespace"]["scope_kind"] == "system"
    await db.create_task(Task(id="recipient", project_id="p", title="r", description="x"))
    task = await notify(context, "task", 2, key="task",
                        message=dict(to_kind="task", to_id="recipient", body="x"))
    assert (await db.get_message(task["message_id"])).project_id == "p"
    async with db.immediate() as conn:
        await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(
            supervisor_enabled=False))
    redirected = await notify(context, "redirect", 3, key="redirect")
    assert redirected["enqueued"]
    assert (await db.get_message(redirected["message_id"])).to_id == "supervisor-global"


async def test_missing_key_material_and_artifact_grant_refuse_before_enqueue(context, db):
    from src.playbooks.private_values import PrivateValueService

    private = db._playbook_private_values
    db.set_playbook_private_values(PrivateValueService(db._engine))
    assert not (await notify(context))["success"]
    assert await counts(db) == [0, 0, 0, 0, 0]
    db.set_playbook_private_values(private)
    raw = context[1].artifact.model_dump(mode="json")
    del raw["steps"]["message_send"]
    assert (await notify(repin(context, raw)))["error_code"] == "unauthorized"
    assert await counts(db) == [0, 0, 0, 0, 0]


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.SHADOW])
async def test_engine_preview_forks_isolate_state_and_fresh_runs(context, db, mode):
    from src.playbooks.engine import PlaybookEngine
    from tests.test_v2_dry_run import RaisingWrites

    raw = context[1].artifact.model_dump(mode="json")
    base = raw["steps"]["playbook_state_notify"]
    # A database-dependent send forks, then both branches attempt the same CAS.
    # Sharing their overlays would make the second path conflict.
    raw["rules"][0]["entry_step"] = "fork"
    raw["steps"]["fork"] = {**base, "command": "message_send",
                              "inputs": {k: {"type": "literal", "value": v} for k, v in dict(
                                  project_id="p", from_kind="system", from_id="playbook:state-test",
                                  to_kind="session", to_id="supervisor-p", body="fork").items()},
                              "save_result_as": None,
                              "transitions": {"queued": "left", "rejected": "right"}}
    for branch in ("left", "right"):
        raw["steps"][branch] = {
            **base, "command": "playbook_state_set", "save_result_as": branch,
            "inputs": {k: {"type": "literal", "value": v} for k, v in dict(
                key="shared", value=branch, expected_revision=0, operation_key=branch).items()},
            "transitions": {"success": "done", "failure": "bad"},
        }
    _, invocation, principal, store = repin(context, raw)
    principal = replace(principal, policy=CapabilityPolicy(
        aq_commands=principal.policy.aq_commands | {"message_send"}))
    engine = PlaybookEngine(services=EngineServices(
        contracts=CONTRACTS, clock=lambda: 1000, artifact_store=store,
        db=RaisingWrites(), private_values=RaisingWrites()), runs=RaisingWrites())
    event = {"event_type": "task.completed", "event_id": "preview", "project_id": "p"}
    for _ in range(2):
        if mode is ExecutionMode.DRY_RUN:
            tree = await engine.dry_run(invocation.artifact_ref, event, principal)
        else:
            outcome = await engine.run_rule(invocation.artifact_ref, "r", event, principal, mode=mode)
            tree = outcome.traversal
            assert {name for _, name, _ in outcome.commands} == {"message_send", "playbook_state_set"}
        assert len(tree.paths) == 2
        assert {path.nodes[-1].step_id for path in tree.paths} == {"done"}, tree
        assert all(path.nodes[1].outcome == "success" for path in tree.paths), tree
    assert await counts(db) == [0, 0, 0, 0, 0]


async def test_preview_unknown_recipient_and_clear_validation_do_not_consume_state(context):
    from src.playbooks.executors.base import StepControl

    _, invocation, principal, store = context
    step = invocation.artifact.steps["playbook_state_notify"].model_copy(update={
        "transitions": {"success": "done", "failure": "bad"}})
    ctx = StepContext(run_id="preview", dispatch_id="preview", artifact_ref=invocation.artifact_ref,
                      artifact=invocation.artifact, rule_id="r", step_id="playbook_state_notify",
                      principal=principal, scope=ResolutionScope(event={}, context={}, bindings={},
                                                                 loop={}),
                      services=EngineServices(contracts=CONTRACTS, clock=lambda: 1000,
                                              artifact_store=store),
                      mode=ExecutionMode.DRY_RUN,
                      inputs={**inputs(), "message": dict(to_kind="task", to_id="t", body="x")})
    executor = PreviewCommandExecutor()
    result = await executor.execute(step, ctx)
    assert result.control is StepControl.UNRESOLVED
    assert result.diagnostics == ("state.preview_recipient_unresolved",)
    assert not ctx.state_preview
    bad_clear = await executor.execute(step, replace(ctx, inputs={**inputs(), "action": "cleared"}))
    assert bad_clear.value["error_code"] == "state.invalid_arguments"
    assert not ctx.state_preview


async def test_preview_kv_cas_expiry_and_immutable_snapshot(context, db):
    _, invocation, principal, store = context
    clock = [1000]
    ctx = StepContext(run_id="preview", dispatch_id="preview", artifact_ref=invocation.artifact_ref,
                      artifact=invocation.artifact, rule_id="r", step_id="playbook_state_set",
                      principal=principal, scope=ResolutionScope(event={}, context={}, bindings={},
                                                                 loop={}),
                      services=EngineServices(contracts=CONTRACTS, clock=lambda: clock[0],
                                              artifact_store=store), mode=ExecutionMode.DRY_RUN)
    executor = PreviewCommandExecutor()

    async def call(command, **args):
        step = invocation.artifact.steps[command].model_copy(update={
            "transitions": {"success": "done", "failure": "bad"}})
        result = await executor.execute(step, replace(ctx, step_id=command, inputs=args))
        assert result.outcome == "success", result
        return result.value

    first = await call("playbook_state_set", key="k", expected_revision=0, operation_key="one",
                       value={"private": "preview-only"}, ttl_seconds=1)
    manifest = await call("playbook_state_list", page_size=1)
    await call("playbook_state_delete", key="k", expected_revision=1, operation_key="delete")
    snapshot = await call("playbook_state_list", page_size=1, snapshot_id=manifest["snapshot_id"],
                          page_token=manifest["page_tokens"][0]["page_token"])
    assert snapshot["entries"][0]["revision"] == 1
    assert snapshot["entries"][0]["value"] == {"private": "preview-only"}
    current = await call("playbook_state_get", key="k")
    assert current["revision"] == 2 and current["tombstone"]
    await call("playbook_state_set", key="k", expected_revision=2, operation_key="recreate",
               value="new", ttl_seconds=1)
    clock[0] += 2
    current = await call("playbook_state_get", key="k")
    assert current["expired"] and current["revision"] == 3 and not current["exists"]
    assert await call("playbook_state_set", key="k", expected_revision=0, operation_key="one",
                      value={"private": "preview-only"}, ttl_seconds=1) == first
    assert await counts(db) == [0, 0, 0, 0, 0]
