from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.claude_usage_request import ClaudeUsageRequest
from ...models.claude_usage_response import ClaudeUsageResponse
from ...models.claude_usage_response_422 import ClaudeUsageResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ClaudeUsageRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/claude-usage",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ClaudeUsageResponse | ClaudeUsageResponse422 | None:
    if response.status_code == 200:
        response_200 = ClaudeUsageResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ClaudeUsageResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ClaudeUsageResponse | ClaudeUsageResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ClaudeUsageRequest,
) -> Response[ClaudeUsageResponse | ClaudeUsageResponse422]:
    """Get Claude Code usage stats from live session data. Computes real token usage by scanning active
    session JSONL files in ~/.claude/projects/. Also reads subscription info from
    ~/.claude/.credentials.json.

     Get Claude Code usage stats from live session data. Computes real token usage by scanning active
    session JSONL files in ~/.claude/projects/. Also reads subscription info from
    ~/.claude/.credentials.json.

    Args:
        body (ClaudeUsageRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ClaudeUsageResponse | ClaudeUsageResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: ClaudeUsageRequest,
) -> ClaudeUsageResponse | ClaudeUsageResponse422 | None:
    """Get Claude Code usage stats from live session data. Computes real token usage by scanning active
    session JSONL files in ~/.claude/projects/. Also reads subscription info from
    ~/.claude/.credentials.json.

     Get Claude Code usage stats from live session data. Computes real token usage by scanning active
    session JSONL files in ~/.claude/projects/. Also reads subscription info from
    ~/.claude/.credentials.json.

    Args:
        body (ClaudeUsageRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ClaudeUsageResponse | ClaudeUsageResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ClaudeUsageRequest,
) -> Response[ClaudeUsageResponse | ClaudeUsageResponse422]:
    """Get Claude Code usage stats from live session data. Computes real token usage by scanning active
    session JSONL files in ~/.claude/projects/. Also reads subscription info from
    ~/.claude/.credentials.json.

     Get Claude Code usage stats from live session data. Computes real token usage by scanning active
    session JSONL files in ~/.claude/projects/. Also reads subscription info from
    ~/.claude/.credentials.json.

    Args:
        body (ClaudeUsageRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ClaudeUsageResponse | ClaudeUsageResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ClaudeUsageRequest,
) -> ClaudeUsageResponse | ClaudeUsageResponse422 | None:
    """Get Claude Code usage stats from live session data. Computes real token usage by scanning active
    session JSONL files in ~/.claude/projects/. Also reads subscription info from
    ~/.claude/.credentials.json.

     Get Claude Code usage stats from live session data. Computes real token usage by scanning active
    session JSONL files in ~/.claude/projects/. Also reads subscription info from
    ~/.claude/.credentials.json.

    Args:
        body (ClaudeUsageRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ClaudeUsageResponse | ClaudeUsageResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
