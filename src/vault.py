"""Vault directory structure initialization and migration.

Creates the ``~/.agent-queue/vault/`` directory tree described in
``docs/specs/design/vault.md`` §2.  The vault is a structured, human-readable
knowledge base (Obsidian-compatible) that serves as the single source of truth
for system configuration and accumulated intelligence.

The top-level structure is created once at orchestrator startup via
``ensure_vault_structure()``.  Per-profile and per-project subdirectories are
created dynamically as profiles and projects are added.

All directory creation is idempotent — calling any function when the directories
already exist is a safe no-op.

The consolidated migration entry point ``run_vault_migration()`` (spec §6)
orchestrates all Phase 1 migrations in the correct order, supports dry-run
mode, and returns a detailed report.
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


def migrate_notes_to_vault(data_dir: str, project_id: str) -> bool:
    """Move project notes from ``notes/{project_id}/`` to ``vault/projects/{project_id}/notes/``.

    Part of vault migration Phase 1 (spec §6).  Moves all files (preserving
    any subdirectory structure) from the legacy ``notes/{project_id}/``
    directory into the vault's per-project notes directory.

    The operation is **idempotent**:

    * If the source directory does not exist, nothing happens (returns ``False``).
    * If a destination file already exists, that individual file is skipped.
    * After all files are moved, empty source directories are removed.
    * Calling the function again after a successful migration is a safe no-op.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        project_id: The project identifier (e.g. ``mech-fighters``).

    Returns:
        ``True`` if any files were moved, ``False`` if skipped entirely.
    """
    source = os.path.join(data_dir, "notes", project_id)
    dest = os.path.join(data_dir, "vault", "projects", project_id, "notes")

    if not os.path.isdir(source):
        logger.debug(
            "Notes migration for %s: source %s does not exist, skipping",
            project_id,
            source,
        )
        return False

    # Ensure the destination exists before moving files into it.
    os.makedirs(dest, exist_ok=True)

    moved_any = False
    for dirpath, _dirnames, filenames in os.walk(source):
        # Compute relative path from source root
        rel_dir = os.path.relpath(dirpath, source)
        dest_dir = os.path.join(dest, rel_dir) if rel_dir != "." else dest
        os.makedirs(dest_dir, exist_ok=True)

        for fname in filenames:
            src_file = os.path.join(dirpath, fname)
            dst_file = os.path.join(dest_dir, fname)

            if os.path.exists(dst_file):
                logger.debug(
                    "Notes migration for %s: %s already exists at destination, skipping",
                    project_id,
                    fname,
                )
                continue

            shutil.move(src_file, dst_file)
            moved_any = True
            logger.debug("Moved note %s → %s", src_file, dst_file)

    # Clean up empty source directories (bottom-up).
    for dirpath, dirnames, filenames in os.walk(source, topdown=False):
        if not filenames and not dirnames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass  # Not empty or permission issue — leave it

    # Remove the top-level source dir if it's now empty.
    try:
        os.rmdir(source)
    except OSError:
        pass

    if moved_any:
        logger.info(
            "Migrated notes for project %s from %s to %s",
            project_id,
            source,
            dest,
        )
    return moved_any


def migrate_rule_files(data_dir: str) -> dict:
    """Move rule markdown files from ``memory/`` to vault playbook locations.

    Part of vault migration Phase 1 (spec §6).  Moves rule files:

    - **Global rules:** ``memory/global/rules/*.md`` →
      ``vault/system/playbooks/``
    - **Project rules:** ``memory/{project_id}/rules/*.md`` →
      ``vault/projects/{project_id}/playbooks/``

    The operation is **idempotent**:

    * Files already present at the destination are skipped.
    * Source files are removed only after a successful copy.
    * Each move is logged individually for auditability.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        Dict with ``moved``, ``skipped``, and ``errors`` counts plus a
        ``details`` list of per-file log messages.
    """
    stats: dict = {"moved": 0, "skipped": 0, "errors": 0, "details": []}
    memory_root = os.path.join(data_dir, "memory")

    if not os.path.isdir(memory_root):
        logger.debug("Rule file migration: memory root %s does not exist, skipping", memory_root)
        return stats

    for scope_dir in sorted(os.listdir(memory_root)):
        rules_dir = os.path.join(memory_root, scope_dir, "rules")
        if not os.path.isdir(rules_dir):
            continue

        # Determine vault destination based on scope
        if scope_dir == "global":
            dest_dir = os.path.join(data_dir, "vault", "system", "playbooks")
        else:
            dest_dir = os.path.join(data_dir, "vault", "projects", scope_dir, "playbooks")

        # Ensure destination directory exists
        os.makedirs(dest_dir, exist_ok=True)

        for filename in sorted(os.listdir(rules_dir)):
            if not filename.endswith(".md"):
                continue

            src_path = os.path.join(rules_dir, filename)
            dest_path = os.path.join(dest_dir, filename)

            # Idempotent: skip if destination already exists
            if os.path.exists(dest_path):
                stats["skipped"] += 1
                detail = f"SKIP {scope_dir}/{filename}: already at destination"
                stats["details"].append(detail)
                logger.debug("Rule migration: %s already at %s, skipping", filename, dest_dir)
                continue

            try:
                shutil.copy2(src_path, dest_path)
                os.remove(src_path)
                stats["moved"] += 1
                detail = f"MOVE {scope_dir}/{filename}: {rules_dir} → {dest_dir}"
                stats["details"].append(detail)
                logger.info("Migrated rule file %s → %s", src_path, dest_path)
            except Exception as e:
                stats["errors"] += 1
                detail = f"ERROR {scope_dir}/{filename}: {e}"
                stats["details"].append(detail)
                logger.warning("Failed to migrate rule file %s: %s", src_path, e)

    if stats["moved"] or stats["errors"]:
        logger.info(
            "Rule file migration complete: %d moved, %d skipped, %d errors",
            stats["moved"],
            stats["skipped"],
            stats["errors"],
        )

    return stats


def migrate_obsidian_config(data_dir: str) -> bool:
    """Move Obsidian config from ``memory/.obsidian/`` to ``vault/.obsidian/``.

    Part of vault migration Phase 1 (spec §6).  Moves the entire
    ``.obsidian/`` directory — themes, plugins, workspace layout — from the
    legacy ``memory/`` location to the new ``vault/`` root.

    The operation is **idempotent**:

    * If the source does not exist, nothing happens (returns ``False``).
    * If the destination already exists, nothing happens (returns ``False``).
    * Only when the source exists *and* the destination does not will the
      move be performed (returns ``True``).

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        ``True`` if the move was performed, ``False`` if skipped.
    """
    source = os.path.join(data_dir, "memory", ".obsidian")
    dest = os.path.join(data_dir, "vault", ".obsidian")

    if not os.path.isdir(source):
        logger.debug("Obsidian config migration: source %s does not exist, skipping", source)
        return False

    if os.path.exists(dest):
        logger.debug("Obsidian config migration: destination %s already exists, skipping", dest)
        return False

    # Ensure the vault/ parent directory exists before moving into it.
    os.makedirs(os.path.join(data_dir, "vault"), exist_ok=True)

    shutil.move(source, dest)
    logger.info("Migrated Obsidian config from %s to %s", source, dest)
    return True


# ---------------------------------------------------------------------------
# Static vault subdirectories (always created at startup)
# ---------------------------------------------------------------------------

_STATIC_DIRS: list[str] = [
    # Obsidian configuration
    "vault/.obsidian",
    # System-scoped playbooks and memory
    "vault/system/playbooks",
    "vault/system/memory",
    # Orchestrator profile, playbooks, and memory
    "vault/orchestrator/playbooks",
    "vault/orchestrator/memory",
    # Agent-types root (subdirs created per profile)
    "vault/agent-types",
    # Projects root (subdirs created per project)
    "vault/projects",
    # Templates for new profiles, playbooks, etc.
    "vault/templates",
]


def ensure_vault_layout(data_dir: str) -> None:
    """Create the static vault directory structure under *data_dir*.

    This covers the directories that exist regardless of which profiles or
    projects are configured:

    - ``vault/system/playbooks/``
    - ``vault/system/memory/``
    - ``vault/orchestrator/playbooks/``
    - ``vault/orchestrator/memory/``
    - ``vault/agent-types/``
    - ``vault/projects/``
    - ``vault/templates/``
    - ``vault/.obsidian/``

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
    """
    for subdir in _STATIC_DIRS:
        path = os.path.join(data_dir, subdir)
        os.makedirs(path, exist_ok=True)

    logger.info("Vault directory structure ensured at %s/vault", data_dir)


def ensure_vault_profile_dirs(data_dir: str, profile_id: str) -> None:
    """Create vault subdirectories for an agent-type profile.

    Creates the ``vault/agent-types/{profile_id}/`` tree with ``playbooks/``
    and ``memory/`` subdirectories, as described in the vault spec §2.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        profile_id: The profile identifier (e.g. ``coding``).
    """
    base = os.path.join(data_dir, "vault", "agent-types", profile_id)
    os.makedirs(os.path.join(base, "playbooks"), exist_ok=True)
    os.makedirs(os.path.join(base, "memory"), exist_ok=True)


def copy_project_memory_to_vault(data_dir: str, project_id: str) -> bool:
    """Copy project memory files from ``memory/{project_id}/`` to the vault.

    Part of vault migration Phase 1 (spec §6).  Copies the following files
    from the legacy ``memory/{project_id}/`` directory into the vault's
    per-project memory directory:

    - ``profile.md`` → ``vault/projects/{project_id}/memory/profile.md``
    - ``factsheet.md`` → ``vault/projects/{project_id}/memory/factsheet.md``
    - ``knowledge/`` → ``vault/projects/{project_id}/memory/knowledge/``

    Files are **copied** (not moved) because the old paths are still used by
    the v1 memory system during the transition period.

    Explicitly excluded: ``tasks/`` (already migrated in Phase 0) and
    ``rules/`` (migrated in roadmap 1.2.2).

    The operation is **idempotent**:

    * If the source directory does not exist, nothing happens (returns ``False``).
    * If a destination file already exists and is at least as recent as the
      source, that file is skipped.
    * If the source file is newer than the destination, the destination is
      updated.
    * Calling the function again after a successful copy is a safe no-op
      (unless source files have been updated).

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        project_id: The project identifier (e.g. ``mech-fighters``).

    Returns:
        ``True`` if any files were copied/updated, ``False`` if skipped.
    """
    source = os.path.join(data_dir, "memory", project_id)
    dest = os.path.join(data_dir, "vault", "projects", project_id, "memory")

    if not os.path.isdir(source):
        logger.debug(
            "Memory copy for %s: source %s does not exist, skipping",
            project_id,
            source,
        )
        return False

    os.makedirs(dest, exist_ok=True)

    copied_any = False

    # Copy top-level memory files (profile.md, factsheet.md)
    for filename in ("profile.md", "factsheet.md"):
        src_file = os.path.join(source, filename)
        dst_file = os.path.join(dest, filename)

        if not os.path.isfile(src_file):
            continue

        if os.path.exists(dst_file):
            # Only update if source is newer
            src_mtime = os.path.getmtime(src_file)
            dst_mtime = os.path.getmtime(dst_file)
            if src_mtime <= dst_mtime:
                logger.debug(
                    "Memory copy for %s: %s is up to date, skipping",
                    project_id,
                    filename,
                )
                continue

        shutil.copy2(src_file, dst_file)
        copied_any = True
        logger.debug("Copied memory file %s → %s", src_file, dst_file)

    # Copy knowledge/ directory contents
    knowledge_src = os.path.join(source, "knowledge")
    knowledge_dst = os.path.join(dest, "knowledge")

    if os.path.isdir(knowledge_src):
        os.makedirs(knowledge_dst, exist_ok=True)

        for dirpath, _dirnames, filenames in os.walk(knowledge_src):
            rel_dir = os.path.relpath(dirpath, knowledge_src)
            dst_dir = os.path.join(knowledge_dst, rel_dir) if rel_dir != "." else knowledge_dst
            os.makedirs(dst_dir, exist_ok=True)

            for fname in filenames:
                src_file = os.path.join(dirpath, fname)
                dst_file = os.path.join(dst_dir, fname)

                if os.path.exists(dst_file):
                    src_mtime = os.path.getmtime(src_file)
                    dst_mtime = os.path.getmtime(dst_file)
                    if src_mtime <= dst_mtime:
                        logger.debug(
                            "Memory copy for %s: knowledge/%s is up to date, skipping",
                            project_id,
                            fname,
                        )
                        continue

                shutil.copy2(src_file, dst_file)
                copied_any = True
                logger.debug("Copied knowledge file %s → %s", src_file, dst_file)

    if copied_any:
        logger.info(
            "Copied project memory files for %s from %s to %s",
            project_id,
            source,
            dest,
        )
    return copied_any


def ensure_vault_project_dirs(data_dir: str, project_id: str) -> None:
    """Create vault subdirectories for a project.

    Creates the ``vault/projects/{project_id}/`` tree with subdirectories
    for memory, playbooks, notes, references, and overrides, as described
    in the vault spec §2.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        project_id: The project identifier (e.g. ``mech-fighters``).
    """
    base = os.path.join(data_dir, "vault", "projects", project_id)
    for subdir in (
        "memory/knowledge",
        "memory/insights",
        "playbooks",
        "notes",
        "references",
        "overrides",
    ):
        os.makedirs(os.path.join(base, subdir), exist_ok=True)


# ---------------------------------------------------------------------------
# Startup auto-migration helpers (spec §6 — smooth transition)
# ---------------------------------------------------------------------------


def has_legacy_data(data_dir: str) -> bool:
    """Check whether legacy data paths exist that should be migrated.

    Returns ``True`` if any of these contain actual content:

    * ``notes/{project}/`` — any project subdirectory with files
    * ``memory/{project}/rules/`` — any project or global rules directory
    * ``memory/.obsidian/`` — Obsidian config at the old location
    * ``memory/{project}/`` — any project memory directory with files

    This is used at startup to decide whether to trigger an automatic
    migration for existing installs (spec §6).
    """
    # Check notes/{project}/ directories
    notes_root = os.path.join(data_dir, "notes")
    if os.path.isdir(notes_root):
        for entry in os.listdir(notes_root):
            entry_path = os.path.join(notes_root, entry)
            if os.path.isdir(entry_path) and any(os.scandir(entry_path)):
                return True

    # Check memory/ tree for rules dirs, obsidian config, or project files
    memory_root = os.path.join(data_dir, "memory")
    if os.path.isdir(memory_root):
        # Obsidian config at old location
        if os.path.isdir(os.path.join(memory_root, ".obsidian")):
            return True

        for entry in os.listdir(memory_root):
            if entry.startswith("."):
                continue
            entry_path = os.path.join(memory_root, entry)
            if not os.path.isdir(entry_path):
                continue
            # Check for rules/ subdirectory with .md files
            rules_path = os.path.join(entry_path, "rules")
            if os.path.isdir(rules_path):
                for f in os.listdir(rules_path):
                    if f.endswith(".md"):
                        return True
            # Check for project memory files (profile.md, factsheet.md, knowledge/)
            if entry not in _MEMORY_SPECIAL_DIRS:
                for mem_file in ("profile.md", "factsheet.md"):
                    if os.path.isfile(os.path.join(entry_path, mem_file)):
                        return True
                if os.path.isdir(os.path.join(entry_path, "knowledge")):
                    return True

    return False


def vault_has_content(data_dir: str) -> bool:
    """Check whether the vault already contains user or migrated content.

    Returns ``True`` if the vault has any regular files beyond the
    bare directory skeleton created by ``ensure_vault_layout``.  This is
    used at startup to avoid overwriting existing vault content when
    deciding whether to auto-migrate (spec §6).

    Specifically, checks for any ``.md`` files or other content files
    inside ``vault/projects/``, ``vault/system/playbooks/``,
    ``vault/orchestrator/``, or ``vault/agent-types/``.
    The ``vault/.obsidian/`` directory is excluded from the check
    because its presence alone doesn't indicate user-created content.
    """
    vault_root = os.path.join(data_dir, "vault")
    if not os.path.isdir(vault_root):
        return False

    # Walk the vault tree looking for any regular files
    # (excluding .obsidian/ which is config, not content)
    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Skip .obsidian/ subtree — it's Obsidian config, not vault content
        rel = os.path.relpath(dirpath, vault_root)
        if rel == ".obsidian" or rel.startswith(".obsidian" + os.sep):
            continue

        if filenames:
            return True

    return False


# ---------------------------------------------------------------------------
# Consolidated vault migration (spec §6)
# ---------------------------------------------------------------------------

# Directories inside memory/ that are NOT project IDs
_MEMORY_SPECIAL_DIRS = frozenset({"global", ".obsidian"})


def _discover_project_ids(data_dir: str) -> list[str]:
    """Discover project IDs from legacy filesystem locations.

    Scans ``notes/`` and ``memory/`` directories for subdirectories that
    represent projects.  Returns a sorted, deduplicated list.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        Sorted list of project identifiers discovered on disk.
    """
    project_ids: set[str] = set()

    # From notes/{project_id}/
    notes_root = os.path.join(data_dir, "notes")
    if os.path.isdir(notes_root):
        for entry in os.listdir(notes_root):
            if os.path.isdir(os.path.join(notes_root, entry)):
                project_ids.add(entry)

    # From memory/{project_id}/ (excluding special dirs)
    memory_root = os.path.join(data_dir, "memory")
    if os.path.isdir(memory_root):
        for entry in os.listdir(memory_root):
            if entry in _MEMORY_SPECIAL_DIRS or entry.startswith("."):
                continue
            if os.path.isdir(os.path.join(memory_root, entry)):
                project_ids.add(entry)

    return sorted(project_ids)


def _scan_obsidian_migration(data_dir: str) -> dict:
    """Preview what ``migrate_obsidian_config`` would do.

    Returns a dict with ``action`` ("move" or "skip") and ``reason``.
    """
    source = os.path.join(data_dir, "memory", ".obsidian")
    dest = os.path.join(data_dir, "vault", ".obsidian")

    if not os.path.isdir(source):
        return {"action": "skip", "reason": "source does not exist"}
    if os.path.exists(dest):
        return {"action": "skip", "reason": "destination already exists"}
    return {"action": "move", "source": source, "dest": dest}


def _scan_notes_migration(data_dir: str, project_id: str) -> dict:
    """Preview what ``migrate_notes_to_vault`` would do for one project.

    Returns a dict with counts: ``would_move`` and ``would_skip``.
    """
    source = os.path.join(data_dir, "notes", project_id)
    dest = os.path.join(data_dir, "vault", "projects", project_id, "notes")
    result: dict = {"would_move": 0, "would_skip": 0, "files": []}

    if not os.path.isdir(source):
        return result

    for dirpath, _dirnames, filenames in os.walk(source):
        rel_dir = os.path.relpath(dirpath, source)
        dest_dir = os.path.join(dest, rel_dir) if rel_dir != "." else dest

        for fname in filenames:
            dst_file = os.path.join(dest_dir, fname)
            rel_path = os.path.join(rel_dir, fname) if rel_dir != "." else fname
            if os.path.exists(dst_file):
                result["would_skip"] += 1
                result["files"].append(f"SKIP {rel_path}: already at destination")
            else:
                result["would_move"] += 1
                result["files"].append(f"MOVE {rel_path}")

    return result


def _scan_memory_copy(data_dir: str, project_id: str) -> dict:
    """Preview what ``copy_project_memory_to_vault`` would do for one project.

    Returns a dict with counts: ``would_copy``, ``would_update``, ``would_skip``.
    """
    source = os.path.join(data_dir, "memory", project_id)
    dest = os.path.join(data_dir, "vault", "projects", project_id, "memory")
    result: dict = {"would_copy": 0, "would_update": 0, "would_skip": 0, "files": []}

    if not os.path.isdir(source):
        return result

    def _check_file(src_file: str, dst_file: str, label: str) -> None:
        if not os.path.isfile(src_file):
            return
        if os.path.exists(dst_file):
            src_mtime = os.path.getmtime(src_file)
            dst_mtime = os.path.getmtime(dst_file)
            if src_mtime <= dst_mtime:
                result["would_skip"] += 1
                result["files"].append(f"SKIP {label}: up to date")
            else:
                result["would_update"] += 1
                result["files"].append(f"UPDATE {label}: source is newer")
        else:
            result["would_copy"] += 1
            result["files"].append(f"COPY {label}")

    # Top-level files
    for filename in ("profile.md", "factsheet.md"):
        _check_file(
            os.path.join(source, filename),
            os.path.join(dest, filename),
            filename,
        )

    # knowledge/ tree
    knowledge_src = os.path.join(source, "knowledge")
    knowledge_dst = os.path.join(dest, "knowledge")
    if os.path.isdir(knowledge_src):
        for dirpath, _dirnames, filenames in os.walk(knowledge_src):
            rel_dir = os.path.relpath(dirpath, knowledge_src)
            dst_dir = os.path.join(knowledge_dst, rel_dir) if rel_dir != "." else knowledge_dst
            for fname in filenames:
                src_file = os.path.join(dirpath, fname)
                dst_file = os.path.join(dst_dir, fname)
                label = (
                    os.path.join("knowledge", rel_dir, fname)
                    if rel_dir != "."
                    else os.path.join("knowledge", fname)
                )
                _check_file(src_file, dst_file, label)

    return result


def _scan_rule_migration(data_dir: str) -> dict:
    """Preview what ``migrate_rule_files`` would do.

    Returns a dict matching the shape of ``migrate_rule_files`` output plus
    ``would_move`` / ``would_skip`` for dry-run clarity.
    """
    result: dict = {"would_move": 0, "would_skip": 0, "details": []}
    memory_root = os.path.join(data_dir, "memory")

    if not os.path.isdir(memory_root):
        return result

    for scope_dir in sorted(os.listdir(memory_root)):
        rules_dir = os.path.join(memory_root, scope_dir, "rules")
        if not os.path.isdir(rules_dir):
            continue

        if scope_dir == "global":
            dest_dir = os.path.join(data_dir, "vault", "system", "playbooks")
        else:
            dest_dir = os.path.join(data_dir, "vault", "projects", scope_dir, "playbooks")

        for filename in sorted(os.listdir(rules_dir)):
            if not filename.endswith(".md"):
                continue
            dest_path = os.path.join(dest_dir, filename)
            if os.path.exists(dest_path):
                result["would_skip"] += 1
                result["details"].append(f"SKIP {scope_dir}/{filename}: already at destination")
            else:
                result["would_move"] += 1
                result["details"].append(f"MOVE {scope_dir}/{filename}")

    return result


def run_vault_migration(
    data_dir: str,
    project_ids: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Run all vault migrations in the correct order (spec §6, Phase 1).

    Consolidates the four individual migration operations into a single
    idempotent entry point that can be called from the CLI
    (``aq vault migrate``) or programmatically.

    **Migration order:**

    1. ``migrate_obsidian_config`` — move ``.obsidian/`` from ``memory/`` to
       ``vault/``
    2. ``ensure_vault_layout`` — create the static vault directory tree
    3. ``ensure_vault_project_dirs`` — per-project vault directories
    4. ``migrate_notes_to_vault`` — per-project notes migration
    5. ``copy_project_memory_to_vault`` — per-project memory file copy
    6. ``migrate_rule_files`` — global and per-project rule migration

    The function is **idempotent** — safe to run multiple times.  It never
    duplicates or overwrites data that is already at the destination.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        project_ids: Explicit list of project IDs to migrate.  If ``None``,
            projects are auto-discovered from ``notes/`` and ``memory/``
            directories.
        dry_run: If ``True``, scan and report what *would* happen without
            making any changes.

    Returns:
        A dict with the migration report::

            {
                "dry_run": bool,
                "data_dir": str,
                "projects_discovered": [str, ...],
                "obsidian": {"action": "move"/"skip", ...},
                "notes": {"project_id": {"moved": N, "skipped": N}, ...},
                "memory": {"project_id": {"copied": N, "updated": N, "skipped": N}, ...},
                "rules": {"moved": N, "skipped": N, "errors": N, "details": [...]},
                "summary": {
                    "total_moved": int,
                    "total_copied": int,
                    "total_skipped": int,
                    "total_errors": int,
                },
                "details": [str, ...],  # human-readable log lines
            }
    """
    if project_ids is None:
        project_ids = _discover_project_ids(data_dir)

    report: dict = {
        "dry_run": dry_run,
        "data_dir": data_dir,
        "projects_discovered": list(project_ids),
        "obsidian": {},
        "notes": {},
        "memory": {},
        "rules": {},
        "summary": {
            "total_moved": 0,
            "total_copied": 0,
            "total_skipped": 0,
            "total_errors": 0,
        },
        "details": [],
    }

    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info("Starting vault migration (%s) for data_dir=%s", mode, data_dir)
    report["details"].append(f"Vault migration ({mode}) — data_dir: {data_dir}")
    report["details"].append(f"Projects: {', '.join(project_ids) or '(none discovered)'}")

    # ------------------------------------------------------------------
    # Step 1: Obsidian config
    # ------------------------------------------------------------------
    if dry_run:
        obs_scan = _scan_obsidian_migration(data_dir)
        report["obsidian"] = obs_scan
        if obs_scan["action"] == "move":
            report["summary"]["total_moved"] += 1
            report["details"].append("  .obsidian: WOULD MOVE → vault/.obsidian/")
        else:
            report["summary"]["total_skipped"] += 1
            report["details"].append(f"  .obsidian: SKIP ({obs_scan['reason']})")
    else:
        moved = migrate_obsidian_config(data_dir)
        report["obsidian"] = {
            "action": "moved" if moved else "skipped",
        }
        if moved:
            report["summary"]["total_moved"] += 1
            report["details"].append("  .obsidian: MOVED → vault/.obsidian/")
        else:
            report["summary"]["total_skipped"] += 1
            report["details"].append("  .obsidian: SKIPPED (already migrated or no source)")

    # ------------------------------------------------------------------
    # Step 2: Ensure vault layout (always needed, even for dry-run reports)
    # ------------------------------------------------------------------
    if not dry_run:
        ensure_vault_layout(data_dir)
        report["details"].append("  Vault layout: ensured")

        # Per-project directories
        for pid in project_ids:
            ensure_vault_project_dirs(data_dir, pid)
    else:
        report["details"].append("  Vault layout: WOULD ensure")

    # ------------------------------------------------------------------
    # Step 3: Notes migration (per-project)
    # ------------------------------------------------------------------
    report["details"].append("  --- Notes migration ---")
    for pid in project_ids:
        if dry_run:
            scan = _scan_notes_migration(data_dir, pid)
            report["notes"][pid] = {
                "would_move": scan["would_move"],
                "would_skip": scan["would_skip"],
            }
            report["summary"]["total_moved"] += scan["would_move"]
            report["summary"]["total_skipped"] += scan["would_skip"]
            if scan["would_move"] or scan["would_skip"]:
                report["details"].append(
                    f"  notes/{pid}: {scan['would_move']} to move, {scan['would_skip']} to skip"
                )
                for f in scan["files"]:
                    report["details"].append(f"    {f}")
        else:
            moved = migrate_notes_to_vault(data_dir, pid)
            report["notes"][pid] = {"moved": moved}
            if moved:
                report["summary"]["total_moved"] += 1
                report["details"].append(f"  notes/{pid}: migrated")
            else:
                report["details"].append(f"  notes/{pid}: skipped (no source or already done)")

    # ------------------------------------------------------------------
    # Step 4: Memory copy (per-project)
    # ------------------------------------------------------------------
    report["details"].append("  --- Memory copy ---")
    for pid in project_ids:
        if dry_run:
            scan = _scan_memory_copy(data_dir, pid)
            report["memory"][pid] = {
                "would_copy": scan["would_copy"],
                "would_update": scan["would_update"],
                "would_skip": scan["would_skip"],
            }
            report["summary"]["total_copied"] += scan["would_copy"] + scan["would_update"]
            report["summary"]["total_skipped"] += scan["would_skip"]
            total = scan["would_copy"] + scan["would_update"] + scan["would_skip"]
            if total:
                report["details"].append(
                    f"  memory/{pid}: {scan['would_copy']} to copy, "
                    f"{scan['would_update']} to update, "
                    f"{scan['would_skip']} up to date"
                )
                for f in scan["files"]:
                    report["details"].append(f"    {f}")
        else:
            copied = copy_project_memory_to_vault(data_dir, pid)
            report["memory"][pid] = {"copied": copied}
            if copied:
                report["summary"]["total_copied"] += 1
                report["details"].append(f"  memory/{pid}: copied")
            else:
                report["details"].append(f"  memory/{pid}: skipped (no source or up to date)")

    # ------------------------------------------------------------------
    # Step 5: Rule file migration
    # ------------------------------------------------------------------
    report["details"].append("  --- Rule migration ---")
    if dry_run:
        scan = _scan_rule_migration(data_dir)
        report["rules"] = {
            "would_move": scan["would_move"],
            "would_skip": scan["would_skip"],
            "details": scan["details"],
        }
        report["summary"]["total_moved"] += scan["would_move"]
        report["summary"]["total_skipped"] += scan["would_skip"]
        if scan["would_move"] or scan["would_skip"]:
            report["details"].append(
                f"  rules: {scan['would_move']} to move, {scan['would_skip']} to skip"
            )
            for d in scan["details"]:
                report["details"].append(f"    {d}")
        else:
            report["details"].append("  rules: nothing to migrate")
    else:
        rule_result = migrate_rule_files(data_dir)
        report["rules"] = rule_result
        report["summary"]["total_moved"] += rule_result["moved"]
        report["summary"]["total_skipped"] += rule_result["skipped"]
        report["summary"]["total_errors"] += rule_result["errors"]
        report["details"].append(
            f"  rules: {rule_result['moved']} moved, "
            f"{rule_result['skipped']} skipped, "
            f"{rule_result['errors']} errors"
        )
        for d in rule_result.get("details", []):
            report["details"].append(f"    {d}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    s = report["summary"]
    summary_line = (
        f"Migration {'preview' if dry_run else 'complete'}: "
        f"{s['total_moved']} moved, {s['total_copied']} copied, "
        f"{s['total_skipped']} skipped, {s['total_errors']} errors"
    )
    report["details"].append(summary_line)
    logger.info(summary_line)

    return report
