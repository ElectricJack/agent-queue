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
    **{f"knowledge_{name}": "/api/knowledge/" + name.replace("_", "-") for name in (
        "propose", "proposal_show", "proposal_decide", "verify", "authority_grant",
        "authority_revoke", "share", "redact",
    )},
    "knowledge_create": "/api/knowledge/create",
    "knowledge_list": "/api/knowledge/list",
    "knowledge_show": "/api/knowledge/show",
    "knowledge_update": "/api/knowledge/update",
    "knowledge_history": "/api/knowledge/history",
    "knowledge_diff": "/api/knowledge/diff",
    "knowledge_retire": "/api/knowledge/retire",
    "knowledge_restore": "/api/knowledge/restore",
    "knowledge_export": "/api/knowledge/export",
    "record_show": "/api/record/show",
    "record_search": "/api/record/search",
    "record_capabilities": "/api/record/capabilities",
    "record_repair": "/api/record/repair",
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
    "record.hash_divergence",
    "record.integrity_conflict",
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
    assert len(ops) == len(ROUTES)
    assert len(ROUTES) == len(set(ROUTES.values())) == 24


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
        ("record.hash_divergence", 503),
        ("record.integrity_conflict", 409),
    ],
)
def test_documented_status_pins(app, code, status):
    expected = ERROR_STATUS[("record_capabilities", code)]
    assert expected == status


async def test_typed_import_apply_and_resume_preserve_durable_response(app, tmp_path):
    from src.api.models.knowledge import KnowledgeImportResponse
    from src.commands.contracts.inventory import KnowledgeImportArgs
    from src.config import KnowledgeFeatureConfig
    from tests.record_helpers import knowledge_config, seed_project
    from tests.test_knowledge_import import args, manifest

    application, ch, _ = app
    await seed_project(ch._db)
    ch.config.knowledge = knowledge_config(import_apply=KnowledgeFeatureConfig(enabled=True))
    # Config.vault_root is derived from data_dir, keeping all artifacts disposable.
    ch.config.data_dir = str(tmp_path)
    from pathlib import Path

    Path(ch.config.vault_root).mkdir(parents=True, exist_ok=True)
    route = _make_route_handler("knowledge_import", KnowledgeImportArgs)
    application.add_api_route("/api/knowledge/import", route, methods=["POST"],
                             response_model=KnowledgeImportResponse)
    sealed = manifest(count=2)
    payload = args(sealed, limit=1)
    payload.pop("principal")
    payload["operation"] = "apply"
    with TestClient(application) as client:
        first = client.post("/api/knowledge/import", json=payload)
        assert first.status_code == 200, first.text
        assert first.json()["state"] == "applying" and first.json()["counts"]["created"] == 1
        final = client.post("/api/knowledge/import", json={
            "operation": "resume", "project_id": "p", "run_id": first.json()["run_id"],
            "manifest_sha256": sealed.sha256,
        })
        assert final.status_code == 200 and final.json()["state"] == "succeeded", final.text
        assert final.json()["counts"]["created"] == 2
        invalid = client.post("/api/knowledge/import", json={"operation": "apply"})
        assert invalid.status_code == 422
        ch.config.knowledge.import_apply.enabled = False
        refused = client.post("/api/knowledge/import", json=payload)
        assert refused.status_code == 409
        assert refused.json()["error_code"] == "knowledge_import.disabled"
