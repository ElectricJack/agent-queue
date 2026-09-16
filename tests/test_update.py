"""`aq update`: fetch, stop, fast-forward, rebuild, restart -- or roll back.

Git is real here: every test builds a bare "origin" and a clone of it, so the
refusals, the fast-forward and the rollback move real commits.  The daemon,
pip, pg_dump and the dashboard build are fakes that record what they were
asked to do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.install import update as update_module
from src.install.command import CommandOutput, run_command
from src.install.dashboard import BuildOutcome, bundle_directory
from src.install.update import (
    OUTCOME_FAILED,
    OUTCOME_ROLLED_BACK,
    OUTCOME_UPDATED,
    Host,
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

    def head(self) -> str:
        return _git(self.checkout, "rev-parse", "HEAD")


class FakeHost:
    """The daemon, pip, pg_dump and the dashboard build; git passes through."""

    def __init__(self, state_dir: Path, *, running: bool = True):
        self.state_dir = state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "config.yaml").write_text("messaging_platform: none\n", encoding="utf-8")
        self.running = running
        self.start_results: list[bool] = []
        self.fail_pip = False
        self.calls: list[tuple[str, ...]] = []
        self.pip_cwds: list[str | None] = []
        self.builds = 0

    def execute(self, argv, **kwargs) -> CommandOutput:
        command = tuple(str(part) for part in argv)
        if command[0] == "git":
            return run_command(command, **kwargs)
        self.calls.append(command)
        if command[1:] == ("stop", "--keep-sessions"):
            self.running = False
            return CommandOutput(argv=command, returncode=0)
        if command[1:] == ("start", "--no-dashboard"):
            ok = self.start_results.pop(0) if self.start_results else True
            self.running = ok
            return CommandOutput(
                argv=command, returncode=0 if ok else 1, stderr="" if ok else "boot failed"
            )
        if "pip" in command:
            self.pip_cwds.append(kwargs.get("cwd"))
            return CommandOutput(argv=command, returncode=1 if self.fail_pip else 0)
        raise AssertionError(f"unexpected command: {command}")

    def probe(self, url: str) -> int | None:
        return 200 if self.running else None

    def build(self, checkout, **kwargs) -> BuildOutcome:
        self.builds += 1
        return BuildOutcome(True, "built")

    def host(self) -> Host:
        return Host(
            execute=self.execute,
            probe=self.probe,
            python="/venv/bin/python",
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
