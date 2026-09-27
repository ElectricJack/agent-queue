"""Scoped wait command, contract, grant and CLI behavior."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from sqlalchemy import update

from src.agent_waits import AgentWaitReconciler
from src.commands import CommandHandler
from src.commands.contracts import CONTRACTS
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig
from src.database.tables import tasks
from src.models import AgentProfile
from src.profiles.capabilities import DENY_ALL
from tests.test_agent_wait_queries import NOW, env as _wait_env

env = _wait_env


@pytest.fixture
async def commands(env):
    await env.db.create_profile(
        AgentProfile(
            id="worker-codex",
            name="Worker",
            aq_commands=["wait_register", "wait_get", "wait_list", "wait_cancel"],
            harness_tools=[],
            plugin_tools=[],
        )
    )
    config = AppConfig()
    orch = SimpleNamespace(db=env.db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
    return CommandHandler(orch, config)


def scope(**extra):
    return dict(
        kind="session", session_id="s", session_instance_token="token", project_id="p", **extra
    )


async def execute(commands, name, args=None, **extra_scope):
    return await commands.execute(name, {**(args or {}), "_scope": scope(**extra_scope)})


async def test_register_show_list_cancel_owner_is_derived(commands, env):
    # A task ref names the producer, not the owner in server scope.
    result = await execute(
        commands,
        "wait_register",
        dict(kind="task", ref="source", timeout=100, idempotency_key="key", claim_epoch=1),
    )
    assert result["success"], result
    row = result["wait"]
    assert row["owner_id"] == "owner" and row["session_id"] == "s"
    assert "End this turn" in result["next_step"]
    shown = await execute(commands, "wait_get", {"wait_id": row["id"]})
    assert shown["wait"]["id"] == row["id"]
    listed = await execute(commands, "wait_list")
    assert listed["count"] == 1
    cancelled = await execute(commands, "wait_cancel", {"wait_id": row["id"], "claim_epoch": 1})
    assert cancelled["wait"]["state"] == "cancelled"
    again = await execute(commands, "wait_cancel", {"wait_id": row["id"], "claim_epoch": 1})
    assert again["wait"]["version"] == cancelled["wait"]["version"]


@pytest.mark.parametrize(
    "fields",
    [
        {"task_id": "source"},
        {"session_id": "someone-else"},
        {"project_id": "q"},
        {"owner_id": "source"},
        {"claim_epoch": 0},
        {"claim_epoch": None},
        {"timeout": 86401},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"due_at": NOW, "kind": "task"},
    ],
)
async def test_register_rejects_forged_owner_stale_epoch_and_unbounded_wait(commands, fields):
    result = await execute(
        commands,
        "wait_register",
        {
            "kind": "task",
            "ref": "source",
            "idempotency_key": "invalid",
            "claim_epoch": 1,
            **fields,
        },
    )
    assert result.get("error"), result


async def test_foreign_target_and_missing_job_source(commands):
    for kind, ref, code in (("task", "foreign", "out_of_scope"),):
        result = await execute(
            commands,
            "wait_register",
            dict(kind=kind, ref=ref, idempotency_key="key", claim_epoch=1),
        )
        assert result["error_code"] == code
    missing = await execute(
        commands,
        "wait_register",
        dict(kind="job", ref="missing", idempotency_key="job", claim_epoch=1),
    )
    assert missing["success"] and missing["wait"]["digest"]["reason"] == "source_unavailable"


async def test_history_across_epochs_remains_readable_but_not_cancellable(commands, env):
    registered = await execute(
        commands,
        "wait_register",
        dict(kind="task", ref="source", idempotency_key="key", claim_epoch=1),
    )
    wait_id = registered["wait"]["id"]
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
    shown = await execute(commands, "wait_get", {"wait_id": wait_id})
    assert shown["success"]
    cancelled = await execute(commands, "wait_cancel", {"wait_id": wait_id, "claim_epoch": 1})
    assert cancelled["error_code"] == "stale_claim"


async def test_read_cannot_cross_owners(commands, env):
    registered = await execute(
        commands,
        "wait_register",
        dict(kind="task", ref="source", idempotency_key="key", claim_epoch=1),
    )
    wait_id = registered["wait"]["id"]
    async with env.db._engine.begin() as conn:
        from src.database.tables import agent_waits

        await conn.execute(
            update(agent_waits).where(agent_waits.c.id == wait_id).values(owner_id="other")
        )
    result = await execute(commands, "wait_get", {"wait_id": wait_id})
    assert result["error_code"] == "out_of_scope"
    assert (await execute(commands, "wait_list"))["count"] == 0


async def test_reconciler_is_service_only_and_calls_command_boundary(commands):
    rejected = await execute(commands, "reconcile_agent_waits")
    assert rejected.get("error")
    result = await AgentWaitReconciler(commands).tick(now=NOW)
    assert result["success"] and result["scanned"] == 0


@pytest.mark.parametrize("terminal", ["COMPLETED", "FAILED", "BLOCKED", "expired"])
@pytest.mark.parametrize("busy", [False, True])
async def test_task_wait_cascade_resolves_and_wakes_idle_pool_holder(
    commands, env, monkeypatch, terminal, busy
):
    """Real wait cascade, durable outbox and lens against a fake terminal."""
    from src.messages.delivery import MessageDeliveryEngine
    from src.messages.session_lens import SessionLens
    from src.orchestrator.core import Orchestrator
    from src.sessions import SessionProviderRegistry
    from src.sessions.fake import FakeProvider
    from src.sessions.provider import SessionSpec
    from tests.test_agent_wait_queries import complete

    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW)
    config = commands.config
    config.sessions.enabled = True
    config.messages.enabled = True
    orch = commands.orchestrator
    orch.config = config
    Orchestrator.set_command_handler(orch, commands)
    orch.transcript_watcher = SimpleNamespace(tick=AsyncMock())
    orch.agent_questions = SimpleNamespace(tick=AsyncMock())
    orch.session_reconciler = SimpleNamespace(tick=AsyncMock())
    # Recovery is unrelated to waits and must not leave background work behind.
    monkeypatch.setattr("src.integration.completion_recovery.schedule_ready_owner_recovery", lambda _: None)
    providers = SessionProviderRegistry({"fake": FakeProvider}, config=config)
    fake = providers.create("fake")
    await fake.start(SessionSpec(
        session_name=env.session.name, work_dir=env.session.work_dir,
        command=("codex",), instance_token=env.session.instance_token,
    ))
    fake.sessions[env.session.name].activity = NOW - 100
    lens = SessionLens(
        db=env.db, providers=providers, spec_builder=None, harness_registry=None,
        config=config, profiles_loader=AsyncMock(),
    )
    orch.message_delivery = MessageDeliveryEngine(env.db, lens, config)
    orch.supervisor_delivery_watchdog = SimpleNamespace(tick=AsyncMock())
    monkeypatch.setattr(env.db, "queue_task_recovery_notifications", AsyncMock())
    orch._last_delivery_pass = 0
    registered = await execute(commands, "wait_register", dict(
        kind="task", ref="source", timeout=100, idempotency_key="cascade", claim_epoch=1,
    ))
    assert registered["success"], registered
    wait_id = registered["wait"]["id"]
    assert registered["wait"]["state"] == "active"
    if terminal != "expired":
        await complete(env.db, at=NOW + 50, status=terminal,
                       outcome="pass" if terminal == "COMPLETED" else "fail")
    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW + 200)

    await Orchestrator._reconcile_sessions(orch)
    resolved = await env.db.get_agent_wait(wait_id)
    assert resolved["state"] == ("expired" if terminal == "expired" else "satisfied")
    assert resolved["wait_resumed_at"] == NOW + 200
    if terminal != "expired":
        assert resolved["digest"]["status"] == terminal
        assert resolved["digest"]["outcome"] == ("pass" if terminal == "COMPLETED" else "fail")
    if busy:
        fake.sessions[env.session.name].activity = NOW + 200
        await Orchestrator._deliver_messages(orch)
        assert fake.sent_nudges == []
        message = await env.db.get_message(resolved["result_message_id"])
        assert message.delivered_at is None
        fake.sessions[env.session.name].activity = NOW - 100
        orch._last_delivery_pass = 0
    await Orchestrator._deliver_messages(orch)
    assert fake.sent_nudges == [
        (env.session.name, f"Handle `aq wait show {wait_id} --json`."),
    ]
    message = await env.db.get_message(resolved["result_message_id"])
    assert message.delivered_at == NOW + 200
    # Repeated reconciliation/delivery must not submit the result twice.
    await Orchestrator._reconcile_sessions(orch)
    orch._last_delivery_pass = 0
    await Orchestrator._deliver_messages(orch)
    assert len(fake.sent_nudges) == 1
    assert (await env.db.get_session("s")).task_id == "owner"
    assert (await env.db.get_task("owner")).status.value == "IN_PROGRESS"


# -- idle wake for plain messages and message-thread waits (2026-09-27) --------
#
# noble-crest, prime-glacier and wise-ember.17 sat idle 15-30 minutes on
# supervisor answers: the cascade tried every pass, but the terminal guard read
# an idle codex-cli 0.157 composer (two footer rows) as a draft. These drive the
# real reconcile + delivery cascade and lens into a TmuxProvider that renders
# that layout, and into the fake provider.

CODEX_0157_BELOW = [
    "",
    "  GPT-6-Sol xhigh · /tmp/wait · Process agent-queue tasks",
    "  ? for shortcuts",
]


def _terminal(kind, env):
    """``(provider, typed)`` — *typed()* lists the text each nudge submitted."""
    from src.sessions.fake import FakeProvider
    from tests.test_tmux_nudge_drafts import CODEX_PLACEHOLDER, Composer, provider_for

    if kind == "fake":
        provider = FakeProvider()
        return provider, lambda: [text for _name, text in provider.sent_nudges]

    class Codex0157(Composer):
        # Codex repaints its dim placeholder once a prompt is submitted.
        async def tmux(self, *args, stdin=None, **kwargs):
            result = await super().tmux(*args, stdin=stdin, **kwargs)
            if args[0] == "send-keys" and args[-1] == "Enter":
                self.row = CODEX_PLACEHOLDER
            return result

    composer = Codex0157(
        row=CODEX_PLACEHOLDER, cursor_y=17, height=18 + len(CODEX_0157_BELOW),
        below=list(CODEX_0157_BELOW),
    )
    provider = provider_for(composer)
    provider.is_running = AsyncMock(return_value=True)
    provider.last_activity = AsyncMock(return_value=NOW - 100)
    return provider, lambda: list(composer.submitted)


async def _wire_delivery(commands, env, monkeypatch, provider):
    from src.messages.delivery import MessageDeliveryEngine
    from src.messages.session_lens import SessionLens
    from src.orchestrator.core import Orchestrator
    from src.sessions import SessionProviderRegistry
    from src.sessions.fake import FakeProvider
    from src.sessions.provider import SessionSpec

    config = commands.config
    config.sessions.enabled = True
    config.messages.enabled = True
    orch = commands.orchestrator
    orch.config = config
    Orchestrator.set_command_handler(orch, commands)
    orch.transcript_watcher = SimpleNamespace(tick=AsyncMock())
    orch.agent_questions = SimpleNamespace(tick=AsyncMock())
    orch.session_reconciler = SimpleNamespace(tick=AsyncMock())
    monkeypatch.setattr(
        "src.integration.completion_recovery.schedule_ready_owner_recovery", lambda _: None
    )
    if isinstance(provider, FakeProvider):
        await provider.start(SessionSpec(
            session_name=env.session.name, work_dir=env.session.work_dir,
            command=("codex",), instance_token=env.session.instance_token,
        ))
        provider.sessions[env.session.name].activity = NOW - 100
    providers = SessionProviderRegistry({"fake": FakeProvider}, config=config)
    providers._instances["fake"] = provider
    lens = SessionLens(
        db=env.db, providers=providers, spec_builder=None, harness_registry=None,
        config=config, profiles_loader=AsyncMock(),
    )
    # Hermetic: activity falls back to the terminal, never ~/.codex transcripts.
    monkeypatch.setattr(lens, "_transcript_activity", AsyncMock(return_value=None))
    orch.message_delivery = MessageDeliveryEngine(env.db, lens, config)
    orch.supervisor_delivery_watchdog = SimpleNamespace(tick=AsyncMock())
    monkeypatch.setattr(env.db, "queue_task_recovery_notifications", AsyncMock())
    orch._last_delivery_pass = 0
    return orch


@pytest.mark.parametrize("terminal", ["fake", "codex-0.157"])
@pytest.mark.parametrize("route", ["task", "agent_message"])
async def test_plain_message_wakes_idle_pool_holder(commands, env, monkeypatch, terminal, route):
    from src.orchestrator.core import Orchestrator

    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW)
    provider, typed = _terminal(terminal, env)
    orch = await _wire_delivery(commands, env, monkeypatch, provider)
    if route == "task":
        sent = await commands._cmd_message_send({
            "project_id": "p", "to_kind": "task", "to_id": "owner",
            "from_kind": "user", "from_id": "cli", "body": "reparented; close now",
        })
    else:
        sent = await commands._cmd_agent_message(
            {"target": "owner", "body": "SUPERVISOR: reparented; close now"}
        )
    message_id = sent["message_id"]

    await Orchestrator._deliver_messages(orch)

    assert typed() == [f"Handle `aq message status {message_id} --json`."]
    message = await env.db.get_message(message_id)
    assert message.delivered_at == NOW and message.via == "nudge"


async def _ask_on_thread(commands, env):
    asked = await commands._cmd_message_send({
        "project_id": "p", "from_kind": "session", "from_id": "s",
        "to_kind": "user", "to_id": "dashboard", "thread_id": "owner:graph-filing",
        "body": "Please reply on this thread once the approved graph is filed.",
    })
    seq = (await env.db.get_message(asked["message_id"])).created_seq
    registered = await execute(commands, "wait_register", dict(
        kind="message", ref="owner:graph-filing", after_seq=seq, timeout=900,
        idempotency_key="graph-filing", claim_epoch=1,
    ))
    assert registered["success"], registered
    return asked["message_id"], registered["wait"]["id"]


@pytest.mark.parametrize("terminal", ["fake", "codex-0.157"])
@pytest.mark.parametrize("answer", ["agent_message_reply_to", "message_reply"])
async def test_thread_wait_fires_on_reply_and_wakes_idle_holder(
    commands, env, monkeypatch, terminal, answer
):
    from src.orchestrator.core import Orchestrator

    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW)
    provider, typed = _terminal(terminal, env)
    orch = await _wire_delivery(commands, env, monkeypatch, provider)
    asked_id, wait_id = await _ask_on_thread(commands, env)

    if answer == "agent_message_reply_to":
        replied = await commands._cmd_agent_message(
            {"target": "owner", "body": "SUPERVISOR: filed", "reply_to": asked_id}
        )
    else:
        replied = await commands._cmd_message_reply({
            "message_id": asked_id, "body": "filed", "from_kind": "user", "from_id": "dashboard",
        })
    reply_id = replied["message_id"] if answer == "agent_message_reply_to" else replied["reply_id"]
    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW + 30)

    await Orchestrator._reconcile_sessions(orch)
    resolved = await env.db.get_agent_wait(wait_id)
    assert resolved["state"] == "satisfied"
    assert resolved["digest"]["message_id"] == reply_id
    assert resolved["digest"]["thread_id"] == "owner:graph-filing"

    await Orchestrator._deliver_messages(orch)
    assert f"Handle `aq wait show {wait_id} --json`." in typed()
    assert (await env.db.get_message(resolved["result_message_id"])).delivered_at == NOW + 30


async def test_unthreaded_guidance_does_not_satisfy_a_thread_wait(commands, env, monkeypatch):
    """The 2026-09-27 noble-crest miss: plain guidance never lands on the thread."""
    from src.orchestrator.core import Orchestrator

    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW)
    provider, _typed = _terminal("fake", env)
    orch = await _wire_delivery(commands, env, monkeypatch, provider)
    _asked_id, wait_id = await _ask_on_thread(commands, env)
    await commands._cmd_agent_message({"target": "owner", "body": "SUPERVISOR: filed"})
    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW + 30)

    await Orchestrator._reconcile_sessions(orch)

    assert (await env.db.get_agent_wait(wait_id))["state"] == "active"


@pytest.mark.parametrize("terminal", ["COMPLETED", "FAILED", "BLOCKED"])
@pytest.mark.parametrize("archived", [False, True])
async def test_doctor_reports_active_wait_with_terminal_target(commands, env, terminal, archived):
    from sqlalchemy import delete, insert, select
    from src.database.tables import archived_tasks
    from src.doctor import default_registry
    from src.doctor.models import DoctorContext, Severity
    from tests.test_agent_wait_queries import complete

    registered = await execute(commands, "wait_register", dict(
        kind="task", ref="source", idempotency_key="doctor", claim_epoch=1,
    ))
    assert registered["success"], registered
    wait_id = registered["wait"]["id"]
    await complete(env.db, at=NOW, status=terminal)
    if archived:
        async with env.db._engine.begin() as conn:
            source = (await conn.execute(select(tasks).where(tasks.c.id == "source"))).mappings().one()
            fields = {key: value for key, value in source.items() if key in archived_tasks.c}
            await conn.execute(insert(archived_tasks).values(**fields, archived_at=NOW + 1))
            await conn.execute(delete(tasks).where(tasks.c.id == "source"))
    check = default_registry().get("waits.pending_terminal_tasks")
    ctx = DoctorContext(config=commands.config, db=env.db)
    found = await check.run(ctx)
    assert found.severity == Severity.WARN
    assert not found.fixable and check.fix is None
    assert found.data["waits"][0]["wait_id"] == wait_id
    assert found.data["waits"][0]["target_task_id"] == "source"
    assert found.data["waits"][0]["target_status"] == terminal
    assert (await env.db.get_agent_wait(wait_id))["state"] == "active"
    await AgentWaitReconciler(commands).tick(now=NOW + 1)
    assert (await check.run(ctx)).severity == Severity.OK


@pytest.mark.parametrize("delay", [5, 100, 101])
async def test_doctor_reports_pending_timer_due_before_hard_deadline(
    commands, env, monkeypatch, delay
):
    from src.doctor import default_registry
    from src.doctor.models import DoctorContext, Severity

    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW)
    registered = await execute(commands, "wait_register", dict(
        kind="timer", due_at=NOW + 5, timeout=100, idempotency_key="doctor", claim_epoch=1,
    ))
    assert registered["success"], registered
    wait_id = registered["wait"]["id"]
    check = default_registry().get("waits.pending_timers")
    ctx = DoctorContext(config=commands.config, db=env.db)
    assert (await check.run(ctx)).severity == Severity.OK
    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW + delay)
    found = await check.run(ctx)
    assert found.severity == Severity.WARN
    assert found.data["waits"] == [dict(
        wait_id=wait_id, project_id="p", owner_kind="task", owner_id="owner", session_id="s",
        due_at=NOW + 5, deadline_at=NOW + 100, checked_at=0.0,
        state="active", resolved_at=None, result_message_id=None,
    )]
    assert not found.fixable and check.fix is None
    assert (await env.db.get_agent_wait(wait_id))["state"] == "active"
    await AgentWaitReconciler(commands).tick(now=NOW + delay)
    assert (await check.run(ctx)).severity == Severity.OK


@pytest.mark.parametrize("interval", [5, 40])
@pytest.mark.parametrize("consumed", ["delivered", "archived"])
async def test_doctor_reports_satisfied_timer_with_undelivered_result(
    commands, env, monkeypatch, interval, consumed
):
    from src.doctor import default_registry
    from src.doctor.models import DoctorContext, Severity

    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW)
    commands.config.messages.delivery_interval = interval
    grace = max(30, interval * 2)
    registered = await execute(commands, "wait_register", dict(
        kind="timer", due_at=NOW + 5, timeout=100, idempotency_key="undelivered", claim_epoch=1,
    ))
    assert registered["success"], registered
    wait_id = registered["wait"]["id"]
    await AgentWaitReconciler(commands).tick(now=NOW + 5)
    check = default_registry().get("waits.pending_timers")
    ctx = DoctorContext(config=commands.config, db=env.db)
    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW + 5 + grace - 1)
    assert (await check.run(ctx)).severity == Severity.OK
    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW + 5 + grace)
    found = await check.run(ctx)
    assert found.severity == Severity.WARN
    assert found.data["waits"][0]["state"] == "satisfied"
    result = await env.db.get_agent_wait(wait_id)
    assert result["state"] == "satisfied"
    message_id = result["result_message_id"]
    assert found.data["waits"][0]["result_message_id"] == message_id
    assert (await env.db.get_message(message_id)).delivered_at is None
    if consumed == "delivered":
        await env.db.mark_delivered(message_id, via="nudge")
    else:
        await env.db.archive_messages([message_id])
    assert (await check.run(ctx)).severity == Severity.OK


async def test_doctor_pending_timer_diagnostic_is_bounded_and_ignores_other_waits(
    commands, env, monkeypatch
):
    from sqlalchemy import insert
    from src.database.tables import agent_waits
    from src.doctor import default_registry
    from src.doctor.models import DoctorContext, Severity

    monkeypatch.setattr("src.commands.wait_commands.time.time", lambda: NOW)
    registered = await execute(commands, "wait_register", dict(
        kind="task", ref="source", idempotency_key="task", claim_epoch=1,
    ))
    wait = registered["wait"]
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(agent_waits), [
            dict(wait, id=f"timer-{i:02}", owner_kind="supervisor", kind="timer",
                 match={"due_at": NOW - 1}, idempotency_key=f"timer-{i}")
            for i in range(60)
        ])
    check = default_registry().get("waits.pending_timers")
    ctx = DoctorContext(config=commands.config, db=env.db)
    found = await check.run(ctx)
    assert found.severity == Severity.WARN
    assert len(found.data["waits"]) == 50 and found.data["truncated"]
    assert {w["wait_id"] for w in found.data["waits"]} == {f"timer-{i:02}" for i in range(50)}
    assert (await check.run(DoctorContext(config=commands.config))).severity == Severity.INFO


async def test_supplied_principal_cannot_replace_owner_instance(commands):
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="s",
        session_instance_token="old",
        project_id="p",
        provenance=("profile-not-found",),
    )
    with principal_context(principal):
        result = await commands.execute(
            "wait_register", dict(kind="task", ref="source", claim_epoch=1, idempotency_key="key")
        )
    assert result.get("error")


def test_four_contracts_are_granted_and_typed():
    from src.api.scope import AGENT_COMMAND_SET
    from src.profiles.parser import parse_profile
    from pathlib import Path

    names = {"wait_register", "wait_get", "wait_list", "wait_cancel"}
    assert names <= AGENT_COMMAND_SET
    for name in names:
        contract = CONTRACTS.require(name).contract
        assert contract.execution.capability == name
        assert contract.execution.retry_safe
    for template in ("worker-codex", "worker-claude"):
        profile = parse_profile(Path(f"src/profiles/defaults/{template}/profile.md").read_text())
        assert names <= set(profile.capabilities["aq_commands"])
    assert (
        CONTRACTS.require("wait_register").contract.execution.idempotency.key_field
        == "idempotency_key"
    )


def test_wait_cli_reads_claim_epoch_and_emits_versioned_json(monkeypatch):
    from src.cli.app import cli
    from src.cli import waits

    calls = []

    @asynccontextmanager
    async def client(*args):
        async def execute(name, params):
            calls.append((name, params))
            return {"success": True, "wait": {"id": "w", "state": "active"}}

        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(waits, "_get_client", client)
    monkeypatch.setattr(
        waits, "resolve_claim_epoch", lambda explicit: explicit if explicit is not None else 7
    )
    result = CliRunner().invoke(
        cli,
        ["wait", "register", "--kind", "task", "--ref", "t", "--idempotency-key", "k", "--json"],
    )
    assert result.exit_code == 0, result.output
    import json

    assert json.loads(result.output)["data"]["wait"]["id"] == "w"
    assert calls[0] == (
        "wait_register",
        {"kind": "task", "ref": "t", "idempotency_key": "k", "claim_epoch": 7},
    )
    shown = CliRunner().invoke(cli, ["wait", "show", "w", "--json"])
    assert shown.exit_code == 0
    assert calls[-1] == ("wait_get", {"wait_id": "w"})
    cancelled = CliRunner().invoke(cli, ["wait", "cancel", "w", "--claim-epoch", "8", "--json"])
    assert cancelled.exit_code == 0
    assert calls[-1] == ("wait_cancel", {"wait_id": "w", "claim_epoch": 8})


async def test_global_supervisor_subscription_does_not_block_and_pointer_needs_no_project(
    commands, env
):
    from src.models import SessionRecord

    await env.db.create_session(
        SessionRecord(
            id="global-row",
            project_id=None,
            profile_id="supervisor",
            harness="codex",
            provider="fake",
            name="n-supervisor--global",
            lifecycle="named",
            work_dir="/tmp/global",
            epoch="boot",
            instance_token="global-token",
            started_at=NOW,
            state="running",
        )
    )
    global_scope = dict(
        kind="session",
        session_id="global-row",
        project_id=None,
        session_instance_token="global-token",
        elevated=True,
    )
    registered = await commands.execute(
        "wait_register",
        {
            "kind": "task",
            "ref": "source",
            "project_id": "p",
            "idempotency_key": "subscription",
            "_scope": global_scope,
        },
    )
    assert registered["success"], registered
    row = registered["wait"]
    assert row["owner_kind"] == "supervisor" and row["owner_id"] == "supervisor-global"
    assert "Continue working" in registered["next_step"]
    shown = await commands.execute("wait_get", {"wait_id": row["id"], "_scope": global_scope})
    assert shown["success"], shown
    cancelled = await commands.execute(
        "wait_cancel", {"wait_id": row["id"], "_scope": global_scope}
    )
    assert cancelled["success"] and cancelled["wait"]["state"] == "cancelled"
    result = await env.db.get_message(cancelled["wait"]["result_message_id"])
    assert result.to_kind == "session" and result.to_id == "supervisor-global"
