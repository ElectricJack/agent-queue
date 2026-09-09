"""Statement/latency budgets for the claim path and pool reconcile — spec §15.

Ruling P2-3: the spec's "≤ 6 logical statements" for ``task_claim`` counts
the claim transaction alone; ``_apply_transition``, activation and metadata
bring the whole command to a larger budget.  The measured numbers are
recorded in each test's docstring.

Ruling P2-7: ``any_db`` (``tests/perf/conftest.py``) leases one PostgreSQL
database -- the only supported backend -- at
``seed_scale(n_tasks=5000, profile_id="worker")``.  Every number recorded
below is a PostgreSQL count: the driver's transaction boundaries are not
cursor statements, so no ``BEGIN``/``COMMIT`` is counted anywhere in this
file.  Run it with ``POSTGRES_TEST_DSN`` set and the ``perf`` marker
selected::

    aq test tests/perf/test_claim_statements.py -q -p no:xdist -s --aq-all-markers

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
from tests.db_fixtures import lease_dsn
from src.config import DatabaseConfig

pytestmark = pytest.mark.perf

PROJECT_ID = "proj"
NOW = time.time()

#: Statements ``_cmd_task_claim`` runs before it opens the claim transaction:
#: the session+profile join, ``touch_session_activity``, and the project read
#: ``_admission_reason`` judges.  The transaction budget subtracts exactly
#: these, so a pre-read added without updating this constant would otherwise
#: be charged to the transaction.
_OUTER_PRE_READS = 3


def _over(n: int, budget: int, statements) -> str:
    """Failure message that names the drift *and* shows the trace.

    A statement budget is only re-baselineable by someone who can see what
    actually ran; printing the numbered trace on failure is the difference
    between "20 > 18" and knowing which two statements to argue about.
    """
    lines = [f"{n} statements > budget {budget}:"]
    lines += [f"{i:>3}. {' '.join(sql.split())}" for i, sql in enumerate(statements, 1)]
    return "\n".join(lines)


async def build_handler(any_db, tmp_path):
    from src.commands.handler import CommandHandler
    from src.config import DatabaseConfig, AppConfig, DiscordConfig
    from src.orchestrator import Orchestrator

    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database=DatabaseConfig(url=lease_dsn("test.db")),
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
    await seed_scale(any_db, profile_id="worker", intelligence_class="standard-medium")


class TestClaimStatementBudgets:
    async def test_claim_happy_path_statement_budget(self, any_db, tmp_path):
        """Whole ``task_claim`` happy path (slot reset stubbed).

        **Measured on PostgreSQL: 20.**  The trace, statement by statement:

        *Outer admission loop (3).*  1 the session+profile join (one read
        for both — spec §15); 2 ``touch_session_activity``, the durable
        proof that an idle worker's claim loop is alive when a long poll
        leaves the harness silent for its whole wait window; 3 the project
        read that ``_admission_reason`` judges.  ``max_event_id`` is
        skipped because ``wait == 0``.

        *The claim transaction (9, statements 4-12).*  Asserted separately
        by ``test_claim_transaction_statement_budget`` below, which
        subtracts exactly the three outer pre-reads above.

        *Preparation and activation (8, statements 13-20).*  13
        ``claim_preparation_is_current`` — one join re-proving the task and
        session fences before any filesystem work; 14
        ``_prepare_and_activate_locked``'s project re-read, which decides
        whether the claim takes the hierarchical-integration branch path
        from the project's *current* ``hierarchical_integration_mode`` (the
        outer loop's read may be a full ``--wait`` old by then); 15-16
        ``activate_claim``'s two ``FOR UPDATE`` locks, taken session-then-
        task in the same order as claim and release so activation cannot
        deadlock against them, the task one also reading ``branch_name``;
        17 the activation CAS (``UPDATE sessions … RETURNING``, so the
        caller needs no re-read); 18 the ``needs_attention`` delete that
        retires an earlier prepare/release warning at the instant the claim
        becomes usable — the *only* clear on the ``preparing`` retry path,
        which re-activates a task already IN_PROGRESS and so never reaches
        ``_apply_transition``'s copy at statement 8; 19 ``tasks.branch_name``,
        publishing the branch the slot reset just created past every
        activation guard and under the task row lock already held (skipped
        when the row already names that branch — a resume, or a hierarchy
        claim whose branch is pinned at filing); 20
        ``clear_claim_preparation_metadata``'s single delete of the pause
        checkpoint and prepare-backoff ladder.

        What went, historically: the separate ``get_profile`` read;
        ``max_event_id``; ``take_claim_slot``'s re-read (now
        ``UPDATE … RETURNING``); the epoch-bump CAS, the transition pre-read
        and the post-write task re-read (all folded into one fenced
        ``UPDATE … RETURNING`` through ``_apply_transition``);
        ``_apply_transition``'s 5-statement blocked-state recompute
        (``projection_stable`` — no clause of ``blocked_predicate()`` can
        tell READY from IN_PROGRESS); one of the two metadata upserts
        (batched); ``get_workspace_for_agent`` (``record_holder`` returns
        the row); the post-activation session re-read; and
        ``_claimed_response``'s whole ``task_show`` payload build (~10 — it
        now returns the task row; ``aq task show`` is the full view).  Most
        recently, ``clear_claim_preparation_metadata``'s second, value-scoped
        ``needs_attention`` delete, which statement 18 had already made
        redundant.
        """
        await _seed_worker_scale(any_db)
        sid, _wd = await pool_session(any_db, tmp_path)
        handler = await build_handler(any_db, tmp_path)
        h = scoped(handler, sid)
        async with count_statements(any_db) as c:
            res = await h._cmd_task_claim({"next": True})
        assert res["result"] == "claimed"
        budget = 20
        print(f"\ntask_claim happy path: {c['n']} statements (budget {budget})")
        assert c["n"] <= budget, _over(c["n"], budget, c["statements"])

    async def test_claim_transaction_statement_budget(self, any_db, tmp_path):
        """The claim transaction alone — spec §15's "≤ 6 logical statements".

        ``_prepare_and_activate`` is stubbed out, so this counts exactly
        ``_attempt_claim``'s ``immediate()`` block plus the outer loop's
        three pre-reads, which are then subtracted.

        **Measured on PostgreSQL: 9**, in order: the slot CAS
        (``UPDATE sessions … RETURNING``); the §10 work query; the
        durable-worker eligibility guard, which reserves the agent and
        fences its soft delete in one ``UPDATE agents … RETURNING``; the
        fenced take (``UPDATE tasks SET status, assigned_agent_id,
        claim_epoch+1 … RETURNING`` through ``_apply_transition``);
        ``_apply_transition``'s ``needs_attention`` delete, which retires the
        previous operational incident for every execution path that reaches
        IN_PROGRESS, push or pull; ``record_holder``'s session write and its
        workspace ``UPDATE … RETURNING``; the batched two-key metadata
        upsert; and the durable ``task_session_attempts`` insert that gives
        every pool claim restart/audit history.  Transaction boundaries are
        not cursor statements on PostgreSQL, so no ``BEGIN``/``COMMIT`` is
        counted.
        """
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
        # The three outer-loop pre-reads (session+profile join,
        # ``touch_session_activity``, project) are not part of the
        # transaction.  Keep this in step with the happy-path enumeration
        # above: when a pre-read is added or removed there, this subtraction
        # moves with it, or the drift is silently charged to the transaction.
        n = c["n"] - _OUTER_PRE_READS
        budget = 9
        print(f"\nclaim transaction only: {n} statements (budget {budget})")
        assert n <= budget, _over(n, budget, c["statements"][_OUTER_PRE_READS:])

    async def test_no_ready_work_statement_budget(self, any_db, tmp_path):
        """No matching ready task.

        **Measured on PostgreSQL: 6** — the three outer-loop pre-reads
        (session+profile join, ``touch_session_activity``, project) plus the
        3-statement ``_attempt_claim`` transaction: the slot CAS
        (``UPDATE … RETURNING`` — no re-read), the ready-task SELECT that
        finds nothing, and the release-slot UPDATE.  This is the statement
        cost of an *idle* worker's poll, so it is the one budget here a long
        ``--wait`` loop pays repeatedly; keep it tight.
        """
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
        budget = 6
        print(f"\nno_ready_work: {c['n']} statements (budget {budget})")
        assert c["n"] <= budget, _over(c["n"], budget, c["statements"])

    async def test_release_claim_statement_budget(self, any_db, tmp_path):
        """``release_claim`` on an active claim.

        **Measured on PostgreSQL: 9** — the session read (``FOR UPDATE``),
        the integration-owner guard, the status ``UPDATE … RETURNING``, the
        merged ``task.ready`` frontier ``INSERT … SELECT … RETURNING``, the
        attempt completion, the workspace / agent / session writes, and the
        ready listener's post-commit task read.  Driver transaction
        boundaries are not counted as SQL statements.

        The 5-statement blocked-state recompute is gone:
        IN_PROGRESS → READY is invisible to every clause of
        ``blocked_predicate()``, which is what ``projection_stable=True``
        asserts (and ``_apply_transition`` re-checks — a release to a
        terminal or BLOCKED status still recomputes in full).

        So is ``_apply_transition``'s project read for
        ``hierarchical_integration_mode``: development mode can only waive a
        *managed* parent's wake/completion guard, so the read is taken
        lazily behind that check instead of on every transition to READY or
        COMPLETED — of which this release is one.
        """
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
        budget = 9
        print(f"\nrelease_claim: {c['n']} statements (budget {budget})")
        assert c["n"] <= budget, _over(c["n"], budget, c["statements"])

    async def test_count_ready_by_profile_statement_budget(self, any_db):
        """``count_ready_by_profile`` is exactly one statement."""
        await _seed_worker_scale(any_db)
        async with count_statements(any_db) as c:
            await any_db.count_ready_by_profile(PROJECT_ID)
        assert c["n"] == 1

    async def test_reconcile_pools_no_starts_statement_budget(self, any_db, tmp_path):
        """3 projects x 3 pool profiles, no starts -- budget <= 2 + 3*3 + 3.

        One ``list_profiles()`` for the whole tick, one ``list_projects()``,
        then one ``count_ready_by_profile`` + one ``count_available_workspaces``
        + one ``list_sessions`` per active project with a pool profile
        (``_measure_pools``'s docstring), plus one first-tick
        ``pool.bounds_rescoped`` audit write per profile. No starts means no
        further writes.
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
            database=DatabaseConfig(url=lease_dsn("test.db")),
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
        budget = 2 + 3 * 3 + 3
        print(f"\n_reconcile_pools no-starts: {c['n']} statements (budget {budget})")
        assert c["n"] <= budget, f"{c['n']} statements > budget {budget}"


class TestClaimLatency:
    @pytest.mark.perf
    async def test_claim_release_p99_latency(self, perf_strict, any_db, tmp_path):
        """Claim/release p99 over 50 iterations at 5,000 tasks (PostgreSQL).

        The spec's ``<= 50 ms`` (§15.2, ``task_claim --next, DB portion``
        row) is the claim transaction alone; this measures claim + release
        end-to-end through the handler.  Before the task-11 trim that was
        82-127 ms across runs (38 + 17 statements); after it the same loop
        runs well inside the ``<= 60 ms`` budget below.  ``xdist`` load
        makes wall-clock latency flaky under parallel test execution, so
        this only runs with ``AQ_PERF_STRICT=1`` set (the ``perf_strict``
        fixture, shared with the layout budgets).
        """
        await _seed_worker_scale(any_db)
        sid, _wd = await pool_session(any_db, tmp_path)
        handler = await build_handler(any_db, tmp_path)
        h = scoped(handler, sid)
        # Measuring 50 claims on one session means opting out of
        # ``fresh_context_per_task``, whose cap of 1 claim per session would
        # otherwise make every iteration after the first
        # ``session_exhausted``.  The cap is compared inside
        # ``take_claim_slot`` and changes no statement, so this only affects
        # the loop, not the budgets asserted above.
        handler.config.swarm.fresh_context_per_task = False

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
