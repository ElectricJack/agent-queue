"""Operator adoption of a quiet aggregate whose children were delivered elsewhere."""

from __future__ import annotations

import json
import time
from uuid import uuid4

from sqlalchemy import insert, or_, select, update

from src.database import tables as t
from src.database.queries.task_queries import _OPERATOR_ADOPTION_TOKEN
from src.integration.cancelled_collection_recovery import CancelledCollectionRecovery
from src.integration.delegate_release import release_delegates_on
from src.integration.delivery_truth import (
    PARENT_ADOPTION_EVENT,
    DeliveryState,
    load_delivery_requests,
)
from src.integration.development import armed_for_branch_cleanup
from src.integration.parent_engine import legacy_parent_allowed_on, parent_engine_guard
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.recovery_controls import IntegrationRecoveryControls
from src.models import TaskStatus


class DeliveredParentAdoption:
    def __init__(self, service):
        self.service, self.db = service, service.db

    async def facts_on(self, conn, task_id):
        """Gather the complete mutation fence; callers serialize with the project lock."""
        parent = (
            (
                await conn.execute(
                    select(t.tasks).where(
                        t.tasks.c.id == task_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if parent is None:
            raise ValueError("parent does not exist")
        project = (
            (
                await conn.execute(
                    select(t.projects).where(
                        t.projects.c.id == parent["project_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        repo = (
            (
                await conn.execute(
                    select(t.repos).where(
                        t.repos.c.id == project["integration_repository_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        checkpoint = (
            (
                await conn.execute(
                    select(t.task_integration_checkpoints).where(
                        t.task_integration_checkpoints.c.task_id == task_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
            or project["hierarchical_integration_draining"]
            or parent["status"] != "PAUSED"
            or repo is None
            or repo["project_id"] != project["id"]
            or parent["repo_id"] != repo["id"]
            or checkpoint is None
            or not checkpoint["episode_id"]
            or checkpoint["repository_id"] != repo["id"]
            or checkpoint["branch"] != parent["branch_name"]
            or not parent["branch_name"]
            or parent["branch_name"].removeprefix("refs/heads/") == repo["default_branch"]
        ):
            raise ValueError("a PAUSED managed parent with a canonical episode is required")
        if not await legacy_parent_allowed_on(conn, self.db, task_id):
            raise ValueError("parent belongs to the reconciler or has a binding human gate")
        episode = (
            (
                await conn.execute(
                    select(t.integration_parent_episodes).where(
                        t.integration_parent_episodes.c.id == checkpoint["episode_id"],
                        t.integration_parent_episodes.c.parent_task_id == task_id,
                        t.integration_parent_episodes.c.repository_id == repo["id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        operations = (
            (
                await conn.execute(
                    select(t.integration_repair_operations).where(
                        t.integration_repair_operations.c.parent_task_id == task_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        operation = next(
            (op for op in operations if op["episode_id"] == checkpoint["episode_id"]), None
        )
        if (
            episode is None
            or checkpoint["generation"] < episode["generation"]
            or operation is None
            or operation["target_kind"] != "parent"
            or operation["state"] not in {"active", "escalated", "cancelled"}
            or any(
                op["id"] != operation["id"]
                and op["state"]
                in {
                    "active",
                    "escalated",
                    "human_required",
                }
                for op in operations
            )
        ):
            raise ValueError("current collection episode/operation is inconsistent or human-held")
        stages = (
            (
                await conn.execute(
                    select(t.integration_repair_stages)
                    .where(
                        t.integration_repair_stages.c.operation_id == operation["id"],
                    )
                    .order_by(t.integration_repair_stages.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
        resolutions = (
            (
                await conn.execute(
                    select(t.integration_candidate_resolutions)
                    .where(
                        t.integration_candidate_resolutions.c.operation_id == operation["id"],
                    )
                    .order_by(t.integration_candidate_resolutions.c.id)
                )
            )
            .mappings()
            .all()
        )
        delegate_ids = sorted(
            {stage["repair_task_id"] for stage in stages if stage["repair_task_id"]}
            | {row["repair_task_id"] for row in resolutions if row["repair_task_id"]}
            | ({operation["verifier_task_id"]} if operation["verifier_task_id"] else set())
        )
        delegates = (
            (
                await conn.execute(
                    select(t.tasks)
                    .where(
                        t.tasks.c.id.in_(delegate_ids),
                    )
                    .order_by(t.tasks.c.id)
                )
            )
            .mappings()
            .all()
        )
        children = (
            (
                await conn.execute(
                    select(t.tasks)
                    .where(
                        t.tasks.c.parent_task_id == task_id,
                    )
                    .order_by(t.tasks.c.id)
                )
            )
            .mappings()
            .all()
        )
        if not children or any(child["status"] != "COMPLETED" for child in children):
            raise ValueError("every current child must be COMPLETED")
        if any(
            child["project_id"] != project["id"]
            or child["repo_id"]
            not in {
                None,
                repo["id"],
            }
            for child in children
        ):
            raise ValueError("a child belongs to another repository or project")
        scope_ids = sorted({task_id, *delegate_ids, *(child["id"] for child in children)})
        for row in [parent, *delegates, *children]:
            if row["assigned_agent_id"] or row["status"] in {"ASSIGNED", "IN_PROGRESS"}:
                raise ValueError(f"{row['id']}: still has a worker assignment")
        for identity in scope_ids:
            if await CancelledCollectionRecovery._live_holder_on(conn, identity):
                raise ValueError(f"{identity}: session, claim or workspace is still retained")
        holds = (
            await conn.execute(
                select(t.task_metadata.c.task_id, t.task_metadata.c.key)
                .where(
                    t.task_metadata.c.task_id.in_(scope_ids),
                    t.task_metadata.c.key.in_(("manual_pause", "blocked_terminal")),
                )
                .order_by(t.task_metadata.c.task_id, t.task_metadata.c.key)
            )
        ).all()
        if holds:
            raise ValueError(
                f"{holds[0][0]}: operator/terminal hold {holds[0][1]} must be resolved"
            )
        label_hold = await conn.scalar(
            select(t.task_labels.c.task_id)
            .where(
                t.task_labels.c.task_id.in_(scope_ids),
                t.task_labels.c.label.like("hold:%"),
            )
            .limit(1)
        )
        if label_hold:
            raise ValueError(f"{label_hold}: operator hold label must be resolved")
        gate = await conn.scalar(
            select(t.gates.c.id)
            .join(
                t.task_gates,
                t.task_gates.c.gate_id == t.gates.c.id,
            )
            .where(t.task_gates.c.task_id.in_(scope_ids), t.gates.c.status == "open")
            .limit(1)
        )
        if gate:
            raise ValueError(f"open human gate {gate} must be resolved")
        branch = parent["branch_name"].removeprefix("refs/heads/")
        owners = (
            (
                await conn.execute(
                    select(t.integration_branch_owners)
                    .where(
                        or_(
                            t.integration_branch_owners.c.owner_id.in_(
                                [operation["id"], *scope_ids]
                            ),
                            (t.integration_branch_owners.c.repository_id == repo["id"])
                            & t.integration_branch_owners.c.ref.in_(
                                (branch, "refs/heads/" + branch)
                            ),
                        )
                    )
                    .order_by(t.integration_branch_owners.c.id)
                )
            )
            .mappings()
            .all()
        )
        for owner in owners:
            if owner["handoff_state"] == "released":
                continue
            if (
                owner["handoff_state"] != "reserved"
                or owner["session_id"]
                or owner["workspace_id"]
                or owner["confirmed_workspace_id"]
                or owner["repository_id"] != repo["id"]
                or owner["ref"].removeprefix("refs/heads/") != branch
                or owner["owner_id"] not in {task_id, operation["id"], *delegate_ids}
            ):
                raise ValueError(f"branch owner {owner['id']} must be recovered first")
        # Only the exact quiet branch reservation may be exempted from writer
        # ambiguity. Every irreversible/pending external write remains binding.
        for owner in owners:
            blockers = await IntegrationRecoveryControls._ambiguous_writes_on(
                conn,
                operation,
                allowed_writer_id=owner["id"],
            )
            if not blockers:
                break
        else:
            blockers = await IntegrationRecoveryControls._ambiguous_writes_on(conn, operation)
        for table, column, condition in (
            (
                t.integration_promotion_intents,
                t.integration_promotion_intents.c.target_branch,
                t.integration_promotion_intents.c.state.not_in(
                    ("committed", "conflict", "superseded")
                ),
            ),
            (
                t.integration_candidate_ref_mutations,
                t.integration_candidate_ref_mutations.c.branch,
                t.integration_candidate_ref_mutations.c.state == "reserved",
            ),
        ):
            if await conn.scalar(
                select(table.c.id)
                .where(
                    table.c.repository_id == repo["id"],
                    column.in_((branch, "refs/heads/" + branch)),
                    condition,
                )
                .limit(1)
            ):
                blockers.append(table.name)
        if blockers:
            raise ValueError("unresolved external write: " + ", ".join(sorted(set(blockers))))
        requests = await load_delivery_requests(
            self.db,
            [child["id"] for child in children],
            repository_id=repo["id"],
            target_ref="refs/heads/" + repo["default_branch"],
            conn=conn,
        )
        return {
            "parent": dict(parent),
            "project": dict(project),
            "repo": dict(repo),
            "checkpoint": dict(checkpoint),
            "operation": dict(operation),
            "episode": dict(episode),
            "stages": [dict(row) for row in stages],
            "resolutions": [dict(row) for row in resolutions],
            "delegates": [dict(row) for row in delegates],
            "delegate_ids": delegate_ids,
            "children": [dict(row) for row in children],
            "requests": requests,
            "owners": [dict(row) for row in owners],
        }

    async def prove(self, facts, truth, *, accept_equivalent=False):
        proofs = []
        for request in facts["requests"].values():
            proof = await truth.evaluate(request)
            if proof.state not in {DeliveryState.CONTAINED, DeliveryState.NO_ARTIFACT}:
                raise ValueError(
                    f"{request.task_id}: child delivery is {proof.state}: {proof.reason}"
                )
            if (
                proof.state == DeliveryState.NO_ARTIFACT
                and proof.reason == "branchless_organization"
            ):
                raise ValueError(
                    f"{request.task_id}: an explicit no-artifact completion is required"
                )
            kind = "no_artifact"
            if proof.state == DeliveryState.CONTAINED:
                provenance = GitProvenance(
                    self.service.git, truth.store, repository_url=facts["repo"]["url"]
                )
                kind = (
                    "ancestry"
                    if await provenance.ancestor(proof.source_oid, truth.target_oid)
                    else "equivalent"
                )
                if kind == "equivalent" and not accept_equivalent:
                    raise ValueError(
                        f"{request.task_id}: equivalent child delivery requires --accept-equivalent"
                    )
            proofs.append(
                {
                    "task_id": request.task_id,
                    "completion_id": request.completion_id,
                    "source_sha": proof.source_oid,
                    "state": proof.state.value,
                    "proof": proof.reason,
                    "kind": kind,
                }
            )
        return proofs

    async def lock_facts_on(self, conn, facts):
        """Pin observed rows while Git evidence is retained and the close commits."""
        for table, column, identities in (
            (t.projects, t.projects.c.id, [facts["project"]["id"]]),
            (t.repos, t.repos.c.id, [facts["repo"]["id"]]),
            (
                t.task_integration_checkpoints,
                t.task_integration_checkpoints.c.task_id,
                [facts["parent"]["id"]],
            ),
            (
                t.integration_repair_operations,
                t.integration_repair_operations.c.id,
                [facts["operation"]["id"]],
            ),
            (
                t.integration_repair_stages,
                t.integration_repair_stages.c.operation_id,
                [facts["operation"]["id"]],
            ),
            (
                t.integration_branch_owners,
                t.integration_branch_owners.c.id,
                [owner["id"] for owner in facts["owners"]],
            ),
            (
                t.integration_candidate_resolutions,
                t.integration_candidate_resolutions.c.operation_id,
                [facts["operation"]["id"]],
            ),
            (
                t.tasks,
                t.tasks.c.id,
                [
                    facts["parent"]["id"],
                    *facts["delegate_ids"],
                    *(child["id"] for child in facts["children"]),
                ],
            ),
        ):
            await conn.execute(
                select(table).where(column.in_(identities)).order_by(column).with_for_update()
            )

    @parent_engine_guard(outcome="blocked")
    async def run(
        self,
        task_id,
        *,
        project_id,
        target_ref,
        head_sha,
        reason,
        operator_id,
        dry_run=False,
        accept_equivalent=False,
    ):
        async with self.db._engine.connect() as conn:
            facts = await self.facts_on(conn, task_id)
        if facts["project"]["id"] != project_id:
            raise ValueError("parent does not belong to this project")
        if target_ref != "refs/heads/" + facts["repo"]["default_branch"]:
            raise ValueError("delivered-child adoption requires the designated default branch")
        repo = await self.db.get_repo(facts["repo"]["id"])
        async with self.service.read_snapshot(repo, target_ref) as truth:
            if truth.error or truth.target_oid != head_sha:
                raise ValueError(
                    "target ref is no longer at the supplied SHA or cannot be observed"
                )
            proofs = await self.prove(facts, truth, accept_equivalent=accept_equivalent)
            report = {
                "task_id": task_id,
                "head_sha": head_sha,
                "operation_id": facts["operation"]["id"],
                "verifier_task_id": facts["operation"]["verifier_task_id"],
                "children": proofs,
                "retire_delegates": facts["delegate_ids"],
                "conclusion": "not_ci_attested",
                "accept_equivalent": accept_equivalent,
            }
            if dry_run:
                return {"outcome": "would_adopt_parent", **report}
            async with self.service.exclusion(repo.id):
                async with self.db.immediate() as conn:
                    await self.db.lock_hierarchy_project(conn, project_id)
                    await self.lock_facts_on(conn, facts)
                    current = await self.facts_on(conn, task_id)
                    if current != facts:
                        raise ValueError("parent or child generation changed; repeat the dry run")
                    if not await truth.is_fresh():
                        raise ValueError("target moved before adoption; repeat the dry run")
                    completion_id, identity = str(uuid4()), str(uuid4())
                    provenance = GitProvenance(
                        self.service.git, truth.store, repository_url=repo.url
                    )
                    await provenance.write_completion(
                        CompletedSource(
                            CompletionIdentity(project_id, repo.id, task_id, completion_id),
                            head_sha,
                        )
                    )
                    now = time.time()
                    operation = facts["operation"]
                    await conn.execute(
                        update(t.integration_repair_operations)
                        .where(
                            t.integration_repair_operations.c.id == operation["id"],
                        )
                        .values(state="cancelled", updated_at=now)
                    )
                    await conn.execute(
                        update(t.integration_repair_stages)
                        .where(
                            t.integration_repair_stages.c.operation_id == operation["id"],
                            t.integration_repair_stages.c.state.not_in(
                                ("passed", "failed", "expired", "cancelled")
                            ),
                        )
                        .values(state="cancelled", completed_at=now)
                    )
                    for owner in facts["owners"]:
                        if owner["handoff_state"] != "released":
                            await conn.execute(
                                update(t.integration_branch_owners)
                                .where(
                                    t.integration_branch_owners.c.id == owner["id"],
                                )
                                .values(
                                    handoff_state="released",
                                    fence_token=owner["fence_token"] + 1,
                                    expires_at=None,
                                    updated_at=now,
                                )
                            )
                    _releases, transitions = await release_delegates_on(
                        self.db,
                        conn,
                        now=now,
                        released_by=operator_id,
                        operation_ids=[operation["id"]],
                        limit=len(facts["delegate_ids"]) + 1,
                    )
                    unfinished = await conn.scalar(
                        select(t.tasks.c.id)
                        .where(
                            t.tasks.c.id.in_(facts["delegate_ids"]),
                            t.tasks.c.status.not_in(("COMPLETED", "FAILED")),
                        )
                        .limit(1)
                    )
                    if unfinished:
                        raise ValueError(f"{unfinished}: delegate retirement did not settle")
                    checkpoint = facts["checkpoint"]
                    await conn.execute(
                        update(t.task_integration_checkpoints)
                        .where(
                            t.task_integration_checkpoints.c.task_id == task_id,
                        )
                        .values(
                            version=checkpoint["version"] + 1, branch_owner_id=None, updated_at=now
                        )
                    )
                    await conn.execute(
                        insert(t.task_completion_records).values(
                            id=completion_id,
                            task_id=task_id,
                            outcome="pass",
                            summary=reason,
                            commits=json.dumps([head_sha]),
                            verification="Operator child-delivery adoption; not CI attested",
                            notes=f"Adoption operation {identity}; operator {operator_id}",
                            completed_at=now,
                        )
                    )
                    transition = await self.db._apply_transition(
                        conn,
                        task_id,
                        TaskStatus.COMPLETED,
                        context="operator_adopt_delivery",
                        _operator_adoption_token=_OPERATOR_ADOPTION_TOKEN,
                        _manual_pause_control=True,
                    )
                    transitions.append(transition)
                    audit = {
                        **report,
                        "project_id": project_id,
                        "repository_id": repo.id,
                        "target_ref": target_ref,
                        "completion_id": completion_id,
                        "operator_id": operator_id,
                        "reason": reason,
                        "adoption_id": identity,
                        "episode_id": checkpoint["episode_id"],
                        "generation": checkpoint["generation"],
                        "checkpoint_sha": checkpoint["checkpoint_sha"],
                        "checkpoint_version": checkpoint["version"] + 1,
                        "branch": checkpoint["branch"],
                        "completed_at": now,
                    }
                    await conn.execute(
                        insert(t.events).values(
                            event_type=PARENT_ADOPTION_EVENT,
                            project_id=project_id,
                            task_id=task_id,
                            payload=json.dumps(audit),
                            timestamp=now,
                        )
                    )
                    await conn.execute(
                        self.service._operation_insert(
                            id=identity,
                            project_id=project_id,
                            repository_id=repo.id,
                            target_ref=target_ref,
                            expected_sha=head_sha,
                            prepared_sha=head_sha,
                            state="finished",
                            manifest=[
                                {
                                    "task_id": task_id,
                                    "source_sha": head_sha,
                                    "acceptance": "delivered_children",
                                }
                            ],
                            evidence=armed_for_branch_cleanup(
                                {
                                    "kind": "operator_accepted",
                                    "operator_id": operator_id,
                                    "conclusion": "not_ci_attested",
                                    "validation": "child_git_proof",
                                    "parent_adoption": audit,
                                }
                            ),
                            reason=reason,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    if not await truth.is_fresh():
                        raise ValueError("target moved during adoption; repeat the dry run")
        for transition in transitions:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        return {"outcome": "adopted", "id": identity, **report}
