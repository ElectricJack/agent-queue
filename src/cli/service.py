"""``aq service`` -- keep the daemon running across reboots and crashes.

The daemon-free half of auto-restart: ``install`` / ``uninstall`` register or
remove the watchdog with the host's service manager, ``status`` says whether it
is installed and working, and ``run`` / ``check`` are the watchdog itself (the
service entries call ``python -m src.install.watchdog`` directly, which starts
twenty times faster than this command tree).  Policy and mechanisms live in
:mod:`src.install.watchdog` and :mod:`src.install.service`; the guide is
``docs/tutorials/install.md`` ("Keep AQ running").
"""

from __future__ import annotations

from typing import Any

import click

from .app import cli, console


def _refuse_worker(ctx: click.Context) -> None:
    from .daemon import _refuse_worker_daemon_management

    _refuse_worker_daemon_management()


def _print_action(action: Any) -> None:
    from rich.markup import escape

    style = "green" if action.ok else ("yellow" if action.needs_user else "bold red")
    console.print(f"[{style}]{escape(action.summary)}[/{style}]", highlight=False)
    for note in action.notes:
        console.print(f"  [dim]-[/dim] {escape(note)}", highlight=False)
    if action.dry_run and action.preview:
        console.print("[dim]Would write:[/dim]")
        console.print(action.preview.rstrip(), markup=False, highlight=False)
    if action.dry_run and action.commands:
        console.print("[dim]Would run:[/dim]")
        for command in action.commands:
            console.print(f"  {command}", markup=False, highlight=False)
    if action.remediation and not action.ok:
        console.print(f"[bold]next:[/bold] {escape(action.remediation)}", highlight=False)


def _finish(ctx: click.Context, action: Any) -> None:
    from .envelope import emit

    emit(ctx, action.to_dict(), render=lambda _data: _print_action(action))
    if not action.ok:
        raise SystemExit(10 if action.needs_user else 1)


@cli.group("service")
def service_group() -> None:
    """Keep the daemon running across reboots and crashes (auto-restart).

    Installs a watchdog -- a systemd user unit, a launchd agent, or cron entries
    where there is no systemd user manager (WSL) -- that starts the daemon at
    boot and after a crash.  It never overrides a deliberate `aq stop`: a
    stopped daemon stays stopped until `aq start`.  Log:
    ~/.agent-queue/logs/aq-service.log.
    """


@service_group.command("install")
@click.option(
    "--mechanism",
    type=click.Choice(["auto", "systemd", "launchd", "cron"]),
    default="auto",
    show_default=True,
    help="Service manager to use; auto picks launchd, then systemd, then cron.",
)
@click.option(
    "--interval",
    type=click.FloatRange(min=5.0),
    default=None,
    help="Seconds between checks (default 30; 120 for cron, which rounds to minutes).",
)
@click.option("--dry-run", is_flag=True, help="Show what would be written and run.")
@click.pass_context
def service_install(
    ctx: click.Context, mechanism: str, interval: float | None, dry_run: bool
) -> None:
    """Install (or reinstall) the auto-restart watchdog.

    Idempotent: a rerun rewrites the entry, and switching mechanism removes the
    old one.  The PATH of this shell is recorded for the daemon it starts, so
    run it from your own terminal.  Exits 0 when installed, 10 when the host
    offers no mechanism (the output says what to change), 1 on a failure.
    """
    from src.install.service import install_service
    from src.install.watchdog import resolve_aq

    if not dry_run:
        _refuse_worker(ctx)
    action = install_service(
        aq=resolve_aq(), requested=mechanism, interval=interval, dry_run=dry_run
    )
    _finish(ctx, action)


@service_group.command("uninstall")
@click.option("--dry-run", is_flag=True, help="Show what would be removed.")
@click.pass_context
def service_uninstall(ctx: click.Context, dry_run: bool) -> None:
    """Remove the auto-restart watchdog.  The daemon is left as it is."""
    from src.install.service import uninstall_service

    if not dry_run:
        _refuse_worker(ctx)
    _finish(ctx, uninstall_service(dry_run=dry_run))


@service_group.command("status")
@click.pass_context
def service_status_command(ctx: click.Context) -> None:
    """Show whether auto-restart is installed and working.

    Answers without a daemon: it reads the install record, asks the service
    manager, and reads the watchdog's last check.  Exits 1 when it is
    installed but needs attention.
    """
    from rich.markup import escape

    from src.install.service import service_status

    from .envelope import emit

    status = service_status()

    def _render(_data: Any) -> None:
        if not status.installed:
            style = "yellow"
        else:
            style = "green" if status.healthy else "bold yellow"
        console.print(f"[{style}]{escape(status.summary)}[/{style}]", highlight=False)
        for problem in status.problems[1:] if not status.healthy else ():
            console.print(f"  [yellow]![/yellow] {escape(problem)}", highlight=False)
        for note in status.notes:
            console.print(f"  [dim]-[/dim] {escape(note)}", highlight=False)
        if status.watchdog and status.watchdog.get("detail"):
            console.print(
                f"  last verdict: {status.watchdog.get('verdict')}: "
                f"{status.watchdog.get('detail')}",
                style="dim",
                markup=False,
                highlight=False,
            )
        console.print(f"  log: {status.log}", style="dim", markup=False, highlight=False)

    emit(ctx, status.to_dict(), render=_render)
    if status.installed and not status.healthy:
        raise SystemExit(1)


@service_group.command("check")
@click.option("--boot", is_flag=True, help="First check after boot: skip the confirmation.")
@click.option(
    "--reset", is_flag=True, help="Clear failed-start backoff and the give-up state first."
)
@click.option("--interval", type=click.FloatRange(min=5.0), default=None, hidden=True)
@click.pass_context
def service_check(ctx: click.Context, boot: bool, reset: bool, interval: float | None) -> None:
    """Run one watchdog check now: start the daemon if it is down and may be.

    The same check the service runs.  It never starts a daemon that was stopped
    on purpose (`aq stop`) or while `aq start` / `aq update` is running.
    """
    import time

    from src.install.watchdog import DEFAULT_INTERVAL, LocalHost, Verdict, default_home, tick

    from .envelope import emit

    _refuse_worker(ctx)
    result = tick(
        LocalHost(home=default_home()),
        now=time.time,
        boot=boot,
        reset=reset,
        mode="check",
        interval=interval or DEFAULT_INTERVAL,
    )

    def _render(_data: Any) -> None:
        console.print(f"{result.verdict.value}: {result.detail}", markup=False, highlight=False)

    emit(ctx, result.to_dict(), render=_render)
    if result.verdict is Verdict.START_FAILED:
        raise SystemExit(1)


@service_group.command("run")
@click.option("--interval", type=click.FloatRange(min=5.0), default=None)
@click.pass_context
def service_run(ctx: click.Context, interval: float | None) -> None:
    """Run the watchdog loop in the foreground (what the service manager runs)."""
    from src.install.watchdog import main

    from .envelope import reject_json_mode

    _refuse_worker(ctx)
    reject_json_mode(ctx, "aq service run", "it runs a foreground loop that logs to a file")
    argv = ["run"] + (["--interval", f"{interval:g}"] if interval else [])
    raise SystemExit(main(argv))
