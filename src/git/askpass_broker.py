"""Linux one-shot credential broker for the packaged Git askpass helper."""

from __future__ import annotations

import array
import asyncio
import os
import socket
import struct
from pathlib import Path

from src.git.askpass_fd import MAX_REQUEST_BYTES, request_payload

_CREDENTIAL_SIZE = struct.calcsize("3i")


def supported() -> bool:
    return all(
        hasattr(socket, name)
        for name in ("AF_UNIX", "SOCK_DGRAM", "SO_PASSCRED", "SCM_CREDENTIALS")
    ) and hasattr(socket, "SCM_RIGHTS")


def make_request_channel() -> tuple[socket.socket, socket.socket]:
    if not supported():
        raise OSError("credential broker is unsupported")
    broker, request = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    broker.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    broker.setblocking(False)
    return broker, request


def zeroize(buffer: bytearray) -> None:
    buffer[:] = b"\x00" * len(buffer)
    buffer.clear()


def _parent_pid(pid: int) -> int | None:
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(stat_fields[1])
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def _is_packaged_helper(
    pid: int,
    uid: int,
    *,
    git_pid: int,
    helper_path: str,
    expected_prompt: str,
) -> bool:
    if pid <= 0 or uid != os.geteuid():
        return False
    try:
        if os.getpgid(pid) != git_pid:
            return False
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
        while arguments and not arguments[-1]:
            arguments.pop()
        if not Path(f"/proc/{pid}/exe").samefile("/usr/bin/python3"):
            return False
    except (FileNotFoundError, OSError, ProcessLookupError):
        return False
    if arguments[1:] != [os.fsencode(helper_path), expected_prompt.encode()]:
        return False
    current = pid
    for _ in range(64):
        if current == git_pid:
            return True
        parent = _parent_pid(current)
        if parent is None or parent <= 1 or parent == current:
            return False
        current = parent
    return False


async def _wait_readable(channel: socket.socket, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def mark_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(channel.fileno(), mark_ready)
    try:
        await asyncio.wait_for(ready, timeout=timeout)
    finally:
        loop.remove_reader(channel.fileno())


def _received_authority(
    channel: socket.socket,
) -> tuple[bytes, tuple[int, int, int] | None, list[int], int]:
    ancillary_size = socket.CMSG_SPACE(_CREDENTIAL_SIZE) + socket.CMSG_SPACE(
        array.array("i").itemsize * 8
    )
    data, ancillary, flags, _ = channel.recvmsg(MAX_REQUEST_BYTES + 1, ancillary_size)
    credentials = None
    descriptors: list[int] = []
    for level, kind, value in ancillary:
        if level != socket.SOL_SOCKET:
            continue
        if kind == socket.SCM_CREDENTIALS and len(value) >= _CREDENTIAL_SIZE:
            credentials = struct.unpack("3i", value[:_CREDENTIAL_SIZE])
        elif kind == socket.SCM_RIGHTS:
            received = array.array("i")
            received.frombytes(value[: len(value) - (len(value) % received.itemsize)])
            descriptors.extend(received)
    return data, credentials, descriptors, flags


async def serve_one_credential(
    channel: socket.socket,
    token: bytearray,
    *,
    git_pid: int,
    helper_path: str,
    authority: str,
    repository: str,
    prompt: str,
    timeout: float,
) -> bool:
    """Release ``token`` once to a validated helper-private reply descriptor."""
    expected = request_payload(authority, repository, prompt)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await _wait_readable(channel, remaining)
            try:
                data, credentials, descriptors, flags = _received_authority(channel)
            except BlockingIOError:
                continue
            malformed = bool(
                flags & (getattr(socket, "MSG_TRUNC", 0) | getattr(socket, "MSG_CTRUNC", 0))
                or len(data) > MAX_REQUEST_BYTES
                or credentials is None
                or len(descriptors) != 1
            )
            reply_fd = descriptors[0] if len(descriptors) == 1 else None
            if malformed:
                for descriptor in descriptors:
                    os.close(descriptor)
                continue
            pid, uid, _gid = credentials
            if data != expected or not _is_packaged_helper(
                pid,
                uid,
                git_pid=git_pid,
                helper_path=helper_path,
                expected_prompt=prompt,
            ):
                os.close(reply_fd)
                continue
            reply = socket.socket(fileno=reply_fd)
            reply.setblocking(False)
            try:
                await asyncio.wait_for(
                    loop.sock_sendall(reply, memoryview(token)), timeout=remaining
                )
            finally:
                reply.close()
            return True
    except asyncio.TimeoutError:
        return False
    finally:
        channel.close()
        zeroize(token)
