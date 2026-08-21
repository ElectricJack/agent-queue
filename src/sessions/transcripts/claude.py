"""Claude Code transcript reader.

Claude writes one JSON object per line into
``~/.claude/projects/<slug>/<session-uuid>.jsonl``, where ``<slug>`` is the
session's ``cwd`` with ``/`` and ``.`` replaced by ``-``.  Each line has a
``type`` (``user`` | ``assistant`` | ``summary`` | ...), a ``uuid`` and a
``parentUuid``, an ISO-8601 ``timestamp``, and a ``message`` object whose
shape mirrors the SDK's ``MessageParam`` (role/content/model/usage).

Reads are byte-offset incremental: the watcher polls every ~2 s, and
appending lines to a JSONL file is atomic-per-line only if you also
tolerate seeing a partial trailing line during the poll.  We do that by
returning the partial line unconsumed so the next tick re-reads it whole.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from src.sessions.transcripts.base import TranscriptEntry, TranscriptReader

logger = logging.getLogger(__name__)

__all__ = ["ClaudeTranscriptReader", "slug_work_dir"]


#: Line ``type`` values we skip entirely (not user-visible content).
_SKIP_TYPES = frozenset({"summary", "queue-operation", "attachment"})


def slug_work_dir(work_dir: str) -> str:
    """Convert ``/home/user/project.git`` to ``-home-user-project-git``.

    Both ``/`` *and* ``.`` become ``-``.  Claude does this so a project
    directory containing a dot (``foo.bar``) does not collide with an
    unrelated ``foo/bar`` — matching that behaviour is the only way the
    reader finds the right slug.
    """
    return str(work_dir).replace("/", "-").replace(".", "-")


def _parse_ts(raw) -> float:
    """ISO-8601 (with trailing ``Z``) → epoch seconds.  Missing → 0.0."""
    if not raw:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        s = str(raw).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()
    except Exception:
        return 0.0


def _extract_text(content) -> str:
    """Flatten Claude's message.content into a plain string.

    Content is either a bare string (older shape / user-typed prompts) or a
    list of blocks (``{"type": "text", "text": "..."}``, ``{"type":
    "tool_use", ...}``, ``{"type": "tool_result", ...}``).  We keep the
    text blocks and stringify the rest so an SSE consumer sees *something*
    for tool activity rather than an empty message.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text") or ""))
        elif btype == "thinking":
            # Thinking is not user-visible content; skip to keep SSE clean.
            continue
        elif btype == "tool_use":
            name = block.get("name") or "tool"
            parts.append(f"[tool_use: {name}]")
        elif btype == "tool_result":
            inner = block.get("content")
            parts.append(
                f"[tool_result] {inner if isinstance(inner, str) else ''}"
            )
    return "".join(parts).strip()


def _entry_from_line(raw: dict) -> TranscriptEntry | None:
    """Convert a decoded JSONL line into an entry, or ``None`` to skip."""
    line_type = str(raw.get("type") or "")
    if not line_type or line_type in _SKIP_TYPES:
        return None
    uuid = str(raw.get("uuid") or "")
    if not uuid:
        return None
    parent = raw.get("parentUuid")
    parent_uuid = None if parent in (None, "") else str(parent)
    ts = _parse_ts(raw.get("timestamp"))
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    model = message.get("model") if isinstance(message, dict) else None
    usage = message.get("usage") if isinstance(message, dict) else None
    text = _extract_text(message.get("content") if isinstance(message, dict) else None)

    return TranscriptEntry(
        uuid=uuid,
        parent_uuid=parent_uuid,
        type=line_type,
        text=text,
        model=str(model) if model else None,
        usage=usage if isinstance(usage, dict) else None,
        ts=ts,
    )


class ClaudeTranscriptReader(TranscriptReader):
    """Reader for the Claude CLI's per-session JSONL files."""

    harness: ClassVar[str] = "claude"

    def resolve_path(
        self, work_dir: str, session_key: str | None
    ) -> Path | None:
        slug = slug_work_dir(work_dir)
        proj_dir = self.base_dir / ".claude" / "projects" / slug
        if not proj_dir.is_dir():
            return None
        if session_key:
            candidate = proj_dir / f"{session_key}.jsonl"
            if candidate.is_file():
                return candidate
            # Fall through: session key given but file not there yet — fall
            # back to mtime rather than returning None so the very first
            # poll (before ``--session-id`` has been written) still sees
            # something.
        files = [p for p in proj_dir.iterdir() if p.suffix == ".jsonl"]
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _read_sync(path: Path, offset: int) -> tuple[bytes, int] | None:
        """Blocking stat + read; returns (buf, size) or None on OSError.

        Kept synchronous so :func:`asyncio.to_thread` can run it off the
        event loop — a slow disk (network mount, contended fs) must not
        stall reconcile or the SSE tail.
        """
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= offset:
            return b"", size
        try:
            with path.open("rb") as f:
                f.seek(offset)
                return f.read(), size
        except OSError:
            return None

    async def read_new(
        self, path: Path, offset: int
    ) -> tuple[list[TranscriptEntry], int]:
        # Run the blocking stat+read off the loop so a slow disk cannot
        # stall the reconciler or a live SSE tail.
        result = await asyncio.to_thread(self._read_sync, path, offset)
        if result is None:
            return [], offset
        buf, _size = result
        if not buf:
            return [], offset

        entries: list[TranscriptEntry] = []
        consumed = 0
        # ``splitlines(keepends=True)`` keeps ``\n`` on complete lines and
        # returns the trailing partial line without a terminator, which is
        # exactly what "tolerate a partial trailing line" needs.
        for chunk in buf.splitlines(keepends=True):
            if not chunk.endswith((b"\n", b"\r\n", b"\r")):
                # Partial — stop and leave it unconsumed for the next tick.
                break
            consumed += len(chunk)
            line = chunk.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("skipping unparseable transcript line in %s", path)
                continue
            if not isinstance(obj, dict):
                continue
            entry = _entry_from_line(obj)
            if entry is not None:
                entries.append(entry)

        return entries, offset + consumed
