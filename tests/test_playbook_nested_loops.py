"""Durable nested traversal, identities, bounded state and observation context."""

import json
from dataclasses import replace
from datetime import datetime

import pytest

from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.definition import PlaybookDefinition, loop_body
from src.playbooks.engine import InterruptedByRestart, TimerFired
from src.playbooks.executors.base import StepContext
from src.playbooks.executors.foreach import ForEachExecutor, collection_digest
from src.playbooks.expressions import ResolutionScope
from src.playbooks.receipts import encode_iteration_path, idempotency_key
from src.playbooks.run_state import MAX_ITERATION_ENTRIES, LoopFrame, RunLifecycle, RunSnapshot
from tests.playbook_v2_engine_helpers import minimal_artifact
from tests.test_v2_engine import RecordingWaitRepository, WaitAwareRepository, build, ok


def nested_artifact(*, wait=False, items=None):
    payload = minimal_artifact().model_dump(mode="json")
    payload["rules"][0]["entry_step"] = "outer"
    common = {"rule": "r", "title": "Nested", "source": payload["steps"]["done"]["source"]}
    payload["steps"].pop("ensure-review-task")
    payload["steps"]["outer"] = {
        **common,
        "type": "foreach",
        "collection": {
            "type": "list",
            "items": [
                {"type": "literal", "value": row}
                for row in (items if items is not None else [["a", "b"], ["c", "d"]])
            ],
        },
        "item_binding": "row",
        "body_entry": "inner",
        "failure_policy": "halt",
        "transitions": {"completed": "done", "failed": "bad", "runtime_error": "bad"},
    }
    payload["steps"]["inner"] = {
        **common,
        "type": "foreach",
        "collection": {"type": "loop_ref", "binding": "row"},
        "item_binding": "cell",
        "body_entry": "body",
        "failure_policy": "halt",
        "transitions": {"completed": "outer", "failed": "bad", "runtime_error": "bad"},
    }
    payload["steps"]["body"] = (
        {
            **common,
            "type": "wait",
            "wait_kind": "timer",
            "timeout_seconds": 1,
            "transitions": {"fired": "inner", "runtime_error": "bad"},
        }
        if wait
        else {
            **common,
            "type": "command",
            "command": "ensure_task",
            "inputs": {
                "project_id": {"type": "literal", "value": "p"},
                "title": {"type": "loop_ref", "binding": "cell"},
            },
            "transitions": {"created": "inner", "reused": "inner", "rejected": "inner"},
        }
    )
    return PlaybookDefinition.model_validate(payload)


def event(**fields):
    return {"_event_type": "task.completed", "event_id": "event-1", **fields}


def nested_engine(*, items=None):
    waits = RecordingWaitRepository()
    runs = WaitAwareRepository(waits)
    artifact = nested_artifact(wait=True, items=items)
    engine, adapter, _, _, ref = build(artifact=artifact, runs=runs)
    engine.waits = waits
    return engine, adapter, runs, waits, ref


def roundtrip(snapshot):
    # The memory repository deliberately has no encryption provider. Private
    # references and real encryption are covered by test_playbook_private_values.
    return RunSnapshot.from_body(json.loads(json.dumps(snapshot.to_body(protect=lambda v: v))))


async def test_nested_effects_have_full_paths_and_pop_only_the_inner_frame():
    artifact = nested_artifact()
    assert loop_body(artifact, "outer") == {"inner", "body"}
    assert loop_body(artifact, "inner") == {"body"}
    engine, adapter, runs, _, ref = build(artifact=artifact)
    adapter.queue.extend(ok(str(n)) for n in range(4))
    result = await engine.run_rule(ref, "r", event(), TRUSTED_LOCAL)
    assert result.lifecycle is RunLifecycle.COMPLETED
    assert result.snapshot.loop_stack == ()
    assert result.snapshot.iteration_entries == 6
    assert [call[1].title for call in adapter.calls] == ["a", "b", "c", "d"]
    receipts = [r for r in runs.receipts if r.step_id == "body" and r.receipt_kind == "step"]
    paths = [
        encode_iteration_path([("outer", i), ("inner", j)]) for i in range(2) for j in range(2)
    ]
    assert [r.iteration_path for r in receipts] == paths
    assert [r.iteration for r in receipts] == [0, 1, 0, 1]
    assert [r.attempt for r in receipts] == [1] * 4
    assert len({r.idempotency_key for r in receipts}) == 4
    assert [call[1].dedup_key for call in adapter.calls] == [r.idempotency_key for r in receipts]


async def test_each_wait_restores_both_frames_and_repeated_inner_indices_are_distinct():
    engine, _, runs, waits, ref = nested_engine()
    result = await engine.run_rule(ref, "r", event(), TRUSTED_LOCAL)
    for i in range(4):
        assert result.lifecycle is RunLifecycle.PAUSED
        assert [(f.step_id, f.index) for f in result.snapshot.loop_stack] == [
            ("outer", i // 2),
            ("inner", i % 2),
        ]
        runs.snapshots[result.run_id] = roundtrip(result.snapshot)
        # A fresh engine must resume exclusively from the durable values.
        engine, _, _, _, _ = build(artifact=nested_artifact(wait=True), runs=runs)
        engine.waits = waits
        result = await engine.resume(result.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
    assert result.lifecycle is RunLifecycle.COMPLETED
    assert len(waits.registered) == 4
    assert len({w.wait_id for w in waits.registered}) == 4
    assert len({w.iteration_path for w in waits.registered}) == 4
    assert result.snapshot.iteration_entries == 6


async def test_four_frames_restore_lexical_items_and_failure_receipt_identity():
    payload = nested_artifact(wait=True, items=[["a"]]).model_dump(mode="json")
    payload["steps"]["inner"]["body_entry"] = "third"
    for step_id, parent, body_entry in (("third", "inner", "fourth"), ("fourth", "third", "body")):
        payload["steps"][step_id] = {
            **payload["steps"]["inner"],
            "collection": {"type": "literal", "value": ["a"]},
            "item_binding": step_id,
            "body_entry": body_entry,
            "transitions": {"completed": parent, "failed": "bad", "runtime_error": "bad"},
        }
    payload["steps"]["body"]["transitions"]["fired"] = "fourth"
    artifact = PlaybookDefinition.model_validate(payload)
    from tests.test_playbook_v2_validation import check, codes

    assert not {"nested_loop", "loop_body_escapes", "loop_variable_shadow"} & codes(check(artifact))
    waits = RecordingWaitRepository()
    runs = WaitAwareRepository(waits)
    engine, _, _, _, ref = build(artifact=artifact, runs=runs)
    engine.waits = waits
    result = await engine.run_rule(ref, "r", event(), TRUSTED_LOCAL)
    paused = roundtrip(result.snapshot)
    assert len(paused.loop_stack) == 4
    assert engine._scope(paused, artifact).loop == {
        "row": ["a"],
        "cell": "a",
        "third": "a",
        "fourth": "a",
        "row#index": 0,
        "cell#index": 0,
        "third#index": 0,
        "fourth#index": 0,
    }
    runs.snapshots[result.run_id] = paused
    result = await engine.resume(result.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
    assert result.lifecycle is RunLifecycle.COMPLETED
    assert result.snapshot.loop_stack == ()
    assert result.snapshot.iteration_entries == 4

    # A fallback failure at a nested boundary must retain the same identity.
    failing = await engine.run_rule(ref, "r", event(), TRUSTED_LOCAL, dispatch_id="failure")
    _, receipt = await engine._terminate(
        failing.snapshot, runs, "state_limit_exceeded", "test failure"
    )
    assert receipt.iteration == 0
    assert receipt.iteration_path == encode_iteration_path(
        [("outer", 0), ("inner", 0), ("third", 0), ("fourth", 0)]
    )
    assert runs.receipts[-1] == receipt


async def test_empty_inner_loop_does_not_pop_the_outer_frame_or_reuse_body_binding():
    artifact = nested_artifact(wait=True, items=[["a"], [], ["c"]])
    payload = artifact.model_dump(mode="json")
    payload["steps"]["body"]["save_result_as"] = "latest"
    waits = RecordingWaitRepository()
    runs = WaitAwareRepository(waits)
    engine, _, _, _, ref = build(artifact=PlaybookDefinition.model_validate(payload), runs=runs)
    engine.waits = waits
    result = await engine.run_rule(ref, "r", event(), TRUSTED_LOCAL)
    result = await engine.resume(result.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
    assert [(f.step_id, f.index) for f in result.snapshot.loop_stack] == [
        ("outer", 2),
        ("inner", 0),
    ]
    assert "latest" not in result.snapshot.bindings
    result = await engine.resume(result.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
    assert result.lifecycle is RunLifecycle.COMPLETED
    assert result.snapshot.iteration_entries == 5
    assert len(waits.registered) == 2


async def test_inner_failure_takes_authored_failure_transition():
    from src.commands.contracts.models import CommandResult
    from tests.fixtures.contracts.engine_contracts import EnsureTaskResult

    engine, adapter, runs, _, ref = build(artifact=nested_artifact())
    adapter.queue.append(
        CommandResult(
            outcome="rejected",
            value=EnsureTaskResult(task_id="t", created=False),
            summary="rejected",
        )
    )
    result = await engine.run_rule(ref, "r", event(), TRUSTED_LOCAL)
    assert result.lifecycle is RunLifecycle.FAILED
    assert result.snapshot.current_step_id == "bad"
    assert len(adapter.calls) == 1
    assert result.snapshot.loop_stack == ()
    assert not any(r.step_id == "done" for r in runs.receipts)


async def test_pinned_collection_survives_rebinding_and_restart():
    artifact = nested_artifact(wait=True)
    payload = artifact.model_dump(mode="json")
    payload["steps"]["outer"]["collection"] = {
        "type": "binding_ref",
        "binding": "page",
        "path": "items",
    }
    artifact = PlaybookDefinition.model_validate(payload)
    waits = RecordingWaitRepository()
    runs = WaitAwareRepository(waits)
    engine, _, _, _, ref = build(artifact=artifact, runs=runs)
    engine.waits = waits
    result = await engine.run_rule(ref, "r", event(), TRUSTED_LOCAL, pause_before_start=True)
    runs.snapshots[result.run_id] = replace(
        result.snapshot,
        lifecycle=RunLifecycle.RUNNING,
        bindings={"page": {"items": [["original"], ["second"]]}},
    )
    result = await engine.resume(
        result.run_id, InterruptedByRestart(process_started_at=1001), TRUSTED_LOCAL
    )
    assert result.lifecycle is RunLifecycle.PAUSED
    runs.snapshots[result.run_id] = roundtrip(
        replace(result.snapshot, bindings={"page": {"items": [["replacement"]]}})
    )
    result = await engine.resume(result.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
    assert result.lifecycle is RunLifecycle.PAUSED
    assert result.snapshot.loop_stack[0].collection == [["original"], ["second"]]
    assert result.snapshot.loop_stack[1].collection == ["second"]
    result = await engine.resume(result.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
    assert result.lifecycle is RunLifecycle.COMPLETED


async def test_entry_budget_persists_and_cannot_route_to_success():
    engine, _, runs, _, ref = nested_engine()
    result = await engine.run_rule(ref, "r", event(), TRUSTED_LOCAL)
    runs.snapshots[result.run_id] = roundtrip(
        replace(result.snapshot, iteration_entries=MAX_ITERATION_ENTRIES)
    )
    result = await engine.resume(result.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
    assert result.lifecycle is RunLifecycle.FAILED
    assert result.snapshot.iteration_entries == MAX_ITERATION_ENTRIES
    assert result.snapshot.error_code == "state_limit_exceeded"
    assert not any(r.step_id == "done" for r in runs.receipts)


async def test_unbound_iterations_keep_bounded_aggregation():
    artifact = nested_artifact()
    engine, _, _, _, ref = build(artifact=artifact)
    step = artifact.steps["inner"]
    collection = [0] * 1000
    frame = LoopFrame(
        "inner",
        "cell",
        collection_digest(collection),
        0,
        len(collection),
        collection=collection,
        last_outcome="created",
        last_failed=False,
    )
    ctx = StepContext(
        run_id="run",
        dispatch_id="dispatch",
        artifact_ref=ref,
        artifact=artifact,
        rule_id="r",
        step_id="inner",
        principal=TRUSTED_LOCAL,
        scope=ResolutionScope(),
        services=engine.services,
    )
    for n in range(999):
        result = await ForEachExecutor().execute(step, replace(ctx, loop_frame=frame))
        frame = replace(result.loop_frame, last_outcome="created", last_failed=False)
        assert frame.partial == ()
        assert frame.succeeded == n + 1


def test_legacy_snapshot_and_effect_keys_keep_the_empty_namespace():
    body = {
        "run_id": "run",
        "playbook_id": "p",
        "artifact_sha256": "sha",
        "rule_id": "r",
        "loop": {
            "step_id": "outer",
            "item_binding": "row",
            "collection_digest": "d",
            "index": 1,
            "total": 2,
        },
        "attempts": {"body:1": 3},
    }
    snapshot = RunSnapshot.from_body(body)
    assert len(snapshot.loop_stack) == 1
    assert snapshot.iteration_path == ""
    assert snapshot.iteration_entries == 0
    assert idempotency_key("run", "body", 1, 3) == "run:body:1:3"
    assert idempotency_key("run", "body", 1, 3, "") == "run:body:1:3"
    assert roundtrip(snapshot).attempts == body["attempts"]


@pytest.mark.parametrize("tick", ["2026-10-12T10:30:00Z", "2026-10-12T12:30:00+02:00"])
async def test_timer_observation_is_frozen_across_dispatch_retry_and_restart(tick):
    engine, _, runs, _, ref = nested_engine(items=[["a"]])
    payload = event(_event_type="timer.tick", tick_time=tick)
    result = await engine.run_rule(ref, "r", payload, TRUSTED_LOCAL)
    frozen = result.snapshot.context
    assert frozen["observed_at"] == datetime.fromisoformat("2026-10-12T10:30:00+00:00").timestamp()
    assert frozen["observation_id"] == "event-1"
    engine.services = replace(engine.services, clock=lambda: 90000)
    duplicate = await engine.run_rule(ref, "r", payload, TRUSTED_LOCAL)
    assert duplicate.snapshot.context == frozen
    runs.snapshots[result.run_id] = roundtrip(result.snapshot)
    resumed = await engine.resume(result.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
    assert resumed.snapshot.context == frozen


@pytest.mark.parametrize("tick", ["2026-10-12T10:30:00", "bad", 42, None, "2026-10-12"])
async def test_timer_rejects_invalid_or_naive_time_before_creating_run(tick):
    engine, _, runs, _, ref = nested_engine()
    with pytest.raises(ValueError, match="tick_time"):
        await engine.run_rule(
            ref, "r", event(_event_type="timer.tick", tick_time=tick), TRUSTED_LOCAL
        )
    assert not runs.snapshots


async def test_other_events_use_durable_dispatch_timestamp_and_id():
    engine, _, _, _, ref = nested_engine()
    result = await engine.run_rule(
        ref,
        "r",
        {"_event_type": "task.completed", "_received_at": 123.5},
        TRUSTED_LOCAL,
        dispatch_id="dispatch-42",
    )
    assert result.snapshot.context["observed_at"] == 123.5
    assert result.snapshot.context["observation_id"] == "dispatch-42"


async def test_interrupted_effect_and_operator_resolution_use_the_second_outer_path():
    from src.commands.contracts.models import CommandResult
    from src.playbooks.engine import OperatorResolution
    from tests.fixtures.contracts.engine_contracts import GATE_CREATE, GateCreateResult

    class ProcessDied(BaseException):
        pass

    payload = nested_artifact(items=[["a"], ["b"]]).model_dump(mode="json")
    body = payload["steps"]["body"]
    body["command"] = "gate_create"
    body["inputs"]["task_id"] = body["inputs"].pop("project_id")
    artifact = PlaybookDefinition.model_validate(payload)
    engine, adapter, runs, _, ref = build(artifact=artifact, contracts=(GATE_CREATE,))
    adapter.queue.extend(
        [
            CommandResult(
                outcome="created", value=GateCreateResult(gate_id="g"), summary="created"
            ),
            ProcessDied(),
        ]
    )
    with pytest.raises(ProcessDied):
        await engine.run_rule(ref, "r", event(), TRUSTED_LOCAL)
    [snapshot] = runs.snapshots.values()
    expected = encode_iteration_path([("outer", 1), ("inner", 0)])
    assert snapshot.iteration_path == expected
    # Older history must not hide the open attempt beyond the UI's 500-row page.
    runs.receipts[:0] = [
        replace(runs.receipts[0], receipt_id=f"old-{i}", step_id="old") for i in range(501)
    ]
    engine, restarted_adapter, _, _, _ = build(
        artifact=artifact, contracts=(GATE_CREATE,), runs=runs
    )
    result = await engine.resume(snapshot.run_id, InterruptedByRestart(1001), TRUSTED_LOCAL)
    assert result.lifecycle is RunLifecycle.PAUSED
    assert result.snapshot.operator_decision.iteration_path == expected
    assert not restarted_adapter.calls
    result = await engine.resume(
        snapshot.run_id, OperatorResolution("accept_outcome", {"outcome": "created"}), TRUSTED_LOCAL
    )
    assert result.lifecycle is RunLifecycle.COMPLETED
    decisions = [r for r in runs.receipts if r.receipt_kind == "operator_decision"]
    assert [r.iteration_path for r in decisions] == [expected]


def test_validation_rejects_cross_frame_entry_shadowing_and_stale_body_reads():
    from tests.playbook_v2_helpers import check, codes

    payload = nested_artifact().model_dump(mode="json")
    # Entry into an inner body bypasses its owning loop.
    payload["steps"]["outer"]["body_entry"] = "body"
    assert "loop_body_escapes" in codes(check(PlaybookDefinition.model_validate(payload)))

    payload = nested_artifact().model_dump(mode="json")
    payload["steps"]["inner"]["item_binding"] = "row"
    assert "loop_variable_shadow" in codes(check(PlaybookDefinition.model_validate(payload)))

    payload = nested_artifact().model_dump(mode="json")
    payload["steps"]["body"]["save_result_as"] = "latest"
    payload["steps"]["inner"]["transitions"]["completed"] = "read"
    payload["steps"]["read"] = {
        **payload["steps"]["body"],
        "save_result_as": None,
        "inputs": {"title": {"type": "binding_ref", "binding": "latest", "path": "task_id"}},
        "transitions": {"created": "outer", "rejected": "bad"},
    }
    diagnostics = check(PlaybookDefinition.model_validate(payload))
    assert any(
        d.code == "binding_not_definitely_assigned" and d.step_id == "read" for d in diagnostics
    )


async def test_symbolic_traversal_uses_nested_frames_and_failure_exits():
    engine, adapter, runs, _, ref = build(artifact=nested_artifact(items=[["a"], ["b"]]))
    tree = await engine.dry_run(ref, event(), TRUSTED_LOCAL)
    assert not tree.truncated
    completed = [p for p in tree.paths if p.nodes[-1].step_id == "done"]
    failed = [p for p in tree.paths if p.nodes[-1].step_id == "bad"]
    assert completed and failed
    assert all(sum(n.step_id == "body" for n in p.nodes) == 2 for p in completed)
    assert all(sum(n.step_id == "body" for n in p.nodes) in {1, 2} for p in failed)
    assert not adapter.calls and not runs.snapshots


async def test_child_waits_and_invocations_preserve_distinct_outer_paths():
    from src.playbooks.invocation import _invocation_context
    from tests.fixtures.contracts.engine_contracts import registry_with
    from tests.test_agent_task_executor import (
        CREATE_TASK,
        StubDatabase,
        StubProfile,
        agent_task_step,
        context,
        created,
        parent_principal,
        run,
    )

    registry, adapter = registry_with(CREATE_TASK)
    adapter.queue.extend([created("child-a"), created("child-b")])
    db = StubDatabase({"reviewer": StubProfile(aq_commands=["a"])})
    ctx = context(registry, principal=parent_principal(aq_commands={"a"}), db=db)
    paths = [encode_iteration_path([("outer", i), ("inner", 0)]) for i in range(2)]
    results = []
    for path in paths:
        iteration_ctx = replace(ctx, iteration_index=0, iteration_path=path)
        with _invocation_context(iteration_ctx) as invocation:
            assert invocation.iteration_path == path
        results.append(await run(agent_task_step(), iteration_ctx))
    assert [result.wait.iteration_path for result in results] == paths
    assert results[0].idempotency_key != results[1].idempotency_key
    assert [result.wait.match["task_id"] for result in results] == ["child-a", "child-b"]


def test_llm_restart_does_not_replay_another_outer_iterations_transcript():
    from src.playbooks.executors.llm import _resume_state

    artifact = nested_artifact()
    engine, _, _, _, ref = build(artifact=artifact)
    paths = [encode_iteration_path([("outer", i), ("inner", 0)]) for i in range(2)]
    ctx = StepContext(
        run_id="run",
        dispatch_id="d",
        artifact_ref=ref,
        artifact=artifact,
        rule_id="r",
        step_id="body",
        principal=TRUSTED_LOCAL,
        scope=ResolutionScope(),
        services=engine.services,
        iteration_index=0,
        iteration_path=paths[1],
        llm_turns=tuple(
            {
                "step_id": "body",
                "iteration": 0,
                "iteration_path": path,
                "attempt": 1,
                "turn_index": 0,
                "transcript_delta": [{"role": "assistant", "content": str(i)}],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
            for i, path in enumerate(paths)
        ),
    )
    messages, next_index, usage = _resume_state("prompt", ctx)
    assert [message["content"] for message in messages] == ["prompt", "1"]
    assert next_index == 1
    assert usage.total == 30
