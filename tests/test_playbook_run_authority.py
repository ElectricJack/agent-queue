"""Run-bound authority for manual, recovery and preview execution of gated artifacts.

A gated run's effects act for the caller that started it, bounded by the
immutable artifact; an operator resolving a paused run authorizes the control
decision only, and the effect it lets proceed is re-derived from the recorded
caller.  Built on the pure ``playbook_state_notify`` harness.
"""
# ruff: noqa: F811 - shared pytest fixtures are imported, then requested by name
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.commands.contracts import CONTRACTS
from src.commands.principal import TRUSTED_LOCAL, ExecutionPrincipal, PrincipalKind
from src.database import tables as t
from src.playbooks.authority import (
    RUN_CALLER_MISSING,
    bind,
    caller_record,
    effect_principal,
    public_caller,
)
from src.playbooks.definition import scope_from_v1, source_digest
from src.playbooks.engine import InterruptedByRestart, OperatorResolution, PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.run_state import RunLifecycle, RunSnapshot
from src.profiles.capabilities import CapabilityPolicy
from tests.playbook_v2_engine_helpers import RecordingRunRepository, StubActivations
from tests.test_playbook_state import command_context, db, service  # noqa: F401 -- pytest fixtures
from tests.test_playbook_state_notify import (  # noqa: F401 -- pytest fixture
    PURE_SOURCE,
    counts,
    pure,
    repin,
)

EVENT = {"event_type": "task.completed", "event_id": "live", "project_id": "p"}
BOTH = frozenset({"playbook_state_notify", "message_send"})
ZERO = [0, 0, 0, 0, 0]


class SimulatedCrash(BaseException):
    pass


@contextmanager
def provider(handler):
    from src.commands.contracts.builtin import set_handler_provider

    set_handler_provider(lambda: handler)
    try:
        yield
    finally:
        set_handler_provider(None)


def engine_for(context, runs):
    _, ctx, _, store = context
    return PlaybookEngine(services=EngineServices(
        contracts=CONTRACTS, clock=lambda: 1000, artifact_store=store,
        private_values=ctx.services.private_values), runs=runs,
        activations=StubActivations([ctx.artifact_ref]))


async def start(context, principal, runs, event=EVENT):
    handler, ctx, _, _ = context
    with provider(handler):
        return await engine_for(context, runs).run_rule(ctx.artifact_ref, "r", event, principal)


async def resume(context, runs, run_id, cause, principal):
    """A fresh engine on the same durable runs: the restarted daemon."""
    handler, *_ = context
    with provider(handler):
        return await engine_for(context, runs).resume(run_id, cause, principal)


def session(project_id="p", grants=BOTH, session_id="s1"):
    return ExecutionPrincipal(kind=PrincipalKind.SESSION, session_id=session_id,
                              project_id=project_id, profile_id="worker",
                              policy=CapabilityPolicy(aq_commands=frozenset(grants)))


async def crash_before_effect(context, principal, monkeypatch):
    """Commit the notify attempt, then die before the command is dispatched."""
    handler, *_ = context
    runs = RecordingRunRepository()
    original = handler.execute

    async def die(command, args, *rest, **kwargs):
        if command == "playbook_state_notify":
            raise SimulatedCrash()
        return await original(command, args, *rest, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(handler, "execute", die)
        with pytest.raises(SimulatedCrash):
            await start(context, principal, runs)
    (run_id,) = runs.snapshots
    paused = await resume(context, runs, run_id, InterruptedByRestart(2000), TRUSTED_LOCAL)
    assert paused.lifecycle is RunLifecycle.PAUSED, paused
    return runs, run_id


async def messages(database):
    async with database._engine.connect() as conn:
        return (await conn.execute(select(t.messages))).mappings().all()


def notify_receipts(runs):
    return [r for r in runs.receipts if r.step_id == "notify" and r.receipt_kind != "operator_decision"]


def system_artifact(context):
    source = PURE_SOURCE.replace("scope: project:p", "scope: system")
    raw = context[1].artifact.model_dump(mode="json")
    raw.update(scope=scope_from_v1("system").model_dump(mode="json"),
               source_hash=source_digest(source))
    return repin(context, raw, source)


async def test_trusted_manual_run_gets_exactly_the_artifact_grant(pure, db):
    runs = RecordingRunRepository()
    outcome = await start(pure, TRUSTED_LOCAL, runs)
    assert outcome.outcome == "completed", outcome
    assert len(await messages(db)) == 1
    snapshot = runs.snapshots[outcome.run_id]
    assert snapshot.caller == {"kind": "local", "session_id": None, "service_name": None,
                               "task_id": None, "project_id": None, "profile_id": None,
                               "elevated": False}
    (receipt,) = [r for r in notify_receipts(runs) if r.completed_at is not None]
    bound = bind(TRUSTED_LOCAL, pure[1].artifact, outcome.run_id, "notify")
    assert bound.project_id == "p" and bound.policy.aq_commands == BOTH
    assert receipt.principal == {"kind": "playbook", "profile_id": None, "session_id": None,
                                 "capability_fingerprint": bound.policy.fingerprint()}


async def test_crash_after_commit_replays_one_notification_after_operator_retry(pure, db,
                                                                                 monkeypatch):
    principal = pure[2]
    runs = RecordingRunRepository()
    original = runs.commit_boundary

    async def lose_receipt(snapshot, receipt, *args, **kwargs):
        if receipt.step_id == "notify" and receipt.completed_at is not None:
            raise SimulatedCrash()
        return await original(snapshot, receipt, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runs, "commit_boundary", lose_receipt)
        with pytest.raises(SimulatedCrash):
            await start(pure, principal, runs)
    sent = await messages(db)
    before = await counts(db)
    assert len(sent) == 1 and before[3] == 1
    (run_id,) = runs.snapshots
    paused = await resume(pure, runs, run_id, InterruptedByRestart(2000), principal)
    # The ambiguous effect still pauses for an operator: no automatic replay.
    assert paused.lifecycle is RunLifecycle.PAUSED and paused.outcome != "completed"
    assert await counts(db) == before
    done = await resume(pure, runs, run_id, OperatorResolution(kind="retry"), TRUSTED_LOCAL)
    assert done.lifecycle is RunLifecycle.COMPLETED, done
    assert [row["id"] for row in await messages(db)] == [sent[0]["id"]]
    assert await counts(db) == before
    decision = [r for r in runs.receipts if r.receipt_kind == "operator_decision"]
    assert decision and decision[-1].principal["kind"] == "local"
    retried = [r for r in notify_receipts(runs) if r.completed_at is not None][-1]
    assert retried.principal["kind"] == "playbook"


@pytest.mark.parametrize("recorded, current", [
    (frozenset({"playbook_state_notify"}), BOTH),   # never more than the run started with
    (BOTH, frozenset({"playbook_state_notify"})),   # revoked since: current authority wins
    (BOTH, frozenset()),                            # session gone: fail closed
])
async def test_operator_retry_keeps_the_session_callers_restrictions(pure, db, monkeypatch,
                                                                     recorded, current):
    handler = pure[0]
    runs, run_id = await crash_before_effect(pure, session(grants=recorded), monkeypatch)
    assert runs.snapshots[run_id].caller["aq_commands"] == sorted(recorded)
    scopes = []

    async def derive(scope):
        scopes.append(scope)
        return session(project_id=scope["project_id"], grants=current,
                       session_id=scope["session_id"])

    monkeypatch.setattr(handler, "_principal_from_scope", derive)
    outcome = await resume(pure, runs, run_id, OperatorResolution(kind="retry"), TRUSTED_LOCAL)
    assert outcome.outcome != "completed", outcome
    assert scopes == [{"kind": "session", "session_id": "s1", "task_id": None,
                       "project_id": "p", "elevated": False}]
    assert await counts(db) == ZERO


async def test_operator_retry_completes_a_session_run_with_current_authority(pure, db,
                                                                            monkeypatch):
    handler = pure[0]
    runs, run_id = await crash_before_effect(pure, session(), monkeypatch)

    async def derive(scope):
        return session(project_id=scope["project_id"], session_id=scope["session_id"])

    monkeypatch.setattr(handler, "_principal_from_scope", derive)
    outcome = await resume(pure, runs, run_id, OperatorResolution(kind="retry"), TRUSTED_LOCAL)
    assert outcome.lifecycle is RunLifecycle.COMPLETED, outcome
    assert len(await messages(db)) == 1


async def test_project_caller_cannot_escalate_to_a_system_artifact(pure, db, monkeypatch):
    context = system_artifact(pure)
    for caller in (pure[2], session()):
        outcome = await start(context, caller, RecordingRunRepository())
        assert outcome.outcome != "completed", outcome
    # Nor can an operator's trusted retry lift the recorded project restriction.
    runs, run_id = await crash_before_effect(context, pure[2], monkeypatch)
    assert runs.snapshots[run_id].caller["project_id"] == "p"
    outcome = await resume(context, runs, run_id, OperatorResolution(kind="retry"),
                           TRUSTED_LOCAL)
    assert outcome.outcome != "completed", outcome
    assert await counts(db) == ZERO


async def test_authored_event_and_context_cannot_choose_the_caller(pure, db):
    runs = RecordingRunRepository()
    forged = {**EVENT, "caller": {"kind": "local", "aq_commands": ["task_delete"]},
              "principal": {"kind": "local"}, "project_id": "other",
              "context": {"caller": {"kind": "service"}}}
    outcome = await start(pure, pure[2], runs, forged)
    snapshot = runs.snapshots[outcome.run_id]
    assert snapshot.caller == caller_record(pure[2], pure[1].artifact)
    assert snapshot.caller["kind"] == "playbook" and snapshot.caller["project_id"] == "p"
    assert snapshot.caller["aq_commands"] == sorted(BOTH)


async def test_missing_caller_record_fails_closed_under_a_trusted_driver(pure, db, monkeypatch):
    runs, run_id = await crash_before_effect(pure, pure[2], monkeypatch)
    runs.snapshots[run_id] = replace(runs.snapshots[run_id], caller=None)
    outcome = await resume(pure, runs, run_id, OperatorResolution(kind="retry"), TRUSTED_LOCAL)
    assert outcome.outcome != "completed", outcome
    assert await counts(db) == ZERO
    closed = await effect_principal(runs.snapshots[run_id], pure[1].artifact, TRUSTED_LOCAL,
                                    SimpleNamespace(handler=None), "notify")
    assert closed.kind is PrincipalKind.PLAYBOOK and not closed.policy.aq_commands
    assert closed.provenance == (RUN_CALLER_MISSING,)
    # An enforced driver can only narrow its own authority.
    narrowed = await effect_principal(runs.snapshots[run_id], pure[1].artifact, session(),
                                      SimpleNamespace(handler=None), "notify")
    assert narrowed.policy.aq_commands == BOTH and narrowed.session_id == "s1"
    assert narrowed.session_instance_token is None


@pytest.mark.parametrize("driver, kind", [
    ("playbook", "retry"),
    ("session", "retry"),
    ("local", "replay"),
])
async def test_invalid_recovery_decisions_never_write(pure, db, monkeypatch, driver, kind):
    runs, run_id = await crash_before_effect(pure, pure[2], monkeypatch)
    principal = {"playbook": pure[2], "session": session(), "local": TRUSTED_LOCAL}[driver]
    outcome = await resume(pure, runs, run_id, OperatorResolution(kind=kind), principal)
    assert outcome.outcome == ("contract_violation" if driver == "local" else "unauthorized")
    assert runs.snapshots[run_id].lifecycle is RunLifecycle.PAUSED
    assert await counts(db) == ZERO


def test_legacy_artifacts_record_no_caller_and_projections_hide_grants(pure):
    artifact = pure[1].artifact
    legacy = artifact.model_copy(update={"trusted_execution": None})
    assert caller_record(TRUSTED_LOCAL, legacy) is None
    assert caller_record(session(), "not an artifact") is None
    record = caller_record(session(), artifact)
    assert record["aq_commands"] == sorted(BOTH) and "session_instance_token" not in record
    assert public_caller(record) == {k: v for k, v in record.items() if k != "aq_commands"}
    snapshot = RunSnapshot(
        run_id="r1", playbook_id="state-test", artifact_sha256="a" * 64, rule_id="r",
        current_step_id="notify", version=1)
    assert "caller" not in snapshot.to_body()
    gated = replace(snapshot, caller=record)
    assert RunSnapshot.from_body(gated.to_body()).caller == record
    assert gated.redacted().caller == public_caller(record)


async def test_trusted_preview_of_a_gated_state_step(pure, db):
    from tests.test_v2_dry_run import RaisingWrites

    handler, ctx, _, store = pure
    engine = PlaybookEngine(services=EngineServices(
        contracts=CONTRACTS, clock=lambda: 1000, artifact_store=store,
        db=RaisingWrites(), private_values=RaisingWrites()), runs=RaisingWrites())
    with provider(handler):
        tree = await engine.dry_run(ctx.artifact_ref, EVENT, TRUSTED_LOCAL)
    assert {path.nodes[-1].step_id for path in tree.paths} == {"done"}, tree
    assert await counts(db) == ZERO
