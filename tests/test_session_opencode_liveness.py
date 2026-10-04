"""OpenCode's own store as AQ's liveness clock (crisp-horizon-90.10).

A TUI is not a clock: OpenCode repaints its in-turn spinner forever, so tmux's
``window_activity`` says "busy" for as long as the pane is wedged -- 42 minutes
on 2026-10-03, with the task held, no nudge, no ``task.stalled`` and no
restart. The store is the other clock, and these tests drive the real reader
over a fixture database shaped like OpenCode's, plus the two places that ask it
a question: ``harness_progress`` (what the stall ladder corroborates a refused
composer with) and the provider probe that says whether anything is generating.

The rules under test are the ones that keep a false positive off a working
agent: a scoped lookup (one AQ session's rows, not a neighbour's), silence that
a still-running tool explains, and ``None`` for everything unreadable.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.config import AppConfig
from src.llm.providers.local_probe import (
    DEFAULT_OLLAMA_BASE_URL,
    api_root,
    is_local_base_url,
    running_models,
)
from src.sessions.context import harness_progress, store_lower_bound
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.opencode_store import (
    OpenCodeLivenessStore,
    resolve_liveness_store,
    store_path,
)
from src.sessions.provider_liveness import local_endpoint, request_inflight

WORK = "/wd/slot-6"
OTHER = "/wd/slot-2"
NOW = 1_000_000.0


def _json(value):
    """OpenCode writes with JSON.stringify: no spaces."""
    return json.dumps(value, separators=(",", ":"))


class OpenCodeDb:
    """A fixture store with the columns the reader queries."""

    def __init__(self, data_dir, work_dir=WORK):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "opencode.db"
        self.work_dir = work_dir
        self._seq = 0
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT,
                    directory TEXT NOT NULL, time_created INTEGER NOT NULL,
                    time_updated INTEGER NOT NULL);
                CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                    data TEXT NOT NULL);
                CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
                    time_updated INTEGER NOT NULL, data TEXT NOT NULL);
                """
            )

    def _conn(self):
        return sqlite3.connect(self.path)

    def _id(self, prefix):
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

    def session(self, session_id, *, at, parent=None, directory=None):
        ms = int(at * 1000)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                (session_id, parent, directory or self.work_dir, ms, ms),
            )

    def message(self, session_id, role, *, at, updated=None):
        ms = int(at * 1000)
        stamp = int((updated or at) * 1000)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (self._id("msg"), session_id, ms, stamp, _json({"role": role})),
            )
            conn.execute("UPDATE session SET time_updated = ? WHERE id = ?", (stamp, session_id))

    def tool(self, session_id, call_id, tool, *, at, status="running", data=None):
        """A tool part. *status* is the store's own in-flight record."""
        ms = int(at * 1000)
        if data is None:
            data = {
                "type": "tool",
                "tool": tool,
                "callID": call_id,
                "state": {"status": status, "time": {"start": ms}},
            }
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._id("prt"),
                    "msg_x",
                    session_id,
                    ms,
                    ms,
                    data if isinstance(data, str) else _json(data),
                ),
            )
            conn.execute("UPDATE session SET time_updated = ? WHERE id = ?", (ms, session_id))

    def text_part(self, session_id, *, at, updated=None):
        """A streaming text part: created once, updated as it streams."""
        ms = int(at * 1000)
        stamp = int((updated or at) * 1000)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._id("prt"),
                    "msg_x",
                    session_id,
                    ms,
                    stamp,
                    _json({"type": "text", "text": "thinking"}),
                ),
            )
            conn.execute("UPDATE session SET time_updated = ? WHERE id = ?", (stamp, session_id))


@pytest.fixture
def store(tmp_path):
    return OpenCodeLivenessStore(tmp_path), OpenCodeDb(tmp_path)


def _activity(reader, db, *, since=NOW - 10_000):
    return reader.activity(db.work_dir, since)


# -- the scoped read --------------------------------------------------------


def test_progress_is_the_newest_row_of_this_sessions_own_sessions(store):
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    db.message("ses_root", "user", at=NOW - 8000)
    db.message("ses_root", "assistant", at=NOW - 5000)
    db.session("ses_child", at=NOW - 4900, parent="ses_root")
    db.message("ses_child", "assistant", at=NOW - 4800)

    activity = _activity(reader, db)

    assert activity.progress_at == NOW - 4800
    assert activity.has_rows and not activity.inflight
    # The subagent shares the worktree and is included: its rows are this
    # session's work, and a root the subagent has not touched is still in scope.
    assert activity.sessions == frozenset({"ses_root", "ses_child"})


def test_a_streaming_part_counts_while_it_streams(store):
    """A long generation writes its part once; ``time_updated`` is the clock."""
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    db.text_part("ses_root", at=NOW - 8000, updated=NOW - 10)

    assert _activity(reader, db).progress_at == NOW - 10


def test_another_slots_rows_are_never_counted(store):
    """The scoping that keeps a neighbour's busy session from looking alive."""
    reader, db = store
    db.session("ses_mine", at=NOW - 9000)
    db.message("ses_mine", "assistant", at=NOW - 8000)
    # Same store, another slot's session: same box, different worktree.
    db.session("ses_theirs", at=NOW - 5000, directory=OTHER)
    db.message("ses_theirs", "assistant", at=NOW - 4000)

    activity = _activity(reader, db)

    assert activity.progress_at == NOW - 8000
    assert activity.sessions == frozenset({"ses_mine"})


def test_a_predecessors_rows_in_the_same_slot_are_never_counted(store):
    """Same worktree, before this session took the task: not this session."""
    reader, db = store
    db.session("ses_old", at=NOW - 9000)
    db.tool("ses_old", "call_1", "bash", at=NOW - 8000, status="running")

    # Started after the abandoned turn, so the untouched predecessor is out of
    # scope -- including its tool call, which is still "running".
    activity = _activity(reader, db, since=NOW - 7000)

    assert not activity.has_rows and not activity.inflight


# -- in flight ---------------------------------------------------------------


def test_a_running_tool_call_is_in_flight(store):
    """One long ``bash`` writes nothing until it finishes."""
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    db.message("ses_root", "assistant", at=NOW - 8000)
    db.tool("ses_root", "call_1", "bash", at=NOW - 4000, status="running")

    assert _activity(reader, db).inflight is True


def test_a_pending_tool_call_is_in_flight(store):
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    db.message("ses_root", "assistant", at=NOW - 8000)
    db.tool("ses_root", "call_1", "read", at=NOW - 4000, status="pending")

    assert _activity(reader, db).inflight is True


def test_a_finished_tool_call_is_not_in_flight(store):
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    db.message("ses_root", "assistant", at=NOW - 8000)
    db.tool("ses_root", "call_1", "bash", at=NOW - 4000, status="completed")

    assert _activity(reader, db).inflight is False


def test_an_unparsable_running_part_is_still_in_flight(store):
    """One truncated row must not hide a call that is still going."""
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    db.message("ses_root", "assistant", at=NOW - 8000)
    # Truncated mid-write, but it still names a running call.
    db.tool(
        "ses_root",
        "call_1",
        "bash",
        at=NOW - 4000,
        data='{"type":"tool","tool":"bash","state":{"status":"running","input":{"cmd":',
    )

    assert _activity(reader, db).inflight is True


def test_a_text_part_naming_running_is_not_a_tool_call(store):
    """Prose that happens to contain the word is not an unfinished call."""
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    db.message("ses_root", "assistant", at=NOW - 8000)
    db.tool(
        "ses_root",
        "call_1",
        "bash",
        at=NOW - 4000,
        data={"type": "text", "text": "still running the suite"},
    )

    assert _activity(reader, db).inflight is False


# -- unknown is never progress that stopped ----------------------------------


def test_a_store_that_does_not_exist_is_unknown(tmp_path):
    assert OpenCodeLivenessStore(tmp_path / "nowhere").activity(WORK, NOW - 1) is None


def test_a_session_with_no_work_dir_is_unknown(store):
    reader, _ = store
    assert reader.activity("", NOW - 1) is None


def test_a_store_with_no_session_of_ours_reports_no_rows(store):
    """Nothing to have stopped: not a stall, and not progress either."""
    reader, db = store
    db.session("ses_theirs", at=NOW - 5000, directory=OTHER)

    activity = _activity(reader, db)
    assert not activity.has_rows and not activity.inflight


def test_a_schema_a_newer_opencode_changed_is_unknown(tmp_path):
    """A moved column must be unknown, never "nothing has been written"."""
    path = store_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE session (id TEXT)")

    assert OpenCodeLivenessStore(tmp_path).activity(WORK, NOW - 1) is None


# -- harness resolution ------------------------------------------------------


def test_the_store_follows_the_cli_not_the_harness_id(tmp_path):
    """``opencode-zen`` runs the same executable and writes the same store."""
    registry = HarnessRegistry()
    registry.upsert(Harness(id="opencode-zen", name="Z", command="opencode"))
    reader = resolve_liveness_store("opencode-zen", tmp_path, registry=registry)
    assert isinstance(reader, OpenCodeLivenessStore)


def test_a_harness_that_is_not_opencode_has_no_store():
    assert resolve_liveness_store("claude") is None


def test_the_store_path_follows_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert store_path().name == "opencode.db"
    assert store_path().parent == tmp_path / "opencode"


# -- harness_progress: the same store, as the ladder's evidence --------------


def _session(**overrides):
    base = {
        "id": "s1",
        "harness": "opencode",
        "work_dir": WORK,
        "started_at": NOW - 10_000,
        "claim_phase_at": None,
        "session_key": None,
        "llm_provider": "ollama",
        "model": "qwen3.8:27b",
    }
    return SimpleNamespace(**{**base, **overrides})


async def test_harness_progress_reads_the_store_for_opencode(store, tmp_path):
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    db.message("ses_root", "assistant", at=NOW - 4000)

    source, at = await harness_progress(_session(), liveness=lambda harness: reader)

    assert source == "opencode:store"
    assert at == NOW - 4000


async def test_harness_progress_is_unknown_when_the_session_wrote_nothing(store):
    reader, db = store
    db.session("ses_theirs", at=NOW - 5000, directory=OTHER)

    source, at = await harness_progress(_session(), liveness=lambda harness: reader)

    # A source, and no reading: "nothing there" must not read as a stopped clock.
    assert source == "opencode:store"
    assert at is None


async def test_harness_progress_is_unknown_when_the_store_cannot_be_read(tmp_path):
    _, at = await harness_progress(
        _session(), liveness=lambda harness: OpenCodeLivenessStore(tmp_path / "gone")
    )
    assert at is None


async def test_harness_progress_falls_back_to_the_transcript_reader(tmp_path):
    """A harness with no store keeps the reader path, unchanged."""
    source, at = await harness_progress(_session(harness="claude"), liveness=lambda h: None)
    assert source == "claude:none" and at is None


def test_the_store_lower_bound_is_the_claim_not_the_launch():
    """A pool session that claimed an hour late starts a fresh clock."""
    assert store_lower_bound(_session(started_at=NOW - 3600, claim_phase_at=NOW - 600)) == (
        NOW - 600
    )
    assert store_lower_bound(_session(started_at=NOW - 600, claim_phase_at=NOW - 3600)) == (
        NOW - 600
    )


# -- the provider's half -----------------------------------------------------


def test_a_local_llm_block_names_the_endpoint():
    config = AppConfig(llm=SimpleNamespace(provider="openai", base_url="http://gpu:11434/v1"))
    assert local_endpoint(_session(llm_provider="ollama"), config) == "http://gpu:11434/v1"


def test_an_ollama_key_names_ollama_when_nothing_else_does():
    """The default address is Ollama's, and only for the local provider key."""
    config = AppConfig(llm=SimpleNamespace(provider="anthropic", base_url=""))
    assert local_endpoint(_session(llm_provider="ollama"), config) == (DEFAULT_OLLAMA_BASE_URL)


def test_a_gateway_provider_has_no_endpoint_to_ask():
    """Nothing local, so nothing to ask: unknown, which holds the ladder."""
    config = AppConfig(llm=SimpleNamespace(provider="anthropic", base_url=""))
    assert local_endpoint(_session(llm_provider="openai-zen"), config) == ""
    assert local_endpoint(_session(llm_provider=None), config) == ""


def test_a_vendor_api_is_never_a_local_endpoint():
    config = AppConfig(llm=SimpleNamespace(provider="openai", base_url="https://api.openai.com/v1"))
    assert local_endpoint(_session(llm_provider="openai"), config) == ""
    assert is_local_base_url("https://api.openai.com/v1") is False


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _ps(monkeypatch, payload):
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=None: _Response(payload))


def test_running_models_reads_the_names(monkeypatch):
    _ps(monkeypatch, json.dumps({"models": [{"name": "qwen3.8:27b"}]}).encode())
    assert running_models("http://localhost:11434/v1") == {"qwen3.8:27b"}


def test_running_models_is_empty_for_a_server_holding_nothing(monkeypatch):
    _ps(monkeypatch, b'{"models":[]}')
    assert running_models(DEFAULT_OLLAMA_BASE_URL) == set()


def test_running_models_is_unknown_for_anything_that_is_not_ollama(monkeypatch):
    _ps(monkeypatch, b"<html>404</html>")
    assert running_models(DEFAULT_OLLAMA_BASE_URL) is None
    _ps(monkeypatch, b'{"models": "not a list"}')
    assert running_models(DEFAULT_OLLAMA_BASE_URL) is None


def test_running_models_is_unknown_when_the_endpoint_is_down(monkeypatch):
    def _boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert running_models(DEFAULT_OLLAMA_BASE_URL) is None


def test_the_probe_drops_the_openai_prefix():
    assert api_root("http://localhost:11434/v1") == "http://localhost:11434"


async def test_a_resident_model_is_in_flight(monkeypatch):
    _ps(monkeypatch, json.dumps({"models": [{"name": "qwen3.8:27b"}]}).encode())
    config = AppConfig(llm=SimpleNamespace(provider="anthropic", base_url=""))
    assert await request_inflight(_session(llm_provider="ollama"), config) is True


async def test_a_server_holding_nothing_is_not_in_flight(monkeypatch):
    _ps(monkeypatch, b'{"models":[]}')
    config = AppConfig(llm=SimpleNamespace(provider="anthropic", base_url=""))
    assert await request_inflight(_session(llm_provider="ollama"), config) is False


async def test_an_unreachable_endpoint_is_unknown_not_free(monkeypatch):
    def _boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    config = AppConfig(llm=SimpleNamespace(provider="anthropic", base_url=""))
    assert await request_inflight(_session(llm_provider="ollama"), config) is None


async def test_a_session_with_no_local_endpoint_is_unknown(monkeypatch):
    def _explode(request, timeout=None):
        raise AssertionError("a gateway session must never probe a local server")

    monkeypatch.setattr("urllib.request.urlopen", _explode)
    config = AppConfig(llm=SimpleNamespace(provider="anthropic", base_url=""))
    assert await request_inflight(_session(llm_provider="openai-zen"), config) is None


# -- the store is read, never copied -----------------------------------------


def test_the_store_is_opened_read_only(store):
    """A read-only URI: OpenCode is writing to this file at the same time."""
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    _activity(reader, db)

    with pytest.raises(sqlite3.OperationalError):
        conn = sqlite3.connect(reader.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("DELETE FROM session")
        finally:
            conn.close()


def test_a_read_does_not_touch_the_clock(store):
    """No writes means no WAL churn and no lock on a store AQ does not own."""
    reader, db = store
    db.session("ses_root", at=NOW - 9000)
    before = db.path.stat().st_mtime_ns
    _activity(reader, db)
    assert db.path.stat().st_mtime_ns == before


def test_the_reader_does_not_pin_a_replaced_store(tmp_path):
    """OpenCode may rotate the file under AQ; the next read must see the new one."""
    reader = OpenCodeLivenessStore(tmp_path)
    fresh = OpenCodeDb(tmp_path)
    fresh.session("ses_root", at=NOW - 9000)
    fresh.message("ses_root", "assistant", at=NOW - 8000)
    first = reader.activity(WORK, NOW - 10_000)
    assert first is not None and first.has_rows

    replacement = tmp_path / "opencode.db.new"
    replacement.write_bytes(tmp_path.joinpath("opencode.db").read_bytes())
    replacement.replace(tmp_path / "opencode.db")

    assert reader.activity(WORK, NOW - 10_000).has_rows is True
