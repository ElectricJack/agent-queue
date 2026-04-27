"""Markdown profile parser — extracts structured config from hybrid profile files.

Parses the hybrid markdown format described in ``docs/specs/design/profiles.md``
Section 2.  Profiles use freeform English for behavioral guidance (injected into
agent prompts) and JSON code blocks for structured configuration (parsed
deterministically).

**Structured sections** (JSON blocks extracted):

- ``## Config`` → model, permission_mode, max_tokens_per_task
- ``## Tools`` → allowed / denied tool lists
- ``## MCP Servers`` → server name → {command, args, env}

**Prompt sections** (English text captured):

- ``## Role`` → system prompt prefix
- ``## Rules`` → behavioral guidance
- ``## Reflection`` → post-task reflection instructions

The parser is deterministic — no LLM required.  Invalid JSON in structured
sections produces parse errors (not silent fallbacks).

See ``docs/specs/design/profiles.md`` for the full specification.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

# Sections whose JSON code blocks are parsed deterministically.
STRUCTURED_SECTIONS = frozenset({"config", "tools", "mcp servers", "install"})

# Sections whose text is captured as prompt content.
PROMPT_SECTIONS = frozenset({"role", "rules", "reflection"})

# All recognized section names (lowercase).
KNOWN_SECTIONS = STRUCTURED_SECTIONS | PROMPT_SECTIONS

# Known Config-block keys with deterministic validation.
CONFIG_KNOWN_KEYS = frozenset({"model", "permission_mode", "max_tokens_per_task"})

# Valid permission_mode values (passed to the Claude Code SDK).
# Empty string is handled separately (means "use adapter default").
VALID_PERMISSION_MODES = frozenset(
    {
        "default",
        "plan",
        "full",
        "bypassPermissions",
        "acceptEdits",
        "auto",
    }
)

# Regex to find fenced code blocks: ```json ... ``` (with optional language tag)
_JSON_BLOCK_RE = re.compile(
    r"```json\s*\n(.*?)```",
    re.DOTALL,
)


@dataclass
class ProfileFrontmatter:
    """YAML frontmatter extracted from a profile markdown file."""

    id: str = ""
    name: str = ""
    tags: list[str] = field(default_factory=list)
    # Preserve any extra frontmatter keys for forward-compatibility.
    extra: dict = field(default_factory=dict)


@dataclass
class ProfileSection:
    """A single ``## heading`` section from the profile markdown.

    For structured sections (Config, Tools, MCP Servers), ``json_data``
    contains the parsed JSON and ``text`` contains any surrounding prose.
    For prompt sections (Role, Rules, Reflection), ``text`` contains the
    full section body and ``json_data`` is None.
    """

    heading: str  # Original heading text (e.g. "Config", "MCP Servers")
    raw: str  # Raw section body (everything between this heading and the next)
    text: str = ""  # Non-code-block text content (stripped)
    json_data: dict | list | None = None  # Parsed JSON (structured sections only)


@dataclass
class ParsedProfile:
    """Result of parsing a markdown profile file.

    Attributes
    ----------
    frontmatter:
        YAML frontmatter (id, name, tags).
    config:
        Parsed JSON from ``## Config`` section, or empty dict.
    tools:
        Parsed JSON from ``## Tools`` section, or empty dict.
    mcp_servers:
        Parsed JSON from ``## MCP Servers`` section, or empty dict.
    role:
        Text from ``## Role`` section, or empty string.
    rules:
        Text from ``## Rules`` section, or empty string.
    reflection:
        Text from ``## Reflection`` section, or empty string.
    sections:
        All parsed sections (including unrecognized ones) keyed by
        lowercase heading name.
    errors:
        List of parse error messages (e.g. invalid JSON).  An empty list
        means the profile parsed successfully.
    """

    frontmatter: ProfileFrontmatter = field(default_factory=ProfileFrontmatter)

    # Structured (JSON) sections
    config: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    # Names of MCP servers this profile uses.  The vault-format ``## MCP
    # Servers`` block now holds a JSON list of registry names; older files
    # that still contain a dict-of-configs are accepted for backward
    # compatibility (the keys are taken as the names) and the inline
    # configs are extracted into the registry by
    # ``src/profiles/mcp_inline_migration.py``.
    mcp_servers: list[str] = field(default_factory=list)
    # Legacy: when the ## MCP Servers block was a dict-of-configs the
    # original mapping is preserved here so the inline-config migration
    # can extract it.  ``None`` means the new list form was used.
    mcp_servers_legacy: dict | None = None
    install: dict = field(default_factory=dict)

    # Prompt (English) sections
    role: str = ""
    rules: str = ""
    reflection: str = ""

    # All sections for extensibility
    sections: dict[str, ProfileSection] = field(default_factory=dict)

    # Parse errors
    errors: list[str] = field(default_factory=list)

    # Warnings (non-fatal issues, e.g. unknown tool names)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True if no parse errors occurred.

        Warnings do not affect validity — they indicate non-fatal issues
        such as unknown tool names (the tool may not be loaded yet).
        """
        return len(self.errors) == 0


def parse_frontmatter(text: str) -> tuple[ProfileFrontmatter, str]:
    """Extract YAML frontmatter from the beginning of a markdown file.

    Parameters
    ----------
    text:
        Raw markdown content.

    Returns
    -------
    tuple[ProfileFrontmatter, str]
        The parsed frontmatter and the remaining content after the
        closing ``---`` delimiter.  If no frontmatter is found, returns
        a default ``ProfileFrontmatter`` and the original text.
    """
    if not text or not text.lstrip().startswith("---"):
        return ProfileFrontmatter(), text

    # Find opening and closing --- delimiters
    stripped = text.lstrip()
    # Skip the opening ---
    after_open = stripped[3:]
    if after_open and after_open[0] == "\n":
        after_open = after_open[1:]
    elif after_open and after_open[0] == "\r":
        after_open = after_open.lstrip("\r\n")

    # Find the closing ---
    close_idx = after_open.find("\n---")
    if close_idx == -1:
        # No closing delimiter — treat entire text as content (no frontmatter)
        return ProfileFrontmatter(), text

    yaml_text = after_open[:close_idx]
    # Find where the remaining content starts (after closing --- and its newline)
    rest_start = close_idx + 4  # len("\n---")
    remaining = after_open[rest_start:]
    if remaining and remaining[0] == "\n":
        remaining = remaining[1:]
    elif remaining and remaining[0] == "\r":
        remaining = remaining.lstrip("\r\n")

    # Parse YAML
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return ProfileFrontmatter(), text

    if not isinstance(data, dict):
        return ProfileFrontmatter(), text

    fm = ProfileFrontmatter(
        id=str(data.pop("id", "")),
        name=str(data.pop("name", "")),
        tags=data.pop("tags", []),
        extra=data,
    )
    if not isinstance(fm.tags, list):
        fm.tags = [fm.tags] if fm.tags else []

    return fm, remaining


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown text into ``(heading, body)`` tuples at ``## `` boundaries.

    Parameters
    ----------
    text:
        Markdown content (frontmatter already stripped).

    Returns
    -------
    list[tuple[str, str]]
        Each tuple is ``(heading_text, section_body)``.  Content before
        the first ``## `` heading is returned with heading ``""``.
    """
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            # Save previous section
            sections.append((current_heading, "".join(current_lines)))
            current_heading = stripped[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Save final section
    sections.append((current_heading, "".join(current_lines)))

    return sections


def _extract_json_block(text: str) -> tuple[str | None, str]:
    """Extract the first JSON code block from section text.

    Parameters
    ----------
    text:
        Section body text that may contain a fenced JSON code block.

    Returns
    -------
    tuple[str | None, str]
        ``(json_string, remaining_text)`` where *json_string* is the raw
        JSON content from inside the code fence (or None if no JSON block
        found), and *remaining_text* is the section text with the code
        block removed.
    """
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return None, text

    json_str = match.group(1).strip()
    remaining = text[: match.start()] + text[match.end() :]
    return json_str, remaining


def _extract_prompt_text(body: str) -> str:
    """Extract raw markdown text from an English prompt section.

    Analogous to :func:`_extract_json_block` for structured sections, this
    function processes the raw body of a prompt section (Role, Rules,
    Reflection).  It preserves all markdown formatting — sub-headings, lists,
    code blocks, emphasis, links — while normalising whitespace boundaries.

    Parameters
    ----------
    body:
        Raw section body (everything between ``## Heading`` and the next
        ``## Heading`` or end of file).

    Returns
    -------
    str
        The cleaned markdown text, or an empty string if the section body
        contains only whitespace.
    """
    text = body.strip()
    return text


def _parse_section(heading: str, body: str) -> tuple[ProfileSection, list[str]]:
    """Parse a single profile section.

    Parameters
    ----------
    heading:
        The section heading (e.g. "Config", "MCP Servers").
    body:
        The raw section body text.

    Returns
    -------
    tuple[ProfileSection, list[str]]
        The parsed section and any error messages.
    """
    errors: list[str] = []
    heading_lower = heading.lower()

    section = ProfileSection(heading=heading, raw=body)

    if heading_lower in STRUCTURED_SECTIONS:
        json_str, remaining_text = _extract_json_block(body)
        section.text = remaining_text.strip()

        if json_str is not None:
            try:
                section.json_data = json.loads(json_str)
            except json.JSONDecodeError as exc:
                errors.append(
                    f"Invalid JSON in ## {heading}: {exc.msg} (line {exc.lineno}, col {exc.colno})"
                )
        # No JSON block in a structured section is not an error —
        # the section may be empty or contain only prose notes.

    elif heading_lower in PROMPT_SECTIONS:
        # For prompt sections, capture raw markdown (no JSON extraction).
        section.text = _extract_prompt_text(body)

    else:
        # Unrecognized section — preserve raw text
        section.text = body.strip()

    return section, errors


def _validate_mcp_servers(servers: dict) -> list[str]:
    """Validate the structure of MCP server definitions.

    Each server entry must be an object with at least a ``command`` string.
    Optional ``args`` must be a list of strings, and optional ``env`` must
    be a dict mapping strings to strings.

    Parameters
    ----------
    servers:
        The parsed MCP Servers dict (server_name → config).

    Returns
    -------
    list[str]
        Validation error messages.  Empty list means all servers are valid.
    """
    errors: list[str] = []

    for name, config in servers.items():
        prefix = f"MCP server '{name}'"

        # Each server entry must be a dict
        if not isinstance(config, dict):
            errors.append(f"{prefix}: expected an object, got {type(config).__name__}")
            continue

        # 'command' is required and must be a non-empty string
        if "command" not in config:
            errors.append(f"{prefix}: missing required field 'command'")
        elif not isinstance(config["command"], str):
            errors.append(
                f"{prefix}: 'command' must be a string, got {type(config['command']).__name__}"
            )
        elif not config["command"].strip():
            errors.append(f"{prefix}: 'command' must not be empty")

        # 'args' is optional but must be a list of strings if present
        if "args" in config:
            args = config["args"]
            if not isinstance(args, list):
                errors.append(f"{prefix}: 'args' must be an array, got {type(args).__name__}")
            else:
                for i, arg in enumerate(args):
                    if not isinstance(arg, str):
                        errors.append(
                            f"{prefix}: args[{i}] must be a string, got {type(arg).__name__}"
                        )

        # 'env' is optional but must be a dict with string values if present
        if "env" in config:
            env = config["env"]
            if not isinstance(env, dict):
                errors.append(f"{prefix}: 'env' must be an object, got {type(env).__name__}")
            else:
                for key, val in env.items():
                    if not isinstance(val, str):
                        errors.append(
                            f"{prefix}: env['{key}'] must be a string, got {type(val).__name__}"
                        )

    return errors


def _validate_config(config: dict) -> list[str]:
    """Validate the structure and values of the ``## Config`` block.

    Validates:

    - **model** — must be a string (non-empty when present).
    - **permission_mode** — must be a string from :data:`VALID_PERMISSION_MODES`.
    - **max_tokens_per_task** — must be a positive integer.

    Unknown keys are silently allowed for forward-compatibility.

    Parameters
    ----------
    config:
        The parsed Config dict from the ``## Config`` JSON block.

    Returns
    -------
    list[str]
        Validation error messages.  Empty list means all fields are valid.
    """
    errors: list[str] = []

    # --- model ---
    if "model" in config:
        model = config["model"]
        if not isinstance(model, str):
            errors.append(f"Config 'model' must be a string, got {type(model).__name__}")
        elif not model.strip():
            errors.append("Config 'model' must not be empty")

    # --- permission_mode ---
    if "permission_mode" in config:
        pm = config["permission_mode"]
        if not isinstance(pm, str):
            errors.append(f"Config 'permission_mode' must be a string, got {type(pm).__name__}")
        elif pm not in VALID_PERMISSION_MODES:
            sorted_modes = sorted(VALID_PERMISSION_MODES)
            errors.append(f"Config 'permission_mode' must be one of {sorted_modes}, got '{pm}'")

    # --- max_tokens_per_task ---
    if "max_tokens_per_task" in config:
        mtt = config["max_tokens_per_task"]
        if isinstance(mtt, bool):
            # bool is a subclass of int in Python — reject explicitly.
            errors.append(
                f"Config 'max_tokens_per_task' must be a positive integer, got {type(mtt).__name__}"
            )
        elif not isinstance(mtt, int):
            errors.append(
                f"Config 'max_tokens_per_task' must be a positive integer, got {type(mtt).__name__}"
            )
        elif mtt <= 0:
            errors.append(f"Config 'max_tokens_per_task' must be positive, got {mtt}")

    return errors


# Known keys in the Tools block.
TOOLS_KNOWN_KEYS = frozenset({"allowed", "denied"})

# Embedded ``agent-queue`` MCP server prefix.  Tool names in
# ``## Tools.allowed`` may legacy-include this prefix; the parser strips it at
# sync time so the DB stores canonical bare names.  See
# ``docs/specs/design/profiles.md`` (Tool naming).
_AQ_PREFIX = "mcp__agent-queue__"


def _validate_tools(
    tools: dict,
    known_tools: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Validate the structure and tool names of the ``## Tools`` block.

    Validates:

    - **allowed** — must be a list of strings (when present).
    - **denied** — must be a list of strings (when present).
    - **unknown keys** — keys other than ``allowed`` and ``denied`` produce
      a warning.
    - **unknown tool names** — if *known_tools* is provided, tool names
      not in that set produce a warning (not a hard failure — the tool
      may not be loaded yet, per spec §2).
    - **duplicates** — tool names appearing in both ``allowed`` and
      ``denied`` produce a warning.

    Parameters
    ----------
    tools:
        The parsed Tools dict from the ``## Tools`` JSON block.
    known_tools:
        Optional set of recognised tool names.  When ``None``, tool-name
        validation is skipped.  Use :func:`get_registry_tool_names` to
        obtain the set from a :class:`~src.tools.registry.ToolRegistry`.

    Returns
    -------
    tuple[list[str], list[str]]
        ``(errors, warnings)`` — structural issues are errors;
        unknown/ambiguous tool names are warnings.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Warn about unknown top-level keys
    unknown_keys = set(tools.keys()) - TOOLS_KNOWN_KEYS
    for key in sorted(unknown_keys):
        warnings.append(f"Tools: unknown key '{key}' (expected 'allowed' and/or 'denied')")

    # --- allowed ---
    allowed_names: set[str] = set()
    if "allowed" in tools:
        allowed = tools["allowed"]
        if not isinstance(allowed, list):
            errors.append(f"Tools 'allowed' must be an array, got {type(allowed).__name__}")
        else:
            for i, item in enumerate(allowed):
                if not isinstance(item, str):
                    errors.append(f"Tools allowed[{i}] must be a string, got {type(item).__name__}")
                elif not item.strip():
                    errors.append(f"Tools allowed[{i}] must not be empty")
                else:
                    allowed_names.add(item)

    # --- denied ---
    denied_names: set[str] = set()
    if "denied" in tools:
        denied = tools["denied"]
        if not isinstance(denied, list):
            errors.append(f"Tools 'denied' must be an array, got {type(denied).__name__}")
        else:
            for i, item in enumerate(denied):
                if not isinstance(item, str):
                    errors.append(f"Tools denied[{i}] must be a string, got {type(item).__name__}")
                elif not item.strip():
                    errors.append(f"Tools denied[{i}] must not be empty")
                else:
                    denied_names.add(item)

    # --- Duplicates between allowed and denied ---
    overlap = allowed_names & denied_names
    for name in sorted(overlap):
        warnings.append(f"Tools: '{name}' appears in both 'allowed' and 'denied'")

    # --- Unknown tool names (warning, not error — tool may not be loaded yet) ---
    if known_tools is not None:
        for name in sorted(allowed_names - known_tools):
            warnings.append(f"Tools: unknown tool '{name}' in 'allowed'")
        for name in sorted(denied_names - known_tools):
            warnings.append(f"Tools: unknown tool '{name}' in 'denied'")

    return errors, warnings


def get_registry_tool_names(registry=None) -> set[str]:
    """Return the set of all known tool names from a ToolRegistry.

    This is a convenience function for obtaining the *known_tools* set
    to pass to :func:`parse_profile` or :func:`_validate_tools`.

    Parameters
    ----------
    registry:
        A :class:`~src.tools.registry.ToolRegistry` instance.  If ``None``,
        a fresh default registry is instantiated (built-in tools only,
        no plugins).

    Returns
    -------
    set[str]
        Set of tool name strings.
    """
    if registry is None:
        from src.tools import ToolRegistry

        registry = ToolRegistry()
    return {t["name"] for t in registry.get_all_tools()}


def parse_profile(
    text: str,
    known_tools: set[str] | None = None,
) -> ParsedProfile:
    """Parse a markdown profile file into structured data.

    This is the main entry point for profile parsing.  Given the raw
    content of a ``profile.md`` file, it extracts:

    - YAML frontmatter (id, name, tags)
    - JSON code blocks from Config, Tools, MCP Servers sections
    - English text from Role, Rules, Reflection sections

    Parameters
    ----------
    text:
        Raw content of a profile.md file (UTF-8 string).
    known_tools:
        Optional set of recognised tool names for validation.  When
        provided, tool names in the ``## Tools`` block that are not
        in this set produce a warning (not an error — the tool may
        not be loaded yet).  Use :func:`get_registry_tool_names` to
        obtain the set from a :class:`~src.tools.registry.ToolRegistry`.

    Returns
    -------
    ParsedProfile
        The parsed profile.  Check ``result.is_valid`` and ``result.errors``
        to determine if parsing succeeded.  Warnings (e.g. unknown tool
        names) are in ``result.warnings`` and do not affect validity.

    Examples
    --------
    >>> result = parse_profile('''---
    ... id: coding
    ... name: Coding Agent
    ... ---
    ...
    ... ## Config
    ... ```json
    ... {"model": "claude-sonnet-4-6"}
    ... ```
    ... ''')
    >>> result.is_valid
    True
    >>> result.config
    {'model': 'claude-sonnet-4-6'}
    >>> result.frontmatter.id
    'coding'
    """
    result = ParsedProfile()

    if not text or not text.strip():
        return result

    # 1. Extract frontmatter
    frontmatter, remaining = parse_frontmatter(text)
    result.frontmatter = frontmatter

    # 2. Split into sections
    raw_sections = _split_sections(remaining)

    # 3. Parse each section
    for heading, body in raw_sections:
        if not heading:
            # Pre-section content (e.g. # Title) — skip
            continue

        section, errors = _parse_section(heading, body)
        result.errors.extend(errors)

        heading_lower = heading.lower()
        result.sections[heading_lower] = section

        # Map to top-level fields
        if heading_lower == "config" and section.json_data is not None:
            if isinstance(section.json_data, dict):
                result.config = section.json_data
                # Validate individual config fields
                result.errors.extend(_validate_config(section.json_data))
            else:
                result.errors.append(
                    f"## Config JSON must be an object, got {type(section.json_data).__name__}"
                )
        elif heading_lower == "tools" and section.json_data is not None:
            if isinstance(section.json_data, dict):
                result.tools = section.json_data
                # Validate structure and tool names
                tool_errors, tool_warnings = _validate_tools(
                    section.json_data, known_tools=known_tools
                )
                result.errors.extend(tool_errors)
                result.warnings.extend(tool_warnings)
            else:
                result.errors.append(
                    f"## Tools JSON must be an object, got {type(section.json_data).__name__}"
                )
        elif heading_lower == "mcp servers" and section.json_data is not None:
            if isinstance(section.json_data, list):
                # New format: list of registry names.
                names: list[str] = []
                for i, item in enumerate(section.json_data):
                    if not isinstance(item, str) or not item.strip():
                        result.errors.append(
                            f"## MCP Servers[{i}] must be a non-empty string, "
                            f"got {type(item).__name__}"
                        )
                    else:
                        names.append(item.strip())
                result.mcp_servers = names
            elif isinstance(section.json_data, dict):
                # Legacy format: dict of name -> inline config.  Take the
                # keys as the server names; preserve the original mapping
                # for the inline-config migration to extract.
                result.mcp_servers = list(section.json_data.keys())
                result.mcp_servers_legacy = dict(section.json_data)
                result.errors.extend(_validate_mcp_servers(section.json_data))
            else:
                result.errors.append(
                    "## MCP Servers JSON must be a list of names "
                    f"(or legacy object), got {type(section.json_data).__name__}"
                )
        elif heading_lower == "install" and section.json_data is not None:
            if isinstance(section.json_data, dict):
                result.install = section.json_data
            else:
                result.errors.append(
                    f"## Install JSON must be an object, got {type(section.json_data).__name__}"
                )
        elif heading_lower == "role":
            result.role = section.text
        elif heading_lower == "rules":
            result.rules = section.text
        elif heading_lower == "reflection":
            result.reflection = section.text

    return result


def parsed_profile_to_agent_profile(parsed: ParsedProfile) -> dict:
    """Convert a :class:`ParsedProfile` to an ``AgentProfile``-compatible dict.

    Maps the parsed markdown fields onto the field names used by
    :class:`~src.models.AgentProfile`.  This dict can be used to
    construct or update an ``AgentProfile`` instance.

    Parameters
    ----------
    parsed:
        A successfully parsed profile.

    Returns
    -------
    dict
        Keys match ``AgentProfile`` field names.  Only fields with
        non-empty values are included.
    """
    result: dict = {}

    # Frontmatter → identity fields
    if parsed.frontmatter.id:
        result["id"] = parsed.frontmatter.id
    if parsed.frontmatter.name:
        result["name"] = parsed.frontmatter.name
    # Description lives in frontmatter.extra (not a dedicated field)
    if parsed.frontmatter.extra.get("description"):
        result["description"] = str(parsed.frontmatter.extra["description"])
    # memory_scope_id — when present, redirects the profile's agent-type
    # memory scope so multiple profiles can share one pool.
    if parsed.frontmatter.extra.get("memory_scope_id"):
        result["memory_scope_id"] = str(parsed.frontmatter.extra["memory_scope_id"])

    # Config → model, permission_mode
    if parsed.config.get("model"):
        result["model"] = parsed.config["model"]
    if parsed.config.get("permission_mode"):
        result["permission_mode"] = parsed.config["permission_mode"]

    # Tools → allowed_tools.  Strip the embedded MCP server prefix at sync
    # time so the DB always stores canonical bare names — the supervisor's
    # tool registry uses bare names, and the Claude CLI adapter re-adds
    # ``mcp__agent-queue__`` at the transport layer.  Keeps third-party MCP
    # tool prefixes (``mcp__github__...``) intact.
    if parsed.tools.get("allowed"):
        result["allowed_tools"] = [
            t[len(_AQ_PREFIX) :] if isinstance(t, str) and t.startswith(_AQ_PREFIX) else t
            for t in parsed.tools["allowed"]
        ]

    # MCP Servers → mcp_servers (always list[str] of registry names).
    if parsed.mcp_servers:
        result["mcp_servers"] = list(parsed.mcp_servers)

    # Install → install manifest
    if parsed.install:
        result["install"] = parsed.install

    # Prompt sections → individual fields + combined system_prompt_suffix
    # Expose each section as a separate field for downstream consumers that
    # need them individually (e.g. Role for system prompt prefix, Reflection
    # for post-task processing).
    if parsed.role:
        result["role"] = parsed.role
    if parsed.rules:
        result["rules"] = parsed.rules
    if parsed.reflection:
        result["reflection"] = parsed.reflection

    # Build system_prompt_suffix with section labels so the receiving LLM
    # can distinguish Role (identity) from Rules (constraints) from
    # Reflection (post-task guidance).
    prompt_parts: list[str] = []
    if parsed.role:
        prompt_parts.append(f"## Role\n{parsed.role}")
    if parsed.rules:
        prompt_parts.append(f"## Rules\n{parsed.rules}")
    if parsed.reflection:
        prompt_parts.append(f"## Reflection\n{parsed.reflection}")
    if prompt_parts:
        result["system_prompt_suffix"] = "\n\n".join(prompt_parts)

    return result


def _split_system_prompt_suffix(suffix: str) -> tuple[str, str, str]:
    """Split a combined ``system_prompt_suffix`` back into (role, rules, reflection).

    The :func:`parsed_profile_to_agent_profile` function builds
    ``system_prompt_suffix`` by joining sections with ``## Role``,
    ``## Rules``, ``## Reflection`` headings.  This function reverses
    that operation for round-tripping back to markdown.

    Parameters
    ----------
    suffix:
        The combined system_prompt_suffix string.

    Returns
    -------
    tuple[str, str, str]
        ``(role, rules, reflection)`` text.  If no section markers are
        found, the entire suffix is returned as the role.
    """
    if not suffix:
        return "", "", ""

    # Split on ## heading markers that were injected by parsed_profile_to_agent_profile
    parts = re.split(r"(?:^|\n\n)## (Role|Rules|Reflection)\n", suffix)

    if len(parts) <= 1:
        # No markers found — treat entire text as role content
        return suffix.strip(), "", ""

    role = ""
    rules = ""
    reflection = ""

    # parts[0] is text before the first ## heading (usually empty).
    # Then alternating: heading_name, content, heading_name, content, ...
    i = 1
    while i < len(parts) - 1:
        heading = parts[i].lower()
        content = parts[i + 1].strip()
        if heading == "role":
            role = content
        elif heading == "rules":
            rules = content
        elif heading == "reflection":
            reflection = content
        i += 2

    return role, rules, reflection


def agent_profile_to_markdown(
    *,
    id: str,
    name: str,
    description: str = "",
    model: str = "",
    permission_mode: str = "",
    allowed_tools: list[str] | None = None,
    mcp_servers: list[str] | dict[str, dict] | None = None,
    system_prompt_suffix: str = "",
    install: dict | None = None,
    role: str = "",
    rules: str = "",
    reflection: str = "",
    tags: list[str] | None = None,
) -> str:
    """Render profile fields into the hybrid markdown format.

    This is the inverse of :func:`parse_profile` — given the structured fields
    of an agent profile, it produces a markdown string suitable for writing to
    ``vault/agent-types/{id}/profile.md``.

    When *role*, *rules*, or *reflection* are not provided individually but
    *system_prompt_suffix* is, the function attempts to split the suffix back
    into its component sections (assuming it was produced by
    :func:`parsed_profile_to_agent_profile`).

    Parameters
    ----------
    id:
        Profile identifier (slug).
    name:
        Display name.
    description:
        Optional description (stored in frontmatter).
    model:
        Model override (empty = use default).
    permission_mode:
        Permission mode override (empty = use default).
    allowed_tools:
        Tool whitelist.
    mcp_servers:
        MCP server configurations.
    system_prompt_suffix:
        Combined prompt text (used as fallback when individual sections
        are not provided).
    install:
        Install manifest dict (npm, pip, commands).
    role:
        Role section text.
    rules:
        Rules section text.
    reflection:
        Reflection section text.
    tags:
        Optional frontmatter tags.

    Returns
    -------
    str
        The rendered markdown profile.
    """
    lines: list[str] = []

    # --- Frontmatter ---
    fm_data: dict = {"id": id, "name": name}
    if description:
        fm_data["description"] = description
    if tags:
        fm_data["tags"] = tags

    lines.append("---")
    lines.append(yaml.dump(fm_data, default_flow_style=False, sort_keys=False).rstrip())
    lines.append("---")
    lines.append("")
    lines.append(f"# {name}")
    lines.append("")

    # Resolve role/rules/reflection from system_prompt_suffix if not provided
    if not role and not rules and not reflection and system_prompt_suffix:
        role, rules, reflection = _split_system_prompt_suffix(system_prompt_suffix)

    # --- Role section ---
    if role:
        lines.append("## Role")
        lines.append(role)
        lines.append("")

    # --- Config section ---
    config: dict = {}
    if model:
        config["model"] = model
    if permission_mode:
        config["permission_mode"] = permission_mode
    if config:
        lines.append("## Config")
        lines.append("```json")
        lines.append(json.dumps(config, indent=2))
        lines.append("```")
        lines.append("")

    # --- Tools section ---
    if allowed_tools:
        tools_data = {"allowed": allowed_tools}
        lines.append("## Tools")
        lines.append("```json")
        lines.append(json.dumps(tools_data, indent=2))
        lines.append("```")
        lines.append("")

    # --- MCP Servers section ---
    # Always render as a JSON list of registry names.  Accept legacy dicts
    # for callers that haven't been updated yet — keys become the names.
    if mcp_servers:
        if isinstance(mcp_servers, dict):
            names_list = list(mcp_servers.keys())
        else:
            names_list = list(mcp_servers)
        if names_list:
            lines.append("## MCP Servers")
            lines.append("```json")
            lines.append(json.dumps(names_list, indent=2))
            lines.append("```")
            lines.append("")

    # --- Rules section ---
    if rules:
        lines.append("## Rules")
        lines.append(rules)
        lines.append("")

    # --- Reflection section ---
    if reflection:
        lines.append("## Reflection")
        lines.append(reflection)
        lines.append("")

    # --- Install section ---
    if install:
        lines.append("## Install")
        lines.append("```json")
        lines.append(json.dumps(install, indent=2))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
