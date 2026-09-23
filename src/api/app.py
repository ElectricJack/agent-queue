"""FastAPI application factory for the agent-queue daemon.

Creates the FastAPI app with all routes mounted, including:
- Backward-compat /api/execute, /api/tools, /api/health
- Health/ready/plans endpoints (consolidated from old TCP server)
- MCP streamable-http sub-app (mounted at /)

The app is created by ``create_app()`` which is called from
``src.embedded_mcp.run_mcp_server()`` during daemon startup.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import quote

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from src.api import dependencies as deps
from src.api.execute import router as execute_router
from src.api.health import router as health_router
from src.api.graph import router as graph_router
from src.api.graph_layout import router as graph_layout_router
from src.api.routers.proposals import router as proposals_router
from src.api.pull_requests import build_pull_requests_router
from src.api.messages import router as messages_router
from src.api.metrics import router as metrics_router
from src.api.providers import router as providers_router
from src.api.pane_stream import router as pane_router
from src.api.sessions import router as sessions_router
from src.api.streams import router as streams_router
from src.api.task_attachments import router as task_attachments_router
from src.api.task_files import router as task_files_router
from src.api.task_sessions import router as task_sessions_router
from src.api.workspace_files import router as workspace_files_router
from src.api.middleware import RequestContextMiddleware, TokenAuthMiddleware
from src.api.terminal_stream import build_terminal_router
from src.api.websocket import WebSocketManager


def _add_binary_upload_format(app: FastAPI) -> None:
    """Keep generated clients compatible with FastAPI's OAS 3.1 upload schema.

    FastAPI describes ``UploadFile`` with ``contentMediaType``. The Python
    client generator still keys multipart files off ``format: binary``, so the
    published OpenAPI contract carries both equivalent annotations.
    """
    original_openapi = app.openapi

    def openapi_with_binary_upload_format() -> dict[str, Any]:
        spec = original_openapi()
        multipart = spec["paths"]["/api/tasks/{task_id}/attachments"]["post"]["requestBody"][
            "content"
        ]["multipart/form-data"]
        body_name = multipart["schema"]["$ref"].rsplit("/", 1)[-1]
        spec["components"]["schemas"][body_name]["properties"]["file"]["format"] = "binary"
        return spec

    app.openapi = openapi_with_binary_upload_format

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.orchestrator import Orchestrator


def create_app(
    orchestrator: Orchestrator,
    config: AppConfig,
    health_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    plan_content_provider: Callable[[str], Awaitable[str | None]] | None = None,
) -> FastAPI:
    """Build the FastAPI application with all routes.

    Args:
        orchestrator: The shared Orchestrator instance.
        config: The daemon's AppConfig.
        health_provider: Async callback returning health check results.
        plan_content_provider: Async callback returning plan markdown for a task_id.

    Returns:
        A configured FastAPI app ready to be served by uvicorn.
    """
    from src.commands.handler import CommandHandler

    app = FastAPI(
        title="Agent Q API",
        description="REST API for the agent-queue daemon.",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )

    # Wire up shared state via the dependencies module
    deps._orchestrator = orchestrator

    ch = orchestrator._command_handler
    if ch is None:
        ch = CommandHandler(orchestrator, config)
        orchestrator.set_command_handler(ch)
    deps._command_handler = ch

    # aq-surface Phase S2: wire the session-scoped bearer-token store.
    # Prefer the orchestrator-owned store (single instance, single cache) so
    # revocations flowing through the cascade sweep and per-request
    # validations always agree.  Fall back to constructing one when the
    # orchestrator hasn't (e.g. router-level tests that build a bare app).
    from src.api.auth import SessionTokenStore

    deps._token_store = getattr(orchestrator, "token_store", None) or SessionTokenStore(
        orchestrator.db,
        ttl_hours=config.api_auth.token_ttl_hours,
    )
    deps._require_session_token = bool(config.api_auth.require_session_token)

    deps._health_provider = health_provider
    deps._plan_content_provider = plan_content_provider
    deps._started_at = time.monotonic()
    deps._base_url = (
        config.health_check.base_url
        if hasattr(config, "health_check") and config.health_check.base_url
        else ""
    )

    # Add request context middleware for structured logging.
    # Starlette middlewares run in LIFO order — register RequestContext
    # FIRST and TokenAuth LAST so TokenAuth wraps outside and resolves
    # ``request.state.scope`` before request-context binds ``session_id``.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(TokenAuthMiddleware)

    # Register routers — backward-compat and health first
    app.include_router(execute_router)
    app.include_router(health_router)

    # Chat relay — /api/sessions/{name}/message[s] (supervisor-agent §6.2).
    # Explicit because the path carries a session name; must be mounted
    # before the codegen routers so its concrete paths win.
    app.include_router(messages_router)

    # Session SSE stream (S3): GET /api/sessions/{id}/stream — transcript
    # replay + live tail with peek-diff fallback.
    app.include_router(sessions_router)

    # Live pane SSE: GET /api/sessions/{id}/pane — capture-pane screens
    # from the shared PaneBroadcaster (one poll loop per watched session).
    app.include_router(pane_router)

    # Streamable-command registry (console-stream pane view): POST/GET
    # /api/streams* — start/metadata/subscribe/tail/kill.
    app.include_router(streams_router)

    # Task file preview (Phase 5): GET /api/tasks/{id}/files + /file
    app.include_router(task_files_router)
    app.include_router(task_attachments_router)
    app.include_router(task_sessions_router)

    # Workspace file browsing (pane view: file-browser): GET
    # /api/workspaces/{id}/browse + /file
    app.include_router(workspace_files_router)

    # Aggregate project-graph endpoint (Phase 4): GET /api/projects/{id}/graph
    app.include_router(graph_router)

    # Fleet metrics history: GET /api/metrics/series — cold start and zoom
    # for the Metrics tab, which otherwise follows metrics.tick over the WS.
    app.include_router(metrics_router)

    # Provider quota as each provider reports it: GET /api/providers/usage.
    # Distinct from the token ledger's estimate, which is what metrics serves.
    app.include_router(providers_router)

    # Viewport-bounded layout endpoints (spatial-layout design §5):
    # /api/projects/{id}/graph/extent + /tiles + /list + /node + /locate + /tidy + /jobs
    app.include_router(graph_layout_router)

    # Task proposal read (Phase 6): GET /api/proposals/{id}
    app.include_router(proposals_router)

    # Open PR links next to document reviews, with GitHub as the state source.
    app.include_router(
        build_pull_requests_router(db=orchestrator.db, github_access=orchestrator.github_access)
    )

    # Auto-generated typed command routes (POST /api/{category}/{command})
    from src.api.routers import register_all_routers

    register_all_routers(app)

    # Raw PTY terminal stream owns attach clients only, never agent processes.
    app.include_router(build_terminal_router(
        orchestrator, config, token_store=deps._token_store,
    ))

    # The daemon is API-only: it no longer serves the dashboard SPA (the
    # bundle now lives in the separate dashboard server process).  A request
    # for /dashboard gets a machine-readable pointer naming the configured
    # dashboard server URL, not a static mount (docs/specs/dashboard-server.md
    # §5): a 307 to that URL while the dashboard server is enabled, a 404 with
    # a null URL when it is not.  The redirect is load-bearing: an `aq update`
    # started before this change keeps its old updater in memory, which probes
    # GET <daemon>/dashboard/ after the pull and fails on >= 400; urllib follows
    # the 307 to the dashboard server's 200 (§6.2).  dashboard_url is derived
    # from dashboard.server alone — never the request's Host — with a wildcard
    # bind rendered as 127.0.0.1.
    def _dashboard_hint_url() -> str | None:
        server = config.dashboard_server
        if not server.enabled:
            return None
        host = server.host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{server.port}/"

    @app.api_route("/dashboard", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/dashboard/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def dashboard_not_served_here(request: Request, rest: str = ""):
        dashboard_url = _dashboard_hint_url()
        body = {
            "ok": False,
            "error": "dashboard_not_served_here",
            "dashboard_url": dashboard_url,
            "hint": "The daemon is API-only. Run `aq dashboard status`.",
        }
        if dashboard_url is None:
            return JSONResponse(body, status_code=404)
        # <dashboard_url><rest>?<query>: the bundle is built for "/", so the
        # old /dashboard/<route> bookmark lands on the same SPA route.  The
        # path is re-encoded (it arrives decoded) and kept relative, so the
        # Location can only ever name the configured dashboard server.
        location = dashboard_url + quote(rest.lstrip("/"), safe="/:@!$&'()*+,;=")
        if request.url.query:
            location += f"?{request.url.query}"
        return JSONResponse(body, status_code=307, headers={"Location": location})

    _add_binary_upload_format(app)

    # WebSocket event stream — forward notify.* events to connected clients
    ws_manager = WebSocketManager(orchestrator.bus, db=orchestrator.db)
    ws_manager.start()

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket):
        await ws_manager.handle(websocket)

    @app.on_event("shutdown")
    async def _shutdown_ws():
        ws_manager.shutdown()
        from src.api.pane_stream import shutdown_broadcaster

        await shutdown_broadcaster()

    return app
