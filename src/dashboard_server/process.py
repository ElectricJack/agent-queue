"""The dashboard server as a managed background process (docs/specs/dashboard-server.md §1).

``aq dashboard start|stop|restart|status`` and the lifecycle commands that call
them (``aq start``, ``aq stop``, ``aq restart``, ``aq status``, ``aq update``)
share this module.  It owns three things:

* **Files.**  ``~/.agent-queue/dashboard-server.pid`` and
  ``dashboard-server.log`` -- deliberately not the Vite dev server's
  ``dashboard.pid``, so an older release's PID file is never mistaken for this
  process.
* **Identity.**  A port that answers is not proof of anything: the process is
  ours only when ``GET /__aq/health`` names :data:`SERVICE_NAME`.  That is what
  tells ``running`` from ``port_conflict``, which a bare TCP connect cannot.
* **Launch and stop.**  ``[sys.executable, "-m", "src.dashboard_server"]`` in
  its own session, output appended to the log, with an allowlisted environment
  -- the process needs no secret, so it gets no database URL, provider key or
  harness marker.  Start waits for the identity to answer with the child's own
  PID; stop is ``SIGTERM``, then ``SIGKILL`` after :data:`STOP_TIMEOUT_SECONDS`.

Nothing supervises the process: a crash shows in ``aq status`` and
``aq doctor``, and ``aq start`` heals it (§1, "Crash and restart").

Import boundary (§1): the standard library and :mod:`src.config` (through
:mod:`.settings`).  Module level stays that light, because ``aq status`` reads
it and the update finisher imports it before dependencies are reinstalled.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import dashboard_server_config_from_raw
from src.dashboard_server.settings import (
    DashboardServerSettings,
    SettingsError,
    _read_yaml,
    settings_from_config,
)

#: What ``/__aq/health`` names itself; equal to ``src.dashboard_server.app.SERVICE_NAME``.
SERVICE_NAME = "aq-dashboard-server"
HEALTH_PATH = "__aq/health"
#: Equal to ``src.dashboard_server.bundle.MANIFEST_NAME``; read here without
#: importing Starlette.
MANIFEST_NAME = "aq-dashboard-manifest.json"

PID_FILE_NAME = "dashboard-server.pid"
LOG_FILE_NAME = "dashboard-server.log"

#: Spec §1: start waits up to 10 s for the identity; stop escalates after 10 s.
START_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 10.0
#: Above the server's own 1 s upstream probe inside ``/__aq/health``.
PROBE_TIMEOUT_SECONDS = 3.0

#: Exit code ``python -m src.dashboard_server`` uses for "no bundle here".
EXIT_NO_BUNDLE = 2

# -- states (``aq status --json`` -> ``dashboard_server.state``) --------------
RUNNING = "running"
STOPPED = "stopped"
DISABLED = "disabled"
NO_BUNDLE = "no_bundle"
STALE_PID = "stale_pid"
PORT_CONFLICT = "port_conflict"
#: Our process is alive (by its PID file) but does not answer at the configured
#: URL: wedged, still starting, or bound to a port the config no longer names.
UNRESPONSIVE = "unresponsive"
#: ``dashboard.server`` does not validate; the message names the key.
MISCONFIGURED = "misconfigured"

#: Environment variables the dashboard server may inherit.  An allowlist, not a
#: denylist: the process serves static files and relays bytes, so anything a
#: shell happens to carry -- a database URL, a provider key, an enclosing
#: harness's session markers -- stays behind.
_ENV_ALLOWED = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "PYTHONPATH", "VIRTUAL_ENV",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    # How the proxy finds the daemon, resolved exactly as every `aq` command does.
    "AQ_API_URL", "AGENT_QUEUE_API_URL",
})
_ENV_ALLOWED_PREFIXES = ("LC_",)

#: Probe result: ``("ours", identity)``, ``("foreign", None)`` or ``("none", None)``.
OURS = "ours"
FOREIGN = "foreign"
NOTHING = "none"

Probe = Callable[[str], "tuple[str, dict[str, Any] | None]"]


# ---------------------------------------------------------------------------
# Files and configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerFiles:
    """Where the managed process keeps its PID and log, and which config it reads."""

    config: Path
    pid_file: Path
    log_file: Path

    @classmethod
    def in_state_dir(cls, state_dir: Path, config: Path | None = None) -> ServerFiles:
        return cls(
            config=config or state_dir / "config.yaml",
            pid_file=state_dir / PID_FILE_NAME,
            log_file=state_dir / LOG_FILE_NAME,
        )


@dataclass(frozen=True)
class Configured:
    """``dashboard.server`` as this install has it, or the reason it cannot be read."""

    enabled: bool
    settings: DashboardServerSettings | None
    error: str = ""

    @property
    def url(self) -> str | None:
        return self.settings.url if self.settings is not None else None


def configured(config: Path, *, api_url: str | None = None) -> Configured:
    """Read ``dashboard.server`` (and what it depends on) the way the server will."""
    try:
        raw = _read_yaml(config)
        section = dashboard_server_config_from_raw(raw)
        settings = settings_from_config(raw, api_url=api_url)
    except (SettingsError, TypeError, ValueError) as error:
        return Configured(enabled=False, settings=None, error=str(error))
    return Configured(enabled=section.enabled is True, settings=settings)


def bundle_directory() -> Path:
    """The packaged bundle directory (``src/dashboard_assets/dist``), found without Starlette."""
    import importlib.util

    spec = importlib.util.find_spec("src.dashboard_assets")
    locations = list(spec.submodule_search_locations or ()) if spec else []
    if not locations:  # pragma: no cover - the data package ships with every install
        return Path(__file__).resolve().parents[1] / "dashboard_assets" / "dist"
    return Path(locations[0]) / "dist"


def installed_manifest_sha256(directory: Path | None = None) -> str | None:
    """SHA-256 of the installed manifest, or ``None`` when no bundle is installed."""
    try:
        raw = ((directory or bundle_directory()) / MANIFEST_NAME).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()


def bundle_installed(directory: Path | None = None) -> bool:
    """A bundle manifest is present -- a release, or a checkout `aq install` built."""
    return ((directory or bundle_directory()) / MANIFEST_NAME).is_file()


def server_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allowlisted environment the dashboard server is started with."""
    source = os.environ if environ is None else environ
    env = {
        key: value
        for key, value in source.items()
        if key in _ENV_ALLOWED or key.startswith(_ENV_ALLOWED_PREFIXES)
    }
    env["PYTHONUNBUFFERED"] = "1"
    return env


def source_root() -> Path:
    """The directory holding the ``src`` package this process imported.

    The child is started with ``-m`` from here, which puts it first on the
    module path: the server runs the same code as the ``aq`` that started it,
    even where the environment's editable install points at another checkout.
    """
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def probe_identity(url: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[str, dict[str, Any] | None]:
    """``GET <url>/__aq/health``: is the process answering at *url* ours?

    *url* is the server's base URL (``http://127.0.0.1:8082/``).  Environment
    proxies are bypassed: the server is local, and a corporate ``http_proxy``
    must not answer on its behalf.
    """
    target = url.rstrip("/") + "/" + HEALTH_PATH
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(target, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(65536)
    except urllib.error.HTTPError as error:
        error.close()
        return FOREIGN, None
    except (urllib.error.URLError, OSError, ValueError):
        return NOTHING, None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return FOREIGN, None
    if isinstance(payload, dict) and payload.get("service") == SERVICE_NAME:
        return OURS, payload
    return FOREIGN, None


def read_pid_file(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _write_pid_file(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(f"{pid}\n", encoding="utf-8")
    os.replace(temporary, path)


def _remove_pid_file(path: Path, *, only_if: int | None = None) -> None:
    if only_if is not None and read_pid_file(path) not in (None, only_if):
        return  # someone else's start has already replaced it
    try:
        path.unlink()
    except OSError:
        pass


def process_alive(pid: int) -> bool:
    """Whether *pid* is a live process, reaping it first if it is our exited child."""
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass  # not our child: the normal case for a server an earlier `aq` started
    except OSError:
        pass
    else:
        if reaped == pid:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    except OSError:
        return False
    return True


def _command_line(pid: int) -> str | None:
    """*pid*'s command line, or ``None`` when this platform cannot say."""
    proc = Path("/proc") / str(pid) / "cmdline"
    try:
        return proc.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except FileNotFoundError:
        if Path("/proc/self").exists():
            return ""  # a /proc system, and the process is gone
    except OSError:
        pass
    try:
        listed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return listed.stdout if listed.returncode == 0 else ""


def is_server_process(pid: int) -> bool:
    """Whether *pid* is a managed dashboard server (guards against a recycled PID).

    Only a PID file needs this: a PID reported by ``/__aq/health`` is the
    server's own word.  Where the command line cannot be read at all the PID
    file is trusted, as ``aq stop`` trusts the daemon's.
    """
    if not process_alive(pid):
        return False
    command = _command_line(pid)
    if command is None:
        return True
    return "src.dashboard_server" in command


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerStatus:
    """What ``aq status`` and ``aq dashboard status`` report (spec §1, "Observers")."""

    state: str
    url: str | None = None
    pid: int | None = None
    detail: str = ""
    enabled: bool = True
    #: ``/__aq/health`` when the server answered as ours.
    identity: dict[str, Any] | None = None
    #: ``False`` when the server serves a different build from the one installed.
    bundle_current: bool | None = None
    #: The PID file named a process that is gone (``stale_pid``) or not ours.
    stale_pid: int | None = None
    log_file: str | None = None

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state,
            "url": self.url,
            "pid": self.pid,
            "enabled": self.enabled,
            "detail": self.detail,
        }
        if self.identity is not None:
            bundle = self.identity.get("bundle")
            payload["version"] = self.identity.get("version")
            payload["bundle"] = bundle if isinstance(bundle, dict) else None
            payload["bundle_current"] = self.bundle_current
            payload["api_url"] = self.identity.get("api_url")
            payload["upstream_ok"] = self.identity.get("upstream_ok")
        if self.stale_pid is not None:
            payload["stale_pid"] = self.stale_pid
        if self.log_file is not None:
            payload["log"] = self.log_file
        return payload


def inspect(
    files: ServerFiles,
    *,
    api_url: str | None = None,
    probe: Probe | None = None,
    bundle_dir: Path | None = None,
) -> ServerStatus:
    """The dashboard server's state, from the PID file and one identity probe.

    Read-only -- a stale PID file is reported, not removed -- and it never
    needs the daemon, so ``aq status`` can answer while the daemon is down.
    What answers on the wire wins: a server that is ours is ``running`` even
    when ``dashboard.server.enabled`` has since been turned off.
    """
    probe = probe or probe_identity
    config = configured(files.config, api_url=api_url)
    log = str(files.log_file)
    if config.settings is None:
        return ServerStatus(MISCONFIGURED, detail=config.error, enabled=False, log_file=log)
    url = config.settings.url
    kind, identity = probe(url)
    if kind == OURS and identity is not None:
        pid = identity.get("pid")
        installed = installed_manifest_sha256(bundle_dir)
        bundle = identity.get("bundle")
        served = bundle.get("manifest_sha256") if isinstance(bundle, dict) else None
        current = None if installed is None or not served else served == installed
        detail = "" if current is not False else (
            "serving an older build than the one installed; run `aq dashboard restart`"
        )
        return ServerStatus(
            RUNNING, url=url, pid=pid if isinstance(pid, int) else None, detail=detail,
            enabled=config.enabled, identity=identity, bundle_current=current, log_file=log,
        )

    recorded = read_pid_file(files.pid_file)
    live = recorded if recorded is not None and is_server_process(recorded) else None
    if live is not None:
        return ServerStatus(
            UNRESPONSIVE, url=url, pid=live, enabled=config.enabled, log_file=log,
            detail=(
                f"PID {live} is running but nothing answers as the dashboard server at "
                f"{url}; run `aq dashboard restart`"
            ),
        )
    if not config.enabled:
        return ServerStatus(
            DISABLED, url=url, enabled=False, log_file=log,
            detail="dashboard.server.enabled is false; `aq start` does not manage it",
        )
    if not bundle_installed(bundle_dir):
        return ServerStatus(
            NO_BUNDLE, url=url, log_file=log,
            detail=(
                "no dashboard bundle is installed (a source checkout): run "
                "`npm -w dashboard run dev`, or build one with "
                "`aq install --restart-from dashboard.build`"
            ),
        )
    if kind == FOREIGN:
        port = config.settings.port
        return ServerStatus(
            PORT_CONFLICT, url=url, log_file=log,
            detail=(
                f"port {port} answers, but not as the dashboard server; free it or set "
                "dashboard.server.port"
            ),
        )
    if recorded is not None:
        return ServerStatus(
            STALE_PID, url=url, stale_pid=recorded, log_file=log,
            detail=(
                f"not running: the PID file names PID {recorded}, which has exited "
                f"(see {log}); `aq start` or `aq dashboard start` starts it again"
            ),
        )
    return ServerStatus(
        STOPPED, url=url, log_file=log,
        detail="not running; `aq start` or `aq dashboard start` starts it",
    )


def describe(status: ServerStatus) -> str:
    """One line for a person: ``running at http://127.0.0.1:8082/ (PID 4242)``."""
    if status.state == RUNNING:
        where = f"running at {status.url}" + (f" (PID {status.pid})" if status.pid else "")
        if status.bundle_current is False:
            return f"{where} -- {status.detail}"
        if status.identity is not None and status.identity.get("upstream_ok") is False:
            return f"{where}; the daemon is not answering it"
        return where
    labels = {
        STOPPED: "stopped",
        DISABLED: "disabled",
        NO_BUNDLE: "no bundle",
        STALE_PID: "stopped (stale PID file)",
        PORT_CONFLICT: "port conflict",
        UNRESPONSIVE: "not responding",
        MISCONFIGURED: "misconfigured",
    }
    label = labels.get(status.state, status.state)
    return f"{label} -- {status.detail}" if status.detail else label


# ---------------------------------------------------------------------------
# Start and stop
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What a start or stop did.  ``exit_code`` is what the CLI exits with."""

    ok: bool
    action: str
    message: str
    status: ServerStatus | None = None
    pid: int | None = None
    exit_code: int = 0
    log_excerpt: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "message": self.message,
            "pid": self.pid,
        }
        if self.status is not None:
            payload["dashboard_server"] = self.status.to_dict()
        if self.log_excerpt:
            payload["log_excerpt"] = self.log_excerpt
        return payload


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _log_since(path: Path, offset: int, lines: int = 20) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()][-lines:]


def _terminate(
    pid: int,
    *,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """``SIGTERM``, wait up to *timeout*, then ``SIGKILL``.  ``True`` once *pid* is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return not process_alive(pid)
    deadline = clock() + timeout
    while clock() < deadline:
        if not process_alive(pid):
            return True
        sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    for _ in range(20):
        if not process_alive(pid):
            return True
        sleep(0.1)
    return not process_alive(pid)


def stop(
    files: ServerFiles,
    *,
    probe: Probe | None = None,
    timeout: float = STOP_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    """Stop the dashboard server: the PID file's process, else whatever answers as ours.

    The second case covers a server whose PID file was lost, and a foreground
    ``aq dashboard serve``: both are the dashboard server on its configured
    port.  A PID file naming a dead or recycled process is removed, never
    signalled.
    """
    probe = probe or probe_identity
    recorded = read_pid_file(files.pid_file)
    pid = recorded if recorded is not None and is_server_process(recorded) else None
    if pid is None:
        config = configured(files.config)
        if config.settings is not None:
            kind, identity = probe(config.settings.url)
            candidate = identity.get("pid") if kind == OURS and identity else None
            if isinstance(candidate, int) and process_alive(candidate):
                pid = candidate
    if pid is None:
        _remove_pid_file(files.pid_file, only_if=recorded)
        return Outcome(True, "not_running", "The dashboard server is not running.")
    if not _terminate(pid, timeout=timeout, clock=clock, sleep=sleep):
        return Outcome(
            False, "stop_failed",
            f"The dashboard server (PID {pid}) did not exit after SIGKILL.",
            pid=pid, exit_code=1,
        )
    _remove_pid_file(files.pid_file, only_if=pid)
    return Outcome(True, "stopped", f"Dashboard server stopped (PID {pid}).", pid=pid)


def start(
    files: ServerFiles,
    *,
    api_url: str | None = None,
    python: str = sys.executable,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    popen: Callable[..., Any] | None = None,
    probe: Probe | None = None,
    timeout: float = START_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    bundle_dir: Path | None = None,
) -> Outcome:
    """Start the dashboard server in the background; idempotent.

    A running server that is ours and serves the installed build is success
    with nothing done.  One serving an older build, or one that is alive but
    not answering, is restarted -- that is what "``aq start`` heals it" means.
    A port held by another program is a failure naming the key, never an
    auto-increment.
    """
    probe = probe or probe_identity
    config = configured(files.config, api_url=api_url)
    if config.settings is None:
        return Outcome(
            False, "misconfigured", f"dashboard server: {config.error}", exit_code=1,
            status=ServerStatus(MISCONFIGURED, detail=config.error, enabled=False),
        )
    if not config.enabled:
        status = inspect(files, api_url=api_url, probe=probe, bundle_dir=bundle_dir)
        return Outcome(
            False, "disabled",
            "dashboard.server.enabled is false, so the dashboard server is not started. "
            "Set it to true, or run `aq dashboard serve` in the foreground.",
            status=status, exit_code=1,
        )

    status = inspect(files, api_url=api_url, probe=probe, bundle_dir=bundle_dir)
    if status.state == RUNNING and status.bundle_current is not False:
        return Outcome(
            True, "already_running",
            f"Dashboard server is already running at {status.url}"
            + (f" (PID {status.pid})." if status.pid else "."),
            status=status, pid=status.pid,
        )
    if status.state in (RUNNING, UNRESPONSIVE):
        stopped = stop(files, probe=probe, clock=clock, sleep=sleep)
        if not stopped.ok:
            return Outcome(False, "restart_failed", stopped.message, status=status, exit_code=1)
        status = inspect(files, api_url=api_url, probe=probe, bundle_dir=bundle_dir)
    if status.state == PORT_CONFLICT:
        return Outcome(False, "port_conflict", status.detail, status=status, exit_code=1)
    if status.state == NO_BUNDLE:
        return Outcome(
            False, "no_bundle", status.detail, status=status, exit_code=EXIT_NO_BUNDLE,
        )
    _remove_pid_file(files.pid_file)

    # The bundle directory is passed explicitly: the child runs the same `src`
    # (see `source_root`), so it is the directory the checks above just read.
    argv = [
        python, "-m", "src.dashboard_server",
        "--config", str(files.config),
        "--bundle-dir", str(bundle_dir or bundle_directory()),
    ]
    if api_url:
        argv += ["--api-url", api_url]
    files.log_file.parent.mkdir(parents=True, exist_ok=True)
    offset = _log_size(files.log_file)
    with files.log_file.open("ab") as log:
        child = (popen or subprocess.Popen)(
            argv,
            cwd=str(cwd or source_root()),
            env=server_environment(environ),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    _write_pid_file(files.pid_file, child.pid)
    url = config.settings.url

    deadline = clock() + timeout
    while True:
        code = child.poll()
        kind, identity = probe(url)
        if kind == OURS and identity is not None and identity.get("pid") == child.pid:
            return Outcome(
                True, "started", f"Dashboard server started at {url} (PID {child.pid}).",
                pid=child.pid,
                # What just answered, rather than a second probe.
                status=inspect(files, api_url=api_url, bundle_dir=bundle_dir,
                               probe=lambda _url, answer=(kind, identity): answer),
            )
        if code is not None:
            _remove_pid_file(files.pid_file, only_if=child.pid)
            excerpt = _log_since(files.log_file, offset)
            if kind == OURS:
                # Another start won the port between our inspect and our bind.
                return Outcome(
                    True, "already_running", f"Dashboard server is already running at {url}.",
                    status=inspect(files, api_url=api_url, probe=probe, bundle_dir=bundle_dir),
                )
            reason = excerpt[-1] if excerpt else f"it exited with status {code}"
            return Outcome(
                False, "exited",
                f"The dashboard server exited during startup: {reason}",
                exit_code=EXIT_NO_BUNDLE if code == EXIT_NO_BUNDLE else 1,
                log_excerpt=excerpt,
            )
        if clock() >= deadline:
            break
        sleep(0.2)

    _terminate(child.pid, timeout=2.0, clock=clock, sleep=sleep)
    _remove_pid_file(files.pid_file, only_if=child.pid)
    return Outcome(
        False, "timeout",
        f"The dashboard server (PID {child.pid}) did not answer at {url} within "
        f"{timeout:.0f}s and was stopped.",
        exit_code=1, log_excerpt=_log_since(files.log_file, offset),
    )


__all__ = [
    "DISABLED",
    "LOG_FILE_NAME",
    "MISCONFIGURED",
    "NO_BUNDLE",
    "PID_FILE_NAME",
    "PORT_CONFLICT",
    "RUNNING",
    "SERVICE_NAME",
    "STALE_PID",
    "STOPPED",
    "UNRESPONSIVE",
    "Configured",
    "Outcome",
    "ServerFiles",
    "ServerStatus",
    "bundle_directory",
    "bundle_installed",
    "configured",
    "describe",
    "inspect",
    "installed_manifest_sha256",
    "probe_identity",
    "process_alive",
    "read_pid_file",
    "server_environment",
    "source_root",
    "start",
    "stop",
]
