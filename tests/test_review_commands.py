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
from src.vault import ensure_default_intelligence_classes
from src.intelligence_classes import load_intelligence_classes


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
    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes = load_intelligence_classes(handler.config.data_dir)
    await db.create_project(Project(id="p", name="Project P"))
    await db.create_project(Project(id="other", name="Other"))
    await db.create_profile(
        AgentProfile(
            id="worker",
            name="Worker",
            harness="codex",
            lifecycle="pool",
            default_class="standard-high",
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
    await db.update_project("p", default_profile_id="worker")
    await db.update_project("other", default_profile_id="worker")
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


async def test_delegated_supervisor_creates_revision_task_without_changing_author(env):
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
    assert not (await db.list_task_comments("author"))["comments"]
    revisions = [task for task in await db.list_tasks(project_id="p")
                 if task.title == f"Revise Plan (review {review_id})"]
    assert len(revisions) == 1
    assert "Tighten it." in revisions[0].description


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


async def test_dispatch_pins_revision_records_activity_and_preserves_gate(env):
    handler, db = env
    submitted = await handler.execute("review_submit", {
        "project_id": "p", "kind": "spec", "title": "Safe rollout", "content": "# Rollout\n",
    })
    review_id = submitted["review_id"]
    before = await db.get_review(review_id)
    dispatched = await handler.execute("review_dispatch", {
        "review_id": review_id, "to": ["worker"], "revision": 1,
        "with_comments": False, "focus": "migration rollback",
    })
    assert dispatched["success"], dispatched
    record = dispatched["dispatches"][0]
    task = await db.get_task(record["task_id"])
    assert task.profile_id == "worker"
    assert task.task_type.value == "research"
    assert f"aq review show --review-id {review_id} --revision 1`" in task.description
    assert "--comments" not in task.description
    assert "migration rollback" in task.description
    assert "ADVERSARIAL" in task.description
    assert record["with_comments"] is False
    assert (await db.get_task_meta(task.id, "review_dispatch"))["with_comments"] is False
    after = await db.get_review(review_id)
    assert (after["state"], after["decider"], after["gate_id"]) == (
        before["state"], before["decider"], before["gate_id"],
    )
    shown = await handler.execute("review_show", {"review_id": review_id})
    assert shown["dispatches"][0]["task_id"] == task.id
    assert shown["dispatches"][0]["task_state"] == task.status.value

    revised = await handler.execute("review_submit", {
        "review_id": review_id, "content": "# Revised rollout\n", "changes": "detail",
    })
    assert revised["revision"] == 2
    assert (await db.get_task(task.id)).description == task.description
    repeated = await handler.execute("review_dispatch", {
        "review_id": review_id, "to": ["worker"], "revision": 1,
    })
    assert repeated["error_code"] == "duplicate_dispatch"
    forced = await handler.execute("review_dispatch", {
        "review_id": review_id, "to": ["worker"], "revision": 1, "force": True,
    })
    assert forced["success"], forced
    assert forced["dispatches"][0]["task_id"] != task.id
    assert "--comments" in (await db.get_task(forced["dispatches"][0]["task_id"])).description
    assert len((await handler.execute("review_show", {"review_id": review_id}))["dispatches"]) == 2


async def test_dispatch_refusals_and_capacity_warning(env):
    handler, db = env
    submitted = await handler.execute("review_submit", {
        "project_id": "p", "kind": "plan", "title": "Plan", "content": "# Plan\n",
    })
    review_id = submitted["review_id"]
    unknown = await handler.execute("review_dispatch", {"review_id": review_id, "to": ["missing"]})
    assert unknown["error_code"] == "unknown_profile"
    await db.create_profile(AgentProfile(
        id="disabled-pool", name="Disabled", harness="codex", lifecycle="pool",
        enabled=False, aq_commands=["review_show"], harness_tools=[], plugin_tools=[],
    ))
    disabled = await handler.execute("review_dispatch", {
        "review_id": review_id, "to": ["disabled-pool"],
    })
    assert disabled["error_code"] == "disabled_profile"
    await db.create_profile(AgentProfile(
        id="no-review", name="No review", harness="codex", lifecycle="pool",
        aq_commands=[], harness_tools=[], plugin_tools=[],
    ))
    cannot_read = await handler.execute("review_dispatch", {
        "review_id": review_id, "to": ["no-review"],
    })
    assert cannot_read["error_code"] == "profile_cannot_review"
    await db.create_profile(AgentProfile(
        id="zero-pool", name="Zero", harness="codex", lifecycle="pool",
        max_active=0, aq_commands=["review_show"], harness_tools=[], plugin_tools=[],
    ))
    fanout = await handler.execute("review_dispatch", {
        "review_id": review_id, "to": ["worker", "zero-pool"],
    })
    assert fanout["success"], fanout
    assert len(fanout["dispatches"]) == 2
    assert fanout["dispatches"][1]["capacity_warning"] == "pool zero-pool has max_active=0"
    assert (await handler.execute("review_dispatch", {
        "review_id": review_id, "to": ["worker"],
    }))["error_code"] == "duplicate_dispatch"
    assert (await handler.execute("review_withdraw", {
        "review_id": review_id, "reason": "cancel",
    }))["success"]
    closed = await handler.execute("review_dispatch", {"review_id": review_id, "to": ["worker"]})
    assert closed["error_code"] == "review_closed"
    assert "withdrawn" in closed["error"]


async def test_only_held_dispatched_worker_can_comment_on_pinned_revision(env):
    handler, db = env
    submitted = await handler.execute("review_submit", {
        "task_id": "author", "kind": "spec", "title": "Read me", "content": "# Section\nClaim.\n",
    })
    review_id = submitted["review_id"]
    other = await handler.execute("review_submit", {
        "project_id": "p", "kind": "spec", "title": "Other", "content": "# Other\n",
    })
    task_id = (await handler.execute("review_dispatch", {
        "review_id": review_id, "to": ["worker"],
    }))["dispatches"][0]["task_id"]
    unassigned = await _scoped(handler, "review_comment", {
        "review_id": review_id, "revision": 1, "quote": "Claim.", "body": "Unsupported",
    }, session_id="worker")
    assert unassigned["error_code"] == "not_dispatched"
    await db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test", force=True)
    await db.update_session("peer-worker", task_id=task_id)
    wrong = await _scoped(handler, "review_comment", {
        "review_id": other["review_id"], "revision": 1, "heading_path": ["Other"], "body": "No",
    }, session_id="peer-worker", task_id=task_id)
    assert wrong["error_code"] == "not_dispatched"
    # A deployed vault profile may predate the new static grant. The task's
    # dispatch record still permits exactly this comment under enforcement.
    profile = await db.get_profile("worker")
    await db.update_profile("worker", aq_commands=[
        name for name in profile.aq_commands if name != "review_comment"
    ])
    handler.config.security.capability_enforcement = "enforce"
    unanchored = await _scoped(handler, "review_comment", {
        "review_id": review_id, "revision": 1, "body": "Needs an anchor",
    }, session_id="peer-worker", task_id=task_id)
    assert unanchored["error_code"] == "anchor_required"
    wrong_revision = await _scoped(handler, "review_comment", {
        "review_id": review_id, "revision": 2, "quote": "Claim.", "body": "Wrong revision",
    }, session_id="peer-worker", task_id=task_id)
    assert wrong_revision["error_code"] == "wrong_revision"
    finding = await _scoped(handler, "review_comment", {
        "review_id": review_id, "revision": 1, "quote": "Claim.", "body": "Unsupported claim",
    }, session_id="peer-worker", task_id=task_id)
    assert finding["success"], finding
    comments = await db.list_review_comments(review_id)
    assert comments[-1]["body"] == "Unsupported claim"
    assert "adversarial reviewer" in comments[-1]["author"]
    await db.update_profile("worker", aq_commands=profile.aq_commands)
    await db.update_session("peer-worker", last_claim_epoch=99)
    stale = await _scoped(handler, "review_comment", {
        "review_id": review_id, "revision": 1, "quote": "Claim.", "body": "Stale claim",
    }, session_id="peer-worker", task_id=task_id)
    assert stale["error_code"] == "not_dispatched"
    assert (await _scoped(handler, "review_decide", {
        "review_id": review_id, "revision": 1, "decision": "approve",
    }, session_id="peer-worker", task_id=task_id))["error"].startswith("out of scope")


def test_dispatch_cli_repeats_to_and_selects_clean_room(monkeypatch):
    from click.testing import CliRunner

    from src.cli.app import cli
    from src.cli import reviews

    captured = {}

    def execute(_ctx, command, params):
        captured.update(command=command, params=params)
        return {"success": True, "review_id": "rev-one", "dispatches": []}

    monkeypatch.setattr(reviews, "_execute", execute)
    result = CliRunner().invoke(cli, [
        "review", "dispatch", "--review-id", "rev-one", "--to", "astra-high-codex",
        "--to", "deep-high-claude", "--revision", "3", "--no-comments",
        "--focus", "security", "--force", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert captured == {
        "command": "review_dispatch",
        "params": {
            "review_id": "rev-one", "to": ["astra-high-codex", "deep-high-claude"],
            "revision": 3, "with_comments": False, "focus": "security", "force": True,
        },
    }


async def test_changes_requested_creates_one_revision_task_for_every_author_state(env):
    handler, db = env
    await db.create_profile(AgentProfile(
        id="fast-high-codex", name="Fast high", harness="codex", lifecycle="pool",
        default_class="fast-high", needs_workspace=False,
        aq_commands=["review_submit", "review_show"], harness_tools=[], plugin_tools=[],
    ))
    await db.create_task(Task(
        id="archived-author", project_id="p", title="Archived author",
        description="Original draft", status=TaskStatus.READY,
    ))
    archived_review = await handler.execute("review_submit", {
        "task_id": "archived-author", "kind": "spec", "title": "Archived", "content": "# Archived\n",
    })
    await db.transition_task("archived-author", TaskStatus.COMPLETED, context="test")
    assert await db.archive_task("archived-author")
    live_review = await handler.execute("review_submit", {
        "task_id": "author", "kind": "spec", "title": "Live", "content": "# Live\n",
    })
    supervisor_review = await handler.execute("review_submit", {
        "project_id": "p", "kind": "other", "title": "Supervisor", "content": "# Supervisor\n",
    })
    for submitted in (archived_review, live_review, supervisor_review):
        review_id = submitted["review_id"]
        decided = await handler.execute("review_decide", {
            "review_id": review_id, "revision": 1, "decision": "request_changes",
            "responder_class": "fast-high", "note": "Expand the reasoning.",
        })
        assert decided["success"], decided
        revisions = [task for task in await db.list_tasks(project_id="p")
                     if task.title.endswith(f"(review {review_id})")]
        assert len(revisions) == 1
        assert revisions[0].profile_id == "fast-high-codex"
        assert revisions[0].intelligence_class == "fast-high"
        assert revisions[0].parent_task_id is None
        assert (await db.get_task_meta(revisions[0].id, "review_response"))["profile_source"] == "explicit"
        repeated = await handler.execute("review_decide", {
            "review_id": review_id, "revision": 1, "decision": "request_changes",
            "responder_class": "fast-high",
        })
        assert not repeated["success"]
        assert len([task for task in await db.list_tasks(project_id="p")
                    if task.title.endswith(f"(review {review_id})")]) == 1
    assert (await db.get_task("author")).status is TaskStatus.IN_PROGRESS
    assert not (await db.list_task_comments("author"))["comments"]
    assert not await db.get_pending_messages("session", "supervisor-p")


async def test_response_routing_uses_decided_revision_class_and_project_default(env):
    handler, db = env
    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes = load_intelligence_classes(handler.config.data_dir)
    for profile_id, class_id in (
        ("fast-high-codex", "fast-high"),
        ("standard-high-codex", "standard-high"),
    ):
        await db.create_profile(AgentProfile(
            id=profile_id, name=profile_id, harness="codex", lifecycle="pool",
            default_class=class_id, needs_workspace=False,
            aq_commands=[], harness_tools=[], plugin_tools=[],
        ))
    await db.update_project("p", default_profile_id="fast-high-codex")

    chosen = await handler.execute("review_submit", {
        "task_id": "author", "kind": "spec", "title": "Chosen route", "content": "# Chosen\n",
    })
    # A retired class may linger in a stale live registry; the vault check wins.
    handler.orchestrator.intelligence_classes["retired-or-unknown"] = (
        handler.orchestrator.intelligence_classes["standard-high"]
    )
    invalid = await handler.execute("review_decide", {
        "review_id": chosen["review_id"], "revision": 1, "decision": "request_changes",
        "responder_class": "retired-or-unknown",
    })
    assert invalid["error_code"] == "invalid_responder_class"
    assert "fast-high" in invalid["error"] and "standard-high" in invalid["error"]
    assert (await db.get_review(chosen["review_id"]))["state"] == "in_review"

    await db.delete_task("author")
    decided = await handler.execute("review_decide", {
        "review_id": chosen["review_id"], "revision": 1, "decision": "request_changes",
        "responder_class": "standard-high", "note": "Use this class.",
    })
    assert decided["success"], decided
    revision = await db.get_review_revision(chosen["review_id"], 1)
    assert (revision["responder_class"], revision["responder_profile_source"]) == (
        "standard-high", "explicit",
    )
    response = next(task for task in await db.list_tasks(project_id="p")
                    if task.title.startswith("Revise Chosen route"))
    assert (response.profile_id, response.intelligence_class) == (
        "standard-high-codex", "standard-high",
    )
    shown = await handler.execute("review_show", {"review_id": chosen["review_id"]})
    assert shown["response_route"]["kind"] == "new_task"
    assert shown["response_route"]["profile_id"] == "standard-high-codex"
    assert shown["response_route"]["class_id"] == "standard-high"
    assert "new revision task on standard-high (explicit)" in shown["response_route"]["summary"]

    fallback = await handler.execute("review_submit", {
        "task_id": "peer", "kind": "spec", "title": "Default route", "content": "# Default\n",
    })
    await db.delete_task("peer")
    decided = await handler.execute("review_decide", {
        "review_id": fallback["review_id"], "revision": 1, "decision": "request_changes",
    })
    assert decided["success"], decided
    revision = await db.get_review_revision(fallback["review_id"], 1)
    assert revision["responder_class"] is None
    assert revision["responder_profile_source"] == "project_default"
    response = next(task for task in await db.list_tasks(project_id="p")
                    if task.title.startswith("Revise Default route"))
    assert response.profile_id == "fast-high-codex"
    assert response.intelligence_class == "fast-high"
    assert response.provider_intent == "class_only"
    assert (await db.get_task_meta(response.id, "review_response"))["profile_source"] == "project_default"
    assert (await db.get_project("p")).default_profile_id == "fast-high-codex"
    shown = await handler.execute("review_show", {"review_id": fallback["review_id"]})
    assert shown["response_route"]["profile_id"] == "fast-high-codex"
    assert shown["response_route"]["class_id"] == "fast-high"
    assert "project_default" in shown["response_route"]["summary"]


async def test_review_show_explains_both_decision_routes(env):
    handler, db = env
    with_author = await handler.execute("review_submit", {
        "task_id": "author", "kind": "spec", "title": "Active author", "content": "# Active\n",
    })
    shown = await handler.execute("review_show", {"review_id": with_author["review_id"]})
    assert shown["response_route"]["kind"] == "new_task"
    assert "Request changes → new revision task on standard-high (project_default)" in shown["response_route"]["summary"]
    assert "Approve → sent to the supervisor" in shown["response_route"]["summary"]

    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes = load_intelligence_classes(handler.config.data_dir)
    await db.create_profile(AgentProfile(
        id="standard-high-codex", name="Standard high", harness="codex", lifecycle="pool",
        default_class="standard-high", needs_workspace=False,
        aq_commands=[], harness_tools=[], plugin_tools=[],
    ))
    decided = await handler.execute("review_decide", {
        "review_id": with_author["review_id"], "revision": 1, "decision": "request_changes",
        "responder_class": "standard-high", "responder_profile": "standard-high-codex",
    })
    assert decided["success"], decided
    shown = await handler.execute("review_show", {"review_id": with_author["review_id"]})
    assert shown["response_route"]["kind"] == "new_task"
    assert "new revision task on standard-high (explicit)" in shown["response_route"]["summary"]

    without_author = await handler.execute("review_submit", {
        "project_id": "p", "kind": "spec", "title": "No author", "content": "# No author\n",
    })
    shown = await handler.execute("review_show", {"review_id": without_author["review_id"]})
    assert shown["response_route"]["kind"] == "new_task"
    assert "new revision task on standard-high (project_default)" in shown["response_route"]["summary"]


async def test_response_routing_accepts_explicit_matching_profile(env):
    handler, db = env
    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes = load_intelligence_classes(handler.config.data_dir)
    await db.create_profile(AgentProfile(
        id="standard-high-codex", name="Standard high", harness="codex", lifecycle="pool",
        default_class="standard-high", needs_workspace=False,
        aq_commands=[], harness_tools=[], plugin_tools=[],
    ))
    submitted = await handler.execute("review_submit", {
        "task_id": "author", "kind": "spec", "title": "Explicit route", "content": "# Explicit\n",
    })
    mismatch = await handler.execute("review_decide", {
        "review_id": submitted["review_id"], "revision": 1, "decision": "request_changes",
        "responder_class": "fast-high", "responder_profile": "standard-high-codex",
    })
    assert mismatch["error_code"] == "invalid_responder_profile"
    assert (await db.get_review(submitted["review_id"]))["state"] == "in_review"
    await db.delete_task("author")
    await db.create_profile(AgentProfile(
        id="disabled-worker", name="Disabled", harness="codex", lifecycle="pool",
        default_class="standard-high", enabled=False, needs_workspace=False,
        aq_commands=[], harness_tools=[], plugin_tools=[],
    ))
    await db.update_project("p", default_profile_id="disabled-worker")
    decided = await handler.execute("review_decide", {
        "review_id": submitted["review_id"], "revision": 1, "decision": "request_changes",
        "responder_class": "standard-high", "responder_profile": "standard-high-codex",
    })
    assert decided["success"], decided
    revision = await db.get_review_revision(submitted["review_id"], 1)
    assert revision["responder_profile"] == "standard-high-codex"
    assert revision["responder_profile_source"] == "explicit"
    response = next(task for task in await db.list_tasks(project_id="p")
                    if task.title.startswith("Revise Explicit route"))
    assert response.profile_id == "standard-high-codex"
    assert response.provider_intent == "preferred"
    shown = await handler.execute("review_show", {"review_id": submitted["review_id"]})
    assert "new revision task on standard-high (explicit)" in shown["response_route"]["summary"]


async def test_revision_task_contains_comments_and_can_resubmit(env):
    handler, db = env
    submitted = await handler.execute("review_submit", {
        "task_id": "author", "kind": "spec", "title": "Commented draft", "content": "# Draft\n",
    })
    review_id = submitted["review_id"]
    first = await handler.execute("review_comment", {
        "review_id": review_id, "revision": 1, "quote": "Draft",
        "heading_path": ["Introduction"], "body": "Explain the motivation.",
    })
    second = await handler.execute("review_comment", {
        "review_id": review_id, "revision": 1,
        "heading_path": ["Appendix"], "body": "Add a source.",
    })
    assert first["success"] and second["success"]
    assert (await handler.execute("review_decide", {
        "review_id": review_id, "revision": 1, "decision": "request_changes",
        "note": "Rework the opening and cite the claim.",
    }))["success"]
    task = next(task for task in await db.list_tasks(project_id="p")
                if task.title == f"Revise Commented draft (review {review_id})")
    assert "Rework the opening and cite the claim." in task.description
    for comment, anchor in ((first, "Introduction — quote: Draft"), (second, "Appendix")):
        assert comment["comment_id"] in task.description
        assert anchor in task.description
    assert "aq review submit --review-id" in task.description
    assert "--resolves" in task.description
    assert (await db.get_task_meta(task.id, "review_response")) == {
        "review_id": review_id, "revision": 1, "profile_source": "project_default",
    }
    refused = await _scoped(handler, "review_submit", {
        "review_id": review_id, "content": "# Wrong worker\n",
    }, session_id="worker")
    assert refused["error_code"] == "not_your_task"
    await db.update_session("worker", task_id=task.id)
    revised = await _scoped(handler, "review_submit", {
        "review_id": review_id, "content": "# Better draft\n", "changes": "Expanded the motivation",
        "resolves": [first["comment_id"]],
    }, session_id="worker", task_id=task.id)
    assert revised["success"], revised
    assert revised["revision"] == 2
    assert (await db.get_review_revision(review_id, 2))["submitted_task_id"] == task.id
    comments = {comment["id"]: comment for comment in await db.list_review_comments(review_id)}
    assert comments[first["comment_id"]]["resolved_in_revision"] == 2
    assert comments[second["comment_id"]]["resolved_in_revision"] is None
    assert (await handler.execute("review_decide", {
        "review_id": review_id, "revision": 2, "decision": "request_changes",
        "note": "Finish the citation.",
    }))["success"]
    next_task = next(task for task in await db.list_tasks(project_id="p")
                     if task.dedup_key == f"review-revision:{review_id}:2")
    assert second["comment_id"] in next_task.description
    assert first["comment_id"] not in next_task.description


async def test_revision_task_uses_authors_parent(env):
    handler, db = env
    await db.create_task(Task(
        id="container", project_id="p", title="Container", description="Group",
        status=TaskStatus.READY,
    ))
    async with db.immediate() as conn:
        await db.set_parent("author", "container", conn=conn)
    submitted = await handler.execute("review_submit", {
        "task_id": "author", "kind": "plan", "title": "Grouped plan", "content": "# Plan\n",
    })
    assert (await handler.execute("review_decide", {
        "review_id": submitted["review_id"], "revision": 1, "decision": "request_changes",
    }))["success"]
    revision = next(task for task in await db.list_tasks(project_id="p")
                    if task.title == f"Revise Grouped plan (review {submitted['review_id']})")
    assert revision.parent_task_id == "container"


async def test_approval_notifies_only_supervisor_once_for_each_author_state(env):
    handler, db = env
    await db.create_task(Task(
        id="archived-author", project_id="p", title="Archived author",
        description="Original draft", status=TaskStatus.READY,
    ))
    reviews = [
        await handler.execute("review_submit", {
            "project_id": "p", "kind": "spec", "title": "Supervisor draft", "content": "# Draft\n",
        }),
        await handler.execute("review_submit", {
            "task_id": "author", "kind": "plan", "title": "Live draft", "content": "# Draft\n",
        }),
        await handler.execute("review_submit", {
            "task_id": "archived-author", "kind": "other", "title": "Archived draft", "content": "# Draft\n",
        }),
    ]
    await db.transition_task("archived-author", TaskStatus.COMPLETED, context="test")
    assert await db.archive_task("archived-author")
    for submitted, kind in zip(reviews, ("spec", "plan", "other"), strict=True):
        review_id = submitted["review_id"]
        review = await db.get_review(review_id)
        assert (await handler.execute("review_decide", {
            "review_id": review_id, "revision": 1, "decision": "approve",
        }))["success"]
        repeated = await handler.execute("review_decide", {
            "review_id": review_id, "revision": 1, "decision": "approve",
        })
        assert not repeated["success"]
        messages = [message for message in await db.get_pending_messages("session", "supervisor-p")
                    if review_id in message.body]
        assert len(messages) == 1
        assert kind in messages[0].body
        assert review["vault_path"] in messages[0].body
