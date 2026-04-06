"""Internal plugin: vibecop static analysis (scan, check, status).

Migrated from the external ``aq-vibecop`` plugin.  Wraps the vibecop CLI
to expose code scanning tools that agents use to self-check their changes.
Vibecop is a deterministic linter -- no LLM tokens are consumed for analysis.

Detects 22+ antipatterns across quality, security, correctness, and testing
categories for JavaScript, TypeScript, TSX, and Python codebases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from src.plugins.base import InternalPlugin, PluginPermission, cron

if TYPE_CHECKING:
    from src.plugins.base import PluginContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module constants for internal-plugin discovery
# ---------------------------------------------------------------------------

TOOL_CATEGORY = "vibecop"

TOOL_DEFINITIONS = [
    {
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
    },
    {
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
    },
    {
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
    },
]


# ---------------------------------------------------------------------------
# Severity constants
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

_SEVERITY_META = {
    "error": {"order": 0, "icon": "[ERROR]", "label": "Errors"},
    "warning": {"order": 1, "icon": "[WARN]", "label": "Warnings"},
    "info": {"order": 2, "icon": "[INFO]", "label": "Info"},
}


# ---------------------------------------------------------------------------
# VibeCopRunner — async subprocess wrapper for the vibecop CLI
# ---------------------------------------------------------------------------

_INSTALL_INSTRUCTIONS = (
    "vibecop is not installed or not found on PATH.\n\n"
    "To install:\n"
    "  npm install -g vibecop\n\n"
    "Or use npx (no install needed):\n"
    "  npx vibecop scan <path>\n\n"
    "Requirements: Node.js >= 20\n\n"
    "You can also configure the plugin with the path to vibecop:\n"
    "  aq plugin config aq-vibecop vibecop_path=/path/to/vibecop"
)


class VibeCopRunner:
    """Async wrapper around the vibecop CLI.

    Resolves the vibecop binary using a fallback chain:
    1. Explicitly configured ``vibecop_path``
    2. ``npx vibecop`` (uses local or cached npm package)
    3. Global ``vibecop`` on PATH

    All commands use ``--format json`` for structured output parsing.
    """

    def __init__(
        self,
        *,
        vibecop_path: str | None = None,
        node_path: str | None = None,
        timeout: int = 60,
    ) -> None:
        self._vibecop_path = vibecop_path
        self._node_path = node_path
        self._timeout = timeout

    def _resolve_vibecop_cmd(self) -> list[str] | None:
        """Resolve the vibecop command using the fallback chain."""
        # 1. Configured path
        if self._vibecop_path:
            path = Path(self._vibecop_path)
            if path.exists():
                return [str(path)]
            if shutil.which(self._vibecop_path):
                return [self._vibecop_path]

        # 2. npx vibecop
        npx = shutil.which("npx")
        if npx:
            return [npx, "vibecop"]

        # 3. Global vibecop
        global_vibecop = shutil.which("vibecop")
        if global_vibecop:
            return [global_vibecop]

        return None

    def _resolve_node_cmd(self) -> str | None:
        """Resolve the Node.js binary path."""
        if self._node_path:
            if Path(self._node_path).exists() or shutil.which(self._node_path):
                return self._node_path
        return shutil.which("node")

    async def _run(self, cmd: list[str], cwd: str | None = None) -> dict:
        """Execute a vibecop command and parse JSON output."""
        logger.debug("Running vibecop command: %s (cwd=%s)", cmd, cwd)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "findings": [],
                "files_scanned": 0,
                "errors": [f"Command timed out after {self._timeout}s: {' '.join(cmd)}"],
            }
        except FileNotFoundError:
            return {
                "success": False,
                "findings": [],
                "files_scanned": 0,
                "errors": [f"Command not found: {cmd[0]}", _INSTALL_INSTRUCTIONS],
            }
        except OSError as exc:
            return {
                "success": False,
                "findings": [],
                "files_scanned": 0,
                "errors": [f"Failed to execute command: {exc}"],
            }

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if stderr_text:
            logger.debug("vibecop stderr: %s", stderr_text)

        if not stdout_text:
            if proc.returncode != 0:
                return {
                    "success": False,
                    "findings": [],
                    "files_scanned": 0,
                    "errors": [
                        f"vibecop exited with code {proc.returncode}",
                        stderr_text or "No output produced",
                    ],
                }
            return {
                "success": True,
                "findings": [],
                "files_scanned": 0,
                "errors": [],
            }

        try:
            data = json.loads(stdout_text)
        except json.JSONDecodeError:
            return {
                "success": False,
                "findings": [],
                "files_scanned": 0,
                "errors": [
                    "Failed to parse vibecop JSON output",
                    f"Raw output: {stdout_text[:500]}",
                ],
            }

        return _normalize_output(data)

    async def scan(self, *, path: str = ".", diff_ref: str | None = None) -> dict:
        """Run ``vibecop scan`` on a directory."""
        base_cmd = self._resolve_vibecop_cmd()
        if base_cmd is None:
            return {
                "success": False,
                "findings": [],
                "files_scanned": 0,
                "errors": [_INSTALL_INSTRUCTIONS],
            }

        cmd = [*base_cmd, "scan", path, "--format", "json"]
        if diff_ref:
            cmd.extend(["--diff", diff_ref])

        return await self._run(cmd, cwd=path if path != "." else None)

    async def check(self, *, files: list[str]) -> dict:
        """Run ``vibecop check`` on specific files."""
        base_cmd = self._resolve_vibecop_cmd()
        if base_cmd is None:
            return {
                "success": False,
                "findings": [],
                "files_scanned": 0,
                "errors": [_INSTALL_INSTRUCTIONS],
            }

        cmd = [*base_cmd, "check", *files, "--format", "json"]
        return await self._run(cmd)

    async def status(self) -> dict:
        """Check vibecop installation status."""
        result: dict = {
            "success": True,
            "installed": False,
            "version": None,
            "node_version": None,
            "detectors": [],
            "config_path": None,
            "errors": [],
        }

        # Check Node.js
        node = self._resolve_node_cmd()
        if node:
            try:
                proc = await asyncio.create_subprocess_exec(
                    node,
                    "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                result["node_version"] = stdout.decode().strip()
            except (asyncio.TimeoutError, OSError):
                result["errors"].append("Failed to check Node.js version")
        else:
            result["errors"].append(
                "Node.js not found. vibecop requires Node.js >= 20."
            )

        # Check vibecop
        base_cmd = self._resolve_vibecop_cmd()
        if base_cmd is None:
            result["errors"].append(_INSTALL_INSTRUCTIONS)
            return result

        # Get version
        try:
            proc = await asyncio.create_subprocess_exec(
                *base_cmd,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version_text = stdout.decode().strip()
            if version_text:
                result["installed"] = True
                result["version"] = version_text
        except (asyncio.TimeoutError, OSError) as exc:
            result["errors"].append(f"Failed to check vibecop version: {exc}")
            return result

        # Get available detectors
        try:
            proc = await asyncio.create_subprocess_exec(
                *base_cmd,
                "list-detectors",
                "--format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            stdout_text = stdout.decode().strip()
            if stdout_text and proc.returncode == 0:
                try:
                    detectors = json.loads(stdout_text)
                    if isinstance(detectors, list):
                        result["detectors"] = detectors
                    elif isinstance(detectors, dict) and "detectors" in detectors:
                        result["detectors"] = detectors["detectors"]
                except json.JSONDecodeError:
                    pass
        except (asyncio.TimeoutError, OSError):
            pass

        return result


# ---------------------------------------------------------------------------
# Output normalization
# ---------------------------------------------------------------------------


def _normalize_output(data: dict | list) -> dict:
    """Normalize vibecop JSON output into a consistent schema."""
    if isinstance(data, list):
        return {
            "success": True,
            "findings": [_normalize_finding(f) for f in data],
            "files_scanned": len({f.get("file", f.get("filePath", "")) for f in data}),
            "errors": [],
        }

    findings_raw = data.get("findings", data.get("results", data.get("violations", [])))
    findings = [_normalize_finding(f) for f in findings_raw]

    errors = data.get("errors", [])
    if isinstance(errors, str):
        errors = [errors]

    files_scanned = data.get(
        "files_scanned", data.get("filesScanned", data.get("totalFiles", 0))
    )

    return {
        "success": True,
        "findings": findings,
        "files_scanned": files_scanned,
        "errors": errors,
    }


def _normalize_finding(finding: dict) -> dict:
    """Normalize a single finding into a consistent schema."""
    return {
        "file": finding.get("file", finding.get("filePath", finding.get("path", ""))),
        "line": finding.get("line", finding.get("lineNumber", finding.get("startLine", 0))),
        "column": finding.get("column", finding.get("startColumn", 0)),
        "severity": finding.get("severity", "warning"),
        "detector": finding.get(
            "detector", finding.get("ruleId", finding.get("rule", ""))
        ),
        "category": finding.get("category", ""),
        "message": finding.get("message", finding.get("description", "")),
        "suggestion": finding.get("suggestion", finding.get("fix", "")),
    }


# ---------------------------------------------------------------------------
# Findings formatter — converts JSON findings to agent-friendly text
# ---------------------------------------------------------------------------

_MAX_DETAIL_CHARS = 8000
_MAX_SUMMARY_CHARS = 2000


def format_findings(findings: list[dict], *, mode: str = "detailed") -> str:
    """Format findings into a human/agent-readable string.

    Args:
        findings: List of normalized finding dicts from runner.
        mode: ``"detailed"`` for full output, ``"summary"`` for compact.
    """
    if not findings:
        return "No findings detected. Code looks clean."

    if mode == "summary":
        return _format_summary(findings)
    return _format_detailed(findings)


def _format_detailed(findings: list[dict]) -> str:
    """Full output with per-finding details, grouped by severity."""
    grouped = _group_by_severity(findings)
    parts: list[str] = []
    char_count = 0

    counts = _severity_counts(findings)
    header = _counts_header(counts)
    parts.append(header)
    char_count += len(header)

    for severity in ("error", "warning", "info"):
        group = grouped.get(severity, [])
        if not group:
            continue

        meta = _SEVERITY_META.get(severity, _SEVERITY_META["info"])
        section_header = f"\n--- {meta['label']} ({len(group)}) ---\n"
        parts.append(section_header)
        char_count += len(section_header)

        for finding in group:
            entry = _format_finding_entry(finding, meta["icon"])
            if char_count + len(entry) > _MAX_DETAIL_CHARS:
                remaining = sum(len(grouped.get(s, [])) for s in ("error", "warning", "info"))
                parts.append(
                    f"\n... truncated ({remaining - len(parts) + 2} more findings). "
                    "Re-run with higher max_findings to see all."
                )
                return "\n".join(parts)
            parts.append(entry)
            char_count += len(entry)

    return "\n".join(parts)


def _format_summary(findings: list[dict]) -> str:
    """Compact summary: counts + top findings only."""
    counts = _severity_counts(findings)
    parts: list[str] = [_counts_header(counts)]
    char_count = len(parts[0])

    for severity in ("error", "warning"):
        relevant = [f for f in findings if f.get("severity") == severity]
        if not relevant:
            continue

        meta = _SEVERITY_META[severity]
        for finding in relevant[:5]:
            line = _format_finding_oneline(finding, meta["icon"])
            if char_count + len(line) > _MAX_SUMMARY_CHARS:
                parts.append("... (truncated)")
                return "\n".join(parts)
            parts.append(line)
            char_count += len(line)

    return "\n".join(parts)


def _format_finding_entry(finding: dict, icon: str) -> str:
    """Format a single finding with full details."""
    file_loc = finding.get("file", "unknown")
    line = finding.get("line", 0)
    if line:
        file_loc = f"{file_loc}:{line}"
    col = finding.get("column", 0)
    if col:
        file_loc = f"{file_loc}:{col}"

    detector = finding.get("detector", "unknown")
    message = finding.get("message", "")
    suggestion = finding.get("suggestion", "")

    lines = [f"{icon} {file_loc} [{detector}]"]
    if message:
        lines.append(f"  {message}")
    if suggestion:
        lines.append(f"  Fix: {suggestion}")

    return "\n".join(lines)


def _format_finding_oneline(finding: dict, icon: str) -> str:
    """Format a single finding as a compact one-liner."""
    file_loc = finding.get("file", "unknown")
    line = finding.get("line", 0)
    if line:
        file_loc = f"{file_loc}:{line}"

    detector = finding.get("detector", "")
    message = finding.get("message", "")

    if len(message) > 80:
        message = message[:77] + "..."

    return f"{icon} {file_loc} [{detector}] {message}"


def _group_by_severity(findings: list[dict]) -> dict[str, list[dict]]:
    """Group findings by severity level."""
    grouped: dict[str, list[dict]] = {}
    for finding in findings:
        severity = finding.get("severity", "info")
        grouped.setdefault(severity, []).append(finding)
    return grouped


def _severity_counts(findings: list[dict]) -> dict[str, int]:
    """Count findings by severity."""
    counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for finding in findings:
        severity = finding.get("severity", "info")
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _counts_header(counts: dict[str, int]) -> str:
    """Build a summary header line from severity counts."""
    total = sum(counts.values())
    parts = [f"Vibecop: {total} finding(s)"]
    for severity in ("error", "warning", "info"):
        count = counts.get(severity, 0)
        if count:
            parts.append(f"{count} {severity}(s)")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Severity filter helper
# ---------------------------------------------------------------------------


def _filter_by_severity(findings: list[dict], threshold: str) -> list[dict]:
    """Filter findings to those at or above the severity threshold."""
    cutoff = _SEVERITY_ORDER.get(threshold, 1)
    return [
        f
        for f in findings
        if _SEVERITY_ORDER.get(f.get("severity", "info"), 2) <= cutoff
    ]


# ---------------------------------------------------------------------------
# Discord notification formatter
# ---------------------------------------------------------------------------


def _format_discord_notification(
    *,
    project_name: str,
    task_id: str | None,
    findings: list[dict],
    workspace_path: str,
    files_scanned: int = 0,
    scan_type: str = "auto",
) -> str:
    """Format a vibecop scan result as a Discord notification message."""
    error_count = sum(1 for f in findings if f.get("severity") == "error")
    warning_count = sum(1 for f in findings if f.get("severity") == "warning")
    info_count = sum(1 for f in findings if f.get("severity") == "info")

    # Header
    if scan_type == "weekly":
        header = f"**Vibecop Weekly Scan -- {project_name}**"
    elif task_id:
        header = f"**Vibecop Scan -- {project_name}** (task `{task_id}`)"
    else:
        header = f"**Vibecop Scan -- {project_name}**"

    if not findings:
        return f"{header}\nNo findings detected ({files_scanned} files scanned)"

    parts = [header]

    counts = []
    if error_count:
        counts.append(f"{error_count} error(s)")
    if warning_count:
        counts.append(f"{warning_count} warning(s)")
    if info_count:
        counts.append(f"{info_count} info")
    parts.append(" | ".join(counts) + f" ({files_scanned} files scanned)")

    # Top findings (up to 5)
    top_findings = findings[:5]
    if top_findings:
        parts.append("")
        parts.append("**Top findings:**")
        for f in top_findings:
            sev = f.get("severity", "info")
            icon = {"error": "[ERR]", "warning": "[WARN]", "info": "[INFO]"}.get(sev, "[?]")
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

    parts.append(f"\nWorkspace: `{workspace_path}`")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI formatters
# ---------------------------------------------------------------------------


def _fmt_vibecop_scan(data: dict):
    """Rich table for vibecop scan/check results."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    findings = data.get("findings", [])
    summary = data.get("summary", "")
    total = data.get("total_findings", 0)
    shown = data.get("shown", 0)
    files = data.get("files_scanned", 0)

    header = Text()
    header.append("Vibecop Scan Results\n", style="bold bright_white")
    header.append(f"Files scanned: {files}", style="dim")
    header.append(f"  |  Findings: {total}", style="dim")
    if shown < total:
        header.append(f" (showing {shown})", style="dim")

    if not findings:
        body = Text("No findings detected. Code looks clean.", style="green")
    else:
        body = Text(summary)

    return Panel(
        Group(header, Text(""), body),
        border_style="bright_cyan",
        padding=(1, 2),
    )


def _fmt_vibecop_status(data: dict):
    """Rich display for vibecop status."""
    from rich.text import Text

    installed = data.get("installed", False)
    version = data.get("version", "unknown")
    node_version = data.get("node_version", "unknown")
    detectors = data.get("detectors", [])
    errors = data.get("errors", [])

    text = Text()
    if installed:
        text.append("Vibecop: ", style="bold")
        text.append(f"v{version}", style="green")
    else:
        text.append("Vibecop: ", style="bold")
        text.append("not installed", style="red")

    text.append(f"\nNode.js: {node_version}", style="dim")

    if detectors:
        text.append(f"\nDetectors: {len(detectors)} available", style="dim")

    for err in errors:
        text.append(f"\n{err}", style="yellow")

    return text


def _build_cli_formatters():
    """Return CLI formatter specs for vibecop commands."""
    from src.cli.formatter_registry import FormatterSpec

    return {
        "vibecop_scan": FormatterSpec(render=_fmt_vibecop_scan, extract=None, many=False),
        "vibecop_check": FormatterSpec(render=_fmt_vibecop_scan, extract=None, many=False),
        "vibecop_status": FormatterSpec(render=_fmt_vibecop_status, extract=None, many=False),
    }


CLI_FORMATTERS = _build_cli_formatters


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "default_severity": "warning",
    "auto_install": False,
    "scan_timeout": 60,
    "enforce_vibecop_checkout": True,
    "auto_scan_on_complete": True,
    "weekly_scan_schedule": "0 6 * * 1",
}

# Rule ID used for the pre-completion vibecop check rule
_VIBECOP_RULE_ID = "rule-vibecop-pre-complete-check"


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class VibeCopPlugin(InternalPlugin):
    """Vibecop static analysis: scan, check, status.

    Wraps the vibecop CLI to expose code scanning tools that agents can use
    to self-check their changes.  Vibecop is a deterministic linter -- no LLM
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
        "enforce_vibecop_checkout": {
            "type": "boolean",
            "description": (
                "Inject a rule requiring agents to run vibecop before task completion. "
                "Applies to code-related task types (feature, bugfix, refactor)."
            ),
            "default": True,
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

    default_config = dict(_DEFAULT_CONFIG)

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._db = ctx.get_service("db")

        config = ctx.get_config()
        merged = {**_DEFAULT_CONFIG, **config}

        self._runner = VibeCopRunner(
            vibecop_path=merged.get("vibecop_path"),
            node_path=merged.get("node_path"),
            timeout=merged.get("scan_timeout", 60),
        )

        # Register commands
        ctx.register_command("vibecop_scan", self.cmd_vibecop_scan)
        ctx.register_command("vibecop_check", self.cmd_vibecop_check)
        ctx.register_command("vibecop_status", self.cmd_vibecop_status)

        # Register tools
        for tool_def in TOOL_DEFINITIONS:
            ctx.register_tool(dict(tool_def), category="vibecop")

        # Rule injection: remind agents to run vibecop before task completion
        if merged.get("enforce_vibecop_checkout", True):
            await self._inject_pre_complete_rule(ctx)

        # Event types
        ctx.register_event_type("vibecop.scan_completed")
        ctx.register_event_type("vibecop.findings_detected")

        # Event subscriptions
        ctx.subscribe("task.completed", self._on_task_completed)

        logger.info("VibeCopPlugin initialized with config: %s", merged)

    async def shutdown(self, ctx: PluginContext) -> None:
        await self._remove_pre_complete_rule(ctx)
        self._runner = None
        self._ctx = None
        logger.info("VibeCopPlugin shut down")

    async def on_config_changed(self, ctx: PluginContext, config: dict) -> None:
        merged = {**_DEFAULT_CONFIG, **config}
        self._runner = VibeCopRunner(
            vibecop_path=merged.get("vibecop_path"),
            node_path=merged.get("node_path"),
            timeout=merged.get("scan_timeout", 60),
        )

        if merged.get("enforce_vibecop_checkout", True):
            await self._inject_pre_complete_rule(ctx)
        else:
            await self._remove_pre_complete_rule(ctx)

    # --- Rule injection ---

    async def _inject_pre_complete_rule(self, ctx: PluginContext) -> None:
        """Inject a passive rule that reminds agents to run vibecop before completion."""
        rule_content = (
            "# Vibecop Pre-Completion Check\n"
            "\n"
            "Before marking your task complete, run `vibecop_scan` with `diff_ref` "
            "set to the base branch to check your changes. Fix any error-severity "
            "findings before completing the task.\n"
            "\n"
            "This rule applies to code-related tasks only: **feature**, **bugfix**, "
            "and **refactor** task types. You may skip this check for docs, chore, "
            "research, plan, test, or sync tasks.\n"
            "\n"
            "## Steps\n"
            "\n"
            "1. Run `vibecop_scan(diff_ref=\"main\")` (use your actual base branch)\n"
            "2. Review the findings -- prioritize errors over warnings\n"
            "3. Fix all error-severity findings\n"
            "4. Re-run the scan to confirm a clean result\n"
            "5. Then mark the task complete\n"
        )

        try:
            result = await ctx.execute_command(
                "save_rule",
                {
                    "id": _VIBECOP_RULE_ID,
                    "project_id": None,
                    "type": "passive",
                    "content": rule_content,
                },
            )
            if result.get("success"):
                logger.info(
                    "Injected vibecop pre-complete rule: %s",
                    result.get("id", _VIBECOP_RULE_ID),
                )
            else:
                logger.warning(
                    "Failed to inject vibecop rule: %s",
                    result.get("error", "unknown"),
                )
        except Exception:
            logger.warning("Could not inject vibecop pre-complete rule", exc_info=True)

    async def _remove_pre_complete_rule(self, ctx: PluginContext) -> None:
        """Remove the vibecop pre-complete rule on plugin shutdown."""
        try:
            result = await ctx.execute_command("delete_rule", {"id": _VIBECOP_RULE_ID})
            if result.get("success"):
                logger.info("Removed vibecop pre-complete rule")
            elif "not found" not in str(result.get("error", "")).lower():
                logger.debug(
                    "Could not remove vibecop rule: %s",
                    result.get("error", "unknown"),
                )
        except Exception:
            logger.debug("Could not remove vibecop pre-complete rule", exc_info=True)

    # --- Command handlers ---

    async def cmd_vibecop_scan(self, args: dict) -> dict:
        """Handle vibecop_scan tool invocation."""
        if self._runner is None:
            return {"success": False, "error": "VibeCop plugin not initialized"}

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

    async def cmd_vibecop_check(self, args: dict) -> dict:
        """Handle vibecop_check tool invocation."""
        if self._runner is None:
            return {"success": False, "error": "VibeCop plugin not initialized"}

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

    async def cmd_vibecop_status(self, args: dict) -> dict:
        """Handle vibecop_status tool invocation."""
        if self._runner is None:
            return {"success": False, "error": "VibeCop plugin not initialized"}
        return await self._runner.status()

    # --- Event handlers ---

    async def _on_task_completed(self, event_data: dict) -> None:
        """Scan a task's workspace when the task completes."""
        if self._ctx is None or self._runner is None:
            return

        config = self._ctx.get_config()
        merged = {**_DEFAULT_CONFIG, **config}
        if not merged.get("auto_scan_on_complete", True):
            return

        task_id = event_data.get("task_id", "")
        project_id = event_data.get("project_id", "")
        if not task_id:
            return

        try:
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
                    "vibecop auto-scan: no locked workspace for task %s, skipping",
                    task_id,
                )
                return

            projects_result = await self._ctx.execute_command("list_projects", {})
            project_name = project_id
            for proj in projects_result.get("projects", []):
                if proj.get("id") == project_id:
                    project_name = proj.get("name", project_id)
                    break

            diff_ref = "main"

            logger.info(
                "vibecop auto-scan: scanning workspace %s for task %s (diff_ref=%s)",
                workspace_path,
                task_id,
                diff_ref,
            )

            result = await self._runner.scan(path=workspace_path, diff_ref=diff_ref)

            findings = result.get("findings", [])
            severity_threshold = merged.get("default_severity", "warning")
            filtered = _filter_by_severity(findings, severity_threshold)

            await self._ctx.emit_event(
                "vibecop.scan_completed",
                {
                    "task_id": task_id,
                    "project_id": project_id,
                    "workspace_path": workspace_path,
                    "total_findings": len(filtered),
                    "files_scanned": result.get("files_scanned", 0),
                    "success": result.get("success", False),
                },
            )

            if filtered:
                await self._ctx.emit_event(
                    "vibecop.findings_detected",
                    {
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
                    },
                )

            notification = _format_discord_notification(
                project_name=project_name,
                task_id=task_id,
                findings=filtered,
                workspace_path=workspace_path,
                files_scanned=result.get("files_scanned", 0),
            )
            await self._ctx.notify(notification, project_id=project_id)

        except Exception:
            logger.exception("vibecop auto-scan failed for task %s", task_id)

    # --- Cron jobs ---

    @cron("0 6 * * 1", config_key="weekly_scan_schedule")
    async def weekly_project_scan(self, ctx: PluginContext) -> None:
        """Weekly full scan of all active project workspaces."""
        if self._runner is None:
            return

        config = ctx.get_config()
        merged = {**_DEFAULT_CONFIG, **config}
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
                    project_name,
                    workspace_path,
                )
                result = await self._runner.scan(path=workspace_path)

                findings = result.get("findings", [])
                filtered = _filter_by_severity(findings, severity_threshold)

                await ctx.emit_event(
                    "vibecop.scan_completed",
                    {
                        "project_id": project_id,
                        "workspace_path": workspace_path,
                        "total_findings": len(filtered),
                        "files_scanned": result.get("files_scanned", 0),
                        "success": result.get("success", False),
                        "scan_type": "weekly",
                    },
                )

                if filtered:
                    error_count = sum(
                        1 for f in filtered if f.get("severity") == "error"
                    )
                    await ctx.emit_event(
                        "vibecop.findings_detected",
                        {
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
                        },
                    )

                    if error_count > 0:
                        try:
                            summary = format_findings(
                                [f for f in filtered if f.get("severity") == "error"],
                                mode="summary",
                            )
                            await ctx.execute_command(
                                "create_task",
                                {
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
                                },
                            )
                        except Exception:
                            logger.exception(
                                "vibecop weekly scan: failed to create task "
                                "for errors in project %s",
                                project_id,
                            )

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
