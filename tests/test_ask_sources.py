"""A01: bounded complete context, safe legacy projection and static source contracts."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from src.asks.adapters import AdapterRegistry
from src.asks.models import AskChoice, AskError, validate_context
from src.database.tables import human_asks
from tests.ask_helpers import db as db_fixture, service as service_fixture, snapshot  # noqa: F401


async def test_complete_context_and_no_source_effect(service, db):
    async with db._engine.begin() as conn:
        result = await service.publish(snapshot().identity, conn=conn)
        assert result.created
        assert result.ask["context_state"] == "complete"
        assert result.ask["decision_state"] == "pending"
        assert result.ask["context"]["choices"][0]["effect"]
    assert (await db.get_review("rev-ask"))["state"] == "in_review"


@pytest.mark.parametrize(
    "missing", ["why", "waiting_effect", "requested_by", "blocked_work", "links"]
)
async def test_missing_context_refuses_without_creating_ask(service, db, missing):
    source = snapshot()
    context = dict(source.context)
    del context[missing]
    async with db._engine.begin() as conn:
        with pytest.raises(AskError) as err:
            await service.publish_snapshot(replace(source, context=context), conn=conn)
        assert err.value.code == "ask.context_missing"
        assert missing in err.value.fields
        assert (await conn.execute(select(func.count()).select_from(human_asks))).scalar_one() == 0
    assert (await db.get_review("rev-ask"))["state"] == "in_review"


@pytest.mark.parametrize("field,value", [("logical_owner", ""), ("decider", {}), ("audience", {})])
async def test_missing_ownership_refuses_publication(service, db, field, value):
    async with db._engine.begin() as conn:
        with pytest.raises(AskError) as err:
            await service.publish_snapshot(replace(snapshot(), **{field: value}), conn=conn)
        assert field in err.value.fields


async def test_legacy_context_needed_preserves_source_link_without_choices(service, db):
    source = snapshot()
    async with db._engine.begin() as conn:
        result = await service.publish_snapshot(
            replace(source, context={"links": source.context["links"]}), legacy=True, conn=conn
        )
        assert result.ask["context_state"] == "context_needed"
        assert result.ask["next_actor"] == "agent"
        assert result.ask["context"]["choices"] == []
        assert result.ask["context"]["links"] == source.context["links"]


@pytest.mark.parametrize("count,valid", [(5, True), (20, True), (21, False)])
def test_discord_choice_limit_is_not_domain_limit(count, valid):
    source = replace(
        snapshot(), choices=tuple(AskChoice(str(i), "Label", "Effect") for i in range(count))
    )
    assert (not validate_context(source)) is valid


@pytest.mark.parametrize("field,limit", [("title", 200), ("why", 2000), ("waiting_effect", 2000)])
def test_context_bounds_are_not_truncated(field, limit):
    source = snapshot()
    assert not validate_context(replace(source, context={**source.context, field: "x" * limit}))
    assert field in validate_context(
        replace(source, context={**source.context, field: "x" * (limit + 1)})
    )


def test_choice_effect_and_duplicate_keys_are_checked():
    source = snapshot()
    assert "choices[0]" in validate_context(replace(source, choices=(AskChoice("a", "A", ""),)))
    assert "choices[1]" in validate_context(replace(source, choices=source.choices[:1] * 2))
    assert "choices[0]" in validate_context(
        replace(source, choices=(AskChoice("a", "A", "x" * 2001),))
    )


def test_unregistered_adapter_never_falls_back_to_generic_command():
    with pytest.raises(AskError) as err:
        AdapterRegistry().get("gate", "caller_command")
    assert err.value.code == "ask.unsupported_source"


async def test_meaning_and_display_and_feedback_have_distinct_versions(service, db):
    source = snapshot()
    async with db._engine.begin() as conn:
        first = (await service.publish_snapshot(source, conn=conn)).ask
        display = replace(source, context={**source.context, "title": "Approve the retry design!"})
        second = (await service.publish_snapshot(display, conn=conn)).ask
        assert second["id"] == first["id"]
        assert second["decision_generation"] == 1
        assert second["display_version"] == 2
        feedback = replace(display, feedback_version=1, fingerprint="comment-1")
        third = (await service.publish_snapshot(feedback, conn=conn)).ask
        assert third["decision_generation"] == 1
        assert third["feedback_version"] == 1
        revised = replace(feedback, generation_token="revision-2", fingerprint="revision-2")
        fourth = (await service.publish_snapshot(revised, conn=conn)).ask
        assert fourth["decision_generation"] == 2
        assert fourth["id"] == first["id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("waiting_effect", "Decline automatically tomorrow"),
        ("blocked_work", [{"label": "New task", "url": "/tasks/new"}]),
    ],
)
async def test_changed_waiters_or_waiting_effect_invalidates_generation(service, db, field, value):
    source = snapshot()
    async with db._engine.begin() as conn:
        await service.publish_snapshot(source, conn=conn)
        result = await service.publish_snapshot(
            replace(source, context={**source.context, field: value}), conn=conn
        )
        assert result.ask["decision_generation"] == 2


async def _human_gate(handler, *, policy=True):
    from src.models import Project
    await handler.db.create_project(Project(id="p-source", name="Sources"))
    created = await handler.execute("gate_create", {
        "project_id": "p-source", "gate_type": "human", "title": "Approve deployment?",
        "question": "The release needs authorization.",
        "human_request": {"policy": "approval_hold_v1", "context": {
            "waiting_effect": "The release remains held until approval.",
        }} if policy else None,
    })
    assert created["success"], created
    shown = await handler._cmd_gate_show({"gate_id": created["gate_id"]})
    return shown["gate"], shown["gate"]["ask"]


async def _source_preview(handler, ask, option):
    args = {"ask_id": ask["id"], "expected": {k: ask[k] for k in (
        "decision_generation", "source_fingerprint", "feedback_version")}, "option_key": option}
    result = await handler._cmd_ask_prepare_decision(args)
    assert result["success"], result
    return {**args, "preview_id": result["preview"]["id"], "idempotency_key": option}


async def test_missing_gate_policy_has_owned_context_needed_and_legacy_link(command_handler_factory):
    handler = await command_handler_factory()
    gate, ask = await _human_gate(handler, policy=False)
    assert ask["context_state"] == "context_needed"
    assert ask["logical_owner"] and ask["context"]["choices"] == []
    refusal = await handler._cmd_gate_resolve({"gate_id": gate["id"], "resolved_by": "spoof"})
    assert refusal["error_code"] == "ask.context_missing"
    assert refusal["ask_id"] == ask["id"]
    assert (await handler.db.get_gate(gate["id"]))["status"] == "open"


async def test_malformed_producer_context_stays_owned_and_disabled(command_handler_factory):
    from sqlalchemy import update
    from src.database.tables import gates

    handler = await command_handler_factory()
    gate, _ = await _human_gate(handler)
    async with handler.db.immediate() as conn:
        await conn.execute(update(gates).where(gates.c.id == gate["id"]).values(
            human_request={"policy": "approval_hold_v1", "context": "not a context packet"}))
    shown = await handler._cmd_gate_show({"gate_id": gate["id"]})
    ask = shown["gate"]["ask"]
    assert ask["context_state"] == "context_needed"
    assert ask["logical_owner"] and ask["context"]["choices"] == []


@pytest.mark.parametrize("option,status", [("approve", "resolved"), ("decline", "cancelled")])
async def test_gate_decision_receipt_replay_and_competing_choice(
    command_handler_factory, option, status,
):
    handler = await command_handler_factory()
    gate, ask = await _human_gate(handler)
    prepared = await _source_preview(handler, ask, option)
    other = await _source_preview(handler, ask, "decline" if option == "approve" else "approve")
    result = await handler._cmd_ask_decide(prepared)
    assert result["success"], result
    assert (await handler.db.get_gate(gate["id"]))["status"] == status
    replay = await handler._cmd_ask_decide(prepared)
    assert replay["success"] and not replay["created"], replay
    assert replay["decision_id"] == result["decision_id"]
    refused = await handler._cmd_ask_decide(other)
    assert not refused["success"], refused
    assert (await handler.db.get_gate(gate["id"]))["status"] == status


async def test_gate_changed_meaning_refuses_old_preview(command_handler_factory):
    from sqlalchemy import update
    from src.database.tables import gates
    handler = await command_handler_factory()
    gate, ask = await _human_gate(handler)
    args = await _source_preview(handler, ask, "approve")
    async with handler.db.immediate() as conn:
        await conn.execute(update(gates).where(gates.c.id == gate["id"]).values(
            human_request={"policy": "approval_hold_v1", "context": {
                "waiting_effect": "A different release now waits here.",
            }}))
    result = await handler._cmd_ask_decide(args)
    assert result["error_code"] == "ask.stale_source", result
    assert (await handler.db.get_gate(gate["id"]))["status"] == "open"


async def test_concurrent_gate_choices_have_one_winner(command_handler_factory):
    import asyncio
    handler = await command_handler_factory()
    gate, ask = await _human_gate(handler)
    choices = [await _source_preview(handler, ask, option) for option in ("approve", "decline")]
    results = await asyncio.gather(*(handler._cmd_ask_decide(choice) for choice in choices))
    assert sum(bool(result["success"]) for result in results) == 1, results
    winner = next(result for result in results if result["success"])
    assert (await handler.db.get_gate(gate["id"]))["human_request"]["receipt"]["decision_id"] == winner["decision_id"]


async def test_declined_gate_keeps_waiters_and_reopen_advances_generation(command_handler_factory):
    from src.models import Task, TaskStatus
    handler = await command_handler_factory()
    gate, _ = await _human_gate(handler)
    await handler.db.create_task(Task(id="waiting", project_id="p-source", title="Wait", description="Wait for approval", status=TaskStatus.READY))
    async with handler.db.immediate() as conn:
        await handler.db.attach_gate_waiters(gate["id"], ["waiting"], conn=conn)
    ask = (await handler._cmd_gate_show({"gate_id": gate["id"]}))["gate"]["ask"]
    result = await handler._cmd_ask_decide(await _source_preview(handler, ask, "decline"))
    assert result["success"], result
    assert (await handler.db.get_task("waiting")).is_blocked
    shown = (await handler._cmd_gate_show({"gate_id": gate["id"]}))["gate"]["ask"]
    reopened = await handler._cmd_gate_reopen({"gate_id": gate["id"], "expected": {k: shown[k] for k in (
        "decision_generation", "source_fingerprint", "feedback_version")}})
    assert reopened["success"], reopened
    refreshed = (await handler._cmd_gate_show({"gate_id": gate["id"]}))["gate"]["ask"]
    assert refreshed["decision_generation"] == ask["decision_generation"] + 1
    assert (await handler.db.get_task("waiting")).is_blocked


async def _proposal_source(handler):
    from src.models import Project
    await handler.db.create_project(Project(id="p-proposal", name="Proposal"))
    proposed = await handler.execute("task_batch_propose", {
        "project_id": "p-proposal", "source": "spec:approved-test",
        "tasks": [{"tempId": "a", "title": "First", "description": "Approved work"}], "edges": [],
    })
    assert proposed["success"], proposed
    created = await handler.execute("gate_create", {"project_id": "p-proposal", "gate_type": "human",
        "title": "Approve proposed work", "question": "Create these tasks?", "await_id": proposed["proposal_id"]})
    assert created["success"], created
    shown = await handler._cmd_gate_show({"gate_id": created["gate_id"]})
    return proposed["proposal_id"], shown["gate"], shown["gate"]["ask"]


@pytest.mark.parametrize("option", ["approve", "decline"])
async def test_proposal_decision_is_atomic_and_receipt_is_replayed(command_handler_factory, option):
    from src.database.tables import task_proposals, tasks
    handler = await command_handler_factory()
    proposal, gate, ask = await _proposal_source(handler)
    prepared = await _source_preview(handler, ask, option)
    first = await handler._cmd_ask_decide(prepared)
    assert first["success"], first
    replay = await handler._cmd_ask_decide(prepared)
    assert replay["decision_id"] == first["decision_id"] and not replay["created"], replay
    async with handler.db.immediate() as conn:
        row = (await conn.execute(select(task_proposals).where(task_proposals.c.id == proposal))).mappings().one()
        count = await conn.scalar(select(func.count()).select_from(tasks).where(tasks.c.project_id == "p-proposal"))
    assert row["status"] == ("committed" if option == "approve" else "discarded")
    assert count == (1 if option == "approve" else 0)
    assert (await handler.db.get_gate(gate["id"]))["status"] == ("resolved" if option == "approve" else "cancelled")
    events = [call.args[0] for call in handler.orchestrator.bus.emit.call_args_list]
    assert events.count("gate.resolved") == (1 if option == "approve" else 0)
    assert events.count("proposal.status_changed") == 1


async def test_proposal_materialization_failure_rolls_back_approval(command_handler_factory, monkeypatch):
    from unittest.mock import AsyncMock
    from src.database.tables import ask_decisions, task_proposals
    handler = await command_handler_factory()
    proposal, gate, ask = await _proposal_source(handler)
    prepared = await _source_preview(handler, ask, "approve")
    monkeypatch.setattr(handler, "_materialize_proposal", AsyncMock(return_value=({"success": False, "error": "conflicting graph edit"}, [])))
    result = await handler._cmd_ask_decide(prepared)
    assert result["error_code"] == "ask.stale_source", result
    async with handler.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(ask_decisions)) == 0
        assert await conn.scalar(select(task_proposals.c.status).where(task_proposals.c.id == proposal)) == "ready"
    assert (await handler.db.get_gate(gate["id"]))["status"] == "open"


async def test_human_resume_command_consumes_exact_wait_receipt(command_handler_factory, monkeypatch):
    from src.asks.reconcile import identity
    from src.asks.sources import WaitAdapter
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import playbook_v2_runs
    from src.models import Project
    from src.playbooks.private_values import PrivateValueService, is_reference
    from tests.test_v2_restart_resume import recovery_engine
    from tests.playbook_v2_engine_helpers import event
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="Wait project"))
    handler.db.set_playbook_private_values(PrivateValueService(
        handler.db._engine, active_key_id="test", keys={"test": b"x" * 32},
    ))
    engine, _, ref = await recovery_engine(handler.db, "wait-kinds.artifact.json")
    started = await engine.run_rule(ref, "gate", {**event("task-completed-code"), "project_id": "p"}, TRUSTED_LOCAL)
    assert started.snapshot.wait.kind == "human"
    monkeypatch.setattr(handler, "_v2_engine", lambda: engine)
    persisted = await handler.db.load_run(started.run_id)
    assert is_reference(persisted.event["project_id"])
    async with handler.db.immediate() as conn:
        row = (await conn.execute(select(playbook_v2_runs).where(
            playbook_v2_runs.c.run_id == started.run_id,
        ))).mappings().one()
        source = await identity(handler, playbook_v2_runs, row, conn)
        assert source.project_id == "p"
        with pytest.raises(AskError, match="Playbook run was not found"):
            await WaitAdapter(handler).snapshot(replace(source, project_id="other"), conn=conn)
    args = {"run_id": started.run_id, "human_input": "approve", "idempotency_key": "wait-answer"}
    refused = await handler._cmd_resume_playbook(args)
    assert refused["error_code"] == "ask.context_missing", refused
    async with handler.db.immediate() as conn:
        ask = await handler.db.get_ask(refused["ask_id"], conn=conn)
    preview = await _source_preview(handler, ask, "approve")
    result = await handler._cmd_resume_playbook({**args, "expected": preview["expected"], "preview_id": preview["preview_id"]})
    assert result["success"], result
    receipt = result["receipt"]
    assert receipt["wait_id"] == started.snapshot.wait.wait_id
    assert receipt["decision_id"] == result["decision_id"]
    retry = await handler._cmd_ask_retry_operation({"decision_id": result["decision_id"]})
    assert retry["success"] and retry["receipt"] == receipt
