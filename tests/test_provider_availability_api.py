"""``/api/providers/availability`` + ``/state`` + ``/recheck`` (provider-failover D20).

The routes run the ``provider_*`` commands through the CommandHandler under
the request's scope, so these tests pin the HTTP shape -- status codes, the
fields the dashboard cards read, and that a task-scoped worker token is
refused -- rather than re-testing the commands.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.api.auth import RequestScope
from src.api.providers import build_providers_router
from src.providers.availability import STARTUP_DIALOG


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    h.orchestrator.provider_availability._probe_impl = AsyncMock(return_value="cannot_tell")
    return h


def _client(handler, scope: RequestScope | None = None) -> AsyncClient:
    app = FastAPI()
    if scope is not None:

        @app.middleware("http")
        async def _scoped(request: Request, call_next):
            request.state.scope = scope
            return await call_next(request)

    app.include_router(
        build_providers_router(db=handler.db, config=handler.config, command_handler=handler)
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _trip(handler) -> None:
    service = handler.orchestrator.provider_availability
    for _ in range(2):
        await service.record("codex", STARTUP_DIALOG, "auth", project_id="p1")
    await service.wait_for_probes()


async def test_availability_lists_what_the_cards_need(handler) -> None:
    await _trip(handler)
    async with _client(handler) as client:
        response = await client.get("/api/providers/availability")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "enforce"
    codex = {row["provider"]: row for row in body["providers"]}["codex"]
    for field in (
        "state",
        "reason",
        "since",
        "until",
        "override",
        "vendor",
        "held",
        "last_success_at",
        "remediation",
    ):
        assert field in codex
    assert codex["state"] == "unauthenticated"
    assert codex["half"] == "unavailable"


async def test_availability_filters_by_provider_or_vendor(handler) -> None:
    await _trip(handler)
    async with _client(handler) as client:
        one = await client.get("/api/providers/availability", params={"provider": "openai"})
        missing = await client.get("/api/providers/availability", params={"provider": "nope"})
    assert [row["provider"] for row in one.json()["providers"]] == ["codex"]
    assert missing.status_code == 404


async def test_state_sets_and_clears_an_override(handler) -> None:
    await _trip(handler)
    async with _client(handler) as client:
        set_ = await client.post(
            "/api/providers/codex/state",
            json={"state": "disabled", "for": "1h", "reason": "rotating"},
        )
        assert set_.status_code == 200, set_.text
        body = set_.json()
        assert body["state"] == "disabled"
        assert body["status"]["override"]["until"] == pytest.approx(time.time() + 3600, abs=10)
        cleared = await client.post("/api/providers/codex/state", json={"state": "auto"})
        assert cleared.status_code == 200
        assert cleared.json()["status"]["override"] is None


async def test_state_rejects_an_invalid_override(handler) -> None:
    await _trip(handler)
    async with _client(handler) as client:
        response = await client.post(
            "/api/providers/codex/state", json={"state": "available", "no_expiry": True,
                                                 "reason": "x"}
        )
    assert response.status_code == 400
    assert "always expires" in response.json()["error"]


async def test_recheck_runs_the_probe(handler) -> None:
    await _trip(handler)
    handler.orchestrator.provider_availability._probe_impl = AsyncMock(
        return_value="authenticated"
    )
    async with _client(handler) as client:
        response = await client.post("/api/providers/codex/recheck")
    assert response.status_code == 200
    body = response.json()
    assert body["probe"] == "authenticated"
    assert body["state"] == "degraded"
    assert body["status"]["probation"] is True


async def test_a_worker_token_is_refused_on_every_route(handler) -> None:
    await _trip(handler)
    worker = RequestScope(kind="session", session_id="s1", task_id="t1", project_id="p1")
    async with _client(handler, worker) as client:
        responses = [
            await client.get("/api/providers/availability"),
            await client.post(
                "/api/providers/codex/state", json={"state": "disabled", "reason": "x"}
            ),
            await client.post("/api/providers/codex/recheck"),
        ]
    assert [r.status_code for r in responses] == [403, 403, 403]
    assert all(r.json()["error"].startswith("out of scope") for r in responses)
    # Nothing changed.
    assert handler.orchestrator.provider_availability.row("codex").override_state is None


async def test_routes_are_503_without_a_handler(handler) -> None:
    app = FastAPI()
    app.include_router(build_providers_router(db=handler.db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/providers/availability")
    assert response.status_code == 503
