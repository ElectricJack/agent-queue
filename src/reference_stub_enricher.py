"""Reference stub enricher — LLM-summarizes spec/doc files into vault stubs.

Implements Roadmap 6.3.2: subscribes to ``workspace.spec.changed`` events
emitted by :class:`~src.workspace_spec_watcher.WorkspaceSpecWatcher` and
enriches the generated reference stubs with LLM-produced summaries.

When a spec/doc file is created or modified, this component:

1. Reads the full source document from the workspace.
2. Sends the content to an LLM with a structured prompt requesting:
   - A concise **Summary** of the document
   - **Key Decisions** extracted from the content
   - **Key Interfaces** (functions, classes, APIs, etc.)
3. Updates the existing vault reference stub at
   ``vault/projects/{id}/references/spec-{name}.md``, replacing the
   placeholder sections with the LLM-generated content.

The enricher is intentionally **async and fire-and-forget**: stub enrichment
runs in the background after the WorkspaceSpecWatcher has already written the
basic stub with metadata and excerpt.  If the LLM call fails, the stub
retains its placeholder text and will be retried on the next change.

**Source hash tracking (Roadmap 6.3.3):** Before calling the LLM, the enricher
compares the current source file's content hash against the ``source_hash``
stored in the stub's YAML frontmatter.  If the hashes match *and* the stub has
already been enriched (i.e. placeholder sections have been replaced), the
enrichment is skipped to avoid redundant LLM calls.  A ``force`` parameter
allows bypassing this check when re-enrichment is explicitly desired.

**Stale stub detection (Roadmap 6.3.4):** The :func:`scan_stale_stubs` function
scans all reference stubs under a project's ``references/`` directory and
compares each stub's recorded ``source_hash`` against the current content of the
source file.  Stubs whose source has changed, whose source is missing, or that
remain unenriched are flagged with a status (``stale``, ``missing_source``,
``unenriched``, ``orphaned``, or ``current``).  The companion
:func:`extract_source_path_from_frontmatter` and
:func:`extract_last_synced_from_frontmatter` helpers parse additional frontmatter
fields needed for the scan.

Configuration is via ``MemoryConfig`` fields prefixed with ``stub_enrichment_``.
The LLM provider falls back through:
``stub_enrichment_provider`` -> ``revision_provider`` -> ``"anthropic"``

See ``docs/specs/design/vault.md`` Section 4 for the stub format specification.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.chat_providers.base import ChatProvider
    from src.config import MemoryConfig
    from src.event_bus import EventBus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum characters to send to the LLM.  Prevents excessive token usage
# for very large spec files.  ~20k chars ≈ ~5000 tokens.
DEFAULT_MAX_SOURCE_CHARS = 20_000

# System prompt for the LLM summarization call.
SUMMARIZE_SYSTEM_PROMPT = """\
You are a technical documentation analyst. Your job is to read software \
specification and documentation files and produce structured summaries \
for a knowledge vault.

You produce concise, accurate summaries that capture the essence of a \
document in a format optimized for search and retrieval. Focus on \
architectural decisions, key interfaces, and important concepts."""

# User prompt template.  {content} is replaced with the document text.
SUMMARIZE_USER_PROMPT = """\
Analyze the following technical document and produce a structured summary.

Respond with EXACTLY three sections, using these exact markdown headers:

## Summary
Write a concise summary (2-5 sentences) of what this document defines or describes. \
Focus on the purpose, scope, and most important aspects.

## Key Decisions
List the most important design decisions, constraints, or rules as bullet points. \
Each bullet should be a single concise line. If there are no clear decisions, \
write "- No explicit design decisions documented."

## Key Interfaces
List the key interfaces, classes, functions, APIs, or concepts defined in this \
document as bullet points. Include brief descriptions. If there are no clear \
interfaces, write "- No explicit interfaces documented."

---

Document:

{content}"""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrichmentResult:
    """Result of a single stub enrichment attempt.

    Attributes:
        project_id: The project that owns the stub.
        stub_name: Filename of the stub (e.g. ``spec-orchestrator.md``).
        success: Whether the enrichment succeeded.
        skipped: Whether enrichment was skipped because the source hash
            matched the existing stub and the stub was already enriched.
        summary: The generated summary text (empty on failure/skip).
        key_decisions: The generated key decisions text (empty on failure/skip).
        key_interfaces: The generated key interfaces text (empty on failure/skip).
        error: Error message if enrichment failed, empty on success.
    """

    project_id: str
    stub_name: str
    success: bool
    skipped: bool = False
    summary: str = ""
    key_decisions: str = ""
    key_interfaces: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


def parse_enrichment_response(response_text: str) -> dict[str, str]:
    """Parse the LLM response into structured sections.

    Extracts content under ``## Summary``, ``## Key Decisions``, and
    ``## Key Interfaces`` headers.  Returns a dict with keys
    ``"summary"``, ``"key_decisions"``, ``"key_interfaces"``.

    If a section is missing, its value will be an empty string.
    """
    sections: dict[str, str] = {
        "summary": "",
        "key_decisions": "",
        "key_interfaces": "",
    }

    if not response_text:
        return sections

    # Map header text to dict key
    header_map = {
        "summary": "summary",
        "key decisions": "key_decisions",
        "key interfaces": "key_interfaces",
    }

    # Split on ## headers (case-insensitive)
    # Pattern: ## <header text> followed by content until next ## or end
    pattern = r"##\s+(.+?)(?:\n)(.*?)(?=\n##\s|\Z)"
    matches = re.findall(pattern, response_text, re.DOTALL | re.IGNORECASE)

    for header, content in matches:
        header_lower = header.strip().lower()
        key = header_map.get(header_lower)
        if key:
            sections[key] = content.strip()

    return sections


def update_stub_content(existing_content: str, sections: dict[str, str]) -> str:
    """Update an existing stub's placeholder sections with LLM-generated content.

    Replaces the content under ``## Summary``, ``## Key Decisions``, and
    ``## Key Interfaces`` headers in the existing stub markdown.

    If a section in ``sections`` is empty, the existing content for that
    section is preserved (placeholder or previous enrichment).

    Parameters
    ----------
    existing_content:
        The current content of the stub file.
    sections:
        Dict with keys ``"summary"``, ``"key_decisions"``,
        ``"key_interfaces"`` and their replacement text.

    Returns
    -------
    str
        The updated stub content.
    """
    result = existing_content

    replacements = {
        "Summary": sections.get("summary", ""),
        "Key Decisions": sections.get("key_decisions", ""),
        "Key Interfaces": sections.get("key_interfaces", ""),
    }

    for header, new_content in replacements.items():
        if not new_content:
            continue

        # Match "## Header\n<content>" up to the next "## " or end of file.
        # The content may be a placeholder like "*Summary pending...*"
        pattern = rf"(## {re.escape(header)}\n\n)(.*?)(?=\n## |\Z)"
        replacement = rf"\g<1>{new_content}"
        result = re.sub(pattern, replacement, result, flags=re.DOTALL)

    return result


# ---------------------------------------------------------------------------
# Source hash extraction and stub state detection
# ---------------------------------------------------------------------------


# Placeholder patterns that indicate a stub section has NOT been enriched yet.
_PLACEHOLDER_PATTERNS = (
    "*summary pending",
    "*pending llm extraction",
    "*pending llm extraction.*",
    "*summary pending --",
    "*summary pending —",
)


def extract_source_hash_from_frontmatter(content: str) -> str:
    """Extract the ``source_hash`` value from a stub file's YAML frontmatter.

    Parses the YAML frontmatter delimited by ``---`` lines and returns the
    value of the ``source_hash`` field.  Returns an empty string if the
    field is not found or the content has no valid frontmatter.

    Parameters
    ----------
    content:
        The full text of a stub file.

    Returns
    -------
    str
        The source_hash value, or ``""`` if not found.
    """
    if not content or not content.startswith("---"):
        return ""

    # Find closing frontmatter delimiter
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return ""

    frontmatter = content[3:end_idx]

    # Simple line-based YAML parsing — avoids dependency on pyyaml here
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("source_hash:"):
            value = line[len("source_hash:") :].strip()
            # Remove optional quotes
            if value and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            return value

    return ""


def stub_is_enriched(content: str) -> bool:
    """Check whether a stub's LLM sections contain real (non-placeholder) content.

    Returns ``True`` if at least one of the Summary, Key Decisions, or
    Key Interfaces sections has been filled in with content beyond the
    default placeholders.

    Parameters
    ----------
    content:
        The full text of a stub file.

    Returns
    -------
    bool
        ``True`` if the stub has been enriched, ``False`` if all sections
        still contain placeholders.
    """
    if not content:
        return False

    # Extract the content of each section
    section_pattern = r"## (?:Summary|Key Decisions|Key Interfaces)\n\n(.*?)(?=\n## |\Z)"
    matches = re.findall(section_pattern, content, re.DOTALL)

    for section_content in matches:
        text = section_content.strip().lower()
        if not text:
            continue
        # Check if it's a placeholder
        is_placeholder = any(text.startswith(p) for p in _PLACEHOLDER_PATTERNS)
        if not is_placeholder:
            return True

    return False


def extract_source_path_from_frontmatter(content: str) -> str:
    """Extract the ``source`` path from a stub file's YAML frontmatter.

    Parses the YAML frontmatter delimited by ``---`` lines and returns the
    value of the ``source`` field.  Returns an empty string if the field is
    not found or the content has no valid frontmatter.

    Parameters
    ----------
    content:
        The full text of a stub file.

    Returns
    -------
    str
        The source path, or ``""`` if not found.
    """
    if not content or not content.startswith("---"):
        return ""

    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return ""

    frontmatter = content[3:end_idx]

    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("source:"):
            value = line[len("source:") :].strip()
            # Remove optional quotes
            if value and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            return value

    return ""


def extract_last_synced_from_frontmatter(content: str) -> str:
    """Extract the ``last_synced`` date from a stub file's YAML frontmatter.

    Parses the YAML frontmatter delimited by ``---`` lines and returns the
    value of the ``last_synced`` field (typically an ISO date string like
    ``2026-04-07``).  Returns an empty string if the field is not found.

    Parameters
    ----------
    content:
        The full text of a stub file.

    Returns
    -------
    str
        The last_synced value, or ``""`` if not found.
    """
    if not content or not content.startswith("---"):
        return ""

    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return ""

    frontmatter = content[3:end_idx]

    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("last_synced:"):
            value = line[len("last_synced:") :].strip()
            # Remove optional quotes
            if value and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            return value

    return ""


# ---------------------------------------------------------------------------
# Stale stub detection (Roadmap 6.3.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaleStubInfo:
    """Information about a single reference stub's freshness status.

    Attributes:
        stub_name: Filename of the stub (e.g. ``spec-orchestrator.md``).
        stub_path: Absolute path to the stub file in the vault.
        source_path: Absolute path to the source document (from frontmatter).
        recorded_hash: The ``source_hash`` stored in the stub's frontmatter.
        current_hash: The current SHA-256 prefix of the source file
            (empty if the source file is missing or unreadable).
        last_synced: The ``last_synced`` date from the stub's frontmatter.
        is_enriched: Whether the stub has been enriched with LLM content.
        status: One of ``"current"``, ``"stale"``, ``"missing_source"``,
            ``"unenriched"``, or ``"orphaned"``.

            - ``current``: source_hash matches and stub is enriched.
            - ``stale``: source_hash differs from the live source file.
            - ``missing_source``: the source file no longer exists on disk.
            - ``unenriched``: hashes match but stub still has placeholders.
            - ``orphaned``: no ``source`` or ``source_hash`` in frontmatter.
    """

    stub_name: str
    stub_path: str
    source_path: str = ""
    recorded_hash: str = ""
    current_hash: str = ""
    last_synced: str = ""
    is_enriched: bool = False
    status: str = "current"


@dataclass
class StaleScanResult:
    """Aggregated result from scanning a project's reference stubs.

    Attributes:
        project_id: The project that was scanned.
        stubs: List of :class:`StaleStubInfo` for every stub found.
        total: Total number of stubs scanned.
        stale: Count of stubs with status ``"stale"``.
        missing_source: Count of stubs with status ``"missing_source"``.
        unenriched: Count of stubs with status ``"unenriched"``.
        orphaned: Count of stubs with status ``"orphaned"``.
        current: Count of stubs with status ``"current"``.
    """

    project_id: str
    stubs: list[StaleStubInfo] = field(default_factory=list)
    total: int = 0
    stale: int = 0
    missing_source: int = 0
    unenriched: int = 0
    orphaned: int = 0
    current: int = 0


def scan_stale_stubs(
    vault_projects_dir: str,
    project_id: str,
) -> StaleScanResult:
    """Scan a project's reference stubs and flag those with stale source_hash.

    Walks the ``vault/projects/{project_id}/references/`` directory,
    reads each stub's YAML frontmatter, and compares the recorded
    ``source_hash`` against the current content of the source file.

    Parameters
    ----------
    vault_projects_dir:
        Path to ``vault/projects/`` where project directories live.
    project_id:
        The project to scan.

    Returns
    -------
    StaleScanResult
        Aggregated scan results with per-stub details and summary counts.
    """
    from src.workspace_spec_watcher import compute_content_hash

    refs_dir = os.path.join(vault_projects_dir, project_id, "references")
    result = StaleScanResult(project_id=project_id)

    if not os.path.isdir(refs_dir):
        return result

    for filename in sorted(os.listdir(refs_dir)):
        if not filename.endswith(".md"):
            continue

        stub_path = os.path.join(refs_dir, filename)
        if not os.path.isfile(stub_path):
            continue

        try:
            with open(stub_path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue

        source_path = extract_source_path_from_frontmatter(content)
        recorded_hash = extract_source_hash_from_frontmatter(content)
        last_synced = extract_last_synced_from_frontmatter(content)
        enriched = stub_is_enriched(content)

        # Determine status
        if not source_path or not recorded_hash:
            status = "orphaned"
        elif not os.path.isfile(source_path):
            status = "missing_source"
        else:
            current_hash = compute_content_hash(source_path)
            if current_hash and current_hash != recorded_hash:
                status = "stale"
            elif not enriched:
                status = "unenriched"
            else:
                status = "current"

        # Compute current hash only when source exists (avoid recomputation)
        current_hash_val = ""
        if source_path and os.path.isfile(source_path):
            current_hash_val = compute_content_hash(source_path)

        info = StaleStubInfo(
            stub_name=filename,
            stub_path=stub_path,
            source_path=source_path,
            recorded_hash=recorded_hash,
            current_hash=current_hash_val,
            last_synced=last_synced,
            is_enriched=enriched,
            status=status,
        )
        result.stubs.append(info)

    # Compute summary counts
    result.total = len(result.stubs)
    for info in result.stubs:
        if info.status == "stale":
            result.stale += 1
        elif info.status == "missing_source":
            result.missing_source += 1
        elif info.status == "unenriched":
            result.unenriched += 1
        elif info.status == "orphaned":
            result.orphaned += 1
        elif info.status == "current":
            result.current += 1

    return result


# ---------------------------------------------------------------------------
# Main enricher class
# ---------------------------------------------------------------------------


class ReferenceStubEnricher:
    """Enriches vault reference stubs with LLM-generated summaries.

    Subscribes to ``workspace.spec.changed`` events on the EventBus and
    updates the corresponding vault stub files with structured summaries.

    Parameters
    ----------
    bus:
        EventBus for subscribing to spec change events.
    vault_projects_dir:
        Path to ``vault/projects/`` where stubs are stored.
    config:
        MemoryConfig with ``stub_enrichment_*`` settings.
    provider:
        Optional pre-built ChatProvider.  If ``None``, a provider is
        created from config on first use.
    enabled:
        Master switch.  When ``False``, events are ignored.
    max_source_chars:
        Maximum characters of source document to send to the LLM.
        Longer documents are truncated with a note.
    """

    def __init__(
        self,
        bus: EventBus,
        vault_projects_dir: str,
        config: MemoryConfig,
        *,
        provider: ChatProvider | None = None,
        enabled: bool = True,
        max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
    ) -> None:
        self._bus = bus
        self._vault_projects_dir = vault_projects_dir
        self._config = config
        self._provider = provider
        self._enabled = enabled
        self._max_source_chars = max_source_chars

        # Statistics
        self._total_enriched: int = 0
        self._total_failed: int = 0
        self._total_skipped: int = 0

        # EventBus unsubscribe handle
        self._unsubscribe: callable | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def total_enriched(self) -> int:
        return self._total_enriched

    @property
    def total_failed(self) -> int:
        return self._total_failed

    @property
    def total_skipped(self) -> int:
        return self._total_skipped

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Subscribe to ``workspace.spec.changed`` events on the EventBus.

        Call this once during initialization (after the EventBus is available).
        The subscription is automatically filtered to only fire for
        ``workspace.spec.changed`` events.
        """
        if self._unsubscribe:
            return  # Already subscribed

        self._unsubscribe = self._bus.subscribe(
            "workspace.spec.changed",
            self._on_spec_changed,
        )
        logger.info("ReferenceStubEnricher: subscribed to workspace.spec.changed events")

    def unsubscribe(self) -> None:
        """Unsubscribe from the EventBus."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_spec_changed(self, data: dict) -> None:
        """Handle a ``workspace.spec.changed`` event.

        Reads the full source document, calls the LLM to summarize it,
        and updates the vault stub file.
        """
        if not self._enabled:
            return

        operation = data.get("operation", "")
        if operation == "deleted":
            # Deletions are handled by WorkspaceSpecWatcher — nothing to enrich
            return

        project_id = data.get("project_id", "")
        abs_path = data.get("abs_path", "")
        stub_name = data.get("stub_name", "")
        content_hash = data.get("content_hash", "")

        if not all([project_id, abs_path, stub_name]):
            logger.warning(
                "ReferenceStubEnricher: incomplete event data: %s",
                {k: v for k, v in data.items() if k != "_event_type"},
            )
            return

        logger.info(
            "ReferenceStubEnricher: enriching stub %s for %s/%s",
            stub_name,
            project_id,
            data.get("rel_path", ""),
        )

        result = await self.enrich_stub(
            project_id=project_id,
            abs_path=abs_path,
            stub_name=stub_name,
            content_hash=content_hash,
        )

        if result.skipped:
            logger.info(
                "ReferenceStubEnricher: skipped %s/%s (source unchanged)",
                project_id,
                stub_name,
            )
        elif result.success:
            logger.info(
                "ReferenceStubEnricher: enriched %s/%s",
                project_id,
                stub_name,
            )
        else:
            logger.warning(
                "ReferenceStubEnricher: failed to enrich %s/%s: %s",
                project_id,
                stub_name,
                result.error,
            )

    # ------------------------------------------------------------------
    # Core enrichment logic
    # ------------------------------------------------------------------

    async def enrich_stub(
        self,
        *,
        project_id: str,
        abs_path: str,
        stub_name: str,
        content_hash: str = "",
        force: bool = False,
    ) -> EnrichmentResult:
        """Read a source doc, LLM-summarize it, and update the vault stub.

        This is the main entry point for enrichment.  It can be called
        directly (e.g. for batch re-enrichment) or via the event handler.

        Before calling the LLM, this method compares the current source
        file's content hash against the ``source_hash`` stored in the
        stub's YAML frontmatter.  If the hashes match and the stub has
        already been enriched with non-placeholder content, the LLM call
        is skipped (Roadmap 6.3.3).  Pass ``force=True`` to bypass this
        check and always re-enrich.

        Parameters
        ----------
        project_id:
            The project that owns the stub.
        abs_path:
            Absolute path to the source document in the workspace.
        stub_name:
            Filename of the stub (e.g. ``spec-orchestrator.md``).
        content_hash:
            Pre-computed content hash of the source file.  If empty,
            the hash will be computed from the file at ``abs_path``.
        force:
            If ``True``, skip the hash comparison and always enrich.

        Returns
        -------
        EnrichmentResult
            Result with success status and extracted sections.
        """
        stub_path = os.path.join(
            self._vault_projects_dir,
            project_id,
            "references",
            stub_name,
        )

        # 0. Source hash comparison — skip enrichment if unchanged (6.3.3)
        if not force:
            skip_result = self._check_skip_enrichment(
                abs_path=abs_path,
                stub_path=stub_path,
                content_hash=content_hash,
                project_id=project_id,
                stub_name=stub_name,
            )
            if skip_result is not None:
                return skip_result

        # 1. Read the source document
        source_content = self._read_source(abs_path)
        if not source_content:
            self._total_failed += 1
            return EnrichmentResult(
                project_id=project_id,
                stub_name=stub_name,
                success=False,
                error=f"Could not read source file: {abs_path}",
            )

        # 2. Get or create the LLM provider
        provider = self._get_provider()
        if not provider:
            self._total_failed += 1
            return EnrichmentResult(
                project_id=project_id,
                stub_name=stub_name,
                success=False,
                error="No LLM provider available for stub enrichment",
            )

        # 3. Call the LLM to summarize the document
        try:
            sections = await self._summarize_document(provider, source_content)
        except Exception as e:
            self._total_failed += 1
            return EnrichmentResult(
                project_id=project_id,
                stub_name=stub_name,
                success=False,
                error=f"LLM summarization failed: {e}",
            )

        if not any(sections.values()):
            self._total_failed += 1
            return EnrichmentResult(
                project_id=project_id,
                stub_name=stub_name,
                success=False,
                error="LLM returned empty sections",
            )

        # 4. Update the stub file
        try:
            updated = self._update_stub_file(stub_path, sections)
        except Exception as e:
            self._total_failed += 1
            return EnrichmentResult(
                project_id=project_id,
                stub_name=stub_name,
                success=False,
                error=f"Failed to update stub file: {e}",
            )

        if not updated:
            self._total_failed += 1
            return EnrichmentResult(
                project_id=project_id,
                stub_name=stub_name,
                success=False,
                error=f"Stub file not found: {stub_path}",
            )

        self._total_enriched += 1
        return EnrichmentResult(
            project_id=project_id,
            stub_name=stub_name,
            success=True,
            summary=sections.get("summary", ""),
            key_decisions=sections.get("key_decisions", ""),
            key_interfaces=sections.get("key_interfaces", ""),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_skip_enrichment(
        self,
        *,
        abs_path: str,
        stub_path: str,
        content_hash: str,
        project_id: str,
        stub_name: str,
    ) -> EnrichmentResult | None:
        """Check whether enrichment can be skipped for an unchanged source.

        Compares the source file's content hash against the ``source_hash``
        stored in the stub's YAML frontmatter.  If they match and the stub
        has already been enriched (non-placeholder content), returns an
        ``EnrichmentResult`` with ``skipped=True``.  Otherwise returns
        ``None`` to indicate enrichment should proceed.

        Parameters
        ----------
        abs_path:
            Absolute path to the source document.
        stub_path:
            Absolute path to the stub file in the vault.
        content_hash:
            Pre-computed content hash, or empty to compute on the fly.
        project_id:
            The project identifier.
        stub_name:
            The stub filename.

        Returns
        -------
        EnrichmentResult | None
            A skipped result if enrichment can be avoided, ``None`` otherwise.
        """
        # Read the existing stub
        if not os.path.isfile(stub_path):
            return None  # No stub yet — must enrich

        try:
            with open(stub_path, encoding="utf-8") as f:
                stub_content = f.read()
        except OSError:
            return None  # Can't read stub — proceed with enrichment

        # Extract hash from stub frontmatter
        existing_hash = extract_source_hash_from_frontmatter(stub_content)
        if not existing_hash:
            return None  # No hash recorded — must enrich

        # Compute current source hash if not provided
        source_hash = content_hash
        if not source_hash:
            from src.workspace_spec_watcher import compute_content_hash

            source_hash = compute_content_hash(abs_path)

        if not source_hash:
            return None  # Can't compute hash — proceed with enrichment

        # Compare hashes
        if source_hash != existing_hash:
            return None  # Hash changed — must re-enrich

        # Hashes match — check if stub is already enriched
        if not stub_is_enriched(stub_content):
            return None  # Stub still has placeholders — must enrich

        # All checks passed: skip enrichment
        self._total_skipped += 1
        logger.debug(
            "ReferenceStubEnricher: skipping %s/%s — source_hash %s unchanged",
            project_id,
            stub_name,
            source_hash,
        )
        return EnrichmentResult(
            project_id=project_id,
            stub_name=stub_name,
            success=True,
            skipped=True,
        )

    def _read_source(self, abs_path: str) -> str:
        """Read the full source document, truncating if necessary.

        Returns the document content (possibly truncated) or empty string
        on failure.
        """
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except (OSError, FileNotFoundError):
            return ""

        if not content.strip():
            return ""

        if len(content) > self._max_source_chars:
            truncated = content[: self._max_source_chars]
            truncated += (
                f"\n\n[Document truncated at {self._max_source_chars} characters. "
                f"Original length: {len(content)} characters.]"
            )
            return truncated

        return content

    def _get_provider(self) -> ChatProvider | None:
        """Get or lazily create the LLM chat provider.

        Falls back through: stub_enrichment_provider -> revision_provider -> "anthropic"
        """
        if self._provider is not None:
            return self._provider

        try:
            from src.chat_providers import create_chat_provider
            from src.config import ChatProviderConfig

            provider_name = (
                self._config.stub_enrichment_provider
                or self._config.revision_provider
                or "anthropic"
            )
            model_name = self._config.stub_enrichment_model or self._config.revision_model or ""

            provider_config = ChatProviderConfig(
                provider=provider_name,
                model=model_name,
            )
            self._provider = create_chat_provider(provider_config)
            return self._provider
        except Exception as e:
            logger.warning("ReferenceStubEnricher: failed to create LLM provider: %s", e)
            return None

    async def _summarize_document(
        self,
        provider: ChatProvider,
        content: str,
    ) -> dict[str, str]:
        """Send document content to the LLM and parse the structured response.

        Parameters
        ----------
        provider:
            The chat provider to use for the LLM call.
        content:
            The source document content (possibly truncated).

        Returns
        -------
        dict[str, str]
            Parsed sections: summary, key_decisions, key_interfaces.
        """
        user_prompt = SUMMARIZE_USER_PROMPT.replace("{content}", content)

        response = await provider.create_message(
            messages=[{"role": "user", "content": user_prompt}],
            system=SUMMARIZE_SYSTEM_PROMPT,
            max_tokens=1024,
        )

        text_parts = response.text_parts
        if not text_parts:
            return {"summary": "", "key_decisions": "", "key_interfaces": ""}

        response_text = "\n".join(text_parts)
        return parse_enrichment_response(response_text)

    def _update_stub_file(self, stub_path: str, sections: dict[str, str]) -> bool:
        """Read an existing stub, update its sections, and write it back.

        Returns ``True`` if the file was updated, ``False`` if the stub
        file does not exist.
        """
        if not os.path.isfile(stub_path):
            return False

        try:
            with open(stub_path, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            return False

        updated = update_stub_content(existing, sections)

        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(updated)

        return True
