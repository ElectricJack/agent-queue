"""Vault directory structure initialization.

Creates the ``~/.agent-queue/vault/`` directory tree described in
``docs/specs/design/vault.md`` §2.  The vault is a structured, human-readable
knowledge base (Obsidian-compatible) that serves as the single source of truth
for system configuration and accumulated intelligence.

The top-level structure is created once at orchestrator startup via
``ensure_vault_structure()``.  Per-profile and per-project subdirectories are
created dynamically as profiles and projects are added.

All directory creation is idempotent — calling any function when the directories
already exist is a safe no-op.
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
        logger.debug(
            "Obsidian config migration: destination %s already exists, skipping", dest
        )
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
