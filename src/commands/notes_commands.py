"""Notes commands mixin — note path resolution helpers."""

from __future__ import annotations

import os


class NotesCommandsMixin:
    """Notes helper methods mixed into CommandHandler.

    The actual note commands have been moved to the aq-notes plugin
    (src/plugins/internal/notes.py). These helpers remain because
    the plugin may reference them via the command handler.
    """

    # -----------------------------------------------------------------------
    # Notes commands -- markdown documents stored in project workspaces.
    # Notes are a lightweight knowledge base: users and playbooks can write
    # specs, brainstorms, or analysis, and later turn them into tasks.
    # Stored as plain .md files under <data_dir>/vault/projects/<project_id>/notes/.
    # -----------------------------------------------------------------------

    def _get_notes_dir(self, project_id: str) -> str:
        """Return the central notes directory for a project.

        Notes live in the vault at ``vault/projects/{project_id}/notes/``.
        """
        return os.path.join(self.config.data_dir, "vault", "projects", project_id, "notes")

    def _resolve_note_path(self, notes_dir: str, title: str) -> str | None:
        """Resolve a note file path from a title, filename, or slug.

        Tries in order:
        1. Exact filename match (e.g. "keen-beacon-splitting-analysis.md")
        2. Filename without .md extension (e.g. "keen-beacon-splitting-analysis")
        3. Slugified title (e.g. "Analysis: Why keen-beacon Was Not Split" → slug)

        Returns the full file path if found, None otherwise.
        """
        # 1. Exact filename
        if title.endswith(".md"):
            fpath = os.path.join(notes_dir, title)
            if os.path.isfile(fpath):
                return fpath

        # 2. Title as filename without extension
        fpath = os.path.join(notes_dir, f"{title}.md")
        if os.path.isfile(fpath):
            return fpath

        # 3. Slugified title
        slug = self.orchestrator.git.slugify(title)
        if slug:
            fpath = os.path.join(notes_dir, f"{slug}.md")
            if os.path.isfile(fpath):
                return fpath

        return None

    # _cmd_list_notes → moved to src/plugins/internal/notes.py (aq-notes plugin)

    # _cmd_write_note → moved to src/plugins/internal/notes.py (aq-notes plugin)

    # _cmd_read_note → moved to src/plugins/internal/notes.py (aq-notes plugin)

    # _cmd_append_note → moved to src/plugins/internal/notes.py (aq-notes plugin)
    # _cmd_compare_specs_notes → moved to src/plugins/internal/notes.py (aq-notes plugin)
    # _cmd_delete_note → moved to src/plugins/internal/notes.py (aq-notes plugin)
    # _cmd_promote_note → moved to src/plugins/internal/notes.py (aq-notes plugin)
    # _trigger_note_profile_revision → moved to src/plugins/internal/notes.py (aq-notes plugin)
