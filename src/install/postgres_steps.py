"""The installer's PostgreSQL steps (``noble-apex.6``).

``aq install`` reaches a healthy AQ database through nine steps.  Three of
them are gated on the ``postgres-managed`` capability and only ever touch a
server AQ installed; the rest work identically against a server the operator
already runs:

======================== ========= ==================================================
step                      mutating  what it is responsible for
======================== ========= ==================================================
``postgres.package``      yes       Install a local server (apt / Homebrew).
``postgres.service``      yes       Start it, and diagnose a port conflict.
``postgres.server``       no        Reach *a* server and admit it (version floor).
``postgres.role``         yes       Create the AQ login role if it is missing.
``postgres.database``     yes       Create the AQ database if it is missing.
``postgres.rotate``       yes       Replace the role's password (opt-in recovery).
``postgres.credentials``  yes       Store the password and point config.yaml at it.
``postgres.connection``   no        Connect as AQ and report the schema boundary.
``postgres.boot``         yes       Make the managed server start again after a restart.
======================== ========= ==================================================

Three properties are worth stating because every step body is written to
preserve them:

**Nothing existing is modified.**  A reachable server is admitted, not
reconfigured.  A role that already exists keeps its password — the installer
never resets a credential it did not create, because on a shared server that
role may be in use.  A database that already exists is never dropped,
re-owned or re-encoded.  Each of those is recorded as ``reused`` and
``owned: false``, which is what keeps a later uninstall away from it.

**A working database short-circuits the whole group.**  Every provisioning
step asks first whether the stored AQ credential already connects; if it does,
the step reports what it found and changes nothing.  That is what makes a
rerun cheap, and it is why pointing the installer at a fully provisioned
server is a supported first-class path rather than an accident.

**The schema is not ours.**  ``postgres.connection`` verifies connectivity and
then explicitly stops, naming ``aq db upgrade`` as the operator's step.  No
code path here imports Alembic — an installer that migrated an operator's
database would be the very failure ``src/database/migration_guard.py`` exists
to prevent.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .postgres import (
    MINIMUM_SERVER_VERSION,
    REASON_AUTH,
    REASON_MISSING_DATABASE,
    REASON_MISSING_ROLE,
    REASON_NOT_POSTGRES,
    RESOURCE_CREDENTIAL,
    RESOURCE_DATABASE,
    RESOURCE_ROLE,
    RESOURCE_SERVER,
    AdminAccess,
    CommandRunner,
    ConnectionCheck,
    Connector,
    PostgresSettings,
    PostgresSettingsError,
    ServicePlan,
    SqlError,
    SqlExecutor,
    asyncpg_check,
    generate_password,
    package_plan,
    package_present,
    quote_literal,
    read_env_file,
    resolve_admin,
    service_enabled_at_boot,
    service_plan,
    subprocess_runner,
    tcp_open,
    write_database_config,
    write_env_value,
)
from .results import ResourceRecord, StepResult
from .state import default_state_dir
from .steps import StepContext, StepRunner, StepSpec

STEP_PACKAGE = "postgres.package"
STEP_SERVICE = "postgres.service"
STEP_SERVER = "postgres.server"
STEP_ROLE = "postgres.role"
STEP_DATABASE = "postgres.database"
STEP_ROTATE = "postgres.rotate"
STEP_CREDENTIALS = "postgres.credentials"
STEP_CONNECTION = "postgres.connection"
STEP_BOOT = "postgres.boot"

#: Opt-in: AQ installs and runs a local server for you.  Without it the
#: installer only ever *uses* a server that is already there, which is what
#: makes an unattended install against managed PostgreSQL safe by default.
CAPABILITY_MANAGED = "postgres-managed"

#: Opt-in: replace the AQ role's password on this run.  This is the documented
#: recovery path for a lost or changed credential, and it is a capability
#: rather than a default so that no ordinary rerun can invalidate a password
#: something else is using.
CAPABILITY_ROTATE = "postgres-rotate"

OWNER = "noble-apex.6"

_MANAGED_ROUTES = (
    "Either let AQ install and run a local server (`aq install --with "
    f"{CAPABILITY_MANAGED}`), or point it at a server you already run with the "
    "`postgres.host` / `postgres.port` / `postgres.admin_url` install settings."
)


class PostgresAdapter:
    """Builds the PostgreSQL steps, sharing one run's cached observations.

    The adapter is stateful on purpose and its state is small: the parsed
    settings, whether the AQ credential connects, the administrator route, and
    a password generated on this run that has to reach the credentials step
    without ever passing through the resume record.  Everything that touches
    the host — running a command, opening a socket, connecting, reading the
    clock — is a constructor argument with a real default, so the whole
    adapter runs in a test with no PostgreSQL anywhere.
    """

    def __init__(
        self,
        *,
        runner: CommandRunner = subprocess_runner,
        which: Callable[[str], str | None] = shutil.which,
        probe: Callable[[str, int, float], bool] = tcp_open,
        connect: Connector = asyncpg_check,
        environ: Mapping[str, str] | None = None,
        state_dir: Path | None = None,
        path_exists: Callable[[str], bool] = os.path.isdir,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        make_password: Callable[[], str] = generate_password,
    ) -> None:
        self._runner = runner
        self._which = which
        self._probe = probe
        self._connect = connect
        self._environ = dict(os.environ if environ is None else environ)
        self._state_dir = state_dir
        self._path_exists = path_exists
        self._sleep = sleep
        self._monotonic = monotonic
        self._make_password = make_password

        self._settings_cache: PostgresSettings | None = None
        self._admin_cache: AdminAccess | None = None
        self._check_cache: dict[str, ConnectionCheck] = {}
        #: Set when this run generated a password that is not on disk yet.
        self._new_password: str | None = None

    # -- shared observations ------------------------------------------------
    def settings(self, context: StepContext) -> PostgresSettings:
        if self._settings_cache is None:
            self._settings_cache = PostgresSettings.from_options(context.options)
        return self._settings_cache

    def data_dir(self) -> Path:
        return self._state_dir or default_state_dir(self._environ)

    def env_path(self) -> Path:
        return self.data_dir() / ".env"

    def config_path(self) -> Path:
        return self.data_dir() / "config.yaml"

    def stored_password(self, settings: PostgresSettings) -> str | None:
        """The AQ role's password, from this run or from the protected store."""
        if self._new_password:
            return self._new_password
        from_env = (self._environ.get(settings.password_env) or "").strip()
        if from_env:
            return from_env
        return (read_env_file(self.env_path()).get(settings.password_env) or "").strip() or None

    def aq_check(self, settings: PostgresSettings, *, refresh: bool = False) -> ConnectionCheck:
        """Connect as the AQ role to the AQ database, once per run per URL."""
        url = settings.libpq_url(self.stored_password(settings))
        if refresh:
            self._check_cache.pop(url, None)
        cached = self._check_cache.get(url)
        if cached is None:
            cached = self._connect(url, settings.connect_timeout)
            self._check_cache[url] = cached
        return cached

    def aq_ready(self, settings: PostgresSettings, *, refresh: bool = False) -> bool:
        return self.aq_check(settings, refresh=refresh).ok

    def admin(self, context: StepContext) -> AdminAccess:
        if self._admin_cache is None:
            self._admin_cache = resolve_admin(
                self.settings(context),
                host_path=context.support.host_path,
                runner=self._runner,
                which=self._which,
                environ=self._environ,
            )
        return self._admin_cache

    def server_reachable(self, settings: PostgresSettings) -> bool:
        return self._probe(settings.host, settings.port, settings.connect_timeout)

    # -- steps --------------------------------------------------------------
    def steps(self) -> tuple[StepSpec, ...]:
        """The steps, in the order they must run."""
        return (
            self._package_step(),
            self._service_step(),
            self._server_step(),
            self._role_step(),
            self._database_step(),
            self._rotate_step(),
            self._credentials_step(),
            self._connection_step(),
            self._boot_step(),
        )

    def _guard(self, step_id: str, run: StepRunner) -> StepRunner:
        """Turn a bad ``postgres.*`` setting into a named, actionable failure.

        Without this the engine would report the raised exception, which names
        the adapter rather than the install input the operator has to fix.
        """

        def guarded(context: StepContext) -> StepResult:
            try:
                return run(context)
            except PostgresSettingsError as error:
                return StepResult.failed(
                    step_id,
                    str(error),
                    (
                        "Correct the `settings.postgres` block of the install input file "
                        "(`aq install --config …`) and rerun."
                    ),
                    retryable=False,
                )

        return guarded

    # -- postgres.package ---------------------------------------------------
    def _package_step(self) -> StepSpec:
        def run(context: StepContext) -> StepResult:
            settings = self.settings(context)
            if self.server_reachable(settings):
                return StepResult.succeeded(
                    STEP_PACKAGE,
                    f"a server is already listening on {settings.server_label}; "
                    "nothing was installed",
                    detail={"installed": False, "reused": True},
                )
            plan = package_plan(settings, host_path=context.support.host_path, which=self._which)
            if plan is None:
                return StepResult.failed(
                    STEP_PACKAGE,
                    "no supported package manager for a managed PostgreSQL install was found",
                    (
                        "Install PostgreSQL with the method your platform documents "
                        "(https://www.postgresql.org/download/), then rerun `aq install` "
                        f"without --with {CAPABILITY_MANAGED}."
                    ),
                    detail={"host_path": context.support.host_path},
                )
            if package_present(plan, self._runner):
                return StepResult.succeeded(
                    STEP_PACKAGE,
                    f"{plan.label} is already installed",
                    detail={"manager": plan.manager, "package": plan.package, "installed": False},
                    resources=(self._package_resource(plan, owned=False),),
                )
            if plan.requires_sudo and not self._sudo_ready():
                return StepResult.needs_user(
                    STEP_PACKAGE,
                    f"installing {plan.label} needs root and sudo would prompt for a password",
                    (
                        "Run `sudo -v` in this terminal and rerun `aq install`, or install "
                        f"the package yourself with `sudo apt-get install -y {plan.package}`."
                    ),
                    detail={"manager": plan.manager, "package": plan.package},
                )
            result = self._runner(list(plan.install_argv), timeout=1800.0)
            if not result.ok:
                return StepResult.failed(
                    STEP_PACKAGE,
                    f"installing {plan.label} failed: {result.diagnostic()}",
                    (
                        f"Run `{' '.join(plan.install_argv)}` by hand to see the package "
                        "manager's own output, resolve what it reports, then rerun "
                        "`aq install`."
                    ),
                    detail={"manager": plan.manager, "package": plan.package},
                )
            return StepResult.succeeded(
                STEP_PACKAGE,
                f"installed {plan.label}",
                detail={"manager": plan.manager, "package": plan.package, "installed": True},
                resources=(self._package_resource(plan, owned=True),),
            )

        def verify(context: StepContext) -> bool:
            settings = self.settings(context)
            if self.server_reachable(settings):
                return True
            plan = package_plan(settings, host_path=context.support.host_path, which=self._which)
            return plan is not None and package_present(plan, self._runner)

        return StepSpec(
            id=STEP_PACKAGE,
            title="Install a local PostgreSQL server",
            description=(
                "Installs PostgreSQL with the platform's package manager (apt on Ubuntu, "
                "Homebrew on macOS) when no server is already reachable. Selected with "
                f"--with {CAPABILITY_MANAGED}; without it AQ only uses a server you already run."
            ),
            run=self._guard(STEP_PACKAGE, run),
            depends_on=("host.supported",),
            capability=CAPABILITY_MANAGED,
            mutating=True,
            consent_prompt="Install PostgreSQL with this machine's package manager?",
            verify=verify,
            owner=OWNER,
        )

    def _package_resource(self, plan: Any, *, owned: bool) -> ResourceRecord:
        return ResourceRecord(
            kind=RESOURCE_SERVER,
            id=plan.package,
            owned=owned,
            reused=not owned,
            detail={"manager": plan.manager},
        )

    def _sudo_ready(self) -> bool:
        return self._runner(["sudo", "-n", "true"], timeout=10.0).ok

    # -- postgres.service ---------------------------------------------------
    def _service_step(self) -> StepSpec:
        def run(context: StepContext) -> StepResult:
            settings = self.settings(context)
            plan = self._service(context, settings)
            # The conflict check comes first: a listener that is not PostgreSQL
            # would otherwise read as "already running" and the real problem
            # would only surface later, as an inexplicable connection failure.
            conflict = self._port_conflict(settings)
            if conflict is not None:
                return conflict
            if self.server_reachable(settings):
                return self._service_running_result(settings, plan)
            if plan.manager == "none" or not plan.start_argv:
                return StepResult.needs_user(
                    STEP_SERVICE,
                    f"nothing is listening on {settings.server_label} and no supported "
                    "service manager was found",
                    (
                        "Start PostgreSQL the way this host expects and rerun `aq install`. "
                        "Set the `postgres.service_manager` install setting to `none` if the "
                        "server's lifecycle is managed elsewhere."
                    ),
                    detail={"service_manager": plan.manager},
                )
            if plan.start_argv[0] == "sudo" and not self._sudo_ready():
                return StepResult.needs_user(
                    STEP_SERVICE,
                    f"starting {plan.label} needs root and sudo would prompt for a password",
                    (
                        "Run `sudo -v` in this terminal and rerun `aq install`, or start the "
                        f"server yourself with `{' '.join(plan.start_argv)}`."
                    ),
                    detail={"service_manager": plan.manager},
                )
            result = self._runner(list(plan.start_argv), timeout=300.0)
            if not result.ok:
                return StepResult.failed(
                    STEP_SERVICE,
                    f"starting {plan.label} failed: {result.diagnostic()}",
                    self._service_failure_advice(plan),
                    detail={"service_manager": plan.manager},
                )
            if not self._wait_for_server(settings):
                return StepResult.failed(
                    STEP_SERVICE,
                    f"{plan.label} started but nothing accepted a connection on "
                    f"{settings.server_label} within {settings.startup_timeout:.0f}s",
                    self._service_failure_advice(plan),
                    detail={"service_manager": plan.manager},
                )
            return self._service_running_result(settings, plan, started=True)

        return StepSpec(
            id=STEP_SERVICE,
            title="Start the local PostgreSQL service",
            description=(
                "Starts the managed server and waits for it to accept connections, "
                "reporting a port conflict as a conflict rather than as a timeout."
            ),
            run=self._guard(STEP_SERVICE, run),
            depends_on=(STEP_PACKAGE,),
            capability=CAPABILITY_MANAGED,
            mutating=True,
            consent_prompt="Start the PostgreSQL service?",
            verify=self._server_verified,
            owner=OWNER,
        )

    def _service(self, context: StepContext, settings: PostgresSettings) -> ServicePlan:
        return service_plan(
            settings,
            host_path=context.support.host_path,
            which=self._which,
            path_exists=self._path_exists,
            package=package_plan(settings, host_path=context.support.host_path, which=self._which),
        )

    def _service_running_result(
        self, settings: PostgresSettings, plan: ServicePlan, *, started: bool = False
    ) -> StepResult:
        verb = "started" if started else "is already running"
        return StepResult.succeeded(
            STEP_SERVICE,
            f"PostgreSQL {verb} and is accepting connections on {settings.server_label}",
            detail={"service_manager": plan.manager, "started": started},
        )

    def _service_failure_advice(self, plan: ServicePlan) -> str:
        if plan.manager == "systemd":
            return (
                "Read the server's own log with `journalctl -u postgresql -n 50`, fix what "
                "it reports (a port already in use and a half-initialised data directory are "
                "the common causes), then rerun `aq install`."
            )
        if plan.manager == "brew":
            return (
                f"Read the server's log with `brew services info {plan.unit}` and the file it "
                "names, fix what it reports, then rerun `aq install`."
            )
        return (
            "Start the server by hand to see its own error output, fix what it reports, "
            "then rerun `aq install`."
        )

    def _wait_for_server(self, settings: PostgresSettings) -> bool:
        deadline = self._monotonic() + settings.startup_timeout
        while True:
            if self.server_reachable(settings):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(1.0)

    def _port_conflict(self, settings: PostgresSettings) -> StepResult | None:
        """Refuse to start a server onto a port something else already owns."""
        if not self._probe(settings.host, settings.port, settings.connect_timeout):
            return None
        check = self._connect(settings.libpq_url(self.stored_password(settings)), 2.0)
        if check.answered:
            return None
        return StepResult.failed(
            STEP_SERVICE,
            f"something that is not PostgreSQL is already listening on {settings.server_label}",
            (
                f"Find the listener (`ss -ltnp 'sport = :{settings.port}'` on Linux, "
                f"`lsof -nP -iTCP:{settings.port} -sTCP:LISTEN` on macOS) and stop it, or give "
                "AQ a different port with the `postgres.port` install setting."
            ),
            detail={"port": settings.port, "probe": check.to_detail()},
        )

    # -- postgres.server ----------------------------------------------------
    def _server_step(self) -> StepSpec:
        def run(context: StepContext) -> StepResult:
            settings = self.settings(context)
            check = self.aq_check(settings, refresh=True)
            if check.ok:
                return StepResult.succeeded(
                    STEP_SERVER,
                    f"{self._version_label(check)} at {settings.server_label} already serves "
                    "the AQ database",
                    detail={"server": settings.redacted_url(), **check.to_detail()},
                    resources=(self._server_resource(settings, check),),
                )
            if not self.server_reachable(settings):
                if context.dry_run:
                    return StepResult.skipped(
                        STEP_SERVER,
                        f"dry run — nothing is listening on {settings.server_label} yet and "
                        "no mutating step was executed",
                        detail={"server_present": False},
                    )
                return StepResult.needs_user(
                    STEP_SERVER,
                    f"no PostgreSQL server is reachable at {settings.server_label}",
                    _MANAGED_ROUTES,
                    detail={"server_present": False, **check.to_detail()},
                )
            check = self._with_server_version(context, check)
            if check.reason == REASON_NOT_POSTGRES:
                return StepResult.failed(
                    STEP_SERVER,
                    f"the service on {settings.server_label} did not answer as PostgreSQL",
                    (
                        "Point AQ at the right port with the `postgres.port` install setting, "
                        "or stop whatever is listening there."
                    ),
                    detail=check.to_detail(),
                )
            if not check.supported_version:
                return StepResult.failed(
                    STEP_SERVER,
                    f"{self._version_label(check)} at {settings.server_label} is older than "
                    f"the supported PostgreSQL {MINIMUM_SERVER_VERSION}",
                    (
                        f"Upgrade the server to PostgreSQL {MINIMUM_SERVER_VERSION} or newer, "
                        "or point AQ at a newer instance with the `postgres.host` / "
                        "`postgres.port` install settings."
                    ),
                    detail=check.to_detail(),
                    retryable=False,
                )
            return StepResult.succeeded(
                STEP_SERVER,
                f"a PostgreSQL server answered on {settings.server_label} "
                f"({self._describe_gap(check)})",
                detail={"server": settings.redacted_url(), **check.to_detail()},
                resources=(self._server_resource(settings, check),),
            )

        return StepSpec(
            id=STEP_SERVER,
            title="Reach a PostgreSQL server",
            description=(
                "Confirms that a PostgreSQL server answers on the configured host and port "
                "and that it is new enough for AQ. Read-only: it admits a server, it never "
                "changes one."
            ),
            run=self._guard(STEP_SERVER, run),
            depends_on=(STEP_SERVICE,),
            verify=self._server_verified,
            owner=OWNER,
        )

    def _server_verified(self, context: StepContext) -> bool:
        settings = self.settings(context)
        if not self.server_reachable(settings):
            return False
        return self.aq_check(settings, refresh=True).answered

    def _server_resource(
        self, settings: PostgresSettings, check: ConnectionCheck
    ) -> ResourceRecord:
        """The server itself is only ever *reused*.

        Even when ``postgres.package`` installed the packages, the running
        server is a host service AQ shares with everything else on the box: the
        package is the owned resource, and that step records it.
        """
        return ResourceRecord(
            kind=RESOURCE_SERVER,
            id=settings.server_label,
            owned=False,
            reused=True,
            detail={
                "host": settings.host,
                "port": settings.port,
                "server_version": check.server_version,
            },
        )

    def _with_server_version(self, context: StepContext, check: ConnectionCheck) -> ConnectionCheck:
        """Fill in the server version when the AQ role could not log in.

        The version floor has to be checked before the installer starts
        creating things, and a wrong password or a missing database must not
        be the reason an unsupported server is admitted. The administrator
        route knows the answer; when there is no administrator route the
        version stays unknown, which the floor treats as "not a refusal".
        """
        if check.server_version_num is not None:
            return check
        executor = self.admin(context).executor
        if executor is None:
            return check
        try:
            raw = executor.scalar("SELECT current_setting('server_version_num')")
        except SqlError:
            return check
        text = (raw or "").strip()
        if not text.isdigit():
            return check
        number = int(text)
        return replace(
            check,
            server_version_num=number,
            server_version=check.server_version or f"PostgreSQL {number // 10000}",
        )

    def _version_label(self, check: ConnectionCheck) -> str:
        return check.server_version or "PostgreSQL"

    def _describe_gap(self, check: ConnectionCheck) -> str:
        return {
            REASON_AUTH: "the AQ role's password is not configured yet",
            REASON_MISSING_ROLE: "the AQ role does not exist yet",
            REASON_MISSING_DATABASE: "the AQ database does not exist yet",
        }.get(check.reason, check.reason)

    # -- postgres.role ------------------------------------------------------
    def _role_step(self) -> StepSpec:
        def run(context: StepContext) -> StepResult:
            settings = self.settings(context)
            if self.aq_ready(settings):
                return StepResult.succeeded(
                    STEP_ROLE,
                    f"the role {settings.role} already connects to the AQ database",
                    detail={"role": settings.role, "created": False},
                    resources=(self._role_resource(settings, owned=False),),
                )
            if self._role_proven_by_the_server(settings):
                # The server itself named the role — it rejected a password for
                # it, or authenticated it into a database that does not exist.
                # Asking an administrator to confirm what the server just said
                # would turn a credential problem into a "no admin" problem.
                return self._role_preserved(settings)
            admin = self.admin(context)
            executor = admin.executor
            if executor is None:
                return self._no_admin(STEP_ROLE, admin, settings)
            try:
                exists = self._role_exists(executor, settings)
            except SqlError as error:
                return self._admin_failed(STEP_ROLE, admin, error)
            if exists:
                return self._role_preserved(settings)
            password = self._new_password or self._make_password()
            try:
                executor.execute(
                    f"CREATE ROLE {settings.role} WITH LOGIN PASSWORD {quote_literal(password)}"
                )
            except SqlError as error:
                return self._admin_failed(STEP_ROLE, admin, error)
            self._new_password = password
            self._check_cache.clear()
            return StepResult.succeeded(
                STEP_ROLE,
                f"created the login role {settings.role}",
                detail={"role": settings.role, "created": True, "credential_source": "generated"},
                resources=(self._role_resource(settings, owned=True),),
            )

        def verify(context: StepContext) -> bool:
            settings = self.settings(context)
            if self.aq_ready(settings, refresh=True):
                return True
            if self._role_proven_by_the_server(settings):
                return True
            executor = self.admin(context).executor
            if executor is None:
                return False
            try:
                return self._role_exists(executor, settings)
            except SqlError:
                return False

        return StepSpec(
            id=STEP_ROLE,
            title="Create the AQ database role",
            description=(
                "Creates a dedicated login role for AQ with a generated password. An "
                "existing role of that name is reused untouched — the installer never "
                "resets a credential it did not create."
            ),
            run=self._guard(STEP_ROLE, run),
            depends_on=(STEP_SERVER,),
            mutating=True,
            consent_prompt="Create a dedicated PostgreSQL login role for AQ if it is missing?",
            verify=verify,
            owner=OWNER,
        )

    def _role_proven_by_the_server(self, settings: PostgresSettings) -> bool:
        """True when the failed AQ connection already proves the role exists."""
        return self.aq_check(settings).reason in (REASON_AUTH, REASON_MISSING_DATABASE)

    def _role_preserved(self, settings: PostgresSettings) -> StepResult:
        """A role AQ did not create is recorded, never rewritten.

        On a shared server that role may be in use by something else, so the
        installer leaves its password alone; ``postgres.credentials`` reports
        the mismatch and names the rotation path.
        """
        return StepResult.succeeded(
            STEP_ROLE,
            f"the role {settings.role} already exists and was left unchanged",
            detail={"role": settings.role, "created": False, "preserved": True},
            resources=(self._role_resource(settings, owned=False),),
        )

    def _role_exists(self, executor: SqlExecutor, settings: PostgresSettings) -> bool:
        value = executor.scalar(
            f"SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(settings.role)}"
        )
        return bool(value and value.strip())

    def _role_resource(self, settings: PostgresSettings, *, owned: bool) -> ResourceRecord:
        return ResourceRecord(
            kind=RESOURCE_ROLE,
            id=settings.role,
            owned=owned,
            reused=not owned,
            detail={"server": settings.server_label},
        )

    # -- postgres.database --------------------------------------------------
    def _database_step(self) -> StepSpec:
        def run(context: StepContext) -> StepResult:
            settings = self.settings(context)
            if self.aq_ready(settings):
                return StepResult.succeeded(
                    STEP_DATABASE,
                    f"the database {settings.database} already exists and accepts AQ",
                    detail={"database": settings.database, "created": False},
                    resources=(self._database_resource(settings, owned=False),),
                )
            admin = self.admin(context)
            executor = admin.executor
            if executor is None:
                if self.aq_check(settings).reason == REASON_AUTH:
                    # The credential, not the database, is the problem. Saying
                    # "no administrator connection" here would send the operator
                    # after the wrong thing.
                    return StepResult.needs_user(
                        STEP_DATABASE,
                        f"the database cannot be checked while the server is rejecting the "
                        f"stored password for {settings.role}",
                        self._connection_advice(settings, self.aq_check(settings))[1],
                        detail={"role": settings.role},
                    )
                return self._no_admin(STEP_DATABASE, admin, settings)
            try:
                if self._database_exists(executor, settings):
                    return StepResult.succeeded(
                        STEP_DATABASE,
                        f"the database {settings.database} already exists and was left unchanged",
                        detail={
                            "database": settings.database,
                            "created": False,
                            "preserved": True,
                        },
                        resources=(self._database_resource(settings, owned=False),),
                    )
                executor.execute(
                    f"CREATE DATABASE {settings.database} OWNER {settings.role} ENCODING 'UTF8'"
                )
            except SqlError as error:
                return self._admin_failed(STEP_DATABASE, admin, error)
            self._check_cache.clear()
            return StepResult.succeeded(
                STEP_DATABASE,
                f"created the database {settings.database} owned by {settings.role}",
                detail={"database": settings.database, "created": True, "owner": settings.role},
                resources=(self._database_resource(settings, owned=True),),
            )

        def verify(context: StepContext) -> bool:
            settings = self.settings(context)
            if self.aq_ready(settings, refresh=True):
                return True
            executor = self.admin(context).executor
            if executor is None:
                return False
            try:
                return self._database_exists(executor, settings)
            except SqlError:
                return False

        return StepSpec(
            id=STEP_DATABASE,
            title="Create the AQ database",
            description=(
                "Creates an empty UTF-8 database owned by the AQ role. An existing database "
                "of that name is reused as it is; it is never dropped or re-owned."
            ),
            run=self._guard(STEP_DATABASE, run),
            depends_on=(STEP_ROLE,),
            mutating=True,
            consent_prompt="Create the AQ database if it is missing?",
            verify=verify,
            owner=OWNER,
        )

    def _database_exists(self, executor: SqlExecutor, settings: PostgresSettings) -> bool:
        value = executor.scalar(
            f"SELECT 1 FROM pg_database WHERE datname = {quote_literal(settings.database)}"
        )
        return bool(value and value.strip())

    def _database_resource(self, settings: PostgresSettings, *, owned: bool) -> ResourceRecord:
        return ResourceRecord(
            kind=RESOURCE_DATABASE,
            id=settings.database,
            owned=owned,
            reused=not owned,
            detail={"server": settings.server_label, "owner": settings.role},
        )

    # -- postgres.rotate ----------------------------------------------------
    def _rotate_step(self) -> StepSpec:
        def run(context: StepContext) -> StepResult:
            settings = self.settings(context)
            admin = self.admin(context)
            executor = admin.executor
            if executor is None:
                return self._no_admin(STEP_ROTATE, admin, settings)
            password = self._make_password()
            try:
                executor.execute(
                    f"ALTER ROLE {settings.role} WITH LOGIN PASSWORD {quote_literal(password)}"
                )
            except SqlError as error:
                return self._admin_failed(STEP_ROTATE, admin, error)
            # The new password only reaches disk in postgres.credentials, which
            # runs next; until it does, the database is unreachable, so a
            # failure there is reported as the emergency it is.
            self._new_password = password
            self._check_cache.clear()
            return StepResult.succeeded(
                STEP_ROTATE,
                f"replaced the password for {settings.role}; the next step stores it",
                detail={"role": settings.role, "rotated": True},
            )

        return StepSpec(
            id=STEP_ROTATE,
            title="Replace the AQ database password",
            description=(
                "Generates a new password for the AQ role and sets it on the server. Opt-in "
                f"with --with {CAPABILITY_ROTATE}: this is the recovery path for a lost or "
                "changed credential, and it runs every time it is selected."
            ),
            run=self._guard(STEP_ROTATE, run),
            depends_on=(STEP_DATABASE,),
            capability=CAPABILITY_ROTATE,
            mutating=True,
            consent_prompt="Replace the AQ database password with a newly generated one?",
            # Selecting the capability *is* the request to rotate, so this step
            # never revalidates its way out of running.
            verify=lambda context: False,
            owner=OWNER,
        )

    # -- postgres.credentials ----------------------------------------------
    def _credentials_step(self) -> StepSpec:
        def run(context: StepContext) -> StepResult:
            settings = self.settings(context)
            password = self.stored_password(settings)
            if password is None:
                return StepResult.failed(
                    STEP_CREDENTIALS,
                    f"no password is available for the role {settings.role}",
                    (
                        "The role already existed, so AQ did not set its password. Put the "
                        f"existing password in {self.env_path()} as "
                        f"{settings.password_env}=…, or let AQ replace it with a generated "
                        f"one by rerunning `aq install --with {CAPABILITY_ROTATE}`."
                    ),
                    detail={"role": settings.role, "credential_store": str(self.env_path())},
                )
            check = self.aq_check(settings, refresh=True)
            if check.reason == REASON_AUTH:
                return StepResult.failed(
                    STEP_CREDENTIALS,
                    f"the stored password for {settings.role} was rejected by the server",
                    (
                        f"Correct {settings.password_env} in {self.env_path()}, or have AQ "
                        "generate and install a new password with `aq install --with "
                        f"{CAPABILITY_ROTATE}`."
                    ),
                    detail={"role": settings.role, "credential_store": str(self.env_path())},
                )
            return self._store_credential(settings, password)

        def verify(context: StepContext) -> bool:
            settings = self.settings(context)
            if self.stored_password(settings) is None:
                return False
            return self._configured_url() == self._expected_url(settings)

        return StepSpec(
            id=STEP_CREDENTIALS,
            title="Store the database credential",
            description=(
                "Writes the password to the daemon's protected .env file (mode 0600) and "
                "points config.yaml at it as ${AQ_DB_PASSWORD}. The password itself never "
                "enters config.yaml, the install result or the resume record."
            ),
            run=self._guard(STEP_CREDENTIALS, run),
            depends_on=(STEP_ROTATE, "prereq.data-dir"),
            mutating=True,
            consent_prompt="Write the database credential and update config.yaml?",
            verify=verify,
            owner=OWNER,
        )

    def _expected_url(self, settings: PostgresSettings) -> str:
        return settings.sqlalchemy_url(password_reference="${" + settings.password_env + "}")

    def _store_credential(self, settings: PostgresSettings, password: str) -> StepResult:
        env_path = self.env_path()
        config_path = self.config_path()
        try:
            write_env_value(env_path, settings.password_env, password)
            update = write_database_config(config_path, url=self._expected_url(settings))
        except OSError as error:
            return StepResult.failed(
                STEP_CREDENTIALS,
                f"could not write the database credential: {error}",
                (f"Make sure {env_path.parent} is writable by this user, then rerun `aq install`."),
                detail={"credential_store": str(env_path)},
            )
        self._new_password = None
        detail: dict[str, Any] = {
            "credential_store": str(env_path),
            "credential_source": f"env:{settings.password_env}",
            "config_path": str(config_path),
            "config_created": update.created,
            "url": settings.redacted_url(),
        }
        note = ""
        if update.backup is not None:
            detail["config_backup"] = str(update.backup)
            note = f"; previous config saved as {update.backup.name}"
        return StepResult.succeeded(
            STEP_CREDENTIALS,
            f"stored the credential in {env_path} and pointed {config_path.name} at "
            f"{settings.redacted_url()}{note}",
            detail=detail,
            resources=(
                ResourceRecord(
                    kind=RESOURCE_CREDENTIAL,
                    id=settings.password_env,
                    owned=True,
                    reused=False,
                    detail={
                        "credential_store": str(env_path),
                        "credential_source": f"env:{settings.password_env}",
                    },
                ),
            ),
        )

    def _configured_url(self) -> str | None:
        path = self.config_path()
        if not path.exists():
            return None
        try:
            from src.config_editor import read_raw_config

            raw = read_raw_config(str(path))
        except Exception:  # noqa: BLE001 - an unreadable config is simply "not configured"
            return None
        database = raw.get("database")
        if not isinstance(database, Mapping):
            return None
        url = database.get("url")
        return str(url) if url else None

    # -- postgres.connection ------------------------------------------------
    def _connection_step(self) -> StepSpec:
        def run(context: StepContext) -> StepResult:
            settings = self.settings(context)
            check = self.aq_check(settings, refresh=True)
            if check.ok:
                return StepResult.succeeded(
                    STEP_CONNECTION,
                    f"connected to {settings.redacted_url()} as {settings.role} "
                    f"({self._version_label(check)}); the schema is the daemon's to create",
                    detail={
                        "url": settings.redacted_url(),
                        "schema": "delegated to the daemon or operator (`aq db upgrade`)",
                        **check.to_detail(),
                    },
                )
            if context.dry_run:
                return StepResult.skipped(
                    STEP_CONNECTION,
                    "dry run — the steps that provision the database were not executed",
                    detail=check.to_detail(),
                )
            summary, remediation = self._connection_advice(settings, check)
            return StepResult.failed(
                STEP_CONNECTION, summary, remediation, detail=check.to_detail()
            )

        return StepSpec(
            id=STEP_CONNECTION,
            title="Check the AQ database connection",
            description=(
                "Connects as the AQ role with the stored credential and reports the server "
                "version. Creating the schema is the daemon's or operator's step "
                "(`aq db upgrade`), never the installer's."
            ),
            run=self._guard(STEP_CONNECTION, run),
            depends_on=(STEP_CREDENTIALS,),
            verify=lambda context: self.aq_ready(self.settings(context), refresh=True),
            owner=OWNER,
        )

    def _connection_advice(
        self, settings: PostgresSettings, check: ConnectionCheck
    ) -> tuple[str, str]:
        if check.reason == REASON_AUTH:
            return (
                f"the server rejected the stored password for {settings.role}",
                (
                    f"Put the role's real password in {self.env_path()} as "
                    f"{settings.password_env}=…, or generate and install a new one with "
                    f"`aq install --with {CAPABILITY_ROTATE}`."
                ),
            )
        if check.reason == REASON_MISSING_DATABASE:
            return (
                f"the database {settings.database} does not exist on {settings.server_label}",
                (
                    f"Rerun `aq install --restart-from {STEP_DATABASE}` to create it, or "
                    "point AQ at the right database with the `postgres.database` install "
                    "setting."
                ),
            )
        if check.reason == REASON_MISSING_ROLE:
            return (
                f"the role {settings.role} does not exist on {settings.server_label}",
                f"Rerun `aq install --restart-from {STEP_ROLE}` to create it.",
            )
        return (
            f"could not connect to {settings.redacted_url()}: {check.message}",
            (
                f"Confirm PostgreSQL is running on {settings.server_label} and reachable from "
                "this host, then rerun `aq install`."
            ),
        )

    # -- postgres.boot ------------------------------------------------------
    def _boot_step(self) -> StepSpec:
        def run(context: StepContext) -> StepResult:
            plan = self._service(context, self.settings(context))
            if plan.manager == "none":
                return StepResult.skipped(
                    STEP_BOOT,
                    "no service manager owns this server, so its restart behaviour is not "
                    "AQ's to configure",
                    detail={"service_manager": plan.manager},
                )
            if plan.boot_needs_user:
                return StepResult.needs_user(
                    STEP_BOOT,
                    f"{plan.label} cannot be enabled at boot on this host",
                    plan.boot_remediation,
                    detail={"service_manager": plan.manager, "boot_enabled": False},
                )
            if service_enabled_at_boot(plan, self._runner):
                return self._boot_enabled_result(plan, changed=False)
            if not plan.enable_argv:
                return StepResult.skipped(
                    STEP_BOOT,
                    f"{plan.label} has no supported enable-at-boot command",
                    detail={"service_manager": plan.manager},
                )
            if plan.enable_argv[0] == "sudo" and not self._sudo_ready():
                return StepResult.needs_user(
                    STEP_BOOT,
                    f"enabling {plan.label} at boot needs root and sudo would prompt",
                    (
                        "Run `sudo -v` in this terminal and rerun `aq install`, or run "
                        f"`{' '.join(plan.enable_argv)}` yourself."
                    ),
                    detail={"service_manager": plan.manager},
                )
            result = self._runner(list(plan.enable_argv), timeout=120.0)
            if not result.ok:
                return StepResult.failed(
                    STEP_BOOT,
                    f"enabling {plan.label} at boot failed: {result.diagnostic()}",
                    (
                        f"Run `{' '.join(plan.enable_argv)}` by hand to see the error, then "
                        "rerun `aq install`."
                    ),
                    detail={"service_manager": plan.manager},
                )
            return self._boot_enabled_result(plan, changed=True)

        def verify(context: StepContext) -> bool:
            plan = self._service(context, self.settings(context))
            if plan.manager == "none" or plan.boot_needs_user:
                return False
            return service_enabled_at_boot(plan, self._runner) is True

        return StepSpec(
            id=STEP_BOOT,
            title="Start PostgreSQL again after a restart",
            description=(
                "Enables the managed server's service so the database comes back after the "
                "machine (or, on WSL2, the distribution) restarts, and says plainly when "
                "that needs a human."
            ),
            run=self._guard(STEP_BOOT, run),
            depends_on=(STEP_CONNECTION,),
            capability=CAPABILITY_MANAGED,
            mutating=True,
            consent_prompt="Enable the PostgreSQL service so it starts after a restart?",
            verify=verify,
            owner=OWNER,
        )

    def _boot_enabled_result(self, plan: ServicePlan, *, changed: bool) -> StepResult:
        scope = "at login" if plan.manager == "brew" else "at boot"
        verb = "enabled" if changed else "was already enabled"
        return StepResult.succeeded(
            STEP_BOOT,
            f"{plan.label} {verb} to start {scope}",
            detail={"service_manager": plan.manager, "boot_enabled": True, "changed": changed},
        )

    # -- shared failure shapes ---------------------------------------------
    def _no_admin(self, step_id: str, admin: AdminAccess, settings: PostgresSettings) -> StepResult:
        return StepResult.needs_user(
            step_id,
            f"no PostgreSQL administrator connection is available: {admin.reason}",
            admin.remediation
            or (
                "Give AQ an administrator connection with the `postgres.admin_url` install "
                f"setting, or create the role {settings.role} and the database "
                f"{settings.database} yourself and rerun `aq install`."
            ),
            detail={"admin_route": admin.label},
        )

    def _admin_failed(self, step_id: str, admin: AdminAccess, error: SqlError) -> StepResult:
        return StepResult.failed(
            step_id,
            f"the administrator connection ({admin.label}) failed: {error}",
            (
                "Fix what the server reports above and rerun `aq install`. If this "
                "installation should use a different administrator, set the "
                "`postgres.admin_url` install setting."
            ),
            detail={"admin_route": admin.label},
        )


def postgres_steps(**kwargs: Any) -> tuple[StepSpec, ...]:
    """The PostgreSQL steps, built on a fresh adapter."""
    return PostgresAdapter(**kwargs).steps()


__all__ = [
    "CAPABILITY_MANAGED",
    "CAPABILITY_ROTATE",
    "STEP_BOOT",
    "STEP_CONNECTION",
    "STEP_CREDENTIALS",
    "STEP_DATABASE",
    "STEP_PACKAGE",
    "STEP_ROLE",
    "STEP_ROTATE",
    "STEP_SERVER",
    "STEP_SERVICE",
    "PostgresAdapter",
    "postgres_steps",
]
