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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.auth import LOCAL_SCOPE
from src.api.dependencies import get_command_handler
from src.api.scope import check_command_scope
from src.messages.session_lens import _GLOBAL_SUPERVISOR_SUFFIX

router = APIRouter(tags=["sessions"])


class MessageSendRequest(BaseModel):
    """Body of POST /api/messages/send for non-session recipients."""

    project_id: str | None = Field(default=None, description="Owning project id")
    to_kind: str = Field(description="Recipient kind: session | task | profile | user")
    to_id: str = Field(description="Recipient id")
    body: str = Field(description="Markdown message body")
    from_id: str = Field(default="cli", description="Sender id")
    from_kind: str = Field(default="user", description="Sender kind")
    subject: str | None = Field(default=None)
    thread_id: str | None = Field(default=None)
    priority: int = Field(default=100)
    archive_after_inject: bool = Field(default=False)
    pane_open: dict | None = Field(default=None)
    system_only: bool = Field(default=False, description="Request projectless system scope")


class SessionMessageRequest(BaseModel):
    """Body of ``POST /api/sessions/{name}/message`` (design §7)."""

    body: str = Field(..., description="Markdown message body")
    # ``from`` is a Python keyword, hence the alias.
    from_: str = Field(
        default="user",
        alias="from",
        description="Sender id, e.g. 'discord:1234' or 'cli'",
    )
    from_kind: str = Field(
        default="user",
        description="Sender kind: session | user | system",
    )
    thread_id: str | None = Field(default=None, description="Conversation grouping key")
    subject: str | None = Field(default=None, description="Optional subject line")
    priority: int = Field(default=100, description="Delivery ordering, lower first")

    model_config = {"populate_by_name": True}


class SessionMessageResponse(BaseModel):
    """Response shape from design §7."""

    success: bool = True
    message_id: str
    state: str  # "queued" | "delivered"


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
    args["_scope"] = {
        "kind": scope.kind,
        "session_id": scope.session_id,
        "task_id": scope.task_id,
        "project_id": scope.project_id,
        "elevated": scope.elevated,
    }
    result = await ch.execute("message_send", args)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=422)
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
        "from_kind": payload.from_kind,
        "from_id": payload.from_,
        "body": payload.body,
        "subject": payload.subject,
        "thread_id": payload.thread_id,
        "priority": payload.priority,
    }
    args.pop("_scope", None)
    scope_err = check_command_scope("message_send", args, scope)
    if scope_err is not None:
        return JSONResponse({"error": scope_err}, status_code=403)
    args["_scope"] = {
        "kind": scope.kind,
        "session_id": scope.session_id,
        "task_id": scope.task_id,
        "project_id": scope.project_id,
        "elevated": scope.elevated,
    }

    result = await ch.execute("message_send", args)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    return SessionMessageResponse(
        message_id=result["message_id"],
        state=result.get("state", "queued"),
    )


@router.get("/api/sessions/{name}/messages")
async def get_session_messages(
    name: str,
    request: Request,
    thread_id: str | None = Query(default=None),
    since: float | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    include_archived: bool = Query(default=False),
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
    args.pop("_scope", None)
    scope_err = check_command_scope("message_list", args, scope)
    if scope_err is not None:
        return JSONResponse({"error": scope_err}, status_code=403)
    args["_scope"] = {
        "kind": scope.kind,
        "session_id": scope.session_id,
        "task_id": scope.task_id,
        "project_id": scope.project_id,
        "elevated": scope.elevated,
    }

    result = await ch.execute("message_list", args)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    return {
        "success": True,
        "session": name,
        "project_id": project_id,
        "count": result.get("count", 0),
        "messages": result.get("messages", []),
    }
