"""Repository configuration CRUD operations."""

from __future__ import annotations

import json
from dataclasses import asdict

from sqlalchemy import delete, insert, select, update

from src.database.tables import repos
from src.models import RepoConfig, RepoSourceType


class RepoQueryMixin:
    """Query mixin for repo operations.  Expects ``self._engine``."""

    async def create_repo(self, repo: RepoConfig) -> None:
        """Insert a new repo configuration."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(repos).values(
                    id=repo.id,
                    project_id=repo.project_id,
                    url=repo.url,
                    default_branch=repo.default_branch,
                    checkout_base_path=repo.checkout_base_path,
                    source_type=repo.source_type.value,
                    source_path=repo.source_path,
                )
            )

    async def get_repo(self, repo_id: str) -> RepoConfig | None:
        """Fetch a single repo by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(repos).where(repos.c.id == repo_id))
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_repo(row)

    async def list_repos(self, project_id: str | None = None) -> list[RepoConfig]:
        """List repos, optionally filtered by project."""
        stmt = select(repos)
        if project_id:
            stmt = stmt.where(repos.c.project_id == project_id)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_repo(r) for r in result.mappings().fetchall()]

    async def update_repo(self, repo_id: str, **kwargs) -> None:
        """Update repo config fields (e.g. default_branch, url)."""
        values = {}
        for key, value in kwargs.items():
            if isinstance(value, RepoSourceType):
                value = value.value
            values[key] = value
        async with self._engine.begin() as conn:
            await conn.execute(update(repos).where(repos.c.id == repo_id).values(**values))

    async def delete_repo(self, repo_id: str) -> None:
        """Delete a repo configuration."""
        async with self._engine.begin() as conn:
            await conn.execute(delete(repos).where(repos.c.id == repo_id))

    async def repair_repository_checkout(
        self, expected: RepoConfig, *, after: dict, reason: str, operator_id: str,
    ) -> dict:
        """CAS the full configuration and commit its audit event atomically."""
        before = {**asdict(expected), "source_type": expected.source_type.value}
        changes = {key: after[key] for key in ("source_path", "checkout_base_path")
                   if before[key] != after[key]}
        if not changes or {**before, **changes} != after or not reason or not operator_id:
            raise ValueError("checkout repair may only change paths and requires attribution")
        async with self._engine.begin() as conn:
            row = (await conn.execute(select(repos).where(
                repos.c.id == expected.id,
            ).with_for_update())).mappings().one_or_none()
            if row is None or self._row_to_repo(row) != expected:
                return {"success": False, "error_code": "repository_checkout_stale",
                        "error": "Repository configuration changed; preview the repair again"}
            await conn.execute(update(repos).where(repos.c.id == expected.id).values(**changes))
            event_id = await self.log_event(
                "project.repository_checkout_repaired", project_id=expected.project_id,
                payload=json.dumps({"repository_id": expected.id, "before": before,
                                    "after": after, "reason": reason,
                                    "operator_id": operator_id}, sort_keys=True), conn=conn,
            )
        return {"success": True, "project_id": expected.project_id, "repository_id": expected.id,
                "changed": True, "preview": False, "before": before, "after": after,
                "event_id": event_id}

    @staticmethod
    def _row_to_repo(row) -> RepoConfig:
        """Convert a database row to a RepoConfig model."""
        return RepoConfig(
            id=row["id"],
            project_id=row["project_id"],
            source_type=RepoSourceType(row["source_type"])
            if row["source_type"]
            else RepoSourceType.CLONE,
            url=row["url"],
            source_path=row["source_path"] if row["source_path"] else "",
            checkout_base_path=row["checkout_base_path"] if row["checkout_base_path"] else "",
            default_branch=row["default_branch"],
        )
