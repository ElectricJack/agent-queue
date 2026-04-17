#!/usr/bin/env python3
"""Migrate task record files from memory to standalone tasks directory.

Part of vault migration Phase 1 (see ``docs/specs/design/vault.md`` Section 6).
Moves task record markdown files from the legacy location
``{data_dir}/memory/{project_id}/tasks/`` to the new location
``{data_dir}/tasks/{project_id}/``.

Properties:
- **Byte-for-byte**: files are copied then verified before removing the source.
- **Idempotent**: running twice has no effect — already-migrated files are skipped.
- **Graceful**: empty source directories are handled without error.
- **Logged**: every action (move, skip, error) is printed.

Usage:
    # Dry run (default) — shows what would be migrated
    python scripts/migrate_task_records.py

    # Actually execute the migration
    python scripts/migrate_task_records.py --execute

    # Custom data directory
    python scripts/migrate_task_records.py --data-dir /path/to/data --execute
"""

from __future__ import annotations

import argparse
import filecmp
import logging
import os
import shutil
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = os.path.expanduser("~/.agent-queue")


def discover_source_projects(data_dir: str) -> list[str]:
    """Return project IDs that have a ``memory/{project}/tasks/`` directory."""
    memory_root = os.path.join(data_dir, "memory")
    if not os.path.isdir(memory_root):
        logger.info("No memory directory found at %s — nothing to migrate", memory_root)
        return []

    projects: list[str] = []
    for entry in sorted(os.listdir(memory_root)):
        tasks_dir = os.path.join(memory_root, entry, "tasks")
        if os.path.isdir(tasks_dir):
            projects.append(entry)
    return projects


def migrate_project(
    data_dir: str,
    project_id: str,
    *,
    execute: bool = False,
) -> tuple[int, int, int]:
    """Migrate task files for a single project.

    Returns ``(moved, skipped, errors)`` counts.
    """
    src_dir = os.path.join(data_dir, "memory", project_id, "tasks")
    dst_dir = os.path.join(data_dir, "tasks", project_id)

    if not os.path.isdir(src_dir):
        logger.info("  [%s] Source directory does not exist — skipping", project_id)
        return 0, 0, 0

    files = sorted(f for f in os.listdir(src_dir) if os.path.isfile(os.path.join(src_dir, f)))
    if not files:
        logger.info("  [%s] Source directory is empty — nothing to migrate", project_id)
        return 0, 0, 0

    moved = 0
    skipped = 0
    errors = 0

    for filename in files:
        src_path = os.path.join(src_dir, filename)
        dst_path = os.path.join(dst_dir, filename)

        # Already exists at destination?
        if os.path.exists(dst_path):
            if filecmp.cmp(src_path, dst_path, shallow=False):
                # Identical — source is a leftover from a previous partial run.
                # Remove the source to complete the migration.
                if execute:
                    os.remove(src_path)
                    logger.info(
                        "  [%s] %s — already at destination (identical), removed source",
                        project_id,
                        filename,
                    )
                else:
                    logger.info(
                        "  [%s] %s — already at destination (identical), would remove source",
                        project_id,
                        filename,
                    )
                skipped += 1
            else:
                # Content differs — don't overwrite, flag for manual review.
                logger.warning(
                    "  [%s] %s — EXISTS at destination with DIFFERENT content, skipping"
                    " (manual review needed)",
                    project_id,
                    filename,
                )
                errors += 1
            continue

        # Move: copy → verify → remove source
        if execute:
            os.makedirs(dst_dir, exist_ok=True)
            try:
                shutil.copy2(src_path, dst_path)
            except OSError as exc:
                logger.error("  [%s] %s — copy failed: %s", project_id, filename, exc)
                errors += 1
                continue

            # Verify byte-for-byte
            if not filecmp.cmp(src_path, dst_path, shallow=False):
                logger.error(
                    "  [%s] %s — verification failed (content mismatch after copy),"
                    " leaving source intact",
                    project_id,
                    filename,
                )
                # Clean up the bad destination copy
                try:
                    os.remove(dst_path)
                except OSError:
                    pass
                errors += 1
                continue

            # Copy verified — remove source
            os.remove(src_path)
            logger.info("  [%s] %s — moved", project_id, filename)
        else:
            logger.info("  [%s] %s — would move", project_id, filename)

        moved += 1

    return moved, skipped, errors


def run_migration(data_dir: str, *, execute: bool = False) -> bool:
    """Run the full migration across all projects.

    Returns ``True`` if migration completed without errors.
    """
    mode = "EXECUTING" if execute else "DRY RUN"
    logger.info("Task record migration — %s", mode)
    logger.info("Data directory: %s", data_dir)
    logger.info("")

    projects = discover_source_projects(data_dir)
    if not projects:
        logger.info("No projects with task records found — nothing to do.")
        return True

    logger.info("Found %d project(s) with task directories: %s", len(projects), ", ".join(projects))
    logger.info("")

    total_moved = 0
    total_skipped = 0
    total_errors = 0

    for project_id in projects:
        moved, skipped, errors = migrate_project(data_dir, project_id, execute=execute)
        total_moved += moved
        total_skipped += skipped
        total_errors += errors

    logger.info("")
    logger.info("--- Summary ---")
    logger.info("  Moved:   %d", total_moved)
    logger.info("  Skipped: %d (already at destination)", total_skipped)
    logger.info("  Errors:  %d", total_errors)

    if not execute and total_moved > 0:
        logger.info("")
        logger.info("This was a dry run. Re-run with --execute to perform the migration.")

    return total_errors == 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate task records from memory/*/tasks/ to tasks/*/",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="Agent Queue data directory (default: %(default)s)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the migration (default is dry run)",
    )
    args = parser.parse_args()

    success = run_migration(args.data_dir, execute=args.execute)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
