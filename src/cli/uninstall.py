"""``aq uninstall`` — remove what ``aq install`` owns, and nothing else.

Like ``aq install`` this command runs without a daemon: it reads the resume
record, classifies every resource it names, and removes only what the operator
selected.  The plan is printed before anything happens, because the interesting
part of an uninstall is not what it removes — it is what it *does not*.

Three rules shape the whole command, all from
``docs/plans/install-onboarding/contract.md``:

* Only ``owned`` resources are candidates.  A PostgreSQL server that was
  already running when ``aq install`` found it is reused, never removed.
* The default scope is the AQ runtime and the installer's own records.
  Configuration, the data directory and the AQ database each need their own
  flag *and* their own confirmation, because each destroys work.
* Something AQ installed but does not exclusively own — a provider CLI, a
  Homebrew formula, a PostgreSQL server package — is reported with the command
  to remove it by hand.  ``tmux`` is AQ's prerequisite and somebody else's
  terminal multiplexer.

Exit codes are the installer's: 0 ready, 10 needs_user, 11 invalid_input,
20 failed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console

from src.install import InstallOutcome, exit_code
from src.install.lifecycle import (
    DEFAULT_SCOPES,
    RemovalScope,
    UninstallResult,
    default_handlers,
    execute_uninstall,
    plan_uninstall,
)
from src.install.state import InstallState, StateError, default_state_path, load_state

from .app import cli, console

#: What each destructive scope is asked about, in the operator's words.  The
#: question names the thing that would be lost, not the flag that was passed.
_CONFIRM: dict[RemovalScope, str] = {
    RemovalScope.CONFIG: ("Remove AQ's configuration files? (a copy is kept beside each as .bak)"),
    RemovalScope.DATA: (
        "Remove AQ's data directory — configuration, vault, projects, memory and "
        "task history? This cannot be undone"
    ),
    RemovalScope.DATABASE: (
        "Drop the PostgreSQL database and role `aq install` created, and every task, "
        "project and result in them? This cannot be undone"
    ),
}


class UninstallInputError(click.ClickException):
    """A bad flag or an unreadable record — reported, not crashed."""

    exit_code = exit_code(InstallOutcome.INVALID_INPUT)


def selected_scopes(
    *, remove_config: bool, remove_data: bool, remove_database: bool, everything: bool
) -> frozenset[RemovalScope]:
    """Fold the removal flags into a scope set.

    ``--all`` is a convenience, never a widening: it selects exactly the three
    destructive scopes that already have flags, so there is nothing an operator
    can remove without a flag naming it.
    """
    scopes = set(DEFAULT_SCOPES)
    if remove_config or everything:
        scopes.add(RemovalScope.CONFIG)
    if remove_data or everything:
        scopes.add(RemovalScope.DATA)
    if remove_database or everything:
        scopes.add(RemovalScope.DATABASE)
    return frozenset(scopes)


def _admin_executor(state: InstallState, *, home: Path) -> tuple[object | None, str]:
    """Resolve a PostgreSQL administrator connection, or say why there is none.

    Imported and resolved lazily, and only when a database removal is actually
    planned: an uninstall that touches no database must not need ``psql``, a
    ``sudo`` credential or an ``asyncpg`` import to run.
    """
    import os
    import shutil

    from src.install.postgres import PostgresSettings, resolve_admin, subprocess_runner

    del home  # settings come from the host's defaults, not from a path
    facts = state.platform or {}
    host_path = str(facts.get("host_path") or "")
    access = resolve_admin(
        PostgresSettings(),
        host_path=host_path,
        runner=subprocess_runner,
        which=shutil.which,
        environ=os.environ,
    )
    return access.executor, (access.remediation or access.reason)


def render_result(result: UninstallResult, target: Console) -> None:
    """Print the plan, then what happened to it."""
    plan = result.plan
    heading = "Plan" if result.dry_run else "Removed"
    if plan.removals:
        target.print(f"\n[bold]{heading}[/bold]")
        rows = (
            [(row.item, row.summary, row.error) for row in result.outcomes]
            if result.outcomes
            else [(item, item.reason, None) for item in plan.removals]
        )
        for item, summary, error in rows:
            style = "bold red" if error else ("dim" if result.dry_run else "green")
            mark = "XX" if error else ("--" if result.dry_run else "OK")
            target.print(f"  [{style}]{mark}[/{style}] {item.kind} {item.id}")
            target.print(f"       [dim]{summary}[/dim]")
            if error:
                target.print(f"       [bold]next:[/bold] {error}")
    else:
        target.print("\n[bold]Nothing to remove[/bold] with the selected scopes.")

    if plan.kept:
        target.print("\n[bold]Kept[/bold]")
        for item in plan.kept:
            target.print(f"  [dim]-[/dim] {item.kind} {item.id}")
            target.print(f"       [dim]{item.reason}[/dim]")

    if plan.manual:
        target.print("\n[bold]Left for you to remove by hand[/bold]")
        for item in plan.manual:
            target.print(f"  [dim]-[/dim] {item.kind} {item.id}")
            target.print(f"       [dim]{item.hint}[/dim]")

    for message in result.messages:
        target.print(f"[dim]note:[/dim] {message}")

    style = "bold green" if result.outcome is InstallOutcome.READY else "bold red"
    target.print(f"\n[{style}]{result.outcome.value}[/{style}] (exit {result.exit_code})")


@cli.command("uninstall")
@click.option("--remove-config", is_flag=True, help="Also remove AQ's configuration files.")
@click.option(
    "--remove-data",
    is_flag=True,
    help="Also remove AQ's data directory — vault, projects, memory and history.",
)
@click.option(
    "--remove-database",
    is_flag=True,
    help="Also drop the PostgreSQL database and role `aq install` created.",
)
@click.option(
    "--all",
    "everything",
    is_flag=True,
    help="Select every destructive scope above. Each is still confirmed separately.",
)
@click.option("--dry-run", is_flag=True, help="Print the plan and remove nothing.")
@click.option("--json", "as_json", is_flag=True, help="Print one machine-readable JSON object.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Answer every confirmation with yes.")
@click.option(
    "--interactive/--non-interactive",
    default=None,
    help="Confirm destructive scopes at a prompt (default), or require --yes.",
)
@click.option(
    "--state-file",
    type=click.Path(dir_okay=False, path_type=Path),
    help=f"Resume record path (default: {default_state_path()}).",
)
@click.pass_context
def uninstall(
    ctx: click.Context,
    remove_config: bool,
    remove_data: bool,
    remove_database: bool,
    everything: bool,
    dry_run: bool,
    as_json: bool,
    assume_yes: bool,
    interactive: bool | None,
    state_file: Path | None,
) -> None:
    """Remove the AQ runtime this machine's `aq install` set up.

    With no flags it stops the daemon it started, undoes the shell edit it
    made, and forgets its own records — your configuration, projects, vault and
    database are left exactly where they are.

    Each destructive scope is opt-in and confirmed on its own: `--remove-config`,
    `--remove-data`, `--remove-database`. Anything AQ installed but shares with
    the rest of the machine — a provider CLI, a Homebrew formula, a PostgreSQL
    server — is reported with the command to remove it by hand, never removed
    for you. `--dry-run` prints the whole plan and changes nothing.
    """
    if interactive is None:
        interactive = sys.stdin.isatty() and not as_json

    state_path = state_file or default_state_path()
    try:
        state = load_state(state_path)
    except StateError as error:
        raise UninstallInputError(str(error)) from error

    scopes = selected_scopes(
        remove_config=remove_config,
        remove_data=remove_data,
        remove_database=remove_database,
        everything=everything,
    )

    if state is None:
        # No record means nothing is known to be owned.  That is a finished
        # uninstall, not a failure: refusing here would send an operator
        # hunting for a file whose absence is the answer.
        result = UninstallResult(
            outcome=InstallOutcome.READY,
            dry_run=dry_run,
            plan=plan_uninstall(InstallState("", ""), scopes=scopes),
            messages=(
                (
                    f"no resume record at {state_path}: this machine has nothing "
                    "`aq install` recorded owning, so nothing was removed."
                ),
            ),
        )
        _emit(result, as_json=as_json)
        ctx.exit(result.exit_code)

    plan = plan_uninstall(state, scopes=scopes, state_path=state_path)
    messages: list[str] = []

    # Confirmation happens against the *plan*, so a scope with nothing owned in
    # it is never confirmed, and a declined scope is re-planned rather than
    # skipped at execution time — the printed plan is always what will run.
    if not dry_run and plan.destructive_scopes:
        if not interactive and not assume_yes:
            flags = " ".join(f"--remove-{scope.value}" for scope in plan.destructive_scopes)
            result = UninstallResult(
                outcome=InstallOutcome.NEEDS_USER,
                dry_run=False,
                plan=plan,
                messages=(
                    (
                        f"{flags} would destroy data and an unattended run cannot confirm "
                        "it. Rerun with --yes once you are sure, or drop the flag."
                    ),
                ),
            )
            _emit(result, as_json=as_json)
            ctx.exit(result.exit_code)
        if interactive and not assume_yes:
            declined = {
                scope
                for scope in plan.destructive_scopes
                if not click.confirm(_CONFIRM[scope], default=False, err=True)
            }
            if declined:
                messages.extend(
                    f"{scope.value} removal was declined at the prompt"
                    for scope in sorted(declined, key=lambda scope: scope.value)
                )
                scopes = frozenset(scopes - declined)
                plan = plan_uninstall(state, scopes=scopes, state_path=state_path)

    handlers = {}
    if not dry_run:
        admin = None
        remediation = ""
        if any(item.scope is RemovalScope.DATABASE for item in plan.removals):
            admin, remediation = _admin_executor(state, home=state_path.parent)
        handlers = default_handlers(
            home=state_path.parent,
            admin=admin,  # type: ignore[arg-type]
            admin_remediation=remediation,
        )

    result = execute_uninstall(plan, handlers, dry_run=dry_run, messages=messages)
    _emit(result, as_json=as_json)
    ctx.exit(result.exit_code)


def _emit(result: UninstallResult, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        render_result(result, console)


__all__ = ["render_result", "selected_scopes", "uninstall"]
