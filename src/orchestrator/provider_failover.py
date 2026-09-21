"""The orchestrator half of in-flight provider failover (provider-failover D13).

:class:`~src.sessions.reconciler.SessionReconciler` decides that a session
died on its provider (:func:`src.providers.inflight.decide`); this mixin owns
the steps that need the orchestrator's Git, database and bus:

* :meth:`provider_failover_checkpoint` -- before anything releases the
  workspace, commit uncommitted work as a WIP checkpoint and push the branch,
  inside a time budget (this runs in the orchestrator cycle, and an
  account-wide limit kills every session on the provider at once);
* :meth:`provider_failover_handoff` -- once the task's outcome is final, the
  hand-off note (task comment plus
  ``task_metadata['provider_failover_handoff']``) the next worker's
  ``aq prime`` shows;
* :meth:`provider_failover_hold` -- when the push failed, hold the task in
  place instead of re-routing it: nothing is discarded to make a move
  possible.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.providers import inflight

logger = logging.getLogger(__name__)

__all__ = ["CHECKPOINT_BUDGET_SECONDS", "ProviderFailoverMixin"]

#: Integration modes whose task branch is fenced by an integration owner;
#: the failover checkpoint never pushes around that fence.
_INTEGRATION_MANAGED_MODES = frozenset({"hierarchy", "train"})
#: The most one dead session's checkpoint may take.  A timeout proves
#: nothing about the work, so it holds the task (``unknown`` is at risk).
CHECKPOINT_BUDGET_SECONDS = 90.0


class ProviderFailoverMixin:
    """Checkpoint, hand-off and hold for a session that died on its provider."""

    async def provider_failover_checkpoint(
        self, task: Any, *, preserve: bool = True
    ) -> inflight.Checkpoint:
        """Preserve the work in *task*'s locked workspace on its branch (D13).

        Only the workspace locked by *task* is touched: a slot the task no
        longer holds may already be another task's live tree.  *preserve*
        False (a pool claim still being prepared, so the daemon owns the
        slot) records ``not_started`` and touches nothing.  Never raises.
        """
        if not preserve:
            return inflight.Checkpoint(status="not_started")
        try:
            return await asyncio.wait_for(
                self._failover_checkpoint(task), timeout=CHECKPOINT_BUDGET_SECONDS
            )
        except TimeoutError:
            logger.warning(
                "Task %s: failover checkpoint exceeded %.0fs; holding the task",
                task.id,
                CHECKPOINT_BUDGET_SECONDS,
            )
            return inflight.Checkpoint(
                status="unknown", error=f"checkpoint exceeded {CHECKPOINT_BUDGET_SECONDS:.0f}s"
            )
        except Exception as exc:  # never let preservation break the exit path
            logger.warning("Task %s: failover checkpoint failed", task.id, exc_info=True)
            return inflight.Checkpoint(status="unknown", error=str(exc))

    async def _failover_checkpoint(self, task: Any) -> inflight.Checkpoint:
        from src.orchestrator.stranded_work import UNMERGED_BRANCH_META, UNMERGED_COMMIT_META

        project = await self.db.get_project(task.project_id)
        ws = await self.db.get_workspace_for_task(task.id)
        workspace = ws.workspace_path if ws else None
        if getattr(project, "hierarchical_integration_mode", None) in _INTEGRATION_MANAGED_MODES:
            # The branch belongs to its integration owner, and the release
            # that follows goes through ``arelease_integration_writer_for_retry``.
            # Pushing around that fence is not this module's call.
            return inflight.Checkpoint(
                status="integration_managed",
                workspace=workspace,
                branch=getattr(task, "branch_name", None),
            )
        checkpoint = await inflight.checkpoint_workspace(
            self.git,
            workspace,
            task.id,
            event_bus=getattr(self, "bus", None),
            project_id=task.project_id,
        )
        branch = checkpoint.pushed_branch or (checkpoint.branch if checkpoint.at_risk else None)
        if branch and checkpoint.commits:
            # The contract a retry, the dashboard and the next agent already
            # read to find a predecessor's commits (``stranded_work``).
            try:
                await self.db.set_task_meta(task.id, UNMERGED_BRANCH_META, branch)
                if checkpoint.head:
                    await self.db.set_task_meta(task.id, UNMERGED_COMMIT_META, checkpoint.head)
            except Exception:
                logger.debug("Task %s: unmerged branch not recorded", task.id, exc_info=True)
        return checkpoint

    async def provider_failover_handoff(
        self,
        task: Any,
        session: Any,
        *,
        failure: inflight.ProviderFailure,
        verdict: str,
        reason: str,
        checkpoint: inflight.Checkpoint,
        disposition: str,
        held: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Write the hand-off note: where the last worker stopped, and why.

        Called once the task's outcome is written, so a failover retried
        after a crash does not leave a note for an outcome that never
        happened.  Never raises.
        """
        at = time.time() if now is None else float(now)
        subtasks: list[dict] = []
        try:
            subtasks = list(await self.db.list_task_subtasks(task.id))
        except Exception:
            logger.debug("Task %s: subtasks unreadable for hand-off", task.id, exc_info=True)
        handoff = inflight.build_handoff(
            task=task,
            session=session,
            failure=failure,
            verdict=verdict,
            reason=reason,
            checkpoint=checkpoint,
            disposition=disposition,
            subtasks=subtasks,
            held=held,
            now=at,
        )
        try:
            await self.db.set_task_meta(task.id, inflight.HANDOFF_META, handoff)
        except Exception:
            logger.warning("Task %s: could not record the failover hand-off", task.id, exc_info=True)
        try:
            await self.db.add_task_comment(
                task.id,
                inflight.handoff_comment(handoff, task.id),
                author_kind="supervisor",
                author_id=inflight.AUTHOR_ID,
            )
        except Exception:
            logger.debug("Task %s: hand-off comment failed", task.id, exc_info=True)
        logger.info(
            "Task %s: provider %s failover (%s): checkpoint %s, wip_commit=%s, branch=%s, head=%s",
            task.id,
            failure.provider,
            handoff["disposition"],
            checkpoint.status,
            checkpoint.wip_commit,
            handoff.get("branch"),
            (checkpoint.head or "")[:12],
        )
        return handoff

    async def provider_failover_hold(self, task: Any, *, reason: str) -> bool:
        """Hold *task* in place: its work could not be pushed (D13).

        An operator hold through the existing pause machinery: the dead
        session is confirmed stopped, a local Git checkpoint of the workspace
        is captured (``task_checkpoint``) and restored by whichever slot the
        task lands in next, and nothing moves it until a human resumes it.
        Returns True when the hold is in place (its cleanup may still be
        retrying, which keeps every resource it has not yet preserved).
        """
        from src.models import TaskStatus

        try:
            await self.pause_task(task.id)
        except Exception as exc:
            # A recorded pause whose cleanup (the checkpoint capture) failed
            # retries from the monitoring cascade and keeps the workspace
            # locked until it succeeds -- still a hold.  Anything else is not.
            logger.warning(
                "Task %s: failover hold incomplete: %s", task.id, exc, exc_info=True
            )
        current = await self.db.get_task(task.id)
        if current is None or not (
            current.status == TaskStatus.PAUSED and current.resume_after is None
        ):
            logger.error("Task %s: could not hold after a failed push", task.id)
            return False
        try:
            await self.db.set_task_meta(task.id, "needs_attention", inflight.PUSH_FAILED_ATTENTION)
        except Exception:
            logger.debug("Task %s: needs_attention not recorded", task.id, exc_info=True)
        logger.warning(
            "Task %s: held in place after a provider failure -- %s; resume with "
            "`aq task resume %s` once the branch can be pushed",
            task.id,
            reason,
            task.id,
        )
        return True
