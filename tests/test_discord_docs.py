"""The Discord/escalation operator docs must describe surfaces that exist.

Documentation for a cutover rots faster than anything else in the repository:
the whole point of the change is that half the commands people knew are gone.
These tests hold the operator-facing Discord docs to the surface this checkout
actually has: the settings block must load and validate, the response fields the
runbook tells an operator to read must still be produced, and a retired knob must
not survive in a doc someone would follow.  The invocations *inside* those docs
are checked by ``test_guidance_docs.py``, which resolves every documented
``aq …`` line against the live Click tree.

Nothing here starts a daemon, touches a database or sends a message.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from src.config import DiscordConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

SPEC_IN_REPO = (
    DOCS / "superpowers" / "specs" / "2026-09-08-discord-simplification-implementation.md"
)


def test_migration_guide_yaml_is_a_valid_discord_config() -> None:
    """The runbook's settings block must load and validate as written."""
    text = (DOCS / "guides" / "discord-migration.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)
    assert blocks, "the migration runbook no longer shows a settings block"

    raw = yaml.safe_load(blocks[0])["discord"]
    from src.config import DiscordDigestConfig, DiscordEscalationConfig

    digest = DiscordDigestConfig(**raw.pop("digest"))
    escalation = DiscordEscalationConfig(**raw.pop("escalation"))
    config = DiscordConfig(**raw, digest=digest, escalation=escalation)

    assert config.validate() == [], "the documented Discord settings do not validate"
    assert config.warnings() == [], "the documented Discord settings warn"


def test_documented_digest_status_fields_are_produced() -> None:
    """Field names the runbook tells operators to read must be real."""
    source = (REPO_ROOT / "src" / "commands" / "digest_commands.py").read_text(encoding="utf-8")
    for field in (
        "cutover",
        "settings_errors",
        "warnings",
        "next_evaluation_at",
        "delivery_health",
        "pending_escalation_deliveries",
        "suppression_reason",
        "would_send",
    ):
        assert f'"{field}"' in source, f"digest commands no longer return {field!r}"

    cutover = (REPO_ROOT / "src" / "discord" / "cutover.py").read_text(encoding="utf-8")
    for field in (
        "migrated_questions",
        "migrated_gates",
        "accepted_answers_preserved",
        "adopted_roots",
        "retired_task_threads",
        "inert_messages",
        "conflicts",
    ):
        assert field in cutover, f"the cutover report no longer carries {field!r}"
    assert "needs_configuration" in cutover, "the documented conflict status is gone"


def test_removed_surfaces_are_not_documented_as_current() -> None:
    """A retired knob must not survive in a doc an operator would follow."""
    retired = {
        "auto_create_channels": (DOCS / "specs" / "command-handler.md",),
    }
    for token, paths in retired.items():
        for path in paths:
            assert token not in path.read_text(encoding="utf-8"), (
                f"{path.relative_to(REPO_ROOT)} still documents the removed {token!r}"
            )


def test_implementation_spec_is_published_in_the_repository() -> None:
    """§12: the vault mirror is a convenience; the repository copy is canonical."""
    assert SPEC_IN_REPO.exists(), "the Discord simplification spec is not published in-repo"
    text = SPEC_IN_REPO.read_text(encoding="utf-8")
    assert "vault/projects/agent-queue/specs/discord-simplification-implementation.md" in text, (
        "the published spec no longer names its vault mirror"
    )


def test_migration_runbook_is_reachable_from_the_docs_index() -> None:
    index = (DOCS / "index.md").read_text(encoding="utf-8")
    assert "guides/discord-migration" in index


#: Names of Discord surfaces the simplification removed.  Each one is a knob or
#: tool an operator or agent could try to use if a doc still presented it as
#: something to reach for.
RETIRED_DISCORD_TOKENS = (
    "auto_create_channels",
    "get_project_channels",
    "get_project_for_channel",
    "archive_channels",
    "per_project_channels",
    "channel_overrides",
    "aq-discord.yaml",
)

#: Words that turn a mention of a retired surface into a statement *about* it.
#: A section that names one of the tokens above must also say it is gone --
#: history and migration guidance are allowed to name what they retired, and a
#: menu that still lists it is not.
RETIREMENT_MARKERS = (
    "supersed",
    "removed",
    "retired",
    "retires",
    "no longer",
    "never built",
    "never shipped",
    "ignored",
    "deleted",
    "historical",
    "does not exist",
)


def _current_docs() -> list[Path]:
    """Every doc a reader would follow as current guidance.

    ``docs/superpowers/plans/`` is a dated archive of finished plans, not
    guidance, so it is excluded -- the same reason ``test_guidance_docs.py``
    does not resolve its command lines.
    """
    return [
        p
        for p in sorted(DOCS.rglob("*.md"))
        if "superpowers/plans" not in p.as_posix() and "/archive/" not in p.as_posix()
    ]


def _sections(text: str) -> list[tuple[str, str]]:
    """Heading-delimited blocks as ``(heading, body + ancestor bodies)``.

    A ``> **Superseded**`` banner is written once under the section it covers,
    so a subsection inherits its parents' prose: ``### 3.3`` is retired by the
    banner under ``## 3``, and reading it alone would miss that.
    """
    parts = [p for p in re.split(r"^(?=#{1,6} )", text, flags=re.MULTILINE) if p.strip()]
    out: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []  # (level, body) of the open ancestors
    for part in parts:
        first = part.splitlines()[0]
        match = re.match(r"(#{1,6}) ", first)
        level = len(match.group(1)) if match else 0
        while stack and stack[-1][0] >= level:
            stack.pop()
        out.append((first.strip(), part + "".join(body for _, body in stack)))
        stack.append((level, part))
    return out


def test_retired_discord_surfaces_are_only_named_as_retired() -> None:
    """A retired channel knob or tool must never read as something to use.

    The cutover deleted per-project channels, every project-channel lookup and
    the separate bot process that would have owned them.  Docs are allowed to
    say so -- the migration runbook has to -- but the section doing the saying
    must carry the retirement in its own words.  The scan is over all of
    ``docs/`` rather than a hand-picked pair of files, because the surfaces
    this task had to reconcile were exactly the ones nothing was watching.
    """
    offenders: list[str] = []
    for path in _current_docs():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for heading, section in _sections(path.read_text(encoding="utf-8")):
            lowered = section.lower()
            if any(marker in lowered for marker in RETIREMENT_MARKERS):
                continue
            for token in RETIRED_DISCORD_TOKENS:
                if token in section:
                    offenders.append(f"{rel} -- {heading!r} presents {token!r} as current")
    assert not offenders, "\n".join(offenders)


def _section(text: str, heading: str) -> str:
    """The body of ``heading`` up to the next heading of the same or higher level."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading) :]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def test_retired_slash_commands_are_not_presented_as_surviving() -> None:
    """§4.5 of the design listed six slash commands; the bot registers none."""
    text = (DOCS / "specs" / "design" / "messaging-rework.md").read_text(encoding="utf-8")
    section = _section(text, "### 4.5 ")
    assert "Superseded" in section, "design §4.5 no longer marks the slash commands removed"
    assert "Why it survives" not in section, "design §4.5 still frames the six as surviving"
    assert "Where it lives now" in section, "design §4.5 no longer names the replacements"


def test_the_separate_bot_process_plan_is_marked_never_built() -> None:
    """§4.1–4.2 of the implementation plan described a process that does not exist."""
    text = (DOCS / "specs" / "implementation" / "messaging-rework.md").read_text(encoding="utf-8")
    section = _section(text, "## 4. Configuration")
    assert "Superseded" in section
    assert "never built" in section or "never shipped" in section
    assert "channel_id" in section, "the implemented one-channel setting is not documented"


def test_documented_project_commands_exist() -> None:
    """A tool table is a menu; every entry on it must be orderable.

    ``get_project_for_channel`` outlived the channels it looked up by living in
    a table nothing checked.
    """
    from src.commands.handler import CommandHandler

    live = {n[len("_cmd_") :] for n in dir(CommandHandler) if n.startswith("_cmd_")}
    live |= {n for n in dir(CommandHandler) if not n.startswith("_")}

    # The loop-control stubs the tool registry injects rather than the handler.
    registry_source = (REPO_ROOT / "src" / "tools" / "registry.py").read_text(encoding="utf-8")
    for stub in ("load_tools", "reply_to_user"):
        assert f'"{stub}"' in registry_source, f"the {stub!r} core stub is gone"
    live |= {"load_tools", "reply_to_user"}

    for rel, heading in (
        ("docs/specs/mcp-server.md", "### Project Management"),
        ("docs/guides/agent-tools.md", "## Project Category"),
        ("docs/guides/agent-tools.md", "### Navigation & Response"),
    ):
        section = _section((REPO_ROOT / rel).read_text(encoding="utf-8"), heading)
        documented = re.findall(r"^\| `([a-z_]+)`", section, re.MULTILINE)
        assert documented, f"{rel} no longer has a project tool table"
        missing = sorted(n for n in documented if n not in live)
        assert not missing, f"{rel} documents commands that do not exist: {missing}"
