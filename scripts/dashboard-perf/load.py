#!/usr/bin/env python3
"""Bounded synthetic load for the dashboard performance experiment (spec §4.2).

CPU workers spin a fixed amount of integer arithmetic in child processes; the
parent writes, fsyncs and reads back files in a private temporary directory
that never holds more than the cap (at most 256 MiB).  Work is fixed
(iterations, bytes) rather than time, so throughput is comparable across
modes; ``seconds`` is a hard deadline that every worker enforces on its own,
so even a killed parent cannot leave a worker running past it.  Every exit the
process gets to see — completion, deadline, SIGTERM, SIGINT — removes the
directory and prints the summary; only SIGKILL can leave it behind.

Launch it the way the daemon launches an agent so it runs at session niceness
with the session caps: ``wrapped_argv(config, [sys.executable, __file__, ...])``.
``nice -n`` is an increment, so a helper launched from a process that is
already niced (a worker session runs at +10) lands higher than an agent does;
the summary's ``nice`` field records what it actually ran at.

The defaults are a starting point, not a calibration.  On the reference host
(24 cores, 2026-09-25) one worker completes roughly 8-9 million iterations a
second, so the default 200 million over four workers is about six seconds of
CPU, and the default gibibyte of IO a few seconds more — far short of a
120-second window.  An experiment sizes ``--iterations`` and ``--io-bytes`` to
its own observation window, keeps them identical across the modes it
compares, and records them in its manifest.

Nothing here touches a database.  Database load, when an experiment measures
it, belongs on an isolated test PostgreSQL — never the operator's.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SECONDS = 120.0
DEFAULT_ITERATIONS = 200_000_000
DEFAULT_IO_BYTES = 1 << 30
DEFAULT_TMP_MAX_MIB = 256
#: The spec's ceiling on the temporary directory; ``--tmp-max-mib`` may only lower it.
MAX_TMP_MIB = 256
FILE_BYTES = 8 << 20
_CHUNK = 100_000
#: How long a worker gets to report its count after the deadline or a stop request.
_GRACE_S = 5.0

_stop_requested = False


class _Interrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def cpu_work(iterations: int, deadline: float) -> int:
    """Spin ``iterations`` LCG steps; stop early at ``deadline``.  Returns steps completed."""
    x, done = 1, 0
    while done < iterations:
        step = min(_CHUNK, iterations - done)
        for _ in range(step):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        done += step
        if _stop_requested or time.monotonic() >= deadline:
            break
    return done


def io_work(directory: Path, max_bytes: int, total_bytes: int, deadline: float,
            *, file_bytes: int = FILE_BYTES, stats: dict | None = None) -> dict:
    """Write ``total_bytes`` as fsynced files, never holding more than ``max_bytes`` on disk.

    The oldest file is deleted before a new one would cross the cap, and each
    file is read back after it is written.  ``stats`` (when given) is updated
    in place as the work proceeds, so an interrupted caller still has the
    counts; it is also the return value.
    """
    if file_bytes <= 0 or file_bytes > max_bytes:
        raise ValueError("file_bytes must be positive and must not exceed max_bytes")
    out = stats if stats is not None else {}
    out.update(bytes_written=0, peak_bytes_on_disk=0, files=0)
    payload = os.urandom(min(file_bytes, 1 << 20))
    files: list[tuple[Path, int]] = []
    on_disk = 0
    while out["bytes_written"] < total_bytes and time.monotonic() < deadline:
        size = min(file_bytes, total_bytes - out["bytes_written"])
        while files and on_disk + size > max_bytes:
            victim, victim_size = files.pop(0)
            victim.unlink()
            on_disk -= victim_size
        path = directory / f"load-{out['files']:06d}.bin"
        with path.open("wb") as handle:
            remaining = size
            while remaining > 0:
                chunk = payload[:remaining]
                handle.write(chunk)
                remaining -= len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        files.append((path, size))
        on_disk += size
        out["files"] += 1
        out["bytes_written"] += size
        out["peak_bytes_on_disk"] = max(out["peak_bytes_on_disk"], on_disk)
        with path.open("rb") as handle:
            while handle.read(1 << 20):
                pass
    return out


def _shares(iterations: int, workers: int) -> list[int]:
    base, extra = divmod(iterations, workers)
    return [base + (1 if index < extra else 0) for index in range(workers)]


def _spawn_cpu_worker(iterations: int, seconds: float) -> subprocess.Popen:
    # A child process, not a fork: the helper may be imported into a process
    # that has threads (pytest, an orchestrator), and a worker re-executing
    # this file needs nothing but the standard library.
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--cpu-worker",
         "--iterations", str(iterations), "--seconds", repr(max(0.0, seconds))],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
    )


def _collect(proc: subprocess.Popen, timeout: float) -> int:
    """The worker's reported count, stopping it if it overruns ``timeout``."""
    try:
        stdout, _ = proc.communicate(timeout=max(0.0, timeout))
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            stdout, _ = proc.communicate(timeout=_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return 0
    try:
        return int(json.loads(stdout or "{}").get("completed", 0))
    except (ValueError, TypeError, AttributeError):
        return 0


def _validate(*, seconds: float, iterations: int, workers: int, tmp_max_mib: int,
              io_bytes: int, file_bytes: int) -> None:
    if not seconds > 0:
        raise ValueError("seconds must be positive")
    if iterations < 0 or io_bytes < 0:
        raise ValueError("iterations and io_bytes must not be negative")
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if not 1 <= tmp_max_mib <= MAX_TMP_MIB:
        raise ValueError(f"tmp_max_mib must be between 1 and {MAX_TMP_MIB}")
    if not 0 < file_bytes <= tmp_max_mib << 20:
        raise ValueError("file_bytes must be positive and fit under tmp_max_mib")


def run(*, seconds: float, iterations: int, workers: int, tmp_max_mib: int, io_bytes: int,
        tmp_root: str | None = None, file_bytes: int = FILE_BYTES) -> dict:
    """Do the fixed work, bounded by ``seconds``, and summarise what got done.

    ``timed_out`` is true when the work was cut short (by the deadline or a
    signal); ``interrupted`` names the signal, if one arrived.  The temporary
    directory is gone on return whatever happened.
    """
    seconds, iterations, workers = float(seconds), int(iterations), int(workers)
    tmp_max_mib, io_bytes, file_bytes = int(tmp_max_mib), int(io_bytes), int(file_bytes)
    _validate(seconds=seconds, iterations=iterations, workers=workers, tmp_max_mib=tmp_max_mib,
              io_bytes=io_bytes, file_bytes=file_bytes)
    started = time.monotonic()
    deadline = started + seconds
    procs: list[subprocess.Popen] = []
    io: dict = {"bytes_written": 0, "peak_bytes_on_disk": 0, "files": 0}
    completed = 0
    tmp: Path | None = None
    elapsed: float | None = None
    # The first signal unwinds the work; any later one (or one that lands
    # during cleanup) is only recorded, so nothing can abandon the cleanup.
    state: dict = {"signum": None, "raise": True}

    def on_signal(signum, _frame):
        if state["signum"] is None:
            state["signum"] = signum
        if state["raise"]:
            state["raise"] = False
            raise _Interrupted(signum)

    previous = {}
    if threading.current_thread() is threading.main_thread():
        previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        tmp = Path(tempfile.mkdtemp(prefix="aq-perf-load-", dir=tmp_root))
        for share in _shares(iterations, workers):
            if share > 0:
                procs.append(_spawn_cpu_worker(share, deadline - time.monotonic()))
        if io_bytes > 0:
            io_work(tmp, tmp_max_mib << 20, io_bytes, deadline, file_bytes=file_bytes, stats=io)
        for proc in procs:
            completed += _collect(proc, deadline - time.monotonic() + _GRACE_S)
        elapsed = time.monotonic() - started
    except _Interrupted:
        pass
    finally:
        state["raise"] = False
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()  # the worker reports its partial count and exits
        if state["signum"] is not None:
            # Workers already collected have a closed stdout; the rest report now.
            pending = [proc for proc in procs if proc.stdout and not proc.stdout.closed]
            completed += sum(_collect(proc, _GRACE_S) for proc in pending)
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if elapsed is None:
        elapsed = time.monotonic() - started
    interrupted = state["signum"]
    return {
        "requested_iterations": iterations,
        "completed_iterations": completed,
        "workers": workers,
        "io": {**io, "requested_bytes": io_bytes, "max_bytes": tmp_max_mib << 20},
        "elapsed_s": round(elapsed, 3),
        "nice": os.nice(0),
        "timed_out": completed < iterations or io["bytes_written"] < io_bytes,
        "interrupted": signal.Signals(interrupted).name if interrupted is not None else None,
        "throughput_iter_per_s": round(completed / elapsed, 1) if elapsed > 0 else None,
        "tmp_dir": str(tmp) if tmp is not None else None,
        "tmp_dir_removed": tmp is None or not tmp.exists(),
    }


def wrapped_argv(config, argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """The argv and environment the daemon would give an agent session."""
    from src.resources.limits import session_env_caps, wrap_session_argv

    return list(wrap_session_argv(argv, config)), {**os.environ, **session_env_caps(config)}


def _cpu_worker_main(iterations: int, seconds: float) -> int:
    def request_stop(_signum, _frame):
        global _stop_requested
        _stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    # Ctrl-C reaches the whole process group; the parent decides when we stop.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    done = cpu_work(iterations, time.monotonic() + seconds)
    print(json.dumps({"completed": done}), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                        help="hard deadline (default %(default)s)")
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS,
                        help="total CPU work, split across workers (default %(default)s)")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--io-bytes", type=int, default=DEFAULT_IO_BYTES,
                        help="total bytes to write, fsync and read back (default %(default)s)")
    parser.add_argument("--tmp-max-mib", type=int, default=DEFAULT_TMP_MAX_MIB,
                        help=f"temporary directory cap, at most {MAX_TMP_MIB} (default %(default)s)")
    parser.add_argument("--file-bytes", type=int, default=FILE_BYTES)
    parser.add_argument("--tmp-root", default=None,
                        help="parent of the private temporary directory (default: system temp)")
    parser.add_argument("--cpu-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.cpu_worker:
        return _cpu_worker_main(args.iterations, args.seconds)
    try:
        out = run(seconds=args.seconds, iterations=args.iterations, workers=args.workers,
                  tmp_max_mib=args.tmp_max_mib, io_bytes=args.io_bytes, tmp_root=args.tmp_root,
                  file_bytes=args.file_bytes)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(out), flush=True)
    return 128 + signal.Signals[out["interrupted"]].value if out["interrupted"] else 0


if __name__ == "__main__":
    sys.exit(main())
