"""Provenance is written in the graph-creation transaction (spec §13)."""

from __future__ import annotations

import json

import pytest

from src.database import Database
from src.models import Project
from src.task_graph.creator import FormulaProvenance, build_plan, create_graph, write_plan
from src.task_graph.parser import parse_graph

PROJECT_ID = "proj"
GRAPH = {"version": 1, "parent": {"title": "Epic"},
         "nodes": [{"key": "a", "title": "A"}, {"key": "b", "title": "B", "needs": [{"on": "a"}]}]}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


def prov(**over):
    base = dict(name="review-and-fix", scope="system", path="formulas/review-and-fix.md",
                vars={"branch": "feat/x"}, chain_sha="ab" * 32, snapshot=GRAPH)
    base.update(over)
    return FormulaProvenance(**base)


async def test_new_container_carries_provenance(db):
    plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
    await write_plan(db, plan, provenance=prov())
    cid = plan.parent_id
    assert await db.get_task_meta(cid, "formula") == "review-and-fix"
    assert await db.get_task_meta(cid, "formula_scope") == "system"
    assert await db.get_task_meta(cid, "formula_path") == "formulas/review-and-fix.md"
    assert json.loads(await db.get_task_meta(cid, "formula_vars")) == {"branch": "feat/x"}
    assert await db.get_task_meta(cid, "formula_chain_sha") == "ab" * 32
    ctx = [c for c in await db.get_task_contexts(cid) if c["type"] == "formula_snapshot"]
    assert len(ctx) == 1 and json.loads(ctx[0]["content"])["nodes"][1]["key"] == "b"
    assert "formula:review-and-fix" in await db.get_task_labels(cid)


async def test_provenance_rolls_back_with_the_graph(db, monkeypatch):
    plan = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)

    async def boom(*a, **k):
        raise RuntimeError("blocked recompute failed")

    monkeypatch.setattr(db, "recompute_blocked", boom)
    with pytest.raises(RuntimeError):
        await write_plan(db, plan, provenance=prov())
    assert await db.get_task(plan.parent_id) is None
    assert await db.get_task_contexts(plan.parent_id) == []


async def test_existing_container_gets_latest_metadata_and_accumulates_snapshots(db):
    first = await build_plan(db, parse_graph(GRAPH), project_id=PROJECT_ID)
    await write_plan(db, first, provenance=prov(chain_sha="11" * 32))
    second = await build_plan(db, parse_graph({"version": 1, "nodes": [{"key": "c", "title": "C"}]}),
                              project_id=PROJECT_ID, parent_id=first.parent_id)
    await write_plan(db, second, provenance=prov(chain_sha="22" * 32))
    assert await db.get_task_meta(first.parent_id, "formula_chain_sha") == "22" * 32
    snaps = [c for c in await db.get_task_contexts(first.parent_id) if c["type"] == "formula_snapshot"]
    assert len(snaps) == 2


async def test_create_graph_report_and_dry_run(db):
    class H:
        pass

    h = H()
    h.db = db
    report = await create_graph(h, parse_graph(GRAPH), project_id=PROJECT_ID, dry_run=True,
                                provenance=prov())
    assert report["dry_run"] is True and report["provenance"]["name"] == "review-and-fix"
    assert "snapshot" not in report["provenance"]
    assert await db.get_task(report["parent_id"]) is None
    report = await create_graph(h, parse_graph(GRAPH), project_id=PROJECT_ID, provenance=prov())
    assert await db.get_task_meta(report["parent_id"], "formula") == "review-and-fix"
