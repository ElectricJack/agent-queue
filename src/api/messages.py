"""Chat relay endpoints — ``/api/sessions/{name}/message[s]``.

An explicit router, not codegen: the path carries a session name, which the
auto-generated ``POST /api/{category}/{command}`` shape can't express.  See
docs/specs/implementation/supervisor-agent.md §6.2.

Both endpoints are thin translations onto the ``messages`` command surface,
so the Discord adapter, the dashboard chat page and ``aq chat`` all end up
writing the same rows through the same validation (design §5, §7).

``name`` is the **logical** session name — ``supervisor-<project_id>`` for a
project's supervisor.  The provider-level name (tmux et al.) is owned by the
session-runtime spec and is deliberately not referenced here.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.auth import LOCAL_SCOPE
from src.api.dependencies import get_command_handler
from src.api.scope import check_command_scope
from src.api.models.message import MessageModel
from src.messages.session_lens import _GLOBAL_SUPERVISOR_SUFFIX

router = APIRouter(tags=["sessions"])


class MessageSendRequest(BaseModel):
    """Body of POST /api/messages/send for non-session recipients."""

    project_id: str | None = Field(default=None, description="Owning project id")
    to_kind: str | None = Field(
        default=None, description="Recipient kind; omit for collaboration fanout"
    )
    to_id: str | None = Field(default=None, description="Recipient id")
    body: str = Field(description="Markdown message body")
    from_id: str | None = Field(default=None, description="Authorized sender id; omit for server default")
    from_kind: str | None = Field(
        default=None, description="Authorized sender kind; omit for server default"
    )
    subject: str | None = Field(default=None)
    thread_id: str | None = Field(default=None)
    client_key: str | None = Field(default=None, min_length=1, max_length=128)
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)
    priority: int = Field(default=100)
    archive_after_inject: bool = Field(default=False)
    pane_open: dict | None = Field(default=None)
    system_only: bool = Field(default=False, description="Request projectless system scope")


class SessionMessageRequest(BaseModel):
    """Body of ``POST /api/sessions/{name}/message`` (design §7)."""

    body: str = Field(..., description="Markdown message body")
    # ``from`` is a Python keyword, hence the alias.
    from_: str | None = Field(
        default=None,
        alias="from",
        description="Authorized sender id; omit for server default",
    )
    from_kind: str | None = Field(
        default=None,
        description="Authorized sender kind: session | user | system; omit for server default",
    )
    thread_id: str | None = Field(default=None, description="Conversation grouping key")
    subject: str | None = Field(default=None, description="Optional subject line")
    priority: int = Field(default=100, description="Delivery ordering, lower first")
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)

    model_config = {"populate_by_name": True}


class SessionMessageResponse(BaseModel):
    """Response shape from design §7."""

    success: bool = True
    message_id: str
    state: str  # "queued" | "delivered"


class SessionDeliveryDiagnostic(BaseModel):
    at: float
    reason: str
    deferred: bool = False


class SessionMessagesResponse(BaseModel):
    success: bool = True
    session: str
    project_id: str | None = None
    count: int
    messages: list[MessageModel]
    delivery_failure: SessionDeliveryDiagnostic | None = None


@router.post("/api/messages/send")
async def post_message(
    payload: MessageSendRequest,
    request: Request,
    ch=Depends(get_command_handler),
) -> dict:
    """Queue a message to any supported recipient kind."""
    scope = getattr(request.state, "scope", LOCAL_SCOPE)
    args = payload.model_dump(exclude_none=True)
    scope_err = check_command_scope("message_send", args, scope)
    if scope_err is not None:
        return JSONResponse({"error": scope_err}, status_code=403)
    args["_scope"] = asdict(scope)
    result = await ch.execute("message_send", args)
    if "error" in result:
        return JSONResponse(result, status_code=422)
    return {"success": True, **result}


#: Logical session roles this build knows how to address.  ``planner`` and
#: ``reviewer`` join it when those sessions exist; until then an unknown role
#: is a 404, not a row queued to a session that will never read it.
KNOWN_SESSION_ROLES: tuple[str, ...] = ("supervisor",)


#: Role half of the global supervisor's address. Kept alongside
#: ``KNOWN_SESSION_ROLES`` so the two can't drift apart.
_SUPERVISOR_ROLE: str = "supervisor"


async def resolve_session_project(name: str, ch) -> str | None:
    """Map a logical session name to its project id.

    Logical names are ``<role>-<project_id>`` (``supervisor-agent-queue``).
    The split is on the **known role prefix**, not on hyphen position: the
    old left-to-right walk never validated the role, so ``junkrole-agent-queue``
    and even ``-agent-queue`` resolved happily, and ``code-reviewer-myproj``
    with projects ``{myproj, reviewer-myproj}`` picked the wrong one.

    ``supervisor-global`` has system scope (None), independent of projects.
    Resolution is read-only; neither reading nor writing global chat creates
    a placeholder project.

    Raises ``HTTPException(404)`` when the role is unknown or the project
    doesn't exist.
    """
    if name == f"{_SUPERVISOR_ROLE}-{_GLOBAL_SUPERVISOR_SUFFIX}":
        return None

    for role in KNOWN_SESSION_ROLES:
        prefix = f"{role}-"
        if not name.startswith(prefix):
            continue
        candidate = name[len(prefix) :]
        if candidate and await ch.db.get_project(candidate):
            return candidate
    raise HTTPException(
        status_code=404,
        detail=(
            f"Unknown session name '{name}' (expected <role>-<project_id>, "
            f"role one of: {', '.join(KNOWN_SESSION_ROLES)})"
        ),
    )


@router.post("/api/sessions/{name}/message", response_model=SessionMessageResponse)
async def post_session_message(
    name: str,
    payload: SessionMessageRequest,
    request: Request,
    ch=Depends(get_command_handler),
) -> SessionMessageResponse:
    """Queue a message addressed to the named session."""
    scope = getattr(request.state, "scope", LOCAL_SCOPE)
    project_id = await resolve_session_project(name, ch)
    args: dict = {
        "project_id": project_id,
        "system_only": project_id is None,
        "to_kind": "session",
        "to_id": name,
        "body": payload.body,
        "subject": payload.subject,
        "thread_id": payload.thread_id,
        "priority": payload.priority,
    }
    if payload.from_kind is not None:
        args["from_kind"] = payload.from_kind
    elif scope.kind != "session":
        args["from_kind"] = "user"
    if payload.from_ is not None:
        args["from_id"] = payload.from_
    elif scope.kind != "session":
        args["from_id"] = "user"
    if payload.claim_epoch is not None:
        args["claim_epoch"] = payload.claim_epoch
    scope_err = check_command_scope("message_send", args, scope)
    if scope_err is not None:
        return JSONResponse({"error": scope_err}, status_code=403)
    args["_scope"] = asdict(scope)

    result = await ch.execute("message_send", args)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    return SessionMessageResponse(
        message_id=result["message_id"],
        state=result.get("state", "queued"),
    )


@router.get("/api/sessions/{name}/messages", response_model=SessionMessagesResponse)
async def get_session_messages(
    name: str,
    request: Request,
    thread_id: str | None = Query(default=None),
    since: float | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    include_archived: bool = Query(default=False),
    supervisor_only: bool = Query(default=False),
    ch=Depends(get_command_handler),
) -> dict:
    """Poll the conversation for a session.

    Returns every message in the session's project matching the filters —
    both directions of the conversation — so a poller (``aq chat --once``
    when the WebSocket isn't available) sees the reply as well as its own
    outbound message.
    """
    scope = getattr(request.state, "scope", LOCAL_SCOPE)
    project_id = await resolve_session_project(name, ch)
    args: dict = {
        "project_id": project_id,
        "system_only": project_id is None,
        "thread_id": thread_id,
        "since": since,
        "limit": limit,
        "include_archived": include_archived,
    }
    scope_err = check_command_scope("message_list", args, scope)
    if scope_err is not None:
        return JSONResponse({"error": scope_err}, status_code=403)
    args["_scope"] = asdict(scope)

    failure = None
    if supervisor_only:
        # Server-derived addresses filter before LIMIT so worker traffic cannot
        # crowd supervisor mail out of the recent conversation. Includes historical
        # UUID recipients and replies; a logical address still sends via aq chat.
        records = [s for s in await ch.db.list_sessions()
                   if s.profile_id == "supervisor" and s.project_id == project_id
                   and s.lifecycle == "named"]
        args["session_addresses"] = [name, *(s.id for s in records)]
        lens = getattr(ch.orchestrator, "session_lens", None)
        if lens is not None:
            diagnostics = [lens.nudge_failure(s.id) for s in records
                           if s.state in {"starting", "running", "draining"}]
            failure = max((d for d in diagnostics if d), key=lambda d: d["at"], default=None)

    result = await ch.execute("message_list", args)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    return {
        "success": True,
        "session": name,
        "project_id": project_id,
        "count": result.get("count", 0),
        "messages": result.get("messages", []),
        "delivery_failure": failure,
    }
