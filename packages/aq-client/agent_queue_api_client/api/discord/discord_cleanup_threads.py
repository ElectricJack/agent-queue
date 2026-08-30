from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.discord_cleanup_threads_request import DiscordCleanupThreadsRequest
from ...models.discord_cleanup_threads_response import DiscordCleanupThreadsResponse
from ...models.discord_cleanup_threads_response_422 import DiscordCleanupThreadsResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: DiscordCleanupThreadsRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/discord/cleanup-threads",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422 | None:
    if response.status_code == 200:
        response_200 = DiscordCleanupThreadsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = DiscordCleanupThreadsResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DiscordCleanupThreadsRequest,
) -> Response[DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422]:
    """Archive or delete threads in a Discord channel. Defaults to mode='archive' and only_closed=true, so
    threads for running tasks are left alone. Dry-run unless confirm=true.

     Archive or delete threads in a Discord channel. Defaults to mode='archive' and only_closed=true, so
    threads for running tasks are left alone. Dry-run unless confirm=true.

    Args:
        body (DiscordCleanupThreadsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422]
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
    body: DiscordCleanupThreadsRequest,
) -> DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422 | None:
    """Archive or delete threads in a Discord channel. Defaults to mode='archive' and only_closed=true, so
    threads for running tasks are left alone. Dry-run unless confirm=true.

     Archive or delete threads in a Discord channel. Defaults to mode='archive' and only_closed=true, so
    threads for running tasks are left alone. Dry-run unless confirm=true.

    Args:
        body (DiscordCleanupThreadsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DiscordCleanupThreadsRequest,
) -> Response[DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422]:
    """Archive or delete threads in a Discord channel. Defaults to mode='archive' and only_closed=true, so
    threads for running tasks are left alone. Dry-run unless confirm=true.

     Archive or delete threads in a Discord channel. Defaults to mode='archive' and only_closed=true, so
    threads for running tasks are left alone. Dry-run unless confirm=true.

    Args:
        body (DiscordCleanupThreadsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DiscordCleanupThreadsRequest,
) -> DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422 | None:
    """Archive or delete threads in a Discord channel. Defaults to mode='archive' and only_closed=true, so
    threads for running tasks are left alone. Dry-run unless confirm=true.

     Archive or delete threads in a Discord channel. Defaults to mode='archive' and only_closed=true, so
    threads for running tasks are left alone. Dry-run unless confirm=true.

    Args:
        body (DiscordCleanupThreadsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DiscordCleanupThreadsResponse | DiscordCleanupThreadsResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
