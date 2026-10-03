"""``discord.legacy_config`` doctor check -- is the retired Discord channel layout gone?

This box's task (2026-10-03 spec §7.1, revision 1): Discord binding moves to a
single ``channel_id`` plus its ``guild_id`` / ``authorized_users`` /
``project_id``; the per-name ``channels`` block (with ``agent_questions`` et
al.) and the ``per_project_channels`` flag are retired.  Cutover
(``src/discord/cutover.py``) promotes the legacy *name* to a *snowflake ID* as
a one-time in-memory step at bot start; once ``discord.channel_id`` is set,
the legacy name has served its purpose and can be retired from the file.
Dropping it *before* a ``channel_id`` exists would lose the only name that
resolves, so the fixer guards on that.

``discord.legacy_config`` reads the config file's ``discord:`` section and
reports WARN when either retired key is present.  The fix retires ``channels``
and ``per_project_channels`` and writes the four canonical keys (empty defaults
when the file has them unset), idempotently, via :mod:`src.config_editor` so
other sections and comments survive.

Doctor runs inside the daemon; this module therefore imports
``src.config_editor`` inside functions (not at module scope) to keep daemon and
CLI import graphs disjoint.
"""

from __future__ import annotations

import asyncio
import os

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "discord"
ID = "discord.legacy_config"

#: The two ``discord:`` keys the spec (2026-10-03 §7.1) retires.
RETIRED_KEYS = ("channels", "per_project_channels")
#: The four canonical keys the fix ensures are present (default when missing).
CANONICAL_KEYS: tuple[tuple[str, object], ...] = (
    ("channel_id", ""),
    ("guild_id", ""),
    ("authorized_users", []),
    ("project_id", ""),
)


def _config_path(ctx: DoctorContext) -> str:
    return getattr(ctx.config, "_config_path", "") or ""


def _discord_section(ctx: DoctorContext) -> dict | None:
    """The ``discord:`` block as written on disk, or ``None`` when absent."""
    path = _config_path(ctx)
    if not path or not os.path.exists(path):
        return None
    from src.config_editor import read_raw_config

    config = read_raw_config(path)
    section = config.get("discord")
    return section if isinstance(section, dict) else None


def _retired_present(section: dict) -> list[str]:
    return [key for key in RETIRED_KEYS if key in section]


async def _check(ctx: DoctorContext) -> CheckResult:
    section = await asyncio.to_thread(_discord_section, ctx)
    if section is None:
        return CheckResult(
            id=ID,
            severity=Severity.INFO,
            detail="no discord: section in the config file (discord not configured)",
        )
    retired = _retired_present(section)
    if not retired:
        return CheckResult(
            id=ID,
            severity=Severity.OK,
            detail="no retired discord channel keys present",
            data={"retired_present": []},
        )
    names = ", ".join(f"discord.{key}" for key in retired)
    channel_id = str(section.get("channel_id", "") or "").strip()
    if channel_id:
        detail = (
            f"retired keys still present: {names}; discord.channel_id is set, so the "
            f"single-channel model is ready to use -- `aq doctor --fix` retires those "
            f"legacy keys and writes {', '.join(k for k, _ in CANONICAL_KEYS)}"
        )
        return CheckResult(
            id=ID,
            severity=Severity.WARN,
            detail=detail,
            fixable=True,
            data={"retired_present": retired, "channel_id_configured": True},
        )
    detail = (
        f"retired keys still present: {names}; discord.channel_id is not set, so "
        f"`aq doctor --fix` cannot drop them without losing the legacy names they "
        f"named -- set discord.channel_id (the Snowflake ID of the channel to bind) "
        f"first, then re-run `aq doctor --fix`"
    )
    return CheckResult(
        id=ID,
        severity=Severity.WARN,
        detail=detail,
        fixable=True,
        data={"retired_present": retired, "channel_id_configured": False},
    )


def _migrate(path: str, section: dict) -> list[str]:
    """Retire the legacy keys; ensure the canonical four are written.  Returns keys removed."""
    from src.config_editor import write_section

    migrated = dict(section)
    removed = [key for key in RETIRED_KEYS if key in migrated]
    for key in removed:
        del migrated[key]
    for key, default in CANONICAL_KEYS:
        if key not in migrated or migrated.get(key) in (None, ""):
            migrated[key] = default
    write_section(path, "discord", migrated)
    return removed


async def _fix(ctx: DoctorContext) -> CheckResult:
    """Discard the retired keys and persist the canonical four.

    The runner applies the fix whenever the check returned WARN or ERROR, so
    this must be safe to run in either state and self-guard on its precondition.
    """
    section = await asyncio.to_thread(_discord_section, ctx)
    if section is None:
        return CheckResult(
            id=ID,
            severity=Severity.INFO,
            detail="no discord: section in the config file (nothing to migrate)",
        )
    retired = _retired_present(section)
    if not retired:
        return CheckResult(
            id=ID,
            severity=Severity.INFO,
            detail="no retired discord channel keys present (nothing to migrate)",
        )
    if not str(section.get("channel_id", "") or "").strip():
        return CheckResult(
            id=ID,
            severity=Severity.WARN,
            detail=(
                "discord.channel_id is not set; refusing to drop the legacy "
                "discord.channels block without a replacement. Set discord.channel_id "
                "(the Snowflake ID of the channel to bind), then re-run `aq doctor --fix`"
            ),
        )
    path = _config_path(ctx)
    removed = await asyncio.to_thread(_migrate, path, section)
    return CheckResult(
        id=ID,
        severity=Severity.OK,
        detail=f"retired {', '.join(f'discord.{k}' for k in removed)}; canonical keys written",
        data={"removed": removed},
    )


def discord_config_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id=ID, run=_check, fix=_fix, owner=OWNER)]


CHECKS = discord_config_checks()
