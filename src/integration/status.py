"""Read-only, snapshot-consistent integration rollout status."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_check_evidence,
    integration_review_evidence,
    projects,
    repos,
    task_branch_origins,
    tasks,
)
from src.git.manager import GitError
from src.integration.delivery_branches import observe_completed_branches
from src.integration.delivery_truth import DeliveryState
from src.integration.models import RepairPolicy, integration_ci_sources
from src.integration.promotion_steps import flow_status
from src.integration.repair import ordinary_repair_id

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
_TRAIN_GIT_BUDGET = 3.0  #: The train read's Git budget; what it has not observed is unknown.


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
        git_first: str = "active", train: Any = None,
        flow_problems: Any = None, cached_only: bool = False,
    ) -> None:
        self.db = db
        self.clock = clock
        # Per project, the layer 1-3 problems the daemon found re-validating
        # a stored promotion flow at start (R17); status re-runs layers 1-2.
        self.flow_problems = flow_problems or {}
        # ``git_first: active`` projects the train's Git, check, review and
        # intent facts. Historical code receipts only retire obsolete PR
        # conflict diagnostics; they never supply Git delivery evidence.
        # ``train`` is the daemon's IntegrationTrain, for its last
        # visit per target; without it only durable facts are reported.
        if git_first != "active":
            raise ValueError("trial mode has been removed; use git_first=active or omit it")
        self.train = train
        self.cached_only = cached_only
        # Git delivery truth (a DeliveryObserver).  The daemon registers one
        # on its database; without it nothing is claimed about delivery.
        self.delivery = (
            delivery
            if delivery is not None
            else getattr(db, "_delivery_observer", None)
        )

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
        """Current train inputs and cached evidence, without remote I/O."""
        return await self.train_status(project_id, observe=False)

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
        """Train targets, batches and Git delivery evidence."""
        return await self.train_status(project_id)

    async def task_blockers(self, task_id: str) -> dict[str, Any] | None:
        """Return train blockers after resolving task/project server-side."""
        return await self.train_task_blockers(task_id)

    async def _paused_project_candidates_on(self, conn, project) -> list[str]:
        from src.integration.train_sources import TRAIN_MODES, _pending_tasks

        if (project is None or project["status"] != "PAUSED"
                or project["hierarchical_integration_mode"] not in TRAIN_MODES
                or not project["integration_repository_id"]):
            return []
        return await _pending_tasks(
            conn, project["id"], project["integration_repository_id"], limit=None,
        )

    async def train_status(self, project_id: str, *, observe: bool = True) -> dict[str, Any] | None:
        """The train's targets and open batches, from Git, checks, reviews and intent.

        ``observe=False`` is the control-only read: the durable batches, the last
        visits and their cached evidence, with no default-branch fetch and no epic
        distance, so ``epics`` is omitted rather than reported empty.
        """
        # Gather Git evidence outside the consistent database snapshot. Control
        # reads use only cached evidence and never fetch a paused project's refs.
        async with self.db._engine.connect() as conn:
            inputs = await self._one(conn, select(projects).where(projects.c.id == project_id))
            candidates = await self._paused_project_candidates_on(conn, inputs)
        delivery = None
        if candidates and self.delivery is not None:
            delivery = await self.delivery.observe(
                candidates, max_age=getattr(self.delivery, "READ_MAX_AGE", 0.0),
                cached_only=self.cached_only or not observe,
            )
        async with self._consistent_snapshot() as conn:
            project = await self._one(conn, select(projects).where(projects.c.id == project_id))
            if project is None:
                return None
            visits = await self._current_train_visits_on(conn, project_id)
            batches = await self._train_batches_on(
                conn, integration_batches.c.project_id == project_id, visits=visits)
            evidence = await self._train_evidence_on(conn, visits)
            from src.operator_decisions import history_on

            decisions = await history_on(conn, project_id)
            promotion_flow = await self._promotion_flow_on(conn, project)
            candidates = await self._paused_project_candidates_on(conn, project)
            verified = await delivery.verified_on(conn, candidates) if delivery else {}
            pending, unknown = [], []
            for task_id in candidates:
                proof = verified.get(task_id)
                if proof is not None and proof.satisfied:
                    continue
                if proof is not None and proof.state is DeliveryState.PENDING:
                    pending.append(task_id)
                else:
                    unknown.append(task_id)
        blockers: list[dict[str, Any]] = []
        if pending or unknown:
            blockers.append(_blocker(
                "project_paused",
                "project paused; the integration train skips this project; "
                f"{len(pending)} completed sources await delivery and "
                f"{len(unknown)} await delivery proof; inspect the pause before resuming",
                project_id, pending=pending, unknown=unknown,
            ))
        for batch in batches:
            blockers.extend(self._train_batch_blockers(batch, visits))
        blockers.extend(self._train_source_blockers(visits))
        from src.integration.stacked_branches import EpicRefresh
        from src.integration.train import TrainTarget
        from src.integration.train_sources import project_snapshot

        epics = []
        repository = await self.db.get_repo(project["integration_repository_id"]) if (
            observe and project["integration_repository_id"]) else None
        if repository is not None:
            root = "refs/heads/" + repository.default_branch.removeprefix("refs/heads/")
            observed, deadline = None, asyncio.get_running_loop().time() + _TRAIN_GIT_BUDGET
            with suppress(TimeoutError):  # reuse a recent fetch; never wait out a slow one
                async with asyncio.timeout_at(deadline):
                    observed = await project_snapshot(self.db, TrainTarget(
                        project_id, repository.id, root), read=True, cached_only=self.cached_only)
            async with self.db._engine.connect() as conn:
                child = tasks.alias("status_epic_child")
                epics_on = (await conn.execute(select(
                    tasks.c.id, tasks.c.status, tasks.c.branch_name,
                ).where(
                    tasks.c.project_id == project_id, tasks.c.repo_id == repository.id,
                    tasks.c.branch_name.is_not(None),
                    select(child.c.id).where(child.c.parent_task_id == tasks.c.id).exists(),
                ).order_by(tasks.c.id))).all()
            retired = getattr(getattr(self.train, "targets", None), "retired_targets", ())
            inspect = EpicRefresh(self.db).inspect
            open_refs = {batch["target_ref"] for batch in batches}
            for task_id, task_status, branch in epics_on:
                ref = "refs/heads/" + branch.removeprefix("refs/heads/")
                absent = (observed is not None and
                          observed.for_target(ref).error == "target_ref_missing")
                if (task_status == "COMPLETED" and ref not in open_refs and
                        (absent or (project_id, repository.id, ref) in retired)):
                    continue
                distance = {"task_id": task_id, "behind": None, "state": "unknown"}
                with suppress(ValueError, GitError, OSError, TimeoutError):
                    async with asyncio.timeout_at(deadline):
                        distance = (await inspect(task_id, snapshot=observed))[3]
                epics.append(distance)
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
            **({"epics": epics} if observe else {}),
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
            visits = await self._current_train_visits_on(conn, row["project_id"])
            carrying = select(integration_batch_members.c.batch_id).where(
                integration_batch_members.c.task_id == task_id
            )
            batches = await self._train_batches_on(
                conn, integration_batches.c.id.in_(carrying), visits=visits)
            ahead = []
            if not batches and row["status"] == "COMPLETED":
                ahead = await self._open_batches_ahead_on(
                    conn, row["project_id"], task_id, visits=visits)
        own = self._train_source_blockers(visits, task_id=task_id)
        blockers: list[dict[str, Any]] = []
        for batch in batches:
            blockers.extend(blocker for blocker in self._train_batch_blockers(batch, visits)
                            if blocker.get("task_id") in (None, task_id))
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
        self, conn: AsyncConnection, project_id: str, task_id: str, *, visits=None,
    ) -> list[dict[str, Any]]:
        """Live batches on *task*'s routed target, when the task's branch is live."""
        from src.integration.delivery_observer import delivery_targets
        from src.integration.release import LIVE_LIFECYCLES

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
        ), visits=visits)

    async def _train_batches_on(
        self, conn: AsyncConnection, where, *, visits=None,
    ) -> list[dict[str, Any]]:
        """Open git batches with their members, intent and open repair tasks."""
        from src.integration.batches import (
            candidate_ref,
            ejection_instruction,
            withheld_release,
            withheld_source_blocker,
        )

        table = integration_batches
        rows = await self._all(
            conn,
            select(table.c.id, table.c.project_id, table.c.repository_id, table.c.target_ref, table.c.intent,
                   table.c.lifecycle, table.c.repair_attempt_count, table.c.created_at,
                   table.c.human_abort_reason,
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
                   integration_batch_members.c.source_sha,
                   withheld_release(integration_batch_members.c.batch_id,
                                    integration_batch_members.c.task_id,
                                    integration_batch_members.c.source_sha).label("released"))
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
            aborted = row["intent"] == "aborted"
            # An aborted batch withholds each exact source until its ejection
            # or an operator release of that source; both stay history.
            row["members"] = [
                {"task_id": m["task_id"], "source_sha": m["source_sha"],
                 "disposition": ("pending" if ejected else "released" if m["released"]
                                 else "withheld") if aborted else row["intent"]}
                for m in members if m["batch_id"] == row["id"]
            ]
            withheld = [m for m in row["members"] if m["disposition"] == "withheld"]
            row["member_disposition"] = (
                ("withheld" if withheld else "pending" if ejected else "released")
                if aborted else row["intent"]
            )
            target = SimpleNamespace(repository_id=row["repository_id"],
                                     target_ref=row["target_ref"])
            source = SimpleNamespace(batch_id=row["id"],
                                     human_abort_reason=row["human_abort_reason"],
                                     repair_attempt_count=row["repair_attempt_count"])
            row["withheld"] = [
                withheld_source_blocker(target, m["task_id"], m["source_sha"], source)
                for m in withheld
            ]
            row["operator_decisions"] = await history_on(
                conn, row["project_id"], await related_on(conn, "batch", row["id"])
            )
            row["candidate_ref"] = candidate_ref(row["id"])
            row["check_reconciliation"] = list((await conn.scalars(
                select(integration_check_evidence.c.check_reconciliation).where(
                    integration_check_evidence.c.check_reconciliation["batch_id"].as_string()
                    == row["id"]
                ).order_by(integration_check_evidence.c.check_name)
            )).all())
            # A retired BLOCKED primary remains history after a successor is
            # allocated; only the batch's current filing can own its candidate.
            row["repairs"] = [
                {"task_id": r["id"], "status": r["status"]}
                for r in repairs if r["created_by_id"] == row["id"]
                and row["repair_attempt_count"] > 0
                and r["id"] == ordinary_repair_id(row["id"], row["repair_attempt_count"])
            ]
            visit = (visits if visits is not None else self._train_visits(row["project_id"])).get(
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

    async def _current_train_visits_on(
        self, conn: AsyncConnection, project_id: str,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Retire PR-conflict diagnostics superseded by an exact code receipt.

        A historical cherry-pick receipt can settle the old PR even though its
        source is not an ancestor of the target. This projection does not change
        the train's last visit or authorize delivery, admission or publication.
        Reopened generations and receipts for other targets keep their blockers.
        """
        from src.integration.root_pull_requests import historically_landed_root_heads_on

        visits = self._train_visits(project_id)
        for key, visit in visits.items():
            blockers = (visit.get("detail") or {}).get("blockers", [])
            ids = {b["task_id"] for b in blockers
                   if b.get("code") == "pr_conflicting" and b.get("task_id")}
            if not ids:
                continue
            settled = await historically_landed_root_heads_on(self.db, conn, project_id, *key, ids)
            current = [b for b in blockers if not (
                b.get("code") == "pr_conflicting"
                and (b.get("task_id"), b.get("source_sha")) in settled
            )]
            if len(current) != len(blockers):
                visits[key] = {**visit, "detail": {**visit["detail"], "blockers": current}}
        return visits

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
            return batch.get("withheld") or [
                _blocker("batch_aborted", "the batch was aborted; it is never rebuilt", ref)]
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
        if detail.get("blocker") == "epic_refresh_checks_failed":
            blockers.append(_blocker(
                detail["blocker"], detail["reason"], ref,
                candidate_sha=visit.get("candidate_sha"), evidence=detail,
                supervisor_action=detail["supervisor_action"],
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
            if current["state"] == "active" and limit and int(current["attempts"]) >= limit:
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
