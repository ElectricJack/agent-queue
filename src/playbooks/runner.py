"""PlaybookRunner — graph walker that steps through playbook nodes with conversation history.

Implements the playbook execution model from docs/specs/design/playbooks.md §6.
The runner walks a compiled playbook graph (JSON), executing each node's prompt
via :meth:`Supervisor.chat` and maintaining a ``messages`` list across nodes so
that downstream nodes naturally see prior context.

**Design decisions:**

- **Executor history vs. Supervisor history.**  The runner's ``messages`` list
  contains only node prompts and the Supervisor's final responses — NOT the raw
  tool-call/result messages from inside each ``supervisor.chat()`` call.  This
  keeps the context lean.  If a downstream node needs specific tool output, the
  node prompt should instruct the LLM to include those details in its response.

- **Transition evaluation** uses a separate, cheap LLM call with the conversation
  history and the list of candidate conditions.  Unconditional ``goto`` edges skip
  the LLM entirely.

- **Run persistence** — the runner writes a ``PlaybookRun`` row at startup and
  updates it after each node so that paused/failed runs can be inspected and
  (eventually) resumed.

See also: :mod:`src.playbooks.handler` (vault watcher / compilation dispatch).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.models import PlaybookRun, PlaybookRunEvent, PlaybookRunStatus
from src.playbooks.runner_context import ContextMixin, _parse_json_from_text
from src.playbooks.runner_events import EventsMixin
from src.playbooks.runner_transitions import TransitionMixin, _event_to_fallback_status
from src.playbooks.state_machine import (
    InvalidPlaybookRunTransition,
    validate_transition,
)
from src.playbooks.token_tracker import (
    DailyTokenTracker,
    _estimate_tokens,
    _midnight_today,
)

# Re-export helpers that tests and other modules import from this location.
# The canonical definitions now live in their respective submodules, but we
# keep backward-compatible imports here.
from src.playbooks.runner_transitions import (  # noqa: F401
    _compare,
    _dot_get,
    _parse_literal,
)

if TYPE_CHECKING:
    from src.database.base import DatabaseBackend
    from src.event_bus import EventBus
    from src.supervisor import Supervisor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper data classes
# ---------------------------------------------------------------------------


@dataclass
class NodeTraceEntry:
    """One entry in the run's ``node_trace`` list."""

    node_id: str
    started_at: float
    completed_at: float | None = None
    status: str = "running"  # running | completed | failed | skipped
    transition_to: str | None = None  # next node ID after evaluation
    transition_method: str | None = None  # "goto" | "llm" | "structured" | "otherwise"
    tokens_used: int = 0  # estimated tokens consumed by this node (roadmap 5.7.1)


@dataclass
class RunResult:
    """Value returned by :meth:`PlaybookRunner.run`."""

    run_id: str
    status: str  # completed | failed | paused | timed_out
    node_trace: list[dict]
    tokens_used: int
    error: str | None = None
    final_response: str | None = None


class _DummySupervisor:
    """Placeholder supervisor used when timeout handling doesn't need LLM calls.

    Only used by :meth:`PlaybookRunner.handle_timeout` for the simple
    timed_out-without-timeout-node path, where the runner constructor
    requires a supervisor but no LLM calls are made.
    """

    async def chat(self, **kwargs: Any) -> str:
        raise RuntimeError("_DummySupervisor does not support chat()")


# ---------------------------------------------------------------------------
# PlaybookRunner
# ---------------------------------------------------------------------------


class PlaybookRunner(EventsMixin, TransitionMixin, ContextMixin):
    """Walk a compiled playbook graph, executing nodes via the Supervisor.

    Parameters
    ----------
    graph:
        The compiled playbook JSON (dict).  Must have ``id``, ``version``,
        ``nodes`` keys.  See docs/specs/design/playbooks.md §5 for schema.
    event:
        The trigger event data (dict) that started this run.
    supervisor:
        A :class:`~src.supervisor.Supervisor` instance for LLM calls.
    db:
        Database backend for persisting the :class:`PlaybookRun` record.
        When *None*, run state is not persisted (useful for testing).
    on_progress:
        Optional async callback ``(event: str, detail: str | None) -> None``
        for reporting execution progress (e.g., to Discord).
    """

    def __init__(
        self,
        graph: dict,
        event: dict,
        supervisor: Supervisor,
        db: DatabaseBackend | None = None,
        on_progress: Callable[[str, str | None], Awaitable[None]] | None = None,
        max_daily_playbook_tokens: int | None = None,
        daily_token_tracker: DailyTokenTracker | None = None,
        daily_token_cap: int | None = None,
        event_bus: EventBus | None = None,
    ):
        self.graph = graph
        self.event = event
        self.supervisor = supervisor
        self.db = db
        self.on_progress = on_progress
        self._daily_token_tracker = daily_token_tracker
        self._daily_token_cap = daily_token_cap
        self.event_bus = event_bus

        # Conversation history — kept for DB persistence and backward compat.
        # NOT used as LLM context between nodes (per-node context is built fresh).
        self.messages: list[dict] = []
        self.run_id: str = str(uuid.uuid4())[:12]
        self.tokens_used: int = 0
        self.node_trace: list[NodeTraceEntry] = []

        # Structured data flow between nodes.  Each node stores its output
        # here (keyed by node_id or by output.as name).  Downstream nodes
        # reference these via dot-path in templates and for_each sources.
        self.node_outputs: dict[str, Any] = {}

        # Seed message — stored separately so per-node context can always
        # include it without scanning self.messages.
        self._seed_message: str = ""

        # Current run status — tracked via the state machine
        # (see src/playbooks/state_machine.py).
        self._status: PlaybookRunStatus = PlaybookRunStatus.RUNNING

        # Dry-run mode — no LLM calls, no DB writes, no event emission.
        # Set via the :meth:`dry_run` classmethod.
        self._dry_run: bool = False

        # Resolved from graph
        self._playbook_id: str = graph.get("id", "unknown")
        self._playbook_version: int = graph.get("version", 0)
        self._max_tokens: int | None = graph.get("max_tokens")
        self._llm_config: dict | None = graph.get("llm_config")
        self._transition_llm_config: dict | None = graph.get("transition_llm_config")

        # Global daily playbook token cap (roadmap 5.2.8).
        # When set, ``run()`` checks today's cumulative playbook token usage
        # before starting and refuses to execute if the cap is already reached.
        self._max_daily_playbook_tokens: int | None = max_daily_playbook_tokens

    # ------------------------------------------------------------------
    # Daily playbook token cap (roadmap 5.2.8)
    # ------------------------------------------------------------------

    async def _get_daily_playbook_tokens(self) -> int:
        """Query the DB for today's cumulative playbook token usage.

        Returns the sum of ``tokens_used`` for all runs started since
        midnight (local time) today.  Returns ``0`` when no DB is
        configured.
        """
        if not self.db:
            return 0
        midnight = _midnight_today()
        return await self.db.get_daily_playbook_token_usage(midnight)

    @staticmethod
    async def check_daily_budget(
        db: DatabaseBackend,
        max_daily_playbook_tokens: int | None,
    ) -> tuple[bool, int]:
        """Pre-flight check: is the daily playbook token cap exceeded?

        Useful for callers that want to decide whether to create a runner
        at all, without incurring the cost of instantiation and DB record
        creation.

        Parameters
        ----------
        db:
            Database backend to query.
        max_daily_playbook_tokens:
            The configured cap, or ``None`` for unlimited.

        Returns
        -------
        tuple[bool, int]
            ``(exceeded, daily_used)`` — *exceeded* is ``True`` when the
            cap is set and today's usage meets or exceeds it.
        """
        if max_daily_playbook_tokens is None:
            return False, 0
        midnight = _midnight_today()
        daily_used = await db.get_daily_playbook_token_usage(midnight)
        return daily_used >= max_daily_playbook_tokens, daily_used

    # ------------------------------------------------------------------
    # State machine integration
    # ------------------------------------------------------------------

    def _transition(self, event: PlaybookRunEvent) -> PlaybookRunStatus:
        """Validate and apply a state transition.

        Uses the formal state machine (:mod:`src.playbooks.state_machine`) to
        check whether the transition is legal.  On success, updates
        ``self._status`` and returns the new status.  On invalid transitions,
        logs a warning but still applies the transition to avoid breaking
        running playbooks — the state machine is currently advisory (matching
        the task state machine approach in :mod:`src.state_machine`).
        """
        try:
            target = validate_transition(self._status, event, self.run_id)
        except InvalidPlaybookRunTransition:
            # Log but don't raise — the runner should not crash on an
            # unexpected transition.  This lets us detect bugs in the
            # transition logic without blocking execution.
            logger.warning(
                "Playbook run %s: applying transition anyway (%s -[%s]-> ???)",
                self.run_id,
                self._status.value,
                event.value,
            )
            # Derive the intended status from the event
            target = _event_to_fallback_status(event)
        self._status = target
        return target

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> RunResult:
        """Execute the playbook graph from entry to terminal node.

        Returns a :class:`RunResult` with the final status, trace, and
        token usage.  Persists state to the database (when ``db`` is set)
        at startup and after each node.
        """
        started_at = time.time()

        # ---- Daily token cap pre-flight check ----
        # If a global daily cap is configured and usage already exceeds it,
        # reject the run before creating any DB records or executing nodes.
        if (
            self._daily_token_tracker is not None
            and self._daily_token_cap is not None
            and self._daily_token_tracker.get_usage() >= self._daily_token_cap
        ):
            daily_usage = self._daily_token_tracker.get_usage()
            error = (
                f"daily_token_cap_exceeded: daily cap {self._daily_token_cap} "
                f"exhausted (used {daily_usage})"
            )
            logger.warning(
                "Playbook '%s' blocked — daily token cap reached: %s/%s",
                self._playbook_id,
                daily_usage,
                self._daily_token_cap,
            )
            # We cannot call _fail() because no db_run exists yet.
            # Build a minimal RunResult directly.
            if self.on_progress:
                await self.on_progress("playbook_failed", error)
            # Emit playbook.run.failed event (roadmap 5.3.6)
            await self._emit_failed_event(error=error, started_at=started_at)
            return RunResult(
                run_id=self.run_id,
                status="failed",
                node_trace=[],
                tokens_used=0,
                error=error,
            )

        # Create the DB record — pin the compiled graph so that in-flight
        # runs continue with the version they started with, even if the
        # playbook is recompiled while the run is paused.
        db_run = PlaybookRun(
            run_id=self.run_id,
            playbook_id=self._playbook_id,
            playbook_version=self._playbook_version,
            trigger_event=json.dumps(self.event),
            status="running",
            started_at=started_at,
            pinned_graph=json.dumps(self.graph),
        )
        if self.db:
            await self.db.create_playbook_run(db_run)

        # Daily playbook token cap check (roadmap 5.2.8).
        # Query today's cumulative usage before spending any tokens.
        if self._max_daily_playbook_tokens is not None and self.db:
            daily_used = await self._get_daily_playbook_tokens()
            if daily_used >= self._max_daily_playbook_tokens:
                return await self._fail(
                    db_run,
                    (
                        f"daily_playbook_token_cap_exceeded: limit "
                        f"{self._max_daily_playbook_tokens} reached "
                        f"(used today: {daily_used})"
                    ),
                    started_at,
                    event=PlaybookRunEvent.BUDGET_EXCEEDED,
                )

        # Seed conversation with event context and execution instructions.
        # The preamble overrides the system prompt's delegation instinct —
        # playbook nodes must be executed directly using tools, not delegated
        # to agents via create_task (unless the node prompt says to).
        seed_message = (
            f"Event received: {json.dumps(self.event)}\n\n"
            f"You are executing playbook '{self._playbook_id}'. "
            f"Each step will give you a specific instruction.\n\n"
            f"**Rules:**\n"
            f"- Call the tools mentioned in each step directly (they are "
            f"available in your tool list — do NOT use run_command)\n"
            f"- Do NOT create tasks unless the step explicitly says to\n"
            f"- Use `load_tools(category=...)` if a tool is not in your "
            f"current tool list\n"
            f"- After completing each step, describe what you did and the results"
        )
        self._seed_message = seed_message
        self.messages.append({"role": "user", "content": seed_message})

        # Find entry node
        entry_node_id = self._find_entry_node()
        if entry_node_id is None:
            return await self._fail(
                db_run,
                "No entry node found in playbook graph",
                started_at,
                event=PlaybookRunEvent.GRAPH_ERROR,
            )

        if self.on_progress:
            await self.on_progress("playbook_started", self._playbook_id)

        # Walk the graph
        current_node_id = entry_node_id
        final_response: str | None = None

        while True:
            node = self.graph["nodes"].get(current_node_id)
            if node is None:
                return await self._fail(
                    db_run,
                    f"Node '{current_node_id}' not found in graph",
                    started_at,
                    event=PlaybookRunEvent.GRAPH_ERROR,
                )

            # Terminal node — execute its prompt (if any) then stop.
            if node.get("terminal"):
                if node.get("prompt"):
                    try:
                        response = await self._execute_node(current_node_id, node, db_run)
                        final_response = response
                    except Exception:
                        logger.exception("Terminal node '%s' execution failed", current_node_id)
                        # Don't fail the run — terminal node errors are non-fatal
                if self.on_progress:
                    await self.on_progress("node_terminal", current_node_id)
                break

            # Check token budget before executing (guard for tokens accumulated
            # by transition evaluation in the previous iteration)
            if self._max_tokens is not None and self.tokens_used >= self._max_tokens:
                return await self._fail(
                    db_run,
                    (
                        f"token_budget_exceeded: budget {self._max_tokens} "
                        f"exhausted before node '{current_node_id}' "
                        f"(used {self.tokens_used})"
                    ),
                    started_at,
                    event=PlaybookRunEvent.BUDGET_EXCEEDED,
                )

            # Execute the node
            try:
                response = await self._execute_node(current_node_id, node, db_run)
                final_response = response
            except Exception as exc:
                logger.exception("Node '%s' execution failed", current_node_id)
                return await self._fail(
                    db_run,
                    f"Node '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.NODE_FAILED,
                )

            # Check token budget after node completes (spec §6 step 6d).
            # The node is allowed to finish (graceful), but we fail before
            # spending additional tokens on transition evaluation.
            if self._max_tokens is not None and self.tokens_used >= self._max_tokens:
                return await self._fail(
                    db_run,
                    (
                        f"token_budget_exceeded: budget {self._max_tokens} "
                        f"exhausted after node '{current_node_id}' "
                        f"(used {self.tokens_used})"
                    ),
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.BUDGET_EXCEEDED,
                )

            # Human-in-the-loop pause (skip in dry-run mode — simulate
            # continuing past the checkpoint)
            if node.get("wait_for_human") and not self._dry_run:
                return await self._pause(db_run, current_node_id, started_at)

            # Event-triggered pause — park the run until an external event
            # fires (e.g. workflow.stage.completed).  Skipped in dry-run mode.
            wait_event = node.get("wait_for_event")
            if wait_event and not self._dry_run:
                event_type = (
                    wait_event if isinstance(wait_event, str) else wait_event.get("event", "")
                )
                return await self._pause_for_event(db_run, current_node_id, started_at, event_type)

            # Determine next node via transition evaluation
            try:
                next_node_id, t_method = await self._evaluate_transition(
                    current_node_id, node, response
                )
            except Exception as exc:
                logger.exception("Transition evaluation failed at node '%s'", current_node_id)
                return await self._fail(
                    db_run,
                    f"Transition from '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.TRANSITION_FAILED,
                )

            # Record transition info on the trace entry for this node
            if self.node_trace:
                self.node_trace[-1].transition_to = next_node_id
                self.node_trace[-1].transition_method = t_method

            if next_node_id is None:
                # No transition matched and no terminal — implicit completion
                logger.warning(
                    "No transition matched at node '%s' — treating as terminal",
                    current_node_id,
                )
                break

            current_node_id = next_node_id

        # Completed successfully — validate via state machine
        self._transition(PlaybookRunEvent.TERMINAL_REACHED)
        completed_at = time.time()
        trace_dicts = [self._trace_to_dict(t) for t in self.node_trace]

        if self.db:
            await self.db.update_playbook_run(
                self.run_id,
                status=self._status.value,
                conversation_history=json.dumps(self.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=self.tokens_used,
                completed_at=completed_at,
            )

        if self.on_progress:
            await self.on_progress("playbook_completed", self._playbook_id)

        # Emit playbook.run.completed event (roadmap 5.3.6)
        await self._emit_completed_event(
            final_context=final_response,
            started_at=started_at,
        )

        return RunResult(
            run_id=self.run_id,
            status=self._status.value,
            node_trace=trace_dicts,
            tokens_used=self.tokens_used,
            final_response=final_response,
        )

    # ------------------------------------------------------------------
    # Resume from a paused run
    # ------------------------------------------------------------------

    @classmethod
    async def resume(
        cls,
        db_run: PlaybookRun,
        graph: dict,
        supervisor: Supervisor,
        human_input: str,
        db: DatabaseBackend | None = None,
        on_progress: Callable[[str, str | None], Awaitable[None]] | None = None,
        event_bus: EventBus | None = None,
    ) -> RunResult:
        """Resume a paused playbook run with human input.

        Reconstructs the runner state from the persisted ``PlaybookRun``,
        injects the human's input into conversation history, and continues
        walking the graph from the paused node.

        **Version pinning (5.2.12):** If the run has a ``pinned_graph``
        (the compiled graph snapshot saved at run start), it is used instead
        of the caller-supplied *graph*.  This ensures that in-flight runs
        continue with the version they started with, even if the playbook
        was recompiled while the run was paused.  The *graph* parameter
        serves as a fallback for runs created before version pinning was
        added.

        Parameters
        ----------
        db_run:
            The persisted :class:`PlaybookRun` with status ``"paused"``.
        graph:
            The compiled playbook graph — used as a fallback when no
            ``pinned_graph`` is stored in the run record (backward
            compatibility with pre-5.2.12 runs).
        supervisor:
            Supervisor instance for LLM calls.
        human_input:
            The human reviewer's response / decision text.
        db:
            Database backend for persisting updates.
        on_progress:
            Optional progress callback.
        """
        # Use the pinned graph from the run record if available (version
        # pinning), otherwise fall back to the caller-supplied graph.
        if db_run.pinned_graph:
            effective_graph = json.loads(db_run.pinned_graph)
            logger.debug(
                "Resuming run %s with pinned graph v%d (current graph v%d)",
                db_run.run_id,
                effective_graph.get("version", 0),
                graph.get("version", 0),
            )
        else:
            effective_graph = graph
            logger.debug(
                "Resuming run %s without pinned graph — using current v%d",
                db_run.run_id,
                graph.get("version", 0),
            )

        runner = cls(
            effective_graph,
            json.loads(db_run.trigger_event),
            supervisor,
            db,
            on_progress,
            event_bus=event_bus,
        )
        runner.run_id = db_run.run_id
        runner.messages = json.loads(db_run.conversation_history)
        runner.node_trace = [NodeTraceEntry(**entry) for entry in json.loads(db_run.node_trace)]
        runner.tokens_used = db_run.tokens_used

        # Reconstruct status from persisted state and transition to running
        runner._status = PlaybookRunStatus.PAUSED
        runner._transition(PlaybookRunEvent.HUMAN_RESUMED)

        # Inject human input into conversation — both the persistence log
        # (for DB/audit) and the paused node's output (so the fresh per-node
        # context builder surfaces it to downstream nodes via the
        # "Prior Step Results" block).
        runner.messages.append(
            {
                "role": "user",
                "content": f"[Human review response]: {human_input}",
            }
        )
        if db_run.current_node:
            runner.node_outputs[f"{db_run.current_node}__human_review"] = (
                f"[Human review response]: {human_input}"
            )

        # Update DB status to running
        if db:
            await db.update_playbook_run(db_run.run_id, status=runner._status.value)

        # Emit playbook.run.resumed event (spec §9) for audit/notification
        paused_node_id = db_run.current_node
        if paused_node_id:
            await runner._emit_resumed_event(
                node_id=paused_node_id,
                human_input=human_input,
            )

        # Find the next node after the paused one
        if not paused_node_id:
            return await runner._fail(
                db_run,
                "Cannot resume: no current_node recorded",
                db_run.started_at,
                event=PlaybookRunEvent.GRAPH_ERROR,
            )

        paused_node = effective_graph["nodes"].get(paused_node_id)
        if not paused_node:
            return await runner._fail(
                db_run,
                f"Cannot resume: node '{paused_node_id}' not found in graph",
                db_run.started_at,
                event=PlaybookRunEvent.GRAPH_ERROR,
            )

        # Get the last response from conversation to evaluate transitions
        last_response = ""
        for msg in reversed(runner.messages):
            if msg["role"] == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_response = content
                    break

        # Evaluate transition from paused node (human input is now in context)
        try:
            next_node_id, _t_method = await runner._evaluate_transition(
                paused_node_id, paused_node, last_response
            )
        except Exception as exc:
            return await runner._fail(
                db_run,
                f"Transition from paused node failed: {exc}",
                db_run.started_at,
                event=PlaybookRunEvent.TRANSITION_FAILED,
            )

        if next_node_id is None:
            # No transition — completed
            runner._transition(PlaybookRunEvent.TERMINAL_REACHED)
            completed_at = time.time()
            trace_dicts = [runner._trace_to_dict(t) for t in runner.node_trace]
            if db:
                await db.update_playbook_run(
                    db_run.run_id,
                    status=runner._status.value,
                    conversation_history=json.dumps(runner.messages),
                    node_trace=json.dumps(trace_dicts),
                    tokens_used=runner.tokens_used,
                    completed_at=completed_at,
                )
            # Emit playbook.run.completed event (roadmap 5.3.6)
            await runner._emit_completed_event(started_at=db_run.started_at)
            return RunResult(
                run_id=db_run.run_id,
                status=runner._status.value,
                node_trace=trace_dicts,
                tokens_used=runner.tokens_used,
            )

        # Continue walking the graph from the next node
        started_at = db_run.started_at
        current_node_id = next_node_id
        final_response: str | None = None

        while True:
            node = effective_graph["nodes"].get(current_node_id)
            if node is None:
                return await runner._fail(
                    db_run,
                    f"Node '{current_node_id}' not found in graph",
                    started_at,
                    event=PlaybookRunEvent.GRAPH_ERROR,
                )

            if node.get("terminal"):
                break

            if runner._max_tokens is not None and runner.tokens_used >= runner._max_tokens:
                return await runner._fail(
                    db_run,
                    (
                        f"token_budget_exceeded: budget {runner._max_tokens} "
                        f"exhausted before node '{current_node_id}' "
                        f"(used {runner.tokens_used})"
                    ),
                    started_at,
                    event=PlaybookRunEvent.BUDGET_EXCEEDED,
                )

            try:
                response = await runner._execute_node(current_node_id, node, db_run)
                final_response = response
            except Exception as exc:
                return await runner._fail(
                    db_run,
                    f"Node '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.NODE_FAILED,
                )

            # Check token budget after node completes (spec §6 step 6d)
            if runner._max_tokens is not None and runner.tokens_used >= runner._max_tokens:
                return await runner._fail(
                    db_run,
                    (
                        f"token_budget_exceeded: budget {runner._max_tokens} "
                        f"exhausted after node '{current_node_id}' "
                        f"(used {runner.tokens_used})"
                    ),
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.BUDGET_EXCEEDED,
                )

            if node.get("wait_for_human"):
                return await runner._pause(db_run, current_node_id, started_at)

            wait_event = node.get("wait_for_event")
            if wait_event:
                event_type = (
                    wait_event if isinstance(wait_event, str) else wait_event.get("event", "")
                )
                return await runner._pause_for_event(
                    db_run, current_node_id, started_at, event_type
                )

            try:
                next_node_id, t_method = await runner._evaluate_transition(
                    current_node_id, node, response
                )
            except Exception as exc:
                return await runner._fail(
                    db_run,
                    f"Transition from '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.TRANSITION_FAILED,
                )

            # Record transition info on the trace entry for this node
            if runner.node_trace:
                runner.node_trace[-1].transition_to = next_node_id
                runner.node_trace[-1].transition_method = t_method

            if next_node_id is None:
                break

            current_node_id = next_node_id

        # Completed successfully — validate via state machine
        runner._transition(PlaybookRunEvent.TERMINAL_REACHED)
        completed_at = time.time()
        trace_dicts = [runner._trace_to_dict(t) for t in runner.node_trace]

        if db:
            await db.update_playbook_run(
                db_run.run_id,
                status=runner._status.value,
                conversation_history=json.dumps(runner.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=runner.tokens_used,
                completed_at=completed_at,
            )

        # Emit playbook.run.completed event (roadmap 5.3.6)
        await runner._emit_completed_event(
            final_context=final_response,
            started_at=db_run.started_at,
        )

        return RunResult(
            run_id=db_run.run_id,
            status=runner._status.value,
            node_trace=trace_dicts,
            tokens_used=runner.tokens_used,
            final_response=final_response,
        )

    # ------------------------------------------------------------------
    # Event-triggered resume (roadmap 7.5.5)
    # ------------------------------------------------------------------

    @classmethod
    async def resume_from_event(
        cls,
        db_run: PlaybookRun,
        graph: dict,
        supervisor: Supervisor,
        event_data: dict,
        db: DatabaseBackend | None = None,
        on_progress: Callable[[str, str | None], Awaitable[None]] | None = None,
        event_bus: EventBus | None = None,
    ) -> RunResult:
        """Resume a paused playbook run with event data.

        Like :meth:`resume`, but designed for event-triggered resumption
        rather than human input.  The ``event_data`` (e.g., the payload
        from ``workflow.stage.completed``) is injected into conversation
        history so the LLM can act on it when evaluating the next
        transition and executing subsequent nodes.

        This implements the long-running playbook support for coordination
        workflows that span multiple stages (Roadmap 7.5.5).  The pattern
        is: a coordination playbook creates tasks for a stage, pauses at a
        ``wait_for_event`` node, then resumes here when the event fires.

        Parameters
        ----------
        db_run:
            The persisted :class:`PlaybookRun` with status ``"paused"``
            and ``waiting_for_event`` set.
        graph:
            Fallback compiled playbook graph (used when no ``pinned_graph``
            is stored in the run record).
        supervisor:
            Supervisor instance for LLM calls.
        event_data:
            The event payload that triggered the resume (e.g.,
            ``{"workflow_id": ..., "stage": ..., "task_ids": [...]}``)
        db:
            Database backend for persisting updates.
        on_progress:
            Optional progress callback.
        event_bus:
            EventBus for emitting lifecycle events.
        """
        # Resolve the graph (pinned or caller-supplied)
        if db_run.pinned_graph:
            effective_graph = json.loads(db_run.pinned_graph)
            logger.debug(
                "Event-resuming run %s with pinned graph v%d",
                db_run.run_id,
                effective_graph.get("version", 0),
            )
        else:
            effective_graph = graph
            logger.debug(
                "Event-resuming run %s without pinned graph — using current v%d",
                db_run.run_id,
                graph.get("version", 0),
            )

        runner = cls(
            effective_graph,
            json.loads(db_run.trigger_event),
            supervisor,
            db,
            on_progress,
            event_bus=event_bus,
        )
        runner.run_id = db_run.run_id
        runner.messages = json.loads(db_run.conversation_history)
        runner.node_trace = [NodeTraceEntry(**entry) for entry in json.loads(db_run.node_trace)]
        runner.tokens_used = db_run.tokens_used

        # Transition from PAUSED → RUNNING via EVENT_RESUMED
        runner._status = PlaybookRunStatus.PAUSED
        runner._transition(PlaybookRunEvent.EVENT_RESUMED)

        # Inject event data into conversation so the LLM has context
        # about what happened (e.g., which stage completed, which tasks
        # finished, what the results were).
        event_summary = json.dumps(event_data, indent=2)
        event_type = db_run.waiting_for_event or "unknown"
        runner.messages.append(
            {
                "role": "user",
                "content": (
                    f"[Event received: {event_type}]\n"
                    f"The event you were waiting for has fired. "
                    f"Here is the event data:\n\n{event_summary}"
                ),
            }
        )

        # Update DB status and clear waiting_for_event
        if db:
            await db.update_playbook_run(
                db_run.run_id,
                status=runner._status.value,
                waiting_for_event=None,
            )

        # Emit playbook.run.resumed event for audit/notification
        paused_node_id = db_run.current_node
        if paused_node_id:
            payload: dict[str, Any] = {
                "playbook_id": runner._playbook_id,
                "run_id": runner.run_id,
                "node_id": paused_node_id,
                "resumed_by_event": event_type,
            }
            await runner._emit_bus_event("playbook.run.resumed", payload)

        # Find the next node after the paused one
        if not paused_node_id:
            return await runner._fail(
                db_run,
                "Cannot resume from event: no current_node recorded",
                db_run.started_at,
                event=PlaybookRunEvent.GRAPH_ERROR,
            )

        paused_node = effective_graph["nodes"].get(paused_node_id)
        if not paused_node:
            return await runner._fail(
                db_run,
                f"Cannot resume from event: node '{paused_node_id}' not found",
                db_run.started_at,
                event=PlaybookRunEvent.GRAPH_ERROR,
            )

        # Get the last response from conversation for transition evaluation
        last_response = ""
        for msg in reversed(runner.messages):
            if msg["role"] == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_response = content
                    break

        # Evaluate transition from paused node (event data is now in context)
        try:
            next_node_id, _t_method = await runner._evaluate_transition(
                paused_node_id, paused_node, last_response
            )
        except Exception as exc:
            return await runner._fail(
                db_run,
                f"Transition from event-paused node failed: {exc}",
                db_run.started_at,
                event=PlaybookRunEvent.TRANSITION_FAILED,
            )

        if next_node_id is None:
            # No transition — completed
            runner._transition(PlaybookRunEvent.TERMINAL_REACHED)
            completed_at = time.time()
            trace_dicts = [runner._trace_to_dict(t) for t in runner.node_trace]
            if db:
                await db.update_playbook_run(
                    db_run.run_id,
                    status=runner._status.value,
                    conversation_history=json.dumps(runner.messages),
                    node_trace=json.dumps(trace_dicts),
                    tokens_used=runner.tokens_used,
                    completed_at=completed_at,
                )
            await runner._emit_completed_event(started_at=db_run.started_at)
            return RunResult(
                run_id=db_run.run_id,
                status=runner._status.value,
                node_trace=trace_dicts,
                tokens_used=runner.tokens_used,
            )

        # Continue walking the graph from the next node
        started_at = db_run.started_at
        current_node_id = next_node_id
        final_response: str | None = None

        while True:
            node = effective_graph["nodes"].get(current_node_id)
            if node is None:
                return await runner._fail(
                    db_run,
                    f"Node '{current_node_id}' not found in graph",
                    started_at,
                    event=PlaybookRunEvent.GRAPH_ERROR,
                )

            if node.get("terminal"):
                break

            if runner._max_tokens is not None and runner.tokens_used >= runner._max_tokens:
                return await runner._fail(
                    db_run,
                    (
                        f"token_budget_exceeded: budget {runner._max_tokens} "
                        f"exhausted before node '{current_node_id}' "
                        f"(used {runner.tokens_used})"
                    ),
                    started_at,
                    event=PlaybookRunEvent.BUDGET_EXCEEDED,
                )

            try:
                response = await runner._execute_node(current_node_id, node, db_run)
                final_response = response
            except Exception as exc:
                return await runner._fail(
                    db_run,
                    f"Node '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.NODE_FAILED,
                )

            if runner._max_tokens is not None and runner.tokens_used >= runner._max_tokens:
                return await runner._fail(
                    db_run,
                    (
                        f"token_budget_exceeded: budget {runner._max_tokens} "
                        f"exhausted after node '{current_node_id}' "
                        f"(used {runner.tokens_used})"
                    ),
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.BUDGET_EXCEEDED,
                )

            if node.get("wait_for_human"):
                return await runner._pause(db_run, current_node_id, started_at)

            wait_event = node.get("wait_for_event")
            if wait_event:
                evt = wait_event if isinstance(wait_event, str) else wait_event.get("event", "")
                return await runner._pause_for_event(db_run, current_node_id, started_at, evt)

            try:
                next_node_id, t_method = await runner._evaluate_transition(
                    current_node_id, node, response
                )
            except Exception as exc:
                return await runner._fail(
                    db_run,
                    f"Transition from '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.TRANSITION_FAILED,
                )

            if runner.node_trace:
                runner.node_trace[-1].transition_to = next_node_id
                runner.node_trace[-1].transition_method = t_method

            if next_node_id is None:
                break

            current_node_id = next_node_id

        # Completed successfully
        runner._transition(PlaybookRunEvent.TERMINAL_REACHED)
        completed_at = time.time()
        trace_dicts = [runner._trace_to_dict(t) for t in runner.node_trace]

        if db:
            await db.update_playbook_run(
                db_run.run_id,
                status=runner._status.value,
                conversation_history=json.dumps(runner.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=runner.tokens_used,
                completed_at=completed_at,
            )

        await runner._emit_completed_event(
            final_context=final_response,
            started_at=db_run.started_at,
        )

        return RunResult(
            run_id=db_run.run_id,
            status=runner._status.value,
            node_trace=trace_dicts,
            tokens_used=runner.tokens_used,
            final_response=final_response,
        )

    # ------------------------------------------------------------------
    # Timeout handling (roadmap 5.4.4)
    # ------------------------------------------------------------------

    @classmethod
    async def handle_timeout(
        cls,
        db_run: PlaybookRun,
        graph: dict,
        supervisor: Supervisor | None = None,
        db: DatabaseBackend | None = None,
        on_progress: Callable[[str, str | None], Awaitable[None]] | None = None,
        event_bus: EventBus | None = None,
    ) -> RunResult:
        """Handle a paused run whose pause timeout has expired.

        Resolves the pause timeout for the paused node (node-level override →
        playbook-level override → default 24h).  If the node defines an
        ``on_timeout`` target, the run transitions to that node and continues
        graph execution.  Otherwise, the run is marked as ``timed_out``.

        This method is called by the orchestrator's background sweep or by
        ``_cmd_resume_playbook()`` when a resume attempt arrives too late.

        Parameters
        ----------
        db_run:
            The persisted :class:`PlaybookRun` with status ``"paused"``.
        graph:
            The compiled playbook graph (fallback if no pinned_graph).
        supervisor:
            Supervisor instance — only needed when an ``on_timeout`` node
            requires LLM calls.  May be *None* for simple timeout-to-fail.
        db:
            Database backend for persisting updates.
        on_progress:
            Optional progress callback.
        event_bus:
            Optional EventBus for emitting timeout events.
        """
        # Resolve effective graph (version pinning)
        if db_run.pinned_graph:
            effective_graph = json.loads(db_run.pinned_graph)
        else:
            effective_graph = graph

        paused_node_id = db_run.current_node
        paused_at = db_run.paused_at or db_run.started_at

        # Resolve timeout: node-level → playbook-level → default 24h
        timeout_seconds = cls._resolve_pause_timeout(effective_graph, paused_node_id)

        # Check if the node defines an on_timeout target
        on_timeout_node = None
        if paused_node_id:
            paused_node = effective_graph.get("nodes", {}).get(paused_node_id, {})
            on_timeout_node = paused_node.get("on_timeout")

        if on_timeout_node and on_timeout_node in effective_graph.get("nodes", {}):
            # Transition to the timeout node and continue graph execution
            if supervisor is None:
                # Cannot transition without a supervisor — fall through to fail
                logger.warning(
                    "Run %s has on_timeout='%s' but no Supervisor available — marking as timed_out",
                    db_run.run_id,
                    on_timeout_node,
                )
            else:
                return await cls._resume_at_timeout_node(
                    db_run=db_run,
                    effective_graph=effective_graph,
                    supervisor=supervisor,
                    timeout_node_id=on_timeout_node,
                    paused_node_id=paused_node_id,
                    paused_at=paused_at,
                    timeout_seconds=timeout_seconds,
                    db=db,
                    on_progress=on_progress,
                    event_bus=event_bus,
                )

        # No timeout node — mark as timed_out
        runner = cls(
            effective_graph,
            json.loads(db_run.trigger_event),
            supervisor or _DummySupervisor(),  # type: ignore[arg-type]
            db,
            on_progress,
            event_bus=event_bus,
        )
        runner.run_id = db_run.run_id
        runner.messages = json.loads(db_run.conversation_history)
        runner.node_trace = [NodeTraceEntry(**entry) for entry in json.loads(db_run.node_trace)]
        runner.tokens_used = db_run.tokens_used
        runner._status = PlaybookRunStatus.PAUSED

        # Transition PAUSED → TIMED_OUT
        runner._transition(PlaybookRunEvent.PAUSE_TIMEOUT)
        completed_at = time.time()
        error = f"Pause timeout exceeded ({timeout_seconds}s)"

        trace_dicts = [runner._trace_to_dict(t) for t in runner.node_trace]
        if db:
            await db.update_playbook_run(
                db_run.run_id,
                status=runner._status.value,
                completed_at=completed_at,
                error=error,
            )

        if on_progress:
            await on_progress("playbook_timed_out", paused_node_id)

        await runner._emit_timed_out_event(
            node_id=paused_node_id or "<unknown>",
            paused_at=paused_at,
            timeout_seconds=timeout_seconds,
        )

        return RunResult(
            run_id=db_run.run_id,
            status=runner._status.value,
            node_trace=trace_dicts,
            tokens_used=runner.tokens_used,
            error=error,
        )

    @classmethod
    async def _resume_at_timeout_node(
        cls,
        *,
        db_run: PlaybookRun,
        effective_graph: dict,
        supervisor: Supervisor,
        timeout_node_id: str,
        paused_node_id: str | None,
        paused_at: float,
        timeout_seconds: int,
        db: DatabaseBackend | None = None,
        on_progress: Callable[[str, str | None], Awaitable[None]] | None = None,
        event_bus: EventBus | None = None,
    ) -> RunResult:
        """Transition a timed-out run to a timeout node and continue execution.

        Reconstructs the runner from the persisted run, injects a timeout
        context message, and walks the graph starting from the timeout node.
        """
        runner = cls(
            effective_graph,
            json.loads(db_run.trigger_event),
            supervisor,
            db,
            on_progress,
            event_bus=event_bus,
        )
        runner.run_id = db_run.run_id
        runner.messages = json.loads(db_run.conversation_history)
        runner.node_trace = [NodeTraceEntry(**entry) for entry in json.loads(db_run.node_trace)]
        runner.tokens_used = db_run.tokens_used
        runner._status = PlaybookRunStatus.PAUSED

        # Transition PAUSED → RUNNING (via HUMAN_RESUMED) — we re-use this
        # because the run continues execution, just with timeout context
        # instead of human input.
        runner._transition(PlaybookRunEvent.HUMAN_RESUMED)

        # Inject timeout context into conversation
        runner.messages.append(
            {
                "role": "user",
                "content": (
                    f"[Pause timeout]: The human review at node "
                    f"'{paused_node_id}' timed out after {timeout_seconds}s. "
                    f"No human input was received. The playbook is now "
                    f"continuing from the designated timeout node "
                    f"'{timeout_node_id}'."
                ),
            }
        )

        # Update DB status to running
        if db:
            await db.update_playbook_run(db_run.run_id, status=runner._status.value)

        # Emit timeout event with transition info
        await runner._emit_timed_out_event(
            node_id=paused_node_id or "<unknown>",
            paused_at=paused_at,
            timeout_seconds=timeout_seconds,
            transitioned_to=timeout_node_id,
        )

        if on_progress:
            await on_progress("playbook_timeout_transition", timeout_node_id)

        # Walk graph from the timeout node
        started_at = db_run.started_at
        current_node_id = timeout_node_id
        final_response: str | None = None

        while True:
            node = effective_graph["nodes"].get(current_node_id)
            if node is None:
                return await runner._fail(
                    db_run,
                    f"Node '{current_node_id}' not found in graph",
                    started_at,
                    event=PlaybookRunEvent.GRAPH_ERROR,
                )

            if node.get("terminal"):
                break

            if runner._max_tokens is not None and runner.tokens_used >= runner._max_tokens:
                return await runner._fail(
                    db_run,
                    (
                        f"token_budget_exceeded: budget {runner._max_tokens} "
                        f"exhausted before node '{current_node_id}' "
                        f"(used {runner.tokens_used})"
                    ),
                    started_at,
                    event=PlaybookRunEvent.BUDGET_EXCEEDED,
                )

            try:
                response = await runner._execute_node(current_node_id, node, db_run)
                final_response = response
            except Exception as exc:
                return await runner._fail(
                    db_run,
                    f"Node '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.NODE_FAILED,
                )

            if runner._max_tokens is not None and runner.tokens_used >= runner._max_tokens:
                return await runner._fail(
                    db_run,
                    (
                        f"token_budget_exceeded: budget {runner._max_tokens} "
                        f"exhausted after node '{current_node_id}' "
                        f"(used {runner.tokens_used})"
                    ),
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.BUDGET_EXCEEDED,
                )

            if node.get("wait_for_human"):
                return await runner._pause(db_run, current_node_id, started_at)

            wait_event = node.get("wait_for_event")
            if wait_event:
                event_type = (
                    wait_event if isinstance(wait_event, str) else wait_event.get("event", "")
                )
                return await runner._pause_for_event(
                    db_run, current_node_id, started_at, event_type
                )

            try:
                next_node_id, t_method = await runner._evaluate_transition(
                    current_node_id, node, response
                )
            except Exception as exc:
                return await runner._fail(
                    db_run,
                    f"Transition from '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                    event=PlaybookRunEvent.TRANSITION_FAILED,
                )

            if runner.node_trace:
                runner.node_trace[-1].transition_to = next_node_id
                runner.node_trace[-1].transition_method = t_method

            if next_node_id is None:
                break

            current_node_id = next_node_id

        # Completed successfully
        runner._transition(PlaybookRunEvent.TERMINAL_REACHED)
        completed_at = time.time()
        trace_dicts = [runner._trace_to_dict(t) for t in runner.node_trace]

        if db:
            await db.update_playbook_run(
                db_run.run_id,
                status=runner._status.value,
                conversation_history=json.dumps(runner.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=runner.tokens_used,
                completed_at=completed_at,
            )

        await runner._emit_completed_event(
            final_context=final_response,
            started_at=db_run.started_at,
        )

        return RunResult(
            run_id=db_run.run_id,
            status=runner._status.value,
            node_trace=trace_dicts,
            tokens_used=runner.tokens_used,
            final_response=final_response,
        )

    @staticmethod
    def _resolve_pause_timeout(graph: dict, node_id: str | None) -> int:
        """Resolve the effective pause timeout for a paused node.

        Priority: node-level ``pause_timeout_seconds`` → playbook-level
        ``pause_timeout_seconds`` → default 86400 (24 hours).
        """
        default_timeout = 86400  # 24 hours

        if node_id:
            node = graph.get("nodes", {}).get(node_id, {})
            node_timeout = node.get("pause_timeout_seconds")
            if node_timeout is not None:
                return int(node_timeout)

        playbook_timeout = graph.get("pause_timeout_seconds")
        if playbook_timeout is not None:
            return int(playbook_timeout)

        return default_timeout

    # ------------------------------------------------------------------
    # Internal: node execution
    # ------------------------------------------------------------------

    async def _execute_node(
        self,
        node_id: str,
        node: dict,
        db_run: PlaybookRun,
    ) -> str:
        """Execute a single node and return the Supervisor's response.

        Each node gets a fresh LLM context built from the seed message
        and compact prior node outputs — NOT the accumulated transcript.

        If the node has a ``for_each`` directive, the prompt executes once
        per item in the source array, collecting results.

        If the node has an ``output`` directive, structured data is extracted
        from tool results and stored in ``node_outputs`` for downstream nodes.
        """
        trace_entry = NodeTraceEntry(node_id=node_id, started_at=time.time())
        self.node_trace.append(trace_entry)

        if self.on_progress:
            await self.on_progress("node_started", node_id)

        # Dry-run mode: skip real LLM calls and return a simulated response.
        if self._dry_run:
            prompt = self._build_node_prompt(node_id, node)
            response = f"[dry-run] Simulated response for node '{node_id}'"

            self.messages.append({"role": "user", "content": prompt})
            self.messages.append({"role": "assistant", "content": response})
            self._store_node_output(node_id, node, response)

            trace_entry.completed_at = time.time()
            trace_entry.status = "completed"

            if self.on_progress:
                await self.on_progress("node_completed", node_id)

            return response

        # Check for for_each iteration
        for_each = node.get("for_each")
        if for_each:
            response = await self._execute_for_each(node_id, node, db_run, trace_entry)
        else:
            response = await self._execute_single_node(node_id, node, trace_entry)

        # Track tokens in daily tracker
        if self._daily_token_tracker is not None:
            self._daily_token_tracker.add_tokens(trace_entry.tokens_used)

        # Budget warning
        if self._max_tokens is not None and self._max_tokens > 0:
            usage_pct = self.tokens_used / self._max_tokens
            if usage_pct >= 0.9 and self.tokens_used < self._max_tokens:
                logger.warning(
                    "Playbook '%s' run %s approaching token budget: %d/%d (%.0f%%)",
                    self._playbook_id,
                    self.run_id,
                    self.tokens_used,
                    self._max_tokens,
                    usage_pct * 100,
                )

        # Update trace
        trace_entry.completed_at = time.time()
        trace_entry.status = "completed"

        # Persist intermediate state
        if self.db:
            trace_dicts = [self._trace_to_dict(t) for t in self.node_trace]
            await self.db.update_playbook_run(
                self.run_id,
                current_node=node_id,
                conversation_history=json.dumps(self.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=self.tokens_used,
            )

        if self.on_progress:
            await self.on_progress("node_completed", node_id)

        return response

    async def _execute_single_node(
        self,
        node_id: str,
        node: dict,
        trace_entry: NodeTraceEntry,
        extra_vars: dict | None = None,
    ) -> str:
        """Execute a single node prompt with fresh per-node context.

        This is the core execution unit. Called once for simple nodes,
        or once per item for for_each nodes.
        """
        prompt = self._build_node_prompt(node_id, node, extra_vars)

        # Resolve per-node LLM config, defaulting to higher max_tokens for playbooks
        pb_max = self.supervisor.config.chat_provider.playbook_max_tokens
        node_llm_config = self._resolve_node_llm_config(node)
        if node_llm_config is None:
            node_llm_config = {"max_tokens": pb_max}
        elif "max_tokens" not in node_llm_config:
            node_llm_config = {**node_llm_config, "max_tokens": pb_max}

        supervisor_progress = self._make_supervisor_progress(node_id)

        # Build fresh per-node context (NOT the accumulated transcript)
        context = self._build_node_context()

        timeout = node.get("timeout_seconds")
        try:
            coro = self.supervisor.chat(
                text=prompt,
                user_name=f"playbook-runner:{node_id}",
                history=context,
                on_progress=supervisor_progress,
                llm_config=node_llm_config,
            )
            if timeout:
                response = await asyncio.wait_for(coro, timeout=timeout)
            else:
                response = await coro
        except asyncio.TimeoutError:
            trace_entry.status = "failed"
            raise TimeoutError(f"Node '{node_id}' timed out after {timeout}s") from None

        # Extract structured output from tool results
        output = self._extract_output(node, response)
        self._store_node_output(node_id, node, output)

        # Track in messages for DB persistence (compact: prompt + response only)
        self.messages.append({"role": "user", "content": prompt})
        self.messages.append({"role": "assistant", "content": response})

        # Track tokens
        token_estimate = _estimate_tokens(prompt, response)
        self.tokens_used += token_estimate
        trace_entry.tokens_used += token_estimate

        return response

    async def _execute_for_each(
        self,
        node_id: str,
        node: dict,
        db_run: PlaybookRun,
        trace_entry: NodeTraceEntry,
    ) -> str:
        """Execute a node once per item in a for_each source array.

        Results are optionally collected into a named array in node_outputs.
        """
        for_each = node["for_each"]
        source_path = for_each["source"]
        item_var = for_each["as"]
        collect_name = for_each.get("collect")

        # Resolve the source array
        items = self._resolve_output_var(source_path)
        logger.info(
            "for_each: source='%s' resolved to %s (%s)",
            source_path,
            type(items).__name__,
            len(items) if isinstance(items, list) else repr(items)[:100],
        )
        if not isinstance(items, list):
            logger.warning(
                "for_each source '%s' resolved to %s (not a list) — skipping node '%s'",
                source_path,
                type(items).__name__ if items is not None else "None",
                node_id,
            )
            return f"for_each source '{source_path}' is not a list — skipped"

        # Apply filter if present
        filter_expr = for_each.get("filter")
        if filter_expr and items:
            items = [item for item in items if self._evaluate_filter(filter_expr, item, item_var)]
            logger.info("for_each: filter '%s' reduced to %d items", filter_expr, len(items))

        collected: list[Any] = []
        responses: list[str] = []

        for i, item in enumerate(items):
            if self.on_progress:
                item_label = item.get("id", item.get("name", str(i))) if isinstance(item, dict) else str(i)
                await self.on_progress("for_each_item", f"{node_id}[{item_label}]")

            extra_vars = {item_var: item, "_index": i, "_total": len(items)}
            response = await self._execute_single_node(
                node_id=f"{node_id}[{i}]",
                node=node,
                trace_entry=trace_entry,
                extra_vars=extra_vars,
            )
            responses.append(response)

            # Collect what _execute_single_node stored in node_outputs.
            # It stores under output.as (if set) or the iteration node_id.
            output_spec = node.get("output")
            iter_node_id = f"{node_id}[{i}]"
            stored_key = (output_spec.get("as") if output_spec else None) or iter_node_id
            item_result = self.node_outputs.pop(stored_key, response)
            # Auto-upgrade raw text to parsed JSON when the iteration response
            # is a structured object (common: the LLM returned JSON in text,
            # and the node had no `output.extract` to parse it). Downstream
            # `{{item.field}}` templates need dicts, not strings.
            if isinstance(item_result, str):
                parsed = _parse_json_from_text(item_result)
                if isinstance(parsed, (dict, list)):
                    item_result = parsed
            collected.append(item_result)

        # Store collected results
        if collect_name:
            self.node_outputs[collect_name] = collected

        return f"Completed {len(items)} iterations of {node_id}"

    # ------------------------------------------------------------------
    # Internal: graph navigation helpers
    # ------------------------------------------------------------------

    def _find_entry_node(self) -> str | None:
        """Return the ID of the entry node (``entry: true``)."""
        nodes = self.graph.get("nodes", {})
        for node_id, node in nodes.items():
            if node.get("entry"):
                return node_id
        # Fallback: if there's exactly one non-terminal node, use it
        non_terminal = [nid for nid, n in nodes.items() if not n.get("terminal")]
        if len(non_terminal) == 1:
            return non_terminal[0]
        return None

    # ------------------------------------------------------------------
    # Internal: terminal states
    # ------------------------------------------------------------------

    async def _fail(
        self,
        db_run: PlaybookRun,
        error: str,
        started_at: float,
        current_node: str | None = None,
        event: PlaybookRunEvent = PlaybookRunEvent.NODE_FAILED,
    ) -> RunResult:
        """Mark the run as failed and persist.

        The target status is determined by the state machine based on the
        *event*.  Both ``BUDGET_EXCEEDED`` and ``NODE_FAILED`` produce
        ``failed`` status.  For budget exceeded, the error string starts
        with ``token_budget_exceeded:`` per spec §6.
        """
        # Validate transition via state machine
        self._transition(event)
        status = self._status.value

        logger.error(
            "Playbook '%s' run %s %s: %s",
            self._playbook_id,
            self.run_id,
            status,
            error,
        )

        trace_dicts = [self._trace_to_dict(t) for t in self.node_trace]

        # Mark the last trace entry as failed if it's still running
        if self.node_trace and self.node_trace[-1].status == "running":
            self.node_trace[-1].status = "failed"
            self.node_trace[-1].completed_at = time.time()
            trace_dicts = [self._trace_to_dict(t) for t in self.node_trace]

        if self.db:
            await self.db.update_playbook_run(
                self.run_id,
                status=status,
                current_node=current_node,
                conversation_history=json.dumps(self.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=self.tokens_used,
                completed_at=time.time(),
                error=error,
            )

        if self.on_progress:
            await self.on_progress("playbook_failed", error)

        # Emit playbook.run.failed event (roadmap 5.3.6)
        await self._emit_failed_event(
            failed_at_node=current_node,
            error=error,
            started_at=started_at,
        )

        return RunResult(
            run_id=self.run_id,
            status=status,
            node_trace=trace_dicts,
            tokens_used=self.tokens_used,
            error=error,
        )

    async def _pause(
        self,
        db_run: PlaybookRun,
        node_id: str,
        started_at: float,
    ) -> RunResult:
        """Mark the run as paused for human review.

        Persists the full run state (conversation history, node trace, token
        usage) so the run can be resumed later via :meth:`resume`.  Emits a
        ``playbook.run.paused`` event on the EventBus so that notification
        systems (Discord, dashboard) can surface the review request to a
        human.  See spec §9.
        """
        # Validate transition via state machine
        self._transition(PlaybookRunEvent.HUMAN_WAIT)

        paused_at = time.time()

        logger.info(
            "Playbook '%s' run %s paused at node '%s' for human review",
            self._playbook_id,
            self.run_id,
            node_id,
        )

        trace_dicts = [self._trace_to_dict(t) for t in self.node_trace]

        if self.db:
            await self.db.update_playbook_run(
                self.run_id,
                status=self._status.value,
                current_node=node_id,
                conversation_history=json.dumps(self.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=self.tokens_used,
                paused_at=paused_at,
            )

        if self.on_progress:
            await self.on_progress("playbook_paused", node_id)

        # Emit playbook.run.paused event (spec §9) so notification systems
        # (Discord, dashboard) can surface the review request.
        await self._emit_paused_event(
            node_id=node_id,
            started_at=started_at,
            paused_at=paused_at,
        )

        return RunResult(
            run_id=self.run_id,
            status=self._status.value,
            node_trace=trace_dicts,
            tokens_used=self.tokens_used,
        )

    async def _pause_for_event(
        self,
        db_run: PlaybookRun,
        node_id: str,
        started_at: float,
        event_type: str,
    ) -> RunResult:
        """Pause the run until an external event fires.

        Similar to :meth:`_pause` (human-in-the-loop) but the run is waiting
        for a system event (e.g. ``workflow.stage.completed``) rather than
        human input.  The ``waiting_for_event`` field is persisted so the
        :class:`WorkflowStageResumeHandler` (or any event-driven handler) can
        find and resume the correct run when the event fires.

        This implements long-running playbook support for coordination
        workflows that span multiple stages (Roadmap 7.5.5).  A coordination
        playbook creates tasks for a stage, then pauses at a
        ``wait_for_event`` node until ``workflow.stage.completed`` fires,
        at which point it resumes to create the next stage.

        Unlike ``wait_for_human``, no notification is emitted to human
        review channels — the resumption is fully automated.
        """
        self._transition(PlaybookRunEvent.EVENT_WAIT)

        paused_at = time.time()

        logger.info(
            "Playbook '%s' run %s paused at node '%s' waiting for event '%s'",
            self._playbook_id,
            self.run_id,
            node_id,
            event_type,
        )

        trace_dicts = [self._trace_to_dict(t) for t in self.node_trace]

        if self.db:
            await self.db.update_playbook_run(
                self.run_id,
                status=self._status.value,
                current_node=node_id,
                conversation_history=json.dumps(self.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=self.tokens_used,
                paused_at=paused_at,
                waiting_for_event=event_type,
            )

        if self.on_progress:
            await self.on_progress("playbook_paused_for_event", node_id)

        # Emit playbook.run.paused event with event context (no human
        # notification — event-triggered resumption is automated).
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
            "node_id": node_id,
            "waiting_for_event": event_type,
        }
        if started_at is not None:
            payload["running_seconds"] = round(paused_at - started_at, 2)
        payload["paused_at"] = paused_at
        payload["tokens_used"] = self.tokens_used
        await self._emit_bus_event("playbook.run.paused", payload)

        return RunResult(
            run_id=self.run_id,
            status=self._status.value,
            node_trace=trace_dicts,
            tokens_used=self.tokens_used,
        )

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _trace_to_dict(entry: NodeTraceEntry) -> dict:
        """Convert a NodeTraceEntry to a JSON-serialisable dict."""
        d: dict = {
            "node_id": entry.node_id,
            "started_at": entry.started_at,
            "completed_at": entry.completed_at,
            "status": entry.status,
        }
        if entry.transition_to is not None:
            d["transition_to"] = entry.transition_to
        if entry.transition_method is not None:
            d["transition_method"] = entry.transition_method
        if entry.tokens_used:
            d["tokens_used"] = entry.tokens_used
        return d

    # ------------------------------------------------------------------
    # Dry-run simulation (roadmap 5.5.2)
    # ------------------------------------------------------------------

    @classmethod
    async def dry_run(
        cls,
        graph: dict,
        event: dict,
        on_progress: Callable[[str, str | None], Awaitable[None]] | None = None,
    ) -> RunResult:
        """Simulate playbook execution with a mock event, producing no side effects.

        Walks the compiled playbook graph from entry to terminal node without
        making real LLM calls, writing to the database, or emitting events.
        Returns the node trace showing the path that *would* be taken.

        **Design decisions:**

        - **No LLM calls:** Node execution returns a simulated response
          (``"[dry-run] Simulated response for node '{node_id}'"``).
        - **No DB writes:** ``db`` is set to ``None``.
        - **No event emission:** ``event_bus`` is set to ``None``.
        - **Transition strategy:** Unconditional ``goto`` edges work normally.
          For conditional transitions, structured conditions are evaluated
          against the simulated response (most will not match), then the
          first natural-language transition is followed without an LLM call.
          If no natural-language conditions exist, the ``otherwise`` fallback
          is used.
        - **Human-in-the-loop:** ``wait_for_human`` nodes are executed
          normally (simulated) and the graph continues past them — the run
          does not pause.
        - **Token budget:** Skipped (no real tokens are consumed).

        Parameters
        ----------
        graph:
            The compiled playbook JSON (dict) to simulate.
        event:
            The mock trigger event data (dict).
        on_progress:
            Optional async callback for progress reporting.

        Returns
        -------
        RunResult
            The simulation result with ``status``, ``node_trace`` (the
            simulated path), and ``tokens_used`` (always 0).

        See Also
        --------
        :meth:`run` : The real execution method.
        docs/specs/design/playbooks.md §15, §19 (Open Questions #2).
        """
        runner = cls(
            graph,
            event,
            _DummySupervisor(),  # type: ignore[arg-type]
            db=None,
            on_progress=on_progress,
            event_bus=None,
        )
        runner._dry_run = True
        return await runner.run()
