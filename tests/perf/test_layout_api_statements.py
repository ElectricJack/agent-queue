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
exact objection CLAUDE.md raises about every wall-clock budget in this
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
from sqlalchemy import event

from scripts.seed_layout_perf import seed_project
from src.api.graph_layout import build_graph_layout_router
from src.task_graph.layout.driver import LayoutDriver
from tests.pg_dsn import ensure_worker_postgres_dsn

DSN = ensure_worker_postgres_dsn()
pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(not DSN, reason="POSTGRES_TEST_DSN not set"),
]

PROJECT = "perf"
TILES = f"/api/projects/{PROJECT}/graph/tiles"

#: Samples in a timed loop, and in each of the two reference loops that
#: bracket it.  The reference gets fewer because it is only ever read at
#: the median and it is paid for twice.
SAMPLES = 50
REFERENCE_SAMPLES = 20

#: Round trips one steady-state tiles request is allowed, as
#: ``(statements, pooled transactions)``.  They are equal because every DB
#: call on this path takes its own connection: seven of them always run --
#: ``get_project``, ``get_layout_meta``, the collapsed containers' paths,
#: the edges touching them, ``list_agents``, ``list_gates`` and the
#: visible rows with their tasks -- and two more (``load_layout_rows``
#: plus ``load_rows_with_tasks`` for the stub titles) only when an edge
#: has an endpoint the request is not returning.
#:
#: Measured on ``pg_small`` at 9/9 for both focus shapes and 7/7 for the
#: rect one, whose cross-epic edge lands on a container that is itself
#: inside the window, so it never reaches the stub queries.  A rect shape
#: that starts paying them is a real change in what the request does, not
#: noise: read the endpoint before raising this.
TILES_ROUND_TRIPS_WITH_STUBS = (9, 9)
TILES_ROUND_TRIPS_NO_STUBS = (7, 7)

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
FOCUS_MEDIAN_SLACK = 6.0
FOCUS_TAIL_SLACK = 9.0


async def _seed(db, *, cross_edge: tuple[str, str] | None = None, **shape) -> None:
    await seed_project(db, PROJECT, **shape)
    if cross_edge is not None:
        await db.add_dependency(*cross_edge)
    drv = LayoutDriver(db)
    await drv.full_layout(PROJECT, "all")
    await drv.full_layout(PROJECT, "active")


@pytest.fixture
async def pg(any_db):
    """The §9 reference project: 5,151 tasks, one 1,000-task epic."""
    if any_db._engine.dialect.name != "postgresql":
        pytest.skip("postgres only")
    await _seed(any_db, epics=100, per_epic=40, big_epic=1000, hub_dependents=50)
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


async def _node(ac, task_id: str) -> dict:
    r = await ac.get(f"/api/projects/{PROJECT}/graph/node/{task_id}?variant=all")
    assert r.status_code == 200, r.text
    return r.json()["node"]


async def _times(ac, payload, samples: int) -> list[float]:
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
    r = await ac.post(TILES, json=payload)
    assert r.status_code == 200, r.text
    times: list[float] = []
    gc.collect()
    gc.freeze()
    try:
        for _ in range(samples):
            started = time.perf_counter()
            r = await ac.post(TILES, json=payload)
            times.append(time.perf_counter() - started)
            assert r.status_code == 200
    finally:
        gc.unfreeze()
    times.sort()
    return times


async def _assert_within_reference(
    ac, payload, reference, label: str, *, median_slack: float, tail_slack: float
) -> None:
    """Time ``payload`` against ``reference`` measured on both sides of it.

    Both readings of the reference are taken because one taken in a quiet
    moment before a busy one is how a normalised budget still turns into a
    coin flip -- the loop between them is seconds long and this box is
    shared.  Both are printed, so a run whose two readings disagree says
    so rather than quietly averaging a moving box into a budget.
    """
    before = statistics.median(await _times(ac, reference, REFERENCE_SAMPLES))
    times = await _times(ac, payload, SAMPLES)
    after = statistics.median(await _times(ac, reference, REFERENCE_SAMPLES))
    floor = (before + after) / 2
    median = statistics.median(times)
    # The third-slowest of 50 rather than an interpolated p95: at this
    # sample count the two differ by a sample or two of the tail and the
    # order statistic is at least a number that was actually measured.
    tail = times[-3]
    print(
        f"\n[perf] {label}: median {median * 1000:.1f}ms "
        f"({median / floor:.2f}x, budget {median_slack}x), "
        f"tail {tail * 1000:.1f}ms ({tail / floor:.2f}x, budget {tail_slack}x), "
        f"max {times[-1] * 1000:.1f}ms over {SAMPLES} samples; "
        f"reference {floor * 1000:.1f}ms ({before * 1000:.1f} then {after * 1000:.1f})"
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


def _rect_around(node: dict) -> dict:
    """A 16x16-unit window centred on ``node`` -- inside ``RECT_CAP``."""
    return {"x0": node["x"] - 1, "y0": node["y"] - 1, "x1": node["x"] + 15, "y1": node["y"] + 15}


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


async def test_tiles_latency_with_big_collapsed_epic_visible(perf_strict, pg):
    """A collapsed 1,000-task epic in the rect, against a collapsed 40-task one.

    A collapsed container owns every edge into its subtree, so the request
    reads all of its descendants' paths and every edge touching them: the
    cost is the subtree, not the one tile that is drawn.  epic1 is the
    same request over a subtree 25x smaller, which is what makes the ratio
    a statement about how that cost scales rather than about the box.
    """
    async with _client(pg) as ac:
        big = await _node(ac, "epic0")
        small = await _node(ac, "epic1")
        payload = {"variant": "all", "rect": _rect_around(big), "expanded": []}
        reference = {"variant": "all", "rect": _rect_around(small), "expanded": []}
        await _assert_within_reference(
            ac,
            payload,
            reference,
            "rect/collapsed-big-epic",
            median_slack=RECT_MEDIAN_SLACK,
            tail_slack=RECT_TAIL_SLACK,
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
        reference = {
            "variant": "all",
            "rect": _own_box(small),
            "root": "epic1",
            "expanded": [],
        }
        for expanded in ([], ["epic0-pkg0"]):
            payload = {
                "variant": "all",
                "rect": _own_box(big),
                "root": "epic0",
                "expanded": expanded,
            }
            warm = await ac.post(TILES, json=payload)
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
