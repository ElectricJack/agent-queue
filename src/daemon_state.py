"""The daemon's on-disk state, readable without loading the CLI.

Two things live here, both needed by ``aq start`` / ``aq stop``
(:mod:`src.cli.daemon`) *and* by the auto-restart watchdog
(:mod:`src.install.watchdog`), which runs from cron every few minutes and must
not pay the two seconds it costs to import the whole ``aq`` command tree:

**Which daemon is running.**  ``daemon.pid`` names it; :func:`read_daemon_pid`
also proves the PID still belongs to the daemon, because after a reboot the
same number is soon handed to some other process.

**The operator's stop intent.**  The watchdog starts the daemon when it finds
it down.  A daemon that is down because somebody *meant* it to be --
``aq stop``, the stop half of ``aq restart``, the stop that begins an
``aq update``, or the daemon's own ``shutdown`` command -- must not be brought
back behind their back.  Every one of those writes ``daemon.stopped`` before
the daemon goes away, and ``aq start`` removes it before starting one, so the
rule is simply: **the watchdog never starts a daemon while the marker
exists.**  A crash writes nothing, which is exactly what makes a crash
distinguishable from a stop, and the marker survives a reboot.

This module is a **leaf**: the CLI, the daemon (the ``shutdown`` command) and
the watchdog import it, and it imports none of them.  Standard library only,
plus YAML where a configuration has to be read.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "LOCK_ABANDONED",
    "LOCK_ABSENT",
    "LOCK_HELD",
    "START_LOCK_OWNER",
    "STOPPED_EXIT_CODE",
    "STOP_INTENT_FILENAME",
    "StopIntent",
    "acquire_start_lock",
    "clear_stop_intent",
    "configured_database_endpoint",
    "database_reachable",
    "default_stop_intent_path",
    "find_daemon_pid",
    "pid_is_foreign",
    "read_daemon_pid",
    "read_stop_intent",
    "record_stop_intent",
    "release_start_lock",
    "start_lock_state",
]

#: Beside ``daemon.pid`` in the AQ home (``~/.agent-queue``).
STOP_INTENT_FILENAME = "daemon.stopped"

#: ``aq start --unless-stopped`` (the watchdog's start) exits with this when a
#: deliberate stop is recorded -- before it began, or while it was starting.
#: It is "left down on purpose", not a failure.
STOPPED_EXIT_CODE = 16


# ---------------------------------------------------------------------------
# The start lock
# ---------------------------------------------------------------------------

#: ``aq start`` holds ``daemon.lock`` (a directory) for the whole start; the
#: PID of the process holding it is written inside, so a lock whose owner died
#: -- a start that was killed -- is recognisably abandoned rather than merely
#: old.  A long pre-migration backup is old but very much alive.
START_LOCK_OWNER = "owner"
LOCK_ABSENT = "absent"
LOCK_HELD = "held"
LOCK_ABANDONED = "abandoned"
#: A lock with no owner file (written by a release that predates it) counts as
#: abandoned only after this long.
_OWNERLESS_LOCK_STALE_AFTER = 1800.0


def acquire_start_lock(lock_dir: str) -> bool:
    """Take the start lock; ``False`` when another start holds it."""
    try:
        os.makedirs(lock_dir)
    except FileExistsError:
        return False
    try:
        with open(os.path.join(lock_dir, START_LOCK_OWNER), "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        pass
    return True


def release_start_lock(lock_dir: str) -> None:
    """Remove the start lock and its owner note, if they are there."""
    _remove(os.path.join(lock_dir, START_LOCK_OWNER))
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass


def start_lock_state(lock_dir: str, *, now: float | None = None) -> str:
    """:data:`LOCK_ABSENT`, :data:`LOCK_HELD` or :data:`LOCK_ABANDONED`."""
    try:
        age = (time.time() if now is None else now) - os.stat(lock_dir).st_mtime
    except OSError:
        return LOCK_ABSENT
    try:
        with open(os.path.join(lock_dir, START_LOCK_OWNER), encoding="utf-8") as handle:
            owner = int(handle.read().strip())
    except (OSError, ValueError):
        return LOCK_ABANDONED if age > _OWNERLESS_LOCK_STALE_AFTER else LOCK_HELD
    try:
        os.kill(owner, 0)
    except ProcessLookupError:
        return LOCK_ABANDONED
    except OSError:
        return LOCK_HELD  # alive, owned by someone else
    return LOCK_HELD


# ---------------------------------------------------------------------------
# Which daemon is running
# ---------------------------------------------------------------------------


def pid_is_foreign(pid: int, config_path: str) -> bool:
    """Whether *pid* is provably some other program than the daemon.

    After a reboot ``daemon.pid`` still names the old process, and on a quiet
    box the same PID is soon handed to something else.  Trusting it made
    ``aq start`` report "already running" for a daemon that did not exist --
    and the watchdog, which starts the daemon through ``aq start``, would have
    done the same forever.  ``/proc`` answers the question on Linux; where it
    cannot be read (macOS) the PID is trusted as before.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return False
    argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    if not argv:
        return False  # a zombie or a kernel thread reads empty: not conclusive
    # Whole arguments, not substrings: every process started under a checkout
    # named ``agent-queue2`` has "agent-queue" somewhere in its argv.
    return not any(
        os.path.basename(part) == "agent-queue" or part in ("src.main", config_path)
        for part in argv
    )


def read_daemon_pid(pid_file: str, config_path: str) -> int | None:
    """The PID in *pid_file* if that process is alive and is the daemon.

    A stale file -- a dead process, or a PID since reused by something else --
    is removed, as ``aq start`` always has for the first case.
    """
    if not os.path.exists(pid_file):
        return None
    try:
        with open(pid_file, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        _remove(pid_file)
        return None
    if pid_is_foreign(pid, config_path):
        _remove(pid_file)
        return None
    return pid


def find_daemon_pid(pid_file: str, config_path: str) -> int | None:
    """The running daemon's PID, from *pid_file* or else from the process table.

    A dashboard server receives the same config path, so matching only the
    checkout name and config can mistake it for the daemon.  The daemon is
    always launched as ``agent-queue <config>``; require that exact argv suffix
    when recovering without a PID file.
    """
    pid = read_daemon_pid(pid_file, config_path)
    if pid:
        return pid
    try:
        result = subprocess.run(
            ["pgrep", "-f", rf"(^|/)agent-queue {re.escape(config_path)}$"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().split()[0])
    except (FileNotFoundError, ValueError):
        pass
    return None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Where the database is
# ---------------------------------------------------------------------------


def configured_database_endpoint(config_path: str) -> tuple[str, int] | None:
    """The host and port of the configured database, or ``None`` if unreadable.

    Read from the raw YAML rather than through ``load_config``: this runs
    before the daemon and must not depend on the whole configuration being
    valid, and it deliberately never looks at the password.
    """
    import yaml

    try:
        with open(config_path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return None
    section = raw.get("database") if isinstance(raw, dict) else None
    url = section.get("url") if isinstance(section, dict) else None
    if not isinstance(url, str) or not url:
        return None
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        return (parts.hostname or "localhost", int(parts.port or 5432))
    except ValueError:
        return None


def database_reachable(config_path: str, *, timeout: float = 2.0) -> bool:
    """True when something is already listening where the configuration points."""
    endpoint = configured_database_endpoint(config_path)
    if endpoint is None:
        return False
    host, port = endpoint
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The stop intent
# ---------------------------------------------------------------------------


def default_stop_intent_path() -> Path:
    """``~/.agent-queue/daemon.stopped`` -- the same home ``aq start`` uses."""
    return Path(os.path.expanduser("~/.agent-queue")) / STOP_INTENT_FILENAME


@dataclass(frozen=True, slots=True)
class StopIntent:
    """Who stopped the daemon on purpose, and when."""

    by: str
    at: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"by": self.by, "at": self.at, "reason": self.reason}


def record_stop_intent(by: str, *, reason: str = "", path: Path | str | None = None) -> None:
    """Mark the daemon as deliberately stopped.

    Written *before* the daemon is signalled, so there is no window in which a
    watchdog sees a dead daemon and no marker.  Best effort: a stop must never
    fail because its note could not be written, so an unwritable home is
    swallowed (and the watchdog then treats the stop like a crash).
    """
    target = Path(path) if path is not None else default_stop_intent_path()
    payload = {"by": by, "at": time.time(), "reason": reason, "pid": os.getpid()}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


def clear_stop_intent(*, path: Path | str | None = None) -> bool:
    """Forget a recorded stop.  Returns whether one was there."""
    target = Path(path) if path is not None else default_stop_intent_path()
    try:
        target.unlink()
        return True
    except OSError:
        return False


def read_stop_intent(*, path: Path | str | None = None) -> StopIntent | None:
    """The recorded stop, or ``None`` when the daemon is meant to run.

    A marker that exists but cannot be parsed still counts as a stop: the file
    only ever exists because something stopped the daemon on purpose, and
    reading a torn write as "go ahead and start it" is the unsafe answer.
    """
    target = Path(path) if path is not None else default_stop_intent_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return StopIntent(by="unknown", at=_mtime(target))
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return StopIntent(by="unknown", at=_mtime(target))
    try:
        at = float(payload.get("at") or 0.0)
    except (TypeError, ValueError):
        at = _mtime(target)
    return StopIntent(
        by=str(payload.get("by") or "unknown"),
        at=at,
        reason=str(payload.get("reason") or ""),
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
