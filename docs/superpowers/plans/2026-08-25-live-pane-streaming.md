# Live Pane Streaming + Agents Console Grid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream each running agent's live terminal screen to the dashboard, and add an Agents-tab grid that shows several at once.

**Architecture:** One `PaneBroadcaster` poll loop per *watched* session calls `provider.peek(..., ansi=True)` on a cadence, pushes a frame only when the screen changed, and fans out to every subscriber — cost is `O(watched sessions)`, never `O(sessions x viewers)`. A new SSE endpoint `GET /api/sessions/{id}/pane` serves those frames. The React side holds one current screen (not a buffer) and renders it through the existing ANSI-SGR converter.

**Tech Stack:** Python 3.12 + asyncio + FastAPI (SSE via `StreamingResponse`), pytest / pytest-asyncio (auto mode) + httpx `ASGITransport`, React + TypeScript + TanStack Query + Tailwind, vitest + @testing-library/react.

**Spec:** `docs/superpowers/specs/2026-08-25-live-pane-streaming-design.md`

## Global Constraints

- Async-first: never `subprocess.run()` in production code; every tmux call goes through `TmuxProvider._tmux` (`src/sessions/tmux.py:106`).
- ruff, line-length 100, target py312.
- Callers branch on `Cap`, never on `provider.name`. Unsupported operations raise `CapabilityUnsupported` rather than returning a plausible lie.
- Unknown is not dead: a failed probe never means "reap it".
- No new frontend dependency. ANSI rendering reuses `dashboard/src/panes/console-stream/ansi.tsx`.
- Config defaults, exact values from the spec: `pane_stream_interval_seconds: 1.0`, `pane_stream_max_sessions: 12`, `pane_stream_lines: 60`.
- The transcript endpoint `GET /api/sessions/{id}/stream` (`src/api/sessions.py`) must not change behaviour. Existing tests in `tests/test_session_stream_api.py` must keep passing untouched.
- Run the suite with `pytest tests/ -n auto`; single files may run sequentially.

---

### Task 1: `peek(..., ansi=)` on the provider contract

Adds an opt-in ANSI-colour flag to `peek`. `ansi=False` is the default so every existing caller is byte-identical to today. Dialog scraping uses the provider-private `_capture` (`src/sessions/tmux.py:256`) and is untouched.

**Files:**
- Modify: `src/sessions/provider.py:305-307` (the abstract `peek`)
- Modify: `src/sessions/tmux.py:445-453`
- Modify: `src/sessions/subprocess.py:204-212`
- Modify: `src/sessions/fake.py:195-199`
- Test: `tests/test_session_provider_conformance.py`, `tests/test_tmux_integration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SessionProvider.peek(self, h: SessionHandle, lines: int = 60, *, ansi: bool = False) -> str` on all three providers.

- [ ] **Step 1: Write the failing conformance test**

Append to `tests/test_session_provider_conformance.py`, inside the same class as `test_peek_returns_a_string_when_supported` (around `:231`), matching that test's existing `provider` / `case` / `tmp_path` fixture usage:

```python
    async def test_peek_accepts_ansi_keyword(self, provider, case, tmp_path):
        """Every provider accepts ansi= and still returns a string."""
        if not provider.supports(Cap.PEEK):
            pytest.skip(f"{provider.name} has no PEEK")
        handle = await case.start(provider, tmp_path)
        assert isinstance(await provider.peek(handle, 10, ansi=True), str)
        assert isinstance(await provider.peek(handle, 10, ansi=False), str)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_session_provider_conformance.py -k ansi_keyword -v`
Expected: FAIL — `TypeError: peek() got an unexpected keyword argument 'ansi'`

- [ ] **Step 3: Add the keyword to the ABC**

In `src/sessions/provider.py`, replace the abstract `peek`:

```python
    @abstractmethod
    async def peek(self, h: SessionHandle, lines: int = 60, *, ansi: bool = False) -> str:
        """Last *lines* of visible output (``""`` when unsupported).

        ``ansi=True`` asks for the screen *with* SGR colour sequences
        retained.  It is opt-in because every existing caller renders the
        text as plain output; only the pane stream wants colour.  A
        provider whose output has no colour to preserve ignores the flag.
        """
```

- [ ] **Step 4: Implement in the three providers**

`src/sessions/tmux.py` — `-e` keeps escape sequences in the capture:

```python
    async def peek(self, h: SessionHandle, lines: int = 60, *, ansi: bool = False) -> str:
        if not await self._fenced(h):
            return ""
        args = ["capture-pane", "-p"]
        if ansi:
            args.append("-e")
        args.extend(["-t", f"={h.name}:", "-S", f"-{max(lines, 1)}"])
        try:
            return await self._tmux(*args)
        except TmuxCommandError:
            return ""
```

`src/sessions/subprocess.py` — the log file is already raw bytes, so the flag changes nothing:

```python
    async def peek(self, h: SessionHandle, lines: int = 60, *, ansi: bool = False) -> str:
        running = self._get(h)
        if running is None:
            return ""
        try:
            text = Path(running.log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])
```

`src/sessions/fake.py`:

```python
    async def peek(self, h: SessionHandle, lines: int = 60, *, ansi: bool = False) -> str:
        s = self._get(h)
        if s is None:
            return ""
        return "\n".join(s.output[-lines:])
```

- [ ] **Step 5: Write the tmux argv test**

Append to `tests/test_tmux_integration.py`. It asserts on the argv the provider builds, so no tmux server is needed:

```python
@pytest.mark.asyncio
async def test_peek_ansi_flag_adds_dash_e(monkeypatch):
    """ansi=True puts -e on capture-pane; ansi=False leaves it off."""
    from src.sessions.provider import SessionHandle
    from src.sessions.tmux import TmuxProvider

    provider = TmuxProvider(config=None)
    calls: list[tuple[str, ...]] = []

    async def fake_tmux(*args, **kwargs):
        calls.append(args)
        return "screen"

    async def fenced(_h):
        return True

    monkeypatch.setattr(provider, "_tmux", fake_tmux)
    monkeypatch.setattr(provider, "_fenced", fenced)
    handle = SessionHandle(name="s1", provider="tmux", instance_token="tok")

    await provider.peek(handle, 10, ansi=True)
    await provider.peek(handle, 10, ansi=False)

    assert "-e" in calls[0]
    assert "-e" not in calls[1]
    assert calls[0][0] == "capture-pane"
```

- [ ] **Step 6: Run both tests**

Run: `pytest tests/test_session_provider_conformance.py tests/test_tmux_integration.py -v`
Expected: PASS

- [ ] **Step 7: Verify no existing caller regressed**

Run: `pytest tests/test_session_commands.py tests/test_session_reconciler.py tests/test_session_stream_api.py tests/test_session_runtime_units.py -q`
Expected: PASS (all pre-existing)

- [ ] **Step 8: Commit**

```bash
git add src/sessions/provider.py src/sessions/tmux.py src/sessions/subprocess.py \
  src/sessions/fake.py tests/test_session_provider_conformance.py tests/test_tmux_integration.py
git commit -m "feat(sessions): opt-in ansi= on provider peek"
```

---

### Task 2: Pane-stream config

Three validated fields. Done before the broadcaster because the broadcaster reads them.

**Files:**
- Modify: `src/config.py:867-943` (`SessionsConfig`)
- Test: `tests/test_config_validation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `config.sessions.pane_stream_interval_seconds: float`, `config.sessions.pane_stream_max_sessions: int`, `config.sessions.pane_stream_lines: int`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_validation.py`:

```python
def test_pane_stream_defaults():
    from src.config import SessionsConfig

    cfg = SessionsConfig()
    assert cfg.pane_stream_interval_seconds == 1.0
    assert cfg.pane_stream_max_sessions == 12
    assert cfg.pane_stream_lines == 60
    assert cfg.validate() == []


def test_pane_stream_rejects_non_positive():
    from src.config import SessionsConfig

    cfg = SessionsConfig(pane_stream_interval_seconds=0)
    fields = {e.field for e in cfg.validate()}
    assert "pane_stream_interval_seconds" in fields

    cfg = SessionsConfig(pane_stream_max_sessions=0)
    fields = {e.field for e in cfg.validate()}
    assert "pane_stream_max_sessions" in fields
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_config_validation.py -k pane_stream -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'pane_stream_interval_seconds'`

- [ ] **Step 3: Add the fields**

In `src/config.py`, after `adopt_on_start: bool = True` in `SessionsConfig`:

```python
    #: Live pane stream (dashboard).  Polling happens only while a
    #: subscriber is attached, so an unwatched daemon pays nothing.
    pane_stream_interval_seconds: float = 1.0
    pane_stream_max_sessions: int = 12
    pane_stream_lines: int = 60
```

- [ ] **Step 4: Add validation**

In `SessionsConfig.validate()`, after the existing `>= 0` loop, add a strictly-positive loop — a zero cadence would busy-spin and a zero cap would silently disable the feature:

```python
        for name in (
            "pane_stream_interval_seconds",
            "pane_stream_max_sessions",
            "pane_stream_lines",
        ):
            if getattr(self, name) <= 0:
                errors.append(ConfigError("sessions", name, "must be > 0"))
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_config_validation.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/config.py tests/test_config_validation.py
git commit -m "feat(config): pane stream cadence, cap and line count"
```

---

### Task 3: `PaneBroadcaster`

The load-bearing unit. One poll loop per watched session, fanned out to all subscribers.

**Files:**
- Create: `src/sessions/pane_broadcaster.py`
- Test: `tests/test_pane_broadcaster.py`

**Interfaces:**
- Consumes: `provider.peek(h, lines, ansi=True)` and `provider.is_running(h)` from Task 1; `config.sessions.pane_stream_*` from Task 2.
- Produces:
  - `PaneFrame` — a `dict` with keys `source` (always `"pane"`), `type` (`"screen"` | `"stopped"` | `"error"`), `seq` (int), `ts` (float), plus `screen` (str) on `"screen"` and `message` (str) on `"error"`.
  - `class PaneBroadcaster(providers, config)` with `async subscribe(session) -> asyncio.Queue`, `async unsubscribe(session_name, queue) -> None`, `async shutdown() -> None`, and `watched_count() -> int`.
  - `PaneStreamRefused(Exception)` with attribute `.message`.
  - `session` is any object with `.name`, `.provider`, `.instance_token` attributes (a `SessionRecord`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pane_broadcaster.py`:

```python
"""PaneBroadcaster — fan-out, dedupe, linger, cap, error handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from src.sessions.fake import FakeProvider
from src.sessions.pane_broadcaster import PaneBroadcaster, PaneStreamRefused
from src.sessions.provider import SessionSpec


@dataclass
class Row:
    """Minimal stand-in for a SessionRecord."""

    name: str
    provider: str = "fake"
    instance_token: str = "tok"


class OneProvider:
    """Registry stub: always hands back the same provider instance."""

    def __init__(self, provider):
        self.provider = provider

    def create(self, name, config=None):
        return self.provider


@dataclass
class Sessions:
    pane_stream_interval_seconds: float = 0.01
    pane_stream_max_sessions: int = 2
    pane_stream_lines: int = 60


@dataclass
class Config:
    sessions: Sessions


async def _fake_with(name: str) -> FakeProvider:
    provider = FakeProvider(config=None)
    await provider.start(
        SessionSpec(session_name=name, work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    return provider


def _bcast(provider, **overrides) -> PaneBroadcaster:
    return PaneBroadcaster(
        OneProvider(provider), Config(sessions=Sessions(**overrides))
    )


async def _next(queue: asyncio.Queue, timeout: float = 2.0) -> dict:
    return await asyncio.wait_for(queue.get(), timeout=timeout)


@pytest.mark.asyncio
async def test_first_frame_is_immediate():
    provider = await _fake_with("s1")
    provider.sessions["s1"].output.append("hello")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    frame = await _next(q)
    assert frame["source"] == "pane"
    assert frame["type"] == "screen"
    assert "hello" in frame["screen"]
    await b.shutdown()


@pytest.mark.asyncio
async def test_two_subscribers_share_one_loop():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    q1 = await b.subscribe(Row("s1"))
    q2 = await b.subscribe(Row("s1"))
    assert b.watched_count() == 1
    await _next(q1)
    await _next(q2)
    provider.sessions["s1"].output.append("later")
    f1 = await _next(q1)
    f2 = await _next(q2)
    assert "later" in f1["screen"]
    assert "later" in f2["screen"]
    await b.shutdown()


@pytest.mark.asyncio
async def test_unchanged_screen_emits_no_frame():
    provider = await _fake_with("s1")
    provider.sessions["s1"].output.append("static")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    await _next(q)  # the immediate first frame
    with pytest.raises(asyncio.TimeoutError):
        await _next(q, timeout=0.2)
    await b.shutdown()


@pytest.mark.asyncio
async def test_seq_increases_per_frame():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    first = await _next(q)
    provider.sessions["s1"].output.append("change")
    second = await _next(q)
    assert second["seq"] > first["seq"]
    await b.shutdown()


@pytest.mark.asyncio
async def test_last_unsubscribe_stops_loop_after_linger():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    b.linger_seconds = 0.05
    q = await b.subscribe(Row("s1"))
    await _next(q)
    await b.unsubscribe("s1", q)
    await asyncio.sleep(0.2)
    assert b.watched_count() == 0
    await b.shutdown()


@pytest.mark.asyncio
async def test_resubscribe_within_linger_reuses_loop():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    b.linger_seconds = 1.0
    q1 = await b.subscribe(Row("s1"))
    await _next(q1)
    await b.unsubscribe("s1", q1)
    q2 = await b.subscribe(Row("s1"))
    assert b.watched_count() == 1
    await _next(q2)
    await b.shutdown()


@pytest.mark.asyncio
async def test_cap_refuses_extra_session():
    provider = await _fake_with("s1")
    await provider.start(
        SessionSpec(session_name="s2", work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    await provider.start(
        SessionSpec(session_name="s3", work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    b = _bcast(provider, pane_stream_max_sessions=2)
    await b.subscribe(Row("s1"))
    await b.subscribe(Row("s2"))
    with pytest.raises(PaneStreamRefused):
        await b.subscribe(Row("s3"))
    await b.shutdown()


@pytest.mark.asyncio
async def test_stopped_session_emits_stopped_frame_and_ends():
    provider = await _fake_with("s1")
    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    await _next(q)
    await provider.stop(provider.sessions["s1"].handle)
    frame = await _next(q)
    while frame["type"] == "screen":
        frame = await _next(q)
    assert frame["type"] == "stopped"
    await asyncio.sleep(0.1)
    assert b.watched_count() == 0
    await b.shutdown()


@pytest.mark.asyncio
async def test_repeated_peek_errors_emit_one_error_frame_and_stop():
    provider = await _fake_with("s1")

    async def boom(*_a, **_k):
        raise RuntimeError("tmux is gone")

    b = _bcast(provider)
    q = await b.subscribe(Row("s1"))
    await _next(q)
    provider.peek = boom
    frame = await _next(q)
    assert frame["type"] == "error"
    assert "tmux is gone" in frame["message"]
    await asyncio.sleep(0.1)
    assert b.watched_count() == 0
    await b.shutdown()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_pane_broadcaster.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.sessions.pane_broadcaster'`

- [ ] **Step 3: Implement the broadcaster**

Create `src/sessions/pane_broadcaster.py`:

```python
"""PaneBroadcaster — one pane poll loop per *watched* session, fanned out.

The dashboard's live terminal view reads ``capture-pane`` snapshots.  A
naive implementation polls once per viewer, which is exactly what
:class:`~src.sessions.state_cache.TmuxStateCache` exists to prevent: every
``TmuxProvider._tmux`` call is a fresh ``create_subprocess_exec``, so
per-viewer polling turns N viewers into N forks per tick.

So the loop is owned per *session*, and subscribers attach to it.  Cost is
``O(watched sessions)``, never ``O(sessions x viewers)``.  Nothing polls
while nobody is watching.

See ``docs/superpowers/specs/2026-08-25-live-pane-streaming-design.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from src.sessions.provider import Cap, SessionHandle

logger = logging.getLogger(__name__)

__all__ = ["PaneBroadcaster", "PaneStreamRefused"]

#: Consecutive provider failures before the loop gives up.  One failure is
#: a hiccup (a tmux command can lose a race with a dying pane); three in a
#: row means the session is not observable any more.
_MAX_CONSECUTIVE_ERRORS = 3

#: Seconds a loop keeps running after its last subscriber leaves.  A page
#: refresh and React StrictMode's double-mount both drop and re-add a
#: subscriber within milliseconds; without the linger they would tear the
#: loop down and rebuild it every time.
_DEFAULT_LINGER_SECONDS = 5.0


class PaneStreamRefused(Exception):
    """Subscription refused — over the watched-session cap.

    Explicit rather than a silently empty stream: a viewer must be able to
    tell "nothing is happening" from "we declined to watch this".
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class _Watch:
    """One session's poll loop plus its subscribers."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.last_screen: str | None = None
        self.seq: int = 0
        self.stop_at: float | None = None


class PaneBroadcaster:
    """Fan-out of pane screens, one poll loop per watched session."""

    def __init__(self, providers, config):
        self._providers = providers
        self._config = config
        self._watches: dict[str, _Watch] = {}
        self._lock = asyncio.Lock()
        sessions_cfg = getattr(config, "sessions", None)
        self.interval: float = float(
            getattr(sessions_cfg, "pane_stream_interval_seconds", 1.0) or 1.0
        )
        self.max_sessions: int = int(
            getattr(sessions_cfg, "pane_stream_max_sessions", 12) or 12
        )
        self.lines: int = int(getattr(sessions_cfg, "pane_stream_lines", 60) or 60)
        self.linger_seconds: float = _DEFAULT_LINGER_SECONDS

    # -- public surface ----------------------------------------------------

    def watched_count(self) -> int:
        """Sessions with a live poll loop (including lingering ones)."""
        return len(self._watches)

    async def subscribe(self, session) -> asyncio.Queue:
        """Attach a subscriber, starting the session's loop if needed.

        Raises :class:`PaneStreamRefused` when the cap is reached, and
        ``CapabilityUnsupported`` when the provider cannot peek at all.
        """
        provider = self._providers.create(session.provider, self._config)
        if not provider.supports(Cap.PEEK):
            from src.sessions.provider import CapabilityUnsupported

            raise CapabilityUnsupported(provider.name, Cap.PEEK)

        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        async with self._lock:
            watch = self._watches.get(session.name)
            if watch is None:
                if len(self._watches) >= self.max_sessions:
                    raise PaneStreamRefused(
                        f"pane stream cap reached ({self.max_sessions} sessions "
                        "already watched)"
                    )
                watch = _Watch()
                self._watches[session.name] = watch
            watch.stop_at = None  # cancels any pending linger
            watch.subscribers.add(queue)
            if watch.last_screen is not None:
                self._emit(watch, {"type": "screen", "screen": watch.last_screen},
                            only=queue)
            if watch.task is None or watch.task.done():
                watch.task = asyncio.create_task(
                    self._run(session, watch), name=f"pane-{session.name}"
                )
        return queue

    async def unsubscribe(self, session_name: str, queue: asyncio.Queue) -> None:
        """Detach a subscriber; the loop lingers before stopping."""
        async with self._lock:
            watch = self._watches.get(session_name)
            if watch is None:
                return
            watch.subscribers.discard(queue)
            if not watch.subscribers:
                watch.stop_at = time.monotonic() + self.linger_seconds

    async def shutdown(self) -> None:
        """Cancel every loop.  Called from daemon/app teardown."""
        async with self._lock:
            watches = list(self._watches.values())
            self._watches.clear()
        for watch in watches:
            if watch.task is not None:
                watch.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watch.task

    # -- internals ---------------------------------------------------------

    def _emit(self, watch: _Watch, payload: dict, *, only: asyncio.Queue | None = None) -> None:
        """Push one frame to every subscriber (or just *only*).

        A subscriber whose queue is full is a slow reader, not a reason to
        block the loop: the frame is dropped for that subscriber alone.
        """
        watch.seq += 1
        frame = {"source": "pane", "seq": watch.seq, "ts": time.time(), **payload}
        targets = [only] if only is not None else list(watch.subscribers)
        for queue in targets:
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                logger.debug("pane subscriber queue full; dropping frame")

    async def _run(self, session, watch: _Watch) -> None:
        handle = SessionHandle(
            name=session.name,
            provider=session.provider,
            instance_token=getattr(session, "instance_token", "") or "",
        )
        provider = self._providers.create(session.provider, self._config)
        errors = 0
        try:
            while True:
                if watch.stop_at is not None and time.monotonic() >= watch.stop_at:
                    break
                try:
                    screen = await provider.peek(handle, self.lines, ansi=True)
                    errors = 0
                except Exception as exc:  # noqa: BLE001 - reported as a frame
                    errors += 1
                    if errors >= _MAX_CONSECUTIVE_ERRORS:
                        self._emit(watch, {"type": "error", "message": str(exc)})
                        break
                    await asyncio.sleep(self.interval)
                    continue

                if screen != watch.last_screen:
                    watch.last_screen = screen
                    self._emit(watch, {"type": "screen", "screen": screen})

                # Liveness is checked *after* emitting, so the final screen
                # of a session that just exited still reaches the viewer.
                try:
                    alive = await provider.is_running(handle)
                except Exception:
                    alive = True  # unknown is not dead
                if not alive:
                    self._emit(watch, {"type": "stopped"})
                    break

                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        finally:
            async with self._lock:
                current = self._watches.get(session.name)
                if current is watch:
                    self._watches.pop(session.name, None)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_pane_broadcaster.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Lint**

Run: `ruff check src/sessions/pane_broadcaster.py tests/test_pane_broadcaster.py`
Expected: no findings

- [ ] **Step 6: Commit**

```bash
git add src/sessions/pane_broadcaster.py tests/test_pane_broadcaster.py
git commit -m "feat(sessions): PaneBroadcaster — per-session pane poll with fan-out"
```

---

### Task 4: `GET /api/sessions/{id}/pane` SSE endpoint

**Files:**
- Create: `src/api/pane_stream.py`
- Modify: `src/api/app.py:113` (router registration), `src/api/app.py:145-147` (shutdown hook)
- Test: `tests/test_pane_stream_api.py`

**Interfaces:**
- Consumes: `PaneBroadcaster`, `PaneStreamRefused` from Task 3.
- Produces:
  - `build_pane_router(*, db, broadcaster) -> APIRouter` serving `GET /api/sessions/{session_id}/pane`.
  - `router` — the default-wired `APIRouter` imported by `src/api/app.py`.
  - `get_broadcaster(orch) -> PaneBroadcaster` and `async shutdown_broadcaster() -> None` (module-level singleton).
  - Wire frames are the `PaneFrame` dicts from Task 3, one per `data:` line.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pane_stream_api.py`:

```python
"""SSE pane stream endpoint (live capture-pane screens)."""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.database import Database
from src.models import Project, SessionRecord, Task
from src.sessions.fake import FakeProvider
from src.sessions.pane_broadcaster import PaneBroadcaster
from src.sessions.provider import SessionSpec


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


async def _make_session(db, *, session_id, name, provider="fake"):
    await db.create_task(
        Task(id=f"t-{session_id}", project_id="p1", title="T", description="d")
    )
    row = SessionRecord(
        id=session_id,
        project_id="p1",
        profile_id="claude-agent",
        harness="claude",
        provider=provider,
        name=name,
        lifecycle="task",
        work_dir="/w",
        epoch="e",
        instance_token="tok",
        started_at=time.time(),
        task_id=f"t-{session_id}",
        state="running",
        session_key="sk",
    )
    await db.create_session(row)
    return row


class OneProvider:
    def __init__(self, provider):
        self.provider = provider

    def create(self, name, config=None):
        return self.provider


class Sessions:
    pane_stream_interval_seconds = 0.02
    pane_stream_max_sessions = 12
    pane_stream_lines = 60


class Config:
    sessions = Sessions()


async def _app(db, provider) -> tuple[FastAPI, PaneBroadcaster]:
    from src.api.pane_stream import build_pane_router

    broadcaster = PaneBroadcaster(OneProvider(provider), Config())
    app = FastAPI()
    app.include_router(build_pane_router(db=db, broadcaster=broadcaster))
    return app, broadcaster


def _frames(text: str) -> list[dict]:
    return [
        json.loads(ln[len("data:"):].strip())
        for ln in text.splitlines()
        if ln.startswith("data:")
    ]


@pytest.mark.asyncio
async def test_pane_stream_emits_screen_frame(db):
    provider = FakeProvider(config=None)
    await provider.start(
        SessionSpec(session_name="s-1", work_dir="/w", command=("x",),
                     instance_token="tok")
    )
    provider.sessions["s-1"].output.append("PANE CONTENT")
    await _make_session(db, session_id="sid1", name="s-1")

    app, broadcaster = await _app(db, provider)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                            timeout=5.0) as client:
        async with client.stream(
            "GET", "/api/sessions/sid1/pane", params={"max_seconds": "0.3"}
        ) as resp:
            assert resp.status_code == 200
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
    frames = _frames(body.decode())
    assert frames
    assert frames[0]["source"] == "pane"
    assert frames[0]["type"] == "screen"
    assert "PANE CONTENT" in frames[0]["screen"]
    await broadcaster.shutdown()


@pytest.mark.asyncio
async def test_pane_stream_unknown_session_404(db):
    provider = FakeProvider(config=None)
    app, broadcaster = await _app(db, provider)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions/nope/pane")
    assert resp.status_code == 404
    await broadcaster.shutdown()


@pytest.mark.asyncio
async def test_pane_stream_without_peek_capability_409(db):
    from src.sessions.subprocess import SubprocessProvider
    from src.sessions.provider import Cap

    provider = SubprocessProvider(config=None)
    # Force the no-PEEK shape without depending on the class's caps.
    provider.capabilities = frozenset(c for c in provider.capabilities if c != Cap.PEEK)
    await _make_session(db, session_id="sid2", name="s-2", provider="subprocess")

    app, broadcaster = await _app(db, provider)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions/sid2/pane")
    assert resp.status_code == 409
    assert "peek" in resp.json()["detail"].lower()
    await broadcaster.shutdown()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_pane_stream_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.api.pane_stream'`

- [ ] **Step 3: Implement the endpoint**

Create `src/api/pane_stream.py`:

```python
"""SSE pane stream — ``GET /api/sessions/{session_id}/pane``.

Live ``capture-pane`` screens for one session, served from the shared
:class:`~src.sessions.pane_broadcaster.PaneBroadcaster` so N viewers of one
session still cost one poll loop.

Deliberately separate from ``/api/sessions/{id}/stream`` (transcript): the
lifecycles differ — a broadcaster-backed fan-out versus a per-connection
file tail — and keeping the transcript endpoint untouched means it cannot
regress behind this feature.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.sessions.pane_broadcaster import PaneBroadcaster, PaneStreamRefused
from src.sessions.provider import CapabilityUnsupported

logger = logging.getLogger(__name__)

__all__ = ["build_pane_router", "router", "get_broadcaster", "shutdown_broadcaster"]

_HEARTBEAT_SECONDS = 15.0


def _sse(frame: dict) -> bytes:
    return f"data: {json.dumps(frame)}\n\n".encode()


def build_pane_router(*, db, broadcaster: PaneBroadcaster) -> APIRouter:
    """Router factory so tests wire a lightweight db + FakeProvider."""

    router = APIRouter()

    @router.get("/api/sessions/{session_id}/pane")
    async def pane(session_id: str, request: Request,
                    max_seconds: float | None = None) -> StreamingResponse:
        session = await db.get_session(session_id)
        if session is None:
            session = await db.get_session_by_name(session_id)
        if session is None:
            raise HTTPException(status_code=404,
                                 detail=f"No session '{session_id}'")
        try:
            queue = await broadcaster.subscribe(session)
        except CapabilityUnsupported as exc:
            raise HTTPException(
                status_code=409,
                detail=f"provider '{exc.provider}' cannot peek; no pane stream",
            ) from exc
        except PaneStreamRefused as exc:
            raise HTTPException(status_code=429, detail=exc.message) from exc

        started_at = time.monotonic()

        async def gen():
            last_beat = time.monotonic()
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    if max_seconds is not None and \
                            time.monotonic() - started_at > max_seconds:
                        return
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except (TimeoutError, asyncio.TimeoutError):
                        frame = None
                    if frame is not None:
                        yield _sse(frame)
                        last_beat = time.monotonic()
                        if frame.get("type") in ("stopped", "error"):
                            return
                        continue
                    now = time.monotonic()
                    if now - last_beat >= _HEARTBEAT_SECONDS:
                        yield b": heartbeat\n\n"
                        last_beat = now
            finally:
                with contextlib.suppress(Exception):
                    await broadcaster.unsubscribe(session.name, queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


# -- default wiring --------------------------------------------------------

_broadcaster: PaneBroadcaster | None = None


def get_broadcaster(orch) -> PaneBroadcaster:
    """Process-wide broadcaster, built on first use from the orchestrator."""
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = PaneBroadcaster(
            orch.session_providers, getattr(orch, "config", None)
        )
    return _broadcaster


async def shutdown_broadcaster() -> None:
    """Cancel every poll loop.  Registered on FastAPI shutdown."""
    global _broadcaster
    if _broadcaster is not None:
        await _broadcaster.shutdown()
        _broadcaster = None


def _build_default_router() -> APIRouter:
    from src.api import dependencies as deps

    router = APIRouter()

    @router.get("/api/sessions/{session_id}/pane")
    async def pane(session_id: str, request: Request,
                    max_seconds: float | None = None) -> StreamingResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        inner = build_pane_router(db=orch.db, broadcaster=get_broadcaster(orch))
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/sessions/{session_id}/pane":
                return await route.endpoint(
                    session_id=session_id, request=request,
                    max_seconds=max_seconds,
                )
        raise HTTPException(status_code=500, detail="pane router misconfigured")

    return router


router = _build_default_router()
```

- [ ] **Step 4: Register the router and the shutdown hook**

In `src/api/app.py`, next to the existing sessions import (`:25`):

```python
from src.api.pane_stream import router as pane_router
```

After `app.include_router(sessions_router)` (`:113`):

```python
    # Live pane SSE: GET /api/sessions/{id}/pane — capture-pane screens
    # from the shared PaneBroadcaster (one poll loop per watched session).
    app.include_router(pane_router)
```

Extend the existing shutdown handler (`:145-147`):

```python
    @app.on_event("shutdown")
    async def _shutdown_ws():
        ws_manager.shutdown()
        from src.api.pane_stream import shutdown_broadcaster

        await shutdown_broadcaster()
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_pane_stream_api.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Verify the transcript endpoint and app wiring still pass**

Run: `pytest tests/test_session_stream_api.py tests/test_api_auth.py tests/test_api_scope.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/api/pane_stream.py src/api/app.py tests/test_pane_stream_api.py
git commit -m "feat(api): live pane SSE endpoint"
```

---

### Task 5: `usePaneStream` + `LivePaneConsole`, wired into the existing views

Makes `session-peek` and `SessionDetail`'s pane toggle live instead of showing a single stale snapshot.

**Files:**
- Create: `dashboard/src/ws/usePaneStream.ts`
- Create: `dashboard/src/components/LivePaneConsole.tsx`
- Create: `dashboard/src/ws/__tests__/usePaneStream.test.tsx`
- Create: `dashboard/src/components/__tests__/LivePaneConsole.test.tsx`
- Modify: `dashboard/src/panes/session-peek/index.tsx`
- Modify: `dashboard/src/pages/SessionDetail.tsx:26-30` (the `viewMode === "pane"` branch)
- Modify: `dashboard/src/panes/session-peek/__tests__/index.test.tsx` (mock the new hook)

**Interfaces:**
- Consumes: `GET /api/sessions/{id}/pane` from Task 4.
- Produces:
  - `export type PaneStatus = "connecting" | "open" | "stopped" | "error" | "closed"`
  - `export interface PaneState { screen: string | null; status: PaneStatus; error: string | null; seq: number }`
  - `export function usePaneStream(sessionId: string | null | undefined, opts?: { enabled?: boolean }): PaneState`
  - `export default function LivePaneConsole(props: { screen: string | null; status: PaneStatus; error?: string | null; className?: string })`

- [ ] **Step 1: Write the failing hook test**

Create `dashboard/src/ws/__tests__/usePaneStream.test.tsx`:

```tsx
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { usePaneStream } from "../usePaneStream";

class MockEventSource {
  static last: MockEventSource | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    MockEventSource.last = this;
  }
  close() {
    this.closed = true;
  }
}

beforeEach(() => {
  MockEventSource.last = null;
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function send(frame: Record<string, unknown>) {
  act(() => {
    MockEventSource.last?.onmessage?.({ data: JSON.stringify(frame) });
  });
}

describe("usePaneStream", () => {
  it("replaces the screen rather than appending", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "first", seq: 1, ts: 1 });
    expect(result.current.screen).toBe("first");
    send({ source: "pane", type: "screen", screen: "second", seq: 2, ts: 2 });
    expect(result.current.screen).toBe("second");
  });

  it("surfaces a stopped frame as status", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "screen", screen: "last", seq: 1, ts: 1 });
    send({ source: "pane", type: "stopped", seq: 2, ts: 2 });
    expect(result.current.status).toBe("stopped");
    expect(result.current.screen).toBe("last");
  });

  it("surfaces an error frame with its message", () => {
    const { result } = renderHook(() => usePaneStream("s1"));
    send({ source: "pane", type: "error", message: "tmux is gone", seq: 1, ts: 1 });
    expect(result.current.status).toBe("error");
    expect(result.current.error).toBe("tmux is gone");
  });

  it("opens no connection when disabled", () => {
    renderHook(() => usePaneStream("s1", { enabled: false }));
    expect(MockEventSource.last).toBeNull();
  });

  it("closes the connection on unmount", () => {
    const { unmount } = renderHook(() => usePaneStream("s1"));
    const es = MockEventSource.last;
    unmount();
    expect(es?.closed).toBe(true);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd dashboard && npx vitest run src/ws/__tests__/usePaneStream.test.tsx`
Expected: FAIL — cannot resolve `../usePaneStream`

- [ ] **Step 3: Implement the hook**

Create `dashboard/src/ws/usePaneStream.ts`:

```ts
/**
 * SSE hook wrapping `GET /api/sessions/{session_id}/pane`.
 *
 * Unlike useTranscriptStream, this holds ONE current screen, not a buffer:
 * each frame is a full `capture-pane` snapshot that supersedes the last, so
 * accumulating them would only grow memory to redraw the same terminal.
 *
 * Frame shapes (src/api/pane_stream.py):
 *   {source:"pane", type:"screen",  screen, seq, ts}
 *   {source:"pane", type:"stopped", seq, ts}
 *   {source:"pane", type:"error",   message, seq, ts}
 */
import { useEffect, useRef, useState } from "react";

export type PaneStatus = "connecting" | "open" | "stopped" | "error" | "closed";

export interface PaneState {
  screen: string | null;
  status: PaneStatus;
  error: string | null;
  seq: number;
}

interface Options {
  enabled?: boolean;
}

const INITIAL: PaneState = { screen: null, status: "closed", error: null, seq: 0 };

export function usePaneStream(
  sessionId: string | null | undefined,
  opts: Options = {},
): PaneState {
  const { enabled = true } = opts;
  const [state, setState] = useState<PaneState>(INITIAL);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!enabled || !sessionId) return;

    const base =
      import.meta.env.VITE_API_URL ||
      `${window.location.protocol}//${window.location.host}`;
    const url = `${base}/api/sessions/${encodeURIComponent(sessionId)}/pane`;

    setState({ ...INITIAL, status: "connecting" });
    const es = new EventSource(url);
    esRef.current = es;

    es.onopen = () => setState((p) => ({ ...p, status: "open" }));

    es.onmessage = (msg) => {
      try {
        const f = JSON.parse(msg.data) as {
          type?: string;
          screen?: string;
          message?: string;
          seq?: number;
        };
        setState((prev) => {
          const seq = f.seq ?? prev.seq;
          if (f.type === "stopped") return { ...prev, status: "stopped", seq };
          if (f.type === "error")
            return {
              ...prev,
              status: "error",
              error: f.message ?? "pane stream error",
              seq,
            };
          return {
            screen: f.screen ?? prev.screen,
            status: "open",
            error: null,
            seq,
          };
        });
      } catch {
        // Malformed frame; heartbeats are comments and never land here.
      }
    };

    es.onerror = () =>
      setState((p) => ({
        ...p,
        status: "error",
        error: "stream error (EventSource will retry)",
      }));

    return () => {
      es.close();
      esRef.current = null;
      setState((p) => ({ ...p, status: "closed" }));
    };
  }, [sessionId, enabled]);

  return state;
}
```

- [ ] **Step 4: Run the hook test**

Run: `cd dashboard && npx vitest run src/ws/__tests__/usePaneStream.test.tsx`
Expected: PASS (5 tests)

- [ ] **Step 5: Write the failing console test**

Create `dashboard/src/components/__tests__/LivePaneConsole.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import LivePaneConsole from "../LivePaneConsole";

describe("LivePaneConsole", () => {
  it("renders the screen text", () => {
    render(<LivePaneConsole screen={"line one\nline two"} status="open" />);
    expect(screen.getByText(/line one/)).toBeInTheDocument();
  });

  it("shows a waiting message before the first frame", () => {
    render(<LivePaneConsole screen={null} status="connecting" />);
    expect(screen.getByText(/waiting for pane/i)).toBeInTheDocument();
  });

  it("shows the error message on an error status", () => {
    render(
      <LivePaneConsole screen={null} status="error" error="tmux is gone" />,
    );
    expect(screen.getByText(/tmux is gone/)).toBeInTheDocument();
  });

  it("labels a stopped session while keeping the last screen", () => {
    render(<LivePaneConsole screen="final screen" status="stopped" />);
    expect(screen.getByText(/final screen/)).toBeInTheDocument();
    expect(screen.getByText(/session ended/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 6: Run it to verify it fails**

Run: `cd dashboard && npx vitest run src/components/__tests__/LivePaneConsole.test.tsx`
Expected: FAIL — cannot resolve `../LivePaneConsole`

- [ ] **Step 7: Implement the console**

Create `dashboard/src/components/LivePaneConsole.tsx`:

```tsx
/**
 * LivePaneConsole — renders one live `capture-pane` screen.
 *
 * A screen is a full snapshot, so this replaces rather than scrolls: no
 * follow-tail, no scrollback. Colour comes from tmux's `capture-pane -e`
 * (SGR only) rendered by the existing ansiToSpans converter — tmux has
 * already done the terminal emulation, so no emulator is needed here.
 */
import { ansiToSpans } from "../panes/console-stream/ansi";
import type { PaneStatus } from "../ws/usePaneStream";

interface LivePaneConsoleProps {
  screen: string | null;
  status: PaneStatus;
  error?: string | null;
  className?: string;
}

export default function LivePaneConsole({
  screen,
  status,
  error,
  className,
}: LivePaneConsoleProps) {
  return (
    <div
      className={
        "overflow-auto bg-black p-3 font-mono text-xs leading-tight text-green-200 " +
        (className ?? "")
      }
    >
      {status === "stopped" && (
        <p className="mb-1 text-amber-400">Session ended — last screen below.</p>
      )}
      {status === "error" && (
        <p className="mb-1 text-red-400">{error ?? "pane stream error"}</p>
      )}
      {screen === null ? (
        status === "error" ? null : (
          <p className="text-gray-500">Waiting for pane snapshot…</p>
        )
      ) : (
        <pre className="whitespace-pre">{ansiToSpans(screen)}</pre>
      )}
    </div>
  );
}
```

- [ ] **Step 8: Run the console test**

Run: `cd dashboard && npx vitest run src/components/__tests__/LivePaneConsole.test.tsx`
Expected: PASS (4 tests)

- [ ] **Step 9: Rewire `session-peek` to the live stream**

In `dashboard/src/panes/session-peek/index.tsx`:
- Replace the `useTranscriptStream` import with `import { usePaneStream } from "../../ws/usePaneStream";`
- Replace the `PeekFrameConsole` import with `import LivePaneConsole from "../../components/LivePaneConsole";`
- Replace `const { entries, status, error } = useTranscriptStream(sessionId, { enabled: true });` with `const { screen, status, error } = usePaneStream(sessionId, { enabled: true });`
- Replace the `<PeekFrameConsole frames={...} .../>` render with `<LivePaneConsole screen={screen} status={status} error={error} />`
- The `tail` arg and its toolbar button become meaningless (a screen has no tail to follow): delete the toolbar entry and drop `tail` from `SessionPeekArgs` in `manifest.ts`, and delete any test in `__tests__/index.test.tsx` asserting on the tail toggle.
- Update the mock at the top of `dashboard/src/panes/session-peek/__tests__/index.test.tsx` from `vi.mock("../../../ws/useTranscriptStream", ...)` to `vi.mock("../../../ws/usePaneStream", () => ({ usePaneStream: (...args: unknown[]) => mockUsePaneStream(...args) }))`, and have `mockUsePaneStream` return `{ screen: "hello", status: "open", error: null, seq: 1 }`.

- [ ] **Step 10: Rewire `SessionDetail`'s pane toggle**

In `dashboard/src/pages/SessionDetail.tsx`, keep `useTranscriptStream` for the transcript view and add `usePaneStream` for the pane view, enabled only in that mode so nothing polls while the transcript is showing:

```tsx
  const pane = usePaneStream(sessionId, { enabled: streamOn && viewMode === "pane" });
```

Then render `<LivePaneConsole screen={pane.screen} status={pane.status} error={pane.error} className="max-h-[60vh]" />` in place of `<PaneView entries={entries} />`. `dashboard/src/components/PaneView.tsx` now has no callers — delete it and its `PeekFrameConsole` usage stays only if another caller remains (check with `grep -rn PeekFrameConsole dashboard/src`; if nothing references it, delete that file too).

- [ ] **Step 11: Run the frontend suite and typecheck**

Run: `cd dashboard && npx vitest run && npx tsc --noEmit`
Expected: PASS, no type errors

- [ ] **Step 12: Commit**

```bash
git add dashboard/src/ws/usePaneStream.ts dashboard/src/components/LivePaneConsole.tsx \
  dashboard/src/ws/__tests__ dashboard/src/components/__tests__ \
  dashboard/src/panes/session-peek dashboard/src/pages/SessionDetail.tsx
git rm -f dashboard/src/components/PaneView.tsx
git commit -m "feat(dashboard): live pane stream in session-peek and SessionDetail"
```

---

### Task 6: Agents tab Table|Grid toggle

**Files:**
- Create: `dashboard/src/pages/command-center/AgentConsoleTile.tsx`
- Create: `dashboard/src/pages/command-center/__tests__/Agents.test.tsx`
- Modify: `dashboard/src/pages/command-center/Agents.tsx`

**Interfaces:**
- Consumes: `usePaneStream`, `LivePaneConsole` from Task 5; `useSessions` from `dashboard/src/api/hooks.ts:1028`.
- Produces: `export default function AgentConsoleTile(props: { sessionId: string; title: string; subtitle?: string; onOpen?: () => void })`.

- [ ] **Step 1: Write the failing tile + toggle test**

Create `dashboard/src/pages/command-center/__tests__/Agents.test.tsx`:

```tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import CommandCenterAgents from "../Agents";

const mockUsePaneStream = vi.fn();
const mockOpen = vi.fn();

vi.mock("../../../ws/usePaneStream", () => ({
  usePaneStream: (...args: unknown[]) => mockUsePaneStream(...args),
}));
vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "p1", name: "P1" }] }),
  useAllAgents: () => ({
    data: [{ name: "n-impl", project_id: "p1", current_task_id: "t1", state: "busy" }],
    isLoading: false,
  }),
  useSessions: () => ({
    data: [{ id: "sid1", name: "n-impl--t1", task_id: "t1", state: "running" }],
  }),
}));
vi.mock("../../../panes/store", () => ({
  useShellPaneStore: () => ({ open: mockOpen }),
}));
vi.mock("../../../shell/hotkeys/useListNav", () => ({
  useListNav: () => ({ current: null }),
}));

beforeEach(() => {
  mockOpen.mockReset();
  mockUsePaneStream.mockReturnValue({
    screen: "AGENT SCREEN",
    status: "open",
    error: null,
    seq: 1,
  });
});

describe("CommandCenterAgents", () => {
  it("shows the table by default and does not subscribe", () => {
    render(<CommandCenterAgents />);
    expect(screen.getByText("n-impl")).toBeInTheDocument();
    expect(mockUsePaneStream).not.toHaveBeenCalled();
  });

  it("renders live tiles after switching to grid", () => {
    render(<CommandCenterAgents />);
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    expect(screen.getByText(/AGENT SCREEN/)).toBeInTheDocument();
  });

  it("opens the session-peek pane when a tile is clicked", () => {
    render(<CommandCenterAgents />);
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    fireEvent.click(screen.getByRole("button", { name: /open n-impl/i }));
    expect(mockOpen).toHaveBeenCalledWith("session-peek", { sessionId: "sid1" });
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd dashboard && npx vitest run src/pages/command-center/__tests__/Agents.test.tsx`
Expected: FAIL — no Grid button in the document

- [ ] **Step 3: Implement the tile**

Create `dashboard/src/pages/command-center/AgentConsoleTile.tsx`:

```tsx
/**
 * AgentConsoleTile — one agent's live terminal screen in the Agents grid.
 *
 * Subscribes only while mounted, so tiles cost nothing in Table view.
 */
import { usePaneStream } from "../../ws/usePaneStream";
import LivePaneConsole from "../../components/LivePaneConsole";

interface AgentConsoleTileProps {
  sessionId: string;
  title: string;
  subtitle?: string;
  onOpen?: () => void;
}

export default function AgentConsoleTile({
  sessionId,
  title,
  subtitle,
  onOpen,
}: AgentConsoleTileProps) {
  const { screen, status, error } = usePaneStream(sessionId, { enabled: true });

  return (
    <button
      type="button"
      aria-label={`Open ${title}`}
      onClick={onOpen}
      className="flex flex-col overflow-hidden rounded border border-gray-800 text-left hover:border-gray-600"
    >
      <span className="flex items-baseline justify-between gap-2 bg-gray-900 px-2 py-1">
        <span className="truncate font-mono text-xs text-gray-200">{title}</span>
        <span className="shrink-0 text-[10px] uppercase text-gray-500">
          {subtitle ?? status}
        </span>
      </span>
      <LivePaneConsole
        screen={screen}
        status={status}
        error={error}
        className="h-48 w-full"
      />
    </button>
  );
}
```

- [ ] **Step 4: Add the toggle and grid to `Agents.tsx`**

Keep the existing table markup exactly as it is. Add above it:

```tsx
  const [view, setView] = useState<"table" | "grid">("table");
  const MAX_TILES = 12;

  const running = (sessions ?? []).filter((s) => s.state === "running");
  const tiles = running.slice(0, MAX_TILES);
  const hidden = running.length - tiles.length;
```

Header, rendered above whichever view is active:

```tsx
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300">Agents</h2>
        <div className="flex overflow-hidden rounded border border-gray-800 text-xs">
          {(["table", "grid"] as const).map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => setView(v)}
              className={
                "px-2 py-1 capitalize " +
                (view === v ? "bg-gray-700 text-white" : "text-gray-400")
              }
            >
              {v}
            </button>
          ))}
        </div>
      </div>
```

Grid branch (the existing `<div className="overflow-x-auto rounded border …">` table wrapper renders only when `view === "table"`):

```tsx
      {view === "grid" && (
        <div className="space-y-2">
          {tiles.length === 0 && (
            <p className="text-sm text-gray-500">No running sessions.</p>
          )}
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {tiles.map((s) => (
              <AgentConsoleTile
                key={s.id}
                sessionId={s.id}
                title={s.name ?? s.id}
                onOpen={() => pane.open("session-peek", { sessionId: s.id })}
              />
            ))}
          </div>
          {hidden > 0 && (
            <p className="text-xs text-amber-400">
              +{hidden} more running {hidden === 1 ? "session" : "sessions"} not
              shown (live view is capped at {MAX_TILES}).
            </p>
          )}
        </div>
      )}
```

The cap note is not decoration: silently showing 12 of 20 agents reads as "there are 12 agents".

- [ ] **Step 5: Run the test**

Run: `cd dashboard && npx vitest run src/pages/command-center/__tests__/Agents.test.tsx`
Expected: PASS (3 tests)

- [ ] **Step 6: Full frontend suite + typecheck**

Run: `cd dashboard && npx vitest run && npx tsc --noEmit`
Expected: PASS, no type errors

- [ ] **Step 7: Full backend suite**

Run: `pytest tests/ -n auto -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add dashboard/src/pages/command-center/
git commit -m "feat(dashboard): Agents tab live console grid"
```

---

### Task 7: Manual verification against a real agent

Everything above runs on `FakeProvider`. This confirms the tmux path works end to end.

**Files:** none (verification only)

**Interfaces:**
- Consumes: all prior tasks.
- Produces: nothing.

- [ ] **Step 1: Start the daemon**

Run: `./run.sh start`

- [ ] **Step 2: Confirm a live tmux session exists**

Run: `tmux -u -L aq list-sessions`
Expected: at least one session. If none, queue a task first and wait for it to promote.

- [ ] **Step 3: Curl the pane endpoint**

Run: `curl -N "http://localhost:8000/api/sessions/<session-id>/pane?max_seconds=5"`
Expected: one or more `data: {"source":"pane","type":"screen",...}` lines whose `screen` contains recognisable harness output, plus escape sequences from `-e`.

- [ ] **Step 4: Confirm polling stops when nobody watches**

With no dashboard tab open and the curl finished, wait ~10s, then run:
`ps -eo etime,cmd | grep -c "[c]apture-pane"`
Expected: `0` — no residual poll loop.

- [ ] **Step 5: Check the Agents grid in a browser**

Open `/command-center/agents`, switch to Grid, confirm tiles paint and update roughly once per second, and that clicking one opens the session-peek pane.

- [ ] **Step 6: Confirm the transcript view is unaffected**

Open a session's full detail page, stay on the Transcript toggle, confirm entries still stream.

---

## Self-Review

**Spec coverage:** §3.1 broadcaster → Task 3; §3.2 provider contract → Task 1; §3.3 endpoint → Task 4; §3.4 frontend → Tasks 5 and 6; §4 config → Task 2; §5 error handling → Tasks 3 (loop-side) and 4 (HTTP-side); §6 testing → tests inside every task; §7 files touched → Tasks 1-6 cover every listed path.

**Deviation from the spec, deliberate:** the spec says an over-cap subscription is refused; the endpoint returns **429** for that (the spec named only the frame, not a status). 409 stays reserved for "this provider cannot peek", which is a different, permanent condition.
