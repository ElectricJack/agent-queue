"""Sparse-payload rendering through the formatter registry (api-cli plan 21).

The human surface renders whatever the daemon answers; optional fields are
frequently absent (fresh installs, minimal rows, older daemons).  A KeyError
in a Rich renderer turns an informational command into a crash, so every
formatter here is driven through ``apply_formatter`` — registry extraction
included — with rows stripped to their bare identifying fields.
"""

from __future__ import annotations

import io

from rich.console import Console


def _render(command: str, result: dict) -> str:
    from src.cli.formatter_registry import apply_formatter

    buffer = io.StringIO()
    console = Console(file=buffer, width=120, force_terminal=False)
    assert apply_formatter(command, result, console), f"no formatter applied for {command}"
    return buffer.getvalue()


def test_sparse_workspace_pool_and_profile_rows_render_without_keyerror():
    """Plan 21: omitted optional fields keep identifying columns visible."""
    # Workspaces: no name, no lock holder, empty path on the second row.
    out = _render("list_workspaces", {"workspaces": [
        {"id": "ws-1"},
        {"id": "ws-2", "workspace_path": "", "locked_by_task_id": "t-9"},
    ]})
    assert "ws-1" in out and "ws-2" in out
    assert "t-9" in out  # the one populated lock still shows

    # Pools: a row with only identity, and one quarantined with no max.
    out = _render("pool_status", {"pools": [
        {"project_id": "proj-a", "profile_id": "coder"},
        {"project_id": "proj-b", "profile_id": "review", "max_active": None,
         "quarantined_until": 4102444800.0},
    ]})
    assert "proj-a" in out and "coder" in out
    assert "proj-b" in out and "∞" in out

    # Profile list: profiles with no tools/model/mcp, and count omitted.
    out = _render("list_profiles", {"profiles": [
        {"id": "triage"},
        {"id": "coder", "name": "Coder"},
    ]})
    assert "triage" in out and "coder" in out

    # Profile detail: an id-only profile keeps its identity visible, and a
    # fully empty payload renders the explicit empty-profile panel.
    out = _render("get_profile", {"id": "bare"})
    assert "bare" in out
    out = _render("get_profile", {})
    assert "empty profile" in out

    # Empty collections take the registry's empty-message path, not a crash.
    buffer = io.StringIO()
    console = Console(file=buffer, width=120)
    from src.cli.formatter_registry import apply_formatter

    assert apply_formatter("pool_status", {"pools": []}, console)
    assert "No worker pools configured" in buffer.getvalue()

    # And a fully absent list key behaves like the empty list.
    out = _render("list_workspaces", {})
    assert "Workspaces" in out
