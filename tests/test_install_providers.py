"""Provider CLI adapters stay optional, idempotent, and credential-free."""

from __future__ import annotations

import subprocess

import pytest

from src.install.engine import InstallEngine, InstallOptions
from src.install.platform import PlatformFacts, SupportVerdict, TIER_SUPPORTED
from src.install.providers import (
    CLAUDE_CODE,
    CODEX,
    GEMINI,
    ProviderInstaller,
    provider_installers,
    provider_step,
    provider_steps,
)
from src.install.results import InstallOutcome, StepResult, StepState
from src.install.steps import StepRegistry, StepSpec


def _completed(command, code=0, stdout=""):
    return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")


def _support() -> SupportVerdict:
    facts = PlatformFacts(
        system="linux",
        release="6.6.0-microsoft-standard-WSL2",
        machine="x86_64",
        arch="x86_64",
        python_version="3.12.3",
        distro_id="ubuntu",
        distro_version="24.04",
        wsl=True,
        wsl_version=2,
    )
    return SupportVerdict(host_path="windows-wsl2", tier=TIER_SUPPORTED, facts=facts)


def test_builtin_provider_registry_uses_current_documented_install_commands():
    assert provider_installers() == (CLAUDE_CODE, CODEX, GEMINI)
    assert CLAUDE_CODE.install_command == (
        "bash",
        "-c",
        "curl -fsSL https://claude.ai/install.sh | bash",
    )
    assert CODEX.install_command == (
        "sh",
        "-c",
        "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    )
    assert GEMINI.install_command == ("npm", "install", "-g", "@google/gemini-cli")


def test_existing_provider_is_reused_with_its_path_and_version():
    calls = []

    def runner(command):
        calls.append(command)
        return _completed(command, stdout="codex 1.2.3\n")

    step = provider_step(CODEX, which=lambda command: "/opt/bin/codex", runner=runner)
    result = step.run(None)  # type: ignore[arg-type]

    assert result.state is StepState.SUCCEEDED
    assert result.detail == {"executable": "/opt/bin/codex", "version": "1.2.3"}
    assert result.resources[0].to_dict()["reused"] is True
    assert calls == [("codex", "--version")]


def test_selected_missing_provider_installs_then_verifies_the_executable():
    installed = False
    calls = []

    def which(command):
        return "/opt/bin/gemini" if installed and command == "gemini" else None

    def runner(command):
        nonlocal installed
        calls.append(command)
        if command == GEMINI.install_command:
            installed = True
            return _completed(command)
        return _completed(command, stdout="0.18.0\n")

    result = provider_step(GEMINI, which=which, runner=runner).run(None)  # type: ignore[arg-type]

    assert result.state is StepState.SUCCEEDED
    assert result.detail == {"executable": "/opt/bin/gemini", "version": "0.18.0"}
    assert result.resources[0].owned is True
    assert calls == [GEMINI.install_command, ("gemini", "--version")]


def test_probe_keeps_only_a_version_token_from_executable_output():
    result = provider_step(
        CODEX,
        which=lambda command: "/opt/bin/codex",
        runner=lambda command: _completed(command, stdout="OPENAI_API_KEY=secret 1.2.3\n"),
    ).run(None)  # type: ignore[arg-type]

    assert result.detail["version"] == "1.2.3"
    assert "secret" not in str(result.detail)


def test_install_success_without_a_working_path_fails_with_a_recovery_action():
    step = provider_step(
        CLAUDE_CODE,
        which=lambda command: None,
        runner=lambda command: _completed(command),
    )
    result = step.run(None)  # type: ignore[arg-type]

    assert result.state is StepState.FAILED
    assert "not executable on PATH" in result.summary
    assert "provider.claude" in (result.remediation or "")


def test_unselected_provider_steps_are_skipped_and_do_not_block_install(tmp_path):
    tmux = StepSpec(
        id="prereq.tmux",
        title="tmux",
        run=lambda context: StepResult.succeeded("prereq.tmux", "available"),
        depends_on=("host.supported",),
    )
    host = StepSpec(
        id="host.supported",
        title="host",
        run=lambda context: StepResult.succeeded("host.supported", "supported"),
    )
    registry = StepRegistry((host, tmux, provider_step(CODEX, which=lambda command: None)))
    result = InstallEngine(
        registry,
        InstallOptions(state_path=tmp_path / "state.json"),
        support=_support(),
    ).run()

    assert result.outcome is InstallOutcome.READY
    provider = next(row for row in result.steps if row.step_id == "provider.codex-cli")
    assert provider.state is StepState.SKIPPED
    assert "not selected" in provider.summary


def test_provider_registry_accepts_additional_adapters_and_rejects_duplicate_ids():
    extra = ProviderInstaller(
        id="test-provider",
        title="Test provider",
        executable="test-provider",
        install_command=("test-provider-install",),
        install_hint="Install test provider.",
    )
    assert provider_steps(additional=(extra,))[-1].capability == "provider.test-provider"
    with pytest.raises(ValueError, match="unique"):
        provider_installers(
            (
                ProviderInstaller(
                    id="claude",
                    title="Another Claude",
                    executable="other-claude",
                    install_command=("install",),
                    install_hint="Install it.",
                ),
            )
        )
