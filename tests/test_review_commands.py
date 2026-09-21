"""Command-layer authorization and wiring for document reviews."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.event_bus import EventBus
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.api.websocket import _FORWARDED_PREFIXES


async def _scoped(
    handler,
    command: str,
    args: dict,
    *,
    session_id: str,
    project_id: str = "p",
    task_id: str | None = None,
):
    if task_id is None:
        task_id = {
            "worker": "author",
            "other-worker": "other-author",
            "peer-worker": "peer",
        }.get(session_id)
    scope_data = {
        "kind": "session",
        "session_id": session_id,
        "session_instance_token": f"{session_id}-token",
        "project_id": project_id,
        "task_id": task_id,
        "elevated": session_id == "supervisor",
    }
    scoped_args = dict(args)
    scope_error = check_command_scope(
        command,
        scoped_args,
        RequestScope(
            kind="session",
            session_id=session_id,
            session_instance_token=f"{session_id}-token",
            project_id=project_id,
            task_id=task_id,
            elevated=session_id == "supervisor",
        ),
    )
    if scope_error:
        return {"success": False, "error": scope_error}
    return await handler.execute(
        command,
        {
            **scoped_args,
            "_scope": scope_data,
        },
    )


@pytest.fixture
async def env(command_handler_factory, tmp_path):
    handler = await command_handler_factory()
    db = handler.db
    handler.orchestrator.bus = EventBus(env="dev")
    await db.create_project(Project(id="p", name="Project P"))
    await db.create_project(Project(id="other", name="Other"))
    await db.create_profile(
        AgentProfile(
            id="worker",
            name="Worker",
            harness="codex",
            lifecycle="pool",
            # Grant every review command here so these tests exercise the
            # request-scope boundary independently from profile discovery.
            # Shipped worker profiles deliberately retain only the first four.
            aq_commands=[
                "review_submit", "review_show", "review_list", "review_withdraw",
                "review_decide", "review_comment", "review_delegate", "review_import_edits",
            ],
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
    await db.create_task(
        Task(
            id="peer",
            project_id="p",
            title="Peer author task",
            description="Write another document",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await db.create_task(
        Task(
            id="other-author",
            project_id="other",
            title="Other-project author task",
            description="Write another document",
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
        SessionRecord(
            id="peer-worker", task_id="peer", project_id="p", profile_id="worker", harness="codex",
            provider="fake", name="peer-worker", lifecycle="pool", work_dir=str(tmp_path), epoch="test",
            instance_token="peer-worker-token", started_at=now, state="running",
        ),
        SessionRecord(
            id="other-worker", task_id="other-author", project_id="other", profile_id="worker",
            harness="codex", provider="fake", name="other-worker", lifecycle="pool",
            work_dir=str(tmp_path), epoch="test", instance_token="other-worker-token",
            started_at=now, state="running",
        ),
    ):
        await db.create_session(session)
    try:
        yield handler, db
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
    assert "review." in _FORWARDED_PREFIXES

    other = await _scoped(
        handler,
        "review_submit",
        {"task_id": "someone-else", "kind": "spec", "title": "No", "content": "# No\n"},
        session_id="worker",
    )
    assert other.get("error_code") == "not_your_task" or "scope" in other["error"]
    listed = await _scoped(
        handler, "review_list", {"task_id": "someone-else"}, session_id="worker"
    )
    assert listed.get("error_code") == "not_your_task" or "scope" in listed["error"]


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


async def test_permission_matrix_for_local_supervisor_and_workers(env):
    handler, db = env

    # LOCAL has the entire surface, including the controls that are hidden
    # from every session principal.
    local = await handler.execute(
        "review_submit",
        {"task_id": "author", "kind": "spec", "title": "Local", "content": "# Local\n"},
    )
    review_id = local["review_id"]
    assert (await handler.execute(
        "review_comment", {"review_id": review_id, "revision": 1, "body": "Looks good."}
    ))["success"]
    assert (await handler.execute(
        "review_delegate", {"review_id": review_id, "to": "supervisor"}
    ))["success"]
    review = await db.get_review(review_id)
    vault_file = Path(handler.config.vault_root) / review["vault_path"]
    vault_file.write_text("# Local edit\n", encoding="utf-8")
    assert (await handler.execute("review_import_edits", {"review_id": review_id}))["success"]
    assert (await handler.execute(
        "review_withdraw", {"review_id": review_id, "reason": "use a new draft"}
    ))["success"]

    user_only = await handler.execute(
        "review_submit",
        {"project_id": "p", "kind": "plan", "title": "User only", "content": "# Plan\n"},
    )
    review_id = user_only["review_id"]
    assert (await _scoped(
        handler, "review_submit",
        {"project_id": "p", "kind": "other", "title": "Supervisor draft", "content": "# Draft\n"},
        session_id="supervisor",
    ))["success"]
    assert (await _scoped(
        handler, "review_withdraw", {"review_id": review_id, "reason": "not now"},
        session_id="supervisor",
    ))["success"]

    # A fresh user-only review proves that an elevated supervisor still
    # cannot decide or comment until the operator explicitly delegates it.
    user_only = await handler.execute(
        "review_submit",
        {"project_id": "p", "kind": "plan", "title": "User only again", "content": "# Plan\n"},
    )
    review_id = user_only["review_id"]
    for command, args in (
        ("review_decide", {"review_id": review_id, "revision": 1, "decision": "approve"}),
        ("review_comment", {"review_id": review_id, "revision": 1, "body": "Please revise."}),
    ):
        assert (await _scoped(handler, command, args, session_id="supervisor"))["error_code"] == "not_decider"
    for command, args in (
        ("review_delegate", {"review_id": review_id, "to": "supervisor"}),
        ("review_import_edits", {"review_id": review_id}),
    ):
        refused = await _scoped(handler, command, args, session_id="supervisor")
        assert "local operator" in refused["error"]

    assert (await handler.execute(
        "review_delegate", {"review_id": review_id, "to": "supervisor"}
    ))["success"]
    assert (await _scoped(
        handler, "review_comment", {"review_id": review_id, "revision": 1, "body": "Delegated."},
        session_id="supervisor",
    ))["success"]
    decided = await _scoped(
        handler, "review_decide", {"review_id": review_id, "revision": 1, "decision": "approve"},
        session_id="supervisor",
    )
    assert decided["success"]
    assert (await db.get_review(review_id))["decided_by"].startswith("supervisor (delegated)")

    stopped = await handler.execute(
        "review_submit",
        {"project_id": "p", "kind": "spec", "title": "Stopped", "content": "# Stopped\n"},
    )
    assert (await handler.execute(
        "review_delegate", {"review_id": stopped["review_id"], "to": "supervisor"}
    ))["success"]
    await db.update_session("supervisor", state="stopped")
    refused = await _scoped(
        handler, "review_decide",
        {"review_id": stopped["review_id"], "revision": 1, "decision": "approve"},
        session_id="supervisor",
    )
    assert refused["error_code"] == "not_decider"

    worker_review = await _scoped(
        handler, "review_submit",
        {"task_id": "author", "kind": "spec", "title": "Worker", "content": "# Worker\n"},
        session_id="worker",
    )
    assert worker_review["success"]
    assert (await _scoped(
        handler, "review_withdraw", {"review_id": worker_review["review_id"], "reason": "redo"},
        session_id="worker",
    ))["success"]
    for command, args in (
        ("review_decide", {"review_id": worker_review["review_id"], "revision": 1, "decision": "approve"}),
        ("review_delegate", {"review_id": worker_review["review_id"], "to": "supervisor"}),
        ("review_import_edits", {"review_id": worker_review["review_id"]}),
    ):
        assert "out of scope" in (await _scoped(handler, command, args, session_id="worker"))["error"]
    assert "scope" in (await _scoped(
        handler, "review_submit",
        {"task_id": "peer", "kind": "spec", "title": "Wrong task", "content": "# No\n"},
        session_id="worker",
    ))["error"]

    peer = await handler.execute(
        "review_submit",
        {"task_id": "peer", "kind": "spec", "title": "Peer", "content": "# Peer\n"},
    )
    assert (await _scoped(
        handler, "review_withdraw", {"review_id": peer["review_id"], "reason": "not mine"},
        session_id="worker",
    ))["error_code"] == "not_decider"

    # A worker in another project cannot inspect or mutate a p review.
    for command, args in (
        ("review_show", {"review_id": peer["review_id"]}),
        ("review_withdraw", {"review_id": peer["review_id"], "reason": "wrong project"}),
        ("review_decide", {"review_id": peer["review_id"], "revision": 1, "decision": "approve"}),
        ("review_comment", {"review_id": peer["review_id"], "revision": 1, "body": "Nope"}),
    ):
        refused = await _scoped(
            handler, command, args, session_id="other-worker", project_id="other"
        )
        assert "scope" in refused["error"] or "another project" in refused["error"]


async def test_changes_requested_routes_feedback_to_each_author_state(env):
    handler, db = env

    completed = await handler.execute(
        "review_submit",
        {"task_id": "author", "kind": "spec", "title": "Completed", "content": "# Completed\n"},
    )
    await db.transition_task("author", TaskStatus.COMPLETED, context="test")
    assert (await handler.execute(
        "review_decide",
        {"review_id": completed["review_id"], "revision": 1, "decision": "request_changes", "note": "Add detail."},
    ))["success"]
    author = await db.get_task("author")
    assert author.status is TaskStatus.READY
    contexts = await db.get_task_contexts("author")
    feedback = next(context["content"] for context in contexts if context["type"] == "reopen_feedback")
    assert "Add detail." in feedback
    assert author.description.endswith(feedback)

    in_progress = await handler.execute(
        "review_submit",
        {"task_id": "peer", "kind": "spec", "title": "In progress", "content": "# In progress\n"},
    )
    assert (await handler.execute(
        "review_decide",
        {"review_id": in_progress["review_id"], "revision": 1, "decision": "request_changes", "note": "Clarify it."},
    ))["success"]
    assert (await db.get_task("peer")).status is TaskStatus.IN_PROGRESS
    assert any("Clarify it." in c["body"] for c in (await db.list_task_comments("peer"))["comments"])

    deleted = await handler.execute(
        "review_submit",
        {"task_id": "other-author", "kind": "spec", "title": "Deleted", "content": "# Deleted\n"},
    )
    await db.delete_task("other-author")
    assert (await handler.execute(
        "review_decide",
        {"review_id": deleted["review_id"], "revision": 1, "decision": "request_changes", "note": "Restore this."},
    ))["success"]
    assert any(
        task.title == f"Revise Deleted (review {deleted['review_id']})"
        for task in await db.list_tasks(project_id="other")
    )

    no_author = await handler.execute(
        "review_submit",
        {"project_id": "p", "kind": "other", "title": "No author", "content": "# No author\n"},
    )
    assert (await handler.execute(
        "review_decide",
        {"review_id": no_author["review_id"], "revision": 1, "decision": "request_changes", "note": "Assign this."},
    ))["success"]
    messages = await db.get_pending_messages("session", "supervisor-p")
    assert any("Assign this." in message.body for message in messages)
