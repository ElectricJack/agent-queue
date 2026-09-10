"""``aq install`` end to end: the composed installer on a scripted machine.

Every adapter has its own suite, and each of those composes the one registry it
is about.  This suite composes the registry ``aq install`` actually runs —
admission, prerequisites, PostgreSQL, provider CLIs, provider logins and
onboarding, wired together by :func:`src.install.registry.build_registry` — and
drives it through the real engine on the scripted host in
:mod:`tests.installer_machine`.

It exists for the failures that only appear *between* adapters: a daemon
started before the database answered, a provider probe that crashes on a host
with no platform adapter, a rerun that re-installs what another adapter already
recorded, a stop at a human checkpoint that leaves the rest of the run
unreached.  So the assertions here are about observable outcomes — exit codes,
which commands ran and in what order, what the resume record owns, what a
following ``aq uninstall`` would remove — rather than about any adapter's
internals.

Nothing here touches a real process, socket, database or credential; that is
itself asserted by
:func:`test_the_composed_run_touches_no_real_process_socket_or_network`.  What
that isolation cannot prove, and therefore still needs a real Windows or macOS
machine, is listed in ``docs/contributing/installer-testing.md``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from src.install import (
    InstallOutcome,
    LifecycleMode,
    RemovalAction,
    RemovalScope,
    build_registry,
    default_handlers,
    execute_uninstall,
    load_state,
    plan_uninstall,
)
from src.install.logins import probe_all
from src.install.onboarding import CAPABILITY_DAEMON, STEP_CONFIG, STEP_DAEMON
from src.install.postgres_steps import (
    CAPABILITY_MANAGED,
    STEP_CONNECTION,
    STEP_PACKAGE,
    STEP_ROLE,
    STEP_SERVER,
    STEP_SERVICE,
)
from src.install.providers import CLAUDE_CODE, CODEX, GEMINI
from src.install.redaction import find_secrets
from src.install.results import StepState
from src.install.steps import StepContext
from tests.installer_machine import (
    WSL2,
    Database,
    Machine,
    Provider,
    provisioned,
    states,
    step,
)

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "contributing" / "installer-testing.md"

#: The selection a newcomer who answers "yes" to everything ends up with.
FULL = (CAPABILITY_MANAGED, CLAUDE_CODE.capability, CAPABILITY_DAEMON)


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer runs before a database exists; it allocates none of its own."""


def signed_in(tmp_path, provider_id: str = "claude", **kwargs) -> Machine:
    """A machine whose *provider_id* CLI is installed and already authenticated."""
    host = Machine(tmp_path, providers={provider_id: Provider(installed=True)}, **kwargs)
    host.sign_in(provider_id)
    return host


# ---------------------------------------------------------------------------
# The newcomer's path, once, on a bare machine
# ---------------------------------------------------------------------------


def test_a_fresh_machine_reaches_a_ready_daemon_through_every_adapter(tmp_path):
    host = signed_in(tmp_path)

    result = host.install(capabilities=FULL)

    assert result.outcome is InstallOutcome.READY
    assert result.exit_code == 0
    assert result.blocking_step is None
    # Every adapter contributed, and none of them stopped the run.
    assert states(result)[STEP_PACKAGE] == "succeeded"
    assert states(result)[CLAUDE_CODE.step_id] == "succeeded"
    assert states(result)["provider.claude-login"] == "succeeded"
    assert states(result)[STEP_DAEMON] == "succeeded"
    # ...and the machine really was changed: a package, a role, a database, a
    # credential file, a configuration and a running daemon.
    assert "postgresql" in host.packages
    assert "agent_queue" in host.database.roles
    assert "agent_queue" in host.database.databases
    assert host.env_path.exists()
    assert host.config_path.exists()
    assert host.daemon_up is True
    kinds = {record.kind for record in result.resources}
    assert {"postgres-role", "postgres-database", "postgres-credential", "daemon"} <= kinds


def test_the_daemon_is_only_started_after_the_database_answers(tmp_path):
    """The cross-adapter dependency ``build_registry`` states, observed as order.

    Registration order alone would let the engine's topological sort interleave
    the onboarding branch with the database branch, and a daemon started before
    the credential was written fails for a reason nobody can read.
    """
    host = signed_in(tmp_path)
    ordered = [spec.id for spec in host.registry().ordered()]
    assert ordered.index(STEP_CONNECTION) < ordered.index(STEP_CONFIG)
    assert ordered.index(STEP_CONFIG) < ordered.index(STEP_DAEMON)

    host.install(capabilities=FULL)

    executed = [" ".join(command) for command in host.commands]
    created_database = next(
        index for index, text in enumerate(executed) if "psql" in text and "--dbname" in text
    )
    started_daemon = next(
        index for index, text in enumerate(executed) if "aq start" in text
    )
    assert created_database < started_daemon


def test_a_provider_probe_on_a_host_with_no_platform_adapter_reads_path(monkeypatch):
    """``build_registry`` composes the provider steps the way the CLI calls it.

    WSL2 has no platform adapter, so the lookup the composition passes down is
    ``None`` there.  The provider steps take a callable rather than defaulting
    one away, so without a fallback ``aq install --with provider.claude``
    reported a ``TypeError`` from the probe instead of "claude is not on PATH".
    """
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    registry = build_registry(WSL2)  # exactly what src/cli/install.py does
    context = StepContext(support=WSL2, options={}, dry_run=False, interactive=False)

    for installer in (CLAUDE_CODE, CODEX, GEMINI):
        assert registry.get(installer.step_id).verify(context) is False


def test_no_step_migrates_a_schema_or_touches_an_unrelated_database(tmp_path):
    """The installer's contract puts schema work in the daemon, never here."""
    host = Machine(tmp_path, database=provisioned())
    host.set_env(AQ_DB_PASSWORD="existing-password")

    host.install(capabilities=(CAPABILITY_DAEMON,))

    for statement in host.database.statements:
        assert not re.search(r"\b(CREATE TABLE|ALTER TABLE|DROP)\b", statement, re.IGNORECASE)
        assert "other_app" not in statement
    assert host.database.roles["other_app"] == "unrelated"
    assert "other_app" in host.database.databases
    assert not host.ran("alembic")
    assert not host.ran("aq", "db")


def test_the_resume_record_of_a_full_run_carries_no_secret(tmp_path):
    host = signed_in(tmp_path)

    result = host.install(capabilities=FULL)

    text = host.state_path.read_text(encoding="utf-8")
    assert find_secrets(json.loads(text)) == []
    assert "generated-password-1" not in text
    assert "generated-password-1" not in json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# Rerunning
# ---------------------------------------------------------------------------


def test_a_rerun_installs_nothing_again_and_owns_the_same_resources(tmp_path):
    host = signed_in(tmp_path)
    first = host.install(capabilities=FULL)
    host.forget()

    second = host.install(capabilities=FULL)

    assert second.outcome is InstallOutcome.READY
    assert {record.key for record in second.resources} == {record.key for record in first.resources}
    assert not host.ran("apt-get", "install")
    assert not host.ran("aq", "start")
    assert not host.ran("curl")
    # The role and database exist, so no statement recreated either of them.
    assert not [row for row in host.database.statements if row.startswith("CREATE")]


def test_restarting_the_database_step_replays_its_dependents_and_leaves_other_branches(tmp_path):
    """``--restart-from`` invalidates one branch, not the whole installation."""
    host = signed_in(tmp_path)
    host.install(capabilities=FULL)
    host.forget()

    result = host.install(capabilities=FULL, restart_from=STEP_ROLE)

    assert result.outcome is InstallOutcome.READY
    # The onboarding branch depends on the database, so it is replayed — and
    # finds a healthy daemon rather than starting a second one.
    assert step(result, STEP_DAEMON).detail.get("started") is False
    assert not host.ran("aq", "start")
    # The provider branch does not depend on the database and was carried
    # forward: no installer ran again.
    assert step(result, CLAUDE_CODE.step_id).state is StepState.SUCCEEDED
    assert not host.ran("curl")
    # Nothing was duplicated on the server the restart replayed.
    assert not [row for row in host.database.statements if row.startswith("CREATE ROLE")]


# ---------------------------------------------------------------------------
# Interruption and recovery
# ---------------------------------------------------------------------------


def test_a_daemon_that_will_not_start_fails_the_run_but_keeps_the_database_work(tmp_path):
    host = signed_in(tmp_path, daemon_starts=False)

    result = host.install(capabilities=FULL)

    assert result.outcome is InstallOutcome.FAILED
    assert result.exit_code == 20
    assert result.blocking_step.step_id == STEP_DAEMON
    assert "aq doctor" in result.blocking_step.remediation
    # The work that succeeded before the failure is durable, which is what makes
    # the next run a resume rather than a restart.
    record = load_state(host.state_path)
    assert record.record_for(STEP_CONNECTION).state is StepState.SUCCEEDED
    assert record.record_for(STEP_DAEMON).state is StepState.FAILED
    assert ("postgres-role", "agent_queue") in record.resources


def test_the_rerun_after_the_daemon_is_fixed_reaches_ready_without_redoing_the_database(tmp_path):
    host = signed_in(tmp_path, daemon_starts=False)
    host.install(capabilities=FULL)
    host.daemon_starts = True  # the human read the log and fixed the reason
    host.forget()

    result = host.install(capabilities=FULL)

    assert result.outcome is InstallOutcome.READY
    assert host.ran("aq", "start")
    assert not host.ran("apt-get", "install")
    assert not [row for row in host.database.statements if row.startswith("CREATE")]


def test_an_upgrade_interrupted_before_the_daemon_is_resumed_by_the_next_run(tmp_path):
    """A version transition is durable before the first step, so a kill resumes."""
    host = signed_in(tmp_path, daemon_starts=False)
    host.install(capabilities=FULL, installer_version="1.2.3", target_version="1.2.3")

    interrupted = host.install(
        capabilities=FULL,
        mode=LifecycleMode.UPGRADE,
        installer_version="1.2.3",
        target_version="2.0.0",
    )
    assert interrupted.outcome is InstallOutcome.FAILED
    record = load_state(host.state_path)
    assert record.upgrade.to_version == "2.0.0"
    assert record.upgrade.in_progress is True

    host.daemon_starts = True
    resumed = host.install(capabilities=FULL, installer_version="1.2.3", target_version="2.0.0")

    assert resumed.outcome is InstallOutcome.READY
    assert any("2.0.0" in message for message in resumed.messages)
    assert load_state(host.state_path).upgrade.in_progress is False


# ---------------------------------------------------------------------------
# A PostgreSQL that is already there
# ---------------------------------------------------------------------------


def test_an_existing_server_is_reused_without_installing_or_owning_anything(tmp_path):
    host = Machine(tmp_path, database=provisioned())
    host.set_env(AQ_DB_PASSWORD="existing-password")

    result = host.install(capabilities=(CAPABILITY_DAEMON,))

    assert result.outcome is InstallOutcome.READY
    assert not host.ran("apt-get", "install")
    assert not host.ran("systemctl", "start")
    owned = {record.key for record in result.resources if record.owned}
    assert ("postgres-server", "localhost:5432") not in owned
    assert ("postgres-role", "agent_queue") not in owned
    assert ("postgres-database", "agent_queue") not in owned
    assert host.database.roles["agent_queue"] == "existing-password"


def test_uninstall_after_reusing_a_server_removes_nothing_of_it(tmp_path):
    """The install→uninstall boundary is the ``owned`` flag each adapter wrote."""
    host = Machine(tmp_path, database=provisioned())
    host.set_env(AQ_DB_PASSWORD="existing-password")
    host.install(capabilities=(CAPABILITY_DAEMON,))

    plan = plan_uninstall(
        load_state(host.state_path),
        scopes=frozenset({RemovalScope.RUNTIME, RemovalScope.DATABASE}),
        state_path=host.state_path,
    )

    removed = {(item.kind, item.id) for item in plan.items if item.action is RemovalAction.REMOVE}
    assert ("postgres-role", "agent_queue") not in removed
    assert ("postgres-database", "agent_queue") not in removed
    assert ("postgres-server", "localhost:5432") not in removed
    kept = {item.kind for item in plan.items if item.action is RemovalAction.KEEP}
    assert "postgres-credential" in kept


def _config_item(host, *, scopes=frozenset({RemovalScope.CONFIG})):
    plan = plan_uninstall(load_state(host.state_path), scopes=scopes, state_path=host.state_path)
    return next(
        item for item in plan.items if (item.kind, item.id) == ("config", str(host.config_path))
    )


def test_uninstall_removes_the_configuration_the_installer_wrote(tmp_path):
    """``config.yaml`` is written by two adapters; only the first one created it.

    ``postgres.credentials`` creates the file to point it at the database, and
    ``config.defaults`` later fills in the tuned defaults — by which time all it
    can see is a file that exists.  Recording that later observation as the
    truth is what made ``aq uninstall --remove-config`` keep a configuration AQ
    wrote, still naming a database the same uninstall had just dropped.
    """
    host = signed_in(tmp_path)
    assert not host.config_path.exists()

    result = host.install(capabilities=FULL)

    assert result.outcome is InstallOutcome.READY
    assert host.config_path.exists()
    item = _config_item(host)
    assert item.action is RemovalAction.REMOVE
    assert item.resource.owned is True


def test_uninstall_keeps_a_configuration_that_was_already_on_the_host(tmp_path):
    """The other half of the boundary: a file AQ found is never AQ's to delete."""
    host = Machine(tmp_path, database=provisioned())
    host.set_env(AQ_DB_PASSWORD="existing-password")
    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text("messaging_platform: none\n", encoding="utf-8")

    result = host.install(capabilities=(CAPABILITY_DAEMON,))

    assert result.outcome is InstallOutcome.READY
    item = _config_item(host)
    assert item.action is RemovalAction.KEEP
    assert item.resource.owned is False
    assert "did not create it" in item.reason


def test_remove_config_actually_deletes_the_file_the_installer_wrote(tmp_path):
    """The plan is only half the promise; this is the file leaving the disk."""
    host = signed_in(tmp_path)
    host.install(capabilities=FULL)

    plan = plan_uninstall(
        load_state(host.state_path),
        scopes=frozenset({RemovalScope.CONFIG}),
        state_path=host.state_path,
    )
    result = execute_uninstall(
        plan,
        default_handlers(home=host.aq_home, runner=host.run_daemon, which=host.which),
    )

    assert result.outcome is InstallOutcome.READY
    assert not host.config_path.exists()
    assert host.env_path.exists(), "the credential store is never AQ's to remove"


def test_a_rerun_does_not_forget_that_the_installer_wrote_the_configuration(tmp_path):
    """A second run sees the file in place; the resume record still owns it."""
    host = signed_in(tmp_path)
    host.install(capabilities=FULL)

    host.install(capabilities=FULL)

    assert _config_item(host).action is RemovalAction.REMOVE


def test_the_configuration_survives_an_uninstall_that_does_not_select_it(tmp_path):
    """Owned is not the same as selected: ``--remove-config`` is still required."""
    host = signed_in(tmp_path)
    host.install(capabilities=FULL)

    item = _config_item(host, scopes=frozenset({RemovalScope.RUNTIME, RemovalScope.DATABASE}))

    assert item.action is RemovalAction.KEEP
    assert "--remove-config" in item.reason


@pytest.mark.parametrize(
    ("capabilities", "blocking"),
    [((CAPABILITY_DAEMON,), STEP_SERVER), ((CAPABILITY_MANAGED, CAPABILITY_DAEMON), STEP_SERVICE)],
)
def test_a_port_owned_by_something_else_stops_the_run_before_the_daemon(
    tmp_path, capabilities, blocking
):
    host = Machine(tmp_path, database=Database(listening=True, speaks_postgres=False))

    result = host.install(capabilities=capabilities)

    assert result.outcome is InstallOutcome.FAILED
    assert result.exit_code == 20
    assert result.blocking_step.step_id == blocking
    assert "postgres.port" in result.blocking_step.remediation
    # Everything downstream is reported as unreached rather than attempted.
    assert states(result)[STEP_CONFIG] == "skipped"
    assert states(result)[STEP_DAEMON] == "skipped"
    assert not host.ran("aq", "start")
    assert host.daemon_up is False


# ---------------------------------------------------------------------------
# Providers: missing, installed, refused
# ---------------------------------------------------------------------------


def test_a_missing_selected_provider_is_installed_by_its_documented_installer(tmp_path):
    host = Machine(tmp_path, database=provisioned())
    host.set_env(AQ_DB_PASSWORD="existing-password")
    host.sign_in("claude")  # the credential store the human's login leaves behind

    result = host.install(capabilities=(CLAUDE_CODE.capability, CAPABILITY_DAEMON))

    assert result.outcome is InstallOutcome.READY
    assert list(CLAUDE_CODE.install_command) in [list(row) for row in host.commands]
    record = next(item for item in result.resources if item.key == ("provider-cli", CLAUDE_CODE.id))
    assert record.owned is True and record.reused is False
    assert host.which("claude") is not None


def test_a_provider_installer_that_fails_stops_the_run_and_names_the_documented_route(tmp_path):
    host = Machine(
        tmp_path,
        database=provisioned(),
        providers={"codex": Provider(installs=False)},
    )
    host.set_env(AQ_DB_PASSWORD="existing-password")

    result = host.install(capabilities=(CODEX.capability, CAPABILITY_DAEMON))

    assert result.outcome is InstallOutcome.FAILED
    assert result.blocking_step.step_id == CODEX.step_id
    assert CODEX.install_hint in result.blocking_step.remediation
    assert states(result)[STEP_DAEMON] == "skipped"
    assert host.daemon_up is False


def test_an_unselected_provider_is_skipped_and_the_install_still_reaches_ready(tmp_path):
    host = Machine(tmp_path, database=provisioned())
    host.set_env(AQ_DB_PASSWORD="existing-password")

    result = host.install(capabilities=(CAPABILITY_DAEMON,))

    assert result.outcome is InstallOutcome.READY
    for installer in (CLAUDE_CODE, CODEX, GEMINI):
        assert states(result)[installer.step_id] == "skipped"
        assert states(result)[f"provider.{installer.id}-login"] == "skipped"
    assert not host.ran("curl")
    assert not host.ran("npm", "install")
    assert host.daemon_up is True


# ---------------------------------------------------------------------------
# The human authentication checkpoint, and the run that follows it
# ---------------------------------------------------------------------------


def test_an_unauthenticated_provider_stops_at_the_human_before_the_daemon_starts(tmp_path):
    host = Machine(
        tmp_path,
        database=provisioned(),
        providers={"claude": Provider(installed=True)},
    )
    host.set_env(AQ_DB_PASSWORD="existing-password")

    result = host.install(capabilities=(CLAUDE_CODE.capability, CAPABILITY_DAEMON))

    assert result.outcome is InstallOutcome.NEEDS_USER
    assert result.exit_code == 10
    assert result.blocking_step.step_id == "provider.claude-login"
    assert "claude" in result.blocking_step.remediation
    assert states(result)[STEP_DAEMON] == "skipped"
    assert host.daemon_up is False
    # The checkpoint is not a failure: what already succeeded stays recorded,
    # and what the run never reached is absent rather than recorded as skipped.
    record = load_state(host.state_path)
    assert record.record_for(CLAUDE_CODE.step_id).state is StepState.SUCCEEDED
    assert record.record_for("provider.claude-login").state is StepState.NEEDS_USER
    assert record.record_for(STEP_CONNECTION) is None


def test_the_rerun_after_the_human_logs_in_continues_to_a_ready_daemon(tmp_path):
    host = Machine(
        tmp_path,
        database=provisioned(),
        providers={"claude": Provider(installed=True)},
    )
    host.set_env(AQ_DB_PASSWORD="existing-password")
    host.install(capabilities=(CLAUDE_CODE.capability, CAPABILITY_DAEMON))
    host.sign_in("claude")  # `claude auth login`, run by the human in their terminal
    host.forget()

    result = host.install(capabilities=(CLAUDE_CODE.capability, CAPABILITY_DAEMON))

    assert result.outcome is InstallOutcome.READY
    assert step(result, "provider.claude-login").state is StepState.SUCCEEDED
    assert host.ran("aq", "start")
    # Resuming re-verified the CLI rather than installing it a second time.
    assert list(CLAUDE_CODE.install_command) not in [list(row) for row in host.commands]


# ---------------------------------------------------------------------------
# Default profile activation follows what the run actually observed
# ---------------------------------------------------------------------------


def _probes(host: Machine):
    return probe_all(environ=host.environ, which=host.which, runner=host.run_provider)


def test_only_authenticated_providers_are_activated_for_routing(tmp_path):
    from src.profiles.catalog import active_catalog_profile_ids, refresh_catalog_profiles

    host = Machine(
        tmp_path,
        database=provisioned(),
        providers={"claude": Provider(installed=True), "codex": Provider(installed=True)},
    )
    host.set_env(AQ_DB_PASSWORD="existing-password")
    host.sign_in("claude")
    host.install(capabilities=(CLAUDE_CODE.capability, CAPABILITY_DAEMON))

    refresh_catalog_profiles(host.aq_home, _probes(host))

    active = active_catalog_profile_ids(host.aq_home)
    assert "worker-standard-medium-claude" in active
    # Installed but never signed in, and not installed at all: neither routes.
    assert not [profile for profile in active if profile.endswith("-codex")]
    assert not [profile for profile in active if profile.endswith("-gemini")]


def test_a_later_login_activates_its_profiles_without_replacing_an_edited_one(tmp_path):
    from src.profiles.catalog import active_catalog_profile_ids, refresh_catalog_profiles

    host = Machine(
        tmp_path,
        database=provisioned(),
        providers={"claude": Provider(installed=True), "codex": Provider(installed=True)},
    )
    host.set_env(AQ_DB_PASSWORD="existing-password")
    host.sign_in("claude")
    refresh_catalog_profiles(host.aq_home, _probes(host))
    edited = host.aq_home / "vault" / "agent-types" / "worker-deep-high-claude" / "profile.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\nOperator note.\n", encoding="utf-8")

    host.sign_in("codex")  # the human finishes the second login and reruns
    refresh_catalog_profiles(host.aq_home, _probes(host))

    active = active_catalog_profile_ids(host.aq_home)
    assert "worker-deep-high-codex" in active
    assert "worker-deep-high-claude" in active
    assert edited.read_text(encoding="utf-8").endswith("Operator note.\n")


def test_the_cli_run_writes_the_activation_record_and_reports_unready_providers(
    tmp_path, monkeypatch
):
    """``aq install`` itself: engine, summary, profile activation, exit code."""
    from click.testing import CliRunner

    from src.cli import install as install_cli
    from src.cli.app import cli
    from src.install import logins as logins_module

    host = Machine(
        tmp_path,
        database=provisioned(),
        providers={"claude": Provider(installed=True), "codex": Provider(installed=True)},
    )
    host.set_env(AQ_DB_PASSWORD="existing-password")
    host.sign_in("claude")
    monkeypatch.setenv("AQ_INSTALL_STATE_DIR", str(host.aq_home))
    monkeypatch.setenv("AQ_DB_PASSWORD", "existing-password")
    monkeypatch.setattr(install_cli, "describe_host", lambda: WSL2)
    monkeypatch.setattr(install_cli, "build_registry", lambda support: host.registry())
    monkeypatch.setattr(logins_module, "probe_all", lambda **kwargs: _probes(host))

    invocation = CliRunner().invoke(
        cli,
        [
            "install",
            "--json",
            "--non-interactive",
            "--yes",
            "--with",
            CLAUDE_CODE.capability,
            "--with",
            CAPABILITY_DAEMON,
        ],
    )

    assert invocation.exit_code == 0, invocation.output
    payload = json.loads(invocation.output.strip().splitlines()[-1])
    assert payload["outcome"] == "ready"
    assert payload["onboarding"]["ready"] is True
    assert any("Profile activation" in message for message in payload["messages"])
    activation = json.loads(
        (host.aq_home / "vault" / "profile-activation.json").read_text(encoding="utf-8")
    )
    assert activation["profiles"]["worker-standard-medium-claude"]["active"] is True
    assert activation["profiles"]["worker-standard-medium-codex"]["active"] is False
    assert activation["profiles"]["worker-standard-medium-codex"]["remediation"]


# ---------------------------------------------------------------------------
# What leaves the machine: the portable bundle
# ---------------------------------------------------------------------------


def test_the_configuration_a_real_install_wrote_exports_without_its_credentials(tmp_path):
    """The bundle is built from the configuration the installer itself produced."""
    from src.portable_config import bundle_preview, export_bundle, read_bundle

    host = signed_in(tmp_path)
    host.install(capabilities=FULL)
    assert "AQ_DB_PASSWORD" in host.env_path.read_text(encoding="utf-8")

    preview = bundle_preview(str(host.config_path), str(host.aq_home))
    assert "database" in preview["excluded_sections"]
    assert "database" not in preview["config"]

    destination = tmp_path / "portable.aqbundle"
    export_bundle(str(destination), str(host.config_path), str(host.aq_home))
    bundle = read_bundle(str(destination))

    body = destination.read_bytes()
    assert b"generated-password-1" not in body
    assert b"AQ_DB_PASSWORD" not in body
    assert "database" not in bundle.config
    assert "resources" in bundle.config  # the resource-aware tuning does travel


# ---------------------------------------------------------------------------
# Isolation, and the boundary of what any of this proves
# ---------------------------------------------------------------------------


def test_the_composed_run_touches_no_real_process_socket_or_network(tmp_path, monkeypatch):
    """Every host access is an injected seam, so a real one is a defect.

    This is the mechanical half of "no test touches the operator database or
    real credentials": with the real process, socket and HTTP entry points
    replaced by a refusal, the whole composed install still reaches ``ready``.
    """
    import socket
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("the installer reached the real host")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    host = signed_in(tmp_path)
    result = host.install(capabilities=FULL)

    assert result.outcome is InstallOutcome.READY
    assert str(host.aq_home).startswith(str(tmp_path))
    assert str(host.store_path("claude")).startswith(str(tmp_path))


def test_the_native_acceptance_page_names_steps_the_installer_really_has():
    """The documented mock boundaries cannot drift away from the registry.

    Each row of the page's limits table names the step whose host access is
    faked, so a step that is renamed or removed makes the page wrong here rather
    than on somebody's Windows box.
    """
    registry = build_registry(WSL2)
    rows = re.findall(
        r"^\| `([a-z][a-z0-9.-]*)` \|", DOC.read_text(encoding="utf-8"), flags=re.MULTILINE
    )
    step_ids = set(registry.ids())

    assert len(rows) >= 10, "the faked-seam table names too few steps to be a contract"
    assert set(rows) <= step_ids, sorted(set(rows) - step_ids)
