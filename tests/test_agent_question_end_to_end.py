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

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.dependencies import get_command_handler
from src.api.execute import router
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from src.sessions.provider import NudgeDeferred
from src.sessions.transcripts.watcher import TranscriptWatcher
from tests.pg_dsn import ensure_worker_postgres_dsn

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


@pytest.fixture(params=["sqlite", "postgresql"])
async def flow(tmp_path, request):
    from src.sessions.questions import AgentQuestionService

    if request.param == "postgresql":
        if not POSTGRES_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
        db = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
    else:
        db = Database(str(tmp_path / "questions.db"))
    await db.initialize()
    if request.param == "postgresql":
        await db.reset_for_tests()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_agent(Agent(id="worker", name="Sol", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(Task(id="task", project_id="p", title="Playbook roots", description="Implement feature",
                              status=TaskStatus.IN_PROGRESS, assigned_agent_id="worker", claim_epoch=7))
    await db.update_task("task", claim_epoch=7)
    await db.update_agent("worker", current_task_id="task")
    now = time.time()
    session = SessionRecord(id="original-session", project_id="p", profile_id="worker", harness="codex",
                            provider="fake", name="s-task", lifecycle="task", work_dir="/workspace",
                            epoch="daemon", instance_token="original-instance", started_at=now-60,
                            task_id="task", state="running", agent_id="worker", last_activity=now-60,
                            last_claim_epoch=7)
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
        {"type":"session_meta", "timestamp":ts, "payload":{"id":"01a05631-2ba0-75b0-8c0b-38806072f86f", "cwd":"/workspace"}},
        {"type":"event_msg", "timestamp":ts, "payload":{"type":"task_started", "turn_id":"turn-one"}},
        {"type":"response_item", "timestamp":ts, "payload":{"type":"message", "role":"assistant", "phase":"final_answer",
            "content":[{"type":"output_text", "text":QUESTION}]}},
        {"type":"event_msg", "timestamp":ts, "payload":{"type":"task_complete", "turn_id":"turn-one", "last_agent_message":QUESTION}},
    ]
    path.write_text("".join(json.dumps(row)+"\n" for row in rows))
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
    yield SimpleNamespace(db=db, session=session, path=path, base=base, terminal=terminal,
                          service=service, events=events, bus=bus, handler=handler)
    if request.param == "postgresql":
        await db.reset_for_tests()
    await db.close()


async def answer_request(flow, scope, args):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: flow.handler

    @app.middleware("http")
    async def set_scope(request: Request, call_next):
        request.state.scope = scope
        return await call_next(request)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/execute", json={"command":"question_answer", "args":args})


async def capture(flow):
    watcher = TranscriptWatcher(db=flow.db, bus=flow.bus, base_dir=flow.base, questions=flow.service)
    await watcher.tick()
    rows = await flow.db.list_agent_questions(session_id=flow.session.id)
    assert len(rows) == 1
    assert rows[0]["question"] == QUESTION
    assert rows[0]["state"] == "human"
    assert rows[0]["requires_human"]
    return rows[0]


async def test_codex_question_survives_restart_and_answer_waits_for_draft(flow):
    q = await capture(flow)
    # Recreate watcher with zero offsets, like a daemon restart.
    await capture(flow)
    response = await answer_request(flow, LOCAL_SCOPE, {"question_id":q["id"], "body":"Keep projectless runs projectless."})
    assert response.status_code == 200, response.text
    await flow.service.tick()
    assert flow.terminal.submissions == []
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "answered"
    flow.terminal.draft = False
    await flow.service.tick()
    await flow.service.tick()
    assert len(flow.terminal.submissions) == 1
    handle, text = flow.terminal.submissions[0]
    assert handle.name == "s-task" and handle.instance_token == "original-instance"
    assert "Keep projectless runs projectless." in text
    task = await flow.db.get_task("task")
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.assigned_agent_id == "worker" and task.claim_epoch == 7
    assert (await flow.db.get_session(flow.session.id)).state == "running"


async def test_worker_cannot_spoof_human_answer_through_execute(flow):
    q = await capture(flow)
    worker_scope = RequestScope(kind="session", session_id=flow.session.id, task_id="task", project_id="p")
    response = await answer_request(flow, worker_scope, {"question_id":q["id"], "body":"Approved",
        "human":True, "actor":"user", "_scope":{"kind":"local"}})
    assert response.status_code == 403
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "human"
    assert flow.terminal.submissions == []


async def test_answer_cannot_follow_reused_tmux_name_to_replacement(flow):
    q = await capture(flow)
    response = await answer_request(flow, LOCAL_SCOPE, {"question_id":q["id"], "body":"Proceed"})
    assert response.status_code == 200, response.text
    await flow.db.update_session(flow.session.id, state="stopped", desired_state="stopped")
    replacement = replace(flow.session, id="replacement", instance_token="replacement-instance", started_at=time.time())
    await flow.db.create_session(replacement)
    flow.terminal.draft = False
    await flow.service.tick()
    assert flow.terminal.submissions == []
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "stale"


def append_native_user(flow, text):
    row = {"type":"event_msg", "timestamp":datetime.now(timezone.utc).isoformat(),
           "payload":{"type":"item_completed", "turn_id":"turn-two", "item":{
               "type":"UserMessage", "id":"user-two", "content":[{"type":"text", "text":text, "text_elements":[]}]}}}
    with flow.path.open("a") as output:
        output.write(json.dumps(row)+"\n")


async def test_direct_native_terminal_reply_resolves_pending_question(flow):
    q = await capture(flow)
    append_native_user(flow, "Keep it projectless and proceed with the approved feature.")
    watcher = TranscriptWatcher(db=flow.db, bus=flow.bus, base_dir=flow.base, questions=flow.service)
    await watcher.tick()
    await flow.service.tick()
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "resolved"
    assert await flow.db.list_agent_questions(session_id=flow.session.id) == []
    assert flow.terminal.submissions == []


async def test_native_automatic_stall_reminder_is_not_a_human_answer(flow):
    q = await capture(flow)
    append_native_user(flow, "No progress for 8 min. Report status, finish the task, or report a blocker with aq message send --to user.")
    watcher = TranscriptWatcher(db=flow.db, bus=flow.bus, base_dir=flow.base, questions=flow.service)
    await watcher.tick()
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "human"
    assert len(await flow.db.list_agent_questions(session_id=flow.session.id)) == 1
    assert flow.terminal.submissions == []


async def test_elevated_supervisor_cannot_supply_human_only_approval(flow):
    q = await capture(flow)
    supervisor = replace(flow.session, id="supervisor-session", project_id=None, profile_id="supervisor",
                         name="n-supervisor--global", lifecycle="named", task_id=None, agent_id=None,
                         instance_token="supervisor-instance")
    await flow.db.create_session(supervisor)
    scope = RequestScope(kind="session", session_id=supervisor.id, task_id=None, project_id=None, elevated=True)
    response = await answer_request(flow, scope, {"question_id":q["id"], "body":"Approved", "human":True, "actor":"user"})
    # Legacy execute reports command denials in its stable 200/error envelope.
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": False, "error": "this question requires a human answer"}
    assert (await flow.db.get_agent_question(q["id"]))["state"] == "human"
    assert flow.terminal.submissions == []
