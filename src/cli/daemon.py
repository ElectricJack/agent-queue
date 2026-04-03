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

import click

from .app import cli, console

CONFIG_DIR = os.path.expanduser("~/.agent-queue")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")
LOG_PATH = os.path.join(CONFIG_DIR, "daemon.log")
PID_FILE = os.path.join(CONFIG_DIR, "daemon.pid")
LOCK_DIR = os.path.join(CONFIG_DIR, "daemon.lock")


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
            capture_output=True, text=True,
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

        # Wait and verify
        time.sleep(2)
        try:
            os.kill(proc.pid, 0)
            console.print(f"[bold green]Daemon started[/] (PID {proc.pid})")
            console.print(f"[dim]Logs: tail -f {LOG_PATH}[/]")
            return True
        except OSError:
            console.print("[bold red]Error:[/] Daemon exited immediately. Check logs:")
            _tail_log(20)
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            return False
    finally:
        try:
            os.rmdir(LOCK_DIR)
        except OSError:
            pass


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
        with open(LOG_PATH) as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                console.print(line.rstrip())
    except OSError as e:
        console.print(f"[bold red]Error reading log:[/] {e}")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@cli.command("start")
def daemon_start() -> None:
    """Start the agent-queue daemon."""
    if not start_daemon():
        raise SystemExit(1)


@cli.command("stop")
def daemon_stop() -> None:
    """Stop the agent-queue daemon."""
    stop_daemon()


@cli.command("restart")
def daemon_restart() -> None:
    """Restart the agent-queue daemon."""
    stop_daemon(quiet=True)
    time.sleep(1)
    if not start_daemon():
        raise SystemExit(1)


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
