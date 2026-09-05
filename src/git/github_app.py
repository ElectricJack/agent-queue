"""Daemon-only GitHub App authentication and narrowly bound REST access."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import aiohttp
import jwt

from src.config import GitHubAppConfig

ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
API_BASE = "https://api.github.com"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_PERMISSIONS = {
    "checks": "write",
    "actions": "read",
    "contents": "write",
    "administration": "read",
}


@dataclass(frozen=True)
class GitHubRepositoryBinding:
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
        owner, separator, repo = self.full_name.partition("/")
        if not separator or not owner or not repo or "/" in repo:
            raise ValueError("full_name must be owner/repository")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class PrivateKeyProvider(Protocol):
    def read_private_key(self, path: str) -> bytes: ...


class OwnerFilePrivateKeyProvider:
    """Read a regular private-key file owned by this daemon user only."""

    def read_private_key(self, path: str) -> bytes:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise GitHubAppError("credentials", "private key is unreadable") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise GitHubAppError("credentials", "private key ownership is invalid")
            if not info.st_mode & stat.S_IRUSR or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise GitHubAppError("credentials", "private key permissions are too broad")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return stream.read(MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise GitHubAppError("credentials", "private key is unreadable") from exc
        finally:
            os.close(descriptor)


class HttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        max_bytes: int,
    ) -> HttpResponse: ...


class AiohttpTransport:
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        max_bytes: int,
    ) -> HttpResponse:
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers, json=json_body) as response:
                    body = await response.content.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise GitHubAppError("transient", "GitHub response exceeded size limit")
                    return HttpResponse(response.status, dict(response.headers), body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise GitHubAppError("transient", "GitHub request failed") from exc


class GitHubAppError(RuntimeError):
    """Safe provider failure without response bodies or credentials."""

    def __init__(self, category: str, message: str, *, retry_at: float | None = None):
        self.category = category
        self.retry_at = retry_at
        super().__init__(message)


class GitHubAppClient:
    def __init__(
        self,
        config: GitHubAppConfig,
        repository: GitHubRepositoryBinding,
        *,
        key_provider: PrivateKeyProvider,
        transport: HttpTransport | None = None,
        clock=time.time,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if config.validate():
            raise ValueError("invalid GitHub App configuration")
        self.config = config
        self.repository = repository
        self.key_provider = key_provider
        self.transport = transport or AiohttpTransport()
        self.clock = clock
        self.max_response_bytes = max_response_bytes
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def _base_headers(self) -> dict[str, str]:
        return {"Accept": ACCEPT, "X-GitHub-Api-Version": API_VERSION}

    def _app_jwt(self) -> str:
        now = int(self.clock())
        private_key = self.key_provider.read_private_key(self.config.private_key_path)
        if len(private_key) > MAX_RESPONSE_BYTES:
            raise GitHubAppError("credentials", "private key exceeded size limit")
        try:
            return jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": self.config.client_id},
                private_key,
                algorithm="RS256",
            )
        except Exception as exc:
            raise GitHubAppError("credentials", "private key could not sign App JWT") from exc

    async def installation_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            if (
                not force_refresh
                and self._token is not None
                and self._token_expires_at - self.clock() > 300
            ):
                return self._token
            token, expiry = await self._mint_installation_token()
            self._token = token
            self._token_expires_at = expiry
            return token

    async def _mint_installation_token(self) -> tuple[str, float]:
        app_jwt = self._app_jwt()
        app = await self._raw_json("GET", "/app", credential=app_jwt)
        if _strict_positive_int(app.get("id")) != self.config.app_id:
            raise GitHubAppError("credentials", "authenticated App identity did not match")
        token_body = await self._raw_json(
            "POST",
            f"/app/installations/{self.config.installation_id}/access_tokens",
            credential=app_jwt,
            json_body={"repository_ids": [self.repository.repository_id], "permissions": _PERMISSIONS},
            expected_statuses={201},
        )
        token = token_body.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError("credentials", "installation token response was malformed")
        repositories = token_body.get("repositories")
        if not isinstance(repositories, list) or [
            _strict_positive_int(repo.get("id")) if isinstance(repo, dict) else None
            for repo in repositories
        ] != [self.repository.repository_id]:
            raise GitHubAppError("permission", "installation repository selection did not match")
        permissions = token_body.get("permissions")
        if not isinstance(permissions, dict) or any(
            permissions.get(name) != level for name, level in _PERMISSIONS.items()
        ) or set(permissions) - (set(_PERMISSIONS) | {"metadata"}):
            raise GitHubAppError("permission", "installation permissions did not match")
        expiry = _parse_timestamp(token_body.get("expires_at"))
        repository = await self._raw_json(
            "GET",
            f"/repositories/{self.repository.repository_id}",
            credential=token,
        )
        if (
            _strict_positive_int(repository.get("id")) != self.repository.repository_id
            or repository.get("full_name") != self.repository.full_name
        ):
            raise GitHubAppError("credentials", "authenticated repository identity did not match")
        return token, expiry

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        token = await self.installation_token()
        try:
            return await self._raw_json(
                method, path, credential=token, json_body=json_body,
                expected_statuses=expected_statuses,
            )
        except GitHubAppError as exc:
            if exc.category != "credentials":
                raise
        token = await self.installation_token(force_refresh=True)
        return await self._raw_json(
            method, path, credential=token, json_body=json_body,
            expected_statuses=expected_statuses,
        )

    async def paged_items(self, path: str, *, key: str, max_pages: int = 20) -> list[dict]:
        next_url = urljoin(API_BASE, path)
        items: list[dict] = []
        for _ in range(max_pages):
            parsed = urlparse(next_url)
            if parsed.scheme != "https" or parsed.netloc != "api.github.com":
                raise GitHubAppError("conflict_or_invalid", "pagination escaped GitHub API host")
            response = await self._authenticated_response("GET", next_url)
            payload = _decode_object(response.body)
            page = payload.get(key)
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise GitHubAppError("conflict_or_invalid", "GitHub page was malformed")
            items.extend(page)
            next_url = _next_link(response.headers.get("Link") or response.headers.get("link"))
            if next_url is None:
                return items
        raise GitHubAppError("transient", "GitHub pagination exceeded page limit")

    async def _authenticated_response(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> HttpResponse:
        token = await self.installation_token()
        try:
            return await self._request_response(
                method, url, credential=token, json_body=json_body
            )
        except GitHubAppError as exc:
            if exc.category != "credentials":
                raise
        token = await self.installation_token(force_refresh=True)
        return await self._request_response(method, url, credential=token, json_body=json_body)

    async def _raw_json(
        self,
        method: str,
        path: str,
        *,
        credential: str,
        json_body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        response = await self._request_response(
            method, urljoin(API_BASE, path), credential=credential, json_body=json_body
        )
        allowed = expected_statuses or {200}
        if response.status not in allowed:
            raise _http_error(response.status, response.headers, self.clock())
        return _decode_object(response.body)

    async def _request_response(
        self,
        method: str,
        url: str,
        *,
        credential: str,
        json_body: dict[str, Any] | None = None,
    ) -> HttpResponse:
        headers = self._base_headers | {"Authorization": f"Bearer {credential}"}
        response = await self.transport.request(
            method, url, headers=headers, json_body=json_body, max_bytes=self.max_response_bytes
        )
        if len(response.body) > self.max_response_bytes:
            raise GitHubAppError("transient", "GitHub response exceeded size limit")
        if response.status >= 400:
            raise _http_error(response.status, response.headers, self.clock())
        return response


def _strict_positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _decode_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubAppError("conflict_or_invalid", "GitHub response was not valid JSON") from exc
    if not isinstance(value, dict):
        raise GitHubAppError("conflict_or_invalid", "GitHub response was not an object")
    return value


def _parse_timestamp(value: Any) -> float:
    if not isinstance(value, str):
        raise GitHubAppError("credentials", "installation token expiry was malformed")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise GitHubAppError("credentials", "installation token expiry was malformed") from exc


def _http_error(status: int, headers: dict[str, str], now: float) -> GitHubAppError:
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    remaining = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
    if status in {403, 429} and (retry_after is not None or remaining == "0"):
        retry_at = now
        if retry_after:
            try:
                retry_at = now + max(0, int(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after).timestamp()
                except (TypeError, ValueError):
                    pass
        return GitHubAppError("rate_limited", "GitHub request was rate limited", retry_at=retry_at)
    category = {
        401: "credentials",
        403: "permission",
        404: "not_found_or_hidden",
        409: "conflict_or_invalid",
        422: "conflict_or_invalid",
    }.get(status, "transient" if status >= 500 else "conflict_or_invalid")
    return GitHubAppError(category, f"GitHub request failed ({category})")


def _next_link(value: str | None) -> str | None:
    if not value:
        return None
    for item in value.split(","):
        target, *parameters = item.split(";")
        if any(parameter.strip() == 'rel="next"' for parameter in parameters):
            target = target.strip()
            if target.startswith("<") and target.endswith(">"):
                return target[1:-1]
    return None
