"""Harness markdown parser — a CLI agent is a file, not a Python module.

``vault/harnesses/<name>.md`` describes one coding-agent CLI: how to invoke
it, how the bootstrap prompt is delivered, how to resume it, what "ready"
looks like, which processes belong to it, and which startup dialogs to
dismiss.  Adding Codex or Gemini is a markdown file; it is never a runtime
module (design §2, goal 2).

Format mirrors ``src/profiles/parser.py`` exactly — YAML frontmatter
(``id``, ``name``, ``tags``) plus a ``## Config`` fenced JSON block::

    ---
    id: claude
    name: Claude Code
    tags: [harness]
    ---

    ## Config

    ```json
    {
      "command": "claude",
      "args": ["--dangerously-skip-permissions"],
      "prompt_mode": "arg",
      "process_names": ["claude", "node"],
      "ready_prompt_prefix": "❯ ",
      "resume": {"style": "flag", "flag": "--resume"}
    }
    ```

    ## Notes

    Free-form prose, not parsed.

Validation is deterministic and refuses rather than guesses: an unknown
``prompt_mode``, an unknown ``resume.style``, a missing ``command``, or a
``base`` chain longer than one level is an error, not a silent default.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from src.profiles.parser import parse_frontmatter
from src.sessions.provider import PROMPT_MODES, DialogRule

logger = logging.getLogger(__name__)

__all__ = [
    "Harness",
    "ParsedHarness",
    "parse_harness_markdown",
    "resolve_base",
    "HARNESS_KNOWN_KEYS",
    "VALID_RESUME_STYLES",
]

#: Recognised ``## Config`` keys.  Unknown keys are a *warning*, not an
#: error: a harness file authored against a newer daemon must still load.
HARNESS_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "base",
        "command",
        "args",
        "prompt_mode",
        "prompt_flag",
        "permission_flag",
        "model_flag",
        "effort_flag",
        "session_id_flag",
        "settings_flag",
        "tools_flag",
        "resume",
        "ready_delay_ms",
        "ready_prompt_prefix",
        "process_names",
        "skip_escape_before_enter",
        "supports_hooks",
        "hook_files",
        "hook_trust_flag",
        "instructions_file",
        "transcript_paths",
        "dialogs",
        "env",
        "max_argv_prompt_bytes",
    }
)

VALID_RESUME_STYLES: frozenset[str] = frozenset({"flag", "subcommand", "none"})

#: Above this many bytes the bootstrap prompt is written to a file and
#: passed by path instead of riding argv.  tmux's ``new-session`` command
#: buffer is roughly 2 KB, so 1 KB is the shipped headroom.
DEFAULT_MAX_ARGV_PROMPT_BYTES = 1024


@dataclass(frozen=True)
class ResumeSpec:
    """How this harness resumes a previous conversation.

    ``style="flag"``       → ``claude --resume <key>``
    ``style="subcommand"`` → ``codex resume <key>``
    ``style="none"``       → no resume support; restarts start fresh.
    """

    style: str = "none"
    flag: str = "--resume"
    subcommand: str = "resume"
    supports_fork: bool = False


@dataclass(frozen=True)
class Harness:
    """One immutable CLI-agent description."""

    id: str
    name: str = ""
    project_id: str | None = None  # None = system scope
    command: str = ""
    args: tuple[str, ...] = ()
    prompt_mode: str = "arg"
    prompt_flag: str = ""  # used when prompt_mode == "flag"
    permission_flag: str = ""
    model_flag: str = ""
    effort_flag: str = ""
    session_id_flag: str = ""
    #: Flag that points the harness at a settings/hooks file, e.g. Claude's
    #: ``--settings``.  Emitted only when ``hook_files`` actually rendered —
    #: without it the hook payload is written and never read.
    settings_flag: str = ""
    #: Flag that restricts the harness's tool surface, e.g. Claude's
    #: ``--allowedTools``.  Emitted only when the profile actually declares
    #: an allowlist; a harness that omits this key cannot be restricted and
    #: the launch says so once rather than silently ignoring the profile.
    tools_flag: str = ""
    resume: ResumeSpec = field(default_factory=ResumeSpec)
    ready_delay_ms: int = 0
    ready_prompt_prefix: str | None = None
    process_names: tuple[str, ...] = ()
    skip_escape_before_enter: bool = True
    supports_hooks: bool = False
    #: ``dest_relative_path -> packaged template name`` written into the
    #: work_dir at spec-build time when ``supports_hooks``.
    hook_files: tuple[tuple[str, str], ...] = ()
    #: Flag a harness needs in order to honour a hook file it has *not*
    #: been shown interactively -- Codex's
    #: ``--dangerously-bypass-hook-trust``.  Emitted only alongside the
    #: permission flag, so it rides the isolated-worktree trust argument in
    #: [[design/trust-and-ops]] §4 rather than widening it: a linked
    #: checkout keeps hook review, and simply reports no native subagents.
    hook_trust_flag: str = ""
    instructions_file: str = ""
    transcript_paths: tuple[str, ...] = ()
    dialogs: tuple[DialogRule, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    max_argv_prompt_bytes: int = DEFAULT_MAX_ARGV_PROMPT_BYTES
    #: The ``base:`` this file declared, before resolution.  Kept so the
    #: registry can resolve inheritance after every file is loaded.
    base: str = ""
    notes: str = ""

    @property
    def env_map(self) -> dict[str, str]:
        return dict(self.env)

    @property
    def scope_key(self) -> tuple[str | None, str]:
        return (self.project_id, self.id)


@dataclass
class ParsedHarness:
    """Result of parsing one harness file."""

    harness: Harness | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors and self.harness is not None


def _config_block(text: str) -> tuple[dict | None, str, list[str]]:
    """Extract the ``## Config`` JSON block and the remaining prose."""
    import re

    errors: list[str] = []
    # Find "## Config" and the first ```json fence after it.
    m = re.search(r"^##\s+Config\s*$", text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return None, text.strip(), ["missing '## Config' section"]
    after = text[m.end() :]
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", after, re.DOTALL)
    if not fence:
        return None, text.strip(), ["'## Config' section has no JSON code block"]
    raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, text.strip(), [f"invalid JSON in '## Config': {exc}"]
    if not isinstance(data, dict):
        return None, text.strip(), ["'## Config' JSON must be an object"]
    notes = (text[: m.start()] + after[fence.end() :]).strip()
    return data, notes, errors


def _parse_dialogs(raw, errors: list[str]) -> tuple[DialogRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append("'dialogs' must be a list of objects")
        return ()
    rules: list[DialogRule] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"dialogs[{i}] must be an object")
            continue
        pattern = entry.get("pattern")
        if not pattern or not isinstance(pattern, str):
            errors.append(f"dialogs[{i}] requires a non-empty string 'pattern'")
            continue
        keys = entry.get("keys", [])
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, list):
            errors.append(f"dialogs[{i}].keys must be a string or list of strings")
            continue
        rules.append(
            DialogRule(
                name=str(entry.get("name") or f"dialog-{i}"),
                pattern=pattern,
                keys=tuple(str(k) for k in keys),
                is_regex=bool(entry.get("is_regex", False)),
                quarantine=bool(entry.get("quarantine", False)),
                once=bool(entry.get("once", True)),
            )
        )
    return tuple(rules)


def _str_tuple(raw, field_name: str, errors: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        errors.append(f"'{field_name}' must be a string or list of strings")
        return ()
    return tuple(raw)


def parse_harness_markdown(
    text: str,
    *,
    fallback_id: str = "",
    project_id: str | None = None,
) -> ParsedHarness:
    """Parse one harness markdown file into a :class:`Harness`.

    *fallback_id* is used when the frontmatter omits ``id`` (the filename
    stem, matching how the MCP registry derives server names from paths).
    ``base`` is recorded but **not** resolved here — resolution needs the
    full set of loaded harnesses and lives in :func:`resolve_base`.
    """
    errors: list[str] = []
    warnings: list[str] = []

    fm, body = parse_frontmatter(text)
    config, notes, cfg_errors = _config_block(body)
    errors.extend(cfg_errors)
    if config is None:
        return ParsedHarness(errors=errors, warnings=warnings)

    hid = (fm.id or fallback_id or "").strip()
    if not hid:
        errors.append("harness needs an 'id' in frontmatter (or a filename to derive it from)")

    for key in config:
        if key not in HARNESS_KNOWN_KEYS:
            warnings.append(f"unknown Config key '{key}' (ignored)")

    base = str(config.get("base") or "").strip()
    command = str(config.get("command") or "").strip()
    if not command and not base:
        errors.append("'command' is required (or inherit one via 'base')")

    prompt_mode = str(config.get("prompt_mode") or "arg")
    if prompt_mode not in PROMPT_MODES:
        errors.append(
            f"invalid prompt_mode '{prompt_mode}' — must be one of {sorted(PROMPT_MODES)}"
        )
    prompt_flag = str(config.get("prompt_flag") or "")
    if prompt_mode == "flag" and not prompt_flag:
        errors.append("prompt_mode 'flag' requires 'prompt_flag'")

    raw_resume = config.get("resume")
    resume = ResumeSpec()
    if raw_resume is not None:
        if not isinstance(raw_resume, dict):
            errors.append("'resume' must be an object")
        else:
            style = str(raw_resume.get("style") or "none")
            if style not in VALID_RESUME_STYLES:
                errors.append(
                    f"invalid resume.style '{style}' — must be one of "
                    f"{sorted(VALID_RESUME_STYLES)}"
                )
            else:
                resume = ResumeSpec(
                    style=style,
                    flag=str(raw_resume.get("flag") or "--resume"),
                    subcommand=str(raw_resume.get("subcommand") or "resume"),
                    supports_fork=bool(raw_resume.get("supports_fork", False)),
                )

    raw_env = config.get("env") or {}
    if not isinstance(raw_env, dict):
        errors.append("'env' must be an object")
        raw_env = {}

    raw_hooks = config.get("hook_files") or {}
    if not isinstance(raw_hooks, dict):
        errors.append("'hook_files' must be an object mapping dest path -> template name")
        raw_hooks = {}

    ready_delay = config.get("ready_delay_ms", 0)
    if not isinstance(ready_delay, int) or isinstance(ready_delay, bool) or ready_delay < 0:
        errors.append("'ready_delay_ms' must be a non-negative integer")
        ready_delay = 0

    max_argv = config.get("max_argv_prompt_bytes", DEFAULT_MAX_ARGV_PROMPT_BYTES)
    if not isinstance(max_argv, int) or isinstance(max_argv, bool) or max_argv <= 0:
        errors.append("'max_argv_prompt_bytes' must be a positive integer")
        max_argv = DEFAULT_MAX_ARGV_PROMPT_BYTES

    dialogs = _parse_dialogs(config.get("dialogs"), errors)

    if errors:
        return ParsedHarness(errors=errors, warnings=warnings)

    harness = Harness(
        id=hid,
        name=fm.name or hid,
        project_id=project_id,
        command=command,
        args=_str_tuple(config.get("args"), "args", errors),
        prompt_mode=prompt_mode,
        prompt_flag=prompt_flag,
        permission_flag=str(config.get("permission_flag") or ""),
        model_flag=str(config.get("model_flag") or ""),
        effort_flag=str(config.get("effort_flag") or ""),
        session_id_flag=str(config.get("session_id_flag") or ""),
        settings_flag=str(config.get("settings_flag") or ""),
        tools_flag=str(config.get("tools_flag") or ""),
        resume=resume,
        ready_delay_ms=ready_delay,
        ready_prompt_prefix=config.get("ready_prompt_prefix") or None,
        process_names=_str_tuple(config.get("process_names"), "process_names", errors),
        skip_escape_before_enter=bool(config.get("skip_escape_before_enter", True)),
        supports_hooks=bool(config.get("supports_hooks", False)),
        hook_files=tuple(sorted((str(k), str(v)) for k, v in raw_hooks.items())),
        hook_trust_flag=str(config.get("hook_trust_flag") or ""),
        instructions_file=str(config.get("instructions_file") or ""),
        transcript_paths=_str_tuple(
            config.get("transcript_paths"), "transcript_paths", errors
        ),
        dialogs=dialogs,
        env=tuple(sorted((str(k), str(v)) for k, v in raw_env.items())),
        max_argv_prompt_bytes=max_argv,
        base=base,
        notes=notes,
    )
    if errors:
        return ParsedHarness(errors=errors, warnings=warnings)
    return ParsedHarness(harness=harness, warnings=warnings)


#: Fields whose *falsy* value means "not set here, inherit from base".
_INHERITABLE = (
    "command",
    "args",
    "prompt_flag",
    "permission_flag",
    "model_flag",
    "effort_flag",
    "session_id_flag",
    "settings_flag",
    "ready_delay_ms",
    "ready_prompt_prefix",
    "process_names",
    "instructions_file",
    "transcript_paths",
    "dialogs",
    "hook_files",
    "hook_trust_flag",
)


def resolve_base(
    harness: Harness,
    lookup,
) -> tuple[Harness, list[str]]:
    """Fold a ``base:`` parent into *harness*.  Single level only.

    *lookup* is ``callable(name) -> Harness | None``.  Returns
    ``(resolved, errors)``.  Inheritance is deliberately one level deep:
    a chain is rejected rather than walked, so a cycle is impossible by
    construction and a harness file is always readable on its own terms.
    """
    if not harness.base:
        return harness, []
    if harness.base == harness.id:
        return harness, [f"harness '{harness.id}' declares itself as its own base"]
    parent = lookup(harness.base)
    if parent is None:
        return harness, [f"harness '{harness.id}': unknown base '{harness.base}'"]
    if parent.base:
        return harness, [
            f"harness '{harness.id}': base '{parent.base}' is itself derived — "
            "harness inheritance is single-level"
        ]

    merged: dict = {}
    for name in _INHERITABLE:
        own = getattr(harness, name)
        merged[name] = own if own else getattr(parent, name)
    # ``env`` merges key-wise (child wins) rather than replacing wholesale:
    # a derived harness adding one variable must not drop the parent's.
    env = dict(parent.env_map)
    env.update(harness.env_map)
    merged["env"] = tuple(sorted(env.items()))
    # Scalars that have meaningful falsy values are taken from the child
    # only when the child actually declared something non-default.
    merged["prompt_mode"] = harness.prompt_mode or parent.prompt_mode
    merged["resume"] = harness.resume if harness.resume.style != "none" else parent.resume
    merged["supports_hooks"] = harness.supports_hooks or parent.supports_hooks
    merged["skip_escape_before_enter"] = harness.skip_escape_before_enter
    merged["max_argv_prompt_bytes"] = (
        harness.max_argv_prompt_bytes or parent.max_argv_prompt_bytes
    )

    import dataclasses

    return dataclasses.replace(harness, **merged), []
