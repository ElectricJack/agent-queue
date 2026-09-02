"""A reviewer token can execute its documented reject path, and nothing more.

The reviewer profile documents exactly two verdicts: approve (``task_close``
on its own review task) and reject (``reopen_with_feedback`` on the *reviewed*
task, then ``task_close``).  The reject half needs a command outside
``AGENT_COMMAND_SET`` aimed at a task other than the token's own — so it needs
a verified carve-out, shaped like the triage one but scoped to the single task
this review was spawned for rather than to the project's whole queue.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.api import dependencies as deps
from src.api.auth import SessionTokenStore
from src.api.codegen import build_category_routers
from src.api.execute import router as execute_router
from src.api.middleware import TokenAuthMiddleware
from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Agent, AgentProfile, AgentState, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.vault import ensure_default_intelligence_classes

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def generated_routers():
    return build_category_routers()


@pytest.fixture(params=["execute", "typed"])
async def api(tmp_path, monkeypatch, request, generated_routers):
    db = Database(str(tmp_path / "reviewer-auth.db"))
    await db.initialize()
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        database_path=str(tmp_path / "reviewer-auth.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=data_dir,
    )
    orch = Orchestrator(config)
    orch.db = db
    handler = CommandHandler(orch, config)
    for pid in ("p", "other"):
        await db.create_project(Project(id=pid, name=pid))
    for profile_id in ("reviewer", "coder"):
        await db.upsert_profile(AgentProfile(
            id=profile_id, name=profile_id, harness="codex", needs_workspace=False,
            default_class="standard-low",
        ))
    # The reviewed work, plus an unrelated completed task in the same project
    # and one in a second project.  All three are reopenable by construction —
    # only scope may separate them.
    for tid, pid in (("reviewed", "p"), ("unrelated", "p"), ("foreign", "other")):
        await db.create_task(Task(
            id=tid, project_id=pid, title=tid, description="Worker output",
            status=TaskStatus.DEFINED, profile_id="coder", branch_name=f"feature/{tid}",
        ))
        await db.transition_task(tid, TaskStatus.COMPLETED, context="test")

    for worker, role, description in (
        ("reviewer-agent", "reviewer", "Reviewing task: reviewed\nBranch: feature/reviewed"),
        ("worker-agent", "coder", "Assigned work"),
    ):
        await db.create_agent(Agent(id=worker, name=worker, profile_id=role))
        await db.create_task(Task(
            id=f"{worker}-job", project_id="p", title=worker, description=description,
            status=TaskStatus.IN_PROGRESS, profile_id=role, assigned_agent_id=worker,
        ))
        await db.update_agent(worker, state=AgentState.BUSY, current_task_id=f"{worker}-job")
        await db.create_session(SessionRecord(
            id=f"s-{worker}", task_id=f"{worker}-job", project_id="p",
            agent_id=worker, profile_id=role, harness="codex", provider="fake",
            name=f"s-{worker}", lifecycle="task", state="running",
            work_dir=str(tmp_path), epoch="test", instance_token=f"instance-{worker}",
            started_at=time.time(), last_claim_epoch=0,
        ))
    # The provenance edge the ``per-task-review`` pipeline rule writes.
    await db.add_dependency("reviewer-agent-job", "reviewed", "discovered-from")

    store = SessionTokenStore(db)
    tokens = {
        worker: await store.mint(
            session_id=f"s-{worker}", task_id=f"{worker}-job", project_id="p",
        )
        for worker in ("reviewer-agent", "worker-agent")
    }
    monkeypatch.setattr(deps, "_command_handler", handler)
    monkeypatch.setattr(deps, "_orchestrator", orch)
    monkeypatch.setattr(deps, "_token_store", store)
    monkeypatch.setattr(deps, "_require_session_token", True)
    app = FastAPI()
    app.include_router(execute_router)
    paths = {}
    for router in generated_routers:
        app.include_router(router)
        for route in router.routes:
            paths[route.operation_id] = route.path
    app.add_middleware(TokenAuthMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        async def post(command, args=None, *, worker="reviewer-agent"):
            headers = {"Authorization": f"Bearer {tokens[worker]}"}
            if request.param == "execute":
                return await client.post(
                    "/api/execute", headers=headers,
                    json={"command": command, "args": args or {}},
                )
            return await client.post(paths[command], headers=headers, json=args or {})

        def result(response):
            assert response.status_code == 200, response.text
            payload = response.json()
            if request.param == "execute":
                assert payload["ok"], payload
                return payload["result"]
            return payload

        yield SimpleNamespace(db=db, store=store, tokens=tokens, post=post, result=result)
    await db.close()


# --- the grant ------------------------------------------------------------


async def test_reviewer_rejects_the_task_it_is_reviewing(api):
    result = api.result(await api.post("reopen_with_feedback", {
        "task_id": "reviewed", "feedback": "Rename the flag and add a regression test.",
    }))
    assert result["reopened"] == "reviewed"
    reviewed = await api.db.get_task("reviewed")
    assert reviewed.status == TaskStatus.READY
    assert "Rename the flag" in reviewed.description


async def test_rejecting_does_not_cancel_the_rejecting_review_itself(api):
    """The reject path ends in ``task_close`` on the review task, so the
    reopen cascade must not fail the review that produced the verdict."""
    api.result(await api.post("reopen_with_feedback", {
        "task_id": "reviewed", "feedback": "Needs work.",
    }))
    assert (await api.db.get_task("reviewer-agent-job")).status == TaskStatus.IN_PROGRESS
    api.result(await api.post("task_close", {
        "task_id": "reviewer-agent-job", "outcome": "pass",
        "summary": "rejected — reopened reviewed with feedback",
    }))


@pytest.mark.parametrize("command", ["get_task", "task_show"])
async def test_reviewer_reads_the_reviewed_task(api, command):
    result = api.result(await api.post(command, {"task_id": "reviewed"}))
    assert result["id"] == "reviewed"
    assert result["project_id"] == "p"


async def test_reviewer_annotates_the_reviewed_task(api):
    result = api.result(await api.post("task_comment", {
        "task_id": "reviewed", "body": "Reviewed the diff; flag name is misleading.",
    }))
    assert result["comment"]["author_kind"] == "agent"
    assert result["comment"]["author_id"] == "reviewer-agent"
    listed = api.result(await api.post("task_comments", {"task_id": "reviewed"}))
    assert [c["body"] for c in listed["comments"]] == [
        "Reviewed the diff; flag name is misleading."
    ]


async def test_reviewer_keeps_ordinary_access_to_its_own_review_task(api):
    result = api.result(await api.post("task_show", {"task_id": "reviewer-agent-job"}))
    assert result["id"] == "reviewer-agent-job"
    api.result(await api.post("task_comment", {
        "task_id": "reviewer-agent-job", "body": "Starting the review.",
    }))


# --- the refusals ---------------------------------------------------------


@pytest.mark.parametrize("command,args", [
    ("reopen_with_feedback", {"task_id": "unrelated", "feedback": "not mine"}),
    ("reopen_with_feedback", {"task_id": "foreign", "feedback": "not mine"}),
    ("task_show", {"task_id": "unrelated"}),
    ("get_task", {"task_id": "unrelated"}),
    ("task_comment", {"task_id": "unrelated", "body": "not mine"}),
    ("task_comments", {"task_id": "unrelated"}),
])
async def test_reviewer_cannot_reach_a_task_it_is_not_reviewing(api, command, args):
    response = await api.post(command, args)
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("unrelated")).status == TaskStatus.COMPLETED
    assert (await api.db.get_task("foreign")).status == TaskStatus.COMPLETED


@pytest.mark.parametrize("target", ["reviewed", "unrelated"])
async def test_plain_worker_cannot_reopen_anything(api, target):
    response = await api.post(
        "reopen_with_feedback", {"task_id": target, "feedback": "let me through"},
        worker="worker-agent",
    )
    assert response.status_code == 403, response.text
    assert response.text.count("out of scope") == 1
    assert (await api.db.get_task(target)).status == TaskStatus.COMPLETED


async def test_worker_cannot_impersonate_a_reviewer_through_request_fields(api):
    response = await api.post("reopen_with_feedback", {
        "task_id": "reviewed", "feedback": "let me through",
        "session_id": "s-reviewer-agent",
        "_scope": {"kind": "local", "elevated": True, "session_id": "s-reviewer-agent"},
    }, worker="worker-agent")
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("reviewed")).status == TaskStatus.COMPLETED


@pytest.mark.parametrize(
    "change",
    ["stopped", "sleeping", "wrong-session-profile", "wrong-task-profile",
     "wrong-agent", "closed-review", "idle-agent", "stale-epoch", "no-edge"],
)
async def test_stale_or_changed_assignment_loses_reviewer_capabilities(api, change):
    if change in {"stopped", "sleeping"}:
        await api.db.update_session("s-reviewer-agent", state=change)
    elif change == "wrong-session-profile":
        await api.db.update_session("s-reviewer-agent", profile_id="coder")
    elif change == "wrong-task-profile":
        await api.db.update_task("reviewer-agent-job", profile_id="coder")
    elif change == "wrong-agent":
        await api.db.update_task("reviewer-agent-job", assigned_agent_id="worker-agent")
    elif change == "closed-review":
        await api.db.update_task("reviewer-agent-job", status=TaskStatus.COMPLETED)
    elif change == "idle-agent":
        await api.db.update_agent(
            "reviewer-agent", state=AgentState.IDLE, current_task_id=None,
        )
    elif change == "stale-epoch":
        await api.db.update_task("reviewer-agent-job", claim_epoch=7)
    else:
        await api.db.remove_dependency("reviewer-agent-job", "reviewed")
    response = await api.post(
        "reopen_with_feedback", {"task_id": "reviewed", "feedback": "stale"},
    )
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("reviewed")).status == TaskStatus.COMPLETED


async def test_rewriting_its_own_description_cannot_widen_a_reviewer_s_reach(api):
    """``task_set`` is a reviewer's own-task capability, so the description is
    agent-controlled: it may only narrow the authoritative provenance edges."""
    api.result(await api.post("task_set", {
        "task_id": "reviewer-agent-job",
        "description": "Reviewing task: unrelated\nBranch: feature/unrelated",
    }))
    response = await api.post(
        "reopen_with_feedback", {"task_id": "unrelated", "feedback": "widened"},
    )
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("unrelated")).status == TaskStatus.COMPLETED
    # The edge still points at the real reviewed task, which stays reachable.
    api.result(await api.post("task_show", {"task_id": "reviewed"}))


async def test_reviewer_does_not_gain_operator_commands(api):
    for command, args in (
        ("list_tasks", {}),
        ("delete_task", {"task_id": "unrelated"}),
        ("restart_task", {"task_id": "unrelated"}),
        ("pr_merge", {"project_id": "p", "pr_url": "https://example.invalid/pr/1"}),
    ):
        response = await api.post(command, args)
        assert response.status_code == 403, (command, response.text)
    assert await api.db.get_task("unrelated") is not None


async def test_typed_and_execute_routes_refuse_identically(api):
    cases = [
        ("reopen_with_feedback", {"task_id": "unrelated", "feedback": "no"},
         "out of scope: a reviewer may only act on the task it is reviewing"),
        ("task_show", {"task_id": "unrelated"},
         "out of scope: a reviewer may only act on the task it is reviewing"),
        ("task_close", {"task_id": "reviewed", "outcome": "pass"},
         "out of scope: task_id mismatch"),
    ]
    for command, args, expected in cases:
        response = await api.post(command, args)
        assert response.status_code == 403, response.text
        payload = response.json()
        assert payload.get("error") == expected or payload.get("detail") == expected, payload
