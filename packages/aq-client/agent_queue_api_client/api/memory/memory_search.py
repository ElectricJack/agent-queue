from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.memory_search_request import MemorySearchRequest
from ...models.memory_search_response import MemorySearchResponse
from ...models.memory_search_response_422 import MemorySearchResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: MemorySearchRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/memory/search",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> MemorySearchResponse | MemorySearchResponse422 | None:
    if response.status_code == 200:
        response_200 = MemorySearchResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = MemorySearchResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[MemorySearchResponse | MemorySearchResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MemorySearchRequest,
) -> Response[MemorySearchResponse | MemorySearchResponse422]:
    """Search project memory for relevant context. Returns semantically similar past task results, notes,
    and knowledge-base entries. Use this when the user asks about past work, wants to find related
    context, or says 'search memory', 'what do we know about', etc.

     Search project memory for relevant context. Returns semantically similar past task results, notes,
    and knowledge-base entries. Use this when the user asks about past work, wants to find related
    context, or says 'search memory', 'what do we know about', etc.

    Args:
        body (MemorySearchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MemorySearchResponse | MemorySearchResponse422]
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
    body: MemorySearchRequest,
) -> MemorySearchResponse | MemorySearchResponse422 | None:
    """Search project memory for relevant context. Returns semantically similar past task results, notes,
    and knowledge-base entries. Use this when the user asks about past work, wants to find related
    context, or says 'search memory', 'what do we know about', etc.

     Search project memory for relevant context. Returns semantically similar past task results, notes,
    and knowledge-base entries. Use this when the user asks about past work, wants to find related
    context, or says 'search memory', 'what do we know about', etc.

    Args:
        body (MemorySearchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MemorySearchResponse | MemorySearchResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MemorySearchRequest,
) -> Response[MemorySearchResponse | MemorySearchResponse422]:
    """Search project memory for relevant context. Returns semantically similar past task results, notes,
    and knowledge-base entries. Use this when the user asks about past work, wants to find related
    context, or says 'search memory', 'what do we know about', etc.

     Search project memory for relevant context. Returns semantically similar past task results, notes,
    and knowledge-base entries. Use this when the user asks about past work, wants to find related
    context, or says 'search memory', 'what do we know about', etc.

    Args:
        body (MemorySearchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MemorySearchResponse | MemorySearchResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: MemorySearchRequest,
) -> MemorySearchResponse | MemorySearchResponse422 | None:
    """Search project memory for relevant context. Returns semantically similar past task results, notes,
    and knowledge-base entries. Use this when the user asks about past work, wants to find related
    context, or says 'search memory', 'what do we know about', etc.

     Search project memory for relevant context. Returns semantically similar past task results, notes,
    and knowledge-base entries. Use this when the user asks about past work, wants to find related
    context, or says 'search memory', 'what do we know about', etc.

    Args:
        body (MemorySearchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MemorySearchResponse | MemorySearchResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
