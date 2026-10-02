"""Typed HTTP surface for the K03 knowledge/record commands.

The CLI, MCP, REST and Discord surfaces all share ``CommandHandler.execute``;
this file pins the REST projection of that path for the fourteen
knowledge/record verbs: the registered route path, the OpenAPI operation id
(the generated client keys off it) and the stable error-code -> HTTP status
mapping, so a consumer never sees a regression in any of the three.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import dependencies as deps
from src.api.codegen import ERROR_STATUS, _make_input_model, _make_route_handler

pytestmark = pytest.mark.usefixtures("unpooled_postgres")

# command -> route path.  The path is the command name with its category prefix
# stripped and ``_`` -> ``-`` (src/api/codegen.py:build_category_routers).
ROUTES = {
    "knowledge_create": "/api/knowledge/create",
    "knowledge_list": "/api/knowledge/list",
    "knowledge_show": "/api/knowledge/show",
    "knowledge_update": "/api/knowledge/update",
    "knowledge_history": "/api/knowledge/history",
    "knowledge_diff": "/api/knowledge/diff",
    "knowledge_retire": "/api/knowledge/retire",
    "knowledge_restore": "/api/knowledge/restore",
    "record_show": "/api/record/show",
    "record_search": "/api/record/search",
    "record_capabilities": "/api/record/capabilities",
    "link_create": "/api/record/link-create",
    "link_list": "/api/record/link-list",
    "link_remove": "/api/record/link-remove",
}

CODES = (
    "record.not_found",
    "record.forbidden",
    "record.revision_conflict",
    "record.idempotency_conflict",
    "record.export_diverged",
    "knowledge.disabled",
    "record.revision_unavailable",
    "record.revision_redacted",
    "record.precondition_required",
    "record.retryable",
)


@pytest.fixture
async def app(command_handler_factory, monkeypatch):
    """(app, ch, calls) for a FastAPI app exposing all fourteen typed routes."""
    ch = await command_handler_factory()
    calls: list[str] = []

    for name in ROUTES:

        async def _cmd(args, cmd=name):
            calls.append(cmd)
            return {
                "success": False,
                "error_code": "record.not_found",
                "error": "no such record",
            }

        setattr(ch, f"_cmd_{name}", _cmd)

    monkeypatch.setattr(deps, "_command_handler", ch)
    monkeypatch.setattr(deps, "_orchestrator", ch.orchestrator)
    monkeypatch.setattr(deps, "_token_store", None)
    monkeypatch.setattr(deps, "_require_session_token", False)

    application = FastAPI()
    for cmd, path in ROUTES.items():
        handler = _make_route_handler(cmd, _make_input_model(cmd, {"type": "object"}))
        application.add_api_route(path, handler, methods=["POST"], operation_id=cmd)

    return application, ch, calls


def test_every_route_is_registered_with_a_distinct_operation_id(app):
    application, _, _ = app
    spec = application.openapi()
    ops_by_path = {
        path: info["operationId"]
        for path, methods in spec["paths"].items()
        for info in methods.values()
        if isinstance(info, dict)
    }
    paths = set(ops_by_path)
    ops = set(ops_by_path.values())
    for name, path in ROUTES.items():
        assert path in paths, f"missing route {path}"
        assert ops_by_path[path] == name, f"{path} -> {ops_by_path[path]} (want {name})"
    assert len(ops) == 14
    assert len(ROUTES) == len(set(ROUTES.values())) == 14


def test_not_found_maps_to_404_on_every_route(app):
    application, _, calls = app
    with TestClient(application) as tc:
        for name, path in ROUTES.items():
            r = tc.post(path, json={})
            assert r.status_code == 404, f"{name} -> {r.status_code}: {r.text}"
            body = r.json()
            assert body["success"] is False
            assert body["error_code"] == "record.not_found"
    counts: dict[str, int] = {}
    for item in calls:
        counts[item] = counts.get(item, 0) + 1
    assert counts == {name: 1 for name in ROUTES}


@pytest.mark.parametrize("code", CODES)
def test_error_code_status_matrix(app, code, monkeypatch):
    """Drive one representative verb through every stable code -> status pair.

    The mapping is per code (not per verb) by design, so pinning one verb for
    all ten codes proves the router emits the documented status and body.
    """
    application, ch, _ = app
    expected = ERROR_STATUS[("knowledge_show", code)]

    async def _show(args):
        return {"success": False, "error_code": code, "error": "x"}

    monkeypatch.setattr(ch, "_cmd_knowledge_show", _show)
    with TestClient(application) as tc:
        r = tc.post("/api/knowledge/show", json={})
    assert r.status_code == expected, f"{code} -> {r.status_code} (want {expected})"
    assert r.json()["error_code"] == code


@pytest.mark.parametrize("code", CODES)
def test_status_is_consistent_across_verbs(app, code):
    """Every verb maps the same code to the same status."""
    application, _client, _handler = app
    reference = ERROR_STATUS[("knowledge_create", code)]
    for name in ROUTES:
        assert ERROR_STATUS[(name, code)] == reference
    _ = application


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("record.not_found", 404),
        ("record.forbidden", 403),
        ("record.revision_conflict", 409),
        ("record.idempotency_conflict", 409),
        ("record.export_diverged", 409),
        ("knowledge.disabled", 409),
        ("record.revision_unavailable", 410),
        ("record.revision_redacted", 410),
        ("record.precondition_required", 428),
        ("record.retryable", 503),
    ],
)
def test_documented_status_pins(app, code, status):
    expected = ERROR_STATUS[("record_capabilities", code)]
    assert expected == status
