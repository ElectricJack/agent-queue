"""Internal plugin: project notes (list, write, read, append, delete, promote, compare).

Extracted from ``CommandHandler._cmd_list_notes`` etc.  Notes are
markdown files stored under ``{data_dir}/notes/{project_id}/``.
"""

from __future__ import annotations

import logging
import os

from src.plugins.base import InternalPlugin, PluginContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions (moved from tool_registry._ALL_TOOL_DEFINITIONS)
# ---------------------------------------------------------------------------

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
# Plugin
# ---------------------------------------------------------------------------


class NotesPlugin(InternalPlugin):
    """Project notes: list, write, read, append, delete, promote, compare."""

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._ws = ctx.get_service("workspace")
        self._db = ctx.get_service("db")
        self._mem = ctx.get_service("memory")
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
            ctx.register_tool(dict(tool_def), category="memory")

    async def shutdown(self, ctx: PluginContext) -> None:
        pass

    # --- Helpers ---

    def _get_notes_dir(self, project_id: str) -> str:
        return self._ws.get_notes_dir(project_id)

    def _resolve_note_path(self, notes_dir: str, title: str) -> str | None:
        return self._ws.resolve_note_path(notes_dir, title)

    async def _trigger_note_profile_revision(
        self, project_id: str, note_filename: str, note_content: str,
    ) -> None:
        if not self._mem.notes_inform_profile:
            return
        try:
            workspace = await self._db.get_project_workspace_path(project_id)
            if not workspace:
                return
            await self._mem.promote_note(project_id, note_filename, note_content, workspace)
        except Exception as e:
            logger.warning(
                "Profile revision after note write failed for project %s: %s",
                project_id, e,
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
            notes.append({
                "name": fname,
                "title": title,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
                "path": fpath,
            })
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
        await self._ctx.emit_event(event_type, {
            "project_id": args["project_id"],
            "note_name": f"{slug}.md",
            "note_path": fpath,
            "title": args["title"],
            "operation": "updated" if existed else "created",
        })
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
        await self._ctx.emit_event(event_type, {
            "project_id": args["project_id"],
            "note_name": os.path.basename(fpath),
            "note_path": fpath,
            "title": args["title"],
            "operation": status,
        })
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
        await self._ctx.emit_event("note.deleted", {
            "project_id": args["project_id"],
            "note_name": os.path.basename(fpath),
            "note_path": fpath,
            "title": args["title"],
        })
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

        try:
            new_profile = await self._mem.promote_note(
                project_id, note_filename, note_content, workspace
            )
        except Exception as e:
            return {"error": f"Note promotion failed: {e}"}

        if not new_profile:
            return {
                "project_id": project_id,
                "status": "no_change",
                "message": "Could not promote note into profile. Profiles may be disabled or the LLM call failed.",
            }

        return {
            "project_id": project_id,
            "note": note_filename,
            "status": "promoted",
            "message": f"Note '{note_filename}' has been incorporated into the project profile.",
            "profile_preview": new_profile[:500] + ("..." if len(new_profile) > 500 else ""),
        }

    async def cmd_compare_specs_notes(self, args: dict) -> dict:
        project = await self._db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self._db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}

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
                files.append({
                    "name": fname,
                    "title": title,
                    "size_bytes": stat.st_size,
                })
            return files

        return {
            "specs": _list_md_files(specs_path),
            "notes": _list_md_files(notes_path),
            "specs_path": specs_path,
            "notes_path": notes_path,
            "project_id": args["project_id"],
        }
