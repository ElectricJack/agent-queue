"""Durable suspension intent and stop/save/park protocol.

The database owns the intent; external effects are retried from persisted steps.
Only CommandHandler service callbacks drive this state machine. No rollout flag
is changed here. A stopped process alone never authorizes releasing its checkout.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

from sqlalchemy import func, insert, select, update

from src.claim_file import remove_claim_file_if_matches
from src.database.queries.continuation_queries import degrade_checkpoint
from src.database.tables import continuations, sessions, tasks, wait_deadlines, wait_records
from src.sessions.provider import SessionHandle
from src.waits.checkpoint import collect_checkpoint_facts
from src.waits.conditions import WaitError
from src.waits.graph import lock_graph
from src.waits.sources import execute_task_status_mutation


async def begin_suspension(conn, wait, task, *, now, budget):
    """Called under graph/task/wait locks, before exposing a checkpoint receipt."""
    existing = (await conn.execute(select(continuations).where(
        continuations.c.task_id == task["id"],
        continuations.c.resume_state.not_in(("acknowledged", "failed", "cancelled")),
    ).with_for_update())).mappings().first()
    if existing:
        if existing["wait_id"] != wait["id"]:
            raise WaitError("wait.active_exists", "another continuation owns this task")
        return dict(existing)
    if not task["branch_name"]:
        raise WaitError("wait.unavailable", "hibernation requires a task branch")
    generation = 1 + (await conn.scalar(select(func.max(
        continuations.c.suspension_generation,
    )).where(continuations.c.task_id == task["id"])) or 0)
    delivery = (wait["decision_inputs"] or {}).get("delivery", {})
    values = dict(
        id=str(uuid.uuid4()), task_id=task["id"], wait_id=wait["id"],
        suspension_generation=generation, facts={}, branch=task["branch_name"],
        prior_status=task["status"], held_cursor_seq=delivery["held_cursor_seq"],
        resume_state="sleeping", created_at=now,
    )
    row = (await conn.execute(insert(continuations).values(**values).returning(
        continuations,
    ))).mappings().one()
    deadline_id = str(uuid.uuid4())
    await conn.execute(insert(wait_deadlines).values(
        id=deadline_id, wait_id=wait["id"], deadline_type="transition",
        due_at=now + budget, generation=generation,
    ))
    await conn.execute(select(func.pg_notify("aq_wait_deadline", deadline_id)))
    await execute_task_status_mutation(conn, update(tasks).where(
        tasks.c.id == task["id"], tasks.c.status == "IN_PROGRESS",
    ).values(status="WAITING", updated_at=now))
    return dict(row)


async def locked_suspension(conn, wait_id):
    """Global graph then task, wait, continuation; never invert dispatch's locks."""
    await lock_graph(conn)
    initial = (await conn.execute(select(wait_records).where(
        wait_records.c.id == wait_id,
    ))).mappings().first()
    if not initial or initial["owner_kind"] != "task":
        raise WaitError("wait.out_of_scope", "suspension requires a task wait")
    task = (await conn.execute(select(tasks).where(
        tasks.c.id == initial["owner_id"],
    ).with_for_update())).mappings().first()
    wait = (await conn.execute(select(wait_records).where(
        wait_records.c.id == wait_id,
    ).with_for_update())).mappings().one()
    intent = (await conn.execute(select(continuations).where(
        continuations.c.wait_id == wait_id,
        continuations.c.resume_state.not_in(("acknowledged", "failed", "cancelled")),
    ).with_for_update())).mappings().first()
    if not task or not intent or task["project_id"] != wait["project_id"]:
        raise WaitError("continuation.stale_generation", "suspension owner disappeared")
    return dict(task), dict(wait), dict(intent)


class HibernationTransaction:
    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.orch = service.orch
        self.db = self.orch.db
        self.clock = clock

    async def hold(self, conn, task, wait, intent, reason, handler, *, detail=""):
        """One incident per generation/reason; no claim/workspace release."""
        result = await handler.wait_escalation_create(conn, {
            "project_id": task["project_id"], "task_id": task["id"],
            "task_title": task["title"], "source_kind": "wait",
            "source_identity": f"continuation:{intent['id']}:{reason}",
            "incident_key": f"continuation:{intent['id']}:{reason}",
            "summary": f"Hibernation held: {reason}",
            "investigation": detail or f"Wait {wait['id']}, continuation {intent['id']}",
            "decision_requested": "Repair the infrastructure hold; preserve the task checkout.",
            "severity": "high",
        })
        if not result.get("success"):
            raise WaitError("wait.unavailable", "operational escalation could not be recorded")
        await conn.execute(update(continuations).where(
            continuations.c.id == intent["id"], continuations.c.version == intent["version"],
        ).values(resume_state="held", hold_reason=reason, version=intent["version"] + 1,
                 facts={**intent["facts"], "hold_escalation_id":
                        result.get("escalation", {}).get("id")}))
        return {"success": True, "held": True, "hold_reason": reason}

    async def advance(self, wait_id, *, handler):
        """One bounded attempt. Subsequent service ticks recover incomplete steps.

        Save after the exact writer is stopped: save_wip_to_task_branch explicitly
        requires that proof. This also captures shell edits after checkpoint. The
        claim/epoch/token/folder remain owned until both stop AND push succeed.
        """
        if not self.orch.config.wait_policy.hibernation_enabled:
            raise WaitError("wait.disabled", "hibernation is disabled")
        async with self.db._engine.connect() as conn:
            task_id = await conn.scalar(select(wait_records.c.owner_id).where(
                wait_records.c.id == wait_id,
            ))
        if task_id is None:
            raise WaitError("wait.source_unresolved", "wait disappeared")
        async with self.orch._task_control_lock(task_id):
            return await self._advance_locked(wait_id, handler=handler)

    async def _advance_locked(self, wait_id, *, handler):
        now = self.clock()
        policy = self.orch.config.wait_policy
        async with self.db._engine.connect() as conn:
            parked = (await conn.execute(select(continuations).where(
                continuations.c.wait_id == wait_id, continuations.c.stopped_at.is_not(None),
                continuations.c.resume_state.in_(("sleeping", "queued", "held")),
            ))).mappings().first()
        if parked:
            return await self.cleanup(dict(parked))
        async with self.db._engine.begin() as conn:
            task, wait, intent = await locked_suspension(conn, wait_id)
            if intent["stopped_at"] is not None:
                return {"success": True, "parked": True}
            if task["status"] == "PAUSED":
                await conn.execute(update(continuations).where(
                    continuations.c.id == intent["id"],
                ).values(resume_state="held", hold_reason="paused",
                         version=intent["version"] + 1))
                return {"success": True, "held": True, "hold_reason": "paused"}
            if task["status"] not in {"WAITING", "IN_PROGRESS"}:
                raise WaitError("continuation.stale_generation", "task is no longer suspendable")
            if intent["resume_state"] == "held":
                return {"success": True, "held": True, "hold_reason": intent["hold_reason"]}
            delivery = wait["decision_inputs"].get("delivery", {})
            session = (await conn.execute(select(sessions).where(
                sessions.c.id == wait["session_id"],
                sessions.c.instance_token == delivery.get("instance_token"),
                sessions.c.task_id == task["id"],
                sessions.c.agent_id == task["assigned_agent_id"],
            ))).mappings().first()
            if (not session or task["claim_epoch"] != wait["claim_epoch"]
                    or task["branch_name"] != intent["branch"]):
                raise WaitError("continuation.stale_generation", "writer identity changed")
            if intent["summary"] is None and not intent["summary_degraded"]:
                if now < intent["created_at"] + policy.transition_budget_seconds:
                    return {"success": True, "next_step": "checkpoint_then_end_turn"}
                current_task = await self.db.get_task(task["id"])
                current_session = await self.db.get_session(session["id"])
                facts = await collect_checkpoint_facts(self.db, current_task, current_session)
                await degrade_checkpoint(
                    conn, task_id=task["id"], wait_id=wait_id, continuation_id=intent["id"],
                    generation=intent["suspension_generation"], now=now,
                    transition_budget_seconds=policy.transition_budget_seconds, facts=facts,
                )
                intent = dict((await conn.execute(select(continuations).where(
                    continuations.c.id == intent["id"],
                ))).mappings().one())
            # Record intent BEFORE signaling. A crash cannot authorize an in-place
            # delivery after this point. External stop/save effects are idempotent.
            facts = dict(intent["facts"] or {})
            state = dict(facts.get("hibernation", {}))
            state.setdefault("stop_started_at", now)
            facts["hibernation"] = state
            await conn.execute(update(continuations).where(
                continuations.c.id == intent["id"],
            ).values(facts=facts, version=continuations.c.version + 1))
            session = dict(session)
        provider = self.orch.session_providers.create(session["provider"], self.orch.config)
        handle = SessionHandle(session["name"], session["provider"], session["instance_token"])
        try:
            remaining = policy.transition_budget_seconds - max(0, now - state["stop_started_at"])
            async with asyncio.timeout(max(0, remaining)):
                await provider.stop(handle)
                record = await self.db.get_session(session["id"])
                reconciler = self.orch.session_reconciler
                while True:
                    stopped = (await provider.confirm_instance_stopped(handle)
                               and not await provider.process_alive(
                                   handle, reconciler._process_names(record),
                               ))
                    if stopped:
                        break
                    await asyncio.sleep(2)
        except (TimeoutError, OSError, RuntimeError):
            stopped = False
        if not stopped:
            async with self.db._engine.begin() as conn:
                task, wait, intent = await locked_suspension(conn, wait_id)
                return await self.hold(conn, task, wait, intent, "stop_uncertain", handler)
        from src.git.manager import commit_identity
        from src.orchestrator.stranded_work import save_wip_to_task_branch

        project = await self.db.get_project(task["project_id"])
        repo_id = task["repo_id"] or project.integration_repository_id
        repository = await self.db.get_repo(repo_id) if repo_id else None
        if repository is None:
            async with self.db._engine.begin() as conn:
                task, wait, intent = await locked_suspension(conn, wait_id)
                return await self.hold(conn, task, wait, intent, "push_failed", handler,
                                       detail="Task repository binding is unavailable")
        if intent["saved_head"]:
            saved = SimpleNamespace(status="pushed", commit=intent["saved_head"], error=None)
        else:
            with commit_identity(self.orch.git.resolve_commit_identity(project)):
                saved = await save_wip_to_task_branch(
                    self.orch.git, session["work_dir"], intent["branch"], f"hibernate:{wait_id}",
                    repository_url=repository.url, event_bus=self.orch.bus,
                    project_id=task["project_id"],
                )
            if saved.status in {"clean", "pushed"} and saved.commit:
                async with self.db._engine.begin() as conn:
                    _, _, current = await locked_suspension(conn, wait_id)
                    await conn.execute(update(continuations).where(
                        continuations.c.id == current["id"],
                    ).values(saved_head=saved.commit, version=current["version"] + 1))
        workspace = await self.db.get_workspace_for_task(task["id"])
        detached = False
        if saved.status in {"clean", "pushed"} and saved.commit and workspace:
            from src.orchestrator.workspace_attachments import (
                detach_slot_for_integration_handoff, detach_workspace_for_integration_handoff,
            )

            if workspace.workspace_path == session["work_dir"]:
                kwargs = dict(expected_branch=intent["branch"], repository_url=repository.url,
                              default_branch=repository.default_branch)
                if workspace.is_slot:
                    detached = await detach_slot_for_integration_handoff(
                        self.db, self.orch.git, self.orch._git_mutex, workspace, **kwargs,
                    )
                else:
                    detached = await detach_workspace_for_integration_handoff(
                        self.orch.git, self.orch._git_mutex, workspace, **kwargs,
                    )
        async with self.db._engine.begin() as conn:
            task, wait, intent = await locked_suspension(conn, wait_id)
            if saved.status not in {"clean", "pushed"} or not saved.commit:
                facts = {**intent["facts"], "local_head": saved.commit}
                await conn.execute(update(continuations).where(
                    continuations.c.id == intent["id"],
                ).values(facts=facts))
                return await self.hold(conn, task, wait, {**intent, "facts": facts},
                                       "push_failed", handler,
                                       detail=saved.error or "No published head proof")
            if not detached:
                return await self.hold(conn, task, wait, intent, "detach_uncertain", handler)
            await self.db.park_task_for_continuation(
                conn, task_id=task["id"], wait_id=wait_id, continuation_id=intent["id"],
                generation=intent["suspension_generation"], session_id=session["id"],
                instance_token=session["instance_token"], claim_epoch=wait["claim_epoch"],
                saved_head=saved.commit, now=self.clock(),
                detached_workspace_id=workspace.id,
            )
        return await self.cleanup(intent)

    async def cleanup(self, intent):
        """Retry cleanup after a crash between parking and releasing the folder."""
        async with self.db._engine.connect() as conn:
            wait = (await conn.execute(select(wait_records).where(
                wait_records.c.id == intent["wait_id"],
            ))).mappings().one()
        session = await self.db.get_session(wait["session_id"])
        if (session is None or session.state != "stopped" or session.end_reason != "hibernated"
                or session.instance_token != wait["decision_inputs"]["delivery"]["instance_token"]):
            raise WaitError("continuation.stop_uncertain", "retired writer binding changed")
        await self.orch.token_store.revoke_session(session.id)
        await asyncio.to_thread(remove_claim_file_if_matches, session.work_dir,
                                intent["task_id"], wait["claim_epoch"])
        workspace = await self.db.get_workspace_for_task(intent["task_id"])
        if workspace:
            if workspace.workspace_path != session.work_dir:
                raise WaitError("continuation.stale_generation", "checkout ownership changed")
            await self.orch._release_workspace_and_cleanup(workspace)
        async with self.db._engine.begin() as conn:
            _, _, current = await locked_suspension(conn, wait["id"])
            facts = {**current["facts"], "workspace_released_at": self.clock(),
                     "costs": {**current['facts'].get('costs', {}),
                               'teardown_seconds': max(0, self.clock() - current['facts'].get(
                                   'hibernation', {}).get('stop_started_at', current['created_at']))}}
            await conn.execute(update(continuations).where(
                continuations.c.id == current["id"],
            ).values(facts=facts, version=current["version"] + 1))
            from src.waits.measurements import update_sample

            await update_sample(conn, wait, {**current, 'facts': facts})
        return {"success": True, "parked": True, "continuation_id": intent["id"]}

    async def transition(self, conn, deadline, wait, *, now, handler):
        """Deadline records readiness only; stop/Git I/O occurs outside its locks."""
        return {"success": True, "wait_id": wait["id"], "transition_due": True}
