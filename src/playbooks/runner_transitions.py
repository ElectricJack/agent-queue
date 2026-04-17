"""TransitionMixin — transition evaluation methods for PlaybookRunner.

Extracted from :mod:`src.playbooks.runner` to reduce file size.
These methods handle determining which transition to follow after a
node completes, including structured (deterministic) evaluation,
expression parsing, and LLM-based classification.

Module-level helpers (``_dot_get``, ``_parse_literal``, ``_compare``,
``_event_to_fallback_status``) are also defined here as they are used
exclusively by transition evaluation logic.

The mixin expects the following attributes on ``self``:
- ``event`` — trigger event dict
- ``supervisor`` — :class:`Supervisor` instance
- ``messages`` — conversation history list
- ``tokens_used`` — cumulative token count
- ``_dry_run`` — dry-run mode flag
- ``_llm_config`` — playbook-level LLM config
- ``_transition_llm_config`` — playbook-level transition LLM config
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.models import PlaybookRunEvent, PlaybookRunStatus
from src.playbooks.token_tracker import _estimate_tokens

logger = logging.getLogger(__name__)


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


class TransitionMixin:
    """Mixin providing transition evaluation methods for the PlaybookRunner."""

    # Attributes expected from PlaybookRunner (for type checking purposes)
    event: dict
    supervisor: Any  # Supervisor
    messages: list[dict]
    tokens_used: int
    _dry_run: bool
    _llm_config: dict | None
    _transition_llm_config: dict | None

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
