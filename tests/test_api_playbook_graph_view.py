"""POST /api/playbook/graph-view — the typed route must preserve the nested
graph-view payload produced by ``build_graph_view``.

The declared response model used to be a flat ``{playbook_id, nodes, edges,
direction, overlays}`` shape while the command returns nested ``playbook`` /
``graph`` / ``layout`` / ``legend`` keys, so Pydantic serialization silently
dropped the whole graph.  These tests pin the real contract (design spec
§2.1 and §4) at the HTTP boundary.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import dependencies as deps
from src.api.codegen import build_category_routers
from src.playbooks.models import (
    CompiledPlaybook,
    LlmConfig,
    PlaybookNode,
    PlaybookTransition,
)


DASHBOARD_REQUEST = {
    "playbook_id": "review-pipeline",
    "direction": "TD",
    "show_prompts": True,
    "include_live_state": False,
    "include_metrics": False,
    "include_history": False,
}


def _compiled_playbook() -> CompiledPlaybook:
    return CompiledPlaybook(
        id="review-pipeline",
        version=4,
        source_hash="abc123",
        triggers=["task.completed"],
        scope="project",
        compiled_at="2026-08-31T00:00:00Z",
        nodes={
            "analyse": PlaybookNode(
                prompt="Analyse the diff.\n\nCite file:line for every finding.\n",
                entry=True,
                transitions=[
                    PlaybookTransition(goto="escalate", when="findings exist"),
                    PlaybookTransition(goto="done", otherwise=True),
                ],
                timeout_seconds=300,
                pause_timeout_seconds=1800,
                on_timeout="done",
                llm_config=LlmConfig(provider="anthropic", model="claude-opus-5"),
                transition_llm_config=LlmConfig(model="claude-haiku-4-5"),
                for_each={"items": "files", "as": "file"},
                output={"schema": {"verdict": "string"}},
                action={"command": "task_comment"},
            ),
            "escalate": PlaybookNode(
                prompt="Ask a human to review", wait_for_human=True, goto="done"
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


class _FakePlaybookManager:
    def __init__(self, playbooks: dict[str, CompiledPlaybook]):
        self._playbooks = playbooks

    def get_playbook(self, playbook_id: str):
        return self._playbooks.get(playbook_id)


@pytest.fixture
async def client(command_handler_factory, monkeypatch):
    ch = await command_handler_factory()
    ch.config.playbooks.enabled = True
    ch.orchestrator.playbook_manager = _FakePlaybookManager(
        {"review-pipeline": _compiled_playbook(),
         "empty-playbook": CompiledPlaybook(
             id="empty-playbook", version=1, source_hash="x",
             triggers=["test"], scope="system", nodes={},
         )}
    )

    monkeypatch.setattr(deps, "_command_handler", ch)
    monkeypatch.setattr(deps, "_orchestrator", ch.orchestrator)
    monkeypatch.setattr(deps, "_token_store", None)
    monkeypatch.setattr(deps, "_require_session_token", False)

    # Use the real generated routers so this test pins the wiring the daemon
    # actually serves (route, declared response model, serialization options).
    app = FastAPI()
    for router in build_category_routers():
        app.include_router(router)
    # FastAPI 0.141 keeps included routers as lazy wrappers in ``app.routes``;
    # older versions flatten their contained routes.  Check both layouts so
    # this remains a route-generation assertion rather than a version-specific
    # implementation detail.
    route_paths = {
        getattr(route, "path", "")
        for route in app.routes
    }
    route_paths.update(
        nested.path
        for route in app.routes
        for router in (getattr(route, "original_router", None),)
        if router is not None
        for nested in router.routes
    )
    assert "/api/playbook/graph-view" in route_paths, "playbook graph-view route is not generated"

    with TestClient(app) as c:
        yield c
    await ch._db.close()


def test_response_preserves_nested_playbook_graph_layout_and_legend(client):
    r = client.post("/api/playbook/graph-view", json=DASHBOARD_REQUEST)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["success"] is True

    assert body["playbook"] == {
        "id": "review-pipeline",
        "version": 4,
        "scope": "project",
        "triggers": [{"event_type": "task.completed"}],
        "node_count": 3,
        "compiled_at": "2026-08-31T00:00:00Z",
    }

    node_ids = [n["id"] for n in body["graph"]["nodes"]]
    assert sorted(node_ids) == ["analyse", "done", "escalate"]

    edges = {(e["source"], e["target"], e["edge_type"]) for e in body["graph"]["edges"]}
    assert ("analyse", "escalate", "condition") in edges
    assert ("analyse", "done", "otherwise") in edges
    assert ("escalate", "done", "goto") in edges
    assert all(isinstance(e["label"], str) for e in body["graph"]["edges"])

    assert body["layout"]["direction"] == "TD"
    assert set(body["layout"]["grid_positions"]) == {"analyse", "escalate", "done"}
    for pos in body["layout"]["grid_positions"].values():
        assert set(pos) == {"x", "y"}

    assert "node_types" in body["legend"]


def test_response_preserves_full_compiled_node_details(client):
    r = client.post("/api/playbook/graph-view", json=DASHBOARD_REQUEST)
    assert r.status_code == 200, r.text
    nodes = {n["id"]: n for n in r.json()["graph"]["nodes"]}

    expected = _compiled_playbook().nodes
    for nid, node in expected.items():
        assert nodes[nid]["details"] == node.to_dict()

    analyse = nodes["analyse"]
    assert analyse["type"] == "entry+decision"
    assert analyse["entry"] is True
    assert analyse["out_degree"] == 2
    assert analyse["details"]["prompt"] == expected["analyse"].prompt
    assert analyse["details"]["transitions"] == [
        {"goto": "escalate", "when": "findings exist"},
        {"goto": "done", "otherwise": True},
    ]
    assert analyse["details"]["llm_config"] == {
        "provider": "anthropic", "model": "claude-opus-5"
    }
    assert analyse["details"]["transition_llm_config"] == {"model": "claude-haiku-4-5"}
    assert analyse["details"]["for_each"] == {"items": "files", "as": "file"}
    assert analyse["details"]["output"] == {"schema": {"verdict": "string"}}
    assert analyse["details"]["action"] == {"command": "task_comment"}
    assert nodes["escalate"]["details"]["wait_for_human"] is True
    assert nodes["done"]["details"] == {"terminal": True}


def test_empty_playbook_keeps_top_level_shape(client):
    r = client.post(
        "/api/playbook/graph-view", json={**DASHBOARD_REQUEST, "playbook_id": "empty-playbook"}
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["success"] is True
    assert body["playbook"]["id"] == "empty-playbook"
    assert body["playbook"]["node_count"] == 0
    assert body["graph"] == {"nodes": [], "edges": []}
    assert body["layout"] == {"direction": "TD", "grid_positions": {}}
    assert "node_types" in body["legend"]


def test_unknown_playbook_still_returns_the_command_not_found_error(client):
    r = client.post("/api/playbook/graph-view", json={**DASHBOARD_REQUEST, "playbook_id": "nope"})
    assert r.status_code == 422, r.text
    assert "not found" in r.json()["error"]


def test_invalid_direction_still_returns_the_command_validation_error(client):
    r = client.post("/api/playbook/graph-view", json={**DASHBOARD_REQUEST, "direction": "XY"})
    assert r.status_code == 422, r.text
    assert "Invalid direction" in r.json()["error"]


def test_missing_playbook_id_is_rejected_before_the_command_runs(client):
    r = client.post("/api/playbook/graph-view", json={"direction": "TD"})
    assert r.status_code == 422, r.text
    assert "playbook_id" in r.text


def test_openapi_edge_type_enum_includes_pipeline_outcomes(client):
    edge_type = client.app.openapi()["components"]["schemas"]["PlaybookGraphEdge"][
        "properties"
    ]["edge_type"]

    assert set(edge_type["enum"]) == {
        "goto", "condition", "otherwise", "timeout", "success", "failure"
    }
