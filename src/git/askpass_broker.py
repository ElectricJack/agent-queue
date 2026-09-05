"""Linux one-shot credential broker for the packaged Git askpass helper."""

from __future__ import annotations

import array
import asyncio
import hashlib
import os
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

from src.git.askpass_fd import MAX_REQUEST_BYTES, request_payload

_CREDENTIAL_SIZE = struct.calcsize("3i")


@dataclass(frozen=True)
class PinnedFile:
    path: str
    device: int
    inode: int
    owner: int
    digest: str | None = None


@dataclass(frozen=True)
class GitCredentialTopology:
    git: PinnedFile
    remote_helper: PinnedFile
    remote_helper_argv0: str
    interpreter: PinnedFile
    askpass: PinnedFile


def _pin_regular_file(
    path: Path, *, owner: int, include_digest: bool = False
) -> PinnedFile:
    resolved = path.resolve(strict=True)
    details = resolved.stat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != owner
        or details.st_mode & 0o022
    ):
        raise OSError("unsafe credential executable")
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest() if include_digest else None
    return PinnedFile(
        path=str(resolved),
        device=details.st_dev,
        inode=details.st_ino,
        owner=owner,
        digest=digest,
    )


def _validate_root_directory_tree(path: Path) -> None:
    resolved = path.resolve(strict=True)
    candidates = [resolved]
    candidates.extend(resolved.parents)
    for candidate in candidates:
        details = candidate.stat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or details.st_mode & 0o022
        ):
            raise OSError("unsafe Git executable path")


def pin_git_credential_topology(
    *, exec_path: Path, askpass_path: Path
) -> GitCredentialTopology:
    if not exec_path.is_absolute():
        raise OSError("unsafe Git executable path")
    _validate_root_directory_tree(exec_path)
    git = _pin_regular_file(Path("/usr/bin/git"), owner=0)
    interpreter = _pin_regular_file(Path("/usr/bin/python3"), owner=0)
    remote_argv0 = exec_path / "git-remote-https"
    link_details = remote_argv0.lstat()
    if link_details.st_uid != 0:
        raise OSError("unsafe Git remote helper")
    remote_helper = _pin_regular_file(remote_argv0, owner=0)
    askpass = _pin_regular_file(
        askpass_path, owner=os.geteuid(), include_digest=True
    )
    return GitCredentialTopology(
        git=git,
        remote_helper=remote_helper,
        remote_helper_argv0=str(remote_argv0),
        interpreter=interpreter,
        askpass=askpass,
    )


def supported() -> bool:
    return all(
        hasattr(socket, name)
        for name in ("AF_UNIX", "SOCK_DGRAM", "SO_PASSCRED", "SCM_CREDENTIALS")
    ) and hasattr(socket, "SCM_RIGHTS")


def make_request_channel() -> tuple[socket.socket, socket.socket]:
    if not supported():
        raise OSError("credential broker is unsupported")
    broker, request = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        broker.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        broker.setblocking(False)
        return broker, request
    except BaseException:
        broker.close()
        request.close()
        raise


def zeroize(buffer: bytearray) -> None:
    buffer[:] = b"\x00" * len(buffer)
    buffer.clear()


def _parent_pid(pid: int) -> int | None:
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(stat_fields[1])
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def _matches_pin(path: Path, pinned: PinnedFile, *, include_digest: bool = False) -> bool:
    try:
        current = _pin_regular_file(
            path, owner=pinned.owner, include_digest=include_digest
        )
    except OSError:
        return False
    return (
        current.path == pinned.path
        and current.device == pinned.device
        and current.inode == pinned.inode
        and current.digest == pinned.digest
    )


def _strip_cmdline(path: Path) -> list[bytes]:
    arguments = path.read_bytes().split(b"\x00")
    while arguments and not arguments[-1]:
        arguments.pop()
    return arguments


def _has_ancestor(pid: int, ancestor: int) -> bool:
    current = pid
    for _ in range(64):
        if current == ancestor:
            return True
        parent = _parent_pid(current)
        if parent is None or parent <= 1 or parent == current:
            return False
        current = parent
    return False


def _is_packaged_helper(
    pid: int,
    uid: int,
    *,
    git_pid: int,
    topology: GitCredentialTopology,
    repository: str,
    expected_prompt: str,
) -> bool:
    if pid <= 0 or uid != os.geteuid():
        return False
    try:
        if not _matches_pin(Path(f"/proc/{git_pid}/exe"), topology.git):
            return False
        if os.getpgid(pid) != git_pid:
            return False
        arguments = _strip_cmdline(Path(f"/proc/{pid}/cmdline"))
        if not _matches_pin(Path(f"/proc/{pid}/exe"), topology.interpreter):
            return False
        if not _matches_pin(
            Path(topology.askpass.path), topology.askpass, include_digest=True
        ):
            return False
        parent = _parent_pid(pid)
        if parent is None or parent == git_pid or os.getpgid(parent) != git_pid:
            return False
        if not _matches_pin(
            Path(f"/proc/{parent}/exe"), topology.remote_helper
        ):
            return False
        parent_arguments = _strip_cmdline(Path(f"/proc/{parent}/cmdline"))
    except (FileNotFoundError, OSError, ProcessLookupError):
        return False
    if arguments[1:] != [os.fsencode(topology.askpass.path), expected_prompt.encode()]:
        return False
    if parent_arguments != [
        os.fsencode(topology.remote_helper_argv0),
        repository.encode(),
        repository.encode(),
    ]:
        return False
    return _has_ancestor(parent, git_pid)


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
    topology: GitCredentialTopology,
    authority: str,
    repository: str,
    prompt: str,
    timeout: float,
) -> bool:
    """Release ``token`` once to a validated helper-private reply descriptor."""
    try:
        expected = request_payload(authority, repository, prompt)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
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
                topology=topology,
                repository=repository,
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
