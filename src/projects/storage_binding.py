"""Read one durable project storage generation; configuration is the legacy default."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from src.database.tables import projects


def bound_path(binding: dict | None, path: str | Path) -> Path:
    path = Path(path).expanduser().absolute()
    for old, new in sorted((binding or {}).get("paths", {}).items(),
                           key=lambda pair: len(pair[0]), reverse=True):
        if path.is_relative_to(old):
            return Path(new) / path.relative_to(old)
    return path


async def read_binding(db, project_id: str, *, conn=None) -> dict | None:
    statement = select(projects.c.storage_binding).where(projects.c.id == project_id)
    if conn is not None:
        return (await conn.execute(statement)).scalar_one_or_none()
    async with db._engine.connect() as connection:
        return (await connection.execute(statement)).scalar_one_or_none()


async def repository_path(db, repository_id: str, path: str | Path) -> Path:
    from src.database.tables import repos

    # A single statement pins the repository's project and its storage generation.
    async with db._engine.connect() as conn:
        binding = (await conn.execute(select(projects.c.storage_binding).join(
            repos, repos.c.project_id == projects.c.id,
        ).where(repos.c.id == repository_id))).scalar_one_or_none()
    return bound_path(binding, path)
