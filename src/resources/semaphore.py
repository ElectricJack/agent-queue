"""A global, crash-safe slot semaphore built on ``flock``.

Layer 2 of resource gating.  Layer 1 bounds what *one* session does; this
bounds the sum.  Four workers each is still 32 test processes when eight
agents happen to test at the same moment, and test runs are exactly the
bursty, all-at-once workload that produces the load spike.

Why ``flock`` and not a database row or a daemon endpoint:

* **Crash release is free.**  The kernel drops an ``flock`` when the last
  descriptor referring to it closes — normal exit, ``SIGKILL``, an OOM
  reaper, a ``tmux kill-session``, all of them.  A lease in a table needs a
  reaper, and the reaper is the part that breaks at 3 a.m.
* **No daemon dependency.**  ``aq test`` has to work in a worktree while
  the daemon is restarting; a test wrapper that fails closed when the
  daemon is down would just be trained around.

Holder metadata is advisory: it is written into the slot file for humans
and for the dashboard, and every reader re-tests the lock rather than
trusting the JSON, so a crashed holder's stale record can never make a free
slot look busy.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "SlotSemaphore",
    "SlotTimeout",
    "SlotState",
    "default_lock_dir",
]

#: Where slot files live under ``data_dir``.
LOCK_SUBDIR = ("locks", "test-slots")


class SlotTimeout(RuntimeError):
    """Raised when no slot became free within the caller's timeout."""


def default_lock_dir(config=None, *, data_dir: str | None = None) -> Path:
    """``<data_dir>/locks/test-slots``.

    Takes either an :class:`~src.config.AppConfig` or a bare ``data_dir``.
    Falls back to ``~/.agent-queue`` so ``aq test`` works from a worktree
    whose config the CLI could not load — the lock directory only has to be
    the *same* path for every agent on the box, not a correct one.
    """
    if data_dir is None and config is not None:
        data_dir = getattr(config, "data_dir", None)
    base = Path(os.path.expanduser(data_dir or "~/.agent-queue"))
    return base.joinpath(*LOCK_SUBDIR)


@dataclass
class SlotState:
    """One slot as an observer sees it."""

    slot: int
    held: bool
    holder: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"slot": self.slot, "held": self.held, "holder": self.holder}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _read_json(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class SlotSemaphore:
    """``slots`` interchangeable slots, one flock file each.

    Not reentrant and not thread-safe: one process holds at most one slot,
    which is the whole point — an agent that could take two would be back to
    saturating the box.
    """

    def __init__(self, lock_dir: str | os.PathLike[str], slots: int) -> None:
        self.lock_dir = Path(lock_dir)
        self.slots = max(1, int(slots))

    # -- paths -------------------------------------------------------------

    def slot_path(self, slot: int) -> Path:
        return self.lock_dir / f"slot-{slot}.lock"

    @property
    def waiters_dir(self) -> Path:
        return self.lock_dir / "waiters"

    def _ensure_dirs(self) -> None:
        self.waiters_dir.mkdir(parents=True, exist_ok=True)

    # -- acquire -----------------------------------------------------------

    def _try_slot(self, slot: int, meta: Mapping[str, object] | None) -> int | None:
        """Take *slot* non-blockingly.  Returns the held fd, or ``None``.

        The fd is deliberately left open and marked inheritable: the lock
        lives on the descriptor, so keeping it open for the duration of the
        run *is* the hold, and making it inheritable means an ``exec``ed
        child keeps holding it.
        """
        path = self.slot_path(slot)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return None
            raise
        try:
            os.ftruncate(fd, 0)
            record = {
                "slot": slot,
                "pid": os.getpid(),
                "since": time.time(),
                **dict(meta or {}),
            }
            os.write(fd, json.dumps(record).encode("utf-8"))
            os.set_inheritable(fd, True)
        except OSError:
            os.close(fd)
            raise
        return fd

    def try_acquire(self, meta: Mapping[str, object] | None = None) -> tuple[int, int] | None:
        """One pass over every slot.  Returns ``(slot, fd)`` or ``None``."""
        self._ensure_dirs()
        for slot in range(self.slots):
            fd = self._try_slot(slot, meta)
            if fd is not None:
                return slot, fd
        return None

    @contextmanager
    def acquire(
        self,
        *,
        timeout: float | None = None,
        poll: float = 2.0,
        meta: Mapping[str, object] | None = None,
        on_wait=None,
    ) -> Iterator[int]:
        """Hold one slot for the duration of the ``with`` block.

        ``on_wait(waited_seconds, snapshot)`` is called once per poll while
        blocked, so a caller can keep the human (and the dashboard) informed
        — an agent stuck behind a busy box must look *queued*, not hung.

        Raises :class:`SlotTimeout` when *timeout* elapses first.  A timeout
        of ``0`` means "try once".
        """
        self._ensure_dirs()
        started = time.monotonic()
        waiter = self.waiters_dir / f"{os.getpid()}.json"
        held: tuple[int, int] | None = self.try_acquire(meta)
        if held is None:
            self._write_waiter(waiter, meta)
            try:
                while held is None:
                    waited = time.monotonic() - started
                    if timeout is not None and waited >= timeout:
                        raise SlotTimeout(
                            f"no test slot free after {waited:.0f}s "
                            f"({self.slots} slot(s) in {self.lock_dir})"
                        )
                    if on_wait is not None:
                        on_wait(waited, self.snapshot())
                    time.sleep(max(0.05, poll))
                    held = self.try_acquire(meta)
            finally:
                waiter.unlink(missing_ok=True)
        slot, fd = held
        try:
            yield slot
        finally:
            # Closing the descriptor releases the flock.  Blanking the file
            # first keeps `aq test --aq-status` from showing a ghost holder
            # in the window before the next acquirer overwrites it.
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
            os.close(fd)

    def _write_waiter(self, path: Path, meta: Mapping[str, object] | None) -> None:
        record = {"pid": os.getpid(), "since": time.time(), **dict(meta or {})}
        try:
            path.write_text(json.dumps(record), encoding="utf-8")
        except OSError:  # pragma: no cover - unwritable lock dir
            logger.debug("could not record test-slot waiter at %s", path)

    # -- observation -------------------------------------------------------

    def _slot_state(self, slot: int) -> SlotState:
        path = self.slot_path(slot)
        if not path.exists():
            return SlotState(slot=slot, held=False)
        holder = _read_json(path)
        # Re-test the lock rather than trusting the record: a killed holder
        # leaves its JSON behind, and reporting that as busy would make the
        # semaphore look full forever.
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return SlotState(slot=slot, held=False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return SlotState(slot=slot, held=True, holder=holder)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return SlotState(slot=slot, held=False)
        finally:
            os.close(fd)

    def snapshot(self) -> dict:
        """``{"slots": [...], "waiting": [...], "free": n}`` for humans."""
        states = [self._slot_state(i) for i in range(self.slots)]
        waiting = []
        for path in sorted(self.waiters_dir.glob("*.json")) if self.waiters_dir.exists() else []:
            record = _read_json(path)
            pid = record.get("pid")
            if isinstance(pid, int) and not _pid_alive(pid):
                path.unlink(missing_ok=True)
                continue
            waiting.append(record)
        return {
            "lock_dir": str(self.lock_dir),
            "slots": [s.to_dict() for s in states],
            "waiting": waiting,
            "free": sum(1 for s in states if not s.held),
            "total": len(states),
        }
