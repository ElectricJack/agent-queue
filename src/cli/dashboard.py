"""``aq dashboard`` -- the dashboard server process (docs/specs/dashboard-server.md §1).

A hand-written group: ``src/cli/auto_commands.py`` merges the generated
``state-*`` commands into it.  The process commands here are local, like
``aq start``: they are not ``CommandHandler`` commands and must work while
the daemon is down.

``serve`` runs the server in the foreground; ``start`` / ``stop`` /
``restart`` / ``status`` manage it in the background through
:mod:`src.dashboard_server.process`, and ``aq start`` / ``aq stop`` /
``aq restart`` / ``aq status`` call the same helpers (defined here, so the
PID file, log and config paths come from one place: ``src/cli/daemon.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.markup import escape

from .app import cli, console

if TYPE_CHECKING:
    from src.dashboard_server.process import Outcome, ServerFiles, ServerStatus


# ---------------------------------------------------------------------------
# Helpers shared with `aq start` / `aq stop` / `aq restart` / `aq status`
# ---------------------------------------------------------------------------


def server_files() -> ServerFiles:
    """The managed server's config, PID file and log, read at call time."""
    from src.dashboard_server.process import ServerFiles

    from . import daemon

    return ServerFiles(
        config=Path(daemon.CONFIG_PATH),
        pid_file=Path(daemon.DASHBOARD_SERVER_PID_FILE),
        log_file=Path(daemon.DASHBOARD_SERVER_LOG_PATH),
    )


def dashboard_server_status(api_url: str | None = None) -> ServerStatus:
    """The dashboard server's state; never raises, never needs the daemon."""
    from src.dashboard_server.process import MISCONFIGURED, ServerStatus, inspect

    try:
        return inspect(server_files(), api_url=api_url)
    except Exception as error:  # noqa: BLE001 - a status line must not break `aq status`
        return ServerStatus(MISCONFIGURED, detail=f"could not inspect it: {error}", enabled=False)


def _print_excerpt(outcome: Outcome) -> None:
    for line in outcome.log_excerpt:
        console.print(f"  {line}", style="dim", highlight=False, markup=False)
    if outcome.log_excerpt:
        console.print(f"Logs: {server_files().log_file}", style="dim", highlight=False, markup=False)


def _print_outcome(outcome: Outcome) -> None:
    if outcome.ok:
        style = "bold green" if outcome.action in {"started", "stopped"} else "dim"
        console.print(outcome.message, style=style, highlight=False, markup=False)
        if outcome.action == "started":
            console.print(
                f"Logs: tail -f {server_files().log_file}", style="dim", highlight=False,
                markup=False,
            )
        return
    console.print(f"[bold red]Error:[/] {escape(outcome.message)}", highlight=False)
    _print_excerpt(outcome)


def start_dashboard_server(*, api_url: str | None = None) -> Outcome:
    from src.dashboard_server import process

    return process.start(server_files(), api_url=api_url)


def stop_dashboard_server(*, quiet: bool = False) -> Outcome:
    """`aq stop`'s half.  Says nothing when there was nothing to stop.

    ``quiet`` also drops the "stopped" line (`aq restart` starts it again and
    says so); a failure is always printed.
    """
    from src.dashboard_server import process

    outcome = process.stop(server_files())
    if not outcome.ok or (outcome.action == "stopped" and not quiet):
        _print_outcome(outcome)
    return outcome


def ensure_dashboard_server() -> Outcome | None:
    """`aq start`'s half: start the dashboard server when this install has one.

    ``None`` when there is nothing to manage -- no bundle (a source checkout,
    where the Vite prompt takes over) or ``dashboard.server.enabled: false``.
    A failure is reported, never raised: the daemon is up, and ``aq status``
    and ``aq doctor`` keep showing what is wrong until ``aq start`` heals it.
    """
    outcome = start_dashboard_server()
    if outcome.action == "no_bundle":
        return None
    if outcome.action == "disabled":
        console.print(
            "[dim]Dashboard server not started (dashboard.server.enabled is false).[/]",
            highlight=False,
        )
        return None
    if outcome.ok:
        _print_outcome(outcome)
        return outcome
    console.print(
        "[bold yellow]Warning:[/] the daemon is up, but the dashboard server is not: "
        f"{escape(outcome.message)}",
        highlight=False,
    )
    _print_excerpt(outcome)
    console.print(
        "Fix the reported problem and run `aq dashboard start`; `aq doctor` checks it.",
        style="dim", highlight=False, markup=False,
    )
    return outcome


def render_status_line(status: ServerStatus, *, label: str = "Dashboard") -> None:
    from src.dashboard_server.process import RUNNING, describe

    style = "green" if status.state == RUNNING and status.bundle_current is not False else "yellow"
    console.print(f"[bold]{label}:[/] [{style}]{escape(describe(status))}[/]", highlight=False)


# ---------------------------------------------------------------------------
# The group
# ---------------------------------------------------------------------------


@cli.group("dashboard")
def dashboard_group() -> None:
    """The dashboard server process and durable dashboard state."""


@dashboard_group.command("serve")
@click.option("--host", default=None, help="Bind address (default: dashboard.server.host).")
@click.option(
    "--port", type=click.IntRange(1, 65535), default=None,
    help="Bind port (default: dashboard.server.port, 8082).",
)
@click.pass_context
def dashboard_serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Serve the verified dashboard and proxy the daemon, in the foreground.

    Serves the bundle packaged with this install at / and forwards /api,
    /health, /ready and /ws to the daemon (the global --api-url, else
    AQ_API_URL, else mcp_server).  Logs go to stderr; stop it with Ctrl-C.
    A source checkout has no bundle and exits 2: run
    `npm -w dashboard run dev` there instead.
    """
    from .envelope import reject_json_mode

    reject_json_mode(ctx, "aq dashboard serve", "it runs a foreground server that logs to stderr")
    from src.dashboard_server.__main__ import main

    argv: list[str] = []
    if host is not None:
        argv += ["--host", host]
    if port is not None:
        argv += ["--port", str(port)]
    api_url = _api_url(ctx)
    if api_url:
        argv += ["--api-url", str(api_url)]
    code = main(argv)
    if code:
        raise SystemExit(code)


def _api_url(ctx: click.Context) -> str | None:
    value = (ctx.find_root().obj or {}).get("api_url")
    return str(value) if value else None


def _finish(ctx: click.Context, outcome: Outcome) -> None:
    """Report *outcome* (the JSON envelope under ``--json``) and exit with its code."""
    from .envelope import emit, emit_error

    if (ctx.find_root().obj or {}).get("json"):
        if outcome.ok:
            emit(ctx, outcome.to_dict())
        else:
            emit_error(f"dashboard_server_{outcome.action}", outcome.message, outcome.to_dict())
    else:
        _print_outcome(outcome)
    if not outcome.ok:
        raise SystemExit(outcome.exit_code or 1)


@dashboard_group.command("start")
@click.pass_context
def dashboard_start(ctx: click.Context) -> None:
    """Start the dashboard server in the background (idempotent).

    Serves the bundle packaged with this install on dashboard.server.host and
    port, logging to ~/.agent-queue/dashboard-server.log.  A running server
    that serves the installed build is left alone; one serving an older build
    is restarted.  Exits 2 when there is no bundle (a source checkout), 1 on
    any other failure.  `aq start` runs this for you.
    """
    _finish(ctx, start_dashboard_server(api_url=_api_url(ctx)))


@dashboard_group.command("stop")
@click.pass_context
def dashboard_stop(ctx: click.Context) -> None:
    """Stop the background dashboard server (SIGTERM, then SIGKILL after 10s)."""
    from src.dashboard_server import process

    _finish(ctx, process.stop(server_files()))


@dashboard_group.command("restart")
@click.pass_context
def dashboard_restart(ctx: click.Context) -> None:
    """Restart the dashboard server, picking up changed settings or a rebuilt bundle."""
    from src.dashboard_server import process

    stopped = process.stop(server_files())
    if not stopped.ok:
        _finish(ctx, stopped)
        return
    _finish(ctx, start_dashboard_server(api_url=_api_url(ctx)))


@dashboard_group.command("link")
@click.pass_context
def dashboard_link(ctx: click.Context) -> None:
    """Show the dashboard origin Discord links name, and why.

    Read-only and local: resolves dashboard.server.public_url (alias
    dashboard.public_url) or a confirmed Tailscale bind from config.yaml the
    way the daemon does, and reports the config source, whether the dashboard
    server's Host/Origin gates admit it, whether the server answers here and
    whether the tailscale CLI does.  A health port is never a dashboard link,
    and reachability from another machine is always unverified.
    """
    import asyncio

    from src import remote_links
    from src.dashboard_server.settings import SettingsError, _read_yaml

    from .envelope import emit, emit_error

    files = server_files()
    try:
        settings = remote_links.DashboardLinkSettings.from_raw(_read_yaml(files.config))
    except (SettingsError, TypeError, ValueError) as error:
        message = f"cannot read {files.config}: {error}"
        if (ctx.find_root().obj or {}).get("json"):
            emit_error("dashboard_link_config", message)
        else:
            console.print(f"[bold red]Error:[/] {escape(message)}", highlight=False)
        raise SystemExit(1) from error
    report = asyncio.run(
        remote_links.diagnose_dashboard_link(settings, probe=remote_links.probe_tailscale)
    )
    status = dashboard_server_status(api_url=_api_url(ctx))
    report["server"] = {"enabled": settings.enabled, "state": status.state}

    def _render(data: dict[str, Any]) -> None:
        if data["url"]:
            console.print(f"[bold]Dashboard link:[/] [green]{escape(data['url'])}[/]")
        else:
            console.print("[bold]Dashboard link:[/] [yellow]unavailable[/]")
            console.print(f"  posts say: {data['notice']}", highlight=False, markup=False)
        lines = [
            f"reason: {data['reason']} -- {data['detail']}",
            f"source: {data['source']}",
        ]
        if data["edge"]["detail"]:
            lines.append(f"edge: {data['edge']['detail']}")
        lines.append(f"dashboard server: {data['server']['state']}")
        tailscale = data["tailscale"]
        addresses = ", ".join(tailscale["addresses"]) or tailscale["detail"]
        lines.append(f"tailscale CLI: {tailscale['cli']} ({addresses})")
        lines.append("remote reachability: unverified -- open the link from the other device")
        for line in lines:
            console.print(f"  {line}", style="dim", highlight=False, markup=False)

    emit(ctx, report, render=_render)


@dashboard_group.command("status")
@click.pass_context
def dashboard_status(ctx: click.Context) -> None:
    """Show whether the dashboard server is running, and where.

    States: running, stopped, disabled, no_bundle, stale_pid, port_conflict,
    unresponsive, misconfigured.  Read from the PID file and the server's
    /__aq/health identity, so it answers while the daemon is down.
    """
    from .envelope import emit

    status = dashboard_server_status(api_url=_api_url(ctx))

    def _render(_data: Any) -> None:
        render_status_line(status, label="Dashboard server")
        if status.identity is not None:
            bundle = status.identity.get("bundle") or {}
            upstream = "answering" if status.identity.get("upstream_ok") else "not answering"
            console.print(
                f"  bundle {bundle.get('version', '?')} ({bundle.get('files', '?')} files); "
                f"proxying {status.identity.get('api_url')} (daemon {upstream})",
                style="dim", highlight=False, markup=False,
            )
        if status.log_file:
            console.print(f"  log: {status.log_file}", style="dim", highlight=False, markup=False)

    emit(ctx, status.to_dict(), render=_render)
