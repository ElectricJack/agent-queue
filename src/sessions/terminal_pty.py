"""A disposable tmux attach client; closing it never stops the agent's pane.

POSIX terminal modules are imported only when attaching/resizing so API modules
can safely import this helper on Windows. No terminal bytes or tmux stderr are
included in errors: output is delivered only through the authorized byte stream.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import re
import signal
import struct
from typing import TYPE_CHECKING

from src.sessions.provider import SessionError

if TYPE_CHECKING:
    from src.models import SessionRecord
    from src.sessions.tmux import TmuxProvider


_UNAVAILABLE = "Terminal session is unavailable."
_CONNECTION_FAILED = "Terminal connection failed."
_REQUIRE_DETACH = "Terminal requires tmux detach-on-destroy on for this session."
_MAX_READ = 16 * 1024
_MAX_INPUT = 64 * 1024
# The window size and ``window-size`` policy from before the first sizing viewer
# attached, then the client pids of the viewers holding it, as
# ``"<cols>x<rows> <policy> <pid>..."``; see ``PtyTmuxClient.attach(restore_size=)``.
_RESTORE_OPTION = "@aq_restore_size"
_RESTORE_VALUE = re.compile(
    r"([0-9]{1,5})x([0-9]{1,5}) (largest|smallest|manual|latest)((?: [0-9]{1,10})*)"
)
_HISTORY_LIMIT = 4 * 1024 * 1024
# Trailing blanks (and the SGR codes between them) a joined history line keeps:
# on a narrower terminal they would wrap into rows of nothing.
_TRAILING_BLANK = re.compile(r"(?:[ \t]|\x1b\[[0-9;:]*m)+$")
# Attach clients run as xterm-256color, whose ``indn`` (CSI n S) scrolls several
# lines at once. A terminal discards lines that CSI S removes but keeps those a
# line feed pushes off the top, so a viewer keeping its own scrollback needs tmux
# to scroll with line feeds; see ``PtyTmuxClient.attach(scrollback=)``.
_LINE_FEED_SCROLL = "xterm-256color:indn@"


class TerminalAttachError(Exception):
    """A safe, fixed terminal error suitable for an API response."""


async def resolve_session_id(provider: TmuxProvider, name: str, token: str | None) -> str | None:
    """The numeric tmux id (``$N``) of session *name* while it still carries *token*.

    ``None`` when the name is gone, the id is malformed, or the session's
    ``AQ_INSTANCE_TOKEN`` differs. Callers compare the result with the id they
    resolved first, so a same-named successor, or a restarted tmux server that
    reused ``$N`` for another instance, never matches. Never uses provider caches.
    """
    if not token:
        return None
    try:
        session_id = (await provider._tmux(
            "display-message", "-p", "-t", f"={name}:",
            "#{session_id}", timeout=1,
        )).strip()
        if not re.fullmatch(r"\$[0-9]+", session_id):
            return None
        observed = await provider._tmux(
            "show-environment", "-t", session_id, "AQ_INSTANCE_TOKEN", timeout=1,
        )
        if observed.rstrip("\r\n") != f"AQ_INSTANCE_TOKEN={token}":
            return None
        return session_id
    except Exception:
        return None


class PtyTmuxClient:
    def __init__(self, provider: TmuxProvider, row: SessionRecord):
        self._provider = provider
        self._name = row.name
        self._token = row.instance_token
        self._session_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._master = -1
        self._closed = False
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._read_waiter: tuple[int, asyncio.Future[bool]] | None = None
        self._write_waiter: tuple[int, asyncio.Future[bool]] | None = None
        self._restore_size = False

    @classmethod
    async def attach(
        cls, provider: TmuxProvider, row: SessionRecord, *, cols: int, rows: int,
        restore_size: bool = False, scrollback: bool = False,
    ) -> PtyTmuxClient:
        """Attach a client at *cols* x *rows*.

        Every attached client resizes the agent's window (``window-size latest``),
        and tmux keeps the last size once no client is left. With *restore_size*
        (phones), the size from before the first such viewer attached is put back
        when the last client detaches, so the agent does not keep a phone-sized
        window after the phone leaves. A phone that leaves only other kinds of
        viewer attached drops the record: they keep the window at their own size.
        With *scrollback* the viewer keeps its own
        scrollback, so tmux is made to scroll with line feeds.
        """
        if os.name != "posix":
            raise TerminalAttachError("Interactive terminals require a POSIX host.")
        import pty

        client = cls(provider, row)
        slave = -1
        try:
            async with asyncio.timeout(3):
                client._session_id = await client._current_session_id()
                if client._session_id is None:
                    raise TerminalAttachError(_UNAVAILABLE)
                policy = await client._detach_policy()
                if policy is None:
                    raise TerminalAttachError(_UNAVAILABLE)
                if policy != "on":
                    raise TerminalAttachError(_REQUIRE_DETACH)
                if restore_size:
                    await client._remember_size()
                if scrollback:
                    await client._scroll_with_line_feeds()
                client._master, slave = pty.openpty()
                client._set_size(slave, cols, rows)
                os.set_blocking(client._master, False)
                env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
                env.pop("TMUX", None)
                env.pop("TMUX_PANE", None)
                # A numeric tmux ID cannot silently follow a replacement name.
                # RGB must be declared explicitly; COLORTERM alone is insufficient.
                # -E prevents a viewer from replacing the agent session environment.
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    "tmux", "-u", "-L", provider.socket, "-T", "RGB",
                    "attach-session", "-E", "-t", client._session_id,
                    stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True, env=env,
                ))
                try:
                    client._process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    # Recover ownership even if cancellation races process creation.
                    client._process = await spawn
                    raise
                finally:
                    os.close(slave)
                    slave = -1
                while not await client._attached():
                    if client._process.returncode is not None:
                        raise TerminalAttachError(_UNAVAILABLE)
                    await asyncio.sleep(0.025)
                # No bytes escape until the name, instance, and actual client target
                # have been checked again after the attachment became visible.
                if not await client.verify():
                    raise TerminalAttachError(_UNAVAILABLE)
                if client._restore_size:
                    await client._hold_record()
            return client
        except asyncio.CancelledError:
            await client.close()
            raise
        except TerminalAttachError:
            await client.close()
            raise
        except Exception:
            await client.close()
            raise TerminalAttachError(_UNAVAILABLE) from None
        finally:
            if slave >= 0:
                os.close(slave)

    async def _current_session_id(self) -> str | None:
        return await resolve_session_id(self._provider, self._name, self._token)

    async def _attached(self) -> bool:
        proc = self._process
        if self._closed or proc is None or proc.returncode is not None:
            return False
        try:
            clients = await self._provider._tmux(
                "list-clients", "-F", "#{client_pid}\t#{session_id}", timeout=1,
            )
            return f"{proc.pid}\t{self._session_id}" in clients.splitlines()
        except Exception:
            return False

    async def _other_clients(self) -> set[str]:
        """Pids of the clients attached to this session, other than this one."""
        own = str(self._process.pid) if self._process is not None else None
        out = await self._provider._tmux(
            "list-clients", "-t", self._session_id, "-F", "#{client_pid}", timeout=1,
        )
        return {pid for pid in out.split() if pid != own}

    async def _record(self) -> re.Match[str] | None:
        return _RESTORE_VALUE.fullmatch((await self._provider._tmux(
            "show-options", "-qv", "-t", self._session_id, _RESTORE_OPTION, timeout=1,
        )).strip())

    async def _remember_size(self) -> None:
        """Record the resting window size before this client changes it.

        A record held by a viewer still attached (another phone) is kept.
        Otherwise this viewer records the current size, which is the size any
        other viewers set, so the last one out restores what was there first.
        """
        sid = self._session_id
        try:
            recorded = await self._record()
            if recorded is not None and set(recorded[4].split()) & await self._other_clients():
                self._restore_size = True
                return
            size = (await self._provider._tmux(
                "display-message", "-p", "-t", f"{sid}:",
                "#{window_width}x#{window_height}", timeout=1,
            )).strip()
            policy = (await self._provider._tmux(
                "show-options", "-wAv", "-t", f"{sid}:", "window-size", timeout=1,
            )).strip()
            value = f"{size} {policy}"
            if not _RESTORE_VALUE.fullmatch(value):
                return
            await self._provider._tmux(
                "set-option", "-t", sid, _RESTORE_OPTION, value, timeout=1,
            )
            self._restore_size = True
        except (SessionError, OSError):
            # Best effort: an unrecorded size only means the window keeps the
            # viewer's size, which is what every other attach does.
            return

    async def _hold_record(self) -> None:
        """Add this attached client's pid to the record, which keeps it live."""
        with contextlib.suppress(SessionError, OSError):
            recorded = await self._record()
            if recorded is not None and self._process is not None:
                await self._provider._tmux(
                    "set-option", "-t", self._session_id, _RESTORE_OPTION,
                    f"{recorded[0]} {self._process.pid}", timeout=1,
                )

    async def _restore_window(self) -> None:
        sid = self._session_id
        if await self._current_session_id() != sid:
            return
        recorded = await self._record()
        if recorded is None:
            return
        others = await self._other_clients()
        if others:
            # Viewers that did not ask for a restore keep the window at their
            # size; a record no attached viewer holds would only go stale.
            if not set(recorded[4].split()) & others:
                await self._provider._tmux(
                    "set-option", "-u", "-t", sid, _RESTORE_OPTION, timeout=1,
                )
            return
        cols, rows, policy = recorded.groups()[:3]
        try:
            # resize-window also switches the window to a manual size; the
            # policy is put back after, which keeps this size until a client
            # attaches (tmux.py repaint does the same).
            await self._provider._tmux(
                "resize-window", "-t", f"{sid}:", "-x", cols, "-y", rows, timeout=1,
            )
        finally:
            with contextlib.suppress(Exception):
                await self._provider._tmux(
                    "set-option", "-w", "-t", f"{sid}:", "window-size", policy, timeout=1,
                )
            with contextlib.suppress(Exception):
                await self._provider._tmux(
                    "set-option", "-u", "-t", sid, _RESTORE_OPTION, timeout=1,
                )

    async def _scroll_with_line_feeds(self) -> None:
        """Drop ``indn`` for xterm-256color clients of this tmux server.

        The override is server-wide and only ever added: every client still sees
        the same screen, drawn with line feeds where tmux would send CSI n S.
        Best effort; without it a viewer's scrollback misses some lines.
        """
        try:
            current = await self._provider._tmux(
                "show-options", "-sv", "terminal-overrides", timeout=1,
            )
            if _LINE_FEED_SCROLL in current.splitlines():
                return
            await self._provider._tmux(
                "set-option", "-sa", "terminal-overrides", _LINE_FEED_SCROLL, timeout=1,
            )
        except (SessionError, OSError):
            pass

    async def history(self, lines: int, rows: int) -> bytes:
        """Up to *lines* of the pane's scrollback, framed for a fresh terminal.

        Sent before any attach output, the lines end up in the receiving
        terminal's own scrollback: *rows* line feeds push the last of them just
        above a blank screen, which tmux's first redraw then paints. Wrapped
        lines are joined (the viewer may be narrower than the pane) and each
        line ends with an SGR reset so no colour leaks into the next.
        """
        if self._closed or lines <= 0:
            return b""
        sid = self._session_id
        try:
            size = int((await self._provider._tmux(
                "display-message", "-p", "-t", f"{sid}:", "#{history_size}", timeout=1,
            )).strip())
            count = min(lines, size)
            if count <= 0:
                return b""
            out = await self._provider._tmux(
                "capture-pane", "-p", "-e", "-J", "-S", f"-{count}", "-E", "-1",
                "-t", f"{sid}:", timeout=2,
            )
        except (SessionError, OSError, ValueError):
            return b""
        out = out.removesuffix("\n")
        kept: list[str] = []
        total = 0
        for line in reversed(out.split("\n")):
            line = _TRAILING_BLANK.sub("", line)
            total += len(line) + 6
            if total > _HISTORY_LIMIT:
                break
            kept.append(line)
        kept.reverse()
        if not kept:
            return b""
        text = "\x1b[0m\r\n".join(kept) + "\x1b[0m" + "\r\n" * rows
        return text.encode("utf-8", errors="replace")

    async def _detach_policy(self) -> str | None:
        try:
            # Include inherited options. Other policies can move this client to an
            # unrelated session when its target dies; tmux has no per-client guard.
            return (await self._provider._tmux(
                "show-options", "-Av", "-t", self._session_id,
                "detach-on-destroy", timeout=1,
            )).strip()
        except Exception:
            return None

    async def verify(self) -> bool:
        """Fresh generation and client-target checks; never use provider caches."""
        if self._closed or await self._current_session_id() != self._session_id:
            return False
        if await self._detach_policy() != "on":
            return False
        return await self._attached()

    @staticmethod
    def _set_size(fd: int, cols: int, rows: int) -> None:
        import fcntl
        import termios

        if not (1 <= cols <= 65535 and 1 <= rows <= 65535):
            raise TerminalAttachError("Terminal dimensions are invalid.")
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def resize(self, cols: int, rows: int) -> None:
        if self._closed or self._process is None or self._process.returncode is not None:
            raise TerminalAttachError(_UNAVAILABLE)
        try:
            self._set_size(self._master, cols, rows)
            # setsid alone does not make the slave a controlling terminal, so the
            # kernel will not deliver SIGWINCH to this client after the ioctl.
            self._process.send_signal(signal.SIGWINCH)
        except TerminalAttachError:
            raise
        except Exception:
            raise TerminalAttachError(_CONNECTION_FAILED) from None

    async def _ready(self, *, writing: bool) -> bool:
        if self._closed:
            return False
        fd = self._master
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        attr = "_write_waiter" if writing else "_read_waiter"
        waiter = (fd, future)
        setattr(self, attr, waiter)

        def ready():
            if getattr(self, attr) is waiter and not future.done():
                future.set_result(not self._closed and self._master == fd)

        add = loop.add_writer if writing else loop.add_reader
        remove = loop.remove_writer if writing else loop.remove_reader
        try:
            add(fd, ready)
            return await future
        finally:
            # close() unregisters before closing the descriptor. If that number
            # has since been reused, this old waiter must not remove its watcher.
            if getattr(self, attr) is waiter:
                setattr(self, attr, None)
                remove(fd)

    async def read(self, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            return b""
        async with self._read_lock:
            while not self._closed:
                try:
                    return os.read(self._master, min(max_bytes, _MAX_READ))
                except BlockingIOError:
                    if not await self._ready(writing=False):
                        return b""
                except OSError as exc:
                    if exc.errno == errno.EIO or self._closed:
                        return b""
                    raise TerminalAttachError(_CONNECTION_FAILED) from None
            return b""

    async def write(self, data: bytes) -> None:
        if len(data) > _MAX_INPUT:
            raise TerminalAttachError("Terminal input is too large.")
        async with self._write_lock:
            offset = 0
            while offset < len(data):
                if self._closed:
                    raise TerminalAttachError(_UNAVAILABLE)
                try:
                    count = os.write(self._master, memoryview(data)[offset:])
                    if not count:
                        raise TerminalAttachError(_CONNECTION_FAILED)
                    offset += count
                except BlockingIOError:
                    if not await self._ready(writing=True):
                        raise TerminalAttachError(_UNAVAILABLE)
                except OSError:
                    raise TerminalAttachError(_CONNECTION_FAILED) from None

    async def close(self) -> None:
        """Detach only this client, preserving the tmux session and its process."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            loop = asyncio.get_running_loop()
            for attr, remove in (
                ("_read_waiter", loop.remove_reader),
                ("_write_waiter", loop.remove_writer),
            ):
                waiter = getattr(self, attr)
                if waiter is not None:
                    setattr(self, attr, None)
                    fd, future = waiter
                    remove(fd)
                    if not future.done():
                        future.set_result(False)
            if self._master >= 0:
                os.close(self._master)
                self._master = -1
            proc = self._process
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 1)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            if self._restore_size:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(3):
                        await self._restore_window()
