from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_chain_health_request import GetChainHealthRequest
from ...models.get_chain_health_response import GetChainHealthResponse
from ...models.get_chain_health_response_422 import GetChainHealthResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GetChainHealthRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/get-chain-health",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetChainHealthResponse | GetChainHealthResponse422 | None:
    if response.status_code == 200:
        response_200 = GetChainHealthResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetChainHealthResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetChainHealthResponse | GetChainHealthResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetChainHealthRequest,
) -> Response[GetChainHealthResponse | GetChainHealthResponse422]:
    """Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id
    for a specific task, or project_id for all stuck chains in a project.

     Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id
    for a specific task, or project_id for all stuck chains in a project.

    Args:
        body (GetChainHealthRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetChainHealthResponse | GetChainHealthResponse422]
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
    body: GetChainHealthRequest,
) -> GetChainHealthResponse | GetChainHealthResponse422 | None:
    """Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id
    for a specific task, or project_id for all stuck chains in a project.

     Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id
    for a specific task, or project_id for all stuck chains in a project.

    Args:
        body (GetChainHealthRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetChainHealthResponse | GetChainHealthResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetChainHealthRequest,
) -> Response[GetChainHealthResponse | GetChainHealthResponse422]:
    """Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id
    for a specific task, or project_id for all stuck chains in a project.

     Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id
    for a specific task, or project_id for all stuck chains in a project.

    Args:
        body (GetChainHealthRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetChainHealthResponse | GetChainHealthResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetChainHealthRequest,
) -> GetChainHealthResponse | GetChainHealthResponse422 | None:
    """Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id
    for a specific task, or project_id for all stuck chains in a project.

     Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id
    for a specific task, or project_id for all stuck chains in a project.

    Args:
        body (GetChainHealthRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetChainHealthResponse | GetChainHealthResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
