"""Repository-bound GitHub REST access through the operator's ``gh`` login."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from typing import Any, ClassVar

from src.git.github_app import (
    MAX_RESPONSE_BYTES,
    GitHubAppClient,
    GitHubAppError,
    GitHubRepositoryBinding,
)


class GitHubCLIClient(GitHubAppClient):
    """The repository-scoped GitHub client contract backed by ``gh api``."""

    auth_mode: ClassVar[str] = "gh"

    def __init__(
        self,
        repository: GitHubRepositoryBinding,
        *,
        executable: str = "gh",
        env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.repository = repository
        self.executable = executable
        self._env = dict(os.environ if env is None else env)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    @classmethod
    async def bind_repository(
        cls,
        full_name: str,
        *,
        executable: str = "gh",
        env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> GitHubCLIClient:
        provisional = GitHubRepositoryBinding(1, full_name)
        client = cls(
            provisional,
            executable=executable,
            env=env,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )
        payload = await client.request_json("GET", f"repos/{full_name}")
        repository_id = payload.get("id")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or payload.get("full_name") != full_name
        ):
            raise GitHubAppError("credentials", "authenticated repository identity did not match")
        client.repository = GitHubRepositoryBinding(repository_id, full_name)
        return client

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        del expected_statuses  # gh exits nonzero for HTTP statuses outside the success range.
        payload = await self._api_json(method, path, json_body=json_body)
        if not isinstance(payload, dict):
            raise GitHubAppError("conflict_or_invalid", "GitHub response was not an object")
        return payload

    async def paged_items(
        self, path: str, *, key: str, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        pages = await self._api_json("GET", path, paginate=True)
        if not isinstance(pages, list) or len(pages) > max_pages:
            raise GitHubAppError("transient", "GitHub pagination exceeded page limit")
        items: list[dict[str, Any]] = []
        for payload in pages:
            page = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise GitHubAppError("conflict_or_invalid", "GitHub page was malformed")
            items.extend(page)
        return items

    async def paged_list(self, path: str, *, max_pages: int = 20) -> list[dict[str, Any]]:
        pages = await self._api_json("GET", path, paginate=True)
        if not isinstance(pages, list) or len(pages) > max_pages:
            raise GitHubAppError("transient", "GitHub pagination exceeded page limit")
        items: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise GitHubAppError("conflict_or_invalid", "GitHub page was malformed")
            items.extend(page)
        return items

    async def installation_token(self, *, force_refresh: bool = False) -> None:
        """Signal that Git must use the existing ``gh`` credential, not a token."""
        del force_refresh
        return None

    async def _api_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        paginate: bool = False,
    ) -> Any:
        method = method.upper()
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise ValueError("unsupported GitHub API method")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith(("-", "//", "http://", "https://"))
            or any(character in path for character in ("\n", "\r", "\x00"))
        ):
            raise ValueError("GitHub API path must be a repository-bound endpoint")
        endpoint = path[1:] if path.startswith("/") else path
        resource_path = endpoint.partition("?")[0]
        allowed_roots = (
            f"repos/{self.repository.full_name}",
            f"repositories/{self.repository.repository_id}",
        )
        if not any(
            resource_path == root or resource_path.startswith(f"{root}/")
            for root in allowed_roots
        ):
            raise ValueError("GitHub API path must be a repository-bound endpoint")
        if json_body is not None and not isinstance(json_body, dict):
            raise ValueError("GitHub API JSON body must be an object")
        args = [
            "api",
            "--hostname",
            self.repository.forge_host,
            "--method",
            method,
            endpoint,
        ]
        if json_body is not None:
            args.extend(["--input", "-"])
        if paginate:
            args.append("--paginate")
        environment = self._env | {
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_PAGER": "cat",
            "PAGER": "cat",
            "NO_COLOR": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
        }
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *args,
                stdin=asyncio.subprocess.PIPE if json_body is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            stdin = (
                json.dumps(json_body, separators=(",", ":")).encode()
                if json_body is not None
                else None
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin), timeout=self.timeout
            )
        except FileNotFoundError as exc:
            raise GitHubAppError("credentials", "GitHub CLI authentication is unavailable") from exc
        except asyncio.TimeoutError as exc:
            if process is not None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            raise GitHubAppError("transient", "GitHub CLI request timed out") from exc
        except asyncio.CancelledError:
            if process is not None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.shield(process.wait())
            raise
        if process.returncode != 0:
            raise _cli_error(process.returncode, stderr)
        if len(stdout) > self.max_response_bytes:
            raise GitHubAppError("transient", "GitHub response exceeded size limit")
        try:
            if not paginate:
                return json.loads(stdout)
            # Older supported gh versions emit successive JSON documents and
            # do not implement --slurp. Decode complete documents, not lines:
            # a page may be pretty-printed or adjacent to the next page.
            remaining = stdout.decode("utf-8").lstrip()
            decoder = json.JSONDecoder()
            pages = []
            while remaining:
                page, end = decoder.raw_decode(remaining)
                pages.append(page)
                remaining = remaining[end:].lstrip()
            return pages
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubAppError(
                "conflict_or_invalid", "GitHub response was not valid JSON"
            ) from exc


def _cli_error(returncode: int, stderr: bytes) -> GitHubAppError:
    diagnostic = stderr.decode("utf-8", "replace")
    status_match = re.search(r"\bHTTP\s+(\d{3})\b", diagnostic, flags=re.IGNORECASE)
    status = int(status_match.group(1)) if status_match else None
    if returncode == 4 or status == 401:
        category = "credentials"
    elif status == 404:
        category = "not_found_or_hidden"
    elif status in {409, 422}:
        category = "conflict_or_invalid"
    elif status in {403, 429} and "rate limit" in diagnostic.lower():
        category = "rate_limited"
    elif status == 403:
        category = "permission"
    else:
        category = "transient"
    return GitHubAppError(category, "GitHub CLI request failed")
