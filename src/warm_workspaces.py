"""Reusable linked checkouts; callers supply their durable ownership fence.

The owner manifest lives outside the checkout. An interrupted preparation is
retained, never adopted merely because a directory looks old or clean. Callers
serialize allocation for the repository and prove the previous owner released
it. This port is also usable by author-assisted close-merge candidates.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from src.git.manager import GitError
from src.jobs.artifacts import atomic_json, read_json


async def primary_store(db, git, project_id, repository_id, fallback, oid):
    """Share the primary clone's objects without changing its index or refs."""
    repo = await db.get_repo(repository_id) if repository_id else None
    if repo and repo.project_id != project_id:
        raise GitError("primary repository belongs to another project")
    path = repo.checkout_base_path if repo else ""
    if repo and not path and Path(repo.source_path).is_dir():
        path = repo.source_path
    store = Path(path or fallback).expanduser().resolve()
    if path and store != Path(fallback).resolve():
        if not store.is_dir():
            raise GitError("primary repository is unavailable")
        await git._arun([
            "fetch", "--no-tags", "--no-write-fetch-head", str(Path(fallback).resolve()), oid,
        ], cwd=str(store))
    return store


class WarmWorkspacePool:
    def __init__(self, git, store, root):
        self.git, self.store = git, Path(store).resolve()
        self.root = Path(root).expanduser().absolute()
        self.key = hashlib.sha256(str(self.store).encode()).hexdigest()

    async def acquire(self, owner, oid, *, released, branch=None):
        """Return a checkout under the caller's repository allocation lock.

        ``released(manifest, path, head)`` must prove a stopped writer, no pins
        and preservation of its committed work. Dirty or foreign checkouts are
        skipped even when the caller reports release. Same-owner replay leaves
        all files intact so the caller can reconcile uncertain execution.
        """
        manifests = self.root / ".owners"
        if self.root.is_symlink() or manifests.is_symlink():
            raise GitError("warm workspace root must not be a symlink")
        await asyncio.to_thread(manifests.mkdir, parents=True, exist_ok=True)
        records = await asyncio.to_thread(lambda: sorted(manifests.glob(self.key + "-*.json")))
        for record in records:
            if record.is_symlink():
                continue
            try:
                manifest = await asyncio.to_thread(read_json, record)
            except (OSError, ValueError):
                continue
            if (not isinstance(manifest, dict) or not isinstance(manifest.get("owner"), dict)
                    or not isinstance(manifest.get("oid"), str)
                    or manifest.get("store") != str(self.store)):
                continue
            path = self.root / record.stem
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                common = await self.git._arun(
                    ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(path),
                )
                expected = await self.git._arun(
                    ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(self.store),
                )
                if Path(common.strip()).resolve() != Path(expected.strip()).resolve():
                    continue
                if manifest["owner"] == owner:
                    return path
                head = await self.git.arev_parse(str(path), "HEAD")
                if not await released(manifest, path, head):
                    continue
                if (await self.git._arun(["status", "--porcelain", "--untracked-files=all"],
                                        cwd=str(path))).strip():
                    continue
                # Refuse an ignored output that the next tree would overwrite.
                await self.git._arun(
                    ["checkout", "--no-overwrite-ignore", "--detach", oid], cwd=str(path),
                )
                await self.git._arun(["reset", "--hard", oid], cwd=str(path))
                if branch:
                    await self._branch(path, branch, oid)
            except GitError:
                continue
            await asyncio.to_thread(atomic_json, record, {
                "store": str(self.store), "owner": owner, "oid": oid,
            })
            return path
        index = 0
        while True:
            path = self.root / f"{self.key}-{index}"
            record = manifests / (path.name + ".json")
            if (not path.exists() and not path.is_symlink()
                    and not record.exists() and not record.is_symlink()):
                break
            index += 1
        # Reserve before creating anything: a crash retains preparation.
        await asyncio.to_thread(atomic_json, record, {
            "store": str(self.store), "owner": owner, "oid": oid,
        })
        await self.git.aworktree_add(str(self.store), str(path), ref=oid)
        if branch:
            await self._branch(path, branch, oid)
        return path

    async def _branch(self, path, branch, oid):
        current = await self.git.arev_parse(str(path), "refs/heads/" + branch)
        if current is None:
            await self.git._arun(["checkout", "-b", branch, oid], cwd=str(path))
        elif current == oid:
            await self.git.acheckout_branch(str(path), branch)
        else:
            raise GitError("warm workspace branch moved; preserve its head")
