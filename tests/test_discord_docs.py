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

SPEC_IN_REPO = DOCS / "superpowers" / "specs" / "2026-09-08-discord-simplification-implementation.md"


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
