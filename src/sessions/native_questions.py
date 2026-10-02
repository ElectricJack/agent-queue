"""Structured evidence of a harness's own question dialog, read without the terminal.

OpenCode has a native ``question`` tool: the model asks the user to choose
between options and the TUI shows a dialog until someone answers or dismisses
it. While that dialog is open the composer is gone, so every AQ nudge defers
and the worker waits on a person who is usually not watching. OpenCode keeps
no transcript file AQ could read (``vault/harnesses/opencode.md``), but it
records every tool call, the question tool included, in its own SQLite store
(``$XDG_DATA_HOME/opencode/opencode.db``) the moment it starts::

    part.data = {"type": "tool", "tool": "question", "callID": "call_…",
                 "state": {"status": "running",
                           "input": {"questions": [{"header": …, "question": …,
                                                    "options": [{"label": …,
                                                                 "description": …}],
                                                    "multiple": false}]},
                           "time": {"start": <ms>}}}

``running`` (or ``pending``) is a dialog still waiting; ``completed`` is an
answer and ``error`` a dismissal ("The user dismissed this question"). This
module only *reads* that store, read-only and outside the event loop, and
never decides anything: :mod:`src.sessions.questions` owns identity, routing
and delivery. An unreadable store is unknown evidence (``None``), never "no
question".
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Statuses of a tool call that has not finished.
_OPEN = ("pending", "running")
#: The tool part that runs a subagent. It stays ``running`` for as long as the
#: child session works, so it never means the parent is waiting on a dialog.
_SUBAGENT_TOOL = "task"
#: The rendered text is quoted into routed messages and escalations, whose
#: ``decision_requested`` is bounded at 4000 characters.
_MAX_RENDERED = 3500


@dataclass(frozen=True)
class NativeOption:
    label: str
    description: str = ""


@dataclass(frozen=True)
class NativeQuestionItem:
    header: str
    question: str
    options: tuple[NativeOption, ...] = ()
    multiple: bool = False


@dataclass(frozen=True)
class NativeQuestion:
    """One pending native question call, identified by the harness itself."""

    harness: str
    native_session_id: str
    call_id: str
    asked_at: float
    items: tuple[NativeQuestionItem, ...]

    @property
    def turn_id(self) -> str:
        return native_turn_id(self.harness, self.native_session_id, self.call_id)

    def render(self, limit: int | None = _MAX_RENDERED) -> str:
        """The dialog as text; ``limit=None`` keeps every option (classification)."""
        count = len(self.items)
        lines = [f"Native {self.harness} question dialog ({count} question{'s' * (count != 1)}):"]
        for number, item in enumerate(self.items, 1):
            header = f"[{item.header}] " if item.header else ""
            lines.append(f"{number}. {header}{item.question}")
            for option in item.options:
                detail = f": {option.description}" if option.description else ""
                lines.append(f"   - {option.label}{detail}")
            if item.multiple:
                lines.append("   (more than one option may be chosen)")
        text = "\n".join(lines)
        return text if limit is None or len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class NativeSnapshot:
    """What the harness store says about one AQ session's native sessions."""

    pending: tuple[NativeQuestion, ...] = ()
    #: ``turn_id`` -> final status (``completed`` / ``error``) of question
    #: calls that have settled. Positive evidence a dialog closed.
    settled: dict[str, str] = field(default_factory=dict)
    #: Native session ids running another tool besides a question (a
    #: permission prompt may be up there). The TUI shows the whole tree's
    #: prompts in one view, permissions first, so any of these blocks a press.
    busy: frozenset[str] = frozenset()
    #: Every native session this snapshot read: a call missing from one of
    #: these is positively gone (undone or reverted), not merely unseen.
    sessions: frozenset[str] = frozenset()
    #: Newest assistant message start, in seconds, in the root session(s): the
    #: conversation the TUI composer drives, so the one an answer resumes.
    assistant_activity: float = 0.0

    def dismissable(self, turn_id: str) -> bool:
        """Whether one ``Escape`` can only mean "close *turn_id*'s dialog".

        The TUI shows one prompt for the whole session tree — a pending
        permission before any question, then the first question — and
        ``Escape`` rejects whichever is shown. So it is safe only when this
        dialog is the tree's sole pending prompt and no tool anywhere in the
        tree is running (one may be waiting on a permission).
        """
        return not self.busy and [question.turn_id for question in self.pending] == [turn_id]

    def gone(self, turn_id: str) -> bool:
        """Positive evidence *turn_id*'s call no longer exists (undo/revert)."""
        parsed = parse_native_turn_id(turn_id)
        return (
            parsed is not None
            and parsed[1] in self.sessions
            and turn_id not in self.settled
            and all(question.turn_id != turn_id for question in self.pending)
        )


def native_turn_id(harness: str, native_session_id: str, call_id: str) -> str:
    return f"{harness}:{native_session_id}:{call_id}"


def parse_native_turn_id(turn_id: str | None) -> tuple[str, str, str] | None:
    """``(harness, native_session_id, call_id)``, or ``None`` for a transcript turn."""
    if not isinstance(turn_id, str):
        return None
    harness, sep, rest = turn_id.partition(":")
    session, sep2, call = rest.partition(":")
    if not (sep and sep2 and harness in NATIVE_HARNESSES and session and call):
        return None
    return harness, session, call


def _items(raw) -> tuple[NativeQuestionItem, ...]:
    items = []
    for question in raw if isinstance(raw, list) else ():
        if not isinstance(question, dict):
            continue
        options = tuple(
            NativeOption(str(option.get("label") or ""), str(option.get("description") or ""))
            for option in question.get("options") or ()
            if isinstance(option, dict) and option.get("label")
        )
        items.append(
            NativeQuestionItem(
                header=str(question.get("header") or ""),
                question=str(question.get("question") or ""),
                options=options,
                multiple=bool(question.get("multiple")),
            )
        )
    return tuple(items)


class OpenCodeQuestionStore:
    """Read-only view of OpenCode's SQLite store for one AQ session.

    An AQ session is matched to its OpenCode sessions by working directory and
    time: every AQ worker owns its worktree, and only calls started after the
    AQ session's lower bound count, so a predecessor's abandoned dialog in the
    same slot (or in a resumed OpenCode session) is never attributed to it.
    Subagent sessions share the directory, and the TUI shows their questions
    too, so they are included.
    """

    harness = "opencode"

    def __init__(self, data_dir: Path | None = None, *, timeout: float = 2.0) -> None:
        if data_dir is None:
            base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
            data_dir = Path(base) / "opencode"
        self.path = Path(data_dir) / "opencode.db"
        self.timeout = timeout

    def snapshot(self, work_dir: str, since: float) -> NativeSnapshot | None:
        """Blocking read; callers run it in a thread. ``None`` when unknown."""
        if not work_dir or not self.path.is_file():
            return None
        try:
            conn = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=self.timeout
            )
        except (sqlite3.Error, ValueError, OSError):
            logger.debug("opencode store %s is unreadable", self.path, exc_info=True)
            return None
        try:
            conn.execute("PRAGMA query_only = 1")
            return self._snapshot(conn, work_dir, since)
        except (sqlite3.Error, ValueError, TypeError):
            logger.debug("opencode store %s read failed", self.path, exc_info=True)
            return None
        finally:
            conn.close()

    def _snapshot(self, conn, work_dir: str, since: float) -> NativeSnapshot:
        since_ms = int(since * 1000)
        directories = sorted({work_dir, os.path.realpath(work_dir)})
        marks = ",".join("?" * len(directories))
        parents = dict(
            conn.execute(
                f"SELECT id, parent_id FROM session WHERE directory IN ({marks})"
                " AND time_updated >= ?",
                (*directories, since_ms),
            ).fetchall()
        )
        # A root busy in a subagent may not have been touched since; it is
        # still the session whose composer AQ types into.
        for parent in {p for p in parents.values() if p} - parents.keys():
            row = conn.execute("SELECT id, parent_id FROM session WHERE id = ?", (parent,)).fetchone()
            if row is not None:
                parents[row[0]] = row[1]
        pending, settled, busy, activity = [], {}, set(), 0.0
        for session_id, parent_id in parents.items():
            for created, data in conn.execute(
                "SELECT time_created, data FROM part WHERE session_id = ? AND time_created >= ?"
                " AND (instr(data, '\"question\"') > 0 OR instr(data, '\"running\"') > 0"
                " OR instr(data, '\"pending\"') > 0)",
                (session_id, since_ms),
            ):
                try:
                    self._part(session_id, created, data, pending, settled, busy)
                except (ValueError, TypeError, AttributeError):
                    # One unreadable row must not hide the session's dialogs;
                    # an unparsable running tool still counts as busy.
                    logger.debug("opencode part in %s is unreadable", session_id, exc_info=True)
                    if isinstance(data, str) and '"running"' in data:
                        busy.add(session_id)
            if parent_id:
                continue  # a subagent's turn is not the worker answering AQ
            for created, data in conn.execute(
                "SELECT time_created, data FROM message WHERE session_id = ? AND time_created >= ?"
                " AND instr(data, '\"assistant\"') > 0",
                (session_id, since_ms),
            ):
                try:
                    message = json.loads(data)
                except (ValueError, TypeError):
                    continue
                if isinstance(message, dict) and message.get("role") == "assistant":
                    activity = max(activity, float(created) / 1000)
        pending.sort(key=lambda question: (question.asked_at, question.turn_id))
        return NativeSnapshot(
            pending=tuple(pending),
            settled=settled,
            busy=frozenset(busy),
            sessions=frozenset(parents),
            assistant_activity=activity,
        )

    def _part(self, session_id, created, data, pending, settled, busy) -> None:
        part = json.loads(data)
        if not isinstance(part, dict) or part.get("type") != "tool":
            return
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        tool = part.get("tool")
        if tool != "question":
            if status in _OPEN and tool != _SUBAGENT_TOOL:
                busy.add(session_id)
            return
        call_id = str(part.get("callID") or "")
        if not call_id:
            return
        turn_id = native_turn_id(self.harness, session_id, call_id)
        if status not in _OPEN:
            settled[turn_id] = str(status)
            return
        timing = state.get("time") if isinstance(state.get("time"), dict) else {}
        started = timing.get("start") or created
        inputs = state.get("input") if isinstance(state.get("input"), dict) else {}
        items = _items(inputs.get("questions"))
        if items:
            pending.append(
                NativeQuestion(
                    harness=self.harness,
                    native_session_id=session_id,
                    call_id=call_id,
                    asked_at=float(started) / 1000,
                    items=items,
                )
            )
        else:
            # A dialog AQ cannot describe is still a dialog on screen.
            busy.add(session_id)


#: Harnesses whose native question dialogs AQ can observe.
NATIVE_HARNESSES = frozenset({OpenCodeQuestionStore.harness})


def resolve_native_question_source(
    harness: str, data_dir: Path | None = None
) -> OpenCodeQuestionStore | None:
    """The structured question store for *harness*, or ``None`` if it has none."""
    if harness == OpenCodeQuestionStore.harness:
        return OpenCodeQuestionStore(data_dir)
    return None
