"""ContextMixin — template/context building methods for PlaybookRunner.

Extracted from :mod:`src.playbooks.runner` to reduce file size.
These methods handle building node prompts, rendering templates,
resolving output variables, extracting structured output from tool
results, and constructing per-node LLM context.

The mixin expects the following attributes on ``self``:
- ``node_outputs`` — dict of stored node outputs
- ``supervisor`` — :class:`Supervisor` instance
- ``on_progress`` — optional progress callback
- ``_seed_message`` — seed message string
- ``_llm_config`` — playbook-level LLM config
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class ContextMixin:
    """Mixin providing template/context building methods for the PlaybookRunner."""

    # Attributes expected from PlaybookRunner (for type checking purposes)
    node_outputs: dict[str, Any]
    supervisor: Any  # Supervisor
    on_progress: Callable[[str, str | None], Awaitable[None]] | None
    _seed_message: str
    _llm_config: dict | None

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

    def _store_node_output(self, node_id: str, node: dict, value: Any) -> None:
        """Store a node's output in node_outputs under the appropriate key."""
        output_spec = node.get("output")
        if output_spec and output_spec.get("as"):
            self.node_outputs[output_spec["as"]] = value
        else:
            self.node_outputs[node_id] = value

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
