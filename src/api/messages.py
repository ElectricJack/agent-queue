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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.dependencies import get_command_handler

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


async def resolve_session_project(name: str, ch) -> str:
    """Map a logical session name to its project id.

    Logical names are ``<role>-<project_id>`` (``supervisor-agent-queue``).
    Project ids themselves contain hyphens, so the split is resolved against
    the project table rather than guessed: the leftmost split whose suffix is
    a real project wins.

    Raises ``HTTPException(404)`` when no split resolves.
    """
    for idx, char in enumerate(name):
        if char != "-":
            continue
        candidate = name[idx + 1 :]
        if not candidate:
            continue
        if await ch.db.get_project(candidate):
            return candidate
    raise HTTPException(
        status_code=404,
        detail=f"Unknown session name '{name}' (expected <role>-<project_id>)",
    )


@router.post("/api/sessions/{name}/message", response_model=SessionMessageResponse)
async def post_session_message(
    name: str,
    payload: SessionMessageRequest,
    ch=Depends(get_command_handler),
) -> SessionMessageResponse:
    """Queue a message addressed to the named session."""
    project_id = await resolve_session_project(name, ch)

    result = await ch.execute(
        "message_send",
        {
            "project_id": project_id,
            "to_kind": "session",
            "to_id": name,
            "from_kind": payload.from_kind,
            "from_id": payload.from_,
            "body": payload.body,
            "subject": payload.subject,
            "thread_id": payload.thread_id,
            "priority": payload.priority,
        },
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    return SessionMessageResponse(
        message_id=result["message_id"],
        state=result.get("state", "queued"),
    )


@router.get("/api/sessions/{name}/messages")
async def get_session_messages(
    name: str,
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
    project_id = await resolve_session_project(name, ch)

    result = await ch.execute(
        "message_list",
        {
            "project_id": project_id,
            "thread_id": thread_id,
            "since": since,
            "limit": limit,
            "include_archived": include_archived,
        },
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    return {
        "success": True,
        "session": name,
        "project_id": project_id,
        "count": result.get("count", 0),
        "messages": result.get("messages", []),
    }
