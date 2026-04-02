"""Shared type aliases for the messaging abstraction layer.

These callback signatures define how the orchestrator communicates with
whichever messaging transport is active.  They were previously defined
inline in ``src/orchestrator.py`` — now they live here so both the
orchestrator and the transport implementations share the same types.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

# Sends a formatted message to a messaging channel.  The optional project_id
# lets the callback route to per-project channels instead of the global one.
# When a ``notification`` kwarg (RichNotification) is provided, the transport
# should render it in its native rich format (embed, HTML, etc.).
# Legacy ``embed`` / ``view`` kwargs are still accepted during migration.
NotifyCallback = Callable[..., Awaitable[Any]]

# Sends a single message into an already-created thread/topic.
ThreadSendCallback = Callable[[str], Awaitable[None]]

# Creates a thread/topic for streaming agent output and returns two
# send functions: one for posting into the thread itself, and one for
# posting a brief summary/reply to the parent notifications channel.
# Args: (thread_name, initial_message, project_id, task_id)
# Returns: (send_to_thread, notify_main) or None if thread creation failed.
CreateThreadCallback = Callable[
    [str, str, str | None, str | None],
    Awaitable[tuple[ThreadSendCallback, ThreadSendCallback] | None],
]

# Returns a Discord jump URL pointing to the last message in the thread
# for a given task_id, so embeds can link directly to the agent's final
# output instead of showing truncated content.
# Args: (task_id)
# Returns: URL string, or None if no thread/message is available.
GetThreadUrlCallback = Callable[[str], Awaitable[str | None]]

# Edits the thread-root message (the "Agent working: ..." message that
# serves as the parent for the task thread).  Used to update the root
# message text when a task completes or fails.
# Args: (task_id, content | None, embed | None)
# Returns: None
EditThreadRootCallback = Callable[
    [str, str | None, Any],
    Awaitable[None],
]
