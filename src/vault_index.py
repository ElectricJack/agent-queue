"""Vault hub generator for Obsidian graph tree structure.

Scans the vault directory tree and generates hub files named after
their parent directory (e.g. ``projects/projects.md``,
``agent-types/agent-types.md``).  This ensures each hub node shows
a meaningful name in the Obsidian graph instead of generic "index".

When a directory already has a meaningful root file (``profile.md``,
``readme.md``), the parent hub links directly to that file instead
of creating a redundant hub.

Hub files use wiki-links with full relative paths to avoid ambiguity::

    [[projects/agent-queue/memory/memory|Memory]]
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SKIP_DIRS = {".obsidian", "__pycache__", ".git"}

# Files that are auto-overwritten — don't add backlinks
_AUTO_GENERATED_PATTERNS = {"facts.md"}
_AUTO_GENERATED_PREFIXES = ("spec-", "doc-")

# Natural root files — if present, no hub needed
_ROOT_FILES = ("profile.md", "readme.md")

# Dirs with >= this many files get a hub for grouping
_LARGE_DIR_THRESHOLD = 10

_DIR_DISPLAY_NAMES: dict[str, str] = {
    "agent-types": "Agent Types",
    "projects": "Projects",
    "system": "System",
    "templates": "Templates",
    "glossary": "Glossary",
    "memory": "Memory",
    "guidance": "Guidance",
    "insights": "Insights",
    "knowledge": "Knowledge",
    "notes": "Notes",
    "playbooks": "Playbooks",
    "references": "References",
    "tasks": "Tasks",
    "overrides": "Overrides",
    "general": "General",
    "coding": "Coding",
    "code-review": "Code Review",
    "qa": "QA",
    "vault": "Vault",
}


def _display_name(dirname: str) -> str:
    if dirname in _DIR_DISPLAY_NAMES:
        return _DIR_DISPLAY_NAMES[dirname]
    return dirname.replace("-", " ").replace("_", " ").title()


def _file_display_name(filename: str) -> str:
    stem = Path(filename).stem
    if re.match(r".*-[a-f0-9]{6}$", stem):
        stem = stem[:-7]
    if len(stem) > 60:
        stem = stem[:57] + "..."
    return stem


def _hub_filename(dir_path: Path) -> str:
    """Hub filename for a directory — matches the directory name.

    ``projects/`` → ``projects.md``
    ``vault/`` (root) → ``vault.md``
    """
    return f"{dir_path.name}.md"


def _find_root_file(dir_path: Path) -> str | None:
    for root_name in _ROOT_FILES:
        if (dir_path / root_name).is_file():
            return root_name
    return None


def _is_hub_file(filepath: Path) -> bool:
    """Check if a file is a generated hub file (named after its directory)."""
    return filepath.stem == filepath.parent.name


class VaultIndexGenerator:
    """Generates hub files throughout the vault tree.

    Hub files are named after their directory (e.g. ``projects.md``
    inside ``projects/``) so graph nodes show meaningful names.
    """

    def __init__(self, vault_root: str | Path, min_children: int = 1):
        self._root = Path(vault_root)
        self._min_children = min_children

    def generate_all(self) -> list[str]:
        """Full rebuild. Two passes: create hubs bottom-up, then
        rewrite with correct breadcrumbs (ancestors exist in pass 2).
        """
        written: list[str] = []
        hub_dirs: list[tuple[str, str]] = []

        for dirpath, dirnames, filenames in os.walk(self._root, topdown=False):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            rel_dir = os.path.relpath(dirpath, self._root)
            if rel_dir == ".":
                rel_dir = ""
            if any(part in _SKIP_DIRS for part in Path(rel_dir).parts):
                continue
            result = self._generate_hub_for_dir(dirpath, rel_dir)
            if result:
                written.append(result)
                hub_dirs.append((dirpath, rel_dir))

        # Pass 2: rewrite with correct breadcrumbs
        for dirpath, rel_dir in hub_dirs:
            self._generate_hub_for_dir(dirpath, rel_dir)

        logger.info("Generated %d vault hub files", len(written))
        return written

    async def generate_all_with_summaries(self, chat_provider: object) -> list[str]:
        """Full rebuild with LLM-generated summaries of folder contents.

        Like :meth:`generate_all` but reads child files in each hub
        directory and asks the LLM to produce a 1-2 sentence summary
        of what the knowledge in that folder covers.
        """
        written: list[str] = []
        hub_dirs: list[tuple[str, str]] = []

        # Pass 1: identify which dirs need hubs (bottom-up)
        for dirpath, dirnames, filenames in os.walk(self._root, topdown=False):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            rel_dir = os.path.relpath(dirpath, self._root)
            if rel_dir == ".":
                rel_dir = ""
            if any(part in _SKIP_DIRS for part in Path(rel_dir).parts):
                continue

            dir_path = Path(dirpath)
            hub_name = _hub_filename(dir_path)
            md_files = sorted(
                f.name for f in dir_path.iterdir()
                if f.is_file() and f.suffix == ".md"
                and f.name != hub_name and not _is_hub_file(f)
            )
            subdirs = sorted(
                d.name for d in dir_path.iterdir()
                if d.is_dir() and d.name not in _SKIP_DIRS and self._has_content(d)
            )

            if (len(md_files) + len(subdirs)) < self._min_children:
                continue
            if not self._needs_hub(dir_path, md_files, subdirs):
                continue

            hub_dirs.append((dirpath, rel_dir))

        # Pass 2: generate summaries and write hubs
        for dirpath, rel_dir in hub_dirs:
            summary = await self._generate_summary(dirpath, chat_provider)
            result = self._generate_hub_for_dir(dirpath, rel_dir, summary=summary)
            if result:
                written.append(result)

        # Pass 3: rewrite with correct parent links
        for dirpath, rel_dir in hub_dirs:
            # Preserve the summary from the file we just wrote
            hub_path = self._hub_path(Path(dirpath))
            existing_summary = ""
            if hub_path.exists():
                existing_summary = self._extract_summary(hub_path)
            self._generate_hub_for_dir(dirpath, rel_dir, summary=existing_summary)

        logger.info("Generated %d vault hub files with summaries", len(written))
        return written

    async def _generate_summary(
        self, abs_dir: str, chat_provider: object
    ) -> str:
        """Generate a short summary of the knowledge in a directory."""
        dir_path = Path(abs_dir)

        # Collect snippets from child files (first ~200 chars each)
        snippets: list[str] = []
        for f in sorted(dir_path.iterdir()):
            if not f.is_file() or f.suffix != ".md" or _is_hub_file(f):
                continue
            try:
                content = f.read_text(encoding="utf-8")
                # Strip frontmatter
                if content.startswith("---"):
                    end = content.find("\n---", 3)
                    if end != -1:
                        content = content[end + 4:]
                # Strip markdown headings for cleaner context
                clean = content.strip()[:300]
                if clean:
                    snippets.append(f"**{f.stem}**: {clean}")
            except Exception:
                continue

        # Also note subdirectory names
        for d in sorted(dir_path.iterdir()):
            if d.is_dir() and d.name not in _SKIP_DIRS and self._has_content(d):
                snippets.append(f"**{d.name}/**: (subdirectory)")

        if not snippets:
            return ""

        context = "\n".join(snippets[:20])  # Cap at 20 items
        dirname = _display_name(dir_path.name)

        prompt = (
            f"This is the '{dirname}' folder in a knowledge vault. "
            f"Based on its contents, write a single concise sentence "
            f"(max 30 words) summarizing what knowledge this folder "
            f"covers. No markdown formatting, just a plain sentence.\n\n"
            f"Contents:\n{context}"
        )

        try:
            response = await chat_provider.complete(prompt)
            summary = response.strip().strip('"').strip("'")
            # Ensure it's a reasonable length
            if summary and len(summary) < 200:
                return summary
        except Exception:
            logger.debug("Summary generation failed for %s", abs_dir, exc_info=True)

        return ""

    def _extract_summary(self, hub_path: Path) -> str:
        """Extract the summary line from an existing hub file.

        The summary is the text between the parent link and the first
        list item or heading.
        """
        try:
            content = hub_path.read_text(encoding="utf-8")
        except Exception:
            return ""

        # Skip frontmatter
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end != -1:
                content = content[end + 4:]

        lines = content.strip().split("\n")
        summary_lines: list[str] = []
        past_header = False
        for line in lines:
            if line.startswith("# "):
                past_header = True
                continue
            if not past_header:
                continue
            if line.startswith("Parent:"):
                continue
            if line.startswith("- ") or line.startswith("## ") or line.startswith("### "):
                break
            stripped = line.strip()
            if stripped:
                summary_lines.append(stripped)

        return " ".join(summary_lines).strip()

    def update_directory(self, rel_dir: str) -> str | None:
        abs_dir = self._root / rel_dir if rel_dir else self._root
        if not abs_dir.is_dir():
            return None

        # Preserve existing summary when regenerating
        existing_summary = ""
        hub = self._hub_path(abs_dir)
        if hub.exists():
            existing_summary = self._extract_summary(hub)

        result = self._generate_hub_for_dir(
            str(abs_dir), rel_dir, summary=existing_summary
        )

        if rel_dir:
            parent_rel = str(Path(rel_dir).parent)
            if parent_rel == ".":
                parent_rel = ""
            parent_abs = self._root / parent_rel if parent_rel else self._root
            if parent_abs.is_dir():
                parent_summary = ""
                parent_hub = self._hub_path(parent_abs)
                if parent_hub.exists():
                    parent_summary = self._extract_summary(parent_hub)
                self._generate_hub_for_dir(
                    str(parent_abs), parent_rel, summary=parent_summary
                )

        return result

    def _hub_path(self, dir_path: Path) -> Path:
        """Get the hub file path for a directory."""
        return dir_path / _hub_filename(dir_path)

    def _find_hub(self, dir_path: Path) -> Path | None:
        """Find the hub or root file for a directory."""
        hub = self._hub_path(dir_path)
        if hub.exists():
            return hub
        root = _find_root_file(dir_path)
        if root:
            return dir_path / root
        return None

    def _hub_link(self, rel_dir: str) -> str:
        """Wiki-link target for a directory's hub file."""
        dir_path = self._root / rel_dir if rel_dir else self._root
        dirname = dir_path.name
        return f"{rel_dir}/{dirname}" if rel_dir else dirname

    def _needs_hub(self, dir_path: Path, md_files: list[str], subdirs: list[str]) -> bool:
        if _find_root_file(dir_path):
            if len(subdirs) >= 3:
                return True
            return False
        if not subdirs and len(md_files) < _LARGE_DIR_THRESHOLD:
            return False
        if subdirs:
            return True
        if len(md_files) >= _LARGE_DIR_THRESHOLD:
            return True
        return False

    def _generate_hub_for_dir(
        self, abs_dir: str, rel_dir: str, summary: str = ""
    ) -> str | None:
        dir_path = Path(abs_dir)
        hub_name = _hub_filename(dir_path)

        md_files = sorted(
            f.name for f in dir_path.iterdir()
            if f.is_file() and f.suffix == ".md"
            and f.name != hub_name
            and not _is_hub_file(f)
        )
        subdirs = sorted(
            d.name for d in dir_path.iterdir()
            if d.is_dir() and d.name not in _SKIP_DIRS and self._has_content(d)
        )

        total_children = len(md_files) + len(subdirs)
        if total_children < self._min_children:
            return None

        if not self._needs_hub(dir_path, md_files, subdirs):
            stale = dir_path / hub_name
            if stale.exists():
                stale.unlink()
            return None

        content = self._build_hub_content(
            rel_dir, md_files, subdirs, dir_path, summary=summary
        )
        hub_path = dir_path / hub_name
        hub_path.write_text(content, encoding="utf-8")
        return str(hub_path)

    def _has_content(self, dir_path: Path) -> bool:
        for p in dir_path.rglob("*.md"):
            if not _is_hub_file(p):
                return True
        return False

    def _build_hub_content(
        self, rel_dir: str, md_files: list[str], subdirs: list[str],
        dir_path: Path, summary: str = "",
    ) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dirname = dir_path.name
        title = _display_name(dirname)

        lines: list[str] = [
            "---",
            "tags: [hub, vault-structure]",
            "type: hub",
            f"updated: {now}",
            "---",
            "",
            f"# {title}",
            "",
        ]

        # Single parent link (not a full breadcrumb chain)
        parent_link = self._get_parent_link(rel_dir)
        if parent_link:
            lines.append(parent_link)
            lines.append("")

        # LLM-generated summary of folder contents
        if summary:
            lines.append(summary)
            lines.append("")

        is_references_dir = dirname == "references"

        if is_references_dir and md_files:
            groups = self._group_reference_stubs(md_files)
            for group_name, files in groups.items():
                lines.append(f"## {group_name}")
                for f in files:
                    stem = Path(f).stem
                    link_path = f"{rel_dir}/{stem}" if rel_dir else stem
                    lines.append(f"- [[{link_path}|{_file_display_name(f)}]]")
                lines.append("")
        else:
            for subdir in subdirs:
                sub_rel = f"{rel_dir}/{subdir}" if rel_dir else subdir
                sub_path = self._root / sub_rel
                sub_display = _display_name(subdir)

                has_hub = self._hub_path(sub_path).exists()
                root_file = _find_root_file(sub_path)

                if has_hub:
                    count = sum(1 for f in sub_path.rglob("*.md")
                                if not _is_hub_file(f))
                    label = f"{sub_display} ({count})" if count > 5 else sub_display
                    lines.append(f"- [[{sub_rel}/{subdir}|{label}]]")
                elif root_file:
                    lines.append(
                        f"- [[{sub_rel}/{Path(root_file).stem}|{sub_display}]]"
                    )
                else:
                    # No hub — list subdir's files inline
                    sub_files = sorted(
                        f.name for f in sub_path.iterdir()
                        if f.is_file() and f.suffix == ".md"
                        and not _is_hub_file(f)
                    )
                    if sub_files:
                        lines.append(f"### {sub_display}")
                        for f in sub_files:
                            stem = Path(f).stem
                            lines.append(
                                f"- [[{sub_rel}/{stem}|{_file_display_name(f)}]]"
                            )
                        lines.append("")

            if md_files:
                if subdirs:
                    lines.append("")
                    lines.append("## Files")
                for f in md_files:
                    stem = Path(f).stem
                    link_path = f"{rel_dir}/{stem}" if rel_dir else stem
                    lines.append(f"- [[{link_path}|{_file_display_name(f)}]]")

        lines.append("")
        return "\n".join(lines)

    def _get_parent_link(self, rel_dir: str) -> str:
        """Return a single parent link (not a full breadcrumb chain).

        Only links to the immediate parent to keep the graph tree-like.
        """
        if not rel_dir:
            return ""

        parent_path = Path(rel_dir).parent
        if str(parent_path) == ".":
            # Parent is vault root
            root_name = self._root.name
            return f"Parent: [[{root_name}|{_display_name(root_name)}]]"

        parent_abs = self._root / parent_path
        parent_name = parent_path.name

        # Find the parent's hub or root file
        if self._hub_path(parent_abs).exists():
            target = f"{parent_path}/{parent_name}"
            return f"Parent: [[{target}|{_display_name(parent_name)}]]"

        root_file = _find_root_file(parent_abs)
        if root_file:
            target = f"{parent_path}/{Path(root_file).stem}"
            return f"Parent: [[{target}|{_display_name(parent_name)}]]"

        return ""

    def _group_reference_stubs(self, files: list[str]) -> dict[str, list[str]]:
        specs_design: list[str] = []
        specs_other: list[str] = []
        docs: list[str] = []
        other: list[str] = []

        for f in files:
            if f.startswith("spec-design-"):
                specs_design.append(f)
            elif f.startswith("spec-"):
                specs_other.append(f)
            elif f.startswith("doc-"):
                docs.append(f)
            else:
                other.append(f)

        groups: dict[str, list[str]] = {}
        if specs_design:
            groups["Specs — Design"] = specs_design
        if specs_other:
            groups["Specs — Components"] = specs_other
        if docs:
            groups["Documentation"] = docs
        if other:
            groups["Other"] = other
        return groups

    # ------------------------------------------------------------------
    # Backlink migration
    # ------------------------------------------------------------------

    def migrate_backlinks(self) -> int:
        """Add ``## See Also`` sections to existing content files."""
        from src.wiki_links import add_see_also

        modified = 0

        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

            rel_dir = os.path.relpath(dirpath, self._root)
            if rel_dir == ".":
                rel_dir = ""

            if any(part in _SKIP_DIRS for part in Path(rel_dir).parts):
                continue

            dir_p = Path(dirpath)

            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                if self._is_auto_generated(fname):
                    continue
                if fname in _ROOT_FILES:
                    continue

                filepath = dir_p / fname

                # Skip hub files
                if _is_hub_file(filepath):
                    continue

                parent_link = self._find_parent_link(rel_dir)
                if not parent_link:
                    continue

                content = filepath.read_text(encoding="utf-8")
                links: list[tuple[str, str]] = [parent_link]

                if "/insights/" in str(filepath):
                    playbook = self._extract_frontmatter_field(
                        content, "source_playbook"
                    )
                    if playbook:
                        links.append(
                            (f"system/playbooks/{playbook}", f"Source: {playbook}")
                        )

                new_content = add_see_also(content, links)
                if new_content != content:
                    filepath.write_text(new_content, encoding="utf-8")
                    modified += 1

        logger.info("Added backlinks to %d vault files", modified)
        return modified

    def _find_parent_link(self, rel_dir: str) -> tuple[str, str] | None:
        if not rel_dir:
            root_name = self._root.name
            return (root_name, _display_name(root_name))

        dir_p = self._root / rel_dir
        dirname = dir_p.name

        # If this directory has a hub file, link to it
        hub = self._hub_path(dir_p)
        if hub.exists():
            return (f"{rel_dir}/{dirname}", _display_name(dirname))

        # If this directory has a root file, link to it
        root_file = _find_root_file(dir_p)
        if root_file:
            return (f"{rel_dir}/{Path(root_file).stem}", _display_name(dirname))

        # Otherwise link to parent's hub
        parent_rel = str(Path(rel_dir).parent)
        if parent_rel == ".":
            root_name = self._root.name
            return (root_name, _display_name(root_name))
        return self._find_parent_link(parent_rel)

    def _is_auto_generated(self, filename: str) -> bool:
        if filename in _AUTO_GENERATED_PATTERNS:
            return True
        if any(filename.startswith(p) for p in _AUTO_GENERATED_PREFIXES):
            return True
        return False

    def _extract_frontmatter_field(self, content: str, field: str) -> str | None:
        if not content.startswith("---"):
            return None
        end = content.find("\n---", 3)
        if end == -1:
            return None
        fm = content[4:end]
        for line in fm.split("\n"):
            if line.startswith(f"{field}:"):
                value = line[len(field) + 1 :].strip().strip('"').strip("'")
                return value if value and value != "null" else None
        return None
