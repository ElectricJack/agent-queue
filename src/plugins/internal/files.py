"""Internal plugin: file operations (read, write, edit, glob, grep, search, list).

Extracted from ``CommandHandler._cmd_read_file`` etc.  These commands
operate on workspace files with path-validation security.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time

from src.plugins.base import InternalPlugin, PluginContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subprocess helpers (extracted from command_handler.py module level)
# ---------------------------------------------------------------------------


async def _run_subprocess(
    *args: str,
    cwd: str | None = None,
    timeout: float = 30,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    return (
        proc.returncode,
        stdout_b.decode() if stdout_b else "",
        stderr_b.decode() if stderr_b else "",
    )


# ---------------------------------------------------------------------------
# Tool definitions (moved from tool_registry._ALL_TOOL_DEFINITIONS)
# ---------------------------------------------------------------------------

TOOL_CATEGORY = "files"

TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read a file's contents from a workspace. Path can be absolute or relative to the workspaces root. Supports offset/limit for reading specific portions of large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (absolute or relative to workspaces root)",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Max lines to return (default 2000)",
                    "default": 2000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-based, default 1)",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read. If set, overrides max_lines.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file (and parent directories) if it doesn't exist, or overwrites if it does. Path can be absolute or relative to the workspaces root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (absolute or relative to workspaces root)",
                },
                "content": {"type": "string", "description": "Content to write to the file"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Perform targeted string replacement in a file. Finds old_string and replaces it with new_string. "
            "The old_string must be unique in the file (include surrounding context to disambiguate). "
            "Use replace_all=true to replace every occurrence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (absolute or relative to workspaces root)",
                },
                "old_string": {"type": "string", "description": "Exact text to find and replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false -- requires unique match)",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "glob_files",
        "description": (
            "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
            "Returns matching file paths sorted by modification time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files (e.g. '**/*.py', 'src/components/**/*.tsx')",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (absolute or relative to workspaces root)",
                },
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Search file contents using regex patterns (ripgrep-style). Supports context lines, "
            "case-insensitive search, file type filtering, and multiple output modes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (absolute or relative to workspaces root)",
                },
                "context": {
                    "type": "integer",
                    "description": "Number of context lines before and after each match",
                    "default": 0,
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default false)",
                    "default": False,
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py', '*.{ts,tsx}')",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Output mode: 'content' shows matching lines, 'files_with_matches' shows file paths only, 'count' shows match counts (default 'content')",
                    "default": "content",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of result lines to return (default 100)",
                    "default": 100,
                },
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for files or content in a workspace. Use 'grep' mode to search file contents, 'find' mode to search filenames.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Search pattern (regex for grep, glob for find)",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (absolute or relative to workspaces root)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["grep", "find"],
                    "description": "Search mode: 'grep' for content, 'find' for filenames",
                    "default": "grep",
                },
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories at a given path within a project workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "path": {
                    "type": "string",
                    "description": "Relative path within the workspace (default: root)",
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace name or ID (default: first workspace)",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "select_files_for_inspection",
        "description": (
            "Select a random sample of files from a project workspace for "
            "codebase inspection, using a weighted distribution across "
            "categories (source, specs, tests, config, recent). Automatically "
            "categorizes each tracked file by path/extension, excludes binary "
            "and generated files, and de-prioritizes files that have been "
            "inspected recently (by consulting project memory history). "
            "Returns the selected file paths plus per-category breakdown and "
            "enumeration statistics. Use this tool to implement the "
            "'codebase-inspector' playbook's file-selection step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": (
                        "Project ID whose workspace to enumerate. Falls back "
                        "to the active project if omitted."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace name or ID (default: first workspace)",
                },
                "count": {
                    "type": "integer",
                    "description": "Total number of files to select (default 5).",
                    "default": 5,
                },
                "weights": {
                    "type": "object",
                    "description": (
                        "Optional weighted distribution across categories. "
                        "Defaults to the codebase-inspector spec: "
                        "{source: 0.40, specs: 0.20, tests: 0.15, "
                        "config: 0.10, recent: 0.15}. Values are normalized."
                    ),
                },
                "recent_days": {
                    "type": "integer",
                    "description": (
                        "Files modified within this many days are eligible "
                        "for the 'recent' category (default 7)."
                    ),
                    "default": 7,
                },
                "history_lookback_days": {
                    "type": "integer",
                    "description": (
                        "Exclude files that were inspected within this "
                        "window, based on project-memory inspection records "
                        "(default 21). Set to 0 to disable."
                    ),
                    "default": 21,
                },
                "seed": {
                    "type": "integer",
                    "description": (
                        "Optional RNG seed for deterministic selection "
                        "(useful for tests and reproducible runs)."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "record_file_inspection",
        "description": (
            "Record that a file has been inspected by the codebase-inspector "
            "(or similar) workflow. Stores an entry in project memory keyed "
            "by the file path under the 'inspections' namespace, with a "
            "timestamp and optional summary. Used so future "
            "select_files_for_inspection calls can de-prioritize files that "
            "were recently inspected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID under which to record the inspection.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Relative or absolute path of the inspected file.",
                },
                "summary": {
                    "type": "string",
                    "description": "Optional short summary of the inspection outcome.",
                },
                "findings_count": {
                    "type": "integer",
                    "description": "Optional number of findings produced by the inspection.",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Optional category label (e.g. 'source', 'specs', "
                        "'tests', 'config', 'recent') for reporting."
                    ),
                },
            },
            "required": ["project_id", "file_path"],
        },
    },
]


# ---------------------------------------------------------------------------
# CLI formatters
# ---------------------------------------------------------------------------


def _fmt_file_content(data: dict):
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text

    content = data.get("content", "")
    path = data.get("path", data.get("file_path", ""))
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    lang_map = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "rs": "rust",
        "go": "go",
        "rb": "ruby",
        "sh": "bash",
        "yaml": "yaml",
        "yml": "yaml",
        "json": "json",
        "toml": "toml",
        "md": "markdown",
        "html": "html",
        "css": "css",
        "sql": "sql",
    }
    lang = lang_map.get(ext, "")
    body = Syntax(content, lang, theme="monokai", line_numbers=True) if lang else Text(content)
    return Panel(
        body, title=f"[bold bright_white]{path}[/]", border_style="bright_cyan", padding=(0, 1)
    )


def _fmt_directory_listing(data: dict):
    from rich.table import Table

    path = data.get("path", "")
    dirs = data.get("directories", [])
    files = data.get("files", [])
    table = Table(
        title=f"{path}/",
        title_style="bold bright_white",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Name", style="white", ratio=1)
    table.add_column("Size", justify="right", style="dim")
    table.add_column("Type", style="dim")
    for d in sorted(dirs):
        table.add_row(f"📁 {d}", "—", "dir")
    for f in sorted(files, key=lambda x: x.get("name", "")):
        name = f.get("name", "")
        size = f.get("size", 0)
        size_str = f"{size:,}" if size < 10000 else f"{size / 1024:.1f}K"
        table.add_row(f"  {name}", size_str, "file")
    return table


def _fmt_glob_results(data: dict):
    from rich.console import Group
    from rich.text import Text

    matches = data.get("matches", [])
    path_text = Text()
    for m in matches:
        path_text.append(f"  {m}\n", style="bright_cyan")
    header = Text(f"  {len(matches)} match(es)", style="bold")
    return Group(header, path_text)


def _fmt_grep_results(data: dict):
    from rich.console import Group
    from rich.text import Text

    matches = data.get("matches", [])
    if not matches:
        return Group(Text("  No matches found.", style="dim"))
    parts = []
    for m in matches:
        line = Text()
        line.append(f"  {m.get('file', '')}", style="bright_cyan")
        line.append(f":{m.get('line_number', '')}", style="dim")
        line.append(f"  {m.get('text', '')}", style="white")
        parts.append(line)
    header = Text(f"  {len(matches)} match(es)", style="bold")
    return Group(header, *parts)


def _fmt_file_status(data: dict):
    from rich.text import Text

    status = data.get("status", "ok")
    path = data.get("path", data.get("file_path", ""))
    icon = "✅" if status in ("written", "created", "edited", "ok") else "📝"
    text = Text()
    text.append(f"{icon} ", style="bold")
    text.append(f"{status.capitalize()}", style="bold green")
    if path:
        text.append(f" — {path}", style="dim")
    return text


def _build_cli_formatters():
    """Return CLI formatter specs for file commands."""
    from src.cli.formatter_registry import FormatterSpec

    formatters = {
        "read_file": FormatterSpec(render=_fmt_file_content, extract=None, many=False),
        "list_directory": FormatterSpec(render=_fmt_directory_listing, extract=None, many=False),
        "glob_files": FormatterSpec(render=_fmt_glob_results, extract=None, many=False),
        "grep": FormatterSpec(render=_fmt_grep_results, extract=None, many=False),
        "search_files": FormatterSpec(render=_fmt_grep_results, extract=None, many=False),
    }
    for cmd in ("write_file", "edit_file"):
        formatters[cmd] = FormatterSpec(render=_fmt_file_status, extract=None, many=False)
    return formatters


CLI_FORMATTERS = _build_cli_formatters


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class FilesPlugin(InternalPlugin):
    """File operations: read, write, edit, glob, grep, search, list."""

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._ws = ctx.get_service("workspace")
        self._cfg = ctx.get_service("config")
        self._db = ctx.get_service("db")

        # Register commands
        ctx.register_command("read_file", self.cmd_read_file)
        ctx.register_command("write_file", self.cmd_write_file)
        ctx.register_command("edit_file", self.cmd_edit_file)
        ctx.register_command("glob_files", self.cmd_glob_files)
        ctx.register_command("grep", self.cmd_grep)
        ctx.register_command("search_files", self.cmd_search_files)
        ctx.register_command("list_directory", self.cmd_list_directory)
        ctx.register_command("select_files_for_inspection", self.cmd_select_files_for_inspection)
        ctx.register_command("record_file_inspection", self.cmd_record_file_inspection)

        # Register tool definitions with category
        for tool_def in TOOL_DEFINITIONS:
            ctx.register_tool(dict(tool_def), category="files")

    async def shutdown(self, ctx: PluginContext) -> None:
        pass

    # --- Command implementations ---

    async def cmd_read_file(self, args: dict) -> dict:
        path = args["path"]
        offset = max(args.get("offset", 1), 1)
        limit = args.get("limit")
        max_lines = limit if limit is not None else args.get("max_lines", 2000)
        if not os.path.isabs(path):
            path = os.path.join(self._cfg.workspace_dir, path)
        validated = await self._ws.validate_path(path)
        if not validated:
            return {"error": "Access denied: path is outside allowed directories"}
        if not os.path.isfile(validated):
            return {"error": f"File not found: {path}"}
        try:
            with open(validated, "r") as f:
                lines = []
                total_lines = 0
                for i, line in enumerate(f, start=1):
                    total_lines = i
                    if i < offset:
                        continue
                    if len(lines) >= max_lines:
                        continue
                    lines.append(line.rstrip("\n"))
            result: dict = {"content": "\n".join(lines), "path": validated}
            if offset > 1:
                result["offset"] = offset
            if len(lines) < total_lines - (offset - 1):
                result["truncated"] = True
                result["total_lines"] = total_lines
                result["lines_returned"] = len(lines)
            return result
        except UnicodeDecodeError:
            return {"error": "Binary file -- cannot display contents"}

    async def cmd_write_file(self, args: dict) -> dict:
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return {"error": "path is required"}
        if not os.path.isabs(path):
            path = os.path.join(self._cfg.workspace_dir, path)
        validated = await self._ws.validate_path(path)
        if not validated:
            return {"error": "Access denied: path is outside allowed directories"}
        try:
            os.makedirs(os.path.dirname(validated), exist_ok=True)
            with open(validated, "w") as f:
                f.write(content)
            return {"path": validated, "written": len(content)}
        except PermissionError:
            return {"error": f"Permission denied: {path}"}
        except OSError as e:
            return {"error": f"Write failed: {e}"}

    async def cmd_edit_file(self, args: dict) -> dict:
        path = args.get("path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = args.get("replace_all", False)
        if not path:
            return {"error": "path is required"}
        if not old_string:
            return {"error": "old_string is required"}
        if not os.path.isabs(path):
            path = os.path.join(self._cfg.workspace_dir, path)
        validated = await self._ws.validate_path(path)
        if not validated:
            return {"error": "Access denied: path is outside allowed directories"}
        if not os.path.isfile(validated):
            return {"error": f"File not found: {path}"}
        try:
            with open(validated, "r") as f:
                content = f.read()
        except UnicodeDecodeError:
            return {"error": "Binary file -- cannot edit"}

        count = content.count(old_string)
        if count == 0:
            return {"error": "old_string not found in file"}
        if count > 1 and not replace_all:
            return {
                "error": (
                    f"old_string found {count} times -- must be unique. "
                    "Include more surrounding context to disambiguate, "
                    "or set replace_all=true."
                )
            }
        new_content = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )
        try:
            with open(validated, "w") as f:
                f.write(new_content)
            return {
                "path": validated,
                "replacements": count if replace_all else 1,
            }
        except PermissionError:
            return {"error": f"Permission denied: {path}"}
        except OSError as e:
            return {"error": f"Edit failed: {e}"}

    async def cmd_glob_files(self, args: dict) -> dict:
        import glob as glob_mod

        pattern = args.get("pattern", "")
        path = args.get("path", "")
        if not pattern:
            return {"error": "pattern is required"}
        if not path:
            return {"error": "path is required"}
        if not os.path.isabs(path):
            path = os.path.join(self._cfg.workspace_dir, path)
        validated = await self._ws.validate_path(path)
        if not validated:
            return {"error": "Access denied: path is outside allowed directories"}
        if not os.path.isdir(validated):
            return {"error": f"Directory not found: {path}"}

        full_pattern = os.path.join(validated, pattern)
        try:
            matches = glob_mod.glob(full_pattern, recursive=True)
            matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            total = len(matches)
            matches = matches[:500]
            rel_matches = [os.path.relpath(m, validated) for m in matches]
            result: dict = {"matches": rel_matches, "count": len(rel_matches)}
            if total > 500:
                result["truncated"] = True
                result["total"] = total
            return result
        except OSError as e:
            return {"error": f"Glob failed: {e}"}

    async def cmd_grep(self, args: dict) -> dict:
        pattern = args.get("pattern", "")
        path = args.get("path", "")
        context = args.get("context", 0)
        case_insensitive = args.get("case_insensitive", False)
        glob_filter = args.get("glob")
        output_mode = args.get("output_mode", "content")
        max_results = min(args.get("max_results", 100), 500)

        if not pattern:
            return {"error": "pattern is required"}
        if not path:
            return {"error": "path is required"}
        if not os.path.isabs(path):
            path = os.path.join(self._cfg.workspace_dir, path)
        validated = await self._ws.validate_path(path)
        if not validated:
            return {"error": "Access denied: path is outside allowed directories"}
        if not os.path.exists(validated):
            return {"error": f"Path not found: {path}"}

        cmd = ["grep", "-rn", "--color=never"]
        if case_insensitive:
            cmd.append("-i")
        if context > 0:
            cmd.extend(["-C", str(context)])
        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")
        if glob_filter:
            cmd.extend(["--include", glob_filter])
        cmd.extend(["-m", str(max_results), "-E", pattern, validated])

        try:
            rc, stdout, stderr = await _run_subprocess(*cmd, timeout=30)
            output = stdout[:8000] if stdout else "(no matches)"
            result: dict = {"results": output, "mode": output_mode}
            if rc == 1 and not stdout:
                result["results"] = "(no matches)"
            return result
        except asyncio.TimeoutError:
            return {"error": "Search timed out"}

    async def cmd_search_files(self, args: dict) -> dict:
        pattern = args["pattern"]
        path = args["path"]
        mode = args.get("mode", "grep")

        if not os.path.isabs(path):
            path = os.path.join(self._cfg.workspace_dir, path)
        validated = await self._ws.validate_path(path)
        if not validated:
            return {"error": "Access denied: path is outside allowed directories"}
        if not os.path.isdir(validated):
            return {"error": f"Directory not found: {path}"}

        try:
            if mode == "grep":
                _, stdout, _ = await _run_subprocess(
                    "grep",
                    "-rn",
                    "--include=*",
                    "-m",
                    "50",
                    pattern,
                    validated,
                    timeout=30,
                )
            else:
                _, stdout, _ = await _run_subprocess(
                    "find",
                    validated,
                    "-name",
                    pattern,
                    "-type",
                    "f",
                    timeout=30,
                )
            output = stdout[:4000] if stdout else "(no matches)"
            return {"results": output, "mode": mode}
        except asyncio.TimeoutError:
            return {"error": "Search timed out"}

    async def cmd_list_directory(self, args: dict) -> dict:
        project_id = args.get("project_id") or self._ctx.active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        workspace_name = args.get("workspace")
        if workspace_name:
            ws = await self._db.get_workspace_by_name(project_id, workspace_name)
            if not ws:
                workspaces = await self._db.list_workspaces(project_id)
                ws = next((w for w in workspaces if w.id == workspace_name), None)
            if not ws:
                return {
                    "error": f"Workspace '{workspace_name}' not found for project '{project_id}'."
                }
            ws_path = ws.workspace_path
            ws_name = ws.name or ws.id
        else:
            workspaces = await self._db.list_workspaces(project_id)
            if not workspaces:
                return {"error": f"Project '{project_id}' has no workspaces."}
            ws = workspaces[0]
            ws_path = ws.workspace_path
            ws_name = ws.name or ws.id

        if not ws_path:
            return {"error": f"Project '{project_id}' has no workspaces."}

        raw_ws_path = ws_path
        ws_path = os.path.realpath(ws_path)
        if raw_ws_path != ws_path:
            self._ctx.logger.debug(
                "list_directory: resolved workspace path %r -> %r for project %s",
                raw_ws_path,
                ws_path,
                project_id,
            )

        rel_path = args.get("path", "")
        if rel_path:
            full_path = os.path.join(ws_path, rel_path)
        else:
            full_path = ws_path

        validated = await self._ws.validate_path(full_path)
        if not validated:
            return {"error": "Access denied: path is outside allowed directories"}
        if not os.path.isdir(validated):
            return {"error": f"Directory not found: {full_path}"}

        try:
            entries = sorted(os.listdir(validated))
        except PermissionError:
            return {"error": f"Permission denied: {rel_path or '/'}"}

        dirs = []
        files = []
        for entry in entries:
            entry_path = os.path.join(validated, entry)
            if os.path.isdir(entry_path):
                dirs.append(entry)
            else:
                try:
                    size = os.path.getsize(entry_path)
                except OSError:
                    size = 0
                files.append({"name": entry, "size": size})

        return {
            "project_id": project_id,
            "path": rel_path or "/",
            "workspace_path": ws_path,
            "workspace_name": ws_name,
            "directories": dirs,
            "files": files,
        }

    # ------------------------------------------------------------------
    # select_files_for_inspection / record_file_inspection
    # ------------------------------------------------------------------

    async def _resolve_workspace_path(
        self, project_id: str, workspace_name: str | None
    ) -> tuple[str, str] | dict:
        """Resolve (ws_path, ws_name) for a project, or return an error dict."""
        if workspace_name:
            ws = await self._db.get_workspace_by_name(project_id, workspace_name)
            if not ws:
                workspaces = await self._db.list_workspaces(project_id)
                ws = next((w for w in workspaces if w.id == workspace_name), None)
            if not ws:
                return {
                    "error": (
                        f"Workspace '{workspace_name}' not found for project '{project_id}'."
                    )
                }
        else:
            workspaces = await self._db.list_workspaces(project_id)
            if not workspaces:
                return {"error": f"Project '{project_id}' has no workspaces."}
            ws = workspaces[0]
        if not ws.workspace_path:
            return {"error": f"Project '{project_id}' has no workspaces."}
        return (os.path.realpath(ws.workspace_path), ws.name or ws.id)

    async def cmd_select_files_for_inspection(self, args: dict) -> dict:
        project_id = args.get("project_id") or self._ctx.active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        count = max(1, int(args.get("count", 5)))
        recent_days = max(0, int(args.get("recent_days", 7)))
        history_lookback_days = max(0, int(args.get("history_lookback_days", 21)))
        seed = args.get("seed")
        workspace_name = args.get("workspace")

        default_weights = {
            "source": 0.40,
            "specs": 0.20,
            "tests": 0.15,
            "config": 0.10,
            "recent": 0.15,
        }
        raw_weights = args.get("weights")
        if isinstance(raw_weights, dict) and raw_weights:
            weights = {k: float(v) for k, v in raw_weights.items() if k in default_weights}
            if not weights:
                weights = dict(default_weights)
        else:
            weights = dict(default_weights)
        total_w = sum(weights.values()) or 1.0
        weights = {k: v / total_w for k, v in weights.items()}

        # Resolve workspace
        ws_info = await self._resolve_workspace_path(project_id, workspace_name)
        if isinstance(ws_info, dict):  # error dict
            return ws_info
        ws_path, ws_name = ws_info
        validated_ws = await self._ws.validate_path(ws_path)
        if not validated_ws:
            return {"error": "Access denied: workspace path is outside allowed directories"}
        if not os.path.isdir(validated_ws):
            return {"error": f"Workspace path not found: {ws_path}"}

        # Enumerate tracked files (respect .gitignore)
        try:
            all_files = await _list_tracked_files(validated_ws)
        except RuntimeError as e:
            return {"error": f"File enumeration failed: {e}"}

        # Filter out binary / generated / lockfile entries
        candidates: list[dict] = []
        now = time.time()
        recent_cutoff = now - (recent_days * 86400) if recent_days > 0 else 0
        for rel in all_files:
            if _is_excluded_path(rel):
                continue
            abs_path = os.path.join(validated_ws, rel)
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError:
                continue
            if not os.path.isfile(abs_path):
                continue
            category = categorize_file(rel)
            candidates.append(
                {
                    "path": rel,
                    "category": category,
                    "mtime": mtime,
                    "recent": recent_cutoff > 0 and mtime >= recent_cutoff,
                }
            )

        total_enumerated = len(candidates)

        # Load inspection history from project memory
        recent_history = await self._load_recent_inspections(
            project_id, history_lookback_days
        )
        excluded_count = 0
        if recent_history:
            before = len(candidates)
            candidates = [c for c in candidates if c["path"] not in recent_history]
            excluded_count = before - len(candidates)

        # Group by category
        pools: dict[str, list[dict]] = {k: [] for k in default_weights}
        for c in candidates:
            cat = c["category"]
            if cat in pools:
                pools[cat].append(c)
            if c["recent"]:
                pools["recent"].append(c)

        # Weighted selection
        rng = random.Random(seed) if seed is not None else random.Random()
        target_counts = _weighted_integer_split(weights, count)
        selected_rel: list[str] = []
        selected_breakdown: dict[str, list[str]] = {k: [] for k in default_weights}
        seen: set[str] = set()

        # First pass: draw each category up to its target count from its pool
        for cat, want in target_counts.items():
            if want <= 0:
                continue
            pool = [p for p in pools.get(cat, []) if p["path"] not in seen]
            rng.shuffle(pool)
            drawn = pool[:want]
            for entry in drawn:
                selected_rel.append(entry["path"])
                selected_breakdown[cat].append(entry["path"])
                seen.add(entry["path"])

        # Second pass: fill any shortfall from remaining candidates
        remaining_slots = count - len(selected_rel)
        if remaining_slots > 0:
            leftovers = [c for c in candidates if c["path"] not in seen]
            rng.shuffle(leftovers)
            for entry in leftovers[:remaining_slots]:
                selected_rel.append(entry["path"])
                selected_breakdown.setdefault(entry["category"], []).append(entry["path"])
                seen.add(entry["path"])

        return {
            "project_id": project_id,
            "workspace_name": ws_name,
            "workspace_path": validated_ws,
            "files": selected_rel,
            "categorized": selected_breakdown,
            "weights": weights,
            "target_counts": target_counts,
            "total_enumerated": total_enumerated,
            "excluded_history": excluded_count,
            "history_files": sorted(recent_history),
            "history_lookback_days": history_lookback_days,
        }

    async def cmd_record_file_inspection(self, args: dict) -> dict:
        project_id = args.get("project_id") or self._ctx.active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        file_path = args.get("file_path", "")
        if not file_path:
            return {"error": "file_path is required"}

        summary = args.get("summary", "") or ""
        findings_count = args.get("findings_count")
        category = args.get("category", "") or ""
        timestamp = int(time.time())

        record = {
            "file": file_path,
            "timestamp": timestamp,
            "summary": summary,
            "category": category,
        }
        if findings_count is not None:
            try:
                record["findings_count"] = int(findings_count)
            except (TypeError, ValueError):
                pass

        key = _sanitize_kv_key(file_path)
        try:
            memory_result = await self._ctx.execute_command(
                "memory_kv_set",
                {
                    "project_id": project_id,
                    "namespace": "inspections",
                    "key": key,
                    "value": json.dumps(record),
                },
            )
        except Exception as e:
            logger.warning("record_file_inspection: memory_kv_set raised %s", e)
            memory_result = {"error": str(e)}

        if isinstance(memory_result, dict) and memory_result.get("error"):
            return {
                "recorded": False,
                "project_id": project_id,
                "file_path": file_path,
                "key": key,
                "record": record,
                "warning": memory_result.get("error"),
            }

        return {
            "recorded": True,
            "project_id": project_id,
            "file_path": file_path,
            "key": key,
            "record": record,
        }

    async def _load_recent_inspections(
        self, project_id: str, lookback_days: int
    ) -> set[str]:
        """Query project memory for files inspected within ``lookback_days``."""
        if lookback_days <= 0:
            return set()
        try:
            result = await self._ctx.execute_command(
                "memory_kv_list",
                {"project_id": project_id, "namespace": "inspections"},
            )
        except Exception as e:
            logger.debug("select_files_for_inspection: history lookup failed: %s", e)
            return set()
        if not isinstance(result, dict) or result.get("error"):
            return set()

        entries = result.get("entries") or result.get("items") or result.get("results") or []
        if isinstance(entries, dict):
            entries = list(entries.values())
        cutoff = time.time() - (lookback_days * 86400)
        recent: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw_value = entry.get("value")
            if raw_value is None:
                raw_value = entry.get("kv_value")
            if not raw_value:
                continue
            try:
                parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            ts = parsed.get("timestamp")
            try:
                ts_f = float(ts) if ts is not None else 0.0
            except (TypeError, ValueError):
                ts_f = 0.0
            if ts_f < cutoff:
                continue
            file_path = parsed.get("file")
            if isinstance(file_path, str) and file_path:
                recent.add(file_path)
        return recent


# ---------------------------------------------------------------------------
# File-selection helpers
# ---------------------------------------------------------------------------

# File categories and their associated path/extension rules. Order matters:
# the first matching rule determines the category for a given file path.

_SOURCE_EXTS: frozenset[str] = frozenset(
    {
        "py", "ts", "tsx", "js", "jsx", "mjs", "cjs",
        "rs", "go", "java", "kt", "kts", "scala",
        "rb", "c", "h", "cc", "cpp", "hpp", "hh", "cs",
        "swift", "php", "lua", "ex", "exs", "erl",
        "hs", "sh", "bash", "zsh", "fish", "pl", "pm",
        "dart", "m", "mm", "clj", "cljs", "cljc", "edn",
    }
)

_SPEC_EXTS: frozenset[str] = frozenset({"md", "rst", "txt", "adoc"})

_CONFIG_NAMES: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "uv.lock",
        "poetry.lock",
        "pipfile",
        "pipfile.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "tsconfig.json",
        "tsconfig.base.json",
        "jsconfig.json",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "makefile",
        "justfile",
        "cargo.toml",
        "cargo.lock",
        "gemfile",
        "gemfile.lock",
        ".pre-commit-config.yaml",
        ".editorconfig",
        ".gitignore",
        ".dockerignore",
        ".env.example",
    }
)

_CONFIG_EXTS: frozenset[str] = frozenset(
    {"toml", "ini", "cfg", "yaml", "yml", "conf"}
)

_CONFIG_DIR_PREFIXES: tuple[str, ...] = (
    ".github/",
    ".gitlab/",
    ".circleci/",
    "ci/",
)

_SPEC_DIR_PREFIXES: tuple[str, ...] = (
    "docs/",
    "doc/",
    "specs/",
    "spec/",
    "notes/",
)

_TEST_DIR_PREFIXES: tuple[str, ...] = (
    "tests/",
    "test/",
    "__tests__/",
)

_TEST_FILENAME_MARKERS: tuple[str, ...] = (
    "test_",
    "_test.",
    ".test.",
    ".spec.",
    "_spec.",
)

_EXCLUDED_DIR_PREFIXES: tuple[str, ...] = (
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "venv/",
    ".env/",
    "dist/",
    "build/",
    "target/",
    ".tox/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".git/",
    ".idea/",
    ".vscode/",
    "coverage/",
    "htmlcov/",
    "site-packages/",
)

_BINARY_EXTS: frozenset[str] = frozenset(
    {
        "png", "jpg", "jpeg", "gif", "bmp", "svg", "ico",
        "webp", "tiff", "tif", "pdf", "zip", "tar", "gz",
        "bz2", "xz", "7z", "rar", "jar", "war", "class",
        "pyc", "pyo", "so", "dylib", "dll", "exe", "o",
        "a", "wasm", "woff", "woff2", "ttf", "otf", "eot",
        "mp3", "mp4", "wav", "avi", "mov", "webm", "mkv",
        "ogg", "flac", "heic",
    }
)

_LOCKFILE_SUFFIXES: tuple[str, ...] = (
    ".lock",
)


def _is_excluded_path(rel_path: str) -> bool:
    """Return True if the path should not be considered for inspection."""
    norm = rel_path.replace(os.sep, "/")
    while norm.startswith("./"):
        norm = norm[2:]
    lower = norm.lower()
    for prefix in _EXCLUDED_DIR_PREFIXES:
        if lower.startswith(prefix) or f"/{prefix}" in f"/{lower}":
            return True
    base = os.path.basename(lower)
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    if ext in _BINARY_EXTS:
        return True
    # Treat *.lock files as generated (but allow known config lockfiles via names)
    if base in _CONFIG_NAMES:
        return False
    for suffix in _LOCKFILE_SUFFIXES:
        if base.endswith(suffix):
            return True
    return False


def categorize_file(rel_path: str) -> str:
    """Assign a category to a file path. Returns one of:
    'source', 'specs', 'tests', 'config', or 'other'.

    The 'recent' category is applied separately by mtime and is not returned
    from this function (a file can belong to both its structural category and
    the recent pool).
    """
    norm = rel_path.replace(os.sep, "/")
    while norm.startswith("./"):
        norm = norm[2:]
    lower = norm.lower()
    base = os.path.basename(lower)
    ext = base.rsplit(".", 1)[-1] if "." in base else ""

    # Tests take precedence over source when under a tests/ dir or marked by name
    for prefix in _TEST_DIR_PREFIXES:
        if lower.startswith(prefix) or f"/{prefix}" in f"/{lower}":
            return "tests"
    if any(marker in base for marker in _TEST_FILENAME_MARKERS):
        # But only if it "looks like" a test file and has a source-ish extension.
        if ext in _SOURCE_EXTS:
            return "tests"

    # Config: known filenames / extensions / well-known dirs
    if base in _CONFIG_NAMES:
        return "config"
    for prefix in _CONFIG_DIR_PREFIXES:
        if lower.startswith(prefix):
            return "config"
    if ext in _CONFIG_EXTS:
        return "config"

    # Specs / docs
    for prefix in _SPEC_DIR_PREFIXES:
        if lower.startswith(prefix) or f"/{prefix}" in f"/{lower}":
            return "specs"
    if ext in _SPEC_EXTS:
        return "specs"

    # Source code
    if ext in _SOURCE_EXTS:
        return "source"

    return "other"


def _weighted_integer_split(weights: dict[str, float], total: int) -> dict[str, int]:
    """Split ``total`` across keys according to weights using largest-remainder."""
    if total <= 0 or not weights:
        return {k: 0 for k in weights}
    raw = {k: w * total for k, w in weights.items()}
    base = {k: int(v) for k, v in raw.items()}
    assigned = sum(base.values())
    remainder = total - assigned
    if remainder > 0:
        # Largest remainders first
        order = sorted(
            weights.keys(), key=lambda k: raw[k] - base[k], reverse=True
        )
        for k in order:
            if remainder <= 0:
                break
            base[k] += 1
            remainder -= 1
    return base


def _sanitize_kv_key(file_path: str) -> str:
    """Turn a file path into a KV-safe key by replacing separators."""
    # Milvus/vault keys should be short and filesystem-safe.
    safe = file_path.replace(os.sep, "/").strip("/")
    safe = safe.replace("/", ":")
    # Guard against pathological lengths
    if len(safe) > 240:
        safe = safe[:120] + "..." + safe[-117:]
    return safe


async def _list_tracked_files(workspace_path: str) -> list[str]:
    """Return repository-tracked files, respecting .gitignore.

    Falls back to a recursive filesystem walk if the workspace is not a git
    repository or ``git ls-files`` is unavailable.
    """
    try:
        rc, stdout, _ = await _run_subprocess(
            "git",
            "-C",
            workspace_path,
            "ls-files",
            timeout=30,
        )
        if rc == 0:
            files = [
                line.strip()
                for line in stdout.splitlines()
                if line.strip()
            ]
            if files:
                return files
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("_list_tracked_files: git ls-files unavailable: %s", e)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("_list_tracked_files: git ls-files failed: %s", e)

    # Fallback: walk filesystem, skipping excluded directories
    results: list[str] = []
    exclude_dirs = {
        "__pycache__",
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "dist",
        "build",
        "target",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
    }
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, workspace_path)
            results.append(rel.replace(os.sep, "/"))
    return results
