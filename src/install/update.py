"""`aq update`: move a source-checkout installation to the latest code, safely.

The one-command install clones AQ into a checkout and runs it from there, so
updating means: stop the daemon, fast-forward the checkout, reinstall what the
new code needs, and start the daemon again.  Each of those can go wrong, and
the order and the guards below exist so that a failed update leaves a working
AQ behind rather than a half-updated one.

Safety, in the order it is applied:

* **Refuse before touching anything** when the update cannot be done cleanly:
  inside a worker slot (workers never restart or migrate the operator's
  daemon), on a detached HEAD or a branch with no upstream, with edits to
  tracked files, with local commits the upstream does not have, or while
  another update holds the lock.
* **Back up the database first** when the update brings migrations: the daemon
  applies migrations when it starts, and that is the one step that cannot be
  undone by moving the code back.  A backup that fails stops the update before
  the daemon is stopped.
* **Stop the daemon, keep the agents.**  `aq stop --keep-sessions` leaves agent
  sessions running; the restarted daemon re-adopts them.
* **Only fast-forward.**  The checkout moves forward to its upstream and never
  merges; a shallow installer clone is reset to the upstream tip, which is safe
  because the tree was verified clean.
* **Rebuild only what changed**: Python dependencies when `pyproject.toml` or
  the generated client changed, the dashboard when its build inputs did.
* **Roll back on failure** to the previous commit and start the old daemon
  again -- unless the update brought migrations *and* the new daemon was
  started, because the database may already be ahead of the old code.  Then
  the update stops, says so, and names the backup.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .command import CommandOutput, CommandRunner, run_command
from .dashboard import (
    BUILD_INPUTS,
    BuildOutcome,
    build_bundle,
    bundle_directory,
    bundle_is_current,
    source_fingerprint,
    stamp_path,
)
from .onboarding import DASHBOARD_PATH, HttpProbe, _read_config, api_base_url, http_status
from .redaction import redact

#: Seconds `aq stop` / `aq start` may take; both wait on the daemon themselves.
DAEMON_TIMEOUT = 180.0
GIT_TIMEOUT = 300.0
PIP_TIMEOUT = 900.0
BACKUP_TIMEOUT = 1800.0

LOCK_NAME = "update.lock"
BACKUP_DIR = "backups"

OUTCOME_UP_TO_DATE = "up_to_date"
OUTCOME_UPDATED = "updated"
OUTCOME_ROLLED_BACK = "rolled_back"
OUTCOME_FAILED = "failed"

#: Exit codes, aligned with `aq install`.
EXIT_CODES = {
    OUTCOME_UP_TO_DATE: 0,
    OUTCOME_UPDATED: 0,
    OUTCOME_ROLLED_BACK: 20,
    OUTCOME_FAILED: 20,
}
EXIT_REFUSED = 10

Progress = Callable[[str, bool, str], None]


class UpdateRefused(Exception):
    """The update will not start; nothing was changed."""

    def __init__(self, reason: str, remediation: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.remediation = remediation


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    checkout: Path
    branch: str
    upstream: str
    current: str
    target: str
    shallow: bool
    changed: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()

    @property
    def up_to_date(self) -> bool:
        return self.current == self.target

    @property
    def migrations(self) -> bool:
        return any(path.startswith("migrations/versions/") for path in self.changed)

    @property
    def dependencies(self) -> bool:
        return any(
            path == "pyproject.toml" or path.startswith("packages/aq-client/")
            for path in self.changed
        )

    @property
    def dashboard_inputs(self) -> bool:
        return any(
            path == entry or path.startswith(f"{entry}/")
            for path in self.changed
            for entry in BUILD_INPUTS
        )


@dataclass(slots=True)
class UpdateReport:
    outcome: str
    plan: UpdatePlan
    steps: list[tuple[str, bool, str]] = field(default_factory=list)
    backup: Path | None = None
    remediation: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]


# ---------------------------------------------------------------------------
# Planning: read-only apart from `git fetch`
# ---------------------------------------------------------------------------


def _git(execute: CommandRunner, checkout: Path, *args: str) -> CommandOutput:
    return execute(["git", "-C", str(checkout), *args], timeout=GIT_TIMEOUT)


def plan_update(
    checkout: Path,
    *,
    execute: CommandRunner = run_command,
    environ: Mapping[str, str] | None = None,
) -> UpdatePlan:
    """Decide whether and how *checkout* can be updated; raise UpdateRefused if not."""
    env = os.environ if environ is None else environ
    if env.get("AQ_DB_SCOPE") == "worker":
        raise UpdateRefused(
            "aq update does not run inside an agent's worker slot",
            "Run `aq update` as the operator, from your own terminal. Workers never restart "
            "or migrate the daemon they work for.",
        )

    branch = _git(execute, checkout, "symbolic-ref", "--short", "-q", "HEAD")
    if not branch.ok or not branch.out.strip():
        raise UpdateRefused(
            f"{checkout} is not on a branch (detached HEAD)",
            f"Check out the branch AQ should follow (`git -C {checkout} checkout main`), then "
            "rerun `aq update`.",
        )
    upstream = _git(execute, checkout, "rev-parse", "--abbrev-ref", "@{u}")
    if not upstream.ok or not upstream.out.strip():
        raise UpdateRefused(
            f"branch {branch.out.strip()} in {checkout} has no upstream to update from",
            f"Set one (`git -C {checkout} branch --set-upstream-to origin/main`), then rerun "
            "`aq update`.",
        )
    upstream_name = upstream.out.strip()

    dirty = _git(execute, checkout, "status", "--porcelain", "--untracked-files=no")
    if not dirty.ok:
        raise UpdateRefused(
            f"could not read the state of {checkout}: {dirty.message()}",
            "Fix the repository, then rerun `aq update`.",
        )
    if dirty.out.strip():
        files = ", ".join(line[3:] for line in dirty.out.splitlines()[:5])
        raise UpdateRefused(
            f"{checkout} has local changes to tracked files ({files})",
            "Commit, stash or discard them, then rerun `aq update`. It never overwrites "
            "local edits.",
        )

    remote = upstream_name.split("/", 1)[0]
    fetched = _git(execute, checkout, "fetch", "--quiet", remote)
    if not fetched.ok:
        raise UpdateRefused(
            f"could not fetch {remote}: {fetched.message()}",
            "Check the network and your access to the repository, then rerun `aq update`.",
        )

    head = _git(execute, checkout, "rev-parse", "HEAD").out.strip()
    target = _git(execute, checkout, "rev-parse", "@{u}").out.strip()
    shallow = _git(execute, checkout, "rev-parse", "--is-shallow-repository").out.strip() == "true"
    plan = UpdatePlan(
        checkout=checkout,
        branch=branch.out.strip(),
        upstream=upstream_name,
        current=head,
        target=target,
        shallow=shallow,
    )
    if plan.up_to_date:
        return plan

    forward = _git(execute, checkout, "merge-base", "--is-ancestor", "HEAD", "@{u}").ok
    if not forward:
        behind_us = _git(execute, checkout, "merge-base", "--is-ancestor", "@{u}", "HEAD").ok
        if behind_us or not shallow:
            raise UpdateRefused(
                f"{plan.branch} has commits that {upstream_name} does not",
                "AQ only fast-forwards. Push or remove those commits, then rerun `aq update`.",
            )

    changed = _git(execute, checkout, "diff", "--name-only", head, target)
    subjects = _git(execute, checkout, "log", "--format=%h %s", f"{head}..{target}")
    return UpdatePlan(
        checkout=checkout,
        branch=plan.branch,
        upstream=upstream_name,
        current=head,
        target=target,
        shallow=shallow,
        changed=tuple(line for line in changed.out.splitlines() if line.strip()),
        subjects=tuple(line for line in subjects.out.splitlines() if line.strip())
        if subjects.ok
        else (),
    )


# ---------------------------------------------------------------------------
# The database backup
# ---------------------------------------------------------------------------


def find_pg_dump(which: Callable[[str], str | None] = shutil.which) -> str | None:
    found = which("pg_dump")
    if found:
        return found
    patterns = (
        "/opt/homebrew/opt/postgresql@*/bin/pg_dump",
        "/usr/local/opt/postgresql@*/bin/pg_dump",
        "/usr/lib/postgresql/*/bin/pg_dump",
    )
    for pattern in patterns:
        matches = sorted(Path("/").glob(pattern.lstrip("/")), reverse=True)
        if matches:
            return str(matches[0])
    return None


def backup_database(
    config_path: Path,
    destination: Path,
    *,
    execute: CommandRunner,
    pg_dump: str | None,
) -> tuple[bool, str]:
    """`pg_dump -Fc` the configured database to *destination*.

    The password travels in ``PGPASSWORD`` for the child only, never on the
    command line where another process could read it.
    """
    if pg_dump is None:
        return False, "pg_dump was not found"
    try:
        from sqlalchemy.engine import make_url

        from src.config import load_config

        url = make_url(load_config(str(config_path)).database.url)
    except Exception as error:  # noqa: BLE001 - reported, and the update stops
        return False, f"could not read the database settings: {error}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    argv = [
        pg_dump,
        "--format=custom",
        "--host",
        url.host or "localhost",
        "--port",
        str(url.port or 5432),
        "--username",
        url.username or "",
        "--dbname",
        url.database or "",
        "--file",
        str(destination),
    ]
    output = execute(argv, timeout=BACKUP_TIMEOUT, env=env)
    if not output.ok:
        destination.unlink(missing_ok=True)
        return False, redact(output.message())
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return True, str(destination)


# ---------------------------------------------------------------------------
# Applying the update
# ---------------------------------------------------------------------------


def installed_extras(present: Callable[[str], bool]) -> list[str]:
    """The optional-dependency groups this environment was installed with."""
    extras = ["cli"]
    if present("pytest"):
        extras.append("dev")
    if present("anthropic"):
        extras.append("llm")
    return extras


def _distribution_present(name: str) -> bool:
    from importlib import metadata

    try:
        metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return False
    return True


@dataclass
class Host:
    """Everything `apply_update` reaches outside itself, so a test can replace it."""

    execute: CommandRunner = run_command
    probe: HttpProbe = http_status
    python: str = sys.executable
    aq: str = "aq"
    system: str = sys.platform
    arch: str = ""
    state_dir: Path = Path.home() / ".agent-queue"
    pg_dump: str | None = None
    build: Callable[..., BuildOutcome] = build_bundle
    extras: list[str] = field(default_factory=lambda: ["cli"])
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


def apply_update(
    plan: UpdatePlan,
    host: Host,
    *,
    backup: bool = True,
    progress: Progress | None = None,
) -> UpdateReport:
    report = UpdateReport(OUTCOME_UP_TO_DATE, plan)
    if plan.up_to_date:
        return report

    def step(name: str, ok: bool, message: str = "") -> None:
        report.steps.append((name, ok, message))
        if progress is not None:
            progress(name, ok, message)

    lock = host.state_dir / LOCK_NAME
    host.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise UpdateRefused(
            "another `aq update` is in progress",
            f"If none is running, remove {lock} and rerun `aq update`.",
        ) from error
    with os.fdopen(descriptor, "w") as handle:
        handle.write(f"{os.getpid()}\n")
    try:
        return _apply_locked(plan, host, report, step, backup=backup)
    finally:
        lock.unlink(missing_ok=True)


def _apply_locked(plan, host: Host, report: UpdateReport, step, *, backup: bool) -> UpdateReport:
    checkout = plan.checkout
    config_path = host.state_dir / "config.yaml"
    base = api_base_url(_read_config(config_path))

    def healthy() -> bool:
        return host.probe(f"{base}/health") in (200, 503)

    # -- 1. back up, while nothing has been stopped yet ---------------------
    if plan.migrations and backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = host.state_dir / BACKUP_DIR / f"agent_queue-{plan.current[:12]}-{stamp}.dump"
        ok, message = backup_database(
            config_path, destination, execute=host.execute, pg_dump=host.pg_dump
        )
        if not ok:
            step("Back up the database", False, message)
            raise UpdateRefused(
                f"this update changes the database schema, and the backup failed: {message}",
                "Nothing was changed. Install PostgreSQL's client tools (pg_dump) or fix the "
                "reported problem and rerun `aq update`, or rerun with `--no-backup` to update "
                "without one.",
            )
        report.backup = destination
        step("Back up the database", True, str(destination))

    # -- 2. stop the daemon, keeping agent sessions ------------------------
    was_running = healthy()
    if was_running:
        stopped = host.execute([host.aq, "stop", "--keep-sessions"], timeout=DAEMON_TIMEOUT)
        for _ in range(20):
            if not healthy():
                break
            host.sleep(0.5)
        if not stopped.ok or healthy():
            step("Stop the daemon", False, stopped.message())
            raise UpdateRefused(
                "the daemon did not stop, so the update did not start",
                "Nothing was changed. Run `aq stop`, then rerun `aq update`.",
            )
        step("Stop the daemon", True, "agent sessions keep running")

    daemon_started_new_code = False
    try:
        # -- 3. move the code forward -------------------------------------
        if plan.shallow:
            moved = _git(host.execute, checkout, "reset", "--hard", "--quiet", plan.target)
        else:
            moved = _git(host.execute, checkout, "merge", "--ff-only", "--quiet", plan.target)
        if not moved.ok:
            raise _StepFailed("Update the code", moved.message())
        step("Update the code", True, f"{plan.current[:9]} -> {plan.target[:9]}")

        # -- 4. dependencies ----------------------------------------------
        if plan.dependencies:
            _install_dependencies(host, checkout)
            step("Reinstall Python dependencies", True)

        # -- 5. dashboard --------------------------------------------------
        _rebuild_dashboard(host, checkout, step)

        # -- 6. start the daemon on the new code ---------------------------
        if was_running:
            daemon_started_new_code = True
            _start_daemon(host, base, checkout, healthy)
            step("Start the daemon", True, "agent sessions are re-adopted")
    except _StepFailed as failure:
        step(failure.name, False, failure.message)
        return _recover(
            plan, host, report, step, failure, was_running, daemon_started_new_code, base, healthy
        )

    report.outcome = OUTCOME_UPDATED
    return report


class _StepFailed(Exception):
    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name
        self.message = message


def _install_dependencies(host: Host, checkout: Path) -> None:
    extras = ",".join(host.extras)
    for argv in (
        [host.python, "-m", "pip", "install", "--quiet", "-e", "./packages/aq-client"],
        [host.python, "-m", "pip", "install", "--quiet", "-e", f".[{extras}]"],
    ):
        # From inside the checkout: the `cli` extra names the API client by a
        # path relative to the working directory.
        output = host.execute(argv, timeout=PIP_TIMEOUT, cwd=str(checkout))
        if not output.ok:
            raise _StepFailed("Reinstall Python dependencies", output.message())


def _rebuild_dashboard(host: Host, checkout: Path, step) -> None:
    has_bundle = bundle_directory(checkout).is_dir() or stamp_path(checkout).exists()
    if not has_bundle:
        # A contributor checkout that runs the dashboard from Vite.
        return
    fingerprint = source_fingerprint(checkout, git="git", execute=host.execute)
    if bundle_is_current(checkout, fingerprint):
        return
    outcome = host.build(
        checkout,
        state_dir=host.state_dir,
        system=host.system,
        arch=host.arch,
        execute=host.execute,
        interpreter=host.python,
        fingerprint=fingerprint,
        rerun="rerun `aq update`",
    )
    if not outcome.ok:
        raise _StepFailed("Rebuild the dashboard", outcome.summary)
    step("Rebuild the dashboard", True)


def _start_daemon(host: Host, base: str, checkout: Path, healthy) -> None:
    started = host.execute([host.aq, "start", "--no-dashboard"], timeout=DAEMON_TIMEOUT)
    if not started.ok or not healthy():
        raise _StepFailed("Start the daemon", started.message() or "it did not answer /health")
    if bundle_directory(checkout).is_dir():
        status = host.probe(f"{base}{DASHBOARD_PATH}/")
        if status is None or status >= 400:
            raise _StepFailed("Start the daemon", "the daemon is up but not serving the dashboard")


def _recover(plan, host: Host, report, step, failure, was_running, started_new, base, healthy):
    checkout = plan.checkout
    log = host.state_dir / "daemon.log"
    if plan.migrations and started_new:
        # The new daemon may already have migrated the database; the old code
        # cannot be trusted against it.  Stop, and say exactly where things are.
        report.outcome = OUTCOME_FAILED
        backup = f" A backup from before the update is at {report.backup}." if report.backup else ""
        report.remediation = (
            f"The update's database migrations may already have run, so AQ was not rolled "
            f"back. Read {log} for why the daemon did not start, fix it, and run `aq start`."
            f"{backup}"
        )
        return report

    reset = _git(host.execute, checkout, "reset", "--hard", "--quiet", plan.current)
    if not reset.ok:
        report.outcome = OUTCOME_FAILED
        report.remediation = (
            f"Rolling back also failed ({reset.message()}). Run "
            f"`git -C {checkout} reset --hard {plan.current}` and `aq start`."
        )
        step("Roll back the code", False, reset.message())
        return report
    step("Roll back the code", True, f"back at {plan.current[:9]}")
    try:
        if plan.dependencies:
            _install_dependencies(host, checkout)
        _rebuild_dashboard(host, checkout, step)
        if was_running:
            _start_daemon(host, base, checkout, healthy)
            step("Start the previous daemon", True)
    except _StepFailed as again:
        step(again.name, False, again.message)
        report.outcome = OUTCOME_FAILED
        report.remediation = (
            f"The code is back at {plan.current[:9]}, but {again.name.lower()} failed too: "
            f"{again.message}. Read {log}, fix it, and run `aq start`."
        )
        return report
    report.outcome = OUTCOME_ROLLED_BACK
    report.remediation = (
        f"{failure.name} failed ({failure.message}), so AQ was put back on {plan.current[:9]} "
        f"and is running as before. Fix the reported problem and rerun `aq update`."
    )
    return report


def describe(plan: UpdatePlan, *, backup: bool, daemon_running: bool) -> list[str]:
    """What the update is about to do, for a person deciding whether to go ahead."""
    lines = [
        f"{len(plan.subjects) or 'new'} commit(s) from {plan.upstream} "
        f"({plan.current[:9]} -> {plan.target[:9]})"
    ]
    if plan.migrations:
        lines.append(
            "Includes database migrations: "
            + ("the database is backed up first" if backup else "NO backup (--no-backup)")
        )
    if plan.dependencies:
        lines.append("Reinstalls Python dependencies")
    if plan.dashboard_inputs:
        lines.append("Rebuilds the dashboard")
    if daemon_running:
        lines.append(
            "Stops the daemon for the update and starts it again; running agents keep running"
        )
    lines.append("Rolls back to the current version if anything fails")
    return lines


__all__ = [
    "EXIT_CODES",
    "EXIT_REFUSED",
    "Host",
    "OUTCOME_FAILED",
    "OUTCOME_ROLLED_BACK",
    "OUTCOME_UPDATED",
    "OUTCOME_UP_TO_DATE",
    "UpdatePlan",
    "UpdateRefused",
    "UpdateReport",
    "apply_update",
    "backup_database",
    "describe",
    "find_pg_dump",
    "installed_extras",
    "plan_update",
]
