"""TranscriptWatcher — poll live sessions' transcripts every ~2 s.

For each live session with a resolvable transcript:

* new entries → ``notify.task_message`` events (``stream_id=session_id``,
  so the Discord per-stream worker edits one message in place instead of
  spamming per line),
* assistant entries carrying ``usage`` → one ``record_token_usage`` row,
* in-turn tail → ``touch_session_activity`` + agent ``last_heartbeat`` so
  the stall ladder does not climb on a busy session.

Missing transcript path is a one-shot ``session.transcript_missing`` event
per session id, then peek-diff fallback (silent from the watcher — the
existing ``session_logs`` command is the peek surface).

State is kept in-process:

* byte offsets per session (so the next tick reads only the new bytes),
* seen assistant uuids (usage is idempotent per assistant entry, not per
  line — a re-tick that re-reads a line must not double-charge tokens),
* whether ``transcript_missing`` has already fired.

That state is bounded by the number of live sessions and is discarded when
a session's row disappears.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.sessions.transcripts import resolve_reader
from src.sessions.transcripts.base import TranscriptEntry

logger = logging.getLogger(__name__)

__all__ = ["TranscriptWatcher", "SessionTrackingState"]


@dataclass
class SessionTrackingState:
    """Per-session watcher bookkeeping."""

    offset: int = 0
    #: Assistant entry uuids for which usage has already been charged.
    charged_uuids: set[str] = field(default_factory=set)
    #: True once ``session.transcript_missing`` has been emitted; suppresses
    #: repeat emissions on subsequent unresolvable ticks.
    missing_emitted: bool = False
    #: Path last resolved; used to detect a rotation to a new transcript
    #: file (session_key was previously unknown, then the ``--session-id``
    #: pin took effect and a specific jsonl became findable).
    last_path: Path | None = None
    #: Independent checkpoint: a failed question write must be retried
    #: without re-emitting transcript output or charging usage twice.
    question_offset: int = 0


class TranscriptWatcher:
    """Polls transcripts for every live session.

    ``db`` needs :meth:`list_sessions`, :meth:`get_session`, :meth:`update_agent`,
    :meth:`touch_session_activity`, :meth:`record_token_usage`.  ``bus`` needs
    :meth:`emit`.
    """

    def __init__(
        self,
        *,
        db,
        bus,
        base_dir: Path | None = None,
        tail_size: int = 20,
        questions=None,
    ) -> None:
        self.db = db
        self.bus = bus
        self.base_dir = base_dir
        self._states: dict[str, SessionTrackingState] = {}
        self._tail_size = tail_size
        self.questions = questions

    async def tick(self, *, now: float | None = None) -> None:
        """One poll pass.  Never raises."""
        try:
            sessions = await self.db.list_sessions(live_only=True)
        except Exception:
            logger.debug("transcript watcher: list_sessions failed", exc_info=True)
            return

        seen_ids: set[str] = set()
        for row in sessions:
            seen_ids.add(row.id)
            try:
                await self._process_session(row, now=now)
            except Exception:
                logger.debug(
                    "transcript watcher: processing session %s failed",
                    row.id,
                    exc_info=True,
                )

        # Drop bookkeeping for sessions that vanished (stopped, deleted).
        for stale in [k for k in self._states if k not in seen_ids]:
            self._states.pop(stale, None)

    async def _process_session(self, row, *, now: float | None) -> None:
        reader = resolve_reader(row.harness, base_dir=self.base_dir)
        if reader is None:
            return

        state = self._states.setdefault(row.id, SessionTrackingState())
        path = reader.resolve_path(row.work_dir, row.session_key)
        if path is None:
            if not state.missing_emitted:
                await self._emit(
                    "session.transcript_missing",
                    session_id=row.id,
                    task_id=row.task_id,
                    project_id=row.project_id,
                    harness=row.harness,
                    work_dir=row.work_dir,
                )
                state.missing_emitted = True
            return

        # A resolved path clears the one-shot suppression: if the file
        # goes away again later, we want to know.
        state.missing_emitted = False
        if state.last_path is not None and state.last_path != path:
            # Rotation to a new file — start reading from its beginning
            # rather than a byte offset that belongs to the previous file.
            # We also clear ``charged_uuids`` because uuids are unique per
            # transcript file: on rotation the new file will not repeat the
            # old file's uuids, so nothing is lost.  Known limitation: if
            # Claude ever re-uses a uuid across files (session resume that
            # replays a prior turn's uuid, etc.), the same uuid seen in
            # the *new* file would be re-charged.  We accept that trade to
            # keep bookkeeping bounded per live session.
            state.offset = 0
            state.question_offset = 0
            state.charged_uuids.clear()
        state.last_path = path
        await self._learn_session_key(row, reader, path)

        previous_offset = state.offset
        entries, new_offset = await reader.read_new(path, previous_offset)
        state.offset = new_offset
        if self.questions is not None:
            try:
                question_entries, question_end = entries, new_offset
                if state.question_offset != previous_offset:
                    question_entries, question_end = await reader.read_new(path, state.question_offset)
                if question_entries:
                    await self.questions.observe(row, question_entries)
                state.question_offset = question_end
            except Exception:
                # Keep the question checkpoint for retry, while ordinary
                # output/accounting/activity still consume this batch once.
                logger.warning("question observation failed for %s", row.id, exc_info=True)
        if not entries:
            return

        # Resolve the agent once per tick: usage rows FK to ``agents.id``,
        # so an unknown agent silently drops the ledger row rather than
        # aborting the whole ingest.
        agent_id = await self._resolve_agent_id(row)

        for entry in entries:
            await self._emit_entry(row, entry)
            if entry.type == "assistant" and entry.usage:
                if entry.uuid in state.charged_uuids:
                    continue
                if agent_id:
                    await self._record_usage(row, entry, agent_id=agent_id)
                state.charged_uuids.add(entry.uuid)

        # Activity: use the entry timestamps, not wall clock, so a busy
        # session catching up on backlog is not marked active *now*.
        activity_ts = max((e.ts for e in entries if e.ts), default=0.0)
        if activity_ts > 0:
            try:
                await self.db.touch_session_activity(row.id, activity_ts)
            except Exception:
                logger.debug(
                    "touch_session_activity failed for %s", row.id, exc_info=True
                )

        tail = entries[-self._tail_size :]
        if reader.infer_activity(tail) == "in-turn":
            await self._on_in_turn(row, now=now)

    async def _learn_session_key(self, row, reader, path: Path) -> None:
        """Write back a harness-chosen conversation id we did not pin.

        Claude never reaches this: ``--session-id`` pins its id at launch,
        so the row already has the key. Codex picks its own UUID and only
        reveals it on disk (``default_harnesses/codex.md``), which is what
        blocks ``codex resume``. Reading it off the resolved transcript is
        the only place the daemon can learn it.

        Only ever fills a blank -- never overwrites a key the launcher set.
        """
        if getattr(row, "session_key", None):
            return
        try:
            key = reader.discover_session_key(path)
        except Exception:
            logger.debug("discover_session_key failed for %s", row.id, exc_info=True)
            return
        if not key:
            return
        try:
            await self.db.update_session(row.id, session_key=key)
        except Exception:
            logger.debug("writing session_key for %s failed", row.id, exc_info=True)
            return
        logger.info("learned %s session key %s for session %s", row.harness, key, row.id)

    async def _emit_entry(self, row, entry: TranscriptEntry) -> None:
        """One ``notify.task_message`` per entry, stream-keyed by session id.

        Entries with no renderable text are skipped entirely.  An assistant
        turn whose content was only ``thinking`` blocks flattens to an empty
        string, and the old ``entry.text or f"[{entry.type}]"`` fallback turned
        each one into a literal ``[assistant]`` line — dozens of them per task,
        carrying no information at all.  Nothing downstream can render an empty
        entry, so there is nothing to announce.
        """
        if not (entry.text or "").strip():
            return
        payload = {
            "event_type": "notify.task_message",
            "severity": "info",
            "category": "task_stream",
            "project_id": row.project_id or None,
            "task_id": row.task_id or "",
            "message": entry.text,
            "message_type": "agent_output",
            # The transcript role (user | assistant | system | ...).  Carried
            # so consumers can tell the agent's own words from prompt echoes
            # and harness machinery: the dashboard renders every role, while
            # the Discord relay forwards only ``assistant``.  Without it the
            # relay had no way to distinguish them and posted the bootstrap
            # prompt, injected messages and system frames verbatim.
            "role": entry.type,
            "stream_id": row.id,
            "stream_done": False,
        }
        try:
            await self.bus.emit("notify.task_message", payload)
        except Exception:
            logger.debug(
                "transcript watcher: emit failed for %s", row.id, exc_info=True
            )

    async def _resolve_agent_id(self, row) -> str | None:
        """The agent to attribute usage to.

        Prefer the task's ``assigned_agent_id`` (that is the actual worker,
        and it is the id joined by :meth:`get_cost_rollup`).  Fall back to
        the session id: the token ledger's ``agent_id`` column is a
        best-effort attribution field and would rather record a row keyed
        by session than lose the usage entirely.
        """
        task_id = getattr(row, "task_id", None)
        if task_id:
            try:
                task = await self.db.get_task(task_id)
            except Exception:
                task = None
            if task is not None and getattr(task, "assigned_agent_id", None):
                return task.assigned_agent_id
        # Fall back to the session id, as documented above.  Previously this
        # returned None, and the caller skips _record_usage entirely when the
        # agent is unresolvable — so every session without an assigned agent
        # (supervisor sessions, or any task whose agent had already been
        # released) silently dropped its usage instead of recording it under
        # a best-effort key.
        return getattr(row, "id", None)

    async def _record_usage(
        self, row, entry: TranscriptEntry, *, agent_id: str
    ) -> None:
        usage = entry.usage or {}
        # Record the true, unmodified ``input_tokens`` from usage — do NOT
        # fold cache reads/writes in.  ``input_tokens`` is priced at the
        # model's input rate downstream; adding cache_read (which is priced
        # separately and cheaper) or cache_creation (priced separately and
        # more expensive) would inflate priced input and produce a wrong
        # cost.  Cache figures still contribute to the raw ``tokens``
        # total for accounting; when the ledger grows dedicated columns
        # for cache reads/writes we can pass them through explicitly.
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        total = input_tokens + output_tokens + cache_read + cache_write
        if total <= 0:
            return
        try:
            await self.db.record_token_usage(
                row.project_id,
                agent_id,
                row.task_id or "",
                total,
                model=entry.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception:
            logger.debug(
                "transcript watcher: record_token_usage failed for %s",
                row.id,
                exc_info=True,
            )

    async def _on_in_turn(self, row, *, now: float | None) -> None:
        """Bump agent heartbeat so the lease does not expire mid-turn."""
        ts = now if now is not None else time.time()
        try:
            fresh = await self.db.get_session(row.id)
        except Exception:
            fresh = row
        task = None
        if fresh and fresh.task_id:
            try:
                task = await self.db.get_task(fresh.task_id)
            except Exception:
                task = None
        agent_id = getattr(task, "assigned_agent_id", None)
        if agent_id:
            try:
                await self.db.update_agent(agent_id, last_heartbeat=ts)
            except Exception:
                logger.debug(
                    "update_agent(last_heartbeat) failed for %s",
                    agent_id,
                    exc_info=True,
                )

    async def _emit(self, event_type: str, **payload) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(event_type, payload)
        except Exception:
            logger.debug("event %s emit failed", event_type, exc_info=True)
