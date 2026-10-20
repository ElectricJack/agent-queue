"""Scoped command contracts and verified-human application for escalations."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.event_bus import EventBus
from src.models import Agent, AgentState, AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def env(tmp_path, reuse_database):
    db = await reuse_database("escalation-commands")
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
                "escalation_resolve",
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


def create_args(**overrides):
    values = {
        "project_id": "p",
        "task_title": "Decide how to resume project work",
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


@pytest.mark.parametrize("field", ["task_title", "project_id", "investigation"])
@pytest.mark.parametrize("empty", [None, "", " \n\t"])
async def test_create_refuses_missing_human_context(env, field, empty):
    handler, db = env
    result = await handler.execute("escalation_create", create_args(**{field: empty}))
    assert result["success"] is False and result["error_code"] == "invalid_request"
    assert await db.list_escalations() == []


async def test_create_replay_scoped_reads_and_stable_cas_errors(env):
    handler, db = env
    created = await handler.execute("escalation_create", create_args())
    replay = await handler.execute("escalation_create", create_args())
    assert created["success"] is True and created["created"] is True
    assert replay["created"] is False
    assert replay["escalation"]["id"] == created["escalation"]["id"]
    assert created["escalation"]["supervisor_owner"] == "supervisor-p"
    assert created["escalation"]["deep_link"].startswith("/focus/escalations/")
    assert len(await db.list_escalation_deliveries(created["escalation"]["id"])) == 1

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
            "text": "approve",
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


async def versioned_apply(handler, args):
    legacy = await supervisor_execute(handler, "escalation_apply_reply", args)
    assert legacy["error_code"] == "ask.context_missing", legacy
    args["expected"] = legacy["expected"]
    prepared = await supervisor_execute(handler, "escalation_apply_reply", {**args, "prepare": True})
    if not prepared["success"]:
        return prepared
    args["preview_id"] = prepared["preview"]["id"]
    return await supervisor_execute(handler, "escalation_apply_reply", args)


async def test_apply_reply_resolves_only_bound_human_gate_once(env):
    handler, db = env
    gate_id, _ = await db.create_gate(
        "p", "human", "Approve rollout", question="Proceed?", await_id="rollout-1",
        human_request={"policy": "approval_hold_v1", "context": {"waiting_effect": "Rollout waits."}}
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
            "text": "approve",
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
        "option_key": "approve",
        "action_kind": "gate_resolve",
        "target_id": gate_id,
    }
    applied = await versioned_apply(handler, args)
    replayed = await supervisor_execute(handler, "escalation_apply_reply", args)
    assert applied["success"] is True and applied["applied"] is True
    assert applied["escalation"]["state"] == "resolved"
    assert replayed["success"] is True and replayed["replayed"] is True
    assert applied["decision_id"] == replayed["decision_id"]
    assert applied["created"] and not replayed["created"]
    assert (await db.get_gate(gate_id))["status"] == "resolved"
    assert (await db.get_gate(other_gate_id))["status"] == "open"


async def test_required_human_question_uses_verified_reply_not_supervisor_text(env):
    handler, db = env
    await db.create_task(
        Task(id="question-task", project_id="p", title="Question", description="Q")
    )
    await db.create_agent(Agent(id="worker-agent", name="Worker", profile_id="worker", state=AgentState.BUSY))
    await db.update_task("question-task", status=TaskStatus.IN_PROGRESS, assigned_agent_id="worker-agent", claim_epoch=4)
    await db.update_agent("worker-agent", current_task_id="question-task")
    await db.create_session(SessionRecord(id="worker-session", project_id="p", profile_id="worker",
        harness="codex", provider="fake", name="worker", lifecycle="task", work_dir="/workspace",
        epoch="test", instance_token="worker-token", started_at=time.time(), task_id="question-task",
        state="running", agent_id="worker-agent", last_claim_epoch=4))
    handler.orchestrator.agent_questions.db = db
    handler.config.messages.enabled = True
    terminal = MagicMock()
    terminal.supports.return_value = True
    terminal.is_running = AsyncMock(return_value=True)
    terminal.nudge = AsyncMock()
    handler.orchestrator.agent_questions.providers = MagicMock()
    handler.orchestrator.agent_questions.providers.create.return_value = terminal
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
    answer = AsyncMock(wraps=handler.orchestrator.agent_questions.answer_on)
    handler.orchestrator.agent_questions.answer_on = answer
    result = await versioned_apply(
        handler,
        {
            "escalation_id": incident["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "apply-question-answer",
            "action_kind": "question_answer",
            "target_id": "question-1",
        },
    )
    assert result["success"] is True, result
    answer.assert_awaited_once()
    packet = answer.await_args.args[1]
    assert packet["overall_note"] == "Use the non-destructive option"
    assert packet["actor_id"] == "human:local-operator"
    assert packet["packet"]["actor"]["input_id"] == accepted["reply"]["id"]
    assert (await db.get_agent_question("question-1"))["submission_state"] == "delivered"
    terminal.nudge.assert_awaited_once()



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
    result = await versioned_apply(
        handler,
        {
            "escalation_id": incident["id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "apply-failed-question-answer",
            "action_kind": "question_answer",
            "target_id": "q-failed",
        },
    )
    assert result["error_code"] == "ask.stale_source"
    assert (await db.get_escalation(incident["id"]))["state"] == "reply_received"
    history = await db.list_escalation_messages(incident["id"])
    assert [entry["direction"] for entry in history] == ["inbound"]
    handler.orchestrator.agent_questions.answer.assert_not_awaited()


async def test_task_recovery_refuses_ambiguous_reply_without_source_action(env):
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
    result = await versioned_apply(
        handler,
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
    assert result["error_code"] == "ask.input_ambiguous"
    recover.assert_not_awaited()
    assert (await db.get_task("blocked-task")).status == TaskStatus.BLOCKED



# ======================================================================
# §5.3 — escalation_resolve: the supervisor closes what a human answered
# ======================================================================


async def answered_incident(handler, *, external="human-answer-1"):
    incident = (await handler.execute("escalation_create", create_args()))["escalation"]
    accepted = await handler.execute(
        "escalation_reply",
        {
            "escalation_id": incident["id"],
            "text": "Ship from main",
            "external_message_id": external,
        },
    )
    return incident, accepted


async def test_resolve_closes_an_answered_incident_with_the_outcome_a_human_reads(env):
    handler, db = env
    incident, accepted = await answered_incident(handler)

    result = await supervisor_execute(
        handler,
        "escalation_resolve",
        {"escalation_id": incident["id"], "outcome": "Shipped from main"},
    )

    assert result["success"] is True and result["resolved"] is True
    stored = await db.get_escalation(incident["id"])
    assert stored["state"] == "resolved"
    assert stored["terminal_outcome"] == "Shipped from main"
    # A person closed it, so §5.2's collapsed row reads "Resolved", not
    # "No longer needed" -- which is what `outcome` decides.
    assert stored["outcome"] == "human"
    assert stored["terminal_evidence"]["resolved_by"] == "session:super-p"
    assert stored["revision"] > accepted["escalation"]["revision"]
    assert stored["terminal_at"] is not None


async def test_resolve_walks_the_legal_path_and_never_resolves_over_a_newer_answer(env):
    handler, db = env
    incident, _accepted = await answered_incident(handler)

    stale = await supervisor_execute(
        handler,
        "escalation_resolve",
        {"escalation_id": incident["id"], "outcome": "Shipped from main", "expected_revision": 0},
    )
    assert stale["error_code"] == "stale_revision"
    assert (await db.get_escalation(incident["id"]))["state"] == "reply_received"

    resolved = await supervisor_execute(
        handler,
        "escalation_resolve",
        {"escalation_id": incident["id"], "outcome": "Shipped from main"},
    )
    assert resolved["success"] is True
    assert (await db.get_escalation(incident["id"]))["state"] == "resolved"
    # The terminal write happened after the intermediate one, never instead.
    assert resolved["escalation"]["revision"] == 3


async def test_resolve_refuses_an_unanswered_incident_and_names_the_alternative(env):
    handler, db = env
    incident = (await handler.execute("escalation_create", create_args()))["escalation"]

    result = await supervisor_execute(
        handler, "escalation_resolve", {"escalation_id": incident["id"], "outcome": "Moot"}
    )

    assert result["error_code"] == "invalid_state"
    assert "cancelled" in result["error"]
    assert (await db.get_escalation(incident["id"]))["state"] == "needs_human"


async def test_resolve_is_terminal_immutable_and_outcome_is_required(env):
    handler, db = env
    incident, _accepted = await answered_incident(handler)
    await supervisor_execute(
        handler, "escalation_resolve", {"escalation_id": incident["id"], "outcome": "Shipped"}
    )

    again = await supervisor_execute(
        handler, "escalation_resolve", {"escalation_id": incident["id"], "outcome": "Shipped"}
    )
    blank = await supervisor_execute(
        handler, "escalation_resolve", {"escalation_id": incident["id"], "outcome": "  "}
    )
    other = await supervisor_execute(
        handler, "escalation_resolve", {"escalation_id": "escalation-nope", "outcome": "x"}
    )

    assert again["error_code"] == "invalid_state"
    assert blank["error_code"] == "invalid_request"
    assert other["error_code"] == "not_found"


async def test_resolve_is_the_owning_supervisors_and_nobody_elses(env):
    handler, db = env
    incident, _accepted = await answered_incident(handler)
    args = {"escalation_id": incident["id"], "outcome": "Shipped"}

    local = await handler.execute("escalation_resolve", args)
    spoofed = await supervisor_execute(
        handler, "escalation_resolve", {**args, "verified_actor": "human:jack"}
    )

    assert local["error_code"] == "out_of_scope"
    assert spoofed["error_code"] == "spoofed_identity"
    assert (await db.get_escalation(incident["id"]))["state"] == "reply_received"


@pytest.mark.parametrize("profile_id", ["supervisor", "supervisor-global"])
@pytest.mark.parametrize("action", ["resolve", "cancel"])
async def test_global_supervisor_resolves_or_cancels_another_projects_escalation(
    env, tmp_path, profile_id, action
):
    handler, db = env
    if profile_id == "supervisor-global":
        await db.create_profile(AgentProfile(
            id=profile_id, name="Global supervisor", harness="codex", lifecycle="named",
            aq_commands=["escalation_resolve", "escalation_update"],
            harness_tools=[], plugin_tools=[], needs_workspace=False,
        ))
    await db.create_session(SessionRecord(
        id="global-super", project_id=None, profile_id=profile_id, harness="codex",
        provider="fake", name="n-supervisor--global", lifecycle="named",
        work_dir=str(tmp_path), epoch="epoch", instance_token="global-token",
        started_at=time.time(), state="running",
    ))
    incident = (await handler.execute(
        "escalation_create", create_args(project_id="other")
    ))["escalation"]
    revision = incident["revision"]
    if action == "resolve":
        accepted = await handler.execute("escalation_reply", {
            "escalation_id": incident["id"], "text": "Authorization granted",
            "external_message_id": "global-handled-answer",
        })
        revision = accepted["escalation"]["revision"]
    args = {"escalation_id": incident["id"], "expected_revision": revision}
    if action == "resolve":
        command = "escalation_resolve"
        args["outcome"] = "Applied the verified authorization"
    else:
        command = "escalation_update"
        args.update(state="cancelled", terminal_outcome="Decision no longer needed")
    denied = await supervisor_execute(handler, command, args)
    assert denied["error_code"] == "out_of_scope"
    result = await handler.execute(command, {**args, "_scope": {
        "kind": "session", "session_id": "global-super",
        "session_instance_token": "global-token", "project_id": None,
        "task_id": None, "elevated": True,
    }})
    assert result["success"] is True
    assert result["escalation"]["state"] == ("resolved" if action == "resolve" else "cancelled")
    assert result["escalation"]["supervisor_owner"] == "supervisor-other"
    if action == "resolve":
        assert result["escalation"]["terminal_evidence"]["resolved_by"] == "session:global-super"


@pytest.mark.parametrize("elevated,state,project_id", [
    (False, "running", None), (True, "stopped", None), (True, "running", "p"),
])
async def test_global_supervisor_authority_requires_a_live_system_scoped_session(
    env, tmp_path, elevated, state, project_id
):
    handler, db = env
    await db.create_profile(AgentProfile(
        id="supervisor-global", name="Global", harness="codex", lifecycle="named",
        aq_commands=["escalation_resolve"], harness_tools=[], plugin_tools=[],
        needs_workspace=False,
    ))
    await db.create_session(SessionRecord(
        id="global-super", project_id=project_id, profile_id="supervisor-global",
        harness="codex", provider="fake", name="n-supervisor--global", lifecycle="named",
        work_dir=str(tmp_path), epoch="epoch", instance_token="global-token",
        started_at=time.time(), state=state,
    ))
    incident, _ = await answered_incident(handler)
    result = await handler.execute("escalation_resolve", {
        "escalation_id": incident["id"], "outcome": "Applied",
        "_scope": {
            "kind": "session", "session_id": "global-super",
            "session_instance_token": "global-token", "project_id": project_id,
            "task_id": None, "elevated": elevated,
        },
    })
    assert result["error_code"] == "out_of_scope"
    assert (await db.get_escalation(incident["id"]))["state"] == "reply_received"
