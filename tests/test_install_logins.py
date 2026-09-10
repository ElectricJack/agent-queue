"""Provider login checkpoints: installed is not authenticated, and no secret escapes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.install.engine import InstallEngine, InstallOptions
from src.install.logins import (
    CLAUDE_CODE_LOGIN,
    CODEX_LOGIN,
    GEMINI_LOGIN,
    AuthProbe,
    CredentialStore,
    EnvironmentCredential,
    ProviderLogin,
    login_step,
    login_steps,
    probe_all,
    probe_login,
    provider_logins,
)
from src.install.platform import TIER_SUPPORTED, PlatformFacts, SupportVerdict
from src.install.prerequisites import default_registry
from src.install.providers import CODEX, ProviderInstaller
from src.install.redaction import assert_secret_free
from src.install.results import InstallOutcome, StepResult, StepState, exit_code
from src.install.state import load_state
from src.install.steps import StepContext, StepRegistry, StepSpec

#: A value shaped exactly like the real thing, so a leak is unmistakable.
FAKE_KEY = "sk-ant-notarealkey-000111222333444555666777888999"


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


def _context(*, interactive: bool = True, dry_run: bool = False) -> StepContext:
    return StepContext(
        support=_support(),
        options={},
        dry_run=dry_run,
        interactive=interactive,
    )


def _env(tmp_path, **extra) -> dict[str, str]:
    """An environment with a private HOME and no inherited provider credentials."""
    return {"HOME": str(tmp_path), **extra}


def _present(*names):
    return lambda command: f"/opt/bin/{command}" if command in names else None


def _never_signed_in(command):
    return _completed(command, code=1)


# -- the registry mirrors current official documentation --------------------


def test_each_supported_provider_has_a_login_adapter_with_documented_commands():
    assert provider_logins() == (CLAUDE_CODE_LOGIN, CODEX_LOGIN, GEMINI_LOGIN)

    assert CLAUDE_CODE_LOGIN.login_command == "claude auth login"
    assert CLAUDE_CODE_LOGIN.status_command == ("claude", "auth", "status")
    assert [entry.variable for entry in CLAUDE_CODE_LOGIN.environment] == [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ]

    assert CODEX_LOGIN.login_command == "codex login"
    assert CODEX_LOGIN.status_command == ("codex", "login", "status")
    assert [entry.variable for entry in CODEX_LOGIN.environment] == ["OPENAI_API_KEY"]
    assert "--device-auth" in CODEX_LOGIN.headless_hint

    # Gemini documents no non-interactive status subcommand: the environment
    # and the OAuth cache are the only readiness evidence there.
    assert GEMINI_LOGIN.status_command is None
    assert [entry.variable for entry in GEMINI_LOGIN.environment] == [
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ]


def test_credential_stores_resolve_to_the_documented_paths(tmp_path):
    claude = CLAUDE_CODE_LOGIN.stores[0]
    assert claude.resolve(_env(tmp_path)) == tmp_path / ".claude" / ".credentials.json"
    # The provider's own directory override moves the store with it.
    assert (
        claude.resolve(_env(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "elsewhere")))
        == tmp_path / "elsewhere" / ".credentials.json"
    )

    assert CODEX_LOGIN.stores[0].resolve(_env(tmp_path)) == tmp_path / ".codex" / "auth.json"
    assert (
        CODEX_LOGIN.stores[0].resolve(_env(tmp_path, CODEX_HOME=str(tmp_path / "codex-home")))
        == tmp_path / "codex-home" / "auth.json"
    )
    assert (
        GEMINI_LOGIN.stores[0].resolve(_env(tmp_path)) == tmp_path / ".gemini" / "oauth_creds.json"
    )


# -- readiness probes -------------------------------------------------------


def test_provider_status_command_decides_readiness_without_capturing_its_output(tmp_path):
    calls = []

    def runner(command):
        calls.append(command)
        # Real `codex login status` prints the signed-in account; none of it
        # may reach a result.
        return _completed(command, stdout=f"Signed in as person@example.com key={FAKE_KEY}")

    probe = probe_login(CODEX_LOGIN, environ=_env(tmp_path), which=_present("codex"), runner=runner)

    assert calls == [("codex", "login", "status")]
    assert probe.installed and probe.authenticated
    assert probe.method == "provider-login"
    assert probe.source == "codex login status"
    assert FAKE_KEY not in json.dumps(probe.detail())
    assert "person@example.com" not in json.dumps(probe.detail())


def test_environment_credential_reports_only_the_variable_name(tmp_path):
    probe = probe_login(
        CLAUDE_CODE_LOGIN,
        environ=_env(tmp_path, ANTHROPIC_API_KEY=FAKE_KEY),
        which=_present("claude"),
        runner=_never_signed_in,
    )

    assert probe.authenticated
    assert probe.method == "api-key"
    assert probe.source == "ANTHROPIC_API_KEY"
    assert probe.store == "environment"
    assert FAKE_KEY not in json.dumps(probe.detail())


def test_credential_store_presence_authenticates_without_reading_the_file(tmp_path):
    cache = tmp_path / ".gemini" / "oauth_creds.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"refresh_token": FAKE_KEY}), encoding="utf-8")

    probe = probe_login(GEMINI_LOGIN, environ=_env(tmp_path), which=_present("gemini"))

    assert probe.authenticated
    assert probe.method == "provider-login"
    assert probe.source == "Gemini CLI OAuth cache"
    assert FAKE_KEY not in json.dumps(probe.detail())


def test_environment_credential_outranks_the_store_but_a_switch_set_to_false_does_not(tmp_path):
    cache = tmp_path / ".gemini" / "oauth_creds.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{}", encoding="utf-8")

    off = probe_login(
        GEMINI_LOGIN,
        environ=_env(tmp_path, GOOGLE_GENAI_USE_VERTEXAI="false"),
        which=_present("gemini"),
    )
    assert off.source == "Gemini CLI OAuth cache"

    on = probe_login(
        GEMINI_LOGIN,
        environ=_env(
            tmp_path,
            GOOGLE_GENAI_USE_VERTEXAI="true",
            GOOGLE_CLOUD_PROJECT="demo",
            GOOGLE_CLOUD_LOCATION="us-central1",
        ),
        which=_present("gemini"),
    )
    assert on.method == "vertex"
    assert on.source == "GOOGLE_GENAI_USE_VERTEXAI"


def test_a_half_configured_method_names_the_variables_it_still_needs(tmp_path):
    probe = probe_login(
        GEMINI_LOGIN,
        environ=_env(tmp_path, GOOGLE_GENAI_USE_VERTEXAI="1", GOOGLE_CLOUD_PROJECT="demo"),
        which=_present("gemini"),
    )

    assert not probe.authenticated
    assert probe.missing_environment == ("GOOGLE_CLOUD_LOCATION",)


@pytest.mark.parametrize(
    "failure",
    [
        OSError("no such executable"),
        subprocess.TimeoutExpired(cmd=("claude", "auth", "status"), timeout=10),
    ],
)
def test_a_broken_or_hung_status_command_is_cannot_tell_not_authenticated(tmp_path, failure):
    def runner(command):
        raise failure

    probe = probe_login(
        CLAUDE_CODE_LOGIN,
        environ=_env(tmp_path, ANTHROPIC_API_KEY=FAKE_KEY),
        which=_present("claude"),
        runner=runner,
    )

    # It falls through to the next evidence rather than crashing the install.
    assert probe.authenticated
    assert probe.source == "ANTHROPIC_API_KEY"


def test_probe_all_reports_every_provider_independently(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")

    probes = {
        probe.provider_id: probe
        for probe in probe_all(
            environ=_env(tmp_path),
            which=_present("claude", "codex"),
            runner=_never_signed_in,
        )
    }

    assert probes["claude"] == AuthProbe(
        "claude",
        installed=True,
        authenticated=False,
        checked=(
            "claude auth status",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "Claude Code credential file",
        ),
    )
    assert probes["codex"].authenticated is True
    assert probes["gemini"].installed is False


# -- the step: checkpoints, retries and headless instructions ---------------


def test_an_authenticated_provider_succeeds_and_records_the_method(tmp_path):
    step = login_step(
        CODEX_LOGIN,
        environ=_env(tmp_path, OPENAI_API_KEY=FAKE_KEY),
        which=_present("codex"),
        runner=_never_signed_in,
    )
    result = step.run(_context())

    assert result.state is StepState.SUCCEEDED
    assert result.detail["installed"] is True
    assert result.detail["authenticated"] is True
    assert result.detail["auth_method"] == "api-key"
    assert result.detail["credential_source"] == "OPENAI_API_KEY"
    assert FAKE_KEY not in json.dumps(result.to_dict())
    # A credential is never an installer-owned resource: AQ must not offer to
    # remove it on uninstall.
    assert result.resources == ()


def test_an_installed_but_unauthenticated_provider_stops_at_a_human_checkpoint(tmp_path):
    step = login_step(
        CLAUDE_CODE_LOGIN,
        environ=_env(tmp_path),
        which=_present("claude"),
        runner=_never_signed_in,
    )
    result = step.run(_context(interactive=True))

    assert result.state is StepState.NEEDS_USER
    assert result.detail == {
        "installed": True,
        "authenticated": False,
        "auth_method": None,
        "credential_source": None,
        "credential_store": None,
        "checked": [
            "claude auth status",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "Claude Code credential file",
        ],
        "missing_environment": [],
    }
    assert "claude auth login" in result.remediation
    assert "rerun `aq install --with provider.claude`" in result.remediation
    assert "never types a password" in result.remediation
    assert CLAUDE_CODE_LOGIN.docs_url in result.remediation
    # Retry is the recovery path, so the state must stay retryable.
    assert result.retryable is True


def test_an_unattended_run_gets_the_headless_contract_not_a_browser(tmp_path):
    step = login_step(
        GEMINI_LOGIN,
        environ=_env(tmp_path),
        which=_present("gemini"),
    )
    result = step.run(_context(interactive=False))

    assert result.state is StepState.NEEDS_USER
    assert "may not open a browser or read a terminal" in result.remediation
    assert "GEMINI_API_KEY" in result.remediation
    assert "GOOGLE_GENAI_USE_VERTEXAI" in result.remediation


def test_the_headless_route_for_each_provider_names_a_supported_credential(tmp_path):
    for login in provider_logins():
        step = login_step(
            login,
            environ=_env(tmp_path),
            which=_present(login.executable),
            runner=_never_signed_in,
        )
        remediation = step.run(_context(interactive=False)).remediation
        assert login.headless_hint in remediation
        assert login.login_command in remediation


def test_a_missing_executable_is_an_install_failure_not_an_auth_failure(tmp_path):
    step = login_step(CODEX_LOGIN, environ=_env(tmp_path), which=lambda command: None)
    result = step.run(_context())

    assert result.state is StepState.FAILED
    assert result.detail["installed"] is False
    assert result.detail["authenticated"] is False
    assert "--restart-from provider.codex-cli" in result.remediation


def test_the_login_step_is_read_only_and_verifies_by_reprobing(tmp_path):
    store = tmp_path / ".codex" / "auth.json"
    step = login_step(
        CODEX_LOGIN, environ=_env(tmp_path), which=_present("codex"), runner=_never_signed_in
    )

    assert step.mutating is False
    assert step.capability == "provider.codex"
    assert step.depends_on == ("provider.codex-cli",)
    assert step.verify(_context()) is False

    store.parent.mkdir(parents=True)
    store.write_text("{}", encoding="utf-8")
    assert step.verify(_context()) is True


# -- registry wiring --------------------------------------------------------


def test_default_registry_orders_each_login_after_its_cli_under_one_capability():
    registry = default_registry(which=lambda command: None)
    order = [step.id for step in registry.ordered()]

    for provider in ("claude", "codex", "gemini"):
        cli = f"provider.{provider}-cli"
        login = f"provider.{provider}-login"
        assert order.index(cli) < order.index(login)
        assert registry.get(login).capability == registry.get(cli).capability
    # Selecting the capability selects both halves; the login is invalidated
    # with the CLI it depends on.
    assert "provider.claude-login" in registry.dependents_of("provider.claude-cli")


def test_login_adapters_without_an_installer_are_dropped_rather_than_breaking_the_plan():
    orphan = ProviderLogin(
        provider_id="orphan",
        title="Orphan CLI",
        executable="orphan",
        login_command="orphan login",
        status_command=None,
        environment=(),
        stores=(),
        headless_hint="Set ORPHAN_API_KEY.",
        docs_url="https://example.invalid/auth",
    )
    steps = login_steps(additional=(orphan,), installers=(CODEX,))
    assert [step.id for step in steps] == ["provider.codex-login"]

    paired = login_steps(
        additional=(orphan,),
        installers=(
            CODEX,
            ProviderInstaller(
                id="orphan",
                title="Orphan CLI",
                executable="orphan",
                install_command=("true",),
                install_hint="Install it.",
            ),
        ),
    )
    assert [step.id for step in paired] == ["provider.codex-login", "provider.orphan-login"]


def test_duplicate_login_ids_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        provider_logins(
            (
                ProviderLogin(
                    provider_id="codex",
                    title="Other Codex",
                    executable="codex",
                    login_command="codex login",
                    status_command=None,
                    environment=(),
                    stores=(),
                    headless_hint="",
                    docs_url="",
                ),
            )
        )


# -- end to end through the engine ------------------------------------------


def _registry(login_kwargs) -> StepRegistry:
    host = StepSpec(
        id="host.supported",
        title="host",
        run=lambda context: StepResult.succeeded("host.supported", "supported"),
    )
    cli = StepSpec(
        id="provider.codex-cli",
        title="Install or reuse Codex CLI",
        run=lambda context: StepResult.succeeded("provider.codex-cli", "reusing codex"),
        depends_on=("host.supported",),
        capability="provider.codex",
    )
    return StepRegistry((host, cli, login_step(CODEX_LOGIN, **login_kwargs)))


def test_a_selected_unauthenticated_provider_ends_the_run_in_needs_user(tmp_path):
    state_path = tmp_path / "state.json"
    registry = _registry(
        {
            "environ": _env(tmp_path),
            "which": _present("codex"),
            "runner": _never_signed_in,
        }
    )
    result = InstallEngine(
        registry,
        InstallOptions(capabilities=frozenset({"provider.codex"}), state_path=state_path),
        support=_support(),
    ).run()

    assert result.outcome is InstallOutcome.NEEDS_USER
    assert result.exit_code == exit_code(InstallOutcome.NEEDS_USER)
    assert result.blocking_step.step_id == "provider.codex-login"
    assert "codex login" in result.next_action

    # The checkpoint survives a restart: the record says the CLI is installed
    # and the login is still owed.
    state = load_state(state_path)
    assert state.record_for("provider.codex-cli").state is StepState.SUCCEEDED
    assert state.record_for("provider.codex-login").state is StepState.NEEDS_USER


def test_a_rerun_after_the_human_logs_in_reaches_ready(tmp_path):
    state_path = tmp_path / "state.json"
    signed_in = False

    def runner(command):
        return _completed(command, code=0 if signed_in else 1)

    registry_kwargs = {
        "environ": _env(tmp_path),
        "which": _present("codex"),
        "runner": runner,
    }
    options = InstallOptions(capabilities=frozenset({"provider.codex"}), state_path=state_path)
    first = InstallEngine(_registry(registry_kwargs), options, support=_support()).run()
    assert first.outcome is InstallOutcome.NEEDS_USER

    signed_in = True  # the human ran `codex login` in their own terminal
    second = InstallEngine(_registry(registry_kwargs), options, support=_support()).run()

    assert second.outcome is InstallOutcome.READY
    login = next(row for row in second.steps if row.step_id == "provider.codex-login")
    assert login.state is StepState.SUCCEEDED


def test_an_unselected_provider_login_is_skipped_and_never_blocks_the_install(tmp_path):
    result = InstallEngine(
        _registry({"environ": _env(tmp_path), "which": _present("codex")}),
        InstallOptions(state_path=tmp_path / "state.json"),
        support=_support(),
    ).run()

    assert result.outcome is InstallOutcome.READY
    login = next(row for row in result.steps if row.step_id == "provider.codex-login")
    assert login.state is StepState.SKIPPED
    assert "not selected" in login.summary


def test_no_credential_reaches_the_resume_record_or_the_json_result(tmp_path):
    state_path = tmp_path / "state.json"
    environ = _env(
        tmp_path,
        OPENAI_API_KEY=FAKE_KEY,
        ANTHROPIC_API_KEY=FAKE_KEY,
        GEMINI_API_KEY=FAKE_KEY,
    )
    result = InstallEngine(
        _registry(
            {
                "environ": environ,
                "which": _present("codex"),
                # The status command says "not signed in", so the environment
                # credential is what authenticates — and it is the name of the
                # variable, never its value, that may be recorded.
                "runner": _never_signed_in,
            }
        ),
        InstallOptions(capabilities=frozenset({"provider.codex"}), state_path=state_path),
        support=_support(),
    ).run()

    assert result.outcome is InstallOutcome.READY
    payload = json.dumps(result.to_dict())
    assert FAKE_KEY not in payload
    assert "OPENAI_API_KEY" in payload  # the name is reported; the value is not

    written = state_path.read_text(encoding="utf-8")
    assert FAKE_KEY not in written
    # The record's own secret fence still passes over everything the login
    # step contributed.
    assert_secret_free(json.loads(written), context="install state")


def test_a_hostile_environment_variable_value_cannot_ride_into_the_record(tmp_path):
    """A credential-shaped value under a login-owned key is still refused."""
    probe = AuthProbe(
        "codex",
        installed=True,
        authenticated=True,
        method="api-key",
        source=FAKE_KEY,  # an adapter that reported material instead of a name
    )
    with pytest.raises(Exception, match="secret-shaped"):
        assert_secret_free(probe.detail(), context="probe detail")


def test_environment_credential_metadata_is_names_and_descriptions_only():
    for login in provider_logins():
        for credential in login.environment:
            assert credential.variable.isupper()
            assert credential.method
            assert credential.description
        for store in login.stores:
            assert isinstance(store, CredentialStore)
            assert store.label


def test_an_environment_credential_switch_requires_a_truthy_value():
    switch = EnvironmentCredential("X_ENABLE", "vertex", "switch", switch=True)
    assert switch.selected({"X_ENABLE": "true"}) is True
    assert switch.selected({"X_ENABLE": "0"}) is False
    assert switch.selected({"X_ENABLE": "  "}) is False

    plain = EnvironmentCredential("X_KEY_NAME", "api-key", "plain")
    assert plain.selected({"X_KEY_NAME": "false"}) is True


# -- the published contract -------------------------------------------------

DOC = Path(__file__).resolve().parent.parent / "docs" / "reference" / "cli" / "install.md"


def test_the_published_authentication_table_matches_the_registry():
    """The doc is what a human follows at a checkpoint; it must not drift."""
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## Provider authentication", 1)[1].split("## Exit codes", 1)[0]
    assert "provider.<name>-login" in section

    for login in provider_logins():
        assert f"`{login.capability}`" in section
        if login.status_command:
            assert f"`{' '.join(login.status_command)}`" in section
        else:
            assert "none documented" in section
        for store in login.stores:
            assert store.filename in section
        documented = [entry.variable for entry in login.environment if entry.variable in section]
        assert documented, f"{login.provider_id} documents no headless credential"
