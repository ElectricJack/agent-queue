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
STRUCTURED_SECTIONS = frozenset({"config", "tools", "mcp servers"})

# Sections whose text is captured as prompt content.
PROMPT_SECTIONS = frozenset({"role", "rules", "reflection"})

# All recognized section names (lowercase).
KNOWN_SECTIONS = STRUCTURED_SECTIONS | PROMPT_SECTIONS

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
    mcp_servers: dict = field(default_factory=dict)

    # Prompt (English) sections
    role: str = ""
    rules: str = ""
    reflection: str = ""

    # All sections for extensibility
    sections: dict[str, ProfileSection] = field(default_factory=dict)

    # Parse errors
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True if no parse errors occurred."""
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
                    f"Invalid JSON in ## {heading}: {exc.msg} "
                    f"(line {exc.lineno}, col {exc.colno})"
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
            errors.append(
                f"{prefix}: expected an object, got {type(config).__name__}"
            )
            continue

        # 'command' is required and must be a non-empty string
        if "command" not in config:
            errors.append(f"{prefix}: missing required field 'command'")
        elif not isinstance(config["command"], str):
            errors.append(
                f"{prefix}: 'command' must be a string, "
                f"got {type(config['command']).__name__}"
            )
        elif not config["command"].strip():
            errors.append(f"{prefix}: 'command' must not be empty")

        # 'args' is optional but must be a list of strings if present
        if "args" in config:
            args = config["args"]
            if not isinstance(args, list):
                errors.append(
                    f"{prefix}: 'args' must be an array, "
                    f"got {type(args).__name__}"
                )
            else:
                for i, arg in enumerate(args):
                    if not isinstance(arg, str):
                        errors.append(
                            f"{prefix}: args[{i}] must be a string, "
                            f"got {type(arg).__name__}"
                        )

        # 'env' is optional but must be a dict with string values if present
        if "env" in config:
            env = config["env"]
            if not isinstance(env, dict):
                errors.append(
                    f"{prefix}: 'env' must be an object, "
                    f"got {type(env).__name__}"
                )
            else:
                for key, val in env.items():
                    if not isinstance(val, str):
                        errors.append(
                            f"{prefix}: env['{key}'] must be a string, "
                            f"got {type(val).__name__}"
                        )

    return errors


def parse_profile(text: str) -> ParsedProfile:
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

    Returns
    -------
    ParsedProfile
        The parsed profile.  Check ``result.is_valid`` and ``result.errors``
        to determine if parsing succeeded.

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
            else:
                result.errors.append(
                    f"## Config JSON must be an object, got {type(section.json_data).__name__}"
                )
        elif heading_lower == "tools" and section.json_data is not None:
            if isinstance(section.json_data, dict):
                result.tools = section.json_data
            else:
                result.errors.append(
                    f"## Tools JSON must be an object, got {type(section.json_data).__name__}"
                )
        elif heading_lower == "mcp servers" and section.json_data is not None:
            if isinstance(section.json_data, dict):
                result.mcp_servers = section.json_data
                # Validate individual server definitions
                result.errors.extend(_validate_mcp_servers(section.json_data))
            else:
                result.errors.append(
                    f"## MCP Servers JSON must be an object, "
                    f"got {type(section.json_data).__name__}"
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

    # Config → model, permission_mode
    if parsed.config.get("model"):
        result["model"] = parsed.config["model"]
    if parsed.config.get("permission_mode"):
        result["permission_mode"] = parsed.config["permission_mode"]

    # Tools → allowed_tools
    if parsed.tools.get("allowed"):
        result["allowed_tools"] = parsed.tools["allowed"]

    # MCP Servers → mcp_servers
    if parsed.mcp_servers:
        result["mcp_servers"] = parsed.mcp_servers

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
