"""`aq update`: fetch, stop, fast-forward, rebuild, restart -- or roll back.

Git is real here: every test builds a bare "origin" and a clone of it, so the
refusals, the fast-forward and the rollback move real commits.  The daemon,
pip, pg_dump and the dashboard build are fakes that record what they were
asked to do.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.install import update as update_module
from src.install import update_finish
from src.install.command import CommandOutput, run_command
from src.install.dashboard import BuildOutcome, bundle_directory
from src.install.update import (
    OUTCOME_FAILED,
    OUTCOME_ROLLED_BACK,
    OUTCOME_UPDATED,
    Host,
    UpdatePlan,
    UpdateRefused,
    apply_update,
    backup_database,
    installed_extras,
    plan_update,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd: Path, *args: str) -> str:
    import os

    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    )
    return completed.stdout.strip()


class Repo:
    def __init__(self, tmp_path: Path, *, shallow: bool = False):
        self.origin = tmp_path / "origin.git"
        self.seed = tmp_path / "seed"
        self.checkout = tmp_path / "checkout"
        _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(self.origin))
        _git(tmp_path, "clone", "--quiet", str(self.origin), str(self.seed))
        for path, text in {
            "pyproject.toml": "[project]\nname = 'agent-queue'\n",
            "README.md": "AQ\n",
            "migrations/versions/a0001.py": "revision = 'a0001'\n",
            "dashboard/src/app.ts": "export const x = 1\n",
        }.items():
            (self.seed / path).parent.mkdir(parents=True, exist_ok=True)
            (self.seed / path).write_text(text, encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "--quiet", "-m", "initial")
        _git(self.seed, "push", "--quiet", "origin", "main")
        clone = ["clone", "--quiet"]
        if shallow:
            clone += ["--depth", "1"]
        _git(tmp_path, *clone, f"file://{self.origin}", str(self.checkout))

    def push(self, path: str, text: str, message: str = "change") -> str:
        target = self.seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "--quiet", "-m", message)
        _git(self.seed, "push", "--quiet", "origin", "main")
        return _git(self.seed, "rev-parse", "HEAD")

    def remove(self, path: str, message: str = "remove") -> str:
        _git(self.seed, "rm", "--quiet", path)
        _git(self.seed, "commit", "--quiet", "-m", message)
        _git(self.seed, "push", "--quiet", "origin", "main")
        return _git(self.seed, "rev-parse", "HEAD")

    def pull(self) -> None:
        _git(self.checkout, "pull", "--quiet", "--ff-only")

    def head(self) -> str:
        return _git(self.checkout, "rev-parse", "HEAD")


FINISH = ("-m", "src.install.update_finish")
#: Where the default `dashboard.server` settings put the dashboard server.
DASHBOARD_URL = "http://127.0.0.1:8082/"


class FakeHost:
    """The daemon, pip, pg_dump and the dashboard build; git passes through.

    The finisher -- the fresh process `aq update` hands the pulled code to --
    runs in-process here with these same fakes, through its real command line
    and its real output, so the hand-off protocol is exercised both ways.
    ``real_finisher`` runs it as the subprocess it is in production.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        running: bool = True,
        dashboard_running: bool = False,
        real_finisher: bool = False,
    ):
        self.state_dir = state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "config.yaml").write_text("messaging_platform: none\n", encoding="utf-8")
        self.running = running
        self.start_results: list[bool] = []
        #: The dashboard server on the default dashboard.server URL.  `aq stop`
        #: stops it with the daemon; `aq start` brings it up unless the next
        #: ``dashboard_start_results`` entry says it fails to.
        self.dashboard_running = dashboard_running
        self.dashboard_start_results: list[bool] = []
        self.dashboard_stops = True
        #: Every URL that was probed, so a test can prove what was never asked.
        self.probed: list[str] = []
        self.fail_pip = False
        self.calls: list[tuple[str, ...]] = []
        self.pip_cwds: list[str | None] = []
        self.builds = 0
        self.real_finisher = real_finisher
        self.finishes: list[tuple[tuple[str, ...], str | None]] = []
        #: Per dashboard server start: does its identity report a verified bundle?
        self.dashboard_verified: list[bool] = []
        self._verified = True
        self.heads_at_finish: list[str] = []

    def execute(self, argv, **kwargs) -> CommandOutput:
        command = tuple(str(part) for part in argv)
        if command[0] == "git":
            return run_command(command, **kwargs)
        if command[1:3] == FINISH:
            return self._finish(command, **kwargs)
        self.calls.append(command)
        if command[1:] == ("stop", "--keep-sessions"):
            self.running = False
            self.dashboard_running = self.dashboard_running and not self.dashboard_stops
            return CommandOutput(argv=command, returncode=0)
        if command[1:] == ("start", "--no-dashboard"):
            ok = self.start_results.pop(0) if self.start_results else True
            self.running = ok
            if ok:
                self._start_dashboard()
            return CommandOutput(
                argv=command, returncode=0 if ok else 1, stderr="" if ok else "boot failed"
            )
        if command[1:] == ("dashboard", "stop"):
            self.dashboard_running = self.dashboard_running and not self.dashboard_stops
            return CommandOutput(argv=command, returncode=0 if self.dashboard_stops else 1)
        if command[1:] == ("dashboard", "start"):
            ok = self._start_dashboard()
            return CommandOutput(argv=command, returncode=0 if ok else 1)
        if "pip" in command:
            self.pip_cwds.append(kwargs.get("cwd"))
            return CommandOutput(argv=command, returncode=1 if self.fail_pip else 0)
        raise AssertionError(f"unexpected command: {command}")

    def _finish(self, command, **kwargs) -> CommandOutput:
        cwd = kwargs.get("cwd")
        self.finishes.append((command, cwd))
        if cwd:
            self.heads_at_finish.append(_git(Path(cwd), "rev-parse", "HEAD"))
        if self.real_finisher:
            return run_command(command, **kwargs)
        out = io.StringIO()
        code = update_finish.main(
            list(command[3:]),
            host_factory=lambda **fields: replace(
                Host(**fields),
                execute=self.execute,
                probe=self.probe,
                identify=self.identify,
                build=self.build,
                sleep=lambda seconds: None,
            ),
            out=out,
        )
        return CommandOutput(argv=command, returncode=code, stdout=out.getvalue())

    def _start_dashboard(self) -> bool:
        if self.dashboard_running:
            return True  # `aq start` leaves a running dashboard server alone
        ok = self.dashboard_start_results.pop(0) if self.dashboard_start_results else True
        self.dashboard_running = ok
        self._verified = self.dashboard_verified.pop(0) if self.dashboard_verified else True
        return ok

    def probe(self, url: str) -> int | None:
        self.probed.append(url)
        if url.startswith(DASHBOARD_URL):
            return 200 if self.dashboard_running else None
        return 200 if self.running else None

    def identify(self, url: str) -> dict | None:
        self.probed.append(f"{url}__aq/health")
        if url != DASHBOARD_URL or not self.dashboard_running:
            return None
        return {
            "service": "aq-dashboard-server",
            "pid": 4242,
            "bundle": {"version": "2", "files": 3, "verified": self._verified},
        }

    def build(self, checkout, **kwargs) -> BuildOutcome:
        self.builds += 1
        return BuildOutcome(True, "built")

    def host(self) -> Host:
        return Host(
            execute=self.execute,
            probe=self.probe,
            identify=self.identify,
            python=sys.executable if self.real_finisher else "/venv/bin/python",
            aq="/venv/bin/aq",
            system="darwin",
            arch="arm64",
            state_dir=self.state_dir,
            pg_dump="/opt/homebrew/bin/pg_dump",
            build=self.build,
            extras=["cli"],
            sleep=lambda seconds: None,
        )

    def ran(self, *tail: str) -> bool:
        return any(call[1:] == tail for call in self.calls)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


@pytest.fixture
def fake(tmp_path):
    return FakeHost(tmp_path / "aq")


def _no_worker_scope(monkeypatch):
    monkeypatch.delenv("AQ_DB_SCOPE", raising=False)


# -- planning ---------------------------------------------------------------


def test_an_up_to_date_checkout_changes_nothing(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    plan = plan_update(repo.checkout)

    report = apply_update(plan, fake.host())

    assert plan.up_to_date
    assert report.exit_code == 0
    assert fake.calls == []


def test_the_plan_names_what_the_update_changes(repo, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("migrations/versions/a0002.py", "revision = 'a0002'\n", "add a migration")
    repo.push("pyproject.toml", "[project]\nname = 'agent-queue'\nversion = '2'\n", "bump")
    target = repo.push("dashboard/src/app.ts", "export const x = 2\n", "dashboard")

    plan = plan_update(repo.checkout)

    assert plan.target == target and not plan.up_to_date
    assert plan.migrations and plan.dependencies and plan.dashboard_inputs
    assert [line.split(" ", 1)[1] for line in plan.subjects] == [
        "dashboard",
        "bump",
        "add a migration",
    ]
    assert repo.head() == plan.current  # planning only fetched


def test_local_edits_are_refused_before_anything_is_fetched(repo, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("README.md", "AQ 2\n")
    (repo.checkout / "README.md").write_text("my edit\n", encoding="utf-8")

    with pytest.raises(UpdateRefused, match="local changes to tracked files"):
        plan_update(repo.checkout)
    assert (repo.checkout / "README.md").read_text(encoding="utf-8") == "my edit\n"


def test_untracked_files_do_not_block_an_update(repo, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("README.md", "AQ 2\n")
    (repo.checkout / "notes.txt").write_text("mine\n", encoding="utf-8")

    assert not plan_update(repo.checkout).up_to_date


def test_local_commits_are_refused_rather_than_merged_or_discarded(repo, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("README.md", "AQ 2\n")
    (repo.checkout / "LOCAL.md").write_text("local\n", encoding="utf-8")
    _git(repo.checkout, "add", "LOCAL.md")
    _git(repo.checkout, "commit", "--quiet", "-m", "local work")

    with pytest.raises(UpdateRefused, match="only fast-forwards|does not"):
        plan_update(repo.checkout)


def test_a_worker_slot_is_refused(repo, monkeypatch):
    monkeypatch.setenv("AQ_DB_SCOPE", "worker")

    with pytest.raises(UpdateRefused, match="worker slot"):
        plan_update(repo.checkout)


def test_a_detached_head_is_refused(repo, monkeypatch):
    _no_worker_scope(monkeypatch)
    _git(repo.checkout, "checkout", "--quiet", "--detach")

    with pytest.raises(UpdateRefused, match="detached HEAD"):
        plan_update(repo.checkout)


# -- applying ---------------------------------------------------------------


def test_an_update_stops_moves_the_code_and_starts_again(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    target = repo.push("README.md", "AQ 2\n")

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_UPDATED, report.remediation
    assert repo.head() == target
    stop = fake.calls.index(("/venv/bin/aq", "stop", "--keep-sessions"))
    start = fake.calls.index(("/venv/bin/aq", "start", "--no-dashboard"))
    assert stop < start
    # Nothing it did not need: no dependency change, no dashboard change.
    assert not [call for call in fake.calls if "pip" in call]
    assert fake.builds == 0
    assert not (fake.state_dir / "update.lock").exists()


def test_a_shallow_installer_clone_is_updated(tmp_path, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo = Repo(tmp_path, shallow=True)
    fake = FakeHost(tmp_path / "aq")
    target = repo.push("README.md", "AQ 2\n")

    plan = plan_update(repo.checkout)
    report = apply_update(plan, fake.host())

    assert plan.shallow
    assert report.outcome == OUTCOME_UPDATED, report.remediation
    assert repo.head() == target


def test_a_daemon_that_was_not_running_is_not_started(repo, monkeypatch, tmp_path):
    _no_worker_scope(monkeypatch)
    fake = FakeHost(tmp_path / "aq", running=False)
    repo.push("README.md", "AQ 2\n")

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_UPDATED
    assert fake.calls == []


def test_changed_dependencies_are_reinstalled_from_inside_the_checkout(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("pyproject.toml", "[project]\nname = 'agent-queue'\nversion = '2'\n")

    apply_update(plan_update(repo.checkout), fake.host())

    pips = [call for call in fake.calls if "pip" in call]
    assert pips[0][-1] == "./packages/aq-client"
    assert pips[1][-1] == ".[cli]"
    assert fake.pip_cwds == [str(repo.checkout)] * 2


def test_a_built_dashboard_is_rebuilt_when_its_inputs_changed(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    bundle_directory(repo.checkout).mkdir(parents=True)  # the installer built one
    repo.push("dashboard/src/app.ts", "export const x = 2\n")

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_UPDATED
    assert fake.builds == 1


def test_a_checkout_without_a_built_dashboard_is_not_given_one(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("dashboard/src/app.ts", "export const x = 2\n")

    apply_update(plan_update(repo.checkout), fake.host())

    assert fake.builds == 0


def test_a_failed_start_rolls_back_and_starts_the_previous_version(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    before = repo.head()
    repo.push("README.md", "AQ 2\n")
    fake.start_results = [False, True]  # the new code fails, the old code starts

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_ROLLED_BACK
    assert report.exit_code == 20
    assert repo.head() == before
    assert fake.running is True
    assert "boot failed" in report.remediation


def test_a_failed_dependency_install_rolls_back_before_the_daemon_starts(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    before = repo.head()
    repo.push("pyproject.toml", "[project]\nname = 'agent-queue'\nversion = '2'\n")
    fake.fail_pip = True

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome in (OUTCOME_ROLLED_BACK, OUTCOME_FAILED)
    assert repo.head() == before


def test_migrations_are_backed_up_before_the_daemon_stops(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("migrations/versions/a0002.py", "revision = 'a0002'\n")
    order: list[str] = []

    def backup(config_path, destination, *, execute, pg_dump):
        order.append(f"backup running={fake.running}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"dump")
        return True, str(destination)

    monkeypatch.setattr(update_module, "backup_database", backup)

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert order == ["backup running=True"]
    assert report.outcome == OUTCOME_UPDATED
    assert report.backup is not None and report.backup.exists()


def test_a_failed_backup_changes_nothing(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    before = repo.head()
    repo.push("migrations/versions/a0002.py", "revision = 'a0002'\n")
    monkeypatch.setattr(
        update_module, "backup_database", lambda *a, **k: (False, "pg_dump was not found")
    )

    with pytest.raises(UpdateRefused, match="backup failed"):
        apply_update(plan_update(repo.checkout), fake.host())

    assert repo.head() == before
    assert fake.running is True and fake.calls == []
    assert not (fake.state_dir / "update.lock").exists()


def test_no_backup_skips_it(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("migrations/versions/a0002.py", "revision = 'a0002'\n")
    monkeypatch.setattr(
        update_module,
        "backup_database",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("backup ran")),
    )

    report = apply_update(plan_update(repo.checkout), fake.host(), backup=False)

    assert report.outcome == OUTCOME_UPDATED


def test_a_failed_start_after_migrations_is_not_rolled_back(repo, fake, monkeypatch):
    """The new daemon may have migrated; old code must not meet the newer schema."""
    _no_worker_scope(monkeypatch)
    target = repo.push("migrations/versions/a0002.py", "revision = 'a0002'\n")
    fake.start_results = [False]
    monkeypatch.setattr(
        update_module,
        "backup_database",
        lambda config_path, destination, **k: (True, str(destination)),
    )

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_FAILED
    assert repo.head() == target
    assert "migrations may already have run" in report.remediation
    assert "backup" in report.remediation


def test_a_second_update_is_refused_while_one_holds_the_lock(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("README.md", "AQ 2\n")
    (fake.state_dir / "update.lock").write_text("123\n", encoding="utf-8")

    with pytest.raises(UpdateRefused, match="in progress"):
        apply_update(plan_update(repo.checkout), fake.host())
    assert fake.calls == []


# -- the hand-off to the new code --------------------------------------------


def test_the_pulled_code_is_finished_by_a_fresh_process_from_the_new_checkout(
    repo, fake, monkeypatch
):
    """The updater's own memory is the old code; only the pull runs there."""
    _no_worker_scope(monkeypatch)
    bundle_directory(repo.checkout).mkdir(parents=True)
    target = repo.push("pyproject.toml", "[project]\nname = 'agent-queue'\nversion = '2'\n")

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_UPDATED, report.remediation
    [(command, cwd)] = fake.finishes
    assert command[:3] == ("/venv/bin/python", *FINISH)
    assert cwd == str(repo.checkout)  # `-m` resolves `src` from the working directory
    assert fake.heads_at_finish == [target]  # started only once the code had moved
    # What the finisher did is reported as the update's own steps.
    assert [name for name, ok, _ in report.steps if ok] == [
        "Stop the daemon",
        "Update the code",
        "Reinstall Python dependencies",
        "Rebuild the dashboard",
        "Start the daemon",
        "Start the dashboard server",
    ]


def test_the_old_process_runs_no_post_pull_step_itself(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    bundle_directory(repo.checkout).mkdir(parents=True)
    repo.push("pyproject.toml", "[project]\nname = 'agent-queue'\nversion = '2'\n")
    host = fake.host()
    seen: list[tuple[str, ...]] = []

    def execute(argv, **kwargs):
        command = tuple(str(part) for part in argv)
        if command[1:3] == FINISH:
            return CommandOutput(argv=command, returncode=0)
        seen.append(command)
        return fake.execute(argv, **kwargs)

    report = apply_update(plan_update(repo.checkout), replace(host, execute=execute))

    assert report.outcome == OUTCOME_UPDATED
    assert not [call for call in seen if "pip" in call or call[1:2] == ("start",)]
    assert fake.builds == 0


def test_a_module_that_disappears_with_the_pull_rolls_the_update_back(repo, fake, monkeypatch):
    """An error nobody planned for must not leave the daemon stopped on moved code."""
    _no_worker_scope(monkeypatch)
    bundle_directory(repo.checkout).mkdir(parents=True)
    before = repo.head()
    target = repo.push("dashboard/src/app.ts", "export const x = 2\n")

    def current(root, fingerprint):
        if repo.head() == target:
            raise ModuleNotFoundError("No module named 'src.dashboard_assets.runtime'")
        return True

    monkeypatch.setattr(update_module, "bundle_is_current", current)

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_ROLLED_BACK, report.remediation
    assert repo.head() == before
    assert fake.running is True
    assert "src.dashboard_assets.runtime" in report.remediation
    assert not (fake.state_dir / "update.lock").exists()


def test_a_new_daemon_that_came_up_is_stopped_before_the_code_moves_back(repo, fake, monkeypatch):
    """`aq start` leaves a running daemon alone, so the rollback has to stop it first."""
    _no_worker_scope(monkeypatch)
    bundle_directory(repo.checkout).mkdir(parents=True)
    monkeypatch.setattr(update_module, "bundle_is_current", lambda root, fingerprint: True)
    before = repo.head()
    repo.push("README.md", "AQ 2\n")
    fake.dashboard_start_results = [False]  # the new code's dashboard server never comes up
    heads_at_stop: list[str] = []
    execute = fake.execute

    def watching(argv, **kwargs):
        if tuple(argv)[1:] == ("stop", "--keep-sessions"):
            heads_at_stop.append(repo.head())
        return execute(argv, **kwargs)

    fake.execute = watching

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_ROLLED_BACK, report.remediation
    assert [call[1] for call in fake.calls] == ["stop", "start", "stop", "start"]
    assert heads_at_stop[1] != before  # stopped while still on the new code
    assert repo.head() == before and fake.running is True


def _watch(fake: FakeHost, repo: Repo, *tails: tuple[str, ...]) -> list[tuple[tuple[str, ...], str]]:
    """Record, for each command in *tails*, the commit the checkout was on when it ran."""
    seen: list[tuple[tuple[str, ...], str]] = []
    execute = fake.execute

    def watching(argv, **kwargs):
        tail = tuple(str(part) for part in argv)[1:]
        if tail in tails:
            seen.append((tail, repo.head()))
        return execute(argv, **kwargs)

    fake.execute = watching
    return seen


def test_the_dashboard_server_is_validated_and_the_daemon_never_probed_for_pages(
    repo, fake, monkeypatch
):
    """Spec §6.2: /health on the daemon, /__aq/health and GET / on the dashboard server."""
    _no_worker_scope(monkeypatch)
    bundle_directory(repo.checkout).mkdir(parents=True)
    monkeypatch.setattr(update_module, "bundle_is_current", lambda root, fingerprint: True)
    repo.push("README.md", "AQ 2\n")

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_UPDATED, report.remediation
    assert ("Start the dashboard server", True, DASHBOARD_URL) in report.steps
    assert f"{DASHBOARD_URL}__aq/health" in fake.probed and DASHBOARD_URL in fake.probed
    assert not [url for url in fake.probed if "/dashboard" in url]


def test_a_running_dashboard_server_is_stopped_before_the_code_moves(repo, tmp_path, monkeypatch):
    _no_worker_scope(monkeypatch)
    fake = FakeHost(tmp_path / "aq", dashboard_running=True)
    bundle_directory(repo.checkout).mkdir(parents=True)
    monkeypatch.setattr(update_module, "bundle_is_current", lambda root, fingerprint: True)
    before = repo.head()
    target = repo.push("README.md", "AQ 2\n")
    seen = _watch(fake, repo, ("dashboard", "stop"), ("stop", "--keep-sessions"),
                  ("start", "--no-dashboard"))

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_UPDATED, report.remediation
    assert seen == [
        (("dashboard", "stop"), before),
        (("stop", "--keep-sessions"), before),
        (("start", "--no-dashboard"), target),
    ]
    assert fake.dashboard_running and fake.running
    assert [name for name, ok, _ in report.steps if ok][:2] == [
        "Stop the dashboard server", "Stop the daemon",
    ]


def test_a_dashboard_server_that_will_not_stop_changes_nothing(repo, tmp_path, monkeypatch):
    _no_worker_scope(monkeypatch)
    fake = FakeHost(tmp_path / "aq", dashboard_running=True)
    fake.dashboard_stops = False
    before = repo.head()
    repo.push("README.md", "AQ 2\n")

    with pytest.raises(UpdateRefused, match="dashboard server did not stop"):
        apply_update(plan_update(repo.checkout), fake.host())

    assert repo.head() == before
    assert fake.running, "the daemon was not stopped"
    assert not fake.ran("stop", "--keep-sessions")


def test_a_dashboard_server_running_alone_is_restarted_on_the_new_code(repo, tmp_path, monkeypatch):
    _no_worker_scope(monkeypatch)
    fake = FakeHost(tmp_path / "aq", running=False, dashboard_running=True)
    bundle_directory(repo.checkout).mkdir(parents=True)
    monkeypatch.setattr(update_module, "bundle_is_current", lambda root, fingerprint: True)
    before = repo.head()
    target = repo.push("README.md", "AQ 2\n")
    seen = _watch(fake, repo, ("dashboard", "stop"), ("dashboard", "start"))

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_UPDATED, report.remediation
    [(command, _cwd)] = fake.finishes
    assert "--start-dashboard-server" in command and "--start-daemon" not in command
    assert seen == [(("dashboard", "stop"), before), (("dashboard", "start"), target)]
    assert fake.dashboard_running and not fake.running


def test_a_rollback_stops_the_new_dashboard_server_before_the_code_moves_back(
    repo, fake, monkeypatch
):
    _no_worker_scope(monkeypatch)
    bundle_directory(repo.checkout).mkdir(parents=True)
    monkeypatch.setattr(update_module, "bundle_is_current", lambda root, fingerprint: True)
    before = repo.head()
    target = repo.push("README.md", "AQ 2\n")
    # The new code's dashboard server comes up but serves no verified bundle.
    fake.dashboard_verified = [False]
    seen = _watch(fake, repo, ("dashboard", "stop"), ("stop", "--keep-sessions"),
                  ("start", "--no-dashboard"))

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_ROLLED_BACK, report.remediation
    assert "reports no verified bundle" in report.remediation
    assert seen == [
        (("stop", "--keep-sessions"), before),
        (("start", "--no-dashboard"), target),
        (("dashboard", "stop"), target),  # still on the code it runs
        (("stop", "--keep-sessions"), target),
        (("start", "--no-dashboard"), before),
    ]
    assert repo.head() == before and fake.running and fake.dashboard_running


def test_a_rollback_does_not_demand_a_dashboard_server_that_was_not_running(
    repo, fake, monkeypatch
):
    """Before the update it was not up (say, its port was taken); restoring that is success."""
    _no_worker_scope(monkeypatch)
    bundle_directory(repo.checkout).mkdir(parents=True)
    monkeypatch.setattr(update_module, "bundle_is_current", lambda root, fingerprint: True)
    before = repo.head()
    repo.push("README.md", "AQ 2\n")
    fake.dashboard_start_results = [False, False]

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_ROLLED_BACK, report.remediation
    assert "nothing answers as the dashboard server" in report.remediation
    assert repo.head() == before and fake.running and not fake.dashboard_running


def test_the_plan_says_the_dashboard_server_is_restarted():
    from src.install.update import describe

    plan = UpdatePlan(
        checkout=Path("/aq"), branch="main", upstream="origin/main",
        current="a" * 40, target="b" * 40, shallow=False,
    )

    lines = describe(plan, backup=True, daemon_running=True, dashboard_server_running=True)

    assert "Stops the dashboard server for the update and starts it again" in lines


def test_an_unexpected_error_in_the_old_process_after_the_pull_rolls_back(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    before = repo.head()
    repo.push("README.md", "AQ 2\n")

    def broken(*args, **kwargs):
        raise RuntimeError("the hand-off blew up")

    monkeypatch.setattr(update_module, "_finish_on_new_code", broken)

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_ROLLED_BACK, report.remediation
    assert repo.head() == before
    assert fake.running is True
    assert "the hand-off blew up" in report.remediation


def test_an_unexpected_error_while_rolling_back_is_reported_not_raised(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    bundle_directory(repo.checkout).mkdir(parents=True)
    before = repo.head()
    repo.push("dashboard/src/app.ts", "export const x = 2\n")

    def current(root, fingerprint):
        raise ModuleNotFoundError("No module named 'src.dashboard_assets.runtime'")

    monkeypatch.setattr(update_module, "bundle_is_current", current)

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_FAILED
    assert repo.head() == before  # the code did move back
    assert "aq start" in report.remediation
    assert "src.dashboard_assets.runtime" in report.remediation


def test_a_finisher_that_dies_without_a_word_rolls_back(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    before = repo.head()
    repo.push("README.md", "AQ 2\n")
    host = fake.host()

    def execute(argv, **kwargs):
        command = tuple(str(part) for part in argv)
        if command[1:3] == FINISH:
            return CommandOutput(
                argv=command,
                returncode=1,
                stdout="not json\n[1, 2]\n",
                stderr="Traceback (most recent call last):\nImportError: cannot import name 'x'\n",
            )
        return fake.execute(argv, **kwargs)

    report = apply_update(plan_update(repo.checkout), replace(host, execute=execute))

    assert report.outcome == OUTCOME_ROLLED_BACK, report.remediation
    assert repo.head() == before
    assert fake.running is True
    assert "cannot import name 'x'" in report.remediation


def test_a_finisher_killed_after_it_began_starting_a_migrated_daemon_is_not_rolled_back(
    repo, fake, monkeypatch
):
    """Its last word was that the new daemon was starting: the schema may have moved."""
    _no_worker_scope(monkeypatch)
    target = repo.push("migrations/versions/a0002.py", "revision = 'a0002'\n")
    monkeypatch.setattr(
        update_module,
        "backup_database",
        lambda config_path, destination, **k: (True, str(destination)),
    )
    host = fake.host()

    def execute(argv, **kwargs):
        command = tuple(str(part) for part in argv)
        if command[1:3] == FINISH:
            return CommandOutput(
                argv=command,
                stdout=json.dumps({"aq_update": "daemon_starting"}) + "\n",
                error="python did not finish within 10s",
            )
        return fake.execute(argv, **kwargs)

    report = apply_update(plan_update(repo.checkout), replace(host, execute=execute))

    assert report.outcome == OUTCOME_FAILED
    assert repo.head() == target
    assert "migrations may already have run" in report.remediation


def test_events_a_newer_finisher_adds_are_ignored_by_an_older_updater(repo, fake, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo.push("README.md", "AQ 2\n")
    host = fake.host()

    def execute(argv, **kwargs):
        command = tuple(str(part) for part in argv)
        if command[1:3] == FINISH:
            lines = [
                {"aq_update": "something_new", "detail": 1},
                {"aq_update": "step", "name": "Start the daemon", "ok": True, "extra": "x"},
                {"unrelated": True},
            ]
            return CommandOutput(
                argv=command,
                returncode=0,
                stdout="npm said something\n" + "\n".join(json.dumps(line) for line in lines),
            )
        return fake.execute(argv, **kwargs)

    report = apply_update(plan_update(repo.checkout), replace(host, execute=execute))

    assert report.outcome == OUTCOME_UPDATED
    assert ("Start the daemon", True, "") in report.steps


# -- the finisher as the real subprocess it is --------------------------------

STUB_FINISHER = """\
import json
import sys

print(json.dumps({"aq_update": "step", "name": "Ran the new checkout's finisher", "ok": True,
                  "message": " ".join(sys.argv[1:])}))
"""


def _with_a_finisher(repo: Repo) -> None:
    """Give the checkout a `src` package of its own, as a real one has.

    Without it `-m src.install.update_finish` would fall through to the
    agent-queue this test suite is installed from.
    """
    repo.push("src/__init__.py", "")
    repo.push("src/install/__init__.py", "")
    repo.push("src/install/update_finish.py", STUB_FINISHER)
    repo.pull()


def test_the_finisher_that_runs_is_the_pulled_checkouts_own(tmp_path, monkeypatch):
    _no_worker_scope(monkeypatch)
    repo = Repo(tmp_path)
    fake = FakeHost(tmp_path / "aq", real_finisher=True)
    _with_a_finisher(repo)
    target = repo.push(
        "src/install/update_finish.py", STUB_FINISHER.replace("Ran the new", "Ran the newer")
    )

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_UPDATED, report.remediation
    [ran] = [step for step in report.steps if step[0].startswith("Ran the")]
    assert ran[0] == "Ran the newer checkout's finisher"
    assert f"--target {target}" in ran[2] and f"--checkout {repo.checkout}" in ran[2]


def test_a_pull_that_deletes_the_finisher_module_rolls_back(tmp_path, monkeypatch):
    """The real thing: the module the old process counts on is gone from the new code."""
    _no_worker_scope(monkeypatch)
    repo = Repo(tmp_path)
    fake = FakeHost(tmp_path / "aq", real_finisher=True)
    _with_a_finisher(repo)
    before = repo.head()
    repo.remove("src/install/update_finish.py")

    report = apply_update(plan_update(repo.checkout), fake.host())

    assert report.outcome == OUTCOME_ROLLED_BACK, report.remediation
    assert repo.head() == before
    assert fake.running is True
    assert "update_finish" in report.remediation
    assert (repo.checkout / "src/install/update_finish.py").exists()


def test_the_finisher_needs_nothing_the_new_code_has_not_installed_yet(tmp_path):
    """It runs before `pip install`: standard library and `src.install` only."""
    root = Path(update_finish.__file__).resolve().parents[2]

    output = run_command([sys.executable, "-S", *FINISH, "--help"], cwd=str(root), timeout=60)

    assert output.ok, output.stderr
    assert "--checkout" in output.stdout


def test_the_finisher_accepts_arguments_a_newer_updater_does_not_know(tmp_path):
    out = io.StringIO()

    code = update_finish.main(
        [
            "--checkout",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "aq"),
            "--previous",
            "a" * 40,
            "--target",
            "b" * 40,
            "--from-the-future",
            "1",
        ],
        host_factory=lambda **fields: replace(
            Host(**fields), execute=lambda argv, **k: CommandOutput(argv=tuple(argv), returncode=0)
        ),
        out=out,
    )

    assert code == 0, out.getvalue()


def test_a_timed_out_command_keeps_what_it_had_printed():
    output = run_command(
        [sys.executable, "-c", "import time; print('so far', flush=True); time.sleep(30)"],
        timeout=1.5,
    )

    assert not output.ok and "did not finish" in (output.error or "")
    assert output.stdout.strip() == "so far"


# -- pieces -----------------------------------------------------------------


def test_the_backup_password_never_reaches_the_command_line(tmp_path, monkeypatch):
    import src.config

    monkeypatch.setattr(
        src.config,
        "load_config",
        lambda path: SimpleNamespace(
            database=SimpleNamespace(url="postgresql+asyncpg://aq:s3cret@db.local:6543/agent_queue")
        ),
    )
    seen = {}

    def execute(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs["env"]
        Path(argv[argv.index("--file") + 1]).write_bytes(b"dump")
        return CommandOutput(argv=tuple(argv), returncode=0)

    destination = tmp_path / "backups" / "b.dump"
    ok, where = backup_database(
        tmp_path / "config.yaml", destination, execute=execute, pg_dump="/usr/bin/pg_dump"
    )

    assert ok and where == str(destination)
    assert "s3cret" not in " ".join(seen["argv"])
    assert seen["env"]["PGPASSWORD"] == "s3cret"
    assert seen["argv"][seen["argv"].index("--port") + 1] == "6543"
    assert oct(destination.stat().st_mode & 0o777) == "0o600"


def test_the_reinstall_keeps_the_extras_this_environment_has():
    present = {"pytest", "anthropic"}.__contains__
    assert installed_extras(present) == ["cli", "dev", "llm"]
    assert installed_extras(lambda name: False) == ["cli"]


def test_update_is_a_top_level_command():
    import click

    from src.cli.app import cli

    assert "update" in cli.list_commands(click.Context(cli))
