"""The persisted wait projection survives HTTP and human CLI rendering."""

from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from src.api.codegen import build_category_routers
from src.api.dependencies import get_command_handler
from src.api.models.wait import WaitExplainResponse, WaitGraphResponse
from src.cli.waits import wait
from src.commands.contracts.wait_v2 import WaitExplainValue, WaitGraphValue
from src.database.queries.wait_queries import wait_explain, wait_graph
from src.database.tables import tasks, wait_records
from tests.test_wait_commands import commands as _commands
from tests.test_wait_graph import NOW, env as _env, register

commands = _commands
env = _env


@pytest.mark.parametrize("unknown_age", [False, True])
async def test_ready_unclaimed_scheduler_wait_survives_typed_http(commands, env, unknown_age):
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "a").values(status="READY"))
        await conn.execute(
            update(wait_records)
            .where(wait_records.c.id == receipt["wait_id"])
            .values(claimed="waiting for CI", decision_inputs={"age_unknown": unknown_age})
        )

    async def explain(args, **kwargs):
        async with env.db._engine.connect() as conn:
            return await wait_explain(conn, project_id="p", task_id=args["task_id"], now=NOW + 21)

    async def graph(args, **kwargs):
        async with env.db._engine.connect() as conn:
            return await wait_graph(conn, project_id="p", now=NOW + 21)

    commands.orchestrator.wait_v2_handlers.update(wait_explain=explain, wait_graph=graph)
    commands.config.wait_policy.v2_enabled = False
    app = FastAPI()
    app.dependency_overrides[get_command_handler] = lambda: commands
    for router in build_category_routers():
        if router.prefix == "/api/wait":
            app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/wait/explain", json={"task_id": "a", "project_id": "p"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["task"]["lifecycle"] == "READY"
        assert body["total"] == 1 and body["truncated"] is False
        explanation = body["waits"][0]
        assert explanation["owner"] == {"kind": "scheduler", "id": "a"}
        assert explanation["verified"]["condition"]["task_id"] == "b"
        assert explanation["claimed"] == "waiting for CI"
        assert explanation["age_seconds"] == 21
        assert explanation["age_unknown"] is unknown_age
        assert "waiting for CI" not in str(explanation["verified"])
        assert WaitExplainValue.model_validate({k: body[k] for k in WaitExplainValue.model_fields})

        response = await client.post("/api/wait/graph", json={"project_id": "p"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["projects"][0]["waits"][0]["verified"] == explanation["verified"]
        assert body["projects"][0]["waits"][0]["claimed"] == "waiting for CI"
        assert WaitGraphValue.model_validate({k: body[k] for k in WaitGraphValue.model_fields})


async def test_response_models_preserve_all_persisted_projection_fields(env):
    receipt = await register(env)
    async with env.db._engine.connect() as conn:
        explained = await wait_explain(conn, project_id="p", wait_id=receipt["wait_id"], now=NOW)
        graph = await wait_graph(conn, project_id="p", now=NOW)
    projected = WaitExplainResponse.model_validate(explained).model_dump(by_alias=True)
    for key, value in explained["waits"][0].items():
        assert projected["waits"][0][key] == value, key
    assert WaitGraphResponse.model_validate(graph).projects[0].waits[0].wait_id == receipt["wait_id"]
    assert WaitGraphResponse.model_validate({"graph": graph}).graph.projects[0].project_id == "p"


@pytest.mark.parametrize("command", ["explain", "graph"])
def test_cli_projects_verified_blocker_age_and_admission_without_relabelling_claims(monkeypatch, command):
    import src.cli.waits as mod

    record = {
        "wait_id": "w", "owner": {"kind": "scheduler", "id": "repair"},
        "lifecycle": "READY", "age_unknown": True,
        "verified": {"kind": "resource", "condition": {"resource_key": "pool"}, "facts": {"used": 2, "capacity": 2}},
        "claimed": "CI passed", "sleep": {"decision": "in_place", "reason": "capacity full"},
        "effective_priority": {"base": 100, "value": 20, "donor": "task:delivery"},
        "reservations": [{"state": "pending"}],
        "chain_truncated": True,
    }
    result = {"success": True, "waits": [record], "truncated": True, "total": 2}
    if command == "graph":
        result = {"success": True, "graph": {"projects": [{"project_id": "p", "waits": [record], "total_waits": 2, "truncated": {"waits": True}}], "truncated": True}}
    monkeypatch.setattr(mod, "_execute", lambda *args: result)
    flags = ["--task", "repair"] if command == "explain" else ["--project", "p"]
    rendered = CliRunner().invoke(wait, [command, *flags])
    assert rendered.exit_code == 0, rendered.output
    assert rendered.output.index("Verified evidence:") < rendered.output.index("Agent commentary:")
    assert "age_unknown" in rendered.output and "resource" in rendered.output
    assert "CI passed" in rendered.output and "pending" in rendered.output
    assert "truncated" in rendered.output


def test_cli_invalid_graph_scope_never_dispatches(monkeypatch):
    import src.cli.waits as mod

    dispatch = AsyncMock()
    monkeypatch.setattr(mod, "_execute", dispatch)
    result = CliRunner().invoke(wait, ["graph", "--fleet", "--project", "p"])
    assert result.exit_code != 0
    dispatch.assert_not_called()
