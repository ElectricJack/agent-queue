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
                # Global-admin tokens (elevated + project_id=None) are
                # loopback-only.  A stolen token cannot be replayed from
                # any remote host, so its blast radius is bounded to
                # processes on this machine.
                if (
                    scope.kind == "session"
                    and scope.elevated
                    and scope.project_id is None
                ):
                    client_host = request.client.host if request.client else None
                    if client_host not in ("127.0.0.1", "::1", "localhost"):
                        return JSONResponse(
                            {"ok": False, "error": "token restricted to loopback"},
                            status_code=403,
                        )
        else:
            if require and request.url.path not in _EXEMPT_PATHS:
                return JSONResponse(
                    {"ok": False, "error": "session token required"},
                    status_code=401,
                )

        # Playbook V2 Package 0 §3.7: attach server-derived identity after
        # ``store.validate()`` returns.  Neither field is minted into the
        # token or persisted — they come from the live ``sessions`` row, so
        # a profile edit takes effect without re-minting.
        if scope.kind == "session" and scope.session_id:
            scope = await _attach_derived_identity(scope)

        request.state.scope = scope
        if scope.kind == "session" and scope.session_id:
            with structlog.contextvars.bound_contextvars(session_id=scope.session_id):
                return await call_next(request)
        return await call_next(request)


async def _attach_derived_identity(scope):
    """Return *scope* with ``profile_id`` / ``policy_fingerprint`` filled in.

    Uses the same resolver the ``CommandHandler`` seam uses, so the two
    surfaces cannot disagree about who a token belongs to.  Any failure
    leaves the scope untouched: this is observability and echo-back for the
    caller, never the enforcement path — that lives at dispatch.
    """
    import dataclasses
    import logging

    handler = deps._command_handler
    if handler is None:
        return scope
    try:
        principal = await handler._principal_from_scope(
            {
                "kind": "session",
                "session_id": scope.session_id,
                "session_instance_token": scope.session_instance_token,
                "task_id": scope.task_id,
                "project_id": scope.project_id,
                "elevated": scope.elevated,
            }
        )
    except Exception:  # pragma: no cover — identity echo must never 500
        logging.getLogger(__name__).debug(
            "could not derive identity for session %s", scope.session_id, exc_info=True
        )
        return scope
    return dataclasses.replace(
        scope,
        profile_id=principal.profile_id,
        policy_fingerprint=principal.policy.fingerprint(),
    )
