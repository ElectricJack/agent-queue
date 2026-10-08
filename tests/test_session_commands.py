"""Session commands, the completion protocol, and the C1 end-to-end path.

``TestEndToEndOnFakeProvider`` is checkpoint C1: a task is launched through
a session provider, closes itself with ``aq task close``, acks the drain,
and the reconciler reaps the session — with no tmux and no WSL anywhere.

See docs/specs/implementation/session-runtime.md §3.8, §8.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.database.tables import (
    integration_check_evidence,
    playbook_artifacts,
    task_branch_origins,
    task_integration_checkpoints,
)
from src.integration.models import (
    ArtifactSnapshot,
    BranchKey,
    Fence,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.integration.ownership import BranchBusy, BranchOwnership
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskCompletion,
    TaskStatus,
    Workspace,
)
from src.orchestrator.git_ops import TRUSTED_EVIDENCE_WAIT_KEY
from src.scheduler import AssignAction
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.harness_registry import HarnessRegistry, load_from_vault
from src.sessions.reconciler import DRAIN_ACK_KEY, SessionReconciler
from src.sessions.spec import SessionSpecBuilder
from src.sessions.provider import SessionHandle
from tests.db_fixtures import lease_dsn


class _Bus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, payload=None):
        self.events.append((event_type, dict(payload or {})))

    def types(self):
        return [t for t, _ in self.events]


def _handoff_git(head="a" * 40):
    from src.git.manager import RemoteRefState

    detached = False

    async def current_branch(_path, *, strict=False):
        return "HEAD" if detached else "aq/t1"

    async def run(args, *, cwd):
        nonlocal detached
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "HEAD" if detached else "aq/t1"
        if args == ["status", "--porcelain"] or args == ["fetch", "origin"]:
            return ""
        if args in (
            ["rev-parse", "refs/heads/aq/t1"],
            ["rev-parse", "refs/remotes/origin/aq/t1"],
            ["rev-parse", "HEAD"],
        ):
            return head
        if args == ["switch", "--detach", head]:
            detached = True
            return ""
        raise AssertionError(f"unexpected handoff git call: {args!r} in {cwd}")

    async def rev_parse(_path, _ref):
        return head

    async def validate_checkout(_path):
        return True

    async def has_remote(_path, *, strict=False):
        return True

    async def remote_ref(_path, _branch):
        return SimpleNamespace(state=RemoteRefState.PRESENT, oid=head)

    async def reserved_paths(_path, _base_ref, _delivery_ref):
        return []

    return SimpleNamespace(
        aget_current_branch=current_branch,
        arev_parse=rev_parse,
        avalidate_checkout=validate_checkout,
        ahas_remote=has_remote,
        als_remote_ref=remote_ref,
        areserved_paths_in_diff=reserved_paths,
        _arun=run,
        _arun_unlocked=run,
    )


class _StubAssignmentRouting:
    """Pass-through route resolver.

    ``ExecutionMixin._check_agent_routing`` asks the coordinator for a fresh
    route before every assignment.  These tests are about the session
    lifecycle, not the routing model, so the route just echoes back what the
    task already carries.  Without it every ``_execute_task`` in this file
    dies on ``'_Orch' object has no attribute 'assignment_routing'`` — the
    mixin's own method shadows ``_StubOrchestrator``'s override via the MRO.
    """

    async def routes_for(self, tasks):
        from src.assignment_routing import EffectiveAssignmentRoute

        return {
            t.id: EffectiveAssignmentRoute(
                task_id=t.id,
                intelligence_class=t.intelligence_class,
                provider=None,
                source="stub",
            )
            for t in tasks
        }

    async def explain(self, task):
        return None, None


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
        # Orchestrator state ``_execute_task``'s no-workspace leg reads and
        # writes: why the acquisition failed, and (for the four waits a
        # freed worktree slot resolves) the parked-task set the cascade
        # cuts short.
        self._workspace_wait_reasons = {}
        self._slot_starved_pauses = {}
        # ``_execute_task`` guards on this: with sessions enabled and no
        # runtimes registry it must still run, because a session-routed
        # task never constructs a runtime.
        self._runtimes = None
        self.llm_logger = None
        self.assignment_routing = _StubAssignmentRouting()
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


class _StaticAssignmentRouting:
    """Minimal route provider for direct execution-path tests.

    The full coordinator is exercised in its own test module. These tests
    need only a fresh, non-LLM route so their real launch path can pass the
    assignment-time recheck introduced before execution starts.
    """

    async def routes_for(self, tasks):
        return {
            task.id: SimpleNamespace(
                intelligence_class=task.intelligence_class or "",
                provider=None,
            )
            for task in tasks
        }


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
    database = Database(lease_dsn("t.db"))
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


@pytest.mark.parametrize("state", ["sleeping", "stopped"])
async def test_prune_inactive_named_session(handler, providers, state, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("src.sessions.proctable.scan_by_env_marker", AsyncMock(return_value=[]))
    row = await _make_session(handler.db, providers.create("fake"),
                              task_id=None, lifecycle="named", state=state, name="n-supervisor--p1")
    await providers.create("fake").stop(SessionHandle(row.name, row.provider, row.instance_token))
    result = await handler.execute("session_prune", {"session_id": row.id})
    assert result["success"], result
    assert await handler.db.get_session(row.id) is None
    assert "session.pruned" in handler.orchestrator.bus.types()


async def test_prune_refuses_live_terminal_and_leftover_marked_process(handler, providers, monkeypatch):
    from unittest.mock import AsyncMock
    from src.sessions.proctable import ProcEntry

    row = await _make_session(handler.db, providers.create("fake"),
                              task_id=None, lifecycle="named", state="sleeping")
    result = await handler.execute("session_prune", {"session_id": row.id})
    assert "live terminal" in result["error"]
    await providers.create("fake").stop(SessionHandle(row.name, row.provider, row.instance_token))
    monkeypatch.setattr("src.sessions.proctable.scan_by_env_marker", AsyncMock(return_value=[
        ProcEntry(42, 1, "codex", 1, row.instance_token),
    ]))
    result = await handler.execute("session_prune", {"session_id": row.id})
    assert "marked processes" in result["error"]
    assert await handler.db.get_session(row.id) is not None


async def test_prune_query_refuses_wake_or_token_change(db, providers):
    row = await _make_session(db, providers.create("fake"),
                              task_id=None, lifecycle="named", state="sleeping")
    assert not await db.prune_named_session(row.id, row.instance_token)
    await db.update_session(row.id, desired_state="stopped")
    assert not await db.prune_named_session(row.id, "different-instance")
    assert await db.prune_named_session(row.id, row.instance_token)


class TestSessionList:
    async def test_empty(self, handler):
        result = await handler.execute("session_list", {})
        assert result["success"] is True and result["count"] == 0

    async def test_lists_and_derives_stalled(self, handler, db, provider, config):
        await _make_task(db)
        row = await _make_session(db, provider)
        # touch_session_activity refuses to rewind; backdate the row directly.
        await db.update_session(row.id, last_activity=time.time() - 10_000)
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

    async def test_paged_list_reports_more_without_returning_extra_row(self, handler, db, provider):
        await _make_task(db)
        row = await _make_session(db, provider)
        await db.update_session(row.id, started_at=100.0)
        await db.create_session(replace(row, id="sess-2", name="s-t1-2", started_at=200.0))
        await db.create_session(replace(row, id="sess-3", name="s-t1-3", started_at=300.0))

        first = await handler.execute("session_list", {"project_id": "p1", "limit": 2})
        assert [s["id"] for s in first["sessions"]] == ["sess-3", "sess-2"]
        assert first["count"] == 2
        assert first["has_more"] is True

        second = await handler.execute("session_list", {"limit": 2, "offset": 2})
        assert [s["id"] for s in second["sessions"]] == ["sess-1"]
        assert second["count"] == 1
        assert second["has_more"] is False

        unpaged = await handler.execute("session_list", {})
        assert unpaged["count"] == 3
        assert unpaged["has_more"] is False

    @pytest.mark.parametrize("args", [
        {"limit": 0}, {"limit": 501}, {"limit": True}, {"offset": -1}, {"offset": "1"}
    ])
    async def test_invalid_page_bounds(self, handler, args):
        result = await handler.execute("session_list", args)
        assert "error" in result


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

    async def test_resolve_by_the_short_id_session_list_prints(self, handler, db, provider):
        """`aq session list` shows 8 characters of the id; those must work too."""
        await _make_task(db)
        await _make_session(db, provider, sid="38f74718-7d4f-4c0c-82c5-f397727b16cd")
        r = await handler.execute("session_peek", {"session_id": "38f74718"})
        assert r["success"] is True, r

    async def test_an_ambiguous_prefix_names_the_candidates(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider, sid="abcd1111-a", task_id="t1", name="s-a")
        await _make_session(db, provider, sid="abcd2222-b", task_id="t1", name="s-b")
        r = await handler.execute("session_show", {"session_id": "abcd"})
        assert "error" in r and "matches 2 sessions" in r["error"]

    async def test_a_too_short_prefix_is_not_guessed(self, handler, db, provider):
        await _make_task(db)
        await _make_session(db, provider, sid="38f74718-7d4f")
        r = await handler.execute("session_show", {"session_id": "38f"})
        assert "error" in r and "No session '38f'" in r["error"]

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

    async def test_kill_records_intent_before_signalling_provider(
        self, handler, db, provider, monkeypatch
    ):
        await _make_task(db)
        await _make_session(db, provider)
        stop = provider.stop

        async def inspect_then_stop(handle, *, grace):
            assert (await db.get_session("sess-1")).desired_state == "stopped"
            await stop(handle, grace=grace)

        monkeypatch.setattr(provider, "stop", inspect_then_stop)

        result = await handler.execute("session_kill", {"session_id": "sess-1"})

        assert result["success"] is True

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

    async def test_a_task_session_ack_does_not_touch_intent(self, handler, db, provider):
        """A task session is stopped by the *provider* ack, not by intent."""
        await _make_task(db)
        await _make_session(db, provider)
        await handler.execute("session_drain_ack", {"session_id": "sess-1"})
        assert (await db.get_session("sess-1")).desired_state == "running"

    async def test_a_pool_session_ack_also_records_the_intent(self, handler, db, provider):
        """``_step_drain_ack`` tears a pool row down on ``desired_state``.

        It never reads the provider meta key for a pool session — that path
        is ``_terminate_pool_session``, gated on intent.  Writing only
        ``state`` left an acked worker parked in ``draining`` forever with
        its agent un-retired and its workspace still locked.
        """
        row = await _make_session(db, provider, lifecycle="pool", task_id=None)
        r = await handler.execute("session_drain_ack", {"session_id": "sess-1"})
        assert r["success"] is True
        fresh = await db.get_session("sess-1")
        assert (fresh.state, fresh.desired_state) == ("draining", "stopped")
        assert await provider.get_meta(handler._session_handle(row), DRAIN_ACK_KEY) == "1"


class TestPerProjectSessionFence:
    """A per-project elevated token may only act on its own project's sessions.

    ``check_command_scope`` pins ``args["project_id"]`` for a per-project
    supervisor, but the session commands address a row by id, unique id
    prefix, name or task and never read that field — so the pin was vacuous
    and ``aq session kill <prefix>`` (the prefix ``aq session list`` prints)
    was enough to stop another project's worker.  The fence is
    ``SessionCommandsMixin._session_project_scope_error``, shared with
    ``session_token``.

    Handlers are called directly with ``_current_scope`` set rather than
    through ``execute``: ``execute``'s capability gate resolves the principal
    from the session row, and a scope naming a session that does not exist
    fails closed to ``DENY_ALL`` before any of this is reached.  What is under
    test is the fence, and it reads nothing but ``self._current_scope``.
    """

    async def _foreign_session(self, db, provider, *, lifecycle="task", task_id="t2"):
        """A live session in project p2, with a task of its own there."""
        await db.create_project(Project(id="p2", name="P2"))
        await db.create_task(Task(id="t2", project_id="p2", title="T2", description="d"))
        await db.transition_task("t2", TaskStatus.IN_PROGRESS)
        row = await _make_session(
            db, provider, sid="sess-p2", task_id=task_id, lifecycle=lifecycle, name="s-t2"
        )
        # ``_make_session`` writes the fixture's project; the row under test is
        # project B's.
        await db.update_session(row.id, project_id="p2")
        return row

    @staticmethod
    def _elevated_scope(supervisor_of: str | None) -> dict:
        """The scope of a supervisor token pinned to *supervisor_of*.

        ``project_id=None`` with ``elevated`` is the global supervisor.
        """
        return {
            "kind": "session",
            "session_id": f"supervisor-{supervisor_of or 'global'}",
            "task_id": None,
            "project_id": supervisor_of,
            "elevated": True,
        }

    @pytest.mark.parametrize("address", [
        {"session_id": "sess-p2"},
        {"session_id": "sess-p"},
        {"name": "s-t2"},
        {"task_id": "t2"},
    ])
    async def test_a_project_supervisor_cannot_kill_another_projects_session(
        self, handler, db, provider, orch, address
    ):
        """The kill must be refused, and nothing about the row may change."""
        await _make_task(db)
        await self._foreign_session(db, provider)

        handler._current_scope = self._elevated_scope("p1")
        try:
            result = await handler._cmd_session_kill(address)
        finally:
            handler._current_scope = None

        assert result["success"] is False
        assert "another project" in result["error"]
        # No intent, no signal, no event: the process is still running and the
        # reconciler still owns classifying it.
        row = await db.get_session("sess-p2")
        assert (row.state, row.desired_state) == ("running", "running")
        assert [h.name for h in await provider.list_running("s-")] == ["s-t2"]
        assert "session.killed" not in orch.bus.types()

    async def test_a_project_supervisor_can_kill_inside_its_own_project(
        self, handler, db, provider
    ):
        await _make_task(db)
        await _make_session(db, provider)

        handler._current_scope = self._elevated_scope("p1")
        try:
            result = await handler._cmd_session_kill({"session_id": "sess-1"})
        finally:
            handler._current_scope = None

        assert result["success"] is True
        assert (await db.get_session("sess-1")).desired_state == "stopped"
        assert await provider.list_running("s-t1") == []

    async def test_the_global_supervisor_may_kill_any_projects_session(
        self, handler, db, provider
    ):
        """``project_id is None`` is the global supervisor: no project pin."""
        await self._foreign_session(db, provider)

        handler._current_scope = self._elevated_scope(None)
        try:
            result = await handler._cmd_session_kill({"session_id": "sess-p2"})
        finally:
            handler._current_scope = None

        assert result["success"] is True
        assert (await db.get_session("sess-p2")).desired_state == "stopped"

    async def test_a_local_caller_may_kill_any_projects_session(self, handler, db, provider):
        """The loopback CLI carries no scope at all, so there is no fence."""
        await self._foreign_session(db, provider)

        result = await handler.execute("session_kill", {"session_id": "sess-p2"})

        assert result["success"] is True
        assert (await db.get_session("sess-p2")).desired_state == "stopped"

    async def test_a_project_supervisor_cannot_peek_at_another_projects_session(
        self, handler, db, provider
    ):
        """A cross-project peek is a cross-project read, not a kill."""
        await _make_task(db)
        await self._foreign_session(db, provider)
        provider.feed_output("s-t2", "project B only")

        handler._current_scope = self._elevated_scope("p1")
        try:
            result = await handler._cmd_session_peek({"session_id": "sess-p2"})
        finally:
            handler._current_scope = None

        assert result["success"] is False
        assert "output" not in result

    async def test_a_project_supervisor_can_peek_inside_its_own_project(
        self, handler, db, provider
    ):
        await _make_task(db)
        await _make_session(db, provider)
        provider.feed_output("s-t1", "line one")

        handler._current_scope = self._elevated_scope("p1")
        try:
            result = await handler._cmd_session_peek({"session_id": "sess-1"})
        finally:
            handler._current_scope = None

        assert result["success"] is True and "line one" in result["output"]

    async def test_a_worker_scope_may_reach_its_own_projects_session(
        self, handler, db, provider
    ):
        """The fence compares projects; it is not a blanket session refusal."""
        await _make_task(db)
        await _make_session(db, provider)
        provider.feed_output("s-t1", "line one")

        handler._current_scope = {
            "kind": "session",
            "session_id": "sess-1",
            "task_id": "t1",
            "project_id": "p1",
            "elevated": False,
        }
        try:
            assert (await handler._cmd_session_peek({"session_id": "sess-1"}))["success"]
        finally:
            handler._current_scope = None

    async def test_a_project_supervisor_cannot_drain_ack_another_projects_pool_session(
        self, handler, db, provider
    ):
        """An ack is a teardown request, so it is fenced like the kill."""
        await _make_task(db)
        row = await self._foreign_session(db, provider, lifecycle="pool", task_id=None)
        handle = handler._session_handle(row)

        handler._current_scope = self._elevated_scope("p1")
        try:
            result = await handler._cmd_session_drain_ack({"session_id": "sess-p2"})
        finally:
            handler._current_scope = None

        assert result["success"] is False
        fresh = await db.get_session("sess-p2")
        assert (fresh.state, fresh.desired_state) == ("running", "running")
        assert await provider.get_meta(handle, DRAIN_ACK_KEY) is None

    async def test_a_worker_can_drain_ack_its_own_projects_session(self, handler, db, provider):
        """The completion protocol's own half of the fence."""
        await _make_task(db)
        await _make_session(db, provider, lifecycle="pool", task_id=None)

        handler._current_scope = {
            "kind": "session",
            "session_id": "sess-1",
            "task_id": None,
            "project_id": "p1",
            "elevated": False,
        }
        try:
            result = await handler._cmd_session_drain_ack({"session_id": "sess-1"})
        finally:
            handler._current_scope = None

        assert result["success"] is True
        assert (await db.get_session("sess-1")).state == "draining"


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
        assert scope.session_instance_token == "tok-1"
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

    async def test_a_project_supervisor_cannot_mint_across_projects(
        self, handler, db, provider, store
    ):
        """`supervisor-A` must not mint a credential for project B.

        ``check_command_scope`` pins ``args["project_id"]`` for a
        per-project elevated caller, but this command addresses a session
        by id and never reads ``project_id`` — so the pin is vacuous here
        and the fence has to live in the command.  A minted token is a
        durable credential, so this is escalation, not a scoping slip.
        """
        await db.create_project(Project(id="p2", name="P2"))
        await _make_task(db)
        row = await _make_session(db, provider)
        await db.update_session(row.id, project_id="p2")

        handler._current_scope = {
            "kind": "session",
            "session_id": "supervisor-p1",
            "task_id": None,
            "project_id": "p1",
            "elevated": True,
        }
        try:
            r = await handler._cmd_session_token({"session_id": "sess-1"})
        finally:
            handler._current_scope = None
        assert r["success"] is False
        assert "another project" in r["error"]
        assert "token" not in r

    async def test_a_project_supervisor_can_mint_inside_its_own_project(
        self, handler, db, provider, store
    ):
        await _make_task(db)
        await _make_session(db, provider)
        handler._current_scope = {
            "kind": "session",
            "session_id": "supervisor-p1",
            "task_id": None,
            "project_id": "p1",
            "elevated": True,
        }
        try:
            r = await handler._cmd_session_token({"session_id": "sess-1"})
        finally:
            handler._current_scope = None
        assert r["success"] is True and r["token"].startswith("aqs_")

    async def test_a_local_caller_is_unrestricted(self, handler, db, provider, store):
        """No project pin (loopback CLI, or the global supervisor) → no fence."""
        await db.create_project(Project(id="p2", name="P2"))
        await _make_task(db)
        row = await _make_session(db, provider)
        await db.update_session(row.id, project_id="p2")
        r = await handler.execute("session_token", {"session_id": "sess-1"})
        assert r["success"] is True and r["project_id"] == "p2"

    def test_is_excluded_from_mcp(self):
        """A credential minter is not an MCP tool, even for a trusted client."""
        from src.mcp_registration import get_effective_exclusions

        assert "session_token" in get_effective_exclusions()


# ---------------------------------------------------------------------------
# task_close / task_heartbeat
# ---------------------------------------------------------------------------


class TestTaskClose:
    @pytest.mark.parametrize("lifecycle", ["task", "pool"])
    async def test_scoped_close_cannot_skip_feedback_by_omitting_session_argument(
        self, handler, db, provider, orch, lifecycle
    ):
        task = await _make_task(db)
        await _make_session(db, provider, lifecycle=lifecycle)
        await db.create_message(
            project_id="p1", from_kind="system", from_id="monitor",
            to_kind="session", to_id="sess-1", body="New deployment evidence.",
        )
        result = await handler.execute("task_close", {
            "task_id": "t1", "outcome": "pass", "summary": "Done.",
            "claim_epoch": task.claim_epoch,
            "_scope": {"kind": "session", "session_id": "sess-1", "project_id": "p1",
                       "task_id": "t1", "elevated": False},
        })
        assert result["code"] == "messages.pending_before_close"
        assert (await db.get_session("sess-1")).task_id == "t1"
        assert not orch.closed_calls

    @pytest.mark.parametrize("recipient_kind,recipient_id", [("task", "t1"), ("session", "sess-1")])
    @pytest.mark.parametrize("outcome", ["pass", "fail"])
    async def test_pending_feedback_preserves_claim_until_read(
        self, handler, db, provider, orch, recipient_kind, recipient_id, outcome
    ):
        await _make_task(db)
        await _make_session(db, provider)
        message = await db.create_message(
            project_id="p1", from_kind="user", from_id="operator",
            to_kind=recipient_kind, to_id=recipient_id,
            body="Deployment completed; recheck the running version before closing.",
        )
        args = {
            "task_id": "t1", "session_id": "sess-1", "outcome": outcome,
            "summary": "Evidence collected before deployment.",
        }
        result = await handler.execute("task_close", args)
        assert result["code"] == "messages.pending_before_close"
        assert f"--to {recipient_kind}:{recipient_id} --inject --limit 50" in result["error"]
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS
        assert (await db.get_session("sess-1")).task_id == "t1"
        assert (await db.get_session("sess-1")).state == "running"
        assert (await db.get_message(message.id)).delivered_at is None
        assert await db.get_task_meta("t1", "outcome") is None
        assert not orch.closed_calls

        # A repeated close cannot silently consume the feedback. The existing
        # inbox command returns the actual body before the retry can proceed.
        assert (await handler.execute("task_close", args))["code"] == result["code"]
        inbox = await handler.execute("message_inbox", {
            "to_kind": recipient_kind, "to_id": recipient_id, "inject": True, "limit": 50,
        })
        assert inbox["messages"][0]["body"] == message.body
        args["summary"] = "Rechecked deployment and incorporated the correction."
        assert (await handler.execute("task_close", args))["success"] is True
        assert len(orch.closed_calls) == 1

    @pytest.mark.parametrize("recipient_kind,recipient_id", [("task", "t1"), ("session", "sess-1")])
    async def test_consumed_body_stays_readable_after_a_crashed_inject(
        self, handler, db, provider, orch, recipient_kind, recipient_id
    ):
        """fair-impact-65: the reviewer's body must not be lost to a dead parser.

        The worker ran the injected read the refusal names, the daemon consumed
        delivery, and the worker's own output died before it printed anything.
        Its pending queue is now empty and — before this fix — nothing anywhere
        still held the body, so it could neither handle the feedback nor tell
        anyone what it had been told.
        """
        task = await _make_task(db)
        await _make_session(db, provider)
        message = await db.create_message(
            project_id="p1", from_kind="system", from_id="review:rev-bold-vault",
            to_kind=recipient_kind, to_id=recipient_id,
            body="Spec review rev-bold-vault: revise section 3 before merging.",
        )
        scope = {"kind": "session", "session_id": "sess-1", "project_id": "p1",
                 "task_id": "t1", "elevated": False}
        args = {
            "task_id": "t1", "outcome": "pass", "summary": "Done.",
            "claim_epoch": task.claim_epoch, "_scope": scope,
        }

        assert (await handler.execute("task_close", dict(args)))["code"] == (
            "messages.pending_before_close"
        )

        # The injected read: delivery consumed, response discarded unread.
        injected = await handler.execute("message_inbox", {
            "to_kind": recipient_kind, "to_id": recipient_id,
            "inject": True, "limit": 50, "_scope": scope,
        })
        assert injected["injected"] == 1
        # A plain re-read is empty — the queue really is drained.
        assert (await handler.execute("message_inbox", {
            "to_kind": recipient_kind, "to_id": recipient_id, "_scope": scope,
        }))["count"] == 0

        # The body is still reachable, read-only, through the consumed re-read.
        recovered = await handler.execute("message_inbox", {
            "to_kind": recipient_kind, "to_id": recipient_id,
            "include_consumed": True, "limit": 50, "_scope": scope,
        })
        assert recovered["count"] == 0
        assert [m["body"] for m in recovered["consumed_messages"]] == [message.body]

        # Recovering the body is not itself a delivery event, and the close
        # that was refused for unread feedback now proceeds on its own merits.
        args["summary"] = "Revised section 3 per review rev-bold-vault."
        assert (await handler.execute("task_close", dict(args)))["success"] is True
        assert len(orch.closed_calls) == 1

    async def test_refusal_names_the_consumed_re_read(self, handler, db, provider):
        """The recovery is discoverable from the refusal itself."""
        task = await _make_task(db)
        await _make_session(db, provider)
        await db.create_message(
            project_id="p1", from_kind="user", from_id="operator",
            to_kind="session", to_id="sess-1", body="A correction.",
        )
        result = await handler.execute("task_close", {
            "task_id": "t1", "outcome": "pass", "summary": "Done.",
            "claim_epoch": task.claim_epoch,
            "_scope": {"kind": "session", "session_id": "sess-1", "project_id": "p1",
                       "task_id": "t1", "elevated": False},
        })
        assert result["code"] == "messages.pending_before_close"
        assert "--include-consumed" in result["error"]

    @pytest.mark.parametrize("bypass", ["operator", "disabled", "other_recipient", "delivered"])
    async def test_feedback_check_does_not_gate_unrelated_or_consumed_messages(
        self, handler, db, provider, config, bypass
    ):
        await _make_task(db)
        await _make_session(db, provider)
        message = await db.create_message(
            project_id="p1", from_kind="system", from_id="monitor",
            to_kind="session", to_id="other-session" if bypass == "other_recipient" else "sess-1",
            body="A correction.",
        )
        if bypass == "disabled":
            config.messages.enabled = False
        if bypass == "delivered":
            await db.mark_delivered(message.id)
        args = {"task_id": "t1", "outcome": "pass", "summary": "Done."}
        if bypass != "operator":
            args["session_id"] = "sess-1"
        assert (await handler.execute("task_close", args))["success"] is True

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
        from src.orchestrator.git_ops import GitOpsMixin
        from src.orchestrator.pools import PoolsMixin
        from src.orchestrator.workspace import WorkspaceMixin

        class _Orch(ExecutionMixin, WorkspaceMixin, GitOpsMixin, _StubOrchestrator):
            #: Pipeline verdict the next close should see: (pr_url, ok).
            pipeline_result = (None, True)
            #: Development tests install a real GitManager; admission only
            #: touches git for a task with development prerequisites.
            git = None

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
            # Launch and assignment re-check development admission, which
            # PoolsMixin supplies on the real Orchestrator.
            _delivery_admission = PoolsMixin._delivery_admission

        orch = _Orch(db, config, providers, harnesses)
        orch.session_reconciler.orchestrator = orch
        return orch

    @pytest.fixture
    def real_handler(self, real_orch, config):
        handler = CommandHandler(real_orch, config)
        real_orch._command_handler = handler
        return handler

    async def _setup_development_git(self, db, real_orch, tmp_path, *, artifact=True):
        from src.git.manager import GitManager

        wd = await self._setup(db, tmp_path)
        git = GitManager()
        remote = tmp_path / "remote.git"
        await git._arun(["init", "--bare", str(remote)], cwd=str(tmp_path))
        await git._arun(["init", "-b", "main"], cwd=wd)
        await git._arun(["config", "user.name", "Tester"], cwd=wd)
        await git._arun(["config", "user.email", "test@example.com"], cwd=wd)
        from pathlib import Path

        (Path(wd) / "base").write_text("base")
        await git.acommit_all(wd, "base")
        await git._arun(["remote", "add", "origin", str(remote)], cwd=wd)
        await git.apush_branch(wd, "main")
        await git._arun(["checkout", "-b", "aq/t1"], cwd=wd)
        base = await git.arev_parse(wd, "HEAD")
        if artifact:
            (Path(wd) / "one").write_text("one")
            await git.acommit_all(wd, "one\n\nAQ-Task: t1")
            (Path(wd) / "two").write_text("two")
            await git.acommit_all(wd, "two\n\nAQ-Task: t1")
        await git.apush_branch(wd, "aq/t1")
        await db.create_repo(RepoConfig(id="repo", project_id="p1", source_type=RepoSourceType.CLONE,
                                        url=str(remote)))
        await db.update_project("p1", hierarchical_integration_mode="development",
                                integration_repository_id="repo")
        await db.update_task("t1", repo_id="repo", branch_name="aq/t1")
        real_orch.git = git
        return wd, git, base

    @pytest.mark.parametrize("mode", ["train", "hierarchy", "development"])
    async def test_train_mode_close_binds_final_source_and_reopen_gets_new_generation(
        self, db, real_orch, real_handler, tmp_path, mode
    ):
        from pathlib import Path
        from sqlalchemy import insert
        from src.database.tables import task_branch_origins
        from src.integration.batches import BatchService, BatchStore
        from src.integration.git_truth import GitTruth
        from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
        from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
        from src.integration.train import TrainTarget
        from src.integration.train_sources import LeasedPublish, _never_trusted
        from tests.test_integration_gitops import LocalGit
        from tests.test_integration_train_sources import fixture_batches

        wd, git, base = await self._setup_development_git(db, real_orch, tmp_path)
        real_orch.config.integration.git_first = "active"
        await db.update_project("p1", hierarchical_integration_mode=mode)
        async with db._engine.begin() as conn:
            await conn.execute(insert(task_branch_origins).values(
                id="t1-origin", task_id="t1", repository_id="repo", branch_name="aq/t1",
                base_sha=base, creation_generation=0, reserved=True, materialized=True,
                created_at=time.time(),
            ))
        args = {"task_id": "t1", "outcome": "pass", "summary": "all changes"}
        final = await git.arev_parse(wd, "HEAD")
        close = await real_handler.execute("task_close", args)
        assert close["success"] and close["status"] == "COMPLETED"
        first = await db.get_task_completion("t1")
        assert first.commits == [final]
        # Written with COMPLETED next to the provenance generation id.
        assert (await db.get_task_meta("t1", "accepted_close"))["completion_id"] == first.id
        assert await db.get_task_meta("t1", "development_completion_id") == first.id
        repo = await db.get_repo("repo")
        store = GitProvenance(git, wd, repository_url=repo.url)
        identity = CompletionIdentity("p1", "repo", "t1", first.id)
        assert (await store.read_completion(identity))["source_oid"] == final
        snapshot = await GitTruth(git).snapshot(
            wd, project_id="p1", repository_id="repo", repository_url=repo.url,
            target_ref="refs/heads/main",
        )
        members, requests, dependencies = await fixture_batches(db).pending(
            TrainTarget("p1", "repo", "refs/heads/main"), snapshot,
        )
        assert [(m.task_id, m.source_sha, m.source_base_sha) for m in members] == [
            ("t1", final, base),
        ]
        assert set(requests) == {"t1"} and dependencies == {"t1": set()}
        repeated = await real_handler.execute("task_close", args)
        assert not repeated["success"]
        assert len(await db.get_task_completions("t1")) == 1
        await db.transition_task("t1", TaskStatus.IN_PROGRESS, assigned_agent_id="a1")
        await db.update_workspace("ws1", locked_by_task_id="t1", locked_by_agent_id="a1")
        (Path(wd) / "three").write_text("three")
        await git.acommit_all(wd, "reopened work\n\nAQ-Task: t1")
        await git.apush_branch(wd, "aq/t1")
        close = await real_handler.execute("task_close", args)
        assert close["success"]
        second = await db.get_task_completion("t1")
        assert second.id != first.id and second.commits != first.commits
        assert not await store.contained(CompletedSource(
            CompletionIdentity("p1", "repo", "t1", second.id), second.commits[0]), final)
        transport = LocalGit(Path(repo.url))

        async def repository(_batch):
            return RetainedRepository("repo", Path(wd), None, "main")

        async def gate(_batch, _sha, _tree):
            return False

        batches = fixture_batches(db)
        service = BatchService(
            BatchStore(db), GitOperations(
                db, git=transport, repository=repository,
                authority=SubjectGitAuthority(db, trusted_green=_never_trusted),
            ),
            publish=LeasedPublish(db, transport), eligible=batches.eligible, gate=gate,
        )
        snapshot = await GitTruth(git).snapshot(
            wd, project_id="p1", repository_id="repo", repository_url=repo.url,
            target_ref="refs/heads/main",
        )
        opened = await batches.open_batch(TrainTarget("p1", "repo", "refs/heads/main"), snapshot, service)
        assert opened.batch is not None and opened.blockers == ()
        assert await service.store.members(opened.batch.id) == opened.members
        assert [(m.task_id, m.source_sha, m.source_base_sha) for m in opened.members] == [
            ("t1", second.commits[0], base),
        ]

    @pytest.mark.parametrize("mode", ["train", "hierarchy", "development"])
    @pytest.mark.parametrize("vault_only", [False, True])
    async def test_empty_close_retains_no_change_proof_in_every_train_mode(
        self, db, real_orch, real_handler, tmp_path, mode, vault_only,
    ):
        from sqlalchemy import insert
        from src.integration.delivery_truth import DeliveryState, load_delivery_requests
        from src.integration.git_truth import GitTruth
        from src.integration.provenance import CompletionIdentity, GitProvenance

        wd, git, base = await self._setup_development_git(db, real_orch, tmp_path, artifact=False)
        await db.update_project("p1", hierarchical_integration_mode=mode)
        async with db._engine.begin() as conn:
            await conn.execute(insert(task_branch_origins).values(
                id="t1-origin", task_id="t1", repository_id="repo", branch_name="aq/t1",
                base_sha=base, creation_generation=0, reserved=True, materialized=True,
                created_at=time.time(),
            ))
        if vault_only:
            await db.create_workspace(Workspace(
                id="vault", project_id="p1", workspace_path=str(tmp_path / "vault"),
                source_type=RepoSourceType.LINK, kind_id="vault",
            ))
            await db.add_task_workspace_requirements("t1", [("vault", None)])
            await git._arun(["push", "origin", "--delete", "aq/t1"], cwd=wd)
        close = await real_handler.execute("task_close", {
            "task_id": "t1", "outcome": "pass", "summary": "no source changes",
            "work_outcome": "shipped" if vault_only else "no-op",
        })
        assert close["success"] and close["status"] == "COMPLETED", close
        completion = await db.get_task_completion("t1")
        assert completion.outcome == "pass" and completion.commits == []
        repo = await db.get_repo("repo")
        # Read from a fresh store after task-ref deletion: close itself must
        # have retained the immutable head; no live branch can supply proof.
        if not vault_only:
            await git._arun(["push", "origin", "--delete", "aq/t1"], cwd=wd)
        retained = str(tmp_path / "retained")
        await git._arun(["clone", repo.url, retained], cwd=str(tmp_path))
        store = GitProvenance(git, retained, repository_url=repo.url)
        record = await store.read_completion(CompletionIdentity("p1", "repo", "t1", completion.id))
        assert record["source_oid"] == base
        request = (await load_delivery_requests(
            db, ["t1"], repository_id="repo", target_ref="refs/heads/main",
        ))["t1"]
        snapshot = await GitTruth(git).snapshot(
            retained, project_id="p1", repository_id="repo", repository_url=repo.url,
            target_ref="refs/heads/main",
        )
        proof = await snapshot.is_delivered(request, source_base=base)
        assert proof.state == DeliveryState.NO_CHANGE and proof.satisfied

    @pytest.mark.parametrize("failure", ["changed_head", "missing_base", "dirty", "publication", "moved_source"])
    async def test_vault_empty_close_refuses_unbound_or_unretained_source(
        self, db, real_orch, real_handler, tmp_path, monkeypatch, failure,
    ):
        from pathlib import Path
        from sqlalchemy import insert
        from src.git.manager import GitError
        from src.integration.provenance import GitProvenance

        wd, git, base = await self._setup_development_git(
            db, real_orch, tmp_path, artifact=failure == "changed_head",
        )
        await db.create_workspace(Workspace(
            id="vault", project_id="p1", workspace_path=str(tmp_path / "vault"),
            source_type=RepoSourceType.LINK, kind_id="vault",
        ))
        await db.add_task_workspace_requirements("t1", [("vault", None)])
        if failure != "missing_base":
            async with db._engine.begin() as conn:
                await conn.execute(insert(task_branch_origins).values(
                    id="t1-origin", task_id="t1", repository_id="repo", branch_name="aq/t1",
                    base_sha=base, creation_generation=0, reserved=True, materialized=True,
                    created_at=time.time(),
                ))
        if failure == "dirty":
            (Path(wd) / "dirty").write_text("uncommitted source")
        elif failure == "publication":
            async def failed(*args, **kwargs):
                raise GitError("provenance publication failed")
            monkeypatch.setattr(GitProvenance, "write_completion", failed)
        elif failure == "moved_source":
            original = GitProvenance.write_completion
            async def moved(store, *args, **kwargs):
                result = await original(store, *args, **kwargs)
                await git._arun(["commit", "--allow-empty", "-m", "racing commit"], cwd=wd)
                return result
            monkeypatch.setattr(GitProvenance, "write_completion", moved)
        close = await real_handler.execute("task_close", {
            "task_id": "t1", "outcome": "pass", "summary": "vault delivery", "work_outcome": "shipped",
        })
        assert close["result"] == "verification_failed", close
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert (await db.get_workspace("ws1")).locked_by_task_id == "t1"
        assert await db.get_task_completion("t1") is None

    @pytest.mark.parametrize("mode", ["train", "hierarchy", "development"])
    @pytest.mark.parametrize("failure", ["old_commit", "unpublished_provenance", "moved_source", "branchless_artifact"])
    async def test_development_provenance_refusal_retains_claim_and_completion_history(
        self, db, real_orch, real_handler, tmp_path, monkeypatch, failure, mode
    ):
        from src.git.manager import GitError
        from src.integration.provenance import GitProvenance

        wd, git, base = await self._setup_development_git(db, real_orch, tmp_path)
        await db.update_project("p1", hierarchical_integration_mode=mode)
        session = await _make_session(db, real_orch.session_providers.create("fake"))
        args = {"task_id": "t1", "session_id": session.id, "outcome": "pass", "summary": "work"}
        if failure == "old_commit":
            args["commit"] = base
        elif failure == "branchless_artifact":
            await db.update_task("t1", branch_name=None)
            args["commit"] = await git.arev_parse(wd, "HEAD")
        elif failure == "unpublished_provenance":
            async def failed(*args, **kwargs):
                raise GitError("provenance publication failed")
            monkeypatch.setattr(GitProvenance, "write_completion", failed)
        else:
            original = GitProvenance.write_completion
            async def moved(store, *args, **kwargs):
                result = await original(store, *args, **kwargs)
                await git._arun(["commit", "--allow-empty", "-m", "racing commit"], cwd=wd)
                await git.apush_branch(wd, "aq/t1")
                return result
            monkeypatch.setattr(GitProvenance, "write_completion", moved)
        close = await real_handler.execute("task_close", args)
        assert close["result"] == "verification_failed"
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert (await db.get_workspace("ws1")).locked_by_task_id == "t1"
        assert await db.get_task_completion("t1") is None
        assert close["escalated"] is False
        assert await db.get_task_meta("t1", "needs_attention") is None

    @pytest.mark.parametrize("failure", [
        "invalid_contract", "missing_completion", "failed_completion",
        "empty_commits", "mismatched_commits",
    ])
    async def test_development_repair_provenance_refusal_flags_operator_and_retains_claim(
        self, db, real_orch, real_handler, tmp_path, failure
    ):
        from src.models import TaskCompletion

        wd, git, base = await self._setup_development_git(db, real_orch, tmp_path)
        session = await _make_session(db, real_orch.session_providers.create("fake"))
        task_before = await db.get_task("t1")
        await db.create_task(Task(
            id="original", project_id="p1", repo_id="repo", title="Original", description="",
            status=TaskStatus.COMPLETED,
        ))
        if failure not in {"invalid_contract", "missing_completion"}:
            await db.save_task_completion(TaskCompletion(
                id="original-close", task_id="original",
                outcome="fail" if failure == "failed_completion" else "pass",
                commits=[] if failure == "empty_commits" else [base], completed_at=time.time(),
            ))
        contract = {"invalid": True} if failure == "invalid_contract" else [
            {"task_id": "original", "source_sha": await git.arev_parse(wd, "HEAD")}
        ]
        await db.set_task_meta("t1", "development_repair_sources", contract)
        close = await real_handler.execute("task_close", {
            "task_id": "t1", "session_id": session.id, "outcome": "pass", "summary": "repair",
        })
        assert close["success"] is False and close["result"] == "verification_failed"
        assert close["escalated"] is True
        assert "you can still fix from this workspace" not in close["error"]
        assert "daemon state this workspace cannot change" in close["error"]
        # An unlabelled source names the diagnostic scoped to this held task.
        remedy = "aq task show t1" if failure in {"empty_commits", "mismatched_commits"} else "aq integration status p1"
        assert f"Operator remedy: {remedy}" in close["error"]
        reason = "delivery_provenance_migration"
        assert await db.get_task_meta("t1", "needs_attention") == reason
        assert ("task.needs_attention", {
            "task_id": "t1", "project_id": "p1", "title": task_before.title, "reason": reason,
        }) in real_orch.bus.events
        task_after = await db.get_task("t1")
        assert task_after.status is TaskStatus.IN_PROGRESS
        assert task_after.claim_epoch == task_before.claim_epoch
        assert task_after.assigned_agent_id == task_before.assigned_agent_id
        assert (await db.get_workspace("ws1")).locked_by_task_id == "t1"
        session_after = await db.get_session(session.id)
        assert session_after.state == session.state
        assert session_after.task_id == session.task_id
        assert await db.get_task_completion("t1") is None

    async def test_development_repair_close_retains_exact_legacy_source_binding(
        self, db, real_orch, real_handler, tmp_path
    ):
        from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
        from src.models import TaskCompletion
        from pathlib import Path

        wd, git, base = await self._setup_development_git(db, real_orch, tmp_path)
        await git._arun(["checkout", "-b", "original", base], cwd=wd)
        (Path(wd) / "original").write_text("original work")
        await git.acommit_all(wd, "original source")
        source = await git.arev_parse(wd, "HEAD")
        await git._arun(["checkout", "aq/t1"], cwd=wd)
        await db.create_task(Task(
            id="original", project_id="p1", repo_id="repo", title="Original", description="",
            status=TaskStatus.COMPLETED,
        ))
        await db.save_task_completion(TaskCompletion(
            id="original-close", task_id="original", outcome="pass", commits=[source],
            completed_at=time.time(),
        ))
        await db.set_task_meta("t1", "development_repair_sources", [
            {"task_id": "original", "source_sha": source},
        ])
        close = await real_handler.execute("task_close", {
            "task_id": "t1", "outcome": "pass", "summary": "exact repair",
        })
        assert close["success"] is True
        store = GitProvenance(git, wd, repository_url=(await db.get_repo("repo")).url)
        original = CompletedSource(CompletionIdentity("p1", "repo", "original", "original-close"), source)
        assert (await store.read_completion(original.identity))["source_oid"] == source
        final = await git.arev_parse(wd, "HEAD")
        assert not await store.ancestor(source, final)
        assert await store.contained(original, final)
        assert await db.get_task_meta("t1", "needs_attention") is None

    async def test_development_code_free_close_keeps_exact_generation_without_false_artifact(
        self, db, real_orch, real_handler, tmp_path
    ):
        from src.integration.provenance import CompletionIdentity, GitProvenance

        wd, git, base = await self._setup_development_git(db, real_orch, tmp_path, artifact=False)
        close = await real_handler.execute("task_close", {
            "task_id": "t1", "outcome": "pass", "work_outcome": "no-op", "summary": "no changes",
        })
        assert close["success"]
        completion = await db.get_task_completion("t1")
        repo = await db.get_repo("repo")
        record = await GitProvenance(git, wd, repository_url=repo.url).read_completion(
            CompletionIdentity("p1", "repo", "t1", completion.id))
        assert record["source_oid"] == base and record["artifact"] is False

    async def _setup_development_repair(self, db, real_orch, tmp_path, source_id, *,
                                        parent=None, failure=None, filing="legacy"):
        """A development repair (t1) of a legacy, commits-less source close.

        The repair merges the parked source onto main; main then advances
        again before the repair closes, as it does in production.  *filing*
        is how the parked attempt that filed it survives: a retired journal
        row kept as ``development.legacy_provenance`` or a current
        ``development.operation`` event.
        """
        import json
        from pathlib import Path

        from sqlalchemy import insert

        from src.database.tables import events
        from src.integration.development import DevelopmentPrimitives

        wd, git, base = await self._setup_development_git(db, real_orch, tmp_path, artifact=False)
        await git._arun(["checkout", "-b", "aq/" + source_id, base], cwd=wd)
        (Path(wd) / "source").write_text("source")
        await git.acommit_all(wd, "source work")
        source = await git.arev_parse(wd, "HEAD")
        await git.apush_branch(wd, "aq/" + source_id)
        if parent:
            await db.create_task(Task(id=parent, project_id="p1", title="parent", description="d",
                                      repo_id="repo", status=TaskStatus.COMPLETED))
        await db.create_task(Task(id=source_id, project_id="p1", title="source", description="d",
                                  repo_id="repo", branch_name="aq/" + source_id,
                                  parent_task_id=parent, status=TaskStatus.COMPLETED))
        closed = time.time()
        reported = {"reported_elsewhere": [base]}.get(failure, [])
        await db.save_task_completion(TaskCompletion(
            id="source-close", task_id=source_id, outcome="pass", commits=reported,
            completed_at=closed,
        ))
        await git._arun(["checkout", "main"], cwd=wd)
        (Path(wd) / "main-work").write_text("main")
        await git.acommit_all(wd, "main work")
        repair_base = await git.arev_parse(wd, "HEAD")
        await git.apush_branch(wd, "main")
        # The publisher parks the source; a source closed again later is a
        # newer generation that parked delivery never named.
        parked_at = closed - 1 if failure == "reclosed_after_park" else closed + 1
        filed = {
            "project_id": "p1", "repository_id": "repo", "target_ref": "refs/heads/main",
            "expected_sha": repair_base, "prepared_sha": None, "created_at": parked_at,
            "manifest": [{"task_id": source_id, "source_sha": source, "parent_task_id": parent}],
        }
        async with db._engine.begin() as conn:
            if filing == "operation":
                await conn.execute(DevelopmentPrimitives._operation_insert(
                    id="parked", state="parked", evidence={"kind": "merge_conflict"},
                    reason="source conflict", updated_at=parked_at, **filed,
                ))
            else:
                await conn.execute(insert(events).values(
                    event_type="development.legacy_provenance", project_id="p1",
                    payload=json.dumps({"id": "legacy-provenance:parked", "legacy_id": "parked",
                                        "kind": "merge_conflict", **filed}),
                    timestamp=parked_at,
                ))
        await db.set_task_meta("t1", "development_repair_sources",
                               [{"task_id": source_id, "source_sha": source, "parent_task_id": parent}])
        await db.set_task_meta("t1", "development_repair_evidence",
                               {"delivery_id": "parked", "kind": "merge_conflict"})
        await git._arun(["checkout", "aq/t1"], cwd=wd)
        await git._arun(["merge", "--ff-only", repair_base], cwd=wd)
        if failure == "source_not_in_repair":
            # Content copied without the source commit is not the ancestry check.
            (Path(wd) / "source").write_text("source")
            await git.acommit_all(wd, "copied source content")
        else:
            await git._arun(["merge", "--no-ff", "--no-edit", source], cwd=wd)
        repair = await git.arev_parse(wd, "HEAD")
        await git.apush_branch(wd, "aq/t1")
        await git._arun(["checkout", "main"], cwd=wd)
        (Path(wd) / "later").write_text("later")
        await git.acommit_all(wd, "main advances before the repair closes")
        await git.apush_branch(wd, "main")
        await git._arun(["checkout", "aq/t1"], cwd=wd)
        return wd, git, source, repair_base, repair

    @pytest.mark.parametrize("source_id,parent,filing", [
        ("nimble-bridge.3", "nimble-bridge.2", "legacy"),
        ("development-repair-24d1d5e707519a49c3fc", "fresh-ember.2", "operation"),
        ("prime-glacier.6", "prime-glacier.1", "legacy"),
    ])
    async def test_development_repair_of_unlabelled_source_closes_by_ancestry(
        self, db, real_orch, real_handler, tmp_path, source_id, parent, filing
    ):
        """keen-quest: the three repairs refused as 'unlabelled repair source'."""
        from src.integration.provenance import (
            PREFIX,
            CompletedSource,
            CompletionIdentity,
            GitProvenance,
        )


        wd, git, source, repair_base, repair = await self._setup_development_repair(
            db, real_orch, tmp_path, source_id, parent=parent, filing=filing
        )
        close = await real_handler.execute(
            "task_close", {"task_id": "t1", "outcome": "pass", "summary": "resolved"}
        )
        assert close["success"] and close["status"] == "COMPLETED", close
        assert (await db.get_task_completion("t1")).commits == [repair]
        repo = await db.get_repo("repo")
        store = GitProvenance(git, wd, repository_url=repo.url)
        original = CompletedSource(CompletionIdentity("p1", "repo", source_id, "source-close"), source)
        assert (await store.read_completion(original.identity))["source_oid"] == source
        refs = (await store.run("for-each-ref", "--format=%(refname)",
                                "refs/remotes/origin/" + PREFIX + "replacements/")).split()
        assert len(refs) == 1
        record = await store._read(refs[0])
        # The replacement names the main commit the repair built on, not the
        # tip main reached afterwards.
        assert record["base_oid"] == repair_base and record["source_oid"] == repair
        assert record["replaces"] == [{"identity": {"project_id": "p1", "repository_id": "repo",
                                                    "task_id": source_id, "generation": "source-close"},
                                       "source_oid": source}]
        assert await store.contained(original, repair)

    @pytest.mark.parametrize("failure", ["reclosed_after_park", "source_not_in_repair",
                                         "reported_elsewhere"])
    async def test_development_repair_unprovable_legacy_source_still_needs_migration(
        self, db, real_orch, real_handler, tmp_path, failure
    ):
        await self._setup_development_repair(db, real_orch, tmp_path, "origin-task", failure=failure)
        close = await real_handler.execute(
            "task_close", {"task_id": "t1", "outcome": "pass", "summary": "resolved"}
        )
        assert close["result"] == "verification_failed"
        assert "requires exact provenance migration" in str(close)
        assert "aq task show t1" in str(close)
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert await db.get_task_completion("t1") is None

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
                 profile_id="claude-opus", route_source="legacy")
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

    async def _enable_hierarchy_launch(
        self, db, tmp_path, *, materialized=True, checkpoint=True
    ):
        await db.create_repo(
            RepoConfig(
                id="repo",
                project_id="p1",
                source_type=RepoSourceType.LINK,
                source_path=str(tmp_path / "wd"),
            )
        )
        await db.update_project(
            "p1",
            hierarchical_integration_mode="hierarchy",
            integration_repository_id="repo",
        )
        await db.update_task("t1", repo_id="repo", branch_name="aq/t1")
        async with db.immediate() as conn:
            await conn.execute(
                task_branch_origins.insert().values(
                    id="origin-t1",
                    task_id="t1",
                    repository_id="repo",
                    branch_name="aq/t1",
                    parent_ref="main",
                    base_sha="a" * 40,
                    creation_generation=0,
                    reserved=True,
                    materialized=materialized,
                    created_at=time.time(),
                    materialized_at=time.time() if materialized else None,
                )
            )
            if checkpoint:
                await conn.execute(
                    task_integration_checkpoints.insert().values(
                        task_id="t1",
                        repository_id="repo",
                        branch="aq/t1",
                        checkpoint_sha="a" * 40,
                        generation=0,
                        state="working",
                        version=0,
                        updated_at=time.time(),
                    )
                )
        ownership = BranchOwnership(db)
        fence = await ownership.acquire(
            BranchKey(repository_id="repo", branch="aq/t1"), "t1", "worker"
        )
        return ownership, fence

    async def _install_hierarchy_policy(self, db):
        artifact = ArtifactSnapshot(
            playbook_id="hierarchical-delivery",
            artifact_sha256="sha256:" + "a" * 64,
            schema_generation=2,
            contract_fingerprint="sha256:" + "b" * 64,
            source_digest="sha256:" + "c" * 64,
            compiler_build="test-build",
            compiled_at="2026-09-05T00:00:00Z",
            version=4,
        )
        route = PlaybookRoute(
            playbook_id=artifact.playbook_id,
            scope="project",
            scope_identifier="p1",
            activation_id="activation-audit-only",
            artifact=artifact,
        )
        boundary = IntegrationBoundaryPolicy(
            required_checks=RequiredCheckSet(
                version="parent-v1", names=("unit",), producer_id="forge-observer"
            ),
            repair=RepairPolicy(debug_intelligence_class="deep-high"),
            route=route,
            primary_intelligence_class="medium",
            primary_profile_id="claude-opus",
            verifier_intelligence_class="high",
            verifier_profile_id="claude-opus",
        )
        policy = HierarchicalIntegrationPolicy(
            parent=boundary,
            root=boundary,
            branchless_parent="verifier",
            on_failed_child="block",
        )
        async with db.immediate() as conn:
            await conn.execute(
                playbook_artifacts.insert().values(
                    **artifact.model_dump(),
                    scope="project",
                    scope_identifier="p1",
                    profile_fingerprint="",
                    path="/tmp/artifact",
                    size_bytes=1,
                    validation="{}",
                    created_at=1.0,
                )
            )
        await db.update_project(
            "p1", hierarchical_integration_policy=policy.model_dump(mode="json")
        )

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

    async def test_disabled_sessions_fail_instead_of_using_a_runtime(
        self, db, real_orch, config, tmp_path
    ):
        """The failure has to name the flag, not the subsystem that is gone.

        "legacy runtime dispatch was removed" told an operator what used to
        exist; it did not tell them which of the two routing conditions they
        had actually failed.
        """
        await self._setup(db, tmp_path, ready=True)
        config.sessions.enabled = False

        with pytest.raises(RuntimeError, match="sessions.enabled is false"):
            await real_orch._execute_task(AssignAction("a1", "t1", "p1"))

    async def test_the_routing_failure_names_the_condition_that_failed(
        self, real_orch, config
    ):
        """Each way of failing ``_is_session_routed`` gets its own sentence."""
        config.sessions.enabled = False
        assert "sessions.enabled is false" in real_orch._why_not_session_routed(
            AgentProfile(id="x", name="x", harness="claude")
        )

        config.sessions.enabled = True
        assert "no session harness" in real_orch._why_not_session_routed(
            AgentProfile(id="x", name="x")
        )
        assert "no agent profile" in real_orch._why_not_session_routed(None)
        assert "never push-launched" in real_orch._why_not_session_routed(
            AgentProfile(id="p", name="p", harness="claude", lifecycle="pool")
        )

    async def test_full_lifecycle(
        self, db, real_orch, real_handler, provider, tmp_path, config, monkeypatch
    ):
        from src.api.auth import SessionTokenStore

        real_orch.token_store = SessionTokenStore(db)
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
        scope = await real_orch.token_store.validate(spec.env["AQ_API_TOKEN"])
        assert scope is not None
        assert scope.session_instance_token == session.instance_token
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

    async def test_fixable_verification_refuses_the_close_and_keeps_the_session(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """The close/verification loop must not kill the worker it needs.

        Reopening the task to READY put a live session next to a task that
        was no longer IN_PROGRESS, which is exactly what the reconciler's
        orphan rule drains — so the agent asked to push and open the PR was
        killed five seconds later.  A close from a live session is now
        *refused* instead: the task keeps its status, agent, workspace and
        claim, the agent gets the issue list back, and a second close after
        fixing them succeeds.
        """
        import asyncio

        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")

        async def _stop_with_feedback(ctx):
            assert ctx.close_session_live is True, "the live session must be visible here"
            ctx.verification_retry_in_session = True
            ctx.verification_issues = ["No open PR found for branch aq/t1."]
            ctx.verification_feedback = "push and open a PR, then close again"
            return None, False

        monkeypatch.setattr(real_orch, "_run_completion_pipeline", _stop_with_feedback)
        args = {
            "task_id": "t1",
            "session_id": session.id,
            "outcome": "pass",
            "work_outcome": "shipped",
            "summary": "Work done.",
        }
        close = await asyncio.wait_for(real_handler.execute("task_close", args), timeout=2)

        assert close["success"] is False
        assert close["result"] == "verification_failed"
        assert close["issues"] == ["No open PR found for branch aq/t1."]
        assert "No open PR" in close["error"]

        # Nothing was torn down: the reconciler must have no reason to act.
        task = await db.get_task("t1")
        assert task.status is TaskStatus.IN_PROGRESS
        assert task.assigned_agent_id == "a1"
        assert (await db.get_agent("a1")).state is AgentState.BUSY
        ws = await db.get_workspace("ws1")
        assert ws.locked_by_task_id == "t1" and ws.locked_by_agent_id == "a1"

        # The orphan sweep drained the worker in the original bug.  With the
        # task still IN_PROGRESS it leaves the session alone.
        await real_orch.session_reconciler.tick(now=time.time())
        assert (await db.get_session(session.id)).state == "running"

        # Second close, after the agent fixed the git state, completes.
        async def _ok(ctx):
            return "https://github.com/org/repo/pull/7", True

        monkeypatch.setattr(real_orch, "_run_completion_pipeline", _ok)
        again = await asyncio.wait_for(real_handler.execute("task_close", args), timeout=2)
        assert again["success"] is True and again["status"] == "COMPLETED"

    async def test_escalated_verification_refusal_does_not_blame_the_workspace(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """A precondition the workspace cannot satisfy reads as an escalation.

        Telling a worker to "fix this from this workspace" when the missing
        thing is daemon-side delivery state sends it into a retry loop or
        into closing ``--outcome fail`` over passing work (task fleet-willow).
        """
        import asyncio

        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")

        async def _stop_escalated(ctx):
            ctx.verification_retry_in_session = True
            ctx.verification_escalated = True
            ctx.verification_issues = ["dirty: task t1 holds no integration workspace"]
            ctx.verification_feedback = ctx.verification_issues[0]
            return None, False

        monkeypatch.setattr(real_orch, "_run_completion_pipeline", _stop_escalated)
        close = await asyncio.wait_for(
            real_handler.execute(
                "task_close",
                {
                    "task_id": "t1",
                    "session_id": session.id,
                    "outcome": "pass",
                    "work_outcome": "shipped",
                    "summary": "Work done.",
                },
            ),
            timeout=2,
        )

        assert close["success"] is False and close["escalated"] is True
        assert "you can still fix from this workspace" not in close["error"]
        assert "daemon state this workspace cannot change" in close["error"]
        assert "user:dashboard" in close["error"]
        # Still a refusal, not a close: the task keeps its claim.
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS

    async def test_verification_reopen_returns_ready_and_releases_resources(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """A verification retry must return READY and release its old worker."""
        import asyncio

        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")

        async def reopen(ctx):
            await db.transition_task(
                ctx.task.id,
                TaskStatus.READY,
                context="verification_reopen",
                assigned_agent_id=None,
            )
            ctx.verification_reopened = True
            return None, False

        monkeypatch.setattr(real_orch, "_run_completion_pipeline", reopen)
        close = await asyncio.wait_for(
            real_handler.execute(
                "task_close",
                {
                    "task_id": "t1",
                    "session_id": session.id,
                    "outcome": "pass",
                    "work_outcome": "shipped",
                    "summary": "Work completed; git verification requested a retry.",
                },
            ),
            timeout=2,
        )

        assert close["success"] is True and close["status"] == "READY"
        task = await db.get_task("t1")
        assert task.status is TaskStatus.READY
        assert task.assigned_agent_id is None
        agent = await db.get_agent("a1")
        assert agent.state is AgentState.IDLE and agent.current_task_id is None
        workspace = await db.get_workspace("ws1")
        assert workspace.locked_by_task_id is None
        assert workspace.locked_by_agent_id is None

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

    @pytest.mark.parametrize(
        "close_args,pipeline_ok,status",
        [
            ({"outcome": "pass"}, True, TaskStatus.COMPLETED),
            ({"outcome": "pass"}, False, TaskStatus.BLOCKED),
            ({"outcome": "fail", "failure_class": "transient"}, True, TaskStatus.READY),
            ({"outcome": "fail", "failure_class": "hard"}, True, TaskStatus.BLOCKED),
        ],
        ids=["completed", "pipeline-stop", "retry", "hard-failure"],
    )
    async def test_restart_before_the_record_recovers_the_accepted_close_once(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch,
        close_args, pipeline_ok, status,
    ):
        """smart-cascade: every accepted leg of the real close records the
        close's identity in its transition, so a restart before
        ``save_task_completion`` recovers exactly the submitted record, once."""
        from src.database.queries.result_queries import PENDING_COMPLETION_KEY
        from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY

        class _Restart(BaseException):
            pass

        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")
        real_orch.pipeline_result = (None, pipeline_ok)
        seen: list = []
        pipeline = real_orch._run_completion_pipeline

        async def run_pipeline(ctx):
            seen.append(ctx.accepted_close)
            return await pipeline(ctx)

        monkeypatch.setattr(real_orch, "_run_completion_pipeline", run_pipeline)
        save = db.save_task_completion

        async def die(*_args, **_kwargs):
            raise _Restart()

        monkeypatch.setattr(db, "save_task_completion", die)
        with pytest.raises(_Restart):
            await real_handler.execute(
                "task_close",
                {
                    "task_id": "t1",
                    "session_id": session.id,
                    "summary": "exact account",
                    "tests": ["aq test tests/test_x.py"],
                    "commands": ["ruff check src/x.py"],
                    **close_args,
                },
            )
        monkeypatch.setattr(db, "save_task_completion", save)

        task = await db.get_task("t1")
        assert task.status is status
        assert await db.get_task_completion("t1") is None
        draft = await db.get_task_meta("t1", PENDING_COMPLETION_KEY)
        accepted = await db.get_task_meta("t1", ACCEPTED_CLOSE_KEY)
        assert accepted == draft["identity"] == {
            "completion_id": draft["completion"]["id"],
            "session_id": session.id,
            "claim_epoch": task.claim_epoch,
        }
        if close_args["outcome"] == "pass":
            assert seen == [accepted]

        assert await db.recover_pending_completions() == ["t1"]
        record = await db.get_task_completion("t1")
        assert record.id == accepted["completion_id"]
        assert (record.outcome, record.summary, record.tests, record.commands) == (
            close_args["outcome"],
            "exact account",
            ["aq test tests/test_x.py"],
            ["ruff check src/x.py"],
        )
        assert await db.recover_pending_completions() == []
        assert len(await db.get_task_completions("t1")) == 1

    async def test_restart_before_the_transition_is_never_recovered_after_requeue(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """The supervisor's reproduction: a pass submitted, the daemon died
        before the transition, the restart requeued the task READY.  READY
        is not acceptance -- no record is invented."""
        from src.database.queries.result_queries import PENDING_COMPLETION_KEY
        from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY

        class _Restart(BaseException):
            pass

        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")

        async def die(_ctx):
            raise _Restart()

        monkeypatch.setattr(real_orch, "_run_completion_pipeline", die)
        with pytest.raises(_Restart):
            await real_handler.execute(
                "task_close",
                {"task_id": "t1", "session_id": session.id, "outcome": "pass",
                 "summary": "submitted, never accepted"},
            )
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert await db.get_task_meta("t1", PENDING_COMPLETION_KEY) is not None
        assert await db.get_task_meta("t1", ACCEPTED_CLOSE_KEY) is None
        await db.transition_task(
            "t1", TaskStatus.READY, context="restart_requeue", assigned_agent_id=None
        )

        assert await db.recover_pending_completions() == []
        assert await db.get_task_completions("t1") == []
        assert await db.get_task_meta("t1", PENDING_COMPLETION_KEY) is None

    async def test_old_task_cleanup_preserves_reused_worker_and_adapter(
        self, db, real_orch, tmp_path
    ):
        await self._setup(db, tmp_path)
        await db.create_task(Task(id="t2", project_id="p1", title="Next", description="next"))
        await db.update_agent("a1", state=AgentState.BUSY, current_task_id="t2")
        next_adapter = object()
        real_orch._adapters["a1"] = next_adapter
        await real_orch.release_session_task_resources("t1", agent_id="a1")
        agent = await db.get_agent("a1")
        assert agent.state == AgentState.BUSY and agent.current_task_id == "t2"
        assert real_orch._adapters["a1"] is next_adapter

    async def test_workspace_backoff_releases_worker_assignment(
        self, db, real_orch, tmp_path, monkeypatch
    ):
        from unittest.mock import AsyncMock
        await self._setup(db, tmp_path, ready=True)
        monkeypatch.setattr(real_orch, "_prepare_workspace", AsyncMock(return_value=None))
        await real_orch._execute_task(AssignAction(task_id="t1", agent_id="a1", project_id="p1"))
        task = await db.get_task("t1")
        agent = await db.get_agent("a1")
        assert task.status == TaskStatus.PAUSED
        assert task.assigned_agent_id is None
        assert agent.state == AgentState.IDLE and agent.current_task_id is None

    async def test_retry_can_reassign_only_after_old_session_is_stopped(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        old = await db.get_session_for_task("t1")
        await real_handler.execute("task_close", {
            "task_id": "t1", "session_id": old.id, "outcome": "fail",
            "failure_class": "transient", "summary": "try again",
        })
        task = await db.get_task("t1")
        assert task.status == TaskStatus.READY and task.assigned_agent_id is None
        assert await db.assign_task_to_agent("t1", "a1") is False
        await db.update_session(old.id, state="stopped")
        assert await db.assign_task_to_agent("t1", "a1") is True
        assert (await db.get_agent("a1")).current_task_id == "t1"

    @pytest.mark.parametrize("harness,model,class_id", [
        ("claude", "claude-fable-5", "deep-high"),
        ("codex", "gpt-5.6-luna", "fast-low"),
    ])
    async def test_dispatch_rechecks_routing_before_assignment(
        self, db, real_orch, provider, tmp_path, monkeypatch, harness, model, class_id
    ):
        wd = await self._setup(db, tmp_path, ready=True)
        await db.create_profile(AgentProfile(
            id="worker-deep-codex", name="Codex Sol", harness="codex",
            model="gpt-5.6-sol", default_class="deep-high",
        ))
        await db.update_task("t1", profile_id="worker-deep-codex",
        route_source="legacy", intelligence_class="deep-high")
        # The action was decided before this worker's settings changed.
        await db.update_agent("a1", harness=harness, model=model, intelligence_class=class_id)

        async def prepare(task, agent):
            return wd

        monkeypatch.setattr(real_orch, "_prepare_workspace", prepare)
        await real_orch._execute_task(AssignAction("a1", "t1", "p1"))
        task = await db.get_task("t1")
        assert task.status == TaskStatus.READY
        assert task.assigned_agent_id is None
        assert (await db.get_agent("a1")).state == AgentState.IDLE
        assert await db.get_session_for_task("t1") is None
        assert provider.starts == []

    async def test_launch_rechecks_worker_after_workspace_preparation(
        self, db, real_orch, provider, tmp_path
    ):
        wd = await self._setup(db, tmp_path)
        await db.create_profile(AgentProfile(
            id="worker-deep-codex", name="Codex Sol", harness="codex",
            model="gpt-5.6-sol", default_class="deep-high",
        ))
        await db.update_task("t1", profile_id="worker-deep-codex",
        route_source="legacy", intelligence_class="deep-high")
        profile = await db.get_profile("worker-deep-codex")
        task = await db.get_task("t1")
        await db.update_agent("a1", harness="codex", model="gpt-5.6-luna", intelligence_class="fast-low")
        await real_orch._launch_session_for_task(AssignAction("a1", "t1", "p1"), task, profile, wd)
        assert await db.get_session_for_task("t1") is None
        assert provider.starts == []
        task = await db.get_task("t1")
        assert task.status == TaskStatus.PAUSED
        assert task.assigned_agent_id is None
        assert task.intelligence_class == "deep-high"
        assert (await db.get_agent("a1")).state == AgentState.IDLE
        assert (await db.get_workspace("ws1")).locked_by_task_id is None

    async def test_launch_keeps_inherited_worker_harness_for_generic_profile(
        self, db, real_orch, provider, tmp_path
    ):
        from src.intelligence_classes import IntelligenceClass

        wd = await self._setup(db, tmp_path)
        await db.create_profile(AgentProfile(
            id="worker-deep", name="Generic deep", harness="claude", default_class="deep-high",
        ))
        await db.create_profile(AgentProfile(
            id="codex-worker", name="Codex worker", harness="codex", default_class="deep-high",
        ))
        await db.update_task("t1", profile_id="worker-deep",
        route_source="legacy", intelligence_class="deep-high")
        await db.update_agent("a1", profile_id="codex-worker")
        real_orch.session_spec_builder._intelligence_classes = {
            "deep-high": IntelligenceClass("deep-high", "Deep", "", {
                "anthropic": {"model": "claude-fable-5"},
                "codex": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
            }),
        }
        task = await db.get_task("t1")
        profile = await db.get_profile("worker-deep")
        await real_orch._launch_session_for_task(AssignAction("a1", "t1", "p1"), task, profile, wd)
        session = await db.get_session_for_task("t1")
        assert session.harness == "codex"
        assert session.model == "gpt-5.6-sol"
        assert session.intelligence_class == "deep-high"
        assert len(provider.starts) == 1
        assert (await db.get_agent("a1")).harness is None
        assert (await db.get_profile("worker-deep")).harness == "claude"

    async def test_task_launch_links_worker_and_freezes_individual_settings(
        self, db, real_orch, provider, tmp_path
    ):
        wd = await self._setup(db, tmp_path)
        await db.update_agent("a1", model="chosen-worker-model", intelligence_class="deep")
        task = await db.get_task("t1")
        profile = await db.get_profile("claude-opus")
        action = AssignAction(task_id="t1", agent_id="a1", project_id="p1")
        await real_orch._launch_session_for_task(action, task, profile, wd)
        session = await db.get_session_for_task("t1")
        assert session.agent_id == "a1" and session.project_id == "p1"
        assert session.model == "chosen-worker-model"
        assert session.intelligence_class == "deep"
        assert session.last_claim_epoch == (await db.get_task("t1")).claim_epoch
        assert session.last_claim_epoch is not None
        assert session.llm_provider == "anthropic"
        await db.update_agent("a1", model="next-session-model")
        assert (await db.get_session(session.id)).model == "chosen-worker-model"
        shared = await db.get_profile("claude-opus")
        assert shared.model == profile.model
        assert shared.default_class == profile.default_class

    async def test_resumed_launch_keeps_original_conversation_identity(
        self, db, real_orch, provider, tmp_path, monkeypatch
    ):
        from unittest.mock import AsyncMock
        wd = await self._setup(db, tmp_path)
        await db.set_task_meta("t1", "session_resume_key", "original-conversation")
        monkeypatch.setattr(real_orch, "_validated_resume_key",
                            AsyncMock(return_value="original-conversation"))
        task = await db.get_task("t1")
        await real_orch._launch_session_for_task(
            AssignAction(task_id="t1", agent_id="a1", project_id="p1"),
            task, await db.get_profile("claude-opus"), wd,
        )
        session = await db.get_session_for_task("t1")
        assert session.session_key == "original-conversation"
        assert (await db.list_task_session_attempts("t1"))[0]["session_key"] == "original-conversation"

    async def test_launch_failure_pauses_rather_than_fabricating_a_result(
        self, db, real_orch, provider, tmp_path
    ):
        wd = await self._setup(db, tmp_path)
        task = await db.get_task("t1")
        profile = await db.get_profile("claude-opus")
        action = AssignAction(task_id="t1", agent_id="a1", project_id="p1")

        provider.script_startup_death("s-t1")
        await real_orch._launch_session_for_task(action, task, profile, wd)

        session = await db.get_session_for_task("t1")
        assert session is not None and session.state == "stopped"
        attempts = await db.list_task_session_attempts("t1")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "startup_exit"
        assert attempts[0]["ended_at"] >= attempts[0]["started_at"]
        assert attempts[0]["agent_id"] == "a1"
        assert attempts[0]["outcome"] is None
        task = await db.get_task("t1")
        assert task.status is TaskStatus.PAUSED
        assert (await db.get_agent("a1")).state is AgentState.IDLE

    async def test_hierarchy_launch_persists_starting_attachment_before_provider(
        self, db, real_orch, provider, tmp_path, monkeypatch
    ):
        import asyncio

        wd = await self._setup(db, tmp_path)
        ownership, _fence = await self._enable_hierarchy_launch(db, tmp_path)
        entered, proceed = asyncio.Event(), asyncio.Event()
        original_start = provider.start

        async def paused_start(spec):
            entered.set()
            await proceed.wait()
            return await original_start(spec)

        monkeypatch.setattr(provider, "start", paused_start)
        task = await db.get_task("t1")
        launch = asyncio.create_task(
            real_orch._launch_session_for_task(
                AssignAction("a1", "t1", "p1"),
                task,
                await db.get_profile("claude-opus"),
                wd,
            )
        )
        entered_wait = asyncio.create_task(entered.wait())
        done, _ = await asyncio.wait(
            {launch, entered_wait}, timeout=30, return_when=asyncio.FIRST_COMPLETED
        )
        entered_wait.cancel()
        await asyncio.gather(entered_wait, return_exceptions=True)
        if launch in done:
            await launch  # Surface a launch failure instead of waiting forever.
            pytest.fail("session launch returned before starting the provider")
        if not entered.is_set():
            launch.cancel()
            await asyncio.gather(launch, return_exceptions=True)
            pytest.fail("session launch did not reach the provider within 30 seconds")
        row = await db.get_session_for_task("t1")
        proceed.set()
        await launch
        owner = await ownership.get_owner(
            BranchKey(repository_id="repo", branch="aq/t1")
        )

        assert row.state == "starting"
        assert owner["handoff_state"] == "attached"
        assert owner["session_id"]
        assert owner["workspace_id"] == "ws1"
        assert (await db.get_session_for_task("t1")).state == "running"

    async def test_hierarchy_pending_origin_never_reaches_assignment_or_started(
        self, db, real_orch, provider, tmp_path, monkeypatch
    ):
        await self._setup(db, tmp_path, ready=True)
        await self._enable_hierarchy_launch(db, tmp_path, materialized=False)

        async def must_not_prepare(*_args, **_kwargs):
            raise AssertionError("pending origin reached workspace preparation")

        monkeypatch.setattr(real_orch, "_prepare_workspace", must_not_prepare)
        await real_orch._execute_task(AssignAction("a1", "t1", "p1"))

        task = await db.get_task("t1")
        assert task.status is TaskStatus.READY
        assert task.assigned_agent_id is None
        assert (await db.get_agent("a1")).state is AgentState.IDLE
        assert provider.starts == []
        assert "task.started" not in real_orch.bus.types()

    async def test_hierarchy_transfer_winning_before_launch_prevents_provider_start(
        self, db, real_orch, provider, tmp_path
    ):
        wd = await self._setup(db, tmp_path)
        await db.update_agent("a1", current_task_id="t1")
        ownership, fence = await self._enable_hierarchy_launch(db, tmp_path)
        await ownership.transfer(fence, "collector", "collector")
        task = await db.get_task("t1")

        await real_orch._launch_session_for_task(
            AssignAction("a1", "t1", "p1"),
            task,
            await db.get_profile("claude-opus"),
            wd,
        )

        assert provider.starts == []
        assert await db.get_session_for_task("t1") is None
        assert (await db.get_workspace("ws1")).locked_by_task_id is None
        old_agent = await db.get_agent("a1")
        assert old_agent.state is AgentState.IDLE
        assert old_agent.current_task_id is None
        current = await ownership.get_owner(
            BranchKey(repository_id="repo", branch="aq/t1")
        )
        assert current["owner_id"] == "collector"
        assert current["owner_role"] == "collector"
        assert current["fence_token"] == fence.token + 1
        assert current["handoff_state"] == "reserved"
        assert current["session_id"] is None
        assert current["workspace_id"] is None

    async def test_hierarchy_launch_workspace_lookup_error_has_initialized_row(
        self, db, real_orch, provider, tmp_path, monkeypatch
    ):
        wd = await self._setup(db, tmp_path)
        await self._enable_hierarchy_launch(db, tmp_path)
        original = db.get_workspace_for_task
        calls = 0

        async def fail_once(task_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("workspace lookup unavailable")
            return await original(task_id)

        monkeypatch.setattr(db, "get_workspace_for_task", fail_once)
        task = await db.get_task("t1")

        await real_orch._launch_session_for_task(
            AssignAction("a1", "t1", "p1"),
            task,
            await db.get_profile("claude-opus"),
            wd,
        )

        assert provider.starts == []
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED

    async def test_managed_leaf_close_advances_finished_head_without_legacy_integrate(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        from unittest.mock import AsyncMock

        from src.git.manager import RemoteRefState

        wd = await self._setup(db, tmp_path)
        await self._enable_hierarchy_launch(db, tmp_path, checkpoint=False)
        async with db.immediate() as conn:
            await conn.execute(
                task_integration_checkpoints.insert().values(
                    task_id="t1",
                    repository_id="repo",
                    branch="aq/t1",
                    checkpoint_sha="a" * 40,
                    generation=0,
                    state="working",
                    version=0,
                    updated_at=time.time(),
                )
            )
        task = await db.get_task("t1")
        await real_orch._launch_session_for_task(
            AssignAction("a1", "t1", "p1"),
            task,
            await db.get_profile("claude-opus"),
            wd,
        )
        session = await db.get_session_for_task("t1")
        head = "b" * 40
        monkeypatch.setattr(real_orch, "_phase_integrate", AsyncMock(), raising=False)
        # This test supplies synthetic Git OIDs to isolate checkpoint/ownership
        # advancement. Real completion publication is covered for every mode
        # by test_train_mode_close_binds_final_source_and_reopen_gets_new_generation.
        record_completion = AsyncMock(return_value=head)
        monkeypatch.setattr("src.integration.provenance.record_worker_completion", record_completion)

        async def git_run(args, *, cwd):
            assert cwd == wd
            if args == ["status", "--porcelain"]:
                return ""
            if args == ["rev-parse", "HEAD"]:
                return head
            raise AssertionError(f"unexpected producer Git command: {args!r}")

        real_orch.git = SimpleNamespace(
            avalidate_checkout=AsyncMock(return_value=True),
            ahas_remote=AsyncMock(return_value=True),
            aget_current_branch=AsyncMock(return_value="aq/t1"),
            als_remote_ref=AsyncMock(
                return_value=SimpleNamespace(state=RemoteRefState.PRESENT, oid=head)
            ),
            areserved_paths_in_diff=AsyncMock(return_value=[]),
            afind_open_pr=AsyncMock(),
            arev_parse=AsyncMock(return_value=head),
            _arun=git_run,
        )
        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "summary": "leaf complete",
            },
        )

        assert close["success"] is True
        record_completion.assert_awaited_once()
        assert close["status"] == "COMPLETED"
        assert (await db.get_integration_checkpoint("t1"))["checkpoint_sha"] == head
        origin = await db.get_task_branch_origin_for_promotion("t1", "repo")
        assert origin["base_sha"] == "a" * 40
        real_orch._phase_integrate.assert_not_awaited()
        real_orch.git.afind_open_pr.assert_not_awaited()
        real_orch.git.areserved_paths_in_diff.assert_awaited_once_with(
            wd, "refs/remotes/origin/main", "refs/heads/aq/t1"
        )

    async def test_train_leaf_root_close_opens_its_pull_request(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """Regression for noble-harbor-74: a childless train root closed with no PR.

        The leaf checkpoint advanced, but nothing opened the PR the train
        seats a root by (``eligible_root_page_on`` requires ``pr_url``).
        """
        from unittest.mock import AsyncMock

        from sqlalchemy import update

        from src.database.tables import repos
        from src.git.manager import RemoteRefState

        wd = await self._setup(db, tmp_path)
        await self._enable_hierarchy_launch(db, tmp_path)
        await db.update_project("p1", hierarchical_integration_mode="train")
        async with db.immediate() as conn:
            await conn.execute(
                update(repos).where(repos.c.id == "repo").values(
                    url="https://github.com/o/r.git", default_branch="main"
                )
            )
        task = await db.get_task("t1")
        await real_orch._launch_session_for_task(
            AssignAction("a1", "t1", "p1"),
            task,
            await db.get_profile("claude-opus"),
            wd,
        )
        session = await db.get_session_for_task("t1")
        head = "b" * 40
        monkeypatch.setattr(real_orch, "_phase_integrate", AsyncMock(), raising=False)
        record_completion = AsyncMock(return_value=head)
        monkeypatch.setattr("src.integration.provenance.record_worker_completion", record_completion)

        async def git_run(args, *, cwd):
            assert cwd == wd
            if args == ["status", "--porcelain"]:
                return ""
            if args == ["rev-parse", "HEAD"]:
                return head
            raise AssertionError(f"unexpected producer Git command: {args!r}")

        pr_url = "https://github.com/o/r/pull/9"
        real_orch.git = SimpleNamespace(
            avalidate_checkout=AsyncMock(return_value=True),
            ahas_remote=AsyncMock(return_value=True),
            aget_current_branch=AsyncMock(return_value="aq/t1"),
            als_remote_ref=AsyncMock(
                return_value=SimpleNamespace(state=RemoteRefState.PRESENT, oid=head)
            ),
            areserved_paths_in_diff=AsyncMock(return_value=[]),
            afind_open_pr=AsyncMock(),
            arev_parse=AsyncMock(return_value=head),
            bind_github_repository=AsyncMock(return_value="binding"),
            acommits_ahead_of_base=AsyncMock(return_value=1),
            acreate_pr=AsyncMock(return_value=pr_url),
            _arun=git_run,
        )
        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "summary": "leaf root complete",
            },
        )

        assert close["success"] is True
        record_completion.assert_awaited_once()
        assert close["status"] == "COMPLETED"
        assert (await db.get_integration_checkpoint("t1"))["checkpoint_sha"] == head
        real_orch.git.acreate_pr.assert_awaited_once()
        assert real_orch.git.acreate_pr.await_args.kwargs["branch"] == "aq/t1"
        assert real_orch.git.acreate_pr.await_args.kwargs["base"] == "main"
        assert (await db.get_task("t1")).pr_url == pr_url
        real_orch._phase_integrate.assert_not_awaited()

    async def test_train_leaf_root_close_survives_a_pull_request_failure(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """A GitHub failure leaves the root COMPLETED for the reconciler to retry."""
        from unittest.mock import AsyncMock

        from sqlalchemy import update

        from src.database.tables import repos
        from src.git.manager import RemoteRefState

        wd = await self._setup(db, tmp_path)
        await self._enable_hierarchy_launch(db, tmp_path)
        await db.update_project("p1", hierarchical_integration_mode="train")
        async with db.immediate() as conn:
            await conn.execute(
                update(repos).where(repos.c.id == "repo").values(
                    url="https://github.com/o/r.git", default_branch="main"
                )
            )
        task = await db.get_task("t1")
        await real_orch._launch_session_for_task(
            AssignAction("a1", "t1", "p1"),
            task,
            await db.get_profile("claude-opus"),
            wd,
        )
        session = await db.get_session_for_task("t1")
        head = "b" * 40
        record_completion = AsyncMock(return_value=head)
        monkeypatch.setattr("src.integration.provenance.record_worker_completion", record_completion)
        monkeypatch.setattr(real_orch, "_phase_integrate", AsyncMock(), raising=False)

        async def git_run(args, *, cwd):
            if args == ["status", "--porcelain"]:
                return ""
            if args == ["rev-parse", "HEAD"]:
                return head
            raise AssertionError(f"unexpected producer Git command: {args!r}")

        real_orch.git = SimpleNamespace(
            avalidate_checkout=AsyncMock(return_value=True),
            ahas_remote=AsyncMock(return_value=True),
            aget_current_branch=AsyncMock(return_value="aq/t1"),
            als_remote_ref=AsyncMock(
                return_value=SimpleNamespace(state=RemoteRefState.PRESENT, oid=head)
            ),
            areserved_paths_in_diff=AsyncMock(return_value=[]),
            afind_open_pr=AsyncMock(),
            arev_parse=AsyncMock(return_value=head),
            bind_github_repository=AsyncMock(return_value="binding"),
            acommits_ahead_of_base=AsyncMock(return_value=1),
            acreate_pr=AsyncMock(side_effect=RuntimeError("GitHub is down")),
            _arun=git_run,
        )
        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "summary": "leaf root complete",
            },
        )

        assert close["success"] is True
        record_completion.assert_awaited_once()
        assert close["status"] == "COMPLETED"
        assert (await db.get_integration_checkpoint("t1"))["checkpoint_sha"] == head
        assert (await db.get_task("t1")).pr_url is None

    async def test_managed_parent_close_suspends_owned_branch_without_legacy_integrate(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        from unittest.mock import AsyncMock

        from src.integration.hierarchy import HierarchyIntegration
        from src.models import PhaseResult

        wd = await self._setup(db, tmp_path)
        await self._enable_hierarchy_launch(db, tmp_path, checkpoint=False)
        await self._install_hierarchy_policy(db)
        async with db.immediate() as conn:
            await conn.execute(
                task_integration_checkpoints.insert().values(
                    task_id="t1",
                    repository_id="repo",
                    branch="aq/t1",
                    checkpoint_sha="a" * 40,
                    generation=0,
                    state="working",
                    version=0,
                    updated_at=time.time(),
                )
            )
        hierarchy = HierarchyIntegration(
            db,
            default_head_resolver=lambda _repo, _branch: "a" * 40,
            checkpoint_verifier=lambda _task, _repo, head: head,
        )
        await hierarchy.file_children("t1", [{"title": "child"}], 0)
        task = await db.get_task("t1")
        await real_orch._launch_session_for_task(
            AssignAction("a1", "t1", "p1"),
            task,
            await db.get_profile("claude-opus"),
            wd,
        )
        session = await db.get_session_for_task("t1")
        head = "b" * 40
        real_orch.git = _handoff_git(head)
        monkeypatch.setattr(
            real_orch,
            "_phase_verify",
            AsyncMock(return_value=PhaseResult.CONTINUE),
            raising=False,
        )
        monkeypatch.setattr(real_orch, "_phase_integrate", AsyncMock(), raising=False)

        async def exact_checkpoint(_db, _git, _task, _repo, requested):
            return requested

        monkeypatch.setattr(
            "src.integration.hierarchy.verify_workspace_checkpoint", exact_checkpoint
        )
        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "summary": "parent producer complete",
            },
        )

        checkpoint = await db.get_integration_checkpoint("t1")
        operation = await db.get_active_parent_integration_operation("t1")
        assert close["success"] is True
        assert close["status"] == "PAUSED"
        assert checkpoint["checkpoint_sha"] == head
        # The suspension transition recorded this close's identity (smart-cascade).
        assert await db.get_task_meta("t1", "accepted_close") == {
            "completion_id": (await db.get_task_completion("t1")).id,
            "session_id": session.id,
            "claim_epoch": (await db.get_task("t1")).claim_epoch,
        }
        assert checkpoint["episode_id"] == operation["episode_id"]
        assert operation["state"] == "active"
        assert (await db.get_task_branch_origin_for_promotion("t1", "repo"))["base_sha"] == (
            "a" * 40
        )
        real_orch._phase_integrate.assert_not_awaited()

    async def test_collector_to_verifier_parent_launch_completes_through_guarded_path(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        from unittest.mock import AsyncMock

        from sqlalchemy import insert

        from src.integration.hierarchy import HierarchyIntegration
        from src.models import PhaseResult

        wd = await self._setup(db, tmp_path)
        ownership, _ = await self._enable_hierarchy_launch(
            db, tmp_path, checkpoint=False
        )
        await self._install_hierarchy_policy(db)
        async with db.immediate() as conn:
            await conn.execute(
                task_integration_checkpoints.insert().values(
                    task_id="t1",
                    repository_id="repo",
                    branch="aq/t1",
                    checkpoint_sha="a" * 40,
                    generation=0,
                    state="working",
                    version=0,
                    updated_at=time.time(),
                )
            )
        hierarchy = HierarchyIntegration(
            db,
            default_head_resolver=lambda _repo, _branch: "a" * 40,
            checkpoint_verifier=lambda _task, _repo, head: head,
        )
        checkpointed = await hierarchy.checkpoint_parent("t1", "a" * 40, 0)
        async with db.immediate() as conn:
            await db._apply_transition(
                conn,
                "t1",
                TaskStatus.PAUSED,
                assigned_agent_id=None,
                _manual_pause_control=True,
            )
        target = BranchKey(repository_id="repo", branch="aq/t1")
        owner = await ownership.get_owner(target)
        worker = Fence(target=target, owner_id="t1", token=owner["fence_token"])
        collector = await ownership.transfer(
            worker, checkpointed["operation_id"], "collector"
        )
        verifier = await ownership.transfer(collector, "t1", "verifier")
        assert (await hierarchy.wake_verifier("t1", verifier))["outcome"] == "woken"
        await db.transition_task("t1", TaskStatus.IN_PROGRESS, assigned_agent_id="a1")

        task = await db.get_task("t1")
        await real_orch._launch_session_for_task(
            AssignAction("a1", "t1", "p1"),
            task,
            await db.get_profile("claude-opus"),
            wd,
        )
        session = await db.get_session_for_task("t1")
        attached = await ownership.get_owner(target)
        assert attached["owner_role"] == "verifier"
        assert attached["handoff_state"] == "attached"
        assert attached["session_id"] == session.id

        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_check_evidence).values(
                    id="check-unit",
                    operation_id=checkpointed["operation_id"],
                    parent_task_id="t1",
                    parent_generation=0,
                    parent_head_sha="a" * 40,
                    producer_id="forge-observer",
                    workflow_id="workflow",
                    run_id="run",
                    attempt=1,
                    required_check_version="parent-v1",
                    checks={"unit": "success"},
                    conclusion="success",
                    classification="conclusive",
                    observed_at=2.0,
                )
            )
        assert (
            await hierarchy.verify_parent("t1", 0, "a" * 40, ["check-unit"])
        )["outcome"] == "verified"
        real_orch.git = _handoff_git()
        monkeypatch.setattr(
            real_orch,
            "_phase_verify",
            AsyncMock(return_value=PhaseResult.CONTINUE),
            raising=False,
        )
        monkeypatch.setattr(real_orch, "_phase_integrate", AsyncMock(), raising=False)

        async def exact_checkpoint(_db, _git, _task, _repo, requested):
            return requested

        monkeypatch.setattr(
            "src.integration.hierarchy.verify_workspace_checkpoint", exact_checkpoint
        )
        close = await real_handler.execute(
            "task_close",
            {
                "task_id": "t1",
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "summary": "verified parent complete",
            },
        )

        assert close["success"] is True
        assert close["status"] == "COMPLETED"
        assert (await db.get_integration_operation(checkpointed["operation_id"]))[
            "state"
        ] == "completed"
        real_orch._phase_integrate.assert_not_awaited()
        # The guarded parent completion recorded this close's identity.
        assert await db.get_task_meta("t1", "accepted_close") == {
            "completion_id": (await db.get_task_completion("t1")).id,
            "session_id": session.id,
            "claim_epoch": (await db.get_task("t1")).claim_epoch,
        }

    async def _aggregate_failure_session(self, db, provider, tmp_path):
        from src.integration.hierarchy import HierarchyIntegration
        from src.integration.records import ParentEpisodeRecords

        await self._setup(db, tmp_path)
        ownership, worker_fence = await self._enable_hierarchy_launch(db, tmp_path)
        await self._install_hierarchy_policy(db)
        head = "a" * 40
        hierarchy = HierarchyIntegration(
            db, default_head_resolver=lambda _repo, _branch: head,
            checkpoint_verifier=lambda _task, _repo, requested: requested,
        )
        await hierarchy.checkpoint_parent("t1", head, 0)
        async with db.immediate() as conn:
            await db._apply_transition(
                conn, "t1", TaskStatus.PAUSED, context="integration_parent_suspended",
                assigned_agent_id=None, _manual_pause_control=True,
            )
        completion = ParentEpisodeRecords(db)
        async with db.immediate() as conn:
            assert (await completion.mark_ready_on(conn, "t1"))["outcome"] == "ready"
        operation = await db.get_active_parent_integration_operation("t1")
        verifier_id = operation["verifier_task_id"]
        collector = await ownership.transfer(worker_fence, operation["id"], "collector")
        fence = await ownership.transfer(collector, verifier_id, "verifier")
        assert (await completion.wake_verifier("t1", fence))["outcome"] == "woken"
        await db.transition_task(verifier_id, TaskStatus.IN_PROGRESS, assigned_agent_id="a1")
        await db.update_workspace("ws1", locked_by_agent_id="a1", locked_by_task_id=verifier_id)
        session = await _make_session(db, provider, sid="verifier-session", task_id=verifier_id)
        return verifier_id, session, head

    @pytest.mark.parametrize("crash", [False, True])
    async def test_aggregate_failure_captures_immutable_head_before_transition(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch, crash
    ):
        from unittest.mock import AsyncMock

        from src.database.queries.result_queries import PENDING_COMPLETION_KEY

        class _Restart(BaseException):
            pass

        verifier_id, session, head = await self._aggregate_failure_session(db, provider, tmp_path)
        # A later local ref lookup cannot replace the original verification subject.
        real_orch.git = SimpleNamespace(
            arev_parse=AsyncMock(return_value="b" * 40),
            avalidate_checkout=AsyncMock(return_value=False),
        )
        save = db.save_task_completion

        async def die(*_args, **_kwargs):
            raise _Restart()

        args = {"task_id": verifier_id, "session_id": session.id, "outcome": "fail",
                "failure_class": "hard", "summary": "found aggregate scope failure"}
        if crash:
            monkeypatch.setattr(db, "save_task_completion", die)
            with pytest.raises(_Restart):
                await real_handler.execute("task_close", args)
            draft = await db.get_task_meta(verifier_id, PENDING_COMPLETION_KEY)
            assert draft["completion"]["commits"] == [head]
            monkeypatch.setattr(db, "save_task_completion", save)
            assert await db.recover_pending_completions() == [verifier_id]
        else:
            result = await real_handler.execute("task_close", args)
            assert result["success"] is True, result
        failure = await db.get_task_completion(verifier_id)
        assert failure.outcome == "fail"
        assert failure.commits == [head]
        assert (await db.get_task(verifier_id)).status is TaskStatus.BLOCKED

    @pytest.mark.parametrize("problem", ["missing_subject", "wrong_commit", "wrong_branch"])
    async def test_aggregate_failure_refuses_unproven_subject_before_transition(
        self, db, real_handler, provider, tmp_path, problem
    ):
        from sqlalchemy import delete

        from src.database.tables import integration_outbox

        verifier_id, session, _ = await self._aggregate_failure_session(db, provider, tmp_path)
        args = {"task_id": verifier_id, "session_id": session.id, "outcome": "fail",
                "failure_class": "hard", "summary": "unproven head"}
        if problem == "missing_subject":
            async with db.immediate() as conn:
                await conn.execute(delete(integration_outbox)
                                   .where(integration_outbox.c.event_type == "task.integration_ready"))
        elif problem == "wrong_commit":
            args["commit"] = "b" * 40
        else:
            await db.update_task(verifier_id, branch_name="aq/other")
        result = await real_handler.execute("task_close", args)
        assert result["result"] == "verification_failed", result
        assert (await db.get_task(verifier_id)).status is TaskStatus.IN_PROGRESS
        assert await db.get_task_completion(verifier_id) is None
        assert await db.get_task_meta(verifier_id, "outcome") is None

    @pytest.mark.parametrize("manual_hold", [False, True])
    async def test_branchless_verifier_close_proves_aggregate_without_pr_to_main(
        self, db, real_orch, real_handler, provider, tmp_path, manual_hold
    ):
        """The real close dispatch uses the verifier proof, not ``_phase_verify``.

        This deliberately supplies a Git double with no ordinary PR result:
        the close must validate the fenced, pushed parent branch, preserve the
        reserved-path inspection, and complete the parent through its guarded
        generation/evidence path without looking for a PR to ``main``.
        """
        from unittest.mock import AsyncMock

        from src.git.manager import RemoteRefState
        from src.integration.hierarchy import HierarchyIntegration
        from src.integration.records import ParentEpisodeRecords

        wd = await self._setup(db, tmp_path)
        ownership, worker_fence = await self._enable_hierarchy_launch(
            db, tmp_path, checkpoint=False
        )
        await self._install_hierarchy_policy(db)
        head = "a" * 40
        async with db.immediate() as conn:
            await conn.execute(
                task_integration_checkpoints.insert().values(
                    task_id="t1",
                    repository_id="repo",
                    branch="aq/t1",
                    checkpoint_sha=head,
                    generation=0,
                    state="working",
                    version=0,
                    updated_at=time.time(),
                )
            )
        hierarchy = HierarchyIntegration(
            db,
            default_head_resolver=lambda _repo, _branch: head,
            checkpoint_verifier=lambda _task, _repo, requested: requested,
        )
        checkpointed = await hierarchy.checkpoint_parent("t1", head, 0)
        async with db.immediate() as conn:
            await db._apply_transition(
                conn, "t1", TaskStatus.PAUSED, context="integration_parent_suspended",
                assigned_agent_id=None, _manual_pause_control=True,
            )
        completion = ParentEpisodeRecords(db)
        async with db.immediate() as conn:
            ready = await completion.mark_ready_on(conn, "t1")
        assert ready["state"] == "integration_ready"
        operation = await db.get_active_parent_integration_operation("t1")
        verifier_id = operation["verifier_task_id"]
        assert verifier_id

        collector = await ownership.transfer(worker_fence, operation["id"], "collector")
        verifier_fence = await ownership.transfer(collector, verifier_id, "verifier")
        assert (await hierarchy.wake_verifier("t1", verifier_fence))["outcome"] == "woken"
        await db.transition_task(verifier_id, TaskStatus.IN_PROGRESS, assigned_agent_id="a1")
        await db.update_workspace("ws1", locked_by_agent_id="a1", locked_by_task_id=verifier_id)
        session = await _make_session(
            db, provider, sid="verifier-session", task_id=verifier_id
        )

        async with db.immediate() as conn:
            await conn.execute(
                integration_check_evidence.insert().values(
                    id="aggregate-check",
                    operation_id=checkpointed["operation_id"],
                    parent_task_id="t1",
                    parent_generation=0,
                    parent_head_sha=head,
                    producer_id="forge-observer",
                    workflow_id="workflow",
                    run_id="run",
                    attempt=1,
                    required_check_version="parent-v1",
                    checks={"unit": "success"},
                    conclusion="success",
                    classification="conclusive",
                    observed_at=2.0,
                )
            )
        assert (await completion.verify_parent("t1", 0, head, ["aggregate-check"]))[
            "outcome"
        ] == "verified"

        async def git_run(args, *, cwd):
            assert cwd == wd
            if args in (
                ["status", "--porcelain"],
                ["rev-parse", "HEAD"],
            ):
                return "" if args[0] == "status" else head
            raise AssertionError(f"unexpected verifier Git command: {args!r}")

        real_orch.git = SimpleNamespace(
            avalidate_checkout=AsyncMock(return_value=True),
            ahas_remote=AsyncMock(return_value=True),
            aget_current_branch=AsyncMock(return_value="aq/t1"),
            als_remote_ref=AsyncMock(
                return_value=SimpleNamespace(state=RemoteRefState.PRESENT, oid=head)
            ),
            areserved_paths_in_diff=AsyncMock(return_value=[]),
            afind_open_pr=AsyncMock(),
            arev_parse=AsyncMock(return_value=head),
            _arun=git_run,
        )
        if manual_hold:
            await db.pause_task("t1")
        close = await real_handler.execute(
            "task_close",
            {
                "task_id": verifier_id,
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "summary": "aggregate verified",
            },
        )

        if manual_hold:
            assert close["result"] == "verification_failed"
            assert (await db.get_task("t1")).status is TaskStatus.PAUSED
            assert (await db.get_task(verifier_id)).status is TaskStatus.IN_PROGRESS
            return
        assert close["success"] is True
        assert close["status"] == "COMPLETED"
        assert (await db.get_task("t1")).status is TaskStatus.COMPLETED
        assert (await db.get_task(verifier_id)).status is TaskStatus.COMPLETED
        assert (await db.get_integration_operation(operation["id"]))["state"] == "completed"
        real_orch.git.afind_open_pr.assert_not_awaited()
        real_orch.git.areserved_paths_in_diff.assert_awaited_once_with(
            wd, "refs/remotes/origin/main", "refs/heads/aq/t1"
        )

    async def test_branchless_verifier_close_replays_after_parent_completion(
        self, db, real_orch, real_handler, provider, tmp_path
    ):
        """A crash after parent completion leaves the verifier closable.

        Complete the parent explicitly before dispatching the verifier's real
        session close.  The replay must retain the ordinary verifier Git proof
        and exact completed operation/checkpoint evidence, without trying the
        legacy PR-to-main path a second time.
        """
        from unittest.mock import AsyncMock

        from src.git.manager import RemoteRefState
        from src.integration.hierarchy import HierarchyIntegration
        from src.integration.records import ParentEpisodeRecords

        wd = await self._setup(db, tmp_path)
        ownership, worker_fence = await self._enable_hierarchy_launch(
            db, tmp_path, checkpoint=False
        )
        await self._install_hierarchy_policy(db)
        head = "b" * 40
        async with db.immediate() as conn:
            await conn.execute(
                task_integration_checkpoints.insert().values(
                    task_id="t1",
                    repository_id="repo",
                    branch="aq/t1",
                    checkpoint_sha=head,
                    generation=0,
                    state="working",
                    version=0,
                    updated_at=time.time(),
                )
            )
        hierarchy = HierarchyIntegration(
            db,
            default_head_resolver=lambda _repo, _branch: head,
            checkpoint_verifier=lambda _task, _repo, requested: requested,
        )
        checkpointed = await hierarchy.checkpoint_parent("t1", head, 0)
        async with db.immediate() as conn:
            await db._apply_transition(
                conn, "t1", TaskStatus.PAUSED, context="integration_parent_suspended",
                assigned_agent_id=None, _manual_pause_control=True,
            )
        completion = ParentEpisodeRecords(db)
        async with db.immediate() as conn:
            assert (await completion.mark_ready_on(conn, "t1"))["state"] == "integration_ready"
        operation = await db.get_active_parent_integration_operation("t1")
        verifier_id = operation["verifier_task_id"]
        assert verifier_id

        collector = await ownership.transfer(worker_fence, operation["id"], "collector")
        verifier_fence = await ownership.transfer(collector, verifier_id, "verifier")
        assert (await hierarchy.wake_verifier("t1", verifier_fence))["outcome"] == "woken"
        await db.transition_task(verifier_id, TaskStatus.IN_PROGRESS, assigned_agent_id="a1")
        await db.update_workspace("ws1", locked_by_agent_id="a1", locked_by_task_id=verifier_id)
        session = await _make_session(
            db, provider, sid="verifier-replay-session", task_id=verifier_id
        )
        async with db.immediate() as conn:
            await conn.execute(
                integration_check_evidence.insert().values(
                    id="aggregate-replay-check",
                    operation_id=checkpointed["operation_id"],
                    parent_task_id="t1",
                    parent_generation=0,
                    parent_head_sha=head,
                    producer_id="forge-observer",
                    workflow_id="workflow",
                    run_id="run",
                    attempt=1,
                    required_check_version="parent-v1",
                    checks={"unit": "success"},
                    conclusion="success",
                    classification="conclusive",
                    observed_at=2.0,
                )
            )
        verified = await completion.verify_parent("t1", 0, head, ["aggregate-replay-check"])
        assert verified["outcome"] == "verified"
        # This is the precise crash boundary: durable parent completion has
        # committed, but the verifier's task-close dispatch has not run.
        completed = await completion.complete_parent("t1", 0, head)
        assert completed["outcome"] == "completed"

        async def git_run(args, *, cwd):
            assert cwd == wd
            if args in (["status", "--porcelain"], ["rev-parse", "HEAD"]):
                return "" if args[0] == "status" else head
            raise AssertionError(f"unexpected verifier Git command: {args!r}")

        real_orch.git = SimpleNamespace(
            avalidate_checkout=AsyncMock(return_value=True),
            ahas_remote=AsyncMock(return_value=True),
            aget_current_branch=AsyncMock(return_value="aq/t1"),
            als_remote_ref=AsyncMock(
                return_value=SimpleNamespace(state=RemoteRefState.PRESENT, oid=head)
            ),
            areserved_paths_in_diff=AsyncMock(return_value=[]),
            afind_open_pr=AsyncMock(),
            arev_parse=AsyncMock(return_value=head),
            _arun=git_run,
        )
        close = await real_handler.execute(
            "task_close",
            {
                "task_id": verifier_id,
                "session_id": session.id,
                "outcome": "pass",
                "work_outcome": "shipped",
                "summary": "aggregate completion replayed",
            },
        )

        assert close["success"] is True
        assert close["status"] == "COMPLETED"
        assert (await db.get_task("t1")).status is TaskStatus.COMPLETED
        assert (await db.get_task(verifier_id)).status is TaskStatus.COMPLETED
        assert (await db.get_integration_operation(operation["id"]))["state"] == "completed"
        replay = await completion.complete_parent("t1", 0, head)
        assert replay["outcome"] == "already_completed"
        real_orch.git.afind_open_pr.assert_not_awaited()
        real_orch.git.areserved_paths_in_diff.assert_awaited_once_with(
            wd, "refs/remotes/origin/main", "refs/heads/aq/t1"
        )

    async def test_verifier_close_waits_for_trusted_evidence_instead_of_rerunning(
        self, db, real_orch, real_handler, provider, tmp_path
    ):
        """Missing trusted CI evidence is a wait, not a stale subject.

        The verifier's own aggregate proof already passed (the 450-test
        focused/schema sweep of calm-grove-25 generation 5); only the trusted
        integration check evidence for this exact generation and head is
        absent.  Answering that close with a fixable "record the aggregate
        verification" issue sent every retry back through the whole local
        suite while no evidence had changed, so:

        * the first refusal names the missing producer and the owner action,
          and says a re-run cannot change it;
        * a replay of that identical subject/evidence state is deduplicated
          into one escalation instead of another identical invitation to
          re-run, and never transitions the task or writes a completion;
        * real trusted evidence arriving completes the parent and the
          verifier through the ordinary path.
        """
        from unittest.mock import AsyncMock

        from src.git.manager import RemoteRefState
        from src.integration.records import ParentEpisodeRecords

        verifier_id, session, head = await self._aggregate_failure_session(db, provider, tmp_path)
        completion = ParentEpisodeRecords(db)
        wd = tmp_path / "wd"

        async def git_run(args, *, cwd):
            assert cwd == str(wd)
            if args in (["status", "--porcelain"], ["rev-parse", "HEAD"]):
                return "" if args[0] == "status" else head
            raise AssertionError(f"unexpected verifier Git command: {args!r}")

        real_orch.git = SimpleNamespace(
            avalidate_checkout=AsyncMock(return_value=True),
            ahas_remote=AsyncMock(return_value=True),
            aget_current_branch=AsyncMock(return_value="aq/t1"),
            als_remote_ref=AsyncMock(
                return_value=SimpleNamespace(state=RemoteRefState.PRESENT, oid=head)
            ),
            areserved_paths_in_diff=AsyncMock(return_value=[]),
            afind_open_pr=AsyncMock(),
            arev_parse=AsyncMock(return_value=head),
            _arun=git_run,
        )
        close_args = {
            "task_id": verifier_id,
            "session_id": session.id,
            "outcome": "pass",
            "work_outcome": "no-op",
            "summary": "aggregate validated locally; waiting for trusted CI evidence",
        }

        first = await real_handler.execute("task_close", dict(close_args))
        assert first["result"] == "verification_failed", first
        assert first["escalated"] is False
        assert len(first["issues"]) == 1
        issue = first["issues"][0]
        assert "awaiting_trusted_verification" in issue
        assert "verification_not_recorded" in issue
        assert "forge-observer" in issue and "parent-v1" in issue
        assert "re-running the test suite does not change this refusal" in issue
        assert "Next owner: parent_ci_producer" in issue
        assert "aq integration status" in issue
        assert "stale_verification" not in issue
        recorded = await db.get_task_meta(verifier_id, TRUSTED_EVIDENCE_WAIT_KEY)
        assert recorded["reason"] == "verification_not_recorded"
        assert recorded["refusals"] == 1
        assert recorded["head_sha"] == head
        assert recorded["required_check_names"] == ["unit"]
        # The refusal keeps the claim, the fence and the task: nothing
        # transitions, nothing is queued, and no completion is recorded.
        assert (await db.get_task(verifier_id)).status is TaskStatus.IN_PROGRESS
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        assert await db.get_task_completion(verifier_id) is None

        replay = await real_handler.execute("task_close", dict(close_args))
        assert replay["result"] == "verification_failed", replay
        assert replay["escalated"] is True
        assert "Refusal 2 on the same subject" in replay["issues"][0]
        assert await db.get_task_meta(verifier_id, TRUSTED_EVIDENCE_WAIT_KEY) == {
            **recorded,
            "refusals": 2,
            "attention_emitted": True,
        }
        assert await db.get_task_meta(verifier_id, "needs_attention") == (
            "awaiting_trusted_verification:verification_not_recorded"
        )
        attention = [
            payload
            for event, payload in real_orch.bus.events
            if event == "task.needs_attention" and payload.get("task_id") == verifier_id
        ]
        assert len(attention) == 1
        third = await real_handler.execute("task_close", dict(close_args))
        assert third["escalated"] is True
        assert len([
            payload for event, payload in real_orch.bus.events
            if event == "task.needs_attention" and payload.get("task_id") == verifier_id
        ]) == 1
        assert (await db.get_task(verifier_id)).status is TaskStatus.IN_PROGRESS
        assert await db.get_task_completion(verifier_id) is None

        operation = await db.get_active_parent_integration_operation("t1")
        async with db.immediate() as conn:
            await conn.execute(
                integration_check_evidence.insert().values(
                    id="aggregate-trusted-check",
                    operation_id=operation["id"],
                    parent_task_id="t1",
                    parent_generation=0,
                    parent_head_sha=head,
                    producer_id="forge-observer",
                    workflow_id="workflow",
                    run_id="run",
                    attempt=1,
                    required_check_version="parent-v1",
                    checks={"unit": "success"},
                    conclusion="success",
                    classification="conclusive",
                    observed_at=2.0,
                )
            )
        changed = await real_handler.execute("task_close", dict(close_args))
        assert changed["result"] == "verification_failed"
        assert changed["escalated"] is False
        assert "Next owner: parent_integration_playbook" in changed["issues"][0]
        changed_wait = await db.get_task_meta(verifier_id, TRUSTED_EVIDENCE_WAIT_KEY)
        assert changed_wait["fingerprint"] != recorded["fingerprint"]
        assert changed_wait["refusals"] == 1
        verified = await completion.verify_parent("t1", 0, head, ["aggregate-trusted-check"])
        assert verified["outcome"] == "verified"

        final = await real_handler.execute(
            "task_close", {**close_args, "summary": "aggregate verified from trusted evidence"}
        )
        assert final["success"] is True, final
        assert final["status"] == "COMPLETED"
        assert (await db.get_task("t1")).status is TaskStatus.COMPLETED
        assert (await db.get_task(verifier_id)).status is TaskStatus.COMPLETED
        assert (await db.get_integration_operation(operation["id"]))["state"] == "completed"

    async def test_hierarchy_transfer_cannot_pass_provider_start_exclusion(
        self, db, real_orch, provider, tmp_path, monkeypatch
    ):
        import asyncio

        wd = await self._setup(db, tmp_path)
        ownership, fence = await self._enable_hierarchy_launch(db, tmp_path)
        entered, proceed = asyncio.Event(), asyncio.Event()
        original_start = provider.start

        async def slow_start(spec):
            entered.set()
            await proceed.wait()
            return await original_start(spec)

        monkeypatch.setattr(provider, "start", slow_start)
        task = await db.get_task("t1")
        launch = asyncio.create_task(
            real_orch._launch_session_for_task(
                AssignAction("a1", "t1", "p1"),
                task,
                await db.get_profile("claude-opus"),
                wd,
            )
        )
        await entered.wait()
        transfer = asyncio.create_task(
            ownership.transfer(fence, "collector", "collector")
        )
        await asyncio.sleep(0.05)
        assert not transfer.done()
        proceed.set()
        await launch
        with pytest.raises(BranchBusy):
            await transfer
        assert (await db.get_session_for_task("t1")).state == "running"

    async def test_hierarchy_starting_reconciler_scan_cannot_release_inflight_launch(
        self, db, real_orch, provider, tmp_path, monkeypatch
    ):
        import asyncio

        wd = await self._setup(db, tmp_path)
        await self._enable_hierarchy_launch(db, tmp_path)
        entered, proceed = asyncio.Event(), asyncio.Event()
        original_start = provider.start

        async def slow_start(spec):
            entered.set()
            await proceed.wait()
            return await original_start(spec)

        monkeypatch.setattr(provider, "start", slow_start)
        task = await db.get_task("t1")
        launch = asyncio.create_task(
            real_orch._launch_session_for_task(
                AssignAction("a1", "t1", "p1"),
                task,
                await db.get_profile("claude-opus"),
                wd,
            )
        )
        await entered.wait()
        starting = await db.get_session_for_task("t1")
        scan = asyncio.create_task(
            real_orch.session_reconciler._step_exits([starting], time.time())
        )
        await asyncio.sleep(0.05)
        assert not scan.done()
        proceed.set()
        await asyncio.gather(launch, scan)

        assert (await db.get_session_for_task("t1")).state == "running"
        assert (await db.get_workspace("ws1")).locked_by_task_id == "t1"

    async def test_hierarchy_failed_start_releases_with_fence_proof_for_retry(
        self, db, real_orch, provider, tmp_path, monkeypatch
    ):
        wd = await self._setup(db, tmp_path)
        ownership, fence = await self._enable_hierarchy_launch(db, tmp_path)
        provider.script_startup_death("s-t1")

        async def confirmed_stopped(_handle):
            return True

        monkeypatch.setattr(provider, "confirm_stopped", confirmed_stopped)
        real_orch.git = _handoff_git()
        real_orch._git_mutex = lambda _path: asyncio.Lock()
        task = await db.get_task("t1")

        await real_orch._launch_session_for_task(
            AssignAction("a1", "t1", "p1"),
            task,
            await db.get_profile("claude-opus"),
            wd,
        )

        session = await db.get_session_for_task("t1")
        owner = await ownership.get_owner(
            BranchKey(repository_id="repo", branch="aq/t1")
        )
        assert session.state == "stopped"
        assert session.end_reason == "startup_exit"
        assert owner["handoff_state"] == "reserved"
        assert owner["fence_token"] == fence.token + 1
        assert owner["session_id"] is None
        assert owner["workspace_id"] is None
        assert owner["confirmed_workspace_id"] == "ws1"
        assert (await db.get_workspace("ws1")).locked_by_task_id is None
        reacquired = await ownership.acquire(
            BranchKey(repository_id="repo", branch="aq/t1"), "t1", "worker"
        )
        assert reacquired.token == fence.token + 1

    async def test_hierarchy_failed_start_unknown_stop_retains_lock_until_transfer_retry(
        self, db, real_orch, provider, tmp_path, monkeypatch
    ):
        wd = await self._setup(db, tmp_path)
        await db.update_agent("a1", current_task_id="t1")
        _ownership, fence = await self._enable_hierarchy_launch(db, tmp_path)
        provider.script_startup_death("s-t1")

        async def unknown_stop(_handle):
            # The fake confirms a stop from its registry; this is the
            # provider that cannot prove the dead start is gone.
            return False

        monkeypatch.setattr(provider, "confirm_stopped", unknown_stop)
        real_orch.git = _handoff_git()
        real_orch._git_mutex = lambda _path: asyncio.Lock()
        task = await db.get_task("t1")
        await real_orch._launch_session_for_task(
            AssignAction("a1", "t1", "p1"),
            task,
            await db.get_profile("claude-opus"),
            wd,
        )

        target = BranchKey(repository_id="repo", branch="aq/t1")
        unresolved = await BranchOwnership(db).get_owner(target)
        assert unresolved["handoff_state"] == "handoff_pending"
        assert (await db.get_workspace("ws1")).locked_by_task_id == "t1"
        unresolved_agent = await db.get_agent("a1")
        assert unresolved_agent.state is AgentState.BUSY
        assert unresolved_agent.current_task_id == "t1"

        async def confirmed_stopped(_handle):
            return True

        monkeypatch.setattr(provider, "confirm_stopped", confirmed_stopped)
        recovered = await BranchOwnership(
            db, confirm_handoff=real_orch.aconfirm_integration_owner_handoff
        ).transfer(fence, "collector", "collector")
        assert recovered.owner_id == "collector"
        assert (await db.get_workspace("ws1")).locked_by_task_id is None
        old_agent = await db.get_agent("a1")
        assert old_agent.state is AgentState.IDLE
        assert old_agent.current_task_id is None

    async def test_hierarchy_crash_after_process_launch_leaves_starting_identity(
        self, db, real_orch, provider, tmp_path, monkeypatch
    ):
        import asyncio

        wd = await self._setup(db, tmp_path)
        ownership, _fence = await self._enable_hierarchy_launch(db, tmp_path)
        original_update = db.publish_session_running

        async def crash_before_running(session_id, instance_token, *, conn=None, **fields):
            if conn is not None:
                raise asyncio.CancelledError
            return await original_update(session_id, instance_token, conn=conn, **fields)

        monkeypatch.setattr(db, "publish_session_running", crash_before_running)
        task = await db.get_task("t1")
        with pytest.raises(asyncio.CancelledError):
            await real_orch._launch_session_for_task(
                AssignAction("a1", "t1", "p1"),
                task,
                await db.get_profile("claude-opus"),
                wd,
            )

        session = await db.get_session_for_task("t1")
        owner = await ownership.get_owner(
            BranchKey(repository_id="repo", branch="aq/t1")
        )
        assert provider.starts
        assert session.state == "starting"
        assert owner["session_id"] == session.id
        assert owner["workspace_id"] == "ws1"

    async def test_unknown_harness_fails_the_launch_loudly(
        self, db, real_orch, provider, tmp_path
    ):
        wd = await self._setup(db, tmp_path)
        task = await db.get_task("t1")
        # Launch re-reads the saved definition after workspace preparation.
        await db.update_profile("claude-opus", harness="does-not-exist")
        profile = await db.get_profile("claude-opus")
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

    async def test_no_op_work_outcome_reaches_git_verification(
        self, db, real_orch, real_handler, provider, tmp_path, monkeypatch
    ):
        """``--work-outcome no-op`` must be visible to git verification.

        The gate's "this task produced no code" decision (task
        swift-ridge-95) keys off ``PipelineContext.work_outcome``; if the
        close surface ever stops threading it through, a read-only reviewer
        is back to being refused for a PR it was never going to open.
        """
        import asyncio

        await self._launch_via_execute_task(db, real_orch, monkeypatch, tmp_path)
        session = await db.get_session_for_task("t1")
        seen: dict = {}

        async def _capture(ctx):
            seen["work_outcome"] = ctx.work_outcome
            return None, True

        monkeypatch.setattr(real_orch, "_run_completion_pipeline", _capture)
        close = await asyncio.wait_for(
            real_handler.execute(
                "task_close",
                {
                    "task_id": "t1",
                    "session_id": session.id,
                    "outcome": "pass",
                    "work_outcome": "no-op",
                    "summary": "Reviewed; nothing to ship.",
                },
            ),
            timeout=2,
        )
        assert close["success"] is True and close["status"] == "COMPLETED"
        assert seen == {"work_outcome": "no-op"}
        assert await db.get_task_meta("t1", "work_outcome") == "no-op"
