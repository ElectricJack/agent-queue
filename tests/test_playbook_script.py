"""Script engine boundaries, source authority, safe projections and real job settlement."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.playbooks.definition import SCRIPT_OUTCOMES, PlaybookDefinition, ScriptStep, TrustedExecutionMetadata
from src.playbooks.engine import EventArrived, InterruptedByRestart
from src.playbooks.executors import executor_for
from src.playbooks.executors.base import ExecutionMode, StepControl
from src.playbooks.invocation import current_invocation
from src.playbooks.run_state import RunLifecycle
from tests.playbook_v2_engine_helpers import minimal_artifact, artifact_ref_for
from tests.test_command_executor import context
from tests.test_playbook_graph_projection import _project
from tests.test_playbook_script_sandbox import pin, program
from tests.test_v2_engine import build
from tests.test_playbook_script_sandbox import process_host as process_host_fixture, local_request

process_host = process_host_fixture


STEP = "ensure-review-task"


def artifact(pinned=None, **step_kwargs):
    base = minimal_artifact()
    pinned = pinned or pin()
    step = ScriptStep(rule="r", title="Run script", source=base.steps[STEP].source,
                      program=pinned.program, save_result_as="script_result",
                      transitions={**{outcome: "bad" for outcome in SCRIPT_OUTCOMES},
                                   "succeeded": "done", "runtime_error": "bad"}, **step_kwargs)
    body = base.model_copy(update={
        "steps": {**base.steps, STEP: step},
        "trusted_execution": TrustedExecutionMetadata(scripts={STEP: pinned}),
    })
    return PlaybookDefinition.model_validate_json(body.model_dump_json())


def engine_setup(definition=None):
    definition = definition or artifact()
    engine, _, runs, _, ref = build(artifact=definition)
    handler = SimpleNamespace(execute=AsyncMock(return_value={"success": True, "job": {"id": "job"}}))
    db = SimpleNamespace(get_job=AsyncMock())
    engine.services = replace(engine.services, handler=handler, db=db)
    return engine, runs, ref, handler, db


async def start(engine, ref):
    return await engine.run_rule(ref, "r", {"event_type": "task.completed"}, TRUSTED_LOCAL)


def completed_job(snapshot, **extra):
    return {"id": "job", "owner_kind": "playbook", "playbook_run_id": snapshot.run_id,
            "playbook_step_id": STEP, "iteration_path": snapshot.iteration_path, "attempt": 1,
            "artifact_digest": snapshot.artifact_sha256, "cleanup_blocked": False,
            "contract": {"private_owner": {"kind": "run", "identifier": snapshot.run_id,
                                            "scope_kind": "system", "scope_owner": "system"}},
            "state": "succeeded", "result": {"outcome": "succeeded", "cleanup_complete": True,
                                              "binding_ref": {"private_ref": "protected"}}, **extra}


async def test_submits_once_with_server_identity_and_waits_without_output():
    engine, runs, ref, handler, db = engine_setup()
    observed = []

    async def submit(command, args):
        observed.append((command, args, current_invocation(), current_principal()))
        return {"success": True, "job": {"id": "job"}}

    handler.execute.side_effect = submit
    paused = await start(engine, ref)
    assert paused.lifecycle is RunLifecycle.PAUSED
    snapshot = runs.snapshots[paused.run_id]
    assert snapshot.wait.match == {"job_id": "job"}
    assert snapshot.wait.iteration_path == snapshot.iteration_path
    command, args, invocation, principal = observed[0]
    assert command == "playbook_script_run" and set(args) == {"arguments", "environment", "stdin_base64"}
    assert invocation.run_id == paused.run_id and invocation.step_id == STEP
    assert principal.kind is PrincipalKind.PLAYBOOK
    assert principal.policy.aq_commands == frozenset({"playbook_script_run", "job_cancel"})
    db.get_job.return_value = completed_job(snapshot, state="running", result=None)
    pending = await engine.resume(paused.run_id, EventArrived("spurious"), TRUSTED_LOCAL)
    assert pending.lifecycle is RunLifecycle.PAUSED
    assert handler.execute.await_count == 1
    db.get_job.return_value = completed_job(snapshot)
    engine.services.private_values.read.return_value = {"stdout": "private-output", "outcome": "succeeded"}
    done = await engine.resume(paused.run_id, EventArrived("finished"), TRUSTED_LOCAL)
    assert done.lifecycle is RunLifecycle.COMPLETED
    assert handler.execute.await_count == 1
    assert "private-output" not in str(runs.receipts)
    duplicate = await engine.resume(paused.run_id, EventArrived("duplicate"), TRUSTED_LOCAL)
    assert duplicate.outcome == "already_terminal"


async def test_submission_response_loss_recovers_identical_operation_identity():
    engine, runs, ref, handler, _ = engine_setup()
    identities = []

    async def submit(command, args):
        invocation = current_invocation()
        identities.append((invocation.run_id, invocation.step_id, invocation.iteration_path, invocation.attempt))
        if len(identities) == 1:
            raise ConnectionError("reply lost after commit")
        return {"success": True, "job": {"id": "original-job"}}

    handler.execute.side_effect = submit
    interrupted = await start(engine, ref)
    assert interrupted.outcome == "script_submission_interrupted"
    assert runs.snapshots[interrupted.run_id].lifecycle is RunLifecycle.RUNNING
    resumed = await engine.resume(interrupted.run_id, InterruptedByRestart(2000), TRUSTED_LOCAL)
    assert resumed.lifecycle is RunLifecycle.PAUSED
    assert identities[0] == identities[1]
    assert resumed.snapshot.wait.match == {"job_id": "original-job"}


@pytest.mark.parametrize("field,value", [("iteration_path", '[["outer",1],["inner",0]]'),
                                        ("attempt", 2), ("artifact_digest", "other")])
async def test_settlement_refuses_foreign_job_identity(field, value):
    engine, runs, ref, _, db = engine_setup()
    paused = await start(engine, ref)
    db.get_job.return_value = completed_job(runs.snapshots[paused.run_id], **{field: value})
    with pytest.raises(ValueError, match="script_job_identity_invalid"):
        await engine.resume(paused.run_id, EventArrived("finished"), TRUSTED_LOCAL)
    assert runs.snapshots[paused.run_id].lifecycle is RunLifecycle.PAUSED


async def test_cancel_paused_script_dispatches_job_cancel():
    engine, _, ref, handler, _ = engine_setup()
    paused = await start(engine, ref)
    done = await engine.cancel(paused.run_id, TRUSTED_LOCAL)
    assert done.lifecycle is RunLifecycle.CANCELLED
    assert handler.execute.call_args.args == ("job_cancel", {"job_id": "job"})


@pytest.mark.parametrize("mode", [ExecutionMode.DRY_RUN, ExecutionMode.SHADOW])
async def test_symbolic_scripts_show_pins_without_services(mode):
    definition = artifact()
    ctx = replace(context(None), artifact=definition, artifact_ref=artifact_ref_for(definition), mode=mode)
    result = await executor_for("script", mode).execute(definition.steps[STEP], ctx)
    assert result.control is StepControl.UNRESOLVED
    assert result.receipt_inputs["program_sha256"] == definition.trusted_execution.scripts[STEP].program_sha256
    assert "succeeded" in result.possible_outcomes
    assert result.receipt_inputs["program"] == definition.steps[STEP].program.model_dump(mode="json")


def test_graph_exposes_pinned_program_without_environment_values():
    definition = artifact(pin(program(env_allowlist=("LABEL",))),
                          environment={"LABEL": {"type": "literal", "value": "private-env"}})
    graph = _project(definition)
    node = next(node for node in graph["nodes"] if node["id"] == STEP)
    assert node["step_kind"] == "script" and node["script"]["program_sha256"]
    assert node["script"]["environment_names"] == ["LABEL"]
    assert "private-env" not in json.dumps(node)


@pytest.fixture
async def real_engine(tmp_path, process_host):
    import asyncio
    import time
    from unittest.mock import MagicMock
    from src.commands.handler import CommandHandler
    from src.config import AppConfig
    from src.database import Database
    from src.jobs.service import JobService
    from src.playbooks.private_values import PrivateValueService
    from tests.db_fixtures import lease_dsn

    request = await local_request(process_host, 'print("private-real-output")')
    definition = artifact(request.pinned)
    engine, _, ref, _, _ = engine_setup(definition)
    db = Database(lease_dsn("script_engine"))
    await db.initialize()
    private = PrivateValueService(db._engine, active_key_id="test", keys={"test": b"x" * 32})
    db.set_playbook_private_values(private)
    await db.upsert_playbook_artifact(ref, scope="system", path="test", size_bytes=1)
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    config.resources.scripts = process_host[0]
    jobs = JobService(db, config)
    orchestrator = MagicMock(db=db, job_service=jobs)
    handler = CommandHandler(orchestrator, config)
    handler._v2_engine = lambda: engine
    engine.services = replace(engine.services, handler=handler, db=db, private_values=private,
                              clock=time.time)
    engine.runs = db
    engine.waits = db
    yield engine, ref, jobs, handler
    await asyncio.gather(*jobs._children, return_exceptions=True)
    await db.close()


@pytest.mark.parametrize("race", ["restart", "completed_before_registration"])
async def test_real_job_wait_recovers_completion_and_preserves_private_binding(real_engine, race):
    import asyncio
    from src.jobs.service import JobService
    from src.playbooks.engine import PlaybookEngine, WaitScheduler
    from src.playbooks.run_overlay import _receipt

    engine, ref, jobs, handler = real_engine
    db = jobs.db

    async def complete(job):
        await jobs.launch(job)
        await asyncio.wait_for(asyncio.gather(*jobs._children), 15)
        await JobService(db, jobs.config).reconcile(await db.get_job(job["id"]))
        done = await db.get_job(job["id"])
        assert done["state"] == "succeeded", {key: done[key] for key in
                                              ("state", "infra_reason", "result")}

    if race == "completed_before_registration":
        execute = handler.execute
        async def complete_before_reply(command, args):
            result = await execute(command, args)
            assert result["success"], result
            await complete(await db.get_job(result["job"]["id"]))
            return result
        handler.execute = complete_before_reply
    paused = await start(engine, ref)
    assert paused.lifecycle is RunLifecycle.PAUSED, paused
    job = (await db.list_jobs())[0]
    if race == "restart":
        await complete(job)
    # Retention cannot remove a terminal job before its waiting run consumes it.
    assert not await db.purge_terminal_job(job["id"])
    assert await db.retained_terminal_jobs() == []
    restarted = PlaybookEngine(services=engine.services, runs=db, waits=db, activations=engine.activations)
    scheduler = WaitScheduler(restarted, db, TRUSTED_LOCAL)
    assert await scheduler.tick() == (paused.run_id,)
    stored = await db.load_run(paused.run_id)
    assert stored.lifecycle is RunLifecycle.COMPLETED
    resolved = await db.resolve_private_snapshot(stored, TRUSTED_LOCAL)
    assert resolved.bindings["script_result"]["stdout"] == "private-real-output\n"
    receipts = await db.list_receipts(paused.run_id)
    assert "private-real-output" not in str(receipts)
    projected = [_receipt(receipt) for receipt in receipts]
    script = next(row["script"] for row in projected
                  if row["script"] and row["script"]["terminal_cause"])
    assert script["terminal_cause"] == "succeeded" and script["output_refs"]["stdout"]
    assert "private-real-output" not in json.dumps(projected)
    assert await scheduler.tick() == ()
    assert len(await db.list_jobs()) == 1


async def test_real_engine_cancel_and_ambiguous_launch_do_not_replay(real_engine):
    import time
    from src.playbooks.engine import WaitScheduler

    engine, ref, jobs, _ = real_engine
    paused = await start(engine, ref)
    job = (await jobs.db.list_jobs())[0]
    await jobs.db.transition_job(job["id"], 0, "starting", launch_at=time.time() - 31)
    await jobs.reconcile(await jobs.db.get_job(job["id"]))
    assert (await jobs.db.get_job(job["id"]))["cleanup_blocked"]
    assert await WaitScheduler(engine, jobs.db, TRUSTED_LOCAL).tick() == ()
    held = await jobs.db.load_run(paused.run_id)
    assert held.lifecycle is RunLifecycle.PAUSED and held.error_code == "script_execution_uncertain"
    assert len(await jobs.db.list_jobs()) == 1 and not jobs._children


async def test_real_engine_cancels_queued_job(real_engine):
    engine, ref, jobs, _ = real_engine
    paused = await start(engine, ref)
    cancelled = await engine.cancel(paused.run_id, TRUSTED_LOCAL)
    assert cancelled.lifecycle is RunLifecycle.CANCELLED
    job = (await jobs.db.list_jobs())[0]
    assert job["state"] == "cancelled" and job["result"]["cleanup_complete"]


@pytest.mark.parametrize("change", [
    {"shell": "printf changed"}, {"interpreter": "bin/other-shell"},
    {"env_allowlist": ["LABEL"]}, {"limits": {"wall_seconds": 10}},
    {"cwd": "inputs"}, {"output": "json", "json_required": True},
    {"arguments": [{"type": "event_ref", "path": "task_id"}]},
    {"shell": None, "interpreter": None, "argv": ["bin/tool"]},
])
def test_program_changes_are_executable_review_differences(change):
    from src.playbooks.script_policy import ScriptProgram
    from src.playbooks.semantic_diff import diff_definitions
    from src.playbooks.artifact_diff import diff_artifacts
    before = artifact()
    changed = ScriptProgram.model_validate({**program().model_dump(mode="json"), **change})
    after = artifact(pin(changed))
    semantic = diff_definitions(before, after)
    assert semantic.executable_change and semantic.execution_metadata
    visible = diff_artifacts(before, after, base_ref=artifact_ref_for(before),
                             target_ref=artifact_ref_for(after))
    assert "program" in json.dumps(visible) and "modified" in json.dumps(visible)


def test_proposal_requires_matching_source_authored_program_and_server_pins(tmp_path):
    import yaml
    from src.playbooks.authoring import PlaybookSource
    from src.playbooks.proposal import propose
    from tests.playbook_v2_helpers import StubContracts, StubProfiles, StubEvents

    definition = artifact()
    path = tmp_path / "source.md"
    path.write_text("---\n" + yaml.safe_dump({"id": "minimal", "scope": "system",
                    "triggers": ["task.completed"],
                    "scripts": {STEP: program().model_dump(mode="json")}}) + "---\nRun script.\n")
    source = PlaybookSource.load(path, vault_root=tmp_path)
    body = {key: definition.model_dump(mode="json")[key] for key in ("steps", "rules")}

    def proposal(body, pins):
        return propose(source, body, script_pins=pins, contracts=StubContracts(),
                       profiles=StubProfiles(), events=StubEvents(), version=1, enforce_inventory=False)

    accepted = proposal(body, {STEP: pin()})
    assert accepted.artifact is not None, accepted.diagnostics
    assert "script_v1" in accepted.artifact.required_features
    assert accepted.artifact.trusted_execution.scripts[STEP] == pin()
    assert proposal(body, {}).artifact is None
    body["steps"][STEP]["program"]["shell"] = "unreviewed"
    assert proposal(body, {STEP: pin()}).artifact is None


async def test_nested_iterations_have_distinct_submission_and_wait_identities():
    definition = artifact()
    engine, _, ref, handler, _ = engine_setup(definition)
    observed = []
    async def submit(command, args):
        observed.append(current_invocation())
        return {"success": True, "job": {"id": f"job-{len(observed)}"}}
    handler.execute.side_effect = submit
    ctx = replace(context(None), artifact=definition, artifact_ref=ref,
                  services=engine.services, principal=TRUSTED_LOCAL, step_id=STEP)
    paths = ['[["outer",0],["inner",0]]', '[["outer",1],["inner",0]]']
    results = [await executor_for("script", ExecutionMode.LIVE).execute(
        definition.steps[STEP], replace(ctx, iteration_index=0, iteration_path=path))
        for path in paths]
    assert [inv.iteration_path for inv in observed] == paths
    assert [result.wait.iteration_path for result in results] == paths
    assert results[0].idempotency_key != results[1].idempotency_key
    assert results[0].wait.wait_id != results[1].wait.wait_id


def test_validation_rejects_non_string_script_arguments():
    from src.playbooks.validation import validate_definition
    from tests.playbook_v2_helpers import StubContracts, StubProfiles, StubEvents
    definition = artifact(pin(program(arguments=({"type": "literal", "value": 42},))))
    diagnostics = validate_definition(definition, contracts=StubContracts(), profiles=StubProfiles(),
                           events=StubEvents())
    assert any(row.code == "type_mismatch" and row.step_id == STEP for row in diagnostics)
