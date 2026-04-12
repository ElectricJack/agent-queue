"""ChatObserver — passive observation and batching of Discord chat messages.

The ChatObserver implements Stage 1 filtering (keyword-based) to identify
potentially relevant messages, buffers them by channel, and provides batch
readiness detection for Stage 2 LLM analysis.

The two-stage design keeps costs low: Stage 1 is pure Python (no LLM calls),
so the vast majority of trivial messages (greetings, acknowledgements) are
rejected without spending tokens.  Only batches that pass Stage 1 are sent
to the Supervisor's ``observe()`` method for Stage 2 LLM triage.

Stage 1 Filter Logic:

- Reject bot messages.
- Reject messages from unknown projects.
- Pass if message length >= 200 chars (detailed discussion).
- Pass if message contains project-specific term (from project profile).
- Pass if message contains action word (deploy, fix, bug, etc.).
- Pass if message contains custom keyword (from config).
- Otherwise reject (trivial messages like "ok", "thanks").

Batching is driven by a background ``asyncio.Task`` that periodically
checks buffers for readiness (by count or age), then invokes the
``on_batch_ready`` callback.

See ``specs/chat-observer.md`` for the full behavioral specification.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Callable, Awaitable

if TYPE_CHECKING:
    from src.config import ObservationConfig

# Action words that indicate work-relevant messages
_ACTION_WORDS = {
    "deploy",
    "hotfix",
    "fix",
    "bug",
    "crash",
    "error",
    "issue",
    "problem",
    "broken",
    "failing",
    "failed",
    "investigate",
    "debug",
    "urgent",
    "blocker",
    "stuck",
    "help",
    "review",
    "merge",
    "release",
    "ship",
    "revert",
    "rollback",
    "incident",
    "outage",
    "down",
    "timeout",
}

# Minimum message length to auto-pass (detailed discussion)
_LENGTH_THRESHOLD = 200


class ChatObserver:
    """Passive chat observer with Stage 1 keyword filtering and batching.

    Lifecycle: ``start()`` begins the background batch timer; ``stop()``
    cancels it.  Between those calls, the Discord layer feeds messages
    via ``on_message()`` and the batch callback fires automatically.

    Attributes:
        _config: Observation configuration (thresholds, keywords, enabled).
        _project_profiles: Mapping of project_id → set of project terms.
        _buffers: Per-channel message buffers awaiting batch analysis.
        _buffer_timestamps: Timestamp of the first buffered message per channel.
        _on_batch_ready: Async callback invoked when a batch is ready.
    """

    def __init__(
        self,
        config: ObservationConfig,
        project_profiles: dict[str, set[str]] | None = None,
        on_batch_ready: Callable[[int, list[dict]], Awaitable[None]] | None = None,
    ):
        """Initialize the observer.

        Args:
            config: Observation configuration
            project_profiles: Map of project_id -> set of project-specific terms
            on_batch_ready: Callback invoked when a batch is ready for analysis
        """
        self._config = config
        self._project_profiles = project_profiles or {}
        self._buffers: dict[int, list[dict]] = defaultdict(list)
        self._buffer_timestamps: dict[int, float] = {}
        self._on_batch_ready = on_batch_ready
        self._running = False
        self._timer_task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        """Return whether observation is enabled."""
        return self._config.enabled

    def set_project_profiles(self, profiles: dict[str, set[str]]) -> None:
        """Update project profiles (used when profiles change at runtime)."""
        self._project_profiles = profiles

    def passes_stage1(self, message: dict) -> bool:
        """Check if a message passes Stage 1 keyword filter.

        Args:
            message: Message dict with keys: channel_id, project_id, author,
                content, timestamp, is_bot

        Returns:
            True if message should be buffered for batch analysis
        """
        if not self._config.enabled:
            return False

        # Reject bot messages
        if message.get("is_bot", False):
            return False

        # Reject unknown projects
        project_id = message.get("project_id")
        if project_id not in self._project_profiles:
            return False

        content = message.get("content", "").lower()

        # Pass long messages (detailed discussion)
        if len(content) >= _LENGTH_THRESHOLD:
            return True

        # Pass if contains project-specific term
        project_terms = self._project_profiles.get(project_id, set())
        if any(term.lower() in content for term in project_terms):
            return True

        # Pass if contains action word
        words = set(content.split())
        if words & _ACTION_WORDS:
            return True

        # Pass if contains custom keyword
        if any(keyword.lower() in content for keyword in self._config.stage1_keywords):
            return True

        # Otherwise reject
        return False

    def buffer_message(self, message: dict) -> None:
        """Add a message to the buffer for its channel.

        Args:
            message: Message dict (must include channel_id)
        """
        channel_id = message["channel_id"]
        self._buffers[channel_id].append(message)
        if channel_id not in self._buffer_timestamps:
            self._buffer_timestamps[channel_id] = time.time()

    def get_buffer_size(self, channel_id: int) -> int:
        """Return the number of buffered messages for a channel."""
        return len(self._buffers.get(channel_id, []))

    def flush_buffer(self, channel_id: int) -> list[dict]:
        """Remove and return all buffered messages for a channel.

        Args:
            channel_id: Channel to flush

        Returns:
            List of buffered messages (empty if no buffer exists)
        """
        messages = self._buffers.pop(channel_id, [])
        self._buffer_timestamps.pop(channel_id, None)
        return messages

    def has_pending_channels(self) -> bool:
        """Return whether any channels have buffered messages."""
        return len(self._buffers) > 0

    def pending_channel_ids(self) -> list[int]:
        """Return list of channel IDs with buffered messages."""
        return list(self._buffers.keys())

    def on_message(self, message: dict) -> None:
        """Process an incoming message through Stage 1 filter.

        If the message passes the filter, it is buffered. Otherwise it is dropped.

        Args:
            message: Message dict with channel_id, project_id, author, content, etc.
        """
        if self.passes_stage1(message):
            self.buffer_message(message)

    def is_batch_ready(self, channel_id: int) -> bool:
        """Check if a channel's buffer is ready for batch analysis.

        A batch is ready when:
        - Buffer size >= max_buffer_size (count threshold)
        - OR buffer age >= batch_window_seconds (time threshold)

        Args:
            channel_id: Channel to check

        Returns:
            True if the batch should be processed now
        """
        buffer_size = self.get_buffer_size(channel_id)
        if buffer_size == 0:
            return False

        # Ready by count
        if buffer_size >= self._config.max_buffer_size:
            return True

        # Ready by time
        if channel_id in self._buffer_timestamps:
            age = time.time() - self._buffer_timestamps[channel_id]
            if age >= self._config.batch_window_seconds:
                return True

        return False

    async def start(self) -> None:
        """Start the background batch timer loop.

        This is a coroutine.  The loop runs until ``stop()`` is called.
        """
        if not self._config.enabled:
            return
        self._running = True
        self._timer_task = asyncio.create_task(self._batch_loop())

    async def stop(self) -> None:
        """Stop the background batch timer loop.

        This is a coroutine.  Safe to call even if the loop is not running.
        """
        self._running = False
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
            self._timer_task = None

    async def _batch_loop(self) -> None:
        """Background loop that checks for ready batches periodically."""
        while self._running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                await self._check_ready_batches()
            except asyncio.CancelledError:
                break
            except Exception:
                # Log error but keep loop running
                pass

    async def _check_ready_batches(self) -> None:
        """Check all channels for ready batches and process them."""
        for channel_id in list(self._buffers.keys()):
            if self.is_batch_ready(channel_id):
                await self._process_batch(channel_id)

    async def _process_batch(self, channel_id: int) -> None:
        """Process a ready batch by invoking the callback.

        Args:
            channel_id: Channel whose batch is ready
        """
        batch = self.flush_buffer(channel_id)
        if self._on_batch_ready and batch:
            await self._on_batch_ready(channel_id, batch)
