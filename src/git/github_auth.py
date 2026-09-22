"""Credential-source selection and in-memory GitHub App token lifecycle."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol

from src.config import GitHubAppConfig
from src.git.github_app import (
    AppTokenCandidate,
    AppTokenProvider,
    OwnerFilePrivateKeyProvider,
)
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubCredentialMode,
    GitHubRepositoryBinding,
)


REFRESH_WINDOW_SECONDS = 300


class TokenProvider(Protocol):
    @property
    def credential_identity(self) -> GitHubCredentialIdentity: ...

    async def mint(self, repository: GitHubRepositoryBinding) -> AppTokenCandidate: ...

    async def mint_for_repository(self, full_name: str) -> AppTokenCandidate: ...


@dataclass(frozen=True, slots=True)
class GitHubCredential:
    """One per-operation credential selection; token is never represented."""

    identity: GitHubCredentialIdentity
    repository: GitHubRepositoryBinding
    token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    generation: int = 0

    def __post_init__(self) -> None:
        if self.identity.mode is GitHubCredentialMode.APP:
            if not self.token or self.expires_at is None or self.generation <= 0:
                raise ValueError("App credentials require token, expiry, and generation")
        elif self.token is not None or self.expires_at is not None or self.generation != 0:
            raise ValueError("existing-login credentials cannot contain App token state")


@dataclass(frozen=True, slots=True)
class _CacheKey:
    identity: GitHubCredentialIdentity
    repository: GitHubRepositoryBinding


class GitHubAuth:
    """Select one startup credential mode and supply fresh per-operation credentials.

    A configured App is authoritative.  Failures from its provider propagate;
    this class never consults ambient tokens or the stored ``gh`` login in App
    mode.  With no App configuration, the returned token is ``None`` so the
    shared runner can leave PAT/keychain discovery to ``gh`` itself.
    """

    def __init__(
        self,
        app_config: GitHubAppConfig | None = None,
        *,
        app_provider: TokenProvider | None = None,
        clock=time.time,
    ) -> None:
        if app_config is None:
            if app_provider is not None:
                raise ValueError("an App token provider requires App configuration")
            self._identity = GitHubCredentialIdentity.existing_login()
            self._provider: TokenProvider | None = None
        else:
            if app_config.validate():
                raise ValueError("invalid GitHub App configuration")
            self._identity = GitHubCredentialIdentity.app(
                app_config.app_id,
                app_config.installation_id,
            )
            self._provider = app_provider or AppTokenProvider(
                app_config,
                key_provider=OwnerFilePrivateKeyProvider(),
                clock=clock,
            )
            if self._provider.credential_identity != self._identity:
                raise ValueError("App token provider identity does not match configuration")
        self._clock = clock
        self._cache: dict[_CacheKey, GitHubCredential] = {}
        self._locks: dict[_CacheKey, asyncio.Lock] = {}
        self._candidate_cache: dict[tuple[GitHubCredentialIdentity, str], AppTokenCandidate] = {}
        self._candidate_locks: dict[tuple[GitHubCredentialIdentity, str], asyncio.Lock] = {}
        self._generations: dict[_CacheKey, int] = {}

    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        return self._identity

    @property
    def mode(self) -> GitHubCredentialMode:
        return self._identity.mode

    async def credential_for(
        self,
        repository: GitHubRepositoryBinding,
        *,
        force_refresh: bool = False,
    ) -> GitHubCredential:
        """Select a fresh credential immediately before a repository operation."""
        if self.mode is GitHubCredentialMode.EXISTING_LOGIN:
            return GitHubCredential(self._identity, repository)
        key = _CacheKey(self._identity, repository)
        async with self._locks.setdefault(key, asyncio.Lock()):
            current = self._cache.get(key)
            if not force_refresh and self._fresh(current):
                return current
            return await self._mint_locked(key)

    async def refresh_after_rejection(
        self,
        repository: GitHubRepositoryBinding,
        *,
        rejected_generation: int,
    ) -> GitHubCredential:
        """Refresh only if the rejected generation is still current.

        A concurrent request may already have refreshed the token.  In that
        case the newer cached generation is reused instead of minting again.
        """
        if self.mode is GitHubCredentialMode.EXISTING_LOGIN:
            return GitHubCredential(self._identity, repository)
        key = _CacheKey(self._identity, repository)
        async with self._locks.setdefault(key, asyncio.Lock()):
            current = self._cache.get(key)
            if (
                self._fresh(current)
                and current is not None
                and current.generation != rejected_generation
            ):
                return current
            return await self._mint_locked(key)

    async def invalidate(
        self,
        repository: GitHubRepositoryBinding,
        *,
        generation: int,
    ) -> bool:
        """Drop exactly the rejected App generation, never a newer token."""
        if self.mode is GitHubCredentialMode.EXISTING_LOGIN:
            return False
        key = _CacheKey(self._identity, repository)
        async with self._locks.setdefault(key, asyncio.Lock()):
            current = self._cache.get(key)
            if current is None or current.generation != generation:
                return False
            del self._cache[key]
            return True

    async def candidate_for_repository(
        self,
        full_name: str,
        *,
        force_refresh: bool = False,
    ) -> AppTokenCandidate:
        """Mint a coalesced App candidate for later ``gh`` identity verification."""
        if self._provider is None:
            raise GitHubAccessError(
                "github_operation_unsupported",
                "App candidate credentials are unavailable in existing-login mode",
            )
        # Validate before making the name part of a cache key.
        canonical_name = GitHubRepositoryBinding(1, full_name).full_name
        key = (self._identity, canonical_name)
        async with self._candidate_locks.setdefault(key, asyncio.Lock()):
            current = self._candidate_cache.get(key)
            if (
                not force_refresh
                and current is not None
                and current.expires_at - self._clock() > REFRESH_WINDOW_SECONDS
            ):
                return current
            candidate = await self._provider.mint_for_repository(canonical_name)
            self._validate_candidate(
                candidate,
                full_name=canonical_name,
            )
            self._candidate_cache[key] = candidate
            return candidate

    async def accept_candidate(self, candidate: AppTokenCandidate) -> GitHubCredential:
        """Publish a candidate only after the caller verifies it through ``gh``."""
        self._validate_candidate(candidate)
        key = _CacheKey(self._identity, candidate.repository)
        async with self._locks.setdefault(key, asyncio.Lock()):
            current = self._cache.get(key)
            if self._fresh(current) and current is not None and current.token == candidate.token:
                return current
            credential = self._store(key, candidate)
            self._candidate_cache.pop((self._identity, candidate.repository.full_name), None)
            return credential

    async def discard_candidate(self, candidate: AppTokenCandidate) -> None:
        """Forget a candidate that failed external repository verification."""
        key = (self._identity, candidate.repository.full_name)
        async with self._candidate_locks.setdefault(key, asyncio.Lock()):
            current = self._candidate_cache.get(key)
            if current is not None and current.token == candidate.token:
                del self._candidate_cache[key]

    async def installation_token(
        self,
        repository: GitHubRepositoryBinding,
        *,
        force_refresh: bool = False,
    ) -> str | None:
        """Compatibility adapter while Git transfer consumers migrate."""
        return (await self.credential_for(repository, force_refresh=force_refresh)).token

    async def token_for(self, repository: GitHubRepositoryBinding) -> str | None:
        """Supply the fresh per-operation token expected by :class:`GhRunner`.

        The runner intentionally receives only this narrow adapter.  Callers
        which need generation-aware authentication recovery use
        :meth:`credential_for` through the composed GitHub access service.
        """
        return (await self.credential_for(repository)).token

    def _fresh(self, credential: GitHubCredential | None) -> bool:
        return (
            credential is not None
            and credential.expires_at is not None
            and credential.expires_at - self._clock() > REFRESH_WINDOW_SECONDS
        )

    async def _mint_locked(self, key: _CacheKey) -> GitHubCredential:
        if self._provider is None:  # pragma: no cover - guarded by mode
            raise AssertionError("App mode has no token provider")
        candidate = await self._provider.mint(key.repository)
        self._validate_candidate(candidate, repository=key.repository)
        return self._store(key, candidate)

    def _store(self, key: _CacheKey, candidate: AppTokenCandidate) -> GitHubCredential:
        generation = self._generations.get(key, 0) + 1
        self._generations[key] = generation
        credential = GitHubCredential(
            identity=self._identity,
            repository=key.repository,
            token=candidate.token,
            expires_at=candidate.expires_at,
            generation=generation,
        )
        self._cache[key] = credential
        return credential

    def _validate_candidate(
        self,
        candidate: AppTokenCandidate,
        *,
        repository: GitHubRepositoryBinding | None = None,
        full_name: str | None = None,
    ) -> None:
        if candidate.identity != self._identity:
            raise GitHubAccessError("credentials", "App candidate identity did not match")
        if repository is not None and candidate.repository != repository:
            raise GitHubAccessError("credentials", "App candidate repository did not match")
        if full_name is not None and candidate.repository.full_name != full_name:
            raise GitHubAccessError("credentials", "App candidate repository did not match")
        if not candidate.token or candidate.expires_at - self._clock() <= REFRESH_WINDOW_SECONDS:
            raise GitHubAccessError("credentials", "App candidate expiry was invalid")


__all__ = [
    "GitHubAuth",
    "GitHubCredential",
    "REFRESH_WINDOW_SECONDS",
    "TokenProvider",
]
