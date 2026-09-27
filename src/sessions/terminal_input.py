"""An input-only tmux terminal: it types into an agent's pane and never attaches.

Phones use this instead of :class:`~src.sessions.terminal_pty.PtyTmuxClient`.
Agent windows run with ``window-size latest``, so any attached client, even one
flagged ``ignore-size``, would size the agent's real window to the phone and
leave it there. This client creates no tmux client at all, so it cannot resize
anything; the phone watches the pane stream instead, and ``read()`` produces
nothing.

* **Fence.** Every write re-resolves the numeric session id (``$N``) with the
  instance-token check the PTY client uses, so a stale phone never types into a
  same-named successor or a restarted server that reused ``$N``.
* **Keystrokes** go through ``send-keys -t $N -H``: raw bytes to the session's
  active pane, the one an attached client types into. A keystroke segment that
  is exactly one arrow key is sent by key name, so tmux encodes it for the
  pane's cursor-key mode (DECCKM) as it does for an attached client.
* **Pastes.** Bytes between ``ESC [200~`` and ``ESC [201~`` are collected, even
  across writes (the end marker may itself be split), up to 64 KiB, then loaded
  into a unique buffer and inserted with ``paste-buffer -p -d``. tmux adds the
  bracketed-paste markers only when the application asked for them, and turns
  LF into CR. A start marker must arrive whole within one write: holding back a
  trailing ``ESC`` would delay a real Esc key.
* **Copy mode** is left before a write, as the daemon's direct input does
  (``TmuxProvider.send_input``): the phone watches ``capture-pane``, which does
  not show copy mode, so keys sent into it would vanish unseen.

Errors carry fixed messages only, never input bytes, tmux argv or stderr.
Spec: ``docs/superpowers/specs/2026-09-26-mobile-interactive-terminal-design.md``.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING

from src.sessions.terminal_pty import TerminalAttachError, resolve_session_id

if TYPE_CHECKING:
    from src.models import SessionRecord
    from src.sessions.tmux import TmuxProvider


_UNAVAILABLE = "Terminal session is unavailable."
_CONNECTION_FAILED = "Terminal connection failed."
_MAX_INPUT = 64 * 1024
_MAX_PASTE = 64 * 1024
#: Bytes per ``send-keys -H`` command; tmux refuses a command line near 8 KiB of argv.
_KEYS_PER_COMMAND = 1024
_TMUX_TIMEOUT = 5.0
_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"
_ARROWS = {
    b"\x1b[A": "Up", b"\x1b[B": "Down", b"\x1b[C": "Right", b"\x1b[D": "Left",
    b"\x1bOA": "Up", b"\x1bOB": "Down", b"\x1bOC": "Right", b"\x1bOD": "Left",
}


class TerminalInputError(TerminalAttachError):
    """Refused input: the terminal stream closes with 4400, not 4409."""

    code = 4400


class TmuxInputClient:
    """The attach client's interface (``read``/``write``/``resize``/``verify``/``close``)."""

    def __init__(self, provider: TmuxProvider, row: SessionRecord):
        self._provider = provider
        self._name = row.name
        self._token = row.instance_token
        self._session_id: str | None = None
        self._closed = False
        self._closed_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        #: Paste content collected so far; ``None`` outside a bracketed paste.
        self._paste: bytearray | None = None

    @classmethod
    async def attach(cls, provider: TmuxProvider, row: SessionRecord) -> TmuxInputClient:
        """Resolve and pin the session's ``$N``. Creates no tmux client."""
        client = cls(provider, row)
        try:
            async with asyncio.timeout(3):
                client._session_id = await client._current_session_id()
        except TimeoutError:
            client._session_id = None
        if client._session_id is None:
            await client.close()
            raise TerminalAttachError(_UNAVAILABLE)
        return client

    async def _current_session_id(self) -> str | None:
        return await resolve_session_id(self._provider, self._name, self._token)

    async def verify(self) -> bool:
        """Fresh generation check; never uses provider caches."""
        if self._closed or self._session_id is None:
            return False
        current = await self._current_session_id()
        return current == self._session_id and not self._closed

    async def read(self, max_bytes: int) -> bytes:
        """Input-only: nothing is ever read. Returns ``b""`` once closed."""
        await self._closed_event.wait()
        return b""

    async def resize(self, cols: int, rows: int) -> None:
        raise TerminalInputError("Input-only terminals do not resize.")

    async def write(self, data: bytes) -> None:
        if len(data) > _MAX_INPUT:
            raise TerminalInputError("Terminal input is too large.")
        async with self._write_lock:
            if self._closed:
                raise TerminalAttachError(_UNAVAILABLE)
            if not data:
                return
            # Before every write: a stale phone must never type into a successor.
            current = await self._current_session_id()
            if current is None or current != self._session_id:
                raise TerminalAttachError(_UNAVAILABLE)
            await self._leave_copy_mode()
            for is_paste, payload in self._split(bytes(data)):
                if self._closed:
                    raise TerminalAttachError(_UNAVAILABLE)
                if is_paste:
                    await self._send_paste(payload)
                else:
                    await self._send_keys(payload)

    def _split(self, data: bytes) -> list[tuple[bool, bytes]]:
        """Advance the paste state machine; return ``(is_paste, bytes)`` operations."""
        operations: list[tuple[bool, bytes]] = []
        while data:
            if self._paste is None:
                start = data.find(_PASTE_START)
                if start < 0:
                    operations.append((False, data))
                    break
                if start:
                    operations.append((False, data[:start]))
                self._paste = bytearray()
                data = data[start + len(_PASTE_START):]
                continue
            # Earlier bytes hold no whole end marker, but may hold its first bytes.
            searched = max(0, len(self._paste) - (len(_PASTE_END) - 1))
            self._paste += data
            end = self._paste.find(_PASTE_END, searched)
            if end < 0:
                if len(self._paste) - (len(_PASTE_END) - 1) > _MAX_PASTE:
                    self._paste = None
                    raise TerminalInputError("Terminal paste is too large.")
                break
            paste = bytes(self._paste[:end])
            data = bytes(self._paste[end + len(_PASTE_END):])
            self._paste = None
            if len(paste) > _MAX_PASTE:
                raise TerminalInputError("Terminal paste is too large.")
            if paste:
                operations.append((True, paste))
        return operations

    async def _tmux(self, *args: str, stdin: bytes | None = None) -> str:
        kwargs = {"timeout": _TMUX_TIMEOUT}
        if stdin is not None:
            kwargs["stdin"] = stdin
        try:
            return await self._provider._tmux(*args, **kwargs)
        except Exception:  # noqa: BLE001 - tmux errors carry argv (typed bytes) and stderr
            raise TerminalAttachError(_CONNECTION_FAILED) from None

    async def _leave_copy_mode(self) -> None:
        in_mode = await self._tmux("display-message", "-p", "-t", self._session_id, "#{pane_in_mode}")
        if in_mode.strip() == "1":
            await self._tmux("send-keys", "-t", self._session_id, "-X", "cancel")

    async def _send_keys(self, payload: bytes) -> None:
        name = _ARROWS.get(payload)
        if name is not None:
            await self._tmux("send-keys", "-t", self._session_id, name)
            return
        for offset in range(0, len(payload), _KEYS_PER_COMMAND):
            if self._closed:
                raise TerminalAttachError(_UNAVAILABLE)
            chunk = payload[offset:offset + _KEYS_PER_COMMAND]
            await self._tmux("send-keys", "-t", self._session_id, "-H", *chunk.hex(" ").split(" "))

    async def _send_paste(self, payload: bytes) -> None:
        # Each buffer is unique, so two phones or tiles cannot swap pastes.
        buffer = "aq-input-" + uuid.uuid4().hex
        pasted = False
        try:
            await self._tmux("load-buffer", "-b", buffer, "-", stdin=payload)
            await self._tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", self._session_id)
            pasted = True  # -d already deleted the buffer.
        finally:
            if not pasted:
                with contextlib.suppress(Exception):
                    await self._provider._tmux("delete-buffer", "-b", buffer, timeout=_TMUX_TIMEOUT)

    async def close(self) -> None:
        """Idempotent. Wakes ``read()`` and drops any partial paste."""
        self._closed = True
        self._paste = None
        self._closed_event.set()
