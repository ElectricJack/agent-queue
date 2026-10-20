"""GET /api/proposals/{id} — dashboard ghost-overlay read.

Phase 6 Task 4 (design §8, spec ingestion).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from src.api.auth import RequestScope
from src.api.models.task import TaskDetail
from src.api.routers.proposals import build_proposals_router
from src.database.queries import ingest_batch_queries as batches
from src.database.queries import proposal_queries
from src.database.tables import ingest_batches
from src.models import Project, Task


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("prop.db")
    await database.create_project(Project(id="p1", name="P1"))
    yield database


@pytest.fixture
def client_factory(db):
    def _make() -> AsyncClient:
        app = FastAPI()
        app.include_router(build_proposals_router(db=db))
        return AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        )

    return _make


async def test_get_proposal_404_when_missing(client_factory):
    async with client_factory() as ac:
        r = await ac.get("/api/proposals/prop-doesnotexist")
    assert r.status_code == 404


async def test_get_proposal_returns_shape(db, client_factory):
    payload = {
        "tasks": [{"tempId": "a", "title": "A", "description": ""}],
        "edges": [],
    }
    prop_id = await proposal_queries.insert_proposal(
        db, project_id="p1", source="spec:foo", payload=payload
    )
    await proposal_queries.update_proposal(db, prop_id, status="ready")

    async with client_factory() as ac:
        resp = await ac.get(f"/api/proposals/{prop_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["proposal_id"] == prop_id
    assert body["project_id"] == "p1"
    assert body["source"] == "spec:foo"
    assert body["status"] == "ready"
    assert body["tasks"] == payload["tasks"]
    assert body["edges"] == []


async def test_get_proposal_exposes_complete_review_and_receipt(db, client_factory):
    payload = {
        "edits": [{"task_id": "existing", "action": "pause"}],
        "remove_edges": [{"from": "existing", "to": "old", "dep_type": "blocks"}],
        "comments": [{"task_id": "existing", "body": "Reviewed finding"}],
        "diff": {"tasks": [{"task_id": "existing", "before": {"status": "READY"},
                            "after": {"status": "PAUSED"}}]},
        "receipt": {"task_ids": [], "edited_task_ids": ["existing"]},
        "expected": {"versions": {"existing": "12345"}},
    }
    prop_id = await proposal_queries.insert_proposal(
        db, project_id="p1", source="change:test", payload=payload, status="committed",
    )
    async with client_factory() as client:
        response = await client.get(f"/api/proposals/{prop_id}")
    assert response.status_code == 200
    body = response.json()
    for key in ("edits", "remove_edges", "comments", "diff", "receipt"):
        assert body[key] == payload[key]
    assert "expected" not in body


@pytest.fixture
async def held_batch(db):
    batch_id = await proposal_queries.insert_proposal(
        db, project_id="p1", source="spec:approved", payload={"diff": {"initial": True}},
        status="held",
    )
    await db.create_task(Task(id="member", project_id="p1", title="Member", description="Do it"))
    async with db._engine.begin() as conn:
        await batches.create_batch(
            conn, batch_id=batch_id, project_id="p1", source_review_id="rev-source",
            source_revision=2, source_content_sha256="source-sha", decision_id="decision",
            lineage={"lineage_kind": "resolved", "design_review_id": "rev-design",
                     "design_revision": 3, "design_content_sha256": "design-sha"},
            idempotency_key="source-key", check_results={"passed": True},
        )
        await batches.add_member(
            conn, batch_id, task_id="member", member_key="leaf", role="leaf",
            section_anchor="Scope", primary_outcome="Keep scope", created_revision=1,
        )
        await batches.assign_reviewer(conn, batch_id, reviewer_task_id="reviewer")
        await batches.store_live_digest(conn, batch_id)
    return batch_id


async def test_get_batch_returns_durable_review_evidence(db, client_factory, held_batch):
    async with db._engine.begin() as conn:
        batch = await batches.get_batch(conn, held_batch)
        receipt = await batches.record_receipt(
            conn, held_batch, structure_revision=1, structure_digest=batch["structure_digest"],
            reviewer_task_id="reviewer", reviewer_session_id="session", passing=True,
            section_verdicts={"Scope": {"verdict": "fixed", "rationale": "Kept constraint"}},
            reverse_coverage_verdict="faithful", walkthrough_verdict="faithful",
            structural_findings={"findings": [{"code": "size", "disposition": "fixed"}],
                                 "lineage": {"source_revision": 2}},
        )
        await batches.set_state(conn, held_batch, "reviewing")
        await batches.set_check_results(conn, held_batch, {"last_diff": {"edited": ["member"]}})

    async with client_factory() as client:
        response = await client.get(f"/api/projects/p1/batches/{held_batch}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "reviewing"
    assert body["source"] == "spec:approved"
    assert body["source_revision"] == 2
    assert body["lineage"]["design_review_id"] == "rev-design"
    assert body["members"] == {"leaf": "member"}
    assert body["membership"][0]["section_anchor"] == "Scope"
    assert body["reviewer_task_id"] == "reviewer"
    assert body["section_verdicts"]["Scope"]["verdict"] == "fixed"
    assert body["reverse_coverage_verdict"] == body["walkthrough_verdict"] == "faithful"
    assert body["structural_findings"] == [{"code": "size", "disposition": "fixed"}]
    assert body["diff"] == {"edited": ["member"]}
    assert body["receipts"] == [receipt["receipt"]]
    assert body["release_receipt"] is None


@pytest.mark.parametrize("stale_digest", [False, True])
async def test_get_batch_keeps_stale_receipts_out_of_current_verdicts(
    db, client_factory, held_batch, stale_digest,
):
    async with db._engine.begin() as conn:
        batch = await batches.get_batch(conn, held_batch)
        result = await batches.record_receipt(
            conn, held_batch, structure_revision=1, structure_digest=batch["structure_digest"],
            reviewer_task_id="reviewer", reviewer_session_id="session", passing=True,
            section_verdicts={"Scope": "faithful"}, reverse_coverage_verdict="faithful",
            walkthrough_verdict="faithful", structural_findings=[],
        )
        if stale_digest:
            await conn.execute(update(ingest_batches).where(
                ingest_batches.c.batch_id == held_batch,
            ).values(structure_digest="changed"))
        else:
            await batches.bump_revision(conn, held_batch, 1)
    async with client_factory() as client:
        response = await client.get(f"/api/projects/p1/batches/{held_batch}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["receipts"] == [result["receipt"]]
    assert body["section_verdicts"] == {}
    assert body["structural_findings"] == []
    assert body["reverse_coverage_verdict"] is None


async def test_get_design_batch_has_server_release_receipt(db, client_factory):
    batch_id = await proposal_queries.insert_proposal(
        db, project_id="p1", source="spec:design", payload={}, status="committed",
    )
    receipt = {"kind": "design_authoring_shape"}
    async with db._engine.begin() as conn:
        await batches.create_batch(
            conn, batch_id=batch_id, project_id="p1", source_review_id="rev-design",
            source_revision=1, source_content_sha256="sha", decision_id="decision",
            lineage={"lineage_kind": "none"}, idempotency_key="design", state="released",
            release_actor="service:design_authoring_shape", release_receipt=receipt,
        )
    async with client_factory() as client:
        response = await client.get(f"/api/projects/p1/batches/{batch_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "released"
    assert body["lineage"]["lineage_kind"] == "none"
    assert body["release_receipt"] == receipt
    assert body["reviewer_task_id"] is None
    assert body["receipts"] == []


async def test_get_batch_missing_or_wrong_project_is_404(client_factory, held_batch):
    async with client_factory() as client:
        for path in ("/api/projects/p1/batches/missing",
                     f"/api/projects/p2/batches/{held_batch}"):
            assert (await client.get(path)).status_code == 404


@pytest.mark.parametrize("project,elevated,status", [
    ("p1", False, 200), ("p2", False, 403), (None, False, 403),
    ("p2", True, 403), (None, True, 200),
])
async def test_get_batch_enforces_session_project(db, held_batch, project, elevated, status):
    app = FastAPI()

    @app.middleware("http")
    async def attach_scope(request, call_next):
        request.state.scope = RequestScope(kind="session", project_id=project, elevated=elevated)
        return await call_next(request)

    app.include_router(build_proposals_router(db=db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get(f"/api/projects/p1/batches/{held_batch}")
    assert response.status_code == status


async def test_task_detail_exposes_batch_and_retains_release_history(
    db, held_batch, command_handler_factory,
):
    ch = await command_handler_factory()
    await ch._db.close()
    ch.orchestrator.db = db
    ch._db = db
    result = await ch.execute("get_task", {"task_id": "member"})
    detail = TaskDetail.model_validate(result)
    assert detail.held_batch.batch_id == held_batch
    assert detail.held_batch.state == "held"
    assert detail.held_batch.lineage["design_revision"] == 3
    async with db._engine.begin() as conn:
        batch = await batches.get_batch(conn, held_batch)
        released = await batches.release(
            conn, held_batch, expected_revision=1, expected_digest=batch["structure_digest"],
            actor="supervisor", release_receipt={"task_ids": ["member"]},
        )
        assert released["success"]
    result = await ch.execute("get_task", {"task_id": "member"})
    assert TaskDetail.model_validate(result).held_batch.state == "released"
    await db.create_task(Task(id="ordinary", project_id="p1", title="Ordinary", description=""))
    result = await ch.execute("get_task", {"task_id": "ordinary"})
    assert TaskDetail.model_validate(result).held_batch is None
