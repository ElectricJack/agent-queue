"""VibeCopPlugin — Plugin class with initialize/shutdown lifecycle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.plugins.base import Plugin, PluginPermission

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
        "enforce_vibecop_checkout": {
            "type": "boolean",
            "description": (
                "Inject a rule requiring agents to run vibecop before task completion. "
                "Applies to code-related task types (feature, bugfix, refactor)."
            ),
            "default": True,
        },
    }

    default_config = {
        "default_severity": "warning",
        "auto_install": False,
        "scan_timeout": 60,
        "enforce_vibecop_checkout": True,
    }

    # Rule ID used for the pre-completion vibecop check rule
    _VIBECOP_RULE_ID = "rule-vibecop-pre-complete-check"

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

        # --- Rule injection: remind agents to run vibecop before task completion ---
        if merged.get("enforce_vibecop_checkout", True):
            await self._inject_pre_complete_rule(ctx)

        logger.info("VibeCopPlugin initialized with config: %s", merged)

    async def shutdown(self, ctx: PluginContext) -> None:
        """Clean up resources."""
        # Remove the injected rule if it was created by this plugin
        await self._remove_pre_complete_rule(ctx)
        self._runner = None
        self._ctx = None
        logger.info("VibeCopPlugin shut down")

    async def on_config_changed(self, ctx: PluginContext, config: dict) -> None:
        """Rebuild runner and update rule injection when config changes."""
        merged = {**self.default_config, **config}
        self._runner = VibeCopRunner(
            vibecop_path=merged.get("vibecop_path"),
            node_path=merged.get("node_path"),
            timeout=merged.get("scan_timeout", 60),
        )

        # Add or remove the pre-complete rule based on config
        if merged.get("enforce_vibecop_checkout", True):
            await self._inject_pre_complete_rule(ctx)
        else:
            await self._remove_pre_complete_rule(ctx)

    # ----- Rule injection -----

    async def _inject_pre_complete_rule(self, ctx: PluginContext) -> None:
        """Inject a passive rule that reminds agents to run vibecop before completion.

        The rule is saved via the command protocol (save_rule) so it gets
        picked up by the PromptBuilder and injected into agent system prompts.
        It applies only to code-related task types: feature, bugfix, refactor.
        """
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
            "2. Review the findings — prioritize errors over warnings\n"
            "3. Fix all error-severity findings\n"
            "4. Re-run the scan to confirm a clean result\n"
            "5. Then mark the task complete\n"
        )

        try:
            result = await ctx.execute_command("save_rule", {
                "id": self._VIBECOP_RULE_ID,
                "project_id": None,  # Global rule — applies to all projects
                "type": "passive",
                "content": rule_content,
            })
            if result.get("success"):
                logger.info(
                    "Injected vibecop pre-complete rule: %s",
                    result.get("id", self._VIBECOP_RULE_ID),
                )
            else:
                logger.warning(
                    "Failed to inject vibecop rule: %s", result.get("error", "unknown")
                )
        except Exception:
            # Rule injection is best-effort — don't block plugin initialization
            logger.warning("Could not inject vibecop pre-complete rule", exc_info=True)

    async def _remove_pre_complete_rule(self, ctx: PluginContext) -> None:
        """Remove the vibecop pre-complete rule on plugin shutdown."""
        try:
            result = await ctx.execute_command("delete_rule", {
                "id": self._VIBECOP_RULE_ID,
            })
            if result.get("success"):
                logger.info("Removed vibecop pre-complete rule")
            elif "not found" not in str(result.get("error", "")).lower():
                logger.debug(
                    "Could not remove vibecop rule: %s", result.get("error", "unknown")
                )
        except Exception:
            # Best-effort cleanup — don't fail shutdown
            logger.debug("Could not remove vibecop pre-complete rule", exc_info=True)

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
