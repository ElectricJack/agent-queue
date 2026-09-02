"""Statement/latency budgets for the claim path and pool reconcile — spec §15.

Ruling P2-3: the spec's "≤ 6 logical statements" for ``task_claim`` counts
the claim transaction alone; ``_apply_transition``, activation and metadata
bring the whole command to a larger budget.  The measured numbers are
recorded in each test's docstring.

Ruling P2-7: ``any_db`` (``tests/perf/conftest.py``) parametrises SQLite
(always) and Postgres (only when ``POSTGRES_TEST_DSN`` is set), at
``seed_scale(n_tasks=5000, profile_id="worker")``.

Scope note: every fixture below stubs ``orch.bus.emit = AsyncMock()``, so
event fan-out (whatever a real subscriber -- a playbook trigger, message
delivery, a Discord notifier -- would do in response to ``task.claimed``
etc.) costs zero statements here and is invisible to every budget in this
file. That's deliberate, not an oversight: these budgets measure the
command's *own* statements, not what arbitrary subscriber code might do:
that a claim event ends up on a plugin's playbook trigger does not obligate
that plugin's handler to a statement budget owned by the claim path.
"""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    SessionRecord,
    TaskStatus,
    Workspace,
)
from src.intelligence_classes import IntelligenceClass
from tests.perf.test_hierarchy_statements import count_statements, seed_scale

pytestmark = pytest.mark.perf

PROJECT_ID = "proj"
NOW = time.time()


async def build_handler(any_db, tmp_path):
    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DiscordConfig
    from src.orchestrator import Orchestrator

    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    cfg.swarm.claim_wait_max = 5
    orch = Orchestrator(cfg)
    orch.session_spec_builder._intelligence_classes = {
        "standard-medium": IntelligenceClass(
            "standard-medium",
            "Standard",
            "",
            {"anthropic": {"model": "claude-sonnet-5"}},
        ),
    }
    orch.db = any_db
    orch.git = MagicMock()
    orch._worktree_slots = MagicMock(
        return_value=MagicMock(reset_slot_for_task=AsyncMock(return_value="aq/t"))
    )
    orch._last_scheduler_state = None
    orch._run_completion_pipeline = AsyncMock(return_value=(None, True))
    orch.bus.emit = AsyncMock()
    orch.register_settlement_listener()
    return CommandHandler(orch, cfg)


def scoped(handler, sid):
    handler._current_scope = {
        "kind": "session",
        "session_id": sid,
        "task_id": None,
        "project_id": PROJECT_ID,
        "elevated": False,
    }
    return handler


async def pool_session(any_db, tmp_path, sid="s1", agent_id="agent-1"):
    work_dir = tmp_path / agent_id
    work_dir.mkdir(exist_ok=True)
    await any_db.create_agent(
        Agent(id=agent_id, name=agent_id, profile_id="worker", state=AgentState.IDLE)
    )
    await any_db.create_workspace(
        Workspace(
            id=f"ws-{agent_id}",
            project_id=PROJECT_ID,
            workspace_path=str(work_dir),
            kind_id="project-repo",
            source_type=RepoSourceType.LINK,
            locked_by_agent_id=agent_id,
        )
    )
    await any_db.create_session(
        SessionRecord(
            id=sid,
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name=f"p-worker--proj--{sid}",
            lifecycle="pool",
            work_dir=str(work_dir),
            epoch="e",
            instance_token="t",
            started_at=NOW,
            state="running",
            agent_id=agent_id,
            llm_provider="anthropic",
            model="claude-sonnet-5",
            intelligence_class="standard-medium",
        )
    )
    return sid, work_dir


async def _seed_worker_scale(any_db):
    """Profile + project, then the §15.2-scale seed with ``profile_id='worker'``."""
    await any_db.create_profile(
        AgentProfile(id="worker", name="w", lifecycle="pool", needs_workspace=False)
    )
    await any_db.create_project(Project(id=PROJECT_ID, name="p"))
    await seed_scale(
        any_db, profile_id="worker", intelligence_class="standard-medium"
    )


def _require_returning(any_db):
    """The budgets below assume the ``RETURNING`` fast paths are live.

    SQLite only grew ``RETURNING`` in 3.35; the production code keeps a
    two-statement fallback for older builds, and those extra re-reads are
    exactly what these budgets forbid.  Skipping (loudly) beats asserting
    a number the fallback path cannot hit.
    """
    from src.database.queries.task_queries import SQLITE_RETURNING

    if any_db._engine.dialect.name == "sqlite" and not SQLITE_RETURNING:
        pytest.skip(
            f"sqlite {sqlite3.sqlite_version} < 3.35 has no RETURNING; the claim path "
            "runs its two-statement fallback, which these budgets deliberately exclude"
        )


class TestClaimStatementBudgets:
    async def test_claim_happy_path_statement_budget(self, any_db, tmp_path):
        """Whole ``task_claim`` happy path (slot reset stubbed).

        **Measured on SQLite after the task-11 trim: 14** (was 37-38).
        The trace, in order: the session+profile join and the project read
        (2, outer admission loop — ``max_event_id`` is skipped because
        ``wait == 0``); the claim transaction (9, asserted separately by
        ``test_claim_transaction_statement_budget`` below); and
        ``activate_claim``'s own BEGIN/UPDATE…RETURNING/COMMIT (3).

        What went: the separate ``get_profile`` read; ``max_event_id``;
        ``take_claim_slot``'s re-read (now ``UPDATE … RETURNING``); the
        epoch-bump CAS and the transition pre-read and the post-write task
        re-read (all folded into one fenced ``UPDATE … RETURNING`` through
        ``_apply_transition``); ``_apply_transition``'s 5-statement
        blocked-state recompute (``projection_stable`` — no clause of
        ``blocked_predicate()`` can tell READY from IN_PROGRESS); one of
        the two metadata upserts (batched); ``get_workspace_for_agent``
        (``record_holder`` returns the row); the post-activation session
        re-read (``activate_claim`` returns the row); and
        ``_claimed_response``'s whole ``task_show`` payload build (~10 —
        it now returns the task row; ``aq task show`` is the full view).

        Postgres is 2 lower (no BEGIN/COMMIT cursor statements); it is
        asserted through the same ``any_db`` parametrisation and runs in CI
        where ``POSTGRES_TEST_DSN`` is set — Docker is unavailable on the
        machine this was measured on.
        """
        _require_returning(any_db)
        await _seed_worker_scale(any_db)
        sid, _wd = await pool_session(any_db, tmp_path)
        handler = await build_handler(any_db, tmp_path)
        h = scoped(handler, sid)
        async with count_statements(any_db) as c:
            res = await h._cmd_task_claim({"next": True})
        assert res["result"] == "claimed"
        dialect = any_db._engine.dialect.name
        # The durable-worker eligibility guard (+1), pre-launch task/session
        # revalidation (+2), activation claim fence (+1), and pause-checkpoint
        # cleanup (+1) bring the measured SQLite path from 14 to 19. These
        # protect a concurrent pause/reassignment; retain a bounded budget.
        #
        # Task-session history (commit 7c9a308e, "retain task session
        # history") adds 3 more, to 22 / 18:
        #   +2 inside the claim transaction -- the attempt row must be
        #      written in the *same* commit as the claim, or a crash between
        #      the two leaves a held task with no attempt to attribute it to.
        #      See ``test_claim_transaction_statement_budget``.
        #   +1 ``activate_claim``'s ``SELECT sessions.id ... FOR UPDATE``
        #      holder lock, which takes the session row *before* the task row
        #      so activation locks in the same order as claim and release
        #      (a PostgreSQL deadlock otherwise).
        budget = 22 if dialect == "sqlite" else 18
        print(f"\ntask_claim happy path ({dialect}): {c['n']} statements (budget {budget})")
        assert c["n"] <= budget, f"{c['n']} statements > budget {budget}"

    async def test_claim_transaction_statement_budget(self, any_db, tmp_path):
        """The claim transaction alone — spec §15's "≤ 6 logical statements".

        ``_prepare_and_activate`` is stubbed out, so this counts exactly
        ``_attempt_claim``'s ``immediate()`` block plus the outer loop's two
        pre-reads, which are then subtracted.

        **Measured on SQLite: 10** — BEGIN, the slot CAS
        (``UPDATE … RETURNING``), the §10 work query, the durable-flock
        agent-eligibility guard (``SELECT agents.id … enabled/role/
        deleted_at``), the fenced take (``UPDATE tasks SET status,
        assigned_agent_id, claim_epoch+1 … RETURNING``), the session /
        agent / workspace holder writes, the batched two-key metadata
        upsert, COMMIT.  Eight of those are logical statements; BEGIN and
        COMMIT are SQLite's explicit ``BEGIN IMMEDIATE`` / ``COMMIT``
        (PostgreSQL does not emit them as cursor statements, hence the
        lower budget there).

        **Now 12 / 10.**  Task-session history (commit 7c9a308e) records
        the attempt row inside this transaction:
        ``_start_task_session_attempt``'s single joined read (the session
        row, the agent's display name, and the "already open for this
        pair?" probe, in one statement) and its ``INSERT``.  Both belong
        here rather than in a follow-up commit: spec §10 requires the
        holder to be recorded *in* the claim transaction, and an attempt
        row written afterwards would be lost by any crash in between,
        leaving a held task with no attempt to attribute it to.  Two
        statements is the floor short of a schema change -- the read
        already collapses what were three reads, and the ``INSERT`` cannot
        be merged with it.
        """
        _require_returning(any_db)
        await _seed_worker_scale(any_db)
        sid, _wd = await pool_session(any_db, tmp_path)
        handler = await build_handler(any_db, tmp_path)
        h = scoped(handler, sid)

        prepared = {}

        async def _fake_prepare(session, row, task, cap=None, *, slot=None):
            prepared["task"] = task
            return {"success": True, "result": "claimed", "task": None, "claim_epoch": None}

        h._prepare_and_activate = _fake_prepare
        async with count_statements(any_db) as c:
            res = await h._cmd_task_claim({"next": True})
        assert res["result"] == "claimed"
        assert prepared["task"] is not None
        dialect = any_db._engine.dialect.name
        # The two outer-loop pre-reads (session+profile join, project) are
        # not part of the transaction.
        n = c["n"] - 2
        budget = 12 if dialect == "sqlite" else 10
        print(f"\nclaim transaction only ({dialect}): {n} statements (budget {budget})")
        assert n <= budget, f"{n} statements > budget {budget}"

    async def test_no_ready_work_statement_budget(self, any_db, tmp_path):
        """No matching ready task.

        **Measured on SQLite after the task-11 trim: 7** (was 10) — the two
        outer-loop pre-reads plus the 5-statement ``_attempt_claim``
        transaction: BEGIN, the slot CAS (``UPDATE … RETURNING`` — no
        re-read), the ready-task SELECT that finds nothing, the
        release-slot UPDATE, COMMIT.
        """
        _require_returning(any_db)
        await any_db.create_profile(
            AgentProfile(id="worker", name="w", lifecycle="pool", needs_workspace=False)
        )
        await any_db.create_project(Project(id=PROJECT_ID, name="p"))
        # A §15.2-scale queue with NO tasks routed to "worker" -- the
        # profile filter must still resolve to nothing in O(1) statements.
        await seed_scale(any_db, profile_id=None)
        sid, _wd = await pool_session(any_db, tmp_path)
        handler = await build_handler(any_db, tmp_path)
        h = scoped(handler, sid)
        async with count_statements(any_db) as c:
            res = await h._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"
        dialect = any_db._engine.dialect.name
        budget = 8
        print(f"\nno_ready_work ({dialect}): {c['n']} statements (budget {budget})")
        assert c["n"] <= budget, f"{c['n']} statements > budget {budget}"

    async def test_release_claim_statement_budget(self, any_db, tmp_path):
        """``release_claim`` on an active claim.

        **Measured on SQLite after the task-11 trim: 10** (was 17) —
        BEGIN, the session read, ``_apply_transition``'s pre-read, the
        status ``UPDATE … RETURNING`` (which also carries back the
        ``claim_epoch`` that used to be a separate read), the merged
        ``task.ready`` frontier ``INSERT … SELECT … RETURNING``, the
        workspace / agent / session writes, COMMIT — plus the ready
        listener's one post-commit task read for the ``task.ready``
        fan-out.

        The 5-statement blocked-state recompute is gone:
        IN_PROGRESS → READY is invisible to every clause of
        ``blocked_predicate()``, which is what ``projection_stable=True``
        asserts (and ``_apply_transition`` re-checks — a release to a
        terminal or BLOCKED status still recomputes in full).

        **Now 11 / 9.**  Task-session history (commit 7c9a308e) closes the
        open attempt in the same transaction that gives the task back
        (``UPDATE task_session_attempts SET state, ended_at, end_reason``);
        an attempt left open past the release is what the dashboard reads
        as "still running".  PostgreSQL was already at its 9 and is
        unchanged; only the SQLite number moves, because ``BEGIN
        IMMEDIATE`` / ``COMMIT`` put it exactly 2 above PostgreSQL and the
        old 10 had drifted off that relationship.
        """
        _require_returning(any_db)
        await _seed_worker_scale(any_db)
        sid, _wd = await pool_session(any_db, tmp_path)
        handler = await build_handler(any_db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        assert claimed["result"] == "claimed"
        async with count_statements(any_db) as c:
            await any_db.release_claim(
                sid, task_status=TaskStatus.READY, context="perf", now=time.time()
            )
        dialect = any_db._engine.dialect.name
        budget = 11 if dialect == "sqlite" else 9
        print(f"\nrelease_claim ({dialect}): {c['n']} statements (budget {budget})")
        assert c["n"] <= budget, f"{c['n']} statements > budget {budget}"

    async def test_count_ready_by_profile_statement_budget(self, any_db):
        """``count_ready_by_profile`` is exactly one statement."""
        await _seed_worker_scale(any_db)
        async with count_statements(any_db) as c:
            await any_db.count_ready_by_profile(PROJECT_ID)
        assert c["n"] == 1

    async def test_reconcile_pools_no_starts_statement_budget(self, any_db, tmp_path):
        """3 projects x 3 pool profiles, no starts -- budget <= 3 + 3*3.

        One ``list_profiles()`` for the whole tick, one ``list_projects()``,
        then one ``count_ready_by_profile`` + one ``list_sessions`` per
        active project with a pool profile (``_measure_pools``'s
        docstring) -- no starts means no further writes.
        """
        from src.config import AppConfig, DiscordConfig
        from src.orchestrator import Orchestrator

        for i in range(3):
            await any_db.create_profile(
                AgentProfile(
                    id=f"worker-{i}",
                    name=f"w{i}",
                    lifecycle="pool",
                    min_active=0,
                    max_active=2,
                    harness="claude",
                )
            )
        for i in range(3):
            await any_db.create_project(
                Project(id=f"proj-{i}", name=f"p{i}", default_profile_id="worker-0")
            )

        cfg = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "ws"),
            database_path=str(tmp_path / "test.db"),
            data_dir=str(tmp_path / "data"),
        )
        cfg.sessions.enabled = True
        cfg.sessions.provider = "fake"
        cfg.swarm.enabled = True
        orch = Orchestrator(cfg)
        orch.db = any_db
        orch.bus.emit = AsyncMock()

        async with count_statements(any_db) as c:
            await orch._reconcile_pools()
        assert await any_db.list_sessions(lifecycle="pool") == []
        dialect = any_db._engine.dialect.name
        budget = 3 + 3 * 3
        print(f"\n_reconcile_pools no-starts ({dialect}): {c['n']} statements (budget {budget})")
        assert c["n"] <= budget, f"{c['n']} statements > budget {budget}"


class TestClaimLatency:
    @pytest.mark.perf
    async def test_claim_release_p99_latency(self, any_db, tmp_path):
        """Claim/release p99 over 50 iterations at 5,000 tasks (SQLite).

        The spec's ``<= 50 ms`` (§15.2, ``task_claim --next, DB portion``
        row) is the claim transaction alone; this measures claim + release
        end-to-end through the handler.  Before the task-11 trim that was
        82-127 ms across runs (38 + 17 statements); after it the same loop
        runs well inside the ``<= 60 ms`` budget below.  ``xdist`` load
        makes wall-clock latency flaky under parallel test execution, so
        this only runs with ``AQ_PERF_STRICT=1`` set.
        """
        import os

        if os.environ.get("AQ_PERF_STRICT") != "1":
            pytest.skip("AQ_PERF_STRICT not set")
        if any_db._engine.dialect.name == "sqlite":
            # Ruling P3-5: PostgreSQL is the production backend and SQLite is
            # deprecated.  Under ``NullPool`` (P2-16, required for claim
            # correctness) every transaction opens a fresh sqlite3
            # connection, which puts this loop at ~900 ms p99; the budget is
            # asserted on Postgres (measured 43.65 ms on postgres:18).
            pytest.xfail("SQLite is deprecated; the p99 budget is a Postgres budget")
        await _seed_worker_scale(any_db)
        sid, _wd = await pool_session(any_db, tmp_path)
        handler = await build_handler(any_db, tmp_path)
        h = scoped(handler, sid)

        times = []
        for _ in range(50):
            started = time.perf_counter()
            res = await h._cmd_task_claim({"next": True})
            assert res["result"] == "claimed"
            await any_db.release_claim(
                sid, task_status=TaskStatus.READY, context="perf", now=time.time()
            )
            times.append(time.perf_counter() - started)
        times.sort()
        p99 = times[48]
        budget_s = 0.060
        print(
            f"\nclaim/release p99 over 50 iters: {p99 * 1000:.2f}ms (budget {budget_s * 1000:.0f}ms)"
        )
        assert p99 < budget_s, f"p99 {p99 * 1000:.2f}ms >= {budget_s * 1000:.0f}ms"
