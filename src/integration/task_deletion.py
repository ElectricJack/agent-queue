"""Preview and execute the operator's unified task deletion decision.

State holds describe consequences; they do not veto an operator delete. Audit
and historical integration identities survive independently of active tasks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextvars import ContextVar
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert

from src.claim_file import remove_claim_file_if_matches
from src.database import tables as t
from src.database.queries.task_references import find_integration_task_references
from src.git.manager import GitError, repository_urls_match
from src.sessions.provider import SessionHandle

deletion_actor: ContextVar[str | None] = ContextVar("deletion_actor", default=None)


def warning(code, message):
    return {"code": code, "message": message}


async def _rows(conn, table, *conditions):
    return [dict(row) for row in (await conn.execute(select(table).where(*conditions))).mappings()]


async def _related(conn, table, ids, *columns):
    return await _rows(conn, table, or_(*(table.c[key].in_(ids) for key in columns or ("id",))))


async def _column(conn, column, *conditions):
    return list((await conn.execute(select(column).where(*conditions))).scalars())


async def _update_ids(conn, table, ids, *conditions, key="id", **values):
    await conn.execute(update(table).where(table.c[key].in_(ids), *conditions).values(**values))


async def _lock_deletion_rows(conn, ids):
    # Claim release and pool termination lock session -> task -> agent. Both
    # deletion transactions must follow that order, including after external
    # cleanup: the reconciler may still be releasing a stopped session then.
    await conn.execute(
        select(t.sessions.c.id)
        .where(t.sessions.c.task_id.in_(ids))
        .order_by(t.sessions.c.id)
        .with_for_update()
    )
    await conn.execute(
        select(t.tasks.c.id)
        .where(t.tasks.c.id.in_(ids))
        .order_by(t.tasks.c.id)
        .with_for_update()
    )


async def _audit(db, conn, event, root, **payload):
    return await db.log_event(
        event, project_id=root["project_id"], task_id=root["id"],
        payload=json.dumps(payload),
        conn=conn,
    )


async def deletion_plan(
    db, orchestrator, task_id, *, recursive=False, branches="keep", stop_running=False
):
    """Read without changing state, including Git tips used by the audit."""
    async with db._engine.connect() as conn:
        root = (await _rows(conn, t.tasks, t.tasks.c.id == task_id))[0]
        ids = await db.subtree_ids(task_id, conn=conn) if recursive else [task_id]
        snapshots = await _related(conn, t.tasks, ids)
        sessions = await _related(conn, t.sessions, ids, "task_id")
        workspaces = await _rows(conn, t.workspaces, t.workspaces.c.project_id == root["project_id"])
        repositories = await _rows(conn, t.repos, t.repos.c.project_id == root["project_id"])
        origins = await _related(conn, t.task_branch_origins, ids, "task_id")
        checkpoints = await _related(conn, t.task_integration_checkpoints, ids, "task_id")
        owners = await _rows(
            conn, t.integration_branch_owners,
            or_(
                t.integration_branch_owners.c.owner_id.in_(ids),
                t.integration_branch_owners.c.session_id.in_([row["id"] for row in sessions]),
                t.integration_branch_owners.c.workspace_id.in_(
                    [row["id"] for row in workspaces if row["locked_by_task_id"] in ids]
                ),
            ),
        )
        children = await _rows(
            conn, t.tasks, t.tasks.c.parent_task_id.in_(ids) & t.tasks.c.id.not_in(ids)
        )
        edges = await _related(conn, t.task_dependencies, ids, "task_id", "depends_on_task_id")
        gate_ids = await _column(conn, t.task_gates.c.gate_id, t.task_gates.c.task_id.in_(ids))
        reviews = await _related(conn, t.doc_reviews, ids, "author_task_id")
        gate_ids += [row["gate_id"] for row in reviews if row["gate_id"]]
        gates = await _rows(
            conn, t.gates,
            or_(
                t.gates.c.id.in_(gate_ids),
                (t.gates.c.gate_type == "task") & t.gates.c.await_id.in_(ids),
            ),
        )
        escalations = await _related(conn, t.escalations, ids, "task_id")
        scope = set(ids)
        ancestor = root["parent_task_id"]
        while ancestor and ancestor not in scope:
            scope.add(ancestor)
            ancestor = await conn.scalar(
                select(t.tasks.c.parent_task_id).where(t.tasks.c.id == ancestor)
            )
        members = t.integration_batch_members
        batch_ids = await _column(conn, members.c.batch_id, members.c.task_id.in_(scope))
        batches = await _related(conn, t.integration_batches, batch_ids)
        operation = t.integration_repair_operations
        operations = await _rows(
            conn, operation,
            or_(
                operation.c.parent_task_id.in_(ids),
                operation.c.verifier_task_id.in_(ids),
                operation.c.batch_id.in_(batch_ids),
                operation.c.id.in_(
                    select(t.integration_repair_stages.c.operation_id).where(
                        t.integration_repair_stages.c.repair_task_id.in_(ids)
                    )
                ),
                operation.c.id.in_(
                    select(t.integration_candidate_resolutions.c.operation_id).where(
                        t.integration_candidate_resolutions.c.repair_task_id.in_(ids)
                    )
                ),
            ),
        )
        subjects = await _rows(
            conn, t.integration_subjects,
            or_(
                t.integration_subjects.c.task_id.in_(ids),
                t.integration_subjects.c.writer_task_id.in_(ids),
                t.integration_subjects.c.batch_id.in_(batch_ids),
            ),
        )
        receipts = await _related(
            conn, t.task_delivery_receipts, ids, "source_task_id", "target_task_id"
        )
        history = await find_integration_task_references(conn, ids)
        pause_metadata = await _rows(
            conn, t.task_metadata,
            t.task_metadata.c.task_id.in_(ids) & (t.task_metadata.c.key == "manual_pause"),
        )
        from src.integration.development import OPEN_OPERATION_STATES, operation_rows_on

        development = [
            row
            for row in await operation_rows_on(
                conn, [root["project_id"]], states=OPEN_OPERATION_STATES
            )
            if set(ids) & {m.get("task_id") for m in row.get("manifest", [])}
        ]
        project_path = await conn.scalar(
            select(t.projects.c.workspace_path).where(t.projects.c.id == root["project_id"])
        )

    notices = []
    running = [
        s for s in sessions if s["state"] in {"starting", "running", "draining"} or s["claim_phase"]
    ]
    if running or any(row["assigned_agent_id"] for row in snapshots):
        notices.append(
            warning("running_work", "Selected sessions and claims will stop and be released.")
        )
    if children:
        notices.append(
            warning(
                "has_children", "Unselected children will survive under the deleted task's parent."
            )
        )
    dependents = sorted({e["task_id"] for e in edges if e["task_id"] not in ids})
    if dependents:
        notices.append(
            warning("dependents", f"Dependency edges will be dropped for: {', '.join(dependents)}.")
        )
    for code, rows, message in (
        ("gates", gates,
            "Selected decision gates will be withdrawn; other waiters may need attention.",
        ),
        ("reviews", reviews, "Open authored reviews will be withdrawn."),
        ("escalations", escalations, "Open task escalations will be cancelled."),
        ("sealed", batches, "Affected unpromoted integration batches will be aborted."),
        ("integration_owned", operations + subjects + development,
            "Live integration authority will be cancelled.",
        ),
        ("integration_cleanup_blocked", owners,
            "Branch owners and writer leases will be released.",
        ),
        ("delivery_target_fixed", receipts,
            "Delivery records will remain as history after the task is deleted.",
        ),
        ("integration_history_retained", origins + checkpoints + history,
            "Integration history will remain with the deletion audit.",
        ),
        ("pause_cleanup_pending", pause_metadata,
            "Any pending pause cleanup will be retried during deletion.",
        ),
    ):
        if rows:
            notices.append(warning(code, message))

    plan = {
        "success": True,
        "deleted": None,
        "title": root["title"],
        "task_ids": ids,
        "disposition": "preview",
        "recursive": recursive,
        "branches": branches,
        "stop_running": bool(stop_running),
        "tasks": [
            {"id": row["id"], "title": row["title"], "status": row["status"]} for row in snapshots
        ],
        "reparented_tasks": [row["id"] for row in children],
        "sessions": [
            {"session_id": s["id"], "task_id": s["task_id"], "state": s["state"]} for s in running
        ],
        "dependents": dependents,
        "gate_ids": [row["id"] for row in gates],
        "review_ids": [row["id"] for row in reviews],
        "escalation_ids": [row["id"] for row in escalations],
        "batch_ids": sorted(set(batch_ids)),
        "operation_ids": [row["id"] for row in operations],
        "branch_details": [],
        "warnings": notices,
    }
    captured = {
        "root": root,
        "tasks": snapshots,
        "sessions": sessions,
        "workspaces": workspaces,
        "repositories": repositories,
        "origins": origins,
        "checkpoints": checkpoints,
        "owners": owners,
        "subjects": subjects,
        "development": development,
    }
    await _branch_plan(orchestrator, plan, captured, project_path)
    return plan, captured


async def _branch_plan(orchestrator, plan, captured, project_path):
    """Only inspect checkouts bound to the branch's repository."""
    git = orchestrator.git
    repositories = {row["id"]: row for row in captured["repositories"]}
    found = {}
    detached_worktrees = {}

    def add(task_id, repository_id, branch):
        if not branch:
            return
        branch = branch.removeprefix("refs/heads/")
        if repository_id is None and len(repositories) == 1:
            repository_id = next(iter(repositories))
        key = (repository_id, branch)
        entry = found.setdefault(
            key,
            {
                "task_ids": [], "repository_id": repository_id,
                "branch": branch, "local_refs": [],
                "remote_sha": None, "remote_state": "unknown",
                "worktrees": [], "action": plan["branches"],
            },
        )
        if task_id not in entry["task_ids"]:
            entry["task_ids"].append(task_id)

    for row in captured["tasks"]:
        add(row["id"], row["repo_id"], row["branch_name"])
    for row in captured["origins"]:
        add(row["task_id"], row["repository_id"], row["branch_name"])
    for row in captured["checkpoints"]:
        add(row["task_id"], row["repository_id"], row["branch"])
    for row in captured["owners"]:
        add(row["owner_id"], row["repository_id"], row["ref"])

    owned_paths = {
        row["workspace_path"]: row["locked_by_task_id"]
        for row in captured["workspaces"]
        if row["locked_by_task_id"] in plan["task_ids"]
    }
    owned_paths.update(
        {
            row["work_dir"]: row["task_id"]
            for row in captured["sessions"]
            if row["work_dir"]
            and (row["state"] in {"starting", "running", "draining"} or row["claim_phase"])
        }
    )
    stores = {}
    for repository_id, repo in repositories.items():
        retained = (
            Path(orchestrator.config.data_dir)
            / "integration-repositories"
            / (hashlib.sha256(repository_id.encode()).hexdigest() + ".git")
        )
        candidates = [repo["checkout_base_path"], repo["source_path"], project_path, str(retained)]
        candidates += [row["workspace_path"] for row in captured["workspaces"]]
        candidates += list(owned_paths)
        paths = set()
        for candidate in dict.fromkeys(candidates):
            if not candidate or not Path(candidate).exists():
                continue
            try:
                common = await git.aworktree_base_path(candidate)
                if not common:
                    continue
                url = await git.aget_remote_url(common)
                if repo["url"] and (
                    not url or not repository_urls_match(url, repo["url"], base=common)
                ):
                    continue
                paths.add(str(Path(common).resolve()))
            except Exception as exc:
                plan["warnings"].append(warning("branch_inspection", f"{candidate}: {exc}"))
        stores[repository_id] = sorted(paths)
        for path in sorted(paths):
            try:
                for checkout in await git.aworktree_list(path):
                    owner = owned_paths.get(checkout["path"])
                    if owner:
                        branch = checkout.get("branch")
                        if not branch:
                            task = next(row for row in captured["tasks"] if row["id"] == owner)
                            branch = task["branch_name"] or f"aq/{owner}"
                            detached_worktrees.setdefault((repository_id, branch), []).append(
                                checkout
                            )
                        add(owner, repository_id, branch)
                names = await git.alist_branches(path)
                for task_id in plan["task_ids"]:
                    recovery = {f"aq/{task_id}", f"aq/{task_id}-wip"}
                    for name in names:
                        if name in recovery:
                            add(task_id, repository_id, name)
                    if await git.ahas_remote(path):
                        remote_refs = await git.als_remote_refs(
                            path, sorted(recovery), repository_url=repo["url"]
                        )
                        for name, remote in remote_refs.items():
                            if remote.oid:
                                add(task_id, repository_id, name)
                            elif remote.error:
                                plan["warnings"].append(warning("remote_inspection", remote.error))
            except Exception as exc:
                plan["warnings"].append(warning("branch_inspection", f"{path}: {exc}"))
    for (repository_id, branch), entry in found.items():
        entry["worktrees"] += detached_worktrees.get((repository_id, branch), [])
        repo = repositories.get(repository_id)
        if repo is None:
            plan["warnings"].append(
                warning("branch_repository_unknown", f"No repository found for {branch}.")
            )
            continue
        if branch == repo["default_branch"]:
            entry["action"] = "keep"
            plan["warnings"].append(
                warning("default_branch", f"Repository default branch {branch} is retained.")
            )
        for path in stores[repository_id]:
            try:
                sha = await git.arev_parse(path, f"refs/heads/{branch}")
                tracking = await git.arev_parse(path, f"refs/remotes/origin/{branch}")
                if sha or tracking:
                    entry["local_refs"].append({"path": path, "sha": sha, "tracking_sha": tracking})
                entry["worktrees"] += [
                    w
                    for w in await git.aworktree_list(path)
                    if w.get("branch") == branch and w not in entry["worktrees"]
                ]
                if entry["remote_state"] == "unknown" and await git.ahas_remote(path):
                    remote = await git.als_remote_ref(path, branch, repository_url=repo["url"])
                    entry["remote_state"] = remote.state.value
                    entry["remote_sha"] = remote.oid
                    entry["remote_path"] = path
                    if remote.error:
                        plan["warnings"].append(
                            warning("remote_inspection", f"{branch}: {remote.error}")
                        )
            except Exception as exc:
                plan["warnings"].append(warning("branch_inspection", f"{branch} in {path}: {exc}"))
        if not stores[repository_id]:
            plan["warnings"].append(
                warning("branch_checkout_unavailable", f"No checkout available for {branch}.")
            )
    plan["branch_details"] = list(found.values())


async def execute_deletion(db, orchestrator, plan, captured, *, actor, reason=None):
    """Fence first, stop exact processes, clean Git, then drop active identities."""
    ids = plan["task_ids"]
    root = captured["root"]
    now = time.time()
    reason = (reason or "").strip()
    from src.waits.graph import lock_component_tasks, lock_graph

    async with db.immediate() as conn:
        # Fence wait writers first, then preserve termination's session -> task
        # row order before locking the remaining wait component.
        await lock_graph(conn)
        await db.lock_hierarchy_project(conn, root["project_id"])
        await _lock_deletion_rows(conn, ids)
        await lock_component_tasks(conn, {f"task:{task_id}" for task_id in ids})
        captured["sessions"] = await _rows(conn, t.sessions, t.sessions.c.task_id.in_(ids))
        # These holds are internal to the confirmed delete and prevent fresh claims.
        await _update_ids(
            conn, t.tasks, ids,
            status="PAUSED", resume_after=None,
            claim_epoch=t.tasks.c.claim_epoch + 1,
            updated_at=now,
        )
        await _update_ids(
            conn, t.sessions, ids, key="task_id",
            desired_state="stopped", end_reason="task deleted by operator",
        )
        await _cancel_authority(db, conn, plan, captured, actor, reason, now)
        audit_id = await _audit(
            db, conn, "task.delete_requested", root,
            actor=actor,
            reason=reason,
            preview=plan,
            task_snapshots=captured["tasks"],
        )

    results = []

    async def cleanup(label, action):
        try:
            await action()
            results.append({"resource": label, "outcome": "complete"})
            return True
        except Exception as exc:
            plan["warnings"].append(warning("cleanup_failed", f"{label}: {exc}"))
            results.append({"resource": label, "outcome": "failed", "error": str(exc)})
            return False

    failed_sessions = []
    for session in captured["sessions"]:

        async def stop(session=session):
            token_store = getattr(orchestrator, "token_store", None)
            if token_store is not None:
                await token_store.revoke_session(session["id"])
            provider = orchestrator.session_providers.create(session["provider"], orchestrator.config)
            await provider.stop(
                SessionHandle(session["name"], session["provider"], session["instance_token"]),
                grace=2.0,
            )

        if session["state"] in {"starting", "running", "draining"} or session["claim_phase"]:
            if not await cleanup(f"session:{session['id']}", stop):
                failed_sessions.append(session["id"])
    for task_id in ids:
        adapter = getattr(orchestrator, "_adapters", {}).get(
            next((r["assigned_agent_id"] for r in captured["tasks"] if r["id"] == task_id), None)
        )
        if adapter is not None:
            await cleanup(f"adapter:{task_id}", adapter.stop)
        background = getattr(orchestrator, "_running_tasks", {}).get(task_id)
        if background and not background.done() and background is not asyncio.current_task():
            background.cancel()
            _, pending = await asyncio.wait({background}, timeout=5)
            if pending:
                plan["warnings"].append(
                    warning("cleanup_failed", f"Execution for {task_id} did not settle.")
                )

    for session in captured["sessions"]:
        task = next((row for row in captured["tasks"] if row["id"] == session["task_id"]), None)
        if task and session["work_dir"]:
            await cleanup(
                f"claim file:{session['id']}",
                lambda session=session, task=task: asyncio.to_thread(
                    remove_claim_file_if_matches,
                    session["work_dir"], task["id"], task["claim_epoch"],
                ),
            )
    if plan["branches"] == "delete":
        await _delete_branches(orchestrator, plan, cleanup)

    async with db.immediate() as conn:
        await lock_graph(conn)
        await db.lock_hierarchy_project(conn, root["project_id"])
        await _lock_deletion_rows(conn, ids)
        await lock_component_tasks(conn, {f"task:{task_id}" for task_id in ids})
        affected = await db._collect_affected(set(ids), conn)
        affected -= set(ids)
        parent_id = root["parent_task_id"]
        if parent_id:
            affected.add(parent_id)
        layout_parents = await db._layout_parent_ids(ids, conn=conn)
        # Also preserve children added while external cleanup was running.
        children = await _rows(
            conn, t.tasks, t.tasks.c.parent_task_id.in_(ids) & t.tasks.c.id.not_in(ids)
        )
        for child in children:
            parent_id = root["parent_task_id"]
            await conn.execute(
                update(t.tasks)
                .where(t.tasks.c.id == child["id"])
                .values(parent_task_id=parent_id, updated_at=time.time())
            )
            if parent_id:
                await conn.execute(
                    insert(t.task_dependencies)
                    .values(
                        task_id=child["id"],
                        depends_on_task_id=parent_id,
                        dep_type="parent-child",
                    )
                    .on_conflict_do_nothing()
                )
            if child["id"] not in plan["reparented_tasks"]:
                plan["reparented_tasks"].append(child["id"])
                plan["warnings"].append(
                    warning("scope_changed", f"New child {child['id']} was retained.")
                )
            affected.add(child["id"])
        await _update_ids(
            conn, t.agents, ids, key="current_task_id", current_task_id=None, state="idle"
        )
        removed_worktrees = [
            row["resource"].removeprefix("worktree:")
            for row in results
            if row["resource"].startswith("worktree:")
            and row["outcome"] == "complete"
            and not Path(row["resource"].removeprefix("worktree:")).exists()
        ]
        if removed_worktrees:
            from src.workspace_disable import WORKTREE_REMOVED, disable_fields

            await _update_ids(
                conn, t.workspaces, removed_worktrees, key="workspace_path",
                **disable_fields(WORKTREE_REMOVED, f"deleting task {root['id']} removed it"),
            )
        await _update_ids(
            conn, t.sessions, ids, key="task_id",
            task_id=None, state="stopped", desired_state="stopped",
            claim_phase=None, claim_phase_at=None,
            ended_at=time.time(),
            last_claim_result="task_deleted",
        )
        if failed_sessions:
            await _update_ids(conn, t.sessions, failed_sessions, state="draining", ended_at=None)
        await db.mark_layout_dirty(
            root["project_id"], [*ids, *affected, *layout_parents], "task.deleted", conn=conn
        )
        for task_id in reversed(ids):
            await db._delete_one(
                task_id, conn=conn, preserve_comments=True, preserve_completion=True,
                preserve_subtasks=True, operator_delete=True,
            )
        flipped = await db.recompute_blocked(affected, conn=conn) if affected else set()
        ready = [
            (tid, "unblocked")
            for tid in await db._note_frontier_entry(conn, flipped, reason="unblocked")
        ]
        settled = []
        if parent_id:

            async def settle_parent():
                # A settlement failure cannot roll back the confirmed deletion.
                async with conn.begin_nested():
                    stale = await db.release_stale_container_claim(parent_id, conn=conn)
                    settlement = await db.settle_containers({parent_id}, conn=conn)
                flipped.update(stale.flipped | settlement.flipped)
                ready.extend([*stale.ready, *settlement.ready])
                settled.extend(settlement.settled)

            await cleanup(f"parent settlement:{parent_id}", settle_parent)
        plan.update(
            deleted=root["id"],
            disposition="deleted",
            audit_id=audit_id,
            cleanup_results=results,
            reason=reason,
        )
        captured["deletion_event_id"] = await _audit(
            db, conn, "task.deleted", root, **plan, actor=actor
        )
    for label, action in (
        ("blocked notifications", lambda: db.log_blocked_flips(flipped)),
        ("settled notifications", lambda: db._notify_settled(settled)),
        ("ready notifications", lambda: db._notify_ready(ready)),
    ):
        await cleanup(label, action)
    await cleanup(
        "cleanup audit",
        lambda: _audit(db, None, "task.delete_cleanup", root, **plan, actor=actor),
    )
    return plan


async def _cancel_authority(db, conn, plan, captured, actor, reason, now):
    ids = plan["task_ids"]
    await conn.execute(
        update(t.integration_branch_owners)
        .where(
            or_(
                t.integration_branch_owners.c.owner_id.in_(ids),
                t.integration_branch_owners.c.id.in_([row["id"] for row in captured["owners"]]),
                t.integration_branch_owners.c.session_id.in_(
                    [s["id"] for s in captured["sessions"]]
                ),
            )
        )
        .values(
            handoff_state="released", holder=None, expires_at=now,
            fence_token=t.integration_branch_owners.c.fence_token + 1,
            updated_at=now,
        )
    )
    await _update_ids(
        conn, t.task_branch_origins, ids, key="task_id",
        retired_at=now, discard_state=None, discard_next_attempt_at=None,
    )
    await _update_ids(
        conn, t.integration_batches, plan["batch_ids"],
        t.integration_batches.c.lifecycle.not_in(["promoted", "empty"]),
        intent="aborted", lifecycle="aborted",
        human_abort_reason=reason or "task deleted",
        cleanup_state="pending", updated_at=now,
    )
    await _update_ids(
        conn, t.integration_repair_operations, plan["operation_ids"],
        t.integration_repair_operations.c.state.in_(["active", "escalated", "human_required"]),
        state="cancelled", updated_at=now,
    )
    await _update_ids(
        conn, t.integration_subjects, [s["id"] for s in captured["subjects"]],
        phase="done", next_due_at=None, gate_id=None,
        closed_reason=reason or "task deleted",
        writer_task_id=None, writer_session_id=None, writer_fence_token=None,
        writer_status="none", wait_reason=None,
        version=t.integration_subjects.c.version + 1,
        updated_at=now,
    )
    await _update_ids(
        conn, t.doc_reviews, plan["review_ids"],
        t.doc_reviews.c.state.in_(["in_review", "changes_requested"]),
        state="withdrawn",
        withdrawn_by=actor, withdrawn_at=now, withdrawn_via="task_delete",
        withdrawal_reason=reason, updated_at=now,
    )
    await _update_ids(
        conn, t.gates, plan["gate_ids"],
        t.gates.c.status == "open",
        status="cancelled", resolved_by=actor,
        resolution=reason or "task deleted",
    )
    await _update_ids(
        conn, t.escalations, plan["escalation_ids"],
        t.escalations.c.state.in_(["needs_human", "reply_received", "resolving"]),
        state="cancelled", terminal_at=now,
        terminal_outcome=reason or "Task deleted",
        outcome="human", updated_at=now,
        revision=t.escalations.c.revision + 1,
    )
    await _update_ids(
        conn, t.agent_questions, ids,
        t.agent_questions.c.state.in_(["supervisor", "human", "answered"]),
        key="task_id",
        state="stale", reason="task deleted", updated_at=now,
    )
    from src.integration.development import revise_operation_on

    for operation in captured["development"]:
        await revise_operation_on(
            conn, operation["id"],
            lambda row: {
                "state": "cancelled",
                "error": reason or "task deleted by operator",
            },
        )


async def _delete_branches(orchestrator, plan, cleanup):
    git = orchestrator.git
    for entry in plan["branch_details"]:
        if entry["action"] != "delete":
            continue
        branch = entry["branch"]
        if entry["remote_sha"]:
            await cleanup(
                f"remote branch:{branch}",
                lambda entry=entry: git.adelete_remote_ref_exact(
                    entry["remote_path"], entry["branch"], entry["remote_sha"],
                ),
            )
        elif entry["remote_state"] in {"unknown", "error"}:
            plan["warnings"].append(
                warning("branch_cleanup_incomplete", f"Remote {branch} was not verified absent.")
            )
        for checkout in entry["worktrees"]:
            path = checkout["path"]

            async def remove_checkout(path=path):
                base = await git.aworktree_base_path(path)
                if not base:
                    raise GitError("Cannot locate worktree repository")
                if Path(base).resolve() == Path(path).resolve():
                    result = await git.arun_git_result(["checkout", "--detach"], cwd=path)
                    if result.returncode:
                        raise GitError(result.stderr or result.stdout or "Cannot detach checkout")
                else:
                    await git.aremove_worktree(base, path)

            await cleanup(f"worktree:{path}", remove_checkout)
        for local in entry["local_refs"]:
            if local["sha"]:
                await cleanup(
                    f"local branch:{branch}@{local['path']}",
                    lambda local=local, branch=branch: git.adelete_local_ref_exact(
                        local["path"], ref=f"refs/heads/{branch}",
                        expected_old_oid=local["sha"],
                    ),
                )
            if local["tracking_sha"]:

                async def delete_tracking(local=local, branch=branch):
                    ref = f"refs/remotes/origin/{branch}"
                    # A successful push-delete can already remove this tracking ref.
                    if await git.arev_parse(local["path"], ref) is not None:
                        await git.adelete_remote_tracking_ref_exact(
                            local["path"], ref=ref,
                            expected_old_oid=local["tracking_sha"],
                        )

                await cleanup(f"tracking branch:{branch}@{local['path']}", delete_tracking)
