"""PostgreSQL detection, provisioning primitives and credential storage.

This is the mechanism half of the installer's database adapter
(``noble-apex.6``); ``src/install/postgres_steps.py`` is the policy half that
declares the :class:`~src.install.steps.StepSpec` values ``aq install`` runs.
The split exists because almost everything here is a *decision about this
host* — which package manager installs a server, how an administrator
connection is obtained, what a failed connection actually means — and those
decisions have to be testable without a PostgreSQL server, a package manager
or a terminal.

Three rules shape the module:

**Reuse beats installation.**  A reachable, compatible server is used as it
is.  Nothing here upgrades, reconfigures or restarts a server AQ did not
install, and no existing role or database is ever modified: the installer
creates what is missing and records what it found.

**The installer never owns the schema.**  It provisions a role and an empty
database and then stops.  ``alembic`` is the daemon/operator's authority
(``docs/guides/migrations.md``), so no code path in this adapter or its steps
runs a migration — a worker or an installer that stamped an operator database
is the exact failure the migration guard exists to prevent.

**Credentials live in one protected place.**  A generated password is written
to ``<data dir>/.env`` at mode ``0600`` — the file :func:`src.config._load_env_file`
already treats as the daemon's credential source — and ``config.yaml`` refers
to it as ``${AQ_DB_PASSWORD}``.  The password is never placed in a step
summary, a resource record, an argv, or the resume record.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit

#: The oldest server AQ's schema and queries are supported on.  Ubuntu 24.04
#: ships 16 and Homebrew's current formula is 17, so the floor only ever
#: rejects a genuinely ancient instance someone asked us to reuse.
MINIMUM_SERVER_VERSION = 14

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432
DEFAULT_ROLE = "agent_queue"
DEFAULT_DATABASE = "agent_queue"

#: The environment variable ``config.yaml`` refers to as ``${AQ_DB_PASSWORD}``.
DEFAULT_PASSWORD_ENV = "AQ_DB_PASSWORD"

#: Homebrew versions its PostgreSQL formulae (``postgresql@17``); Debian and
#: Ubuntu ship an unversioned meta-package that pulls the distribution's
#: default major.  Both are overridable with the ``postgres.version`` setting.
DEFAULT_BREW_FORMULA_VERSION = "17"

#: Unquoted SQL identifiers, which is all the installer ever needs: a role and
#: a database name it either created or was told to reuse.  Anything outside
#: this shape is rejected at settings-parse time rather than escaped, because
#: an installer has no reason to accept an exotic identifier and every reason
#: not to interpolate one into DDL.
IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

SERVICE_MANAGERS = ("auto", "systemd", "sysv", "brew", "none")

#: Resource kinds recorded in the resume record.  ``(kind, id)`` is the
#: identity a rerun deduplicates on, and ``owned`` is what a later uninstall is
#: allowed to remove.
RESOURCE_SERVER = "postgres-server"
RESOURCE_ROLE = "postgres-role"
RESOURCE_DATABASE = "postgres-database"
RESOURCE_CREDENTIAL = "postgres-credential"


class PostgresSettingsError(ValueError):
    """A ``postgres.*`` install setting is unusable."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """The ``postgres:`` block of the install input file, validated.

    Every field has a working default, so ``aq install`` on a fresh supported
    host needs no input file at all; the settings exist for the reuse case,
    where the operator already has a server, a port, or a naming convention
    the installer must respect rather than guess.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    database: str = DEFAULT_DATABASE
    role: str = DEFAULT_ROLE
    password_env: str = DEFAULT_PASSWORD_ENV
    #: Administrator connection for an existing server, e.g.
    #: ``postgresql://postgres@db.internal:5432/postgres``.  Used instead of
    #: the platform's local superuser route when it is set.
    admin_url: str | None = None
    #: Overrides the platform default for the local administrator identity
    #: (``postgres`` on Debian/Ubuntu, the invoking user on Homebrew).
    admin_user: str | None = None
    #: ``auto`` picks systemd, the SysV ``service`` shim or ``brew services``
    #: from the observed host.  ``none`` disables service management entirely,
    #: which is what a container or an externally managed server wants.
    service_manager: str = "auto"
    #: Major version for a managed installation (``16``, ``17``…).  Unset means
    #: the platform's default package.
    version: str | None = None
    connect_timeout: float = 5.0
    #: How long ``postgres.service`` waits for a freshly started server to
    #: accept connections, in seconds.
    startup_timeout: float = 60.0

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> PostgresSettings:
        """Parse and validate ``settings.postgres`` from the install input.

        An unknown key is an error rather than a no-op: an unattended install
        that quietly ignored ``postgres.databse`` would provision the wrong
        database and only say so much later.
        """
        raw = options.get("postgres") if isinstance(options, Mapping) else None
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise PostgresSettingsError("the 'postgres' install setting must be a mapping")

        known = {field_name for field_name in cls.__dataclass_fields__}
        unknown = sorted({str(key) for key in raw} - known)
        if unknown:
            raise PostgresSettingsError(
                f"unknown postgres setting(s): {', '.join(unknown)} "
                f"(recognised: {', '.join(sorted(known))})"
            )

        def _identifier(name: str, default: str) -> str:
            value = str(raw.get(name, default) or default).strip()
            if not IDENTIFIER_RE.match(value):
                raise PostgresSettingsError(
                    f"postgres.{name} must be a lower-case SQL identifier "
                    f"([a-z_][a-z0-9_]*, at most 63 characters); got {value!r}"
                )
            return value

        try:
            port = int(raw.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            raise PostgresSettingsError(
                f"postgres.port must be a TCP port number; got {raw.get('port')!r}"
            ) from None
        if not 1 <= port <= 65535:
            raise PostgresSettingsError(f"postgres.port must be 1-65535; got {port}")

        service_manager = str(raw.get("service_manager", "auto") or "auto").strip().lower()
        if service_manager not in SERVICE_MANAGERS:
            raise PostgresSettingsError(
                f"postgres.service_manager must be one of {', '.join(SERVICE_MANAGERS)}; "
                f"got {service_manager!r}"
            )

        password_env = str(raw.get("password_env", DEFAULT_PASSWORD_ENV) or "").strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", password_env):
            raise PostgresSettingsError(
                f"postgres.password_env must be an environment variable name; got {password_env!r}"
            )

        def _timeout(name: str, default: float) -> float:
            try:
                value = float(raw.get(name, default))
            except (TypeError, ValueError):
                raise PostgresSettingsError(
                    f"postgres.{name} must be a number of seconds; got {raw.get(name)!r}"
                ) from None
            if value <= 0:
                raise PostgresSettingsError(f"postgres.{name} must be greater than zero")
            return value

        admin_url = raw.get("admin_url")
        version = raw.get("version")
        admin_user = raw.get("admin_user")
        return cls(
            host=str(raw.get("host", DEFAULT_HOST) or DEFAULT_HOST).strip(),
            port=port,
            database=_identifier("database", DEFAULT_DATABASE),
            role=_identifier("role", DEFAULT_ROLE),
            password_env=password_env,
            admin_url=str(admin_url).strip() if admin_url else None,
            admin_user=str(admin_user).strip() if admin_user else None,
            service_manager=service_manager,
            version=str(version).strip() if version not in (None, "") else None,
            connect_timeout=_timeout("connect_timeout", 5.0),
            startup_timeout=_timeout("startup_timeout", 60.0),
        )

    @property
    def server_label(self) -> str:
        return f"{self.host}:{self.port}"

    def libpq_url(self, password: str | None, *, database: str | None = None) -> str:
        """A ``postgresql://`` DSN for asyncpg and ``psql``."""
        return build_url(
            "postgresql",
            user=self.role,
            password=password,
            host=self.host,
            port=self.port,
            database=database or self.database,
        )

    def sqlalchemy_url(self, *, password_reference: str) -> str:
        """The DSN written into ``config.yaml``.

        The password is an unresolved ``${ENV_VAR}`` reference, never material:
        ``src.config`` substitutes it at load time from ``.env``, so the file
        that is easy to copy, paste and commit never carries the secret.
        """
        return (
            f"postgresql+asyncpg://{quote(self.role)}:{password_reference}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    def redacted_url(self) -> str:
        """The connection string with no credential in it, safe to report."""
        return build_url(
            "postgresql",
            user=self.role,
            password=None,
            host=self.host,
            port=self.port,
            database=self.database,
        )


def build_url(
    scheme: str,
    *,
    user: str | None,
    password: str | None,
    host: str,
    port: int | None,
    database: str,
) -> str:
    authority = ""
    if user:
        authority = quote(user, safe="")
        if password:
            authority += ":" + quote(password, safe="")
        authority += "@"
    authority += host
    if port:
        authority += f":{port}"
    return urlunsplit((scheme, authority, f"/{database}", "", ""))


def split_password(url: str) -> tuple[str, str | None]:
    """Return *url* with its password removed, plus the password.

    A DSN handed to a child process on the command line is visible to every
    user on the box through ``ps``.  Splitting the password out lets the caller
    pass it in the child's environment instead, which is the same thing libpq's
    own ``PGPASSWORD`` does.
    """
    parts = urlsplit(url)
    if not parts.password:
        return url, None
    userinfo = quote(unquote(parts.username or ""), safe="")
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    authority = f"{userinfo}@{host}" if userinfo else host
    if parts.port:
        authority += f":{parts.port}"
    stripped = urlunsplit((parts.scheme, authority, parts.path, parts.query, parts.fragment))
    return stripped, unquote(parts.password)


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout or self.stderr).strip()

    def diagnostic(self, limit: int = 400) -> str:
        """A short, single-line summary of a failure, for a step summary."""
        text = (self.stderr or self.stdout or "").strip()
        text = " ".join(text.split())
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return text or f"exit status {self.returncode}"


class CommandRunner(Protocol):
    """How the adapter reaches the host.  Injected everywhere, so tests never shell out."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult: ...


def subprocess_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    timeout: float | None = None,
) -> CommandResult:
    """The real runner: capture output, never raise, never inherit stdin."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            env=merged,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        return CommandResult(tuple(argv), 127, "", str(error))
    except subprocess.TimeoutExpired:
        return CommandResult(tuple(argv), 124, "", f"timed out after {timeout}s")
    except OSError as error:  # pragma: no cover - permission/exec failures
        return CommandResult(tuple(argv), 126, "", str(error))
    return CommandResult(
        tuple(argv), completed.returncode, completed.stdout or "", completed.stderr or ""
    )


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """True when something is listening on *host:port*."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------

#: Why a connection attempt ended.  These are the branches every step's
#: reporting is written against, so they are named for the *operator's*
#: question ("is the database missing, or is my password wrong?") rather than
#: for the driver exception that produced them.
REASON_OK = "ok"
REASON_AUTH = "auth"
REASON_MISSING_DATABASE = "missing-database"
REASON_MISSING_ROLE = "missing-role"
REASON_UNREACHABLE = "unreachable"
REASON_NOT_POSTGRES = "not-postgres"
REASON_ERROR = "error"

#: Reasons that still prove a PostgreSQL server answered.
POSTGRES_ANSWERED = frozenset(
    {REASON_OK, REASON_AUTH, REASON_MISSING_DATABASE, REASON_MISSING_ROLE}
)


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    """The result of one connection attempt, with no credential in it."""

    reason: str
    message: str = ""
    server_version: str = ""
    server_version_num: int | None = None

    @property
    def ok(self) -> bool:
        return self.reason == REASON_OK

    @property
    def answered(self) -> bool:
        return self.reason in POSTGRES_ANSWERED

    @property
    def supported_version(self) -> bool:
        if self.server_version_num is None:
            return True
        return self.server_version_num // 10000 >= MINIMUM_SERVER_VERSION

    def to_detail(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "message": self.message,
            "server_version": self.server_version,
        }


#: A connector is ``(url, timeout) -> ConnectionCheck``.  The default one uses
#: asyncpg — a core AQ dependency, so the check works before ``psql`` exists.
Connector = Callable[[str, float], ConnectionCheck]


def asyncpg_check(url: str, timeout: float = 5.0) -> ConnectionCheck:
    """Connect once with asyncpg and classify what happened.

    Classification is the point.  "Cannot connect" is useless to an operator;
    "the role exists but the password does not match" and "the server is there
    but ``agent_queue`` is not a database on it" lead to different, documented
    recoveries, and each has its own remediation in the steps above.
    """
    import asyncpg
    from asyncpg import exceptions as pg_errors

    async def _connect() -> ConnectionCheck:
        connection = await asyncpg.connect(dsn=url, timeout=timeout)
        try:
            version = await connection.fetchval("SELECT version()")
            version_num = await connection.fetchval("SELECT current_setting('server_version_num')")
        finally:
            await connection.close()
        return ConnectionCheck(
            reason=REASON_OK,
            message="connected",
            server_version=str(version or "").split(" on ", 1)[0],
            server_version_num=int(version_num) if str(version_num or "").isdigit() else None,
        )

    try:
        return asyncio.run(asyncio.wait_for(_connect(), timeout=timeout * 3))
    except pg_errors.InvalidPasswordError as error:
        return ConnectionCheck(REASON_AUTH, str(error))
    except pg_errors.InvalidCatalogNameError as error:
        return ConnectionCheck(REASON_MISSING_DATABASE, str(error))
    except pg_errors.InvalidAuthorizationSpecificationError as error:
        text = str(error)
        reason = REASON_MISSING_ROLE if "does not exist" in text else REASON_AUTH
        return ConnectionCheck(reason, text)
    except (
        pg_errors.ProtocolViolationError,
        pg_errors.ConnectionDoesNotExistError,
        pg_errors.CannotConnectNowError,
    ) as error:
        return ConnectionCheck(REASON_NOT_POSTGRES, str(error))
    except (TimeoutError, OSError, ConnectionError) as error:
        return ConnectionCheck(REASON_UNREACHABLE, str(error) or type(error).__name__)
    except Exception as error:  # noqa: BLE001 - any other driver error is still a diagnosis
        return ConnectionCheck(REASON_ERROR, f"{type(error).__name__}: {error}")


# ---------------------------------------------------------------------------
# Executing SQL
# ---------------------------------------------------------------------------


class SqlError(RuntimeError):
    """A statement could not be run.  Carries no credential."""


class SqlExecutor(Protocol):
    """The narrow port the provisioning steps speak.

    Two implementations exist because the two ways to reach a server as an
    administrator are genuinely different: a local Debian/Ubuntu or Homebrew
    server is reached as an operating-system user over a unix socket
    (``sudo -u postgres psql``), and a remote or explicitly configured one is
    reached over TCP with a DSN.
    """

    label: str

    def scalar(self, sql: str) -> str | None: ...

    def execute(self, sql: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PsqlExecutor:
    """Runs SQL through ``psql``, reading statements from stdin.

    Statements go in on stdin rather than in ``-c`` so that nothing a
    statement contains — a freshly generated password, above all — is visible
    in the process table.
    """

    runner: CommandRunner
    prefix: tuple[str, ...]
    label: str
    env: Mapping[str, str] = field(default_factory=dict)
    database: str = "postgres"
    timeout: float = 60.0

    def _argv(self) -> list[str]:
        return [
            *self.prefix,
            "--no-psqlrc",
            "--quiet",
            "--no-align",
            "--tuples-only",
            "--set",
            "ON_ERROR_STOP=1",
            "--dbname",
            self.database,
        ]

    def _run(self, sql: str) -> CommandResult:
        return self.runner(self._argv(), env=dict(self.env), stdin=sql, timeout=self.timeout)

    def scalar(self, sql: str) -> str | None:
        result = self._run(sql)
        if not result.ok:
            raise SqlError(result.diagnostic())
        text = result.stdout.strip()
        return text.splitlines()[0].strip() if text else None

    def execute(self, sql: str) -> None:
        result = self._run(sql)
        if not result.ok:
            raise SqlError(result.diagnostic())


@dataclass(frozen=True, slots=True)
class AsyncpgExecutor:
    """Runs SQL over a TCP administrator DSN."""

    url: str
    label: str
    timeout: float = 30.0

    def _run(self, sql: str, *, fetch: bool) -> Any:
        import asyncpg

        async def _go() -> Any:
            connection = await asyncpg.connect(dsn=self.url, timeout=self.timeout)
            try:
                if fetch:
                    return await connection.fetchval(sql)
                await connection.execute(sql)
                return None
            finally:
                await connection.close()

        try:
            return asyncio.run(_go())
        except Exception as error:  # noqa: BLE001 - reported, never raised through
            raise SqlError(f"{type(error).__name__}: {error}") from None

    def scalar(self, sql: str) -> str | None:
        value = self._run(sql, fetch=True)
        return None if value is None else str(value)

    def execute(self, sql: str) -> None:
        self._run(sql, fetch=False)


def quote_literal(value: str) -> str:
    """Quote *value* as a SQL string literal.

    Only ever used for a generated password, whose alphabet is
    ``[A-Za-z0-9_-]``; the doubling is belt-and-braces so that a future caller
    with a different alphabet is still safe.
    """
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Administrator access
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminAccess:
    """How (or whether) this run can act as a PostgreSQL administrator."""

    executor: SqlExecutor | None
    label: str
    reason: str = ""
    remediation: str = ""

    @property
    def available(self) -> bool:
        return self.executor is not None


def _sudo_available(runner: CommandRunner, which: Callable[[str], str | None]) -> bool:
    """True when this user can run a command as another without a password.

    ``sudo -n`` fails rather than prompting, which is what an installer that
    must never hang on a hidden password prompt needs.
    """
    if which("sudo") is None:
        return False
    return runner(["sudo", "-n", "true"], timeout=10.0).ok


def resolve_admin(
    settings: PostgresSettings,
    *,
    host_path: str,
    runner: CommandRunner,
    which: Callable[[str], str | None],
    environ: Mapping[str, str],
) -> AdminAccess:
    """Pick the administrator route for this host, or explain why there is none.

    Order of preference:

    1. ``postgres.admin_url`` — an explicit connection the operator gave us.
       Its password (if any) is moved into the child's environment.
    2. The local superuser route for the platform: the ``postgres`` operating
       system user on Debian/Ubuntu (peer authentication over the unix socket),
       the invoking user on Homebrew, whose ``initdb`` makes them a superuser.

    Returning an unavailable :class:`AdminAccess` with a remediation is a
    first-class outcome: an operator reusing a managed cloud database has no
    superuser at all, and the steps handle that by checking whether the AQ
    role and database already work instead of failing.
    """
    if settings.admin_url:
        return AdminAccess(
            executor=AsyncpgExecutor(url=settings.admin_url, label="postgres.admin_url"),
            label=f"postgres.admin_url ({_redact_url(settings.admin_url)})",
        )

    psql = which("psql")
    if psql is None:
        return AdminAccess(
            executor=None,
            label="none",
            reason="psql is not on PATH, so no local administrator connection is possible",
            remediation=(
                "Install the PostgreSQL client (Ubuntu: `sudo apt-get install -y "
                "postgresql-client`; macOS: `brew install libpq`), or set the "
                "`postgres.admin_url` install setting to an administrator connection."
            ),
        )

    current_user = (environ.get("USER") or environ.get("LOGNAME") or "").strip()
    admin_user = settings.admin_user or ("postgres" if host_path.startswith("windows") else "")

    if host_path.startswith("macos"):
        # Homebrew's formula runs `initdb` as the installing user, so that user
        # is the cluster superuser; there is no `postgres` OS account to sudo to.
        user = settings.admin_user or current_user or "postgres"
        prefix = (psql, "--host", settings.host, "--port", str(settings.port), "--username", user)
        return AdminAccess(
            executor=PsqlExecutor(runner=runner, prefix=prefix, label=f"psql as {user}"),
            label=f"psql as {user}",
        )

    admin_user = admin_user or "postgres"
    if current_user and current_user == admin_user:
        prefix = (psql,)
        return AdminAccess(
            executor=PsqlExecutor(runner=runner, prefix=prefix, label=f"psql as {admin_user}"),
            label=f"psql as {admin_user}",
        )
    if not _sudo_available(runner, which):
        return AdminAccess(
            executor=None,
            label="none",
            reason=f"cannot run psql as the {admin_user} system user without a sudo password",
            remediation=(
                "Run `sudo -v` in this terminal and rerun `aq install`, run the installer as "
                f"{admin_user}, or set the `postgres.admin_url` install setting to an "
                "administrator connection on the server you want AQ to use."
            ),
        )
    prefix = ("sudo", "-n", "-u", admin_user, psql)
    return AdminAccess(
        executor=PsqlExecutor(runner=runner, prefix=prefix, label=f"sudo -u {admin_user} psql"),
        label=f"sudo -u {admin_user} psql",
    )


def _redact_url(url: str) -> str:
    stripped, _ = split_password(url)
    return stripped


# ---------------------------------------------------------------------------
# Packages and services
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackagePlan:
    """How a managed server is installed on this host."""

    manager: str
    package: str
    install_argv: tuple[str, ...]
    present_argv: tuple[str, ...]
    label: str
    requires_sudo: bool = False


def package_plan(
    settings: PostgresSettings,
    *,
    host_path: str,
    which: Callable[[str], str | None],
) -> PackagePlan | None:
    """The install plan for *host_path*, or None when there is no supported one.

    The commands are the ones the upstream projects document: ``apt-get
    install postgresql`` for Debian/Ubuntu (the distribution package; Ubuntu
    24.04's is PostgreSQL 16) and ``brew install postgresql@NN`` for Homebrew,
    whose formulae are always version-qualified.
    """
    if host_path.startswith("macos"):
        brew = which("brew")
        if brew is None:
            return None
        formula = f"postgresql@{settings.version or DEFAULT_BREW_FORMULA_VERSION}"
        return PackagePlan(
            manager="brew",
            package=formula,
            install_argv=(brew, "install", formula),
            present_argv=(brew, "list", "--versions", formula),
            label=f"Homebrew formula {formula}",
        )
    if which("apt-get") is not None:
        package = f"postgresql-{settings.version}" if settings.version else "postgresql"
        return PackagePlan(
            manager="apt",
            package=package,
            install_argv=(
                "sudo",
                "-n",
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "apt-get",
                "install",
                "-y",
                package,
            ),
            present_argv=("dpkg-query", "--show", "--showformat=${Status}", package),
            label=f"apt package {package}",
            requires_sudo=True,
        )
    return None


def package_present(plan: PackagePlan, runner: CommandRunner) -> bool:
    result = runner(list(plan.present_argv), timeout=60.0)
    if not result.ok:
        return False
    if plan.manager == "apt":
        return "install ok installed" in result.stdout.lower()
    return bool(result.stdout.strip())


@dataclass(frozen=True, slots=True)
class ServicePlan:
    """How a managed server is started and enabled at boot on this host.

    ``boot_needs_user`` is the WSL2 case and is deliberately not a failure: a
    distribution without systemd cannot be made to start a service at boot by
    an installer at all — enabling systemd in ``/etc/wsl.conf`` and restarting
    WSL is a human action, and the contract's word for that is ``needs_user``.
    """

    manager: str
    unit: str
    start_argv: tuple[str, ...] = ()
    enable_argv: tuple[str, ...] = ()
    is_enabled_argv: tuple[str, ...] = ()
    label: str = ""
    boot_needs_user: bool = False
    boot_remediation: str = ""


def _systemd_running(path_exists: Callable[[str], bool]) -> bool:
    return path_exists("/run/systemd/system")


def service_plan(
    settings: PostgresSettings,
    *,
    host_path: str,
    which: Callable[[str], str | None],
    path_exists: Callable[[str], bool] = os.path.isdir,
    package: PackagePlan | None = None,
) -> ServicePlan:
    """Choose the service manager for this host."""
    chosen = settings.service_manager
    if chosen == "none":
        return ServicePlan(manager="none", unit="", label="unmanaged")

    if chosen in ("brew", "auto") and host_path.startswith("macos"):
        brew = which("brew")
        if brew is not None:
            formula = (
                package.package
                if package
                else (f"postgresql@{settings.version or DEFAULT_BREW_FORMULA_VERSION}")
            )
            return ServicePlan(
                manager="brew",
                unit=formula,
                start_argv=(brew, "services", "start", formula),
                enable_argv=(brew, "services", "start", formula),
                is_enabled_argv=(brew, "services", "list"),
                label=f"brew services {formula}",
            )
        if chosen == "brew":
            return ServicePlan(manager="none", unit="", label="brew is not on PATH")

    if (
        chosen in ("systemd", "auto")
        and which("systemctl") is not None
        and (chosen == "systemd" or _systemd_running(path_exists))
    ):
        return ServicePlan(
            manager="systemd",
            unit="postgresql",
            start_argv=("sudo", "-n", "systemctl", "start", "postgresql"),
            enable_argv=("sudo", "-n", "systemctl", "enable", "postgresql"),
            is_enabled_argv=("systemctl", "is-enabled", "postgresql"),
            label="systemd unit postgresql",
        )

    if chosen in ("sysv", "auto") and which("service") is not None:
        # A WSL2 distribution with systemd turned off: `service` still starts
        # the cluster, but nothing will start it again after `wsl --shutdown`.
        return ServicePlan(
            manager="sysv",
            unit="postgresql",
            start_argv=("sudo", "-n", "service", "postgresql", "start"),
            label="service postgresql",
            boot_needs_user=True,
            boot_remediation=(
                "This WSL distribution is not running systemd, so nothing will start "
                "PostgreSQL after `wsl --shutdown`. Add\n"
                "    [boot]\n    systemd=true\n"
                "to /etc/wsl.conf, run `wsl --shutdown` from Windows, reopen the "
                "distribution and rerun `aq install`; or start the server by hand with "
                "`sudo service postgresql start` at the beginning of each session."
            ),
        )

    return ServicePlan(manager="none", unit="", label="no supported service manager")


def service_enabled_at_boot(plan: ServicePlan, runner: CommandRunner) -> bool | None:
    """True/False when the answer is knowable, None when it is not."""
    if not plan.is_enabled_argv:
        return None
    result = runner(list(plan.is_enabled_argv), timeout=30.0)
    if plan.manager == "systemd":
        return result.ok and result.stdout.strip().startswith("enabled")
    if plan.manager == "brew":
        if not result.ok:
            return None
        for line in result.stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == plan.unit:
                return len(fields) > 1 and fields[1] in ("started", "scheduled")
        return False
    return None


# ---------------------------------------------------------------------------
# Credential and configuration storage
# ---------------------------------------------------------------------------


def generate_password(entropy_bytes: int = 24) -> str:
    """A password with no shell, URL or SQL metacharacters in it.

    ``token_urlsafe`` yields ``[A-Za-z0-9_-]``, which survives a DSN, a YAML
    scalar and a SQL literal untouched — an installer-generated credential
    that needs quoting in three formats is a credential that will eventually
    be corrupted by one of them.
    """
    return secrets.token_urlsafe(entropy_bytes)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines, matching :func:`src.config._load_env_file`."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def write_env_value(path: Path, key: str, value: str) -> None:
    """Set ``key`` in the daemon env file, in place, at mode ``0600``.

    The file is the daemon's credential store, so it is written the way a
    credential store must be: existing keys are replaced rather than appended
    twice, unrelated lines survive, and the mode is tightened before the value
    is readable by anyone else.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    found = False
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.split("=", 1)[0].strip() == key:
                if not found:
                    lines.append(f"{key}={value}")
                    found = True
                continue
            lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    # Create with a private mode from the start: writing then chmod-ing leaves
    # a window in which the password is world-readable.
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


@dataclass(frozen=True, slots=True)
class ConfigUpdate:
    path: Path
    created: bool
    changed: bool
    backup: Path | None = None


_CONFIG_HEADER = """\
# AQ configuration, created by `aq install`.
# Secrets are referenced as ${ENV_VAR} and resolved from the .env file beside
# this one; never paste a password here.

# Discord is optional and off until it is configured (Settings -> Messaging).
# The daemon refuses to load a config that selects Discord without credentials,
# so a freshly installed machine starts with no messaging platform selected.
messaging_platform: none
"""


def backup_path_for(path: Path) -> Path:
    """The next free ``<name>.bak[.N]`` beside *path*.

    A numbered backup rather than a timestamped one so the function stays
    clock-free and its output is reproducible in a test.
    """
    candidate = path.with_name(path.name + ".bak")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak.{index}")
        index += 1
    return candidate


def write_database_config(config_path: Path, *, url: str) -> ConfigUpdate:
    """Point ``config.yaml`` at *url*, preserving everything else in the file.

    The contract requires a versioned backup before AQ's configuration is
    changed and requires unknown, user-owned fields to survive.  Both come from
    the round-trip writer in :mod:`src.config_editor`: keys outside the
    ``database`` section keep their comments and order byte-for-byte, and the
    keys inside it that the installer does not own (pool sizes, ``pre_ping``)
    are read back and rewritten unchanged.
    """
    from src.config_editor import read_raw_config, write_section

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(_CONFIG_HEADER + "database:\n  url: ''\n", encoding="utf-8")
        section: dict[str, Any] = {}
        created = True
        backup = None
    else:
        created = False
        raw = read_raw_config(str(config_path))
        existing = raw.get("database")
        section = dict(existing) if isinstance(existing, Mapping) else {}
        if section.get("url") == url:
            return ConfigUpdate(path=config_path, created=False, changed=False)
        backup = backup_path_for(config_path)
        backup.write_bytes(config_path.read_bytes())

    section["url"] = url
    write_section(str(config_path), "database", section)
    return ConfigUpdate(path=config_path, created=created, changed=True, backup=backup)


__all__ = [
    "DEFAULT_BREW_FORMULA_VERSION",
    "DEFAULT_DATABASE",
    "DEFAULT_HOST",
    "DEFAULT_PASSWORD_ENV",
    "DEFAULT_PORT",
    "DEFAULT_ROLE",
    "IDENTIFIER_RE",
    "MINIMUM_SERVER_VERSION",
    "POSTGRES_ANSWERED",
    "REASON_AUTH",
    "REASON_ERROR",
    "REASON_MISSING_DATABASE",
    "REASON_MISSING_ROLE",
    "REASON_NOT_POSTGRES",
    "REASON_OK",
    "REASON_UNREACHABLE",
    "RESOURCE_CREDENTIAL",
    "RESOURCE_DATABASE",
    "RESOURCE_ROLE",
    "RESOURCE_SERVER",
    "AdminAccess",
    "AsyncpgExecutor",
    "CommandResult",
    "CommandRunner",
    "ConfigUpdate",
    "ConnectionCheck",
    "Connector",
    "PackagePlan",
    "PostgresSettings",
    "PostgresSettingsError",
    "PsqlExecutor",
    "ServicePlan",
    "SqlError",
    "SqlExecutor",
    "asyncpg_check",
    "backup_path_for",
    "build_url",
    "generate_password",
    "package_plan",
    "package_present",
    "quote_literal",
    "read_env_file",
    "resolve_admin",
    "service_enabled_at_boot",
    "service_plan",
    "split_password",
    "subprocess_runner",
    "tcp_open",
    "write_database_config",
    "write_env_value",
]
