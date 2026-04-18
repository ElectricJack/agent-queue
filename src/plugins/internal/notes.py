"""Internal plugin: project notes (list, write, read, append, delete, promote, compare).

Extracted from ``CommandHandler._cmd_list_notes`` etc.  Notes are
markdown files stored under ``{data_dir}/notes/{project_id}/``.
"""

from __future__ import annotations

import os

from src.plugins.base import InternalPlugin, PluginContext


# ---------------------------------------------------------------------------
# Tool definitions (moved from tool_registry._ALL_TOOL_DEFINITIONS)
# ---------------------------------------------------------------------------

TOOL_CATEGORY = "notes"

TOOL_DEFINITIONS = [
    {
        "name": "list_notes",
        "description": "List all notes for a project. Returns name (filename), title, and size for each note. Use the 'name' field when calling read_note or delete_note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "write_note",
        "description": "Create or overwrite a project note. Use to create new notes or to save edits (read with read_file first, modify, then write back).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {"type": "string", "description": "Note title (used as filename)"},
                "content": {"type": "string", "description": "Full markdown content"},
            },
            "required": ["project_id", "title", "content"],
        },
    },
    {
        "name": "delete_note",
        "description": (
            "Delete a project note by title. If the user provides the note name "
            "directly, call this tool immediately -- no need to list notes first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {
                    "type": "string",
                    "description": (
                        "Note filename from list_notes 'name' field (e.g. 'my-note.md'), "
                        "or the note title"
                    ),
                },
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "read_note",
        "description": (
            "Read a note's full contents. Returns the markdown content, path, and size. "
            "Use the 'name' field from list_notes (e.g. 'my-note.md') as the title parameter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {
                    "type": "string",
                    "description": (
                        "Note filename from list_notes 'name' field (e.g. 'my-note.md'), "
                        "or the note title"
                    ),
                },
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "append_note",
        "description": (
            "Append content to an existing note, or create a new note if it doesn't exist. "
            "Ideal for stream-of-consciousness input -- appends with a blank line separator "
            "without needing to read and rewrite the entire note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {"type": "string", "description": "Note title (used as filename)"},
                "content": {
                    "type": "string",
                    "description": "Content to append (or initial content if creating)",
                },
            },
            "required": ["project_id", "title", "content"],
        },
    },
    {
        "name": "promote_note",
        "description": (
            "Explicitly incorporate a note's content into the project profile. "
            "Uses an LLM to integrate the note's knowledge into the living profile "
            "rather than simply appending. Use when a note contains important knowledge "
            "that should be part of the project's core understanding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {
                    "type": "string",
                    "description": (
                        "Note filename from list_notes 'name' field (e.g. 'my-note.md'), "
                        "or the note title"
                    ),
                },
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "compare_specs_notes",
        "description": (
            "List all spec files and note files for a project side by side. "
            "Returns raw file listings (names, titles, sizes) for gap analysis. "
            "Use this when the user asks to compare specs with notes or find "
            "what's missing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "specs_path": {
                    "type": "string",
                    "description": "Override path to specs directory (optional)",
                },
            },
            "required": ["project_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# CLI formatters — registered automatically by the formatter registry
# ---------------------------------------------------------------------------


def _relative_time(ts):
    """Format a Unix timestamp as relative time."""
    import time as _time

    if not ts:
        return "—"
    delta = _time.time() - ts
    if delta < 0:
        return "in the future"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _fmt_note_list(data: dict):
    from rich.table import Table

    notes = data.get("notes", [])
    table = Table(
        title=f"Notes — {data.get('project_id', '')}",
        title_style="bold bright_white",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Name", style="bold bright_cyan")
    table.add_column("Title", style="white", ratio=1)
    table.add_column("Size", justify="right", style="dim")
    table.add_column("Modified", style="dim")
    for note in notes:
        size = note.get("size_bytes", 0)
        size_str = f"{size:,}" if size < 10000 else f"{size / 1024:.1f}K"
        modified = note.get("modified")
        mod_str = (
            _relative_time(modified) if isinstance(modified, (int, float)) else str(modified or "—")
        )
        table.add_row(note.get("name", ""), note.get("title", ""), size_str, mod_str)
    return table


def _fmt_note_content(data: dict):
    from rich.console import Group
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.text import Text

    content = data.get("content", "")
    title = data.get("title", "")
    path = data.get("path", "")
    size = data.get("size_bytes", 0)
    footer = Text()
    footer.append(f"\n{path}", style="dim")
    footer.append(f"  ({size:,} bytes)", style="dim")
    return Panel(
        Group(Markdown(content), footer),
        title=f"[bold bright_white]{title}[/]",
        border_style="bright_cyan",
        padding=(1, 2),
    )


def _fmt_note_status(data: dict):
    from rich.text import Text

    status = data.get("status", "ok")
    title = data.get("title", "")
    icon = "✅" if status in ("created", "written", "appended", "deleted", "ok") else "📝"
    text = Text()
    text.append(f"{icon} ", style="bold")
    text.append(f"{status.capitalize()}", style="bold green")
    if title:
        text.append(f" — {title}", style="white")
    path = data.get("path", "")
    if path:
        text.append(f"\n  {path}", style="dim")
    return text


def _build_cli_formatters():
    """Return CLI formatter specs for notes commands."""
    from src.cli.formatter_registry import FormatterSpec

    formatters = {
        "list_notes": FormatterSpec(render=_fmt_note_list, extract=None, many=False),
        "read_note": FormatterSpec(render=_fmt_note_content, extract=None, many=False),
    }
    for cmd in ("write_note", "append_note", "delete_note", "promote_note"):
        formatters[cmd] = FormatterSpec(render=_fmt_note_status, extract=None, many=False)
    formatters["compare_specs_notes"] = FormatterSpec(
        render=_fmt_note_list,
        extract=None,
        many=False,
    )
    return formatters


CLI_FORMATTERS = _build_cli_formatters


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class NotesPlugin(InternalPlugin):
    """Project notes: list, write, read, append, delete, promote, compare."""

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._ws = ctx.get_service("workspace")
        self._db = ctx.get_service("db")
        # Use memory_v2 — v1 was removed in roadmap 8.6 and the 'memory'
        # service no longer exists.  When v2 is unavailable (degraded mode,
        # missing milvus), note-promotion features no-op instead of erroring.
        try:
            self._memv2 = ctx.get_service("memory_v2")
        except ValueError:
            self._memv2 = None
        self._git = ctx.get_service("git")
        self._cfg = ctx.get_service("config")

        ctx.register_command("list_notes", self.cmd_list_notes)
        ctx.register_command("write_note", self.cmd_write_note)
        ctx.register_command("read_note", self.cmd_read_note)
        ctx.register_command("append_note", self.cmd_append_note)
        ctx.register_command("delete_note", self.cmd_delete_note)
        ctx.register_command("promote_note", self.cmd_promote_note)
        ctx.register_command("compare_specs_notes", self.cmd_compare_specs_notes)

        for tool_def in TOOL_DEFINITIONS:
            ctx.register_tool(dict(tool_def), category="notes")

    async def shutdown(self, ctx: PluginContext) -> None:
        pass

    # --- Helpers ---

    def _get_notes_dir(self, project_id: str) -> str:
        return self._ws.get_notes_dir(project_id)

    def _resolve_note_path(self, notes_dir: str, title: str) -> str | None:
        return self._ws.resolve_note_path(notes_dir, title)

    async def _trigger_note_profile_revision(
        self,
        project_id: str,
        note_filename: str,
        note_content: str,
    ) -> None:
        """Store the note content into memory_v2 so it becomes searchable.

        Memory V1 used to LLM-rewrite the project profile from the note;
        V2's semantic store is a better fit for free-form note content —
        the extractor will classify it as an insight/fact and index it.
        """
        if not self._memv2 or not getattr(self._memv2, "available", False):
            return
        try:
            await self._ctx.execute_command(
                "memory_store",
                {
                    "project_id": project_id,
                    "content": note_content,
                    "source": f"note:{note_filename}",
                },
            )
        except Exception as e:
            self._ctx.logger.warning(
                "memory_store after note write failed for project %s: %s",
                project_id,
                e,
            )

    # --- Commands ---

    async def cmd_list_notes(self, args: dict) -> dict:
        project = await self._db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        notes_dir = self._get_notes_dir(args["project_id"])
        if not os.path.isdir(notes_dir):
            return {"project_id": args["project_id"], "notes": []}
        notes = []
        for fname in sorted(os.listdir(notes_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(notes_dir, fname)
            stat = os.stat(fpath)
            title = fname[:-3].replace("-", " ").title()
            try:
                with open(fpath, "r") as f:
                    first_line = f.readline().strip()
                if first_line.startswith("# "):
                    title = first_line[2:].strip()
            except Exception:
                pass
            notes.append(
                {
                    "name": fname,
                    "title": title,
                    "size_bytes": stat.st_size,
                    "modified": stat.st_mtime,
                    "path": fpath,
                }
            )
        return {"project_id": args["project_id"], "notes": notes}

    async def cmd_write_note(self, args: dict) -> dict:
        project = await self._db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        notes_dir = self._get_notes_dir(args["project_id"])
        os.makedirs(notes_dir, exist_ok=True)
        title_for_slug = args["title"]
        if title_for_slug.lower().endswith(".md"):
            title_for_slug = title_for_slug[:-3]
        slug = self._git.slugify(title_for_slug)
        if not slug:
            return {"error": "Title produces an empty filename"}
        fpath = os.path.join(notes_dir, f"{slug}.md")
        existed = os.path.isfile(fpath)
        with open(fpath, "w") as f:
            f.write(args["content"])
        result = {
            "path": fpath,
            "title": args["title"],
            "status": "updated" if existed else "created",
        }
        # Emit note event for hook automation
        event_type = "note.updated" if existed else "note.created"
        await self._ctx.emit_event(
            event_type,
            {
                "project_id": args["project_id"],
                "note_name": f"{slug}.md",
                "note_path": fpath,
                "title": args["title"],
                "operation": "updated" if existed else "created",
            },
        )
        await self._trigger_note_profile_revision(args["project_id"], f"{slug}.md", args["content"])
        return result

    async def cmd_read_note(self, args: dict) -> dict:
        project = await self._db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        notes_dir = self._get_notes_dir(args["project_id"])
        fpath = self._resolve_note_path(notes_dir, args["title"])
        if not fpath:
            return {"error": f"Note '{args['title']}' not found"}
        with open(fpath, "r") as f:
            content = f.read()
        stat = os.stat(fpath)
        return {
            "content": content,
            "title": args["title"],
            "path": fpath,
            "size_bytes": stat.st_size,
        }

    async def cmd_append_note(self, args: dict) -> dict:
        project = await self._db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        notes_dir = self._get_notes_dir(args["project_id"])
        os.makedirs(notes_dir, exist_ok=True)
        fpath = self._resolve_note_path(notes_dir, args["title"])
        existed = fpath is not None
        if not existed:
            title_for_slug = args["title"]
            if title_for_slug.lower().endswith(".md"):
                title_for_slug = title_for_slug[:-3]
            slug = self._git.slugify(title_for_slug)
            if not slug:
                return {"error": "Title produces an empty filename"}
            fpath = os.path.join(notes_dir, f"{slug}.md")
        if existed:
            with open(fpath, "a") as f:
                f.write(f"\n\n{args['content']}")
            status = "appended"
        else:
            with open(fpath, "w") as f:
                f.write(f"# {args['title']}\n\n{args['content']}")
            status = "created"
        stat = os.stat(fpath)
        result = {
            "path": fpath,
            "title": args["title"],
            "status": status,
            "size_bytes": stat.st_size,
        }
        event_type = "note.updated" if existed else "note.created"
        await self._ctx.emit_event(
            event_type,
            {
                "project_id": args["project_id"],
                "note_name": os.path.basename(fpath),
                "note_path": fpath,
                "title": args["title"],
                "operation": status,
            },
        )
        try:
            with open(fpath, "r") as f:
                full_content = f.read()
        except Exception:
            full_content = args["content"]
        await self._trigger_note_profile_revision(
            args["project_id"], os.path.basename(fpath), full_content
        )
        return result

    async def cmd_delete_note(self, args: dict) -> dict:
        project = await self._db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        notes_dir = self._get_notes_dir(args["project_id"])
        fpath = self._resolve_note_path(notes_dir, args["title"])
        if not fpath:
            return {"error": f"Note '{args['title']}' not found"}
        os.remove(fpath)
        await self._ctx.emit_event(
            "note.deleted",
            {
                "project_id": args["project_id"],
                "note_name": os.path.basename(fpath),
                "note_path": fpath,
                "title": args["title"],
            },
        )
        return {"deleted": fpath, "title": args["title"]}

    async def cmd_promote_note(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        title = args.get("title")
        if not title:
            return {"error": "title is required"}

        project = await self._db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        workspace = await self._db.get_project_workspace_path(project_id)
        if not workspace:
            return {"error": f"Project '{project_id}' has no workspaces."}

        notes_dir = self._get_notes_dir(project_id)
        fpath = self._resolve_note_path(notes_dir, title)
        if not fpath:
            return {"error": f"Note '{title}' not found"}

        try:
            with open(fpath, "r") as f:
                note_content = f.read()
        except Exception as e:
            return {"error": f"Failed to read note: {e}"}

        note_filename = os.path.basename(fpath)

        if not self._memv2 or not getattr(self._memv2, "available", False):
            return {
                "error": "memory_v2 service is not available; cannot promote note."
            }
        try:
            result = await self._ctx.execute_command(
                "memory_store",
                {
                    "project_id": project_id,
                    "content": note_content,
                    "source": f"note:{note_filename}",
                },
            )
        except Exception as e:
            return {"error": f"Note promotion failed: {e}"}

        if not isinstance(result, dict) or result.get("error"):
            err = result.get("error") if isinstance(result, dict) else str(result)
            return {"error": f"memory_store failed: {err}"}

        return {
            "project_id": project_id,
            "note": note_filename,
            "status": "promoted",
            "message": (
                f"Note '{note_filename}' indexed into project memory "
                f"(memory_v2). It will surface in future semantic searches."
            ),
            "memory_result": result,
        }

    async def cmd_compare_specs_notes(self, args: dict) -> dict:
        project = await self._db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self._db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {
                "error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."
            }

        specs_path = args.get("specs_path")
        if not specs_path:
            repos = await self._db.list_repos()
            for repo in repos:
                if repo.project_id == args["project_id"] and repo.source_path:
                    candidate = os.path.join(repo.source_path, "specs")
                    if os.path.isdir(candidate):
                        specs_path = candidate
                        break
            if not specs_path:
                specs_path = os.path.join(workspace, "specs")

        notes_path = self._get_notes_dir(args["project_id"])

        def _list_md_files(dirpath: str) -> list[dict]:
            if not os.path.isdir(dirpath):
                return []
            files = []
            for fname in sorted(os.listdir(dirpath)):
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(dirpath, fname)
                stat = os.stat(fpath)
                title = fname[:-3].replace("-", " ").title()
                try:
                    with open(fpath, "r") as f:
                        first_line = f.readline().strip()
                    if first_line.startswith("# "):
                        title = first_line[2:].strip()
                except Exception:
                    pass
                files.append(
                    {
                        "name": fname,
                        "title": title,
                        "size_bytes": stat.st_size,
                    }
                )
            return files

        return {
            "specs": _list_md_files(specs_path),
            "notes": _list_md_files(notes_path),
            "specs_path": specs_path,
            "notes_path": notes_path,
            "project_id": args["project_id"],
        }
