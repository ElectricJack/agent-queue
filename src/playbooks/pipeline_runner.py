"""Runner for ``kind: pipeline`` playbooks — deterministic action dispatch.

Walks a compiled pipeline graph and executes each action node by calling
``CommandHandler.execute()`` directly. No LLM anywhere in the loop.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_TMPL_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


@dataclass
class RunResult:
    run_id: str
    status: str  # completed | failed
    error: str | None = None
    outputs: dict | None = None


def _resolve_ref(ref: str, event: dict, outputs: dict) -> Any:
    """Resolve ``event.foo.bar`` or ``outputs.name.field`` to a value."""
    parts = ref.split(".")
    if not parts:
        return None
    root = parts[0]
    if root == "event":
        cur: Any = event
    elif root == "outputs":
        cur = outputs
    else:
        return None
    for p in parts[1:]:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _substitute(value: Any, event: dict, outputs: dict) -> Any:
    """Recursively substitute {{...}} placeholders in a JSON-ish structure."""
    if isinstance(value, str):
        # If the entire string is one placeholder, return the raw value (not str).
        m = _TMPL_RE.fullmatch(value)
        if m:
            return _resolve_ref(m.group(1), event, outputs)
        return _TMPL_RE.sub(
            lambda mm: "" if (v := _resolve_ref(mm.group(1), event, outputs)) is None else str(v),
            value,
        )
    if isinstance(value, dict):
        return {k: _substitute(v, event, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, event, outputs) for v in value]
    return value


def _flatten_node(node: dict) -> dict:
    """Return a flat action-node view regardless of compiled vs raw shape.

    Compiled nodes carry action fields nested under ``action`` (see
    ``PlaybookNode.to_dict``); raw / hand-authored graphs used by unit
    tests keep them at the top level.  Fields set at the top level
    override values from the nested dict so callers can still layer
    control keys like ``terminal`` / ``entry`` above the action body.
    """
    if "action" in node and isinstance(node["action"], dict):
        merged: dict = dict(node["action"])
        for k, v in node.items():
            if k == "action":
                continue
            merged[k] = v
        return merged
    return node


class PipelineRunner:
    def __init__(self, graph: dict, event: dict, handler, db=None) -> None:
        self.graph = graph
        self.event = event
        self.handler = handler
        self.db = db
        self.run_id = uuid.uuid4().hex[:12]
        self.outputs: dict[str, Any] = {}

    def _entry(self) -> str | None:
        for nid, node in self.graph["nodes"].items():
            if node.get("entry"):
                return nid
        return None

    async def run(self) -> RunResult:
        current = self._entry()
        if current is None:
            return RunResult(self.run_id, "failed", "No entry node")

        visited: set[str] = set()
        while current:
            if current in visited:
                return RunResult(self.run_id, "failed", f"Cycle at '{current}'")
            visited.add(current)
            raw_node = self.graph["nodes"].get(current)
            if raw_node is None:
                return RunResult(self.run_id, "failed", f"Node '{current}' missing")
            if raw_node.get("terminal"):
                return RunResult(self.run_id, "completed", outputs=self.outputs)
            # Compiled pipeline nodes carry action data nested under ``action``
            # (see ``PlaybookNode.to_dict``); raw dispatch (older callers +
            # unit tests) uses a flat shape.  Normalize by preferring
            # ``action`` fields when present and falling back to the flat
            # shape.
            node = _flatten_node(raw_node)

            fe = node.get("for_each")
            if fe:
                # Pass the flattened node — _run_for_each reads command/args
                # off it and would otherwise trip on the nested action dict.
                current = await self._run_for_each(current, node)
                if isinstance(current, RunResult):
                    return current
                continue

            cmd = node.get("command")
            args = _substitute(node.get("args") or {}, self.event, self.outputs)
            try:
                result = await self.handler.execute(cmd, args)
            except Exception as exc:
                logger.exception("pipeline node %s raised", current)
                return RunResult(self.run_id, "failed", str(exc))

            # Commands normally return {"success": bool}; absent key defaults to success
            # unless "error" is present.
            success = not (result.get("success") is False or "error" in result)
            if success:
                out_spec = node.get("output")
                if out_spec:
                    self.outputs[out_spec["as"]] = result
            hop = node.get("on_success" if success else "on_failure")
            if hop is None:
                return RunResult(
                    self.run_id,
                    "completed" if success else "failed",
                    None if success else str(result.get("error")),
                    outputs=self.outputs,
                )
            if hop not in self.graph["nodes"]:
                return RunResult(self.run_id, "failed", f"Missing target '{hop}'")
            current = hop

        return RunResult(self.run_id, "completed", outputs=self.outputs)

    async def _run_for_each(self, node_id: str, node: dict):
        fe = node["for_each"]
        src = _resolve_ref(fe["source"], self.event, self.outputs)
        if not isinstance(src, list):
            return RunResult(self.run_id, "failed", f"for_each.source not a list at {node_id}")
        cmd = node.get("command")
        args_tmpl = node.get("args") or {}
        var = fe["as"]
        # The loop variable is scoped to the iterations: pop it on every exit
        # (success, on_failure hop, failed result, raised command) so failure-
        # branch substitutions can never resolve it to a stale item.
        try:
            for item in src:
                self.outputs[var] = item
                args = _substitute(args_tmpl, self.event, self.outputs)
                result = await self.handler.execute(cmd, args)
                if not result.get("success"):
                    fail_hop = node.get("on_failure")
                    if fail_hop:
                        return fail_hop
                    return RunResult(self.run_id, "failed", str(result.get("error")))
        finally:
            self.outputs.pop(var, None)
        return node.get("on_success")
