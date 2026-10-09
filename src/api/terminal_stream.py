"""Authorized, fixed-generation terminal WebSockets with bounded raw-byte flow.

These routes never launch an agent. ``/ws/terminal/{id}`` owns only a disposable
tmux attach client. Two opt-in query parameters serve phones: ``history=N`` sends
up to N lines of the pane's scrollback ahead of the attach output (and has tmux
scroll with line feeds, so later lines reach the viewer's scrollback too), and
``restore_size=1`` puts the agent's window size back when the last client leaves.
``/ws/terminal/{id}/input`` never attaches: it types through tmux commands against
the fenced session and sends no output. Both run the same origin, credential,
operator, loopback, generation and connection-limit checks. Input is ephemeral: no
command dispatch, transcripts, replay, or input logging.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from src.api.auth import (
    LOCAL_SCOPE,
    request_operator_viewer,
    request_remote_dashboard_viewer,
)
from src.api.host_shell import (
    HOST_SHELL_NO_BEARER,
    HOST_SHELL_VIEWER_REQUIRED,
    audit_host_shell,
)
from src.models import TaskStatus
from src.sessions.host_shell import HostShellManager, is_host_shell_name
from src.sessions.terminal_pty import TerminalAttachError

_PROTOCOL = "aq-terminal-v1"
_LIVE = {"starting", "running", "draining"}
_ACTIVE_TASKS = {
    TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_INPUT,
}
_INPUT_FRAME_LIMIT = 64 * 1024
_INPUT_QUEUE_LIMIT = 128 * 1024
_OUTPUT_CHUNK = 16 * 1024
_HISTORY_LINES = 10_000


class TerminalAccessResponse(BaseModel):
    status: str
    code: int = 0
    message: str = ""
    retryable: bool = False


class TerminalStreamError(Exception):
    """Only constant, operator-safe messages cross the WebSocket boundary."""

    def __init__(self, message: str, code: int = 4409):
        super().__init__(message)
        self.code = code


def _origin(value: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or "?" in value or "#" in value
            or parsed.port == 0
            or any(c.isspace() for c in value) or "\\" in value
        ):
            raise ValueError
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (
            443 if parsed.scheme == "https" else 80
        )
    except ValueError:
        raise TerminalStreamError("Terminal origin is not allowed", 4403) from None


def _dimensions(cols, rows) -> tuple[int, int]:
    if type(cols) is not int or type(rows) is not int or not (2 <= cols <= 500 and 1 <= rows <= 300):
        raise TerminalStreamError("Invalid terminal dimensions", 4400)
    return cols, rows


def _phone_options(query) -> tuple[int, bool]:
    """``history`` (0..10000 lines) and ``restore_size`` (0/1); absent means off."""
    history = query.get("history", "0")
    restore = query.get("restore_size", "0")
    if (
        not history.isascii() or not history.isdigit() or int(history) > _HISTORY_LINES
        or restore not in {"0", "1"}
    ):
        raise TerminalStreamError("Invalid terminal options", 4400)
    return int(history), restore == "1"


def _generation(row):
    return row.id, row.name, row.provider, row.instance_token, row.agent_id, row.started_at


async def _attach(provider, row, *, cols, rows, restore_size=False, scrollback=False):
    # POSIX imports are deliberately confined to the platform-specific helper.
    from src.sessions.terminal_pty import PtyTmuxClient
    return await PtyTmuxClient.attach(
        provider, row, cols=cols, rows=rows, restore_size=restore_size,
        scrollback=scrollback,
    )


async def _attach_input(provider, row):
    # Input-only: resolves the fenced session and creates no tmux client.
    from src.sessions.terminal_input import TmuxInputClient
    return await TmuxInputClient.attach(provider, row)


class TerminalStreamService:
    def __init__(
        self, orchestrator, config, *, token_store=None, attach=None, attach_input=None,
        recheck_seconds: float = 2.0, output_limit: int = 128 * 1024,
        ack_timeout: float = 30.0, connection_limit: int = 16,
    ):
        self.orchestrator = orchestrator
        self.config = config
        self.token_store = token_store
        self.attach = attach or _attach
        self.attach_input = attach_input or _attach_input
        self.recheck_seconds = recheck_seconds
        self.output_limit = output_limit
        self.ack_timeout = ack_timeout
        self.connection_limit = connection_limit
        self._handlers: set[asyncio.Task] = set()

    def _credentials(self, ws) -> str | None:
        headers = ws.headers.getlist("authorization")
        protocols = ws.scope.get("subprotocols", [])
        if any(p.startswith("aq-bearer") and not p.startswith("aq-bearer.") for p in protocols):
            raise TerminalStreamError("Invalid terminal credentials", 4401)
        tokens = [p[len("aq-bearer."):] for p in protocols if p.startswith("aq-bearer.")]
        if len(headers) > 1 or len(tokens) > 1 or (headers and tokens):
            raise TerminalStreamError("Invalid terminal credentials", 4401)
        token = None
        if headers:
            if not headers[0].startswith("Bearer "):
                raise TerminalStreamError("Invalid terminal credentials", 4401)
            token = headers[0][7:]
        elif tokens:
            token = tokens[0]
        if token is not None and not re.fullmatch(r"aqs_[A-Za-z0-9_-]{1,200}", token):
            raise TerminalStreamError("Invalid terminal credentials", 4401)
        # Credentials in URLs are never consumed (and may leak to access logs).
        if any(k in ws.query_params for k in ("token", "access_token", "authorization")):
            raise TerminalStreamError("Terminal credentials must not be in the URL", 4401)
        return token

    def _check_origin(self, ws, *, browser_probe=False, browser_origin=None):
        origins = ws.headers.getlist("origin")
        if not origins and browser_probe:
            # Same-origin GETs may omit Origin. Still check their browser Host
            # against the same literal-loopback/trusted-origin policy as WS.
            origins = [browser_origin or f"{ws.url.scheme}://{ws.headers.get('host', '')}"]
        if not origins:
            return  # Native clients have no Origin; loopback/auth still apply.
        if len(origins) != 1 or len(ws.headers.getlist("host")) != 1:
            raise TerminalStreamError("Terminal origin is not allowed", 4403)
        origin = _origin(origins[0])
        scheme = "https" if ws.url.scheme in {"wss", "https"} else "http"
        expected = _origin(f"{scheme}://{ws.headers['host']}")
        trusted = getattr(self.config.api_auth, "trusted_dashboard_origins", [])
        # A matching attacker-controlled Host/Origin can be DNS-rebound onto
        # loopback. Automatic trust is only for literal loopback dashboard hosts;
        # custom domains must be explicitly configured even through a local proxy.
        local_same_origin = origin == expected and expected[1] in {"localhost", "127.0.0.1", "::1"}
        if not local_same_origin and origin not in {_origin(item) for item in trusted}:
            raise TerminalStreamError("Terminal origin is not allowed", 4403)

    def host_shells(self) -> HostShellManager:
        return HostShellManager(self.orchestrator.session_providers.create("tmux"))

    def host_shell_enabled(self) -> bool:
        cfg = getattr(self.config, "host_shell", None)
        return bool(cfg is not None and cfg.enabled is True)

    def host_shell_allow_remote(self) -> bool:
        cfg = getattr(self.config, "host_shell", None)
        return bool(cfg is not None and getattr(cfg, "allow_remote", False) is True)

    def host_shell_viewer(self, request) -> bool:
        """The local operator, or a remote dashboard viewer under ``allow_remote``."""
        if request_operator_viewer(request):
            return True
        return self.host_shell_allow_remote() and request_remote_dashboard_viewer(request)

    async def _authorize(self, ws, token, *, host_shell: bool = False):
        if host_shell:
            # A host shell is remote code execution by design: never a bearer
            # token (a worker or a supervisor); the local operator on loopback
            # or through the dashboard edge's operator verdict; and, with
            # ``allow_remote``, a viewer the edge proxied from another machine.
            if not self.host_shell_enabled():
                raise TerminalStreamError("Host shells are disabled", 4403)
            if token is not None:
                raise TerminalStreamError(HOST_SHELL_NO_BEARER, 4403)
            if not self.host_shell_viewer(ws):
                raise TerminalStreamError(HOST_SHELL_VIEWER_REQUIRED, 4403)
        scope = LOCAL_SCOPE
        if token is not None:
            if self.token_store is None:
                raise TerminalStreamError("Invalid or expired terminal credentials", 4401)
            scope = await self.token_store.validate(token, refresh=True)
            if scope is None:
                raise TerminalStreamError("Invalid or expired terminal credentials", 4401)
        elif self.config.api_auth.require_session_token:
            raise TerminalStreamError("Terminal session token required", 4401)
        if scope.kind != "local" and not (
            scope.kind == "session" and scope.elevated and scope.project_id is None
            and scope.task_id is None
        ):
            raise TerminalStreamError("Global operator access is required", 4403)
        if not ws.client or ws.client.host not in {"127.0.0.1", "::1", "localhost"}:
            raise TerminalStreamError("Terminal access is restricted to loopback", 4403)

    async def _host_shell(self, session_id, generation=None):
        shell = await self.host_shells().get(session_id)
        if shell is None:
            raise TerminalStreamError("Terminal session is no longer available")
        identity = (shell.name, shell.instance_token)
        if generation is not None and identity != generation:
            raise TerminalStreamError("Terminal session instance has changed")
        return shell, identity

    async def _session(self, session_id, generation=None):
        if is_host_shell_name(session_id):
            return await self._host_shell(session_id, generation)
        db = self.orchestrator.db
        row = await db.get_session(session_id)
        if (
            row is None or row.id != session_id or row.state not in _LIVE
            or row.provider != "tmux" or not row.instance_token
        ):
            raise TerminalStreamError("Terminal session is no longer available")
        task = await db.get_task(row.task_id) if row.task_id else None
        agent_id = row.agent_id or (task.assigned_agent_id if task else None)
        agent = await db.get_agent(agent_id) if agent_id else None
        if agent_id and (agent is None or agent.deleted_at is not None):
            raise TerminalStreamError("Terminal agent is no longer available")
        if row.task_id and (
            task is None or task.status not in _ACTIVE_TASKS
            or not agent or task.assigned_agent_id != agent.id
            or task.project_id != row.project_id
            or agent.current_task_id != task.id
            or (row.last_claim_epoch is not None and row.last_claim_epoch != task.claim_epoch)
        ):
            raise TerminalStreamError("Terminal task assignment has changed")
        if not row.task_id and agent and agent.current_task_id:
            raise TerminalStreamError("Terminal task assignment has changed")
        # Pool workers may claim a new task in the same process. Task sessions
        # cannot follow a later claim, including legacy rows without snapshots.
        identity = _generation(row) + (agent_id,)
        if row.lifecycle != "pool":
            identity += (row.task_id, task.claim_epoch if task else None)
        if generation is not None and identity != generation:
            raise TerminalStreamError("Terminal session instance has changed")
        return row, identity

    async def shutdown(self):
        tasks = list(self._handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _error(ws, message: str, code: int, accepted: bool):
        # A peer that stopped reading must not keep a PTY client alive while
        # we attempt to deliver its final error/close frames.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(1):
                if accepted:
                    await ws.send_json({
                        "type": "error", "message": message, "code": code,
                        "retryable": code in {1011, 4408, 4429},
                    })
                await ws.close(code=code, reason=message)

    async def handle(self, ws, session_id: str, *, input_only: bool = False):
        client = None
        accepted = False
        children = []
        current = asyncio.current_task()
        registered = False
        host_shell = is_host_shell_name(session_id)
        audited = False
        try:
            self._check_origin(ws)
            token = self._credentials(ws)
            await self._authorize(ws, token, host_shell=host_shell)
            if len(self._handlers) >= self.connection_limit:
                raise TerminalStreamError("Too many terminal connections", 4429)
            self._handlers.add(current)
            registered = True
            if not input_only:
                try:
                    cols, rows = _dimensions(
                        int(ws.query_params.get("cols", "80")),
                        int(ws.query_params.get("rows", "24")),
                    )
                except (TypeError, ValueError):
                    raise TerminalStreamError("Invalid terminal dimensions", 4400) from None
                history, restore_size = _phone_options(ws.query_params)
            row, generation = await self._session(session_id)
            await ws.accept(subprotocol=_PROTOCOL if _PROTOCOL in ws.scope.get("subprotocols", []) else None)
            accepted = True
            if host_shell:
                # Audit the accepted connection even if the attach backend fails.
                audited = True
                await audit_host_shell(
                    self, ws, "input_connected" if input_only else "attached", session_id,
                )
            provider = self.orchestrator.session_providers.create(row.provider)
            if input_only:
                client = await self.attach_input(provider, row)
                ready = {"type": "ready", "session_id": session_id, "mode": "input"}
            else:
                # Only a phone asks for these; other attach backends need not
                # know the keywords.
                options = {}
                if restore_size:
                    options["restore_size"] = True
                if history:
                    options["scrollback"] = True
                client = await self.attach(provider, row, cols=cols, rows=rows, **options)
                ready = {"type": "ready", "session_id": session_id, "cols": cols, "rows": rows}
            await self._session(session_id, generation)
            await self._authorize(ws, token, host_shell=host_shell)
            if not await client.verify():
                raise TerminalStreamError("Terminal session instance has changed")
            # Captured after the generation checks, so it is this instance's
            # history; it goes out first, under the same output credit.
            pending = b"" if input_only or not history else await client.history(history, rows)
            await asyncio.wait_for(ws.send_json(ready), self.ack_timeout)

            outstanding = 0
            queued_input = 0
            last_input_at = 0.0
            touched_input_at = 0.0
            credit = asyncio.Event()
            credit.set()
            inputs = asyncio.Queue(maxsize=64)

            async def receive():
                nonlocal outstanding, queued_input
                while True:
                    frame = await ws.receive()
                    if frame["type"] == "websocket.disconnect":
                        return "disconnect"
                    data = frame.get("bytes")
                    if data is not None:
                        if len(data) > _INPUT_FRAME_LIMIT:
                            raise TerminalStreamError("Terminal input frame is too large", 4400)
                        if not data:
                            continue
                        if queued_input + len(data) > _INPUT_QUEUE_LIMIT or inputs.full():
                            raise TerminalStreamError("Terminal input is arriving too quickly", 4400)
                        queued_input += len(data)
                        inputs.put_nowait(data)
                        continue
                    text = frame.get("text")
                    if not isinstance(text, str) or len(text) > 1024:
                        raise TerminalStreamError("Invalid terminal control", 4400)
                    try:
                        control = json.loads(text)
                    except (ValueError, RecursionError):
                        raise TerminalStreamError("Invalid terminal control", 4400) from None
                    if not isinstance(control, dict):
                        raise TerminalStreamError("Invalid terminal control", 4400)
                    if control.get("type") == "ack" and set(control) == {"type", "bytes"}:
                        count = control["bytes"]
                        if type(count) is not int or not (0 < count <= outstanding):
                            raise TerminalStreamError("Invalid terminal acknowledgement", 4400)
                        outstanding -= count
                        credit.set()
                    elif (
                        not input_only and control.get("type") == "resize"
                        and set(control) == {"type", "cols", "rows"}
                    ):
                        await client.resize(*_dimensions(control["cols"], control["rows"]))
                    elif control == {"type": "ping"}:
                        await asyncio.wait_for(ws.send_json({"type": "pong"}), self.ack_timeout)
                    else:
                        raise TerminalStreamError("Invalid terminal control", 4400)

            async def write_input():
                nonlocal queued_input, last_input_at
                while True:
                    data = await inputs.get()
                    await client.write(data)
                    last_input_at = time.time()
                    queued_input -= len(data)

            async def read_output():
                nonlocal outstanding, pending
                while True:
                    while outstanding >= self.output_limit:
                        credit.clear()
                        try:
                            await asyncio.wait_for(credit.wait(), self.ack_timeout)
                        except TimeoutError:
                            raise TerminalStreamError("Terminal output acknowledgement timed out", 4408) from None
                    budget = min(_OUTPUT_CHUNK, self.output_limit - outstanding)
                    if pending:
                        data, pending = pending[:budget], pending[budget:]
                    else:
                        data = await client.read(budget)
                        if not data:
                            return "exit"
                    outstanding += len(data)
                    try:
                        await asyncio.wait_for(ws.send_bytes(data), self.ack_timeout)
                    except TimeoutError:
                        raise TerminalStreamError("Terminal output transport timed out", 4408) from None

            async def monitor():
                nonlocal touched_input_at
                while True:
                    await asyncio.sleep(self.recheck_seconds)
                    await self._authorize(ws, token, host_shell=host_shell)
                    await self._session(session_id, generation)
                    if not await client.verify():
                        raise TerminalStreamError("Terminal session instance has changed")
                    if host_shell:
                        continue  # no sessions row to touch
                    # tmux window_activity measures output, not silent typing.
                    # Amortize persistence here; input never awaits a DB request.
                    observed_input = last_input_at
                    if observed_input > touched_input_at:
                        await self.orchestrator.db.touch_session_activity(session_id, observed_input)
                        touched_input_at = observed_input

            children = [asyncio.create_task(fn()) for fn in (receive, write_input, read_output, monitor)]
            done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            # Stop all I/O before any final control frame or ownership cleanup.
            for task in children:
                if task not in done:
                    task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            results = [task.result() for task in done]
            if "exit" in results:
                await asyncio.wait_for(ws.send_json({"type": "exit"}), 1)
                await asyncio.wait_for(ws.close(code=1000), 1)
        except WebSocketDisconnect:
            pass
        except (TerminalStreamError, TerminalAttachError) as exc:
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            await self._error(ws, str(exc), getattr(exc, "code", 4409), accepted)
        except Exception:
            # Never include provider stderr, credentials, command args or input.
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            await self._error(ws, "Terminal connection failed", 1011, accepted)
        finally:
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            if registered:
                self._handlers.discard(current)
            if audited:
                await audit_host_shell(
                    self, ws, "input_disconnected" if input_only else "detached", session_id,
                )


def build_terminal_router(orchestrator, config, *, token_store=None, **kwargs) -> APIRouter:
    service = TerminalStreamService(orchestrator, config, token_store=token_store, **kwargs)
    router = APIRouter()

    @router.get("/ws/terminal/{session_id}", operation_id="terminal_access")
    async def terminal_access(
        request: Request, session_id: str, response: Response, browser_origin: str | None = None,
    ) -> TerminalAccessResponse:
        # Same edge prefix and checks as the WS. No attach, input or side effects.
        # Browsers cannot read an HTTP rejection of a WebSocket handshake.
        response.headers["Cache-Control"] = "no-store"
        try:
            service._check_origin(request, browser_probe=True, browser_origin=browser_origin)
            token = service._credentials(request)
            await service._authorize(request, token, host_shell=is_host_shell_name(session_id))
            await service._session(session_id)
            if len(service._handlers) >= service.connection_limit:
                raise TerminalStreamError("Too many terminal connections", 4429)
            return TerminalAccessResponse(status="ready")
        except TerminalStreamError as exc:
            return TerminalAccessResponse(
                status="exited" if exc.code == 4409 else "error",
                code=exc.code, message=str(exc), retryable=exc.code == 4429,
            )

    @router.websocket("/ws/terminal/{session_id}")
    async def terminal(websocket: WebSocket, session_id: str):
        await service.handle(websocket, session_id)

    @router.websocket("/ws/terminal/{session_id}/input")
    async def terminal_input(websocket: WebSocket, session_id: str):
        # Phones type here and watch the pane stream: never an attach at phone size.
        await service.handle(websocket, session_id, input_only=True)

    from src.api.host_shell import add_host_shell_routes

    add_host_shell_routes(router, service)
    router.add_event_handler("shutdown", service.shutdown)
    return router
