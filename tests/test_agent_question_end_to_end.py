"""Native transcript -> persisted question -> scoped answer -> original worker.

Only the external terminal is fake; parser, watcher, DB, commands, and HTTP
routing are real. This catches integration that unit-level service doubles miss.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request

from src.api.auth import RequestScope
from src.api.dependencies import get_command_handler
from src.api.execute import router
from src.asks.models import SourceIdentity
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from src.sessions.provider import NudgeDeferred
from src.sessions.transcripts.watcher import TranscriptWatcher
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.db_fixtures import lease_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()

QUESTION = (
    "One decision is needed before I finalize the design: where should the root "
    "task for a projectless system playbook run live?\n\n"
    "I recommend attaching it to the first available project. Is that acceptable?"
)


class Terminal:
    def __init__(self):
        self.draft = True
        self.submissions = []

    def supports(self, _cap):
        return True

    async def is_running(self, handle, **_kwargs):
        return handle.name == "s-task" and handle.instance_token == "original-instance"

    async def nudge(self, handle, text):
        if self.draft:
            raise NudgeDeferred("User draft is present")
        self.submissions.append((handle, text))


@pytest.fixture
async def flow(tmp_path, request):
    from src.sessions.questions import AgentQuestionService

    db = Database(lease_dsn("questions.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_agent(
        Agent(id="worker", name="Sol", profile_id="worker", state=AgentState.BUSY)
    )
    await db.create_task(
        Task(
            id="task",
            project_id="p",
            title="Playbook roots",
            description="Implement feature",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="worker",
            claim_epoch=7,
        )
    )
    await db.update_task("task", claim_epoch=7)
    await db.update_agent("worker", current_task_id="task")
    now = time.time()
    session = SessionRecord(
        id="original-session",
        project_id="p",
        profile_id="worker",
        harness="codex",
        provider="fake",
        name="s-task",
        lifecycle="task",
        work_dir="/workspace",
        epoch="daemon",
        instance_token="original-instance",
        started_at=now - 60,
        task_id="task",
        state="running",
        agent_id="worker",
        last_activity=now - 60,
        last_claim_epoch=7,
    )
    await db.create_session(session)
    base = tmp_path / "home"
    launch_date = datetime.fromtimestamp(now, timezone.utc).strftime("%Y/%m/%d")
    path = (
        base
        / ".codex/sessions"
        / launch_date
        / "rollout-2026-08-30T22-00-58-01a05631-2ba0-75b0-8c0b-38806072f86f.jsonl"
    )
    path.parent.mkdir(parents=True)
    # Keep the metadata safely inside CodexTranscriptReader's launch-time
    # matching window. Formatting through ISO-8601 can otherwise round a
    # timestamp just beyond the exact ``started_at + 60`` boundary.
    ts = datetime.fromtimestamp(now - 1, timezone.utc).isoformat()
    rows = [
        {
            "type": "session_meta",
            "timestamp": ts,
            "payload": {"id": "01a05631-2ba0-75b0-8c0b-38806072f86f", "cwd": "/workspace"},
        },
        {
            "type": "event_msg",
            "timestamp": ts,
            "payload": {"type": "task_started", "turn_id": "turn-one"},
        },
        {
            "type": "response_item",
            "timestamp": ts,
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": QUESTION}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": ts,
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-one",
                "last_agent_message": QUESTION,
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    terminal = Terminal()
    providers = SimpleNamespace(create=lambda *_args, **_kwargs: terminal)
    config = AppConfig()
    config.messages.enabled = True
    bus = EventBus()
    service = AgentQuestionService(db, bus, providers, config)
    events = []
    bus.subscribe("*", lambda data: events.append(dict(data)))
    orch = SimpleNamespace(db=db, bus=bus, agent_questions=service, plugin_registry=None)
    handler = CommandHandler(orch, config)
    yield SimpleNamespace(
        db=db,
        session=session,
        path=path,
        base=base,
        terminal=terminal,
        service=service,
        events=events,
        bus=bus,
        handler=handler,
    )
    await db.close()


async def answer_request(flow, scope, args):
    return await execute_request(flow, scope, "question_answer", args)


async def execute_request(flow, scope, command, args):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: flow.handler

    @app.middleware("http")
    async def set_scope(request: Request, call_next):
        request.state.scope = scope
        return await call_next(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/api/execute", json={"command": command, "args": args})


async def capture(flow):
    watcher = TranscriptWatcher(
        db=flow.db, bus=flow.bus, base_dir=flow.base, questions=flow.service
    )
    await watcher.tick()
    rows = await flow.db.list_agent_questions(session_id=flow.session.id)
    assert len(rows) == 1
    assert rows[0]["question"] == QUESTION
    assert rows[0]["state"] == "supervisor"
    assert rows[0]["requires_human"]
    return rows[0]


async def decide_question(flow, question_id, text="Proceed"):
    ask = await flow.handler._publish_source_ask(
        SourceIdentity("p", "question", question_id, {"id": question_id}))
    args = {"ask_id": ask["id"], "expected": {k: ask[k] for k in (
        "decision_generation", "source_fingerprint", "feedback_version")},
        "option_key": "answer", "overall_note": text, "idempotency_key": "question-decision"}
    preview = await flow.handler.execute("ask_prepare_decision", args)
    assert preview["success"], preview
    return await flow.handler.execute("ask_decide", {**args, "preview_id": preview["preview"]["id"]})


async def test_codex_question_survives_restart_and_answer_waits_for_draft(flow):
    q = await capture(flow)
    # Recreate watcher with zero offsets, like a daemon restart.
    await capture(flow)
    escalated = await flow.service.escalate(
        q["id"], "Repository inspection did not determine this product decision"
    )
    accepted = await flow.handler.execute(
        "escalation_reply",
        {
            "escalation_id": escalated["escalation_id"],
            "text": "Keep projectless runs projectless.",
            "external_message_id": "dashboard-question-answer",
        },
    )
    supervisor = replace(
        flow.session,
        id="supervisor-session",
        project_id="p",
        profile_id="supervisor",
        name="n-supervisor--p",
        lifecycle="named",
        task_id=None,
        agent_id=None,
        instance_token="supervisor-instance",
    )
    await flow.db.create_session(supervisor)
    args = {
            "escalation_id": escalated["escalation_id"],
            "reply_id": accepted["reply"]["id"],
            "expected_revision": accepted["escalation"]["revision"],
            "idempotency_key": "apply-dashboard-question-answer",
            "action_kind": "question_answer",
            "target_id": q["id"],
            "_scope": {
                "kind": "session",
                "session_id": supervisor.id,
                "session_instance_token": supervisor.instance_token,
                "project_id": "p",
                "elevated": True,
            },
        }
    legacy = await flow.handler.execute("escalation_apply_reply", args)
    assert legacy["error_code"] == "ask.context_missing"
    args["expected"] = legacy["expected"]
    preview = await flow.handler.execute("escalation_apply_reply", {**args, "prepare": True})
    assert preview["success"], preview
    args["preview_id"] = preview["preview"]["id"]
    applied = await flow.handler.execute("escalation_apply_reply", args)
    assert applied["application_state"] == "unknown", applied
    assert (await flow.db.get_escalation(escalated["escalation_id"]))["state"] == "reply_received"
    await flow.service.tick()
    assert flow.terminal.submissions == []
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "answered"
    flow.terminal.draft = False
    await flow.service.tick()
    await flow.service.tick()
    assert len(flow.terminal.submissions) == 1
    reconciled = await flow.handler.execute("ask_retry_operation", {
        "decision_id": applied["decision_id"]})
    assert reconciled["success"], reconciled
    assert reconciled["escalation"]["state"] == "resolved"
    assert (await flow.db.get_agent_question(q["id"]))["answer"] == "Keep projectless runs projectless."
    handle, text = flow.terminal.submissions[0]
    assert handle.name == "s-task" and handle.instance_token == "original-instance"
    # One line naming the durable answer, which the asking worker reads through
    # the same scope gate as every other command it runs.
    message_id = f"question:{q['id']}:answer"
    assert text == f"[aq question answered] Handle `aq message status {message_id} --json`."
    worker_scope = RequestScope(
        kind="session", session_id=flow.session.id, task_id="task", project_id="p"
    )
    response = await execute_request(
        flow, worker_scope, "message_status", {"message_id": message_id}
    )
    assert response.status_code == 200, response.text
    body = response.json()["result"]["message"]["body"]
    assert "Keep projectless runs projectless." in body
    task = await flow.db.get_task("task")
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.assigned_agent_id == "worker" and task.claim_epoch == 7
    assert (await flow.db.get_session(flow.session.id)).state == "running"


async def test_worker_cannot_spoof_human_answer_through_execute(flow):
    q = await capture(flow)
    worker_scope = RequestScope(
        kind="session", session_id=flow.session.id, task_id="task", project_id="p"
    )
    response = await answer_request(
        flow,
        worker_scope,
        {
            "question_id": q["id"],
            "body": "Approved",
            "human": True,
            "actor": "user",
            "_scope": {"kind": "local"},
        },
    )
    assert response.status_code == 403
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "supervisor"
    assert flow.terminal.submissions == []


async def test_answer_cannot_follow_reused_tmux_name_to_replacement(flow):
    q = await capture(flow)
    answer = await decide_question(flow, q["id"])
    assert answer["application_state"] == "unknown", answer
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "answered"
    await flow.db.update_session(flow.session.id, state="stopped", desired_state="stopped")
    replacement = replace(
        flow.session,
        id="replacement",
        instance_token="replacement-instance",
        started_at=time.time(),
    )
    await flow.db.create_session(replacement)
    flow.terminal.draft = False
    await flow.service.tick()
    assert flow.terminal.submissions == []
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "stale"


def append_native_user(flow, text):
    row = {
        "type": "event_msg",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "type": "item_completed",
            "turn_id": "turn-two",
            "item": {
                "type": "UserMessage",
                "id": "user-two",
                "content": [{"type": "text", "text": text, "text_elements": []}],
            },
        },
    }
    with flow.path.open("a") as output:
        output.write(json.dumps(row) + "\n")


async def test_direct_native_terminal_reply_resolves_pending_question(flow):
    q = await capture(flow)
    append_native_user(flow, "Keep it projectless and proceed with the approved feature.")
    watcher = TranscriptWatcher(
        db=flow.db, bus=flow.bus, base_dir=flow.base, questions=flow.service
    )
    await watcher.tick()
    await flow.service.tick()
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "resolved"
    assert await flow.db.list_agent_questions(session_id=flow.session.id) == []
    assert flow.terminal.submissions == []


async def test_native_automatic_stall_reminder_is_not_a_human_answer(flow):
    q = await capture(flow)
    append_native_user(
        flow,
        "No progress for 8 min. Report status, finish the task, or report a blocker with aq message send --to user:dashboard.",
    )
    watcher = TranscriptWatcher(
        db=flow.db, bus=flow.bus, base_dir=flow.base, questions=flow.service
    )
    await watcher.tick()
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "supervisor"
    assert len(await flow.db.list_agent_questions(session_id=flow.session.id)) == 1
    assert flow.terminal.submissions == []


async def test_elevated_supervisor_cannot_supply_human_only_approval(flow):
    q = await capture(flow)
    supervisor = replace(
        flow.session,
        id="supervisor-session",
        project_id=None,
        profile_id="supervisor",
        name="n-supervisor--global",
        lifecycle="named",
        task_id=None,
        agent_id=None,
        instance_token="supervisor-instance",
    )
    await flow.db.create_session(supervisor)
    scope = RequestScope(
        kind="session", session_id=supervisor.id, task_id=None, project_id=None, elevated=True
    )
    response = await answer_request(
        flow, scope, {"question_id": q["id"], "body": "Approved", "human": True, "actor": "user"}
    )
    # Legacy execute reports command denials in its stable 200/error envelope.
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": False, "error": "this question requires a human answer"}
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "supervisor"
    assert flow.terminal.submissions == []


async def test_expired_claim_cannot_accept_a_prepared_question(flow):
    q = await capture(flow)
    ask = await flow.handler._publish_source_ask(SourceIdentity("p", "question", q["id"], {"id": q["id"]}))
    args = {"ask_id": ask["id"], "expected": {k: ask[k] for k in (
        "decision_generation", "source_fingerprint", "feedback_version")},
        "option_key": "answer", "overall_note": "Proceed", "idempotency_key": "stale-question"}
    preview = await flow.handler.execute("ask_prepare_decision", args)
    assert preview["success"], preview
    await flow.db.update_task("task", claim_epoch=8)
    result = await flow.handler.execute("ask_decide", {**args, "preview_id": preview["preview"]["id"]})
    assert result["error_code"] == "ask.stale_source"
    assert (await flow.db.get_agent_question(q["id"]))["ask_decision_id"] is None
    assert flow.terminal.submissions == []


async def test_question_answer_while_paused_does_not_resume_worker(flow):
    q = await capture(flow)
    await flow.db.update_project("p", status="PAUSED")
    flow.terminal.draft = False
    result = await decide_question(flow, q["id"])
    assert result["application_state"] == "applying", result
    await flow.service.tick()
    assert flow.terminal.submissions == []
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "answered"

    from src.commands.principal import ExecutionPrincipal, principal_context
    await flow.db.update_project("p", status="ACTIVE")
    with principal_context(ExecutionPrincipal.service("ask-reconciliation")):
        resumed = await flow.handler._cmd_ask_retry_operation({"decision_id": result["decision_id"]})
    assert resumed["success"] and resumed["application_state"] == "applied", resumed
    assert len(flow.terminal.submissions) == 1


async def test_crash_after_terminal_submission_never_sends_twice(flow):
    q = await capture(flow)
    flow.terminal.draft = False
    nudge = flow.terminal.nudge
    async def crash(handle, text):
        await nudge(handle, text)
        raise RuntimeError("lost terminal acknowledgement")
    flow.terminal.nudge = crash
    result = await decide_question(flow, q["id"])
    assert result["application_state"] == "unknown", result
    await flow.service.tick()
    retry = await flow.handler.execute("ask_retry_operation", {"decision_id": result["decision_id"]})
    assert retry["application_state"] == "unknown"
    assert len(flow.terminal.submissions) == 1
    assert (await flow.db.get_agent_question(q["id"]))["submission_state"] == "unknown"
