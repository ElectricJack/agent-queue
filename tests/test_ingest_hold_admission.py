"""Ingestion hold at every admission and activation path (ingestion design §4).

A task whose ``held_batch_id`` is set belongs to an ingestion batch awaiting
fidelity review: it may be READY, but no scheduler, claim, launch, ready
notification, delivery gate, train root or settlement path admits it, and no
gate, resume or reroute clears the hold.

The inventory below is a ratchet in both directions.  Every function that
calls ``not_ingest_held``, ``claim_frontier_predicates``,
``_frontier_predicates`` or ``ingest_held_task_ids`` must have an entry, and
every entry must still call it; each SQL row's executed statements must carry
the predicate.  A new admission path therefore fails here until it is listed
and proved.
"""

from __future__ import annotations

import ast
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql

from src.config import AppConfig, DatabaseConfig
from src.database.queries.claim_queries import _frontier_predicates, claim_frontier_predicates
from src.database.queries.hierarchy_queries import _settlement_clauses, stale_container_clauses
from src.database.queries.routing_queries import _eligible_backlog_predicate
from src.database.tables import tasks
from src.integration.delivery_observer import gating_delivery_ids
from src.models import Agent, AgentProfile, AgentState, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.scheduler import AssignAction
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"
NOW = time.time()
REPO = Path(__file__).resolve().parents[1]
HELD = re.compile(r"\bheld_batch_id IS NULL\b")

# Every guarded function, keyed ``path::qualname``, with the proof that it
# keeps the hold: ``sql`` (a probe below executes it and its SQL must carry
# the predicate), ``builder`` (a pure predicate builder compiled below),
# ``frontier`` (reads the shared frontier builders) or a named behavioral test.
NOT_INGEST_HELD = {
    "src/database/queries/claim_queries.py::_frontier_predicates": "builder",
    "src/database/queries/claim_queries.py::ClaimQueryMixin.take_task": "sql",
    "src/database/queries/claim_queries.py::ClaimQueryMixin.lock_claim_for_activation": (
        "tests/test_claim_commands.py::test_activation_rechecks_ingestion_hold_without_admission"
    ),
    "src/database/queries/task_queries.py::TaskQueryMixin.list_active_tasks": "sql",
    "src/database/queries/task_queries.py::TaskQueryMixin._note_frontier_entry": "sql",
    "src/database/queries/task_queries.py::TaskQueryMixin.ingest_held_task_ids": (
        "test_ingest_held_task_ids_names_only_held_members"
    ),
    "src/database/queries/blocked_state.py::BlockedStateMixin.get_ready_frontier": "sql",
    "src/database/queries/hierarchy_queries.py::_settlement_clauses": "builder",
    "src/database/queries/hierarchy_queries.py::HierarchyQueryMixin.hierarchy_runnable_task_ids": (
        "sql"
    ),
    "src/database/queries/agent_queries.py::AgentQueryMixin._assign_task_to_agent": "sql",
    "src/database/queries/integration_train_queries.py::"
    "IntegrationTrainQueriesMixin.eligible_root_page_on": "sql",
    "src/integration/delivery_observer.py::gating_delivery_ids": "sql",
}
FRONTIER_CALLERS = {
    "src/database/queries/claim_queries.py::claim_frontier_predicates": "builder",
    "src/database/queries/claim_queries.py::_frontier_where": "sql",
    "src/database/queries/claim_queries.py::ClaimQueryMixin.claim_frontier_exclusions_many": (
        "test_frontier_diagnostics_report_ingestion_hold"
    ),
    "src/database/queries/routing_queries.py::_eligible_backlog_predicate": "builder",
    "src/database/queries/task_reroute_queries.py::TaskRerouteQueryMixin.list_spill_candidates": (
        "sql"
    ),
    "src/doctor/task_checks.py::_check_ready_frontier_exclusions": "frontier",
    "src/integration/epic_delivery.py::EpicDeliveryProjection._workers_on": "frontier",
}
HELD_ID_CALLERS = {
    "src/database/queries/task_queries.py::TaskQueryMixin._notify_ready": (
        "test_ready_notification_skips_held_members"
    ),
    "src/orchestrator/execution.py::ExecutionMixin._launch_session_for_task_locked": (
        "test_launch_refuses_member_held_after_assignment"
    ),
    "src/orchestrator/core.py::Orchestrator._recover_stale_state": (
        "tests/test_v2_restart_resume.py::test_recovery_keeps_hold"
    ),
}


def _callers(name: str) -> set[str]:
    """``path::qualname`` of every function under ``src/`` that calls *name*."""
    found: set[str] = set()
    for path in sorted((REPO / "src").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()

        def walk(node, scope: str, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                inner = scope
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    inner = f"{scope}.{child.name}" if scope else child.name
                if isinstance(child, ast.Call):
                    func = child.func
                    called = getattr(func, "id", None) or getattr(func, "attr", None)
                    if called == name:
                        found.add(f"{rel}::{scope}")
                walk(child, inner, rel)

        walk(ast.parse(path.read_text()), "")
    return found


@pytest.mark.parametrize(
    ("names", "inventory"),
    [
        (("not_ingest_held",), NOT_INGEST_HELD),
        (("claim_frontier_predicates", "_frontier_predicates"), FRONTIER_CALLERS),
        (("ingest_held_task_ids",), HELD_ID_CALLERS),
    ],
)
def test_inventory_lists_every_caller(names, inventory):
    callers = set().union(*(_callers(name) for name in names))
    missing = callers - set(inventory)
    assert not missing, f"new {names} caller(s) need an inventory row and a proof: {missing}"
    stale = set(inventory) - callers
    assert not stale, f"inventory rows no longer call {names}: {stale}"


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect()))


def test_frontier_builders_carry_the_hold():
    for predicates in (_frontier_predicates(), claim_frontier_predicates()):
        assert HELD.search(_sql(predicates["ingest_held"]))
    assert HELD.search(_sql(_eligible_backlog_predicate([PROJECT_ID])))


def test_settlement_builders_carry_the_hold():
    for clauses in (_settlement_clauses(), stale_container_clauses()):
        assert any(HELD.search(_sql(clause)) for clause in clauses)


# -- SQL probes: the statements each inventory row really executes ------------


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("test.db")
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(
        AgentProfile(id="worker", name="w", lifecycle="pool", needs_workspace=False)
    )
    await database.create_agent(
        Agent(id="agent-1", name="agent-1", profile_id="worker", state=AgentState.IDLE)
    )
    yield database


async def mktask(db, tid, status=TaskStatus.READY, **kw):
    kw.setdefault("intelligence_class", "standard-medium")
    kw.setdefault("route_source", "legacy")
    kw.setdefault("profile_id", "worker")
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def hold(db, *task_ids, batch="batch-held"):
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id.in_(task_ids))
                           .values(held_batch_id=batch))


async def held_batch(db, task_id):
    async with db._engine.connect() as conn:
        return await conn.scalar(select(tasks.c.held_batch_id).where(tasks.c.id == task_id))


@contextmanager
def captured_sql(db):
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db._engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def _note_frontier_entry(db):
    async with db.immediate() as conn:
        return await db._note_frontier_entry(conn, {"held"}, reason="probe")


async def _select_ready(db):
    async with db.immediate() as conn:
        return await db.select_ready_for_profile(
            conn, project_id=PROJECT_ID, profile_id="worker", agent_id="agent-1",
        )


async def _take_task(db):
    async with db.immediate() as conn:
        return await db.take_task(conn, "held", agent_id="agent-1", now=NOW)


async def _gating(db):
    async with db._engine.connect() as conn:
        return await gating_delivery_ids(conn, PROJECT_ID)


async def _train_roots(db):
    async with db._engine.connect() as conn:
        return await db.eligible_root_page_on(
            conn, project_id=PROJECT_ID, repository_id="repo", after=None, limit=10,
        )


SQL_PROBES: dict[str, Callable[..., Awaitable]] = {
    "ClaimQueryMixin.take_task": _take_task,
    "_frontier_where": _select_ready,
    "TaskQueryMixin.list_active_tasks": lambda db: db.list_active_tasks(exclude_held_ready=True),
    "TaskQueryMixin._note_frontier_entry": _note_frontier_entry,
    "BlockedStateMixin.get_ready_frontier": lambda db: db.get_ready_frontier(PROJECT_ID),
    "HierarchyQueryMixin.hierarchy_runnable_task_ids": (
        lambda db: db.hierarchy_runnable_task_ids(["held"])
    ),
    "AgentQueryMixin._assign_task_to_agent": lambda db: db.assign_task_to_agent("held", "agent-1"),
    "IntegrationTrainQueriesMixin.eligible_root_page_on": _train_roots,
    "gating_delivery_ids": _gating,
    "TaskRerouteQueryMixin.list_spill_candidates": (
        lambda db: db.list_spill_candidates(["worker"], ready_project_ids=[PROJECT_ID])
    ),
}


def test_every_sql_row_has_a_probe():
    rows = {
        key.split("::", 1)[1]
        for key, proof in {**NOT_INGEST_HELD, **FRONTIER_CALLERS}.items()
        if proof == "sql"
    }
    assert rows == set(SQL_PROBES)


@pytest.mark.parametrize("symbol", sorted(SQL_PROBES))
async def test_sql_row_executes_the_hold(db, symbol):
    await mktask(db, "held")
    await hold(db, "held")
    with captured_sql(db) as statements:
        result = await SQL_PROBES[symbol](db)
    assert any(HELD.search(statement) for statement in statements), (symbol, statements)
    # Every probe targets a held READY task: none may admit it.
    assert result in (None, False, [], set(), ([], 0)) or "held" not in str(result), result
    assert await held_batch(db, "held") == "batch-held"


# -- behavior --------------------------------------------------------------


@pytest.mark.parametrize("batched", [False, True])
async def test_frontier_diagnostics_report_ingestion_hold(db, batched):
    await mktask(db, "held")
    await mktask(db, "free")
    await hold(db, "held")
    options = {"cached_only": True, "hierarchy_modes": {}}
    if batched:
        reasons = await db.claim_frontier_exclusions_many(["held", "free"], **options)
        assert reasons["free"] == []
        held_reasons = reasons["held"]
    else:
        held_reasons = await db.claim_frontier_exclusions("held", **options)
    [reason] = [r for r in held_reasons if r["code"] == "frontier_ingest_held"]
    assert reason["ref"] == "held"
    assert "batch-held" in reason["detail"]
    assert await held_batch(db, "held") == "batch-held"


async def test_ingest_held_task_ids_names_only_held_members(db):
    await mktask(db, "held")
    await mktask(db, "free")
    await hold(db, "held")
    assert await db.ingest_held_task_ids({"held", "free", "missing"}) == {"held"}
    assert await db.ingest_held_task_ids(set()) == set()


async def test_scheduler_inputs_skip_held(db):
    await mktask(db, "held")
    await mktask(db, "free")
    await hold(db, "held")
    assert {t.id for t in await db.list_active_tasks(exclude_held_ready=True)} == {"free"}
    assert await db.hierarchy_runnable_task_ids(["held", "free"]) == {"free"}
    assert [t.id for t in await db.get_ready_frontier(PROJECT_ID)] == ["free"]
    assert await db.assign_task_to_agent("held", "agent-1") is False
    task, agent = await db.get_task("held"), await db.get_agent("agent-1")
    assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)
    assert (agent.state, agent.current_task_id) == (AgentState.IDLE, None)


async def test_ready_notification_skips_held_members(db):
    seen: list[list[tuple[str, str]]] = []

    async def listener(entries):
        seen.append(entries)

    await mktask(db, "held")
    await mktask(db, "free")
    await hold(db, "held")
    async with db.immediate() as conn:
        assert await db._note_frontier_entry(conn, {"held", "free"}, reason="probe") == ["free"]
    db.set_ready_listener(listener)
    await db._notify_ready([("held", "gate"), ("free", "gate")])
    await db._notify_ready([("held", "gate")])
    assert seen == [[("free", "gate")]]


async def test_gate_resume_and_reroute_never_clear_the_hold(db):
    await mktask(db, "gated")
    await mktask(db, "paused", status=TaskStatus.PAUSED)
    await mktask(db, "moved")
    await db.create_profile(
        AgentProfile(id="other", name="o", lifecycle="pool", needs_workspace=False)
    )
    await hold(db, "gated", "paused", "moved")
    gate_id, _ = await db.create_gate(PROJECT_ID, "human", "approve", waiter_task_ids=["gated"])
    assert (await db.get_task("gated")).is_blocked
    await db.resolve_gate(gate_id, resolved_by="test")
    await db.resume_task("paused")
    assert await db.apply_task_reroute(
        "moved", expected_profile_id="worker", to_profile_id="other",
        record={"project_id": PROJECT_ID, "reason_code": "provider_unavailable"},
    )
    for task_id in ("gated", "paused", "moved"):
        task = await db.get_task(task_id)
        assert (task.status, task.is_blocked) == (TaskStatus.READY, False), task_id
        assert await held_batch(db, task_id) == "batch-held", task_id
    # READY and unblocked, yet still outside every admission path.
    assert await db.list_active_tasks(exclude_held_ready=True) == []
    assert await db.get_ready_frontier(PROJECT_ID) == []
    assert await db.count_ready_by_profile(PROJECT_ID) == {}


async def test_launch_refuses_member_held_after_assignment(db, tmp_path, caplog):
    """A batch hold that lands between assignment and launch starts no session."""
    caplog.set_level("INFO", logger="src.orchestrator.execution")
    await mktask(db, "held")
    assert await db.assign_task_to_agent("held", "agent-1")
    await hold(db, "held")
    config = AppConfig(
        database=DatabaseConfig(url=lease_dsn("test.db")),
        data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"),
    )
    orch = Orchestrator(config)
    orch.db = db
    task = await db.get_task("held")
    await orch._launch_session_for_task(
        AssignAction("agent-1", "held", PROJECT_ID), task, await db.get_profile("worker"), None
    )
    assert await db.list_sessions() == []
    task, agent = await db.get_task("held"), await db.get_agent("agent-1")
    assert (task.status, task.assigned_agent_id) == (TaskStatus.PAUSED, None)
    assert (agent.state, agent.current_task_id) == (AgentState.IDLE, None)
    assert await held_batch(db, "held") == "batch-held"
    assert "session launch refused -- ingest_held" in caplog.text
