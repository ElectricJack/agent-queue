"""`aq update` — stop AQ, update it to the latest code, rebuild, restart.

The work and its safety rules live in :mod:`src.install.update`; this module
is the terminal around them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from .app import cli, console


def _host():
    from src.install.platform import describe_host
    from src.install.state import default_state_dir
    from src.install.update import Host, _distribution_present, find_pg_dump, installed_extras

    facts = describe_host().facts
    venv_aq = Path(sys.executable).parent / "aq"
    return Host(
        python=sys.executable,
        aq=str(venv_aq) if venv_aq.exists() else "aq",
        system=facts.system,
        arch=facts.arch,
        state_dir=default_state_dir(),
        pg_dump=find_pg_dump(),
        extras=installed_extras(_distribution_present),
    )


@cli.command("update")
@click.option("--check", is_flag=True, help="Only report whether an update is available.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Update without asking first.")
@click.option(
    "--no-backup",
    is_flag=True,
    help="Skip the database backup an update with migrations otherwise takes first.",
)
def update(check: bool, assume_yes: bool, no_backup: bool) -> None:
    """Update AQ to the latest code and restart what needs restarting.

    Fetches the branch this installation follows, and if it is behind: backs up
    the database when the update changes its schema, stops the daemon (running
    agents keep running), fast-forwards the code, and then lets the new code
    finish in a fresh process: reinstall dependencies and rebuild the dashboard
    when they changed, and start the daemon again.

    If any of that fails, AQ is rolled back to the version it was on and
    started again — except after the new daemon has started with database
    migrations, when rolling back could run old code against a newer schema;
    then it stops and names the backup.

    Refuses, changing nothing, when the checkout has local edits or commits,
    when run inside an agent's worker slot, or while another update runs.
    Exit codes: 0 updated or already up to date, 10 refused, 20 failed.
    """
    # Everything this process needs is imported before it moves the checkout:
    # a module first imported after `git merge` would be the *new* code
    # running inside the old process.  What has to run *on* the new code --
    # dependencies, the dashboard build, starting and validating the daemon --
    # is not run here at all: `apply_update` hands it to a fresh
    # `python -m src.install.update_finish` started from the new checkout.
    from src.install.dashboard import source_checkout_root
    from src.install.update import (
        EXIT_REFUSED,
        OUTCOME_UP_TO_DATE,
        UpdateRefused,
        apply_update,
        dashboard_server_running,
        describe,
        plan_update,
    )

    checkout = source_checkout_root()
    if checkout is None:
        console.print(
            "[bold red]aq update[/] updates an AQ installed from a source checkout, and this "
            "one was installed from a release package. Upgrade the package instead."
        )
        raise SystemExit(EXIT_REFUSED)

    host = _host()
    try:
        console.print(f"Checking {checkout} for updates...")
        plan = plan_update(checkout)
    except UpdateRefused as refused:
        _refused(refused)
        raise SystemExit(EXIT_REFUSED) from None

    if plan.up_to_date:
        console.print(f"AQ is up to date ({plan.upstream} at {plan.current[:9]}).")
        return

    from src.install.onboarding import _read_config, api_base_url

    base = api_base_url(_read_config(host.state_dir / "config.yaml"))
    daemon_running = host.probe(f"{base}/health") in (200, 503)
    console.print("\n[bold]An update is available:[/]")
    for line in describe(
        plan,
        backup=not no_backup,
        daemon_running=daemon_running,
        dashboard_server_running=dashboard_server_running(host),
    ):
        console.print(f"  • {line}")
    for subject in plan.subjects[:10]:
        console.print(f"    [dim]{subject}[/]")
    if len(plan.subjects) > 10:
        console.print(f"    [dim]… and {len(plan.subjects) - 10} more[/]")

    if check:
        return
    if not assume_yes:
        if not sys.stdin.isatty():
            console.print("Rerun with `--yes` to update without a terminal to confirm on.")
            raise SystemExit(EXIT_REFUSED)
        if not click.confirm("\nUpdate now?", default=True):
            console.print("Nothing was changed.")
            return
    console.print("")

    def progress(name: str, ok: bool, message: str) -> None:
        mark = "[green]OK[/]" if ok else "[bold red]XX[/]"
        console.print(f"  {mark} {name}" + (f" [dim]— {message}[/]" if message else ""))

    try:
        report = apply_update(plan, host, backup=not no_backup, progress=progress)
    except UpdateRefused as refused:
        _refused(refused)
        raise SystemExit(EXIT_REFUSED) from None

    console.print("")
    if report.outcome == OUTCOME_UP_TO_DATE:
        console.print("AQ is up to date.")
    elif report.exit_code == 0:
        console.print(f"[bold green]AQ is updated[/] to {plan.target[:9]}.")
        if report.backup:
            console.print(f"[dim]Database backup from before the update: {report.backup}[/]")
    else:
        heading = "rolled back" if report.outcome == "rolled_back" else "failed"
        console.print(f"[bold red]The update {heading}.[/] {report.remediation}")
    raise SystemExit(report.exit_code)


def _refused(refused) -> None:
    console.print(f"[bold red]Not updating:[/] {refused.reason}")
    console.print(f"[dim]next:[/] {refused.remediation}")
