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

if TYPE_CHECKING:
    from src.models import SessionRecord
    from src.sessions.tmux import TmuxProvider


_UNAVAILABLE = "Terminal session is unavailable."
_CONNECTION_FAILED = "Terminal connection failed."
_REQUIRE_DETACH = "Terminal requires tmux detach-on-destroy on for this session."
_MAX_READ = 16 * 1024
_MAX_INPUT = 64 * 1024


class TerminalAttachError(Exception):
    """A safe, fixed terminal error suitable for an API response."""


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

    @classmethod
    async def attach(
        cls, provider: TmuxProvider, row: SessionRecord, *, cols: int, rows: int,
    ) -> PtyTmuxClient:
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
        if not self._token:
            return None
        try:
            session_id = (await self._provider._tmux(
                "display-message", "-p", "-t", f"={self._name}:",
                "#{session_id}", timeout=1,
            )).strip()
            if not re.fullmatch(r"\$[0-9]+", session_id):
                return None
            token = await self._provider._tmux(
                "show-environment", "-t", session_id, "AQ_INSTANCE_TOKEN", timeout=1,
            )
            if token.rstrip("\r\n") != f"AQ_INSTANCE_TOKEN={self._token}":
                return None
            return session_id
        except Exception:
            return None

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
