"""The Reviews inbox reads a durable snapshot and pins human approvals."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.auth import RequestScope
from src.api.middleware import TokenAuthMiddleware
from src.api.pull_requests import PendingPullRequestInbox, build_pull_requests_router
from src.commands.review_commands import ReviewCommandsMixin
from src.database.queries.pull_request_queries import (
    list_known_pull_requests, read_pull_request_snapshot, write_pull_request_snapshot,
)
from src.database.tables import archived_tasks, projects, tasks
from src.git.github import GitHubAccess, GitHubClient
from src.git.github_auth import GitHubAuth
from src.integration.ci import AuthenticatedGitHubObserver, CIObservationDeferred, IntegrationCITrust
from src.git.github_activity import GitHubActivity
from src.git.github_contracts import GitHubAccessError, GitHubRepositoryBinding

SHA = "a" * 40
MOVED = "b" * 40
BASE = "https://github.com/acme/repo/pull/"


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("pending-prs")
    yield database


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
                 "updated_at": "2026-09-20T00:00:00Z",
                 "created_at": "2026-09-20T00:00:00Z"},
                {"html_url": BASE + "4", "number": 4, "title": "Archived title",
                 "state": "open", "head": {"sha": SHA},
                 "updated_at": "2026-09-20T00:00:00Z",
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


async def test_inbox_and_hosted_ci_observers_share_one_check_run_request(db):
    await seed(db)
    auth = GitHubAuth()
    calls = []

    class Runner:
        credential_identity = auth.credential_identity

        async def run(self, args, **kwargs):
            path = args[-1]
            calls.append(path)
            await asyncio.sleep(0)  # let the three independent observers overlap
            if "/check-runs?" in path:
                body = {"check_runs": [{"id": 11, "name": "Tests", "app": {"id": 404},
                                       "status": "completed", "conclusion": "success"}]}
            elif "/actions/runs?" in path:
                body = {"workflow_runs": []}
            elif "state=open" in path:
                body = [{"html_url": BASE + "1", "state": "open", "head": {"sha": SHA}}]
            elif path.endswith("/pulls/1"):
                body = {"number": 1, "html_url": BASE + "1", "state": "open", "head": {"sha": SHA},
                        "base": {"repo": {"id": 1, "full_name": "acme/repo"}}}
            else:
                body = []  # reviews
            return SimpleNamespace(returncode=0, stderr="", stdout=(
                b"HTTP/2.0 200 OK\r\n\r\n" + json.dumps(body).encode()))

    access = GitHubAccess(auth, Runner())
    binding = GitHubRepositoryBinding(1, "acme/repo")
    access.bind_repository = lambda _: _binding()

    async def _binding():
        return binding

    trust = IntegrationCITrust(canonical_repository_id="repo", repository_id=1,
                              full_name="acme/repo", producer_id="github-actions",
                              required_checks={"version": "v1", "names": ["unit"]})
    inbox = PendingPullRequestInbox(db, access)
    observers = [AuthenticatedGitHubObserver(GitHubClient(binding, access=access))
                 for _ in range(2)]
    results = await asyncio.gather(
        inbox.refresh(), *(observer.observe(trust, SHA) for observer in observers),
        return_exceptions=True,
    )
    assert results[0] is None
    assert all(isinstance(result, CIObservationDeferred) for result in results[1:])
    items, _ = await read_pull_request_snapshot(db)
    assert items[0]["ci_status"] == "success"
    assert sum("/check-runs?" in path for path in calls) == 1
    await inbox.refresh_row("open-task")
    assert sum("/check-runs?" in path for path in calls) == 1


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


async def test_project_summary_aggregates_snapshot_without_pr_details_or_github(db, monkeypatch):
    await seed(db)
    async with db._engine.begin() as conn:
        await conn.execute(projects.insert().values(
            id="empty", name="Empty project", repo_url="git@github.com:acme/empty.git",
            status="ACTIVE", created_at=1,
        ))
        # Project names come from current configuration, not cached PR details.
        await conn.execute(projects.update().where(projects.c.id == "active").values(
            name="Renamed project",
        ))
    await write_pull_request_snapshot(db, [
        {"project_id": "active", "url": BASE + "1"},
        {"project_id": "active", "url": BASE + "1"},  # two tasks share the same PR
        {"project_id": "active", "url": BASE + "4"},  # archived task's open PR
        {"project_id": "paused", "url": BASE + "3"},
    ], now=100)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Summary must not load PR details or refresh GitHub")

    import src.api.pull_requests as api
    monkeypatch.setattr(api, "read_pull_request_snapshot", forbidden)
    restarted = PendingPullRequestInbox(db, None)
    monkeypatch.setattr(restarted, "list", forbidden)
    monkeypatch.setattr(restarted, "refresh", forbidden)
    app = FastAPI()
    app.include_router(build_pull_requests_router(db=db, inbox=restarted))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        response = await ac.get("/api/reviews/pull-requests/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot_at"] == 100
    assert body["snapshot_age_seconds"] > 0
    assert body["projects"] == [
        {"project_id": "empty", "project_name": "Empty project", "repository": "acme/empty",
         "pulls_url": "https://github.com/acme/empty/pulls", "open_pr_count": 0},
        {"project_id": "active", "project_name": "Renamed project", "repository": "acme/repo",
         "pulls_url": "https://github.com/acme/repo/pulls", "open_pr_count": 2},
    ]
    assert "pull_requests" not in body
    assert "can_approve" not in body


async def test_project_summary_distinguishes_missing_and_empty_snapshot(db):
    await seed(db)
    app = FastAPI()
    app.include_router(build_pull_requests_router(db=db, inbox=PendingPullRequestInbox(db, None)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        missing = (await ac.get("/api/reviews/pull-requests/summary")).json()
        await write_pull_request_snapshot(db, [], now=100)
        empty = (await ac.get("/api/reviews/pull-requests/summary")).json()
    assert missing["snapshot_at"] is None
    assert missing["snapshot_age_seconds"] is None
    assert empty["snapshot_at"] == 100
    assert missing["projects"] == empty["projects"]
    assert empty["projects"][0]["open_pr_count"] == 0


@pytest.mark.parametrize(("repo_url", "pulls_url"), [
    ("https://github.com/acme/repo.git/", "https://github.com/acme/repo/pulls"),
    ("ssh://git@github.com/acme/repo.git", "https://github.com/acme/repo/pulls"),
    ("acme/repo", "https://github.com/acme/repo/pulls"),
    ("https://gitlab.com/acme/repo", None),
    ("https://github.com/../repo", None),
    ("https://github.com/acme/repo?token=secret", None),
    ("https://user:password@github.com/acme/repo", None),
    (None, None),
])
async def test_project_summary_only_returns_canonical_github_links(db, repo_url, pulls_url):
    await seed(db)
    async with db._engine.begin() as conn:
        await conn.execute(projects.update().where(projects.c.id == "active").values(
            repo_url=repo_url,
        ))
    app = FastAPI()
    app.include_router(build_pull_requests_router(db=db, inbox=PendingPullRequestInbox(db, None)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        response = await ac.get("/api/reviews/pull-requests/summary")
    assert response.status_code == 200
    rows = response.json()["projects"]
    if pulls_url is None:
        assert rows == []
    else:
        assert len(rows) == 1
        assert rows[0]["pulls_url"] == pulls_url


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
        summary = await ac.get("/api/reviews/pull-requests/summary")
        approval = await ac.post(
            "/api/reviews/pull-requests/open-task/approve", json={"head_sha": SHA},
        )
    assert listing.status_code == summary.status_code == approval.status_code == 403
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


@pytest.fixture
async def cached_inbox(db):
    await seed(db)
    access = FakeAccess()
    activity = GitHubActivity(lambda: 1000)

    class Client(FakeClient):
        pulls = None
        fail_checks = False

        async def pull_request(self, url):
            self.calls.append(url)
            activity.check_background()
            activity.record(True)
            return next(pull for pull in self.pulls if pull["html_url"] == url)

        async def paged_list(self, path, **kwargs):
            activity.check_background()
            activity.record(True)
            if "state=open" in path and self.pulls is not None:
                self.calls.append(path)
                return self.pulls
            return await super().paged_list(path, **kwargs)

        async def paged_items(self, path, **kwargs):
            activity.check_background()
            activity.record(True)
            if self.fail_checks:
                raise RuntimeError("checks unavailable")
            return await super().paged_items(path, **kwargs)

    client = Client(await access.bind_repository("acme/repo"), access=access)
    client.pulls = await FakeClient.paged_list(client, "state=open")
    client.calls.clear()
    inbox = PendingPullRequestInbox(db, access, client_factory=lambda *_a, **_kw: client)
    return inbox, client, activity


async def test_unchanged_refresh_makes_no_badge_calls_and_updates_metadata(db, cached_inbox):
    inbox, client, activity = cached_inbox
    assert inbox.interval == 300
    await inbox.refresh()
    assert len(client.calls) == 5  # inventory + two badge requests per PR
    assert activity.snapshot()["api_calls_per_hour"] == {"dashboard_pr_snapshot": 5}
    client.calls.clear()
    async with db._engine.begin() as conn:
        await conn.execute(projects.update().where(projects.c.id == "active").values(
            name="Renamed project",
        ))
    await inbox.refresh()
    assert client.calls == ["repos/acme/repo/pulls?state=open&per_page=100"]
    items, _ = await read_pull_request_snapshot(db)
    assert {item["project_name"] for item in items} == {"Renamed project"}
    assert {item["review_decision"] for item in items} == {"approved"}
    assert {item["ci_status"] for item in items} == {"success"}


@pytest.mark.parametrize("field", ["head", "updated_at", "missing_timestamp"])
async def test_badge_cache_invalidates_only_changed_pr(db, cached_inbox, field):
    inbox, client, _ = cached_inbox
    await inbox.refresh()
    client.calls.clear()
    if field == "head":
        client.pulls[0]["head"] = {"sha": MOVED}
    elif field == "updated_at":
        client.pulls[0]["updated_at"] = "2026-09-21T00:00:00Z"
    else:
        client.pulls[0].pop("updated_at")
    await inbox.refresh()
    expected = (f"repos/acme/repo/commits/{MOVED}/check-runs?per_page=100"
                if field == "head" else "repos/acme/repo/pulls/1/reviews?per_page=100")
    assert client.calls == ["repos/acme/repo/pulls?state=open&per_page=100", expected]
    items, _ = await read_pull_request_snapshot(db)
    changed = next(item for item in items if item["task_id"] == "open-task")
    assert changed["review_decision"] == ("changes requested" if field == "head" else "approved")
    if field == "missing_timestamp":
        client.calls.clear()
        await inbox.refresh()
        assert client.calls == ["repos/acme/repo/pulls?state=open&per_page=100", expected]


@pytest.mark.parametrize("conclusion", ["success", "failure"])
async def test_review_update_reuses_terminal_checks(db, cached_inbox, conclusion):
    inbox, client, _ = cached_inbox
    original_checks = client.paged_items

    async def checks(path, **kwargs):
        await original_checks(path, **kwargs)
        return [{"status": "completed", "conclusion": conclusion}]

    client.paged_items = checks
    await inbox.refresh()
    original_reviews = client.paged_list

    async def reviews(path, **kwargs):
        result = await original_reviews(path, **kwargs)
        if "/pulls/1/reviews" in path:
            return [{"id": 5, "state": "CHANGES_REQUESTED", "commit_id": SHA,
                     "user": {"login": "jack", "type": "User"}}]
        return result

    client.paged_list = reviews
    client.pulls[0]["updated_at"] = "2026-09-21T00:00:00Z"
    client.calls.clear()
    await inbox.refresh()
    assert client.calls == ["repos/acme/repo/pulls?state=open&per_page=100",
                            "repos/acme/repo/pulls/1/reviews?per_page=100"]
    items, _ = await read_pull_request_snapshot(db)
    changed = next(item for item in items if item["task_id"] == "open-task")
    assert changed["review_decision"] == "changes requested"
    assert changed["ci_status"] == conclusion


async def test_pending_checks_retry_with_capped_backoff_and_stop_when_terminal(
    db, cached_inbox, monkeypatch,
):
    inbox, client, _ = cached_inbox
    now = 0
    monkeypatch.setattr("src.api.pull_requests.monotonic", lambda: now)
    original_checks = client.paged_items
    status = "in_progress"

    async def checks(path, **kwargs):
        await original_checks(path, **kwargs)
        return [{"status": status, "conclusion": "success"}]

    client.paged_items = checks
    await inbox.refresh()
    for deadline in (300, 900, 1800, 2700):
        now = deadline - 1
        client.calls.clear()
        await inbox.refresh()
        assert client.calls == ["repos/acme/repo/pulls?state=open&per_page=100"]
        now = deadline
        client.calls.clear()
        await inbox.refresh()
        assert len(client.calls) == 3
        assert sum("/check-runs" in path for path in client.calls) == 2
        assert not any("/reviews" in path for path in client.calls)
    status = "completed"
    now = 3600
    await inbox.refresh()
    items, _ = await read_pull_request_snapshot(db)
    assert {item["ci_status"] for item in items} == {"success"}
    now = 10000
    client.calls.clear()
    await inbox.refresh()
    assert client.calls == ["repos/acme/repo/pulls?state=open&per_page=100"]


async def test_new_head_resets_pending_check_backoff(cached_inbox, monkeypatch):
    inbox, client, _ = cached_inbox
    now = 0
    monkeypatch.setattr("src.api.pull_requests.monotonic", lambda: now)
    original_checks = client.paged_items

    async def checks(path, **kwargs):
        await original_checks(path, **kwargs)
        return []  # No runs yet is also pending.

    client.paged_items = checks
    await inbox.refresh()
    now = 300
    await inbox.refresh()  # Old head's next check is now at 900.
    now = 301
    client.pulls[0]["head"] = {"sha": MOVED}
    client.calls.clear()
    await inbox.refresh()
    assert client.calls == ["repos/acme/repo/pulls?state=open&per_page=100",
                            f"repos/acme/repo/commits/{MOVED}/check-runs?per_page=100"]
    now = 601
    client.calls.clear()
    await inbox.refresh()
    assert client.calls == ["repos/acme/repo/pulls?state=open&per_page=100",
                            f"repos/acme/repo/commits/{MOVED}/check-runs?per_page=100"]


async def test_closed_pr_is_evicted_and_reopening_fetches_badges(db, cached_inbox):
    inbox, client, _ = cached_inbox
    await inbox.refresh()
    closed = client.pulls.pop(0)
    await inbox.refresh()
    items, _ = await read_pull_request_snapshot(db)
    assert {item["task_id"] for item in items} == {"archived-task"}
    client.calls.clear()
    client.pulls.append(closed)
    await inbox.refresh()
    assert len(client.calls) == 3
    assert any("/pulls/1/reviews" in path for path in client.calls)


async def test_failed_badge_read_is_retried_without_replacing_snapshot(db, cached_inbox):
    inbox, client, _ = cached_inbox
    await inbox.refresh()
    before = await read_pull_request_snapshot(db)
    client.pulls[0]["head"] = {"sha": MOVED}
    client.fail_checks = True
    with pytest.raises(RuntimeError, match="checks unavailable"):
        await inbox.refresh()
    assert await read_pull_request_snapshot(db) == before
    client.fail_checks = False
    client.calls.clear()
    await inbox.refresh()
    assert client.calls == ["repos/acme/repo/pulls?state=open&per_page=100",
                            f"repos/acme/repo/commits/{MOVED}/check-runs?per_page=100"]


async def test_row_refresh_forces_badges_and_background_respects_reserve(db, cached_inbox):
    inbox, client, activity = cached_inbox
    await inbox.refresh()
    before = await read_pull_request_snapshot(db)
    activity.observe({"x-ratelimit-limit": "100", "x-ratelimit-remaining": "10",
                      "x-ratelimit-reset": "1100"})
    client.calls.clear()
    with pytest.raises(GitHubAccessError, match="reserve"):
        await inbox.refresh()
    assert client.calls == []
    assert await read_pull_request_snapshot(db) == before
    # A user-triggered row refresh can use the reserved foreground quota.
    await inbox.refresh_row("open-task")
    assert len(client.calls) == 3
    assert not any("state=open" in path or "/pulls/4/" in path for path in client.calls)
    activity.quota = None
    client.calls.clear()
    await inbox.refresh()
    assert len(client.calls) == 1
    # A closed row is removed immediately; its old fingerprint cannot be reused.
    client.pulls[0]["state"] = "closed"
    await inbox.refresh_row("open-task")
    items, _ = await read_pull_request_snapshot(db)
    assert {item["task_id"] for item in items} == {"archived-task"}
    client.pulls[0]["state"] = "open"
    client.calls.clear()
    await inbox.refresh()
    assert len(client.calls) == 3


async def test_approval_survives_cached_badges_when_review_list_lags(db, cached_inbox):
    inbox, client, _ = cached_inbox
    await inbox.refresh()
    original = client.paged_list

    async def lagging_reviews(path, **kwargs):
        if "/reviews" in path:
            client.calls.append(path)
            return []
        return await original(path, **kwargs)

    client.paged_list = lagging_reviews

    class Handler:
        async def execute(self, command, params):
            assert command == "approve_pull_request"
            assert params == {"task_id": "open-task", "head_sha": SHA}
            return {"success": True, "url": BASE + "1"}

    app = FastAPI()
    app.include_router(build_pull_requests_router(db=db, inbox=inbox, command_handler=Handler()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        response = await ac.post(
            "/api/reviews/pull-requests/open-task/approve", json={"head_sha": SHA},
        )
    assert response.status_code == 200
    client.calls.clear()
    await inbox.refresh()
    assert len(client.calls) == 1
    items, _ = await read_pull_request_snapshot(db)
    assert next(item for item in items if item["task_id"] == "open-task")["review_decision"] == (
        "approved"
    )
    # A head that moved after approval cannot inherit the old head's approval.
    client.pulls[0]["head"] = {"sha": MOVED}
    await inbox.refresh_row("open-task", approved_head_sha=SHA)
    items, _ = await read_pull_request_snapshot(db)
    assert next(item for item in items if item["task_id"] == "open-task")["review_decision"] == (
        "pending"
    )


async def test_failed_refresh_joins_remaining_projections_before_releasing_lock(
    db, cached_inbox,
):
    inbox, client, _ = cached_inbox
    await inbox.refresh()
    before = await read_pull_request_snapshot(db)
    for pull in client.pulls:
        pull["updated_at"] = "2026-09-21T00:00:00Z"
    started = asyncio.Event()
    release = asyncio.Event()
    original = client.paged_list

    async def failing_reviews(path, **kwargs):
        if "/pulls/1/reviews" in path:
            raise RuntimeError("reviews unavailable")
        if "/pulls/4/reviews" in path:
            started.set()
            await release.wait()
        return await original(path, **kwargs)

    client.paged_list = failing_reviews
    refresh = asyncio.create_task(inbox.refresh())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not refresh.done()
    finally:
        release.set()
        with pytest.raises(RuntimeError, match="reviews unavailable"):
            await refresh
    assert await read_pull_request_snapshot(db) == before
