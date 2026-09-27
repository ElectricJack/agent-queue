"""Bounded, conservative bridge from legacy source locators to Git provenance.

Legacy state never proves containment. Leave original rows intact, report every
unbound/ambiguous generation, and publish only exact Git-verified identities.
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import asdict

from sqlalchemy import select, text, tuple_, union

from src.database.queries.task_identity import resolve_task_identity_on
from src.database.tables import (
    archived_tasks,
    development_deliveries,
    task_completion_records,
    tasks,
)
from src.git.manager import GitError
from src.integration.provenance import (
    CompletedSource,
    CompletionIdentity,
    GitProvenance,
    legacy_repair_source,
)

# The delivery journal is read in keyset chunks; only the rows naming a page's
# tasks are retained, and those alone are bounded.
HISTORY_CHUNK = 500
MAX_PAGE_HISTORY = 5000


class ProvenanceMigration:
    def __init__(self, db, git):
        self.db, self.git = db, git

    async def run(self, project_id: str, *, apply: bool = False, limit: int = 500, offset: int = 0,
                  task_id: str | None = None):
        """Inventory one page of completion generations, or only a held task's sources.

        *task_id* scopes the run to the generations that task's close needs: the
        current completion of every repair-contract source (or the task's own
        passing generations when it has no contract). *limit*/*offset* then
        do not apply and ``next_offset`` is ``None``.
        """
        if type(apply) is not bool:
            raise ValueError("apply must be an explicit boolean")
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("migration requires limit 1..1000 and nonnegative offset")
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise ValueError("task_id must name one task")
        project = await self.db.get_project(project_id)
        if project is None or project.hierarchical_integration_mode != "development":
            raise ValueError("migration requires a development project")
        repo = await self.db.get_repo(project.integration_repository_id or "")
        if repo is None or repo.project_id != project_id:
            raise ValueError("migration repository is not configured for this project")
        project_tasks = union(select(tasks.c.id).where(tasks.c.project_id == project_id),
            select(archived_tasks.c.id).where(archived_tasks.c.project_id == project_id))
        async with self.db._engine.connect() as conn:
            if task_id is None:
                rows = (await conn.execute(select(task_completion_records).where(
                    task_completion_records.c.task_id.in_(project_tasks),
                    task_completion_records.c.outcome == "pass",
                ).order_by(task_completion_records.c.completed_at, task_completion_records.c.id)
                  .offset(offset).limit(limit + 1))).mappings().all()
                more, rows, held, unheld = len(rows) > limit, rows[:limit], {}, []
                unlabelled = union(*[
                    select(table.c.id).where(
                        table.c.project_id == project_id, table.c.status == "COMPLETED",
                        table.c.branch_name.is_not(None),
                        ~select(task_completion_records.c.id).where(
                            task_completion_records.c.task_id == table.c.id,
                        ).exists(),
                    ) for table in (tasks, archived_tasks)
                ]).subquery()
                missing = (await conn.execute(select(unlabelled.c.id).order_by(unlabelled.c.id)
                    .offset(offset).limit(limit + 1))).scalars().all()
                more = more or len(missing) > limit
                unheld = [{"task_id": identity, "generation": None,
                           "reason": "missing immutable completion generation"}
                          for identity in missing[:limit]]
            else:
                more = False
                rows, held, unheld = await self._held_task_rows(conn, project_id, task_id)
            page = {row["task_id"] for row in rows}
            history = await self._history(conn, project_id, repo.id, page)
            identities = {row["task_id"]: await resolve_task_identity_on(conn, row["task_id"])
                          for row in rows}
        # An isolated read clone prevents even a dry-run fetch from changing
        # refs/index/config in the operator or another worker's repository.
        with tempfile.TemporaryDirectory(prefix="aq-provenance-migrate-") as path:
            await self.git.acreate_checkout(repo.url, path, no_checkout=True)
            store = GitProvenance(self.git, path, repository_url=repo.url)
            target = await store.run("rev-parse", "refs/remotes/origin/" + repo.default_branch)
            inventory, ambiguous, bindings, fallback = [], list(unheld), {}, list(unheld)
            for row in rows:
                source_task = row["task_id"]
                entry = {"task_id": source_task, "generation": row["id"]}
                try:
                    task = identities[source_task]
                    if (task is None or task.project_id != project_id
                            or task.repo_id not in (None, repo.id)):
                        raise ValueError("legacy task belongs to a different or missing repository")
                    identity = CompletionIdentity(project_id, repo.id, source_task, row["id"])
                    existing = await store.read_completion(identity)
                    try:
                        source = await self._source(
                            store, row, history, project_id, held.get(source_task))
                    except (ValueError, KeyError, TypeError, GitError):
                        if existing is None:
                            raise
                        # Retained already (a held-task run or a close); Git
                        # is the authority once the legacy locator is spent.
                        source = existing["source_oid"]
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
                    if existing is None and not apply:
                        fallback.append({**entry, "reason": "missing_git_completion"})
                except (ValueError, KeyError, TypeError, GitError) as exc:
                    ambiguous.append({**entry, "reason": str(exc)})
                    fallback.append({**entry, "reason": str(exc)})
            # A held task's close retains its own replacement; its sources'
            # older repair groups belong to the paged inventory.
            repairs = ([], []) if task_id is not None else await self._repairs(
                store, history, rows, bindings, target, apply, page)
            ambiguous.extend(repairs[1])
            operations = await self._operations(history, apply)
            # Old operator equivalence rows name neither the immutable original
            # generation nor a replacement base. Do not silently convert an
            # operator's acceptance into a broader, unprovable Git claim.
            for delivery in history:
                for member in delivery["manifest"] or []:
                    if (member.get("acceptance") == "operator_equivalent"
                            and member.get("task_id") in page):
                        ambiguous.append({"task_id": member["task_id"], "legacy_id": delivery["id"],
                            "reason": "operator equivalence requires exact original generation/source "
                                      "and nonempty replacement base/source evidence"})
            return {"success": True, "outcome": "migrated" if apply else "inventory",
                    "project_id": project_id, "repository_id": repo.id,
                    "inventory": inventory, "repairs": repairs[0], "ambiguous": ambiguous,
                    "fallback_generations": fallback,
                    "fallback_count": len(fallback),
                    "zero_fallback": not fallback and not more and not unheld,
                    "operations": operations,
                    "legacy_heads": [{"id": r["id"], "target_ref": r["target_ref"],
                        "prepared_sha": r["prepared_sha"], "manifest": r["manifest"]} for r in history],
                    "next_offset": offset + limit if more else None}

    async def _operations(self, history, apply):
        """Retain outstanding actions and test evidence, never delivery receipts.

        Only a pending push, unresolved test/merge attempt, infrastructure
        streak or pending cleanup has recovery work. Terminal receipt mappings
        belong in git and are deliberately not copied to another model.
        """
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import JSONB

        from src.database.tables import events
        from src.integration.development import BRANCH_CLEANUP_KEY, DevelopmentIntegration
        from src.integration.development_validation import DEFERRAL_KIND

        inventory = []
        for old in history:
            evidence = old["evidence"] or {}
            cleanup = evidence.get(BRANCH_CLEANUP_KEY) or {}
            if not (old["state"] in {"prepared", "publishing", "parked"}
                    or evidence.get("kind") == DEFERRAL_KIND
                    or cleanup.get("state") == "pending"):
                continue
            identity = "legacy-operation:" + old["id"]
            # These keys are obsolete receipt bindings, not operation facts.
            retained = {key: value for key, value in evidence.items() if key not in {
                "completion_sources", "resolved_by_delivered_repair", "resolved_by_main_ancestry",
            }}
            row = {key: old[key] for key in (
                "project_id", "repository_id", "target_ref", "expected_sha", "prepared_sha",
                "manifest", "reason", "created_at", "updated_at",
            )}
            row.update(id=identity, state=old["state"], evidence=retained)
            if apply:
                async with self.db._engine.begin() as conn:
                    await conn.execute(text(
                        "SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"
                    ), {"id": "development-operation:" + identity})
                    present = await conn.scalar(select(events.c.id).where(
                        events.c.event_type == "development.operation",
                        cast(events.c.payload, JSONB)["id"].as_string() == identity,
                    ).limit(1))
                    if present is None:
                        await conn.execute(DevelopmentIntegration._operation_insert(**row))
            inventory.append({"legacy_id": old["id"], "operation_id": identity,
                              "action": "retained" if apply else "would_retain"})
        return inventory

    async def _held_task_rows(self, conn, project_id, task_id):
        """Current source generations a held task's close needs, keyed by contract.

        Mirrors the close: each repair-contract source uses its latest
        completion, which must pass. Without a contract, the task's own passing
        generations are the scope.
        """
        identity = await resolve_task_identity_on(conn, task_id)
        if identity is None or identity.project_id != project_id:
            raise ValueError("task does not belong to this project")
        contract = await self.db.get_task_meta(task_id, "development_repair_sources")
        if not contract:
            rows = (await conn.execute(select(task_completion_records).where(
                task_completion_records.c.task_id == task_id,
                task_completion_records.c.outcome == "pass",
            ).order_by(task_completion_records.c.completed_at, task_completion_records.c.id)
              .limit(1000))).mappings().all()
            return rows, {}, []
        if not isinstance(contract, list) or len(contract) > 100:
            raise ValueError("invalid exact development repair contract")
        rows, held, unheld = [], {}, []
        for member in contract:
            latest = (await conn.execute(select(task_completion_records).where(
                task_completion_records.c.task_id == member["task_id"],
            ).order_by(task_completion_records.c.completed_at.desc()).limit(1))).mappings().first()
            if latest is None or latest["outcome"] != "pass":
                unheld.append({"task_id": member["task_id"], "generation": latest and latest["id"],
                               "reason": "repair source has no passing immutable completion"})
                continue
            rows.append(latest)
            held[member["task_id"]] = (task_id, member)
        return rows, held, unheld

    async def _history(self, conn, project_id, repository_id, task_ids):
        """Keyset-page the whole delivery journal, retaining rows naming *task_ids*.

        No single read holds a project's full history and a long history never
        refuses a page; only the page's own relevant rows are bounded.
        """
        history, after = [], None
        column = development_deliveries.c
        while task_ids:
            query = select(development_deliveries).where(
                column.project_id == project_id, column.repository_id == repository_id,
            )
            if after is not None:
                query = query.where(tuple_(column.created_at, column.id) > tuple_(*after))
            chunk = (await conn.execute(query.order_by(column.created_at, column.id)
                                        .limit(HISTORY_CHUNK))).mappings().all()
            history.extend(row for row in chunk if _names(row, task_ids))
            if len(history) > MAX_PAGE_HISTORY:
                raise ValueError("legacy history for this page exceeds bounded inventory; "
                                 "use a smaller --limit or --task-id")
            if len(chunk) < HISTORY_CHUNK:
                break
            after = chunk[-1]["created_at"], chunk[-1]["id"]
        return history

    async def _source(self, store, row, history, project_id, held=None):
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
        if held is not None:
            # A held repair's daemon-authored contract, fenced by the delivery
            # that filed it, binds a source the legacy close never reported.
            repair_id, member = held
            located = await legacy_repair_source(
                self.db, store, project_id, repair_id, member, self.db._row_to_task_completion(row)
            )
            if located is not None:
                candidates.add(located)
        if len(candidates) != 1:
            raise ValueError("legacy generation has missing or ambiguous exact source bindings")
        source = await store.exact(candidates.pop())
        if held is not None and source != held[1].get("source_sha"):
            # Binding another source would make the held repair's exact
            # replacement impossible; leave that to an operator decision.
            raise ValueError("legacy completion source conflicts with the held repair contract")
        return source

    async def _repairs(self, store, history, rows, bindings, target, apply, page):
        inventory, ambiguous, seen = [], [], set()
        for delivery in history:
            members = delivery["manifest"] or []
            groups = {}
            proof = (delivery["evidence"] or {}).get("resolved_by_delivered_repair") or {}
            if proof.get("task_id"):
                groups[proof["task_id"]] = members
            for member in members:
                if member.get("superseded_by"):
                    groups.setdefault(member["superseded_by"], []).append(member)
            for repair_id, sources in groups.items():
                if repair_id not in page and not any(m["task_id"] in page for m in sources):
                    continue  # another page reports this group
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


def _names(row, task_ids):
    """Whether a legacy delivery row locates or supersedes any of *task_ids*."""
    members = [m for m in row["manifest"] or [] if isinstance(m, dict)]
    proofs = (row["evidence"] or {}).get("completion_sources") or []
    return any(
        m.get("task_id") in task_ids or m.get("superseded_by") in task_ids for m in members
    ) or any(isinstance(p, dict) and p.get("task_id") in task_ids for p in proofs)
