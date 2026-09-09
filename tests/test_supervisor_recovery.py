"""Durable incident delivery and bounded, claim-fenced supervisor decisions."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.api.auth import RequestScope
from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.database.tables import messages
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.asyncio
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture
async def env(tmp_path, request):
    db = Database(lease_dsn("recovery.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_project(Project(id="other", name="Other"))
    await db.create_profile(AgentProfile(id="worker", name="Worker"))
    await db.create_task(
        Task(
            id="t",
            project_id="p",
            title="Work",
            status=TaskStatus.BLOCKED,
            profile_id="worker",
            intelligence_class="deep-high",
            branch_name="aq/keep",
            description="Keep requirements",
            created_at=100,
            updated_at=100,
        )
    )
    await db.update_task("t", created_at=100)
    config = AppConfig(database=DatabaseConfig(url=lease_dsn("recovery.db")), data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"))
    orch = Orchestrator(config)
    orch.db = db
    orch.session_providers = SimpleNamespace(
        create=lambda *_: SimpleNamespace(confirm_stopped=AsyncMock(return_value=True))
    )
    yield SimpleNamespace(db=db, orch=orch, config=config, handler=CommandHandler(orch, config))
    await db.close()


async def incident(env, sid="s", started=200, reason="stuck_timeout"):
    await env.db.create_session(
        SessionRecord(
            id=sid,
            task_id="t",
            project_id="p",
            profile_id="worker",
            harness="fake",
            provider="fake",
            name=sid,
            lifecycle="task",
            state="running",
            desired_state="running",
            epoch="test",
            instance_token=sid,
            work_dir="/never-used",
            started_at=started,
            last_activity=started + 7199,
        )
    )
    await env.db.update_session(
        sid, state="stopped", desired_state="stopped", ended_at=started + 7200, end_reason=reason
    )
    await env.db.set_task_meta("t", "needs_attention", reason)
    await env.db.queue_task_recovery_notifications()
    return await env.db.get_task_meta("t", "supervisor_recovery_incident")


async def decide(env, current, decision="retry", **extra):
    return await env.handler.execute(
        "task_recover",
        {
            "task_id": "t",
            "incident_id": current["id"],
            "decision": decision,
            "reason": "Transcript shows progress; preserve work and continue.",
            **extra,
        },
    )


async def apply_human_recovery_reply(env, current, decision):
    created = await env.handler.execute(
        "escalation_create",
        {
            "project_id": "p",
            "task_id": "t",
            "source_kind": "task_recovery",
            "source_identity": current["id"],
            "incident_key": "task-recovery:" + current["id"],
            "summary": "Worker attempt is blocked",
            "investigation": "Supervisor inspected the stopped attempt and recovery guards",
            "decision_requested": "Retry the task or keep it blocked?",
            "choices": ["retry", "hold"],
            "severity": "high",
        },
    )
    replay = await env.handler.execute(
        "escalation_create",
        {
            "project_id": "p",
            "task_id": "t",
            "source_kind": "task_recovery",
            "source_identity": current["id"],
            "incident_key": "task-recovery:" + current["id"],
            "summary": "Worker attempt is blocked",
            "investigation": "Supervisor inspected the stopped attempt and recovery guards",
            "decision_requested": "Retry the task or keep it blocked?",
            "choices": ["retry", "hold"],
            "severity": "high",
        },
    )
    assert created["created"] is True
    assert replay["created"] is False
    escalation = created["escalation"]
    accepted = await env.handler.execute(
        "escalation_reply",
        {
            "escalation_id": escalation["id"],
            "text": f"Decision: {decision}",
            "external_message_id": "human-recovery-" + decision,
        },
    )
    await env.db.create_session(
        SessionRecord(
            id="supervisor-" + decision,
            project_id="p",
            profile_id="supervisor",
            harness="fake",
            provider="fake",
            name="n-supervisor--p",
            lifecycle="named",
            state="running",
            desired_state="running",
            epoch="test",
            instance_token="supervisor-token-" + decision,
            work_dir="/never-used",
            started_at=300,
        )
    )
    return await env.handler.execute(
        "escalation_apply_reply",
        {
            "escalation_id": escalation["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "apply-human-recovery-" + decision,
            "action_kind": "task_recover",
            "target_id": "t",
            "decision": decision,
            "_scope": {
                "kind": "session",
                "session_id": "supervisor-" + decision,
                "session_instance_token": "supervisor-token-" + decision,
                "project_id": "p",
                "elevated": True,
            },
        },
    )


async def test_notification_survives_restart_without_duplicates_and_has_diagnostics(env):
    current = await incident(env)
    await asyncio.gather(
        env.db.queue_task_recovery_notifications(), env.db.queue_task_recovery_notifications()
    )
    async with env.db._engine.connect() as conn:
        queued = (await conn.execute(select(messages))).mappings().all()
    assert len(queued) == 1
    assert queued[0]["to_id"] == "supervisor-p"
    assert queued[0]["project_id"] == "p"
    assert queued[0]["from_kind"] == "system"
    assert queued[0]["archive_after_inject"] == 1
    assert current["id"] in queued[0]["body"]
    assert '"idle_seconds": 1' in queued[0]["body"]
    assert "aq task recover" in queued[0]["body"]
    assert "task-recovery:<incident-id>" in queued[0]["body"]
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED


async def test_stale_pending_incident_is_superseded_when_task_resumes(env):
    current = await incident(env)
    message_id = "msg-" + current["id"]

    await env.db.transition_task("t", TaskStatus.READY, force=True, context="restart")
    await env.db.queue_task_recovery_notifications()

    stale = await env.db.get_task_meta("t", "supervisor_recovery_incident")
    assert stale["decision"] == "superseded"
    assert (await env.db.get_message(message_id)).archived_at is not None

    await env.db.transition_task("t", TaskStatus.BLOCKED, force=True)
    replacement = await incident(env, sid="replacement", started=9000)
    assert replacement["id"] != current["id"]
    assert (await env.db.get_message("msg-" + replacement["id"])).archived_at is None


async def test_retry_preserves_work_and_routing_records_comment_and_consumes_budget_once(env):
    current = await incident(env)
    results = await asyncio.gather(decide(env, current), decide(env, current))
    assert sum("error" not in r for r in results) == 1, results
    task = await env.db.get_task("t")
    assert task.status == TaskStatus.READY
    assert task.retry_count == 1
    assert task.profile_id == "worker" and task.intelligence_class == "deep-high"
    assert task.branch_name == "aq/keep" and task.description == "Keep requirements"
    assert await env.db.get_task_meta("t", "needs_attention") is None
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") == 1
    comments = await env.handler.execute("task_comments", {"task_id": "t"})
    assert len(comments["comments"]) == 1
    assert "Transcript shows progress" in comments["comments"][0]["body"]


@pytest.mark.parametrize(
    "guard",
    [
        "pause",
        "project_pause",
        "gate",
        "hold_label",
        "budget",
        "live",
        "routing",
        "new_attempt",
        "retry_budget",
        "approval_reason",
    ],
)
async def test_retry_rejects_protected_or_changed_state(env, guard):
    current = await incident(env)
    if guard == "pause":
        await env.db.pause_task("t")
    elif guard == "project_pause":
        await env.db.update_project("p", status="PAUSED")
    elif guard == "gate":
        await env.db.create_gate(
            project_id="p", gate_type="human", title="Review", waiter_task_ids=["t"]
        )
    elif guard == "hold_label":
        await env.db.add_task_label("t", "hold:human")
    elif guard == "budget":
        await env.db.update_project("p", budget_limit=1, total_tokens_used=1)
    elif guard == "live":
        await env.db.create_session(
            SessionRecord(
                id="live",
                task_id="t",
                project_id="p",
                profile_id="worker",
                harness="fake",
                provider="fake",
                name="live",
                lifecycle="task",
                state="running",
                desired_state="running",
                epoch="test",
                instance_token="live",
                work_dir="/never-used",
                started_at=9000,
            )
        )
    elif guard == "routing":
        await env.db.update_task("t", intelligence_class="fast-low")
    elif guard == "new_attempt":
        await incident(env, sid="new", started=9000)
    elif guard == "retry_budget":
        await env.db.update_task("t", retry_count=3)
    else:
        await env.db.set_task_meta("t", "needs_attention", "human_approval_required")
    before = await env.db.get_task("t")
    result = await decide(env, current)
    assert "error" in result, result
    after = await env.db.get_task("t")
    assert after.status == before.status and after.retry_count == before.retry_count
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None


async def test_hold_decision_is_durable_and_does_not_restart(env):
    current = await incident(env)
    assert "error" not in await decide(env, current, "hold")
    await env.db.queue_task_recovery_notifications()
    assert "error" in await decide(env, current)
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] == "hold"


@pytest.mark.parametrize("decision,expected_status", [("retry", "READY"), ("hold", "BLOCKED")])
async def test_human_evidence_drives_guarded_recovery_or_keep_blocked(env, decision, expected_status):
    current = await incident(env)
    result = await apply_human_recovery_reply(env, current, decision)
    assert result["success"] is True
    assert result["escalation"]["state"] == "resolved"
    assert result["action_result"]["status"] == expected_status
    assert (await env.db.get_task("t")).status.value == expected_status


async def test_integration_owned_recovery_keeps_operation_authority_and_budget(env, monkeypatch):
    current = await incident(env)
    monkeypatch.setattr(
        env.db,
        "get_active_integration_repair_for_task",
        AsyncMock(return_value={"id": "integration-operation"}),
    )
    result = await decide(env, current, decision="hold")
    assert "integration operation integration-operation" in result["error"]
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["decision"] is None
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None


async def test_two_recoveries_maximum_even_if_manual_retry_count_reset(env):
    for n in range(3):
        await env.db.transition_task("t", TaskStatus.BLOCKED, force=True, retry_count=0)
        current = await incident(env, sid=f"s{n}", started=200 + n * 8000)
        result = await decide(env, current)
        assert ("error" in result) == (n == 2), result
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") == 2


async def test_workers_and_foreign_project_supervisors_cannot_recover(env):
    current = await incident(env)
    for scope in [
        RequestScope(kind="session", session_id="x", project_id="p"),
        RequestScope(kind="session", session_id="x", project_id="other", elevated=True),
    ]:
        result = await decide(env, current, _scope=asdict(scope))
        assert "scope" in result.get("error", "").lower(), result


async def test_recovery_metadata_cannot_be_reset_by_task_set(env):
    result = await env.handler.execute(
        "task_set", {"task_id": "t", "meta": {"supervisor_recovery_attempts": 0}}
    )
    assert "reserved" in result.get("error", "").lower()


async def test_ordinary_dependency_blocks_are_not_incidents(env):
    await env.db.queue_task_recovery_notifications()
    async with env.db._engine.connect() as conn:
        assert not (await conn.execute(select(messages))).all()


async def test_orchestrator_enqueues_before_delivery_and_respects_disabled_messaging(env):
    env.orch.message_delivery = SimpleNamespace(
        run_delivery_pass=AsyncMock(), check_reply_timeouts=AsyncMock()
    )
    env.db.queue_task_recovery_notifications = AsyncMock()
    env.config.messages.enabled = False
    await env.orch._deliver_messages()
    env.db.queue_task_recovery_notifications.assert_not_awaited()
    env.config.messages.enabled = True
    await env.orch._deliver_messages()
    env.db.queue_task_recovery_notifications.assert_awaited_once()


async def test_manual_stop_with_stale_attention_metadata_cannot_retry(env):
    current = await incident(env)
    from sqlalchemy import update
    from src.database.tables import task_session_attempts

    async with env.db._engine.begin() as conn:
        await conn.execute(update(task_session_attempts).values(end_reason="manual_stop"))
    assert "error" in await decide(env, current)


async def test_internal_recovery_nudge_does_not_request_a_user_reply():
    from src.messages.delivery import _render_nudge
    from src.models import Message

    notice = Message(
        id="internal",
        project_id=None,
        from_kind="system",
        from_id="task-recovery",
        to_kind="session",
        to_id="supervisor-global",
        body="inspect and decide",
        body_kind="task_recovery",
    )
    assert "aq reply" not in _render_nudge([notice])
    user = Message(
        id="user-message",
        project_id=None,
        from_kind="user",
        from_id="user",
        to_kind="session",
        to_id="supervisor-global",
        body="question",
    )
    mixed = _render_nudge([notice, user])
    assert "Reply with `aq reply user-message" in mixed
    assert "Reply with `aq reply internal" not in mixed


@pytest.mark.parametrize("still_running", [True, "unknown"])
async def test_stopped_record_requires_confirmed_process_exit(env, still_running):
    current = await incident(env)
    probe = (
        AsyncMock(return_value=False)
        if still_running is True
        else AsyncMock(side_effect=RuntimeError("unavailable"))
    )
    env.orch.session_providers = SimpleNamespace(
        create=lambda *_: SimpleNamespace(confirm_stopped=probe)
    )
    result = await decide(env, current)
    assert "error" in result
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED
    assert await env.db.get_task_meta("t", "supervisor_recovery_attempts") is None


async def test_delivered_incident_rearmed_after_supervisor_exit_without_duplicate_row(env):
    from sqlalchemy import update

    await env.db.create_session(
        SessionRecord(
            id="supervisor",
            project_id="p",
            profile_id="worker",
            harness="fake",
            provider="fake",
            name="n-supervisor--p",
            lifecycle="named",
            state="running",
            desired_state="running",
            epoch="test",
            instance_token="supervisor",
            work_dir="/never-used",
            started_at=100,
        )
    )
    current = await incident(env)
    message_id = "msg-" + current["id"]
    await env.db.mark_delivered(message_id)
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(messages)
            .where(messages.c.id == message_id)
            .values(delivered_at=300, archived_at=300)
        )
    await env.db.queue_task_recovery_notifications()
    assert (await env.db.get_message(message_id)).delivered_at == 300
    await env.db.update_session("supervisor", state="stopped", desired_state="stopped")
    await env.db.queue_task_recovery_notifications()
    assert (await env.db.get_message(message_id)).delivered_at is None
    assert (await env.db.get_message(message_id)).archived_at is None
    async with env.db._engine.connect() as conn:
        assert len((await conn.execute(select(messages))).all()) == 1
    assert (await env.db.get_task_meta("t", "supervisor_recovery_incident"))["redeliveries"] == 1


async def test_supervisor_unavailable_watchdog_is_bounded_deduplicated_and_non_actionable(env):
    from src.escalations import SupervisorDeliveryWatchdog

    current = await incident(env)
    message = await env.db.get_message("msg-" + current["id"])
    watchdog = SupervisorDeliveryWatchdog(env.db, env.orch.bus, env.config)
    assert await watchdog.tick(message.created_at + 901) == 1
    assert await watchdog.tick(message.created_at + 1800) == 0
    escalations = await env.db.list_escalations(project_id="p")
    watchdog_incidents = [e for e in escalations if e["source_kind"] == "supervisor_delivery"]
    assert len(watchdog_incidents) == 1
    unavailable = watchdog_incidents[0]
    assert unavailable["source_identity"] == "task_recovery:" + current["id"]
    assert "does not approve" in unavailable["decision_requested"]

    accepted = await env.handler.execute(
        "escalation_reply",
        {
            "escalation_id": unavailable["id"],
            "text": "Retry it",
            "external_message_id": "watchdog-reply",
        },
    )
    await env.db.create_session(
        SessionRecord(
            id="supervisor-p",
            project_id="p",
            profile_id="supervisor",
            harness="fake",
            provider="fake",
            name="n-supervisor--p",
            lifecycle="named",
            state="running",
            desired_state="running",
            epoch="test",
            instance_token="supervisor-p-token",
            work_dir="/never-used",
            started_at=message.created_at,
        )
    )
    result = await env.handler.execute(
        "escalation_apply_reply",
        {
            "escalation_id": unavailable["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "watchdog-cannot-retry",
            "action_kind": "task_recover",
            "target_id": "t",
            "decision": "retry",
            "_scope": {
                "kind": "session",
                "session_id": "supervisor-p",
                "session_instance_token": "supervisor-p-token",
                "project_id": "p",
                "elevated": True,
            },
        },
    )
    assert result["error_code"] == "invalid_binding"
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED


@pytest.mark.parametrize("probe", ["unavailable", "still-listed"])
async def test_cached_tmux_absence_is_not_stop_confirmation(env, probe):
    from src.sessions.tmux import TmuxProvider, TmuxCommandError

    current = await incident(env)
    provider = TmuxProvider(env.config)
    provider.socket = "disposable-never-contacted"
    provider.is_running = AsyncMock(return_value=False)
    provider._tmux = (
        AsyncMock(side_effect=TmuxCommandError(("list-sessions",), 1, "probe unavailable"))
        if probe == "unavailable"
        else AsyncMock(return_value="s\n")
    )
    env.orch.session_providers = SimpleNamespace(create=lambda *_: provider)
    result = await decide(env, current)
    assert "error" in result
    assert (await env.db.get_task("t")).status == TaskStatus.BLOCKED


async def test_strict_tmux_probe_bypasses_cached_absence_and_confirms_missing_name():
    from src.sessions.tmux import TmuxProvider
    from src.sessions.provider import SessionHandle

    provider = TmuxProvider()
    provider.socket = "disposable-never-contacted"
    provider._tmux = AsyncMock(return_value="n-supervisor--global\nother-worker\n")
    assert (
        await provider.confirm_stopped(SessionHandle("old-worker", "tmux", "old-instance")) is True
    )
    provider._tmux.assert_awaited_once_with("list-sessions", "-F", "#{session_name}")
