"""Accept root delegates' handoffs without entering the legacy publisher."""

from __future__ import annotations

import logging
import os

from sqlalchemy import select, update

from src.commands.principal import PrincipalKind, current_principal, matches_session_instance
from src.database import tables as t
from src.database.queries.hierarchy_queries import HierarchyError
from src.integration.engine import EngineRefused, RootEngineOwnership
from src.integration.hierarchy import resolve_repair_commit_proof
from src.integration.repair import RepairService, repair_subject_sha
from src.integration.subjects import Subject, SubjectPhase
from src.models import TaskStatus

logger = logging.getLogger(__name__)


def _ref_forms(name):
    """Owner rows store full refs; tasks store bare branch names. Match both."""
    bare = (name or "").removeprefix("refs/heads/")
    return [bare, f"refs/heads/{bare}"]


async def reconciler_batch_subject(db, batch_id, *, include_done=False):
    """Read durable engine ownership; terminal roots still fence legacy calls."""
    statement = select(t.integration_subjects).where(
        t.integration_subjects.c.batch_id == batch_id,
        t.integration_subjects.c.kind == "root_batch",
        t.integration_subjects.c.engine == "reconciler",
    )
    if not include_done:
        statement = statement.where(t.integration_subjects.c.phase != "done")
    async with db._engine.connect() as conn:
        row = (await conn.execute(statement)).mappings().one_or_none()
    return Subject.from_row(row) if row else None


async def close_reconciler_delegate(
    orchestrator,
    task,
    scope,
    *,
    session_id,
    claim_epoch,
    outcome,
    commit,
    accepted_close,
    skip_open_subtasks=False,
):
    """Return None only for a legacy scope; owned refusals must not fall through."""
    db, git = orchestrator.db, orchestrator.git
    async with db._engine.connect() as conn:
        operation = (
            (
                await conn.execute(
                    select(t.integration_repair_operations).where(
                        t.integration_repair_operations.c.id == scope["operation_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
    subject = await reconciler_batch_subject(db, operation["batch_id"])

    def refused(detail):
        return {
            "status": task.status.value,
            "pr_url": None,
            "pipeline_ok": False,
            "retry_count": None,
            "verification_retry": True,
            "issues": [detail],
            "feedback": detail,
        }

    if subject is None:
        # A promoted batch closes its own subject, so there is no live
        # reconciler subject left to answer for this writer and no in-flight
        # handoff to prove. Delivery on main is the only proof left that can
        # accept the close; an unproved scope returns None and keeps today's
        # refusal rather than gaining a permissive one.
        try:
            return await close_delivered_reconciler_delegate(
                orchestrator,
                task,
                scope,
                operation,
                session_id=session_id,
                claim_epoch=claim_epoch,
                outcome=outcome,
                commit=commit,
                accepted_close=accepted_close,
                skip_open_subtasks=skip_open_subtasks,
            )
        except (EngineRefused, HierarchyError, ValueError) as exc:
            return refused(str(exc))

    try:
        async with RootEngineOwnership(db).operation(subject.repository_id, subject=subject):
            return await _close_on_owner(
                db,
                git,
                task,
                scope,
                subject,
                session_id=session_id,
                claim_epoch=claim_epoch,
                outcome=outcome,
                commit=commit,
                accepted_close=accepted_close,
                skip_open_subtasks=skip_open_subtasks,
                now=(getattr(orchestrator, "repair_service", None) or RepairService(db)).clock(),
            )
    except (EngineRefused, HierarchyError, ValueError) as exc:
        return refused(str(exc))


async def _held_claim(conn, task, *, session_id, claim_epoch):
    """The exact session, task and workspace rows this close may release."""
    async def row(table, *where):
        return (
            (await conn.execute(select(table).where(*where).with_for_update()))
            .mappings()
            .one_or_none()
        )

    session = await row(t.sessions, t.sessions.c.id == session_id)
    held = await row(t.tasks, t.tasks.c.id == task.id)
    workspace = await row(t.workspaces, t.workspaces.c.locked_by_task_id == task.id)
    principal = current_principal()
    if (
        session is None
        or workspace is None
        or principal is None
        or principal.kind != PrincipalKind.SESSION
        or principal.session_id != session_id
        or not matches_session_instance(principal, session["instance_token"])
        or session["task_id"] != task.id
        or session["project_id"] != task.project_id
        or session["last_claim_epoch"] != claim_epoch
        or held["claim_epoch"] != claim_epoch
        or session["claim_phase"] != "active"
        or session["state"] not in {"starting", "running", "draining"}
        or held["status"] not in {"ASSIGNED", "IN_PROGRESS"}
        or workspace["locked_by_agent_id"] != session["agent_id"]
        or workspace["workspace_path"] != session["work_dir"]
        or workspace["project_id"] != task.project_id
        or held["assigned_agent_id"] != session["agent_id"]
        or not workspace["enabled"]
    ):
        raise ValueError("Repair close requires the original live claim and workspace")
    return session, held, workspace


async def _close_on_owner(
    db,
    git,
    task,
    scope,
    subject,
    *,
    session_id,
    claim_epoch,
    outcome,
    commit,
    accepted_close,
    skip_open_subtasks,
    now,
):
    # Keep the ownership checks and release atomic. No remote write occurs here.
    async with db.immediate() as conn:
        await db.lock_hierarchy_project(conn, task.project_id)
        current = await db.lock_integration_subject_on(conn, subject.id)
        if current["version"] != subject.version or current["engine"] != "reconciler":
            raise ValueError("Repair subject changed during close")

        async def row(table, *where):
            return (
                (await conn.execute(select(table).where(*where).with_for_update()))
                .mappings()
                .one_or_none()
            )

        session, _, workspace = await _held_claim(
            conn, task, session_id=session_id, claim_epoch=claim_epoch
        )
        owner = await row(
            t.integration_branch_owners,
            t.integration_branch_owners.c.repository_id == task.repo_id,
            t.integration_branch_owners.c.ref.in_(_ref_forms(task.branch_name)),
        )
        fresh = await db.get_repair_filing_scope(task.id, session_id=session_id, conn=conn)
        attached = (
            fresh is not None
            and fresh["active"]
            and all(
                fresh[key] == scope[key]
                for key in (
                    "operation_id",
                    "stage",
                    "session_id",
                    "instance_token",
                    "workspace_id",
                    "fence_token",
                )
            )
        )
        handoffs = (
            (
                await conn.execute(
                    select(t.integration_candidate_resolutions).where(
                        t.integration_candidate_resolutions.c.repair_task_id == task.id,
                        t.integration_candidate_resolutions.c.batch_id == subject.batch_id,
                        t.integration_candidate_resolutions.c.state == "accepted",
                    )
                )
            )
            .mappings()
            .all()
        )
        handoff = next(
            (
                r
                for r in handoffs
                if r["operation_id"] == scope["operation_id"]
                and r["stage_ordinal"] == scope["stage"]
                and r["repair_session_id"] == session_id
                and r["repair_session_instance_token"] == session["instance_token"]
                and r["repair_workspace_id"] == workspace["id"]
                and (r["push_evidence"] or {}).get("remote_sha") == r["resolved_head_sha"]
                and session["claim_phase_at"] is not None
                and session["claim_phase_at"] <= r["created_at"]
                and owner is not None
                and owner["owner_id"] == r["handoff_owner_id"]
                and owner["fence_token"] == r["handoff_fence_token"]
                and owner["session_id"] is None
                and owner["workspace_id"] is None
            ),
            None,
        )
        if not attached and handoff is None:
            raise ValueError("Repair writer fence changed; close is stale")
        if current["writer_task_id"] not in {None, task.id}:
            raise ValueError("Repair subject already has a successor writer")
        path = workspace["workspace_path"]
        if await git._arun(["status", "--porcelain"], cwd=path):
            raise ValueError("Repair workspace has uncommitted changes; preserve them before close")
        head = await git._arun(["rev-parse", "HEAD"], cwd=path)
        if commit and head != commit:
            raise ValueError("Repair close commit differs from workspace HEAD")
        if handoff is not None:
            if head != handoff["resolved_head_sha"]:
                raise ValueError("Repair workspace differs from accepted handoff")
        else:
            from src.git.manager import RemoteRefState

            remote = await git.als_remote_ref(path, task.branch_name.removeprefix("refs/heads/"))
            if remote.state is not RemoteRefState.PRESENT or remote.oid != head:
                raise ValueError("Repair HEAD must be exactly pushed before close")
            proof = await resolve_repair_commit_proof(
                git,
                path,
                base_sha=repair_subject_sha(fresh["current_subject"]),
                head_sha=head,
            )
            # Even a failed delegate may have useful pushed work; preserve that
            # revision and let the next policy visit decide CI or another writer.
            await RepairService(db).adopt_batch_repair_on(
                conn,
                scope["operation_id"],
                head_sha=head,
                commit_proof=proof,
                now=now,
            )
        await git._arun(["switch", "--detach", head], cwd=path)
        if await git._arun(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path) != "HEAD":
            raise ValueError("Repair checkout did not detach")
        status = TaskStatus.COMPLETED if outcome == "pass" else TaskStatus.BLOCKED
        transition = await db._apply_transition(
            conn,
            task.id,
            status,
            context="integration_reconciler_repair_closed",
            assigned_agent_id=None,
            expect_claim_epoch=claim_epoch,
            accepted_close=accepted_close,
            skip_open_subtasks=skip_open_subtasks,
        )
        if attached:
            await conn.execute(
                update(t.integration_branch_owners)
                .where(
                    t.integration_branch_owners.c.id == owner["id"],
                )
                .values(
                    handoff_state="released",
                    session_id=None,
                    workspace_id=None,
                    confirmed_workspace_id=workspace["id"],
                    updated_at=now,
                )
            )
        await conn.execute(
            update(t.workspaces)
            .where(
                t.workspaces.c.id == workspace["id"],
            )
            .values(
                locked_by_task_id=None,
                locked_by_agent_id=session["agent_id"] if session["lifecycle"] == "pool" else None,
                locked_at=workspace["locked_at"] if session["lifecycle"] == "pool" else None,
            )
        )
        proof = {
            "stop_proof": {
                "kind": "accepted_handoff",
                "confirmed_at": now,
                "task_id": task.id,
                "session_id": session_id,
                "instance_token": session["instance_token"],
                "claim_epoch": claim_epoch,
                "head_sha": head,
                "fence_token": scope["fence_token"],
                "outcome": outcome,
            }
        }
        stage = await row(
            t.integration_repair_stages,
            t.integration_repair_stages.c.operation_id == scope["operation_id"],
            t.integration_repair_stages.c.ordinal == scope["stage"],
        )
        await conn.execute(
            update(t.integration_repair_stages)
            .where(
                t.integration_repair_stages.c.operation_id == scope["operation_id"],
                t.integration_repair_stages.c.ordinal == scope["stage"],
            )
            .values(
                dossier={
                    **(stage["dossier"] or {}),
                    "reconciler_delegate_completion": proof["stop_proof"],
                }
            )
        )
        await db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            now=now,
            values={
                "writer_task_id": task.id,
                "writer_session_id": session_id,
                "writer_status": "stopped",
                "writer_stop_proof": proof,
                "next_due_at": now,
                "due_set_at": now,
            },
        )
    await db.log_blocked_flips(transition.flipped)
    await db._notify_settled(transition.settled)
    await db._notify_ready(transition.ready)
    return {
        "status": status.value,
        "pr_url": None,
        "pipeline_ok": True,
        "retry_count": None,
        "completion_source": head,
        "preserve_terminal_task": True,
    }


async def close_delivered_reconciler_delegate(
    orchestrator,
    task,
    scope,
    operation,
    *,
    session_id,
    claim_epoch,
    outcome,
    commit,
    accepted_close,
    skip_open_subtasks=False,
):
    """Accept a close whose batch reached main before this delegate finished.

    Promotion closes the batch's root subject and returns the integration
    branch to the collector, so the in-flight handoff proofs a reconciler
    close used to rely on are gone by the time a slow delegate calls it.
    Delivery replaces them: this delegate's own accepted resolution, the
    batch's committed promotion, and that head's reachability from the branch
    main holds now.

    All of it is durable and exact, and the same live-claim guard still has to
    pass, so this answers a proven delivery rather than relaxing the close.
    Anything short of the whole proof returns ``None`` and leaves the existing
    refusal in charge.
    """
    db, git = orchestrator.db, orchestrator.git
    proof = await _promoted_delivery_on(db, task, scope, operation)
    if proof is None or not await _delivered_on_main(orchestrator, proof):
        logger.info(
            "integration: no proven delivery for %s batch=%s; the ordinary "
            "repair-close refusal stands",
            task.id,
            operation["batch_id"],
        )
        return None

    now = (getattr(orchestrator, "repair_service", None) or RepairService(db)).clock()
    async with db.immediate() as conn:
        await db.lock_hierarchy_project(conn, task.project_id)
        # The pre-read chose which proof to demand; it did not grant it.
        if await _promoted_delivery_on(db, task, scope, operation, conn=conn) != proof:
            return None

        async def row(table, *where):
            return (
                (await conn.execute(select(table).where(*where).with_for_update()))
                .mappings()
                .one_or_none()
            )

        session, _, workspace = await _held_claim(
            conn, task, session_id=session_id, claim_epoch=claim_epoch
        )
        stage = await row(
            t.integration_repair_stages,
            t.integration_repair_stages.c.operation_id == scope["operation_id"],
            t.integration_repair_stages.c.ordinal == scope["stage"],
        )
        if stage is None:
            raise ValueError("Repair stage no longer exists; close is stale")
        path = workspace["workspace_path"]
        if await git._arun(["status", "--porcelain"], cwd=path):
            raise ValueError("Repair workspace has uncommitted changes; preserve them before close")
        head = await git._arun(["rev-parse", "HEAD"], cwd=path)
        if head != proof["head_sha"]:
            raise ValueError("Repair workspace differs from its delivered head")
        if commit and head != commit:
            raise ValueError("Repair close commit differs from workspace HEAD")
        await git._arun(["switch", "--detach", head], cwd=path)
        if await git._arun(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path) != "HEAD":
            raise ValueError("Repair checkout did not detach")
        status = TaskStatus.COMPLETED if outcome == "pass" else TaskStatus.BLOCKED
        transition = await db._apply_transition(
            conn,
            task.id,
            status,
            context="integration_reconciler_repair_delivered",
            assigned_agent_id=None,
            expect_claim_epoch=claim_epoch,
            accepted_close=accepted_close,
            skip_open_subtasks=skip_open_subtasks,
        )
        await conn.execute(
            update(t.workspaces)
            .where(
                t.workspaces.c.id == workspace["id"],
            )
            .values(
                locked_by_task_id=None,
                locked_by_agent_id=session["agent_id"] if session["lifecycle"] == "pool" else None,
                locked_at=workspace["locked_at"] if session["lifecycle"] == "pool" else None,
            )
        )
        # A done subject refuses writer writes, so the stop record of a delivery
        # the reconciler already closed belongs to the delegate's repair stage.
        receipt = {
            "kind": "promoted_delivery",
            "confirmed_at": now,
            "task_id": task.id,
            "session_id": session_id,
            "instance_token": session["instance_token"],
            "claim_epoch": claim_epoch,
            "workspace_id": workspace["id"],
            "head_sha": proof["head_sha"],
            "promoted_head_sha": proof["promoted_head_sha"],
            "promotion_intent_id": proof["promotion_intent_id"],
            "batch_id": proof["batch_id"],
            "operation_id": scope["operation_id"],
            "stage": scope["stage"],
            "outcome": outcome,
        }
        await conn.execute(
            update(t.integration_repair_stages)
            .where(
                t.integration_repair_stages.c.operation_id == scope["operation_id"],
                t.integration_repair_stages.c.ordinal == scope["stage"],
            )
            .values(
                dossier={
                    **(stage["dossier"] or {}),
                    "reconciler_delivered_completion": receipt,
                }
            )
        )
    await db.log_blocked_flips(transition.flipped)
    await db._notify_settled(transition.settled)
    await db._notify_ready(transition.ready)
    return {
        "status": status.value,
        "pr_url": None,
        "pipeline_ok": True,
        "retry_count": None,
        "completion_source": proof["head_sha"],
        "preserve_terminal_task": True,
    }


async def _promoted_delivery_on(db, task, scope, operation, *, conn=None):
    """This delegate's exact promotion-delivery proof, or None when it lacks one."""
    async def read(connection, *, lock):
        def one(table, *where):
            statement = select(table).where(*where)
            return statement.with_for_update() if lock else statement

        async def rows(table, *where):
            return (await connection.execute(one(table, *where))).mappings().all()

        subjects = await rows(
            t.integration_subjects,
            t.integration_subjects.c.batch_id == operation["batch_id"],
            t.integration_subjects.c.kind == "root_batch",
            t.integration_subjects.c.engine == "reconciler",
        )
        if len(subjects) != 1:
            return None
        subject = subjects[0]
        if Subject.from_row(subject).phase is not SubjectPhase.DONE:
            return None
        batches = await rows(
            t.integration_batches,
            t.integration_batches.c.id == operation["batch_id"],
        )
        batch = batches[0] if batches else None
        if (
            batch is None
            or batch["project_id"] != task.project_id
            or subject["repository_id"] != task.repo_id
            or batch["repository_id"] != subject["repository_id"]
            or batch["lifecycle"] != "promoted"
            or not batch["final_main_sha"]
            or not batch["integration_branch"]
        ):
            return None
        accepted = await rows(
            t.integration_candidate_resolutions,
            t.integration_candidate_resolutions.c.batch_id == batch["id"],
            t.integration_candidate_resolutions.c.repair_task_id == task.id,
            t.integration_candidate_resolutions.c.operation_id == scope["operation_id"],
            t.integration_candidate_resolutions.c.stage_ordinal == scope["stage"],
            t.integration_candidate_resolutions.c.state == "accepted",
        )
        if len(accepted) != 1:
            return None
        resolution = accepted[0]
        if (
            resolution["operation_episode_id"] != operation["episode_id"]
            or resolution["project_id"] != task.project_id
            or resolution["repository_id"] != subject["repository_id"]
            or resolution["fence_owner_id"] != task.id
            or resolution["handoff_owner_id"] != operation["id"]
            or resolution["handoff_fence_token"] != resolution["fence_token"] + 1
            or str(resolution["branch"]).removeprefix("refs/heads/")
            != str(batch["integration_branch"]).removeprefix("refs/heads/")
            or (resolution["push_evidence"] or {}).get("remote_sha")
            != resolution["resolved_head_sha"]
            or int(resolution["revision"]) != int(batch["current_revision"])
        ):
            return None
        promoted = batch["final_main_sha"]
        revisions = await rows(
            t.integration_candidate_revisions,
            t.integration_candidate_revisions.c.batch_id == batch["id"],
            t.integration_candidate_revisions.c.revision == batch["current_revision"],
        )
        revision = revisions[0] if revisions else None
        if (
            revision is None
            or revision["state"] != "promoted"
            or revision["head_sha"] != promoted
        ):
            return None
        intents = await rows(
            t.integration_promotion_intents,
            t.integration_promotion_intents.c.intent_kind == "root",
            t.integration_promotion_intents.c.operation_key == operation["id"],
            t.integration_promotion_intents.c.root_batch_id == batch["id"],
            t.integration_promotion_intents.c.root_candidate_revision
            == batch["current_revision"],
            t.integration_promotion_intents.c.prepared_sha == promoted,
            t.integration_promotion_intents.c.ci_evidence_id == revision["ci_evidence_id"],
            t.integration_promotion_intents.c.state == "committed",
        )
        if len(intents) != 1:
            return None
        intent = intents[0]
        if (
            intent["project_id"] != task.project_id
            or intent["repository_id"] != subject["repository_id"]
            or not str(intent["target_branch"]).startswith("refs/heads/")
        ):
            return None
        return {
            "batch_id": batch["id"],
            "repository_id": subject["repository_id"],
            "head_sha": resolution["resolved_head_sha"],
            "promoted_head_sha": promoted,
            "promotion_intent_id": intent["id"],
            "target_branch": intent["target_branch"],
        }

    if conn is not None:
        return await read(conn, lock=True)
    async with db._engine.connect() as owned:
        return await read(owned, lock=False)


async def _delivered_on_main(orchestrator, proof):
    """Is the delivered head an ancestor of the commit main was given?

    The store is the retained repository the promotion pushed that commit from,
    so it holds both objects and no fetch is needed. Main only moves by another
    promotion, and each one re-proves a fast-forward from the tip it read, so
    ancestry against the promoted head is ancestry against main. A rewritten
    main cannot be proven from here and simply leaves the refusal standing.
    """
    promotion = getattr(orchestrator, "root_promotion_service", None)
    if promotion is None:
        return False
    store = str(promotion._store(proof["repository_id"]))
    if not os.path.isdir(store):
        logger.info(
            "integration: no retained repository store at %s; delivery unproved", store
        )
        return False
    async with orchestrator.git.arepository_transaction(store):
        return await orchestrator.git.ais_ancestor(
            store, proof["head_sha"], proof["promoted_head_sha"], strict=True
        ) is True
