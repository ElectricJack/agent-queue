"""``daemon.autostart`` -- is auto-restart installed, and is it working?

The watchdog (``aq service``, :mod:`src.install.service`) is what brings the
daemon back after a reboot or a crash.  This check reads the same things
``aq service status`` reads -- the install record, the service manager's own
answer and the watchdog's last check -- and asks the service manager off the
event loop (``asyncio`` subprocesses), then interprets the answers with
:func:`src.install.service.interpret_status` so the two can never disagree.

* not installed      -- ``info``: a supported choice, but nothing restarts the
  daemon; the detail names ``aq service install``.
* installed, working -- ``ok``, with the mechanism and the last check.
* installed, broken  -- ``warn``: disabled or unloaded entry, cron not running,
  a watchdog that stopped checking in, or one that gave up after failed starts.

Not fixable here: installing a service is the operator's decision, made from
their own shell (whose PATH the service records), never the daemon's.
"""

from __future__ import annotations

import asyncio
import sys
import time

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "install"
AUTOSTART = "daemon.autostart"

_PROBE_SECONDS = 10.0


async def _run(argv: list[str]):
    """One read-only probe as a :class:`CommandOutput`, without blocking the loop."""
    from src.install.command import CommandOutput

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return CommandOutput(argv=tuple(argv), error=f"{argv[0]} is not installed")
    except OSError as error:
        return CommandOutput(argv=tuple(argv), error=f"{argv[0]} could not be run: {error}")
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=_PROBE_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return CommandOutput(
            argv=tuple(argv), error=f"{argv[0]} did not finish within {_PROBE_SECONDS:g}s"
        )
    return CommandOutput(
        argv=tuple(argv),
        returncode=process.returncode,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


async def _check_autostart(ctx: DoctorContext) -> CheckResult:
    from src.daemon_state import STOP_INTENT_FILENAME, read_stop_intent
    from src.install.service import (
        ServicePaths,
        current_user,
        interpret_status,
        linger_enabled,
        read_record,
        status_commands,
    )
    from src.install.watchdog import load_state

    paths = ServicePaths.for_host()
    system = "darwin" if sys.platform == "darwin" else "linux"

    def _files():
        return (
            read_record(paths),
            load_state(paths.aq_home),
            read_stop_intent(path=paths.aq_home / STOP_INTENT_FILENAME),
            linger_enabled(current_user()) if system == "linux" else None,
        )

    record, watchdog, intent, linger = await asyncio.to_thread(_files)
    commands = status_commands(paths, system=system, record=record)
    answers = await asyncio.gather(*(_run(argv) for argv in commands.values()))
    status = interpret_status(
        paths,
        record=record,
        outputs=dict(zip(commands, answers, strict=True)),
        watchdog=watchdog,
        now=time.time(),
        linger=linger,
        stop_intent=intent,
    )
    data = status.to_dict()
    if not status.installed:
        return CheckResult(
            id=AUTOSTART,
            severity=Severity.INFO,
            detail=(
                "auto-restart is not installed: nothing starts the daemon after a reboot or "
                "a crash. `aq service install` (or `aq install --with autostart`) adds it."
            ),
            data=data,
        )
    if status.healthy:
        notes = f" ({'; '.join(status.notes)})" if status.notes else ""
        return CheckResult(id=AUTOSTART, severity=Severity.OK, detail=status.summary + notes, data=data)
    return CheckResult(
        id=AUTOSTART,
        severity=Severity.WARN,
        detail="; ".join((status.summary, *status.problems[1:])),
        data=data,
    )


def service_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id=AUTOSTART, run=_check_autostart, timeout_s=20.0, owner=OWNER)]


CHECKS = service_checks()
