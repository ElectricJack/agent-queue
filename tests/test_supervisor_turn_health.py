"""Turn health and recovery with exact transcripts and disposable PostgreSQL."""
# Imported fixtures are intentionally re-exported for pytest.
# ruff: noqa: F811
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import SupervisorConfig
from src.intelligence_classes import IntelligenceClass
from src.models import Message, Project, SessionRecord
from src.profiles.capabilities import CapabilityPolicy
from src.sessions.harness_parser import Harness
from src.sessions import supervisor_health as health
from tests.test_agent_terminals import handler, provider, start_supervisor  # noqa: F401


def session(**fields):
    values = dict(id="supervisor-id", project_id="p", profile_id="supervisor", harness="codex",
                  provider="fake", name="n-supervisor--p", lifecycle="named", work_dir="/tmp",
                  epoch="e", instance_token="token", started_at=1000, state="running",
                  session_key="exact-conversation")
    return SessionRecord(**(values | fields))


def request(**fields):
    return Message(**(dict(id="question", project_id="p", from_kind="session", from_id="worker",
                          to_kind="session", to_id="supervisor-p", body="Need a decision",
                          created_at=1100) | fields))


def record(at, kind, payload):
    return {"timestamp": at, "type": kind, "payload": payload}


def write_tail(tmp_path, records):
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in records))
    return path


@pytest.mark.parametrize("text", [
    "Selected model is at capacity. Please try a different model.",
    "Rate limit exceeded", "Authentication failed: 401", "Invalid API key", "Quota exceeded",
])
def test_codex_provider_failures_are_idle_turn_failures(tmp_path, text):
    path = write_tail(tmp_path, [record(1200, "event_msg", {"type": "error", "message": text}),
                                 record(1201, "event_msg", {"type": "task_complete"})])
    observation = health.observe_tail(path, session(), now=2000)
    assert observation["error"]["reason"] == text
    assert observation["idle"] and observation["last_completed_at"] is None


@pytest.mark.parametrize("text", ["rate_limit_error", "authentication_error", "overloaded_error"])
def test_claude_api_error_is_not_success(tmp_path, text):
    path = write_tail(tmp_path, [{"type": "assistant", "timestamp": 1200,
                                 "isApiErrorMessage": True,
                                 "message": {"stop_reason": "end_turn", "content": text}}])
    observation = health.observe_tail(path, session(harness="claude"), now=2000)
    assert observation["error"] and observation["last_completed_at"] is None


def test_only_a_new_successful_turn_clears_error(tmp_path):
    path = write_tail(tmp_path, [
        record(1200, "event_msg", {"type": "error", "message": "Model at capacity"}),
        record(1201, "event_msg", {"type": "token_count", "info": {}}),
    ])
    assert health.observe_tail(path, session(), now=2000)["error"]
    with path.open("a") as stream:
        for event in (record(1300, "event_msg", {"type": "task_started"}),
                      record(1400, "event_msg", {"type": "task_complete"})):
            stream.write(json.dumps(event) + "\n")
    observation = health.observe_tail(path, session(), now=2000)
    assert observation["error"] is None and observation["last_completed_at"] == 1400


def test_prelaunch_errors_and_partial_records_are_unknown(tmp_path):
    path = write_tail(tmp_path, [record(900, "event_msg", {"type": "error", "message": "401"})])
    assert health.observe_tail(path, session(), now=2000)["known"] is False
    path.write_text(json.dumps(record(1200, "event_msg", {"type": "task_complete"})))
    assert health.observe_tail(path, session(), now=2000)["known"] is False


def test_pane_activity_does_not_hide_unanswered_work():
    row = session(last_activity=2999)
    result = health.assess(row, {"known": True}, [request()], SupervisorConfig(), now=3000)
    assert result["unhealthy"] and result["reason"] == "turn_timeout"
    assert result["pending_count"] == 1


def test_success_resets_budget_but_does_not_answer_worker_question():
    row = session(supervisor_health={"last_completed_at": 1200, "attempts": 4, "next_retry_at": 9000})
    result = health.assess(row, {"known": True, "last_completed_at": 2500}, [request()],
                           SupervisorConfig(), now=2600)
    assert result["attempts"] == 0 and result["next_retry_at"] == 0
    assert result["pending_count"] == 1 and not result["unhealthy"]


@pytest.mark.parametrize("idle,known,age,tokens,rotate", [
    (True, True, 86401, None, True), (True, True, 1, 160000, True),
    (False, True, 86401, 160000, False), (True, False, 86401, None, False),
    (True, True, 1, 10000, False),
])
def test_rotation_requires_idle_turn_evidence(idle, known, age, tokens, rotate):
    result = health.assess(session(), {"known": known, "idle": idle, "input_tokens": tokens},
                           [], SupervisorConfig(), now=1000 + age)
    assert result["rotate"] is rotate


@pytest.mark.parametrize("key", ["turn_timeout_seconds", "question_timeout_seconds",
    "retry_backoff_seconds", "max_turn_retries", "max_session_age_seconds", "context_ceiling_tokens"])
def test_health_config_requires_positive_integers(key):
    config = SupervisorConfig()
    setattr(config, key, 0)
    assert config.validate()[0].field == key


async def test_retry_backoff_survives_new_reconciler_and_fresh_restart(handler, monkeypatch):
    old = await start_supervisor(handler)
    row = await handler.db.get_session(old["session_id"])
    lens = handler.orchestrator.session_lens
    lens.nudge = AsyncMock(return_value=True)
    error = {"known": True, "idle": True, "error": {"at": row.started_at + 1, "reason": "401"}}
    monkeypatch.setattr(health, "observe", AsyncMock(return_value=error))
    now = row.started_at + 2
    config = handler.config
    reconciler = SimpleNamespace(db=handler.db, config=config, transcript_base_dir=None,
                                 starter=lens, _named_address=lambda _: "supervisor-global")
    await health.reconcile(reconciler, row, now=now)
    assert lens.nudge.await_count == 1
    row = await handler.db.get_session(row.id)
    assert row.supervisor_health["next_retry_at"] == now + 60
    # A newly created reconciler cannot reset the persisted cooldown.
    reconciler = SimpleNamespace(**vars(reconciler))
    await health.reconcile(reconciler, row, now=now + 59)
    assert lens.nudge.await_count == 1
    await health.reconcile(reconciler, await handler.db.get_session(row.id), now=now + 60)
    assert lens.nudge.await_count == 2
    token = provider(handler).starts[-1].env["AQ_API_TOKEN"]
    await health.reconcile(reconciler, await handler.db.get_session(row.id), now=now + 180)
    successor = await handler.db.get_session_by_name(row.name)
    assert successor.id != row.id and successor.supervisor_health["attempts"] == 3
    assert await handler.orchestrator.token_store.validate(token) is None
    assert (await handler.db.get_session(row.id)).end_reason == "provider_error"
    notices = await handler.db.list_messages(include_archived=True)
    assert any(msg.body_kind == "supervisor_handoff" and row.id in msg.body for msg in notices)
    assert any(msg.to_kind == "user" and msg.to_id == "dashboard" for msg in notices)


async def test_consumed_worker_request_escalates_once_and_survives_replacement(handler):
    await handler.db.create_project(Project(id="p", name="P", supervisor_enabled=True))
    row = session()
    await handler.db.create_session(row)
    msg = await handler.db.create_message(project_id="p", from_kind="session", from_id="worker",
        to_kind="session", to_id=row.id, body="Need decision", thread_id="owned-worker-thread")
    await handler.db.mark_delivered(msg.id, via="inbox")
    await handler.db.archive_messages([msg.id])
    assert [item.id for item in await handler.db.supervisor_requests(row)] == [msg.id]
    assert await handler.db.escalate_supervisor_request(msg, row, now=msg.created_at + 901)
    assert not await handler.db.escalate_supervisor_request(msg, row, now=msg.created_at + 1000)
    alerts = await handler.db.list_messages(to_id="supervisor-global")
    assert len(alerts) == 1 and alerts[0].thread_id == msg.thread_id
    assert alerts[0].reply_to_id is None  # cannot clear the worker's message wait
    await handler.db.update_session(row.id, state="stopped", desired_state="stopped")
    successor = replace(row, id="successor", instance_token="new-token", started_at=2000)
    await handler.db.create_session(successor)
    assert [item.id for item in await handler.db.supervisor_requests(successor)] == [msg.id]
    await handler.db.create_message(project_id="p", from_kind="session", from_id="supervisor-global",
        to_kind="session", to_id="worker", body="Decision", reply_to_id=msg.id)
    assert not await handler.db.supervisor_requests(successor)


async def test_answered_and_system_chatter_cannot_starve_oldest_requests(handler):
    await handler.db.create_project(Project(id="p", name="P", supervisor_enabled=True))
    row = session()
    await handler.db.create_session(row)
    msg = await handler.db.create_message(project_id="p", from_kind="session", from_id="worker",
        to_kind="session", to_id="supervisor-p", body="oldest question")
    await handler.db.create_message(project_id="p", from_kind="session", from_id="supervisor-p",
        to_kind="session", to_id="worker", body="answer", reply_to_id=msg.id)
    assert not await handler.db.supervisor_requests(row)


async def test_project_recovery_restart_sends_global_notice_and_handoff(handler, monkeypatch):
    await handler.db.create_project(Project(id="p", name="P", supervisor_enabled=True))
    lens = handler.orchestrator.session_lens
    assert await lens.ensure_started(kind="session", target_id="supervisor-p", project_id="p")
    row = await handler.db.get_session_by_name("n-supervisor--p")
    msg = await handler.db.create_message(project_id="p", from_kind="session", from_id="worker",
        to_kind="session", to_id=row.id, body="Wait for decision")
    now = msg.created_at + 901
    monkeypatch.setattr(health, "observe", AsyncMock(return_value={"known": True, "idle": True}))
    await handler.db.update_session(row.id, supervisor_health={"attempts": 2})
    reconciler = SimpleNamespace(db=handler.db, config=handler.config, transcript_base_dir=None,
        starter=lens, _named_address=lambda _: "supervisor-p")
    await health.reconcile(reconciler, await handler.db.get_session(row.id), now=now)
    successor = await handler.db.get_session_by_name(row.name)
    assert successor.id != row.id
    alerts = await handler.db.list_messages(to_id="supervisor-global")
    assert len(alerts) == 2  # overdue question and fresh-session notice
    handoffs = await handler.db.list_messages(to_id="supervisor-p")
    assert any(msg.id in item.body and row.session_key in item.body for item in handoffs)
    assert msg.id in [item.id for item in await handler.db.supervisor_requests(successor)]


async def test_stale_instance_cannot_recover_or_escalate(handler, monkeypatch):
    await start_supervisor(handler)
    row = await handler.db.get_session_by_name("n-supervisor--global")
    monkeypatch.setattr(health, "observe", AsyncMock(return_value={"known": True,
        "error": {"reason": "401", "at": row.started_at + 1}}))
    lens = AsyncMock()
    reconciler = SimpleNamespace(db=handler.db, config=handler.config, transcript_base_dir=None,
        starter=lens, _named_address=lambda _: "supervisor-global")
    await handler.db.update_session(row.id, instance_token="replacement-token")
    await health.reconcile(reconciler, row, now=row.started_at + 10)
    lens.nudge.assert_not_awaited()
    lens.restart_supervisor.assert_not_awaited()


async def test_class_constrained_failover_skips_unavailable_and_missing_harnesses(handler, monkeypatch):
    await start_supervisor(handler)
    lens = handler.orchestrator.session_lens
    row = await handler.db.get_session_by_name("n-supervisor--global")
    registry = handler.orchestrator.harness_registry
    registry.upsert(Harness(id="codex", command="codex", provider="openai", model_flag="-m"))
    registry.upsert(Harness(id="gemini", command="gemini", provider="google", model_flag="-m"))
    handler.orchestrator.session_spec_builder._intelligence_classes["test-class"] = IntelligenceClass(
        id="test-class", name="Test", description="", mapping={
            "codex": {"model": "codex-model"}, "openai": {"model": "api-model"},
            "google": {"model": "gemini-model"}, "anthropic": {"model": "claude-model"}})
    row = replace(row, intelligence_class="test-class")
    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")
    lens._availability = SimpleNamespace(suppresses=lambda provider: provider == "codex")
    assert await lens.supervisor_recovery_route(row) == "gemini"
    monkeypatch.setattr("shutil.which", lambda command: None)
    assert await lens.supervisor_recovery_route(row) is None


async def test_explicit_model_pin_is_not_overridden_by_failover(handler, monkeypatch):
    await start_supervisor(handler)
    row = await handler.db.get_session_by_name("n-supervisor--global")
    await handler.db.update_agent("supervisor-global", model="pinned")
    assert await handler.orchestrator.session_lens.supervisor_recovery_route(row) is None


@pytest.mark.parametrize("caller_project,target,allowed", [
    ("p", "p", True), ("p", "other", False), ("p", None, False), (None, "p", True),
])
async def test_live_supervisor_restart_control_is_project_scoped(handler, caller_project, target, allowed):
    for pid in ("p", "other"):
        await handler.db.create_project(Project(id=pid, name=pid, supervisor_enabled=True))
    lens = handler.orchestrator.session_lens
    for pid in ("p", "other", None):
        address = f"supervisor-{pid}" if pid else "supervisor-global"
        assert await lens.ensure_started(kind="session", target_id=address, project_id=pid)
    caller = await handler.db.get_session_by_name(f"n-supervisor--{caller_project or 'global'}")
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy(aq_commands=frozenset({"supervisor_restart"})),
        session_id=caller.id, session_instance_token=caller.instance_token,
        profile_id="supervisor", project_id=caller_project, elevated=True)
    with principal_context(principal):
        result = await handler.execute("supervisor_restart", {
            "name": f"supervisor-{target}" if target else "supervisor-global"})
    assert result["success"] is allowed, result


async def test_idle_context_rotation_restarts_fresh_with_handoff(handler, monkeypatch):
    await start_supervisor(handler)
    row = await handler.db.get_session_by_name("n-supervisor--global")
    monkeypatch.setattr(health, "observe", AsyncMock(return_value={"known": True, "idle": True,
        "input_tokens": handler.config.supervisor.context_ceiling_tokens}))
    reconciler = SimpleNamespace(db=handler.db, config=handler.config, transcript_base_dir=None,
        starter=handler.orchestrator.session_lens, _named_address=lambda _: "supervisor-global")
    await health.reconcile(reconciler, row, now=row.started_at + 1)
    successor = await handler.db.get_session_by_name(row.name)
    assert successor.id != row.id and successor.session_key != row.session_key
    assert "--resume" not in provider(handler).starts[-1].command
    assert (await handler.db.get_session(row.id)).end_reason == "session_rotation"
    handoffs = await handler.db.list_messages(to_id="supervisor-global")
    assert any(row.id in msg.body and "session_rotation" in msg.body for msg in handoffs)


async def test_failed_recovery_launch_preserves_health_and_cooldown(handler, monkeypatch):
    from src.messages.session_lens import SupervisorRestartError

    await start_supervisor(handler)
    lens = handler.orchestrator.session_lens
    row = await handler.db.get_session_by_name("n-supervisor--global")
    saved = {"attempts": 3, "next_retry_at": row.started_at + 1800,
             "recovery_harness": "claude", "reason": "provider_error"}
    await handler.db.update_session(row.id, supervisor_health=saved)
    provider(handler).script_start_error(row.name, RuntimeError("launch failed"))
    with pytest.raises(SupervisorRestartError, match="relaunch failed"):
        await lens.restart_supervisor(target_id="supervisor-global", project_id=None,
            recovery_harness="claude", recovery_health=saved)
    retired = await handler.db.get_session(row.id)
    assert retired.state == "stopped" and retired.desired_state == "running"
    assert retired.supervisor_health == saved
    count = len(provider(handler).starts)
    assert not await lens.ensure_started(kind="session", target_id="supervisor-global", project_id=None)
    assert len(provider(handler).starts) == count
    monkeypatch.setattr("src.messages.session_lens.time.time", lambda: saved["next_retry_at"] + 1)
    assert await lens.ensure_started(kind="session", target_id="supervisor-global", project_id=None)
    successor = await handler.db.get_session_by_name(row.name)
    assert successor.id != row.id and successor.supervisor_health == saved
    assert "--resume" not in provider(handler).starts[-1].command


async def test_uncertain_recovery_stop_keeps_retry_intent_without_duplicate_launch(handler, monkeypatch):
    from src.messages.session_lens import SupervisorRestartError

    await start_supervisor(handler)
    row = await handler.db.get_session_by_name("n-supervisor--global")
    monkeypatch.setattr(provider(handler), "confirm_stopped", AsyncMock(return_value=False))
    with pytest.raises(SupervisorRestartError, match="confirm"):
        await handler.orchestrator.session_lens.restart_supervisor(
            target_id="supervisor-global", project_id=None, recovery_health={"attempts": 3})
    assert len(provider(handler).starts) == 1
    assert (await handler.db.get_session(row.id)).desired_state == "running"


async def test_automatic_failover_launch_resolves_the_authorized_class_model(handler, monkeypatch):
    await handler.db.update_profile("supervisor", default_class="recovery")
    builder = handler.orchestrator.session_spec_builder
    builder._intelligence_classes["recovery"] = IntelligenceClass(
        id="recovery", name="Recovery", description="", mapping={
            "anthropic": {"model": "claude-authorized"}, "codex": {"model": "codex-authorized"}})
    registry = handler.orchestrator.harness_registry
    registry.upsert(Harness(id="codex", command="codex", provider="openai", model_flag="-m"))
    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")
    await start_supervisor(handler)
    row = await handler.db.get_session_by_name("n-supervisor--global")
    await handler.db.update_session(row.id, supervisor_health={"attempts": 2})
    monkeypatch.setattr(health, "observe", AsyncMock(return_value={"known": True, "idle": True,
        "error": {"reason": "rate limit", "at": row.started_at + 1}}))
    reconciler = SimpleNamespace(db=handler.db, config=handler.config, transcript_base_dir=None,
        starter=handler.orchestrator.session_lens, _named_address=lambda _: "supervisor-global")
    await health.reconcile(reconciler, await handler.db.get_session(row.id), now=row.started_at + 2)
    successor = await handler.db.get_session_by_name(row.name)
    assert successor.harness == "codex" and successor.model == "codex-authorized"
    assert successor.intelligence_class == "recovery"
    assert "codex-authorized" in provider(handler).starts[-1].command
    assert successor.supervisor_health["attempts"] == 3


async def test_restart_rejects_stale_supervisor_instance_token(handler):
    await start_supervisor(handler)
    row = await handler.db.get_session_by_name("n-supervisor--global")
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=CapabilityPolicy(),
        session_id=row.id, session_instance_token="stale", profile_id="supervisor", elevated=True)
    with principal_context(principal):
        result = await handler._cmd_supervisor_restart({})
    assert not result["success"] and "stale" in result["error"]
    assert len(provider(handler).starts) == 1


async def test_named_reconciler_runs_turn_health_before_idle_handling(handler, monkeypatch):
    from src.sessions.reconciler import SessionReconciler

    await start_supervisor(handler)
    row = await handler.db.get_session_by_name("n-supervisor--global")
    monkeypatch.setattr(health, "observe", AsyncMock(return_value={"known": True,
        "error": {"reason": "at capacity", "at": row.started_at + 1}}))
    rec = SessionReconciler(handler.db, handler.config, handler.orchestrator.session_providers,
        starter=handler.orchestrator.session_lens)
    await rec._step_named([row], row.started_at + 2)
    saved = await handler.db.get_session(row.id)
    assert saved.supervisor_health["unhealthy"]
    assert saved.supervisor_health["attempts"] == 1
    assert len(provider(handler).sent_nudges) == 1


@pytest.mark.parametrize("mode,policy", [("observe", "same_class"), ("off", "same_class"),
                                        ("enforce", "hold")])
async def test_failover_respects_operator_policy(handler, monkeypatch, mode, policy):
    await start_supervisor(handler)
    row = await handler.db.get_session_by_name("n-supervisor--global")
    row = replace(row, intelligence_class="test-class")
    handler.config.provider_failover.mode = mode
    handler.config.provider_failover.classes["test-class"] = policy
    handler.orchestrator.harness_registry.upsert(Harness(id="codex", command="codex"))
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    assert await handler.orchestrator.session_lens.supervisor_recovery_route(row) is None


@pytest.mark.migration
async def test_supervisor_health_migration_replays_preserves_rows_and_reverses():
    from importlib import import_module

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from src.database.engine import create_postgres_engine
    from tests.pg_dsn import create_scratch_database

    migration = import_module("migrations.versions.a00000000114_supervisor_health")
    engine = create_postgres_engine(await create_scratch_database("supervisorhealthmigration"))
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE sessions (id TEXT PRIMARY KEY)"))
            await conn.execute(sa.text("INSERT INTO sessions VALUES ('preserved')"))

            def check(sync_conn):
                with Operations.context(MigrationContext.configure(sync_conn)):
                    migration.upgrade()
                    migration.upgrade()
                    column = next(c for c in sa.inspect(sync_conn).get_columns("sessions")
                                  if c["name"] == "supervisor_health")
                    assert isinstance(column["type"], sa.dialects.postgresql.JSONB)
                    assert column["nullable"]
                    sync_conn.execute(sa.text(
                        "UPDATE sessions SET supervisor_health = '{\"attempts\": 3}'::jsonb"))
                    migration.upgrade()
                    assert sync_conn.execute(sa.text(
                        "SELECT supervisor_health->>'attempts' FROM sessions"
                    )).scalar_one() == "3"
                    migration.downgrade()
                    migration.downgrade()
                    assert sync_conn.execute(sa.text("SELECT id FROM sessions")).scalar_one() == "preserved"

            await conn.run_sync(check)
    finally:
        await engine.dispose()


async def test_completed_turn_discharges_consumed_worker_status_reports(handler):
    await handler.db.create_project(Project(id="p", name="P", supervisor_enabled=True))
    row = session()
    await handler.db.create_session(row)
    msg = await handler.db.create_message(project_id="p", from_kind="session", from_id="worker",
        to_kind="session", to_id="supervisor-p", body="Finished the check")
    await handler.db.mark_delivered(msg.id, via="inbox")
    await handler.db.archive_messages([msg.id])
    assert [item.id for item in await handler.db.supervisor_requests(row)] == [msg.id]
    row = replace(row, supervisor_health={"last_completed_at": msg.created_at + 1})
    assert not await handler.db.supervisor_requests(row)


async def test_verified_worker_reply_wait_survives_unrelated_successful_turn(handler):
    from sqlalchemy import insert

    from src.database.tables import wait_records

    await handler.db.create_project(Project(id="p", name="P", supervisor_enabled=True))
    row = session()
    await handler.db.create_session(row)
    msg = await handler.db.create_message(project_id="p", from_kind="session", from_id="worker",
        to_kind="session", to_id="supervisor-p", body="Need approval", thread_id="decision-thread")
    await handler.db.mark_delivered(msg.id, via="inbox")
    await handler.db.archive_messages([msg.id])
    async with handler.db._engine.begin() as conn:
        await conn.execute(insert(wait_records).values(
            id="worker-wait", project_id="p", owner_kind="task", owner_id="worker-task",
            session_id="worker", origin="agent", condition_kind="message",
            condition={"kind": "message", "thread_id": msg.thread_id, "after_seq": 0},
            source_key=f"message:{msg.thread_id}", source_generation=0, source_watermark_seq=0,
            verification_state="verified", evidence_ref="test", verified_at=msg.created_at,
            created_at=msg.created_at, request_hash="test", outcome="pending"))
    row = replace(row, supervisor_health={"last_completed_at": msg.created_at + 1})
    assert [item.id for item in await handler.db.supervisor_requests(row)] == [msg.id]
    assert await handler.db.escalate_supervisor_request(msg, row, now=msg.created_at + 901)
    alerts = await handler.db.list_messages(to_id="supervisor-global")
    assert len(alerts) == 1 and alerts[0].reply_to_id is None


async def test_consumed_inbound_message_without_completed_turn_remains_pending(handler):
    await handler.db.create_project(Project(id="p", name="P", supervisor_enabled=True))
    row = session()
    await handler.db.create_session(row)
    msg = await handler.db.create_message(project_id="p", from_kind="user", from_id="cli",
        to_kind="session", to_id="supervisor-p", body="Investigate the blocked queue")
    await handler.db.mark_delivered(msg.id, via="inbox")
    await handler.db.archive_messages([msg.id])
    requests = await handler.db.supervisor_requests(row)
    assert [item.id for item in requests] == [msg.id]
    assessed = health.assess(row, {"known": False}, requests, SupervisorConfig(), now=msg.created_at + 901)
    assert assessed["unhealthy"]
