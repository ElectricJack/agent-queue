"""The macOS installation bootstrap: adapter steps and host composition.

Every case is built from injected readers — a fake command runner, a fake
``which``, temporary Homebrew prefixes and a temporary ``HOME`` — because the
acceptance criteria are about Macs and the suite runs on Linux.  What the tests
assert is what the criteria say: a clean Mac and a developer's Mac reach the
same place, and a missing tool, a permission problem or an architecture
mismatch produces something the user can act on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.install.command import CommandOutput
from src.install.engine import InstallEngine, InstallOptions
from src.install.macos import (
    ARM_PREFIX,
    HOMEBREW_INSTALL_COMMAND,
    INTEL_PREFIX,
    STEP_ARCH,
    STEP_DEVELOPER_TOOLS,
    STEP_HOMEBREW,
    STEP_PACKAGES,
    STEP_PYTHON_RUNTIME,
    STEP_SERVICES,
    STEP_SHELL_PATH,
    architecture_step,
    brew_aware_which,
    developer_tools_step,
    find_brew,
    homebrew_step,
    launch_services_step,
    packages_step,
    python_runtime_step,
    shell_path_step,
    shellenv_line,
)
from src.install.platform import (
    HOST_MACOS_ARM,
    HOST_MACOS_INTEL,
    HOST_WSL2,
    TIER_COMPATIBILITY,
    TIER_SUPPORTED,
    TIER_UNSUPPORTED,
    PlatformFacts,
    SupportVerdict,
)
from src.install.prerequisites import STEP_DATA_DIR, STEP_GIT, STEP_HOST, STEP_PYTHON, STEP_TMUX
from src.install.registry import build_registry
from src.install.results import InstallOutcome, StepState
from src.install.steps import StepContext


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer runs before a database exists."""


# -- fakes -------------------------------------------------------------------


@dataclass
class FakeRunner:
    """Answers commands from a table keyed by an argv prefix, and remembers."""

    responses: dict[tuple[str, ...], CommandOutput] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, argv, **_: object) -> CommandOutput:
        command = tuple(str(part) for part in argv)
        self.calls.append(command)
        for length in range(len(command), 0, -1):
            if command[:length] in self.responses:
                return self.responses[command[:length]]
        return CommandOutput(argv=command, error=f"{command[0]} is not installed")

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == tuple(prefix) for call in self.calls)

    def count(self, *prefix: str) -> int:
        return sum(1 for call in self.calls if call[: len(prefix)] == tuple(prefix))


def ok(*argv: str, stdout: str = "") -> tuple[tuple[str, ...], CommandOutput]:
    return tuple(argv), CommandOutput(argv=tuple(argv), returncode=0, stdout=stdout)


def fail(*argv: str, stderr: str = "", code: int = 1) -> tuple[tuple[str, ...], CommandOutput]:
    return tuple(argv), CommandOutput(argv=tuple(argv), returncode=code, stderr=stderr)


def which_from(paths: dict[str, str]):
    return lambda command: paths.get(command)


def mac_facts(arch: str = "arm64", **overrides) -> PlatformFacts:
    base = {
        "system": "darwin",
        "release": "23.5.0",
        "machine": arch,
        "arch": arch,
        "python_version": "3.12.4",
        "macos_version": "14.5",
    }
    base.update(overrides)
    return PlatformFacts(**base)


def mac_support(arch: str = "arm64") -> SupportVerdict:
    return SupportVerdict(
        host_path=HOST_MACOS_ARM if arch == "arm64" else HOST_MACOS_INTEL,
        tier=TIER_SUPPORTED if arch == "arm64" else TIER_COMPATIBILITY,
        facts=mac_facts(arch),
    )


def context(arch: str = "arm64", **kwargs) -> StepContext:
    resources = kwargs.pop("resources", {})
    return StepContext(
        support=mac_support(arch),
        options=kwargs.pop("options", {}),
        dry_run=kwargs.pop("dry_run", False),
        interactive=kwargs.pop("interactive", False),
        resources={record.key: record for record in resources},
        completed=kwargs.pop("completed", {}),
    )


@pytest.fixture
def brew_home(tmp_path):
    """A temporary pair of Homebrew prefixes plus a HOME to write profiles in."""

    @dataclass
    class Fixture:
        arm: Path
        intel: Path
        home: Path

        @property
        def prefixes(self) -> dict[str, Path]:
            return {"arm64": self.arm, "x86_64": self.intel}

        def install_brew(self, prefix: Path) -> Path:
            binary = prefix / "bin" / "brew"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            return binary

    home = tmp_path / "home"
    home.mkdir()
    return Fixture(arm=tmp_path / "opt-homebrew", intel=tmp_path / "usr-local", home=home)


# -- architecture ------------------------------------------------------------


NATIVE_ARM = dict(
    [
        ok("sysctl", "-n", "sysctl.proc_translated", stdout="0\n"),
        ok("sysctl", "-n", "hw.optional.arm64", stdout="1\n"),
    ]
)
NATIVE_INTEL = dict(
    [
        fail("sysctl", "-n", "sysctl.proc_translated", stderr="unknown oid"),
        fail("sysctl", "-n", "hw.optional.arm64", stderr="unknown oid"),
    ]
)
TRANSLATED = dict(
    [
        ok("sysctl", "-n", "sysctl.proc_translated", stdout="1\n"),
        ok("sysctl", "-n", "hw.optional.arm64", stdout="1\n"),
    ]
)


def test_a_native_apple_silicon_shell_is_accepted():
    step = architecture_step(runner=FakeRunner(NATIVE_ARM))
    result = step.run(context("arm64"))
    assert result.state is StepState.SUCCEEDED
    assert "arm64" in result.summary
    assert result.detail["apple_silicon_hardware"] is True


def test_a_native_intel_mac_is_accepted_and_named_as_the_compatibility_tier():
    step = architecture_step(runner=FakeRunner(NATIVE_INTEL))
    result = step.run(context("x86_64"))
    assert result.state is StepState.SUCCEEDED
    assert "Intel" in result.summary
    assert "compatibility" in result.summary


def test_a_rosetta_translated_shell_fails_with_the_command_that_opens_a_native_one():
    step = architecture_step(runner=FakeRunner(TRANSLATED))
    result = step.run(context("x86_64"))
    assert result.state is StepState.FAILED
    assert "Rosetta" in result.summary
    assert "arch -arm64" in (result.remediation or "")
    assert result.detail["proc_translated"] is True


def test_an_x86_shell_on_apple_silicon_hardware_fails_even_without_the_translation_flag():
    """`uname -m` reports x86_64 in an x86 shell, which would mis-tier the Mac."""
    runner = FakeRunner(
        dict(
            [
                ok("sysctl", "-n", "sysctl.proc_translated", stdout="0\n"),
                ok("sysctl", "-n", "hw.optional.arm64", stdout="1\n"),
            ]
        )
    )
    result = architecture_step(runner=runner).run(context("x86_64"))
    assert result.state is StepState.FAILED
    assert result.detail["apple_silicon_hardware"] is True


def test_the_architecture_verifier_agrees_with_the_step():
    step = architecture_step(runner=FakeRunner(TRANSLATED))
    assert step.verify(context("x86_64")) is False
    assert architecture_step(runner=FakeRunner(NATIVE_ARM)).verify(context("arm64")) is True


# -- Command Line Tools ------------------------------------------------------


def test_installed_command_line_tools_are_reported_as_a_reused_resource(tmp_path):
    developer_dir = tmp_path / "CommandLineTools"
    developer_dir.mkdir()
    runner = FakeRunner(dict([ok("xcode-select", "-p", stdout=f"{developer_dir}\n")]))
    result = developer_tools_step(runner=runner).run(context())
    assert result.state is StepState.SUCCEEDED
    resource = result.resources[0]
    assert (resource.kind, resource.id, resource.owned) == (
        "developer-tools",
        "command-line-tools",
        False,
    )


def test_missing_command_line_tools_stop_at_a_human_checkpoint():
    runner = FakeRunner(dict([fail("xcode-select", "-p", stderr="error: unable to get path")]))
    result = developer_tools_step(runner=runner).run(context())
    assert result.state is StepState.NEEDS_USER
    assert "xcode-select --install" in (result.remediation or "")


def test_a_developer_directory_that_no_longer_exists_is_named_with_its_repair(tmp_path):
    missing = tmp_path / "gone"
    runner = FakeRunner(dict([ok("xcode-select", "-p", stdout=f"{missing}\n")]))
    result = developer_tools_step(runner=runner).run(context())
    assert result.state is StepState.NEEDS_USER
    assert "xcode-select --reset" in (result.remediation or "")
    assert str(missing) in result.summary


# -- Homebrew discovery ------------------------------------------------------


def test_find_brew_prefers_the_exported_prefix_then_path_then_the_default(brew_home):
    exported = brew_home.install_brew(brew_home.arm)
    assert (
        find_brew(
            arch="arm64",
            environ={"HOMEBREW_PREFIX": str(brew_home.arm)},
            which=which_from({}),
            prefixes=brew_home.prefixes,
        )
        == exported
    )
    assert find_brew(
        arch="arm64",
        environ={},
        which=which_from({"brew": "/somewhere/else/bin/brew"}),
        prefixes=brew_home.prefixes,
    ) == Path("/somewhere/else/bin/brew")
    assert (
        find_brew(arch="arm64", environ={}, which=which_from({}), prefixes=brew_home.prefixes)
        == exported
    )


def test_find_brew_finds_the_intel_prefix_from_an_arm_host_and_returns_none_when_absent(brew_home):
    intel = brew_home.install_brew(brew_home.intel)
    assert (
        find_brew(arch="arm64", environ={}, which=which_from({}), prefixes=brew_home.prefixes)
        == intel
    )
    empty = {"arm64": brew_home.arm, "x86_64": brew_home.arm}
    assert find_brew(arch="arm64", environ={}, which=which_from({}), prefixes=empty) is None


def test_the_documented_default_prefixes_are_the_ones_homebrew_publishes():
    assert ARM_PREFIX == Path("/opt/homebrew")
    assert INTEL_PREFIX == Path("/usr/local")


def _homebrew(brew_home, *, arch="arm64", environ=None, extra=None):
    responses = {}
    if extra:
        responses.update(extra)
    runner = FakeRunner(responses)
    step = homebrew_step(
        environ=environ or {},
        which=which_from({}),
        runner=runner,
        prefixes=brew_home.prefixes,
    )
    return step, runner


def test_homebrew_is_reported_at_the_prefix_it_actually_lives_in(brew_home):
    binary = brew_home.install_brew(brew_home.arm)
    step, _ = _homebrew(
        brew_home,
        extra=dict(
            [
                ok(str(binary), "--prefix", stdout=f"{brew_home.arm}\n"),
                ok(str(binary), "--version", stdout="Homebrew 4.3.9\n"),
            ]
        ),
    )
    result = step.run(context("arm64"))
    assert result.state is StepState.SUCCEEDED
    assert result.detail["prefix"] == str(brew_home.arm)
    assert result.detail["version"] == "Homebrew 4.3.9"
    resource = result.resources[0]
    assert (resource.kind, resource.id, resource.owned, resource.reused) == (
        "package-manager",
        "homebrew",
        False,
        True,
    )


def test_a_mac_without_homebrew_gets_the_official_install_command(brew_home):
    step, _ = _homebrew(brew_home)
    result = step.run(context("arm64"))
    assert result.state is StepState.NEEDS_USER
    assert HOMEBREW_INSTALL_COMMAND in (result.remediation or "")
    assert "administrator password" in (result.remediation or "")
    assert result.detail["expected_prefix"] == str(brew_home.arm)


def test_an_intel_homebrew_on_apple_silicon_is_an_actionable_architecture_mismatch(brew_home):
    binary = brew_home.install_brew(brew_home.intel)
    step, _ = _homebrew(
        brew_home, extra=dict([ok(str(binary), "--prefix", stdout=f"{brew_home.intel}\n")])
    )
    result = step.run(context("arm64"))
    assert result.state is StepState.FAILED
    assert "Intel build" in result.summary
    assert HOMEBREW_INSTALL_COMMAND in (result.remediation or "")


def test_an_intel_homebrew_on_an_intel_mac_is_fine(brew_home):
    binary = brew_home.install_brew(brew_home.intel)
    step, _ = _homebrew(
        brew_home, extra=dict([ok(str(binary), "--prefix", stdout=f"{brew_home.intel}\n")])
    )
    result = step.run(context("x86_64"))
    assert result.state is StepState.SUCCEEDED


def test_a_prefix_this_user_cannot_write_reports_homebrews_own_chown_repair(brew_home):
    binary = brew_home.install_brew(brew_home.arm)
    step, _ = _homebrew(
        brew_home, extra=dict([ok(str(binary), "--prefix", stdout=f"{brew_home.arm}\n")])
    )
    (brew_home.arm / "bin").chmod(0o500)
    try:
        result = step.run(context("arm64"))
    finally:
        (brew_home.arm / "bin").chmod(0o755)
    if os.geteuid() == 0:  # pragma: no cover - root ignores the mode bits
        pytest.skip("root can write any directory")
    assert result.state is StepState.FAILED
    assert "not writable" in result.summary
    assert "chown -R" in (result.remediation or "")


def test_a_non_default_prefix_installs_but_says_what_it_costs(brew_home, tmp_path):
    custom = tmp_path / "custom-brew"
    binary = custom / "bin" / "brew"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    step, _ = _homebrew(
        brew_home,
        environ={"HOMEBREW_PREFIX": str(custom)},
        extra=dict([ok(str(binary), "--prefix", stdout=f"{custom}\n")]),
    )
    result = step.run(context("arm64"))
    assert result.state is StepState.SUCCEEDED
    assert "non-default prefix" in result.summary


def test_a_broken_brew_prefix_probe_falls_back_to_the_documented_layout(brew_home):
    binary = brew_home.install_brew(brew_home.arm)
    step, _ = _homebrew(brew_home, extra=dict([fail(str(binary), "--prefix", stderr="boom")]))
    result = step.run(context("arm64"))
    assert result.state is StepState.SUCCEEDED
    assert result.detail["prefix"] == str(brew_home.arm)


# -- shell PATH --------------------------------------------------------------


def _shell_path(brew_home, *, shell="zsh", path="", resources=()):
    step = shell_path_step(
        environ={"SHELL": f"/bin/{shell}", "PATH": path, "HOME": str(brew_home.home)},
        home=brew_home.home,
        prefixes=brew_home.prefixes,
        which=which_from({}),
    )
    return step


def _homebrew_resource(prefix: Path):
    from src.install.results import ResourceRecord

    return ResourceRecord(
        kind="package-manager",
        id="homebrew",
        owned=False,
        reused=True,
        detail={"prefix": str(prefix), "brew": str(prefix / "bin" / "brew")},
    )


def test_the_shellenv_line_is_added_once_to_the_zsh_login_file(brew_home):
    step = _shell_path(brew_home)
    ctx = context("arm64", resources=[_homebrew_resource(brew_home.arm)])
    result = step.run(ctx)
    profile = brew_home.home / ".zprofile"
    assert result.state is StepState.SUCCEEDED
    assert shellenv_line(brew_home.arm) in profile.read_text(encoding="utf-8")
    assert result.resources[0].kind == "shell-profile"

    # A rerun sees its own marker and writes nothing more.
    assert step.verify(ctx) is True
    again = step.run(ctx)
    assert again.state is StepState.SUCCEEDED
    assert profile.read_text(encoding="utf-8").count("brew shellenv") == 1
    assert not again.resources


def test_an_existing_manual_shellenv_line_is_left_alone(brew_home):
    profile = brew_home.home / ".zprofile"
    profile.write_text('eval "$(/opt/homebrew/bin/brew shellenv)"\n', encoding="utf-8")
    step = _shell_path(brew_home)
    result = step.run(context("arm64", resources=[_homebrew_resource(brew_home.arm)]))
    assert result.state is StepState.SUCCEEDED
    assert "already" in result.summary
    assert profile.read_text(encoding="utf-8").count("brew shellenv") == 1


def test_bash_gets_its_own_login_file(brew_home):
    step = _shell_path(brew_home, shell="bash")
    step.run(context("arm64", resources=[_homebrew_resource(brew_home.arm)]))
    assert (brew_home.home / ".bash_profile").exists()
    assert not (brew_home.home / ".zprofile").exists()


def test_an_unrecognised_shell_is_told_what_to_add_rather_than_guessed_at(brew_home):
    step = _shell_path(brew_home, shell="fish")
    result = step.run(context("arm64", resources=[_homebrew_resource(brew_home.arm)]))
    assert result.state is StepState.NEEDS_USER
    assert "brew shellenv" in (result.remediation or "")
    assert not list(brew_home.home.iterdir())


def test_a_prefix_already_on_path_satisfies_the_verifier_without_an_edit(brew_home):
    step = _shell_path(brew_home, path=str(brew_home.arm / "bin"))
    assert step.verify(context("arm64", resources=[_homebrew_resource(brew_home.arm)])) is True


def test_the_shell_step_is_mutating_and_consent_gated(brew_home):
    step = _shell_path(brew_home)
    assert step.mutating is True
    assert step.consent_prompt


# -- Homebrew packages -------------------------------------------------------


def test_nothing_is_installed_when_the_prerequisites_are_already_present():
    runner = FakeRunner({})
    step = packages_step(which=which_from({"tmux": "/usr/bin/tmux", "git": "/usr/bin/git"}))
    result = step.run(context())
    assert result.state is StepState.SUCCEEDED
    assert result.detail["installed"] == []
    assert runner.calls == []
    assert step.verify(context()) is True


def test_a_missing_prerequisite_is_installed_with_brew_and_recorded_as_owned(brew_home):
    binary = brew_home.install_brew(brew_home.arm)
    runner = FakeRunner(dict([ok(str(binary), "install", "tmux", stdout="pouring tmux\n")]))
    step = packages_step(which=which_from({"git": "/usr/bin/git"}), runner=runner)
    result = step.run(context("arm64", resources=[_homebrew_resource(brew_home.arm)]))
    assert result.state is StepState.SUCCEEDED
    assert result.detail["installed"] == ["tmux"]
    assert runner.ran(str(binary), "install", "tmux")
    assert not runner.ran(str(binary), "install", "git")
    formula = result.resources[0]
    assert (formula.kind, formula.id, formula.owned) == ("brew-formula", "tmux", True)


def test_a_failed_brew_install_names_the_formula_and_the_command_to_rerun(brew_home):
    binary = brew_home.install_brew(brew_home.arm)
    runner = FakeRunner(
        dict([fail(str(binary), "install", "tmux", stderr="Error: no bottle available\n")])
    )
    step = packages_step(which=which_from({"git": "/usr/bin/git"}), runner=runner)
    result = step.run(context("arm64", resources=[_homebrew_resource(brew_home.arm)]))
    assert result.state is StepState.FAILED
    assert "no bottle available" in result.summary
    assert "brew install tmux" in (result.remediation or "")


def test_installing_without_homebrew_fails_at_the_homebrew_step_not_silently():
    step = packages_step(which=which_from({}), runner=FakeRunner({}))
    result = step.run(context())
    assert result.state is StepState.FAILED
    assert "Homebrew is required" in result.summary
    assert sorted(result.detail["missing"]) == ["git", "tmux"]


def test_brew_aware_which_finds_what_this_run_just_installed(brew_home):
    binary = brew_home.arm / "bin" / "tmux"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    lookup = brew_aware_which(which_from({}), [brew_home.arm, brew_home.intel])
    assert lookup("tmux") == str(binary)
    assert lookup("nothing-here") is None


# -- launchd -----------------------------------------------------------------


def test_a_reachable_launchd_domain_succeeds():
    runner = FakeRunner(dict([ok("launchctl", "print", "gui/501")]))
    result = launch_services_step(runner=runner, uid=501).run(context())
    assert result.state is StepState.SUCCEEDED
    assert result.detail["domain"] == "gui/501"


def test_an_unreachable_launchd_domain_is_skipped_with_the_reason_preserved():
    runner = FakeRunner(dict([fail("launchctl", "print", "gui/501", stderr="Bad request")]))
    result = launch_services_step(runner=runner, uid=501).run(context())
    assert result.state is StepState.SKIPPED
    assert "will not start at login" in result.summary
    assert "desktop session" in result.summary


# -- Python ------------------------------------------------------------------


def test_the_macos_system_python_is_refused_with_a_supported_alternative():
    step = python_runtime_step(executable="/usr/bin/python3")
    result = step.run(context())
    assert result.state is StepState.FAILED
    assert result.retryable is False
    assert "brew install python@3.12" in (result.remediation or "")
    assert step.verify(context()) is False


def test_a_virtualenv_built_on_the_system_python_is_recognised_for_what_it_is(tmp_path):
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    base = tmp_path / "usr" / "bin" / "python3"
    base.parent.mkdir(parents=True)
    base.write_text("", encoding="utf-8")
    interpreter = venv / "python"
    interpreter.symlink_to(base)
    # The resolved base is what decides it; the temporary tree stands in for
    # /usr/bin so the case is provable off a Mac.
    step = python_runtime_step(
        executable=str(interpreter), system_prefixes=(str(tmp_path / "usr" / "bin"),)
    )
    result = step.run(context())
    assert result.state is StepState.FAILED
    assert "built on the macOS system Python" in result.summary
    assert result.detail["base_executable"] == str(base)


def test_a_homebrew_python_is_accepted(brew_home):
    interpreter = brew_home.arm / "bin" / "python3.12"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")
    step = python_runtime_step(executable=str(interpreter))
    assert step.run(context()).state is StepState.SUCCEEDED
    assert step.verify(context()) is True


# -- registry composition ----------------------------------------------------


def _ids(registry):
    return [step.id for step in registry.ordered()]


def test_a_mac_registry_provisions_before_the_engine_checks_the_prerequisites():
    registry = build_registry(mac_support("arm64"))
    ids = _ids(registry)
    assert ids[0] == STEP_HOST
    assert ids.index(STEP_PACKAGES) < ids.index(STEP_TMUX)
    assert ids.index(STEP_PACKAGES) < ids.index(STEP_GIT)
    assert ids.index(STEP_PYTHON_RUNTIME) < ids.index(STEP_PYTHON)
    assert registry.get(STEP_TMUX).depends_on == (STEP_HOST, STEP_PACKAGES)
    assert registry.get(STEP_PYTHON).depends_on == (STEP_HOST, STEP_PYTHON_RUNTIME)
    assert STEP_DATA_DIR in ids


def test_both_mac_architectures_get_the_same_steps():
    assert _ids(build_registry(mac_support("arm64"))) == _ids(build_registry(mac_support("x86_64")))


def test_a_non_mac_host_registers_no_mac_steps():
    wsl = SupportVerdict(
        host_path=HOST_WSL2,
        tier=TIER_SUPPORTED,
        facts=PlatformFacts(
            system="linux",
            release="6.6.0-microsoft-standard-WSL2",
            machine="x86_64",
            arch="x86_64",
            python_version="3.12.4",
            distro_id="ubuntu",
            distro_version="24.04",
            wsl=True,
            wsl_version=2,
        ),
    )
    ids = _ids(build_registry(wsl))
    assert not [step for step in ids if step.startswith("macos.")]
    assert ids == [STEP_HOST, STEP_PYTHON, STEP_GIT, STEP_TMUX, STEP_DATA_DIR]


def test_an_unsupported_host_still_composes_the_engines_own_steps():
    unsupported = SupportVerdict(
        host_path="unsupported",
        tier=TIER_UNSUPPORTED,
        facts=PlatformFacts(
            system="freebsd",
            release="14.0",
            machine="x86_64",
            arch="x86_64",
            python_version="3.12.4",
        ),
    )
    assert _ids(build_registry(unsupported)) == [
        STEP_HOST,
        STEP_PYTHON,
        STEP_GIT,
        STEP_TMUX,
        STEP_DATA_DIR,
    ]


def test_every_mac_step_declares_the_adapter_as_its_owner():
    registry = build_registry(mac_support("arm64"))
    for step in registry.ordered():
        if step.id.startswith("macos."):
            assert step.owner == "macos"


# -- documentation -----------------------------------------------------------


def test_the_published_reference_names_every_macos_step():
    """The step ids are the user-facing contract; the doc cannot drift from them."""
    doc = (
        Path(__file__).resolve().parent.parent / "docs" / "reference" / "cli" / "install.md"
    ).read_text(encoding="utf-8")
    for step in build_registry(mac_support("arm64")).ordered():
        if step.id.startswith("macos."):
            assert f"`{step.id}`" in doc, f"{step.id} is not documented"


# -- end to end --------------------------------------------------------------


def _mac_registry(brew_home, *, runner, which, arch="arm64", state_dir=None, executable=None):
    return build_registry(
        mac_support(arch),
        environ={
            "SHELL": "/bin/zsh",
            "HOME": str(brew_home.home),
            "PATH": "/usr/bin",
            "AQ_INSTALL_STATE_DIR": str(state_dir or brew_home.home / "aq"),
        },
        which=which,
        state_dir=state_dir or brew_home.home / "aq",
        runner=runner,
        prefixes=brew_home.prefixes,
        home=brew_home.home,
        executable=executable or "/opt/homebrew/opt/python@3.12/bin/python3.12",
        uid=501,
    )


def _clean_mac(brew_home, tmp_path, *, tmux: bool):
    """A Mac with Homebrew and the tools, with or without tmux installed."""
    binary = brew_home.install_brew(brew_home.arm)
    developer_dir = tmp_path / "CommandLineTools"
    developer_dir.mkdir(exist_ok=True)
    responses = dict(
        [
            ok("sysctl", "-n", "sysctl.proc_translated", stdout="0\n"),
            ok("sysctl", "-n", "hw.optional.arm64", stdout="1\n"),
            ok("xcode-select", "-p", stdout=f"{developer_dir}\n"),
            ok(str(binary), "--prefix", stdout=f"{brew_home.arm}\n"),
            ok(str(binary), "--version", stdout="Homebrew 4.3.9\n"),
            ok("launchctl", "print", "gui/501"),
        ]
    )
    installed = {"git": "/usr/bin/git"}
    if tmux:
        installed["tmux"] = "/usr/bin/tmux"

    def install_tmux(argv, **_):
        target = brew_home.arm / "bin" / "tmux"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o755)
        return CommandOutput(argv=tuple(argv), returncode=0, stdout="pouring tmux\n")

    runner = FakeRunner(responses)
    real_call = runner.__call__

    def call(argv, **kwargs):
        command = tuple(str(part) for part in argv)
        if command == (str(binary), "install", "tmux"):
            runner.calls.append(command)
            return install_tmux(command)
        return real_call(argv, **kwargs)

    return call, runner, which_from(installed)


def test_a_clean_mac_reaches_ready_and_records_what_it_owns(brew_home, tmp_path):
    call, runner, which = _clean_mac(brew_home, tmp_path, tmux=False)
    state_dir = tmp_path / "aq-home"
    registry = _mac_registry(brew_home, runner=call, which=which, state_dir=state_dir)
    options = InstallOptions(
        interactive=False,
        approve=frozenset({"*"}),
        state_path=state_dir / "install-state.json",
    )
    result = InstallEngine(registry, options, support=mac_support("arm64")).run()

    assert result.outcome is InstallOutcome.READY
    assert result.exit_code == 0
    states = {step.step_id: step.state for step in result.steps}
    assert states[STEP_ARCH] is StepState.SUCCEEDED
    assert states[STEP_DEVELOPER_TOOLS] is StepState.SUCCEEDED
    assert states[STEP_HOMEBREW] is StepState.SUCCEEDED
    assert states[STEP_SHELL_PATH] is StepState.SUCCEEDED
    assert states[STEP_PACKAGES] is StepState.SUCCEEDED
    assert states[STEP_TMUX] is StepState.SUCCEEDED
    kinds = {(resource.kind, resource.id) for resource in result.resources}
    assert ("brew-formula", "tmux") in kinds
    assert ("package-manager", "homebrew") in kinds
    assert (brew_home.home / ".zprofile").exists()
    assert runner.count(str(brew_home.arm / "bin" / "brew"), "install", "tmux") == 1


def test_an_existing_developer_mac_reaches_the_same_result_without_installing_anything(
    brew_home, tmp_path
):
    call, runner, which = _clean_mac(brew_home, tmp_path, tmux=True)
    state_dir = tmp_path / "aq-home"
    (brew_home.home / ".zprofile").write_text(f"{shellenv_line(brew_home.arm)}\n", encoding="utf-8")
    registry = _mac_registry(brew_home, runner=call, which=which, state_dir=state_dir)
    options = InstallOptions(
        interactive=False,
        approve=frozenset({"*"}),
        state_path=state_dir / "install-state.json",
    )
    result = InstallEngine(registry, options, support=mac_support("arm64")).run()

    assert result.outcome is InstallOutcome.READY
    assert not runner.ran(str(brew_home.arm / "bin" / "brew"), "install")
    assert (brew_home.home / ".zprofile").read_text(encoding="utf-8").count("brew shellenv") == 1


def test_rerunning_revalidates_instead_of_installing_again(brew_home, tmp_path):
    call, runner, which = _clean_mac(brew_home, tmp_path, tmux=False)
    state_dir = tmp_path / "aq-home"
    options = InstallOptions(
        interactive=False,
        approve=frozenset({"*"}),
        state_path=state_dir / "install-state.json",
    )
    first = InstallEngine(
        _mac_registry(brew_home, runner=call, which=which, state_dir=state_dir),
        options,
        support=mac_support("arm64"),
    ).run()
    assert first.outcome is InstallOutcome.READY

    # The second run sees tmux where the first put it: in the Homebrew prefix.
    second = InstallEngine(
        _mac_registry(brew_home, runner=call, which=which, state_dir=state_dir),
        options,
        support=mac_support("arm64"),
    ).run()
    assert second.outcome is InstallOutcome.READY
    assert runner.count(str(brew_home.arm / "bin" / "brew"), "install", "tmux") == 1
    packages = next(step for step in second.steps if step.step_id == STEP_PACKAGES)
    assert packages.detail.get("revalidated") is True
    ids = [(resource.kind, resource.id) for resource in second.resources]
    assert len(ids) == len(set(ids))


def test_a_mac_without_homebrew_stops_at_needs_user_and_changes_nothing(brew_home, tmp_path):
    developer_dir = tmp_path / "CommandLineTools"
    developer_dir.mkdir(exist_ok=True)
    runner = FakeRunner(
        dict(
            [
                ok("sysctl", "-n", "sysctl.proc_translated", stdout="0\n"),
                ok("sysctl", "-n", "hw.optional.arm64", stdout="1\n"),
                ok("xcode-select", "-p", stdout=f"{developer_dir}\n"),
            ]
        )
    )
    state_dir = tmp_path / "aq-home"
    registry = _mac_registry(
        brew_home, runner=runner, which=which_from({"git": "/usr/bin/git"}), state_dir=state_dir
    )
    options = InstallOptions(
        interactive=False,
        approve=frozenset({"*"}),
        state_path=state_dir / "install-state.json",
    )
    result = InstallEngine(registry, options, support=mac_support("arm64")).run()

    assert result.outcome is InstallOutcome.NEEDS_USER
    assert result.exit_code == 10
    assert result.blocking_step.step_id == STEP_HOMEBREW
    assert HOMEBREW_INSTALL_COMMAND in (result.next_action or "")
    assert not (brew_home.home / ".zprofile").exists()
    later = {step.step_id for step in result.steps if step.state is StepState.SKIPPED}
    assert {STEP_SHELL_PATH, STEP_PACKAGES, STEP_SERVICES} <= later


def test_a_dry_run_on_a_mac_reports_the_mutations_without_making_them(brew_home, tmp_path):
    call, _, which = _clean_mac(brew_home, tmp_path, tmux=False)
    state_dir = tmp_path / "aq-home"
    registry = _mac_registry(brew_home, runner=call, which=which, state_dir=state_dir)
    options = InstallOptions(
        interactive=False,
        dry_run=True,
        state_path=state_dir / "install-state.json",
    )
    result = InstallEngine(registry, options, support=mac_support("arm64")).run()

    actions = {row.step_id: row.action.value for row in result.plan}
    assert actions[STEP_SHELL_PATH] == "would_run"
    assert actions[STEP_PACKAGES] == "would_run"
    assert not (brew_home.home / ".zprofile").exists()
    assert result.state_path is None
    assert not (brew_home.arm / "bin" / "tmux").exists()


def test_the_resume_record_of_a_mac_install_carries_no_secret(brew_home, tmp_path):
    call, _, which = _clean_mac(brew_home, tmp_path, tmux=False)
    state_dir = tmp_path / "aq-home"
    state_path = state_dir / "install-state.json"
    registry = _mac_registry(brew_home, runner=call, which=which, state_dir=state_dir)
    options = InstallOptions(interactive=False, approve=frozenset({"*"}), state_path=state_path)
    InstallEngine(registry, options, support=mac_support("arm64")).run()

    from src.install.redaction import assert_secret_free
    from src.install.state import load_state

    state = load_state(state_path)
    assert state is not None
    assert_secret_free(state.to_dict(), context="macOS install resume record")
    assert state.platform["arch"] == "arm64"
