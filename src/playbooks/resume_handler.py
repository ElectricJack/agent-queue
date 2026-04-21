"""PlaybookResumeHandler — event-driven resume of paused playbook runs.

Subscribes to ``human.review.completed`` events on the EventBus and
automatically resumes the corresponding paused playbook run from its
saved conversation state.  This implements the event-driven resume path
described in ``docs/specs/design/playbooks.md`` Section 9 (steps 6–7):

    6. A ``human.review.completed`` event fires with the human's input
    7. The executor resumes the run from the paused node, with the
       human's input added to context

External systems (Discord buttons, API endpoints, other playbooks) fire
``human.review.completed`` with ``{playbook_id, run_id, node_id, decision}``
to trigger the resume.  The handler performs validation, creates a
Supervisor, and delegates to :meth:`PlaybookRunner.resume`, which
restores the full conversation history from the database and continues
execution from the exact saved state.

The command-based resume path (``resume_playbook`` command via
:class:`CommandHandler`) remains available for direct invocation and
returns a synchronous result.  Both paths use the same underlying
``PlaybookRunner.resume()`` method and emit ``playbook.run.resumed``
on completion.

Roadmap 5.4.3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.logging_config import CorrelationContext

if TYPE_CHECKING:
    from src.database.base import DatabaseBackend
    from src.event_bus import EventBus
    from src.playbooks.manager import PlaybookManager

logger = logging.getLogger(__name__)

# Default pause timeout in seconds (24 hours, matching spec §9).
_DEFAULT_PAUSE_TIMEOUT_S = 86400


class PlaybookResumeHandler:
    """Subscribe to ``human.review.completed`` events and resume paused runs.

    Parameters
    ----------
    db:
        Database backend for loading/updating playbook runs.
    event_bus:
        EventBus to subscribe to ``human.review.completed`` events.
    orchestrator:
        Orchestrator instance — used to create Supervisors for LLM calls
        during the resumed execution.
    playbook_manager:
        PlaybookManager — used to resolve the playbook graph when the run
        has no pinned graph (backward compatibility with pre-5.2.12 runs).
    config:
        Application config — passed to Supervisor on creation.
    pause_timeout_seconds:
        Maximum time (seconds) a run can remain paused before it is
        considered timed out.  Defaults to 24 hours.
    """

    def __init__(
        self,
        *,
        db: DatabaseBackend,
        event_bus: EventBus,
        orchestrator: Any,
        playbook_manager: PlaybookManager,
        config: Any,
        pause_timeout_seconds: int = _DEFAULT_PAUSE_TIMEOUT_S,
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
        """Register the ``human.review.completed`` handler on the EventBus.

        Safe to call multiple times — previous subscriptions are cleared
        first.
        """
        self.unsubscribe()
        unsub = self._bus.subscribe(
            "human.review.completed",
            self._on_human_review_completed,
        )
        self._unsubscribes.append(unsub)
        logger.info("PlaybookResumeHandler subscribed to human.review.completed")

    def unsubscribe(self) -> None:
        """Remove all EventBus subscriptions and cancel in-flight resumes."""
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes.clear()

    def shutdown(self) -> None:
        """Unsubscribe and cancel any in-flight resume tasks."""
        self.unsubscribe()
        for run_id, task in list(self._running_resumes.items()):
            if not task.done():
                task.cancel()
                logger.info("Cancelled in-flight resume for run %s", run_id)
        self._running_resumes.clear()

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_human_review_completed(self, data: dict[str, Any]) -> None:
        """Handle a ``human.review.completed`` event.

        Validates the event payload, checks that the referenced run exists
        and is paused, then launches the resume as a background asyncio
        task (since it involves LLM calls and can take minutes).

        Duplicate events for the same ``run_id`` are silently ignored
        while a resume is already in progress.
        """
        run_id = data.get("run_id")
        decision = data.get("decision", "")

        if not run_id:
            logger.warning(
                "human.review.completed event missing run_id: %s",
                {k: v for k, v in data.items() if k != "_event_type"},
            )
            return
        if not decision:
            logger.warning(
                "human.review.completed event for run %s has empty decision",
                run_id,
            )
            return

        # Prevent double-resume of the same run
        if run_id in self._running_resumes:
            task = self._running_resumes[run_id]
            if not task.done():
                logger.info(
                    "Resume already in progress for run %s, skipping duplicate event",
                    run_id,
                )
                return

        # Launch resume as a background task (LLM calls are long-running)
        task = asyncio.create_task(
            self._resume_run(run_id, decision, data),
            name=f"playbook-resume-{run_id}",
        )
        self._running_resumes[run_id] = task
        task.add_done_callback(lambda _t: self._running_resumes.pop(run_id, None))

    # ------------------------------------------------------------------
    # Resume logic (runs as background task)
    # ------------------------------------------------------------------

    async def _resume_run(
        self,
        run_id: str,
        decision: str,
        event_data: dict[str, Any],
    ) -> None:
        """Perform the full resume pipeline from saved conversation state.

        1. Fetch the ``PlaybookRun`` from the database.
        2. Validate it is in ``paused`` status and within timeout.
        3. Resolve the compiled playbook graph (pinned or current).
        4. Create a :class:`Supervisor` for LLM calls.
        5. Call :meth:`PlaybookRunner.resume` to restore conversation
           history and continue execution from the paused node.
        """
        from src.playbooks.runner import PlaybookRunner

        try:
            # 1. Fetch the paused run from the database
            db_run = await self._db.get_playbook_run(run_id)
            if not db_run:
                logger.warning(
                    "human.review.completed: run '%s' not found in database",
                    run_id,
                )
                return

            if db_run.status != "paused":
                logger.info(
                    "human.review.completed: run '%s' has status '%s' (not paused), "
                    "skipping resume",
                    run_id,
                    db_run.status,
                )
                return

            # 2. Check pause timeout (spec §9: default 24 hours)
            paused_at = self._get_paused_at(db_run)
            if paused_at and (time.time() - paused_at) > self._pause_timeout_seconds:
                logger.warning(
                    "Run '%s' exceeded pause timeout (%ds), marking as timed_out",
                    run_id,
                    self._pause_timeout_seconds,
                )
                await self._db.update_playbook_run(
                    run_id,
                    status="timed_out",
                    completed_at=time.time(),
                    error=f"Pause timeout exceeded ({self._pause_timeout_seconds}s)",
                )
                return

            # 3. Resolve the playbook graph — pinned version preferred
            graph = self._resolve_graph(db_run)
            if not graph:
                logger.error(
                    "Cannot resolve playbook graph for run '%s' (playbook '%s')",
                    run_id,
                    db_run.playbook_id,
                )
                return

            # 4. Create a Supervisor for LLM calls
            from src.supervisor import Supervisor

            supervisor = Supervisor(self._orchestrator, self._config)
            if not supervisor.initialize():
                logger.error(
                    "Failed to initialize LLM provider for resume of run '%s'",
                    run_id,
                )
                return

            # 5. Resume from saved conversation state
            with CorrelationContext(run_id=db_run.run_id):
                result = await PlaybookRunner.resume(
                    db_run=db_run,
                    graph=graph,
                    supervisor=supervisor,
                    human_input=decision,
                    db=self._db,
                    event_bus=self._bus,
                )

            logger.info(
                "Run '%s' resumed via human.review.completed event: status=%s, tokens=%d",
                run_id,
                result.status,
                result.tokens_used,
            )

        except Exception:
            logger.error(
                "Failed to resume run '%s' from human.review.completed event",
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
                    "Failed to parse pinned_graph for run '%s', falling back to active version",
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

    @staticmethod
    def _get_paused_at(db_run: Any) -> float | None:
        """Extract the timestamp when a run was paused.

        Looks at the node trace for the ``completed_at`` of the last entry
        (the node where execution paused).  Falls back to ``started_at``
        of the last entry, then the run's ``started_at``.
        """
        try:
            trace = json.loads(db_run.node_trace) if db_run.node_trace else []
        except (json.JSONDecodeError, TypeError):
            return db_run.started_at

        if not trace:
            return db_run.started_at

        last_entry = trace[-1]
        # Prefer completed_at (the moment execution of the node finished
        # and pause was triggered), fall back to started_at of that node
        return last_entry.get("completed_at") or last_entry.get("started_at", db_run.started_at)

    @property
    def running_resumes(self) -> dict[str, asyncio.Task]:
        """In-flight resume tasks keyed by run_id (read-only view)."""
        return dict(self._running_resumes)
