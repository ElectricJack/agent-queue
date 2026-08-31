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


def _text_entry(
    ts: float, uuid: str, kind: str, text: str, turn: str | None, turn_complete: bool = False
):
    return TranscriptEntry(
        uuid=uuid,
        parent_uuid=turn,
        type=kind,
        text=text,
        model=None,
        usage=None,
        ts=ts,
        turn_complete=turn_complete,
    )


def _assistant_output_text(payload: dict) -> str:
    """Return only visible assistant output blocks from a response item."""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "output_text"
    ).strip()


def _legacy_message_in_recent_prefix(path: Path, end: int, text: str, phase: str) -> bool:
    """Whether a nearby legacy event already rendered this response text.

    The two Codex message encodings are adjacent in practice.  Limiting this
    compatibility lookup keeps ordinary incremental commentary reads bounded.
    """
    try:
        with path.open("rb") as file:
            start = max(0, end - 262_144)
            file.seek(start)
            data = file.read(end - start)
    except OSError:
        return False
    for chunk in data.splitlines():
        try:
            raw = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        payload = raw.get("payload") if isinstance(raw, dict) else None
        if not isinstance(payload, dict):
            continue
        if raw.get("type") != "event_msg" or payload.get("type") != "agent_message":
            continue
        if (
            str(payload.get("phase") or "commentary") == phase
            and str(payload.get("message") or "") == text
        ):
            return True
    return False


def _prefix_final_state(
    path: Path, end: int
) -> tuple[str | None, dict[str | None, tuple[str, float, str]]]:
    """Recover one incremental read's final-answer context from its prefix.

    Readers are recreated by the watcher.  A later task-complete event must
    therefore recover a final answer from preceding bytes, but only once per
    batch rather than once for every completion in a cold replay.
    """
    try:
        with path.open("rb") as file:
            data = file.read(end)
    except OSError:
        return None, {}

    active_turn = None
    finals: dict[str | None, tuple[str, float, str]] = {}
    consumed = 0
    for chunk in data.splitlines(keepends=True):
        line_start = consumed
        consumed += len(chunk)
        try:
            raw = json.loads(chunk.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            continue
        line_type = raw.get("type")
        payload_type = payload.get("type")
        turn = payload.get("turn_id")
        turn_id = str(turn) if turn else None
        if line_type == "event_msg" and payload_type == "task_started":
            active_turn = turn_id
            continue
        phase = str(payload.get("phase") or "commentary")
        final_text = ""
        if line_type == "event_msg" and payload_type == "agent_message" and phase == "final_answer":
            final_text = str(payload.get("message") or "").strip()
        elif (
            line_type == "response_item"
            and payload_type == "message"
            and payload.get("role") == "assistant"
            and phase == "final_answer"
        ):
            final_text = _assistant_output_text(payload)
        if final_text:
            finals[turn_id or active_turn] = (
                f"{path.stem}:{line_start}",
                parse_iso_ts(raw.get("timestamp")),
                final_text,
            )
    return active_turn, finals


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
            return _text_entry(ts, uuid, "assistant", str(payload.get("message") or ""), turn_id)
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
        buf, size = result
        if size < offset:
            # A reused session path was truncated; re-read from its start.
            result = await asyncio.to_thread(self._read_sync, path, 0)
            if result is None:
                return [], offset
            buf, _size = result
            offset = 0
        if not buf:
            return [], offset

        active_turn: str | None = None
        finals: dict[str | None, tuple[str, float, str]] = {}
        # A restart can resume at the task-complete line. Recover the prefix
        # once for this batch; normal commentary-only ticks never pay for it.
        if offset and b"task_complete" in buf:
            active_turn, finals = await asyncio.to_thread(_prefix_final_state, path, offset)

        entries: list[TranscriptEntry] = []
        visible_here: set[tuple[str, str]] = set()
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
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            line_type = str(obj.get("type") or "")
            payload_type = str(payload.get("type") or "")
            turn = payload.get("turn_id")
            turn_id = str(turn) if turn else None
            line_uuid = f"{stem}:{line_start}"

            if line_type == "event_msg" and payload_type == "task_started":
                active_turn = turn_id
                continue

            if (
                line_type == "event_msg"
                and payload_type == "agent_message"
                and str(payload.get("phase") or "commentary") == "final_answer"
            ):
                final_text = str(payload.get("message") or "").strip()
                if final_text:
                    finals[turn_id or active_turn] = (
                        line_uuid,
                        parse_iso_ts(obj.get("timestamp")),
                        final_text,
                    )
                continue

            if line_type == "event_msg" and payload_type == "item_completed":
                item = payload.get("item")
                if isinstance(item, dict) and item.get("type") == "UserMessage":
                    content = item.get("content")
                    user_text = (
                        "".join(
                            str(block.get("text") or "")
                            for block in content
                            if isinstance(block, dict)
                            and str(block.get("type") or "").lower() == "text"
                        ).strip()
                        if isinstance(content, list)
                        else ""
                    )
                    if user_text:
                        entries.append(
                            _text_entry(
                                parse_iso_ts(obj.get("timestamp")),
                                line_uuid,
                                "user",
                                user_text,
                                turn_id,
                            )
                        )
                continue

            if line_type == "event_msg" and payload_type == "task_complete":
                completed_turn = turn_id or active_turn
                final = finals.pop(completed_turn, None)
                if final is None and completed_turn is None:
                    final = finals.pop(active_turn, None)
                if final is None:
                    final_text = str(payload.get("last_agent_message") or "").strip()
                    if final_text:
                        final = (line_uuid, parse_iso_ts(obj.get("timestamp")), final_text)
                if final is not None:
                    uuid, ts, final_text = final
                    entries.append(
                        _text_entry(
                            ts,
                            uuid,
                            "assistant",
                            final_text,
                            completed_turn,
                            turn_complete=True,
                        )
                    )
                active_turn = None
                continue

            if (
                line_type == "response_item"
                and payload_type == "message"
                and payload.get("role") == "assistant"
            ):
                phase = str(payload.get("phase") or "commentary")
                response_text = _assistant_output_text(payload)
                if phase == "final_answer":
                    if response_text:
                        finals[turn_id or active_turn] = (
                            line_uuid,
                            parse_iso_ts(obj.get("timestamp")),
                            response_text,
                        )
                    continue
                if not response_text:
                    continue
                key = (phase, response_text)
                duplicate = key in visible_here
                if not duplicate and offset:
                    duplicate = await asyncio.to_thread(
                        _legacy_message_in_recent_prefix, path, line_start, response_text, phase
                    )
                if not duplicate:
                    visible_here.add(key)
                    entries.append(
                        _text_entry(
                            parse_iso_ts(obj.get("timestamp")),
                            line_uuid,
                            "assistant",
                            response_text,
                            turn_id,
                        )
                    )
                continue

            entry = _entry_from_line(obj, line_uuid)
            if entry is not None and entry.type == "assistant" and entry.text:
                key = (str(payload.get("phase") or "commentary"), entry.text)
                if key in visible_here:
                    continue
                visible_here.add(key)
            if entry is not None:
                entries.append(entry)

        return entries, offset + consumed
