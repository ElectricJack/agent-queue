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

The one budget that is not a statement count is ``TestClaimLatency``.
Statements are only half of what a round trip costs: this path opens
``ROUND_TRIP_TRANSACTIONS`` pooled *transactions* per claim + release, and
a pooled transaction is about six statements' worth of wire (``BEGIN``,
``COMMIT``, and a ``pool_pre_ping`` that is itself three round trips on
asyncpg).  ``test_claim_release_round_trip_budget`` pins both counts, and
the latency budget is derived from them against a wire floor measured on
the box the test is running on -- see its docstring for why a flat
millisecond number stopped meaning anything.

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
from sqlalchemy import event, text

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

        **Measured on PostgreSQL: 19.**  The trace, statement by statement:

        *Outer admission loop (3).*  1 the session+profile join (one read
        for both — spec §15); 2 ``touch_session_activity``, the durable
        proof that an idle worker's claim loop is alive when a long poll
        leaves the harness silent for its whole wait window; 3 the project
        read that ``_admission_reason`` judges.  ``max_event_id`` is
        skipped because ``wait == 0``.  All three share one pooled
        connection — see ``CLAIM_TRANSACTIONS``.

        *The claim transaction (9, statements 4-12).*  Asserted separately
        by ``test_claim_transaction_statement_budget`` below, which
        subtracts exactly the three outer pre-reads above.

        *Preparation and activation (7, statements 13-19).*  13
        ``claim_preparation_is_current`` — one join re-proving the task and
        session fences before any filesystem work; 14
        ``_prepare_and_activate``'s project re-read, which decides whether
        the claim takes the hierarchical-integration branch path from the
        project's *current* ``hierarchical_integration_mode`` (the outer
        loop's read may be a full ``--wait`` old by then), taken on 13's
        connection; 15-16 ``activate_claim``'s two ``FOR UPDATE`` locks,
        taken session-then-task in the same order as claim and release so
        activation cannot deadlock against them, the task one also reading
        ``branch_name``; 17 the activation CAS (``UPDATE sessions …
        RETURNING``, so the caller needs no re-read); 18 the stale-metadata
        delete: the ``needs_attention`` warning an earlier prepare/release
        left — the *only* clear on the ``preparing`` retry path, which
        re-activates a task already IN_PROGRESS and so never reaches
        ``_apply_transition``'s copy at statement 8 — together with the
        pause checkpoint and prepare-backoff ladder, which go stale at
        exactly this boundary (``activate_claim``'s
        ``clear_preparation_metadata``); 19 ``tasks.branch_name``,
        publishing the branch the slot reset just created past every
        activation guard and under the task row lock already held (skipped
        when the row already names that branch — a resume, or a hierarchy
        claim whose branch is pinned at filing).

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
        now returns the task row; ``aq task show`` is the full view).  Then
        ``clear_claim_preparation_metadata``'s second, value-scoped
        ``needs_attention`` delete, which statement 18 had already made
        redundant.  Most recently that method itself: its remaining delete
        is statement 18's ``IN`` list, inside the activation transaction.
        """
        await _seed_worker_scale(any_db)
        sid, _wd = await pool_session(any_db, tmp_path)
        handler = await build_handler(any_db, tmp_path)
        h = scoped(handler, sid)
        async with count_statements(any_db) as c:
            res = await h._cmd_task_claim({"next": True})
        assert res["result"] == "claimed"
        budget = 19  # keep in step with ``CLAIM_STATEMENTS`` below
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


#: One ``task_claim`` happy path plus one ``release_claim``, as
#: ``test_claim_release_round_trip_budget`` asserts them.  The statement
#: halves are the same 20 and 9 the budgets above own; the transaction halves
#: are new here, and they are what a millisecond budget on this path is
#: actually spending.  A pooled checkout is not free: SQLAlchemy's asyncpg
#: pre-ping is ``BEGIN``/``;``/``ROLLBACK`` (three round trips, ``asyncpg.py``
#: ``_async_ping``) and the transaction itself adds ``BEGIN`` and ``COMMIT``,
#: so one transaction costs about six times what one statement on an
#: already-held connection costs.  That is why the transaction budget is a
#: ratchet in its own right: a query moved onto a caller's open connection
#: is free, and a query given its own ``begin()`` costs six statements'
#: worth of wire without moving a single statement budget.
CLAIM_STATEMENTS = 19
CLAIM_TRANSACTIONS = 4
RELEASE_STATEMENTS = 9
RELEASE_TRANSACTIONS = 2
ROUND_TRIP_STATEMENTS = CLAIM_STATEMENTS + RELEASE_STATEMENTS
ROUND_TRIP_TRANSACTIONS = CLAIM_TRANSACTIONS + RELEASE_TRANSACTIONS

#: How much of the claim/release round trip may be work rather than wire,
#: as a multiple of the floor those two budgets imply.  Measured across
#: eleven runs on 2026-09-09 (PostgreSQL 18 in Docker over localhost, load
#: average 7-16): median 2.6-3.2x once the floor is averaged over both
#: sides of the loop, p99 3.9-8.0x.
#:
#: The median is the assertion because it is the statistic that measures
#: this code -- it held within 0.5x across that whole load range.  A p99
#: over 50 samples is the second-worst sample, and on a shared box that was
#: 2x the median and moved by 3x between runs no matter what the claim path
#: did; its bound is kept loose enough to be a "one iteration in fifty took
#: seconds" guard rather than a budget.
#:
#: Neither number is the regression detector for *count* growth -- an added
#: statement or transaction is a few percent of the median and no
#: wall-clock slack can see it.  ``test_claim_release_round_trip_budget``
#: catches those deterministically.  What these catch is a statement
#: getting much slower without the count changing: a dropped index, a
#: planner regression, a correlated subquery added to the §10 work query.
MEDIAN_SLACK = 4.0
P99_SLACK = 12.0


async def measure_round_trip_shape(db, h, sid) -> dict:
    """Statements and transactions for one claim + one release.

    Both are counted from the engine: ``before_cursor_execute`` for
    statements (as ``count_statements`` does) and the connection pool's
    ``checkout`` for transactions, since every ``begin()`` in this path
    takes a fresh pooled connection.
    """
    counts = {"claim_statements": 0, "claim_transactions": 0}
    bucket = {"statements": 0, "transactions": 0}

    def _statement(conn, cursor, statement, parameters, context, executemany):
        bucket["statements"] += 1

    def _checkout(dbapi_connection, record, proxy):
        bucket["transactions"] += 1

    sync_engine = db._engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _statement)
    event.listen(sync_engine.pool, "checkout", _checkout)
    try:
        res = await h._cmd_task_claim({"next": True})
        assert res["result"] == "claimed"
        counts["claim_statements"] = bucket["statements"]
        counts["claim_transactions"] = bucket["transactions"]
        bucket["statements"] = bucket["transactions"] = 0
        await db.release_claim(sid, task_status=TaskStatus.READY, context="perf", now=time.time())
    finally:
        event.remove(sync_engine, "before_cursor_execute", _statement)
        event.remove(sync_engine.pool, "checkout", _checkout)
    counts["release_statements"] = bucket["statements"]
    counts["release_transactions"] = bucket["transactions"]
    return counts


async def measure_wire_floor(db, samples: int = 40) -> tuple[float, float]:
    """``(per transaction, per statement)`` seconds on *this* box's wire.

    A budget written in milliseconds is a budget on the machine as much as
    on the code -- which is the objection CLAUDE.md raises against every
    wall-clock budget in this suite, and the reason the flat ``60 ms`` this
    file used to assert stopped meaning anything once it was read on a
    different box.  Measuring the floor in the same process, from the same
    pool, against the same server, is what keeps the assertion below about
    the claim path rather than about the hardware: the two numbers returned
    here are the cost of doing *nothing* in the shape the claim path does
    it.

    Medians, not means: one descheduled sample would otherwise inflate the
    floor and hide a real regression.  Call it on both sides of the loop it
    normalises and average -- the floor takes a third of a second and the
    loop takes five, so a single reading taken in a quiet moment before a
    busy one is how a normalised budget still turns into a coin flip.
    """
    one = text("SELECT 1")
    async with db._engine.connect() as conn:
        await conn.execute(one)  # warm the pool and the statement cache
        held = []
        for _ in range(samples):
            started = time.perf_counter()
            await conn.execute(one)
            held.append(time.perf_counter() - started)
    fresh = []
    for _ in range(samples):
        started = time.perf_counter()
        async with db._engine.begin() as conn:
            await conn.execute(one)
        fresh.append(time.perf_counter() - started)
    held.sort()
    fresh.sort()
    return fresh[len(fresh) // 2], held[len(held) // 2]


class TestClaimLatency:
    async def test_claim_release_round_trip_budget(self, any_db, tmp_path):
        """Round trips -- statements *and* transactions -- for claim + release.

        The statement halves duplicate ``TestClaimStatementBudgets`` on
        purpose: this test's subject is the pair, because the latency budget
        below is derived from both and a change to either has to move a
        number here first.

        The transaction halves are the part nothing else in this file
        guards, and they are the larger cost.  A statement on an
        already-held connection was 0.54 ms on the box this was measured on
        (2026-09-09, PostgreSQL 18, load ~7-10); a ``begin()`` around the
        same statement was 3.38 ms, of which ``pool_pre_ping`` (enabled in
        ``create_postgres_engine``) is 1.56 ms -- SQLAlchemy's asyncpg
        pre-ping opens and rolls back a transaction to stay pgbouncer-safe,
        so it is three round trips, not one.  Ten transactions is therefore
        ~34 ms of the round trip before any row is read.

        The four claim transactions, in order: the outer admission loop's
        pre-reads (``get_session_with_profile``, ``touch_session_activity``
        and ``get_project`` on one connection — they run back to back with
        nothing awaited between them, strictly before the long poll, so
        nothing is held across a sleep); ``_attempt_claim``'s
        ``immediate()`` block; ``_prepare_and_activate``'s block
        (``claim_preparation_is_current`` and the project re-read, likewise
        back to back, under the task control lock); and ``activate_claim``'s
        ``immediate()`` block.  Release is two: ``release_claim``'s own
        block, and the ready listener's post-commit ``get_task``.

        This was eight before 2026-09-09: those first three were three
        checkouts, the project re-read was its own, and
        ``clear_claim_preparation_metadata`` was a fifth after activation
        committed (it is now the ``IN`` list on the delete activation
        already issued, which also makes the clear atomic with the claim
        going active).  No statement moved except that merged delete, so
        every statement budget above held across the change.
        """
        await _seed_worker_scale(any_db)
        sid, _wd = await pool_session(any_db, tmp_path)
        handler = await build_handler(any_db, tmp_path)
        handler.config.swarm.fresh_context_per_task = False
        h = scoped(handler, sid)

        counts = await measure_round_trip_shape(any_db, h, sid)
        print(
            f"\nclaim+release round trips: "
            f"claim {counts['claim_statements']} statements "
            f"(budget {CLAIM_STATEMENTS}) / {counts['claim_transactions']} transactions "
            f"(budget {CLAIM_TRANSACTIONS}), "
            f"release {counts['release_statements']} statements "
            f"(budget {RELEASE_STATEMENTS}) / {counts['release_transactions']} transactions "
            f"(budget {RELEASE_TRANSACTIONS})"
        )
        for label, measured, budget in (
            ("claim statements", counts["claim_statements"], CLAIM_STATEMENTS),
            ("claim transactions", counts["claim_transactions"], CLAIM_TRANSACTIONS),
            ("release statements", counts["release_statements"], RELEASE_STATEMENTS),
            ("release transactions", counts["release_transactions"], RELEASE_TRANSACTIONS),
        ):
            assert measured <= budget, f"{label}: {measured} > budget {budget}"

    @pytest.mark.perf
    async def test_claim_release_latency_against_the_wire_floor(
        self, perf_strict, any_db, tmp_path
    ):
        """Claim/release latency over 50 iterations at 5,000 tasks (PostgreSQL).

        The spec's ``<= 50 ms`` (§15.2, ``task_claim --next, DB portion``
        row) is the claim transaction alone; this measures claim + release
        end-to-end through the handler.

        **The flat ``<= 60 ms`` this used to assert is gone**, and so is
        asserting on the p99.  The 60 ms was set against 43.65 ms measured
        in b2769aa0, and the test then went dark: the ``perf`` marker hides
        it from every default run, and once ``swarm.fresh_context_per_task``
        defaulted to true its cap of one claim per session made every
        iteration after the first ``session_exhausted``, so between that
        default and 58b9d944 the loop was not measuring anything at all.
        Run again it missed by 2x -- and the reason is not a regression in
        the claim path.  On the box it was re-measured on (2026-09-09,
        PostgreSQL 18 in Docker over localhost, load average 7-14) one
        statement on a held connection costs 0.46-0.54 ms and one pooled
        transaction costs 2.5-3.4 ms, so the ``ROUND_TRIP_TRANSACTIONS``
        transactions and ``ROUND_TRIP_STATEMENTS`` statements that
        ``test_claim_release_round_trip_budget`` pins were a 34-44 ms wire
        floor on their own when the round trip took ten transactions: 60-73%
        of a 60 ms budget spent before a row is read.  A number of
        milliseconds cannot separate that from a regression, which is
        CLAUDE.md's standing objection to every wall-clock budget in this
        suite.  (Coalescing four of those checkouts on 2026-09-09 took the
        floor down with it -- which is the point of deriving the budget from
        the two constants rather than declaring it: the absolute
        measurements quoted below are from before that change, and the
        *multiples* are what this asserts.)

        So the budget is derived rather than declared.  ``measure_wire_floor``
        measures what a transaction and a statement cost on *this* box, on
        both sides of the loop; the floor is computed from the two budget
        constants rather than from what the path actually issued -- an added
        transaction has to raise the measurement without raising the floor
        or this would absorb it silently -- and the median is allowed
        ``MEDIAN_SLACK`` times that, the p99 ``P99_SLACK`` times.

        Measured across eleven runs here, load average 7-16: floor
        31-50 ms, median 79-153 ms, p99 118-284 ms.  In absolute
        milliseconds that is a 2x spread with the claim path unchanged; as a
        multiple of the floor the median is 2.6-3.2x throughout, which is
        the whole point.  Of the ~50 ms above the floor at the median,
        ~19 ms is the §10 work query alone: it walks the 2,499-row frontier
        and top-N sorts it because the ``ORDER BY`` leads with the affinity
        ``CASE``, and it costs ~30 ms rather than ~19 ms once the planner
        has real statistics for the seeded rows.

        Still ``perf_strict``-gated: normalising by the wire floor removes
        the machine, not the neighbours, and ``xdist`` makes any wall-clock
        latency flaky under parallel execution.
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

        before = await measure_wire_floor(any_db)
        times = []
        for _ in range(50):
            started = time.perf_counter()
            res = await h._cmd_task_claim({"next": True})
            assert res["result"] == "claimed"
            await any_db.release_claim(
                sid, task_status=TaskStatus.READY, context="perf", now=time.time()
            )
            times.append(time.perf_counter() - started)
        after = await measure_wire_floor(any_db)

        transaction_s = (before[0] + after[0]) / 2
        statement_s = (before[1] + after[1]) / 2
        # ``transaction_s`` already carries one statement, so only the
        # statements beyond one per transaction are charged again.
        floor_s = (
            ROUND_TRIP_TRANSACTIONS * transaction_s
            + (ROUND_TRIP_STATEMENTS - ROUND_TRIP_TRANSACTIONS) * statement_s
        )
        times.sort()
        median, p99 = times[25], times[48]
        print(
            f"\nclaim/release over 50 iters: median {median * 1000:.2f}ms "
            f"({median / floor_s:.1f}x, budget {MEDIAN_SLACK}x), "
            f"p99 {p99 * 1000:.2f}ms ({p99 / floor_s:.1f}x, budget {P99_SLACK}x); "
            f"wire floor {floor_s * 1000:.2f}ms for {ROUND_TRIP_TRANSACTIONS} "
            f"transactions and {ROUND_TRIP_STATEMENTS} statements "
            f"({transaction_s * 1000:.2f}ms/transaction, "
            f"{statement_s * 1000:.2f}ms/statement on this box)"
        )
        assert median < MEDIAN_SLACK * floor_s, (
            f"median {median * 1000:.2f}ms is {median / floor_s:.1f}x the "
            f"{floor_s * 1000:.2f}ms wire floor for {ROUND_TRIP_TRANSACTIONS} "
            f"transactions and {ROUND_TRIP_STATEMENTS} statements "
            f"(budget {MEDIAN_SLACK}x)"
        )
        assert p99 < P99_SLACK * floor_s, (
            f"p99 {p99 * 1000:.2f}ms is {p99 / floor_s:.1f}x the "
            f"{floor_s * 1000:.2f}ms wire floor (budget {P99_SLACK}x)"
        )
