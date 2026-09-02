"""``aq task claim`` — pull-based work selection (swarm-work-model §10)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from src.models import ClaimResult, TaskStatus

logger = logging.getLogger(__name__)

CLAIM_FILE = os.path.join(".aq", "claim.json")
_ADMISSION_EVENTS = ("project.resumed", "constraint.released", "snapshot.refreshed")
_FRONTIER_EVENTS = ("task.ready", "gate.resolved", "task.restarted")


def write_claim_file(work_dir: str, payload: dict) -> str:
    path = os.path.join(work_dir, CLAIM_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    return path


def _task_block(task) -> dict:
    """The claimed task's own row, field-for-field with ``task_show``'s core.

    Same key names and value shapes as ``_cmd_get_task``'s scalar fields, so
    a caller reading ``result["task"]["status"]`` is unaffected by the §15
    trim — only the *joined* sections (``depends_on``, ``blocks``,
    ``subtasks``, ``children``, ``context``, ``labels``, ``parent``) are
    gone, and ``aq task show`` still has them.  ``claim_epoch`` is added:
    the claim protocol's fence lives on the row.
    """
    info = {
        "id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority,
        "assigned_agent": task.assigned_agent_id,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "integration_mode": task.integration_mode,
        "is_blocked": task.is_blocked,
        "is_plan_subtask": task.is_plan_subtask,
        "task_type": task.task_type.value if task.task_type else None,
        "parent_task_id": task.parent_task_id,
        "profile_id": task.profile_id,
        "intelligence_class": task.intelligence_class,
        "skip_verification": task.skip_verification,
        "workflow_id": task.workflow_id,
        "affinity_agent_id": task.affinity_agent_id,
        "affinity_reason": task.affinity_reason,
        "workspace_mode": task.workspace_mode.value if task.workspace_mode else None,
        "claim_epoch": task.claim_epoch,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
    if task.pr_url:
        info["pr_url"] = task.pr_url
    return info


def remove_claim_file(work_dir: str) -> None:
    try:
        os.remove(os.path.join(work_dir, CLAIM_FILE))
    except FileNotFoundError:
        pass


def remove_claim_file_if_matches(work_dir: str, task_id: str, claim_epoch: int | None) -> None:
    """Remove a claim file only when it still belongs to this claim.

    Pool workers can claim again between a terminal close and its delayed
    cleanup, so unconditional removal could erase the successor's fence.
    """
    path = os.path.join(work_dir, CLAIM_FILE)
    try:
        with open(path) as f:
            claim = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return
    if claim.get("task_id") != task_id or claim.get("claim_epoch") != claim_epoch:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


class ClaimCommandsMixin:
    """Mixed into CommandHandler.  Needs ``self.db``, ``self.orchestrator``, ``self.config``."""

    # -- fence ---------------------------------------------------------------

    def _assert_task_in_scope(self, task) -> dict | None:
        """``None`` when *task* is readable by the scoped session; else an error.

        A pool worker's token carries ``task_id=None`` (its task changes
        with every claim), so ``check_command_scope``'s ``task_id`` pin is
        vacuous for it -- without this the read commands would happily
        serve any task in any project.  ``project_id`` is the fence that
        *is* meaningful on such a token, so a plain (non-elevated) session
        scope with no task pinned may only read tasks in its own project.

        Local callers, elevated supervisor tokens, and session scopes that
        *do* pin a ``task_id`` (already enforced by
        ``check_command_scope``) are unaffected.
        """
        scope = getattr(self, "_current_scope", None) or {}
        if scope.get("kind") != "session" or scope.get("elevated"):
            return None
        if scope.get("task_id") is not None:
            return None
        project_id = scope.get("project_id")
        if project_id is None or task is None or task.project_id == project_id:
            return None
        return {
            "success": False,
            "result": ClaimResult.OUT_OF_SCOPE.value,
            "error": (
                f"task '{task.id}' belongs to project '{task.project_id}', "
                f"outside this session's scope ('{project_id}')"
            ),
        }

    async def _assert_session_owns(self, task_id, *, session_id, claim_epoch) -> dict | None:
        """``None`` when the caller holds *task_id*; else an error dict.

        Rules (swarm-work-model §10): no session in scope → ``None``
        (local/elevated callers are not fenced); ``sessions.task_id !=
        task_id`` → ``out_of_scope``; ``claim_epoch`` given and mismatched
        → ``stale_claim``; ``claim_epoch`` absent — pool sessions →
        ``stale_claim`` (they must read it from ``.aq/claim.json``), task
        sessions → accepted (legacy).
        """
        if not session_id:
            return None
        session = await self.db.get_session(session_id)
        if session is None:
            return {
                "success": False,
                "result": ClaimResult.OUT_OF_SCOPE.value,
                "error": f"No session '{session_id}'",
            }
        if session.task_id != task_id:
            return {
                "success": False,
                "result": ClaimResult.OUT_OF_SCOPE.value,
                "error": f"session {session_id} does not hold task {task_id}",
            }
        task = await self.db.get_task(task_id)
        if task is None:
            return {"success": False, "error": f"No task '{task_id}'"}
        if claim_epoch is None:
            if session.lifecycle == "pool":
                return {
                    "success": False,
                    "result": ClaimResult.STALE_CLAIM.value,
                    "error": "claim_epoch is required for pool sessions "
                    "(read it from .aq/claim.json)",
                }
            return None
        if int(claim_epoch) != task.claim_epoch:
            return {
                "success": False,
                "result": ClaimResult.STALE_CLAIM.value,
                "error": f"claim epoch {claim_epoch} is not current for {task_id} "
                f"(current {task.claim_epoch}); the task is no longer yours",
            }
        return None

    # -- admission -----------------------------------------------------------

    def _admission_reason(self, project) -> str | None:
        if project is None or getattr(project.status, "value", project.status) != "ACTIVE":
            return "project_inactive"
        state = getattr(self.orchestrator, "_last_scheduler_state", None)
        if state is None:
            return None
        from src.scheduler import _is_scheduling_paused

        if _is_scheduling_paused(project.id, state.project_constraints):
            return "scheduling_paused"
        if state.global_budget is not None and state.global_tokens_used >= state.global_budget:
            return "budget_exhausted"
        limit = getattr(project, "budget_limit", None)
        if limit and state.project_token_usage.get(project.id, 0) >= limit:
            return "budget_exhausted"
        return None

    # -- the command -----------------------------------------------------------

    async def _cmd_task_claim(self, args: dict) -> dict:
        """Claim a ready task for the calling session (``aq task claim``)."""
        # M8: the command surface stays present when ``swarm.enabled`` is
        # false, but it does not hand out work -- otherwise the flag that
        # stops ``_reconcile_pools`` from launching pool workers would still
        # let an already-running one keep pulling tasks.
        if not getattr(self.config.swarm, "enabled", True):
            return {
                "success": False,
                "result": ClaimResult.NOT_ADMISSIBLE.value,
                "reason": "swarm_disabled",
                "task": None,
                "claim_epoch": None,
            }
        scope = self._current_scope or {}
        session_id = scope.get("session_id") or (
            args.get("session_id") if scope.get("elevated", True) else None
        )
        if not session_id:
            return {
                "success": False,
                "result": ClaimResult.OUT_OF_SCOPE.value,
                "error": "task_claim needs a session in scope",
            }
        # One statement for both (spec §15): the profile is only needed on
        # the pool path below, but reading it in the same join costs nothing.
        session, profile = await self.db.get_session_with_profile(session_id)
        if session is None or session.lifecycle not in ("pool", "task"):
            return {
                "success": False,
                "result": ClaimResult.OUT_OF_SCOPE.value,
                "error": "not a claimable session",
            }
        if scope.get("project_id") and scope["project_id"] != session.project_id:
            return {
                "success": False,
                "result": ClaimResult.OUT_OF_SCOPE.value,
                "error": "project_id mismatch",
            }
        # A pool profile can be converted back to task lifecycle while a
        # worker is finishing current work.  The conversion marks each live
        # worker stopped; reject a fresh claim before it can take new work.
        if session.lifecycle == "pool" and session.desired_state == "stopped":
            return self._simple(
                ClaimResult.DRAIN_REQUESTED, "pool is draining", session
            )
        want_id = args.get("task_id")
        if not want_id and not args.get("next"):
            return {"success": False, "error": "task_id or next=true is required"}
        wait = max(0, min(int(args.get("wait") or 0), int(self.config.swarm.claim_wait_max)))
        deadline = time.monotonic() + wait

        if session.lifecycle == "task":
            if session.task_id and want_id in (None, session.task_id):
                task = await self.db.get_task(session.task_id)
                return await self._claimed_response(task, task.claim_epoch, session)
            return {
                "success": False,
                "result": ClaimResult.OUT_OF_SCOPE.value,
                "error": "task sessions cannot claim other work",
            }

        cap = self._pool_context_claim_cap(profile)
        project = await self.db.get_project(session.project_id)
        default_profile = getattr(project, "default_profile_id", None)
        refresh_routing = False

        while True:
            if refresh_routing:
                # Long-poll wakes may follow profile/class edits. Keep the
                # session's frozen launch settings but refresh its requirements.
                profile = await self.db.get_profile(session.profile_id)
                cap = self._pool_context_claim_cap(profile)
                project = await self.db.get_project(session.project_id)
                default_profile = getattr(project, "default_profile_id", None)
            refresh_routing = True
            # Subscribe before checking admissibility (same discipline as
            # the frontier waiter below) — otherwise a ``project.resumed`` /
            # ``constraint.released`` / ``snapshot.refreshed`` landing
            # between the check and the subscribe is lost and this call
            # blocks for the full wait even though it could have woken up.
            admission_waiter = self.orchestrator.bus.waiter(_ADMISSION_EVENTS)
            try:
                reason = self._admission_reason(project)
                if reason:
                    if time.monotonic() >= deadline:
                        return {
                            "success": False,
                            "result": ClaimResult.NOT_ADMISSIBLE.value,
                            "reason": reason,
                            "task": None,
                            "claim_epoch": None,
                        }
                    await admission_waiter.wait(max(0.0, deadline - time.monotonic()))
                    project = await self.db.get_project(session.project_id)
                    continue
            finally:
                admission_waiter.close()

            waiter = self.orchestrator.bus.waiter(
                _FRONTIER_EVENTS, filter={"project_id": session.project_id}
            )
            try:
                # The sequence watermark only feeds the ``wait`` branch's
                # missed-``task.ready`` check below; a non-waiting claim
                # never reads it (spec §15).
                seq0 = await self.db.max_event_id() if wait else 0
                routing = self._pool_claim_routing(session, profile)
                outcome = await self._attempt_claim(session, want_id, cap, default_profile, routing=routing)
                result = outcome["result"]
                if (result == ClaimResult.SESSION_EXHAUSTED.value
                        and self.config.swarm.fresh_context_per_task):
                    # Also retire idle sessions that predate this policy.
                    await self.db.update_session(session.id, desired_state="stopped")
                if result == ClaimResult.NO_READY_WORK.value and wait:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return outcome
                    if await self.db.count_events_after(
                        seq0, event_type="task.ready", project_id=session.project_id
                    ):
                        await asyncio.sleep(0)
                        continue
                    if await waiter.wait(remaining) is None:
                        return outcome
                    continue
                if result == ClaimResult.CLAIM_IN_PROGRESS.value and wait:
                    settled = await self._await_attempt(
                        session.id, outcome.get("claim_epoch"), deadline
                    )
                    if settled is None:
                        return outcome
                    continue  # re-run: active → claimed; released → new attempt
                return outcome
            finally:
                waiter.close()

    def _pool_context_claim_cap(self, profile):
        # A reused global worker must not carry a previous task's conversation.
        # Same-task active/preparing claims remain idempotent in take_claim_slot.
        if self.config.swarm.fresh_context_per_task:
            return 1
        return getattr(profile, "max_claims_per_session", None)

    def _pool_claim_routing(
        self, session, profile
    ) -> tuple[str | None, str | None, str | None]:
        """Restrict claims to the recorded live session, not next-launch settings.

        A running pool cannot change model or reasoning class between claims.
        Unknown launch metadata cannot prove compatibility with a route. The
        option hash is read from the coordinator's reconciliation cache so a
        claim adds no catalog query to its transaction.
        """
        from src.agents.routing import task_agent_mismatch
        from src.models import Agent, Task

        live_class = session.intelligence_class or None
        worker = Agent(
            id=session.agent_id or session.id, name=session.name,
            profile_id=session.profile_id, harness=session.harness,
            model=session.model, intelligence_class=live_class,
        )
        classes = getattr(
            getattr(self.orchestrator, "session_spec_builder", None),
            "_intelligence_classes", None,
        )

        def matches(class_id):
            required_class = class_id or getattr(profile, "default_class", None)
            if required_class and (required_class != live_class or not session.model):
                return False
            if not required_class and getattr(profile, "model", None) and not session.model:
                return False
            task = Task(
                id="", project_id=session.project_id, title="", description="",
                profile_id=session.profile_id, intelligence_class=class_id,
            )
            return task_agent_mismatch(
                task, worker, task_profile=profile, agent_profile=profile,
                harness_registry=getattr(self.orchestrator, "harness_registry", None),
                intelligence_classes=classes,
            ) is None

        coordinator = getattr(self.orchestrator, "assignment_routing", None)
        catalog_hash = (
            coordinator.cached_options_hash(session.project_id) if coordinator else None
        )
        return (
            live_class if live_class and matches(live_class) else None,
            session.llm_provider or None,
            catalog_hash,
        )

    async def _attempt_claim(self, session, want_id, cap, default_profile, *, routing=None) -> dict:
        """Decide the outcome on one ``immediate()`` transaction, on *conn* only.

        Every read/write in here must take ``conn`` explicitly (never a
        bare ``self.db.*`` call opening its own connection) — SQLite's
        shared single-connection pool means a second implicit transaction
        would auto-commit and cut this one's ``BEGIN IMMEDIATE`` out from
        under it (see ``immediate()``'s docstring).  Anything that needs
        its own connection (``_claimed_response`` → ``task_show``, the
        slot-reset + activation in ``_prepare_and_activate``, the
        ``claim_conflict`` event's task lookup) is deferred until after the
        ``async with`` block closes.
        """
        now = time.time()
        # What to do once the transaction has committed — set inside the
        # block, acted on outside it.
        active_claim: tuple | None = None  # (task, epoch, row) — already active
        new_claim: tuple | None = None  # (row, task) — a fresh "slot" claim
        conflict_task_id: str | None = None  # a specific task_id held by someone else
        async with self.db.immediate() as conn:
            kind, row = await self.db.take_claim_slot(conn, session.id, now=now, cap=cap)
            if kind == "active":
                if want_id and want_id != row.task_id:
                    return self._simple(
                        ClaimResult.OUT_OF_SCOPE, "session already holds a task", row, cap
                    )
                task = await self.db._get_task_conn(row.task_id, conn=conn)
                if task is None:
                    # The held task row is gone (deleted out from under the
                    # session).  There is nothing to re-serve, and dropping
                    # through would raise ``AttributeError`` on
                    # ``task.claim_epoch``.
                    return self._simple(
                        ClaimResult.OUT_OF_SCOPE,
                        f"session holds task '{row.task_id}', which no longer exists",
                        row,
                        cap,
                    )
                active_claim = (task, task.claim_epoch, row)
            elif kind in ("preparing", "claiming"):
                epoch = None
                if row.task_id:
                    held = await self.db._get_task_conn(row.task_id, conn=conn)
                    epoch = held.claim_epoch if held else None
                out = self._simple(ClaimResult.CLAIM_IN_PROGRESS, kind, row, cap)
                out.update(
                    task_id=row.task_id,
                    claim_epoch=epoch,
                    claim_phase=row.claim_phase,
                    claim_phase_at=row.claim_phase_at,
                )
                return out
            elif kind == "session_exhausted":
                return self._simple(
                    ClaimResult.SESSION_EXHAUSTED, "max_claims_per_session reached", row, cap
                )
            elif kind == "drain_requested":
                return self._simple(ClaimResult.DRAIN_REQUESTED, "pool scaled down", row, cap)
            elif kind != "slot":
                return self._simple(ClaimResult.OUT_OF_SCOPE, kind, row, cap)
            else:
                tid = await self.db.select_ready_for_profile(
                    conn,
                    project_id=session.project_id,
                    profile_id=session.profile_id,
                    default_profile_id=default_profile,
                    agent_id=row.agent_id,
                    task_id=want_id,
                    enforce_routing=routing is not None,
                    intelligence_class=routing[0] if routing else None,
                    llm_provider=routing[1] if routing else None,
                    options_hash=routing[2] if routing else None,
                )
                task = None
                if tid is not None:
                    task = await self.db.take_task(conn, tid, agent_id=row.agent_id, now=now)
                if task is None:
                    await self.db.release_claim_slot(conn, session.id)
                    if want_id:
                        conflict_task_id = want_id
                else:
                    slot = await self.db.record_holder(
                        conn,
                        session_id=session.id,
                        task_id=tid,
                        agent_id=row.agent_id,
                        work_dir=row.work_dir,
                        now=now,
                        agent_reserved=True,
                    )
                    new_claim = (row, task, slot)

        if active_claim is not None:
            task, epoch, row = active_claim
            return await self._claimed_response(task, epoch, row, cap)
        if new_claim is not None:
            row, task, slot = new_claim
            # A concurrent ``claim_in_progress`` caller (``_await_attempt``)
            # can await this instead of polling once the row settles.
            key = (session.id, task.claim_epoch)
            self.orchestrator.claim_waiters[key] = asyncio.get_running_loop().create_future()
            try:
                return await self._prepare_and_activate(session, row, task, cap, slot=slot)
            finally:
                # Every ordinary exit already resolved and popped the future
                # via ``_resolve_claim_waiters``; this covers the paths that
                # don't -- an unexpected exception, and cancellation (the
                # caller's ``--wait`` deadline firing mid-prepare).  Leaving
                # a pending future in the dict would strand every
                # ``claim_in_progress`` poller on it until its own deadline.
                stale = self.orchestrator.claim_waiters.pop(key, None)
                if stale is not None and not stale.done():
                    stale.set_result("prepare_failed")
        if conflict_task_id is not None:
            conflict_task = await self.db.get_task(conflict_task_id)
            if conflict_task is not None:
                await self.orchestrator._emit_task_event(
                    "task.claim_conflict", conflict_task, session_id=session.id
                )
            return self._simple(ClaimResult.CLAIM_CONFLICT, "", row, cap)
        return self._simple(ClaimResult.NO_READY_WORK, "", row, cap)

    async def _prepare_and_activate(self, session, row, task, cap=None, *, slot=None) -> dict:
        async with self.orchestrator._task_control_lock(task.id):
            if not await self.db.claim_preparation_is_current(
                session.id, task.id, task.claim_epoch
            ):
                self._resolve_claim_waiters(session.id, task.claim_epoch, "prepare_failed")
                return self._simple(ClaimResult.PREPARE_FAILED, "claim changed before preparation", row, cap)
            return await self._prepare_and_activate_locked(session, row, task, cap, slot=slot)

    async def _prepare_and_activate_locked(self, session, row, task, cap=None, *, slot=None) -> dict:
        """Reset the slot, write the claim file, activate.

        *slot* is the workspace row ``record_holder`` already returned from
        inside the claim transaction (spec §15); it is only re-read here for
        callers that did not have one.
        """
        epoch = task.claim_epoch
        try:
            if slot is None:
                slot = await self.db.get_workspace_for_agent(row.agent_id)
            if slot is None:
                raise RuntimeError("session holds no workspace slot")
            await self.orchestrator._worktree_slots().reset_slot_for_task(slot, task)
            # Writing the claim file joins the same guard as the slot
            # reset: any failure after ``record_holder`` committed — a slot
            # reset error or an OSError on this write — must release the
            # claim and leave no claim file behind.
            write_claim_file(
                row.work_dir,
                {
                    "task_id": task.id,
                    "claim_epoch": epoch,
                    "session_id": session.id,
                    "claimed_at": time.time(),
                },
            )
        except Exception as exc:
            logger.warning("claim %s/%s: prepare failed: %s", session.id, task.id, exc)
            remove_claim_file(row.work_dir)
            await self.db.release_claim(
                session.id,
                task_status=TaskStatus.READY,
                context="slot_reset_failed",
                now=time.time(),
                result="prepare_failed",
                needs_attention="slot_reset_failed",
            )
            self._resolve_claim_waiters(session.id, epoch, "prepare_failed")
            return self._simple(ClaimResult.PREPARE_FAILED, str(exc), row, cap)
        fresh = await self.db.activate_claim(session.id, task.id, epoch=epoch, now=time.time())
        if not fresh:
            remove_claim_file(row.work_dir)
            self._resolve_claim_waiters(session.id, epoch, "prepare_failed")
            return self._simple(ClaimResult.PREPARE_FAILED, "released before activation", row, cap)
        await self.db.delete_task_meta(task.id, "manual_pause_checkpoint")
        self._resolve_claim_waiters(session.id, epoch, "claimed")
        await self.orchestrator._emit_task_event(
            "task.claimed",
            task,
            session_id=session.id,
            profile_id=session.profile_id,
            claim_epoch=epoch,
        )
        await self.orchestrator._emit_task_event("task.started", task, agent_id=row.agent_id)
        if session.lifecycle == "pool":
            await self.orchestrator.bus.emit(
                "pool.session_claimed",
                {
                    "project_id": session.project_id,
                    "profile_id": session.profile_id,
                    "session_id": session.id,
                    "name": session.name,
                    "task_id": task.id,
                    "task_title": task.title,
                },
            )
        # ``activate_claim`` returned the row it just wrote — no re-read.
        return await self._claimed_response(task, epoch, fresh, cap)

    # -- helpers -------------------------------------------------------------------

    def _session_block(self, row, cap=None) -> dict:
        if row is None:
            return {}
        return {
            "id": row.id,
            "claims": row.claims,
            "cap": cap,
            "desired_state": row.desired_state,
            "claim_phase": row.claim_phase,
        }

    def _simple(self, result: ClaimResult, reason: str, row=None, cap=None) -> dict:
        out = {
            "success": False,
            "result": result.value,
            "task": None,
            "claim_epoch": None,
            "session": self._session_block(row, cap),
        }
        if reason:
            out["reason"] = reason
        return out

    async def _claimed_response(self, task, epoch: int, row, cap=None) -> dict:
        """The claimed payload: the task **row**, not the ``task_show`` view.

        Spec §15: building the full ``task_show`` payload (dependencies,
        dependents, children, progress, context, labels) cost ~10 statements
        on every claim for data the worker rarely reads at claim time.  The
        row carries everything the claim protocol needs (id, status,
        ``claim_epoch``, workspace mode, …); ``aq task show <id>`` is the
        full view and the tool definition says so.
        """
        return {
            "success": True,
            "result": ClaimResult.CLAIMED.value,
            "task": _task_block(task),
            "claim_epoch": epoch,
            "session": self._session_block(row, cap),
        }

    def _resolve_claim_waiters(self, session_id: str, epoch: int | None, result: str) -> None:
        fut = self.orchestrator.claim_waiters.pop((session_id, epoch), None)
        if fut is not None and not fut.done():
            fut.set_result(result)

    async def _await_attempt(self, session_id: str, epoch: int | None, deadline: float):
        """Wait for the in-flight attempt to settle; poll the row when no future exists."""
        key = (session_id, epoch)
        fut = self.orchestrator.claim_waiters.get(key)
        while time.monotonic() < deadline:
            if fut is not None:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(fut), timeout=max(0.0, deadline - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    return None
            await asyncio.sleep(0.2)
            row = await self.db.get_session(session_id)
            if row is None:
                return None
            if row.claim_phase in (None, "active"):
                return row.last_claim_result or "released"
        return None
