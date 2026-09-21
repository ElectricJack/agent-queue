"""``aq install`` — the WSL2 platform adapter (``src/install/wsl.py``).

The adapter exists because a fresh Ubuntu 24.04 under WSL2 has no tmux, and AQ
runs every agent harness inside one: the install used to stop at ``prereq.tmux``
and tell the operator to run apt themselves, which is the kind of "extra step"
the install epoch exists to delete.  Nothing here needs apt, root or a real WSL
distribution — the adapter's only host access is a command runner and a
``which``.
"""

from __future__ import annotations

from pathlib import Path

from src.install.command import CommandOutput
from src.install.platform import PlatformFacts, SupportVerdict
from src.install.results import StepState
from src.install.steps import StepContext
from src.install.wsl import APT_PREREQUISITES, STEP_PACKAGES, packages_step

WSL2 = SupportVerdict(
    host_path="windows-wsl2",
    tier="supported",
    facts=PlatformFacts(
        system="linux",
        release="6.6.0-microsoft-standard-WSL2",
        machine="x86_64",
        arch="x86_64",
        python_version="3.12.3",
        distro_id="ubuntu",
        distro_version="24.04",
        wsl=True,
        wsl_version=2,
    ),
)


class FakeApt:
    """A machine with a set of executables and an apt that installs into it."""

    def __init__(self, *, present: tuple[str, ...] = (), sudo: bool = True) -> None:
        self.present = set(present)
        self.sudo = sudo
        self.commands: list[tuple[str, ...]] = []
        self.install_fails = False

    def which(self, command: str) -> str | None:
        return f"/usr/bin/{command}" if command in self.present else None

    def run(self, argv, *, env=None, stdin=None, timeout=None) -> CommandOutput:
        argv = tuple(argv)
        self.commands.append(argv)
        if argv[:3] == ("sudo", "-n", "true"):
            return CommandOutput(argv, 0 if self.sudo else 1, "", "sudo: a password is required")
        if "apt-get" in argv and "update" in argv:
            return CommandOutput(argv, 0, "Reading package lists...", "")
        if "apt-get" in argv and "install" in argv:
            if self.install_fails:
                return CommandOutput(argv, 100, "", "E: Could not get lock /var/lib/dpkg/lock")
            index = argv.index("-y")
            self.present.update(argv[index + 1 :])
            return CommandOutput(argv, 0, "Setting up tmux", "")
        raise AssertionError(f"unexpected command: {argv}")


def _context(tmp_path: Path) -> StepContext:
    return StepContext(support=WSL2, options={}, dry_run=False, interactive=False)


def _run(host: FakeApt, tmp_path: Path):
    step = packages_step(which=host.which, runner=host.run)
    return step, step.run(_context(tmp_path))


def test_a_fresh_distribution_gets_the_prerequisites_installed(tmp_path):
    host = FakeApt(present=("sudo", "apt-get", "python3"))

    step, result = _run(host, tmp_path)

    assert result.state is StepState.SUCCEEDED
    assert result.detail["installed"] == ["git", "tmux"]
    assert ("sudo", "apt-get", "update") in host.commands
    assert ("sudo", "apt-get", "install", "-y", "git", "tmux") in host.commands
    # The very checks that used to fail are now satisfied by this step.
    assert step.verify(_context(tmp_path)) is True


def test_only_what_is_missing_is_installed(tmp_path):
    host = FakeApt(present=("sudo", "apt-get", "git"))

    _step, result = _run(host, tmp_path)

    assert result.detail["installed"] == ["tmux"]
    assert ("sudo", "apt-get", "install", "-y", "tmux") in host.commands


def test_a_distribution_that_has_everything_installs_nothing(tmp_path):
    host = FakeApt(present=("sudo", "apt-get", "git", "tmux"))

    step, result = _run(host, tmp_path)

    assert result.state is StepState.SUCCEEDED
    assert result.detail["installed"] == []
    assert host.commands == [], "an idempotent rerun does not call apt at all"
    assert not result.resources, "nothing was installed, so nothing is owned"
    assert step.verify(_context(tmp_path)) is True


def test_what_apt_installed_is_recorded_as_owned(tmp_path):
    host = FakeApt(present=("sudo", "apt-get"))

    _step, result = _run(host, tmp_path)

    assert {(resource.kind, resource.id, resource.owned) for resource in result.resources} == {
        ("apt-package", "git", True),
        ("apt-package", "tmux", True),
    }


def test_a_password_prompt_is_a_needs_user_checkpoint_not_a_prompt(tmp_path):
    """The installer never types a password; it says which command to run."""
    host = FakeApt(present=("sudo", "apt-get"), sudo=False)

    _step, result = _run(host, tmp_path)

    assert result.state is StepState.NEEDS_USER
    assert "sudo -v" in result.remediation
    assert "sudo apt-get install -y git tmux" in result.remediation
    assert not any("install" in " ".join(argv) for argv in host.commands)


def test_an_apt_failure_names_the_command_to_run_by_hand(tmp_path):
    host = FakeApt(present=("sudo", "apt-get"))
    host.install_fails = True

    _step, result = _run(host, tmp_path)

    assert result.state is StepState.FAILED
    assert "Could not get lock" in result.summary
    assert "sudo apt-get install -y git tmux" in result.remediation


def test_apt_reporting_success_without_the_command_is_still_a_failure(tmp_path):
    """apt can exit 0 and leave nothing on PATH (a held or virtual package)."""

    class Liar(FakeApt):
        def run(self, argv, *, env=None, stdin=None, timeout=None) -> CommandOutput:
            argv = tuple(argv)
            if "install" in argv:
                self.commands.append(argv)
                return CommandOutput(argv, 0, "Setting up tmux", "")
            return super().run(argv, env=env, stdin=stdin, timeout=timeout)

    _step, result = _run(Liar(present=("sudo", "apt-get")), tmp_path)

    assert result.state is StepState.FAILED
    assert "still not on PATH" in result.summary


def test_the_step_is_mutating_consent_gated_and_owned_by_the_adapter(tmp_path):
    step = packages_step(which=FakeApt().which, runner=FakeApt().run)

    assert step.id == STEP_PACKAGES
    assert step.mutating is True
    assert step.consent_prompt
    assert step.owner == "wsl"
    assert step.advisory is False, "a missing tmux really does block the install"
    assert [package for package, _ in APT_PREREQUISITES] == ["git", "tmux"]
