from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.memory_reindex_request import MemoryReindexRequest
from ...models.memory_reindex_response import MemoryReindexResponse
from ...models.memory_reindex_response_422 import MemoryReindexResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: MemoryReindexRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/memory/reindex",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> MemoryReindexResponse | MemoryReindexResponse422 | None:
    if response.status_code == 200:
        response_200 = MemoryReindexResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = MemoryReindexResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[MemoryReindexResponse | MemoryReindexResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MemoryReindexRequest,
) -> Response[MemoryReindexResponse | MemoryReindexResponse422]:
    """Force a full reindex of a project's memory. Re-scans all markdown files in memory/ and notes/
    directories, re-embeds changed content, and updates the vector index. Use when memory seems stale or
    after bulk file changes.

     Force a full reindex of a project's memory. Re-scans all markdown files in memory/ and notes/
    directories, re-embeds changed content, and updates the vector index. Use when memory seems stale or
    after bulk file changes.

    Args:
        body (MemoryReindexRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MemoryReindexResponse | MemoryReindexResponse422]
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
    body: MemoryReindexRequest,
) -> MemoryReindexResponse | MemoryReindexResponse422 | None:
    """Force a full reindex of a project's memory. Re-scans all markdown files in memory/ and notes/
    directories, re-embeds changed content, and updates the vector index. Use when memory seems stale or
    after bulk file changes.

     Force a full reindex of a project's memory. Re-scans all markdown files in memory/ and notes/
    directories, re-embeds changed content, and updates the vector index. Use when memory seems stale or
    after bulk file changes.

    Args:
        body (MemoryReindexRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MemoryReindexResponse | MemoryReindexResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MemoryReindexRequest,
) -> Response[MemoryReindexResponse | MemoryReindexResponse422]:
    """Force a full reindex of a project's memory. Re-scans all markdown files in memory/ and notes/
    directories, re-embeds changed content, and updates the vector index. Use when memory seems stale or
    after bulk file changes.

     Force a full reindex of a project's memory. Re-scans all markdown files in memory/ and notes/
    directories, re-embeds changed content, and updates the vector index. Use when memory seems stale or
    after bulk file changes.

    Args:
        body (MemoryReindexRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MemoryReindexResponse | MemoryReindexResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: MemoryReindexRequest,
) -> MemoryReindexResponse | MemoryReindexResponse422 | None:
    """Force a full reindex of a project's memory. Re-scans all markdown files in memory/ and notes/
    directories, re-embeds changed content, and updates the vector index. Use when memory seems stale or
    after bulk file changes.

     Force a full reindex of a project's memory. Re-scans all markdown files in memory/ and notes/
    directories, re-embeds changed content, and updates the vector index. Use when memory seems stale or
    after bulk file changes.

    Args:
        body (MemoryReindexRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MemoryReindexResponse | MemoryReindexResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
