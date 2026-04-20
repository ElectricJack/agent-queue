"""Render compiled playbook graphs as ASCII art or Mermaid diagram syntax.

Provides two output formats for visualising :class:`CompiledPlaybook` graphs:

- **ASCII** — box-drawing characters, suitable for terminals and plain-text
  channels (Discord code blocks, CLI output).
- **Mermaid** — `flowchart TD` syntax, renderable by GitHub, GitLab, Obsidian,
  and the Mermaid Live Editor.

Both renderers perform a topological-ish walk from the entry node, preserving
the logical order of the playbook's execution flow while handling conditional
branches and convergence points.

See ``docs/specs/design/playbooks.md`` §14 (Dashboard Visualization) and §15
(``show_playbook_graph`` command).

Typical usage::

    from src.playbooks.graph import render_ascii, render_mermaid
    from src.playbooks.models import CompiledPlaybook

    playbook = CompiledPlaybook.from_dict(data)
    print(render_ascii(playbook))
    print(render_mermaid(playbook))
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.playbooks.models import CompiledPlaybook, PlaybookNode


# ---------------------------------------------------------------------------
# Node classification
# ---------------------------------------------------------------------------


def _node_type(node: PlaybookNode) -> str:
    """Classify a node for display purposes.

    Returns one of: ``"entry"``, ``"terminal"``, ``"checkpoint"``,
    ``"decision"``, or ``"action"``.
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


def _node_type_symbol(node: PlaybookNode) -> str:
    """Return a short symbol/badge for the node type."""
    ntype = _node_type(node)
    return {
        "entry": "▶",
        "entry+decision": "▶◆",
        "terminal": "■",
        "checkpoint": "⏸",
        "decision": "◆",
        "action": "●",
    }.get(ntype, "●")


def _prompt_preview(node: PlaybookNode, max_len: int = 50) -> str:
    """Return a truncated first-line preview of the node's prompt."""
    if not node.prompt:
        return ""
    first_line = node.prompt.strip().split("\n")[0].strip()
    if len(first_line) > max_len:
        return first_line[: max_len - 3] + "..."
    return first_line


def _edge_label(transition) -> str:
    """Build a human-readable label for a transition edge."""
    if transition.otherwise:
        return "otherwise"
    if isinstance(transition.when, str):
        return transition.when
    if isinstance(transition.when, dict):
        # Structured expression — show a compact repr
        return str(transition.when)
    return ""


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------


def _topo_order(playbook: CompiledPlaybook) -> list[str]:
    """Return node IDs in a BFS order starting from the entry node.

    Unreachable nodes are appended at the end (shouldn't happen in valid
    playbooks but handled for robustness).
    """
    entry = playbook.entry_node_id()
    if not entry:
        # Fallback: return dict order
        return list(playbook.nodes.keys())

    ordered: list[str] = []
    visited: set[str] = set()
    queue: deque[str] = deque([entry])

    while queue:
        nid = queue.popleft()
        if nid in visited or nid not in playbook.nodes:
            continue
        visited.add(nid)
        ordered.append(nid)

        node = playbook.nodes[nid]
        # Add successors: goto first, then transitions in order
        if node.goto is not None and node.goto not in visited:
            queue.append(node.goto)
        for t in node.transitions:
            if t.goto not in visited:
                queue.append(t.goto)
        # on_timeout edge
        if node.on_timeout and node.on_timeout not in visited:
            queue.append(node.on_timeout)

    # Append any unreachable nodes
    for nid in playbook.nodes:
        if nid not in visited:
            ordered.append(nid)

    return ordered


# ---------------------------------------------------------------------------
# Edge collection
# ---------------------------------------------------------------------------


def _collect_edges(
    playbook: CompiledPlaybook,
) -> list[tuple[str, str, str]]:
    """Collect all edges as ``(source_id, target_id, label)`` triples."""
    edges: list[tuple[str, str, str]] = []
    for nid, node in playbook.nodes.items():
        if node.goto is not None:
            edges.append((nid, node.goto, ""))
        for t in node.transitions:
            edges.append((nid, t.goto, _edge_label(t)))
        if node.on_timeout and node.on_timeout != node.goto:
            edges.append((nid, node.on_timeout, "timeout"))
    return edges


# ---------------------------------------------------------------------------
# ASCII renderer
# ---------------------------------------------------------------------------


def render_ascii(
    playbook: CompiledPlaybook,
    *,
    show_prompts: bool = True,
    max_prompt_len: int = 50,
) -> str:
    """Render a compiled playbook graph as ASCII art.

    The output shows each node as a labelled box with its type badge, and
    edges drawn as indented arrows beneath each node.

    Parameters
    ----------
    playbook:
        The compiled playbook to render.
    show_prompts:
        Include a truncated prompt preview in each node box.
    max_prompt_len:
        Maximum characters for the prompt preview line.

    Returns
    -------
    str
        Multi-line ASCII representation.
    """
    if not playbook.nodes:
        return f"Playbook '{playbook.id}' has no nodes."

    order = _topo_order(playbook)
    lines: list[str] = []

    # Header
    lines.append(f"Playbook: {playbook.id} (v{playbook.version}, scope={playbook.scope})")
    triggers = ", ".join(
        t.event_type if hasattr(t, "event_type") else str(t) for t in playbook.triggers
    )
    lines.append(f"Triggers: {triggers}")
    lines.append(f"Nodes: {len(playbook.nodes)}")
    lines.append("")

    for nid in order:
        node = playbook.nodes[nid]
        symbol = _node_type_symbol(node)
        ntype = _node_type(node)

        # Build the box content lines
        box_lines: list[str] = []
        box_lines.append(f"{symbol} {nid}  [{ntype}]")
        if show_prompts and not node.terminal:
            preview = _prompt_preview(node, max_prompt_len)
            if preview:
                box_lines.append(f'  "{preview}"')
        if node.wait_for_human:
            box_lines.append("  ⏸ waits for human review")
        if node.timeout_seconds:
            box_lines.append(f"  ⏱ timeout: {node.timeout_seconds}s")

        # Compute box width
        content_width = max(len(line) for line in box_lines)
        box_width = content_width + 4  # 2 padding + 2 border chars

        # Draw the box
        lines.append("┌" + "─" * (box_width - 2) + "┐")
        for bl in box_lines:
            padded = bl.ljust(content_width)
            lines.append(f"│ {padded} │")
        lines.append("└" + "─" * (box_width - 2) + "┘")

        # Draw outgoing edges
        if node.goto is not None:
            lines.append(f"  └──→ {node.goto}")
        for t in node.transitions:
            label = _edge_label(t)
            if label:
                lines.append(f"  ├──→ {t.goto}  [{label}]")
            else:
                lines.append(f"  ├──→ {t.goto}")
        if node.on_timeout and node.on_timeout != node.goto:
            lines.append(f"  └──⏱→ {node.on_timeout}  [timeout]")

        lines.append("")  # blank line between nodes

    # Legend
    lines.append("Legend: ▶ entry  ■ terminal  ◆ decision  ⏸ checkpoint  ● action")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mermaid renderer
# ---------------------------------------------------------------------------

# Characters that need escaping in Mermaid node labels
_MERMAID_SPECIAL = str.maketrans(
    {
        '"': "'",
        "<": "&lt;",
        ">": "&gt;",
        "&": "&amp;",
    }
)


def _mermaid_escape(text: str) -> str:
    """Escape text for safe inclusion in a Mermaid node label."""
    return text.translate(_MERMAID_SPECIAL)


def _mermaid_node_shape(nid: str, node: PlaybookNode, show_prompts: bool, max_prompt_len: int):
    """Return the Mermaid node definition string.

    Uses different shapes for different node types:
    - Terminal: ``[[id]]`` (stadium / rounded)
    - Decision: ``{id}`` (rhombus)
    - Checkpoint: ``>id]`` (flag / asymmetric)
    - Entry/Action: ``[id]`` (rectangle)
    """
    ntype = _node_type(node)
    symbol = _node_type_symbol(node)

    label_parts = [f"{symbol} {nid}"]
    if show_prompts and not node.terminal:
        preview = _prompt_preview(node, max_prompt_len)
        if preview:
            label_parts.append(_mermaid_escape(preview))
    if node.wait_for_human:
        label_parts.append("⏸ human review")

    label = "<br/>".join(label_parts)

    # Use safe Mermaid IDs (replace hyphens/special chars)
    safe_id = nid.replace("-", "_").replace(".", "_").replace(" ", "_")

    if node.terminal:
        return f'    {safe_id}(["{label}"])'
    if ntype in ("decision", "entry+decision"):
        return f'    {safe_id}{{"{label}"}}'
    if node.wait_for_human:
        return f'    {safe_id}>"{label}"]'
    # Default: rectangle
    return f'    {safe_id}["{label}"]'


def render_mermaid(
    playbook: CompiledPlaybook,
    *,
    direction: str = "TD",
    show_prompts: bool = True,
    max_prompt_len: int = 40,
) -> str:
    """Render a compiled playbook graph as Mermaid flowchart syntax.

    Parameters
    ----------
    playbook:
        The compiled playbook to render.
    direction:
        Flowchart direction — ``"TD"`` (top-down) or ``"LR"`` (left-right).
    show_prompts:
        Include truncated prompt previews in node labels.
    max_prompt_len:
        Maximum characters for prompt preview text.

    Returns
    -------
    str
        Mermaid flowchart syntax that can be pasted into any Mermaid
        renderer (GitHub markdown, Obsidian, mermaid.live, etc.).
    """
    if not playbook.nodes:
        return f"%%{{ Playbook '{playbook.id}' has no nodes }}%%"

    order = _topo_order(playbook)
    lines: list[str] = []

    # Header comment
    lines.append(f'---\ntitle: "{playbook.id} v{playbook.version} ({playbook.scope})"\n---')
    lines.append(f"flowchart {direction}")

    # Node definitions
    for nid in order:
        node = playbook.nodes[nid]
        lines.append(_mermaid_node_shape(nid, node, show_prompts, max_prompt_len))

    lines.append("")  # blank separator

    # Edges
    for nid in order:
        node = playbook.nodes[nid]
        safe_src = nid.replace("-", "_").replace(".", "_").replace(" ", "_")

        if node.goto is not None:
            safe_tgt = node.goto.replace("-", "_").replace(".", "_").replace(" ", "_")
            lines.append(f"    {safe_src} --> {safe_tgt}")

        for t in node.transitions:
            safe_tgt = t.goto.replace("-", "_").replace(".", "_").replace(" ", "_")
            label = _edge_label(t)
            if label:
                escaped = _mermaid_escape(label)
                lines.append(f'    {safe_src} -->|"{escaped}"| {safe_tgt}')
            else:
                lines.append(f"    {safe_src} --> {safe_tgt}")

        if node.on_timeout and node.on_timeout != node.goto:
            safe_tgt = node.on_timeout.replace("-", "_").replace(".", "_").replace(" ", "_")
            lines.append(f'    {safe_src} -.->|"timeout"| {safe_tgt}')

    # Style classes for node types
    lines.append("")
    lines.append("    %% Node type styles")

    entry_ids = []
    terminal_ids = []
    decision_ids = []
    checkpoint_ids = []
    action_ids = []

    for nid in order:
        node = playbook.nodes[nid]
        safe_id = nid.replace("-", "_").replace(".", "_").replace(" ", "_")
        ntype = _node_type(node)
        if ntype in ("entry", "entry+decision"):
            entry_ids.append(safe_id)
        if node.terminal:
            terminal_ids.append(safe_id)
        elif ntype == "decision":
            decision_ids.append(safe_id)
        elif node.wait_for_human:
            checkpoint_ids.append(safe_id)
        else:
            action_ids.append(safe_id)

    lines.append("    classDef entryNode fill:#4CAF50,stroke:#2E7D32,color:#fff")
    lines.append("    classDef terminalNode fill:#9E9E9E,stroke:#616161,color:#fff")
    lines.append("    classDef decisionNode fill:#FF9800,stroke:#E65100,color:#fff")
    lines.append("    classDef checkpointNode fill:#2196F3,stroke:#0D47A1,color:#fff")
    lines.append("    classDef actionNode fill:#E3F2FD,stroke:#1565C0,color:#000")

    if entry_ids:
        lines.append(f"    class {','.join(entry_ids)} entryNode")
    if terminal_ids:
        lines.append(f"    class {','.join(terminal_ids)} terminalNode")
    if decision_ids:
        lines.append(f"    class {','.join(decision_ids)} decisionNode")
    if checkpoint_ids:
        lines.append(f"    class {','.join(checkpoint_ids)} checkpointNode")
    if action_ids:
        lines.append(f"    class {','.join(action_ids)} actionNode")

    return "\n".join(lines)
