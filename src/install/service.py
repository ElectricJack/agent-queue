"""Registering the auto-restart watchdog with the host's service manager.

``aq service install`` (and ``aq install --with autostart``) makes the daemon
come back after a reboot or a crash.  What it installs is the *watchdog* of
:mod:`src.install.watchdog`, never the daemon itself, with the best mechanism
the host offers:

``systemd``
    A user unit, ``~/.config/systemd/user/aq-watchdog.service``, running the
    watchdog loop with ``Restart=on-failure``.  ``KillMode=process``: stopping,
    restarting or removing the unit ends the watchdog only, never the daemon it
    started or the agent sessions that daemon runs.  Linger is enabled when the
    host allows it, so the unit starts at boot rather than at login.
``launchd``
    A LaunchAgent, ``~/Library/LaunchAgents/com.agent-queue.watchdog.plist``,
    with ``RunAtLoad``, ``KeepAlive`` on failure and ``AbandonProcessGroup`` for
    the same reason as ``KillMode=process``.
``cron``
    The fallback where there is no systemd user manager -- WSL is the common
    case: an ``@reboot`` check and a check every few minutes, in a marked block
    of the user's crontab that nothing else in it is touched by.

Every entry runs ``<python> -m src.install.watchdog`` with the PATH captured
at install time, and every entry's output goes to
``~/.agent-queue/logs/aq-service.log``.  The install is recorded in
``~/.agent-queue/service/install.json``; :func:`service_status` compares that
record with what the service manager reports, and is what ``aq service
status`` and the ``daemon.autostart`` doctor check show.
"""

from __future__ import annotations

import json
import math
import os
import plistlib
import shlex
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.daemon_state import STOP_INTENT_FILENAME, read_stop_intent

from .command import CommandOutput, CommandRunner, run_command
from .results import ResourceRecord, StepResult
from .steps import StepContext, StepSpec
from .watchdog import (
    DEFAULT_INTERVAL,
    INSTALL_RECORD_FILENAME,
    WatchdogState,
    default_home,
    load_state,
    log_path,
    service_dir,
    state_path,
)

MECHANISM_SYSTEMD = "systemd"
MECHANISM_LAUNCHD = "launchd"
MECHANISM_CRON = "cron"
MECHANISMS: tuple[str, ...] = (MECHANISM_SYSTEMD, MECHANISM_LAUNCHD, MECHANISM_CRON)
AUTO = "auto"

UNIT_NAME = "aq-watchdog.service"
LAUNCHD_LABEL = "com.agent-queue.watchdog"
CRON_BEGIN = (
    "# BEGIN agent-queue auto-restart (written by `aq service install`; "
    "remove with `aq service uninstall`)"
)
CRON_END = "# END agent-queue auto-restart"
RECORD_FILENAME = INSTALL_RECORD_FILENAME

#: Cron cannot run more often than once a minute, and every check is a Python
#: start: two minutes keeps it cheap and a crash still comes back in minutes.
DEFAULT_CRON_INTERVAL = 120.0
#: The watchdog loop's restart delay under systemd/launchd.
RESTART_SECONDS = 10

CAPABILITY_AUTOSTART = "autostart"
STEP_AUTOSTART = "daemon.autostart"
#: The resume-record kind ``aq uninstall`` removes (``src.install.lifecycle``).
RESOURCE_SERVICE = "service"

#: PATH entries that belong to the session ``aq service install`` happened to
#: run in, not to the machine: a harness's per-session plugin shims.
_SESSION_PATH_MARKERS = ("/.claude/plugins/",)
_STANDARD_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServicePaths:
    """Every file the service installs or reads, for one user."""

    user_home: Path
    aq_home: Path
    config_home: Path

    @classmethod
    def for_host(cls, environ: Mapping[str, str] | None = None) -> ServicePaths:
        env = os.environ if environ is None else environ
        user_home = Path(os.path.expanduser("~"))
        config_home = Path(env.get("XDG_CONFIG_HOME") or user_home / ".config")
        return cls(user_home=user_home, aq_home=default_home(), config_home=config_home)

    @property
    def unit_path(self) -> Path:
        return self.config_home / "systemd" / "user" / UNIT_NAME

    @property
    def plist_path(self) -> Path:
        return self.user_home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    @property
    def record_path(self) -> Path:
        return service_dir(self.aq_home) / RECORD_FILENAME

    @property
    def log_path(self) -> Path:
        return log_path(self.aq_home)

    def artifact_for(self, mechanism: str) -> str:
        if mechanism == MECHANISM_SYSTEMD:
            return str(self.unit_path)
        if mechanism == MECHANISM_LAUNCHD:
            return str(self.plist_path)
        return "crontab"


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """What one installation runs: which interpreter, which ``aq``, how often."""

    mechanism: str
    python: str
    aq: str
    path: str
    interval: float
    lang: str | None = None

    def watchdog_argv(self, mode: str, *, boot: bool = False) -> list[str]:
        # -P: the entries run with the home directory as their working
        # directory, and a stray ~/src must not shadow the installed package.
        argv = [self.python, "-P", "-m", "src.install.watchdog", mode]
        argv += ["--interval", f"{self.interval:g}", "--aq", self.aq]
        if boot:
            argv.append("--boot")
        return argv

    @property
    def cron_minutes(self) -> int:
        return max(1, math.ceil(self.interval / 60))


def service_path(environ: Mapping[str, str], *, aq: str | None = None) -> str:
    """The PATH the service runs with: this shell's, minus the session's own.

    A service manager starts with a bare PATH (cron: ``/usr/bin:/bin``), so the
    daemon it starts could not find ``tmux``, ``git`` or a harness CLI.  The
    operator's PATH at install time is the one ``aq start`` would have used;
    a harness's plugin shims and directories that do not exist are dropped, and
    the directory ``aq`` lives in comes first so ``aq`` and ``agent-queue``
    resolve to this installation.
    """
    entries: list[str] = [str(Path(aq).parent)] if aq else []

    def add(entry: str) -> None:
        if not entry or entry in entries:
            return
        if any(marker in entry for marker in _SESSION_PATH_MARKERS):
            return
        if not os.path.isdir(entry):
            return
        entries.append(entry)

    for entry in (environ.get("PATH") or "").split(os.pathsep):
        add(entry)
    for entry in _STANDARD_PATH:
        add(entry)
    return os.pathsep.join(entries)


def default_interval(mechanism: str) -> float:
    return DEFAULT_CRON_INTERVAL if mechanism == MECHANISM_CRON else DEFAULT_INTERVAL


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------


def _systemd_quote(value: str) -> str:
    """One double-quoted systemd word: escape quotes, backslashes and specifiers."""
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    )
    return f'"{escaped}"'


def render_systemd_unit(spec: ServiceSpec, paths: ServicePaths) -> str:
    exec_start = " ".join(_systemd_quote(part) for part in spec.watchdog_argv("run"))
    environment = [f"Environment={_systemd_quote('PATH=' + spec.path)}"]
    if spec.lang:
        environment.append(f"Environment={_systemd_quote('LANG=' + spec.lang)}")
    log = str(paths.log_path)
    output = (
        [f"StandardOutput=append:{log}", f"StandardError=append:{log}"]
        if not any(character.isspace() for character in log) and "%" not in log
        else []
    )
    lines = [
        "# Written by `aq service install`; remove it with `aq service uninstall`.",
        "# Runs AQ's watchdog, which starts the daemon at boot and after a crash and",
        "# never overrides a deliberate `aq stop`.  The daemon is not this unit's",
        "# process: KillMode=process makes stopping the unit stop the watchdog only.",
        "[Unit]",
        "Description=Agent Queue watchdog (restarts the AQ daemon at boot and after a crash)",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        f"RestartSec={RESTART_SECONDS}",
        "KillMode=process",
        "WorkingDirectory=%h",
        *environment,
        *output,
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def render_launchd_plist(spec: ServiceSpec, paths: ServicePaths) -> bytes:
    environment = {"PATH": spec.path}
    if spec.lang:
        environment["LANG"] = spec.lang
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": spec.watchdog_argv("run"),
        "RunAtLoad": True,
        # Restart the watchdog when it fails; a clean exit (launchctl bootout,
        # `aq service uninstall`) stays down.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": RESTART_SECONDS,
        # The daemon the watchdog starts must outlive the job, as with
        # systemd's KillMode=process.
        "AbandonProcessGroup": True,
        "ProcessType": "Background",
        "WorkingDirectory": str(paths.user_home),
        "EnvironmentVariables": environment,
        "StandardOutPath": str(paths.log_path),
        "StandardErrorPath": str(paths.log_path),
    }
    return plistlib.dumps(payload)


#: cron refuses a command longer than this (``man 5 crontab``: 998 bytes).
CRON_COMMAND_MAX = 998


def _cron_command(argv: Sequence[str], spec: ServiceSpec, log: Path) -> str:
    # No PATH on the line: the watchdog reads it from the install record,
    # because a WSL PATH with the Windows one appended is longer than cron
    # accepts for a whole command.
    command = " ".join(shlex.quote(part) for part in argv)
    # The watchdog writes its own log lines; only an unexpected traceback on
    # stderr is worth appending, never a verdict line every few minutes.
    line = f"{command} > /dev/null 2>> {shlex.quote(str(log))}"
    # `%` ends a cron command and starts its standard input.
    return line.replace("%", "\\%")


def render_cron_block(spec: ServiceSpec, paths: ServicePaths) -> str:
    log = paths.log_path
    boot = _cron_command(spec.watchdog_argv("check", boot=True), spec, log)
    every = _cron_command(spec.watchdog_argv("check"), spec, log)
    minutes = spec.cron_minutes
    schedule = "* * * * *" if minutes == 1 else f"*/{minutes} * * * *"
    return "\n".join((CRON_BEGIN, f"@reboot {boot}", f"{schedule} {every}", CRON_END)) + "\n"


class CronBlockError(ValueError):
    """AQ's crontab block is damaged, so it cannot be told apart from the rest."""


def strip_cron_block(text: str) -> tuple[str, bool]:
    """*text* without AQ's block; the operator's own entries are untouched.

    Raises :class:`CronBlockError` for a begin marker with no end marker: the
    lines after it may be the operator's own, and a crontab is written back
    whole, so guessing where the block ends could delete their jobs.
    """
    kept: list[str] = []
    inside = removed = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not inside and stripped == CRON_BEGIN:
            inside = removed = True
            continue
        if inside:
            if stripped == CRON_END:
                inside = False
            continue
        kept.append(line)
    if inside:
        raise CronBlockError(
            f"the crontab has AQ's begin marker but no `{CRON_END}` line; restore that "
            "line (or remove AQ's lines) with `crontab -e`, then rerun"
        )
    return "".join(kept), removed


def merge_cron_block(existing: str, block: str) -> tuple[str | None, str]:
    """``(crontab with AQ's block replaced, "")``, or ``(None, why)``."""
    try:
        kept, _removed = strip_cron_block(existing)
    except CronBlockError as error:
        return None, str(error)
    if kept and not kept.endswith("\n"):
        kept += "\n"
    return kept + block, ""


# ---------------------------------------------------------------------------
# What the host offers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostFacts:
    """The service managers this host has, and whether they answer."""

    system: str
    systemd_user: bool = False
    systemd_detail: str = ""
    systemd_booted: bool = False
    launchctl: bool = False
    crontab: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "systemd_user": self.systemd_user,
            "systemd_detail": self.systemd_detail,
            "systemd_booted": self.systemd_booted,
            "launchctl": self.launchctl,
            "crontab": self.crontab,
        }


def _normalise_system(system: str) -> str:
    for known in ("darwin", "linux"):
        if system.startswith(known):
            return known
    return system


def probe_host(
    *,
    runner: CommandRunner = run_command,
    which: Callable[[str], str | None] = shutil.which,
    system: str | None = None,
    path_exists: Callable[[str], bool] = os.path.exists,
) -> HostFacts:
    """Ask the host, read-only, which mechanisms it can run."""
    system = _normalise_system(system or sys.platform)
    if system == "darwin":
        return HostFacts(system=system, launchctl=bool(which("launchctl")))
    systemd_user = False
    detail = ""
    if which("systemctl"):
        probe = runner(["systemctl", "--user", "show-environment"], timeout=10.0)
        systemd_user = probe.ok
        detail = "" if probe.ok else probe.message()
    else:
        detail = "systemctl is not installed"
    return HostFacts(
        system=system,
        systemd_user=systemd_user,
        systemd_detail=detail,
        systemd_booted=path_exists("/run/systemd/system"),
        crontab=bool(which("crontab")),
    )


def choose_mechanism(facts: HostFacts, requested: str = AUTO) -> tuple[str | None, str]:
    """The mechanism to install and why -- or ``None`` and why none fits."""
    no_user_manager = "no systemd user manager answers" + (
        f" ({facts.systemd_detail})" if facts.systemd_detail else ""
    )
    available: dict[str, str | None] = {
        MECHANISM_LAUNCHD: None if facts.launchctl else "launchctl is not available",
        MECHANISM_SYSTEMD: None if facts.systemd_user else no_user_manager,
        MECHANISM_CRON: None if facts.crontab else "crontab is not installed",
    }
    if facts.system == "darwin":
        available[MECHANISM_SYSTEMD] = "systemd does not exist on macOS"
        available[MECHANISM_CRON] = "cron is not offered on macOS; launchd is its service manager"
    elif facts.system == "linux":
        available[MECHANISM_LAUNCHD] = "launchd exists only on macOS"
    if requested != AUTO:
        if requested not in available:
            return None, f"unknown mechanism {requested!r} (choose from {', '.join(MECHANISMS)})"
        problem = available[requested]
        return (None, problem) if problem else (requested, f"{requested} was requested")
    for mechanism in (MECHANISM_LAUNCHD, MECHANISM_SYSTEMD, MECHANISM_CRON):
        if available[mechanism] is None:
            reason = {
                MECHANISM_LAUNCHD: "launchd is macOS's service manager",
                MECHANISM_SYSTEMD: "a systemd user manager is running",
                MECHANISM_CRON: f"cron runs the checks: {no_user_manager}",
            }[mechanism]
            if mechanism == MECHANISM_CRON and facts.systemd_booted:
                reason += (
                    "; systemd itself is running, so `sudo loginctl enable-linger $USER` "
                    "and `aq service install --mechanism systemd` would use a user unit instead"
                )
            return mechanism, reason
    return None, "; ".join(problem for problem in available.values() if problem)


def linger_enabled(user: str, *, root: Path = Path("/var/lib/systemd/linger")) -> bool | None:
    """Whether systemd starts this user's manager at boot; ``None`` if unknowable."""
    if not root.is_dir():
        return None
    return (root / user).exists()


def current_user() -> str:
    import pwd

    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # pragma: no cover - a uid with no passwd entry
        return os.environ.get("USER") or str(os.getuid())


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceAction:
    """The outcome of ``aq service install`` or ``uninstall``."""

    ok: bool
    summary: str
    mechanism: str | None = None
    remediation: str = ""
    notes: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    dry_run: bool = False
    needs_user: bool = False
    #: The entry as written (or, in a dry run, as it would be).
    preview: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "preview": self.preview,
            "summary": self.summary,
            "mechanism": self.mechanism,
            "remediation": self.remediation,
            "notes": list(self.notes),
            "commands": list(self.commands),
            "artifacts": list(self.artifacts),
            "dry_run": self.dry_run,
            "needs_user": self.needs_user,
            "detail": dict(self.detail),
        }


class _Steps:
    """Run host commands, keeping a transcript and the first failure."""

    def __init__(self, runner: CommandRunner, *, dry_run: bool) -> None:
        self.runner = runner
        self.dry_run = dry_run
        self.transcript: list[str] = []

    def run(self, argv: Sequence[str], *, input_text: str | None = None) -> CommandOutput:
        self.transcript.append(" ".join(shlex.quote(part) for part in argv))
        if self.dry_run:
            return CommandOutput(argv=tuple(argv), returncode=0)
        if input_text is None:
            return self.runner(list(argv), timeout=60.0)
        return self.runner(list(argv), timeout=60.0, input_text=input_text)


def read_record(paths: ServicePaths) -> dict[str, Any] | None:
    try:
        payload = json.loads(paths.record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_file(path: Path, content: bytes | str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(content, str):
        tmp.write_text(content, encoding="utf-8")
    else:
        tmp.write_bytes(content)
    tmp.chmod(mode)
    os.replace(tmp, path)


def _read_crontab(steps: _Steps) -> tuple[str | None, str]:
    """``(text, "")``, or ``(None, why)`` when the crontab cannot be read safely."""
    listed = steps.runner(["crontab", "-l"], timeout=30.0)
    if listed.ok:
        return listed.stdout, ""
    if "no crontab" in (listed.stderr + listed.stdout).lower():
        return "", ""
    return None, listed.message()


def _cron_running(runner: CommandRunner) -> bool:
    return any(runner(["pgrep", "-x", name], timeout=10.0).ok for name in ("cron", "crond"))


def build_spec(
    mechanism: str,
    *,
    aq: str,
    environ: Mapping[str, str],
    interval: float | None = None,
    python: str | None = None,
) -> ServiceSpec:
    return ServiceSpec(
        mechanism=mechanism,
        python=python or sys.executable,
        aq=aq,
        path=service_path(environ, aq=aq),
        interval=float(interval) if interval else default_interval(mechanism),
        lang=environ.get("LANG") or None,
    )


def install_service(
    *,
    aq: str | None,
    requested: str = AUTO,
    interval: float | None = None,
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner = run_command,
    which: Callable[[str], str | None] = shutil.which,
    paths: ServicePaths | None = None,
    facts: HostFacts | None = None,
    python: str | None = None,
    user: str | None = None,
    linger_root: Path = Path("/var/lib/systemd/linger"),
    dry_run: bool = False,
    clock: Callable[[], float] = time.time,
) -> ServiceAction:
    """Install (or reinstall) the watchdog with the best available mechanism.

    Idempotent: a rerun rewrites the entry in place, and switching mechanism
    removes the old one, so a host never runs two watchdogs.
    """
    env = os.environ if environ is None else environ
    paths = paths or ServicePaths.for_host(env)
    if not aq:
        return ServiceAction(
            ok=False,
            summary="the `aq` command was not found",
            remediation="Run `aq service install` from the installation it should manage.",
        )
    facts = facts or probe_host(runner=runner, which=which)
    mechanism, reason = choose_mechanism(facts, requested)
    if mechanism is None:
        if requested != AUTO:
            summary = f"{requested} is not available here: {reason}"
            remediation = (
                "Rerun with `--mechanism auto` to use what this host offers"
                + (
                    ", or run `sudo loginctl enable-linger $USER` (which starts a systemd user "
                    "manager) and rerun `aq service install --mechanism systemd`."
                    if requested == MECHANISM_SYSTEMD and facts.systemd_booted
                    else "."
                )
            )
        else:
            summary = f"no service mechanism is available: {reason}"
            remediation = (
                "Install cron (`sudo apt-get install cron` and `sudo service cron start`), or "
                "enable systemd (on WSL: `[boot] systemd=true` in /etc/wsl.conf, then "
                "`wsl --shutdown`), then rerun `aq service install`."
            )
        return ServiceAction(
            ok=False,
            needs_user=True,
            summary=summary,
            remediation=remediation,
            detail={"host": facts.to_dict()},
        )
    steps = _Steps(runner, dry_run=dry_run)
    notes: list[str] = [reason]
    user = user or current_user()

    if mechanism == MECHANISM_SYSTEMD:
        # Without linger the user manager -- and with it every process in the
        # watchdog's unit: the daemon it started, that daemon's tmux server and
        # agent sessions -- is stopped at the last logout.  A daemon started
        # from a shell survives logout, so a unit without linger is a
        # regression; cron's processes are not the user manager's to stop.
        lingering, linger_notes = _ensure_linger(user, steps, linger_root=linger_root)
        notes.extend(linger_notes)
        if not lingering and requested == AUTO:
            if not facts.crontab:
                return ServiceAction(
                    ok=False,
                    needs_user=True,
                    mechanism=MECHANISM_SYSTEMD,
                    summary=(
                        f"linger could not be enabled for {user}, and without it systemd "
                        "stops the watchdog -- and the daemon it starts -- at logout"
                    ),
                    remediation=(
                        f"Run `sudo loginctl enable-linger {user}` (or install cron), then "
                        "rerun `aq service install`."
                    ),
                    notes=tuple(notes),
                    commands=tuple(steps.transcript),
                    dry_run=dry_run,
                    detail={"host": facts.to_dict()},
                )
            mechanism = MECHANISM_CRON
            notes.append(
                f"using cron instead of systemd: linger could not be enabled for {user}, and "
                "without it systemd would stop the daemon and its agent sessions at logout. "
                f"`sudo loginctl enable-linger {user}`, then `aq service install`, switches to "
                "the user unit."
            )
        elif not lingering:
            notes.append(
                "WARNING: without linger, logging out stops this watchdog and every daemon "
                "and agent session it started"
            )
    spec = build_spec(mechanism, aq=aq, environ=env, interval=interval, python=python)

    if mechanism == MECHANISM_SYSTEMD:
        failure = _install_systemd(spec, paths, steps)
    elif mechanism == MECHANISM_LAUNCHD:
        failure = _install_launchd(spec, paths, steps)
    else:
        failure = _install_cron(spec, paths, steps)
        if failure is None and not dry_run and not _cron_running(runner):
            notes.append(
                "cron is not running, so these entries will not fire until it is: "
                "`sudo service cron start` (on WSL without systemd, also add "
                "`[boot] command = service cron start` to /etc/wsl.conf)"
            )
    if failure is not None:
        return ServiceAction(
            ok=False,
            mechanism=mechanism,
            summary=f"could not install the {mechanism} watchdog: {failure}",
            remediation="Fix the error above and rerun `aq service install`.",
            commands=tuple(steps.transcript),
            dry_run=dry_run,
            detail={"host": facts.to_dict()},
        )

    # One watchdog per host: whatever another mechanism left behind goes.
    for other in MECHANISMS:
        if other != mechanism:
            _remove_mechanism(other, paths, steps, only_if_present=True)

    record = {
        "mechanism": mechanism,
        "python": spec.python,
        "aq": spec.aq,
        "path": spec.path,
        "lang": spec.lang,
        "interval": spec.interval,
        "installed_at": clock(),
        "artifact": paths.artifact_for(mechanism),
        "log": str(paths.log_path),
    }
    if not dry_run:
        _write_file(paths.record_path, json.dumps(record, indent=2) + "\n")
    preview = {
        MECHANISM_SYSTEMD: lambda: render_systemd_unit(spec, paths),
        MECHANISM_LAUNCHD: lambda: render_launchd_plist(spec, paths).decode("utf-8"),
        MECHANISM_CRON: lambda: render_cron_block(spec, paths),
    }[mechanism]()
    where = {
        MECHANISM_SYSTEMD: f"systemd user unit {UNIT_NAME}",
        MECHANISM_LAUNCHD: f"launchd agent {LAUNCHD_LABEL}",
        MECHANISM_CRON: f"cron (@reboot and every {spec.cron_minutes} min)",
    }[mechanism]
    return ServiceAction(
        ok=True,
        mechanism=mechanism,
        summary=(
            f"{'would install' if dry_run else 'installed'} the AQ watchdog as a {where}; "
            f"it logs to {paths.log_path}"
        ),
        notes=tuple(notes),
        commands=tuple(steps.transcript),
        artifacts=(paths.artifact_for(mechanism), str(paths.record_path)),
        dry_run=dry_run,
        preview=preview,
        detail={"record": record, "host": facts.to_dict()},
    )


def _install_systemd(spec: ServiceSpec, paths: ServicePaths, steps: _Steps) -> str | None:
    if not steps.dry_run:
        _write_file(paths.unit_path, render_systemd_unit(spec, paths))
        paths.log_path.parent.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", UNIT_NAME],
        # (Re)start so a rewritten unit takes effect; KillMode=process means
        # this restarts the watchdog only.
        ["systemctl", "--user", "restart", UNIT_NAME],
    ):
        result = steps.run(argv)
        if not result.ok:
            return f"`{' '.join(argv)}` failed: {result.message()}"
    return None


def _ensure_linger(user: str, steps: _Steps, *, linger_root: Path) -> tuple[bool, list[str]]:
    """Keep the user manager running without a login: ``(lingering, notes)``.

    Linger is what starts the unit at boot and, as importantly, what keeps the
    daemon it started alive after the user logs out.
    """
    if linger_enabled(user, root=linger_root):
        return True, []
    if steps.dry_run:
        return True, [f"would enable linger for {user} (loginctl, then sudo -n)"]
    for argv in (
        ["loginctl", "enable-linger", user],
        ["sudo", "-n", "loginctl", "enable-linger", user],
    ):
        if steps.run(argv).ok:
            return True, [f"enabled linger for {user}: the watchdog runs from boot, logged in or not"]
    return False, [f"linger could not be enabled for {user} (loginctl and sudo -n both refused)"]


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _install_launchd(spec: ServiceSpec, paths: ServicePaths, steps: _Steps) -> str | None:
    if not steps.dry_run:
        _write_file(paths.plist_path, render_launchd_plist(spec, paths))
        paths.log_path.parent.mkdir(parents=True, exist_ok=True)
    domain = _launchd_domain()
    # A loaded copy must go first, or bootstrap refuses; neither may fail the install.
    steps.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
    steps.run(["launchctl", "enable", f"{domain}/{LAUNCHD_LABEL}"])
    loaded = steps.run(["launchctl", "bootstrap", domain, str(paths.plist_path)])
    if not loaded.ok:
        return f"`launchctl bootstrap` failed: {loaded.message()}"
    return None


def _install_cron(spec: ServiceSpec, paths: ServicePaths, steps: _Steps) -> str | None:
    block = render_cron_block(spec, paths)
    too_long = [
        line for line in block.splitlines() if not line.startswith("#") and len(line) > CRON_COMMAND_MAX
    ]
    if too_long:
        return (
            f"a cron entry would be {len(too_long[0])} characters, over cron's "
            f"{CRON_COMMAND_MAX}; install AQ under a shorter path"
        )
    existing, problem = _read_crontab(steps)
    if existing is None:
        # Never overwrite a crontab that could not be read: the operator's own
        # entries are in it.
        return f"could not read the current crontab ({problem})"
    merged, problem = merge_cron_block(existing, block)
    if merged is None:
        return problem
    if not steps.dry_run:
        paths.log_path.parent.mkdir(parents=True, exist_ok=True)
    written = steps.run(["crontab", "-"], input_text=merged)
    if not written.ok:
        return f"`crontab -` failed: {written.message()}"
    return None


def _remove_mechanism(
    mechanism: str, paths: ServicePaths, steps: _Steps, *, only_if_present: bool
) -> tuple[bool, str | None]:
    """Remove one mechanism's entry.  ``(removed, error)``."""
    if mechanism == MECHANISM_SYSTEMD:
        if not paths.unit_path.exists():
            return False, None
        stopped = steps.run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
        if not steps.dry_run:
            paths.unit_path.unlink(missing_ok=True)
        steps.run(["systemctl", "--user", "daemon-reload"])
        error = None if stopped.ok else f"`systemctl --user disable` said: {stopped.message()}"
        return True, error
    if mechanism == MECHANISM_LAUNCHD:
        if not paths.plist_path.exists():
            return False, None
        steps.run(["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"])
        if not steps.dry_run:
            paths.plist_path.unlink(missing_ok=True)
        return True, None
    existing, problem = _read_crontab(steps)
    if existing is None:
        if only_if_present and "not installed" in problem:
            return False, None  # no cron at all: nothing of ours can be in it
        return False, f"could not read the crontab ({problem})"
    try:
        kept, removed = strip_cron_block(existing)
    except CronBlockError as error:
        return False, str(error)
    if not removed:
        return False, None
    written = steps.run(["crontab", "-"], input_text=kept)
    return True, None if written.ok else f"`crontab -` failed: {written.message()}"


def uninstall_service(
    *,
    runner: CommandRunner = run_command,
    paths: ServicePaths | None = None,
    dry_run: bool = False,
) -> ServiceAction:
    """Remove every watchdog entry AQ made, whatever the record says.

    The daemon is left exactly as it is: removing the service stops the
    watchdog, never the daemon it may have started.
    """
    paths = paths or ServicePaths.for_host()
    steps = _Steps(runner, dry_run=dry_run)
    removed: list[str] = []
    errors: list[str] = []
    for mechanism in MECHANISMS:
        did, error = _remove_mechanism(mechanism, paths, steps, only_if_present=True)
        if did:
            removed.append(mechanism)
        if error:
            errors.append(f"{mechanism}: {error}")
    record = paths.record_path.exists()
    if not dry_run:
        paths.record_path.unlink(missing_ok=True)
        state_path(paths.aq_home).unlink(missing_ok=True)
    if not removed and not record:
        return ServiceAction(
            ok=not errors,
            summary="no AQ watchdog was installed",
            notes=tuple(errors),
            commands=tuple(steps.transcript),
            dry_run=dry_run,
        )
    return ServiceAction(
        ok=not errors,
        mechanism=removed[0] if removed else None,
        summary=(
            f"{'would remove' if dry_run else 'removed'} the AQ watchdog "
            f"({', '.join(removed) or 'record only'}); the daemon was left as it is"
        ),
        remediation="; ".join(errors),
        notes=tuple(errors),
        commands=tuple(steps.transcript),
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """Is auto-restart installed, and is it actually working?"""

    installed: bool
    healthy: bool
    summary: str
    mechanism: str | None = None
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    record: dict[str, Any] | None = None
    artifacts: dict[str, bool] = field(default_factory=dict)
    manager: dict[str, Any] = field(default_factory=dict)
    watchdog: dict[str, Any] | None = None
    stop_intent: dict[str, Any] | None = None
    log: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "healthy": self.healthy,
            "summary": self.summary,
            "mechanism": self.mechanism,
            "problems": list(self.problems),
            "notes": list(self.notes),
            "record": self.record,
            "artifacts": dict(self.artifacts),
            "manager": dict(self.manager),
            "watchdog": self.watchdog,
            "stop_intent": self.stop_intent,
            "log": self.log,
        }


def status_commands(
    paths: ServicePaths, *, system: str, record: Mapping[str, Any] | None
) -> dict[str, list[str]]:
    """The read-only host queries a status needs, by name.

    Named rather than run here so the doctor check can run them off its event
    loop (``asyncio`` subprocesses) and ``aq service status`` synchronously,
    and both interpret the same answers with :func:`interpret_status`.
    """
    recorded = (record or {}).get("mechanism")
    commands: dict[str, list[str]] = {}
    if system == "darwin":
        if paths.plist_path.exists() or recorded == MECHANISM_LAUNCHD:
            commands["launchd"] = ["launchctl", "print", f"{_launchd_domain()}/{LAUNCHD_LABEL}"]
        return commands
    commands["crontab"] = ["crontab", "-l"]
    commands["cron"] = ["pgrep", "-x", "cron"]
    commands["crond"] = ["pgrep", "-x", "crond"]
    if paths.unit_path.exists() or recorded == MECHANISM_SYSTEMD:
        commands["systemd.enabled"] = ["systemctl", "--user", "is-enabled", UNIT_NAME]
        commands["systemd.active"] = ["systemctl", "--user", "is-active", UNIT_NAME]
    return commands


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def interpret_status(
    paths: ServicePaths,
    *,
    record: Mapping[str, Any] | None,
    outputs: Mapping[str, CommandOutput],
    watchdog: WatchdogState,
    now: float,
    linger: bool | None = None,
    stop_intent: Any = None,
    aq_exists: Callable[[str], bool] = os.path.exists,
) -> ServiceStatus:
    """Turn the record, the files and the manager's answers into one verdict."""
    crontab = outputs.get("crontab")
    artifacts = {
        MECHANISM_SYSTEMD: paths.unit_path.exists(),
        MECHANISM_LAUNCHD: paths.plist_path.exists(),
        MECHANISM_CRON: bool(crontab and crontab.ok and CRON_BEGIN in crontab.stdout),
    }
    recorded = (record or {}).get("mechanism")
    mechanism = recorded or next((name for name, present in artifacts.items() if present), None)
    installed = bool(record) or any(artifacts.values())
    intent = stop_intent.to_dict() if stop_intent is not None else None
    watchdog_payload = watchdog.to_dict() if watchdog.last_check else None
    if not installed:
        return ServiceStatus(
            installed=False,
            healthy=False,
            summary="auto-restart is not installed; `aq service install` adds it",
            artifacts=artifacts,
            watchdog=watchdog_payload,
            stop_intent=intent,
            log=str(paths.log_path),
        )

    problems: list[str] = []
    notes: list[str] = []
    manager: dict[str, Any] = {}
    if record is None:
        notes.append(
            f"a {mechanism} entry exists without an install record; "
            "`aq service install` rewrites both"
        )
    elif mechanism in artifacts and not artifacts[mechanism]:
        problems.append(
            f"the {mechanism} entry is gone although it was installed; "
            "rerun `aq service install`"
        )
    extra = [name for name, present in artifacts.items() if present and name != mechanism]
    if extra:
        problems.append(
            f"more than one watchdog entry is installed ({', '.join([mechanism or '?', *extra])}); "
            "rerun `aq service install` to keep one"
        )

    if mechanism == MECHANISM_SYSTEMD:
        enabled = outputs.get("systemd.enabled")
        active = outputs.get("systemd.active")
        manager["enabled"] = enabled.out if enabled else None
        manager["active"] = active.out if active else None
        if enabled is not None and enabled.out != "enabled":
            problems.append(
                f"the unit is not enabled ({enabled.out or enabled.message()}); "
                f"`systemctl --user enable --now {UNIT_NAME}`"
            )
        if active is not None and active.out != "active":
            problems.append(
                f"the watchdog is not running ({active.out or active.message()}); "
                f"`systemctl --user start {UNIT_NAME}`, or read {paths.log_path}"
            )
        manager["linger"] = linger
        if linger is False:
            problems.append(
                "linger is off: logging out stops the watchdog and every daemon and agent "
                "session it started, and nothing starts it before a login "
                f"(`sudo loginctl enable-linger {current_user()}`)"
            )
    elif mechanism == MECHANISM_LAUNCHD:
        loaded = outputs.get("launchd")
        manager["loaded"] = bool(loaded and loaded.ok)
        if loaded is not None and not loaded.ok:
            problems.append(
                f"the launchd agent is not loaded; `launchctl bootstrap {_launchd_domain()} "
                f"{paths.plist_path}`"
            )
    elif mechanism == MECHANISM_CRON:
        probes = [outputs.get(name) for name in ("cron", "crond")]
        running = any(probe is not None and probe.ok for probe in probes)
        known = any(probe is not None and probe.returncode is not None for probe in probes)
        manager["cron_running"] = running if known else None
        if known and not running:
            problems.append(
                "cron is not running, so the watchdog entries never fire: "
                "`sudo service cron start` (on WSL without systemd, also "
                "`[boot] command = service cron start` in /etc/wsl.conf)"
            )

    aq = (record or {}).get("aq")
    if aq and not aq_exists(str(aq)):
        problems.append(f"{aq} no longer exists; rerun `aq service install` from the new install")
    python = (record or {}).get("python")
    if python and not aq_exists(str(python)):
        problems.append(
            f"{python} no longer exists; rerun `aq service install` from the new install"
        )

    interval = float(watchdog.interval or (record or {}).get("interval") or DEFAULT_INTERVAL)
    allowed = 3 * interval + 60
    installed_at = float((record or {}).get("installed_at") or 0.0)
    if watchdog.last_check:
        age = now - watchdog.last_check
        if age > allowed and not problems:
            problems.append(
                f"the watchdog last checked {_ago(age)} (it checks every {interval:g}s); "
                f"read {paths.log_path}"
            )
    elif installed_at and now - installed_at > allowed and not problems:
        problems.append(f"the watchdog has never checked in; read {paths.log_path}")

    verdict = watchdog.verdict
    if verdict in {"gave_up", "crash_loop", "start_failed", "backoff"}:
        problems.append(f"{verdict.replace('_', ' ')}: {watchdog.detail}")
    if intent is not None:
        notes.append(
            f"the daemon was stopped on purpose by {intent['by']}; the watchdog leaves it "
            "down until `aq start`"
        )

    healthy = not problems
    label = {
        MECHANISM_SYSTEMD: "systemd user unit",
        MECHANISM_LAUNCHD: "launchd agent",
        MECHANISM_CRON: "cron",
    }.get(mechanism or "", mechanism or "unknown mechanism")
    last = f"last check {_ago(now - watchdog.last_check)}" if watchdog.last_check else "no check yet"
    state = f" ({watchdog.verdict.replace('_', ' ')})" if watchdog.verdict else ""
    summary = (
        f"auto-restart via {label}: {last}{state}"
        if healthy
        else f"auto-restart via {label} needs attention: {problems[0]}"
    )
    return ServiceStatus(
        installed=True,
        healthy=healthy,
        summary=summary,
        mechanism=mechanism,
        problems=tuple(problems),
        notes=tuple(notes),
        record=dict(record) if record else None,
        artifacts=artifacts,
        manager=manager,
        watchdog=watchdog_payload,
        stop_intent=intent,
        log=str(paths.log_path),
    )


def service_status(
    *,
    runner: CommandRunner | None = None,
    paths: ServicePaths | None = None,
    system: str | None = None,
    clock: Callable[[], float] = time.time,
) -> ServiceStatus:
    """:func:`interpret_status` over this host's answers, run synchronously."""
    runner = runner or run_command
    paths = paths or ServicePaths.for_host()
    system = _normalise_system(system or sys.platform)
    record = read_record(paths)
    outputs = {
        name: runner(argv, timeout=10.0)
        for name, argv in status_commands(paths, system=system, record=record).items()
    }
    return interpret_status(
        paths,
        record=record,
        outputs=outputs,
        watchdog=load_state(paths.aq_home),
        now=clock(),
        linger=linger_enabled(current_user()) if system == "linux" else None,
        stop_intent=read_stop_intent(path=paths.aq_home / STOP_INTENT_FILENAME),
    )


# ---------------------------------------------------------------------------
# aq install -- the opt-in step, and aq uninstall -- its removal
# ---------------------------------------------------------------------------


def autostart_step(
    *,
    which: Callable[[str], str | None] | None = None,
    runner: CommandRunner | None = None,
    paths: ServicePaths | None = None,
    facts: HostFacts | None = None,
    environ: Mapping[str, str] | None = None,
    depends_on: tuple[str, ...] = (),
) -> StepSpec:
    """``daemon.autostart`` -- install the watchdog when ``autostart`` is selected.

    Optional and off by default.  It never halts the run: the daemon and the
    dashboard are already up, and a host that offers no mechanism is something
    the operator settles in their own time.
    """
    execute = runner or run_command

    def _settings(context: StepContext) -> Mapping[str, Any]:
        section = context.option(CAPABILITY_AUTOSTART) or {}
        return section if isinstance(section, Mapping) else {}

    def _aq() -> str | None:
        from .onboarding import aq_aware_which

        return aq_aware_which(which or shutil.which)("aq")

    def run(context: StepContext) -> StepResult:
        settings = _settings(context)
        unknown = sorted(set(settings) - {"mechanism", "interval"})
        if unknown:
            return StepResult.failed(
                STEP_AUTOSTART,
                f"unknown autostart setting(s): {', '.join(unknown)}",
                "Recognised keys under `settings.autostart` are `mechanism` "
                f"({AUTO} or {', '.join(MECHANISMS)}) and `interval` (seconds).",
                retryable=False,
            )
        action = install_service(
            aq=_aq(),
            requested=str(settings.get("mechanism") or AUTO),
            interval=float(settings["interval"]) if settings.get("interval") else None,
            environ=environ,
            runner=execute,
            paths=paths,
            facts=facts,
        )
        detail = action.to_dict()
        if not action.ok:
            if action.needs_user:
                return StepResult.needs_user(
                    STEP_AUTOSTART, action.summary, action.remediation, detail=detail
                )
            return StepResult.failed(
                STEP_AUTOSTART, action.summary, action.remediation, detail=detail
            )
        extra = [note for note in action.notes[1:] if note]
        summary = action.summary + (f" ({'; '.join(extra)})" if extra else "")
        return StepResult.succeeded(
            STEP_AUTOSTART,
            summary,
            detail=detail,
            resources=(
                ResourceRecord(
                    kind=RESOURCE_SERVICE,
                    id=str(action.mechanism),
                    owned=True,
                    detail={"artifacts": list(action.artifacts)},
                ),
            ),
        )

    def verify(context: StepContext) -> bool:
        return service_status(runner=execute, paths=paths).healthy

    return StepSpec(
        id=STEP_AUTOSTART,
        title="Keep the daemon running across reboots and crashes",
        description=(
            "Optional. Installs AQ's watchdog as a systemd user unit, a launchd agent or "
            "cron entries (whichever the host offers). It starts the daemon at boot and "
            "after a crash, never overrides `aq stop`, and logs to "
            "~/.agent-queue/logs/aq-service.log. `aq service` manages it afterwards."
        ),
        run=run,
        depends_on=depends_on,
        capability=CAPABILITY_AUTOSTART,
        mutating=True,
        halts=False,
        consent_prompt="Install the auto-restart watchdog (starts AQ at boot and after a crash)?",
        verify=verify,
        owner="onboarding",
    )


def installed_service_resource(paths: ServicePaths | None = None) -> ResourceRecord | None:
    """The service as an uninstallable resource, if one is installed.

    ``aq service install`` does not write the install resume record, so
    ``aq uninstall`` asks here too: a watchdog left behind by an uninstall would
    keep starting a daemon that is supposed to be gone.
    """
    paths = paths or ServicePaths.for_host()
    record = read_record(paths)
    mechanism = (record or {}).get("mechanism")
    if not mechanism:
        if paths.unit_path.exists():
            mechanism = MECHANISM_SYSTEMD
        elif paths.plist_path.exists():
            mechanism = MECHANISM_LAUNCHD
        else:
            return None
    return ResourceRecord(
        kind=RESOURCE_SERVICE,
        id=str(mechanism),
        owned=True,
        detail={"note": "installed with `aq service install`"},
    )


def removal_handler(runner: CommandRunner | None = None) -> Callable[[Any], Any]:
    """The ``aq uninstall`` handler for :data:`RESOURCE_SERVICE` records."""
    from .lifecycle import RemovalOutcome

    def handle(item: Any) -> Any:
        action = uninstall_service(runner=runner or run_command)
        error = None if action.ok else (action.remediation or action.summary)
        return RemovalOutcome(item, removed=action.ok, summary=action.summary, error=error)

    return handle


__all__ = [
    "AUTO",
    "CAPABILITY_AUTOSTART",
    "CRON_BEGIN",
    "CRON_END",
    "DEFAULT_CRON_INTERVAL",
    "LAUNCHD_LABEL",
    "MECHANISMS",
    "MECHANISM_CRON",
    "MECHANISM_LAUNCHD",
    "MECHANISM_SYSTEMD",
    "RESOURCE_SERVICE",
    "STEP_AUTOSTART",
    "UNIT_NAME",
    "CronBlockError",
    "HostFacts",
    "ServiceAction",
    "ServicePaths",
    "ServiceSpec",
    "ServiceStatus",
    "autostart_step",
    "build_spec",
    "choose_mechanism",
    "install_service",
    "installed_service_resource",
    "interpret_status",
    "linger_enabled",
    "merge_cron_block",
    "probe_host",
    "read_record",
    "removal_handler",
    "render_cron_block",
    "render_launchd_plist",
    "render_systemd_unit",
    "service_path",
    "service_status",
    "status_commands",
    "strip_cron_block",
    "uninstall_service",
]
