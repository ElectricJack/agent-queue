"""``discord.legacy_config``: WARN while the retired channel block/flag survive; the
fixer retires only those two keys, writes the canonical four, refuses when
``discord.channel_id`` is absent, and is idempotent (2026-10-03 spec §7.1)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import yaml

from src.doctor import default_registry
from src.doctor.discord_config_checks import (
    CANONICAL_KEYS,
    ID,
    OWNER,
    RETIRED_KEYS,
    discord_config_checks,
)
from src.doctor.models import Severity

# ---------------------------------------------------------------------------
# Fixtures: a real config file on disk, and a context that names it
# ---------------------------------------------------------------------------

_CONFIG_TEMPLATE = """
# the operator's own comment, outside the discord: section, must survive
dashboard:
  enabled: true

discord:
  bot_token: ${DISCORD_BOT_TOKEN}
  """


@pytest.fixture(autouse=True)
def _pg_backend(monkeypatch):
    """These checks never touch the database; keep the import-time backend from
    reaching for it (same convention as the sibling dashboard-server tests)."""
    del monkeypatch


def _write_config(path, *, channel_id: str = "", extra: dict | None = None) -> None:
    document = yaml.safe_load(_CONFIG_TEMPLATE) or {}
    discord = document.setdefault("discord", {})
    if channel_id:
        discord["channel_id"] = channel_id
    if extra is not None:
        discord.update(extra)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _ctx(tmp_path, section_file) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(_config_path=str(section_file)),
        db=None,
        handler=None,
    )


def _run(checks, ctx):
    check = next(c for c in checks if c.id == ID)
    return asyncio.run(check.run(ctx))


def _fix(checks, ctx):
    check = next(c for c in checks if c.id == ID)
    assert check.fix is not None
    return asyncio.run(check.fix(ctx))


def _read_discord(section_file) -> dict:
    document = yaml.safe_load(section_file.read_text(encoding="utf-8")) or {}
    return document.get("discord", {})


# ---------------------------------------------------------------------------
# What the check reports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"channels": {"general": 123456789, "agent_questions": 987654321}, "per_project_channels": True},
        {"channels": {"ops": 123456789}},
        {"per_project_channels": False},
    ],
)
def test_it_warns_and_offers_a_fix_when_a_retired_key_is_present(tmp_path, extra):
    section_file = tmp_path / "config.yaml"
    _write_config(section_file, channel_id="1234567890123456789", extra=extra)

    result = _run(discord_config_checks(), _ctx(tmp_path, section_file))

    assert result.severity is Severity.WARN
    assert result.fixable is True
    assert sorted(result.data["retired_present"]) == sorted(k for k in RETIRED_KEYS if k in extra)
    assert "discord.channel_id" in result.detail


def test_it_stays_fixable_but_refuses_without_a_channel_id(tmp_path):
    section_file = tmp_path / "config.yaml"
    # Retired keys present, channel_id absent: the fix cannot drop them.
    _write_config(section_file, extra={"channels": {"general": 123456789}})

    result = _run(discord_config_checks(), _ctx(tmp_path, section_file))

    assert result.severity is Severity.WARN
    assert result.fixable is True
    assert "discord.channel_id" in result.detail
    assert "not set" in result.detail


def test_it_is_ok_when_no_retired_key_is_present(tmp_path):
    section_file = tmp_path / "config.yaml"
    _write_config(
        section_file,
        channel_id="1234567890123456789",
        extra={"guild_id": "1", "authorized_users": [1], "project_id": "p"},
    )

    result = _run(discord_config_checks(), _ctx(tmp_path, section_file))

    assert result.severity is Severity.OK
    assert result.data["retired_present"] == []


def test_it_is_info_when_there_is_no_discord_section(tmp_path):
    section_file = tmp_path / "config.yaml"
    section_file.write_text("dashboard:\n  enabled: true\n", encoding="utf-8")

    result = _run(discord_config_checks(), _ctx(tmp_path, section_file))

    assert result.severity is Severity.INFO
    assert "no discord: section" in result.detail


def test_it_is_info_when_the_config_file_is_absent(tmp_path):
    result = _run(discord_config_checks(), _ctx(tmp_path, tmp_path / "missing.yaml"))
    assert result.severity is Severity.INFO


# ---------------------------------------------------------------------------
# What the fix does
# ---------------------------------------------------------------------------


def test_the_fix_retires_the_legacy_keys_and_writes_the_canonical_four(tmp_path):
    section_file = tmp_path / "config.yaml"
    _write_config(
        section_file,
        channel_id="1234567890123456789",
        extra={
            "channels": {"general": 123456789, "agent_questions": 987654321},
            "per_project_channels": True,
            "guild_id": "777",
        },
    )

    result = _fix(discord_config_checks(), _ctx(tmp_path, section_file))

    assert result.severity is Severity.OK
    assert set(result.data["removed"]) == {"channels", "per_project_channels"}

    on_disk = _read_discord(section_file)
    for key in RETIRED_KEYS:
        assert key not in on_disk
    for key, _default in CANONICAL_KEYS:
        assert key in on_disk
    # The fix never invents or clobbers an existing value.
    assert on_disk["channel_id"] == "1234567890123456789"
    assert on_disk["guild_id"] == "777"
    # Untouched values and other sections survive the round-trip.
    assert on_disk["bot_token"] == "${DISCORD_BOT_TOKEN}"


def test_the_fix_refuses_to_drop_the_legacy_block_without_a_channel_id(tmp_path):
    section_file = tmp_path / "config.yaml"
    _write_config(section_file, extra={"channels": {"general": 123456789}})

    result = _fix(discord_config_checks(), _ctx(tmp_path, section_file))

    assert result.severity is Severity.WARN
    on_disk = _read_discord(section_file)
    assert "channels" in on_disk  # untouched: nothing was dropped


def test_the_fix_is_noop_when_nothing_is_retired(tmp_path):
    section_file = tmp_path / "config.yaml"
    _write_config(section_file, channel_id="1234567890123456789")
    before = section_file.read_text(encoding="utf-8")

    result = _fix(discord_config_checks(), _ctx(tmp_path, section_file))

    assert result.severity is Severity.INFO
    assert section_file.read_text(encoding="utf-8") == before


def test_the_fix_is_idempotent_across_a_second_run(tmp_path):
    section_file = tmp_path / "config.yaml"
    _write_config(
        section_file,
        channel_id="1234567890123456789",
        extra={"channels": {"general": 123456789}, "per_project_channels": True},
    )

    first = _fix(discord_config_checks(), _ctx(tmp_path, section_file))
    second = _fix(discord_config_checks(), _ctx(tmp_path, section_file))

    assert first.severity is Severity.OK
    assert second.severity is Severity.INFO
    after_first = section_file.read_text(encoding="utf-8")
    assert section_file.read_text(encoding="utf-8") == after_first


# ---------------------------------------------------------------------------
# Registration: id, owner, and the default registry
# ---------------------------------------------------------------------------


def test_its_contract_matches_what_doctor_registers():
    checks = discord_config_checks()
    assert len(checks) == 1
    assert checks[0].id == ID
    assert checks[0].owner == OWNER
    assert checks[0].fix is not None


def test_it_is_registered_by_the_default_registry():
    check = next((c for c in default_registry().checks() if c.id == ID), None)
    assert check is not None
