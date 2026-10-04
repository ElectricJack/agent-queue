"""``discord.config`` doctor check -- is the single-channel Discord config in place?

Spec 2026-10-03 "Discord as a chat extension of the supervisor", §1.2 and the
§7.1 config migration: drop the retired ``discord.channels`` and
``per_project_channels`` blocks; write ``discord.channel_id``,
``discord.guild_id``, ``discord.authorized_users`` and ``discord.project_id``.

The check reads the ``discord:`` section *as written on disk* (``${ENV}``
references intact) next to the daemon's live view of it, and reports each key
that is missing, retired or wrong.  The fix writes only what it can derive:

* ``channel_id`` -- the ID the startup cutover (``src/discord/cutover.py``)
  promoted in memory from the one legacy channel name.  Discord settings are
  restart-only, so the bot's config object is the one the cutover wrote to.
* ``project_id`` -- the sole project, when there is exactly one.  The runtime
  infers that project too, but pinning it means adding a second project later
  does not leave Discord input queued with no recipient.
* ``per_project_channels`` -- dropped; nothing reads it.
* ``channels`` -- dropped once a channel ID is known *and* this daemon's
  cutover has completed (or no bot is configured, so no cutover will run).
  Until then the legacy names are the cutover's only way to find the channels
  whose old cards it inventories at start.

``guild_id`` and ``authorized_users`` are never derived: who may talk to the
supervisor is the operator's call.  An empty allowlist disables conversations
and escalation replies (``src/conversations/preconditions.py`` fails closed)
while the bot's gateway gate still admits everyone.  Absent keys are written
empty, so the file names every setting the operator has left to fill in.

Edits go through :func:`src.config_editor.update_section_keys`, which leaves
the other keys, the other sections and comments as they were.  It is imported
inside functions, like every config-editor use in doctor.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "discord"
ID = "discord.config"

#: The ``discord:`` keys the spec (§7.1) retires.
RETIRED_KEYS = ("channels", "per_project_channels")
#: The keys the spec (§7.1) has the fix write, each with its empty value.
CANONICAL_KEYS: tuple[tuple[str, Any], ...] = (
    ("channel_id", ""),
    ("guild_id", ""),
    ("authorized_users", []),
    ("project_id", ""),
)


@dataclass
class _State:
    """Everything the check and the fix decide from, read once."""

    path: str
    #: The ``discord:`` section as written, ``${ENV}`` references intact.
    section: dict[str, Any]
    #: The live (resolved) settings: the bot's own config when one is connected.
    channel_id: str
    guild_id: str
    authorized_users: list[str]
    project_id: str
    bot_configured: bool
    #: The running cutover's status, or ``None`` when no bot is connected.
    cutover_status: str | None
    #: Every project id, or ``None`` when the database is out of reach.
    projects: list[str] | None


@dataclass
class _Plan:
    #: Keys the fix sets, and the values it sets them to.
    set_values: dict[str, Any] = field(default_factory=dict)
    #: Retired keys the fix removes.
    remove: list[str] = field(default_factory=list)
    #: ``(key, problem)`` the fix resolves.
    fixable: list[tuple[str, str]] = field(default_factory=list)
    #: ``(key, problem)`` only the operator can resolve.
    manual: list[tuple[str, str]] = field(default_factory=list)

    def resolves(self, key: str, problem: str) -> None:
        self.fixable.append((key, problem))

    def asks(self, key: str, problem: str) -> None:
        self.manual.append((key, problem))


def _config_path(ctx: DoctorContext) -> str:
    return getattr(ctx.config, "_config_path", "") or ""


def _read_section(path: str) -> dict[str, Any] | None:
    if not path or not os.path.exists(path):
        return None
    from src.config_editor import read_raw_config

    section = read_raw_config(path).get("discord")
    return section if isinstance(section, dict) else None


def _bot(ctx: DoctorContext) -> Any:
    orchestrator = getattr(ctx.handler, "orchestrator", None)
    return getattr(orchestrator, "_discord_bot", None)


async def _gather(ctx: DoctorContext) -> _State | None:
    path = _config_path(ctx)
    section = await asyncio.to_thread(_read_section, path)
    if section is None:
        return None
    bot = _bot(ctx)
    live = getattr(getattr(bot, "config", None) or ctx.config, "discord", None)
    report = getattr(bot, "_cutover_report", None)
    projects: list[str] | None = None
    if ctx.db is not None:
        try:
            projects = sorted(project.id for project in await ctx.db.list_projects())
        except Exception:  # noqa: BLE001 - the other keys still check without it
            projects = None
    return _State(
        path=path,
        section=section,
        channel_id=str(getattr(live, "channel_id", "") or "").strip(),
        guild_id=str(getattr(live, "guild_id", "") or "").strip(),
        authorized_users=[str(u) for u in (getattr(live, "authorized_users", None) or [])],
        project_id=str(getattr(live, "project_id", "") or "").strip(),
        bot_configured=bool(str(getattr(live, "bot_token", "") or "").strip()),
        cutover_status=getattr(report, "status", None) if bot is not None else None,
        projects=projects,
    )


def _written(section: dict[str, Any], key: str) -> bool:
    """Whether the file gives ``key`` a non-empty value (an ``${ENV}`` reference counts)."""
    value = section.get(key)
    return bool(value) and bool(str(value).strip())


def _plan(state: _State) -> _Plan:
    plan = _Plan()
    section = state.section

    # -- discord.channel_id -------------------------------------------------
    if not _written(section, "channel_id"):
        if state.channel_id:
            plan.set_values["channel_id"] = state.channel_id
            plan.resolves(
                "channel_id",
                f"not written; the startup cutover resolved the legacy channel name to "
                f"{state.channel_id} in memory only",
            )
        elif state.bot_configured:
            plan.asks(
                "channel_id",
                "not set and no legacy name resolved to one: set it to the numeric ID of "
                "the one channel the bot talks in, then restart the daemon",
            )
    elif state.channel_id and not state.channel_id.isdigit():
        plan.asks(
            "channel_id",
            "is not a numeric Discord channel ID (a channel name no longer works here)",
        )

    # -- retired keys -------------------------------------------------------
    if "per_project_channels" in section:
        plan.remove.append("per_project_channels")
        plan.resolves("per_project_channels", "retired; nothing reads it")
    if "channels" in section:
        if not state.bot_configured:
            plan.remove.append("channels")
            plan.resolves("channels", "retired")
        elif not state.channel_id:
            plan.asks(
                "channels",
                "retired, but it still names the only channel binding; it is dropped "
                "once discord.channel_id is set",
            )
        elif state.cutover_status != "complete":
            seen = state.cutover_status or "no bot connected"
            plan.asks(
                "channels",
                f"retired, but the startup cutover still reads it to find old cards and "
                f"has not completed (now: {seen}); it is dropped once the cutover completes",
            )
        else:
            plan.remove.append("channels")
            plan.resolves("channels", "retired; the cutover has completed")

    # -- guild_id / authorized_users: never derived -------------------------
    if state.bot_configured and not state.guild_id:
        plan.asks(
            "guild_id",
            "not set: set it to the Discord server ID so the bot can resolve its channel",
        )
    if state.bot_configured and not state.authorized_users:
        plan.asks(
            "authorized_users",
            "empty, so Discord conversations stay off and escalation replies are "
            "ignored, while the bot's slash commands answer anyone: list the Discord "
            "user IDs that may talk to the supervisor",
        )

    # -- project_id ---------------------------------------------------------
    # The value the file sets is the one the next restart applies; the live value
    # stands in only for an ${ENV} reference, which the file alone cannot resolve.
    written = str(section.get("project_id") or "").strip()
    project_id = written if written and "${" not in written else state.project_id
    projects = state.projects
    if project_id:
        if projects is not None and project_id not in projects:
            plan.asks(
                "project_id",
                f"names no project (known: {', '.join(projects) or 'none'})",
            )
    elif projects is not None and len(projects) == 1:
        plan.set_values["project_id"] = projects[0]
        plan.resolves(
            "project_id",
            f"not set; inferred as the sole project {projects[0]}, which stops working "
            f"when a second project is added",
        )
    elif projects is not None and len(projects) > 1:
        plan.asks(
            "project_id",
            f"not set and there are {len(projects)} projects, so Discord conversation "
            f"input stays queued: set it to one of {', '.join(projects)}",
        )

    # Any write also gives each absent canonical key its empty value.
    if plan.set_values or plan.remove:
        for key, empty in CANONICAL_KEYS:
            if key not in section and key not in plan.set_values:
                plan.set_values[key] = empty
    return plan


def _describe(items: list[tuple[str, str]]) -> str:
    return "; ".join(f"discord.{key} {problem}" for key, problem in items)


async def _check(ctx: DoctorContext) -> CheckResult:
    state = await _gather(ctx)
    if state is None:
        return CheckResult(
            id=ID,
            severity=Severity.INFO,
            detail="no discord: section in the config file (Discord is not configured)",
        )
    plan = _plan(state)
    data = {
        "retired_present": [key for key in RETIRED_KEYS if key in state.section],
        "fix_sets": sorted(key for key in plan.set_values if key in dict(plan.fixable)),
        "fix_removes": list(plan.remove),
        "manual": [key for key, _ in plan.manual],
        "cutover_status": state.cutover_status,
    }
    if not plan.fixable and not plan.manual:
        return CheckResult(
            id=ID,
            severity=Severity.OK,
            detail="discord config uses the single-channel keys; no retired keys remain",
            data=data,
        )
    parts = []
    if plan.fixable:
        parts.append(f"`aq doctor --check {ID} --fix` resolves: {_describe(plan.fixable)}")
    if plan.manual:
        parts.append(f"needs the operator: {_describe(plan.manual)}")
    return CheckResult(id=ID, severity=Severity.WARN, detail=". ".join(parts), data=data)


async def _fix(ctx: DoctorContext) -> CheckResult:
    """Write what :func:`_plan` can derive; the runner re-runs the check afterwards."""
    state = await _gather(ctx)
    if state is None:
        return CheckResult(id=ID, severity=Severity.INFO, detail="no discord: section")
    plan = _plan(state)
    if not plan.set_values and not plan.remove:
        return CheckResult(
            id=ID, severity=Severity.INFO, detail="nothing the fix can derive; see the check"
        )
    from src.config_editor import update_section_keys

    await asyncio.to_thread(
        update_section_keys,
        state.path,
        "discord",
        set_values=plan.set_values,
        remove=plan.remove,
    )
    return CheckResult(
        id=ID,
        severity=Severity.OK,
        detail=(
            f"wrote {', '.join(sorted(plan.set_values)) or 'nothing'}; "
            f"removed {', '.join(plan.remove) or 'nothing'}"
        ),
        data={"set": sorted(plan.set_values), "removed": list(plan.remove)},
    )


def discord_config_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id=ID, run=_check, fix=_fix, owner=OWNER)]


CHECKS = discord_config_checks()
