"""Reopen a parent collection that ``cancel-preserving`` stopped mid-episode.

Historical cancellation ended a repair operation. When the
operation is a parent's *collection* operation -- the one
``ParentEpisodeRecords.reserve_episode_on`` created for the parent's current
episode -- nothing collects that parent any more.  Its checkpoint still says
``awaiting_children`` in the original episode, its completed children's
receipts are bound to ``(operation, episode)``, the collector's reservation is
released, and every path that could continue refuses (2026-10-02,
calm-grove-25 and azure-vault-92):

* the collector and ``aq integration redrive-child`` need an ``active`` or
  ``escalated`` operation for the episode;
* ``ParentEpisodeRecords.reserve_episode_on`` returns the episode's existing
  (cancelled) operation, and one operation per episode is a unique index;
* a later child's conflict cannot open a repair, because
  ``RepairService.start`` continues only a live stage whose delegate is still
  inside its deadline.

A replacement episode cannot inherit the receipts either: accepting a receipt
into a new episode requires a verified previous aggregate.  So
:class:`CancelledCollectionRecovery` reactivates the operation in place, in its
original episode, which leaves every receipt valid as recorded.

It is dry-run first and refuses anything it cannot prove quiet: an unresolved
promotion, resolution, ref mutation or attestation (an ambiguous Git push is
never replayed), a branch owner that is not a released or detached
reservation, a stage delegate that is unsettled, attached or locks a
workspace, a verifier, an operator hold, more than one conflict, a conflict
for a child that has moved on, and a parent branch whose remote tip is not the
recorded head.  Applying needs the remote head the dry run reported and a
reason, and within one transaction under the project lock:

* reclaims the collector reservation for the same operation at the next fence
  token, so nothing still carrying an old token (a queued delivery) can write;
* returns the operation to ``active`` (stage 0) or ``escalated``;
* when exactly one current conflict exists, opens a fresh repair stage bound to
  it, with no writer, a new deadline and its own attempt budget.  Ordinary
  dispatch then files a *new* delegate ``repair-<operation>-<stage>``: archived
  delegates are never restored and their cancelled stages stay cancelled;
* returns a parent the exhausted repair terminally BLOCKED to PAUSED, exactly as
  the shared parent transition requires.

With no conflict waiting, the operation resumes with its cancelled stage still
active; ``RepairService.start`` gives the next conflict a fresh stage past it.
The durable Subject retries a reopened stage whose dispatch failed, whatever
the frozen policy's ``on_exhausted``.

Gates, manual holds and the root's human review are never touched; open human
gates on the parent are reported.  Every apply writes an
``integration.collection_reopened`` event.

An escalated, terminal no-progress incident can use the same control for a
later conflict that no old stage owns. This additional path requires the
exact detached collector fence, preserves every old stage, and rechecks Git
under the apply lock. It never retries an exhausted conflict with a new budget.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import insert, select, update

from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.database.tables import (
    archived_tasks,
    events,
    gates,
    integration_branch_owners,
    integration_candidate_ref_mutations,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    projects,
    repos,
    sessions,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.git.manager import RemoteRefState
from src.integration.owner_guards import parent_engine_guard
from src.integration.models import BranchKey, Fence
from src.integration.promotion_contracts import PromotionError
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: Event written once per applied reopen.
REOPEN_EVENT = "integration.collection_reopened"

#: What settles a stage delegate the cancellation left unsettled.
SETTLE_DELEGATES_COMMAND = "automatic delegate cleanup"

MANAGED_MODES = ("hierarchy", "train")
_TERMINAL_STAGE_STATES = ("passed", "failed", "expired", "cancelled")
_SETTLED_DELEGATE_STATUSES = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
_SETTLED_INTENT_STATES = ("committed", "conflict", "superseded")
_LIVE_OPERATION_STATES = ("active", "escalated", "human_required")
_REPAIR_EXHAUSTED = "integration_repair_exhausted"

Dispatch = Callable[[str, int], Awaitable[dict[str, Any]]]


class _ProofFailed(Exception):
    """Git does not show the recorded collection head on the parent branch."""

    def __init__(self, reason: str, remote_head: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.remote_head = remote_head


class _Changed(Exception):
    """The locked re-check found state the dry run did not report."""


class CancelledCollectionRecovery:
    """Reactivate cancelled collection or dispatch its later, stranded conflict."""

    def __init__(
        self,
        db: Any,
        promotion_service: Any,
        *,
        dispatch: Dispatch | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.promotion = promotion_service
        self.dispatch = dispatch
        self.clock = clock

    async def diagnose(self, task_id: str) -> dict[str, Any]:
        """Report whether the parent's collection can be reopened; writes nothing."""
        diagnosis, _facts = await self._diagnose(task_id)
        return diagnosis

    async def _prepare_apply(self, task_id, facts):
        """Allow a specialized recovery to prove an external handoff before its CAS."""
        return None, facts

    @parent_engine_guard()
    async def run(
        self,
        task_id: str,
        *,
        dry_run: bool = True,
        expected_head_sha: str | None = None,
        reason: str | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        diagnosis, facts = await self._diagnose(task_id)
        if dry_run or diagnosis["outcome"] != "would_reopen":
            return diagnosis
        if not reason or not reason.strip() or expected_head_sha != diagnosis["head_sha"]:
            return {
                **diagnosis,
                "outcome": "changed",
                "reason": "supply the head the dry run reported and an audit reason",
            }
        refusal, facts = await self._prepare_apply(task_id, facts)
        if refusal is not None:
            return {**diagnosis, **refusal}
        now = self.clock()
        transition = None
        try:
            async with self.db.immediate() as conn:
                await self.db.lock_hierarchy_project(conn, facts["project_id"])
                refusal, current = await self._facts_on(conn, task_id, lock=True)
                if refusal is not None or current["fingerprint"] != facts["fingerprint"]:
                    raise _Changed()
                if current.get("no_progress"):
                    try:
                        await self._prove(current)
                    except _ProofFailed as exc:
                        raise _Changed() from exc
                transition, applied = await self._apply_on(
                    conn, current, now=now, head_sha=expected_head_sha,
                    reason=reason, operator_id=operator_id,
                )
        except _Changed:
            return {
                **diagnosis,
                "outcome": "changed",
                "reason": "collection state changed; repeat the dry run",
            }
        if transition is not None:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        dispatched = None
        if applied["stage"] is not None and self.dispatch is not None:
            # The stage is durable; a failed dispatch is retried by the
            # orchestrator's continuation sweep, so report it and move on.
            try:
                dispatched = await self.dispatch(facts["operation"]["id"], applied["stage"])
            except Exception as exc:
                logger.warning(
                    "Reopened collection %s could not dispatch stage %s now; the "
                    "continuation sweep will",
                    task_id,
                    applied["stage"],
                    exc_info=True,
                )
                dispatched = {"outcome": "runtime_error", "error": str(exc)}
        return {
            **diagnosis,
            "outcome": "reopened",
            "collector_fence_token": applied["fence_token"],
            "stage": diagnosis["stage"],
            "dispatch": (
                {
                    key: dispatched.get(key)
                    for key in ("outcome", "repair_task_id", "stage", "error")
                    if dispatched.get(key) is not None
                }
                if dispatched is not None
                else None
            ),
            "reason": (
                "reopened collection; a fresh repair stage owns the current conflict"
                if applied["stage"] is not None
                else "reopened collection under the existing episode"
            ),
        }

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------

    async def _diagnose(self, task_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        async with self.db._engine.connect() as conn:
            refusal, facts = await self._facts_on(conn, task_id)
        if refusal is not None:
            return refusal, facts
        try:
            remote_head = await self._prove(facts)
        except _ProofFailed as exc:
            return {
                **facts["report"],
                "outcome": "blocked",
                "remote_head_sha": exc.remote_head,
                "reason": exc.reason,
            }, facts
        return {
            **facts["report"],
            "outcome": "would_reopen",
            "head_sha": remote_head,
            "remote_head_sha": remote_head,
            "reason": (
                "the collection can resume in its episode; a fresh repair stage "
                "takes the current conflict"
                if facts["conflict"] is not None
                else "the collection can resume in its episode"
            ),
        }, facts

    async def _facts_on(
        self, conn: Any, task_id: str, *, lock: bool = False
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Every durable fact the reopen depends on, or the refusal it implies."""

        def guarded(statement):
            return statement.with_for_update() if lock else statement

        parent = (
            await conn.execute(guarded(select(tasks).where(tasks.c.id == task_id)))
        ).mappings().one_or_none()
        if parent is None:
            return {"outcome": "not_found", "task_id": task_id}, None
        report: dict[str, Any] = {
            "task_id": task_id,
            "project_id": parent["project_id"],
            "branch": parent["branch_name"],
            "kind": "collection_reopen",
        }

        def refused(outcome: str, reason: str, **extra: Any):
            return {**report, **extra, "outcome": outcome, "reason": reason}, None

        project = (
            await conn.execute(select(projects).where(projects.c.id == parent["project_id"]))
        ).mappings().one()
        if project["hierarchical_integration_mode"] not in MANAGED_MODES:
            return refused("not_eligible", "the project is not in hierarchy or train mode")
        if project["hierarchical_integration_draining"]:
            return refused("blocked", "the project is draining its hierarchical integration")
        checkpoint = (
            await conn.execute(
                guarded(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == task_id
                    )
                )
            )
        ).mappings().one_or_none()
        if (
            checkpoint is None
            or checkpoint["state"] != "awaiting_children"
            or not checkpoint["episode_id"]
        ):
            return refused(
                "not_eligible", "the parent checkpoint is not awaiting children in an episode"
            )
        report["checkpoint"] = {
            key: checkpoint[key]
            for key in ("state", "generation", "episode_id", "checkpoint_sha")
        }
        branch = parent["branch_name"]
        repo = (
            await conn.execute(
                select(repos).where(
                    repos.c.id == parent["repo_id"], repos.c.project_id == parent["project_id"]
                )
            )
        ).mappings().one_or_none()
        if (
            not branch
            or repo is None
            or repo["id"] != project["integration_repository_id"]
            or checkpoint["repository_id"] != repo["id"]
            or checkpoint["branch"] != branch
            or branch.removeprefix("refs/heads/") == repo["default_branch"]
        ):
            return refused(
                "blocked", "the parent's canonical integration branch identity is inconsistent"
            )
        episode = (
            await conn.execute(
                select(integration_parent_episodes).where(
                    integration_parent_episodes.c.id == checkpoint["episode_id"],
                    integration_parent_episodes.c.parent_task_id == task_id,
                    integration_parent_episodes.c.repository_id == parent["repo_id"],
                )
            )
        ).mappings().one_or_none()
        operation = (
            await conn.execute(
                guarded(
                    select(integration_repair_operations).where(
                        integration_repair_operations.c.parent_task_id == task_id,
                        integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                    )
                )
            )
        ).mappings().one_or_none()
        if (
            episode is None
            or operation is None
            or operation["target_kind"] != "parent"
            or int(checkpoint["generation"]) < int(episode["generation"])
        ):
            return refused("blocked", "the parent has no matching collection episode and operation")
        operation = dict(operation)
        report.update(operation_id=operation["id"], episode_id=episode["id"])
        no_progress = operation["state"] == "escalated"
        if operation["state"] == "active":
            return refused(
                "nothing_to_reopen",
                f"the collection operation is {operation['state']}; "
                "`aq integration redrive-root` and `redrive-child` handle a live collection",
            )
        if operation["state"] == "human_required":
            return refused(
                "not_eligible",
                "the operation awaits a human decision; inspect the owning Subject and its gate",
            )
        if operation["state"] != "cancelled" and not no_progress:
            return refused("not_eligible", f"the collection operation is {operation['state']}")
        other = (
            await conn.execute(
                select(integration_repair_operations.c.id).where(
                    integration_repair_operations.c.parent_task_id == task_id,
                    integration_repair_operations.c.id != operation["id"],
                    integration_repair_operations.c.state.in_(_LIVE_OPERATION_STATES),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if other is not None:
            return refused("blocked", f"operation {other} is already live for the parent")
        if operation["verifier_task_id"]:
            return refused(
                "blocked",
                f"the operation bound verifier {operation['verifier_task_id']}; reopening "
                "would re-arm a settled verifier",
            )

        # The parent: an unassigned collector, under no human hold.
        holds = dict(
            (
                await conn.execute(
                    select(task_metadata.c.key, task_metadata.c.value).where(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key.in_(("manual_pause", TERMINAL_BLOCKED_META_KEY)),
                    )
                )
            ).all()
        )
        if "manual_pause" in holds:
            return refused(
                "blocked", "an operator hold (manual_pause) is its own decision; release it first"
            )
        terminal = holds.get(TERMINAL_BLOCKED_META_KEY)
        try:
            terminal = json.loads(terminal) if terminal is not None else None
        except (TypeError, ValueError):
            pass  # Older metadata may store the context without JSON encoding.
        if parent["assigned_agent_id"] is not None:
            return refused("blocked", "the parent is assigned to an agent")
        if parent["status"] == TaskStatus.PAUSED.value:
            restore_parent = False
        elif parent["status"] == TaskStatus.BLOCKED.value and terminal == _REPAIR_EXHAUSTED:
            restore_parent = True
        else:
            return refused(
                "blocked", f"the parent is {parent['status']}, not a PAUSED collector"
            )
        if await self._live_holder_on(conn, task_id):
            return refused("blocked", "a session or workspace still holds the parent")

        # Stages and their delegates: all ended, none able to write.
        stages = [
            dict(row)
            for row in (
                await conn.execute(
                    guarded(
                        select(integration_repair_stages)
                        .where(integration_repair_stages.c.operation_id == operation["id"])
                        .order_by(integration_repair_stages.c.ordinal)
                    )
                )
            ).mappings()
        ]
        if no_progress:
            active = next(
                (s for s in stages if s["ordinal"] == operation["active_stage"]), None
            )
            incident = (active["dossier"] or {}).get("supervisor_recovery") if active else None
            if (
                active is None
                or active["state"] not in {"failed", "expired"}
                or not isinstance(incident, dict)
                or incident.get("incident_id")
                != f"repair-no-progress:{operation['id']}:{active['ordinal']}"
                or any(
                    incident.get(key) != active[key]
                    for key in ("attempts", "deadline_at", "repair_task_id")
                )
                or incident.get("stage") != active["ordinal"]
                or incident.get("subject") != active["current_subject"]
            ):
                return refused(
                    "nothing_to_reopen", "the escalated stage has no exact no-progress incident"
                )
        for stage in stages:
            if stage["state"] not in _TERMINAL_STAGE_STATES:
                return refused(
                    "blocked", f"repair stage {stage['ordinal']} is still {stage['state']}"
                )
        delegates = []
        for delegate_id in dict.fromkeys(
            stage["repair_task_id"] for stage in stages if stage["repair_task_id"]
        ):
            live = (
                await conn.execute(guarded(select(tasks).where(tasks.c.id == delegate_id)))
            ).mappings().one_or_none()
            if live is not None and (
                live["status"] not in _SETTLED_DELEGATE_STATUSES
                or live["assigned_agent_id"] is not None
            ):
                return refused(
                    "blocked",
                    f"repair delegate {delegate_id} is {live['status']}; settle it "
                    f"(`{SETTLE_DELEGATES_COMMAND}`) before reopening",
                )
            if await self._live_holder_on(conn, delegate_id):
                return refused(
                    "blocked",
                    f"repair delegate {delegate_id} still has a session or workspace",
                )
            if live is not None:
                delegates.append(
                    {"task_id": delegate_id, "status": live["status"], "archived": False}
                )
                continue
            archived = (
                await conn.execute(
                    select(archived_tasks.c.status).where(archived_tasks.c.id == delegate_id)
                )
            ).scalar_one_or_none()
            delegates.append({"task_id": delegate_id, "status": archived, "archived": True})
        report["delegates"] = delegates

        # The branch owner: provably unable to write.
        owner = (
            await conn.execute(
                guarded(
                    select(integration_branch_owners).where(
                        integration_branch_owners.c.repository_id == parent["repo_id"],
                        integration_branch_owners.c.ref == branch,
                    )
                )
            )
        ).mappings().one_or_none()
        if owner is None:
            return refused("blocked", "the parent branch has no ownership record")
        owner = dict(owner)
        report["owner"] = {
            key: owner[key]
            for key in ("owner_id", "owner_role", "fence_token", "handoff_state")
        }
        detached = owner["session_id"] is None and owner["workspace_id"] is None
        if not (
            owner["handoff_state"] == "released"
            or (
                owner["handoff_state"] == "reserved"
                and owner["owner_id"] == operation["id"]
                and owner["owner_role"] == "collector"
                and detached
            )
        ):
            return refused(
                "blocked",
                f"{owner['owner_role']} {owner['owner_id']} holds the branch "
                f"({owner['handoff_state']}, fence {owner['fence_token']}); recover that "
                "writer first",
            )
        if no_progress and (
            owner["handoff_state"] != "reserved"
            or owner["owner_id"] != operation["id"]
            or owner["owner_role"] != "collector"
        ):
            return refused("blocked", "the no-progress conflict needs its detached collector")
        workspace = None
        if no_progress and owner["confirmed_workspace_id"]:
            workspace = (
                await conn.execute(guarded(select(workspaces).where(
                    workspaces.c.id == owner["confirmed_workspace_id"],
                    workspaces.c.project_id == parent["project_id"],
                )))
            ).mappings().one_or_none()
            if workspace is None:
                return refused("blocked", "confirmed parent workspace is unavailable")

        # No external mutation whose outcome is unknown.
        from src.integration.writers import OperationSafety

        blockers = await OperationSafety._ambiguous_writes_on(
            conn, operation, allowed_writer_id=owner["id"] if no_progress else None
        )
        target_promotion = (
            await conn.execute(
                select(integration_promotion_intents.c.id).where(
                    integration_promotion_intents.c.repository_id == parent["repo_id"],
                    integration_promotion_intents.c.target_branch == branch,
                    integration_promotion_intents.c.state.not_in(_SETTLED_INTENT_STATES),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if target_promotion is not None and "promotion" not in blockers:
            blockers.append("promotion")
        target_mutation = (
            await conn.execute(
                select(integration_candidate_ref_mutations.c.id).where(
                    integration_candidate_ref_mutations.c.repository_id == parent["repo_id"],
                    integration_candidate_ref_mutations.c.branch == branch,
                    integration_candidate_ref_mutations.c.state == "reserved",
                ).limit(1)
            )
        ).scalar_one_or_none()
        if target_mutation is not None and "ref_mutation" not in blockers:
            blockers.append("ref_mutation")
        if blockers:
            return refused(
                "ambiguous",
                "an external write's outcome is unknown; it is never replayed",
                blockers=[
                    {
                        "code": "ambiguous_external_write",
                        "detail": "operation has unresolved external mutation evidence",
                        "ref": blocker,
                    }
                    for blocker in sorted(blockers)
                ],
            )

        receipts = [
            dict(row)
            for row in (
                await conn.execute(
                    select(
                        task_delivery_receipts.c.id,
                        task_delivery_receipts.c.source_task_id,
                        task_delivery_receipts.c.reviewed_head_sha,
                        task_delivery_receipts.c.after_sha,
                        task_delivery_receipts.c.created_at,
                    )
                    .where(
                        task_delivery_receipts.c.parent_operation_id == operation["id"],
                        task_delivery_receipts.c.parent_episode_id == episode["id"],
                    )
                    .order_by(
                        task_delivery_receipts.c.created_at, task_delivery_receipts.c.id
                    )
                )
            ).mappings()
        ]
        report["receipts"] = [
            {key: row[key] for key in ("id", "source_task_id", "reviewed_head_sha", "after_sha")}
            for row in receipts
        ]
        conflicts = [
            dict(row)
            for row in (
                await conn.execute(
                    guarded(
                        select(integration_promotion_intents)
                        .where(
                            integration_promotion_intents.c.repository_id == parent["repo_id"],
                            integration_promotion_intents.c.target_branch == branch,
                            integration_promotion_intents.c.state == "conflict",
                        )
                        .order_by(integration_promotion_intents.c.id)
                    )
                )
            ).mappings()
        ]
        if len(conflicts) > 1:
            return refused(
                "blocked",
                "more than one conflict waits on the parent branch: "
                + ", ".join(row["id"] for row in conflicts),
            )
        conflict = conflicts[0] if conflicts else None
        if no_progress and (
            conflict is None
            or conflict["fence_token"] != owner["fence_token"]
            or any(
                stage["trigger_id"] == conflict["id"]
                or ((stage["dossier"] or {}).get("current_conflict") or {}).get("intent_id")
                == conflict["id"]
                for stage in stages
            )
        ):
            return refused(
                "blocked", "no later conflict at the collector fence; an exhausted conflict "
                "cannot receive another stage",
            )
        stage_plan = None
        next_ordinal = max((int(stage["ordinal"]) for stage in stages), default=-1) + 1
        if conflict is not None:
            report["conflict"] = {
                "intent_id": conflict["id"],
                "source_task_id": conflict["source_task_id"],
                "source_head": conflict["source_head"],
                "expected_target": conflict["expected_target"],
                "fence_token": conflict["fence_token"],
            }
            stale = await self._stale_conflict_on(conn, conflict, operation, parent)
            if stale is not None:
                return refused("blocked", stale)
            from src.integration.repair import RepairService

            stage_plan = await RepairService(
                self.db, clock=self.clock
            ).fresh_parent_stage_plan_on(conn, operation, conflict, next_ordinal)
            if isinstance(stage_plan, str):
                return refused("blocked", stage_plan)
            delegate_id = f"repair-{operation['id']}-{next_ordinal}"
            for table in (tasks, archived_tasks):
                taken = (
                    await conn.execute(select(table.c.id).where(table.c.id == delegate_id))
                ).scalar_one_or_none()
                if taken is not None:
                    return refused(
                        "blocked", f"the next delegate id {delegate_id} is already taken"
                    )
            report["stage"] = {
                key: stage_plan[key]
                for key in ("ordinal", "intelligence_class", "timeout_seconds", "attempt_limit")
            }
            expected_tip = conflict["expected_target"]
        else:
            active = next(
                (s for s in stages if int(s["ordinal"]) == int(operation["active_stage"])),
                None,
            )
            if stages and (active is None or active["state"] != "cancelled"):
                # A reopened collection's next conflict opens a fresh stage
                # only past a cancelled one (``RepairService.start``).
                return refused(
                    "blocked",
                    f"active repair stage {operation['active_stage']} is "
                    f"{active['state'] if active else 'missing'}, not cancelled",
                )
            report["stage"] = None
            delivered = [row["after_sha"] for row in receipts if row["after_sha"]]
            expected_tip = (
                delivered[-1] if delivered else episode["pre_collection_checkpoint_sha"]
            )
        report["human_gates"] = list(
            (
                await conn.execute(
                    select(gates.c.id)
                    .join(task_gates, task_gates.c.gate_id == gates.c.id)
                    .where(task_gates.c.task_id == task_id, gates.c.status == "open")
                    .order_by(gates.c.id)
                )
            ).scalars()
        )
        report["recorded_head_sha"] = expected_tip
        if no_progress and report["human_gates"]:
            return refused("blocked", "open human gates must be resolved before conflict recovery")
        facts = {
            "report": report,
            "project_id": parent["project_id"],
            "parent": dict(parent),
            "restore_parent": restore_parent,
            "operation": operation,
            "episode": dict(episode),
            "stages": stages,
            "owner": owner,
            "conflict": conflict,
            "stage_plan": stage_plan,
            "receipts": receipts,
            "expected_tip": expected_tip,
            "target": BranchKey(repository_id=parent["repo_id"], branch=branch),
            "checkpoint": dict(checkpoint),
            "workspace": dict(workspace) if workspace else None,
            "no_progress": no_progress,
        }
        facts["fingerprint"] = json.dumps(
            {
                "parent": [parent["status"], parent["assigned_agent_id"], parent["repo_id"],
                           branch],
                "holds": sorted(holds),
                "checkpoint": [checkpoint[key] for key in (
                    "state", "episode_id", "generation", "checkpoint_sha", "version")],
                "operation": [operation[key] for key in ("state", "active_stage", "updated_at")],
                "stages": [[s["ordinal"], s["state"], s["repair_task_id"]] for s in stages],
                "delegates": delegates,
                "owner": [owner[key] for key in (
                    "id", "owner_id", "owner_role", "fence_token", "handoff_state",
                    "session_id", "workspace_id")],
                "receipts": [row["id"] for row in receipts],
                "conflict": (
                    [conflict[key] for key in (
                        "id", "state", "source_head", "expected_target", "updated_at")]
                    if conflict is not None
                    else None
                ),
                "expected_tip": expected_tip,
                "workspace": facts["workspace"],
                "recovery_incident": incident if no_progress else None,
            },
            sort_keys=True,
            default=str,
        )
        return None, facts

    @staticmethod
    async def _live_holder_on(conn: Any, task_id: str) -> bool:
        session = (
            await conn.execute(
                select(sessions.c.id).where(
                    sessions.c.task_id == task_id,
                    (sessions.c.state != "stopped") | sessions.c.claim_phase.is_not(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        workspace = (
            await conn.execute(
                select(workspaces.c.id).where(workspaces.c.locked_by_task_id == task_id).limit(1)
            )
        ).scalar_one_or_none()
        return session is not None or workspace is not None

    @staticmethod
    async def _stale_conflict_on(
        conn: Any, conflict: dict[str, Any], operation: dict[str, Any], parent: Any
    ) -> str | None:
        """Why the conflict is no longer the work a fresh stage should repair."""
        if (
            conflict["operation_key"] != operation["id"]
            or conflict["fence_owner_id"] != operation["id"]
            or conflict["target_task_id"] != parent["id"]
            or conflict["project_id"] != parent["project_id"]
        ):
            return f"conflict {conflict['id']} belongs to another collection"
        source = (
            await conn.execute(
                select(
                    tasks.c.parent_task_id, tasks.c.status, tasks.c.project_id, tasks.c.repo_id
                ).where(
                    tasks.c.id == conflict["source_task_id"]
                )
            )
        ).mappings().one_or_none()
        head = (
            await conn.execute(
                select(task_integration_checkpoints.c.checkpoint_sha).where(
                    task_integration_checkpoints.c.task_id == conflict["source_task_id"]
                )
            )
        ).scalar_one_or_none()
        if (
            source is None
            or source["parent_task_id"] != parent["id"]
            or source["project_id"] != parent["project_id"]
            or source["repo_id"] != parent["repo_id"]
            or source["status"] != TaskStatus.COMPLETED.value
            or head != conflict["source_head"]
        ):
            return (
                f"conflict {conflict['id']} no longer names {conflict['source_task_id']}'s "
                "completed head"
            )
        return None

    async def _prove(self, facts: dict[str, Any]) -> str:
        """Return the parent's remote tip once Git shows it is the recorded head."""
        target = facts["target"]
        expected = facts["expected_tip"]
        promotion = self.promotion
        try:
            resolved = await promotion._resolve_repository(target.repository_id)
            if resolved.repo.project_id != facts["project_id"]:
                raise _ProofFailed("the parent's repository belongs to another project")
            await promotion._ensure_retained_repository(resolved)
            store = resolved.retained_git_dir
            async with promotion.git.arepository_transaction(str(store)):
                await promotion._fetch_all_heads(store, resolved.origin_url)
                remote = await promotion.git.als_remote_ref(str(store), target.branch)
                if remote.state is RemoteRefState.ERROR:
                    raise _ProofFailed(
                        f"could not read the parent's remote branch: {remote.error}"
                    )
                if remote.state is not RemoteRefState.PRESENT:
                    raise _ProofFailed("the parent branch is not published on the repository")
                if remote.oid != expected:
                    raise _ProofFailed(
                        f"the parent branch is at {remote.oid}, not the recorded collection "
                        f"head {expected}",
                        remote_head=remote.oid,
                    )
                delivered = [
                    (row["id"], row["after_sha"])
                    for row in facts["receipts"]
                    if row["after_sha"]
                ]
                for label, sha in [
                    ("the episode's pre-collection head",
                     facts["episode"]["pre_collection_checkpoint_sha"]),
                    *((f"receipt {receipt_id}", sha) for receipt_id, sha in delivered),
                ]:
                    if not await promotion._is_ancestor(store, sha, remote.oid):
                        raise _ProofFailed(
                            f"{label} ({sha}) is no longer on the parent branch",
                            remote_head=remote.oid,
                        )
            if facts.get("no_progress"):
                from src.integration.parent_repair_heads import ParentHeadRecovery

                async with self.db._engine.connect() as conn:
                    try:
                        await ParentHeadRecovery(promotion)._workspace_proof_on(
                            conn, facts, resolved, remote.oid
                        )
                    except ValueError as exc:
                        raise _ProofFailed(str(exc), remote_head=remote.oid) from exc
        except PromotionError as exc:
            raise _ProofFailed(f"could not prove the parent branch: {exc}") from exc
        return remote.oid

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    async def _apply_on(
        self,
        conn: Any,
        facts: dict[str, Any],
        *,
        now: float,
        head_sha: str,
        reason: str,
        operator_id: str | None,
    ) -> tuple[Any | None, dict[str, Any]]:
        from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
        from src.integration.writers import OperationSafety

        operation = facts["operation"]
        owner = facts["owner"]
        ownership = BranchOwnership(self.db, clock=self.clock)
        try:
            if owner["handoff_state"] == "released":
                fence = await ownership.acquire(
                    facts["target"], operation["id"], "collector", conn=conn
                )
            else:
                # A detached reservation the cancellation kept: reclaim it at
                # the next token all the same, so nothing holding the old
                # token (a queued delivery) can still write.
                fence = await ownership.transfer_detached_on(
                    conn,
                    Fence(
                        target=facts["target"],
                        owner_id=owner["owner_id"],
                        token=int(owner["fence_token"]),
                    ),
                    operation["id"],
                    "collector",
                )
        except (BranchBusy, StaleFence) as exc:
            raise _Changed() from exc
        fence_token = fence.token

        transition = None
        if facts["restore_parent"]:
            transition, refusal = await OperationSafety(
                self.db
            )._restore_parent_collection_on(conn, operation, allow_paused=False)
            if refusal is not None:
                raise _Changed()

        plan = facts["stage_plan"]
        active_stage = int(plan["ordinal"]) if plan is not None else int(operation["active_stage"])
        state = "active" if active_stage == 0 else "escalated"
        reopened = await conn.execute(
            update(integration_repair_operations)
            .where(
                integration_repair_operations.c.id == operation["id"],
                integration_repair_operations.c.state == operation["state"],
                integration_repair_operations.c.active_stage == operation["active_stage"],
            )
            .values(state=state, active_stage=active_stage, updated_at=now)
        )
        if reopened.rowcount != 1:
            raise _Changed()

        audit = {
            "operator_id": operator_id,
            "reason": reason,
            "at": now,
            "operation_id": operation["id"],
            "episode_id": facts["episode"]["id"],
            "head_sha": head_sha,
            "previous_owner": {
                key: owner[key] for key in ("owner_id", "owner_role", "fence_token")
            },
            "collector_fence_token": fence_token,
            "cancelled_stages": [
                int(stage["ordinal"]) for stage in facts["stages"] if stage["state"] == "cancelled"
            ],
            "ended_stages": [int(stage["ordinal"]) for stage in facts["stages"]],
            "previous_operation_state": operation["state"],
            "receipts": [row["id"] for row in facts["receipts"]],
        }
        if plan is not None:
            from src.integration.repair import RepairService

            conflict = facts["conflict"]
            await conn.execute(
                insert(integration_repair_stages).values(
                    **await RepairService(self.db, clock=self.clock).fresh_parent_stage_row_on(
                        conn,
                        operation | {"state": state},
                        conflict,
                        plan,
                        now=now,
                        extra={"reopened": dict(audit)},
                    )
                )
            )
            audit["stage"] = int(plan["ordinal"])
            audit["conflict_intent_id"] = conflict["id"]
        await conn.execute(
            insert(events).values(
                event_type=REOPEN_EVENT,
                project_id=facts["project_id"],
                task_id=facts["parent"]["id"],
                timestamp=now,
                payload=json.dumps(audit),
            )
        )
        return transition, {
            "fence_token": fence_token,
            "stage": int(plan["ordinal"]) if plan is not None else None,
        }
