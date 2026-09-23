"""Dependency-free contracts shared by GitHub authentication and transports.

This module deliberately owns no configuration lookup, token handling, process
launching, or network I/O.  Keeping these values here lets the authentication
provider, ``gh`` runner, repository client, and Git transport share identity
without importing one another.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any


_FULL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class GitHubRepositoryBinding:
    """Validated immutable identity for one GitHub repository."""

    repository_id: int
    full_name: str
    forge_host: str = "github.com"

    def __post_init__(self) -> None:
        if (
            isinstance(self.repository_id, bool)
            or not isinstance(self.repository_id, int)
            or self.repository_id <= 0
        ):
            raise ValueError("repository_id must be a positive integer")
        if self.forge_host != "github.com":
            raise ValueError("unsupported_host")
        if _FULL_NAME.fullmatch(self.full_name) is None:
            raise ValueError("full_name must be owner/repository")


class GitHubCredentialMode(StrEnum):
    """Credential source, independent of the command or Git transport."""

    APP = "app"
    EXISTING_LOGIN = "existing_login"


@dataclass(frozen=True, slots=True)
class GitHubCredentialIdentity:
    """Non-secret identity of the credential authority selected at startup."""

    mode: GitHubCredentialMode
    app_id: int | None = None
    installation_id: int | None = None

    def __post_init__(self) -> None:
        try:
            mode = GitHubCredentialMode(self.mode)
        except ValueError as exc:
            raise ValueError("unsupported GitHub credential mode") from exc
        object.__setattr__(self, "mode", mode)

        for name in ("app_id", "installation_id"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer when present")
        if mode is GitHubCredentialMode.APP and self.app_id is None:
            raise ValueError("app credentials require app_id")
        if mode is GitHubCredentialMode.EXISTING_LOGIN and (
            self.app_id is not None or self.installation_id is not None
        ):
            raise ValueError("existing_login credentials cannot carry App identity")

    @classmethod
    def app(cls, app_id: int, installation_id: int | None = None) -> "GitHubCredentialIdentity":
        return cls(
            mode=GitHubCredentialMode.APP,
            app_id=app_id,
            installation_id=installation_id,
        )

    @classmethod
    def existing_login(cls) -> "GitHubCredentialIdentity":
        return cls(mode=GitHubCredentialMode.EXISTING_LOGIN)


class GitHubAccessError(RuntimeError):
    """Safe GitHub failure containing classification but no raw diagnostics."""

    def __init__(self, category: str, message: str, *, retry_at: float | None = None):
        self.category = category
        self.retry_at = retry_at
        super().__init__(message)


def credential_identity_from_client(client: Any) -> GitHubCredentialIdentity:
    """Read the shared credential identity from the composed client.

    Both credential sources are surfaced by the single ``GitHubClient`` as an
    explicit ``credential_identity``; legacy adapters had that identity
    carried by their transport instead, which the final migration removed.
    """

    identity = getattr(client, "credential_identity", None)
    if isinstance(identity, GitHubCredentialIdentity):
        return identity
    raise ValueError("GitHub client credential identity is unavailable")


__all__ = [
    "GitHubAccessError",
    "GitHubCredentialIdentity",
    "GitHubCredentialMode",
    "GitHubRepositoryBinding",
    "credential_identity_from_client",
]
