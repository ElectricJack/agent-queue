"""Command-layer authorization and wiring for document reviews."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn


async def _scoped(handler, command: str, args: dict, *, session_id: str, project_id: str = "p"):
    return await handler.execute(
        command,
        {
            **args,
            "_scope": {
                "kind": "session",
                "session_id": session_id,
                "session_instance_token": f"{session_id}-token",
                "project_id": project_id,
                "task_id": "author" if session_id == "worker" else None,
                "elevated": session_id == "supervisor",
            },
        },
    )


@pytest.fixture
async def env(tmp_path):
    db = Database(lease_dsn("review-commands"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project P"))
    await db.create_project(Project(id="other", name="Other"))
    await db.create_profile(
        AgentProfile(
            id="worker",
            name="Worker",
            harness="codex",
            lifecycle="pool",
            aq_commands=["review_submit", "review_show", "review_list", "review_withdraw"],
            harness_tools=[],
            plugin_tools=[],
        )
    )
    await db.create_profile(
        AgentProfile(
            id="supervisor",
            name="Supervisor",
            harness="codex",
            lifecycle="named",
            aq_commands=[
                "review_submit", "review_show", "review_list", "review_withdraw", "review_decide",
                "review_comment", "review_delegate", "review_import_edits", "edit_project",
            ],
            harness_tools=[],
            plugin_tools=[],
            needs_workspace=False,
        )
    )
    await db.create_task(
        Task(
            id="author",
            project_id="p",
            title="Author task",
            description="Write the document",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    now = time.time()
    for session in (
        SessionRecord(
            id="worker", task_id="author", project_id="p", profile_id="worker", harness="codex",
            provider="fake", name="worker", lifecycle="pool", work_dir=str(tmp_path), epoch="test",
            instance_token="worker-token", started_at=now, state="running",
        ),
        SessionRecord(
            id="supervisor", project_id="p", profile_id="supervisor", harness="codex",
            provider="fake", name="supervisor", lifecycle="named", work_dir=str(tmp_path), epoch="test",
            instance_token="supervisor-token", started_at=now, state="running",
        ),
    ):
        await db.create_session(session)
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=lease_dsn("review-commands")),
        data_dir=str(tmp_path / "data"),
    )
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    orchestrator.bus = EventBus(env="dev")
    try:
        yield CommandHandler(orchestrator, config), db
    finally:
        await db.close()


async def test_submit_persists_events_and_enforces_worker_author_scope(env):
    handler, db = env
    created = await _scoped(
        handler,
        "review_submit",
        {
            "task_id": "author",
            "kind": "spec",
            "title": "The document",
            "content": "# The document\n",
        },
        session_id="worker",
    )
    assert created["success"] is True
    assert (await db.get_recent_events(event_type="review.submitted"))[0]["event_type"] == "review.submitted"

    other = await _scoped(
        handler,
        "review_submit",
        {"task_id": "someone-else", "kind": "spec", "title": "No", "content": "# No\n"},
        session_id="worker",
    )
    assert other["error_code"] == "not_your_task"
    listed = await _scoped(
        handler, "review_list", {"task_id": "someone-else"}, session_id="worker"
    )
    assert listed["error_code"] == "not_your_task"


async def test_delegated_supervisor_can_decide_and_changes_reopen_author(env):
    handler, db = env
    created = await handler.execute(
        "review_submit",
        {"task_id": "author", "kind": "plan", "title": "Plan", "content": "# Plan\n"},
    )
    review_id = created["review_id"]
    not_delegated = await _scoped(
        handler,
        "review_decide",
        {"review_id": review_id, "revision": 1, "decision": "approve"},
        session_id="supervisor",
    )
    assert not_delegated["error_code"] == "not_decider"

    assert (await handler.execute("review_delegate", {"review_id": review_id, "to": "supervisor"}))["success"]
    decided = await _scoped(
        handler,
        "review_decide",
        {"review_id": review_id, "revision": 1, "decision": "request_changes", "note": "Tighten it."},
        session_id="supervisor",
    )
    assert decided["state"] == "changes_requested"
    review = await db.get_review(review_id)
    assert review["decided_by"].startswith("supervisor (delegated)")
    author = await db.get_task("author")
    assert author.status is TaskStatus.IN_PROGRESS
    comments = await db.list_task_comments("author")
    assert any("Tighten it." in comment["body"] for comment in comments["comments"])


async def test_edit_project_review_default_is_local_only(env):
    handler, _db = env
    updated = await handler.execute(
        "edit_project", {"project_id": "p", "review_delegate_to": "supervisor"}
    )
    assert updated["fields"] == ["review_delegate_to"]
    refused = await _scoped(
        handler,
        "edit_project",
        {"project_id": "p", "review_delegate_to": "user"},
        session_id="supervisor",
    )
    assert refused["error_code"] == "local_operator_only"
