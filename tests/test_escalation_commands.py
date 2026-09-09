"""Scoped command contracts and verified-human application for escalations."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def env(tmp_path):
    db = Database(lease_dsn("escalation-commands"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project P"))
    await db.create_project(Project(id="other", name="Other"))
    await db.create_profile(
        AgentProfile(
            id="supervisor",
            name="Supervisor",
            harness="codex",
            lifecycle="named",
            aq_commands=[
                "escalation_create",
                "escalation_list",
                "escalation_get",
                "escalation_reply",
                "escalation_update",
                "escalation_apply_reply",
            ],
            harness_tools=[],
            plugin_tools=[],
            needs_workspace=False,
        )
    )
    session = SessionRecord(
        id="super-p",
        project_id="p",
        profile_id="supervisor",
        harness="codex",
        provider="fake",
        name="n-supervisor--p",
        lifecycle="named",
        work_dir=str(tmp_path),
        epoch="epoch",
        instance_token="super-token",
        started_at=time.time(),
        state="running",
    )
    await db.create_session(session)
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=lease_dsn("escalation-commands")),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.bus = EventBus(env="dev")
    yield CommandHandler(orch, config), db
    await db.close()


def create_args(**overrides):
    values = {
        "project_id": "p",
        "source_kind": "core",
        "source_identity": "incident-source-1",
        "incident_key": "incident-1",
        "summary": "Work is blocked",
        "investigation": "The supervisor inspected the current attempt",
        "decision_requested": "Choose whether to proceed",
        "severity": "high",
    }
    values.update(overrides)
    return values


async def supervisor_execute(handler, command, args):
    return await handler.execute(
        command,
        {
            **args,
            "_scope": {
                "kind": "session",
                "session_id": "super-p",
                "session_instance_token": "super-token",
                "task_id": None,
                "project_id": "p",
                "elevated": True,
            },
        },
    )


async def test_create_replay_scoped_reads_and_stable_cas_errors(env):
    handler, db = env
    created = await handler.execute("escalation_create", create_args())
    replay = await handler.execute("escalation_create", create_args())
    assert created["success"] is True and created["created"] is True
    assert replay["created"] is False
    assert replay["escalation"]["id"] == created["escalation"]["id"]
    assert created["escalation"]["supervisor_owner"] == "supervisor-p"

    visible = await supervisor_execute(
        handler, "escalation_get", {"escalation_id": created["escalation"]["id"]}
    )
    assert visible["success"] is True and visible["actions"] == []
    cross_project = await supervisor_execute(
        handler, "escalation_list", {"project_id": "other"}
    )
    assert cross_project["error_code"] == "out_of_scope"

    local_update = await handler.execute(
        "escalation_update",
        {"escalation_id": created["escalation"]["id"], "expected_revision": 0},
    )
    assert local_update["error_code"] == "out_of_scope"
    stale = await supervisor_execute(
        handler,
        "escalation_update",
        {
            "escalation_id": created["escalation"]["id"],
            "expected_revision": 99,
            "summary": "new summary",
        },
    )
    assert stale["error_code"] == "stale_revision"
    assert (await db.get_escalation(created["escalation"]["id"]))["summary"] == "Work is blocked"


async def test_reply_identity_cannot_be_spoofed_and_notice_is_atomic(env):
    handler, db = env
    incident = (await handler.execute("escalation_create", create_args()))["escalation"]
    spoof = await handler.execute(
        "escalation_reply",
        {
            "escalation_id": incident["id"],
            "text": "Proceed",
            "external_message_id": "dashboard-1",
            "human": True,
            "actor_id": "admin",
        },
    )
    assert spoof["error_code"] == "spoofed_identity"
    assert await db.list_escalation_messages(incident["id"]) == []

    supervisor_spoof = await supervisor_execute(
        handler,
        "escalation_reply",
        {
            "escalation_id": incident["id"],
            "text": "I am the human",
            "external_message_id": "supervisor-1",
        },
    )
    assert supervisor_spoof["error_code"] == "human_evidence_required"
    accepted = await handler.execute(
        "escalation_reply",
        {
            "escalation_id": incident["id"],
            "text": "Proceed with the reviewed option",
            "external_message_id": "dashboard-1",
        },
    )
    assert accepted["success"] is True and accepted["supervisor_enqueued"] is True
    reply = accepted["reply"]
    assert reply["verified_actor"] == "human:local-operator"
    queued = await db.get_pending_messages("session", "supervisor-p")
    assert [message.id for message in queued] == [reply["supervisor_message_id"]]


async def test_apply_reply_resolves_only_bound_human_gate_once(env):
    handler, db = env
    gate_id, _ = await db.create_gate(
        "p", "human", "Approve rollout", question="Proceed?", await_id="rollout-1"
    )
    other_gate_id, _ = await db.create_gate(
        "p", "human", "Other choice", question="Other?", await_id="other-1"
    )
    incident = (
        await handler.execute(
            "escalation_create",
            create_args(
                source_kind="gate",
                source_identity=gate_id,
                incident_key="gate-incident",
            ),
        )
    )["escalation"]
    accepted = await handler.execute(
        "escalation_reply",
        {
            "escalation_id": incident["id"],
            "text": "Proceed",
            "external_message_id": "dashboard-gate-answer",
        },
    )
    wrong = await supervisor_execute(
        handler,
        "escalation_apply_reply",
        {
            "escalation_id": incident["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "wrong-target",
            "action_kind": "gate_resolve",
            "target_id": other_gate_id,
        },
    )
    assert wrong["error_code"] == "invalid_binding"
    assert await db.list_escalation_actions(incident["id"]) == []
    assert (await db.get_escalation(incident["id"]))["revision"] == 1

    args = {
        "escalation_id": incident["id"],
        "reply_id": accepted["reply"]["id"],
        "expected_revision": accepted["escalation"]["revision"],
        "idempotency_key": "apply-gate-answer",
        "action_kind": "gate_resolve",
        "target_id": gate_id,
    }
    applied = await supervisor_execute(handler, "escalation_apply_reply", args)
    replayed = await supervisor_execute(handler, "escalation_apply_reply", args)
    assert applied["success"] is True and applied["applied"] is True
    assert applied["escalation"]["state"] == "resolved"
    assert replayed["success"] is True and replayed["replayed"] is True
    assert len(await db.list_escalation_actions(incident["id"])) == 1
    assert (await db.get_gate(gate_id))["status"] == "resolved"
    assert (await db.get_gate(other_gate_id))["status"] == "open"


async def test_required_human_question_uses_verified_reply_not_supervisor_text(env):
    handler, db = env
    await db.create_task(
        Task(id="question-task", project_id="p", title="Question", description="Q")
    )
    await db.create_agent_question(
        id="question-1",
        session_id="worker-session",
        session_name="worker",
        instance_token="worker-token",
        task_id="question-task",
        project_id="p",
        agent_id="worker-agent",
        claim_epoch=4,
        turn_id="turn-1",
        question="May I proceed?",
        requires_human=True,
        state="human",
        created_at=1.0,
        updated_at=1.0,
        source_ts=1.0,
    )
    incident = (
        await handler.execute(
            "escalation_create",
            create_args(
                source_kind="question",
                source_identity="question-1",
                incident_key="question-incident",
            ),
        )
    )["escalation"]
    accepted = await handler.execute(
        "escalation_reply",
        {
            "escalation_id": incident["id"],
            "text": "Use the non-destructive option",
            "external_message_id": "question-human-answer",
        },
    )
    answer = AsyncMock(return_value={"id": "question-1", "state": "delivered"})
    handler.orchestrator.agent_questions.answer = answer
    result = await supervisor_execute(
        handler,
        "escalation_apply_reply",
        {
            "escalation_id": incident["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "apply-question-answer",
            "action_kind": "question_answer",
            "target_id": "question-1",
        },
    )
    assert result["success"] is True
    answer.assert_awaited_once_with(
        "question-1",
        "Use the non-destructive option",
        actor="human:local-operator",
        human=True,
        verified_escalation_id=incident["id"],
    )


async def test_failed_question_recovery_stays_open_with_same_conversation_followup(env):
    handler, db = env
    await db.create_task(Task(id="q-task", project_id="p", title="Question", description="Q"))
    await db.create_agent_question(
        id="q-failed",
        session_id="worker-session",
        session_name="worker",
        instance_token="worker-token",
        task_id="q-task",
        project_id="p",
        agent_id="worker-agent",
        claim_epoch=9,
        turn_id="turn-failed",
        question="Which option?",
        requires_human=True,
        state="human",
        created_at=1.0,
        updated_at=1.0,
        source_ts=1.0,
    )
    incident = (
        await handler.execute(
            "escalation_create",
            create_args(
                task_id="q-task",
                source_kind="question",
                source_identity="q-failed",
                incident_key="question-failed",
            ),
        )
    )["escalation"]
    accepted = await handler.execute(
        "escalation_reply",
        {
            "escalation_id": incident["id"],
            "text": "Choose option B",
            "external_message_id": "question-failed-answer",
        },
    )
    handler.orchestrator.agent_questions.answer = AsyncMock(
        return_value={"error": "original worker claim is stale"}
    )
    result = await supervisor_execute(
        handler,
        "escalation_apply_reply",
        {
            "escalation_id": incident["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "apply-failed-question-answer",
            "action_kind": "question_answer",
            "target_id": "q-failed",
        },
    )
    assert result["error_code"] == "action_failed"
    assert result["escalation"]["state"] == "reply_received"
    history = await db.list_escalation_messages(incident["id"])
    assert [entry["direction"] for entry in history] == ["inbound", "outbound"]
    assert "original worker claim is stale" in history[-1]["text"]


async def test_task_recovery_delegates_exact_incident_decision_and_reply(env):
    handler, db = env
    await db.create_task(
        Task(
            id="blocked-task",
            project_id="p",
            title="Blocked",
            description="Blocked task",
            status=TaskStatus.BLOCKED,
        )
    )
    await db.set_task_meta(
        "blocked-task", "supervisor_recovery_incident", {"id": "recovery-incident"}
    )
    incident = (
        await handler.execute(
            "escalation_create",
            create_args(
                task_id="blocked-task",
                source_kind="task_recovery",
                source_identity="recovery-incident",
                incident_key="recovery-incident-key",
            ),
        )
    )["escalation"]
    accepted = await handler.execute(
        "escalation_reply",
        {
            "escalation_id": incident["id"],
            "text": "Retry after preserving the current evidence",
            "external_message_id": "recovery-human-answer",
        },
    )
    recover = AsyncMock(
        return_value={
            "task_id": "blocked-task",
            "incident_id": "recovery-incident",
            "decision": "retry",
            "status": "READY",
        }
    )
    handler._cmd_task_recover = recover
    result = await supervisor_execute(
        handler,
        "escalation_apply_reply",
        {
            "escalation_id": incident["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "apply-recovery-answer",
            "action_kind": "task_recover",
            "target_id": "blocked-task",
            "decision": "retry",
        },
    )
    assert result["success"] is True
    recover.assert_awaited_once_with(
        {
            "task_id": "blocked-task",
            "incident_id": "recovery-incident",
            "decision": "retry",
            "reason": "Retry after preserving the current evidence",
        }
    )
