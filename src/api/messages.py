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

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from src.api.auth import LOCAL_SCOPE
from src.api.dependencies import get_command_handler
from src.api.scope import check_command_scope
from src.messages.session_lens import _GLOBAL_SUPERVISOR_SUFFIX
from src.models import Project

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])


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


#: Logical session roles this build knows how to address.  ``planner`` and
#: ``reviewer`` join it when those sessions exist; until then an unknown role
#: is a 404, not a row queued to a session that will never read it.
KNOWN_SESSION_ROLES: tuple[str, ...] = ("supervisor",)


#: Role half of the global supervisor's address. Kept alongside
#: ``KNOWN_SESSION_ROLES`` so the two can't drift apart.
_SUPERVISOR_ROLE: str = "supervisor"


async def _ensure_global_project_stub(ch) -> None:
    """Idempotently create the stub ``projects`` row the global supervisor's
    rows hang off. Mirrors the create in :mod:`src.messages.session_lens`;
    whichever path runs first wins and the other is a no-op."""
    if await ch.db.get_project(_GLOBAL_SUPERVISOR_SUFFIX):
        return
    try:
        await ch.db.create_project(
            Project(id=_GLOBAL_SUPERVISOR_SUFFIX, name="Global")
        )
    except IntegrityError:
        # Raced with the session-lens create — the row is there either way.
        logger.debug("global project stub already present")


async def resolve_session_project(name: str, ch) -> str:
    """Map a logical session name to its project id.

    Logical names are ``<role>-<project_id>`` (``supervisor-agent-queue``).
    The split is on the **known role prefix**, not on hyphen position: the
    old left-to-right walk never validated the role, so ``junkrole-agent-queue``
    and even ``-agent-queue`` resolved happily, and ``code-reviewer-myproj``
    with projects ``{myproj, reviewer-myproj}`` picked the wrong one.

    ``supervisor-global`` is special: it addresses the *global* supervisor
    (the Agent Q chat at ``/`` in the dashboard), not a per-project supervisor
    for a project literally named "global" — see
    :mod:`src.messages.session_lens`. Its stub ``projects`` row is created
    lazily on the SessionLens cold-start path, which is only reachable by a
    request that already got past this resolver, so requiring the row up front
    deadlocked the global chat behind a permanent 404. Resolve it directly.

    Resolution is deliberately side-effect free: merely *reading* the global
    transcript must not conjure a ``projects`` row, or every dashboard load
    would add a phantom "Global" project to the sidebar. The write path calls
    :func:`_ensure_global_project_stub` instead.

    Raises ``HTTPException(404)`` when the role is unknown or the project
    doesn't exist.
    """
    if name == f"{_SUPERVISOR_ROLE}-{_GLOBAL_SUPERVISOR_SUFFIX}":
        return _GLOBAL_SUPERVISOR_SUFFIX

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
    if project_id == _GLOBAL_SUPERVISOR_SUFFIX:
        # ``messages.project_id`` is a NOT NULL FK to ``projects.id``, so the
        # stub has to exist before this row lands. Only on the write path —
        # see resolve_session_project.
        await _ensure_global_project_stub(ch)
    args: dict = {
        "project_id": project_id,
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
