"""Transcript reader ABC and shared types.

One :class:`TranscriptEntry` per interesting line of a harness's on-disk
conversation record.  ``type`` is normalized across harnesses to
``user | assistant | system | tool_use | tool_result``; everything else
(harness id, on-disk shape, byte-offset resumption) is the concrete
reader's business.

The ``base_dir`` constructor argument is a deliberate extension of the
design spec.  Production omits it (default :func:`pathlib.Path.home`, which
is what the spec's ``~/.claude/projects/...`` resolves to); tests and the
C3 checkpoint pass a tmp directory so a fake agent's fake transcript
directory does not collide with a real one under ``~/.claude``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

__all__ = ["TranscriptEntry", "TranscriptReader", "parse_iso_ts"]


def parse_iso_ts(raw) -> float:
    """ISO-8601 (with trailing ``Z``) → epoch seconds.  Missing → 0.0.

    Shared because every harness so far stamps its transcript lines this
    way; a reader that does something else is free to not call it.
    """
    if not raw:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).timestamp()
    except Exception:
        return 0.0


#: A completed assistant turn counts as *in-turn* until this many seconds
#: after the last assistant/tool_use entry.  Beyond it, activity is idle.
#: 60 s is short enough that a truly stopped agent is not flagged busy for
#: long, and long enough to bridge the gap between two model turns.
_IN_TURN_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class TranscriptEntry:
    """One normalized event from a harness transcript.

    ``uuid``/``parent_uuid`` come from the harness (Claude's ``uuid`` /
    ``parentUuid``); they are opaque strings, kept only so downstream
    consumers can build a thread view.
    """

    uuid: str
    parent_uuid: str | None
    type: str  # user | assistant | system | tool_use | tool_result
    text: str
    model: str | None
    usage: dict | None
    ts: float


class TranscriptReader(ABC):
    """Abstract base for one harness's on-disk transcript.

    Concrete subclasses set :attr:`harness` (the string used to look them
    up in :func:`resolve_reader`) and implement path resolution +
    incremental read.
    """

    harness: ClassVar[str] = ""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else Path.home()

    def discover_session_key(self, path: Path) -> str | None:
        """The harness's own conversation id for *path*, if it is derivable.

        Claude does not need this — we pin its id with ``--session-id``, so
        the key is known before the file exists.  Codex picks its own UUID
        and only reveals it on disk, so the watcher writes what this returns
        back onto the row.  Default: not derivable.
        """
        return None

    @abstractmethod
    def resolve_path(
        self, work_dir: str, session_key: str | None
    ) -> Path | None:
        """Locate the transcript file for a session, or ``None``."""

    @abstractmethod
    async def read_new(
        self, path: Path, offset: int
    ) -> tuple[list[TranscriptEntry], int]:
        """Parse everything after byte *offset*.  Returns (entries, new_offset)."""

    def infer_activity(self, tail: list[TranscriptEntry]) -> str:
        """Coarse activity signal from a small trailing window.

        Returns ``"in-turn"`` iff the tail ends in an assistant/tool_use
        entry whose timestamp is within :data:`_IN_TURN_WINDOW_SECONDS` of
        now.  Anything else — empty tail, a completed turn (user entry
        last), or an old assistant entry — is ``"idle"``.
        """
        import time

        if not tail:
            return "idle"
        last = tail[-1]
        if last.type not in ("assistant", "tool_use"):
            return "idle"
        if not last.ts:
            return "idle"
        if time.time() - last.ts > _IN_TURN_WINDOW_SECONDS:
            return "idle"
        return "in-turn"
