"""Markdown profile parser — extracts structured config from hybrid profile files.

Parses the hybrid markdown format described in ``docs/specs/design/profiles.md``
Section 2.  Profiles use freeform English for behavioral guidance (injected into
agent prompts) and JSON code blocks for structured configuration (parsed
deterministically).

**Structured sections** (JSON blocks extracted):

- ``## Config`` → harness, default_class, permission_mode, provider-specific
  autonomous permission opt-ins, max_tokens_per_task
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

from src.profiles.capabilities import NAMESPACES, WILDCARD_CHARS

logger = logging.getLogger(__name__)

# Sections whose JSON code blocks are parsed deterministically.
STRUCTURED_SECTIONS = frozenset({"config", "tools", "capabilities", "mcp servers", "install"})

# Sections whose text is captured as prompt content.
PROMPT_SECTIONS = frozenset({"role", "rules", "reflection"})

# All recognized section names (lowercase).
KNOWN_SECTIONS = STRUCTURED_SECTIONS | PROMPT_SECTIONS

# Known Config-block keys with deterministic validation.
CONFIG_KNOWN_KEYS = frozenset(
    {
        "permission_mode",
        "codex_full_auto",
        "claude_dangerously_skip_permissions",
        "max_tokens_per_task",
        # Named-session fields (supervisor-agent spec §7).  ``workspaces``
        # is parsed and validated here but is *not* persisted on
        # ``agent_profiles`` — session-runtime owns attachment resolution.
        "harness",
        "lifecycle",
        "mode",
        "wake_mode",
        "idle_timeout",
        "max_session_age",
        "workspaces",
        "default_class",
        "needs_workspace",
        "read_only",
        "allow_base_checkout",
        "min_active",
        "max_active",
        "max_claims_per_session",
    }
)

# Valid ``lifecycle`` values.  ``task`` is the default (one session per
# task); ``named`` is a long-lived session addressed by name (mechanics
# owned by the session-runtime spec).
VALID_LIFECYCLES = frozenset({"task", "named", "pool"})

# Valid ``mode`` values — meaningful only with ``lifecycle: named``.
VALID_MODES = frozenset({"always", "on_demand"})

# Valid ``wake_mode`` values — meaningful only with ``lifecycle: named``.
VALID_WAKE_MODES = frozenset({"resume", "fresh"})

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
    # ``## Capabilities`` — the normalized three-namespace replacement for
    # ``## Tools`` (Playbook V2 Package 0 §3.2).  ``None`` means the block
    # was absent, which is what routes the profile through the legacy
    # ``allowed_tools`` adapter; a present block must name all three keys.
    capabilities: dict[str, list[str]] | None = None
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

    - **model** — retired; select an intelligence class instead.
    - **permission_mode** — must be a string from :data:`VALID_PERMISSION_MODES`.
    - **codex_full_auto** / **claude_dangerously_skip_permissions** — strict
      booleans whose enabled value requires the matching harness.
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
    # A profile's model is derived from its intelligence class and harness.
    # Keep this explicit (rather than treating it as an unknown key) so a
    # hand-edited legacy profile gets an actionable error.
    if "model" in config:
        errors.append(
            "Config 'model' was removed; select 'default_class' instead. "
            "The class and harness resolve the launch model."
        )

    # --- permission_mode ---
    if "permission_mode" in config:
        pm = config["permission_mode"]
        if not isinstance(pm, str):
            errors.append(f"Config 'permission_mode' must be a string, got {type(pm).__name__}")
        elif pm not in VALID_PERMISSION_MODES:
            sorted_modes = sorted(VALID_PERMISSION_MODES)
            errors.append(f"Config 'permission_mode' must be one of {sorted_modes}, got '{pm}'")

    # Provider-specific autonomous permission modes are explicit booleans.
    # ``false`` is accepted with every harness so generated/default config can
    # carry disabled values harmlessly; enabling one requires its matching
    # harness so a typo cannot silently grant a different permission mode.
    for key, required_harness in (
        ("codex_full_auto", "codex"),
        ("claude_dangerously_skip_permissions", "claude"),
    ):
        if key not in config:
            continue
        value = config[key]
        if not isinstance(value, bool):
            errors.append(f"Config '{key}' must be a boolean, got {type(value).__name__}")
        elif value and config.get("harness") != required_harness:
            errors.append(f"Config '{key}: true' requires harness '{required_harness}'")

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

    # --- agent_name --- retired with the ACPX runtime.  Rejected rather
    # than ignored: a profile still carrying it was written for a dispatch
    # path that no longer exists, and silently dropping the key would leave
    # the author believing agent selection still happens here instead of
    # through ``harness``.
    if "agent_name" in config:
        errors.append(
            "Config 'agent_name' was removed with the 'acpx' runtime. "
            "Select the agent with 'harness' instead "
            '("claude", "codex", "gemini").'
        )

    # --- runtime --- retired with the in-process Supervisor (llm-direct-path L6).
    if "runtime" in config:
        errors.append(
            "Config 'runtime' was removed; every agent runs as a tmux session. "
            'Select the CLI with \'harness\' ("claude", "codex", "gemini").'
        )

    if "default_class" in config:
        v = config["default_class"]
        if not isinstance(v, str):
            errors.append(f"Config 'default_class' must be a string, got {type(v).__name__}")

    if "needs_workspace" in config:
        v = config["needs_workspace"]
        if not isinstance(v, bool):
            errors.append(f"Config 'needs_workspace' must be a boolean, got {type(v).__name__}")

    if "read_only" in config:
        v = config["read_only"]
        if not isinstance(v, bool):
            errors.append(f"Config 'read_only' must be a boolean, got {type(v).__name__}")

    if "allow_base_checkout" in config:
        v = config["allow_base_checkout"]
        if not isinstance(v, bool):
            errors.append(
                f"Config 'allow_base_checkout' must be a boolean, got {type(v).__name__}"
            )

    errors.extend(_validate_session_config(config))

    return errors


def _validate_session_config(config: dict) -> list[str]:
    """Validate the named-session ``## Config`` keys (supervisor-agent §7).

    - **harness** — any non-empty string.  Existence is *not* checked here:
      the harness registry (``vault/harnesses/``) is owned by the
      session-runtime spec, and profiles are allowed to land before their
      harness does.  Sync emits a warning instead.
    - **lifecycle** — :data:`VALID_LIFECYCLES`; defaults to ``"task"``.
    - **mode** / **wake_mode** / **idle_timeout** — only valid with
      ``lifecycle: named``; a parse error otherwise, so a typo'd lifecycle
      can't silently strand a session config.
    - **max_session_age** — positive integer seconds, named lifecycle only.
    - **workspaces** — list of workspace-kind ids (strings).
    """
    errors: list[str] = []

    # --- lifecycle --- resolved first; the rest key off it.
    lifecycle = "task"
    if "lifecycle" in config:
        raw = config["lifecycle"]
        if not isinstance(raw, str):
            errors.append(f"Config 'lifecycle' must be a string, got {type(raw).__name__}")
        elif raw not in VALID_LIFECYCLES:
            errors.append(
                f"Config 'lifecycle' must be one of {sorted(VALID_LIFECYCLES)}, got '{raw}'"
            )
        else:
            lifecycle = raw

    # --- harness --- opaque string; the schema belongs to session-runtime.
    if "harness" in config:
        harness = config["harness"]
        if not isinstance(harness, str):
            errors.append(f"Config 'harness' must be a string, got {type(harness).__name__}")
        elif not harness.strip():
            errors.append("Config 'harness' must not be empty")

    named_only_enums = (
        ("mode", VALID_MODES),
        ("wake_mode", VALID_WAKE_MODES),
    )
    for key, valid in named_only_enums:
        if key not in config:
            continue
        value = config[key]
        if not isinstance(value, str):
            errors.append(f"Config '{key}' must be a string, got {type(value).__name__}")
            continue
        if value not in valid:
            errors.append(f"Config '{key}' must be one of {sorted(valid)}, got '{value}'")
            continue
        if lifecycle != "named":
            errors.append(
                f"Config '{key}' is only valid with lifecycle 'named' "
                f"(this profile's lifecycle is '{lifecycle}')"
            )

    for key in ("idle_timeout", "max_session_age"):
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(
                f"Config '{key}' must be a positive integer (seconds), got {type(value).__name__}"
            )
            continue
        if value <= 0:
            errors.append(f"Config '{key}' must be positive, got {value}")
            continue
        if lifecycle != "named":
            errors.append(
                f"Config '{key}' is only valid with lifecycle 'named' "
                f"(this profile's lifecycle is '{lifecycle}')"
            )

    # --- workspaces --- declared attachments; resolved by session-runtime.
    if "workspaces" in config:
        workspaces = config["workspaces"]
        if not isinstance(workspaces, list):
            errors.append(
                f"Config 'workspaces' must be a list of workspace-kind ids, "
                f"got {type(workspaces).__name__}"
            )
        else:
            for entry in workspaces:
                if not isinstance(entry, str) or not entry.strip():
                    errors.append(
                        f"Config 'workspaces' entries must be non-empty strings, got {entry!r}"
                    )

    # Pool-only sizing keys (swarm-work-model §9).  NULL/absent = unlimited
    # for max_claims_per_session; 0 is a parse error everywhere.
    for key in ("min_active", "max_active", "max_claims_per_session"):
        if key not in config:
            continue
        value = config[key]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"Config '{key}' must be an integer, got {type(value).__name__}")
            continue
        if key == "min_active":
            if value < 0:
                errors.append(f"Config 'min_active' must be >= 0, got {value}")
        elif value <= 0:
            errors.append(f"Config '{key}' must be positive (omit it for unlimited), got {value}")
        if lifecycle != "pool":
            errors.append(
                f"Config '{key}' is only valid with lifecycle 'pool' "
                f"(this profile's lifecycle is '{lifecycle}')"
            )

    return errors


# Known keys in the Tools block.
TOOLS_KNOWN_KEYS = frozenset({"allowed", "denied"})

# Embedded ``agent-queue`` MCP server prefix.  Tool names in
# ``## Tools.allowed`` may legacy-include this prefix; the parser strips it at
# sync time so the DB stores canonical bare names.  See
# ``docs/specs/design/profiles.md`` (Tool naming).
_AQ_PREFIX = "mcp__agent-queue__"


#: Keys required in the ``## Capabilities`` block.  All three are required
#: when the block is present: "you forgot" and "you meant none" must not
#: look alike.
CAPABILITY_KEYS = frozenset(NAMESPACES)


def _validate_capabilities(caps: dict) -> list[str]:
    """Validate the ``## Capabilities`` block (Playbook V2 Package 0 §3.2)."""
    errors: list[str] = []
    if not isinstance(caps, dict):
        return [f"## Capabilities JSON must be an object, got {type(caps).__name__}"]

    missing = sorted(CAPABILITY_KEYS - set(caps))
    for key in missing:
        errors.append(
            f"Capabilities: '{key}' is required — an omitted namespace is an "
            "error, not an implicit empty one"
        )
    for key in sorted(set(caps) - CAPABILITY_KEYS):
        errors.append(
            f"Capabilities: unknown key '{key}' (expected "
            f"{', '.join(sorted(CAPABILITY_KEYS))})"
        )

    for key in sorted(CAPABILITY_KEYS & set(caps)):
        value = caps[key]
        if not isinstance(value, list):
            errors.append(
                f"Capabilities '{key}' must be an array, got {type(value).__name__}"
            )
            continue
        for i, item in enumerate(value):
            if not isinstance(item, str):
                errors.append(
                    f"Capabilities {key}[{i}] must be a string, got {type(item).__name__}"
                )
            elif not item.strip():
                errors.append(f"Capabilities {key}[{i}] must not be empty")
            elif any(ch in item for ch in WILDCARD_CHARS):
                errors.append(
                    f"Capabilities {key}[{i}] {item!r} contains a wildcard; "
                    "wildcard capabilities are prohibited — list every name explicitly"
                )

    harness = caps.get("harness_tools")
    aq = caps.get("aq_commands")
    if isinstance(harness, list) and isinstance(aq, list) and not harness and aq:
        errors.append(
            "Capabilities: aq_commands are unreachable because harness_tools is "
            "empty — a session needs Bash to run the aq CLI"
        )
    return errors


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
                elif any(ch in item for ch in WILDCARD_CHARS):
                    # Playbook V2 Package 0 §3.2: wildcard capabilities are
                    # prohibited, and "*" used to mean "everything" here.
                    errors.append(
                        f"Tools allowed[{i}] {item!r} contains a wildcard; "
                        "wildcard capabilities are prohibited — list every name "
                        "explicitly, or migrate the file to a ## Capabilities block"
                    )
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
                elif any(ch in item for ch in WILDCARD_CHARS):
                    # Playbook V2 Package 0 §3.2: wildcard capabilities are
                    # prohibited, and "*" used to mean "everything" here.
                    errors.append(
                        f"Tools denied[{i}] {item!r} contains a wildcard; "
                        "wildcard capabilities are prohibited — list every name "
                        "explicitly, or migrate the file to a ## Capabilities block"
                    )
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
    ... {"default_class": "standard-medium"}
    ... ```
    ... ''')
    >>> result.is_valid
    True
    >>> result.config
    {'default_class': 'standard-medium'}
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
        elif heading_lower == "capabilities" and section.json_data is not None:
            cap_errors = _validate_capabilities(section.json_data)
            result.errors.extend(cap_errors)
            if not cap_errors:
                result.capabilities = {
                    ns: list(section.json_data[ns]) for ns in NAMESPACES
                }
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

    # Rule 4 (§3.2): the two shapes must not coexist.  An operator migrates
    # deliberately — silently preferring one would leave the other looking
    # enforced when it is not.
    if result.capabilities is not None and "tools" in result.sections:
        result.errors.append(
            "Profile declares both '## Capabilities' and '## Tools'; remove "
            "'## Tools' — capabilities supersede it"
        )

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

    # Config → permission_mode
    if parsed.config.get("permission_mode"):
        result["permission_mode"] = parsed.config["permission_mode"]
    for key in ("codex_full_auto", "claude_dangerously_skip_permissions"):
        if key in parsed.config:
            result[key] = parsed.config[key]

    if "default_class" in parsed.config:
        result["default_class"] = parsed.config["default_class"]
    if "needs_workspace" in parsed.config:
        result["needs_workspace"] = bool(parsed.config["needs_workspace"])
    if "read_only" in parsed.config:
        result["read_only"] = bool(parsed.config["read_only"])
    if "allow_base_checkout" in parsed.config:
        result["allow_base_checkout"] = bool(parsed.config["allow_base_checkout"])

    # Config → named-session fields (supervisor-agent §7).  Pass-through
    # storage; validated above, interpreted by the session runtime.
    for key in ("harness", "lifecycle", "mode", "wake_mode"):
        if parsed.config.get(key):
            result[key] = parsed.config[key]
    for key in ("idle_timeout", "max_session_age"):
        value = parsed.config.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
    for key in ("min_active", "max_active", "max_claims_per_session"):
        value = parsed.config.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
    # ``workspaces`` has no agent_profiles column — session-runtime owns
    # attachment resolution.  Surfaced for callers that read the parsed
    # profile directly (it is dropped on the way to the DB row).
    if parsed.config.get("workspaces"):
        result["workspaces"] = list(parsed.config["workspaces"])

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

    # Capabilities → the three normalized namespaces.  Present block wins
    # outright: ``allowed_tools`` is not consulted once they are authored.
    if parsed.capabilities is not None:
        for ns in NAMESPACES:
            result[ns] = [
                t[len(_AQ_PREFIX) :] if t.startswith(_AQ_PREFIX) else t
                for t in parsed.capabilities.get(ns, [])
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
    permission_mode: str = "",
    harness: str | None = None,
    codex_full_auto: bool = False,
    claude_dangerously_skip_permissions: bool = False,
    allowed_tools: list[str] | None = None,
    mcp_servers: list[str] | dict[str, dict] | None = None,
    system_prompt_suffix: str = "",
    install: dict | None = None,
    role: str = "",
    rules: str = "",
    reflection: str = "",
    tags: list[str] | None = None,
    default_class: str = "",
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
    permission_mode:
        Permission mode override (empty = use default).
    harness:
        CLI harness id. Required when either provider-specific autonomous
        permission opt-in is enabled.
    codex_full_auto:
        Whether Codex should run with its sandboxed ``--full-auto`` mode.
    claude_dangerously_skip_permissions:
        Whether Claude should skip permission prompts.
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
    for key, value in (
        ("codex_full_auto", codex_full_auto),
        ("claude_dangerously_skip_permissions", claude_dangerously_skip_permissions),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean, got {type(value).__name__}")

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
    # ``permission_mode: bypassPermissions`` remains accepted on input for
    # every harness.  For Claude it names the same behavior as the canonical
    # provider-specific boolean, so writers converge old profiles on one
    # representation.  Codex keeps the legacy value because it selects the
    # stronger approvals-and-sandbox bypass, not ``--full-auto``.
    if harness == "claude" and permission_mode == "bypassPermissions":
        permission_mode = ""
        claude_dangerously_skip_permissions = True
    if harness:
        config["harness"] = harness
    if permission_mode:
        config["permission_mode"] = permission_mode
    if codex_full_auto:
        config["codex_full_auto"] = True
    if claude_dangerously_skip_permissions:
        config["claude_dangerously_skip_permissions"] = True
    if default_class:
        config["default_class"] = default_class
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


def set_frontmatter_id(markdown: str, profile_id: str) -> str:
    """Return *markdown* with its frontmatter ``id`` set to *profile_id*.

    Used when seeding a project-scoped override from a system profile:
    :func:`~src.profiles.sync.sync_profile_to_db` resolves the row id as
    *frontmatter id > fallback_id*, so an override copied verbatim from the
    system file would otherwise upsert the **system** row.

    Frontmatter is created when the document has none.  Every other line is
    preserved byte for byte.
    """
    _, remaining = parse_frontmatter(markdown)
    if remaining == markdown:
        # No frontmatter at all — prepend a minimal block.
        return f"---\nid: {profile_id}\n---\n\n{markdown.lstrip()}"

    stripped = markdown.lstrip()
    after_open = stripped[3:].lstrip("\r\n")
    close_idx = after_open.find("\n---")
    yaml_text = after_open[:close_idx]

    lines = yaml_text.splitlines()
    out, replaced = [], False
    for line in lines:
        if re.match(r"^id\s*:", line):
            if not replaced:
                out.append(f"id: {profile_id}")
                replaced = True
            continue  # drop any duplicate id keys
        out.append(line)
    if not replaced:
        out.insert(0, f"id: {profile_id}")

    # Drop exactly one newline after the closing delimiter (the delimiter's
    # own line break), preserving any blank line the author put there.
    rest = after_open[close_idx + 4 :]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    return "---\n" + "\n".join(out) + "\n---\n" + rest


def update_config_keys(markdown: str, updates: dict) -> str:
    """Return *markdown* with *updates* merged into its ``## Config`` JSON block.

    A surgical rewrite: only the ``## Config`` fenced JSON object is replaced,
    so Role/Rules/Tools/MCP Servers prose and every other section survive
    untouched.  This is what makes the vault — not the ``agent_profiles`` row —
    the source of truth for values a command mutates (swarm spec §14): a
    whole-document re-render via :func:`agent_profile_to_markdown` would drop
    any section it has no parameter for.

    When the document has no ``## Config`` section, one is appended.  Keys
    whose value is ``None`` are removed from the block.
    """
    frontmatter_text = ""
    body = markdown
    _, remaining = parse_frontmatter(markdown)
    if remaining != markdown:
        frontmatter_text = markdown[: len(markdown) - len(remaining)]
        body = remaining

    def _render(config: dict) -> str:
        return "```json\n" + json.dumps(config, indent=2) + "\n```"

    sections = _split_sections(body)
    rebuilt: list[str] = []
    found = False
    for heading, section_body in sections:
        if heading.strip().lower() != "config":
            if heading:
                rebuilt.append(f"## {heading}\n{section_body}")
            elif section_body:
                rebuilt.append(section_body)
            continue

        found = True
        json_str, _ = _extract_json_block(section_body)
        try:
            config = json.loads(json_str) if json_str else {}
        except json.JSONDecodeError:
            config = {}
        if not isinstance(config, dict):
            config = {}
        for key, value in updates.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        rebuilt.append(f"## Config\n\n{_render(config)}\n\n")

    result = "".join(rebuilt)
    if not found:
        config = {k: v for k, v in updates.items() if v is not None}
        if config:
            if result and not result.endswith("\n"):
                result += "\n"
            result = result.rstrip("\n") + f"\n\n## Config\n\n{_render(config)}\n"

    return frontmatter_text + result
