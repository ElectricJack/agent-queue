"""``aq install`` — the common installer interface.

This command runs before a daemon exists, so unlike almost every other ``aq``
command it does not talk to the REST API: it builds a step registry, runs
:class:`src.install.engine.InstallEngine` in-process and prints the result.

The published contract — flags, JSON payload and exit codes — is
``docs/reference/cli/install.md``; the design it implements is
``docs/plans/install-onboarding/contract.md``.  Everything the command knows
about *what* to install comes from the registry, so a platform or provider
adapter is added by registering steps, not by editing this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from src.install import (
    InstallEngine,
    InstallOptions,
    InstallOutcome,
    InstallResult,
    PlanAction,
    ProgressEvent,
    StepRegistry,
    StepSpec,
    StepState,
    build_registry,
    default_state_path,
    describe_host,
    exit_code,
)
from src.install.results import RESULT_SCHEMA_VERSION

from .app import cli, console

#: Recognised keys in the ``--config`` install input file.  An unknown key is
#: a usage error rather than a silent no-op: an unattended run that quietly
#: ignored a misspelled ``capabilities:`` would install the wrong thing.
_CONFIG_KEYS = frozenset({"version", "capabilities", "approve", "settings"})

_STATE_STYLE = {
    StepState.SUCCEEDED: ("green", "OK"),
    StepState.SKIPPED: ("dim", "--"),
    StepState.NEEDS_USER: ("yellow", "!!"),
    StepState.FAILED: ("bold red", "XX"),
}

_OUTCOME_STYLE = {
    InstallOutcome.READY: "bold green",
    InstallOutcome.NEEDS_USER: "bold yellow",
    InstallOutcome.INVALID_INPUT: "bold red",
    InstallOutcome.UNSUPPORTED_HOST: "bold red",
    InstallOutcome.FAILED: "bold red",
}


class InstallInputError(click.ClickException):
    """A bad flag or input file — reported as ``invalid_input``, not a crash."""

    exit_code = exit_code(InstallOutcome.INVALID_INPUT)


def installer_version() -> str:
    """The version of the installer that is running."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("agent-queue")
    except PackageNotFoundError:  # pragma: no cover - source checkout without metadata
        from . import __version__

        return __version__
    except Exception:  # noqa: BLE001 - pragma: no cover; version is never fatal
        return "unknown"


def load_install_input(path: Path) -> dict[str, Any]:
    """Read and validate the unattended install input file.

    YAML or JSON — a caller that generates the file from a script should not
    have to depend on a YAML writer.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InstallInputError(f"cannot read install input {path}: {error}") from error
    try:
        import yaml

        payload = yaml.safe_load(text)
    except Exception as error:
        raise InstallInputError(f"install input {path} is not valid YAML/JSON: {error}") from error
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InstallInputError(f"install input {path} must be a mapping at the top level")
    unknown = sorted(set(payload) - _CONFIG_KEYS)
    if unknown:
        raise InstallInputError(
            f"install input {path} has unknown key(s): {', '.join(unknown)} "
            f"(recognised: {', '.join(sorted(_CONFIG_KEYS))})"
        )
    version = payload.get("version", 1)
    if version not in (1, "1"):
        raise InstallInputError(
            f"install input {path} declares version {version}; this installer reads version 1"
        )
    for key in ("capabilities", "approve"):
        value = payload.get(key)
        if value is not None and not isinstance(value, list):
            raise InstallInputError(f"install input {path}: '{key}' must be a list")
    settings = payload.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise InstallInputError(f"install input {path}: 'settings' must be a mapping")
    return payload


def build_options(
    registry: StepRegistry,
    *,
    interactive: bool,
    dry_run: bool,
    resume: bool,
    fresh: bool,
    restart_from: str | None,
    capabilities: tuple[str, ...],
    approve: tuple[str, ...],
    assume_yes: bool,
    config_path: Path | None,
    state_path: Path | None,
    target_version: str | None = None,
) -> InstallOptions:
    """Fold flags and the optional input file into one validated options value.

    Flags win over the file: a script that passes both is asking for an
    override, and silently preferring the file would make the override
    invisible.
    """
    payload = load_install_input(config_path) if config_path else {}
    selected = list(payload.get("capabilities") or []) + list(capabilities)
    approved = list(payload.get("approve") or []) + list(approve)
    if assume_yes:
        approved.append("*")

    known = set(registry.capabilities())
    unknown_caps = sorted({str(name) for name in selected} - known)
    if unknown_caps:
        raise InstallInputError(
            f"unknown capability: {', '.join(unknown_caps)}"
            + (f" (available: {', '.join(sorted(known))})" if known else "")
        )
    unknown_steps = sorted({str(name) for name in approved if name != "*"} - set(registry.ids()))
    if unknown_steps:
        raise InstallInputError(f"--approve names unknown step(s): {', '.join(unknown_steps)}")
    if restart_from and restart_from not in registry:
        raise InstallInputError(
            f"--restart-from names an unknown step: {restart_from} "
            f"(known steps: {', '.join(registry.ids())})"
        )
    if fresh and restart_from:
        raise InstallInputError("--fresh and --restart-from are mutually exclusive")

    version = installer_version()
    return InstallOptions(
        installer_version=version,
        target_version=target_version or version,
        interactive=interactive,
        dry_run=dry_run,
        resume=resume and not fresh,
        fresh=fresh,
        restart_from=restart_from,
        capabilities=frozenset(str(name) for name in selected),
        approve=frozenset(str(name) for name in approved),
        settings=dict(payload.get("settings") or {}),
        state_path=state_path,
    )


def _consent() -> Any:
    def ask(step: StepSpec) -> bool:
        # Prompts go to stderr: a caller that combines --interactive with
        # --json is parsing stdout, and a consent question printed there would
        # break ``json.loads`` on the first line.
        return click.confirm(step.consent_prompt or f"{step.title}?", default=True, err=True)

    return ask


def _progress(target: Console) -> Any:
    def report(event: ProgressEvent) -> None:
        if event.phase == "start":
            if event.action is PlanAction.WOULD_RUN:
                target.print(f"[dim][{event.index}/{event.total}][/dim] would run {event.title}")
            return
        style, mark = _STATE_STYLE.get(event.state or StepState.SKIPPED, ("dim", "--"))
        target.print(
            f"[dim][{event.index}/{event.total}][/dim] [{style}]{mark}[/{style}] {event.title}"
        )

    return report


def render_result(result: InstallResult, target: Console) -> None:
    """Print the human view of a run: what happened, then what to do next."""
    if result.dry_run:
        target.print("\n[bold]Plan[/bold] (dry run — nothing was changed)")
        for row in result.plan:
            target.print(
                f"  [dim]{row.action.value:<18}[/dim] {row.step_id}  [dim]{row.reason}[/dim]"
            )

    for step in result.steps:
        if step.state is StepState.SUCCEEDED:
            continue
        style, mark = _STATE_STYLE[step.state]
        target.print(f"\n[{style}]{mark} {step.step_id}[/{style}]: {step.summary}")
        if step.remediation:
            target.print(f"   [bold]next:[/bold] {step.remediation}")

    for message in result.messages:
        target.print(f"[dim]note:[/dim] {message}")

    style = _OUTCOME_STYLE[result.outcome]
    target.print(
        f"\n[{style}]{result.outcome.value}[/{style}] "
        f"(exit {result.exit_code}) — host {result.platform.get('host_path', 'unknown')}"
    )
    if result.state_path:
        target.print(f"[dim]resume record: {result.state_path}[/dim]")
    if result.next_action and not result.blocking_step:
        target.print(f"[bold]next:[/bold] {result.next_action}")


@cli.command("install")
@click.option(
    "--interactive/--non-interactive",
    default=None,
    help="Prompt for consent (default), or run unattended and never read a terminal.",
)
@click.option("--dry-run", is_flag=True, help="Report the plan and run only read-only checks.")
@click.option("--json", "as_json", is_flag=True, help="Print one machine-readable JSON object.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Unattended install input file (YAML or JSON).",
)
@click.option(
    "--with",
    "capabilities",
    multiple=True,
    help="Select an optional capability (repeatable).",
)
@click.option(
    "--approve",
    "approve",
    multiple=True,
    help="Approve one mutating step by id in an unattended run (repeatable).",
)
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Approve every mutating step.")
@click.option(
    "--resume/--no-resume",
    default=True,
    help="Continue from the existing resume record (default), or ignore it for this run.",
)
@click.option("--fresh", is_flag=True, help="Start a new resume record, leaving the old one.")
@click.option("--restart-from", metavar="STEP", help="Redo STEP and the steps that depend on it.")
@click.option(
    "--state-file",
    type=click.Path(dir_okay=False, path_type=Path),
    help=f"Resume record path (default: {default_state_path()}).",
)
@click.option("--list-steps", is_flag=True, help="Print the registered steps and exit.")
@click.pass_context
def install(
    ctx: click.Context,
    interactive: bool | None,
    dry_run: bool,
    as_json: bool,
    config_path: Path | None,
    capabilities: tuple[str, ...],
    approve: tuple[str, ...],
    assume_yes: bool,
    resume: bool,
    fresh: bool,
    restart_from: str | None,
    state_file: Path | None,
    list_steps: bool,
) -> None:
    """Install or repair this machine's AQ prerequisites.

    Reruns are the normal recovery path: the command records what it completed
    and what it owns, revalidates a completed step instead of repeating it, and
    stops at the first step that needs attention.  Exit codes: 0 ready,
    10 needs_user, 11 invalid_input, 12 unsupported_host, 20 failed.
    """
    support = describe_host()
    registry = build_registry(support)
    if list_steps:
        _emit_steps(registry, as_json=as_json)
        return

    if interactive is None:
        interactive = sys.stdin.isatty() and not as_json

    options = build_options(
        registry,
        interactive=interactive,
        dry_run=dry_run,
        resume=resume,
        fresh=fresh,
        restart_from=restart_from,
        capabilities=capabilities,
        approve=approve,
        assume_yes=assume_yes,
        config_path=config_path,
        state_path=state_file,
    )

    quiet = as_json
    engine = InstallEngine(
        registry,
        options,
        support=support,
        consent=_consent() if interactive else None,
        progress=None if quiet else _progress(console),
    )
    result = engine.run()

    if as_json:
        click.echo(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        render_result(result, console)
    ctx.exit(result.exit_code)


def _emit_steps(registry: StepRegistry, *, as_json: bool) -> None:
    rows = registry.describe()
    if as_json:
        click.echo(
            json.dumps(
                {"schema_version": RESULT_SCHEMA_VERSION, "steps": rows},
                ensure_ascii=False,
            )
        )
        return
    for row in rows:
        flags = []
        if row["mutating"]:
            flags.append("mutating")
        if row["capability"]:
            flags.append(f"capability={row['capability']}")
        suffix = f" [dim]({', '.join(flags)})[/dim]" if flags else ""
        console.print(f"[bold]{row['id']}[/bold] — {row['title']}{suffix}")
        if row["depends_on"]:
            console.print(f"   [dim]after: {', '.join(row['depends_on'])}[/dim]")
