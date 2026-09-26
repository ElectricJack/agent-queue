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
2. Preflight the required ``POSTGRES_TEST_DSN`` and every path-shaped
   argument. Refuse before taking a slot when configuration is absent or a
   path does not exist, rather than producing hundreds of fixture errors or
   xdist's misleading "no tests ran".
3. Take shared box admission and one of N ``flock`` slots, printing a "waiting" line so a
   queued agent looks queued rather than hung.  A run that selects the
   whole suite first takes the box-wide full-suite lock (capacity one), so
   at most one slot is ever spent on the whole suite: three agents each
   running it for an hour once starved the publisher's focused validation.
4. Exec pytest with a fresh database-ownership token, ``-n <cap> --dist
   loadfile``, and the default marker
   deselects folded in — only when the caller did not pass their own, so
   an explicit ``-n 0`` / ``-p no:xdist`` / ``-m perf`` is always honoured.
5. Treat pytest's exit code 5 ("no tests collected") as the failure it is,
   with a line saying so, rather than letting a run that executed nothing
   pass for a green one.

Option names are all ``--aq-``-prefixed on purpose: everything else on the
command line is pytest's, and a wrapper that quietly ate ``-k`` or ``-x``
would be worse than no wrapper.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import click

from src.test_selection.discovery import SKIP_DIRS
from src.test_selection.discovery import pytest_rootdir as _pytest_rootdir
from src.test_selection.discovery import test_modules as _test_modules

from .app import _get_client, _run, cli, console
from .exceptions import CommandError, DaemonNotRunningError

CONFIG_PATH = os.path.expanduser("~/.agent-queue/config.yaml")

#: Fallbacks when there is neither session env nor a readable config.
_FALLBACK_SLOTS = 2
_FALLBACK_WORKERS = 4
_FALLBACK_MARKERS = "not perf and not migration and not slow and not tmux and not integration"

_POSTGRES_SETUP = """POSTGRES_TEST_DSN is not set; PostgreSQL is required by this test suite.
Nothing was run. Start the separate disposable test service and set a base DSN:

  docker compose up -d postgres-test
  export POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres

The test harness creates uniquely named databases under that server and removes only the
databases it owns. Do not point POSTGRES_TEST_DSN at the daemon's :5533 server."""


def postgres_test_dsn_error(environ: Mapping[str, str] | None = None) -> str | None:
    """Actionable setup error when the required PostgreSQL test DSN is absent."""
    values = os.environ if environ is None else environ
    return None if values.get("POSTGRES_TEST_DSN", "").strip() else _POSTGRES_SETUP


def _new_test_run_id() -> str:
    """Opaque ownership token inherited by every pytest-xdist child."""
    return secrets.token_hex(8)


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


def _slot_wait_timeout(configured: int, flag: int | None) -> int:
    """Seconds to wait for a slot: ``--aq-timeout``, else the env, else config.

    ``AQ_TEST_WAIT_TIMEOUT`` is how a supervising caller — the development
    publisher running its selected validation — bounds queueing separately
    from the run it times.  ``0`` means "try once", as for the flag.
    """
    if flag is not None:
        return flag
    from src.resources.slot_report import WAIT_TIMEOUT_ENV

    raw = os.environ.get(WAIT_TIMEOUT_ENV)
    try:
        value = int(raw) if raw else None
    except ValueError:
        value = None
    return value if value is not None and value >= 0 else configured


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
            if (
                arg == flag
                or arg.startswith(f"{flag}=")
                or (len(flag) == 2 and flag.startswith("-") and arg.startswith(flag))
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


#: pytest/xdist options whose value is the *next* argv entry.  Anything not
#: listed here is assumed to be a flag, which is the safe direction: the
#: worst case is that a value we failed to recognise gets path-checked, and
#: the path check only ever fires on arguments that look like paths.
_VALUE_OPTIONS = frozenset(
    {
        "-c",
        "-k",
        "-m",
        "-n",
        "-o",
        "-p",
        "-r",
        "-W",
        "--basetemp",
        "--capture",
        "--color",
        "--confcutdir",
        "--deselect",
        "--dist",
        "--durations",
        "--ignore",
        "--ignore-glob",
        "--import-mode",
        "--junit-xml",
        "--junitxml",
        "--keyword",
        "--log-file",
        "--max-worker-restart",
        "--maxfail",
        "--markexpr",
        "--numprocesses",
        "--override-ini",
        "--rootdir",
        "--tb",
        "--tx",
    }
)


def _positional_args(args: tuple[str, ...]) -> list[str]:
    """The arguments pytest will read as test paths.

    Options and the values that follow them are skipped; everything after a
    bare ``--`` is positional by definition.
    """
    positionals: list[str] = []
    skip_next = False
    rest_are_positional = False
    for arg in args:
        if rest_are_positional:
            positionals.append(arg)
            continue
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            rest_are_positional = True
            continue
        if arg.startswith("-"):
            if arg in _VALUE_OPTIONS:
                skip_next = True
            continue
        positionals.append(arg)
    return positionals


def _looks_like_a_path(arg: str) -> bool:
    """True for arguments that are unambiguously file paths.

    Deliberately narrow.  A missed path (``aq test tests -k foo``) merely
    leaves the old behaviour in place; a false positive would refuse a
    perfectly valid command line, which is how wrappers get worked around.
    """
    head = arg.split("::", 1)[0]
    return bool(head) and (os.sep in head or head.endswith(".py"))


def _missing_paths(args: tuple[str, ...]) -> list[str]:
    """Path-shaped arguments that do not exist on disk.

    The ``::node-id`` suffix is stripped before the stat: only the file part
    of ``tests/test_x.py::TestY::test_z`` is a path.
    """
    missing = []
    for arg in _positional_args(args):
        if not _looks_like_a_path(arg):
            continue
        if not os.path.exists(arg.split("::", 1)[0]):
            missing.append(arg)
    return missing


#: A path selection naming at least this share of the tree's test modules is
#: the full suite in all but name: ``tests/test_*.py`` expanded by the shell
#: leaves out only the subpackages.
_FULL_SUITE_SHARE = 0.5

#: Collection only: nothing runs, whatever is selected.
_COLLECT_ONLY_FLAGS = frozenset({"--co", "--collect-only", "--collectonly"})
_LAST_FAILED_FLAGS = frozenset({"--lf", "--last-failed"})

#: Directories never worth walking for test modules.
_SKIP_DIRS = SKIP_DIRS


def _flags(args: tuple[str, ...]) -> list[str]:
    """Option-shaped arguments before a bare ``--``."""
    flags = []
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("-"):
            flags.append(arg)
    return flags


def _option_value(args: tuple[str, ...], flag: str) -> str | None:
    """The value of the last short *flag* (``-k expr``, ``-kexpr``, ``-k=expr``).

    Last wins, as it does for pytest.  ``None`` when the flag is absent.
    """
    value: str | None = None
    skip_next = False
    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            break
        if arg == flag:
            value = args[index + 1] if index + 1 < len(args) else None
            skip_next = True
        elif arg.startswith(flag):
            value = arg[len(flag) :].removeprefix("=")
        elif arg in _VALUE_OPTIONS:
            skip_next = True
    return value


def _expression_is_broad(expression: str) -> bool:
    """True when a ``-k``/``-m`` expression keeps what it does not name.

    pytest's own grammar decides: evaluate the expression for an item that
    matches none of its words.  ``not slow`` keeps such an item (the whole
    suite minus a slice), ``claim or pools`` drops it (a slice).  An empty
    expression is pytest's "no filter".  One pytest cannot parse fails before
    anything runs, so it is never a full-suite run.
    """
    if not expression.strip():
        return True
    try:
        from _pytest.mark import expression as grammar
    except ImportError:  # pragma: no cover - pytest is what this wraps
        return False
    # pytest 9 raises SyntaxError; earlier releases had their own ParseError.
    unparseable = (SyntaxError, getattr(grammar, "ParseError", SyntaxError), TypeError)
    try:
        return bool(grammar.Expression.compile(expression).evaluate(lambda _name, **_kw: False))
    except unparseable:
        return False


def _last_failed_is_a_slice(rootdir: Path) -> bool:
    """``--lf`` reruns only the recorded failures, when there are any.

    With nothing recorded, pytest's default ``--last-failed-no-failures all``
    runs everything.
    """
    record = rootdir / ".pytest_cache" / "v" / "cache" / "lastfailed"
    try:
        return bool(json.loads(record.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


def _is_full_suite(args: tuple[str, ...], *, cwd: Path | None = None) -> bool:
    """True when *args* select the whole suite rather than a slice of it.

    Full: no path at all (pytest collects ``testpaths``), ``tests/``, ``.``,
    or a list of files covering at least :data:`_FULL_SUITE_SHARE` of the
    tree's test modules.  A narrowing ``-k``/``-m`` expression, collect-only,
    or ``--lf`` with failures on record makes any of those a slice.  A
    ``::node-id`` names part of one module and never counts toward coverage.

    Deliberately static: the point is to classify before taking a lock, and
    collecting 14,000 tests to find out would itself be the heavy run.
    """
    flags = _flags(args)
    if any(flag in _COLLECT_ONLY_FLAGS for flag in flags):
        return False
    for option in ("-k", "-m"):
        expression = _option_value(args, option)
        if expression is not None and not _expression_is_broad(expression):
            return False
    cwd = (cwd or Path.cwd()).resolve()
    rootdir, testpaths = _pytest_rootdir(cwd)
    if any(flag in _LAST_FAILED_FLAGS for flag in flags) and _last_failed_is_a_slice(rootdir):
        return False
    positionals = _positional_args(args)
    if positionals:
        targets = [(cwd / arg).resolve() for arg in positionals if "::" not in arg]
    else:
        # pytest reads ``testpaths`` only when run from the rootdir; anywhere
        # else a bare invocation collects the current directory.
        targets = testpaths if cwd == rootdir and testpaths else [cwd]
    if not targets:
        return False
    if not testpaths:
        # No configured tree to measure against: only a selection of the
        # whole rootdir (which a bare run from it is) counts.
        return any(t == rootdir or t in rootdir.parents for t in targets)
    tree = _test_modules(testpaths)
    if not tree:
        return False
    selected = sum(1 for m in tree if any(m == t or t in m.parents for t in targets))
    return selected >= _FULL_SUITE_SHARE * len(tree)


def _compose_pytest_argv(
    args: tuple[str, ...], *, workers: int, markers: str, apply_markers: bool
) -> list[str]:
    """The full pytest command line, with caps folded in where absent."""
    argv = [sys.executable, "-m", "pytest"]
    if not _xdist_disabled(args):
        if not _has_flag(args, "-n", "--numprocesses"):
            argv.extend(["-n", str(workers)])
        # Keep a module's tests on one worker (its database fixtures are
        # per-module).  This cannot live in pyproject's ``addopts``:
        # ``-p no:xdist`` unloads the option with the plugin.
        if not _has_flag(args, "--dist"):
            argv.extend(["--dist", "loadfile"])
    if not _has_flag(args, "-m"):
        if apply_markers and markers:
            argv.extend(["-m", markers])
        elif not apply_markers:
            # pyproject.toml carries the same default deselection so bare
            # pytest remains safe. An empty command-line expression is the
            # only way ``--aq-all-markers`` can override that configured
            # value; omitting ``-m`` merely lets the config take effect.
            argv.extend(["-m", ""])
    argv.extend(args)
    return argv


def _run_forwarding_signals(argv: list[str], *, env: dict[str, str] | None = None) -> int:
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
    proc = subprocess.Popen(argv, close_fds=False, env=env)
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


def _elapsed(seconds: float) -> str:
    """``45s`` / ``12m05s`` / ``1h02m`` — a full-suite hold is measured in hours."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _full_suite_holder(snapshot: dict, now: float | None = None) -> str | None:
    """Who holds the full-suite lock and for how long, or ``None`` when it is free."""
    row = snapshot["slots"][0]
    if not row["held"]:
        return None
    holder = row.get("holder") or {}
    since = holder.get("since")
    held_for = _elapsed((now or time.time()) - since) if isinstance(since, (int, float)) else "?"
    who = f"task {holder['task_id']}" if holder.get("task_id") else f"pid {holder.get('pid', '?')}"
    return f"{who}, cwd {holder.get('cwd') or '?'}, running {held_for}"


_VERDICT_STYLE = {"live": "green", "orphaned": "red", "unattributed": "yellow"}


def _holder_verdicts(lock_dir) -> dict[int, str]:
    """``{slot: live|orphaned|unattributed}``; empty when attribution fails.

    Status is a read a human runs while the box is in trouble, so a
    ``/proc`` surprise here degrades to the plain table, never a traceback.
    """
    try:
        from src.resources.test_runs import held_slots

        return {held.slot: held.state for held in held_slots(lock_dir)}
    except Exception:  # noqa: BLE001 - status must render even when /proc surprises us
        return {}


def _render_status(
    snapshot: dict,
    full_suite: dict,
    verdicts: Mapping[int, str] | None = None,
    protocol: dict | None = None,
) -> None:
    from rich.markup import escape
    from rich.table import Table

    verdicts = verdicts or {}
    table = Table(title=f"Test slots — {snapshot['free']}/{snapshot['total']} free")
    table.add_column("Slot", justify="right")
    table.add_column("State")
    table.add_column("Holder")
    table.add_column("Session")
    table.add_column("Held for", justify="right")
    now = time.time()
    for row in snapshot["slots"]:
        holder = row.get("holder") or {}
        if not row["held"]:
            table.add_row(str(row["slot"]), "[green]free[/]", "-", "-", "-")
            continue
        since = holder.get("since")
        held_for = f"{now - since:.0f}s" if isinstance(since, (int, float)) else "?"
        who = holder.get("task_id") or holder.get("cwd") or f"pid {holder.get('pid', '?')}"
        state = (
            "[yellow]busy[/] (full suite)" if holder.get("scope") == "full" else "[yellow]busy[/]"
        )
        verdict = verdicts.get(row["slot"])
        session = f"[{_VERDICT_STYLE.get(verdict, 'dim')}]{verdict}[/]" if verdict else "?"
        table.add_row(str(row["slot"]), state, escape(str(who)), session, held_for)
    console.print(table)
    if protocol is not None:
        box = protocol["box"]
        console.print(
            f"Box-lock protocol v{protocol['version']}: {box['mode'] if box['held'] else 'free'}"
        )
        for row in protocol["incompatible_slots"]:
            console.print(
                f"[yellow]Slot {row['slot']}: incompatible slot-only client.[/] "
                "Upgrade local entry points and drain old runs before exclusive work."
            )
    if "orphaned" in verdicts.values():
        console.print(
            "[red]A slot is held by a run whose session is gone.[/] "
            "[dim]Preview with `aq test --aq-reap-orphans`; add --aq-apply to free it.[/]"
        )
    for waiter in snapshot["waiting"]:
        since = waiter.get("since")
        waited = f"{now - since:.0f}s" if isinstance(since, (int, float)) else "?"
        who = waiter.get("task_id") or f"pid {waiter.get('pid', '?')}"
        console.print(f"[dim]waiting:[/] {escape(str(who))} ({waited})")
    holder = _full_suite_holder(full_suite, now)
    if holder is None:
        console.print("Full-suite lock: [green]free[/] (one full-suite run at a time)")
    else:
        console.print(f"Full-suite lock: [yellow]held[/] by {escape(holder)}")
    for waiter in full_suite["waiting"]:
        since = waiter.get("since")
        waited = _elapsed(now - since) if isinstance(since, (int, float)) else "?"
        who = waiter.get("task_id") or f"pid {waiter.get('pid', '?')}"
        console.print(f"[dim]waiting for the full-suite lock:[/] {escape(str(who))} ({waited})")


def _holder_meta(test_run_id: str) -> dict:
    """The slot record: who holds it, attributable to a session and task.

    A pool worker's environment carries no ``AQ_TASK_ID``, so a record of
    the environment alone named nobody — the 2026-09-24 orphans showed up
    as bare worktree paths.  Attribution must never cost a test run, so any
    failure falls back to the plain record.
    """
    try:
        from src.resources.test_runs import holder_identity

        return holder_identity(test_run_id=test_run_id)
    except Exception:  # noqa: BLE001 - attribution must never cost a test run
        return {
            "pid": os.getpid(),
            "task_id": os.environ.get("AQ_TASK_ID"),
            "session": os.environ.get("AQ_SESSION_NAME"),
            "session_id": os.environ.get("AQ_SESSION_ID"),
            "test_run_id": test_run_id,
            "cwd": os.getcwd(),
        }


def _reap_orphans(lock_dir, *, apply: bool) -> int:
    """``--aq-reap-orphans``: report, and with *apply* terminate, orphaned runs.

    Exit 0 when nothing is orphaned or every orphan was freed, 1 when a
    reap did not free its slot.  A live session's run is never touched —
    stop the session instead.
    """
    from src.resources.test_runs import reap_orphans

    report = reap_orphans(lock_dir, apply=apply)
    for row in report["held"]:
        if row["state"] == "orphaned":
            continue
        console.print(
            f"[dim]slot {row['slot']}: {row['owner']} — {row['state']} ({row['reason']})[/]"
        )
    if not report["orphaned"]:
        console.print("[green]aq test:[/] no orphaned test runs.")
        return 0
    if not apply:
        for row in report["orphaned"]:
            console.print(
                f"[red]slot {row['slot']}[/]: {row['owner']} — orphaned ({row['reason']}), "
                f"pid {row['pid']}, held {row['held_for_s']}s"
            )
        console.print(
            "[dim]Dry run. Re-run with --aq-apply to terminate these and free the slots.[/]"
        )
        return 0
    failed = 0
    for row in report["results"]:
        pids = ", ".join(str(pid) for pid in row["pids"]) or "none"
        style = "green" if row["freed"] else "red"
        failed += 0 if row["freed"] else 1
        console.print(
            f"[{style}]slot {row['slot']}[/]: {row['owner']} — {row['action']} "
            f"(signalled: {pids}; {row['reason']})"
        )
    return 1 if failed else 0


NARROWING_VALUE_FLAGS = ("-k", "-m", "--keyword", "--markexpr")
NARROWING_FLAGS = frozenset({"--lf", "--last-failed", "--deselect"})


def _narrowing_flags(args: tuple[str, ...]) -> list[str]:
    positionals = set(_positional_args(args))
    narrowing = []
    skip_next = False
    after_separator = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            after_separator = True
            continue
        if not after_separator and arg.startswith("-"):
            if any(
                arg == flag
                or arg.startswith(f"{flag}=")
                or (len(flag) == 2 and arg.startswith(flag))
                for flag in (*NARROWING_VALUE_FLAGS, *NARROWING_FLAGS)
            ):
                narrowing.append(arg)
            skip_next = arg in _VALUE_OPTIONS
        elif arg in positionals and "::" in arg:
            narrowing.append(arg)
    return narrowing


@dataclass(frozen=True)
class SmartPlan:
    selection_id: str | None
    mode: str
    recorded: bool
    result: dict

    @property
    def full_required(self) -> bool:
        return bool(self.result.get("full_required"))

    @property
    def full_suite_authorized(self) -> bool:
        return self.result.get("full_suite_authorized") is True

    @property
    def ordered(self) -> list[str]:
        return list(self.result.get("ordered", []))


def _smart_identity(cwd: str) -> dict:
    from src.claim_file import read_claim_file

    claim = read_claim_file(cwd) or {}
    return {key: claim[key] for key in ("task_id", "claim_epoch") if key in claim}


def _selection_execute(command: str, args: dict) -> dict:
    ctx = click.get_current_context(silent=True)
    api_url = (ctx.obj or {}).get("api_url") if ctx else None

    async def execute():
        async with _get_client(api_url) as client:
            result = await client.execute(command, args)
        if not result.get("success", False):
            raise CommandError(command, result.get("error", "selection request failed"), result)
        return result

    return _run(execute())


def _offline_base(cwd: str) -> str:
    from src.git.manager import GitError, GitManager

    try:
        result = _run(
            GitManager().arun_git_result(
                ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=cwd
            )
        )
        if result.returncode == 0:
            return result.stdout.removeprefix("refs/remotes/").strip()
    except GitError:
        pass
    # No fetch: an absent local base becomes incomplete snapshot evidence.
    return "origin/main"


def _smart_error(ctx: click.Context, exc: CommandError) -> None:
    code = exc.details.get("error_code") or exc.details.get("result")
    click.echo(f"aq test: {code + ' — ' if code else ''}{exc.detail_message}", err=True)
    ctx.exit(4 if code in {"selection_stale", "full_suite_required", "empty_selection"} else 2)


def _smart_plan(
    ctx, *, mode, base, plan_only, jev, project, pytest_args, marker_policy
) -> SmartPlan:
    cwd = os.getcwd()
    identity = _smart_identity(cwd)
    args = {
        **identity,
        "mode": "plan_only" if plan_only else mode,
        "targets": _positional_args(pytest_args),
        "jev": jev,
        "marker_policy": marker_policy,
        "narrowing_flags": _narrowing_flags(pytest_args),
    }
    if base is not None:
        args["base_ref"] = base
    project_id = project or os.environ.get("AQ_PROJECT_ID")
    if project_id:
        args["project_id"] = project_id
    if not identity and not os.environ.get("AQ_SESSION_ID"):
        args["workspace"] = cwd
    try:
        result = _selection_execute("test_select", args)
    except DaemonNotRunningError as exc:
        if plan_only:
            from src.test_selection.service import select_offline

            result = select_offline(
                cwd,
                base_ref=base or _offline_base(cwd),
                targets=args["targets"],
                marker_policy=marker_policy,
            )
            return SmartPlan(None, mode, False, {**result, "recorded": False})
        if mode == "shadow":
            click.echo(
                "aq test: selection not recorded (daemon unreachable); "
                "running the explicit targets unchanged"
            )
            return SmartPlan(None, mode, False, {"recorded": False})
        click.echo(f"aq test: daemon_unreachable — {exc}", err=True)
        ctx.exit(3)
    except CommandError as exc:
        _smart_error(ctx, exc)
    return SmartPlan(result.get("selection_id"), mode, bool(result.get("recorded")), result)


def _smart_recheck(ctx: click.Context, plan: SmartPlan) -> None:
    if not plan.recorded or not plan.selection_id:
        return
    try:
        result = _selection_execute("test_selection_recheck", {"selection_id": plan.selection_id})
    except DaemonNotRunningError as exc:
        click.echo(f"aq test: daemon_unreachable — {exc}", err=True)
        ctx.exit(3)
    except CommandError as exc:
        _smart_error(ctx, exc)
    if result.get("stale") is not False:
        click.echo("aq test: selection_stale — the tree changed; select again", err=True)
        ctx.exit(4)


def _smart_observe(plan: SmartPlan, *, exit_code, duration_ms, executed) -> None:
    if not plan.recorded or not plan.selection_id:
        return
    try:
        _selection_execute(
            "test_selection_observe",
            {
                "selection_id": plan.selection_id,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "executed_modules": executed,
            },
        )
    except Exception:
        # Reporting must not turn a real pytest failure into a different result.
        click.echo("aq test: execution observation not recorded", err=True)


class _TestCommand(click.Command):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # Click's optional value consumes a following path. A bare --aq-smart
        # is shadow unless the caller supplies one of the two explicit modes.
        normalized = list(args)
        for index, arg in enumerate(args):
            if arg == "--":
                break
            if arg == "--aq-smart" and (
                index + 1 == len(args) or args[index + 1] not in {"shadow", "enforce"}
            ):
                normalized[index] = "--aq-smart=shadow"
        return super().parse_args(ctx, normalized)


@cli.command(
    "test",
    cls=_TestCommand,
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": ["--aq-help"],
    },
)
@click.option("--aq-detach", is_flag=True, help="Submit to the managed queue and return its ID.")
@click.option("--aq-wait", is_flag=True, help="With --aq-detach, register a durable job wait.")
@click.option("--aq-idempotency-key", default=None, help="Replay a detached submission safely.")
@click.option("--aq-status", is_flag=True, help="Show slot and full-suite lock occupancy and exit.")
@click.option(
    "--aq-no-wait",
    is_flag=True,
    help="Fail immediately when every slot, or the full-suite lock, is busy.",
)
@click.option("--aq-workers", type=int, default=None, help="Override the enforced -n cap.")
@click.option(
    "--aq-timeout",
    type=int,
    default=None,
    help="Seconds to wait for a slot (and, for a full-suite run, the full-suite lock).",
)
@click.option(
    "--aq-all-markers",
    is_flag=True,
    help="Override the default marker deselects (perf/migration/slow/tmux/integration).",
)
@click.option("--aq-dry-run", is_flag=True, help="Print the pytest command and exit.")
@click.option(
    "--aq-smart",
    "aq_smart",
    is_flag=False,
    flag_value="shadow",
    default=None,
    type=click.Choice(["shadow", "enforce"]),
    help="Record a smart selection (shadow: run exactly what you named; enforce: run the selected union).",
)
@click.option(
    "--aq-base",
    default=None,
    help="Comparison base ref for --aq-smart (default: origin/<default branch>).",
)
@click.option(
    "--aq-plan-only", is_flag=True, help="Print the selection and its reasons; start nothing."
)
@click.option(
    "--aq-no-jev", is_flag=True, help="Use the static fallback only; record Jev as disabled."
)
@click.option(
    "--aq-project", default=None, help="Project id for an operator run outside a worker session."
)
@click.option(
    "--aq-reap-orphans",
    is_flag=True,
    help="List test runs whose session is gone (dry run); add --aq-apply to terminate them.",
)
@click.option("--aq-apply", is_flag=True, help="With --aq-reap-orphans: terminate the orphans.")
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def test_command(
    ctx: click.Context,
    aq_detach: bool,
    aq_wait: bool,
    aq_idempotency_key: str | None,
    aq_status: bool,
    aq_no_wait: bool,
    aq_workers: int | None,
    aq_timeout: int | None,
    aq_all_markers: bool,
    aq_dry_run: bool,
    aq_smart: str | None,
    aq_base: str | None,
    aq_plan_only: bool,
    aq_no_jev: bool,
    aq_project: str | None,
    aq_reap_orphans: bool,
    aq_apply: bool,
    pytest_args: tuple[str, ...],
) -> None:
    """Run pytest under the box-wide test semaphore.

    \b
    aq test tests/test_pools.py            one file, still slot-gated
    aq test tests/ -k claim                a slice of the suite
    aq test --aq-status                    who is holding the slots
    aq test --aq-no-wait tests/            fail instead of queueing
    aq test --aq-reap-orphans [--aq-apply] free slots held by dead sessions

    Everything that is not an ``--aq-*`` option is passed to pytest
    untouched.  ``-n`` and ``-m`` are added only when you did not supply
    them.  Use ``--aq-help`` for this help (``-h``/``--help`` belong to
    pytest).

    A run that selects the whole suite (no path, ``tests/``, or most of its
    files, without a narrowing ``-k``/``-m``) also takes the box-wide
    full-suite lock: one full-suite run at a time, queued without holding a
    slot.  Focused runs never wait for it.

    A path-shaped argument that does not exist is refused before pytest
    starts (exit 4), and a run that collected nothing exits nonzero and
    says so.
    """
    if aq_wait and not aq_detach:
        raise click.UsageError("--aq-wait requires --aq-detach")
    if aq_idempotency_key and not aq_detach:
        raise click.UsageError("--aq-idempotency-key requires --aq-detach")
    if aq_detach:
        if aq_smart or aq_plan_only or aq_base or aq_no_jev or aq_project:
            raise click.UsageError("Smart selection requires a local run; cannot use --aq-detach")
        if (
            aq_status
            or aq_no_wait
            or aq_timeout is not None
            or aq_workers is not None
            or aq_dry_run
            or aq_reap_orphans
            or aq_apply
        ):
            raise click.UsageError("Detached jobs use server-owned admission and resource caps")
        if not pytest_args:
            raise click.UsageError("Name the tests to submit")
        from .jobs import submit
        from .envelope import emit
        from .app import _handle_errors

        args = list(pytest_args)
        if aq_all_markers and not any(
            a in {"-m", "--markexpr"} or a.startswith("--markexpr=") for a in args
        ):
            args += ["-m", ""]
        result = _handle_errors(submit)(
            ctx, preset="test", argv=args, wait=aq_wait, idempotency_key=aq_idempotency_key
        )
        emit(ctx, result)
        return

    from src.resources.semaphore import (
        SlotSemaphore,
        SlotTimeout,
        default_lock_dir,
        full_suite_lock_dir,
    )
    from src.resources.slot_report import REPORT_ENV, append_event
    from src.resources.box_lock import BoxLock, IncompatibleLockClient

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
    timeout = _slot_wait_timeout(timeout, aq_timeout)

    lock_dir = default_lock_dir(config)
    sem = SlotSemaphore(lock_dir, slots)
    box_lock = BoxLock(lock_dir, slots)
    full_lock = SlotSemaphore(full_suite_lock_dir(sem.lock_dir), 1)

    if aq_status:
        _render_status(
            sem.snapshot(), full_lock.snapshot(), _holder_verdicts(lock_dir), box_lock.snapshot()
        )
        return

    if aq_apply and not aq_reap_orphans:
        console.print("[red]aq test:[/] --aq-apply only applies to --aq-reap-orphans")
        ctx.exit(2)
    if aq_reap_orphans:
        ctx.exit(_reap_orphans(lock_dir, apply=aq_apply))

    plan = None
    if aq_plan_only and aq_smart is None:
        aq_smart = "shadow"
    if aq_smart is not None:
        from src.test_selection.report import render_report

        narrowing = _narrowing_flags(pytest_args)
        if aq_smart == "enforce" and narrowing:
            click.echo(
                "aq test: --aq-smart=enforce refuses -k/-m/--lf/--deselect/node ids "
                f"({', '.join(narrowing)}); use --aq-smart (shadow) or a normal run",
                err=True,
            )
            ctx.exit(2)
        targets = _positional_args(pytest_args)
        plan = _smart_plan(
            ctx,
            mode=aq_smart,
            base=aq_base,
            plan_only=aq_plan_only,
            jev=not aq_no_jev,
            project=aq_project,
            pytest_args=pytest_args,
            marker_policy="all" if aq_all_markers else "default",
        )
        if aq_plan_only or (aq_smart == "shadow" and not targets):
            click.echo(render_report(plan.result, mode=aq_smart, plan_only=True))
        if aq_plan_only:
            ctx.exit(0)
        if aq_smart == "shadow" and not targets:
            click.echo("aq test: nothing was verified — name the targets to run", err=True)
            ctx.exit(2)
        if aq_smart == "enforce":
            if plan.full_required and not plan.full_suite_authorized:
                reason = plan.result.get("fallback_reason") or "global or incomplete inputs"
                click.echo(
                    "aq test: full_suite_required — the selection fell back to the whole suite "
                    f"({reason}); run your task's focused and area checks and report the requirement",
                    err=True,
                )
                ctx.exit(4)
            if not plan.ordered and not targets:
                click.echo("aq test: empty_selection — nothing was verified", err=True)
                ctx.exit(4)
            pytest_args = tuple(plan.ordered) + tuple(
                a for a in pytest_args if a not in plan.ordered
            )

    if not pytest_args:
        console.print("[yellow]No pytest arguments given.[/] Try: aq test tests/test_config.py")
        console.print("[dim]Refusing to run the whole suite implicitly — see --aq-help.[/]")
        ctx.exit(2)

    missing = _missing_paths(pytest_args)
    if missing:
        # Fail before taking a slot.  pytest under xdist turns a bad path
        # into "no tests ran", which an agent reads as a pass and closes
        # its task on; the whole point of naming a file is to run it.
        console.print(f"[red]aq test:[/] no such test path: {', '.join(missing)}")
        console.print(f"[dim]cwd: {os.getcwd()} — nothing was run.[/]")
        ctx.exit(4)

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

    dsn_error = postgres_test_dsn_error()
    if dsn_error:
        console.print(f"[red]aq test:[/] {dsn_error}")
        ctx.exit(4)

    full_suite = _is_full_suite(pytest_args)
    test_run_id = _new_test_run_id()
    meta = {
        **_holder_meta(test_run_id),
        "command": shlex.join(argv),
        "scope": "full" if full_suite else "focused",
    }

    # A supervising caller (the development publisher) asks, through the
    # env, to be told how long this run queued, so it can charge its run
    # budget for running only.  See src/resources/slot_report.py.  A
    # full-suite run queues on the full-suite lock and then on a slot; the
    # report records both as one wait, measured from queued_at.
    report = os.environ.get(REPORT_ENV) or None
    queued_at = time.monotonic()
    announced = False

    def _announce_wait() -> None:
        nonlocal announced
        if report and not announced:
            append_event(report, "waiting", at=time.time() - (time.monotonic() - queued_at))
            announced = True

    def _on_wait(waited: float, snapshot: dict) -> None:
        _announce_wait()
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

    def _on_full_wait(waited: float, snapshot: dict) -> None:
        from rich.markup import escape

        _announce_wait()
        # Same reasoning as _on_wait: a run queued behind another full
        # suite, possibly for an hour, has to look queued.
        holder = _full_suite_holder(snapshot) or "nobody (retrying)"
        console.print(
            f"[dim]aq test: waiting {waited:.0f}s for the full-suite lock "
            f"(one full-suite run at a time); held by {escape(holder)}[/]"
        )

    budget = 0 if aq_no_wait else timeout
    if plan is not None:
        _smart_recheck(ctx, plan)
    try:
        with ExitStack() as held:
            if full_suite:
                # The full-suite lock first, a slot second: a run queued
                # behind another full suite must not sit on a slot a
                # focused run could be using.
                try:
                    held.enter_context(
                        full_lock.acquire(
                            timeout=budget, poll=poll, meta=meta, on_wait=_on_full_wait
                        )
                    )
                except SlotTimeout:
                    if report:
                        append_event(
                            report,
                            "slot_timeout",
                            waited=round(time.monotonic() - queued_at, 3),
                        )
                    _report_full_suite_busy(
                        full_lock.snapshot(), waited_for=None if aq_no_wait else budget
                    )
                    ctx.exit(75)  # EX_TEMPFAIL, like a full box
            admitted_slots = held.enter_context(
                # Phase 1 preserves the current policy: even full suites
                # use shared box admission, after their separate full lock.
                box_lock.acquire(
                    timeout=max(0.0, budget - (time.monotonic() - queued_at)),
                    poll=poll,
                    meta=meta,
                    on_wait=_on_wait,
                )
            )
            slot = admitted_slots[0]
            if plan is not None:
                _smart_recheck(ctx, plan)
            if report:
                append_event(
                    report, "acquired", waited=round(time.monotonic() - queued_at, 3), slot=slot
                )
            scope = "full-suite lock + " if full_suite else ""
            console.print(f"[dim]aq test: {scope}slot {slot} of {slots}, -n {workers}[/]")
            click.echo(f"$ {shlex.join(argv)}", err=True)
            child_env = os.environ.copy()
            # Always replace an inherited token. Nested or concurrent `aq test`
            # invocations are separate owners and must never derive the same
            # PostgreSQL database names. The slot record names it too, so a
            # reaper can find pytest processes that outlive this wrapper.
            child_env["AQ_TEST_RUN_ID"] = test_run_id
            started_at = time.monotonic()
            try:
                returncode = _run_forwarding_signals(argv, env=child_env)
            finally:
                if report:
                    append_event(report, "released")
        if plan is not None:
            _smart_observe(
                plan,
                exit_code=returncode,
                duration_ms=round((time.monotonic() - started_at) * 1000),
                executed=_positional_args(pytest_args),
            )
        if returncode == 5:
            # pytest's EXIT_NOTESTSCOLLECTED.  Nonzero already, but silent
            # about *why*: say plainly that nothing was verified.
            console.print(
                "[red]aq test: no tests were collected[/] — nothing was verified. "
                "Check the paths, -k expression and marker deselects."
            )
        ctx.exit(returncode)
    except (SlotTimeout, IncompatibleLockClient) as exc:
        if report:
            append_event(report, "slot_timeout", waited=round(time.monotonic() - queued_at, 3))
        console.print(f"[red]aq test:[/] {exc}")
        console.print("[dim]Run `aq test --aq-status` to see who is holding them.[/]")
        ctx.exit(75)  # EX_TEMPFAIL — retryable, not a test failure


def _report_full_suite_busy(snapshot: dict, *, waited_for: int | None) -> None:
    """Refuse a second full-suite run, naming the one already in progress."""
    from rich.markup import escape

    holder = _full_suite_holder(snapshot) or "a run that has just finished"
    if waited_for is None:
        console.print(
            f"[red]aq test:[/] a full-suite run is already in progress: {escape(holder)}."
        )
    else:
        console.print(
            f"[red]aq test:[/] waited {_elapsed(waited_for)} for the full-suite lock; "
            f"it is still held: {escape(holder)}."
        )
    console.print(
        "Only one full-suite run may run box-wide at a time. Run the focused tests for "
        "your change instead (aq test tests/test_<area>.py), or retry later"
        + (" without --aq-no-wait to queue behind it." if waited_for is None else ".")
    )
    console.print("[dim]Run `aq test --aq-status` to see every holder.[/]")
