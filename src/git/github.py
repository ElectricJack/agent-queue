"""Startup-composed GitHub authentication and repository-bound operations.

``GitHubAccess`` owns credential selection and the trusted ``gh`` runner.
``GitHubClient`` supplies immutable repository targeting, validates every API
endpoint, and decodes bounded responses without depending on credential mode.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol
from urllib.parse import quote, unquote, urlsplit

from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubCredentialMode,
    GitHubRepositoryBinding,
)

if TYPE_CHECKING:
    from src.config import GitHubAppConfig
    from src.git.github_app import AppTokenCandidate
    from src.git.github_auth import GitHubAuth, GitHubCredential, TokenProvider
    from src.git.github_cli import GhResult, GhRunner


DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_DIAGNOSTIC_BYTES = 256 * 1024
MAX_STDIN_BYTES = 1024 * 1024


BINDING_REVERIFY_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class GitHubAccessStatus:
    """Non-secret status for the effective startup credential composition."""

    cli_available: bool
    credential_identity: GitHubCredentialIdentity
    configuration_scope: Literal["startup"] = "startup"
    configuration_changes_require_restart: bool = True

    def to_dict(self) -> dict[str, Any]:
        identity = self.credential_identity
        return {
            "cli_available": self.cli_available,
            "credential_mode": identity.mode.value,
            "app_id": identity.app_id,
            "installation_id": identity.installation_id,
            "configuration_scope": self.configuration_scope,
            "configuration_changes_require_restart": self.configuration_changes_require_restart,
        }


class GitHubWriteNotStarted(GitHubAccessError):
    """Credential selection failed before a mutating command could launch."""


class GitHubAccess:
    """One startup-owned authentication service and shared ``gh`` runner."""

    def __init__(self, auth: GitHubAuth, runner: GhRunner) -> None:
        if runner.credential_identity != auth.credential_identity:
            raise ValueError("GitHub auth and runner credential identities do not match")
        self.auth = auth
        self.runner = runner
        self._bindings: dict[str, GitHubRepositoryBinding] = {}
        self._binding_verified_at: dict[str, float] = {}
        self._binding_clock: Callable[[], float] = time.monotonic

    @classmethod
    def from_config(
        cls,
        app_config: GitHubAppConfig | None = None,
        *,
        app_provider: TokenProvider | None = None,
        clock=time.time,
        executable: str = "gh",
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int = MAX_DIAGNOSTIC_BYTES,
        max_stdin_bytes: int = MAX_STDIN_BYTES,
    ) -> GitHubAccess:
        """Capture the effective credential mode and runner configuration.

        The returned service is intentionally independent of future config
        reloads.  Changing App identity, installation, key reference or mode
        requires constructing a new service during daemon restart.
        """
        from src.git.github_auth import GitHubAuth
        from src.git.github_cli import GhRunner

        auth = GitHubAuth(app_config, app_provider=app_provider, clock=clock)
        runner_options: dict[str, Any] = {
            "executable": executable,
            "env": env,
            "cwd": cwd,
            "timeout": timeout,
            "max_stderr_bytes": max_stderr_bytes,
            "max_stdin_bytes": max_stdin_bytes,
        }
        if max_stdout_bytes is not None:
            runner_options["max_stdout_bytes"] = max_stdout_bytes
        return cls(auth, GhRunner(auth, **runner_options))

    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        return self.auth.credential_identity

    def _require_account_gists(self) -> None:
        if self.credential_identity.mode is GitHubCredentialMode.APP:
            raise GitHubAccessError(
                "github_operation_unsupported",
                "Profile gists are unavailable with a configured GitHub App; "
                "use YAML directly or configure an existing gh login and restart the daemon",
            )

    async def create_repository(
        self,
        name: str,
        *,
        private: bool = True,
        org: str | None = None,
        description: str = "",
    ) -> str:
        """Create an account repository using the startup-selected existing login."""
        if self.credential_identity.mode is GitHubCredentialMode.APP:
            raise GitHubAccessError(
                "github_operation_unsupported",
                "Repository creation is unavailable with a configured GitHub App; "
                "configure an existing gh login and restart the daemon",
            )

        from src.projects.github import GitHubError, parse_github_repository

        if not isinstance(name, str) or name.startswith("-") or (
            org is not None and not isinstance(org, str)
        ):
            raise GitHubAccessError("conflict_or_invalid", "GitHub repository name was invalid")
        try:
            target = parse_github_repository(f"{org or 'placeholder'}/{name}")
        except GitHubError as exc:
            raise GitHubAccessError("conflict_or_invalid", "GitHub repository name was invalid") from exc
        full_name = target.full_name if org else name

        auth = await self.runner.run(
            ["auth", "status"], hostname="github.com", timeout=30, check=False
        )
        if auth.returncode:
            raise GitHubAccessError(
                "credentials",
                "GitHub CLI is not authenticated. Run `gh auth login` on the host to configure credentials.",
            )

        command = ["repo", "create", full_name, "--private" if private else "--public"]
        if description:
            command.extend(("--description", description))
        result = await self.runner.run(command, hostname="github.com", timeout=60, check=False)
        if result.returncode:
            if "already exists" in result.stderr.lower():
                raise GitHubAccessError("conflict_or_invalid", "GitHub repository already exists")
            raise _command_error(result.returncode, result.stderr)
        output = result.stdout.decode("utf-8", errors="replace")
        for line in (*output.splitlines(), *result.stderr.splitlines()):
            url = line.strip()
            if not url.startswith("https://"):
                continue
            try:
                created = parse_github_repository(url)
            except GitHubError:
                continue
            if created.name.lower() == name.lower() and (
                org is None or created.owner.lower() == org.lower()
            ):
                return url
        raise GitHubAccessError(
            "conflict_or_invalid", "GitHub repository creation did not return the requested URL"
        )

    async def create_profile_gist(self, profile_id: str, name: str, yaml_text: str) -> str:
        """Publish a public profile gist through the startup-owned credential authority."""
        self._require_account_gists()
        filename = re.sub(r"[^A-Za-z0-9._-]", "-", profile_id)[:80] or "profile"
        payload = await self._account_gist_request(
            "POST",
            "gists",
            json_body={
                "description": f"Agent Profile: {name}",
                "public": True,
                "files": {f"agent-profile-{filename}.yaml": {"content": yaml_text}},
            },
            expected_status=201,
        )
        url = payload.get("html_url")
        gist_id = payload.get("id")
        if not isinstance(url, str) or not isinstance(gist_id, str):
            raise GitHubAccessError("conflict_or_invalid", "GitHub gist response was incomplete")
        if _profile_gist_id(url) != gist_id:
            raise GitHubAccessError("conflict_or_invalid", "GitHub gist response identity differed")
        return url

    async def read_profile_gist(self, url: str) -> str:
        """Read one YAML profile file from a validated GitHub gist URL."""
        self._require_account_gists()
        gist_id = _profile_gist_id(url)
        payload = await self._account_gist_request(
            "GET", f"gists/{gist_id}", expected_status=200
        )
        if payload.get("id") != gist_id:
            raise GitHubAccessError("conflict_or_invalid", "GitHub gist response identity differed")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            raise GitHubAccessError("conflict_or_invalid", "GitHub gist contains no profile file")
        candidates = [
            value
            for filename, value in files.items()
            if isinstance(filename, str)
            and filename.lower().endswith((".yaml", ".yml"))
            and isinstance(value, dict)
        ]
        if len(candidates) != 1:
            raise GitHubAccessError(
                "conflict_or_invalid", "GitHub gist must contain exactly one YAML profile file"
            )
        file = candidates[0]
        content = file.get("content")
        if file.get("truncated") or not isinstance(content, str):
            raise GitHubAccessError("conflict_or_invalid", "GitHub gist profile file is incomplete")
        return content

    async def _account_gist_request(
        self,
        method: Literal["GET", "POST"],
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected_status: int,
    ) -> dict[str, Any]:
        args = [
            "api",
            "--include",
            "--method",
            method,
            "--header",
            f"Accept: {_ACCEPT}",
            "--header",
            f"X-GitHub-Api-Version: {_API_VERSION}",
            endpoint,
        ]
        stdin = None
        if json_body is not None:
            args.extend(("--input", "-"))
            stdin = json.dumps(json_body, separators=(",", ":")).encode()
        result = await self.runner.run(
            args,
            hostname="github.com",
            stdin=stdin,
            max_stdout_bytes=MAX_HEADER_BYTES + MAX_RESPONSE_BYTES,
            check=False,
        )
        try:
            response = _decode_included_response(
                result.stdout,
                max_header_bytes=MAX_HEADER_BYTES,
                max_body_bytes=MAX_RESPONSE_BYTES,
            )
        except GitHubAccessError:
            if result.returncode:
                raise _command_error(result.returncode, result.stderr) from None
            raise
        if response.status != expected_status:
            raise _http_error(response.status, response.headers, time.time())
        if result.returncode:
            raise _command_error(result.returncode, result.stderr)
        payload = _decode_json(response.body)
        if not isinstance(payload, dict):
            raise GitHubAccessError("conflict_or_invalid", "GitHub gist response was not an object")
        return payload

    async def installation_token(
        self,
        repository: GitHubRepositoryBinding,
        *,
        force_refresh: bool = False,
    ) -> str | None:
        """Return the selected Git transfer credential for one exact binding.

        Repository API calls stay on the shared runner.  Integration's
        isolated Git transfer path still needs the same credential authority
        during the staged migration, so expose that narrow adapter here rather
        than letting consumers construct a second App client.
        """
        return await self.auth.installation_token(
            repository,
            force_refresh=force_refresh,
        )

    def status(self) -> GitHubAccessStatus:
        """Report mode and CLI availability without a personal identity call."""
        return GitHubAccessStatus(
            cli_available=self.runner.cli_available(),
            credential_identity=self.credential_identity,
        )

    async def bind_repository(
        self,
        repository_reference: str,
        *,
        expected_binding: GitHubRepositoryBinding | None = None,
    ) -> GitHubRepositoryBinding:
        """Resolve and verify one trusted repository through the shared runner.

        ``expected_binding`` is the durable project/task authority when one
        already exists.  Supplying a different URL cannot borrow that
        authority to mint or use a credential for another repository.
        """
        full_name = self._canonical_full_name(repository_reference)
        if expected_binding is not None and expected_binding.full_name != full_name:
            raise GitHubAccessError(
                "conflict_or_invalid",
                "GitHub repository reference did not match the authorized repository",
            )

        binding = self._bindings.get(full_name)
        if binding is not None:
            if expected_binding is not None and binding != expected_binding:
                raise GitHubAccessError(
                    "credentials", "authenticated repository identity did not match",
                )
            # A recently verified identity is reused without another API call:
            # re-verifying on every bind tripped GitHub's secondary rate limit.
            verified_at = self._binding_verified_at.get(full_name)
            if verified_at is not None and self._binding_clock() - verified_at < BINDING_REVERIFY_SECONDS:
                return binding
            # Retain identity only, never a selected token. Every verification
            # goes through the same expiry/401 recovery as other repository reads.
            result = await self.run_read(
                ["api", "--method", "GET", f"repos/{full_name}"],
                repository=binding,
            )
            verified = self._verified_binding(
                result, full_name=full_name, expected_binding=binding,
            )
            self._binding_verified_at[full_name] = self._binding_clock()
            return verified

        self._check_backoff()
        return await self._bind_new_repository(full_name, expected_binding=expected_binding)

    async def _bind_new_repository(
        self,
        full_name: str,
        *,
        expected_binding: GitHubRepositoryBinding | None,
    ) -> GitHubRepositoryBinding:
        """Verify an unbound candidate; only a CLI authentication rejection is replayable."""
        candidate: AppTokenCandidate | None = None
        selected: AppTokenCandidate | GitHubCredential
        if self.auth.mode is GitHubCredentialMode.APP:
            candidate = await self.auth.candidate_for_repository(full_name)
            if expected_binding is not None and candidate.repository != expected_binding:
                await self.auth.discard_candidate(candidate)
                raise GitHubAccessError(
                    "credentials",
                    "authenticated repository identity did not match",
                )
            binding = candidate.repository
            selected = candidate
        else:
            binding = expected_binding or GitHubRepositoryBinding(1, full_name)
            selected = await self.auth.credential_for(binding)

        verification_binding = binding if candidate is not None else expected_binding
        try:
            try:
                result = await self.runner.run(
                    ["api", "--method", "GET", f"repos/{full_name}"],
                    repository=binding,
                    credential=selected,
                )
            except GitHubAccessError as exc:
                if candidate is None or exc.category != "credentials":
                    raise
                await self.auth.discard_candidate(candidate)
                candidate = await self.auth.candidate_for_repository(full_name)
                if candidate.repository != binding:
                    raise GitHubAccessError(
                        "credentials", "authenticated repository identity did not match",
                    )
                result = await self.runner.run(
                    ["api", "--method", "GET", f"repos/{full_name}"],
                    repository=binding,
                    credential=candidate,
                )
            verified = self._verified_binding(
                result,
                full_name=full_name,
                expected_binding=verification_binding,
            )
            if candidate is not None:
                if verified != candidate.repository:
                    raise GitHubAccessError(
                        "credentials",
                        "authenticated repository identity did not match",
                    )
                await self.auth.accept_candidate(candidate)
            if candidate is not None:
                self._bindings[full_name] = verified
                self._binding_verified_at[full_name] = self._binding_clock()
            return verified
        except asyncio.CancelledError:
            if candidate is not None:
                await asyncio.shield(self.auth.discard_candidate(candidate))
            raise
        except Exception:
            if candidate is not None:
                await self.auth.discard_candidate(candidate)
            raise

    @classmethod
    def validate_repository_reference(
        cls,
        binding: GitHubRepositoryBinding,
        repository_reference: str,
    ) -> None:
        """Reject a repository URL/name outside an existing authority."""
        if cls._canonical_full_name(repository_reference) != binding.full_name:
            raise GitHubAccessError(
                "conflict_or_invalid",
                "GitHub repository reference did not match the authorized repository",
            )

    @classmethod
    def validate_pr_url(cls, binding: GitHubRepositoryBinding, pr_url: str) -> int:
        """Return a positive PR number only for this binding's canonical host/name."""
        if not isinstance(pr_url, str) or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in pr_url
        ):
            raise GitHubAccessError("conflict_or_invalid", "GitHub pull request URL was invalid")
        try:
            parts = urlsplit(pr_url)
        except ValueError as exc:
            raise GitHubAccessError(
                "conflict_or_invalid", "GitHub pull request URL was invalid"
            ) from exc
        try:
            port = parts.port
        except ValueError:
            port = -1
        path = parts.path.strip("/").split("/")
        if (
            parts.scheme.lower() != "https"
            or (parts.hostname or "").lower() not in {"github.com", "www.github.com"}
            or parts.username is not None
            or parts.password is not None
            or port not in (None, 443)
            or parts.query
            or parts.fragment
            or len(path) != 4
            or path[2] != "pull"
            or not path[3].isdigit()
            or int(path[3]) <= 0
        ):
            raise GitHubAccessError("conflict_or_invalid", "GitHub pull request URL was invalid")
        cls.validate_repository_reference(
            binding,
            f"https://{parts.hostname}/{path[0]}/{path[1]}",
        )
        return int(path[3])

    async def run_read(
        self,
        args: Sequence[str],
        *,
        repository: GitHubRepositoryBinding,
        stdin: bytes | str | None = None,
        timeout: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        check_result: Callable[[GhResult], None] | None = None,
    ) -> GhResult:
        """Run a read and refresh one rejected App generation at most once."""
        self._check_backoff()
        selected = await self.auth.credential_for(repository)
        try:
            return await self._run(
                args,
                repository=repository,
                credential=selected,
                stdin=stdin,
                timeout=timeout,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
                check_result=check_result,
            )
        except GitHubAccessError as exc:
            if exc.category == "credentials":
                # An auth rejection forces the next bind to re-verify identity.
                self._binding_verified_at.pop(repository.full_name, None)
            if self.auth.mode is not GitHubCredentialMode.APP or exc.category != "credentials":
                raise
        await self.auth.invalidate(repository, generation=selected.generation)
        refreshed = await self.auth.refresh_after_rejection(
            repository,
            rejected_generation=selected.generation,
        )
        try:
            return await self._run(
                args,
                repository=repository,
                credential=refreshed,
                stdin=stdin,
                timeout=timeout,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
                check_result=check_result,
            )
        except GitHubAccessError as exc:
            if exc.category == "credentials":
                await self.auth.invalidate(repository, generation=refreshed.generation)
            raise

    async def run_write(
        self,
        args: Sequence[str],
        *,
        repository: GitHubRepositoryBinding,
        stdin: bytes | str | None = None,
        timeout: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        check_result: Callable[[GhResult], None] | None = None,
    ) -> GhResult:
        """Run a write once; an auth failure invalidates but never replays it."""
        try:
            self._check_backoff()
            selected = await self.auth.credential_for(repository)
        except GitHubAccessError as exc:
            raise GitHubWriteNotStarted(
                exc.category, str(exc), retry_at=exc.retry_at, http_status=exc.http_status
            ) from exc
        try:
            return await self._run(
                args,
                repository=repository,
                credential=selected,
                stdin=stdin,
                timeout=timeout,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
                check_result=check_result,
            )
        except GitHubAccessError as exc:
            if self.auth.mode is GitHubCredentialMode.APP and exc.category == "credentials":
                await self.auth.invalidate(repository, generation=selected.generation)
            raise

    def _check_backoff(self) -> None:
        check_backoff = getattr(self.runner, "check_backoff", None)
        if callable(check_backoff):
            check_backoff()

    async def _run(
        self,
        args: Sequence[str],
        *,
        repository: GitHubRepositoryBinding,
        credential: GitHubCredential,
        stdin: bytes | str | None,
        timeout: float | None,
        max_stdout_bytes: int | None,
        max_stderr_bytes: int | None,
        check_result: Callable[[GhResult], None] | None,
    ) -> GhResult:
        result = await self.runner.run(
            args,
            repository=repository,
            credential=credential,
            stdin=stdin,
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            check=check_result is None,
        )
        if check_result is not None:
            _validate_cli_result(self.runner, repository, result, check_result)
        return result

    @staticmethod
    def _canonical_full_name(repository_reference: str) -> str:
        # Keep the staged dependency lazy: the onboarding adapter will import
        # this shared module when its production cutover lands in package 3.
        from src.projects.github import GitHubError, parse_github_repository

        try:
            return parse_github_repository(repository_reference).full_name
        except GitHubError as exc:
            raise GitHubAccessError(
                "conflict_or_invalid",
                "GitHub repository reference was invalid",
            ) from exc

    @staticmethod
    def _verified_binding(
        result: GhResult,
        *,
        full_name: str,
        expected_binding: GitHubRepositoryBinding | None,
    ) -> GitHubRepositoryBinding:
        try:
            payload = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubAccessError(
                "conflict_or_invalid",
                "GitHub repository response was not valid JSON",
            ) from exc
        repository_id = payload.get("id") if isinstance(payload, dict) else None
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or not isinstance(payload, dict)
            or payload.get("full_name") != full_name
        ):
            raise GitHubAccessError(
                "credentials",
                "authenticated repository identity did not match",
            )
        binding = GitHubRepositoryBinding(repository_id, full_name)
        if expected_binding is not None and binding != expected_binding:
            raise GitHubAccessError(
                "credentials",
                "authenticated repository identity did not match",
            )
        return binding


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGINATION_BYTES = 8 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
DEFAULT_MAX_PAGES = 20

_API_HOST = "api.github.com"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_STATUS_LINE = re.compile(rb"HTTP/\S+\s+(\d{3})(?:\s+.*)?")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class GhResultLike(Protocol):
    returncode: int
    stdout: bytes
    stderr: str


class GhRunnerLike(Protocol):
    @property
    def credential_identity(self) -> GitHubCredentialIdentity: ...

    async def run(
        self,
        args: Sequence[str],
        *,
        repository: GitHubRepositoryBinding | None = None,
        hostname: str | None = None,
        stdin: bytes | str | None = None,
        timeout: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        check: bool = True,
    ) -> GhResultLike: ...


class GitHubExecutionAccess(Protocol):
    """Credential-aware execution seam consumed by :class:`GitHubClient`."""

    @property
    def credential_identity(self) -> GitHubCredentialIdentity: ...

    async def installation_token(
        self,
        repository: GitHubRepositoryBinding,
        *,
        force_refresh: bool = False,
    ) -> str | None: ...

    async def run_read(
        self,
        args: Sequence[str],
        *,
        repository: GitHubRepositoryBinding,
        stdin: bytes | str | None = None,
        timeout: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        check_result: Callable[[GhResultLike], None] | None = None,
    ) -> GhResultLike: ...

    async def run_write(
        self,
        args: Sequence[str],
        *,
        repository: GitHubRepositoryBinding,
        stdin: bytes | str | None = None,
        timeout: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        check_result: Callable[[GhResultLike], None] | None = None,
    ) -> GhResultLike: ...


class GitHubRunnerAccess:
    """Compatibility access wrapper for staged client constructors.

    New composition uses :class:`GitHubAccess`.  The old App/CLI constructors
    retain their public surface through this adapter until the final migration
    package removes them.
    """

    def __init__(
        self,
        runner: GhRunnerLike,
        *,
        refresh_read: Callable[[], Any] | None = None,
    ) -> None:
        self.runner = runner
        self._refresh_read = refresh_read

    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        return self.runner.credential_identity

    async def installation_token(
        self,
        repository: GitHubRepositoryBinding,
        *,
        force_refresh: bool = False,
    ) -> str | None:
        """Bridge legacy runner-owned credentials until their removal."""
        credentials = getattr(self.runner, "credentials", None)
        provider = getattr(credentials, "installation_token", None)
        if callable(provider):
            value = provider(repository, force_refresh=force_refresh)
            if hasattr(value, "__await__"):
                value = await value
            return value
        if self.credential_identity.mode is GitHubCredentialMode.EXISTING_LOGIN:
            return None
        raise GitHubAccessError("credentials", "GitHub App credential provider is unavailable")

    async def run_read(self, args: Sequence[str], **kwargs: Any) -> GhResultLike:
        try:
            return await self._run(args, **kwargs)
        except GitHubAccessError as exc:
            if exc.category != "credentials" or self._refresh_read is None:
                raise
        refreshed = self._refresh_read()
        if hasattr(refreshed, "__await__"):
            refreshed = await refreshed
        if not refreshed:
            raise GitHubAccessError("credentials", "GitHub credential refresh failed")
        return await self._run(args, **kwargs)

    async def run_write(self, args: Sequence[str], **kwargs: Any) -> GhResultLike:
        return await self._run(args, **kwargs)

    async def _run(
        self,
        args: Sequence[str],
        *,
        repository: GitHubRepositoryBinding,
        stdin: bytes | str | None = None,
        timeout: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        check_result: Callable[[GhResultLike], None] | None = None,
    ) -> GhResultLike:
        result = await self.runner.run(
            args,
            repository=repository,
            stdin=stdin,
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            check=check_result is None,
        )
        if check_result is not None:
            _validate_cli_result(self.runner, repository, result, check_result)
        return result


def _validate_cli_result(
    runner: GhRunnerLike,
    repository: GitHubRepositoryBinding,
    result: GhResultLike,
    check_result: Callable[[GhResultLike], None],
) -> None:
    """Report actual API failures after decoding status and expected outcomes."""
    try:
        check_result(result)
    except GitHubAccessError as exc:
        # Injected runners may implement only the execution seam. Production
        # uses GhRunner's shared warning cache for both checked and API calls.
        warn_failure = getattr(runner, "warn_failure", None)
        if callable(warn_failure):
            warn_failure(exc, repository=repository)
        raise


@dataclass(frozen=True, slots=True)
class GitHubApiResponse:
    """One bounded HTTP response decoded from ``gh api --include`` output."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class GitHubMergeReconciled(GitHubAccessError):
    """The merge landed although ``gh`` did not finish cleanly."""

    def __init__(self, sha: str, outcome: str) -> None:
        super().__init__("transient", "GitHub merged the PR but branch cleanup was not confirmed")
        self.sha = sha
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class PullRequestCreation:
    url: str
    created: bool


class GitHubClient:
    """Validated repository operations shared by every credential source."""

    def __init__(
        self,
        repository: GitHubRepositoryBinding,
        *,
        access: GitHubExecutionAccess | None = None,
        runner: GhRunnerLike | None = None,
        clock=time.time,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_pagination_bytes: int = MAX_PAGINATION_BYTES,
        max_header_bytes: int = MAX_HEADER_BYTES,
    ) -> None:
        if not isinstance(repository, GitHubRepositoryBinding):
            raise ValueError("repository must be a GitHubRepositoryBinding")
        if (access is None) == (runner is None):
            raise ValueError("exactly one GitHub access service or runner is required")
        for label, value in (
            ("max_response_bytes", max_response_bytes),
            ("max_pagination_bytes", max_pagination_bytes),
            ("max_header_bytes", max_header_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if max_pagination_bytes < max_response_bytes:
            raise ValueError("max_pagination_bytes must be at least max_response_bytes")
        self.repository = repository
        if access is None:
            assert runner is not None
            access = GitHubRunnerAccess(
                runner,
                refresh_read=self._refresh_rejected_credential,
            )
        self.access = access
        self.clock = clock
        self.max_response_bytes = max_response_bytes
        self.max_pagination_bytes = max_pagination_bytes
        self.max_header_bytes = max_header_bytes

    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        identity = self.access.credential_identity
        if not isinstance(identity, GitHubCredentialIdentity):
            raise ValueError("GitHub credential identity is unavailable")
        return identity

    async def installation_token(self, *, force_refresh: bool = False) -> str | None:
        """Supply isolated Git with this client's startup-selected credential."""
        return await self.access.installation_token(
            self.repository,
            force_refresh=force_refresh,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
        max_response_bytes: int | None = None,
        timeout: float | None = None,
    ) -> GitHubApiResponse:
        """Return one framed response after validating status and containment."""
        endpoint = self._validated_endpoint(path)
        return await self._request_endpoint(
            method,
            endpoint,
            json_body=json_body,
            expected_statuses=expected_statuses,
            max_response_bytes=max_response_bytes,
            timeout=timeout,
        )

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        response = await self.request(
            method,
            path,
            json_body=json_body,
            expected_statuses=expected_statuses,
        )
        if not response.body.strip():
            return {}
        value = _decode_json(response.body)
        if not isinstance(value, dict):
            raise GitHubAccessError("conflict_or_invalid", "GitHub response was not an object")
        return value

    async def authenticated_user(self) -> dict[str, Any]:
        """Identify the human existing gh login without widening repository endpoints."""
        from src.git.github_contracts import GitHubCredentialMode

        if self.credential_identity.mode is not GitHubCredentialMode.EXISTING_LOGIN:
            raise ValueError("human identity requires existing-login credentials")
        response = await self._request_endpoint("GET", "user")
        value = _decode_json(response.body)
        if not isinstance(value, dict):
            raise GitHubAccessError("conflict_or_invalid", "GitHub user response was not an object")
        return value

    async def request_text(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: set[int] | None = None,
        max_response_bytes: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Return a bounded text response, including Actions log payloads."""
        response = await self.request(
            method,
            path,
            expected_statuses=expected_statuses,
            max_response_bytes=max_response_bytes,
            timeout=timeout,
        )
        return response.body.decode("utf-8", "replace")

    async def paged_items(
        self,
        path: str,
        *,
        key: str,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[dict[str, Any]]:
        pages = await self._paged_json(path, max_pages=max_pages)
        items: list[dict[str, Any]] = []
        for payload in pages:
            page = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise GitHubAccessError("conflict_or_invalid", "GitHub page was malformed")
            items.extend(page)
        return items

    async def paged_list(
        self,
        path: str,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[dict[str, Any]]:
        pages = await self._paged_json(path, max_pages=max_pages)
        items: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise GitHubAccessError("conflict_or_invalid", "GitHub page was malformed")
            items.extend(page)
        return items

    async def list_open_issues_without_label(self, label: str) -> list[dict[str, Any]]:
        """Oldest first, excluding pull requests and issues with *label*.

        GitHub's issues endpoint includes pull requests. Its labels query is
        inclusive, so exclusion happens after bounded repository pagination.
        """
        path = (
            f"repos/{self.repository.full_name}/issues"
            "?state=open&sort=created&direction=asc&per_page=100"
        )
        rows = await self.paged_list(path, max_pages=50)
        issues = [
            row for row in rows
            if row.get("state") == "open"
            and "pull_request" not in row
            and not any(
                isinstance(item, dict) and item.get("name") == label
                for item in (row.get("labels") or [])
            )
        ]
        return sorted(issues, key=lambda row: (str(row.get("created_at", "")), row.get("number", 0)))

    async def issue(self, number: int) -> dict[str, Any]:
        """Read one issue in the bound repository, refusing a PR payload."""
        number = _validated_pull_request_number(number)
        row = await self.request_json("GET", f"repos/{self.repository.full_name}/issues/{number}")
        if row.get("number") != number or "pull_request" in row:
            raise GitHubAccessError("conflict_or_invalid", "GitHub issue identity did not match")
        return row

    async def add_issue_label(self, number: int, label: str) -> None:
        """Apply one label; reconcile an uncertain write by reading the issue."""
        number = _validated_pull_request_number(number)
        path = f"repos/{self.repository.full_name}/issues/{number}/labels"
        try:
            await self.request_json("POST", path, json_body={"labels": [label]})
        except GitHubAccessError:
            row = await self.issue(number)
            if not any(
                isinstance(item, dict) and item.get("name") == label
                for item in (row.get("labels") or [])
            ):
                raise

    async def close_issue_with_reason(self, number: int, reason: str, marker: str) -> None:
        """Post a marked closing reason once, then close the bound issue."""
        number = _validated_pull_request_number(number)
        row = await self.issue(number)
        if row.get("state") == "closed":
            return
        path = f"repos/{self.repository.full_name}/issues/{number}"
        comments = await self.paged_list(f"{path}/comments?per_page=100", max_pages=50)
        body = f"{reason.strip()}\n\n<!-- {marker} -->"
        if not any(marker in str(comment.get("body") or "") for comment in comments):
            try:
                await self.request_json("POST", f"{path}/comments", json_body={"body": body},
                                        expected_statuses={201})
            except GitHubAccessError:
                comments = await self.paged_list(f"{path}/comments?per_page=100", max_pages=50)
                if not any(marker in str(comment.get("body") or "") for comment in comments):
                    raise
        try:
            await self.request_json("PATCH", path, json_body={"state": "closed"})
        except GitHubAccessError:
            if (await self.issue(number)).get("state") != "closed":
                raise

    async def _paged_json(self, path: str, *, max_pages: int) -> list[Any]:
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages <= 0:
            raise ValueError("max_pages must be a positive integer")
        endpoint = self._validated_endpoint(path)
        pages: list[Any] = []
        aggregate_bytes = 0
        for page_number in range(1, max_pages + 1):
            remaining = self.max_pagination_bytes - aggregate_bytes
            if remaining <= 0:
                raise GitHubAccessError(
                    "transient", "GitHub pagination exceeded aggregate size limit"
                )
            response = await self._request_endpoint(
                "GET",
                endpoint,
                expected_statuses={200},
                max_response_bytes=min(self.max_response_bytes, remaining),
            )
            aggregate_bytes += len(response.body)
            pages.append(_decode_json(response.body))
            next_link = _next_link(response.headers.get("link"))
            if next_link is None:
                return pages
            endpoint = self._validated_next_endpoint(next_link)
            if page_number == max_pages:
                raise GitHubAccessError("transient", "GitHub pagination exceeded page limit")
            if aggregate_bytes >= self.max_pagination_bytes:
                raise GitHubAccessError(
                    "transient", "GitHub pagination exceeded aggregate size limit"
                )
        raise AssertionError("bounded pagination loop did not terminate")

    async def _request_endpoint(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
        max_response_bytes: int | None = None,
        timeout: float | None = None,
    ) -> GitHubApiResponse:
        normalized_method = _validated_method(method)
        allowed = _validated_statuses(expected_statuses)
        body_limit = self._response_limit(max_response_bytes)
        if json_body is not None and not isinstance(json_body, dict):
            raise ValueError("GitHub API JSON body must be an object")
        args = [
            "api",
            "--include",
            "--method",
            normalized_method,
            "--header",
            f"Accept: {_ACCEPT}",
            "--header",
            f"X-GitHub-Api-Version: {_API_VERSION}",
            endpoint,
        ]
        stdin = None
        if json_body is not None:
            args.extend(["--input", "-"])
            stdin = json.dumps(json_body, separators=(",", ":")).encode()

        response: GitHubApiResponse | None = None

        def validate_result(result: GhResultLike) -> None:
            nonlocal response
            try:
                response = _decode_included_response(
                    result.stdout,
                    max_header_bytes=self.max_header_bytes,
                    max_body_bytes=body_limit,
                )
            except GitHubAccessError:
                if result.returncode:
                    raise _command_error(result.returncode, result.stderr) from None
                raise
            if response.status not in allowed:
                raise _http_error(response.status, response.headers, self.clock(), response.body)
            if result.returncode and response.status in allowed:
                # gh exits nonzero for HTTP failures.  An explicitly expected
                # status remains usable, but unrelated command failures do not.
                if response.status < 400:
                    raise _command_error(result.returncode, result.stderr)

        execute = self.access.run_read if normalized_method == "GET" else self.access.run_write
        await execute(
            args,
            repository=self.repository,
            stdin=stdin,
            max_stdout_bytes=self.max_header_bytes + body_limit,
            timeout=timeout,
            check_result=validate_result,
        )
        if response is None:  # pragma: no cover - execution contract violation
            raise AssertionError("GitHub access returned without validating a response")
        return response

    async def _refresh_rejected_credential(self) -> bool:
        """Compatibility hook for App-backed adapters; ordinary clients do not retry."""
        return False

    def _response_limit(self, override: int | None) -> int:
        if override is None:
            return self.max_response_bytes
        if isinstance(override, bool) or not isinstance(override, int) or override <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        return min(override, self.max_response_bytes)

    def _validated_next_endpoint(self, link: str) -> str:
        if not isinstance(link, str) or not link:
            raise GitHubAccessError("conflict_or_invalid", "GitHub pagination link was malformed")
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme != "https"
                or parsed.netloc != _API_HOST
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
            ):
                raise GitHubAccessError("conflict_or_invalid", "GitHub pagination escaped API host")
            relative = parsed.path
            if parsed.query:
                relative += "?" + parsed.query
            if parsed.fragment:
                raise GitHubAccessError(
                    "conflict_or_invalid", "GitHub pagination link was malformed"
                )
        else:
            relative = link
        try:
            return self._validated_endpoint(relative)
        except ValueError as exc:
            raise GitHubAccessError(
                "conflict_or_invalid", "GitHub pagination escaped repository scope"
            ) from exc

    def _validated_endpoint(self, path: str) -> str:
        if (
            not isinstance(path, str)
            or not path
            or path.startswith(("-", "//"))
            or any(character in path for character in ("\n", "\r", "\x00"))
        ):
            raise ValueError("GitHub API path must be a repository-bound endpoint")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("GitHub API path must be a repository-bound endpoint")
        endpoint_path = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
        if not endpoint_path or _INVALID_PERCENT_ESCAPE.search(endpoint_path):
            raise ValueError("GitHub API path must be a repository-bound endpoint")
        allowed_roots = (
            f"repos/{self.repository.full_name}",
            f"repositories/{self.repository.repository_id}",
        )
        root = next(
            (
                candidate
                for candidate in allowed_roots
                if endpoint_path == candidate or endpoint_path.startswith(candidate + "/")
            ),
            None,
        )
        if root is None:
            raise ValueError("GitHub API path must be a repository-bound endpoint")
        decoded = endpoint_path
        for _ in range(4):
            next_decoded = unquote(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
        if (
            "\\" in decoded
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
            or any(segment in {".", ".."} for segment in decoded.split("/"))
            or not (decoded == root or decoded.startswith(root + "/"))
            or _PERCENT_ESCAPE.search(decoded)
        ):
            raise ValueError("GitHub API path must be a repository-bound endpoint")
        endpoint = endpoint_path
        if parsed.query:
            endpoint += "?" + parsed.query
        return endpoint

    async def exact_head_ref(self, branch: str) -> str | None:
        if not isinstance(branch, str) or not branch or branch.startswith("refs/"):
            raise ValueError("branch must be a short head name")
        path = (
            f"/repositories/{self.repository.repository_id}/git/ref/heads/{quote(branch, safe='')}"
        )
        try:
            payload = await self.request_json("GET", path)
        except GitHubAccessError as exc:
            if exc.category == "not_found_or_hidden":
                return None
            raise
        obj = payload.get("object")
        oid = obj.get("sha") if isinstance(obj, dict) else None
        if payload.get("ref") != f"refs/heads/{branch}" or not isinstance(oid, str):
            raise GitHubAccessError("conflict_or_invalid", "GitHub ref identity was malformed")
        if re.fullmatch(r"[0-9a-f]{40}", oid) is None:
            raise GitHubAccessError("conflict_or_invalid", "GitHub ref OID was malformed")
        return oid

    async def exact_pull_request(self, *, number: int) -> dict[str, Any] | None:
        number = _validated_pull_request_number(number)
        try:
            payload = await self.request_json(
                "GET", f"/repositories/{self.repository.repository_id}/pulls/{number}"
            )
        except GitHubAccessError as exc:
            if exc.category == "not_found_or_hidden":
                return None
            raise
        head = payload.get("head")
        head_repository = head.get("repo") if isinstance(head, dict) else None
        base = payload.get("base")
        base_repository = base.get("repo") if isinstance(base, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        expected_url = f"https://github.com/{self.repository.full_name}/pull/{number}"
        if (
            not isinstance(head_repository, dict)
            or _strict_positive_int(head_repository.get("id")) is None
            or not isinstance(head_repository.get("full_name"), str)
            or not isinstance(base_repository, dict)
            or _strict_positive_int(base_repository.get("id")) != self.repository.repository_id
            or base_repository.get("full_name") != self.repository.full_name
            or not isinstance(sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", sha) is None
            or payload.get("number") != number
            or payload.get("html_url") != expected_url
            or payload.get("state") not in {"open", "closed"}
        ):
            raise GitHubAccessError("conflict_or_invalid", "GitHub PR identity was malformed")
        return {
            "repository_numeric_id": head_repository["id"],
            "repository_full_name": head_repository["full_name"],
            "head_sha": sha,
            "state": payload["state"],
        }

    async def has_comment_marker(self, *, number: int, marker: str) -> bool:
        number = _validated_pull_request_number(number)
        if not marker or "\n" in marker:
            raise ValueError("comment marker must be one non-empty line")
        path = (
            f"/repositories/{self.repository.repository_id}/issues/{number}/comments?per_page=100"
        )
        for comment in await self.paged_list(path):
            if marker in str(comment.get("body") or ""):
                return True
        return False

    async def comment_pull_request(self, *, number: int, marker: str, body: str) -> None:
        number = _validated_pull_request_number(number)
        if marker not in body:
            raise ValueError("delivery comment must contain its stable marker")
        await self.request_json(
            "POST",
            f"/repositories/{self.repository.repository_id}/issues/{number}/comments",
            json_body={"body": body},
            expected_statuses={201},
        )

    async def close_pull_request(self, *, number: int) -> None:
        number = _validated_pull_request_number(number)
        payload = await self.request_json(
            "PATCH",
            f"/repositories/{self.repository.repository_id}/pulls/{number}",
            json_body={"state": "closed"},
        )
        if payload.get("number") != number or payload.get("state") != "closed":
            raise GitHubAccessError("conflict_or_invalid", "GitHub PR close was not confirmed")

    # Ordinary PR and CI operations use the same repository authority as the
    # integration operations above.  The caller supplies the repository; a PR
    # URL is only an identifier to check against it, never an auth selector.
    async def pull_request(self, pr_url: str) -> dict[str, Any]:
        number = GitHubAccess.validate_pr_url(self.repository, pr_url)
        payload = await self.request_json(
            "GET", f"/repositories/{self.repository.repository_id}/pulls/{number}"
        )
        base = payload.get("base")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        if (
            payload.get("number") != number
            or not isinstance(base_repo, dict)
            or _strict_positive_int(base_repo.get("id")) != self.repository.repository_id
            or base_repo.get("full_name") != self.repository.full_name
            or payload.get("html_url")
            != f"https://github.com/{self.repository.full_name}/pull/{number}"
        ):
            raise GitHubAccessError("conflict_or_invalid", "GitHub PR repository did not match")
        return payload

    async def create_pull_request(
        self, *, title: str, body: str, base: str, head: str
    ) -> str:
        return (await self.create_pull_request_result(
            title=title, body=body, base=base, head=head
        )).url

    async def create_pull_request_result(
        self, *, title: str, body: str, base: str, head: str
    ) -> PullRequestCreation:
        _validated_short_head(base, label="base branch")
        _validated_short_head(head, label="head branch")
        if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
            raise ValueError("PR title and body must be supplied explicitly")
        head_oid = await self.exact_head_ref(head)
        if head_oid is None:
            if await self.exact_head_ref(base) is None:
                raise GitHubAccessError(
                    "not_found_or_hidden", "PR repository refs were not readable"
                )
            raise GitHubAccessError(
                "conflict_or_invalid", "PR head branch is not published on the authorized repository"
            )
        existing = await self._ordinary_pr_for_head(head, base, head_oid)
        if existing is not None:
            if existing["state"] == "closed" and not existing.get("merged_at"):
                return await self._reopen_ordinary_pr(existing, head, base, head_oid)
            return PullRequestCreation(existing["html_url"], created=False)
        try:
            result = await self.access.run_write(
                ["pr", "create", "--title", title, "--body-file", "-", "--base", base,
                 "--head", head],
                repository=self.repository,
                stdin=body,
            )
            url = result.stdout.decode("utf-8", "replace").strip()
            GitHubAccess.validate_pr_url(self.repository, url)
            return PullRequestCreation(url, created=True)
        except (GitHubAccessError, ValueError) as exc:
            if isinstance(exc, GitHubWriteNotStarted) or (
                isinstance(exc, GitHubAccessError) and exc.category == "cli_missing"
            ):
                raise
            # gh pr create makes multiple API calls.  A nonzero exit or bad
            # final output says nothing about whether the PR was published.
            # Read with a fresh credential when appropriate; never replay the
            # write inside this invocation.
            existing = await self._ordinary_pr_for_head(head, base, head_oid)
            if existing is not None and (
                existing["state"] == "open" or existing.get("merged_at")
            ):
                return PullRequestCreation(existing["html_url"], created=False)
            raise

    async def _reopen_ordinary_pr(
        self, pull: dict[str, Any], head: str, base: str, head_oid: str
    ) -> PullRequestCreation:
        write_error = None
        try:
            await self.request_json(
                "PATCH",
                f"/repositories/{self.repository.repository_id}/pulls/{pull['number']}",
                json_body={"state": "open"},
            )
        except GitHubAccessError as exc:
            if isinstance(exc, GitHubWriteNotStarted) or exc.category == "cli_missing":
                raise
            write_error = exc
        # A failed write may have reopened the PR. Validate the same request's
        # identity and state with a read, never another write in this invocation.
        current = await self._ordinary_pr_for_head(head, base, head_oid)
        if current is not None and current["html_url"] == pull["html_url"] and (
            current["state"] == "open" or current.get("merged_at")
        ):
            return PullRequestCreation(current["html_url"], created=False)
        if write_error is not None:
            raise write_error
        raise GitHubAccessError("conflict_or_invalid", "PR reopen was not confirmed")

    async def _ordinary_pr_for_head(
        self, head: str, base: str, head_oid: str
    ) -> dict[str, Any] | None:
        owner = self.repository.full_name.split("/", 1)[0]
        pulls = await self.paged_list(
            f"/repositories/{self.repository.repository_id}/pulls?state=all&per_page=100"
            + "&head=" + quote(f"{owner}:{head}", safe="")
        )
        matches = []
        for pull in pulls:
            pr_head = pull.get("head")
            if not isinstance(pr_head, dict) or pr_head.get("ref") != head:
                continue
            matches.append(pull)
        if not matches:
            return None
        if len(matches) != 1:
            raise GitHubAccessError("conflict_or_invalid", "PR head has multiple existing requests")
        pull = matches[0]
        pr_head = pull["head"]
        pr_base = pull.get("base")
        head_repo = pr_head.get("repo")
        base_repo = pr_base.get("repo") if isinstance(pr_base, dict) else None
        number = _strict_positive_int(pull.get("number"))
        url = pull.get("html_url")
        if (
            number is None
            or url != f"https://github.com/{self.repository.full_name}/pull/{number}"
            or not isinstance(head_repo, dict)
            or head_repo.get("id") != self.repository.repository_id
            or head_repo.get("full_name") != self.repository.full_name
            or not isinstance(base_repo, dict)
            or base_repo.get("id") != self.repository.repository_id
            or base_repo.get("full_name") != self.repository.full_name
            or pr_base.get("ref") != base
            or pr_head.get("sha") != head_oid
            or pull.get("state") not in {"open", "closed"}
        ):
            raise GitHubAccessError(
                "conflict_or_invalid", "existing PR head did not match the requested delivery"
            )
        return pull

    async def list_pull_requests(
        self, *, state: str = "open", base: str | None = None,
        head: str | None = None, limit: int = 30, include_head_oid: bool = False,
    ) -> list[dict[str, Any]]:
        if state not in {"open", "closed", "merged", "all"}:
            raise ValueError("invalid PR state")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("PR list limit must be between 1 and 100")
        fields = "number,url,title,baseRefName,headRefName,state"
        if include_head_oid:
            fields += ",headRefOid"
        args = ["pr", "list", "--state", state, "--limit", str(limit), "--json", fields]
        if base is not None:
            args.extend(["--base", _validated_short_head(base, label="base branch")])
        if head is not None:
            args.extend(["--head", _validated_short_head(head, label="head branch")])
        result = await self.access.run_read(args, repository=self.repository)
        payload = _decode_json(result.stdout)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise GitHubAccessError("conflict_or_invalid", "GitHub PR list was malformed")
        for item in payload:
            GitHubAccess.validate_pr_url(self.repository, item.get("url"))
        return payload

    async def merge_pull_request(
        self, pr_url: str, *, method: str, expected_head_oid: str,
        expected_base_ref: str | None = None,
    ) -> str | None:
        number = GitHubAccess.validate_pr_url(self.repository, pr_url)
        if method not in {"squash", "merge", "rebase"}:
            raise ValueError("invalid PR merge method")
        if re.fullmatch(r"[0-9a-f]{40}", expected_head_oid) is None:
            raise ValueError("invalid expected PR head OID")
        if expected_base_ref is not None:
            _validated_short_head(expected_base_ref, label="base branch")
        try:
            result = await self.access.run_write(
                ["pr", "merge", str(number), f"--{method}", "--match-head-commit",
                 expected_head_oid, "--delete-branch"],
                repository=self.repository,
            )
        except GitHubAccessError as write_error:
            if isinstance(write_error, GitHubWriteNotStarted) or write_error.category == "cli_missing":
                raise
            # A successful merge followed by failed remote branch deletion is
            # a success with cleanup outstanding, not permission to merge again.
            try:
                pull = await self.pull_request(pr_url)
            except GitHubAccessError as exc:
                raise GitHubAccessError(
                    "transient", "Merge outcome is uncertain; PR reconciliation failed"
                ) from exc
            head = pull.get("head")
            base = pull.get("base")
            sha = pull.get("merge_commit_sha")
            if pull.get("merged_at"):
                head_repo = head.get("repo") if isinstance(head, dict) else None
                if (
                    pull.get("state") != "closed"
                    or not isinstance(head, dict)
                    or head.get("sha") != expected_head_oid
                    or (
                        head_repo is not None
                        and (
                            not isinstance(head_repo, dict)
                            or head_repo.get("id") != self.repository.repository_id
                            or head_repo.get("full_name") != self.repository.full_name
                        )
                    )
                    or not isinstance(base, dict)
                    or (expected_base_ref is not None and base.get("ref") != expected_base_ref)
                    or not isinstance(sha, str)
                    or re.fullmatch(r"[0-9a-f]{40}", sha) is None
                ):
                    raise GitHubAccessError(
                        "conflict_or_invalid", "merged PR identity did not match validated delivery"
                    ) from None
                branch = head.get("ref")
                outcome = "merged_cleanup_unknown"
                if isinstance(branch, str):
                    try:
                        remaining = await self.exact_head_ref(branch)
                    except (GitHubAccessError, ValueError):
                        pass
                    else:
                        if remaining:
                            outcome = "merged_cleanup_failed"
                        else:
                            # A 404 can hide a repository or a ref permission.
                            # Prove this credential can read refs before
                            # interpreting the missing head as deleted.
                            base_ref = base.get("ref") if isinstance(base, dict) else None
                            if isinstance(base_ref, str):
                                try:
                                    base_oid = await self.exact_head_ref(base_ref)
                                except (GitHubAccessError, ValueError):
                                    base_oid = None
                                if base_oid:
                                    outcome = "merged_reconciled"
                raise GitHubMergeReconciled(sha, outcome) from None
            if pull.get("state") != "open":
                raise GitHubAccessError(
                    "conflict_or_invalid", "PR closed without a confirmed merge"
                ) from None
            raise
        for token in result.stdout.decode("utf-8", "replace").split():
            token = token.strip("().,;:")
            if re.fullmatch(r"[0-9a-f]{40}", token):
                return token
        return None

    async def check_rollup(self, pr_url: str) -> list[dict[str, Any]]:
        number = GitHubAccess.validate_pr_url(self.repository, pr_url)
        result = await self.access.run_read(
            ["pr", "view", str(number), "--json", "statusCheckRollup"],
            repository=self.repository,
        )
        payload = _decode_json(result.stdout)
        rollup = payload.get("statusCheckRollup") if isinstance(payload, dict) else None
        if rollup is None:
            return []
        if not isinstance(rollup, list) or not all(isinstance(item, dict) for item in rollup):
            raise GitHubAccessError("conflict_or_invalid", "GitHub PR check rollup was malformed")
        return rollup

    async def commit_head(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref or any(c in ref for c in "?#\x00\r\n"):
            raise ValueError("invalid commit reference")
        payload = await self.request_json(
            "GET", f"/repositories/{self.repository.repository_id}/commits/{quote(ref, safe='')}"
        )
        sha = payload.get("sha")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise GitHubAccessError("conflict_or_invalid", "GitHub commit OID was malformed")
        return sha

    async def commit_check_runs(self, sha: str) -> list[dict[str, Any]]:
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise ValueError("invalid commit OID")
        return await self.paged_items(
            f"/repositories/{self.repository.repository_id}/commits/{sha}/check-runs?per_page=100",
            key="check_runs",
        )

    async def rerequest_check_suite(self, check_suite_id: int) -> dict[str, Any]:
        """Ask GitHub to run one check suite again, without pushing code.

        GitHub's documented "Rerequest a check suite" endpoint:
        ``POST /repos/{owner}/{repo}/check-suites/{check_suite_id}/rerequest``
        (https://docs.github.com/en/rest/checks/suites#rerequest-a-check-suite).
        It resets that suite to ``queued`` and clears its conclusion, which is
        the only way to retry CI for a head that has not moved.  It is addressed
        by suite id, so it cannot reach another repository, commit or head, and
        it re-runs checks rather than dispatching a workflow — no ``actions:
        write`` is involved.

        GitHub documents writes to checks as GitHub-App-only: OAuth app tokens
        and personal access tokens can read check suites and check runs but
        cannot write them (https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-checks).
        An existing-login credential therefore refuses here, before any request
        is issued, instead of asking for one that cannot succeed; widening a
        credential is a human decision, and the caller falls back to waiting for
        its named infrastructure blocker.
        """
        if (
            isinstance(check_suite_id, bool)
            or not isinstance(check_suite_id, int)
            or check_suite_id <= 0
        ):
            raise ValueError("invalid check suite ID")
        if self.credential_identity.mode is not GitHubCredentialMode.APP:
            raise GitHubAccessError(
                "github_operation_unsupported",
                "Re-requesting a check suite requires a GitHub App credential; GitHub "
                "documents writes to checks as GitHub-App-only",
            )
        return await self.request_json(
            "POST",
            f"repos/{self.repository.full_name}/check-suites/{check_suite_id}/rerequest",
            expected_statuses={201},
        )

    async def job_log(self, job_id: int) -> str:
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            raise ValueError("invalid Actions job ID")
        return await self.request_text(
            "GET", f"/repositories/{self.repository.repository_id}/actions/jobs/{job_id}/logs",
            max_response_bytes=16 * 1024 * 1024,
            timeout=120,
        )

    async def compare(self, base: str, head: str) -> dict[str, Any]:
        _validated_short_head(base, label="base branch")
        if re.fullmatch(r"[0-9a-f]{7,40}", head) is None:
            raise ValueError("invalid comparison head OID")
        return await self.request_json(
            "GET", f"/repositories/{self.repository.repository_id}/compare/{quote(base, safe='')}...{head}"
        )

    async def lookup_audit_pr(self, *, idempotency_key: str, branch: str | None = None):
        marker = self._audit_marker(idempotency_key)
        path = f"/repositories/{self.repository.repository_id}/pulls?state=all&per_page=100"
        if branch is not None:
            _validated_short_head(branch, label="audit PR branch")
            owner = self.repository.full_name.split("/", 1)[0]
            path += "&head=" + quote(f"{owner}:{branch}", safe="")
        pulls = await self.paged_list(path)
        matches = [pull for pull in pulls if marker in str(pull.get("body") or "")]
        if not matches:
            return None
        if len(matches) != 1:
            raise GitHubAccessError("conflict_or_invalid", "audit PR marker was not unique")
        result = self._audit_pull_request(matches[0], idempotency_key)
        if branch is not None and result.head_branch != branch:
            raise GitHubAccessError(
                "conflict_or_invalid", "GitHub audit PR branch identity did not match"
            )
        return result

    async def create_audit_pr(
        self,
        *,
        repository_id: str,
        branch: str,
        head_sha: str,
        base_branch: str,
        batch_id: str,
        idempotency_key: str,
        repository_numeric_id: int,
        repository_full_name: str,
    ):
        if not repository_id or not batch_id:
            raise ValueError("candidate audit identity must be non-empty")
        if (
            repository_numeric_id != self.repository.repository_id
            or repository_full_name != self.repository.full_name
        ):
            raise GitHubAccessError("credentials", "candidate repository binding did not match")
        try:
            _validated_short_head(branch, label="candidate audit branch")
            _validated_short_head(base_branch, label="candidate audit base")
        except ValueError as exc:
            raise ValueError("candidate audit ref identity was malformed") from exc
        if re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
            raise ValueError("candidate audit ref identity was malformed")
        marker = self._audit_marker(idempotency_key)
        pulls = await self.paged_list(
            f"/repositories/{self.repository.repository_id}/pulls?state=all&per_page=100"
            + "&head="
            + quote(f"{self.repository.full_name.split('/', 1)[0]}:{branch}", safe="")
        )
        matches = [
            pull
            for pull in pulls
            if isinstance(pull.get("head"), dict) and pull["head"].get("ref") == branch
        ]
        open_matches = [pull for pull in matches if pull.get("state") == "open"]
        if open_matches:
            matches = open_matches
        else:
            # A closed audit PR for an earlier revision of this same batch is
            # frozen at the old head (GitHub never moves a closed PR's head and
            # cannot reopen it after a force-push).  After a repair rebuilds the
            # candidate it is superseded, not a conflict: open a fresh PR.
            matches = [
                pull
                for pull in matches
                if not self._superseded_closed_audit_pr(pull, batch_id, head_sha, base_branch)
            ]
        if matches:
            if len(matches) != 1:
                raise GitHubAccessError("conflict_or_invalid", "audit PR branch was not unique")
            existing = matches[0]
            body = str(existing.get("body") or "")
            old_markers = re.findall(r"<!-- aq-integration-audit:([0-9a-f]{64}) -->", body)
            if not old_markers or f"Root integration batch `{batch_id}`." not in body:
                raise GitHubAccessError(
                    "conflict_or_invalid", "existing PR belongs to another batch"
                )
            result = self._audit_pull_request(existing, old_markers[0])
            if result.head_sha != head_sha or result.base_branch != base_branch:
                raise GitHubAccessError("conflict_or_invalid", "existing audit PR target moved")
            if result.state == "closed":
                if await self.exact_head_ref(base_branch) != head_sha:
                    raise GitHubAccessError(
                        "conflict_or_invalid", "closed audit PR is not exact base"
                    )
            expected_state = result.state
            if marker not in body:
                existing = await self.request_json(
                    "PATCH",
                    f"/repositories/{self.repository.repository_id}/pulls/{result.number}",
                    json_body={"body": f"{body}\n{marker}"},
                )
            result = self._audit_pull_request(existing, idempotency_key)
            if (
                result.head_sha != head_sha
                or result.head_branch != branch
                or result.base_branch != base_branch
                or existing.get("state") != expected_state
            ):
                raise GitHubAccessError(
                    "conflict_or_invalid", "audit PR changed during revision update"
                )
            return result
        payload = await self.request_json(
            "POST",
            f"/repositories/{self.repository.repository_id}/pulls",
            json_body={
                "title": f"Integration train {batch_id}",
                "head": branch,
                "base": base_branch,
                "body": f"{marker}\nRoot integration batch `{batch_id}`.",
            },
            expected_statuses={201},
        )
        result = self._audit_pull_request(payload, idempotency_key)
        if (
            result.head_sha != head_sha
            or result.head_branch != branch
            or result.base_branch != base_branch
        ):
            raise GitHubAccessError(
                "conflict_or_invalid", "audit PR target did not match candidate"
            )
        return result

    @staticmethod
    def _superseded_closed_audit_pr(
        pull: dict[str, Any], batch_id: str, head_sha: str, base_branch: str
    ) -> bool:
        head = pull.get("head")
        base = pull.get("base")
        body = str(pull.get("body") or "")
        return (
            pull.get("state") == "closed"
            and not pull.get("merged_at")
            and isinstance(head, dict)
            and isinstance(base, dict)
            and head.get("sha") != head_sha
            and base.get("ref") == base_branch
            and f"Root integration batch `{batch_id}`." in body
            and re.search(r"<!-- aq-integration-audit:[0-9a-f]{64} -->", body) is not None
        )

    @staticmethod
    def _audit_marker(idempotency_key: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", idempotency_key) is None:
            raise ValueError("audit PR idempotency key must be a SHA-256 digest")
        return f"<!-- aq-integration-audit:{idempotency_key} -->"

    def _audit_pull_request(self, payload: dict[str, Any], idempotency_key: str):
        from src.integration.promotion_contracts import AuditPullRequest

        head = payload.get("head")
        base = payload.get("base")
        repository = head.get("repo") if isinstance(head, dict) else None
        base_repository = base.get("repo") if isinstance(base, dict) else None
        number = _strict_positive_int(payload.get("number"))
        expected_url = (
            f"https://github.com/{self.repository.full_name}/pull/{number}"
            if number is not None
            else None
        )
        if (
            number is None
            or payload.get("html_url") != expected_url
            or not isinstance(head, dict)
            or not isinstance(base, dict)
            or not isinstance(repository, dict)
            or _strict_positive_int(repository.get("id")) != self.repository.repository_id
            or repository.get("full_name") != self.repository.full_name
            or (
                base_repository is not None
                and (
                    not isinstance(base_repository, dict)
                    or _strict_positive_int(base_repository.get("id"))
                    != self.repository.repository_id
                    or base_repository.get("full_name") != self.repository.full_name
                )
            )
            or re.fullmatch(r"[0-9a-f]{40}", str(head.get("sha") or "")) is None
            or not isinstance(head.get("ref"), str)
            or not head["ref"]
            or not isinstance(base.get("ref"), str)
            or not base["ref"]
            or payload.get("state") not in {"open", "closed"}
            or self._audit_marker(idempotency_key) not in str(payload.get("body") or "")
        ):
            raise GitHubAccessError("conflict_or_invalid", "GitHub audit PR identity was malformed")
        return AuditPullRequest(
            url=expected_url,
            number=number,
            head_sha=head["sha"],
            head_branch=head["ref"],
            base_branch=base["ref"],
            repository_numeric_id=self.repository.repository_id,
            repository_full_name=self.repository.full_name,
            idempotency_key=idempotency_key,
            state=payload["state"],
        )


def _validated_method(method: str) -> str:
    if not isinstance(method, str):
        raise ValueError("unsupported GitHub API method")
    normalized = method.upper()
    if normalized not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
        raise ValueError("unsupported GitHub API method")
    return normalized


def _validated_statuses(statuses: set[int] | None) -> frozenset[int]:
    values = {200} if statuses is None else statuses
    if (
        not isinstance(values, set)
        or not values
        or any(
            isinstance(status, bool) or not isinstance(status, int) or status < 100 or status > 599
            for status in values
        )
    ):
        raise ValueError("expected_statuses must contain HTTP status integers")
    return frozenset(values)


def _decode_included_response(
    output: bytes,
    *,
    max_header_bytes: int,
    max_body_bytes: int,
) -> GitHubApiResponse:
    marker = b"\r\n\r\n"
    boundary = output.find(marker)
    if boundary < 0:
        marker = b"\n\n"
        boundary = output.find(marker)
    if boundary < 0 or boundary > max_header_bytes:
        raise GitHubAccessError("conflict_or_invalid", "GitHub CLI response framing was malformed")
    header_block = output[:boundary]
    body = output[boundary + len(marker) :]
    if len(body) > max_body_bytes:
        raise GitHubAccessError("transient", "GitHub response exceeded size limit")
    lines = header_block.splitlines()
    if not lines:
        raise GitHubAccessError("conflict_or_invalid", "GitHub CLI response framing was malformed")
    status_match = _STATUS_LINE.fullmatch(lines[0].strip())
    if status_match is None:
        raise GitHubAccessError("conflict_or_invalid", "GitHub CLI response framing was malformed")
    headers: dict[str, str] = {}
    for raw_line in lines[1:]:
        if b":" not in raw_line:
            raise GitHubAccessError(
                "conflict_or_invalid", "GitHub CLI response framing was malformed"
            )
        raw_name, raw_value = raw_line.split(b":", 1)
        try:
            name = raw_name.decode("ascii").strip().lower()
            value = raw_value.decode("latin-1").strip()
        except UnicodeDecodeError as exc:
            raise GitHubAccessError(
                "conflict_or_invalid", "GitHub CLI response framing was malformed"
            ) from exc
        if not name or any(character.isspace() for character in name):
            raise GitHubAccessError(
                "conflict_or_invalid", "GitHub CLI response framing was malformed"
            )
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return GitHubApiResponse(int(status_match.group(1)), headers, body)


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubAccessError(
            "conflict_or_invalid", "GitHub response was not valid JSON"
        ) from exc


def _profile_gist_id(url: str) -> str:
    if not isinstance(url, str):
        raise GitHubAccessError("conflict_or_invalid", "A GitHub gist URL is required")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise GitHubAccessError("conflict_or_invalid", "A valid GitHub gist URL is required") from exc
    path = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "gist.github.com"
        or parsed.query
        or len(path) not in {1, 2}
        or re.fullmatch(r"[0-9A-Fa-f]{5,64}", path[-1]) is None
    ):
        raise GitHubAccessError(
            "conflict_or_invalid", "A valid https://gist.github.com profile gist URL is required"
        )
    return path[-1]


def _next_link(value: str | None) -> str | None:
    if not value:
        return None
    links: list[str] = []
    position = 0
    while position < len(value):
        start = value.find("<", position)
        end = value.find(">", start + 1) if start >= 0 else -1
        if start < 0 or end < 0:
            raise GitHubAccessError("conflict_or_invalid", "GitHub pagination link was malformed")
        separator = value.find(",", end + 1)
        item_end = len(value) if separator < 0 else separator
        parameters = value[end + 1 : item_end].split(";")
        if any(parameter.strip().lower() == 'rel="next"' for parameter in parameters):
            links.append(value[start + 1 : end])
        position = item_end + 1
    if len(links) > 1:
        raise GitHubAccessError("conflict_or_invalid", "GitHub pagination link was ambiguous")
    return links[0] if links else None


def _http_error(
    status: int, headers: Mapping[str, str], now: float, body: bytes = b""
) -> GitHubAccessError:
    retry_after = headers.get("retry-after")
    remaining = headers.get("x-ratelimit-remaining")
    reset = headers.get("x-ratelimit-reset")
    secondary_limit = any(
        text in body.lower() for text in (b"secondary rate limit", b"rate limit exceeded")
    )
    if status == 429 or (
        status == 403 and (retry_after is not None or remaining == "0" or secondary_limit)
    ):
        retry_times: list[float] = []
        if retry_after:
            try:
                retry_times.append(now + max(0, int(retry_after)))
            except (ValueError, OverflowError):
                try:
                    retry_times.append(parsedate_to_datetime(retry_after).timestamp())
                except (TypeError, ValueError, OverflowError):
                    pass
        # A secondary limit may leave the primary quota untouched. Its reset
        # header does not govern that limit unless the primary quota is empty.
        if reset and remaining == "0":
            try:
                retry_times.append(float(int(reset)))
            except (ValueError, OverflowError):
                pass
        return GitHubAccessError(
            "rate_limited",
            f"GitHub request was rate limited (rate_limited, HTTP {status})",
            retry_at=max(now, *retry_times) if retry_times else now + 60.0,
            http_status=status,
        )
    category = {
        401: "credentials",
        403: "permission",
        404: "not_found_or_hidden",
        409: "conflict_or_invalid",
        422: "conflict_or_invalid",
    }.get(status, "transient" if status >= 500 else "conflict_or_invalid")
    return GitHubAccessError(
        category, f"GitHub request failed ({category}, HTTP {status})", http_status=status
    )


def _command_error(returncode: int, diagnostic: str) -> GitHubAccessError:
    status_match = re.search(r"\bHTTP\s+(\d{3})\b", diagnostic, flags=re.IGNORECASE)
    status = int(status_match.group(1)) if status_match else None
    if returncode == 4 or status == 401:
        category = "credentials"
    elif status == 404:
        category = "not_found_or_hidden"
    elif status in {409, 422}:
        category = "conflict_or_invalid"
    elif status == 429 or (status == 403 and "rate limit" in diagnostic.lower()):
        category = "rate_limited"
    elif status == 403:
        category = "permission"
    else:
        category = "transient"
    detail = category if status is None else f"{category}, HTTP {status}"
    return GitHubAccessError(
        category, f"GitHub CLI request failed ({detail})", http_status=status
    )


def _strict_positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _validated_pull_request_number(value: Any) -> int:
    number = _strict_positive_int(value)
    if number is None:
        raise ValueError("pull request number must be positive")
    return number


def _validated_short_head(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value.startswith(("refs/", "-", "/"))
        or value.endswith(("/", ".", ".lock"))
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
        or ".." in value
        or "@{" in value
        or any(character in value for character in " ~^:?*[\\%#")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be a short head name")
    return value


__all__ = [
    "DEFAULT_MAX_PAGES",
    "GitHubApiResponse",
    "GitHubAccess",
    "GitHubAccessStatus",
    "GitHubClient",
    "GitHubExecutionAccess",
    "GitHubRunnerAccess",
    "MAX_HEADER_BYTES",
    "MAX_PAGINATION_BYTES",
    "MAX_RESPONSE_BYTES",
]
