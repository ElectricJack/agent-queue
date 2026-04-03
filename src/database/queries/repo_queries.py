"""Repository configuration CRUD operations."""

from __future__ import annotations

from src.models import RepoConfig, RepoSourceType


class RepoQueryMixin:
    """Query mixin for repo operations.  Expects ``self._db``."""

    async def create_repo(self, repo: RepoConfig) -> None:
        """Insert a new repo configuration."""
        await self._db.execute(
            "INSERT INTO repos (id, project_id, url, default_branch, "
            "checkout_base_path, source_type, source_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                repo.id,
                repo.project_id,
                repo.url,
                repo.default_branch,
                repo.checkout_base_path,
                repo.source_type.value,
                repo.source_path,
            ),
        )
        await self._db.commit()

    async def get_repo(self, repo_id: str) -> RepoConfig | None:
        """Fetch a single repo by ID."""
        cursor = await self._db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_repo(row)

    async def list_repos(self, project_id: str | None = None) -> list[RepoConfig]:
        """List repos, optionally filtered by project."""
        if project_id:
            cursor = await self._db.execute(
                "SELECT * FROM repos WHERE project_id = ?", (project_id,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM repos")
        rows = await cursor.fetchall()
        return [self._row_to_repo(r) for r in rows]

    async def update_repo(self, repo_id: str, **kwargs) -> None:
        """Update repo config fields (e.g. default_branch, url)."""
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, RepoSourceType):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(repo_id)
        await self._db.execute(f"UPDATE repos SET {', '.join(sets)} WHERE id = ?", vals)
        await self._db.commit()

    async def delete_repo(self, repo_id: str) -> None:
        """Delete a repo configuration."""
        await self._db.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        await self._db.commit()

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
            source_path=row["source_path"] if "source_path" in row.keys() else "",
            checkout_base_path=row["checkout_base_path"]
            if "checkout_base_path" in row.keys()
            else "",
            default_branch=row["default_branch"],
        )
