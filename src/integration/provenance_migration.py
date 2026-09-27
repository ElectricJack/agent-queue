"""Bounded, conservative bridge from legacy source locators to Git provenance.

Legacy state never proves containment. Leave original rows intact, report every
unbound/ambiguous generation, and publish only exact Git-verified identities.
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import asdict

from sqlalchemy import select, union

from src.database.queries.task_identity import resolve_task_identity_on
from src.database.tables import archived_tasks, development_deliveries, task_completion_records, tasks
from src.git.manager import GitError
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance


class ProvenanceMigration:
    def __init__(self, db, git):
        self.db, self.git = db, git

    async def run(self, project_id: str, *, apply: bool = False, limit: int = 500, offset: int = 0):
        if type(apply) is not bool:
            raise ValueError("apply must be an explicit boolean")
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("migration requires limit 1..1000 and nonnegative offset")
        project = await self.db.get_project(project_id)
        if project is None or project.hierarchical_integration_mode != "development":
            raise ValueError("migration requires a development project")
        repo = await self.db.get_repo(project.integration_repository_id or "")
        if repo is None or repo.project_id != project_id:
            raise ValueError("migration repository is not configured for this project")
        project_tasks = union(select(tasks.c.id).where(tasks.c.project_id == project_id),
            select(archived_tasks.c.id).where(archived_tasks.c.project_id == project_id))
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(select(task_completion_records).where(
                task_completion_records.c.task_id.in_(project_tasks),
                task_completion_records.c.outcome == "pass",
            ).order_by(task_completion_records.c.completed_at, task_completion_records.c.id)
              .offset(offset).limit(limit + 1))).mappings().all()
            history = (await conn.execute(select(development_deliveries).where(
                development_deliveries.c.project_id == project_id,
                development_deliveries.c.repository_id == repo.id,
            ).order_by(development_deliveries.c.created_at).limit(1001))).mappings().all()
            identities = {row["task_id"]: await resolve_task_identity_on(conn, row["task_id"])
                          for row in rows[:limit]}
        if len(history) > 1000:
            raise ValueError("legacy history exceeds bounded inventory; partition before migration")
        # An isolated read clone prevents even a dry-run fetch from changing
        # refs/index/config in the operator or another worker's repository.
        with tempfile.TemporaryDirectory(prefix="aq-provenance-migrate-") as path:
            await self.git.acreate_checkout(repo.url, path, no_checkout=True)
            store = GitProvenance(self.git, path, repository_url=repo.url)
            target = await store.run("rev-parse", "refs/remotes/origin/" + repo.default_branch)
            inventory, ambiguous, bindings = [], [], {}
            for row in rows[:limit]:
                task_id = row["task_id"]
                entry = {"task_id": task_id, "generation": row["id"]}
                try:
                    task = identities[task_id]
                    if task is None or task.repo_id not in (None, repo.id):
                        raise ValueError("legacy task belongs to a different or missing repository")
                    source = await self._source(store, row, history)
                    identity = CompletionIdentity(project_id, repo.id, task_id, row["id"])
                    existing = await store.read_completion(identity)
                    if existing and existing["source_oid"] != source:
                        raise ValueError("git provenance conflicts with the legacy binding")
                    binding = CompletedSource(identity, source)
                    bindings[row["id"]] = binding
                    # Unlabelled legacy code outcomes stay artifacts. Missing
                    # source evidence cannot be interpreted as code-free.
                    if apply and existing is None:
                        await store.write_completion(binding)
                    entry.update(source_oid=source, archived=task.archived,
                                 action="present" if existing else "written" if apply else "would_write")
                    inventory.append(entry)
                except (ValueError, KeyError, TypeError, GitError) as exc:
                    ambiguous.append({**entry, "reason": str(exc)})
            repairs = await self._repairs(store, history, rows[:limit], bindings, target, apply)
            ambiguous.extend(repairs[1])
            # Old operator equivalence rows name neither the immutable original
            # generation nor a replacement base. Do not silently convert an
            # operator's acceptance into a broader, unprovable Git claim.
            for delivery in history:
                for member in delivery["manifest"] or []:
                    if member.get("acceptance") == "operator_equivalent":
                        ambiguous.append({"task_id": member["task_id"], "legacy_id": delivery["id"],
                            "reason": "operator equivalence requires exact original generation/source "
                                      "and nonempty replacement base/source evidence"})
            return {"success": True, "outcome": "migrated" if apply else "inventory",
                    "project_id": project_id, "repository_id": repo.id,
                    "inventory": inventory, "repairs": repairs[0], "ambiguous": ambiguous,
                    "legacy_heads": [{"id": r["id"], "target_ref": r["target_ref"],
                        "prepared_sha": r["prepared_sha"], "manifest": r["manifest"]} for r in history],
                    "next_offset": offset + limit if len(rows) > limit else None}

    async def _source(self, store, row, history):
        commits = json.loads(row["commits"] or "[]")
        if not isinstance(commits, list):
            raise ValueError("legacy completion commits are malformed")
        reported = commits[-1] if commits else None
        candidates = set()
        if reported is not None:
            if not isinstance(reported, str) or not re.fullmatch(r"[0-9a-f]{7,40}", reported):
                raise ValueError("legacy source is not a unique Git object prefix")
            candidates.add(await store.run("--no-replace-objects", "rev-parse", "--verify",
                                            "--end-of-options", reported + "^{commit}"))
        for delivery in history:
            for proof in (delivery["evidence"] or {}).get("completion_sources", []):
                if proof.get("task_id") == row["task_id"] and proof.get("completion_id") == row["id"]:
                    source = await store.exact(proof["source_sha"])
                    # An explicit old publisher binding can refine its own
                    # aggregate-head close, but cannot replace a different
                    # worker source or use a historical generation's trailer.
                    if reported and source not in candidates:
                        prepared = delivery["prepared_sha"]
                        if prepared not in candidates or not await store.ancestor(source, prepared):
                            raise ValueError("legacy mapping conflicts with final completion source")
                        candidates.discard(prepared)
                    candidates.add(source)
        if len(candidates) != 1:
            raise ValueError("legacy generation has missing or ambiguous exact source bindings")
        return await store.exact(candidates.pop())

    async def _repairs(self, store, history, rows, bindings, target, apply):
        inventory, ambiguous, seen = [], [], set()
        for delivery in history:
            members = delivery["manifest"] or []
            groups = {}
            for member in members:
                if member.get("superseded_by"):
                    groups.setdefault(member["superseded_by"], []).append(member)
            for repair_id, sources in groups.items():
                entry = {"task_id": repair_id, "legacy_id": delivery["id"]}
                try:
                    contract = await self.db.get_task_meta(repair_id, "development_repair_sources")
                    exact = {(m["task_id"], m.get("source_sha")) for m in sources}
                    if not contract or exact != {(m["task_id"], m.get("source_sha")) for m in contract}:
                        raise ValueError("legacy replacement does not name the complete exact repair contract")
                    repairs = [bindings[r["id"]] for r in rows if r["task_id"] == repair_id and r["id"] in bindings]
                    if len(repairs) != 1:
                        raise ValueError("repair generation is missing or ambiguous in this inventory page")
                    repair = repairs[0]
                    if not await store.ancestor(repair.source_oid, target):
                        raise ValueError("exact complete repair source is not contained in the fetched target")
                    replaced = []
                    for task_id, oid in sorted(exact):
                        originals = [b for b in bindings.values()
                                     if b.identity.task_id == task_id and b.source_oid == oid]
                        if len(originals) != 1:
                            raise ValueError(f"original generation is missing or ambiguous: {task_id}")
                        replaced.extend(originals)
                    await store._changed_source(repair.source_oid, delivery["expected_sha"])
                    signature = (repair.source_oid, tuple(_binding_key(b) for b in replaced))
                    if signature in seen:
                        continue
                    seen.add(signature)
                    if apply:
                        await store.write_replacement(source_oid=repair.source_oid,
                            base_oid=delivery["expected_sha"], replaces=replaced,
                            authority="repair_contract", reason=f"Verified legacy repair: {repair_id}")
                    inventory.append({**entry, "source_oid": repair.source_oid,
                                      "replaces": [asdict(b) for b in replaced],
                                      "action": "written" if apply else "would_write"})
                except (ValueError, KeyError, TypeError, GitError) as exc:
                    ambiguous.append({**entry, "reason": str(exc)})
        return inventory, ambiguous


def _binding_key(binding):
    return binding.identity.task_id, binding.identity.generation, binding.source_oid
