"""The auto-restart policy: when the service may start the daemon, and when not.

``aq service install`` registers a *watchdog* with the host's service manager
(:mod:`src.install.service`): a systemd user unit or a launchd agent running
``aq service run`` (a loop), or cron entries running ``aq service check`` (one
check per entry).  Every one of them runs the same :func:`tick`, so the rules
below hold whatever mechanism the host offered.

The service never runs the daemon as its own child.  The daemon stays what
``aq start`` makes it -- a detached process with a PID file whose agent tmux
sessions outlive it -- and ``aq start`` / ``aq stop`` / ``aq restart`` /
``aq update`` keep working exactly as before.  The watchdog only ever *starts*
a daemon that is not running, through ``aq start`` in a scrubbed environment;
it never stops, restarts or signals a live one.

The rules, in the order :func:`decide` applies them:

1. **A running daemon is left alone**, and resets every counter.
2. **A deliberate stop stays stopped.**  ``aq stop``, the stop half of
   ``aq restart`` and ``aq update``, and the daemon's ``shutdown`` command
   write :mod:`src.daemon_state`'s marker; only ``aq start`` removes it.
3. **A start or update in progress is not raced** (``daemon.lock``,
   ``update.lock``).
4. **Down is confirmed**, not assumed: two consecutive checks must find no
   daemon before anything is started (a boot check, which has no earlier
   observation to confirm, skips this).
5. **The database is waited for** -- up to :data:`DATABASE_WAIT` seconds from
   the first down observation -- before ``aq start`` is tried.
6. **Failures back off** exponentially, a crash loop is rate limited, and after
   :data:`MAX_START_FAILURES` consecutive failed starts the watchdog stops
   trying until the daemon is seen running again (``aq start``) or the
   operator runs ``aq service check --reset``.

Everything the watchdog decides is logged, one line per change, to
``<home>/logs/aq-service.log``, and its state is ``<home>/service/state.json``
-- what ``aq service status`` and the ``daemon.autostart`` doctor check read.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from src.daemon_state import (
    STOP_INTENT_FILENAME,
    StopIntent,
    database_reachable,
    find_daemon_pid,
    read_stop_intent,
)

from .command import CommandOutput, CommandRunner, run_command

#: ``<home>/service/`` holds the watchdog's state, its lock and the install record.
SERVICE_DIRNAME = "service"
STATE_FILENAME = "state.json"
LOCK_FILENAME = "watchdog.lock"
#: ``<home>/logs/aq-service.log`` -- beside the daemon's own ``agent-queue.log``.
LOG_FILENAME = "aq-service.log"
#: The log is rotated to ``aq-service.log.1`` past this size.
LOG_MAX_BYTES = 1024 * 1024

#: Seconds between checks in ``aq service run``.
DEFAULT_INTERVAL = 30.0
#: Consecutive checks that must find the daemon down before it is started.
CONFIRMATIONS = 2
#: How long to wait for the database from the first down observation.
DATABASE_WAIT = 180.0
#: The first retry after a failed start, doubled per further failure.
BACKOFF_BASE = 60.0
BACKOFF_MAX = 1800.0
#: Consecutive failed starts before the watchdog stops trying.
MAX_START_FAILURES = 5
#: At most this many automatic starts in :data:`CRASH_WINDOW` seconds.
MAX_STARTS_PER_WINDOW = 5
CRASH_WINDOW = 3600.0
#: A gap this long between two checks breaks a down series (a reboot, or the
#: service being off): what was observed before it no longer confirms anything.
SERIES_GAP_MIN = 600.0

#: How long ``aq start`` may take: waiting for PostgreSQL, a pre-migration
#: backup, ``/health``, ``/ready`` and the post-start pool fix.
START_TIMEOUT = 900.0


class Verdict(str, Enum):
    """What one check concluded."""

    RUNNING = "running"
    STOPPED = "stopped"
    BUSY = "busy"
    UNCONFIGURED = "unconfigured"
    DOWN = "down"
    WAITING_FOR_DATABASE = "waiting_for_database"
    BACKOFF = "backoff"
    CRASH_LOOP = "crash_loop"
    GAVE_UP = "gave_up"
    START = "start"
    STARTED = "started"
    START_FAILED = "start_failed"
    SKIPPED = "skipped"

    @property
    def holding(self) -> bool:
        """The daemon is down and the watchdog is deliberately not starting it."""
        return self in {
            Verdict.STOPPED,
            Verdict.BUSY,
            Verdict.UNCONFIGURED,
            Verdict.BACKOFF,
            Verdict.CRASH_LOOP,
            Verdict.GAVE_UP,
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """What one check saw.  ``database_ready`` is ``None`` when not probed."""

    now: float
    daemon_pid: int | None = None
    stop_intent: StopIntent | None = None
    busy: str | None = None
    config_present: bool = True
    database_ready: bool | None = None


@dataclass(slots=True)
class WatchdogState:
    """What the watchdog remembers between checks (``service/state.json``)."""

    last_check: float = 0.0
    verdict: str = ""
    detail: str = ""
    down_since: float | None = None
    down_checks: int = 0
    start_failures: int = 0
    next_attempt_at: float = 0.0
    starts: list[float] = field(default_factory=list)
    last_start: dict[str, Any] | None = None
    #: Who last checked: ``loop`` (``aq service run``) or ``check`` (cron).
    mode: str = ""
    interval: float = DEFAULT_INTERVAL
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> WatchdogState:
        if not isinstance(payload, dict):
            return cls()
        known = {name: payload[name] for name in cls.__slots__ if name in payload}
        try:
            state = cls(**known)
        except TypeError:
            return cls()
        state.starts = [float(value) for value in (state.starts or []) if _number(value)]
        return state


def _number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def backoff_seconds(failures: int) -> float:
    """The wait after the *failures*-th consecutive failed start."""
    if failures <= 0:
        return 0.0
    return min(BACKOFF_BASE * (2 ** (failures - 1)), BACKOFF_MAX)


def _reset_down(state: WatchdogState) -> None:
    state.down_since = None
    state.down_checks = 0


def decide(
    observation: Observation,
    previous: WatchdogState,
    *,
    boot: bool = False,
    interval: float = DEFAULT_INTERVAL,
) -> tuple[Verdict, str, WatchdogState]:
    """Apply the rules to one observation.  Pure: returns the new state.

    ``START`` means "start the daemon now"; the caller does, then reports the
    outcome through :func:`record_start`.
    """
    now = observation.now
    state = replace(previous, starts=list(previous.starts))
    gap = now - state.last_check if state.last_check else 0.0
    if state.last_check and gap > max(3 * interval, SERIES_GAP_MIN):
        _reset_down(state)
    state.last_check = now
    state.starts = [at for at in state.starts if now - at < CRASH_WINDOW]

    if observation.daemon_pid is not None:
        _reset_down(state)
        state.start_failures = 0
        state.next_attempt_at = 0.0
        return Verdict.RUNNING, f"daemon running (PID {observation.daemon_pid})", state

    if observation.stop_intent is not None:
        # The operator has spoken; whatever failed before is theirs to judge.
        _reset_down(state)
        state.start_failures = 0
        state.next_attempt_at = 0.0
        intent = observation.stop_intent
        when = _iso(intent.at) if intent.at else "an unknown time"
        why = f": {intent.reason}" if intent.reason else ""
        return (
            Verdict.STOPPED,
            f"stopped on purpose by {intent.by} at {when}{why}; `aq start` resumes",
            state,
        )

    if observation.busy:
        _reset_down(state)
        return Verdict.BUSY, observation.busy, state

    if not observation.config_present:
        _reset_down(state)
        return Verdict.UNCONFIGURED, "no configuration yet; run `aq install`", state

    if state.down_since is None:
        state.down_since = now
    state.down_checks += 1
    if state.down_checks < CONFIRMATIONS and not boot:
        return (
            Verdict.DOWN,
            f"daemon not running ({state.down_checks}/{CONFIRMATIONS} checks)",
            state,
        )

    if state.start_failures >= MAX_START_FAILURES:
        return (
            Verdict.GAVE_UP,
            (
                f"{state.start_failures} starts failed in a row; not trying again until the "
                "daemon is started by hand (`aq start`) or `aq service check --reset`"
            ),
            state,
        )

    if now < state.next_attempt_at:
        return (
            Verdict.BACKOFF,
            f"last start failed; next attempt in {int(state.next_attempt_at - now)}s",
            state,
        )

    if len(state.starts) >= MAX_STARTS_PER_WINDOW:
        resume = min(state.starts) + CRASH_WINDOW
        return (
            Verdict.CRASH_LOOP,
            (
                f"{len(state.starts)} automatic starts in the last hour; next attempt in "
                f"{int(resume - now)}s"
            ),
            state,
        )

    waited = now - state.down_since
    if observation.database_ready is False and waited < DATABASE_WAIT:
        return (
            Verdict.WAITING_FOR_DATABASE,
            f"database not reachable yet (waited {int(waited)}s of {int(DATABASE_WAIT)}s)",
            state,
        )

    return Verdict.START, "daemon down; starting it", state


def record_start(state: WatchdogState, *, now: float, ok: bool, message: str) -> WatchdogState:
    """Fold one ``aq start`` outcome into the state."""
    state = replace(state, starts=[*state.starts, now])
    state.last_start = {"at": now, "ok": ok, "message": message[-500:]}
    if ok:
        _reset_down(state)
        state.start_failures = 0
        state.next_attempt_at = 0.0
    else:
        state.start_failures += 1
        state.next_attempt_at = now + backoff_seconds(state.start_failures)
    return state


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------


def service_dir(home: Path) -> Path:
    return Path(home) / SERVICE_DIRNAME


def state_path(home: Path) -> Path:
    return service_dir(home) / STATE_FILENAME


def log_path(home: Path) -> Path:
    return Path(home) / "logs" / LOG_FILENAME


def load_state(home: Path) -> WatchdogState:
    try:
        payload = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WatchdogState()
    return WatchdogState.from_dict(payload)


def save_state(home: Path, state: WatchdogState) -> None:
    target = state_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def _iso(timestamp: float) -> str:
    return (
        datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def append_log(home: Path, message: str, *, now: float) -> None:
    """One timestamped line in the service log, rotated past :data:`LOG_MAX_BYTES`."""
    target = log_path(home)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > LOG_MAX_BYTES:
            os.replace(target, target.with_name(target.name + ".1"))
        with target.open("a", encoding="utf-8") as handle:
            for line in message.splitlines() or [""]:
                handle.write(f"{_iso(now)} {line}\n")
    except OSError:
        pass


@contextlib.contextmanager
def exclusive(home: Path) -> Iterator[bool]:
    """Hold the watchdog lock if nobody else does; yields whether it was taken.

    Overlapping cron checks, a manual ``aq service check`` and a running loop
    must never start two daemons, so every check runs under one ``flock``.
    """
    import fcntl

    target = service_dir(home) / LOCK_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# One check
# ---------------------------------------------------------------------------


class WatchdogHost(Protocol):
    """What a check reads from, and does to, the machine.

    :mod:`src.cli.service` binds it to ``aq start``'s own helpers; tests hand
    in a fake.
    """

    home: Path

    def daemon_pid(self) -> int | None: ...

    def stop_intent(self) -> StopIntent | None: ...

    def busy(self) -> str | None: ...

    def config_present(self) -> bool: ...

    def database_ready(self) -> bool: ...

    def start_daemon(self) -> CommandOutput: ...


@dataclass(frozen=True, slots=True)
class TickResult:
    verdict: Verdict
    detail: str
    state: WatchdogState
    output: CommandOutput | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "detail": self.detail,
            "state": self.state.to_dict(),
        }


def tick(
    host: WatchdogHost,
    *,
    now: Callable[[], float],
    boot: bool = False,
    reset: bool = False,
    mode: str = "check",
    interval: float = DEFAULT_INTERVAL,
) -> TickResult:
    """Observe, decide, act once and record it.  Never raises for a host error."""
    home = Path(host.home)
    with exclusive(home) as owned:
        if not owned:
            return TickResult(
                Verdict.SKIPPED, "another watchdog check is running", load_state(home)
            )
        previous = load_state(home)
        if reset:
            previous = replace(previous, start_failures=0, next_attempt_at=0.0, starts=[])
            append_log(home, "reset: start failures and backoff cleared", now=now())

        observation = observe(host, now=now())
        verdict, detail, state = decide(observation, previous, boot=boot, interval=interval)
        state.mode = mode
        state.interval = interval
        state.pid = os.getpid()
        if verdict.value != previous.verdict or reset:
            append_log(home, f"{verdict.value}: {detail}", now=observation.now)

        output: CommandOutput | None = None
        if verdict is Verdict.START:
            output = host.start_daemon()
            finished = now()
            ok = output.ok and host.daemon_pid() is not None
            state = record_start(state, now=finished, ok=ok, message=output.message())
            verdict = Verdict.STARTED if ok else Verdict.START_FAILED
            if ok:
                detail = "daemon started"
            else:
                detail = (
                    f"`aq start` failed ({output.message()}); retry in "
                    f"{int(state.next_attempt_at - finished)}s"
                    if state.start_failures < MAX_START_FAILURES
                    else f"`aq start` failed ({output.message()}); giving up after "
                    f"{state.start_failures} attempts"
                )
            transcript = "\n".join(
                part for part in (output.stdout.strip(), output.stderr.strip()) if part
            )
            if transcript:
                append_log(home, _tail(transcript, 40), now=finished)
            append_log(home, f"{verdict.value}: {detail}", now=finished)
        state.verdict = verdict.value
        state.detail = detail
        try:
            save_state(home, state)
        except OSError as error:
            append_log(home, f"could not save {state_path(home)}: {error}", now=now())
        return TickResult(verdict, detail, state, output)


def observe(host: WatchdogHost, *, now: float) -> Observation:
    """Read the machine, probing the database only when the answer matters."""
    pid = host.daemon_pid()
    if pid is not None:
        return Observation(now=now, daemon_pid=pid)
    intent = host.stop_intent()
    if intent is not None:
        return Observation(now=now, stop_intent=intent)
    busy = host.busy()
    if busy:
        return Observation(now=now, busy=busy)
    if not host.config_present():
        return Observation(now=now, config_present=False)
    return Observation(now=now, database_ready=host.database_ready())


def _tail(text: str, lines: int) -> str:
    kept = text.splitlines()[-lines:]
    return "\n".join(f"  | {line}" for line in kept)


# ---------------------------------------------------------------------------
# The loop (`aq service run`)
# ---------------------------------------------------------------------------

#: ``aq service run`` exits with this when its own code changed on disk, so
#: the service manager (``Restart=on-failure``, launchd ``KeepAlive``) starts
#: it again on the new code.  ``EX_TEMPFAIL``.
EXIT_RELOAD = 75


def files_fingerprint(paths: tuple[Path, ...]) -> tuple[float, ...]:
    """Modification times of *paths* (``0.0`` for a missing one)."""
    stamps: list[float] = []
    for path in paths:
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            stamps.append(0.0)
    return tuple(stamps)


def run_loop(
    host: WatchdogHost,
    *,
    now: Callable[[], float],
    wait: Callable[[float], bool],
    interval: float = DEFAULT_INTERVAL,
    code_changed: Callable[[], bool] | None = None,
) -> int:
    """Check every *interval* seconds until *wait* reports a stop request.

    *wait* sleeps for up to the given seconds and returns ``True`` when the
    loop should end (SIGTERM from ``systemctl stop`` / ``launchctl bootout``):
    that is a clean exit, ``0``, which a service manager configured to restart
    on failure leaves alone.  A check that raises is logged and the loop goes
    on -- one bad probe must not take the watchdog down with it.
    """
    home = Path(host.home)
    append_log(
        home, f"watchdog started (PID {os.getpid()}, checking every {interval:g}s)", now=now()
    )
    while True:
        try:
            tick(host, now=now, mode="loop", interval=interval)
        except Exception as error:  # noqa: BLE001 - the loop outlives one bad check
            append_log(home, f"check failed: {error!r}", now=now())
        if code_changed is not None and code_changed():
            append_log(
                home,
                "the watchdog's code changed on disk; exiting so the service manager "
                "restarts it on the new code",
                now=now(),
            )
            return EXIT_RELOAD
        if wait(interval):
            append_log(home, "watchdog stopped", now=now())
            return 0


# ---------------------------------------------------------------------------
# The real machine
# ---------------------------------------------------------------------------

#: ``aq start`` holds this directory while it starts the daemon
#: (``src.cli.daemon.LOCK_DIR``); ``aq update`` holds this file
#: (``src.install.update.LOCK_NAME``).
START_LOCK_NAME = "daemon.lock"
UPDATE_LOCK_NAME = "update.lock"
#: A start lock older than this was abandoned (a killed ``aq start``): removing
#: it is what lets the next start run, exactly as ``aq stop`` does.
START_LOCK_STALE_AFTER = 1800.0
#: An update lock older than this no longer holds the watchdog back.  It is
#: never removed here -- ``aq update`` owns it and says how to clear it.
UPDATE_LOCK_STALE_AFTER = 7200.0


def default_home() -> Path:
    """``~/.agent-queue`` -- the home ``aq start`` and ``aq stop`` use."""
    return Path(os.path.expanduser("~/.agent-queue"))


def is_worker_session(environ: Mapping[str, str]) -> bool:
    """A worker must never manage the operator's daemon (``src.cli.daemon``)."""
    return environ.get("AQ_SESSION_KIND") in {"pool", "task"} or (
        environ.get("AQ_DB_SCOPE") == "worker" and bool(environ.get("AQ_SESSION_ID"))
    )


def clean_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """*environ* without any harness or AQ session variable.

    ``aq start`` scrubs the daemon's own environment too; scrubbing here as
    well keeps ``aq start`` itself from refusing (it rejects a worker's
    markers) when a check was run by hand from inside a session.
    """
    from src.env_scrub import strip_harness_session_markers
    from src.sessions.env import DAEMON_ENV_STRIP_KEYS

    env = dict(environ)
    strip_harness_session_markers(env)
    for key in DAEMON_ENV_STRIP_KEYS:
        env.pop(key, None)
    return env


def resolve_aq(explicit: str | None = None) -> str | None:
    """The ``aq`` of this installation: beside the interpreter, else on PATH."""
    if explicit:
        return explicit
    sibling = Path(sys.executable).with_name("aq")
    if sibling.exists() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which("aq")


@dataclass
class LocalHost:
    """:class:`WatchdogHost` for this machine.

    Reads what ``aq start`` reads (``daemon.pid``, ``daemon.stopped``, the
    configured database) through :mod:`src.daemon_state`, never through the
    ``aq`` command tree, so a cron check costs a tenth of a second instead of
    the seconds a full ``aq`` import takes.
    """

    home: Path
    aq: str | None = None
    runner: CommandRunner = run_command
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    clock: Callable[[], float] = time.time

    @property
    def config_path(self) -> Path:
        return Path(self.home) / "config.yaml"

    def daemon_pid(self) -> int | None:
        return find_daemon_pid(str(Path(self.home) / "daemon.pid"), str(self.config_path))

    def stop_intent(self) -> StopIntent | None:
        return read_stop_intent(path=Path(self.home) / STOP_INTENT_FILENAME)

    def busy(self) -> str | None:
        home = Path(self.home)
        start_lock = home / START_LOCK_NAME
        age = _age(start_lock, self.clock())
        if age is not None:
            if age < START_LOCK_STALE_AFTER:
                return f"an `aq start` is in progress ({start_lock})"
            try:
                start_lock.rmdir()
            except OSError:
                return f"{start_lock} is left over from an interrupted `aq start`; remove it"
        update_lock = home / UPDATE_LOCK_NAME
        age = _age(update_lock, self.clock())
        if age is not None and age < UPDATE_LOCK_STALE_AFTER:
            return f"an `aq update` is in progress ({update_lock})"
        return None

    def config_present(self) -> bool:
        return self.config_path.exists()

    def database_ready(self) -> bool:
        return database_reachable(str(self.config_path))

    def start_daemon(self) -> CommandOutput:
        aq = self.aq or resolve_aq()
        if not aq:
            return CommandOutput(argv=("aq",), error="the `aq` command was not found")
        return self.runner(
            [aq, "start", "--no-dashboard"],
            timeout=START_TIMEOUT,
            env=clean_environment(self.environ),
        )


def _age(path: Path, now: float) -> float | None:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return None


def _code_files() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    return (here, here.with_name("command.py"), here.parents[1] / "daemon_state.py")


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m src.install.watchdog run|check`` -- what the service runs.

    ``aq service run`` / ``aq service check`` call this too; the service
    manager entries call the module directly because it starts twenty times
    faster than the ``aq`` command tree.
    """
    parser = argparse.ArgumentParser(prog="python -m src.install.watchdog")
    parser.add_argument("mode", choices=("run", "check"))
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--aq", default=None, help="the aq executable that starts the daemon")
    parser.add_argument("--boot", action="store_true", help="first check after boot")
    parser.add_argument("--reset", action="store_true", help="clear failures and backoff")
    parser.add_argument("--home", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if is_worker_session(os.environ):
        print(
            "Refused: a worker must never manage the operator's daemon; "
            "report the need to the operator instead.",
            file=sys.stderr,
        )
        return 10

    home = Path(args.home) if args.home else default_home()
    host = LocalHost(home=home, aq=args.aq)
    if args.mode == "check":
        interval = args.interval if args.interval else DEFAULT_INTERVAL
        result = tick(
            host, now=time.time, boot=args.boot, reset=args.reset, mode="check",
            interval=interval,
        )
        print(f"{result.verdict.value}: {result.detail}")
        return 1 if result.verdict is Verdict.START_FAILED else 0

    import signal
    import threading

    stop = threading.Event()

    def _request_stop(signum: int, frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    baseline = files_fingerprint(_code_files())
    return run_loop(
        host,
        now=time.time,
        wait=stop.wait,
        interval=args.interval or DEFAULT_INTERVAL,
        code_changed=lambda: files_fingerprint(_code_files()) != baseline,
    )


__all__ = [
    "BACKOFF_BASE",
    "BACKOFF_MAX",
    "CONFIRMATIONS",
    "CRASH_WINDOW",
    "DATABASE_WAIT",
    "DEFAULT_INTERVAL",
    "EXIT_RELOAD",
    "LOG_FILENAME",
    "MAX_STARTS_PER_WINDOW",
    "MAX_START_FAILURES",
    "START_TIMEOUT",
    "LocalHost",
    "Observation",
    "TickResult",
    "Verdict",
    "WatchdogHost",
    "WatchdogState",
    "append_log",
    "backoff_seconds",
    "clean_environment",
    "decide",
    "default_home",
    "exclusive",
    "files_fingerprint",
    "is_worker_session",
    "load_state",
    "log_path",
    "main",
    "observe",
    "record_start",
    "resolve_aq",
    "run_loop",
    "save_state",
    "service_dir",
    "state_path",
    "tick",
]


if __name__ == "__main__":  # pragma: no cover - exercised through the service entries
    raise SystemExit(main())
