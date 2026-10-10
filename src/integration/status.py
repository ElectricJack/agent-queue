"""Read-only, snapshot-consistent integration rollout status."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_check_evidence,
    integration_legacy_deliveries,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    integration_subject_journal,
    integration_subjects,
    projects,
    repos,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitError
from src.integration.delivery_branches import observe_completed_branches
from src.integration.delivery_truth import DeliveryState
from src.integration.models import RepairPolicy, integration_ci_sources
from src.integration.promotion_steps import flow_status
from src.integration.records import ParentEpisodeRecords

ACTIVE_BATCH_STATES = (
    "sealing",
    "sealed",
    "building",
    "testing",
    "repairing",
    "human_blocked",
    "promoting",
    "cleanup_pending",
)
TERMINAL_TASK_STATES = ("COMPLETED", "FAILED", "CANCELLED")
# What a train visit's state asks of whoever reads the status.
TRAIN_VISIT_BLOCKERS = {
    "conflict": ("merge_conflict", "the batch does not merge onto its target"),
    
    "no_regenerator": (
        "no_regenerator",
        "no regenerate command is configured, so generated artifacts cannot be rebuilt",
    ),
    "held": ("held", "an explicit hold or a missing review stops publication"),
    "moved": ("target_moved", "the target moved; the next visit rebuilds the candidate"),
    "source_moved": ("source_moved", "a member's source moved since the batch froze"),
    "unknown": ("unobserved", "the last visit could not observe Git or checks"),
}


#: How long one task's explain waits for its completed-branch read.
_COMPLETED_BRANCH_BUDGET_SECONDS = 5.0


def _blocker(
    code: str, detail: str, ref: str | None = None, **facts: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "detail": detail, "ref": ref}
    item.update(facts)
    return item


def _sorted_blockers(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for value in values:
        key = (value["code"], str(value.get("ref") or ""), value["detail"])
        unique[key] = value
    return [unique[key] for key in sorted(unique)]


class IntegrationStatusService:
    """Project/task integration projections with no provider I/O or writes."""

    def __init__(
        self, db, *, clock: Callable[[], float] = time.time, delivery: Any = None,
        git_first: str = "shadow", train: Any = None,
        flow_problems: Any = None, cached_only: bool = False,
    ) -> None:
        self.db = db
        self.clock = clock
        # Per project, the layer 1-3 problems the daemon found re-validating
        # a stored promotion flow at start (R17); status re-runs layers 1-2.
        self.flow_problems = flow_problems or {}
        # ``git_first: active`` projects the train's Git, check, review and
        # intent facts; it never reads subjects, journals, generations or
        # receipts. ``train`` is the daemon's IntegrationTrain, for its last
        # visit per target; without it only durable facts are reported.
        self.git_first = git_first
        self.train = train
        self.cached_only = cached_only
        # Git delivery truth (a DeliveryObserver).  The daemon registers one
        # on its database; without it nothing is claimed about delivery.
        self.delivery = (
            delivery
            if delivery is not None
            else getattr(db, "_delivery_observer", None)
        )

    async def _observe(self, candidates) -> Any:
        """A git view of *candidates*, prepared before any database snapshot.

        Status only reads, so a target that moved since the fetch is still
        reported (the view names the OID it inspected); identities are
        rechecked on the snapshot, and one that changed is reported unknown.
        """
        ids = set(candidates)
        if self.delivery is None or not ids:
            return None
        return await self.delivery.observe(
            ids, max_age=getattr(self.delivery, "READ_MAX_AGE", 0.0),
            **({"cached_only": True} if self.cached_only else {}),
        )

    async def _delivery_candidates(
        self, *, project_id: str | None = None, task_id: str | None = None
    ) -> set[str]:
        """The completed tasks a projection will ask git about (no locks)."""
        from src.integration.delivery_observer import (
            STATUS_DELIVERY_LIMIT,
            delivery_sensitive_ids,
            gating_delivery_ids,
        )

        if self.delivery is None:
            return set()
        async with self.db._engine.connect() as conn:
            if task_id is not None:
                row = (
                    await conn.execute(
                        select(tasks.c.status).where(tasks.c.id == task_id)
                    )
                ).one_or_none()
                if row is None:
                    return set()
                found = await delivery_sensitive_ids(conn, [task_id])
                if row.status in TERMINAL_TASK_STATES:
                    found |= await self._completed_children(conn, [task_id])
                return found
            project = (
                await conn.execute(
                    select(
                        projects.c.hierarchical_integration_mode,
                        projects.c.hierarchical_integration_desired_mode,
                    ).where(projects.c.id == project_id)
                )
            ).one_or_none()
            if project is None:
                return set()
            mode, desired = project
            if mode == "development":
                ids, _total = await gating_delivery_ids(conn, project_id)
                return set(ids)
            if mode == "disabled" and desired == "disabled":
                # Inactive integration reports no train readiness to prove.
                return set()
            parent = tasks.alias("delivery_candidate_parent")
            recorded = (
                select(integration_legacy_deliveries.c.task_id)
                .where(integration_legacy_deliveries.c.task_id == tasks.c.id)
                .exists()
            )
            rows = (
                await conn.execute(
                    select(tasks.c.id)
                    .select_from(
                        tasks.join(parent, parent.c.id == tasks.c.parent_task_id)
                    )
                    .where(
                        tasks.c.project_id == project_id,
                        tasks.c.status == "COMPLETED",
                        parent.c.status.in_(TERMINAL_TASK_STATES),
                        ~recorded,
                    )
                    .order_by(tasks.c.updated_at.desc(), tasks.c.id)
                    .limit(STATUS_DELIVERY_LIMIT)
                )
            ).scalars()
            return set(rows)

    @staticmethod
    async def _completed_children(conn: AsyncConnection, parent_ids) -> set[str]:
        rows = await conn.execute(
            select(tasks.c.id).where(
                tasks.c.parent_task_id.in_(sorted(parent_ids)),
                tasks.c.status == "COMPLETED",
            )
        )
        return set(rows.scalars())

    async def _development_delivery_on(
        self, conn: AsyncConnection, project_id: str, view: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """What the scheduler and settlement wait for, as git answers it now.

        Nothing here is persisted.  Pending work is the publisher's ordinary
        backlog; unknown work (missing ref, wrong repository, git failure,
        or an identity that changed during observation) is a blocker because
        nothing will deliver it on its own.
        """
        from src.integration.delivery_observer import gating_delivery_ids

        ids, total = await gating_delivery_ids(conn, project_id)
        summary: dict[str, Any] = {
            "available": self.delivery is not None,
            "evaluated": 0,
            "total": total,
            "targets": [],
            "pending": [],
            "unknown": [],
        }
        if view is None or not ids:
            return summary, []
        verified = await view.verified_on(conn, ids)
        summary["evaluated"] = len(ids)
        summary["targets"] = sorted(
            (
                {
                    "repository_id": snapshot.repository_id,
                    "target_ref": snapshot.target_ref,
                    "target_oid": snapshot.target_oid,
                    "error": snapshot.error,
                }
                for snapshot in view.snapshots
            ),
            key=lambda item: (item["repository_id"], item["target_ref"]),
        )
        for task_id in ids:
            evidence = verified.get(task_id)
            if evidence is not None and evidence.satisfied:
                continue
            if evidence is not None and evidence.state is DeliveryState.PENDING:
                summary["pending"].append(task_id)
                continue
            summary["unknown"].append(
                {
                    "task_id": task_id,
                    "reason": evidence.reason
                    if evidence is not None
                    else "changed_during_observation",
                }
            )
        blockers = [
            _blocker(
                "delivery_unknown",
                "completed work cannot be proven on the delivery target",
                item["task_id"],
                cause=item["reason"],
            )
            for item in summary["unknown"]
        ]
        return summary, blockers

    @asynccontextmanager
    async def _consistent_snapshot(self) -> AsyncIterator[AsyncConnection]:
        engine = self.db._engine
        if engine is None:
            raise RuntimeError("database is not initialized")
        conn = await engine.connect()
        try:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            transaction = await conn.begin()
            try:
                yield conn
            finally:
                await transaction.rollback()
        finally:
            await conn.close()

    async def control_status(self, project_id: str) -> dict[str, Any] | None:
        """Current project inputs and subject schedules, without remote I/O."""
        if self.git_first == "active":
            return await self.train_status(project_id)
        async with self._consistent_snapshot() as conn:
            return await self._control_status_on(conn, project_id)

    async def _control_status_on(self, conn: AsyncConnection, project_id: str):
        project = await self._one(
            conn, select(projects).where(projects.c.id == project_id)
        )
        if project is None:
            return None
        rows = await self._all(
            conn,
            select(integration_subjects)
            .where(
                integration_subjects.c.project_id == project_id,
                integration_subjects.c.phase != "done",
            )
            .order_by(integration_subjects.c.created_at, integration_subjects.c.id)
            .limit(100),
        )
        from src.operator_decisions import history_on, related_on

        subjects = []
        for row in rows:
            journal = await self._one(
                conn,
                select(integration_subject_journal)
                .where(
                    integration_subject_journal.c.subject_id == row["id"],
                )
                .order_by(integration_subject_journal.c.seq.desc())
                .limit(1),
            )
            refs = set()
            for kind, identity in (("task", row.get("task_id")),
                                   ("batch", row.get("batch_id"))):
                if identity:
                    refs.update(await related_on(conn, kind, identity))
            subjects.append({**row, "last_record": journal,
                             "operator_decisions": await history_on(conn, project_id, refs)})
        return {
            "projection_kind": "subjects",
            "project_id": project_id,
            "effective_mode": project["hierarchical_integration_mode"],
            "desired_mode": project["hierarchical_integration_desired_mode"],
            "generation": project["hierarchical_integration_generation"],
            "repository_id": project["integration_repository_id"],
            "subjects": subjects,
            "operator_decisions": await history_on(conn, project_id),
            "promotion_flow": await self._promotion_flow_on(conn, project),
            "ci_source": integration_ci_sources(project["hierarchical_integration_policy"]),
        }

    async def _promotion_flow_on(self, conn: AsyncConnection, project) -> dict[str, Any] | None:
        """The stored flow as a chain; ``misconfigured`` when it no longer validates."""
        if not project["promotion_flow"]:
            return None
        default_branch = None
        if project["integration_repository_id"] is not None:
            default_branch = await conn.scalar(select(repos.c.default_branch).where(
                repos.c.id == project["integration_repository_id"],
                repos.c.project_id == project["id"]))
        return flow_status(
            project["promotion_flow"],
            default_branch=default_branch or project["repo_default_branch"],
            recorded=self.flow_problems.get(project["id"]),
        )

    async def status(self, project_id: str) -> dict[str, Any] | None:
        """Subject facts and Git delivery evidence, verified on one snapshot."""
        if self.git_first == "active":
            return await self.train_status(project_id)
        view = await self._observe(await self._delivery_candidates(project_id=project_id))
        async with self._consistent_snapshot() as conn:
            projection = await self._control_status_on(conn, project_id)
            if projection is not None and projection["effective_mode"] == "development":
                delivery, blockers = await self._development_delivery_on(conn, project_id, view)
                projection.update(delivery=delivery, blockers=_sorted_blockers(blockers))
            return projection

    async def task_blockers(self, task_id: str) -> dict[str, Any] | None:
        """Return integration blockers after resolving task/project server-side.

        Git delivery evidence for the task and its completed children is
        gathered first, outside the snapshot.
        """
        if self.git_first == "active":
            return await self.train_task_blockers(task_id)
        view = await self._observe(await self._delivery_candidates(task_id=task_id))
        async with self._consistent_snapshot() as conn:
            return await self._task_blockers_on(conn, task_id, delivery=view)

    async def _task_blockers_on(
        self,
        conn: AsyncConnection,
        task_id: str,
        *,
        expected_project_id: str | None = None,
        delivery: Any = None,
    ) -> dict[str, Any] | None:
        row = (
            (
                await conn.execute(
                    select(tasks, projects)
                    .select_from(
                        tasks.join(projects, projects.c.id == tasks.c.project_id)
                    )
                    .where(tasks.c.id == task_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or (
            expected_project_id is not None and row["project_id"] != expected_project_id
        ):
            return None
        blockers: list[dict[str, Any]] = []
        designated = row["integration_repository_id"]
        integration_active = (
            row["hierarchical_integration_mode"] != "disabled"
            or row["hierarchical_integration_desired_mode"] != "disabled"
        )
        # Development publication accepts an unpinned task on the project's
        # designated repository, just as its candidate query does.
        effective_repo = row["repo_id"]
        if (
            effective_repo is None
            and row["hierarchical_integration_mode"] == "development"
        ):
            effective_repo = designated
        if integration_active and (designated is None or effective_repo != designated):
            # Name the task: its repository is usually unset, and a ref of
            # ``None`` collapsed every such task into one anonymous blocker.
            if designated is None:
                cause, detail = (
                    "project_repository_unset",
                    "project has no designated integration repository",
                )
            elif effective_repo is None:
                cause, detail = (
                    "task_repository_unset",
                    (
                        "task has no repository; it is not bound to the project's "
                        "designated integration repository"
                    ),
                )
            else:
                cause, detail = (
                    "task_repository_mismatch",
                    "task repository is not the project's designated integration repository",
                )
            blockers.append(
                _blocker(
                    "repository_not_designated",
                    detail,
                    task_id,
                    cause=cause,
                    repository_id=row["repo_id"],
                    designated_repository_id=designated,
                )
            )
        if delivery is not None and row["status"] == "COMPLETED":
            blockers.extend(await self._own_delivery_blockers(conn, task_id, delivery))
        checkpoint = await self._one(
            conn,
            select(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == task_id
            ),
        )
        if checkpoint is not None:
            if checkpoint["verified_generation"] is not None and (
                checkpoint["verified_generation"] != checkpoint["generation"]
            ):
                blockers.append(
                    _blocker(
                        "stale_generation",
                        "verified generation does not match the current task generation",
                        task_id,
                    )
                )
            if checkpoint["verified_sha"] is not None and (
                checkpoint["verified_sha"] != checkpoint["checkpoint_sha"]
            ):
                blockers.append(
                    _blocker(
                        "stale_head",
                        "verified head does not match the current checkpoint",
                        task_id,
                    )
                )
            review = await self._one(
                conn,
                select(integration_review_evidence)
                .where(integration_review_evidence.c.source_task_id == task_id)
                .order_by(
                    integration_review_evidence.c.created_at.desc(),
                    integration_review_evidence.c.id.desc(),
                ),
            )
            if review is not None and (
                review["generation"] != checkpoint["generation"]
                or review["reviewed_head_sha"] != checkpoint["checkpoint_sha"]
                or review["verdict"] != "approved"
            ):
                blockers.append(
                    _blocker(
                        "stale_review",
                        "review does not bind the current task head",
                        review["id"],
                    )
                )
        owners = (
            await self._all(
                conn,
                select(integration_branch_owners.c.id).where(
                    integration_branch_owners.c.repository_id == designated,
                    integration_branch_owners.c.ref == row["branch_name"],
                    integration_branch_owners.c.handoff_state != "released",
                ),
            )
            if designated and row["branch_name"]
            else []
        )
        if owners:
            blockers.append(
                _blocker(
                    "active_owner", "task branch has an active owner", owners[0]["id"]
                )
            )

        readiness_operation = None
        if checkpoint is not None and checkpoint["episode_id"] is not None:
            readiness_operation = await self._one(
                conn,
                select(integration_repair_operations).where(
                    integration_repair_operations.c.parent_task_id == task_id,
                    integration_repair_operations.c.episode_id
                    == checkpoint["episode_id"],
                ),
            )
        operation_rows = await self._all(
            conn,
            select(integration_repair_operations).where(
                integration_repair_operations.c.parent_task_id == task_id,
                integration_repair_operations.c.state.in_(
                    ("active", "escalated", "human_required")
                ),
            ),
        )
        child_rows = await self._all(
            conn,
            select(tasks.c.id, tasks.c.status).where(tasks.c.parent_task_id == task_id),
        )
        child_status = {child["id"]: child["status"] for child in child_rows}
        parent_readiness = None
        # A terminal parent whose collection was cancelled (switching to
        # development cancels the parent operation and keeps its checkpoint)
        # finished outside the train and will never be collected again.
        collection_abandoned = (
            readiness_operation is not None
            and readiness_operation["state"] == "cancelled"
            and row["status"] in TERMINAL_TASK_STATES
        )
        if (
            checkpoint is not None
            and readiness_operation is not None
            and not collection_abandoned
        ):
            parent_readiness = await ParentEpisodeRecords(self.db).readiness_on(
                conn,
                parent=dict(row),
                project=dict(row),
                checkpoint=checkpoint,
                operation=readiness_operation,
            )
            for item in parent_readiness["blockers"]:
                child_id = item["task_id"]
                cause = item["reason"]
                if child_status.get(child_id) not in TERMINAL_TASK_STATES:
                    blockers.append(
                        _blocker("open_child", "child task is not terminal", child_id)
                    )
                elif cause == "origin_mismatch":
                    blockers.append(
                        _blocker(
                            "repository_not_designated",
                            "child origin does not match the current parent target",
                            child_id,
                            cause=cause,
                        )
                    )
                elif cause == "receipt_chain":
                    blockers.append(
                        _blocker(
                            "stale_head",
                            "delivery receipt chain does not bind the current parent head",
                            child_id,
                            cause=cause,
                        )
                    )
                else:
                    blockers.append(
                        _blocker(
                            "missing_receipt",
                            "child has no applicable current delivery receipt",
                            child_id,
                            cause=cause,
                        )
                    )
        repair = await self._repair_projection(conn, operation_rows)
        for item in repair:
            if item["state"] == "human_required":
                blockers.append(
                    _blocker(
                        "human_hold", "repair requires a human decision", item["id"]
                    )
                )
        blockers.extend(self._repair_blockers(repair, now=self.clock()))
        return {
            "task_id": task_id,
            "project_id": row["project_id"],
            "integration_active": integration_active,
            "checkpoint": checkpoint,
            "parent_readiness": parent_readiness,
            "repair": repair,
            "blockers": _sorted_blockers(blockers),
        }

    @staticmethod
    async def _own_delivery_blockers(
        conn: AsyncConnection, task_id: str, delivery: Any
    ) -> list[dict[str, Any]]:
        """Whether this completed task's current work is on its target, per git.

        The same evaluator and scope that admission, settlement, archive and
        branch cleanup use; a changed identity is unknown, not delivered.
        """
        from src.integration.delivery_observer import delivery_sensitive_ids

        if task_id not in await delivery_sensitive_ids(conn, [task_id]):
            return []
        evidence = (await delivery.verified_on(conn, [task_id])).get(task_id)
        if evidence is not None and evidence.satisfied:
            return []
        if evidence is not None and evidence.state is DeliveryState.PENDING:
            return [
                _blocker(
                    "development_delivery_pending",
                    "completed work is not on the delivery target yet",
                    task_id,
                    source_oid=evidence.source_oid,
                    target_oid=evidence.target_oid,
                )
            ]
        return [
            _blocker(
                "delivery_unknown",
                "completed work cannot be proven on the delivery target",
                task_id,
                cause=evidence.reason
                if evidence is not None
                else "changed_during_observation",
            )
        ]

    # -- git_first: active ---------------------------------------------------

    async def train_status(self, project_id: str) -> dict[str, Any] | None:
        """The train's targets and open batches, from Git, checks, reviews and intent."""
        async with self._consistent_snapshot() as conn:
            project = await self._one(
                conn, select(projects).where(projects.c.id == project_id)
            )
            if project is None:
                return None
            batches = await self._train_batches_on(
                conn, integration_batches.c.project_id == project_id
            )
            visits = self._train_visits(project_id)
            evidence = await self._train_evidence_on(conn, visits)
            from src.operator_decisions import history_on

            decisions = await history_on(conn, project_id)
            promotion_flow = await self._promotion_flow_on(conn, project)
        blockers: list[dict[str, Any]] = []
        for batch in batches:
            blockers.extend(self._train_batch_blockers(batch, visits))
        blockers.extend(self._train_source_blockers(visits))
        from src.integration.stacked_branches import EpicRefresh
        from src.integration.train import TrainTarget
        from src.integration.train_sources import project_snapshot

        epics = []
        repository = await self.db.get_repo(project["integration_repository_id"]) if project[
            "integration_repository_id"] else None
        if repository is not None:
            default = "refs/heads/" + repository.default_branch.removeprefix("refs/heads/")
            observed = await project_snapshot(self.db, TrainTarget(
                project_id, repository.id, default))
            async with self.db._engine.connect() as conn:
                child = tasks.alias("status_epic_child")
                ids = (await conn.execute(select(tasks.c.id).where(
                    tasks.c.project_id == project_id, tasks.c.repo_id == repository.id,
                    tasks.c.branch_name.is_not(None),
                    select(child.c.id).where(child.c.parent_task_id == tasks.c.id).exists(),
                ).order_by(tasks.c.id))).scalars().all()
            for task_id in ids:
                try:
                    if observed is None:
                        raise ValueError("default branch cannot be observed")
                    _, _, _, distance = await EpicRefresh(self.db).inspect(task_id, snapshot=observed)
                    epics.append(distance)
                except (ValueError, GitError, OSError):
                    epics.append({"task_id": task_id, "behind": None, "state": "unknown"})
        return {
            "projection_kind": "train",
            "project_id": project_id,
            "effective_mode": project["hierarchical_integration_mode"],
            "desired_mode": project["hierarchical_integration_desired_mode"],
            # The train replaces the *subject* projection; the project's
            # integration generation is ordinary configuration and is the
            # compare-and-set token `edit_project` requires, so it stays.
            "generation": project["hierarchical_integration_generation"],
            "repository_id": project["integration_repository_id"],
            "operator_decisions": decisions,
            "targets": [{**visit, **evidence.get(key, {})} for key, visit in visits.items()],
            "batches": batches,
            "epics": epics,
            "blockers": _sorted_blockers(blockers),
            "promotion_flow": promotion_flow,
            # Which runner produces each target kind's required checks.
            "ci_source": integration_ci_sources(project["hierarchical_integration_policy"]),
        }

    async def train_task_blockers(self, task_id: str) -> dict[str, Any] | None:
        """Why *task*'s delivery waits, from the open batch that carries it."""
        async with self._consistent_snapshot() as conn:
            row = await self._one(
                conn,
                select(tasks.c.id, tasks.c.project_id, tasks.c.status,
                       projects.c.hierarchical_integration_mode,
                       projects.c.hierarchical_integration_desired_mode)
                .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
                .where(tasks.c.id == task_id),
            )
            if row is None:
                return None
            carrying = select(integration_batch_members.c.batch_id).where(
                integration_batch_members.c.task_id == task_id
            )
            batches = await self._train_batches_on(
                conn, integration_batches.c.id.in_(carrying)
            )
            ahead = []
            if not batches and row["status"] == "COMPLETED":
                ahead = await self._open_batches_ahead_on(conn, row["project_id"], task_id)
        visits = self._train_visits(row["project_id"])
        own = self._train_source_blockers(visits, task_id=task_id)
        blockers: list[dict[str, Any]] = []
        for batch in batches:
            blockers.extend(self._train_batch_blockers(batch, visits))
        for batch in [] if own else ahead:
            # One batch at a time per target: an admissible source the open batch
            # does not carry waits for it to settle, whatever holds that batch. A
            # source the train refused is blocked by its own reason, not the queue.
            blockers.append(_blocker(
                "queued_behind_open_batch",
                f"{batch['target_ref']} is held by open batch {batch['id']} "
                f"({batch['lifecycle']}); {task_id} is not a member and waits for it "
                "to settle",
                batch["id"], task_id=task_id, batch_id=batch["id"],
                lifecycle=batch["lifecycle"],
            ))
            blockers.extend(self._train_batch_blockers(batch, visits))
        blockers.extend(own)
        # Likewise a refused source already says why its branch waits.
        if row["status"] == "COMPLETED" and not (batches or ahead or own):
            blockers.extend(await self._completed_branch_blockers(row["project_id"], task_id))
        return {
            "task_id": task_id,
            "project_id": row["project_id"],
            "projection_kind": "train",
            "integration_active": (
                row["hierarchical_integration_mode"] != "disabled"
                or row["hierarchical_integration_desired_mode"] != "disabled"
            ),
            "batches": batches,
            "blockers": _sorted_blockers(blockers),
        }

    async def _completed_branch_blockers(
        self, project_id: str, task_id: str
    ) -> list[dict[str, Any]]:
        """A COMPLETED task branch holding work nothing will deliver or retire.

        The read ``aq doctor --check integration.completed_undelivered`` makes,
        for one task under a short budget; an unreadable snapshot says nothing.
        """
        if self.delivery is None or getattr(self.delivery, "git", None) is None:
            return []
        try:
            inventory = await asyncio.wait_for(observe_completed_branches(
                self.db, self.delivery, project_ids={project_id}, task_ids={task_id},
                now=self.clock(), cached_only=self.cached_only,
            ), _COMPLETED_BRANCH_BUDGET_SECONDS)
        except (TimeoutError, GitError, OSError):
            return []
        return [
            _blocker(
                "completed_undelivered",
                f"{entry['branch']} holds work the default branch cannot reach: "
                f"{entry['reason']}; {entry['remedy']}",
                entry["branch"], task_id=task_id, rule=entry["rule"], head=entry["head"],
                remedy=entry["remedy"],
            )
            for entry in inventory["entries"]
            if not entry["accounted"] and entry["rule"] != "unknown"
        ]

    async def _open_batches_ahead_on(
        self, conn: AsyncConnection, project_id: str, task_id: str
    ) -> list[dict[str, Any]]:
        """Live batches on *task*'s routed target, when the task's branch is live."""
        from src.integration.delivery_observer import delivery_targets
        from src.integration.stale_schedule import LIVE_LIFECYCLES

        live = (await conn.execute(select(exists(select(task_branch_origins.c.task_id).where(
            task_branch_origins.c.task_id == task_id,
            task_branch_origins.c.retired_at.is_(None),
        ))))).scalar_one()
        routed = (await delivery_targets(conn, [task_id], reduced=True)).get(task_id)
        if not live or routed is None:
            return []
        return await self._train_batches_on(conn, and_(
            integration_batches.c.project_id == project_id,
            integration_batches.c.repository_id == routed.repository_id,
            integration_batches.c.target_ref == routed.target_ref,
            integration_batches.c.lifecycle.in_(LIVE_LIFECYCLES),
        ))

    async def _train_batches_on(self, conn: AsyncConnection, where) -> list[dict[str, Any]]:
        """Open git batches with their members, intent and open repair tasks."""
        from src.integration.batches import candidate_ref, ejection_instruction

        table = integration_batches
        rows = await self._all(
            conn,
            select(table.c.id, table.c.project_id, table.c.repository_id, table.c.target_ref, table.c.intent,
                   table.c.lifecycle, table.c.repair_attempt_count, table.c.created_at,
                   ejection_instruction(table.c.id).label("ejected"))
            .where(where, table.c.target_ref.is_not(None), table.c.lifecycle != "promoted")
            .order_by(table.c.created_at, table.c.id)
            .limit(100),
        )
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        members = await self._all(
            conn,
            select(integration_batch_members.c.batch_id, integration_batch_members.c.task_id,
                   integration_batch_members.c.source_sha)
            .where(integration_batch_members.c.batch_id.in_(ids))
            .order_by(integration_batch_members.c.batch_id,
                      integration_batch_members.c.ordinal),
        )
        repairs = await self._all(
            conn,
            select(tasks.c.id, tasks.c.status, tasks.c.created_by_id)
            .where(
                tasks.c.created_by_kind == "system",
                tasks.c.created_by_id.in_(ids),
                tasks.c.dedup_key.like("repair:%"),
                tasks.c.status.not_in(TERMINAL_TASK_STATES),
            )
            .order_by(tasks.c.id),
        )
        from src.operator_decisions import history_on, related_on

        for row in rows:
            ejected = row.pop("ejected")
            row["member_disposition"] = (
                "pending" if row["intent"] == "aborted" and ejected
                else "withheld" if row["intent"] == "aborted"
                else row["intent"]
            )
            row["operator_decisions"] = await history_on(
                conn, row["project_id"], await related_on(conn, "batch", row["id"])
            )
            row["candidate_ref"] = candidate_ref(row["id"])
            row["members"] = [
                {"task_id": m["task_id"], "source_sha": m["source_sha"]}
                for m in members if m["batch_id"] == row["id"]
            ]
            row["repairs"] = [
                {"task_id": r["id"], "status": r["status"]}
                for r in repairs if r["created_by_id"] == row["id"]
            ]
            visit = self._train_visits(row["project_id"]).get(
                (row["repository_id"], row["target_ref"])
            )
            if visit and visit.get("batch_id") == row["id"]:
                row["detail"] = visit.get("detail")
        return rows

    def _train_visits(self, project_id: str) -> dict[tuple[str, str], dict[str, Any]]:
        """The train's last visit per target of *project_id*, by (repository, ref)."""
        rows = self.train.status() if self.train is not None else []
        return {
            (row["repository_id"], row["target_ref"]): row
            for row in rows if row.get("project_id") == project_id
        }

    async def _train_evidence_on(
        self, conn: AsyncConnection, visits: dict[tuple[str, str], dict[str, Any]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Cached exact-commit checks and tree reviews for each visited candidate."""
        found: dict[str, dict[str, Any]] = {}
        for key, visit in visits.items():
            sha, tree = visit.get("candidate_sha"), visit.get("tree_sha")
            facts: dict[str, Any] = {"check_runs": [], "reviews": []}
            if sha:
                table = integration_check_evidence
                facts["check_runs"] = await self._all(
                    conn,
                    select(table.c.check_name, table.c.producer_id, table.c.conclusion,
                           table.c.classification, table.c.run_url, table.c.observed_at)
                    .where(table.c.repository_id == visit["repository_id"],
                           table.c.sha == sha)
                    .order_by(table.c.check_name, table.c.producer_id),
                )
            if tree:
                table = integration_review_evidence
                facts["reviews"] = await self._all(
                    conn,
                    select(table.c.id, table.c.reviewer_identity, table.c.review_kind,
                           table.c.verdict, table.c.created_at)
                    .where(table.c.repository_id == visit["repository_id"],
                           table.c.reviewed_tree_sha == tree)
                    .order_by(table.c.created_at, table.c.id),
                )
            found[key] = facts
        return found

    @staticmethod
    def _train_source_blockers(
        visits: dict[tuple[str, str], dict[str, Any]], *, task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Unknown completions can block a target before any batch exists."""
        blockers = [
            blocker
            for visit in visits.values()
            for blocker in (visit.get("detail") or {}).get("blockers", [])
            if task_id is None or blocker.get("task_id") == task_id
        ]
        if task_id is None:
            for visit in visits.values():
                detail = visit.get("detail") or {}
                if detail.get("reason") == "visit_timeout":
                    blockers.append(_blocker(
                        "visit_timeout", f"target visit exceeded {detail['timeout_seconds']}s "
                        f"during {detail['stage']}", visit["target_ref"], evidence=detail,
                    ))
        return blockers

    @staticmethod
    def _train_batch_blockers(
        batch: dict[str, Any], visits: dict[tuple[str, str], dict[str, Any]]
    ) -> list[dict[str, Any]]:
        ref = batch["id"]
        holds = [row for row in batch.get("operator_decisions", []) if row["active"]]
        if holds:
            return [_blocker("operator_decision_hold", row["decision"], row["id"])
                    for row in holds]
        if batch["intent"] == "aborted":
            return [_blocker("batch_aborted", "the batch was aborted; it is never rebuilt", ref)]
        if batch["intent"] == "paused":
            return [_blocker("batch_paused", "the batch is paused by operator intent", ref)]
        blockers = [
            _blocker("repair_open", "a repair task owns the candidate", repair["task_id"],
                     batch_id=ref)
            for repair in batch["repairs"]
        ]
        visit = visits.get((batch["repository_id"], batch["target_ref"]))
        if visit is None or visit.get("batch_id") != ref:
            return blockers + [_blocker(
                "awaiting_visit", "the train has not visited this batch since start", ref)]
        if (visit.get("detail") or {}).get("outcome") == "conflict":
            blockers.append(_blocker("merge_conflict", "the batch does not merge onto its target",
                                     ref, evidence=visit["detail"]))
        if (visit.get("repair") or {}).get("outcome") == "unconfirmed":
            blockers.append(_blocker("repair_target_unconfirmed",
                                     "the repair starting head is not confirmed on its remote ref",
                                     ref, evidence=visit["repair"]))
        missing_push = (visit.get("detail") or {}).get("ci_not_triggered")
        if missing_push:
            blockers.append(_blocker(
                "ci_not_triggered", missing_push["reason"], ref,
                candidate_sha=visit.get("candidate_sha"), evidence=missing_push,
            ))
        detail = visit.get("detail") or {}
        if detail.get("blocker") == "candidate_failing_checks_unknown":
            blockers.append(_blocker(
                detail["blocker"], detail["reason"], ref,
                candidate_sha=visit.get("candidate_sha"), evidence=detail,
            ))
        if visit["state"] == "testing":
            code = "checks_red" if visit.get("checks") == "red" else "checks_pending"
            blockers.append(_blocker(
                code, f"required checks are {visit.get('checks') or 'not requested'} "
                "on the exact candidate", ref, candidate_sha=visit.get("candidate_sha")))
        elif visit["state"] == "preexisting":
            detail = visit.get("detail") or {}
            re_request = detail.get("re_request") or {}
            # The bound is spent: a person owns this batch until the target's own
            # required checks pass, the batch is aborted, or the head moves.
            blockers.append(_blocker(
                re_request.get("blocker") or "checks_preexisting",
                "no repair is filed while the candidate's failing checks also fail on the "
                "target or the target has not decided them",
                ref, candidate_sha=visit.get("candidate_sha"),
                evidence=detail.get("baseline")))
        elif visit["state"] in TRAIN_VISIT_BLOCKERS:
            code, detail_text = TRAIN_VISIT_BLOCKERS[visit["state"]]
            blockers.append(_blocker(code, detail_text, ref, evidence=visit.get("detail")))
        return blockers

    @staticmethod
    async def _one(conn: AsyncConnection, statement) -> dict[str, Any] | None:
        row = (await conn.execute(statement.limit(1))).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    async def _all(conn: AsyncConnection, statement) -> list[dict[str, Any]]:
        rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def _repair_projection(
        self, conn: AsyncConnection, operations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        result = []
        for operation in sorted(operations, key=lambda item: item["id"]):
            stage_rows = await self._all(
                conn,
                select(integration_repair_stages)
                .where(integration_repair_stages.c.operation_id == operation["id"])
                .order_by(integration_repair_stages.c.ordinal),
            )
            stages = [
                {
                    "ordinal": stage["ordinal"],
                    "state": stage["state"],
                    "attempts": stage["attempts"],
                    "deadline_at": stage["deadline_at"],
                    "started_at": stage["started_at"],
                    "intelligence_class": stage["intelligence_class"],
                    "profile_id": stage["profile_id"],
                    "policy": stage["policy"],
                }
                for stage in stage_rows
            ]
            result.append(
                {
                    "id": operation["id"],
                    "target_kind": operation["target_kind"],
                    "batch_id": operation["batch_id"],
                    "parent_task_id": operation["parent_task_id"],
                    "active_stage": operation["active_stage"],
                    "state": operation["state"],
                    "stages": stages,
                }
            )
        return result

    @staticmethod
    def _batch_projection(
        batch: dict[str, Any] | None, revision: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if batch is None:
            return None
        return {
            "id": batch["id"],
            "repository_id": batch["repository_id"],
            "request_id": batch["request_id"],
            "trigger": batch["trigger"],
            "state": batch["lifecycle"],
            "revision": batch["current_revision"],
            "base_sha": batch["base_sha"],
            "integration_branch": batch["integration_branch"],
            "tested_candidate_sha": batch["tested_candidate_sha"],
            "ci_evidence_id": batch["ci_evidence_id"],
            "final_main_sha": batch["final_main_sha"],
            "cleanup_state": batch["cleanup_state"],
            "revision_state": revision["state"] if revision else None,
            "candidate_sha": revision["head_sha"] if revision else None,
        }

    @staticmethod
    def _project_blockers(
        project,
        batch: dict[str, Any] | None,
        revision: dict[str, Any] | None,
        repair: list[dict[str, Any]],
        cleanup: list[dict[str, Any]],
        *,
        now: float,
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if batch is not None and (
            batch["lifecycle"] == "testing"
            or revision is None
            or revision["state"] not in {"green", "promoted"}
        ):
            blockers.append(
                _blocker(
                    "pending_ci", "active candidate is not proven green", batch["id"]
                )
            )
        for operation in repair:
            if operation["state"] == "human_required" or (
                batch is not None and batch["lifecycle"] == "human_blocked"
            ):
                blockers.append(
                    _blocker(
                        "human_hold",
                        "integration repair awaits a human",
                        operation["id"],
                    )
                )
        blockers.extend(IntegrationStatusService._repair_blockers(repair, now=now))
        conflict = next((item for item in cleanup if item["state"] == "conflict"), None)
        if conflict is not None:
            blockers.append(
                _blocker(
                    "cleanup_conflict",
                    "cleanup requires operator reconciliation",
                    conflict["identity"],
                )
            )
        return blockers

    @staticmethod
    def _repair_blockers(
        repair: list[dict[str, Any]], *, now: float
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for operation in repair:
            current = next(
                (
                    stage
                    for stage in operation["stages"]
                    if int(stage["ordinal"]) == int(operation["active_stage"])
                ),
                None,
            )
            if current is None or current["state"] not in {
                "active",
                "awaiting_completion",
            }:
                continue
            policy = RepairPolicy.model_validate(current["policy"])
            ordinal = int(current["ordinal"])
            limit = policy.primary_attempts if ordinal == 0 else policy.debug_attempts
            if current["state"] == "active" and int(current["attempts"]) >= limit:
                blockers.append(
                    _blocker(
                        "budget_exhausted",
                        "current repair stage has exhausted its attempt budget",
                        operation["id"],
                        cause="attempts",
                        stage=ordinal,
                        attempts=int(current["attempts"]),
                        limit=limit,
                    )
                )
            deadline_at = current["deadline_at"]
            deadline_bound = (
                current["state"] == "active" or operation["target_kind"] == "parent"
            )
            if deadline_bound and deadline_at is not None and now >= float(deadline_at):
                blockers.append(
                    _blocker(
                        "budget_exhausted",
                        "current repair stage deadline is exhausted",
                        operation["id"],
                        cause="deadline",
                        stage=ordinal,
                        deadline_at=float(deadline_at),
                    )
                )
        return blockers


__all__ = ["IntegrationStatusService"]
