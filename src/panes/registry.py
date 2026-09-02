"""Server-side mirror of the frontend pane view registry.

Kept in sync manually with dashboard/src/panes/<view>/manifest.ts.
Adding a pane view is a two-line change here (view id + agent_pushable
flag). The parity test in tests/test_pane_registry_parity.py enforces
the two lists are equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaneEntry:
    id: str
    agent_pushable: bool


SERVER_PANE_REGISTRY: dict[str, PaneEntry] = {
    # Populated by per-view specs as they land.
    "__stub-smoke": PaneEntry(id="__stub-smoke", agent_pushable=True),
    "console-stream": PaneEntry(id="console-stream", agent_pushable=True),
    "contextual-settings": PaneEntry(id="contextual-settings", agent_pushable=True),
    "diff-review-changes": PaneEntry(id="diff-review-changes", agent_pushable=True),
    "playbook-detail": PaneEntry(id="playbook-detail", agent_pushable=True),
    "file-browser": PaneEntry(id="file-browser", agent_pushable=True),
    "playbook-run-inspector": PaneEntry(id="playbook-run-inspector", agent_pushable=True),
    "proposal-preview": PaneEntry(id="proposal-preview", agent_pushable=True),
    "session-peek": PaneEntry(id="session-peek", agent_pushable=True),
    "spec-doc-reader": PaneEntry(id="spec-doc-reader", agent_pushable=True),
    "task-detail": PaneEntry(id="task-detail", agent_pushable=True),
}
