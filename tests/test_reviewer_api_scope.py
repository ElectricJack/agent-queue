"""Reviewer sessions can inspect and reject only their reviewed task."""

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

    for project_id in ("p", "other"):
        await db.create_project(Project(id=project_id, name=project_id))
    for profile_id in ("reviewer", "worker"):
        await db.upsert_profile(AgentProfile(
            id=profile_id,
            name=profile_id,
            harness="codex",
            needs_workspace=False,
        ))

    await db.create_agent(Agent(id="reviewer-agent", name="reviewer", profile_id="reviewer"))
    await db.create_task(Task(
        id="reviewed",
        project_id="p",
        title="Reviewed work",
        description="Implementation to review",
        status=TaskStatus.COMPLETED,
        profile_id="worker",
    ))
    await db.create_task(Task(
        id="unrelated",
        project_id="p",
        title="Unrelated work",
        description="Must remain inaccessible",
        status=TaskStatus.COMPLETED,
        profile_id="worker",
    ))
    await db.create_task(Task(
        id="foreign",
        project_id="other",
        title="Foreign work",
        description="Must remain inaccessible",
        status=TaskStatus.COMPLETED,
        profile_id="worker",
    ))
    await db.create_task(Task(
        id="review-job",
        project_id="p",
        title="Review: Reviewed work",
        description="Reviewing task: reviewed\nBranch: feature/reviewed",
        status=TaskStatus.IN_PROGRESS,
        profile_id="reviewer",
        assigned_agent_id="reviewer-agent",
    ))
    await db.add_dependency("review-job", "reviewed", "discovered-from")
    await db.update_agent(
        "reviewer-agent", state=AgentState.BUSY, current_task_id="review-job",
    )
    await db.create_session(SessionRecord(
        id="s-reviewer",
        task_id="review-job",
        project_id="p",
        agent_id="reviewer-agent",
        profile_id="reviewer",
        harness="codex",
        provider="fake",
        name="s-reviewer",
        lifecycle="task",
        state="running",
        work_dir=str(tmp_path),
        epoch="test",
        instance_token="instance-reviewer",
        started_at=time.time(),
        last_claim_epoch=0,
    ))
    await db.add_task_comment(
        "reviewed",
        "Evidence from the worker",
        author_kind="agent",
        author_id="worker-agent",
    )

    store = SessionTokenStore(db)
    token = await store.mint(
        session_id="s-reviewer", task_id="review-job", project_id="p",
    )
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
        async def post(command, args=None):
            headers = {"Authorization": f"Bearer {token}"}
            if request.param == "execute":
                return await client.post(
                    "/api/execute",
                    headers=headers,
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

        yield SimpleNamespace(db=db, post=post, result=result)
    await db.close()


@pytest.mark.parametrize("command", ["get_task", "task_show"])
async def test_reviewer_can_read_the_reviewed_task(api, command):
    result = api.result(await api.post(command, {"task_id": "reviewed"}))
    assert result["id"] == "reviewed"
    assert result["project_id"] == "p"


async def test_reviewer_can_read_the_reviewed_task_comments(api):
    result = api.result(await api.post("task_comments", {"task_id": "reviewed"}))
    assert result["total"] == 1
    assert result["comments"][0]["body"] == "Evidence from the worker"


async def test_reviewer_can_reopen_the_reviewed_task_with_feedback(api):
    result = api.result(await api.post("reopen_with_feedback", {
        "task_id": "reviewed",
        "feedback": "Add a regression test for the failure path.",
    }))
    assert result["reopened"] == "reviewed"
    task = await api.db.get_task("reviewed")
    assert task.status == TaskStatus.READY
    assert "Add a regression test for the failure path." in task.description


@pytest.mark.parametrize("command,args", [
    ("get_task", {"task_id": "unrelated"}),
    ("task_show", {"task_id": "unrelated"}),
    ("task_comments", {"task_id": "unrelated"}),
    ("reopen_with_feedback", {"task_id": "unrelated", "feedback": "Not authorized"}),
    ("get_task", {"task_id": "foreign"}),
])
async def test_reviewer_cannot_access_any_other_task(api, command, args):
    response = await api.post(command, args)
    assert response.status_code == 403, response.text
    assert "out of scope" in response.json()["error"]
    assert (await api.db.get_task("unrelated")).status == TaskStatus.COMPLETED


async def test_stale_reviewer_session_loses_reviewed_task_access(api):
    await api.db.update_session("s-reviewer", state="stopped")
    response = await api.post("task_show", {"task_id": "reviewed"})
    assert response.status_code == 403, response.text
    assert "out of scope" in response.json()["error"]


async def test_non_reviewer_session_cannot_impersonate_reviewer(api):
    await api.db.update_session("s-reviewer", profile_id="worker")
    response = await api.post("reopen_with_feedback", {
        "task_id": "reviewed", "feedback": "Spoofed rejection",
    })
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("reviewed")).status == TaskStatus.COMPLETED


async def test_ambiguous_review_provenance_fails_closed(api):
    await api.db.add_dependency("review-job", "unrelated", "discovered-from")
    response = await api.post("task_show", {"task_id": "reviewed"})
    assert response.status_code == 403, response.text
