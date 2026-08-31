"""Plan file discovery and reading utilities.

Nothing discovers plan files for approval anymore: the completion-pipeline
plan flow (discovery → parse → ``AWAITING_PLAN_APPROVAL``) was deleted with
the LLM plan parser (llm-direct-path §6.3; see the supersession notice in
``docs/specs/plan-parser.md``).  ``approve_plan``/``reject_plan`` survive
only as remediation commands for pre-existing rows and do not call into
this module for discovery.

Current ownership (EVT-4): the sole production caller is the plan-cleanup
step of ``TaskCommandsMixin`` (``src/commands/task_commands.py``), which
uses ``find_all_plan_files`` to delete leftover plan files from project
workspaces after a plan is approved.  ``find_plan_file`` and
``read_plan_file`` are retained as manual/remediation utilities with no
in-tree production caller.
"""

from __future__ import annotations

import os


# The ONE canonical location where agents must write plan files.
# Only .claude/plan.md is checked — no glob expansion, no fallback scan.
# Rationale: detecting an invalid/stale plan is far worse than missing one.
DEFAULT_PLAN_FILE_PATTERNS = [
    ".claude/plan.md",
]


def find_plan_file(workspace: str, patterns: list[str] | None = None) -> str | None:
    """Search for a plan file in the workspace directory.

    Checks each candidate path in order.  Returns the first match, or
    ``None`` if no plan file is found.

    By design this is intentionally narrow — only ``.claude/plan.md`` by
    default.  Detecting a stale or unrelated plan is far worse than
    missing one, so there is no glob expansion and no deep-scan fallback.
    """
    candidates = patterns or DEFAULT_PLAN_FILE_PATTERNS
    for pattern in candidates:
        full_path = os.path.join(workspace, pattern)
        if os.path.isfile(full_path):
            return full_path
    return None


def find_all_plan_files(workspace: str) -> list[dict]:
    """Search a workspace for ALL plan files and return them with timestamps.

    Looks for:
    - ``.claude/plan.md`` (the canonical plan location)
    - ``plan.md`` (root-level plan)
    - Any ``.md`` files under ``.claude/plans/`` that are NOT archived plans

    Archived plans (format: ``{task-id}-plan.md`` or ``stale-{task-id}-plan.md``)
    are excluded because they accumulate as tasks complete and would cause
    the same plan to be re-discovered on every rescan.

    Returns a list of dicts with keys: ``path``, ``ctime`` (file creation
    time, falling back to mtime on Linux where birthtime isn't available).
    The list is sorted newest-first by ctime.
    """
    import glob as _glob

    found: list[dict] = []

    # Check canonical plan file locations
    for candidate in (".claude/plan.md", "plan.md"):
        full_path = os.path.join(workspace, candidate)
        if os.path.isfile(full_path):
            try:
                stat = os.stat(full_path)
                # Use birthtime if available (macOS), otherwise mtime
                ctime = getattr(stat, "st_birthtime", None) or stat.st_mtime
                found.append({"path": full_path, "ctime": ctime})
            except OSError:
                pass

    # Check .md files under .claude/plans/ — skip archived plans from
    # previous task runs.
    plans_dir = os.path.join(workspace, ".claude", "plans")
    if os.path.isdir(plans_dir):
        for md_path in _glob.glob(os.path.join(plans_dir, "*.md")):
            basename = os.path.basename(md_path)
            if basename.startswith("stale-") or basename.endswith("-plan.md"):
                continue
            if os.path.isfile(md_path):
                try:
                    stat = os.stat(md_path)
                    ctime = getattr(stat, "st_birthtime", None) or stat.st_mtime
                    found.append({"path": md_path, "ctime": ctime})
                except OSError:
                    pass

    # Sort newest first
    found.sort(key=lambda f: f["ctime"], reverse=True)
    return found


def read_plan_file(path: str) -> str:
    """Read the raw contents of a plan file."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
