"""Versioned, daemon-independent admission for cooperative host workloads.

Lock order is turnstile -> box -> slots. An exclusive waiter retains the
turnstile while shared holders drain. Weighted shared admission rolls back
every partial claim before retrying. Descriptors are inherited by executed
children, so a wrapper's death does not release a running child's capacity.

The original slot files remain the capacity locks and the compatibility
bridge: exclusive admission also reserves all observed slots. This fences
old slot-only clients racing admission, but does not certify an installation
as upgraded. An already held unversioned slot refuses exclusive admission.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from src.resources.semaphore import SlotSemaphore, SlotTimeout, _read_json

PROTOCOL_VERSION = 1
PROTOCOL_NAME = "aq-box-lock"
PROTOCOL_RECORD = {"protocol": PROTOCOL_NAME, "version": PROTOCOL_VERSION}


class IncompatibleLockClient(RuntimeError):
    """A lock protocol or active slot holder cannot provide box exclusion."""


def observed_slot_count(lock_dir: Path, configured_slots: int) -> int:
    """Include slots left by clients using a larger capacity override."""
    count = max(1, configured_slots)
    for path in lock_dir.glob("slot-*.lock"):
        suffix = path.name.removeprefix("slot-").removesuffix(".lock")
        if suffix.isdecimal():
            count = max(count, int(suffix) + 1)
    return count


def _try_lock(path: Path, mode: int) -> int | None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        os.set_inheritable(fd, True)
        return fd
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise


def _write_record(fd: int, meta: Mapping[str, object]) -> None:
    record = {**dict(meta), "pid": os.getpid(), "since": time.time()}
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps(record).encode())


def _lock_state(path: Path) -> dict:
    """Probe without creating files or interpreting stale metadata as a lock."""
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return {"held": False, "mode": None, "holder": {}}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"held": True, "mode": "exclusive", "holder": _read_json(path)}
            return {"held": True, "mode": "shared", "holder": {}}
        return {"held": False, "mode": None, "holder": {}}
    finally:
        os.close(fd)


class BoxLock:
    """Shared weighted capacity or exclusive box admission on stable inodes.

    ``SlotSemaphore`` remains the low-level slot API for observers and old
    clients. Execution entry points use this API; they must share a lock
    directory. Never unlink or replace a lock file, even after a crash.
    """

    def __init__(self, lock_dir: str | os.PathLike[str], slots: int) -> None:
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            raise ValueError("box capacity must be a positive integer")
        self.lock_dir = Path(lock_dir)
        self.slots = slots
        self.semaphore = SlotSemaphore(self.lock_dir, slots)

    @property
    def turnstile_path(self) -> Path:
        return self.lock_dir / "turnstile.lock"

    @property
    def box_path(self) -> Path:
        return self.lock_dir / "box.lock"

    def _check_version(self) -> None:
        # Called only under the turnstile. Atomic replacement applies to the
        # manifest, never to a flock inode. Corrupt/unknown manifests fail closed.
        path = self.lock_dir / "protocol.json"
        if path.exists():
            if _read_json(path) != PROTOCOL_RECORD:
                raise IncompatibleLockClient(f"incompatible box-lock protocol at {path}")
            return
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(PROTOCOL_RECORD), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot(self) -> dict:
        sem = SlotSemaphore(self.lock_dir, observed_slot_count(self.lock_dir, self.slots))
        incompatible = [
            row
            for row in sem.snapshot()["slots"]
            if row["held"] and row["holder"].get("box_protocol") != PROTOCOL_RECORD
        ]
        return {
            **PROTOCOL_RECORD,
            "box": _lock_state(self.box_path),
            "turnstile": _lock_state(self.turnstile_path),
            "incompatible_slots": incompatible,
        }

    @contextmanager
    def acquire(
        self,
        *,
        exclusive: bool = False,
        weight: int = 1,
        timeout: float | None = None,
        poll: float = 2.0,
        meta: Mapping[str, object] | None = None,
        on_wait=None,
    ) -> Iterator[tuple[int, ...]]:
        """Hold execution locks; timeout includes every admission stage.

        The yielded slots retain today's observer/reaper contract. Exclusive
        holds all observed slots as a migration fence; weight applies only
        to shared admission. No environment variable can bypass admission.
        """
        if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= self.slots:
            raise ValueError(f"weight must be an integer between 1 and capacity {self.slots}")
        record = {
            **dict(meta or {}),
            "box_protocol": dict(PROTOCOL_RECORD),
            "box_mode": "exclusive" if exclusive else "shared",
            "weight": weight,
        }
        self.semaphore._ensure_dirs()
        started = time.monotonic()
        waiter = self.semaphore.waiters_dir / f"{os.getpid()}.json"
        turnstile = None
        box = None
        claims: list[tuple[int, int]] = []

        def wait() -> None:
            waited = time.monotonic() - started
            if timeout is not None and waited >= timeout:
                raise SlotTimeout(
                    f"no test slot free after {waited:.0f}s "
                    f"({self.slots} slot(s) in {self.lock_dir}; box admission)"
                )
            self.semaphore._write_waiter(waiter, record)
            if on_wait is not None:
                on_wait(waited, self.semaphore.snapshot())
            delay = max(0.05, poll)
            if timeout is not None:
                delay = min(delay, max(0, timeout - waited))
            time.sleep(delay)

        try:
            while turnstile is None:
                turnstile = _try_lock(self.turnstile_path, fcntl.LOCK_EX)
                if turnstile is None:
                    wait()
            self._check_version()
            _write_record(turnstile, record)
            while True:
                if exclusive:
                    incompatible = self.snapshot()["incompatible_slots"]
                    if incompatible:
                        slots = ", ".join(str(row["slot"]) for row in incompatible)
                        raise IncompatibleLockClient(
                            f"test slot {slots} is occupied by an incompatible slot-only client; "
                            "upgrade local entry points and drain old runs before exclusive work"
                        )
                box = _try_lock(self.box_path, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                if box is not None:
                    capacity = (
                        observed_slot_count(self.lock_dir, self.slots) if exclusive else self.slots
                    )
                    sem = SlotSemaphore(self.lock_dir, capacity)
                    needed = capacity if exclusive else weight
                    for slot in range(capacity):
                        fd = sem._try_slot(slot, record)
                        if fd is not None:
                            claims.append((slot, fd))
                        if len(claims) == needed:
                            break
                    if len(claims) == needed:
                        break
                    for _, fd in reversed(claims):
                        os.close(fd)
                    claims.clear()
                    os.close(box)
                    box = None
                wait()
            if exclusive:
                _write_record(box, record)
            # Admission ends here. Execution retains box and capacity, never
            # the turnstile, which the next exclusive waiter may hold to drain.
            os.close(turnstile)
            turnstile = None
            waiter.unlink(missing_ok=True)
            yield tuple(slot for slot, _ in claims)
        finally:
            # Close, rather than LOCK_UN: inherited descriptors must continue
            # holding locks if a child outlives its wrapper. Keep metadata for it.
            for _, fd in reversed(claims):
                os.close(fd)
            if box is not None:
                os.close(box)
            if turnstile is not None:
                os.close(turnstile)
            waiter.unlink(missing_ok=True)
