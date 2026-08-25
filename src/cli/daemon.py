"""Daemon lifecycle commands (aq start/stop/restart/logs).

Manages the agent-queue daemon process using PID file tracking and
signal-based shutdown, replicating the logic from ``run.sh`` in Python
so the CLI is fully self-contained.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from .app import cli, console

CONFIG_DIR = os.path.expanduser("~/.agent-queue")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")
LOG_PATH = os.path.join(CONFIG_DIR, "daemon.log")
PID_FILE = os.path.join(CONFIG_DIR, "daemon.pid")
LOCK_DIR = os.path.join(CONFIG_DIR, "daemon.lock")

DASHBOARD_PORT = 5173  # Vite default; matches dashboard/vite.config.ts
DASHBOARD_PID_FILE = os.path.join(CONFIG_DIR, "dashboard.pid")
DASHBOARD_LOG_PATH = os.path.join(CONFIG_DIR, "dashboard.log")


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
    # Fallback: search for process
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"agent-queue.*{CONFIG_PATH}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().split()[0])
    except (FileNotFoundError, ValueError):
        pass
    return None


def _resolve_agent_queue_bin() -> str:
    """Find the agent-queue entry point binary."""
    import shutil

    path = shutil.which("agent-queue")
    if path:
        return path
    # Fallback: try the venv bin
    venv_bin = os.path.join(os.path.dirname(sys.executable), "agent-queue")
    if os.path.exists(venv_bin):
        return venv_bin
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
            "[bold red]Error:[/] docker-compose.yml not found. "
            "Cannot auto-start PostgreSQL."
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
            result = subprocess.run(
                ["docker", "compose", "-f", compose_file, "up", "-d", "postgres"],
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
                    "docker", "compose", "-f", compose_file,
                    "exec", "-T", "postgres",
                    "pg_isready", "-U", "agent_queue",
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

    # If PostgreSQL is configured, ensure Docker + container are running
    if _config_uses_postgres():
        if not _ensure_docker_postgres():
            return False

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

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "/bin/false"
        env["GH_PROMPT_DISABLED"] = "1"
        # Strip Claude Code session markers
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

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
        n.strip()
        for n in listed.stdout.splitlines()
        if n.strip().startswith(_AQ_SESSION_PREFIXES)
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
        console.print(
            "[yellow]Cannot locate the agent-queue repo to launch the dashboard.[/]"
        )
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


def _maybe_prompt_dashboard(no_dashboard: bool) -> None:
    """If the dashboard isn't running, prompt the user to launch it."""
    if no_dashboard:
        return
    if _dashboard_running():
        return
    if _repo_root() is None:
        # Not running from a source checkout; nothing to launch.
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


@cli.command("start")
@click.option("--no-dashboard", is_flag=True, help="Skip the dashboard prompt.")
def daemon_start(no_dashboard: bool) -> None:
    """Start the agent-queue daemon."""
    if not start_daemon():
        raise SystemExit(1)
    _maybe_prompt_dashboard(no_dashboard)


@cli.command("stop")
@click.option(
    "--keep-sessions",
    is_flag=True,
    help="Leave agent tmux sessions running (they are re-adopted on next start).",
)
def daemon_stop(keep_sessions: bool) -> None:
    """Stop the agent-queue daemon and its agent sessions.

    Agent sessions are stopped too: they outlive the daemon by design so a
    *restart* can re-adopt them, but leaving them running after an explicit
    stop means agents working against a dead API with no way to report back.
    Pass ``--keep-sessions`` to preserve them.
    """
    stopped = stop_daemon()
    if keep_sessions:
        if stopped:
            console.print("[dim]Agent sessions left running (--keep-sessions).[/]")
        return
    # After the daemon is down: nothing is left to notice the sessions
    # disappearing and try to reconcile them mid-shutdown.
    stop_agent_sessions()


@cli.command("restart")
@click.option("--no-dashboard", is_flag=True, help="Skip the dashboard prompt.")
def daemon_restart(no_dashboard: bool) -> None:
    """Restart the agent-queue daemon.

    Agent sessions are deliberately left running — ``sessions.adopt_on_start``
    re-adopts them, so in-flight work survives the restart. Use ``aq stop`` to
    end them.
    """
    stop_daemon(quiet=True)
    time.sleep(1)
    if not start_daemon():
        raise SystemExit(1)
    _maybe_prompt_dashboard(no_dashboard)


@cli.command("logs")
@click.option("-n", "--lines", default=50, help="Number of lines to show")
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
def daemon_logs(lines: int, follow: bool) -> None:
    """View daemon logs."""
    if not os.path.exists(LOG_PATH):
        console.print(f"[dim]No log file at {LOG_PATH}[/]")
        return

    if follow:
        # Use tail -f for following
        try:
            subprocess.run(["tail", "-f", "-n", str(lines), LOG_PATH])
        except KeyboardInterrupt:
            pass
    else:
        _tail_log(lines)
