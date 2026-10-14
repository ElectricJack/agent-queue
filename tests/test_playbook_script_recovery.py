"""Real playbook owners, protected detached capture and no-replay recovery."""
from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from src.commands.playbook_script_commands import PlaybookScriptCommandsMixin
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, principal_context
from src.config import AppConfig
from src.database import Database
from src.database.tables import (job_input_pins, job_workspace_pins, jobs, playbook_private_values,
                                 playbook_v2_runs)
from src.jobs.artifacts import atomic_json, job_directory, read_json
from src.jobs.playbook import (ScriptSubmission, private_service, protect_result, encode_rows,
                               owner, read_stream)
from src.jobs.policy import JobError, next_admission
from src.jobs.script_sandbox import SandboxResult
from src.jobs.service import JobService
from src.playbooks.private_values import PrivateValueService, PrivateValueError
from src.playbooks.run_state import RunLifecycle
from tests.db_fixtures import lease_dsn
from tests.test_playbook_run_repository import ARTIFACT, make_snapshot, seed_artifact
from tests.test_playbook_script_sandbox import process_host as process_host_fixture, local_request


process_host = process_host_fixture


@pytest.fixture
async def setup(tmp_path, process_host):
    db = Database(lease_dsn("script_recovery"))
    await db.initialize()
    await seed_artifact(db, ARTIFACT)
    await db.create_run(make_snapshot(current_step_id="script"))
    private = PrivateValueService(db._engine, active_key_id="test", keys={"test": b"x" * 32})
    db.set_playbook_private_values(private)
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    config.resources.scripts = process_host[0]
    svc = JobService(db, config)
    yield db, svc, process_host
    await asyncio.gather(*svc._children, return_exceptions=True)
    await db.close()


async def submission(setup, code='print("private-output")', **kwargs):
    _, _, host = setup
    req = await local_request(host, code, **kwargs)
    req = req.model_copy(update={"run_id": "run-1"})
    return ScriptSubmission("run-1", "script", "", 1, ARTIFACT, "system", "system", req)


async def submit(svc, script):
    return await svc.submit(project_id=None, task_id=None, session_id=None, claim_epoch=None,
                            workspace_id=None, generation=None, preset="playbook_script", args=[],
                            idempotency_key=script.operation_key, owner_kind="playbook", script=script)


async def test_real_owner_racing_replay_and_pin_retention(setup):
    db, svc, _ = setup
    script = await submission(setup)
    one, two = await asyncio.gather(submit(svc, script), submit(svc, script))
    assert one["id"] == two["id"]
    assert one["project_id"] is None and one["workspace_id"] is None
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(job_input_pins)) == 1
        assert await conn.scalar(select(func.count()).select_from(job_workspace_pins)) == 0
        assert await conn.scalar(select(func.count()).select_from(playbook_private_values)) == 1
    with pytest.raises(JobError, match="idempotency_conflict"):
        await submit(svc, await submission(setup, 'print("changed")'))
    # Full nested iteration identity never collides with another outer iteration.
    other = await submit(svc, replace(script, iteration_path='[["outer",1],["inner",0]]'))
    assert other["id"] != one["id"]
    with pytest.raises(IntegrityError):
        async with db._engine.begin() as conn:
            await conn.execute(delete(playbook_v2_runs).where(playbook_v2_runs.c.run_id == "run-1"))
    cancelled = await svc.cancel(one)
    assert cancelled["result"]["outcome"] == "cancelled"
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(job_input_pins.c.job_id).where(
            job_input_pins.c.job_id == one["id"]))


async def test_detached_capture_restart_import_and_authorized_streams(setup):
    db, svc, _ = setup
    script = await submission(setup, 'import sys; print("private-output"); print("secret-error", file=sys.stderr)')
    job = await submit(svc, script)
    await svc.launch(job)
    await asyncio.wait_for(asyncio.gather(*svc._children), 15)
    directory = job_directory(Path(svc.config.data_dir), job["id"])
    receipt = read_json(directory / "completion.json")
    assert receipt is not None
    assert "private-output" not in (directory / "completion.json").read_text()
    assert "secret-error" not in (directory / "request.json").read_text()
    restarted = JobService(db, svc.config)
    restarted.config.resources.jobs.enabled = False
    await restarted.reconcile(await db.get_job(job["id"]))
    done = await db.get_job(job["id"])
    assert done["state"] == "succeeded", done
    value = await private_service(svc).read(done["result"]["binding_ref"], owner=owner(done),
                                            principal=TRUSTED_LOCAL)
    assert value["stdout"] == "private-output\n" and value["stderr"] == "secret-error\n"
    assert value["terminal_cause"] == "succeeded" and value["capture_complete"]
    chunk = await read_stream(svc, done, TRUSTED_LOCAL, stream="stderr", after=7, limit=5)
    assert base64.b64decode(chunk["chunks"][0]["data"]) == b"error"
    with pytest.raises(PrivateValueError, match="unauthorized"):
        await read_stream(svc, done, None)
    from src.playbooks.invocation import _invocation_context
    from tests.test_command_executor import context

    caller = replace(TRUSTED_LOCAL, kind=PrincipalKind.PLAYBOOK)
    with _invocation_context(context(None, principal=caller, run_id="other-run")):
        with pytest.raises(PrivateValueError, match="unauthorized"):
            await read_stream(svc, done, caller)
    async with db._engine.connect() as conn:
        for table in (jobs, playbook_private_values):
            assert "private-output" not in str((await conn.execute(select(table))).all())
    await restarted.reconcile(done)
    assert (await db.get_job(job["id"]))["result"] == done["result"]


@pytest.mark.parametrize("artifact", ["absent", "wrong_nonce", "incomplete"])
async def test_ambiguous_execution_pauses_without_terminal_result_or_replay(setup, artifact):
    db, svc, _ = setup
    job = await submit(svc, await submission(setup))
    job = await db.transition_job(job["id"], 0, "starting", launch_at=time.time() - 31)
    if artifact != "absent":
        directory = job_directory(Path(svc.config.data_dir), job["id"])
        result, rows = protect_result(private_service(svc), job, SandboxResult(
            job["contract"]["execution_id"], "runtime_error", cleanup_complete=False))
        atomic_json(directory / "completion.json", {
            "job_id": job["id"], "nonce": "wrong" if artifact == "wrong_nonce" else job["runner_nonce"],
            "script_result": result, "private_rows": encode_rows(rows)})
    await svc.reconcile(job)
    held = await db.get_job(job["id"])
    assert held["state"] == "starting" and held["result"] is None and held["cleanup_blocked"]
    run = await db.load_run("run-1")
    assert run.lifecycle is RunLifecycle.PAUSED
    version = run.version
    await svc.reconcile(held)
    assert (await db.load_run("run-1")).version == version
    # Even a repeated submission rejoins the original launch, never reruns it.
    assert (await submit(svc, await submission(setup)))["id"] == job["id"]


async def test_private_key_readiness_prevents_job_creation(setup):
    db, svc, _ = setup
    db.set_playbook_private_values(PrivateValueService(db._engine))
    with pytest.raises(PrivateValueError, match="key_unavailable"):
        await submit(svc, await submission(setup))
    assert await db.list_jobs() == []


async def test_shared_capacity_and_per_run_admission(setup):
    _, svc, _ = setup
    script = await submission(setup)
    first = await submit(svc, script)
    second = await submit(svc, replace(script, attempt=2))
    assert next_admission([first, second], time.time(), 3, 10)["id"] == first["id"]
    first = {**first, "state": "running"}
    assert next_admission([first, second], time.time(), 3, 10) is None


@pytest.mark.parametrize("kind", [PrincipalKind.LOCAL, PrincipalKind.SERVICE, PrincipalKind.PLAYBOOK])
async def test_raw_callers_cannot_impersonate_script_invocation(kind):
    handler = PlaybookScriptCommandsMixin()
    principal = replace(TRUSTED_LOCAL, kind=kind)
    with principal_context(principal):
        result = await handler._cmd_playbook_script_run({"run_id": "forged"})
    assert result["error_code"] == "unauthorized"


async def test_live_restart_rejoin_and_cancellation(setup):
    db, svc, _ = setup
    job = await submit(svc, await submission(setup, 'import time; print("hello", flush=True); time.sleep(10)'))
    await svc.launch(job)
    directory = job_directory(Path(svc.config.data_dir), job["id"])
    for _ in range(100):
        if (directory / "started.json").exists():
            break
        await asyncio.sleep(.05)
    restarted = JobService(db, svc.config)
    await restarted.reconcile(await db.get_job(job["id"]))
    live = await db.get_job(job["id"])
    assert live["state"] == "running" and live["result"] is None
    await restarted.cancel(live)
    await asyncio.wait_for(asyncio.gather(*svc._children), 15)
    await restarted.reconcile(await db.get_job(job["id"]))
    done = await db.get_job(job["id"])
    assert done["state"] == "cancelled"
    assert done["result"]["outcome"] == "cancelled"
    assert done["result"]["cleanup_complete"]


@pytest.mark.parametrize("tamper", ["ciphertext", "metadata", "owner", "missing_key"])
async def test_untrusted_or_unreadable_receipts_never_publish_result(setup, tamper):
    db, svc, _ = setup
    job = await submit(svc, await submission(setup))
    await svc.launch(job)
    await asyncio.wait_for(asyncio.gather(*svc._children), 15)
    directory = job_directory(Path(svc.config.data_dir), job["id"])
    receipt = read_json(directory / "completion.json")
    if tamper == "ciphertext":
        receipt["private_rows"][0]["ciphertext"] = base64.b64encode(b"bad").decode()
    elif tamper == "metadata":
        receipt["script_result"]["outcome"] = "cancelled"
    elif tamper == "owner":
        receipt["private_rows"][0]["owner_id"] = "other-run"
    else:
        db.set_playbook_private_values(PrivateValueService(db._engine))
    atomic_json(directory / "completion.json", receipt)
    await svc.reconcile(await db.get_job(job["id"]))
    held = await db.get_job(job["id"])
    assert held["result"] is None and held["cleanup_blocked"]
    assert (await db.load_run("run-1")).lifecycle is RunLifecycle.PAUSED
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(playbook_private_values)) == 1
        assert await conn.scalar(select(job_input_pins.c.job_id)) == job["id"]


async def test_retirement_releases_only_settled_results_and_inputs(setup):
    db, svc, _ = setup
    job = await submit(svc, await submission(setup))
    assert not await db.purge_terminal_job(job["id"])
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(job_input_pins.c.job_id)) == job["id"]
    await svc.launch(job)
    await asyncio.wait_for(asyncio.gather(*svc._children), 15)
    await svc.reconcile(await db.get_job(job["id"]))
    quota_file = Path(svc.config.resources.scripts.state_root) / "quotas/quota.json"
    assert read_json(quota_file)
    directory = job_directory(Path(svc.config.data_dir), job["id"])
    receipt = read_json(directory / "completion.json")
    receipt["quota_key"] = "f" * 64  # Retirement derives the actual reservation from SQL.
    atomic_json(directory / "completion.json", receipt)
    svc.settings.result_days = 0
    await svc.sweep()
    assert read_json(quota_file) == {}
    assert await db.get_job(job["id"]) is None
    async with db._engine.connect() as conn:
        assert not await conn.scalar(select(job_input_pins.c.job_id))
        assert await conn.scalar(select(func.count()).select_from(playbook_private_values)) == 0


@pytest.mark.parametrize("code,outcome,json_status", [
    ('print("{\\\"answer\\\":42}")', "succeeded", "parsed"),
    ('print("not json")', "succeeded", "invalid"),
    ('import sys; print("partial"); sys.exit(3)', "nonzero_exit", "invalid"),
])
async def test_capture_preserves_exit_and_strict_json_metadata(setup, code, outcome, json_status):
    db, svc, _ = setup
    job = await submit(svc, await submission(setup, code, output="json"))
    await svc.launch(job)
    await asyncio.wait_for(asyncio.gather(*svc._children), 15)
    await svc.reconcile(await db.get_job(job["id"]))
    done = await db.get_job(job["id"])
    assert done["result"]["outcome"] == outcome
    assert done["result"]["json_status"] == json_status


@pytest.mark.parametrize("variant", ["script", "command", "llm", "wrong_dispatch", "wrong_scope", "no_grant", "forged_owner",
                                          "wrong_digest", "wrong_generation"])
async def test_submission_requires_the_actual_pinned_script_step(variant):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.playbooks.definition import ScriptStep, LlmStep, TrustedExecutionMetadata
    from src.playbooks.invocation import _invocation_context
    from src.profiles.capabilities import CapabilityPolicy
    from tests.playbook_v2_engine_helpers import minimal_artifact, artifact_ref_for
    from tests.test_command_executor import context
    from tests.test_playbook_script_sandbox import pin

    artifact = minimal_artifact()
    pinned = pin()
    step_id = "ensure-review-task"
    original = artifact.steps[step_id]
    step = ScriptStep(rule="r", title="Script", source=original.source,
                      program=pinned.program, transitions={"succeeded": "done"})
    if variant == "command":
        step = original.model_copy(update={"command": "playbook_script_run"})
    if variant == "llm":
        step = LlmStep(rule="r", title="LLM", source=original.source,
                       profile_id="worker", prompt={"type": "literal", "value": "hi"},
                       output_schema={"type": "object"},
                       budget={"max_calls": 1, "max_output_tokens": 100,
                               "max_total_tokens": 200, "timeout_seconds": 10},
                       tool_use={"aq_commands": ["playbook_script_run"]},
                       transitions={"success": "done"})
    # Unit-test the dormant boundary interface; the production definition parser
    # must still reject ScriptStep until E3 installs its union and executor.
    artifact = artifact.model_copy(update={
        "steps": {**artifact.steps, step_id: step},
        "trusted_execution": TrustedExecutionMetadata(scripts={step_id: pinned}),
    })
    ref = artifact_ref_for(artifact)
    if variant == "wrong_digest":
        ref = replace(ref, artifact_sha256="sha256:" + "f" * 64)
    if variant == "wrong_generation":
        # Simulate a stale server snapshot; normal ArtifactRef construction also
        # rejects this, independently of the mutation boundary's own check.
        object.__setattr__(ref, "schema_generation", 1)
    policy = CapabilityPolicy.from_namespaces(aq_commands=["playbook_script_run"])
    caller = replace(TRUSTED_LOCAL, kind=PrincipalKind.PLAYBOOK, policy=policy,
                     project_id="foreign" if variant == "wrong_scope" else None)
    if variant == "no_grant":
        caller = replace(caller, policy=CapabilityPolicy())
    ctx = replace(context(None, principal=caller), artifact=artifact, artifact_ref=ref)
    run = SimpleNamespace(dispatch_id="different" if variant == "wrong_dispatch" else "d-1",
                          rule_id="r", artifact_sha256=ref.artifact_sha256, mode="live")
    handler = PlaybookScriptCommandsMixin()
    handler.db = SimpleNamespace(load_run=AsyncMock(return_value=run))
    handler._v2_engine = lambda: SimpleNamespace(services=SimpleNamespace(
        artifact_store=SimpleNamespace(load=lambda _: artifact)))
    service = SimpleNamespace(submit=AsyncMock(return_value={"id": "job", "contract": {}}))
    handler._jobs = lambda: service
    args = {"run_id": "forged"} if variant == "forged_owner" else {}
    with principal_context(caller), _invocation_context(ctx):
        result = await handler._cmd_playbook_script_run(args)
    assert result["success"] is (variant == "script"), result
    if variant == "script":
        submitted = service.submit.call_args.kwargs["script"]
        assert submitted.run_id == ctx.run_id and submitted.step_id == ctx.step_id
        assert submitted.request.pinned == pinned and "contract" not in result["job"]
    else:
        service.submit.assert_not_called()


async def test_queue_timeout_is_distinct_and_does_not_launch(setup):
    from sqlalchemy import update

    db, svc, _ = setup
    job = await submit(svc, await submission(setup))
    async with db._engine.begin() as conn:
        await conn.execute(update(jobs).where(jobs.c.id == job["id"])
                           .values(queue_deadline=time.time() - 1))
    await svc.tick()
    done = await db.get_job(job["id"])
    assert done["state"] == "failed"
    assert done["result"]["outcome"] == "admission_timed_out"
    assert done["result"]["reason"] == "queue"
    assert done["result"]["started_at"] is None
    assert not svc._children


async def test_oversized_private_input_rejected_before_storage(setup):
    db, svc, _ = setup
    script = await submission(setup)
    script = replace(script, request=script.request.model_copy(update={
        "environment": {"LABEL": "x" * (2 * 1024**2)},
    }))
    with pytest.raises(JobError, match="script_input_too_large"):
        await submit(svc, script)
    assert await db.list_jobs() == []


def test_script_model_remains_dormant_until_engine_integration():
    from pydantic import ValidationError
    from src.playbooks.definition import PlaybookDefinition
    from tests.playbook_v2_engine_helpers import minimal_artifact
    from tests.test_playbook_script_sandbox import program

    body = minimal_artifact().model_dump(mode="json")
    body["steps"]["ensure-review-task"] = {
        "type": "script", "rule": "r", "title": "Dormant",
        "source": {"path": "x.md", "start_line": 1, "end_line": 1},
        "program": program().model_dump(mode="json"), "transitions": {"succeeded": "done"},
    }
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        PlaybookDefinition.model_validate(body)
