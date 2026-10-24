"""Atomic job producers share verified registration, delivery and rollback."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, update

from src.commands import CommandHandler
from src.commands.contracts.job import JobValue
from src.config import AppConfig
from src.config_wait_policy import WaitProjectOverrides
from src.database.tables import (
    agent_waits, continuations, jobs, job_outbox, job_workspace_pins, messages,
    playbook_private_values, tasks, wait_deadlines, wait_records, workspaces,
)
from src.models import AgentProfile
from src.wait_runtime.job_wait import AtomicJobWait
from src.wait_runtime.service import WaitService
from src.waits.conditions import Scope, WaitError
from tests.test_agent_wait_queries import env as _env
from tests.test_jobs_waits import NOW, finish, job_values, setup as _setup

env = _env
setup = _setup


@pytest.fixture
async def runtime(setup, tmp_path):
    await setup.db.create_profile(AgentProfile(
        id="worker-codex", name="Worker", aq_commands=["job_submit", "wait_get", "wait_ack"],
        harness_tools=[], plugin_tools=[],
    ))
    async with setup.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(branch_name="aq/owner"))
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    config.wait_policy.hibernation_enabled = False
    owner_lock = asyncio.Lock()
    orch = SimpleNamespace(db=setup.db, config=config, bus=SimpleNamespace(emit=AsyncMock()),
                           plugin_registry=None, _task_control_lock=lambda _: owner_lock)
    handler = CommandHandler(orch, config)
    service = orch.wait_service = WaitService(orch, clock=lambda: NOW)
    orch._command_handler = handler
    scope = dict(kind="session", session_id="s", session_instance_token="token", project_id="p",
                 task_id="owner")
    try:
        yield SimpleNamespace(db=setup.db, config=config, handler=handler, service=service,
                              scope=scope, owner=Scope("p", "task", "owner", "s", "token", 1))
    finally:
        await service.lifecycle.stop()


async def submit(r, **kwargs):
    return await r.handler.execute("job_submit", {
        "preset": "lint", "argv": ["src"], "wait": True, "idempotency_key": "verified",
        "claim_epoch": 1, **kwargs, "_scope": r.scope,
    })


async def rows(r, table):
    async with r.db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(table))).mappings()]


async def registration(r):
    return AtomicJobWait(r.service.lifecycle, r.owner, await r.db.wait_source_watermark())


@pytest.mark.parametrize("hibernate", [False, True])
async def test_atomic_command_returns_verified_wait_and_checkpoint_decision(runtime, hibernate):
    r = runtime
    r.config.wait_policy.hibernation_enabled = hibernate
    r.config.wait_policy.kinds["job"].expected_duration_seconds = 3600
    result = await submit(r)
    assert result["success"], result
    JobValue.model_validate({k: v for k, v in result.items() if k != "success"})
    assert result["wait"]["condition"]["job_id"] == result["job"]["id"]
    assert result["wait_id"] == result["wait"]["id"]
    assert result["decision"]["mode"] == ("hibernate" if hibernate else "in_place")
    assert result["next_step"] == ("checkpoint_then_end_turn" if hibernate else "end_turn")
    assert not await rows(r, agent_waits)
    assert (await r.db.get_task("owner")).status.value == "WAITING"
    intents = await rows(r, continuations)
    if hibernate:
        assert len(intents) == 1 and result["continuation_id"] == intents[0]["id"]
        assert result["generation"] == 1
    else:
        assert not intents


@pytest.mark.parametrize("hibernate", [False, True])
async def test_concurrent_submit_replay_keeps_one_job_wait_and_pin(runtime, hibernate):
    r = runtime
    r.config.wait_policy.hibernation_enabled = hibernate
    r.config.wait_policy.kinds["job"].expected_duration_seconds = 3600
    r.scope.pop("task_id")  # Pool credentials bind the live claim, not a fixed task.
    first, second = await asyncio.gather(submit(r), submit(r))
    assert first["success"] and second["success"], (first, second)
    assert first["job"]["id"] == second["job"]["id"]
    assert first["wait_id"] == second["wait_id"]
    for table in (jobs, job_workspace_pins, wait_records):
        assert len(await rows(r, table)) == 1
    assert (await rows(r, workspaces))[0]["job_pin_count"] == 1
    assert len(await rows(r, continuations)) == int(hibernate)
    if hibernate:
        assert first["continuation_id"] == second["continuation_id"]
        assert first["generation"] == second["generation"] == 1
    # WAITING admits only the existing producer, never a new submission key.
    denied = await submit(r, idempotency_key="second-producer")
    assert not denied["success"] and denied["error_code"] == "jobs.stale_claim"
    assert len(await rows(r, jobs)) == 1


async def test_flag_rollback_preserves_atomic_continuation_and_delivers_outcome(runtime):
    r = runtime
    r.config.wait_policy.hibernation_enabled = True
    r.config.wait_policy.kinds["job"].expected_duration_seconds = 3600
    result = await submit(r)
    assert result["success"], result
    intent = (await rows(r, continuations))[0]
    r.config.wait_policy.hibernation_enabled = False
    r.config.wait_policy.v2_enabled = False
    r.config.wait_policy.scheduler_waits_enabled = False
    r.config.wait_policy.v1_bodies = "translate"
    await finish(r, result["job"])
    assert await r.service.processing_enabled()
    await r.service.tick()
    # Writer maintenance runs independently of source delivery. Await its
    # completion before checking the accounting produced by that command.
    await asyncio.gather(*r.service.lifecycle._hibernate_tasks.values())
    # The listener can yield to maintenance's graph lock in the first tick.
    # Drain again after maintenance finishes so the outcome is committed.
    await r.service.listener.tick()
    assert (await rows(r, wait_records))[0]["outcome"] == "satisfied"
    persisted = (await rows(r, continuations))[0]
    # The runtime adds usage accounting while retaining the original intent and
    # checkpoint evidence. It must not cancel or replace the suspension.
    assert {k: v for k, v in persisted.items() if k != "facts"} == {
        k: v for k, v in intent.items() if k != "facts"
    }
    assert persisted["facts"]["usage_checked_at"] == NOW
    assert all(persisted["facts"].get(k) == v for k, v in intent["facts"].items())
    assert not await rows(r, agent_waits)


@pytest.mark.parametrize("boundary", ["wait_records", "continuations", "wait_deadlines"])
async def test_registration_failure_rolls_back_job_pins_wait_and_private_values(runtime, boundary):
    r = runtime
    r.config.wait_policy.hibernation_enabled = True
    r.config.wait_policy.kinds["job"].expected_duration_seconds = 3600
    private = dict(value_id="private", owner_kind="run", owner_id="run", scope_kind="project",
                   scope_owner="p", format_version=1, content_type="json", ciphertext=b"data",
                   key_id="key", nonce=b"x" * 12, digest="digest", byte_count=4)

    def reject(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(f"INSERT INTO {boundary}"):
            raise RuntimeError("injected registration failure")

    reg = await registration(r)
    event.listen(r.db._engine.sync_engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="injected registration failure"):
            await r.db.submit_job(job_values(), wait_registration=reg, private_rows=(private,))
    finally:
        event.remove(r.db._engine.sync_engine, "before_cursor_execute", reject)
    for table in (jobs, job_workspace_pins, wait_records, continuations, wait_deadlines,
                  playbook_private_values):
        assert not await rows(r, table)
    assert (await rows(r, workspaces))[0]["job_pin_count"] == 0
    assert (await r.db.get_task("owner")).status.value == "IN_PROGRESS"


async def test_completion_dispatches_once_and_retry_does_not_sleep_after_ack(runtime):
    r = runtime
    result = await submit(r)
    assert result["success"], result
    await finish(r, result["job"])
    # Result outbox and listener may reconcile in either order.
    await r.db.reconcile_job_results(now=NOW + 60, messaging_enabled=True)
    assert not await rows(r, messages)
    await r.service.tick()
    await r.service.tick()
    wait = (await rows(r, wait_records))[0]
    assert wait["outcome"] == "satisfied" and wait["result_ref"]
    notices = await rows(r, messages)
    assert len(notices) == 1 and notices[0]["body_kind"] == "wait_outcome"
    replay = await submit(r)
    assert replay["success"] and replay["next_step"] == "acknowledge", replay
    ack = await r.handler.execute("wait_get", {
        "wait_id": result["wait_id"], "consume": True, "claim_epoch": 1, "_scope": r.scope,
    })
    assert ack["success"], ack
    replay = await submit(r)
    assert replay["success"] and replay["next_step"] == "continue", replay
    assert (await r.db.get_task("owner")).status.value == "IN_PROGRESS"
    assert len(await rows(r, wait_records)) == 1
    assert (await rows(r, job_outbox))[0]["delivered_at"] is not None


async def test_terminal_source_during_subscribe_commits_audit_without_missed_wake(runtime, monkeypatch):
    r = runtime
    registry = r.service.registry
    observe = registry.observe

    async def complete_before_second_read(conn, cond):
        # A newly inserted job cannot be visible to a separate writer until this
        # transaction commits. Simulate a producer finishing in this transaction
        # to exercise the stricter subscribe/reread boundary as well.
        from src.waits.sources import record_source_event

        await conn.execute(update(jobs).where(jobs.c.id == cond.parameters["job_id"]).values(
            state="failed", state_version=2, ended_at=NOW + 1, result_ref="job:terminal",
        ))
        await record_source_event(conn, source_key=f"job:{cond.parameters['job_id']}", generation=1,
                                  revision="2", outcome_ref="job:terminal", occurred_at=NOW + 1,
                                  payload={"state": "failed"})
        return await observe(conn, cond)

    monkeypatch.setattr(registry, "observe", complete_before_second_read)
    result = await submit(r)
    assert result["success"] and result["next_step"] == "continue", result
    assert result["wait"]["outcome"] == "satisfied"
    assert result["wait"]["cleared_at"] is not None
    assert len(await rows(r, jobs)) == 1 and not await rows(r, continuations)
    assert not await rows(r, agent_waits)


@pytest.mark.parametrize("global_flag,project_flag,verified", [
    (False, True, True), (True, False, False), (False, None, False),
])
async def test_job_wait_obeys_canary_and_explicit_legacy_rollback(runtime, global_flag,
                                                              project_flag, verified):
    r = runtime
    r.config.wait_policy.v2_enabled = global_flag
    r.config.wait_policy.v1_bodies = "translate" if not verified else "refuse"
    if project_flag is not None:
        r.config.wait_policy.projects["p"] = WaitProjectOverrides(v2_enabled=project_flag)
    result = await submit(r)
    assert result["success"], result
    assert len(await rows(r, wait_records)) == int(verified)
    assert len(await rows(r, agent_waits)) == int(not verified)


async def test_stale_owner_and_missing_runtime_cannot_create_producer(runtime):
    r = runtime
    stale = await submit(r, claim_epoch=0)
    assert not stale["success"]
    r.handler.orchestrator.wait_service = None
    missing = await submit(r)
    assert missing["error_code"] == "wait.unavailable"
    assert not await rows(r, jobs)
    reg = AtomicJobWait(r.service.lifecycle, Scope("p", "task", "owner", "s", "wrong", 1),
                        await r.db.wait_source_watermark())
    with pytest.raises(WaitError):
        await r.db.submit_job(job_values(), wait_registration=reg)
    assert not await rows(r, jobs)
