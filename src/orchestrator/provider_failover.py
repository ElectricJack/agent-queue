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
from dataclasses import replace
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
        self, task: Any, *, preserve: bool = True, reason: str | None = None
    ) -> inflight.Checkpoint:
        """Preserve the work in *task*'s locked workspace on its branch (D13).

        Only the workspace locked by *task* is touched: a slot the task no
        longer holds may already be another task's live tree.  *preserve*
        False (a pool claim still being prepared, so the daemon owns the
        slot) records ``not_started`` and touches nothing.  *reason* names
        the stop in a hierarchy/train branch's ``WIP saved by AQ`` commit.
        The caller has stopped the session.  Never raises.
        """
        if not preserve:
            return inflight.Checkpoint(status="not_started")
        try:
            return await asyncio.wait_for(
                self._failover_checkpoint(task, reason), timeout=CHECKPOINT_BUDGET_SECONDS
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

    async def _failover_checkpoint(self, task: Any, reason: str | None) -> inflight.Checkpoint:
        from src.orchestrator.stranded_work import UNMERGED_BRANCH_META, UNMERGED_COMMIT_META

        project = await self.db.get_project(task.project_id)
        ws = await self.db.get_workspace_for_task(task.id)
        workspace = ws.workspace_path if ws else None
        if getattr(project, "hierarchical_integration_mode", None) in _INTEGRATION_MANAGED_MODES:
            return await self._integration_wip_checkpoint(task, project, workspace, reason)
        from src.git.manager import commit_identity

        # The WIP commit is the project's AQ-authored commit (git identity).
        with commit_identity(self.git.resolve_commit_identity(project)):
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

    async def _is_task_writer_owner(self, task: Any, repository_id: str, branch: str) -> bool:
        """Whether *task* itself owns *branch* as its writer (``worker``/``repair``)."""
        from src.integration.models import RETRYABLE_INTEGRATION_OWNER_ROLES, BranchKey
        from src.integration.ownership import BranchOwnership

        ownership = BranchOwnership(self.db)
        try:
            owner = await ownership.get_owner(
                BranchKey(repository_id=repository_id, branch=branch)
            )
            if owner is None and not branch.startswith("refs/heads/"):
                owner = await ownership.get_owner(
                    BranchKey(repository_id=repository_id, branch=f"refs/heads/{branch}")
                )
        except Exception:
            logger.debug("Task %s: branch owner unreadable", task.id, exc_info=True)
            return False
        return (
            owner is not None
            and owner.get("owner_id") == task.id
            and owner.get("owner_role") in RETRYABLE_INTEGRATION_OWNER_ROLES
        )

    async def _integration_wip_checkpoint(
        self, task: Any, project: Any, workspace: str | None, reason: str | None
    ) -> inflight.Checkpoint:
        """Save a stopped hierarchy/train writer's work onto its own task branch.

        The branch still belongs to its integration owner, and the release
        that follows goes through ``arelease_integration_writer_for_retry``:
        this only fast-forwards ``origin/<task branch>`` to a ``WIP saved by
        AQ`` commit, confined to the project's integration repository, so
        that release finds a clean, pushed checkout and the next worker
        resumes on the same branch.  A branch that cannot fast-forward is
        left untouched (``integration_managed``) for owner recovery, whose
        ``aq/preserved`` snapshot is what remains for that case.  A saved one
        records the resume point the next preparation continues from.
        """
        from src.git.manager import commit_identity
        from src.orchestrator.stranded_work import (
            WIP_SAVED_PREFIX,
            record_resume_point,
            save_wip_to_task_branch,
        )

        branch = getattr(task, "branch_name", None)
        untouched = inflight.Checkpoint(
            status="integration_managed", workspace=workspace, branch=branch
        )
        repository_id = getattr(project, "integration_repository_id", None)
        repository = await self.db.get_repo(repository_id) if repository_id else None
        if not workspace or not branch or repository is None or task.repo_id != repository.id:
            return untouched
        if not await self._is_task_writer_owner(task, repository.id, branch):
            # A verifier's (or any other owner's) checkout is not the
            # task's work; owner recovery decides what it keeps.
            return untouched
        why = " ".join((reason or "provider failover").split())[:160]
        with commit_identity(self.git.resolve_commit_identity(project)):
            saved = await save_wip_to_task_branch(
                self.git,
                workspace,
                branch,
                why,
                repository_url=repository.url,
                event_bus=getattr(self, "bus", None),
                project_id=task.project_id,
            )
        if saved.status not in ("pushed", "clean"):
            logger.warning(
                "Task %s: WIP not saved onto %s (%s: %s); owner recovery keeps the checkout",
                task.id, branch, saved.status, saved.error,
            )
            return replace(untouched, error=saved.error)
        await record_resume_point(self.db, task.id, repository.id, branch, saved.commit)
        return inflight.Checkpoint(
            status=saved.status,
            workspace=workspace,
            branch=saved.branch,
            head=saved.commit,
            wip_commit=bool(saved.count),
            pushed_branch=saved.branch if saved.status == "pushed" else None,
            commits=saved.count,
            wip_message=WIP_SAVED_PREFIX + why if saved.count else None,
        )

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
        screen: str | None = None,
    ) -> dict[str, Any]:
        """Write the hand-off note: where the last worker stopped, and why.

        Called once the task's outcome is written, so a failover retried
        after a crash does not leave a note for an outcome that never
        happened.  *screen* is the stopped session's last screenful.  Emits
        :data:`~src.providers.inflight.HANDOFF_EVENT` for the dashboard and
        the supervisor.  Never raises.
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
            screen=screen,
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
        emit = getattr(self, "_emit_task_event", None)
        if emit is not None:
            try:
                await emit(
                    inflight.HANDOFF_EVENT,
                    task,
                    reason=reason,
                    session_id=handoff.get("session_id"),
                    provider=failure.provider,
                    harness=handoff.get("harness"),
                    disposition=handoff["disposition"],
                    checkpoint=checkpoint.status,
                    branch=handoff.get("branch"),
                    head=checkpoint.head,
                )
            except Exception:
                logger.debug("Task %s: hand-off event failed", task.id, exc_info=True)
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
