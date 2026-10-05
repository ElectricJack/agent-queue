"""``discord.config``: the single-channel Discord config of spec 2026-10-03 §1.2/§7.1.

The check reports retired blocks (``discord.channels``,
``discord.per_project_channels``) and each of ``discord.channel_id``,
``guild_id``, ``authorized_users`` and ``project_id`` that is missing or
wrong.  The fix writes only what it can derive, in place, and never invents a
guild or an allowlist.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from src.config import DiscordConfig
from src.doctor import default_registry
from src.doctor.discord_config_checks import (
    CANONICAL_KEYS,
    ID,
    OWNER,
    discord_config_checks,
)
from src.doctor.models import DoctorContext, Severity
from src.doctor.runner import apply_fix

CHANNEL = "1234567890123456789"
GUILD = "987654321098765432"
USER = "111111111111111111"

_LEGACY = """\
# operator comment outside the section
dashboard:
  enabled: true

discord:
  bot_token: ${DISCORD_BOT_TOKEN}  # kept as a reference, never resolved
  guild_id: '987654321098765432'
  channels:
    channel: aq-control
    agent_questions: agent-questions
  per_project_channels:
    auto_create: false
  digest:
    enabled: true
"""

_MIGRATED = f"""\
discord:
  bot_token: ${{DISCORD_BOT_TOKEN}}
  channel_id: '{CHANNEL}'
  guild_id: '{GUILD}'
  authorized_users:
  - '{USER}'
  project_id: agent-queue
"""


@pytest.fixture(autouse=True)
def _pg_backend():
    """These checks read a config file and in-memory state only; no test database."""


def _live(**overrides: Any) -> DiscordConfig:
    values: dict[str, Any] = {
        "bot_token": "token",
        "guild_id": GUILD,
        "authorized_users": [USER],
        "channel_id": "",
    }
    values.update(overrides)
    return DiscordConfig(**values)


def _ctx(
    path,
    *,
    live: DiscordConfig | None = None,
    cutover: str | None = None,
    bot: bool = True,
    projects: list[str] | None = ("agent-queue",),  # type: ignore[assignment]
) -> DoctorContext:
    """``live`` is what the daemon holds in memory; with ``bot`` it is the bot's config."""
    live = live if live is not None else _live()
    config = SimpleNamespace(_config_path=str(path), discord=live)
    handler = None
    if bot:
        discord_bot = SimpleNamespace(
            config=SimpleNamespace(discord=live),
            _cutover_report=SimpleNamespace(status=cutover) if cutover else None,
        )
        handler = SimpleNamespace(orchestrator=SimpleNamespace(_discord_bot=discord_bot))
    db = None
    if projects is not None:

        async def list_projects():
            return [SimpleNamespace(id=project) for project in projects]

        db = SimpleNamespace(list_projects=list_projects)
    return DoctorContext(config=config, db=db, handler=handler)  # type: ignore[arg-type]


def _check():
    return next(c for c in discord_config_checks() if c.id == ID)


def _run(ctx):
    return asyncio.run(_check().run(ctx))


def _fix(ctx):
    return asyncio.run(_check().fix(ctx))


def _section(path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("discord") or {}


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_LEGACY, encoding="utf-8")
    return path


@pytest.fixture
def migrated(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_MIGRATED, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_a_migrated_config_is_ok(migrated):
    result = _run(_ctx(migrated, live=_live(channel_id=CHANNEL, project_id="agent-queue")))

    assert result.severity is Severity.OK, result.detail
    assert result.data["retired_present"] == []


def test_no_discord_section_or_no_file_is_info(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("dashboard:\n  enabled: true\n", encoding="utf-8")

    assert _run(_ctx(path)).severity is Severity.INFO
    assert _run(_ctx(tmp_path / "missing.yaml")).severity is Severity.INFO


def test_the_legacy_box_shape_names_every_gap(legacy):
    # This box before migration: channels + per_project_channels + guild_id, and
    # the cutover promoted aq-control to an ID in memory only.
    result = _run(_ctx(legacy, live=_live(channel_id=CHANNEL), cutover="complete"))

    assert result.severity is Severity.WARN
    assert sorted(result.data["retired_present"]) == ["channels", "per_project_channels"]
    assert set(result.data["fix_sets"]) == {"channel_id", "project_id"}
    assert set(result.data["fix_removes"]) == {"channels", "per_project_channels"}
    assert result.data["manual"] == []
    assert f"resolved the legacy channel name to {CHANNEL}" in result.detail


def test_guild_and_allowlist_are_left_to_the_operator(legacy):
    live = _live(channel_id=CHANNEL, guild_id="", authorized_users=[])
    result = _run(_ctx(legacy, live=live, cutover="complete"))

    assert result.severity is Severity.WARN
    assert {"guild_id", "authorized_users"} <= set(result.data["manual"])
    assert "needs the operator" in result.detail
    assert "conversations stay off" in result.detail


def test_a_channel_name_in_channel_id_is_flagged(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_MIGRATED.replace(f"'{CHANNEL}'", "aq-control"), encoding="utf-8")

    result = _run(_ctx(path, live=_live(channel_id="aq-control", project_id="agent-queue")))

    assert result.severity is Severity.WARN
    assert result.data["manual"] == ["channel_id"]
    assert "not a numeric Discord channel ID" in result.detail


def test_without_any_channel_id_the_operator_must_pick_one(legacy):
    result = _run(_ctx(legacy, cutover="needs_configuration"))

    assert result.severity is Severity.WARN
    assert {"channel_id", "channels"} <= set(result.data["manual"])
    assert "channel_id" not in result.data["fix_sets"]
    assert "channels" not in result.data["fix_removes"]


def test_project_id_with_several_projects_must_be_chosen(migrated):
    path = migrated
    path.write_text(_MIGRATED.replace("  project_id: agent-queue\n", ""), encoding="utf-8")

    result = _run(_ctx(path, live=_live(channel_id=CHANNEL), projects=["a", "b"]))

    assert result.severity is Severity.WARN
    assert result.data["manual"] == ["project_id"]
    assert "stays queued" in result.detail
    assert "a, b" in result.detail


def test_project_id_naming_no_project_is_flagged(migrated):
    live = _live(channel_id=CHANNEL, project_id="gone")
    migrated.write_text(_MIGRATED.replace("agent-queue", "gone"), encoding="utf-8")

    result = _run(_ctx(migrated, live=live, projects=["agent-queue"]))

    assert result.severity is Severity.WARN
    assert result.data["manual"] == ["project_id"]
    assert "names no project" in result.detail


@pytest.mark.parametrize(
    ("written", "live", "flagged"),
    [
        ("gone", "agent-queue", True),  # edited in the file, not yet restarted
        ("agent-queue", "gone", False),
        ("${AQ_DISCORD_PROJECT}", "gone", True),  # a reference: the live value decides
        ("${AQ_DISCORD_PROJECT}", "agent-queue", False),
    ],
)
def test_project_id_is_judged_by_what_the_file_sets(migrated, written, live, flagged):
    migrated.write_text(_MIGRATED.replace("project_id: agent-queue", f"project_id: {written}"))

    result = _run(_ctx(migrated, live=_live(channel_id=CHANNEL, project_id=live)))

    assert (result.data["manual"] == ["project_id"]) is flagged, result.detail


def test_an_unreachable_database_skips_only_project_id(migrated):
    live = _live(channel_id=CHANNEL, project_id="agent-queue")

    assert _run(_ctx(migrated, live=live, projects=None)).severity is Severity.OK


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------


def test_the_fix_migrates_the_legacy_box_shape_in_place(legacy):
    ctx = _ctx(legacy, live=_live(channel_id=CHANNEL), cutover="complete")

    result = _fix(ctx)

    assert result.severity is Severity.OK, result.detail
    text = legacy.read_text(encoding="utf-8")
    section = _section(legacy)
    assert "channels" not in section and "per_project_channels" not in section
    assert section["channel_id"] == CHANNEL
    assert section["project_id"] == "agent-queue"
    # Never invented: the allowlist is written empty for the operator to fill.
    assert section["authorized_users"] == []
    # Untouched keys keep their value, ${ENV} reference, comments and order.
    assert section["guild_id"] == GUILD
    assert section["digest"] == {"enabled": True}
    assert "bot_token: ${DISCORD_BOT_TOKEN}  # kept as a reference, never resolved" in text
    assert "# operator comment outside the section" in text
    assert text.index("bot_token") < text.index("guild_id") < text.index("digest")
    # The written ID stays a string: YAML would read a bare 19-digit number as an int.
    assert isinstance(section["channel_id"], str)


def test_the_fix_then_check_is_clean_and_a_second_fix_is_a_noop(legacy):
    ctx = _ctx(legacy, live=_live(channel_id=CHANNEL, project_id=""), cutover="complete")

    post = asyncio.run(apply_fix(_check(), ctx))
    # The daemon reads the file only at restart; until then the check must judge
    # what the fix wrote, not the stale in-memory value.
    after = legacy.read_text(encoding="utf-8")

    assert post.fix_applied is True
    assert _run(ctx).severity is Severity.OK
    assert _fix(ctx).severity is Severity.INFO
    assert legacy.read_text(encoding="utf-8") == after


@pytest.mark.parametrize("cutover", [None, "needs_configuration", "failed"])
def test_the_fix_keeps_channels_until_the_cutover_completes(legacy, cutover):
    ctx = _ctx(legacy, live=_live(channel_id=CHANNEL), cutover=cutover)

    check = _run(ctx)
    _fix(ctx)

    section = _section(legacy)
    assert "channels" in check.data["manual"]
    assert "has not completed" in check.detail
    assert "channels" in section  # the cutover still needs the names
    assert "per_project_channels" not in section  # nothing reads this one
    assert section["channel_id"] == CHANNEL


def test_the_fix_drops_channels_when_no_bot_is_configured(legacy):
    ctx = _ctx(legacy, live=_live(bot_token=""), bot=False)

    _fix(ctx)

    section = _section(legacy)
    assert "channels" not in section and "per_project_channels" not in section
    assert section["channel_id"] == ""  # nothing to derive, written empty


def test_the_fix_writes_nothing_when_nothing_is_derivable(legacy):
    ctx = _ctx(legacy, cutover="needs_configuration", projects=["a", "b"])
    legacy.write_text(_LEGACY.replace("  per_project_channels:\n    auto_create: false\n", ""))
    before = legacy.read_text(encoding="utf-8")

    result = _fix(ctx)

    assert result.severity is Severity.INFO
    assert legacy.read_text(encoding="utf-8") == before


def test_the_fix_never_overwrites_a_written_value(migrated):
    # The file names an ${ENV} channel; the live (resolved) value differs in form.
    migrated.write_text(_MIGRATED.replace(f"'{CHANNEL}'", "${DISCORD_CHANNEL_ID}"))
    migrated.write_text(migrated.read_text() + "  per_project_channels: true\n")
    ctx = _ctx(migrated, live=_live(channel_id=CHANNEL, project_id="agent-queue"))

    _fix(ctx)

    section = _section(migrated)
    assert section["channel_id"] == "${DISCORD_CHANNEL_ID}"
    assert "per_project_channels" not in section


def test_every_canonical_key_is_present_after_any_fix(legacy):
    _fix(_ctx(legacy, live=_live(channel_id=CHANNEL), cutover="complete"))

    assert {key for key, _ in CANONICAL_KEYS} <= set(_section(legacy))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_it_is_registered_with_a_fix_under_its_owner():
    check = next((c for c in default_registry().checks() if c.id == ID), None)

    assert check is not None
    assert check.owner == OWNER == "discord"
    assert check.fix is not None
