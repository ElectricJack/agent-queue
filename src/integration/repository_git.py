"""Retained repository reads shared by train reviews and release admission.

This port resolves canonical repositories and reads their Git objects. It owns no
integration schedule, subject, candidate, promotion intent or branch publication.
"""
from __future__ import annotations

import hashlib
import inspect
import re
from pathlib import Path

from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError
from src.integration.promotion_contracts import (
    PromotionInvariantError, PromotionRuntimeError, PromotionSourceMoved,
    ResolvedRepository,
)
from src.models import RepoConfig, RepoSourceType

_OID_RE = re.compile(r"^[0-9a-f]{40}$")


class RepositoryGit:
    def __init__(self, db, *, data_dir, git_manager, repository_resolver=None):
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager
        self.repository_resolver = repository_resolver

    async def _get_repo(self, repository_id: str) -> RepoConfig | None:
        result = (
            self.repository_resolver(repository_id)
            if self.repository_resolver is not None
            else self.db.get_repo(repository_id)
        )
        if inspect.isawaitable(result):
            result = await result
        return result


    async def _resolve_repository(self, repository_id: str) -> ResolvedRepository:
        repo = await self._get_repo(repository_id)
        if repo is None or repo.id != repository_id:
            raise PromotionInvariantError("canonical repository is not configured")
        origin = repo.url
        if not origin and repo.source_type is RepoSourceType.LINK:
            source = Path(repo.source_path)
            if not source.is_dir():
                raise PromotionInvariantError("linked repository source path is unavailable")
            result = await self.git.arun_git_result(
                ["remote", "get-url", "origin"], cwd=str(source), env={"LC_ALL": "C"}
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise PromotionInvariantError("linked repository origin is unavailable")
            origin = result.stdout.strip()
        if not origin:
            raise PromotionInvariantError("canonical repository has no immutable origin")
        digest = hashlib.sha256(repository_id.encode("utf-8")).hexdigest()
        return ResolvedRepository(
            repo=repo,
            origin_url=origin,
            retained_git_dir=self.data_dir / "integration-repositories" / f"{digest}.git",
        )


    async def _ensure_retained_repository(self, repository: ResolvedRepository) -> None:
        store = repository.retained_git_dir
        store.parent.mkdir(parents=True, exist_ok=True)
        async with self.git.arepository_transaction(str(store)):
            if not store.exists():
                try:
                    await self.git.acreate_bare_checkout(repository.origin_url, str(store))
                except (GitError, GitHubAccessError) as exc:
                    raise PromotionRuntimeError(f"retained clone failed: {exc}") from exc
            bare = await self.git.arun_git_result(
                ["rev-parse", "--is-bare-repository"],
                cwd=str(store),
                env={"LC_ALL": "C"},
                lock_held=True,
            )
            configured = await self.git.arun_git_result(
                ["remote", "get-url", "origin"],
                cwd=str(store),
                env={"LC_ALL": "C"},
                lock_held=True,
            )
            if (
                bare.returncode != 0
                or bare.stdout.strip() != "true"
                or configured.returncode != 0
                or configured.stdout.strip() != repository.origin_url
            ):
                raise PromotionInvariantError("retained repository identity changed")


    async def _fetch_all_heads(self, store: Path, origin_url: str) -> None:
        try:
            await self.git.afetch_origin(
                str(store), repository_url=origin_url, lock_held=True,
                all_heads=True,
            )
        except (GitError, GitHubAccessError) as exc:
            raise PromotionRuntimeError(f"git fetch failed: {exc}") from exc


    async def _is_ancestor(self, store: Path, ancestor: str, descendant: str) -> bool:
        result = await self.git.arun_git_result(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        if result.returncode not in {0, 1}:
            raise PromotionRuntimeError((result.stderr or "ancestry check failed").strip())
        return result.returncode == 0


    async def _tree_oid(self, store: Path, commit: str) -> str:
        result = await self.git.arun_git_result(
            ["rev-parse", f"{commit}^{{tree}}"],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        oid = result.stdout.strip().lower()
        if result.returncode != 0 or not _OID_RE.fullmatch(oid):
            raise PromotionSourceMoved("reviewed source tree cannot be resolved")
        return oid

