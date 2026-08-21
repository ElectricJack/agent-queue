"""FastAPI middleware for request-scoped logging context."""

from __future__ import annotations

from uuid import uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.api import dependencies as deps
from src.api.auth import LOCAL_SCOPE


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind request metadata into structlog contextvars for every request.

    Downstream handlers and any ``logging.getLogger()`` calls within the
    request automatically include ``request_id``, ``route``, ``method``,
    and ``component="api"`` in their log output.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID", uuid4().hex[:8])
        with structlog.contextvars.bound_contextvars(
            request_id=request_id,
            route=request.url.path,
            method=request.method,
            component="api",
        ):
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response


_EXEMPT_PATHS: frozenset[str] = frozenset(
    {"/api/health", "/health", "/ready", "/docs", "/redoc", "/openapi.json"}
)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Resolve ``request.state.scope`` from ``Authorization: Bearer aqs_...``.

    * No header → :data:`LOCAL_SCOPE` (unless ``api_auth.require_session_token``
      is on and the path is not exempt, in which case 401).
    * Header present but token invalid/expired/revoked → 401 JSON.
    * Header present and valid → session-kind :class:`RequestScope`.

    Ordering: register AFTER :class:`RequestContextMiddleware` in
    ``create_app`` so that Starlette's LIFO application order runs
    the token resolver first (before the request-context logger binds).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        header = request.headers.get("Authorization", "")
        store = deps._token_store
        require = deps._require_session_token

        scope = LOCAL_SCOPE
        if header.startswith("Bearer "):
            token = header[len("Bearer ") :].strip()
            if store is not None:
                resolved = await store.validate(token)
                if resolved is None:
                    return JSONResponse(
                        {"ok": False, "error": "invalid or expired token"},
                        status_code=401,
                    )
                scope = resolved
        else:
            if require and request.url.path not in _EXEMPT_PATHS:
                return JSONResponse(
                    {"ok": False, "error": "session token required"},
                    status_code=401,
                )

        request.state.scope = scope
        if scope.kind == "session" and scope.session_id:
            with structlog.contextvars.bound_contextvars(session_id=scope.session_id):
                return await call_next(request)
        return await call_next(request)
