"""Scope-check tests for the global-admin path (elevated + project_id=None).

See dashboard-shell-v2 plan §Task 1.
"""

from __future__ import annotations

from src.api.auth import RequestScope
from src.api.scope import check_command_scope


def test_global_admin_scope_allows_any_command() -> None:
    scope = RequestScope(
        kind="session",
        session_id="supervisor-global",
        task_id=None,
        project_id=None,
        elevated=True,
    )
    for cmd in ("create_project", "task_close", "playbook_v2_validate", "delete_task"):
        assert check_command_scope(cmd, {}, scope) is None


def test_global_admin_scope_skips_project_id_match() -> None:
    scope = RequestScope(
        kind="session",
        session_id="supervisor-global",
        task_id=None,
        project_id=None,
        elevated=True,
    )
    args = {"project_id": "any-project"}
    assert check_command_scope("task_create", args, scope) is None
    # Args passed through untouched — no injection.
    assert args == {"project_id": "any-project"}


def test_per_project_elevated_still_enforces_project_match() -> None:
    scope = RequestScope(
        kind="session",
        session_id="s1",
        task_id=None,
        project_id="demo",
        elevated=True,
    )
    assert check_command_scope("task_create", {"project_id": "demo"}, scope) is None
    r = check_command_scope("task_create", {"project_id": "other"}, scope)
    assert r is not None and "project_id mismatch" in r


def test_non_elevated_with_null_project_still_narrow() -> None:
    scope = RequestScope(
        kind="session",
        session_id="s1",
        task_id="t1",
        project_id=None,
        elevated=False,
    )
    r = check_command_scope("create_project", {}, scope)
    assert r is not None and "out of scope" in r
