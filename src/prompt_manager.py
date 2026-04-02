"""Prompt template manager for discoverable, editable project prompts.

Prompts are stored as Markdown files with YAML frontmatter in the
``<workspace>/prompts/`` directory.  The frontmatter carries metadata
(name, description, variables, tags, category) and the body uses
Mustache-style ``{{variable}}`` placeholders for context injection.

This module provides:
  - Loading and parsing prompt templates from disk
  - Listing / filtering / searching templates
  - Rendering templates with variable substitution
  - Validation of required variables

Template format example::

    ---
    name: task-execution
    description: Prompt for executing a coding task
    category: task
    variables:
      - name: task_title
        description: The title of the task
        required: true
      - name: workspace_path
        description: Path to the workspace
        required: false
        default: "."
    tags: [execution, agent]
    version: 1
    ---

    # {{task_title}}

    You are working in ``{{workspace_path}}``.

The template system is intentionally lightweight — it uses the same
``{{placeholder}}`` syntax already used by the hook engine, keeping the
codebase consistent.  For advanced logic (conditionals, loops), users
should compose multiple templates or use the hook context-step pipeline.

See ``specs/prompt-system.md`` for the template format specification.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

PROMPTS_DIR_NAME = "prompts"

# Categories that map to subdirectories
CATEGORIES = ("system", "task", "hooks", "custom")


@dataclass
class PromptVariable:
    """Schema for a single template variable."""

    name: str
    description: str = ""
    required: bool = False
    default: str = ""


@dataclass
class PromptTemplate:
    """A parsed prompt template with metadata and body."""

    # Identity
    name: str
    description: str = ""
    category: str = "custom"

    # Schema
    variables: list[PromptVariable] = field(default_factory=list)

    # Discovery
    tags: list[str] = field(default_factory=list)
    version: int = 1
    author: str = ""

    # Content
    body: str = ""

    # Source
    file_path: str = ""
    file_name: str = ""
    size_bytes: int = 0
    modified: float = 0.0

    @property
    def required_variables(self) -> list[PromptVariable]:
        """Return only required variables."""
        return [v for v in self.variables if v.required]

    @property
    def optional_variables(self) -> list[PromptVariable]:
        """Return only optional variables."""
        return [v for v in self.variables if not v.required]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for command responses."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "version": self.version,
            "author": self.author,
            "file_name": self.file_name,
            "file_path": self.file_path,
            "size_bytes": self.size_bytes,
            "modified": self.modified,
            "variables": [
                {
                    "name": v.name,
                    "description": v.description,
                    "required": v.required,
                    "default": v.default,
                }
                for v in self.variables
            ],
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Regex to split YAML frontmatter from body.
# Matches: --- (newline) yaml content (newline) --- (newline) body
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)",
    re.DOTALL,
)

# Regex for {{variable}} placeholders (same as hooks.py)
_PLACEHOLDER_RE = re.compile(r"\{\{(.+?)\}\}")


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split a template file into (frontmatter_dict, body_text).

    If no frontmatter is found, returns an empty dict and the full content.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content

    raw_yaml = match.group(1)
    body = match.group(2)

    try:
        fm = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse YAML frontmatter: %s", exc)
        return {}, content

    if not isinstance(fm, dict):
        logger.warning("Frontmatter is not a mapping, ignoring")
        return {}, content

    return fm, body


def _parse_variables(raw: list[dict] | None) -> list[PromptVariable]:
    """Parse the ``variables`` frontmatter field into PromptVariable objects."""
    if not raw or not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        if not name:
            continue
        result.append(
            PromptVariable(
                name=name,
                description=item.get("description", ""),
                required=bool(item.get("required", False)),
                default=str(item.get("default", "")),
            )
        )
    return result


def load_template(file_path: str) -> PromptTemplate | None:
    """Load a single prompt template from a file path.

    Returns ``None`` if the file cannot be read or parsed.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Cannot read template %s: %s", file_path, exc)
        return None

    fm, body = parse_frontmatter(content)

    # Derive name from frontmatter or filename
    fname = os.path.basename(file_path)
    name = fm.get("name", "")
    if not name:
        name = fname.removesuffix(".md")

    stat = os.stat(file_path)

    # Infer category from parent directory name
    parent_dir = os.path.basename(os.path.dirname(file_path))
    category = fm.get("category", "")
    if not category:
        category = parent_dir if parent_dir in CATEGORIES else "custom"

    return PromptTemplate(
        name=name,
        description=fm.get("description", ""),
        category=category,
        variables=_parse_variables(fm.get("variables")),
        tags=fm.get("tags", []) or [],
        version=int(fm.get("version", 1)),
        author=fm.get("author", ""),
        body=body.strip(),
        file_path=file_path,
        file_name=fname,
        size_bytes=stat.st_size,
        modified=stat.st_mtime,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_template(
    template: PromptTemplate,
    variables: dict[str, str] | None = None,
    *,
    strict: bool = False,
) -> str:
    """Render a prompt template by substituting ``{{var}}`` placeholders.

    Parameters
    ----------
    template : PromptTemplate
        The parsed template to render.
    variables : dict or None
        Variable values to substitute.  Keys are variable names.
    strict : bool
        If True, raises ``ValueError`` when a required variable is missing.
        If False (default), missing variables are left as ``{{var}}`` literals.

    Returns
    -------
    str
        The rendered prompt text.
    """
    variables = variables or {}

    # Build effective values: user values > defaults > leave as-is
    effective: dict[str, str] = {}
    for var in template.variables:
        if var.name in variables:
            effective[var.name] = variables[var.name]
        elif var.default:
            effective[var.name] = var.default
        elif strict and var.required:
            raise ValueError(
                f"Required variable '{var.name}' not provided for "
                f"template '{template.name}'"
            )
    # Also include any extra variables not declared in schema
    for key, value in variables.items():
        if key not in effective:
            effective[key] = value

    def _replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        return effective.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(_replacer, template.body)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class PromptManager:
    """Manages prompt templates stored in a workspace's ``prompts/`` directory.

    This is the main entry point for loading, listing, searching, and
    rendering prompt templates.  It mirrors the notes system's approach
    of scanning a directory of ``.md`` files.
    """

    def __init__(self, prompts_dir: str):
        self.prompts_dir = prompts_dir

    # -- Listing -----------------------------------------------------------

    def list_templates(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
    ) -> list[PromptTemplate]:
        """List all prompt templates, optionally filtered by category or tag.

        Scans the prompts directory recursively for ``.md`` files,
        skipping ``README.md`` and the ``_examples/`` directory by default
        unless the category filter explicitly targets them.
        """
        if not os.path.isdir(self.prompts_dir):
            return []

        templates: list[PromptTemplate] = []
        for dirpath, dirnames, filenames in os.walk(self.prompts_dir):
            # Skip hidden directories
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

            rel_dir = os.path.relpath(dirpath, self.prompts_dir)

            # Skip _examples unless browsing root
            if rel_dir.startswith("_examples"):
                continue

            for fname in sorted(filenames):
                if not fname.endswith(".md"):
                    continue
                if fname.lower() == "readme.md":
                    continue

                fpath = os.path.join(dirpath, fname)
                tmpl = load_template(fpath)
                if tmpl is None:
                    continue

                # Apply filters
                if category and tmpl.category != category:
                    continue
                if tag and tag not in tmpl.tags:
                    continue

                templates.append(tmpl)

        # Sort by category then name
        templates.sort(key=lambda t: (t.category, t.name))
        return templates

    def get_template(self, name: str) -> PromptTemplate | None:
        """Get a specific template by name, filename, or slug.

        Searches for:
        1. Exact filename match (e.g. "task-execution.md")
        2. Name without .md extension
        3. Frontmatter ``name`` field match
        """
        if not os.path.isdir(self.prompts_dir):
            return None

        # Normalize: strip .md suffix for comparison
        search_name = name.removesuffix(".md")

        for dirpath, dirnames, filenames in os.walk(self.prompts_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

            for fname in filenames:
                if not fname.endswith(".md"):
                    continue

                # Check filename match
                fname_base = fname.removesuffix(".md")
                if fname_base == search_name or fname == name:
                    fpath = os.path.join(dirpath, fname)
                    return load_template(fpath)

        # Second pass: check frontmatter name fields
        for dirpath, dirnames, filenames in os.walk(self.prompts_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

            for fname in filenames:
                if not fname.endswith(".md"):
                    continue

                fpath = os.path.join(dirpath, fname)
                tmpl = load_template(fpath)
                if tmpl and tmpl.name == search_name:
                    return tmpl

        return None

    def get_categories(self) -> list[dict[str, Any]]:
        """Return a summary of available categories and their template counts."""
        if not os.path.isdir(self.prompts_dir):
            return []

        templates = self.list_templates()
        cat_counts: dict[str, int] = {}
        for tmpl in templates:
            cat_counts[tmpl.category] = cat_counts.get(tmpl.category, 0) + 1

        return [
            {"category": cat, "count": count}
            for cat, count in sorted(cat_counts.items())
        ]

    def get_all_tags(self) -> list[str]:
        """Return a sorted list of all unique tags across templates."""
        tags: set[str] = set()
        for tmpl in self.list_templates():
            tags.update(tmpl.tags)
        return sorted(tags)

    def render(
        self,
        name: str,
        variables: dict[str, str] | None = None,
        *,
        strict: bool = False,
    ) -> str | None:
        """Look up a template by name and render it.

        Returns None if the template is not found.
        """
        tmpl = self.get_template(name)
        if tmpl is None:
            return None
        return render_template(tmpl, variables, strict=strict)

    def ensure_directory(self) -> None:
        """Create the prompts directory structure if it doesn't exist."""
        os.makedirs(self.prompts_dir, exist_ok=True)
        for cat in CATEGORIES:
            os.makedirs(os.path.join(self.prompts_dir, cat), exist_ok=True)
