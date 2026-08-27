"""Codex CLI transcript reader.

Codex writes one JSON object per line into
``~/.codex/sessions/YYYY/MM/DD/rollout-<iso>-<uuid>.jsonl``.  Two things
make it a different problem from Claude's:

* **The tree is keyed by date, not by ``cwd``.**  There is no slug to
  compute, so the working directory has to be read out of each candidate
  file's first line (``session_meta.payload.cwd``).
* **Codex picks its own conversation UUID** and offers no ``--session-id``
  to pin ours (see ``default_harnesses/codex.md``), so ``session_key`` is
  unknown at launch.  :meth:`CodexTranscriptReader.discover_session_key`
  reads it back off the resolved path; once the watcher writes it to the
  row, resolution is a direct filename match instead of a scan — and the
  harness's deferred ``codex resume <uuid>`` becomes possible.

**Two channels record the same conversation** and mixing them naively
double-counts every turn.  ``event_msg`` is the UI event stream (clean
human-visible text, no system-prompt blob); ``response_item`` is the
model-facing record (the only place tool calls appear, but its ``message``
rows repeat every ``event_msg`` message *and* carry the giant
``<environment_context>`` and developer-instruction frames).  We take text
from ``event_msg`` and tools from ``response_item``, which are disjoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import ClassVar

from src.sessions.transcripts.base import (
    TranscriptEntry,
    TranscriptReader,
    parse_iso_ts,
)

logger = logging.getLogger(__name__)

__all__ = ["CodexTranscriptReader"]

#: ``rollout-2026-08-21T13-28-35-<uuid>.jsonl`` — the trailing UUID is the
#: conversation id ``codex resume`` wants.
_ROLLOUT_UUID = re.compile(
    r"rollout-.*?-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)

#: Upper bound on files examined during a ``cwd`` scan.  A busy host
#: accumulates one rollout per codex run forever; without a cap, resolution
#: cost grows without limit for a session whose transcript is simply not
#: there yet.  Newest-first ordering means the cap only ever hides history.
_MAX_SCAN = 200


def _text_entry(ts: float, uuid: str, kind: str, text: str, turn: str | None):
    return TranscriptEntry(
        uuid=uuid,
        parent_uuid=turn,
        type=kind,
        text=text,
        model=None,
        usage=None,
        ts=ts,
    )


def _usage_from_token_count(info: dict) -> dict | None:
    """``token_count.info`` → the usage shape the token ledger expects.

    Uses ``last_token_usage`` (this turn), never ``total_token_usage``
    (cumulative): the watcher charges once per entry, so cumulative figures
    would re-bill the whole session on every turn.

    ``input_tokens`` from Codex *includes* the cached prefix, while the
    ledger prices ``input_tokens`` and ``cache_read_input_tokens``
    separately — so the cached share is subtracted out rather than counted
    at the full input rate.
    """
    last = info.get("last_token_usage")
    if not isinstance(last, dict):
        return None
    total_in = int(last.get("input_tokens") or 0)
    cached = int(last.get("cached_input_tokens") or 0)
    return {
        "input_tokens": max(total_in - cached, 0),
        "cache_read_input_tokens": cached,
        "output_tokens": int(last.get("output_tokens") or 0),
    }


def _entry_from_line(raw: dict, uuid: str) -> TranscriptEntry | None:
    """One decoded rollout line → an entry, or ``None`` to skip."""
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return None
    ts = parse_iso_ts(raw.get("timestamp"))
    line_type = str(raw.get("type") or "")
    ptype = str(payload.get("type") or "")
    turn = payload.get("turn_id")
    turn_id = str(turn) if turn else None

    if line_type == "event_msg":
        if ptype == "user_message":
            return _text_entry(ts, uuid, "user", str(payload.get("message") or ""), turn_id)
        if ptype == "agent_message":
            return _text_entry(
                ts, uuid, "assistant", str(payload.get("message") or ""), turn_id
            )
        if ptype == "token_count":
            info = payload.get("info")
            usage = _usage_from_token_count(info) if isinstance(info, dict) else None
            if not usage:
                return None
            # Typed ``assistant`` with empty text: the watcher charges usage
            # off assistant entries, and ``_emit_entry`` drops entries with
            # no renderable text — so this bills without adding stream noise.
            return TranscriptEntry(
                uuid=uuid,
                parent_uuid=turn_id,
                type="assistant",
                text="",
                model=None,
                usage=usage,
                ts=ts,
            )
        return None

    if line_type != "response_item":
        # session_meta / turn_context are provenance, not conversation.
        return None

    if ptype in ("function_call", "custom_tool_call"):
        name = payload.get("name") or "tool"
        return _text_entry(ts, uuid, "tool_use", f"[tool_use: {name}]", turn_id)
    if ptype in ("function_call_output", "custom_tool_call_output"):
        out = payload.get("output")
        return _text_entry(
            ts,
            uuid,
            "tool_result",
            f"[tool_result] {out if isinstance(out, str) else ''}".strip(),
            turn_id,
        )
    # ``message`` duplicates event_msg (see the module docstring);
    # ``reasoning`` is encrypted and not user-visible.
    return None


class CodexTranscriptReader(TranscriptReader):
    """Reader for the Codex CLI's date-partitioned rollout files."""

    harness: ClassVar[str] = "codex"

    # -- path resolution ---------------------------------------------------

    @property
    def _sessions_root(self) -> Path:
        return self.base_dir / ".codex" / "sessions"

    def discover_session_key(self, path: Path) -> str | None:
        match = _ROLLOUT_UUID.search(path.stem)
        return match.group(1) if match else None

    def _candidates(self) -> list[Path]:
        """Rollout files, newest first, capped at :data:`_MAX_SCAN`.

        Date directories are walked newest-first so the cap discards old
        history rather than the run we are looking for.
        """
        root = self._sessions_root
        if not root.is_dir():
            return []
        files: list[Path] = []
        try:
            for year in sorted(root.iterdir(), reverse=True):
                if not year.is_dir():
                    continue
                for month in sorted(year.iterdir(), reverse=True):
                    if not month.is_dir():
                        continue
                    for day in sorted(month.iterdir(), reverse=True):
                        if not day.is_dir():
                            continue
                        files.extend(
                            p
                            for p in sorted(day.iterdir(), reverse=True)
                            if p.suffix == ".jsonl" and p.name.startswith("rollout-")
                        )
                        if len(files) >= _MAX_SCAN:
                            return files[:_MAX_SCAN]
        except OSError:
            logger.debug("codex session scan failed under %s", root, exc_info=True)
        return files[:_MAX_SCAN]

    @staticmethod
    def _cwd_of(path: Path) -> str | None:
        """``session_meta.payload.cwd`` from the file's first line."""
        try:
            with path.open("rb") as f:
                first = f.readline(64_000)
        except OSError:
            return None
        try:
            obj = json.loads(first)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(obj, dict) or obj.get("type") != "session_meta":
            return None
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return None
        cwd = payload.get("cwd")
        return str(cwd) if cwd else None

    def resolve_path(self, work_dir: str, session_key: str | None) -> Path | None:
        candidates = self._candidates()
        if not candidates:
            return None
        if session_key:
            # Known conversation id — a direct match, no cwd scan.  This is
            # the steady state once the watcher has written the key back.
            for path in candidates:
                if session_key.lower() in path.stem.lower():
                    return path
            # Fall through rather than returning None: the key may belong to
            # a rolled-off file, and a cwd match is still better than
            # reporting the transcript missing.
        wanted = str(work_dir or "").rstrip("/")
        if not wanted:
            return None
        for path in candidates:
            cwd = self._cwd_of(path)
            if cwd and cwd.rstrip("/") == wanted:
                return path
        return None

    # -- incremental read --------------------------------------------------

    @staticmethod
    def _read_sync(path: Path, offset: int) -> tuple[bytes, int] | None:
        """Blocking stat + read, run via :func:`asyncio.to_thread`."""
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

    async def read_new(self, path: Path, offset: int) -> tuple[list[TranscriptEntry], int]:
        result = await asyncio.to_thread(self._read_sync, path, offset)
        if result is None:
            return [], offset
        buf, _size = result
        if not buf:
            return [], offset

        entries: list[TranscriptEntry] = []
        consumed = 0
        stem = path.stem
        for chunk in buf.splitlines(keepends=True):
            if not chunk.endswith((b"\n", b"\r\n", b"\r")):
                # Partial trailing line — leave it for the next tick.
                break
            line_start = offset + consumed
            consumed += len(chunk)
            line = chunk.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("skipping unparseable codex line in %s", path)
                continue
            if not isinstance(obj, dict):
                continue
            # Codex lines carry no per-line id.  The byte offset of the line
            # start is unique within the file and stable across re-reads,
            # which is exactly what the watcher's ``charged_uuids`` set needs
            # to avoid double-billing a re-read turn.
            entry = _entry_from_line(obj, f"{stem}:{line_start}")
            if entry is not None:
                entries.append(entry)

        return entries, offset + consumed
