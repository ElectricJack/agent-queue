"""SessionReconciler and the exit classifier, on FakeProvider.

Covers: adoption (live kept, dead classified, partial listing deferred),
classifier verdicts, rapid-crash backoff → quarantine with persisted
counters, the stall ladder's rungs and events, drain-ack happy and
premature paths, named idle drain, and the stuck-timeout backstop.

See docs/specs/implementation/session-runtime.md §3.6, §8, §9.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from src.config import AppConfig
from src.database import Database
from src.models import (
    Agent,
    AgentState,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from src.database.tables import task_branch_origins
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchOwnership
from src.providers.availability import DEGRADED, EXHAUSTED
from src.providers.availability_service import ProviderAvailabilityService
from src.sessions import SessionProviderRegistry
from src.sessions.exit_classifier import Verdict, classify_exit
from src.sessions.fake import FakeProvider
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.provider import SessionHandle, SessionSpec
from src.sessions.reconciler import (
    DRAIN_ACK_KEY,
    META_STALL_LAST_ACTION,
    META_STALL_NUDGES,
    SessionReconciler,
)
from tests.db_fixtures import lease_dsn
from src.config import DatabaseConfig

NOW = 1_000_000.0


class _Bus:
    """Records emitted events so ladder rungs can be asserted."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, payload=None):
        self.events.append((event_type, dict(payload or {})))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def payload(self, event_type) -> dict | None:
        for t, p in self.events:
            if t == event_type:
                return p
        return None


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("t.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


@pytest.fixture
def config():
    cfg = AppConfig(database=DatabaseConfig(url=lease_dsn("reconciler")))
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.sessions.lease_ttl_seconds = 480
    cfg.sessions.stall_max_nudges = 3
    cfg.sessions.stall_backoff_seconds = 300
    cfg.sessions.max_restarts = 3
    cfg.sessions.restart_window_seconds = 600
    cfg.sessions.restart_backoff_seconds = 30
    cfg.agents_config.stuck_timeout_seconds = 0  # backstop off unless a test wants it
    return cfg


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def registry(provider):
    class _Reg(SessionProviderRegistry):
        def create(self, name, config=None):
            return provider

    return _Reg({"fake": FakeProvider})


@pytest.fixture
def bus():
    return _Bus()


@pytest.fixture
def reconciler(db, config, registry, bus):
    return SessionReconciler(db, config, registry, bus=bus, epoch="epoch-new")


class _ReleasingOrch:
    """The cleanup half of the orchestrator, backed by the real database.

    ``SessionReconciler`` calls exactly one method on the orchestrator, so
    this is the whole surface.  The body is ``ExecutionMixin.
    release_session_task_resources`` verbatim in behaviour — agent to IDLE,
    workspace lock released — but writing through ``db`` rather than
    recording the call, because a recorder is what hid B1.
    """

    def __init__(self, db):
        self.db = db
        self.calls: list[str] = []
        #: Mirrors ``Orchestrator._running_tasks`` — the reconciler reads it
        #: so a relaunch in progress is not mistaken for a stranded task.
        self._running_tasks: dict = {}

    async def release_session_task_resources(self, task_id, *, agent_id=None, **_kw):
        self.calls.append(task_id)
        await self.db.release_workspaces_for_task(task_id)
        if agent_id:
            await self.db.update_agent(
                agent_id, state=AgentState.IDLE, current_task_id=None
            )


@pytest.fixture
def releasing_reconciler(db, config, registry, bus):
    """A reconciler wired to a real releasing orchestrator."""
    orch = _ReleasingOrch(db)
    rec = SessionReconciler(
        db, config, registry, bus=bus, orchestrator=orch, epoch="epoch-new"
    )
    rec.test_orch = orch
    return rec


class _PoolOrchestrator:
    """Production-shaped pool teardown with observable call boundaries."""

    def __init__(self, db, provider):
        self.db = db
        self.provider = provider
        self.claim_waiters: dict[tuple[str, int], asyncio.Future] = {}
        self._pool_quarantine: dict[tuple[str, str], float] = {}
        self.terminations: list[tuple[str, str]] = []
        self.generic_releases: list[str] = []

    async def _terminate_pool_session(self, session, *, reason, task_status=TaskStatus.READY):
        current = await self.db.get_session(session.id)
        if current is None or current.state == "stopped":
            return
        self.terminations.append((current.id, reason))
        await self.provider.stop(
            SessionHandle(current.name, current.provider, current.instance_token)
        )
        await self.db.terminate_pool_session(current.id, reason=reason, task_status=task_status)
        await self.db.update_session(
            current.id,
            state="stopped",
            desired_state="stopped",
            end_reason=reason,
        )
        if current.agent_id:
            await self.db.update_agent(
                current.agent_id, state=AgentState.IDLE, current_task_id=None
            )

    async def release_session_task_resources(self, task_id, **_kwargs):
        self.generic_releases.append(task_id)


@pytest.fixture
def pool_reconciler(db, config, registry, bus, provider):
    config.swarm.enabled = True
    orch = _PoolOrchestrator(db, provider)
    rec = SessionReconciler(
        db,
        config,
        registry,
        bus=bus,
        orchestrator=orch,
        epoch="epoch-new",
    )
    rec.test_orch = orch
    return rec


async def _busy_agent_and_workspace(db, tmp_dir, *, task_id="t1"):
    """An agent BUSY on *task_id* holding a workspace lock, in the database."""
    await db.create_agent(
        Agent(
            id="a1",
            name="agent-1",
            profile_id="claude-opus",
            state=AgentState.BUSY,
            current_task_id=task_id,
        )
    )
    await db.create_workspace(
        Workspace(
            id="ws1",
            project_id="p1",
            workspace_path=str(tmp_dir),
            source_type=RepoSourceType.LINK,
            name="main",
            locked_by_agent_id="a1",
            locked_by_task_id=task_id,
        )
    )
    await db.transition_task(task_id, TaskStatus.IN_PROGRESS, assigned_agent_id="a1")


async def _task(db, task_id="t1", status=TaskStatus.IN_PROGRESS, agent_id=None):
    await db.create_task(Task(id=task_id, project_id="p1", title="T", description="d"))
    await db.transition_task(task_id, status, assigned_agent_id=agent_id)
    return await db.get_task(task_id)


async def _session(
    db, provider, *, sid="s1", task_id="t1", name=None, started_at=NOW, **overrides
):
    """Create both the provider-side session and its row, consistently."""
    name = name or f"s-{task_id}"
    token = overrides.pop("instance_token", f"tok-{sid}")
    await provider.start(
        SessionSpec(
            session_name=name,
            work_dir="/wd",
            command=("claude",),
            instance_token=token,
        )
    )
    row = SessionRecord(
        id=sid,
        project_id="p1",
        profile_id="claude-opus",
        harness="claude",
        provider="fake",
        name=name,
        lifecycle=overrides.pop("lifecycle", "task"),
        work_dir="/wd",
        epoch=overrides.pop("epoch", "epoch-old"),
        instance_token=token,
        started_at=started_at,
        task_id=task_id,
        state=overrides.pop("state", "running"),
        **overrides,
    )
    await db.create_session(row)
    return row


async def _claimed_pool_session(
    db,
    provider,
    tmp_path,
    *,
    phase="active",
    phase_at=NOW,
    started_at=NOW - 5000,
    last_activity=NOW,
):
    await _task(db)
    await _busy_agent_and_workspace(db, tmp_path)
    await db.transition_task(
        "t1",
        TaskStatus.IN_PROGRESS,
        context="pool_test_claim",
        assigned_agent_id="a1",
        force=True,
    )
    task = await db.get_task("t1")
    return await _session(
        db,
        provider,
        name="p-worker",
        lifecycle="pool",
        agent_id="a1",
        claim_phase=phase,
        claim_phase_at=phase_at,
        last_claim_epoch=task.claim_epoch,
        started_at=started_at,
        last_activity=last_activity,
    )


# ---------------------------------------------------------------------------
# Exit classifier
# ---------------------------------------------------------------------------


class TestExitClassifier:
    def _row(self, started_at=NOW):
        return SessionRecord(
            id="s",
            project_id="p",
            profile_id="pr",
            harness="claude",
            provider="fake",
            name="s-t",
            lifecycle="task",
            work_dir="/wd",
            epoch="e",
            instance_token="tok",
            started_at=started_at,
        )

    def _task(self, status=TaskStatus.IN_PROGRESS):
        return Task(id="t", project_id="p", title="T", description="d", status=status)

    def test_closed_task_is_a_normal_drain(self):
        v = classify_exit(self._row(), self._task(TaskStatus.COMPLETED), "", now=NOW)
        assert v.verdict is Verdict.DRAINED

    def test_named_session_with_no_task_is_a_drain(self):
        v = classify_exit(self._row(), None, "", now=NOW)
        assert v.verdict is Verdict.DRAINED

    @pytest.mark.parametrize(
        "text",
        [
            "Claude usage limit reached",
            "You are approaching your usage limit",
            "HTTP 429 Too Many Requests",
            "overloaded_error",
            "rate limit exceeded",
        ],
    )
    def test_rate_limit_text_wins_over_timing(self, text):
        v = classify_exit(self._row(started_at=NOW - 10), self._task(), text, now=NOW)
        assert v.verdict is Verdict.RATE_LIMIT
        # Restarting straight back into the limit burns the restart budget
        # without ever making progress -- so a cooldown comes with it.
        assert v.cooldown_seconds > 0

    def test_rapid_crash_inside_the_window(self):
        v = classify_exit(
            self._row(started_at=NOW - 30), self._task(), "", now=NOW, rapid_crash_window=600
        )
        assert v.verdict is Verdict.RAPID_CRASH

    def test_intentionally_stopped_pool_worker_is_a_drain_even_with_open_task(self):
        row = replace(
            self._row(started_at=NOW - 30), lifecycle="pool", desired_state="stopped"
        )

        verdict = classify_exit(row, self._task(), "", now=NOW)

        assert verdict.verdict is Verdict.DRAINED
        assert verdict.reason == "pool session intentionally stopped"

    def test_productive_death_outside_the_window(self):
        v = classify_exit(
            self._row(started_at=NOW - 5000),
            self._task(),
            "traceback",
            now=NOW,
            rapid_crash_window=600,
        )
        assert v.verdict is Verdict.PRODUCTIVE_DEATH

    def test_empty_peek_degrades_to_timing_not_a_crash_guess(self):
        v = classify_exit(self._row(started_at=NOW - 5000), self._task(), "", now=NOW)
        assert v.verdict is Verdict.PRODUCTIVE_DEATH


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------


class TestAdoption:
    async def test_live_session_is_rebound_to_the_new_epoch(self, db, provider, reconciler, bus):
        await _task(db)
        await _session(db, provider, sid="s1")
        report = await reconciler.adopt_on_start()
        assert report.adopted == ["s1"]
        assert (await db.get_session("s1")).epoch == "epoch-new"
        assert "session.adopted" in bus.types()
        # The task keeps running -- that is the whole point.
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS

    async def test_dead_row_is_reported_not_adopted(self, db, provider, reconciler):
        await _task(db)
        row = await _session(db, provider, sid="s1")
        await provider.stop(reconciler._handle(row))  # gone from the provider
        report = await reconciler.adopt_on_start()
        assert report.adopted == [] and report.dead == ["s1"]

    async def test_token_mismatch_is_not_adopted(self, db, provider, reconciler):
        """A name-reusing successor must not be mistaken for our session."""
        await _task(db)
        await _session(db, provider, sid="s1", instance_token="tok-a")
        await db.update_session("s1", instance_token="tok-different")
        report = await reconciler.adopt_on_start()
        assert report.adopted == [] and report.dead == ["s1"]

    async def test_partial_listing_defers_instead_of_reaping(self, db, provider, reconciler):
        """Unknown is not dead -- a failed enumeration must change nothing."""
        await _task(db)
        await _session(db, provider, sid="s1")
        provider.script_partial_list(RuntimeError("no server"))
        report = await reconciler.adopt_on_start()
        assert "s-" in report.deferred
        assert report.adopted == [] and report.dead == []
        assert (await db.get_session("s1")).epoch == "epoch-old"

    async def test_unknown_live_session_is_killed_not_left_running(
        self, db, provider, reconciler
    ):
        """A live session with no row has nothing to match markers against.

        Design §8 asks for adopt-if-markers-match else quarantine-kill.
        With no row there is no task, no profile and no instance token to
        match, so the only reachable half is the kill -- and leaving it
        running is the worse failure: an unreachable agent writing into a
        workspace the scheduler believes is free.
        """
        await provider.start(
            SessionSpec(session_name="s-orphan", work_dir="/wd", command=("claude",))
        )
        report = await reconciler.adopt_on_start()
        assert report.unknown_live == ["s-orphan"]
        assert report.unknown_killed == ["s-orphan"]
        assert await provider.list_running("s-") == []

    async def test_disabled_flag_makes_adoption_a_no_op(self, db, provider, reconciler, config):
        config.sessions.enabled = False
        await _task(db)
        await _session(db, provider, sid="s1")
        report = await reconciler.adopt_on_start()
        assert report.total == 0

    async def test_adopt_on_start_flag_is_respected(self, db, provider, reconciler, config):
        config.sessions.adopt_on_start = False
        await _task(db)
        await _session(db, provider, sid="s1")
        assert (await reconciler.adopt_on_start()).total == 0

    async def test_adopted_task_ids_come_from_the_report_not_the_row_set(
        self, db, provider, reconciler
    ):
        """B4: the skip set is what we *confirmed alive*, not what is live.

        The old assertion created a live row and called it "adopted"
        without ever running adoption, which is precisely the bug: the skip
        set has to be derived from :class:`AdoptReport`, because a live row
        proves only that the last daemon wrote one.
        """
        await _task(db)
        await _session(db, provider, sid="s1")
        report = await reconciler.adopt_on_start()
        assert report.adopted == ["s1"]
        assert await reconciler.adopted_task_ids(report) == {"t1"}

    async def test_a_live_row_we_did_not_confirm_is_not_protected(
        self, db, provider, reconciler
    ):
        """The shipped SubprocessProvider hits this on every restart.

        ``SubprocessProvider.list_running`` is an in-memory dict, so after a
        daemon restart it sees nothing and adoption adopts nothing.  If the
        skip set were "every live row" the task would stay IN_PROGRESS with
        its agent BUSY, its workspace locked and its worktree un-cleaned --
        while the detached process kept running unreachable.  Deriving from
        the report degrades that to the blanket reset: lossy, but correct.
        """
        await _task(db)
        row = await _session(db, provider, sid="s1")
        await provider.stop(reconciler._handle(row))  # provider can no longer see it

        report = await reconciler.adopt_on_start()
        assert report.adopted == [] and report.dead == ["s1"]
        # The row is still live in the database...
        assert (await db.get_session("s1")).state == "running"
        # ...but recovery is not told to skip it.
        assert await reconciler.adopted_task_ids(report) == set()

    async def test_a_deferred_prefix_does_not_protect_its_tasks(
        self, db, provider, reconciler
    ):
        """PartialListError defers reaping; it must not also grant immunity."""
        await _task(db)
        await _session(db, provider, sid="s1")
        provider.script_partial_list(RuntimeError("no server"))
        report = await reconciler.adopt_on_start()
        assert "s-" in report.deferred
        assert await reconciler.adopted_task_ids(report) == set()


# ---------------------------------------------------------------------------
# Drain-ack
# ---------------------------------------------------------------------------


class TestDrainAck:
    async def test_ack_with_a_closed_task_stops_the_session(
        self, db, provider, reconciler, bus
    ):
        await _task(db, status=TaskStatus.COMPLETED)
        row = await _session(db, provider)
        await provider.set_meta(reconciler._handle(row), DRAIN_ACK_KEY, "1")
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "stopped"
        assert "session.drain_acked" in bus.types()

    async def test_premature_ack_nudges_instead_of_killing(
        self, db, provider, reconciler, bus
    ):
        """Acking must never be a way to end a task."""
        await _task(db, status=TaskStatus.IN_PROGRESS)
        row = await _session(db, provider)
        await provider.set_meta(reconciler._handle(row), DRAIN_ACK_KEY, "1")
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "running"
        assert "session.premature_drain" in bus.types()
        assert provider.sent_nudges
        assert "aq task close" in provider.sent_nudges[0][1]

    async def test_premature_ack_is_cleared_so_it_does_not_nudge_forever(
        self, db, provider, reconciler
    ):
        await _task(db)
        row = await _session(db, provider)
        await provider.set_meta(reconciler._handle(row), DRAIN_ACK_KEY, "1")
        await reconciler.tick(now=NOW)
        await reconciler.tick(now=NOW)
        assert len(provider.sent_nudges) == 1

    async def test_no_ack_means_no_action(self, db, provider, reconciler):
        await _task(db)
        await _session(db, provider)
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "running"


# ---------------------------------------------------------------------------
# Exit handling
# ---------------------------------------------------------------------------


class TestExitHandling:
    async def test_stale_hierarchy_starting_release_is_fenced_and_transferable(
        self, db, provider, registry, bus, config, tmp_path, monkeypatch
    ):
        from src.orchestrator.workspace import WorkspaceMixin

        await db.create_repo(
            RepoConfig(id="repo", project_id="p1", source_type=RepoSourceType.LINK)
        )
        await db.update_project(
            "p1",
            hierarchical_integration_mode="hierarchy",
            integration_repository_id="repo",
        )
        await _task(db)
        await db.update_task("t1", repo_id="repo", branch_name="aq/t1")
        await _busy_agent_and_workspace(db, tmp_path)
        row = await _session(
            db,
            provider,
            started_at=NOW - 100,
            state="starting",
            agent_id="a1",
        )
        await db.update_session(row.id, work_dir=str(tmp_path))
        row = await db.get_session(row.id)
        provider.script_death(row.name)
        async with db.immediate() as conn:
            await conn.execute(
                task_branch_origins.insert().values(
                    id="origin-t1",
                    task_id="t1",
                    repository_id="repo",
                    parent_ref="main",
                    base_sha="a" * 40,
                    creation_generation=0,
                    reserved=True,
                    materialized=True,
                    created_at=NOW - 200,
                    materialized_at=NOW - 200,
                )
            )
        target = BranchKey(repository_id="repo", branch="aq/t1")
        ownership = BranchOwnership(db)
        fence = await ownership.acquire(target, "t1", "worker")
        await ownership.attach(fence, row.id, "ws1", agent_id="a1")
        confirmations = []

        async def confirmed_stopped(handle):
            confirmations.append(handle.name)
            return True

        monkeypatch.setattr(provider, "confirm_stopped", confirmed_stopped)

        class Git:
            detached = False

            async def aget_current_branch(self, _path, *, strict=False):
                return "HEAD" if self.detached else "aq/t1"

            async def _arun_unlocked(self, args, *, cwd):
                if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return "HEAD" if self.detached else "aq/t1"
                if args == ["status", "--porcelain"] or args == ["fetch", "origin"]:
                    return ""
                if args in (
                    ["rev-parse", "refs/heads/aq/t1"],
                    ["rev-parse", "refs/remotes/origin/aq/t1"],
                    ["rev-parse", "HEAD"],
                ):
                    return "a" * 40
                if args == ["switch", "--detach", "a" * 40]:
                    self.detached = True
                    return ""
                raise AssertionError(f"unexpected handoff git call: {args!r} in {cwd}")

        class IntegrationOrchestrator(WorkspaceMixin):
            def __init__(self):
                self.db = db
                self.git = Git()
                self.session_providers = registry
                self.config = config
                self._git_locks = {}
                self._running_tasks = {}

            def _git_mutex(self, _path):
                return asyncio.Lock()

            async def release_session_task_resources(
                self, task_id, *, agent_id=None, **_kwargs
            ):
                await self.db.release_workspaces_for_task(task_id)
                if agent_id:
                    await self.db.update_agent(
                        agent_id, state=AgentState.IDLE, current_task_id=None
                    )

        orch = IntegrationOrchestrator()
        reconciler = SessionReconciler(
            db, config, registry, bus=bus, orchestrator=orch, epoch="epoch-new"
        )
        await reconciler._step_exits([row], NOW)

        owner = await ownership.get_owner(target)
        assert confirmations == [row.name]
        assert owner["handoff_state"] == "reserved"
        assert owner["fence_token"] == fence.token + 1
        assert owner["confirmed_workspace_id"] == "ws1"
        assert owner["session_id"] is None and owner["workspace_id"] is None
        assert (await db.get_workspace("ws1")).locked_by_task_id is None
        collector = await ownership.transfer(
            Fence(target=target, owner_id="t1", token=owner["fence_token"]),
            "collector",
            "collector",
        )
        assert collector.owner_id == "collector"

    async def test_rate_limit_pauses_the_task_and_sleeps_the_session(
        self, db, provider, reconciler, bus
    ):
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 100)
        provider.feed_output(row.name, "Claude usage limit reached", activity=False)
        provider.script_death(row.name)
        await reconciler.tick(now=NOW)
        session = await db.get_session("s1")
        assert session.state == "sleeping" and session.sleep_reason == "rate_limit"
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        assert bus.payload("session.exited")["verdict"] == "rate_limit"

    async def test_rapid_crash_bumps_restarts_and_pauses_with_backoff(
        self, db, provider, reconciler, bus
    ):
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 10)
        provider.script_death(row.name)
        await reconciler.tick(now=NOW)
        session = await db.get_session("s1")
        assert session.restarts == 1 and session.state == "stopped"
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        assert "task.restarted" in bus.types()

    async def test_rapid_crashes_quarantine_once_the_budget_is_spent(
        self, db, provider, reconciler, bus, config
    ):
        """Counters live on the row so the ladder survives a daemon restart."""
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 10, restarts=2)
        provider.script_death(row.name)
        await reconciler.tick(now=NOW)  # bump 2 -> 3 == max_restarts
        session = await db.get_session("s1")
        assert session.state == "quarantined"
        assert session.quarantined_at == NOW
        assert (await db.get_task("t1")).status is TaskStatus.BLOCKED
        assert await db.get_task_meta("t1", "needs_attention") == "session_rapid_crash"
        assert "session.quarantined" in bus.types()
        assert "task.quarantined" in bus.types()
        failed = bus.payload("task.failed")
        assert failed is not None, bus.types()
        assert failed["status"] == TaskStatus.BLOCKED.value
        assert failed["context"] == "session_rapid_crash"

    async def test_productive_death_pauses_with_a_backoff_never_silently_ready(
        self, db, provider, reconciler, bus, config, caplog
    ):
        """Exit-without-close is a transient operational failure, not BLOCKED.

        BLOCKED means "a dependency or gate holds this"; a worker that died
        mid-task with retry budget left is the retry ladder's business.  The
        exit still has to be *loud*: needs_attention, an INFO line naming
        the reason, and a durable comment.
        """
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 100_000)
        provider.script_death(row.name)
        with caplog.at_level("INFO", logger="src.sessions.reconciler"):
            await reconciler.tick(now=NOW)
        task = await db.get_task("t1")
        assert task.status is TaskStatus.PAUSED
        assert task.resume_after == NOW + config.sessions.restart_backoff_seconds
        assert task.retry_count == 1
        assert await db.get_task_meta("t1", "needs_attention") == "session_exited_open"
        assert "task.needs_attention" in bus.types()
        assert "task.restarted" in bus.types()
        assert any(
            "exited without close" in r.getMessage() for r in caplog.records
        ), "the transition must be logged with its reason"

    async def test_productive_death_records_a_durable_incident_comment(
        self, db, provider, reconciler
    ):
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 100_000)
        provider.script_death(row.name)
        await reconciler.tick(now=NOW)
        listed = await db.list_task_comments("t1")
        bodies = [c["body"] for c in listed["comments"]]
        assert any("exited without calling" in b for b in bodies), bodies
        assert any("session_exited_open" in b for b in bodies), bodies

    async def test_productive_death_blocks_once_the_retry_budget_is_spent(
        self, db, provider, reconciler, bus
    ):
        """Only the terminal leg is BLOCKED — that is the supervisor's incident."""
        await _task(db)
        await db.update_task("t1", retry_count=3, max_retries=3)
        row = await _session(db, provider, started_at=NOW - 100_000)
        provider.script_death(row.name)
        await reconciler.tick(now=NOW)
        task = await db.get_task("t1")
        assert task.status is TaskStatus.BLOCKED
        assert await db.get_task_meta("t1", "needs_attention") == "session_exited_open"
        assert "task.restarted" not in bus.types()
        # The terminal leg is a *failure*: task.failed is what the reflection
        # playbook and the failure-notification path trigger on, so an exit
        # that ends here has to raise it rather than going silent.
        failed = bus.payload("task.failed")
        assert failed is not None, bus.types()
        assert failed["task_id"] == "t1"
        assert failed["status"] == TaskStatus.BLOCKED.value
        assert failed["context"] == "session_exited_without_close_exhausted"
        assert "exited without close" in failed["error"]

    async def test_productive_death_with_budget_left_does_not_emit_task_failed(
        self, db, provider, reconciler, bus
    ):
        """A retriable exit is paused for a retry, not reported as failed."""
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 100_000)
        provider.script_death(row.name)
        await reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        assert "task.failed" not in bus.types()

    async def test_exit_without_close_is_explained_by_aq_task_explain(
        self, db, provider, reconciler
    ):
        """``aq task explain`` must name the reason, not go silent."""
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 100_000)
        provider.script_death(row.name)
        await reconciler.tick(now=NOW)
        assert await db.get_task_meta("t1", "needs_attention") == "session_exited_open"
        task = await db.get_task("t1")
        assert task.status is TaskStatus.PAUSED and task.resume_after

    async def test_closed_task_with_a_dead_process_just_stops(self, db, provider, reconciler):
        await _task(db, status=TaskStatus.COMPLETED)
        row = await _session(db, provider)
        provider.script_death(row.name)
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "stopped"

    async def test_a_live_process_is_left_alone(self, db, provider, reconciler):
        await _task(db)
        await _session(db, provider)
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "running"
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Pool lifecycle
# ---------------------------------------------------------------------------


class TestPoolPrepareTimeout:
    async def test_claiming_pool_releases_expired_claim_and_wakes_waiter(
        self, db, provider, pool_reconciler, bus, tmp_path
    ):
        row = await _claimed_pool_session(
            db,
            provider,
            tmp_path,
            phase="claiming",
            phase_at=NOW - 1000,
        )
        task = await db.get_task("t1")
        waiter = asyncio.get_running_loop().create_future()
        pool_reconciler.test_orch.claim_waiters[(row.id, task.claim_epoch)] = waiter

        await pool_reconciler._step_prepare_timeout([row], NOW)

        current = await db.get_session(row.id)
        task = await db.get_task("t1")
        agent = await db.get_agent("a1")
        workspace = await db.get_workspace("ws1")
        assert (current.task_id, current.claim_phase, current.claim_phase_at) == (
            None,
            None,
            None,
        )
        assert current.last_claim_result == "prepare_failed"
        assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)
        assert agent.state is AgentState.IDLE and agent.current_task_id is None
        assert workspace.locked_by_agent_id == "a1"
        assert workspace.locked_by_task_id is None
        assert waiter.result() == "prepare_failed"
        assert pool_reconciler.test_orch.claim_waiters == {}
        assert bus.payload("session.claim_timeout") == {
            "session_id": row.id,
            "task_id": "t1",
        }

    async def test_preparing_pool_without_task_clears_only_phase(
        self, db, provider, pool_reconciler
    ):
        row = await _session(
            db,
            provider,
            sid="pool-idle",
            task_id=None,
            name="p-idle",
            lifecycle="pool",
            claim_phase="preparing",
            claim_phase_at=NOW - 1000,
        )

        await pool_reconciler._step_prepare_timeout([row], NOW)

        current = await db.get_session(row.id)
        assert current.state == "running"
        assert current.task_id is None
        assert current.claim_phase is None and current.claim_phase_at is None
        assert len(provider.starts) == 1
        assert pool_reconciler.test_orch.terminations == []
        assert await db.list_tasks() == []


class TestAbandonedPoolClaimLoop:
    async def _failed_idle_pool(self, db, provider, *, sid="pool-idle", **overrides):
        """A live worker which received ``prepare_failed`` and then went quiet."""
        overrides.setdefault("last_activity", NOW - 1_000)
        return await _session(
            db,
            provider,
            sid=sid,
            task_id=None,
            name=f"p-{sid}",
            lifecycle="pool",
            last_claim_result="prepare_failed",
            **overrides,
        )

    async def test_recycles_stale_unclaimed_prepare_failure(self, db, provider, pool_reconciler):
        row = await self._failed_idle_pool(db, provider)

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        current = await db.get_session(row.id)
        assert (current.state, current.desired_state) == ("stopped", "stopped")
        assert pool_reconciler.test_orch.terminations == [(row.id, "claim_loop_stalled")]

    async def test_recycles_a_worker_that_never_reached_its_claim_loop(
        self, db, provider, pool_reconciler
    ):
        """swift-dune: a harness parked on a provider screen never claims.

        Codex's usage-limit screen is painted in answer to the bootstrap
        prompt, long after the startup dialog pass, so the session is
        ``running`` with no claim result at all.  It holds its agent and
        workspace and takes no work; the claim-loop grace applies to it just
        as it does after a prepare failure.
        """
        row = await _session(
            db, provider, sid="pool-stuck", task_id=None, name="p-pool-stuck",
            lifecycle="pool", last_activity=NOW - 1_000,
        )
        assert row.last_claim_result is None

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        current = await db.get_session(row.id)
        assert (current.state, current.desired_state) == ("stopped", "stopped")
        assert pool_reconciler.test_orch.terminations == [(row.id, "claim_loop_stalled")]

    async def test_idle_worker_inside_its_long_poll_window_is_not_recycled(
        self, db, provider, pool_reconciler
    ):
        # A healthy worker's last claim entry is at most one long poll (plus
        # a turn) old; the grace is two windows.
        grace = pool_reconciler._pool_claim_loop_stall_seconds()
        row = await _session(
            db, provider, sid="pool-polling", task_id=None, name="p-pool-polling",
            lifecycle="pool", last_activity=NOW - grace + 5,
        )

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        current = await db.get_session(row.id)
        assert (current.state, current.desired_state) == ("running", "running")
        assert pool_reconciler.test_orch.terminations == []

    async def test_successor_after_recycle_fence_is_not_terminated(
        self, db, provider, pool_reconciler, monkeypatch
    ):
        row = await self._failed_idle_pool(db, provider)
        recycle = db.request_idle_pool_recycle

        async def replace_after_fence(*args, **kwargs):
            result = await recycle(*args, **kwargs)
            assert result
            await db.update_session(
                row.id, instance_token="successor-instance", desired_state="running"
            )
            return result

        monkeypatch.setattr(db, "request_idle_pool_recycle", replace_after_fence)
        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        current = await db.get_session(row.id)
        assert current.instance_token == "successor-instance"
        assert current.desired_state == "running"
        assert pool_reconciler.test_orch.terminations == []

    async def test_recent_claim_loop_progress_is_not_recycled(self, db, provider, pool_reconciler):
        row = await self._failed_idle_pool(db, provider, last_activity=NOW - 1)

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        current = await db.get_session(row.id)
        assert (current.state, current.desired_state) == ("running", "running")
        assert pool_reconciler.test_orch.terminations == []

    @pytest.mark.parametrize(
        ("state", "desired_state"),
        [("sleeping", "sleeping"), ("draining", "stopped"), ("quarantined", "stopped")],
    )
    async def test_respects_intentional_non_running_states(
        self, db, provider, pool_reconciler, state, desired_state
    ):
        row = await self._failed_idle_pool(
            db, provider, sid=f"pool-{state}", state=state, desired_state=desired_state
        )

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        assert (await db.get_session(row.id)).state == state
        assert pool_reconciler.test_orch.terminations == []

    async def test_cas_refuses_a_worker_that_began_another_claim(self, db, provider, pool_reconciler):
        row = await self._failed_idle_pool(db, provider)
        await db.update_session(row.id, claim_phase="claiming", claim_phase_at=NOW)

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        current = await db.get_session(row.id)
        assert current.state == "running" and current.desired_state == "running"
        assert current.claim_phase == "claiming"
        assert pool_reconciler.test_orch.terminations == []

    async def test_prepare_timeout_is_disabled_when_swarm_is_disabled(
        self, db, provider, pool_reconciler, bus, tmp_path
    ):
        row = await _claimed_pool_session(
            db,
            provider,
            tmp_path,
            phase="claiming",
            phase_at=NOW - 1000,
        )
        pool_reconciler.config.swarm.enabled = False

        await pool_reconciler._step_prepare_timeout([row], NOW)

        current = await db.get_session(row.id)
        task = await db.get_task("t1")
        assert current.task_id == "t1" and current.claim_phase == "claiming"
        assert task.status is TaskStatus.IN_PROGRESS
        assert "session.claim_timeout" not in bus.types()


class TestPoolLifecycle:
    async def test_killed_pool_worker_requeues_task_without_quarantining_pool(
        self, db, provider, pool_reconciler, tmp_path
    ):
        row = await _claimed_pool_session(db, provider, tmp_path, started_at=NOW - 30)
        await db.update_session(row.id, desired_state="stopped")
        provider.script_death(row.name)

        await pool_reconciler.tick(now=NOW)

        task = await db.get_task("t1")
        assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)
        assert pool_reconciler.test_orch.terminations == [(row.id, "drained")]
        assert ("p1", "claude-opus") not in pool_reconciler.test_orch._pool_quarantine

    async def test_pool_exit_terminates_pool_and_returns_held_task_ready(
        self, db, provider, pool_reconciler, tmp_path
    ):
        row = await _claimed_pool_session(db, provider, tmp_path)
        provider.script_death(row.name)

        await pool_reconciler.tick(now=NOW)

        current = await db.get_session(row.id)
        task = await db.get_task("t1")
        assert pool_reconciler.test_orch.terminations == [(row.id, "productive_death")]
        assert current.state == "stopped" and current.restarts == 0
        assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)
        assert len(await db.list_sessions()) == 1
        assert len(provider.starts) == 1
        assert pool_reconciler.test_orch.generic_releases == []

    async def test_rate_limited_pool_quarantines_pool_key_but_not_task(
        self, db, provider, pool_reconciler, tmp_path
    ):
        row = await _claimed_pool_session(db, provider, tmp_path)
        provider.feed_output(row.name, "rate limit exceeded", activity=False)
        provider.script_death(row.name)

        await pool_reconciler.tick(now=NOW)

        current = await db.get_session(row.id)
        task = await db.get_task("t1")
        assert current.sleep_reason == "rate_limit"
        assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)
        assert pool_reconciler.test_orch._pool_quarantine[("p1", "claude-opus")] > NOW
        assert task.status not in (TaskStatus.PAUSED, TaskStatus.BLOCKED)
        assert pool_reconciler.test_orch.terminations == [(row.id, "rate_limit")]

    async def test_terminal_pool_task_releases_hold_without_terminating_worker(
        self, db, provider, pool_reconciler, tmp_path
    ):
        row = await _claimed_pool_session(db, provider, tmp_path)
        await db.transition_task("t1", TaskStatus.COMPLETED, context="test", force=True)

        await pool_reconciler._step_orphans([row], NOW)

        assert pool_reconciler.test_orch.terminations == []
        assert pool_reconciler.test_orch.generic_releases == []
        session = await db.get_session(row.id)
        assert (session.state, session.desired_state, session.task_id) == ("running", "stopped", None)
        assert (await db.get_agent(row.agent_id)).current_task_id is None
        assert (await db.get_task("t1")).status is TaskStatus.COMPLETED

    async def test_mid_prepare_pool_is_excluded_from_stall_ladder(
        self, db, provider, pool_reconciler, tmp_path
    ):
        row = await _claimed_pool_session(
            db,
            provider,
            tmp_path,
            phase="preparing",
            phase_at=NOW,
            last_activity=NOW - 1000,
        )
        await db.set_task_meta("t1", META_STALL_NUDGES, "99")

        await pool_reconciler._step_stall_ladder([row], NOW)

        assert provider.sent_nudges == []
        assert pool_reconciler.test_orch.terminations == []
        assert (await db.get_session(row.id)).state == "running"
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS

    async def test_stalled_active_pool_interrupts_then_terminates_without_resume(
        self, db, provider, pool_reconciler, config, tmp_path
    ):
        row = await _claimed_pool_session(
            db,
            provider,
            tmp_path,
            last_activity=NOW - 1000,
        )
        process = provider.sessions[row.name]
        await db.set_task_meta("t1", META_STALL_NUDGES, str(config.sessions.stall_max_nudges))
        await db.set_task_meta("t1", META_STALL_LAST_ACTION, "0")

        await pool_reconciler._step_stall_ladder([row], NOW)

        current = await db.get_session(row.id)
        task = await db.get_task("t1")
        assert process.interrupts == 1
        assert pool_reconciler.test_orch.terminations == [(row.id, "stalled")]
        assert current.state == "stopped" and current.restarts == 0
        assert task.status is TaskStatus.READY
        assert len(provider.starts) == 1
        assert await db.get_task_meta("t1", "session_resume_key") is None

    async def test_backstop_uses_last_activity_for_pool_not_started_at(
        self, db, provider, pool_reconciler, config, tmp_path
    ):
        config.agents_config.stuck_timeout_seconds = 300
        row = await _claimed_pool_session(
            db,
            provider,
            tmp_path,
            started_at=NOW - 10_000,
            last_activity=NOW - 10,
        )

        await pool_reconciler._step_backstop([row], NOW)
        assert pool_reconciler.test_orch.terminations == []
        assert (await db.get_session(row.id)).state == "running"

        await db.update_session(row.id, last_activity=NOW - 1000)
        stale = await db.get_session(row.id)
        await pool_reconciler._step_backstop([stale], NOW)
        assert pool_reconciler.test_orch.terminations == [(row.id, "stuck_timeout")]
        assert (await db.get_task("t1")).status is TaskStatus.READY


# ---------------------------------------------------------------------------
# Stall ladder
# ---------------------------------------------------------------------------


class TestStallLadder:
    async def _stalled(self, db, provider, idle=1000.0):
        await _task(db)
        row = await _session(
            db, provider, started_at=NOW - 5000, last_activity=NOW - idle
        )
        # FakeProvider stamps activity from the real clock; the reconciler
        # (correctly) folds observed activity back into the row it measures
        # idleness from, so the fake's clock has to live on the test's
        # timeline or every session looks freshly active.
        provider.sessions[row.name].activity = NOW - idle
        return row

    async def test_first_rung_emits_stalled_and_nudged(self, db, provider, reconciler, bus):
        await self._stalled(db, provider)
        await reconciler.tick(now=NOW)
        assert "task.stalled" in bus.types()
        assert "task.nudged" in bus.types()
        assert provider.sent_nudges
        assert await db.get_task_meta("t1", META_STALL_NUDGES) == "1"

    async def test_within_the_lease_nothing_happens(self, db, provider, reconciler, bus):
        await self._stalled(db, provider, idle=10.0)
        await reconciler.tick(now=NOW)
        assert "task.stalled" not in bus.types()
        assert provider.sent_nudges == []

    async def test_backoff_prevents_a_second_nudge_in_the_same_window(
        self, db, provider, reconciler
    ):
        await self._stalled(db, provider)
        await reconciler.tick(now=NOW)
        await reconciler.tick(now=NOW + 10)
        assert len(provider.sent_nudges) == 1

    async def test_ladder_climbs_after_the_backoff_elapses(self, db, provider, reconciler):
        await self._stalled(db, provider)
        await reconciler.tick(now=NOW)
        await reconciler.tick(now=NOW + 400)
        assert len(provider.sent_nudges) == 2
        assert await db.get_task_meta("t1", META_STALL_NUDGES) == "2"

    async def test_after_max_nudges_it_restarts_with_resume(
        self, db, provider, reconciler, bus, config
    ):
        await self._stalled(db, provider)
        await db.set_task_meta("t1", META_STALL_NUDGES, str(config.sessions.stall_max_nudges))
        await db.set_task_meta("t1", META_STALL_LAST_ACTION, "0")
        await reconciler.tick(now=NOW)
        session = await db.get_session("s1")
        assert session.state == "stopped" and session.restarts == 1
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        assert "task.restarted" in bus.types()
        # The rung counter resets so the next session starts a fresh ladder.
        assert await db.get_task_meta("t1", META_STALL_NUDGES) == "0"

    async def test_restart_budget_exhaustion_quarantines(
        self, db, provider, reconciler, bus, config
    ):
        await self._stalled(db, provider)
        await db.update_session("s1", restarts=config.sessions.max_restarts)
        await db.set_task_meta("t1", META_STALL_NUDGES, str(config.sessions.stall_max_nudges))
        await db.set_task_meta("t1", META_STALL_LAST_ACTION, "0")
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "quarantined"
        assert (await db.get_task("t1")).status is TaskStatus.BLOCKED

    async def test_a_swallowed_nudge_still_advances_the_rung(
        self, db, provider, reconciler, bus
    ):
        """NotSubmitted must not leave the ladder stuck on rung 1 forever."""
        row = await self._stalled(db, provider)
        provider.swallow_next_nudge(row.name)
        await reconciler.tick(now=NOW)
        assert await db.get_task_meta("t1", META_STALL_NUDGES) == "1"
        # ...but it is not reported as delivered.
        assert "task.nudged" not in bus.types()

    async def test_a_swallowed_nudge_warns_and_announces_the_stuck_composer(
        self, db, provider, reconciler, bus, caplog
    ):
        """The stall in stark-journey-63: the text stays in the composer.

        Info-level "will retry" was how that stayed invisible for hours —
        the retry can never happen while the composer is dirty, so the
        operator needs both a WARNING and an event the dashboard can hang
        "message stuck" off.
        """
        import logging

        row = await self._stalled(db, provider)
        provider.swallow_next_nudge(row.name)
        with caplog.at_level(logging.WARNING, logger="src.sessions.reconciler"):
            await reconciler.tick(now=NOW)

        assert any(
            rec.levelno == logging.WARNING and "not submitted" in rec.getMessage()
            and row.name in rec.getMessage() and "t1" in rec.getMessage()
            for rec in caplog.records
        ), [r.getMessage() for r in caplog.records]

        payload = bus.payload("session.nudge_unsubmitted")
        assert payload is not None
        assert payload["session_id"] == "s1"
        assert payload["name"] == row.name
        assert payload["task_id"] == "t1"
        assert payload["composer_dirty"] is True

    async def test_a_draft_defers_without_consuming_restart_attempts(
        self, db, provider, reconciler, bus, monkeypatch
    ):
        from src.sessions.provider import NudgeDeferred

        await self._stalled(db, provider)
        send = provider.nudge

        async def draft_present(handle, text):
            raise NudgeDeferred("existing user draft")

        monkeypatch.setattr(provider, "nudge", draft_present)
        for tick in range(5):
            await reconciler.tick(now=NOW + 400 * tick)
            assert await db.get_task_meta("t1", META_STALL_NUDGES) is None
            assert await db.get_task_meta("t1", META_STALL_LAST_ACTION) is None
            assert (await db.get_session("s1")).state == "running"
            assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert "task.nudged" not in bus.types()
        assert "task.restarted" not in bus.types()
        assert "task.stalled" not in bus.types()

        # When the user clears/submits their own draft, the first actual
        # reminder consumes exactly one rung.
        monkeypatch.setattr(provider, "nudge", send)
        await reconciler.tick(now=NOW + 400 * 5)
        assert len(provider.sent_nudges) == 1
        assert await db.get_task_meta("t1", META_STALL_NUDGES) == "1"
        assert bus.types().count("task.stalled") == 1

    async def test_lease_ttl_zero_disables_the_ladder(self, db, provider, reconciler, config):
        config.sessions.lease_ttl_seconds = 0
        await self._stalled(db, provider)
        await reconciler.tick(now=NOW)
        assert provider.sent_nudges == []

    async def test_named_sessions_are_not_on_the_task_ladder(
        self, db, provider, reconciler
    ):
        await _session(
            db,
            provider,
            sid="n1",
            task_id=None,
            name="n-supervisor",
            lifecycle="named",
            started_at=NOW - 5000,
            last_activity=NOW - 5000,
        )
        await reconciler.tick(now=NOW)
        assert provider.sent_nudges == []


# ---------------------------------------------------------------------------
# Named desired-state and backstop
# ---------------------------------------------------------------------------


class TestNamedSessions:
    async def test_idle_named_session_drains_to_sleeping(
        self, db, provider, reconciler, bus, monkeypatch
    ):
        await _session(
            db,
            provider,
            sid="n1",
            task_id=None,
            name="n-supervisor",
            lifecycle="named",
            started_at=NOW - 5000,
            last_activity=NOW - 5000,
        )
        # Keep the fake's activity clock on the test's timeline (see the
        # note in TestStallLadder._stalled).
        provider.sessions["n-supervisor"].activity = NOW - 5000

        class _P:
            idle_timeout = 600

        async def _get_profile(_pid):
            return _P()

        monkeypatch.setattr(db, "get_profile", _get_profile)
        await reconciler.tick(now=NOW)
        session = await db.get_session("n1")
        assert session.state == "sleeping" and session.sleep_reason == "idle_timeout"
        assert "session.sleeping" in bus.types()

    async def test_no_idle_timeout_means_no_drain(self, db, provider, reconciler, monkeypatch):
        await _session(
            db,
            provider,
            sid="n1",
            task_id=None,
            name="n-supervisor",
            lifecycle="named",
            started_at=NOW - 5000,
            last_activity=NOW - 5000,
        )

        class _P:
            idle_timeout = 0

        async def _get_profile(_pid):
            return _P()

        monkeypatch.setattr(db, "get_profile", _get_profile)
        await reconciler.tick(now=NOW)
        assert (await db.get_session("n1")).state == "running"


class TestDesiredState:
    """The intent column and the up-convergence branch it enables.

    Spec: docs/superpowers/specs/2026-08-27-session-desired-state-design.md
    """

    class _Starter:
        """Stand-in for SessionLens — records the addresses it was asked for."""

        def __init__(self, result=True):
            self.calls: list[tuple[str, str | None]] = []
            self.result = result

        async def ensure_started(self, *, kind, target_id, project_id):
            self.calls.append((target_id, project_id))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    async def _named(self, db, provider, **kw):
        kw.setdefault("name", "n-supervisor--p1")
        kw.setdefault("lifecycle", "named")
        kw.setdefault("task_id", None)
        return await _session(db, provider, sid="n1", **kw)

    async def _idle_profile(self, db, monkeypatch, timeout=600):
        class _P:
            idle_timeout = timeout

        async def _get_profile(_pid):
            return _P()

        monkeypatch.setattr(db, "get_profile", _get_profile)

    async def test_named_session_recovers_owned_submit_without_a_task(
        self, db, provider, reconciler, monkeypatch
    ):
        from unittest.mock import AsyncMock

        await self._named(db, provider, started_at=NOW - 1000, last_activity=NOW - 1000)
        provider.sessions["n-supervisor--p1"].activity = NOW - 1000
        await self._idle_profile(db, monkeypatch, timeout=600)
        provider.resubmit_pending = AsyncMock(return_value=True)
        await reconciler.tick(now=NOW)
        provider.resubmit_pending.assert_awaited_once()
        assert provider.resubmit_pending.await_args.args[0].name == "n-supervisor--p1"
        assert (await db.get_session("n1")).state == "running"
        assert (await db.get_session("n1")).last_activity == NOW

    async def test_drain_writes_intent_as_well_as_state(
        self, db, provider, reconciler, monkeypatch
    ):
        """The half that stops the flap: a drained session stops being wanted."""
        await self._named(db, provider, started_at=NOW - 5000, last_activity=NOW - 5000)
        provider.sessions["n-supervisor--p1"].activity = NOW - 5000
        await self._idle_profile(db, monkeypatch)
        await reconciler.tick(now=NOW)
        row = await db.get_session("n1")
        assert row.state == "sleeping" and row.desired_state == "sleeping"

    async def test_wanted_but_not_live_is_started(self, db, provider, reconciler, bus):
        starter = self._Starter()
        reconciler.starter = starter
        await self._named(
            db,
            provider,
            state="sleeping",
            desired_state="running",
            started_at=NOW - 5000,
            last_activity=NOW - 5000,
        )
        await reconciler.tick(now=NOW)
        assert starter.calls == [("supervisor-p1", "p1")]
        assert "session.started" in bus.types()
        # Intent retired: the lens inserted a *new* row for it, so leaving
        # this one wanted would start a second session next tick.
        assert (await db.get_session("n1")).desired_state == "stopped"

    async def test_sleeping_and_wanted_sleeping_is_left_alone(
        self, db, provider, reconciler
    ):
        starter = self._Starter()
        reconciler.starter = starter
        await self._named(
            db,
            provider,
            state="sleeping",
            desired_state="sleeping",
            started_at=NOW - 5000,
            last_activity=NOW - 5000,
        )
        await reconciler.tick(now=NOW)
        assert starter.calls == []

    async def test_no_starter_means_no_up_convergence(self, db, provider, reconciler):
        """Every pre-existing caller constructs a reconciler without one."""
        await self._named(
            db,
            provider,
            state="sleeping",
            desired_state="running",
            started_at=NOW - 5000,
            last_activity=NOW - 5000,
        )
        await reconciler.tick(now=NOW)  # must not raise
        assert (await db.get_session("n1")).desired_state == "running"

    async def test_task_sessions_are_not_woken(self, db, provider, reconciler):
        """A task session is started by the task lifecycle, never by intent."""
        starter = self._Starter()
        reconciler.starter = starter
        await _task(db)
        await _session(
            db,
            provider,
            sid="s1",
            state="sleeping",
            desired_state="running",
            started_at=NOW - 5000,
            last_activity=NOW - 5000,
        )
        await reconciler.tick(now=NOW)
        assert starter.calls == []

    @pytest.mark.parametrize("initial_state", ["sleeping", "stopped"])
    async def test_repeated_start_failure_quarantines(
        self, db, provider, reconciler, config, bus, initial_state
    ):
        """A misconfigured supervisor must not cost an attempt every tick."""
        reconciler.starter = self._Starter(result=RuntimeError("no harness"))
        await self._named(
            db,
            provider,
            state=initial_state,
            desired_state="running",
            started_at=NOW - 5000,
            last_activity=NOW - 5000,
        )
        now = NOW
        for _ in range(config.sessions.max_restarts + 1):
            await reconciler.tick(now=now)
            now += config.sessions.restart_backoff_seconds * 10
        row = await db.get_session("n1")
        expected_state = "stopped" if initial_state == "stopped" else "quarantined"
        assert row.state == expected_state and row.sleep_reason == "start_failed"
        assert row.desired_state == "stopped"
        assert row.quarantined_at is not None
        assert bus.types().count("session.quarantined") == 1
        restarts = row.restarts
        await reconciler.tick(now=now + config.sessions.restart_backoff_seconds * 10)
        assert (await db.get_session("n1")).restarts == restarts

    async def test_backoff_holds_between_attempts(self, db, provider, reconciler, config):
        starter = self._Starter(result=False)  # declines, so intent stands
        reconciler.starter = starter
        await self._named(
            db,
            provider,
            state="sleeping",
            desired_state="running",
            started_at=NOW - 5000,
            last_activity=NOW - 5000,
        )
        await reconciler.tick(now=NOW)
        assert len(starter.calls) == 1
        await reconciler.tick(now=NOW + 1)  # same tick window — no second attempt
        assert len(starter.calls) == 1
        await reconciler.tick(now=NOW + config.sessions.restart_backoff_seconds * 5)
        assert len(starter.calls) == 2

    async def test_terminal_verdicts_leave_intent_stopped(self, db, provider, reconciler):
        """Nothing resurrects a session that finished its work."""
        await _task(db, status=TaskStatus.COMPLETED)
        row = await _session(db, provider)
        await provider.set_meta(reconciler._handle(row), DRAIN_ACK_KEY, "1")
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).desired_state == "stopped"


class TestBackstop:
    async def test_stuck_timeout_force_kills_and_blocks(
        self, db, provider, reconciler, bus, config
    ):
        config.sessions.lease_ttl_seconds = 0  # isolate the backstop from the ladder
        config.agents_config.stuck_timeout_seconds = 3600
        await _task(db)
        await _session(db, provider, started_at=NOW - 7200, last_activity=NOW)
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "stopped"
        task = await db.get_task("t1")
        assert task.status is TaskStatus.BLOCKED
        assert await db.get_task_meta("t1", "needs_attention") == "stuck_timeout"
        assert "task.quarantined" in bus.types()
        failed = bus.payload("task.failed")
        assert failed is not None, bus.types()
        assert failed["status"] == TaskStatus.BLOCKED.value
        assert failed["context"] == "stuck_timeout"

    async def test_backstop_block_survives_next_promotion_cycle(
        self, db, provider, reconciler, config, tmp_path
    ):
        from src.orchestrator import Orchestrator

        config.data_dir = str(tmp_path / "data")
        config.workspace_dir = str(tmp_path / "workspaces")
        config.database = DatabaseConfig(url=lease_dsn("unused.db"))
        config.sessions.lease_ttl_seconds = 0
        config.agents_config.stuck_timeout_seconds = 3600
        await _task(db)
        await db.create_task(Task(
            id="completed-dependency", project_id="p1", title="Done", description="Done",
            status=TaskStatus.COMPLETED,
        ))
        await db.add_dependency("t1", "completed-dependency")
        await _session(db, provider, started_at=NOW - 7200, last_activity=NOW)
        await reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.BLOCKED

        # Exercise the next real cascade, not just the backstop in isolation.
        orch = Orchestrator(config)
        orch.db = db
        await orch._check_defined_tasks()
        assert (await db.get_task("t1")).status is TaskStatus.BLOCKED
        assert (await db.get_session("s1")).state == "stopped"
        assert await db.get_task_meta("t1", "needs_attention") == "stuck_timeout"

    async def test_backstop_off_when_the_timeout_is_zero(self, db, provider, reconciler):
        await _task(db)
        await _session(db, provider, started_at=NOW - 999_999)
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "running"


class TestTickRobustness:
    async def test_disabled_flag_makes_tick_a_no_op(self, db, provider, reconciler, config):
        config.sessions.enabled = False
        await _task(db)
        await _session(db, provider, started_at=NOW - 999_999)
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "running"

    async def test_a_failing_step_does_not_abort_the_rest(
        self, db, provider, reconciler, monkeypatch
    ):
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 10)
        provider.script_death(row.name)

        async def _boom(*_a, **_k):
            raise RuntimeError("drain-ack step exploded")

        monkeypatch.setattr(reconciler, "_step_drain_ack", _boom)
        await reconciler.tick(now=NOW)
        # The exit step still ran.
        assert (await db.get_session("s1")).restarts == 1

    async def test_tick_never_raises_even_with_a_broken_database(self, reconciler, monkeypatch):
        async def _boom(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(reconciler.db, "list_sessions", _boom)
        await reconciler.tick(now=NOW)  # must not raise

    async def test_activity_observed_from_the_provider_is_persisted(
        self, db, provider, reconciler
    ):
        await _task(db)
        row = await _session(db, provider, last_activity=NOW - 5000)
        provider.feed_output(row.name, "working...")
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).last_activity > NOW - 5000

    async def test_unknown_provider_leaves_the_row_untouched(self, db, provider, reconciler):
        await _task(db)
        await _session(db, provider)
        await db.update_session("s1", provider="ghost")

        def _raise(name, config=None):
            raise ValueError(f"Unknown session provider: {name!r}")

        reconciler.providers.create = _raise
        await reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "running"


class TestNudgelessProvider:
    """A provider with no input channel skips the ladder's nudge rungs.

    Talking to a session that has no stdin is not a degraded nudge, it is
    no nudge — so burning three backoff cycles on it just delays the
    restart that was always going to be the real remedy (design §5.2).
    """

    @pytest.fixture
    def mute_provider(self):
        from src.sessions.provider import Cap

        class _Mute(FakeProvider):
            capabilities = frozenset({Cap.PEEK, Cap.ACTIVITY})

        return _Mute()

    @pytest.fixture
    def mute_reconciler(self, db, config, mute_provider, bus):
        class _Reg(SessionProviderRegistry):
            def create(self, name, config=None):
                return mute_provider

        return SessionReconciler(
            db, config, _Reg({"fake": FakeProvider}), bus=bus, epoch="epoch-new"
        )

    async def test_stall_goes_straight_to_restart(
        self, db, mute_provider, mute_reconciler, bus
    ):
        await _task(db)
        row = await _session(
            db, mute_provider, started_at=NOW - 5000, last_activity=NOW - 1000
        )
        mute_provider.sessions[row.name].activity = NOW - 1000
        await mute_reconciler.tick(now=NOW)
        assert mute_provider.sent_nudges == []
        session = await db.get_session("s1")
        assert session.state == "stopped" and session.restarts == 1
        assert "task.restarted" in bus.types()
        assert "task.nudged" not in bus.types()


# ---------------------------------------------------------------------------
# B1 — every terminal path releases the agent and the workspace lock
# ---------------------------------------------------------------------------


class TestTerminalPathsReleaseResources:
    """Regression cover for B1.

    Before the fix, none of RATE_LIMIT / RAPID_CRASH / PRODUCTIVE_DEATH /
    quarantine / backstop freed the agent or the workspace.  The legacy
    runtime always did, so this was a regression rather than missing
    parity — and ``AgentReconciler``'s orphan sweep does not cover it,
    because it only resets a BUSY agent whose *task row is gone* and a
    PAUSED/BLOCKED task still has one.  Every assertion here reads the
    database back.
    """

    async def _assert_freed(self, db):
        agent = await db.get_agent("a1")
        assert agent.state is AgentState.IDLE, "agent left BUSY — B1"
        ws = await db.get_workspace("ws1")
        assert ws.locked_by_task_id is None, "workspace left locked — B1"
        assert ws.locked_by_agent_id is None
        assert await db.get_workspace_for_task("t1") is None

    async def test_rate_limit_frees_the_agent_and_the_lock(
        self, db, provider, releasing_reconciler, tmp_path
    ):
        await _task(db)
        await _busy_agent_and_workspace(db, tmp_path)
        row = await _session(db, provider, started_at=NOW - 100)
        provider.feed_output(row.name, "Claude usage limit reached", activity=False)
        provider.script_death(row.name)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        await self._assert_freed(db)

    async def test_rapid_crash_frees_the_agent_and_the_lock(
        self, db, provider, releasing_reconciler, tmp_path
    ):
        """The one that bites hardest: the re-queued task needs the lock back.

        Without the release the task goes PAUSED → READY, gets a fresh
        agent, and then cannot acquire a workspace because its own dead
        predecessor still holds the lock — pause/retry until restart.
        """
        await _task(db)
        await _busy_agent_and_workspace(db, tmp_path)
        row = await _session(db, provider, started_at=NOW - 10)
        provider.script_death(row.name)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        assert (await db.get_session("s1")).restarts == 1
        await self._assert_freed(db)

    async def test_productive_death_frees_the_agent_and_the_lock(
        self, db, provider, releasing_reconciler, tmp_path
    ):
        await _task(db)
        await _busy_agent_and_workspace(db, tmp_path)
        row = await _session(db, provider, started_at=NOW - 5000)
        provider.script_death(row.name)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        await self._assert_freed(db)

    async def test_quarantine_frees_the_agent_and_the_lock(
        self, db, provider, releasing_reconciler, tmp_path, config
    ):
        await _task(db)
        await _busy_agent_and_workspace(db, tmp_path)
        row = await _session(
            db, provider, started_at=NOW - 10, restarts=config.sessions.max_restarts - 1
        )
        provider.script_death(row.name)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "quarantined"
        assert (await db.get_task("t1")).status is TaskStatus.BLOCKED
        await self._assert_freed(db)

    async def test_backstop_frees_the_agent_and_the_lock(
        self, db, provider, releasing_reconciler, tmp_path, config
    ):
        config.sessions.lease_ttl_seconds = 0  # isolate the backstop
        config.agents_config.stuck_timeout_seconds = 3600
        await _task(db)
        await _busy_agent_and_workspace(db, tmp_path)
        await _session(db, provider, started_at=NOW - 7200, last_activity=NOW)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.BLOCKED
        await self._assert_freed(db)

    async def test_stall_restart_frees_the_agent_and_the_lock(
        self, db, provider, releasing_reconciler, tmp_path, config
    ):
        await _task(db)
        await _busy_agent_and_workspace(db, tmp_path)
        row = await _session(
            db, provider, started_at=NOW - 5000, last_activity=NOW - 1000
        )
        provider.sessions[row.name].activity = NOW - 1000
        await db.set_task_meta("t1", META_STALL_NUDGES, str(config.sessions.stall_max_nudges))
        await db.set_task_meta("t1", META_STALL_LAST_ACTION, "0")
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        await self._assert_freed(db)

    async def test_a_drained_session_does_not_double_release(
        self, db, provider, releasing_reconciler, tmp_path
    ):
        """DRAINED is the happy path; ``complete_session_task`` already ran."""
        await _task(db, status=TaskStatus.COMPLETED)
        row = await _session(db, provider)
        provider.script_death(row.name)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "stopped"
        assert releasing_reconciler.test_orch.calls == []


# ---------------------------------------------------------------------------
# B3 — session live, task no longer open (and its mirror)
# ---------------------------------------------------------------------------


class TestOrphanStep:
    """Design §4.1 promised this row and nothing implemented it.

    ``Verdict.DRAINED`` is only reachable from ``_step_exits``, which needs
    a **dead** process.  A live session whose task has closed had no path
    at all until ``_step_orphans``.
    """

    async def test_live_session_with_a_closed_task_is_drained(
        self, db, provider, releasing_reconciler, bus
    ):
        """The agent closed the task and never acked — B3(a)."""
        await _task(db, status=TaskStatus.COMPLETED)
        await _session(db, provider)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "stopped"
        assert await provider.list_running("s-") == []

    @pytest.mark.parametrize(
        "status", [TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.PAUSED]
    )
    async def test_live_session_with_a_task_in_any_closed_state_is_drained(
        self, db, provider, releasing_reconciler, status
    ):
        """"Closed" is every status outside (IN_PROGRESS, ASSIGNED).

        ``stop_task`` leaves a BLOCKED task with the session still running;
        the rate-limit verdict leaves a PAUSED one.  Both are a live agent
        in a workspace the daemon has already written off.

        (A task row that is *gone* is not reachable: ``sessions.task_id``
        carries a foreign key, so the row keeps the task alive.  The
        reconciler still handles ``task is None`` — cheap, and the FK is
        not this module's to rely on.)
        """
        await _task(db, status=status)
        await _session(db, provider)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "stopped"
        assert await provider.list_running("s-") == []

    async def test_a_task_that_is_still_open_is_left_alone(
        self, db, provider, releasing_reconciler
    ):
        await _task(db)
        await releasing_reconciler.tick(now=NOW)
        await _session(db, provider)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_session("s1")).state == "running"
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS

    async def test_named_sessions_have_no_task_to_disagree_with(
        self, db, provider, releasing_reconciler
    ):
        await _session(
            db,
            provider,
            sid="n1",
            task_id=None,
            name="n-supervisor",
            lifecycle="named",
        )
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_session("n1")).state == "running"

    async def test_open_task_with_a_non_live_row_is_released(
        self, db, provider, releasing_reconciler, bus, tmp_path
    ):
        """B3's mirror: nothing else looks at this.

        Every other step iterates the live set, so a task whose row went
        non-live without a verdict — ``stop_task``'s path, or a crash
        between the two writes — would hold its agent and its workspace
        until the daemon restarted.
        """
        await _task(db)
        await _busy_agent_and_workspace(db, tmp_path)
        await _session(db, provider)
        await db.update_session("s1", state="stopped")
        await releasing_reconciler.tick(now=NOW)
        task = await db.get_task("t1")
        assert task.status is TaskStatus.BLOCKED
        assert await db.get_task_meta("t1", "needs_attention") == "session_not_live"
        assert (await db.get_agent("a1")).state is AgentState.IDLE
        assert (await db.get_workspace("ws1")).locked_by_task_id is None
        failed = bus.payload("task.failed")
        assert failed is not None, bus.types()
        assert failed["status"] == TaskStatus.BLOCKED.value
        assert failed["context"] == "session_not_live"

    async def test_a_relaunch_in_flight_is_not_mistaken_for_a_stranded_task(
        self, db, provider, releasing_reconciler, tmp_path
    ):
        """``_execute_task`` goes IN_PROGRESS long before the row exists.

        Workspace preparation sits between them and can be a git clone
        taking minutes, so a retry spends that whole window looking exactly
        like "IN_PROGRESS with a stopped row" — and blocking it there would
        kill every relaunch.
        """
        await _task(db)
        await _busy_agent_and_workspace(db, tmp_path)
        await _session(db, provider)
        await db.update_session("s1", state="stopped")
        releasing_reconciler.test_orch._running_tasks["t1"] = object()

        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert (await db.get_agent("a1")).state is AgentState.BUSY

        # Once the launch finishes without producing a live row, it is fair game.
        releasing_reconciler.test_orch._running_tasks.clear()
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.BLOCKED

    async def test_a_task_that_never_had_a_session_is_not_touched(
        self, db, provider, releasing_reconciler, tmp_path
    ):
        """Legacy-runtime tasks are not the session reconciler's business."""
        await _task(db)
        await _busy_agent_and_workspace(db, tmp_path)
        await releasing_reconciler.tick(now=NOW)
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert (await db.get_agent("a1")).state is AgentState.BUSY


# ---------------------------------------------------------------------------
# H2 — restart-with-resume actually carries a key
# ---------------------------------------------------------------------------


class TestResumeKey:
    async def test_legacy_resume_placeholder_is_not_carried(
        self, db, provider, releasing_reconciler
    ):
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 10)
        from dataclasses import replace
        row = replace(row, harness="codex", session_key=row.id)
        await releasing_reconciler._carry_resume_key(row, await db.get_task("t1"))
        assert await db.get_task_meta("t1", "session_resume_key") is None

    async def test_a_crash_hands_the_conversation_id_to_the_task(
        self, db, provider, releasing_reconciler
    ):
        """Ladder rung 3 and the RAPID_CRASH restart both promise ``--resume``.

        ``session_resume_key`` was only ever *read*; nothing wrote it, so
        every restart started a fresh conversation and lost all context.
        """
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 10, session_key="conv-abc")
        provider.script_death(row.name)
        await releasing_reconciler.tick(now=NOW)
        assert await db.get_task_meta("t1", "session_resume_key") == "conv-abc"

    async def test_a_stall_restart_hands_it_over_too(
        self, db, provider, releasing_reconciler, config
    ):
        await _task(db)
        row = await _session(
            db,
            provider,
            started_at=NOW - 5000,
            last_activity=NOW - 1000,
            session_key="conv-xyz",
        )
        provider.sessions[row.name].activity = NOW - 1000
        await db.set_task_meta("t1", META_STALL_NUDGES, str(config.sessions.stall_max_nudges))
        await db.set_task_meta("t1", META_STALL_LAST_ACTION, "0")
        await releasing_reconciler.tick(now=NOW)
        assert await db.get_task_meta("t1", "session_resume_key") == "conv-xyz"


# ---------------------------------------------------------------------------
# Smaller findings
# ---------------------------------------------------------------------------


class TestForensicsAndBudgets:
    async def test_a_backstop_kill_does_not_clobber_sleep_reason(
        self, db, provider, reconciler, config
    ):
        """``_stop_session`` used to re-send the stale in-memory value.

        A backstop firing on a RATE_LIMIT-slept session overwrote
        ``sleep_reason="rate_limit"`` with ``None`` — destroying the one
        field that recorded why the session is not running.
        """
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 10)
        await db.update_session("s1", sleep_reason="rate_limit")
        # Stop with a row object that still carries the pre-update value.
        await reconciler._stop_session(provider, row, reason="stuck_timeout")
        session = await db.get_session("s1")
        assert session.state == "stopped"
        assert session.sleep_reason == "rate_limit"

    async def test_both_ladders_agree_on_the_restart_budget(
        self, db, provider, reconciler, config
    ):
        """Quarantine threshold was ``>=`` in one branch and ``>`` in the other.

        Three ladders spend the budget now: rapid-crash, stall-restart, and
        named up-convergence.  All three must agree on what the cap means.
        """
        import inspect

        src = inspect.getsource(type(reconciler))
        assert "count > self.sessions_config.max_restarts" not in src
        assert src.count("count >= self.sessions_config.max_restarts") == 3


class TestCreateSessionLiveGuard:
    async def test_a_second_live_row_for_one_name_is_refused(
        self, db, provider
    ):
        """"At most one live row per name" was documented but not enforced."""
        await _task(db)
        await _session(db, provider, sid="s1")
        with pytest.raises(ValueError, match="already has a live row"):
            await db.create_session(
                SessionRecord(
                    id="s2",
                    task_id="t1",
                    project_id="p1",
                    profile_id="claude-opus",
                    harness="claude",
                    provider="fake",
                    name="s-t1",
                    lifecycle="task",
                    state="running",
                    work_dir="/wd",
                    epoch="epoch-new",
                    instance_token="tok-2",
                    started_at=NOW,
                )
            )

    async def test_a_stopped_predecessor_does_not_block_a_restart(
        self, db, provider
    ):
        """The restart-with-resume path reuses the name deliberately."""
        await _task(db)
        await _session(db, provider, sid="s1")
        await db.update_session("s1", state="stopped")
        await db.create_session(
            SessionRecord(
                id="s2",
                task_id="t1",
                project_id="p1",
                profile_id="claude-opus",
                harness="claude",
                provider="fake",
                name="s-t1",
                lifecycle="task",
                state="running",
                work_dir="/wd",
                epoch="epoch-new",
                instance_token="tok-2",
                started_at=NOW,
            )
        )
        assert (await db.get_session_by_name("s-t1")).id == "s2"



async def test_adoption_links_current_worker_without_guessing_launch_settings(
    db, provider, reconciler
):
    await _task(db)
    await db.create_agent(Agent(id="a1", name="Alice", profile_id="old",
                               state=AgentState.BUSY, current_task_id="t1",
                               model="new-next-session-model"))
    await db.update_task("t1", assigned_agent_id="a1")
    await _session(db, provider)
    await reconciler.adopt_on_start()
    adopted = await db.get_session("s1")
    assert adopted.agent_id == "a1"
    assert adopted.project_id == "p1"
    assert adopted.instance_token == "tok-s1"
    assert adopted.model is None and adopted.llm_provider is None
    assert adopted.epoch == "epoch-new"


async def test_adopted_pool_claim_remains_protected_from_restart_recovery(db, provider, reconciler):
    await _task(db)
    await db.create_agent(Agent(id="a1", name="Alice", profile_id="worker",
                               state=AgentState.BUSY, current_task_id="t1"))
    await db.update_task("t1", assigned_agent_id="a1")
    await _session(db, provider, name="p-worker--p1--test", lifecycle="pool",
                   agent_id="a1", claim_phase="active", last_claim_epoch=1)
    report = await reconciler.adopt_on_start()
    assert "s1" in report.adopted
    assert await reconciler.adopted_task_ids(report) == {"t1"}


# ---------------------------------------------------------------------------
# Provider availability (docs/specs/provider-failover.md D2, D13)
# ---------------------------------------------------------------------------


class _Availability:
    """The slice of ``ProviderAvailabilityService`` the reconciler reads."""

    def __init__(self, *, unavailable=()):
        self.unavailable = set(unavailable)
        self.rate_limit_exits: list[str] = []
        self.rate_limit_reasons: list[str] = []

    def provider_for_harness(self, harness, project_id=None):
        return harness

    def is_unavailable(self, provider, now=None):
        return provider in self.unavailable

    async def record_rate_limit_exit(self, session, *, reason=""):
        self.rate_limit_exits.append(session.id)
        self.rate_limit_reasons.append(reason)


class TestProviderAvailability:
    async def test_a_rate_limit_exit_is_recorded_as_provider_evidence(
        self, db, provider, config, registry, bus
    ):
        orch = _ReleasingOrch(db)
        orch.provider_availability = _Availability()
        rec = SessionReconciler(db, config, registry, bus=bus, orchestrator=orch, epoch="e")
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 100)
        provider.feed_output(row.name, "usage limit reached", activity=False)
        provider.script_death(row.name)
        await rec.tick(now=NOW)
        assert orch.provider_availability.rate_limit_exits == ["s1"]

    async def test_a_rapid_crash_on_an_unavailable_provider_spends_no_restart(
        self, db, provider, config, registry, bus
    ):
        orch = _ReleasingOrch(db)
        orch.provider_availability = _Availability(unavailable={"claude"})
        rec = SessionReconciler(db, config, registry, bus=bus, orchestrator=orch, epoch="e")
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 10, restarts=2)
        provider.script_death(row.name)
        await rec.tick(now=NOW)
        session = await db.get_session("s1")
        # Not quarantined, restart counter untouched: the provider's fault.
        assert session.state == "stopped" and session.restarts == 2
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        assert "task.restarted" not in bus.types()

    async def test_a_rapid_crash_on_a_healthy_provider_still_spends_the_ladder(
        self, db, provider, config, registry, bus
    ):
        orch = _ReleasingOrch(db)
        orch.provider_availability = _Availability()
        rec = SessionReconciler(db, config, registry, bus=bus, orchestrator=orch, epoch="e")
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 10)
        provider.script_death(row.name)
        await rec.tick(now=NOW)
        assert (await db.get_session("s1")).restarts == 1

    async def test_a_pool_rapid_crash_on_an_unavailable_provider_arms_no_key_quarantine(
        self, db, provider, pool_reconciler, tmp_path
    ):
        pool_reconciler.test_orch.provider_availability = _Availability(unavailable={"claude"})
        row = await _claimed_pool_session(db, provider, tmp_path, started_at=NOW - 10)
        provider.script_death(row.name)
        await pool_reconciler.tick(now=NOW)
        assert pool_reconciler.test_orch._pool_quarantine == {}

    async def test_a_pool_rapid_crash_on_a_healthy_provider_still_quarantines_the_key(
        self, db, provider, pool_reconciler, tmp_path
    ):
        pool_reconciler.test_orch.provider_availability = _Availability()
        row = await _claimed_pool_session(db, provider, tmp_path, started_at=NOW - 10)
        provider.script_death(row.name)
        await pool_reconciler.tick(now=NOW)
        assert pool_reconciler.test_orch._pool_quarantine


# ---------------------------------------------------------------------------
# Usage-limit screen on a stalled session (bold-rapids.8, provider-failover D13)
# ---------------------------------------------------------------------------

#: The bottom of a real Claude Code 2.1.278 pane parked on its limit (see
#: tests/test_usage_limit_screen.py for the capture).
LIMIT_PANE = (
    "  ⎿  You've hit your session limit · resets 1:40am (America/Los_Angeles)\n"
    "     /usage-credits to finish what you’re working on.\n"
    "\n"
    "❯ "
)


class TestUsageLimitScreen:
    """A CLI parked on its limit screen is a RATE_LIMIT exit, not a stall.

    It cannot answer a nudge, so the ladder's nudges and restart only burn
    ~23 minutes and a restart before the task gets anywhere.  In
    ``provider_failover.mode: enforce`` the ladder recognises the screen,
    stops the process and hands the task to the exit path's RATE_LIMIT
    verdict — provider evidence, no restart spent.
    """

    async def _parked(self, db, provider, *, text=LIMIT_PANE, idle=1000.0):
        await _task(db)
        row = await _session(db, provider, started_at=NOW - 5000, last_activity=NOW - idle)
        provider.sessions[row.name].activity = NOW - idle
        provider.feed_output(row.name, text, activity=False)
        return row

    @staticmethod
    def _reconciler(db, config, registry, bus):
        orch = _ReleasingOrch(db)
        orch.provider_availability = _Availability()
        rec = SessionReconciler(db, config, registry, bus=bus, orchestrator=orch, epoch="e")
        return rec, orch

    async def test_a_limit_screen_is_a_rate_limit_exit_not_a_nudge(
        self, db, provider, config, registry, bus
    ):
        rec, orch = self._reconciler(db, config, registry, bus)
        row = await self._parked(db, provider)

        await rec.tick(now=NOW)

        assert provider.sent_nudges == []
        assert row.name not in provider.sessions  # the parked CLI is stopped
        session = await db.get_session("s1")
        assert (session.state, session.sleep_reason) == ("sleeping", "rate_limit")
        assert session.restarts == 0
        assert (await db.get_task("t1")).status is TaskStatus.PAUSED
        exited = bus.payload("session.exited")
        assert exited["verdict"] == "rate_limit"
        assert exited["reason"].startswith("usage-limit screen on a stalled session")
        assert "resets 1:40am" in exited["reason"]
        assert orch.provider_availability.rate_limit_exits == ["s1"]
        assert "task.stalled" not in bus.types() and "task.nudged" not in bus.types()
        assert orch.calls == ["t1"]  # agent and workspace released

    async def test_a_limit_screen_reached_after_the_nudges_spends_no_restart(
        self, db, provider, config, registry, bus
    ):
        rec, _orch = self._reconciler(db, config, registry, bus)
        await self._parked(db, provider)
        await db.set_task_meta("t1", META_STALL_NUDGES, str(config.sessions.stall_max_nudges))
        await db.set_task_meta("t1", META_STALL_LAST_ACTION, "0")

        await rec.tick(now=NOW)

        session = await db.get_session("s1")
        assert (session.state, session.restarts) == ("sleeping", 0)
        assert "task.restarted" not in bus.types()
        # The next session on this task starts a fresh ladder.
        assert await db.get_task_meta("t1", META_STALL_NUDGES) == "0"

    @pytest.mark.parametrize("mode", ["observe", "off"])
    async def test_outside_enforce_the_ladder_is_unchanged(
        self, db, provider, config, registry, bus, mode
    ):
        config.provider_failover.mode = mode
        rec, orch = self._reconciler(db, config, registry, bus)
        row = await self._parked(db, provider)

        await rec.tick(now=NOW)

        assert provider.sent_nudges
        assert row.name in provider.sessions
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert orch.provider_availability.rate_limit_exits == []

    async def test_broad_rate_limit_text_is_still_just_a_stall(
        self, db, provider, config, registry, bus
    ):
        """``429`` / ``rate limit`` are exit-classifier evidence, not a limit screen."""
        rec, orch = self._reconciler(db, config, registry, bus)
        row = await self._parked(
            db, provider, text="  ⎿  HTTP 429 Too Many Requests: rate limit, retrying\n❯ "
        )

        await rec.tick(now=NOW)

        assert provider.sent_nudges
        assert row.name in provider.sessions
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert orch.provider_availability.rate_limit_exits == []

    async def test_a_limit_screen_inside_the_lease_is_left_alone(
        self, db, provider, config, registry, bus
    ):
        rec, orch = self._reconciler(db, config, registry, bus)
        row = await self._parked(db, provider, idle=10.0)

        await rec.tick(now=NOW)

        assert row.name in provider.sessions
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert orch.provider_availability.rate_limit_exits == []

    async def test_a_failed_stop_falls_back_to_the_ladder(
        self, db, provider, config, registry, bus, monkeypatch
    ):
        """Nothing is released while the process may still be alive."""

        async def _stop_fails(h, *, grace=2.0):
            raise RuntimeError("tmux server unreachable")

        monkeypatch.setattr(provider, "stop", _stop_fails)
        rec, orch = self._reconciler(db, config, registry, bus)
        row = await self._parked(db, provider)

        await rec.tick(now=NOW)

        assert provider.sent_nudges
        assert (await db.get_session(row.id)).state == "running"
        assert (await db.get_task("t1")).status is TaskStatus.IN_PROGRESS
        assert orch.provider_availability.rate_limit_exits == []
        assert orch.calls == []

    async def test_a_parked_pool_worker_releases_its_claim_as_a_rate_limit(
        self, db, provider, pool_reconciler, tmp_path
    ):
        pool_reconciler.test_orch.provider_availability = _Availability()
        row = await _claimed_pool_session(db, provider, tmp_path, last_activity=NOW - 1000)
        provider.sessions[row.name].activity = NOW - 1000
        provider.feed_output(row.name, LIMIT_PANE, activity=False)

        await pool_reconciler._step_stall_ladder([row], NOW)

        assert provider.sent_nudges == []
        assert pool_reconciler.test_orch.terminations == [(row.id, "rate_limit")]
        current = await db.get_session(row.id)
        assert current.state == "stopped" and current.restarts == 0
        task = await db.get_task("t1")
        assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)
        assert pool_reconciler.test_orch.provider_availability.rate_limit_exits == [row.id]


# ---------------------------------------------------------------------------
# Usage-limit screen on an idle pool worker (azure-ridge, provider-failover D2)
# ---------------------------------------------------------------------------

CODEX_LIMIT_LINE = (
    "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
    "to purchase more credits or try again at Sep 23rd, 2026 3:44 PM."
)
CLAUDE_LIMIT_LINE = "You've hit your session limit · resets 1:40am (America/Los_Angeles)"

#: A Codex pool worker whose bootstrap prompt was answered with the limit: the
#: pane the operator pasted on 2026-09-21 (vault session 264468f1).
CODEX_IDLE_POOL_PANE = "\n".join(
    [
        "› You are a pool worker for project agent-queue (profile standard-high-codex).",
        "  Loop: run `aq task claim --next --wait 60`. On `claimed`, run `aq prime`, do the",
        "",
        f"■ {CODEX_LIMIT_LINE}",
        "",
        "› Ask Codex to do anything",
        "",
        "  gpt-6-astra xhigh · ~/dev/agent-queue2/.aq/worktrees/slot-0",
    ]
)

#: The same shape on Claude Code: the bootstrap prompt, then the limit.
CLAUDE_IDLE_POOL_PANE = "\n".join(
    [
        "❯ You are a pool worker for project agent-queue (profile standard-high-claude).",
        "  Loop: run `aq task claim --next --wait 60`. On `claimed`, run `aq prime`, do the",
        f"  ⎿  {CLAUDE_LIMIT_LINE}",
        "     /usage-credits to finish what you’re working on.",
        "",
        "❯ ",
    ]
)


class TestIdlePoolWorkerOnUsageLimitScreen:
    """A pool worker parked on its usage limit before it ever claimed.

    Its bootstrap prompt is answered with the limit screen, so it never
    reaches its claim loop and holds no task: the stall ladder (which
    recognises the screen on a session holding a task) never sees it, and the
    abandoned-claim-loop step recycles it.  That recycle is the only place the
    limit is observed, so it records the rate-limit exit — two such workers
    trip the provider — instead of letting pool sizing relaunch into the same
    limit with no evidence at all.
    """

    async def _parked(
        self, db, provider, pool_reconciler, *, text, sid="pool-limited", idle=1_000.0,
        availability=True,
    ):
        if availability:
            pool_reconciler.test_orch.provider_availability = _Availability()
        row = await _session(
            db, provider, sid=sid, task_id=None, name=f"p-{sid}", lifecycle="pool",
            last_activity=NOW - idle,
        )
        provider.sessions[row.name].activity = NOW - idle
        provider.feed_output(row.name, text, activity=False)
        return row

    @pytest.mark.parametrize("mode", ["enforce", "observe"])
    @pytest.mark.parametrize(
        ("pane", "line"),
        [(CODEX_IDLE_POOL_PANE, CODEX_LIMIT_LINE), (CLAUDE_IDLE_POOL_PANE, CLAUDE_LIMIT_LINE)],
        ids=["codex", "claude"],
    )
    async def test_a_limit_screen_is_recorded_as_a_rate_limit_exit(
        self, db, provider, pool_reconciler, mode, pane, line
    ):
        pool_reconciler.config.provider_failover.mode = mode
        row = await self._parked(db, provider, pool_reconciler, text=pane)

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        availability = pool_reconciler.test_orch.provider_availability
        assert availability.rate_limit_exits == [row.id]
        assert availability.rate_limit_reasons == [
            f"usage-limit screen on an idle pool worker: {line}"
        ]
        # Recycled all the same -- no task is held, so there is nothing to
        # checkpoint or requeue -- under a reason that says why.
        assert pool_reconciler.test_orch.terminations == [(row.id, "usage_limit_screen")]
        current = await db.get_session(row.id)
        assert (current.state, current.end_reason) == ("stopped", "usage_limit_screen")
        assert row.name not in provider.sessions

    async def test_the_orchestrator_tick_records_it(self, db, provider, pool_reconciler):
        row = await self._parked(db, provider, pool_reconciler, text=CODEX_IDLE_POOL_PANE)

        await pool_reconciler.tick(now=NOW)

        assert pool_reconciler.test_orch.provider_availability.rate_limit_exits == [row.id]
        assert pool_reconciler.test_orch.terminations == [(row.id, "usage_limit_screen")]

    async def test_two_parked_workers_trip_the_provider(self, db, provider, pool_reconciler):
        """On the real availability service: one recycle degrades, two sessions trip (D3)."""
        harnesses = HarnessRegistry()
        harnesses.upsert(Harness(id="claude", name="claude", command="claude"))
        service = ProviderAvailabilityService(
            db=db,
            config_getter=lambda: pool_reconciler.config,
            harness_registry=harnesses,
            clock=lambda: NOW,
        )
        pool_reconciler.test_orch.provider_availability = service

        first = await self._parked(
            db, provider, pool_reconciler, text=CLAUDE_IDLE_POOL_PANE, sid="pool-a",
            availability=False,
        )
        await pool_reconciler._step_abandoned_pool_claim_loop([first], NOW)
        assert service.effective_state("claude") == DEGRADED

        second = await self._parked(
            db, provider, pool_reconciler, text=CLAUDE_IDLE_POOL_PANE, sid="pool-b",
            availability=False,
        )
        await pool_reconciler._step_abandoned_pool_claim_loop([second], NOW)
        assert service.effective_state("claude") == EXHAUSTED
        assert pool_reconciler.test_orch.terminations == [
            (first.id, "usage_limit_screen"),
            (second.id, "usage_limit_screen"),
        ]

    async def test_with_failover_off_it_is_an_ordinary_stalled_claim_loop(
        self, db, provider, pool_reconciler
    ):
        pool_reconciler.config.provider_failover.mode = "off"
        row = await self._parked(db, provider, pool_reconciler, text=CODEX_IDLE_POOL_PANE)

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        assert pool_reconciler.test_orch.provider_availability.rate_limit_exits == []
        assert pool_reconciler.test_orch.terminations == [(row.id, "claim_loop_stalled")]

    @pytest.mark.parametrize(
        "pane",
        [
            # Parked on an empty composer: a stalled loop, cause unknown.
            "› Ask Codex to do anything\n\n  gpt-6-astra xhigh · ~/wt/slot-0",
            # Broad rate-limit text is exit-classifier evidence, not the screen.
            "  ⎿  HTTP 429 Too Many Requests: rate limit, retrying\n❯ ",
            # A diff that quotes the wording is output, not the screen.
            f"+■ {CODEX_LIMIT_LINE}\n› Ask Codex to do anything",
        ],
        ids=["empty-composer", "broad-429", "quoted-in-a-diff"],
    )
    async def test_anything_else_is_an_ordinary_stalled_claim_loop(
        self, db, provider, pool_reconciler, pane
    ):
        row = await self._parked(db, provider, pool_reconciler, text=pane)

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        assert pool_reconciler.test_orch.provider_availability.rate_limit_exits == []
        assert pool_reconciler.test_orch.terminations == [(row.id, "claim_loop_stalled")]

    async def test_a_limit_screen_inside_the_grace_is_left_alone(
        self, db, provider, pool_reconciler
    ):
        grace = pool_reconciler._pool_claim_loop_stall_seconds()
        row = await self._parked(
            db, provider, pool_reconciler, text=CODEX_IDLE_POOL_PANE, idle=grace - 5
        )

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        assert pool_reconciler.test_orch.provider_availability.rate_limit_exits == []
        assert pool_reconciler.test_orch.terminations == []
        assert (await db.get_session(row.id)).state == "running"

    async def test_a_worker_that_began_a_claim_records_nothing(
        self, db, provider, pool_reconciler
    ):
        """Evidence follows the recycle fence: a claim that won it is not a stalled loop."""
        row = await self._parked(db, provider, pool_reconciler, text=CODEX_IDLE_POOL_PANE)
        await db.update_session(row.id, claim_phase="claiming", claim_phase_at=NOW)

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        assert pool_reconciler.test_orch.provider_availability.rate_limit_exits == []
        assert pool_reconciler.test_orch.terminations == []

    async def test_without_an_availability_service_it_still_says_why(
        self, db, provider, pool_reconciler
    ):
        row = await self._parked(
            db, provider, pool_reconciler, text=CODEX_IDLE_POOL_PANE, availability=False
        )

        await pool_reconciler._step_abandoned_pool_claim_loop([row], NOW)

        assert pool_reconciler.test_orch.terminations == [(row.id, "usage_limit_screen")]


async def _waiting_session(db, provider, rec, config, tmp_path, lifecycle="task", timeout=7200):
    """Real wait commands, claim and workspace, with only the terminal faked."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.commands import CommandHandler

    await _task(db)
    await _busy_agent_and_workspace(db, tmp_path)
    task = await db.get_task("t1")
    row = await _session(
        db, provider, agent_id="a1", lifecycle=lifecycle,
        claim_phase="active" if lifecycle == "pool" else None,
        last_claim_epoch=task.claim_epoch if lifecycle == "pool" else None,
        started_at=NOW - 10000, last_activity=NOW - 10000,
    )
    provider.sessions[row.name].activity = NOW - 10000
    # The test clock must also govern subsequent nudges/teardown.
    orch = rec.orchestrator
    if orch is None:
        orch = SimpleNamespace(db=db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
        rec.orchestrator = orch
    if not hasattr(orch, "bus"):
        orch.bus = SimpleNamespace(emit=AsyncMock())
    orch.command_handler = CommandHandler(orch, config)
    await db.create_task(Task(id="producer", project_id="p1", title="Producer", description=""))
    wait = await db.register_agent_wait(
        identity=dict(session_id=row.id, instance_token=row.instance_token, project_id="p1",
                      claim_epoch=task.claim_epoch, elevated=False),
        kind="task", match={"task_id": "producer"}, deadline_at=NOW + timeout,
        idempotency_key="durable", now=NOW,
    )
    return row, wait


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
async def test_long_wait_preserves_claim_workspace_and_silent_activity(
    db, provider, pool_reconciler, config, tmp_path, lifecycle, monkeypatch
):
    config.agents_config.stuck_timeout_seconds = 60
    rec = pool_reconciler
    row, _ = await _waiting_session(db, provider, rec, config, tmp_path, lifecycle)
    monkeypatch.setattr("src.sessions.reconciler.time.time", lambda: NOW)
    for tick in (NOW, NOW + 1800, NOW + 3600, NOW + 7199):
        await rec.tick(now=tick)
        current = await db.get_session(row.id)
        assert current.state == "running"
        assert current.task_id == "t1"
        assert current.last_activity == NOW - 10000
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS
        assert (await db.get_agent("a1")).state == AgentState.BUSY
        assert (await db.get_workspace("ws1")).locked_by_task_id == "t1"
    assert provider.sent_nudges == []
    assert rec.test_orch.terminations == []


@pytest.mark.parametrize("resolution", ["completed", "expired", "missing", "cancelled"])
@pytest.mark.parametrize("lifecycle", ["task", "pool"])
async def test_wait_wake_gets_fresh_lease_and_resets_stall_ladder(
    db, provider, pool_reconciler, config, tmp_path, resolution, lifecycle, monkeypatch
):
    from sqlalchemy import delete
    from src.database.tables import tasks

    rec = pool_reconciler
    config.agents_config.stuck_timeout_seconds = 30  # less than one lease
    row, wait = await _waiting_session(db, provider, rec, config, tmp_path, lifecycle)
    await db.set_task_meta("t1", META_STALL_NUDGES, "3")
    await db.set_task_meta("t1", META_STALL_LAST_ACTION, str(NOW - 5000))
    resumed = NOW + 7200
    monkeypatch.setattr("src.sessions.reconciler.time.time", lambda: resumed)
    if resolution == "completed":
        await db.transition_task("producer", TaskStatus.COMPLETED)
    elif resolution == "missing":
        async with db._engine.begin() as conn:
            await conn.execute(delete(tasks).where(tasks.c.id == "producer"))
        resumed = NOW + 3600
    elif resolution == "cancelled":
        await db.cancel_agent_wait(wait["id"], identity=None, now=resumed)
    # Do not run the global scan: lease consumers must target this wait themselves.
    await rec.tick(now=resumed)
    result = await db.get_agent_wait(wait["id"])
    assert result["state"] != "active"
    assert result["wait_resumed_at"] == resumed
    assert (await db.get_session(row.id)).state == "running"
    assert await db.get_task_meta("t1", META_STALL_NUDGES) == "0"
    assert await db.get_task_meta("t1", META_STALL_LAST_ACTION) == str(resumed)
    await rec.tick(now=resumed + config.sessions.lease_ttl_seconds)
    assert (await db.get_session(row.id)).state == "running"
    assert provider.sent_nudges == []
    # Grace is bounded: a session that ignores the result is enforced normally.
    await rec.tick(now=resumed + config.sessions.lease_ttl_seconds + 1)
    assert (await db.get_session(row.id)).state == "stopped"


async def test_unrelated_wait_and_stale_epoch_cannot_exempt_a_stall(
    db, provider, reconciler, config, tmp_path, monkeypatch
):
    from sqlalchemy import update
    from src.database.tables import tasks

    row, wait = await _waiting_session(db, provider, reconciler, config, tmp_path)
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "t1").values(claim_epoch=2))
    monkeypatch.setattr("src.sessions.reconciler.time.time", lambda: NOW)
    await reconciler.tick(now=NOW)
    assert len(provider.sent_nudges) == 1
    assert (await db.get_session(row.id)).state == "running"
    assert await db.blocking_wait_for(row, 2, NOW) is None
    assert await db.agent_wait_for_claim(row, 2) is None
    # A producer's existence, or a wait belonging to another claim, supplies no exemption.
    await db.reconcile_agent_waits(now=NOW)
    assert (await db.get_agent_wait(wait["id"]))["state"] == "cancelled"


async def test_dead_waiting_session_is_recovered_and_old_result_survives(
    db, provider, releasing_reconciler, config, tmp_path, monkeypatch
):
    rec = releasing_reconciler
    row, wait = await _waiting_session(db, provider, rec, config, tmp_path)
    monkeypatch.setattr("src.sessions.reconciler.time.time", lambda: NOW)
    await provider.stop(rec._handle(row))
    await rec.tick(now=NOW)
    assert (await db.get_session(row.id)).state != "running"
    assert (await db.get_task("t1")).status != TaskStatus.IN_PROGRESS
    await db.reconcile_agent_waits(now=NOW + 1)
    result = await db.get_agent_wait(wait["id"])
    assert result["state"] == "cancelled"
    assert await db.get_message(result["result_message_id"])


async def test_manual_pause_is_not_unpaused_by_wait_result(
    db, provider, reconciler, config, tmp_path, monkeypatch
):
    row, wait = await _waiting_session(db, provider, reconciler, config, tmp_path)
    await db.transition_task("t1", TaskStatus.PAUSED, resume_after=None)
    monkeypatch.setattr("src.sessions.reconciler.time.time", lambda: NOW)
    await reconciler.tick(now=NOW)
    await db.reconcile_agent_waits(now=NOW + 1)
    assert (await db.get_task("t1")).status == TaskStatus.PAUSED
    assert (await db.get_task("t1")).resume_after is None
    assert (await db.get_agent_wait(wait["id"]))["state"] == "cancelled"
    assert (await db.get_session(row.id)).state == "stopped"


async def test_restart_reconstructs_wait_exemption_without_heartbeat(
    db, provider, pool_reconciler, registry, bus, config, tmp_path
):
    row, _ = await _waiting_session(db, provider, pool_reconciler, config, tmp_path, "pool")
    restarted = SessionReconciler(
        db, config, registry, bus=bus, orchestrator=pool_reconciler.orchestrator, epoch="restart"
    )
    await restarted.adopt_on_start()
    await restarted.tick(now=NOW + 3600)
    assert (await db.get_session(row.id)).state == "running"
    assert (await db.get_workspace("ws1")).locked_by_task_id == "t1"
    assert provider.sent_nudges == []


@pytest.mark.tmux
async def test_opt_in_real_harness_wait_idle(db, config, tmp_path):
    """AQ_WAIT_REAL_HARNESS=codex|claude enables a paid, isolated live-idle probe.

    Observe actual transcripts across more than one shortened lease interval:
    no heartbeat/tool/model turn while waiting, then one result-pointer nudge.
    This never launches or contacts the operator daemon.
    """
    import os
    import shutil
    import time
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pathlib import Path
    from src.commands import CommandHandler
    from src.sessions.harness_parser import parse_harness_markdown
    from src.sessions.tmux import TmuxProvider
    from src.sessions.transcripts import resolve_reader
    from src.messages.delivery import MessageDeliveryEngine

    harness_id = os.environ.get("AQ_WAIT_REAL_HARNESS")
    if not harness_id:
        pytest.skip("opt in with AQ_WAIT_REAL_HARNESS=codex or claude (uses installed credentials)")
    assert harness_id in {"codex", "claude"}
    assert shutil.which(harness_id) and shutil.which("tmux"), "real harness and tmux required"
    harness = parse_harness_markdown(
        Path(f"src/sessions/default_harnesses/{harness_id}.md").read_text()
    ).harness
    assert harness is not None
    config.sessions.provider = "tmux"
    config.sessions.tmux_socket = f"aq-wait-test-{uuid.uuid4().hex[:10]}"
    config.sessions.lease_ttl_seconds = 3
    config.agents_config.stuck_timeout_seconds = 3
    config.data_dir = str(tmp_path / "state")
    provider = TmuxProvider(config=config)
    launch = time.time()
    token = uuid.uuid4().hex
    prompt = (
        "This is an isolated live-idle test. Reply exactly WAIT_IDLE and end your turn. "
        "Use no tools and do not poll or sleep. If another prompt arrives, reply WAIT_RESULT "
        "and end that turn, again without tools."
    )
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("AQ_")}
    child_env.pop("CLAUDECODE", None)
    child_env.update(AQ_SESSION_ID="wait-probe", AQ_INSTANCE_TOKEN=token)
    work = tmp_path / "harness"
    work.mkdir()
    args = (harness_id,)
    if harness_id == "claude":
        args += ("--session-id", str(uuid.uuid4()))
    spec = SessionSpec(
        session_name="s-wait-probe", work_dir=str(work), command=(*args, prompt),
        env=child_env, instance_token=token, process_names=harness.process_names,
        ready_delay_ms=harness.ready_delay_ms, ready_prompt_prefix=harness.ready_prompt_prefix,
        dialogs=harness.dialogs, skip_escape_before_enter=harness.skip_escape_before_enter,
        composer_clear_keys=harness.composer_clear_keys,
    )
    reader = resolve_reader(harness_id)
    assert reader is not None
    handle = None
    try:
        handle = await provider.start(spec)
        # Initial model turn is setup, not the measured idle interval.
        path = None
        for _ in range(120):
            path = reader.resolve_path(str(work), None)
            if path:
                entries, offset = await reader.read_new(path, 0)
                if any(e.turn_complete for e in entries) and reader.activity(entries) == "idle":
                    break
            await asyncio.sleep(0.5)
        else:
            pytest.fail("real harness did not finish its initial turn within 60 seconds")
        await _task(db)
        await _busy_agent_and_workspace(db, work)
        task = await db.get_task("t1")
        row = SessionRecord(
            id="wait-probe", project_id="p1", profile_id="worker", harness=harness_id,
            provider="tmux", name=spec.session_name, lifecycle="task", work_dir=str(work),
            epoch="probe", instance_token=token, started_at=launch, task_id="t1",
            state="running", agent_id="a1", last_activity=launch,
        )
        await db.create_session(row)
        orch = SimpleNamespace(db=db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
        orch.command_handler = CommandHandler(orch, config)
        registry = SimpleNamespace(create=lambda *_: provider)
        rec = SessionReconciler(db, config, registry, orchestrator=orch)
        now = time.time()
        wait = await db.register_agent_wait(
            identity=dict(session_id=row.id, instance_token=token, project_id="p1",
                          claim_epoch=task.claim_epoch, elevated=False),
            kind="timer", match={"due_at": now + 60}, deadline_at=now + 120,
            idempotency_key="idle-probe", now=now,
        )
        for _ in range(8):
            await rec.tick(now=time.time())
            await asyncio.sleep(1)
        idle_entries, _ = await reader.read_new(path, offset)
        assert not [
            e for e in idle_entries
            if e.type in {"user", "tool_use"} or e.usage
            or (e.type == "assistant" and (e.text or e.turn_complete))
        ]
        assert (await db.get_session(row.id)).state == "running"
        assert (await db.get_workspace("ws1")).locked_by_task_id == "t1"
        assert await db.get_task_meta("t1", META_STALL_NUDGES) is None
        # Resolve normally and deliver using the real provider input channel.
        await db.cancel_agent_wait(wait["id"], identity=None, now=time.time())
        class IdleLens:
            async def activity(self, **_):
                return "idle"

            async def nudge(self, *, text, **_):
                await provider.nudge(handle, text)
                return True

        engine = MessageDeliveryEngine(db, IdleLens(), config)
        assert (await engine.run_delivery_pass())["delivered"] == 1
        assert (await engine.run_delivery_pass())["delivered"] == 0
        result_seen = False
        for _ in range(120):
            after, _ = await reader.read_new(path, offset)
            if any(e.type == "user" and f"aq wait show {wait['id']}" in e.text for e in after):
                result_seen = True
                break
            await asyncio.sleep(0.5)
        assert result_seen, "result pointer did not reach the real transcript"
    finally:
        await provider.stop(handle or SessionHandle(spec.session_name, "tmux", token), grace=1)
        process = await asyncio.create_subprocess_exec(
            "tmux", "-L", config.sessions.tmux_socket, "kill-server",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=10)
        socket_root = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}"
        (socket_root / config.sessions.tmux_socket).unlink(missing_ok=True)
