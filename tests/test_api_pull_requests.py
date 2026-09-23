"""The dashboard PR list uses GitHub state, not task status."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.auth import RequestScope
from src.api.pull_requests import build_pull_requests_router
from src.database import Database
from src.database.queries.pull_request_queries import list_known_pull_requests
from src.database.tables import archived_tasks, projects, tasks
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("pending-prs"))
    await database.initialize()
    yield database
    await database.close()


async def seed(db):
    async with db._engine.begin() as conn:
        await conn.execute(projects.insert(), [
            {"id": "active", "name": "Active project", "repo_url": "https://github.com/acme/repo", "status": "ACTIVE", "created_at": 1},
            {"id": "paused", "name": "Paused project", "repo_url": "https://github.com/acme/repo", "status": "PAUSED", "created_at": 1},
        ])
        await conn.execute(tasks.insert(), [
            {"id": "open-task", "project_id": "active", "title": "Open task", "description": "", "status": "COMPLETED", "pr_url": "https://github.com/acme/repo/pull/1", "created_at": 1, "updated_at": 1},
            {"id": "merged-task", "project_id": "active", "title": "Merged task", "description": "", "status": "DEFINED", "pr_url": "https://github.com/acme/repo/pull/2", "created_at": 1, "updated_at": 1},
            {"id": "closed-task", "project_id": "active", "title": "Closed task", "description": "", "status": "DEFINED", "pr_url": "https://github.com/acme/repo/pull/3", "created_at": 1, "updated_at": 1},
            {"id": "failed-lookup", "project_id": "active", "title": "Fallback title", "description": "", "status": "DEFINED", "pr_url": "https://github.com/acme/repo/pull/4", "created_at": 1, "updated_at": 1},
            {"id": "paused-task", "project_id": "paused", "title": "Paused", "description": "", "status": "DEFINED", "pr_url": "https://github.com/acme/repo/pull/5", "created_at": 1, "updated_at": 1},
            {"id": "no-pr", "project_id": "active", "title": "No PR", "description": "", "status": "DEFINED", "pr_url": None, "created_at": 1, "updated_at": 1},
        ])
        await conn.execute(archived_tasks.insert().values(
            id="archived-task", project_id="active", title="Archived",
            description="", status="COMPLETED", pr_url="https://github.com/acme/repo/pull/6",
            created_at=1, updated_at=1, archived_at=2,
        ))


async def test_candidates_include_completed_and_archived_but_only_active_projects(db):
    await seed(db)
    rows = await list_known_pull_requests(db)
    assert {row["task_id"] for row in rows} == {
        "open-task", "merged-task", "closed-task", "failed-lookup", "archived-task",
    }
    assert {row["repository_url"] for row in rows} == {"https://github.com/acme/repo"}


async def test_open_closed_merged_and_lookup_failure(db):
    await seed(db)
    calls = []

    async def lookup(repository_url, pr_url):
        calls.append((repository_url, pr_url))
        number = int(pr_url.rsplit("/", 1)[-1])
        if number == 4:
            raise RuntimeError("GitHub unavailable")
        return {
            "state": "closed" if number in {2, 3} else "open",
            "merged_at": "2026-09-20T00:00:00Z" if number == 2 else None,
            "title": f"PR {number}",
            "created_at": "2026-09-20T00:00:00Z",
        }

    app = FastAPI()
    app.include_router(build_pull_requests_router(db=db, lookup=lookup))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        response = await ac.get("/api/reviews/pull-requests")
        repeat = await ac.get("/api/reviews/pull-requests")

    assert response.status_code == 200
    assert repeat.json() == response.json()
    assert len(calls) == 5  # cached; no second GitHub pass
    rows = {row["task_id"]: row for row in response.json()["pull_requests"]}
    assert set(rows) == {"open-task", "failed-lookup", "archived-task"}
    assert rows["open-task"]["title"] == "PR 1"
    assert rows["open-task"]["state"] == "open"
    assert rows["open-task"]["opened_at"] is not None
    assert rows["failed-lookup"]["title"] == "Fallback title"
    assert rows["failed-lookup"]["state"] == "unknown"
    assert rows["failed-lookup"]["opened_at"] is None
    assert rows["archived-task"]["repository"] == "acme/repo"


async def test_task_scoped_session_cannot_read_cross_project_inbox(db):
    app = FastAPI()
    app.include_router(build_pull_requests_router(db=db, lookup=lambda *_: None))

    @app.middleware("http")
    async def scoped(request, call_next):
        request.state.scope = RequestScope(
            kind="session", session_id="worker", project_id="active", task_id="open-task"
        )
        return await call_next(request)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        response = await ac.get("/api/reviews/pull-requests")
    assert response.status_code == 403


async def test_invalid_stored_url_is_never_rendered(db):
    async with db._engine.begin() as conn:
        await conn.execute(projects.insert().values(
            id="active", name="Active project", repo_url="https://github.com/acme/repo",
            created_at=1,
        ))
        await conn.execute(tasks.insert().values(
            id="unsafe", project_id="active", title="Unsafe", description="",
            pr_url="javascript:alert(1)", created_at=1, updated_at=1,
        ))

    async def lookup(*_):
        pytest.fail("invalid URL must be rejected before GitHub lookup")

    app = FastAPI()
    app.include_router(build_pull_requests_router(db=db, lookup=lookup))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        response = await ac.get("/api/reviews/pull-requests")
    assert response.status_code == 200
    assert response.json() == {"pull_requests": []}
