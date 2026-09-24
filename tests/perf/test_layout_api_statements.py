"""tiles round-trip shape and latency on PostgreSQL (spec §9).

Two kinds of budget live here and they catch different regressions.

``test_tiles_round_trip_budget`` is deterministic: it counts the SQL
statements and the pooled connection checkouts one steady-state tiles
request issues.  Which calls the endpoint makes does not depend on how
much each returns, so it is counted on a seed 1/100th the size of the one
below and runs in seconds.  It needs neither a quiet box nor
``perf_strict``, and it is the detector for a request that grows a round
trip -- an added statement or transaction is a few percent of the
millisecond totals below, which no wall-clock slack can see.

The two latency tests are wall-clock, so each takes ``perf_strict``
(``tests/conftest.py``) and the module is marked ``perf``: without both, a
latency assertion runs in CI's ``Tests (default)`` job under ``-n auto
--dist loadfile`` and fails on a saturated box rather than on a
regression.

**Their budgets are derived, not declared.**  The flat ``p95 < 100 ms``
they used to assert went red at 107.8 ms on a box at load average 10-16
with the median at 90.2 ms and nothing in the endpoint changed -- the
exact objection AGENTS.md raises about every wall-clock budget in this
suite, that "they measure the machine as much as the query".  Each test
now measures a *reference* request -- the same endpoint, the same round
trips, over a container whose subtree is 25x smaller -- on both sides of
its timed loop, and asserts the median as a multiple of that reference.
A slower box moves both numbers together, so the ratio stays about the
code; and because the reference issues the same round trips as the
subject, the ratio is about the *volume* each request moves rather than
about the wire.  What that leaves uncovered is a regression that slows
the shared fixed path, which raises the reference too: that is what the
round-trip budget and the sibling ``test_layout_statements.py`` are for.

Run them deliberately, serially, on a quiet machine, with
``POSTGRES_TEST_DSN`` and ``AQ_PERF_STRICT=1`` in the environment::

    aq test -m perf -p no:xdist -s tests/perf/test_layout_api_statements.py
"""

from __future__ import annotations

import gc
import statistics
import time
from contextlib import asynccontextmanager, contextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql

from scripts.seed_layout_perf import seed_project
from src.api.graph_layout import build_graph_layout_router
from src.database.tables import layout_jobs
from src.database.queries.layout_queries import crossing_edges_statement
from src.task_graph.layout.driver import LayoutDriver
from src.task_graph.layout.view import owner_map, remap_edges, resolve_visible
from tests.pg_dsn import ensure_worker_postgres_dsn

DSN = ensure_worker_postgres_dsn()
pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DSN not set"),
]

PROJECT = "perf"
#: The reference project for the rect budget: the same shape at about a
#: fifth the size, in the same database and the same process.  It replaces the old
#: "same request over epic1 instead of epic0" reference, which stopped being
#: a request at all once the geometry a rect is culled against became the
#: *compacted* one: ``_rect_around`` reads a node's PERSISTED box from
#: ``/graph/node``, and with every container collapsed nothing is drawn
#: there any more.  Measured on this fixture, that reference returned zero
#: nodes -- so the budget it anchored was "median < 8 x the cost of an empty
#: response", i.e. the flat wall-clock number the module docstring says this
#: file stopped asserting.
REFERENCE_PROJECT = "perfref"
TILES = f"/api/projects/{PROJECT}/graph/tiles"


def _tiles(project: str) -> str:
    return f"/api/projects/{project}/graph/tiles"

#: Samples in a timed loop, and in each of the two reference loops that
#: bracket it.  The reference gets fewer because it is only ever read at
#: the median and it is paid for twice.
SAMPLES = 50
REFERENCE_SAMPLES = 20

#: ``LIST_CAP`` in the endpoint; the page size ``_drawn_rect`` walks with.
LIST_PAGE = 200

#: Round trips one steady-state tiles request is allowed, as
#: ``(statements, pooled transactions)``.  They are equal because every DB
#: call on this path takes its own connection: nine of them always run --
#: ``get_project``, ``get_layout_meta``, the collapsed containers' paths,
#: the edges touching them, ``list_agents``, ``list_gates``, the visible
#: rows with their tasks, one ``count_task_subtasks`` lookup and one
#: ``get_task_meta_bulk`` (phase metadata) lookup over those same visible
#: ids -- and two more (``load_layout_rows`` plus ``load_rows_with_tasks``
#: for the stub titles) only when an edge has an endpoint the request is
#: not returning.
#:
#: Measured on ``pg_small`` at 11/11 for both focus shapes and 9/9 for the
#: rect one, whose cross-epic edge lands on a container that is itself
#: inside the window, so it never reaches the stub queries.  A rect shape
#: that starts paying them is a real change in what the request does, not
#: noise: read the endpoint before raising this.
TILES_ROUND_TRIPS_WITH_STUBS = (11, 11)
TILES_ROUND_TRIPS_NO_STUBS = (9, 9)

#: Latency budgets, as a multiple of the reference request measured on the
#: same box in the same process (see the module docstring).
#:
#: The assertion is on the **median**, not the p95.  A p95 over 50 samples
#: is interpolated between the second- and third-slowest sample, so it is
#: only as stable as the tail: it is what went red at 107.8 ms against a
#: 100 ms budget while the median sat at 90.2 ms.  The tail slack keeps
#: the third-slowest sample bounded as a "one request in fifty stalled"
#: guard rather than as a budget.
#:
#: The two request shapes get their own numbers because their natural
#: ratios differ -- a collapsed container costs its whole subtree against
#: a reference window that has none of it, while a focus request and its
#: reference both pay for their own scope.
#:
#: Measured 2026-09-09 on PostgreSQL 18 in Docker over localhost, one
#: 24-core box, two full runs -- the first at load average 2-6, the second
#: with eight spinners at 12-13, which is the load the flat budget failed
#: at.  Median as a multiple of the reference, and in milliseconds:
#:
#: ================================  ===========  ===========
#: case                              load 2-6     load 12-13
#: ================================  ===========  ===========
#: rect/collapsed-big-epic           5.18x  55.1  5.81x  66.5
#: focus/root=epic0 expanded=[]      3.86x  74.1  4.00x  80.6
#: focus/root=epic0 expanded=[pkg0]  3.78x  75.3  2.98x  58.4
#: ================================  ===========  ===========
#:
#: Each budget is ~1.4x the largest ratio seen.  Note what the
#: normalisation does and does not buy: it shrinks the spread on two of
#: the three cases and not much on the third, because a good deal of the
#: run-to-run movement is the subject's own and not the box's.  What it
#: does buy is that a slower box -- a different machine, a PostgreSQL over
#: a slower link, a CI runner -- moves the reference by the same factor it
#: moves the subject, where a flat millisecond number moves only the
#: verdict.  Within a run the reference is steady: its two readings, taken
#: on either side of a ten-second loop, agreed to within 6% on every case
#: of both runs.
RECT_MEDIAN_SLACK = 8.0
RECT_TAIL_SLACK = 12.0
#: Absolute wall-clock ceiling per node the rect request actually draws (see
#: that test's docstring for the ruling this encodes).  Measured 0.61ms/node;
#: ~2x headroom, so the 185ms-at-109-nodes regression this budget was written
#: after (1.70ms/node) fails while an honestly denser window does not.
RECT_MS_PER_NODE = 1.5
FOCUS_MEDIAN_SLACK = 6.0
FOCUS_TAIL_SLACK = 9.0


async def _seed(
    db, project: str = PROJECT, *, cross_edge: tuple[str, str] | None = None, **shape
) -> None:
    await seed_project(db, project, **shape)
    if cross_edge is not None:
        await db.add_dependency(*cross_edge)
    drv = LayoutDriver(db)
    await drv.full_layout(project, "all")
    await drv.full_layout(project, "active")


async def test_layout_job_ledger_uses_the_kind_leading_index_at_history_scale(pg_small):
    """The convergence ledger is append-only, so its rules-kind sweep must index.

    This is deliberately a plan test rather than a timing threshold: the
    production reader's actual predicate is observed first, then the same
    projection is explained after a multi-version history has been bulk
    loaded and analysed.  A small table makes a sequential scan a valid
    planner choice and would not guard the 900-second fleet sweep.
    """
    target_kind = "rules:37"
    history_kinds = 80
    rows_per_kind = 250
    async with pg_small._engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO layout_jobs "
            "(id, project_id, variant, kind, status, requested_at, finished_at, error) "
            "SELECT 'ledger-' || k || '-' || n, "
            "       'ledger-project-' || (n % 25), "
            "       CASE WHEN n % 2 = 0 THEN 'all' ELSE 'active' END, "
            "       'rules:' || k, "
            "       CASE WHEN n % 7 = 0 THEN 'failed' ELSE 'done' END, "
            "       n, n + 1, NULL "
            f"FROM generate_series(0, {history_kinds - 1}) AS k "
            f"CROSS JOIN generate_series(1, {rows_per_kind}) AS n"
        )
        await conn.exec_driver_sql("ANALYZE layout_jobs")

    statements: list[str] = []

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(pg_small._engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        await pg_small.layout_job_ledger(target_kind)
    finally:
        event.remove(pg_small._engine.sync_engine, "before_cursor_execute", record_statement)
    actual = next(statement for statement in statements if "FROM layout_jobs" in statement)
    assert "layout_jobs.kind" in actual

    projection = select(
        layout_jobs.c.project_id,
        layout_jobs.c.variant,
        layout_jobs.c.status,
        layout_jobs.c.error,
        layout_jobs.c.finished_at,
    ).where(layout_jobs.c.kind == target_kind)
    compiled = projection.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    async with pg_small._engine.begin() as conn:
        result = await conn.exec_driver_sql(f"EXPLAIN (ANALYZE, BUFFERS) {compiled}")
    plan = "\n".join(row[0] for row in result)
    assert "idx_layout_jobs_kind_project_variant_status" in plan
    assert "Seq Scan on layout_jobs" not in plan


@pytest.fixture
async def pg(any_db):
    """The §9 reference project (5,151 tasks) plus its 1/25 reference twin."""
    if any_db._engine.dialect.name != "postgresql":
        pytest.skip("postgres only")
    await _seed(any_db, epics=100, per_epic=40, big_epic=1000, hub_dependents=50)
    # The twin is scaled down *structurally*, not just numerically: its own
    # big epic (200 rather than 1,000 tasks) and 20 root children rather than
    # 4, so it exercises the same row target and the same
    # collapsed-container-owns-its-subtree cost the subject does, a fifth as
    # much of it.  A twin without those exercises mostly the fixed per-request
    # cost, and then the ratio normalises the box rather than the shape.
    # ``tasks.id`` is unique across projects, so it gets its own id namespace
    # rather than colliding with ``epic0`` / ``hub``.
    await _seed(
        any_db,
        REFERENCE_PROJECT,
        epics=20,
        per_epic=40,
        big_epic=200,
        hub_dependents=10,
        id_prefix="ref-",
    )
    yield any_db


@pytest.fixture
async def pg_small(any_db):
    """The same *shape* at 1/100th the size.

    Which DB calls a tiles request makes does not depend on how much each
    one returns, so the round-trip budget can be counted here -- and
    seeding is per-test (``any_db`` is function-scoped), which at the §9
    scale above is minutes rather than seconds.

    The one thing the seeder does not give this shape is an edge that
    *leaves* the container under test: every dependency it writes is
    inside one package, so every edge remaps onto a node the request
    already returns and the two stub queries are never reached.  A budget
    counted without them would be a budget for a graph nobody has, so one
    cross-epic dependency is added before the layout is published.
    """
    if any_db._engine.dialect.name != "postgresql":
        pytest.skip("postgres only")
    await _seed(
        any_db,
        epics=3,
        per_epic=20,
        big_epic=50,
        hub_dependents=2,
        cross_edge=("epic0-pkg0-t1", "epic2-pkg0-t1"),
    )
    yield any_db


@asynccontextmanager
async def _client(db):
    app = FastAPI()
    app.include_router(build_graph_layout_router(db=db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        yield ac


@contextmanager
def _count_round_trips(db):
    """Statements *and* pooled transactions for the enclosed requests.

    Statements come from ``before_cursor_execute`` (what
    ``tests/perf/test_hierarchy_statements.count_statements`` counts) and
    transactions from the pool's ``checkout``, since every ``begin()`` /
    ``connect()`` on this path takes a fresh pooled connection.  The
    transactions are the larger cost and the half nothing else counts: on
    asyncpg a checkout pays ``BEGIN``, ``COMMIT`` and a ``pool_pre_ping``
    that is itself three round trips, so one is worth about six statements
    of wire.
    """
    counts = {"statements": 0, "transactions": 0}

    def _statement(conn, cursor, statement, parameters, context, executemany):
        counts["statements"] += 1

    def _checkout(dbapi_connection, record, proxy):
        counts["transactions"] += 1

    sync_engine = db._engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _statement)
    event.listen(sync_engine.pool, "checkout", _checkout)
    try:
        yield counts
    finally:
        event.remove(sync_engine, "before_cursor_execute", _statement)
        event.remove(sync_engine.pool, "checkout", _checkout)


#: Reads whose result size is what a tiles request pays for in rows.
_ROW_READS = (
    "load_layout_rows",
    "load_rows_for_containers",
    "load_rows_with_tasks",
    "load_paths_by_prefixes",
    "load_paths_by_ids",
    "load_edges_touching",
)


@contextmanager
def _count_rows_loaded(db):
    """Rows each tiles read returns, per adapter method.

    The companion to :func:`_count_round_trips`: that one sees a request
    grow a *statement*, this one sees it grow a *result set*.  Both are
    deterministic on a fixed seed, which is what makes them budgets rather
    than weather reports.
    """
    counts: dict[str, int] = dict.fromkeys(_ROW_READS, 0)
    originals = {name: getattr(db, name) for name in _ROW_READS}

    def instrument(name, original):
        async def wrapper(*args, **kwargs):
            result = await original(*args, **kwargs)
            counts[name] += len(result)
            return result

        return wrapper

    for name, original in originals.items():
        setattr(db, name, instrument(name, original))
    try:
        yield counts
    finally:
        for name, original in originals.items():
            setattr(db, name, original)


async def _node(ac, task_id: str) -> dict:
    r = await ac.get(f"/api/projects/{PROJECT}/graph/node/{task_id}?variant=all")
    assert r.status_code == 200, r.text
    return r.json()["node"]


async def _times(ac, request: tuple[str, dict], samples: int) -> list[float]:
    """Sorted timings for ``samples`` tiles requests after a warm-up.

    The warm-up primes the connection pool, the query-plan caches and the
    endpoint's geometry cache, so the timed loop measures steady state.

    ``gc.freeze()`` around the loop is what makes the number a measurement
    of the endpoint rather than of the fixture.  This process is holding
    the seeded 5,151-task project alive and a gen-2 collection has to walk
    all of it: measured here, ~2 to 5 of 50 samples caught one and each
    cost an extra 50-70 ms, which was the entire difference between a
    43 ms median and a 95 ms "p95".  Freezing moves everything allocated
    up to this point into the permanent generation, so gen-2 stops
    rescanning the fixture.  GC stays *enabled* -- per-request garbage is
    still collected, so the request keeps paying for its own allocations,
    which is the cost a real server would pay.  A server holds a
    connection pool, not a test's object graph.

    Measured by these tests on PostgreSQL 18, one 24-core box at load ~2,
    run serially -- p95 / max in milliseconds:

    ================================  =============  ============
    case                              unfrozen       frozen
    ================================  =============  ============
    rect/collapsed-big-epic           123.4 / 130.8  53.1 / 56.3
    focus/root=epic0 expanded=[]       80.5 / 157.7  59.1 / 61.5
    focus/root=epic0 expanded=[pkg0]  105.7 / 150.0  57.9 / 64.4
    ================================  =============  ============

    Frozen, max lands within 25% of the median; unfrozen, the tail is
    really "did three gen-2 collections happen to land in this loop" --
    note that the unfrozen medians (49.8 / 73.2 / 72.7) sit as close to
    the frozen ones as they do.  The endpoint was never the problem.
    """
    url, payload = request
    r = await ac.post(url, json=payload)
    assert r.status_code == 200, r.text
    times: list[float] = []
    gc.collect()
    gc.freeze()
    try:
        for _ in range(samples):
            started = time.perf_counter()
            r = await ac.post(url, json=payload)
            times.append(time.perf_counter() - started)
            assert r.status_code == 200
    finally:
        gc.unfreeze()
    times.sort()
    return times


async def _assert_within_reference(
    ac,
    payload,
    reference,
    label: str,
    *,
    median_slack: float,
    tail_slack: float,
    ms_per_node: float | None = None,
) -> None:
    """Time ``payload`` against ``reference`` measured on both sides of it.

    Both readings of the reference are taken because one taken in a quiet
    moment before a busy one is how a normalised budget still turns into a
    coin flip -- the loop between them is seconds long and this box is
    shared.  Both are printed, so a run whose two readings disagree says
    so rather than quietly averaging a moving box into a budget.
    """
    subject_nodes = len((await ac.post(payload[0], json=payload[1])).json()["nodes"])
    reference_nodes = len((await ac.post(reference[0], json=reference[1])).json()["nodes"])
    assert subject_nodes > 0, f"{label}: subject fixture drew no nodes"
    assert reference_nodes > 0, f"{label}: reference fixture drew no nodes"
    before = statistics.median(await _times(ac, reference, REFERENCE_SAMPLES))
    times = await _times(ac, payload, SAMPLES)
    after = statistics.median(await _times(ac, reference, REFERENCE_SAMPLES))
    floor = (before + after) / 2
    median = statistics.median(times)
    # A reference that draws nothing is not a reference: it measures the
    # endpoint's fixed round trips and turns the ratio below into a flat
    # millisecond budget wearing a normalisation's clothes.
    assert reference_nodes > 0, f"{label}: the reference request returned no nodes"
    # The third-slowest of 50 rather than an interpolated p95: at this
    # sample count the two differ by a sample or two of the tail and the
    # order statistic is at least a number that was actually measured.
    tail = times[-3]
    print(
        f"\n[perf] {label}: median {median * 1000:.1f}ms "
        f"({median / floor:.2f}x, budget {median_slack}x), "
        f"tail {tail * 1000:.1f}ms ({tail / floor:.2f}x, budget {tail_slack}x), "
        f"max {times[-1] * 1000:.1f}ms over {SAMPLES} samples; "
        f"reference {floor * 1000:.1f}ms ({before * 1000:.1f} then {after * 1000:.1f}); "
        f"nodes {subject_nodes} vs {reference_nodes}, "
        f"{median * 1000 / subject_nodes:.3f}ms/node vs "
        f"{floor * 1000 / reference_nodes:.3f}ms/node"
    )
    assert median < median_slack * floor, (
        f"{label}: median {median * 1000:.1f}ms is {median / floor:.2f}x the "
        f"{floor * 1000:.1f}ms reference request (budget {median_slack}x)"
    )
    assert tail < tail_slack * floor, (
        f"{label}: third-slowest of {SAMPLES} was {tail * 1000:.1f}ms, "
        f"{tail / floor:.2f}x the {floor * 1000:.1f}ms reference request "
        f"(budget {tail_slack}x)"
    )
    if ms_per_node is not None:
        per_node = median * 1000 / subject_nodes
        assert per_node < ms_per_node, (
            f"{label}: {per_node:.3f}ms per drawn node over {subject_nodes} nodes "
            f"(budget {ms_per_node}ms/node)"
        )


def _rect_around(node: dict) -> dict:
    """A 16x16-unit window centred on ``node`` -- inside ``RECT_CAP``."""
    return {"x0": node["x"] - 1, "y0": node["y"] - 1, "x1": node["x"] + 15, "y1": node["y"] + 15}


async def _drawn_rect(ac, project: str, size: float = 16.0) -> dict:
    """A ``size``x``size`` window anchored where the collapsed view is DRAWN.

    ``_rect_around`` below takes a node's persisted box from ``/graph/node``,
    which is where the engine published it with every container expanded. A
    tiles request culls against the *compacted* geometry instead (design
    §3.5), and under ``expanded: []`` every container shrinks to one tile and
    the whole root scope packs back towards its origin -- so a rect built
    from persisted coordinates can easily frame a region nothing is drawn in.
    The ``list`` endpoint reports the same compacted boxes the canvas gets,
    so the window is anchored on those -- on **all** of them.  A single page
    would make the anchor a function of ``depth_first_order``'s first 200
    rows, which is deterministic but arbitrary: paging to exhaustion makes it
    the geometry's own top-left corner, which is what the window means.
    """
    nodes: list[dict] = []
    cursor = None
    while True:
        body = {"variant": "all", "expanded": [], "limit": LIST_PAGE}
        if cursor:
            body["cursor"] = cursor
        r = await ac.post(f"/api/projects/{project}/graph/list", json=body)
        assert r.status_code == 200, r.text
        page = r.json()
        nodes.extend(page["nodes"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert nodes, f"{project}: nothing is drawn in the collapsed view"
    x0 = min(n["x"] for n in nodes) - 1
    y0 = min(n["y"] for n in nodes) - 1
    return {"x0": x0, "y0": y0, "x1": x0 + size, "y1": y0 + size}


def _own_box(node: dict) -> dict:
    """The node's own cell.  Under ``root`` the rect neither caps nor culls,
    but the request model still requires one."""
    return {"x0": node["x"], "y0": node["y"], "x1": node["x"] + 1, "y1": node["y"] + 1}


async def test_tiles_round_trip_budget(pg_small):
    """Round trips -- statements *and* transactions -- for one tiles request.

    This is the deterministic half of this file's budgets, and the only
    one that can see a request grow a round trip.  A steady-state request
    reads the endpoint's geometry cache, so what is counted is the
    per-request tail: the project, the layout meta, the collapsed
    containers' paths, the edges touching them, the agent list, the open
    gates and the visible rows with their tasks -- plus, when an edge
    points at something the request is not returning, the stub rows and
    their titles.

    ``list_gate_waiters_for_project`` is deliberately absent: the seeded
    project has no open gate, and the endpoint only pays for waiters when
    ``list_gates`` returns one.  That is the fix a long-lived project
    needed (327 resolved gates, 668 ms per request) and this count is what
    holds it -- a per-gate loop would show up here as statements that grow
    with the project.
    """
    async with _client(pg_small) as ac:
        epic0 = await _node(ac, "epic0")
        shapes = [
            (
                "rect/collapsed-epic0",
                {"variant": "all", "rect": _rect_around(epic0), "expanded": []},
                TILES_ROUND_TRIPS_NO_STUBS,
            ),
            (
                "focus/root=epic0 expanded=[]",
                {
                    "variant": "all",
                    "rect": _own_box(epic0),
                    "root": "epic0",
                    "expanded": [],
                },
                TILES_ROUND_TRIPS_WITH_STUBS,
            ),
            (
                "focus/root=epic0 expanded=[pkg0]",
                {
                    "variant": "all",
                    "rect": _own_box(epic0),
                    "root": "epic0",
                    "expanded": ["epic0-pkg0"],
                },
                TILES_ROUND_TRIPS_WITH_STUBS,
            ),
        ]
        for label, payload, (statements, transactions) in shapes:
            # Two warm-ups, not one: the first fills the geometry cache
            # this shape reads and the second proves the counted request
            # is the steady-state one rather than the fill.
            for _ in range(2):
                assert (await ac.post(TILES, json=payload)).status_code == 200
            with _count_round_trips(pg_small) as counts:
                r = await ac.post(TILES, json=payload)
            assert r.status_code == 200, r.text
            print(
                f"\n[round trips] {label}: {counts['statements']} statements "
                f"(budget {statements}) over {counts['transactions']} "
                f"transactions (budget {transactions})"
            )
            assert counts["statements"] <= statements, (
                f"{label}: {counts['statements']} statements > budget {statements}"
            )
            assert counts["transactions"] <= transactions, (
                f"{label}: {counts['transactions']} transactions > budget {transactions}"
            )


#: Edge rows a steady-state collapsed tiles request may load per edge it
#: draws.
#:
#: This is the budget with teeth.  A collapsed container owns every dependency
#: inside its subtree and can draw none of them, so an endpoint that reads
#: "every edge touching every hidden task" reads the whole project to draw a
#: handful of arrows: measured on the §9 fixture before this budget existed, a
#: fully collapsed root view loaded 9,490 edge rows and drew 22.
#:
#: It is per *drawn* edge on purpose -- a view that legitimately frames more
#: nodes (a wider row target packs more collapsed tiles into the same window)
#: moves the denominator too, so this catches work that grows without the
#: picture growing, which is the only kind that is a regression.
#:
#: There is deliberately no companion "rows per node" aggregate.  The other
#: big read on this path, ``load_paths_by_prefixes``, is *supposed* to scale
#: with the hidden subtrees rather than with the tiles on screen -- it is how
#: the owner map is built -- so any number put on it would be a constant about
#: this fixture's shape, not an invariant, and would fail the day somebody
#: seeds a deeper one.
EDGE_ROWS_PER_EDGE = 4.0


async def test_tiles_row_budget_for_a_collapsed_view(pg_small):
    """Edge rows loaded, per edge the response actually draws."""
    async with _client(pg_small) as ac:
        payload = {"variant": "all", "rect": await _drawn_rect(ac, PROJECT), "expanded": []}
        for _ in range(2):
            assert (await ac.post(TILES, json=payload)).status_code == 200
        with _count_rows_loaded(pg_small) as rows:
            r = await ac.post(TILES, json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        nodes, edges = len(body["nodes"]), len(body["edges"])
        assert nodes and edges, "the budget needs a response that draws something"
        print(
            f"\n[rows] collapsed view: {nodes} nodes, "
            f"{rows['load_edges_touching']} edge rows for {edges} edges "
            f"({rows['load_edges_touching'] / edges:.1f}/edge, "
            f"budget {EDGE_ROWS_PER_EDGE}); {rows}"
        )
        assert rows["load_edges_touching"] <= EDGE_ROWS_PER_EDGE * edges, (
            f"{rows['load_edges_touching']} edge rows loaded for {edges} drawn edges "
            f"({rows['load_edges_touching'] / edges:.1f}/edge, "
            f"budget {EDGE_ROWS_PER_EDGE}) -- an edge whose endpoints share a "
            "collapsed container can never be drawn and must not cross the wire"
        )


async def _resolved_view(db, expanded: list[str]):
    """The ids and owner map one tiles request would build, for ``expanded``."""
    cand = {
        t: rt[0]
        for t, rt in (
            await db.load_rows_for_containers(PROJECT, "all", [None, *expanded])
        ).items()
    }
    vis = resolve_visible(
        cand, expanded=set(expanded), max_depth=None, root=None, forced_expanded=set()
    )
    collapsed = dict(vis.collapsed_paths)
    hidden = await db.load_paths_by_prefixes(PROJECT, "all", list(collapsed.values()))
    hidden_owner = owner_map(hidden, collapsed)
    owners = dict(hidden_owner)
    owners.update({t: t for t in vis.visible})
    return dict(vis.visible), hidden_owner, sorted(set(vis.visible) | set(hidden_owner))


async def test_owner_filtered_edge_read_draws_the_same_graph(pg_small):
    """Differential: the filtered read and the unfiltered one draw the same graph.

    The owner-equality test moved into SQL, and the value of that move is
    exactly that most rows never arrive -- which is also how it could go
    wrong unnoticed, since ``remap_edges`` would simply have fewer rows to
    discard.  So the two paths are run side by side over several expanded
    sets and their *outputs* compared: the wire the canvas draws and the
    orphan set that becomes stubs, not the row counts.
    """
    for expanded in ([], ["epic0"], ["epic0", "epic0-pkg0"]):
        visible, hidden_owner, ids = await _resolved_view(pg_small, expanded)
        owners = dict(hidden_owner)
        owners.update({t: t for t in visible})
        unfiltered = await pg_small.load_edges_touching(ids)
        filtered = await pg_small.load_edges_touching(ids, owners=owners)
        assert len(filtered) <= len(unfiltered)
        wire_a, orphans_a = remap_edges(unfiltered, visible, hidden_owner)
        wire_b, orphans_b = remap_edges(filtered, visible, hidden_owner)
        assert wire_a == wire_b, f"expanded={expanded}: the drawn edges differ"
        assert orphans_a == orphans_b, f"expanded={expanded}: the stub endpoints differ"
        print(
            f"\n[differential] expanded={expanded}: {len(unfiltered)} rows -> "
            f"{len(filtered)}, {len(wire_a)} drawn edges, {len(orphans_a)} orphans"
        )


async def test_crossing_edge_read_uses_the_dependency_indexes(pg_small):
    """The statement must not sequentially scan ``task_dependencies``.

    That table carries every project's edges and this read has no project
    column to filter on, so a plan without an index scan costs the whole
    install on every pan -- and a single-project fixture cannot see it.  The
    first shape of this statement had exactly that defect: with
    ``task_dependencies`` on the preserved side of two outer joins, its
    restriction (``a.task_id IS NOT NULL OR b.task_id IS NOT NULL``) was a
    post-join predicate the planner could not push down.

    A plan is a cost decision, so the table is first made big enough that the
    decision is not a coin toss: ~60k unrelated rows, bulk-loaded in one
    statement, then ``ANALYZE``.  Measured on top of the §9 fixture plus one
    560k-edge project, the two forms ran 232ms (seq scan) against 84ms.
    """
    other = 60000
    async with pg_small._engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO projects (id, name, status, created_at)"
            " VALUES ('planscale', 'planscale', 'ACTIVE', 0)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO tasks (id, project_id, title, description, created_at, updated_at)"
            f" SELECT 'ps'||g, 'planscale', 'ps'||g, '', 0, 0"
            f" FROM generate_series(0, {other}) g"
        )
        await conn.exec_driver_sql(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id, dep_type)"
            f" SELECT 'ps'||g, 'ps'||(g-1), 'blocks' FROM generate_series(1, {other}) g"
        )
    async with pg_small._engine.begin() as conn:
        await conn.exec_driver_sql("ANALYZE task_dependencies")

    _visible, _hidden_owner, ids = await _resolved_view(pg_small, [])
    owners = dict(_hidden_owner)
    owners.update({t: t for t in _visible})
    async with pg_small._engine.begin() as conn:
        rows = (await conn.execute(crossing_edges_statement(ids, owners, explain=True))).all()
    plan = "\n".join(r[0] for r in rows)
    print("\n[plan]\n" + "\n".join(line[:140] for line in plan.splitlines()))
    assert "Seq Scan on task_dependencies" not in plan, (
        "the crossing-edge read fell back to a sequential scan of every project's "
        f"dependencies:\n{plan}"
    )
    # The source-side lookup may use either its dedicated (task_id, dep_type)
    # index or the primary key, whose leading task_id column is equally
    # selective for this fixture. The reverse lookup must use its dedicated
    # depends_on index. The no-sequential-scan assertion above is the invariant.
    source_indexed = (
        "idx_task_deps_task_type" in plan or "task_dependencies_pkey" in plan
    )
    assert source_indexed and "idx_task_deps_depson_type" in plan, (
        f"expected both dependency indexes in the plan:\n{plan}"
    )


async def test_tiles_latency_with_big_collapsed_epic_visible(perf_strict, pg):
    """A window full of collapsed epics, against the same window 5x smaller.

    A collapsed container owns every edge into its subtree, so the request
    reads all of its descendants' paths and every edge that leaves them: the
    cost is the subtrees behind the window, not the tiles that are drawn.
    The reference is the identical request over a project of the *same shape*
    -- its own big epic, its own long root row -- at about a fifth the size,
    which is what makes the ratio a statement about how that cost scales
    rather than about the box.

    The window is anchored on the drawn geometry rather than on epic0's
    persisted box -- see :func:`_drawn_rect`.  Under ``expanded: []`` the
    root's children all collapse to one tile each and pack back to the
    origin, so this window frames the collapsed view rather than the hole
    the fully expanded epic0 used to occupy.

    **What this test is allowed to assert, and why it changed** (2026-09-20).
    It used to hold the wall-clock of one fixed window: ~42ms before the
    layout reorganisation lane, ~185ms after, and the brief that opened the
    investigation asked for "no more than ~50ms on this box".  That target is
    superseded, and deliberately.  The aspect-balanced row target
    (``flow.row_target``) publishes the project root as a landscape block
    instead of a one-or-two-wide column, so after compaction this *same*
    window frames 109 collapsed tiles where it framed 13 -- 8.4x the picture
    for 1.6x the time.  Per drawn node the request went from 3.62ms to
    0.61ms.  Holding the old number would have meant either undoing a layout
    decision the operator wants or shrinking the window until it agreed with
    the old one, and neither is a statement about the endpoint.

    So the acceptance criterion is now **per drawn node and per drawn edge**:
    the wall-clock ceiling below (with ~3x headroom over the measured
    0.61ms/node, so a return to 185ms at this node count fails), the ratio to
    a same-shape reference, and the deterministic
    ``test_tiles_row_budget_for_a_collapsed_view`` next door.
    """
    async with _client(pg) as ac:
        payload = (
            _tiles(PROJECT),
            {"variant": "all", "rect": await _drawn_rect(ac, PROJECT), "expanded": []},
        )
        reference = (
            _tiles(REFERENCE_PROJECT),
            {
                "variant": "all",
                "rect": await _drawn_rect(ac, REFERENCE_PROJECT),
                "expanded": [],
            },
        )
        await _assert_within_reference(
            ac,
            payload,
            reference,
            "rect/collapsed-big-epic",
            median_slack=RECT_MEDIAN_SLACK,
            tail_slack=RECT_TAIL_SLACK,
            ms_per_node=RECT_MS_PER_NODE,
        )


async def test_tiles_focus_root_latency(perf_strict, pg):
    """Focus on the 1,000-task epic: cost must track the open containers.

    ``root`` disables the rect cap and ``max_depth``, so before the
    container-scoped candidate load this request pulled epic0's entire
    subtree on every poll.  The reference is the same focus request on
    epic1, whose subtree is 25x smaller: if the scoping ever comes undone
    the subject grows with the big epic and the reference does not.

    The warm-up's assertions are the semantic half of that guard, and they
    are not perf-gated in spirit -- they hold on any box.
    """
    async with _client(pg) as ac:
        big = await _node(ac, "epic0")
        small = await _node(ac, "epic1")
        reference = (
            _tiles(PROJECT),
            {
                "variant": "all",
                "rect": _own_box(small),
                "root": "epic1",
                "expanded": [],
            },
        )
        for expanded in ([], ["epic0-pkg0"]):
            payload = (
                _tiles(PROJECT),
                {
                    "variant": "all",
                    "rect": _own_box(big),
                    "root": "epic0",
                    "expanded": expanded,
                },
            )
            warm = await ac.post(payload[0], json=payload[1])
            assert warm.status_code == 200, warm.text
            ids = {n["id"] for n in warm.json()["nodes"]}
            assert "epic0" in ids
            if expanded:
                # the open package's children are there; its siblings' are not
                assert "epic0-pkg0-t0" in ids
                assert not any(i.startswith("epic0-pkg1-") for i in ids)
            await _assert_within_reference(
                ac,
                payload,
                reference,
                f"focus/root=epic0 expanded={expanded}",
                median_slack=FOCUS_MEDIAN_SLACK,
                tail_slack=FOCUS_TAIL_SLACK,
            )
