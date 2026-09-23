import pytest

from src.git.github_app import GitHubAppError
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


def test_composed_client_exposes_shared_identity() -> None:
    class _Client:
        credential_identity = GitHubCredentialIdentity.app(101, 202)

    assert credential_identity_from_client(_Client()) == GitHubCredentialIdentity.app(101, 202)


def test_credential_identity_from_client_requires_explicit_identity() -> None:
    with pytest.raises(ValueError, match="credential identity"):
        credential_identity_from_client(object())
    with pytest.raises(ValueError, match="credential identity"):
        credential_identity_from_client({"credential_identity": "app"})


def test_safe_error_contract_is_shared_with_legacy_app_name() -> None:
    error = GitHubAccessError("rate_limited", "GitHub request was rate limited", retry_at=123.0)

    assert GitHubAppError is GitHubAccessError
    assert error.category == "rate_limited"
    assert error.retry_at == 123.0
    assert str(error) == "GitHub request was rate limited"
