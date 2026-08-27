"""Transcript readers — normalize agent CLI transcript files into events.

A transcript reader maps a harness's on-disk conversation record (Claude's
``~/.claude/projects/<slug>/*.jsonl`` files, Codex's ``~/.codex/sessions``,
etc.) into a stream of ``TranscriptEntry`` objects.  ``TranscriptWatcher``
(``watcher.py``) polls them; the SSE endpoint (``src/api/sessions.py``)
replays and tails them.

The reader lives here rather than inside each provider because a provider
knows how a session runs, not how the harness records the conversation —
and Codex's transcripts survive a run under tmux, subprocess, or bare shell
identically.
"""

from __future__ import annotations

from pathlib import Path

from src.sessions.transcripts.base import (
    TranscriptEntry,
    TranscriptReader,
)
from src.sessions.transcripts.claude import ClaudeTranscriptReader
from src.sessions.transcripts.codex import CodexTranscriptReader

__all__ = [
    "TranscriptEntry",
    "TranscriptReader",
    "ClaudeTranscriptReader",
    "CodexTranscriptReader",
    "resolve_reader",
]


def resolve_reader(
    harness: str, base_dir: Path | None = None
) -> TranscriptReader | None:
    """Return a reader for *harness* or ``None`` if unsupported.

    *base_dir* is threaded through so tests (and the C3 checkpoint) can
    point the reader at a temp directory; production callers omit it and
    inherit the spec's ``Path.home()`` default.
    """
    if harness == "claude":
        return ClaudeTranscriptReader(base_dir=base_dir)
    if harness == "codex":
        return CodexTranscriptReader(base_dir=base_dir)
    return None
