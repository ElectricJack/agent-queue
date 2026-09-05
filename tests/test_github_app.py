from __future__ import annotations

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
                    '"administration":"read","metadata":"read"}}' % expires
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
        },
    }
    for request in transport.requests:
        assert request[2]["Accept"] == "application/vnd.github+json"
        assert request[2]["X-GitHub-Api-Version"] == "2022-11-28"


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
            '"administration":"read"}}'
        ).encode(),
    )
    transport = ScriptedTransport(
        [
            HttpResponse(200, {}, b'{"id":101}'), token_response("first"),
            HttpResponse(200, {}, b'{"id":303,"full_name":"acme/widgets"}'),
            HttpResponse(401, {}, b'never expose this body'),
            HttpResponse(200, {}, b'{"id":101}'), token_response("second"),
            HttpResponse(200, {}, b'{"id":303,"full_name":"acme/widgets"}'),
            HttpResponse(200, {}, b'{"ok":true}'),
        ]
    )
    client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        GitHubRepositoryBinding(303, "acme/widgets"),
        key_provider=StaticKeyProvider(private), transport=transport,
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
            HttpResponse(201, {}, b'{"token":"sensitive","expires_at":"2030-01-01T00:00:00Z",'
                b'"repositories":[{"id":303}],"permissions":{"checks":"write",'
                b'"actions":"read","contents":"write","administration":"read"}}'),
            HttpResponse(200, {}, b'{"id":303,"full_name":"attacker/redirected"}'),
        ]
    )
    client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        GitHubRepositoryBinding(303, "acme/widgets"),
        key_provider=StaticKeyProvider(private), transport=transport,
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
