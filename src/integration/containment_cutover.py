"""One-time guarded preparation of strict-containment history, never activation."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

from sqlalchemy import text, update

from src.database import tables as t
from src.git.manager import is_valid_git_oid
from src.integration.containment_audit import (
    MAX_AUDIT_BYTES, MAX_GENERATIONS, SCHEMA, build_audit, canonical, digest,
    insert_audit, load_audits,
)
from src.integration.containment_inventory import FENCED_TABLES, inventory, writer_blockers
from src.integration.delivery_truth import (
    CONTAINMENT_CONTRACT_VERSION,
    DELIVERED_BY_OTHER_MEANS,
    DeliveryState,
    delivered,
    load_delivery_requests,
)
from src.integration.engine import RootEngineOwnership
from src.integration.provenance import CompletionIdentity, GitProvenance

ROLLBACK = {
    "boundary": "prepared_history_only",
    "preserve": ["dirty_work", "original_objects", "private_refs", "new_heads", "audit"],
    "restore": "only a compatible strict-containment publisher/configuration",
    "incompatible_binary": "remain paused and fix forward",
    "forbidden": ["receipt_authority", "target_reset", "destructive_schema_rollback"],
}


def ordered(facts):
    return {**facts, "inventory": {
        key: sorted(rows, key=canonical) for key, rows in facts["inventory"].items()
    }}


def classification(answer, legacy):
    if answer.disposition:
        return "retired"
    if answer.state is DeliveryState.NOT_APPLICABLE:
        return "code_free"
    if answer.state is DeliveryState.CONTAINED:
        return "already_contained"
    if answer.state is DeliveryState.UNKNOWN:
        return "missing_or_ambiguous_source"
    if legacy.state is DeliveryState.CONTAINED:
        return "equivalent_only"
    return "pending_retained_source"


class ContainmentCutover:
    def __init__(self, db, git, *, runtime_bindings=lambda: {}, clock=time.time):
        self.db, self.git = db, git
        self.runtime_bindings, self.clock = runtime_bindings, clock

    async def _stored(self, facts, conn):
        return await load_audits(conn, [(facts["project"]["id"], facts["repository"]["id"])])

    def _replay(self, prior, expected_digest, facts):
        if prior["plan_digest"] != expected_digest:
            raise ValueError("already_prepared: later history cannot receive cutover exclusions")
        if prior["configuration_generation"] != facts["project"]["hierarchical_integration_generation"]:
            raise ValueError("already_prepared: configuration generation has changed")
        expected = json.loads(canonical(prior["manifest"]["facts"]))
        expected["project"]["hierarchical_integration_generation"] = prior["configuration_generation"]
        if facts != expected:
            raise ValueError("already_prepared: configuration or completion history has changed")
        return {"outcome": "prepared", "plan": prior["manifest"], "replayed": True,
                "generation": prior["configuration_generation"]}

    async def _facts(self, project_id, conn):
        facts = ordered(await inventory(conn, project_id, now=self.clock()))
        # Copy mutable config: a concurrent reload cannot change the preview by alias.
        facts["runtime_bindings"] = json.loads(canonical(self.runtime_bindings()))
        return facts

    async def _requests(self, facts, conn):
        ids = [row["id"] for family in ("tasks", "archived")
               for row in facts["inventory"][family] if row["status"] == "COMPLETED"]
        return await load_delivery_requests(
            self.db, ids, repository_id=facts["repository"]["id"],
            target_ref="refs/heads/" + facts["repository"]["default_branch"].removeprefix(
                "refs/heads/"), conn=conn,
        )

    async def run(self, project_id, *, choices=None, plan=None, expected_digest=None,
                  dry_run=True, operator_id=None):
        choices = choices or {}
        if not dry_run:
            if (not operator_id or not plan or not expected_digest
                or plan.get("digest") != expected_digest or digest(plan) != expected_digest
                or plan.get("facts", {}).get("project", {}).get("id") != project_id):
                raise ValueError("plan_digest_required: apply needs the exact saved manifest digest")
            if choices and choices != plan.get("choices"):
                raise ValueError("choices_changed: generate a new preview")
            choices = plan["choices"]
        async with self.db._engine.connect() as conn:
            facts = await self._facts(project_id, conn)
            stored = await self._stored(facts, conn)
            if stored and not dry_run:
                return self._replay(stored[0], expected_digest, facts)
            requests = await self._requests(facts, conn)
        async with self.git.observe(facts) as snapshot:
            external = await self.git.external(facts, snapshot)
            current = await self._manifest(facts, requests, snapshot, external, choices)
            if dry_run:
                return {"outcome": "preview", "plan": current, "blockers": current["blockers"]}
            if current["digest"] != expected_digest:
                raise ValueError("stale_plan: configuration, sources, refs, choices or writers changed")
            if current["blockers"]:
                return {"outcome": "blocked", "plan": current, "blockers": current["blockers"]}
            # Validate the entire bounded audit before publishing any retention ref.
            audit = build_audit(current, operator_id, self.clock())
            targets = {row["parent_ref"] for row in current["rows"]
                       if row["parent"] and row["parent"]["target_oid"]}
            targets.add(snapshot.observation.target_ref)
            if not all([await snapshot.for_target(ref).is_fresh() for ref in targets]):
                raise ValueError("target_changed: re-preview before applying")
            # Existing markers are verified before any authority/disposition changes.
            retained = await self.git.retain(snapshot, current["rows"])
            # Re-fetch all branch/private refs, including new writers. Only the exact
            # additive markers this operation just verified may differ from the plan.
            async with self.git.observe(facts) as fresh_snapshot:
                fresh_external = await self.git.external(facts, fresh_snapshot)
                expected_external = {**external, "refs": {**external["refs"], **retained}}
                if fresh_snapshot.error or fresh_external != expected_external:
                    raise ValueError("external_fence_changed: refs, protection or checkout changed")
                if not all([await fresh_snapshot.for_target(ref).is_fresh() for ref in targets]):
                    raise ValueError("target_changed_during_retention")
            async with asyncio.timeout(10):
                return await self._apply(audit, requests)

    async def _manifest(self, facts, requests, snapshot, external, choices):
        blockers = writer_blockers(facts)
        if not external.get("protection"):
            blockers.append({"code": "protection_unavailable"})
        for checkout in external["checkouts"]:
            if checkout.get("error"):
                blockers.append({"code": checkout["error"], "path": checkout["path"]})
        if snapshot.error:
            blockers.append({"code": "git_observation_unavailable"})
        origins = {r["task_id"]: r for r in facts["inventory"]["origins"]
                   if r["retired_at"] is None}
        rows = []
        for task_id, request in sorted(requests.items()):
            origin = origins.get(task_id, {})
            strict = await delivered(request, request.target_ref, observation=snapshot)
            legacy = await snapshot.is_delivered(request, source_base=origin.get("base_sha"))
            kind = classification(strict, legacy)
            parent_ref = origin.get("parent_ref") if origin.get("parent_task_id") else None
            parent = None
            if parent_ref:
                parent_ref = "refs/heads/" + parent_ref.removeprefix("refs/heads/")
                parent_answer = await delivered(replace(request, target_ref=parent_ref), parent_ref,
                                               observation=snapshot.for_target(parent_ref))
                parent = {"state": parent_answer.state.value, "reason": parent_answer.reason,
                          "target_oid": parent_answer.target_oid}
                if origin.get("parent_repository_id") not in {None, request.repository_id}:
                    blockers.append({"code": "parent_repository_mismatch", "task_id": task_id})
                elif (parent_answer.state is DeliveryState.UNKNOWN
                      and strict.state is not DeliveryState.CONTAINED):
                    blockers.append({"code": "unresolved_parent_history", "task_id": task_id})
            row = {
                "task_id": task_id, "completion_id": request.completion_id,
                "source_oid": strict.source_oid, "base_oid": origin.get("base_sha"),
                "target_ref": request.target_ref, "target_oid": strict.target_oid,
                "parent_ref": parent_ref, "parent": parent,
                "classification": kind, "strict_state": strict.state.value,
                "strict_reason": strict.reason, "legacy_state": legacy.state.value,
                "legacy_reason": legacy.reason,
                "verified_parent": request.parent_completion is not None,
                "provenance_ref": CompletionIdentity(
                    request.project_id, request.repository_id, task_id, request.completion_id,
                ).ref if request.completion_id else None,
                "action": {"already_contained": "retain", "code_free": "exclude_code_free",
                           "retired": "preserve_disposition", "equivalent_only": "choose",
                           "pending_retained_source": "retain_then_redrive",
                           "missing_or_ambiguous_source": "resolve_history"}[kind],
            }
            choice = choices.get(request.completion_id)
            if kind == "missing_or_ambiguous_source":
                blockers.append({"code": "unresolved_history", "task_id": task_id})
            elif kind == "equivalent_only":
                await self._choice(row, choice, request, snapshot, blockers)
            elif choice:
                blockers.append({"code": "choice_not_equivalent_only", "task_id": task_id})
            rows.append(row)
        for completion_id in choices.keys() - {r["completion_id"] for r in rows}:
            blockers.append({"code": "unknown_choice_generation", "completion_id": completion_id})
        manifest = {
            "schema": SCHEMA, "contract_version": CONTAINMENT_CONTRACT_VERSION,
            "authority": "unchanged", "stage": "history_preparation",
            "facts": facts, "external": external, "choices": choices, "rows": rows,
            "writers": [{**blocker, "classification": "active_writer_or_uncertain_publish",
                         "action": "finish_or_prove_stopped_then_preview"}
                        for blocker in blockers if blocker["code"].startswith(("live_", "active_"))],
            "blockers": blockers, "rollback": ROLLBACK,
            "observed_at": snapshot.observation.observed_at,
            "observation_token": snapshot.observation.observation_token,
        }
        if (len(rows) > MAX_GENERATIONS
            or len(canonical(manifest).encode()) > MAX_AUDIT_BYTES):
            blockers.append({"code": "manifest_limit_exceeded"})
        manifest["digest"] = digest(manifest)
        return manifest

    async def _choice(self, row, choice, request, snapshot, blockers):
        code = "legacy_disposition_required"
        if choice and choice.get("action") == "reconstruct":
            row["action"] = "reconstruct_reviewed_merge_then_preview"
            code = "reconstruction_required"
        elif choice and choice.get("action") == DELIVERED_BY_OTHER_MEANS:
            oid = choice.get("carrying_oid")
            if (choice.get("source_oid") != row["source_oid"] or not is_valid_git_oid(oid)
                or choice.get("equivalence_proof") != row["legacy_reason"]
                or not isinstance(choice.get("evidence"), str) or not choice["evidence"].strip()):
                code = "legacy_evidence_invalid"
            else:
                observation = snapshot.observation
                provenance = GitProvenance(observation.git, observation.store,
                                           repository_url=observation.repository_url)
                if await provenance.strict_ancestor(oid, row["target_oid"]):
                    # Recheck the old evaluator at the explicitly named carrying commit.
                    at_carrying = replace(snapshot, observation=replace(
                        observation, target_oid=oid,
                    ))
                    proof = await at_carrying.is_delivered(request, source_base=row["base_oid"])
                    if proof.state is DeliveryState.CONTAINED:
                        row["action"] = DELIVERED_BY_OTHER_MEANS
                        return
                code = "carrying_commit_unverified"
        blockers.append({"code": code, "task_id": row["task_id"]})

    async def _apply(self, audit, requests):
        plan = audit["manifest"]
        facts = plan["facts"]
        project_id, repository_id = facts["project"]["id"], facts["repository"]["id"]
        async with RootEngineOwnership(self.db).exclusion(repository_id) as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
            await conn.execute(text("LOCK TABLE " + ", ".join(FENCED_TABLES)
                                    + " IN SHARE ROW EXCLUSIVE MODE"))
            fresh = await self._facts(project_id, conn)
            stored = await self._stored(fresh, conn)
            if stored:
                return self._replay(stored[0], plan["digest"], fresh)
            if fresh != facts or await self._requests(fresh, conn) != requests:
                raise ValueError("stale_plan: writer, generation or configuration changed at apply")
            if writer_blockers(fresh):
                raise ValueError("live_writer: stopped-writer proof no longer holds")
            generation = audit["configuration_generation"]
            await insert_audit(conn, audit)
            await conn.execute(update(t.projects).where(t.projects.c.id == project_id).values(
                hierarchical_integration_generation=generation,
            ))
        return {"outcome": "prepared", "plan": plan, "generation": generation, "replayed": False}
