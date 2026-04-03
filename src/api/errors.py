"""Error handling for the agent-queue REST API."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


async def command_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unhandled exceptions in API routes."""
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"},
    )
