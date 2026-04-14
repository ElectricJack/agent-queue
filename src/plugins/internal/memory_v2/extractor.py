"""Automatic memory extraction from system events.

Subscribes to key EventBus events, buffers them by project, and
periodically runs cheap LLM extraction to populate the vault memory
system with facts, insights, knowledge, and guidance.

Follows the ChatObserver pattern: buffer → filter → batch → process.
Each batch is sent to Gemini Flash with a focused extraction prompt.
Extracted items are routed to the appropriate vault location based
on their type (fact → facts.md, insight → insights/, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.chat_providers.base import ChatProvider
    from src.database.base import DatabaseBackend
    from src.event_bus import EventBus
    from src.plugins.internal.memory_v2.service import MemoryV2Service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Buffered event entry
# ---------------------------------------------------------------------------


@dataclass
class BufferedEvent:
    """A single event waiting to be processed in a batch."""

    event_type: str
    timestamp: float
    project_id: str
    summary: str
    source_ref: str


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a knowledge extractor for a software project's memory system.
Extract concrete, reusable facts from the following system events.

Rules:
- Each item must be self-contained and understandable without context.
- Focus on: architecture decisions, bug patterns, conventions, dependencies,
  configuration, API behaviors, gotchas, what works and what doesn't.
- Skip trivial observations ("task was created", "file was committed").
- Skip transient state ("build is running", "PR is open").
- 1-3 sentences per item.
- Output a JSON array. Output [] if nothing worth saving.

Types:
- fact: Simple key-value pair (test_framework: pytest, default_branch: main)
- insight: An observation or pattern discovered
- knowledge: Deeper understanding of how something works (architecture, design)
- guidance: A prescription or rule for how to work in this project (do/don't)"""

_USER_TEMPLATE = """\
Project: {project_id}

Recent events:
{events_text}

Extract knowledge as JSON:
[
  {{"type": "fact", "key": "example_key", "value": "example_value"}},
  {{"type": "insight", "content": "...", "topic": "..."}},
  {{"type": "knowledge", "content": "...", "topic": "..."}},
  {{"type": "guidance", "content": "...", "topic": "..."}}
]"""

# Trivial error patterns that aren't worth extracting
_TRIVIAL_ERRORS = frozenset({
    "timeout", "rate_limit", "rate limit", "cancelled", "context deadline",
})


# ---------------------------------------------------------------------------
# MemoryExtractor
# ---------------------------------------------------------------------------


class MemoryExtractor:
    """Automatic memory extraction from system events.

    Subscribes to key EventBus events, buffers them by project,
    and periodically runs cheap LLM extraction to populate the
    memory system with facts, insights, knowledge, and guidance.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        db: DatabaseBackend,
        memory_service: MemoryV2Service,
        config: dict[str, Any],
        chat_provider_config: Any,
    ):
        self._bus = bus
        self._db = db
        self._memory = memory_service
        self._config = config
        self._chat_provider_config = chat_provider_config

        # Buffers: project_id → list of BufferedEvent
        self._buffers: dict[str, list[BufferedEvent]] = defaultdict(list)
        self._buffer_first_ts: dict[str, float] = {}

        # Background loop
        self._running = False
        self._task: asyncio.Task | None = None

        # Lazy LLM provider
        self._provider: ChatProvider | None = None

        # Event subscriptions (for cleanup)
        self._unsubs: list[Any] = []

        # Stats
        self.events_received: int = 0
        self.events_buffered: int = 0
        self.batches_processed: int = 0
        self.facts_saved: int = 0
        self.llm_errors: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Subscribe to relevant events on the EventBus."""
        cfg = self._config
        subs = []

        if cfg.get("extract_task_completions", True):
            subs.append(self._bus.subscribe("task.completed", self._on_task_completed))
        if cfg.get("extract_task_failures", True):
            subs.append(self._bus.subscribe("task.failed", self._on_task_failed))
        if cfg.get("extract_chat_messages", True):
            subs.append(self._bus.subscribe("chat.message", self._on_chat_message))
        if cfg.get("extract_git_events", True):
            subs.append(self._bus.subscribe("git.commit", self._on_git_commit))
            subs.append(self._bus.subscribe("git.pr.created", self._on_git_pr))
        if cfg.get("extract_playbook_results", True):
            subs.append(self._bus.subscribe("playbook.run.completed", self._on_playbook_completed))
            subs.append(self._bus.subscribe("playbook.run.failed", self._on_playbook_failed))
        if cfg.get("extract_spec_changes", True):
            subs.append(self._bus.subscribe("workspace.spec.changed", self._on_spec_changed))

        # Supervisor chat completion (new event)
        subs.append(self._bus.subscribe("supervisor.chat.completed", self._on_supervisor_chat))

        self._unsubs = subs
        logger.info("MemoryExtractor subscribed to %d event types", len(subs))

    def unsubscribe(self) -> None:
        """Remove all event subscriptions."""
        for unsub in self._unsubs:
            if callable(unsub):
                unsub()
        self._unsubs.clear()

    async def start(self) -> None:
        """Start the background batch processing loop."""
        self._running = True
        self._task = asyncio.create_task(self._batch_loop())
        logger.info("MemoryExtractor started")

    async def stop(self) -> None:
        """Stop the background loop and flush remaining buffers."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Flush remaining buffers
        for project_id in list(self._buffers.keys()):
            try:
                await self._process_batch(project_id)
            except Exception:
                logger.warning("Flush failed for %s", project_id, exc_info=True)
        logger.info(
            "MemoryExtractor stopped (events=%d, batches=%d, facts=%d)",
            self.events_received, self.batches_processed, self.facts_saved,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_task_completed(self, data: dict) -> None:
        self.events_received += 1
        project_id = data.get("project_id")
        task_id = data.get("task_id")
        if not project_id or not task_id:
            return

        title = data.get("title", "")
        # Async fetch task result for richer context
        summary = ""
        files = []
        try:
            result = await self._db.get_task_result(task_id)
            if result:
                summary = getattr(result, "summary", "") or ""
                files_raw = getattr(result, "files_changed", None)
                if files_raw:
                    files = json.loads(files_raw) if isinstance(files_raw, str) else files_raw
        except Exception:
            pass

        text = f'Task completed: "{title}"'
        if summary:
            text += f"\nSummary: {summary}"
        if files:
            text += f"\nFiles changed: {', '.join(str(f) for f in files[:15])}"

        self._buffer_event(project_id, text, f"task:{task_id}", "task.completed")

    async def _on_task_failed(self, data: dict) -> None:
        self.events_received += 1
        project_id = data.get("project_id")
        if not project_id:
            return

        error = data.get("error", "") or data.get("context", "") or ""
        # Skip trivial errors
        if any(t in error.lower() for t in _TRIVIAL_ERRORS):
            return
        if len(error) < 20:
            return

        title = data.get("title", "")
        text = f'Task failed: "{title}"\nError: {error[:500]}'
        self._buffer_event(project_id, text, f"task:{data.get('task_id', '')}", "task.failed")

    async def _on_supervisor_chat(self, data: dict) -> None:
        self.events_received += 1
        project_id = data.get("project_id")
        if not project_id:
            return

        tools_used = data.get("tools_used", [])
        # Skip pure Q&A with no tool use (low signal)
        if not tools_used:
            return

        user_text = data.get("user_text", "")
        response = data.get("response", "")
        tools_str = ", ".join(str(t) for t in tools_used)

        text = f"Supervisor interaction:\nUser: {user_text}\nTools: {tools_str}\nResponse: {response}"
        self._buffer_event(project_id, text, "supervisor", "supervisor.chat")

    async def _on_chat_message(self, data: dict) -> None:
        self.events_received += 1
        # Reject bot messages
        if data.get("is_bot"):
            return
        project_id = data.get("project_id")
        if not project_id:
            return
        content = data.get("content", "")

        author = data.get("author", "user")
        text = f"{author}: {content}"
        self._buffer_event(project_id, text, f"chat:{data.get('channel_id', '')}", "chat.message")

    async def _on_git_commit(self, data: dict) -> None:
        self.events_received += 1
        project_id = data.get("project_id")
        if not project_id:
            return

        branch = data.get("branch", "")
        message = data.get("message", "")
        files = data.get("changed_files", [])

        text = f"Git commit on {branch}: {message}"
        if files:
            text += f"\nFiles: {', '.join(str(f) for f in files[:15])}"
        self._buffer_event(project_id, text, f"git:{data.get('commit_hash', '')[:8]}", "git.commit")

    async def _on_git_pr(self, data: dict) -> None:
        self.events_received += 1
        project_id = data.get("project_id")
        if not project_id:
            return

        text = (
            f"PR created: \"{data.get('title', '')}\" "
            f"on branch {data.get('branch', '')}\n"
            f"URL: {data.get('pr_url', '')}"
        )
        self._buffer_event(project_id, text, f"pr:{data.get('pr_url', '')}", "git.pr")

    async def _on_playbook_completed(self, data: dict) -> None:
        self.events_received += 1
        project_id = data.get("project_id")
        final_context = data.get("final_context", "")
        if not project_id or not final_context:
            return

        text = (
            f"Playbook \"{data.get('playbook_id', '')}\" completed.\n"
            f"Result: {str(final_context)[:500]}"
        )
        self._buffer_event(project_id, text, f"playbook:{data.get('run_id', '')}", "playbook.completed")

    async def _on_playbook_failed(self, data: dict) -> None:
        self.events_received += 1
        project_id = data.get("project_id")
        if not project_id:
            return

        text = (
            f"Playbook \"{data.get('playbook_id', '')}\" failed.\n"
            f"Error: {data.get('error', 'unknown')[:500]}"
        )
        self._buffer_event(project_id, text, f"playbook:{data.get('run_id', '')}", "playbook.failed")

    async def _on_spec_changed(self, data: dict) -> None:
        self.events_received += 1
        project_id = data.get("project_id")
        if not project_id:
            return

        text = f"Spec changed: {data.get('rel_path', 'unknown')} in project {project_id}"
        self._buffer_event(project_id, text, f"spec:{data.get('rel_path', '')}", "spec.changed")

    # ------------------------------------------------------------------
    # Buffering
    # ------------------------------------------------------------------

    def _buffer_event(
        self, project_id: str, summary: str, source_ref: str, event_type: str
    ) -> None:
        """Add an event to the project buffer."""
        self._buffers[project_id].append(
            BufferedEvent(
                event_type=event_type,
                timestamp=time.time(),
                project_id=project_id,
                summary=summary,
                source_ref=source_ref,
            )
        )
        if project_id not in self._buffer_first_ts:
            self._buffer_first_ts[project_id] = time.time()
        self.events_buffered += 1

    def _is_batch_ready(self, project_id: str) -> bool:
        """Check if a project buffer should be flushed."""
        buf = self._buffers.get(project_id)
        if not buf:
            return False
        max_size = self._config.get("max_buffer_size", 15)
        window = self._config.get("batch_window_seconds", 120)
        if len(buf) >= max_size:
            return True
        first_ts = self._buffer_first_ts.get(project_id, 0)
        if first_ts and (time.time() - first_ts) >= window:
            return True
        return False

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    async def _process_batch(self, project_id: str) -> None:
        """Process a project's buffered events: extract and save."""
        events = self._buffers.pop(project_id, [])
        self._buffer_first_ts.pop(project_id, None)
        if not events:
            return

        provider = self._get_provider()
        if not provider:
            logger.warning("MemoryExtractor: no LLM provider available, skipping batch")
            return

        # Build batch text
        max_chars = self._config.get("max_input_chars", 8000)
        event_texts = []
        chars = 0
        for ev in events:
            if chars + len(ev.summary) > max_chars:
                break
            event_texts.append(f"[{ev.event_type}] {ev.summary}")
            chars += len(ev.summary)

        events_text = "\n---\n".join(event_texts)
        user_msg = _USER_TEMPLATE.format(project_id=project_id, events_text=events_text)

        # LLM extraction
        try:
            resp = await provider.create_message(
                messages=[{"role": "user", "content": user_msg}],
                system=_SYSTEM_PROMPT,
                max_tokens=1024,
            )
            text = "\n".join(resp.text_parts).strip()
            items = self._parse_response(text)
        except Exception:
            logger.warning("MemoryExtractor: LLM extraction failed for %s", project_id, exc_info=True)
            self.llm_errors += 1
            return

        self.batches_processed += 1

        if not items:
            return

        # Save each extracted item
        source_refs = ", ".join(ev.source_ref for ev in events[:5])
        for item in items:
            try:
                await self._save_item(project_id, item, source_refs)
                self.facts_saved += 1
            except Exception:
                logger.warning(
                    "MemoryExtractor: failed to save item for %s: %s",
                    project_id, item, exc_info=True,
                )

        logger.info(
            "MemoryExtractor: extracted %d items from %d events for '%s'",
            len(items), len(events), project_id,
        )

    async def _save_item(self, project_id: str, item: dict, source_refs: str) -> None:
        """Route an extracted item to the appropriate memory store."""
        item_type = item.get("type", "insight")

        if item_type == "fact":
            # KV fact → goes to facts.md, loaded as L1 context
            key = item.get("key", "")
            value = item.get("value", "")
            if key and value:
                await self._memory.kv_set(
                    project_id=project_id,
                    namespace="project",
                    key=key,
                    value=value,
                )
        else:
            # insight / knowledge / guidance → vault document
            content = item.get("content", "")
            if not content:
                return

            # Dedup check: search for similar existing memories
            try:
                existing = await self._memory.search(
                    project_id=project_id,
                    query=content,
                    top_k=1,
                )
                if existing:
                    best = existing[0]
                    similarity = best.get("score", 0)
                    if similarity > 0.85:
                        logger.info(
                            "MemoryExtractor: skipping duplicate (%.2f similarity): %s",
                            similarity, content[:80],
                        )
                        return
            except Exception:
                pass  # search failed, save anyway

            topic = item.get("topic")
            tags = ["auto-extracted", item_type]
            if topic:
                tags.append(topic)

            await self._memory.save_document(
                project_id=project_id,
                content=content,
                tags=tags,
                topic=topic,
                source_task=source_refs,
                source_playbook="memory-extractor",
            )

    def _parse_response(self, text: str) -> list[dict]:
        """Parse LLM response into list of extracted items."""
        # Try direct JSON parse
        try:
            result = json.loads(text)
            if isinstance(result, list):
                max_items = self._config.get("max_facts_per_batch", 10)
                return result[:max_items]
        except (json.JSONDecodeError, TypeError):
            pass

        # Try extracting from markdown code fence
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(1))
                if isinstance(result, list):
                    max_items = self._config.get("max_facts_per_batch", 10)
                    return result[:max_items]
            except (json.JSONDecodeError, TypeError):
                pass

        return []

    # ------------------------------------------------------------------
    # LLM provider (lazy)
    # ------------------------------------------------------------------

    def _get_provider(self) -> ChatProvider | None:
        """Get or create the LLM provider for extraction."""
        if self._provider is not None:
            return self._provider

        try:
            from src.chat_providers import create_chat_provider

            cfg = self._chat_provider_config
            self._provider = create_chat_provider(cfg)
            return self._provider
        except Exception:
            logger.warning("MemoryExtractor: failed to create LLM provider", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _batch_loop(self) -> None:
        """Periodically check buffers and process ready batches."""
        while self._running:
            try:
                await asyncio.sleep(15)  # check every 15 seconds
                for project_id in list(self._buffers.keys()):
                    if self._is_batch_ready(project_id):
                        try:
                            await self._process_batch(project_id)
                        except Exception:
                            logger.warning(
                                "MemoryExtractor: batch failed for %s",
                                project_id,
                                exc_info=True,
                            )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("MemoryExtractor: batch loop error", exc_info=True)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return extraction statistics."""
        return {
            "events_received": self.events_received,
            "events_buffered": self.events_buffered,
            "batches_processed": self.batches_processed,
            "facts_saved": self.facts_saved,
            "llm_errors": self.llm_errors,
            "pending_buffers": {
                pid: len(buf) for pid, buf in self._buffers.items() if buf
            },
        }
