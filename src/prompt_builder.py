"""Unified prompt assembly pipeline.

Assembles system prompts from composable sections for the Supervisor and hook
LLM calls.  Replaces ``prompt_registry.py`` and scattered string concatenation
across orchestrator, adapters, supervisor, and hooks.  Each prompt is built by
stacking named sections (identity, rules, memory context, tool instructions)
with YAML-driven templates that can be overridden per-project.

The pipeline has five layers, assembled in order:

1. **Identity** — who the LLM is (loaded from a Markdown template).
2. **Project context** — project profile from the memory system.
3. **Rules** — applicable active/passive rules from the RuleManager.
4. **Context blocks** — arbitrary named sections (task depth, active
   project, dependency results, etc.).
5. **Tools** — JSON Schema tool definitions for the LLM's tool-use loop.

Templates live in ``src/prompts/*.md`` as Markdown files with YAML
frontmatter.  The ``{{variable}}`` placeholders use Mustache-style
syntax for variable substitution.

See ``specs/prompt-system.md`` for the full design specification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass
class _TemplateCache:
    """Parsed template with metadata and body."""

    name: str
    body: str
    variables: list[dict] = field(default_factory=list)
    category: str = ""
    metadata: dict = field(default_factory=dict)


class PromptBuilder:
    """Unified 5-layer prompt assembly pipeline.

    Layers:
        1. Identity — who is the LLM? (loaded from template)
        2. Project context — what project? (from memory)
        3. Relevant rules — what rules apply? (Phase 2)
        4. Specific context — named context blocks
        5. Tools — JSON Schema tool definitions
    """

    def __init__(
        self,
        project_id: str | None = None,
        memory_manager: Any | None = None,
        rule_manager: Any | None = None,
        prompts_dir: Path | str | None = None,
    ):
        """Initialise the builder with optional integrations.

        Args:
            project_id: Active project for context loading.  When set,
                layers 2 (project context) and 3 (rules) can auto-load
                relevant data.
            memory_manager: A ``MemoryManager`` instance for loading project
                profiles (layer 2).  May be ``None`` — layer 2 is skipped.
            rule_manager: A ``RuleManager`` instance for loading applicable
                rules (layer 3).  May be ``None`` — layer 3 is skipped.
            prompts_dir: Override for the template directory.  Defaults to
                ``src/prompts/``.
        """
        self._project_id = project_id
        self._memory_manager = memory_manager
        self._rule_manager = rule_manager
        self._prompts_dir = Path(prompts_dir) if prompts_dir else _DEFAULT_PROMPTS_DIR

        # Layer state
        self._identity: str = ""
        self._project_context: str = ""
        self._rules: str = ""
        self._context_blocks: list[tuple[str, str]] = []
        self._tools: list[dict] = []

        # Template cache
        self._templates: dict[str, _TemplateCache] = {}
        self._templates_loaded = False

    # ------------------------------------------------------------------
    # Template loading
    # ------------------------------------------------------------------

    def _ensure_templates_loaded(self) -> None:
        if self._templates_loaded:
            return
        self._templates_loaded = True
        if not self._prompts_dir.is_dir():
            return
        for md_file in self._prompts_dir.glob("*.md"):
            tpl = self._parse_template_file(md_file)
            if tpl:
                self._templates[tpl.name] = tpl

    def _parse_template_file(self, path: Path) -> _TemplateCache | None:
        """Parse a single Markdown template file into a ``_TemplateCache``.

        Args:
            path: Path to the ``.md`` file.

        Returns:
            A ``_TemplateCache`` with name, body, and metadata, or ``None``
            if the file could not be read.
        """
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None

        frontmatter, body = self._split_frontmatter(content)
        name = frontmatter.get("name", path.stem)
        variables = frontmatter.get("variables", [])
        category = frontmatter.get("category", "")

        return _TemplateCache(
            name=name,
            body=body.strip(),
            variables=variables if isinstance(variables, list) else [],
            category=category,
            metadata=frontmatter,
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[dict, str]:
        """Split YAML frontmatter from Markdown body.

        Args:
            content: Raw file content starting with ``---``.

        Returns:
            Tuple of ``(metadata_dict, body_string)``.  Returns
            ``({}, content)`` when no valid frontmatter is found.
        """
        if not content.startswith("---"):
            return {}, content
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return {}, content
        return meta, parts[2]

    def _render_variables(self, body: str, variables: dict[str, str] | None) -> str:
        """Substitute ``{{placeholder}}`` tokens in *body*.

        Args:
            body: Template string with ``{{variable}}`` placeholders.
            variables: Mapping of variable name → replacement value.

        Returns:
            The rendered string.  Unknown placeholders are left as-is.
        """
        if not variables:
            return body

        def replacer(match: re.Match) -> str:
            var_name = match.group(1).strip()
            return str(variables.get(var_name, match.group(0)))

        return re.sub(r"\{\{(.+?)\}\}", replacer, body)

    # ------------------------------------------------------------------
    # Public template API
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Force re-read templates from disk."""
        self._templates.clear()
        self._templates_loaded = False

    def get_template(self, name: str) -> str | None:
        """Return raw template body with {{placeholders}} intact."""
        self._ensure_templates_loaded()
        tpl = self._templates.get(name)
        return tpl.body if tpl else None

    def render_template(self, name: str, variables: dict[str, str] | None = None) -> str | None:
        """Load and render a template with variable substitution."""
        self._ensure_templates_loaded()
        tpl = self._templates.get(name)
        if not tpl:
            return None
        return self._render_variables(tpl.body, variables)

    # ------------------------------------------------------------------
    # Layer setters
    # ------------------------------------------------------------------

    def set_identity(self, name: str, variables: dict[str, str] | None = None) -> None:
        """Layer 1: Set the identity from a prompt template."""
        rendered = self.render_template(name, variables)
        self._identity = rendered or ""

    async def load_project_context(self) -> None:
        """Layer 2: Load project context from memory system."""
        if not self._memory_manager or not self._project_id:
            return
        try:
            ctx = await self._memory_manager.build_context(
                self._project_id, task=None, workspace_path=""
            )
            if ctx and not ctx.is_empty():
                self._project_context = ctx.to_context_block()
        except Exception:
            pass  # graceful degradation

    async def load_relevant_rules(self, query: str) -> None:
        """Layer 3: Load relevant rules from the rule system.

        Uses RuleManager to load all applicable rules for the current
        project (plus globals). Without memsearch, loads ALL rules.
        Future: semantic search against query for relevance filtering.
        """
        if not self._rule_manager or not self._project_id:
            self._rules = ""
            return
        try:
            rules_text = self._rule_manager.get_rules_for_prompt(self._project_id, query)
            self._rules = rules_text
        except Exception:
            self._rules = ""  # graceful degradation

    def add_context(self, name: str, content: str) -> None:
        """Layer 4: Add a named context block."""
        if content and content.strip():
            self._context_blocks.append((name, content))

    def add_context_section(self, name: str, data: dict) -> None:
        """Layer 4: Add structured context rendered as markdown.

        Special handling for name="task_depth":
          - depth 0 (root): plan structure guide
          - intermediate: controlled splitting
          - max_depth: execution focus only
        """
        if not data:
            return

        if name == "task_depth":
            self._add_depth_context(data)
            return

        lines = [f"## {name.replace('_', ' ').title()}"]
        for key, value in data.items():
            lines.append(f"- **{key}:** {value}")
        self._context_blocks.append((name, "\n".join(lines)))

    def _add_depth_context(self, data: dict) -> None:
        """Dispatch to appropriate depth-aware template."""
        depth = int(data.get("depth", 0))
        max_depth = int(data.get("max_depth", 2))
        max_steps = int(data.get("max_steps", 20))

        if depth >= max_depth:
            content = self.render_template("execution-focus") or ""
        elif depth == 0:
            content = (
                self.render_template("plan-structure-guide", {"max_steps": str(max_steps)}) or ""
            )
        else:
            content = (
                self.render_template(
                    "controlled-splitting",
                    {"current_depth": str(depth), "max_depth": str(max_depth)},
                )
                or ""
            )

        if content:
            self.add_context("execution_rules", content)

    def set_core_tools(self, tools: list[dict]) -> None:
        """Layer 5: Set tool definitions."""
        self._tools = list(tools)

    def set_tools(self, tools: list[dict]) -> None:
        """Alias for set_core_tools."""
        self.set_core_tools(tools)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> tuple[str, list[dict]]:
        """Assemble all layers into a system prompt and tool list.

        Returns:
            Tuple of ``(system_prompt, tools)`` where *system_prompt*
            is a single string with layers separated by ``---`` dividers
            and *tools* is the list of JSON Schema tool dicts.
        """
        sections: list[str] = []

        if self._identity:
            sections.append(self._identity)
        if self._project_context:
            sections.append(self._project_context)
        if self._rules:
            sections.append(self._rules)
        for _name, content in self._context_blocks:
            sections.append(content)

        system_prompt = "\n\n---\n\n".join(sections) if sections else ""
        return system_prompt, list(self._tools)

    def build_task_prompt(self) -> str:
        """Assemble into a flat prompt string (for task execution).

        Returns:
            The system prompt string only, discarding tool definitions.
            Used by adapters that manage their own tool sets.
        """
        prompt, _ = self.build()
        return prompt
