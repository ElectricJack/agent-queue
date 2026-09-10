"""``aq install`` — the PostgreSQL adapter (``noble-apex.6``).

The suite runs the real engine over the real steps against a scripted host: a
fake command runner, a fake ``which``, and a small in-memory PostgreSQL that
answers connections and executes the handful of statements the adapter issues.
Nothing here needs a server, a package manager or root, and nothing here may
touch the operator's database — the installer's own contract is that schema
work belongs to the daemon, so a test that ran a migration would be asserting
the wrong thing anyway.

What the acceptance criteria ask for, and where it is proved:

* *A fresh installation reaches a healthy database without manual SQL* —
  :func:`test_a_fresh_managed_install_reaches_a_healthy_database`.
* *Existing databases and unrelated roles are preserved* —
  :func:`test_an_existing_role_is_never_reset`,
  :func:`test_an_existing_database_is_reused_not_recreated`,
  :func:`test_unrelated_roles_and_databases_are_never_touched`.
* *Failures and credential changes have a documented recovery path* — the
  ``needs_user``/``failed`` tests below, plus
  :func:`test_the_documented_steps_and_capabilities_match_the_registry`.
* *Verify database startup and AQ reconnection after restart* —
  :func:`test_the_boot_step_enables_the_service_on_systemd`,
  :func:`test_a_wsl_distribution_without_systemd_asks_the_human`,
  :func:`test_a_rerun_after_a_restart_reconnects_without_changing_anything`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.install import InstallEngine, InstallOptions, InstallOutcome, default_registry
from src.install.platform import PlatformFacts, SupportVerdict
from src.install.postgres import (
    REASON_AUTH,
    REASON_MISSING_DATABASE,
    REASON_MISSING_ROLE,
    REASON_NOT_POSTGRES,
    REASON_OK,
    REASON_UNREACHABLE,
    AsyncpgExecutor,
    CommandResult,
    ConnectionCheck,
    PostgresSettings,
    PostgresSettingsError,
    PsqlExecutor,
    backup_path_for,
    build_url,
    generate_password,
    package_plan,
    read_env_file,
    resolve_admin,
    service_plan,
    split_password,
    write_database_config,
    write_env_value,
)
from src.install.postgres_steps import (
    CAPABILITY_MANAGED,
    CAPABILITY_ROTATE,
    STEP_BOOT,
    STEP_CONNECTION,
    STEP_CREDENTIALS,
    STEP_DATABASE,
    STEP_PACKAGE,
    STEP_ROLE,
    STEP_ROTATE,
    STEP_SERVER,
    STEP_SERVICE,
    PostgresAdapter,
)
from src.install.redaction import find_secrets
from src.install.results import StepState

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "reference" / "cli" / "install.md"


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer runs before a database exists; it allocates none of its own."""


def _facts(**overrides) -> PlatformFacts:
    base = {
        "system": "linux",
        "release": "6.6.0-microsoft-standard-WSL2",
        "machine": "x86_64",
        "arch": "x86_64",
        "python_version": "3.12.3",
        "distro_id": "ubuntu",
        "distro_version": "24.04",
        "distro_name": "Ubuntu 24.04 LTS",
        "wsl": True,
        "wsl_version": 2,
    }
    base.update(overrides)
    return PlatformFacts(**base)


WSL2 = SupportVerdict(host_path="windows-wsl2", tier="supported", facts=_facts())
MACOS = SupportVerdict(
    host_path="macos-apple-silicon",
    tier="supported",
    facts=_facts(system="darwin", arch="arm64", wsl=False, wsl_version=None, macos_version="14.5"),
)


# ---------------------------------------------------------------------------
# A scripted host
# ---------------------------------------------------------------------------


class FakeServer:
    """An in-memory PostgreSQL: roles, databases, passwords and a version.

    It understands exactly the statements the adapter issues, which is the
    point — a statement this class does not recognise is a statement the
    adapter should not be sending, and the test fails loudly rather than
    silently succeeding.
    """

    def __init__(
        self,
        *,
        listening: bool = True,
        speaks_postgres: bool = True,
        roles: dict[str, str] | None = None,
        databases: set[str] | None = None,
        version_num: int = 160004,
    ) -> None:
        self.listening = listening
        self.speaks_postgres = speaks_postgres
        self.roles = dict(roles or {})
        self.databases = set(databases or set())
        self.version_num = version_num
        self.statements: list[str] = []

    # -- connecting ---------------------------------------------------------
    def connect(self, url: str, timeout: float) -> ConnectionCheck:
        if not self.listening:
            return ConnectionCheck(REASON_UNREACHABLE, "connection refused")
        if not self.speaks_postgres:
            return ConnectionCheck(REASON_NOT_POSTGRES, "unexpected response")
        _, password = split_password(url)
        from urllib.parse import unquote, urlsplit

        parts = urlsplit(url)
        role = unquote(parts.username or "")
        database = (parts.path or "/").lstrip("/")
        if role not in self.roles:
            return ConnectionCheck(REASON_MISSING_ROLE, f'role "{role}" does not exist')
        if self.roles[role] != (password or ""):
            return ConnectionCheck(REASON_AUTH, f'password authentication failed for "{role}"')
        if database not in self.databases:
            return ConnectionCheck(REASON_MISSING_DATABASE, f'database "{database}" does not exist')
        return ConnectionCheck(
            REASON_OK,
            "connected",
            server_version=f"PostgreSQL {self.version_num // 10000}.{self.version_num % 100}",
            server_version_num=self.version_num,
        )

    # -- executing ----------------------------------------------------------
    def execute(self, sql: str) -> str:
        sql = sql.strip().rstrip(";")
        self.statements.append(sql)
        if not self.listening or not self.speaks_postgres:
            raise AssertionError("a statement was sent to a server that is not answering")

        if sql == "SELECT current_setting('server_version_num')":
            return f"{self.version_num}\n"
        match = re.fullmatch(r"SELECT 1 FROM pg_roles WHERE rolname = '([^']+)'", sql)
        if match:
            return "1\n" if match.group(1) in self.roles else ""
        match = re.fullmatch(r"SELECT 1 FROM pg_database WHERE datname = '([^']+)'", sql)
        if match:
            return "1\n" if match.group(1) in self.databases else ""
        match = re.fullmatch(r"CREATE ROLE (\w+) WITH LOGIN PASSWORD '([^']*)'", sql)
        if match:
            if match.group(1) in self.roles:
                raise RuntimeError(f'role "{match.group(1)}" already exists')
            self.roles[match.group(1)] = match.group(2)
            return ""
        match = re.fullmatch(r"ALTER ROLE (\w+) WITH LOGIN PASSWORD '([^']*)'", sql)
        if match:
            if match.group(1) not in self.roles:
                raise RuntimeError(f'role "{match.group(1)}" does not exist')
            self.roles[match.group(1)] = match.group(2)
            return ""
        match = re.fullmatch(r"CREATE DATABASE (\w+) OWNER (\w+) ENCODING 'UTF8'", sql)
        if match:
            if match.group(1) in self.databases:
                raise RuntimeError(f'database "{match.group(1)}" already exists')
            self.databases.add(match.group(1))
            return ""
        raise AssertionError(f"unexpected statement: {sql!r}")


class FakeHost:
    """The commands, executables and services of one scripted machine."""

    def __init__(
        self,
        server: FakeServer,
        *,
        executables: tuple[str, ...] = ("git", "tmux", "psql", "sudo", "apt-get", "systemctl"),
        installed_packages: tuple[str, ...] = (),
        sudo: bool = True,
        boot_enabled: bool = False,
        service_starts: bool = True,
    ) -> None:
        self.server = server
        self.executables = set(executables)
        self.installed_packages = set(installed_packages)
        self.sudo = sudo
        self.boot_enabled = boot_enabled
        self.service_starts = service_starts
        self.commands: list[tuple[str, ...]] = []

    def which(self, command: str) -> str | None:
        return f"/usr/bin/{command}" if command in self.executables else None

    def probe(self, host: str, port: int, timeout: float) -> bool:
        return self.server.listening

    def connect(self, url: str, timeout: float) -> ConnectionCheck:
        return self.server.connect(url, timeout)

    def run(self, argv, *, env=None, stdin=None, timeout=None) -> CommandResult:
        argv = tuple(argv)
        self.commands.append(argv)
        joined = " ".join(argv)

        if argv[:3] == ("sudo", "-n", "true"):
            return CommandResult(
                argv, 0 if self.sudo else 1, "", "" if self.sudo else "sudo: a password is required"
            )
        if "psql" in joined:
            try:
                return CommandResult(argv, 0, self.server.execute(stdin or ""), "")
            except RuntimeError as error:
                return CommandResult(argv, 1, "", f"ERROR:  {error}")
        if "dpkg-query" in joined:
            package = argv[-1]
            if package in self.installed_packages:
                return CommandResult(argv, 0, "install ok installed", "")
            return CommandResult(argv, 1, "", f"dpkg-query: no packages found matching {package}")
        if "apt-get" in joined and "install" in joined:
            self.installed_packages.add(argv[-1])
            return CommandResult(argv, 0, f"Setting up {argv[-1]}", "")
        if "brew" in joined and argv[1:2] == ("list",):
            return CommandResult(argv, 0 if argv[-1] in self.installed_packages else 1, "17.0", "")
        if "brew" in joined and argv[1:2] == ("install",):
            self.installed_packages.add(argv[-1])
            return CommandResult(argv, 0, "", "")
        if "brew" in joined and argv[1:3] == ("services", "list"):
            state = "started" if self.boot_enabled else "none"
            return CommandResult(argv, 0, f"{argv[-1] if False else 'postgresql@17'} {state}", "")
        if "brew" in joined and argv[1:3] == ("services", "start"):
            self.boot_enabled = True
            self.server.listening = True
            return CommandResult(argv, 0, "", "")
        if "systemctl" in joined and "is-enabled" in joined:
            return CommandResult(
                argv,
                0 if self.boot_enabled else 1,
                "enabled" if self.boot_enabled else "disabled",
                "",
            )
        if "systemctl" in joined and "enable" in joined:
            self.boot_enabled = True
            return CommandResult(argv, 0, "", "")
        if ("systemctl" in joined or "service" in joined) and "start" in joined:
            if not self.service_starts:
                return CommandResult(argv, 1, "", "Job for postgresql.service failed")
            self.server.listening = True
            return CommandResult(argv, 0, "", "")
        return CommandResult(argv, 0, "", "")


def build(
    host: FakeHost,
    tmp_path: Path,
    *,
    support: SupportVerdict = WSL2,
    environ: dict[str, str] | None = None,
    path_exists=lambda path: True,
    passwords=("generated-password-1", "generated-password-2"),
):
    """A registry whose only host access goes through *host*."""
    passwords = list(passwords)
    adapter = PostgresAdapter(
        runner=host.run,
        which=host.which,
        probe=host.probe,
        connect=host.connect,
        environ=environ if environ is not None else {"USER": "installer"},
        state_dir=tmp_path / "data",
        path_exists=path_exists,
        sleep=lambda seconds: None,
        monotonic=lambda: 0.0,
        make_password=lambda: passwords.pop(0),
    )
    registry = default_registry(
        which=host.which, state_dir=tmp_path / "data", adapters=adapter.steps()
    )
    return registry, adapter


def run(
    host: FakeHost,
    tmp_path: Path,
    *,
    support: SupportVerdict = WSL2,
    capabilities: tuple[str, ...] = (),
    settings: dict | None = None,
    dry_run: bool = False,
    environ: dict[str, str] | None = None,
    path_exists=lambda path: True,
    passwords=("generated-password-1", "generated-password-2"),
    state: Path | None = None,
):
    registry, _ = build(
        host,
        tmp_path,
        support=support,
        environ=environ,
        path_exists=path_exists,
        passwords=passwords,
    )
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
    return InstallEngine(registry, options, support=support).run()


def step(result, step_id):
    return next(row for row in result.steps if row.step_id == step_id)


def provisioned_server() -> FakeServer:
    """A server that already has the AQ role, database and password."""
    return FakeServer(roles={"agent_queue": "existing-password"}, databases={"agent_queue"})


def env_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / ".env"


def config_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / "config.yaml"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_default_to_a_working_local_install():
    settings = PostgresSettings.from_options({})
    assert (settings.host, settings.port) == ("localhost", 5432)
    assert (settings.role, settings.database) == ("agent_queue", "agent_queue")
    assert settings.password_env == "AQ_DB_PASSWORD"


def test_an_unknown_postgres_setting_is_rejected_by_name():
    with pytest.raises(PostgresSettingsError) as error:
        PostgresSettings.from_options({"postgres": {"databse": "aq"}})
    assert "databse" in str(error.value)
    assert "database" in str(error.value), "the message lists what was recognised"


@pytest.mark.parametrize(
    "block",
    [
        {"role": "Robert'); DROP TABLE students;--"},
        {"database": "UPPER"},
        {"port": "not-a-port"},
        {"port": 0},
        {"service_manager": "launchd"},
        {"connect_timeout": -1},
    ],
)
def test_an_unusable_postgres_setting_is_refused_before_anything_runs(block):
    with pytest.raises(PostgresSettingsError):
        PostgresSettings.from_options({"postgres": block})


def test_a_bad_setting_fails_the_step_with_an_actionable_message(tmp_path):
    result = run(FakeHost(provisioned_server()), tmp_path, settings={"postgres": {"role": "NOPE"}})
    assert result.outcome is InstallOutcome.FAILED
    failure = result.blocking_step
    assert failure.step_id == STEP_SERVER
    assert "postgres.role" in failure.summary
    assert "install input" in failure.remediation


def test_the_configured_url_references_the_password_and_never_carries_it():
    settings = PostgresSettings()
    url = settings.sqlalchemy_url(password_reference="${AQ_DB_PASSWORD}")
    assert url == "postgresql+asyncpg://agent_queue:${AQ_DB_PASSWORD}@localhost:5432/agent_queue"
    assert "@" not in settings.redacted_url().split("//", 1)[1].split("/")[0].replace(
        "agent_queue@", ""
    )


def test_a_password_is_split_out_of_a_dsn_rather_than_passed_on_a_command_line():
    stripped, password = split_password("postgresql://admin:s3cr3t@db.internal:5432/postgres")
    assert stripped == "postgresql://admin@db.internal:5432/postgres"
    assert password == "s3cr3t"
    assert split_password("postgresql://admin@db/postgres") == (
        "postgresql://admin@db/postgres",
        None,
    )


def test_a_generated_password_survives_a_dsn_a_yaml_scalar_and_a_sql_literal():
    for _ in range(20):
        password = generate_password()
        assert re.fullmatch(r"[A-Za-z0-9_\-]{16,}", password)
        assert build_url(
            "postgresql", user="u", password=password, host="h", port=5432, database="d"
        ).endswith("/d")


# ---------------------------------------------------------------------------
# The fresh, managed install
# ---------------------------------------------------------------------------


def test_a_fresh_managed_install_reaches_a_healthy_database(tmp_path):
    """The headline criterion: a fresh host, no manual SQL, a working database."""
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))

    assert result.outcome is InstallOutcome.READY, result.blocking_step
    assert step(result, STEP_PACKAGE).state is StepState.SUCCEEDED
    assert step(result, STEP_SERVICE).state is StepState.SUCCEEDED
    assert step(result, STEP_ROLE).detail["created"] is True
    assert step(result, STEP_DATABASE).detail["created"] is True
    assert step(result, STEP_CONNECTION).state is StepState.SUCCEEDED

    assert host.server.roles == {"agent_queue": "generated-password-1"}
    assert host.server.databases == {"agent_queue"}
    assert read_env_file(env_file(tmp_path))["AQ_DB_PASSWORD"] == "generated-password-1"
    assert "${AQ_DB_PASSWORD}" in config_file(tmp_path).read_text(encoding="utf-8")


def test_the_installed_package_is_owned_and_the_shared_server_is_not(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    owned = {(row.kind, row.id) for row in result.resources if row.owned}
    assert ("postgres-server", "postgresql") in owned, "the package AQ installed is AQ's"
    assert ("postgres-role", "agent_queue") in owned
    assert ("postgres-database", "agent_queue") in owned
    assert ("postgres-credential", "AQ_DB_PASSWORD") in owned
    shared = next(row for row in result.resources if row.id == "localhost:5432")
    assert (shared.owned, shared.reused) == (False, True)


def test_the_password_never_reaches_the_result_or_the_resume_record(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    payload = json.dumps(result.to_dict())
    assert "generated-password-1" not in payload
    record = (tmp_path / "install-state.json").read_text(encoding="utf-8")
    assert "generated-password-1" not in record
    assert find_secrets(json.loads(record)) == []


def test_the_credential_file_is_owner_only(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    assert env_file(tmp_path).stat().st_mode & 0o777 == 0o600


def test_no_step_ever_runs_a_migration(tmp_path):
    """Schema work is the daemon's; the installer stops at an empty database."""
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    every_command = " ".join(" ".join(argv) for argv in host.commands).lower()
    assert "alembic" not in every_command
    assert "db upgrade" not in every_command
    assert not any(
        keyword in statement.upper()
        for statement in host.server.statements
        for keyword in ("CREATE TABLE", "ALTER TABLE", "DROP")
    )
    assert "aq db upgrade" in json.dumps(step(result, STEP_CONNECTION).detail)


# ---------------------------------------------------------------------------
# Reuse
# ---------------------------------------------------------------------------


def test_a_provisioned_server_is_reused_without_any_administrator_command(tmp_path):
    host = FakeHost(provisioned_server())
    write_env_value(env_file(tmp_path), "AQ_DB_PASSWORD", "existing-password")
    result = run(host, tmp_path)

    assert result.outcome is InstallOutcome.READY, result.blocking_step
    assert host.server.statements == [], "nothing was asked of the server but a connection"
    assert step(result, STEP_ROLE).detail["created"] is False
    assert step(result, STEP_DATABASE).detail["created"] is False
    assert all(not row.owned for row in result.resources if row.kind.startswith("postgres-role"))


def test_reuse_needs_no_managed_capability_and_installs_nothing(tmp_path):
    host = FakeHost(provisioned_server())
    write_env_value(env_file(tmp_path), "AQ_DB_PASSWORD", "existing-password")
    result = run(host, tmp_path)
    assert step(result, STEP_PACKAGE).state is StepState.SKIPPED
    assert "not selected" in step(result, STEP_PACKAGE).summary
    assert host.installed_packages == set()


def test_an_existing_role_is_never_reset(tmp_path):
    """A role AQ did not create keeps its password, whatever AQ wanted."""
    host = FakeHost(FakeServer(roles={"agent_queue": "someone-elses"}, databases=set()))
    result = run(host, tmp_path)

    assert host.server.roles == {"agent_queue": "someone-elses"}
    assert step(result, STEP_ROLE).detail["preserved"] is True
    failure = result.blocking_step
    assert failure.step_id == STEP_CREDENTIALS
    assert CAPABILITY_ROTATE in failure.remediation, "the recovery path is named"


def test_an_existing_database_is_reused_not_recreated(tmp_path):
    host = FakeHost(FakeServer(roles={}, databases={"agent_queue"}))
    run(host, tmp_path)
    assert not any(statement.startswith("CREATE DATABASE") for statement in host.server.statements)
    assert "agent_queue" in host.server.databases


def test_unrelated_roles_and_databases_are_never_touched(tmp_path):
    host = FakeHost(
        FakeServer(roles={"grafana": "g", "postgres": "p"}, databases={"grafana", "postgres"})
    )
    run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    assert host.server.roles["grafana"] == "g"
    assert host.server.roles["postgres"] == "p"
    assert {"grafana", "postgres"} <= host.server.databases


def test_an_existing_password_in_the_environment_is_used_as_it_is(tmp_path):
    host = FakeHost(provisioned_server())
    result = run(
        host, tmp_path, environ={"USER": "installer", "AQ_DB_PASSWORD": "existing-password"}
    )
    assert result.outcome is InstallOutcome.READY, result.blocking_step
    assert read_env_file(env_file(tmp_path))["AQ_DB_PASSWORD"] == "existing-password"


# ---------------------------------------------------------------------------
# Failure and recovery
# ---------------------------------------------------------------------------


def test_no_server_and_no_chosen_route_stops_with_both_routes_named(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    result = run(host, tmp_path)
    assert result.outcome is InstallOutcome.NEEDS_USER
    blocking = result.blocking_step
    assert blocking.step_id == STEP_SERVER
    assert CAPABILITY_MANAGED in blocking.remediation
    assert "postgres.admin_url" in blocking.remediation


def test_a_port_owned_by_something_else_is_reported_as_a_conflict(tmp_path):
    server = FakeServer(listening=True, speaks_postgres=False)
    host = FakeHost(server)
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    blocking = result.blocking_step
    assert blocking.step_id == STEP_SERVICE
    assert "5432" in blocking.remediation
    assert "postgres.port" in blocking.remediation


def test_a_service_that_will_not_start_points_at_the_servers_own_log(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()), service_starts=False)
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    blocking = result.blocking_step
    assert blocking.step_id == STEP_SERVICE
    assert "journalctl" in blocking.remediation


def test_a_server_older_than_the_floor_is_refused_and_not_retried(tmp_path):
    server = FakeServer(
        roles={"agent_queue": "existing-password"}, databases={"agent_queue"}, version_num=130010
    )
    host = FakeHost(server)
    write_env_value(env_file(tmp_path), "AQ_DB_PASSWORD", "wrong")
    result = run(host, tmp_path)
    blocking = result.blocking_step
    assert blocking.step_id == STEP_SERVER
    assert blocking.retryable is False
    assert "14" in blocking.summary


def test_a_missing_sudo_password_is_a_human_checkpoint_not_a_failure(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()), sudo=False)
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    assert result.outcome is InstallOutcome.NEEDS_USER
    blocking = result.blocking_step
    assert blocking.step_id == STEP_PACKAGE
    assert "sudo -v" in blocking.remediation


def test_no_administrator_route_names_the_admin_url_setting(tmp_path):
    host = FakeHost(
        FakeServer(roles={}, databases=set()),
        executables=("git", "tmux"),  # no psql, no sudo
    )
    result = run(host, tmp_path)
    assert result.outcome is InstallOutcome.NEEDS_USER
    blocking = result.blocking_step
    assert blocking.step_id == STEP_ROLE
    assert "postgres.admin_url" in blocking.remediation


def test_the_server_itself_proves_the_role_exists_without_an_administrator(tmp_path):
    """A rejected password names the role; that is proof enough not to ask an admin."""
    host = FakeHost(provisioned_server(), executables=("git", "tmux"))  # no psql, no sudo
    write_env_value(env_file(tmp_path), "AQ_DB_PASSWORD", "stale")
    result = run(host, tmp_path)
    role = step(result, STEP_ROLE)
    assert role.state is StepState.SUCCEEDED
    assert role.detail["preserved"] is True


def test_a_wrong_password_is_reported_as_a_credential_problem_not_a_missing_admin(tmp_path):
    host = FakeHost(provisioned_server(), executables=("git", "tmux"))
    write_env_value(env_file(tmp_path), "AQ_DB_PASSWORD", "stale")
    result = run(host, tmp_path)
    blocking = result.blocking_step
    assert blocking.step_id == STEP_DATABASE
    assert "rejecting the stored password" in blocking.summary
    assert CAPABILITY_ROTATE in blocking.remediation
    assert "administrator" not in blocking.remediation


def test_a_rejected_stored_password_names_the_rotation_path(tmp_path):
    host = FakeHost(provisioned_server())
    write_env_value(env_file(tmp_path), "AQ_DB_PASSWORD", "stale")
    result = run(host, tmp_path)
    blocking = result.blocking_step
    assert blocking.step_id == STEP_CREDENTIALS
    assert f"--with {CAPABILITY_ROTATE}" in blocking.remediation


def test_rotating_replaces_the_password_on_the_server_and_on_disk(tmp_path):
    host = FakeHost(provisioned_server())
    write_env_value(env_file(tmp_path), "AQ_DB_PASSWORD", "stale")
    result = run(host, tmp_path, capabilities=(CAPABILITY_ROTATE,))

    assert result.outcome is InstallOutcome.READY, result.blocking_step
    assert host.server.roles["agent_queue"] == "generated-password-1"
    assert read_env_file(env_file(tmp_path))["AQ_DB_PASSWORD"] == "generated-password-1"
    assert "stale" not in env_file(tmp_path).read_text(encoding="utf-8")
    assert step(result, STEP_ROTATE).detail["rotated"] is True


def test_rotation_is_opt_in_so_an_ordinary_rerun_keeps_the_password(tmp_path):
    host = FakeHost(provisioned_server())
    write_env_value(env_file(tmp_path), "AQ_DB_PASSWORD", "existing-password")
    result = run(host, tmp_path)
    assert step(result, STEP_ROTATE).state is StepState.SKIPPED
    assert host.server.roles["agent_queue"] == "existing-password"


# ---------------------------------------------------------------------------
# Restart, rerun and resume
# ---------------------------------------------------------------------------


def test_a_rerun_changes_nothing_and_records_the_same_resources(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    first = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    statements_after_first = list(host.server.statements)
    second = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))

    assert second.outcome is InstallOutcome.READY, second.blocking_step
    assert host.server.statements == statements_after_first, "a rerun issues no new statements"
    assert {(r.kind, r.id) for r in first.resources} == {(r.kind, r.id) for r in second.resources}
    assert step(second, STEP_ROLE).detail.get("revalidated") is True


def test_a_rerun_after_a_restart_reconnects_without_changing_anything(tmp_path):
    """The machine rebooted: the service comes back and AQ reconnects."""
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))

    host.server.listening = False  # the box restarted; the service is not up yet
    after = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))

    assert after.outcome is InstallOutcome.READY, after.blocking_step
    assert step(after, STEP_SERVICE).state is StepState.SUCCEEDED
    assert host.server.listening is True
    assert step(after, STEP_CONNECTION).state is StepState.SUCCEEDED
    assert host.server.roles == {"agent_queue": "generated-password-1"}


def test_restart_from_the_role_step_does_not_recreate_what_exists(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    registry, _ = build(host, tmp_path)
    run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))

    registry, _ = build(host, tmp_path, passwords=("generated-password-2",))
    options = InstallOptions(
        installer_version="1.2.3",
        target_version="1.2.3",
        interactive=False,
        capabilities=frozenset({CAPABILITY_MANAGED}),
        approve=frozenset({"*"}),
        restart_from=STEP_ROLE,
        state_path=tmp_path / "install-state.json",
    )
    result = InstallEngine(registry, options, support=WSL2).run()
    assert result.outcome is InstallOutcome.READY, result.blocking_step
    assert host.server.roles == {"agent_queue": "generated-password-1"}, "no password was reset"


def test_a_dry_run_touches_nothing_and_reports_what_it_would_do(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,), dry_run=True)

    assert result.state_path is None
    assert host.server.roles == {}
    assert host.installed_packages == set()
    assert not env_file(tmp_path).exists()
    actions = {row.step_id: row.action.value for row in result.plan}
    assert actions[STEP_PACKAGE] == "would_run"
    assert actions[STEP_ROLE] == "would_run"
    assert result.outcome is not InstallOutcome.FAILED


# ---------------------------------------------------------------------------
# Service lifecycle and restart behaviour
# ---------------------------------------------------------------------------


def test_the_boot_step_enables_the_service_on_systemd(tmp_path):
    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))
    boot = step(result, STEP_BOOT)
    assert boot.state is StepState.SUCCEEDED
    assert boot.detail["boot_enabled"] is True
    assert host.boot_enabled is True
    assert any("enable" in " ".join(argv) for argv in host.commands)


def test_a_wsl_distribution_without_systemd_asks_the_human(tmp_path):
    host = FakeHost(
        FakeServer(listening=False, roles={}, databases=set()),
        executables=("git", "tmux", "psql", "sudo", "apt-get", "service"),
    )
    result = run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,), path_exists=lambda path: False)
    boot = step(result, STEP_BOOT)
    assert boot.state is StepState.NEEDS_USER
    assert "wsl.conf" in boot.remediation
    assert "systemd=true" in boot.remediation
    assert result.outcome is InstallOutcome.NEEDS_USER


def test_the_boot_step_runs_after_the_database_is_proven(tmp_path):
    """Restart-persistence is checked last, so it never blocks provisioning."""
    registry, _ = build(FakeHost(provisioned_server()), tmp_path)
    order = [item.id for item in registry.ordered()]
    assert order.index(STEP_BOOT) == len(order) - 1
    assert order.index(STEP_CONNECTION) < order.index(STEP_BOOT)


def test_macos_uses_homebrew_for_the_package_and_the_service(tmp_path):
    host = FakeHost(
        FakeServer(listening=False, roles={}, databases=set()),
        executables=("git", "tmux", "psql", "brew"),
    )
    result = run(host, tmp_path, support=MACOS, capabilities=(CAPABILITY_MANAGED,))
    assert result.outcome is InstallOutcome.READY, result.blocking_step
    assert "postgresql@17" in host.installed_packages
    assert step(result, STEP_BOOT).detail["boot_enabled"] is True
    assert "at login" in step(result, STEP_BOOT).summary


def test_an_externally_managed_service_is_left_alone(tmp_path):
    host = FakeHost(provisioned_server())
    write_env_value(env_file(tmp_path), "AQ_DB_PASSWORD", "existing-password")
    result = run(
        host,
        tmp_path,
        capabilities=(CAPABILITY_MANAGED,),
        settings={"postgres": {"service_manager": "none"}},
    )
    assert result.outcome is InstallOutcome.READY, result.blocking_step
    assert step(result, STEP_BOOT).state is StepState.SKIPPED


# ---------------------------------------------------------------------------
# Platform plans and administrator routes
# ---------------------------------------------------------------------------


def test_ubuntu_installs_the_distribution_package_noninteractively():
    plan = package_plan(
        PostgresSettings(), host_path="windows-wsl2", which=lambda name: f"/usr/bin/{name}"
    )
    assert plan.manager == "apt"
    assert plan.package == "postgresql"
    assert "DEBIAN_FRONTEND=noninteractive" in plan.install_argv
    assert "-y" in plan.install_argv


def test_a_requested_major_version_selects_the_versioned_package():
    apt = package_plan(
        PostgresSettings(version="16"),
        host_path="windows-wsl2",
        which=lambda name: f"/usr/bin/{name}",
    )
    brew = package_plan(
        PostgresSettings(version="16"),
        host_path="macos-apple-silicon",
        which=lambda name: f"/opt/homebrew/bin/{name}",
    )
    assert apt.package == "postgresql-16"
    assert brew.package == "postgresql@16"


def test_a_host_with_no_package_manager_has_no_managed_install():
    assert (
        package_plan(PostgresSettings(), host_path="macos-intel", which=lambda name: None) is None
    )


def test_the_service_plan_prefers_systemd_and_falls_back_to_the_service_shim():
    available = {"systemctl", "service"}
    systemd = service_plan(
        PostgresSettings(),
        host_path="windows-wsl2",
        which=lambda name: f"/usr/bin/{name}" if name in available else None,
        path_exists=lambda path: True,
    )
    sysv = service_plan(
        PostgresSettings(),
        host_path="windows-wsl2",
        which=lambda name: f"/usr/bin/{name}" if name in available else None,
        path_exists=lambda path: False,
    )
    assert systemd.manager == "systemd"
    assert systemd.boot_needs_user is False
    assert sysv.manager == "sysv"
    assert sysv.boot_needs_user is True


def test_the_local_administrator_route_is_the_postgres_user_on_ubuntu():
    host = FakeHost(FakeServer())
    admin = resolve_admin(
        PostgresSettings(),
        host_path="windows-wsl2",
        runner=host.run,
        which=host.which,
        environ={"USER": "installer"},
    )
    assert isinstance(admin.executor, PsqlExecutor)
    assert admin.executor.prefix[:4] == ("sudo", "-n", "-u", "postgres")


def test_the_local_administrator_route_is_the_invoking_user_on_homebrew():
    host = FakeHost(FakeServer(), executables=("psql",))
    admin = resolve_admin(
        PostgresSettings(),
        host_path="macos-apple-silicon",
        runner=host.run,
        which=host.which,
        environ={"USER": "ada"},
    )
    assert isinstance(admin.executor, PsqlExecutor)
    assert "ada" in admin.executor.prefix


def test_an_explicit_admin_url_wins_and_is_reported_without_its_password():
    host = FakeHost(FakeServer())
    admin = resolve_admin(
        PostgresSettings(admin_url="postgresql://root:hunter2@db.internal:5432/postgres"),
        host_path="windows-wsl2",
        runner=host.run,
        which=host.which,
        environ={},
    )
    assert isinstance(admin.executor, AsyncpgExecutor)
    assert "hunter2" not in admin.label


def test_a_statement_is_piped_in_rather_than_placed_in_the_process_table():
    host = FakeHost(FakeServer(roles={"agent_queue": "x"}))
    executor = PsqlExecutor(runner=host.run, prefix=("/usr/bin/psql",), label="psql")
    assert executor.scalar("SELECT 1 FROM pg_roles WHERE rolname = 'agent_queue'") == "1"
    argv = host.commands[-1]
    assert "agent_queue" not in " ".join(argv), "the statement is not visible in `ps`"
    assert "--set" in argv and "ON_ERROR_STOP=1" in argv


# ---------------------------------------------------------------------------
# Connection classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("InvalidPasswordError", REASON_AUTH),
        ("InvalidCatalogNameError", REASON_MISSING_DATABASE),
        ("ProtocolViolationError", REASON_NOT_POSTGRES),
    ],
)
def test_a_driver_error_is_translated_into_the_operators_question(monkeypatch, error, expected):
    import asyncpg
    from asyncpg import exceptions as pg_errors

    from src.install.postgres import asyncpg_check

    async def _raise(*args, **kwargs):
        raise getattr(pg_errors, error)("boom")

    monkeypatch.setattr(asyncpg, "connect", _raise)
    check = asyncpg_check("postgresql://agent_queue@localhost:5432/agent_queue", 1.0)
    assert check.reason == expected
    assert check.ok is False


def test_a_refused_connection_is_unreachable_rather_than_an_error(monkeypatch):
    import asyncpg

    from src.install.postgres import asyncpg_check

    async def _raise(*args, **kwargs):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(asyncpg, "connect", _raise)
    assert asyncpg_check("postgresql://a@localhost:5432/b", 1.0).reason == REASON_UNREACHABLE


def test_a_missing_role_is_told_apart_from_a_wrong_password(monkeypatch):
    import asyncpg
    from asyncpg import exceptions as pg_errors

    from src.install.postgres import asyncpg_check

    async def _raise(*args, **kwargs):
        raise pg_errors.InvalidAuthorizationSpecificationError('role "agent_queue" does not exist')

    monkeypatch.setattr(asyncpg, "connect", _raise)
    assert asyncpg_check("postgresql://a@localhost:5432/b", 1.0).reason == REASON_MISSING_ROLE


def test_the_version_floor_is_read_from_the_server_not_guessed():
    assert ConnectionCheck(REASON_OK, server_version_num=140000).supported_version is True
    assert ConnectionCheck(REASON_OK, server_version_num=130010).supported_version is False
    assert ConnectionCheck(REASON_OK).supported_version is True, "unknown is not a refusal"


# ---------------------------------------------------------------------------
# Configuration and credential files
# ---------------------------------------------------------------------------


def test_writing_the_credential_replaces_the_key_and_keeps_the_rest(tmp_path):
    path = tmp_path / ".env"
    path.write_text("DISCORD_TOKEN=abc\nAQ_DB_PASSWORD=old\nOTHER=1\n", encoding="utf-8")
    write_env_value(path, "AQ_DB_PASSWORD", "new")
    values = read_env_file(path)
    assert values == {"DISCORD_TOKEN": "abc", "AQ_DB_PASSWORD": "new", "OTHER": "1"}
    assert path.read_text(encoding="utf-8").count("AQ_DB_PASSWORD") == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_updating_the_config_backs_it_up_and_preserves_everything_else(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# operator's own note\n"
        "database:\n  url: postgresql://old\n  pool_max_size: 42\n"
        "resources:\n  max_concurrent_agents: 3\n",
        encoding="utf-8",
    )
    update = write_database_config(path, url="postgresql+asyncpg://agent_queue:${P}@h:5432/d")

    assert update.backup is not None and update.backup.exists()
    assert "postgresql://old" in update.backup.read_text(encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert "operator's own note" in text
    assert "max_concurrent_agents: 3" in text
    assert "pool_max_size: 42" in text, "keys the installer does not own survive"
    assert "${P}" in text


def test_an_unchanged_url_is_not_rewritten_and_takes_no_backup(tmp_path):
    path = tmp_path / "config.yaml"
    url = "postgresql+asyncpg://agent_queue:${AQ_DB_PASSWORD}@localhost:5432/agent_queue"
    write_database_config(path, url=url)
    again = write_database_config(path, url=url)
    assert again.changed is False
    assert again.backup is None
    assert list(tmp_path.glob("config.yaml.bak*")) == []


def test_backups_are_numbered_rather_than_overwritten(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("database:\n  url: a\n", encoding="utf-8")
    first = backup_path_for(path)
    first.write_text("", encoding="utf-8")
    assert backup_path_for(path).name == "config.yaml.bak.1"


def test_the_written_config_loads_and_resolves_the_password_reference(tmp_path, monkeypatch):
    """The point of the ${ENV_VAR} indirection: the daemon must be able to read it."""
    from src.config import load_config

    host = FakeHost(FakeServer(listening=False, roles={}, databases=set()))
    run(host, tmp_path, capabilities=(CAPABILITY_MANAGED,))

    monkeypatch.setenv("AQ_DB_PASSWORD", "generated-password-1")
    config = load_config(str(config_file(tmp_path)))
    assert config.database.url == (
        "postgresql+asyncpg://agent_queue:generated-password-1@localhost:5432/agent_queue"
    )


# ---------------------------------------------------------------------------
# The published contract
# ---------------------------------------------------------------------------


def test_the_documented_steps_and_capabilities_match_the_registry():
    """``docs/reference/cli/install.md`` is the published surface, not a summary."""
    text = DOC.read_text(encoding="utf-8")
    registry = default_registry()
    for step_id in (
        STEP_PACKAGE,
        STEP_SERVICE,
        STEP_SERVER,
        STEP_ROLE,
        STEP_DATABASE,
        STEP_ROTATE,
        STEP_CREDENTIALS,
        STEP_CONNECTION,
        STEP_BOOT,
    ):
        assert step_id in registry, f"{step_id} is not registered"
        assert step_id in text, f"{step_id} is not documented"
    assert CAPABILITY_MANAGED in text
    assert CAPABILITY_ROTATE in text
    assert set(registry.capabilities()) == {CAPABILITY_MANAGED, CAPABILITY_ROTATE}


def test_every_documented_postgres_setting_is_one_the_parser_accepts():
    """The settings table is the operator's only list; it may not invent a key."""
    text = DOC.read_text(encoding="utf-8")
    table = text.split("### Settings", 1)[1].split("\n### ", 1)[0]
    documented = set(re.findall(r"^\| `postgres\.(\w+)`", table, re.MULTILINE))
    known = set(PostgresSettings.__dataclass_fields__)
    assert documented, "the settings table is missing"
    assert documented <= known, f"documented but unknown: {sorted(documented - known)}"
    assert known <= documented, f"undocumented settings: {sorted(known - documented)}"
