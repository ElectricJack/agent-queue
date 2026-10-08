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
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

if os.name != "posix":  # pragma: no cover - the registry's gate
    raise ImportError("the tmux session provider requires a POSIX host")

from src.sessions import proctable
from src.sessions.dialogs import DialogBudget, first_match, run_dialog_dismissal
from src.sessions.provider import (
    Cap,
    DialogRule,
    NotSubmitted,
    NudgeDeferred,
    NudgeReason,
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

__all__ = ["TmuxCommandError", "TmuxProvider"]

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

#: Poll schedule for the submit confirmation, one tuple of sleeps per Enter
#: attempt.  The first attempt is the fast path an idle harness always takes
#: (~0.6 s); the later ones widen because an ink composer under a repaint
#: storm — a dashboard terminal attaching or detaching resizes the pane —
#: can take the better part of a second to redraw the input line, and a
#: submit judged "unconfirmed" there is a nudge lost for good.
_SUBMIT_POLLS: tuple[tuple[float, ...], ...] = (
    (0.15, 0.15, 0.15, 0.15),
    (0.15, 0.25, 0.5),
    (0.25, 0.5, 1.0),
    (0.5, 1.0, 2.0),
)

#: How many clear batches may leave the composer unchanged before the clear
#: gives up (``TmuxProvider._clear_composer``).  Claude's ``C-u`` kills one
#: visual row, or the newline under an empty row, so a wrapped or multi-line
#: injection takes one press per row; a key that changes nothing is a TUI
#: that ignores it.
_CLEAR_ATTEMPTS = 3
#: Pause after each clear batch before the composer is read again.
_CLEAR_SETTLE_SECONDS = 0.15
#: Most clear-key presses one clear may send.
_CLEAR_MAX_PRESSES = 200

#: Claude Code's stand-in for input it treats as a paste (measured on
#: 2.1.286, 2026-10-01): ``[Pasted text #3]`` or ``[Pasted text #3 +6 lines]``.
#: ``#N`` counts the pastes of one Claude process, so a later paste never
#: reuses AQ's token; ``+M`` is the paste's newline count.  A typed burst is
#: collapsed when it is longer than :data:`_PASTE_COLLAPSE_CHARS`.
_PASTE_PLACEHOLDER = re.compile(r"\[Pasted text #\d+(?: \+(\d+) lines?)?\]")
_PASTE_COLLAPSE_CHARS = 800

#: Landed check (``TmuxProvider._await_landed``): typed text must render on
#: the input line before Enter.  OpenCode renders keystroke by keystroke,
#: ~2 s for a 330-character stall reminder (measured on 1.18.32), so the
#: former fixed ~1.2 s window read every OpenCode nudge as swallowed.  The
#: wait now ends after this many polls on an unchanged screen, or at the cap.
_LANDED_POLL_SECONDS = 0.15
_LANDED_QUIET_POLLS = 7
_LANDED_MAX_SECONDS = 6.0

#: A detached pane is force-repainted at most this often (seconds) when its
#: composer shows text, and re-read on this schedule afterwards; OpenCode
#: redraws within a second (see ``TmuxProvider._repaint_stray_output``).
_REPAINT_MIN_INTERVAL = 60.0
_REPAINT_SETTLE_POLLS: tuple[float, ...] = (0.25, 0.5, 0.75)

#: Characters of composer text reported by ``composer_probe``.
_PREVIEW_CHARS = 200

_META_TOKEN_KEY = "AQ_INSTANCE_TOKEN"
_PENDING_SUBMIT_KEY = "AQ_PENDING_SUBMIT"
_PENDING_SUBMIT_VERSION = 1


def _normalize(text: str) -> str:
    return text.replace(_NBSP, " ")


@dataclass(frozen=True)
class _PendingSubmit:
    """One AQ injection which may still be in a session's composer.

    The record is written to tmux's session environment before the text is
    injected.  That makes the narrow "typed, then daemon died before Enter
    was confirmed" window recoverable, while the token and exact text keep a
    recycled session or an edited draft from being submitted.

    ``placeholder`` is the paste stand-in (``[Pasted text #N +M lines]``) the
    composer showed instead of this text, recorded the moment AQ saw its own
    typing collapse.  It is what attributes that stand-in to AQ after a
    restart; an older daemon ignores the extra field.
    """

    instance_token: str
    marker: str
    text: str
    placeholder: str = ""

    def encode(self) -> str:
        payload = {
            "v": _PENDING_SUBMIT_VERSION,
            "instance_token": self.instance_token,
            "marker": self.marker,
            "text": self.text,
            "text_sha256": hashlib.sha256(self.text.encode()).hexdigest(),
        }
        if self.placeholder:
            payload["placeholder"] = self.placeholder
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def decode(cls, value: str | None) -> _PendingSubmit | None:
        if not value:
            return None
        try:
            raw = base64.urlsafe_b64decode(value.encode())
            payload = json.loads(raw)
            instance_token = payload["instance_token"]
            marker = payload["marker"]
            text = payload["text"]
            digest = payload["text_sha256"]
            placeholder = payload.get("placeholder", "")
        except (AttributeError, binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError):
            return None
        if (
            payload.get("v") != _PENDING_SUBMIT_VERSION
            or not all(
                isinstance(value, str)
                for value in (instance_token, marker, text, digest, placeholder)
            )
            or marker != _marker_for(text)
            or digest != hashlib.sha256(text.encode()).hexdigest()
            or (placeholder and _paste_placeholder(placeholder) != placeholder)
        ):
            return None
        return cls(
            instance_token=instance_token, marker=marker, text=text, placeholder=placeholder
        )


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
    #: Exposed on doctor output without revealing the injected prompt text.
    pending_submit_evidence: ClassVar[str] = "durable_tmux_session_environment"
    capabilities: ClassVar[frozenset[Cap]] = frozenset(
        {Cap.ATTACH, Cap.PEEK, Cap.NUDGE, Cap.ACTIVITY, Cap.RELAUNCH, Cap.INPUT}
    )

    def __init__(self, config=None):
        self.config = config
        sessions_cfg = getattr(config, "sessions", None)
        self.socket: str = getattr(sessions_cfg, "tmux_socket", None) or "aq"
        self.nudge_debounce_ms: int = getattr(sessions_cfg, "nudge_debounce_ms", 500)
        self.dialog_budget_seconds: float = getattr(sessions_cfg, "dialog_budget_seconds", 8)
        #: How long the pane must stay dialog-free before startup accepts
        #: it.  Both Claude and Codex paint their trust screen *after* the
        #: first frames, so a zero-length window declares a blocked
        #: session ready.
        self.dialog_settle_seconds: float = getattr(sessions_cfg, "dialog_settle_seconds", 1.5)
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
        #: In-process cache of the durable tmux-environment records.  The
        #: cache avoids an environment lookup on each submit poll; the
        #: environment is authoritative after a daemon restart.
        self._unsubmitted: dict[str, _PendingSubmit] = {}
        #: Monotonic time of the last forced repaint per session name.
        self._last_repaint_at: dict[str, float] = {}

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
        except TimeoutError:
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
        # A new tmux session may reuse an old derived name. Its environment
        # is fresh and its instance token is different, so never let this
        # provider object's cache carry a predecessor's pending injection.
        self._unsubmitted.pop(spec.session_name, None)

        # Persist the nudge quirks on the *session* environment: the tmux
        # server owns it, so both survive a daemon restart (the spec
        # object does not).
        for key, value in (
            ("AQ_SKIP_ESCAPE", "1" if spec.skip_escape_before_enter else "0"),
            ("AQ_PROCESS_NAMES", ",".join(spec.process_names)),
            ("AQ_READY_PREFIX", spec.ready_prompt_prefix or ""),
            ("AQ_CLEAR_KEYS", ",".join(spec.composer_clear_keys)),
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
                # Readiness is about the current viewport. Codex retains
                # dismissed startup menus in scrollback; matching that history
                # can quarantine or kill a session already doing useful work.
                return await self._tmux("capture-pane", "-p", "-t", target)
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

        async def die(detail: str, *, dialog: DialogRule | None = None) -> None:
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
                dialog=dialog.name if dialog is not None else None,
                signal=dialog.signal if dialog is not None else None,
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

        # Phase 2+3: dialogs may cover the prompt — dismiss before waiting
        # for it, and again after (§3.2 "interleave").  Both halves run in
        # one loop because the two are not independent: a dialog glyph line
        # (Codex's "› 1. Yes, continue", Claude's "❯ No, exit") starts with
        # the very prefix the readiness poll looks for, so readiness is only
        # believable on a capture where *no* declared dialog is on screen.
        # And because the trust screen is painted late, the last pass holds
        # its "quiet" verdict open for ``dialog_settle_seconds`` and, if a
        # dialog does turn up in that window, goes back to waiting for the
        # composer.  Both clocks are bounded, so this terminates.
        settle = max(0.0, float(self.dialog_settle_seconds))
        # Only a harness that actually declares dialogs earns the extra
        # dialog-budget headroom; a dialog-free spec keeps §3.2's deadline.
        ready_deadline = (
            max(deadline, time.monotonic() + dialog_budget.remaining())
            if spec.dialogs
            else deadline
        )

        async def dismiss(quiet_seconds: float = 0.0):
            outcome = await run_dialog_dismissal(
                capture=capture,
                send_keys=send,
                dialogs=spec.dialogs,
                budget=dialog_budget,
                fired=fired,
                quiet_seconds=quiet_seconds,
            )
            if outcome.quarantined is not None:
                await die(
                    f"quarantine dialog {outcome.quarantined.name!r} matched during startup",
                    dialog=outcome.quarantined,
                )
            return outcome

        await dismiss()

        prefix = _normalize(spec.ready_prompt_prefix) if spec.ready_prompt_prefix else ""
        if not prefix and spec.ready_delay_ms:
            await asyncio.sleep(min(spec.ready_delay_ms, budget_ms) / 1000.0)
            if await pane_dead():
                await die("process died inside the ready delay")

        while True:
            if prefix:
                while time.monotonic() <= ready_deadline:
                    if await pane_dead():
                        await die("process died while waiting for the ready prompt")
                    raw = await capture()
                    if first_match(spec.dialogs, raw) is not None:
                        # A dialog is up: answer it rather than mistaking one
                        # of its menu rows for the composer.
                        if (await dismiss()).budget_exhausted:
                            break
                        # A once-only answer may not have cleared the menu
                        # yet. Keep polling without resending or busy-spinning.
                        await asyncio.sleep(0.2)
                        continue
                    text = _normalize(raw)
                    if any(line.lstrip().startswith(prefix) for line in text.splitlines()):
                        break
                    await asyncio.sleep(0.2)
                # Timeout: non-fatal with a live pane.

            outcome = await dismiss(quiet_seconds=settle)
            remaining_dialog = first_match(spec.dialogs, await capture()) if spec.dialogs else None
            if remaining_dialog is not None:
                await die(f"startup dialog {remaining_dialog.name!r} remains unresolved")
            if not outcome.fired or outcome.budget_exhausted:
                break
            if not prefix or dialog_budget.exhausted():
                break
            # A late dialog was answered — the composer has not been seen
            # since, so look for it again on the remaining budget.
            ready_deadline = max(ready_deadline, time.monotonic() + dialog_budget.remaining())

    async def stop(self, h: SessionHandle, *, grace: float = 2.0) -> None:
        if await self._fenced(h):
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
        # Whatever the session left behind outside the pane's tree: a
        # detached Bash-tool shell whose harness already exited (a drained
        # pool worker) is parented to init, so neither kill_tree nor the
        # kill-session above reaches its ``aq test`` run.  Swept even when
        # the fence missed — the session may be gone while its leftovers
        # are not — and a same-named successor carries a different token.
        if h.instance_token:
            await proctable.kill_marked(h.instance_token, grace=grace)

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

    async def confirm_stopped(self, h: SessionHandle) -> bool:
        # Bypass both pane and token caches. A failed list (including an
        # inaccessible socket) propagates; it must never mean "safe to retry".
        # A same-named successor also withholds recovery conservatively.
        names = await self._tmux("list-sessions", "-F", "#{session_name}")
        return h.name not in names.splitlines()

    async def confirm_instance_stopped(self, h: SessionHandle) -> bool:
        """Fresh proof that this token no longer owns the tmux name."""
        names = await self._tmux("list-sessions", "-F", "#{session_name}")
        if h.name not in names.splitlines():
            return True
        if not h.instance_token:
            return False
        try:
            observed = await self._tmux("show-environment", "-t", f"={h.name}", _META_TOKEN_KEY)
        except TmuxCommandError:
            return False
        token = _parse_environment_value(observed, _META_TOKEN_KEY)
        return token is not None and token != h.instance_token

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
                "show-environment",
                "-t",
                f"={h.name}",
                _META_TOKEN_KEY,
            )
            if _parse_environment_value(observed, _META_TOKEN_KEY) != h.instance_token:
                raise SessionError("Terminal session instance changed")

            # Unpark copy mode before sending input.
            with contextlib.suppress(TmuxCommandError):
                in_mode = (
                    await self._tmux(
                        "display-message",
                        "-p",
                        "-t",
                        pane,
                        "#{pane_in_mode}",
                    )
                ).strip()
                if in_mode == "1":
                    await self._tmux("send-keys", "-t", pane, "-X", "cancel")
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
                raise NudgeDeferred(
                    f"terminal {h.name!r} has recent manual input",
                    session_name=h.name,
                    reason=NudgeReason.RECENT_INPUT,
                )
            if not await self._fenced(h):
                raise NotSubmitted(f"session {h.name!r} is gone", session_name=h.name)

            # Debounce successive nudges (input collision pitfall).
            debounce = self.nudge_debounce_ms / 1000.0
            elapsed = time.monotonic() - self._last_nudge_at.get(h.name, 0.0)
            if elapsed < debounce:
                await asyncio.sleep(debounce - elapsed)

            spec_names = await self._process_names_hint(h.name)
            pane = await self._find_agent_pane(h.name, spec_names)
            if pane is None:
                raise NotSubmitted(f"no live pane found for {h.name!r}", session_name=h.name)

            prefix = await self._ready_prefix_hint(h.name)
            # Record pre-send activity for poke discounting.
            before = await self._raw_activity(h.name)

            # Resubmit only an exact, durable AQ injection.  A marker by
            # itself is deliberately insufficient: a human can edit a draft
            # while preserving the last line that supplied the marker.
            pending = await self._pending_record(h)
            if pending is not None:
                view = await self._pending_view(pane, prefix, pending)
                if view == "exact":
                    self._last_nudge_at[h.name] = time.monotonic()
                    self._poke[h.name] = (time.time(), before)
                    await self._submit(h, pane, prefix, pending, before)
                    if pending.text == text:
                        return
                    # A different AQ injection is still AQ's own text: submit
                    # it rather than defer on it.  Deferring deadlocked every
                    # later wake — the stall reminder names its idle minutes,
                    # so no retry ever matched the text left in the composer.
                    # This nudge is typed next, into the now-empty composer.
                elif view == "absent":
                    await self._forget_pending(h)
                elif view in ("collapsed", "windowed"):
                    # AQ's own text, shown in a form no submit can confirm
                    # (2026-10-01: a task comment as ``[Pasted text #1 +6
                    # lines]`` held back every later wake for 20 minutes).
                    # Clear it -- never Enter -- and type this nudge into the
                    # emptied composer.  Its sender never saw a delivery, so
                    # it retries; nothing is acknowledged or handled twice.
                    if not await self._clear_composer(h, pane, prefix, pending):
                        raise NudgeDeferred(
                            f"terminal {h.name!r} holds AQ input shown {view} "
                            "that could not be cleared",
                            session_name=h.name,
                            reason=NudgeReason.UNREADABLE,
                        )
                    await self._forget_pending(h)
                    logger.warning(
                        "Cleared unsubmitted AQ input from %s: the composer showed it %s, "
                        "so it was never delivered and its sender will retry",
                        h.name,
                        view,
                    )
                else:
                    # No safe observation means no key press and no record
                    # removal. A later doctor/reconciler pass can recover it.
                    # AQ's own text cannot be attributed to a human here, so
                    # the refusal is unreadable rather than a draft.
                    raise NudgeDeferred(
                        f"terminal {h.name!r} input is unknown",
                        session_name=h.name,
                        reason=NudgeReason.UNREADABLE,
                    )

            # Never append a reminder to a user's draft or compete with an
            # attached terminal. This guard shares send_input's lock and runs
            # before any key or paste (including copy-mode cancel).
            await self._require_empty_composer(h.name, pane, prefix)

            # A repaint or a newly attached client can invalidate the first
            # observation. Recheck immediately before writing the reminder.
            await self._require_empty_composer(h.name, pane, prefix)
            pending = _PendingSubmit(
                instance_token=h.instance_token,
                marker=_marker_for(text),
                text=text,
            )
            # Store before sending any characters. If the daemon dies after
            # the paste lands but before Enter is confirmed, a fresh provider
            # can recover exactly this injection from tmux's session env.
            await self._remember_pending(h, pending)
            payload = text.encode("utf-8")
            if len(payload) <= _SEND_KEYS_MAX_BYTES:
                await self._tmux("send-keys", "-t", pane, "-l", "--", text)
            else:
                await self._tmux("load-buffer", "-b", "aq-nudge", "-", stdin=payload)
                await self._tmux("paste-buffer", "-p", "-d", "-b", "aq-nudge", "-t", pane)

            self._last_nudge_at[h.name] = time.monotonic()
            self._poke[h.name] = (time.time(), before)

            # Landed check (live-test regression): a TUI mid-turn swallows
            # typed keys entirely — the text never reaches the input line,
            # yet the absence-of-marker confirm below would read that as
            # "submitted" and the message would be marked delivered unseen.
            # Require the marker to render before pressing Enter.  Paste-
            # buffer pastes are exempt: harnesses collapse large pastes to
            # a placeholder, so the marker legitimately never renders.
            if (
                pending.marker
                and len(payload) <= _SEND_KEYS_MAX_BYTES
                and not await self._await_landed(pane, prefix, pending.marker)
            ):
                reason = f"typed text never rendered in {h.name!r}"
                # Claude collapses a long burst into a paste stand-in, which
                # never shows the marker: clear that rather than strand it.
                await self._settle_unverifiable(h, pane, prefix, pending, reason, fresh=True)
                raise NotSubmitted(reason, session_name=h.name)

            # Per-harness Escape semantics (§9): only when the harness says
            # it is safe — grok clears the input line, codex backtracks.
            if not await self._skip_escape(h.name):
                await self._tmux("send-keys", "-t", pane, "Escape")
                await asyncio.sleep(0.05)

            await self._submit(h, pane, prefix, pending, before, fresh=True)

    async def _await_landed(self, pane: str, prefix: str, marker: str) -> bool:
        """Whether typed text rendered on the input line (the landed check).

        A screen that stops changing without the marker is a swallowed
        input, given up after :data:`_LANDED_QUIET_POLLS` unchanged polls as
        before.  A screen still changing is a composer still rendering, and
        is waited for up to :data:`_LANDED_MAX_SECONDS`.
        """
        deadline = time.monotonic() + _LANDED_MAX_SECONDS
        previous: str | None = None
        quiet = 0
        while True:
            tail = await self._capture_tail(pane, lines=40)
            # Composers wrap long input onto rows of their own, so the
            # marker is looked for on the input line with the wrap's
            # whitespace ignored (see :func:`_submit_pending`).
            if _submit_pending(tail, marker, prefix):
                return True
            quiet = quiet + 1 if tail == previous else 0
            previous = tail
            if quiet >= _LANDED_QUIET_POLLS or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_LANDED_POLL_SECONDS)

    async def _pending_composer_state(
        self, pane: str, prefix: str, pending: _PendingSubmit
    ) -> bool | None:
        """Whether the composer holds *pending* verbatim (or is safely observed clear).

        ``True`` only for the exact AQ text, ``False`` when a recognizable
        composer no longer contains it (submitted or deleted), ``None`` for
        everything else -- see :meth:`_pending_view`.
        """
        view = await self._pending_view(pane, prefix, pending)
        return True if view == "exact" else False if view == "absent" else None

    async def _pending_view(
        self, pane: str, prefix: str, pending: _PendingSubmit
    ) -> str | None:
        """How the composer shows *pending*: :func:`_injection_view`, or ``"absent"``.

        ``"exact"`` is deliberately stricter than the old marker-only test:
        both the marker and the complete persisted AQ text must remain after
        the last visible prompt.  ``"absent"`` means a recognizable composer
        no longer contains it (submitted or deleted).  ``"collapsed"`` and
        ``"windowed"`` are AQ text the composer will not show verbatim: never
        submitted, only cleared.  ``None`` is unknown and must never trigger a
        key or erase recovery evidence.
        """
        if not pending.marker or not _normalize(prefix).strip():
            return None
        try:
            in_mode = (
                await self._tmux(
                    "display-message",
                    "-p",
                    "-t",
                    pane,
                    "#{pane_in_mode}",
                )
            ).strip()
        except TmuxCommandError:
            return None
        if in_mode == "1":
            return None
        # ``-J`` joins soft terminal wraps while retaining explicit newlines,
        # allowing an exact injected multi-line payload comparison instead
        # of mistaking a narrow pane's wrapping for an edited composer.
        # OpenCode paints every box row itself, bar included, so it has no
        # soft wrap to join and ``-J`` would glue a full-width row onto the
        # next one.
        tail = await self._capture_tail(
            pane, lines=40, join_wrapped=not _is_opencode_prefix(prefix)
        )
        if not tail:
            return None
        prefix_text = _normalize(prefix).strip()
        lines = _normalize(tail).splitlines()
        if _is_opencode_prefix(prefix):
            typed = _opencode_input(lines)
            if typed is None or re.search(r"\[Pasted\b[^\]]*\]", typed, re.IGNORECASE):
                return None
            if _squash(pending.marker) not in _squash(typed):
                return "absent"
            # The box holds the input and nothing else, so its whole content
            # is the draft: identical to the injection, or not ours to submit.
            return "exact" if _squash(typed) == _squash(pending.text) else None
        last_prompt = next(
            (
                index
                for index in range(len(lines) - 1, -1, -1)
                if lines[index].lstrip().startswith(prefix_text)
            ),
            None,
        )
        if last_prompt is None:
            return None
        input_text = "\n".join(lines[last_prompt:])
        # Truncated prompts and unknown layouts fail closed (``None``).
        content = _input_after_prompt(lines, last_prompt, prefix)
        if re.search(r"\[Pasted (?:Content|text)[^\]]*\]", input_text, re.IGNORECASE):
            # A collapsed paste proves neither delivery nor exact draft
            # identity.  The one AQ can attribute is cleared, never submitted;
            # any other keeps its recovery evidence and gets no key.
            if content is not None and _injection_view(content, pending) == "collapsed":
                return "collapsed"
            return None
        if _squash(pending.marker) not in _squash(input_text):
            return "absent"
        if content is None:
            return None
        # The composer wraps the text itself, onto indented rows, so identity
        # is judged character for character with whitespace ignored.  A busy
        # or changed footer, or a draft edit, prevents exact attribution:
        # keep the evidence without pressing keys; a later idle capture may
        # become fully observable again.
        view = _injection_view(content, pending)
        return view if view in ("exact", "windowed") else None

    async def _submit(
        self,
        h: SessionHandle,
        pane: str,
        prefix: str,
        pending: _PendingSubmit,
        before: float | None,
        *,
        fresh: bool = False,
    ) -> None:
        """Press Enter until the typed text leaves the input line.

        Confirmation is anchored to the last prompt-prefixed line rather
        than "marker anywhere on screen": harnesses echo the submitted
        prompt into the transcript (Claude repaints it as ``❯ <text>``), so
        a whole-screen scan reads every *successful* submit as a failure
        (see :func:`_submit_pending`).

        Enter races the composer's repaint, and the backoff widens across
        attempts because the race is not uniform — an ink composer being
        repainted by an attaching or detaching dashboard terminal can lag
        several hundred milliseconds.  When even that fails the text must
        not simply be abandoned in the composer: it would block every later
        nudge on :meth:`_require_empty_composer` and the stall ladder would
        stop climbing.  So the marker is remembered (the next nudge for the
        same text resubmits it instead of deferring) and, if the harness
        declares a clear sequence, the composer is emptied.
        """
        for attempt, polls in enumerate(_SUBMIT_POLLS):
            if await self._pending_composer_state(pane, prefix, pending) is not True:
                reason = f"AQ injection changed or became unobservable in {h.name!r}"
                if attempt == 0:
                    # Before any Enter: AQ text the composer will not show
                    # verbatim is cleared rather than left to block delivery.
                    await self._settle_unverifiable(h, pane, prefix, pending, reason, fresh=fresh)
                raise NotSubmitted(reason, session_name=h.name, composer_dirty=True)
            await self._tmux("send-keys", "-t", pane, "Enter")
            for delay in polls:
                await asyncio.sleep(delay)
                tail = await self._capture_tail(pane, lines=40)
                if not _submit_pending(tail, pending.marker, prefix):
                    await self._forget_pending(h)
                    self._poke[h.name] = (time.time(), before)
                    return
        cleared = await self._clear_composer(h, pane, prefix, pending)
        if cleared:
            await self._forget_pending(h)
        raise NotSubmitted(
            f"submit unconfirmed for {h.name!r} after {len(_SUBMIT_POLLS)} attempts"
            + ("; composer cleared" if cleared else "; text left in composer"),
            session_name=h.name,
            composer_dirty=not cleared,
        )

    async def _settle_unverifiable(
        self,
        h: SessionHandle,
        pane: str,
        prefix: str,
        pending: _PendingSubmit,
        reason: str,
        *,
        fresh: bool,
    ) -> None:
        """Raise for AQ text the composer shows collapsed or windowed; else return.

        Such text can never be confirmed, so it is never submitted, and left in
        place it blocks every later nudge.  It is cleared, and
        :class:`NotSubmitted` leaves the caller's delivery pending: the agent
        never saw the text, so delivering it again is not a duplicate.

        *fresh* means AQ typed *pending* moments ago, under the nudge lock,
        into a composer verified empty: a lone paste stand-in with the
        injection's newline count is then AQ's own.  Its exact token is
        recorded durably before any key, so a restart mid-clear still
        attributes it (``#N`` is never reused by a later paste).
        """
        if fresh and not pending.placeholder:
            token = _paste_placeholder(
                _composer_input(await self._capture_tail(pane, lines=40), prefix) or ""
            )
            if token is not None and _placeholder_newlines(token) == pending.text.count("\n"):
                pending = replace(pending, placeholder=token)
                with contextlib.suppress(NotSubmitted):
                    await self._remember_pending(h, pending)
        view = await self._pending_view(pane, prefix, pending)
        if view not in ("collapsed", "windowed"):
            return
        if await self._clear_composer(h, pane, prefix, pending):
            await self._forget_pending(h)
            raise NotSubmitted(
                f"{reason}: the composer showed it {view}, so it was cleared unsubmitted",
                session_name=h.name,
            )
        raise NotSubmitted(
            f"{reason}: the composer shows it {view}; text left in composer",
            session_name=h.name,
            composer_dirty=True,
        )

    async def _input_unwatched(self, pane: str) -> bool:
        """No copy mode and no attached client: nobody is using this terminal."""
        try:
            out = await self._tmux(
                "display-message", "-p", "-t", pane, "#{pane_in_mode}\t#{session_attached}"
            )
            in_mode, attached = map(int, out.split())
        except (TmuxCommandError, ValueError):
            return False
        return in_mode == 0 and attached == 0

    async def _clear_composer(
        self, h: SessionHandle, pane: str, prefix: str, pending: _PendingSubmit
    ) -> bool:
        """Empty a composer still holding *our* unsubmitted text; True once empty.

        The first read must attribute the composer to *pending*
        (:func:`_injection_view`: verbatim, AQ's paste stand-in, or the last
        rows of a windowed injection).  The harness's ``composer_clear_keys``
        then go once per visible row, and every later read must still be a
        piece of the injection (:func:`_clear_remnant`), so a human who
        starts typing mid-clear stops it and keeps what is left.  Nothing is
        pressed while the pane is in copy mode or has a client attached.

        Only the declared keys are sent -- for Claude ``C-u``, which kills a
        visual row or a newline and does nothing on an empty composer -- never
        Ctrl-C or Escape, which interrupt a running turn.  A harness with no
        declared keys is left alone: guessing a key that means something else
        in that TUI is worse than leaving text the resubmit path can recover.
        """
        keys = await self._clear_keys_hint(h.name)
        if not keys or not pending.marker or not _normalize(prefix).strip():
            return False
        recent = self._poke.get(h.name)
        before = (
            recent[1]
            if recent is not None and time.time() - recent[0] < 10.0
            else await self._raw_activity(h.name)
        )
        previous: str | None = None
        unchanged = 0
        pressed = 0
        try:
            while True:
                if not await self._input_unwatched(pane):
                    return False
                content = _composer_input(
                    await self._capture_tail(
                        pane, lines=40, join_wrapped=not _is_opencode_prefix(prefix)
                    ),
                    prefix,
                )
                if content is None:
                    return False
                if not content.strip():
                    return True
                if previous is None:
                    ours = _injection_view(content, pending) is not None
                else:
                    ours = _clear_remnant(content, pending)
                    unchanged = unchanged + 1 if content == previous else 0
                if not ours:
                    # Possibly emptied after all (a placeholder hint the
                    # input read cannot tell from text); only the composer
                    # guard may say so.
                    refusal, _ = await self._composer_refusal(h.name, pane, prefix)
                    return previous is not None and refusal is None
                if unchanged >= _CLEAR_ATTEMPTS or pressed >= _CLEAR_MAX_PRESSES:
                    return False
                # One press per visible row, then read again.  Dashboard input
                # shares this lock; a client attaching mid-batch stops it.
                batch = min(len(content.splitlines()) or 1, _CLEAR_MAX_PRESSES - pressed)
                for index in range(batch):
                    if index and not await self._input_unwatched(pane):
                        return False
                    for key in keys:
                        await self._tmux("send-keys", "-t", pane, key)
                    pressed += 1
                previous = content
                await asyncio.sleep(_CLEAR_SETTLE_SECONDS)
        except TmuxCommandError:
            return False
        finally:
            if pressed:
                # Our own keystrokes are not agent progress.
                self._poke[h.name] = (time.time(), before)

    # -- stuck-composer recovery (doctor: ``sessions.stuck_composer``) ------

    async def _pending_record(self, h: SessionHandle) -> _PendingSubmit | None:
        """Load this exact instance's persisted pending-injection record."""
        cached = self._unsubmitted.get(h.name)
        if cached is not None:
            return cached if cached.instance_token == h.instance_token else None
        try:
            value = await self._tmux("show-environment", "-t", f"={h.name}", _PENDING_SUBMIT_KEY)
        except TmuxCommandError:
            return None
        pending = _PendingSubmit.decode(_parse_environment_value(value, _PENDING_SUBMIT_KEY))
        if pending is None or pending.instance_token != h.instance_token:
            return None
        self._unsubmitted[h.name] = pending
        return pending

    async def _remember_pending(self, h: SessionHandle, pending: _PendingSubmit) -> None:
        """Persist before typing, or leave the composer untouched."""
        if pending.instance_token != h.instance_token:
            raise NotSubmitted(
                f"pending injection token mismatch for {h.name!r}", session_name=h.name
            )
        try:
            await self._tmux(
                "set-environment", "-t", f"={h.name}", _PENDING_SUBMIT_KEY, pending.encode()
            )
        except TmuxCommandError as exc:
            raise NotSubmitted(
                f"could not persist AQ injection for {h.name!r}", session_name=h.name
            ) from exc
        self._unsubmitted[h.name] = pending

    async def _forget_pending(self, h: SessionHandle) -> None:
        """Clear cache and durable evidence once the AQ text is gone."""
        self._unsubmitted.pop(h.name, None)
        if not await self._fenced(h):
            return
        with contextlib.suppress(TmuxCommandError):
            await self._tmux("set-environment", "-u", "-t", f"={h.name}", _PENDING_SUBMIT_KEY)

    async def pending_submit(self, h: SessionHandle) -> str | None:
        """The marker of a nudge still sitting unsubmitted in the composer.

        ``None`` when nothing is known to be stuck *or* when the composer no
        longer shows it (the agent submitted or deleted it in the meantime),
        in which case the record is dropped.  Read-only: it never presses a
        key, so ``aq doctor`` without ``--fix`` cannot disturb a session.
        """
        detail = await self.pending_submit_detail(h)
        if detail is None or not detail["observable"]:
            return None
        return detail["marker"]

    async def pending_submit_detail(self, h: SessionHandle) -> dict | None:
        """:meth:`pending_submit`, plus the records the composer guard cannot read.

        ``{"marker": ..., "observable": True}`` is a nudge still sitting in
        the composer exactly as typed.  ``"observable": False`` is a durable
        record whose composer no screen parse can attribute (an unknown
        footer, a collapsed paste, copy mode): the text may well still be
        there, blocking every later wake, and no key is pressed on it.  That
        second state is the one an operator could not see on 2026-09-27,
        when Codex 0.157 layouts left reminders typed but never submitted.
        Read-only, like :meth:`pending_submit`.
        """
        if not await self._fenced(h):
            self._unsubmitted.pop(h.name, None)
            return None
        record = await self._pending_record(h)
        if record is None:
            return None
        pane = await self._find_agent_pane(h.name, await self._process_names_hint(h.name))
        if pane is None:
            return None
        prefix = await self._ready_prefix_hint(h.name)
        view = await self._pending_view(pane, prefix, record)
        if view == "absent":
            await self._forget_pending(h)
            return None
        return {
            "marker": record.marker,
            "observable": view == "exact",
            # AQ's own text the composer shows collapsed or windowed: never
            # submitted, but :meth:`clear_pending` (``--fix``) clears it.
            "clearable": view in ("collapsed", "windowed"),
        }

    async def resubmit_pending(self, h: SessionHandle) -> bool:
        """Press Enter on a stuck composer.  True when the text went in.

        The ``--fix`` half of ``sessions.stuck_composer``: exactly the
        manual ``tmux send-keys Enter`` an operator would run, gated on the
        composer still holding the marker this provider typed.
        """
        async with self._nudge_locks[h.name]:
            if not await self._fenced(h):
                return False
            record = await self._pending_record(h)
            if record is None:
                return False
            pane = await self._find_agent_pane(h.name, await self._process_names_hint(h.name))
            if pane is None:
                return False
            prefix = await self._ready_prefix_hint(h.name)
            state = await self._pending_composer_state(pane, prefix, record)
            if state is not True:
                if state is False:
                    await self._forget_pending(h)
                return False
            before = await self._raw_activity(h.name)
            try:
                await self._submit(h, pane, prefix, record, before)
            except NotSubmitted:
                return False
            return True

    async def clear_pending(self, h: SessionHandle) -> bool:
        """Clear AQ text the composer shows collapsed or windowed.  True once empty.

        The ``--fix`` half of ``sessions.stuck_composer`` for a ``clearable``
        record: the clear the next nudge would run, never Enter.  The message
        behind the text stays pending and is redelivered.  Refused while a
        human typed into the dashboard terminal within the last few seconds.
        """
        async with self._nudge_locks[h.name]:
            last_input = self._last_input_at.get(h.name)
            if (
                last_input is not None
                and time.monotonic() - last_input < _MANUAL_INPUT_QUIET_SECONDS
            ):
                return False
            if not await self._fenced(h):
                return False
            record = await self._pending_record(h)
            if record is None:
                return False
            pane = await self._find_agent_pane(h.name, await self._process_names_hint(h.name))
            if pane is None:
                return False
            prefix = await self._ready_prefix_hint(h.name)
            view = await self._pending_view(pane, prefix, record)
            if view == "absent":
                await self._forget_pending(h)
                return False
            if view not in ("collapsed", "windowed"):
                return False
            if not await self._clear_composer(h, pane, prefix, record):
                return False
            await self._forget_pending(h)
            logger.warning(
                "Cleared unsubmitted AQ input from %s (shown %s); its sender will retry",
                h.name,
                view,
            )
            return True

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

    async def _capture_tail(self, pane: str, lines: int = 5, *, join_wrapped: bool = False) -> str:
        # ``capture-pane -S -N`` starts N lines *above the visible screen*
        # and captures through the bottom — the whole screen plus history,
        # not a tail.  The nudge submit-confirm relies on genuinely seeing
        # only the input-box region: Claude echoes the submitted prompt
        # into the transcript, so a whole-screen capture still contains the
        # marker after a successful submit and every nudge reads as
        # NotSubmitted (observed live: envelope delivered twice, row never
        # marked).  Trim to the last N non-blank-padded lines here.
        try:
            args = ["capture-pane", "-p"]
            if join_wrapped:
                args.append("-J")
            args.extend(("-t", pane))
            out = await self._tmux(*args)
        except TmuxCommandError:
            return ""
        trimmed = out.rstrip("\n")
        return "\n".join(trimmed.splitlines()[-lines:])

    async def _require_empty_composer(self, name: str, pane: str, prefix: str) -> None:
        """Fail closed on drafts, active terminal clients, and unknown TUIs.

        The one repair attempted first is a repaint of an OpenCode box that
        shows text (:meth:`_repaint_stray_output`): a draft survives it, text
        a plugin painted over the input does not.
        """
        refusal, repaintable = await self._composer_refusal(name, pane, prefix)
        if refusal is not None and repaintable and await self._repaint_stray_output(name, pane):
            for delay in _REPAINT_SETTLE_POLLS:
                await asyncio.sleep(delay)
                refusal, _ = await self._composer_refusal(name, pane, prefix)
                if refusal is None:
                    break
        if refusal is not None:
            raise NudgeDeferred(
                refusal.message, session_name=name, reason=refusal.reason
            )

    async def _composer_refusal(
        self, name: str, pane: str, prefix: str
    ) -> tuple[_ComposerRefusal | None, bool]:
        """Why a nudge cannot be typed now (``None`` when it can), read-only.

        The flag says whether a repaint could change the answer: a detached,
        stable OpenCode pane whose box was read cleanly and still showed text.

        The *reason* is what the stall ladder branches on, and it is decided
        from the evidence in hand, never from the message text: a person at
        the keyboard is reported as one, and only text that provably cannot
        be typed input (a wedged TUI's residue, or a read that could not be
        taken consistently) is reported as unreadable.
        """
        fmt = (
            "#{cursor_x}\t#{cursor_y}\t#{pane_width}\t#{pane_height}\t"
            "#{cursor_flag}\t#{pane_in_mode}\t#{session_attached}"
        )
        try:
            before = await self._tmux("display-message", "-p", "-t", pane, fmt)
            x, y, width, height, visible, in_mode, attached = map(int, before.split())
            reasons = []
            if not prefix.strip():
                reasons.append("unknown prompt prefix")
            if in_mode != 0:
                reasons.append("pane in copy mode")
            if attached != 0:
                reasons.append("client attached")
            if not 0 <= x < width or not 0 <= y < height:
                reasons.append(f"cursor outside pane ({x},{y}; {width}x{height})")
            if reasons:
                # Copy mode, an attached client, or a layout with no known
                # input line: a person is at this terminal.
                return (
                    _ComposerRefusal(
                        f"terminal {name!r} is busy or its input is unknown: "
                        + "; ".join(reasons),
                        NudgeReason.TERMINAL_BUSY,
                    ),
                    False,
                )
            screen = await self._tmux("capture-pane", "-p", "-e", "-t", pane)
            after = await self._tmux("display-message", "-p", "-t", pane, fmt)
        except (TmuxCommandError, ValueError):
            return (
                _ComposerRefusal(
                    f"cannot inspect input for {name!r}", NudgeReason.UNREADABLE
                ),
                False,
            )
        if before != after:
            # The pane moved between the two reads, so the captured screen
            # describes no state that exists: unreadable, not a draft.
            return (
                _ComposerRefusal(
                    f"terminal {name!r} has a draft or its input is unknown",
                    NudgeReason.UNREADABLE,
                ),
                False,
            )
        if _composer_is_empty(screen, prefix, x, y, height, cursor_visible=visible == 1):
            return None, False
        reason = (
            _opencode_stale_frame(screen, x, y)
            if _is_opencode_prefix(prefix)
            else NudgeReason.DRAFT
        )
        return (
            _ComposerRefusal(
                f"terminal {name!r} has a draft or its input is unknown", reason
            ),
            _is_opencode_prefix(prefix),
        )

    async def _repaint_stray_output(self, name: str, pane: str) -> bool:
        """Make a detached pane redraw its whole screen; False when not attempted.

        OpenCode runs its plugins inside the TUI process, so a plugin that
        writes to stderr paints straight over the composer (fast-jev logged
        every compaction that way, 2026-09-26/27).  OpenCode's renderer only
        redraws cells it changed itself, so that text stays until something
        forces a full frame, and :meth:`_require_empty_composer` reads it as
        a draft on every stall and message nudge — the worker waits for a
        human.  Shrinking the window one row and restoring it is that full
        frame; a real draft survives it.  ``resize-pane`` cannot shrink a
        single-pane window and a bare SIGWINCH at an unchanged size is
        ignored.  ``resize-window`` switches the window to ``manual`` sizing,
        so the ``latest`` set at start is put back.

        At most once per :data:`_REPAINT_MIN_INTERVAL` per session, so a real
        draft in a detached pane is not resized on every delivery attempt.
        """
        now = time.monotonic()
        last = self._last_repaint_at.get(name)
        if last is not None and now - last < _REPAINT_MIN_INTERVAL:
            return False
        self._last_repaint_at[name] = now
        window = f"={name}:"
        try:
            height = int(
                (await self._tmux("display-message", "-p", "-t", pane, "#{window_height}")).strip()
            )
        except (TmuxCommandError, ValueError):
            return False
        if height < 2:
            return False
        try:
            await self._tmux("resize-window", "-t", window, "-y", str(height - 1))
            await self._tmux("resize-window", "-t", window, "-y", str(height))
        except TmuxCommandError:
            logger.debug("repaint of %s failed", name, exc_info=True)
            return False
        finally:
            with contextlib.suppress(TmuxCommandError):
                await self._tmux("set-option", "-w", "-t", window, "window-size", "latest")
        return True

    async def composer_probe(self, h: SessionHandle) -> dict | None:
        """Whether a nudge would be typed into *h* now, and what its input shows.

        ``{"ready": bool, "reason": str | None, "reason_kind": str | None,
        "input": str}``.  ``input`` is the visible composer text (for
        OpenCode, the whole box region), so an operator can tell a human
        draft from output painted over the input; ``reason_kind`` is the
        structured :class:`NudgeReason`, so the report can also say whether
        the stall ladder is allowed to advance on it.
        ``None`` when the session or its pane is gone.  Read-only: neither a
        repaint nor a key, so ``aq doctor`` cannot disturb a session.
        """
        if not await self._fenced(h):
            return None
        pane = await self._find_agent_pane(h.name, await self._process_names_hint(h.name))
        if pane is None:
            return None
        prefix = await self._ready_prefix_hint(h.name)
        refusal, _ = await self._composer_refusal(h.name, pane, prefix)
        tail = await self._capture_tail(pane, lines=40)
        return {
            "ready": refusal is None,
            "reason": None if refusal is None else refusal.message,
            "reason_kind": None if refusal is None else str(refusal.reason),
            "input": _composer_preview(tail, prefix)[:_PREVIEW_CHARS],
        }

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

    async def _clear_keys_hint(self, name: str) -> tuple[str, ...]:
        """The harness's ``composer_clear_keys``, recovered from the env.

        Stored at start like ``AQ_SKIP_ESCAPE`` so a session started before
        a daemon restart still knows how to clear its own composer.  Absent
        marker → no keys, which means "leave the text alone".
        """
        try:
            out = await self._tmux("show-environment", "-t", f"={name}", "AQ_CLEAR_KEYS")
        except TmuxCommandError:
            return ()
        value = _parse_environment_value(out, "AQ_CLEAR_KEYS") or ""
        return tuple(part for part in value.split(",") if part)

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
#: The cursor cell Claude paints itself while the terminal cursor is hidden.
_CLAUDE_DRAWN_CURSOR = "\x1b[7m \x1b[0m"
#: Codex's model/status row under the composer: ``gpt-5.6-sol high · /cwd``
#: (older builds) or ``GPT-6-Sol xhigh · ~/cwd · title`` (codex-cli 0.157,
#: which appends `` · ⠦`` while a turn runs).  Older builds show
#: ``NN% context left`` in that slot instead.
_CODEX_MODEL_ROW = re.compile(r"(?:gpt|o\d)[\w. -]* · .+", re.IGNORECASE)
_CODEX_CONTEXT_ROW = re.compile(r"\d+% context left")


def _strip_codex_footer(rows: list[str]) -> list[str] | None:
    """*rows* above Codex's status footer, or ``None`` when none is recognised.

    The footer is a blank separator, the model row, then at most one hint
    or notice row and terminal padding.  That last row's wording differs
    between builds and states (``? for shortcuts``, ``← for agents · ? for
    shortcuts``, a right-aligned ``⚠ 2 warnings · f2 to view``, painted
    over with spaces once the composer holds text), so it is bounded by
    shape rather than enumerated.  The search runs from the bottom: Codex
    paints its composer above the footer, so no input sits below the model
    row, while a draft line that merely looks like one sits above the real
    footer.  A busy footer (``tab to queue message    91% context left``)
    is not a model row and is not recognised.
    """
    for index in range(len(rows) - 1, 0, -1):
        status = rows[index].strip()
        if not (_CODEX_MODEL_ROW.fullmatch(status) or _CODEX_CONTEXT_ROW.fullmatch(status)):
            continue
        notices = [row for row in rows[index + 1 :] if row.strip()]
        if rows[index - 1].strip() or len(notices) > 1:
            return None
        return rows[: index - 1]
    return None


#: OpenCode's composer is a box, not a prompt line.  Its left edge is this
#: heavy vertical bar (U+2503), which the operator's OpenCode harness names
#: as its ``ready_prompt_prefix``; two cells of padding separate it from the
#: input.  Measured on OpenCode 1.18.32, idle in a session::
#:
#:       ┃                                  top padding
#:       ┃  <cursor>                        input rows (one per wrapped line)
#:       ┃                                  bottom padding
#:       ┃  Build · Qwen3.8 27B (local)     agent row
#:       ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀   bottom border
#:
#: The transcript draws its message blocks with the same bar at the same
#: indent, so the input is never "the last bar-prefixed line" (that is the
#: agent row): the box is found from its bottom border upwards.
_OPENCODE_BAR = "┃"
_OPENCODE_PAD = 2
_OPENCODE_BORDER = re.compile(r"╹▀+")
#: The agent row names an agent and a model (``Build · Qwen3.8 27B (local)``,
#: ``Build auto · …`` under ``--auto``).  Shell mode paints a bare ``Shell``
#: there and would run typed text as a command, so it never matches.
_OPENCODE_AGENT_ROW = re.compile(r"\S.* · \S.*")


@dataclass(frozen=True)
class _OpenCodeBox:
    """Row indices of the OpenCode composer box on one captured screen."""

    indent: int
    top: int
    agent: int

    def rows(self, lines: list[str]) -> list[str]:
        """The rows between the top padding and the agent row, bar removed."""
        return [line[self.indent + len(_OPENCODE_BAR) :] for line in lines[self.top : self.agent]]


@dataclass(frozen=True)
class _ComposerRefusal:
    """One composer refusal: the message, and the structured reason for it."""

    message: str
    reason: NudgeReason


def _is_opencode_prefix(prompt_prefix: str) -> bool:
    return _normalize(prompt_prefix).strip() == _OPENCODE_BAR


def _opencode_border(lines: list[str]) -> int | None:
    """Index of the bottom-most composer border row, or ``None``."""
    return next(
        (i for i in range(len(lines) - 1, -1, -1) if _OPENCODE_BORDER.fullmatch(lines[i].strip())),
        None,
    )


def _opencode_box(lines: list[str]) -> _OpenCodeBox | None:
    """The composer box above the last bottom border, or ``None``.

    Only the bottom-most border is considered; a malformed box there fails
    closed rather than matching a box further up the screen.
    """
    border = _opencode_border(lines)
    if not border:
        return None
    indent = len(lines[border]) - len(lines[border].lstrip(" "))
    bar = " " * indent + _OPENCODE_BAR
    agent = border - 1
    if not lines[agent].startswith(bar) or not _OPENCODE_AGENT_ROW.fullmatch(
        lines[agent][len(bar) :].strip()
    ):
        return None
    top = agent
    while top > 0 and lines[top - 1].startswith(bar):
        top -= 1
    return _OpenCodeBox(indent=indent, top=top, agent=agent) if top < agent else None


def _opencode_input(lines: list[str]) -> str | None:
    """Everything typed into the OpenCode composer, or ``None`` if none is seen."""
    box = _opencode_box(lines)
    if box is None:
        return None
    return "\n".join(box.rows(lines))


def _opencode_composer_is_empty(lines: list[str], cursor_x: int, cursor_y: int) -> bool:
    """The measured idle shape exactly: three blank rows, cursor on the middle one.

    A multi-line draft adds rows, a wrapped stray write breaks the bar, and
    plugin output painted over the input row is text — each fails closed.
    """
    box = _opencode_box(lines)
    if box is None or box.agent - box.top != 3 or cursor_y != box.top + 1:
        return False
    if cursor_x != box.indent + len(_OPENCODE_BAR) + _OPENCODE_PAD:
        return False
    return all(not row.strip() for row in box.rows(lines))


def _opencode_stale_frame(screen: str, cursor_x: int, cursor_y: int) -> NudgeReason:
    """Whether an OpenCode box at idle geometry holds residue instead of input.

    Only one shape is provably *not* typed input.  At the idle geometry the
    box has exactly three interior rows with the cursor parked on the middle
    one at the input position, and a draft can only be the text on that
    cursor row: a wrapped or multi-line draft grows the box
    (:func:`_opencode_composer_is_empty` requires exactly three rows for
    precisely that reason).  Text on either of the *other* interior rows is
    therefore residue from an earlier layout — the wrapped tail of a Todo
    line above, or of the agent row below — that a wedged TUI cannot repaint
    away, and no keyboard put it there.

    That was ``vivid-quest-44.3``: 116 identical refusals over 78 minutes on
    a pane whose box read exactly this way, holding its task with zero rungs,
    zero backoffs and zero ``task.stalled`` events.  Everything else that is
    not the idle shape stays :attr:`NudgeReason.DRAFT`: an unrecognized box is
    far more likely to be a layout this code has not measured than a human
    who is not there, and only proof buys an escalation.
    """
    lines = [_normalize(_SGR.sub("", line)) for line in screen.splitlines()]
    box = _opencode_box(lines)
    if box is None or box.agent - box.top != 3 or cursor_y != box.top + 1:
        return NudgeReason.DRAFT
    if cursor_x != box.indent + len(_OPENCODE_BAR) + _OPENCODE_PAD:
        return NudgeReason.DRAFT  # The cursor moved off the input row: typing.
    rows = box.rows(lines)
    return (
        NudgeReason.STALE_FRAME
        if any(row.strip() for row in (rows[0], rows[2]))
        else NudgeReason.DRAFT
    )


def _composer_preview(tail: str, prompt_prefix: str) -> str:
    """The composer's visible text on one line, for an operator to read.

    For OpenCode, every non-blank row of the region that ends at its bottom
    border, bar removed — including a stray write that broke the box, which
    is exactly what the operator needs to see.  Otherwise the last
    prompt-prefixed line.
    """
    lines = [_normalize(line) for line in tail.splitlines()]
    if _is_opencode_prefix(prompt_prefix):
        border = _opencode_border(lines)
        if border is None:
            return ""
        top = border
        while top > 0 and lines[top - 1].strip():
            top -= 1
        rows = (line.strip().removeprefix(_OPENCODE_BAR).strip() for line in lines[top:border])
        return " / ".join(row for row in rows if row)
    prefix = _normalize(prompt_prefix).strip()
    if not prefix:
        return ""
    return next(
        (line.strip() for line in reversed(lines) if line.lstrip().startswith(prefix)), ""
    )


def _squash(text: str) -> str:
    """*text* with every run of whitespace removed.

    Harness composers wrap long input themselves: Codex and Claude paint
    explicit rows with a two-space continuation indent, which
    ``capture-pane -J`` cannot join, and break at word boundaries.  Neither
    the rows nor a space-joined reading reproduce the typed text, but its
    visible characters, in order, survive the wrap unchanged.
    """
    return "".join(_normalize(text).split())


def _composer_is_empty(
    screen: str,
    prompt_prefix: str,
    cursor_x: int,
    cursor_y: int,
    height: int,
    *,
    cursor_visible: bool = True,
) -> bool:
    """Recognize an empty *current* input, never a prompt in scrollback.

    Cursor position alone is insufficient: Home can leave text after the
    cursor, and a multiline draft can begin with a blank line. Only accept
    a blank composer with no continuation, both borders, or Codex's
    actual dim placeholder. Unrecognized layouts defer without sending keys.

    A hidden cursor is accepted only between Claude's two borders.  Claude
    Code 2.1 can keep the terminal cursor hidden for a pane's whole life
    (observed on every pane of a freshly started tmux server) while still
    parking it at the input and painting its own cursor cell there, so the
    borders, not the cursor flag, identify its idle composer.  Every other
    layout still requires a visible cursor.

    OpenCode has no prompt line at all; its box is recognised whole
    (:func:`_opencode_composer_is_empty`).
    """
    raw_lines = screen.splitlines()
    if len(raw_lines) != height or not 0 <= cursor_y < len(raw_lines):
        return False
    lines = [_normalize(_SGR.sub("", line)) for line in raw_lines]
    if _is_opencode_prefix(prompt_prefix):
        return cursor_visible and _opencode_composer_is_empty(lines, cursor_x, cursor_y)
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
    if (
        not cursor_visible
        and prefix == "❯ "
        and suffix == " "
        and raw_lines[cursor_y].endswith(_CLAUDE_DRAWN_CURSOR)
    ):
        # With the terminal cursor hidden Claude paints its own: one
        # inverse-video blank at the input, which is not a draft.
        suffix = ""
    if suffix:
        # A literal draft with these words must NOT be mistaken for the
        # placeholder. Its verified dim styling is part of the contract.
        placeholder = (
            prefix == "› "
            and suffix == _CODEX_PLACEHOLDER
            and f"\x1b[2m{_CODEX_PLACEHOLDER}" in raw_lines[cursor_y]
        )
        # Codex can leave blank screen rows below its status footer after
        # resizing. Accept its footer (see _strip_codex_footer) or padding
        # only; extra content still fails closed.
        above_footer = _strip_codex_footer(below)
        return (
            placeholder
            and cursor_visible
            and bool(below)
            and not below[0].strip()
            and all(not row.strip() for row in (below if above_footer is None else above_footer))
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
    if not cursor_visible or any(row.lstrip().startswith(prefix) for row in lines[:cursor_y]):
        return False
    return all(not row.strip() for row in below)


def _marker_for(text: str) -> str:
    """The tail slice of *text* used to recognise it on the input line.

    The last non-blank line, last 48 characters: long enough to be unique
    against a harness's own chrome, short enough to survive the wrapping
    and truncation a composer applies to a multi-line nudge.
    """
    stripped = text.strip()
    return stripped.splitlines()[-1][-48:] if stripped else ""


def _paste_placeholder(content: str) -> str | None:
    """*content* when it is exactly one Claude paste stand-in, else ``None``."""
    token = content.strip()
    return token if _PASTE_PLACEHOLDER.fullmatch(token) else None


def _placeholder_newlines(token: str) -> int:
    match = _PASTE_PLACEHOLDER.fullmatch(token)
    return int(match.group(1) or 0) if match else -1


def _collapses(text: str) -> bool:
    """Whether Claude collapses *text* typed in one burst (JS string length)."""
    return len(text.encode("utf-16-le")) // 2 > _PASTE_COLLAPSE_CHARS


def _input_after_prompt(lines: list[str], last_prompt: int, prompt_prefix: str) -> str | None:
    """The composer's text, from the prompt line at *last_prompt* to its border.

    The known rendered prompt is stripped and the read stops at the composer
    border, so human text before or after an injection stays part of what
    is compared.  ``None`` when the prompt line does not start with the
    rendered prefix: an unknown layout fails closed.
    """
    rendered_prefix = _normalize(prompt_prefix).lstrip()
    prompt_line = lines[last_prompt].lstrip()
    if not prompt_line.startswith(rendered_prefix):
        return None
    content = [prompt_line[len(rendered_prefix) :]]
    for line in lines[last_prompt + 1 :]:
        border = line.strip()
        if len(border) >= 8 and set(border) <= {"─", "━"}:
            break
        content.append(line)
    if _normalize(prompt_prefix).strip() == "›":
        # Codex renders a blank separator and a status footer below input.
        # Remove only a recognisable footer; arbitrary text remains part of
        # the draft.
        above_footer = _strip_codex_footer(content)
        if above_footer is not None:
            content = above_footer
    return "\n".join(content)


def _composer_input(tail: str, prompt_prefix: str) -> str | None:
    """The text in the harness's input region, or ``None`` when unreadable."""
    if not _normalize(prompt_prefix).strip():
        return None
    lines = _normalize(tail).splitlines()
    if _is_opencode_prefix(prompt_prefix):
        return _opencode_input(lines)
    prefix_text = _normalize(prompt_prefix).strip()
    last_prompt = next(
        (i for i in range(len(lines) - 1, -1, -1) if lines[i].lstrip().startswith(prefix_text)),
        None,
    )
    if last_prompt is None:
        return None
    return _input_after_prompt(lines, last_prompt, prompt_prefix)


def _injection_view(content: str, pending: _PendingSubmit) -> str | None:
    """How the composer text *content* shows AQ's injection *pending*.

    ``"exact"``: verbatim, so Enter may submit it.  ``"collapsed"``: exactly
    one paste stand-in AQ can attribute, by the token it recorded when its own
    typing collapsed or, for a record without one, by a text long enough to
    collapse whose newline count is the stand-in's.  ``"windowed"``: a proper
    suffix ending with the marker, which is what a composer showing only its
    last rows displays.  ``None``: anything else -- a human draft, AQ text
    with human text around it, a paste AQ cannot attribute.  Only ``"exact"``
    is ever submitted; the other two can only be cleared.
    """
    token = _paste_placeholder(content)
    if token is not None:
        if pending.placeholder:
            return "collapsed" if token == pending.placeholder else None
        if _collapses(pending.text) and _placeholder_newlines(token) == pending.text.count("\n"):
            return "collapsed"
        return None
    shown, typed = _squash(content), _squash(pending.text)
    if shown == typed:
        return "exact"
    if shown and typed.endswith(shown) and _squash(pending.marker) in shown:
        return "windowed"
    return None


def _clear_remnant(content: str, pending: _PendingSubmit) -> bool:
    """Whether *content* can still be what is left of *pending* mid-clear.

    A clear kills rows from the end, so the text left is a prefix of the
    injection and a windowed composer shows a piece of it: every screen
    during a clear must be a contiguous run of AQ's own characters.
    """
    if _injection_view(content, pending) is not None:
        return True
    shown = _squash(content)
    return bool(shown) and shown in _squash(pending.text)


def _marker_on_input_line(tail: str, marker: str, prompt_prefix: str) -> bool:
    """True when *marker* sits at or after the last prompt-prefixed line.

    The positive half of :func:`_submit_pending` with none of its
    fail-safe guesses: an unrecognised screen answers ``False`` here.  Used
    to decide whether text in a composer is a nudge this daemon typed,
    where a wrong "yes" resubmits something already delivered.
    """
    if not marker:
        return False
    prefix = _normalize(prompt_prefix).strip()
    if not prefix:
        return False
    lines = _normalize(tail).splitlines()
    if _is_opencode_prefix(prompt_prefix):
        typed = _opencode_input(lines)
        return typed is not None and _squash(marker) in _squash(typed)
    last_prompt = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith(prefix):
            last_prompt = i
    if last_prompt is None:
        return False
    return _squash(marker) in _squash("\n".join(lines[last_prompt:]))


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

    The composer wraps long input onto rows of its own, which can split the
    marker, so matching ignores whitespace (:func:`_squash`).  A per-row
    match read a wrapped nudge as never typed and, after Enter, as
    submitted.

    OpenCode's input is the inside of its composer box, above the agent
    row that is its last bar-prefixed line (:func:`_opencode_box`).  Every
    wrapped row of it carries the bar, so the box is read bar-less.
    """
    if not marker:
        return False
    tail = _normalize(tail)
    needle = _squash(marker)
    if _is_opencode_prefix(prompt_prefix):
        typed = _opencode_input(tail.splitlines())
        if typed is None:
            # No recognisable box: a visible marker is still pending, as below.
            return needle in _squash(tail.replace(_OPENCODE_BAR, ""))
        return needle in _squash(typed)
    if needle not in _squash(tail):
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
    return needle in _squash("\n".join(lines[last_prompt:]))


_SAFE_META_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_environment_value(output: str, key: str) -> str | None:
    """Parse ``show-environment`` output: ``KEY=value`` or ``-KEY`` (unset)."""
    for line in output.splitlines():
        if line.startswith(f"{key}="):
            return line[len(key) + 1 :]
        if line == f"-{key}":
            return None
    return None
