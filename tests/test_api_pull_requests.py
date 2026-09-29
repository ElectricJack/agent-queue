"""The Reviews inbox reads a durable snapshot and pins human approvals."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.auth import RequestScope
from src.api.middleware import TokenAuthMiddleware
from src.api.pull_requests import PendingPullRequestInbox, build_pull_requests_router
from src.commands.review_commands import ReviewCommandsMixin
from src.database import Database
from src.database.queries.pull_request_queries import (
    list_known_pull_requests, read_pull_request_snapshot,
)
from src.database.tables import archived_tasks, projects, tasks
from src.git.github_contracts import GitHubRepositoryBinding
from tests.db_fixtures import lease_dsn

SHA = "a" * 40
MOVED = "b" * 40
BASE = "https://github.com/acme/repo/pull/"


@pytest.fixture
async def db():
    database = Database(lease_dsn("pending-prs"))
    await database.initialize()
    yield database
    await database.close()


async def seed(db):
    async with db._engine.begin() as conn:
        await conn.execute(projects.insert(), [
            {"id": "active", "name": "Active project", "repo_url": "https://github.com/acme/repo",
             "status": "ACTIVE", "created_at": 1, "hierarchical_integration_mode": "train"},
            {"id": "paused", "name": "Paused project", "repo_url": "https://github.com/acme/repo",
             "status": "PAUSED", "created_at": 1, "hierarchical_integration_mode": "disabled"},
        ])
        await conn.execute(tasks.insert(), [
            {"id": "open-task", "project_id": "active", "title": "Open task",
             "description": "", "status": "COMPLETED", "pr_url": BASE + "1",
             "created_at": 1, "updated_at": 1},
            {"id": "closed-task", "project_id": "active", "title": "Closed task",
             "description": "", "status": "COMPLETED", "pr_url": BASE + "2",
             "created_at": 1, "updated_at": 1},
            {"id": "paused-task", "project_id": "paused", "title": "Paused",
             "description": "", "status": "DEFINED", "pr_url": BASE + "3",
             "created_at": 1, "updated_at": 1},
        ])
        await conn.execute(archived_tasks.insert().values(
            id="archived-task", project_id="active", title="Archived", description="",
            status="COMPLETED", pr_url=BASE + "4", created_at=1, updated_at=1, archived_at=2,
        ))


class FakeAccess:
    def __init__(self):
        self.binds = []
        self.runner = self

    async def run(self, args, **kwargs):
        assert args == ["api", "user"]
        return type("Result", (), {"stdout": b'{"type":"User","login":"jack"}'})()

    async def bind_repository(self, url):
        self.binds.append(url)
        return GitHubRepositoryBinding(1, "acme/repo")


class FakeClient:
    def __init__(self, binding, *, access):
        self.repository = binding
        self.access = access
        self.calls = []

    async def paged_list(self, path, **kwargs):
        self.calls.append(path)
        if "state=open" in path:
            return [
                {"html_url": BASE + "1", "number": 1, "title": "Fresh title",
                 "state": "open", "head": {"sha": SHA},
                 "created_at": "2026-09-20T00:00:00Z"},
                {"html_url": BASE + "4", "number": 4, "title": "Archived title",
                 "state": "open", "head": {"sha": SHA},
                 "created_at": "2026-09-19T00:00:00Z"},
            ]
        return [
            {"id": 1, "state": "CHANGES_REQUESTED", "commit_id": SHA,
             "user": {"login": "jack", "type": "User"}},
            {"id": 2, "state": "APPROVED", "commit_id": SHA,
             "user": {"login": "jack", "type": "User"}},
            {"id": 3, "state": "CHANGES_REQUESTED", "commit_id": SHA,
             "user": {"login": "bot", "type": "Bot"}},
            {"id": 4, "state": "CHANGES_REQUESTED", "commit_id": MOVED,
             "user": {"login": "other", "type": "User"}},
        ]

    async def paged_items(self, path, **kwargs):
        self.calls.append(path)
        return [{"status": "completed", "conclusion": "success"}]


async def test_candidates_include_archived_but_only_active_projects(db):
    await seed(db)
    rows = await list_known_pull_requests(db)
    assert {row["task_id"] for row in rows} == {
        "open-task", "closed-task", "archived-task",
    }


async def test_app_lifespan_starts_and_stops_refresher(db):
    class Inbox:
        started = False
        stopped = False

        def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

    inbox = Inbox()
    app = FastAPI()
    app.include_router(build_pull_requests_router(db=db, inbox=inbox))
    async with app.router.lifespan_context(app):
        assert inbox.started
        assert not inbox.stopped
    assert inbox.stopped


async def test_refresh_matches_open_prs_once_per_repo_and_get_uses_durable_snapshot(db):
    await seed(db)
    access = FakeAccess()
    clients = []

    def client_factory(binding, *, access):
        client = FakeClient(binding, access=access)
        clients.append(client)
        return client

    inbox = PendingPullRequestInbox(db, access, client_factory=client_factory)
    await inbox.refresh()
    assert len(access.binds) == 1
    assert sum("state=open" in path for path in clients[0].calls) == 1
    assert not any("/pulls/2/" in path for path in clients[0].calls)

    # Simulate daemon restart: a new inbox with unusable GitHub access still
    # reads the persisted projection; GET never invokes the refresher.
    restarted = PendingPullRequestInbox(db, None)
    app = FastAPI()
    app.include_router(build_pull_requests_router(db=db, inbox=restarted))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        response = await ac.get("/api/reviews/pull-requests")
        lan_response = await ac.get("/api/reviews/pull-requests", headers={
            "x-aq-dashboard-viewer": "other",
        })
        rebound = await ac.get("/api/reviews/pull-requests", headers={
            "host": "evil.example",
        })
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot_age_seconds"] is not None
    assert body["can_approve"] is True
    assert lan_response.json()["can_approve"] is False
    assert rebound.json()["can_approve"] is False
    rows = {row["task_id"]: row for row in body["pull_requests"]}
    assert set(rows) == {"open-task", "archived-task"}
    assert rows["open-task"]["title"] == "Fresh title"
    assert rows["open-task"]["review_decision"] == "approved"
    assert rows["open-task"]["ci_status"] == "success"
    assert rows["open-task"]["train_state"] == "awaiting review"
    assert rows["open-task"]["head_sha"] == SHA


async def test_failed_refresh_keeps_previous_snapshot(db):
    await seed(db)
    access = FakeAccess()
    inbox = PendingPullRequestInbox(db, access, client_factory=FakeClient)
    await inbox.refresh()
    first = await read_pull_request_snapshot(db)

    async def unavailable(_):
        raise RuntimeError("GitHub unavailable")

    access.bind_repository = unavailable
    with pytest.raises(RuntimeError):
        await inbox.refresh()
    assert await read_pull_request_snapshot(db) == first


async def test_task_scoped_session_cannot_read_or_approve(db):
    class Handler:
        called = False

        async def execute(self, *_):
            self.called = True
            return {"success": True}

    handler = Handler()
    app = FastAPI()
    app.include_router(build_pull_requests_router(
        db=db, inbox=PendingPullRequestInbox(db, None), command_handler=handler,
    ))

    @app.middleware("http")
    async def scoped(request, call_next):
        request.state.scope = RequestScope(
            kind="session", session_id="worker", project_id="active", task_id="open-task",
        )
        return await call_next(request)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        listing = await ac.get("/api/reviews/pull-requests")
        approval = await ac.post(
            "/api/reviews/pull-requests/open-task/approve", json={"head_sha": SHA},
        )
    assert listing.status_code == approval.status_code == 403
    assert not handler.called


async def test_generic_command_path_cannot_bypass_dashboard_viewer_gate(monkeypatch):
    from src.api import dependencies as deps

    monkeypatch.setattr(deps, "_require_session_token", False)
    monkeypatch.setattr(deps, "_token_store", None)
    class Handler(ReviewCommandsMixin):
        db = None  # An unauthorized call must stop before database access.

    app = FastAPI()
    app.add_middleware(TokenAuthMiddleware)

    @app.post("/api/execute")
    async def generic_command():
        return await Handler()._cmd_approve_pull_request({
            "task_id": "open-task", "head_sha": SHA,
        })

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        lan = await ac.post("/api/execute", headers={"x-aq-dashboard-viewer": "other"})
    assert lan.json()["error_code"] == "unauthorized"


async def test_approval_refuses_moved_head_before_write(db, monkeypatch):
    await seed(db)
    await PendingPullRequestInbox(db, FakeAccess(), client_factory=FakeClient).refresh()
    writes = []

    class HumanClient:
        def __init__(self, binding, *, access):
            self.repository = binding

        async def pull_request(self, url):
            return {"state": "open", "head": {"sha": MOVED}}

        async def request_json(self, *_args, **_kwargs):
            writes.append("posted")
            return {}

    import src.commands.review_commands as commands
    monkeypatch.setattr(commands.GitHubAccess, "from_config", lambda *_args, **_kwargs: FakeAccess())
    monkeypatch.setattr(commands, "GitHubClient", HumanClient)

    class Handler(ReviewCommandsMixin):
        def __init__(self):
            self.db = db

    result = await Handler()._cmd_approve_pull_request({"task_id": "open-task", "head_sha": SHA})
    assert result["error_code"] == "stale_head"
    assert writes == []


async def test_approval_refuses_a_bot_saved_as_gh_login(db, monkeypatch):
    await seed(db)
    await PendingPullRequestInbox(db, FakeAccess(), client_factory=FakeClient).refresh()
    access = FakeAccess()

    async def bot_user(*_args, **_kwargs):
        return type("Result", (), {"stdout": b'{"type":"Bot","login":"aq-app"}'})()

    access.run = bot_user
    import src.commands.review_commands as commands
    monkeypatch.setattr(commands.GitHubAccess, "from_config", lambda *_args, **_kwargs: access)

    class Handler(ReviewCommandsMixin):
        def __init__(self):
            self.db = db

    result = await Handler()._cmd_approve_pull_request({"task_id": "open-task", "head_sha": SHA})
    assert result["error_code"] == "credentials"
    assert access.binds == []


async def test_approval_posts_as_human_at_snapshot_head_and_flushes_train(db, monkeypatch):
    await seed(db)
    await PendingPullRequestInbox(db, FakeAccess(), client_factory=FakeClient).refresh()
    writes = []

    class HumanClient:
        def __init__(self, binding, *, access):
            self.repository = binding

        async def pull_request(self, url):
            return {"state": "open", "head": {"sha": SHA}}

        async def request_json(self, method, path, *, json_body, expected_statuses):
            writes.append((method, path, json_body))
            return {"id": 123, "state": "APPROVED", "commit_id": SHA,
                    "user": {"type": "User", "login": "jack"}}

    import src.commands.review_commands as commands
    monkeypatch.setattr(commands.GitHubAccess, "from_config", lambda *_args, **kwargs: FakeAccess())
    monkeypatch.setattr(commands, "GitHubClient", HumanClient)

    class Handler(ReviewCommandsMixin):
        def __init__(self):
            self.db = db
            self.flushes = []

        async def execute(self, name, args):
            self.flushes.append((name, args))
            return {"success": True}

    handler = Handler()
    result = await handler._cmd_approve_pull_request({"task_id": "open-task", "head_sha": SHA})
    assert result["success"] is True
    assert writes == [("POST", "repos/acme/repo/pulls/1/reviews",
                       {"event": "APPROVE", "commit_id": SHA})]
    assert handler.flushes == [("integration_flush", {"project_id": "active"})]
