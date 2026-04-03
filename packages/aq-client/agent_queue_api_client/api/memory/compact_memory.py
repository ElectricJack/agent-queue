from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.compact_memory_request import CompactMemoryRequest
from ...models.compact_memory_response import CompactMemoryResponse
from ...models.compact_memory_response_422 import CompactMemoryResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CompactMemoryRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/memory/compact",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CompactMemoryResponse | CompactMemoryResponse422 | None:
    if response.status_code == 200:
        response_200 = CompactMemoryResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CompactMemoryResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CompactMemoryResponse | CompactMemoryResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CompactMemoryRequest,
) -> Response[CompactMemoryResponse | CompactMemoryResponse422]:
    """Trigger memory compaction for a project. Groups task memories by age: recent (kept as-is), medium
    (LLM-summarized into weekly digests), old (deleted after digesting). Returns stats on tasks
    inspected, digests created, and files removed.

     Trigger memory compaction for a project. Groups task memories by age: recent (kept as-is), medium
    (LLM-summarized into weekly digests), old (deleted after digesting). Returns stats on tasks
    inspected, digests created, and files removed.

    Args:
        body (CompactMemoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CompactMemoryResponse | CompactMemoryResponse422]
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
    body: CompactMemoryRequest,
) -> CompactMemoryResponse | CompactMemoryResponse422 | None:
    """Trigger memory compaction for a project. Groups task memories by age: recent (kept as-is), medium
    (LLM-summarized into weekly digests), old (deleted after digesting). Returns stats on tasks
    inspected, digests created, and files removed.

     Trigger memory compaction for a project. Groups task memories by age: recent (kept as-is), medium
    (LLM-summarized into weekly digests), old (deleted after digesting). Returns stats on tasks
    inspected, digests created, and files removed.

    Args:
        body (CompactMemoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CompactMemoryResponse | CompactMemoryResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CompactMemoryRequest,
) -> Response[CompactMemoryResponse | CompactMemoryResponse422]:
    """Trigger memory compaction for a project. Groups task memories by age: recent (kept as-is), medium
    (LLM-summarized into weekly digests), old (deleted after digesting). Returns stats on tasks
    inspected, digests created, and files removed.

     Trigger memory compaction for a project. Groups task memories by age: recent (kept as-is), medium
    (LLM-summarized into weekly digests), old (deleted after digesting). Returns stats on tasks
    inspected, digests created, and files removed.

    Args:
        body (CompactMemoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CompactMemoryResponse | CompactMemoryResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CompactMemoryRequest,
) -> CompactMemoryResponse | CompactMemoryResponse422 | None:
    """Trigger memory compaction for a project. Groups task memories by age: recent (kept as-is), medium
    (LLM-summarized into weekly digests), old (deleted after digesting). Returns stats on tasks
    inspected, digests created, and files removed.

     Trigger memory compaction for a project. Groups task memories by age: recent (kept as-is), medium
    (LLM-summarized into weekly digests), old (deleted after digesting). Returns stats on tasks
    inspected, digests created, and files removed.

    Args:
        body (CompactMemoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CompactMemoryResponse | CompactMemoryResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
