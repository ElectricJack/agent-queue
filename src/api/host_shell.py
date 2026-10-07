"""Operator host shell routes: open, list and close (attach is ``/ws/terminal/{name}``).

A request with any bearer token (a worker, a supervisor) is refused. The local
operator may use them; so may a viewer the dashboard edge proxied from another
machine when ``dashboard.host_shell.allow_remote`` is on. Every route answers
403 while ``dashboard.host_shell`` is off. Open/close and accepted terminal/input
connections and disconnects are audit-logged (daemon log and the ``events``
table) with the caller's identity and real peer.
See :mod:`src.sessions.host_shell`.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from src.sessions.host_shell import HostShellError, is_host_shell_name

logger = logging.getLogger("aq.audit.host_shell")
HOST_SHELL_NO_BEARER = "Host shells do not accept bearer tokens"
HOST_SHELL_VIEWER_REQUIRED = (
    "Host shells require a local operator or an allowed remote dashboard viewer "
    "(dashboard.host_shell.allow_remote)"
)


class HostShellInfo(BaseModel):
    name: str
    created_at: float
    attached_clients: int


class HostShellListResponse(BaseModel):
    enabled: bool
    shells: list[HostShellInfo]


class HostShellOpenResponse(BaseModel):
    shell: HostShellInfo


class HostShellCloseResponse(BaseModel):
    name: str
    closed: bool


def _identity(request: HTTPConnection) -> str:
    peer = request.client.host if request.client else "unknown"
    via = request.headers.get("x-aq-dashboard-viewer")
    real = request.headers.get("x-aq-dashboard-peer")
    if via == "other":
        return f"remote-dashboard-viewer (peer {real or 'unknown'})"
    if via:
        return f"local-operator via dashboard (peer {real or 'unknown'})"
    return f"local-operator ({peer})"


def _require_operator(request: Request, service, *, allow_disabled: bool = False) -> None:
    scope = getattr(request.state, "scope", None)
    if request.headers.getlist("authorization") or (
        scope is not None and getattr(scope, "kind", "local") != "local"
    ):
        raise HTTPException(403, HOST_SHELL_NO_BEARER)
    if not service.host_shell_viewer(request):
        raise HTTPException(403, HOST_SHELL_VIEWER_REQUIRED)
    if not allow_disabled and not service.host_shell_enabled():
        raise HTTPException(403, "Host shells are disabled (dashboard.host_shell.enabled)")


async def audit_host_shell(service, request: HTTPConnection, event: str, name: str) -> None:
    """Record HTTP and WebSocket lifecycle events with the same caller identity."""
    identity = _identity(request)
    logger.warning("host shell %s: %s by %s", event, name, identity)
    db = getattr(service.orchestrator, "db", None)
    if db is None:
        return
    try:
        await db.log_event(
            f"host_shell.{event}", payload=json.dumps({"name": name, "identity": identity}),
        )
    except Exception:
        logger.exception("host shell %s: audit event for %s not recorded", event, name)


def add_host_shell_routes(router: APIRouter, service) -> None:
    @router.get("/api/host-shell", operation_id="host_shell_list")
    async def host_shell_list(request: Request, response: Response) -> HostShellListResponse:
        response.headers["Cache-Control"] = "no-store"
        _require_operator(request, service, allow_disabled=True)
        if not service.host_shell_enabled():
            return HostShellListResponse(enabled=False, shells=[])
        shells = await service.host_shells().list()
        return HostShellListResponse(
            enabled=True, shells=[HostShellInfo(**s.to_dict()) for s in shells],
        )

    @router.post("/api/host-shell", operation_id="host_shell_open")
    async def host_shell_open(request: Request) -> HostShellOpenResponse:
        _require_operator(request, service)
        try:
            shell = await service.host_shells().open(max_shells=service.config.host_shell.max_shells)
        except HostShellError as exc:
            raise HTTPException(409, str(exc)) from None
        await audit_host_shell(service, request, "opened", shell.name)
        return HostShellOpenResponse(shell=HostShellInfo(**shell.to_dict()))

    @router.post("/api/host-shell/{name}/close", operation_id="host_shell_close")
    async def host_shell_close(request: Request, name: str) -> HostShellCloseResponse:
        _require_operator(request, service)
        if not is_host_shell_name(name):
            raise HTTPException(404, "No such host shell")
        try:
            closed = await service.host_shells().close(name)
        except HostShellError as exc:
            raise HTTPException(409, str(exc)) from None
        if not closed:
            raise HTTPException(404, "No such host shell")
        await audit_host_shell(service, request, "closed", name)
        return HostShellCloseResponse(name=name, closed=True)
