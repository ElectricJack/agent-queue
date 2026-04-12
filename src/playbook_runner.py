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

See also: :mod:`src.playbook_handler` (vault watcher / compilation dispatch).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.models import PlaybookRun, PlaybookRunEvent, PlaybookRunStatus
from src.playbook_state_machine import (
    InvalidPlaybookRunTransition,
    validate_transition,
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


# ---------------------------------------------------------------------------
# Daily token cap helper
# ---------------------------------------------------------------------------


def _midnight_today() -> float:
    """Return the Unix timestamp for midnight (00:00) of the current local day.

    Used by the daily playbook token cap (roadmap 5.2.8) to determine
    the start of the accounting window.
    """
    today = datetime.date.today()
    return datetime.datetime.combine(today, datetime.time.min).timestamp()


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def _estimate_tokens(*texts: str) -> int:
    """Rough token estimate (~4 chars per token) for budget tracking.

    This is intentionally approximate — the real token count comes from the
    LLM provider, but we don't have access to that at the executor level.
    The estimate is used for budget enforcement which is meant to be a
    guardrail, not an exact meter.
    """
    total_chars = sum(len(t) for t in texts if t)
    return max(1, total_chars // 4)


# ---------------------------------------------------------------------------
# Daily token tracking (global playbook cap, spec §6 Token Budget)
# ---------------------------------------------------------------------------


class DailyTokenTracker:
    """Track cumulative playbook token usage per calendar day.

    Used to enforce a global daily token cap (``max_daily_playbook_tokens``
    in config) across all playbook runs.  The tracker stores per-day totals
    and automatically resets when the date changes (at midnight by default,
    or at a configured ``reset_hour``).

    Thread-safety note: this class is *not* thread-safe but is designed for
    use in a single-threaded asyncio loop.
    """

    def __init__(self, *, reset_hour: int = 0) -> None:
        """Initialise the tracker.

        Parameters
        ----------
        reset_hour:
            Hour of day (0–23) when the daily counter resets.  Defaults to
            0 (midnight).
        """
        self._usage: dict[str, int] = {}
        self._reset_hour: int = reset_hour

    @property
    def reset_hour(self) -> int:
        return self._reset_hour

    @reset_hour.setter
    def reset_hour(self, value: int) -> None:
        self._reset_hour = value

    def _today_key(self, *, now: datetime.datetime | None = None) -> str:
        """Return the date key for the current accounting day.

        If *now* is provided it is used instead of ``datetime.datetime.now()``
        (useful for testing).
        """
        now = now or datetime.datetime.now()
        # Subtract reset_hour so that e.g. 02:00 with reset_hour=6 still
        # belongs to the previous calendar day.
        adjusted = now - datetime.timedelta(hours=self._reset_hour)
        return adjusted.strftime("%Y-%m-%d")

    def add_tokens(self, count: int, *, now: datetime.datetime | None = None) -> None:
        """Record *count* tokens for the current day."""
        key = self._today_key(now=now)
        self._usage[key] = self._usage.get(key, 0) + count

    def get_usage(self, *, now: datetime.datetime | None = None) -> int:
        """Return total tokens used today."""
        key = self._today_key(now=now)
        return self._usage.get(key, 0)


# ---------------------------------------------------------------------------
# Expression evaluation helpers (structured transitions §6, roadmap 5.2.5)
# ---------------------------------------------------------------------------

# Pattern for comparison expressions: variable op literal
# Supports: task.status == "completed", output.count > 0, response != "error"
_EXPR_PATTERN = re.compile(
    r"^\s*"
    r"(?P<var>[a-zA-Z_][a-zA-Z0-9_.]*)"  # dotted variable path
    r"\s*"
    r"(?P<op>==|!=|>=|<=|>|<)"  # comparison operator
    r"\s*"
    r'(?P<literal>"(?:[^"\\]|\\.)*"'  # double-quoted string
    r"|'(?:[^'\\]|\\.)*'"  # single-quoted string
    r"|-?\d+(?:\.\d+)?"  # number (int or float)
    r"|true|false|null)"  # boolean / null
    r"\s*$",
    re.IGNORECASE,
)


def _dot_get(data: dict, path: str) -> tuple[Any, bool]:
    """Resolve a dot-separated path against a nested dict.

    Returns ``(value, True)`` on success, ``(None, False)`` if any
    segment is missing or the data is not traversable.
    """
    current: Any = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None, False
    return current, True


def _parse_literal(raw: str) -> str | int | float | bool | None:
    """Parse a literal token from an expression string.

    Handles double-quoted strings, single-quoted strings, integers,
    floats, booleans (``true``/``false``), and ``null``.
    """
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        inner = raw[1:-1]
        return inner.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")

    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null":
        return None

    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def _compare(left: Any, op: str, right: Any) -> bool:
    """Apply a comparison operator, with numeric coercion for ordering ops."""
    # For ordering operators, attempt numeric conversion on type mismatch
    if op in (">", "<", ">=", "<="):
        try:
            if isinstance(left, str) and isinstance(right, (int, float)):
                left = type(right)(left)
            elif isinstance(right, str) and isinstance(left, (int, float)):
                right = type(left)(right)
        except (ValueError, TypeError):
            pass

    try:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
    except TypeError:
        return False
    return False


# ---------------------------------------------------------------------------
# Event → fallback status (used when the state machine rejects a transition)
# ---------------------------------------------------------------------------

_EVENT_FALLBACK_STATUS: dict[PlaybookRunEvent, PlaybookRunStatus] = {
    PlaybookRunEvent.TERMINAL_REACHED: PlaybookRunStatus.COMPLETED,
    PlaybookRunEvent.NODE_FAILED: PlaybookRunStatus.FAILED,
    PlaybookRunEvent.TRANSITION_FAILED: PlaybookRunStatus.FAILED,
    PlaybookRunEvent.GRAPH_ERROR: PlaybookRunStatus.FAILED,
    PlaybookRunEvent.BUDGET_EXCEEDED: PlaybookRunStatus.FAILED,
    PlaybookRunEvent.HUMAN_WAIT: PlaybookRunStatus.PAUSED,
    PlaybookRunEvent.HUMAN_RESUMED: PlaybookRunStatus.RUNNING,
    PlaybookRunEvent.EVENT_WAIT: PlaybookRunStatus.PAUSED,
    PlaybookRunEvent.EVENT_RESUMED: PlaybookRunStatus.RUNNING,
    PlaybookRunEvent.PAUSE_TIMEOUT: PlaybookRunStatus.TIMED_OUT,
}


def _event_to_fallback_status(event: PlaybookRunEvent) -> PlaybookRunStatus:
    """Derive a reasonable target status from an event, bypassing the state machine.

    This is only used when the state machine rejects a transition (i.e., a
    bug in transition ordering).  The fallback ensures the runner can still
    complete without crashing.
    """
    return _EVENT_FALLBACK_STATUS[event]


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


class PlaybookRunner:
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
        # (see src/playbook_state_machine.py).
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

        Uses the formal state machine (:mod:`src.playbook_state_machine`) to
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
    # Event emission (roadmap 5.3.6)
    # ------------------------------------------------------------------

    async def _emit_bus_event(self, event_type: str, payload: dict) -> None:
        """Emit an event on the EventBus if one is configured.

        Silently ignores errors to avoid breaking the runner if a subscriber
        misbehaves.  The caller is responsible for building the payload —
        this helper only adds ``project_id`` from the trigger event when
        present (for scope-based filtering by downstream playbooks).
        """
        if self.event_bus is None:
            return
        # Inject project_id from the trigger event if available
        project_id = self.event.get("project_id")
        if project_id and "project_id" not in payload:
            payload["project_id"] = project_id
        try:
            await self.event_bus.emit(event_type, payload)
        except Exception:
            logger.warning(
                "Failed to emit %s for playbook run %s",
                event_type,
                self.run_id,
                exc_info=True,
            )

    async def _emit_completed_event(
        self,
        *,
        final_context: str | None = None,
        started_at: float | None = None,
    ) -> None:
        """Emit ``playbook.run.completed`` on the EventBus.

        See ``docs/specs/design/playbooks.md`` Section 7 — Event System.
        """
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
        }
        if final_context is not None:
            payload["final_context"] = final_context
        if started_at is not None:
            payload["duration_seconds"] = round(time.time() - started_at, 2)
        payload["tokens_used"] = self.tokens_used
        await self._emit_bus_event("playbook.run.completed", payload)

    async def _emit_failed_event(
        self,
        *,
        failed_at_node: str | None = None,
        error: str | None = None,
        started_at: float | None = None,
    ) -> None:
        """Emit ``playbook.run.failed`` on the EventBus.

        See ``docs/specs/design/playbooks.md`` Section 7 — Event System.
        """
        # Determine the node where failure occurred — fall back to the last
        # node in the trace, or "<unknown>" if the failure happened before
        # any node was reached (e.g. missing entry node, budget pre-check).
        if failed_at_node is None and self.node_trace:
            failed_at_node = self.node_trace[-1].node_id
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
            "failed_at_node": failed_at_node or "<unknown>",
        }
        if error is not None:
            payload["error"] = error
        if started_at is not None:
            payload["duration_seconds"] = round(time.time() - started_at, 2)
        payload["tokens_used"] = self.tokens_used
        await self._emit_bus_event("playbook.run.failed", payload)

    async def _emit_paused_event(
        self,
        *,
        node_id: str,
        started_at: float | None = None,
        paused_at: float | None = None,
    ) -> None:
        """Emit ``playbook.run.paused`` on the EventBus.

        Fired when execution pauses at a ``wait_for_human`` node (spec §9).
        Notification subscribers (Discord, dashboard) use this to surface
        the review request.  The payload includes the node ID, conversation
        context summary, and timing information so the notification can be
        informative without requiring a DB lookup.

        Also emits a ``notify.playbook_run_paused`` event (roadmap 5.4.2)
        so that notification transports (Discord, Telegram) can deliver a
        human-readable context summary to the reviewer.
        """
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
            "node_id": node_id,
        }
        if started_at is not None:
            payload["running_seconds"] = round((paused_at or time.time()) - started_at, 2)
        if paused_at is not None:
            payload["paused_at"] = paused_at
        payload["tokens_used"] = self.tokens_used
        # Include the last assistant response as context for the reviewer
        last_response = ""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    last_response = content[:2000]  # cap for event payload
                    break
        if last_response:
            payload["last_response"] = last_response
        await self._emit_bus_event("playbook.run.paused", payload)

        # Emit typed notification event for Discord/Telegram (roadmap 5.4.2)
        await self._emit_paused_notification(payload, last_response)

    async def _emit_paused_notification(
        self,
        raw_payload: dict[str, Any],
        last_response: str,
    ) -> None:
        """Emit ``notify.playbook_run_paused`` for notification transports.

        Converts the raw ``playbook.run.paused`` payload into a typed
        ``PlaybookRunPausedEvent`` and emits it on the EventBus so that
        Discord, Telegram, and other transports can render a rich
        human-review notification with context summary (roadmap 5.4.2).
        """
        from src.notifications.events import PlaybookRunPausedEvent

        event = PlaybookRunPausedEvent(
            playbook_id=raw_payload.get("playbook_id", ""),
            run_id=raw_payload.get("run_id", ""),
            node_id=raw_payload.get("node_id", ""),
            last_response=last_response,
            running_seconds=raw_payload.get("running_seconds", 0.0),
            tokens_used=raw_payload.get("tokens_used", 0),
            paused_at=raw_payload.get("paused_at", 0.0),
            project_id=raw_payload.get("project_id"),
        )
        await self._emit_bus_event("notify.playbook_run_paused", event.model_dump(mode="json"))

    async def _emit_resumed_event(
        self,
        *,
        node_id: str,
        human_input: str,
    ) -> None:
        """Emit ``playbook.run.resumed`` on the EventBus.

        Fired when a paused run is successfully resumed after human review
        (spec §9).  Downstream subscribers can use this for audit logging,
        dashboards, or chaining further automation.

        .. note::

           Prior to roadmap 5.4.3 this method emitted ``human.review.completed``.
           That event is now the **trigger** (fired by Dashboard/Discord to
           initiate the resume); ``playbook.run.resumed`` is the **notification**
           confirming the resume occurred.
        """
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
            "node_id": node_id,
            "decision": human_input[:2000],  # cap for event payload
        }
        await self._emit_bus_event("playbook.run.resumed", payload)

        # Also emit a typed notification event for Discord/Telegram transports
        await self._emit_resumed_notification(payload, human_input)

    async def _emit_resumed_notification(
        self,
        raw_payload: dict[str, Any],
        human_input: str,
    ) -> None:
        """Emit ``notify.playbook_run_resumed`` for notification transports.

        Converts the raw ``playbook.run.resumed`` payload into a typed
        ``PlaybookRunResumedEvent`` and emits it on the EventBus so that
        Discord, Telegram, and other transports can render a rich
        notification confirming the run was resumed (roadmap 5.4.3).
        """
        from src.notifications.events import PlaybookRunResumedEvent

        event = PlaybookRunResumedEvent(
            playbook_id=raw_payload.get("playbook_id", ""),
            run_id=raw_payload.get("run_id", ""),
            node_id=raw_payload.get("node_id", ""),
            decision=human_input[:2000],
            project_id=raw_payload.get("project_id"),
        )
        await self._emit_bus_event("notify.playbook_run_resumed", event.model_dump(mode="json"))

    async def _emit_timed_out_event(
        self,
        *,
        node_id: str,
        paused_at: float,
        timeout_seconds: int,
        transitioned_to: str | None = None,
    ) -> None:
        """Emit ``playbook.run.timed_out`` on the EventBus.

        Fired when a paused run exceeds its configured pause timeout (spec §9,
        roadmap 5.4.4).  If the run transitions to a timeout node, the node ID
        is included so downstream subscribers know the run continues rather
        than simply failing.
        """
        payload: dict[str, Any] = {
            "playbook_id": self._playbook_id,
            "run_id": self.run_id,
            "node_id": node_id,
            "paused_at": paused_at,
            "timeout_seconds": timeout_seconds,
            "waited_seconds": round(time.time() - paused_at, 2),
        }
        if transitioned_to is not None:
            payload["transitioned_to"] = transitioned_to
        payload["tokens_used"] = self.tokens_used
        await self._emit_bus_event("playbook.run.timed_out", payload)

        # Emit typed notification event so Discord/Telegram route the timeout
        # to the same channel that received the original pause notification
        # (roadmap 5.4.7 test case (f)).
        await self._emit_timed_out_notification(payload)

    async def _emit_timed_out_notification(
        self,
        raw_payload: dict[str, Any],
    ) -> None:
        """Emit ``notify.playbook_run_timed_out`` for notification transports.

        Mirrors :meth:`_emit_paused_notification` — converts the raw
        ``playbook.run.timed_out`` payload into a typed
        ``PlaybookRunTimedOutEvent`` and emits it on the EventBus.  Because
        the runner's ``_emit_bus_event`` automatically injects ``project_id``
        from the trigger event, the notification routes to the same channel
        that received the original pause notification (roadmap 5.4.7 case f).
        """
        from src.notifications.events import PlaybookRunTimedOutEvent

        event = PlaybookRunTimedOutEvent(
            playbook_id=raw_payload.get("playbook_id", ""),
            run_id=raw_payload.get("run_id", ""),
            node_id=raw_payload.get("node_id", ""),
            timeout_seconds=raw_payload.get("timeout_seconds", 0),
            waited_seconds=raw_payload.get("waited_seconds", 0.0),
            tokens_used=raw_payload.get("tokens_used", 0),
            transitioned_to=raw_payload.get("transitioned_to"),
            project_id=raw_payload.get("project_id"),
        )
        await self._emit_bus_event("notify.playbook_run_timed_out", event.model_dump(mode="json"))

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
            f"I will guide you through each step.\n\n"
            f"**IMPORTANT: Execute each step yourself using the available tools. "
            f"Do NOT delegate work by creating tasks unless the step explicitly "
            f"instructs you to create a task. Use `load_tools(category=...)` to "
            f"load any tool categories you need, then call the tools directly.**"
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
                    except Exception as exc:
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

        # Inject human input into conversation
        runner.messages.append(
            {
                "role": "user",
                "content": f"[Human review response]: {human_input}",
            }
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
        node_llm_config = self._resolve_node_llm_config(node)
        if node_llm_config is None:
            node_llm_config = {"max_tokens": 4096}
        elif "max_tokens" not in node_llm_config:
            node_llm_config = {**node_llm_config, "max_tokens": 4096}

        supervisor_progress = self._make_supervisor_progress(node_id)

        # Build fresh per-node context (NOT the accumulated transcript)
        context = self._build_node_context()

        # Pre-load ALL tools for playbook nodes so the LLM doesn't need
        # to waste turns calling load_tools.  This gives each node access
        # to every registered tool (core + all categories + plugins).
        all_tool_names = [t["name"] for t in self.supervisor._registry.get_all_tools()]

        timeout = node.get("timeout_seconds")
        try:
            coro = self.supervisor.chat(
                text=prompt,
                user_name=f"playbook-runner:{node_id}",
                history=context,
                on_progress=supervisor_progress,
                llm_config=node_llm_config,
                tool_overrides=all_tool_names,
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
            collected.append(self.node_outputs.pop(stored_key, response))

        # Store collected results
        if collect_name:
            self.node_outputs[collect_name] = collected

        return f"Completed {len(items)} iterations of {node_id}"

    # ------------------------------------------------------------------
    # Data flow: variable resolution, templates, context building
    # ------------------------------------------------------------------

    def _resolve_output_var(self, path: str, extra_vars: dict | None = None) -> Any:
        """Resolve a dot-path variable against node_outputs and extra_vars.

        Used by template rendering and for_each source resolution.  Distinct
        from ``_resolve_variable`` which handles structured transition conditions.

        Examples:
            "discover_projects.active_projects"  → node_outputs["discover_projects"]["active_projects"]
            "project.workspace"                  → extra_vars["project"]["workspace"]
            "scan_results"                       → node_outputs["scan_results"]
        """
        parts = path.split(".")
        root = parts[0]

        # Check extra vars first (e.g. for_each item)
        if extra_vars and root in extra_vars:
            val = extra_vars[root]
        elif root in self.node_outputs:
            val = self.node_outputs[root]
        else:
            return None

        # Walk remaining path
        for part in parts[1:]:
            if isinstance(val, dict):
                val = val.get(part)
            elif isinstance(val, list) and part.isdigit():
                idx = int(part)
                val = val[idx] if idx < len(val) else None
            else:
                return None
            if val is None:
                return None
        return val

    def _render_template(self, template: str, extra_vars: dict | None = None) -> str:
        """Substitute {{variable}} references in a template string.

        Supports:
        - {{name}} — resolved via _resolve_output_var
        - {{name | length}} — len() of the resolved value
        - {{name | json}} — JSON-serialized value
        - Plain {{name}} with dict/list values are JSON-serialized automatically
        """
        import re as _re

        def _replace(match: _re.Match) -> str:
            expr = match.group(1).strip()
            # Check for pipe filters
            if "|" in expr:
                var_part, filter_part = expr.rsplit("|", 1)
                var_part = var_part.strip()
                filter_part = filter_part.strip()
                val = self._resolve_output_var(var_part, extra_vars)
                if filter_part == "length":
                    return str(len(val)) if val is not None else "0"
                elif filter_part == "json":
                    return json.dumps(val, indent=2) if val is not None else "null"
                # Unknown filter — just serialize
                return str(val)

            val = self._resolve_output_var(expr, extra_vars)
            if val is None:
                return f"{{{{UNRESOLVED:{expr}}}}}"
            if isinstance(val, (dict, list)):
                return json.dumps(val)
            return str(val)

        return _re.sub(r"\{\{(.+?)\}\}", _replace, template)

    def _build_node_context(self) -> list[dict]:
        """Build fresh per-node context from seed + prior node outputs.

        Each node gets a clean slate instead of the accumulated transcript.
        Prior node outputs are rendered as a compact structured summary.
        """
        context: list[dict] = []

        # 1. Seed message (event data + execution preamble)
        if self._seed_message:
            context.append({"role": "user", "content": self._seed_message})

        # 2. Prior node outputs as structured context
        if self.node_outputs:
            parts = ["## Prior Step Results\n"]
            for key, value in self.node_outputs.items():
                if isinstance(value, (dict, list)):
                    # Compact: show structure but truncate large values
                    serialized = json.dumps(value)
                    if len(serialized) > 500:
                        # For large outputs, show a summary
                        if isinstance(value, list):
                            parts.append(
                                f"### {key}\n"
                                f"Array with {len(value)} items. "
                                f"First item: {json.dumps(value[0])[:200]}..."
                            )
                        else:
                            parts.append(
                                f"### {key}\n"
                                f"{serialized[:500]}..."
                            )
                    else:
                        parts.append(f"### {key}\n{serialized}")
                else:
                    parts.append(f"### {key}\n{value}")

            context.append({"role": "user", "content": "\n\n".join(parts)})
            context.append(
                {"role": "assistant", "content": "Understood. I have the context from prior steps."}
            )

        return context

    def _extract_output(self, node: dict, response: str) -> Any:
        """Extract structured output from a node's execution.

        If the node has an ``output.extract`` directive, pull that key from
        the last tool result in supervisor._last_messages.  Otherwise return
        the text response.
        """
        output_spec = node.get("output")
        if not output_spec or "extract" not in output_spec:
            return response

        extract_path = output_spec["extract"]

        # Get the last tool result from the supervisor's messages
        last_messages = getattr(self.supervisor, "_last_messages", None)
        if not last_messages:
            logger.debug("_extract_output: no _last_messages available")
            return response
        logger.info("_extract_output: searching %d messages for key '%s'", len(last_messages), extract_path)

        # Walk backwards to find the last tool_result
        for msg in reversed(last_messages):
            content = msg.get("content")
            if isinstance(content, list):
                for item in reversed(content):
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        raw = item.get("content", "")
                        try:
                            parsed = json.loads(raw) if isinstance(raw, str) else raw
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if isinstance(parsed, dict):
                            # Resolve dot-path extraction
                            val = parsed
                            for part in extract_path.split("."):
                                if isinstance(val, dict):
                                    val = val.get(part)
                                else:
                                    val = None
                                    break
                            if val is not None:
                                logger.info(
                                    "_extract_output: extracted '%s' → %s (%d items)"
                                    if isinstance(val, list)
                                    else "_extract_output: extracted '%s' → %s",
                                    extract_path,
                                    type(val).__name__,
                                    len(val) if isinstance(val, list) else 0,
                                )
                                return val
        logger.warning("_extract_output: key '%s' not found in any tool result", extract_path)
        return response

    def _evaluate_filter(self, expr: str, item: Any, item_var: str) -> bool:
        """Evaluate a simple filter expression against an item.

        Supports:
        - ``item.field == "value"`` / ``item.field != "value"``
        - ``item.field in ["a", "b"]``
        - ``item.field`` (truthy check)
        - ``item.status == "ACTIVE"``

        Falls back to truthy evaluation of the item var if the expression
        can't be parsed.
        """
        extra = {item_var: item, "item": item}
        expr = expr.strip()

        # Simple truthy: "item.findings"
        if " " not in expr:
            val = self._resolve_output_var(expr, extra)
            return bool(val)

        # Comparison: "item.status == 'ACTIVE'"
        import re as _re

        # Match: path OP value
        m = _re.match(
            r"([\w.]+)\s*(==|!=|in|not\s+in)\s*(.+)$", expr
        )
        if not m:
            # Can't parse — include the item (permissive)
            logger.debug("for_each filter: can't parse '%s', including item", expr)
            return True

        var_path, op, rhs_str = m.group(1), m.group(2).strip(), m.group(3).strip()
        val = self._resolve_output_var(var_path, extra)

        # Parse RHS
        try:
            rhs = json.loads(rhs_str)
        except (json.JSONDecodeError, TypeError):
            # Try unquoted string
            rhs = rhs_str.strip("'\"")

        if op == "==":
            return val == rhs
        elif op == "!=":
            return val != rhs
        elif op == "in":
            return val in rhs if isinstance(rhs, (list, str)) else False
        elif op == "not in":
            return val not in rhs if isinstance(rhs, (list, str)) else True

        return True

    def _store_node_output(self, node_id: str, node: dict, value: Any) -> None:
        """Store a node's output in node_outputs under the appropriate key."""
        output_spec = node.get("output")
        if output_spec and output_spec.get("as"):
            self.node_outputs[output_spec["as"]] = value
        else:
            self.node_outputs[node_id] = value

    # ------------------------------------------------------------------
    # Internal: prompt building (legacy, now with template support)
    # ------------------------------------------------------------------

    def _build_node_prompt(self, node_id: str, node: dict, extra_vars: dict | None = None) -> str:
        """Build the prompt text for a single node.

        Resolves {{variable}} references in the node's prompt using
        node_outputs and any extra_vars (e.g. for_each current item).

        This method exists as a clean extension point for future enrichment
        (e.g., injecting tool-availability hints, step-position metadata, or
        node-specific instructions) without changing ``_execute_node()``.

        Parameters
        ----------
        node_id:
            Identifier of the node being executed (for logging/debugging).
        node:
            The node definition dict from the compiled playbook graph.

        Returns
        -------
        str
            The fully constructed prompt to send to the Supervisor.
        """
        raw = node.get("prompt", "")
        if "{{" in raw:
            return self._render_template(raw, extra_vars)
        return raw

    def _resolve_node_llm_config(self, node: dict) -> dict | None:
        """Resolve the effective LLM config for a node.

        Node-level ``llm_config`` overrides playbook-level ``llm_config``.
        When neither is set, returns *None* to use the Supervisor's default
        provider.

        Parameters
        ----------
        node:
            The node definition dict from the compiled playbook graph.

        Returns
        -------
        dict or None
            LLM config dict suitable for passing to ``supervisor.chat()``,
            or *None* for default behaviour.
        """
        return node.get("llm_config") or self._llm_config

    def _make_supervisor_progress(
        self,
        node_id: str,
    ) -> Callable[[str, str | None], Awaitable[None]] | None:
        """Create a progress callback bridge for a supervisor.chat() call.

        Maps supervisor-level progress events (``"thinking"``, ``"tool_use"``,
        ``"responding"``) into node-scoped events that the runner's
        ``on_progress`` callback can forward to the UI.

        Emits events of the form ``("node_tool_use", "node_id:tool_name")``.

        Returns *None* when no ``on_progress`` callback is configured (so the
        Supervisor skips progress reporting entirely, avoiding overhead).

        Parameters
        ----------
        node_id:
            The node this supervisor call is executing, used as a prefix.
        """
        if not self.on_progress:
            return None

        on_progress = self.on_progress  # capture for closure

        async def _bridge(event: str, detail: str | None) -> None:
            # Map supervisor events to node-scoped events
            await on_progress(f"node_{event}", f"{node_id}:{detail}" if detail else node_id)

        return _bridge

    # ------------------------------------------------------------------
    # Internal: transition evaluation
    # ------------------------------------------------------------------

    async def _evaluate_transition(
        self,
        node_id: str,
        node: dict,
        response: str,
    ) -> tuple[str | None, str]:
        """Determine the next node ID based on the node's transition config.

        Returns a tuple of ``(next_node_id, method)`` where *method* is one of
        ``"goto"``, ``"llm"``, ``"structured"``, ``"otherwise"``, or ``"none"``.

        Handles four cases per the spec §6:

        1. **Unconditional ``goto``** — return target directly (no LLM call).
        2. **Structured transitions** — when ``when`` is a dict, evaluate
           locally against the node response without an LLM call.
        3. **Natural-language transitions** — when ``when`` is a string,
           use a separate, cheap LLM call to classify which condition
           matches.
        4. **No transitions and no goto** — return *None* (implicit end).

        Mixed lists (some structured, some natural-language) are supported:
        structured conditions are checked first; if none match, remaining
        natural-language conditions are classified via LLM.
        """
        # Case 1: unconditional goto
        if "goto" in node:
            target = node["goto"]
            logger.debug("Node '%s' → unconditional goto '%s'", node_id, target)
            return target, "goto"

        # Case 4: no transitions defined
        transitions = node.get("transitions")
        if not transitions:
            return None, "none"

        # Separate transitions into structured vs. natural-language
        structured: list[tuple[int, dict]] = []
        natural_lang: list[tuple[int, dict]] = []
        otherwise_target: str | None = None

        for i, t in enumerate(transitions):
            if t.get("otherwise"):
                otherwise_target = t["goto"]
            elif isinstance(t.get("when"), dict):
                structured.append((i, t))
            else:
                natural_lang.append((i, t))

        # Case 2: try structured transitions first (no LLM call)
        for _idx, t in structured:
            if self._evaluate_structured_condition(t["when"], response):
                target = t["goto"]
                logger.debug(
                    "Node '%s' → structured transition to '%s' (condition: %s)",
                    node_id,
                    target,
                    t["when"],
                )
                return target, "structured"

        # Case 3: natural-language transitions via LLM classification
        if natural_lang:
            target = await self._classify_transition(node_id, node, transitions, response)
            if target is not None:
                return target, "llm"

        # Fallback to otherwise
        if otherwise_target:
            logger.debug(
                "Node '%s' → otherwise fallback to '%s'",
                node_id,
                otherwise_target,
            )
            return otherwise_target, "otherwise"

        # Transitions were defined but nothing matched — this is a runtime
        # error, not an implicit terminal.  Nodes with no transitions at all
        # are handled earlier (Case 4) and treated as implicit terminals.
        conditions = [t.get("when") for t in transitions if not t.get("otherwise")]
        raise RuntimeError(
            f"Node '{node_id}': no transition matched and no 'otherwise' "
            f"fallback defined. Conditions: {conditions}"
        )

    # ------------------------------------------------------------------
    # Internal: structured transition evaluation
    # ------------------------------------------------------------------

    def _evaluate_structured_condition(self, condition: dict, response: str) -> bool:
        """Evaluate a structured (dict-based) transition condition locally.

        Structured conditions allow deterministic evaluation without an
        LLM call, per spec §6.  The compiler emits these for simple,
        unambiguous conditionals.

        Supported condition formats:

        **Function-based conditions** (roadmap 5.2.4):

        - ``{"function": "response_contains", "value": "text"}``
          → ``True`` if *value* appears in *response* (case-insensitive).

        - ``{"function": "response_not_contains", "value": "text"}``
          → ``True`` if *value* does NOT appear in *response* (case-insensitive).

        - ``{"function": "has_tool_output", "contains": "text"}``
          → Alias for ``response_contains`` — the node's final response
          summarises tool output, so checking the response suffices.

        **Expression conditions** (roadmap 5.2.5):

        - ``{"expression": "task.status == \\"completed\\""}``
          → Parses and evaluates a comparison expression deterministically.

        - ``{"function": "expression", "expression": "..."}``
          → Alternative format using the ``function`` key.

        - ``{"function": "compare", "variable": "task.status",
          "operator": "==", "value": "completed"}``
          → Pre-parsed structured comparison (no string parsing needed).

        Expression variable namespaces:

        - ``task.*`` / ``event.*`` → trigger event data (``self.event``)
        - ``output.*`` → JSON-parsed fields from the node response
        - ``response`` → the raw response text

        Supported operators: ``==``, ``!=``, ``>``, ``<``, ``>=``, ``<=``

        Unrecognised function names log a warning and return ``False``
        (falling through to LLM evaluation or the ``otherwise`` branch).

        Parameters
        ----------
        condition:
            The structured condition dict from the compiled transition.
        response:
            The LLM's response text from the current node.

        Returns
        -------
        bool
            Whether the condition is satisfied.
        """
        # --- Expression conditions (5.2.5) ---

        # Top-level expression key (no function required)
        expression = condition.get("expression")
        if expression is not None and "function" not in condition:
            return self._evaluate_expression(expression, response)

        func = condition.get("function", "")

        # Expression via function key
        if func == "expression":
            expr_str = condition.get("expression", "")
            return self._evaluate_expression(expr_str, response)

        # Pre-parsed structured comparison
        if func == "compare":
            return self._evaluate_compare(condition, response)

        # --- Function-based conditions (5.2.4) ---

        response_lower = response.lower()

        if func in ("response_contains", "has_tool_output"):
            value = condition.get("value") or condition.get("contains") or ""
            return value.lower() in response_lower

        if func == "response_not_contains":
            value = condition.get("value") or condition.get("contains") or ""
            return value.lower() not in response_lower

        logger.warning("Unknown structured condition function: '%s'", func)
        return False

    def _evaluate_expression(self, expression: str, response: str) -> bool:
        """Parse and evaluate a comparison expression string.

        Supported syntax::

            variable op literal

        Where *variable* is a dotted path (e.g. ``task.status``,
        ``output.approval``), *op* is a comparison operator
        (``==``, ``!=``, ``>``, ``<``, ``>=``, ``<=``), and *literal*
        is a quoted string, number, boolean, or null.

        Parameters
        ----------
        expression:
            The expression string to evaluate.
        response:
            The current node's response text (used for ``output.*``
            and ``response`` variable resolution).

        Returns
        -------
        bool
            Whether the expression evaluates to true.  Returns ``False``
            for invalid syntax or undefined variables (with a warning log).
        """
        match = _EXPR_PATTERN.match(expression)
        if not match:
            logger.warning(
                "Invalid expression syntax: '%s' — expected 'variable op literal'",
                expression,
            )
            return False

        var_path = match.group("var")
        op = match.group("op")
        literal_raw = match.group("literal")

        # Resolve the variable
        value, resolved = self._resolve_variable(var_path, response)
        if not resolved:
            logger.warning(
                "Undefined variable '%s' in expression: '%s'",
                var_path,
                expression,
            )
            return False

        # Parse the literal
        literal = _parse_literal(literal_raw)

        return _compare(value, op, literal)

    def _evaluate_compare(self, condition: dict, response: str) -> bool:
        """Evaluate a pre-parsed structured comparison condition.

        Expected format::

            {"function": "compare", "variable": "task.status",
             "operator": "==", "value": "completed"}

        This is an alternative to expression strings — the compiler can
        emit either format.

        Parameters
        ----------
        condition:
            The condition dict with ``variable``, ``operator``, ``value`` keys.
        response:
            The current node's response text.

        Returns
        -------
        bool
            Whether the comparison is satisfied.
        """
        var_path = condition.get("variable", "")
        op = condition.get("operator", "")
        literal_value = condition.get("value")

        if not var_path or not op:
            logger.warning(
                "Incomplete compare condition — missing 'variable' or 'operator': %s",
                condition,
            )
            return False

        if op not in ("==", "!=", ">", "<", ">=", "<="):
            logger.warning("Unsupported operator '%s' in compare condition", op)
            return False

        value, resolved = self._resolve_variable(var_path, response)
        if not resolved:
            logger.warning(
                "Undefined variable '%s' in compare condition: %s",
                var_path,
                condition,
            )
            return False

        return _compare(value, op, literal_value)

    def _resolve_variable(self, var_path: str, response: str) -> tuple[Any, bool]:
        """Resolve a dotted variable path to a value.

        Variable namespaces:

        - ``task.*`` / ``event.*`` — fields from ``self.event`` (trigger data)
        - ``output.*`` — fields from the JSON-parsed node response
        - ``response`` — the raw response text (no sub-fields)

        Parameters
        ----------
        var_path:
            Dot-separated variable path (e.g. ``task.status``).
        response:
            The current node's response text.

        Returns
        -------
        tuple[Any, bool]
            ``(value, True)`` on success, ``(None, False)`` if the
            variable cannot be resolved.
        """
        parts = var_path.split(".", 1)
        namespace = parts[0]
        field = parts[1] if len(parts) > 1 else None

        if namespace in ("task", "event"):
            if field is None:
                return self.event, True
            return _dot_get(self.event, field)

        if namespace == "output":
            # Try to parse the response as JSON for structured field access
            try:
                data = json.loads(response)
            except (json.JSONDecodeError, TypeError):
                logger.debug(
                    "Cannot resolve output.* — response is not valid JSON (var_path=%s)",
                    var_path,
                )
                return None, False

            if not isinstance(data, dict):
                logger.debug(
                    "Cannot resolve output.* — JSON response is not a dict (var_path=%s, type=%s)",
                    var_path,
                    type(data).__name__,
                )
                return None, False

            if field is None:
                return data, True
            return _dot_get(data, field)

        if var_path == "response":
            return response, True

        return None, False

    # ------------------------------------------------------------------
    # Internal: LLM-based transition classification
    # ------------------------------------------------------------------

    def _resolve_transition_llm_config(self, node: dict) -> dict | None:
        """Resolve the LLM config for a transition evaluation call.

        Priority order (first non-None wins):

        1. ``node["transition_llm_config"]`` — per-node override for transitions
        2. ``self._transition_llm_config`` — playbook-level transition config
        3. ``node["llm_config"]`` — per-node general config
        4. ``self._llm_config`` — playbook-level general config
        5. ``None`` — use Supervisor default

        This allows playbooks to route transition classification calls to
        a cheaper/faster model (e.g., Haiku) while keeping node execution
        on a capable model (e.g., Sonnet).

        Parameters
        ----------
        node:
            The node definition dict.

        Returns
        -------
        dict or None
            LLM config for the transition call, or *None* for defaults.
        """
        return (
            node.get("transition_llm_config")
            or self._transition_llm_config
            or node.get("llm_config")
            or self._llm_config
        )

    async def _classify_transition(
        self,
        node_id: str,
        node: dict,
        transitions: list[dict],
        response: str,
    ) -> str | None:
        """Use a lightweight LLM call to determine which transition condition matches.

        Builds a numbered list of candidate conditions from the
        *natural-language* transitions (structured conditions and
        ``otherwise`` are excluded — they are handled by the caller).
        The LLM responds with the number of the matching condition.

        Falls back to ``otherwise`` transitions if no match is found.

        Parameters
        ----------
        node_id:
            Current node ID (for logging).
        node:
            The full node dict (used for resolving transition LLM config).
        transitions:
            The complete transitions list (including otherwise entries).
        response:
            The LLM's response text from the current node.

        Returns
        -------
        str or None
            The target node ID, or *None* if no condition matched
            (caller should fall back to ``otherwise``).
        """
        # Dry-run mode: follow the first natural-language transition without
        # making an LLM call.  Falls back to otherwise if none defined.
        if self._dry_run:
            for t in transitions:
                if not t.get("otherwise") and isinstance(t.get("when"), str):
                    return t["goto"]
            for t in transitions:
                if t.get("otherwise"):
                    return t["goto"]
            return None

        # Build the classification prompt — only natural-language conditions
        condition_lines = []
        nl_transitions: list[dict] = []  # ordered subset for index mapping
        otherwise_target: str | None = None

        for t in transitions:
            if t.get("otherwise"):
                otherwise_target = t["goto"]
            elif isinstance(t.get("when"), str):
                nl_transitions.append(t)
                condition_lines.append(f"{len(nl_transitions)}. {t['when']}")

        if not condition_lines:
            # No natural-language conditions to evaluate
            return otherwise_target

        # Add the otherwise option for the LLM to pick if nothing matches
        if otherwise_target:
            condition_lines.append(f"{len(nl_transitions) + 1}. [DEFAULT/OTHERWISE]")

        transition_prompt = (
            "Based on the result above, which condition best matches?\n\n"
            + "\n".join(condition_lines)
            + "\n\nRespond with ONLY the number of the matching condition "
            "(e.g., '1' or '2'). If none clearly match, respond with '0'."
        )

        # Resolve LLM config: prefer transition-specific, then general
        transition_llm_config = self._resolve_transition_llm_config(node)
        if transition_llm_config:
            logger.debug(
                "Node '%s': transition classification using LLM config %s",
                node_id,
                transition_llm_config,
            )

        # Make the classification call with full conversation context
        decision = await self.supervisor.chat(
            text=transition_prompt,
            user_name=f"playbook-runner:transition:{node_id}",
            history=list(self.messages),
            llm_config=transition_llm_config,
            tool_overrides=[],  # No tools needed for classification
        )

        # Parse the LLM's choice
        decision = decision.strip()

        # Build a virtual transitions list for _match_transition_by_number
        # so indices align with the numbered prompt
        virtual_transitions = list(nl_transitions)
        if otherwise_target:
            virtual_transitions.append({"otherwise": True, "goto": otherwise_target})

        matched_target = self._match_transition_by_number(
            decision, virtual_transitions, otherwise_target
        )

        if matched_target:
            logger.debug(
                "Node '%s' → LLM transition to '%s' (decision: %s)",
                node_id,
                matched_target,
                decision,
            )
        else:
            logger.warning(
                "Node '%s': LLM transition — no match (decision: '%s')",
                node_id,
                decision,
            )

        # Track tokens for the transition call
        self.tokens_used += _estimate_tokens(transition_prompt, decision)

        return matched_target

    @staticmethod
    def _match_transition_by_number(
        decision: str,
        transitions: list[dict],
        otherwise_target: str | None,
    ) -> str | None:
        """Match the LLM's numeric response to a transition target.

        Tries to parse an integer from the decision string.  Falls back
        to fuzzy text matching against ``when`` clauses if numeric parsing
        fails.
        """
        # Try numeric match first
        try:
            # Extract first number from the response
            digits = "".join(c for c in decision if c.isdigit())
            if digits:
                idx = int(digits)
                if idx == 0:
                    return otherwise_target
                if 1 <= idx <= len(transitions):
                    return transitions[idx - 1]["goto"]
        except (ValueError, IndexError):
            pass

        # Fuzzy text match: check if the decision text contains a condition
        decision_lower = decision.lower()
        for t in transitions:
            when = t.get("when", "")
            if when and when.lower() in decision_lower:
                return t["goto"]

        return None

    # ------------------------------------------------------------------
    # Internal: context summarization
    # ------------------------------------------------------------------

    # -- Playbook-specific summarization prompts -------------------------

    _SUMMARIZE_SYSTEM = (
        "You are a concise technical summarizer for multi-step playbook executions. "
        "Produce a brief summary that a downstream LLM step can use as context."
    )

    _SUMMARIZE_INSTRUCTION = (
        "Summarize the following playbook execution transcript concisely (~500 tokens). "
        "Preserve:\n"
        "- Key outputs, decisions, and conclusions from each completed step\n"
        "- File paths, code changes, or tool results that downstream steps may need\n"
        "- Any errors, warnings, or unresolved issues\n"
        "Omit step-by-step narration — focus on *what was accomplished* and "
        "*what matters going forward*."
    )

    async def _summarize_history(self) -> None:
        """Compress conversation history into a summary to manage context size.

        Replaces all messages except the seed (first message) with a single
        summary message.  Uses the Supervisor's summarize capability with a
        playbook-specific prompt that focuses on preserving technical outputs
        and decisions rather than conversational details.

        Token cost of the summarization call itself is tracked and counted
        toward the run's budget.  A ``node_summarizing`` progress event is
        emitted so callers can observe when compression happens.
        """
        if len(self.messages) <= 2:
            return  # Nothing worth summarizing

        if self.on_progress:
            await self.on_progress("node_summarizing", self._playbook_id)

        # Build a transcript of the conversation so far
        original_count = len(self.messages)
        transcript_parts: list[str] = []
        for msg in self.messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                transcript_parts.append(f"**{role}:** {content}")

        transcript = "\n\n".join(transcript_parts)

        summary = await self.supervisor.summarize(
            transcript,
            system_prompt=self._SUMMARIZE_SYSTEM,
            instruction=self._SUMMARIZE_INSTRUCTION,
        )
        if not summary:
            logger.warning("History summarization returned empty — keeping full history")
            return

        # Track the token cost of the summarization LLM call itself
        summarize_tokens = _estimate_tokens(transcript, summary)
        self.tokens_used += summarize_tokens

        # Replace history with seed + summary
        seed = self.messages[0]
        self.messages = [
            seed,
            {
                "role": "user",
                "content": ("[Context summary of prior steps]\n\n" + summary),
            },
        ]

        logger.debug(
            "Summarized %d messages into condensed context for playbook '%s' "
            "(~%d tokens for summarization call)",
            original_count,
            self._playbook_id,
            summarize_tokens,
        )

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
