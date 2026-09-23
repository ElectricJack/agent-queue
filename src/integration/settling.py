"""A bounded window for batching project integration approvals."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from src.database.tables import project_integration_schedules

SETTLING_EXTENSION_SECONDS = 300.0
SETTLING_CAP_SECONDS = 1800.0


async def note_approval(conn, *, project_id: str, now: float) -> dict[str, Any]:
    """Arm or extend a project's window without moving its first-approval cap."""
    row = (
        await conn.execute(
            select(
                project_integration_schedules.c.settling_first_approval_at,
                project_integration_schedules.c.settling_fires_at,
            )
            .where(project_integration_schedules.c.project_id == project_id)
            .with_for_update()
        )
    ).one()

    first_approval_at = row.settling_first_approval_at
    if first_approval_at is None:
        first_approval_at = now
        outcome = "armed"
    else:
        outcome = "extended"

    cap = first_approval_at + SETTLING_CAP_SECONDS
    fires_at = now + SETTLING_EXTENSION_SECONDS
    if row.settling_fires_at is not None:
        fires_at = max(fires_at, row.settling_fires_at)
    fires_at = min(fires_at, cap)
    if outcome == "extended" and fires_at == cap:
        outcome = "capped"

    await conn.execute(
        update(project_integration_schedules)
        .where(project_integration_schedules.c.project_id == project_id)
        .values(settling_first_approval_at=first_approval_at, settling_fires_at=fires_at)
    )
    return {
        "outcome": outcome,
        "project_id": project_id,
        "first_approval_at": first_approval_at,
        "fires_at": fires_at,
    }


async def settled(conn, *, project_id: str, now: float) -> bool:
    """Return whether an armed window has reached its firing time."""
    fires_at = (
        await conn.execute(
            select(project_integration_schedules.c.settling_fires_at).where(
                project_integration_schedules.c.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    return fires_at is not None and now >= fires_at


async def clear(conn, *, project_id: str) -> None:
    """Disarm the window after a batch seals or is abandoned."""
    await conn.execute(
        update(project_integration_schedules)
        .where(project_integration_schedules.c.project_id == project_id)
        .values(settling_first_approval_at=None, settling_fires_at=None)
    )
