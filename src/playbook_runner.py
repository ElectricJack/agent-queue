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

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.models import PlaybookRun

if TYPE_CHECKING:
    from src.database.base import DatabaseBackend
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
    ):
        self.graph = graph
        self.event = event
        self.supervisor = supervisor
        self.db = db
        self.on_progress = on_progress

        # Conversation history — node prompts and final responses only.
        self.messages: list[dict] = []
        self.run_id: str = str(uuid.uuid4())[:12]
        self.tokens_used: int = 0
        self.node_trace: list[NodeTraceEntry] = []

        # Resolved from graph
        self._playbook_id: str = graph.get("id", "unknown")
        self._playbook_version: int = graph.get("version", 0)
        self._max_tokens: int | None = graph.get("max_tokens")
        self._llm_config: dict | None = graph.get("llm_config")

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

        # Create the DB record
        db_run = PlaybookRun(
            run_id=self.run_id,
            playbook_id=self._playbook_id,
            playbook_version=self._playbook_version,
            trigger_event=json.dumps(self.event),
            status="running",
            started_at=started_at,
        )
        if self.db:
            await self.db.create_playbook_run(db_run)

        # Seed conversation with event context
        seed_message = (
            f"Event received: {json.dumps(self.event)}\n\n"
            f"You are executing playbook '{self._playbook_id}'. "
            f"I will guide you through each step."
        )
        self.messages.append({"role": "user", "content": seed_message})

        # Find entry node
        entry_node_id = self._find_entry_node()
        if entry_node_id is None:
            return await self._fail(db_run, "No entry node found in playbook graph", started_at)

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
                )

            # Terminal node — we're done
            if node.get("terminal"):
                if self.on_progress:
                    await self.on_progress("node_terminal", current_node_id)
                break

            # Check token budget before executing
            if self._max_tokens and self.tokens_used >= self._max_tokens:
                return await self._fail(
                    db_run,
                    f"Token budget exceeded ({self.tokens_used}/{self._max_tokens})",
                    started_at,
                    status="timed_out",
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
                )

            # Human-in-the-loop pause
            if node.get("wait_for_human"):
                return await self._pause(db_run, current_node_id, started_at)

            # Determine next node via transition evaluation
            try:
                next_node_id = await self._evaluate_transition(current_node_id, node, response)
            except Exception as exc:
                logger.exception("Transition evaluation failed at node '%s'", current_node_id)
                return await self._fail(
                    db_run,
                    f"Transition from '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                )

            if next_node_id is None:
                # No transition matched and no terminal — implicit completion
                logger.warning(
                    "No transition matched at node '%s' — treating as terminal",
                    current_node_id,
                )
                break

            current_node_id = next_node_id

        # Completed successfully
        completed_at = time.time()
        trace_dicts = [self._trace_to_dict(t) for t in self.node_trace]

        if self.db:
            await self.db.update_playbook_run(
                self.run_id,
                status="completed",
                conversation_history=json.dumps(self.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=self.tokens_used,
                completed_at=completed_at,
            )

        if self.on_progress:
            await self.on_progress("playbook_completed", self._playbook_id)

        return RunResult(
            run_id=self.run_id,
            status="completed",
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
    ) -> RunResult:
        """Resume a paused playbook run with human input.

        Reconstructs the runner state from the persisted ``PlaybookRun``,
        injects the human's input into conversation history, and continues
        walking the graph from the paused node.

        Parameters
        ----------
        db_run:
            The persisted :class:`PlaybookRun` with status ``"paused"``.
        graph:
            The compiled playbook graph (must match ``db_run.playbook_id``).
        supervisor:
            Supervisor instance for LLM calls.
        human_input:
            The human reviewer's response / decision text.
        db:
            Database backend for persisting updates.
        on_progress:
            Optional progress callback.
        """
        runner = cls(graph, json.loads(db_run.trigger_event), supervisor, db, on_progress)
        runner.run_id = db_run.run_id
        runner.messages = json.loads(db_run.conversation_history)
        runner.node_trace = [NodeTraceEntry(**entry) for entry in json.loads(db_run.node_trace)]
        runner.tokens_used = db_run.tokens_used

        # Inject human input into conversation
        runner.messages.append(
            {
                "role": "user",
                "content": f"[Human review response]: {human_input}",
            }
        )

        # Update DB status to running
        if db:
            await db.update_playbook_run(db_run.run_id, status="running")

        # Find the next node after the paused one
        paused_node_id = db_run.current_node
        if not paused_node_id:
            return RunResult(
                run_id=db_run.run_id,
                status="failed",
                node_trace=[runner._trace_to_dict(t) for t in runner.node_trace],
                tokens_used=runner.tokens_used,
                error="Cannot resume: no current_node recorded",
            )

        paused_node = graph["nodes"].get(paused_node_id)
        if not paused_node:
            return RunResult(
                run_id=db_run.run_id,
                status="failed",
                node_trace=[runner._trace_to_dict(t) for t in runner.node_trace],
                tokens_used=runner.tokens_used,
                error=f"Cannot resume: node '{paused_node_id}' not found in graph",
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
            next_node_id = await runner._evaluate_transition(
                paused_node_id, paused_node, last_response
            )
        except Exception as exc:
            return RunResult(
                run_id=db_run.run_id,
                status="failed",
                node_trace=[runner._trace_to_dict(t) for t in runner.node_trace],
                tokens_used=runner.tokens_used,
                error=f"Transition from paused node failed: {exc}",
            )

        if next_node_id is None:
            # No transition — completed
            completed_at = time.time()
            trace_dicts = [runner._trace_to_dict(t) for t in runner.node_trace]
            if db:
                await db.update_playbook_run(
                    db_run.run_id,
                    status="completed",
                    conversation_history=json.dumps(runner.messages),
                    node_trace=json.dumps(trace_dicts),
                    tokens_used=runner.tokens_used,
                    completed_at=completed_at,
                )
            return RunResult(
                run_id=db_run.run_id,
                status="completed",
                node_trace=trace_dicts,
                tokens_used=runner.tokens_used,
            )

        # Continue walking the graph from the next node
        started_at = db_run.started_at
        current_node_id = next_node_id
        final_response: str | None = None

        while True:
            node = graph["nodes"].get(current_node_id)
            if node is None:
                return await runner._fail(
                    db_run,
                    f"Node '{current_node_id}' not found in graph",
                    started_at,
                )

            if node.get("terminal"):
                break

            if runner._max_tokens and runner.tokens_used >= runner._max_tokens:
                return await runner._fail(
                    db_run,
                    f"Token budget exceeded ({runner.tokens_used}/{runner._max_tokens})",
                    started_at,
                    status="timed_out",
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
                )

            if node.get("wait_for_human"):
                return await runner._pause(db_run, current_node_id, started_at)

            try:
                next_node_id = await runner._evaluate_transition(current_node_id, node, response)
            except Exception as exc:
                return await runner._fail(
                    db_run,
                    f"Transition from '{current_node_id}' failed: {exc}",
                    started_at,
                    current_node=current_node_id,
                )

            if next_node_id is None:
                break

            current_node_id = next_node_id

        completed_at = time.time()
        trace_dicts = [runner._trace_to_dict(t) for t in runner.node_trace]

        if db:
            await db.update_playbook_run(
                db_run.run_id,
                status="completed",
                conversation_history=json.dumps(runner.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=runner.tokens_used,
                completed_at=completed_at,
            )

        return RunResult(
            run_id=db_run.run_id,
            status="completed",
            node_trace=trace_dicts,
            tokens_used=runner.tokens_used,
            final_response=final_response,
        )

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

        1. Optionally summarize history (if ``summarize_before`` is set).
        2. Send the node prompt to the Supervisor with accumulated history.
        3. Append prompt/response to ``self.messages``.
        4. Track tokens and update node trace.
        5. Persist run state to DB.
        """
        trace_entry = NodeTraceEntry(node_id=node_id, started_at=time.time())
        self.node_trace.append(trace_entry)

        if self.on_progress:
            await self.on_progress("node_started", node_id)

        # Context size management: summarize history before this node
        if node.get("summarize_before") and len(self.messages) > 2:
            await self._summarize_history()

        prompt = node.get("prompt", "")

        # Resolve per-node LLM config (node overrides playbook-level)
        node_llm_config = node.get("llm_config") or self._llm_config

        # Execute via Supervisor — the Supervisor handles the internal
        # multi-turn tool-use loop and returns only the final text response.
        response = await self.supervisor.chat(
            text=prompt,
            user_name="playbook-runner",
            history=list(self.messages),  # Copy so Supervisor doesn't mutate ours
            llm_config=node_llm_config,
        )

        # Append to our conversation history (node-level granularity)
        self.messages.append({"role": "user", "content": prompt})
        self.messages.append({"role": "assistant", "content": response})

        # Track tokens
        token_estimate = _estimate_tokens(prompt, response)
        self.tokens_used += token_estimate

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

    # ------------------------------------------------------------------
    # Internal: transition evaluation
    # ------------------------------------------------------------------

    async def _evaluate_transition(
        self,
        node_id: str,
        node: dict,
        response: str,
    ) -> str | None:
        """Determine the next node ID based on the node's transition config.

        Returns the next node ID, or *None* if no transition applies
        (implicit terminal).

        Handles three cases per the spec:
        1. Unconditional ``goto`` — return target directly.
        2. Conditional ``transitions`` list with natural-language ``when``
           clauses — use a separate LLM call to classify which condition
           matches.
        3. No transitions and no goto — return *None* (implicit end).
        """
        # Case 1: unconditional goto
        if "goto" in node:
            target = node["goto"]
            logger.debug("Node '%s' → unconditional goto '%s'", node_id, target)
            return target

        # Case 3: no transitions defined
        transitions = node.get("transitions")
        if not transitions:
            return None

        # Case 2: conditional transitions via LLM classification
        return await self._classify_transition(node_id, transitions, response)

    async def _classify_transition(
        self,
        node_id: str,
        transitions: list[dict],
        response: str,
    ) -> str | None:
        """Use a lightweight LLM call to determine which transition condition matches.

        The prompt presents the node response and lists all candidate
        conditions.  The LLM responds with just the matching condition
        text, which is then fuzzy-matched to find the target node.

        Falls back to ``otherwise`` transitions if no match is found.
        """
        # Build the classification prompt
        condition_lines = []
        otherwise_target: str | None = None
        for i, t in enumerate(transitions, 1):
            if t.get("otherwise"):
                otherwise_target = t["goto"]
                condition_lines.append(f"{i}. [DEFAULT/OTHERWISE]")
            else:
                condition_lines.append(f"{i}. {t['when']}")

        transition_prompt = (
            "Based on the result above, which condition best matches?\n\n"
            + "\n".join(condition_lines)
            + "\n\nRespond with ONLY the number of the matching condition "
            "(e.g., '1' or '2'). If none clearly match, respond with '0'."
        )

        # Use a cheap config if the playbook/node specifies one for transitions
        transition_llm_config = self._llm_config

        # Make the classification call with full conversation context
        decision = await self.supervisor.chat(
            text=transition_prompt,
            user_name="playbook-runner:transition",
            history=list(self.messages),
            llm_config=transition_llm_config,
            tool_overrides=[],  # No tools needed for classification
        )

        # Parse the LLM's choice
        decision = decision.strip()
        matched_target = self._match_transition_by_number(decision, transitions, otherwise_target)

        if matched_target:
            logger.debug(
                "Node '%s' → transition to '%s' (decision: %s)",
                node_id,
                matched_target,
                decision,
            )
        else:
            logger.warning(
                "Node '%s': no transition matched (decision: '%s'), falling back to otherwise=%s",
                node_id,
                decision,
                otherwise_target,
            )
            matched_target = otherwise_target

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

    async def _summarize_history(self) -> None:
        """Compress conversation history into a summary to manage context size.

        Replaces all messages except the seed (first message) with a single
        summary message.  Uses the Supervisor's summarize capability.
        """
        if len(self.messages) <= 2:
            return  # Nothing worth summarizing

        # Build a transcript of the conversation so far
        transcript_parts: list[str] = []
        for msg in self.messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                transcript_parts.append(f"**{role}:** {content}")

        transcript = "\n\n".join(transcript_parts)

        summary = await self.supervisor.summarize(transcript)
        if not summary:
            logger.warning("History summarization returned empty — keeping full history")
            return

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
            "Summarized %d messages into condensed context for playbook '%s'",
            len(transcript_parts),
            self._playbook_id,
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
        status: str = "failed",
    ) -> RunResult:
        """Mark the run as failed and persist."""
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
        """Mark the run as paused for human review."""
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
                status="paused",
                current_node=node_id,
                conversation_history=json.dumps(self.messages),
                node_trace=json.dumps(trace_dicts),
                tokens_used=self.tokens_used,
            )

        if self.on_progress:
            await self.on_progress("playbook_paused", node_id)

        return RunResult(
            run_id=self.run_id,
            status="paused",
            node_trace=trace_dicts,
            tokens_used=self.tokens_used,
        )

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _trace_to_dict(entry: NodeTraceEntry) -> dict:
        """Convert a NodeTraceEntry to a JSON-serialisable dict."""
        return {
            "node_id": entry.node_id,
            "started_at": entry.started_at,
            "completed_at": entry.completed_at,
            "status": entry.status,
        }
