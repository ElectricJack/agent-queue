"""Async subprocess wrapper for the vibecop CLI.

Handles command construction, process execution, output parsing, and
graceful error handling when vibecop is not installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Install instructions shown when vibecop is not found
_INSTALL_INSTRUCTIONS = (
    "vibecop is not installed or not found on PATH.\n\n"
    "To install:\n"
    "  npm install -g vibecop\n\n"
    "Or use npx (no install needed):\n"
    "  npx vibecop scan <path>\n\n"
    "Requirements: Node.js >= 20\n\n"
    "You can also configure the plugin with the path to vibecop:\n"
    "  aq plugin config vibecop vibecop_path=/path/to/vibecop"
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
        """Resolve the vibecop command using the fallback chain.

        Returns:
            Command list (e.g. ["vibecop"] or ["npx", "vibecop"]), or None
            if vibecop cannot be found.
        """
        # 1. Configured path
        if self._vibecop_path:
            path = Path(self._vibecop_path)
            if path.exists():
                return [str(path)]
            # Treat as a command name (might be on PATH)
            if shutil.which(self._vibecop_path):
                return [self._vibecop_path]

        # 2. npx vibecop (works even without global install)
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
        """Execute a vibecop command and parse JSON output.

        Args:
            cmd: Full command list to execute.
            cwd: Working directory for the subprocess.

        Returns:
            Parsed result dict with success, findings, files_scanned, errors.
        """
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

        # vibecop may exit non-zero when findings exist — that's normal
        if stderr_text:
            logger.debug("vibecop stderr: %s", stderr_text)

        if not stdout_text:
            # No output at all — likely an error
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
            # Clean exit, no output = no findings
            return {
                "success": True,
                "findings": [],
                "files_scanned": 0,
                "errors": [],
            }

        # Parse JSON output
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

    async def scan(
        self,
        *,
        path: str = ".",
        diff_ref: str | None = None,
    ) -> dict:
        """Run ``vibecop scan`` on a directory.

        Args:
            path: Directory to scan.
            diff_ref: Optional git ref to diff against (scans only changed files).

        Returns:
            Result dict: {success, findings, files_scanned, errors}.
        """
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
        """Run ``vibecop check`` on specific files.

        Args:
            files: List of file paths to check.

        Returns:
            Result dict: {success, findings, files_scanned, errors}.
        """
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
        """Check vibecop installation status.

        Returns:
            Status dict with version, detectors, node_version, config info.
        """
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
                    node, "--version",
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
                *base_cmd, "--version",
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

        # Get available detectors (vibecop list-detectors --format json if available)
        try:
            proc = await asyncio.create_subprocess_exec(
                *base_cmd, "list-detectors", "--format", "json",
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
                    # Not all vibecop versions support this command
                    pass
        except (asyncio.TimeoutError, OSError):
            # list-detectors may not exist in all versions
            pass

        return result


def _normalize_output(data: dict | list) -> dict:
    """Normalize vibecop JSON output into a consistent schema.

    Vibecop's JSON output format may vary by version. This normalizes
    it into: {success, findings, files_scanned, errors}.
    """
    if isinstance(data, list):
        # Some versions return a flat array of findings
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

    files_scanned = data.get("files_scanned", data.get("filesScanned", data.get("totalFiles", 0)))

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
        "detector": finding.get("detector", finding.get("ruleId", finding.get("rule", ""))),
        "category": finding.get("category", ""),
        "message": finding.get("message", finding.get("description", "")),
        "suggestion": finding.get("suggestion", finding.get("fix", "")),
    }
