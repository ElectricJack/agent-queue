"""Route labels are templates, streams are handshakes, failures are re-raised.

``RouteLatencyMiddleware`` (spec 2026-09-24 dashboard performance §4.1) is the
daemon's per-route latency source.  These cases pin its contract: a label is
the registered route template (never a raw path), an SSE response or a
WebSocket records its handshake and an ``open`` gauge instead of a lifetime,
the application's exception is re-raised unchanged, and nothing the registry
does can fail a request.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api import perf_middleware
from src.api.perf_middleware import RouteLatencyMiddleware, is_event_stream, route_label
from src.metrics.perf import PerfRegistry, install_registry


def make_app(registry: PerfRegistry, *, included: bool = True) -> FastAPI:
    app = FastAPI()
    router = APIRouter() if included else app

    @router.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        return {"id": task_id}

    @router.get("/api/boom")
    async def boom():
        raise RuntimeError("boom")

    @router.get("/api/teapot", status_code=418)
    async def teapot():
        return {}

    @router.get("/api/sessions/{session_id}/pane")
    async def pane(session_id: str):
        async def gen():
            yield b"data: 1\n\n"
            await asyncio.sleep(0.05)
            yield b"data: 2\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @router.websocket("/ws/events")
    async def ws(websocket: WebSocket):
        if websocket.query_params.get("deny"):
            await websocket.close(code=4401)
            return
        if websocket.query_params.get("boom"):
            raise RuntimeError("before accept")
        await websocket.accept()
        if websocket.query_params.get("late"):
            raise RuntimeError("after accept")
        await websocket.send_text("hello")
        await websocket.close()

    async def asset(request):
        return PlainTextResponse(request.path_params["name"])

    app.mount("/static", Starlette(routes=[Route("/{name}", asset)]))
    if included:
        app.include_router(router)

    app.add_middleware(RouteLatencyMiddleware, registry=registry)
    return app


@pytest.fixture
def registry():
    return PerfRegistry()


@pytest.fixture
def client(registry):
    return AsyncClient(transport=ASGITransport(app=make_app(registry)), base_url="http://t")


async def test_labels_are_route_templates_never_raw_paths(registry, client):
    async with client:
        assert (await client.get("/api/tasks/secret-task-id")).status_code == 200
        assert (await client.get("/api/teapot")).status_code == 418
    routes = registry.snapshot()["api"]["routes"]
    assert set(routes) == {"GET /api/tasks/{task_id}", "GET /api/teapot"}
    assert routes["GET /api/tasks/{task_id}"]["status"] == {"kind": "sum", "2xx": 1}
    assert routes["GET /api/teapot"]["status"] == {"kind": "sum", "4xx": 1}
    assert routes["GET /api/tasks/{task_id}"]["latency"]["count"] == 1
    assert "secret-task-id" not in repr(routes)


async def test_an_unmatched_path_is_counted_not_labelled(registry, client):
    async with client:
        assert (await client.get("/nope/12345")).status_code == 404
    snap = registry.snapshot()
    assert snap["api"]["errors"]["unmatched"] == 1
    assert "GET unmatched" in snap["api"]["routes"]
    assert "12345" not in repr(snap)


async def test_a_mount_is_labelled_by_its_prefix_even_after_routing_rewrites_the_scope(
    registry, client
):
    # The router rewrites ``root_path`` in place when it enters a mount, so a
    # label computed after the app ran would no longer match the mount.
    async with client:
        response = await client.get("/static/app-3f9c.js")
    assert response.text == "app-3f9c.js"
    snap = registry.snapshot()
    assert set(snap["api"]["routes"]) == {"GET MOUNT/static"}
    assert snap["api"]["errors"]["unmatched"] == 0
    assert "3f9c" not in repr(snap)


async def test_a_wrong_method_is_labelled_by_the_template_it_hit(registry, client):
    async with client:
        assert (await client.post("/api/tasks/t-1")).status_code == 405
    snap = registry.snapshot()
    assert snap["api"]["routes"]["POST /api/tasks/{task_id}"]["status"] == {
        "kind": "sum",
        "4xx": 1,
    }
    assert snap["api"]["errors"]["unmatched"] == 0


async def test_an_unknown_method_is_folded_so_labels_stay_bounded(registry, client):
    async with client:
        await client.request("PROPFIND", "/nope")
    assert set(registry.snapshot()["api"]["routes"]) == {"OTHER unmatched"}


@pytest.mark.parametrize("included", [False, True])
async def test_flat_and_included_routes_keep_distinct_templates(registry, included):
    app = make_app(registry, included=included)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        assert (await ac.get("/api/tasks/private-id")).status_code == 200
        assert (await ac.get("/api/teapot")).status_code == 418
        assert (await ac.post("/api/tasks/private-id")).status_code == 405
    assert set(registry.snapshot()["api"]["routes"]) == {
        "GET /api/tasks/{task_id}",
        "GET /api/teapot",
        "POST /api/tasks/{task_id}",
    }


async def test_nested_includes_use_effective_prefixes_and_full_match_precedence(registry):
    app = FastAPI()
    child = APIRouter()

    @child.get("/tasks/{task_id:int}")
    async def get_task(task_id: int):
        return {"id": task_id}

    parent = APIRouter()
    parent.include_router(child, prefix="/v1")
    app.include_router(parent, prefix="/api")
    app.include_router(child, prefix="/other")
    later = APIRouter()

    @later.post("/tasks/{task_id:int}")
    async def post_task(task_id: int):
        return {"posted": task_id}

    app.include_router(later, prefix="/api/v1")
    app.add_middleware(RouteLatencyMiddleware, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        assert (await ac.get("/api/v1/tasks/123")).status_code == 200
        assert (await ac.get("/other/tasks/123")).status_code == 200
        assert (await ac.post("/api/v1/tasks/123")).json() == {"posted": 123}
        assert (await ac.delete("/api/v1/tasks/123")).status_code == 405
        assert (await ac.get("/api/v1/tasks/not-an-int")).status_code == 404
    routes = registry.snapshot()["api"]["routes"]
    assert set(routes) == {
        "GET /api/v1/tasks/{task_id:int}",
        "GET /other/tasks/{task_id:int}",
        "POST /api/v1/tasks/{task_id:int}",
        "DELETE /api/v1/tasks/{task_id:int}",
        "GET unmatched",
    }
    assert routes["POST /api/v1/tasks/{task_id:int}"]["status"] == {"kind": "sum", "2xx": 1}


def test_included_websockets_and_mounts_use_effective_prefixes(registry):
    app = FastAPI()
    router = APIRouter()

    @router.websocket("/ws/{session_id}")
    async def ws(websocket: WebSocket, session_id: str):
        await websocket.accept()
        await websocket.close()

    async def asset(request):
        return PlainTextResponse("asset")

    app.mount("/api/static", Starlette(routes=[Route("/{name}", asset)]))
    app.include_router(router, prefix="/api")
    app.add_middleware(RouteLatencyMiddleware, registry=registry)
    with TestClient(app) as tc:
        with tc.websocket_connect("/api/ws/private-session"):
            pass
        assert tc.get("/api/static/private-file.js").status_code == 200
    snap = registry.snapshot()
    assert set(snap["api"]["streams"]) == {"WS /api/ws/{session_id}"}
    assert snap["api"]["streams"]["WS /api/ws/{session_id}"]["open"] == 0
    assert set(snap["api"]["routes"]) == {"GET MOUNT/api/static"}
    assert "private-" not in repr(snap)


async def test_included_endpoint_labels_still_obey_registry_capacity(registry, monkeypatch):
    from src.metrics import perf

    monkeypatch.setattr(perf, "ROUTE_LIMIT", 1)
    async with AsyncClient(
        transport=ASGITransport(app=make_app(registry)), base_url="http://t"
    ) as ac:
        assert (await ac.get("/api/tasks/private-id")).status_code == 200
        assert (await ac.get("/api/teapot")).status_code == 418
        assert (await ac.post("/api/tasks/other-private-id")).status_code == 405
        assert (await ac.get("/api/tasks/another-private-id")).status_code == 200
    routes = registry.snapshot()["api"]["routes"]
    assert set(routes) == {"GET /api/tasks/{task_id}", perf.OVERFLOW_LABEL}
    assert routes["GET /api/tasks/{task_id}"]["latency"]["count"] == 2
    assert routes[perf.OVERFLOW_LABEL]["latency"]["count"] == 2
    assert "private-id" not in repr(routes)


async def test_include_router_post_reproduction_and_route_updates(registry):
    app = FastAPI()
    router = APIRouter()

    @router.post("/api/agent/list")
    async def agents():
        return []

    app.include_router(router)
    app.add_middleware(RouteLatencyMiddleware, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        assert (await ac.post("/api/agent/list")).status_code == 200

        # A later include must be observed rather than retaining our own stale
        # flattened list. FastAPI snapshots routers when they are included.
        additional = APIRouter()

        @additional.get("/api/new")
        async def new():
            return {}

        app.include_router(additional)
        assert (await ac.get("/api/new")).status_code == 200
    routes = registry.snapshot()["api"]["routes"]
    assert set(routes) == {"POST /api/agent/list", "GET /api/new"}
    assert [entry["latency"]["count"] for entry in routes.values()] == [1, 1]


async def test_full_match_beats_an_earlier_included_partial_template(registry):
    app = FastAPI()
    router = APIRouter()

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        return {}

    app.include_router(router, prefix="/api")

    @app.post("/api/tasks/create")
    async def create():
        return {}

    app.add_middleware(RouteLatencyMiddleware, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        assert (await ac.post("/api/tasks/create")).status_code == 200
        assert (await ac.delete("/api/tasks/create")).status_code == 405
    assert set(registry.snapshot()["api"]["routes"]) == {
        "POST /api/tasks/create",
        "DELETE /api/tasks/{task_id}",
    }


def test_pathless_foreign_matches_do_not_invent_a_root_template():
    from types import SimpleNamespace

    from starlette.routing import Match

    class Pathless:
        def matches(self, scope):
            return Match.FULL, {}

    app = SimpleNamespace(router=SimpleNamespace(routes=[Pathless()]))
    assert route_label({"type": "http", "method": "GET", "app": app}) == "GET unmatched"


async def test_an_exception_is_recorded_as_5xx_and_re_raised(registry, client):
    async with client:
        with pytest.raises(RuntimeError, match="boom"):
            await client.get("/api/boom")
    snap = registry.snapshot()
    assert snap["api"]["errors"]["exceptions"] == 1
    assert snap["api"]["routes"]["GET /api/boom"]["status"] == {"kind": "sum", "5xx": 1}


def _raw_scope(registry: PerfRegistry) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions/s1/pane",
        "app": make_app(registry),
        "headers": [],
        "query_string": b"",
    }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def test_an_sse_route_records_a_handshake_and_an_open_gauge_not_a_lifetime(registry):
    # Driven as raw ASGI: httpx's ASGITransport buffers a whole streaming body
    # before returning, so it cannot observe the gauge while the stream is open.
    release = asyncio.Event()

    async def sse_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
        await release.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = RouteLatencyMiddleware(sse_app, registry=registry)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(middleware(_raw_scope(registry), _receive, send))
    await asyncio.sleep(0.02)
    mid = registry.snapshot()
    stream = mid["api"]["streams"]["GET /api/sessions/{session_id}/pane"]
    assert stream["open"] == 1
    assert stream["handshake"]["count"] == 1 and stream["handshake"]["max"] < 40
    assert stream["outcome"] == {"kind": "sum", "accepted": 1, "refused": 0}
    release.set()
    await task
    end = registry.snapshot()
    assert end["api"]["streams"]["GET /api/sessions/{session_id}/pane"]["open"] == 0
    assert "GET /api/sessions/{session_id}/pane" not in end["api"]["routes"]
    assert end["api"]["all"]["count"] == 0
    assert [m["type"] for m in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]


async def test_an_sse_stream_that_raises_leaves_the_gauge_and_re_raises(registry):
    async def sse_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        raise RuntimeError("pane gone")

    async def send(message):
        pass

    middleware = RouteLatencyMiddleware(sse_app, registry=registry)
    with pytest.raises(RuntimeError, match="pane gone"):
        await middleware(_raw_scope(registry), _receive, send)
    snap = registry.snapshot()
    assert snap["api"]["streams"]["GET /api/sessions/{session_id}/pane"]["open"] == 0
    assert snap["api"]["errors"]["exceptions"] == 1
    assert snap["api"]["routes"] == {}


async def test_the_sse_route_through_a_client_is_a_stream_not_a_route(registry, client):
    async with client:
        response = await client.get("/api/sessions/s1/pane")
    assert response.text == "data: 1\n\ndata: 2\n\n"
    snap = registry.snapshot()
    assert snap["api"]["routes"] == {}
    stream = snap["api"]["streams"]["GET /api/sessions/{session_id}/pane"]
    assert stream["outcome"]["accepted"] == 1 and stream["open"] == 0


def test_websocket_accept_and_refusal_are_handshakes(registry):
    app = make_app(registry)
    with TestClient(app) as tc:
        with tc.websocket_connect("/ws/events") as ws:
            assert ws.receive_text() == "hello"
        with pytest.raises(WebSocketDisconnect), tc.websocket_connect("/ws/events?deny=1"):
            pass
    snap = registry.snapshot()
    streams = snap["api"]["streams"]["WS /ws/events"]
    assert streams["outcome"] == {"kind": "sum", "accepted": 1, "refused": 1}
    assert streams["handshake"]["count"] == 2
    assert streams["open"] == 0
    assert snap["api"]["routes"] == {}


def test_a_websocket_that_raises_before_accept_is_a_refusal_and_an_exception(registry):
    app = make_app(registry)
    with (
        TestClient(app) as tc,
        pytest.raises(RuntimeError, match="before accept"),
        tc.websocket_connect("/ws/events?boom=1"),
    ):
        pass
    snap = registry.snapshot()
    assert snap["api"]["streams"]["WS /ws/events"]["outcome"] == {
        "kind": "sum",
        "accepted": 0,
        "refused": 1,
    }
    assert snap["api"]["errors"]["exceptions"] == 1
    assert snap["api"]["routes"] == {}


def test_a_websocket_that_raises_after_accept_closes_its_gauge(registry):
    app = make_app(registry)
    with (
        TestClient(app) as tc,
        pytest.raises(RuntimeError, match="after accept"),
        tc.websocket_connect("/ws/events?late=1") as ws,
    ):
        ws.receive_text()
    snap = registry.snapshot()
    stream = snap["api"]["streams"]["WS /ws/events"]
    assert stream["outcome"] == {"kind": "sum", "accepted": 1, "refused": 0}
    assert stream["open"] == 0
    assert snap["api"]["errors"]["exceptions"] == 1


async def test_disabled_registry_records_nothing(registry, client):
    registry.enabled = False
    async with client:
        assert (await client.get("/api/tasks/t")).status_code == 200
        assert (await client.get("/nope")).status_code == 404
    registry.enabled = True
    snap = registry.snapshot()
    assert snap["api"]["all"]["count"] == 0
    assert snap["api"]["errors"]["unmatched"] == 0


async def test_a_registry_that_raises_does_not_break_the_request(client, registry):
    def explode(*args, **kwargs):
        raise RuntimeError("registry down")

    # Bypass the registry's own never-raise wrapper on purpose.
    for name in (
        "observe_route",
        "observe_unmatched",
        "observe_exception",
        "observe_stream_handshake",
        "stream_opened",
        "stream_closed",
    ):
        setattr(registry, name, explode)
    async with client:
        assert (await client.get("/api/tasks/t")).status_code == 200
        assert (await client.get("/nope")).status_code == 404
        assert (await client.get("/api/sessions/s1/pane")).status_code == 200
        with pytest.raises(RuntimeError, match="boom"):
            await client.get("/api/boom")


async def test_a_registry_that_cannot_be_read_does_not_break_the_request(monkeypatch):
    class Unreadable:
        @property
        def enabled(self):
            raise RuntimeError("no registry")

    async with AsyncClient(
        transport=ASGITransport(app=make_app(Unreadable())), base_url="http://t"
    ) as ac:
        assert (await ac.get("/api/tasks/t")).status_code == 200

    def no_registry():
        raise RuntimeError("no registry")

    monkeypatch.setattr(perf_middleware, "perf_registry", no_registry)
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return {}

    app.add_middleware(RouteLatencyMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        assert (await ac.get("/ok")).status_code == 200


async def test_without_an_explicit_registry_the_process_registry_is_used():
    registry = PerfRegistry()
    install_registry(registry)
    try:
        app = FastAPI()

        @app.get("/ok")
        async def ok():
            return {}

        app.add_middleware(RouteLatencyMiddleware)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            assert (await ac.get("/ok")).status_code == 200
        assert set(registry.snapshot()["api"]["routes"]) == {"GET /ok"}
    finally:
        install_registry(None)


async def test_lifespan_passes_through_untouched(registry):
    events: list[str] = []

    async def app(scope, receive, send):
        events.append(scope["type"])

    await RouteLatencyMiddleware(app, registry=registry)({"type": "lifespan"}, _receive, None)
    assert events == ["lifespan"]
    assert registry.snapshot()["api"]["all"]["count"] == 0


def test_route_label_and_event_stream_helpers():
    assert is_event_stream([(b"content-type", b"text/event-stream; charset=utf-8")])
    assert is_event_stream([(b"Content-Type", b"Text/Event-Stream")])
    assert not is_event_stream([(b"content-type", b"application/json")])
    assert not is_event_stream(None)
    assert route_label({"type": "http", "method": "GET", "path": "/x", "app": None}) == (
        "GET unmatched"
    )
    assert route_label({"type": "websocket", "path": "/x", "app": None}) == "WS unmatched"


async def test_create_app_registers_the_middleware_outermost(tmp_path):
    """The daemon's app times every request, including a token refusal."""
    from tests.test_api_dashboard_pointer import daemon_app

    registry = PerfRegistry()
    install_registry(registry)
    try:
        async with daemon_app(tmp_path, name="perf_middleware.db") as app:
            assert app.user_middleware[0].cls is RouteLatencyMiddleware
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://t") as ac:
                assert (await ac.get("/openapi.json")).status_code == 200
                refused = await ac.get(
                    "/openapi.json", headers={"Authorization": "Bearer aqs_not-a-token"}
                )
                assert refused.status_code == 401
        routes = registry.snapshot()["api"]["routes"]
        assert routes["GET /openapi.json"]["status"] == {"kind": "sum", "2xx": 1, "4xx": 1}
    finally:
        install_registry(None)
