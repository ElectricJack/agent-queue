from types import SimpleNamespace

import pytest

from src.config import GitHubAppConfig
from src.git.github_app import GitHubAppClient, GitHubAppError
from src.git.github_cli import GitHubCLIClient
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubCredentialMode,
    GitHubRepositoryBinding,
    credential_identity_from_client,
)


BINDING = GitHubRepositoryBinding(303, "acme/widgets")


def test_shared_repository_binding_retains_existing_validation() -> None:
    assert BINDING.forge_host == "github.com"
    with pytest.raises(ValueError, match="positive integer"):
        GitHubRepositoryBinding(0, "acme/widgets")
    with pytest.raises(ValueError, match="owner/repository"):
        GitHubRepositoryBinding(303, "acme/widgets/extra")
    with pytest.raises(ValueError, match="unsupported_host"):
        GitHubRepositoryBinding(303, "acme/widgets", "example.com")


def test_credential_identity_is_non_secret_and_transport_independent() -> None:
    app = GitHubCredentialIdentity.app(101, 202)
    existing = GitHubCredentialIdentity.existing_login()

    assert app == GitHubCredentialIdentity(GitHubCredentialMode.APP, 101, 202)
    assert existing == GitHubCredentialIdentity(GitHubCredentialMode.EXISTING_LOGIN)
    assert set(app.__dataclass_fields__) == {"mode", "app_id", "installation_id"}


@pytest.mark.parametrize(
    "identity",
    [
        lambda: GitHubCredentialIdentity(GitHubCredentialMode.APP),
        lambda: GitHubCredentialIdentity(GitHubCredentialMode.APP, True),
        lambda: GitHubCredentialIdentity(GitHubCredentialMode.EXISTING_LOGIN, 101),
    ],
)
def test_credential_identity_rejects_ambiguous_or_invalid_values(identity) -> None:
    with pytest.raises(ValueError):
        identity()


def test_current_clients_expose_shared_identity_without_changing_selection() -> None:
    app_client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        BINDING,
        key_provider=SimpleNamespace(read_private_key=lambda _path: b"unused"),
    )
    cli_client = GitHubCLIClient(BINDING)

    assert app_client.credential_identity == GitHubCredentialIdentity.app(101, 202)
    assert cli_client.credential_identity == GitHubCredentialIdentity.existing_login()


def test_staged_legacy_client_adapter_prefers_explicit_identity() -> None:
    explicit = SimpleNamespace(
        credential_identity=GitHubCredentialIdentity.app(101, 202),
        auth_mode="gh",
    )
    legacy_cli = SimpleNamespace(auth_mode="gh")
    legacy_app = SimpleNamespace(config=SimpleNamespace(app_id=101))

    assert credential_identity_from_client(explicit).mode is GitHubCredentialMode.APP
    assert credential_identity_from_client(legacy_cli).mode is GitHubCredentialMode.EXISTING_LOGIN
    assert credential_identity_from_client(legacy_app) == GitHubCredentialIdentity.app(101)


def test_safe_error_contract_is_shared_with_legacy_app_name() -> None:
    error = GitHubAccessError("rate_limited", "GitHub request was rate limited", retry_at=123.0)

    assert GitHubAppError is GitHubAccessError
    assert error.category == "rate_limited"
    assert error.retry_at == 123.0
    assert str(error) == "GitHub request was rate limited"
