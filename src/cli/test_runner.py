"""``aq test`` — run pytest behind the box-wide test semaphore.

Layer 2 of resource gating (docs/guides/resource-gating.md).  Agents are
told to use this for anything heavier than a single file, because the thing
that takes the box down is not one agent's test run, it is eight of them
arriving at once.

What it does, in order:

1. Resolve the caps.  ``AQ_TEST_SLOTS`` / ``AQ_TEST_WORKERS`` from the
   session env win (the daemon derived them at launch and they are visible
   from inside a worktree); otherwise the ``resources:`` section of
   ``~/.agent-queue/config.yaml``; otherwise built-in defaults.  A worktree
   with no config still gets gating.
2. Take one of N ``flock`` slots, printing a "waiting" line every poll so a
   queued agent looks queued rather than hung.
3. Exec pytest with ``-n <cap>`` and the default marker deselects folded in
   — only when the caller did not pass their own, so an explicit
   ``-n 0`` / ``-m perf`` is always honoured.

Option names are all ``--aq-``-prefixed on purpose: everything else on the
command line is pytest's, and a wrapper that quietly ate ``-k`` or ``-x``
would be worse than no wrapper.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time

import click

from .app import cli, console

CONFIG_PATH = os.path.expanduser("~/.agent-queue/config.yaml")

#: Fallbacks when there is neither session env nor a readable config.
_FALLBACK_SLOTS = 2
_FALLBACK_WORKERS = 4
_FALLBACK_MARKERS = "not perf and not migration and not slow and not tmux and not integration"


def _load_config():
    """The application config, or ``None`` when unreadable.

    Deliberately forgiving: ``aq test`` runs in worktrees, in CI, and on
    boxes where the daemon has never started.  A missing or broken config
    means "use the fallbacks", never a traceback in front of a test run.
    """
    try:
        from src.config import load_config

        return load_config(CONFIG_PATH)
    except Exception:
        return None


def _env_int(key: str) -> int | None:
    raw = os.environ.get(key)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _caps(resources) -> tuple[int, int, str, float, int]:
    """``(slots, workers, markers, poll, timeout)`` from env → config → defaults."""
    slots = _env_int("AQ_TEST_SLOTS")
    workers = _env_int("AQ_TEST_WORKERS")
    markers = _FALLBACK_MARKERS
    poll = 2.0
    timeout = 1800
    if resources is not None:
        if slots is None:
            slots = max(1, int(resources.test_slots))
        if workers is None:
            workers = resources.test_worker_cap()
        markers = resources.test_deselect_markers
        poll = float(resources.test_poll_interval)
        timeout = int(resources.test_wait_timeout)
    return (
        slots or _FALLBACK_SLOTS,
        workers or _FALLBACK_WORKERS,
        markers,
        poll,
        timeout,
    )


def _has_flag(args: tuple[str, ...], *flags: str) -> bool:
    """True when the caller already passed one of *flags*.

    Matches both ``-n 4`` and ``-n4``/``--numprocesses=4`` spellings, since
    silently adding a second ``-n`` would make pytest error out on a
    perfectly reasonable command line.
    """
    for arg in args:
        for flag in flags:
            if arg == flag or arg.startswith(f"{flag}=") or (
                len(flag) == 2 and flag.startswith("-") and arg.startswith(flag)
            ):
                return True
    return False


def _xdist_disabled(args: tuple[str, ...]) -> bool:
    """True when the caller has explicitly turned xdist off.

    Only ``-p no:xdist`` counts.  An earlier version treated *any* ``-p`` as
    "the caller is managing plugins", which quietly dropped the worker cap
    from every ``aq test ... -p no:cacheprovider`` — the exact command shape
    agents use most.  Adding ``-n`` alongside an unrelated ``-p`` is
    perfectly valid pytest, so the test has to be this specific.
    """
    joined = " ".join(args)
    return "no:xdist" in joined


def _compose_pytest_argv(
    args: tuple[str, ...], *, workers: int, markers: str, apply_markers: bool
) -> list[str]:
    """The full pytest command line, with caps folded in where absent."""
    argv = [sys.executable, "-m", "pytest"]
    if not _has_flag(args, "-n", "--numprocesses") and not _xdist_disabled(args):
        argv.extend(["-n", str(workers)])
    if apply_markers and markers and not _has_flag(args, "-m"):
        argv.extend(["-m", markers])
    argv.extend(args)
    return argv


def _run_forwarding_signals(argv: list[str]) -> int:
    """Run *argv*, passing SIGINT/SIGTERM on to it, and return its code.

    Without the forwarding, a targeted ``SIGTERM`` (the daemon's stall
    ladder, or a human's ``kill``) would kill this wrapper, release the
    flock with the descriptor, and leave pytest running unsupervised — a
    runaway that the semaphore no longer knows about.  Terminal ``Ctrl-C``
    already reaches both through the process group; this covers everything
    that does not.
    """
    # SlotSemaphore deliberately marks its flock descriptor inheritable.
    # Preserve inheritable descriptors so a hard-killed wrapper cannot
    # release the slot while the pytest child continues running.
    proc = subprocess.Popen(argv, close_fds=False)
    previous: dict[int, object] = {}

    def _forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except (ProcessLookupError, OSError):  # pragma: no cover - already gone
            pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, _forward)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass
    try:
        return proc.wait()
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):  # pragma: no cover
                pass


def _render_status(snapshot: dict) -> None:
    from rich.table import Table

    table = Table(title=f"Test slots — {snapshot['free']}/{snapshot['total']} free")
    table.add_column("Slot", justify="right")
    table.add_column("State")
    table.add_column("Holder")
    table.add_column("Held for", justify="right")
    now = time.time()
    for row in snapshot["slots"]:
        holder = row.get("holder") or {}
        if not row["held"]:
            table.add_row(str(row["slot"]), "[green]free[/]", "-", "-")
            continue
        since = holder.get("since")
        held_for = f"{now - since:.0f}s" if isinstance(since, (int, float)) else "?"
        who = holder.get("task_id") or holder.get("cwd") or f"pid {holder.get('pid', '?')}"
        table.add_row(str(row["slot"]), "[yellow]busy[/]", str(who), held_for)
    console.print(table)
    for waiter in snapshot["waiting"]:
        since = waiter.get("since")
        waited = f"{now - since:.0f}s" if isinstance(since, (int, float)) else "?"
        who = waiter.get("task_id") or f"pid {waiter.get('pid', '?')}"
        console.print(f"[dim]waiting:[/] {who} ({waited})")


@cli.command(
    "test",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": ["--aq-help"],
    },
)
@click.option("--aq-status", is_flag=True, help="Show slot occupancy and exit.")
@click.option("--aq-no-wait", is_flag=True, help="Fail immediately when every slot is busy.")
@click.option("--aq-workers", type=int, default=None, help="Override the enforced -n cap.")
@click.option("--aq-timeout", type=int, default=None, help="Seconds to wait for a slot.")
@click.option(
    "--aq-all-markers",
    is_flag=True,
    help="Do not add the default marker deselects (perf/migration/slow/tmux/integration).",
)
@click.option("--aq-dry-run", is_flag=True, help="Print the pytest command and exit.")
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def test_command(
    ctx: click.Context,
    aq_status: bool,
    aq_no_wait: bool,
    aq_workers: int | None,
    aq_timeout: int | None,
    aq_all_markers: bool,
    aq_dry_run: bool,
    pytest_args: tuple[str, ...],
) -> None:
    """Run pytest under the box-wide test semaphore.

    \b
    aq test tests/test_pools.py            one file, still slot-gated
    aq test tests/ -k claim                a slice of the suite
    aq test --aq-status                    who is holding the slots
    aq test --aq-no-wait tests/            fail instead of queueing

    Everything that is not an ``--aq-*`` option is passed to pytest
    untouched.  ``-n`` and ``-m`` are added only when you did not supply
    them.  Use ``--aq-help`` for this help (``-h``/``--help`` belong to
    pytest).
    """
    from src.resources.semaphore import SlotSemaphore, SlotTimeout, default_lock_dir

    config = _load_config()
    resources = getattr(config, "resources", None)
    slots, workers, markers, poll, timeout = _caps(resources)
    if aq_workers is not None and aq_workers > 0:
        # An escape hatch for an operator running this by hand, clamped to
        # the machine: overriding the share is reasonable, asking for more
        # workers than there are cores never is.
        cores = resources.core_count() if resources is not None else (os.cpu_count() or 1)
        workers = min(aq_workers, cores)
        if workers < aq_workers:
            console.print(f"[yellow]aq test:[/] --aq-workers clamped to {cores} (cores)")
    if aq_timeout is not None:
        timeout = aq_timeout

    sem = SlotSemaphore(default_lock_dir(config), slots)

    if aq_status:
        _render_status(sem.snapshot())
        return

    if not pytest_args:
        console.print("[yellow]No pytest arguments given.[/] Try: aq test tests/test_config.py")
        console.print("[dim]Refusing to run the whole suite implicitly — see --aq-help.[/]")
        ctx.exit(2)

    argv = _compose_pytest_argv(
        pytest_args,
        workers=workers,
        markers=markers,
        apply_markers=not aq_all_markers,
    )

    if aq_dry_run:
        # click.echo, not console.print: a pytest command line is full of
        # brackets ("-k [case]") that Rich would eat as markup, and the
        # point of --aq-dry-run is a line you can paste.
        click.echo(shlex.join(argv))
        return

    meta = {
        "pid": os.getpid(),
        "task_id": os.environ.get("AQ_TASK_ID"),
        "session": os.environ.get("AQ_SESSION_NAME"),
        "cwd": os.getcwd(),
        "command": shlex.join(argv),
    }

    def _on_wait(waited: float, snapshot: dict) -> None:
        # Printed every poll on purpose: the daemon reads terminal silence
        # as a stall, and an agent queued behind a busy box must be visibly
        # queued rather than looking hung.
        holders = [
            (row.get("holder") or {}).get("task_id") or "?"
            for row in snapshot["slots"]
            if row["held"]
        ]
        console.print(
            f"[dim]aq test: waiting {waited:.0f}s for 1 of {slots} test slot(s); "
            f"held by {', '.join(holders) or 'unknown'}[/]"
        )

    try:
        with sem.acquire(
            timeout=0 if aq_no_wait else timeout,
            poll=poll,
            meta=meta,
            on_wait=_on_wait,
        ) as slot:
            console.print(f"[dim]aq test: slot {slot} of {slots}, -n {workers}[/]")
            click.echo(f"$ {shlex.join(argv)}", err=True)
            returncode = _run_forwarding_signals(argv)
        ctx.exit(returncode)
    except SlotTimeout as exc:
        console.print(f"[red]aq test:[/] {exc}")
        console.print("[dim]Run `aq test --aq-status` to see who is holding them.[/]")
        ctx.exit(75)  # EX_TEMPFAIL — retryable, not a test failure
