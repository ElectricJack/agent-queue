from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.config import GitHubAppConfig
from src.git.github_app import (
    GitHubAppClient,
    GitHubAppError,
    GitHubRepositoryBinding,
    HttpResponse,
    OwnerFilePrivateKeyProvider,
)


@pytest.mark.parametrize(
    "full_name",
    ["acme/widgets?redirect=https://attacker.example", "acme/widgets/extra", "-acme/widgets"],
)
def test_repository_binding_rejects_nonliteral_github_full_name(full_name):
    with pytest.raises(ValueError, match="full_name"):
        GitHubRepositoryBinding(303, full_name)


class StaticKeyProvider:
    def __init__(self, key: bytes):
        self.key = key

    def read_private_key(self, path: str) -> bytes:
        assert path == "/daemon/key.pem"
        return self.key


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, url, *, headers, json_body=None, max_bytes):
        self.requests.append((method, url, dict(headers), json_body, max_bytes))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_exact_head_ref_is_numeric_repository_bound_and_escaped():
    private, _public = _private_key()
    oid = "a" * 40
    transport = ScriptedTransport(
        [
            HttpResponse(
                200, {}, ('{"ref":"refs/heads/feature/x","object":{"sha":"%s"}}' % oid).encode()
            )
        ]
    )
    client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        GitHubRepositoryBinding(303, "acme/widgets"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )
    client._token = "installation-secret"
    client._token_expires_at = 1_800_001_000.0

    assert await client.exact_head_ref("feature/x") == oid
    assert transport.requests[0][1] == (
        "https://api.github.com/repositories/303/git/ref/heads/feature%2Fx"
    )


def _private_key() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public


@pytest.mark.asyncio
async def test_mints_narrow_installation_token_after_app_and_repository_binding():
    private, public = _private_key()
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'),
            HttpResponse(
                201,
                {},
                (
                    '{"token":"installation-secret","expires_at":"%s",'
                    '"repositories":[{"id":303}],"permissions":'
                    '{"checks":"write","actions":"read","contents":"write",'
                    '"administration":"read","pull_requests":"write",'
                    '"issues":"write","variables":"read","metadata":"read"}}'
                    % expires
                ).encode(),
            ),
            HttpResponse(200, {}, b'{"id":303,"full_name":"acme/widgets"}'),
        ]
    )
    client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        GitHubRepositoryBinding(303, "acme/widgets"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )

    assert await client.installation_token() == "installation-secret"
    app_jwt = transport.requests[0][2]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(
        app_jwt,
        public,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {"iat": 1_799_999_940, "exp": 1_800_000_540, "iss": "Iv1.client"}
    assert transport.requests[1][3] == {
        "repository_ids": [303],
        "permissions": {
            "checks": "write",
            "actions": "read",
            "contents": "write",
            "administration": "read",
            "pull_requests": "write",
            "issues": "write",
            "variables": "read",
        },
    }
    for request in transport.requests:
        assert request[2]["Accept"] == "application/vnd.github+json"
        assert request[2]["X-GitHub-Api-Version"] == "2022-11-28"


@pytest.mark.asyncio
async def test_rejects_installation_token_without_variables_read_permission():
    private, _public = _private_key()
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'),
            HttpResponse(
                201,
                {},
                b'{"token":"installation-secret",'
                b'"expires_at":"2030-01-01T00:00:00Z",'
                b'"repositories":[{"id":303}],"permissions":{"checks":"write",'
                b'"actions":"read","contents":"write","administration":"read",'
                b'"pull_requests":"write","issues":"write"}}',
            ),
            HttpResponse(200, {}, b'{"id":303,"full_name":"acme/widgets"}'),
        ]
    )
    client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        GitHubRepositoryBinding(303, "acme/widgets"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )

    with pytest.raises(GitHubAppError) as caught:
        await client.installation_token()

    assert caught.value.category == "permission"
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_binds_repository_by_name_with_one_narrow_installation_token():
    private, _public = _private_key()
    expires = "2030-01-01T00:00:00Z"
    permissions = {
        "checks": "write",
        "actions": "read",
        "contents": "write",
        "administration": "read",
        "pull_requests": "write",
        "issues": "write",
        "variables": "read",
    }
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'),
            HttpResponse(
                201,
                {},
                (
                    '{"token":"installation-secret","expires_at":"%s",'
                    '"repositories":[{"id":303,"name":"widgets",'
                    '"full_name":"acme/widgets"}],"permissions":%s}'
                    % (expires, json.dumps(permissions))
                ).encode(),
            ),
        ]
    )

    client = await GitHubAppClient.bind_repository(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        "acme/widgets",
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )

    assert client.repository == GitHubRepositoryBinding(303, "acme/widgets")
    assert await client.installation_token() == "installation-secret"
    assert transport.requests[1][3] == {
        "repositories": ["widgets"],
        "permissions": permissions,
    }


@pytest.mark.asyncio
async def test_audit_pr_transport_reconciles_by_marker_and_creates_exact_bound_pr():
    private, _public = _private_key()
    head = "a" * 40
    key = "b" * 64
    payload = {
        "html_url": "https://github.com/acme/widgets/pull/7",
        "number": 7,
        "state": "open",
        "body": f"<!-- aq-integration-audit:{key} -->",
        "head": {
            "sha": head,
            "ref": "aq/integration/batch",
            "repo": {"id": 303, "full_name": "acme/widgets"},
        },
        "base": {"ref": "main"},
    }
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, json.dumps([payload]).encode()),
            HttpResponse(201, {}, json.dumps(payload).encode()),
        ]
    )
    client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        GitHubRepositoryBinding(303, "acme/widgets"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )
    client._token = "installation-secret"
    client._token_expires_at = 1_800_001_000.0

    found = await client.lookup_audit_pr(idempotency_key=key)
    created = await client.create_audit_pr(
        repository_id="repo",
        branch="aq/integration/batch",
        head_sha=head,
        base_branch="main",
        batch_id="batch",
        idempotency_key=key,
        repository_numeric_id=303,
        repository_full_name="acme/widgets",
    )

    assert found == created
    assert found.idempotency_key == key
    assert transport.requests[0][1].endswith("/repositories/303/pulls?state=all&per_page=100")
    assert transport.requests[1][3] == {
        "title": "Integration train batch",
        "head": "aq/integration/batch",
        "base": "main",
        "body": f"<!-- aq-integration-audit:{key} -->\nRoot integration batch `batch`.",
    }


@pytest.mark.asyncio
async def test_authenticated_request_retries_one_401_with_a_fresh_token():
    private, _ = _private_key()
    expires = "2030-01-01T00:00:00Z"
    token_response = lambda token: HttpResponse(  # noqa: E731
        201,
        {},
        (
            f'{{"token":"{token}","expires_at":"{expires}",'
            '"repositories":[{"id":303}],"permissions":'
            '{"checks":"write","actions":"read","contents":"write",'
            '"administration":"read","pull_requests":"write","issues":"write",'
            '"variables":"read"}}'
        ).encode(),
    )
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'),
            token_response("first"),
            HttpResponse(200, {}, b'{"id":303,"full_name":"acme/widgets"}'),
            HttpResponse(401, {}, b"never expose this body"),
            HttpResponse(200, {}, b'{"id":101}'),
            token_response("second"),
            HttpResponse(200, {}, b'{"id":303,"full_name":"acme/widgets"}'),
            HttpResponse(200, {}, b'{"ok":true}'),
        ]
    )
    client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        GitHubRepositoryBinding(303, "acme/widgets"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )

    assert await client.request_json("GET", "/repositories/303") == {"ok": True}
    assert transport.requests[-1][2]["Authorization"] == "Bearer second"


@pytest.mark.asyncio
async def test_repository_identity_mismatch_fails_closed_without_response_body():
    private, _ = _private_key()
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'),
            HttpResponse(
                201,
                {},
                b'{"token":"sensitive","expires_at":"2030-01-01T00:00:00Z",'
                b'"repositories":[{"id":303}],"permissions":{"checks":"write",'
                b'"actions":"read","contents":"write","administration":"read",'
                b'"pull_requests":"write","issues":"write","variables":"read"}}',
            ),
            HttpResponse(200, {}, b'{"id":303,"full_name":"attacker/redirected"}'),
        ]
    )
    client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        GitHubRepositoryBinding(303, "acme/widgets"),
        key_provider=StaticKeyProvider(private),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )

    with pytest.raises(GitHubAppError) as caught:
        await client.installation_token()
    assert caught.value.category == "credentials"
    assert "sensitive" not in str(caught.value)
    assert "redirected" not in str(caught.value)


def test_file_key_provider_rejects_group_readable_and_symlink_paths(tmp_path):
    key = tmp_path / "key.pem"
    key.write_bytes(b"private-sentinel")
    key.chmod(0o640)
    provider = OwnerFilePrivateKeyProvider()
    with pytest.raises(GitHubAppError, match="permissions") as broad:
        provider.read_private_key(str(key))
    assert "private-sentinel" not in str(broad.value)

    link = tmp_path / "link.pem"
    link.symlink_to(key)
    with pytest.raises(GitHubAppError, match="unreadable"):
        provider.read_private_key(str(link))
