"""Bounded, conservative bridge from legacy source locators to Git provenance.

The retired ``development_deliveries`` journal survives only as immutable
``development.legacy_provenance`` events (source locators without delivery
state) and, for outstanding actions, ``legacy-operation:`` operation events,
both written by revision a00000000038. Legacy state never proves containment.
Report every unbound/ambiguous generation and publish only exact Git-verified
identities; a generation left unlabelled evaluates unknown, never delivered.
"""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict

from sqlalchemy import cast, select, union
from sqlalchemy.dialects.postgresql import JSONB

from src.database.queries.task_identity import resolve_task_identity_on
from src.database.tables import (
    archived_tasks,
    events,
    task_completion_records,
    task_metadata,
    tasks,
    sessions,
)
from src.git.manager import GitError, is_valid_git_oid
from src.integration.provenance import (
    LEGACY_PROVENANCE_EVENT,
    CompletedSource,
    CompletionIdentity,
    GitProvenance,
    legacy_repair_source,
)
from src.integration.delivery_truth import legacy_completion_id, load_delivery_requests
from src.integration.publishable_artifact import LEGACY_ARTIFACT_KEY

# The retained journal is read in keyset chunks; only the rows naming a page's
# tasks are retained, and those alone are bounded.
HISTORY_CHUNK = 500
MAX_PAGE_HISTORY = 5000
# Generations bound per batch; an apply publishes a batch's new refs in one
# remote transfer rather than one round trip set per generation.
BIND_BATCH = 50
# Seconds after which a page starts no further batch. It then ends early and
# ``next_offset`` resumes at its first unexamined generation, so a page's Git
# work stays well inside the client's response timeout.
PAGE_TIME_BUDGET = 45.0


class ProvenanceMigration:
    def __init__(self, db, git):
        self.db, self.git = db, git

    async def run(self, project_id: str, *, apply: bool = False, limit: int = 500, offset: int = 0,
                  task_id: str | None = None, source: str | None = None,
                  no_artifact: bool = False, reason: str | None = None,
                  operator_id: str = "local"):
        """Inventory one page of completion generations, or only a held task's sources.

        *task_id* scopes the run to the generations that task's close needs: the
        current completion of every repair-contract source (or the task's own
        passing generations when it has no contract). *limit*/*offset* then
        do not apply and ``next_offset`` is ``None``.

        Generations are bound in batches of :data:`BIND_BATCH`. A page starts no
        batch after :data:`PAGE_TIME_BUDGET`; it then reports
        ``budget_exhausted`` and a ``next_offset`` at its first unexamined
        generation (a ``--task-id`` run is simply repeated). ``counts``
        summarises the page.

        *source* is an operator's attestation of the exact final source of
        *task_id*'s current completion, for a legacy close that retained none
        Git can verify (no reported commit, no ``completion_sources``). It binds
        only that generation of a COMPLETED task without a repair contract,
        and never overrides a source the generation's own evidence names.
        A missing descriptive completion row requires *reason* and uses the
        shared evaluator's current/legacy generation identity. *no_artifact*
        instead declares artifact-free work on a reason. A COMPLETED task whose
        latest completion did not pass is attested the same way on a reason,
        fenced to that generation: its recorded commits are what it read, so
        *no_artifact* replaces them, and an attested source must already be
        contained in the target. None of these creates a worker completion row;
        applies fence the generation and audit the operator's decision before
        releasing its task lock.
        """
        if type(apply) is not bool:
            raise ValueError("apply must be an explicit boolean")
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("migration requires limit 1..1000 and nonnegative offset")
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise ValueError("task_id must name one task")
        if source is not None and (task_id is None or not is_valid_git_oid(source)):
            raise ValueError("an attested source needs --task-id and a full lowercase Git OID")
        if type(no_artifact) is not bool or no_artifact and (
            source is not None or task_id is None or not (reason or "").strip()
        ):
            raise ValueError("--no-artifact requires --task-id and a reason, without --source")
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
                # A retired manifest's source for a branchless live task is an
                # artifact too (the retirement marker); without a generation it
                # cannot be bound, only reported.
                marked = select(task_metadata.c.task_id).where(
                    task_metadata.c.task_id == tasks.c.id,
                    task_metadata.c.key == LEGACY_ARTIFACT_KEY,
                ).exists()
                unlabelled = union(*[
                    select(table.c.id).where(
                        table.c.project_id == project_id, table.c.status == "COMPLETED",
                        table.c.branch_name.is_not(None) | marked
                        if table is tasks else table.c.branch_name.is_not(None),
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
                rows, held, unheld = await self._held_task_rows(
                    conn, project_id, task_id, attested=source is not None or no_artifact,
                    repository_id=repo.id, target_ref="refs/heads/" + repo.default_branch,
                    reason=reason)
            page = {row["task_id"] for row in rows}
            history = await self._history(conn, project_id, repo.id, page)
            operations = await self._operations(conn, project_id, history)
            identities = {row["task_id"]: await resolve_task_identity_on(conn, row["task_id"])
                          for row in rows}
        # An isolated read clone prevents even a dry-run fetch from changing
        # refs/index/config in the operator or another worker's repository.
        with tempfile.TemporaryDirectory(prefix="aq-provenance-migrate-") as path:
            await self.git.acreate_checkout(repo.url, path, no_checkout=True)
            store = GitProvenance(self.git, path, repository_url=repo.url)
            target = await store.run("rev-parse", "refs/remotes/origin/" + repo.default_branch)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PAGE_TIME_BUDGET
            inventory, ambiguous, bindings, fallback, done = [], [], {}, [], 0
            # The first batch always runs, so every page makes progress.
            while done < len(rows) and (not done or loop.time() < deadline):
                batch = rows[done:done + BIND_BATCH]
                done += len(batch)
                async with self._attestation_guard(batch, repo, apply=apply) as guard:
                    results = await self._bind(
                        store, batch, identities, history, held, project_id, repo.id, apply,
                        source, no_artifact=no_artifact, anchor=target,
                    )
                    if guard is not None:
                        await self.db.log_event(
                            "development.provenance_attested", project_id=project_id,
                            task_id=task_id, conn=guard,
                            payload=json.dumps({"operator_id": operator_id, "reason": reason,
                                                "source_oid": source, "artifact": not no_artifact,
                                                "results": [entry | {"error": error}
                                                            for entry, _, error in results]}),
                        )
                for entry, binding, error in results:
                    if error is not None:
                        ambiguous.append({**entry, "reason": error})
                        fallback.append({**entry, "reason": error})
                        continue
                    bindings[entry["generation"]] = binding
                    inventory.append(entry)
                    if entry["action"] == "would_write":
                        fallback.append({**entry, "reason": "missing_git_completion"})
            budget_exhausted = done < len(rows)
            if budget_exhausted:
                # The page ends at its last examined generation; the paged
                # missing-generation report shares the offset window, so it is
                # cut to the same span and the next page resumes both.
                rows = rows[:done]
                page = {row["task_id"] for row in rows}
                history = [row for row in history if _names(row, page)]
                kept = {"legacy-operation:" + row["id"] for row in history}
                operations = [item for item in operations if item["operation_id"] in kept]
                if task_id is None:
                    more, unheld = True, unheld[:done]
            ambiguous[:0], fallback[:0] = unheld, unheld
            # A held task's close retains its own replacement; its sources'
            # older repair groups belong to the paged inventory.
            repairs = ([], []) if task_id is not None else await self._repairs(
                store, history, rows, bindings, target, apply, page)
            ambiguous.extend(repairs[1])
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
            actions = Counter(entry["action"] for entry in inventory)
            return {"success": True, "outcome": "migrated" if apply else "inventory",
                    "project_id": project_id, "repository_id": repo.id,
                    "inventory": inventory, "repairs": repairs[0], "ambiguous": ambiguous,
                    "fallback_generations": fallback,
                    "fallback_count": len(fallback),
                    "zero_fallback": not fallback and not ambiguous and not more
                                     and not budget_exhausted,
                    "operations": operations,
                    "legacy_heads": [{"id": r["id"], "target_ref": r["target_ref"],
                        "prepared_sha": r["prepared_sha"], "manifest": r["manifest"]} for r in history],
                    "counts": {"generations": len(rows), "present": actions["present"],
                               "written": actions["written"], "would_write": actions["would_write"],
                               "missing_generation": len(unheld), "ambiguous": len(ambiguous),
                               "repairs": len(repairs[0]), "fallback": len(fallback)},
                    "budget_exhausted": budget_exhausted,
                    "next_offset": (offset + (len(rows) if budget_exhausted else limit)
                                    if more and task_id is None else None)}

    async def _bind(self, store, rows, identities, history, held, project_id, repository_id,
                    apply, attested=None, *, no_artifact=False, anchor=None):
        """Bind one batch of generations; an apply publishes its new refs in one transfer.

        Returns ``(entry, binding, reason)`` per row, in order; *reason* is set
        when the generation stays unbound, including when its write failed.
        """
        results, writes, aliases = [], [], {}
        for row in rows:
            source_task = row["task_id"]
            entry = {"task_id": source_task, "generation": row["id"]}
            try:
                task = identities[source_task]
                if (task is None or task.project_id != project_id
                        or task.repo_id not in (None, repository_id)):
                    raise ValueError("legacy task belongs to a different or missing repository")
                identity = CompletionIdentity(project_id, repository_id, source_task, row["id"])
                existing = await store.read_completion(identity)
                try:
                    if no_artifact:
                        # A generation that did not pass recorded what it read,
                        # not an artifact of its own; a retained manifest naming
                        # this generation is still a conflict.
                        recorded = json.loads(row["commits"] or "[]")
                        if (recorded and not row.get("failed_completion")) or any(
                            member.get("task_id") == source_task and member.get("source_sha")
                            for delivery in history for member in (
                                (delivery["manifest"] or []) + [proof for proof in
                                    (delivery["evidence"] or {}).get("completion_sources", [])
                                    if proof.get("completion_id") == row["id"]]
                            )
                        ):
                            raise ValueError("no-artifact attestation conflicts with recorded source")
                        source = existing["source_oid"] if existing else await store.exact(anchor)
                    elif row.get("failed_completion"):
                        source = await self._failed_source(store, attested, anchor)
                    else:
                        source = await self._source(
                            store, row, history, project_id, held.get(source_task), attested)
                except (ValueError, KeyError, TypeError, GitError):
                    # A refused attestation is reported, never replaced.
                    if existing is None or attested or no_artifact:
                        raise
                    # Retained already (a held-task run or a close); Git
                    # is the authority once the legacy locator is spent.
                    source = existing["source_oid"]
                if existing and existing["source_oid"] != source:
                    raise ValueError("git provenance conflicts with the legacy binding")
                if existing and (attested or no_artifact) and (
                    existing["artifact"] != (not no_artifact)
                ):
                    raise ValueError("attestation conflicts with the retained artifact declaration")
                binding = CompletedSource(identity, source)
                alias = None
                if row.get("missing_completion_row"):
                    # Archiving drops current-generation metadata, but retains
                    # the task version. Keep exactly the same attestation under
                    # that immutable locator, without inventing a close row.
                    alias_id = CompletionIdentity(project_id, repository_id, source_task,
                                                  legacy_completion_id(row["attestation_request"]))
                    if alias_id != identity:
                        alias_record = await store.read_completion(alias_id)
                        if alias_record and (
                            alias_record["source_oid"] != source
                            or alias_record["artifact"] != (not no_artifact)
                        ):
                            raise ValueError("attestation conflicts with retained legacy generation")
                        if alias_record is None:
                            alias = CompletedSource(alias_id, source)
            except (ValueError, KeyError, TypeError, GitError) as exc:
                results.append((entry, None, str(exc)))
                continue
            # Unlabelled legacy code outcomes stay artifacts. Missing
            # source evidence cannot be interpreted as code-free.
            if apply and existing is None:
                writes.append(binding)
            if alias is not None:
                aliases[identity] = alias.identity
                if apply:
                    writes.append(alias)
            results.append(({**entry, "source_oid": source, "archived": task.archived,
                             "action": "present" if existing and alias is None else "would_write",
                             **({"legacy_generation": alias.identity.generation}
                                if alias is not None else {}),
                             **({"authority": "operator", "artifact": not no_artifact}
                                if attested or no_artifact else {}),
                             **({"completion_outcome": row["outcome"]}
                                if row.get("failed_completion") else {})}, binding, None))
        written = await store.write_completions(writes, artifact=not no_artifact) if writes else {}
        settled = []
        for entry, binding, reason in results:
            outcome = written.get(binding.identity) if binding is not None else None
            if binding is not None and binding.identity in aliases:
                alias_outcome = written.get(aliases[binding.identity])
                if not isinstance(outcome, Exception) and alias_outcome is not None:
                    outcome = alias_outcome
            if isinstance(outcome, Exception):
                entry = {"task_id": entry["task_id"], "generation": entry["generation"]}
                binding, reason = None, str(outcome) or type(outcome).__name__
            elif outcome is not None:
                entry = {**entry, "action": "written"}
            settled.append((entry, binding, reason))
        return settled

    @staticmethod
    async def _operations(conn, project_id, history):
        """Outstanding legacy actions the retirement retained as operation events.

        Only a pending push, unresolved test/merge attempt, infrastructure streak
        or pending cleanup had recovery work; revision a00000000038 appended
        each as ``legacy-operation:<id>``. Terminal receipts were not copied.
        """
        wanted = {"legacy-operation:" + row["id"] for row in history}
        if not wanted:
            return []
        identity = cast(events.c.payload, JSONB)["id"].as_string()
        retained = set((await conn.execute(select(identity).where(
            events.c.event_type == "development.operation",
            events.c.project_id == project_id,
            identity.in_(sorted(wanted)),
        ).distinct())).scalars())
        return [{"legacy_id": operation.removeprefix("legacy-operation:"),
                 "operation_id": operation, "action": "retained"}
                for operation in sorted(retained)]

    async def _held_task_rows(self, conn, project_id, task_id, *, attested=False,
                              repository_id=None, target_ref=None, reason=None):
        """Current source generations a held task's close needs, keyed by contract.

        Mirrors the close: each repair-contract source uses its latest
        completion, which must pass. Without a contract, the task's own passing
        generations are the scope.
        """
        identity = await resolve_task_identity_on(conn, task_id)
        if identity is None or identity.project_id != project_id:
            raise ValueError("task does not belong to this project")
        contract = await self.db.get_task_meta(task_id, "development_repair_sources")
        if attested:
            # One operator-attested source binds exactly one generation: the
            # current completion of a finished task that names no contract.
            if contract:
                raise ValueError("a repair contract names its own sources; "
                                 "an attested source applies to one task's own completion")
            if identity.status != "COMPLETED":
                raise ValueError("an attested source needs a COMPLETED task")
            latest = (await conn.execute(select(task_completion_records).where(
                task_completion_records.c.task_id == task_id,
            ).order_by(task_completion_records.c.completed_at.desc(),
                       task_completion_records.c.id.desc()).limit(1))).mappings().first()
            request = (await load_delivery_requests(
                self.db, [task_id], repository_id=repository_id, target_ref=target_ref, conn=conn,
            ))[task_id]
            if request.requires_parent_completion:
                raise ValueError("current verified parent completion is required; "
                                 "parent verification cannot be replaced by a leaf attestation")
            if latest is not None and latest["id"] == request.completion_id and (
                latest["outcome"] != "pass"
            ):
                # The generation is real and current; its close just did not
                # pass. An operator may still attest what that generation
                # delivered, fenced to it, without inventing a pass.
                if not (reason or "").strip():
                    raise ValueError("attesting a completion that did not pass requires a reason")
                return [{**latest, "attestation_request": request,
                         "failed_completion": True}], {}, []
            if latest is None or latest["id"] != request.completion_id:
                if not (reason or "").strip():
                    raise ValueError("attesting a missing completion row requires a reason")
                latest = {"id": request.completion_id, "task_id": task_id,
                          "commits": "[]", "completed_at": request.task_version,
                          "missing_completion_row": True}
            return [{**latest, "attestation_request": request}], {}, []
        if not contract:
            rows = (await conn.execute(select(task_completion_records).where(
                task_completion_records.c.task_id == task_id,
                task_completion_records.c.outcome == "pass",
            ).order_by(task_completion_records.c.completed_at, task_completion_records.c.id)
              .limit(1000))).mappings().all()
            # The paged inventory's missing-generation report, for one task:
            # nothing to bind is not the same as nothing left unknown.
            if not rows and identity.status == "COMPLETED" and (
                identity.branch_name or (not identity.archived and await conn.scalar(
                    select(task_metadata.c.task_id).where(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == LEGACY_ARTIFACT_KEY,
                    ).limit(1)))):
                return rows, {}, [{"task_id": task_id, "generation": None,
                                   "reason": "missing immutable completion generation"}]
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

    @asynccontextmanager
    async def _attestation_guard(self, rows, repo, *, apply):
        """Fence operator writes to a stopped, unchanged completed generation."""
        observed = {row["task_id"]: row["attestation_request"] for row in rows
                    if "attestation_request" in row}
        if not apply or not observed:
            yield None
            return
        async with self.db.immediate() as conn:
            for table in (tasks, archived_tasks):
                locked = (await conn.execute(select(table).where(
                    table.c.id.in_(observed),
                ).with_for_update())).mappings().all()
                if table is tasks and any(row["assigned_agent_id"] for row in locked):
                    raise ValueError("attested task still has a worker assignment")
            if await conn.scalar(select(sessions.c.id).where(
                sessions.c.state != "stopped",
                sessions.c.task_id.in_(observed),
            ).limit(1)):
                raise ValueError("attested task still has a live session")
            if await conn.scalar(select(task_metadata.c.task_id).where(
                task_metadata.c.task_id.in_(observed),
                task_metadata.c.key == "development_repair_sources",
                cast(task_metadata.c.value, JSONB) != cast("null", JSONB),
                cast(task_metadata.c.value, JSONB) != cast("[]", JSONB),
            ).limit(1)):
                raise ValueError("attested task acquired a repair contract; observe it again")
            current = await load_delivery_requests(
                self.db, observed, repository_id=repo.id,
                target_ref="refs/heads/" + repo.default_branch, conn=conn,
            )
            if current != observed:
                raise ValueError("attested completion changed; observe it again")
            yield conn

    async def _history(self, conn, project_id, repository_id, task_ids):
        """Keyset-page the retained legacy journal, keeping rows naming *task_ids*.

        No single read holds a project's full history and a long history never
        refuses a page; only the page's own relevant rows are bounded. Each
        :data:`LEGACY_PROVENANCE_EVENT` payload is read back in its journal
        row's shape (``id`` is the retired row id), without any state.
        """
        history, after = [], 0
        payload = cast(events.c.payload, JSONB)
        while task_ids:
            chunk = (await conn.execute(select(events.c.id, events.c.payload).where(
                events.c.event_type == LEGACY_PROVENANCE_EVENT,
                events.c.project_id == project_id,
                payload["repository_id"].as_string() == repository_id,
                events.c.id > after,
            ).order_by(events.c.id).limit(HISTORY_CHUNK))).all()
            for _event_id, raw in chunk:
                row = _journal_row(json.loads(raw))
                if _names(row, task_ids):
                    history.append(row)
            if len(history) > MAX_PAGE_HISTORY:
                raise ValueError("legacy history for this page exceeds bounded inventory; "
                                 "use a smaller --limit or --task-id")
            if len(chunk) < HISTORY_CHUNK:
                break
            after = chunk[-1][0]
        history.sort(key=lambda row: (row["created_at"], row["id"]))
        return history

    async def _source(self, store, row, history, project_id, held=None, attested=None):
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
        if attested is not None:
            # The attestation supplies what the legacy close never recorded;
            # it cannot replace a source the generation's evidence names.
            attested = await store.exact(attested)
            if candidates - {attested}:
                raise ValueError("attested source conflicts with the generation's recorded source")
            candidates = {attested}
        if len(candidates) != 1:
            raise ValueError("legacy generation has missing or ambiguous exact source bindings")
        source = await store.exact(candidates.pop())
        if held is not None and source != held[1].get("source_sha"):
            # Binding another source would make the held repair's exact
            # replacement impossible; leave that to an operator decision.
            raise ValueError("legacy completion source conflicts with the held repair contract")
        return source

    @staticmethod
    async def _failed_source(store, attested, anchor):
        """The exact source an operator may attest for a completion that did not pass.

        Such a generation recorded what it read rather than an artifact of its
        own, so the attestation replaces that commit instead of conflicting
        with it. The source must already be contained in the target: the
        control can only clear work the target holds, never deliver new work.
        """
        if attested is None:
            raise ValueError("a completion that did not pass needs an attested source")
        source = await store.exact(attested)
        if not await store.ancestor(source, anchor):
            raise ValueError("attested source for a completion that did not pass is not "
                             "contained in the target branch")
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
                    generation = proof.get("completion_id") if proof.get("task_id") == repair_id else None
                    repairs = [bindings[r["id"]] for r in rows if r["task_id"] == repair_id
                               and r["id"] in bindings and (generation is None or r["id"] == generation)]
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


def _journal_row(retained):
    """A retained legacy provenance payload in the retired journal row's shape."""
    return {
        "id": retained["legacy_id"],
        "project_id": retained["project_id"],
        "repository_id": retained["repository_id"],
        "target_ref": retained.get("target_ref"),
        "expected_sha": retained.get("expected_sha"),
        "prepared_sha": retained.get("prepared_sha"),
        "manifest": retained.get("manifest") or [],
        "evidence": {
            key: retained[key]
            for key in ("completion_sources", "resolved_by_delivered_repair")
            if retained.get(key)
        },
        "created_at": float(retained.get("created_at") or 0),
    }


def _binding_key(binding):
    return binding.identity.task_id, binding.identity.generation, binding.source_oid


def _names(row, task_ids):
    """Whether a legacy delivery row locates or supersedes any of *task_ids*."""
    members = [m for m in row["manifest"] or [] if isinstance(m, dict)]
    proofs = (row["evidence"] or {}).get("completion_sources") or []
    return any(
        m.get("task_id") in task_ids or m.get("superseded_by") in task_ids for m in members
    ) or any(isinstance(p, dict) and p.get("task_id") in task_ids for p in proofs)
