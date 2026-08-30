"""Session commands, the completion protocol, and the C1 end-to-end path.

``TestEndToEndOnFakeProvider`` is checkpoint C1: a task is launched through
a session provider, closes itself with ``aq task close``, acks the drain,
and the reconciler reaps the session — with no tmux and no WSL anywhere.

See docs/specs/implementation/session-runtime.md §3.8, §8.
"""

from __future__ import annotations

import time

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from src.scheduler import AssignAction
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.harness_registry import HarnessRegistry, load_from_vault
from src.sessions.reconciler import DRAIN_ACK_KEY, SessionReconciler
from src.sessions.spec import SessionSpecBuilder


class _Bus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, payload=None):
        self.events.append((event_type, dict(payload or {})))

    def types(self):
        return [t for t, _ in self.events]


class _StubOrchestrator:
    """Just enough orchestrator for the command mixin and the launch fork.

    Deliberately not a mock of ``complete_session_task``: the real method is
    bound in from ``ExecutionMixin`` in the end-to-end test, because the
    point of C1 is that the *real* close path runs.
    """

    def __init__(self, db, config, providers, harnesses):
        self.db = db
        self.config = config
        self.bus = _Bus()
        self.session_providers = providers
        self.harness_registry = harnesses
        self.session_spec_builder = SessionSpecBuilder(config, harnesses)
        self.daemon_epoch = "epoch-test"
        self._adapters = {}
        self._task_exec_start = {}
        self._task_pre_exec_sha = {}
        self.closed_calls: list[dict] = []
        self._task_attachments = {}
        self._task_added_messages = {}
        # ``_execute_task`` guards on this: with sessions enabled and no
        # runtimes registry it must still run, because a session-routed
        # task never constructs a runtime.
        self._runtimes = None
        self.llm_logger = None
        self.session_reconciler = SessionReconciler(
            db, config, providers, harnesses=harnesses, bus=self.bus, epoch="epoch-test"
        )

    async def _resolve_profile(self, task):
        # The real cascade lives on the Orchestrator, not on a mixin.  This
        # is the task-level leg of it, which is the one C1 exercises.
        return await self.db.get_profile(task.profile_id) if task.profile_id else None

    async def _check_constraints_before_assignment(self, action):
        return None

    async def complete_session_task(self, task, **kwargs):
        self.closed_calls.append({"task_id": task.id, **kwargs})
        status = (
            TaskStatus.COMPLETED if kwargs.get("outcome") == "pass" else TaskStatus.FAILED
        )
        await self.db.transition_task(task.id, status, context="session_close")
        return {"status": status.value, "pr_url": None, "pipeline_ok": True}


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def providers(provider):
    class _Reg(SessionProviderRegistry):
        def create(self, name, config=None):
            return provider

    return _Reg({"fake": FakeProvider})


@pytest.fixture
def harnesses(tmp_path):
    from src.vault import ensure_default_harnesses

    ensure_default_harnesses(str(tmp_path))
    registry = HarnessRegistry()
    load_from_vault(registry, str(tmp_path / "vault"))
    return registry


@pytest.fixture
def config(tmp_path):
    cfg = AppConfig()
    cfg.data_dir = str(tmp_path)
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    return cfg


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


@pytest.fixture
def orch(db, config, providers, harnesses):
    return _StubOrchestrator(db, config, providers, harnesses)


@pytest.fixture
def handler(orch, config):
    return CommandHandler(orch, config)


async def _make_session(
    db,
    provider,
    *,
    sid="sess-1",
    task_id="t1",
    state="running",
    lifecycle="task",
    name=None,
    desired_state="running",
):
    from src.sessions.provider import SessionSpec

    name = name or f"s-{task_id}"
    await provider.start(
        SessionSpec(
            session_name=name, work_dir="/wd", command=("claude",), instance_token="tok-1"
        )
    )
    row = SessionRecord(
        id=sid,
        project_id="p1",
        profile_id="claude-opus",
        harness="claude",
        provider="fake",
        name=name,
        lifecycle=lifecycle,
        work_dir="/wd",
        epoch="epoch-test",
        instance_token="tok-1",
        started_at=time.time(),
        last_activity=time.time(),
        task_id=task_id,
        state=state,
        desired_state=desired_state,
    )
    await db.create_session(row)
    return row


async def _make_task(db, task_id="t1", status=TaskStatus.IN_PROGRESS, agent_id=None):
    await db.create_task(Task(id=task_id, project_id="p1", title="T", description="d"))
    await db.transition_task(task_id, status, assigned_agent_id=agent_id)
    return await db.get_task(task_id)


# ---------------------------------------------------------------------------
# Operator surface
# ---------------------------------------------------------------------------


class TestSessionList:
    async def test_empty(self, handler):
        result = await handler.execute("session_list", {})
        assert result["success"] is True and result["count"] == 0

    async def test_lists_and_derives_stalled(self, handler, db, provider, config):
        await _make_task(db)
        row = await _make_session(db, provider)
        await db.touch_session_activity(row.id, time.time() - 10_000)
        config.sessions.lease_ttl_seconds = 480
        result = await handler.execute("session_list", {})
        entry = result["sessions"][0]
        assert entry["id"] == "sess-1"
        assert entry["harness"] == "claude"
        # "stalled" is derived from the lease TTL, never stored.
        assert entry["stalled"] is True
        assert entry["idle_seconds"] > 480
        assert entry["state"] == "running"

    async def test_not_stalled_within_the_lease(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        entry = (await handler.execute("session_list", {}))["sessions"][0]
        assert entry["stalled"] is False

    async def test_live_only_filter(self, handler, db, provider):
        await _make_task(db)
        row = await _make_session(db, provider)
        await db.update_session(row.id, state="stopped")
        assert (await handler.execute("session_list", {"live_only": True}))["count"] == 0
        assert (await handler.execute("session_list", {}))["count"] == 1


class TestSessionResolution:
    async def test_resolve_by_id(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute("session_show", {"session_id": "sess-1"})
        assert r["session"]["id"] == "sess-1"

    async def test_resolve_by_name_via_the_id_argument(self, handler, db, provider):
        """Operators paste names as often as ids."""
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute("session_show", {"session_id": "s-t1"})
        assert r["session"]["id"] == "sess-1"

    async def test_resolve_by_task_id(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute("session_show", {"task_id": "t1"})
        assert r["session"]["id"] == "sess-1"

    async def test_missing_identifier_is_an_error(self, handler):
        assert "error" in await handler.execute("session_show", {})

    async def test_unknown_session_is_an_error(self, handler):
        r = await handler.execute("session_show", {"session_id": "ghost"})
        assert "error" in r and "ghost" in r["error"]


class TestPeekNudgeAttachKill:
    async def test_peek_returns_output(self, handler, db, provider):
        await _make_task(db)
        row = await _make_session(db, provider)
        provider.feed_output(row.name, "line one")
        provider.feed_output(row.name, "line two")
        r = await handler.execute("session_peek", {"session_id": "sess-1"})
        assert r["success"] is True and "line two" in r["output"]

    async def test_nudge_delivers(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute(
            "session_nudge", {"session_id": "sess-1", "text": "status?"}
        )
        assert r["success"] is True and r["delivered"] is True
        assert provider.sent_nudges == [("s-t1", "status?")]

    async def test_nudge_surfaces_not_submitted_as_a_typed_failure(
        self, handler, db, provider
    ):
        await _make_task(db)
        row = await _make_session(db, provider)
        provider.swallow_next_nudge(row.name)
        r = await handler.execute("session_nudge", {"session_id": "sess-1", "text": "hi"})
        assert r["success"] is False and r["error"] == "not_submitted"

    async def test_nudge_requires_text(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        assert "error" in await handler.execute("session_nudge", {"session_id": "sess-1"})

    async def test_attach_returns_a_command(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute("session_attach", {"session_id": "sess-1"})
        assert r["success"] is True and isinstance(r["attach_command"], str)

    async def test_logs_labels_its_source_honestly(self, handler, db, provider):
        await _make_task(db)
        row = await _make_session(db, provider)
        provider.feed_output(row.name, "output")
        r = await handler.execute("session_logs", {"session_id": "sess-1"})
        assert r["source"] == "peek"

    async def test_kill_stops_without_transitioning_the_task(self, handler, db, provider):
        """A human killing a session must never mark a task complete."""
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute("session_kill", {"session_id": "sess-1"})
        assert r["success"] is True
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        # The process is gone from the provider...
        assert await provider.list_running("s-") == []

    async def test_kill_leaves_the_row_live_so_the_classifier_can_run(
        self, handler, db, provider
    ):
        """B2: writing ``state="stopped"`` here guaranteed the opposite.

        ``_step_exits`` iterates live rows only, so dropping the row out of
        ``_LIVE_STATES`` at kill time meant no tick ever classified the
        exit -- the task stayed IN_PROGRESS forever, the agent BUSY and the
        workspace locked, exactly the outcome the docstring promised would
        not happen.  The row must stay live until the ``process_alive``
        probe says otherwise.
        """
        await _make_task(db)
        await _make_session(db, provider)
        await handler.execute("session_kill", {"session_id": "sess-1"})
        assert (await db.get_session("sess-1")).state == "running"


class TestDesiredStateCommands:
    """Intent is separate from observation — see the design spec."""

    async def test_kill_records_that_the_session_is_not_wanted(
        self, handler, db, provider
    ):
        """Otherwise up-convergence restarts what an operator just killed."""
        await _make_task(db)
        await _make_session(db, provider)
        await handler.execute("session_kill", {"session_id": "sess-1"})
        row = await db.get_session("sess-1")
        # Intent moved; observed state deliberately did not (see B2 above).
        assert row.desired_state == "stopped" and row.state == "running"

    async def test_sleep_sets_intent_without_signalling(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute("session_sleep", {"session_id": "sess-1"})
        assert r["success"] is True and r["desired_state"] == "sleeping"
        assert (await db.get_session("sess-1")).desired_state == "sleeping"
        # Still running: sleep is a statement of intent, not a signal.
        assert [h.name for h in await provider.list_running("s-")] == ["s-t1"]

    async def test_wake_marks_a_named_session_wanted(self, handler, db, provider):
        await _make_session(
            db,
            provider,
            sid="n1",
            task_id=None,
            name="n-supervisor--p1",
            lifecycle="named",
            state="sleeping",
            desired_state="sleeping",
        )
        r = await handler.execute("session_wake", {"session_id": "n1"})
        assert r["success"] is True
        row = await db.get_session("n1")
        assert row.desired_state == "running"
        # The restart budget counts *consecutive* failed starts; an
        # operator asking for a wake is a fresh intent, not a retry.
        assert row.restarts == 0

    async def test_wake_refuses_task_sessions(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider, state="sleeping")
        r = await handler.execute("session_wake", {"session_id": "sess-1"})
        assert r["success"] is False
        assert (await db.get_session("sess-1")).desired_state == "running"

    async def test_list_reports_intent(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        await handler.execute("session_sleep", {"session_id": "sess-1"})
        entry = (await handler.execute("session_list", {}))["sessions"][0]
        assert entry["state"] == "running" and entry["desired_state"] == "sleeping"


class TestDrainAckCommand:
    async def test_marks_the_session_draining(self, handler, db, provider):
        await _make_task(db)
        row = await _make_session(db, provider)
        r = await handler.execute("session_drain_ack", {"session_id": "sess-1"})
        assert r["success"] is True and r["state"] == "draining"
        assert (await db.get_session("sess-1")).state == "draining"
        handle = handler._session_handle(row)
        assert await provider.get_meta(handle, DRAIN_ACK_KEY) == "1"

    async def test_unknown_session(self, handler):
        assert "error" in await handler.execute("session_drain_ack", {"session_id": "x"})


class TestSessionToken:
    """``session_token`` — the dev/e2e credential minter.

    Exists so ``scripts/e2e-smoke.sh`` can act as a pool worker while
    ``sessions.provider: fake`` means no real agent is running.
    """

    @pytest.fixture
    def store(self, db, orch):
        from src.api.auth import SessionTokenStore

        orch.token_store = SessionTokenStore(db)
        return orch.token_store

    async def test_mints_a_usable_token_for_a_task_session(
        self, handler, db, provider, store
    ):
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute("session_token", {"session_id": "sess-1"})
        assert r["success"] is True
        assert r["token"].startswith("aqs_")
        scope = await store.validate(r["token"])
        assert scope is not None
        assert (scope.session_id, scope.project_id, scope.task_id) == ("sess-1", "p1", "t1")
        # Never elevated: the minted token is the worker's own scope, not
        # the operator's.
        assert scope.elevated is False

    async def test_pool_session_token_pins_no_task(self, handler, db, provider, store):
        """A pool worker's task changes with every claim, so it is not pinned.

        Mirrors what ``PoolsMixin._launch_pool_session`` mints.
        """
        await _make_task(db)
        await _make_session(db, provider, lifecycle="pool")
        r = await handler.execute("session_token", {"session_id": "sess-1"})
        scope = await store.validate(r["token"])
        assert r["task_id"] is None
        assert scope.task_id is None and scope.project_id == "p1"

    async def test_each_call_mints_a_fresh_token(self, handler, db, provider, store):
        await _make_task(db)
        await _make_session(db, provider)
        first = (await handler.execute("session_token", {"session_id": "sess-1"}))["token"]
        second = (await handler.execute("session_token", {"session_id": "sess-1"}))["token"]
        assert first != second
        assert await store.validate(first) is not None
        assert await store.validate(second) is not None

    async def test_unknown_session(self, handler, store):
        assert "error" in await handler.execute("session_token", {"session_id": "nope"})

    async def test_without_a_token_store(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute("session_token", {"session_id": "sess-1"})
        assert r["success"] is False and "token store" in r["error"]

    def test_is_not_reachable_with_an_agent_token(self):
        """A session token must never mint another session's token.

        ``check_command_scope`` gates a plain session scope on
        ``AGENT_COMMAND_SET``; keeping ``session_token`` out of that set is
        the whole enforcement, so guard it here rather than trusting a
        comment.
        """
        from src.api.auth import RequestScope
        from src.api.scope import AGENT_COMMAND_SET, check_command_scope

        assert "session_token" not in AGENT_COMMAND_SET
        agent = RequestScope(kind="session", session_id="sess-1", project_id="p1")
        assert check_command_scope("session_token", {}, agent) is not None
        # Elevated (supervisor) and local callers are allowed through.
        elevated = RequestScope(
            kind="session", session_id="sup", project_id="p1", elevated=True
        )
        assert check_command_scope("session_token", {}, elevated) is None

    def test_is_excluded_from_mcp(self):
        """A credential minter is not an MCP tool, even for a trusted client."""
        from src.mcp_registration import get_effective_exclusions

        assert "session_token" in get_effective_exclusions()


# ---------------------------------------------------------------------------
# task_close / task_heartbeat
# ---------------------------------------------------------------------------


class TestTaskClose:
    async def test_happy_path(self, handler, db, provider, orch):
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": "sess-1",
                "outcome": "pass",
                "work_outcome": "shipped",
                "commit": "abc123",
                "notes": "Done: wired the thing",
            },
        )
        assert r["success"] is True
        assert r["next_step"].startswith("run `aq session drain-ack`")
        assert await db.get_task_meta("t1", "outcome") == "pass"
        assert await db.get_task_meta("t1", "work_outcome") == "shipped"
        assert await db.get_task_meta("t1", "work_commit") == "abc123"
        assert await db.get_task_meta("t1", "close_notes") == "Done: wired the thing"
        assert await db.get_task_meta("t1", "close_session_id") == "sess-1"
        assert orch.closed_calls[0]["outcome"] == "pass"

    async def test_metadata_keys_follow_the_work_graph_contract(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        await handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "outcome": "fail",
                "failure_class": "hard",
                "work_outcome": "blocked",
                "verification": "pytest: 3 failures",
            },
        )
        assert await db.get_task_meta("t1", "failure_class") == "hard"
        assert await db.get_task_meta("t1", "verification") == "pytest: 3 failures"

    async def test_missing_task_id(self, handler):
        assert "error" in await handler.execute("task_close", {"outcome": "pass"})

    async def test_invalid_outcome_is_rejected(self, handler, db, provider):
        await _make_task(db)
        r = await handler.execute("task_close", {"task_id": "t1", "outcome": "maybe"})
        assert "error" in r and "outcome must be" in r["error"]

    async def test_invalid_failure_class_is_rejected(self, handler, db):
        await _make_task(db)
        r = await handler.execute(
            "task_close",
            {"task_id": "t1", "outcome": "fail", "failure_class": "kinda"},
        )
        assert "error" in r and "failure_class" in r["error"]

    async def test_invalid_work_outcome_is_rejected(self, handler, db):
        await _make_task(db)
        r = await handler.execute(
            "task_close", {"task_id": "t1", "outcome": "pass", "work_outcome": "vibes"}
        )
        assert "error" in r and "work_outcome" in r["error"]

    async def test_unknown_task(self, handler):
        r = await handler.execute("task_close", {"task_id": "ghost", "outcome": "pass"})
        assert "error" in r and "ghost" in r["error"]

    async def test_a_task_that_is_not_running_cannot_be_closed(self, handler, db):
        await _make_task(db, status=TaskStatus.READY)
        r = await handler.execute("task_close", {"task_id": "t1", "outcome": "pass"})
        assert "error" in r and "not in progress" in r["error"]

    async def test_assigned_is_closeable_so_the_protocol_is_not_racy(self, handler, db, provider):
        await _make_task(db, status=TaskStatus.ASSIGNED)
        await _make_session(db, provider)
        r = await handler.execute(
            "task_close", {"task_id": "t1", "outcome": "pass", "session_id": "sess-1"}
        )
        assert r["success"] is True

    async def test_close_from_the_wrong_session_is_refused(self, handler, db, provider):
        await _make_task(db, task_id="t1")
        await _make_task(db, task_id="t2")
        await _make_session(db, provider, sid="sess-1", task_id="t1")
        await _make_session(db, provider, sid="sess-2", task_id="t2")
        r = await handler.execute(
            "task_close", {"task_id": "t1", "outcome": "pass", "session_id": "sess-2"}
        )
        assert "error" in r
        assert "refusing to close another task's work" in r["error"]
        # ...and nothing was written.
        assert await db.get_task_meta("t1", "outcome") is None

    async def test_unknown_calling_session_is_refused(self, handler, db):
        await _make_task(db)
        r = await handler.execute(
            "task_close", {"task_id": "t1", "outcome": "pass", "session_id": "ghost"}
        )
        assert "error" in r

    async def test_close_without_a_session_still_works(self, handler, db):
        """MCP and the dashboard can close a task with no session in scope."""
        await _make_task(db)
        r = await handler.execute("task_close", {"task_id": "t1", "outcome": "pass"})
        assert r["success"] is True


class TestTaskHeartbeat:
    async def test_touches_session_and_agent(self, handler, db, provider):
        await db.create_profile(AgentProfile(id="claude-opus", name="Claude"))
        await db.create_agent(
            Agent(id="a1", name="agent-1", profile_id="claude-opus", state=AgentState.BUSY)
        )
        await _make_task(db, agent_id="a1")
        await _make_session(db, provider)
        await db.touch_session_activity("sess-1", 1.0)
        r = await handler.execute("task_heartbeat", {"task_id": "t1"})
        assert r["success"] is True
        assert r["session_id"] == "sess-1"
        assert r["lease_expires_at"] > r["heartbeat_at"]
        assert (await db.get_session("sess-1")).last_activity > 1.0
        assert (await db.get_agent("a1")).last_heartbeat is not None

    async def test_resolves_the_task_from_the_session(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider)
        r = await handler.execute("task_heartbeat", {"session_id": "sess-1"})
        assert r["success"] is True and r["task_id"] == "t1"

    async def test_no_scope_is_an_error(self, handler):
        assert "error" in await handler.execute("task_heartbeat", {})

    async def test_unknown_task(self, handler):
        assert "error" in await handler.execute("task_heartbeat", {"task_id": "ghost"})

    async def test_works_without_a_session_row(self, handler, db):
        await _make_task(db)
        r = await handler.execute("task_heartbeat", {"task_id": "t1"})
        assert r["success"] is True and r["session_id"] is None


# ---------------------------------------------------------------------------
# Checkpoint C1
# ---------------------------------------------------------------------------


class TestEndToEndOnFakeProvider:
    """C1: launch → work → close → drain-ack → reaped.  No tmux, no WSL."""

    @pytest.fixture
    def real_orch(self, db, config, providers, harnesses, tmp_path):
        """An orchestrator with the *real* execution and workspace mixins.

        What is stubbed is deliberately minimal, because C1's claim is that
        the whole path runs:

        * ``_emit_text_notify`` / ``_emit_notify`` — Discord I/O;
        * ``_run_completion_pipeline`` — git/commit/PR, whose *return value*
          the tests drive so both the ``(None, True)`` and the pipeline-STOP
          branch are exercised;
        * ``_get_default_branch`` — needs a real repo.

        **Workspace release is not stubbed.**  ``_release_workspaces_for_task``
        comes from the real ``WorkspaceMixin`` and writes to the database,
        so every assertion about a released workspace re-reads the row
        instead of trusting a recorder list.  The recorder is what let B1
        (every reconciler exit path leaking the agent and the lock) sit
        under a green test.
        """
        from src.orchestrator.execution import ExecutionMixin
        from src.orchestrator.workspace import WorkspaceMixin

        class _Orch(ExecutionMixin, WorkspaceMixin, _StubOrchestrator):
            #: Pipeline verdict the next close should see: (pr_url, ok).
            pipeline_result = (None, True)

            async def _emit_text_notify(self, *a, **k):
                self.text_notifies = getattr(self, "text_notifies", [])
                self.text_notifies.append((a, k))

            async def _emit_notify(self, event_type, payload=None):
                await self.bus.emit(event_type, {"event": event_type})

            async def _emit_task_event(self, event_type, task, **extra):
                await self.bus.emit(
                    event_type,
                    {"task_id": task.id, "project_id": task.project_id, **extra},
                )

            async def _get_default_branch(self, project, path):
                return "main"

            async def _run_completion_pipeline(self, ctx):
                self.pipeline_ran = True
                return self.pipeline_result

            # Not mocks: the real ones are inherited from ExecutionMixin.
            complete_session_task = ExecutionMixin.complete_session_task
            release_session_task_resources = (
                ExecutionMixin.release_session_task_resources
            )
            _release_workspaces_for_task = WorkspaceMixin._release_workspaces_for_task

        orch = _Orch(db, config, providers, harnesses)
        orch.session_reconciler.orchestrator = orch
        return orch

    @pytest.fixture
    def real_handler(self, real_orch, config):
        handler = CommandHandler(real_orch, config)
        real_orch._command_handler = handler
        return handler

    async def _setup(self, db, tmp_path, *, ready=False):
        """Profile + agent + task + a locked workspace row.

        ``ready=True`` leaves the task READY and the agent IDLE so
        ``_execute_task`` can do the assigning itself — that is the entry
        point C1 is supposed to exercise, and it is where the fork,
        ``platform = None`` and workspace preparation actually run.
        """
        await db.create_profile(
            AgentProfile(
                id="claude-opus", name="Claude Opus", harness="claude", lifecycle="task"
            )
        )
        await db.create_agent(
            Agent(
                id="a1",
                name="agent-1",
                profile_id="claude-opus",
                state=AgentState.IDLE if ready else AgentState.BUSY,
            )
        )
        wd = tmp_path / "wd"
        wd.mkdir(exist_ok=True)
        # Task before workspace: the workspace lock carries an FK to it.
        await db.create_task(
            Task(id="t1", project_id="p1", title="Do the thing", description="d",
                 profile_id="claude-opus")
        )
        if ready:
            await db.transition_task("t1", TaskStatus.READY)
            await db.create_workspace(
                Workspace(
                    id="ws1",
                    project_id="p1",
                    workspace_path=str(wd),
                    source_type=RepoSourceType.LINK,
                    name="main",
                )
            )
        else:
            await db.transition_task("t1", TaskStatus.IN_PROGRESS, assigned_agent_id="a1")
            await db.create_workspace(
                Workspace(
                    id="ws1",
                    project_id="p1",
                    workspace_path=str(wd),
                    source_type=RepoSourceType.LINK,
                    name="main",
                    locked_by_agent_id="a1",
                    locked_by_task_id="t1",
                )
            )
        return str(wd)

    async def _launch_via_execute_task(self, db, real_orch, monkeypatch, tmp_path):
        """Drive a launch through the *real* ``_execute_task`` entry point.

        Calling ``_launch_session_for_task`` directly (what this test used
        to do) skips the three things the fork is actually about: the
        routing decision, ``platform = None`` for a session-routed task
        (so no runtime adapter is ever constructed), and workspace prep.
        """
        wd = await self._setup(db, tmp_path, ready=True)

        # Point workspace preparation at the row we created rather than at
        # git.  Everything after it -- the fork, the launch, the row insert
        # -- is real.
        async def _prepare(task, agent):
            await db.update_workspace(
                "ws1", locked_by_agent_id="a1", locked_by_task_id=task.id
            )
            return wd

        monkeypatch.setattr(real_orch, "_prepare_workspace", _prepare)

        action = AssignAction(task_id="t1", agent_id="a1", project_id="p1")
        await real_orch._execute_task(action)
        return wd

    async def test_routing_rule_needs_both_flag_and_harness(self, real_orch, config):
        profile = AgentProfile(id="x", name="x", harness="claude")
        assert real_orch._is_session_routed(profile) is True

        config.sessions.enabled = False
        assert real_orch._is_session_routed(profile) is False

        config.sessions.enabled = True
        assert real_orch._is_session_routed(AgentProfile(id="y", name="y")) is False
        assert real_orch._is_session_routed(None) is False

    async def test_full_lifecycle(
        self, db, real_orch, real_handler, provider, tmp_path, config, monkeypatch
    ):
        # 1. Launch through the real ``_execute_task`` — routing fork,
        #    ``platform = None``, workspace prep, then return immediately
        #    with no stream to block on.
        wd = await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)

        # A session-routed task must never construct a runtime adapter --
        # that is what ``platform = None`` is for, and an adapter registered
        # here would make ``stop_task`` believe it has something to cancel.
        assert real_orch._adapters == {}

        session = await db.get_session_for_task("t1")
        assert session is not None
        assert session.name == "s-t1"
        assert session.state == "running"
        assert session.provider == "fake"
        assert session.harness == "claude"
        assert session.epoch == "epoch-test"
        assert "session.started" in real_orch.bus.types()
        # The task is still IN_PROGRESS: launching is not completing.
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        # The workspace is really locked, in the database, by this task.
        ws = await db.get_workspace("ws1")
        assert ws.locked_by_task_id == "t1" and ws.locked_by_agent_id == "a1"
        # H2: the resume key is persisted at launch, not left None.
        assert session.session_key == session.id

        # The spawned session got the AQ_* handshake the CLI depends on.
        spec = provider.starts[0]
        assert spec.env["AQ_TASK_ID"] == "t1"
        assert spec.env["AQ_SESSION_ID"] == session.id
        assert spec.env["AQ_INSTANCE_TOKEN"] == session.instance_token
        assert spec.env["AQ_WORK_DIR"] == wd
        assert "aq prime" in spec.prompt

        # 2. A reconciler tick with a live session changes nothing.
        await real_orch.session_reconciler.tick()
        assert (await db.get_session(session.id)).state == "running"
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS

        # 3. Heartbeat keeps the lease alive.
        hb = await real_handler.execute(
            "task_heartbeat", {"session_id": session.id}
        )
        assert hb["success"] is True

        # 4. The agent closes the task explicitly.
        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "notes": "Done: the thing is wired",
                "summary": "Wired the thing end-to-end.",
            },
        )
        assert close["success"] is True, close
        assert close["status"] == "COMPLETED"
        assert real_orch.pipeline_ran is True
        assert (await db.get_task("t1")).status is TaskStatus.COMPLETED
        assert await db.get_task_meta("t1", "outcome") == "pass"
        # The agent was freed and the workspace released at close time --
        # asserted against the database, not against a stub's bookkeeping.
        assert (await db.get_agent("a1")).state is AgentState.IDLE
        ws = await db.get_workspace("ws1")
        assert ws.locked_by_task_id is None and ws.locked_by_agent_id is None
        assert await db.get_workspace_for_task("t1") is None

        # 5. The agent acks the drain.
        ack = await real_handler.execute(
            "session_drain_ack", {"session_id": session.id}
        )
        assert ack["success"] is True
        assert (await db.get_session(session.id)).state == "draining"

        # 6. The next tick reaps it.
        await real_orch.session_reconciler.tick()
        reaped = await db.get_session(session.id)
        assert reaped.state == "stopped"
        assert "session.drain_acked" in real_orch.bus.types()
        assert await provider.list_running("s-") == []

    async def test_exit_without_close_is_never_treated_as_success(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """The whole point of the runtime: exit is a failure signal."""
        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)

        session = await db.get_session_for_task("t1")
        # The agent walks off without closing.
        provider.script_death(session.name)
        await real_orch.session_reconciler.tick()

        assert (await db.get_task("t1")).status is not TaskStatus.COMPLETED
        assert (await db.get_session(session.id)).state in ("stopped", "quarantined")
        # B1: the exit path owes the same cleanup the happy path does.
        assert (await db.get_agent("a1")).state is AgentState.IDLE
        ws = await db.get_workspace("ws1")
        assert ws.locked_by_task_id is None

    async def test_pipeline_stop_blocks_instead_of_completing(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """``outcome=pass`` is the *trigger* for the pipeline, not a verdict.

        When the completion pipeline says stop (verification reopened the
        task, work left uncommitted, ...) the task goes BLOCKED, never
        COMPLETED.  Consistent with work-graph's ``hard -> BLOCKED``: a
        human has to look.  No test executed this branch before, because
        ``_run_completion_pipeline`` was stubbed to a constant ``True``.
        """
        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")
        real_orch.pipeline_result = (None, False)

        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "summary": "Work completed; pipeline blocked on verification.",
            },
        )
        assert close["success"] is True and close["status"] == "BLOCKED"
        assert close["pipeline_ok"] is False
        assert (await db.get_task("t1")).status is TaskStatus.BLOCKED
        # The claim the agent made is still on the record.
        assert await db.get_task_meta("t1", "outcome") == "pass"
        # ...and the resources are still freed, against the database.
        assert (await db.get_agent("a1")).state is AgentState.IDLE
        assert (await db.get_workspace("ws1")).locked_by_task_id is None

    async def test_transient_failure_retries_instead_of_going_terminal(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """H5: ``outcome=fail`` + transient follows work-graph's retry contract.

        work-graph §outcome-metadata: *"transient (or absent -- legacy
        default) -> existing retry-with-backoff path"*, and that path
        increments ``retry_count`` and re-queues.  Sending it straight to
        FAILED made a session-run flake terminal where a legacy one retries.
        """
        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")

        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "fail",
                "failure_class": "transient",
                "notes": "flaky network",
                "summary": "Failed due to transient network issue.",
            },
        )
        assert close["status"] == "READY"
        task = await db.get_task("t1")
        assert task.status is TaskStatus.READY
        assert task.retry_count == 1
        # The pipeline never runs on a failure.
        assert getattr(real_orch, "pipeline_ran", False) is False
        # Resources freed so the retry can actually acquire a workspace.
        assert (await db.get_agent("a1")).state is AgentState.IDLE
        assert (await db.get_workspace("ws1")).locked_by_task_id is None

    async def test_hard_failure_blocks_and_does_not_retry(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")
        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "fail",
                "failure_class": "hard",
                "summary": "Hard failure; manual intervention required.",
            },
        )
        assert close["status"] == "BLOCKED"
        task = await db.get_task("t1")
        assert task.status is TaskStatus.BLOCKED and task.retry_count == 0

    async def test_transient_failure_blocks_once_retries_are_spent(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")
        task = await db.get_task("t1")
        await db.update_task(task.id, retry_count=task.max_retries - 1)

        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "fail",
                "summary": "Retries exhausted; task blocked.",
            },
        )
        assert close["status"] == "BLOCKED"
        assert (await db.get_task("t1")).status is TaskStatus.BLOCKED

    async def test_launch_failure_pauses_rather_than_fabricating_a_result(
        self, db, real_orch, provider, tmp_path
    ):
        wd = await self._setup(db, tmp_path)
        task = await db.get_task("t1")
        profile = await db.get_profile("claude-opus")
        action = AssignAction(task_id="t1", agent_id="a1", project_id="p1")

        provider.script_startup_death("s-t1")
        await real_orch._launch_session_for_task(action, task, profile, wd)

        assert await db.get_session_for_task("t1") is None
        task = await db.get_task("t1")
        assert task.status is TaskStatus.PAUSED
        assert (await db.get_agent("a1")).state is AgentState.IDLE

    async def test_unknown_harness_fails_the_launch_loudly(
        self, db, real_orch, provider, tmp_path
    ):
        wd = await self._setup(db, tmp_path)
        task = await db.get_task("t1")
        profile = AgentProfile(id="claude-opus", name="C", harness="does-not-exist")
        action = AssignAction(task_id="t1", agent_id="a1", project_id="p1")
        await real_orch._launch_session_for_task(action, task, profile, wd)
        assert await db.get_session_for_task("t1") is None
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED

    async def test_missing_work_dir_fails_the_launch(
        self, db, real_orch, provider, tmp_path
    ):
        await self._setup(db, tmp_path)
        task = await db.get_task("t1")
        profile = await db.get_profile("claude-opus")
        action = AssignAction(task_id="t1", agent_id="a1", project_id="p1")
        await real_orch._launch_session_for_task(action, task, profile, None)
        assert await db.get_session_for_task("t1") is None
