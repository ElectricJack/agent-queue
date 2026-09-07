"""Typed-route request models must keep JSON Schema union types intact.

``update_config``'s ``data`` is declared ``object|array|string|number|
boolean|null`` — replacing a top-level section can legitimately write a
mapping, a list, a scalar, or ``null`` to delete it.  The codegen used to
collapse a ``{"type": [...]}`` union to its first branch, which narrowed
``data`` to ``dict`` and made the typed route answer

    422 {"loc": ["body", "data"], "msg": "Input should be a valid dictionary"}

for every list-valued section.  ``project_roots`` is one of those, so the
dashboard's Settings → Project Roots page could not add a project root at
all.  These tests pin the union so the route accepts each legal shape.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import dependencies as deps
from src.api.codegen import (
    _json_schema_type_to_python,
    _make_input_model,
    _make_route_handler,
)
from src.tools import _ALL_TOOL_DEFINITIONS


def _tool_schema(name: str) -> dict:
    return next(t for t in _ALL_TOOL_DEFINITIONS if t["name"] == name)["input_schema"]


def test_json_schema_union_type_becomes_a_python_union_not_its_first_branch():
    assert _json_schema_type_to_python({"type": ["object", "array", "null"]}) == (
        dict | list | None
    )
    # The common nullable-scalar spelling is unchanged.
    assert _json_schema_type_to_python({"type": ["integer", "null"]}) == int | None
    assert _json_schema_type_to_python({"type": "string"}) is str


def test_update_config_data_accepts_every_declared_branch():
    model = _make_input_model("update_config", _tool_schema("update_config"))
    roots = [{"id": "dev_root", "label": "Local Project Root", "path": "/home/jkern/dev"}]

    assert model(section="project_roots", data=roots).data == roots
    assert model(section="swarm", data={"enabled": True}).data == {"enabled": True}
    assert model(section="swarm", data="text").data == "text"
    assert model(section="swarm", data=True).data is True
    # ``data: null`` is the documented way to delete a section, and it stays
    # required — an omitted ``data`` is still a validation error.
    assert model(section="swarm", data=None).data is None
    with pytest.raises(Exception):
        model(section="swarm")


@pytest.fixture
async def config_route(command_handler_factory, monkeypatch):
    """(client, records) for an app serving the real ``update_config`` route."""
    ch = await command_handler_factory()
    records: list[dict] = []

    async def _cmd_update_config(args):
        records.append(dict(args))
        return {"success": True, "applied": True, "changed": True}

    ch._cmd_update_config = _cmd_update_config

    monkeypatch.setattr(deps, "_command_handler", ch)
    monkeypatch.setattr(deps, "_orchestrator", ch.orchestrator)
    monkeypatch.setattr(deps, "_token_store", None)
    monkeypatch.setattr(deps, "_require_session_token", False)

    app = FastAPI()
    handler = _make_route_handler(
        "update_config", _make_input_model("update_config", _tool_schema("update_config"))
    )
    app.add_api_route("/api/system/update-config", handler, methods=["POST"])

    with TestClient(app) as client:
        yield client, records
    await ch._db.close()


def test_typed_route_accepts_a_list_valued_section(config_route):
    client, records = config_route
    roots = [
        {"id": "dev_root", "label": "Local Project Root", "path": "/home/jkern/dev"},
        {"id": "d_dev_root", "label": "Ext Dev", "path": "/mnt/d/dev"},
    ]

    r = client.post(
        "/api/system/update-config", json={"section": "project_roots", "data": roots}
    )

    assert r.status_code == 200, r.text
    assert records[-1]["section"] == "project_roots"
    assert records[-1]["data"] == roots
