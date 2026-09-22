"""Isolated GitHub CLI execution and the staged repository client adapter."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from src.git.github_app import MAX_RESPONSE_BYTES, GitHubAppClient
from src.git.github_contracts import (
    GitHubAccessError as GitHubAppError,
    GitHubCredentialIdentity,
    GitHubCredentialMode,
    GitHubRepositoryBinding,
)

MAX_STDIN_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
PROCESS_CLEANUP_SECONDS = 1.0

_TARGET_OPTIONS = ("--hostname", "--repo", "-R")
_TOKEN_ENVIRONMENT_KEYS = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    }
)
_REMOVED_ENVIRONMENT_KEYS = frozenset(
    {
        "GH_REPO",
        "GH_HOST",
        "GH_DEBUG",
        "DEBUG",
        "GH_FORCE_TTY",
        "GH_HTTP_UNIX_SOCKET",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_EXEC_PATH",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_CONFIG",
    }
)
_SAFE_ENVIRONMENT = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_PAGER": "cat",
    "PAGER": "cat",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "GH_BROWSER": "/bin/false",
    "BROWSER": "/bin/false",
    "GH_EDITOR": "/bin/false",
    "GIT_EDITOR": "/bin/false",
    "EDITOR": "/bin/false",
    "VISUAL": "/bin/false",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/false",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}
_KNOWN_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bauthorization\s*:\s*(?:bearer|token|basic)\s+)[^\s,;]+"),
    re.compile(
        r"(?i)(\b(?:GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|"
        r"GITHUB_ENTERPRISE_TOKEN)\s*=\s*)[^\s]+"
    ),
    re.compile(r"\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+\b"),
)


class GhCredentialProvider(Protocol):
    """The narrow credential seam consumed by :class:`GhRunner`.

    ``GitHubAuth`` implements this seam in the composed daemon. Keeping it as
    a protocol here lets process-containment tests inject credentials without
    importing token bootstrap or repository operations.
    """

    @property
    def credential_identity(self) -> GitHubCredentialIdentity: ...

    async def token_for(self, repository: GitHubRepositoryBinding) -> str | None: ...


class GhSelectedCredential(Protocol):
    """One already-selected credential supplied by trusted composition code.

    This seam lets App repository bootstrap verify a newly minted candidate
    through the shared runner without asking the still-unbound provider for a
    token recursively.  The runner fences the identity and repository before
    reading the secret.
    """

    identity: GitHubCredentialIdentity
    repository: GitHubRepositoryBinding
    token: str | None


class ExistingLoginCredentials:
    """Credential provider that deliberately leaves discovery to ``gh``."""

    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        return GitHubCredentialIdentity.existing_login()

    async def token_for(self, repository: GitHubRepositoryBinding) -> None:
        del repository
        return None


@dataclass(frozen=True, slots=True)
class GhResult:
    """Bounded command result with intact machine output and safe diagnostics."""

    returncode: int
    stdout: bytes
    stderr: str


class _OutputLimitExceeded(Exception):
    def __init__(self, stream_name: str):
        self.stream_name = stream_name


class _ProcessCleanupError(Exception):
    pass


class GhRunner:
    """Launch one explicitly scoped ``gh`` process in a contained environment."""

    def __init__(
        self,
        credentials: GhCredentialProvider,
        *,
        executable: str = "gh",
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_stdout_bytes: int = MAX_RESPONSE_BYTES,
        max_stderr_bytes: int = MAX_DIAGNOSTIC_BYTES,
        max_stdin_bytes: int = MAX_STDIN_BYTES,
        cleanup_timeout: float = PROCESS_CLEANUP_SECONDS,
    ) -> None:
        if not isinstance(executable, str) or not executable or "\x00" in executable:
            raise ValueError("GitHub CLI executable must be a non-empty path")
        for label, value in (
            ("timeout", timeout),
            ("cleanup_timeout", cleanup_timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{label} must be positive")
        for label, value in (
            ("max_stdout_bytes", max_stdout_bytes),
            ("max_stderr_bytes", max_stderr_bytes),
            ("max_stdin_bytes", max_stdin_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")

        self.credentials = credentials
        self.executable = executable
        self._base_env = dict(os.environ if env is None else env)
        self.timeout = float(timeout)
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_stdin_bytes = max_stdin_bytes
        self.cleanup_timeout = float(cleanup_timeout)
        self._resolved_executable: str | None = None
        self._temporary_work_dir: tempfile.TemporaryDirectory[str] | None = None
        if cwd is None:
            self._temporary_work_dir = tempfile.TemporaryDirectory(prefix="aq-gh-")
            Path(self._temporary_work_dir.name).chmod(0o700)
            self.cwd = Path(self._temporary_work_dir.name)
        else:
            resolved_cwd = Path(cwd).resolve(strict=True)
            if not resolved_cwd.is_dir():
                raise ValueError("GitHub CLI working directory must be a directory")
            self.cwd = resolved_cwd

    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        identity = self.credentials.credential_identity
        if not isinstance(identity, GitHubCredentialIdentity):
            raise ValueError("GitHub credential identity is unavailable")
        return identity

    def cli_available(self) -> bool:
        """Report executable availability without consulting any credential."""
        try:
            self._resolve_executable()
        except GitHubAppError as exc:
            if exc.category == "cli_missing":
                return False
            raise
        return True

    async def run(
        self,
        args: Sequence[str],
        *,
        repository: GitHubRepositoryBinding | None = None,
        hostname: str | None = None,
        credential: GhSelectedCredential | None = None,
        stdin: bytes | str | None = None,
        timeout: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        check: bool = True,
    ) -> GhResult:
        """Run ``gh`` with explicit target, fresh credentials and bounded I/O."""

        command = self._scoped_args(args, repository=repository, hostname=hostname)
        standard_input = self._stdin_bytes(stdin)
        self._validate_stdin_contract(command, standard_input)
        operation_timeout = self.timeout if timeout is None else timeout
        stdout_limit = self.max_stdout_bytes if max_stdout_bytes is None else max_stdout_bytes
        stderr_limit = self.max_stderr_bytes if max_stderr_bytes is None else max_stderr_bytes
        if (
            isinstance(operation_timeout, bool)
            or not isinstance(operation_timeout, (int, float))
            or operation_timeout <= 0
        ):
            raise ValueError("timeout must be positive")
        for label, value in (
            ("max_stdout_bytes", stdout_limit),
            ("max_stderr_bytes", stderr_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")

        executable = self._resolve_executable()
        identity = self.credential_identity
        token: str | None = None
        if credential is not None:
            if repository is None or credential.repository != repository:
                raise ValueError("selected GitHub credential repository did not match")
            if credential.identity != identity:
                raise ValueError("selected GitHub credential identity did not match")
            token = credential.token
            if identity.mode is GitHubCredentialMode.EXISTING_LOGIN and token is not None:
                raise ValueError("existing-login credentials cannot supply an App token")
        elif identity.mode is GitHubCredentialMode.APP:
            if repository is None:
                raise ValueError("App-backed GitHub commands require a repository binding")
            try:
                token = await self.credentials.token_for(repository)
            except GitHubAppError:
                raise
            except Exception:
                raise GitHubAppError(
                    "credentials", "GitHub App credential is unavailable"
                ) from None
            if (
                not isinstance(token, str)
                or not token
                or "\x00" in token
                or "\n" in token
                or "\r" in token
            ):
                raise GitHubAppError("credentials", "GitHub App credential is unavailable")

        argument_secrets = {
            value
            for key, value in self._base_env.items()
            if key in _TOKEN_ENVIRONMENT_KEYS and value
        }
        if token is not None:
            argument_secrets.add(token)
        if any(secret in argument for secret in argument_secrets for argument in command):
            raise ValueError("GitHub credentials must not appear in process arguments")

        app_config: tempfile.TemporaryDirectory[str] | None = None
        if identity.mode is GitHubCredentialMode.APP:
            app_config = tempfile.TemporaryDirectory(prefix=".config-", dir=self.cwd)
            Path(app_config.name).chmod(0o700)
        environment = self._environment(identity, token=token, app_config=app_config)
        process: asyncio.subprocess.Process | None = None
        diagnostic_secrets = tuple(argument_secrets)
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *command,
                cwd=str(self.cwd),
                stdin=(
                    asyncio.subprocess.PIPE
                    if standard_input is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
            async with asyncio.timeout(float(operation_timeout)):
                stdout, stderr = await self._communicate_bounded(
                    process,
                    standard_input,
                    stdout_limit=stdout_limit,
                    stderr_limit=stderr_limit,
                )
        except asyncio.TimeoutError as exc:
            if process is not None:
                await asyncio.shield(self._terminate_process_group(process))
            raise GitHubAppError("transient", "GitHub CLI request timed out") from exc
        except OSError as exc:
            raise GitHubAppError("cli_missing", "GitHub CLI is unavailable") from exc
        except _OutputLimitExceeded as exc:
            if process is not None:
                await asyncio.shield(self._terminate_process_group(process))
            raise GitHubAppError(
                "transient", f"GitHub CLI {exc.stream_name} exceeded size limit"
            ) from exc
        except _ProcessCleanupError as exc:
            if process is not None:
                await asyncio.shield(self._terminate_process_group(process, raise_on_timeout=False))
            raise GitHubAppError("transient", "GitHub CLI process cleanup failed") from exc
        except asyncio.CancelledError:
            if process is not None:
                await asyncio.shield(self._terminate_process_group(process, raise_on_timeout=False))
            raise
        finally:
            environment.pop("GH_TOKEN", None)
            token = None
            if app_config is not None:
                app_config.cleanup()

        safe_stderr = _scrub_diagnostic(stderr, secrets=diagnostic_secrets)
        result = GhResult(process.returncode or 0, stdout, safe_stderr)
        if check and result.returncode != 0:
            raise _cli_error(result.returncode, stderr)
        return result

    def _resolve_executable(self) -> str:
        if self._resolved_executable is not None:
            return self._resolved_executable
        candidate = self.executable
        if os.path.isabs(candidate):
            resolved = candidate
        elif os.sep in candidate or (os.altsep is not None and os.altsep in candidate):
            raise GitHubAppError("cli_missing", "GitHub CLI is unavailable")
        else:
            resolved = shutil.which(candidate, path=self._base_env.get("PATH", os.defpath)) or ""
        try:
            executable = Path(resolved).resolve(strict=True)
            mode = executable.stat().st_mode
        except (OSError, RuntimeError) as exc:
            raise GitHubAppError("cli_missing", "GitHub CLI is unavailable") from exc
        if not stat.S_ISREG(mode) or not os.access(executable, os.X_OK):
            raise GitHubAppError("cli_missing", "GitHub CLI is unavailable")
        self._resolved_executable = str(executable)
        return self._resolved_executable

    @staticmethod
    def _scoped_args(
        args: Sequence[str],
        *,
        repository: GitHubRepositoryBinding | None,
        hostname: str | None,
    ) -> tuple[str, ...]:
        if isinstance(args, (str, bytes)) or not args:
            raise ValueError("GitHub CLI arguments must be a non-empty sequence")
        normalized: list[str] = []
        for argument in args:
            if (
                not isinstance(argument, str)
                or not argument
                or "\x00" in argument
                or "\n" in argument
                or "\r" in argument
            ):
                raise ValueError("GitHub CLI arguments must be non-empty strings")
            if any(
                argument == option or argument.startswith(f"{option}=")
                for option in _TARGET_OPTIONS
            ):
                raise ValueError("GitHub CLI target options are runner-controlled")
            normalized.append(argument)
        if normalized[0].startswith("-"):
            raise ValueError("GitHub CLI command must name an operation")
        if repository is not None and not isinstance(repository, GitHubRepositoryBinding):
            raise ValueError("repository must be a GitHubRepositoryBinding")
        target_host = repository.forge_host if repository is not None else hostname
        if target_host != "github.com":
            raise ValueError("GitHub CLI hostname must be github.com")
        if repository is None and hostname is None:
            raise ValueError("GitHub CLI commands require an explicit repository or hostname")
        if hostname is not None and hostname != target_host:
            raise ValueError("GitHub CLI repository and hostname targets disagree")

        if normalized[0] == "api":
            return ("api", "--hostname", target_host, *normalized[1:])
        if repository is not None:
            return (*normalized, "--repo", repository.full_name)
        return (*normalized, "--hostname", target_host)

    def _stdin_bytes(self, stdin: bytes | str | None) -> bytes | None:
        if isinstance(stdin, str):
            payload = stdin.encode("utf-8")
        elif stdin is None or isinstance(stdin, bytes):
            payload = stdin
        else:
            raise ValueError("GitHub CLI stdin must be bytes, text or None")
        if payload is not None and len(payload) > self.max_stdin_bytes:
            raise ValueError("GitHub CLI stdin exceeded size limit")
        return payload

    @staticmethod
    def _validate_stdin_contract(command: Sequence[str], stdin: bytes | None) -> None:
        if command[0] == "api":
            for option in ("--field", "--raw-field", "-f", "-F"):
                if any(
                    argument == option or argument.startswith(f"{option}=") for argument in command
                ):
                    raise ValueError("GitHub API bodies must be supplied with --input -")
            if "--input" in command:
                input_index = command.index("--input")
                if input_index + 1 >= len(command) or command[input_index + 1] != "-":
                    raise ValueError("GitHub API input must be read from stdin")
                if stdin is None:
                    raise ValueError("GitHub API --input - requires stdin")
            elif stdin is not None:
                raise ValueError("GitHub API stdin requires --input -")

        if tuple(command[:2]) == ("pr", "create"):
            for option in ("--body", "-b"):
                if any(
                    argument == option or argument.startswith(f"{option}=") for argument in command
                ):
                    raise ValueError("GitHub PR bodies must be supplied with --body-file -")
            if "--body-file" in command:
                body_index = command.index("--body-file")
                if body_index + 1 >= len(command) or command[body_index + 1] != "-":
                    raise ValueError("GitHub PR body files must use stdin")
                if stdin is None:
                    raise ValueError("GitHub PR --body-file - requires stdin")

    def _environment(
        self,
        identity: GitHubCredentialIdentity,
        *,
        token: str | None,
        app_config: tempfile.TemporaryDirectory[str] | None,
    ) -> dict[str, str]:
        environment = dict(self._base_env)
        for key in tuple(environment):
            if key in _REMOVED_ENVIRONMENT_KEYS or key.startswith("GIT_CONFIG_"):
                environment.pop(key, None)
        environment.update(_SAFE_ENVIRONMENT)
        if identity.mode is GitHubCredentialMode.APP:
            for key in _TOKEN_ENVIRONMENT_KEYS:
                environment.pop(key, None)
            if token is None or app_config is None:
                raise GitHubAppError("credentials", "GitHub App credential is unavailable")
            environment.update(
                {
                    "GH_TOKEN": token,
                    "GH_HOST": "github.com",
                    "GH_CONFIG_DIR": app_config.name,
                }
            )
        return environment

    async def _communicate_bounded(
        self,
        process: asyncio.subprocess.Process,
        stdin: bytes | None,
        *,
        stdout_limit: int,
        stderr_limit: int,
    ) -> tuple[bytes, bytes]:
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            self._read_bounded(process.stdout, stdout_limit, "output")
        )
        stderr_task = asyncio.create_task(
            self._read_bounded(process.stderr, stderr_limit, "diagnostic")
        )
        wait_task = asyncio.create_task(self._wait_and_clean_descendants(process))
        input_task = (
            asyncio.create_task(self._write_stdin(process, stdin)) if stdin is not None else None
        )
        tasks = [stdout_task, stderr_task, wait_task]
        if input_task is not None:
            tasks.append(input_task)
        try:
            await asyncio.gather(*tasks)
            return stdout_task.result(), stderr_task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_and_clean_descendants(self, process: asyncio.subprocess.Process) -> None:
        await process.wait()
        await self._terminate_process_group(process)

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader,
        limit: int,
        stream_name: str,
    ) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await stream.read(min(64 * 1024, limit - size + 1))
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > limit:
                raise _OutputLimitExceeded(stream_name)
            chunks.append(chunk)

    @staticmethod
    async def _write_stdin(process: asyncio.subprocess.Process, payload: bytes) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def _terminate_process_group(
        self,
        process: asyncio.subprocess.Process,
        *,
        raise_on_timeout: bool = True,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cleanup_timeout
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(process.wait()),
                    timeout=min(0.2, max(0.0, deadline - loop.time())),
                )
            except asyncio.TimeoutError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        remaining = deadline - loop.time()
        if process.returncode is None and remaining > 0:
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=remaining)
            except asyncio.TimeoutError:
                pass
        while loop.time() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)
        if raise_on_timeout:
            raise _ProcessCleanupError


class GitHubCLIClient(GitHubAppClient):
    """Staged repository client backed by :class:`GhRunner`.

    Repository operations move to ``GitHubClient`` in the next migration
    package. Until then this compatibility adapter keeps the old surface while
    ensuring it cannot launch a second, less-contained ``gh`` process.
    """

    auth_mode: ClassVar[str] = "gh"

    def __init__(
        self,
        repository: GitHubRepositoryBinding,
        *,
        executable: str = "gh",
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        runner: GhRunner | None = None,
    ) -> None:
        self.repository = repository
        self.executable = executable
        self._env = dict(os.environ if env is None else env)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._runner = runner or GhRunner(
            ExistingLoginCredentials(),
            executable=executable,
            env=self._env,
            timeout=timeout,
            max_stdout_bytes=max_response_bytes,
        )

    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        """Return the non-secret daemon-login identity used by this client."""
        return self._runner.credential_identity

    @classmethod
    async def bind_repository(
        cls,
        full_name: str,
        *,
        executable: str = "gh",
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> GitHubCLIClient:
        provisional = GitHubRepositoryBinding(1, full_name)
        client = cls(
            provisional,
            executable=executable,
            env=env,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )
        payload = await client.request_json("GET", f"repos/{full_name}")
        repository_id = payload.get("id")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or payload.get("full_name") != full_name
        ):
            raise GitHubAppError("credentials", "authenticated repository identity did not match")
        client.repository = GitHubRepositoryBinding(repository_id, full_name)
        return client

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        del expected_statuses  # Framed status handling moves to the common client.
        payload = await self._api_json(method, path, json_body=json_body)
        if not isinstance(payload, dict):
            raise GitHubAppError("conflict_or_invalid", "GitHub response was not an object")
        return payload

    async def paged_items(
        self, path: str, *, key: str, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        pages = await self._api_json("GET", path, paginate=True)
        if not isinstance(pages, list) or len(pages) > max_pages:
            raise GitHubAppError("transient", "GitHub pagination exceeded page limit")
        items: list[dict[str, Any]] = []
        for payload in pages:
            page = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise GitHubAppError("conflict_or_invalid", "GitHub page was malformed")
            items.extend(page)
        return items

    async def paged_list(self, path: str, *, max_pages: int = 20) -> list[dict[str, Any]]:
        pages = await self._api_json("GET", path, paginate=True)
        if not isinstance(pages, list) or len(pages) > max_pages:
            raise GitHubAppError("transient", "GitHub pagination exceeded page limit")
        items: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise GitHubAppError("conflict_or_invalid", "GitHub page was malformed")
            items.extend(page)
        return items

    async def installation_token(self, *, force_refresh: bool = False) -> None:
        """Signal that Git must use the existing ``gh`` credential, not a token."""
        del force_refresh
        return None

    async def _api_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        paginate: bool = False,
    ) -> Any:
        method = method.upper()
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise ValueError("unsupported GitHub API method")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith(("-", "//", "http://", "https://"))
            or any(character in path for character in ("\n", "\r", "\x00"))
        ):
            raise ValueError("GitHub API path must be a repository-bound endpoint")
        endpoint = path[1:] if path.startswith("/") else path
        resource_path = endpoint.partition("?")[0]
        allowed_roots = (
            f"repos/{self.repository.full_name}",
            f"repositories/{self.repository.repository_id}",
        )
        if not any(
            resource_path == root or resource_path.startswith(f"{root}/") for root in allowed_roots
        ):
            raise ValueError("GitHub API path must be a repository-bound endpoint")
        if json_body is not None and not isinstance(json_body, dict):
            raise ValueError("GitHub API JSON body must be an object")
        args = ["api", "--method", method, endpoint]
        if json_body is not None:
            args.extend(["--input", "-"])
        if paginate:
            args.append("--paginate")
        stdin = (
            json.dumps(json_body, separators=(",", ":")).encode() if json_body is not None else None
        )
        result = await self._runner.run(args, repository=self.repository, stdin=stdin)
        stdout = result.stdout
        try:
            if not paginate:
                return json.loads(stdout)
            # gh 2.45 emits successive JSON documents and has no --slurp.
            remaining = stdout.decode("utf-8").lstrip()
            decoder = json.JSONDecoder()
            pages = []
            while remaining:
                page, end = decoder.raw_decode(remaining)
                pages.append(page)
                remaining = remaining[end:].lstrip()
            return pages
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubAppError(
                "conflict_or_invalid", "GitHub response was not valid JSON"
            ) from exc


def _scrub_diagnostic(diagnostic: bytes, *, secrets: Sequence[str] = ()) -> str:
    scrubbed = diagnostic.decode("utf-8", "replace")
    for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    for pattern in _KNOWN_SECRET_PATTERNS:
        if pattern.groups:
            scrubbed = pattern.sub(r"\1[REDACTED]", scrubbed)
        else:
            scrubbed = pattern.sub("[REDACTED]", scrubbed)
    return scrubbed


def _cli_error(returncode: int, stderr: bytes) -> GitHubAppError:
    diagnostic = stderr.decode("utf-8", "replace")
    status_match = re.search(r"\bHTTP\s+(\d{3})\b", diagnostic, flags=re.IGNORECASE)
    status = int(status_match.group(1)) if status_match else None
    if returncode == 4 or status == 401:
        category = "credentials"
    elif status == 404:
        category = "not_found_or_hidden"
    elif status in {409, 422}:
        category = "conflict_or_invalid"
    elif status in {403, 429} and "rate limit" in diagnostic.lower():
        category = "rate_limited"
    elif status == 403:
        category = "permission"
    else:
        category = "transient"
    return GitHubAppError(category, "GitHub CLI request failed")


__all__ = [
    "ExistingLoginCredentials",
    "GhCredentialProvider",
    "GhResult",
    "GhRunner",
    "GhSelectedCredential",
    "GitHubCLIClient",
]
