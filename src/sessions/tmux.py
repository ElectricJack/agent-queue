"""TmuxProvider — the default session provider on Linux/WSL2.

Every session is one detached tmux session with one pane whose initial
process is the harness CLI (``exec``'d, never left inside a shell).  The
session survives daemon restarts because the tmux *server* owns it; the
daemon re-adopts by name + ``AQ_*`` markers + instance token.

Implements ``docs/specs/implementation/session-runtime.md`` §3.2.  The §9
pitfall table (Gas City post-mortem) is treated as a checklist here; each
mitigation is called out at its implementation site.

POSIX-only by construction (``/proc``, signals, tmux).  Importing this
module on Windows raises ``ImportError`` so
:func:`~src.sessions.default_session_registry` skips registration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import ClassVar

if os.name != "posix":  # pragma: no cover - the registry's gate
    raise ImportError("the tmux session provider requires a POSIX host")

from src.sessions import proctable
from src.sessions.dialogs import DialogBudget, run_dialog_dismissal
from src.sessions.provider import (
    Cap,
    NotSubmitted,
    NudgeDeferred,
    PartialListError,
    SessionDiedDuringStartup,
    SessionError,
    SessionHandle,
    SessionProvider,
    SessionSpec,
    require_session_executable,
    validate_terminal_input,
)
from src.sessions.state_cache import TmuxStateCache, TmuxUnavailable
from src.sessions.subprocess import _write_spec_files

logger = logging.getLogger(__name__)

__all__ = ["TmuxProvider", "TmuxCommandError"]

#: Shell commands that mean "the agent has not started yet" during the
#: readiness poll (the pane briefly shows the wrapper shell).
_SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh"})

#: NBSP-insensitive comparison for readiness prefixes — Claude's ``❯`` is
#: followed by a non-breaking space, and harness files are written by
#: humans who cannot see the difference.
_NBSP = " "

#: ``send-keys -l`` payload ceiling; larger nudges go through a buffer.
_SEND_KEYS_MAX_BYTES = 4096

# tmux may accept manual input before the harness paints the updated draft.
_MANUAL_INPUT_QUIET_SECONDS = 2.0

_META_TOKEN_KEY = "AQ_INSTANCE_TOKEN"


def _normalize(text: str) -> str:
    return text.replace(_NBSP, " ")


class TmuxCommandError(SessionError):
    """A tmux invocation failed; carries the command and stderr."""

    def __init__(self, args: tuple[str, ...], returncode: int | None, stderr: str):
        self.args_ = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"tmux {' '.join(args)} -> {returncode}: {stderr.strip()}")


class TmuxProvider(SessionProvider):
    """Attachable tmux panes, one per session, on a dedicated socket."""

    name: ClassVar[str] = "tmux"
    capabilities: ClassVar[frozenset[Cap]] = frozenset(
        {Cap.ATTACH, Cap.PEEK, Cap.NUDGE, Cap.ACTIVITY, Cap.RELAUNCH, Cap.INPUT}
    )

    def __init__(self, config=None):
        self.config = config
        sessions_cfg = getattr(config, "sessions", None)
        self.socket: str = getattr(sessions_cfg, "tmux_socket", None) or "aq"
        self.nudge_debounce_ms: int = getattr(sessions_cfg, "nudge_debounce_ms", 500)
        self.dialog_budget_seconds: float = getattr(sessions_cfg, "dialog_budget_seconds", 8)
        ttl = getattr(sessions_cfg, "state_cache_ttl_seconds", 2)
        self._cache = TmuxStateCache(self._tmux, ttl=ttl)
        self._nudge_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_nudge_at: dict[str, float] = {}
        self._last_input_at: dict[str, float] = {}
        #: Poke discounting state: name -> (send_ts, activity observed
        #: immediately before the send).
        self._poke: dict[str, tuple[float, float | None]] = {}
        #: Session-env instance tokens, so list_running does not need one
        #: ``show-environment`` per session per tick.
        self._token_cache: dict[str, str] = {}

    # -- plumbing ----------------------------------------------------------

    async def _tmux(self, *args: str, timeout: float = 30.0, stdin: bytes | None = None) -> str:
        """Run ``tmux -u -L <socket> <args…>`` and return stdout.

        Async-first (never ``subprocess.run``); a hung socket raises
        rather than freezing the daemon.
        """
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-u",
            "-L",
            self.socket,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise TmuxCommandError(args, None, f"timed out after {timeout}s") from None
        if proc.returncode != 0:
            raise TmuxCommandError(args, proc.returncode, err.decode(errors="replace"))
        return out.decode(errors="replace")

    def _state_dir(self, name: str) -> Path:
        data_dir = getattr(self.config, "data_dir", None) or os.path.expanduser("~/.agent-queue")
        return Path(data_dir) / "sessions" / name

    async def _probe_server(self) -> None:
        """Fail fast on an unresponsive socket before creating anything.

        Pitfall §9: a slow/hung socket makes tmux unlink and rebind it,
        orphaning every session on the old server.  The probe is any cheap
        command with a short timeout; "session not found" is a *healthy*
        answer (the server responded).
        """
        try:
            await self._tmux("has-session", "-t", "=__aq_probe__", timeout=5.0)
        except TmuxCommandError as exc:
            if exc.returncode is None:  # timeout — the dangerous case
                raise SessionError(
                    f"tmux socket {self.socket!r} is unresponsive; refusing to create "
                    "a session that could orphan the existing server"
                ) from exc
            # Nonzero exit: server responded (or there is no server yet,
            # which new-session will fix).  Either way, safe to proceed.

    async def _session_token(self, name: str) -> str | None:
        """The session's ``AQ_INSTANCE_TOKEN`` from its tmux environment."""
        cached = self._token_cache.get(name)
        if cached is not None:
            return cached
        try:
            out = await self._tmux("show-environment", "-t", f"={name}", _META_TOKEN_KEY)
        except TmuxCommandError:
            return None
        value = _parse_environment_value(out, _META_TOKEN_KEY)
        if value is not None:
            self._token_cache[name] = value
        return value

    async def _fenced(self, h: SessionHandle) -> bool:
        """True when *h* addresses the session currently holding the name."""
        token = await self._session_token(h.name)
        if token is None:
            # No session (or no marker): nothing to operate on under this
            # name, so a fenced operation misses rather than guesses.
            return False
        return not h.instance_token or token == h.instance_token

    # -- lifecycle ---------------------------------------------------------

    async def start(self, spec: SessionSpec) -> SessionHandle:
        require_session_executable(spec)
        work_dir = Path(spec.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        _write_spec_files(spec)

        if not spec.command:
            raise ValueError(f"session {spec.session_name!r} has an empty command")

        await self._probe_server()

        # ``exec`` so the agent *is* the pane process, not a child of a
        # lingering shell — process discovery and signalling depend on it.
        # tmux replaces a pane's PATH with the client's PATH even when
        # new-session -e PATH=... set the session environment. Restore the
        # same lookup context used by preflight at exec time; quote it as
        # literal data, including relative entries and shell metacharacters.
        launch_path = shlex.quote(spec.env.get("PATH", os.defpath))
        shell_cmd = f"PATH={launch_path} exec " + " ".join(shlex.quote(a) for a in spec.command)

        args = ["new-session", "-d", "-s", spec.session_name, "-c", str(work_dir)]
        for key, value in spec.env.items():
            args.extend(["-e", f"{key}={value}"])
        args.append(shell_cmd)
        await self._tmux(*args)
        self._cache.mark_server_seen()
        self._cache.invalidate()
        self._token_cache.pop(spec.session_name, None)

        # Persist the nudge quirks on the *session* environment: the tmux
        # server owns it, so both survive a daemon restart (the spec
        # object does not).
        for key, value in (
            ("AQ_SKIP_ESCAPE", "1" if spec.skip_escape_before_enter else "0"),
            ("AQ_PROCESS_NAMES", ",".join(spec.process_names)),
            ("AQ_READY_PREFIX", spec.ready_prompt_prefix or ""),
        ):
            with contextlib.suppress(TmuxCommandError):
                await self._tmux("set-environment", "-t", f"={spec.session_name}", key, value)

        # ``=name`` exact-matches *session* targets only; window/pane
        # targets need the ``=name:`` form or tmux reports "no such window".
        window = f"={spec.session_name}:"
        # Pitfall §9: tmux 3.3 pins detached sessions at 80×24 without
        # ``window-size latest``; ``remain-on-exit`` keeps the pane (and
        # its last output) around after the agent dies so the exit
        # classifier has evidence; activity monitoring is ours, not tmux's.
        for opt in (
            ("set-option", "-w", "-t", window, "window-size", "latest"),
            ("set-option", "-w", "-t", window, "remain-on-exit", "on"),
            ("set-option", "-t", f"={spec.session_name}", "mouse", "off"),
            ("set-option", "-w", "-t", window, "monitor-activity", "off"),
        ):
            with contextlib.suppress(TmuxCommandError):
                await self._tmux(*opt)

        handle = SessionHandle(
            name=spec.session_name,
            provider=self.name,
            instance_token=spec.instance_token,
        )
        await self._await_ready(handle, spec)
        return handle

    async def _await_ready(self, h: SessionHandle, spec: SessionSpec) -> None:
        """Wait until the harness looks started; §3.2's readiness dance.

        A readiness *timeout* with a live process is not an error — some
        harnesses paint slowly.  A dead pane is: its last output goes to
        ``start-stderr.log`` and :class:`SessionDiedDuringStartup` carries
        the path.
        """
        target = f"={h.name}:"  # window/pane form; see start()
        budget_ms = max(5000, min(spec.ready_delay_ms + 5000, 60000))
        deadline = time.monotonic() + budget_ms / 1000.0
        dialog_budget = DialogBudget(float(self.dialog_budget_seconds))
        fired: set[str] = set()

        async def capture() -> str:
            try:
                return await self._tmux("capture-pane", "-p", "-t", target, "-S", "-60")
            except TmuxCommandError:
                return ""

        async def send(keys: tuple[str, ...]) -> None:
            with contextlib.suppress(TmuxCommandError):
                await self._tmux("send-keys", "-t", target, *keys)

        async def pane_dead() -> bool:
            try:
                out = await self._tmux("list-panes", "-t", target, "-F", "#{pane_dead}")
            except TmuxCommandError:
                return True  # session gone entirely
            lines = out.split()
            return bool(lines) and all(line == "1" for line in lines)

        async def die(detail: str) -> None:
            text = await capture()
            state_dir = self._state_dir(h.name)
            path = state_dir / "start-stderr.log"
            try:
                await asyncio.to_thread(state_dir.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_text, text, "utf-8")
            except OSError:
                path = None  # type: ignore[assignment]
            with contextlib.suppress(Exception):
                await self._tmux("kill-session", "-t", f"={h.name}")
            raise SessionDiedDuringStartup(
                h.name,
                start_stderr_path=str(path) if path else None,
                detail=detail,
            )

        # Phase 1: wait for the pane's command to stop being a shell.
        while True:
            if await pane_dead():
                await die("process died before start")
            try:
                current = (
                    await self._tmux(
                        "display-message", "-p", "-t", target, "#{pane_current_command}"
                    )
                ).strip()
            except TmuxCommandError:
                current = ""
            if current and current not in _SHELLS:
                break
            if time.monotonic() > deadline:
                break  # non-fatal: slow paint, live process
            await asyncio.sleep(0.1)

        # Phase 2: dialogs may cover the prompt — dismiss before waiting
        # for it, and again after (§3.2 "interleave").
        outcome = await run_dialog_dismissal(
            capture=capture,
            send_keys=send,
            dialogs=spec.dialogs,
            budget=dialog_budget,
            fired=fired,
        )
        if outcome.quarantined is not None:
            await die(f"quarantine dialog {outcome.quarantined.name!r} matched during startup")

        if spec.ready_prompt_prefix:
            prefix = _normalize(spec.ready_prompt_prefix)
            while time.monotonic() <= deadline:
                if await pane_dead():
                    await die("process died while waiting for the ready prompt")
                text = _normalize(await capture())
                if any(line.lstrip().startswith(prefix) for line in text.splitlines()):
                    break
                await asyncio.sleep(0.2)
            # Timeout: non-fatal with a live pane.
        elif spec.ready_delay_ms:
            await asyncio.sleep(min(spec.ready_delay_ms, budget_ms) / 1000.0)
            if await pane_dead():
                await die("process died inside the ready delay")

        outcome = await run_dialog_dismissal(
            capture=capture,
            send_keys=send,
            dialogs=spec.dialogs,
            budget=dialog_budget,
            fired=fired,
        )
        if outcome.quarantined is not None:
            await die(f"quarantine dialog {outcome.quarantined.name!r} matched during startup")

    async def stop(self, h: SessionHandle, *, grace: float = 2.0) -> None:
        if not await self._fenced(h):
            return  # idempotent, and never a same-named successor
        pid = await self._pane_pid(h.name)
        if pid is not None:
            # Pitfall §9 (PID recycling): every signal inside is fenced by
            # start-time and, where readable, the instance token.
            await proctable.kill_tree(pid, instance_token=h.instance_token or None, grace=grace)
        with contextlib.suppress(TmuxCommandError):
            await self._tmux("kill-session", "-t", f"={h.name}")
        self._token_cache.pop(h.name, None)
        self._poke.pop(h.name, None)
        self._cache.forget(h.name)

    async def interrupt(self, h: SessionHandle) -> None:
        if not await self._fenced(h):
            return
        pane = await self._find_agent_pane(h.name, ())
        if pane is None:
            return
        with contextlib.suppress(TmuxCommandError):
            await self._tmux("send-keys", "-t", pane, "C-c")

    # -- observation -------------------------------------------------------

    async def _panes(self) -> dict:
        return await self._cache.panes()

    async def is_running(self, h: SessionHandle) -> bool:
        try:
            panes = await self._panes()
        except TmuxUnavailable as exc:
            panes = exc.last_good  # unknown ≠ dead; answer from last-known-good
        if h.name not in panes:
            self._token_cache.pop(h.name, None)
            return False
        return await self._fenced(h)

    async def process_alive(self, h: SessionHandle, process_names: tuple[str, ...] = ()) -> bool:
        try:
            panes = await self._panes()
        except TmuxUnavailable as exc:
            panes = exc.last_good
        pane = panes.get(h.name)
        if pane is None or pane.dead:
            return False
        if not await self._fenced(h):
            return False
        procs = await self._cache.procs()
        if procs is None:
            return True  # ps failed — optimistic-alive, never reap on a failed probe
        subtree = self._cache.descendants(procs, pane.pid)
        if not subtree:
            return False
        if not process_names:
            return True
        for proc in subtree:
            for wanted in process_names:
                if wanted in proc.comm or wanted in proc.args:
                    return True
        return False

    async def list_running(self, prefix: str) -> list[SessionHandle]:
        try:
            panes = await self._panes()
        except TmuxUnavailable as exc:
            partial = await self._handles_for([n for n in exc.last_good if n.startswith(prefix)])
            raise PartialListError(partial, exc.cause) from exc
        return await self._handles_for(sorted(n for n in panes if n.startswith(prefix)))

    async def _handles_for(self, names: list[str]) -> list[SessionHandle]:
        handles = []
        for name in names:
            token = await self._session_token(name) or ""
            handles.append(SessionHandle(name=name, provider=self.name, instance_token=token))
        return handles

    async def last_activity(self, h: SessionHandle) -> float | None:
        if not await self._fenced(h):
            return None
        try:
            # Pitfall §9: ``#{session_activity}`` goes stale on detached
            # sessions; the max over ``#{window_activity}`` does not.
            out = await self._tmux("list-windows", "-t", f"={h.name}", "-F", "#{window_activity}")
        except TmuxCommandError:
            return None
        stamps = [float(line) for line in out.split() if line.isdigit()]
        if not stamps:
            return None
        activity = max(stamps)
        poke = self._poke.get(h.name)
        if poke is not None:
            sent_at, before = poke
            # Poke discounting: output within 3 s of our own send is the
            # echo of the nudge, not agent progress.  ``window_activity``
            # is truncated to whole seconds, so the echo's stamp can read
            # up to ~1 s *before* our fractional send time.
            if activity >= sent_at - 1.001 and activity <= sent_at + 3.0:
                return before
        return activity

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

    # -- interaction -------------------------------------------------------

    async def send_input(
        self, h: SessionHandle, *, text: str | None = None, key: str | None = None
    ) -> None:
        """Direct human input, serialized with nudges and fenced to one pane.

        A cached name token is insufficient here: a stale browser must never
        type into a same-named successor. Resolve the exact pane, then read the
        live token and send to that pane ID rather than the recyclable name.
        """
        validate_terminal_input(text, key)
        async with self._nudge_locks[h.name]:
            if not h.instance_token:
                raise SessionError("Terminal input requires an instance token")
            names = await self._process_names_hint(h.name)
            pane = await self._find_agent_pane(h.name, names)
            if pane is None:
                raise SessionError("No live terminal pane")
            observed = await self._tmux(
                "show-environment", "-t", f"={h.name}", _META_TOKEN_KEY,
            )
            if _parse_environment_value(observed, _META_TOKEN_KEY) != h.instance_token:
                raise SessionError("Terminal session instance changed")

            # Unpark copy mode. A detached TUI may also need a one-time
            # resize signal before accepting input after an idle period.
            with contextlib.suppress(TmuxCommandError):
                in_mode = (await self._tmux(
                    "display-message", "-p", "-t", pane, "#{pane_in_mode}",
                )).strip()
                if in_mode == "1":
                    await self._tmux("send-keys", "-t", pane, "-X", "cancel")
                if time.monotonic() - self._last_input_at.get(h.name, 0) > 5:
                    await self._tmux("resize-pane", "-t", pane, "-D", "1")
                    await self._tmux("resize-pane", "-t", pane, "-U", "1")
            if key is not None:
                await self._tmux("send-keys", "-t", pane, key)
            else:
                assert text is not None
                payload = text.encode("utf-8")
                if len(payload) <= _SEND_KEYS_MAX_BYTES and "\n" not in text and "\r" not in text:
                    await self._tmux("send-keys", "-t", pane, "-l", "--", text)
                else:
                    # Bracketed paste preserves multiline input. Each buffer
                    # is unique, so two tiled terminals cannot swap pastes.
                    buffer = "aq-input-" + uuid.uuid4().hex
                    await self._tmux("load-buffer", "-b", buffer, "-", stdin=payload)
                    try:
                        await self._tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", pane)
                    finally:
                        with contextlib.suppress(TmuxCommandError):
                            await self._tmux("delete-buffer", "-b", buffer)
            self._last_input_at[h.name] = time.monotonic()

    async def nudge(self, h: SessionHandle, text: str) -> None:
        lock = self._nudge_locks[h.name]
        async with lock:
            last_input = self._last_input_at.get(h.name)
            if (
                last_input is not None
                and time.monotonic() - last_input < _MANUAL_INPUT_QUIET_SECONDS
            ):
                # An old empty frame is not proof the newly accepted input
                # is empty. Defer immediately; do not sleep while holding input.
                raise NudgeDeferred(f"terminal {h.name!r} has recent manual input")
            if not await self._fenced(h):
                raise NotSubmitted(f"session {h.name!r} is gone")

            # Debounce successive nudges (input collision pitfall).
            debounce = self.nudge_debounce_ms / 1000.0
            elapsed = time.monotonic() - self._last_nudge_at.get(h.name, 0.0)
            if elapsed < debounce:
                await asyncio.sleep(debounce - elapsed)

            spec_names = await self._process_names_hint(h.name)
            pane = await self._find_agent_pane(h.name, spec_names)
            if pane is None:
                raise NotSubmitted(f"no live pane found for {h.name!r}")

            # Never append a reminder to a user's draft or compete with an
            # attached terminal. This guard shares send_input's lock and runs
            # before any resize, key, or paste (including copy-mode cancel).
            prefix = await self._ready_prefix_hint(h.name)
            await self._require_empty_composer(h.name, pane, prefix)

            # Record pre-send activity for poke discounting.
            before = await self._raw_activity(h.name)

            # Detached TUIs drop pastes until a SIGWINCH wakes them (§9).
            with contextlib.suppress(TmuxCommandError):
                await self._tmux("resize-pane", "-t", pane, "-D", "1")
                await self._tmux("resize-pane", "-t", pane, "-U", "1")

            # A repaint or a newly attached client can invalidate the first
            # observation. Recheck immediately before writing the reminder.
            await self._require_empty_composer(h.name, pane, prefix)
            payload = text.encode("utf-8")
            if len(payload) <= _SEND_KEYS_MAX_BYTES:
                await self._tmux("send-keys", "-t", pane, "-l", "--", text)
            else:
                await self._tmux("load-buffer", "-b", "aq-nudge", "-", stdin=payload)
                await self._tmux("paste-buffer", "-p", "-d", "-b", "aq-nudge", "-t", pane)

            self._last_nudge_at[h.name] = time.monotonic()
            self._poke[h.name] = (time.time(), before)

            marker = text.strip().splitlines()[-1][-48:] if text.strip() else ""

            # Landed check (live-test regression): a TUI mid-turn swallows
            # typed keys entirely — the text never reaches the input line,
            # yet the absence-of-marker confirm below would read that as
            # "submitted" and the message would be marked delivered unseen.
            # Require the marker to render before pressing Enter.  Paste-
            # buffer pastes are exempt: harnesses collapse large pastes to
            # a placeholder, so the marker legitimately never renders.
            if marker and len(payload) <= _SEND_KEYS_MAX_BYTES:
                for _poll in range(8):
                    tail = await self._capture_tail(pane, lines=40)
                    if marker in _normalize(tail):
                        break
                    await asyncio.sleep(0.15)
                else:
                    raise NotSubmitted(f"typed text never rendered in {h.name!r}")

            # Per-harness Escape semantics (§9): only when the harness says
            # it is safe — grok clears the input line, codex backtracks.
            if not await self._skip_escape(h.name):
                await self._tmux("send-keys", "-t", pane, "Escape")
                await asyncio.sleep(0.05)

            # Enter, then confirm submission: the pasted text must leave
            # the *input line*.  Enter races bracketed paste (§9), so retry.
            # Harnesses echo the submitted prompt into the transcript
            # (Claude repaints it as ``❯ <text>``), so "marker anywhere on
            # screen" is a false negative after a successful submit — the
            # check is anchored to the last prompt-prefixed line instead
            # (see :func:`_submit_pending`).
            for _attempt in range(3):
                await self._tmux("send-keys", "-t", pane, "Enter")
                for _poll in range(4):
                    await asyncio.sleep(0.15)
                    tail = await self._capture_tail(pane, lines=40)
                    if not _submit_pending(tail, marker, prefix):
                        self._poke[h.name] = (time.time(), before)
                        return
            raise NotSubmitted(f"submit unconfirmed for {h.name!r} after 3 attempts")

    async def attach_command(self, h: SessionHandle) -> str:
        return f"tmux -u -L {self.socket} attach -t ={h.name}"

    # -- provider-side metadata -------------------------------------------

    async def set_meta(self, h: SessionHandle, key: str, value: str) -> None:
        if not _SAFE_META_KEY.match(key):
            raise ValueError(f"invalid meta key: {key!r}")
        if not await self._fenced(h):
            return
        with contextlib.suppress(TmuxCommandError):
            # The tmux *server* owns session environments, so this survives
            # a daemon restart — the drain-ack marker depends on that.
            await self._tmux("set-environment", "-t", f"={h.name}", key, value)

    async def get_meta(self, h: SessionHandle, key: str) -> str | None:
        if not _SAFE_META_KEY.match(key):
            raise ValueError(f"invalid meta key: {key!r}")
        if not await self._fenced(h):
            return None
        try:
            out = await self._tmux("show-environment", "-t", f"={h.name}", key)
        except TmuxCommandError:
            return None
        return _parse_environment_value(out, key)

    # -- internals ---------------------------------------------------------

    async def _pane_pid(self, name: str) -> int | None:
        try:
            out = await self._tmux("list-panes", "-t", f"={name}:", "-F", "#{pane_pid}")
        except TmuxCommandError:
            return None
        for line in out.split():
            try:
                return int(line)
            except ValueError:
                continue
        return None

    async def _find_agent_pane(self, name: str, process_names: tuple[str, ...]) -> str | None:
        """The pane id whose subtree contains the agent process.

        Discovery is by ``process_names`` against the pane's *descendants*
        (§9: systemd may reparent into ``tmux-spawn-*.scope``, and
        ``pane_current_command`` alone lies).  One pane per session is the
        common case; the walk matters when an attached operator split one.
        """
        try:
            out = await self._tmux(
                "list-panes", "-t", f"={name}:", "-F", "#{pane_id}\t#{pane_pid}\t#{pane_dead}"
            )
        except TmuxCommandError:
            return None
        panes: list[tuple[str, int]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3 or parts[2] == "1":
                continue
            try:
                panes.append((parts[0], int(parts[1])))
            except ValueError:
                continue
        if not panes:
            return None
        if len(panes) == 1 or not process_names:
            return panes[0][0]
        procs = await self._cache.procs()
        if procs is None:
            return panes[0][0]
        for pane_id, pid in panes:
            for proc in self._cache.descendants(procs, pid):
                if any(w in proc.comm or w in proc.args for w in process_names):
                    return pane_id
        return panes[0][0]

    async def _raw_activity(self, name: str) -> float | None:
        try:
            out = await self._tmux("list-windows", "-t", f"={name}", "-F", "#{window_activity}")
        except TmuxCommandError:
            return None
        stamps = [float(line) for line in out.split() if line.isdigit()]
        return max(stamps) if stamps else None

    async def _capture_tail(self, pane: str, lines: int = 5) -> str:
        # ``capture-pane -S -N`` starts N lines *above the visible screen*
        # and captures through the bottom — the whole screen plus history,
        # not a tail.  The nudge submit-confirm relies on genuinely seeing
        # only the input-box region: Claude echoes the submitted prompt
        # into the transcript, so a whole-screen capture still contains the
        # marker after a successful submit and every nudge reads as
        # NotSubmitted (observed live: envelope delivered twice, row never
        # marked).  Trim to the last N non-blank-padded lines here.
        try:
            out = await self._tmux("capture-pane", "-p", "-t", pane)
        except TmuxCommandError:
            return ""
        trimmed = out.rstrip("\n")
        return "\n".join(trimmed.splitlines()[-lines:])

    async def _require_empty_composer(self, name: str, pane: str, prefix: str) -> None:
        """Fail closed on drafts, active terminal clients, and unknown TUIs."""
        fmt = (
            "#{cursor_x}\t#{cursor_y}\t#{pane_width}\t#{pane_height}\t"
            "#{cursor_flag}\t#{pane_in_mode}\t#{session_attached}"
        )
        try:
            before = await self._tmux("display-message", "-p", "-t", pane, fmt)
            x, y, width, height, visible, in_mode, attached = map(int, before.split())
            if (
                not prefix.strip()
                or visible != 1
                or in_mode != 0
                or attached != 0
                or not 0 <= x < width
                or not 0 <= y < height
            ):
                raise NudgeDeferred(f"terminal {name!r} is busy or its input is unknown")
            screen = await self._tmux("capture-pane", "-p", "-e", "-t", pane)
            after = await self._tmux("display-message", "-p", "-t", pane, fmt)
        except (TmuxCommandError, ValueError) as exc:
            raise NudgeDeferred(f"cannot inspect input for {name!r}") from exc
        if before != after or not _composer_is_empty(screen, prefix, x, y, height):
            raise NudgeDeferred(f"terminal {name!r} has a draft or its input is unknown")

    async def _process_names_hint(self, name: str) -> tuple[str, ...]:
        """The spec's ``process_names``, recovered from the session env."""
        try:
            out = await self._tmux("show-environment", "-t", f"={name}", "AQ_PROCESS_NAMES")
        except TmuxCommandError:
            return ()
        value = _parse_environment_value(out, "AQ_PROCESS_NAMES")
        if not value:
            return ()
        return tuple(part for part in value.split(",") if part)

    async def _ready_prefix_hint(self, name: str) -> str:
        """The spec's ``ready_prompt_prefix``, recovered from the session env.

        Stored at start (like ``AQ_PROCESS_NAMES``) so nudges after a daemon
        restart still know where the harness's input line is.  Empty for
        sessions started before this key existed — the submit check then
        falls back to the whole-tail marker scan.
        """
        try:
            out = await self._tmux("show-environment", "-t", f"={name}", "AQ_READY_PREFIX")
        except TmuxCommandError:
            return ""
        return _parse_environment_value(out, "AQ_READY_PREFIX") or ""

    async def _skip_escape(self, name: str) -> bool:
        """Whether the session's spec said to skip Escape before Enter.

        Stored on the session env at start so the answer survives a daemon
        restart (the spec object does not).  Absent marker → skip (safe
        default: a stray Escape clears some harnesses' input line).
        """
        try:
            out = await self._tmux("show-environment", "-t", f"={name}", "AQ_SKIP_ESCAPE")
        except TmuxCommandError:
            return True
        value = _parse_environment_value(out, "AQ_SKIP_ESCAPE")
        return value != "0"


_SGR = re.compile(r"\x1b\[[0-9;:]*m")
_CODEX_PLACEHOLDER = "Ask Codex to do anything"


def _composer_is_empty(
    screen: str, prompt_prefix: str, cursor_x: int, cursor_y: int, height: int
) -> bool:
    """Recognize an empty *current* input, never a prompt in scrollback.

    Cursor position alone is insufficient: Home can leave text after the
    cursor, and a multiline draft can begin with a blank line. Only accept
    a blank composer with no continuation, both borders, or Codex's
    actual dim placeholder. Unrecognized layouts defer without sending keys.
    """
    raw_lines = screen.splitlines()
    if len(raw_lines) != height or not 0 <= cursor_y < len(raw_lines):
        return False
    lines = [_normalize(_SGR.sub("", line)) for line in raw_lines]
    line = lines[cursor_y]
    prefix = _normalize(prompt_prefix)
    indent = len(line) - len(line.lstrip(" "))
    if not prefix.strip() or not line[indent:].startswith(prefix):
        return False
    input_start = indent + len(prefix)
    if cursor_x != input_start:
        return False
    suffix = line[input_start:]
    below = lines[cursor_y + 1 :]
    if suffix:
        # A literal draft with these words must NOT be mistaken for the
        # placeholder. Its verified dim styling is part of the contract.
        placeholder = (
            prefix == "› "
            and suffix == _CODEX_PLACEHOLDER
            and f"\x1b[2m{_CODEX_PLACEHOLDER}" in raw_lines[cursor_y]
        )
        # Codex can leave blank screen rows below its status footer after
        # resizing. Accept padding only; extra content still fails closed.
        return (
            placeholder
            and len(below) >= 2
            and not below[0].strip()
            and all(not row.strip() for row in below[2:])
        )
    # Continuation lines can contain a pasted prompt glyph. An indented
    # one must never be mistaken for the start of an empty composer.
    if indent:
        return False
    if prefix == "❯ " and cursor_y > 0 and below:
        borders = (lines[cursor_y - 1].strip(), below[0].strip())
        if all(len(border) >= 8 and set(border) <= {"─", "━"} for border in borders):
            return True
    # Without a known placeholder or both Claude borders, earlier prompt
    # rows make the input boundary ambiguous for every harness.
    if any(row.lstrip().startswith(prefix) for row in lines[:cursor_y]):
        return False
    return all(not row.strip() for row in below)


def _submit_pending(tail: str, marker: str, prompt_prefix: str) -> bool:
    """True while the pasted text still sits in the harness's input line.

    TUI harnesses echo the submitted prompt into the transcript (Claude
    repaints it as ``❯ <text>``), so the marker being *somewhere* on
    screen does not mean the submit failed.  What distinguishes the two
    states is position: the input line is the **last** prompt-prefixed
    line in the pane (harnesses repaint it at the bottom), and unsubmitted
    text lives at or after it, while a transcript echo lives above it.

    With no ``prompt_prefix`` hint (sessions started by an older daemon),
    fall back to the historical whole-tail scan — a false "pending" there
    only costs a retry Enter, whereas a false "submitted" silently drops
    the nudge.
    """
    if not marker:
        return False
    tail = _normalize(tail)
    if marker not in tail:
        return False
    prefix = _normalize(prompt_prefix).strip()
    if not prefix:
        return True
    lines = tail.splitlines()
    last_prompt = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith(prefix):
            last_prompt = i
    if last_prompt is None:
        # Input line not visible (e.g. long wrapped paste pushed it out of
        # the captured window) — treat a visible marker as still pending.
        return True
    return any(marker in line for line in lines[last_prompt:])


_SAFE_META_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_environment_value(output: str, key: str) -> str | None:
    """Parse ``show-environment`` output: ``KEY=value`` or ``-KEY`` (unset)."""
    for line in output.splitlines():
        if line.startswith(f"{key}="):
            return line[len(key) + 1 :]
        if line == f"-{key}":
            return None
    return None
