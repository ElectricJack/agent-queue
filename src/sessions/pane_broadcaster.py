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
        self._closed = False
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

        Raises :class:`PaneStreamRefused` when the cap is reached or the
        broadcaster has been shut down, and ``CapabilityUnsupported`` when
        the provider cannot peek at all.
        """
        if self._closed:
            raise PaneStreamRefused("pane broadcaster is shut down")
        provider = self._providers.create(session.provider, self._config)
        if not provider.supports(Cap.PEEK):
            from src.sessions.provider import CapabilityUnsupported

            raise CapabilityUnsupported(provider.name, Cap.PEEK)

        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        async with self._lock:
            if self._closed:
                raise PaneStreamRefused("pane broadcaster is shut down")
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

    def detach(self, session_name: str, queue: asyncio.Queue) -> None:
        """Detach a subscriber; the loop lingers before stopping.

        Deliberately **synchronous and lock-free**: teardown runs in an
        SSE generator's ``finally``, which Starlette reaches by cancelling
        the generator on client disconnect.  Any ``await`` there — even
        ``async with self._lock`` — can re-deliver ``CancelledError`` at
        the suspension point and skip the removal, stranding a poll loop
        with a subscriber that will never read again.  There is no await
        between the read and the write below, so the discard cannot be
        interrupted and the property holds unconditionally.
        """
        watch = self._watches.get(session_name)
        if watch is None:
            return
        watch.subscribers.discard(queue)
        if not watch.subscribers:
            watch.stop_at = time.monotonic() + self.linger_seconds

    async def unsubscribe(self, session_name: str, queue: asyncio.Queue) -> None:
        """Async alias for :meth:`detach` (kept for existing callers)."""
        self.detach(session_name, queue)

    async def shutdown(self) -> None:
        """Cancel every loop.  Called from daemon/app teardown.

        Sets ``_closed`` under the lock *before* releasing it, so no
        ``subscribe()`` racing this call can observe an empty ``_watches``
        and slip a fresh loop in after the snapshot below is taken — it
        either lands before this lock is acquired (and gets cancelled with
        everything else) or after ``_closed`` is set (and is refused).
        """
        async with self._lock:
            self._closed = True
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
        block the loop.  Each frame is a *full snapshot*, so the oldest
        frame is the worthless one: dropping the newest would pin a
        briefly-slow reader to a stale screen, and a dropped terminal
        frame would leave its SSE generator running forever.  Drop from
        the front and enqueue instead.
        """
        watch.seq += 1
        frame = {"source": "pane", "seq": watch.seq, "ts": time.time(), **payload}
        targets = [only] if only is not None else list(watch.subscribers)
        for queue in targets:
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                logger.debug("pane subscriber queue full; dropping oldest frame")
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(frame)

    async def _run(self, session, watch: _Watch) -> None:
        handle = SessionHandle(
            name=session.name,
            provider=session.provider,
            instance_token=getattr(session, "instance_token", "") or "",
        )
        provider = self._providers.create(session.provider, self._config)
        errors = 0
        probe_errors = 0
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
                #
                # ``process_alive`` — not ``is_running``.  TmuxProvider sets
                # ``remain-on-exit on``, so a pane outlives its process:
                # ``is_running`` stays True for a finished agent forever and
                # the viewer never learns the session ended.  ``process_alive``
                # is the method that consults ``pane_dead`` / the process
                # subtree.  ``process_names`` is left empty on purpose: the
                # reconciler narrows by harness name to decide whether to
                # *reap*, while this loop only decides whether to show a
                # banner, and an empty tuple can only ever err towards
                # "still alive".
                try:
                    alive = await provider.process_alive(handle, ())
                except Exception:
                    alive = True  # unknown is not dead
                    probe_errors += 1
                    # A persistently failing probe would otherwise be
                    # invisible: the loop just never reports a stop.
                    if probe_errors == 1 or probe_errors % 30 == 0:
                        logger.warning(
                            "pane liveness probe failing for %s (%d in a row); "
                            "treating the session as alive",
                            session.name,
                            probe_errors,
                            exc_info=True,
                        )
                else:
                    probe_errors = 0
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
