"""Startup-composed GitHub authentication and trusted repository binding.

This module is the narrow composition boundary between :class:`GitHubAuth`
and :class:`GhRunner`.  Repository operations are added here during the next
migration package; production consumers continue using their compatibility
clients until that cutover lands.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from src.config import GitHubAppConfig
from src.git.github_app import AppTokenCandidate
from src.git.github_auth import GitHubAuth, GitHubCredential, TokenProvider
from src.git.github_cli import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_DIAGNOSTIC_BYTES,
    MAX_STDIN_BYTES,
    GhResult,
    GhRunner,
)
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubCredentialMode,
    GitHubRepositoryBinding,
)


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


class GitHubAccess:
    """One startup-owned authentication service and shared ``gh`` runner."""

    def __init__(self, auth: GitHubAuth, runner: GhRunner) -> None:
        if runner.credential_identity != auth.credential_identity:
            raise ValueError("GitHub auth and runner credential identities do not match")
        self.auth = auth
        self.runner = runner

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
            result = await self.runner.run(
                ["api", "--method", "GET", f"repos/{full_name}"],
                repository=binding,
                credential=selected,
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
    ) -> GhResult:
        """Run a read and refresh one rejected App generation at most once."""
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
            )
        except GitHubAccessError as exc:
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
    ) -> GhResult:
        """Run a write once; an auth failure invalidates but never replays it."""
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
            )
        except GitHubAccessError as exc:
            if self.auth.mode is GitHubCredentialMode.APP and exc.category == "credentials":
                await self.auth.invalidate(repository, generation=selected.generation)
            raise

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
    ) -> GhResult:
        return await self.runner.run(
            args,
            repository=repository,
            credential=credential,
            stdin=stdin,
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )

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


__all__ = ["GitHubAccess", "GitHubAccessStatus"]
