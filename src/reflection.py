"""Reflection engine for the Supervisor's action-reflect cycle.

Every action the Supervisor takes gets a verification pass.  The
reflection depth varies by trigger type and configured level.

The engine classifies triggers into three tiers:

- **Deep** (``task.completed``, ``task.failed``, ``hook.failed``) — full
  five-point evaluation including rule checks and memory updates.
- **Standard** (``user.request``, ``hook.completed``) — quick success
  check and rule scan.
- **Light** (``passive.observation``, ``periodic.sweep``) — minimal:
  update memory if notable, otherwise skip.

Safety controls prevent runaway loops:

- Maximum reflection depth (default 3) — caps recursive reflect calls.
- Per-cycle token cap — limits total tokens spent in one reflect chain.
- Hourly circuit breaker — hard cap on tokens across all reflection
  within a rolling 1-hour window.

See ``specs/supervisor.md`` for the reflection lifecycle specification.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from src.config import ReflectionConfig


@dataclass
class ReflectionVerdict:
    """Structured verdict from a reflection pass."""

    passed: bool
    reason: str
    suggested_followup: str | None = None


_DEEP_TRIGGERS = {"task.completed", "task.failed", "hook.failed"}
_STANDARD_TRIGGERS = {"user.request", "hook.completed"}
_LIGHT_TRIGGERS = {"passive.observation", "periodic.sweep"}


class ReflectionEngine:
    """Manages the action-reflect cycle for the Supervisor.

    The engine is stateless between conversations — it only tracks token
    usage for circuit-breaker purposes.  The Supervisor owns the instance
    and calls ``should_reflect`` → ``determine_depth`` →
    ``build_reflection_prompt`` → ``parse_verdict`` for each cycle.

    Attributes:
        _config: Reflection configuration (level, caps, circuit breaker).
        _token_ledger: Rolling list of ``(timestamp, token_count)`` tuples
            for circuit-breaker tracking.
    """

    def __init__(self, config: ReflectionConfig):
        """Initialise the engine with reflection configuration.

        Args:
            config: Controls reflection level (``off``, ``minimal``,
                ``moderate``, ``full``), depth limits, and token caps.
        """
        self._config = config
        self._token_ledger: list[tuple[float, int]] = []

    @property
    def level(self) -> str:
        return self._config.level

    def should_reflect(self, trigger: str) -> bool:
        """Decide whether to run reflection for the given trigger.

        Args:
            trigger: Event name (e.g. ``"user.request"``, ``"task.completed"``).

        Returns:
            ``True`` if reflection should proceed.  Returns ``False`` when
            reflection is disabled or the hourly circuit breaker is tripped.
        """
        if self._config.level == "off":
            return False
        if self.is_circuit_breaker_tripped():
            return False
        return True

    def determine_depth(self, trigger: str, context: dict) -> str | None:
        """Map a trigger to a reflection depth based on configuration level.

        Args:
            trigger: Event name to classify.
            context: Additional context (reserved for future use).

        Returns:
            One of ``"deep"``, ``"standard"``, ``"light"``, or ``None``
            if reflection is off.
        """
        if self._config.level == "off":
            return None
        if self._config.level == "minimal":
            return "light"
        if self._config.level == "moderate":
            if trigger in _DEEP_TRIGGERS:
                return "standard"
            return "light"
        # full
        if trigger in _DEEP_TRIGGERS:
            return "deep"
        if trigger in _STANDARD_TRIGGERS:
            return "standard"
        return "light"

    def build_reflection_prompt(
        self, depth: str, trigger: str, action_summary: str, action_results: list[dict]
    ) -> str:
        """Build the reflection prompt for the LLM.

        Args:
            depth: Reflection depth (``"deep"``, ``"standard"``, ``"light"``).
            trigger: The event that triggered reflection.
            action_summary: Human-readable summary of the action taken.
            action_results: List of ``{"tool": ..., "result": ...}`` dicts
                from the tool-use round.

        Returns:
            A Markdown-formatted prompt string for the reflection LLM call.
        """
        if depth == "deep":
            return self._build_deep_prompt(trigger, action_summary, action_results)
        if depth == "standard":
            return self._build_standard_prompt(trigger, action_summary, action_results)
        return self._build_light_prompt(trigger, action_summary, action_results)

    def _build_deep_prompt(self, trigger: str, summary: str, results: list[dict]) -> str:
        results_text = (
            "\n".join(f"- {r.get('tool', 'action')}: {r.get('result', '')}" for r in results)
            if results
            else "No tool results."
        )
        return (
            f"## Reflection (trigger: {trigger})\n\n"
            f"**Action taken:** {summary}\n\n"
            f"**Results:**\n{results_text}\n\n"
            "Evaluate this action:\n"
            "1. Did I do what was asked/intended?\n"
            "2. Did the actions succeed? **Verify concretely** — if you created "
            "a task, call `list_tasks` to confirm it exists. If you updated "
            "something, read it back. Do not assume success.\n"
            "3. Are there relevant rules I should evaluate now? Use "
            "`browse_rules` to check.\n"
            "4. Did I learn anything that should update memory?\n"
            "5. Is there follow-up work needed?\n"
            "6. Did I modify files directly when I should have created a task? "
            "An agent with a full context window and isolated workspace would "
            "do this better. If so, note this for improvement.\n\n"
            "If follow-up is needed, take action. Otherwise, confirm completion.\n\n"
            "After your analysis, output a JSON verdict on its own line:\n"
            '```json\n{"passed": true/false, "reason": "...", "followup": "suggested followup or null"}\n```'
        )

    def _build_standard_prompt(self, trigger: str, summary: str, results: list[dict]) -> str:
        results_text = (
            "\n".join(f"- {r.get('tool', 'action')}: {r.get('result', '')}" for r in results)
            if results
            else "No tool results."
        )
        return (
            f"## Reflection (trigger: {trigger})\n\n"
            f"**Action taken:** {summary}\n\n"
            f"**Results:**\n{results_text}\n\n"
            "Quick check:\n"
            "1. Did the action succeed?\n"
            "2. Any directly relevant rules to check?\n"
            "3. Did I do inline file work (write/edit) that should have been "
            "delegated as a task? Agents execute code changes more reliably.\n\n"
            "After your analysis, output a JSON verdict on its own line:\n"
            '```json\n{"passed": true/false, "reason": "...", "followup": "suggested followup or null"}\n```'
        )

    def _build_light_prompt(self, trigger: str, summary: str, results: list[dict]) -> str:
        return (
            f"## Reflection (trigger: {trigger})\n\n"
            f"**Summary:** {summary}\n\n"
            "Update memory if this is relevant. Skip if not notable."
        )

    def can_reflect_deeper(self, current_depth: int) -> bool:
        """Check whether another recursive reflection pass is allowed.

        Args:
            current_depth: How many reflection passes have run so far.

        Returns:
            ``True`` if *current_depth* is below ``max_depth``.
        """
        return current_depth < self._config.max_depth

    def can_continue_cycle(self, tokens_used: int) -> bool:
        """Check whether the per-cycle token cap allows more reflection.

        Args:
            tokens_used: Tokens consumed in this reflection cycle so far.

        Returns:
            ``True`` if under the cap.
        """
        return tokens_used < self._config.per_cycle_token_cap

    def record_tokens(self, tokens: int) -> None:
        """Record token usage for circuit-breaker tracking.

        Args:
            tokens: Estimated tokens consumed by a reflection call.
        """
        self._token_ledger.append((time.time(), tokens))

    def hourly_tokens_used(self) -> int:
        """Return total tokens used by reflection in the last hour."""
        cutoff = time.time() - 3600
        return sum(t for ts, t in self._token_ledger if ts > cutoff)

    def is_circuit_breaker_tripped(self) -> bool:
        """Check whether hourly reflection token usage exceeds the limit."""
        return self.hourly_tokens_used() >= self._config.hourly_token_circuit_breaker

    @staticmethod
    def parse_verdict(text: str) -> ReflectionVerdict:
        """Extract a structured verdict from the LLM reflection response.

        Looks for a JSON block in the response text. Falls back to
        ``passed=True`` if no valid JSON is found (safe default — don't
        retry if we can't parse the verdict).
        """
        # Try to find JSON in a fenced code block first
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if not match:
            # Try bare JSON object on a line
            match = re.search(r'(\{"passed".*?\})', text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return ReflectionVerdict(
                    passed=bool(data.get("passed", True)),
                    reason=str(data.get("reason", "")),
                    suggested_followup=data.get("followup"),
                )
            except (json.JSONDecodeError, TypeError):
                pass
        # Default: assume passed if we can't parse
        return ReflectionVerdict(passed=True, reason="Could not parse verdict")
