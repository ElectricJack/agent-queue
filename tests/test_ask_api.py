"""Human ask schemas and every public gateway exclusion boundary agree."""

import pytest
import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from src.api import dependencies as deps
from src.api.auth import RequestScope
from src.api.codegen import API_EXCLUDED, build_category_routers
from src.api.execute import router as execute_router
from src.api.middleware import TokenAuthMiddleware
from src.api.models.ask import ASK_ARGS, RESPONSE_MODELS, AskApplyInputArgs
from src.api.scope import check_command_scope
from src.api.websocket import _FORWARDED_PREFIXES
from src.commands.contracts import CONTRACTS
from src.commands.contracts.ask import GATEWAY_COMMANDS, OPERATOR_COMMANDS
from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS
from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES
from tests.test_ask_discussion import env as env  # noqa: F401
from tests.test_ask_discussion import ask_for
from tests.test_review_commands import env as review_env  # noqa: F401


def test_all_frozen_commands_have_schema_contract_category_and_response():
    definitions = {d["name"]: d for d in _ALL_TOOL_DEFINITIONS}
    for name, args in ASK_ARGS.items():
        assert definitions[name]["input_schema"] == args.model_json_schema()
        assert CONTRACTS.get(name).contract.execution.args_model is args
        assert name in RESPONSE_MODELS and _TOOL_CATEGORIES[name] == "review"
    assert "ask." in _FORWARDED_PREFIXES


def test_cutover_status_contract_is_read_only_and_operator_scoped():
    from src.commands.contracts.models import ReadClause, SideEffectClass

    contract = CONTRACTS.get("ask_cutover_status").contract.execution
    assert contract.side_effect is SideEffectClass.READ
    assert all(isinstance(effect, ReadClause) for effect in contract.effects)
    assert "ask_cutover_status" in OPERATOR_COMMANDS
    assert (
        check_command_scope("ask_cutover_status", {"project_id": "p"}, RequestScope(kind="local"))
        is None
    )


@pytest.mark.parametrize("surface", ["typed", "execute"])
async def test_cutover_status_http_returns_initial_generation(env, monkeypatch, surface):
    from src.asks.delivery import destination

    handler, _ = env
    monkeypatch.setattr(deps, "_command_handler", handler)
    monkeypatch.setattr(deps, "_orchestrator", handler.orchestrator)
    monkeypatch.setattr(deps, "_require_session_token", False)
    app = FastAPI()
    app.include_router(execute_router)
    paths = {}
    for router in build_category_routers():
        app.include_router(router)
        for route in router.routes:
            paths[route.name] = route.path
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            paths["ask_cutover_status"] if surface == "typed" else "/api/execute",
            json={"project_id": "p"}
            if surface == "typed"
            else {
                "command": "ask_cutover_status",
                "args": {"project_id": "p"},
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    if surface == "execute":
        body = body["result"]
    assert body["success"], body
    assert body["result"]["configured_destination"] == destination(handler.config)
    assert body["result"]["destinations"][0]["generation"] == 0


@pytest.mark.parametrize("kind,elevated", [("local", False), ("session", True), ("session", False)])
def test_gateway_tools_cannot_be_called_by_http_or_mcp(kind, elevated):
    scope = RequestScope(kind=kind, elevated=elevated, project_id="p", session_id="s")
    for name in GATEWAY_COMMANDS:
        assert name in API_EXCLUDED and name in DEFAULT_EXCLUDED_COMMANDS
        assert check_command_scope(name, {}, scope)
    if kind == "session":
        for name in OPERATOR_COMMANDS:
            assert check_command_scope(name, {}, scope)


def test_interpreter_cannot_supply_human_identity():
    with pytest.raises(ValidationError):
        AskApplyInputArgs.model_validate(
            {
                "ask_id": "ask-1",
                "input_id": "input-1",
                "expected": {
                    "decision_generation": 1,
                    "feedback_version": 0,
                    "source_fingerprint": "hash",
                },
                "option_key": "approve",
                "interpretation": "Yes",
                "idempotency_key": "k",
                "verified_actor": "human:operator",
            }
        )


async def test_local_command_cannot_forge_gateway_envelope(env):
    handler, _ = env
    from tests.test_ask_intake import envelope

    result = await handler.execute("ask_intake", {"envelope": envelope("yes")})
    assert result["error_code"] == "ask.gateway_only"


@pytest.mark.parametrize("surface", ["typed", "execute"])
async def test_http_discussion_and_stale_receipt_preserve_command_envelope(
    env, monkeypatch, surface
):
    handler, _ = env
    _, ask = await ask_for(handler)
    monkeypatch.setattr(deps, "_command_handler", handler)
    monkeypatch.setattr(deps, "_orchestrator", handler.orchestrator)
    monkeypatch.setattr(deps, "_require_session_token", False)
    app = FastAPI()
    app.include_router(execute_router)
    paths = {}
    for router in build_category_routers():
        app.include_router(router)
        paths.update({route.operation_id: route.path for route in router.routes})
    app.add_middleware(TokenAuthMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def post(command, args):
            if surface == "typed":
                return await client.post(paths[command], json=args)
            return await client.post("/api/execute", json={"command": command, "args": args})

        args = {
            "ask_id": ask["id"],
            "expected": ask["expected"],
            "text": "Explain the design",
            "idempotency_key": "http-discuss",
        }
        response = await post("ask_reply", args)
        assert response.status_code == 200, response.text
        body = response.json()
        result = body["result"] if surface == "execute" else body
        assert result["created"] and result["receipt"]["state"] == "queued"
        shown = await post("ask_show", {"ask_id": ask["id"]})
        assert shown.status_code == 200, shown.text
        stale = await post(
            "ask_reply",
            {
                **args,
                "idempotency_key": "http-stale",
                "expected": {**args["expected"], "decision_generation": 99},
            },
        )
        body = stale.json()
        result = body["details"] if surface == "execute" else body
        assert result["error_code"] == "ask.stale_source", body
        assert "fields" in result
