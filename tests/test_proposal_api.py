"""GET /api/proposals/{id} — dashboard ghost-overlay read.

Phase 6 Task 4 (design §8, spec ingestion).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.routers.proposals import build_proposals_router
from src.database import Database
from src.database.queries import proposal_queries
from src.models import Project


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "prop.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


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
