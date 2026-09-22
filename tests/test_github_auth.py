from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from src.config import GitHubAppConfig
from src.git.github import GitHubAccess
from src.git.github_app import AppTokenCandidate
from src.git.github_auth import GitHubAuth
from src.git.github_cli import GhResult
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


class FakeRunner:
    def __init__(
        self,
        identity: GitHubCredentialIdentity,
        *,
        payloads: dict[GitHubRepositoryBinding, dict[str, Any]] | None = None,
        reject_generations: set[int] | None = None,
        reject_all: bool = False,
    ) -> None:
        self.credential_identity = identity
        self.payloads = payloads or {}
        self.reject_generations = reject_generations or set()
        self.reject_all = reject_all
        self.calls: list[tuple[GitHubRepositoryBinding, Any]] = []
        self.availability_checks = 0

    def cli_available(self) -> bool:
        self.availability_checks += 1
        return True

    async def run(
        self,
        args,
        *,
        repository,
        credential,
        **kwargs,
    ) -> GhResult:
        del args, kwargs
        self.calls.append((repository, credential))
        await asyncio.sleep(0)
        generation = getattr(credential, "generation", 0)
        if self.reject_all or generation in self.reject_generations:
            raise GitHubAccessError("credentials", "candidate credential was rejected")
        payload = self.payloads.get(
            repository,
            {"id": repository.repository_id, "full_name": repository.full_name},
        )
        return GhResult(0, json.dumps(payload).encode(), "")


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


@pytest.mark.asyncio
async def test_composed_binding_verifies_candidates_without_recursive_provider_calls() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)
    runner = FakeRunner(auth.credential_identity)
    access = GitHubAccess(auth, runner)

    widgets, gadgets = await asyncio.gather(
        access.bind_repository("git@github.com:acme/widgets.git"),
        access.bind_repository("https://github.com/acme/gadgets"),
    )

    assert {widgets, gadgets} == {WIDGETS, GADGETS}
    assert set(provider.bind_calls) == {"acme/widgets", "acme/gadgets"}
    assert provider.mint_calls == []
    assert {repository for repository, _credential in runner.calls} == {WIDGETS, GADGETS}
    assert all(isinstance(credential, AppTokenCandidate) for _, credential in runner.calls)
    assert await auth.credential_for(WIDGETS) is not None
    assert provider.mint_calls == []


@pytest.mark.asyncio
async def test_binding_fences_trusted_identity_and_rejects_api_identity_mismatch() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)
    runner = FakeRunner(
        auth.credential_identity,
        payloads={WIDGETS: {"id": 999, "full_name": WIDGETS.full_name}},
    )
    access = GitHubAccess(auth, runner)

    with pytest.raises(GitHubAccessError, match="authorized repository"):
        await access.bind_repository("acme/gadgets", expected_binding=WIDGETS)
    with pytest.raises(GitHubAccessError, match="invalid"):
        await access.bind_repository("https://example.com/acme/widgets")
    with pytest.raises(GitHubAccessError, match="identity"):
        await access.bind_repository("acme/widgets")

    assert provider.bind_calls == ["acme/widgets"]
    assert provider.mint_calls == []
    assert access.validate_pr_url(WIDGETS, "https://github.com/acme/widgets/pull/17") == 17
    with pytest.raises(GitHubAccessError, match="authorized repository"):
        access.validate_pr_url(WIDGETS, "https://github.com/acme/gadgets/pull/17")
    with pytest.raises(GitHubAccessError, match="invalid"):
        access.validate_pr_url(WIDGETS, "https://evil.example/acme/widgets/pull/17")
    with pytest.raises(GitHubAccessError, match="invalid"):
        access.validate_pr_url(WIDGETS, "https://[invalid/acme/widgets/pull/17")


@pytest.mark.asyncio
async def test_read_auth_retry_is_once_and_reuses_concurrently_refreshed_generation() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)
    runner = FakeRunner(auth.credential_identity, reject_generations={1})
    access = GitHubAccess(auth, runner)

    results = await asyncio.gather(
        *(access.run_read(["api", "repos/acme/widgets"], repository=WIDGETS) for _ in range(8))
    )

    assert len(results) == 8
    assert len(provider.mint_calls) == 2
    assert [credential.generation for _, credential in runner.calls].count(1) == 8
    assert [credential.generation for _, credential in runner.calls].count(2) == 8

    always_rejected_provider = FakeProvider(clock)
    always_rejected_auth = GitHubAuth(
        CONFIG,
        app_provider=always_rejected_provider,
        clock=clock,
    )
    always_rejected_runner = FakeRunner(always_rejected_auth.credential_identity, reject_all=True)
    always_rejected_access = GitHubAccess(
        always_rejected_auth,
        always_rejected_runner,
    )
    with pytest.raises(GitHubAccessError, match="rejected"):
        await always_rejected_access.run_read(
            ["api", "repos/acme/widgets"],
            repository=WIDGETS,
        )
    assert len(always_rejected_provider.mint_calls) == 2
    assert len(always_rejected_runner.calls) == 2
    assert await always_rejected_auth.invalidate(WIDGETS, generation=2) is False


@pytest.mark.asyncio
async def test_write_auth_failure_is_invalidated_but_never_replayed() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    auth = GitHubAuth(CONFIG, app_provider=provider, clock=clock)
    runner = FakeRunner(auth.credential_identity, reject_all=True)
    access = GitHubAccess(auth, runner)

    with pytest.raises(GitHubAccessError, match="rejected"):
        await access.run_write(
            ["api", "--method", "POST", "repos/acme/widgets/issues"],
            repository=WIDGETS,
            stdin=b"{}",
        )

    assert len(runner.calls) == 1
    assert len(provider.mint_calls) == 1
    assert await auth.invalidate(WIDGETS, generation=1) is False


def test_composition_status_is_startup_bound_and_has_no_personal_identity_probe() -> None:
    clock = Clock()
    provider = FakeProvider(clock)
    access = GitHubAccess.from_config(
        CONFIG,
        app_provider=provider,
        clock=clock,
        executable="/usr/bin/true",
        env={},
    )

    status = access.status()

    assert status.to_dict() == {
        "cli_available": True,
        "credential_mode": "app",
        "app_id": 101,
        "installation_id": 202,
        "configuration_scope": "startup",
        "configuration_changes_require_restart": True,
    }
    assert access.runner.credentials is access.auth
    assert provider.bind_calls == []
    assert provider.mint_calls == []
