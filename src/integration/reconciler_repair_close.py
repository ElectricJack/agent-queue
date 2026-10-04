"""Accept root delegates' handoffs without entering the legacy publisher."""

from __future__ import annotations

from sqlalchemy import select, update

from src.commands.principal import PrincipalKind, current_principal, matches_session_instance
from src.database import tables as t
from src.database.queries.hierarchy_queries import HierarchyError
from src.integration.engine import EngineRefused, RootEngineOwnership
from src.integration.hierarchy import resolve_repair_commit_proof
from src.integration.repair import RepairService, repair_subject_sha
from src.integration.subjects import Subject
from src.models import TaskStatus


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
    if subject is None:
        return None

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

        session = await row(t.sessions, t.sessions.c.id == session_id)
        held = await row(t.tasks, t.tasks.c.id == task.id)
        workspace = await row(t.workspaces, t.workspaces.c.locked_by_task_id == task.id)
        principal = current_principal()
        if (
            session is not None
            and held is not None
            and session["lifecycle"] != "pool"
            and claim_epoch is None
        ):
            # Task-lifecycle sessions predate explicit claim epochs.  Their
            # live claim is still fenced by the locked task row; pool sessions
            # carry the epoch in both the request and session row.
            claim_epoch = held["claim_epoch"]
        if (
            session is None
            or held is None
            or workspace is None
            or principal is None
            or principal.kind != PrincipalKind.SESSION
            or principal.session_id != session_id
            or not matches_session_instance(principal, session["instance_token"])
            or session["task_id"] != task.id
            or session["project_id"] != task.project_id
            or (
                session["lifecycle"] == "pool"
                and (claim_epoch is None or session["last_claim_epoch"] != claim_epoch)
            )
            or (
                session["lifecycle"] != "pool"
                and session["last_claim_epoch"] not in {None, claim_epoch}
            )
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
        owner = await row(
            t.integration_branch_owners,
            t.integration_branch_owners.c.repository_id == task.repo_id,
            t.integration_branch_owners.c.ref == task.branch_name,
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

            repair = RepairService(db)
            binding = await repair.bind_current_batch_subject_on(
                conn, scope["operation_id"], now=now
            )
            fresh = {**fresh, "current_subject": binding["subject"]}
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
            await repair.adopt_batch_repair_on(
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
        current_subject = stage["current_subject"]
        from src.integration.outbox import enqueue_integration_event

        event_id = (
            f"repair-delegate-closed-{scope['operation_id']}-{scope['stage']}-{task.id}"
            f"-{scope['fence_token']}-{session_id}"
        )
        await enqueue_integration_event(
            conn,
            event_id=event_id,
            dedup_key=(
                f"repair-delegate-closed:{scope['operation_id']}:{scope['stage']}:{task.id}"
                f":{scope['fence_token']}:{session_id}"
            ),
            project_id=task.project_id,
            event_type="integration.repair_delegate_closed",
            payload={
                "operation_id": scope["operation_id"],
                "stage": scope["stage"],
                "task_id": task.id,
                "session_id": session_id,
                "instance_token": session["instance_token"],
                "workspace_id": workspace["id"],
                "fence_token": scope["fence_token"],
                "batch_id": subject.batch_id,
                "revision": current_subject["revision"],
                "head_sha": current_subject["candidate_sha"],
            },
            available_at=now,
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
