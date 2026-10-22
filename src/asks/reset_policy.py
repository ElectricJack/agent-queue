"""The project reset is a discard boundary, never transport or decision evidence."""

import json

from sqlalchemy import cast, Float, select
from sqlalchemy.dialects.postgresql import JSONB

from src.database.tables import system_config


def reset_key(project):
    return f"ask-legacy-reset:v1:{project}"


async def reset_state(conn, project):
    raw = await conn.scalar(
        select(system_config.c.value).where(
            system_config.c.key == reset_key(project),
        )
    )
    return json.loads(raw) if raw else None


def before_reset(table, project):
    """Correlated project watermark, including reads spanning all projects."""
    cutoff = cast(cast(system_config.c.value, JSONB)["cutoff"].astext, Float)
    stmt = (
        select(system_config.c.key)
        .where(
            system_config.c.key == "ask-legacy-reset:v1:" + project,
            table.c.created_at <= cutoff,
        )
        .correlate(table)
    )
    if hasattr(project, "table"):
        stmt = stmt.correlate(project.table)
    return stmt.exists()


async def skip_row(conn, table, row, project):
    if not project:
        return False
    state = await reset_state(conn, project)
    if not state or row.get("created_at", float("inf")) > state["cutoff"]:
        return False
    return not (table.name == "doc_reviews" and row["id"] in state["kept_reviews"])
