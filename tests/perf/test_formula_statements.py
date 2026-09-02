"""formula_cook is write_plan + 3 statements (spec §15); formula_show writes nothing."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from sqlalchemy import event

from src.commands.handler import CommandHandler
from src.task_graph.formulas import FormulaRegistry, load_from_vault
from tests.perf.test_hierarchy_statements import PLAN_NODES, PROJECT_ID, _graph, count_statements, db, seed_scale  # noqa: F401

pytestmark = pytest.mark.perf


def _formula_text(n):
    doc = _graph(n)
    for node in doc["nodes"]:
        node["title"] = node["title"] + " on {branch}"
    doc["parent"]["title"] = "Epic {branch}"
    return "---\nname: big\nvars:\n  branch: {required: true}\n---\n```aq-graph\n" + yaml.safe_dump(doc) + "```\n"


def _plain_graph(n, branch="b"):
    """The same document ``_formula_text`` produces, vars already substituted --
    the plain-``create_task_graph`` equivalent of cooking ``big`` with
    ``branch=<branch>``."""
    doc = _graph(n)
    for node in doc["nodes"]:
        node["title"] = node["title"] + f" on {branch}"
    doc["parent"]["title"] = f"Epic {branch}"
    return doc


@pytest.fixture
async def handler(db, tmp_path):  # noqa: F811 -- fixture param shadows the imported fixture
    vault = tmp_path / "vault"
    (vault / "formulas").mkdir(parents=True)
    (vault / "formulas" / "big.md").write_text(_formula_text(PLAN_NODES))
    reg = FormulaRegistry()
    assert load_from_vault(reg, str(vault)) == []
    orch = MagicMock()
    orch.db = db
    orch.bus.emit = AsyncMock()
    orch._emit_notify = AsyncMock()
    orch.formula_registry = reg
    config = MagicMock()
    config.vault_root = str(vault)
    h = CommandHandler(orch, config)
    h._active_project_id = None
    return h


async def test_formula_cook_budget(handler, db):  # noqa: F811
    await seed_scale(db)
    async with count_statements(db) as c:
        started = time.perf_counter()
        res = await handler._cmd_formula_cook({"name": "big", "project_id": PROJECT_ID,
                                               "vars": {"branch": "b"}})
        elapsed = time.perf_counter() - started
    assert res["success"], res
    budget = 3 * PLAN_NODES + 28  # write_plan (3N+20) + 3 provenance writes + ≤5 validate_graph reads
    print(f"\nformula_cook({PLAN_NODES}) : {c['n']} statements, {elapsed:.2f}s (budget {budget})")
    assert c["n"] <= budget and elapsed <= 4.0


async def test_formula_cook_overhead_over_plain_create_task_graph(handler, db):  # noqa: F811
    """``formula_cook`` does everything ``create_task_graph`` does, plus provenance.

    Both paths resolve/parse/validate the same 200-node document and then
    call ``create_graph`` — ``formula_cook`` additionally writes a
    :class:`FormulaProvenance` (3 extra writes: ``task_metadata`` rows for
    ``formula``/``formula_path``/``formula_vars``/``formula_scope``/
    ``formula_chain_sha`` collapse into ``write_plan``'s existing metadata
    write path, plus the ``formula_snapshot`` context row and the
    ``formula:<name>`` label — see ``write_plan``'s provenance branch) over
    plain ``create_task_graph`` on the identical document.  Measured on this
    branch, 2026-08-29: plain ``create_task_graph`` = 230 statements,
    ``formula_cook`` = 233 statements — a 3-statement gap, exactly the
    provenance writes (cook's extra `resolve_formula`/`parse_graph`/
    `validate_graph` work is in-memory over the already-loaded registry and
    adds no statements). Budget is the measured gap exactly (+3) — any
    slack beyond that would mask a real regression.
    """
    await seed_scale(db)
    async with count_statements(db) as c_plain:
        plain = await handler._cmd_create_task_graph(
            {"graph": _plain_graph(PLAN_NODES), "project_id": PROJECT_ID}
        )
    assert "error" not in plain, plain
    async with count_statements(db) as c_cook:
        cook = await handler._cmd_formula_cook(
            {"name": "big", "project_id": PROJECT_ID, "vars": {"branch": "b"}}
        )
    assert cook["success"], cook
    print(
        f"\ncreate_task_graph({PLAN_NODES}) : {c_plain['n']} statements | "
        f"formula_cook({PLAN_NODES}) : {c_cook['n']} statements"
    )
    assert c_cook["n"] <= c_plain["n"] + 3


async def test_formula_show_is_read_only(handler, db):  # noqa: F811
    writes = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement[:80])

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        async with count_statements(db) as c:
            res = await handler._cmd_formula_show({"name": "big", "project_id": PROJECT_ID,
                                                   "vars": {"branch": "b"}})
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)
    assert res["success"], res
    assert writes == [] and c["n"] <= 10  # validate_graph reads only (profiles, needs)
