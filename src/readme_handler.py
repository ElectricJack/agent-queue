"""Vault watcher handler for project ``README.md`` files — orchestrator summary generation.

Registers a glob pattern with the :class:`~src.vault_watcher.VaultWatcher` to
detect changes to ``README.md`` files under ``projects/*/`` directories.  When
a project README is created or modified, the handler reads its content and
generates/updates a structured summary in the orchestrator's memory directory
(``vault/orchestrator/memory/project-{id}.md``).  When a README is deleted,
the corresponding summary file is also removed.

On startup, :func:`scan_and_generate_readme_summaries` iterates all existing
project READMEs in the vault and ensures orchestrator summaries are current.
The :class:`~src.vault_watcher.VaultWatcher` only detects *changes* after its
initial snapshot, so the startup scan fills the gap for pre-existing files.

Pattern registered (relative to vault root)::

    projects/*/README.md    — per-project README files

See ``docs/specs/design/self-improvement.md`` Section 5 for the orchestrator
memory model, and ``docs/specs/design/playbooks.md`` Section 17 for the unified
watcher architecture.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob pattern for project README files (relative to vault root).
README_PATTERN: str = "projects/*/README.md"

# Frontmatter template for orchestrator project summary files.
_SUMMARY_FRONTMATTER = """\
---
project_id: "{project_id}"
source: "vault/projects/{project_id}/README.md"
last_updated: "{timestamp}"
type: project-summary
---
"""


@dataclass(frozen=True)
class ReadmeChangeInfo:
    """Parsed change event for a project README file.

    Attributes:
        file_path: Absolute filesystem path to the README file.
        change_type: One of ``"created"``, ``"modified"``, or ``"deleted"``.
        project_id: The project identifier derived from the vault path
            (the directory name under ``projects/``).
    """

    file_path: str
    change_type: str
    project_id: str


@dataclass
class ReadmeScanResult:
    """Result of scanning/generating a single project README summary.

    Attributes:
        project_id: The project identifier.
        success: Whether the summary was generated successfully.
        action: One of ``"created"``, ``"updated"``, ``"skipped"``, ``"removed"``,
            or ``"error"``.
        summary_path: Absolute path to the generated summary file, or empty string.
        errors: List of error messages if the operation failed.
    """

    project_id: str
    success: bool
    action: str
    summary_path: str = ""
    errors: list[str] = field(default_factory=list)


def derive_project_id(rel_path: str) -> str | None:
    """Derive the project ID from a vault-relative README path.

    Expects paths of the form ``projects/<project_id>/README.md``.
    Returns ``None`` if the path does not match the expected structure.

    Parameters
    ----------
    rel_path:
        Path relative to the vault root, e.g.
        ``projects/my-app/README.md``.

    Returns
    -------
    str | None
        The project ID, or ``None`` if the path cannot be parsed.

    Examples
    --------
    >>> derive_project_id("projects/my-app/README.md")
    'my-app'
    >>> derive_project_id("projects/mech-fighters/README.md")
    'mech-fighters'
    >>> derive_project_id("system/README.md")
    """
    parts = rel_path.replace("\\", "/").split("/")

    if len(parts) == 3 and parts[0] == "projects" and parts[2] == "README.md":
        return parts[1]

    return None


def _extract_title(readme_text: str) -> str:
    """Extract the first ``# heading`` from a README as the project title.

    Falls back to an empty string if no heading is found.
    """
    for line in readme_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _extract_section(readme_text: str, heading_pattern: str) -> str:
    """Extract the body of a markdown section whose heading matches *heading_pattern*.

    Searches for a ``## heading`` (level-2) that contains *heading_pattern*
    (case-insensitive), then returns all text until the next ``## `` heading
    or end-of-file.  Returns an empty string if the section is not found.
    """
    lines = readme_text.splitlines()
    collecting = False
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if collecting:
                break  # hit the next section
            if re.search(heading_pattern, stripped, re.IGNORECASE):
                collecting = True
                continue
        elif collecting:
            result.append(line)

    return "\n".join(result).strip()


def generate_summary_content(
    project_id: str,
    readme_text: str,
    *,
    timestamp: str | None = None,
) -> str:
    """Generate a structured orchestrator summary from a project README.

    This is a **lightweight indexer operation** — no LLM calls.  The summary
    captures metadata and key sections extracted from the README in a
    standardised format that the orchestrator can use for cross-project
    understanding.

    The summary includes:
    - YAML frontmatter with project_id, source path, and timestamp
    - Project title (from the ``# heading``)
    - Full README content (for reference and search indexing)

    Parameters
    ----------
    project_id:
        The project identifier (directory name under ``vault/projects/``).
    readme_text:
        Raw text content of the project's ``README.md``.
    timestamp:
        ISO-format timestamp for the ``last_updated`` field.  Defaults to
        the current UTC time.

    Returns
    -------
    str
        Formatted markdown suitable for writing to
        ``vault/orchestrator/memory/project-{id}.md``.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    title = _extract_title(readme_text) or project_id

    frontmatter = _SUMMARY_FRONTMATTER.format(
        project_id=project_id,
        timestamp=timestamp,
    )

    # Build the summary document.
    parts = [
        frontmatter.rstrip(),
        "",
        f"# {title}",
        "",
        readme_text.rstrip(),
        "",
    ]

    return "\n".join(parts) + "\n"


def summary_path_for_project(vault_root: str, project_id: str) -> str:
    """Return the absolute path for a project's orchestrator summary file.

    The summary lives at ``vault/orchestrator/memory/project-{id}.md``.
    """
    return os.path.join(vault_root, "orchestrator", "memory", f"project-{project_id}.md")


def _write_summary(
    vault_root: str,
    project_id: str,
    readme_text: str,
    *,
    timestamp: str | None = None,
) -> str:
    """Generate and write a project summary to the orchestrator memory directory.

    Creates the ``vault/orchestrator/memory/`` directory if needed.

    Returns the absolute path to the written summary file.
    """
    summary = generate_summary_content(project_id, readme_text, timestamp=timestamp)
    summary_file = summary_path_for_project(vault_root, project_id)
    os.makedirs(os.path.dirname(summary_file), exist_ok=True)
    Path(summary_file).write_text(summary, encoding="utf-8")
    return summary_file


def _remove_summary(vault_root: str, project_id: str) -> bool:
    """Remove a project's orchestrator summary file if it exists.

    Returns ``True`` if the file was deleted, ``False`` if it did not exist.
    """
    summary_file = summary_path_for_project(vault_root, project_id)
    try:
        os.remove(summary_file)
        return True
    except FileNotFoundError:
        return False


async def on_readme_changed(
    changes: list[VaultChange],
    *,
    vault_root: str = "",
) -> list[ReadmeScanResult]:
    """Handle project README.md file changes detected by the VaultWatcher.

    For each ``created`` or ``modified`` change, reads the README and
    generates/updates the orchestrator summary at
    ``vault/orchestrator/memory/project-{id}.md``.  For ``deleted`` changes,
    removes the corresponding summary file.

    When *vault_root* is empty (e.g. when called from the VaultWatcher without
    wiring), the handler falls back to deriving the vault root from the
    change's absolute path — the path is expected to contain
    ``vault/projects/{id}/README.md``.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for
        README files that were created, modified, or deleted.
    vault_root:
        Absolute path to the vault root.  If empty, derived from the
        change paths.

    Returns
    -------
    list[ReadmeScanResult]
        One result per change processed.
    """
    results: list[ReadmeScanResult] = []

    for change in changes:
        project_id = derive_project_id(change.rel_path)
        if project_id is None:
            logger.warning(
                "README handler: could not derive project_id from path: %s",
                change.rel_path,
            )
            continue

        # Derive vault_root from the absolute path if not provided.
        effective_root = vault_root
        if not effective_root:
            # change.path ends with e.g. /vault/projects/{id}/README.md
            # change.rel_path is projects/{id}/README.md
            # So vault_root = change.path minus the rel_path suffix
            if change.path.endswith(change.rel_path.replace("/", os.sep)):
                effective_root = change.path[: -len(change.rel_path)].rstrip(os.sep)
            elif change.path.endswith(change.rel_path):
                effective_root = change.path[: -len(change.rel_path)].rstrip("/")

        if change.operation == "deleted":
            removed = _remove_summary(effective_root, project_id) if effective_root else False
            action = "removed" if removed else "skipped"
            results.append(
                ReadmeScanResult(
                    project_id=project_id,
                    success=True,
                    action=action,
                )
            )
            logger.info(
                "README deleted for project %s — orchestrator summary %s",
                project_id,
                action,
            )
            continue

        # created or modified — read and generate summary
        if not effective_root:
            logger.warning(
                "README handler: cannot derive vault_root for project %s — skipping summary",
                project_id,
            )
            results.append(
                ReadmeScanResult(
                    project_id=project_id,
                    success=False,
                    action="error",
                    errors=["Could not derive vault_root from change path"],
                )
            )
            continue

        try:
            readme_text = Path(change.path).read_text(encoding="utf-8")
        except OSError as exc:
            logger.error(
                "README handler: could not read %s: %s",
                change.path,
                exc,
            )
            results.append(
                ReadmeScanResult(
                    project_id=project_id,
                    success=False,
                    action="error",
                    errors=[f"Could not read file: {exc}"],
                )
            )
            continue

        try:
            summary_file = _write_summary(effective_root, project_id, readme_text)
            action = "created" if change.operation == "created" else "updated"
            results.append(
                ReadmeScanResult(
                    project_id=project_id,
                    success=True,
                    action=action,
                    summary_path=summary_file,
                )
            )
            logger.info(
                "README %s for project %s — orchestrator summary %s at %s",
                change.operation,
                project_id,
                action,
                summary_file,
            )
        except OSError as exc:
            logger.error(
                "README handler: could not write summary for %s: %s",
                project_id,
                exc,
            )
            results.append(
                ReadmeScanResult(
                    project_id=project_id,
                    success=False,
                    action="error",
                    errors=[f"Could not write summary: {exc}"],
                )
            )

    return results


def _make_watcher_callback(vault_root: str):
    """Create a VaultWatcher callback bound to a specific *vault_root*.

    The VaultWatcher invokes callbacks with a single ``changes`` argument.
    This factory binds the vault_root so the handler can resolve summary
    paths without guessing.
    """

    async def _callback(changes: list[VaultChange]) -> None:
        await on_readme_changed(changes, vault_root=vault_root)

    return _callback


def register_readme_handlers(
    watcher: VaultWatcher,
    *,
    vault_root: str = "",
) -> str:
    """Register the project README watcher handler on the given *watcher*.

    Registers a single handler for the :data:`README_PATTERN` with
    :func:`on_readme_changed` as the callback.

    Parameters
    ----------
    watcher:
        The :class:`~src.vault_watcher.VaultWatcher` instance to register
        the handler on (typically ``orchestrator.vault_watcher``).
    vault_root:
        Absolute path to the vault root.  When provided, the handler can
        reliably resolve summary output paths.  When empty, the handler
        derives vault_root from each change's absolute path.

    Returns
    -------
    str
        The handler ID assigned by the watcher.
    """
    callback = _make_watcher_callback(vault_root) if vault_root else on_readme_changed
    handler_id = watcher.register_handler(
        README_PATTERN,
        callback,
        handler_id="readme:projects/*/README.md",
    )
    logger.info(
        "Registered README watcher handler for pattern %r (id=%s)",
        README_PATTERN,
        handler_id,
    )
    return handler_id


# ---------------------------------------------------------------------------
# Startup scan — sync existing READMEs to orchestrator summaries
# ---------------------------------------------------------------------------


def _find_project_readmes(vault_root: str) -> list[tuple[str, str]]:
    """Discover all ``vault/projects/*/README.md`` files.

    Returns a list of ``(absolute_path, relative_path)`` tuples.
    """
    results: list[tuple[str, str]] = []
    projects_dir = os.path.join(vault_root, "projects")

    if not os.path.isdir(projects_dir):
        return results

    for entry in sorted(os.listdir(projects_dir)):
        readme_path = os.path.join(projects_dir, entry, "README.md")
        if os.path.isfile(readme_path):
            rel_path = f"projects/{entry}/README.md"
            results.append((readme_path, rel_path))

    return results


async def scan_and_generate_readme_summaries(
    vault_root: str,
) -> list[ReadmeScanResult]:
    """Scan the vault for existing project READMEs and generate orchestrator summaries.

    This is the **startup scan** — called once during orchestrator
    initialization to ensure all pre-existing README files have corresponding
    summaries in ``vault/orchestrator/memory/``.  The
    :class:`~src.vault_watcher.VaultWatcher` only detects *changes* after its
    initial snapshot; this function fills the gap.

    Each README is read and compared against its existing summary (if any).
    Summaries are only (re-)generated when the README content has changed or
    no summary exists yet, keeping startup fast for large vaults.

    Parameters
    ----------
    vault_root:
        Absolute path to the vault root directory.

    Returns
    -------
    list[ReadmeScanResult]
        One result per README file found (both successes and skips).
    """
    readme_files = _find_project_readmes(vault_root)
    if not readme_files:
        logger.debug("Startup README scan: no project READMEs found in %s", vault_root)
        return []

    results: list[ReadmeScanResult] = []

    for abs_path, rel_path in readme_files:
        project_id = derive_project_id(rel_path)
        if project_id is None:
            logger.warning(
                "Startup README scan: could not derive project_id from %s",
                rel_path,
            )
            continue

        # Read the README
        try:
            readme_text = Path(abs_path).read_text(encoding="utf-8")
        except OSError:
            logger.error(
                "Startup README scan: could not read %s",
                abs_path,
                exc_info=True,
            )
            results.append(
                ReadmeScanResult(
                    project_id=project_id,
                    success=False,
                    action="error",
                    errors=[f"Could not read file: {abs_path}"],
                )
            )
            continue

        # Check if summary already exists and is up-to-date.
        # Compare README mtime against summary mtime — skip regeneration
        # if the summary is newer (was generated after the last README edit).
        summary_file = summary_path_for_project(vault_root, project_id)
        if os.path.isfile(summary_file):
            try:
                readme_mtime = os.path.getmtime(abs_path)
                summary_mtime = os.path.getmtime(summary_file)
                if summary_mtime >= readme_mtime:
                    results.append(
                        ReadmeScanResult(
                            project_id=project_id,
                            success=True,
                            action="skipped",
                            summary_path=summary_file,
                        )
                    )
                    logger.debug(
                        "Startup README scan: summary up-to-date for %s",
                        project_id,
                    )
                    continue
            except OSError:
                pass  # If we can't check mtimes, regenerate

        # Generate/update the summary
        try:
            summary_path = _write_summary(vault_root, project_id, readme_text)
            action = "updated" if os.path.isfile(summary_file) else "created"
            results.append(
                ReadmeScanResult(
                    project_id=project_id,
                    success=True,
                    action=action,
                    summary_path=summary_path,
                )
            )
            logger.info(
                "Startup README scan: %s summary for %s at %s",
                action,
                project_id,
                summary_path,
            )
        except OSError as exc:
            logger.error(
                "Startup README scan: could not write summary for %s: %s",
                project_id,
                exc,
            )
            results.append(
                ReadmeScanResult(
                    project_id=project_id,
                    success=False,
                    action="error",
                    errors=[f"Could not write summary: {exc}"],
                )
            )

    generated = sum(1 for r in results if r.success and r.action in ("created", "updated"))
    skipped = sum(1 for r in results if r.action == "skipped")
    failed = sum(1 for r in results if not r.success)
    logger.info(
        "Startup README scan complete: %d README(s) found, %d generated/updated, "
        "%d skipped (up-to-date), %d failed",
        len(results),
        generated,
        skipped,
        failed,
    )
    return results
