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

    # Pools: a row with only identity, and one whose project breakdown is
    # missing entirely (one row per profile now -- global-worker-pools §6.1).
    out = _render("pool_status", {"pools": [
        {"profile_id": "coder"},
        {"profile_id": "review", "max_active": None, "projects": []},
    ]})
    assert "coder" in out and "review" in out
    assert "∞" in out

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


def test_task_progress_renders_a_raw_response_dict():
    """`aq task progress` crashed with AttributeError: the ``task_progress``
    spec had no proxy, so ``_render_progress`` got the raw response dict and
    ``p.parent_id`` blew up.  Render a real payload end-to-end."""
    out = _render("task_progress", {
        "success": True,
        "parent_id": "eager-impact-14",
        "total": 5,
        "done": 2,
        "ready": 1,
        "blocked": 1,
        "in_progress": 1,
        "waves": [["a", "b"], ["c"]],
        "max_parallelism": 2,
        "depth": 2,
    })
    assert "eager-impact-14: 2/5 done, 1 ready, 1 blocked, 1 in progress" in out
    assert "waves: 2, max parallelism: 2" in out
    assert "1. a, b" in out and "2. c" in out


def test_task_progress_renders_a_typed_response_model():
    """The same spec must handle the generated client's typed model."""
    from src.api.models.task import TaskProgressResponse

    out = _render("task_progress", TaskProgressResponse(
        success=True, parent_id="p-1", total=0, done=0, ready=0, blocked=0,
        in_progress=0, waves=[], max_parallelism=0, depth=0,
    ))
    assert "p-1: 0/0 done" in out


def test_recent_activity_renders_models_attempts_and_a_labelled_window():
    import time

    now = time.time()
    out = _render("task_recent_activity", {
        "success": True,
        "since": now - 86400,
        "until": now,
        "hours": 24.0,
        "total": 2,
        "truncated": False,
        "items": [
            {
                "task_id": "nimble-current", "title": "Tasks tab activity",
                "status": "COMPLETED", "outcome": "pass",
                "models": ["claude-opus-5"], "unattributed_attempts": 1,
                "attempt_count": 2, "last_activity_at": now - 600,
            },
            # A sparse row: no attempts, no outcome, no models.
            {"task_id": "hand-edited", "last_activity_at": now - 90},
        ],
        "by_model": [
            {"model": "claude-opus-5", "tasks": 1, "attempts": 1},
            {"model": None, "tasks": 1, "attempts": 1},
        ],
    })
    assert "last 24" in out
    assert "claude-opus-5" in out
    assert "unattributed" in out
    assert "no agent session" in out
    assert "10m ago" in out and "1m ago" in out


def test_recent_activity_renders_an_empty_window_without_a_model_table():
    import time

    now = time.time()
    out = _render("task_recent_activity", {
        "success": True, "since": now - 3600, "until": now, "hours": 1.0,
        "total": 0, "truncated": False, "items": [], "by_model": [],
    })
    assert "0 task(s)" in out
    assert "By model" not in out


def test_pool_table_summarises_placement_and_keeps_the_quarantine_reason():
    """§6.2: one line per pool, plus the reason a project stopped growing."""
    out = _render("pool_status", {"pools": [
        {
            "profile_id": "worker-standard-medium-claude",
            "min_active": 2, "max_active": 8, "min_per_project": 1,
            "desired": 3, "running_idle": 2, "running_busy": 1,
            "starting": 0, "draining": 0, "ready": 4,
            "projects": [
                {"project_id": "web", "running_idle": 2, "running_busy": 1},
                {"project_id": "api", "running_idle": 1},
                {"project_id": "svc", "workspace_capacity": 0,
                 "quarantined_until": 4102444800.0,
                 "quarantined_reason": "startup death: harness exited 1"},
            ],
        },
    ]})
    # The project is no longer a column, and placement is summarised inline.
    assert "web:2i/1b" in out and "api:1i" in out and "svc:0" in out
    # A bare deadline is useless to an operator, so the reason travels with it.
    assert "quarantined until" in out
    assert "startup death: harness exited 1" in out


def test_pool_table_renders_a_reasonless_quarantine():
    """A key quarantined without a captured reason still reports its deadline."""
    out = _render("pool_status", {"pools": [
        {"profile_id": "coder", "projects": [
            {"project_id": "web", "quarantined_until": 4102444800.0},
        ]},
    ]})
    assert "quarantined until" in out
