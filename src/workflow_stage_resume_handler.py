"""WorkflowStageResumeHandler — event-driven resume of paused playbook runs
when workflow stages complete.

Subscribes to ``workflow.stage.completed`` events on the EventBus and
automatically resumes the coordination playbook run that is paused waiting
for that event.  This implements long-running playbook support for
multi-stage coordination workflows (Roadmap 7.5.5).

The flow:

    1. A coordination playbook creates tasks for a workflow stage
    2. The playbook pauses at a ``wait_for_event`` node
       (``waiting_for_event = "workflow.stage.completed"``)
    3. As tasks complete, the orchestrator checks stage completion
    4. When all tasks in the stage are done, ``workflow.stage.completed``
       fires on the EventBus
    5. This handler catches the event, looks up the workflow's
       ``playbook_run_id``, and resumes the paused run with the event
       data injected into conversation context
    6. The playbook continues, creating the next stage's tasks, and
       the cycle repeats

This handler is separate from :class:`PlaybookResumeHandler` (which
handles ``human.review.completed`` events) for clean separation of
concerns — human-triggered vs. event-triggered resumption.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.database.base import DatabaseBackend
    from src.event_bus import EventBus
    from src.playbooks.manager import PlaybookManager

logger = logging.getLogger(__name__)

# Default pause timeout for event-triggered pauses (48 hours).
# Longer than the human-review default (24h) because workflow stages
# may take longer to complete than a human review cycle.
_DEFAULT_EVENT_PAUSE_TIMEOUT_S = 172800


class WorkflowStageResumeHandler:
    """Subscribe to ``workflow.stage.completed`` and resume paused playbook runs.

    Parameters
    ----------
    db:
        Database backend for loading workflows and playbook runs.
    event_bus:
        EventBus to subscribe to ``workflow.stage.completed`` events.
    orchestrator:
        Orchestrator instance — used to create Supervisors for LLM calls.
    playbook_manager:
        PlaybookManager — used to resolve playbook graphs when the run
        has no pinned graph.
    config:
        Application config — passed to Supervisor on creation.
    pause_timeout_seconds:
        Maximum time (seconds) an event-triggered pause can last before
        the run is considered timed out.  Defaults to 48 hours.
    """

    def __init__(
        self,
        *,
        db: DatabaseBackend,
        event_bus: EventBus,
        orchestrator: Any,
        playbook_manager: PlaybookManager,
        config: Any,
        pause_timeout_seconds: int = _DEFAULT_EVENT_PAUSE_TIMEOUT_S,
    ) -> None:
        self._db = db
        self._bus = event_bus
        self._orchestrator = orchestrator
        self._playbook_manager = playbook_manager
        self._config = config
        self._pause_timeout_seconds = pause_timeout_seconds

        self._unsubscribes: list[Callable[[], None]] = []
        # Track in-flight resumes to prevent double-resuming the same run
        self._running_resumes: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Register the ``workflow.stage.completed`` handler on the EventBus.

        Safe to call multiple times — previous subscriptions are cleared
        first.
        """
        self.unsubscribe()
        unsub = self._bus.subscribe(
            "workflow.stage.completed",
            self._on_stage_completed,
        )
        self._unsubscribes.append(unsub)
        logger.info("WorkflowStageResumeHandler subscribed to workflow.stage.completed")

    def unsubscribe(self) -> None:
        """Remove all EventBus subscriptions."""
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes.clear()

    def shutdown(self) -> None:
        """Unsubscribe and cancel any in-flight resume tasks."""
        self.unsubscribe()
        for run_id, task in list(self._running_resumes.items()):
            if not task.done():
                task.cancel()
                logger.info("Cancelled in-flight event resume for run %s", run_id)
        self._running_resumes.clear()

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_stage_completed(self, data: dict[str, Any]) -> None:
        """Handle a ``workflow.stage.completed`` event.

        Looks up the workflow to find the associated playbook run, checks
        that it's paused and waiting for this event type, then launches
        the resume as a background asyncio task.
        """
        workflow_id = data.get("workflow_id")
        if not workflow_id:
            logger.warning(
                "workflow.stage.completed event missing workflow_id: %s",
                {k: v for k, v in data.items() if k != "_event_type"},
            )
            return

        try:
            workflow = await self._db.get_workflow(workflow_id)
        except Exception:
            logger.error(
                "Failed to fetch workflow %s for stage resume",
                workflow_id,
                exc_info=True,
            )
            return

        if not workflow:
            logger.debug(
                "workflow.stage.completed: workflow '%s' not found",
                workflow_id,
            )
            return

        run_id = workflow.playbook_run_id
        if not run_id:
            logger.debug(
                "workflow.stage.completed: workflow '%s' has no playbook_run_id",
                workflow_id,
            )
            return

        # Prevent double-resume of the same run
        if run_id in self._running_resumes:
            task = self._running_resumes[run_id]
            if not task.done():
                logger.info(
                    "Event resume already in progress for run %s, skipping",
                    run_id,
                )
                return

        # Launch resume as a background task (LLM calls are long-running)
        task = asyncio.create_task(
            self._resume_run(run_id, data),
            name=f"event-resume-{run_id}",
        )
        self._running_resumes[run_id] = task
        task.add_done_callback(lambda _t: self._running_resumes.pop(run_id, None))

    # ------------------------------------------------------------------
    # Resume logic (runs as background task)
    # ------------------------------------------------------------------

    async def _resume_run(
        self,
        run_id: str,
        event_data: dict[str, Any],
    ) -> None:
        """Perform the full resume pipeline for event-triggered resumption.

        1. Fetch the ``PlaybookRun`` from the database.
        2. Validate it is paused and waiting for this event type.
        3. Check pause timeout.
        4. Resolve the compiled playbook graph.
        5. Create a :class:`Supervisor` for LLM calls.
        6. Call :meth:`PlaybookRunner.resume_from_event` to continue
           execution with the event data in conversation context.
        """
        from src.playbooks.runner import PlaybookRunner

        try:
            # 1. Fetch the paused run
            db_run = await self._db.get_playbook_run(run_id)
            if not db_run:
                logger.warning(
                    "workflow.stage.completed: run '%s' not found in database",
                    run_id,
                )
                return

            if db_run.status != "paused":
                logger.info(
                    "workflow.stage.completed: run '%s' has status '%s' (not paused), "
                    "skipping resume",
                    run_id,
                    db_run.status,
                )
                return

            # 2. Verify the run is waiting for workflow.stage.completed
            if db_run.waiting_for_event != "workflow.stage.completed":
                logger.info(
                    "workflow.stage.completed: run '%s' is waiting for '%s', "
                    "not 'workflow.stage.completed' — skipping",
                    run_id,
                    db_run.waiting_for_event,
                )
                return

            # 3. Check pause timeout
            paused_at = db_run.paused_at or db_run.started_at
            if paused_at and (time.time() - paused_at) > self._pause_timeout_seconds:
                logger.warning(
                    "Run '%s' exceeded event pause timeout (%ds), marking as timed_out",
                    run_id,
                    self._pause_timeout_seconds,
                )
                await self._db.update_playbook_run(
                    run_id,
                    status="timed_out",
                    completed_at=time.time(),
                    error=f"Event pause timeout exceeded ({self._pause_timeout_seconds}s)",
                    waiting_for_event=None,
                )
                return

            # 4. Resolve the playbook graph (pinned preferred)
            graph = self._resolve_graph(db_run)
            if not graph:
                logger.error(
                    "Cannot resolve playbook graph for run '%s' (playbook '%s')",
                    run_id,
                    db_run.playbook_id,
                )
                return

            # 5. Create a Supervisor for LLM calls
            from src.supervisor import Supervisor

            supervisor = Supervisor(self._orchestrator, self._config)
            if not supervisor.initialize():
                logger.error(
                    "Failed to initialize LLM provider for event resume of run '%s'",
                    run_id,
                )
                return

            # 6. Resume from the event data
            # Strip internal fields from event data before passing to the runner
            clean_data = {k: v for k, v in event_data.items() if not k.startswith("_")}

            result = await PlaybookRunner.resume_from_event(
                db_run=db_run,
                graph=graph,
                supervisor=supervisor,
                event_data=clean_data,
                db=self._db,
                event_bus=self._bus,
            )

            logger.info(
                "Run '%s' resumed via workflow.stage.completed: status=%s, tokens=%d",
                run_id,
                result.status,
                result.tokens_used,
            )

        except Exception:
            logger.error(
                "Failed to resume run '%s' from workflow.stage.completed event",
                run_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_graph(self, db_run: Any) -> dict | None:
        """Resolve the compiled playbook graph for a paused run.

        Prefers the pinned graph stored in the run record (version pinning,
        roadmap 5.2.12).  Falls back to the current active version from
        :class:`PlaybookManager`.
        """
        if db_run.pinned_graph:
            try:
                return json.loads(db_run.pinned_graph)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to parse pinned_graph for run '%s', falling back",
                    db_run.run_id,
                )

        # Fall back to PlaybookManager's active version
        active = getattr(self._playbook_manager, "_active", {})
        pb = active.get(db_run.playbook_id)
        if pb is not None:
            if hasattr(pb, "to_dict"):
                return pb.to_dict()
            return getattr(pb, "__dict__", None)

        return None

    @property
    def running_resumes(self) -> dict[str, asyncio.Task]:
        """In-flight resume tasks keyed by run_id (read-only view)."""
        return dict(self._running_resumes)
