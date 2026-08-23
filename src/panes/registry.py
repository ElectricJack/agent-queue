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
}
