"""Copy immutable provenance out of branch refs before deleting legacy copies.

Invoked only by the operator command. Remote inventory lives in a temporary
checkout; preview never mutates a managed checkout or the remote repository.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from src.git.manager import GitError, RemoteRefState
from src.integration.provenance import (
    LEGACY_PREFIX,
    PREFIX,
    CompletionIdentity,
    GitProvenance,
    _json,
)


def _record_ref(record: dict) -> str:
    if record["kind"] == "completion":
        return CompletionIdentity(**record["identity"]).ref
    return PREFIX + "replacements/" + hashlib.sha256(_json(record).encode()).hexdigest()


class ProvenanceMigration:
    def __init__(self, git):
        self.git = git

    async def run(
        self, repository_url: str, *, dry_run: bool = True, limit: int = 50,
        checkout: str | None = None,
    ) -> dict:
        """A bounded page, repeatable until no legacy refs remain.

        There is no offset on apply: each successful page removes its old refs.
        A blocked row stays visible and must be resolved before it can disappear.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if checkout is not None:
            local = GitProvenance(self.git, checkout, repository_url=repository_url)
            if await local.run("remote", "get-url", "origin") != repository_url:
                raise ValueError("local checkout does not name the authorized repository")
        rows = []
        with tempfile.TemporaryDirectory(prefix="aq-provenance-migration-") as temporary:
            path = str(Path(temporary) / "repository")
            await self.git.acreate_checkout(repository_url, path, no_checkout=True)
            await self.git.afetch_origin(path, repository_url=repository_url, all_heads=True)
            store = GitProvenance(self.git, path, repository_url=repository_url)
            legacy = "refs/remotes/origin/" + LEGACY_PREFIX
            inventory = await store.run(
                "for-each-ref", "--format=%(refname) %(objectname)", legacy,
            )
            entries = [line.split(" ", 1) for line in inventory.splitlines()]
            staged = {}
            for old_ref, oid in entries[:limit]:
                ref = PREFIX + old_ref.removeprefix(legacy)
                row = {"scope": "remote", "old_ref": "refs/heads/" +
                       old_ref.removeprefix("refs/remotes/origin/"), "ref": ref,
                       "oid": oid, "action": "preview", "error": None}
                rows.append(row)
                try:
                    record = await store._read(oid)
                    if record is None or _record_ref(record) != ref:
                        raise ValueError("legacy ref does not match its provenance identity")
                    staged[ref] = oid
                except (GitError, ValueError, KeyError, TypeError) as exc:
                    row.update(action="blocked", error=str(exc))
            observed = await self.git.als_remote_qualified_refs(
                path, list(staged), repository_url=repository_url,
            ) if staged else {}
            absent = {}
            for row in rows:
                if row["action"] == "blocked":
                    continue
                current = observed[row["ref"]]
                if current.state is RemoteRefState.ERROR:
                    row.update(action="blocked", error=current.error or "remote read failed")
                elif current.state is RemoteRefState.PRESENT and current.oid != row["oid"]:
                    row.update(action="blocked", error="destination binds different evidence")
                elif current.state is RemoteRefState.ABSENT:
                    absent[row["ref"]] = row["oid"]
            if not dry_run:
                copied = await self.git.apush_new_refs(
                    path, absent, qualified=True, repository_url=repository_url,
                ) if absent else {}
                for row in rows:
                    if row["action"] == "blocked":
                        continue
                    destination = copied.get(row["ref"], observed[row["ref"]])
                    if (destination.state is not RemoteRefState.PRESENT
                            or destination.oid != row["oid"]):
                        row.update(action="blocked", error=destination.error or "copy not verified")
                        continue
                    try:
                        await self.git.adelete_remote_ref_exact(
                            path, row["old_ref"].removeprefix("refs/heads/"), row["oid"],
                        )
                        row["action"] = "migrated"
                    except GitError as exc:
                        row.update(action="blocked", error=str(exc))
        if checkout is not None:
            await self._local(repository_url, checkout, rows, dry_run=dry_run, limit=limit)
        blocked = any(row["action"] == "blocked" for row in rows)
        return {"dry_run": dry_run, "rows": rows, "remaining": len(entries) - sum(
            row["scope"] == "remote" and row["action"] == "migrated" for row in rows),
            "outcome": "blocked" if blocked else "preview" if dry_run else "migrated"}

    async def _local(self, repository_url, checkout, rows, *, dry_run, limit):
        store = GitProvenance(self.git, checkout, repository_url=repository_url)
        if await store.run("remote", "get-url", "origin") != repository_url:
            raise ValueError("local checkout does not name the authorized repository")
        legacy = "refs/heads/" + LEGACY_PREFIX
        inventory = await store.run(
            "for-each-ref", "--format=%(refname) %(objectname)", legacy,
        )
        attached = {item["branch"] for item in await self.git.aworktree_list(checkout)
                    if item.get("branch")}
        for old_ref, oid in [line.split(" ", 1) for line in inventory.splitlines()][:limit]:
            ref = PREFIX + old_ref.removeprefix(legacy)
            row = {"scope": "local", "old_ref": old_ref, "ref": ref, "oid": oid,
                   "action": "preview", "error": None}
            rows.append(row)
            try:
                if old_ref in attached or old_ref.removeprefix("refs/heads/") in attached:
                    raise ValueError("legacy branch is attached to a worktree")
                record = await store._read(oid)
                if record is None or _record_ref(record) != ref:
                    raise ValueError("legacy ref does not match its provenance identity")
                current = await self.git.arun_git_result(
                    ["rev-parse", "--verify", "--quiet", ref], cwd=checkout,
                )
                if current.returncode not in (0, 1):
                    raise GitError(current.stderr or "local destination read failed")
                if current.returncode == 0 and current.stdout.strip() != oid:
                    raise ValueError("destination binds different evidence")
                if not dry_run:
                    if current.returncode == 1:
                        await store.run("update-ref", ref, oid, "0" * 40)
                    if await store.run("rev-parse", "--verify", ref) != oid:
                        raise GitError("local copy not verified")
                    await self.git.adelete_local_ref_exact(
                        checkout, ref=old_ref, expected_old_oid=oid,
                    )
                    row["action"] = "migrated"
            except (GitError, ValueError, KeyError, TypeError) as exc:
                row.update(action="blocked", error=str(exc))
