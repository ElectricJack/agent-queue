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


def remove_claim_file(work_dir: str) -> None:
    try:
        os.remove(os.path.join(work_dir, CLAIM_FILE))
    except FileNotFoundError:
        pass


class ClaimCommandsMixin:
    """Mixed into CommandHandler.  Needs ``self.db``, ``self.orchestrator``, ``self.config``."""

    # -- fence ---------------------------------------------------------------

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
        limit = getattr(project, "token_budget", None)
        if limit and state.project_token_usage.get(project.id, 0) >= limit:
            return "budget_exhausted"
        return None

    # -- the command -----------------------------------------------------------

    async def _cmd_task_claim(self, args: dict) -> dict:
        """Claim a ready task for the calling session (``aq task claim``)."""
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
        session = await self.db.get_session(session_id)
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

        profile = await self.db.get_profile(session.profile_id)
        cap = getattr(profile, "max_claims_per_session", None)
        project = await self.db.get_project(session.project_id)
        default_profile = getattr(project, "default_profile_id", None)

        while True:
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
                w = self.orchestrator.bus.waiter(_ADMISSION_EVENTS)
                try:
                    await w.wait(max(0.0, deadline - time.monotonic()))
                finally:
                    w.close()
                project = await self.db.get_project(session.project_id)
                continue

            waiter = self.orchestrator.bus.waiter(
                _FRONTIER_EVENTS, filter={"project_id": session.project_id}
            )
            try:
                seq0 = await self.db.max_event_id()
                outcome = await self._attempt_claim(session, want_id, cap, default_profile)
                result = outcome["result"]
                if result == ClaimResult.NO_READY_WORK.value and wait:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return outcome
                    if await self.db.count_events_after(
                        seq0, event_type="task.ready", project_id=session.project_id
                    ):
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

    async def _attempt_claim(self, session, want_id, cap, default_profile) -> dict:
        """Decide the outcome on one ``immediate()`` transaction, on *conn* only.

        Every read/write in here must take ``conn`` explicitly (never a
        bare ``self.db.*`` call opening its own connection) — SQLite's
        shared single-connection pool means a second implicit transaction
        would auto-commit and cut this one's ``BEGIN IMMEDIATE`` out from
        under it (see ``immediate()``'s docstring).  Anything that needs
        its own connection (``_claimed_response`` → ``task_show``, the
        slot-reset + activation in ``_prepare_and_activate``) is deferred
        until after the ``async with`` block closes.
        """
        now = time.time()
        # What to do once the transaction has committed — set inside the
        # block, acted on outside it.
        active_claim: tuple | None = None  # (task, epoch, row) — already active
        new_claim: tuple | None = None  # (row, task) — a fresh "slot" claim
        async with self.db.immediate() as conn:
            kind, row = await self.db.take_claim_slot(conn, session.id, now=now, cap=cap)
            if kind == "active":
                if want_id and want_id != row.task_id:
                    return self._simple(
                        ClaimResult.OUT_OF_SCOPE, "session already holds a task", row
                    )
                task = await self.db._get_task_conn(row.task_id, conn=conn)
                active_claim = (task, task.claim_epoch, row)
            elif kind in ("preparing", "claiming"):
                epoch = None
                if row.task_id:
                    held = await self.db._get_task_conn(row.task_id, conn=conn)
                    epoch = held.claim_epoch if held else None
                out = self._simple(ClaimResult.CLAIM_IN_PROGRESS, kind, row)
                out.update(
                    task_id=row.task_id,
                    claim_epoch=epoch,
                    claim_phase=row.claim_phase,
                    claim_phase_at=row.claim_phase_at,
                )
                return out
            elif kind == "session_exhausted":
                return self._simple(
                    ClaimResult.SESSION_EXHAUSTED, "max_claims_per_session reached", row
                )
            elif kind == "drain_requested":
                return self._simple(ClaimResult.DRAIN_REQUESTED, "pool scaled down", row)
            elif kind != "slot":
                return self._simple(ClaimResult.OUT_OF_SCOPE, kind, row)
            else:
                tid = await self.db.select_ready_for_profile(
                    conn,
                    project_id=session.project_id,
                    profile_id=session.profile_id,
                    default_profile_id=default_profile,
                    agent_id=row.agent_id,
                    task_id=want_id,
                )
                task = None
                if tid is not None:
                    task = await self.db.take_task(conn, tid, agent_id=row.agent_id, now=now)
                if task is None:
                    await self.db.release_claim_slot(conn, session.id)
                    miss = ClaimResult.CLAIM_CONFLICT if want_id else ClaimResult.NO_READY_WORK
                    return self._simple(miss, "", row)
                await self.db.record_holder(
                    conn,
                    session_id=session.id,
                    task_id=tid,
                    agent_id=row.agent_id,
                    work_dir=row.work_dir,
                    now=now,
                )
                new_claim = (row, task)

        if active_claim is not None:
            task, epoch, row = active_claim
            return await self._claimed_response(task, epoch, row)
        row, task = new_claim
        return await self._prepare_and_activate(session, row, task)

    async def _prepare_and_activate(self, session, row, task) -> dict:
        epoch = task.claim_epoch
        try:
            slot = await self.db.get_workspace_for_agent(row.agent_id)
            if slot is None:
                raise RuntimeError("session holds no workspace slot")
            await self.orchestrator._worktree_slots().reset_slot_for_task(slot, task)
        except Exception as exc:
            logger.warning("claim %s/%s: slot reset failed: %s", session.id, task.id, exc)
            await self.db.release_claim(
                session.id,
                task_status=TaskStatus.READY,
                context="slot_reset_failed",
                now=time.time(),
                result="prepare_failed",
                needs_attention="slot_reset_failed",
            )
            self._resolve_claim_waiters(session.id, epoch, "prepare_failed")
            return self._simple(ClaimResult.PREPARE_FAILED, str(exc), row)
        write_claim_file(
            row.work_dir,
            {
                "task_id": task.id,
                "claim_epoch": epoch,
                "session_id": session.id,
                "claimed_at": time.time(),
            },
        )
        if not await self.db.activate_claim(session.id, task.id, epoch=epoch, now=time.time()):
            remove_claim_file(row.work_dir)
            self._resolve_claim_waiters(session.id, epoch, "prepare_failed")
            return self._simple(ClaimResult.PREPARE_FAILED, "released before activation", row)
        self._resolve_claim_waiters(session.id, epoch, "claimed")
        await self.orchestrator._emit_task_event(
            "task.claimed",
            task,
            session_id=session.id,
            profile_id=session.profile_id,
            claim_epoch=epoch,
        )
        await self.orchestrator._emit_task_event("task.started", task, agent_id=row.agent_id)
        fresh = await self.db.get_session(session.id)
        return await self._claimed_response(task, epoch, fresh)

    # -- helpers -------------------------------------------------------------------

    def _session_block(self, row) -> dict:
        if row is None:
            return {}
        return {
            "id": row.id,
            "claims": row.claims,
            "cap": None,
            "desired_state": row.desired_state,
            "claim_phase": row.claim_phase,
        }

    def _simple(self, result: ClaimResult, reason: str, row=None) -> dict:
        out = {
            "success": False,
            "result": result.value,
            "task": None,
            "claim_epoch": None,
            "session": self._session_block(row),
        }
        if reason:
            out["reason"] = reason
        return out

    async def _claimed_response(self, task, epoch: int, row) -> dict:
        shown = await self._cmd_task_show({"task_id": task.id})
        return {
            "success": True,
            "result": ClaimResult.CLAIMED.value,
            "task": shown.get("task", shown),
            "claim_epoch": epoch,
            "session": self._session_block(row),
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
