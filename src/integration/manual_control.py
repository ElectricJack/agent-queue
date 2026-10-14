"""Local operator controls; obligation retirement never grants Git delivery proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from functools import wraps

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from src.database import tables as t
from src.integration.engine import RootEngineOwnership, lock_key
from src.integration.lock import PublishWithdrawn


def digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


@asynccontextmanager
async def enabled_train_write(db, repository_id):
    """Hold the shared side of off's fence through the bounded remote write.

    This guard belongs only to integration writers. Worker source publication
    remains governed by its own claim and branch authority.
    """
    async with db.immediate() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"),
                           {"key": lock_key(repository_id)})
        enabled = await conn.scalar(select(t.projects.c.id).where(
            t.projects.c.integration_repository_id == repository_id,
            t.projects.c.status == "ACTIVE",
            t.projects.c.hierarchical_integration_mode.in_(("train", "hierarchy", "development")),
        ))
        if enabled is None:
            raise PublishWithdrawn("automatic integration is off")
        yield


def train_write(method):
    @wraps(method)
    async def fenced(self, repo, *args, **kwargs):
        async with enabled_train_write(self.locks.db, repo.repository_id):
            return await method(self, repo, *args, **kwargs)
    return fenced


def fence_disable(method):
    """Existing project configuration must serialize disabled mode with writers too."""
    @wraps(method)
    async def fenced(self, project_id, **kwargs):
        if kwargs.get("updates", {}).get("hierarchical_integration_mode") != "disabled":
            return await method(self, project_id, **kwargs)
        project = await self.db.get_project(project_id)
        if project is None or not project.integration_repository_id:
            return await method(self, project_id, **kwargs)
        async with asyncio.timeout(60):
            async with RootEngineOwnership(self.db).exclusion(project.integration_repository_id):
                return await method(self, project_id, **kwargs)
    return fenced


class ManualIntegration:
    def __init__(self, db, *, snapshot=None, clock=time.time):
        self.db, self.snapshot, self.clock = db, snapshot, clock

    async def _binding_on(self, conn, project_id):
        await self.db.lock_hierarchy_project(conn, project_id)
        project = (await conn.execute(select(t.projects).where(t.projects.c.id == project_id)
                                     .with_for_update())).mappings().one_or_none()
        if project is None or not project["integration_repository_id"]:
            raise ValueError("project has no designated integration repository")
        repo = (await conn.execute(select(t.repos).where(
            t.repos.c.id == project["integration_repository_id"],
            t.repos.c.project_id == project_id,
        ).with_for_update())).mappings().one_or_none()
        if repo is None or not repo["url"] or not repo["default_branch"]:
            raise ValueError("designated repository binding is incomplete")
        return project, repo

    @staticmethod
    def _binding(project, repo):
        return {
            "project_id": project["id"], "repository_id": repo["id"],
            "repository_url": repo["url"],
            "target_ref": "refs/heads/" + repo["default_branch"].removeprefix("refs/heads/"),
            "generation": project["hierarchical_integration_generation"],
        }

    async def run(self, request, *, operator_id):
        project = await self.db.get_project(request.project_id)
        if project is None or not project.integration_repository_id:
            raise ValueError("project has no designated integration repository")
        # Same order as root engine transfers: repository, hierarchy, project.
        async with asyncio.timeout(60):
            async with RootEngineOwnership(self.db).exclusion(
                project.integration_repository_id
            ) as conn:
                current, repo = await self._binding_on(conn, request.project_id)
                if repo["id"] != project.integration_repository_id:
                    raise ValueError("repository changed; preview again")
                if hasattr(request, "entries"):
                    return await self._retire_on(conn, current, repo, request, operator_id)
                plan = {
                    **self._binding(current, repo), "action": "off", "reason": request.reason,
                    "effective_mode": current["hierarchical_integration_mode"],
                    "desired_mode": current["hierarchical_integration_desired_mode"],
                    "draining": current["hierarchical_integration_draining"],
                }
                result = self._result(request, plan, "disabled")
                if request.dry_run:
                    return result
                self._exact_plan(request, result["plan_digest"])
                now = self.clock()
                await conn.execute(update(t.projects).where(t.projects.c.id == request.project_id)
                                   .values(hierarchical_integration_mode="disabled",
                                           hierarchical_integration_desired_mode="disabled",
                                           hierarchical_integration_draining=False,
                                           hierarchical_integration_generation=plan["generation"] + 1))
                await conn.execute(update(t.project_integration_schedules).where(
                    t.project_integration_schedules.c.project_id == request.project_id,
                ).values(enabled=False, updated_at=now))
                await self._audit(conn, "integration.operator_off", result, operator_id)
                return result

    @staticmethod
    def _exact_plan(request, actual):
        if request.expected_plan_digest != actual:
            raise ValueError("plan changed or digest missing; preview again")

    @staticmethod
    def _result(request, plan, applied):
        plan = json.loads(json.dumps(plan))
        return {"outcome": "preview" if request.dry_run else applied,
                "project_id": request.project_id, "dry_run": request.dry_run,
                "plan": plan, "plan_digest": digest(plan)}

    async def _audit(self, conn, event, result, operator_id):
        await self.db.log_event(event, project_id=result["project_id"],
                                payload=json.dumps(result | {"principal": operator_id}), conn=conn)

    async def _retire_on(self, conn, project, repo, request, operator_id):
        from src.integration.delivery_truth import load_delivery_requests
        from src.integration.git_truth import GitTruthSnapshot
        from src.integration.provenance import CompletionIdentity, GitProvenance

        binding = self._binding(project, repo)
        table = t.integration_delivery_cutovers
        key = (table.c.project_id == project["id"], table.c.repository_id == repo["id"],
               table.c.target_ref == binding["target_ref"])
        old = (await conn.execute(select(table).where(*key))).mappings().one_or_none()
        if old is not None:
            if old["manifest"]["repository_url"] != repo["url"]:
                raise ValueError("repository binding changed since its legacy cutover")
            # Exact retries return the immutable record, even if the remote has
            # since advanced. They cannot extend the selected generations.
            selected = [{k: entry[k] for k in ("task_id", "completion_id", "source_sha",
                                              "content_sha")} for entry in old["manifest"]["entries"]]
            supplied = [entry.model_dump() for entry in sorted(request.entries,
                                                               key=lambda entry: entry.task_id)]
            if (selected != supplied or request.reason != old["manifest"]["reason"]
                or (not request.dry_run and request.expected_plan_digest != old["plan_digest"])):
                raise ValueError("this binding already has its immutable legacy cutover")
            return {"outcome": "preview" if request.dry_run else "retired",
                    "project_id": project["id"], "dry_run": request.dry_run,
                    "plan": old["manifest"], "plan_digest": old["plan_digest"]}
        if project["hierarchical_integration_mode"] != "disabled":
            raise ValueError("legacy retirement requires automatic integration off")
        ids = [entry.task_id for entry in request.entries]
        await self._retirement_guards_on(conn, project, repo, ids)
        requests = await load_delivery_requests(self.db, ids, repository_id=repo["id"],
                                                target_ref=binding["target_ref"], conn=conn,
                                                reduced=True)
        origins = (await conn.execute(select(t.task_branch_origins).where(
            t.task_branch_origins.c.task_id.in_(ids),
            t.task_branch_origins.c.repository_id == repo["id"],
            t.task_branch_origins.c.retired_at.is_(None),
        ).with_for_update())).mappings().all()
        snapshot = await self._snapshot(binding)
        if not isinstance(snapshot, GitTruthSnapshot) or not snapshot.target_oid:
            raise ValueError("Git delivery observation is unavailable")
        observation = snapshot.observation
        if (observation.project_id, observation.repository_id, observation.repository_url,
            observation.target_ref) != (binding["project_id"], binding["repository_id"],
                                       binding["repository_url"], binding["target_ref"]):
            raise ValueError("Git observation names another repository or target binding")
        provenance = GitProvenance(observation.git, observation.store, repository_url=repo["url"])
        entries = []
        for entry in sorted(request.entries, key=lambda entry: entry.task_id):
            current = requests.get(entry.task_id)
            origin = [row for row in origins if row["task_id"] == entry.task_id]
            if (current is None or current.project_id != project["id"]
                or current.repository_id != repo["id"] or current.task_status != "COMPLETED"
                or current.completion_id != entry.completion_id
                or current.completion_outcome != "pass" or len(origin) != 1):
                raise ValueError(f"{entry.task_id}: passing generation or unique origin changed")
            identity = CompletionIdentity(project["id"], repo["id"], entry.task_id,
                                          entry.completion_id)
            record = await snapshot.read_completion(identity)
            if record is None or not record["artifact"] or record["source_oid"] != entry.source_sha:
                raise ValueError(f"{entry.task_id}: original completion source is unknown")
            ref, remote = await provenance.remote_completion(identity)
            pinned = observation.source_heads.get(ref)
            if not pinned or remote.oid != pinned:
                raise ValueError(f"{entry.task_id}: original source lacks immutable remote retention")
            if await provenance.ancestor(entry.source_sha, snapshot.target_oid):
                raise ValueError(f"{entry.task_id}: source is already contained; no retirement needed")
            await provenance.exact(entry.content_sha)
            if not await provenance.ancestor(entry.content_sha, snapshot.target_oid):
                raise ValueError(f"{entry.task_id}: named content commit is outside the default target")
            content = replace(snapshot, observation=replace(observation,
                              target_oid=entry.content_sha))
            proof = await content.is_delivered(current, source_base=origin[0]["base_sha"])
            if proof.reason not in {"source_trailer", "whole_source_patch", "full_tree", "merge_noop"}:
                raise ValueError(f"{entry.task_id}: accepted equivalence evidence is unavailable")
            entries.append(entry.model_dump() | {
                "request": asdict(current), "origin": dict(origin[0]),
                "retained_ref": ref, "retained_oid": pinned,
                "equivalence": proof.reason,
            })
        plan = binding | {"action": "retire_legacy", "disposition": "delivered_by_other_means",
                          "target_oid": snapshot.target_oid, "reason": request.reason,
                          "entries": entries}
        result = self._result(request, plan, "retired")
        if not await snapshot.is_fresh():
            raise ValueError("remote default target changed; preview again")
        if request.dry_run:
            return result
        self._exact_plan(request, result["plan_digest"])
        await conn.execute(insert(table).values(
            project_id=project["id"], repository_id=repo["id"], target_ref=binding["target_ref"],
            plan_digest=result["plan_digest"], manifest=plan, operator_id=operator_id,
            created_at=self.clock(),
        ))
        await self._audit(conn, "integration.legacy_obligations_retired", result, operator_id)
        return result

    async def _snapshot(self, binding):
        from src.integration.delivery_observer import DeliveryTarget, prerequisite_observer

        target = DeliveryTarget(binding["project_id"], binding["repository_id"],
                                binding["repository_url"], binding["target_ref"])
        if self.snapshot is not None:
            return await self.snapshot(target)
        observer = prerequisite_observer(self.db) or getattr(self.db, "_delivery_observer", None)
        if observer is None:
            raise ValueError("Git delivery observer unavailable")
        return await observer.snapshot(target)

    async def _retirement_guards_on(self, conn, project, repo, ids):
        from src.database.queries.integration_state_queries import (
            session_attached_clause, unresolved_claim_clause,
        )
        from src.integration.records import PolicyActivation
        from src.operator_decisions import history_on, related_on

        if project["status"] != "ACTIVE":
            raise ValueError("project is inactive")
        # Frozen or uncertain work is preserved and blocks this one-time
        # cutover. Off itself deliberately has no such barrier.
        if await PolicyActivation._has_active_work_on(conn, project["id"]):
            raise ValueError("project has frozen work, a writer or an unresolved integration operation")
        selected = (await conn.execute(select(t.tasks).where(t.tasks.c.id.in_(ids))
                           .order_by(t.tasks.c.id).with_for_update())).mappings().all()
        if (len(selected) != len(ids) or any(row["project_id"] != project["id"]
            or row["repo_id"] != repo["id"] for row in selected)):
            raise ValueError("selected task is outside the designated repository")
        statements = (
            select(t.integration_candidate_ref_mutations.c.id).where(
                t.integration_candidate_ref_mutations.c.repository_id == repo["id"],
                t.integration_candidate_ref_mutations.c.state == "reserved"),
            select(t.sessions.c.id).where(t.sessions.c.task_id.in_(ids), session_attached_clause()),
            select(t.workspaces.c.id).where(t.workspaces.c.locked_by_task_id.in_(ids)),
            select(t.task_metadata.c.task_id).where(t.task_metadata.c.task_id.in_(ids),
                t.task_metadata.c.key == "claimed_by_session", unresolved_claim_clause()),
            select(t.task_metadata.c.task_id).where(t.task_metadata.c.task_id.in_(ids),
                t.task_metadata.c.key == "manual_pause"),
            select(t.task_labels.c.task_id).where(t.task_labels.c.task_id.in_(ids),
                t.task_labels.c.label.like("hold:%")),
            select(t.task_gates.c.task_id).join(t.gates).where(
                t.task_gates.c.task_id.in_(ids), t.gates.c.status == "open"),
        )
        for statement in statements:
            if await conn.scalar(statement.limit(1)):
                raise ValueError("selected task has a claim, hold, gate or unresolved writer")
        refs = set()
        for task_id in ids:
            refs.update(await related_on(conn, "task", task_id))
        if any(row["active"] for row in await history_on(conn, project["id"], refs)):
            raise ValueError("binding operator decision holds the selected work")
        parent_ids = select(t.tasks.c.id).where(t.tasks.c.project_id == project["id"])
        batch_ids = select(t.integration_batches.c.id).where(
            t.integration_batches.c.project_id == project["id"])
        publications = (
            select(t.integration_repair_operations.c.id).where(
                (t.integration_repair_operations.c.parent_task_id.in_(parent_ids)
                 | t.integration_repair_operations.c.batch_id.in_(batch_ids)),
                t.integration_repair_operations.c.state.not_in(("completed", "cancelled"))),
            select(t.integration_candidate_resolutions.c.id).where(
                t.integration_candidate_resolutions.c.batch_id.in_(batch_ids),
                t.integration_candidate_resolutions.c.state.in_(("reserved", "pushed"))),
            select(t.integration_attestation_publications.c.id).where(
                t.integration_attestation_publications.c.batch_id.in_(batch_ids),
                t.integration_attestation_publications.c.state == "reserved"),
        )
        for statement in publications:
            if await conn.scalar(statement.limit(1)):
                raise ValueError("integration publication or operation remains unresolved")
