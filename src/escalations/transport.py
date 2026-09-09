"""The narrow port the escalation dispatcher drives, plus an in-memory sink.

An external send can never be transactionally exactly-once with the database
(§7), so the port is designed around saying honestly what happened:

* a normal return carries a :class:`SendOutcome` with a confirmed receipt --
  only then may a delivery be recorded ``sent``;
* :class:`TransportRetryable` is a transient fault (rate limit, gateway
  hiccup) and earns bounded backoff;
* :class:`TransportAmbiguous` is a timeout or a crash *after* the request left
  -- the dispatcher must reconcile against message history before it retries,
  and marks the delivery ``unknown`` when it cannot;
* :class:`TransportUnavailable` is a missing channel or a missing permission:
  an actionable delivery fault, never a reason to create a channel;
* :class:`TransportMissing` means the post or thread this incident is bound to
  is gone, which is the only thing that earns a replacement generation.

Tests use :class:`SinkTransport`; no test may touch a real Discord client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class TransportError(Exception):
    """Base class for every fault the dispatcher classifies."""


class TransportRetryable(TransportError):
    """Transient: try again after backoff."""


class TransportAmbiguous(TransportError):
    """The send may or may not have landed; reconcile before retrying."""


class TransportUnavailable(TransportError):
    """Channel missing, not configured, or the bot lacks permission."""


class TransportMissing(TransportError):
    """The bound root message or thread no longer exists."""


@dataclass(frozen=True)
class SendOutcome:
    """A confirmed external write."""

    receipt_id: str
    channel_id: str | None = None
    root_message_id: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True)
class ThreadHandle:
    """The incident's one thread, and whether *this* call is what made it.

    Thread creation and the opener message are two separate external writes,
    and §7 forbids replaying the second one blindly.  ``created`` is what lets
    the dispatcher tell "brand new and provably empty" (post the opener) from
    "already there" (reconcile the opener's marker first).
    """

    thread_id: str
    created: bool


class EscalationTransport(Protocol):
    """Everything §7 needs from a chat platform, and nothing else."""

    async def post_root(self, *, channel_id: str, content: str) -> SendOutcome: ...

    async def ensure_thread(
        self, *, channel_id: str, root_message_id: str, name: str
    ) -> ThreadHandle:
        """Create — or re-find — the incident's thread.  Sends no message."""
        ...

    async def post_thread_message(self, *, thread_id: str, content: str) -> SendOutcome: ...

    async def edit_root(self, *, channel_id: str, root_message_id: str, content: str) -> None: ...

    async def archive_thread(self, *, thread_id: str) -> None: ...

    async def find_marker(
        self, *, channel_id: str, thread_id: str | None, marker: str
    ) -> SendOutcome | None:
        """Look for an earlier send of ``marker`` in recent message history."""
        ...


@dataclass
class SinkMessage:
    """One message the sink accepted."""

    id: str
    where: str
    content: str
    thread_id: str | None = None
    archived: bool = False


@dataclass
class SinkTransport:
    """Deterministic in-memory transport for tests and dry runs.

    Faults are injected by appending to :attr:`faults`: each entry is keyed by
    the operation name and raised once, in order, so a test can say "the first
    thread creation times out" without patching anything.
    """

    messages: dict[str, SinkMessage] = field(default_factory=dict)
    threads: dict[str, str] = field(default_factory=dict)  # thread id -> root message id
    archived: set[str] = field(default_factory=set)
    edits: list[tuple[str, str]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    faults: list[tuple[str, Exception]] = field(default_factory=list)
    _counter: int = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _maybe_fail(self, operation: str) -> None:
        for index, (name, error) in enumerate(self.faults):
            if name == operation:
                self.faults.pop(index)
                raise error

    def record(self, where: str, content: str, *, thread_id: str | None = None) -> SinkMessage:
        message = SinkMessage(
            id=self._next_id("msg"), where=where, content=content, thread_id=thread_id
        )
        self.messages[message.id] = message
        return message

    async def post_root(self, *, channel_id: str, content: str) -> SendOutcome:
        self.calls.append("post_root")
        self._maybe_fail("post_root")
        message = self.record(channel_id, content)
        return SendOutcome(receipt_id=message.id, channel_id=channel_id, root_message_id=message.id)

    async def ensure_thread(
        self, *, channel_id: str, root_message_id: str, name: str
    ) -> ThreadHandle:
        self.calls.append("ensure_thread")
        self._maybe_fail("ensure_thread")
        if root_message_id not in self.messages:
            raise TransportMissing(f"root {root_message_id} is gone")
        for thread_id, root in self.threads.items():
            if root == root_message_id:
                return ThreadHandle(thread_id=thread_id, created=False)
        thread_id = self._next_id("thread")
        self.threads[thread_id] = root_message_id
        return ThreadHandle(thread_id=thread_id, created=True)

    async def post_thread_message(self, *, thread_id: str, content: str) -> SendOutcome:
        self.calls.append("post_thread_message")
        self._maybe_fail("post_thread_message")
        if thread_id not in self.threads:
            raise TransportMissing(f"thread {thread_id} is gone")
        if thread_id in self.archived:
            raise TransportMissing(f"thread {thread_id} is archived")
        message = self.record(thread_id, content, thread_id=thread_id)
        return SendOutcome(receipt_id=message.id, thread_id=thread_id)

    async def edit_root(self, *, channel_id: str, root_message_id: str, content: str) -> None:
        self.calls.append("edit_root")
        self._maybe_fail("edit_root")
        if root_message_id not in self.messages:
            raise TransportMissing(f"root {root_message_id} is gone")
        self.messages[root_message_id].content = content
        self.edits.append((root_message_id, content))

    async def archive_thread(self, *, thread_id: str) -> None:
        self.calls.append("archive_thread")
        self._maybe_fail("archive_thread")
        if thread_id not in self.threads:
            raise TransportMissing(f"thread {thread_id} is gone")
        self.archived.add(thread_id)

    async def find_marker(
        self, *, channel_id: str, thread_id: str | None, marker: str
    ) -> SendOutcome | None:
        self.calls.append("find_marker")
        self._maybe_fail("find_marker")
        for message in self.messages.values():
            if marker not in message.content:
                continue
            if message.thread_id:
                return SendOutcome(
                    receipt_id=message.id,
                    channel_id=channel_id,
                    root_message_id=self.threads.get(message.thread_id),
                    thread_id=message.thread_id,
                )
            child = next((tid for tid, root in self.threads.items() if root == message.id), None)
            return SendOutcome(
                receipt_id=message.id,
                channel_id=message.where,
                root_message_id=message.id,
                thread_id=child,
            )
        return None

    # -- test helpers ---------------------------------------------------
    def delete(self, message_id: str) -> None:
        """Simulate a human deleting a post."""
        self.messages.pop(message_id, None)
        for thread_id, root in list(self.threads.items()):
            if root == message_id:
                self.threads.pop(thread_id)

    def delete_thread(self, thread_id: str) -> None:
        self.threads.pop(thread_id, None)
