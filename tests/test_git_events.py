"""Comprehensive tests for GitManager event emission.

Covers all three git event types (git.commit, git.push, git.pr.created),
validates payloads against event_schemas, tests failure isolation and
concurrent agent isolation.

See docs/specs/design/playbooks.md Section 7 for the specification.
"""

import asyncio
import pathlib
import re
import subprocess

import pytest

from src.event_bus import EventBus
from src.event_schemas import validate_event
from src.git.manager import GitError, GitManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit_file(clone: str, filename: str, content: str, message: str) -> str:
    pathlib.Path(clone, filename).write_text(content)
    _git(["add", filename], cwd=clone)
    _git(
        ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", message],
        cwd=clone,
    )
    return _git(["rev-parse", "HEAD"], cwd=clone)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bare_repo(tmp_path):
    """Create a bare repo to act as 'origin'."""
    bare = str(tmp_path / "origin.git")
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", bare],
        check=True,
        capture_output=True,
    )
    return bare


@pytest.fixture
def clone(tmp_path, bare_repo):
    """Clone the bare repo with an initial commit pushed to main."""
    clone_path = str(tmp_path / "clone")
    subprocess.run(["git", "clone", bare_repo, clone_path], check=True, capture_output=True)
    _git(["config", "user.name", "Test"], cwd=clone_path)
    _git(["config", "user.email", "t@t.com"], cwd=clone_path)
    pathlib.Path(clone_path, "README.md").write_text("init")
    _git(["add", "."], cwd=clone_path)
    _git(["commit", "-m", "init"], cwd=clone_path)
    _git(["push", "origin", "main"], cwd=clone_path)
    return clone_path


@pytest.fixture
def mgr():
    return GitManager()


@pytest.fixture
def bus():
    """EventBus with validation enabled in dev mode (raises on schema failures)."""
    return EventBus(env="dev", validate_events=True)


@pytest.fixture
def collector(bus):
    """Returns a dict of event_type -> list[event_data], subscribed to all events."""
    collected: dict[str, list[dict]] = {}

    def _handler(data):
        event_type = data.get("_event_type", "unknown")
        collected.setdefault(event_type, []).append(data)

    bus.subscribe("*", _handler)
    return collected


# ===========================================================================
# (a) acommit_all() emits git.commit with correct payload
# ===========================================================================


class TestCommitEventPayload:
    """Verify acommit_all() emits git.commit with commit_hash, branch,
    changed_files, message, project_id, agent_id."""

    @pytest.mark.asyncio
    async def test_commit_event_full_payload(self, clone, mgr, bus):
        """All expected fields are present and correct in the event."""
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        pathlib.Path(clone, "feature.py").write_text("print('hello')")
        committed = await mgr.acommit_all(
            clone,
            "feat: add feature",
            event_bus=bus,
            project_id="proj-a",
            agent_id="agent-42",
        )

        assert committed is True
        assert len(received) == 1
        evt = received[0]

        # commit_hash: 40-char hex SHA
        assert len(evt["commit_hash"]) == 40
        assert re.fullmatch(r"[0-9a-f]{40}", evt["commit_hash"])
        # Verify it matches the actual HEAD
        actual_sha = _git(["rev-parse", "HEAD"], cwd=clone)
        assert evt["commit_hash"] == actual_sha

        # branch
        assert evt["branch"] == "main"

        # changed_files
        assert "feature.py" in evt["changed_files"]
        assert isinstance(evt["changed_files"], list)

        # message
        assert evt["message"] == "feat: add feature"

        # project_id and agent_id
        assert evt["project_id"] == "proj-a"
        assert evt["agent_id"] == "agent-42"

    @pytest.mark.asyncio
    async def test_commit_event_on_feature_branch(self, clone, mgr, bus):
        """Event branch field reflects the actual branch name."""
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        await mgr.acreate_branch(clone, "feature/my-feature")
        pathlib.Path(clone, "on_branch.txt").write_text("on branch")
        await mgr.acommit_all(
            clone, "on branch commit", event_bus=bus, project_id="p1"
        )

        assert len(received) == 1
        assert received[0]["branch"] == "feature/my-feature"

    @pytest.mark.asyncio
    async def test_commit_event_multiple_files(self, clone, mgr, bus):
        """changed_files lists every file in the commit."""
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        pathlib.Path(clone, "alpha.txt").write_text("a")
        pathlib.Path(clone, "beta.txt").write_text("b")
        pathlib.Path(clone, "gamma.txt").write_text("g")
        await mgr.acommit_all(clone, "multi-file", event_bus=bus, project_id="p1")

        assert len(received) == 1
        assert set(received[0]["changed_files"]) == {"alpha.txt", "beta.txt", "gamma.txt"}

    @pytest.mark.asyncio
    async def test_commit_event_no_event_on_empty_commit(self, clone, mgr, bus):
        """No event when there are no changes to commit."""
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        committed = await mgr.acommit_all(
            clone, "empty", event_bus=bus, project_id="p1", agent_id="a1"
        )
        assert committed is False
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_commit_event_without_bus(self, clone, mgr):
        """Backward compatibility: no bus means no emission, no error."""
        pathlib.Path(clone, "compat.txt").write_text("compat")
        committed = await mgr.acommit_all(clone, "no bus")
        assert committed is True

    @pytest.mark.asyncio
    async def test_commit_event_with_none_project_id(self, clone, mgr, bus):
        """project_id=None is passed through (system-scoped event)."""
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        pathlib.Path(clone, "sys.txt").write_text("sys")
        await mgr.acommit_all(
            clone, "sys commit", event_bus=bus, project_id=None, agent_id=None
        )

        assert len(received) == 1
        evt = received[0]
        assert evt["project_id"] is None
        assert evt["agent_id"] is None


# ===========================================================================
# (b) apush_branch() emits git.push with correct payload
# ===========================================================================


class TestPushEventPayload:
    """Verify apush_branch() emits git.push with branch, remote,
    commit_range, project_id. Remote defaults to 'origin'."""

    @pytest.mark.asyncio
    async def test_push_event_first_push(self, clone, mgr, bus):
        """First push: commit_range is the local ref (no remote ref before)."""
        received: list[dict] = []
        bus.subscribe("git.push", lambda data: received.append(data))

        await mgr.aprepare_for_task(clone, "task/push-first")
        _commit_file(clone, "first.txt", "data", "first push")
        local_ref = _git(["rev-parse", "HEAD"], cwd=clone)

        await mgr.apush_branch(
            clone, "task/push-first", event_bus=bus, project_id="proj-push"
        )

        assert len(received) == 1
        evt = received[0]
        assert evt["branch"] == "task/push-first"
        assert evt["remote"] == "origin"
        assert evt["commit_range"] == local_ref
        assert evt["project_id"] == "proj-push"

    @pytest.mark.asyncio
    async def test_push_event_remote_defaults_to_origin(self, clone, mgr, bus):
        """remote field is always 'origin'."""
        received: list[dict] = []
        bus.subscribe("git.push", lambda data: received.append(data))

        await mgr.aprepare_for_task(clone, "task/remote-check")
        _commit_file(clone, "r.txt", "data", "remote check")
        await mgr.apush_branch(
            clone, "task/remote-check", event_bus=bus, project_id="p1"
        )

        assert received[0]["remote"] == "origin"

    @pytest.mark.asyncio
    async def test_push_event_commit_range_on_subsequent_push(self, clone, mgr, bus):
        """Second push: commit_range is old_ref..new_ref."""
        received: list[dict] = []
        bus.subscribe("git.push", lambda data: received.append(data))

        await mgr.aprepare_for_task(clone, "task/push-range")
        _commit_file(clone, "one.txt", "1", "first")
        await mgr.apush_branch(clone, "task/push-range")  # no bus — first push
        remote_ref = _git(["rev-parse", "origin/task/push-range"], cwd=clone)

        _commit_file(clone, "two.txt", "2", "second")
        local_ref = _git(["rev-parse", "HEAD"], cwd=clone)

        await mgr.apush_branch(
            clone, "task/push-range", event_bus=bus, project_id="proj-range"
        )

        assert len(received) == 1
        evt = received[0]
        assert evt["commit_range"] == f"{remote_ref}..{local_ref}"

    @pytest.mark.asyncio
    async def test_push_event_without_bus(self, clone, mgr):
        """No bus means no emission, push still succeeds."""
        await mgr.aprepare_for_task(clone, "task/nobus-push")
        _commit_file(clone, "nb.txt", "data", "no bus push")
        await mgr.apush_branch(clone, "task/nobus-push")
        # No exception = success


# ===========================================================================
# (c) acreate_pr() emits git.pr.created with correct payload
# ===========================================================================


class TestPRCreatedEventPayload:
    """Verify acreate_pr() emits git.pr.created with pr_url, branch,
    title, project_id. PR URL should be valid."""

    @pytest.mark.asyncio
    async def test_pr_created_event_payload(self, clone, mgr, bus, monkeypatch):
        """Mocked gh pr create — verifies event payload."""
        received: list[dict] = []
        bus.subscribe("git.pr.created", lambda data: received.append(data))

        fake_url = "https://github.com/test/repo/pull/42"

        async def _mock_subprocess(args, cwd=None, timeout=None):
            """Simulate a successful `gh pr create`."""

            class _Result:
                returncode = 0
                stdout = fake_url
                stderr = ""

            return _Result()

        monkeypatch.setattr(mgr, "_arun_subprocess", _mock_subprocess)

        pr_url = await mgr.acreate_pr(
            clone,
            "feature/my-branch",
            "My PR Title",
            "Description body",
            base="main",
            event_bus=bus,
            project_id="proj-pr",
        )

        assert pr_url == fake_url
        assert len(received) == 1
        evt = received[0]
        assert evt["pr_url"] == fake_url
        assert evt["branch"] == "feature/my-branch"
        assert evt["title"] == "My PR Title"
        assert evt["project_id"] == "proj-pr"

    @pytest.mark.asyncio
    async def test_pr_url_looks_valid(self, clone, mgr, bus, monkeypatch):
        """PR URL should match a GitHub PR URL pattern."""
        received: list[dict] = []
        bus.subscribe("git.pr.created", lambda data: received.append(data))

        fake_url = "https://github.com/org/repo/pull/123"

        async def _mock_subprocess(args, cwd=None, timeout=None):
            class _Result:
                returncode = 0
                stdout = fake_url
                stderr = ""

            return _Result()

        monkeypatch.setattr(mgr, "_arun_subprocess", _mock_subprocess)

        await mgr.acreate_pr(
            clone, "feat/x", "Title", "Body", event_bus=bus, project_id="p1"
        )

        url = received[0]["pr_url"]
        assert re.match(r"https://github\.com/.+/pull/\d+", url)

    @pytest.mark.asyncio
    async def test_pr_created_without_bus(self, clone, mgr, monkeypatch):
        """No bus means no emission, PR creation still succeeds."""
        async def _mock_subprocess(args, cwd=None, timeout=None):
            class _Result:
                returncode = 0
                stdout = "https://github.com/test/repo/pull/1"
                stderr = ""

            return _Result()

        monkeypatch.setattr(mgr, "_arun_subprocess", _mock_subprocess)

        url = await mgr.acreate_pr(clone, "b", "T", "B")
        assert "pull" in url


# ===========================================================================
# (d) Failed git operations do NOT emit events
# ===========================================================================


class TestFailedOperationsNoEvent:
    """Failed git operations should never emit events."""

    @pytest.mark.asyncio
    async def test_failed_push_no_event(self, clone, mgr, bus):
        """Push to nonexistent branch does not emit git.push."""
        received: list[dict] = []
        bus.subscribe("git.push", lambda data: received.append(data))

        with pytest.raises(GitError):
            await mgr.apush_branch(
                clone,
                "nonexistent-branch-xyz",
                event_bus=bus,
                project_id="proj-fail",
            )
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_failed_commit_no_event(self, clone, mgr, bus):
        """Empty commit (nothing to commit) does not emit git.commit."""
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        committed = await mgr.acommit_all(
            clone, "nothing", event_bus=bus, project_id="proj-fail"
        )
        assert committed is False
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_failed_pr_creation_no_event(self, clone, mgr, bus, monkeypatch):
        """Failed gh pr create does not emit git.pr.created."""
        received: list[dict] = []
        bus.subscribe("git.pr.created", lambda data: received.append(data))

        async def _mock_subprocess(args, cwd=None, timeout=None):
            class _Result:
                returncode = 1
                stdout = ""
                stderr = "authorization required"

            return _Result()

        monkeypatch.setattr(mgr, "_arun_subprocess", _mock_subprocess)

        with pytest.raises(GitError, match="authorization required"):
            await mgr.acreate_pr(
                clone, "b", "T", "B", event_bus=bus, project_id="p1"
            )
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_pr_timeout_no_event(self, clone, mgr, bus, monkeypatch):
        """gh pr create timeout does not emit git.pr.created."""
        received: list[dict] = []
        bus.subscribe("git.pr.created", lambda data: received.append(data))

        async def _mock_subprocess(args, cwd=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

        monkeypatch.setattr(mgr, "_arun_subprocess", _mock_subprocess)

        with pytest.raises(GitError, match="timed out"):
            await mgr.acreate_pr(
                clone, "b", "T", "B", event_bus=bus, project_id="p1"
            )
        assert len(received) == 0


# ===========================================================================
# (e) Event payloads pass the schemas defined in event_schemas.py
# ===========================================================================


class TestEventPayloadsPassSchema:
    """Every emitted event should pass validate_event() with no errors."""

    @pytest.mark.asyncio
    async def test_commit_event_passes_schema(self, clone, mgr, bus, collector):
        """git.commit event payload validates against its schema."""
        pathlib.Path(clone, "schema_test.txt").write_text("schema")
        await mgr.acommit_all(
            clone,
            "schema test",
            event_bus=bus,
            project_id="proj-s",
            agent_id="agent-s",
        )

        events = collector.get("git.commit", [])
        assert len(events) == 1
        # Remove meta field injected by EventBus before validating
        payload = {k: v for k, v in events[0].items() if not k.startswith("_")}
        errors = validate_event("git.commit", payload)
        assert errors == [], f"Schema validation errors: {errors}"

    @pytest.mark.asyncio
    async def test_push_event_passes_schema(self, clone, mgr, bus, collector):
        """git.push event payload validates against its schema."""
        await mgr.aprepare_for_task(clone, "task/schema-push")
        _commit_file(clone, "sp.txt", "data", "schema push")
        await mgr.apush_branch(
            clone, "task/schema-push", event_bus=bus, project_id="proj-s"
        )

        events = collector.get("git.push", [])
        assert len(events) == 1
        payload = {k: v for k, v in events[0].items() if not k.startswith("_")}
        errors = validate_event("git.push", payload)
        assert errors == [], f"Schema validation errors: {errors}"

    @pytest.mark.asyncio
    async def test_pr_event_passes_schema(self, clone, mgr, bus, collector, monkeypatch):
        """git.pr.created event payload validates against its schema."""

        async def _mock_subprocess(args, cwd=None, timeout=None):
            class _Result:
                returncode = 0
                stdout = "https://github.com/org/repo/pull/99"
                stderr = ""

            return _Result()

        monkeypatch.setattr(mgr, "_arun_subprocess", _mock_subprocess)

        await mgr.acreate_pr(
            clone, "feat/x", "Title", "Body", event_bus=bus, project_id="proj-s"
        )

        events = collector.get("git.pr.created", [])
        assert len(events) == 1
        payload = {k: v for k, v in events[0].items() if not k.startswith("_")}
        errors = validate_event("git.pr.created", payload)
        assert errors == [], f"Schema validation errors: {errors}"

    @pytest.mark.asyncio
    async def test_commit_event_with_strict_extras(self, clone, mgr, bus, collector):
        """git.commit passes strict_extras validation (no unexpected fields)."""
        pathlib.Path(clone, "strict.txt").write_text("strict")
        await mgr.acommit_all(
            clone,
            "strict test",
            event_bus=bus,
            project_id="proj-strict",
            agent_id="agent-strict",
        )

        events = collector.get("git.commit", [])
        assert len(events) == 1
        payload = {k: v for k, v in events[0].items() if not k.startswith("_")}
        errors = validate_event("git.commit", payload, strict_extras=True)
        assert errors == [], f"Strict validation errors: {errors}"


# ===========================================================================
# (f) Event payloads captured by an EventBus subscriber (integration test)
# ===========================================================================


class TestEventBusSubscriberIntegration:
    """Integration tests: events are properly captured by subscribers."""

    @pytest.mark.asyncio
    async def test_wildcard_subscriber_captures_all_events(
        self, clone, mgr, bus, monkeypatch
    ):
        """A wildcard ('*') subscriber sees commit, push, and PR events."""
        all_events: list[dict] = []
        bus.subscribe("*", lambda data: all_events.append(data))

        # 1) Commit event
        await mgr.aprepare_for_task(clone, "task/wild-integ")
        pathlib.Path(clone, "wild.txt").write_text("wildcard")
        await mgr.acommit_all(
            clone, "wildcard commit", event_bus=bus, project_id="proj-wild"
        )

        # 2) Push event
        await mgr.apush_branch(
            clone, "task/wild-integ", event_bus=bus, project_id="proj-wild"
        )

        # 3) PR event (mocked)
        async def _mock_subprocess(args, cwd=None, timeout=None):
            class _Result:
                returncode = 0
                stdout = "https://github.com/test/repo/pull/7"
                stderr = ""

            return _Result()

        monkeypatch.setattr(mgr, "_arun_subprocess", _mock_subprocess)
        await mgr.acreate_pr(
            clone, "task/wild-integ", "Wild PR", "Body",
            event_bus=bus, project_id="proj-wild",
        )

        event_types = [e["_event_type"] for e in all_events]
        assert "git.commit" in event_types
        assert "git.push" in event_types
        assert "git.pr.created" in event_types

    @pytest.mark.asyncio
    async def test_filtered_subscriber(self, clone, mgr, bus):
        """A subscriber with a filter only receives matching events."""
        proj_a_events: list[dict] = []
        proj_b_events: list[dict] = []

        bus.subscribe(
            "git.commit", lambda d: proj_a_events.append(d),
            filter={"project_id": "proj-a"},
        )
        bus.subscribe(
            "git.commit", lambda d: proj_b_events.append(d),
            filter={"project_id": "proj-b"},
        )

        pathlib.Path(clone, "fa.txt").write_text("a")
        await mgr.acommit_all(
            clone, "commit for a", event_bus=bus, project_id="proj-a"
        )

        pathlib.Path(clone, "fb.txt").write_text("b")
        await mgr.acommit_all(
            clone, "commit for b", event_bus=bus, project_id="proj-b"
        )

        assert len(proj_a_events) == 1
        assert proj_a_events[0]["project_id"] == "proj-a"
        assert len(proj_b_events) == 1
        assert proj_b_events[0]["project_id"] == "proj-b"

    @pytest.mark.asyncio
    async def test_async_subscriber_receives_event(self, clone, mgr, bus):
        """Async handler is properly awaited and receives the event."""
        received: list[dict] = []

        async def async_handler(data):
            received.append(data)

        bus.subscribe("git.commit", async_handler)

        pathlib.Path(clone, "async_sub.txt").write_text("async")
        await mgr.acommit_all(
            clone, "async subscriber test", event_bus=bus, project_id="p1"
        )

        assert len(received) == 1
        assert received[0]["message"] == "async subscriber test"

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive(self, clone, mgr, bus):
        """Multiple subscribers to the same event all receive it."""
        lists = [[] for _ in range(5)]
        for lst in lists:
            bus.subscribe("git.commit", lambda d, target=lst: target.append(d))

        pathlib.Path(clone, "multi_sub.txt").write_text("multi")
        await mgr.acommit_all(
            clone, "multi subscriber", event_bus=bus, project_id="p1"
        )

        for lst in lists:
            assert len(lst) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_receiving(self, clone, mgr, bus):
        """After unsubscribe(), handler no longer receives events."""
        received: list[dict] = []
        unsub = bus.subscribe("git.commit", lambda d: received.append(d))

        pathlib.Path(clone, "before_unsub.txt").write_text("before")
        await mgr.acommit_all(
            clone, "before unsub", event_bus=bus, project_id="p1"
        )
        assert len(received) == 1

        unsub()

        pathlib.Path(clone, "after_unsub.txt").write_text("after")
        await mgr.acommit_all(
            clone, "after unsub", event_bus=bus, project_id="p1"
        )
        assert len(received) == 1  # Still 1, not 2


# ===========================================================================
# Event emission resilience (handler failures don't break git ops)
# ===========================================================================


class TestEventEmissionResilience:
    """Verify that event emission failures never break git operations."""

    @pytest.mark.asyncio
    async def test_commit_survives_bad_handler(self, clone, mgr, bus):
        """Commit succeeds even when event handler raises."""
        async def bad_handler(data):
            raise RuntimeError("handler exploded")

        bus.subscribe("git.commit", bad_handler)

        pathlib.Path(clone, "resilient_c.txt").write_text("resilient")
        committed = await mgr.acommit_all(
            clone, "resilient commit", event_bus=bus, project_id="p1"
        )
        assert committed is True

    @pytest.mark.asyncio
    async def test_push_survives_bad_handler(self, clone, mgr, bus):
        """Push succeeds even when event handler raises."""
        async def bad_handler(data):
            raise RuntimeError("handler exploded")

        bus.subscribe("git.push", bad_handler)

        await mgr.aprepare_for_task(clone, "task/resilient-push")
        _commit_file(clone, "rp.txt", "data", "resilient push")
        await mgr.apush_branch(
            clone, "task/resilient-push", event_bus=bus, project_id="p1"
        )

    @pytest.mark.asyncio
    async def test_pr_survives_bad_handler(self, clone, mgr, bus, monkeypatch):
        """PR creation succeeds even when event handler raises."""
        async def bad_handler(data):
            raise RuntimeError("handler exploded")

        bus.subscribe("git.pr.created", bad_handler)

        async def _mock_subprocess(args, cwd=None, timeout=None):
            class _Result:
                returncode = 0
                stdout = "https://github.com/test/repo/pull/1"
                stderr = ""

            return _Result()

        monkeypatch.setattr(mgr, "_arun_subprocess", _mock_subprocess)

        url = await mgr.acreate_pr(
            clone, "b", "T", "B", event_bus=bus, project_id="p1"
        )
        assert "pull" in url


# ===========================================================================
# (g) Concurrent git operations from different agents emit separate
#     events with correct agent_id isolation
# ===========================================================================


class TestConcurrentAgentIsolation:
    """Multiple agents committing concurrently should each get their own
    event with the correct agent_id."""

    @pytest.fixture
    def second_clone(self, tmp_path, bare_repo):
        """A second clone of the same bare repo for concurrent agent testing."""
        clone_path = str(tmp_path / "clone2")
        subprocess.run(
            ["git", "clone", bare_repo, clone_path], check=True, capture_output=True
        )
        _git(["config", "user.name", "Test2"], cwd=clone_path)
        _git(["config", "user.email", "t2@t.com"], cwd=clone_path)
        return clone_path

    @pytest.mark.asyncio
    async def test_concurrent_commits_isolated_agent_ids(
        self, clone, second_clone, mgr, bus
    ):
        """Two concurrent acommit_all() calls emit events with distinct agent_ids."""
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        # Prepare different branches in different workspaces
        await mgr.acreate_branch(clone, "agent-1-branch")
        await mgr.acreate_branch(second_clone, "agent-2-branch")

        pathlib.Path(clone, "agent1.txt").write_text("agent 1 work")
        pathlib.Path(second_clone, "agent2.txt").write_text("agent 2 work")

        # Run both commits concurrently
        results = await asyncio.gather(
            mgr.acommit_all(
                clone, "agent 1 commit",
                event_bus=bus, project_id="proj-1", agent_id="agent-1",
            ),
            mgr.acommit_all(
                second_clone, "agent 2 commit",
                event_bus=bus, project_id="proj-1", agent_id="agent-2",
            ),
        )

        assert results[0] is True
        assert results[1] is True
        assert len(received) == 2

        agent_ids = {evt["agent_id"] for evt in received}
        assert agent_ids == {"agent-1", "agent-2"}

        # Each event has the correct message for its agent
        for evt in received:
            if evt["agent_id"] == "agent-1":
                assert evt["message"] == "agent 1 commit"
                assert "agent1.txt" in evt["changed_files"]
            elif evt["agent_id"] == "agent-2":
                assert evt["message"] == "agent 2 commit"
                assert "agent2.txt" in evt["changed_files"]

    @pytest.mark.asyncio
    async def test_concurrent_pushes_isolated_events(
        self, clone, second_clone, mgr, bus
    ):
        """Two concurrent apush_branch() calls emit events with distinct data."""
        received: list[dict] = []
        bus.subscribe("git.push", lambda data: received.append(data))

        await mgr.aprepare_for_task(clone, "agent-1-push")
        await mgr.aprepare_for_task(second_clone, "agent-2-push")

        _commit_file(clone, "p1.txt", "data", "agent 1 push content")
        _commit_file(second_clone, "p2.txt", "data", "agent 2 push content")

        await asyncio.gather(
            mgr.apush_branch(
                clone, "agent-1-push",
                event_bus=bus, project_id="proj-1",
            ),
            mgr.apush_branch(
                second_clone, "agent-2-push",
                event_bus=bus, project_id="proj-2",
            ),
        )

        assert len(received) == 2

        branches = {evt["branch"] for evt in received}
        assert branches == {"agent-1-push", "agent-2-push"}

        project_ids = {evt["project_id"] for evt in received}
        assert project_ids == {"proj-1", "proj-2"}

    @pytest.mark.asyncio
    async def test_concurrent_mixed_operations(
        self, clone, second_clone, mgr, bus, monkeypatch
    ):
        """Concurrent commit + push + PR emit separate, correctly typed events."""
        all_events: list[dict] = []
        bus.subscribe("*", lambda data: all_events.append(data))

        # Set up the commit workspace
        await mgr.acreate_branch(clone, "mixed-commit")
        pathlib.Path(clone, "mixed.txt").write_text("mixed")

        # Set up the push workspace
        await mgr.aprepare_for_task(second_clone, "mixed-push")
        _commit_file(second_clone, "mp.txt", "data", "for push")

        # We need a separate GitManager for the PR mock to not interfere
        pr_mgr = GitManager()

        async def _mock_subprocess(args, cwd=None, timeout=None):
            class _Result:
                returncode = 0
                stdout = "https://github.com/test/repo/pull/55"
                stderr = ""

            return _Result()

        monkeypatch.setattr(pr_mgr, "_arun_subprocess", _mock_subprocess)

        await asyncio.gather(
            mgr.acommit_all(
                clone, "mixed commit",
                event_bus=bus, project_id="proj-m", agent_id="agent-commit",
            ),
            mgr.apush_branch(
                second_clone, "mixed-push",
                event_bus=bus, project_id="proj-m",
            ),
            pr_mgr.acreate_pr(
                clone, "mixed-pr", "Mixed PR", "Body",
                event_bus=bus, project_id="proj-m",
            ),
        )

        event_types = [e["_event_type"] for e in all_events]
        assert "git.commit" in event_types
        assert "git.push" in event_types
        assert "git.pr.created" in event_types

        # Verify each event has correct project_id
        for evt in all_events:
            assert evt["project_id"] == "proj-m"

    @pytest.mark.asyncio
    async def test_many_concurrent_commits_all_emit(
        self, bare_repo, tmp_path, mgr, bus
    ):
        """N concurrent commits from different workspaces each emit exactly one event."""
        n_agents = 5
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        # Seed the bare repo with an initial commit so clones have history
        # (git diff-tree needs a parent commit to list changed files)
        seed = str(tmp_path / "seed")
        subprocess.run(
            ["git", "clone", bare_repo, seed], check=True, capture_output=True
        )
        _git(["config", "user.name", "Seed"], cwd=seed)
        _git(["config", "user.email", "s@t.com"], cwd=seed)
        pathlib.Path(seed, "README.md").write_text("seed")
        _git(["add", "."], cwd=seed)
        _git(["commit", "-m", "seed"], cwd=seed)
        _git(["push", "origin", "main"], cwd=seed)

        # Create N separate clones
        clones = []
        for i in range(n_agents):
            cp = str(tmp_path / f"clone_{i}")
            subprocess.run(
                ["git", "clone", bare_repo, cp], check=True, capture_output=True
            )
            _git(["config", "user.name", f"Agent{i}"], cwd=cp)
            _git(["config", "user.email", f"a{i}@t.com"], cwd=cp)
            await mgr.acreate_branch(cp, f"agent-{i}-branch")
            pathlib.Path(cp, f"file_{i}.txt").write_text(f"content {i}")
            clones.append(cp)

        # Commit all concurrently
        results = await asyncio.gather(
            *[
                mgr.acommit_all(
                    clones[i], f"agent {i} commit",
                    event_bus=bus, project_id=f"proj-{i}", agent_id=f"agent-{i}",
                )
                for i in range(n_agents)
            ]
        )

        assert all(r is True for r in results)
        assert len(received) == n_agents

        # Each agent_id should appear exactly once
        agent_ids = [evt["agent_id"] for evt in received]
        assert sorted(agent_ids) == [f"agent-{i}" for i in range(n_agents)]

        # Each event has the correct file in changed_files
        for evt in received:
            idx = int(evt["agent_id"].split("-")[1])
            assert f"file_{idx}.txt" in evt["changed_files"]
