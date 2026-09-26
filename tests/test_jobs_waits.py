"""Atomic producer/pin/wait rollback, replay, resolution and public transports."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from sqlalchemy import event, func, select, update

from src.agent_waits import WaitError, job_wait_deadline
from src.commands import CommandHandler
from src.commands.contracts import CONTRACTS
from src.config import AppConfig
from src.database.tables import (
    agent_waits,
    jobs,
    job_outbox,
    job_workspace_pins,
    messages,
    workspaces,
)
from src.jobs.result import build_result
from src.models import AgentProfile, Workspace, RepoSourceType
from tests.test_agent_wait_queries import NOW, env as _wait_env, result_count
from tests.test_jobs_queries import values

env = _wait_env


@pytest.fixture
async def setup(env, tmp_path):
    await env.db.create_workspace(
        Workspace(
            id="w",
            project_id="p",
            workspace_path=str(tmp_path),
            source_type=RepoSourceType.LINK,
            locked_by_agent_id="a",
            locked_by_task_id="owner",
        )
    )
    return env


def job_values(**kwargs):
    return values(
        task_id="owner", owner_id="owner", submitted_at=NOW, queue_deadline=NOW + 1800, **kwargs
    )


async def submit(env, **kwargs):
    return await env.db.submit_job(job_values(**kwargs), wait_identity=env.identity)


async def register(env, job_id, *, now=NOW, timeout=None, key="manual"):
    return await env.db.register_agent_wait(
        identity=env.identity,
        kind="job",
        match={"job_id": job_id},
        deadline_at=None if timeout is None else now + timeout,
        idempotency_key=key,
        now=now,
    )


async def finish(env, job, *, ended_at=NOW + 50, exit_code=1):
    job = await env.db.transition_job(job["id"], 0, "starting")
    return await env.db.transition_job(
        job["id"],
        1,
        "failed",
        cleaned=True,
        ended_at=ended_at,
        result=build_result(job, {"exit_code": exit_code}),
        result_ref=job["id"],
    )


async def test_atomic_submit_replay_keeps_one_job_pin_and_wait(setup):
    first, second = await asyncio.gather(submit(setup), submit(setup))
    assert first["id"] == second["id"]
    assert first["wait"]["id"] == second["wait"]["id"]
    assert first["wait"]["deadline_at"] == NOW + 1800 + 7200 + 300
    later = await setup.db.submit_job(
        job_values(),
        wait_identity=setup.identity,
        max_queued=0,
    )
    assert later["wait"]["id"] == first["wait"]["id"]
    async with setup.db._engine.connect() as conn:
        for table in (jobs, job_workspace_pins, agent_waits):
            assert await conn.scalar(select(func.count()).select_from(table)) == 1
        assert await conn.scalar(select(workspaces.c.job_pin_count)) == 1


async def test_active_wait_rolls_back_new_job_pin_and_reservation(setup):
    await setup.db.register_agent_wait(
        identity=setup.identity,
        kind="timer",
        match={"due_at": NOW + 10},
        deadline_at=NOW + 100,
        now=NOW,
        idempotency_key="busy",
    )
    with pytest.raises(WaitError) as exc:
        await submit(setup)
    assert exc.value.code == "wait.already_active"
    async with setup.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(jobs)) == 0
        assert await conn.scalar(select(func.count()).select_from(job_workspace_pins)) == 0
        assert await conn.scalar(select(workspaces.c.job_pin_count)) == 0


async def test_stale_claim_cannot_create_job_or_wait(setup):
    identity = {**setup.identity, "claim_epoch": 0}
    with pytest.raises(WaitError):
        await setup.db.submit_job(job_values(), wait_identity=identity)
    assert await setup.db.list_jobs(project_id="p") == []


async def test_terminal_before_registration_returns_failure_and_one_pointer(setup):
    job = await setup.db.submit_job(job_values())
    await finish(setup, job, ended_at=NOW - 1)
    wait = await register(setup, job["id"])
    assert wait["state"] == "satisfied"
    assert wait["digest"]["outcome"] == "failed"
    assert wait["digest"]["exit_code"] == 1
    assert wait["result_ref"] == f"job:{job['id']}"
    assert await result_count(setup.db, wait["id"]) == 1


async def test_concurrent_registration_completion_and_restart_scan(setup):
    job = await setup.db.submit_job(job_values())
    wait, _ = await asyncio.gather(register(setup, job["id"]), finish(setup, job))
    # Reconciliation consults durable state; no event delivery is required.
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    assert (await setup.db.get_agent_wait(wait["id"]))["state"] == "satisfied"
    assert await result_count(setup.db, wait["id"]) == 1


@pytest.mark.parametrize("delta,state", [(99, "satisfied"), (100, "satisfied"), (101, "expired")])
async def test_deadline_completion_timestamp_wins_and_expiry_does_not_cancel_job(
    setup, delta, state
):
    job = await setup.db.submit_job(job_values())
    wait = await register(setup, job["id"], timeout=100)
    await finish(setup, job, ended_at=NOW + delta)
    await setup.db.reconcile_agent_waits(now=NOW + 200)
    row = await setup.db.get_agent_wait(wait["id"])
    assert row["state"] == state and row["wait_resumed_at"] == NOW + 200
    assert (await setup.db.get_job(job["id"]))["state"] == "failed"


async def test_only_the_awaited_job_exempts_the_current_claim(setup):
    first = await setup.db.submit_job(job_values())
    other = await setup.db.submit_job(job_values(idempotency_key="other"))
    assert await setup.db.blocking_wait_for(setup.session, 1, NOW) is None
    wait = await register(setup, first["id"], timeout=100)
    assert await setup.db.blocking_wait_for(setup.session, 1, NOW)
    await finish(setup, other)
    assert await setup.db.blocking_wait_for(setup.session, 1, NOW + 60)
    await setup.db.cancel_agent_wait(wait["id"], identity=setup.identity, now=NOW + 60)
    assert await setup.db.blocking_wait_for(setup.session, 1, NOW + 60) is None
    assert (await setup.db.get_job(first["id"]))["state"] == "queued"


async def test_other_owner_and_project_denied_missing_source_resolves(setup):
    job = await setup.db.submit_job(job_values())
    for fields in ({"owner_id": "source", "task_id": "source"}, {"project_id": "q"}):
        async with setup.db._engine.begin() as conn:
            await conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(**fields))
        with pytest.raises(WaitError) as exc:
            await register(setup, job["id"])
        assert exc.value.code == "out_of_scope"
    wait = await register(setup, "missing")
    assert wait["digest"] == {"reason": "source_unavailable"}


def test_job_deadline_uses_remaining_budget_and_hard_cap():
    assert job_wait_deadline(dict(started_at=None, queue_deadline=200, run_timeout=400), 100) == 900
    assert job_wait_deadline(dict(started_at=80, queue_deadline=200, run_timeout=400), 100) == 780
    assert (
        job_wait_deadline(dict(started_at=None, queue_deadline=1e9, run_timeout=400), 100) == 86500
    )


async def test_scoped_submit_commands_return_wait_pointer_and_preserve_replay(setup, tmp_path):
    names = [
        "job_submit",
        "job_get",
        "job_list",
        "job_cancel",
        "job_result",
        "job_logs",
        "wait_get",
    ]
    await setup.db.create_profile(
        AgentProfile(
            id="worker-codex", name="Worker", aq_commands=names, harness_tools=[], plugin_tools=[]
        )
    )
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    orch = SimpleNamespace(db=setup.db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
    handler = CommandHandler(orch, config)
    scope = dict(
        kind="session",
        session_id="s",
        session_instance_token="token",
        project_id="p",
        task_id="owner",
    )
    args = dict(
        preset="lint",
        argv=["src"],
        idempotency_key="command",
        wait=True,
        claim_epoch=1,
        _scope=scope,
    )
    first = await handler.execute("job_submit", args)
    assert first["success"], first
    assert "End this turn" in first["next_step"]
    replay = await handler.execute("job_submit", args)
    assert replay["wait"]["id"] == first["wait"]["id"]
    changed = await handler.execute("job_submit", {**args, "wait": False})
    assert changed["error"] == "jobs.idempotency_conflict"
    missing_epoch = await handler.execute("job_submit", {**args, "claim_epoch": None})
    assert not missing_epoch["success"]
    no_wait_stale = await handler.execute(
        "job_submit", {**args, "wait": False, "claim_epoch": None}
    )
    from src.api.models.job import JobErrorResponse

    refusal = JobErrorResponse.model_validate(no_wait_stale)
    assert refusal.error_code == "jobs.stale_claim" and refusal.result is None
    foreign = await handler.execute("job_submit", {**args, "task_id": "source"})
    assert not foreign["success"]
    cancelled = await handler.execute("job_cancel", {"job_id": first["job"]["id"], "_scope": scope})
    assert cancelled["job"]["state"] == "cancelled"
    await setup.db.reconcile_agent_waits(now=1e10)
    assert (await setup.db.get_agent_wait(first["wait"]["id"]))["digest"]["outcome"] == "cancelled"


def test_public_contracts_transports_and_shipped_grants():
    from pathlib import Path
    from src.api.scope import AGENT_COMMAND_SET
    from src.api.codegen import API_EXCLUDED
    from src.cli.auto_commands import EXCLUDED
    from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS
    from src.profiles.parser import parse_profile

    names = {"job_submit", "job_get", "job_list", "job_cancel", "job_result", "job_logs"}
    assert names <= AGENT_COMMAND_SET
    assert not names & (API_EXCLUDED | EXCLUDED | DEFAULT_EXCLUDED_COMMANDS)
    for name in names:
        assert CONTRACTS.require(name).contract.execution.retry_safe
    for template in ("worker-codex", "worker-claude"):
        profile = parse_profile(Path(f"src/profiles/defaults/{template}/profile.md").read_text())
        assert names <= set(profile.capabilities["aq_commands"])
    assert {"job_reconcile", "job_submit_integration"} <= (
        API_EXCLUDED & EXCLUDED & DEFAULT_EXCLUDED_COMMANDS
    )


def test_job_cli_and_detach_mint_keys_use_epoch_and_never_fallback(monkeypatch):
    from src.cli.app import cli
    from src.cli import jobs as job_cli

    calls = []

    @asynccontextmanager
    async def client(*args):
        async def execute(name, params):
            calls.append((name, params))
            return {"success": True, "job": {"id": "j"}, "wait": {"id": "w"}}

        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(job_cli, "_get_client", client)
    monkeypatch.setattr(job_cli, "resolve_claim_epoch", lambda explicit: 7)
    for argv in (
        ["job", "submit", "--preset", "test", "--wait", "--", "tests/test_agent_waits.py"],
        ["run", "--preset", "test", "--wait", "--", "tests/test_agent_waits.py"],
        ["test", "--aq-detach", "--aq-wait", "tests/test_agent_waits.py"],
    ):
        result = CliRunner().invoke(cli, ["--json", *argv])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["data"]["wait"]["id"] == "w"
        assert calls[-1][1]["wait"] and calls[-1][1]["claim_epoch"] == 7
        assert calls[-1][1]["idempotency_key"] in result.stderr
    assert len({params["idempotency_key"] for _, params in calls}) == 3
    result = CliRunner().invoke(cli, ["test", "--aq-wait", "tests/test_agent_waits.py"])
    assert result.exit_code == 2 and len(calls) == 3


async def test_default_wait_replay_keeps_deadline_and_changed_timeout_is_rejected(setup):
    job = await setup.db.submit_job(job_values())
    first = await register(setup, job["id"])
    replay = await register(setup, job["id"], now=NOW + 20)
    assert replay["id"] == first["id"] and replay["deadline_at"] == first["deadline_at"]
    with pytest.raises(WaitError) as exc:
        await register(setup, job["id"], now=NOW + 20, timeout=100)
    assert exc.value.code == "wait.idempotency_conflict"


async def test_job_resolution_outbox_retries_once_after_restart(setup, monkeypatch):
    job = await submit(setup)
    original = setup.db._enqueue_wait_result
    monkeypatch.setattr(setup.db, "_enqueue_wait_result", AsyncMock())
    await finish(setup, job)
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    row = await setup.db.get_agent_wait(job["wait"]["id"])
    assert row["state"] == "satisfied" and row["result_message_id"] is None
    monkeypatch.setattr(setup.db, "_enqueue_wait_result", original)
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    assert await result_count(setup.db, row["id"]) == 1


async def test_cancellation_and_completion_race_has_one_result(setup):
    job = await submit(setup)
    await asyncio.gather(
        setup.db.cancel_agent_wait(job["wait"]["id"], identity=setup.identity, now=NOW + 60),
        finish(setup, job),
    )
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    wait = await setup.db.get_agent_wait(job["wait"]["id"])
    assert wait["state"] in {"cancelled", "satisfied"}
    assert await result_count(setup.db, wait["id"]) == 1


def test_api_job_models_are_registered_with_typed_wait_result():
    from src.api.models import get_all_request_models, get_all_response_models
    from src.api.models.job import JobResponse
    from src.api.spec import build_openapi_spec

    assert (
        get_all_request_models()["job_submit"]
        is CONTRACTS.require("job_submit").contract.execution.args_model
    )
    assert get_all_response_models()["job_submit"] is JobResponse
    spec = build_openapi_spec()
    for command in ("submit", "get", "list", "cancel", "result", "logs"):
        assert f"/api/job/{command}" in spec["paths"]
    assert "AgentWaitRecord" in spec["components"]["schemas"]


async def test_typed_job_errors_preserve_expired_log_result_pointer(setup, monkeypatch):
    from src.api.codegen import _make_route_handler
    from src.commands.contracts.job import JobLogsArgs

    raw = {"success": False, "error": "logs_expired", "result": {"job_id": "j"}}
    handler = SimpleNamespace(execute=AsyncMock(return_value=raw), db=setup.db)
    result = await _make_route_handler("job_logs", JobLogsArgs)(JobLogsArgs(job_id="j"), ch=handler)
    assert result.status_code == 422
    assert json.loads(result.body) == raw


async def test_supervisor_can_subscribe_within_project_and_deleted_task_revokes_source(setup):
    from src.models import SessionRecord

    await setup.db.create_session(
        SessionRecord(
            id="super",
            project_id="p",
            profile_id="supervisor",
            harness="codex",
            provider="fake",
            name="n-super",
            lifecycle="named",
            work_dir="/tmp/super",
            epoch="boot",
            instance_token="super-token",
            started_at=NOW,
            state="running",
        )
    )
    identity = dict(
        session_id="super",
        instance_token="super-token",
        project_id="p",
        claim_epoch=0,
        elevated=True,
    )
    job = await setup.db.submit_job(job_values())
    wait = await setup.db.register_agent_wait(
        identity=identity,
        kind="job",
        match={"job_id": job["id"]},
        deadline_at=None,
        idempotency_key="subscription",
        now=NOW,
    )
    assert wait["owner_kind"] == "supervisor" and wait["state"] == "active"
    # Mirrors task deletion's ON DELETE SET NULL without deleting the held fixture task.
    async with setup.db._engine.begin() as conn:
        await conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(task_id=None))
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    assert (await setup.db.get_agent_wait(wait["id"]))["digest"]["reason"] == "source_unavailable"


def test_ambiguous_job_submit_keeps_key_and_does_not_run_locally(monkeypatch):
    from src.cli.app import cli
    from src.cli import jobs as job_cli
    from src.cli.exceptions import CommandResponseError

    calls = []

    @asynccontextmanager
    async def client(*args):
        async def execute(name, params):
            calls.append(params)
            raise CommandResponseError(name)

        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(job_cli, "_get_client", client)
    result = CliRunner().invoke(cli, ["--json", "test", "--aq-detach", "--aq-wait", "tests/"])
    assert result.exit_code == 1 and len(calls) == 1
    assert calls[0]["idempotency_key"] in result.stderr
    assert json.loads(result.stdout)["error"]["details"]["automatic_retry"] is False


async def test_expired_wait_keeps_queued_producer_and_workspace_pin(setup):
    job = await setup.db.submit_job(job_values())
    wait = await register(setup, job["id"], timeout=100)
    await setup.db.reconcile_agent_waits(now=NOW + 101)
    assert (await setup.db.get_agent_wait(wait["id"]))["state"] == "expired"
    assert (await setup.db.get_job(job["id"]))["state"] == "queued"
    assert await setup.db.workspace_has_job_pin("w")
    assert await setup.db.blocking_wait_for(setup.session, 1, NOW + 101) is None


async def test_lost_job_resolves_once_without_releasing_cleanup_pin_and_replay_attaches(setup):
    job = await submit(setup)
    starting = await setup.db.transition_job(job["id"], 0, "starting")
    await setup.db.transition_job(
        job["id"],
        1,
        "lost",
        result=build_result(starting, None),
        cleanup_blocked=True,
        ended_at=NOW + 50,
    )
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    wait = await setup.db.get_agent_wait(job["wait"]["id"])
    assert wait["state"] == "satisfied" and wait["digest"]["outcome"] == "lost"
    assert await setup.db.workspace_has_job_pin("w")
    replay = await submit(setup)
    assert replay["wait"]["id"] == wait["id"] and replay["wait"]["state"] == "satisfied"
    assert await result_count(setup.db, wait["id"]) == 1


@pytest.mark.parametrize("resolve_first", [False, True])
async def test_terminal_dispatch_uses_only_the_owner_wait_wake(setup, resolve_first):
    from src.messages.delivery import MessageDeliveryEngine
    from tests.test_message_delivery import FakeSessionManager

    job = await submit(setup)
    await finish(setup, job)
    if resolve_first:
        await setup.db.reconcile_agent_waits(now=NOW + 60)
    await asyncio.gather(
        *[setup.db.reconcile_job_results(now=NOW + 60, messaging_enabled=True) for _ in range(2)]
    )
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    pending = await setup.db.get_pending_messages("task", "owner")
    assert [msg.id for msg in pending] == [f"wait:{job['wait']['id']}:result"]
    manager = FakeSessionManager(activity_map={("task", "owner", "p"): "idle"})
    engine = MessageDeliveryEngine(setup.db, manager, AppConfig(), bus=None)
    await engine.run_delivery_pass()
    await setup.db.reconcile_job_results(now=NOW + 61, messaging_enabled=True)
    await setup.db.reconcile_agent_waits(now=NOW + 61)
    await engine.run_delivery_pass()
    assert len(manager.nudges) == 1
    assert job["wait"]["id"] in manager.nudges[0][3]


async def test_unwaited_job_nudges_once_and_prime_reads_create_no_messages(setup, tmp_path):
    from src.jobs.service import JobService
    from src.messages.delivery import MessageDeliveryEngine
    from src.prime.sections import build_messages_section
    from tests.test_message_delivery import FakeSessionManager

    job = await setup.db.submit_job(job_values())
    await finish(setup, job)
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.messages.enabled = True
    # The ordinary service tick dispatches existing terminal work even when
    # new job admission is disabled.
    assert not config.resources.jobs.enabled
    await JobService(setup.db, config).tick()
    pending = await setup.db.get_pending_messages("task", "owner")
    assert [msg.id for msg in pending] == [f"job:{job['id']}:terminal"]
    payload = json.loads(pending[0].body)
    assert payload["result_ref"] == f"job:{job['id']}"
    assert payload["digest"]["outcome"] == "failed"
    manager = FakeSessionManager(activity_map={("task", "owner", "p"): "idle"})
    engine = MessageDeliveryEngine(setup.db, manager, config, bus=None)
    await engine.run_delivery_pass()
    assert manager.nudges[0][3] == f"Handle `aq job result {job['id']} --json`."
    for _ in range(2):
        section = await build_messages_section(
            setup.db, "owner", config=config, mark_delivered=True
        )
        assert f"aq job result {job['id']} --json" in section.body
        await setup.db.reconcile_job_results(now=NOW + 61, messaging_enabled=True)
        await engine.run_delivery_pass()
    assert len(manager.nudges) == 1
    async with setup.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(messages)) == 1


@pytest.mark.parametrize("boundary", ["message", "ack"])
async def test_job_notification_failure_and_disabled_messaging_keep_durable_result(setup, boundary):
    from src.prime.sections import build_messages_section

    job = await setup.db.submit_job(job_values())
    terminal = await finish(setup, job)
    config = AppConfig()
    config.messages.enabled = False
    await setup.db.reconcile_job_results(now=NOW + 60, messaging_enabled=False)
    section = await build_messages_section(setup.db, "owner", config=config)
    assert f"aq job result {job['id']} --json" in section.body
    assert '"outcome": "failed"' in section.body

    def reject(conn, cursor, statement, parameters, context, executemany):
        if (boundary == "message" and statement.startswith("INSERT INTO messages")) or (
            boundary == "ack" and statement.startswith("UPDATE job_outbox")
        ):
            raise RuntimeError("injected job delivery failure")

    event.listen(setup.db._engine.sync_engine, "before_cursor_execute", reject)
    try:
        await setup.db.reconcile_job_results(now=NOW + 61, messaging_enabled=True)
    finally:
        event.remove(setup.db._engine.sync_engine, "before_cursor_execute", reject)
    async with setup.db._engine.connect() as conn:
        assert await conn.scalar(select(job_outbox.c.delivered_at)) is None
        assert await conn.scalar(select(func.count()).select_from(messages)) == 0
    assert (await setup.db.get_job(job["id"]))["result"] == terminal["result"]
    await setup.db.reconcile_job_results(now=NOW + 62, messaging_enabled=True)
    await setup.db.reconcile_job_results(now=NOW + 63, messaging_enabled=True)
    assert len(await setup.db.get_pending_messages("task", "owner")) == 1


async def test_pending_wait_message_intent_suppresses_job_notification(setup, monkeypatch):
    job = await submit(setup)
    original = setup.db._enqueue_wait_result
    monkeypatch.setattr(setup.db, "_enqueue_wait_result", AsyncMock())
    await finish(setup, job)
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    await setup.db.reconcile_job_results(now=NOW + 60, messaging_enabled=True)
    assert await setup.db.get_pending_messages("task", "owner") == []
    assert (await setup.db.get_agent_wait(job["wait"]["id"]))["digest"]["outcome"] == "failed"
    monkeypatch.setattr(setup.db, "_enqueue_wait_result", original)
    await setup.db.reconcile_agent_waits(now=NOW + 61)
    assert await result_count(setup.db, job["wait"]["id"]) == 1


@pytest.mark.parametrize("state", ["busy", "absent", "sleeping", "paused"])
@pytest.mark.parametrize("waiting", [False, True])
async def test_job_output_queues_without_resuming_task_sessions(setup, state, waiting):
    from src.messages.delivery import MessageDeliveryEngine
    from tests.test_message_delivery import FakeSessionManager

    job = await submit(setup) if waiting else await setup.db.submit_job(job_values())
    await finish(setup, job)
    await setup.db.reconcile_agent_waits(now=NOW + 60)
    await setup.db.reconcile_job_results(now=NOW + 60, messaging_enabled=True)
    if state == "paused":
        await setup.db.update_task("owner", status="PAUSED")
    manager = FakeSessionManager(
        activity_map={
            ("task", "owner", "p"): "idle" if state == "paused" else state,
        }
    )
    engine = MessageDeliveryEngine(setup.db, manager, AppConfig(), bus=None)
    await engine.run_delivery_pass()
    assert manager.nudges == [] and manager.ensure_started_calls == []
    assert len(await setup.db.get_pending_messages("task", "owner")) == 1


async def test_prime_job_summary_is_bounded_and_selects_recent_terminal_results(setup):
    from src.prime.sections import build_messages_section

    completed = []
    for index in range(12):
        job = await setup.db.submit_job(job_values(idempotency_key=f"prime-{index}"))
        await finish(setup, job, ended_at=NOW + index)
        completed.append(job["id"])
    rows = await setup.db.list_task_job_results("owner", limit=100)
    assert [row["id"] for row in rows] == list(reversed(completed[2:]))
    config = AppConfig()
    config.messages.enabled = False
    section = await build_messages_section(setup.db, "owner", config=config)
    assert completed[-1] in section.body and completed[0] not in section.body
    assert len(section.body.encode()) <= 6000 + len("Managed job results:\n")
    assert await setup.db.get_pending_messages("task", "owner") == []
