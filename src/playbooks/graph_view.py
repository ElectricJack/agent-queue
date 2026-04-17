"""Dashboard-ready playbook graph view — structured JSON for interactive rendering.

Produces a complete graph view representation of a compiled playbook suitable for
dashboard rendering.  Nodes become positioned boxes (color-coded by type), transitions
become labelled arrows, and optional overlays add live state (current node highlighting)
and run history (path taken through the graph).

This module is the data layer for spec §14 (Dashboard Visualization):

- **Graph view**: nodes as boxes, transitions as arrows, conditions as edge labels.
  Color-code by node type (action, decision, human checkpoint, terminal).
- **Live state**: for running instances, highlight the current node and show status
  of completed nodes.
- **Run history**: overlay a specific run's path through the graph, including per-node
  timing and token usage.

All functions are pure — they accept pre-fetched data and return dicts.  No database
access; the caller (command handler) is responsible for fetching runs and playbooks.

See ``docs/specs/design/playbooks.md`` §14 and the ``show_playbook_graph`` command
in §15 for related prior work.

Roadmap 5.7.2.

Example usage::

    from src.playbooks.graph_view import build_graph_view
    from src.playbooks.models import CompiledPlaybook

    playbook = CompiledPlaybook.from_dict(data)
    view = build_graph_view(playbook)
    # view is a dict ready for JSON serialization to a dashboard frontend

    # With live state and run overlay:
    view = build_graph_view(
        playbook,
        active_runs=active_run_list,
        run_overlay=specific_run,
        node_metrics=health_node_metrics,
    )
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.models import PlaybookRun
    from src.playbooks.models import CompiledPlaybook, PlaybookNode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node type classification & color palette
# ---------------------------------------------------------------------------

# Color palette follows the existing Mermaid styles from playbook_graph.py
NODE_TYPE_COLORS: dict[str, dict[str, str]] = {
    "entry": {"fill": "#4CAF50", "stroke": "#2E7D32", "text": "#ffffff"},
    "entry+decision": {"fill": "#4CAF50", "stroke": "#2E7D32", "text": "#ffffff"},
    "terminal": {"fill": "#9E9E9E", "stroke": "#616161", "text": "#ffffff"},
    "checkpoint": {"fill": "#2196F3", "stroke": "#0D47A1", "text": "#ffffff"},
    "decision": {"fill": "#FF9800", "stroke": "#E65100", "text": "#ffffff"},
    "action": {"fill": "#E3F2FD", "stroke": "#1565C0", "text": "#000000"},
}

NODE_TYPE_SYMBOLS: dict[str, str] = {
    "entry": "▶",
    "entry+decision": "▶◆",
    "terminal": "■",
    "checkpoint": "⏸",
    "decision": "◆",
    "action": "●",
}

# Highlight colors for live state
LIVE_STATE_COLORS: dict[str, dict[str, str]] = {
    "active": {"fill": "#FFC107", "stroke": "#FF6F00", "text": "#000000"},
    "completed": {"fill": "#C8E6C9", "stroke": "#388E3C", "text": "#000000"},
    "failed": {"fill": "#FFCDD2", "stroke": "#C62828", "text": "#000000"},
    "paused": {"fill": "#E1BEE7", "stroke": "#6A1B9A", "text": "#000000"},
}

# Run status label colors for the timeline legend
RUN_STATUS_COLORS: dict[str, str] = {
    "running": "#FFC107",
    "paused": "#E1BEE7",
    "completed": "#4CAF50",
    "failed": "#F44336",
    "timed_out": "#FF9800",
}


# ---------------------------------------------------------------------------
# Node / edge classification helpers
# ---------------------------------------------------------------------------


def _classify_node(node: PlaybookNode) -> str:
    """Classify a playbook node by type for display purposes.

    Returns one of: ``"entry"``, ``"entry+decision"``, ``"terminal"``,
    ``"checkpoint"``, ``"decision"``, or ``"action"``.
    """
    if node.terminal:
        return "terminal"
    if node.entry:
        if len(node.transitions) > 1:
            return "entry+decision"
        return "entry"
    if node.wait_for_human:
        return "checkpoint"
    if len(node.transitions) > 1:
        return "decision"
    return "action"


def _edge_label(transition) -> str:
    """Build a human-readable label for a transition edge."""
    if transition.otherwise:
        return "otherwise"
    if isinstance(transition.when, str):
        return transition.when
    if isinstance(transition.when, dict):
        return str(transition.when)
    return ""


def _prompt_preview(node: PlaybookNode, max_len: int = 60) -> str:
    """Return a truncated first-line preview of the node's prompt."""
    if not node.prompt:
        return ""
    first_line = node.prompt.strip().split("\n")[0].strip()
    if len(first_line) > max_len:
        return first_line[: max_len - 3] + "..."
    return first_line


# ---------------------------------------------------------------------------
# Layout computation (topological BFS with column assignment)
# ---------------------------------------------------------------------------


def _compute_layout(
    playbook: CompiledPlaybook,
    direction: str = "TD",
) -> dict[str, dict[str, int]]:
    """Compute node positions using layered BFS layout.

    Assigns each node a (row, col) position based on BFS depth from the entry
    node.  Nodes at the same BFS depth are placed in the same row (for top-down
    layout) or same column (for left-right layout).

    Returns a dict mapping node_id → {"x": int, "y": int} in grid coordinates.
    The caller can scale these to pixel positions.
    """
    entry = playbook.entry_node_id()
    if not entry:
        # Fallback: single row
        return {
            nid: {"x": i, "y": 0} for i, nid in enumerate(playbook.nodes)
        }

    # BFS to assign layers (depth)
    layers: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque([(entry, 0)])
    visited: set[str] = set()

    while queue:
        nid, depth = queue.popleft()
        if nid in visited or nid not in playbook.nodes:
            continue
        visited.add(nid)
        layers[nid] = depth

        node = playbook.nodes[nid]
        successors: list[str] = []
        if node.goto is not None:
            successors.append(node.goto)
        for t in node.transitions:
            successors.append(t.goto)
        if node.on_timeout and node.on_timeout != node.goto:
            successors.append(node.on_timeout)

        for succ in successors:
            if succ not in visited:
                queue.append((succ, depth + 1))

    # Unreachable nodes get appended at the end
    max_depth = max(layers.values(), default=0)
    for nid in playbook.nodes:
        if nid not in layers:
            max_depth += 1
            layers[nid] = max_depth

    # Group by layer to assign column positions
    layer_groups: dict[int, list[str]] = {}
    for nid, depth in layers.items():
        layer_groups.setdefault(depth, []).append(nid)

    positions: dict[str, dict[str, int]] = {}
    for depth, nids in layer_groups.items():
        for col_idx, nid in enumerate(nids):
            if direction == "LR":
                positions[nid] = {"x": depth, "y": col_idx}
            else:
                positions[nid] = {"x": col_idx, "y": depth}

    return positions


# ---------------------------------------------------------------------------
# Node trace parsing
# ---------------------------------------------------------------------------


def _parse_node_trace(run: PlaybookRun) -> list[dict[str, Any]]:
    """Parse the node_trace JSON from a PlaybookRun into a list of dicts."""
    if not run.node_trace:
        return []
    if isinstance(run.node_trace, str):
        try:
            return json.loads(run.node_trace)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(run.node_trace, list):
        return run.node_trace
    return []


def _run_path(run: PlaybookRun) -> list[str]:
    """Extract the ordered list of node IDs visited in a run."""
    trace = _parse_node_trace(run)
    return [entry["node_id"] for entry in trace if "node_id" in entry]


def _run_edges(run: PlaybookRun) -> set[tuple[str, str]]:
    """Extract the set of edges (from_node, to_node) traversed in a run."""
    path = _run_path(run)
    edges: set[tuple[str, str]] = set()
    for i in range(len(path) - 1):
        edges.add((path[i], path[i + 1]))
    return edges


# ---------------------------------------------------------------------------
# Build individual graph view components
# ---------------------------------------------------------------------------


def build_nodes(
    playbook: CompiledPlaybook,
    positions: dict[str, dict[str, int]],
    *,
    show_prompts: bool = True,
    max_prompt_len: int = 60,
) -> list[dict[str, Any]]:
    """Build the node list for the graph view.

    Each node includes its id, type classification, display attributes (label,
    symbol, colors), position, and metadata (timeout, human review, etc.).
    """
    from src.playbooks.graph import _topo_order

    order = _topo_order(playbook)
    nodes: list[dict[str, Any]] = []

    for nid in order:
        node = playbook.nodes[nid]
        ntype = _classify_node(node)
        symbol = NODE_TYPE_SYMBOLS.get(ntype, "●")
        colors = NODE_TYPE_COLORS.get(ntype, NODE_TYPE_COLORS["action"])
        pos = positions.get(nid, {"x": 0, "y": 0})

        node_data: dict[str, Any] = {
            "id": nid,
            "type": ntype,
            "symbol": symbol,
            "label": nid,
            "position": pos,
            "colors": colors,
            "entry": node.entry,
            "terminal": node.terminal,
            "wait_for_human": node.wait_for_human,
        }

        if show_prompts and not node.terminal:
            preview = _prompt_preview(node, max_prompt_len)
            if preview:
                node_data["prompt_preview"] = preview

        # Optional metadata
        if node.timeout_seconds:
            node_data["timeout_seconds"] = node.timeout_seconds
        if node.on_timeout:
            node_data["on_timeout"] = node.on_timeout
        if node.summarize_before:
            node_data["summarize_before"] = True

        # Transition count for sizing hint
        out_edges = len(node.transitions) + (1 if node.goto else 0)
        node_data["out_degree"] = out_edges

        nodes.append(node_data)

    return nodes


def build_edges(playbook: CompiledPlaybook) -> list[dict[str, Any]]:
    """Build the edge list for the graph view.

    Each edge includes source, target, label, and edge type for styling.
    """
    from src.playbooks.graph import _topo_order

    order = _topo_order(playbook)
    edges: list[dict[str, Any]] = []

    for nid in order:
        node = playbook.nodes[nid]

        # Unconditional goto
        if node.goto is not None:
            edges.append({
                "source": nid,
                "target": node.goto,
                "label": "",
                "edge_type": "goto",
            })

        # Conditional transitions
        for t in node.transitions:
            label = _edge_label(t)
            edge_type = "otherwise" if t.otherwise else "condition"
            edges.append({
                "source": nid,
                "target": t.goto,
                "label": label,
                "edge_type": edge_type,
            })

        # Timeout edge
        if node.on_timeout and node.on_timeout != node.goto:
            edges.append({
                "source": nid,
                "target": node.on_timeout,
                "label": "timeout",
                "edge_type": "timeout",
            })

    return edges


# ---------------------------------------------------------------------------
# Live state overlay
# ---------------------------------------------------------------------------


def build_live_state(
    playbook: CompiledPlaybook,
    active_runs: list[PlaybookRun],
) -> dict[str, Any]:
    """Build the live state overlay for currently active playbook instances.

    Highlights the current node for running/paused instances and shows the
    status of nodes that have already been completed in the current run.

    Returns a dict with:
    - ``instances``: list of active instance summaries (run_id, status, current_node)
    - ``node_states``: dict mapping node_id → aggregated state from all active runs
    """
    if not active_runs:
        return {"instances": [], "node_states": {}}

    instances: list[dict[str, Any]] = []
    node_states: dict[str, dict[str, Any]] = {}

    for run in active_runs:
        if run.playbook_id != playbook.id:
            continue

        trace = _parse_node_trace(run)
        path = [e["node_id"] for e in trace if "node_id" in e]

        instance: dict[str, Any] = {
            "run_id": run.run_id,
            "status": run.status,
            "current_node": run.current_node,
            "started_at": run.started_at,
            "tokens_used": run.tokens_used,
            "nodes_visited": len(path),
        }
        instances.append(instance)

        # Mark completed nodes from this run's trace
        for entry in trace:
            node_id = entry.get("node_id")
            if not node_id:
                continue

            status = entry.get("status", "completed")
            if node_id not in node_states:
                node_states[node_id] = {
                    "status": status,
                    "active_instances": 0,
                    "highlight": LIVE_STATE_COLORS.get(
                        "completed" if status in ("completed", "ok") else status,
                        LIVE_STATE_COLORS["completed"],
                    ),
                }

        # Highlight the current node for running/paused instances.
        # This is separate from the trace loop because the current node
        # may not have a trace entry yet (execution is in progress).
        if run.current_node and run.status in ("running", "paused"):
            state_key = "active" if run.status == "running" else "paused"
            current = run.current_node
            prev_count = (
                node_states[current].get("active_instances", 0) if current in node_states else 0
            )
            node_states[current] = {
                "status": state_key,
                "active_instances": prev_count + 1,
                "highlight": LIVE_STATE_COLORS[state_key],
            }

    return {
        "instances": instances,
        "node_states": node_states,
    }


# ---------------------------------------------------------------------------
# Run overlay (single run path highlight)
# ---------------------------------------------------------------------------


def build_run_overlay(
    playbook: CompiledPlaybook,
    run: PlaybookRun,
) -> dict[str, Any]:
    """Build a run overlay showing the path taken through the graph.

    Returns detailed per-node execution data for a specific run, suitable for
    highlighting the path through the graph and showing timing/token info
    when clicking on individual nodes.
    """
    trace = _parse_node_trace(run)
    path = _run_path(run)
    traversed_edges = _run_edges(run)

    node_details: dict[str, dict[str, Any]] = {}
    for entry in trace:
        node_id = entry.get("node_id")
        if not node_id:
            continue

        detail: dict[str, Any] = {
            "visited": True,
            "status": entry.get("status", "completed"),
            "order": path.index(node_id) if node_id in path else -1,
        }

        # Timing
        started = entry.get("started_at")
        completed = entry.get("completed_at")
        if started and completed:
            detail["duration_seconds"] = round(completed - started, 2)
            detail["started_at"] = started
            detail["completed_at"] = completed
        elif started:
            detail["started_at"] = started

        # Transition info
        if entry.get("transition_to"):
            detail["transition_to"] = entry["transition_to"]
        if entry.get("transition_method"):
            detail["transition_method"] = entry["transition_method"]

        # Token usage
        tokens = entry.get("tokens_used", 0)
        if tokens:
            detail["tokens_used"] = tokens

        # Determine highlight color
        status = entry.get("status", "completed")
        if status in ("completed", "ok"):
            detail["highlight"] = LIVE_STATE_COLORS["completed"]
        elif status == "failed":
            detail["highlight"] = LIVE_STATE_COLORS["failed"]
        else:
            detail["highlight"] = LIVE_STATE_COLORS.get(status, LIVE_STATE_COLORS["completed"])

        node_details[node_id] = detail

    # Mark edges that were traversed
    highlighted_edges: list[dict[str, str]] = [
        {"source": src, "target": tgt}
        for src, tgt in traversed_edges
    ]

    return {
        "run_id": run.run_id,
        "status": run.status,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "tokens_used": run.tokens_used,
        "error": run.error,
        "path": path,
        "node_details": node_details,
        "highlighted_edges": highlighted_edges,
    }


# ---------------------------------------------------------------------------
# Run history timeline
# ---------------------------------------------------------------------------


def build_run_history(
    runs: list[PlaybookRun],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Build a timeline of past runs for the run history panel.

    Returns a list of run summaries sorted by start time (most recent first),
    each containing the path taken and status information.
    """
    # Sort by started_at descending
    sorted_runs = sorted(runs, key=lambda r: r.started_at or 0, reverse=True)

    history: list[dict[str, Any]] = []
    for run in sorted_runs[:limit]:
        path = _run_path(run)
        trace = _parse_node_trace(run)

        # Compute total duration
        duration = None
        if run.started_at and run.completed_at:
            duration = round(run.completed_at - run.started_at, 2)

        # Count per-status nodes
        node_statuses: dict[str, int] = {}
        for entry in trace:
            s = entry.get("status", "unknown")
            node_statuses[s] = node_statuses.get(s, 0) + 1

        entry: dict[str, Any] = {
            "run_id": run.run_id,
            "status": run.status,
            "status_color": RUN_STATUS_COLORS.get(run.status, "#9E9E9E"),
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "duration_seconds": duration,
            "tokens_used": run.tokens_used,
            "path": path,
            "nodes_visited": len(path),
            "node_statuses": node_statuses,
        }

        if run.error:
            entry["error"] = run.error[:200]  # Truncate for summary

        history.append(entry)

    return history


# ---------------------------------------------------------------------------
# Node metrics overlay
# ---------------------------------------------------------------------------


def build_node_metrics_overlay(
    node_metrics: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Transform health metrics into per-node overlay data for the graph view.

    Takes the ``node_metrics`` dict from ``compute_node_metrics()`` and
    restructures it for dashboard display (badge counts, color intensities
    based on failure rate, etc.).
    """
    if not node_metrics:
        return {}

    overlay: dict[str, dict[str, Any]] = {}
    for node_id, metrics in node_metrics.items():
        failure_rate = metrics.get("failure_rate", 0)

        # Color intensity for failure heat map (green → yellow → red)
        if failure_rate == 0:
            heat_color = "#4CAF50"  # green
        elif failure_rate < 0.1:
            heat_color = "#8BC34A"  # light green
        elif failure_rate < 0.25:
            heat_color = "#FFC107"  # amber
        elif failure_rate < 0.5:
            heat_color = "#FF9800"  # orange
        else:
            heat_color = "#F44336"  # red

        overlay[node_id] = {
            "execution_count": metrics.get("execution_count", 0),
            "failure_rate": round(failure_rate, 3),
            "avg_duration_seconds": metrics.get("avg_duration_seconds"),
            "p95_duration_seconds": metrics.get("p95_duration_seconds"),
            "avg_tokens": metrics.get("avg_tokens"),
            "heat_color": heat_color,
        }

    return overlay


# ---------------------------------------------------------------------------
# Main entry point: build_graph_view
# ---------------------------------------------------------------------------


def build_graph_view(
    playbook: CompiledPlaybook,
    *,
    direction: str = "TD",
    show_prompts: bool = True,
    max_prompt_len: int = 60,
    active_runs: list[PlaybookRun] | None = None,
    run_overlay: PlaybookRun | None = None,
    all_runs: list[PlaybookRun] | None = None,
    node_metrics: dict[str, Any] | None = None,
    history_limit: int = 20,
) -> dict[str, Any]:
    """Build the complete graph view data structure for dashboard rendering.

    Combines the static graph structure with optional dynamic overlays:

    Parameters
    ----------
    playbook:
        The compiled playbook to visualize.
    direction:
        Layout direction — ``"TD"`` (top-down) or ``"LR"`` (left-right).
    show_prompts:
        Include truncated prompt previews in node labels.
    max_prompt_len:
        Maximum characters for prompt preview text.
    active_runs:
        List of currently running/paused instances for live state overlay.
    run_overlay:
        A specific run to highlight in the graph (path overlay).
    all_runs:
        All recent runs for the run history timeline.
    node_metrics:
        Pre-computed node metrics from ``compute_node_metrics()`` output.
    history_limit:
        Maximum number of runs in the history timeline.

    Returns
    -------
    dict
        A JSON-serializable dict with keys:

        - ``playbook``: identity (id, version, scope, triggers)
        - ``graph``: nodes and edges
        - ``layout``: layout metadata (direction, positions)
        - ``live_state``: (optional) active instance overlay
        - ``run_overlay``: (optional) specific run path highlight
        - ``run_history``: (optional) timeline of past runs
        - ``node_metrics``: (optional) per-node health metrics
        - ``legend``: color legend for node types and states
    """
    if not playbook.nodes:
        return {
            "playbook": {
                "id": playbook.id,
                "version": playbook.version,
                "scope": playbook.scope,
            },
            "graph": {"nodes": [], "edges": []},
            "layout": {"direction": direction},
            "legend": _build_legend(),
        }

    # Compute layout
    positions = _compute_layout(playbook, direction)

    # Build static graph structure
    nodes = build_nodes(
        playbook,
        positions,
        show_prompts=show_prompts,
        max_prompt_len=max_prompt_len,
    )
    edges = build_edges(playbook)

    # Build trigger list
    triggers = []
    for t in playbook.triggers:
        if hasattr(t, "event_type"):
            trigger_data: dict[str, Any] = {"event_type": t.event_type}
            if hasattr(t, "filter") and t.filter:
                trigger_data["filter"] = t.filter
            triggers.append(trigger_data)
        else:
            triggers.append({"event_type": str(t)})

    result: dict[str, Any] = {
        "playbook": {
            "id": playbook.id,
            "version": playbook.version,
            "scope": playbook.scope,
            "triggers": triggers,
            "node_count": len(playbook.nodes),
            "compiled_at": playbook.compiled_at,
        },
        "graph": {
            "nodes": nodes,
            "edges": edges,
        },
        "layout": {
            "direction": direction,
            "grid_positions": positions,
        },
        "legend": _build_legend(),
    }

    # Optional overlays
    if active_runs:
        result["live_state"] = build_live_state(playbook, active_runs)

    if run_overlay is not None:
        result["run_overlay"] = build_run_overlay(playbook, run_overlay)

    if all_runs:
        result["run_history"] = build_run_history(all_runs, limit=history_limit)

    if node_metrics is not None:
        result["node_metrics"] = build_node_metrics_overlay(node_metrics)

    return result


# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------


def _build_legend() -> dict[str, Any]:
    """Build the color legend for the graph view."""
    return {
        "node_types": {
            ntype: {
                "symbol": NODE_TYPE_SYMBOLS.get(ntype, "●"),
                "colors": colors,
                "label": ntype.replace("+", " + ").replace("_", " ").title(),
            }
            for ntype, colors in NODE_TYPE_COLORS.items()
        },
        "live_states": {
            state: {
                "colors": colors,
                "label": state.title(),
            }
            for state, colors in LIVE_STATE_COLORS.items()
        },
        "run_statuses": {
            status: {
                "color": color,
                "label": status.replace("_", " ").title(),
            }
            for status, color in RUN_STATUS_COLORS.items()
        },
    }
