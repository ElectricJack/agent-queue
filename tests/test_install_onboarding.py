"""``aq install`` — the onboarding steps that finish the newcomer's path.

These are the steps between "this machine has the prerequisites" and "there is
a daemon answering and a URL to open": the configuration, the optional Discord
delivery, the daemon and the dashboard report.  They run through the real
engine over a scripted host — no daemon, no database, no network — because the
acceptance criteria are about the *run*, not about any one function:

* *A newcomer follows one documented entry point from prerequisites through a
  ready daemon/dashboard* — :func:`test_a_default_run_reaches_a_ready_daemon_and_a_dashboard_url`.
* *Skipping optional providers or Discord does not fail setup* —
  :func:`test_an_install_that_never_mentions_discord_is_ready`,
  :func:`test_skipping_every_provider_still_reaches_ready`.
* *Interruption can resume without starting over* —
  :func:`test_a_run_stopped_at_a_login_resumes_into_the_daemon_step`,
  :func:`test_a_rerun_revalidates_the_daemon_instead_of_restarting_it`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.install import InstallEngine, InstallOptions, InstallOutcome
from src.install.command import CommandOutput
from src.install.onboarding import (
    CAPABILITY_DAEMON,
    CAPABILITY_DISCORD,
    STEP_CHECK,
    STEP_CONFIG,
    STEP_DAEMON,
    STEP_DASHBOARD,
    STEP_DISCORD,
    api_base_url,
    data_locations,
    inspect_dashboard,
    onboarding_steps,
)
from src.install.platform import PlatformFacts, SupportVerdict
from src.install.prerequisites import STEP_DATA_DIR, data_directory_step, host_step
from src.install.redaction import find_secrets
from src.install.results import StepState
from src.install.steps import StepRegistry

TOKEN = "MTIzNDU2Nzg5.GaBcDe.a-real-looking-discord-bot-token-value"
CHANNEL = "123456789012345678"
GUILD = "876543210987654321"


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer runs before a database exists; it allocates none of its own."""


def _facts() -> PlatformFacts:
    return PlatformFacts(
        system="linux",
        release="6.6.0-microsoft-standard-WSL2",
        machine="x86_64",
        arch="x86_64",
        python_version="3.12.3",
        distro_id="ubuntu",
        distro_version="24.04",
        distro_name="Ubuntu 24.04 LTS",
        wsl=True,
        wsl_version=2,
    )


WSL2 = SupportVerdict(host_path="windows-wsl2", tier="supported", facts=_facts())

VALID_DSN = "postgresql+asyncpg://agent_queue:${AQ_DB_PASSWORD}@localhost:5432/agent_queue"


class FakeDaemon:
    """A daemon that is down until ``aq start`` is run, then answers /health."""

    def __init__(self, *, up: bool = False, starts: bool = True, dashboard: int | None = 200):
        self.up = up
        self.starts = starts
        self.dashboard = dashboard
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv, **kwargs) -> CommandOutput:
        command = tuple(str(part) for part in argv)
        self.commands.append(command)
        if command[1:] == ("start",):
            if not self.starts:
                return CommandOutput(argv=command, returncode=1, stderr="database is unreachable")
            self.up = True
            return CommandOutput(argv=command, returncode=0, stdout="Daemon started")
        raise AssertionError(f"unexpected command: {command}")

    def probe(self, url: str) -> int | None:
        if url.endswith("/health"):
            return 200 if self.up else None
        if url.endswith("/dashboard"):
            return self.dashboard if self.up else None
        raise AssertionError(f"unexpected probe: {url}")


def home_with_config(tmp_path: Path, *, body: str | None = None) -> Path:
    home = tmp_path / "aq-home"
    home.mkdir(parents=True, exist_ok=True)
    if body is not None:
        (home / "config.yaml").write_text(body, encoding="utf-8")
    return home


def registry_for(
    home: Path,
    daemon: FakeDaemon,
    *,
    which=lambda name: f"/usr/bin/{name}",
    extra=(),
) -> StepRegistry:
    """The engine's admission plus the onboarding steps, and nothing else.

    The prerequisite, PostgreSQL and provider adapters have their own suites;
    composing them here would make every assertion about the daemon depend on
    whether this box happens to run PostgreSQL.
    """
    registry = StepRegistry((host_step(), data_directory_step(path=home)))
    registry.extend(extra)
    registry.extend(
        onboarding_steps(
            environ={"HOME": str(home)},
            home=home,
            runner=daemon.run,
            which=which,
            probe=daemon.probe,
        )
    )
    return registry


def run(
    registry: StepRegistry,
    tmp_path: Path,
    *,
    capabilities: tuple[str, ...] = (),
    settings: dict | None = None,
    state: Path | None = None,
    dry_run: bool = False,
):
    options = InstallOptions(
        installer_version="1.2.3",
        target_version="1.2.3",
        interactive=False,
        dry_run=dry_run,
        capabilities=frozenset(capabilities),
        approve=frozenset({"*"}),
        settings=settings or {},
        state_path=state or (tmp_path / "install-state.json"),
    )
    return InstallEngine(registry, options, support=WSL2).run()


def step(result, step_id):
    return next(row for row in result.steps if row.step_id == step_id)


def env_with_password(home: Path) -> None:
    (home / ".env").write_text("AQ_DB_PASSWORD=local-development\n", encoding="utf-8")


def configured(tmp_path: Path) -> Path:
    """A home whose configuration is what the PostgreSQL steps would have left."""
    home = home_with_config(
        tmp_path,
        body=f"messaging_platform: none\ndatabase:\n  url: {VALID_DSN}\n",
    )
    env_with_password(home)
    return home


# ---------------------------------------------------------------------------
# The whole path
# ---------------------------------------------------------------------------


def test_a_default_run_reaches_a_ready_daemon_and_a_dashboard_url(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon()

    result = run(
        registry_for(home, daemon),
        tmp_path,
        capabilities=(CAPABILITY_DAEMON,),
    )

    assert result.outcome is InstallOutcome.READY
    assert step(result, STEP_CONFIG).state is StepState.SUCCEEDED
    assert step(result, STEP_CHECK).state is StepState.SUCCEEDED
    assert step(result, STEP_DAEMON).state is StepState.SUCCEEDED
    assert daemon.commands == [("/usr/bin/aq", "start")]
    board = step(result, STEP_DASHBOARD).detail["dashboard"]
    assert board["reachable"] is True
    assert board["url"].endswith("/dashboard")


def test_the_configuration_step_tunes_for_this_machine_and_keeps_what_exists(tmp_path):
    home = configured(tmp_path)
    config = home / "config.yaml"
    daemon = FakeDaemon(up=True)

    result = run(registry_for(home, daemon), tmp_path)

    written = step(result, STEP_CONFIG).detail
    assert written["tuned"] is True
    assert written["written"], "the tuned sections should have been added"
    body = config.read_text(encoding="utf-8")
    # The database the PostgreSQL steps configured is not disturbed, and the
    # Discord-free default survives.
    assert VALID_DSN in body
    assert "messaging_platform: none" in body


def test_a_config_that_does_not_load_names_the_step_and_the_fix(tmp_path):
    home = home_with_config(tmp_path, body="database:\n  url: ''\n")
    daemon = FakeDaemon(up=True)

    result = run(registry_for(home, daemon), tmp_path)

    assert result.outcome is InstallOutcome.FAILED
    check = step(result, STEP_CHECK)
    assert check.state is StepState.FAILED
    assert "aq system config edit" in (check.remediation or "")
    assert result.blocking_step is not None
    assert result.blocking_step.step_id == STEP_CHECK


def test_the_check_step_reports_where_every_kind_of_data_lives(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)

    result = run(registry_for(home, daemon), tmp_path)

    labels = {entry["label"] for entry in step(result, STEP_CHECK).detail["locations"]}
    assert {"Configuration", "Secrets", "Vault", "Worktrees", "Daemon log", "Database"} <= labels


def test_a_rerun_still_reports_where_data_lives_and_which_url_to_open(tmp_path):
    """The summary is built from this run's detail, so a rerun must carry it.

    ``config.check`` and ``daemon.dashboard`` only read, so the engine
    revalidates them by running them and records what they observed.  Answering
    "still true" instead is what printed an empty "Where AQ stores your data"
    on every second ``aq install``, ``--repair`` and ``--upgrade``.
    """
    from src.install.wizard import summarize

    home = configured(tmp_path)
    daemon = FakeDaemon()
    state = tmp_path / "install-state.json"

    first = run(
        registry_for(home, daemon), tmp_path, capabilities=(CAPABILITY_DAEMON,), state=state
    )
    second = run(
        registry_for(home, daemon), tmp_path, capabilities=(CAPABILITY_DAEMON,), state=state
    )

    assert second.outcome is InstallOutcome.READY
    action = {row.step_id: row.action.value for row in second.plan}
    assert action[STEP_CHECK] == "revalidate"
    assert action[STEP_DASHBOARD] == "revalidate"
    assert step(second, STEP_CHECK).detail["revalidated"] is True
    assert daemon.commands == [("/usr/bin/aq", "start")], "a rerun must not restart the daemon"

    before, after = summarize(first), summarize(second)
    assert after.locations == before.locations != ()
    assert after.dashboard == before.dashboard
    assert after.dashboard is not None and after.dashboard.reachable is True
    assert after.readiness is not None
    assert {check.id for check in after.readiness.checks if check.ready} == {
        check.id for check in before.readiness.checks if check.ready
    }


def test_a_rerun_reports_the_configuration_as_it_now_stands(tmp_path):
    """Revalidation observes the box, so an edited setting is reported, not replayed."""
    from src.install.wizard import summarize

    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)
    state = tmp_path / "install-state.json"

    run(registry_for(home, daemon), tmp_path, state=state)
    config = home / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + f"workspace_dir: {tmp_path / 'elsewhere'}\n",
        encoding="utf-8",
    )
    second = run(registry_for(home, daemon), tmp_path, state=state)

    worktrees = next(
        location for location in summarize(second).locations if location.label == "Worktrees"
    )
    assert worktrees.path == str(tmp_path / "elsewhere")


def test_the_reported_database_location_carries_no_password(tmp_path):
    locations = data_locations(
        tmp_path,
        {"database": {"url": "postgresql+asyncpg://agent_queue:hunter2@localhost:5432/agent_queue"}},
    )
    database = next(entry for entry in locations if entry.label == "Database")
    assert "hunter2" not in database.path
    assert "localhost:5432/agent_queue" in database.path


# ---------------------------------------------------------------------------
# Discord is optional
# ---------------------------------------------------------------------------


def test_an_install_that_never_mentions_discord_is_ready(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)

    result = run(registry_for(home, daemon), tmp_path)

    assert result.outcome is InstallOutcome.READY
    discord = step(result, STEP_DISCORD)
    assert discord.state is StepState.SKIPPED
    assert CAPABILITY_DISCORD in discord.summary
    assert "messaging_platform: none" in (home / "config.yaml").read_text(encoding="utf-8")


def test_selecting_discord_without_a_token_stops_at_the_human_and_says_where(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)

    result = run(
        registry_for(home, daemon),
        tmp_path,
        capabilities=(CAPABILITY_DISCORD,),
        settings={"discord": {"channel_id": CHANNEL, "guild_id": GUILD}},
    )

    assert result.outcome is InstallOutcome.NEEDS_USER
    discord = step(result, STEP_DISCORD)
    assert discord.state is StepState.NEEDS_USER
    assert "DISCORD_BOT_TOKEN" in discord.summary
    assert str(home / ".env") in (discord.remediation or "")
    assert "fully usable without it" in (discord.remediation or "") or "0600" in (
        discord.remediation or ""
    )


def test_discord_without_a_channel_says_which_setting_is_missing(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)

    result = run(
        registry_for(home, daemon),
        tmp_path,
        capabilities=(CAPABILITY_DISCORD,),
        settings={"discord": {"guild_id": GUILD}},
    )

    discord = step(result, STEP_DISCORD)
    assert discord.state is StepState.NEEDS_USER
    assert discord.detail["missing"] == ["channel_id"]


def test_configured_discord_writes_a_reference_and_never_the_token(tmp_path):
    home = configured(tmp_path)
    (home / ".env").write_text(
        f"AQ_DB_PASSWORD=local-development\nDISCORD_BOT_TOKEN={TOKEN}\n", encoding="utf-8"
    )
    daemon = FakeDaemon(up=True)
    state_path = tmp_path / "install-state.json"

    result = run(
        registry_for(home, daemon),
        tmp_path,
        capabilities=(CAPABILITY_DISCORD,),
        settings={"discord": {"channel_id": CHANNEL, "guild_id": GUILD}},
        state=state_path,
    )

    assert step(result, STEP_DISCORD).state is StepState.SUCCEEDED
    body = (home / "config.yaml").read_text(encoding="utf-8")
    assert "messaging_platform: discord" in body
    assert "${DISCORD_BOT_TOKEN}" in body
    assert TOKEN not in body
    # Neither the machine-readable result nor the resume record may carry it.
    assert TOKEN not in json.dumps(result.to_dict())
    assert TOKEN not in state_path.read_text(encoding="utf-8")
    assert find_secrets(result.to_dict()) == []


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


def test_a_daemon_that_is_already_answering_is_reused_not_restarted(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)

    result = run(registry_for(home, daemon), tmp_path, capabilities=(CAPABILITY_DAEMON,))

    started = step(result, STEP_DAEMON)
    assert started.state is StepState.SUCCEEDED
    assert started.detail["started"] is False
    assert daemon.commands == []


def test_a_daemon_that_does_not_come_up_names_the_log_and_the_doctor(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon(starts=False)

    result = run(registry_for(home, daemon), tmp_path, capabilities=(CAPABILITY_DAEMON,))

    assert result.outcome is InstallOutcome.FAILED
    failure = step(result, STEP_DAEMON)
    assert failure.state is StepState.FAILED
    assert "daemon.log" in (failure.remediation or "")
    assert "aq doctor" in (failure.remediation or "")


def test_a_missing_aq_executable_is_reported_rather_than_guessed(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon()

    result = run(
        registry_for(home, daemon, which=lambda name: None),
        tmp_path,
        capabilities=(CAPABILITY_DAEMON,),
    )

    failure = step(result, STEP_DAEMON)
    assert failure.state is StepState.FAILED
    assert "not on PATH" in failure.summary
    assert daemon.commands == []


def test_a_rerun_revalidates_the_daemon_instead_of_restarting_it(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon()
    state_path = tmp_path / "install-state.json"

    first = run(
        registry_for(home, daemon),
        tmp_path,
        capabilities=(CAPABILITY_DAEMON,),
        state=state_path,
    )
    assert first.outcome is InstallOutcome.READY
    second = run(
        registry_for(home, daemon),
        tmp_path,
        capabilities=(CAPABILITY_DAEMON,),
        state=state_path,
    )

    assert second.outcome is InstallOutcome.READY
    # One start across two runs: the second run revalidated /health.
    assert daemon.commands == [("/usr/bin/aq", "start")]


def test_the_daemon_is_not_started_unless_it_was_selected(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon()

    result = run(registry_for(home, daemon), tmp_path)

    assert result.outcome is InstallOutcome.READY
    assert step(result, STEP_DAEMON).state is StepState.SKIPPED
    assert daemon.commands == []


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


def test_a_release_install_reports_the_bundled_dashboard_url():
    info = inspect_dashboard("http://127.0.0.1:8081", lambda url: 200)
    assert info.url == "http://127.0.0.1:8081/dashboard"
    assert info.reachable is True
    assert info.source == "bundled"


def test_a_source_checkout_is_told_to_run_the_dev_server():
    info = inspect_dashboard("http://127.0.0.1:8081", lambda url: 404)
    assert info.source == "dev-server"
    assert info.url == "http://localhost:5173"
    assert "npm -w dashboard run dev" in info.hint


def test_a_daemon_that_is_not_answering_says_so_without_failing_the_run(tmp_path):
    home = configured(tmp_path)
    daemon = FakeDaemon(up=False)

    result = run(registry_for(home, daemon), tmp_path)

    dashboard = step(result, STEP_DASHBOARD)
    assert dashboard.state is StepState.SUCCEEDED
    assert dashboard.detail["dashboard"]["reachable"] is False
    assert "aq start" in dashboard.detail["dashboard"]["hint"]


def test_the_api_url_follows_the_configuration_and_the_environment():
    assert api_base_url({}, {}) == "http://127.0.0.1:8081"
    assert api_base_url({"mcp_server": {"host": "0.0.0.0", "port": 9000}}, {}) == (
        "http://0.0.0.0:9000"
    )
    assert api_base_url({"mcp_server": {"port": 9000}}, {"AQ_API_URL": "http://box:1234/"}) == (
        "http://box:1234"
    )


# ---------------------------------------------------------------------------
# Interruption and resume
# ---------------------------------------------------------------------------


def test_a_run_stopped_at_a_login_resumes_into_the_daemon_step(tmp_path):
    """A harness login is a human checkpoint, not a lost install.

    The first run stops at the login and starts nothing; the human signs in;
    the second run revalidates everything that was already done and continues
    into the daemon.  Nothing before the checkpoint is repeated.
    """
    from src.install.results import StepResult
    from src.install.steps import StepSpec

    home = configured(tmp_path)
    daemon = FakeDaemon()
    signed_in: list[bool] = [False]

    def login(context):
        if signed_in[0]:
            return StepResult.succeeded("provider.claude-login", "Claude Code is authenticated")
        return StepResult.needs_user(
            "provider.claude-login",
            "Claude Code is installed but not authenticated",
            "Run `claude auth login`, then rerun `aq install`.",
        )

    checkpoint = StepSpec(
        id="provider.claude-login",
        title="Sign in to Claude Code",
        run=login,
        depends_on=(STEP_DATA_DIR,),
        verify=lambda context: signed_in[0],
    )
    state_path = tmp_path / "install-state.json"

    first = run(
        registry_for(home, daemon, extra=(checkpoint,)),
        tmp_path,
        capabilities=(CAPABILITY_DAEMON,),
        state=state_path,
    )
    assert first.outcome is InstallOutcome.NEEDS_USER
    assert first.blocking_step.step_id == "provider.claude-login"
    assert daemon.commands == []
    assert step(first, STEP_DAEMON).state is StepState.SKIPPED

    signed_in[0] = True
    second = run(
        registry_for(home, daemon, extra=(checkpoint,)),
        tmp_path,
        capabilities=(CAPABILITY_DAEMON,),
        state=state_path,
    )

    assert second.outcome is InstallOutcome.READY
    assert step(second, STEP_DAEMON).detail["started"] is True
    backups = list(home.glob("config.yaml.bak*"))

    third = run(
        registry_for(home, daemon, extra=(checkpoint,)),
        tmp_path,
        capabilities=(CAPABILITY_DAEMON,),
        state=state_path,
    )

    # A third run repeats nothing: no second backup, no second `aq start`.
    assert third.outcome is InstallOutcome.READY
    assert list(home.glob("config.yaml.bak*")) == backups
    assert daemon.commands == [("/usr/bin/aq", "start")]


def test_skipping_every_provider_still_reaches_ready(tmp_path):
    """No harness selected is a complete install of *this machine*.

    A provider is a capability, so an install that selects none records them
    skipped and finishes ready — the machine is set up, and a harness can be
    added later with another `aq install --with provider.<name>`.
    """
    from src.install.prerequisites import tmux_step
    from src.install.providers import provider_steps

    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)
    registry = registry_for(
        home,
        daemon,
        extra=(
            tmux_step(which=lambda name: f"/usr/bin/{name}"),
            *provider_steps(which=lambda name: None),
        ),
    )

    result = run(registry, tmp_path)

    assert result.outcome is InstallOutcome.READY
    skipped = {row.step_id for row in result.steps if row.state is StepState.SKIPPED}
    assert {"provider.claude-cli", "provider.codex-cli", "provider.gemini-cli"} <= skipped


# ---------------------------------------------------------------------------
# The published contract
# ---------------------------------------------------------------------------


def test_the_documented_onboarding_steps_and_capabilities_match_the_registry():
    """``docs/reference/cli/install.md`` is the published surface, not a summary."""
    from src.install import build_registry

    doc = (
        Path(__file__).resolve().parent.parent / "docs" / "reference" / "cli" / "install.md"
    ).read_text(encoding="utf-8")
    registry = build_registry(WSL2)
    for step_id in (STEP_CONFIG, STEP_CHECK, STEP_DISCORD, STEP_DAEMON, STEP_DASHBOARD):
        assert step_id in registry, f"{step_id} is not registered by build_registry"
        assert step_id in doc, f"{step_id} is not documented"
    assert CAPABILITY_DISCORD in registry.capabilities()
    assert CAPABILITY_DAEMON in registry.capabilities()
    assert "--with discord" in doc
    assert "--advanced" in doc


def test_an_unknown_discord_setting_is_refused_by_name(tmp_path):
    """A misspelled setting must not deliver to the wrong channel, or to none."""
    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)

    result = run(
        registry_for(home, daemon),
        tmp_path,
        capabilities=(CAPABILITY_DISCORD,),
        settings={"discord": {"chanel_id": CHANNEL, "guild_id": GUILD}},
    )

    failure = step(result, STEP_DISCORD)
    assert failure.state is StepState.FAILED
    assert "chanel_id" in failure.summary
    assert failure.retryable is False


def test_a_dry_run_on_a_bare_machine_reports_a_plan_rather_than_a_failure(tmp_path):
    """A dry run must not fail because of what a dry run did not do.

    ``config.defaults`` is mutating, so a dry run never writes the file that
    ``config.check`` would read; reporting "it does not parse" there would be a
    failure the dry run itself caused.
    """
    home = home_with_config(tmp_path)
    daemon = FakeDaemon()

    result = run(
        registry_for(home, daemon),
        tmp_path,
        capabilities=(CAPABILITY_DAEMON,),
        dry_run=True,
    )

    assert result.outcome is InstallOutcome.READY
    check = step(result, STEP_CHECK)
    assert check.state is StepState.SKIPPED
    assert "dry run" in check.summary
    assert not (home / "config.yaml").exists()
    assert daemon.commands == []


def test_a_dry_run_still_reports_a_configuration_that_is_already_broken(tmp_path):
    """A read-only finding is not caused by the dry run, so it is not softened."""
    home = home_with_config(tmp_path, body="database:\n  url: ''\n")
    daemon = FakeDaemon()

    result = run(registry_for(home, daemon), tmp_path, dry_run=True)

    assert step(result, STEP_CHECK).state is StepState.FAILED


def test_the_reported_worktree_directory_is_the_one_the_daemon_uses(tmp_path):
    """A configuration that names no worktree directory still reports the real one."""
    from src.config import AppConfig
    from src.install.onboarding import DEFAULT_WORKSPACE_DIR

    assert DEFAULT_WORKSPACE_DIR == AppConfig().workspace_dir

    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)
    result = run(registry_for(home, daemon), tmp_path)

    worktrees = next(
        entry
        for entry in step(result, STEP_CHECK).detail["locations"]
        if entry["label"] == "Worktrees"
    )
    assert worktrees["path"] == AppConfig().workspace_dir


def test_a_configured_worktree_directory_is_reported_as_configured(tmp_path):
    home = home_with_config(
        tmp_path,
        body=(
            f"messaging_platform: none\nworkspace_dir: {tmp_path / 'checkouts'}\n"
            f"database:\n  url: {VALID_DSN}\n"
        ),
    )
    env_with_password(home)
    daemon = FakeDaemon(up=True)

    result = run(registry_for(home, daemon), tmp_path)

    worktrees = next(
        entry
        for entry in step(result, STEP_CHECK).detail["locations"]
        if entry["label"] == "Worktrees"
    )
    assert worktrees["path"] == str(tmp_path / "checkouts")


def test_a_channel_name_where_an_id_belongs_is_caught_by_the_installer(tmp_path):
    """The daemon refuses it at the next start; the installer says so now."""
    home = configured(tmp_path)
    daemon = FakeDaemon(up=True)

    result = run(
        registry_for(home, daemon),
        tmp_path,
        capabilities=(CAPABILITY_DISCORD,),
        settings={"discord": {"channel_id": "#general", "guild_id": GUILD}},
    )

    discord = step(result, STEP_DISCORD)
    assert discord.state is StepState.NEEDS_USER
    assert discord.detail["malformed"] == ["channel_id"]
    assert "Copy ID" in (discord.remediation or "")
    assert "messaging_platform: none" in (home / "config.yaml").read_text(encoding="utf-8")
