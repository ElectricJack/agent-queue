"""Comments and description writes across real authenticated HTTP surfaces."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.api import dependencies as deps
from src.api.auth import SessionTokenStore
from src.api.codegen import build_category_routers
from src.api.execute import router as execute_router
from src.api.middleware import TokenAuthMiddleware
from tests.test_task_comments import env  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def task_routers():
    return [r for r in build_category_routers() if r.prefix == "/api/task"]


@pytest.fixture(params=["execute", "typed"])
async def api(env, monkeypatch, request, task_routers):  # noqa: F811 - imported pytest fixture
    store = SessionTokenStore(env.db)
    tokens = {}
    for name, sid, tid, pid, elevated in (
        ("worker", "session", None, "p", False),
        ("pinned", "session", "t", "p", False),
        ("supervisor", "supervisor", None, "p", True),
        ("global", "supervisor-global", None, None, True),
        ("unassigned", "interactive", None, None, False),
    ):
        tokens[name] = await store.mint(
            session_id=sid, task_id=tid, project_id=pid, elevated=elevated
        )
    monkeypatch.setattr(deps, "_command_handler", env.handler)
    monkeypatch.setattr(deps, "_orchestrator", env.orch)
    monkeypatch.setattr(deps, "_token_store", store)
    monkeypatch.setattr(deps, "_require_session_token", False)
    app = FastAPI()
    app.include_router(execute_router)
    paths = {}
    for router in task_routers:
        app.include_router(router)
        for route in router.routes:
            paths[route.operation_id] = route.path
    app.add_middleware(TokenAuthMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def post(command, args, *, caller="worker"):
            headers = {"Authorization": f"Bearer {tokens[caller]}"} if caller != "local" else {}
            if request.param == "execute":
                response = await client.post(
                    "/api/execute", json={"command": command, "args": args}, headers=headers
                )
            else:
                response = await client.post(paths[command], json=args, headers=headers)
            data = response.json()
            ok = response.status_code == 200 and (
                data.get("ok", True) if request.param == "execute" else True
            )
            return ok, data.get("result", data) if ok else data

        yield SimpleNamespace(post=post, env=env, mode=request.param)


async def test_comments_http_authors_and_page_shape(api):
    for caller, kind, author in (
        ("local", "user", "local"),
        ("worker", "agent", "agent"),
        ("supervisor", "supervisor", "supervisor"),
    ):
        ok, data = await api.post(
            "task_comment", {"task_id": "t", "body": caller, "claim_epoch": 7}, caller=caller
        )
        assert ok, data
        assert data["comment"]["author_kind"] == kind
        assert data["comment"]["author_id"] == author
        assert data["comment"]["created_at"] > 0
    ok, data = await api.post("task_comments", {"task_id": "t", "limit": 2, "offset": 1})
    assert ok, data
    assert data["total"] == 3 and data["limit"] == 2 and data["offset"] == 1
    assert [c["body"] for c in data["comments"]] == ["worker", "local"]


async def test_comments_http_scope_and_claim_fences(api):
    for caller, task_id in (
        ("worker", "foreign"),
        ("supervisor", "foreign"),
        ("pinned", "peer"),
        ("unassigned", "t"),
    ):
        for command, extra in (
            ("task_comment", {"body": "No", "claim_epoch": 7}),
            ("task_comments", {}),
            ("task_set", {"description": "No", "claim_epoch": 7}),
        ):
            ok, data = await api.post(command, {"task_id": task_id, **extra}, caller=caller)
            assert not ok, (caller, command, data)
    for args in ({"body": "No"}, {"body": "No", "claim_epoch": 6}):
        ok, data = await api.post("task_comment", {"task_id": "t", **args})
        assert not ok, data
    ok, data = await api.post(
        "task_comment", {"task_id": "foreign", "body": "Global"}, caller="global"
    )
    assert ok, data
    assert data["comment"]["author_id"] == "supervisor-global"


async def test_comments_http_cannot_forge_scope_or_identity(api):
    ok, data = await api.post(
        "task_comment",
        {
            "task_id": "peer",
            "body": "Forged",
            "claim_epoch": 7,
            "_scope": {"kind": "local", "elevated": True},
        },
    )
    assert not ok, data
    ok, data = await api.post(
        "task_comment",
        {
            "task_id": "t",
            "body": "Forged",
            "claim_epoch": 7,
            "author_id": "local",
            "author_kind": "user",
            "created_at": 1,
        },
    )
    # The typed request model may discard unknown fields. It must never
    # persist forged attribution even if that request otherwise succeeds.
    if ok:
        assert data["comment"]["author_id"] == "agent"
        assert data["comment"]["author_kind"] == "agent"
        assert data["comment"]["created_at"] != 1


async def test_description_http_cas_and_null_validation(api):
    ok, data = await api.post(
        "task_set",
        {
            "task_id": "t",
            "description": "Findings",
            "expected_description": "Original requirements",
            "claim_epoch": 7,
        },
    )
    assert ok, data
    assert data["description"] == "Findings" and "description" in data["fields_changed"]
    for extra in (
        {"description": "Stale", "expected_description": "Original requirements"},
        {"description": None},
        {"description": "Invalid", "expected_description": None},
        {"expected_description": "Findings"},
    ):
        ok, data = await api.post(
            "task_set", {"task_id": "t", "branch": "bad", "claim_epoch": 7, **extra}
        )
        assert not ok, data
        assert (await api.env.db.get_task("t")).description == "Findings"
        assert (await api.env.db.get_task("t")).branch_name is None


async def test_collision_http_reads_and_feedback_stay_in_active_project(api):
    from tests.test_task_comments import seed_cross_project_collision

    await seed_cross_project_collision(api.env)
    ok, data = await api.post("task_comments", {"task_id": "peer"}, caller="local")
    assert ok, data
    assert data["comments"] == [] and data["total"] == 0
    ok, data = await api.post("task_comment", {"task_id": "peer", "body": "Feedback"}, caller="local")
    assert ok, data
    ok, data = await api.post("task_comments", {"task_id": "peer"}, caller="local")
    assert ok and [c["body"] for c in data["comments"]] == ["Feedback"], data
    ok, data = await api.post("task_comments", {"task_id": "peer"}, caller="supervisor")
    assert not ok, data
