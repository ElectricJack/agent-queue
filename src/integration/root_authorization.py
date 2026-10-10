"""Explicit operator authorization of one exact train root source.

A train project with root ``admission: authorized`` admits completed
feature/bugfix roots and the ids its policy lists.  Changing that list means a
policy generation swap, which withdraws every pending source's authorization and
source-CI evidence, so it is only configurable while the train is drained.
This records an operator's decision for one exact source instead: the kind
check in :meth:`ReviewEvidenceProducer._authorization_on` accepts it, and
every other admission guard (holds, gates, rejections, admission mode, Git
proof, generation-pinned evidence and source CI) stays as it is.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from sqlalchemy import exists, insert, select

from src.database.tables import (
    gates,
    integration_review_evidence,
    integration_root_authorizations,
    projects,
    task_branch_origins,
    task_gates,
    task_labels,
    tasks,
)
from src.integration.models import HierarchicalIntegrationPolicy

_AUTHORIZATION_NAMESPACE = uuid.UUID("0c6f7f43-5a52-4c55-9d0f-7c2b8e1f4a36")
POLICY_KINDS = frozenset({"feature", "bugfix"})


def _identity_conditions(task_id: str, source: dict[str, Any]) -> list:
    table = integration_root_authorizations
    return [
        table.c.project_id == source["project_id"],
        table.c.task_id == task_id,
        table.c.repository_id == source["repository_id"],
        table.c.source_base == source["base"],
        table.c.source_head == source["head"],
        table.c.generation == source["generation"],
    ]


async def exact_root_authorization_on(
    conn, task_id: str, source: dict[str, Any]
) -> dict[str, Any] | None:
    """The grant for exactly this source, or ``None``."""
    row = (
        await conn.execute(
            select(integration_root_authorizations).where(*_identity_conditions(task_id, source))
        )
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


class RootAuthorization:
    """Dry-run first, head-fenced, idempotent recording of one exact grant."""

    def __init__(self, db, *, clock=time.time, train_blockers=None) -> None:
        self.db = db
        self.clock = clock
        # Set only while the integration train runs (``git_first: active``):
        # ``train_blockers(task_id)`` answers why the train has not delivered
        # a task (``IntegrationStatusService.train_task_blockers``).
        self.train_blockers = train_blockers

    async def run(
        self,
        task_id: str,
        *,
        dry_run: bool = True,
        expected_head_sha: str | None = None,
        reason: str | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        if dry_run:
            async with self.db._engine.connect() as conn:
                state = await self._state(conn, task_id)
            if state["outcome"] != "candidate":
                return state
            return {**state, "outcome": "would_authorize"}
        if not expected_head_sha or not reason or not reason.strip() or not operator_id:
            return {
                "outcome": "invalid",
                "task_id": task_id,
                "reason": "applying requires the dry run's head, a reason and an operator",
            }
        project_id = await self._project_of(task_id)
        if project_id is None:
            return {"outcome": "not_found", "task_id": task_id}
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            state = await self._state(conn, task_id)
            if state["outcome"] != "candidate":
                return state
            if state["project_id"] != project_id:
                return {**state, "outcome": "changed", "reason": "task project changed"}
            if state["head_sha"] != expected_head_sha:
                return {
                    **state,
                    "outcome": "changed",
                    "reason": "source head differs from the dry run",
                }
            now = self.clock()
            identity = ":".join((
                state["project_id"], task_id, state["repository_id"], state["base_sha"],
                state["head_sha"], str(state["generation"]),
            ))
            authorization_id = (
                f"root-authorization-{uuid.uuid5(_AUTHORIZATION_NAMESPACE, identity)}"
            )
            await conn.execute(
                insert(integration_root_authorizations).values(
                    id=authorization_id,
                    project_id=state["project_id"],
                    task_id=task_id,
                    repository_id=state["repository_id"],
                    source_base=state["base_sha"],
                    source_head=state["head_sha"],
                    generation=state["generation"],
                    review_kind=state["review_kind"],
                    pr_url=state["pr_url"],
                    task_type=state["task_type"],
                    policy_generation=state["policy_generation"],
                    operator_id=operator_id,
                    reason=reason,
                    created_at=now,
                )
            )
            await self.db.log_event(
                "integration.root_authorized",
                project_id=state["project_id"],
                task_id=task_id,
                payload=json.dumps(
                    {
                        "authorization_id": authorization_id,
                        "repository_id": state["repository_id"],
                        "base_sha": state["base_sha"],
                        "head_sha": state["head_sha"],
                        "generation": state["generation"],
                        "task_type": state["task_type"],
                        "pr_url": state["pr_url"],
                        "policy_generation": state["policy_generation"],
                        "operator_id": operator_id,
                        "reason": reason,
                        "at": now,
                    }
                ),
                conn=conn,
            )
        return {
            **state,
            "outcome": "authorized",
            "authorization_id": authorization_id,
            "authorized_by": "grant",
        }

    async def _project_of(self, task_id: str) -> str | None:
        async with self.db._engine.connect() as conn:
            return (
                await conn.execute(select(tasks.c.project_id).where(tasks.c.id == task_id))
            ).scalar_one_or_none()

    async def _state(self, conn, task_id: str) -> dict[str, Any]:
        """Classify one task; ``candidate`` carries the exact source to record."""
        from src.integration.review_evidence import ReviewEvidenceProducer

        task = (
            await conn.execute(
                select(tasks.c.project_id, tasks.c.task_type).where(tasks.c.id == task_id)
            )
        ).mappings().one_or_none()
        if task is None:
            return {"outcome": "not_found", "task_id": task_id}
        base = {"task_id": task_id, "project_id": task["project_id"]}
        source = await ReviewEvidenceProducer(self.db, None)._pull_request_source_on(
            conn, task_id
        )
        if source is None:
            return await self._train_root_state(conn, task_id, base)
        project = (
            await conn.execute(
                select(
                    projects.c.hierarchical_integration_policy,
                    projects.c.hierarchical_integration_generation,
                ).where(projects.c.id == source["project_id"])
            )
        ).mappings().one()
        task_type = task["task_type"]
        result = {
            **base,
            "task_type": task_type,
            "repository_id": source["repository_id"],
            "pr_url": source["pr_url"],
            "base_sha": source["base"],
            "head_sha": source["head"],
            "generation": source["generation"],
            "review_kind": source["review_kind"],
            "policy_generation": int(project["hierarchical_integration_generation"]),
        }
        policy_data = project["hierarchical_integration_policy"]
        policy = HierarchicalIntegrationPolicy.model_validate(policy_data) if policy_data else None
        if policy is None or policy.root.admission != "authorized":
            return {
                **result,
                "outcome": "blocked",
                "reason": (
                    "root admission is not 'authorized'; an explicit authorization "
                    "cannot replace the required human review"
                ),
            }
        holds = sorted(
            (
                await conn.execute(
                    select(task_labels.c.label).where(
                        task_labels.c.task_id == task_id, task_labels.c.label.like("hold:%")
                    )
                )
            ).scalars()
        )
        if holds:
            return {**result, "outcome": "blocked", "reason": "task is held: " + ", ".join(holds)}
        open_gate = (
            await conn.execute(
                select(
                    exists(
                        select(task_gates.c.task_id)
                        .select_from(task_gates.join(gates, gates.c.id == task_gates.c.gate_id))
                        .where(task_gates.c.task_id == task_id, gates.c.status == "open")
                    )
                )
            )
        ).scalar_one()
        if open_gate:
            return {**result, "outcome": "blocked", "reason": "task waits on an open gate"}
        evidence = integration_review_evidence
        latest = (
            await conn.execute(
                select(evidence.c.verdict)
                .where(
                    evidence.c.source_task_id == task_id,
                    evidence.c.repository_id == source["repository_id"],
                    evidence.c.source_base == source["base"],
                    evidence.c.reviewed_head_sha == source["head"],
                    evidence.c.generation == source["generation"],
                )
                .order_by(evidence.c.created_at.desc(), evidence.c.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest == "rejected":
            return {
                **result,
                "outcome": "blocked",
                "reason": "a reviewer rejected this exact head",
            }
        if task_type in POLICY_KINDS:
            return {**result, "outcome": "already_authorized", "authorized_by": "policy_kind"}
        if task_id in policy.root.authorized_task_ids:
            return {
                **result,
                "outcome": "already_authorized",
                "authorized_by": "policy_allowlist",
            }
        existing = await exact_root_authorization_on(conn, task_id, source)
        if existing is not None:
            return {
                **result,
                "outcome": "already_authorized",
                "authorized_by": "grant",
                "authorization_id": existing["id"],
            }
        return {**result, "outcome": "candidate"}

    async def _train_root_state(self, conn, task_id: str, base: dict[str, Any]) -> dict[str, Any]:
        """A root without a verified checkpoint: name the failing condition.

        The integration train collects an epic itself (``EpicCompletions``)
        and never verifies the hierarchy checkpoint this grant is keyed on, so
        a train root has no exact source here. It needs no grant either: the
        train admits any completed root once its PR passes the root gate
        (``RootPullRequestGate``). Say so, with what the train is waiting on,
        instead of ``not_eligible``.
        """
        row = (
            await conn.execute(
                select(
                    tasks.c.parent_task_id, tasks.c.status, tasks.c.task_type,
                    tasks.c.branch_name, tasks.c.pr_url, tasks.c.repo_id,
                    projects.c.hierarchical_integration_mode,
                    projects.c.integration_repository_id,
                    projects.c.hierarchical_integration_policy,
                    projects.c.hierarchical_integration_generation,
                )
                .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
                .where(tasks.c.id == task_id)
            )
        ).mappings().one()
        live_origin = (
            await conn.execute(
                select(
                    exists(
                        select(task_branch_origins.c.task_id).where(
                            task_branch_origins.c.task_id == task_id,
                            task_branch_origins.c.repository_id == row["repo_id"],
                            task_branch_origins.c.retired_at.is_(None),
                        )
                    )
                )
            )
        ).scalar_one()
        mode = row["hierarchical_integration_mode"]
        conditions = (
            (row["parent_task_id"] is None,
             f"not a root: its parent {row['parent_task_id']} carries it"),
            (row["status"] == "COMPLETED", f"the root is {row['status']}, not COMPLETED"),
            (mode == "train", f"project integration mode is '{mode}', not 'train'"),
            (row["repo_id"] == row["integration_repository_id"],
             "the root's repository is not the project's integration repository"),
            (bool((row["branch_name"] or "").strip()), "the root has no recorded branch"),
            (bool((row["pr_url"] or "").strip()),
             "the root has no PR; `aq integration redrive-root` opens it"),
            (live_origin, "the root has no live branch origin"),
        )
        failed = next((reason for held, reason in conditions if not held), None)
        if failed is None and self.train_blockers is None:
            failed = (
                "the root's integration checkpoint is not verified at its head, and "
                "without the integration train only a verified exact source is admitted"
            )
        if failed:
            return {**base, "outcome": "not_eligible", "task_type": row["task_type"],
                    "reason": failed}
        result = {
            **base,
            "task_type": row["task_type"],
            "repository_id": row["repo_id"],
            "pr_url": row["pr_url"],
            "policy_generation": int(row["hierarchical_integration_generation"]),
        }
        policy_data = row["hierarchical_integration_policy"]
        policy = HierarchicalIntegrationPolicy.model_validate(policy_data) if policy_data else None
        blockers = (await self.train_blockers(task_id) or {}).get("blockers", [])
        waiting = "; ".join(
            f"{item['code']}: {item['detail']}" for item in blockers
        ) or "no train blocker is recorded for it"
        if policy is None or policy.root.admission != "authorized":
            return {
                **result,
                "outcome": "blocked",
                "reason": (
                    "root admission is not 'authorized': the train admits this root "
                    f"after an approved review of its PR head; train: {waiting}"
                ),
            }
        # ``authorized_by`` stays unset: the train admits every completed root
        # whatever its kind, so neither the kind nor the allowlist is the cause.
        return {
            **result,
            "outcome": "already_authorized",
            "reason": (
                "the integration train admits this completed root without a "
                f"per-source grant once its PR passes the root gate; train: {waiting}"
            ),
        }
