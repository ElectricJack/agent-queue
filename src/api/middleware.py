"""FastAPI middleware for request-scoped logging context."""

from __future__ import annotations

from uuid import uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


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
