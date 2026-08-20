"""Degraded session provider: a detached process group, no pane.

For hosts without tmux.  Env markers, work_dir, transcripts and the
``aq task close`` completion protocol are identical to the tmux path, so
tasks complete normally — what is lost is ATTACH, PEEK and NUDGE.  The
stall ladder therefore skips its nudge rungs and goes straight to restart.

This module is named ``subprocess`` to match the spec's module layout.
Python 3 absolute imports mean ``import subprocess`` inside this package
still resolves to the standard library.

Known limitation (honest, not deferred-by-omission): ``list_running`` only
enumerates sessions **this daemon process started**, because the
process-table scan (``src/sessions/proctable.py``, which needs
``/proc/<pid>/environ``) lands with the tmux wave.  Until then a subprocess
session does not survive a daemon restart for adoption purposes: the
reconciler sees an empty listing rather than a wrong one, so nothing is
mis-reaped, but nothing is re-adopted either.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from src.sessions.provider import (
    Cap,
    CapabilityUnsupported,
    SessionDiedDuringStartup,
    SessionHandle,
    SessionProvider,
    SessionSpec,
)

logger = logging.getLogger(__name__)

__all__ = ["SubprocessProvider"]

_POSIX = os.name == "posix"


@dataclass
class _Running:
    spec: SessionSpec
    handle: SessionHandle
    proc: asyncio.subprocess.Process
    log_path: str
    started_at: float
    meta: dict[str, str] = field(default_factory=dict)
    last_poke: float = 0.0


class SubprocessProvider(SessionProvider):
    """Detached process group with stdout/stderr redirected to a log file."""

    name: ClassVar[str] = "subprocess"
    #: No ATTACH / NUDGE.  PEEK is advertised because the log file *is* a
    #: readable tail — degraded, but honest output rather than ``""``.
    capabilities: ClassVar[frozenset[Cap]] = frozenset({Cap.ACTIVITY, Cap.PEEK})

    def __init__(self, config=None):
        self.config = config
        self._sessions: dict[str, _Running] = {}

    # -- paths -------------------------------------------------------------

    def _state_dir(self, name: str) -> Path:
        data_dir = getattr(self.config, "data_dir", None) or os.path.expanduser(
            "~/.agent-queue"
        )
        return Path(data_dir) / "sessions" / name

    # -- lifecycle ---------------------------------------------------------

    async def start(self, spec: SessionSpec) -> SessionHandle:
        work_dir = Path(spec.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        _write_spec_files(spec)

        state_dir = self._state_dir(spec.session_name)
        state_dir.mkdir(parents=True, exist_ok=True)
        log_path = state_dir / "out.log"

        argv = list(spec.command)
        if not argv:
            raise ValueError(f"session {spec.session_name!r} has an empty command")

        # ``start_new_session`` detaches the child into its own process
        # group so a daemon restart does not take it down with a stray
        # signal, and so ``stop`` can signal the whole tree at once.
        kwargs: dict = {}
        if _POSIX:
            kwargs["start_new_session"] = True

        log_file = open(log_path, "ab", buffering=0)  # noqa: SIM115 - lives with the child
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(work_dir),
                env=dict(spec.env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                **kwargs,
            )
        except OSError as exc:
            log_file.close()
            raise SessionDiedDuringStartup(
                spec.session_name, start_stderr_path=str(log_path), detail=str(exc)
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                log_file.close()

        handle = SessionHandle(
            name=spec.session_name,
            provider=self.name,
            instance_token=spec.instance_token,
        )
        self._sessions[spec.session_name] = _Running(
            spec=spec,
            handle=handle,
            proc=proc,
            log_path=str(log_path),
            started_at=time.time(),
        )

        # Readiness for a pane-less provider is "did it survive the first
        # moment" — there is no prompt to scrape.  A death inside the ready
        # delay is a startup failure with the log as evidence.
        delay = max(0.0, min(spec.ready_delay_ms, 5000) / 1000.0)
        if delay:
            await asyncio.sleep(delay)
        if proc.returncode is not None:
            raise SessionDiedDuringStartup(
                spec.session_name,
                start_stderr_path=str(log_path),
                detail=f"exited with code {proc.returncode}",
            )
        return handle

    async def stop(self, h: SessionHandle, *, grace: float = 2.0) -> None:
        running = self._get(h)
        if running is None:
            return  # idempotent + fenced
        proc = running.proc
        if proc.returncode is None:
            _signal_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except (TimeoutError, asyncio.TimeoutError):
                # 2 s default, not 100 ms: a short grace orphans Claude.
                _signal_group(proc, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=grace)
        self._sessions.pop(h.name, None)

    async def interrupt(self, h: SessionHandle) -> None:
        running = self._get(h)
        if running is None or running.proc.returncode is not None:
            return
        _signal_group(running.proc, getattr(signal, "SIGINT", signal.SIGTERM))

    # -- observation -------------------------------------------------------

    async def is_running(self, h: SessionHandle) -> bool:
        running = self._get(h)
        return bool(running and running.proc.returncode is None)

    async def process_alive(self, h: SessionHandle, process_names: tuple[str, ...] = ()) -> bool:
        # For this provider the tracked child *is* the agent, so there is no
        # second probe to fail and no optimistic-alive branch to take.
        running = self._get(h)
        return bool(running and running.proc.returncode is None)

    async def list_running(self, prefix: str) -> list[SessionHandle]:
        return [
            r.handle
            for name, r in sorted(self._sessions.items())
            if name.startswith(prefix) and r.proc.returncode is None
        ]

    async def last_activity(self, h: SessionHandle) -> float | None:
        running = self._get(h)
        if running is None:
            return None
        try:
            mtime = os.path.getmtime(running.log_path)
        except OSError:
            return None
        # Poke discounting: we have no input channel, so the only writer is
        # the agent — but keep the guard so the semantics match tmux.
        if running.last_poke and abs(mtime - running.last_poke) < 3.0:
            return running.started_at
        return mtime

    async def peek(self, h: SessionHandle, lines: int = 60) -> str:
        running = self._get(h)
        if running is None:
            return ""
        try:
            text = Path(running.log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    # -- interaction -------------------------------------------------------

    async def nudge(self, h: SessionHandle, text: str) -> None:
        raise CapabilityUnsupported(self.name, Cap.NUDGE)

    async def attach_command(self, h: SessionHandle) -> str:
        raise CapabilityUnsupported(self.name, Cap.ATTACH)

    # -- metadata ----------------------------------------------------------

    async def set_meta(self, h: SessionHandle, key: str, value: str) -> None:
        running = self._get(h)
        if running is not None:
            running.meta[key] = value

    async def get_meta(self, h: SessionHandle, key: str) -> str | None:
        running = self._get(h)
        return running.meta.get(key) if running else None

    # -- internals ---------------------------------------------------------

    def _get(self, h: SessionHandle) -> _Running | None:
        running = self._sessions.get(h.name)
        if running is None:
            return None
        if h.instance_token and running.handle.instance_token != h.instance_token:
            # Name reuse: this handle addresses a predecessor, not the
            # session currently holding the name.  Never signal it.
            return None
        return running


def _write_spec_files(spec: SessionSpec) -> None:
    """Materialise ``spec.files`` under ``work_dir`` before launch."""
    root = Path(spec.work_dir)
    for rel, content in spec.files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the child's whole process group, falling back to the child.

    On Windows there are no process groups in the POSIX sense; the
    ``terminate``/``kill`` fallback is the honest degradation there.
    """
    if proc.returncode is not None:
        return
    if _POSIX:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    hard = sig == getattr(signal, "SIGKILL", None)
    try:
        proc.kill() if hard else proc.terminate()
    except (ProcessLookupError, OSError):
        pass
