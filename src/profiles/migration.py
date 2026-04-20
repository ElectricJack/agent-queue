"""DB → vault profile migration — generate markdown files from existing DB profiles.

Roadmap 4.2.1: Read existing agent profiles from the ``agent_profiles`` database
table and write them as hybrid-format markdown files in the vault at
``vault/agent-types/{profile_id}/profile.md``.

The migration is **idempotent** — profiles that already have a vault file are
skipped.  After writing, each generated file is parsed back and compared to the
original DB row to verify round-trip fidelity.

This module provides:

- :func:`migrate_db_profiles_to_vault` — the async migration entry point
  (reads DB, writes vault files, returns a report).
- :func:`scan_profile_migration` — dry-run preview (no writes).
- :func:`verify_round_trip` — parse generated markdown and compare to the
  original :class:`~src.models.AgentProfile`.

Designed to be called from:

- The ``migrate_profiles`` command handler (Discord / MCP tool)
- Startup auto-migration (roadmap 4.2.4)
- CLI ``aq vault migrate-profiles`` (future)

See ``docs/specs/design/profiles.md`` §2 for the hybrid markdown format and
``docs/specs/design/vault.md`` §6 Phase 4 for the migration plan.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.models import AgentProfile
from src.profiles.parser import (
    agent_profile_to_markdown,
    parse_profile,
    parsed_profile_to_agent_profile,
)
from src.vault import ensure_vault_profile_dirs

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class ProfileMigrationResult:
    """Result of migrating a single DB profile to a vault markdown file.

    Attributes:
        profile_id: The profile identifier.
        name: The profile display name.
        action: What happened — ``"written"``, ``"skipped"`` (already exists),
            or ``"error"`` (write or verification failed).
        reason: Human-readable explanation of the action.
        round_trip_ok: Whether the generated markdown passes round-trip
            verification (None if not checked).
        round_trip_diffs: List of field differences found during round-trip
            verification (empty if round-trip is clean).
    """

    profile_id: str
    name: str
    action: str  # "written" | "skipped" | "error"
    reason: str = ""
    round_trip_ok: bool | None = None
    round_trip_diffs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to a dict for command handler responses."""
        d: dict[str, Any] = {
            "profile_id": self.profile_id,
            "name": self.name,
            "action": self.action,
        }
        if self.reason:
            d["reason"] = self.reason
        if self.round_trip_ok is not None:
            d["round_trip_ok"] = self.round_trip_ok
        if self.round_trip_diffs:
            d["round_trip_diffs"] = self.round_trip_diffs
        return d


@dataclass
class MigrationReport:
    """Aggregate result of the profile migration run.

    Attributes:
        dry_run: Whether this was a preview (no files written).
        total: Total number of profiles found in the database.
        written: Number of markdown files written.
        skipped: Number of profiles skipped (vault file already exists).
        errors: Number of profiles that failed to migrate.
        results: Per-profile results.
        details: Human-readable log lines.
    """

    dry_run: bool = False
    total: int = 0
    written: int = 0
    skipped: int = 0
    errors: int = 0
    results: list[ProfileMigrationResult] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to a dict for command handler responses."""
        return {
            "success": self.errors == 0,
            "dry_run": self.dry_run,
            "total": self.total,
            "written": self.written,
            "skipped": self.skipped,
            "errors": self.errors,
            "results": [r.to_dict() for r in self.results],
            "details": self.details,
        }


def _vault_profile_path(data_dir: str, profile_id: str) -> str:
    """Return the vault file path for a profile's markdown definition."""
    return os.path.join(data_dir, "vault", "agent-types", profile_id, "profile.md")


def verify_round_trip(profile: AgentProfile, markdown: str) -> tuple[bool, list[str]]:
    """Parse generated markdown and compare to the original DB profile.

    Verifies that the markdown → parse → AgentProfile conversion produces
    values that match the original database record.  This ensures no data
    is lost during the DB → markdown generation step.

    Parameters
    ----------
    profile:
        The original AgentProfile from the database.
    markdown:
        The generated markdown string.

    Returns
    -------
    tuple[bool, list[str]]
        ``(ok, diffs)`` where *ok* is True if all checked fields match,
        and *diffs* is a list of human-readable difference descriptions.
    """
    parsed = parse_profile(markdown)
    diffs: list[str] = []

    if not parsed.is_valid:
        diffs.append(f"Parse errors in generated markdown: {parsed.errors}")
        return False, diffs

    rebuilt = parsed_profile_to_agent_profile(parsed)

    # Check identity fields
    if rebuilt.get("id", "") != profile.id:
        diffs.append(f"id: DB={profile.id!r}, markdown={rebuilt.get('id', '')!r}")
    if rebuilt.get("name", "") != profile.name:
        diffs.append(f"name: DB={profile.name!r}, markdown={rebuilt.get('name', '')!r}")

    # Check config fields
    if rebuilt.get("model", "") != profile.model:
        diffs.append(f"model: DB={profile.model!r}, markdown={rebuilt.get('model', '')!r}")
    if rebuilt.get("permission_mode", "") != profile.permission_mode:
        diffs.append(
            f"permission_mode: DB={profile.permission_mode!r}, "
            f"markdown={rebuilt.get('permission_mode', '')!r}"
        )

    # Check tools
    rebuilt_tools = rebuilt.get("allowed_tools", [])
    if rebuilt_tools != profile.allowed_tools:
        diffs.append(f"allowed_tools: DB={profile.allowed_tools!r}, markdown={rebuilt_tools!r}")

    # Check MCP servers
    rebuilt_mcp = rebuilt.get("mcp_servers", {})
    if rebuilt_mcp != profile.mcp_servers:
        diffs.append(
            f"mcp_servers: DB keys={sorted(profile.mcp_servers.keys())}, "
            f"markdown keys={sorted(rebuilt_mcp.keys())}"
        )

    # Check install
    rebuilt_install = rebuilt.get("install", {})
    if rebuilt_install != profile.install:
        diffs.append(f"install: DB={profile.install!r}, markdown={rebuilt_install!r}")

    # Check description
    rebuilt_desc = rebuilt.get("description", "")
    if rebuilt_desc != profile.description:
        diffs.append(f"description: DB={profile.description!r}, markdown={rebuilt_desc!r}")

    # For system_prompt_suffix, we can't do exact comparison because the
    # render → parse round-trip normalises whitespace and adds section
    # headers.  Instead, verify the content is semantically present.
    if profile.system_prompt_suffix and not rebuilt.get("system_prompt_suffix"):
        diffs.append("system_prompt_suffix: DB has content, markdown is empty")

    return len(diffs) == 0, diffs


def _render_profile_markdown(profile: AgentProfile) -> str:
    """Render an AgentProfile to the hybrid markdown format.

    Parameters
    ----------
    profile:
        The AgentProfile instance from the database.

    Returns
    -------
    str
        The rendered markdown string.
    """
    return agent_profile_to_markdown(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        model=profile.model,
        permission_mode=profile.permission_mode,
        allowed_tools=profile.allowed_tools if profile.allowed_tools else None,
        mcp_servers=profile.mcp_servers if profile.mcp_servers else None,
        system_prompt_suffix=profile.system_prompt_suffix,
        install=profile.install if profile.install else None,
    )


async def scan_profile_migration(
    db: Any,
    data_dir: str,
) -> MigrationReport:
    """Preview the profile migration without writing any files (dry-run).

    Reads all profiles from the database and reports which ones would be
    migrated (no vault file) and which would be skipped (vault file exists).

    Parameters
    ----------
    db:
        Database instance with ``list_profiles()`` method.
    data_dir:
        Root data directory (e.g. ``~/.agent-queue``).

    Returns
    -------
    MigrationReport
        Preview report with ``dry_run=True``.
    """
    report = MigrationReport(dry_run=True)

    try:
        profiles = await db.list_profiles()
    except Exception as exc:
        report.errors = 1
        report.details.append(f"Failed to read profiles from database: {exc}")
        return report

    report.total = len(profiles)
    report.details.append(f"Found {len(profiles)} profile(s) in database")

    for profile in profiles:
        vault_path = _vault_profile_path(data_dir, profile.id)
        if os.path.isfile(vault_path):
            result = ProfileMigrationResult(
                profile_id=profile.id,
                name=profile.name,
                action="skipped",
                reason="vault file already exists",
            )
            report.skipped += 1
            report.details.append(f"  SKIP {profile.id} ({profile.name}): vault file exists")
        else:
            result = ProfileMigrationResult(
                profile_id=profile.id,
                name=profile.name,
                action="would_write",
                reason="no vault file found",
            )
            report.written += 1
            report.details.append(f"  WOULD WRITE {profile.id} ({profile.name})")
        report.results.append(result)

    report.details.append(f"Summary: {report.written} would write, {report.skipped} already exist")
    return report


async def migrate_db_profiles_to_vault(
    db: Any,
    data_dir: str,
    *,
    dry_run: bool = False,
    verify: bool = True,
    force: bool = False,
) -> MigrationReport:
    """Migrate all DB profiles to vault markdown files.

    Reads every profile from the ``agent_profiles`` table, renders each one
    to the hybrid markdown format (``docs/specs/design/profiles.md`` §2), and
    writes it to ``vault/agent-types/{profile_id}/profile.md``.

    The migration is **idempotent**: profiles that already have a vault file
    are skipped unless *force* is True.

    Parameters
    ----------
    db:
        Database instance implementing ``list_profiles()``.
    data_dir:
        Root data directory (e.g. ``~/.agent-queue``).
    dry_run:
        If True, scan and report without writing files.
    verify:
        If True, parse each generated file back and compare to the DB
        record (round-trip verification).
    force:
        If True, overwrite existing vault files instead of skipping them.

    Returns
    -------
    MigrationReport
        Detailed report of the migration.
    """
    if dry_run:
        return await scan_profile_migration(db, data_dir)

    report = MigrationReport(dry_run=False)

    # 1. Read all profiles from DB
    try:
        profiles = await db.list_profiles()
    except Exception as exc:
        report.errors = 1
        report.details.append(f"Failed to read profiles from database: {exc}")
        logger.error("Profile migration: DB read failed: %s", exc, exc_info=True)
        return report

    report.total = len(profiles)
    if not profiles:
        report.details.append("No profiles found in database — nothing to migrate")
        logger.info("Profile migration: no profiles in database")
        return report

    report.details.append(f"Found {len(profiles)} profile(s) in database")
    logger.info("Profile migration: starting migration of %d profile(s)", len(profiles))

    # 2. Process each profile
    for profile in profiles:
        # Guard: ``orchestrator`` is the deprecated name for the singleton
        # supervisor scope.  A stray DB row with id=="orchestrator" (leftover
        # from an older install) would otherwise cause us to recreate the
        # stale vault/agent-types/orchestrator/ tree on every startup.  Skip
        # it loudly so the operator can clean up the DB row.
        if profile.id == "orchestrator":
            result = ProfileMigrationResult(
                profile_id=profile.id,
                name=profile.name,
                action="skipped",
                reason="deprecated profile id — use 'supervisor' instead",
            )
            report.skipped += 1
            report.results.append(result)
            report.details.append(
                f"  SKIP {profile.id} ({profile.name}): deprecated id — "
                "delete this DB row (use 'supervisor' scope instead)"
            )
            logger.warning(
                "Profile migration: skipping deprecated 'orchestrator' "
                "profile row — delete it from the DB; supervisor is the "
                "singleton agent-type now."
            )
            continue

        vault_path = _vault_profile_path(data_dir, profile.id)

        # Check if vault file already exists
        if os.path.isfile(vault_path) and not force:
            result = ProfileMigrationResult(
                profile_id=profile.id,
                name=profile.name,
                action="skipped",
                reason="vault file already exists",
            )
            report.skipped += 1
            report.results.append(result)
            report.details.append(f"  SKIP {profile.id} ({profile.name}): vault file exists")
            logger.debug("Profile migration: skip %s (vault file exists)", profile.id)
            continue

        # Render markdown
        try:
            markdown = _render_profile_markdown(profile)
        except Exception as exc:
            result = ProfileMigrationResult(
                profile_id=profile.id,
                name=profile.name,
                action="error",
                reason=f"Failed to render markdown: {exc}",
            )
            report.errors += 1
            report.results.append(result)
            report.details.append(f"  ERROR {profile.id} ({profile.name}): render failed — {exc}")
            logger.error(
                "Profile migration: render failed for %s: %s",
                profile.id,
                exc,
                exc_info=True,
            )
            continue

        # Verify round-trip before writing (catch issues early)
        rt_ok: bool | None = None
        rt_diffs: list[str] = []
        if verify:
            rt_ok, rt_diffs = verify_round_trip(profile, markdown)
            if not rt_ok:
                logger.warning(
                    "Profile migration: round-trip diffs for %s: %s",
                    profile.id,
                    rt_diffs,
                )

        # Create vault directories
        try:
            ensure_vault_profile_dirs(data_dir, profile.id)
        except Exception as exc:
            result = ProfileMigrationResult(
                profile_id=profile.id,
                name=profile.name,
                action="error",
                reason=f"Failed to create vault directories: {exc}",
            )
            report.errors += 1
            report.results.append(result)
            report.details.append(f"  ERROR {profile.id} ({profile.name}): mkdir failed — {exc}")
            logger.error(
                "Profile migration: mkdir failed for %s: %s",
                profile.id,
                exc,
                exc_info=True,
            )
            continue

        # Write the markdown file
        try:
            Path(vault_path).write_text(markdown, encoding="utf-8")
        except OSError as exc:
            result = ProfileMigrationResult(
                profile_id=profile.id,
                name=profile.name,
                action="error",
                reason=f"Failed to write vault file: {exc}",
            )
            report.errors += 1
            report.results.append(result)
            report.details.append(f"  ERROR {profile.id} ({profile.name}): write failed — {exc}")
            logger.error(
                "Profile migration: write failed for %s: %s",
                profile.id,
                exc,
                exc_info=True,
            )
            continue

        # Success
        action_verb = "OVERWRITE" if (os.path.isfile(vault_path) and force) else "WRITE"
        result = ProfileMigrationResult(
            profile_id=profile.id,
            name=profile.name,
            action="written",
            reason=vault_path,
            round_trip_ok=rt_ok,
            round_trip_diffs=rt_diffs,
        )
        report.written += 1
        report.results.append(result)

        detail = f"  {action_verb} {profile.id} ({profile.name}) → {vault_path}"
        if rt_ok is False:
            detail += f" [round-trip diffs: {len(rt_diffs)}]"
        report.details.append(detail)
        logger.info(
            "Profile migration: wrote %s to %s%s",
            profile.id,
            vault_path,
            f" (round-trip diffs: {rt_diffs})" if rt_diffs else "",
        )

    # 3. Summary
    report.details.append(
        f"Migration complete: {report.written} written, "
        f"{report.skipped} skipped, {report.errors} errors "
        f"(of {report.total} total)"
    )
    logger.info(
        "Profile migration complete: %d written, %d skipped, %d errors (of %d total)",
        report.written,
        report.skipped,
        report.errors,
        report.total,
    )

    return report
