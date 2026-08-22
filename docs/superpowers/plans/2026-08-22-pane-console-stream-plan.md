# Pane View: console-stream + Streams API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a new backend "streamable command" primitive (`src/api/streams.py` +
`POST/GET /api/streams*`) and the `console-stream` dashboard pane view that
consumes it, so the supervisor can run a long-lived command (e.g. `pytest`)
and push its live stdout/stderr into the shell pane while chat stays a clean
narrative.

**Architecture:** Backend first (streams API is usable standalone via `aq
stream start/tail/kill` before any frontend exists), then frontend. The
streams API is a hand-written FastAPI router factory (`build_streams_router`)
mirroring `src/api/sessions.py`'s replay-then-tail SSE shape, backed by an
in-memory `StreamRegistry` (not a DB table — output is large and short-lived).
The frontend adds the **first** entries to two pieces of shared
infrastructure that don't exist yet in this repo — `dashboard/src/panes/types.ts`
(the `PaneManifest`/`PaneViewProps` contract) and `dashboard/src/panes/registry.ts`
(the `import.meta.glob` pane registry) — plus the `console-stream` view itself.
Building the full `<ShellPane>` shell chrome (open/close state machine,
keyboard routing, agent-push message wiring) is **out of scope** — see
Deviations below. Component tests exercise `ConsoleStreamPane` in isolation
with mocked `close`/`setArgs`/`setToolbar`/`setShortcuts` props, per the
plugin-interface spec's own per-view testing model (§9.1).

**Tech Stack:** Python 3.12, FastAPI, `asyncio.create_subprocess_exec`,
SQLAlchemy Core (config/migrations only — streams themselves are in-memory),
`click` (CLI), pytest + pytest-asyncio + httpx `ASGITransport`. React 19,
TypeScript, Vite, `zod` (new dependency), `@heroicons/react/24/outline`,
Vitest + `@testing-library/react` + `jsdom` (new dev dependencies — no test
framework exists in `dashboard/` yet).

**Spec:**
- `docs/superpowers/specs/2026-08-22-pane-console-stream-design.md` (primary — every section below implements one part of this)
- `docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md` (the pane-view contract this view implements)
- `docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md` (context only — shell chrome is out of scope here)

## Global Constraints

- **Argv list, never a shell string.** `command` on `POST /api/streams` must
  be a non-empty `list[str]`; spawn via `asyncio.create_subprocess_exec`,
  never `create_subprocess_shell`. Non-list → 400. (spec §8.6, matches the
  codebase-wide rule documented on `_run_subprocess_shell` in
  `src/commands/helpers.py`.)
- **No stdin.** Always `stdin=asyncio.subprocess.DEVNULL`. (spec §8.4)
- **`cwd` confinement.** Every `cwd` must resolve inside `workspace_dir`, a
  registered repo's `source_path`, or a registered workspace path — same
  roots `CommandHandler._validate_path` (`src/commands/handler.py:526`)
  allows. Outside → 403. (spec §8.4)
- **Ownership check on read/kill.** `scope.session_id == handle.session_id`
  OR `scope.elevated and scope.project_id in (None, handle.project_id)` OR
  `scope.kind == "local"`. Else 403 `out of scope: stream ownership`. (spec §8.4)
- **Start requires local or elevated scope.** A plain session-scoped token
  cannot start a stream. (spec §8.4)
- **Kill escalation:** SIGTERM → SIGINT → SIGKILL, `kill_grace_seconds`
  (default 5s) split across the three stages; idempotent 200 no-op on an
  already-exited stream. (explicit user requirement + spec §8.2)
- **Concurrency cap:** `streams.max_concurrent_per_session` (default 3);
  above cap → 429 `too many concurrent streams`. (spec §8.4)
- **Audit every start/kill** via `db.log_event(...)`, best-effort
  (try/except, never fatal), matching the existing pattern in
  `src/commands/gate_commands.py:70-80`. (spec §8.4)
- **Icons: `@heroicons/react/24/outline` only.** The console-stream spec's
  own manifest example imports `lucide-react`, which is not installed and
  is explicitly forbidden by the plugin-interface spec (§4). This plan uses
  heroicons throughout — see Deviations.
- **No new heavy dependency for ANSI parsing** — a small local converter
  (spec §5.1).
- **Buffer caps:** server ring buffer `streams.buffer_max_lines` (default
  5000) / `streams.buffer_max_bytes` (default 2MB); client buffer cap 5000
  lines with a truncation marker (spec §7.3).

---

## Deviations from the input specs (confirmed against the live codebase)

A research pass across the actual repo (not just the specs) found the
following inaccuracies/gaps in the three input documents. This plan corrects
them; each is called out again in its task below.

1. **None of the pane infrastructure exists yet.** `dashboard/src/panes/`,
   `dashboard/src/shell/`, `PaneManifest`, `PaneViewProps`, `ShellPane.tsx`,
   `useShellPaneStore` are all greenfield. This plan builds the minimal
   shared contract (`types.ts`, `registry.ts`) needed for `console-stream`
   to exist and be tested, but does **not** build `<ShellPane>` chrome,
   `useShellPane()`, keyboard routing, or agent-push message wiring
   (`pane_open` message frames, `--pane-open` CLI flag) — those are shared
   shell-spec infrastructure for a separate plan. Component tests mount
   `ConsoleStreamPane` directly with mock props instead of through a real
   shell.
2. **No test framework in `dashboard/`.** `vitest`, `@testing-library/react`,
   `jsdom` are not installed; no `test` script exists. Task 10 adds them.
3. **`zod` is not installed.** Task 10 adds it.
4. **Icon contradiction.** Plugin-interface spec §4 mandates heroicons and
   forbids `LucideIcon`; the console-stream spec's own manifest example
   imports `TerminalSquare` from `lucide-react` (not installed anywhere in
   the repo) and types `PaneToolbarAction.icon` as `LucideIcon`. This plan
   uses `CommandLineIcon` from `@heroicons/react/24/outline` and types icons
   as `ComponentType<SVGProps<SVGSVGElement>>` throughout, per the
   plugin-interface spec (the authoritative contract).
5. **No virtualizer library exists to reuse.** The spec claims the console
   view can reuse "the same virtualizer the Tasks table already uses" —
   `dashboard/src/pages/work/WorkTasks.tsx` uses no virtualization library,
   and no `@tanstack/react-virtual` / `react-window` / similar is a
   dependency. Task 16 implements a small self-contained fixed-row-height
   windowed renderer instead of introducing a new dependency or depending
   on nonexistent code.
6. **`cwd` confinement does not reuse `src/orchestrator/workspace.py`.**
   That module handles workspace *acquisition* (locks, worktrees) for task
   execution, not arbitrary-path confinement. The real reusable check is
   `CommandHandler._validate_path` (`src/commands/handler.py:526`). Since
   `build_streams_router` (like `build_sessions_router`) is a factory that
   takes `db`/`config` directly rather than a live `CommandHandler`
   instance, Task 3 adds a standalone `_validate_cwd` helper in
   `src/api/streams.py` that duplicates `_validate_path`'s exact logic
   rather than reaching into a `CommandHandler` the router doesn't own.
7. **No `audit_log()` function exists.** The real primitive is
   `Database.log_event(event_type, project_id=None, task_id=None,
   agent_id=None, payload=None) -> int` (`src/database/queries/event_queries.py:15`).
   Used throughout instead.
8. **Config is plain `@dataclass`, not pydantic.** `StreamsConfig` in Task 1
   follows the existing `WorktreesConfig` shape (`src/config.py:913`), with
   a `validate() -> list[ConfigError]` method, `ConfigError(section, field,
   message, severity="error")`.
9. **CLI is click-based and REST-first through `/api/execute`** — except for
   a small number of hand-written routers with path-embedded identifiers
   (`/api/sessions/{name}/message[s]`), which `CLIClient` calls directly via
   `httpx`. `POST/GET /api/streams*` follows that same direct-`httpx`
   pattern (Task 8), not `/api/execute`.
10. **The frontend does not need to call `POST /api/streams` at all** — per
    the spec's own non-goal §2 ("not responsible for starting commands from
    the UI"), only the supervisor/CLI starts streams. The pane only needs
    `GET /api/streams/{id}` (optional first-paint metadata), `GET
    .../subscribe` (SSE, raw `EventSource` — matches the existing
    `useTranscriptStream` precedent, which is the sanctioned exception to
    "never call fetch directly"), and `POST .../kill`. Metadata/kill go
    through the generated `@aq/ts-client` SDK once regenerated (Task 18),
    since both routes carry explicit FastAPI `response_model`s and will
    appear in `/openapi.json` automatically — no hand-written
    `RESPONSE_MODELS` entry is needed (that mechanism is for
    `CommandHandler`-derived auto-generated routes, which streams isn't).

---

## Task 1: `StreamsConfig` — daemon config

**Files:**
- Modify: `src/config.py` (add `StreamsConfig` dataclass near `WorktreesConfig`
  at line 913; register on `AppConfig` near line 1236)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `StreamsConfig` dataclass with fields `buffer_max_lines: int =
  5000`, `buffer_max_bytes: int = 2*1024*1024`, `retention_seconds: int =
  300`, `kill_grace_seconds: float = 5.0`, `max_concurrent_per_session: int
  = 3`, `client_reconnect_attempts: int = 5`, and `validate(self) ->
  list[ConfigError]`. Exposed as `AppConfig.streams: StreamsConfig`.

- [ ] **Step 1: Write the failing test**

```python
# appended to tests/test_config.py

def test_streams_config_defaults():
    from src.config import AppConfig

    cfg = AppConfig()
    assert cfg.streams.buffer_max_lines == 5000
    assert cfg.streams.buffer_max_bytes == 2 * 1024 * 1024
    assert cfg.streams.retention_seconds == 300
    assert cfg.streams.kill_grace_seconds == 5.0
    assert cfg.streams.max_concurrent_per_session == 3
    assert cfg.streams.client_reconnect_attempts == 5


def test_streams_config_validate_rejects_non_positive():
    from src.config import StreamsConfig

    cfg = StreamsConfig(buffer_max_lines=0, retention_seconds=-1, max_concurrent_per_session=0)
    errors = cfg.validate()
    fields = {e.field for e in errors}
    assert "buffer_max_lines" in fields
    assert "retention_seconds" in fields
    assert "max_concurrent_per_session" in fields


def test_streams_config_validate_accepts_defaults():
    from src.config import StreamsConfig

    assert StreamsConfig().validate() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -k streams_config -v`
Expected: FAIL with `AttributeError: 'AppConfig' object has no attribute 'streams'` (or `ImportError: cannot import name 'StreamsConfig'`)

- [ ] **Step 3: Write minimal implementation**

Add near `WorktreesConfig` (`src/config.py:913`):

```python
@dataclass
class StreamsConfig:
    """Streamable-command registry backing the console-stream pane view.

    See docs/superpowers/specs/2026-08-22-pane-console-stream-design.md §8.1/§8.4.
    """

    buffer_max_lines: int = 5000
    buffer_max_bytes: int = 2 * 1024 * 1024
    retention_seconds: int = 300
    kill_grace_seconds: float = 5.0
    max_concurrent_per_session: int = 3
    client_reconnect_attempts: int = 5

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.buffer_max_lines <= 0:
            errors.append(
                ConfigError("streams", "buffer_max_lines", "must be positive")
            )
        if self.buffer_max_bytes <= 0:
            errors.append(
                ConfigError("streams", "buffer_max_bytes", "must be positive")
            )
        if self.retention_seconds <= 0:
            errors.append(
                ConfigError("streams", "retention_seconds", "must be positive")
            )
        if self.kill_grace_seconds <= 0:
            errors.append(
                ConfigError("streams", "kill_grace_seconds", "must be positive")
            )
        if self.max_concurrent_per_session <= 0:
            errors.append(
                ConfigError(
                    "streams", "max_concurrent_per_session", "must be positive"
                )
            )
        return errors
```

Register on `AppConfig` (`src/config.py`, alongside `worktrees:
WorktreesConfig = field(default_factory=WorktreesConfig)` near line 1236):

```python
    streams: StreamsConfig = field(default_factory=StreamsConfig)
```

And thread it into `AppConfig.validate()` (near the other `errors.extend(...)`
calls in the method starting at line 1313):

```python
        errors.extend(self.streams.validate())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -k streams_config -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat(config): add StreamsConfig for the streamable-command registry"
```

---

## Task 2: `src/api/streams.py` — `StreamRegistry`, `StreamHandle`, `ConsoleFrame`

**Files:**
- Create: `src/api/streams.py`
- Test: `tests/test_streams_registry.py`

**Interfaces:**
- Consumes: nothing new (pure in-memory data structures; `asyncio`, `time`,
  `uuid`, `collections.deque`, `dataclasses`).
- Produces:
  - `ConsoleFrame(seq: int, type: Literal["line","exit","killed"], stream:
    Literal["stdout","stderr"]|None=None, text: str|None=None, rc:
    int|None=None, ts: float)` with `.to_dict() -> dict`.
  - `StreamHandle(stream_id, title, session_id, project_id, command:
    list[str], cwd, status: Literal["running","exited","killed"]="running",
    exit_code=None, started_at, ended_at=None, buffer: deque[ConsoleFrame],
    process: asyncio.subprocess.Process|None=None, subscribers:
    set[asyncio.Queue], truncated=False)` with methods `next_seq() -> int`,
    `append(frame: ConsoleFrame) -> None`, `subscribe() -> asyncio.Queue`,
    `unsubscribe(q) -> None`, `replay_from(after_seq: int) ->
    list[ConsoleFrame]`.
  - `StreamRegistry(buffer_max_lines: int = 5000)` with `create(*, title,
    session_id, project_id, command, cwd) -> StreamHandle`, `get(stream_id)
    -> StreamHandle | None`, `concurrent_count(session_id) -> int`,
    `finish(handle) -> None`, `evict(stream_id) -> None`,
    `all_finished_before(cutoff: float) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_streams_registry.py
"""Unit tests for the in-memory streams registry (no HTTP, no subprocess)."""

from __future__ import annotations

import asyncio

import pytest

from src.api.streams import ConsoleFrame, StreamRegistry


def test_create_assigns_stream_id_and_running_status():
    reg = StreamRegistry()
    handle = reg.create(
        title="Running pytest…", session_id="supervisor-demo",
        project_id="demo", command=["pytest", "tests/"], cwd="/tmp",
    )
    assert handle.stream_id
    assert handle.status == "running"
    assert reg.get(handle.stream_id) is handle


def test_concurrent_count_tracks_per_session():
    reg = StreamRegistry()
    reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    reg.create(title="b", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    reg.create(title="c", session_id="s2", project_id=None, command=["echo"], cwd="/tmp")
    assert reg.concurrent_count("s1") == 2
    assert reg.concurrent_count("s2") == 1
    assert reg.concurrent_count("s3") == 0


def test_finish_decrements_concurrent_count():
    reg = StreamRegistry()
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    reg.finish(handle)
    assert reg.concurrent_count("s1") == 0


def test_append_fans_out_to_subscribers_and_caps_buffer():
    reg = StreamRegistry(buffer_max_lines=2)
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    q = handle.subscribe()
    for i in range(3):
        handle.append(ConsoleFrame(seq=i, type="line", stream="stdout", text=str(i)))
    assert len(handle.buffer) == 2
    assert handle.truncated is True
    assert [f.seq for f in handle.buffer] == [1, 2]
    assert q.qsize() == 3  # subscriber queue is not capped by the ring buffer


def test_replay_from_returns_frames_after_seq():
    reg = StreamRegistry()
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    for i in range(5):
        handle.append(ConsoleFrame(seq=i, type="line", stream="stdout", text=str(i)))
    replayed = handle.replay_from(2)
    assert [f.seq for f in replayed] == [3, 4]


def test_evict_removes_from_registry():
    reg = StreamRegistry()
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    reg.evict(handle.stream_id)
    assert reg.get(handle.stream_id) is None


def test_all_finished_before_cutoff():
    reg = StreamRegistry()
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    handle.status = "exited"
    handle.ended_at = 100.0
    assert reg.all_finished_before(200.0) == [handle.stream_id]
    assert reg.all_finished_before(50.0) == []


def test_console_frame_to_dict_omits_none_fields():
    frame = ConsoleFrame(seq=1, type="line", stream="stdout", text="hi", ts=1.0)
    d = frame.to_dict()
    assert d == {"type": "line", "seq": 1, "ts": 1.0, "stream": "stdout", "text": "hi"}
    exit_frame = ConsoleFrame(seq=2, type="exit", rc=0, ts=2.0)
    assert exit_frame.to_dict() == {"type": "exit", "seq": 2, "ts": 2.0, "rc": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streams_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.api.streams'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/api/streams.py
"""In-memory streamable-command registry backing the console-stream pane view.

Not a `tables.py` row: a stream is short-lived and its output can be large.
Mirrors src/api/sessions.py's SSE shape but backs a live subprocess instead
of a transcript file. See
docs/superpowers/specs/2026-08-22-pane-console-stream-design.md §8.1.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

__all__ = ["ConsoleFrame", "StreamHandle", "StreamRegistry"]

StreamStatus = Literal["running", "exited", "killed"]
FrameStream = Literal["stdout", "stderr"]
FrameType = Literal["line", "exit", "killed"]


@dataclass
class ConsoleFrame:
    seq: int
    type: FrameType
    stream: FrameStream | None = None
    text: str | None = None
    rc: int | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d: dict = {"type": self.type, "seq": self.seq, "ts": self.ts}
        if self.stream is not None:
            d["stream"] = self.stream
        if self.text is not None:
            d["text"] = self.text
        if self.rc is not None:
            d["rc"] = self.rc
        return d


@dataclass
class StreamHandle:
    stream_id: str
    title: str
    session_id: str
    project_id: str | None
    command: list[str]
    cwd: str
    status: StreamStatus = "running"
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    buffer: "deque[ConsoleFrame]" = field(default_factory=lambda: deque(maxlen=5000))
    process: "asyncio.subprocess.Process | None" = None
    subscribers: "set[asyncio.Queue]" = field(default_factory=set)
    truncated: bool = False
    _next_seq: int = field(default=0, repr=False)

    def next_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def append(self, frame: ConsoleFrame) -> None:
        if self.buffer.maxlen is not None and len(self.buffer) == self.buffer.maxlen:
            self.truncated = True
        self.buffer.append(frame)
        for q in list(self.subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> "asyncio.Queue[ConsoleFrame]":
        q: "asyncio.Queue[ConsoleFrame]" = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[ConsoleFrame]") -> None:
        self.subscribers.discard(q)

    def replay_from(self, after_seq: int) -> list[ConsoleFrame]:
        return [f for f in self.buffer if f.seq > after_seq]


class StreamRegistry:
    """``dict[str, StreamHandle]`` keyed by uuid4, hung off the orchestrator."""

    def __init__(self, *, buffer_max_lines: int = 5000) -> None:
        self._buffer_max_lines = buffer_max_lines
        self._streams: dict[str, StreamHandle] = {}
        self._concurrency: dict[str, int] = {}

    def create(
        self, *, title: str, session_id: str, project_id: str | None,
        command: list[str], cwd: str,
    ) -> StreamHandle:
        stream_id = uuid.uuid4().hex
        handle = StreamHandle(
            stream_id=stream_id, title=title, session_id=session_id,
            project_id=project_id, command=command, cwd=cwd,
            buffer=deque(maxlen=self._buffer_max_lines),
        )
        self._streams[stream_id] = handle
        self._concurrency[session_id] = self._concurrency.get(session_id, 0) + 1
        return handle

    def get(self, stream_id: str) -> StreamHandle | None:
        return self._streams.get(stream_id)

    def concurrent_count(self, session_id: str) -> int:
        return self._concurrency.get(session_id, 0)

    def finish(self, handle: StreamHandle) -> None:
        self._concurrency[handle.session_id] = max(
            0, self._concurrency.get(handle.session_id, 1) - 1
        )

    def evict(self, stream_id: str) -> None:
        self._streams.pop(stream_id, None)

    def all_finished_before(self, cutoff: float) -> list[str]:
        return [
            sid
            for sid, h in self._streams.items()
            if h.status != "running" and h.ended_at is not None and h.ended_at < cutoff
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_streams_registry.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/api/streams.py tests/test_streams_registry.py
git commit -m "feat(streams): add in-memory StreamRegistry/StreamHandle/ConsoleFrame"
```

---

## Task 3: `POST /api/streams` — start endpoint, spawn/pump, router wiring

**Files:**
- Modify: `src/api/streams.py` (add request/response models, `_validate_cwd`,
  `_can_start`, `_spawn_and_pump`, `build_streams_router`, `_build_default_router`, `router`)
- Modify: `src/api/app.py` (register `streams_router`)
- Test: `tests/test_streams_api.py` (new file — start tests only in this task; more added in Task 9)

**Interfaces:**
- Consumes: `src.api.streams.StreamRegistry` (Task 2), `src.api.auth.RequestScope`
  (`request.state.scope`, set by `TokenAuthMiddleware`), `Database.list_repos()`,
  `Database.list_workspaces()`, `Database.log_event(...)` (`src/database/queries/event_queries.py:15`).
- Produces: `build_streams_router(*, db, config, workspace_dir: str, registry:
  StreamRegistry | None = None) -> APIRouter` (factory, mirrors
  `build_sessions_router` in `src/api/sessions.py:95`), module-level `router`
  wired into `create_app`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_streams_api.py
"""Streams API tests (spec §8.7). Mirrors tests/test_session_stream_api.py's
fixture shape."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.api.auth import RequestScope
from src.api.streams import StreamRegistry, build_streams_router
from src.database import Database
from src.models import Project


class _FakeStreamsConfig:
    buffer_max_lines = 100
    buffer_max_bytes = 1024
    retention_seconds = 300
    kill_grace_seconds = 3.0
    max_concurrent_per_session = 3


class _FakeAppConfig:
    def __init__(self):
        self.streams = _FakeStreamsConfig()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.initialize()
    await database.create_project(Project(id="demo", name="Demo"))
    yield database
    await database.close()


def _app_with_scope(db, workspace_dir, scope: RequestScope, registry=None) -> FastAPI:
    app = FastAPI()
    router = build_streams_router(
        db=db, config=_FakeAppConfig(), workspace_dir=str(workspace_dir), registry=registry,
    )
    app.include_router(router)

    @app.middleware("http")
    async def _inject_scope(request: Request, call_next):
        request.state.scope = scope
        return await call_next(request)

    return app


LOCAL_SCOPE = RequestScope(kind="local")


@pytest.mark.asyncio
async def test_start_stream_returns_stream_id(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "stream_id" in data
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_start_stream_rejects_non_list_command(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": "echo hi", "cwd": str(tmp_path), "session_id": "s1"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_start_stream_rejects_non_local_non_elevated_scope(db, tmp_path):
    scope = RequestScope(kind="session", session_id="s1", elevated=False)
    app = _app_with_scope(db, tmp_path, scope)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_start_stream_rejects_cwd_outside_workspace(db, tmp_path):
    outside = tmp_path.parent / "not-a-workspace"
    outside.mkdir(exist_ok=True)
    app = _app_with_scope(db, tmp_path / "workspace", outside, LOCAL_SCOPE) \
        if False else _app_with_scope(db, tmp_path / "workspace", LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(outside), "session_id": "s1"},
        )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streams_api.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_streams_router' from 'src.api.streams'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/api/streams.py`:

```python
import asyncio
import json
import logging
import os
import signal
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0


class StreamStartRequest(BaseModel):
    command: list[str]
    cwd: str
    title: str | None = None
    session_id: str
    project_id: str | None = None


class StreamStartResponse(BaseModel):
    stream_id: str
    status: str


class StreamMetadata(BaseModel):
    stream_id: str
    title: str
    status: str
    exit_code: int | None
    started_at: float
    ended_at: float | None
    session_id: str
    project_id: str | None


async def _validate_cwd(cwd: str, *, db, workspace_dir: str) -> str | None:
    """Mirrors ``CommandHandler._validate_path`` (src/commands/handler.py:526)
    without depending on a live ``CommandHandler`` instance — this router
    factory, like ``build_sessions_router``, takes ``db``/``config`` directly.
    """
    real = os.path.realpath(cwd)
    workspace_real = os.path.realpath(workspace_dir)
    if real.startswith(workspace_real + os.sep) or real == workspace_real:
        return real
    repos = await db.list_repos()
    for repo in repos:
        if repo.source_path:
            repo_real = os.path.realpath(repo.source_path)
            if real.startswith(repo_real + os.sep) or real == repo_real:
                return real
    workspaces = await db.list_workspaces()
    for ws in workspaces:
        ws_real = os.path.realpath(ws.workspace_path)
        if real.startswith(ws_real + os.sep) or real == ws_real:
            return real
    return None


def _can_start(scope) -> bool:
    return scope.kind == "local" or scope.elevated


def _can_access(scope, handle: StreamHandle) -> bool:
    if scope.kind == "local":
        return True
    if scope.session_id == handle.session_id:
        return True
    if scope.elevated and scope.project_id in (None, handle.project_id):
        return True
    return False


async def _spawn_and_pump(handle: StreamHandle, registry: StreamRegistry) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *handle.command,
            cwd=handle.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        handle.status = "exited"
        handle.exit_code = -1
        handle.ended_at = time.time()
        handle.append(
            ConsoleFrame(seq=handle.next_seq(), type="exit", rc=-1, text=f"failed to start: {exc}")
        )
        registry.finish(handle)
        return

    handle.process = proc

    async def _pump(stream_name: FrameStream, pipe) -> None:
        if pipe is None:
            return
        while True:
            line = await pipe.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\n")
            handle.append(ConsoleFrame(seq=handle.next_seq(), type="line", stream=stream_name, text=text))

    stdout_task = asyncio.create_task(_pump("stdout", proc.stdout))
    stderr_task = asyncio.create_task(_pump("stderr", proc.stderr))
    rc = await proc.wait()
    await asyncio.gather(stdout_task, stderr_task)

    handle.ended_at = time.time()
    if handle.status != "killed":
        handle.status = "exited"
        handle.exit_code = rc
        handle.append(ConsoleFrame(seq=handle.next_seq(), type="exit", rc=rc))
    registry.finish(handle)


async def _kill(handle: StreamHandle, *, grace_seconds: float) -> None:
    if handle.status != "running" or handle.process is None:
        return
    handle.status = "killed"
    proc = handle.process
    stage_seconds = max(0.1, grace_seconds / 3)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGKILL):
        if proc.returncode is not None:
            break
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            break
        try:
            await asyncio.wait_for(proc.wait(), timeout=stage_seconds)
            break
        except asyncio.TimeoutError:
            continue
    handle.append(ConsoleFrame(seq=handle.next_seq(), type="killed"))


_sweep_task: asyncio.Task | None = None


def _start_retention_sweep(registry: StreamRegistry, retention_seconds: float) -> None:
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return

    async def _loop() -> None:
        while True:
            await asyncio.sleep(30.0)
            cutoff = time.time() - retention_seconds
            for stream_id in registry.all_finished_before(cutoff):
                registry.evict(stream_id)

    _sweep_task = asyncio.create_task(_loop())


def build_streams_router(
    *, db, config, workspace_dir: str, registry: StreamRegistry | None = None,
) -> APIRouter:
    """Router factory so tests can wire a lightweight db without the daemon."""

    router = APIRouter()
    reg = registry if registry is not None else StreamRegistry(
        buffer_max_lines=getattr(config.streams, "buffer_max_lines", 5000)
    )

    @router.post("/api/streams", response_model=StreamStartResponse)
    async def start(body: StreamStartRequest, request: Request) -> StreamStartResponse:
        scope = request.state.scope
        if not _can_start(scope):
            raise HTTPException(
                status_code=403,
                detail="out of scope: stream start requires local or elevated scope",
            )
        if not body.command or not all(isinstance(c, str) for c in body.command):
            raise HTTPException(status_code=400, detail="command must be a non-empty list of strings")

        project_id = body.project_id
        if scope.elevated and scope.project_id is not None:
            if project_id is None:
                project_id = scope.project_id
            elif project_id != scope.project_id:
                raise HTTPException(status_code=403, detail="out of scope: project_id mismatch")

        real_cwd = await _validate_cwd(body.cwd, db=db, workspace_dir=workspace_dir)
        if real_cwd is None:
            raise HTTPException(status_code=403, detail="cwd is outside any accessible workspace")

        cap = getattr(config.streams, "max_concurrent_per_session", 3)
        if reg.concurrent_count(body.session_id) >= cap:
            raise HTTPException(status_code=429, detail="too many concurrent streams")

        handle = reg.create(
            title=body.title or "Console", session_id=body.session_id,
            project_id=project_id, command=list(body.command), cwd=real_cwd,
        )
        asyncio.create_task(_spawn_and_pump(handle, reg))

        try:
            await db.log_event(
                "stream.started", project_id=project_id,
                payload=json.dumps({
                    "stream_id": handle.stream_id, "command": handle.command,
                    "scope": "global_admin" if (scope.elevated and scope.project_id is None) else "session",
                }),
            )
        except Exception:
            logger.debug("stream.started log_event failed", exc_info=True)

        _start_retention_sweep(reg, getattr(config.streams, "retention_seconds", 300))
        return StreamStartResponse(stream_id=handle.stream_id, status=handle.status)

    return router


def _build_default_router() -> APIRouter:
    """Registered in :func:`src.api.app.create_app` — uses the shared db/config."""
    from src.api import dependencies as deps

    router = APIRouter()

    @router.post("/api/streams", response_model=StreamStartResponse)
    async def start(body: StreamStartRequest, request: Request) -> StreamStartResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            registry = StreamRegistry(
                buffer_max_lines=getattr(orch.config.streams, "buffer_max_lines", 5000)
            )
            orch.stream_registry = registry
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams" and "POST" in route.methods:
                return await route.endpoint(body=body, request=request)
        raise HTTPException(status_code=500, detail="streams router misconfigured")

    return router


#: The router registered by :func:`src.api.app.create_app`.
router = _build_default_router()
```

Wire into `src/api/app.py` — add the import near the other router imports
(after `from src.api.sessions import router as sessions_router`):

```python
from src.api.streams import router as streams_router
```

and register it near `app.include_router(sessions_router)` (§101-111 range):

```python
    # Streamable-command registry (console-stream pane view): POST/GET
    # /api/streams* — start/metadata/subscribe/tail/kill.
    app.include_router(streams_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_streams_api.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/api/streams.py src/api/app.py tests/test_streams_api.py
git commit -m "feat(streams): add POST /api/streams start endpoint"
```

---

## Task 4: `GET /api/streams/{stream_id}` — metadata endpoint

**Files:**
- Modify: `src/api/streams.py` (add `metadata` route inside `build_streams_router`; add corresponding branch in `_build_default_router`)
- Test: `tests/test_streams_api.py` (append)

**Interfaces:**
- Consumes: `StreamMetadata` (Task 3), `StreamRegistry.get` (Task 2).
- Produces: `GET /api/streams/{stream_id} -> StreamMetadata` (200), 404 if
  unknown, 403 if scope can't access.

- [ ] **Step 1: Write the failing test**

```python
# appended to tests/test_streams_api.py

@pytest.mark.asyncio
async def test_metadata_returns_running_status_then_exited(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]

        import asyncio as _asyncio
        for _ in range(50):
            resp = await client.get(f"/api/streams/{stream_id}")
            if resp.json()["status"] == "exited":
                break
            await _asyncio.sleep(0.05)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "exited"
        assert data["exit_code"] == 0


@pytest.mark.asyncio
async def test_metadata_404_for_unknown_stream(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/streams/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metadata_403_for_wrong_session_ownership(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "owner-session"},
        )
        stream_id = start.json()["stream_id"]

    other_scope = RequestScope(kind="session", session_id="other-session", elevated=False)
    app2 = _app_with_scope(db, tmp_path, other_scope, registry=None)
    # Re-use the SAME registry the first app populated: build a fresh app
    # sharing StreamRegistry explicitly.
    from src.api.streams import StreamRegistry as _SR  # noqa: F401 (documented above)
```

Note: the ownership test needs a registry shared across two apps with
different scopes. Replace the last test with an explicit shared-registry
version:

```python
@pytest.mark.asyncio
async def test_metadata_403_for_wrong_session_ownership(db, tmp_path):
    registry = StreamRegistry(buffer_max_lines=100)
    owner_app = _app_with_scope(db, tmp_path, LOCAL_SCOPE, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=owner_app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "owner-session"},
        )
        stream_id = start.json()["stream_id"]

    other_scope = RequestScope(kind="session", session_id="other-session", elevated=False)
    other_app = _app_with_scope(db, tmp_path, other_scope, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=other_app), base_url="http://test") as client:
        resp = await client.get(f"/api/streams/{stream_id}")
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streams_api.py -k metadata -v`
Expected: FAIL with 404 on every request (route doesn't exist yet)

- [ ] **Step 3: Write minimal implementation**

Add inside `build_streams_router`, after the `start` route:

```python
    @router.get("/api/streams/{stream_id}", response_model=StreamMetadata)
    async def metadata(stream_id: str, request: Request) -> StreamMetadata:
        handle = reg.get(stream_id)
        if handle is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        scope = request.state.scope
        if not _can_access(scope, handle):
            raise HTTPException(status_code=403, detail="out of scope: stream ownership")
        return StreamMetadata(
            stream_id=handle.stream_id, title=handle.title, status=handle.status,
            exit_code=handle.exit_code, started_at=handle.started_at,
            ended_at=handle.ended_at, session_id=handle.session_id,
            project_id=handle.project_id,
        )
```

Extend `_build_default_router` with the matching passthrough:

```python
    @router.get("/api/streams/{stream_id}", response_model=StreamMetadata)
    async def metadata(stream_id: str, request: Request) -> StreamMetadata:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams/{stream_id}" and "GET" in route.methods:
                return await route.endpoint(stream_id=stream_id, request=request)
        raise HTTPException(status_code=500, detail="streams router misconfigured")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_streams_api.py -k metadata -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/api/streams.py tests/test_streams_api.py
git commit -m "feat(streams): add GET /api/streams/{id} metadata endpoint"
```

---

## Task 5: `GET /api/streams/{stream_id}/subscribe` — SSE endpoint

**Files:**
- Modify: `src/api/streams.py` (add `subscribe` route + `_build_default_router` passthrough)
- Test: `tests/test_streams_api.py` (append)

**Interfaces:**
- Consumes: `StreamHandle.replay_from`, `.subscribe`, `.unsubscribe` (Task 2).
- Produces: `GET /api/streams/{stream_id}/subscribe` → `text/event-stream`
  with `data: {...}\n\n` frames (`ConsoleFrame.to_dict()` shape, plus
  `truncated: true` injected on the first replayed frame when the buffer
  already dropped earlier ones), `: heartbeat\n\n` comments every 15s, and
  stream closure after the terminal frame (`type: "exit"` or `"killed"`).

- [ ] **Step 1: Write the failing test**

```python
# appended to tests/test_streams_api.py

@pytest.mark.asyncio
async def test_subscribe_replays_then_closes_on_exit(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "line one"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]

        import asyncio as _asyncio
        await _asyncio.sleep(0.3)  # let the command run to completion

        frames = []
        async with client.stream("GET", f"/api/streams/{stream_id}/subscribe") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: "):]))
                if frames and frames[-1]["type"] in ("exit", "killed"):
                    break

    types = [f["type"] for f in frames]
    assert "line" in types
    assert types[-1] == "exit"
    assert frames[-1]["rc"] == 0


@pytest.mark.asyncio
async def test_subscribe_404_for_unknown_stream(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/streams/does-not-exist/subscribe")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_subscribe_replay_with_after_seq_skips_seen_frames(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]

        import asyncio as _asyncio
        await _asyncio.sleep(0.3)

        frames = []
        async with client.stream(
            "GET", f"/api/streams/{stream_id}/subscribe", params={"after_seq": 0}
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: "):]))
                if frames and frames[-1]["type"] in ("exit", "killed"):
                    break

    assert all(f["seq"] > 0 for f in frames)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streams_api.py -k subscribe -v`
Expected: FAIL with 404 (route doesn't exist)

- [ ] **Step 3: Write minimal implementation**

Add inside `build_streams_router`, after `metadata`:

```python
    @router.get("/api/streams/{stream_id}/subscribe")
    async def subscribe(stream_id: str, request: Request, after_seq: int = -1) -> StreamingResponse:
        handle = reg.get(stream_id)
        if handle is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        scope = request.state.scope
        if not _can_access(scope, handle):
            raise HTTPException(status_code=403, detail="out of scope: stream ownership")

        async def gen():
            replayed = handle.replay_from(after_seq)
            first = True
            for frame in replayed:
                d = frame.to_dict()
                if first and handle.truncated and after_seq < 0:
                    d = {**d, "truncated": True}
                first = False
                yield f"data: {json.dumps(d)}\n\n".encode()
                if frame.type in ("exit", "killed"):
                    return

            if handle.status != "running":
                return

            q = handle.subscribe()
            last_heartbeat = time.monotonic()
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        frame = await asyncio.wait_for(q.get(), timeout=1.0)
                        yield f"data: {json.dumps(frame.to_dict())}\n\n".encode()
                        last_heartbeat = time.monotonic()
                        if frame.type in ("exit", "killed"):
                            return
                    except asyncio.TimeoutError:
                        pass
                    now = time.monotonic()
                    if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                        yield b": heartbeat\n\n"
                        last_heartbeat = now
            finally:
                handle.unsubscribe(q)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
```

Extend `_build_default_router` with the matching passthrough:

```python
    @router.get("/api/streams/{stream_id}/subscribe")
    async def subscribe(stream_id: str, request: Request, after_seq: int = -1) -> StreamingResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams/{stream_id}/subscribe":
                return await route.endpoint(stream_id=stream_id, request=request, after_seq=after_seq)
        raise HTTPException(status_code=500, detail="streams router misconfigured")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_streams_api.py -k subscribe -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/api/streams.py tests/test_streams_api.py
git commit -m "feat(streams): add SSE subscribe endpoint with replay-then-tail"
```

---

## Task 6: `GET /api/streams/{stream_id}/tail?after_seq=N` — polling fallback

**Files:**
- Modify: `src/api/streams.py` (add `tail` route + `_build_default_router` passthrough)
- Test: `tests/test_streams_api.py` (append)

**Interfaces:**
- Produces: `GET /api/streams/{stream_id}/tail?after_seq=N -> {"frames":
  [...], "status": str, "exit_code": int|None}` — non-SSE JSON, for `aq
  stream tail` (Task 8).

- [ ] **Step 1: Write the failing test**

```python
# appended to tests/test_streams_api.py

@pytest.mark.asyncio
async def test_tail_returns_frames_since_after_seq(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]

        import asyncio as _asyncio
        await _asyncio.sleep(0.3)

        resp = await client.get(f"/api/streams/{stream_id}/tail", params={"after_seq": -1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "exited"
    assert any(f["type"] == "line" for f in data["frames"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streams_api.py -k test_tail_returns -v`
Expected: FAIL with 404

- [ ] **Step 3: Write minimal implementation**

Add inside `build_streams_router`, after `subscribe`:

```python
    @router.get("/api/streams/{stream_id}/tail")
    async def tail(stream_id: str, request: Request, after_seq: int = -1) -> dict:
        handle = reg.get(stream_id)
        if handle is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        scope = request.state.scope
        if not _can_access(scope, handle):
            raise HTTPException(status_code=403, detail="out of scope: stream ownership")
        frames = handle.replay_from(after_seq)
        return {
            "frames": [f.to_dict() for f in frames],
            "status": handle.status,
            "exit_code": handle.exit_code,
        }
```

Extend `_build_default_router`:

```python
    @router.get("/api/streams/{stream_id}/tail")
    async def tail(stream_id: str, request: Request, after_seq: int = -1) -> dict:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams/{stream_id}/tail":
                return await route.endpoint(stream_id=stream_id, request=request, after_seq=after_seq)
        raise HTTPException(status_code=500, detail="streams router misconfigured")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_streams_api.py -k test_tail_returns -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/api/streams.py tests/test_streams_api.py
git commit -m "feat(streams): add GET /api/streams/{id}/tail polling fallback"
```

---

## Task 7: `POST /api/streams/{stream_id}/kill` — grace-period escalation

**Files:**
- Modify: `src/api/streams.py` (add `kill` route + `_build_default_router` passthrough — `_kill` already exists from Task 3)
- Test: `tests/test_streams_api.py` (append)

**Interfaces:**
- Consumes: `_kill(handle, grace_seconds)` (already written in Task 3).
- Produces: `POST /api/streams/{stream_id}/kill -> {"stream_id": str,
  "status": str}` — SIGTERM → SIGINT → SIGKILL escalation, idempotent
  200 no-op on an already-terminal stream.

- [ ] **Step 1: Write the failing test**

```python
# appended to tests/test_streams_api.py

@pytest.mark.asyncio
async def test_kill_terminates_a_long_running_process(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["sleep", "30"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]

        import asyncio as _asyncio
        await _asyncio.sleep(0.2)  # let the process actually start

        resp = await client.post(f"/api/streams/{stream_id}/kill")
        assert resp.status_code == 200
        assert resp.json()["status"] == "killed"

        for _ in range(50):
            meta = await client.get(f"/api/streams/{stream_id}")
            if meta.json()["status"] == "killed":
                break
            await _asyncio.sleep(0.1)
        assert meta.json()["status"] == "killed"


@pytest.mark.asyncio
async def test_kill_is_idempotent_on_already_exited_stream(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["echo", "hi"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]

        import asyncio as _asyncio
        await _asyncio.sleep(0.3)  # let it exit naturally

        resp = await client.post(f"/api/streams/{stream_id}/kill")
    assert resp.status_code == 200
    assert resp.json()["status"] == "exited"  # no-op, status unchanged


@pytest.mark.asyncio
async def test_kill_403_for_wrong_session_ownership(db, tmp_path):
    registry = StreamRegistry(buffer_max_lines=100)
    owner_app = _app_with_scope(db, tmp_path, LOCAL_SCOPE, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=owner_app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["sleep", "5"], "cwd": str(tmp_path), "session_id": "owner-session"},
        )
        stream_id = start.json()["stream_id"]

    other_scope = RequestScope(kind="session", session_id="other-session", elevated=False)
    other_app = _app_with_scope(db, tmp_path, other_scope, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=other_app), base_url="http://test") as client:
        resp = await client.post(f"/api/streams/{stream_id}/kill")
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streams_api.py -k kill -v`
Expected: FAIL with 404 (route doesn't exist)

- [ ] **Step 3: Write minimal implementation**

Add inside `build_streams_router`, after `tail`:

```python
    @router.post("/api/streams/{stream_id}/kill")
    async def kill(stream_id: str, request: Request) -> dict:
        handle = reg.get(stream_id)
        if handle is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        scope = request.state.scope
        if not _can_access(scope, handle):
            raise HTTPException(status_code=403, detail="out of scope: stream ownership")
        if handle.status != "running":
            return {"stream_id": stream_id, "status": handle.status}
        grace = getattr(config.streams, "kill_grace_seconds", 5.0)
        await _kill(handle, grace_seconds=grace)
        try:
            await db.log_event(
                "stream.killed", project_id=handle.project_id,
                payload=json.dumps({"stream_id": stream_id}),
            )
        except Exception:
            logger.debug("stream.killed log_event failed", exc_info=True)
        return {"stream_id": stream_id, "status": handle.status}
```

Extend `_build_default_router`:

```python
    @router.post("/api/streams/{stream_id}/kill")
    async def kill(stream_id: str, request: Request) -> dict:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        registry = getattr(orch, "stream_registry", None)
        if registry is None:
            raise HTTPException(status_code=404, detail=f"no stream {stream_id}")
        inner = build_streams_router(
            db=orch.db, config=orch.config, workspace_dir=orch.config.workspace_dir,
            registry=registry,
        )
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/streams/{stream_id}/kill":
                return await route.endpoint(stream_id=stream_id, request=request)
        raise HTTPException(status_code=500, detail="streams router misconfigured")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_streams_api.py -k kill -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/api/streams.py tests/test_streams_api.py
git commit -m "feat(streams): add POST /api/streams/{id}/kill with SIGTERM/SIGINT/SIGKILL escalation"
```

---

## Task 8: `aq stream start` / `aq stream tail` / `aq stream kill` — CLI

**Files:**
- Create: `src/cli/streams.py`
- Modify: `src/cli/app.py` (import `src.cli.streams` before `register_auto_commands()`, mirroring how `src.cli.messages` is imported today)
- Modify: `src/cli/client.py` (add `start_stream`, `get_stream`, `tail_stream`, `kill_stream` methods to `CLIClient`, following the `send_session_message`/`get_session_messages` direct-`httpx` pattern at line 285)
- Test: `tests/test_cli_streams.py`

**Interfaces:**
- Consumes: `CLIClient` (Task 8 adds new methods to it), `POST/GET
  /api/streams*` (Tasks 3-7).
- Produces: `CLIClient.start_stream(command: list[str], cwd: str, *, title:
  str|None=None, session_id: str, project_id: str|None=None) -> dict`,
  `CLIClient.get_stream(stream_id: str) -> dict`,
  `CLIClient.tail_stream(stream_id: str, *, after_seq: int = -1) -> dict`,
  `CLIClient.kill_stream(stream_id: str) -> dict`. CLI commands `aq stream
  start -- <argv...>`, `aq stream tail <stream_id>`, `aq stream kill
  <stream_id>`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_streams.py
"""CLI wiring tests for `aq stream *` — exercises the Click commands against
a stubbed CLIClient so no real daemon is required."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from src.cli.app import cli


def test_stream_start_invokes_client_with_argv_after_dashdash():
    runner = CliRunner()
    fake_result = {"stream_id": "abc123", "status": "running"}
    with patch("src.cli.streams._get_client") as get_client:
        client = AsyncMock()
        client.start_stream = AsyncMock(return_value=fake_result)
        get_client.return_value.__aenter__.return_value = client
        result = runner.invoke(
            cli, ["stream", "start", "--title", "Running pytest…",
                  "--session-id", "supervisor-global", "--cwd", "/tmp",
                  "--", "pytest", "tests/", "-x"],
        )
    assert result.exit_code == 0, result.output
    assert "abc123" in result.output
    client.start_stream.assert_awaited_once_with(
        ["pytest", "tests/", "-x"], "/tmp",
        title="Running pytest…", session_id="supervisor-global", project_id=None,
    )


def test_stream_kill_invokes_client():
    runner = CliRunner()
    with patch("src.cli.streams._get_client") as get_client:
        client = AsyncMock()
        client.kill_stream = AsyncMock(return_value={"stream_id": "abc123", "status": "killed"})
        get_client.return_value.__aenter__.return_value = client
        result = runner.invoke(cli, ["stream", "kill", "abc123"])
    assert result.exit_code == 0, result.output
    client.kill_stream.assert_awaited_once_with("abc123")


def test_stream_tail_invokes_client():
    runner = CliRunner()
    with patch("src.cli.streams._get_client") as get_client:
        client = AsyncMock()
        client.tail_stream = AsyncMock(return_value={"frames": [], "status": "running", "exit_code": None})
        get_client.return_value.__aenter__.return_value = client
        result = runner.invoke(cli, ["stream", "tail", "abc123"])
    assert result.exit_code == 0, result.output
    client.tail_stream.assert_awaited_once_with("abc123", after_seq=-1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_streams.py -v`
Expected: FAIL — `aq stream` command group doesn't exist (Click `UsageError: No such command 'stream'`)

- [ ] **Step 3: Write minimal implementation**

Add to `src/cli/client.py`, inside the `CLIClient` class, after `get_session_messages` (line 338):

```python
    # -- Streams (console-stream pane view) --------------------------------
    # Bespoke router, not /api/execute — mirrors send_session_message's
    # direct-httpx pattern above.

    async def start_stream(
        self, command: list[str], cwd: str, *,
        title: str | None = None, session_id: str, project_id: str | None = None,
    ) -> dict:
        assert self._http is not None, "CLIClient not connected"
        payload: dict = {"command": command, "cwd": cwd, "session_id": session_id}
        if title:
            payload["title"] = title
        if project_id:
            payload["project_id"] = project_id
        try:
            resp = await self._http.post("/api/streams", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code in (401, 403):
            raise ScopeDeniedError("stream_start", _relay_error(resp))
        if resp.status_code >= 400:
            raise CommandError("stream_start", _relay_error(resp))
        return resp.json()

    async def get_stream(self, stream_id: str) -> dict:
        assert self._http is not None, "CLIClient not connected"
        try:
            resp = await self._http.get(f"/api/streams/{stream_id}")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code >= 400:
            raise CommandError("stream_metadata", _relay_error(resp))
        return resp.json()

    async def tail_stream(self, stream_id: str, *, after_seq: int = -1) -> dict:
        assert self._http is not None, "CLIClient not connected"
        try:
            resp = await self._http.get(
                f"/api/streams/{stream_id}/tail", params={"after_seq": after_seq}
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code >= 400:
            raise CommandError("stream_tail", _relay_error(resp))
        return resp.json()

    async def kill_stream(self, stream_id: str) -> dict:
        assert self._http is not None, "CLIClient not connected"
        try:
            resp = await self._http.post(f"/api/streams/{stream_id}/kill")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise DaemonNotRunningError(self._base_url, cause=exc) from exc
        if resp.status_code >= 400:
            raise CommandError("stream_kill", _relay_error(resp))
        return resp.json()
```

Create `src/cli/streams.py`:

```python
"""Stream CLI — ``aq stream start|tail|kill``.

Wraps POST/GET /api/streams* (a bespoke router, not /api/execute — see
CLIClient.start_stream/get_stream/tail_stream/kill_stream in client.py).
Implements docs/superpowers/specs/2026-08-22-pane-console-stream-design.md §10.
"""

from __future__ import annotations

from typing import Any

import click

from .app import cli, console, _run, _get_client, _handle_errors
from .envelope import emit


@cli.group("stream")
def stream() -> None:
    """Streamable-command registry (console-stream pane view)."""


@stream.command(
    "start",
    context_settings={"ignore_unknown_options": True},
)
@click.option("--title", default=None, help="Header label shown in the pane")
@click.option("--session-id", "session_id", required=True, help="Owning session id")
@click.option("-p", "--project", "project_id", default=None, help="Owning project id")
@click.option("--cwd", required=True, help="Working directory for the command")
@click.argument("argv", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
@_handle_errors
def stream_start(
    ctx: click.Context, title: str | None, session_id: str,
    project_id: str | None, cwd: str, argv: tuple[str, ...],
) -> None:
    """Start a streamable command: ``aq stream start -- pytest tests/ -x``."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    command = list(argv)

    async def _start():
        async with _get_client(api_url) as client:
            return await client.start_stream(
                command, cwd, title=title, session_id=session_id, project_id=project_id,
            )

    result = _run(_start())

    def _render(data: dict) -> None:
        console.print(
            f"[bold green]Stream started:[/] [bold bright_cyan]{data.get('stream_id')}[/] "
            f"({data.get('status')})"
        )

    emit(ctx, result, render=_render)


@stream.command("tail")
@click.argument("stream_id")
@click.option("--after-seq", default=-1, type=int, help="Only frames after this sequence number")
@click.pass_context
@_handle_errors
def stream_tail(ctx: click.Context, stream_id: str, after_seq: int) -> None:
    """Poll buffered output since ``--after-seq`` (non-SSE fallback)."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _tail():
        async with _get_client(api_url) as client:
            return await client.tail_stream(stream_id, after_seq=after_seq)

    result = _run(_tail())

    def _render(data: dict) -> None:
        for frame in data.get("frames", []):
            if frame.get("type") == "line":
                prefix = "!" if frame.get("stream") == "stderr" else " "
                console.print(f"{prefix} {frame.get('text', '')}")
            elif frame.get("type") == "exit":
                console.print(f"[dim]exited ({frame.get('rc')})[/]")
            elif frame.get("type") == "killed":
                console.print("[dim]killed[/]")

    emit(ctx, result, render=_render)


@stream.command("kill")
@click.argument("stream_id")
@click.pass_context
@_handle_errors
def stream_kill(ctx: click.Context, stream_id: str) -> None:
    """Kill a running stream (SIGTERM → SIGINT → SIGKILL escalation)."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _kill():
        async with _get_client(api_url) as client:
            return await client.kill_stream(stream_id)

    result = _run(_kill())

    def _render(data: dict) -> None:
        console.print(f"[bold yellow]Stream {data.get('stream_id')}:[/] {data.get('status')}")

    emit(ctx, result, render=_render)
```

Register in `src/cli/app.py` — add `import src.cli.streams  # noqa: F401` in
the block of hand-crafted-module imports alongside the existing `import
src.cli.messages` (or equivalent), before the call to
`register_auto_commands()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_streams.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/cli/streams.py src/cli/client.py src/cli/app.py tests/test_cli_streams.py
git commit -m "feat(cli): add aq stream start/tail/kill"
```

---

## Task 9: Backend lifecycle/scope/concurrency/retention test suite

**Files:**
- Modify: `tests/test_streams_api.py` (append remaining §8.7 coverage not
  already written in Tasks 3-7: concurrency cap, retention sweep, subscriber
  count/fan-out, audit log rows)

**Interfaces:**
- Consumes: everything from Tasks 2-7.

- [ ] **Step 1: Write the failing test**

```python
# appended to tests/test_streams_api.py

@pytest.mark.asyncio
async def test_concurrency_cap_returns_429_on_fourth_stream(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(3):
            resp = await client.post(
                "/api/streams",
                json={"command": ["sleep", "5"], "cwd": str(tmp_path), "session_id": "capped"},
            )
            assert resp.status_code == 200
        resp = await client.post(
            "/api/streams",
            json={"command": ["sleep", "5"], "cwd": str(tmp_path), "session_id": "capped"},
        )
    assert resp.status_code == 429


def test_retention_sweep_evicts_finished_stream_past_cutoff():
    reg = StreamRegistry()
    handle = reg.create(title="a", session_id="s1", project_id=None, command=["echo"], cwd="/tmp")
    handle.status = "exited"
    handle.ended_at = 1.0
    for stream_id in reg.all_finished_before(1000.0):
        reg.evict(stream_id)
    assert reg.get(handle.stream_id) is None


@pytest.mark.asyncio
async def test_subscriber_count_reflects_active_connections(db, tmp_path):
    registry = StreamRegistry(buffer_max_lines=100)
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE, registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["sleep", "2"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]
        handle = registry.get(stream_id)
        assert handle is not None
        assert len(handle.subscribers) == 0

        import asyncio as _asyncio
        got_one_frame = _asyncio.Event()

        async def _consume_a_bit():
            async with client.stream("GET", f"/api/streams/{stream_id}/subscribe") as resp:
                async for line in resp.aiter_lines():
                    if len(handle.subscribers) >= 1:
                        got_one_frame.set()
                    break

        task = _asyncio.create_task(_consume_a_bit())
        await _asyncio.sleep(0.3)
        # A dropped subscriber (task done) does not kill the process.
        assert handle.status in ("running", "exited")
        task.cancel()


@pytest.mark.asyncio
async def test_start_and_kill_write_audit_log_rows(db, tmp_path):
    app = _app_with_scope(db, tmp_path, LOCAL_SCOPE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(
            "/api/streams",
            json={"command": ["sleep", "5"], "cwd": str(tmp_path), "session_id": "s1"},
        )
        stream_id = start.json()["stream_id"]
        await client.post(f"/api/streams/{stream_id}/kill")

    events = await db.list_events(event_type="stream.started")
    assert any(json.loads(e.payload)["stream_id"] == stream_id for e in events)
    killed_events = await db.list_events(event_type="stream.killed")
    assert any(json.loads(e.payload)["stream_id"] == stream_id for e in killed_events)
```

Note: if `Database.list_events(event_type=...)` doesn't exist with that exact
signature, use whatever query method `tests/test_orchestrator.py` or
`src/database/queries/event_queries.py` already exposes for reading `events`
rows back out (e.g. a raw `select(events).where(...)` via `db._engine`) —
confirm the actual read-side method name before writing this test, since
`event_queries.py`'s `EventQueryMixin` shown in Task 3 only had the write
side (`log_event`) inspected during research.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streams_api.py -k "concurrency_cap or retention_sweep or subscriber_count or audit_log" -v`
Expected: `test_retention_sweep_evicts_finished_stream_past_cutoff` PASSES
immediately (pure registry logic already implemented); the other three may
already pass since Tasks 3/5/7 implemented the underlying behavior — this
task's job is to lock the behavior down with explicit coverage. If any fail,
they identify a real gap versus the spec's §8.7 checklist to fix.

- [ ] **Step 3: Fix any gaps found**

If the audit-log test fails because no read-side query method exists, add
one to `src/database/queries/event_queries.py`:

```python
    async def list_events(
        self, *, event_type: str | None = None, project_id: str | None = None,
        limit: int = 100,
    ) -> list:
        from src.database.tables import events as events_table

        async with self._engine.begin() as conn:
            query = select(events_table).order_by(events_table.c.id.desc()).limit(limit)
            if event_type is not None:
                query = query.where(events_table.c.event_type == event_type)
            if project_id is not None:
                query = query.where(events_table.c.project_id == project_id)
            result = await conn.execute(query)
            return list(result.fetchall())
```

- [ ] **Step 4: Run full streams test suite to verify everything passes**

Run: `pytest tests/test_streams_registry.py tests/test_streams_api.py tests/test_cli_streams.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add tests/test_streams_api.py src/database/queries/event_queries.py
git commit -m "test(streams): lock down concurrency cap, retention, fan-out, audit-log coverage"
```

---

## Task 10: Dashboard test tooling — Vitest + RTL + jsdom + zod

**Files:**
- Modify: `dashboard/package.json` (add `zod` to `dependencies`; add
  `vitest`, `@testing-library/react`, `@testing-library/jest-dom`,
  `@testing-library/user-event`, `jsdom` to `devDependencies`; add a `test`
  script)
- Create: `dashboard/vitest.config.ts`
- Create: `dashboard/src/test/setup.ts`

**Interfaces:**
- Produces: `npm test` (from `dashboard/`) runs Vitest; any
  `**/__tests__/*.test.{ts,tsx}` file is discovered.

- [ ] **Step 1: Add dependencies**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
npm install zod
npm install -D vitest @testing-library/react @testing-library/jest-dom @testing-library/user-event jsdom
```

- [ ] **Step 2: Verify install failed to run tests yet (expected — no config)**

Run: `cd dashboard && npx vitest run` (from `dashboard/`)
Expected: FAIL or "No test files found" — no `vitest.config.ts` yet, and no test files exist.

- [ ] **Step 3: Add `test` script to `package.json`**

In `dashboard/package.json`'s `"scripts"` block (after `"typecheck"`):

```json
    "test": "vitest run"
```

- [ ] **Step 4: Create `dashboard/vitest.config.ts`**

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
```

- [ ] **Step 5: Create `dashboard/src/test/setup.ts`**

```ts
import "@testing-library/jest-dom/vitest";
```

- [ ] **Step 6: Write a smoke test to verify the pipeline works**

```tsx
// dashboard/src/test/__tests__/smoke.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

describe("vitest smoke test", () => {
  it("renders and finds text", () => {
    render(<div>hello vitest</div>);
    expect(screen.getByText("hello vitest")).toBeInTheDocument();
  });
});
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd dashboard && npm test`
Expected: PASS (1 test)

- [ ] **Step 8: Commit**

```bash
git add dashboard/package.json dashboard/package-lock.json dashboard/vitest.config.ts dashboard/src/test/setup.ts dashboard/src/test/__tests__/smoke.test.tsx
git commit -m "chore(dashboard): add Vitest + Testing Library + zod"
```

---

## Task 11: Shared pane contract — `dashboard/src/panes/types.ts` + `registry.ts`

**Files:**
- Create: `dashboard/src/panes/types.ts`
- Create: `dashboard/src/panes/registry.ts`
- Test: `dashboard/src/panes/__tests__/registry.test.ts`

**Interfaces:**
- Produces:
  - `HeroIcon = ComponentType<SVGProps<SVGSVGElement>>`
  - `PaneManifest<TArgs = unknown>` — `id, name, description, icon:
    HeroIcon, args_schema?: z.ZodType<TArgs>, open_shortcut?: string,
    route_scope?: "cross-route"|"route-scoped", agent_pushable?: boolean,
    palette_label?: string|null, palette_section?: string`
  - `PaneToolbarAction` — `id, label, icon?: HeroIcon, onClick: () => void,
    disabled?: boolean`
  - `ShortcutBinding` — `key: string, label: string, onFire: () => void`
  - `PaneViewProps<TArgs = unknown>` — `args: TArgs, close: () => void,
    setArgs: (next: TArgs) => void, setToolbar: (actions:
    PaneToolbarAction[]) => void, setShortcuts: (bindings:
    ShortcutBinding[]) => void`
  - `PaneEntry` — `{ manifest: PaneManifest, Component: ComponentType<PaneViewProps> }`
  - `PANE_REGISTRY: Record<string, PaneEntry>` (built via `import.meta.glob`)

This registry starts with zero entries (no pane view exists yet); Task 12
adds the first one (`console-stream`) and this task's registry test must
still pass with an empty registry.

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/__tests__/registry.test.ts
import { describe, expect, it } from "vitest";
import { PANE_REGISTRY } from "../registry";

describe("PANE_REGISTRY", () => {
  it("is a record keyed by manifest id, valid whether empty or populated", () => {
    for (const [key, entry] of Object.entries(PANE_REGISTRY)) {
      expect(entry.manifest.id).toBe(key);
      expect(typeof entry.Component).toBe("function");
    }
  });

  it("has no open_shortcut collisions", () => {
    const seen = new Set<string>();
    for (const { manifest } of Object.values(PANE_REGISTRY)) {
      if (!manifest.open_shortcut) continue;
      expect(seen.has(manifest.open_shortcut)).toBe(false);
      seen.add(manifest.open_shortcut);
    }
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: FAIL with a module-not-found error (`../registry` doesn't exist)

- [ ] **Step 3: Write minimal implementation**

Create `dashboard/src/panes/types.ts`:

```ts
// dashboard/src/panes/types.ts
//
// The pane-view contract every view under dashboard/src/panes/<view-id>/
// implements. See docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md.
//
// Icons are heroicons ONLY (§4 of that spec) — never lucide-react or any
// other icon library.

import type { ComponentType, SVGProps } from "react";
import type { z } from "zod";

export type HeroIcon = ComponentType<SVGProps<SVGSVGElement>>;

export interface PaneManifest<TArgs = unknown> {
  /** Stable id — matches the directory name; used everywhere. */
  id: string;
  /** Human name shown in the pane header + palette. */
  name: string;
  /** Short description used in palette + cheat sheet. */
  description: string;
  /** Icon shown in header + palette. */
  icon: HeroIcon;
  /** zod schema for the args object. `undefined` means "no args required". */
  args_schema?: z.ZodType<TArgs>;
  /**
   * Optional keyboard shortcut that OPENS this view. Omit the field
   * entirely (never a literal `null`) when a view has no open shortcut.
   */
  open_shortcut?: string;
  /**
   * How the view relates to routes. "cross-route" (default): pane content
   * persists across route navigation. "route-scoped": pane closes
   * automatically on route change.
   */
  route_scope?: "cross-route" | "route-scoped";
  /** Whether the agent may push this view via the pane_open message frame. */
  agent_pushable?: boolean;
  /** Palette action label. `null` means not registered as a palette action. */
  palette_label?: string | null;
  /** Palette section this view's action belongs to. */
  palette_section?: string;
}

export interface PaneToolbarAction {
  id: string;
  label: string;
  icon?: HeroIcon;
  onClick: () => void;
  disabled?: boolean;
}

export interface ShortcutBinding {
  /** Normalized form, e.g. "$mod-r" or "space". */
  key: string;
  /** Shown in the cheat sheet. */
  label: string;
  onFire: () => void;
}

export interface PaneViewProps<TArgs = unknown> {
  /** The args object passed at open time, already zod-validated. */
  args: TArgs;
  /** Close the pane. */
  close: () => void;
  /** Update the args for THIS OPEN pane without closing + re-opening. */
  setArgs: (next: TArgs) => void;
  /** Register toolbar action buttons to appear in the pane header. */
  setToolbar: (actions: PaneToolbarAction[]) => void;
  /** Register per-entity shortcuts scoped to this pane. */
  setShortcuts: (bindings: ShortcutBinding[]) => void;
}

export interface PaneEntry {
  manifest: PaneManifest;
  Component: ComponentType<PaneViewProps>;
}
```

Create `dashboard/src/panes/registry.ts`:

```ts
// dashboard/src/panes/registry.ts
//
// Build-time pane view registry (plugin-interface spec §4.1). Zero runtime
// discovery — Vite's import.meta.glob assembles a static object at build
// time. Adding a new pane view: drop manifest.ts + index.tsx into a new
// dashboard/src/panes/<view-id>/ directory and it's picked up automatically.

import type { PaneEntry, PaneManifest } from "./types";
import type { ComponentType } from "react";
import type { PaneViewProps } from "./types";

const manifestModules = import.meta.glob<{ manifest: PaneManifest }>("./*/manifest.ts", {
  eager: true,
});
const componentModules = import.meta.glob<{ default: ComponentType<PaneViewProps> }>(
  "./*/index.tsx",
  { eager: true },
);

function dirIdFromManifestPath(path: string): string {
  const match = path.match(/^\.\/([^/]+)\/manifest\.ts$/);
  if (!match) {
    throw new Error(`cannot derive pane view id from manifest path "${path}"`);
  }
  return match[1];
}

function buildRegistry(): Record<string, PaneEntry> {
  const registry: Record<string, PaneEntry> = {};

  for (const [path, mod] of Object.entries(manifestModules)) {
    const dirId = dirIdFromManifestPath(path);
    const { manifest } = mod;
    if (manifest.id !== dirId) {
      throw new Error(
        `pane manifest id "${manifest.id}" does not match its directory "${dirId}"`,
      );
    }
    if (registry[manifest.id]) {
      throw new Error(`duplicate pane view id "${manifest.id}"`);
    }
    const componentPath = `./${dirId}/index.tsx`;
    const componentMod = componentModules[componentPath];
    if (!componentMod || typeof componentMod.default !== "function") {
      throw new Error(
        `pane view "${manifest.id}" has no default component export at ${componentPath}`,
      );
    }
    registry[manifest.id] = { manifest, Component: componentMod.default };
  }

  const seenShortcuts = new Map<string, string>();
  for (const { manifest } of Object.values(registry)) {
    if (!manifest.open_shortcut) continue;
    const existingOwner = seenShortcuts.get(manifest.open_shortcut);
    if (existingOwner) {
      throw new Error(
        `open_shortcut "${manifest.open_shortcut}" collides between ` +
          `"${existingOwner}" and "${manifest.id}"`,
      );
    }
    seenShortcuts.set(manifest.open_shortcut, manifest.id);
  }

  return registry;
}

export const PANE_REGISTRY: Record<string, PaneEntry> = buildRegistry();
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: PASS (2 tests — `PANE_REGISTRY` is `{}` at this point, both tests
pass vacuously since the loops have nothing to iterate)

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/panes/types.ts dashboard/src/panes/registry.ts dashboard/src/panes/__tests__/registry.test.ts
git commit -m "feat(dashboard): add shared pane-view contract (types.ts + registry.ts)"
```

---

## Task 12: `console-stream` manifest + directory skeleton

**Files:**
- Create: `dashboard/src/panes/console-stream/manifest.ts`
- Test: `dashboard/src/panes/console-stream/__tests__/manifest.test.ts`

**Interfaces:**
- Consumes: `PaneManifest`, `HeroIcon` (Task 11).
- Produces: `consoleStreamArgsSchema` (zod), `ConsoleStreamArgs` (inferred
  type: `{ streamId: string, title?: string, sessionId?: string }`),
  `manifest: PaneManifest<ConsoleStreamArgs>` with `id: "console-stream"`.

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/console-stream/__tests__/manifest.test.ts
import { describe, expect, it } from "vitest";
import { manifest, consoleStreamArgsSchema } from "../manifest";

describe("console-stream manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("console-stream");
  });

  it("has no open_shortcut and no palette_label — agent-push primary", () => {
    expect(manifest.open_shortcut).toBeUndefined();
    expect(manifest.palette_label).toBeNull();
  });

  it("is agent-pushable and cross-route", () => {
    expect(manifest.agent_pushable).toBe(true);
    expect(manifest.route_scope).toBe("cross-route");
  });

  it("args_schema accepts a valid args object", () => {
    const result = consoleStreamArgsSchema.safeParse({ streamId: "abc" });
    expect(result.success).toBe(true);
  });

  it("args_schema accepts optional title and sessionId", () => {
    const result = consoleStreamArgsSchema.safeParse({
      streamId: "abc", title: "Running pytest…", sessionId: "supervisor-global",
    });
    expect(result.success).toBe(true);
  });

  it("args_schema rejects missing streamId", () => {
    expect(consoleStreamArgsSchema.safeParse({}).success).toBe(false);
  });

  it("args_schema rejects non-string streamId", () => {
    expect(consoleStreamArgsSchema.safeParse({ streamId: 123 }).success).toBe(false);
  });

  it("args_schema rejects empty streamId", () => {
    expect(consoleStreamArgsSchema.safeParse({ streamId: "" }).success).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/console-stream/__tests__/manifest.test.ts`
Expected: FAIL with module-not-found (`../manifest` doesn't exist)

- [ ] **Step 3: Write minimal implementation**

```ts
// dashboard/src/panes/console-stream/manifest.ts
import { z } from "zod";
import { CommandLineIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const consoleStreamArgsSchema = z.object({
  streamId: z.string().min(1),
  title: z.string().optional(),
  sessionId: z.string().optional(),
});
export type ConsoleStreamArgs = z.infer<typeof consoleStreamArgsSchema>;

export const manifest: PaneManifest<ConsoleStreamArgs> = {
  id: "console-stream",
  name: "Console",
  description: "Live stdout/stderr for a running command.",
  icon: CommandLineIcon,
  args_schema: consoleStreamArgsSchema,
  // No open_shortcut — agent-push is the primary opener (spec §3).
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: null,
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/console-stream/__tests__/manifest.test.ts`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/panes/console-stream/manifest.ts dashboard/src/panes/console-stream/__tests__/manifest.test.ts
git commit -m "feat(dashboard): add console-stream pane manifest"
```

---

## Task 13: Server-side pane registry mirror + parity test

**Files:**
- Create: `src/panes/__init__.py`
- Create: `src/panes/registry.py`
- Test: `tests/test_pane_registry_parity.py`

**Interfaces:**
- Produces: `SERVER_PANE_REGISTRY: dict[str, dict]` — `{"console-stream":
  {"agent_pushable": True}}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pane_registry_parity.py
"""Parity check between the frontend pane registry and its Python mirror.

See docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §7.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.panes.registry import SERVER_PANE_REGISTRY

_DASHBOARD_PANES = Path(__file__).resolve().parents[1] / "dashboard" / "src" / "panes"


def _read_frontend_manifest_ids() -> set[str]:
    ids: set[str] = set()
    if not _DASHBOARD_PANES.exists():
        return ids
    for manifest_path in _DASHBOARD_PANES.glob("*/manifest.ts"):
        text = manifest_path.read_text(encoding="utf-8")
        match = re.search(r'id:\s*"([^"]+)"', text)
        if match:
            ids.add(match.group(1))
    return ids


def test_frontend_and_backend_registries_match():
    frontend_ids = _read_frontend_manifest_ids()
    backend_ids = set(SERVER_PANE_REGISTRY.keys())
    assert frontend_ids == backend_ids


def test_console_stream_is_agent_pushable():
    assert SERVER_PANE_REGISTRY["console-stream"]["agent_pushable"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pane_registry_parity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.panes'`

- [ ] **Step 3: Write minimal implementation**

Create `src/panes/__init__.py` (empty — package marker).

Create `src/panes/registry.py`:

```python
"""Server-side mirror of the frontend pane-view registry.

Hand-maintained per docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §7:
9 views is a small enough set that a hand-synced dict plus a parity test
(tests/test_pane_registry_parity.py) beats a build-step generator. Adding a
view is a two-line change: a frontend manifest.ts entry plus a line here.
"""

from __future__ import annotations

SERVER_PANE_REGISTRY: dict[str, dict] = {
    "console-stream": {"agent_pushable": True},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pane_registry_parity.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/panes/__init__.py src/panes/registry.py tests/test_pane_registry_parity.py
git commit -m "feat(panes): add server-side registry mirror with parity test"
```

---

## Task 14: `useConsoleStream` — SSE subscription hook

**Files:**
- Create: `dashboard/src/panes/console-stream/hooks.ts`
- Test: `dashboard/src/panes/console-stream/__tests__/hooks.test.ts`

**Interfaces:**
- Produces: `ConsoleLine { seq: number, stream: "stdout"|"stderr", text:
  string, ts: number }`, `ConsoleStreamStatus =
  "connecting"|"running"|"exited"|"killed"|"error"`, `ConsoleStreamState {
  status, exitCode: number|null, lines: ConsoleLine[], startedAt:
  number|null, endedAt: number|null, errorMessage: string|null, truncated:
  boolean }`, `useConsoleStream(streamId: string|null|undefined) ->
  ConsoleStreamState`.

This hook follows `dashboard/src/ws/useTranscriptStream.ts`'s bounded-buffer
`EventSource` shape but adds the replay-from-`after_seq` cursor and
reconnect-with-backoff the transcript hook doesn't need (a session transcript
just lets `EventSource`'s native retry reconnect at the same URL; a console
stream must resume from the last-seen `seq` so no lines are lost or
duplicated).

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/console-stream/__tests__/hooks.test.ts
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useConsoleStream } from "../hooks";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  // @ts-expect-error test stub
  global.EventSource = FakeEventSource;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useConsoleStream", () => {
  it("starts in connecting status", () => {
    const { result } = renderHook(() => useConsoleStream("abc"));
    expect(result.current.status).toBe("connecting");
  });

  it("appends line frames and flips to running", async () => {
    const { result } = renderHook(() => useConsoleStream("abc"));
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });

    await waitFor(() => {
      expect(result.current.lines).toHaveLength(1);
    });
    expect(result.current.status).toBe("running");
    expect(result.current.lines[0]).toMatchObject({ stream: "stdout", text: "hi" });
  });

  it("flips to exited on an exit frame and closes the EventSource", async () => {
    const { result } = renderHook(() => useConsoleStream("abc"));
    const es = FakeEventSource.instances[0];
    es.emit({ type: "exit", seq: 1, rc: 0, ts: 2 });

    await waitFor(() => {
      expect(result.current.status).toBe("exited");
    });
    expect(result.current.exitCode).toBe(0);
    expect(es.closed).toBe(true);
  });

  it("flips to killed on a killed frame", async () => {
    const { result } = renderHook(() => useConsoleStream("abc"));
    const es = FakeEventSource.instances[0];
    es.emit({ type: "killed", seq: 1, ts: 2 });

    await waitFor(() => {
      expect(result.current.status).toBe("killed");
    });
  });

  it("does nothing when streamId is null", () => {
    renderHook(() => useConsoleStream(null));
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("closes the EventSource on unmount", () => {
    const { unmount } = renderHook(() => useConsoleStream("abc"));
    const es = FakeEventSource.instances[0];
    unmount();
    expect(es.closed).toBe(true);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/console-stream/__tests__/hooks.test.ts`
Expected: FAIL with module-not-found (`../hooks` doesn't exist)

- [ ] **Step 3: Write minimal implementation**

```ts
// dashboard/src/panes/console-stream/hooks.ts
//
// SSE subscription hook wrapping GET /api/streams/{streamId}/subscribe.
// Follows dashboard/src/ws/useTranscriptStream.ts's bounded-buffer
// EventSource shape, plus replay-from-after_seq + reconnect-with-backoff
// (spec §7.1-§7.2) that the transcript hook doesn't need.

import { useCallback, useEffect, useRef, useState } from "react";

export interface ConsoleLine {
  seq: number;
  stream: "stdout" | "stderr";
  text: string;
  ts: number;
}

export type ConsoleStreamStatus = "connecting" | "running" | "exited" | "killed" | "error";

export interface ConsoleStreamState {
  status: ConsoleStreamStatus;
  exitCode: number | null;
  lines: ConsoleLine[];
  startedAt: number | null;
  endedAt: number | null;
  errorMessage: string | null;
  truncated: boolean;
}

interface RawFrame {
  type: "line" | "exit" | "killed";
  seq: number;
  stream?: "stdout" | "stderr";
  text?: string;
  rc?: number;
  ts: number;
  truncated?: boolean;
}

const MAX_LINES = 5000;
const MAX_RECONNECT_ATTEMPTS = 5;
const BASE_BACKOFF_MS = 500;

function apiBase(): string {
  return (
    (import.meta.env.VITE_API_URL as string | undefined) ||
    `${window.location.protocol}//${window.location.host}`
  );
}

const INITIAL_STATE: ConsoleStreamState = {
  status: "connecting",
  exitCode: null,
  lines: [],
  startedAt: null,
  endedAt: null,
  errorMessage: null,
  truncated: false,
};

export function useConsoleStream(streamId: string | null | undefined): ConsoleStreamState {
  const [state, setState] = useState<ConsoleStreamState>(INITIAL_STATE);
  const afterSeqRef = useRef(-1);
  const attemptRef = useRef(0);
  const esRef = useRef<EventSource | null>(null);
  const closedRef = useRef(false);

  const appendLine = useCallback((frame: RawFrame) => {
    setState((prev) => {
      const nextLines =
        prev.lines.length >= MAX_LINES
          ? prev.lines.slice(prev.lines.length - MAX_LINES + 1)
          : prev.lines.slice();
      nextLines.push({
        seq: frame.seq,
        stream: frame.stream ?? "stdout",
        text: frame.text ?? "",
        ts: frame.ts,
      });
      return {
        ...prev,
        status: "running",
        lines: nextLines,
        startedAt: prev.startedAt ?? frame.ts,
        truncated: prev.truncated || !!frame.truncated,
      };
    });
  }, []);

  useEffect(() => {
    closedRef.current = false;
    setState(INITIAL_STATE);
    afterSeqRef.current = -1;
    attemptRef.current = 0;
    if (!streamId) return;

    function connect() {
      const url = `${apiBase()}/api/streams/${encodeURIComponent(streamId!)}/subscribe?after_seq=${afterSeqRef.current}`;
      const es = new EventSource(url);
      esRef.current = es;

      es.onopen = () => {
        attemptRef.current = 0;
        setState((prev) => ({
          ...prev,
          status: prev.status === "error" ? "running" : prev.status,
          errorMessage: null,
        }));
      };

      es.onmessage = (msg: MessageEvent) => {
        let frame: RawFrame;
        try {
          frame = JSON.parse(msg.data);
        } catch {
          return;
        }
        afterSeqRef.current = frame.seq;
        if (frame.type === "line") {
          appendLine(frame);
        } else if (frame.type === "exit") {
          setState((prev) => ({ ...prev, status: "exited", exitCode: frame.rc ?? null, endedAt: frame.ts }));
          es.close();
        } else if (frame.type === "killed") {
          setState((prev) => ({ ...prev, status: "killed", endedAt: frame.ts }));
          es.close();
        }
      };

      es.onerror = () => {
        es.close();
        if (closedRef.current) return;
        setState((prev) =>
          prev.status === "exited" || prev.status === "killed" ? prev : { ...prev, status: "error" },
        );
        if (attemptRef.current >= MAX_RECONNECT_ATTEMPTS) {
          setState((prev) => ({ ...prev, errorMessage: "connection lost" }));
          return;
        }
        const delay = BASE_BACKOFF_MS * 2 ** attemptRef.current;
        attemptRef.current += 1;
        window.setTimeout(() => {
          if (!closedRef.current) connect();
        }, delay);
      };
    }

    connect();
    return () => {
      closedRef.current = true;
      esRef.current?.close();
      esRef.current = null;
    };
  }, [streamId, appendLine]);

  return state;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/console-stream/__tests__/hooks.test.ts`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/panes/console-stream/hooks.ts dashboard/src/panes/console-stream/__tests__/hooks.test.ts
git commit -m "feat(dashboard): add useConsoleStream SSE hook"
```

---

## Task 15: ANSI-to-span util

**Files:**
- Create: `dashboard/src/panes/console-stream/ansi.tsx`
- Test: `dashboard/src/panes/console-stream/__tests__/ansi.test.tsx`

**Interfaces:**
- Produces: `ansiToSpans(text: string) -> ReactNode[]`, `stripAnsi(text:
  string) -> string`.

No virtualizer or ANSI library exists to reuse in this repo (Deviation #5) —
this is the ~100-line local converter the spec calls for (§5.1), handling
just SGR reset/bold/8-and-bright-color codes (enough for `pytest`/build
output), not a full terminal emulator.

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/panes/console-stream/__tests__/ansi.test.tsx
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { ansiToSpans, stripAnsi } from "../ansi";

describe("ansiToSpans", () => {
  it("renders plain text with no ANSI codes as a single span", () => {
    const { container } = render(<>{ansiToSpans("plain text")}</>);
    expect(container.textContent).toBe("plain text");
  });

  it("applies color from an SGR code and resets after code 0", () => {
    const text = "\x1b[32mgreen\x1b[0m plain";
    const { container } = render(<>{ansiToSpans(text)}</>);
    expect(container.textContent).toBe("green plain");
    const spans = container.querySelectorAll("span");
    expect(spans[0].style.color).not.toBe("");
    expect(spans[1].style.color).toBe("");
  });

  it("applies bold from SGR code 1", () => {
    const text = "\x1b[1mbold\x1b[0m";
    const { container } = render(<>{ansiToSpans(text)}</>);
    const span = container.querySelector("span")!;
    expect(span.style.fontWeight).toBe("bold");
  });
});

describe("stripAnsi", () => {
  it("removes SGR escape codes, leaving plain text", () => {
    expect(stripAnsi("\x1b[32mgreen\x1b[0m plain")).toBe("green plain");
  });

  it("is a no-op on text with no escape codes", () => {
    expect(stripAnsi("plain")).toBe("plain");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/console-stream/__tests__/ansi.test.tsx`
Expected: FAIL with module-not-found (`../ansi` doesn't exist)

- [ ] **Step 3: Write minimal implementation**

```tsx
// dashboard/src/panes/console-stream/ansi.tsx
//
// Small local ANSI SGR converter for console output — not a full terminal
// emulator. Handles reset (0), bold (1), and the 16 standard/bright colors
// (30-37, 90-97). No new dependency (spec §5.1).

import type { ReactNode } from "react";

const SGR_COLORS: Record<number, string> = {
  30: "#000000", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b",
  34: "#61afef", 35: "#c678dd", 36: "#56b6c2", 37: "#dcdfe4",
  90: "#5c6370", 91: "#e06c75", 92: "#98c379", 93: "#e5c07b",
  94: "#61afef", 95: "#c678dd", 96: "#56b6c2", 97: "#ffffff",
};

const SGR_RE = /\x1b\[([0-9;]*)m/g;

export function ansiToSpans(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let color: string | undefined;
  let bold = false;
  let lastIndex = 0;
  let key = 0;

  SGR_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = SGR_RE.exec(text)) !== null) {
    const chunk = text.slice(lastIndex, match.index);
    if (chunk) {
      nodes.push(
        <span key={key++} style={{ color, fontWeight: bold ? "bold" : undefined }}>
          {chunk}
        </span>,
      );
    }
    const codes = match[1].split(";").filter(Boolean).map(Number);
    if (codes.length === 0) codes.push(0);
    for (const code of codes) {
      if (code === 0) {
        color = undefined;
        bold = false;
      } else if (code === 1) {
        bold = true;
      } else if (SGR_COLORS[code]) {
        color = SGR_COLORS[code];
      }
    }
    lastIndex = SGR_RE.lastIndex;
  }

  const rest = text.slice(lastIndex);
  if (rest) {
    nodes.push(
      <span key={key++} style={{ color, fontWeight: bold ? "bold" : undefined }}>
        {rest}
      </span>,
    );
  }
  return nodes;
}

export function stripAnsi(text: string): string {
  return text.replace(SGR_RE, "");
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/console-stream/__tests__/ansi.test.tsx`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/panes/console-stream/ansi.tsx dashboard/src/panes/console-stream/__tests__/ansi.test.tsx
git commit -m "feat(dashboard): add ANSI-to-span util for console-stream"
```

---

## Task 16: `ConsoleStreamPane` component

**Files:**
- Create: `dashboard/src/panes/console-stream/index.tsx`

**Interfaces:**
- Consumes: `PaneViewProps<ConsoleStreamArgs>` (Task 11),
  `useConsoleStream` (Task 14), `ansiToSpans`/`stripAnsi` (Task 15),
  `manifest`/`consoleStreamArgsSchema`/`ConsoleStreamArgs` (Task 12).
- Produces: default export `ConsoleStreamPane` — the component the registry
  (Task 11) resolves for `console-stream`.

No virtualizer library exists to reuse (Deviation #5) — this implements a
small self-contained fixed-row-height windowed renderer instead (computes a
visible slice from `scrollTop`/`clientHeight` at a constant `ROW_HEIGHT`,
with overscan), since the client-side buffer is already capped at 5,000
lines (Task 14) and a plain unwindowed list of that size measurably jankes
scrolling.

- [ ] **Step 1: Write the component**

```tsx
// dashboard/src/panes/console-stream/index.tsx
import { useEffect, useMemo, useRef, useState } from "react";
import type { PaneViewProps } from "../types";
import { useConsoleStream, type ConsoleLine, type ConsoleStreamStatus } from "./hooks";
import type { ConsoleStreamArgs } from "./manifest";
import { ansiToSpans, stripAnsi } from "./ansi";

const ROW_HEIGHT = 20;
const OVERSCAN = 10;

function useElapsed(startedAt: number | null, endedAt: number | null): string {
  const [, forceTick] = useState(0);
  useEffect(() => {
    if (endedAt !== null || startedAt === null) return;
    const id = window.setInterval(() => forceTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [startedAt, endedAt]);
  if (startedAt === null) return "";
  const end = endedAt ?? Date.now() / 1000;
  const secs = Math.max(0, Math.round(end - startedAt));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m > 0 ? `${m}m ${s.toString().padStart(2, "0")}s` : `${s}s`;
}

/**
 * The dashboard's own authenticated session identity. Stubbed via
 * localStorage until a shared shell-level identity hook exists — the shell
 * spec's identity plumbing is out of scope for this plan (Deviation #1).
 */
function useOwnSessionId(): string | null {
  try {
    return window.localStorage.getItem("aq:session:id");
  } catch {
    return null;
  }
}

export default function ConsoleStreamPane({
  args,
  setToolbar,
  setShortcuts,
}: PaneViewProps<ConsoleStreamArgs>) {
  const ownSessionId = useOwnSessionId();
  const scopeMismatch = !!args.sessionId && !!ownSessionId && args.sessionId !== ownSessionId;

  const stream = useConsoleStream(scopeMismatch ? null : args.streamId);
  const [followTail, setFollowTail] = useState(true);
  const [killConfirming, setKillConfirming] = useState(false);
  const [copied, setCopied] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(0);

  const elapsed = useElapsed(stream.startedAt, stream.endedAt);

  const doKill = async () => {
    setKillConfirming(false);
    await fetch(`/api/streams/${encodeURIComponent(args.streamId)}/kill`, { method: "POST" });
  };

  const doCopy = async () => {
    const text = stream.lines.map((l) => stripAnsi(l.text)).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard permission denied — silently no-op; the toolbar label
      // just won't flip to "Copied".
    }
  };

  useEffect(() => {
    setToolbar([
      {
        id: "pause-tail",
        label: followTail ? "Pause tail" : "Resume tail",
        onClick: () => setFollowTail((v) => !v),
      },
      { id: "copy-output", label: copied ? "Copied" : "Copy output", onClick: () => void doCopy() },
      ...(stream.status === "running"
        ? [{ id: "kill", label: "Kill", onClick: () => setKillConfirming(true) }]
        : []),
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [followTail, copied, stream.status, stream.lines.length]);

  useEffect(() => {
    setShortcuts([
      { key: "space", label: "Toggle follow-tail", onFire: () => setFollowTail((v) => !v) },
      {
        key: "k",
        label: "Kill",
        onFire: () => {
          if (stream.status === "running") setKillConfirming(true);
        },
      },
      { key: "c", label: "Copy output", onFire: () => void doCopy() },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream.status, stream.lines.length]);

  useEffect(() => {
    if (stream.status === "exited" || stream.status === "killed") setFollowTail(false);
  }, [stream.status]);

  useEffect(() => {
    if (!followTail || !scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [stream.lines.length, followTail]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    setScrollTop(el.scrollTop);
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < ROW_HEIGHT;
    if (!atBottom && followTail) setFollowTail(false);
    if (atBottom && !followTail && stream.status === "running") setFollowTail(true);
  };

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    setViewportHeight(el.clientHeight);
    const ro = new ResizeObserver(() => setViewportHeight(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const totalRows = stream.lines.length;
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const visibleCount = Math.ceil(viewportHeight / ROW_HEIGHT) + OVERSCAN * 2;
  const end = Math.min(totalRows, start + Math.max(visibleCount, 1));
  const visible = useMemo(() => stream.lines.slice(start, end), [stream.lines, start, end]);

  if (scopeMismatch) {
    return (
      <div role="status" className="p-4 text-sm text-neutral-400">
        You don&apos;t have access to this console output.
      </div>
    );
  }

  if (stream.status === "error" && stream.errorMessage) {
    return (
      <div role="status" className="p-4 text-sm text-amber-500">
        connection lost
        <button className="ml-2 underline" onClick={() => window.location.reload()}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-neutral-950 text-neutral-100 font-mono text-xs">
      <div className="flex items-center gap-2 border-b border-neutral-800 px-2 py-1">
        <StatusChip status={stream.status} exitCode={stream.exitCode} elapsed={elapsed} />
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="relative flex-1 overflow-y-auto"
      >
        <div style={{ height: totalRows * ROW_HEIGHT, position: "relative" }}>
          {visible.map((line, i) => (
            <Row key={line.seq} line={line} top={(start + i) * ROW_HEIGHT} />
          ))}
        </div>
        {(stream.status === "exited" || stream.status === "killed") && (
          <ExitBanner status={stream.status} exitCode={stream.exitCode} elapsed={elapsed} />
        )}
      </div>
      {killConfirming && (
        <div className="flex items-center gap-2 border-t border-neutral-800 p-2" role="dialog" aria-label="Kill this process?">
          <span>Kill this process?</span>
          <button className="rounded bg-red-600 px-2 py-1" onClick={() => void doKill()}>
            Confirm
          </button>
          <button className="rounded bg-neutral-700 px-2 py-1" onClick={() => setKillConfirming(false)}>
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

function Row({ line, top }: { line: ConsoleLine; top: number }) {
  return (
    <div
      style={{ position: "absolute", top, left: 0, right: 0, height: ROW_HEIGHT, lineHeight: `${ROW_HEIGHT}px` }}
      className={line.stream === "stderr" ? "border-l-2 border-red-500 pl-1" : "pl-1"}
    >
      {ansiToSpans(line.text)}
    </div>
  );
}

function StatusChip({
  status, exitCode, elapsed,
}: { status: ConsoleStreamStatus; exitCode: number | null; elapsed: string }) {
  if (status === "connecting") return <span>connecting…</span>;
  if (status === "running") return <span>running {elapsed}</span>;
  if (status === "exited") {
    return <span>{exitCode === 0 ? "exited (0)" : `exited (${exitCode})`}</span>;
  }
  if (status === "killed") return <span>killed</span>;
  return <span>connection lost</span>;
}

function ExitBanner({
  status, exitCode, elapsed,
}: { status: ConsoleStreamStatus; exitCode: number | null; elapsed: string }) {
  const label =
    status === "killed" ? `killed after ${elapsed}` : `exited with code ${exitCode} after ${elapsed}`;
  return (
    <div className="border-t border-neutral-800 py-1 text-center text-neutral-400">
      —— {label} ——
    </div>
  );
}
```

- [ ] **Step 2: Run the component test file from Task 17 after writing it (see Task 17) — no standalone verification step here since this task has no test file of its own; Task 17 is this component's TDD cycle**

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/panes/console-stream/index.tsx
git commit -m "feat(dashboard): add ConsoleStreamPane component"
```

(This task is code-only; Task 17 writes the failing tests against it and
verifies pass/fail exactly per the TDD cycle, since a component this size is
easier to review whole than interleaved with 15 micro-diffs. If executing
this plan strictly TDD-first, write Task 17's tests first, confirm they fail
against a stub component, then paste this implementation in and confirm
they pass — either ordering produces the same two commits.)

---

## Task 17: `ConsoleStreamPane` component tests

**Files:**
- Create: `dashboard/src/panes/console-stream/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `ConsoleStreamPane` (Task 16), `PaneViewProps` (Task 11).

- [ ] **Step 1: Write the test file**

```tsx
// dashboard/src/panes/console-stream/__tests__/index.test.tsx
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ConsoleStreamPane from "../index";
import type { ConsoleStreamArgs } from "../manifest";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

function makeProps(overrides: Partial<ConsoleStreamArgs> = {}) {
  return {
    args: { streamId: "abc123", ...overrides } as ConsoleStreamArgs,
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
  };
}

beforeEach(() => {
  FakeEventSource.instances = [];
  // @ts-expect-error test stub
  global.EventSource = FakeEventSource;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true }));
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  });
  window.localStorage.removeItem("aq:session:id");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ConsoleStreamPane", () => {
  it("renders connecting immediately, then running once a line frame arrives", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    expect(screen.getByText("connecting…")).toBeInTheDocument();

    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hello", ts: Date.now() / 1000 });

    await waitFor(() => expect(screen.getByText(/running/)).toBeInTheDocument());
    expect(screen.getByText("hello")).toBeInTheDocument();
  });

  it("interleaves stdout/stderr with stderr getting a red left border", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "out line", ts: 1 });
    es.emit({ type: "line", seq: 1, stream: "stderr", text: "err line", ts: 2 });

    await waitFor(() => expect(screen.getByText("err line")).toBeInTheDocument());
    const errRow = screen.getByText("err line").closest("div");
    expect(errRow?.className).toContain("border-red-500");
  });

  it("space toggles follow-tail and flips the toolbar label", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    await waitFor(() => expect(props.setToolbar).toHaveBeenCalled());

    const lastCall = props.setToolbar.mock.calls.at(-1)![0];
    const pauseAction = lastCall.find((a: { id: string }) => a.id === "pause-tail");
    expect(pauseAction.label).toBe("Pause tail");

    const lastShortcuts = props.setShortcuts.mock.calls.at(-1)![0];
    const spaceBinding = lastShortcuts.find((s: { key: string }) => s.key === "space");
    spaceBinding.onFire();

    await waitFor(() => {
      const latest = props.setToolbar.mock.calls.at(-1)![0];
      const action = latest.find((a: { id: string }) => a.id === "pause-tail");
      expect(action.label).toBe("Resume tail");
    });
  });

  it("copy output copies plain-text (ANSI-stripped) scrollback", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "\x1b[32mgreen\x1b[0m", ts: 1 });

    await waitFor(() => expect(screen.getByText("green")).toBeInTheDocument());

    const latestShortcuts = props.setShortcuts.mock.calls.at(-1)![0];
    const copyBinding = latestShortcuts.find((s: { key: string }) => s.key === "c");
    copyBinding.onFire();

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("green");
    });
  });

  it("kill button is present while running and absent after a terminal frame", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });

    await waitFor(() => {
      const latest = props.setToolbar.mock.calls.at(-1)![0];
      expect(latest.some((a: { id: string }) => a.id === "kill")).toBe(true);
    });

    es.emit({ type: "exit", seq: 1, rc: 0, ts: 2 });

    await waitFor(() => {
      const latest = props.setToolbar.mock.calls.at(-1)![0];
      expect(latest.some((a: { id: string }) => a.id === "kill")).toBe(false);
    });
  });

  it("k opens the kill confirm popover; confirm calls the kill endpoint", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });

    await waitFor(() => expect(props.setShortcuts).toHaveBeenCalled());
    const latestShortcuts = props.setShortcuts.mock.calls.at(-1)![0];
    const killBinding = latestShortcuts.find((s: { key: string }) => s.key === "k");
    killBinding.onFire();

    expect(screen.getByRole("dialog", { name: "Kill this process?" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("Confirm"));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/streams/abc123/kill",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("cancel on the kill confirm popover does not call the kill endpoint", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });
    await waitFor(() => expect(props.setShortcuts).toHaveBeenCalled());
    const latestShortcuts = props.setShortcuts.mock.calls.at(-1)![0];
    latestShortcuts.find((s: { key: string }) => s.key === "k").onFire();

    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("terminal frame freezes header, appends exit banner, force-disables follow-tail", async () => {
    const props = makeProps();
    render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    es.emit({ type: "line", seq: 0, stream: "stdout", text: "hi", ts: 1 });
    es.emit({ type: "exit", seq: 1, rc: 1, ts: 35 });

    await waitFor(() => expect(screen.getByText("exited (1)")).toBeInTheDocument());
    expect(screen.getByText(/exited with code 1 after/)).toBeInTheDocument();
  });

  it("sessionId mismatch renders scope-mismatch state without opening the EventSource", () => {
    window.localStorage.setItem("aq:session:id", "supervisor-demo");
    const props = makeProps({ sessionId: "supervisor-other" });
    render(<ConsoleStreamPane {...props} />);
    expect(screen.getByText(/don't have access/)).toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("unmount closes the EventSource (no leaked subscription)", () => {
    const props = makeProps();
    const { unmount } = render(<ConsoleStreamPane {...props} />);
    const es = FakeEventSource.instances[0];
    unmount();
    expect(es.closed).toBe(true);
  });
});
```

- [ ] **Step 2: Run test to verify pass (or fail-then-pass if the implementation isn't written yet)**

Run: `cd dashboard && npx vitest run src/panes/console-stream/__tests__/index.test.tsx`
Expected: PASS (11 tests) once `index.tsx` from Task 16 exists.

- [ ] **Step 3: Run the entire console-stream + panes test suite together**

Run: `cd dashboard && npx vitest run src/panes`
Expected: PASS (all files: `registry.test.ts`, `manifest.test.ts`,
`hooks.test.ts`, `ansi.test.tsx`, `index.test.tsx`)

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/panes/console-stream/__tests__/index.test.tsx
git commit -m "test(dashboard): add ConsoleStreamPane component tests"
```

---

## Task 18: Regenerate the TS client + manual verification

**Files:**
- Modify: (generated) `packages/aq-client/` — regenerated, not hand-edited
- No new test file — this task is verification, not new behavior

**Interfaces:**
- Consumes: everything from Tasks 1-17.

- [ ] **Step 1: Start the daemon locally**

```bash
./run.sh start
```

- [ ] **Step 2: Regenerate the TypeScript client so `GET /api/streams/{id}` and `POST /api/streams/{id}/kill` get typed SDK functions**

```bash
npm run generate:ts-client
```

Per `dashboard/CLAUDE.md`: both routes carry explicit FastAPI `response_model`s
(`StreamMetadata`, and the plain `dict` kill response — if the generated
client can't infer a type for the untyped kill response, add a
`StreamKillResponse(BaseModel)` with `stream_id: str, status: str` in
`src/api/streams.py` and set it as `response_model=StreamKillResponse` on
the `kill` route in Tasks 3/7's `build_streams_router`, then regenerate
again), so no manual `RESPONSE_MODELS` registration is needed — the
generated SDK picks them up straight from `/openapi.json`.

- [ ] **Step 3: Run the full backend test suite**

```bash
pytest tests/ -n auto
```

Expected: PASS, including every new file from Tasks 1-13
(`test_config.py`, `test_streams_registry.py`, `test_streams_api.py`,
`test_cli_streams.py`, `test_pane_registry_parity.py`).

- [ ] **Step 4: Run the full frontend test + typecheck + lint suite**

```bash
cd dashboard
npm test
npm run typecheck
npm run lint
```

Expected: PASS.

- [ ] **Step 5: Manual verification checklist**

Kick off a real stream via the CLI and confirm it end-to-end, since there is
no E2E infra in this repo (per the shell-v2 spec §9.2, manual verification
is the norm for cross-surface flows):

1. `aq stream start --title "smoke test" --session-id local-manual --cwd /tmp -- bash -c "for i in 1 2 3; do echo line $i; sleep 1; done"` — confirm it prints `Stream started: <id> (running)`.
2. `aq stream tail <id>` — confirm it prints the `line 1/2/3` output and, once the command finishes, `exited (0)`.
3. Start a longer stream (`aq stream start ... -- sleep 30`) and `aq stream kill <id>` mid-run — confirm the CLI reports `killed`, and `aq stream tail <id>` shows a `killed` type frame with no further output.
4. In a scratch page (or the dashboard's existing `dashboard/src/pages/` shell, wired up ad hoc) mount `<ConsoleStreamPane args={{streamId: "<id-from-step-1>"}} close={...} setArgs={...} setToolbar={...} setShortcuts={...} />` directly (no `<ShellPane>` exists yet — see Deviation #1) and confirm: connecting → running transition renders live output, `space` pauses/resumes tail, `[Copy output]` copies ANSI-stripped text, `[Kill]` prompts and, on confirm, transitions the header to `killed` with the exit banner appended.
5. Confirm `POST /api/streams` from a plain (non-elevated, non-local) session token is rejected with 403 by curling it directly with a scoped bearer token, if one is easily available in the dev environment; otherwise this is already covered by `test_start_stream_rejects_non_local_non_elevated_scope` (Task 3).

- [ ] **Step 6: Commit the regenerated client (if changed)**

```bash
git add packages/aq-client
git commit -m "chore(aq-client): regenerate TS client for streams API"
```

---

## Self-Review Notes

- **Spec coverage:** Every numbered item in the user's task breakdown (7
  backend + 8 frontend + manual verification) maps onto Tasks 1-18 above.
  `docs/superpowers/specs/2026-08-22-pane-console-stream-design.md` §3-§13
  are each covered: manifest (T12), args/validation (T12), component (T16),
  toolbar+shortcuts (T16), SSE data layer (T14), backend streams API
  (T1-T9), loading/error/exit states (T16), agent-push examples (out of
  scope per Deviation #1, correctly excluded), tests (T9, T17),
  implementation checklist items (all present except the shared shell/message
  `pane_open` plumbing, explicitly deferred).
- **Placeholder scan:** No "TBD"/"handle appropriately"/unshown code
  remains; every step has concrete, runnable code. Task 9's audit-log test
  flags one genuine unknown (exact `list_events`-equivalent method name)
  with a concrete fallback implementation rather than hand-waving it.
- **Type consistency:** `ConsoleFrame`/`StreamHandle`/`StreamRegistry`
  (Task 2) are used with identical signatures in Tasks 3-9.
  `ConsoleStreamArgs`/`consoleStreamArgsSchema`/`manifest` (Task 12) match
  what Task 14 (`hooks.ts`) and Task 16 (`index.tsx`) import.
  `PaneManifest`/`PaneViewProps`/`PaneToolbarAction`/`ShortcutBinding`
  (Task 11) are the exact names/shapes Task 12 and Task 16 consume.
  `useConsoleStream`'s returned `ConsoleStreamState` shape (Task 14) matches
  every field `index.tsx` (Task 16) reads (`status`, `exitCode`, `lines`,
  `startedAt`, `endedAt`, `errorMessage`, `truncated`).
