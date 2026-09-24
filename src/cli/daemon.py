"""Daemon lifecycle commands (aq start/stop/restart/logs).

Manages the agent-queue daemon process using PID file tracking and
signal-based shutdown, replicating the logic from ``run.sh`` in Python
so the CLI is fully self-contained.

The dashboard server (docs/specs/dashboard-server.md §1) rides along: when a
verified bundle is installed, ``aq start`` starts it once the daemon answers,
``aq stop`` stops it before the daemon, and ``aq restart`` restarts both.  The
process itself is :mod:`src.dashboard_server.process`; the CLI glue is in
:mod:`src.cli.dashboard`.  A source checkout with no bundle keeps the
interactive offer to launch the Vite dev server instead.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from src.env_scrub import harness_session_markers, strip_harness_session_markers
from src.sessions.env import (
    AQ_MARKER_KEYS,
    DAEMON_ENV_STRIP_KEYS,
    DB_ISOLATION_KEYS,
)

from .app import cli, console

CONFIG_DIR = os.path.expanduser("~/.agent-queue")
BACKUPS_DIR = os.path.join(CONFIG_DIR, "backups")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")
LOG_PATH = os.path.join(CONFIG_DIR, "daemon.log")
PID_FILE = os.path.join(CONFIG_DIR, "daemon.pid")
LOCK_DIR = os.path.join(CONFIG_DIR, "daemon.lock")

#: The Vite dev server a source checkout runs (dashboard/vite.config.ts).  The
#: dashboard *server*'s port is configuration -- ``dashboard.server.port``.
DASHBOARD_PORT = 5173
DASHBOARD_PID_FILE = os.path.join(CONFIG_DIR, "dashboard.pid")
DASHBOARD_LOG_PATH = os.path.join(CONFIG_DIR, "dashboard.log")
#: The managed dashboard server.  Its own names, not Vite's: an older release's
#: ``dashboard.pid`` is a Vite process and must never be mistaken for it.
DASHBOARD_SERVER_PID_FILE = os.path.join(CONFIG_DIR, "dashboard-server.pid")
DASHBOARD_SERVER_LOG_PATH = os.path.join(CONFIG_DIR, "dashboard-server.log")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_pid() -> int | None:
    """Read and validate the PID from the PID file."""
    if not os.path.exists(PID_FILE):
        return None
    try:
        pid = int(open(PID_FILE).read().strip())
    except (ValueError, OSError):
        return None
    # Check if process is actually running
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        # Stale PID file
        os.remove(PID_FILE)
        return None


def _find_daemon_pid() -> int | None:
    """Find a running daemon PID via PID file or pgrep fallback."""
    pid = _read_pid()
    if pid:
        return pid
    # A dashboard server receives the same config path, so matching only the
    # checkout name and config can mistake it for the daemon. The daemon is
    # always launched as ``agent-queue <config>`` in ``start_daemon``; require
    # that exact argv suffix when recovering without a PID file.
    try:
        result = subprocess.run(
            ["pgrep", "-f", rf"(^|/)agent-queue {re.escape(CONFIG_PATH)}$"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().split()[0])
    except (FileNotFoundError, ValueError):
        pass
    return None


def _resolve_agent_queue_bin() -> str:
    """Find the agent-queue entry point for the active installation."""
    import shutil

    # A checkout-local ``aq`` must restart the matching checkout-local
    # daemon.  Looking at PATH first can silently select an older user-wide
    # install and make a successful source deployment appear ineffective.
    venv_bin = os.path.join(os.path.dirname(sys.executable), "agent-queue")
    if os.path.exists(venv_bin):
        return venv_bin
    path = shutil.which("agent-queue")
    if path:
        return path
    return "agent-queue"


def is_daemon_running() -> bool:
    """Check if the daemon is currently running."""
    return _find_daemon_pid() is not None


def _is_docker_running() -> bool:
    """Check whether the Docker daemon is reachable."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_container_running(name: str) -> bool:
    """Check if a Docker container is running by name."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _find_compose_file() -> str | None:
    """Find docker-compose.yml relative to the project root."""
    project_root = Path(__file__).resolve().parent.parent.parent
    compose_file = project_root / "docker-compose.yml"
    if compose_file.exists():
        return str(compose_file)
    return None


def _start_docker_desktop() -> bool:
    """Attempt to start Docker Desktop (WSL2 / Linux)."""
    # On WSL2, Docker Desktop is a Windows app
    wsl_docker = "/mnt/c/Program Files/Docker/Docker/Docker Desktop.exe"
    if os.path.exists(wsl_docker):
        console.print("[dim]Starting Docker Desktop...[/]")
        try:
            subprocess.Popen(
                [wsl_docker],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass
    else:
        # Native Linux — try systemctl
        console.print("[dim]Starting Docker via systemctl...[/]")
        try:
            subprocess.run(
                ["sudo", "systemctl", "start", "docker"],
                capture_output=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Wait for Docker daemon to become reachable
    for i in range(60):
        if _is_docker_running():
            return True
        if i % 10 == 0 and i > 0:
            console.print(f"[dim]  Waiting for Docker daemon... ({i}s)[/]")
        time.sleep(1)
    return False


def _ensure_docker_postgres() -> bool:
    """Ensure Docker and the aq-postgres container are running.

    Returns True if Postgres is ready, False if it could not be started.
    """
    compose_file = _find_compose_file()
    if not compose_file:
        console.print(
            "[bold red]Error:[/] docker-compose.yml not found. Cannot auto-start PostgreSQL."
        )
        return False

    # Step 1: Make sure Docker itself is running
    if not _is_docker_running():
        console.print("[yellow]Docker is not running.[/]")
        if not _start_docker_desktop():
            console.print(
                "[bold red]Error:[/] Could not start Docker. "
                "Please start Docker Desktop manually and retry."
            )
            return False
        console.print("[green]Docker is running.[/]")

    # Step 2: Start the Postgres container if not already running
    if not _is_container_running("aq-postgres"):
        console.print("[dim]Starting PostgreSQL container...[/]")
        try:
            inspection = subprocess.run(
                ["docker", "container", "inspect", "aq-postgres"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if inspection.returncode == 0:
                command = ["docker", "start", "aq-postgres"]
            elif any(
                f"no such {kind}: aq-postgres" in inspection.stderr.lower()
                for kind in ("container", "object")
            ):
                command = [
                    "docker", "compose", "-f", compose_file,
                    "up", "-d", "--no-recreate", "postgres",
                ]
            else:
                console.print("[bold red]Error:[/] Could not inspect PostgreSQL container")
                if inspection.stderr:
                    console.print(f"[dim]{inspection.stderr.strip()}[/]")
                return False
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                console.print("[bold red]Error:[/] Failed to start PostgreSQL container")
                if result.stderr:
                    console.print(f"[dim]{result.stderr.strip()}[/]")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            console.print(f"[bold red]Error:[/] {e}")
            return False

    # Step 3: Wait for Postgres to accept connections
    console.print("[dim]Waiting for PostgreSQL to be ready...[/]")
    for i in range(30):
        try:
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    compose_file,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_isready",
                    "-U",
                    "agent_queue",
                ],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                console.print("[green]PostgreSQL is ready.[/]")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        time.sleep(1)

    console.print("[bold red]Error:[/] PostgreSQL did not become ready in 30s.")
    return False


def _config_uses_postgres() -> bool:
    """Quick check whether the config file references PostgreSQL."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("url:") and "postgresql" in stripped:
                    return True
    except OSError:
        pass
    return False


def _configured_database_endpoint() -> tuple[str, int] | None:
    """The host and port of the configured database, or ``None`` if unreadable.

    Read from the raw YAML rather than through ``load_config``: this runs
    before the daemon and must not depend on the whole configuration being
    valid, and it deliberately never looks at the password.
    """
    import yaml

    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return None
    section = raw.get("database")
    url = section.get("url") if isinstance(section, dict) else None
    if not isinstance(url, str) or not url:
        return None
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        return (parts.hostname or "localhost", int(parts.port or 5432))
    except ValueError:
        return None


def _database_reachable() -> bool:
    """True when something is already listening where the configuration points.

    A PostgreSQL that `aq install` provisioned — or any server the operator
    already runs — is the normal case, and it has nothing to do with Docker.
    Probing first is what keeps `aq start` from demanding a compose file on a
    machine that has a perfectly good database.
    """
    endpoint = _configured_database_endpoint()
    if endpoint is None:
        return False
    import socket

    host, port = endpoint
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _ensure_database() -> bool:
    """Make sure the configured database answers before the daemon is started.

    Three cases, in the order they are true on real machines:

    1. Something is already listening — the installed or operator-run server.
       Nothing else to do.
    2. Nothing is listening and this is a source checkout with a compose file —
       start the development container, as `aq start` always has.
    3. Nothing is listening and there is no compose file — say so, and name the
       command that installs or repairs one. AQ is PostgreSQL-only, so a
       missing server is the whole problem, not a missing Docker.
    """
    if _database_reachable():
        return True
    if _find_compose_file():
        return _ensure_docker_postgres()
    endpoint = _configured_database_endpoint()
    where = f"{endpoint[0]}:{endpoint[1]}" if endpoint else "the configured address"
    console.print(
        f"[bold red]Error:[/] no PostgreSQL server is answering at {where}.\n"
        "[dim]Start the server you configured, or run `aq install` to install and "
        "configure one (`aq install --with postgres-managed`).[/]"
    )
    return False


def _daemon_environment(*, home: str | None = None) -> dict[str, str]:
    """Build a stable daemon environment for non-login shell launches."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    env["GH_PROMPT_DISABLED"] = "1"
    # Preserve the caller's PATH precedence, then expose conventional
    # per-user executable locations omitted by non-login service shells.
    user_home = home or str(Path.home())
    path_parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    for candidate in (
        os.path.join(user_home, ".local", "bin"),
        os.path.join(user_home, ".local", "share", "pnpm"),
    ):
        if os.path.isdir(candidate) and candidate not in path_parts:
            path_parts.append(candidate)
    env["PATH"] = os.pathsep.join(path_parts)
    strip_harness_session_markers(env)
    # Every marker a harness or worker session may have set must die here,
    # not just the narrow per-session block.  The session/DB keys that leak
    # AQ_DB_SCOPE, AQ_DATABASE_URL and AGENT_QUEUE_DB into the daemon are the
    # 2026-09-21 incident, so they are part of the default strip set.
    for key in set(AQ_MARKER_KEYS) | set(DB_ISOLATION_KEYS) | set(DAEMON_ENV_STRIP_KEYS):
        env.pop(key, None)
    return env


def _warn_harness_environment(command: str) -> None:
    """Explain that a daemon launched from a harness will be sanitized."""
    markers = sorted(
        set(harness_session_markers(os.environ))
        .union(AQ_MARKER_KEYS, DB_ISOLATION_KEYS, DAEMON_ENV_STRIP_KEYS)
        .intersection(os.environ)
    )
    if markers:
        console.print(
            f"[yellow]aq {command} detected enclosing harness marker(s) and/or AQ session marker(s): "
            f"{', '.join(markers)}. The daemon will be launched with them removed.[/]"
        )


def _is_worker_session_environment() -> bool:
    """Whether this environment belongs to a worker that cannot manage AQ."""
    return os.environ.get("AQ_SESSION_KIND") in {"pool", "task"} or (
        os.environ.get("AQ_DB_SCOPE") == "worker" and bool(os.environ.get("AQ_SESSION_ID"))
    )


def _refuse_worker_daemon_management() -> None:
    """Stop workers before a lifecycle command can change operator state."""
    if _is_worker_session_environment():
        console.print(
            "[bold red]Refused:[/] a worker must never manage the operator's daemon; "
            "report the need to the operator instead."
        )
        raise SystemExit(10)


def _database_is_at_head() -> bool:
    """Whether the stamped schema matches this checkout's Alembic head.

    Fail-safe: an unreachable database, a missing table, or any other check
    error returns ``False`` — that is the safe side, because the caller then
    backs the database up rather than skipping it.  Mirrors ``aq db current``.
    """
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        from .db import _head_revisions, _load_config, _make_engine

        async def _main() -> list[str]:
            config = _load_config()
            engine, _url = _make_engine(config)
            try:
                async with engine.connect() as conn:
                    result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                    return sorted(row[0] for row in result.fetchall())
            except SQLAlchemyError:
                return []  # no alembic_version table — unstamped
            finally:
                await engine.dispose()

        return asyncio.run(_main()) == _head_revisions()
    except Exception:  # noqa: BLE001 — fail-safe: a check error must read as "not at head"
        return False


def _backup_database() -> None:
    """Dump the configured Postgres database to BACKUPS_DIR.

    Mirrors the operator wrapper: ``pg_dump`` inside the ``aq-postgres``
    container is ``docker cp``-ed out, and a dump that is missing the marker
    comment is refused as incomplete.  Any failure is a loud error — never a
    silent skip.
    """
    import datetime

    try:
        os.makedirs(BACKUPS_DIR, exist_ok=True)
    except OSError as exc:
        raise SystemExit(12) from exc

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(BACKUPS_DIR, f"pre-deploy-{ts}.sql")
    container, user, db = "aq-postgres", "agent_queue", "agent_queue"
    tmp = f"/tmp/pre-deploy-{ts}.sql"

    def _docker(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, check=True)

    try:
        _docker("docker", "exec", container, "pg_dump", "-U", user, "-d", db, "-f", tmp)
        _docker("docker", "cp", f"{container}:{tmp}", out)
        _docker("docker", "exec", container, "rm", "-f", tmp)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(13) from exc

    # The wrapper's integrity check: a real pg_dump carries that comment.
    size = os.path.getsize(out) if os.path.exists(out) else 0
    try:
        with open(out, "rb") as handle:
            if size > 4096:
                handle.seek(-4096, os.SEEK_END)
            tail = handle.read().decode("utf-8", "replace")
    except OSError as exc:
        raise SystemExit(14) from exc
    if "unrestrict" not in tail:
        raise SystemExit(15)
    console.print(f"[green]Backup written[/] {out} ({size:,} bytes)")


def _post_daemon_checks() -> None:
    """Finish what the daemon's start left undone: readiness and the pool fix.

    The operator wrapper waited for ``/ready`` (not just ``/health``) and then
    ran the stale-worktree doctor fix, because a daemon that answers ``/health``
    is not yet reconciling pools.  Both are now baked into ``aq start`` and
    ``aq restart`` so the wrapper is unnecessary.
    """
    import urllib.error
    import urllib.request

    from .client import _resolve_api_url

    api_base = _resolve_api_url()

    def _ready_ok() -> bool:
        try:
            with urllib.request.urlopen(f"{api_base}/ready", timeout=5) as resp:
                return resp.status == 200
        except urllib.error.HTTPError:
            return False
        except (urllib.error.URLError, OSError):
            return False

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _ready_ok():
            console.print("[green]Daemon is ready.[/]")
            break
        time.sleep(1)
    else:
        console.print("[yellow]Daemon is up but still not /ready after 60s.[/]")

    # Let the daemon repair the stale worktrees it owns.  Fail-safe: this is a
    # postcondition, not a gate — a doctor transport error here must never
    # undo a successful daemon start, so every failure is caught and downgraded.
    try:
        result = subprocess.run(
            [
                _resolve_agent_queue_bin(),
                "doctor",
                "--check",
                "pools.stale_worktree_checkouts",
                "--fix",
            ],
            env=_daemon_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — a post-start failure must never kill the start
        console.print(f"[dim]Post-start doctor fix skipped: {exc}[/]")
        return
    for line in result.stderr.strip().splitlines()[-3:]:
        console.print(f"[dim]{line}[/]")
    for line in result.stdout.strip().splitlines()[-3:]:
        console.print(line)


def start_daemon() -> bool:
    """Start the daemon. Returns True on success."""
    if not os.path.exists(CONFIG_PATH):
        console.print(f"[bold red]Error:[/] Config not found at {CONFIG_PATH}")
        console.print("[dim]Run setup first.[/]")
        return False

    existing = _find_daemon_pid()
    if existing:
        console.print(f"[yellow]Daemon is already running[/] (PID {existing})")
        return True

    # The daemon cannot start without its database. Reach for Docker only when
    # nothing is listening *and* this is a checkout that ships a compose file.
    if _config_uses_postgres() and not _ensure_database():
        return False

    # The operator wrapper backed this up before the restart whenever the
    # schema was not confirmed at this checkout's head — because a daemon that
    # then runs its migration is exactly when a bad schema revision can burn
    # the live database.  Mirror that: back up first when not at head.
    if _config_uses_postgres() and not _database_is_at_head():
        _backup_database()

    # Acquire lock
    try:
        os.makedirs(LOCK_DIR)
    except FileExistsError:
        console.print(
            "[bold red]Error:[/] Another start is in progress.\n"
            f"[dim]If not, remove: rm -rf {LOCK_DIR}[/]"
        )
        return False

    try:
        console.print("[bold]Starting agent-queue daemon...[/]")
        bin_path = _resolve_agent_queue_bin()

        env = _daemon_environment()

        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as log_file:
            proc = subprocess.Popen(
                [bin_path, CONFIG_PATH],
                stdout=log_file,
                stderr=log_file,
                env=env,
                start_new_session=True,
            )

        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))

        # Wait until the daemon's HTTP /health endpoint responds — replaces the
        # old fixed 5s pid check, which falsely reported success when crashes
        # (e.g. Discord login failure) happened later in async init. See #28.
        import urllib.error
        import urllib.request

        from .client import _resolve_api_url

        api_base = _resolve_api_url()
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            # Early-crash detect: if the process is gone, surface the log immediately.
            try:
                os.kill(proc.pid, 0)
            except OSError:
                console.print("[bold red]Error:[/] Daemon exited during startup:")
                _tail_log(30)
                try:
                    os.remove(PID_FILE)
                except OSError:
                    pass
                return False
            # Probe /health. 200 = healthy, 503 = degraded but the daemon is up
            # and serving (e.g. messaging-degraded mode from #29) — both count
            # as "started successfully" here.
            try:
                with urllib.request.urlopen(f"{api_base}/health", timeout=1) as resp:
                    if resp.status in (200, 503):
                        ready = True
                        break
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code == 503:
                    console.print(
                        "[yellow]Daemon is running with degraded health; run aq doctor.[/]"
                    )
                    ready = True
                    break
            except (urllib.error.URLError, OSError):
                pass  # not yet listening
            time.sleep(0.5)

        if not ready:
            console.print(
                f"[bold red]Error:[/] Daemon process is alive (PID {proc.pid}) but "
                f"/health didn't respond within 30s. Killing it. Last log lines:"
            )
            _tail_log(30)
            try:
                os.kill(proc.pid, signal.SIGTERM)
                # Brief grace period, then SIGKILL
                for _ in range(5):
                    time.sleep(1)
                    try:
                        os.kill(proc.pid, 0)
                    except OSError:
                        break
                else:
                    try:
                        os.kill(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
            except OSError:
                pass
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            return False

        console.print(f"[bold green]Daemon started[/] (PID {proc.pid})")
        _post_daemon_checks()
        console.print(f"[dim]Logs: tail -f {LOG_PATH}[/]")
        return True
    finally:
        try:
            os.rmdir(LOCK_DIR)
        except OSError:
            pass


#: tmux session-name prefixes the daemon owns: ``s-<task_id>`` for task
#: sessions and ``n-<profile>--<project>`` for named ones.  The reconciler
#: adopts on exactly these two prefixes, so they are also the safe set to
#: reap — anything else on the socket belongs to someone else.
_AQ_SESSION_PREFIXES = ("s-", "n-")


def _tmux_socket() -> str:
    """The tmux socket the daemon launches sessions on.

    Read from config rather than hardcoded: an operator who changed
    ``sessions.tmux_socket`` would otherwise have their sessions silently
    left running by a stop that reported success.
    """
    try:
        from src.config import load_config

        cfg = load_config(os.path.expanduser("~/.agent-queue/config.yaml"))
        return getattr(getattr(cfg, "sessions", None), "tmux_socket", None) or "aq"
    except Exception:
        return "aq"


def stop_agent_sessions(quiet: bool = False) -> int:
    """Kill the daemon's tmux sessions.  Returns how many were stopped.

    Agent sessions deliberately outlive the daemon — ``sessions.adopt_on_start``
    re-adopts them so a restart does not throw away in-flight work.  That is
    right for ``restart`` and wrong for ``stop``: "shut down agent-queue" that
    leaves five agents running against a dead API is not a shutdown, and those
    agents cannot reach ``aq`` to report anything.

    ``tmux kill-session`` hangs up the pane, which the agent CLI treats as a
    normal terminal close.  Work in progress is lost — that is what stopping
    means; use ``restart`` (or ``stop --keep-sessions``) to preserve it.
    """
    socket = _tmux_socket()
    try:
        listed = subprocess.run(
            ["tmux", "-L", socket, "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if listed.returncode != 0:
        # No server on this socket == no sessions.  Not an error.
        return 0

    names = [
        n.strip() for n in listed.stdout.splitlines() if n.strip().startswith(_AQ_SESSION_PREFIXES)
    ]
    stopped = 0
    for name in names:
        try:
            killed = subprocess.run(
                ["tmux", "-L", socket, "kill-session", "-t", name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if killed.returncode == 0:
            stopped += 1
        elif not quiet:
            console.print(f"[yellow]Could not stop session {name}[/]")
    if stopped and not quiet:
        console.print(f"[bold]Stopped {stopped} agent session(s).[/]")
    return stopped


def stop_daemon(quiet: bool = False) -> bool:
    """Stop the daemon. Returns True if it was stopped."""
    pid = _find_daemon_pid()
    if not pid:
        if not quiet:
            console.print("[dim]Daemon is not running.[/]")
        return False

    if not quiet:
        console.print(f"[bold]Stopping daemon[/] (PID {pid})...")

    os.kill(pid, signal.SIGTERM)

    # Wait for graceful shutdown
    for _ in range(10):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(1)
    else:
        # Still running — force kill
        if not quiet:
            console.print("[yellow]Daemon didn't stop gracefully, sending SIGKILL...[/]")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    if not quiet:
        console.print("[bold green]Daemon stopped.[/]")
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    try:
        os.rmdir(LOCK_DIR)
    except OSError:
        pass
    return True


def _tail_log(lines: int = 20) -> None:
    """Print the last N lines of the daemon log."""
    if not os.path.exists(LOG_PATH):
        console.print(f"[dim]No log file at {LOG_PATH}[/]")
        return
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                console.print(line.rstrip())
    except OSError as e:
        console.print(f"[bold red]Error reading log:[/] {e}")


# ---------------------------------------------------------------------------
# Dashboard helpers
# ---------------------------------------------------------------------------


def _dashboard_running(port: int = DASHBOARD_PORT) -> bool:
    """Return True if something is listening on the dashboard port."""
    import socket

    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


def _repo_root() -> Path | None:
    """Best-effort locate the agent-queue repo root from this source file.

    Works for editable installs (pip install -e .) where this file lives at
    ``<repo>/src/cli/daemon.py``. Returns None if the layout doesn't match.
    """
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "package.json").exists() and (candidate / "dashboard").is_dir():
        return candidate
    return None


def _start_dashboard() -> bool:
    """Daemonize ``npm -w dashboard run dev`` and write a PID file. Returns True on success."""
    repo = _repo_root()
    if repo is None:
        console.print("[yellow]Cannot locate the agent-queue repo to launch the dashboard.[/]")
        return False
    if not (repo / "dashboard" / "node_modules").exists():
        console.print(
            f"[yellow]Dashboard deps not installed.[/] Run: [bold]cd {repo} && npm install[/]"
        )
        return False
    import shutil

    if shutil.which("npm") is None:
        console.print("[yellow]npm not found on PATH; cannot launch dashboard.[/]")
        return False

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(DASHBOARD_LOG_PATH, "a") as log_file:
        proc = subprocess.Popen(
            ["npm", "-w", "dashboard", "run", "dev"],
            cwd=str(repo),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    with open(DASHBOARD_PID_FILE, "w") as f:
        f.write(str(proc.pid))

    # Wait briefly for vite to come up
    for _ in range(20):  # ~10s
        time.sleep(0.5)
        try:
            os.kill(proc.pid, 0)
        except OSError:
            console.print("[bold red]Dashboard exited during startup. Last log lines:[/]")
            try:
                with open(DASHBOARD_LOG_PATH, encoding="utf-8", errors="replace") as f:
                    for line in f.readlines()[-20:]:
                        console.print(line.rstrip())
            except OSError:
                pass
            try:
                os.remove(DASHBOARD_PID_FILE)
            except OSError:
                pass
            return False
        if _dashboard_running():
            console.print(
                f"[bold green]Dashboard started[/] at "
                f"http://localhost:{DASHBOARD_PORT} (PID {proc.pid})"
            )
            console.print(f"[dim]Logs: tail -f {DASHBOARD_LOG_PATH}[/]")
            console.print(f"[dim]Stop: kill $(cat {DASHBOARD_PID_FILE})[/]")
            return True

    console.print(
        f"[yellow]Dashboard launched (PID {proc.pid}) but didn't bind port "
        f"{DASHBOARD_PORT} within 10s.[/]"
    )
    console.print(f"[dim]Logs: tail -f {DASHBOARD_LOG_PATH}[/]")
    return True


def _bundle_installed() -> bool:
    from src.dashboard_server.process import bundle_installed

    return bundle_installed()


def _maybe_prompt_dashboard(no_dashboard: bool) -> None:
    """If the Vite dev server isn't running, offer to launch it (source checkouts only)."""
    if no_dashboard:
        return
    if not sys.stdin.isatty():
        # There is nobody to ask. `click.confirm` raises Abort at end of input,
        # which turned a perfectly healthy `aq start` into a failure for every
        # caller that is not a terminal — a script, a CI job, and `aq install`'s
        # own daemon step before it learned to pass `--no-dashboard`.
        return
    if _dashboard_running():
        return
    if _repo_root() is None:
        # Not running from a source checkout; nothing to launch.
        return
    if _bundle_installed():
        # `aq install` built the dashboard and the dashboard server serves it;
        # offering a Vite dev server as well would only confuse.
        return
    console.print("")
    if click.confirm(
        f"Dashboard isn't running on http://localhost:{DASHBOARD_PORT}. Launch it?",
        default=False,
    ):
        _start_dashboard()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


_NO_DASHBOARD_HELP = (
    "Skip the interactive offer to launch the Vite dev server (source checkouts). "
    "Does not stop the dashboard server from starting; see --no-dashboard-server."
)
_NO_DASHBOARD_SERVER_HELP = "Leave the dashboard server alone: manage the daemon only."


def _after_daemon_started(*, no_dashboard: bool, no_dashboard_server: bool) -> None:
    """Bring up the dashboard: the managed server when a bundle exists, else offer Vite.

    ``--no-dashboard`` keeps its historical meaning -- skip the Vite prompt --
    and deliberately does *not* suppress the dashboard server: an updater runs
    ``aq start --no-dashboard`` and then expects the dashboard to be served
    (docs/specs/dashboard-server.md §6.2).  Neither path can block a caller
    with no terminal: the server start never prompts, and the Vite prompt
    returns at once without one.
    """
    if not no_dashboard_server:
        from .dashboard import ensure_dashboard_server

        ensure_dashboard_server()
    _maybe_prompt_dashboard(no_dashboard)


@cli.command("start")
@click.option("--no-dashboard", is_flag=True, help=_NO_DASHBOARD_HELP)
@click.option("--no-dashboard-server", is_flag=True, help=_NO_DASHBOARD_SERVER_HELP)
@click.pass_context
def daemon_start(ctx: click.Context, no_dashboard: bool, no_dashboard_server: bool) -> None:
    """Start the agent-queue daemon, then the dashboard server.

    The dashboard server starts once the daemon answers (also when the daemon
    was already running) if dashboard.server.enabled is true and a verified
    dashboard bundle is installed.  A dashboard server that fails to start is
    reported as a warning; the daemon stays up and `aq status` / `aq doctor`
    show the problem.
    """
    from .envelope import reject_json_mode

    _refuse_worker_daemon_management()
    reject_json_mode(
        ctx,
        "aq start",
        "daemon lifecycle output is subprocess progress and may prompt for the dashboard",
    )
    _warn_harness_environment("start")
    if not start_daemon():
        raise SystemExit(1)
    _after_daemon_started(no_dashboard=no_dashboard, no_dashboard_server=no_dashboard_server)


@cli.command("stop")
@click.option(
    "--keep-sessions",
    is_flag=True,
    help="Leave agent tmux sessions running (they are re-adopted on next start).",
)
@click.option("--no-dashboard-server", is_flag=True, help=_NO_DASHBOARD_SERVER_HELP)
@click.pass_context
def daemon_stop(ctx: click.Context, keep_sessions: bool, no_dashboard_server: bool) -> None:
    """Stop the agent-queue daemon, its agent sessions and the dashboard server.

    Agent sessions are stopped too: they outlive the daemon by design so a
    *restart* can re-adopt them, but leaving them running after an explicit
    stop means agents working against a dead API with no way to report back.
    Pass ``--keep-sessions`` to preserve them.  The dashboard server is
    stopped first, with or without ``--keep-sessions``.
    """
    from .envelope import reject_json_mode

    _refuse_worker_daemon_management()
    reject_json_mode(
        ctx,
        "aq stop",
        "daemon lifecycle output is local process and tmux progress",
    )
    if not no_dashboard_server:
        from .dashboard import stop_dashboard_server

        stop_dashboard_server()
    stopped = stop_daemon()
    if keep_sessions:
        if stopped:
            console.print("[dim]Agent sessions left running (--keep-sessions).[/]")
        return
    # After the daemon is down: nothing is left to notice the sessions
    # disappearing and try to reconcile them mid-shutdown.
    stop_agent_sessions()


@cli.command("restart")
@click.option("--no-dashboard", is_flag=True, help=_NO_DASHBOARD_HELP)
@click.option("--no-dashboard-server", is_flag=True, help=_NO_DASHBOARD_SERVER_HELP)
@click.pass_context
def daemon_restart(ctx: click.Context, no_dashboard: bool, no_dashboard_server: bool) -> None:
    """Restart the agent-queue daemon and the dashboard server.

    Agent sessions are deliberately left running — ``sessions.adopt_on_start``
    re-adopts them, so in-flight work survives the restart. Use ``aq stop`` to
    end them.  Both processes restart, so a changed dashboard.server setting
    or a rebuilt bundle needs no second command.
    """
    from .envelope import reject_json_mode

    _refuse_worker_daemon_management()
    reject_json_mode(
        ctx,
        "aq restart",
        "daemon lifecycle output is subprocess progress and may prompt for the dashboard",
    )
    _warn_harness_environment("restart")
    if not no_dashboard_server:
        from .dashboard import stop_dashboard_server

        stop_dashboard_server(quiet=True)
    stop_daemon(quiet=True)
    time.sleep(1)
    if not start_daemon():
        raise SystemExit(1)
    _after_daemon_started(no_dashboard=no_dashboard, no_dashboard_server=no_dashboard_server)
