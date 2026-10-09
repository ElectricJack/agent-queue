"""Subject writer filing, expiring authority and provider-backed stop proof.

These are primitives 11–13 of rev-agile-ridge revision 2. CommandHandler's
reconciler adapters bind them with ``WriterPrimitives.bind(ports)``; importing
this module activates nothing. The caller supplies routing policy and the
daemon's existing OwnerRecovery. Publish adapters must use ``ownership`` (or
``mutation_exclusion``), which checks expiry as well as the durable fence.

Expiry removes write authority. It never proves a process stopped and never
permits taking another writer's branch. Only stop proof releases that branch.
Filing and its original budget are journalled atomically with the task, so
replays, including after archival, cannot allocate another worker or clock.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select, update

from src.database.queries.integration_state_queries import session_attached_clause
from src.database.tables import (
    integration_repair_operations,
    archived_tasks,
    integration_branch_owners,
    integration_candidate_ref_mutations,
    integration_owner_recoveries,
    integration_subject_journal,
    projects,
    sessions,
    task_metadata,
    tasks,
    workspaces,
)
from src.integration.models import BranchKey, Fence
from src.integration.outbox import enqueue_integration_event
from src.integration.owner_recovery import (
    CHECKOUT_IN_USE,
    PRESERVED_AND_RELEASED,
    RELEASED,
    WRITER_LIVE,
    OwnerRecovery,
    _project_ids,
    _Refusal,
)
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
from src.integration.runtime_contracts import (
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    Subject,
    SubjectEngine,
    SubjectState,
    WriterBudget,
    WriterFileArgs,
    WriterLease,
    WriterLeaseArgs,
    WriterRole,
    WriterStatus,
    WriterStopProofArgs,
    budget_values,
    writer_values,
)
from src.models import Task, TaskStatus, TaskType

from sqlalchemy import and_
from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.database.tables import (
    integration_attestation_publications,
    integration_batches,
    integration_candidate_publications,
    integration_candidate_resolutions,
    integration_cleanup_items,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_stages,
    task_integration_checkpoints,
)


class ExpiringBranchOwnership(BranchOwnership):
    """The subject publisher's ownership port: an expired fence is stale."""

    def _require_current(self, row: dict[str, Any] | None, fence: Fence) -> None:
        super()._require_current(row, fence)
        if row["expires_at"] is None or self._clock() >= row["expires_at"]:
            raise StaleFence("writer lease has no unexpired authority")


class _ExactWriterRecovery(OwnerRecovery):
    """Reuse recovery's preservation/release, pinned to the requested writer."""

    recoverable_states = ("reserved", "attached", "handoff_pending")

    def __init__(self, recovery: OwnerRecovery, fence: Fence, subject: Subject) -> None:
        super().__init__(
            recovery.db,
            recovery.git,
            recovery.git_mutex,
            confirm_stopped=recovery.confirm_stopped,
            clock=recovery.clock,
        )
        self.fence = fence
        self.subject = subject

    async def _load(self, owner_row_id: str) -> dict[str, Any] | None:
        row = await super()._load(owner_row_id)
        if row is None or BranchOwnership._fence(row) != self.fence:
            return None
        return row

    async def _prove_writer_gone(self, row: dict[str, Any], evidence: dict[str, Any]):
        # An incomplete attachment is unknown, not the "never attached" proof
        # of a reserved row. Missing provider evidence also stays unknown.
        async with self.db._engine.connect() as conn:
            session = None
            if row["session_id"]:
                session = (
                    (await conn.execute(select(sessions).where(sessions.c.id == row["session_id"])))
                    .mappings()
                    .one_or_none()
                )
                if session is None:
                    raise _Refusal("stop_unknown", "writer session record is missing")
            elif row["handoff_state"] != "reserved":
                raise _Refusal("stop_unknown", "attached writer has no session identity")
            if row["workspace_id"] and not await conn.scalar(
                select(workspaces.c.id).where(workspaces.c.id == row["workspace_id"])
            ):
                raise _Refusal("stop_unknown", "writer workspace record is missing")
        if session is not None and session["state"] == session["desired_state"] == "stopped":
            if self.confirm_stopped is None:
                raise _Refusal("stop_unknown", "session provider stop proof is unavailable")
            confirmer = self.confirm_stopped
            probe_unknown = False

            async def probe(snapshot):
                nonlocal probe_unknown
                try:
                    return await confirmer(snapshot)
                except Exception as exc:
                    probe_unknown = True
                    raise _Refusal("stop_unknown", f"session provider probe failed: {exc}") from exc

            self.confirm_stopped = probe
        else:
            probe_unknown = False
        try:
            return await super()._prove_writer_gone(row, evidence)
        except _Refusal as exc:
            if probe_unknown:
                raise _Refusal("stop_unknown", exc.detail) from exc
            raise

    async def _release(self, row, session, plan, outcome, evidence, principal):
        # Keep OwnerRecovery's release, audit, workspace and claim behavior.
        # Its transaction is augmented with subject/session revalidation, so
        # a new claim or a resumed process during remote preservation cannot
        # be unlocked by a proof from the earlier snapshot.
        db = self.db
        self.db = _ReleaseCheckedDB(db, self.subject, row, session)
        try:
            return await super()._release(row, session, plan, outcome, evidence, principal)
        finally:
            self.db = db


class _ReleaseCheckedDB:
    """Join extra checks to the existing recovery's release transaction."""

    def __init__(self, db, subject, owner, session):
        self.db, self.subject, self.owner, self.session = db, subject, owner, session

    def __getattr__(self, name):
        return getattr(self.db, name)

    @asynccontextmanager
    async def immediate(self):
        async with self.db.immediate() as conn:
            for project_id in await _project_ids(conn, self.subject.repository_id):
                await self.db.lock_hierarchy_project(conn, project_id)
            subject = await self.db.lock_integration_subject_on(conn, self.subject.id)
            if (
                subject is None
                or subject["version"] != self.subject.version
                or subject["gate_id"] is not None
                or subject["engine"] != "reconciler"
            ):
                raise _Refusal("subject_changed", "subject changed during stop proof")
            if (
                await conn.scalar(
                    select(projects.c.status)
                    .where(
                        projects.c.id == self.subject.project_id,
                    )
                    .with_for_update()
                )
                != "ACTIVE"
            ):
                raise _Refusal("subject_held", "project became inactive during stop proof")
            for task_id in sorted({self.subject.task_id, self.owner["owner_id"]} - {None}):
                await conn.execute(
                    select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
                )
                if await self.db._read_manual_pause(conn, task_id) is not None:
                    raise _Refusal("subject_held", "task manually paused during stop proof")
            row = (
                (
                    await conn.execute(
                        select(integration_branch_owners)
                        .where(
                            integration_branch_owners.c.id == self.owner["id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            fields = ("owner_id", "fence_token", "handoff_state", "session_id", "workspace_id")
            if row is None or any(row[field] != self.owner[field] for field in fields):
                raise _Refusal("stale_fence", "writer binding changed during stop proof")
            if self.session is not None:
                session = (
                    (
                        await conn.execute(
                            select(sessions)
                            .where(
                                sessions.c.id == self.session["id"],
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                fields = (
                    "id",
                    "name",
                    "provider",
                    "instance_token",
                    "state",
                    "desired_state",
                    "task_id",
                    "agent_id",
                    "work_dir",
                    "last_claim_epoch",
                )
                if session is None:
                    raise _Refusal("stop_unknown", "writer session disappeared during stop proof")
                if any(session[field] != self.session[field] for field in fields):
                    raise _Refusal(WRITER_LIVE, "writer session changed during stop proof")
            if await conn.scalar(
                select(sessions.c.id)
                .where(
                    sessions.c.task_id == self.owner["owner_id"],
                    session_attached_clause(),
                )
                .limit(1)
            ):
                raise _Refusal(WRITER_LIVE, "writer was claimed during stop proof")
            if self.owner["workspace_id"]:
                workspace = (
                    (
                        await conn.execute(
                            select(workspaces)
                            .where(
                                workspaces.c.id == self.owner["workspace_id"],
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if workspace is None:
                    raise _Refusal("stop_unknown", "workspace disappeared during stop proof")
                if workspace["locked_by_task_id"] not in {
                    None,
                    self.owner["owner_id"],
                } or await conn.scalar(
                    select(sessions.c.id)
                    .where(
                        sessions.c.work_dir == workspace["workspace_path"],
                        session_attached_clause(),
                    )
                    .limit(1)
                ):
                    raise _Refusal(CHECKOUT_IN_USE, "workspace acquired during stop proof")
            if await conn.scalar(
                select(integration_candidate_ref_mutations.c.id)
                .where(
                    integration_candidate_ref_mutations.c.repository_id
                    == self.subject.repository_id,
                    integration_candidate_ref_mutations.c.branch.in_(
                        (
                            self.owner["ref"].removeprefix("refs/heads/"),
                            f"refs/heads/{self.owner['ref'].removeprefix('refs/heads/')}",
                        )
                    ),
                    integration_candidate_ref_mutations.c.state == "reserved",
                )
                .limit(1)
            ):
                raise _Refusal("write_in_flight", "external mutation is still in flight")
            yield conn
            await _retire_writer_on(self.db, conn, self.owner["owner_id"])


async def _retire_writer_on(db, conn, task_id):
    """A released ordinal stays inspectable but cannot re-enter the frontier."""
    status = await conn.scalar(
        select(tasks.c.status).where(tasks.c.id == task_id).with_for_update()
    )
    if status is not None and status not in {"COMPLETED", "FAILED", "PAUSED", "BLOCKED"}:
        await db._apply_transition(
            conn,
            task_id,
            TaskStatus.BLOCKED,
            context="integration_repair_retained_handoff",
            resume_after=None,
            assigned_agent_id=None,
        )


async def _writer_blocked_on(conn, task_id):
    marker = await conn.scalar(
        select(task_metadata.c.value).where(
            task_metadata.c.task_id == task_id,
            task_metadata.c.key == "blocked_terminal",
        )
    )
    return marker is not None and json.loads(marker) == "integration_repair_retained_handoff"


class WriterPrimitives:
    """One filing path and one authority/proof path for all subject kinds.

    Filed tasks are BLOCKED until the lease is reserved. Leasing wakes them and
    emits task.created for mandatory routing; no provider/profile is selected
    here. ``routing_policy`` is the same callback CommandHandler gives task
    creation. Parent/reconciler wiring is deliberately owned by the next task.
    """

    def __init__(
        self,
        db,
        *,
        owner_recovery: OwnerRecovery | None = None,
        routing_policy: Callable[[Task], bool] | None = None,
        clock: Callable[[], float] = time.time,
        managed_leases: bool = False,
    ) -> None:
        self.db = db
        self.recovery = owner_recovery
        self.routing_policy = routing_policy
        self.clock = clock
        # Shadow continues to use legacy execution. The active git-first
        # adapter selects this port, without project-level write authority.
        self.managed_leases = managed_leases
        self.ownership = ExpiringBranchOwnership(db, clock=clock)

    def bind(self, ports: PrimitivePorts) -> None:
        ports.bind(Primitive.WRITER_FILE, self.file)
        ports.bind(Primitive.WRITER_LEASE, self.lease)
        ports.bind(Primitive.WRITER_STOP_PROOF, self.stop_proof)

    async def _subject_on(self, conn, expected: Subject, *, replay: bool = False) -> Subject | None:
        # Same order as claim, routing and OwnerRecovery: hierarchy, subject,
        # task/branch rows. Reject old observations and binding human holds.
        await self.db.lock_hierarchy_project(conn, expected.project_id)
        row = await self.db.lock_integration_subject_on(conn, expected.id)
        if row is None:
            return None
        current = Subject.from_row(row)
        if (
            (current.version != expected.version and not replay)
            or current.project_id != expected.project_id
            or current.repository_id != expected.repository_id
            or current.engine is not SubjectEngine.RECONCILER
            or not current.is_live
            or current.state is SubjectState.HELD
        ):
            return None
        if (
            await conn.scalar(
                select(projects.c.status)
                .where(
                    projects.c.id == current.project_id,
                )
                .with_for_update()
            )
            != "ACTIVE"
        ):
            return None
        for task_id in sorted({current.task_id, current.writer.task_id} - {None}):
            await conn.execute(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update())
            if await self.db._read_manual_pause(conn, task_id) is not None:
                return None
        return current

    async def _journal_on(self, conn, subject, primitive, key, outcome, payload):
        return await self.db.append_integration_subject_journal_on(
            conn,
            {
                "subject_id": subject.id,
                "entry_kind": "action",
                "idempotency_key": key,
                "mode": "active",
                "policy_artifact_sha256": subject.policy.artifact_sha256,
                "subject_version": subject.version,
                "phase": subject.phase.value,
                "head_sha": subject.head_sha,
                "generation": subject.generation,
                "primitive": primitive.value,
                "outcome": outcome,
                "payload": payload,
                "recorded_at": self.clock(),
            },
        )

    async def _update_on(self, conn, subject, values):
        row = await self.db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            values=values,
            now=self.clock(),
        )
        if row is None:  # the row is locked; missing CAS would be an invariant violation
            raise RuntimeError("locked writer subject changed")
        return Subject.from_row(row)

    async def file(self, subject: Subject, args: WriterFileArgs) -> PrimitiveOutcome:
        p = Primitive.WRITER_FILE
        key = f"writer-file:{args.ordinal}"
        async with self.db.immediate() as conn:
            current = await self._subject_on(conn, subject, replay=True)
            if current is None:
                return PrimitiveOutcome(
                    primitive=p, outcome="configuration_blocked", reason="subject_stale_or_held"
                )
            existing = (
                (
                    await conn.execute(
                        select(integration_subject_journal).where(
                            integration_subject_journal.c.subject_id == subject.id,
                            integration_subject_journal.c.idempotency_key == key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            request = args.model_dump(mode="json")
            if existing is not None:
                payload = existing["payload"]
                if payload["request"] != request:
                    return PrimitiveOutcome(
                        primitive=p,
                        outcome="configuration_blocked",
                        reason="ordinal_request_changed",
                    )
                return PrimitiveOutcome(primitive=p, outcome="exists", detail=payload)
            if current.version != subject.version:
                return PrimitiveOutcome(
                    primitive=p, outcome="configuration_blocked", reason="subject_stale_or_held"
                )
            if not current.target_ref:
                return PrimitiveOutcome(
                    primitive=p, outcome="configuration_blocked", reason="target_ref_missing"
                )
            if current.writer.status not in {WriterStatus.NONE, WriterStatus.STOPPED}:
                return PrimitiveOutcome(
                    primitive=p,
                    outcome="configuration_blocked",
                    reason="previous_writer_not_proven_stopped",
                )
            if current.budget and args.ordinal <= current.budget.ordinal:
                return PrimitiveOutcome(
                    primitive=p, outcome="configuration_blocked", reason="ordinal_not_increasing"
                )
            identity = hashlib.sha256(f"{subject.id}:{args.ordinal}".encode()).hexdigest()
            task_id = f"writer-{identity}"
            for table in (tasks, archived_tasks):
                if await conn.scalar(select(table.c.id).where(table.c.id == task_id)):
                    return PrimitiveOutcome(
                        primitive=p,
                        outcome="configuration_blocked",
                        reason="writer_identity_collision",
                    )
            now = self.clock()
            budget = WriterBudget(
                ordinal=args.ordinal,
                intelligence_class=args.intelligence_class,
                started_at=now,
                deadline_at=now + args.budget_seconds,
                attempt_limit=args.attempt_limit,
            )
            await self.db.create_task(
                Task(
                    id=task_id,
                    project_id=subject.project_id,
                    title=f"{args.role.value} for integration subject {subject.id}",
                    description=(
                        f"{args.brief}\n\nSubject: {subject.id}; generation: "
                        f"{current.generation}; head: {current.head_sha}; ordinal: "
                        f"{args.ordinal}; deadline: {budget.deadline_at}; attempt limit: "
                        f"{args.attempt_limit}; members: {', '.join(args.scope_members)}"
                    ),
                    status=TaskStatus.DEFINED,
                    repo_id=subject.repository_id,
                    branch_name=current.target_ref.removeprefix("refs/heads/"),
                    task_type=TaskType.TEST
                    if args.role is WriterRole.VERIFIER
                    else TaskType.BUGFIX,
                    class_hint=args.intelligence_class,
                    dedup_key=f"{subject.id}:{key}",
                    created_by_kind="integration_writer",
                    created_by_id=subject.id,
                ),
                conn=conn,
                routing_policy=self.routing_policy,
            )
            await _retire_writer_on(self.db, conn, task_id)
            await self._update_on(
                conn,
                current,
                {
                    **writer_values(WriterLease(status=WriterStatus.FILED, task_id=task_id)),
                    **budget_values(budget),
                },
            )
            payload = {
                "task_id": task_id,
                "request": request,
                "budget": budget.model_dump(mode="json"),
            }
            await self._journal_on(conn, current, p, key, "filed", payload)
            return PrimitiveOutcome(primitive=p, outcome="filed", detail=payload)

    async def _owner_on(self, conn, subject):
        from src.integration.lock import BranchLock

        # Acquisition, mutation and adapters must take advisory then row lock.
        return await BranchLock(self.db, clock=self.clock).lock_on(
            conn, BranchKey(repository_id=subject.repository_id, branch=subject.target_ref)
        )

    async def lease(self, subject: Subject, args: WriterLeaseArgs) -> PrimitiveOutcome:
        p = Primitive.WRITER_LEASE
        ready = []
        async with self.db.immediate() as conn:
            current = await self._subject_on(conn, subject)
            if (
                current is None
                or not current.target_ref
                or current.budget is None
                or current.writer.task_id != args.owner_task_id
                or current.writer.status in {WriterStatus.STOPPED, WriterStatus.UNKNOWN}
                or args.ref.removeprefix("refs/heads/")
                != current.target_ref.removeprefix("refs/heads/")
                or current.budget.expired(self.clock())
                or current.budget.exhausted
            ):
                return PrimitiveOutcome(primitive=p, outcome="stale")
            try:
                owner = await self._owner_on(conn, current)
            except BranchBusy as exc:
                return PrimitiveOutcome(primitive=p, outcome="busy", detail={"holder": str(exc)})
            transfer = False
            managed_lease = self.managed_leases or (
                owner is not None and owner.get("fence") is not None
            )
            if managed_lease:
                # Handoff/session/workspace state is not lease eligibility.
                # Leave subject policy/claim admission in this adapter; the
                # per-ref lock is the only managed write authority.
                if owner and owner.get("holder") and owner["expires_at"] > self.clock():
                    if owner["holder"] != args.owner_task_id:
                        return PrimitiveOutcome(
                            primitive=p, outcome="busy", detail={"holder": owner["holder"]}
                        )
                    if current.writer.fence_token != owner["fence"]:
                        return PrimitiveOutcome(primitive=p, outcome="stale")
                    return PrimitiveOutcome(
                        primitive=p,
                        outcome="leased",
                        detail={
                            "fence": BranchOwnership._fence(owner).model_dump(mode="json"),
                            "expires_at": owner["expires_at"],
                        },
                    )
                # An expired grant cannot be refreshed by replaying its old
                # primitive. A fresh ordinary task acquires a fresh fence.
                if current.writer.fence_token is not None:
                    return PrimitiveOutcome(primitive=p, outcome="stale")
                owner = None
            if owner and owner["handoff_state"] != "released":
                if owner["owner_id"] != args.owner_task_id:
                    # A detached domain reservation belongs to this subject,
                    # unlike an old writer's reservation. Transfer it only
                    # after checking it has no process or workspace holder.
                    domain_ids = {current.id, current.task_id, current.batch_id} - {None}
                    if current.parent_episode_id:
                        operation_id = await conn.scalar(
                            select(integration_repair_operations.c.id).where(
                                integration_repair_operations.c.parent_task_id == current.task_id,
                                integration_repair_operations.c.episode_id
                                == current.parent_episode_id,
                            )
                        )
                        if operation_id:
                            domain_ids.add(operation_id)
                    transfer = (
                        owner["owner_id"] in domain_ids
                        and owner["handoff_state"] == "reserved"
                        and owner["session_id"] is None
                        and owner["workspace_id"] is None
                        and not await conn.scalar(
                            select(sessions.c.id)
                            .where(
                                sessions.c.task_id == owner["owner_id"],
                                session_attached_clause(),
                            )
                            .limit(1)
                        )
                        and not await conn.scalar(
                            select(workspaces.c.id)
                            .where(
                                workspaces.c.locked_by_task_id == owner["owner_id"],
                            )
                            .limit(1)
                        )
                    )
                    if not transfer:
                        return PrimitiveOutcome(
                            primitive=p, outcome="busy", detail={"holder": owner["owner_id"]}
                        )
                # An existing binding can only be replayed, never extended.
                if not transfer and (
                    owner["handoff_state"] not in {"reserved", "attached"}
                    or current.writer.fence_token != owner["fence_token"]
                    or owner["expires_at"] is None
                    or self.clock() >= owner["expires_at"]
                ):
                    return PrimitiveOutcome(primitive=p, outcome="stale")
                if not transfer:
                    fence = BranchOwnership._fence(owner)
                    return PrimitiveOutcome(
                        primitive=p,
                        outcome="leased",
                        detail={
                            "fence": fence.model_dump(mode="json"),
                            "expires_at": owner["expires_at"],
                        },
                    )
            if current.writer.fence_token is not None:
                return PrimitiveOutcome(primitive=p, outcome="stale")
            task = (
                (
                    await conn.execute(
                        select(tasks)
                        .where(
                            tasks.c.id == args.owner_task_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                task is None
                or task["status"] != "BLOCKED"
                or not await _writer_blocked_on(conn, args.owner_task_id)
            ):
                return PrimitiveOutcome(primitive=p, outcome="stale")
            target = BranchKey(
                repository_id=current.repository_id,
                branch=owner["ref"] if owner else args.ref.removeprefix("refs/heads/"),
            )
            filing = (
                await conn.execute(
                    select(integration_subject_journal.c.payload).where(
                        integration_subject_journal.c.subject_id == current.id,
                        integration_subject_journal.c.idempotency_key
                        == f"writer-file:{current.budget.ordinal}",
                    )
                )
            ).scalar_one()
            role = "verifier" if filing["request"]["role"] == "verifier" else "repair"
            # acquire does not assert expiry on a released row; a fresh token
            # receives its expiry before this transaction exposes it.
            try:
                if transfer:
                    # The old domain reservation need not have a writer TTL.
                    fence = await BranchOwnership(self.db).transfer_detached_on(
                        conn,
                        BranchOwnership._fence(owner),
                        args.owner_task_id,
                        role,
                    )
                else:
                    if managed_lease:
                        from src.integration.lock import BranchLock

                        fence = await BranchLock(self.db, clock=self.clock).acquire(
                            target,
                            args.owner_task_id,
                            role=role,
                            conn=conn,
                            ttl_seconds=min(
                                args.ttl_seconds, current.budget.deadline_at - self.clock()
                            ),
                        )
                    else:
                        fence = await self.ownership.acquire(
                            target, args.owner_task_id, role, conn=conn
                        )
            except BranchBusy:
                return PrimitiveOutcome(
                    primitive=p, outcome="busy", detail={"holder": "racing_owner"}
                )
            expiry = min(self.clock() + args.ttl_seconds, current.budget.deadline_at)
            await conn.execute(
                update(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == target.repository_id,
                    integration_branch_owners.c.ref == target.branch,
                    integration_branch_owners.c.fence_token == fence.token,
                )
                .values(expires_at=expiry, updated_at=self.clock())
            )
            await self._update_on(conn, current, {"writer_fence_token": fence.token})
            task = (
                (
                    await conn.execute(
                        select(tasks)
                        .where(
                            tasks.c.id == args.owner_task_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if task["status"] == TaskStatus.BLOCKED.value:
                transition = await self.db._apply_transition(
                    conn,
                    task["id"],
                    TaskStatus.READY,
                    context="integration_writer_lease",
                )
                ready = transition.ready
            await enqueue_integration_event(
                conn,
                event_id=f"writer-created:{current.id}:{current.budget.ordinal}",
                dedup_key=f"writer-created:{current.id}:{current.budget.ordinal}",
                project_id=current.project_id,
                event_type="task.created",
                payload={
                    "task_id": task["id"],
                    "project_id": current.project_id,
                    "title": task["title"],
                    "task_type": task["task_type"],
                    "class_hint": task["class_hint"],
                    "created_by_kind": "integration_writer",
                    "created_by_id": current.id,
                },
                available_at=self.clock(),
            )
            detail = {"fence": fence.model_dump(mode="json"), "expires_at": expiry}
            await self._journal_on(
                conn, current, p, f"writer-lease:{current.budget.ordinal}", "leased", detail
            )
        await self.db._notify_ready(ready)
        return PrimitiveOutcome(primitive=p, outcome="leased", detail=detail)

    async def stop_proof(self, subject: Subject, args: WriterStopProofArgs) -> PrimitiveOutcome:
        p = Primitive.WRITER_STOP_PROOF
        async with self.db.immediate() as conn:
            current = await self._subject_on(conn, subject, replay=True)
            if current is None or not current.target_ref or current.writer.task_id != args.task_id:
                return PrimitiveOutcome.unknown(p, "writer_subject_stale")
            if current.writer.status is WriterStatus.STOPPED:
                proof = current.writer.stop_proof or {}
                if proof.get("outcome") not in {RELEASED, PRESERVED_AND_RELEASED}:
                    return PrimitiveOutcome.unknown(p, "stop_proof_missing")
                if args.fence_token is not None and args.fence_token != proof.get("fence_token"):
                    return PrimitiveOutcome.unknown(p, "writer_fence_stale")
                return PrimitiveOutcome(primitive=p, outcome=proof["outcome"], detail=proof)
            if current.version != subject.version:
                return PrimitiveOutcome.unknown(p, "writer_subject_stale")
            try:
                owner = await self._owner_on(conn, current)
            except BranchBusy as exc:
                return PrimitiveOutcome.unknown(p, str(exc))
            if self.managed_leases or (owner is not None and owner.get("fence") is not None):
                # This primitive is a legacy process/workspace recovery saga.
                # Active callers release exact lease identities through lock.py;
                # expiry already frees the ref without asserting process death.
                return PrimitiveOutcome.unknown(p, "managed_ref_uses_lease_release")
            token = current.writer.fence_token
            if token is None and args.fence_token is None:
                # Filing is blocked until its first lease. A never-leased,
                # never-started task has no write authority to transfer. Its
                # stop proof changes no other owner's branch reservation.
                task = (
                    (
                        await conn.execute(
                            select(tasks)
                            .where(
                                tasks.c.id == args.task_id,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    task is None
                    or current.budget is None
                    or task["status"] != "BLOCKED"
                    or not await _writer_blocked_on(conn, args.task_id)
                    or task["created_by_kind"] != "integration_writer"
                    or task["created_by_id"] != current.id
                    or (owner is not None and owner["owner_id"] == args.task_id)
                    or await conn.scalar(
                        select(sessions.c.id)
                        .where(
                            sessions.c.task_id == args.task_id,
                        )
                        .limit(1)
                    )
                    or await conn.scalar(
                        select(workspaces.c.id)
                        .where(
                            workspaces.c.locked_by_task_id == args.task_id,
                        )
                        .limit(1)
                    )
                ):
                    return PrimitiveOutcome.unknown(p, "never_leased_stop_unproven")
                proof = {
                    "outcome": RELEASED,
                    "fence_token": None,
                    "task_id": args.task_id,
                    "detail": "never leased or started",
                }
                await self._update_on(
                    conn,
                    current,
                    {
                        "writer_status": WriterStatus.STOPPED.value,
                        "writer_stop_proof": proof,
                    },
                )
                await self._journal_on(
                    conn,
                    current,
                    p,
                    f"writer-stop-unleased:{current.budget.ordinal}",
                    RELEASED,
                    proof,
                )
                return PrimitiveOutcome(primitive=p, outcome=RELEASED, detail=proof)
            if (
                owner is None
                or owner["owner_id"] != args.task_id
                or token is None
                or (args.fence_token is not None and args.fence_token != token)
            ):
                return PrimitiveOutcome.unknown(p, "writer_fence_stale")
            if (
                args.preserve_ref is not None
                and args.preserve_ref.removeprefix("refs/heads/") not in {
                    owner["ref"].removeprefix("refs/heads/"),
                    f"aq/preserved/{owner['id']}",
                }
                and not args.preserve_ref.removeprefix("refs/heads/").startswith(
                    f"aq/recovery/{owner['id']}/"
                )
            ):
                return PrimitiveOutcome.unknown(p, "preservation_ref_must_use_existing_recovery")
            if await conn.scalar(
                select(integration_candidate_ref_mutations.c.id)
                .where(
                    integration_candidate_ref_mutations.c.repository_id == current.repository_id,
                    integration_candidate_ref_mutations.c.branch == owner["ref"],
                    integration_candidate_ref_mutations.c.state == "reserved",
                )
                .limit(1)
            ):
                return PrimitiveOutcome.unknown(p, "write_in_flight")
            fence = Fence(
                target=BranchKey(repository_id=current.repository_id, branch=owner["ref"]),
                owner_id=args.task_id,
                token=token,
            )
            if owner["handoff_state"] == "released" and owner["fence_token"] == token + 1:
                # Recovery committed before a crash interrupted subject bookkeeping.
                audit = (
                    (
                        await conn.execute(
                            select(integration_owner_recoveries)
                            .where(
                                integration_owner_recoveries.c.owner_row_id == owner["id"],
                                integration_owner_recoveries.c.outcome.in_(
                                    (RELEASED, PRESERVED_AND_RELEASED)
                                ),
                            )
                            .order_by(integration_owner_recoveries.c.created_at.desc())
                        )
                    )
                    .mappings()
                    .all()
                )
                result = next(
                    (
                        row
                        for row in audit
                        if row["evidence"].get("fence_token") == token
                        and row["evidence"].get("released_fence_token") == token + 1
                    ),
                    None,
                )
                if result is None:
                    return PrimitiveOutcome.unknown(p, "release_proof_missing")
                proof = {"outcome": result["outcome"], **result["evidence"]}
            else:
                if owner["fence_token"] != token or self.recovery is None:
                    return PrimitiveOutcome.unknown(p, "writer_fence_or_recovery_unavailable")
                proof = None
        if proof is None:
            result = await _ExactWriterRecovery(self.recovery, fence, current).recover(
                owner["id"],
                principal=f"integration-subject:{subject.id}",
            )
            if result.outcome not in {RELEASED, PRESERVED_AND_RELEASED}:
                if result.reason in {WRITER_LIVE, CHECKOUT_IN_USE}:
                    return PrimitiveOutcome(primitive=p, outcome="live", detail=result.evidence)
                return PrimitiveOutcome.unknown(
                    p, result.reason or "stop_unproven", **result.evidence
                )
            proof = {"outcome": result.outcome, **result.evidence}
        async with self.db.immediate() as conn:
            current = await self._subject_on(conn, subject)
            if current is None:
                return PrimitiveOutcome.unknown(p, "subject_changed_after_recovery", **proof)
            owner = await self._owner_on(conn, current)
            if (
                owner is None
                or owner["owner_id"] != args.task_id
                or owner["handoff_state"] != "released"
                or owner["fence_token"] != token + 1
            ):
                return PrimitiveOutcome.unknown(p, "fence_changed_after_recovery", **proof)
            if await conn.scalar(
                select(sessions.c.id)
                .where(
                    sessions.c.task_id == args.task_id,
                    session_attached_clause(),
                )
                .limit(1)
            ):
                return PrimitiveOutcome.unknown(p, "writer_claimed_after_recovery", **proof)
            await _retire_writer_on(self.db, conn, args.task_id)
            await self._update_on(
                conn,
                current,
                {
                    "writer_status": WriterStatus.STOPPED.value,
                    "writer_stop_proof": proof,
                },
            )
            await self._journal_on(
                conn, current, p, f"writer-stop:{args.task_id}:{token}", proof["outcome"], proof
            )
        return PrimitiveOutcome(primitive=p, outcome=proof["outcome"], detail=proof)


__all__ = ["ExpiringBranchOwnership", "WriterPrimitives"]


class OperationSafety:
    """Exact operation locks and ambiguity checks shared by subject primitives."""

    def __init__(self, db):
        self.db = db

    async def _restore_parent_collection_on(
        self,
        conn: Any,
        operation: dict[str, Any],
        *,
        allow_paused: bool,
        validate_only: bool = False,
    ) -> tuple[Any | None, str | None]:
        """Restore only the terminally blocked parent for this exact episode.

        The transition owns terminal-block metadata and the blocked projection;
        the surrounding checks are repeated in its UPDATE guard so an old
        recovery request cannot revive a parent a newer writer has claimed.
        """
        if operation["target_kind"] != "parent":
            return None, None
        parent = (
            await conn.execute(
                select(tasks)
                .where(tasks.c.id == operation["parent_task_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == operation["parent_task_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        episode = (
            await conn.execute(
                select(integration_parent_episodes)
                .where(integration_parent_episodes.c.id == operation["episode_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            parent is None
            or checkpoint is None
            or episode is None
            or checkpoint["episode_id"] != operation["episode_id"]
            or episode["parent_task_id"] != operation["parent_task_id"]
            or parent["repo_id"] != checkpoint["repository_id"]
            or parent["branch_name"] != checkpoint["branch"]
            or episode["repository_id"] != parent["repo_id"]
            or int(checkpoint["generation"]) < int(episode["generation"])
        ):
            return None, "stale"
        if await self.db._read_manual_pause(conn, operation["parent_task_id"]) is not None:
            return None, "invalid_state"
        if parent["assigned_agent_id"] is not None:
            return None, "invalid_state"
        if parent["status"] == TaskStatus.PAUSED.value:
            return (None, None) if allow_paused else (None, "invalid_state")
        terminal_context = (
            await conn.execute(
                select(task_metadata.c.value).where(
                    task_metadata.c.task_id == operation["parent_task_id"],
                    task_metadata.c.key == TERMINAL_BLOCKED_META_KEY,
                )
            )
        ).scalar_one_or_none()
        encoded_terminal_context = terminal_context
        try:
            terminal_context = json.loads(terminal_context)
        except (TypeError, ValueError):
            pass  # Older metadata may store the context without JSON encoding.
        if (
            parent["status"] != TaskStatus.BLOCKED.value
            or terminal_context != "integration_repair_exhausted"
        ):
            return None, "invalid_state"
        if validate_only:
            return None, None
        transition = await self.db._apply_transition(
            conn,
            operation["parent_task_id"],
            TaskStatus.PAUSED,
            context="integration_repair_resume",
            force=True,
            resume_after=None,
            assigned_agent_id=None,
            extra_where=and_(
                tasks.c.status == TaskStatus.BLOCKED.value,
                tasks.c.assigned_agent_id.is_(None),
                select(task_metadata.c.task_id)
                .where(
                    task_metadata.c.task_id == operation["parent_task_id"],
                    task_metadata.c.key == TERMINAL_BLOCKED_META_KEY,
                    task_metadata.c.value == encoded_terminal_context,
                )
                .exists(),
            ),
            returning=True,
        )
        if transition.row is None:
            return None, "stale"
        return transition, None


    @staticmethod
    async def _locked_operation_on(conn: Any, operation_id: str) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                select(integration_repair_operations)
                .where(integration_repair_operations.c.id == operation_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None


    @staticmethod
    async def _locked_stage_on(conn: Any, operation: dict[str, Any]) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None


    @staticmethod
    async def _project_id_on(conn: Any, operation: dict[str, Any]) -> str:
        if operation["target_kind"] == "batch":
            statement = select(integration_batches.c.project_id).where(
                integration_batches.c.id == operation["batch_id"]
            )
        else:
            from src.database.queries.task_identity import resolve_task_identity_on

            identity = await resolve_task_identity_on(conn, operation["parent_task_id"])
            project_id = identity.project_id if identity is not None else None
            if project_id is not None:
                return str(project_id)
            statement = select(tasks.c.project_id).where(tasks.c.id == operation["parent_task_id"])
        project_id = (await conn.execute(statement)).scalar_one_or_none()
        if project_id is None:
            raise ValueError("operation target has no owning project")
        return str(project_id)


    @staticmethod
    async def _ambiguous_writes_on(
        conn: Any,
        operation: dict[str, Any],
        *,
        allow_reserved_delegate: bool = False,
        allowed_writer_id: str | None = None,
        allowed_promotion_intent_id: str | None = None,
    ) -> list[str]:
        operation_id = operation["id"]
        writer = select(integration_branch_owners.c.id).where(
            (
                integration_branch_owners.c.owner_id.in_(
                    select(integration_repair_stages.c.repair_task_id).where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.repair_task_id.is_not(None),
                    )
                )
            )
            | (
                integration_branch_owners.c.owner_id.in_(
                    select(integration_candidate_ref_mutations.c.branch_owner_id).where(
                        integration_candidate_ref_mutations.c.operation_id
                        == operation_id
                    )
                )
            ),
            integration_branch_owners.c.handoff_state != "released",
        )
        if allow_reserved_delegate:
            exact_delegate = select(
                integration_repair_stages.c.repair_task_id
            ).where(
                integration_repair_stages.c.operation_id == operation_id,
                integration_repair_stages.c.ordinal == operation["active_stage"],
                integration_repair_stages.c.writer_kind == "repair_delegate",
            )
            writer = writer.where(
                ~and_(
                    integration_branch_owners.c.owner_id.in_(exact_delegate),
                    integration_branch_owners.c.owner_role == "repair",
                    integration_branch_owners.c.handoff_state == "reserved",
                    integration_branch_owners.c.session_id.is_(None),
                    integration_branch_owners.c.workspace_id.is_(None),
                )
            )
            # A rejected reservation excuses only the exact stopped writer
            # that created it. A successor with the same task id is a live
            # ambiguous writer until its own handoff completes.
            resolution = integration_candidate_resolutions.c
            owner = integration_branch_owners.c
            exact_rejected_writer = select(resolution.id).select_from(
                integration_candidate_resolutions.join(
                    sessions, sessions.c.id == resolution.repair_session_id
                ).join(
                    workspaces, workspaces.c.id == resolution.repair_workspace_id
                ).join(
                    integration_repair_stages,
                    (integration_repair_stages.c.operation_id == resolution.operation_id)
                    & (integration_repair_stages.c.ordinal == resolution.stage_ordinal),
                )
            ).where(
                resolution.operation_id == operation_id,
                resolution.stage_ordinal == operation["active_stage"],
                resolution.state == "rejected",
                integration_repair_stages.c.repair_task_id == resolution.repair_task_id,
                integration_repair_stages.c.writer_kind == "repair_delegate",
                owner.owner_id == resolution.repair_task_id,
                owner.owner_role == "repair",
                owner.fence_token == resolution.fence_token,
                owner.handoff_state == "attached",
                owner.session_id == resolution.repair_session_id,
                owner.workspace_id == resolution.repair_workspace_id,
                sessions.c.task_id == resolution.repair_task_id,
                sessions.c.project_id == resolution.project_id,
                sessions.c.instance_token == resolution.repair_session_instance_token,
                sessions.c.state == "stopped",
                sessions.c.desired_state == "stopped",
                sessions.c.work_dir == resolution.repair_workspace_path,
                workspaces.c.project_id == resolution.project_id,
                workspaces.c.locked_by_task_id == resolution.repair_task_id,
                workspaces.c.workspace_path == resolution.repair_workspace_path,
            ).correlate(integration_branch_owners).exists()
            writer = writer.where(
                ~exact_rejected_writer
            )
        if allow_reserved_delegate and operation["batch_id"] is not None:
            # A detached collector is the daemon's reservation, not a live
            # worker. Applied writes are checked separately below; retain all
            # ambiguity guards for attached collectors and other targets.
            exact_target = select(integration_batches.c.id).where(
                integration_batches.c.id == operation["batch_id"],
                integration_batches.c.repository_id == integration_branch_owners.c.repository_id,
                integration_batches.c.integration_branch == integration_branch_owners.c.ref,
            ).exists()
            writer = writer.where(
                ~and_(
                    integration_branch_owners.c.owner_id == operation_id,
                    integration_branch_owners.c.owner_role == "collector",
                    exact_target,
                    integration_branch_owners.c.handoff_state == "reserved",
                    integration_branch_owners.c.session_id.is_(None),
                    integration_branch_owners.c.workspace_id.is_(None),
                )
            )
        if allowed_writer_id is not None:
            writer = writer.where(integration_branch_owners.c.id != allowed_writer_id)
        promotion = select(integration_promotion_intents.c.id).where(
            (integration_promotion_intents.c.operation_key == operation_id)
            | (integration_promotion_intents.c.resolution_operation_id == operation_id),
            integration_promotion_intents.c.state.not_in(("committed", "conflict", "superseded")),
        )
        if allowed_promotion_intent_id is not None:
            promotion = promotion.where(
                integration_promotion_intents.c.id != allowed_promotion_intent_id
            )
        statements = {
            "ref_mutation": select(integration_candidate_ref_mutations.c.id).where(
                integration_candidate_ref_mutations.c.operation_id == operation_id,
                integration_candidate_ref_mutations.c.state == "reserved",
            ),
            "resolution": select(integration_candidate_resolutions.c.id).where(
                integration_candidate_resolutions.c.operation_id == operation_id,
                integration_candidate_resolutions.c.state.in_(("reserved", "pushed")),
            ),
            "attestation": select(integration_attestation_publications.c.id).where(
                integration_attestation_publications.c.operation_id == operation_id,
                integration_attestation_publications.c.state == "reserved",
            ),
            "promotion": promotion,
            "writer": writer,
        }
        if operation["batch_id"] is not None:
            statements["candidate_publication"] = select(
                integration_candidate_publications.c.batch_id
            ).where(
                integration_candidate_publications.c.batch_id == operation["batch_id"],
                integration_candidate_publications.c.state != "pr_published",
            )
            if allow_reserved_delegate:
                # pr_reserved follows a confirmed ref write. Audit-PR metadata
                # may remain pending without making that branch write uncertain.
                pub = integration_candidate_publications.c
                mutation = integration_candidate_ref_mutations.c
                applied_ref = select(mutation.id).where(
                    mutation.purpose == "candidate_final",
                    mutation.state == "applied",
                    mutation.operation_id == operation_id,
                    mutation.operation_episode_id == operation["episode_id"],
                    mutation.batch_id == pub.batch_id,
                    mutation.revision == pub.revision,
                    mutation.repository_id == pub.repository_id,
                    mutation.target_branch == "refs/heads/" + pub.head_ref,
                    mutation.branch == mutation.target_branch,
                    mutation.target_branch == select(integration_batches.c.integration_branch)
                        .where(integration_batches.c.id == operation["batch_id"]).scalar_subquery(),
                    mutation.expected_old_sha == pub.expected_old_sha,
                    mutation.desired_sha == pub.head_sha,
                    mutation.remote_sha == pub.head_sha,
                ).exists()
                statements["candidate_publication"] = statements["candidate_publication"].where(
                    ~and_(pub.state == "pr_reserved", applied_ref)
                )
            statements["cleanup_prewrite"] = select(
                integration_cleanup_items.c.domain_key
            ).where(
                integration_cleanup_items.c.batch_id == operation["batch_id"],
                integration_cleanup_items.c.irreversible_prewrite_at.is_not(None),
                integration_cleanup_items.c.state.in_(("pending", "retryable")),
            )
        blockers = []
        for kind, statement in statements.items():
            value = (await conn.execute(statement.limit(1))).scalar_one_or_none()
            if value is not None:
                # A pushed repair is deliberately not collapsed into an
                # anonymous generic write.  It is frozen external evidence,
                # and the public recovery refusal must tell the operator that
                # it needs exact lineage acceptance rather than a retry that
                # could create a competing writer.
                blockers.append(
                    f"resolution:{value}"
                    if kind == "resolution"
                    else kind
                )
        return sorted(blockers)
