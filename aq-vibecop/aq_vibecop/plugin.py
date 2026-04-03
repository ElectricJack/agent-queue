"""VibeCopPlugin — Plugin class with initialize/shutdown lifecycle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.plugins.base import Plugin, PluginPermission, cron

from aq_vibecop.runner import VibeCopRunner
from aq_vibecop.formatter import format_findings

if TYPE_CHECKING:
    from src.plugins.base import PluginContext

logger = logging.getLogger(__name__)


class VibeCopPlugin(Plugin):
    """Vibecop static analysis plugin for Agent Queue.

    Wraps the vibecop CLI to expose code scanning tools that agents can use
    to self-check their changes. Vibecop is a deterministic linter — no LLM
    tokens are consumed for analysis.
    """

    plugin_permissions = [PluginPermission.SHELL]

    config_schema = {
        "node_path": {
            "type": "string",
            "description": "Path to Node.js binary (defaults to system node)",
        },
        "vibecop_path": {
            "type": "string",
            "description": "Path to vibecop binary (falls back to npx/global)",
        },
        "default_severity": {
            "type": "string",
            "description": "Default severity threshold: error, warning, or info",
            "default": "warning",
        },
        "auto_install": {
            "type": "boolean",
            "description": "Auto-install vibecop via npm if not found",
            "default": False,
        },
        "scan_timeout": {
            "type": "integer",
            "description": "Timeout in seconds for vibecop commands",
            "default": 60,
        },
        "auto_scan_on_complete": {
            "type": "boolean",
            "description": (
                "Automatically scan task workspace when a task completes. "
                "Uses diff against the task's base branch to check only changed files."
            ),
            "default": True,
        },
        "weekly_scan_schedule": {
            "type": "string",
            "description": (
                "Cron expression for weekly full project scan. "
                "Default: Monday 6 AM (0 6 * * 1)."
            ),
            "default": "0 6 * * 1",
        },
    }

    default_config = {
        "default_severity": "warning",
        "auto_install": False,
        "scan_timeout": 60,
        "auto_scan_on_complete": True,
        "weekly_scan_schedule": "0 6 * * 1",
    }

    def __init__(self) -> None:
        self._runner: VibeCopRunner | None = None
        self._ctx: PluginContext | None = None

    async def initialize(self, ctx: PluginContext) -> None:
        """Register vibecop tools and commands."""
        self._ctx = ctx

        config = ctx.get_config()
        merged = {**self.default_config, **config}

        self._runner = VibeCopRunner(
            vibecop_path=merged.get("vibecop_path"),
            node_path=merged.get("node_path"),
            timeout=merged.get("scan_timeout", 60),
        )

        # --- Tool: vibecop_scan ---
        ctx.register_tool({
            "name": "vibecop_scan",
            "description": (
                "Run vibecop static analysis on a directory. Detects AI-generated "
                "code antipatterns (god-functions, SQL injection, dead code, etc.) "
                "using deterministic AST-based analysis. Use this to scan a workspace "
                "or project directory for quality issues."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Directory path to scan. Defaults to current working directory."
                        ),
                    },
                    "diff_ref": {
                        "type": "string",
                        "description": (
                            "Git ref to diff against (e.g. 'main', 'HEAD~3'). "
                            "When set, only scans files changed since that ref."
                        ),
                    },
                    "max_findings": {
                        "type": "integer",
                        "description": "Maximum number of findings to return.",
                        "default": 50,
                    },
                    "severity_threshold": {
                        "type": "string",
                        "enum": ["error", "warning", "info"],
                        "description": (
                            "Minimum severity to include in results. "
                            "Findings below this level are excluded."
                        ),
                        "default": "warning",
                    },
                },
                "required": [],
            },
        })
        ctx.register_command("vibecop_scan", self._handle_scan)

        # --- Tool: vibecop_check ---
        ctx.register_tool({
            "name": "vibecop_check",
            "description": (
                "Run vibecop on specific files. Use this after modifying files to "
                "check only the files you changed, rather than scanning the entire "
                "project. Faster than a full scan for targeted quality checks."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to check.",
                    },
                    "max_findings": {
                        "type": "integer",
                        "description": "Maximum number of findings to return.",
                        "default": 50,
                    },
                },
                "required": ["files"],
            },
        })
        ctx.register_command("vibecop_check", self._handle_check)

        # --- Tool: vibecop_status ---
        ctx.register_tool({
            "name": "vibecop_status",
            "description": (
                "Check vibecop installation status. Reports installed version, "
                "available detectors, configuration path, and Node.js version. "
                "Use this to verify vibecop is available before scanning."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        })
        ctx.register_command("vibecop_status", self._handle_status)

        # --- Event types ---
        ctx.register_event_type("vibecop.scan_completed")
        ctx.register_event_type("vibecop.findings_detected")

        # --- Event subscriptions ---
        ctx.subscribe("task.completed", self._on_task_completed)

        logger.info("VibeCopPlugin initialized with config: %s", merged)

    async def shutdown(self, ctx: PluginContext) -> None:
        """Clean up resources."""
        self._runner = None
        self._ctx = None
        logger.info("VibeCopPlugin shut down")

    async def on_config_changed(self, ctx: PluginContext, config: dict) -> None:
        """Rebuild runner when config changes."""
        merged = {**self.default_config, **config}
        self._runner = VibeCopRunner(
            vibecop_path=merged.get("vibecop_path"),
            node_path=merged.get("node_path"),
            timeout=merged.get("scan_timeout", 60),
        )

    # ----- Command handlers -----

    async def _handle_scan(self, args: dict) -> dict:
        """Handle vibecop_scan tool invocation."""
        assert self._runner is not None

        path = args.get("path", ".")
        diff_ref = args.get("diff_ref")
        max_findings = args.get("max_findings", 50)
        severity_threshold = args.get("severity_threshold", "warning")

        result = await self._runner.scan(path=path, diff_ref=diff_ref)

        if not result["success"]:
            return result

        findings = result.get("findings", [])
        filtered = _filter_by_severity(findings, severity_threshold)
        truncated = filtered[:max_findings]

        summary = format_findings(truncated, mode="detailed")
        return {
            "success": True,
            "summary": summary,
            "findings": truncated,
            "total_findings": len(filtered),
            "shown": len(truncated),
            "files_scanned": result.get("files_scanned", 0),
            "errors": result.get("errors", []),
        }

    async def _handle_check(self, args: dict) -> dict:
        """Handle vibecop_check tool invocation."""
        assert self._runner is not None

        files = args.get("files", [])
        if not files:
            return {"success": False, "error": "No files provided. Pass a 'files' array."}

        max_findings = args.get("max_findings", 50)

        result = await self._runner.check(files=files)

        if not result["success"]:
            return result

        findings = result.get("findings", [])
        truncated = findings[:max_findings]

        summary = format_findings(truncated, mode="detailed")
        return {
            "success": True,
            "summary": summary,
            "findings": truncated,
            "total_findings": len(findings),
            "shown": len(truncated),
            "files_scanned": result.get("files_scanned", 0),
            "errors": result.get("errors", []),
        }

    async def _handle_status(self, args: dict) -> dict:
        """Handle vibecop_status tool invocation."""
        assert self._runner is not None
        return await self._runner.status()

    # ----- Event handlers -----

    async def _on_task_completed(self, event_data: dict) -> None:
        """Scan a task's workspace when the task completes.

        Triggered by ``task.completed`` events. When ``auto_scan_on_complete``
        is enabled, runs a diff-aware vibecop scan against the project's
        default branch and posts a findings summary to Discord.
        """
        if self._ctx is None or self._runner is None:
            return

        config = self._ctx.get_config()
        merged = {**self.default_config, **config}
        if not merged.get("auto_scan_on_complete", True):
            return

        task_id = event_data.get("task_id", "")
        project_id = event_data.get("project_id", "")
        if not task_id:
            return

        try:
            # Look up workspace path by finding which workspace is locked by this task
            ws_result = await self._ctx.execute_command(
                "list_workspaces", {"project_id": project_id}
            )
            workspace_path: str | None = None
            for ws in ws_result.get("workspaces", []):
                if ws.get("locked_by_task_id") == task_id:
                    workspace_path = ws.get("workspace_path")
                    break

            if not workspace_path:
                logger.debug(
                    "vibecop auto-scan: no locked workspace found for task %s, skipping",
                    task_id,
                )
                return

            # Get project info for name and default branch
            projects_result = await self._ctx.execute_command("list_projects", {})
            project_name = project_id
            for proj in projects_result.get("projects", []):
                if proj.get("id") == project_id:
                    project_name = proj.get("name", project_id)
                    break

            # Diff against the default branch to check only the agent's changes
            diff_ref = "main"

            logger.info(
                "vibecop auto-scan: scanning workspace %s for task %s (diff_ref=%s)",
                workspace_path, task_id, diff_ref,
            )

            result = await self._runner.scan(path=workspace_path, diff_ref=diff_ref)

            findings = result.get("findings", [])
            severity_threshold = merged.get("default_severity", "warning")
            filtered = _filter_by_severity(findings, severity_threshold)

            # Emit vibecop.scan_completed for other subscribers
            await self._ctx.emit_event("vibecop.scan_completed", {
                "task_id": task_id,
                "project_id": project_id,
                "workspace_path": workspace_path,
                "total_findings": len(filtered),
                "files_scanned": result.get("files_scanned", 0),
                "success": result.get("success", False),
            })

            # Emit vibecop.findings_detected if there are findings
            if filtered:
                await self._ctx.emit_event("vibecop.findings_detected", {
                    "task_id": task_id,
                    "project_id": project_id,
                    "workspace_path": workspace_path,
                    "findings_count": len(filtered),
                    "error_count": sum(
                        1 for f in filtered if f.get("severity") == "error"
                    ),
                    "warning_count": sum(
                        1 for f in filtered if f.get("severity") == "warning"
                    ),
                    "info_count": sum(
                        1 for f in filtered if f.get("severity") == "info"
                    ),
                })

            # Post Discord notification with findings summary
            notification = _format_discord_notification(
                project_name=project_name,
                task_id=task_id,
                findings=filtered,
                workspace_path=workspace_path,
                files_scanned=result.get("files_scanned", 0),
            )
            await self._ctx.notify(notification, project_id=project_id)

        except Exception:
            logger.exception(
                "vibecop auto-scan failed for task %s", task_id
            )

    # ----- Cron jobs -----

    @cron("0 6 * * 1", config_key="weekly_scan_schedule")
    async def weekly_project_scan(self, ctx: PluginContext) -> None:
        """Weekly full scan of all active project workspaces.

        Runs every Monday at 6 AM by default (configurable via
        ``weekly_scan_schedule``). Scans each active project's workspace
        and posts a per-project summary to Discord.
        """
        if self._runner is None:
            return

        config = ctx.get_config()
        merged = {**self.default_config, **config}
        severity_threshold = merged.get("default_severity", "warning")

        try:
            projects_result = await ctx.execute_command("list_projects", {})
        except Exception:
            logger.exception("vibecop weekly scan: failed to list projects")
            return

        projects = projects_result.get("projects", [])
        active_projects = [p for p in projects if p.get("status") == "ACTIVE"]

        if not active_projects:
            logger.info("vibecop weekly scan: no active projects found")
            return

        for project in active_projects:
            project_id = project.get("id", "")
            project_name = project.get("name", project_id)
            workspace_path = project.get("workspace")

            if not workspace_path:
                logger.debug(
                    "vibecop weekly scan: no workspace for project %s, skipping",
                    project_id,
                )
                continue

            try:
                logger.info(
                    "vibecop weekly scan: scanning project %s at %s",
                    project_name, workspace_path,
                )
                result = await self._runner.scan(path=workspace_path)

                findings = result.get("findings", [])
                filtered = _filter_by_severity(findings, severity_threshold)

                # Emit scan_completed event
                await ctx.emit_event("vibecop.scan_completed", {
                    "project_id": project_id,
                    "workspace_path": workspace_path,
                    "total_findings": len(filtered),
                    "files_scanned": result.get("files_scanned", 0),
                    "success": result.get("success", False),
                    "scan_type": "weekly",
                })

                # Emit findings_detected if any
                if filtered:
                    error_count = sum(
                        1 for f in filtered if f.get("severity") == "error"
                    )
                    await ctx.emit_event("vibecop.findings_detected", {
                        "project_id": project_id,
                        "workspace_path": workspace_path,
                        "findings_count": len(filtered),
                        "error_count": error_count,
                        "warning_count": sum(
                            1 for f in filtered if f.get("severity") == "warning"
                        ),
                        "info_count": sum(
                            1 for f in filtered if f.get("severity") == "info"
                        ),
                        "scan_type": "weekly",
                    })

                    # Optionally create tasks for error-severity findings
                    if error_count > 0:
                        try:
                            summary = format_findings(
                                [f for f in filtered if f.get("severity") == "error"],
                                mode="summary",
                            )
                            await ctx.execute_command("create_task", {
                                "project_id": project_id,
                                "title": (
                                    f"Fix {error_count} vibecop error(s) "
                                    f"in {project_name}"
                                ),
                                "description": (
                                    f"Vibecop weekly scan found {error_count} "
                                    f"error-severity finding(s) in project "
                                    f"{project_name}.\n\n"
                                    f"Workspace: {workspace_path}\n\n"
                                    f"Findings:\n{summary}\n\n"
                                    f"Run `vibecop_scan` on the workspace to see "
                                    f"full details, then fix all error-severity "
                                    f"findings."
                                ),
                                "priority": 5,
                            })
                        except Exception:
                            logger.exception(
                                "vibecop weekly scan: failed to create task "
                                "for errors in project %s", project_id,
                            )

                # Post notification
                notification = _format_discord_notification(
                    project_name=project_name,
                    task_id=None,
                    findings=filtered,
                    workspace_path=workspace_path,
                    files_scanned=result.get("files_scanned", 0),
                    scan_type="weekly",
                )
                await ctx.notify(notification, project_id=project_id)

            except Exception:
                logger.exception(
                    "vibecop weekly scan: failed scanning project %s",
                    project_id,
                )


# ----- Helpers -----

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _filter_by_severity(
    findings: list[dict],
    threshold: str,
) -> list[dict]:
    """Filter findings to those at or above the severity threshold."""
    cutoff = _SEVERITY_ORDER.get(threshold, 1)
    return [
        f for f in findings
        if _SEVERITY_ORDER.get(f.get("severity", "info"), 2) <= cutoff
    ]


def _format_discord_notification(
    *,
    project_name: str,
    task_id: str | None,
    findings: list[dict],
    workspace_path: str,
    files_scanned: int = 0,
    scan_type: str = "auto",
) -> str:
    """Format a vibecop scan result as a Discord notification message.

    Args:
        project_name: Human-readable project name.
        task_id: Task ID that triggered the scan (None for scheduled scans).
        findings: Filtered findings list.
        workspace_path: Path to the scanned workspace.
        files_scanned: Number of files scanned.
        scan_type: "auto" for task-completion scans, "weekly" for scheduled.

    Returns:
        Formatted notification string for Discord.
    """
    error_count = sum(1 for f in findings if f.get("severity") == "error")
    warning_count = sum(1 for f in findings if f.get("severity") == "warning")
    info_count = sum(1 for f in findings if f.get("severity") == "info")

    # Header
    if scan_type == "weekly":
        header = f"📊 **Vibecop Weekly Scan — {project_name}**"
    elif task_id:
        header = f"🔍 **Vibecop Scan — {project_name}** (task `{task_id}`)"
    else:
        header = f"🔍 **Vibecop Scan — {project_name}**"

    # Counts line
    if not findings:
        return f"{header}\n✅ No findings detected ({files_scanned} files scanned)"

    parts = [header]

    counts = []
    if error_count:
        counts.append(f"🔴 {error_count} error(s)")
    if warning_count:
        counts.append(f"🟡 {warning_count} warning(s)")
    if info_count:
        counts.append(f"🔵 {info_count} info")
    parts.append(" | ".join(counts) + f" ({files_scanned} files scanned)")

    # Top findings (up to 5)
    top_findings = findings[:5]
    if top_findings:
        parts.append("")
        parts.append("**Top findings:**")
        for f in top_findings:
            sev = f.get("severity", "info")
            icon = {"error": "🔴", "warning": "🟡", "info": "🔵"}.get(sev, "⚪")
            file_loc = f.get("file", "?")
            line = f.get("line", 0)
            if line:
                file_loc = f"{file_loc}:{line}"
            detector = f.get("detector", "")
            message = f.get("message", "")
            if len(message) > 60:
                message = message[:57] + "..."
            parts.append(f"{icon} `{file_loc}` [{detector}] {message}")

    remaining = len(findings) - len(top_findings)
    if remaining > 0:
        parts.append(f"... and {remaining} more")

    # Workspace link
    parts.append(f"\n📁 Workspace: `{workspace_path}`")

    return "\n".join(parts)
