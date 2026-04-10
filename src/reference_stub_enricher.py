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

Configuration is via ``MemoryConfig`` fields prefixed with ``stub_enrichment_``.
The LLM provider falls back through:
``stub_enrichment_provider`` -> ``revision_provider`` -> ``"anthropic"``

See ``docs/specs/design/vault.md`` Section 4 for the stub format specification.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
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
        summary: The generated summary text (empty on failure).
        key_decisions: The generated key decisions text (empty on failure).
        key_interfaces: The generated key interfaces text (empty on failure).
        error: Error message if enrichment failed, empty on success.
    """

    project_id: str
    stub_name: str
    success: bool
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
        )

        if result.success:
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
    ) -> EnrichmentResult:
        """Read a source doc, LLM-summarize it, and update the vault stub.

        This is the main entry point for enrichment.  It can be called
        directly (e.g. for batch re-enrichment) or via the event handler.

        Parameters
        ----------
        project_id:
            The project that owns the stub.
        abs_path:
            Absolute path to the source document in the workspace.
        stub_name:
            Filename of the stub (e.g. ``spec-orchestrator.md``).

        Returns
        -------
        EnrichmentResult
            Result with success status and extracted sections.
        """
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
        stub_path = os.path.join(
            self._vault_projects_dir,
            project_id,
            "references",
            stub_name,
        )

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
            model_name = (
                self._config.stub_enrichment_model
                or self._config.revision_model
                or ""
            )

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
