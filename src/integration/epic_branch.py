"""Legible epic branch names, derived once and reserved against collisions."""

from __future__ import annotations

import re

from sqlalchemy import select

from src.database.tables import tasks

EPIC_PREFIX = "aq/epic/"
TASK_PREFIX = "aq/"
MAX_SLUG_LENGTH = 60
FALLBACK_SLUG = "epic"

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """Return a lowercase hyphenated slug, truncated on a word boundary."""
    slug = _NON_WORD.sub("-", title.strip().lower()).strip("-")
    if not slug:
        return FALLBACK_SLUG
    if len(slug) <= MAX_SLUG_LENGTH:
        return slug
    head = slug[:MAX_SLUG_LENGTH]
    if "-" in head:
        head = head.rsplit("-", 1)[0]
    return head.strip("-") or FALLBACK_SLUG


async def reserve_branch_name(conn, *, task_id: str, title: str, is_epic: bool) -> str:
    """Reserve this task's branch in its repository; never change an existing name."""
    row = (
        await conn.execute(
            select(tasks.c.branch_name, tasks.c.repo_id).where(tasks.c.id == task_id)
        )
    ).one()
    if row.branch_name:
        return row.branch_name

    if not is_epic:
        name = f"{TASK_PREFIX}{task_id}"
    else:
        base = slugify(title)
        taken = set(
            (
                await conn.execute(
                    select(tasks.c.branch_name).where(
                        tasks.c.repo_id == row.repo_id,
                        tasks.c.branch_name.like(f"{EPIC_PREFIX}{base}%"),
                    )
                )
            ).scalars()
        )
        name = f"{EPIC_PREFIX}{base}"
        suffix = 1
        while name in taken:
            suffix += 1
            name = f"{EPIC_PREFIX}{base}-{suffix}"

    await conn.execute(tasks.update().where(tasks.c.id == task_id).values(branch_name=name))
    return name
