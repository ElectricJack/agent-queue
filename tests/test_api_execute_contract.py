"""Error-envelope parity between /api/execute and the typed routes (plan 10).

Both HTTP command surfaces project the same CommandHandler results, but with
different documented envelopes: the generic endpoint always answers 200 with
``{ok, result|error[, details]}`` while typed routes map errors onto HTTP
statuses (422 command error, 409 revision conflict).  These tests pin both
projections for the same handler results so a consumer switching surfaces
never loses actionable fields the contract promises it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import dependencies as deps
from src.api.codegen import _make_input_model, _make_route_handler
from src.api.execute import router as execute_router


GRAPH_ERROR = {
    "error": "graph validation failed with 2 error(s)",
    "errors": [
        {"node": "a", "field": "profile_id", "message": "unknown profile"},
        {"node": "b", "field": "depends_on", "message": "cycle a->b->a"},
    ],
    "warnings": [{"node": "c", "message": "no intelligence_class"}],
}

CONFLICT = {
    "error": "intelligence class changed since it was loaded",
    "error_code": "revision_conflict",
    "current_revision": 7,
}

ONBOARDING_ERROR = {
    "success": False,
    "error": "Destination already exists",
    "error_code": "destination_conflict",
    "phase": "preflight",
    "field_errors": [{"field": "relativePath", "message": "Choose another directory"}],
}


@pytest.fixture
async def surfaces(command_handler_factory, monkeypatch):
    """(client, records) for an app serving /api/execute plus typed routes."""
    ch = await command_handler_factory()
    records: list[tuple[str, dict]] = []

    async def _cmd_create_task_graph(args):
        records.append(("create_task_graph", dict(args)))
        return dict(GRAPH_ERROR)

    async def _cmd_edit_intelligence_class(args):
        records.append(("edit_intelligence_class", dict(args)))
        return dict(CONFLICT)

    async def _cmd_onboard_project(args):
        records.append(("onboard_project", dict(args)))
        return dict(ONBOARDING_ERROR)

    ch._cmd_create_task_graph = _cmd_create_task_graph
    ch._cmd_edit_intelligence_class = _cmd_edit_intelligence_class
    ch._cmd_onboard_project = _cmd_onboard_project

    monkeypatch.setattr(deps, "_command_handler", ch)
    monkeypatch.setattr(deps, "_orchestrator", ch.orchestrator)
    monkeypatch.setattr(deps, "_token_store", None)
    monkeypatch.setattr(deps, "_require_session_token", False)

    app = FastAPI()
    app.include_router(execute_router)
    for cmd, path, schema in (
        (
            "create_task_graph",
            "/api/task/create-graph",
            {"type": "object", "properties": {"graph": {"type": "object"}}},
        ),
        (
            "edit_intelligence_class",
            "/api/system/edit-intelligence-class",
            {
                "type": "object",
                "properties": {
                    "class_id": {"type": "string"},
                    "revision": {"type": "integer"},
                },
            },
        ),
        (
            "onboard_project",
            "/api/project/onboard",
            {
                "type": "object",
                "required": ["request_id", "source_mode", "root_id", "relative_path", "project_name", "project_id"],
                "properties": {
                    "request_id": {"type": "string"},
                    "source_mode": {"type": "string"},
                    "root_id": {"type": "string"},
                    "relative_path": {"type": "string"},
                    "project_name": {"type": "string"},
                    "project_id": {"type": "string"},
                },
            },
        ),
    ):
        handler = _make_route_handler(cmd, _make_input_model(cmd, schema))
        app.add_api_route(path, handler, methods=["POST"])

    with TestClient(app) as client:
        yield client, records
    await ch._db.close()


def test_generic_and_typed_routes_preserve_error_details_and_status_contracts(surfaces):
    client, records = surfaces

    # Structured command error — generic keeps every finding under details.
    r = client.post(
        "/api/execute",
        json={"command": "create_task_graph", "args": {"graph": {}}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == GRAPH_ERROR["error"]
    assert body["details"]["errors"] == GRAPH_ERROR["errors"]
    assert body["details"]["warnings"] == GRAPH_ERROR["warnings"]

    # Same handler result through the typed route — documented 422 shape.
    r = client.post("/api/task/create-graph", json={"graph": {}})
    assert r.status_code == 422, r.text
    assert r.json() == {"error": GRAPH_ERROR["error"]}

    # Revision conflict — typed surface answers a documented 409 carrying
    # the fields the editor needs to retry (error_code, current_revision).
    r = client.post(
        "/api/system/edit-intelligence-class",
        json={"class_id": "deep-high", "revision": 3},
    )
    assert r.status_code == 409, r.text
    assert r.json() == {
        "error": CONFLICT["error"],
        "error_code": "revision_conflict",
        "current_revision": 7,
    }

    # Onboarding is the exception to the generic typed-route error envelope:
    # its wizard needs code/phase/field details to preserve values and retry.
    r = client.post(
        "/api/project/onboard",
        json={
            "request_id": "request-1",
            "source_mode": "init",
            "root_id": "dev",
            "relative_path": "widgets",
            "project_name": "Widgets",
            "project_id": "widgets",
        },
    )
    assert r.status_code == 422, r.text
    assert r.json() == ONBOARDING_ERROR

    # Generic surface: same conflict stays a 200 ok:false envelope with the
    # full payload under details — no field loss for CLI consumers.
    r = client.post(
        "/api/execute",
        json={"command": "edit_intelligence_class", "args": {"class_id": "deep-high"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == CONFLICT["error"]
    assert body["details"] == {"error_code": "revision_conflict", "current_revision": 7}

    # Both surfaces actually reached the handler for each call above.
    assert [name for name, _ in records] == [
        "create_task_graph",
        "create_task_graph",
        "edit_intelligence_class",
        "onboard_project",
        "edit_intelligence_class",
    ]

    # Excluded-command denial: /api/execute must refuse API_EXCLUDED
    # commands (run_command above all) instead of back-dooring them.
    records.clear()
    r = client.post(
        "/api/execute", json={"command": "run_command", "args": {"command": "id"}},
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["ok"] is False
    assert "not available over the API" in body["error"]
    assert records == []
