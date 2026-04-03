"""Internal plugin: file operations (read, write, edit, glob, grep, search, list).

Extracted from ``CommandHandler._cmd_read_file`` etc.  These commands
operate on workspace files with path-validation security.
"""

from __future__ import annotations

import asyncio
import os
import logging

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
    return proc.returncode, stdout_b.decode() if stdout_b else "", stderr_b.decode() if stderr_b else ""


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
                "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
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
                "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
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
                "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
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
                "pattern": {"type": "string", "description": "Glob pattern to match files (e.g. '**/*.py', 'src/components/**/*.tsx')"},
                "path": {"type": "string", "description": "Directory to search in (absolute or relative to workspaces root)"},
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
                "path": {"type": "string", "description": "File or directory to search in (absolute or relative to workspaces root)"},
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
                "pattern": {"type": "string", "description": "Search pattern (regex for grep, glob for find)"},
                "path": {"type": "string", "description": "Directory to search in (absolute or relative to workspaces root)"},
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
                "path": {"type": "string", "description": "Relative path within the workspace (default: root)"},
                "workspace": {"type": "string", "description": "Workspace name or ID (default: first workspace)"},
            },
            "required": ["project_id"],
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
        "py": "python", "js": "javascript", "ts": "typescript",
        "rs": "rust", "go": "go", "rb": "ruby", "sh": "bash",
        "yaml": "yaml", "yml": "yaml", "json": "json", "toml": "toml",
        "md": "markdown", "html": "html", "css": "css", "sql": "sql",
    }
    lang = lang_map.get(ext, "")
    body = Syntax(content, lang, theme="monokai", line_numbers=True) if lang else Text(content)
    return Panel(body, title=f"[bold bright_white]{path}[/]", border_style="bright_cyan", padding=(0, 1))


def _fmt_directory_listing(data: dict):
    from rich.table import Table
    path = data.get("path", "")
    dirs = data.get("directories", [])
    files = data.get("files", [])
    table = Table(
        title=f"{path}/", title_style="bold bright_white",
        border_style="bright_black", expand=True,
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
        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
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
                    "grep", "-rn", "--include=*", "-m", "50", pattern, validated,
                    timeout=30,
                )
            else:
                _, stdout, _ = await _run_subprocess(
                    "find", validated, "-name", pattern, "-type", "f",
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
                return {"error": f"Workspace '{workspace_name}' not found for project '{project_id}'."}
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
            logger.debug(
                "list_directory: resolved workspace path %r -> %r for project %s",
                raw_ws_path, ws_path, project_id,
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
