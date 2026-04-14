"""Task summary notes — written to the vault on task completion.

Standalone module with no project-internal imports to avoid circular
dependencies between ``src.commands`` and ``src.orchestrator``.
"""

from __future__ import annotations

import datetime
import glob
import os
import re


def _slugify(text: str, max_length: int = 60) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "untitled"


def build_task_summary(
    task,
    result: dict | None,
    dependencies: set[str] | None = None,
    commits: list[tuple[str, str]] | None = None,
) -> str:
    """Build a markdown summary note for a completed task.

    Format mirrors the concise style used in vault task notes:
    compact metadata header, summary, files-changed list, and commits.

    Parameters
    ----------
    commits:
        List of ``(full_hash, subject)`` tuples from ``git log``.
    """
    lines: list[str] = []

    task_type = task.task_type.value if task.task_type else "unknown"
    lines.append(f"# Task: {task.id} — {task.title}")
    lines.append("")

    meta = (
        f"**Project:** {task.project_id} | **Type:** {task_type}"
        f" | **Status:** {task.status.value}"
    )
    lines.append(meta)

    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    date_parts = [f"**Date:** {date_str}"]
    if result and result.get("tokens_used"):
        date_parts.append(f"**Tokens:** {result['tokens_used']:,}")
    lines.append(" | ".join(date_parts))
    lines.append("")

    if task.branch_name:
        lines.append(f"**Branch:** `{task.branch_name}`")
    if task.pr_url:
        lines.append(f"**PR:** {task.pr_url}")
    if task.parent_task_id:
        lines.append(f"**Parent Task:** `{task.parent_task_id}`")
    if dependencies:
        dep_list = ", ".join(f"`{d}`" for d in sorted(dependencies))
        lines.append(f"**Dependencies:** {dep_list}")
    if task.branch_name or task.pr_url or task.parent_task_id or dependencies:
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    if result and result.get("summary"):
        lines.append(result["summary"])
    elif task.description:
        lines.append(task.description)
    else:
        lines.append("_No summary available._")
    lines.append("")

    lines.append("## Files Changed")
    lines.append("")
    if result and result.get("files_changed"):
        for fc in result["files_changed"]:
            lines.append(f"- `{fc}`")
    else:
        lines.append("No files changed.")
    lines.append("")

    if commits:
        lines.append("## Commits")
        lines.append("")
        for sha, subject in commits:
            lines.append(f"- `{sha[:10]}` {subject}")
        lines.append("")

    return "\n".join(lines)


def task_summary_path(vault_root: str, task) -> str:
    """Build the vault path for a task summary note.

    Returns ``{vault}/projects/{pid}/tasks/{category}/{datetime}_{title}({id}).md``.
    """
    now = datetime.datetime.now()
    category = task.task_type.value if task.task_type else "general"
    date_time = now.strftime("%Y-%m-%d_%H%M")
    slug = _slugify(task.title)
    filename = f"{date_time}_{slug}({task.id}).md"
    return os.path.join(
        vault_root, "projects", task.project_id, "tasks", category, filename
    )


def task_summary_exists(vault_root: str, task) -> str | None:
    """Check if a summary already exists for this task ID. Returns path or None."""
    pattern = os.path.join(
        vault_root, "projects", task.project_id, "tasks", "**", f"*({task.id}).md"
    )
    matches = glob.glob(pattern, recursive=True)
    return matches[0] if matches else None


def write_task_summary(
    vault_root: str,
    task,
    result: dict | None,
    dependencies: set[str] | None = None,
    commits: list[tuple[str, str]] | None = None,
) -> str | None:
    """Write a task summary to the vault. Returns path written, or None.

    Skips writing if a summary for this task ID already exists (prevents
    duplicates when both completion and archival paths fire).
    """
    if not task.project_id:
        return None

    existing = task_summary_exists(vault_root, task)
    if existing:
        return existing

    path = task_summary_path(vault_root, task)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    note = build_task_summary(task, result, dependencies, commits)
    with open(path, "w") as f:
        f.write(note)
    return path
