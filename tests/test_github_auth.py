from __future__ import annotations

import asyncio
import os

import pytest

from src.config import GitHubAppConfig
from src.git.github_app import AppTokenCandidate
from src.git.github_auth import GitHubAuth
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubCredentialMode,
    GitHubRepositoryBinding,
)


CONFIG = GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem")
WIDGETS = GitHubRepositoryBinding(303, "acme/widgets")
GADGETS = GitHubRepositoryBinding(404, "acme/gadgets")


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeProvider:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.credential_identity = GitHubCredentialIdentity.app(101, 202)
        self.mint_calls: list[GitHubRepositoryBinding] = []
        self.bind_calls: list[str] = []
        self.failure: GitHubAccessError | None = None

    async def mint(self, repository: GitHubRepositoryBinding) -> AppTokenCandidate:
        self.mint_calls.append(repository)
        await asyncio.sleep(0)
        if self.failure is not None:
            raise self.failure
        return self._candidate(repository, len(self.mint_calls))

    async def mint_for_repository(self, full_name: str) -> AppTokenCandidate:
        self.bind_calls.append(full_name)
        await asyncio.sleep(0)
        repository = WIDGETS if full_name == WIDGETS.full_name else GADGETS
        return self._candidate(repository, len(self.bind_calls))

    def _candidate(self, repository: GitHubRepositoryBinding, generation: int) -> AppTokenCandidate:
        return AppTokenCandidate(
            identity=self.credential_identity,
            repository=repository,
            token=f"secret-{repository.repository_id}-{generation}",
            expires_at=self.clock() + 3600,
        )


@pytest.mark.asyncio
async def test_existing_login_delegates_discovery_without_extracting_a_pat(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ambient-gh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-secret")
    auth = GitHubAuth()

    selected = await auth.credential_for(WIDGETS)

    assert auth.mode is GitHubCredentialMode.EXISTING_LOGIN
    assert selected.identity == GitHubCredentialIdentity.existing_login()
    assert selected.token is None
    assert selected.generation == 0
    assert "ambient" not in repr(selected)
    assert os.environ["GH_TOKEN"] == "ambient-gh-secret"
    assert os.environ["GITHUB_TOKEN"] == "ambient-github-secret"


@pytest.mark.asyncio
async def test_configured_app_failure_never_falls_back_to_ambient_credentials(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ambient-gh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-secret")
    clock = Clock()
    provider = FakeProvider(clock)
    provider.failure = GitHubAccessError("permission", "App installation denied")
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)

    with pytest.raises(GitHubAccessError, match="App installation denied") as caught:
        await auth.credential_for(WIDGETS)

    assert caught.value.category == "permission"
    assert provider.mint_calls == [WIDGETS]
    assert auth.mode is GitHubCredentialMode.APP
    assert os.environ["GH_TOKEN"] == "ambient-gh-secret"
    assert os.environ["GITHUB_TOKEN"] == "ambient-github-secret"


@pytest.mark.asyncio
async def test_repository_cache_coalesces_concurrent_refresh_without_mixing_tokens() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)

    widgets = await asyncio.gather(*(auth.credential_for(WIDGETS) for _ in range(12)))
    widget, gadget = await asyncio.gather(
        auth.credential_for(WIDGETS),
        auth.credential_for(GADGETS),
    )

    assert provider.mint_calls.count(WIDGETS) == 1
    assert provider.mint_calls.count(GADGETS) == 1
    assert len({credential.token for credential in widgets}) == 1
    assert widget.token != gadget.token
    assert widget.repository == WIDGETS
    assert gadget.repository == GADGETS


@pytest.mark.asyncio
async def test_refresh_window_and_rejected_generation_coalesce_to_one_new_token() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)
    first = await auth.credential_for(WIDGETS)

    refreshed = await asyncio.gather(
        *(
            auth.refresh_after_rejection(
                WIDGETS,
                rejected_generation=first.generation,
            )
            for _ in range(8)
        )
    )

    assert len(provider.mint_calls) == 2
    assert {credential.generation for credential in refreshed} == {2}
    assert len({credential.token for credential in refreshed}) == 1
    assert refreshed[0].token != first.token

    expires_at = refreshed[0].expires_at
    assert expires_at is not None
    clock.now = expires_at - 299
    near_expiry = await auth.credential_for(WIDGETS)
    assert near_expiry.generation == 3


@pytest.mark.asyncio
async def test_candidate_is_coalesced_hidden_and_not_ready_until_explicit_acceptance() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)

    candidates = await asyncio.gather(
        *(auth.candidate_for_repository("acme/widgets") for _ in range(6))
    )

    assert provider.bind_calls == ["acme/widgets"]
    assert len({candidate.token for candidate in candidates}) == 1
    assert candidates[0].repository == WIDGETS
    assert candidates[0].token not in repr(candidates[0])

    accepted = await auth.accept_candidate(candidates[0])
    ready = await auth.credential_for(WIDGETS)
    assert ready is accepted
    assert ready.token == candidates[0].token
    assert provider.mint_calls == []


@pytest.mark.asyncio
async def test_candidate_identity_and_repository_are_fenced() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)
    foreign = AppTokenCandidate(
        identity=GitHubCredentialIdentity.app(999, 202),
        repository=WIDGETS,
        token="foreign-secret",
        expires_at=clock() + 3600,
    )

    with pytest.raises(GitHubAccessError, match="identity"):
        await auth.accept_candidate(foreign)

    provider.credential_identity = GitHubCredentialIdentity.app(999, 202)
    with pytest.raises(ValueError, match="does not match"):
        GitHubAuth(CONFIG, app_provider=provider, clock=clock)


@pytest.mark.asyncio
async def test_invalidation_cannot_drop_a_newer_generation() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)
    first = await auth.credential_for(WIDGETS)
    second = await auth.credential_for(WIDGETS, force_refresh=True)

    assert not await auth.invalidate(WIDGETS, generation=first.generation)
    assert await auth.credential_for(WIDGETS) is second
    assert await auth.invalidate(WIDGETS, generation=second.generation)
    third = await auth.credential_for(WIDGETS)
    assert third.generation == 3
