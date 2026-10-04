"""Scoped, read-only liveness from OpenCode's own store.

A TUI is not a clock. OpenCode repaints its in-turn spinner forever, so a
wedged pane keeps ``#{window_activity}`` fresh and AQ reads it as busy forever:
crisp-horizon-90.10 (2026-10-03) held its task on that reading for 42 minutes
after 16 compactions, with no nudge, no ``task.stalled`` and no restart, while
the pane was byte-identical across captures and Ollama held no model at all.

OpenCode does keep the other clock, in its own SQLite store: every user
message, assistant message and part it writes lands there with the time it was
written. That is the liveness signal this module reads — *scoped to the AQ
session*, so a predecessor's abandoned turn in the same slot, or another slot's
session on the same box, is never counted as this one's progress.

Two rules make the read safe:

* **Read in place, never copied.** The store grows to tens of gigabytes
  (``crisp-horizon-90.10``: ~64 GB), so this is a read-only URI connection
  plus ``PRAGMA query_only`` and three aggregate queries against indexed
  columns. No ``VACUUM``, no dump, no lock, no bytes moved.
* **Every failure is unknown, never "no progress".** A missing file, a locked
  database, a schema OpenCode has since changed and a row of unparsable JSON all
  return ``None``. A caller that may spend a rung must hold on ``None``; this
  module never decides anything.

The same store answers "is a native question dialog open" for the question
service (:mod:`src.sessions.native_questions`); the session scoping lives here
so both readers agree on which native sessions an AQ session owns.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Tables whose rows are progress. OpenCode stamps ``time_updated`` on insert
#: and on every update, so a streaming part counts while it streams.
_PROGRESS_TABLES = ("message", "part")

#: A tool call that has not finished: the store's own in-flight record.
_OPEN_STATUSES = ("pending", "running")


def store_path(data_dir: Path | None = None) -> Path:
    """Where OpenCode keeps its store, honouring ``XDG_DATA_HOME``."""
    if data_dir is None:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        data_dir = Path(base) / "opencode"
    return Path(data_dir) / "opencode.db"


def scoped_session_ids(conn, work_dir: str, since: float) -> dict[str, str | None]:
    """The OpenCode sessions one AQ session owns: ``id -> parent_id``.

    Matched by working directory and time, exactly as the question store
    matches its dialogs: every AQ worker owns its worktree, and only sessions
    touched since the AQ session took the task count, so a predecessor's
    abandoned turn in the same slot is never attributed to it. Subagents share
    the directory and are pulled in, including a root that a busy subagent has
    not touched since (the root is still the session whose composer AQ types
    into).
    """
    since_ms = int(since * 1000)
    directories = sorted({work_dir, os.path.realpath(work_dir)})
    marks = ",".join("?" * len(directories))
    parents = dict(
        conn.execute(
            f"SELECT id, parent_id FROM session WHERE directory IN ({marks}) AND time_updated >= ?",
            (*directories, since_ms),
        ).fetchall()
    )
    for parent in {p for p in parents.values() if p} - parents.keys():
        row = conn.execute("SELECT id, parent_id FROM session WHERE id = ?", (parent,)).fetchone()
        if row is not None:
            parents[row[0]] = row[1]
    return parents


@dataclass(frozen=True)
class OpenCodeActivity:
    """What one AQ session's OpenCode sessions have written, and what is open.

    ``progress_at`` is 0.0 when the scoped sessions have written no row at all.
    That is **not** "progress that stopped" — it is a session AQ cannot see
    working, and :attr:`has_rows` is what tells the two apart.
    """

    #: Newest ``time_updated`` over the scoped sessions' messages and parts.
    progress_at: float = 0.0
    #: Tool calls still ``pending``/``running`` anywhere in the tree: work in
    #: progress that writes nothing until it finishes (a long build, a sleep).
    open_tools: int = 0
    #: The OpenCode sessions this reading covers, for the log line.
    sessions: frozenset[str] = frozenset()

    @property
    def has_rows(self) -> bool:
        return self.progress_at > 0.0

    @property
    def inflight(self) -> bool:
        """Whether the store says something is still running."""
        return self.open_tools > 0


class OpenCodeLivenessStore:
    """Read-only view of OpenCode's store for one AQ session.

    One object per harness; the path is resolved once and every read is a fresh
    short-lived read-only connection, so a store OpenCode replaces underneath
    AQ is picked up rather than pinned.
    """

    harness = "opencode"

    def __init__(self, data_dir: Path | None = None, *, timeout: float = 2.0) -> None:
        self.path = store_path(data_dir)
        self.timeout = timeout

    def activity(self, work_dir: str, since: float) -> OpenCodeActivity | None:
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
            return self._activity(conn, work_dir, since)
        except (sqlite3.Error, ValueError, TypeError):
            logger.debug("opencode store %s liveness read failed", self.path, exc_info=True)
            return None
        finally:
            conn.close()

    def _activity(self, conn, work_dir: str, since: float) -> OpenCodeActivity:
        sessions = scoped_session_ids(conn, work_dir, since)
        if not sessions:
            return OpenCodeActivity()  # No session of this AQ session is visible.
        progress, open_tools = 0.0, 0
        for session_id in sessions:
            for table in _PROGRESS_TABLES:
                newest = conn.execute(
                    f"SELECT MAX(time_updated) FROM {table} WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                if newest:
                    progress = max(progress, float(newest) / 1000)
            open_tools += self._open_tools(conn, session_id, since)
        return OpenCodeActivity(
            progress_at=progress,
            open_tools=open_tools,
            sessions=frozenset(sessions),
        )

    def _open_tools(self, conn, session_id: str, since: float) -> int:
        """How many tool calls in *session_id* have not finished yet.

        Bounded by the same lower bound as the session match: a call that
        started before this AQ session took the task belongs to a predecessor,
        however unfinished it still is. An unreadable row that names an open
        status still counts — an unparsable *running* tool is in flight.
        """
        count = 0
        for (data,) in conn.execute(
            "SELECT data FROM part WHERE session_id = ? AND time_created >= ?"
            " AND (instr(data, '\"running\"') > 0 OR instr(data, '\"pending\"') > 0)",
            (session_id, int(since * 1000)),
        ):
            try:
                part = json.loads(data)
            except (ValueError, TypeError):
                if isinstance(data, str) and ('"running"' in data or '"pending"' in data):
                    count += 1
                continue
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            if str(state.get("status") or "") in _OPEN_STATUSES:
                count += 1
        return count


def resolve_liveness_store(
    harness: str, data_dir: Path | None = None, *, registry=None
) -> OpenCodeLivenessStore | None:
    """The liveness store for *harness*, or ``None`` if it has none.

    The store belongs to the CLI, not to one harness file: a harness such as
    ``opencode-zen`` that runs the ``opencode`` executable against another
    backend reads the same store
    (:func:`~src.sessions.harness_registry.runs_cli`).
    """
    from src.sessions.harness_registry import runs_cli

    if runs_cli(OpenCodeLivenessStore.harness, harness, registry):
        return OpenCodeLivenessStore(data_dir)
    return None
