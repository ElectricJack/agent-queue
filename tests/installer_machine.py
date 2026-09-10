"""One scripted machine behind every seam ``aq install`` reaches the host through.

The installer is six adapters composed by :func:`src.install.registry.build_registry`:
admission, prerequisites, PostgreSQL, provider CLIs, provider logins and
onboarding.  Each of them touches the host through an injected callable — a
command runner, a ``which``, a TCP probe, a database connector, an HTTP probe —
and every one of those is a constructor argument rather than an import.  This
module supplies all of them from a single object, so a whole-installer test
runs the *real* engine over the *real* composition on a machine that has no
PostgreSQL, no provider CLI, no daemon and no network.

What the machine deliberately does **not** provide is a fallback: a command,
statement or probe it does not recognise raises ``AssertionError``.  A step that
reaches for something unscripted is a step doing something the installer's
contract does not describe, and the test should say so loudly rather than
quietly succeed.

Every path is under one temporary root: the user's home (where a provider's
credential store lives) and AQ's install home (config, ``.env``, vault, resume
record) are siblings, so nothing here can resolve to the operator's own
``~/.agent-queue`` or read a real credential.  See
``docs/contributing/installer-testing.md`` for what this can and cannot prove.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.install import InstallEngine, InstallOptions, InstallResult
from src.install.command import CommandOutput
from src.install.lifecycle import LifecycleMode
from src.install.logins import provider_logins
from src.install.platform import PlatformFacts, SupportVerdict
from src.install.postgres import (
    REASON_AUTH,
    REASON_MISSING_DATABASE,
    REASON_MISSING_ROLE,
    REASON_NOT_POSTGRES,
    REASON_OK,
    REASON_UNREACHABLE,
    CommandResult,
    ConnectionCheck,
    split_password,
)
from src.install.postgres_steps import PostgresAdapter
from src.install.providers import provider_installers
from src.install.registry import build_registry
from src.install.steps import StepRegistry

#: The commands a supported machine already has before AQ installs anything.
BASE_EXECUTABLES: tuple[str, ...] = (
    "git",
    "tmux",
    "psql",
    "sudo",
    "apt-get",
    "dpkg-query",
    "systemctl",
    "aq",
    "npm",
)


def facts(**overrides: Any) -> PlatformFacts:
    base: dict[str, Any] = {
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


WSL2 = SupportVerdict(host_path="windows-wsl2", tier="supported", facts=facts())


@dataclass
class Provider:
    """One harness CLI as this machine has it: present, working, signed in."""

    installed: bool = False
    signed_in: bool = False
    #: Whether the provider's documented installer succeeds on this machine.
    installs: bool = True
    #: Whether the executable is actually on PATH after its installer ran.
    #: ``False`` reproduces the "open a new shell" case.
    lands_on_path: bool = True
    version: str = "1.4.2"


@dataclass
class Database:
    """An in-memory PostgreSQL: what answers, which roles and databases exist."""

    listening: bool = False
    #: Something answers the port but is not PostgreSQL — the port-conflict case.
    speaks_postgres: bool = True
    roles: dict[str, str] = field(default_factory=dict)
    databases: set[str] = field(default_factory=set)
    version_num: int = 160004
    statements: list[str] = field(default_factory=list)

    def connect(self, url: str, timeout: float) -> ConnectionCheck:
        del timeout
        if not self.listening:
            return ConnectionCheck(REASON_UNREACHABLE, "connection refused")
        if not self.speaks_postgres:
            return ConnectionCheck(REASON_NOT_POSTGRES, "unexpected response")
        from urllib.parse import unquote, urlsplit

        _, password = split_password(url)
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
            server_version=f"PostgreSQL {self.version_num // 10000}.0",
            server_version_num=self.version_num,
        )

    def execute(self, sql: str) -> str:
        import re

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


def provisioned(password: str = "existing-password") -> Database:
    """A PostgreSQL somebody else already installed, with AQ's role and database."""
    return Database(
        listening=True,
        roles={"agent_queue": password, "postgres": "", "other_app": "unrelated"},
        databases={"agent_queue", "postgres", "other_app"},
    )


class Machine:
    """A scripted host, and the registry that installs onto it."""

    def __init__(
        self,
        root: Path,
        *,
        support: SupportVerdict = WSL2,
        database: Database | None = None,
        executables: Sequence[str] = BASE_EXECUTABLES,
        packages: Sequence[str] = (),
        providers: Mapping[str, Provider] | None = None,
        sudo: bool = True,
        systemd: bool = True,
        service_starts: bool = True,
        boot_enabled: bool = False,
        daemon_up: bool = False,
        daemon_starts: bool = True,
        dashboard_status: int | None = 200,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.root = root
        self.support = support
        self.database = database if database is not None else Database()
        self.executables = set(executables)
        self.packages = set(packages)
        self.providers: dict[str, Provider] = dict(providers or {})
        for installer in provider_installers():
            self.providers.setdefault(installer.id, Provider())
            if self.providers[installer.id].installed:
                self.executables.add(installer.executable)
        self.sudo = sudo
        self.systemd = systemd
        self.service_starts = service_starts
        self.boot_enabled = boot_enabled
        self.daemon_up = daemon_up
        self.daemon_starts = daemon_starts
        self.dashboard_status = dashboard_status

        self.user_home = root / "home"
        self.aq_home = self.user_home / ".agent-queue"
        self.user_home.mkdir(parents=True, exist_ok=True)
        #: Every command any adapter ran, in order, across all three runners.
        self.commands: list[tuple[str, ...]] = []
        self._passwords = iter(f"generated-password-{index}" for index in range(1, 100))
        self._environ = dict(environ or {})
        self._environ.setdefault("HOME", str(self.user_home))
        self._environ.setdefault("USER", "installer")

    # -- paths and environment ---------------------------------------------
    @property
    def environ(self) -> dict[str, str]:
        return dict(self._environ)

    def set_env(self, **values: str) -> None:
        self._environ.update(values)

    @property
    def config_path(self) -> Path:
        return self.aq_home / "config.yaml"

    @property
    def env_path(self) -> Path:
        return self.aq_home / ".env"

    @property
    def state_path(self) -> Path:
        return self.aq_home / "install-state.json"

    # -- host observations --------------------------------------------------
    def which(self, command: str) -> str | None:
        return f"/usr/bin/{command}" if command in self.executables else None

    def path_exists(self, path: str) -> bool:
        if path == "/run/systemd/system":
            return self.systemd
        return True

    def tcp(self, host: str, port: int, timeout: float) -> bool:
        del host, port, timeout
        return self.database.listening

    def connect(self, url: str, timeout: float) -> ConnectionCheck:
        return self.database.connect(url, timeout)

    def http(self, url: str) -> int | None:
        if url.endswith("/health"):
            return 200 if self.daemon_up else None
        if url.endswith("/dashboard"):
            return self.dashboard_status if self.daemon_up else None
        raise AssertionError(f"unexpected probe: {url}")

    # -- providers ----------------------------------------------------------
    def store_path(self, provider_id: str) -> Path:
        login = next(item for item in provider_logins() if item.provider_id == provider_id)
        return login.stores[0].resolve(self.environ)

    def sign_in(self, provider_id: str) -> None:
        """What the human does in their own terminal: finish the provider login."""
        provider = self.providers[provider_id]
        provider.signed_in = True
        store = self.store_path(provider_id)
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text('{"note": "opaque provider credential"}', encoding="utf-8")

    def sign_out(self, provider_id: str) -> None:
        self.providers[provider_id].signed_in = False
        self.store_path(provider_id).unlink(missing_ok=True)

    # -- the three runners --------------------------------------------------
    def run_postgres(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        del env, timeout
        argv = tuple(str(part) for part in argv)
        self.commands.append(argv)
        joined = " ".join(argv)

        if argv[:3] == ("sudo", "-n", "true"):
            return CommandResult(
                argv,
                0 if self.sudo else 1,
                "",
                "" if self.sudo else "sudo: a password is required",
            )
        if "psql" in joined:
            if not self.database.listening or not self.database.speaks_postgres:
                # What psql prints when the port answers with something else,
                # or with nothing at all.  A step that keeps going from here is
                # a step that has to survive a server it cannot talk to.
                return CommandResult(
                    argv,
                    2,
                    "",
                    'psql: error: connection to server at "localhost" (127.0.0.1), '
                    "port 5432 failed",
                )
            try:
                return CommandResult(argv, 0, self.database.execute(stdin or ""), "")
            except RuntimeError as error:
                return CommandResult(argv, 1, "", f"ERROR:  {error}")
        if "dpkg-query" in joined:
            package = argv[-1]
            present = package in self.packages
            return CommandResult(
                argv,
                0 if present else 1,
                "install ok installed" if present else "",
                "" if present else f"dpkg-query: no packages found matching {package}",
            )
        if "apt-get" in joined and "install" in joined:
            self.packages.add(argv[-1])
            return CommandResult(argv, 0, f"Setting up {argv[-1]}", "")
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
            self.database.listening = True
            return CommandResult(argv, 0, "", "")
        raise AssertionError(f"unexpected host command: {argv}")

    def run_provider(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = tuple(str(part) for part in argv)
        self.commands.append(argv)

        for installer in provider_installers():
            provider = self.providers[installer.id]
            if argv == installer.install_command:
                if not provider.installs:
                    return self._completed(argv, 1, stderr="installer exited with an error")
                provider.installed = True
                if provider.lands_on_path:
                    self.executables.add(installer.executable)
                return self._completed(argv, 0, stdout=f"installed {installer.executable}")
            if argv == (installer.executable, "--version"):
                if not provider.installed:
                    raise OSError(f"{installer.executable}: no such file")
                return self._completed(argv, 0, stdout=f"{installer.executable} {provider.version}")

        for login in provider_logins():
            if login.status_command and argv == login.status_command:
                provider = self.providers[login.provider_id]
                return self._completed(argv, 0 if provider.signed_in else 1)
        raise AssertionError(f"unexpected provider command: {argv}")

    def run_daemon(self, argv: Sequence[str], **kwargs: Any) -> CommandOutput:
        del kwargs
        argv = tuple(str(part) for part in argv)
        self.commands.append(argv)
        if argv[1:] == ("start",):
            if not self.daemon_starts:
                return CommandOutput(argv=argv, returncode=1, stderr="database is unreachable")
            self.daemon_up = True
            return CommandOutput(argv=argv, returncode=0, stdout="Daemon started")
        raise AssertionError(f"unexpected daemon command: {argv}")

    @staticmethod
    def _completed(
        argv: tuple[str, ...], returncode: int, *, stdout: str = "", stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)

    # -- the installer ------------------------------------------------------
    def adapter(self) -> PostgresAdapter:
        return PostgresAdapter(
            runner=self.run_postgres,
            which=self.which,
            probe=self.tcp,
            connect=self.connect,
            environ=self.environ,
            state_dir=self.aq_home,
            path_exists=self.path_exists,
            sleep=lambda seconds: None,
            monotonic=lambda: 0.0,
            make_password=lambda: next(self._passwords),
        )

    def registry(self) -> StepRegistry:
        """The registry ``aq install`` composes, with this machine behind it."""
        return build_registry(
            self.support,
            environ=self.environ,
            which=self.which,
            state_dir=self.aq_home,
            database_steps=self.adapter().steps(),
            provider_runner=self.run_provider,
            daemon_runner=self.run_daemon,
            http_probe=self.http,
        )

    def install(
        self,
        *,
        capabilities: Sequence[str] = (),
        settings: Mapping[str, Any] | None = None,
        mode: LifecycleMode = LifecycleMode.INSTALL,
        dry_run: bool = False,
        interactive: bool = False,
        approve: Sequence[str] = ("*",),
        restart_from: str | None = None,
        resume: bool = True,
        fresh: bool = False,
        installer_version: str = "1.2.3",
        target_version: str = "1.2.3",
        state_path: Path | None = None,
    ) -> InstallResult:
        options = InstallOptions(
            installer_version=installer_version,
            target_version=target_version,
            mode=mode,
            interactive=interactive,
            dry_run=dry_run,
            resume=resume,
            fresh=fresh,
            restart_from=restart_from,
            capabilities=frozenset(capabilities),
            approve=frozenset(approve),
            settings=dict(settings or {}),
            state_path=state_path or self.state_path,
        )
        return InstallEngine(self.registry(), options, support=self.support).run()

    # -- reading a run back -------------------------------------------------
    def ran(self, *fragments: str) -> list[tuple[str, ...]]:
        """Every recorded command whose text contains all *fragments*."""
        return [
            command
            for command in self.commands
            if all(fragment in " ".join(command) for fragment in fragments)
        ]

    def forget(self) -> None:
        """Forget what happened so far, so the next run is read on its own.

        Both logs are cleared together: "did this rerun install anything?" is a
        question about commands *and* about statements, and remembering one half
        of a previous run is how a rerun assertion silently passes.
        """
        self.commands.clear()
        self.database.statements.clear()


def step(result: InstallResult, step_id: str):
    """The result of one step, by id — an ``AssertionError`` when it never ran."""
    for row in result.steps:
        if row.step_id == step_id:
            return row
    raise AssertionError(f"{step_id} is not in the result: {[s.step_id for s in result.steps]}")


def states(result: InstallResult) -> dict[str, str]:
    return {row.step_id: row.state.value for row in result.steps}


def resources(result: InstallResult) -> dict[tuple[str, str], Any]:
    return {record.key: record for record in result.resources}
